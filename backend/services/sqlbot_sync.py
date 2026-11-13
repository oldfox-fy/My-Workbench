# backend/services/sqlbot_sync.py
"""
SQLBot 模型同步（M3）。

把 My Workbench 配置的大模型「实时同步」到本地 SQLBot（dataease/SQLBot），
使 SQLBot 问数复用工作台当前模型，无需在 SQLBot 控制台重复配置。

流程（幂等，可反复执行）：
  1. 用 admin 账号拿 JWT —— POST /api/v1/mcp/access_token（REST 端口 rest_url=8000）
  2. 拿工作空间 id —— POST /api/v1/mcp/mcp_ws_list?token=<jwt>
  3. 遍历 My Workbench 的「对话类」模型（default/vision/reasoning）→ 建/改 SQLBot 模型 + 绑定工作空间
  4. 首个（default 角色优先）模型显式设为 SQLBot 默认模型
  5. 删除 SQLBot 侧孤儿模型（先取消默认再删除）
  6. 回写 sqlbot_model_map 映射表

鉴权头：X-SQLBOT-TOKEN: Bearer <jwt>（非标准 Authorization）。
所有响应都被 ResponseMiddleware 包装成 {code, data, msg}，用 _unwrap 解包。
"""
import json
import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from config_loader import config as app_config
from backend.database import get_db


# 模块级 token 缓存：SQLBot token 有效期 8 天，缓存 6 小时。
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_TOKEN_TTL = 6 * 60 * 60

# 串行化同步：模型 CRUD 可能短时间内连续触发多次同步，加锁避免并发
# 双写/重复建模型（同步本身幂等，但并发会短暂读到旧映射导致重复 POST）。
_sync_lock = asyncio.Lock()


def _rest_url() -> str:
    return (getattr(app_config, "sqlbot_rest_url", "") or "http://localhost:8000").rstrip("/")


def _enabled() -> bool:
    return bool(getattr(app_config, "sqlbot_enabled", True))


def _unwrap(data: Any) -> Any:
    """解包 SQLBot 响应信封 {code, data, msg}。"""
    if isinstance(data, dict) and "code" in data and "data" in data:
        code = data.get("code")
        if code not in (0, None):
            msg = data.get("msg") or data.get("data")
            raise RuntimeError(f"SQLBot 返回错误 code={code}：{str(msg)[:300]}")
        return data.get("data")
    return data


