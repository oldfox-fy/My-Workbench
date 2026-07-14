# backend/services/model_router.py
"""
模型路由器：根据消息内容自动选择合适的模型。

检测规则（按优先级）：
  1. 图片/多模态内容 → role=vision
  2. 音频内容 → role=audio
  3. 图像生成请求 → role=image_gen
  4. 深度推理关键词 → role=reasoning
  5. 其余 → role=default

如果目标角色没有配置模型，不切换（保留用户当前模型）。
"""
from typing import Dict, List, Optional, Any
from backend.database import get_db


# 图像生成触发关键词（中英文）
# 注意：关键词必须明确指向"图片/图像/画"——不能仅凭"生成"就切换，
# 否则"生成一段代码""生成一个总结"等纯文本请求也会被误路由到生图模型。
IMAGE_GEN_KEYWORDS = [
    # 中文 — 明确包含"图""图片""图像""一张""一幅"等图像产物的词
    "生成一张", "生成一幅", "生成图片", "生成图像",
    "画一张", "画一幅", "画一张图", "画一幅图",
    "生成一张图", "生成一张图片", "画一张图片",
    "来张图", "做一张图", "做张图",
    "创建一张", "创建图片", "创建一张图片",
    # 英文 — 明确包含 image/picture/photo 等图像产物
    "generate an image", "generate a picture", "generate a photo",
    "create an image", "create a picture", "create a photo",
    "make an image", "make a picture", "make a photo",
    "draw an image", "draw a picture", "draw a photo",
]

# 深度推理触发关键词（中英文）
REASONING_KEYWORDS = [
    # 中文
    "分析", "推理", "对比", "为什么", "解释原理", "深入", "源码",
    "底层", "原理", "论证", "逻辑", "辩证", "复杂度", "评估",
    "权衡", "利弊", "优化方案", "架构设计", "代码审查", "review",
    # 英文
    "analyze", "reasoning", "compare", "explain why", "in depth",
    "deep dive", "trade off", "architecture", "evaluate", "critique",
    "refactor", "optimize", "complexity", "root cause",
]

# 已知支持多模态的模型名关键词（不区分大小写）
VISION_CAPABLE_KEYWORDS = [
    "gpt-4o", "gpt-4-turbo", "gpt-4-vision", "claude", "gemini",
    "vision", "multimodal", "llava", "bakllava", "cogvlm", "qwen-vl",
    "glm-4v", "yi-vl", "internvl", "phi-3-vision", "minicpm-v",
    "pixtral", "llama-3.2-90b", "llama-3.2-11b",
]


def _looks_vision_capable(model_name: str) -> bool:
    """通过模型名推断是否原生支持图片/多模态。"""
    name_lower = (model_name or "").lower()
    return any(kw in name_lower for kw in VISION_CAPABLE_KEYWORDS)


def _has_image_content(messages: List[Dict[str, Any]]) -> bool:
    """检测消息列表中是否包含用户上传的图片/多模态内容。

    只检查 user 角色的消息，避免匹配 assistant 回复中的 markdown 图片。
    """
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
        elif isinstance(content, str):
            # 检测 base64 图片内联（data:image/...）
            if "data:image/" in content:
                return True
    return False


def _has_audio_content(messages: List[Dict[str, Any]]) -> bool:
    """检测消息列表中是否包含音频内容。"""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    ptype = part.get("type", "")
                    if ptype in ("audio", "input_audio"):
                        return True
                    if "audio_url" in part or "input_audio" in part:
                        return True
    return False


def _needs_image_gen(messages: List[Dict[str, Any]]) -> bool:
    """检测用户是否在请求图像生成。

    只检查最后一条用户消息的关键词。
    """
    if not messages:
        return False

    last_user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_text = content.lower()
            elif isinstance(content, list):
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") == "text":
                        last_user_text = part.get("text", "").lower()
                        break
            break

    return any(kw.lower() in last_user_text for kw in IMAGE_GEN_KEYWORDS)


