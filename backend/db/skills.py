# backend/db/skills.py
"""
自定义技能（Skill）的数据库读写。

Skill 分两类：
- prompt：提示词 + 工具白名单（人人可建）。启用后把 instruction 注入系统提示，
          并把 tools 白名单展开为该角色可调用的工具集合。
- code  ：可执行 Python 脚本（仅管理员可建/编辑）。启用后作为一个可被 LLM
          调用的 function（skill_<name>），执行源码中的 run(**kwargs)。
"""
import json
from typing import Any, Dict, List, Optional
from backend.database import get_db


def _row_to_skill(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "title": row["title"],
        "description": row["description"] or "",
        "skill_type": row["skill_type"] or "prompt",
        "enabled": bool(row["enabled"]),
        "instruction": row["instruction"] or "",
        "tools": _loads(row["tools"], []),
        "code": row["code"] or "",
        "parameters": _loads(row["parameters"], {}),
        "isolated": bool(row["isolated"]),
    }


def _loads(raw: Optional[str], default):
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


async def list_skills(only_enabled: bool = False) -> List[Dict[str, Any]]:
    db = await get_db()
    try:
        sql = "SELECT * FROM skills"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        return [_row_to_skill(r) for r in rows]
    finally:
        await db.close()


async def get_skill(skill_id: int) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM skills WHERE id = ?", (skill_id,))
        row = await cursor.fetchone()
        return _row_to_skill(row) if row else None
    finally:
        await db.close()


async def get_skill_by_name(name: str) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM skills WHERE name = ?", (name,))
        row = await cursor.fetchone()
        return _row_to_skill(row) if row else None
    finally:
        await db.close()


async def create_skill(data: Dict[str, Any]) -> Dict[str, Any]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO skills
               (name, title, description, skill_type, enabled, instruction, tools, code, parameters, isolated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["name"], data["title"], data.get("description", ""),
                data.get("skill_type", "prompt"), 1 if data.get("enabled", True) else 0,
                data.get("instruction", ""), json.dumps(data.get("tools", []), ensure_ascii=False),
                data.get("code", ""), json.dumps(data.get("parameters", {}), ensure_ascii=False),
                1 if data.get("isolated", False) else 0,
            ),
        )
        await db.commit()
        skill_id = cursor.lastrowid
    finally:
        await db.close()
    return await get_skill(skill_id)


async def update_skill(skill_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """UPDATE skills SET
               name = ?, title = ?, description = ?, skill_type = ?, enabled = ?,
               instruction = ?, tools = ?, code = ?, parameters = ?, isolated = ?
               WHERE id = ?""",
            (
                data["name"], data["title"], data.get("description", ""),
                data.get("skill_type", "prompt"), 1 if data.get("enabled", True) else 0,
                data.get("instruction", ""), json.dumps(data.get("tools", []), ensure_ascii=False),
                data.get("code", ""), json.dumps(data.get("parameters", {}), ensure_ascii=False),
                1 if data.get("isolated", False) else 0,
                skill_id,
            ),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        await db.close()
    return await get_skill(skill_id)


async def set_enabled(skill_id: int, enabled: bool) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE skills SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, skill_id),
        )
        await db.commit()
    finally:
        await db.close()
    return await get_skill(skill_id)


async def delete_skill(skill_id: int) -> None:
    db = await get_db()
    try:
        await db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        await db.commit()
    finally:
        await db.close()
