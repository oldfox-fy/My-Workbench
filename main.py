# main.py
# 禁用 beartype 的运行时类型检查以避免 Python 3.11 兼容性问题
# （beartype 0.22.x 在处理 key_value / fastmcp 的类型注解时会无限递归）
import os
os.environ.setdefault("BEARTYPE_DISABLE", "1")

from backend.bootstrap import logger
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
    app.state.reminder_scheduler = None
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
            # 从数据库恢复管理员（单用户部署，id=1）的 KB 路径到全局变量，
            # 供后台监听器与索引任务使用（后台任务无请求上下文，读不到 ContextVar）。
            try:
                from backend.database import get_db
                db = await get_db()
                try:
                    cur = await db.execute("SELECT kb_path FROM users WHERE id = 1")
                    row = await cur.fetchone()
                    if row and row[0]:
                        _be.kb_path = row[0]
                        _be._user_kb_path.set(row[0])
                finally:
                    await db.close()
            except Exception as _e:
                logger.warning(f"恢复 KB 路径失败：{_e}")
            kb_watcher = KbFileWatcher(
                lambda: getattr(_be, "kb_path", ""),
                lambda: 1,
            )
            await kb_watcher.start()
            app.state.kb_watcher = kb_watcher
            # 启动语音助手（P0 按键触发；P1 再上唤醒词监听）
            try:
                # 先应用持久化的语音设置 overlay（P4：语音设置面板），再启动助手
                from backend.voice.settings import apply_voice_settings
                apply_voice_settings()
                from backend.voice.assistant import get_assistant
                voice_assistant = get_assistant()
                voice_assistant.bind(mcp_manager=mcp_manager, skill_registry=skill_registry)
                await voice_assistant.start()
                app.state.voice_assistant = voice_assistant
            except Exception as _ve:
                logger.warning(f"语音助手启动失败（不影响主流程）：{_ve}")
            # 启动定时提醒调度器（P2）
            try:
                if config.reminders_enabled:
                    from backend.voice.reminders import get_store, ReminderScheduler
                    reminder_scheduler = ReminderScheduler(
                        get_store(), interval=config.reminders_check_interval
                    )
                    await reminder_scheduler.start()
                    app.state.reminder_scheduler = reminder_scheduler
            except Exception as _re:
                logger.warning(f"提醒调度器启动失败（不影响主流程）：{_re}")
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
    try:
        if getattr(app.state, "voice_assistant", None):
            await app.state.voice_assistant.stop()
    except Exception as e:
        logger.warning(f"停止语音助手出错: {e}")
    try:
        if getattr(app.state, "reminder_scheduler", None):
            await app.state.reminder_scheduler.stop()
    except Exception as e:
        logger.warning(f"停止提醒调度器出错: {e}")


# ============ FastAPI App 构建 ============
app = FastAPI(lifespan=lifespan)

# ── 用户上下文中间件：认证 + KB 路径注入 ──
@app.middleware("http")
async def user_context_middleware(request: Request, call_next):
    """对 /api/* 路由（除 auth 外）进行 token 校验，并注入用户上下文变量"""
    path = request.url.path

    # 公开路由（无需登录）
    public_prefixes = ("/api/auth/", "/api/wait-ready", "/files/", "/app/", "/ws/")
    is_public = any(path.startswith(p) for p in public_prefixes)

    if path.startswith("/api/") and not is_public:
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        if not token:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "请先登录"})

        try:
            from backend.auth import validate_token
            user = await validate_token(token)
        except Exception:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=500, content={"detail": "认证服务异常"})

        if not user:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "登录已过期，请重新登录"})

        if user.get("status") == "disabled":
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "账号已被禁用"})

        request.state.user = user
        request.state.token = token

        # 注入用户 KB / Workspace 路径到 context var
        try:
            from backend import _user_kb_path, _user_workspace_path
            _user_kb_path.set(user.get("kb_path", ""))
            _user_workspace_path.set(user.get("workspace_path", "") or backend.workspace_path)
        except Exception:
            pass

    response = await call_next(request)
    return response

# 注册API路由（必须在mount静态文件之前）
register_all_routers(app)

