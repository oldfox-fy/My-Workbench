# backend/services/scheduled_tasks.py
# AI 定时自动化任务系统：任务定义持久化 + 后台调度器 + 自然语言调度解析。
#
# 设计：
#   - 任务存 SQLite（scheduled_tasks），执行日志存 scheduled_task_runs。
#   - ScheduledTaskScheduler 后台 asyncio 轮询到期的 active 任务，到点调用
#     LLMService.generate_response 执行 Agent 提示词，保存结果并广播/Toast。
#   - 调度方式（与用户确认）：仅应用运行时触发，关机期间错过的触发点跳过，
#     循环任务顺延到下一个未来时间点（不补执行）。
#   - 无人值守执行：默认 auto_approve 自动放行敏感工具，任务级可关闭。
#
# 支持的自然语言调度：一次性（今天/明天/后天/具体时间/X分钟后）、每天、
# 工作日、每周X、每月X号；规则解析失败时回落到 LLM JSON 提取。
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional

logger = logging.getLogger("My Workbench")

from backend.database import get_db
from backend.services.model_router import get_default_model
from config_loader import config as app_config

# ── 调度类型枚举 ──
SCHEDULE_TYPES = ("once", "daily", "workdays", "weekly", "monthly", "on_startup")

# ── 中文数字 ──
_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEK_RE = re.compile(r"(?:周|星期|礼拜)\s*([一二三四五六日天])")
_PERIOD_ADD12 = {"中午", "下午", "傍晚", "晚上", "夜里", "夜间", "半夜"}
_DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
# 启动触发（事件触发，非时间触发）与修饰启动的时段词
_STARTUP_KEYWORDS = ("启动后", "开机后", "打开后", "登录后", "启动时", "开机时", "打开时", "启动的时候", "启动之后", "启动以后")
_PERIOD_WORDS = ("凌晨", "早晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里", "夜间", "半夜")


def _cn_num(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if "十" in s:
        left, right = s.split("十", 1)
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _parse_dt(s: str) -> Optional[datetime]:
    """把常见时间字符串解析为 datetime（本地时区，无秒补零）。"""
    if not s:
        return None
    s = s.strip().replace("T", " ")
    s = s.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


# ── 调度时间计算 ──

def _parse_time_of_day(tod: str) -> tuple:
    """解析 'HH:MM'，非法或缺省时回落到 09:00。"""
    m = re.match(r"(\d{1,2}):(\d{2})", (tod or "").strip())
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    return 9, 0


def _month_end(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def compute_next_run(
    schedule_type: str,
    time_of_day: str = "",
    weekday: Optional[int] = None,
    monthday: Optional[int] = None,
    run_at: Optional[str] = None,
    after: Optional[datetime] = None,
) -> Optional[datetime]:
    """计算下一次触发时间；一次性任务已过期返回 None。"""
    now = after or datetime.now()
    schedule_type = schedule_type or "once"

    if schedule_type == "on_startup":
        # 启动触发不依赖时间点，由应用启动事件触发，无 next_run_at
        return None

    if schedule_type == "once":
        dt = _parse_dt(run_at) if run_at else None
        if dt is None:
            return None
        return dt if dt > now else None

    hh, mm = _parse_time_of_day(time_of_day)

    def at(hour: int, minute: int, d: datetime) -> datetime:
        return d.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 从「今天」开始逐日找下一个满足条件的日期
    candidate = datetime(now.year, now.month, now.day, hh, mm, 0, 0)
    if candidate <= now:
        candidate += timedelta(days=1)

    guard = 0
    while guard < 800:
        if schedule_type == "daily":
            return candidate
        elif schedule_type == "workdays":
            if candidate.weekday() < 5:  # 周一~周五
                return candidate
        elif schedule_type == "weekly":
            target = int(weekday) if weekday is not None else 0
            if candidate.weekday() == target:
                return candidate
        elif schedule_type == "monthly":
            md = int(monthday) if monthday is not None else 1
            day = min(md, _month_end(candidate.year, candidate.month))
            if candidate.day == day:
                return candidate
        candidate += timedelta(days=1)
        guard += 1
    return None


def human_readable(task: dict) -> str:
    """把任务调度字段渲染成中文说明。"""
    st = task.get("schedule_type") or "once"
    tod = task.get("time_of_day") or ""
    if st == "on_startup":
        return "应用启动后触发"
    if st == "once":
        return f"一次性 · {task.get('run_at') or '（未设置时间）'}"
    if st == "daily":
        return f"每天 {tod or '09:00'}"
    if st == "workdays":
        return f"工作日 {tod or '09:00'}"
    if st == "weekly":
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = task.get("weekday") if task.get("weekday") is not None else 0
        return f"每{names[int(wd) % 7]} {tod or '09:00'}"
    if st == "monthly":
        md = task.get("monthday") if task.get("monthday") is not None else 1
        return f"每月 {int(md)} 号 {tod or '09:00'}"
    return st


# ── 自然语言调度解析 ──

_TIME_RE = re.compile(
    r"(早上|上午|中午|下午|傍晚|晚上|夜里|夜间|半夜|凌晨)?\s*"
    r"([0-9]{1,2}|[零〇一二两三四五六七八九十]+)\s*[:：点]\s*"
    r"([0-9]{1,2}\s*分?|半)?"
)
_COLON_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})")
_REL_RE = re.compile(
    r"([0-9]+|[零〇一二两三四五六七八九十]+)\s*(?:个)?\s*"
    r"(分钟|小时|钟头|秒|分|时|天|日)\s*(?:后|之后)"
)
_REL_UNIT = {"秒": 1, "分": 60, "分钟": 60, "时": 3600, "小时": 3600, "钟头": 3600, "天": 86400, "日": 86400}


