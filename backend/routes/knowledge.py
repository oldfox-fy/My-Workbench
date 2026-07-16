# backend/routes/knowledge.py
"""
我的知识库：在用户选择的根目录下浏览目录树、读写 Markdown 笔记、新建/删除文件与文件夹。
所有文件操作均通过 validate_path 限制在知识库根目录内，防止路径遍历。
"""
import os
import shutil
import asyncio
import mimetypes
from pathlib import Path
from typing import Optional, List
from urllib.parse import quote as _quote

# 强制初始化 MIME 类型数据库（Windows 上注册表可能不可靠）
mimetypes.init()

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import backend
from backend.utils.validators import validate_path
from backend.system_tools.reader import file_read, FileReadError
from backend.system_tools.writer import file_write

router = APIRouter(prefix="/api/kb", tags=["knowledge"])

# 可内嵌查看的图片扩展名（前端用 <img> 直接渲染）
# 注：.svg 不列入，保持其作为文本可编辑（避免内嵌渲染任意 SVG 的风险）
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
# 可内嵌查看的 PDF（前端用 <iframe> 由浏览器/内核原生渲染）
_PDF_EXTS = {".pdf"}

# 目录树中忽略的名称
_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}

# 只读目录名称：这些目录下的所有文件和子目录均为只读，不可编辑/新建/删除
# 可通过 app_config.yaml 的 kb_readonly_dirs 字段扩展
_READONLY_DIR_NAMES = {"公共基础"}


def _init_readonly_dirs():
    """从 app_config.yaml 加载额外的只读目录名称（与内置常量合并）。"""
    try:
        from config_loader import config
        extra = config.raw_config.get("kb_readonly_dirs", [])
        if isinstance(extra, list):
            _READONLY_DIR_NAMES.update(str(d) for d in extra if d)
    except Exception:
        pass


def _is_under_readonly_dir(path: Path, root: Path) -> bool:
    """
    检查 path 是否位于只读目录下（包括只读目录本身）。
    匹配规则：路径中任意一级父目录（或路径自身）名称在 _READONLY_DIR_NAMES 中。
    """
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False  # 不在知识库根目录内，不判断
    for part in rel.parts:
        if part in _READONLY_DIR_NAMES:
            return True
    return False

# 模块加载时从配置合并只读目录名称
_init_readonly_dirs()

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

