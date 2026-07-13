# backend/routes/crew.py
"""Crew 模板管理 API"""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from backend.database import get_db

router = APIRouter(prefix="/api/crew", tags=["crew"])


class AgentDef(BaseModel):
    role_name: str
    goal: str
    backstory: str = ""
    tools: List[str] = []
    max_steps: int = 3


class CrewTemplateIn(BaseModel):
    name: str
    title: str
    description: str = ""
    mode: str = "sequential"
    agents: List[AgentDef] = []


@router.get("/templates")
async def list_templates():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, title, description, mode, config, created_at FROM crew_templates ORDER BY id"
        )
        rows = await cursor.fetchall()
        return {
            "templates": [
                {
                    "id": r[0], "name": r[1], "title": r[2], "description": r[3],
                    "mode": r[4], "agents": json.loads(r[5]) if isinstance(r[5], str) else r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@router.post("/templates")
async def create_template(body: CrewTemplateIn):
    db = await get_db()
    try:
        config = json.dumps([a.dict() for a in body.agents], ensure_ascii=False)
        cursor = await db.execute(
            "INSERT INTO crew_templates (name, title, description, mode, config) VALUES (?, ?, ?, ?, ?)",
            (body.name, body.title, body.description, body.mode, config),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "status": "created"}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"模板名称 {body.name} 已存在")
        raise HTTPException(500, str(e))
    finally:
        await db.close()


@router.put("/templates/{template_id}")
async def update_template(template_id: int, body: CrewTemplateIn):
    db = await get_db()
    try:
        config = json.dumps([a.dict() for a in body.agents], ensure_ascii=False)
        cursor = await db.execute(
            """UPDATE crew_templates SET name=?, title=?, description=?, mode=?, config=?
               WHERE id=?""",
            (body.name, body.title, body.description, body.mode, config, template_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "模板不存在")
        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        await db.close()


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM crew_templates WHERE id = ?", (template_id,))
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "模板不存在")
        return {"status": "deleted"}
    finally:
        await db.close()
