# backend/system_tools/kb_reader.py
"""
知识库只读工具：供 LLM 在对话中浏览「我的知识库」目录树、读取笔记内容进行分析。

设计约束：
- 只读。不提供任何写入/删除能力，避免误改用户笔记。
- 所有路径均以知识库根目录（backend.kb_path）为基准的相对路径，内部解析并经
  validate_path 严格限制在根目录内，防止路径遍历。
- 若用户尚未在界面中配置知识库目录，返回明确的提示信息而非抛错。
"""
import os
import asyncio
from pathlib import Path
from typing import Optional, List

import backend
from backend.utils.validators import validate_path
from backend.system_tools.reader import file_read, FileReadError

# 目录树中忽略的名称（与 routes/knowledge.py 保持一致）
_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}


class KbNotConfiguredError(Exception):
    """知识库根目录尚未配置或无效。"""
    pass


def _kb_root() -> Path:
    """获取并校验知识库根目录，未配置时抛 KbNotConfiguredError。"""
    root = getattr(backend, "kb_path", "")
    if not root:
        raise KbNotConfiguredError(
            "知识库尚未配置。请提示用户先在「我的知识库」界面选择一个根目录。"
        )
    p = Path(root)
    if not p.is_dir():
        raise KbNotConfiguredError(f"知识库目录不存在或不是有效目录：{root}")
    return p.resolve()


def _resolve_in_kb(rel_or_abs: str, root: Path) -> Path:
    """将输入路径（相对根目录或绝对）解析并校验限制在知识库根目录内。"""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = root / rel_or_abs
    try:
        return validate_path(str(p), [root])
    except ValueError as e:
        raise FileReadError(f"路径校验失败：{e}", code="PATH_DENIED") from e


async def kb_list(
    subpath: str = "",
    max_files: int = 500,
    max_depth: int = 10,
) -> str:
    """
    列出知识库目录树（相对知识库根目录的路径），供 LLM 了解知识库结构后再按需读取。

    Args:
        subpath: 相对知识库根目录的子目录，空表示从根目录开始。
        max_files: 返回的最大条目数量，防止上下文溢出，默认 500。
        max_depth: 递归的最大深度，默认 10。

    Returns:
        以换行分隔的相对路径列表字符串；目录以 "/" 结尾。
    """
    try:
        root = _kb_root()
    except KbNotConfiguredError as e:
        return str(e)

    start = _resolve_in_kb(subpath, root) if subpath else root
    if not start.exists():
        return f"目录不存在：{subpath or '.'}"
    if not start.is_dir():
        return f"路径不是目录：{subpath or '.'}"

    def _walk() -> str:
        lines: List[str] = []
        truncated = False

        def _recurse(cur: Path, depth: int):
            nonlocal truncated
            if truncated or depth > max_depth:
                return
            try:
                entries = sorted(
                    os.scandir(cur),
                    key=lambda e: (not e.is_dir(), e.name.lower()),
                )
            except OSError:
                return
            for entry in entries:
                if entry.name in _IGNORE or entry.name.startswith("."):
                    continue
                if len(lines) >= max_files:
                    truncated = True
                    return
                rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                if entry.is_dir(follow_symlinks=False):
                    lines.append(rel + "/")
                    _recurse(Path(entry.path), depth + 1)
                else:
                    lines.append(rel)

        _recurse(start, 0)

        header = f"知识库根目录：{root}"
        if not lines:
            return f"{header}\n(空)"
        body = "\n".join(lines)
        if truncated:
            body += f"\n\n(已截断：达到 {max_files} 条上限，请指定更具体的 subpath)"
        return f"{header}\n\n{body}"

    return await asyncio.to_thread(_walk)


async def kb_read(
    path: str,
    sheet_name: Optional[str] = None,
    encoding: str = "UTF-8",
    max_size_mb: int = 10,
) -> dict:
    """
    读取知识库中某个文件的内容。path 为相对知识库根目录的路径。

    支持与 system_read_file 相同的多格式解析（文本/表格/文档/图片等）。
    返回包含 content、format、mime_type、metadata 的字典。
    """
    try:
        root = _kb_root()
    except KbNotConfiguredError as e:
        return {"success": False, "error": str(e)}

    try:
        safe_path = _resolve_in_kb(path, root)
    except FileReadError as e:
        return {"success": False, "error": str(e)}

    if not safe_path.exists():
        return {"success": False, "error": f"文件不存在：{path}"}
    if not safe_path.is_file():
        return {"success": False, "error": f"路径不是文件：{path}"}

    try:
        result = await file_read(
            str(safe_path),
            sheet_name=sheet_name,
            encoding=encoding,
            max_size_mb=max_size_mb,
            allowed_dirs=[str(root)],
        )
    except FileReadError as e:
        return {"success": False, "error": str(e)}

    # 附上相对路径，便于 LLM 引用
    result["kb_path"] = os.path.relpath(safe_path, root).replace(os.sep, "/")
    return result
