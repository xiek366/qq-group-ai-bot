"""
统一对话流（P0-1 / P0-2）回归测试
=================================
覆盖：
  1. 追加 + 倒读窗口（顺序、条数上限、跨多个读块的大文件）
  2. 时间标记策略（间隔 <1h 不标 / 1~24h 标"N小时前" / ≥24h 标绝对时间）
  3. 昵称前缀（LLM 要知道谁在说话）
  4. 旧 memory 一次性迁移（顺序保留、旧文件改名 .migrated、不重复迁移）
  5. clear 走备份而不是直接删
  6. 并发写不丢（走 app.util.blocking 的单 worker）
  7. GroupMemory 门面与对话流是同一份数据（写进去能读出来）

运行: python tests/test_conversation_stream.py
（必须在项目根目录运行）
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.paths as paths
from app.context.stream import ConversationStream, _time_marker
from app.util.blocking import run_blocking, shutdown_blocking

TEST_GID = 100000010
TMP = Path("data/_tmp_stream_test")


def _fresh_stream() -> ConversationStream:
    """用临时目录的对话流，别碰真实数据"""
    shutil.rmtree(TMP, ignore_errors=True)
    legacy = TMP / "legacy_memory"
    legacy.mkdir(parents=True, exist_ok=True)
    return ConversationStream(stream_dir=TMP, legacy_memory_dir=legacy)


def test_append_and_window():
    s = _fresh_stream()
    for i in range(5):
        s.append(TEST_GID, "user", f"消息{i}", user_id=1000 + i, nick=f"昵称{i}")
    s.append(TEST_GID, "assistant", "回复", kind="reply")

    win = s.window(TEST_GID, limit=10)
    assert len(win) == 6, win
    assert [e["text"] for e in win] == [f"消息{i}" for i in range(5)] + ["回复"], win
    assert win[-1]["role"] == "assistant" and win[-1]["kind"] == "reply"
    assert s.count(TEST_GID) == 6
    assert s.stats(TEST_GID)["bytes"] > 0

    # 窗口上限生效
    assert len(s.window(TEST_GID, limit=2)) == 2
    print("✅ test_append_and_window PASS")


def test_tail_read_across_blocks():
    """写足够多的数据跨越多个 64KB 读块，验证倒读不丢不错"""
    s = _fresh_stream()
    filler = "x" * 300
    for i in range(600):          # 约 200KB
        s.append(TEST_GID, "user", f"{i:04d}-{filler}", user_id=1, nick="压测")
    win = s.window(TEST_GID, limit=5)
    assert len(win) == 5, len(win)
    # 必须是最后 5 条，且顺序正确
    assert [e["text"][:4] for e in win] == ["0595", "0596", "0597", "0598", "0599"], [e["text"][:4] for e in win]
    # 首行不能是被截断的半行
    assert all(e["text"].startswith(("059", "060")) or e["text"][:4].isdigit() for e in win)
    print(f"   文件 {s.stats(TEST_GID)['bytes']} 字节，倒读窗口 5 条 OK")
    print("✅ test_tail_read_across_blocks PASS")


def test_time_markers():
    now = time.time()
    # 间隔 <1h → 不标
    assert _time_marker(now - 60, now, now, first=False) == ""
    # 间隔 3 小时 → "3小时前"
    assert _time_marker(now - 3 * 3600, now, now, first=False) == "[3小时前]"
    # 间隔 2 天 → 绝对时间
    m = _time_marker(now - 2 * 86400, now, now, first=False)
    assert m.startswith("[") and ":" in m, m
    # 窗口第一条且很旧 → 绝对时间锚点
    assert _time_marker(None, now - 7200, now, first=True).startswith("[")
    # 窗口第一条但很新 → 不打扰
    assert _time_marker(None, now - 30, now, first=True) == ""

    s = _fresh_stream()
    s.append(TEST_GID, "user", "很久以前", user_id=1, nick="小明", ts=now - 2 * 86400)
    s.append(TEST_GID, "user", "刚刚", user_id=1, nick="小明", ts=now - 10)
    s.append(TEST_GID, "user", "紧接着", user_id=1, nick="小明", ts=now - 5)
    msgs = ConversationStream.to_messages(s.window(TEST_GID, limit=10), now=now)
    # 第一条：窗口起点 + 已经过去很久 → 绝对时间锚点
    assert msgs[0]["content"].startswith("小明: ["), msgs[0]
    # 第二条：与上一条隔了 2 天 → 也给绝对锚点（提示中间断了很久）
    assert msgs[1]["content"].startswith("小明: [") and msgs[1]["content"].endswith("刚刚"), msgs[1]
    # 第三条：只隔 5 秒 → 不打标记（省 token）
    assert msgs[2]["content"] == "小明: 紧接着", msgs[2]
    print("✅ test_time_markers PASS")


def test_legacy_migration():
    """旧 memory → 对话流，顺序保留、旧文件改名、第二次调用不重复迁"""
    s = _fresh_stream()
    legacy = s._legacy_dir / f"{TEST_GID}.json"
    old = [
        {"role": "user", "user_id": 888888, "nickname": "小明", "content": "我叫小明"},
        {"role": "assistant", "content": "你好小明"},
        {"role": "user", "user_id": 888888, "nickname": "小明", "content": "我叫什么？"},
    ]
    legacy.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    assert s.migrate_all() == 1, "应该迁移 1 个群"
    win = s.window(TEST_GID, limit=10)
    assert [e["text"] for e in win] == ["我叫小明", "你好小明", "我叫什么？"], win
    assert win[0]["nick"] == "小明" and win[0]["user_id"] == 888888
    assert win[1]["role"] == "assistant"
    assert not legacy.exists(), "旧文件应已改名"
    assert legacy.with_suffix(".json.migrated").exists(), "应留下 .migrated 备份"

    # 幂等：再调一次不该重复迁移
    assert s.migrate_all() == 0, "重复迁移了"
    assert len(s.window(TEST_GID, limit=10)) == 3
    print("✅ test_legacy_migration PASS")


def test_clear_makes_backup():
    s = _fresh_stream()
    s.append(TEST_GID, "user", "清空前", user_id=1, nick="小明")
    backup = s.clear(TEST_GID)
    assert backup and Path(backup).exists(), f"应该留备份: {backup}"
    assert s.window(TEST_GID, limit=10) == []
    assert s.count(TEST_GID) == 0
    print("✅ test_clear_makes_backup PASS")


async def test_concurrent_appends():
    """并发写（走单 worker 线程）不该丢数据、不该写坏 JSON"""
    s = _fresh_stream()
    await asyncio.gather(*[
        run_blocking(s.append, TEST_GID, "user", f"并发{i}", user_id=i, nick="压测")
        for i in range(30)
    ])
    win = s.window(TEST_GID, limit=100)
    assert len(win) == 30, len(win)
    texts = {e["text"] for e in win}
    assert texts == {f"并发{i}" for i in range(30)}, "并发写丢了数据"
    # 文件每行都必须是合法 JSON
    raw = (TMP / f"{TEST_GID}.jsonl").read_text(encoding="utf-8").strip().split("\n")
    for line in raw:
        json.loads(line)
    print("✅ test_concurrent_appends PASS")


async def test_memory_facade_uses_stream():
    """GroupMemory 门面写进去的东西，能原样从对话流读出来"""
    from app.memory.memory import GroupMemory

    shutil.rmtree(TMP, ignore_errors=True)
    stream = ConversationStream(stream_dir=TMP, legacy_memory_dir=TMP / "none")
    memory = GroupMemory(max_history=5, persist=False, stream=stream)

    await run_blocking(memory.add_user, TEST_GID, 888888, "小明", "你好")
    await run_blocking(memory.add_assistant, TEST_GID, "你好呀", kind="reply")

    msgs = memory.get_messages(TEST_GID)
    assert len(msgs) == 2, msgs
    assert msgs[0]["content"] == "小明: 你好", msgs
    assert msgs[1] == {"role": "assistant", "content": "你好呀"}, msgs
    assert memory.size(TEST_GID) == 2 and memory.total_entries(TEST_GID) == 2

    # 窗口上限 = max_history × 2
    for i in range(20):
        await run_blocking(memory.add_user, TEST_GID, 888888, "小明", f"刷屏{i}")
    assert len(memory.get_messages(TEST_GID)) == 10, "窗口上限应为 max_history×2"
    assert memory.size(TEST_GID) == 10
    assert memory.total_entries(TEST_GID) == 22

    stream.clear(TEST_GID)
    assert memory.get_messages(TEST_GID) == []
    print("✅ test_memory_facade_uses_stream PASS")


async def _run_all():
    test_append_and_window()
    test_tail_read_across_blocks()
    test_time_markers()
    test_legacy_migration()
    test_clear_makes_backup()
    await test_concurrent_appends()
    await test_memory_facade_uses_stream()


if __name__ == "__main__":
    print("=== 统一对话流回归测试 ===")
    # 【P0-4】对话流 append 会记指标 —— 指向临时文件，避免污染生产统计
    from app import metrics as _metrics_mod
    _mtmp = Path("data/_tmp_stream_metrics")
    shutil.rmtree(_mtmp, ignore_errors=True)
    _mtmp.mkdir(parents=True, exist_ok=True)
    _metrics_mod._instance = _metrics_mod.Metrics(path=_mtmp / "m.json", save_every=999)
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：对话流（P0-1）与时间标记（P0-2）就绪")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        shutil.rmtree(_mtmp, ignore_errors=True)
        _metrics_mod._instance = None
        shutdown_blocking(wait=False)
