# main.py
from backend.bootstrap import logger
import os
import sys
import socket
import time
import argparse
import mimetypes
import asyncio

# 强制 Windows 使用 ProactorEventLoop（支持子进程）。
# Python 3.8+ 默认即为此策略，但某些第三方库/环境可能将其覆写为
# SelectorEventLoop，导致 system_run_command 无法启动子进程。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from contextlib import asynccontextmanager

import uvicorn
import httpx
import aiofiles
from starlette.responses import StreamingResponse
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.routes import register_all_routers
from backend.database import init_db
from backend.mcp_client import MCPClientManager
from backend.services.skills import SkillRegistry
from backend.services.kb_watcher import KbFileWatcher
from config_loader import config


class PrecompressedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        is_html = path.endswith(".html") or path == "" or path == "/"
        no_cache_headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        # 检查 .br 文件
        br_path = path + ".br"
        full_br = os.path.join(self.directory, br_path) if self.directory else br_path
        if os.path.isfile(full_br):
            # 异步打开文件
            file_handle = await aiofiles.open(full_br, mode="rb")
            
            async def file_iterator():
                try:
                    while chunk := await file_handle.read(64 * 1024):
                        yield chunk
                finally:
                    await file_handle.close()
            
            # 获取正确的 Content-Type（基于原始文件扩展名）
            content_type, _ = mimetypes.guess_type(path)
            headers = {
                "Content-Encoding": "br",
                "Content-Type": content_type or "application/octet-stream",
                "Vary": "Accept-Encoding",
            }

            if is_html:
                headers.update(no_cache_headers)

            # 注意：不设置 Content-Length，因为 StreamingResponse 会自动分块传输
            return StreamingResponse(
                file_iterator(),
                status_code=200,
                headers=headers,
            )
        
        # 无 .gz 文件时回退
        response = await super().get_response(path, scope)

        if is_html:
            response.headers.update(no_cache_headers)
        return response

# ============ Lifespan 管理 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时后台初始化，关闭时清理资源"""
    ready_event = asyncio.Event()
    app.state.ready_event = ready_event
    app.state.mcp_manager = None
    app.state.skill_registry = None
    app.state.kb_watcher = None
    app.state.init_success = False
    app.state.init_error = None

    async def bg_init_services():
        logger.info("🚀 后台开始异步初始化基础设施 (DB, MCP)...")
        try:
            await init_db()
            mcp_manager = MCPClientManager()
            await mcp_manager.connect_from_config(config.mcp_config_path)
            app.state.mcp_manager = mcp_manager
            # 加载自定义技能注册表（依赖 DB，已 init_db 之后）
            skill_registry = SkillRegistry()
            await skill_registry.reload()
            app.state.skill_registry = skill_registry
            # 启动知识库文件监听器（自动增量索引）
            import backend as _be
            kb_watcher = KbFileWatcher(lambda: getattr(_be, "kb_path", ""))
            await kb_watcher.start()
            app.state.kb_watcher = kb_watcher
            app.state.init_success = True
            ready_event.set()
            logger.info("✅ 后台基础设施全部初始化完毕！")
        except Exception as e:
            logger.error(f"❌ 后台初始化失败: {e}", exc_info=True)
            app.state.init_error = str(e)
            ready_event.set()

    init_task = asyncio.create_task(bg_init_services())

    # 复用 HTTP 客户端连接池
    app.state.http_client = httpx.AsyncClient(
        base_url="http://localhost",
        follow_redirects=True,
        timeout=30
    )

    yield  # FastAPI 在此处开始接收请求

    # ---- 关闭清理 ----
    logger.info("🛑 应用收到关闭信号，正在清理资源...")
    init_task.cancel()
    try:
        await app.state.http_client.aclose()
    except Exception as e:
        logger.warning(f"关闭HTTP客户端出错: {e}")
    try:
        if app.state.mcp_manager:
            await app.state.mcp_manager.close_all()
    except Exception as e:
        logger.warning(f"关闭MCP管理器出错: {e}")
    try:
        if app.state.kb_watcher:
            await app.state.kb_watcher.stop()
    except Exception as e:
        logger.warning(f"停止KB监听器出错: {e}")


