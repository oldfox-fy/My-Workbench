# backend/services/kb_chunking.py
"""
分块参数运行时配置。

默认值来自 app_config.yaml 的 kb_index.*（经 config_loader 读取），
用户可在设置页覆盖，覆盖值持久化到 app_settings（key=kb_chunking_config，归管理员 id=1）。

- 同步 getter（get_chunk_max_chars / get_chunk_overlap / get_chunk_min_chars）
  供 kb_parser / kb_indexer 在分块循环中直接调用，读内存缓存，避免在解析里 await。
- 异步 load / get / save 供路由层与启动初始化使用，保存后即时刷新内存缓存。
"""
from typing import Dict, Any, Optional

_KEY = "kb_chunking_config"
_DEFAULT_USER_ID = 1

_DEFAULT: Dict[str, int] = {
    "chunk_max_chars": 800,
    "chunk_overlap": 100,
    "chunk_min_chars": 30,
}

_cache: Optional[Dict[str, int]] = None


def _sanitize(cfg: Dict[str, Any]) -> Dict[str, int]:
    """归一化并约束取值，防止非法值导致分块异常（如 overlap >= max 造成死循环）。"""
    def _int(k: str) -> int:
        try:
            return int(cfg.get(k, _DEFAULT[k]))
        except (TypeError, ValueError):
            return _DEFAULT[k]

    m = _int("chunk_max_chars")
    m = max(50, min(m, 100000))
    o = _int("chunk_overlap")
    o = max(0, min(o, m - 1))          # overlap 必须 < max_chars
    n = _int("chunk_min_chars")
    n = max(1, min(n, m))
    return {"chunk_max_chars": m, "chunk_overlap": o, "chunk_min_chars": n}


def _config_defaults() -> Dict[str, int]:
    """优先取 app_config.yaml（config_loader），读不到回退硬编码默认。"""
    try:
        from config_loader import config
        return _sanitize({
            "chunk_max_chars": getattr(config, "kb_chunk_max_chars", _DEFAULT["chunk_max_chars"]),
            "chunk_overlap": getattr(config, "kb_chunk_overlap", _DEFAULT["chunk_overlap"]),
            "chunk_min_chars": getattr(config, "kb_chunk_min_chars", _DEFAULT["chunk_min_chars"]),
        })
    except Exception:
        return dict(_DEFAULT)


def _get_cache() -> Dict[str, int]:
    global _cache
    if _cache is None:
        _cache = _config_defaults()
    return _cache


def _set_cache(cfg: Dict[str, int]) -> None:
    global _cache
    _cache = dict(cfg)


# ── 同步 getter（供解析/分块直接调用） ──

def get_chunk_max_chars() -> int:
    return _get_cache()["chunk_max_chars"]


def get_chunk_overlap() -> int:
    return _get_cache()["chunk_overlap"]


def get_chunk_min_chars() -> int:
    return _get_cache()["chunk_min_chars"]


# ── 异步读写（供路由层 / 启动初始化） ──

async def get_chunking_config(user_id: int = _DEFAULT_USER_ID) -> Dict[str, int]:
    """返回当前生效配置。"""
    return dict(_get_cache())


async def load_chunking_config(user_id: int = _DEFAULT_USER_ID) -> None:
    """启动时从 app_settings 读回已保存的覆盖值，刷新内存缓存。"""
    import json
    from backend.db.kb_settings import get_setting
    raw = await get_setting(_KEY, user_id)
    if not raw:
        return
    try:
        stored = json.loads(raw)
        if isinstance(stored, dict):
            _set_cache(_sanitize({**_get_cache(), **stored}))
    except (json.JSONDecodeError, TypeError):
        pass


async def save_chunking_config(cfg: Dict[str, Any],
                               user_id: int = _DEFAULT_USER_ID) -> Dict[str, int]:
    """保存覆盖值并即时刷新缓存，返回合并后的完整配置。"""
    import json
    from backend.db.kb_settings import set_setting
    filtered = {k: v for k, v in (cfg or {}).items() if v is not None}
    merged = _sanitize({**_get_cache(), **filtered})
    await set_setting(_KEY, json.dumps(merged, ensure_ascii=False), user_id)
    _set_cache(merged)
    return dict(merged)