class SidecarSaveRequest(BaseModel):
    path: str      # 目标资源（被附注的文件）相对根目录路径
    content: str


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
        entry_path = Path(entry.path)
        rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
        is_readonly = _is_under_readonly_dir(entry_path, root)
        if entry.is_dir(follow_symlinks=False):
            nodes.append({
                "label": entry.name,
                "key": rel,
                "isDir": True,
                "readonly": is_readonly,
                "children": _build_tree(entry_path, root, depth + 1, max_depth),
            })
        else:
            nodes.append({
                "label": entry.name,
                "key": rel,
                "isDir": False,
                "editable": not is_readonly and _is_editable(entry.name),
                "readonly": is_readonly,
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
    # 确保「生成内容」目录存在
    gen_dir = os.path.join(req.path, "生成内容")
    os.makedirs(gen_dir, exist_ok=True)
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
    is_readonly = _is_under_readonly_dir(safe_path, root)
    ext = safe_path.suffix.lower()

    # 图片：交给前端 <img> 内嵌查看，走 /api/kb/raw 取原始字节
    if ext in _IMAGE_EXTS:
        return {
            "path": rel,
            "content": "",
            "format": "image",
            "raw_url": f"/api/kb/raw?path={_quote(rel)}",
            "abs_path": str(safe_path),
            "editable": False,
            "readonly": is_readonly,
        }

    # PDF：交给前端 <iframe> 由内核原生渲染，走 /api/kb/raw 取原始字节
    if ext in _PDF_EXTS:
        return {
            "path": rel,
            "content": "",
            "format": "pdf",
            "raw_url": f"/api/kb/raw?path={_quote(rel)}",
            "abs_path": str(safe_path),
            "editable": False,
            "readonly": is_readonly,
        }

    # 其余富格式（docx/xlsx/ppt 等）走专用只读渲染
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
            "readonly": is_readonly,
        }

    # 其余按文本读取；能解码即可编辑，否则明确告知是二进制
    text = await asyncio.to_thread(_read_text_or_none, safe_path)
    if text is None:
        return {
            "path": rel,
            "content": "（该文件为二进制或非文本内容，无法在此查看或编辑）",
            "format": "binary",
            "editable": False,
            "readonly": is_readonly,
        }
    return {
        "path": rel,
        "content": text,
        "format": "text",
        "editable": not is_readonly,
        "readonly": is_readonly,
    }


# MIME 类型显式映射（Windows 上 mimetypes.guess_type 可能返回 None，导致浏览器下载而非预览）
_MIME_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


@router.get("/raw")
async def get_raw_file(path: str):
    """流式返回知识库内文件的原始字节（供图片 <img> / PDF <iframe> 内嵌查看）。"""
    root = _get_root()
    safe_path = _safe(path, root)
    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(404, "文件不存在")
    ext = safe_path.suffix.lower()
    media_type = _MIME_MAP.get(ext) or mimetypes.guess_type(str(safe_path))[0]
    return FileResponse(
        str(safe_path),
        media_type=media_type or "application/octet-stream",
        filename=safe_path.name,
        content_disposition_type="inline",
    )


@router.post("/file/save")
async def save_file(req: SaveRequest):
    root = _get_root()
    safe_path = _safe(req.path, root)
    if _is_under_readonly_dir(safe_path, root):
        raise HTTPException(403, "该文件位于只读目录（公共基础）下，不可编辑")
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


# ──────────────────────── 附注（sidecar）────────────────────────
# 为不可直接编辑的资源（pdf/docx/图片等）提供一篇相邻的 md 附注笔记，
# 命名规则：<原文件名>.md（如 讲义.pdf → 讲义.pdf.md）。
# 该附注本身是真实 md 文件，会自动进入双链图谱与反向链接，无需改图谱引擎。

def _sidecar_rel(target_rel: str) -> str:
    """由目标资源相对路径推导其 sidecar 附注的相对路径。"""
    return target_rel + ".md"


@router.get("/sidecar")
async def get_sidecar(path: str):
    """读取某资源的附注笔记内容（不存在则返回空，前端可直接新建）。"""
    root = _get_root()
    target = _safe(path, root)  # 校验目标资源合法且在库内
    sidecar_rel = _sidecar_rel(os.path.relpath(target, root).replace(os.sep, "/"))
    sidecar_path = _safe(sidecar_rel, root)
    exists = sidecar_path.exists() and sidecar_path.is_file()
    content = ""
    if exists:
        text = await asyncio.to_thread(_read_text_or_none, sidecar_path)
        content = text if text is not None else ""
    return {
        "path": sidecar_rel,
        "content": content,
        "exists": exists,
        # 只读目录（公共基础）下的资源，其附注也随之只读
        "editable": not _is_under_readonly_dir(target, root),
    }


@router.post("/sidecar/save")
async def save_sidecar(req: SidecarSaveRequest):
    """写入某资源的附注笔记（<原文件名>.md）。"""
    root = _get_root()
    target = _safe(req.path, root)
    if _is_under_readonly_dir(target, root):
        raise HTTPException(403, "该资源位于只读目录（公共基础）下，不可添加附注")
    sidecar_rel = _sidecar_rel(os.path.relpath(target, root).replace(os.sep, "/"))
    sidecar_path = _safe(sidecar_rel, root)
    prev = backend.workspace_path
    backend.workspace_path = str(root)
    try:
        result = await file_write(str(sidecar_path), req.content)
    finally:
        backend.workspace_path = prev
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "保存失败"))
    return {"status": "ok", "path": sidecar_rel, "bytes_written": result.get("bytes_written", 0)}


