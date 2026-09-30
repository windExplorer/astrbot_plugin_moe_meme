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
import os
import random
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageChain,
    ResultContentType,
    filter,
)
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.event_message_type import EventMessageType

from . import sticker_cache, sticker_gate, wuwa_source

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
        global sticker_gate, wuwa_source, sticker_cache
        try:
            for _dep_name in ("sticker_cache", "sticker_gate", "wuwa_source"):
                try:
                    _mod = importlib.import_module(f"{__package__}.{_dep_name}")
                    importlib.reload(_mod)
                except Exception as _dep_err:
                    logger.warning(
                        f"[萌萌表情包] {_dep_name} 强制重载失败（沿用已加载模块）: {_dep_err}"
                    )
            from . import sticker_cache as _sc, sticker_gate as _sg, wuwa_source as _ws

            sticker_cache, sticker_gate, wuwa_source = _sc, _sg, _ws
        except Exception as _reload_err:
            logger.warning(
                f"[萌萌表情包] 依赖模块重载流程异常（沿用已加载模块）: {_reload_err}"
            )

        self.config = config
        self.source: "wuwa_source.WuwaSource | None" = None
        # 会话 UMO -> 闸门状态（指令去重 + 主动发送冷却/限额，全部内存态，重启清零即可）
        self._gate_states: dict[str, "sticker_gate.GateState"] = {}
        # 本地表情缓存：目录在 initialize() 建；id -> 文件路径 的内存索引
        self.cache_dir: "Path | None" = None
        self._sticker_files: dict[str, str] = {}
        # 回复概率跟图在本事件上的暂存键（on_llm_response 写，decorating/after_sent 消费）
        self._reply_extra = "moe_meme_reply_sticker"
        # 跟图「未发送原因」的会话级记录：同一原因只打一条 INFO，免得每条回复都刷屏
        self._skip_notes: dict[str, str] = {}

    async def initialize(self) -> None:
        """生命周期钩子：异常会导致整个插件加载失败，这里全程兜底。"""
        try:
            token = str((self._cfg() or {}).get("api_token") or "").strip()
            self.source = wuwa_source.WuwaSource(token=token)
        except Exception as e:
            self.source = None
            logger.error(f"[萌萌表情包] 数据源初始化失败（指令将不可用）: {e}")
        try:
            # 走 StarTools 的 data/plugin_data/<名>/，插件更新被 remove_dir 也不会清掉缓存
            self.cache_dir = Path(StarTools.get_data_dir("astrbot_plugin_moe_meme")) / "cache"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.cache_dir = None
            logger.warning(f"[萌萌表情包] 本地缓存目录不可用（将直发票券链接）: {e}")

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

    def _cache_max_files(self) -> int:
        try:
            raw = self._cfg().get("cache_max_files")
            return 500 if raw is None else max(0, int(raw))
        except (TypeError, ValueError):
            return 500

    async def _acquire_local(self, stk: "wuwa_source.Sticker") -> "str | None":
        """把表情落到本地缓存并返回文件路径；失败返回 None（调用方回退直发票券链）。

        站方图片链是约 16 分钟有效的临时票券，落盘后发送更稳更快；
        仅作本机器人发送用的运行时缓存（更新插件不清掉，超过上限自动淘汰最旧）。
        """
        if self.cache_dir is None:
            return None
        sid = stk.sticker_id
        cached = self._sticker_files.get(sid)
        if cached and os.path.isfile(cached):
            return cached
        existing = sticker_cache.find_existing(self.cache_dir, sid)
        if existing is not None:
            self._sticker_files[sid] = str(existing)
            return str(existing)
        try:
            data = await self.source.download(stk)
        except Exception as e:
            logger.warning(f"[萌萌表情包] 本地缓存下载失败（回退直发链接）: {e}")
            return None
        path = sticker_cache.store(self.cache_dir, sid, stk.fmt, data)
        if path is None:
            return None
        if len(self._sticker_files) > 2000:
            self._sticker_files.clear()  # 内存索引防膨胀；文件还在，find_existing 能找回
        self._sticker_files[sid] = str(path)
        try:
            sticker_cache.prune(self.cache_dir, self._cache_max_files())
        except Exception as e:
            logger.warning(f"[萌萌表情包] 缓存淘汰失败: {e}")
        return str(path)

    def _image_component(self, stk: "wuwa_source.Sticker", path: "str | None") -> Image:
        """优先本地文件，缓存不可用时回退站方票券链。"""
        if path:
            return Image.fromFileSystem(path)
        return Image.fromURL(stk.url)

    # ---------- 指令回执：直发（绕开核心的「回复时 @ 发送者」） ----------
    #
    # 为什么指令回执不用 `yield event.plain_result(...)` / `event.image_result(...)`：
    # AstrBot 的 `ResultDecorateStage` 会给「只含 Plain / Image 的结果链」在最前面插一个
    # `At(发送者)`，开关是全局的 `platform_settings.reply_with_mention`（群聊里开了就每条都 @）。
    # 表情包回执恰好就是一张 Image —— 于是每条指令回图都自带一个 @；而插 At 发生在所有插件
    # `on_decorating_result` 钩子之后，插件撤不掉，只能绕开：直发（`event.send`）不进结果链。
    # 顺带的好处：`_has_send_oper` 置位，本轮不会再触发一次默认 LLM 回复。
    # 代价：结果链上的「长文本转图 / 分段回复 / TTS / 回复前缀」等装饰对指令回执不再生效
    # （长列表会原样发文字），是否 @ 触发者改由本插件的 `at_sender` 决定。
    # 同工作区 `astrbot_plugin_model_panel` 的指令走的就是这一套。

    @staticmethod
    async def _send_text(event: AstrMessageEvent, text: str) -> bool:
        """直发一条文本回执。失败只记日志，不回抛（别让回执带崩整轮流程）。"""
        try:
            await event.send(MessageChain(chain=[Plain(str(text))]))
            return True
        except Exception as e:
            logger.warning(f"[萌萌表情包] 文本直发失败（忽略）: {e}")
            return False

    @staticmethod
    async def _send_chain(event: AstrMessageEvent, chain: list) -> bool:
        """直发一个消息链（表情图用）。"""
        try:
            await event.send(MessageChain(chain=chain))
            return True
        except Exception as e:
            logger.error(f"[萌萌表情包] 消息直发失败: {e}")
            return False

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
            await self._manage_lists(event, head, parts[1] if len(parts) > 1 else "")
            return

        if not self._whitelist_ok(umo):
            return  # 白名单外静默忽略，不暴露指令存在

        if head in ("帮助", "help", "菜单"):
            await self._send_text(event, HELP_TEXT)
            return

        if head in ("列表", "list", "角色"):
            await self._send_text(event, await self._list_text())
            return

        if self.source is None:
            await self._send_text(event, "表情包源还没初始化好，稍后再试。")
            return

        character = None
        if text:
            resolved = await self._resolve(text)
            character = resolved or text  # 解析不出就按原文试（slug / 站方精确名）
        try:
            stk = await self._fetch_sticker(character, umo)
        except wuwa_source.SourceError as e:
            if e.code == "CHARACTER_EMPTY":
                await self._send_text(event, f"没找到「{text}」的表情包，试试 /表情包 列表")
            else:
                await self._send_text(event, f"表情包拉取失败：{e}")
            return
        except Exception as e:
            logger.error(f"[萌萌表情包] 指令拉取异常: {e}")
            await self._send_text(event, "表情包拉取失败，稍后再试。")
            return
        self._gate_state(umo).remember_recent(stk.sticker_id, self._dedup_keep())
        path = await self._acquire_local(stk)
        chain: list = [self._image_component(stk, path)]
        if self._cfg().get("at_sender", False):
            # 可选 @ 触发者（默认不 @）：把 At 组件插到图前面
            chain.insert(0, At(qq=event.get_sender_id(), name=event.get_sender_name()))
        if not await self._send_chain(event, chain):
            await self._send_text(event, "表情包发送失败，稍后再试。")

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
            await self._send_text(event, "该操作仅 AstrBot 管理员可用。")
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
                await self._send_text(event, f"本会话已在{label}列表中。")
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
            await self._send_text(event, f"已开启本会话的{label}。{extra}")
            return
        if action in ("关闭", "移除", "off", "disable"):
            if not now_in:
                await self._send_text(event, f"本会话本就不在{label}列表中。")
                return
            lst.remove(umo)
            self.config[key] = lst
            self._save()
            await self._send_text(event, f"已关闭本会话的{label}。")
            return
        state = "已开启" if now_in else "未开启"
        await self._send_text(
            event, f"本会话{label}：{state}。用「/表情包 {which} 开启|关闭」修改。"
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
            path = await self._acquire_local(stk)
            await event.send(MessageChain(chain=[self._image_component(stk, path)]))
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
            await self._keyword_tick(event)
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
            path = await self._acquire_local(stk)
            await event.send(MessageChain(chain=[self._image_component(stk, path)]))
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

    # ---------- 主动发送 3：回复概率跟图（bot 每次回复按概率决定是否带一张表情） ----------
    #
    # 交付路径（三条，取决于这一轮回复长什么样）：
    #   1. 普通回复 → on_decorating_result 把图追加到回复链末尾（文字 + 图一条消息）；
    #   2. 流式回复 → 文本已随流发完，最终那个 STREAMING_FINISH 结果 respond 阶段会直接丢弃，
    #      追加等于丢掉，所以改在 on_decorating_result 里单独补发（顺序天然在回复之后）；
    #   3. 本轮回复会被核心「长文本转图」整体替换成一张渲染图时 → 不追加，
    #      留给 after_message_sent 补发（那时连渲染图都已经发出去了）。
    # 其余原因导致不发送，都会留一条 INFO（同一会话同一原因只记一次）。

    @filter.on_llm_response()
    async def on_llm_reply(self, event: AstrMessageEvent, response=None):
        """LLM 回复完成后按概率决定是否跟一张表情；命中则暂存到事件上等交付钩子消费。"""
        try:
            await self._reply_sticker_tick(event)
        except Exception as e:
            logger.warning(f"[萌萌表情包] 回复跟图判定异常（已忽略）: {e}")

    def _note_skip(self, umo: str, reason: str, text: str) -> None:
        """记录「跟图没发」的原因：同会话同原因只打一条 INFO（换原因会再打）。"""
        if self._skip_notes.get(umo) == reason:
            return
        self._skip_notes[umo] = reason
        if len(self._skip_notes) > 500:
            self._skip_notes.clear()
        logger.info(f"[萌萌表情包] 回复跟图未发送：{text}")

    def _gate_reason_text(self, reason: str, sub: dict) -> str:
        if reason == sticker_gate.REASON_COOLDOWN:
            return (
                f"撞上会话冷却（本项 {self._int(sub, 'cooldown_minutes', 10)} 分钟）——"
                "冷却与每日上限是「LLM 工具 / 关键词触发 / 回复跟图」三类共用一个计数，"
                "想每次回复都跟图可把本项冷却设为 0"
            )
        if reason == sticker_gate.REASON_DAILY_CAP:
            return (
                f"当日主动表情已达上限（本项 {self._int(sub, 'daily_cap', 20)} 张，"
                "三类主动共用一个计数）"
            )
        if reason == sticker_gate.REASON_QUIET:
            return f"现在是安静时段（本项 {sub.get('quiet_hours') or '未设置'}）"
        return str(reason)

    async def _reply_sticker_tick(self, event: AstrMessageEvent) -> None:
        sub = self._sub("reply_sticker")
        if not sub.get("enable", False):
            return  # 功能没开，不打扰用户
        umo = event.unified_msg_origin
        if not self._in_proactive(umo):
            self._note_skip(
                umo,
                "not_proactive",
                "本会话不在「主动发送」列表里（跟图只对列表内会话生效），"
                "管理员发 /表情包 主动 开启 即可",
            )
            return
        try:
            prob = float(sub.get("probability", 0.1))
        except (TypeError, ValueError):
            prob = 0.1
        if random.random() >= max(0.0, min(1.0, prob)):
            logger.debug(f"[萌萌表情包] 回复跟图未命中概率（probability={prob}）")
            return
        reason = self._gate_check(umo, sub)
        if reason:
            self._note_skip(umo, reason, self._gate_reason_text(reason, sub))
            return
        if self.source is None:
            self._note_skip(umo, "no_source", "表情包源不可用（看插件初始化日志）")
            return
        char = str(sub.get("character") or "").strip()
        try:
            resolved = await self._resolve(char) if char else None
            stk = await self._fetch_sticker(resolved or (char or None), umo)
        except Exception as e:
            logger.warning(f"[萌萌表情包] 回复跟图拉取失败: {e}")
            return
        path = await self._acquire_local(stk)
        if not path and not stk.url:
            return
        event.set_extra(
            self._reply_extra, {"path": path, "url": stk.url, "id": stk.sticker_id}
        )
        self._skip_notes.pop(umo, None)
        logger.debug(f"[萌萌表情包] 回复跟图已就绪：{stk.character or '随机'}")

    def _reply_sticker_component(self, payload: dict) -> Image:
        if payload.get("path"):
            return Image.fromFileSystem(payload["path"])
        return Image.fromURL(payload.get("url") or "")

    async def _send_reply_sticker(
        self, event: AstrMessageEvent, payload: dict, *, source: str
    ) -> bool:
        """单独补发一张跟图表情（不走结果链），成功才记账。"""
        if not await self._send_chain(event, [self._reply_sticker_component(payload)]):
            return False
        sticker_gate.record(
            self._gate_state(event.unified_msg_origin),
            datetime.now(),
            sticker_id=str(payload.get("id") or ""),
            dedup_keep=self._dedup_keep(),
        )
        logger.info(f"[萌萌表情包] 回复跟图已发出（{source}）")
        return True

    def _t2i_replaces_chain(self, result) -> bool:
        """预判核心的「长文本转图」会不会把整条结果链换成一张渲染图。

        `ResultDecorateStage` 是在跑完 `on_decorating_result` 钩子**之后**才做 t2i，
        命中就直接 `result.chain = [Image(...)]` —— 追加进去的表情会被一起丢掉。
        这里按同一套判断（开关 + 链首连续 Plain 的渲染长度阈值）提前让路。
        """
        try:
            cfg = self.context.get_config() or {}
            if not ((result.use_t2i_ is None and cfg.get("t2i")) or result.use_t2i_):
                return False
            threshold = max(int(cfg.get("t2i_word_threshold") or 150), 50)
        except Exception:
            return False
        parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                parts.append("\n\n" + comp.text)
            else:
                break
        return len("".join(parts)) > threshold

    @filter.on_decorating_result()
    async def attach_reply_sticker(self, event: AstrMessageEvent):
        """把概率命中的表情交付出去：能并进回复链就并，不能就单独补发。"""
        try:
            payload = event.get_extra(self._reply_extra)
            if not payload:
                return
            result = event.get_result()
            if result is None or not isinstance(getattr(result, "chain", None), list):
                return
            if result.result_content_type == ResultContentType.STREAMING_FINISH:
                # 流式：文本已经发完了，这个结果 respond 阶段不会再发，只能自己补发
                event.set_extra(self._reply_extra, None)
                await self._send_reply_sticker(event, payload, source="流式回复后补发")
                return
            if self._t2i_replaces_chain(result):
                # 不消费 payload：留给 after_message_sent（那时回复渲染图已发出，顺序正确）
                logger.info(
                    "[萌萌表情包] 回复跟图：本轮回复会被「长文本转图」替换，改为回复后补发"
                )
                return
            event.set_extra(self._reply_extra, None)
            result.chain.append(self._reply_sticker_component(payload))
            sticker_gate.record(
                self._gate_state(event.unified_msg_origin),
                datetime.now(),
                sticker_id=str(payload.get("id") or ""),
                dedup_keep=self._dedup_keep(),
            )
            logger.info("[萌萌表情包] 回复跟图已并入回复消息")
        except Exception as e:
            logger.warning(f"[萌萌表情包] 回复跟图挂载失败: {e}")

    @filter.after_message_sent()
    async def send_reply_sticker_fallback(self, event: AstrMessageEvent):
        """回复已发出但没能并进回复链的场景（如长文本转图）：单独补发。"""
        try:
            payload = event.get_extra(self._reply_extra)
            if not payload:
                return
            event.set_extra(self._reply_extra, None)
            await self._send_reply_sticker(event, payload, source="回复后补发")
        except Exception as e:
            logger.warning(f"[萌萌表情包] 回复跟图兜底发送失败: {e}")
