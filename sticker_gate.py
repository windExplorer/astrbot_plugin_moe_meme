"""萌萌表情包 — 防打扰闸门（纯函数，不依赖 AstrBot / aiohttp，可独立测试）。

主动发送（LLM 工具 send_sticker / 关键词触发）共用一套判定：
冷却、每日上限、安静时段。两类主动共享同一份会话状态——
对用户来说「bot 又刷表情了」不区分来源，防打扰也应当合并计数。

指令点播不走本闸门，只共用「最近去重」（GateState.recent）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

# decide() 的返回值：OK 为空串，其余为原因码（文案转换在 main.py 做）
OK = ""
REASON_QUIET = "quiet"
REASON_COOLDOWN = "cooldown"
REASON_DAILY_CAP = "daily_cap"


def parse_quiet_hours(text: str):
    """解析 "01:00-08:00" → (time(1,0), time(8,0))。

    空 / 格式非法 / 越界一律返回 None（等于关闭安静时段），调用方无需再判错。
    """
    if not text:
        return None
    s = text.strip().replace("：", ":")
    if "-" not in s:
        return None
    a, b = s.split("-", 1)

    def _t(part: str):
        part = part.strip()
        if ":" not in part:
            return None
        hh, mm = part.split(":", 1)
        if not (hh.isdigit() and mm.isdigit()):
            return None
        h, m = int(hh), int(mm)
        if h > 23 or m > 59:
            return None
        return dtime(h, m)

    ta, tb = _t(a), _t(b)
    if ta is None or tb is None:
        return None
    return ta, tb


def in_quiet_hours(now: dtime, span) -> bool:
    """now 是否落在安静时段内。span 为 None 恒 False；支持跨零点（如 23:00-06:00）。"""
    if span is None:
        return False
    a, b = span
    if a == b:
        return False
    if a < b:
        return a <= now < b
    return now >= a or now < b


@dataclass
class GateState:
    """单个会话的闸门状态。"""

    last_ts: float = 0.0
    sent_today: int = 0
    day: str = ""
    recent: deque = field(default_factory=lambda: deque(maxlen=50))

    def remember_recent(self, sticker_id: str, dedup_keep: int = 20) -> None:
        """记下刚发过的表情 id；dedup_keep <= 0 表示关闭去重。"""
        if not sticker_id or dedup_keep <= 0:
            return
        self.recent.append(sticker_id)
        while len(self.recent) > dedup_keep:
            self.recent.popleft()

    def seen_recently(self, sticker_id: str) -> bool:
        return bool(sticker_id) and sticker_id in self.recent


def _rollover(state: GateState, now: datetime) -> None:
    today = now.strftime("%Y-%m-%d")
    if state.day != today:
        state.day = today
        state.sent_today = 0


def decide(
    state: GateState,
    now: datetime,
    cooldown_minutes: int = 10,
    daily_cap: int = 20,
    quiet_hours=None,
) -> str:
    """判定此刻能否主动发送。只读不改状态；返回 OK("") 或原因码。"""
    _rollover(state, now)
    if in_quiet_hours(now.time(), quiet_hours):
        return REASON_QUIET
    if daily_cap > 0 and state.sent_today >= daily_cap:
        return REASON_DAILY_CAP
    if cooldown_minutes > 0 and state.last_ts > 0:
        if now.timestamp() - state.last_ts < cooldown_minutes * 60:
            return REASON_COOLDOWN
    return OK


def record(state: GateState, now: datetime, sticker_id: str = "", dedup_keep: int = 20) -> None:
    """实际发送成功后记账：刷新冷却时间、累计当日次数、写入最近去重表。"""
    _rollover(state, now)
    state.last_ts = now.timestamp()
    state.sent_today += 1
    state.remember_recent(sticker_id, dedup_keep)
