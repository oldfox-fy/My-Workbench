# backend/system_tools/web_fetch.py
"""
网页抓取工具：抓取网页内容 → 转为 Markdown → 保存到知识库 → 自动索引。

依赖：requests + markitdown（已在 requirements.txt 中）
"""
import io as _io
import os
import re
import time
import hashlib
import asyncio as _asyncio
from pathlib import Path
from typing import Any, Dict

import backend
from backend.bootstrap import logger


async def web_fetch(url: str, path: str = "") -> Dict[str, Any]:
    """
    抓取网页内容并保存到知识库。

    Args:
        url: 网页地址
        path: 保存路径（相对于知识库的「07-生成内容/web_fetch/」目录），省略则自动生成

    Returns:
        {success, file_path, title, summary}
    """
    import requests as _requests
    from markitdown import MarkItDown

    if not url or not url.strip():
        return {"success": False, "error": "url 不能为空"}

    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "url 必须以 http:// 或 https:// 开头"}

    url = url.strip()

    # ── 全部同步 I/O（HTTP 请求 + markitdown 解析）放入线程池 ──
    # 防止阻塞事件循环导致 delegate_task 等嵌套调用永久卡死。
    loop = _asyncio.get_running_loop()

    def _sync_fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = _requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        # markitdown 转换：用 BytesIO 包裹字节内容 + file_extension，
        # 避免 markitdown 把 HTML 文本当成文件路径处理。
        md = MarkItDown()
        result = md.convert(_io.BytesIO(resp.content), file_extension=".html", url=url)
        content = result.text_content if hasattr(result, 'text_content') else str(result)

        return resp.text, content

    try:
        raw_html, content = await loop.run_in_executor(None, _sync_fetch)
    except _requests.RequestException as e:
        return {"success": False, "error": f"网页请求失败：{str(e)}"}
    except Exception as e:
        logger.warning(f"[web_fetch] 抓取失败: {e}")
        return {"success": False, "error": f"网页抓取失败：{str(e)}"}

    # ── 以下为纯 CPU/本地 I/O，回到事件循环线程执行 ──

    # 解析标题
    title = url.split("/")[-1] or "web_page"
    title_match = re.search(r"<title>(.*?)</title>", raw_html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
    else:
        m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
        if m:
            title = m.group(1).strip()

    # 清理标题
    title = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]

    # 写入工作区（而非知识库）
    workspace = getattr(backend, "workspace_path", "")
    if not workspace:
        return {"success": False, "error": "工作区尚未配置"}

    # 目标目录：workspace/web_fetch/ 或用户指定子路径
    if path:
        dest_dir = Path(workspace) / path
    else:
        dest_dir = Path(workspace) / "web_fetch"

    dest_dir.mkdir(parents=True, exist_ok=True)

    # 生成唯一文件名
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{safe_title}_{ts}.md"
    filepath = dest_dir / filename

    # 添加元数据头部
    full_content = (
        f"# {title}\n\n"
        f"> 来源: {url}\n"
        f"> 抓取时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"---\n\n"
        f"{content}"
    )

    filepath.write_text(full_content, encoding="utf-8")

    # 摘要
    summary = content[:300].replace("\n", " ").strip()

    return {
        "success": True,
        "file_path": str(filepath),
        "title": title,
        "summary": summary,
        "message": f"已保存到工作区：web_fetch/{filename}",
    }
