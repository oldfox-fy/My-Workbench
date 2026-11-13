# backend/utils/base.py
import os
import sys
import socket
from pathlib import Path
from datetime import datetime, timezone as tz


def is_absolute(path: str) -> bool:
    return Path(path).is_absolute()

def resource_path(relative_path):
    """获取资源的绝对路径，兼容 PyInstaller 打包"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        base_path = os.path.dirname(os.path.dirname(current_dir))
    return os.path.join(base_path, relative_path)

def get_current_time(timezone: str = "local") -> str:
    """
    获取当前真实的日期和时间。

    在没有明确时区的情况下直接使用默认值"local"。
    
    Args:
        timezone: 时区设置，可选值：
            - "local": 本地时间（默认）
            - "utc": UTC 时间
            - "iso": ISO 8601 格式的 UTC 时间
    """
    try:
       
        if timezone == "utc":
            now = datetime.now(tz.utc)
            tz_info = "UTC"
        elif timezone == "iso":
            now = datetime.now(tz.utc)
            return {
                "success": True,
                "iso": now.isoformat().replace("+00:00", "Z"),
                "timestamp": int(now.timestamp()),
                "timezone": "UTC",
            }
        else:  # local
            now = datetime.now()
            tz_info = "本地时间"
        
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday_cn = weekdays[now.weekday()]
        
        return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekday_cn} "
    except Exception as e:
        return f"获取时间失败：{str(e)}"

# OpenAI 兼容 API 常见的具体端点路径（用户可能误填为 Base URL）
_ENDPOINT_PATHS = {
    "/models",
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/audio/transcriptions",
    "/audio/translations",
    "/audio/speech",
    "/images/generations",
    "/images/edits",
    "/moderations",
    "/files",
    "/fine_tuning",
    "/fine-tunes",
}


def normalize_base_url(url: str) -> str:
    """
    规范化 OpenAI 兼容 API 的 Base URL。

    自动处理两种常见错误：
    1. 缺少 /v1 → 自动补全
    2. 用户误填了具体端点（如 /chat/completions）→ 裁切到 /v1
    """
    if not url:
        return url
    from urllib.parse import urlparse

    url = url.rstrip("/")
    parsed = urlparse(url)
    path = parsed.path

    # 裁掉用户误填的具体端点路径（如 /v1/audio/transcriptions → /v1）
    for ep in _ENDPOINT_PATHS:
        if path.endswith(ep):
            path = path[: -len(ep)].rstrip("/")
            break

    # 重建不包含多余路径的 URL
    base = f"{parsed.scheme}://{parsed.netloc}{path}"

    # 如果没有 /v 版本路径则追加 /v1
    if not (path.endswith("/v1") or "/v1/" in path or "/v2/" in path or "/v2/" in path):
        base += "/v1"

    return base


def to_download_url(file_path: str) -> str:
    """
    把服务端可访问的文件路径转换为 /files/generate/ 下载链接。

    按「工作区 / 用户工作区 / 知识库 / 生成目录 / 上传目录」依次寻找能包含
    该文件的根目录，返回相对根目录的下载链接；都找不到时退化为仅用文件名。
    与 main.py 的 /files/generate/{file_path} 端点解析逻辑保持一致。
    """
    import backend as _backend
    from config_loader import config as _config

    file_path = os.path.abspath(str(file_path or ""))
    roots = [
        getattr(_backend, "workspace_path", ""),
        (_backend.get_user_workspace_path() if hasattr(_backend, "get_user_workspace_path") else ""),
        getattr(_backend, "kb_path", ""),
        str(getattr(_config, "generate_dir", "")),
        str(getattr(_config, "uploads_dir", "")),
    ]
    for root in roots:
        if not root:
            continue
        try:
            rel = os.path.relpath(file_path, root)
        except ValueError:
            continue
        if rel == "." or rel.startswith("..") or os.path.isabs(rel):
            continue
        rel = rel.replace(os.sep, "/")
        return f"/files/generate/{rel}"
    return f"/files/generate/{os.path.basename(file_path)}"


def get_local_ip():
    """获取本机IP地址"""
    try:
        # 创建一个UDP套接字，连接到一个外部地址（不发送数据）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"