def _extract_time(text: str):
    """从文本提取时间点，返回 (hour, minute, matched_phrase) 或 None。"""
    m = _COLON_RE.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi, m.group(0)
    m = _TIME_RE.search(text)
    if m:
        h = _cn_num(m.group(2))
        if h is None or not (0 <= h <= 23):
            return None
        mi = 0
        tail = (m.group(3) or "").strip()
        if tail == "半":
            mi = 30
        elif tail:
            mm = re.match(r"[0-9]{1,2}", tail)
            if mm:
                mi = int(mm.group(0))
        period = m.group(1) or ""
        if period in _PERIOD_ADD12 and h < 12:
            h += 12
        return h, mi, m.group(0)
    return None


def _parse_relative(text: str):
    """解析「X分钟后/小时后」返回 (seconds, phrase) 或 None。"""
    m = _REL_RE.search(text)
    if not m:
        return None
    num = _cn_num(m.group(1))
    unit = _REL_UNIT.get(m.group(2))
    if num is None or not unit:
        return None
    return num * unit, m.group(0)


def _clean_prompt(text: str) -> str:
    text = re.sub(r"^(请|帮我|麻烦|记得|提醒我|提醒|定时|到点|在.*?(?:时|的时候))", "", text)
    text = text.strip(" ，。！？、,;:；~…~！？·：")
    return text.strip()


