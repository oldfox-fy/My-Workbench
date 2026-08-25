# backend/voice/reminders.py
# 定时/提醒（P2）：持久化提醒存储 + 后台调度器 + 中文时间短语解析。
#
# 设计：
#   - ReminderStore：JSON 持久化到 data_dir/reminders.json，线程安全（加锁）。
#   - ReminderScheduler：asyncio 后台任务，每 N 秒轮询到期的提醒，触发时
#     广播到前端（/ws/assistant 的 type=reminder）+ 本地 TTS 播报。
#   - parse_reminder：中文时间短语解析（快通道），解析不了返回 None → 交给 LLM。
#
# 覆盖的常见说法：
#   "10分钟后提醒我开会"、"8点叫我起床"、"明天早上8点提醒我吃药"、
#   "每30分钟提醒我喝水"、"半小时后提醒我"、"提醒我下午3点开会" …
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger("My Workbench")

# ── 中文数字 / 时间单位 ──

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_UNIT_SEC = {
    "秒": 1, "分钟": 60, "分": 60, "小时": 3600, "时": 3600,
    "钟头": 3600, "天": 86400, "日": 86400,
}

_DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}

# 时段 → 是否需要 +12 小时（下午/晚上等）。凌晨/早上/上午/早晨不 +12。
_PERIOD_ADD12 = {"中午", "下午", "傍晚", "晚上", "夜里", "半夜", "夜间"}

# 连接词（提醒/叫我/记得…），用于从句子中剥离出「提醒内容」
_CONNECTOR_RE = re.compile(r"^\s*(提醒我|提醒|叫我|记得|通知我|告诉我|喊我|帮我|叫一下|叫)\s*")

# 相对时间：如 "10分钟后"、"半小时后"、"两个小时后"
_REL_RE = re.compile(
    r"(?P<num>半|[0-9]+|[零〇一二两三四五六七八九十]+)\s*(?:个)?\s*"
    r"(?P<unit>分钟|小时|钟头|秒|分|时|天|日)\s*(?:后|之后)"
)

# 循环提醒：如 "每30分钟"、"每小时"、"每天"
_RECUR_RE = re.compile(
    r"每\s*(?P<num>半|[0-9]+|[零〇一二两三四五六七八九十]+)\s*(?:个)?\s*"
    r"(?P<unit>分钟|小时|钟头|秒|分|时|天|日)"
)

# 绝对时间点：如 "8点"、"8点半"、"8点30分"、"8:30"、"八点"
_ABS_TIME_RE = re.compile(
    r"([0-9]{1,2}|[零〇一二两三四五六七八九十]+)\s*[:：点]\s*([0-9]{1,2}\s*分?|半)?"
)


def _parse_number(s: str) -> Optional[int]:
    """解析阿拉伯数字或简单中文数字（支持「十」「十五」「二十三」）。"""
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


def _unit_seconds(num, unit: str) -> int:
    base = _UNIT_SEC.get(unit)
    if base is None or num is None:
        return 0
    return int(round(num * base))


def _clean_content(rest: str) -> Optional[str]:
    """剥离连接词与标点，得到提醒内容。"""
    rest = _CONNECTOR_RE.sub("", rest, count=1)
    rest = rest.strip(" ，。！？、,;:；:~…~！？·")
    return rest or None


def _extract_content(text: str, *phrases: str) -> Optional[str]:
    """从原句中依次去掉匹配到的时间短语，再清理得到内容。"""
    rest = text
    for p in phrases:
        rest = rest.replace(p, "", 1)
    return _clean_content(rest)


