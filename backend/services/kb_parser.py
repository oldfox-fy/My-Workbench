# backend/services/kb_parser.py
"""
统一文档解析器：支持 OCR、表格提取、图片处理、布局感知分块。

设计原则：
  - OCR / 表格提取为可选能力（依赖未安装时自动降级）
  - 所有解析结果统一为 ParsedDocument 结构
  - 分块策略保留表格、代码块、列表的结构完整性
"""

import os
import re
import hashlib
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional

from backend.bootstrap import logger

# ── 可选依赖探测 ──
_OCR_AVAILABLE = False
_OCR_ENGINE = None

try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
    _OCR_ENGINE = "tesseract"
except ImportError:
    pass

try:
    import pdfplumber as _pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False


# ── 文件类型常量 ──
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".gif"}
MD_EXTS = {".md", ".markdown"}
TEXT_EXTS = {".txt", ".rst", ".html", ".htm"}
PDF_EXT = ".pdf"


# ── 分块参数 ──
MAX_CHARS = 800        # 单块最大字符数
OVERLAP = 100          # 二次切分重叠字符数
MIN_CHARS = 30         # 过短块丢弃阈值
MAX_TABLE_CHARS = 3000 # 表格块最大字符数（可超过 MAX_CHARS）


# ── 数据结构 ──

@dataclass
class DocumentElement:
    """文档结构化元素"""
    type: str          # "heading", "paragraph", "table", "code_block", "list", "image"
    content: str
    heading_path: str = ""
    page_number: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """统一解析结果"""
    text: str = ""                     # 全文纯文本
    elements: List[DocumentElement] = field(default_factory=list)
    tables: List[Dict] = field(default_factory=list)  # [{page, markdown, bbox}]
    images: List[Dict] = field(default_factory=list)   # [{page, description, ocr_text}]
    has_text_layer: bool = True        # PDF 是否有可提取文字
    error: Optional[str] = None


