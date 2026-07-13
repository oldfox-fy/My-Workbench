# backend/services/kb_graph.py
"""
知识库双链解析与图谱构建。

解析三类链接（Obsidian 兼容）：
- [[目标笔记]] / [[目标笔记|显示名]] / [[目标笔记#章节]]  —— wiki 双链
- [文本](相对路径.md)                                    —— 标准 Markdown 链接
- #标签                                                   —— 标签（可选纳入图谱）

wikilink 目标按"文件名（不含扩展名）"解析（与 Obsidian 一致），重名时优先匹配同目录，
其次取任意匹配；解析不到的目标记为"未创建笔记"（虚节点），供图谱提示。

产出：
- graph: {nodes:[{id,label,type,degree,tags}], edges:[{source,target,type}]}
- backlinks(file): 引用了该文件的其它笔记列表
所有文件访问限制在 backend.kb_path 内。
"""
import os
import re
import math
import asyncio
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import backend
from backend.bootstrap import logger

_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}
_MD_EXTS = {".md", ".markdown"}

# 可作为图谱节点的"附件"类文件（pdf / Office / 图片等）。
# 这些文件本身不含双链，但可通过相邻的附注（sidecar，<文件>.md）承载 [[]]，
# 也可被其它笔记用 [[讲义.pdf]] / [[讲义]] 直接链接到。
_ATTACHMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".xlsm",
    ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff",
}

# 链接可解析到的全部扩展名（md + 附件），用于标准 md 链接 [text](path.ext)
_LINK_EXT_GROUP = "md|markdown|pdf|docx?|pptx?|xlsx?|xlsm|csv|tsv|png|jpe?g|gif|bmp|webp|svg|ico|tiff"

# [[wikilink]] / [[wikilink|alias]] / [[wikilink#heading]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]")
# [text](path.ext) —— 捕获指向 md 或附件的相对链接（排除 http/绝对）
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.(?:" + _LINK_EXT_GROUP + r"))\)", re.IGNORECASE)
# #tag （行内标签，避免匹配标题 # ，要求前面非行首或有非#字符）
_TAG_RE = re.compile(r"(?:^|\s)#([\w一-龥/-]+)")
# 代码块 / 行内代码，解析前剔除，避免误伤
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


class KbNotConfiguredError(Exception):
    pass


def _kb_root() -> Path:
    root = getattr(backend, "kb_path", "")
    if not root:
        raise KbNotConfiguredError("知识库尚未配置，请先在「我的知识库」界面选择根目录。")
    p = Path(root)
    if not p.is_dir():
        raise KbNotConfiguredError(f"知识库目录无效：{root}")
    return p.resolve()


def _scan_md(root: Path) -> List[Path]:
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in _MD_EXTS:
                files.append(Path(dirpath) / name)
    return files


def _scan_attachments(root: Path) -> List[Path]:
    """扫描附件类文件（pdf/Office/图片等），供图谱作为节点。"""
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in _ATTACHMENT_EXTS:
                files.append(Path(dirpath) / name)
    return files


def _is_sidecar_of(md_rel: str, attach_rels: set) -> Optional[str]:
    """若某 md 是某附件的附注（<附件>.md），返回该附件的相对路径，否则 None。"""
    # 附注命名规则：<附件相对路径>.md，去掉结尾 .md 后应命中某附件
    if md_rel.lower().endswith(".md"):
        base = md_rel[:-3]
        if base in attach_rels:
            return base
    return None


def _rel(path: Path, root: Path) -> str:
    return os.path.relpath(str(path), str(root)).replace(os.sep, "/")


