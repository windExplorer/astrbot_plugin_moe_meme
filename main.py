"""萌萌表情包 — 发鸣潮表情包的 AstrBot 插件。

指令：/表情包 [角色 | 列表 | 帮助 | 白名单 开启|关闭 | 主动 开启|关闭]
主动发送：LLM 工具 send_sticker（对话中由模型自主决定）+ 关键词概率触发（默认关）。
数据源：表情包仓鼠库 https://emoji.wuwa.games（CC BY-NC-SA，只现拉现转发，不存储素材）。

白名单语义：whitelist_sessions 非空 → 仅列表内会话响应指令；留空 → 所有会话可用。
主动发送只对显式开启（proactive_sessions）的会话生效，不随白名单「空=全放行」。
"""

# 注意：不要使用 from __future__ import annotations —— AstrBot 按对象同一性识别
# GreedyStr 哨兵（default is GreedyStr），字符串化注解会让「/表情包」无参调用失效。

import importlib
import random
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.event_message_type import EventMessageType

from . import sticker_gate, wuwa_source

HELP_TEXT = (
    "萌萌表情包（数据源：表情包仓鼠库 emoji.wuwa.games，鸣潮玩家众筹整理）\n"
    "/表情包            随机一张\n"
    "/表情包 角色名      指定角色，如 /表情包 爱弥斯（支持全名/英文id/别名/模糊）\n"
    "/表情包 列表        查看可用角色\n"
    "/表情包 白名单 开启|关闭   管理员：本会话是否可用表情包指令\n"
    "/表情包 主动 开启|关闭     管理员：本会话是否允许主动发表情"
)

_GATE_REASON_TEXT = {
    sticker_gate.REASON_QUIET: "现在是安静时段，先不发表情啦。",
    sticker_gate.REASON_COOLDOWN: "刚刚才发过表情包，先歇一歇。",
    sticker_gate.REASON_DAILY_CAP: "今天的表情包份额用完啦。",
}