async def _get_token(client: httpx.AsyncClient) -> str:
    """REST 方式获取 SQLBot JWT（带缓存）。"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    username = getattr(app_config, "sqlbot_username", "admin") or "admin"
    password = getattr(app_config, "sqlbot_password", "") or ""
    resp = await client.post(
        f"{_rest_url()}/api/v1/mcp/access_token",
        json={"username": username, "password": password},
    )
    resp.raise_for_status()
    token = _unwrap(resp.json()).get("access_token")
    if not token:
        raise RuntimeError("SQLBot 认证失败：access_token 为空")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + _TOKEN_TTL
    return token


def _clear_token_cache() -> None:
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0


async def _get_workspace_ids(client: httpx.AsyncClient, token: str) -> List[str]:
    """返回 SQLBot 当前账号可见的工作空间 id 列表（默认工作空间 id=1）。"""
    resp = await client.post(
        f"{_rest_url()}/api/v1/mcp/mcp_ws_list",
        params={"token": token},
    )
    resp.raise_for_status()
    data = _unwrap(resp.json())
    ids: List[str] = []
    if isinstance(data, list):
        for w in data:
            wid = w.get("id") if isinstance(w, dict) else None
            if wid is not None:
                ids.append(str(wid))
    return ids


async def _list_sqlbot_models(client: httpx.AsyncClient, token: str) -> Dict[str, Dict]:
    """列出 SQLBot 已配置的模型，返回 {sqlbot_model_id: model}。"""
    resp = await client.get(
        f"{_rest_url()}/api/v1/system/aimodel",
        headers={"X-SQLBOT-TOKEN": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = _unwrap(resp.json())
    result: Dict[str, Dict] = {}
    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("grid") or data.get("list") or data.get("records") or []
    for m in items:
        if isinstance(m, dict) and m.get("id") is not None:
            result[str(m["id"])] = m
    return result


def _build_payload(m: Dict[str, Any], is_default: bool) -> Dict[str, Any]:
    """My Workbench 模型 → SQLBot AiModelCreator/AiModelEditor 请求体。"""
    return {
        "name": m.get("name") or m.get("modelName") or "未命名模型",
        "model_type": 0,                       # 0 = 对话
        "base_model": m.get("modelName") or m.get("name") or "",
        "supplier": 15,                        # 15 = 通用 OpenAI 兼容
        "protocol": 1,                         # 1 = openai 兼容
        "api_domain": m.get("baseUrl") or "",
        "api_key": m.get("apiKey") or "",
        "default_model": bool(is_default),
        "config_list": [],                     # 必传，可为空数组
    }


async def sync_models_to_sqlbot() -> Dict[str, Any]:
    """把 My Workbench 全部模型同步到 SQLBot（幂等）。返回统计信息。"""
    if not _enabled():
        return {"ok": False, "error": "SQLBot 未启用"}

    async with _sync_lock:
        return await _sync_models_to_sqlbot_locked()


async def _sync_models_to_sqlbot_locked() -> Dict[str, Any]:
    """sync_models_to_sqlbot 的加锁实现。"""

    # ── 读取 My Workbench 模型 + 现有映射 ──
    db = await get_db()
    try:
        cols = ["id", "name", "type", "modelName", "baseUrl", "apiKey", "role", "user_id"]
        # 只同步「对话类」模型（default/vision/reasoning）；生图/视频/语音走各自
        # 专用 API，对 text-to-SQL 无用，同步过去只会占 SQLBot 模型槽并可能报错。
        cursor = await db.execute(
            "SELECT id, name, type, modelName, baseUrl, apiKey, role, user_id "
            "FROM models WHERE role NOT IN ('image_gen', 'video', 'audio') "
            "ORDER BY (role='default') DESC, name"
        )
        wb_models = [dict(zip(cols, row)) for row in await cursor.fetchall()]

        cursor = await db.execute("SELECT wb_model_id, sqlbot_model_id, workspace_id FROM sqlbot_model_map")
        map_rows = {r[0]: r for r in await cursor.fetchall()}  # wb_id -> (wb_id, sqlbot_id, ws_id)
    finally:
        await db.close()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    ) as client:
        token = await _get_token(client)
        headers = {"X-SQLBOT-TOKEN": f"Bearer {token}"}

        ws_ids = await _get_workspace_ids(client, token) or ["1"]
        existing = await _list_sqlbot_models(client, token)

        stats = {"created": 0, "updated": 0, "deleted": 0, "bound": 0, "skipped": 0}
        final_map: Dict[str, str] = {}  # wb_model_id -> sqlbot_model_id
        first_sid: Optional[str] = None

        for i, m in enumerate(wb_models):
            wb_id = m["id"]
            payload = _build_payload(m, is_default=(i == 0))
            sid = map_rows.get(wb_id, (None, None, None))[1]

            if sid and sid in existing:
                body = {**payload, "id": sid}
                resp = await client.put(
                    f"{_rest_url()}/api/v1/system/aimodel", json=body, headers=headers
                )
                resp.raise_for_status()
                stats["updated"] += 1
            else:
                resp = await client.post(
                    f"{_rest_url()}/api/v1/system/aimodel", json=payload, headers=headers
                )
                resp.raise_for_status()
                detail = _unwrap(resp.json())
                sid = str(detail.get("id")) if isinstance(detail, dict) else None
                if not sid:
                    raise RuntimeError(f"SQLBot 创建模型失败：{str(detail)[:200]}")
                stats["created"] += 1

            # 绑定工作空间（覆盖绑定，幂等）
            resp = await client.put(
                f"{_rest_url()}/api/v1/system/aimodel/{sid}/ws_mapping",
                json=ws_ids, headers=headers,
            )
            resp.raise_for_status()
            stats["bound"] += 1
            final_map[wb_id] = sid
            if i == 0:
                first_sid = sid

        # ── 显式把首个模型设为默认（清掉 SQLBot 控制台里手动配的旧默认） ──
        if first_sid:
            try:
                resp = await client.put(
                    f"{_rest_url()}/api/v1/system/aimodel/default/{first_sid}",
                    headers=headers,
                )
                resp.raise_for_status()
            except Exception as e:
                # 设默认失败不影响问数（custom_model 会显式传模型 id）
                stats["skipped"] += 1
                _log(f"SQLBot 设置默认模型失败（非致命）: {e}")

        # ── 删除孤儿模型（My Workbench 已删但 SQLBot 还在） ──
        # 设默认之后重新拉取列表，拿到最新的 default_model 标志（旧默认已转移）。
        keep_ids = set(final_map.values())
        try:
            existing = await _list_sqlbot_models(client, token)
        except Exception as e:
            _log(f"SQLBot 重新拉取模型列表失败（跳过孤儿清理）: {e}")
            existing = {}

        for sid, sqlbot_model in existing.items():
            if sid in keep_ids:
                continue
            try:
                # 默认模型不可删：先通过完整 payload 更新取消默认，再删除。
                if sqlbot_model.get("default_model"):
                    unset = {
                        "id": sid,
                        "name": sqlbot_model.get("name") or "orphan",
                        "model_type": sqlbot_model.get("model_type", 0),
                        "base_model": sqlbot_model.get("base_model") or "",
                        "supplier": sqlbot_model.get("supplier", 15),
                        "protocol": sqlbot_model.get("protocol", 1),
                        "api_domain": "",
                        "api_key": "",
                        "default_model": False,
                        "config_list": [],
                    }
                    resp = await client.put(
                        f"{_rest_url()}/api/v1/system/aimodel", json=unset, headers=headers
                    )
                    resp.raise_for_status()
                resp = await client.delete(
                    f"{_rest_url()}/api/v1/system/aimodel/{sid}", headers=headers
                )
                resp.raise_for_status()
                stats["deleted"] += 1
            except Exception as e:
                stats["skipped"] += 1
                _log(f"SQLBot 删除孤儿模型失败（非致命）: {e}")

        # ── 回写映射表 ──
        db = await get_db()
        try:
            await db.execute("DELETE FROM sqlbot_model_map")
            for wb_id, sid in final_map.items():
                m = next((x for x in wb_models if x["id"] == wb_id), None)
                user_id = m.get("user_id") if m else None
                await db.execute(
                    "INSERT INTO sqlbot_model_map (wb_model_id, sqlbot_model_id, user_id, workspace_id) "
                    "VALUES (?, ?, ?, ?)",
                    (wb_id, sid, user_id, ",".join(ws_ids)),
                )
            await db.commit()
        finally:
            await db.close()

    stats["ok"] = True
    stats["workspace_ids"] = ws_ids
    return stats


def _log(msg: str) -> None:
    try:
        from backend.bootstrap import logger
        logger.warning(msg)
    except Exception:
        print(msg)


async def get_sqlbot_status() -> Dict[str, Any]:
    """返回 SQLBot 连接与同步状态，供设置面板 / 状态查询使用。"""
    if not _enabled():
        return {"enabled": False, "connected": False, "message": "SQLBot 未启用"}

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM models")
        wb_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM sqlbot_model_map")
        map_count = (await cursor.fetchone())[0]
    finally:
        await db.close()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
        ) as client:
            token = await _get_token(client)
            headers = {"X-SQLBOT-TOKEN": f"Bearer {token}"}
            sqlbot_models = await _list_sqlbot_models(client, token)
            ws_ids = await _get_workspace_ids(client, token) or ["1"]
    except Exception as e:
        return {
            "enabled": True,
            "connected": False,
            "message": f"SQLBot 连接失败：{e}",
            "wb_models": wb_count,
            "synced": map_count,
        }

    return {
        "enabled": True,
        "connected": True,
        "message": "已连接",
        "rest_url": _rest_url(),
        "workspace_ids": ws_ids,
        "wb_models": wb_count,
        "sqlbot_models": len(sqlbot_models),
        "synced": map_count,
    }
