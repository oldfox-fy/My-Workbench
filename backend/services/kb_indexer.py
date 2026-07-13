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

import backend
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


class KbNotConfiguredError(Exception):
    pass


def _kb_root() -> Path:
    root = getattr(backend, "kb_path", "")
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


async def rebuild(full: bool = False) -> Dict[str, Any]:
    """
    重建知识库索引。
    - full=True：清空所有分片与向量后全量重建。
    - full=False：增量，按 file_hash 跳过未变化文件，清理已删除文件。

    返回统计信息 dict。
    """
    root = _kb_root()

    # 检查向量扩展可用性
    available, msg = vec_store.check_available()
    if not available:
        raise RuntimeError(msg)

    embedder: Embedder = await get_embedder()
    dim = embedder.cfg.dim
    if not dim:
        # 未探测维度：先探测一次
        probe = await embedder.embed_one("dimension probe")
        dim = len(probe)
    if not dim:
        raise RuntimeError("无法确定向量维度，请先在知识库设置中「测试连接」。")

    model_name = embedder.cfg.model

    if full:
        await kb_chunks.clear_all()
        await vec_store.clear()

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

    indexed_count = 0
    skipped = 0
    total_chunks = 0

    for rel, abs_path in disk_rel.items():
        try:
            fhash = _file_hash(abs_path)
            mtime = abs_path.stat().st_mtime
        except OSError:
            continue

        if not full:
            meta = await kb_chunks.get_meta(rel)
            if meta and meta["file_hash"] == fhash:
                skipped += 1
                continue
            # 文件已变化：先清理旧分片与向量
            if meta:
                old_ids = await kb_chunks.delete_file_chunks(rel)
                await vec_store.delete_ids(old_ids)

        text = await _read_text(abs_path, root)
        if not text or not text.strip():
            await kb_chunks.upsert_meta(rel, fhash, mtime, 0)
            continue

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
            await kb_chunks.upsert_meta(rel, fhash, mtime, 0)
            continue

        # 插入分片元数据 → 取得 id → 向量化 → 写入向量表
        inserted = await kb_chunks.insert_chunks(rel, fhash, model_name, pieces)
        contents = [c for _, c in inserted]
        vectors = await embedder.embed(contents)
        rows = [(cid, vec) for (cid, _), vec in zip(inserted, vectors)]
        await vec_store.upsert(rows)
        await kb_chunks.upsert_meta(rel, fhash, mtime, len(inserted))

        indexed_count += 1
        total_chunks += len(inserted)
        logger.info(f"[kb_indexer] 已索引 {rel}（{len(inserted)} 块）")

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


async def search(query: str, top_k: int = 8) -> List[Dict[str, Any]]:
    """
    语义检索：query 向量化 → 向量库 KNN → 回填分片内容。
    返回 [{file_path, heading_path, content, distance}, ...]。
    """
    available, msg = vec_store.check_available()
    if not available:
        raise RuntimeError(msg)

    embedder = await get_embedder()
    qvec = await embedder.embed_one(query)
    if not qvec:
        return []

    hits = await vec_store.search(qvec, top_k)
    if not hits:
        return []

    ids = [cid for cid, _ in hits]
    meta = await kb_chunks.get_chunks_by_ids(ids)
    results: List[Dict[str, Any]] = []
    for cid, distance in hits:
        m = meta.get(cid)
        if not m:
            continue
        results.append({
            "file_path": m["file_path"],
            "heading_path": m["heading_path"],
            "content": m["content"],
            "distance": round(distance, 4),
            "citation_id": m.get("citation_id", ""),
            "citation_text": f"[来源: {m['file_path']}{' > ' + m['heading_path'] if m['heading_path'] else ''}](cite://{m.get('citation_id', '')})" if m.get("citation_id") else "",
            "chunk_type": m.get("chunk_type", "text"),
            "page_number": m.get("page_number"),
        })
    return results
