# backend/system_tools/kb_search.py
"""
知识库检索工具：支持语义检索、关键词检索、混合检索三种模式。

与 system_kb_list（浏览目录）/ system_kb_read（读取全文）互补。
"""
from typing import Any, Dict


async def kb_search(query: str, top_k: int = 5, method: str = "semantic",
                   use_rerank: bool = False) -> Dict[str, Any]:
    """
    对知识库做检索。

    Args:
        query: 检索问题 / 关键语义描述。
        top_k: 返回最相关的片段数量，默认 5。
        method: 检索方式 — "semantic"（语义向量，默认）、"keyword"（关键词全文）、"hybrid"（混合）
        use_rerank: 是否使用 reranker 精排（仅在 semantic/hybrid 模式下生效）

    Returns:
        dict：包含命中片段列表。
    """
    from backend.services.kb_indexer import search as _search_semantic, KbNotConfiguredError

    if not query or not query.strip():
        return {"success": False, "error": "检索内容不能为空。"}

    top_k = max(1, min(int(top_k or 5), 20))
    method = method.lower() if method else "semantic"

    try:
        if method == "keyword":
            return await _keyword_search(query.strip(), top_k)
        elif method == "hybrid":
            return await _hybrid_search(query.strip(), top_k, use_rerank=use_rerank)
        else:
            return await _semantic_search(query.strip(), top_k, use_rerank=use_rerank)
    except KbNotConfiguredError as e:
        return {"success": False, "error": str(e)}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"检索失败：{e}"}


async def _semantic_search(query: str, top_k: int,
                           use_rerank: bool = False) -> Dict[str, Any]:
    from backend.services.kb_indexer import search as _search
    hits = await _search(query, top_k, use_rerank=use_rerank)
    label = "semantic+rerank" if use_rerank else "semantic"
    if not hits:
        return {"success": True, "hits": [], "method": label,
                "message": "未检索到相关内容。知识库可能尚未建立索引。"}
    return {"success": True, "count": len(hits), "hits": hits, "method": label}


async def _keyword_search(query: str, top_k: int) -> Dict[str, Any]:
    from backend.services.kb_indexer import keyword_search as _kw
    hits = await _kw(query, top_k)
    if not hits:
        return {"success": True, "hits": [], "method": "keyword",
                "message": "未检索到相关内容。知识库可能尚未建立索引。"}
    return {"success": True, "count": len(hits), "hits": hits, "method": "keyword"}


async def _hybrid_search(query: str, top_k: int,
                       use_rerank: bool = False) -> Dict[str, Any]:
    """混合搜索：语义 + 关键词 RRF 融合（可选 reranker 精排）。"""
    from backend.services.kb_indexer import hybrid_search as _hy
    hits = await _hy(query, top_k, use_rerank=use_rerank)
    label = "hybrid+rerank" if use_rerank else "hybrid"
    if not hits:
        return {"success": True, "hits": [], "method": label,
                "message": "未检索到相关内容。知识库可能尚未建立索引。"}
    return {"success": True, "count": len(hits), "hits": hits, "method": label}
