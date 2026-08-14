# backend/routes/__init__.py
# 需要手动导入所有模块，解决打包路由失效的问题
from fastapi import FastAPI


def register_all_routers(app: FastAPI):
    # ── 认证路由（无需登录）──
    from . import auth
    app.include_router(auth.router)

    # ── 受保护的路由（需登录）──
    # 认证由 user_context_middleware（main.py）统一处理，
    # 路由处理函数通过 request.state.user 获取当前用户。
    from . import chat, chats, files, model, models, profiles, workspace, toolcalls, mcp, knowledge, kb_rag, skills, voice, crew, user_prefs, memory
    modules = [chat, chats, files, model, models, profiles, workspace, toolcalls, mcp, knowledge, kb_rag, skills, voice, crew, user_prefs, memory]
    for mod in modules:
        router = getattr(mod, "router", None)
        if router is not None:
            app.include_router(router)
