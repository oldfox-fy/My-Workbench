# backend/__init__.py
import sys
from pathlib import Path


workspace_dir = Path.cwd() / 'workspace'
workspace_path = str(workspace_dir)

# 我的知识库根目录（由前端设置）。
# 开发模式下默认指向本地知识库，方便调试；打包模式默认空，由用户在界面中配置。
_DEV_DEFAULT_KB = r"E:\fuyu\LearnAI\MyKg"
if not getattr(sys, "frozen", False) and Path(_DEV_DEFAULT_KB).is_dir():
    kb_path = _DEV_DEFAULT_KB
else:
    kb_path = ""