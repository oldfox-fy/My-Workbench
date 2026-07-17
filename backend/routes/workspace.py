# backend/routes/workspace.py
import os
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from backend.database import get_db
import backend

router = APIRouter(prefix="/api", tags=["workspace"])

class WorkspaceRequest(BaseModel):
    path: str

@router.post("/workspace/set")
async def set_workspace(req: WorkspaceRequest, request: Request):
    if not os.path.isdir(req.path):
        raise HTTPException(400, "提供的路径不是一个有效目录")
    user_id = request.state.user["id"]
    db = await get_db()
    try:
        await db.execute("UPDATE users SET workspace_path = ? WHERE id = ?", (req.path, user_id))
        await db.commit()
    finally:
        await db.close()
    # 同步更新全局 + ContextVar
    backend.workspace_path = req.path
    try:
        from backend import _user_workspace_path
        _user_workspace_path.set(req.path)
    except Exception:
        pass
    return {"status": "ok", "path": req.path}

@router.get("/workspace")
async def get_workspace(request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    try:
        cursor = await db.execute("SELECT workspace_path FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        path = row[0] if row and row[0] else backend.workspace_path
        return {"path": path}
    finally:
        await db.close()