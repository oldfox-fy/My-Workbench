# backend/services/skill_package.py
"""
Skill 压缩包（.zip）的解析与打包。

约定的包结构（对齐 Claude Agent Skills 习惯）：

    myskill.zip
    ├── SKILL.md        # 必需：YAML frontmatter（元数据）+ 正文（作为 instruction）
    └── skill.py        # 可选：code 型技能的 Python 源码（需含 run(**kwargs)）

SKILL.md 的 frontmatter 字段映射到数据模型（backend/db/skills.py）：
    name, title, description, skill_type(prompt|code), enabled,
    instruction, tools(list), parameters(dict), isolated(bool)

- instruction：frontmatter 显式给出时以其为准，否则用正文（frontmatter 之外的 Markdown）。
- code 型：frontmatter 未给 code 时，从 skill.py（或 frontmatter.code_file 指定的文件）读取。

解析注重防御：限制解压条目数与总大小、拒绝绝对路径 / 目录穿越（zip-slip）。
不引入新依赖：zipfile 为标准库，PyYAML 项目已在用。
"""
import io
import os
import zipfile
from typing import Any, Dict, List

import yaml

from backend.bootstrap import logger

# 解析限制（尽力而为的防滥用）
_MAX_ENTRIES = 64                     # 压缩包内最多文件数
_MAX_TOTAL_UNCOMPRESSED = 5 * 1024 * 1024   # 解压后总大小上限 5MB
_MAX_SINGLE_UNCOMPRESSED = 2 * 1024 * 1024  # 单文件解压上限 2MB

_SKILL_MD_NAMES = ("SKILL.md", "skill.md")
_DEFAULT_CODE_FILE = "skill.py"


def _safe_member(name: str) -> bool:
    """拒绝绝对路径与目录穿越，避免 zip-slip。"""
    if not name or name.endswith("/"):
        return False
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def _read_member(zf: zipfile.ZipFile, name: str) -> str:
    info = zf.getinfo(name)
    if info.file_size > _MAX_SINGLE_UNCOMPRESSED:
        raise ValueError(f"压缩包内文件 {name} 过大（超过 2MB）")
    return zf.read(name).decode("utf-8-sig")


def _find_member(names: List[str], target_basename: str) -> str:
    """在包内查找匹配的成员（支持位于子目录下）。"""
    target = target_basename.lower()
    for n in names:
        if n.replace("\\", "/").split("/")[-1].lower() == target:
            return n
    return ""


def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """
    解析形如：
        ---
        key: value
        ---
        正文...
    返回 (frontmatter dict, 正文)。无 frontmatter 时返回 ({}, 全文)。
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return {}, text.strip()
    # 去掉首个 --- 行后，找下一个仅含 --- 的分隔行
    lines = stripped.splitlines()
    # lines[0] 是 '---'
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, text.strip()
    fm_raw = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()
    try:
        meta = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md 的 frontmatter 不是合法 YAML：{e}")
    if not isinstance(meta, dict):
        raise ValueError("SKILL.md 的 frontmatter 必须是键值映射")
    return meta, body


def _as_str_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def parse_skill_zip(raw: bytes) -> Dict[str, Any]:
    """
    解析 skill 压缩包字节，返回与 SkillRequest 同构的 dict。
    校验交由上层（routes 的 _validate + require_admin）统一处理。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("上传的文件不是有效的 zip 压缩包")

    with zf:
        names = [n for n in zf.namelist() if _safe_member(n)]
        if len(zf.namelist()) > _MAX_ENTRIES:
            raise ValueError("压缩包内文件过多")
        total = sum(i.file_size for i in zf.infolist())
        if total > _MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("压缩包解压后体积过大（超过 5MB）")

        md_name = ""
        for base in _SKILL_MD_NAMES:
            md_name = _find_member(names, base)
            if md_name:
                break
        if not md_name:
            raise ValueError("压缩包内缺少 SKILL.md")

        meta, body = _split_frontmatter(_read_member(zf, md_name))

        name = str(meta.get("name", "")).strip()
        title = str(meta.get("title", "") or name).strip()
        skill_type = str(meta.get("skill_type", meta.get("type", "prompt"))).strip() or "prompt"
        instruction = meta.get("instruction")
        instruction = str(instruction) if instruction is not None else body

        code = str(meta.get("code", "") or "")
        if skill_type == "code" and not code:
            code_file = str(meta.get("code_file", "") or _DEFAULT_CODE_FILE)
            member = _find_member(names, code_file.replace("\\", "/").split("/")[-1])
            if member:
                code = _read_member(zf, member)

        parameters = meta.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ValueError("SKILL.md 的 parameters 必须是 JSON 对象")

        return {
            "name": name,
            "title": title,
            "description": str(meta.get("description", "") or ""),
            "skill_type": skill_type,
            "enabled": bool(meta.get("enabled", True)),
            "instruction": instruction or "",
            "tools": _as_str_list(meta.get("tools")),
            "code": code,
            "parameters": parameters,
            "isolated": bool(meta.get("isolated", False)),
        }


def build_skill_zip(skill: Dict[str, Any]) -> bytes:
    """把一个技能打包成与 parse_skill_zip 对应的 zip 字节（用于导出/分享）。"""
    meta: Dict[str, Any] = {
        "name": skill.get("name", ""),
        "title": skill.get("title", ""),
        "description": skill.get("description", ""),
        "skill_type": skill.get("skill_type", "prompt"),
        "enabled": bool(skill.get("enabled", True)),
        "tools": skill.get("tools", []) or [],
        "isolated": bool(skill.get("isolated", False)),
    }
    parameters = skill.get("parameters") or {}
    if parameters:
        meta["parameters"] = parameters

    is_code = skill.get("skill_type") == "code"
    if is_code:
        # 正文放说明，code 单独成文件，frontmatter 记录引用
        meta["code_file"] = _DEFAULT_CODE_FILE
        body = skill.get("description", "") or ""
    else:
        # prompt 型：instruction 作为正文
        body = skill.get("instruction", "") or ""

    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    md = f"---\n{fm}\n---\n\n{body}\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", md)
        if is_code and (skill.get("code") or ""):
            zf.writestr(_DEFAULT_CODE_FILE, skill["code"])
    logger.info(f"已打包技能 {skill.get('name')} 为压缩包。")
    return buf.getvalue()