def _rule_parse(text: str) -> Optional[dict]:
    t = text.strip()
    if not t:
        return None

    schedule_type = None
    weekday = None
    monthday = None
    run_at = None
    time_of_day = ""
    phrases: List[str] = []

    tm = _extract_time(t)
    if tm:
        time_of_day = f"{tm[0]:02d}:{tm[1]:02d}"
        phrases.append(tm[2])

    # 触发类型：on_startup（事件触发，非时间触发）优先于周期/一次性
    if any(k in t for k in _STARTUP_KEYWORDS):
        schedule_type = "on_startup"
        phrases.append(next(k for k in _STARTUP_KEYWORDS if k in t))
        for w in _PERIOD_WORDS:
            if w in t:
                phrases.append(w)
    elif "工作日" in t:
        schedule_type = "workdays"
        phrases.append("工作日")
    elif any(k in t for k in ("每天", "每日", "天天")):
        schedule_type = "daily"
        phrases.append(next(k for k in ("每天", "每日", "天天") if k in t))
    elif _WEEK_RE.search(t) or any(k in t for k in ("每周", "每星期", "每礼拜")):
        schedule_type = "weekly"
        m = _WEEK_RE.search(t)
        if m:
            weekday = _WEEKDAY[m.group(1)]
            phrases.append(m.group(0))
        else:
            phrases.append(next(k for k in ("每周", "每星期", "每礼拜") if k in t))
    elif "每月" in t or "每个月" in t or re.search(r"\d+\s*号", t):
        schedule_type = "monthly"
        m = re.search(r"每月\s*([0-9]{1,2}|[零〇一二两三四五六七八九十]+)\s*[号日]", t)
        if m:
            monthday = _cn_num(m.group(1))
            phrases.append(m.group(0))
        else:
            m = re.search(r"([0-9]{1,2})\s*号", t)
            if m:
                monthday = int(m.group(1))
                phrases.append(m.group(0))
            else:
                phrases.append(next(k for k in ("每月", "每个月") if k in t))

    # 一次性
    if schedule_type is None:
        schedule_type = "once"
        rel = _parse_relative(t)
        day_word = next((w for w in ("大后天", "后天", "明天", "今天") if w in t), None)
        if rel:
            run_at = _iso(datetime.now() + timedelta(seconds=rel[0]))
            phrases.append(rel[1])
        elif day_word:
            off = _DAY_OFFSET[day_word]
            h, mi = tm if tm else (9, 0)
            d = (datetime.now() + timedelta(days=off)).replace(hour=h, minute=mi, second=0, microsecond=0)
            run_at = _iso(d)
            phrases.append(day_word)
        else:
            # 显式日期时间（如 2026-08-27 15:30 / 8月27日）
            m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", t)
            if m and tm:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tm[0], tm[1], 0)
                run_at = _iso(d)
                phrases.append(m.group(0))
            elif m:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 9, 0, 0)
                run_at = _iso(d)
                phrases.append(m.group(0))
            elif not tm:
                # 既无周期也无时间：交给 LLM
                return None

    # 剥离已识别的短语，得到任务 prompt
    rest = t
    for p in phrases:
        rest = rest.replace(p, " ", 1)
    prompt = _clean_prompt(rest)
    if not prompt:
        return None

    title = prompt[:30]
    return {
        "title": title,
        "prompt": prompt,
        "schedule_type": schedule_type,
        "time_of_day": time_of_day,
        "weekday": weekday,
        "monthday": monthday,
        "run_at": run_at,
        "source": "rule",
    }


_LLM_PARSE_PROMPT = """你是定时任务解析器。从用户的一句话里，把「要做的事」和「什么时候执行」分开，输出 JSON。

规则：
- schedule_type 只能是 once / daily / workdays / weekly / monthly / on_startup
- daily/workdays/weekly/monthly 用 time_of_day（HH:MM，24小时制）
- weekly 用 weekday（0=周一..6=周日）
- monthly 用 monthday（1..31）
- once 用 run_at（ISO 格式 "YYYY-MM-DD HH:MM:SS"）
- on_startup 表示「应用启动后触发」，无需时间字段，time_of_day/weekday/monthday/run_at 均设为 null
- title 是简短任务名；prompt 是去掉时间描述后的完整指令

只输出 JSON，不要其它文字。示例：
{"title":"今日新闻简报","prompt":"整理今天的重要新闻并总结","schedule_type":"daily","time_of_day":"08:00","weekday":null,"monthday":null,"run_at":null}

用户输入：{text}"""


async def _llm_parse(text: str, llm_service) -> Optional[dict]:
    try:
        resp = await llm_service.client.chat.completions.create(
            model=llm_service.model_name,
            messages=[
                {"role": "system", "content": "你是一个只输出 JSON 的解析器。"},
                {"role": "user", "content": _LLM_PARSE_PROMPT.replace("{text}", text)},
            ],
            temperature=0,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"[ScheduledTask] LLM 解析调度失败：{e}")
        return None

    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None

    st = data.get("schedule_type") or "once"
    if st not in SCHEDULE_TYPES:
        st = "once"
    prompt = (data.get("prompt") or text).strip()
    result = {
        "title": (data.get("title") or prompt[:30]).strip(),
        "prompt": prompt,
        "schedule_type": st,
        "time_of_day": data.get("time_of_day") or "",
        "weekday": data.get("weekday"),
        "monthday": data.get("monthday"),
        "run_at": data.get("run_at") or None,
        "source": "llm",
    }
    # 一次性必须有 run_at
    if st == "once" and not result["run_at"]:
        result["run_at"] = _iso(datetime.now() + timedelta(hours=1))
    return result


