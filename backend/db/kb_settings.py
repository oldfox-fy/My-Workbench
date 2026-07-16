# backend/db/kb_settings.py
"""
知识库应用级设置的读写（存于 app_settings 键值表）。

当前主要用于存储 embedding（向量化）配置与 reranker（重排序）配置：
- provider: "ollama" | "openai"    向量化服务提供方
- base_url: str                    OpenAI 兼容端点（Ollama 通常为 http://127.0.0.1:11434/v1）
- api_key:  str                    云端服务密钥（本地可留空）
- model:    str                    embedding 模型名（如 bge-m3 / text-embedding-3-small）
- dim:      int                    向量维度（由"测试连接"探测后写入，切换模型需重建索引）
"""
import json
from typing import Optional, Dict, Any
from backend.database import get_db

# app_settings 中存 embedding 配置的键名
_EMBEDDING_KEY = "kb_embedding_config"
_RERANKER_KEY = "kb_reranker_config"

# 默认配置：本地 Ollama + bge-m3（隐私优先，符合项目"双引擎"定位）
DEFAULT_EMBEDDING_CONFIG: Dict[str, Any] = {
    "provider": "ollama",
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "",
    "model": "bge-m3",
    "dim": 0,  # 0 表示尚未探测；由 /embedding/test 探测后写入
}

# 重排序（Reranker）默认配置：默认关闭，用户手动开启
DEFAULT_RERANKER_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "provider": "openai",   # 与 embedding 共用 base_url / api_key
    "model": "BAAI/bge-reranker-v2-m3",
}


async def get_setting(key: str) -> Optional[str]:
    """读取任意 app_settings 值，不存在返回 None。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


async def set_setting(key: str, value: str) -> None:
    """写入（或覆盖）任意 app_settings 值。"""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def get_embedding_config() -> Dict[str, Any]:
    """
    读取 embedding 配置，与默认值合并（保证字段齐全）。
    未配置时返回默认（本地 Ollama / bge-m3，dim=0）。
    """
    raw = await get_setting(_EMBEDDING_KEY)
    cfg = dict(DEFAULT_EMBEDDING_CONFIG)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
        except json.JSONDecodeError:
            pass
    return cfg


async def save_embedding_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    保存 embedding 配置。仅持久化已知字段，避免脏数据。
    返回合并后的完整配置。
    """
    merged = await get_embedding_config()
    for k in DEFAULT_EMBEDDING_CONFIG:
        if k in cfg and cfg[k] is not None:
            merged[k] = cfg[k]
    await set_setting(_EMBEDDING_KEY, json.dumps(merged, ensure_ascii=False))
    return merged


async def update_embedding_dim(dim: int) -> None:
    """探测到向量维度后单独更新 dim 字段。"""
    cfg = await get_embedding_config()
    cfg["dim"] = int(dim)
    await set_setting(_EMBEDDING_KEY, json.dumps(cfg, ensure_ascii=False))


# ──────────────────────── Reranker 配置读写 ────────────────────────

async def get_reranker_config() -> Dict[str, Any]:
    """读取 reranker 配置，未配置时返回默认（enabled=False）。"""
    raw = await get_setting(_RERANKER_KEY)
    cfg = dict(DEFAULT_RERANKER_CONFIG)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
        except json.JSONDecodeError:
            pass
    return cfg


async def save_reranker_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """保存 reranker 配置。仅持久化已知字段。返回合并后的完整配置。"""
    merged = await get_reranker_config()
    for k in DEFAULT_RERANKER_CONFIG:
        if k in cfg and cfg[k] is not None:
            merged[k] = cfg[k]
    await set_setting(_RERANKER_KEY, json.dumps(merged, ensure_ascii=False))
    return merged
