# backend/system_tools/ppt_generator.py
"""
PPT 生成工具：根据结构化幻灯片数据生成 .pptx 文件。

支持：
- 4 种页面布局：cover（封面）、toc（目录）、content（内容）、ending（结尾）
- 3 套配色主题：blue（简约蓝）、warm（活力橙）、clean（极简白）
- 基础 Markdown 解析：**加粗**、- 列表、1. 编号
- 生成 .pptx → 返回下载链接 + 推送前端预览标记

Agent 执行流程：
  用户输入 → system_todo 规划大纲 → LLM 生成 slides JSON
  → system_generate_pptx → 前端渲染幻灯片卡片 + 下载链接
"""
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

from config_loader import config
import backend

# ── 预览事件队列 ──
_ppt_preview_events: List[str] = []


def push_ppt_preview(marker: str):
    """追加一条 PPT 预览标记到事件队列。"""
    _ppt_preview_events.append(marker)


def pop_ppt_preview_events() -> List[str]:
    """取出并清空当前累积的所有 PPT 预览事件标记。"""
    global _ppt_preview_events
    events = _ppt_preview_events
    _ppt_preview_events = []
    return events


# ── 主题配置 ──
THEMES = {
    "blue": {
        "name": "简约蓝",
        "bg": RGBColor(0xFF, 0xFF, 0xFF),
        "title_color": RGBColor(0x25, 0x63, 0xEB),   # 蓝色标题
        "text_color": RGBColor(0x1E, 0x29, 0x3B),     # 深灰正文
        "accent": RGBColor(0x25, 0x63, 0xEB),
        "accent_light": RGBColor(0xDB, 0xEA, 0xFE),
        "footer_color": RGBColor(0x94, 0xA3, 0xB8),
    },
    "warm": {
        "name": "活力橙",
        "bg": RGBColor(0xFF, 0xFB, 0xEB),
        "title_color": RGBColor(0xEA, 0x58, 0x0C),
        "text_color": RGBColor(0x44, 0x40, 0x3C),
        "accent": RGBColor(0xEA, 0x58, 0x0C),
        "accent_light": RGBColor(0xFE, 0xD7, 0xAA),
        "footer_color": RGBColor(0xA8, 0xA2, 0x9E),
    },
    "clean": {
        "name": "极简白",
        "bg": RGBColor(0xFF, 0xFF, 0xFF),
        "title_color": RGBColor(0x0F, 0x17, 0x2A),
        "text_color": RGBColor(0x47, 0x55, 0x69),
        "accent": RGBColor(0x47, 0x55, 0x69),
        "accent_light": RGBColor(0xF1, 0xF5, 0xF9),
        "footer_color": RGBColor(0x94, 0xA3, 0xB8),
    },
}

DEFAULT_THEME = "blue"

# ── 字体配置 ──
FONT_FAMILY = "Microsoft YaHei"  # 微软雅黑（Windows 默认中文字体）
FONT_FAMILY_FALLBACK = "SimSun"   # 备选：宋体

# ── 幻灯片尺寸（16:9 宽屏） ──
SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)


def _get_font_family() -> str:
    """检测可用字体，优先微软雅黑。"""
    return FONT_FAMILY


def _apply_theme_colors(theme_name: str) -> Dict[str, Any]:
    """获取主题配色，无效主题回退默认。"""
    return THEMES.get(theme_name, THEMES[DEFAULT_THEME])


