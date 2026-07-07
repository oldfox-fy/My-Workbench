# backend/db/kb_chunks.py
"""
知识库分片元数据（kb_chunks）与索引状态（kb_index_meta）的读写。

kb_chunks.id 与 vec_store 中 vec_chunks.rowid 一一对齐：
先插 kb_chunks 取得自增 id，再用该 id 作为向量 rowid 写入向量表。
"""
from typing import List, Dict, Any, Optional, Tuple
from backend.database import get_db


async def get_meta(file_path: str) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT file_path, file_hash, mtime, chunk_count, indexed_at FROM kb_index_meta WHERE file_path = ?",
            (file_path,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "file_path": row[0], "file_hash": row[1], "mtime": row[2],
            "chunk_count": row[3], "indexed_at": row[4],
        }
    finally:
        await db.close()


async def all_indexed_paths() -> List[str]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT file_path FROM kb_index_meta")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
    finally:
        await db.close()


async def chunk_ids_for_file(file_path: str) -> List[int]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM kb_chunks WHERE file_path = ?", (file_path,)
        )
        rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]
    finally:
        await db.close()


async def delete_file_chunks(file_path: str) -> List[int]:
    """删除某文件的所有分片与索引记录，返回被删的 chunk id（供清理向量表）。"""
    ids = await chunk_ids_for_file(file_path)
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_chunks WHERE file_path = ?", (file_path,))
        await db.execute("DELETE FROM kb_index_meta WHERE file_path = ?", (file_path,))
        await db.commit()
    finally:
        await db.close()
    return ids


async def insert_chunks(
    file_path: str, file_hash: str, model_name: str,
    chunks: List[Tuple[str, str]],
) -> List[Tuple[int, str]]:
    """
    插入分片。chunks: [(heading_path, content), ...]。
    返回 [(chunk_id, content), ...]，供上层向量化后写入向量表。
    """
    db = await get_db()
    try:
        result: List[Tuple[int, str]] = []
        for idx, (heading_path, content) in enumerate(chunks):
            cursor = await db.execute(
                """INSERT INTO kb_chunks (file_path, chunk_index, heading_path, content, file_hash, model_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (file_path, idx, heading_path, content, file_hash, model_name),
            )
            result.append((cursor.lastrowid, content))
        await db.commit()
        return result
    finally:
        await db.close()


async def upsert_meta(file_path: str, file_hash: str, mtime: float, chunk_count: int):
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO kb_index_meta (file_path, file_hash, mtime, chunk_count, indexed_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(file_path) DO UPDATE SET
                   file_hash = excluded.file_hash, mtime = excluded.mtime,
                   chunk_count = excluded.chunk_count, indexed_at = CURRENT_TIMESTAMP""",
            (file_path, file_hash, mtime, chunk_count),
        )
        await db.commit()
    finally:
        await db.close()


async def get_chunks_by_ids(ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """按 id 批量取分片内容，用于检索结果回填。"""
    if not ids:
        return {}
    db = await get_db()
    try:
        placeholders = ",".join("?" * len(ids))
        cursor = await db.execute(
            f"SELECT id, file_path, heading_path, content FROM kb_chunks WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
        return {
            int(r[0]): {"file_path": r[1], "heading_path": r[2], "content": r[3]}
            for r in rows
        }
    finally:
        await db.close()


async def stats() -> Tuple[int, int, str]:
    """返回 (已索引文件数, 分片总数, 模型名)。"""
    db = await get_db()
    try:
        c1 = await db.execute("SELECT COUNT(*) FROM kb_index_meta")
        files = (await c1.fetchone())[0]
        c2 = await db.execute("SELECT COUNT(*) FROM kb_chunks")
        chunks = (await c2.fetchone())[0]
        c3 = await db.execute("SELECT model_name FROM kb_chunks LIMIT 1")
        mrow = await c3.fetchone()
        model = mrow[0] if mrow else ""
        return int(files), int(chunks), model
    finally:
        await db.close()


async def last_indexed_at() -> Optional[str]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT MAX(indexed_at) FROM kb_index_meta")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None
    finally:
        await db.close()


async def clear_all():
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_chunks")
        await db.execute("DELETE FROM kb_index_meta")
        await db.commit()
    finally:
        await db.close()