def _needs_reasoning(messages: List[Dict[str, Any]], enable_tools: bool = False) -> bool:
    """检测是否需要深度推理。

    判断依据：
    1. 最后一条用户消息包含推理关键词（>=2 个命中）
    2. 消息中有大量代码块（>3 个）
    3. 启用了工具调用 + 总文本较长（>500 字符）
    """
    if not messages:
        return False

    # 获取最后一条用户消息（倒序查找）
    last_user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_text = content.lower()
            elif isinstance(content, list):
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") == "text":
                        last_user_text = part.get("text", "").lower()
                        break
            break

    # 关键词匹配（>=2 个命中才触发，减少误判）
    keyword_hits = sum(1 for kw in REASONING_KEYWORDS if kw.lower() in last_user_text)
    if keyword_hits >= 2:
        return True

    # 代码块数量检测
    all_text = ""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            all_text += content

    code_block_count = all_text.count("```")
    if code_block_count >= 6:  # 3 对 ``` 标记
        return True

    # 工具调用 + 长消息
    if enable_tools and len(all_text) > 500:
        return True

    return False


def detect_input_role(messages: List[Dict[str, Any]], enable_tools: bool = False) -> str:
    """检测输入内容类型，返回应使用的模型角色名。

    优先级: image_gen > vision > audio > reasoning > default

    注意: image_gen 优先级最高，因为用户可能在对话中上传了参考图片后
    再说"生成一张类似的"，此时关键词检测命中 → 应优先走生图模型；
    而仅上传图片但未触发生图关键词时仍走 vision。
    """
    if _needs_image_gen(messages):
        return "image_gen"
    if _has_image_content(messages):
        return "vision"
    if _has_audio_content(messages):
        return "audio"
    if _needs_reasoning(messages, enable_tools):
        return "reasoning"
    return "default"


async def get_model_by_role(role: str) -> Optional[Dict[str, Any]]:
    """从数据库获取指定角色的第一个模型配置。

    若该角色无模型，返回 None（由调用方决定是否 fallback）。
    """
    db = await get_db()

    cursor = await db.execute(
        "SELECT id, name, type, modelName, baseUrl, apiKey, role FROM models WHERE role = ? LIMIT 1",
        (role,)
    )
    row = await cursor.fetchone()

    if row:
        result = {
            "id": row[0], "name": row[1], "type": row[2],
            "modelName": row[3], "baseUrl": row[4], "apiKey": row[5],
            "role": row[6] if len(row) > 6 else "default",
        }
        await db.close()
        return result

    # 若请求的是非 default 角色且无匹配，返回 None
    # 调用方会判断是否让用户当前模型继续处理
    await db.close()
    return None


async def get_default_model() -> Optional[Dict[str, Any]]:
    """获取 default 角色的模型（用于 fallback）。"""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, name, type, modelName, baseUrl, apiKey, role FROM models WHERE role = 'default' LIMIT 1"
    )
    row = await cursor.fetchone()
    if not row:
        # 终极 fallback：任意模型
        cursor = await db.execute(
            "SELECT id, name, type, modelName, baseUrl, apiKey, role FROM models LIMIT 1"
        )
        row = await cursor.fetchone()
    await db.close()

    if row:
        return {
            "id": row[0], "name": row[1], "type": row[2],
            "modelName": row[3], "baseUrl": row[4], "apiKey": row[5],
            "role": row[6] if len(row) > 6 else "default",
        }
    return None


async def lookup_model_role(model_name: str, base_url: str = "") -> str:
    """根据模型名和 base URL 从数据库查找其真实角色。

    前端可能不发送 role 字段（用户手动选择模型而非自动切换时），
    此函数作为后端兜底，确保生图等特殊路由不会因为 role=default 而走错 API。
    """
    if not model_name:
        return "default"
    db = await get_db()
    cursor = await db.execute(
        "SELECT role FROM models WHERE modelName = ? AND baseUrl = ? LIMIT 1",
        (model_name, base_url)
    )
    row = await cursor.fetchone()
    # 如果精确匹配失败，尝试仅按 modelName 查找
    if not row:
        cursor = await db.execute(
            "SELECT role FROM models WHERE modelName = ? LIMIT 1",
            (model_name,)
        )
        row = await cursor.fetchone()
    await db.close()
    if row and row[0]:
        return row[0]
    return "default"


def should_switch_for_images(current_model_name: str, messages: List[Dict[str, Any]]) -> bool:
    """判断是否需要为图片切换到 vision 模型。

    如果当前模型本身支持多模态，不切换。
    """
    if not _has_image_content(messages):
        return False
    return not _looks_vision_capable(current_model_name)
