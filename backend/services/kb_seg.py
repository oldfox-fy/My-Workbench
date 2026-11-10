# backend/services/kb_seg.py
"""
jieba 中文分词辅助：知识库 FTS5 关键词索引与查询。

为什么需要：SQLite FTS5 内置 unicode61 tokenizer 会把「连续的一段中文」当作
一个 token，导致中文关键词检索几乎只能做整句精确匹配（「知识库」匹配不到
「这是一个知识库检索系统的测试文档」）。解决思路是入库前用 jieba 分词、
以空格连接后写入 FTS，查询时同样分词，从而获得词级匹配与更合理的 BM25 排序。
"""
import threading
from typing import List

_jieba = None
_jieba_lock = threading.Lock()

# 查询时的停用词：避免「的/是/了」等虚词作为 AND 必需项，导致过度收紧
_QUERY_STOP = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没", "没有", "看",
    "好", "自己", "这", "他", "她", "它", "们", "那", "些", "什么", "怎么", "如何",
    "为什么", "因为", "所以", "但是", "然而", "可以", "需要", "应该", "能够", "可能",
    "已经", "还是", "只是", "如果", "虽然", "不过", "而且", "或者", "以及", "与", "及",
    "等", "之", "其", "其中", "其他", "这个", "那个", "哪个", "进行", "通过", "使用",
    "利用", "根据", "按照", "关于", "对于", "由于", "为了", "除了", "之后", "之前",
    "以后", "以前", "比较", "非常", "更加", "特别", "十分", "相当", "一点", "一些",
    "很多", "许多", "大量", "少量",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "this", "that", "these", "those",
    "it", "its", "they", "them", "and", "or", "but", "not", "no", "if", "then",
    "else", "when", "where", "which", "who", "whom", "what", "how", "why",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as", "into",
    "through", "during", "before", "after", "above", "below", "between", "under",
    "again", "further", "once", "here", "there", "all", "both", "each", "few",
    "more", "most", "other", "some", "such", "only", "own", "same", "too",
    "very", "just", "than",
}


def _get_jieba():
    global _jieba
    if _jieba is None:
        with _jieba_lock:
            if _jieba is None:
                try:
                    import jieba
                    jieba.setLogLevel(60)
                    _jieba = jieba
                except ImportError:
                    _jieba = False
    return _jieba or None


def seg_text(text: str) -> str:
    """分词并以空格连接（用于写入 FTS 的 content_seg 列）。jieba 不可用则原样返回。"""
    if not text:
        return ""
    jieba = _get_jieba()
    if jieba is None:
        return text
    return " ".join(w.strip() for w in jieba.cut(text) if w.strip())


def seg_query_terms(query: str) -> List[str]:
    """查询分词并过滤停用词。jieba 不可用时按空白切分。"""
    if not query:
        return []
    jieba = _get_jieba()
    if jieba is None:
        return [t for t in query.split() if t]
    terms = []
    for w in jieba.cut(query):
        w = w.strip()
        if not w or w in _QUERY_STOP:
            continue
        terms.append(w)
    return terms


def _quote(term: str) -> str:
    term = term.replace('"', "").strip()
    return f'"{term}"' if term else ""


def build_match_query(query: str, mode: str = "and") -> str:
    """构造 FTS5 MATCH 查询串。mode: 'and'（全部词项，默认）或 'or'。"""
    terms = seg_query_terms(query)
    if not terms:
        q = query.replace('"', "").strip()
        return f'"{q}"' if q else ""
    quoted = [q for q in (_quote(t) for t in terms) if q]
    if not quoted:
        return ""
    if mode == "or" and len(quoted) > 1:
        return " OR ".join(quoted)
    return " AND ".join(quoted)
