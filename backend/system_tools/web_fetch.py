# backend/system_tools/web_fetch.py
"""
网页抓取工具：抓取网页内容 → 转为 Markdown → 保存到知识库 → 自动索引。

依赖：requests + markitdown（已在 requirements.txt 中）
"""
import os
import re
import time
import hashlib
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

    try:
        # 1. 抓取网页
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = _requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        # 2. 转为 Markdown
        md = MarkItDown()
        result = md.convert(resp.text, url=url)
        content = result.text_content if hasattr(result, 'text_content') else str(result)

        # 解析标题
        title = url.split("/")[-1] or "web_page"
        title_match = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()
        else:
            m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
            if m:
                title = m.group(1).strip()

        # 清理标题
        title = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]

        # 3. 写入知识库
        kb_root = getattr(backend, "kb_path", "")
        if not kb_root:
            return {"success": False, "error": "知识库尚未配置"}

        # 目标目录
        if path:
            # 用户指定路径
            dest_dir = Path(kb_root) / path
        else:
            dest_dir = Path(kb_root) / "07-生成内容" / "web_fetch"

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
            "message": f"已保存到知识库：{filename}，请提示用户可在知识库中查看。",
        }

    except _requests.RequestException as e:
        return {"success": False, "error": f"网页请求失败：{str(e)}"}
    except Exception as e:
        logger.warning(f"[web_fetch] 抓取失败: {e}")
        return {"success": False, "error": f"网页抓取失败：{str(e)}"}
