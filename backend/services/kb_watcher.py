# backend/services/kb_watcher.py
"""
知识库文件监听器：监控 KB 目录的文件变更，自动触发增量索引。

实现方式：纯 asyncio 轮询（零外部依赖），每 30s 扫描一次文件系统，
对比文件哈希/mtime 检测增删改，变更后在 5s 去抖窗口内合并触发增量索引。

集成：在 FastAPI lifespan 中启动/停止。
"""
import os
import hashlib
import asyncio
import time
from pathlib import Path
from typing import Dict, Set, Optional

from backend.bootstrap import logger

# 轮询间隔（秒）
_POLL_INTERVAL = 30
# 去抖延迟（秒）：检测到变更后等待此时间再触发索引，合并快速连续变更
_DEBOUNCE_SEC = 5.0
# 忽略的目录
_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}
# 参与索引的文件扩展名
_INDEX_EXTS = {
    ".md", ".markdown", ".txt", ".rst",
    ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".csv", ".tsv", ".xlsx", ".xls",
}


def _file_hash(path: Path) -> str:
    """快速 MD5 哈希（仅读前 64KB 用于变更检测）。"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return ""


class KbFileWatcher:
    """
    知识库文件变更监听器。

    用法:
        watcher = KbFileWatcher(kb_root)
        await watcher.start()
        ...
        await watcher.stop()
    """

    def __init__(self, kb_root_getter):
        """
        kb_root_getter: 无参可调用对象，返回当前 KB 根目录路径（str）或 None。
                       使用 getter 而非固定值以支持运行时切换 KB 目录。
        """
        self._get_root = kb_root_getter
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._snapshot: Dict[str, tuple] = {}  # rel_path → (mtime, size, hash_head)
        self._debounce_task: Optional[asyncio.Task] = None
        self._pending_index = False

    async def start(self):
        """启动后台轮询任务。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[kb_watcher] 文件监听已启动（轮询间隔 %ds，去抖 %ds）",
                     _POLL_INTERVAL, _DEBOUNCE_SEC)

    async def stop(self):
        """停止后台轮询任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._debounce_task:
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
            self._debounce_task = None
        self._snapshot.clear()
        logger.info("[kb_watcher] 文件监听已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    def _scan(self, root: Path) -> Dict[str, tuple]:
        """扫描 KB 目录，返回 {rel_path: (mtime, size, hash_head)}。"""
        result: Dict[str, tuple] = {}
        if not root or not root.is_dir():
            return result
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in _IGNORE_DIRS and not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    p = Path(dirpath) / name
                    if p.suffix.lower() not in _INDEX_EXTS:
                        continue
                    try:
                        st = p.stat()
                        rel = os.path.relpath(str(p), str(root)).replace(os.sep, "/")
                        result[rel] = (st.st_mtime, st.st_size, "")
                    except OSError:
                        continue
        except Exception as e:
            logger.warning(f"[kb_watcher] 扫描异常: {e}")
        return result

    async def _poll_loop(self):
        """后台轮询循环。"""
        # 初始快照：留空，首次总是全量同步
        while self._running:
            try:
                root_str = self._get_root()
                if not root_str:
                    await asyncio.sleep(_POLL_INTERVAL)
                    continue

                root = Path(root_str)
                if not root.is_dir():
                    await asyncio.sleep(_POLL_INTERVAL)
                    continue

                current = self._scan(root)

                # 对比快照，检测变更
                changed = False
                old_keys = set(self._snapshot.keys())
                new_keys = set(current.keys())

                # 新增/修改
                for k in new_keys:
                    if k not in old_keys or current[k] != self._snapshot.get(k):
                        changed = True
                        break

                # 删除
                if not changed:
                    for k in old_keys - new_keys:
                        changed = True
                        break

                # 更新快照
                self._snapshot = current

                if changed and not self._pending_index:
                    self._pending_index = True
                    self._debounce_task = asyncio.create_task(self._debounced_index())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[kb_watcher] 轮询异常: {e}")

            await asyncio.sleep(_POLL_INTERVAL)

    async def _debounced_index(self):
        """去抖后触发增量索引。"""
        try:
            await asyncio.sleep(_DEBOUNCE_SEC)
        except asyncio.CancelledError:
            self._pending_index = False
            raise

        if not self._pending_index:
            return

        try:
            from backend.services.kb_indexer import rebuild
            logger.info("[kb_watcher] 检测到文件变更，触发增量索引...")
            result = await rebuild(full=False)
            logger.info(
                "[kb_watcher] 增量索引完成 — "
                f"新增/更新 {result['indexed_files_this_run']} 个文件，"
                f"删除 {result['removed']} 个，"
                f"跳过 {result['skipped']} 个"
            )
        except Exception as e:
            # rebuild 内部已有完整的错误处理，这里兜底
            logger.warning(f"[kb_watcher] 增量索引失败（不影响监听继续）: {e}")
        finally:
            self._pending_index = False
            self._debounce_task = None
