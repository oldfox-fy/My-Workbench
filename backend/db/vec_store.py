# backend/db/vec_store.py
"""
基于 sqlite-vec 的向量存储封装。

要点：
- sqlite-vec 是按连接加载的扩展，且 `enable_load_extension` 在个别 Windows Python
  发行版可能未编译进 sqlite3。因此这里用同步 sqlite3 连接 + asyncio.to_thread，
  并在加载失败时抛出清晰的中文错误（VecUnavailableError），不影响知识库其它功能。
- 向量表 vec_chunks(rowid, embedding float[DIM]) 的维度由 embedding 模型决定，
  维度变化时需要重建（drop + create）。rowid 与 kb_chunks.id 对齐。
- 与主库同一个文件（lumneo.db），保证事务与备份一致性。
"""
import sqlite3
import asyncio
from typing import List, Tuple, Optional

from config_loader import config
from backend.bootstrap import logger

try:
    import sqlite_vec
    from sqlite_vec import serialize_float32
    _HAS_SQLITE_VEC = True
except ImportError:
    sqlite_vec = None
    serialize_float32 = None
    _HAS_SQLITE_VEC = False


_DB_PATH = f"{config.data_dir}/data/lumneo.db"


class VecUnavailableError(Exception):
    """向量扩展不可用（未安装或当前 sqlite3 不支持加载扩展）。"""
    pass


def _open_vec_conn() -> sqlite3.Connection:
    """打开一个已加载 sqlite-vec 扩展的同步连接。失败抛 VecUnavailableError。"""
    if not _HAS_SQLITE_VEC:
        raise VecUnavailableError("未安装 sqlite-vec，请执行 pip install sqlite-vec。")
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn
    except AttributeError as e:
        # 某些精简版 Python 的 sqlite3 未编译 enable_load_extension
        raise VecUnavailableError(
            "当前 Python 的 sqlite3 不支持加载扩展，无法启用向量检索。"
            "请更换标准 CPython 发行版或安装支持扩展的 sqlite3。"
        ) from e
    except Exception as e:
        raise VecUnavailableError(f"加载 sqlite-vec 扩展失败：{e}") from e


def check_available() -> Tuple[bool, str]:
    """探测向量扩展是否可用。返回 (available, message)。"""
    try:
        conn = _open_vec_conn()
        try:
            version, = conn.execute("SELECT vec_version()").fetchone()
            return True, f"sqlite-vec {version}"
        finally:
            conn.close()
    except VecUnavailableError as e:
        return False, str(e)


# ──────────────────────── 同步实现（在线程池中执行） ────────────────────────

def _table_dim(conn: sqlite3.Connection) -> Optional[int]:
    """读取现有 vec_chunks 表的维度；不存在返回 None。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    ).fetchone()
    if not row or not row[0]:
        return None
    # sql 形如: CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[1024])
    import re
    m = re.search(r"float\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def _ensure_table(dim: int):
    conn = _open_vec_conn()
    try:
        existing = _table_dim(conn)
        if existing == dim:
            return
        if existing is not None and existing != dim:
            # 维度变化，重建向量表（旧向量作废）
            conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{dim}])"
        )
        conn.commit()
    finally:
        conn.close()


def _upsert(rows: List[Tuple[int, List[float]]]):
    """rows: [(chunk_id, embedding), ...]。先删同 rowid 再插，保证幂等。"""
    conn = _open_vec_conn()
    try:
        for chunk_id, emb in rows:
            conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                (chunk_id, serialize_float32(emb)),
            )
        conn.commit()
    finally:
        conn.close()


def _delete_ids(ids: List[int]):
    if not ids:
        return
    conn = _open_vec_conn()
    try:
        conn.executemany("DELETE FROM vec_chunks WHERE rowid = ?", [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()


def _clear():
    conn = _open_vec_conn()
    try:
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.commit()
    finally:
        conn.close()


def _search(query: List[float], k: int) -> List[Tuple[int, float]]:
    """返回 [(chunk_id, distance), ...]，按距离升序。"""
    conn = _open_vec_conn()
    try:
        # 表不存在时直接返回空
        if _table_dim(conn) is None:
            return []
        rows = conn.execute(
            """SELECT rowid, distance FROM vec_chunks
               WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
            (serialize_float32(query), k),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    finally:
        conn.close()


def _count() -> int:
    conn = _open_vec_conn()
    try:
        if _table_dim(conn) is None:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ──────────────────────── 异步接口 ────────────────────────

async def ensure_table(dim: int):
    await asyncio.to_thread(_ensure_table, dim)


async def upsert(rows: List[Tuple[int, List[float]]]):
    await asyncio.to_thread(_upsert, rows)


async def delete_ids(ids: List[int]):
    await asyncio.to_thread(_delete_ids, ids)


async def clear():
    await asyncio.to_thread(_clear)


async def search(query: List[float], k: int) -> List[Tuple[int, float]]:
    return await asyncio.to_thread(_search, query, k)


async def count() -> int:
    return await asyncio.to_thread(_count)


# ──────────────────── 会话记忆向量表 ────────────────────

_SESSION_MEM_TABLE = "vec_session_memories"


def _ensure_session_table(dim: int):
    """创建或重建会话记忆向量表。"""
    conn = _open_vec_conn()
    try:
        existing_dim = None
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (_SESSION_MEM_TABLE,)
        ).fetchone()
        if row and row[0]:
            import re
            m = re.search(r"float\[(\d+)\]", row[0])
            existing_dim = int(m.group(1)) if m else None

        if existing_dim == dim:
            return
        if existing_dim is not None:
            conn.execute(f"DROP TABLE IF EXISTS {_SESSION_MEM_TABLE}")
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_SESSION_MEM_TABLE} "
            f"USING vec0(embedding float[{dim}])"
        )
        conn.commit()
    finally:
        conn.close()


def _upsert_session(rows: "List[Tuple[int, List[float]]]"):
    """rows: [(session_memory_id, embedding), ...]"""
    conn = _open_vec_conn()
    try:
        for mem_id, emb in rows:
            conn.execute(f"DELETE FROM {_SESSION_MEM_TABLE} WHERE rowid = ?", (mem_id,))
            conn.execute(
                f"INSERT INTO {_SESSION_MEM_TABLE}(rowid, embedding) VALUES (?, ?)",
                (mem_id, serialize_float32(emb)),
            )
        conn.commit()
    finally:
        conn.close()


def _search_sessions(query: List[float], k: int) -> "List[Tuple[int, float]]":
    """返回 [(session_memory_id, distance), ...]"""
    conn = _open_vec_conn()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (_SESSION_MEM_TABLE,)
        ).fetchone()
        if not row or not row[0]:
            return []
        rows = conn.execute(
            f"SELECT rowid, distance FROM {_SESSION_MEM_TABLE} "
            f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (serialize_float32(query), k),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    finally:
        conn.close()


# ──────────── 会话记忆异步接口 ────────────

async def ensure_session_table(dim: int):
    await asyncio.to_thread(_ensure_session_table, dim)


async def upsert_session(rows: "List[Tuple[int, List[float]]]"):
    await asyncio.to_thread(_upsert_session, rows)


async def search_sessions(query: List[float], k: int) -> "List[Tuple[int, float]]":
    return await asyncio.to_thread(_search_sessions, query, k)
