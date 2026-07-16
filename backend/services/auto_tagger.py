# backend/services/auto_tagger.py
"""
自动标签服务：方案 C（TF-IDF + LLM 混合精炼）。

流程：
  第一层：TF-IDF 快速预筛（零 API 成本）
    → jieba 中文分词 → 计算 TF-IDF 权重 → 取 Top-N 候选关键词
    → 过滤停用词、过短词、纯数字

  第二层：LLM 精炼（可选，提升精度）
    → 将 TF-IDF 候选词 + 文档摘要发给 LLM
    → LLM 从候选中选择 + 补充遗漏 → 输出最终标签
    → 相比纯 LLM token 消耗减半

设计约束：
  - 标签存储完全复用现有 kb_tags + kb_file_tags 表
  - 手动标签和自动标签共存（自动标签使用 color="#22c55e" 绿色标识）
  - 全量索引时自动触发；也可通过 API 手动触发
"""

import re
import math
import asyncio
from collections import Counter
from typing import Dict, List, Tuple, Optional, Any

from backend.bootstrap import logger

# ── 中文停用词表（精简版） ──
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "什么", "怎么", "如何", "为什么", "因为", "所以", "但是", "然而",
    "可以", "需要", "应该", "能够", "可能", "已经", "还是", "只是",
    "如果", "虽然", "不过", "而且", "或者", "以及", "与", "及",
    "等", "之", "其", "其中", "其他", "这个", "那个", "哪个",
    "进行", "通过", "使用", "利用", "根据", "按照", "关于", "对于",
    "由于", "为了", "除了", "之后", "之前", "以后", "以前",
    "比较", "非常", "更加", "特别", "十分", "相当", "比较",
    "一点", "一些", "很多", "许多", "大量", "少量",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "and", "or", "but", "not", "no", "if", "then", "else", "when",
    "where", "which", "who", "whom", "what", "how", "why",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after",
    "above", "below", "between", "under", "again", "further",
    "once", "here", "there", "all", "both", "each", "few",
    "more", "most", "other", "some", "such", "only", "own",
    "same", "too", "very", "just", "than",
}

# 最小/最大标签长度
_MIN_TAG_LEN = 2
_MAX_TAG_LEN = 12

# TF-IDF 候选数量（送入 LLM 精炼）
_TFIDF_CANDIDATES = 15

# 自动标签的颜色（绿色，与手动标签的默认紫色区分）
_AUTO_TAG_COLOR = "#22c55e"

# LLM 标签精炼 prompt
_LLM_REFINE_PROMPT = """你是一个文档标签专家。根据以下文档摘要和候选关键词，为文档选择 3-5 个最合适的标签。

规则：
- 每个标签 1-3 个词，简洁准确
- 优先选择候选关键词中已有的（但也补充遗漏的重要标签）
- 标签应反映文档的核心主题/领域/类型
- 避免过于通用的词（如"笔记""文档""内容"）
- 用中文返回

文档摘要（前 500 字）：
{doc_summary}

候选关键词（TF-IDF 提取）：
{candidates}

只返回 JSON（不要其他文字）：
{{"tags": ["标签1", "标签2", "标签3"]}}"""


def _load_jieba():
    """惰性加载 jieba（首次加载词典有 ~1-2s 延迟）。"""
    try:
        import jieba
        # 禁用 jieba 的日志输出
        jieba.setLogLevel(60)
        return jieba
    except ImportError:
        logger.warning("[auto_tagger] jieba 未安装，无法进行中文分词。请 pip install jieba")
        return None


def extract_content_for_tagging(file_path: str, text: str) -> str:
    """
    从文件内容中提取用于标签生成的文本。
    取前 3000 字符（足够做 TF-IDF + LLM 摘要）。
    """
    if not text or not text.strip():
        return ""
    return text[:3000]


