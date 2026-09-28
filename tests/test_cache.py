"""sticker_cache 纯函数自检。运行：uv run --no-project python tests/test_cache.py"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sticker_cache", ROOT / "sticker_cache.py")
sc = importlib.util.module_from_spec(_spec)
sys.modules["sticker_cache"] = sc
_spec.loader.exec_module(sc)


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


tmp = Path(tempfile.mkdtemp(prefix="moe_meme_cache_test_"))
try:
    # --- safe_id ---
    expect(sc.safe_id("r-49d83f0f72a655d2cd70") == "r-49d83f0f72a655d2cd70", "常规 id 不变")
    expect(sc.safe_id("bad/id with 空格") == "bad_id_with", "非法字符（连续的合并）替换为下划线并去首尾")
    expect(sc.safe_id("") == "", "空 id → 空串")
    expect(sc.safe_id("///") == "", "全非法字符 → 空串")
    long_id = "a" * 200
    expect(len(sc.safe_id(long_id)) == 80, "超长 id 截断")

    # --- path_for ---
    p = sc.path_for(tmp, "r-abc", "gif")
    expect(p is not None and p.name == "r-abc.gif", "按 fmt 定扩展名")
    expect(sc.path_for(tmp, "r-abc", "").name == "r-abc.img", "无 fmt 回退 .img")
    expect(sc.path_for(tmp, "r-abc", "exe/x").name == "r-abc.img", "非法 fmt 回退 .img")
    expect(sc.path_for(tmp, " ", "") is None, "纯空白 id → None")

    # --- store / find_existing 往返 ---
    stored = sc.store(tmp, "r-abc", "gif", b"\x00gifdata")
    expect(stored is not None and stored.exists(), "store 落盘")
    expect(stored.read_bytes() == b"\x00gifdata", "内容一致")
    found = sc.find_existing(tmp, "r-abc")
    expect(found == stored, "find_existing 命中")
    expect(sc.find_existing(tmp, "r-notexist") is None, "未缓存返回 None")
    expect(sc.store(tmp, " ", "", b"x") is None, "非法 id 拒绝写入")
    # 覆盖写（同 id 再次下载）
    again = sc.store(tmp, "r-abc", "gif", b"newdata")
    expect(again is not None and again.read_bytes() == b"newdata", "同 id 覆盖写")

    # --- prune 按最旧淘汰（独立目录，排除前面 store 的文件干扰） ---
    pr = tmp / "prune"
    pr.mkdir()
    for i in range(5):
        f = pr / f"p{i}.gif"
        f.write_bytes(b"x")
        stamp = time.time() + i
        os.utime(f, (stamp, stamp))  # p0 最旧, p4 最新
    removed = sc.prune(pr, 3)
    expect(removed == 2, f"淘汰 2 张（实际 {removed}）")
    expect((pr / "p0.gif").exists() is False, "最旧的 p0 被删")
    expect((pr / "p4.gif").exists(), "最新的 p4 保留")
    expect(sc.prune(pr, 0) == 0, "max_files=0 不淘汰")
    expect(sc.prune(pr, 999) == 0, "未超上限不淘汰")

    # --- 目录不存在时不抛异常 ---
    missing = tmp / "no_such_dir"
    expect(sc.find_existing(missing, "x") is None, "目录缺失 find_existing 安全")
    expect(sc.prune(missing, 10) == 0, "目录缺失 prune 安全")

    print("test_cache: all OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
