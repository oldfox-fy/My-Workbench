# backend/routes/model.py
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from openai import AsyncOpenAI
from backend.utils.base import normalize_base_url

router = APIRouter(prefix="/api", tags=["model"])

class ModelQuery(BaseModel):
    base_url: str
    api_key: str = ""


@router.post("/model")
async def list_models(query: ModelQuery):
    try:
        base = normalize_base_url(query.base_url)
        temp_client = AsyncOpenAI(api_key=query.api_key or None, base_url=base)
        models = await temp_client.models.list()
        return [m.id for m in models.data]
    except Exception as e:
        raise HTTPException(500, str(e))