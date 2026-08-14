# backend/services/kb_indexer.py
"""
知识库索引引擎：切片 → 向量化 → 写入向量库。

切片策略（标题层级 + 字数兜底）：
- Markdown 按 ATX 标题（#..######）分块，每块保留其标题路径（如「笔记 > 章节 > 小节」）作为上下文。
- 超过字数上限的块，按字数 + 重叠窗口二次切分，避免单块过大稀释语义。
- 非 Markdown 文档复用 system_tools.reader.file_read 转为文本后按纯字数切分。

增量索引：以 file_hash 为准，未变化的文件跳过；已删除的文件清理其分片与向量。
所有文件访问限制在 backend.kb_path 内。
"""
import os
import re
import hashlib
import asyncio
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

# 自动标签开关（由 app_config 控制，延迟导入避免循环引用）
_AUTO_TAG_ENABLED = True

import backend
from config_loader import config
from backend.bootstrap import logger
from backend.db import kb_chunks, vec_store
from backend.services.embedding import get_embedder, Embedder
from backend.system_tools.reader import file_read, FileReadError

# 忽略的目录/文件（与 knowledge.py 保持一致）
_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}
# 参与索引的文本类扩展名
_INDEX_EXTS = {
    ".md", ".markdown", ".txt", ".rst",
    ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".csv", ".tsv", ".xlsx", ".xls",
    ".html", ".htm", ".epub", ".odt", ".rtf",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
}
_MD_EXTS = {".md", ".markdown"}

# 切片参数
_MAX_CHARS = 800      # 单块最大字符数
_OVERLAP = 100        # 二次切分的重叠字符数
_MIN_CHARS = 30       # 过短的块丢弃（如孤立标题）

# 并发索引文件数默认值（fallback）：同时处理的文件数。
# 实际值优先取 app_config.yaml 的 kb_index.file_concurrency。
_FILE_CONCURRENCY = 8


def _get_file_concurrency() -> int:
    """读取文件级并发数，优先取 app_config.yaml 的 kb_index.file_concurrency。"""
    try:
        v = int(getattr(config, "kb_file_concurrency", _FILE_CONCURRENCY))
        return v if v > 0 else _FILE_CONCURRENCY
    except Exception:
        return _FILE_CONCURRENCY


# 串行化知识库「写库」临界区：SQLite 单写者模型下，file_concurrency 个文件并发写
# kb_chunks / kb_index_meta / vec_chunks（各自独立连接）会撞写锁，超过 busy_timeout
# 就抛 "database is locked"。embedding 网络请求仍在锁外并行，只有写库被串行化。
_db_write_lock = asyncio.Lock()


class KbNotConfiguredError(Exception):
    pass


def _kb_root(kb_path: Optional[str] = None) -> Path:
    root = kb_path or backend.get_user_kb_path() or getattr(backend, "kb_path", "")
    if not root:
        raise KbNotConfiguredError("知识库尚未配置，请先在「我的知识库」界面选择根目录。")
    p = Path(root)
    if not p.is_dir():
        raise KbNotConfiguredError(f"知识库目录无效：{root}")
    return p.resolve()


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _split_long(text: str) -> List[str]:
    """按字数 + 重叠窗口切分过长文本。"""
    text = text.strip()
    if len(text) <= _MAX_CHARS:
        return [text] if text else []
    parts: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _MAX_CHARS, n)
        parts.append(text[start:end])
        if end >= n:
            break
        start = end - _OVERLAP
    return parts


def _chunk_markdown(text: str) -> List[Tuple[str, str]]:
    """
    按标题层级切分 Markdown，返回 [(heading_path, content), ...]。
    heading_path 形如 "一级标题 > 二级标题"。
    """
    lines = text.splitlines()
    chunks: List[Tuple[str, str]] = []
    heading_stack: List[Tuple[int, str]] = []  # [(level, title)]
    buf: List[str] = []

    def _current_path() -> str:
        return " > ".join(t for _, t in heading_stack)

    def _flush():
        content = "\n".join(buf).strip()
        buf.clear()
        if len(content) < _MIN_CHARS:
            return
        hp = _current_path()
        for piece in _split_long(content):
            if len(piece.strip()) >= _MIN_CHARS:
                chunks.append((hp, piece.strip()))

    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    for line in lines:
        m = heading_re.match(line)
        if m:
            # 遇到新标题，先落盘上一段
            _flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # 维护标题栈：弹出同级或更深的标题
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        else:
            buf.append(line)
    _flush()
    return chunks


