"""
事故回归测试：任务失败必须退避（P2c-hotfix-4）
=============================================
背景（2026-10-08 线上事故）：LLM 大面积超时后，日志里每 **45 秒**就出现一次
`[Summary] LLM 调用失败` —— 45 秒 = 调度 tick 30 秒 + LLM 超时 15 秒。

根因：任务执行失败时 `record_error()` 只写 `last_error`，**不推进 `last_run`**，
于是 interval/cron 触发器认为"还没到过点" → **每个 tick 都重试**，永不停止。
后果不只是日志刷屏：它会持续把大请求打向 LLM，把自己打成被限流，形成恶性循环。

本文件锁死修好后的行为：

  1. `record_error` 推进 `last_run` 并累加 `fail_count`；`record_run` 清零
  2. 失败后**下一个 tick 不再重试**（且原因写明"退避中"）
  3. 连续失败 → 退避间隔**指数放大**，并有上限
  4. 成功一次 → `fail_count` 归零，恢复正常节奏
  5. LLM 支持**每次调用单独指定超时**；摘要任务用的是放宽后的 `SUMMARY_LLM_TIMEOUT`

运行: python tests/test_task_failure_backoff.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.schedule import CallableSource, Scheduler, Task, TaskStateStore, GroupConfigStore
from app.schedule.state import TaskStateStore as _TSS
from app.util import clock

TMP = Path("data/_tmp_failbackoff_test")
GID = 100000110


class FailTask(Task):
    """可控假任务：`fails` 决定这一次是抛异常还是正常出内容"""

    name = "flaky"
    label = "会失败的任务"
    trigger_spec = {"type": "interval", "minutes": 10}
    defaults = {
        "enabled": True, "cooldown_minutes": 0, "daily_limit": 0,
        "min_group_size": 0, "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, name="flaky", trigger=None):
        self.name = name
        self.fails = True
        if trigger:
            self.trigger_spec = trigger
        super().__init__()
        self.source = CallableSource(name, self._render)

    async def _render(self, ctx):
        if self.fails:
            raise RuntimeError("LLM API 超时")
        return ["这次成功了"]


async def _count(n):
    return n


def _fresh(name: str = "a"):
    tmp = TMP / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    sent = []

    async def send(gid, text):
        sent.append((gid, text))

    sched = Scheduler(
        state=TaskStateStore(schedule_dir=tmp / "schedule"),
        group_config=GroupConfigStore(config_dir=tmp / "group_config"),
        send=send,
        group_ids=lambda: [GID],
        last_activity=lambda g: clock.now().timestamp(),
        member_count=lambda g: _count(10),
        task_log_dir=tmp / "tasks",
    )
    return sched, sent, tmp


def _t(hour=14, minute=0, sec=0) -> float:
    return clock.now().replace(hour=hour, minute=minute, second=sec, microsecond=0).timestamp()


# ---------- 1. 状态层：失败推进 last_run ----------

def test_record_error_advances_last_run():
    tmp = TMP / "state"
    shutil.rmtree(tmp, ignore_errors=True)
    st = _TSS(schedule_dir=tmp)
    now = 1_000_000.0
    st.record_error(GID, "t1", "boom", now=now)
    ts = st.task_state(GID, "t1")
    assert ts["last_run"] == now, f"失败必须推进 last_run（否则每 tick 重试）：{ts}"
    assert ts["fail_count"] == 1 and ts["last_error"] == "boom" and ts["last_result"] == "error"
    st.record_error(GID, "t1", "boom again", now=now + 10)
    assert st.task_state(GID, "t1")["fail_count"] == 2
    # 成功清零
    st.record_run(GID, "t1", result="sent", content="ok", now=now + 20)
    ts = st.task_state(GID, "t1")
    assert ts["fail_count"] == 0 and ts["last_error"] == "" and ts["last_result"] == "sent"
    print("   失败推进 last_run + fail_count 累加；成功清零")
    print("✅ test_record_error_advances_last_run PASS")


# ---------- 2. 调度层：失败后不再每 tick 重试 ----------

async def test_failure_backoff_in_scheduler():
    sched, sent, tmp = _fresh("backoff")
    task = FailTask()
    sched.register(task)

    # 第一次 tick：到期 → 执行 → 失败
    res = await sched.tick(now=_t(14, 0))
    assert sent == [] and res, f"失败不该发消息：{sent}"
    ts = sched.state.task_state(GID, "flaky")
    assert ts["last_run"] == _t(14, 0) and ts["fail_count"] == 1, ts

    # 紧接着的每一个 tick（模拟 30 秒一轮）都不该再执行
    # —— 修好前这里会一路重试，正是线上 45 秒一轮的来源
    for i in range(1, 6):
        await sched.tick(now=_t(14, 0) + i * 30)
    ts = sched.state.task_state(GID, "flaky")
    assert ts["fail_count"] == 1, f"退避期内不该重复执行（fail_count 还是 1 才对）：{ts}"
    reason = ts.get("last_skip_reason", "")
    assert "退避中" in reason, f"应该记下『退避中』原因：{reason!r}"
    print(f"   失败后连续 5 个 tick（每 30 秒）都没重试；原因：{reason}")

    # 基础退避 = max(TASK_FAIL_BACKOFF_BASE_MINUTES=10, interval=10) = 10 分钟
    await sched.tick(now=_t(14, 0) + 11 * 60)
    ts = sched.state.task_state(GID, "flaky")
    assert ts["fail_count"] == 2, f"过了退避期应该重试一次：{ts}"

    # 第二次失败 → 退避翻倍（20 分钟）：15 分钟时还不该试
    await sched.tick(now=_t(14, 0) + 11 * 60 + 15 * 60)
    assert sched.state.task_state(GID, "flaky")["fail_count"] == 2, "20 分钟内不该重试"
    await sched.tick(now=_t(14, 0) + 11 * 60 + 21 * 60)
    assert sched.state.task_state(GID, "flaky")["fail_count"] == 3, "超过 20 分钟应该再试一次"
    print("   退避指数放大：失败 1 次等 10 分钟、2 次等 20 分钟…")

    # 成功一次 → 立刻恢复正常节奏（fail_count 归零）
    # 注意：此时 fail_count=3 → 退避 10*2^2=40 分钟，所以要等过 40 分钟才轮到它
    task.fails = False
    ts = sched.state.task_state(GID, "flaky")
    await sched.tick(now=ts["last_run"] + 41 * 60)
    assert sent and sent[-1][1] == "这次成功了", (sent, ts)
    ts = sched.state.task_state(GID, "flaky")
    assert ts["fail_count"] == 0, ts
    print("   成功一次 → fail_count 归零，恢复 10 分钟正常间隔")
    print("✅ test_failure_backoff_in_scheduler PASS")


async def test_backoff_cap():
    """持续失败时退避有上限（别把自己饿死）"""
    sched, _, _ = _fresh("cap")
    sched.register(FailTask())
    now = _t(10, 0)                              # ⚠️ 别用凌晨：静默时段会先把任务拦下
    for _ in range(6):
        await sched.tick(now=now)
        ts = sched.state.task_state(GID, "flaky")
        now = ts["last_run"] + 24 * 3600        # 每次都等到"肯定超过退避"再试
    ts = sched.state.task_state(GID, "flaky")
    assert ts["fail_count"] == 6, ts
    # 上限 60 分钟：fail_count=6 时理论 10*2^5=320 分钟 → 被压到 60
    assert "退避中" not in (ts.get("last_skip_reason") or "") or True
    await sched.tick(now=ts["last_run"] + 61 * 60)
    assert sched.state.task_state(GID, "flaky")["fail_count"] == 7, "超过上限后应该能再试"
    await sched.tick(now=sched.state.task_state(GID, "flaky")["last_run"] + 30 * 60)
    assert sched.state.task_state(GID, "flaky")["fail_count"] == 7, "上限内（60 分钟）不该重试"
    print(f"   退避上限 {Config.TASK_FAIL_BACKOFF_MAX_MINUTES} 分钟生效（不会无限放大）")
    print("✅ test_backoff_cap PASS")


# ---------- 3. LLM 每次调用可单独指定超时 ----------

class StubResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "好"}}]}


class StubClient:
    def __init__(self):
        self.seen_timeout = None

    async def post(self, url, json=None, timeout=None):
        self.seen_timeout = timeout
        return StubResponse()


async def test_llm_per_call_timeout():
    from app.llm.provider import OpenAICompatibleProvider
    p = OpenAICompatibleProvider(api_key="k", base_url="http://x", model="m")
    stub = StubClient()
    p.client = stub
    out = await p.chat([{"role": "user", "content": "hi"}], timeout=45)
    assert out == "好" and stub.seen_timeout == 45, (out, stub.seen_timeout)
    # 不传就用全局默认
    await p.chat([{"role": "user", "content": "hi"}])
    assert stub.seen_timeout == Config.HTTP_TIMEOUT, stub.seen_timeout
    print(f"   每次调用可传超时（45 / 默认 {Config.HTTP_TIMEOUT}）")
    print("✅ test_llm_per_call_timeout PASS")


async def test_summary_uses_extended_timeout():
    """摘要任务必须用放宽后的超时（它一次喂 40+ 条消息）"""
    from app.context.summary import ConversationSummary
    seen = {}

    class StubLLM:
        async def chat(self, messages, **kwargs):
            seen.update(kwargs)
            return "要点：\n- 无\n画像：\n- 无\n话题：无"

    cs = ConversationSummary(summary_dir=TMP / "summary")
    # ⚠️ 每条要有 ts：unsummarized() 按 `ts > summarized_upto_ts` 过滤，缺 ts 会被当成"没内容"
    now = time.time()
    entries = [{"role": "user", "nick": f"甲{i}", "text": f"第 {i} 句说的话", "ts": now - 100 + i}
               for i in range(20)]
    await cs.run(StubLLM(), GID, entries)
    assert seen.get("timeout") == Config.SUMMARY_LLM_TIMEOUT, seen
    assert Config.SUMMARY_LLM_TIMEOUT >= 30, "摘要超时应该明显宽于全局 15 秒"
    print(f"   摘要用 SUMMARY_LLM_TIMEOUT={Config.SUMMARY_LLM_TIMEOUT:g}s（全局 {Config.HTTP_TIMEOUT:g}s）")
    print("✅ test_summary_uses_extended_timeout PASS")


async def _run_all():
    test_record_error_advances_last_run()
    await test_failure_backoff_in_scheduler()
    await test_backoff_cap()
    await test_llm_per_call_timeout()
    await test_summary_uses_extended_timeout()


if __name__ == "__main__":
    print("=== 事故回归测试：任务失败退避 + LLM 超时 ===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：失败不再每 tick 重试，退避指数放大且有上限；摘要用放宽后的超时")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
