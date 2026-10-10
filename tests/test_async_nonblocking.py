"""
异步非阻塞回归测试
==================
证明「磁盘 I/O」和「分段发送间隔」不再钉住事件循环：
在耗时的阻塞操作进行时，另开一个协程数 tick，tick 必须继续往上涨。

对照组：故意用 time.sleep 卡住协程 —— tick 应该几乎不涨，
以此说明这个测试真的能测出阻塞（而不是永远通过）。

运行: python tests/test_async_nonblocking.py
（必须在项目根目录运行：记忆/画像走的是相对路径 data/）
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.util.blocking import run_blocking, shutdown_blocking

# 用不会和真实群冲突的测试群号
TEST_GID = 100000001


async def _count_until(deadline: float, counter: list):
    """每隔一会儿涨一次 tick，用来观察事件循环有没有被卡住。

    deadline 由调用方**在阻塞操作之前**算好：
    如果事件循环被钉死，ticker 在 deadline 之前一次都轮不到调度。
    """
    while time.time() < deadline:
        counter[0] += 1
        await asyncio.sleep(0.01)


async def test_naive_sleep_blocks_loop():
    """对照组：协程里直接 time.sleep → 事件循环被钉死（tick 涨不动）"""
    ticks = [0]
    ticker = asyncio.create_task(_count_until(time.time() + 0.3, ticks))
    await asyncio.sleep(0)  # 先让 ticker 拿到一次调度
    time.sleep(0.3)         # 故意阻塞（这是我们要消灭的写法）
    await ticker
    print(f"   [对照] 协程内 time.sleep(0.3) → tick={ticks[0]}")
    assert ticks[0] <= 2, f"对照组不该涨这么多 tick={ticks[0]}，测试本身可能失效"
    print("✅ test_naive_sleep_blocks_loop PASS（证明测试能测出阻塞）")


async def test_run_blocking_keeps_loop_alive():
    """run_blocking：同样的 0.3s 阻塞，事件循环应该照常转"""
    ticks = [0]
    ticker = asyncio.create_task(_count_until(time.time() + 0.3, ticks))
    await asyncio.sleep(0)
    await run_blocking(time.sleep, 0.3)
    await ticker
    print(f"   [修复] run_blocking(time.sleep, 0.3) → tick={ticks[0]}")
    assert ticks[0] >= 15, f"事件循环被卡住了，tick 只有 {ticks[0]}"
    print("✅ test_run_blocking_keeps_loop_alive PASS")


async def test_send_group_multipart_nonblocking():
    """分段发送的间隔必须是 await asyncio.sleep，不能是 time.sleep"""
    from app.bot.handler import MessageHandler
    from app.config import Config

    sent: list[str] = []

    class StubOneBot:
        ws_connected = True

        async def send_group_message(self, group_id, text):
            sent.append(text)

        async def get_group_members(self, group_id):
            return []

    handler = MessageHandler(onebot=StubOneBot(), persona="测试人设")
    if handler._scheduler_task:
        handler._scheduler_task.cancel()

    # 3 个 400 字段落 → 会被切成 6 段 → 期间有 5 次 0.3s 间隔
    long_text = "\n".join("A" * 400 for _ in range(3))
    ticks = [0]
    ticker = asyncio.create_task(_count_until(time.time() + 10, ticks))
    await asyncio.sleep(0)
    start = time.time()
    await handler._send_group(TEST_GID, long_text)
    elapsed = time.time() - start
    ticker.cancel()

    print(f"   分段发送 {len(sent)} 段，耗时 {elapsed:.2f}s，期间 tick={ticks[0]}")
    assert len(sent) >= 4, f"分段数不对: {len(sent)}"
    assert elapsed >= 0.3 * (len(sent) - 1) - 0.05, "间隔似乎没生效"
    # 事件循环必须一直在转（被 time.sleep 钉死的话这里是 0）
    assert ticks[0] >= 20, f"分段发送期间事件循环被卡住，tick 只有 {ticks[0]}"
    # 内容没丢
    assert sum(p.count("A") for p in sent) == 1200, "分段后内容对不上"
    print("✅ test_send_group_multipart_nonblocking PASS")


async def test_state_managers_via_thread():
    """记忆 / 画像 / 外号在线程里读写：数据不丢、JSON 不坏"""
    from app.memory.memory import GroupMemory
    from app.style.profile import StyleProfile

    memory = GroupMemory()
    await run_blocking(memory.add_user, TEST_GID, 888888, "小明", "你好")
    await run_blocking(memory.add_assistant, TEST_GID, "你也好呀")
    messages = memory.get_messages(TEST_GID)
    assert len(messages) == 2, messages
    assert messages[0]["content"].startswith("小明: "), messages

    style = StyleProfile()
    # 并发丢 20 条：单 worker 串行化 → 内存和磁盘都不该丢
    await asyncio.gather(*[
        run_blocking(style.add_message, TEST_GID, "小明", f"消息{i}") for i in range(20)
    ])
    assert style.pending_count(TEST_GID) == 20, style.pending_count(TEST_GID)
    popped = await run_blocking(style.pop_pending, TEST_GID)
    assert len(popped) == 20, len(popped)
    assert style.pending_count(TEST_GID) == 0

    await run_blocking(style.update, TEST_GID, {"summary": "测试画像", "group_id": TEST_GID})
    assert style.get_summary(TEST_GID) == "测试画像"
    print("✅ test_state_managers_via_thread PASS")


async def test_alias_manager_concurrent_writes():
    """外号表并发写：所有条目都要落盘，JSON 必须还能解析"""
    import json
    from app.alias.manager import AliasManager

    alias = AliasManager()
    results = await asyncio.gather(*[
        run_blocking(alias.add, TEST_GID, 900000 + i, f"外号{i}", nick=f"昵称{i}")
        for i in range(10)
    ])
    assert all(ok for ok, _ in results), results

    data = alias.all_of_group(TEST_GID)
    assert len(data) == 10, f"并发写丢了条目: {len(data)}"
    raw = json.loads((Path("data/aliases") / f"{TEST_GID}.json").read_text(encoding="utf-8"))
    assert len(raw) == 10, f"磁盘上丢了条目: {len(raw)}"
    assert alias.find_qq_by_alias(TEST_GID, "外号3") == "900003"
    print("✅ test_alias_manager_concurrent_writes PASS")


async def test_alias_read_while_writing():
    """一边写外号（worker 线程）一边读外号表（事件循环线程）

    不加锁的话，读到一半 dict 被插入新 key 会炸
    「RuntimeError: dictionary changed size during iteration」。
    """
    from app.alias.manager import AliasManager

    alias = AliasManager()
    stop = False
    writes = [0]

    async def writer():
        i = 0
        while not stop:
            await run_blocking(alias.add, TEST_GID, 950000 + i, f"并发{i}", nick=f"昵称{i}")
            writes[0] += 1
            i += 1

    async def reader():
        while not stop:
            alias.get_prompt_view(TEST_GID)  # 事件循环线程里直接读
            await asyncio.sleep(0)

    w = asyncio.create_task(writer())
    r = asyncio.create_task(reader())
    await asyncio.sleep(0.5)
    stop = True
    await asyncio.gather(w, r)
    print(f"   并发读写 0.5s：写 {writes[0]} 次，无异常")
    assert writes[0] > 0, "写入没跑起来"
    print("✅ test_alias_read_while_writing PASS")


async def test_sticker_commit_dedup_and_write():
    """表情包落盘 + MD5 去重（在临时目录里做，不碰真实表情库）"""
    from app.sticker.manager import StickerManager

    mgr = StickerManager()
    # 临时目录放在工作区内（data/_tmp_stickers），测完删掉
    tmp = Path("data/_tmp_stickers")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    mgr._dir = tmp
    mgr._index_path = tmp / "index.json"
    mgr._stickers = []

    payload = b"\xff\xd8\xff" + b"x" * 2048
    first = await run_blocking(mgr._commit, payload, ".jpg", "id-1", TEST_GID, 888888, "小明")
    assert first and Path(first).exists(), first
    # 同一份内容再来一次 → MD5 去重
    second = await run_blocking(mgr._commit, payload, ".jpg", "id-2", TEST_GID, 888888, "小明")
    assert second is None, "MD5 去重失效"
    assert len(mgr.all()) == 1
    assert (tmp / "index.json").exists()
    shutil.rmtree(tmp, ignore_errors=True)
    print("✅ test_sticker_commit_dedup_and_write PASS")


async def _run_all():
    await test_naive_sleep_blocks_loop()
    await test_run_blocking_keeps_loop_alive()
    await test_send_group_multipart_nonblocking()
    await test_state_managers_via_thread()
    await test_alias_manager_concurrent_writes()
    await test_alias_read_while_writing()
    await test_sticker_commit_dedup_and_write()


def _cleanup():
    """把测试期间产生的假数据删掉"""
    for path in (
        Path("data/memory") / f"{TEST_GID}.json",
        Path("data/stream") / f"{TEST_GID}.jsonl",
        Path("app/data/memory") / f"{TEST_GID}.json",
        Path("data/group_style") / f"{TEST_GID}.json",
        Path("data/aliases") / f"{TEST_GID}.json",
    ):
        try:
            path.unlink()
        except OSError:
            pass
    shutil.rmtree(Path("data/_tmp_stickers"), ignore_errors=True)


if __name__ == "__main__":
    print("=== 异步非阻塞回归测试 ===\n")
    # 【P0-4/P2a】本测试会往对话流写数据，而对话流会记指标；handler 还会建调度器。
    # 所有会落盘的单例统一指到临时目录（指标 / 风格画像 / 任务状态与记录）。
    import shutil as _shutil
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from helpers_isolate import isolate_writable_state, reset_writable_state
    _mtmp = Path("data/_tmp_nonblocking")
    _shutil.rmtree(_mtmp, ignore_errors=True)
    isolate_writable_state(_mtmp)
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：阻塞 I/O 已不再钉住事件循环")
    finally:
        _cleanup()
        _shutil.rmtree(_mtmp, ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
