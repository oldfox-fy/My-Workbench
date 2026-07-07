# backend/services/kb_graph.py
"""
知识库双链解析与图谱构建。

解析三类链接（Obsidian 兼容）：
- [[目标笔记]] / [[目标笔记|显示名]] / [[目标笔记#章节]]  —— wiki 双链
- [文本](相对路径.md)                                    —— 标准 Markdown 链接
- #标签                                                   —— 标签（可选纳入图谱）

wikilink 目标按“文件名（不含扩展名）”解析（与 Obsidian 一致），重名时优先匹配同目录，
其次取任意匹配；解析不到的目标记为“未创建笔记”（虚节点），供图谱提示。

产出：
- graph: {nodes:[{id,label,type,degree,tags}], edges:[{source,target,type}]}
- backlinks(file): 引用了该文件的其它笔记列表
所有文件访问限制在 backend.kb_path 内。
"""
import os
import re
import asyncio
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import backend
from backend.bootstrap import logger

_IGNORE = {".git", "node_modules", "__pycache__", ".DS_Store", ".obsidian"}
_MD_EXTS = {".md", ".markdown"}

# [[wikilink]] / [[wikilink|alias]] / [[wikilink#heading]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]")
# [text](path.md) —— 仅捕获指向 .md 的相对链接（排除 http/绝对）
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")
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


def _build_sync(root: Path, include_tags: bool) -> Dict[str, Any]:
    md_files = _scan_md(root)

    # rel_path -> 文件信息
    rel_paths = [_rel(p, root) for p in md_files]
    rel_set = set(rel_paths)

    # 文件名（不含扩展名，小写）-> [rel_path]，用于 wikilink 解析
    stem_index: Dict[str, List[str]] = {}
    for rel in rel_paths:
        stem = Path(rel).stem.lower()
        stem_index.setdefault(stem, []).append(rel)

    def resolve_wikilink(target: str, from_rel: str) -> Optional[str]:
        """把 [[目标]] 解析为已存在笔记的 rel_path；解析不到返回 None。"""
        target = target.strip()
        if not target:
            return None
        # 允许带路径的 wikilink，如 [[folder/note]]
        cand = target
        if not cand.lower().endswith((".md", ".markdown")):
            cand_md = cand + ".md"
        else:
            cand_md = cand
        # 1) 直接按相对路径匹配
        norm = cand_md.replace("\\", "/").lstrip("./")
        if norm in rel_set:
            return norm
        # 2) 按文件名（stem）匹配
        stem = Path(target).stem.lower()
        matches = stem_index.get(stem, [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        # 3) 重名：优先同目录
        from_dir = os.path.dirname(from_rel)
        same_dir = [m for m in matches if os.path.dirname(m) == from_dir]
        return same_dir[0] if same_dir else matches[0]

    edges: List[Dict[str, str]] = []
    edge_seen = set()
    tags_by_file: Dict[str, set] = {}
    missing_targets: Dict[str, str] = {}  # 虚节点 id -> label

    def add_edge(src: str, dst: str, etype: str):
        key = (src, dst, etype)
        if key in edge_seen or src == dst:
            return
        edge_seen.add(key)
        edges.append({"source": src, "target": dst, "type": etype})

    for path in md_files:
        rel = _rel(path, root)
        text = _strip_code(_read_text(path))

        # wiki 双链
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1)
            resolved = resolve_wikilink(target, rel)
            if resolved:
                add_edge(rel, resolved, "wiki")
            else:
                vid = "missing:" + target.strip().lower()
                missing_targets[vid] = target.strip()
                add_edge(rel, vid, "missing")

        # 标准 md 链接
        for m in _MDLINK_RE.finditer(text):
            raw = m.group(1).split("#")[0]
            if raw.startswith(("http://", "https://", "/")):
                continue
            # 相对 from 文件目录解析
            target_abs = (path.parent / raw).resolve()
            try:
                target_rel = _rel(target_abs, root)
            except ValueError:
                continue
            if target_rel in rel_set:
                add_edge(rel, target_rel, "md")

        # 标签
        if include_tags:
            for m in _TAG_RE.finditer(text):
                tag = m.group(1)
                tags_by_file.setdefault(rel, set()).add(tag)

    # ---------- 组装节点 ----------
    degree: Dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    nodes: List[Dict[str, Any]] = []
    for rel in rel_paths:
        nodes.append({
            "id": rel,
            "label": Path(rel).stem,
            "type": "note",
            "degree": degree.get(rel, 0),
            "tags": sorted(tags_by_file.get(rel, [])),
        })
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
        # 重算标签节点度数
        deg2: Dict[str, int] = {}
        for e in edges:
            deg2[e["source"]] = deg2.get(e["source"], 0) + 1
            deg2[e["target"]] = deg2.get(e["target"], 0) + 1
        for n in nodes:
            n["degree"] = deg2.get(n["id"], 0)

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "note_count": len(rel_paths),
            "edge_count": len(edges),
            "missing_count": len(missing_targets),
        },
    }


def _backlinks_sync(root: Path, target_rel: str) -> List[Dict[str, str]]:
    """找出所有引用了 target_rel 的笔记。"""
    graph = _build_sync(root, include_tags=False)
    result = []
    for e in graph["edges"]:
        if e["target"] == target_rel and e["type"] in ("wiki", "md"):
            result.append({"file_path": e["source"], "type": e["type"]})
    return result


# ──────────────────────── 异步接口 ────────────────────────

async def build_graph(include_tags: bool = False) -> Dict[str, Any]:
    root = _kb_root()
    return await asyncio.to_thread(_build_sync, root, include_tags)


async def get_backlinks(target_rel: str) -> List[Dict[str, str]]:
    root = _kb_root()
    return await asyncio.to_thread(_backlinks_sync, root, target_rel)


async def list_note_names() -> List[str]:
    """返回所有笔记的相对路径（供编辑器 [[ 自动补全）。"""
    root = _kb_root()
    return await asyncio.to_thread(lambda: [_rel(p, root) for p in _scan_md(root)])