async def parse_schedule_text(text: str, llm_service=None) -> Optional[dict]:
    """解析自然语言调度，返回带 human_readable 的 dict 或 None。"""
    result = _rule_parse(text)
    if result is None and llm_service is not None:
        result = await _llm_parse(text, llm_service)
    if result is None:
        return None
    result["human_readable"] = human_readable({
        "schedule_type": result["schedule_type"],
        "time_of_day": result["time_of_day"],
        "weekday": result["weekday"],
        "monthday": result["monthday"],
        "run_at": result["run_at"],
    })
    return result


# ── 模型快照 → LLMService ──

async def _resolve_model_config(task: dict) -> dict:
    cfg = json.loads(task.get("model_config") or "{}") or {}
    # 定时任务是 chat 型 Agent 任务；非 chat 角色（生图/生视频）或未配置模型时，
    # 回落到默认 chat 模型，避免把提示词发给 Images/Video 生成 API。
    if not cfg.get("model_name") or cfg.get("role") in ("image_gen", "video"):
        m = await get_default_model()
        if m:
            cfg = {
                "type": m.get("type", "online"),
                "model_name": m.get("modelName", ""),
                "base_url": m.get("baseUrl", ""),
                "api_key": m.get("apiKey", ""),
                "role": m.get("role", "default"),
                "thinking": "enabled",
            }
    return cfg


async def _build_service(task: dict):
    from backend.services.llm_service import LLMService
    cfg = await _resolve_model_config(task)
    return LLMService(
        model_type=cfg.get("type", "online"),
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url") or None,
        api_key=cfg.get("api_key", ""),
        thinking=cfg.get("thinking", "enabled"),
        max_retries=getattr(app_config, "max_retries", 3),
        base_delay=getattr(app_config, "base_delay", 1.0),
        fallback_config=getattr(app_config, "fallback_config", None),
        role=cfg.get("role", "default"),
    )


def _build_messages(task: dict) -> List[dict]:
    from backend.routes.chat import BASE_SYSTEM_PROMPT
    from backend.utils.base import get_current_time
    import backend
    sp = BASE_SYSTEM_PROMPT.replace("{{workspace_path}}", backend.workspace_path)
    sp = sp.replace("{{kb_path}}", getattr(backend, "kb_path", "") or backend.workspace_path)
    sp = sp + "\n\n---\n当前时间：" + get_current_time()
    # 定时任务专属上下文：明确这是无人值守的自动执行，结果会由系统自动通知用户，
    # 避免模型输出「我无法推送/无法通知/无法新建窗口」之类的自述，或试图自己发通知。
    sp = sp + (
        "\n\n---\n【定时任务执行上下文】\n"
        "你正在由「定时任务」系统自动触发执行（无人值守）。\n"
        "执行完成后，你的最终回答会被系统自动通知用户并写入对话，"
        "你无需也无法主动「推送消息」「发送通知」或「新建对话/窗口」。\n"
        "请直接专注完成用户指令，输出最终结果正文，不要就「如何通知用户」本身进行说明。"
    )
    return [
        {"role": "system", "content": sp},
        {"role": "user", "content": task.get("prompt") or ""},
    ]


async def _build_task_tools(mcp_manager, skill_registry, enable_tools, profile_id, messages):
    from backend.services.tools import get_local_tools, get_mcp_tools, get_all_tools
    from backend.routes.chat import default_tools, disabled_tools, _detect_command_intent, _detect_ppt_intent

    if not enable_tools:
        return None

    local_tools = get_local_tools()
    tools: List[dict] = []

    if profile_id == 0:
        tools = await get_all_tools(mcp_manager, skill_registry)
    elif profile_id is not None:
        db = await get_db()
        cur = await db.execute("SELECT tools, skills FROM profiles WHERE id = ?", (profile_id,))
        row = await cur.fetchone()
        await db.close()
        allowed_tools = json.loads(row[0] or "[]") if row else []
        profile_skills = json.loads(row[1] or "[]") if row and len(row) > 1 and row[1] else []

        tools = [t for t in local_tools if t["function"]["name"] in default_tools]
        mcp_tools = await get_mcp_tools(mcp_manager)
        enabled = [t for t in local_tools if t["function"]["name"] in disabled_tools]
        enabled.extend(mcp_tools)
        tools.extend([t for t in enabled if t["function"]["name"] in allowed_tools])
        if skill_registry and profile_skills:
            code_defs = skill_registry.code_tool_definitions()
            tools.extend([d for d in code_defs if d["function"]["name"] in profile_skills])
    else:
        tools = [t for t in local_tools if t["function"]["name"] in default_tools]
        # 定时任务默认也带上工作台的 MCP 工具 + code 型技能，便于执行联网检索等自动化任务
        tools.extend(await get_mcp_tools(mcp_manager))
        if skill_registry:
            tools.extend(skill_registry.code_tool_definitions())

    # 命令/PPT 意图按需注入
    existing = {t["function"]["name"] for t in tools}
    if _detect_command_intent(messages):
        for t in local_tools:
            if t["function"]["name"] == "system_run_command" and t["function"]["name"] not in existing:
                tools.append(t)
    if _detect_ppt_intent(messages):
        for t in local_tools:
            if t["function"]["name"] == "system_generate_pptx" and t["function"]["name"] not in existing:
                tools.append(t)

    tools.sort(key=lambda t: t.get("function", {}).get("name", ""))
    return tools