def _parse_markdown_runs(text: str, paragraph, theme: Dict):
    """将基础 Markdown 文本转换为 python-pptx 的富文本 runs。

    支持：**加粗**、*斜体*、- / * 无序列表、1. 有序列表。
    """
    import re

    font_family = _get_font_family()
    text_color = theme["text_color"]

    # 清除首尾空白
    text = text.strip()

    # 检测列表项
    bullet_match = re.match(r'^(\s*)[-*]\s+(.+)$', text)
    numbered_match = re.match(r'^(\s*)\d+[.)]\s+(.+)$', text)

    if bullet_match:
        paragraph.level = min(len(bullet_match.group(1)) // 2, 8)
        text = bullet_match.group(2)
    elif numbered_match:
        paragraph.level = min(len(numbered_match.group(1)) // 2, 8)
        text = numbered_match.group(2)

    # 按 ** 和 * 分段
    segments = re.split(r'(\*\*.*?\*\*|\*[^*]+\*)', text)

    for seg in segments:
        if not seg:
            continue
        if seg.startswith('**') and seg.endswith('**'):
            run = paragraph.add_run()
            run.text = seg[2:-2]
            run.font.bold = True
            run.font.size = Pt(18)
            run.font.color.rgb = text_color
            run.font.name = font_family
        elif seg.startswith('*') and seg.endswith('*') and len(seg) > 2:
            run = paragraph.add_run()
            run.text = seg[1:-1]
            run.font.italic = True
            run.font.size = Pt(18)
            run.font.color.rgb = text_color
            run.font.name = font_family
        else:
            run = paragraph.add_run()
            run.text = seg
            run.font.size = Pt(18)
            run.font.color.rgb = text_color
            run.font.name = font_family


def _add_cover_slide(prs, slide_data: Dict, theme: Dict, title: str = ""):
    """添加封面页。"""
    slide_layout = prs.slide_layouts[6]  # 空白布局
    slide = prs.slides.add_slide(slide_layout)

    # 背景色
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = theme["bg"]

    # 顶部装饰条
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.08)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = theme["accent"]
    shape.line.fill.background()

    # 主标题
    slide_title = slide_data.get("title", title or "演示文稿")
    txBox = slide.shapes.add_textbox(Inches(1.5), Inches(2.2), Inches(10.3), Inches(1.8))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = slide_title
    p.font.size = Pt(44)
    p.font.bold = True
    p.font.color.rgb = theme["title_color"]
    p.font.name = _get_font_family()
    p.alignment = PP_ALIGN.CENTER

    # 副标题
    subtitle = slide_data.get("subtitle", "")
    if subtitle:
        txBox2 = slide.shapes.add_textbox(Inches(1.5), Inches(4.2), Inches(10.3), Inches(1.0))
        tf2 = txBox2.text_frame
        tf2.word_wrap = True
        p2 = tf2.paragraphs[0]
        p2.text = subtitle
        p2.font.size = Pt(22)
        p2.font.color.rgb = theme["text_color"]
        p2.font.name = _get_font_family()
        p2.alignment = PP_ALIGN.CENTER

    # 底部信息
    speaker = slide_data.get("speaker", "")
    if speaker:
        txBox3 = slide.shapes.add_textbox(Inches(1.5), Inches(6.2), Inches(10.3), Inches(0.6))
        tf3 = txBox3.text_frame
        p3 = tf3.paragraphs[0]
        p3.text = speaker
        p3.font.size = Pt(14)
        p3.font.color.rgb = theme["footer_color"]
        p3.font.name = _get_font_family()
        p3.alignment = PP_ALIGN.CENTER


def _add_toc_slide(prs, slide_data: Dict, theme: Dict, index: int):
    """添加目录页。"""
    slide_layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(slide_layout)

    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = theme["bg"]

    # 标题
    slide_title = slide_data.get("title", "目录")
    txBox = slide.shapes.add_textbox(Inches(1.0), Inches(0.6), Inches(11.3), Inches(0.9))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = slide_title
    p.font.size = Pt(36)
    p.font.bold = True
    p.font.color.rgb = theme["title_color"]
    p.font.name = _get_font_family()

    # 左侧装饰线
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(1.0), Inches(1.7), Inches(11.3), Inches(0.03)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = theme["accent"]
    shape.line.fill.background()

    # 目录项
    items = slide_data.get("items", [])
    if not items:
        items = slide_data.get("bullets", [])

    y_start = 2.2
    for i, item in enumerate(items, 1):
        # 编号
        txNum = slide.shapes.add_textbox(Inches(1.2), Inches(y_start + i * 0.7), Inches(0.8), Inches(0.5))
        tn = txNum.text_frame
        tnp = tn.paragraphs[0]
        tnp.text = f"{i:02d}"
        tnp.font.size = Pt(24)
        tnp.font.bold = True
        tnp.font.color.rgb = theme["accent"]
        tnp.font.name = _get_font_family()

        # 条目文字
        txItem = slide.shapes.add_textbox(Inches(2.2), Inches(y_start + i * 0.7), Inches(9.5), Inches(0.5))
        ti = txItem.text_frame
        tip = ti.paragraphs[0]
        tip.text = item
        tip.font.size = Pt(20)
        tip.font.color.rgb = theme["text_color"]
        tip.font.name = _get_font_family()


def _add_content_slide(prs, slide_data: Dict, theme: Dict, index: int):
    """添加内容页。"""
    slide_layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(slide_layout)

    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = theme["bg"]

    # 页面标题
    slide_title = slide_data.get("title", f"第 {index + 1} 页")
    txBox = slide.shapes.add_textbox(Inches(1.0), Inches(0.5), Inches(11.3), Inches(0.8))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = slide_title
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = theme["title_color"]
    p.font.name = _get_font_family()

    # 标题下划线
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(1.0), Inches(1.4), Inches(2.5), Inches(0.05)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = theme["accent"]
    shape.line.fill.background()

    # 正文区域
    content = slide_data.get("content", "")
    bullets = slide_data.get("bullets", [])

    txBody = slide.shapes.add_textbox(Inches(1.2), Inches(1.8), Inches(10.9), Inches(5.0))
    tf_body = txBody.text_frame
    tf_body.word_wrap = True

    lines = []
    if bullets:
        lines = [f"- {b}" for b in bullets]
    elif content:
        lines = content.strip().split('\n')

    if not lines:
        lines = [""]

    for i, line in enumerate(lines):
        if i == 0:
            para = tf_body.paragraphs[0]
        else:
            para = tf_body.add_paragraph()
        _parse_markdown_runs(line, para, theme)

    # 页码
    txPage = slide.shapes.add_textbox(Inches(11.5), Inches(7.0), Inches(1.5), Inches(0.4))
    tp = txPage.text_frame
    tpp = tp.paragraphs[0]
    tpp.text = str(index + 1)
    tpp.font.size = Pt(11)
    tpp.font.color.rgb = theme["footer_color"]
    tpp.font.name = _get_font_family()
    tpp.alignment = PP_ALIGN.RIGHT


def _add_ending_slide(prs, slide_data: Dict, theme: Dict, index: int):
    """添加结尾页。"""
    slide_layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(slide_layout)

    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = theme["bg"]

    # 顶部装饰条
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.08)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = theme["accent"]
    shape.line.fill.background()

    # 主文字
    slide_title = slide_data.get("title", "感谢聆听")
    txBox = slide.shapes.add_textbox(Inches(1.5), Inches(2.5), Inches(10.3), Inches(1.5))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = slide_title
    p.font.size = Pt(48)
    p.font.bold = True
    p.font.color.rgb = theme["title_color"]
    p.font.name = _get_font_family()
    p.alignment = PP_ALIGN.CENTER

    subtitle = slide_data.get("subtitle", "")
    if subtitle:
        txBox2 = slide.shapes.add_textbox(Inches(1.5), Inches(4.2), Inches(10.3), Inches(0.8))
        tf2 = txBox2.text_frame
        p2 = tf2.paragraphs[0]
        p2.text = subtitle
        p2.font.size = Pt(20)
        p2.font.color.rgb = theme["text_color"]
        p2.font.name = _get_font_family()
        p2.alignment = PP_ALIGN.CENTER


