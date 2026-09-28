"""萌萌表情包 — 「表情包仓鼠库」(emoji.wuwa.games) 数据源。

站方为鸣潮玩家自发众筹、共同整理的非官方表情包项目（CC BY-NC-SA，仅供个人非商业使用）。
本源只做「现拉现转发」的随机引用：随机接口返回带票券的临时图片链（约 16 分钟有效），
必须立即发送、不可缓存，更不能把图片落盘再分发。

除 aiohttp 外不依赖 AstrBot，聚合/匹配等纯函数可独立测试。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp

API_BASE = "https://emoji.wuwa.games/apis/api.random-emoji.wuwa.games/v1alpha1"
RANDOM_URL = f"{API_BASE}/random"
# 站方角色图鉴页自用的数据接口：一次返回全量表情包（含 characters 字段），
# 插件侧聚合出角色名 → 张数表，用于 /表情包 列表 与用户输入的模糊匹配。
ARCHIVE_INDEX_URL = (
    "https://emoji.wuwa.games/apis/api.emoji.jaspin.top/v1alpha1/archive-index"
)
USER_AGENT = (
    "astrbot_plugin_moe_meme/0.1.0 (AstrBot plugin; "
    "+https://github.com/windExplorer/astrbot_plugin_moe_meme)"
)

CHAR_INDEX_TTL = 24 * 3600.0


class SourceError(Exception):
    """数据源错误。str(e) 为可直接展示给用户的中文文案；code 供调用方分支。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Sticker:
    url: str
    character: str  # 站方中文全名
    character_slug: str
    fmt: str  # png / gif
    animated: bool
    sticker_id: str


def aggregate_characters(items) -> dict[str, int]:
    """archive-index 响应的 items → {角色名: 表情张数}。"""
    counts: dict[str, int] = {}
    for it in items or []:
        try:
            n = int(it.get("imageCount") or 0)
        except (TypeError, ValueError):
            n = 0
        for name in it.get("characters") or []:
            if isinstance(name, str) and name.strip():
                counts[name] = counts.get(name, 0) + n
    return counts


def resolve_character(
    query: str, char_counts: dict[str, int] | None, aliases: dict[str, str] | None
) -> str | None:
    """把用户输入解析成站方角色名（站方接口只认全名/slug 精确匹配，模糊在插件侧做）。

    优先级：别名精确 → 角色全名精确 → 包含匹配（多个命中取张数最多者）。
    解析不出返回 None，调用方可把原文直接交给站方接口（slug / 精确名兜底）。
    """
    q = (query or "").strip()
    if not q:
        return None
    aliases = aliases or {}
    if q in aliases:
        return aliases[q]
    if char_counts:
        if q in char_counts:
            return q
        ql = q.casefold()
        # slug 无法从 archive-index 拿到，纯 ASCII 输入交给站方接口精确匹配
        contains = [n for n in char_counts if ql in n.casefold()]
        if contains:
            return max(contains, key=lambda n: char_counts.get(n, 0))
    return None


class WuwaSource:
    """emoji.wuwa.games 随机表情 + 角色列表（带 TTL 缓存）。"""

    def __init__(self, token: str = "", timeout_total: float = 15.0):
        self._token = (token or "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout_total)
        self._session: aiohttp.ClientSession | None = None
        self._char_counts: dict[str, int] | None = None
        self._char_ts: float = 0.0
        self._char_lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"User-Agent": USER_AGENT}
            if self._token:
                # 站方未公布 Token 携带方式；按 Bearer 头实现，配置留空即匿名。
                headers["Authorization"] = f"Bearer {self._token}"
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers=headers
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def random_sticker(self, character: str | None = None) -> Sticker:
        """随机取一张表情；character 传站方全名或 slug（精确匹配），None = 全库随机。"""
        params = {}
        name = (character or "").strip()
        if name:
            params["character"] = name
        session = await self._ensure_session()
        try:
            async with session.get(RANDOM_URL, params=params) as resp:
                if resp.status == 404:
                    code = "CHARACTER_EMPTY"
                    try:
                        body = await resp.json(content_type=None)
                        if isinstance(body, dict) and body.get("code"):
                            code = str(body["code"])
                    except Exception:
                        pass
                    if code == "CHARACTER_EMPTY":
                        raise SourceError("CHARACTER_EMPTY", "角色不存在或没有公开表情")
                    raise SourceError(code, f"站方接口返回 {resp.status}")
                if resp.status != 200:
                    raise SourceError(f"HTTP_{resp.status}", f"站方接口返回 {resp.status}")
                data = await resp.json(content_type=None)
        except SourceError:
            raise
        except asyncio.TimeoutError:
            raise SourceError("timeout", "站方接口请求超时") from None
        except aiohttp.ClientError as e:
            raise SourceError("network", f"网络异常（{e.__class__.__name__}）") from e

        if not isinstance(data, dict) or not data.get("url"):
            raise SourceError("bad_payload", "站方返回数据缺图片地址")
        ch = data.get("character") or {}
        return Sticker(
            url=str(data["url"]),
            character=str(ch.get("name") or ""),
            character_slug=str(ch.get("slug") or ""),
            fmt=str(data.get("format") or ""),
            animated=bool(data.get("animated")),
            sticker_id=str(data.get("id") or ""),
        )

    async def characters(self, force: bool = False) -> dict[str, int]:
        """角色名 → 张数（archive-index 聚合，TTL 24h）。失败抛 SourceError。"""
        now = time.monotonic()
        if (
            not force
            and self._char_counts is not None
            and now - self._char_ts < CHAR_INDEX_TTL
        ):
            return self._char_counts
        async with self._char_lock:
            again = time.monotonic()
            if (
                not force
                and self._char_counts is not None
                and again - self._char_ts < CHAR_INDEX_TTL
            ):
                return self._char_counts
            session = await self._ensure_session()
            try:
                async with session.get(ARCHIVE_INDEX_URL) as resp:
                    if resp.status != 200:
                        raise SourceError(
                            f"HTTP_{resp.status}", f"角色列表接口返回 {resp.status}"
                        )
                    data = await resp.json(content_type=None)
            except SourceError:
                raise
            except asyncio.TimeoutError:
                raise SourceError("timeout", "角色列表请求超时") from None
            except aiohttp.ClientError as e:
                raise SourceError("network", f"网络异常（{e.__class__.__name__}）") from e

            counts = aggregate_characters(
                data.get("items") if isinstance(data, dict) else None
            )
            if not counts:
                # 站方结构变化时保留旧缓存，别把能用的数据清掉
                if self._char_counts:
                    return self._char_counts
                raise SourceError("bad_payload", "角色列表为空，站方接口可能已变更")
            self._char_counts = counts
            self._char_ts = time.monotonic()
            return counts
