# backend/routes/kb_rag.py
"""
知识库 RAG 相关接口：
- embedding 配置读写与连通性测试（M1）
- 索引重建 / 状态查询、语义搜索（M2）

所有接口挂在 /api/kb 前缀下，与 routes/knowledge.py（文件浏览/读写）互补。
"""
import asyncio
import os
from pathlib import Path
from typing import Optional

from backend.database import get_db

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.kb_settings import (
    get_embedding_config, save_embedding_config,
    get_reranker_config, save_reranker_config,
)
from backend.services.embedding import probe_config
from backend.services.reranker import probe_reranker_config
from backend.db import kb_chunks, vec_store
from backend.services import kb_indexer, kb_graph
from backend.services.kb_indexer import KbNotConfiguredError
from backend.bootstrap import logger

router = APIRouter(prefix="/api/kb", tags=["kb-rag"])

# 后台索引任务状态（单实例，简单够用）
_index_state = {"running": False, "last_result": None, "last_error": None}


# ──────────────────────── 数据模型 ────────────────────────

class EmbeddingConfigIn(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class RebuildIn(BaseModel):
    full: bool = False


class RerankerConfigIn(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class SearchIn(BaseModel):
    query: str
    top_k: int = 8
    use_rerank: bool = False


# ──────────────────────── embedding 配置（M1） ────────────────────────

@router.get("/embedding/config")
async def get_embedding_cfg():
    """读取当前 embedding 配置（apiKey 原样返回，与 models 表一致）。"""
    return await get_embedding_config()


@router.post("/embedding/config")
async def set_embedding_cfg(cfg: EmbeddingConfigIn):
    """保存 embedding 配置。若修改了 provider/base_url/model，维度需重新探测。"""
    payload = {k: v for k, v in cfg.dict().items() if v is not None}
    saved = await save_embedding_config(payload)
    return {"status": "ok", "config": saved}


@router.post("/embedding/test")
async def test_embedding_cfg(cfg: EmbeddingConfigIn):
    """
    用给定配置（未填的字段回退到已保存值）测试连通性并探测向量维度。
    成功返回 {"success": true, "dim": N}。
    """
    current = await get_embedding_config()
    merged = dict(current)
    for k, v in cfg.dict().items():
        if v is not None:
            merged[k] = v
    result = await probe_config(merged)
    return result


# ──────────────────────── reranker 配置 ────────────────────────

@router.get("/reranker/config")
async def get_reranker_cfg():
    """读取当前 reranker 配置。"""
    return await get_reranker_config()


@router.post("/reranker/config")
async def set_reranker_cfg(cfg: RerankerConfigIn):
    """保存 reranker 配置。"""
    payload = {k: v for k, v in cfg.dict().items() if v is not None}
    saved = await save_reranker_config(payload)
    return {"status": "ok", "config": saved}


@router.post("/reranker/test")
async def test_reranker_cfg(cfg: RerankerConfigIn):
    """
    用给定配置测试 reranker 连通性。
    成功返回 {"success": true, "top_score": 0.98}。
    """
    current = await get_reranker_config()
    merged = dict(current)
    for k, v in cfg.dict().items():
        if v is not None:
            merged[k] = v
    result = await probe_reranker_config(merged)
    return result


# ──────────────────────── 索引管理（M2） ────────────────────────

async def _run_rebuild(full: bool):
    _index_state["running"] = True
    _index_state["last_error"] = None
    try:
        result = await kb_indexer.rebuild(full=full)
        _index_state["last_result"] = result
        logger.info(f"[kb_index] 索引完成：{result}")
    except Exception as e:
        _index_state["last_error"] = str(e)
        logger.warning(f"[kb_index] 索引失败：{e}")
    finally:
        _index_state["running"] = False


@router.post("/index/rebuild")
async def rebuild_index(req: RebuildIn):
    """启动后台索引任务（增量或全量）。立即返回，进度通过 /index/status 查询。"""
    if _index_state["running"]:
        raise HTTPException(409, "索引任务正在进行中，请稍候。")
    # 预检查：配置与向量扩展
    available, msg = vec_store.check_available()
    if not available:
        raise HTTPException(400, msg)
    cfg = await get_embedding_config()
    if not cfg.get("model"):
        raise HTTPException(400, "尚未配置 embedding 模型。")

    asyncio.create_task(_run_rebuild(req.full))
    return {"status": "started", "full": req.full}


@router.get("/index/status")
async def index_status():
    """返回索引状态：文件数、片段数、模型、维度、上次索引时间、向量扩展可用性。"""
    available, msg = vec_store.check_available()
    files, chunks_total, model = await kb_chunks.stats()
    cfg = await get_embedding_config()
    last = await kb_chunks.last_indexed_at()
    return {
        "configured": bool(cfg.get("model")),
        "running": _index_state["running"],
        "indexed_files": files,
        "chunk_count": chunks_total,
        "model_name": model or cfg.get("model", ""),
        "dim": cfg.get("dim", 0),
        "last_indexed_at": last,
        "vec_available": available,
        "vec_message": msg,
        "last_error": _index_state["last_error"],
    }


@router.post("/search")
async def semantic_search(req: SearchIn):
    """界面内语义搜索（可选 reranker 精排）。"""
    if not req.query or not req.query.strip():
        raise HTTPException(400, "检索内容不能为空。")
    top_k = max(1, min(int(req.top_k or 8), 20))
    try:
        hits = await kb_indexer.search(
            req.query.strip(), top_k,
            use_rerank=req.use_rerank,
        )
    except KbNotConfiguredError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"query": req.query, "hits": hits}


@router.get("/keyword-search")
async def keyword_search(q: str = "", limit: int = 20):
    """FTS5 关键词全文搜索。"""
    if not q or not q.strip():
        raise HTTPException(400, "检索内容不能为空。")
    limit = max(1, min(limit, 50))
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT kc.id, kc.file_path, kc.heading_path,
                      snippet(kb_chunks_fts, 2, '<mark>', '</mark>', '...', 40) as snippet
               FROM kb_chunks_fts JOIN kb_chunks kc ON kc.id = kb_chunks_fts.rowid
               WHERE kb_chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (q.strip(), limit),
        )
        rows = await cursor.fetchall()
        hits = [
            {"id": r[0], "file_path": r[1], "heading_path": r[2], "snippet": r[3]}
            for r in rows
        ]
        return {"query": q, "hits": hits, "method": "keyword"}
    finally:
        await db.close()


