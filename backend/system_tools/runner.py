# backend/system_tools/runner.py
"""
命令执行工具：在工作区目录内执行 shell 命令（如 `node compile.js`、`python build.py`），
捕获标准输出/错误，并施加超时与输出大小保护。

设计约束（安全）：
- 工作目录（cwd）必须落在工作区目录内，防止越权到任意路径执行。
- 强制超时，防止进程挂死拖垮事件循环。
- 输出做大小截断，防止超大日志撑爆上下文。

跨平台可靠性：
- 使用 subprocess.run() + ThreadPoolExecutor，不依赖 asyncio 子进程 API。
- 避免 Windows 下 ProactorEventLoop / SelectorEventLoop 兼容性问题，
  以及 uvicorn reload spawn 子进程导致的事件循环策略失效。
"""
import asyncio
import concurrent.futures
import os
import sys
import shlex
import subprocess
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


def _run_sync(
    args: list[str] | str,
    cwd: str,
    timeout: int,
    shell: bool,
) -> subprocess.CompletedProcess:
    """
    在独立线程中执行 subprocess.run()，不接触事件循环。
    这是解决 Windows 下 ProactorEventLoop / uvicorn reload
    子进程兼容问题的关键——完全绕过 asyncio 子进程 API。
    """
    return subprocess.run(
        args,
        capture_output=True,
        cwd=cwd,
        timeout=timeout,
        shell=shell,
    )


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
    # 统一错误返回的基准字段
    def _error(msg: str, **extra) -> Dict[str, Any]:
        return {
            "success": False,
            "return_code": -1,
            "timed_out": extra.pop("timed_out", False),
            "error": msg,
            "command": command,
            "cwd": "",
            "stdout": "",
            "stderr": "",
            **extra,
        }

    if not command or not command.strip():
        return _error("command 不能为空")

    try:
        safe_cwd = _resolve_cwd(cwd)
    except (ValueError, RuntimeError) as e:
        return _error(f"工作目录校验失败：{e}")

    if not safe_cwd.exists() or not safe_cwd.is_dir():
        # 自动创建目录（常见场景：模型在知识库「生成内容」目录首次执行命令）
        try:
            safe_cwd.mkdir(parents=True, exist_ok=True)
            logger.info(f"[runner] 自动创建工作目录: {safe_cwd}")
        except (PermissionError, OSError) as e:
            return _error(f"工作目录不存在且无法自动创建：{safe_cwd}（{e}）", cwd=str(safe_cwd))

    try:
        eff_timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        eff_timeout = DEFAULT_TIMEOUT

    # 检测命令是否需要 shell 特性（管道、重定向、条件执行）
    _SHELL_CHARS = set("|&><;$`")
    needs_shell = any(c in command for c in _SHELL_CHARS)

    # 构建参数：非 shell 模式需要解析为列表
    if needs_shell:
        proc_args = command
        use_shell = True
    else:
        try:
            proc_args = shlex.split(command)
        except ValueError:
            proc_args = command.split()
        use_shell = False

    # ── 核心：用 ThreadPoolExecutor + subprocess.run 替代 asyncio 子进程 API ──
    # 优势：不依赖 ProactorEventLoop，uvicorn reload / 任何事件循环都能正常工作。
    loop = asyncio.get_running_loop()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    pool,
                    _run_sync,
                    proc_args,
                    str(safe_cwd),
                    eff_timeout,
                    use_shell,
                ),
                timeout=eff_timeout + 10,  # 10s 容差：线程调度 + Python 清理
            )
    except asyncio.TimeoutError:
        return _error(
            f"命令执行超时（>{eff_timeout}s），已终止进程。",
            timed_out=True, cwd=str(safe_cwd),
        )
    except subprocess.TimeoutExpired:
        return _error(
            f"命令执行超时（>{eff_timeout}s），已终止进程。",
            timed_out=True, cwd=str(safe_cwd),
        )
    except Exception as e:
        logger.error(f"[runner] 进程启动失败: command={command!r}, cwd={safe_cwd}, error={e!r}")
        hint = ""
        if isinstance(e, FileNotFoundError):
            hint = f"。提示：找不到可执行程序，请检查命令中的程序名是否在 PATH 中（当前 Python: {sys.executable}）"
        elif isinstance(e, PermissionError):
            hint = "。提示：权限不足，请检查目标目录的访问权限"
        return _error(f"启动进程失败：{e}{hint}", cwd=str(safe_cwd))

    stdout = _truncate(_decode(result.stdout or b""))
    stderr = _truncate(_decode(result.stderr or b""))
    return_code = result.returncode if result.returncode is not None else -1

    return {
        "success": return_code == 0,
        "return_code": return_code,
        "timed_out": False,
        "command": command,
        "cwd": str(safe_cwd),
        "stdout": stdout,
        "stderr": stderr,
    }