def tfidf_extract(text: str, top_n: int = _TFIDF_CANDIDATES) -> List[Tuple[str, float]]:
    """
    方案 A：TF-IDF 关键词提取。

    Args:
        text: 输入文本
        top_n: 返回候选关键词数量

    Returns:
        [(关键词, 权重), ...] 按权重降序排列
    """
    if not text or not text.strip():
        return []

    jieba = _load_jieba()
    if jieba is None:
        # 无 jieba：回退到简单的词频统计（按空白/标点切分）
        return _fallback_frequency_extract(text, top_n)

    # 分词
    words = jieba.cut(text)

    # 过滤：去除停用词、过短/过长的词、纯数字/标点
    filtered = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w in _STOP_WORDS:
            continue
        if len(w) < _MIN_TAG_LEN or len(w) > _MAX_TAG_LEN:
            continue
        if re.match(r'^[\d.,;:!?，。；：！？、""''（）()\[\]{}【】\s\-_+=*/\\|@#$%^&]+$', w):
            continue
        if re.match(r'^[a-zA-Z]$', w):
            continue  # 单个英文字母无意义
        filtered.append(w)

    if not filtered:
        return []

    # TF 计算
    word_counts = Counter(filtered)
    total = sum(word_counts.values())

    # IDF 近似（单文档内使用对数平滑）
    tfidf_scores = {}
    for word, count in word_counts.items():
        tf = count / total
        # 单文档 IDF：用 1 / (1 + count) 近似（高频词降权）
        idf = math.log(1 + 1.0 / (1 + count))
        tfidf_scores[word] = tf * idf

    # 排序取 Top-N
    sorted_words = sorted(tfidf_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_words[:top_n]


def _fallback_frequency_extract(text: str, top_n: int) -> List[Tuple[str, float]]:
    """无 jieba 时的回退方案：按空白/标点切分 + 词频统计。"""
    # 按中文标点和空白切分
    tokens = re.split(r'[\s，。；：！？、""''（）()\[\]{}【】\-\n\r\t]+', text)
    filtered = []
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        if t in _STOP_WORDS:
            continue
        if len(t) < _MIN_TAG_LEN or len(t) > _MAX_TAG_LEN:
            continue
        if re.match(r'^[\d.,;:!?]+$', t):
            continue
        filtered.append(t)

    if not filtered:
        return []

    word_counts = Counter(filtered)
    total = sum(word_counts.values())
    scores = {w: c / total for w, c in word_counts.items()}
    sorted_words = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_words[:top_n]


# ── 方案 C：TF-IDF + LLM 混合精炼 ──

async def llm_refine_tags(
    text: str,
    tfidf_candidates: List[Tuple[str, float]],
    llm_service=None,
) -> List[str]:
    """
    第二层：LLM 精炼标签。

    将 TF-IDF 候选词 + 文档摘要发给 LLM，
    LLM 从候选中选择最合适的 + 补充遗漏的标签。

    Args:
        text: 文档原始文本
        tfidf_candidates: TF-IDF 提取的候选词 [(词, 权重), ...]
        llm_service: LLMService 实例

    Returns:
        最终标签列表
    """
    if not llm_service or not tfidf_candidates:
        # 无 LLM：直接取 TF-IDF Top-5 作为标签
        return [w for w, _ in tfidf_candidates[:5]]

    try:
        doc_summary = text[:500]
        candidates_str = "、".join([w for w, _ in tfidf_candidates])

        prompt = _LLM_REFINE_PROMPT.format(
            doc_summary=doc_summary,
            candidates=candidates_str,
        )

        import json as _json
        response = await llm_service.client.chat.completions.create(
            model=llm_service.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.3,
            stream=False,
        )

        raw = response.choices[0].message.content.strip() if response.choices else ""
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        result = _json.loads(raw)
        tags = result.get("tags", [])

        # 合并：LLM 标签优先 + TF-IDF 补充
        final_tags = list(tags)
        if len(final_tags) < 5:
            for w, _ in tfidf_candidates:
                if w not in final_tags and len(final_tags) < 5:
                    final_tags.append(w)
        return final_tags[:5]

    except Exception as e:
        logger.warning(f"[auto_tagger] LLM 精炼失败，降级到纯 TF-IDF: {e}")
        return [w for w, _ in tfidf_candidates[:5]]


async def auto_tag_file(
    file_path: str,
    text: str,
    llm_service=None,
) -> List[str]:
    """
    对单个文件执行自动标签提取。

    Args:
        file_path: 文件相对路径（用于日志）
        text: 文件文本内容
        llm_service: 可选的 LLMService（用于精炼）

    Returns:
        标签名称列表
    """
    content = extract_content_for_tagging(file_path, text)
    if not content:
        return []

    # 第一层：TF-IDF
    candidates = tfidf_extract(content)

    # 第二层：LLM 精炼（如果可用）
    if llm_service and candidates:
        tags = await llm_refine_tags(content, candidates, llm_service)
    else:
        tags = [w for w, _ in candidates[:5]]

    logger.info(f"[auto_tagger] {file_path} → {tags}（候选: {[w for w, _ in candidates[:5]]}）")
    return tags


async def auto_tag_and_persist(
    file_path: str,
    text: str,
    llm_service=None,
) -> int:
    """
    自动提取标签并持久化到数据库。

    返回写入的标签数量。
    """
    # 诊断：记录输入文本长度
    content_len = len(text) if text else 0
    logger.info(f"[auto_tagger] {file_path} 开始分析（文本长度 {content_len}）")

    tags = await auto_tag_file(file_path, text, llm_service)
    if not tags:
        logger.info(f"[auto_tagger] {file_path} 未提取到标签（文本过短或无可分词内容）")
        return 0

    from backend.database import get_db

    db = await get_db()
    try:
        written = 0
        for tag_name in tags:
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            await db.execute(
                "INSERT OR IGNORE INTO kb_tags (name, color) VALUES (?, ?)",
                (tag_name, _AUTO_TAG_COLOR),
            )
            cursor = await db.execute("SELECT id FROM kb_tags WHERE name = ?", (tag_name,))
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "INSERT OR IGNORE INTO kb_file_tags (file_path, tag_id) VALUES (?, ?)",
                    (file_path, row[0]),
                )
                written += 1
        await db.commit()
        if written:
            logger.info(f"[auto_tagger] {file_path} 已打标 {written} 个: {tags}")
        else:
            logger.warning(f"[auto_tagger] {file_path} DB写入异常：{len(tags)} 个标签，0 个成功写入")
    except Exception as e:
        logger.error(f"[auto_tagger] 持久化失败 {file_path}: {e}")
        written = 0
    finally:
        await db.close()

    return written