# WebSocket 聊天端点（双向通信，即时取消）
from backend.routes.ws_chat import ws_chat_endpoint
app.websocket("/ws/chat")(ws_chat_endpoint)

# WebSocket 语音助手状态推送端点（P0 预留，P1 唤醒后前端订阅）
from backend.routes.assistant import ws_assistant
app.websocket("/ws/assistant")(ws_assistant)

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
    FRONTEND_URL = "http://localhost:5175"
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


def _build_orb_html(ws_url: str) -> str:
    """构建悬浮球窗口的内联 HTML（无外部资源，直接连 /ws/assistant 状态通道）。"""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: transparent; overflow: hidden; }
  #wrap { width: 100vw; height: 100vh; display: flex; flex-direction: column;
          align-items: center; justify-content: center; gap: 3px; }
  #orb { width: 62px; height: 62px; border-radius: 50%;
    background: radial-gradient(circle at 30% 30%, rgba(255,255,255,.55), rgba(0,0,0,0) 60%), #22c55e;
    box-shadow: 0 0 16px 5px #22c55e;
    display: flex; align-items: center; justify-content: center;
    transition: background .3s ease, box-shadow .3s ease;
    user-select: none; cursor: pointer; }
  #mic { font-size: 28px; line-height: 1; }
  #label { font: 10px/1.2 "Segoe UI", "Microsoft YaHei", sans-serif;
           color: #fff; text-shadow: 0 0 3px #000; white-space: nowrap; }
</style>
</head>
<body>
  <div id="wrap">
    <div id="orb"><span id="mic">🎤</span></div>
    <div id="label">待命</div>
  </div>
<script>
  var COLORS = { idle:'#22c55e', listening:'#3b82f6', thinking:'#f59e0b',
                 acting:'#a855f7', speaking:'#06b6d4', error:'#ef4444' };
  var LABELS = { idle:'待命', listening:'聆听', thinking:'思考',
                 acting:'执行', speaking:'播报', error:'出错' };
  var orb = document.getElementById('orb');
  var label = document.getElementById('label');
  function setState(s) {
    var c = COLORS[s] || COLORS.idle;
    orb.style.background = 'radial-gradient(circle at 30% 30%, rgba(255,255,255,.55), rgba(0,0,0,0) 60%), ' + c;
    orb.style.boxShadow = '0 0 16px 5px ' + c;
    label.textContent = LABELS[s] || s;
  }
  // 区分「点击」与「拖动」：按下→抬起位移很小时视为点击，唤起主窗口
  var downX = 0, downY = 0, moved = false;
  document.addEventListener('mousedown', function(e){ downX = e.screenX; downY = e.screenY; moved = false; });
  document.addEventListener('mousemove', function(e){
    if (Math.abs(e.screenX - downX) + Math.abs(e.screenY - downY) > 6) moved = true; });
  orb.addEventListener('mouseup', function(){
    if (moved) return;
    try { window.pywebview.api.show_main(); } catch (_) {} });
  // 状态订阅（服务端广播，主窗口/悬浮球共享）
  var ws = null, retry = null;
  function schedule(){ if (retry) return; retry = setTimeout(function(){ retry = null; connect(); }, 3000); }
  function connect(){
    try { ws = new WebSocket('__WS_URL__'); } catch (e) { schedule(); return; }
    ws.onmessage = function(ev){
      var m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m && m.type === 'state' && m.state) setState(m.state); };
    ws.onclose = function(){ schedule(); };
    ws.onerror = function(){ try { ws.close(); } catch (_) {} };
  }
  connect();