# ============ FastAPI App 构建 ============
app = FastAPI(lifespan=lifespan)

# 注册API路由（必须在mount静态文件之前）
register_all_routers(app)

# WebSocket 聊天端点（双向通信，即时取消）
from backend.routes.ws_chat import ws_chat_endpoint
app.websocket("/ws/chat")(ws_chat_endpoint)

@app.get("/api/wait-ready")
async def wait_ready(request: Request):
    """检测后台基础设施初始化是否完毕（或失败）"""
    await request.app.state.ready_event.wait()
    if request.app.state.init_success:
        return {"ready": True, "status": "ok"}
    else:
        return {
            "ready": False,
            "status": "error",
            "error": getattr(request.app.state, 'init_error', '初始化失败'),
        }

@app.get("/files/generate/{file_path:path}")
async def serve_generated_file(request: Request, file_path: str):
    """
    以下载方式提供工作区内生成的文件。

    智能体生成产物（如 node compile.js 产出的 .pptx）后，会在正文中给出
    /files/generate/<相对工作区的路径> 链接；前端桌面端点击后弹出保存对话框下载。
    路径被限制在工作区 / 生成目录 / 上传目录内，防止任意文件读取。
    """
    from urllib.parse import unquote
    from pathlib import Path as _Path
    from fastapi.responses import FileResponse
    import backend as _backend
    from backend.utils.validators import validate_path

    rel = unquote(file_path)
    allowed = [
        _Path(_backend.workspace_path).resolve() if _backend.workspace_path else None,
        _Path(config.generate_dir).resolve(),
        _Path(config.uploads_dir).resolve(),
        _Path(_backend.kb_path).resolve() if getattr(_backend, "kb_path", "") else None,
    ]
    allowed = [d for d in allowed if d]

    # 依次尝试把相对路径拼到各允许根目录下，命中存在的文件即返回
    for root in allowed:
        candidate = (root / rel)
        try:
            safe = validate_path(str(candidate), allowed)
        except (ValueError, RuntimeError):
            continue
        if safe.is_file():
            return FileResponse(
                path=str(safe),
                filename=safe.name,
                media_type="application/octet-stream",  # 强制以附件形式下载
            )

    return Response(content=f"文件不存在: {rel}", status_code=404)


# ============ 静态文件挂载 ============
app.mount("/files/uploads", StaticFiles(directory=config.uploads_dir), name="uploaded_files")

if os.path.exists(config.static_dir):
    app.mount("/app", PrecompressedStaticFiles(directory=config.static_dir, html=True), name="static")


# ============ 运行模式判断 ============
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    mimetypes.add_type("application/javascript", ".js")
    SERVER_PORT = 52025
    FRONTEND_URL = f"http://127.0.0.1:{SERVER_PORT}/app/"
    DEBUG_MODE = False
else:
    SERVER_PORT = 8080
    FRONTEND_URL = "http://localhost:5173"
    DEBUG_MODE = True

def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """检测指定端口是否已经被监听"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0

def wait_for_server_ready(host: str, port: int, timeout: int = 15) -> bool:
    """轮询等待服务启动"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_port_open(host, port):
            return True
        time.sleep(0.1) # 每 100ms 检测一次
    return False


def start_fastapi(reload: bool = False):
    try:
        logger.info(f"🌐 FastAPI 启动于 0.0.0.0:{SERVER_PORT}"
                    + ("（自动重载模式）" if reload else ""))
        if reload:
            # reload 模式要求传入导入字符串，且需在主线程运行
            uvicorn.run("main:app", host="0.0.0.0", port=SERVER_PORT,
                        log_level="info", reload=True,
                        timeout_keep_alive=300)
        else:
            uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="info",
                        timeout_keep_alive=300)
    except Exception as e:
        # 捕获端口占用等异常，防止线程静默死亡
        logger.error(f"❌ FastAPI 启动失败: {e}")