def _parse_absolute(text: str, now: float) -> Optional[dict]:
    """解析绝对时间（如「明天早上8点」「下午3点半」）。"""
    m = _ABS_TIME_RE.search(text)
    if not m:
        return None
    hour = _parse_number(m.group(1))
    if hour is None or not (0 <= hour <= 23):
        return None

    minute = 0
    tail = (m.group(2) or "").strip()
    if tail == "半":
        minute = 30
    elif tail:
        mm = re.match(r"[0-9]{1,2}", tail)
        if mm:
            minute = int(mm.group(1))
    if not (0 <= minute <= 59):
        return None

    # 日期偏移（今天/明天/后天/大后天）
    day_offset = 0
    day_specified = False
    day_word = ""
    for word in ("大后天", "后天", "明天", "今天"):
        if word in text:
            day_offset = _DAY_OFFSET[word]
            day_specified = True
            day_word = word
            break

    # 时段（下午/晚上 → +12 小时）
    period_word = ""
    for word in ("凌晨", "早晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里", "夜间", "半夜"):
        if word in text:
            period_word = word
            break
    if period_word in _PERIOD_ADD12 and hour < 12:
        hour += 12

    now_dt = datetime.fromtimestamp(now)
    target = (now_dt + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if not day_specified and target.timestamp() <= now:
        target += timedelta(days=1)  # 未指定日期且时间已过 → 顺延到明天

    content = _extract_content(text, m.group(0), day_word, period_word)
    if not content:
        return None
    return {"content": content, "due_ts": target.timestamp(), "recurring_seconds": 0}


def parse_reminder(text: str, now: Optional[float] = None) -> Optional[dict]:
    """解析中文提醒短语，返回 {content, due_ts, recurring_seconds} 或 None。

    解析不了返回 None，调用方（try_fast_route）应回落到 LLM 慢通道。
    """
    text = (text or "").strip()
    if not text:
        return None
    if now is None:
        now = time.time()

    # 1. 循环提醒（每 X 分钟/小时/天）
    m = _RECUR_RE.search(text)
    if m:
        num = 0.5 if m.group("num") == "半" else _parse_number(m.group("num"))
        secs = _unit_seconds(num, m.group("unit"))
        if secs:
            content = _extract_content(text, m.group(0))
            if content:
                return {"content": content, "due_ts": now + secs, "recurring_seconds": secs}

    # 2. 相对时间（X 分钟后/小时后）
    m = _REL_RE.search(text)
    if m:
        num = 0.5 if m.group("num") == "半" else _parse_number(m.group("num"))
        secs = _unit_seconds(num, m.group("unit"))
        if secs:
            content = _extract_content(text, m.group(0))
            if content:
                return {"content": content, "due_ts": now + secs, "recurring_seconds": 0}

    # 3. 绝对时间（X 点 / 明天早上 X 点）
    return _parse_absolute(text, now)


# ── 存储 ──

class ReminderStore:
    """提醒的 JSON 持久化存储（线程安全）。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._items: List[dict] = []
        self._next_id = 1
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._items = data.get("items", [])
                self._next_id = int(data.get("next_id", len(self._items) + 1))
        except Exception as e:
            logger.warning(f"[Reminder] 读取提醒文件失败，使用空列表：{e}")
            self._items = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": self._items, "next_id": self._next_id}, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[Reminder] 保存提醒失败：{e}")

    def add(self, content: str, due_ts: float, recurring_seconds: int = 0) -> dict:
        with self._lock:
            item = {
                "id": self._next_id,
                "content": (content or "").strip(),
                "due_ts": float(due_ts),
                "recurring_seconds": int(recurring_seconds or 0),
                "created_ts": time.time(),
            }
            self._next_id += 1
            self._items.append(item)
            self._save()
            return dict(item)

    def list(self) -> List[dict]:
        with self._lock:
            return sorted(self._items, key=lambda x: x["due_ts"])

    def cancel(self, reminder_id: int) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != int(reminder_id)]
            changed = len(self._items) != before
            if changed:
                self._save()
            return changed

    def pop_due(self, now: float) -> List[dict]:
        """取出到期的提醒。一次性提醒移除；循环提醒顺延到下一周期。"""
        with self._lock:
            due = [i for i in self._items if i["due_ts"] <= now]
            for item in due:
                if item["recurring_seconds"] > 0:
                    # 循环提醒：顺延，跳过已错过的周期
                    item["due_ts"] += item["recurring_seconds"]
                    while item["due_ts"] <= now:
                        item["due_ts"] += item["recurring_seconds"]
                else:
                    self._items.remove(item)
            if due:
                self._save()
            return [dict(i) for i in due]


# 进程内单例
_store: Optional[ReminderStore] = None


def get_store() -> ReminderStore:
    global _store
    if _store is None:
        from config_loader import config
        path = getattr(config, "reminders_file", "") or str(config.data_dir / "reminders.json")
        _store = ReminderStore(path)
    return _store


# ── 调度器 ──

class ReminderScheduler:
    """后台提醒调度器：轮询到期提醒 → 广播 + 本地播报。"""

    def __init__(self, store: ReminderStore, interval: float = 2.0):
        self._store = store
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _on_fire(self, reminder: dict):
        from .state import connection_manager
        from .tts import get_tts
        from backend.utils.toast import send_toast_async
        await connection_manager.broadcast({
            "type": "reminder",
            "id": reminder["id"],
            "content": reminder["content"],
            "recurring_seconds": reminder.get("recurring_seconds", 0),
        })
        # 视觉弹窗 + 本地语音播报（Toast 失败不影响语音）
        try:
            await send_toast_async("提醒", reminder["content"])
        except Exception as e:
            logger.warning(f"[Reminder] 发送 Toast 失败：{e}")
        await get_tts().speak_async(f"提醒：{reminder['content']}")

    async def _loop(self):
        while self._running:
            try:
                due = await asyncio.to_thread(self._store.pop_due, time.time())
                for r in due:
                    try:
                        await self._on_fire(r)
                    except Exception as e:
                        logger.warning(f"[Reminder] 触发提醒失败：{e}")
            except Exception as e:
                logger.warning(f"[Reminder] 调度循环异常：{e}")
            await asyncio.sleep(self._interval)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[Reminder] 提醒调度器已启动")

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("[Reminder] 提醒调度器已停止")
