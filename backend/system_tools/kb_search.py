# backend/system_tools/kb_search.py
"""
知识库语义检索工具：供 LLM 在对话中对「我的知识库」做向量检索，
先用本工具语义命中最相关的笔记片段，再按需用 system_kb_read 精读全文。

与 system_kb_list（浏览目录）/ system_kb_read（读取全文）互补：
- kb_list：按目录结构了解知识库有哪些笔记
- kb_search：按语义找到「和问题最相关」的片段（推荐优先用）
- kb_read：拿到片段来源后读取完整笔记
"""
from typing import Any, Dict


async def kb_search(query: str, top_k: int = 5) -> Dict[str, Any]:
    """
    对知识库做语义检索。

    Args:
        query: 检索问题 / 关键语义描述。
        top_k: 返回最相关的片段数量，默认 5。

    Returns:
        dict：包含命中片段列表（来源文件、标题路径、内容、相似距离）。
    """
    # 延迟导入，避免 system_tools 包初始化时与 kb_indexer 形成循环导入
    from backend.services.kb_indexer import search as _search, KbNotConfiguredError

    if not query or not query.strip():
        return {"success": False, "error": "检索内容不能为空。"}

    top_k = max(1, min(int(top_k or 5), 20))

    try:
        hits = await _search(query.strip(), top_k)
    except KbNotConfiguredError as e:
        return {"success": False, "error": str(e)}
    except RuntimeError as e:
        # 向量扩展不可用 / 维度未定 等
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"检索失败：{e}"}

    if not hits:
        return {
            "success": True,
            "hits": [],
            "message": "未检索到相关内容。知识库可能尚未建立索引，"
                       "请提示用户在「知识库设置」中点击「重建索引」。",
        }

    return {"success": True, "count": len(hits), "hits": hits}
