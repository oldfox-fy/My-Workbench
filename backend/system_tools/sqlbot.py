# backend/system_tools/sqlbot.py
"""
SQLBot 智能问数工具（M2）。

通过 MCP 协议调用本地 Docker 部署的 SQLBot（dataease/SQLBot），
把「获取 token → 建会话 → 提问」的流程封装成一个工具，避免 LLM
直接操作 SQLBot 的原始 MCP 工具（access_token / mcp_start / mcp_question）。

依赖：
  - mcp_config.json 中已注册 sqlbot 服务器（url + transport: "sse"）
  - app_config.yaml 的 sqlbot 节提供账号密码（默认 admin / SQLBot@123456）
"""
import json
import time
from typing import Any, Dict, Optional

from config_loader import config as app_config
from backend.database import get_db


# 模块级 token 缓存：SQLBot 的 token 有效期 8 天，这里缓存 6 小时，
# 既避免每次问数都重新认证，又能在 SECRET_KEY 变更后较快自愈。
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_TOKEN_TTL = 6 * 60 * 60


def _sqlbot_config() -> Dict[str, Any]:
    sc = app_config.raw_config.get("sqlbot", {}) or {}
    return {
        "enabled": bool(sc.get("enabled", True)),
        "username": sc.get("username", "admin") or "admin",
        "password": sc.get("password", "SQLBot@123456") or "",
        "server_name": sc.get("server_name", "sqlbot") or "sqlbot",
    }


def _tool_name(server_name: str, tool: str) -> str:
    return f"mcp_{server_name}__{tool}"


def _parse_json(text: Any) -> Any:
    """把 MCP 工具返回的文本解析成对象（已经是 dict/list 则原样返回）。"""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 非 JSON（如纯文本错误消息），包一层返回
        return {"raw": text}


def _unwrap_sqlbot(data: Any) -> Any:
    """解包 SQLBot 的响应信封 {code, data, msg}。

    SQLBot 的 ResponseMiddleware 会把所有 JSON 响应包装成
    {"code": 0, "data": <实际负载>, "msg": null}，code=0 表示成功。
    这里取出实际负载；若 code 非 0 则抛错。
    """
    if isinstance(data, dict) and "code" in data and "data" in data:
        code = data.get("code")
        if code not in (0, None):
            msg = data.get("msg") or data.get("data")
            raise RuntimeError(f"SQLBot 返回错误 code={code}：{str(msg)[:300]}")
        return data.get("data")
    return data


async def _get_token(mcp_manager) -> str:
    """获取 SQLBot 的 access token（带缓存）。"""
    cfg = _sqlbot_config()
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    result = await mcp_manager.call_tool(
        _tool_name(cfg["server_name"], "access_token"),
        {"username": cfg["username"], "password": cfg["password"]},
    )
    payload = _unwrap_sqlbot(_parse_json(result))
    token = None
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
    if not token:
        raise RuntimeError(f"SQLBot 认证失败，返回：{str(result)[:200]}")

    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + _TOKEN_TTL
    return token


def _clear_token_cache() -> None:
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0


async def _resolve_custom_model(llm_service) -> Optional[str]:
    """由当前对话模型推导 SQLBot 侧的模型 id（custom_model）。

    逻辑：当前 LLM 的 modelName → My Workbench models 表 id → sqlbot_model_map
    的 sqlbot_model_id。找不到时返回 None（SQLBot 回退用其默认模型）。
    """
    model_name = getattr(llm_service, "model_name", None)
    if not model_name:
        return None

    db = await get_db()
    try:
        # 同 modelName 可能被多个用户配置，优先 default 角色
        cursor = await db.execute(
            "SELECT id FROM models WHERE modelName = ? ORDER BY (role='default') DESC, name LIMIT 1",
            (model_name,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        wb_id = row[0]
        cursor = await db.execute(
            "SELECT sqlbot_model_id FROM sqlbot_model_map WHERE wb_model_id = ?",
            (wb_id,),
        )
        mrow = await cursor.fetchone()
        return mrow[0] if mrow else None
    finally:
        await db.close()


async def sqlbot_ask(
    question: str,
    datasource_id: Optional[int] = None,
    custom_model: Optional[str] = None,
    mcp_manager=None,
    llm_service=None,
    **_kwargs,
) -> Dict[str, Any]:
    """向 SQLBot 提问，返回问数结果。

    Args:
        question: 要问的自然语言问题，例如「上个月各产品线的销售额是多少？」
        datasource_id: 数据源 ID（可选）。省略时使用会话默认数据源。
        custom_model: SQLBot 侧的大模型 ID（可选）。省略时自动取当前对话模型
                      同步到 SQLBot 的对应模型（模型实时同步的落点）。
        mcp_manager: MCP 客户端管理器（由 execute_tool 注入）。
        llm_service: 当前 LLM 服务实例（由 execute_tool 注入，用于推导当前模型）。
    """
    cfg = _sqlbot_config()
    if not cfg["enabled"]:
        return {"success": False, "error": "SQLBot 未启用，请先在 app_config.yaml 的 sqlbot 节开启 enabled。"}
    if not mcp_manager:
        return {"success": False, "error": "MCP 管理器不可用，无法调用 SQLBot。"}

    server_name = cfg["server_name"]
    status = mcp_manager.get_server_status(server_name) if hasattr(mcp_manager, "get_server_status") else None
    if status is not None and not status.get("connected"):
        return {
            "success": False,
            "error": f"SQLBot MCP 服务器 '{server_name}' 未连接。请确认本地 SQLBot 已启动（docker compose up -d，http://localhost:8001）。",
        }

    # 未显式指定 custom_model 时，用当前对话模型在映射表里的 SQLBot 模型 id。
    if custom_model is None:
        try:
            custom_model = await _resolve_custom_model(llm_service)
        except Exception as e:
            # 映射解析失败不阻断问数，回退 SQLBot 默认模型
            custom_model = None

    try:
        token = await _get_token(mcp_manager)

        # 每次问数新建一个 MCP 会话，拿到 chat_id（各次提问相互独立）
        start_res = await mcp_manager.call_tool(
            _tool_name(server_name, "mcp_start"),
            {"token": token},
        )
        start_data = _unwrap_sqlbot(_parse_json(start_res))
        chat_id = start_data.get("chat_id") if isinstance(start_data, dict) else None
        if not chat_id:
            return {"success": False, "error": f"SQLBot 创建会话失败，返回：{str(start_res)[:200]}"}

        args = {
            "question": question,
            "chat_id": chat_id,
            "token": token,
            "stream": False,   # 关闭流式，一次返回完整 JSON
            "lang": "zh-CN",
            "return_img": True,  # 返回图表（图片 URL），由前端渲染
        }
        if datasource_id is not None:
            args["datasource_id"] = datasource_id
        if custom_model is not None:
            args["custom_model"] = custom_model

        answer = await mcp_manager.call_tool(
            _tool_name(server_name, "mcp_question"),
            args,
        )
        result = _unwrap_sqlbot(_parse_json(answer))
        out: Dict[str, Any] = {"success": True, "content": result}
        # 若 SQLBot 返回了图表图片链接，附加 Markdown，方便模型在最终回答中直接展示图表。
        if isinstance(result, dict) and result.get("image_url"):
            title = result.get("title") or "图表"
            out["chart_markdown"] = f"![{title}]({result['image_url']})"
        return out
    except Exception as e:
        # token 失效等场景：清缓存，让下次调用重新认证
        _clear_token_cache()
        return {"success": False, "error": f"SQLBot 问数失败：{e}"}
