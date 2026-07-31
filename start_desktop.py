"""
My Workbench 一键启动器
功能：启动前端 vite 开发服务器 → 启动后端 FastAPI → 打开原生桌面窗口
"""
import os
import sys
import time
import socket
import subprocess
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(SCRIPT_DIR, "frontend")


def find_free_port(start=5175, max_attempts=10):
    """查找可用端口"""
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def is_port_open(port, host="127.0.0.1", timeout=0.5):
    """检查端口是否已监听"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def wait_for_port(port, host="127.0.0.1", timeout=30):
    """轮询等待端口就绪"""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(port, host):
            return True
        time.sleep(0.2)
    return False


def check_command(cmd):
    """检查命令是否可用"""
    try:
        subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception:
        return False


def start_frontend():
    """启动前端 vite 开发服务器"""
    print("[1/3] 启动前端开发服务器...")

    if not check_command("node"):
        print("      未找到 Node.js，跳过前端（将使用静态文件模式）")
        return None

    # 检查 node_modules
    node_modules = os.path.join(FRONTEND_DIR, "node_modules")
    if not os.path.isdir(node_modules):
        print("      首次运行，安装前端依赖...")
        result = subprocess.run(
            ["npm", "install"],
            cwd=FRONTEND_DIR,
            shell=True,
        )
        if result.returncode != 0:
            print("[错误] 前端依赖安装失败")
            return None

    # 启动 vite
    print("      启动 vite 开发服务器（端口 5175）...")
    if sys.platform == "win32":
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=FRONTEND_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=FRONTEND_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # 等待 vite 就绪
    print("      等待 vite 就绪...")
    if wait_for_port(5175, timeout=15):
        print("      前端已就绪: http://localhost:5175")
        return proc
    else:
        print("[警告] 前端可能尚未完全就绪，继续启动...")
        return proc


def start_gui():
    """启动后端 + 桌面窗口"""
    print("[2/3] 启动后端 + 桌面窗口...")

    # 直接调用 main.py 的 start_gui 函数
    sys.path.insert(0, SCRIPT_DIR)
    os.chdir(SCRIPT_DIR)

    import main
    main.start_gui()


def cleanup(frontend_proc):
    """清理前端进程"""
    if frontend_proc is None:
        return
    print("正在关闭前端开发服务器...")
    try:
        if sys.platform == "win32":
            frontend_proc.terminate()
            try:
                frontend_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                frontend_proc.kill()
        else:
            frontend_proc.terminate()
            try:
                frontend_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                frontend_proc.kill()
    except Exception as e:
        print(f"[警告] 关闭前端进程失败: {e}")
        # 兜底：通过端口杀进程
        if sys.platform == "win32":
            subprocess.run(
                ['for /f "tokens=5" %a in (\'netstat -ano ^| findstr ":5175.*LISTENING"\') do taskkill /f /pid %a 2>nul'],
                shell=True,
            )


def main():
    print()
    print("=" * 60)
    print("   My Workbench — 一键启动桌面版")
    print("=" * 60)
    print()

    os.chdir(SCRIPT_DIR)

    # 1. 激活虚拟环境
    if sys.platform == "win32":
        activate_script = os.path.join(SCRIPT_DIR, ".venv", "Scripts", "activate.bat")
    else:
        activate_script = os.path.join(SCRIPT_DIR, ".venv", "bin", "activate")

    # 将 venv Python 加入 PATH 最前面
    if sys.platform == "win32":
        venv_scripts = os.path.join(SCRIPT_DIR, ".venv", "Scripts")
    else:
        venv_scripts = os.path.join(SCRIPT_DIR, ".venv", "bin")
    if os.path.isdir(venv_scripts):
        os.environ["PATH"] = venv_scripts + os.pathsep + os.environ.get("PATH", "")
        # 确保后续 subprocess 使用 venv Python
        python_exe = os.path.join(venv_scripts, "python.exe" if sys.platform == "win32" else "python")
        if os.path.exists(python_exe):
            sys.executable = python_exe
        print(f"[venv] 使用虚拟环境: {venv_scripts}")
    else:
        print("[venv] 未找到虚拟环境，使用系统 Python")

    # 2. 前置检查
    print("[检查] 验证依赖...")
    try:
        import webview
        import uvicorn
        import fastapi
    except ImportError as e:
        print(f"[错误] 缺少依赖: {e}")
        print("       请运行: pip install -r requirements.txt")
        input("按任意键退出...")
        sys.exit(1)
    print("[检查] 核心依赖 OK")

    # 3. 启动前端
    frontend_proc = start_frontend()

    # 4. 启动后端 + GUI
    print()
    print("  ┌────────────────────────────────────────┐")
    print("  │  后端地址 : http://localhost:8080       │")
    print("  │  前端地址 : http://localhost:5175       │")
    print("  │  桌面窗口将自动打开                     │")
    print("  │  关闭桌面窗口即退出程序                 │")
    print("  └────────────────────────────────────────┘")
    print()

    try:
        start_gui()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[错误] GUI 启动失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cleanup(frontend_proc)

    print("My Workbench 已退出。")


if __name__ == "__main__":
    main()
