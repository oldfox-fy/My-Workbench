"""
My Workbench — 开机自启动管理

用法：
    python autostart.py install     注册开机自启动（静默后台启动，托盘 + 悬浮球常驻）
    python autostart.py uninstall   取消开机自启动
    python autostart.py status      查看当前状态

实现说明：
    通过 Windows 注册表 HKCU\\...\\Run 键注册一条启动项，指向
    .venv\\Scripts\\pythonw.exe（无控制台 Python）运行
    start_desktop.py --silent。silent 模式会隐藏 vite 控制台、
    把日志写入 logs/autostart.log，并且主窗口启动即隐藏——只驻留
    系统托盘 + 悬浮球，点托盘或悬浮球即可唤起。
"""
import os
import sys

# Windows 控制台默认 GBK，emoji（✅/❌/ℹ️）会触发 UnicodeEncodeError；
# 统一转 UTF-8 并容错，避免在任意终端环境下崩溃。
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ENTRY_NAME = "MyWorkbench"


def _project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _launch_command() -> str:
    """返回开机自启动要执行的命令行（pythonw + start_desktop.py --silent）。"""
    root = _project_root()
    pythonw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        # 回退到系统 PATH 中的 pythonw
        pythonw = "pythonw"
    script = os.path.join(root, "start_desktop.py")
    return f'"{pythonw}" "{script}" --silent'


def install() -> None:
    import winreg

    cmd = _launch_command()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, cmd)
    print(f"✅ 已注册开机自启动:\n   {cmd}")


def uninstall() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
        print("✅ 已取消开机自启动")
    except FileNotFoundError:
        print("ℹ️  未检测到开机自启动项，无需取消")


def status() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
        print(f"✅ 已启用开机自启动:\n   {value}")
    except FileNotFoundError:
        print("❌ 未启用开机自启动")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    handler = {"install": install, "uninstall": uninstall, "status": status}.get(
        action
    )
    if handler is None:
        print(__doc__)
        sys.exit(1)
    handler()
