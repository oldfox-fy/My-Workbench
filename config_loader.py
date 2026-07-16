# config_loader.py
import os
import sys
from pathlib import Path
import yaml

class AppConfig:
    def __init__(self, config_file="app_config.yaml"):
        self.config_file = config_file
        self.raw_config = self._load_yaml()
        self._resolve_paths()
        self._ensure_dirs()

    def _load_yaml(self):
        # 搜索 config.yaml 的位置：当前工作目录 -> exe 所在目录 -> 代码目录
        search_paths = [
            Path.cwd() / self.config_file,
            Path(sys.executable).parent / self.config_file,
        ]
        if not getattr(sys, 'frozen', False):
            # 开发环境，尝试当前文件所在目录
            search_paths.append(Path(__file__).parent / self.config_file)
        else:
            search_paths.append(Path(sys._MEIPASS) / self.config_file)
        
        for path in search_paths:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
        
        raise FileNotFoundError(f"配置文件 {self.config_file} 未找到，搜索路径: {search_paths}")

    def _resolve_paths(self):
        # 确定基础目录（用于相对路径解析）
        if getattr(sys, 'frozen', False):
            self.executable_dir = Path(sys.executable).parent  # exe 所在目录
            self.resource_dir = Path(sys._MEIPASS)            # 打包资源临时目录
            data_dir_raw = self.raw_config.get("data_dir", "data")
            self.data_dir = self._resolve_path(data_dir_raw, base=self.executable_dir)
        else:
            self.resource_dir = Path.cwd()
            self.data_dir = Path.cwd()
        
        # 子目录（相对于 data_dir）
        self.uploads_dir = self.data_dir / self.raw_config.get("uploads_dir", "data/uploads")
        self.logs_dir = self.data_dir / self.raw_config.get("logs_dir", "logs")
        self.temp_dir = self.data_dir / self.raw_config.get("temp_dir", "temp")
        self.generate_dir = self.data_dir / self.raw_config.get("generate_dir", "data/generate")
        
        # mcp_config.json 路径：可以是绝对路径或相对于 data_dir
        mcp_raw = self.raw_config.get("mcp_config_path", "mcp_config.json")
        if Path(mcp_raw).is_absolute():
            self.mcp_config_path = Path(mcp_raw)
        else:
            self.mcp_config_path = self.data_dir / mcp_raw
        
        # 静态文件目录（只读，位于 resource_dir 下）
        static_rel = self.raw_config.get("static_dir", "frontend/dist")
        self.static_dir = self.resource_dir / static_rel
        
        # 其他配置项
        self.max_upload_size = int(self.raw_config.get("max_upload_size_mb", 100)) * 1024 * 1024
        self.max_tool_steps = int(self.raw_config.get("max_tool_steps", 25))

        # 模型调用容错配置
        retry_cfg = self.raw_config.get("retry", {}) or {}
        self.max_retries = int(retry_cfg.get("max_retries", 3))
        self.base_delay = float(retry_cfg.get("base_delay", 1.0))

        # 模型降级配置
        fb = self.raw_config.get("fallback", {}) or {}
        self.fallback_config = None
        if fb.get("enabled", False) and fb.get("model_name"):
            self.fallback_config = {
                "model_name": fb.get("model_name", ""),
                "base_url": fb.get("base_url", ""),
                "api_key": fb.get("api_key", ""),
            }

        # 语音服务配置
        vc = self.raw_config.get("voice", {}) or {}
        self.voice_enabled = vc.get("enabled", True)
        self.voice_stt_model = vc.get("stt_model", "whisper-1")
        self.voice_tts_model = vc.get("tts_model", "tts-1")
        self.voice_tts_voice = vc.get("tts_voice", "nova")
        self.voice_stt_base_url = vc.get("stt_base_url", "")
        self.voice_stt_api_key = vc.get("stt_api_key", "")
        self.voice_tts_base_url = vc.get("tts_base_url", "")
        self.voice_tts_api_key = vc.get("tts_api_key", "")

        # 工具审批配置
        ta = self.raw_config.get("tool_approval", {}) or {}
        self.tool_approval_enabled = ta.get("enabled", True)
        self.tool_approval_sensitive = set(ta.get("sensitive_tools", [
            "system_write_file", "system_patch_file",
            "system_run_command", "system_delegate_task",
            "system_ask_user",
        ]))
        self.tool_approval_session_whitelist = ta.get("session_whitelist", True)

        # Skill 智能选择配置
        sc = self.raw_config.get("skill_selection", {}) or {}
        self.skill_selection_enabled = sc.get("enabled", True)
        self.skill_selection_top_k = int(sc.get("top_k", 5))
        self.skill_selection_min_similarity = float(sc.get("min_similarity", 0.3))

        # Skill 优先模式配置
        sf = self.raw_config.get("skill_first", {}) or {}
        self.skill_first_enabled = sf.get("enabled", True)
        self.skill_first_threshold = float(sf.get("threshold", 0.4))

        # 意图识别路由配置
        ir = self.raw_config.get("intent_router", {}) or {}
        self.intent_router_enabled = ir.get("enabled", True)
        self.intent_router_llm_classify = ir.get("llm_classify", True)
        self.intent_router_llm_threshold = float(ir.get("llm_threshold", 0.7))

    def _resolve_path(self, path_str: str, base: Path) -> Path:
        """将路径字符串解析为 Path 对象，支持绝对路径和相对路径"""
        p = Path(path_str)
        if p.is_absolute():
            return p
        else:
            return base / p

    def _ensure_dirs(self):
        """确保所有可写目录存在（data_dir 及其子目录）"""
        dirs = [self.data_dir, self.uploads_dir, self.logs_dir, self.temp_dir, self.generate_dir]
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                # 如果 ProgramData 无权限，尝试 fallback 到用户目录
                fallback_base = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / '.MyWorkbench'
                # 重新设定所有路径
                self.data_dir = fallback_base
                self.uploads_dir = fallback_base / "uploads"
                self.logs_dir = fallback_base / "logs"
                self.temp_dir = fallback_base / "temp"
                self.mcp_config_path = fallback_base / "mcp_config.json"
                # 再次创建
                for d2 in [self.data_dir, self.uploads_dir, self.logs_dir, self.temp_dir]:
                    d2.mkdir(parents=True, exist_ok=True)
                break

    @property
    def frontend_index(self) -> str:
        """返回前端入口文件的本地文件路径"""
        if getattr(sys, 'frozen', False):
            # 打包后的环境，资源文件在 sys._MEIPASS 中
            base_path = Path(sys._MEIPASS)
        else:
            base_path = self.base_dir

        index_path = base_path / "frontend/dist/index.html"

        if not index_path.exists():
            fallback_path = self.static_dir / "index.html"
            if fallback_path.exists():
                index_path = fallback_path
            else:
                raise FileNotFoundError(f"前端入口文件不存在: {index_path}")

        return str(index_path.resolve())

    def resource_path(self, relative_path: str) -> str:
        """获取打包后资源文件的绝对路径（用于图标等）"""
        if getattr(sys, 'frozen', False):
            # 打包环境，资源在 sys._MEIPASS 中
            base_path = Path(sys._MEIPASS)
        else:
            # 开发环境，相对于当前文件所在目录
            base_path = Path(__file__).parent
        return str(base_path / relative_path)

config = AppConfig()