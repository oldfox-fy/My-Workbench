# backend/system_tools/kb_search.py
"""
知识库检索工具：支持语义检索、关键词检索、混合检索三种模式。

与 system_kb_list（浏览目录）/ system_kb_read（读取全文）互补。
"""
from typing import Any, Dict


async def kb_search(query: str, top_k: int = 5, method: str = "semantic") -> Dict[str, Any]:
    """
    对知识库做检索。

    Args:
        query: 检索问题 / 关键语义描述。
        top_k: 返回最相关的片段数量，默认 5。
        method: 检索方式 — "semantic"（语义向量，默认）、"keyword"（关键词全文）、"hybrid"（混合）

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
            return await _hybrid_search(query.strip(), top_k)
        else:
            return await _semantic_search(query.strip(), top_k)
    except KbNotConfiguredError as e:
        return {"success": False, "error": str(e)}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"检索失败：{e}"}


async def _semantic_search(query: str, top_k: int) -> Dict[str, Any]:
    from backend.services.kb_indexer import search as _search
    hits = await _search(query, top_k)
    if not hits:
        return {"success": True, "hits": [], "method": "semantic",
                "message": "未检索到相关内容。知识库可能尚未建立索引。"}
    return {"success": True, "count": len(hits), "hits": hits, "method": "semantic"}


async def _keyword_search(query: str, top_k: int) -> Dict[str, Any]:
    from backend.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT kc.id, kc.file_path, kc.heading_path, kc.content,
                      snippet(kb_chunks_fts, 2, '', '', '...', 40) as snippet
               FROM kb_chunks_fts JOIN kb_chunks kc ON kc.id = kb_chunks_fts.rowid
               WHERE kb_chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, top_k),
        )
        rows = await cursor.fetchall()
        hits = [
            {"file_path": r[1], "heading_path": r[2], "content": r[3], "snippet": r[4]}
            for r in rows
        ]
        return {"success": True, "count": len(hits), "hits": hits, "method": "keyword"}
    finally:
        await db.close()


async def _hybrid_search(query: str, top_k: int) -> Dict[str, Any]:
    """混合搜索：语义 + 关键词取并集，按语义分数排序。"""
    try:
        sem = await _semantic_search(query, top_k)
        kw = await _keyword_search(query, top_k)
    except Exception:
        return await _semantic_search(query, top_k)

    sem_hits = sem.get("hits", [])
    kw_hits = kw.get("hits", [])

    # 按 content 前 80 字符去重，语义结果优先
    seen = set()
    merged = []
    for h in sem_hits:
        key = h.get("content", "")[:80]
        if key not in seen:
            seen.add(key)
            h["_source"] = "semantic"
            merged.append(h)
    for h in kw_hits:
        key = h.get("content", "")[:80]
        if key not in seen:
            seen.add(key)
            h["_source"] = "keyword"
            merged.append(h)

    return {"success": True, "count": len(merged), "hits": merged[:top_k], "method": "hybrid"}
