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
import sys
import shlex
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from config_loader import config
from backend.utils.validators import validate_path
from backend.utils.base import is_absolute
import backend

logger = logging.getLogger("My Workbench")

# 单次命令最长执行时间（秒），可被参数覆盖，但不超过硬上限
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 600

# 单个输出流（stdout / stderr）最多保留的字符数，超出则截断
MAX_OUTPUT_CHARS = 20_000


def _get_allowed_dirs() -> list[Path]:
    """允许作为工作目录的根目录列表（工作区 + 上传目录 + 知识库）。"""
    paths = [backend.workspace_path, config.uploads_dir]
    # 知识库路径（若已配置）也允许作为工作目录
    kb = getattr(backend, "kb_path", "")
    if kb:
        paths.append(kb)
    dirs = [Path(p).resolve() for p in paths if p]
    if not dirs:
        raise RuntimeError("backend.workspace_path 未配置")
    return dirs


def _resolve_cwd(cwd: Optional[str]) -> Path:
    """把传入的 cwd 解析为工作区内的安全绝对路径。"""
    if not cwd:
        return Path(backend.workspace_path).resolve()
    if not is_absolute(cwd):
        # 相对路径：尝试拼接工作区、知识库路径
        kb = getattr(backend, "kb_path", "")
        candidates = [backend.workspace_path]
        if kb:
            candidates.append(kb)
        for base in candidates:
            resolved = Path(base).resolve() / cwd
            if resolved.exists():
                return validate_path(str(resolved), _get_allowed_dirs())
        # 都没命中，默认拼工作区（让 validate_path 报错给出明确提示）
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
        timeout: 超时时间（秒），默认 30，上限 600。

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
        # 自动创建目录（常见场景：模型在知识库「生成内容」目录首次执行命令）
        try:
            safe_cwd.mkdir(parents=True, exist_ok=True)
            logger.info(f"[runner] 自动创建工作目录: {safe_cwd}")
        except (PermissionError, OSError) as e:
            return {"success": False, "error": f"工作目录不存在且无法自动创建：{safe_cwd}（{e}）"}

    try:
        eff_timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        eff_timeout = DEFAULT_TIMEOUT

    # 检测命令是否需要 shell 特性（管道、重定向、条件执行）
    _SHELL_CHARS = set("|&><;$`")
    needs_shell = any(c in command for c in _SHELL_CHARS)

    proc = None
    last_error = None

    # 策略：优先 exec（Windows 更可靠），需要 shell 特性时用 shell
    methods_to_try = []
    if needs_shell:
        methods_to_try.append(("shell", asyncio.create_subprocess_shell))
    else:
        try:
            args = shlex.split(command)
        except ValueError:
            args = command.split()
        methods_to_try.append(("exec", lambda cmd=args, **kw: asyncio.create_subprocess_exec(*cmd, **kw)))
        methods_to_try.append(("shell", asyncio.create_subprocess_shell))

    for method_name, factory in methods_to_try:
        try:
            if method_name == "shell":
                proc = await factory(
                    command,
                    cwd=str(safe_cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await factory(
                    cwd=str(safe_cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            last_error = None
            break
        except Exception as e:
            last_error = e
            logger.warning(f"[runner] {method_name} 方式启动失败: {e!r}")

    if last_error is not None:
        logger.error(f"[runner] 所有启动方式均失败: command={command!r}, cwd={safe_cwd}, last_error={last_error!r}")
        hint = ""
        if isinstance(last_error, FileNotFoundError):
            hint = f"。提示：找不到可执行程序，请检查命令中的程序名是否在 PATH 中（当前 Python: {sys.executable}）"
        elif isinstance(last_error, PermissionError):
            hint = "。提示：权限不足，请检查目标目录的访问权限"
        elif isinstance(last_error, RuntimeError):
            hint = "。提示：事件循环不支持子进程（Windows 需 ProactorEventLoop）"
        return {
            "success": False,
            "error": f"启动进程失败：{last_error}{hint}",
            "command": command,
            "cwd": str(safe_cwd),
        }

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
