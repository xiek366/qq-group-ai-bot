"""
运行指标（P0-4）回归测试
========================
覆盖：计数、被回应率口径（10 分钟窗口 + 同一句只记一次）、失败率/延迟/token 汇总、
按天裁剪、落盘与重载、以及"事件循环接口不落盘"的约定。

运行: python tests/test_metrics.py
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.metrics import KEEP_DAYS, REPLY_WINDOW_SECONDS, Metrics

TMP = Path("data/_tmp_metrics_test")
GID = 100000004


def _fresh() -> Metrics:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    return Metrics(path=TMP / "summary.json", save_every=3)


def test_message_counts():
    m = _fresh()
    for _ in range(3):
        m.record_message(GID, "user")
    m.record_message(GID, "assistant")
    snap = m.snapshot()
    g = next(x for x in snap["groups"] if x["group_id"] == str(GID))
    assert g["user_msgs"] == 3 and g["bot_msgs"] == 1, g
    today = snap["days"][-1]
    assert today["user_msgs"] == 3 and today["bot_msgs"] == 1, today
    print("✅ test_message_counts PASS")


def test_reply_rate():
    m = _fresh()
    # bot 发言 → 5 分钟后有人说话 = 被回应
    m.record_message(GID, "assistant")
    m._pending_bot[GID] = time.time() - 300          # 模拟 5 分钟前
    m.record_message(GID, "user")
    # bot 再说一句，但没人理（超过窗口）
    m.record_message(GID, "assistant")
    m._pending_bot[GID] = time.time() - (REPLY_WINDOW_SECONDS + 60)
    m.record_message(GID, "user")

    g = next(x for x in m.snapshot()["groups"] if x["group_id"] == str(GID))
    assert g["bot_msgs"] == 2, g
    assert g["bot_responded"] == 1, f"只该记 1 次被回应: {g}"
    assert abs(g["reply_rate"] - 0.5) < 1e-6, g
    print(f"   被回应率 = {g['reply_rate']:.0%}（2 句里 1 句有人接）")
    print("✅ test_reply_rate PASS")


def test_reply_counted_once_per_bot_message():
    """一句话被多人回应只算一次 —— 否则被回应率会虚高"""
    m = _fresh()
    m.record_message(GID, "assistant")
    for _ in range(5):
        m.record_message(GID, "user")
    g = next(x for x in m.snapshot()["groups"] if x["group_id"] == str(GID))
    assert g["bot_responded"] == 1, g
    print("✅ test_reply_counted_once_per_bot_message PASS")


def test_llm_and_vision_stats():
    m = _fresh()
    m.record_llm(True, latency=2.0, tokens_in=100, tokens_out=50)
    m.record_llm(True, latency=4.0, tokens_in=200, tokens_out=80)
    m.record_llm(False)
    m.record_vision(cached=True, ok=True)
    m.record_vision(ok=False)
    snap = m.snapshot()
    llm, vis = snap["llm"], snap["vision"]
    assert llm["calls"] == 3 and llm["failures"] == 1, llm
    assert abs(llm["failure_rate"] - 1 / 3) < 1e-3, llm
    assert abs(llm["avg_latency"] - 2.0) < 1e-6, llm       # (2+4+0)/3
    assert llm["tokens_in"] == 300 and llm["tokens_out"] == 130, llm
    assert vis["calls"] == 2 and vis["cache_hits"] == 1 and vis["failures"] == 1, vis
    assert abs(vis["hit_rate"] - 0.5) < 1e-6, vis
    print("✅ test_llm_and_vision_stats PASS")


def test_persist_and_reload():
    m = _fresh()
    m.record_message(GID, "user")
    m.record_llm(True, latency=1.0, tokens_in=10, tokens_out=5)
    m.save()                                   # 显式落盘

    m2 = Metrics(path=TMP / "summary.json")
    snap = m2.snapshot()
    assert snap["llm"]["calls"] == 1 and snap["llm"]["tokens_in"] == 10, snap["llm"]
    g = next(x for x in snap["groups"] if x["group_id"] == str(GID))
    assert g["user_msgs"] == 1, g
    print("✅ test_persist_and_reload PASS")


def test_daily_pruned_to_keep_days():
    m = _fresh()
    with m._lock:
        for i in range(20):
            m._data["daily"][f"2026-01-{i + 1:02d}"] = {"user_msgs": 1, "bot_msgs": 0,
                                                         "llm_calls": 0, "vision_calls": 0}
    m.save()
    daily = m.snapshot()["days"]
    assert len(daily) == KEEP_DAYS, f"应只保留 {KEEP_DAYS} 天，实得 {len(daily)}"
    assert daily[-1]["date"] == "2026-01-20"
    print(f"   按天数据裁剪为 {len(daily)} 天（保留最近 {KEEP_DAYS} 天）")
    print("✅ test_daily_pruned_to_keep_days PASS")


def test_event_loop_paths_do_not_write_disk():
    """record_llm / record_vision 由事件循环调用 → 绝不能触发磁盘写"""
    m = _fresh()
    m.save()                                    # 先建好文件
    path = TMP / "summary.json"
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    for _ in range(50):                         # 远超 save_every
        m.record_llm(True, latency=0.1)
        m.record_vision(ok=True)
    after = path.stat().st_mtime_ns
    assert before == after, "事件循环路径竟然写了盘（会把阻塞带回事件循环）"
    # 但消息路径（worker 线程）该批量落盘
    for _ in range(5):
        m.record_message(GID, "user")
    assert path.stat().st_mtime_ns != after, "消息路径应该触发批量落盘"
    print("✅ test_event_loop_paths_do_not_write_disk PASS")


def test_snapshot_is_a_copy():
    m = _fresh()
    m.record_message(GID, "user")
    snap = m.snapshot()
    snap["groups"][0]["user_msgs"] = 999
    assert m.snapshot()["groups"][0]["user_msgs"] == 1, "快照必须是深拷贝"
    print("✅ test_snapshot_is_a_copy PASS")


if __name__ == "__main__":
    print("=== 运行指标回归测试（P0-4）===")
    try:
        test_message_counts()
        test_reply_rate()
        test_reply_counted_once_per_bot_message()
        test_llm_and_vision_stats()
        test_persist_and_reload()
        test_daily_pruned_to_keep_days()
        test_event_loop_paths_do_not_write_disk()
        test_snapshot_is_a_copy()
        print("\n🎉 全部通过：指标可用、口径明确、不把磁盘 I/O 带回事件循环")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
