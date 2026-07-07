# backend/routes/kb_rag.py
"""
知识库 RAG 相关接口：
- embedding 配置读写与连通性测试（M1）
- 索引重建 / 状态查询、语义搜索（M2）

所有接口挂在 /api/kb 前缀下，与 routes/knowledge.py（文件浏览/读写）互补。
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.kb_settings import get_embedding_config, save_embedding_config
from backend.services.embedding import probe_config
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


class SearchIn(BaseModel):
    query: str
    top_k: int = 8


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
    """界面内语义搜索。"""
    if not req.query or not req.query.strip():
        raise HTTPException(400, "检索内容不能为空。")
    top_k = max(1, min(int(req.top_k or 8), 20))
    try:
        hits = await kb_indexer.search(req.query.strip(), top_k)
    except KbNotConfiguredError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"query": req.query, "hits": hits}


# ──────────────────────── 双链与图谱（M3 / M4） ────────────────────────

@router.get("/graph")
async def knowledge_graph(include_tags: bool = False):
    """构建知识库双链图谱：节点（笔记/虚节点/标签）+ 边（wiki/md/tag/missing）。"""
    try:
        return await kb_graph.build_graph(include_tags=include_tags)
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