def _strip_code(text: str) -> str:
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return text


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ──────────────── 语义相似度 ────────────────
_FRONTMATTER_RE = re.compile(r'^---\s*\n.*?\n---\s*\n', re.DOTALL)
_SEMANTIC_THRESHOLD = 0.72
_SUMMARY_MAX_CHARS = 800


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """返回两个等长浮点向量的余弦相似度。"""
    dot = sum(ai * bi for ai, bi in zip(a, b))
    norm_a = math.sqrt(sum(ai * ai for ai in a))
    norm_b = math.sqrt(sum(bi * bi for bi in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _get_text_summary(text: str, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """提取文本摘要用于 embedding：去除 YAML frontmatter、代码块，截取前缀。"""
    text = _FRONTMATTER_RE.sub('', text, count=1)
    text = _strip_code(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


# ──────────────────────── 图谱构建 ────────────────────────

def _build_sync(root: Path, include_tags: bool, collect_texts: bool = False,
                scope_files: Optional[List[str]] = None) -> Dict[str, Any]:
    md_files = _scan_md(root)
    attach_files = _scan_attachments(root)

    # 按 scope 白名单过滤
    if scope_files is not None:
        scope_set = set(scope_files)
        md_files = [p for p in md_files if _rel(p, root) in scope_set]
        attach_files = [p for p in attach_files if _rel(p, root) in scope_set]

    md_rel_paths = [_rel(p, root) for p in md_files]
    md_rel_set = set(md_rel_paths)
    attach_rels = set(_rel(p, root) for p in attach_files)

    # 识别附注（sidecar）：<附件>.md → 其承载的附件相对路径
    # 附注不作为独立节点，其链接归并到所属附件节点上。
    sidecar_of: Dict[str, str] = {}     # md_rel -> attachment_rel
    for md_rel in md_rel_paths:
        owner = _is_sidecar_of(md_rel, attach_rels)
        if owner:
            sidecar_of[md_rel] = owner

    # 真正作为"笔记节点"的 md（排除附注）
    note_rels = [r for r in md_rel_paths if r not in sidecar_of]
    note_rel_set = set(note_rels)

    # 文件名（不含扩展名，小写）-> [rel_path]，用于 wikilink 解析
    # 同时纳入笔记与附件，使 [[讲义]] 能命中 讲义.pdf
    stem_index: Dict[str, List[str]] = {}
    for rel in note_rels:
        stem_index.setdefault(Path(rel).stem.lower(), []).append(rel)
    for rel in attach_rels:
        stem_index.setdefault(Path(rel).stem.lower(), []).append(rel)

    # 可被链接解析命中的全部真实文件（笔记 + 附件）
    resolvable_set = note_rel_set | attach_rels

    def resolve_link(target: str, from_rel: str) -> Optional[str]:
        """把 [[目标]] 解析为已存在的笔记或附件 rel_path；解析不到返回 None。"""
        target = target.strip()
        if not target:
            return None
        norm = target.replace("\\", "/").lstrip("./")
        # 1) 直接按相对路径匹配（带扩展名）
        if norm in resolvable_set:
            return norm
        # 2) 不带扩展名时，补 .md 再匹配
        if norm + ".md" in resolvable_set:
            return norm + ".md"
        # 3) 按文件名（stem）匹配笔记或附件
        stem = Path(target).stem.lower()
        matches = stem_index.get(stem, [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        # 4) 重名：优先同目录
        from_dir = os.path.dirname(from_rel)
        same_dir = [m for m in matches if os.path.dirname(m) == from_dir]
        return same_dir[0] if same_dir else matches[0]

    edges: List[Dict[str, str]] = []
    edge_seen = set()
    tags_by_file: Dict[str, set] = {}
    missing_targets: Dict[str, str] = {}  # 虚节点 id -> label
    texts_by_rel: Dict[str, str] = {}     # 笔记文本摘要（供语义关联）

    def add_edge(src: str, dst: str, etype: str):
        key = (src, dst, etype)
        if key in edge_seen or src == dst:
            return
        edge_seen.add(key)
        edges.append({"source": src, "target": dst, "type": etype})

    for path in md_files:
        rel = _rel(path, root)
        # 附注的链接归并到其所属附件上（source 用附件路径）
        source_rel = sidecar_of.get(rel, rel)
        text = _strip_code(_read_text(path))

        # 收集文本用于语义关联
        if collect_texts:
            # 只对非 sidecar 的笔记记录文本（sidecar 内容归并到附件）
            if rel not in sidecar_of:
                full = _read_text(path)
                texts_by_rel[rel] = _get_text_summary(full)

        # wiki 双链
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1)
            resolved = resolve_link(target, rel)
            if resolved:
                add_edge(source_rel, resolved, "wiki")
            else:
                vid = "missing:" + target.strip().lower()
                missing_targets[vid] = target.strip()
                add_edge(source_rel, vid, "missing")

        # 标准 md 链接（可指向笔记或附件）
        for m in _MDLINK_RE.finditer(text):
            raw = m.group(1).split("#")[0]
            if raw.startswith(("http://", "https://", "/")):
                continue
            target_abs = (path.parent / raw).resolve()
            try:
                target_rel = _rel(target_abs, root)
            except ValueError:
                continue
            if target_rel in resolvable_set:
                add_edge(source_rel, target_rel, "md")

        # 标签（附注的标签也归并到附件）
        if include_tags:
            for m in _TAG_RE.finditer(text):
                tags_by_file.setdefault(source_rel, set()).add(m.group(1))

    # ---------- 组装节点 ----------
    degree: Dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    nodes: List[Dict[str, Any]] = []
    # 笔记节点
    for rel in note_rels:
        nodes.append({
            "id": rel,
            "label": Path(rel).stem,
            "type": "note",
            "degree": degree.get(rel, 0),
            "tags": sorted(tags_by_file.get(rel, [])),
        })
    # 附件节点：scope 模式下包含全部匹配附件，否则仅纳入"已连接"的
    attach_with_sidecar = set(sidecar_of.values())
    attachment_count = 0
    for rel in attach_rels:
        if scope_files is not None or degree.get(rel, 0) > 0 or rel in attach_with_sidecar:
            nodes.append({
                "id": rel,
                "label": Path(rel).stem,
                "type": "attachment",
                "degree": degree.get(rel, 0),
                "tags": sorted(tags_by_file.get(rel, [])),
            })
            attachment_count += 1
    # 虚节点（被引用但不存在的笔记）
    for vid, label in missing_targets.items():
        nodes.append({
            "id": vid,
            "label": label,
            "type": "missing",
            "degree": degree.get(vid, 0),
            "tags": [],
        })
    # 标签节点
    if include_tags:
        tag_set = set()
        for tags in tags_by_file.values():
            tag_set |= tags
        for tag in tag_set:
            tid = "tag:" + tag
            nodes.append({"id": tid, "label": "#" + tag, "type": "tag", "degree": 0, "tags": []})
        # 文件 -> 标签边
        for rel, tags in tags_by_file.items():
            for tag in tags:
                add_edge(rel, "tag:" + tag, "tag")
        # 重算所有节点度数
        deg2: Dict[str, int] = {}
        for e in edges:
            deg2[e["source"]] = deg2.get(e["source"], 0) + 1
            deg2[e["target"]] = deg2.get(e["target"], 0) + 1
        for n in nodes:
            n["degree"] = deg2.get(n["id"], 0)

    result = {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "note_count": len(note_rels),
            "attachment_count": attachment_count,
            "edge_count": len(edges),
            "missing_count": len(missing_targets),
        },
    }
    if collect_texts:
        result["_texts"] = texts_by_rel
    return result


def _backlinks_sync(root: Path, target_rel: str) -> List[Dict[str, str]]:
    """找出所有引用了 target_rel 的笔记/附件。

    target 既可以是笔记，也可以是附件；若传入的是附件的附注路径（<附件>.md），
    自动折算到附件本身（因为附注的链接已归并到附件）。
    """
    graph = _build_sync(root, include_tags=False)
    # 若传入附注路径，折算到其附件
    if target_rel.lower().endswith(".md"):
        base = target_rel[:-3]
        # base 是否是附件
        if any(e["target"] == base or e["source"] == base for e in graph["edges"]):
            # 优先用附件本身作为反链目标
            attach_base = base
        else:
            attach_base = None
    else:
        attach_base = None
    lookup = attach_base or target_rel
    result = []
    for e in graph["edges"]:
        if e["target"] == lookup and e["type"] in ("wiki", "md", "semantic"):
            result.append({"file_path": e["source"], "type": e["type"]})
    return result


# ──────────────────────── 异步接口 ────────────────────────

async def build_graph(
    include_tags: bool = False,
    include_semantic: bool = False,
    semantic_threshold: float = _SEMANTIC_THRESHOLD,
    scope_files: Optional[List[str]] = None,
    keyword: str = "",
) -> Dict[str, Any]:
    """构建知识库双链图谱。

    - scope_files: 白名单，只包含指定文件（用于主题筛选）
    - keyword: 按文件名（含路径）模糊匹配，大小写不敏感
    - 若 scope_files 非空或 keyword 非空，图谱仅包含匹配的文件
    """
    root = _kb_root()

    # 按关键词匹配文件名
    if keyword:
        kw = keyword.strip().lower()
        matched = []
        for p in _scan_md(root):
            rel = _rel(p, root)
            if kw in rel.lower():
                matched.append(rel)
        for p in _scan_attachments(root):
            rel = _rel(p, root)
            if kw in rel.lower():
                matched.append(rel)
        scope_files = matched
        logger.info("[kb_graph] 关键词 '%s' → 文件名匹配 %d 个文件", keyword, len(matched))

    # 检查 embedding 是否已配置
    embedding_available = False
    try:
        from backend.db.kb_settings import get_embedding_config
        cfg = await get_embedding_config()
        embedding_available = bool(cfg.get("model"))
    except Exception:
        embedding_available = False

    # 同步构建结构图谱 + 收集文本
    result = await asyncio.to_thread(
        _build_sync, root, include_tags,
        collect_texts=include_semantic,
        scope_files=scope_files,
    )
    texts = result.pop("_texts", {})

    # scope/关键词模式下，附件也需要提取文本才能参与语义比较
    if include_semantic and scope_files:
        _readable = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm"}
        from backend.system_tools.reader import file_read
        for n in result["nodes"]:
            if n["type"] != "attachment":
                continue
            rel = n["id"]
            if rel in texts:
                continue
            ext = Path(rel).suffix.lower()
            if ext not in _readable:
                continue
            try:
                fr = await file_read(str(root / rel), allowed_dirs=[str(root)])
                content = (fr.get("content") or "").strip()
                if len(content) >= 80:
                    texts[rel] = content[:1500]
            except Exception:
                pass

    # 计算语义边
    if include_semantic and embedding_available and texts:
        try:
            from backend.services.embedding import get_embedder as _ge
            embedder = await _ge()
            items = list(texts.items())
            snippets = [v for _, v in items]
            vectors = await embedder.embed(snippets)
            embeddings_map = {k: vec for (k, _), vec in zip(items, vectors)}

            note_list = list(embeddings_map.keys())
            new_edges = 0
            for i in range(len(note_list)):
                for j in range(i + 1, len(note_list)):
                    a, b = note_list[i], note_list[j]
                    sim = _cosine_similarity(embeddings_map[a], embeddings_map[b])
                    if sim >= semantic_threshold:
                        result["edges"].append(
                            {"source": a, "target": b, "type": "semantic"}
                        )
                        new_edges += 1

            if new_edges > 0:
                degree: Dict[str, int] = {}
                for e in result["edges"]:
                    degree[e["source"]] = degree.get(e["source"], 0) + 1
                    degree[e["target"]] = degree.get(e["target"], 0) + 1
                for n in result["nodes"]:
                    n["degree"] = degree.get(n["id"], 0)
                result["stats"]["edge_count"] += new_edges

            logger.info(
                "[kb_graph] 语义链接：%d 项 → %d 条语义边（阈值=%.2f）",
                len(embeddings_map), new_edges, semantic_threshold,
            )
        except Exception as e:
            logger.warning("[kb_graph] 语义链接跳过：%s", e)
            embedding_available = False

    result["stats"]["embedding_available"] = embedding_available
    return result


async def get_backlinks(target_rel: str) -> List[Dict[str, str]]:
    root = _kb_root()
    return await asyncio.to_thread(_backlinks_sync, root, target_rel)


async def list_note_names() -> List[str]:
    """返回所有可被 [[ 链接的目标（笔记 + 附件）相对路径，供编辑器自动补全。

    附件的附注（<附件>.md）本身不作为补全目标（避免与附件重复）。
    """
    root = _kb_root()

    def _collect():
        attach_rels = set(_rel(p, root) for p in _scan_attachments(root))
        names: List[str] = []
        for p in _scan_md(root):
            rel = _rel(p, root)
            # 跳过附件的附注
            if rel.lower().endswith(".md") and rel[:-3] in attach_rels:
                continue
            names.append(rel)
        names.extend(sorted(attach_rels))
        return names

    return await asyncio.to_thread(_collect)