def _chunk_plain(text: str) -> List[Tuple[str, str]]:
    """非 Markdown：无标题结构，直接按字数切分，heading_path 为空。"""
    return [("", p.strip()) for p in _split_long(text) if len(p.strip()) >= _MIN_CHARS]


async def _read_text(abs_path: Path, root: Path) -> Optional[str]:
    """读取文件文本内容。优先使用增强解析器（OCR/表格），降级到原始 reader。"""
    from backend.services.kb_parser import DocumentParser
    parser = DocumentParser()
    return await parser.parse_text_only(abs_path)


def _scan_files(root: Path) -> List[Path]:
    """遍历知识库，返回参与索引的文件绝对路径列表。"""
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地过滤忽略目录 + 隐藏目录
        dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in _INDEX_EXTS:
                files.append(p)
    return files


# 自动标签待处理队列：[(file_path, text), ...]
# rebuild 期间收集，rebuild 结束后批量处理，避免与主索引流程的写锁冲突
_pending_auto_tags: list = []


def _schedule_auto_tag(file_path: str, text: str):
    """收集待自动标签的文件（不立即执行，等 rebuild 结束批量处理）。"""
    if not _AUTO_TAG_ENABLED:
        return
    _pending_auto_tags.append((file_path, text))


async def _flush_auto_tags():
    """批量执行所有待处理的自动标签。"""
    global _pending_auto_tags
    if not _pending_auto_tags:
        return
    pending = _pending_auto_tags
    _pending_auto_tags = []
    tagged = 0
    try:
        from backend.services.auto_tagger import auto_tag_and_persist
        for file_path, text in pending:
            try:
                written = await auto_tag_and_persist(file_path, text)
                if written > 0:
                    tagged += 1
            except Exception as e:
                logger.warning(f"[kb_indexer] 自动标签失败 {file_path}: {e}")
    except Exception as e:
        logger.warning(f"[kb_indexer] 自动标签批量处理异常: {e}")
    if tagged:
        logger.info(f"[kb_indexer] 自动标签完成：{tagged}/{len(pending)} 个文件已打标")


async def _index_file(rel: str, abs_path: Path, root: Path, full: bool,
                      embedder: Embedder, model_name: str) -> Tuple[str, int]:
    """索引单个文件（供并发调度）。返回 (status, chunk_count)。

    status:
      - "indexed"：已向量化并写库
      - "skipped"：file_hash 未变化，跳过
      - "empty"  ：无文本或切不出分片（仅写入 0 块 meta）
      - "hash_error"：读取/哈希失败，忽略
    """
    try:
        fhash = _file_hash(abs_path)
        mtime = abs_path.stat().st_mtime
    except OSError:
        return ("hash_error", 0)

    if not full:
        meta = await kb_chunks.get_meta(rel)
        if meta and meta["file_hash"] == fhash:
            return ("skipped", 0)
        # 文件已变化：先清理旧分片与向量
        if meta:
            async with _db_write_lock:
                old_ids = await kb_chunks.delete_file_chunks(rel)
                await vec_store.delete_ids(old_ids)

    text = await _read_text(abs_path, root)
    if not text or not text.strip():
        async with _db_write_lock:
            await kb_chunks.upsert_meta(rel, fhash, mtime, 0)
        return ("empty", 0)

    ext = abs_path.suffix.lower()
    # 使用增强的布局感知分块器
    from backend.services.kb_parser import _parse_to_elements, chunk_elements, MD_EXTS as _PARSER_MD_EXTS
    if ext in _PARSER_MD_EXTS:
        elements = _parse_to_elements(text)
        pieces = chunk_elements(elements)  # → [(heading, content, chunk_type), ...]
    else:
        # 非 MD：纯文本分块，标记为 text 类型
        elements = _parse_to_elements(text)
        pieces = chunk_elements(elements) if elements else [
            ("", p.strip(), "text") for p in _split_long(text) if len(p.strip()) >= _MIN_CHARS
        ]
    if not pieces:
        async with _db_write_lock:
            await kb_chunks.upsert_meta(rel, fhash, mtime, 0)
        return ("empty", 0)

    # 插入分片元数据 → 取得 id → 向量化 → 写入向量表
    # 写库分两段加锁：embedding 是网络耗时，放在锁外保持并发，只串行化写库临界区。
    async with _db_write_lock:
        inserted = await kb_chunks.insert_chunks(rel, fhash, model_name, pieces)
    contents = [c for _, c in inserted]
    vectors = await embedder.embed(contents)
    rows = [(cid, vec) for (cid, _), vec in zip(inserted, vectors)]
    async with _db_write_lock:
        await vec_store.upsert(rows)
        await kb_chunks.upsert_meta(rel, fhash, mtime, len(inserted))

    logger.info(f"[kb_indexer] 已索引 {rel}（{len(inserted)} 块）")

    # ── 自动标签：新文件/变更文件索引后收集（rebuild 结束批量处理）──
    _schedule_auto_tag(rel, text)
    return ("indexed", len(inserted))