</script>
</body>
</html>""".replace("__WS_URL__", ws_url)


def _make_tray_image():
    """用 PIL 生成托盘图标（蓝紫圆底 + 白色麦克风，避免依赖外部图标文件）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(99, 102, 241, 255))                       # 强调色底
    d.rounded_rectangle((26, 14, 38, 40), radius=6, fill=(255, 255, 255, 255))  # 麦克风头
    d.line((32, 40, 32, 46), fill=(255, 255, 255, 255), width=4)                # 麦克风杆
    d.line((24, 46, 40, 46), fill=(255, 255, 255, 255), width=4)                # 底座
    return img


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

    # 查找 favicon.ico（必须在项目根目录，且是真正的 ICO 格式）
    # 注意：frontend/public/favicon.ico 实际是 PNG 格式，不能用于 WinForms Icon
    icon_path = None
    root_ico = 'favicon.ico'
    if os.path.exists(root_ico):
        icon_path = root_ico

    # 使用私有的 WebView2 用户数据目录，避免与其他 Edge/WebView2 进程冲突
    # （private_mode=True 会在临时目录创建独立的数据目录，不会锁住共享目录）
    import tempfile as _tempfile
    _storage_dir = os.path.join(_tempfile.gettempdir(), 'MyWorkbench_WebView2')
    os.makedirs(_storage_dir, exist_ok=True)

    # ── P4：托盘 + 悬浮球 + 关闭即后台常驻 ──
    silent = "--silent" in sys.argv     # 开机自启动时隐藏主窗口，仅托盘/悬浮球常驻
    state = {"quit": False}             # 退出标志（托盘「退出」置位后，点 X 才真正退出）
    main_window = None
    orb_window = None
    tray_icon = None

    def do_quit():
        """真正退出：先销毁所有窗口让 webview.start() 返回，再停掉托盘。"""
        state["quit"] = True
        for w in (orb_window, main_window):
            if w is not None:
                try:
                    w.destroy()   # pywebview 内部会 Invoke 到 GUI 线程，线程安全
                except Exception:
                    pass
        try:
            if tray_icon is not None:
                tray_icon.stop()
        except Exception:
            pass

    def on_main_closing():
        """主窗口点 X：不退出，只隐藏到托盘（返回 False 取消关闭）。"""
        if state["quit"]:
            return True
        try:
            main_window.hide()
        except Exception:
            pass
        return False

    def on_orb_closing():
        """悬浮球被关闭（如 Alt+F4）：非退出场景下隐藏而非销毁。"""
        if state["quit"]:
            return True
        try:
            orb_window.hide()
        except Exception:
            pass
        return False

    class OrbApi:
        """悬浮球暴露给前端 JS 的接口：点击唤起主窗口。"""
        def show_main(self):
            if main_window is not None:
                try:
                    main_window.show()
                except Exception:
                    pass

    def _show_main():
        if main_window is not None:
            try:
                main_window.show()
            except Exception:
                pass

    def _show_orb():
        if orb_window is not None:
            try:
                orb_window.show()
            except Exception:
                pass

    def _start_tray():
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda icon, item: _show_main(), default=True),
            pystray.MenuItem("显示悬浮球", lambda icon, item: _show_orb()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, item: do_quit()),
        )
        return pystray.Icon("MyWorkbench", _make_tray_image(), "My Workbench", menu)

    # 主窗口（silent 模式启动即隐藏，开机常驻）
    main_window = webview.create_window(
        title="My Workbench",
        url=FRONTEND_URL,
        width=1200, height=860,
        min_size=(800, 768),
        resizable=True,
        text_select=True,
        js_api=Api(),
        hidden=silent,
    )
    main_window.events.closing += on_main_closing

    # 悬浮球（无边框、透明、置顶、可拖动；点击唤起主窗口）
    orb_window = webview.create_window(
        title="",
        html=_build_orb_html(f"ws://127.0.0.1:{SERVER_PORT}/ws/assistant"),
        width=96, height=100,
        frameless=True,
        transparent=True,
        on_top=True,
        easy_drag=True,
        js_api=OrbApi(),
    )
    orb_window.events.closing += on_orb_closing

    # 系统托盘（独立线程跑 pystray 主循环）
    tray_icon = _start_tray()
    threading.Thread(target=tray_icon.run, daemon=True, name="tray-icon").start()

    webview.start(
        debug=DEBUG_MODE,
        http_server=True,
        private_mode=True,
        storage_path=_storage_dir,
        icon=icon_path,
    )

    # webview.start() 在所有窗口销毁后返回（正常只有点托盘「退出」才会走到这里）
    try:
        if tray_icon is not None:
            tray_icon.stop()
    except Exception:
        pass


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