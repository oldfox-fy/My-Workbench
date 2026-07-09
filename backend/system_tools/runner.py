# backend/system_tools/runner.py
"""
命令执行工具：在工作区目录内执行 shell 命令（如 `node compile.js`、`python build.py`），
捕获标准输出/错误，并施加超时与输出大小保护。

设计约束（安全）：
- 工作目录（cwd）必须落在工作区目录内，防止越权到任意路径执行。
- 强制超时，防止进程挂死拖垮事件循环。
- 输出做大小截断，防止超大日志撑爆上下文。
"""
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

from config_loader import config
from backend.utils.validators import validate_path
from backend.utils.base import is_absolute
import backend

# 单次命令最长执行时间（秒），可被参数覆盖，但不超过硬上限
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600

# 单个输出流（stdout / stderr）最多保留的字符数，超出则截断
MAX_OUTPUT_CHARS = 20_000


def _get_allowed_dirs() -> list[Path]:
    """允许作为工作目录的根目录列表（工作区 + 上传目录）。"""
    paths = [backend.workspace_path, config.uploads_dir]
    dirs = [Path(p).resolve() for p in paths if p]
    if not dirs:
        raise RuntimeError("backend.workspace_path 未配置")
    return dirs


def _resolve_cwd(cwd: Optional[str]) -> Path:
    """把传入的 cwd 解析为工作区内的安全绝对路径。"""
    if not cwd:
        return Path(backend.workspace_path).resolve()
    if not is_absolute(cwd):
        cwd = os.path.join(backend.workspace_path, cwd)
    return validate_path(cwd, _get_allowed_dirs())


def _decode(raw: bytes) -> str:
    """尽量把子进程字节输出解码为文本（先 UTF-8，再 GBK，最后宽容替换）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = text[:MAX_OUTPUT_CHARS]
    return f"{head}\n...（输出过长，已截断，共 {len(text)} 字符）"


async def run_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    在工作区目录内执行一条 shell 命令，并返回执行结果。

    典型用途：编译前端生成的脚本（如用 pptxgenjs 生成 PPTX：`node compile.js`）、
    运行 Python 脚本、执行构建命令等。

    Args:
        command: 要执行的完整命令行字符串，例如 "node compile.js"。
        cwd:     命令的工作目录（相对工作区或绝对路径），必须位于工作区内。
                 省略时默认为工作区根目录。
        timeout: 超时时间（秒），默认 120，上限 600。

    Returns:
        dict：包含 success、return_code、stdout、stderr、cwd、command、timed_out 等字段。
    """
    if not command or not command.strip():
        return {"success": False, "error": "command 不能为空"}

    try:
        safe_cwd = _resolve_cwd(cwd)
    except (ValueError, RuntimeError) as e:
        return {"success": False, "error": f"工作目录校验失败：{e}"}

    if not safe_cwd.exists() or not safe_cwd.is_dir():
        return {"success": False, "error": f"工作目录不存在或不是目录：{safe_cwd}"}

    try:
        eff_timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        eff_timeout = DEFAULT_TIMEOUT

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(safe_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return {"success": False, "error": f"启动进程失败：{e}", "command": command, "cwd": str(safe_cwd)}

    timed_out = False
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=eff_timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return {
            "success": False,
            "timed_out": True,
            "error": f"命令执行超时（>{eff_timeout}s），已终止进程。",
            "command": command,
            "cwd": str(safe_cwd),
        }

    stdout = _truncate(_decode(stdout_raw or b""))
    stderr = _truncate(_decode(stderr_raw or b""))
    return_code = proc.returncode if proc.returncode is not None else -1

    return {
        "success": return_code == 0,
        "return_code": return_code,
        "timed_out": timed_out,
        "command": command,
        "cwd": str(safe_cwd),
        "stdout": stdout,
        "stderr": stderr,
    }