class IndexInProgressError(Exception):
    """已有重建任务在进行中（watcher 自动增量与手动重建并发时触发）。"""


# 全局重建互斥：watcher 自动增量索引与手动「重建索引」可能同时触发 rebuild。
# SQLite 单写者下双写会撞锁/重复索引，这里同一时刻只允许一个 rebuild 运行；
# 已有任务在跑时抛 IndexInProgressError，由调用方决定跳过还是提示。
_rebuild_lock = asyncio.Lock()


def is_rebuilding() -> bool:
    """是否已有重建任务在进行中（供路由/监听器查询互斥状态）。"""
    return _rebuild_lock.locked()


async def rebuild(full: bool = False, user_id: int = 0, kb_path: Optional[str] = None) -> Dict[str, Any]:
    """重建知识库索引（互斥入口）。已在索引中则抛 IndexInProgressError。"""
    if _rebuild_lock.locked():
        raise IndexInProgressError("索引任务正在进行中，请稍候。")
    async with _rebuild_lock:
        return await _rebuild(full, user_id, kb_path)


async def _rebuild(full: bool = False, user_id: int = 0, kb_path: Optional[str] = None) -> Dict[str, Any]:
    """
    重建知识库索引。
    - full=True：清空所有分片与向量后全量重建。
    - full=False：增量，按 file_hash 跳过未变化文件，清理已删除文件。

    user_id / kb_path 用于读取该用户的知识库路径与 embedding 配置，
    避免重建时回退到全局（user_id=0）或空路径的旧配置。

    返回统计信息 dict。
    """
    root = _kb_root(kb_path)

    # 检查向量扩展可用性
    available, msg = vec_store.check_available()
    if not available:
        raise RuntimeError(msg)

    embedder: Embedder = await get_embedder(user_id)
    # 始终重新探测维度：切换模型后存储的 dim 可能过期，信任旧值会导致
    # 向量表维度与模型实际输出不一致而写入失败。
    probe = await embedder.embed_one("dimension probe")
    dim = len(probe)
    if not dim:
        raise RuntimeError("无法确定向量维度，请先在知识库设置中「测试连接」。")
    if embedder.cfg.dim != dim:
        from backend.db.kb_settings import update_embedding_dim
        await update_embedding_dim(dim, user_id)
        logger.info(f"[kb_indexer] 向量维度已更新：{embedder.cfg.dim} -> {dim}")

    model_name = embedder.cfg.model

    if full:
        try:
            await kb_chunks.clear_all()
        except Exception as e:
            if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
                logger.warning(
                    f"[kb_index] 数据库损坏，自动重建 KB 表结构..."
                )
                from backend.database import _force_repair_kb_tables
                if await _force_repair_kb_tables():
                    logger.info("[kb_index] KB 表重建完成，继续全量索引")
                else:
                    raise RuntimeError("数据库损坏且自动修复失败，请手动处理。") from e
            else:
                raise
        try:
            await vec_store.clear()
        except Exception as e:
            if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
                from backend.database import _force_repair_vec
                await _force_repair_vec()
            else:
                raise

    await vec_store.ensure_table(dim)

    disk_files = _scan_files(root)
    disk_rel = {os.path.relpath(str(p), str(root)).replace(os.sep, "/"): p for p in disk_files}

    # 清理磁盘上已删除的文件
    indexed = await kb_chunks.all_indexed_paths()
    removed = 0
    for rel in indexed:
        if rel not in disk_rel:
            ids = await kb_chunks.delete_file_chunks(rel)
            await vec_store.delete_ids(ids)
            removed += 1

    # ── 并发索引文件（bounded semaphore + 快速失败）──
    sem = asyncio.Semaphore(_get_file_concurrency())

    async def _guarded(rel: str, abs_path: Path):
        async with sem:
            return await _index_file(rel, abs_path, root, full, embedder, model_name)

    tasks = [asyncio.create_task(_guarded(rel, p)) for rel, p in disk_rel.items()]

    # FIRST_EXCEPTION：任一文件抛异常（如 embedding 网络故障）立即返回，
    # 取消其余任务，避免串行重试拖垮整库。
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        exc = t.exception()
        if exc is not None:
            for p in pending:
                p.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise exc

    indexed_count = 0
    skipped = 0
    total_chunks = 0
    for t in tasks:
        status, n = t.result()
        if status == "indexed":
            indexed_count += 1
            total_chunks += n
        elif status == "skipped":
            skipped += 1

    # ── 批量自动标签（索引阶段结束，统一打标，避免写锁冲突）──
    await _flush_auto_tags()

    files, chunks_total, _ = await kb_chunks.stats()
    return {
        "indexed_files_this_run": indexed_count,
        "skipped": skipped,
        "removed": removed,
        "chunks_this_run": total_chunks,
        "total_files": files,
        "total_chunks": chunks_total,
        "model_name": model_name,
        "dim": dim,
    }