# ──────────────────────── 双链与图谱（M3 / M4） ────────────────────────

@router.get("/graph")
async def knowledge_graph(
    include_tags: bool = False,
    include_semantic: bool = False,
    semantic_threshold: float = 0.72,
    files: str = "",
    keyword: str = "",
):
    """构建知识库双链图谱。

    - files: 逗号分隔的文件相对路径，限定图谱范围。
    - keyword: 按文件名（含路径）模糊匹配，大小写不敏感。
    """
    try:
        scope_files = [f.strip() for f in files.split(",") if f.strip()] if files else None
        return await kb_graph.build_graph(
            include_tags=include_tags,
            include_semantic=include_semantic,
            semantic_threshold=semantic_threshold,
            scope_files=scope_files,
            keyword=keyword.strip(),
        )
    except kb_graph.KbNotConfiguredError as e:
        raise HTTPException(400, str(e))


@router.get("/backlinks")
async def backlinks(path: str):
    """返回引用了指定笔记的其它笔记（反向链接）。path 为相对知识库根目录。"""
    try:
        links = await kb_graph.get_backlinks(path)
    except kb_graph.KbNotConfiguredError as e:
        raise HTTPException(400, str(e))
    return {"path": path, "backlinks": links}


@router.get("/notes")
async def note_names():
    """返回所有笔记相对路径，供编辑器 [[ 自动补全。"""
    try:
        return {"notes": await kb_graph.list_note_names()}
    except kb_graph.KbNotConfiguredError as e:
        raise HTTPException(400, str(e))


@router.get("/ocr-status")
async def ocr_status():
    """返回 OCR 可用性状态。"""
    from backend.services.kb_parser import check_ocr_available
    return check_ocr_available()


@router.get("/citation/{citation_id}")
async def resolve_citation(citation_id: str):
    """根据 citation_id 查找源文件信息。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT file_path, heading_path, content, chunk_type, page_number FROM kb_chunks WHERE citation_id = ?",
            (citation_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"引用 {citation_id} 未找到")
        import backend
        kb_root = getattr(backend, "kb_path", "")
        full_path = str(Path(kb_root) / row[0]) if kb_root else row[0]
        return {
            "citation_id": citation_id,
            "file_path": row[0],
            "heading_path": row[1],
            "content_preview": (row[2] or "")[:500],
            "chunk_type": row[3],
            "page_number": row[4],
            "file_exists": os.path.isfile(full_path) if kb_root else False,
        }
    finally:
        await db.close()