class _TaskRequest:
    """伪请求对象：为 generate_response 提供 user 上下文，且永不判定为断开。"""

    def __init__(self, user_id):
        self.state = SimpleNamespace(user={"id": user_id})

    async def is_disconnected(self) -> bool:
        return False


async def _persist_result_chat(task: dict, output_text: str, error_text: str, status: str) -> Optional[str]:
    """把定时任务执行结果落库为一个新对话（user=任务指令，assistant=结果）。

    方向 C：结果进新会话，而不是塞进用户当前对话，便于用户在新会话里查看与继续。
    """
    import uuid
    user_id = task.get("user_id")
    prompt = task.get("prompt") or ""
    title = task.get("title") or "定时任务"
    content = output_text if status == "success" else (error_text or "执行失败")
    chat_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO chats (id, title, created_at, user_id) VALUES (?, ?, ?, ?)",
                (chat_id, title, now, user_id),
            )
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, file_ref, user_id) VALUES (?, 'user', ?, NULL, ?)",
                (chat_id, prompt, user_id),
            )
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, file_ref, user_id) VALUES (?, 'assistant', ?, NULL, ?)",
                (chat_id, content, user_id),
            )
            await db.commit()
        finally:
            await db.close()
        return chat_id
    except Exception as e:
        logger.warning(f"[ScheduledTask] 结果落库新对话失败：{e}")
        return None


async def execute_task(task_id: int, mcp_manager=None, skill_registry=None) -> Optional[dict]:
    """执行单个定时任务：写运行记录 → 跑 Agent → 更新记录 → 广播 + Toast。"""
    db = await get_db()
    cur = await db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    await db.close()
    if not row:
        return None
    task = dict(row)

    # 创建运行记录
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO scheduled_task_runs (task_id, status, scheduled_for) VALUES (?, 'running', ?)",
        (task_id, task.get("next_run_at")),
    )
    run_id = cur.lastrowid
    await db.commit()
    await db.close()

    status = "success"
    output_text = ""
    error_text = ""
    try:
        service = await _build_service(task)
        messages = _build_messages(task)
        tools = await _build_task_tools(
            mcp_manager, skill_registry,
            bool(task.get("enable_tools", 1)), task.get("profile_id"), messages,
        )
        req = _TaskRequest(task.get("user_id"))
        chunks: List[str] = []
        agen = service.generate_response(
            messages=messages,
            enable_tools=bool(task.get("enable_tools", 1)),
            tools=tools,
            request=req,
            mcp_manager=mcp_manager,
            params={},
            skill_registry=skill_registry,
            auto_approve=bool(task.get("auto_approve", 1)),
        )
        async for chunk in agen:
            if chunk and not chunk.startswith("<!--"):
                chunks.append(chunk)
        output_text = "".join(chunks).strip()
    except Exception as e:
        status = "failed"
        error_text = str(e)
        logger.warning(f"[ScheduledTask] 任务 {task_id} 执行失败：{e}")

    db = await get_db()
    await db.execute(
        "UPDATE scheduled_task_runs SET status=?, finished_at=?, output=?, error=? WHERE id=?",
        (status, _iso(datetime.now()), output_text, error_text, run_id),
    )
    await db.commit()
    await db.close()

    # 结果落库为新对话（方向 C，任务级开关 create_chat 控制），并把新会话 id 随广播带给前端
    if bool(task.get("create_chat", 1)):
        chat_id = await _persist_result_chat(task, output_text, error_text, status)
    else:
        chat_id = None

    # 广播 + 系统通知
    title = task.get("title") or "定时任务"
    preview = (output_text or error_text)[:200]
    try:
        from backend.voice.state import connection_manager
        await connection_manager.broadcast({
            "type": "scheduled_task",
            "task_id": task_id,
            "title": title,
            "status": status,
            "output_preview": preview,
            "chat_id": chat_id,
            "scheduled_for": task.get("next_run_at"),
        })
    except Exception as e:
        logger.warning(f"[ScheduledTask] 广播失败：{e}")
    try:
        from backend.utils.toast import send_toast_async
        body = preview[:100] if status == "success" else f"执行失败：{error_text[:100]}"
        await send_toast_async(f"定时任务 · {title}", body)
    except Exception:
        pass

    return {"id": task_id, "status": status, "output": output_text, "error": error_text}


