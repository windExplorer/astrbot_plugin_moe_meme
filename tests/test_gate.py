"""sticker_gate 纯函数自检。运行：uv run --no-project python tests/test_gate.py"""

import importlib.util
import sys
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sticker_gate", ROOT / "sticker_gate.py")
sg = importlib.util.module_from_spec(_spec)
# dataclass 解析字符串化注解时会查 sys.modules，必须先注册
sys.modules["sticker_gate"] = sg
_spec.loader.exec_module(sg)


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# --- parse_quiet_hours ---
expect(sg.parse_quiet_hours("") is None, "空字符串应为 None")
expect(sg.parse_quiet_hours(None) is None, "None 应为 None")
expect(sg.parse_quiet_hours("01:00-08:00") == (dtime(1, 0), dtime(8, 0)), "常规解析")
expect(sg.parse_quiet_hours("23:00-06:00") == (dtime(23, 0), dtime(6, 0)), "跨零点解析")
expect(sg.parse_quiet_hours(" 1:05 - 8:00 ") == (dtime(1, 5), dtime(8, 0)), "容忍空白")
expect(sg.parse_quiet_hours("25:00-08:00") is None, "非法小时拒绝")
expect(sg.parse_quiet_hours("abc") is None, "非法格式拒绝")
expect(sg.parse_quiet_hours("01:00 08:00") is None, "缺分隔符拒绝")

# --- in_quiet_hours ---
span = sg.parse_quiet_hours("01:00-08:00")
expect(not sg.in_quiet_hours(dtime(9, 0), span), "9 点不在安静段")
expect(sg.in_quiet_hours(dtime(2, 0), span), "2 点在安静段")
expect(not sg.in_quiet_hours(dtime(8, 0), span), "8 点整不算（左闭右开）")
expect(not sg.in_quiet_hours(dtime(1, 0), None), "span=None 恒 False")
overnight = sg.parse_quiet_hours("23:00-06:00")
expect(sg.in_quiet_hours(dtime(23, 30), overnight), "跨零点：23:30 在段内")
expect(sg.in_quiet_hours(dtime(5, 0), overnight), "跨零点：5 点在段内")
expect(sg.in_quiet_hours(dtime(0, 30), overnight), "跨零点：0:30 在段内")
expect(not sg.in_quiet_hours(dtime(12, 0), overnight), "跨零点：白天不在段内")

# --- decide/record：冷却 ---
st = sg.GateState()
now = datetime(2026, 9, 29, 12, 0, 0)
expect(sg.decide(st, now, cooldown_minutes=10) == sg.OK, "初始可发")
sg.record(st, now, sticker_id="a", dedup_keep=20)
expect(sg.decide(st, datetime(2026, 9, 29, 12, 5)) == sg.REASON_COOLDOWN, "5 分钟后仍在冷却")
expect(sg.decide(st, datetime(2026, 9, 29, 12, 10, 1)) == sg.OK, "过冷却可发")

# --- decide/record：每日上限与跨天重置 ---
st2 = sg.GateState()
t = datetime(2026, 9, 29, 12, 0)
for i in range(20):
    sg.record(st2, t, sticker_id=f"s{i}", dedup_keep=50)
expect(sg.decide(st2, datetime(2026, 9, 29, 23, 0), cooldown_minutes=10, daily_cap=20)
       == sg.REASON_DAILY_CAP, "达到日上限")
expect(sg.decide(st2, datetime(2026, 9, 30, 12, 0), cooldown_minutes=10, daily_cap=20)
       == sg.OK, "次日重置")
expect(sg.decide(st2, datetime(2026, 9, 29, 23, 0), cooldown_minutes=10, daily_cap=0)
       == sg.OK, "daily_cap=0 表示不限（此时冷却已过）")

# --- decide：安静时段优先 ---
st3 = sg.GateState()
qh = sg.parse_quiet_hours("01:00-08:00")
expect(sg.decide(st3, datetime(2026, 9, 29, 3, 0), quiet_hours=qh) == sg.REASON_QUIET,
       "安静时段拦截")
expect(sg.decide(st3, datetime(2026, 9, 29, 9, 0), quiet_hours=qh) == sg.OK, "白天放行")

# --- 最近去重 ---
st4 = sg.GateState()
st4.remember_recent("x", 3)
expect(st4.seen_recently("x"), "remember 后 seen")
st4.remember_recent("y", 3)
st4.remember_recent("z", 3)
st4.remember_recent("w", 3)
expect(len(st4.recent) == 3 and "x" not in st4.recent, "超出 keep 容量淘汰最旧")
st5 = sg.GateState()
st5.remember_recent("k", 0)
expect(not st5.seen_recently("k"), "dedup_keep=0 关闭去重")

print("test_gate: all OK")