async def _read_file_for_tagging(abs_path: str) -> str:
    """
    直接读取文件文本（绕过 file_read 的路径白名单校验）。
    知识库内的文件已经过 _scan_files 安全扫描，无需额外路径校验。
    仅读取前 8KB，足够标签提取使用。
    """
    import os as _os
    ext = _os.path.splitext(abs_path)[1].lower()
    # 文本类扩展名：直接读
    _text_exts = {".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".csv", ".tsv", ".json", ".yaml", ".yml"}
    if ext in _text_exts:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(8192)
        except Exception:
            # 尝试 GBK 编码
            try:
                with open(abs_path, "r", encoding="gbk", errors="replace") as f:
                    return f.read(8192)
            except Exception:
                return ""
    # 非文本文件：用 kb_parser 解析
    try:
        from backend.services.kb_parser import DocumentParser
        parser = DocumentParser()
        text = await parser.parse_text_only(abs_path)
        return (text or "")[:8192]
    except Exception:
        return ""


async def auto_tag_all_files(
    llm_service=None,
    progress_callback=None,
) -> Dict[str, Any]:
    """
    对知识库中所有文件执行自动标签（后台异步任务）。

    Args:
        llm_service: 可选的 LLMService
        progress_callback: 可选的回调函数 (current, total, file_path) → None

    Returns:
        统计信息 dict
    """
    import os
    from pathlib import Path
    import backend
    from backend.services.kb_indexer import _INDEX_EXTS, _IGNORE

    kb_root = getattr(backend, "kb_path", "")
    if not kb_root:
        return {"success": False, "error": "知识库尚未配置"}

    root = Path(kb_root).resolve()
    if not root.is_dir():
        return {"success": False, "error": f"知识库目录无效：{kb_root}"}

    # 扫描所有可索引文件
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in _INDEX_EXTS:
                rel = os.path.relpath(str(p), str(root)).replace(os.sep, "/")
                files.append((rel, str(p)))

    total = len(files)
    tagged = 0
    skipped = 0
    skipped_no_content = 0  # 文件无内容/读不到
    skipped_no_tags = 0     # 有内容但分词无结果
    skipped_error = 0       # 异常跳过
    sample_no_tags: list = []  # 记录前几个无标签文件用于诊断

    for i, (rel_path, abs_path) in enumerate(files):
        try:
            # 直接读文件（KB 目录内的文件，无需 file_read 的路径白名单校验）
            text = await _read_file_for_tagging(abs_path)
            if not text or len(text) < 10:
                skipped += 1
                skipped_no_content += 1
                if len(sample_no_tags) < 5:
                    sample_no_tags.append(f"{rel_path}（无内容或过短）")
                continue

            written = await auto_tag_and_persist(rel_path, text, llm_service)
            if written > 0:
                tagged += 1
            else:
                skipped += 1
                skipped_no_tags += 1
                if len(sample_no_tags) < 5:
                    sample_no_tags.append(f"{rel_path}（{len(text)}字）")

        except Exception as e:
            logger.warning(f"[auto_tagger] 跳过 {rel_path}: {e}")
            skipped += 1
            skipped_error += 1

        if progress_callback:
            progress_callback(i + 1, total, rel_path)

    logger.info(
        f"[auto_tagger] 全量完成: 扫描{total} 打标{tagged} 跳过{skipped} "
        f"（无内容{skipped_no_content} 无分词{skipped_no_tags} 异常{skipped_error}）"
    )
    if sample_no_tags:
        logger.info(f"[auto_tagger] 无标签样本: {sample_no_tags}")

    return {
        "success": True,
        "total_files": total,
        "tagged": tagged,
        "skipped": skipped,
        "skipped_no_content": skipped_no_content,
        "skipped_no_tags": skipped_no_tags,
        "skipped_error": skipped_error,
        "sample_no_tags": sample_no_tags[:5],
    }
