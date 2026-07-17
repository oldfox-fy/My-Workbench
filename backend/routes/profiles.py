# backend/routes/profiles.py
import json
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from backend.database import get_db

router = APIRouter(prefix="/api/profiles", tags=["profiles"])

class ProfileCreate(BaseModel):
    name: str
    tools: List[str] = []
    profile_prompt: str = ""
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=1, le=100)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    skills: List[str] = []

class ProfileResponse(BaseModel):
    id: int
    name: str
    tools: List[str]
    profile_prompt: str
    temperature: float
    top_p: float
    top_k: int
    frequency_penalty: float
    presence_penalty: float
    skills: List[str] = []

# 创建角色
@router.post("/", response_model=ProfileResponse)
async def create_profile(profile: ProfileCreate, request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    tools_json = json.dumps(profile.tools)
    skills_json = json.dumps(profile.skills)
    cursor = await db.execute(
        """INSERT INTO profiles
           (name, tools, profile_prompt, temperature, top_p, top_k, frequency_penalty, presence_penalty, skills, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (profile.name, tools_json, profile.profile_prompt,
         profile.temperature, profile.top_p, profile.top_k, profile.frequency_penalty, profile.presence_penalty,
         skills_json, user_id)
    )
    await db.commit()
    profile_id = cursor.lastrowid
    await db.close()
    return {
        "id": profile_id,
        "name": profile.name,
        "tools": profile.tools,
        "profile_prompt": profile.profile_prompt,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "top_k": profile.top_k,
        "frequency_penalty": profile.frequency_penalty,
        "presence_penalty": profile.presence_penalty,
        "skills": profile.skills
    }

# 更新角色
@router.put("/{profile_id}", response_model=ProfileResponse)
async def update_profile(profile_id: int, profile: ProfileCreate, request: Request):
    if profile_id == 0:
        raise HTTPException(status_code=400, detail="内置角色不可编辑")
    db = await get_db()
    try:
        # 检查所有权
        user_id = request.state.user["id"]
        cur = await db.execute("SELECT user_id FROM profiles WHERE id = ?", (profile_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="角色不存在")
        if row[0] is not None and row[0] != user_id:
            raise HTTPException(status_code=403, detail="无权修改他人的角色")

        tools_json = json.dumps(profile.tools)
        skills_json = json.dumps(profile.skills)
        cursor = await db.execute(
            """UPDATE profiles
               SET name = ?, tools = ?, profile_prompt = ?,
                   temperature = ?, top_p = ?, top_k = ?, frequency_penalty = ?, presence_penalty = ?, skills = ?
               WHERE id = ? AND (user_id = ? OR user_id IS NULL)""",
            (profile.name, tools_json, profile.profile_prompt,
             profile.temperature, profile.top_p, profile.top_k, profile.frequency_penalty, profile.presence_penalty,
             skills_json, profile_id, user_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=403, detail="无权修改此角色")
        await db.commit()
    finally:
        await db.close()
    return {
        "id": profile_id,
        "name": profile.name,
        "tools": profile.tools,
        "profile_prompt": profile.profile_prompt,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "top_k": profile.top_k,
        "frequency_penalty": profile.frequency_penalty,
        "presence_penalty": profile.presence_penalty,
        "skills": profile.skills
    }

# 获取所有角色
@router.get("/", response_model=List[ProfileResponse])
async def list_profiles(request: Request):
    user_id = request.state.user["id"]
    is_admin = request.state.user.get("role") == "admin"

    db = await get_db()
    if is_admin:
        # 管理员：看到所有角色 + 虚拟角色"全能助手"
        cursor = await db.execute(
            """SELECT id, name, tools, profile_prompt,
                      temperature, top_p, top_k, frequency_penalty, presence_penalty, skills
               FROM profiles"""
        )
    else:
        # 普通用户：只看到自己创建的角色
        cursor = await db.execute(
            """SELECT id, name, tools, profile_prompt,
                      temperature, top_p, top_k, frequency_penalty, presence_penalty, skills
               FROM profiles WHERE user_id = ?""",
            (user_id,),
        )
    rows = await cursor.fetchall()
    await db.close()
    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "name": row[1],
            "tools": __parse_tools(row[2]),
            "profile_prompt": row[3] or "",
            "temperature": row[4] if row[4] is not None else 1.0,
            "top_p": row[5] if row[5] is not None else 1.0,
            "top_k": row[6] if row[6] is not None else 40,
            "frequency_penalty": row[7] if row[7] is not None else 0.0,
            "presence_penalty": row[8] if row[8] is not None else 0.0,
            "skills": __parse_tools(row[9]) if len(row) > 9 else []
        })

    # 管理员插入内置虚拟角色"全能助手"（id=0，全放行）
    if is_admin:
        results.insert(0, {
            "id": 0,
            "name": "全能助手",
            "tools": [],
            "profile_prompt": "",
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 40,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "skills": [],
        })

    return results

@router.delete("/{profile_id}")
async def delete_profile(profile_id: int, request: Request):
    if profile_id == 0:
        raise HTTPException(status_code=400, detail="内置角色不可删除")
    user_id = request.state.user["id"]
    is_admin = request.state.user.get("role") == "admin"
    db = await get_db()
    if not is_admin:
        # 普通用户只能删除自己的角色
        cursor = await db.execute("SELECT user_id FROM profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        if not row:
            await db.close()
            raise HTTPException(status_code=404, detail="角色不存在")
        if row[0] is not None and row[0] != user_id:
            await db.close()
            raise HTTPException(status_code=403, detail="无权删除此角色")
    await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    await db.commit()
    await db.close()
    return {"status": "ok"}

def __parse_tools(tools_str: str) -> List[str]:
    try:
        return json.loads(tools_str)
    except:
        return []