# backend/services/sqlbot_datasource.py
"""
SQLBot 数据源管理（M4）。

把 My Workbench 设置面板里的「连接数据库」操作代理到本地 SQLBot 的数据源 REST 接口，
复用 sqlbot_sync 里的 token / rest_url / 解包助手。

关键点：
  - SQLBot 数据源 `configuration` 字段是「AES-ECB-PKCS7 加密 + base64」的 JSON 字符串，
    密钥硬编码为 `SQLBot1234567890`（与 SQLBot 前端 CryptoJS 完全一致）。
  - 列表用 `/api/v1/mcp/mcp_ds_list`（会剥离 configuration 等敏感字段）。
  - 新增/校验/删除走 `/api/v1/datasource/{add,check,delete}`。
  - `check` 失败时 SQLBot 直接返回 500 + 纯文本 detail（不是 {code,data,msg} 信封），需单独处理。
"""
import json
import base64
from typing import Any, Dict, List

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from config_loader import config as app_config
from backend.services.sqlbot_sync import _get_token, _rest_url, _unwrap, _enabled


_SQLBOT_AES_KEY = b"SQLBot1234567890"

# 支持的数据库类型（type → 显示名 + 默认端口）。type 值必须与 SQLBot DB 枚举一致。
DATASOURCE_TYPES: List[Dict[str, Any]] = [
    {"type": "mysql", "name": "MySQL", "default_port": 3306},
    {"type": "pg", "name": "PostgreSQL", "default_port": 5432},
    {"type": "sqlServer", "name": "SQL Server", "default_port": 1433},
    {"type": "oracle", "name": "Oracle", "default_port": 1521},
    {"type": "ck", "name": "ClickHouse", "default_port": 8123},
    {"type": "doris", "name": "Apache Doris", "default_port": 9030},
    {"type": "starrocks", "name": "StarRocks", "default_port": 9030},
    {"type": "hive", "name": "Apache Hive", "default_port": 10000},
    {"type": "dm", "name": "达梦", "default_port": 5236},
    {"type": "kingbase", "name": "Kingbase", "default_port": 54321},
]

_VALID_TYPES = {t["type"] for t in DATASOURCE_TYPES}


def encrypt_configuration(conf: Dict[str, Any]) -> str:
    """AES-ECB-PKCS7 加密 + base64（与 SQLBot 前端 CryptoJS.AES 一致）。"""
    raw = json.dumps(conf, ensure_ascii=False)
    padder = PKCS7(128).padder()
    padded = padder.update(raw.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(_SQLBOT_AES_KEY), modes.ECB()).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _build_configuration(ds: Dict[str, Any]) -> str:
    """由前端提交的连接字段构建加密后的 configuration 字符串。"""
    conf = {
        "host": ds.get("host") or "",
        "port": int(ds.get("port") or 0),
        "username": ds.get("username") or "",
        "password": ds.get("password") or "",
        "database": ds.get("database") or "",
        "dbSchema": ds.get("dbSchema") or "",
        "extraJdbc": ds.get("extraJdbc") or "",
        "ssl": bool(ds.get("ssl", False)),
        "timeout": int(ds.get("timeout") or 30),
        "poolSize": int(ds.get("poolSize") or 5),
        "engine": ds.get("engine") or "hive",
    }
    return encrypt_configuration(conf)


def _ds_body(ds: Dict[str, Any]) -> Dict[str, Any]:
    """构建 SQLBot CreateDatasource/CoreDatasource 请求体。"""
    return {
        "name": (ds.get("name") or "").strip(),
        "description": ds.get("description") or "",
        "type": ds.get("type") or "",
        "configuration": _build_configuration(ds),
    }


async def _headers(client: httpx.AsyncClient) -> Dict[str, str]:
    token = await _get_token(client)
    return {"X-SQLBOT-TOKEN": f"Bearer {token}"}


async def list_datasources() -> Dict[str, Any]:
    """列出 SQLBot 当前工作空间的数据源（已剥离敏感字段）。"""
    if not _enabled():
        return {"ok": False, "error": "SQLBot 未启用"}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        ) as client:
            token = await _get_token(client)
            resp = await client.post(
                f"{_rest_url()}/api/v1/mcp/mcp_ds_list",
                json={"token": token},
            )
            resp.raise_for_status()
            data = _unwrap(resp.json())
            items = data if isinstance(data, list) else []
            return {"ok": True, "datasources": items}
    except Exception as e:
        return {"ok": False, "error": f"获取数据源列表失败：{e}"}


async def check_datasource(ds: Dict[str, Any]) -> Dict[str, Any]:
    """测试数据源连接（不保存）。返回 {ok, message}。"""
    if not _enabled():
        return {"ok": False, "error": "SQLBot 未启用"}

    body = _ds_body(ds)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        ) as client:
            headers = await _headers(client)
            resp = await client.post(
                f"{_rest_url()}/api/v1/datasource/check", json=body, headers=headers
            )
            if resp.status_code >= 400:
                # check 失败时 SQLBot 直接抛 500 + 纯文本 detail
                detail = _plain_detail(resp)
                return {"ok": False, "error": detail or f"连接失败（HTTP {resp.status_code}）"}
            data = _unwrap(resp.json())
            ok = data is True or (isinstance(data, dict) and data.get("success", True))
            return {"ok": bool(ok), "message": "连接成功" if ok else "连接失败"}
    except Exception as e:
        return {"ok": False, "error": f"连接测试失败：{e}"}


async def add_datasource(ds: Dict[str, Any]) -> Dict[str, Any]:
    """新增数据源（会实际连接并拉取表结构）。返回 {ok, datasource?, error?}。"""
    if not _enabled():
        return {"ok": False, "error": "SQLBot 未启用"}

    body = {**_ds_body(ds), "oid": 1, "tables": [], "recommended_config": 1}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        ) as client:
            headers = await _headers(client)
            resp = await client.post(
                f"{_rest_url()}/api/v1/datasource/add", json=body, headers=headers
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": _plain_detail(resp) or f"新增失败（HTTP {resp.status_code}）"}
            data = _unwrap(resp.json())
            return {"ok": True, "datasource": data if isinstance(data, dict) else None}
    except Exception as e:
        return {"ok": False, "error": f"新增数据源失败：{e}"}


async def delete_datasource(ds_id: int, name: str = "") -> Dict[str, Any]:
    """删除数据源。返回 {ok, error?}。"""
    if not _enabled():
        return {"ok": False, "error": "SQLBot 未启用"}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        ) as client:
            headers = await _headers(client)
            resp = await client.post(
                f"{_rest_url()}/api/v1/datasource/delete/{ds_id}/{name}",
                headers=headers,
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": _plain_detail(resp) or f"删除失败（HTTP {resp.status_code}）"}
            return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"删除数据源失败：{e}"}


def _plain_detail(resp: httpx.Response) -> str:
    """把 SQLBot 的非信封错误响应尽量还原成可读文本。"""
    text = (resp.text or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data.get("detail") or data.get("msg") or text[:300]
        if isinstance(data, str):
            return data[:300]
        return text[:300]
    except (json.JSONDecodeError, ValueError):
        return text[:300]