class DocumentParser:
    """统一文档解析器"""

    def __init__(self, ocr_lang: str = "chi_sim+eng"):
        self.ocr_lang = ocr_lang

    # ── 公共入口 ──

    async def parse(self, abs_path: Path) -> ParsedDocument:
        """解析任意支持的文件，返回结构化文档"""
        ext = abs_path.suffix.lower()

        if ext == PDF_EXT:
            return await self._parse_pdf(abs_path)
        elif ext in IMAGE_EXTS:
            return await self._parse_image(abs_path)
        elif ext in MD_EXTS or ext in TEXT_EXTS:
            return await self._parse_text(abs_path)
        else:
            # Office 文档等 → 走统一 reader 转为文本 → 解析为元素
            return await self._parse_via_reader(abs_path)

    async def parse_text_only(self, abs_path: Path) -> Optional[str]:
        """仅返回纯文本（兼容旧接口），供 kb_indexer._read_text 使用"""
        doc = await self.parse(abs_path)
        if doc.error:
            logger.warning(f"[kb_parser] 解析失败 {abs_path}: {doc.error}")
        return doc.text if doc.text else None

    # ── PDF 解析 ──

    async def _parse_pdf(self, path: Path) -> ParsedDocument:
        doc = ParsedDocument()
        text_parts = []
        table_index = 0

        try:
            if _PDFPLUMBER_AVAILABLE:
                import pdfplumber
                with pdfplumber.open(str(path)) as pdf:
                    for page_num, page in enumerate(pdf.pages, 1):
                        # 提取文本
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(page_text)

                        # 提取表格
                        tables = page.extract_tables()
                        for tbl in tables:
                            if tbl and any(any(cell for cell in row) for row in tbl):
                                md_table = _table_to_markdown(tbl)
                                doc.tables.append({
                                    "page": page_num,
                                    "table_index": table_index,
                                    "markdown": md_table,
                                })
                                text_parts.append(f"\n[表格 {table_index + 1}，第 {page_num} 页]\n{md_table}")
                                table_index += 1

                doc.text = "\n\n".join(text_parts)
                doc.has_text_layer = len(doc.text.strip()) > 100
            else:
                # 降级到 pdfplumber 不可用时的处理
                doc = await self._parse_via_reader(path)
                doc.has_text_layer = len(doc.text.strip()) > 100
        except Exception as e:
            logger.warning(f"[kb_parser] pdfplumber 解析异常 {path}: {e}")
            doc = await self._parse_via_reader(path)
            doc.has_text_layer = len(doc.text.strip()) > 100 if doc.text else False

        # 文本量不足 → OCR 兜底
        if not doc.has_text_layer and _OCR_AVAILABLE:
            try:
                ocr_text = await self._ocr_pdf(path)
                if ocr_text:
                    doc.text = (doc.text or "") + "\n\n[OCR]\n" + ocr_text
            except Exception as e:
                logger.warning(f"[kb_parser] OCR 失败 {path}: {e}")

        # 结构化分块
        if doc.text:
            doc.elements = _parse_to_elements(doc.text)

        return doc

    async def _ocr_pdf(self, path: Path) -> str:
        """对 PDF 执行 OCR（需要 pdf2image + pytesseract）"""
        try:
            from pdf2image import convert_from_path
            images = await asyncio.to_thread(
                convert_from_path, str(path), first_page=1, last_page=50
            )
            texts = []
            for i, img in enumerate(images, 1):
                txt = await asyncio.to_thread(
                    pytesseract.image_to_string, img, lang=self.ocr_lang
                )
                if txt.strip():
                    texts.append(f"[第 {i} 页]\n{txt.strip()}")
            return "\n\n".join(texts)
        except ImportError:
            logger.warning("[kb_parser] pdf2image 未安装，无法对 PDF 执行 OCR")
            return ""
        except Exception as e:
            logger.warning(f"[kb_parser] PDF OCR 异常: {e}")
            return ""

    # ── 图片解析 ──

    async def _parse_image(self, path: Path) -> ParsedDocument:
        doc = ParsedDocument()
        parts = []

        # 基础描述
        try:
            from PIL import Image
            img = await asyncio.to_thread(Image.open, str(path))
            parts.append(f"[图片: {path.name}, 尺寸: {img.size[0]}x{img.size[1]}, 格式: {img.format}]")
        except Exception:
            parts.append(f"[图片: {path.name}]")

        # OCR
        if _OCR_AVAILABLE:
            try:
                from PIL import Image
                img = await asyncio.to_thread(Image.open, str(path))
                ocr_text = await asyncio.to_thread(
                    pytesseract.image_to_string, img, lang=self.ocr_lang
                )
                if ocr_text.strip():
                    parts.append(f"[OCR文字]\n{ocr_text.strip()}")
                    doc.images.append({"path": str(path), "ocr_text": ocr_text.strip()})
            except Exception as e:
                logger.warning(f"[kb_parser] 图片 OCR 失败 {path}: {e}")

        doc.text = "\n\n".join(parts)
        doc.elements = _parse_to_elements(doc.text)
        return doc

    # ── 纯文本/Markdown 解析 ──

    async def _parse_text(self, path: Path) -> ParsedDocument:
        doc = ParsedDocument()
        try:
            raw = path.read_bytes()
            for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
                try:
                    doc.text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if doc.text is None:
                doc.text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            doc.error = str(e)
            return doc

        doc.elements = _parse_to_elements(doc.text)
        return doc

    # ── 通用 reader 回退 ──

    async def _parse_via_reader(self, path: Path) -> ParsedDocument:
        """通过 system_tools.reader.file_read 解析（Office 文档等）"""
        doc = ParsedDocument()
        try:
            from backend.system_tools.reader import file_read
            from backend import kb_path as _kb_root
            root = str(Path(_kb_root).resolve()) if hasattr(__import__("backend"), "kb_path") else str(path.parent)
            result = await file_read(str(path), allowed_dirs=[root])
            doc.text = result.get("content", "") if isinstance(result, dict) else str(result)
        except Exception as e:
            doc.error = str(e)
            doc.text = ""

        if doc.text:
            doc.elements = _parse_to_elements(doc.text)
        return doc


# ── 结构化分块 ──