# 布局分发映射
_LAYOUT_HANDLERS = {
    "cover": _add_cover_slide,
    "toc": _add_toc_slide,
    "content": _add_content_slide,
    "ending": _add_ending_slide,
}


async def generate_pptx(
    slides: list = None,
    theme: str = "blue",
    filename: str = "",
    title: str = "",
    **_kwargs,
) -> Dict[str, Any]:
    """
    根据结构化幻灯片数据生成 .pptx 演示文稿文件。

    Args:
        slides: 幻灯片数组，每项是一个页面的描述对象：
            {title, subtitle?, type?, content?, bullets?, items?, speaker?}
            type 可选值：cover / toc / content / ending（默认 content）
        theme: 主题配色方案，可选 blue / warm / clean（默认 blue）
        filename: 输出文件名（不含扩展名），省略则自动生成
        title: 演示文稿总标题，显示在封面页

    Returns:
        {success, path, download_url, slide_count, filename, slides_preview}
        同时推送 <!--ppt_preview:...--> 标记供前端渲染预览卡片。
    """
    slides = slides or []
    if not isinstance(slides, list) or not slides:
        return {"success": False, "error": "slides 必须是非空数组"}

    theme_name = theme if theme in THEMES else DEFAULT_THEME
    theme_colors = _apply_theme_colors(theme_name)

    # ── 创建 Presentation ──
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT

    # ── 逐页生成 ──
    preview_slides = []
    for i, slide_data in enumerate(slides):
        if not isinstance(slide_data, dict):
            continue
        slide_type = slide_data.get("type", "content")
        handler = _LAYOUT_HANDLERS.get(slide_type, _add_content_slide)
        handler(prs, slide_data, theme_colors, i)

        # 累积预览数据（截断正文至 120 字符）
        raw_content = slide_data.get("content", "")
        bullets = slide_data.get("bullets", [])
        if bullets:
            content_preview = " · ".join(bullets[:3])
        elif raw_content:
            content_preview = raw_content[:120].replace('\n', ' ')
        else:
            content_preview = ""

        preview_slides.append({
            "title": slide_data.get("title", ""),
            "content_preview": content_preview,
            "type": slide_type,
        })

    # ── 确定输出路径 ──
    output_dir = config.generate_dir
    os.makedirs(output_dir, exist_ok=True)

    safe_filename = filename.strip() if filename and filename.strip() else ""
    if not safe_filename:
        safe_filename = f"presentation_{uuid.uuid4().hex[:8]}"
    # 移除不安全字符
    safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in "._-（）")
    if not safe_filename:
        safe_filename = f"presentation_{uuid.uuid4().hex[:8]}"

    pptx_path = os.path.join(str(output_dir), f"{safe_filename}.pptx")

    # 避免覆盖已有文件
    counter = 1
    while os.path.exists(pptx_path):
        pptx_path = os.path.join(str(output_dir), f"{safe_filename}_{counter}.pptx")
        counter += 1

    # ── 保存 ──
    try:
        prs.save(pptx_path)
    except Exception as e:
        return {"success": False, "error": f"保存 PPTX 文件失败：{e}"}

    # ── 计算下载链接 ──
    # 使用相对于 generate_dir 的路径作为下载路径
    rel_path = os.path.relpath(pptx_path, str(config.generate_dir))
    download_url = f"/files/generate/{rel_path.replace(os.sep, '/')}"

    # ── 构建预览数据 ──
    preview_data = {
        "file": download_url,
        "filename": os.path.basename(pptx_path),
        "title": title or safe_filename,
        "slide_count": len(preview_slides),
        "theme": theme_name,
        "theme_label": theme_colors["name"],
        "slides": preview_slides,
    }

    # ── 推送预览标记 ──
    import base64
    marker_json = json.dumps(preview_data, ensure_ascii=False)
    # 用 base64 编码 JSON 内容，避免特殊字符破坏 HTML 标记格式
    encoded = base64.b64encode(marker_json.encode('utf-8')).decode('ascii')
    marker = f"<!--ppt_preview:{encoded}-->"
    push_ppt_preview(marker)

    return {
        "success": True,
        "path": pptx_path,
        "download_url": download_url,
        "filename": os.path.basename(pptx_path),
        "slide_count": len(preview_slides),
        "slides_preview": preview_slides,
        "message": f"已生成 {len(preview_slides)} 页 PPT（主题：{theme_colors['name']}）\n下载链接：{download_url}",
    }