async def search(query: str, top_k: int = 8,
                use_rerank: bool = False,
                user_id: int = 0) -> List[Dict[str, Any]]:
    """
    语义检索：query 向量化 → 向量库 KNN → 回填分片内容。
    当 use_rerank=True 时：先召回 top_k × 5 候选 → Reranker 精排 → 返回 top_k。

    返回 [{file_path, heading_path, content, distance, ...}]。
    """
    available, msg = vec_store.check_available()
    if not available:
        raise RuntimeError(msg)

    embedder = await get_embedder(user_id)
    qvec = await embedder.embed_one(query)
    if not qvec:
        return []

    # 如果启用 reranker，多召回一些候选
    recall_k = min(top_k * 5, 64) if use_rerank else top_k

    hits = await vec_store.search(qvec, recall_k)
    if not hits:
        return []

    ids = [cid for cid, _ in hits]
    meta = await kb_chunks.get_chunks_by_ids(ids)

    def _build_result(cid: int, distance: float, rerank_score: Optional[float] = None) -> Dict[str, Any]:
        m = meta.get(cid)
        if not m:
            return None
        r = {
            "file_path": m["file_path"],
            "heading_path": m["heading_path"],
            "content": m["content"],
            "distance": round(distance, 4),
            "citation_id": m.get("citation_id", ""),
            "citation_text": f"[来源: {m['file_path']}{' > ' + m['heading_path'] if m['heading_path'] else ''}](cite://{m.get('citation_id', '')})" if m.get("citation_id") else "",
            "chunk_type": m.get("chunk_type", "text"),
            "page_number": m.get("page_number"),
        }
        if rerank_score is not None:
            r["rerank_score"] = round(rerank_score, 4)
        return r

    results: List[Dict[str, Any]] = []
    for cid, distance in hits:
        b = _build_result(cid, distance)
        if b:
            results.append(b)

    # ── Reranker 精排 ──
    if use_rerank and results:
        try:
            from backend.services.reranker import get_reranker
            reranker = await get_reranker(user_id)
            if reranker:
                # 用 reranker 对候选文档重新打分
                contents = [r["content"] for r in results]
                reranked = await reranker.rerank(query, contents, top_n=top_k)
                if reranked:
                    rescored: List[Dict[str, Any]] = []
                    for item in reranked:
                        idx = item.get("index", 0)
                        score = item.get("relevance_score", 0)
                        if 0 <= idx < len(results):
                            results[idx]["rerank_score"] = round(score, 4)
                            rescored.append(results[idx])
                    if rescored:
                        results = rescored
                    logger.info(
                        f"[kb_indexer] Reranker 精排完成：{len(hits)} 候选 → {len(results)} 结果"
                    )
        except Exception as e:
            logger.warning(f"[kb_indexer] Reranker 调用失败，回退到原始排序：{e}")
            # 回退：保持向量距离排序即可，无需额外处理

    return results[:top_k]