def _parse_to_elements(text: str) -> List[DocumentElement]:
    """将文本解析为结构化元素列表"""
    if not text or not text.strip():
        return []

    elements = []
    heading_stack: List[Tuple[int, str]] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
    code_fence_re = re.compile(r"^```", re.MULTILINE)
    table_row_re = re.compile(r"^\|.*\|$")

    lines = text.splitlines()
    buf: List[str] = []
    current_type = "paragraph"
    in_code_block = False

    def _heading_path() -> str:
        return " > ".join(t for _, t in heading_stack)

    def _flush():
        nonlocal current_type
        content = "\n".join(buf).strip()
        buf.clear()
        if len(content) < MIN_CHARS:
            return
        elements.append(DocumentElement(
            type=current_type,
            content=content,
            heading_path=_heading_path(),
        ))
        current_type = "paragraph"

    i = 0
    while i < len(lines):
        line = lines[i]

        # 代码围栏
        if code_fence_re.match(line):
            if in_code_block:
                # 结束代码块
                buf.append(line)
                _flush()
                current_type = "paragraph"
                in_code_block = False
                i += 1
                continue
            else:
                # 开始代码块
                _flush()
                current_type = "code_block"
                in_code_block = True
                buf.append(line)
                i += 1
                continue

        if in_code_block:
            buf.append(line)
            i += 1
            continue

        # 标题
        m = heading_re.match(line)
        if m:
            _flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            elements.append(DocumentElement(
                type="heading",
                content=title,
                heading_path=_heading_path(),
            ))
            i += 1
            continue

        # 表格行
        if table_row_re.match(line):
            if current_type != "table":
                _flush()
                current_type = "table"
            buf.append(line)
            i += 1
            continue

        # 普通行
        if current_type == "table":
            _flush()
        buf.append(line)
        i += 1

    _flush()
    return elements


def chunk_elements(elements: List[DocumentElement]) -> List[Tuple[str, str, str]]:
    """
    将结构化元素分块，返回 [(heading_path, content, element_type), ...]。
    规则：
      - 表格 (table)：保持完整，不拆分（最大 MAX_TABLE_CHARS）
      - 代码块 (code_block)：保持完整，不拆分
      - 段落 (paragraph)：按 MAX_CHARS + OVERLAP 切分
      - 列表：跟随前一个段落
    """
    chunks: List[Tuple[str, str, str]] = []
    buf: List[str] = []
    buf_type = "text"
    buf_heading = ""
    buf_len = 0

    def _emit():
        nonlocal buf_len
        content = "\n".join(buf).strip()
        buf.clear()
        buf_len = 0
        if len(content) < MIN_CHARS:
            return
        # 大块按字符切分
        if buf_type in ("text", "paragraph") and len(content) > MAX_CHARS:
            for piece in _split_long(content):
                if len(piece.strip()) >= MIN_CHARS:
                    chunks.append((buf_heading, piece.strip(), buf_type))
        else:
            chunks.append((buf_heading, content, buf_type))

    for el in elements:
        if el.type in ("heading",):
            _emit()
            buf_heading = el.heading_path + (" > " + el.content if el.heading_path else el.content)
            continue

        chunk_type = _map_type(el.type)
        el_len = len(el.content)

        # 表格/代码块：保持完整
        if el.type in ("table", "code_block"):
            _emit()
            chunks.append((el.heading_path or buf_heading, el.content, chunk_type))
            continue

        # 普通段落：累积到 buf
        if buf_type != chunk_type:
            _emit()
            buf_type = chunk_type
            buf_heading = el.heading_path or buf_heading

        if buf_len + el_len > MAX_CHARS and buf:
            _emit()
            buf_type = chunk_type
            buf_heading = el.heading_path or buf_heading

        buf.append(el.content)
        buf_len += el_len

    _emit()
    return chunks


def _map_type(el_type: str) -> str:
    mapping = {
        "table": "table",
        "code_block": "code",
        "heading": "text",
        "paragraph": "text",
        "list": "text",
        "image": "image",
    }
    return mapping.get(el_type, "text")


def _split_long(text: str) -> List[str]:
    """按字数 + 重叠窗口切分过长文本"""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    parts = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + MAX_CHARS, n)
        parts.append(text[start:end])
        if end >= n:
            break
        start = end - OVERLAP
    return parts


def _table_to_markdown(table: List[List]) -> str:
    """将 pdfplumber 表格转为 Markdown 表格"""
    if not table:
        return ""
    rows = []
    for row in table:
        cells = [str(cell) if cell else "" for cell in row]
        rows.append("| " + " | ".join(cells) + " |")

    if len(rows) >= 2:
        # 插入分隔行
        col_count = len(table[0]) if table[0] else 1
        sep = "|" + "|".join(["---"] * col_count) + "|"
        rows.insert(1, sep)

    return "\n".join(rows)


def check_ocr_available() -> Dict[str, Any]:
    """检查 OCR 可用性，返回状态信息"""
    result = {
        "tesseract_available": False,
        "pdfplumber_available": _PDFPLUMBER_AVAILABLE,
        "ocr_engine": None,
        "ocr_languages": [],
    }
    if _OCR_AVAILABLE:
        try:
            langs = pytesseract.get_languages()
            result["tesseract_available"] = True
            result["ocr_engine"] = "tesseract"
            result["ocr_languages"] = langs
        except Exception:
            pass
    return result
