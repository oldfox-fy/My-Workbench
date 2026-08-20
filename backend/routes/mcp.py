# backend/routes/mcp.py
import json
import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import List, Optional
from backend.db.kb_settings import get_setting, set_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_MCP_KEY = "mcp_servers"


class MCPServerRequest(BaseModel):
    name: str
    transport: str = "http"          # 'http'（远程 URL）或 'stdio'（本地命令）
    url: Optional[str] = None        # transport=http 时使用
    command: Optional[str] = None    # transport=stdio 时使用
    args: List[str] = []             # transport=stdio 时使用
    api_key: Optional[str] = None    # transport=http 时使用的鉴权 key（转成 Authorization: Bearer）


async def get_mcp_manager(request: Request):
    return request.app.state.mcp_manager


def _to_server_config(req: MCPServerRequest, existing: Optional[dict] = None) -> dict:
    """将请求转换为存储用的 server 配置片段。existing 为已保存的旧配置，用于编辑时保留未填写的字段。"""
    if req.transport == "stdio":
        if not req.command:
            raise HTTPException(400, "本地（stdio）服务必须提供命令 command")
        return {"command": req.command, "args": req.args or []}
    else:
        if not req.url:
            raise HTTPException(400, "远程（http）服务必须提供 URL")
        cfg = {"url": req.url}
        headers = {}
        if req.api_key:
            headers["Authorization"] = f"Bearer {req.api_key}"
        elif existing:
            # 编辑时未填写 key，保留原有 headers，避免清空已保存的鉴权信息
            headers = dict(existing.get("headers") or {})
        if headers:
            cfg["headers"] = headers
        return cfg


async def _read_user_config(user_id: int) -> dict:
    """读取指定用户的 MCP 配置，不存在则返回空结构"""
    raw = await get_setting(_MCP_KEY, user_id)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "mcpServers" in data:
                return data
        except json.JSONDecodeError:
            pass
    return {"mcpServers": {}}


async def _write_user_config(user_id: int, data: dict):
    """保存指定用户的 MCP 配置"""
    await set_setting(_MCP_KEY, json.dumps(data, ensure_ascii=False), user_id)


@router.get("/servers")
async def list_servers(request: Request, mcp_manager=Depends(get_mcp_manager)):
    """列出当前用户已配置的 MCP 服务器，并合并实时连接状态"""
    user_id = request.state.user["id"]
    data = await _read_user_config(user_id)
    servers = []
    for name, cfg in data["mcpServers"].items():
        transport = "stdio" if ("command" in cfg or "commad" in cfg) else "http"
        status = mcp_manager.get_server_status(name) if mcp_manager else {"connected": False, "tools": []}
        servers.append({
            "name": name,
            "transport": transport,
            "url": cfg.get("url"),
            "command": cfg.get("command") or cfg.get("commad"),
            "args": cfg.get("args", []),
            "has_api_key": bool((cfg.get("headers") or {}).get("Authorization")),
            "connected": status["connected"],
            "tools": status["tools"],
        })
    return {"servers": servers}


@router.post("/servers")
async def save_server(req: MCPServerRequest, request: Request, mcp_manager=Depends(get_mcp_manager)):
    """新增或更新一个 MCP 服务器：写入用户配置并立即热连接"""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "服务名称不能为空")

    user_id = request.state.user["id"]
    data = await _read_user_config(user_id)
    existing = data["mcpServers"].get(name)
    server_config = _to_server_config(req, existing)

    # 1. 写入用户配置
    data["mcpServers"][name] = server_config
    await _write_user_config(user_id, data)

    # 2. 热连接（若管理器尚未就绪则仅保存配置，重启后生效）
    if not mcp_manager:
        return {"status": "saved", "connected": False, "tools": [],
                "error": "MCP 服务尚未就绪，配置已保存，重启后生效"}

    result = await mcp_manager.add_server(name, server_config)
    return {
        "status": "ok" if result["success"] else "error",
        "connected": result["success"],
        "tools": result["tools"],
        "error": result["error"],
    }


@router.delete("/servers/{name}")
async def delete_server(name: str, request: Request, mcp_manager=Depends(get_mcp_manager)):
    """删除一个 MCP 服务器：断开连接并从用户配置移除"""
    user_id = request.state.user["id"]
    data = await _read_user_config(user_id)
    if name in data["mcpServers"]:
        del data["mcpServers"][name]
        await _write_user_config(user_id, data)
    if mcp_manager:
        await mcp_manager.remove_server(name)
    return {"status": "ok"}
