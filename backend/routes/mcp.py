# backend/routes/mcp.py
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import List, Optional, Dict
from config_loader import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MCPServerRequest(BaseModel):
    name: str
    transport: str = "http"          # 'http'（远程 URL）或 'stdio'（本地命令）
    url: Optional[str] = None        # transport=http 时使用
    command: Optional[str] = None    # transport=stdio 时使用
    args: List[str] = []             # transport=stdio 时使用


async def get_mcp_manager(request: Request):
    return request.app.state.mcp_manager


def _read_config() -> dict:
    """读取 mcp_config.json，不存在则返回空结构"""
    path = Path(config.mcp_config_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"mcpServers": {}}
    if "mcpServers" not in data or not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    return data


def _write_config(data: dict):
    """整体写回 mcp_config.json"""
    path = Path(config.mcp_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _to_server_config(req: MCPServerRequest) -> dict:
    """将请求转换为存储用的 server 配置片段"""
    if req.transport == "stdio":
        if not req.command:
            raise HTTPException(400, "本地（stdio）服务必须提供命令 command")
        return {"command": req.command, "args": req.args or []}
    else:
        if not req.url:
            raise HTTPException(400, "远程（http）服务必须提供 URL")
        return {"url": req.url}


@router.get("/servers")
async def list_servers(mcp_manager=Depends(get_mcp_manager)):
    """列出所有已配置的 MCP 服务器，并合并实时连接状态"""
    data = _read_config()
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
            "connected": status["connected"],
            "tools": status["tools"],
        })
    return {"servers": servers}


@router.post("/servers")
async def save_server(req: MCPServerRequest, mcp_manager=Depends(get_mcp_manager)):
    """新增或更新一个 MCP 服务器：写入配置文件并立即热连接"""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "服务名称不能为空")

    server_config = _to_server_config(req)

    # 1. 写入配置文件（整体读-改-写，避免覆盖其他条目）
    data = _read_config()
    data["mcpServers"][name] = server_config
    _write_config(data)

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
async def delete_server(name: str, mcp_manager=Depends(get_mcp_manager)):
    """删除一个 MCP 服务器：断开连接并从配置文件移除"""
    data = _read_config()
    if name in data["mcpServers"]:
        del data["mcpServers"][name]
        _write_config(data)
    if mcp_manager:
        await mcp_manager.remove_server(name)
    return {"status": "ok"}