# ── 启动触发 ──

async def _run_startup_task(task_id: int, mcp_manager=None, skill_registry=None):
    try:
        await execute_task(task_id, mcp_manager, skill_registry)
    except Exception as e:
        logger.warning(f"[ScheduledTask] 启动任务 {task_id} 执行异常：{e}")


async def trigger_startup_tasks(mcp_manager=None, skill_registry=None) -> List[int]:
    """触发所有「应用启动后」类型的 active 任务（应用每次启动时调用一次）。

    这些任务不依赖 next_run_at，由启动事件触发；结果照常落库/广播/通知。
    不阻塞启动：逐个用 asyncio.create_task 后台执行。返回触发的任务 id 列表。
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT id FROM scheduled_tasks WHERE status='active' AND schedule_type='on_startup'"
    )
    rows = await cur.fetchall()
    await db.close()
    ids = [r["id"] for r in rows]
    for tid in ids:
        asyncio.create_task(_run_startup_task(tid, mcp_manager, skill_registry))
    if ids:
        logger.info(f"[ScheduledTask] 已触发 {len(ids)} 个「启动后」任务")
    return ids


# ── 调度器 ──

class ScheduledTaskScheduler:
    """后台定时任务调度器：轮询到期任务并执行。"""

    def __init__(self, interval: float = 30.0, mcp_manager=None, skill_registry=None):
        self._interval = interval
        self._mcp_manager = mcp_manager
        self._skill_registry = skill_registry
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _set(self, task_id: int, **fields):
        if not fields:
            return
        sets = [f"{k}=?" for k in fields]
        vals = list(fields.values())
        sets.append("updated_at=?")
        vals.append(_iso(datetime.now()))
        db = await get_db()
        await db.execute(
            f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id=?", (*vals, task_id)
        )
        await db.commit()
        await db.close()

    async def _fetch_due(self, now_iso: str) -> List[dict]:
        db = await get_db()
        cur = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now_iso,),
        )
        rows = await cur.fetchall()
        await db.close()
        return [dict(r) for r in rows]

    async def _loop(self):
        while self._running:
            try:
                now_iso = _iso(datetime.now())
                due = await self._fetch_due(now_iso)
                for t in due:
                    try:
                        await execute_task(t["id"], self._mcp_manager, self._skill_registry)
                    except Exception as e:
                        logger.warning(f"[ScheduledTask] 任务 {t['id']} 执行异常：{e}")
                    # 推进下次触发时间（一次性 → completed；循环 → 顺延到未来）
                    if t["schedule_type"] == "once":
                        await self._set(t["id"], status="completed", next_run_at=None)
                    elif t["schedule_type"] == "on_startup":
                        # 启动触发任务不推进、不改状态（每次启动由 trigger_startup_tasks 触发）
                        continue
                    else:
                        nxt = compute_next_run(
                            t["schedule_type"], t.get("time_of_day"),
                            t.get("weekday"), t.get("monthday"), None, after=datetime.now(),
                        )
                        if nxt:
                            await self._set(t["id"], next_run_at=_iso(nxt))
                        else:
                            await self._set(t["id"], status="completed", next_run_at=None)
            except Exception as e:
                logger.warning(f"[ScheduledTask] 调度循环异常：{e}")
            await asyncio.sleep(self._interval)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[ScheduledTask] 定时任务调度器已启动")

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("[ScheduledTask] 定时任务调度器已停止")
