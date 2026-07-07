# backend/routes/knowledge.py
"""
我的知识库：在用户选择的根目录下浏览目录树、读写 Markdown 笔记、新建/删除文件与文件夹。
所有文件操作均通过 validate_path 限制在知识库根目录内，防止路径遍历。
"""
import os
import shutil
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import backend
from backend.utils.validators import validate_path
from backend.system_tools.reader import file_read, FileReadError
from backend.system_tools.writer import file_write

router = APIRouter(prefix="/api/kb", tags=["knowledge"])

# 目录树中忽略的名称
_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}
# 富格式 / 二进制扩展名：使用专用只读渲染（reader），不可在界面内直接编辑。
# 其余一律尝试按文本解码，能解码即可读可写。
_RICH_EXTS = {
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".ico",
    # 文档
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm", ".xlsb",
    # 压缩
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    # 可执行 / 二进制
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".class", ".pyc", ".pyd", ".o", ".a",
    # 媒体
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".mov", ".avi", ".webm",
    # 字体 / 数据库
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".db", ".sqlite", ".sqlite3",
}
# 单个文件按文本读取的大小上限
_MAX_TEXT_BYTES = 5 * 1024 * 1024


def _is_editable(name: str) -> bool:
    """扩展名不属于富格式/二进制即视为可编辑文本。"""
    return Path(name).suffix.lower() not in _RICH_EXTS


def _read_text_or_none(path: Path) -> Optional[str]:
    """
    尝试把文件解码为文本。成功返回文本；判定为二进制或过大返回 None。
    多编码探测，NUL 字节或替换字符过多则视为二进制。
    """
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return ""  # 空文件 → 可编辑空文本
    # 含 NUL 字节几乎必是二进制
    if b"\x00" in raw[:8192]:
        return None

    candidates = ["utf-8-sig", "utf-8"]
    try:
        import chardet
        if len(raw) > 32:
            det = chardet.detect(raw).get("encoding")
            if det:
                candidates.append(det)
    except Exception:
        pass
    candidates += ["gbk", "gb18030", "big5", "latin-1"]

    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # 全部失败：用替换解码并检查替换比例，过高则判为二进制
    text = raw.decode("utf-8", errors="replace")
    if text.count("�") > max(8, len(text) * 0.05):
        return None
    return text


# ──────────────────────── 数据模型 ────────────────────────

class RootRequest(BaseModel):
    path: str

class SaveRequest(BaseModel):
    path: str
    content: str

class CreateRequest(BaseModel):
    parent: Optional[str] = None   # 相对根目录的父目录，空表示根目录
    name: str
    type: str                      # "file" | "dir"

class DeleteRequest(BaseModel):
    path: str


# ──────────────────────── 工具函数 ────────────────────────

def _get_root() -> Path:
    root = getattr(backend, "kb_path", "")
    if not root:
        raise HTTPException(400, "尚未设置知识库目录")
    p = Path(root)
    if not p.is_dir():
        raise HTTPException(400, "知识库目录不存在或不是有效目录")
    return p.resolve()


def _safe(path: str, root: Path) -> Path:
    """校验路径位于知识库根目录内。path 可为相对根目录或绝对路径。"""
    p = Path(path)
    if not p.is_absolute():
        p = root / path
    try:
        return validate_path(str(p), [root])
    except ValueError as e:
        raise HTTPException(403, f"路径校验失败：{e}")


def _build_tree(dir_path: Path, root: Path, depth: int = 0, max_depth: int = 12) -> list:
    """递归构建目录树，返回适配 naive-ui n-tree 的节点列表。"""
    if depth > max_depth:
        return []
    nodes = []
    try:
        entries = sorted(
            os.scandir(dir_path),
            key=lambda e: (not e.is_dir(), e.name.lower())
        )
    except OSError:
        return nodes

    for entry in entries:
        if entry.name in _IGNORE or entry.name.startswith("."):
            continue
        rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
        if entry.is_dir(follow_symlinks=False):
            nodes.append({
                "label": entry.name,
                "key": rel,
                "isDir": True,
                "children": _build_tree(Path(entry.path), root, depth + 1, max_depth),
            })
        else:
            nodes.append({
                "label": entry.name,
                "key": rel,
                "isDir": False,
                "editable": _is_editable(entry.name),
            })
    return nodes


