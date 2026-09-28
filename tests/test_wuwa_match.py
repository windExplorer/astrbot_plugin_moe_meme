"""wuwa_source 纯函数自检（角色聚合 / 查询解析）。

运行：uv run --no-project --with aiohttp python tests/test_wuwa_match.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("wuwa_source", ROOT / "wuwa_source.py")
ws = importlib.util.module_from_spec(_spec)
# dataclass 解析字符串化注解时会查 sys.modules，必须先注册
sys.modules["wuwa_source"] = ws
_spec.loader.exec_module(ws)


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


counts = {
    "爱弥斯": 1448,
    "卡提希娅": 906,
    "今汐": 571,
    "漂泊者": 584,
    "秧秧": 460,
    "椿": 447,
}

# 精确匹配
expect(ws.resolve_character("爱弥斯", counts, {}) == "爱弥斯", "全名精确")
expect(ws.resolve_character("爱弥斯", counts, None) == "爱弥斯", "无别名表可用")
expect(ws.resolve_character(" 爱弥斯 ", counts, {}) == "爱弥斯", "去首尾空白")
expect(ws.resolve_character("aimisi", counts, {}) is None, "未知 ASCII 不乱猜（由调用方直传站方试 slug）")

# 别名
expect(ws.resolve_character("小爱", counts, {"小爱": "爱弥斯"}) == "爱弥斯", "别名精确")
expect(ws.resolve_character("小椿", counts, {"小椿": "椿"}) == "椿", "别名生效")
expect(ws.resolve_character("小爱", counts, {"小爱": "不存在角色"}) == "不存在角色",
       "别名目标不在缓存中仍按别名返回（由站方接口裁决）")

# 模糊包含
expect(ws.resolve_character("弥斯", counts, {}) == "爱弥斯", "包含唯一命中")
expect(ws.resolve_character("娅", counts, {}) == "卡提希娅", "包含唯一命中（娅）")
amb = {"今汐A": 100, "今汐B": 200}
expect(ws.resolve_character("今汐", amb, {}) == "今汐B", "多命中取张数最多")
expect(ws.resolve_character("不存在的角色", counts, {}) is None, "完全无命中返回 None")
expect(ws.resolve_character("", counts, {}) is None, "空输入返回 None")
expect(ws.resolve_character("爱弥斯", None, None) is None, "无缓存数据时只靠别名")

# 别名优先于包含（别名是用户显式定义的，优先级最高）
expect(ws.resolve_character("小汐", {"今汐": 571}, {"小汐": "秧秧"}) == "秧秧", "别名优先于包含匹配")

# --- aggregate_characters ---
items = [
    {"characters": ["爱弥斯", "今汐"], "imageCount": 10},
    {"characters": ["爱弥斯"], "imageCount": 5},
    {"characters": [], "imageCount": 7},
    {"characters": [""], "imageCount": 3},
    {"characters": ["椿"], "imageCount": "x"},  # 非法张数按 0 处理
]
agg = ws.aggregate_characters(items)
expect(agg == {"爱弥斯": 15, "今汐": 10, "椿": 0}, "聚合求和")
expect(ws.aggregate_characters(None) == {}, "None 输入")
expect(ws.aggregate_characters([]) == {}, "空列表输入")

print("test_wuwa_match: all OK")