@register(
    "astrbot_plugin_moe_meme",
    "windExplorer",
    "萌萌表情包：/表情包 随机或按角色发鸣潮表情，支持主动发送",
    "v0.1.0",
)
class MoeMemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        # AstrBot 热更新只重载 main.py，依赖子模块会残留在 sys.modules 里继续跑旧代码。
        # 这里按依赖顺序（被依赖者在前）强制重载并重新绑定全局名（同 box/anima 的做法）。
        global sticker_gate, wuwa_source
        try:
            for _dep_name in ("sticker_gate", "wuwa_source"):
                try:
                    _mod = importlib.import_module(f"{__package__}.{_dep_name}")
                    importlib.reload(_mod)
                except Exception as _dep_err:
                    logger.warning(
                        f"[萌萌表情包] {_dep_name} 强制重载失败（沿用已加载模块）: {_dep_err}"
                    )
            from . import sticker_gate as _sg, wuwa_source as _ws

            sticker_gate, wuwa_source = _sg, _ws
        except Exception as _reload_err:
            logger.warning(
                f"[萌萌表情包] 依赖模块重载流程异常（沿用已加载模块）: {_reload_err}"
            )

        self.config = config
        self.source: "wuwa_source.WuwaSource | None" = None
        # 会话 UMO -> 闸门状态（指令去重 + 主动发送冷却/限额，全部内存态，重启清零即可）
        self._gate_states: dict[str, "sticker_gate.GateState"] = {}

    async def initialize(self) -> None:
        """生命周期钩子：异常会导致整个插件加载失败，这里全程兜底。"""
        try:
            token = str((self._cfg() or {}).get("api_token") or "").strip()
            self.source = wuwa_source.WuwaSource(token=token)
        except Exception as e:
            self.source = None
            logger.error(f"[萌萌表情包] 数据源初始化失败（指令将不可用）: {e}")

    async def terminate(self) -> None:
        if self.source is not None:
            try:
                await self.source.close()
            except Exception:
                pass

    # ---------- 配置读取（每次现读，管理页改完即时生效） ----------

    def _cfg(self) -> dict:
        return self.config if isinstance(self.config, dict) else {}

    def _sub(self, key: str) -> dict:
        v = self._cfg().get(key)
        return v if isinstance(v, dict) else {}

    @staticmethod
    def _int(sub: dict, key: str, default: int) -> int:
        try:
            return int(sub.get(key, default))
        except (TypeError, ValueError):
            return default

    def _dedup_keep(self) -> int:
        try:
            raw = self._cfg().get("recent_dedup")
            return 20 if raw is None else max(0, int(raw))
        except (TypeError, ValueError):
            return 20

    def _aliases(self) -> dict:
        raw = self._cfg().get("aliases")
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            k, v = str(k).strip(), str(v).strip()
            if k and v:
                out[k] = v
        return out

    def _whitelist_ok(self, umo: str) -> bool:
        lst = self._cfg().get("whitelist_sessions")
        if not isinstance(lst, list) or not lst:
            return True  # 留空 = 所有会话可用
        return umo in lst

    def _in_proactive(self, umo: str) -> bool:
        lst = self._cfg().get("proactive_sessions")
        return isinstance(lst, list) and umo in lst

    def _save(self) -> None:
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[萌萌表情包] 配置保存失败: {e}")

    # ---------- 闸门与取图 ----------

    def _gate_state(self, umo: str) -> "sticker_gate.GateState":
        st = self._gate_states.get(umo)
        if st is None:
            st = sticker_gate.GateState()
            self._gate_states[umo] = st
        return st

    def _gate_check(self, umo: str, sub: dict) -> str:
        return sticker_gate.decide(
            self._gate_state(umo),
            datetime.now(),
            cooldown_minutes=self._int(sub, "cooldown_minutes", 10),
            daily_cap=self._int(sub, "daily_cap", 20),
            quiet_hours=sticker_gate.parse_quiet_hours(str(sub.get("quiet_hours") or "")),
        )

    async def _fetch_sticker(self, character: str | None, umo: str):
        """拉一张表情；命中会话内最近已发过的 id 时最多重试 2 次（去重）。"""
        st = self._gate_state(umo)
        keep = self._dedup_keep()
        last = None
        for _ in range(3):
            stk = await self.source.random_sticker(character)
            if keep <= 0 or not st.seen_recently(stk.sticker_id):
                return stk
            last = stk
        return last  # 连续三次都撞上去重就发这张，别死循环

    async def _resolve(self, query: str) -> str | None:
        """别名/模糊解析成站方角色名；列表接口失败时返回 None（原文直传站方）。"""
        try:
            counts = await self.source.characters()
        except wuwa_source.SourceError:
            counts = None
        return wuwa_source.resolve_character(query, counts, self._aliases())

    # ---------- 指令 ----------

    @filter.command("表情包")
    async def moe_meme(self, event: AstrMessageEvent, query: GreedyStr = GreedyStr):
        """发表情包：/表情包 随机一张；/表情包 爱弥斯 按角色；/表情包 列表 看角色"""
        umo = event.unified_msg_origin
        text = "" if query is GreedyStr else str(query or "")
        text = text.strip()
        parts = text.split()
        head = parts[0] if parts else ""

        # 会话管理子指令不受白名单限制：否则白名单外的管理员没法把自己加进来
        if head in ("白名单", "主动"):
            async for r in self._manage_lists(event, head, parts[1] if len(parts) > 1 else ""):
                yield r
            return

        if not self._whitelist_ok(umo):
            return  # 白名单外静默忽略，不暴露指令存在

        if head in ("帮助", "help", "菜单"):
            yield event.plain_result(HELP_TEXT)
            return

        if head in ("列表", "list", "角色"):
            yield event.plain_result(await self._list_text())
            return

        if self.source is None:
            yield event.plain_result("表情包源还没初始化好，稍后再试。")
            return

        character = None
        if text:
            resolved = await self._resolve(text)
            character = resolved or text  # 解析不出就按原文试（slug / 站方精确名）
        try:
            stk = await self._fetch_sticker(character, umo)
        except wuwa_source.SourceError as e:
            if e.code == "CHARACTER_EMPTY":
                yield event.plain_result(f"没找到「{text}」的表情包，试试 /表情包 列表")
            else:
                yield event.plain_result(f"表情包拉取失败：{e}")
            return
        except Exception as e:
            logger.error(f"[萌萌表情包] 指令拉取异常: {e}")
            yield event.plain_result("表情包拉取失败，稍后再试。")
            return
        self._gate_state(umo).remember_recent(stk.sticker_id, self._dedup_keep())
        yield event.image_result(stk.url)

    async def _list_text(self) -> str:
        try:
            counts = await self.source.characters()
        except wuwa_source.SourceError as e:
            return f"角色列表拉取失败：{e}"
        names = sorted(counts.items(), key=lambda kv: -kv[1])
        total = sum(counts.values())
        lines = [f"仓库共 {len(names)} 位角色 / {total} 张表情（按张数排序）："]
        row = [f"{name}({cnt})" for name, cnt in names]
        for i in range(0, len(row), 5):
            lines.append("  " + "、".join(row[i : i + 5]))
        lines.append("用法：/表情包 角色名，如 /表情包 爱弥斯")
        return "\n".join(lines)

    async def _manage_lists(self, event: AstrMessageEvent, which: str, action: str):
        if not event.is_admin():
            yield event.plain_result("该操作仅 AstrBot 管理员可用。")
            return
        umo = event.unified_msg_origin
        key = "whitelist_sessions" if which == "白名单" else "proactive_sessions"
        label = "指令白名单" if which == "白名单" else "主动发送"
        lst = self._cfg().get(key)
        lst = list(lst) if isinstance(lst, list) else []
        now_in = umo in lst
        action = (action or "").strip().lower()

        if action in ("开启", "打开", "加入", "on", "enable"):
            if now_in:
                yield event.plain_result(f"本会话已在{label}列表中。")
                return
            was_empty = not lst
            lst.append(umo)
            self.config[key] = lst
            self._save()
            extra = (
                f"注意：白名单此前为空（原本所有会话可用），现在只有列表内会话能用指令了。"
                if which == "白名单" and was_empty
                else ""
            )
            yield event.plain_result(f"已开启本会话的{label}。{extra}")
            return
        if action in ("关闭", "移除", "off", "disable"):
            if not now_in:
                yield event.plain_result(f"本会话本就不在{label}列表中。")
                return
            lst.remove(umo)
            self.config[key] = lst
            self._save()
            yield event.plain_result(f"已关闭本会话的{label}。")
            return
        state = "已开启" if now_in else "未开启"
        yield event.plain_result(
            f"本会话{label}：{state}。用「/表情包 {which} 开启|关闭」修改。"
        )

    # ---------- 主动发送 1：LLM 工具（对话中由模型自主决定） ----------

    @filter.llm_tool()
    async def send_sticker(self, event: AstrMessageEvent, character: str = "", mood: str = ""):
        """在聊天氛围合适时发送一个鸣潮表情包活跃气氛。仅在对话轻松、玩梗、搞笑、开心或惊讶时使用；每次回复最多调用一次；拿不准就不要调用。

        Args:
            character(string): 表情包角色名（如 爱弥斯、今汐、秧秧），无关紧要或不确定时传空字符串。
            mood(string): 当前聊天情绪（如 搞笑、感动、惊讶），用于记录，可传空字符串。
        """
        umo = event.unified_msg_origin
        sub = self._sub("llm_sticker")
        if not sub.get("enable", True):
            yield "本插件未启用「LLM 自主发表情」，本次不发送。"
            return
        if not self._in_proactive(umo):
            yield "当前会话未开启主动表情包（管理员可发 /表情包 主动 开启），本次不发送。"
            return
        reason = self._gate_check(umo, sub)
        if reason:
            yield _GATE_REASON_TEXT.get(reason, "现在不发表情。")
            return
        if self.source is None:
            yield "表情包源不可用，请自然地继续对话，不要重试。"
            return
        q = (character or "").strip()
        try:
            resolved = await self._resolve(q) if q else None
            stk = await self._fetch_sticker(resolved or (q or None), umo)
        except wuwa_source.SourceError as e:
            yield f"表情包获取失败（{e}），请自然地继续对话，不要重试。"
            return
        except Exception as e:
            logger.error(f"[萌萌表情包] llm 工具拉取异常: {e}")
            yield "表情包获取失败，请自然地继续对话，不要重试。"
            return
        try:
            await event.send(MessageChain(chain=[Image.fromURL(stk.url)]))
        except Exception as e:
            logger.error(f"[萌萌表情包] llm 工具发送失败: {e}")
            yield "表情包发送失败，请忽略并继续对话。"
            return
        # 发送成功才记账（冷却/限额/去重）
        sticker_gate.record(
            self._gate_state(umo),
            datetime.now(),
            sticker_id=stk.sticker_id,
            dedup_keep=self._dedup_keep(),
        )
        who = stk.character or "随机"
        logger.info(f"[萌萌表情包] llm 工具发送表情：{who}（mood={mood or '未注明'}）")
        yield f"已发送「{who}」的表情包。请自然地继续对话。"

    # ---------- 主动发送 2：关键词概率触发（默认关） ----------

    @filter.event_message_type(EventMessageType.ALL, priority=-100)
    async def on_keyword(self, event: AstrMessageEvent):
        """低优先级监听全部消息做关键词触发；纯协程不 yield，绝不打断消息管道。"""
        try:
            self._keyword_tick(event)
        except Exception as e:
            logger.warning(f"[萌萌表情包] 关键词触发异常（已忽略）: {e}")

    async def _keyword_tick(self, event: AstrMessageEvent) -> None:
        sub = self._sub("keyword_sticker")
        if not sub.get("enable", False):
            return
        umo = event.unified_msg_origin
        if not self._in_proactive(umo):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        triggers = sub.get("triggers")
        triggers = triggers if isinstance(triggers, dict) else {}
        hit_char, hit_len = None, 0
        for word, ch in triggers.items():
            w = str(word).strip()
            if w and w in text and len(w) > hit_len:
                hit_char, hit_len = (str(ch).strip() or None), len(w)
        if hit_len == 0:
            return
        try:
            prob = float(sub.get("probability", 0.04))
        except (TypeError, ValueError):
            prob = 0.04
        if random.random() >= max(0.0, min(1.0, prob)):
            return
        reason = self._gate_check(umo, sub)
        if reason:
            return
        if self.source is None:
            return
        try:
            resolved = await self._resolve(hit_char) if hit_char else None
            stk = await self._fetch_sticker(resolved or hit_char, umo)
        except Exception as e:
            logger.warning(f"[萌萌表情包] 关键词触发拉取失败: {e}")
            return
        try:
            await event.send(MessageChain(chain=[Image.fromURL(stk.url)]))
        except Exception as e:
            logger.warning(f"[萌萌表情包] 关键词触发发送失败: {e}")
            return
        sticker_gate.record(
            self._gate_state(umo),
            datetime.now(),
            sticker_id=stk.sticker_id,
            dedup_keep=self._dedup_keep(),
        )
        logger.info(f"[萌萌表情包] 关键词触发发送表情：{stk.character or '随机'}")