# ──────────────────────── 接口 ────────────────────────

@router.get("/root")
async def get_root():
    return {"path": getattr(backend, "kb_path", "")}


@router.post("/root/set")
async def set_root(req: RootRequest):
    if not os.path.isdir(req.path):
        raise HTTPException(400, "提供的路径不是一个有效目录")
    backend.kb_path = req.path
    return {"status": "ok", "path": backend.kb_path}


@router.get("/tree")
async def get_tree():
    root = _get_root()
    tree = await asyncio.to_thread(_build_tree, root, root)
    return {"root": str(root), "tree": tree}


@router.get("/file")
async def get_file(path: str):
    root = _get_root()
    safe_path = _safe(path, root)
    if not safe_path.exists():
        raise HTTPException(404, "文件不存在")
    if not safe_path.is_file():
        raise HTTPException(400, "路径不是文件")

    rel = os.path.relpath(safe_path, root).replace(os.sep, "/")

    # 富格式（图片/PDF/Office 等）走专用只读渲染
    if not _is_editable(safe_path.name):
        try:
            result = await file_read(str(safe_path), allowed_dirs=[str(root)])
        except FileReadError as e:
            raise HTTPException(400, str(e))
        return {
            "path": rel,
            "content": result.get("content", ""),
            "format": result.get("format", "text"),
            "editable": False,
        }

    # 其余按文本读取；能解码即可编辑，否则明确告知是二进制
    text = await asyncio.to_thread(_read_text_or_none, safe_path)
    if text is None:
        return {
            "path": rel,
            "content": "（该文件为二进制或非文本内容，无法在此查看或编辑）",
            "format": "binary",
            "editable": False,
        }
    return {
        "path": rel,
        "content": text,
        "format": "text",
        "editable": True,
    }


@router.post("/file/save")
async def save_file(req: SaveRequest):
    root = _get_root()
    safe_path = _safe(req.path, root)
    if safe_path.exists() and safe_path.is_dir():
        raise HTTPException(400, "目标是目录，无法写入")
    # file_write 使用 backend.workspace_path 做校验，这里临时切换到知识库根目录
    prev = backend.workspace_path
    backend.workspace_path = str(root)
    try:
        result = await file_write(str(safe_path), req.content)
    finally:
        backend.workspace_path = prev
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "保存失败"))
    return {"status": "ok", "bytes_written": result.get("bytes_written", 0)}


@router.post("/create")
async def create_entry(req: CreateRequest):
    root = _get_root()
    name = (req.name or "").strip().strip("/\\")
    if not name or any(c in name for c in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        raise HTTPException(400, "名称非法")
    parent_rel = (req.parent or "").strip()
    target_rel = f"{parent_rel}/{name}" if parent_rel else name
    safe_path = _safe(target_rel, root)
    if safe_path.exists():
        raise HTTPException(400, "同名文件或文件夹已存在")

    def _do_create():
        if req.type == "dir":
            safe_path.mkdir(parents=True, exist_ok=False)
        else:
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.touch(exist_ok=False)

    try:
        await asyncio.to_thread(_do_create)
    except OSError as e:
        raise HTTPException(400, f"创建失败：{e}")
    return {"status": "ok", "path": os.path.relpath(safe_path, root).replace(os.sep, "/")}


@router.post("/delete")
async def delete_entry(req: DeleteRequest):
    root = _get_root()
    safe_path = _safe(req.path, root)
    if safe_path.resolve() == root:
        raise HTTPException(400, "不能删除知识库根目录")
    if not safe_path.exists():
        raise HTTPException(404, "目标不存在")

    def _do_delete():
        if safe_path.is_dir():
            shutil.rmtree(safe_path)
        else:
            safe_path.unlink()

    try:
        await asyncio.to_thread(_do_delete)
    except OSError as e:
        raise HTTPException(400, f"删除失败：{e}")
    return {"status": "ok"}
