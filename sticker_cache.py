"""萌萌表情包 — 本地表情缓存（纯 stdlib，可独立测试）。

把每次取到的表情落盘到 data/plugin_data/astrbot_plugin_moe_meme/cache/，
之后发送走本地文件（比 16 分钟有效期的站方票券链更稳更快）。
仅作本机器人发送用的运行时缓存，不用于二次配布；超过上限按最旧淘汰。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_ID_MAX_LEN = 80


def safe_id(sticker_id: str) -> str:
    """把表情 id 清洗成安全文件名主干；无法安全命名时返回空串。"""
    cleaned = _SAFE_ID.sub("_", (sticker_id or "").strip())[:_ID_MAX_LEN].strip("_")
    return cleaned


def _fallback_id(sticker_id: str) -> str:
    return "id-" + hashlib.md5((sticker_id or "").encode("utf-8")).hexdigest()[:16]


def _name_parts(sticker_id: str) -> str:
    """清洗后的文件名主干；原始 id 去空白后为空 → 空串（视为无 id）。"""
    raw = (sticker_id or "").strip()
    if not raw:
        return ""
    return safe_id(raw) or _fallback_id(raw)


def path_for(cache_dir: Path, sticker_id: str, fmt: str = "") -> Path | None:
    """该表情在缓存中的目标路径；id 不可用返回 None。"""
    sid = _name_parts(sticker_id)
    if not sid:
        return None
    ext = (fmt or "").strip().lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
        ext = "img"
    return Path(cache_dir) / f"{sid}.{ext}"


def find_existing(cache_dir: Path, sticker_id: str) -> Path | None:
    """按 id 找已缓存的任意扩展名文件（重启后内存索引丢失时兜底）。"""
    sid = _name_parts(sticker_id)
    if not sid:
        return None
    try:
        matches = sorted(
            (p for p in Path(cache_dir).glob(f"{sid}.*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return matches[0] if matches else None


def store(cache_dir: Path, sticker_id: str, fmt: str, data: bytes) -> Path | None:
    """原子写入缓存文件（临时文件 + rename）；失败返回 None，不抛异常。"""
    target = path_for(cache_dir, sticker_id, fmt)
    if target is None or not data:
        return None
    tmp = None
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return None


def prune(cache_dir: Path, max_files: int) -> int:
    """按最旧优先淘汰缓存文件到 max_files 张以内；max_files <= 0 表示不限。返回删除数。"""
    if max_files <= 0:
        return 0
    try:
        files = [p for p in Path(cache_dir).iterdir() if p.is_file()]
    except OSError:
        return 0
    if len(files) <= max_files:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # 新 -> 旧
    removed = 0
    for victim in files[max_files:]:
        try:
            victim.unlink()
            removed += 1
        except OSError:
            continue
    return removed