def start_gui():
    import webview
    import threading
    import subprocess
    from urllib.parse import unquote

    class Api:
        """暴露给前端的Python API"""
        def select_folder(self):
            result = webview.windows[0].create_file_dialog(
                dialog_type=webview.FOLDER_DIALOG,
                allow_multiple=False
            )
            return result[0] if result else None
        def open_with_default_app(self, file_path: str):
            """使用系统默认程序打开本地文件"""
            # 解码 URL 编码（例如 %5C 转为 \，%E8%BD%AC 转为中文字符）
            decoded_path = unquote(file_path)
            # 去除可能的 file:// 前缀
            if decoded_path.startswith('file://'):
                decoded_path = decoded_path[7:]
            # 确保路径存在
            if not os.path.exists(decoded_path):
                return {"success": False, "error": f"文件不存在: {decoded_path}"}
            try:
                if sys.platform == "win32":
                    os.startfile(decoded_path)
                elif sys.platform == "darwin":
                    subprocess.run(["open", decoded_path])
                else:
                    subprocess.run(["xdg-open", decoded_path])
                return {"success": True}
            except Exception as e:
                return {"success": False, "error": str(e)}

        def download_file(self, url: str, name: str):
            """下载普通 http/https 文件，弹出保存对话框"""
            try:
                # 从 URL 中提取文件名
                filename = name or url.split('/')[-1].split('?')[0]
                if not filename:
                    filename = "downloaded_file"
                print(f"准备下载: {filename}")

                result = webview.windows[0].create_file_dialog(
                    dialog_type=webview.SAVE_DIALOG,
                    save_filename=filename,
                    file_types=('所有文件 (*.*)',)
                )

                # result 正常返回的是一个包含路径的元组，如果用户取消则返回 None
                save_path = result[0] if result else None

                if not save_path:
                    return {"success": False, "error": "用户取消了保存"}
                
                # 使用 httpx 同步客户端下载（避免阻塞主线程，但此处 API 方法本身在后台线程）
                with httpx.Client(follow_redirects=True, timeout=60.0) as client:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with open(save_path, "wb") as f:
                            for chunk in response.iter_bytes(chunk_size=8192):
                                f.write(chunk)
                return {"success": True, "path": save_path}
            
            except Exception as e:
                # 打印出具体错误，避免下次再被 except 静默吃掉报错导致无法排查
                print(f"下载文件时发生错误: {e}") 
                return {"success": False, "error": str(e)}

    # FastAPI 放在守护线程
    server_thread = threading.Thread(target=start_fastapi, daemon=True)
    server_thread.start()

    logger.info(f"⏳ 等待 FastAPI 服务就绪...")
    if not wait_for_server_ready("127.0.0.1", SERVER_PORT, timeout=15):
        # 如果 15 秒后还没启动，说明内部报错了（大概率是端口被占用）
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败", 
            f"后端服务无法启动 (端口 {SERVER_PORT} 可能被占用)。\n请检查是否有同名进程残留，然后重试。"
        )
        sys.exit(1) # 强制退出
        
    logger.info("✅ FastAPI 服务已就绪，准备打开界面...")

    # 允许在 WebView 中进行文件下载
    webview.settings['ALLOW_DOWNLOADS'] = True

    webview.create_window(
        title="My Workbench",
        url=FRONTEND_URL,
        width=1200, height=860,
        min_size=(800, 768),
        resizable=True,
        text_select=True,
        js_api=Api(),
    )
    webview.start(debug=DEBUG_MODE, http_server=True, private_mode=False, icon='favicon.ico')


# ============ 入口 ============
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 My Workbench")

    parser.add_argument("--debug", action="store_true", help="启用 DEBUG 模式")

    if DEBUG_MODE:
        parser.add_argument("--gui", action="store_true", help="启动 GUI 界面")
    else:
        parser.add_argument("--no-gui", action="store_true", help="仅启动后端服务，不启动GUI")
    args = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True
        use_gui = True
    else:
        use_gui = args.gui if DEBUG_MODE else not args.no_gui

    if use_gui:
        start_gui()
    else:
        # 纯后端模式启用自动重载，便于开发调试
        start_fastapi(reload=True)