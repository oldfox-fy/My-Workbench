# backend/routes/models.py
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from backend.database import get_db

router = APIRouter(prefix="/api", tags=["models"])

# 模型角色：default(通用) / vision(视觉) / reasoning(推理) / audio(语音) / fast(快速) / image_gen(生图)
MODEL_ROLES = ["default", "vision", "reasoning", "audio", "fast", "image_gen"]


class ModelConfigBase(BaseModel):
    name: str
    type: str  # 'local' or 'online'
    modelName: Optional[str] = None
    baseUrl: str
    apiKey: str
    role: str = "default"  # 模型角色，用于自动切换


class ModelConfigResponse(ModelConfigBase):
    id: str


class UpdateModelRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    modelName: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    role: Optional[str] = None


@router.get("/models", response_model=List[ModelConfigResponse])
async def list_models():
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, name, type, modelName, baseUrl, apiKey, role FROM models ORDER BY name"
    )
    rows = await cursor.fetchall()
    await db.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "modelName": row[3],
            "baseUrl": row[4],
            "apiKey": row[5],
            "role": row[6] if len(row) > 6 else "default",
        }
        for row in rows
    ]


@router.post("/models", response_model=ModelConfigResponse)
async def create_model(data: ModelConfigBase):
    if data.role not in MODEL_ROLES:
        raise HTTPException(status_code=400, detail=f"无效的角色类型: {data.role}，可选值: {MODEL_ROLES}")
    model_id = str(uuid.uuid4())
    db = await get_db()
    await db.execute(
        "INSERT INTO models (id, name, type, modelName, baseUrl, apiKey, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (model_id, data.name, data.type, data.modelName or "", data.baseUrl, data.apiKey, data.role)
    )
    await db.commit()
    await db.close()
    return {**data.dict(), "id": model_id}


@router.put("/models/{model_id}")
async def update_model(model_id: str, data: UpdateModelRequest):
    if data.role is not None and data.role not in MODEL_ROLES:
        raise HTTPException(status_code=400, detail=f"无效的角色类型: {data.role}，可选值: {MODEL_ROLES}")
    db = await get_db()
    # 构建动态更新语句
    updates = []
    params = []
    if data.name is not None:
        updates.append("name = ?")
        params.append(data.name)
    if data.type is not None:
        updates.append("type = ?")
        params.append(data.type)
    if data.modelName is not None:
        updates.append("modelName = ?")
        params.append(data.modelName)
    if data.baseUrl is not None:
        updates.append("baseUrl = ?")
        params.append(data.baseUrl)
    if data.apiKey is not None:
        updates.append("apiKey = ?")
        params.append(data.apiKey)
    if data.role is not None:
        updates.append("role = ?")
        params.append(data.role)
    if not updates:
        return {"status": "ok"}
    params.append(model_id)
    query = f"UPDATE models SET {', '.join(updates)} WHERE id = ?"
    await db.execute(query, params)
    if db.total_changes == 0:
        await db.close()
        raise HTTPException(status_code=404, detail="Model not found")
    await db.commit()
    await db.close()
    return {"status": "ok"}


@router.delete("/models/{model_id}")
async def delete_model(model_id: str):
    db = await get_db()
    await db.execute("DELETE FROM models WHERE id = ?", (model_id,))
    await db.commit()
    await db.close()
    return {"status": "ok"}


@router.get("/models/roles")
async def get_model_roles():
    """返回可用的模型角色列表及其说明。"""
    return {
        "roles": [
            {"value": "default", "label": "默认", "desc": "通用对话模型，未匹配到特殊角色时使用"},
            {"value": "vision", "label": "视觉", "desc": "支持图片/多模态理解（如 gpt-4o, claude-sonnet）"},
            {"value": "reasoning", "label": "推理", "desc": "深度推理模型（如 deepseek-r1, o1, o3）"},
            {"value": "audio", "label": "语音", "desc": "支持语音输入/输出（如 gpt-4o-audio-preview）"},
            {"value": "fast", "label": "快速", "desc": "轻量快速模型，用于简单对话（如 gpt-4o-mini）"},
            {"value": "image_gen", "label": "生图", "desc": "图像生成模型（如 DALL-E 3, stable-diffusion）"},
        ]
    }