@router.post("/create")
async def create_entry(req: CreateRequest):
    root = _get_root()
    name = (req.name or "").strip().strip("/\\")
    if not name or any(c in name for c in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        raise HTTPException(400, "名称非法")
    parent_rel = (req.parent or "").strip()
    target_rel = f"{parent_rel}/{name}" if parent_rel else name
    safe_path = _safe(target_rel, root)
    if _is_under_readonly_dir(safe_path, root):
        raise HTTPException(403, "该位置位于只读目录（公共基础）下，不可新建")
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
    if _is_under_readonly_dir(safe_path, root):
        raise HTTPException(403, "该文件/文件夹位于只读目录（公共基础）下，不可删除")
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


# ──────────── 标签系统 ────────────

class TagCreate(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"

class FileTagSet(BaseModel):
    tags: List[str] = []


@router.get("/tags")
async def list_tags():
    """列出所有标签。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, name, color FROM kb_tags ORDER BY name")
        rows = await cursor.fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]
    finally:
        await db.close()


@router.post("/tags")
async def create_tag(data: TagCreate):
    """创建新标签。"""
    db = await get_db()
    try:
        await db.execute("INSERT OR IGNORE INTO kb_tags (name, color) VALUES (?, ?)", (data.name.strip(), data.color))
        await db.commit()
        cursor = await db.execute("SELECT id, name, color FROM kb_tags WHERE name = ?", (data.name.strip(),))
        row = await cursor.fetchone()
        return {"id": row[0], "name": row[1], "color": row[2]} if row else {"status": "ok"}
    finally:
        await db.close()


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int):
    """删除标签（同时清除所有文件上的此标签）。"""
    db = await get_db()
    try:
        await db.execute("DELETE FROM kb_tags WHERE id = ?", (tag_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.get("/files/tags")
async def get_file_tags(file_path: str = ""):
    """获取指定文件的标签列表。"""
    if not file_path:
        raise HTTPException(400, "file_path 不能为空")
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT kt.id, kt.name, kt.color FROM kb_tags kt "
            "JOIN kb_file_tags kft ON kt.id = kft.tag_id WHERE kft.file_path = ? ORDER BY kt.name",
            (file_path,),
        )
        rows = await cursor.fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]
    finally:
        await db.close()


@router.post("/files/tags")
async def set_file_tags(data: FileTagSet, file_path: str = ""):
    """设置文件标签（替换模式：先清空再设置）。"""
    if not file_path:
        raise HTTPException(400, "file_path 不能为空")
    db = await get_db()
    try:
        # 清空旧标签
        await db.execute("DELETE FROM kb_file_tags WHERE file_path = ?", (file_path,))
        # 为每个标签名查找或创建 tag，然后关联
        for name in data.tags:
            name = name.strip()
            if not name:
                continue
            await db.execute("INSERT OR IGNORE INTO kb_tags (name, color) VALUES (?, ?)", (name, "#6366f1"))
            cursor = await db.execute("SELECT id FROM kb_tags WHERE name = ?", (name,))
            row = await cursor.fetchone()
            if row:
                await db.execute("INSERT OR IGNORE INTO kb_file_tags (file_path, tag_id) VALUES (?, ?)", (file_path, row[0]))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.get("/tree-by-tag")
async def tree_by_tag(tag: str = ""):
    """按标签筛选文件树（只返回有该标签的文件路径）。"""
    if not tag:
        return {"files": []}
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT kft.file_path FROM kb_file_tags kft "
            "JOIN kb_tags kt ON kt.id = kft.tag_id WHERE kt.name = ? ORDER BY kft.file_path",
            (tag.strip(),),
        )
        rows = await cursor.fetchall()
        return {"tag": tag, "files": [r[0] for r in rows]}
    finally:
        await db.close()


# ──────────── 自动标签 ────────────

class AutoTagRequest(BaseModel):
    file_path: str = ""  # 为空则对全部文件执行


@router.post("/tags/auto/suggest")
async def auto_tag_suggest(data: AutoTagRequest):
    """
    对指定文件提取建议标签（不自动存储）。
    返回标签名称列表，用户可在前端确认后手动应用。
    """
    from pathlib import Path
    import backend

    if not data.file_path:
        raise HTTPException(400, "file_path 不能为空")

    kb_root = getattr(backend, "kb_path", "")
    if not kb_root:
        raise HTTPException(400, "知识库尚未配置")

    abs_path = Path(kb_root) / data.file_path
    if not abs_path.exists():
        raise HTTPException(404, f"文件不存在：{data.file_path}")

    # 读取文件文本（使用 system_tools reader）
    from backend.system_tools.reader import file_read
    from backend.services.auto_tagger import auto_tag_file

    try:
        result = await file_read(str(abs_path))
        text = result.get("content", "") if isinstance(result, dict) else str(result)
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        tags = await auto_tag_file(data.file_path, text)
        return {"file_path": data.file_path, "suggested_tags": tags}
    except Exception as e:
        raise HTTPException(500, f"自动标签失败：{e}")


@router.post("/tags/auto/apply")
async def auto_tag_apply(data: AutoTagRequest):
    """
    对指定文件自动提取标签并直接写入数据库。
    如果 file_path 为空，则对知识库全部文件执行。
    """
    from pathlib import Path
    import backend

    kb_root = getattr(backend, "kb_path", "")
    if not kb_root:
        raise HTTPException(400, "知识库尚未配置")

    if data.file_path:
        abs_path = Path(kb_root) / data.file_path
        if not abs_path.exists():
            raise HTTPException(404, f"文件不存在：{data.file_path}")

        from backend.system_tools.reader import file_read
        from backend.services.auto_tagger import auto_tag_and_persist

        try:
            result = await file_read(str(abs_path))
            text = result.get("content", "") if isinstance(result, dict) else str(result)
            if isinstance(text, list):
                text = "\n".join(str(t) for t in text)
            written = await auto_tag_and_persist(data.file_path, text)
            return {"file_path": data.file_path, "tags_written": written}
        except Exception as e:
            raise HTTPException(500, f"自动标签失败：{e}")
    else:
        # 全部文件（后台异步执行）
        from backend.services.auto_tagger import auto_tag_all_files
        result = await auto_tag_all_files()
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "自动标签失败"))
        return result

