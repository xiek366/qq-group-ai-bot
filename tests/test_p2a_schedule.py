"""
P2a 调度框架回归测试
====================
覆盖 DEVLOG [P2a-0] §4 的测试计划：

触发判定
  1. idle / interval / cron 三种触发的基本判定
  2. cron 的边界：没到点、到点、今天已跑过、迟到超过容忍值、星期不匹配
  3. 静默时段（含跨午夜）与工作时段
  4. 时段窗 window

打扰控制（顺序即优先级）
  5. 任务每日上限 / 每群每日总预算
  6. 冷却（算"没到点"，不该写成"被拦"）
  7. 群规模不足 / 群不活跃前置
  8. D15：不在 PROACTIVE_GROUPS 的群，主动任务被拦；维护型任务不受限
  9. 无人应答退避：门槛翻倍；有人说话 → 清零

执行与记账
 10. 内容源返回空 → 不发、不记账（记录 empty）
 11. 内容源抛异常 → 记 error，不发送
 12. 内容指纹去重（跨天去重的基础）
 13. **干跑语义**：发送器不被调用、状态文件前后完全一致、预算不涨
 14. 手动执行：真发 + 记账
 15. last_run 落在未来（时钟回拨/脏数据）→ 不被永久卡死
 16. 配置优先级：群覆盖 > 环境变量 > 代码默认
 17. 加新任务不需要改任何既有代码（只 register）

运行: python tests/test_p2a_schedule.py
（全部状态写临时目录）
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.schedule import (
    CallableSource, CronTrigger, IdleTrigger, IntervalTrigger, PRIORITY_ICEBREAK,
    PRIORITY_MAINTENANCE, Scheduler, Task, TaskStateStore, GroupConfigStore,
)
from app.schedule.group_config import normalize_quiet_hours
from app.schedule.state import fingerprint, today_key
from app.schedule.triggers import in_quiet_hours, in_window
from app.util import clock

TMP = Path("data/_tmp_p2a_test")
GID = 100000050
GID2 = 100000051


# ---------- 测试脚手架 ----------

class FakeTask(Task):
    """一个可配置的假任务：内容固定，方便断言"""

    name = "fake"
    label = "假任务"
    priority = PRIORITY_ICEBREAK
    trigger_spec = {"type": "idle", "minutes": 30}
    defaults = {
        "enabled": True, "cooldown_minutes": 0, "daily_limit": 0,
        "min_group_size": 0, "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False, "factor": 2, "max_level": 3},
    }

    def __init__(self, name="fake", parts=None, boom=False, source=None, trigger=None):
        self.name = name
        self.parts = parts if parts is not None else ["你好呀"]
        self.boom = boom
        if trigger:
            self.trigger_spec = trigger
        super().__init__()
        self.source = source or CallableSource(name, self._render)

    async def _render(self, ctx):
        if self.boom:
            raise RuntimeError("内容源炸了")
        return list(self.parts)


def _fresh(name: str = "a") -> tuple[Scheduler, list, Path]:
    tmp = TMP / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    sent: list = []

    async def send(gid, text):
        sent.append((gid, text))

    sched = Scheduler(
        state=TaskStateStore(schedule_dir=tmp / "schedule"),
        group_config=GroupConfigStore(config_dir=tmp / "group_config"),
        send=send,
        group_ids=lambda: [GID],
        last_activity=lambda g: 0.0,
        member_count=lambda g: _count(10),
        task_log_dir=tmp / "tasks",
    )
    return sched, sent, tmp


async def _count(n):
    return n


def _day(hour: int = 14, minute: int = 0, offset_days: int = 0) -> float:
    base = clock.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base.timestamp() + offset_days * 86400


# ---------- 触发判定 ----------

def test_trigger_basics():
    idle = IdleTrigger(minutes=30)
    assert idle.due(now=1000, last_run=0, idle_minutes=31, task_state={})
    assert not idle.due(now=1000, last_run=0, idle_minutes=29, task_state={})

    itv = IntervalTrigger(minutes=60)
    assert itv.due(now=1000, last_run=0, idle_minutes=0, task_state={}), "从没跑过应可跑"
    assert itv.due(now=1000 + 3600, last_run=1000, idle_minutes=0, task_state={})
    assert not itv.due(now=1000 + 3599, last_run=1000, idle_minutes=0, task_state={})

    # 时段窗
    noon = _day(12)
    assert IntervalTrigger(minutes=1, window=["10:00", "18:00"]).due(
        now=noon, last_run=0, idle_minutes=0, task_state={})
    night = _day(22)
    assert not IntervalTrigger(minutes=1, window=["10:00", "18:00"]).due(
        now=night, last_run=0, idle_minutes=0, task_state={})
    print("✅ test_trigger_basics PASS")


def test_cron_boundaries():
    cron = CronTrigger(at="08:00")
    before = _day(7, 59)
    at = _day(8, 0)
    late = _day(8, 30)
    too_late = _day(8, 0) + (Config.CRON_CATCH_UP_MINUTES + 10) * 60

    assert not cron.due(now=before, last_run=0, idle_minutes=0, task_state={}), "没到点不该触发"
    assert cron.due(now=at, last_run=0, idle_minutes=0, task_state={}), "到点该触发"
    assert cron.due(now=late, last_run=0, idle_minutes=0, task_state={}), "迟到 30 分钟内可补"
    assert not cron.due(now=too_late, last_run=0, idle_minutes=0, task_state={}), "迟到太久不补"
    # 今天已经跑过 → 不再触发
    assert not cron.due(now=late, last_run=at, idle_minutes=0, task_state={}), "当天跑过不该重复"
    # 昨天跑过 → 今天照常
    assert cron.due(now=at, last_run=at - 86400, idle_minutes=0, task_state={})

    # 星期：今天不是周五就不触发（用与今天不同的星期号）
    today_wd = clock.now().isoweekday()
    other_wd = today_wd % 7 + 1
    cron_wd = CronTrigger(at="08:00", weekday=other_wd)
    assert not cron_wd.due(now=at, last_run=0, idle_minutes=0, task_state={})
    cron_same = CronTrigger(at="08:00", weekday=today_wd)
    assert cron_same.due(now=at, last_run=0, idle_minutes=0, task_state={})
    print("✅ test_cron_boundaries PASS")


def test_quiet_hours_and_work_hours():
    # 跨午夜：23:00-08:00
    assert in_quiet_hours(_day(23, 30), "23:00-08:00")
    assert in_quiet_hours(_day(3, 0), "23:00-08:00")
    assert not in_quiet_hours(_day(12, 0), "23:00-08:00")
    assert in_quiet_hours(_day(13, 0), "12:00-14:00"), "非跨午夜也要对"
    assert not in_quiet_hours(_day(14, 0), "12:00-14:00")
    assert not in_quiet_hours(_day(12, 0), ""), "空配置 = 不静默"
    assert not in_quiet_hours(_day(12, 0), "乱写的"), "解析不了宁可视为不静默"

    assert in_window(_day(12), ["10:00", "18:00"])
    assert not in_window(_day(9), ["10:00", "18:00"])
    assert in_window(_day(23), ["22:00", "02:00"]), "跨午夜时段窗"
    assert in_window(_day(12), None), "不配 window = 不限制"

    assert normalize_quiet_hours(["23:00", "08:00"]) == "23:00-08:00"
    assert normalize_quiet_hours("23:00-08:00") == "23:00-08:00"
    print("✅ test_quiet_hours_and_work_hours PASS")


# ---------- 打扰控制 ----------

async def test_daily_limits():
    sched, sent, _ = _fresh("limits")
    Config.PROACTIVE_GROUPS = [GID]
    task = FakeTask()
    task.defaults["daily_limit"] = 2
    task.defaults["cooldown_minutes"] = 0
    sched.register(task)
    sched._last_activity = lambda g: _day(13, 0)          # 静默 60 分钟

    for i in range(2):
        await sched.tick(now=_day(14) + i * 60)
    assert len(sent) == 2, f"每日上限 2 应该只发 2 条，实际 {len(sent)}"
    await sched.tick(now=_day(14) + 3600)
    assert len(sent) == 2, "达到任务每日上限后不该再发"
    ts = sched.state.task_state(GID, "fake")
    assert ts["last_skip_reason"].startswith("已达任务每日上限"), ts

    # 每群总预算：换个任务，把上限压到 1
    sched2, sent2, tmp2 = _fresh("budget")
    sched2.group_config._load_file = lambda gid: {"daily_total_limit": 1}   # type: ignore
    sched2.group_config.invalidate()
    sched2._last_activity = lambda g: _day(13, 0)
    t2 = FakeTask(name="t2")
    t3 = FakeTask(name="t3", parts=["第二条"])
    t3.priority = PRIORITY_MAINTENANCE
    sched2.register(t2)
    sched2.register(t3)
    await sched2.tick(now=_day(14))
    await sched2.tick(now=_day(14) + 120)
    assert len(sent2) == 1, f"每群每日总上限 1，应只发 1 条，实际 {len(sent2)}"
    print(f"   任务上限 2 → 发出 {len(sent)} 条；群总上限 1 → 发出 {len(sent2)} 条")
    print("✅ test_daily_limits PASS")


async def test_cooldown_and_gates():
    sched, sent, _ = _fresh("cooldown")
    Config.PROACTIVE_GROUPS = [GID]
    task = FakeTask()
    task.defaults["cooldown_minutes"] = 60
    sched.register(task)
    sched._last_activity = lambda g: _day(13, 0)

    await sched.tick(now=_day(14))
    assert len(sent) == 1, f"第一次应执行，实际 {sent}"
    await sched.tick(now=_day(14) + 600)          # 10 分钟后：冷却中
    assert len(sent) == 1, "冷却期内不该再执行"
    ts = sched.state.task_state(GID, "fake")
    # 冷却属于"没到点"，不该写成"被拦"（否则 /任务 列表 会误导）
    assert not ts.get("last_skip_reason"), f"冷却中不该记'被拦'：{ts.get('last_skip_reason')!r}"
    await sched.tick(now=_day(14) + 3700)         # 冷却过后
    assert len(sent) == 2, "冷却过了应该再发"

    # 群规模不足
    sched2, sent2, _ = _fresh("size")
    sched2._member_count = lambda g: _count(2)
    t2 = FakeTask(name="size")
    t2.defaults["min_group_size"] = 3
    sched2.register(t2)
    sched2._last_activity = lambda g: _day(13, 0)
    await sched2.tick(now=_day(14))
    assert not sent2, "群人数不足不该发"
    assert "群人数不足" in sched2.state.task_state(GID, "size")["last_skip_reason"]

    # 群不活跃（提醒类：死群不打扰）
    sched3, sent3, _ = _fresh("inactive")
    sched3._last_activity = lambda g: _day(2, 0)      # 12 小时前
    t3 = FakeTask(name="remind")
    t3.defaults["require_active_within_minutes"] = 30
    sched3.register(t3)
    await sched3.tick(now=_day(14))
    assert not sent3, "群不活跃时提醒类任务不该发"
    print("✅ test_cooldown_and_gates PASS")


async def test_d15_and_maintenance_task():
    """D15：不在主动名单的群不打扰；维护型任务（is_proactive=False）不受限"""
    sched, sent, _ = _fresh("d15")
    Config.PROACTIVE_GROUPS = [GID]          # 只有 GID 允许
    sched._group_ids = lambda: [GID, GID2]
    sched._last_activity = lambda g: _day(13, 0)

    proactive = FakeTask(name="proactive")
    maint = FakeTask(name="maint", parts=["维护消息"])
    maint.is_proactive = False
    maint.priority = PRIORITY_MAINTENANCE
    sched.register(proactive)
    sched.register(maint)

    await sched.tick(now=_day(14))
    proactive_targets = {g for g, text in sent if text == "你好呀"}
    maint_targets = {g for g, text in sent if text == "维护消息"}
    assert GID in proactive_targets, f"允许的群应该收到主动消息：{sent}"
    assert GID2 not in proactive_targets, f"D15：{GID2} 不该收到主动消息，实际 {sent}"
    assert GID2 in maint_targets, f"维护型任务不该受主动名单限制，实际 {sent}"
    assert sched.state.task_state(GID2, "proactive")["last_skip_reason"] == "不在主动打扰名单"
    print(f"   主动消息只到 {sorted(proactive_targets)}；维护消息到 {sorted(maint_targets)}")
    print("✅ test_d15_and_maintenance_task PASS")


async def test_backoff_on_no_reply():
    sched, sent, _ = _fresh("backoff")
    Config.PROACTIVE_GROUPS = [GID]
    task = FakeTask()
    task.defaults["cooldown_minutes"] = 0
    task.defaults["backoff"] = {"on_no_reply": True, "factor": 2, "max_level": 3}
    sched.register(task)
    activity = {"ts": _day(13, 0)}
    sched._last_activity = lambda g: activity["ts"]

    await sched.tick(now=_day(14))
    assert len(sent) == 1
    # 过了宽限期仍没人说话 → 退避 1 级
    await sched.tick(now=_day(14) + (Config.NO_REPLY_GRACE_MINUTES + 1) * 60)
    assert sched.state.task_state(GID, "fake")["backoff_level"] == 1, "没人应答应该退避"

    # 退避后门槛翻倍：先把"等待应答"收尾（否则 14:29 的活跃会被判定成"有人应答"而清零），
    # 再给 31 分钟静默 —— 此时门槛已被抬到 60 分钟，不该破冰
    st = sched.state.task_state(GID, "fake")
    st["pending_check_ts"] = 0.0
    st["last_run"] = 0
    activity["ts"] = _day(15) - 31 * 60
    before = len(sent)
    await sched.tick(now=_day(15))
    assert len(sent) == before, f"退避后门槛应翻倍（60 分钟），31 分钟静默不该再破冰（实际 {sent}）"

    # 有人说话 → 清零（把 pending 设成过去，活跃时间晚于它 = 有人应答）
    st["pending_check_ts"] = _day(15)
    activity["ts"] = _day(15) + 10
    await sched.tick(now=_day(15) + 60)
    assert sched.state.task_state(GID, "fake")["backoff_level"] == 0, "有人应答应清零退避"
    print("✅ test_backoff_on_no_reply PASS")


# ---------- 执行与记账 ----------

async def test_empty_and_error():
    sched, sent, _ = _fresh("empty")
    sched._last_activity = lambda g: _day(13, 0)
    sched.register(FakeTask(name="empty_task", parts=[]))
    sched.register(FakeTask(name="boom_task", boom=True, parts=[]))
    await sched.tick(now=_day(14))
    assert not sent, "内容为空或异常都不该发送"
    log = sched.recent_log(GID, limit=10)
    statuses = sorted(e["status"] for e in log)
    assert statuses == ["empty", "error"], statuses
    print(f"   任务记录：{[(e['task'], e['status']) for e in log]}")
    print("✅ test_empty_and_error PASS")


async def test_fingerprint_dedup():
    fp1 = fingerprint("icebreak", GID, "今天聊聊显卡")
    fp2 = fingerprint("icebreak", GID, "今天聊聊显卡")
    fp3 = fingerprint("icebreak", GID, "今天聊聊别的")
    assert fp1 == fp2 and fp1 != fp3
    sched, _, _ = _fresh("fp")
    sched._last_activity = lambda g: _day(13, 0)
    task = FakeTask()
    sched.register(task)
    await sched.tick(now=_day(14))
    today = today_key(_day(14))
    assert sched.state.task_state(GID, "fake")["day"] == today
    # 指纹按 (任务名, 群号, 内容) 算 —— 用真实任务名与真实内容才对得上
    real_fp = fingerprint("fake", GID, task.parts[0])
    assert sched.state.seen_fingerprint(GID, "fake", real_fp), "发过的内容指纹应被记住"
    assert not sched.state.seen_fingerprint(GID, "fake", fp3), "没发过的内容不该被当成重复"
    print("✅ test_fingerprint_dedup PASS")


async def test_dry_run_semantics():
    """**验收重点**：试跑 = 不发送 + 状态零变化 + 预算不涨 + 冷却不动"""
    sched, sent, tmp = _fresh("dryrun")
    Config.PROACTIVE_GROUPS = [GID]
    task = FakeTask()
    task.defaults["cooldown_minutes"] = 60
    sched.register(task)
    sched._last_activity = lambda g: _day(13, 0)

    # 先真跑一次，留下"已执行"状态
    await sched.tick(now=_day(14))
    assert len(sent) == 1
    state_file = tmp / "schedule" / f"{GID}.json"
    before = json.loads(state_file.read_text(encoding="utf-8"))

    # 试跑：内容要生成，但不能发送、不能改状态
    res = await sched.run_task("fake", GID, dry_run=True)
    assert res.status == "dry_run" and res.parts == ["你好呀"], res
    assert len(sent) == 1, "试跑不该真的发出去"
    after = json.loads(state_file.read_text(encoding="utf-8"))
    assert before == after, f"试跑改了状态：\n{before}\n{after}"

    # 试跑也不该影响冷却：紧接着的 tick 仍处于冷却
    await sched.tick(now=_day(14) + 120)
    assert len(sent) == 1, "试跑不该影响冷却/预算"
    print("   试跑生成了内容，但发送数、状态文件、预算、冷却全部不变")
    print("✅ test_dry_run_semantics PASS")


async def test_manual_run_records():
    sched, sent, tmp = _fresh("manual")
    Config.PROACTIVE_GROUPS = [GID]
    sched.register(FakeTask())
    # 用白天时间戳，避开静默时段的闸门（那是另一条用例专门测的）
    res = await sched.run_task("fake", GID, dry_run=False, now=_day(14))
    assert res.status == "sent" and sent == [(GID, "你好呀")], (res, sent)
    ts = sched.state.task_state(GID, "fake")
    assert ts["last_result"] == "sent" and ts["count"] == 1, ts
    assert sched.state.budget_used_today(GID, _day(14)) == 1
    # 未知任务
    bad = await sched.run_task("不存在", GID, now=_day(14))
    assert bad.status == "error" and "未知任务" in bad.reason
    print("✅ test_manual_run_records PASS")


async def test_future_last_run_not_stuck():
    """时钟回拨/脏数据保护：last_run 在未来不能被永久卡死"""
    sched, sent, _ = _fresh("future")
    Config.PROACTIVE_GROUPS = [GID]
    sched.register(FakeTask())
    sched._last_activity = lambda g: _day(13, 0)
    sched.state.task_state(GID, "fake")["last_run"] = _day(14) + 86400      # 未来的时间戳
    await sched.tick(now=_day(14))
    assert len(sent) == 1, "last_run 在未来时应按'没跑过'处理，而不是永远冷却"
    print("✅ test_future_last_run_not_stuck PASS")


async def test_group_config_priority():
    """群覆盖 > 环境变量 > 代码默认"""
    tmp = TMP / "cfg"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "group_config").mkdir(parents=True, exist_ok=True)
    (tmp / "group_config" / f"{GID}.json").write_text(json.dumps({
        "quiet_hours": ["22:00", "07:00"],
        "daily_total_limit": 3,
        "tasks": {"icebreak": {"trigger": {"type": "idle", "minutes": 99},
                               "cooldown_minutes": 15}},
    }, ensure_ascii=False), encoding="utf-8")

    store = GroupConfigStore(config_dir=tmp / "group_config")
    cfg = store.get(GID)
    assert cfg["quiet_hours"] == "22:00-07:00", cfg["quiet_hours"]
    assert cfg["daily_total_limit"] == 3
    assert cfg["tasks"]["icebreak"]["trigger"]["minutes"] == 99
    assert cfg["tasks"]["icebreak"]["cooldown_minutes"] == 15
    # 没被覆盖的项要保留环境变量默认（破冰的最小群人数来自 Config）
    assert cfg["tasks"]["icebreak"]["min_group_size"] == Config.ICEBREAK_MIN_GROUP_SIZE
    # 另一个群没有文件 → 全默认
    assert store.get(GID2)["quiet_hours"] == Config.QUIET_HOURS
    print(f"   群覆盖生效：quiet={cfg['quiet_hours']} 上限={cfg['daily_total_limit']} "
          f"idle={cfg['tasks']['icebreak']['trigger']['minutes']}min")
    print("✅ test_group_config_priority PASS")


async def test_register_new_task_without_touching_others():
    """验收标准 1：加新任务不改既有代码 —— 调度器/任务基类一行都不用动"""
    sched, sent, _ = _fresh("extend")
    Config.PROACTIVE_GROUPS = [GID]
    sched._last_activity = lambda g: _day(13, 0)

    class WeatherTask(Task):
        name = "weather_morning"
        label = "早间天气"
        trigger_spec = {"type": "cron", "at": "08:00"}
        defaults = {"enabled": True, "cooldown_minutes": 0, "daily_limit": 1}

        def __init__(self):
            super().__init__()
            self.source = CallableSource("weather", lambda ctx: _w(ctx))

    async def _w(ctx):
        return ["今天晴，适合出门"]

    sched.register(WeatherTask())
    await sched.tick(now=_day(8, 0))
    assert sent and sent[0][1] == "今天晴，适合出门", sent
    print("   新任务只靠 Task 子类 + register 就接入了（handler 无需改动）")
    print("✅ test_register_new_task_without_touching_others PASS")


async def test_manual_run_respects_d15_and_quiet_hours():
    """【P2a-hotfix-1】手动"立即执行"也必须过安全闸门

    事故经过（2026-10-08 服务器实测）：管理员在**测试群** `/任务 执行 icebreak`，
    破冰内容被真发进了测试群 —— 因为 run_task 当初谁都不问，绕过了 D15 与静默时段。
    """
    sched, sent, _ = _fresh("manual_gate")
    Config.PROACTIVE_GROUPS = [GID]          # 只有 GID 允许
    sched._group_ids = lambda: [GID, GID2]
    sched._last_activity = lambda g: _day(13, 0)
    sched.register(FakeTask())

    # ① D15：非白名单群的手动执行必须被拒（force 也不放行）
    res = await sched.run_task("fake", GID2, dry_run=False, now=_day(14))
    assert res.status == "blocked" and "主动打扰名单" in res.reason, res
    assert not sent, f"被拒就不该发送：{sent}"
    res_force = await sched.run_task("fake", GID2, dry_run=False, force=True, now=_day(14))
    assert res_force.status == "blocked", "D15 是硬闸门，force 也不该放行"

    # ② 静默时段：默认拒绝，提示里告诉管理员怎么显式强制
    night = _day(3, 0)
    res2 = await sched.run_task("fake", GID, dry_run=False, now=night)
    assert res2.status == "blocked" and "静默时段" in res2.reason, res2
    assert "强制" in res2.reason, res2.reason
    assert not sent, "静默时段被拒也不该发送"
    # force 明确确认后放行（半夜补播的合法场景）
    res3 = await sched.run_task("fake", GID, dry_run=False, force=True, now=night)
    assert res3.status == "sent", res3

    # ③ 试跑不受这些闸门影响（它不发送）
    res4 = await sched.run_task("fake", GID2, dry_run=True, now=night)
    assert res4.status == "dry_run" and res4.parts, res4
    print("   手动执行：非白名单群被拒（force 也无效）、静默时段需显式强制、试跑不受限")
    print("✅ test_manual_run_respects_d15_and_quiet_hours PASS")


async def test_sanitize_at_all():
    """【P2a-hotfix-1/2】主动消息里绝不能出现 @全体成员 或 @自己"""
    from app.schedule.task import sanitize_outgoing

    raw = "@全体成员 今晚有人打魔夜吗，我卡在第七关半小时了……"
    cleaned = sanitize_outgoing(raw)
    assert "@全体成员" not in cleaned and "魔夜" in cleaned, cleaned

    for bad in ("[CQ:at,qq=all] 早上好", "@所有人 起来", "@all hi", "[CQ:at,qq=0] 你好"):
        out = sanitize_outgoing(bad)
        assert "all" not in out.lower() and "全体" not in out and "所有人" not in out, (bad, out)

    # 【hotfix-2】@自己：明文 @昵称（含**简称**）、以及真 CQ 码（后者会触发自我回复循环）
    # 注意服务器上 BOT_NAME=天童爱丽丝，而模型爱写简称「@爱丽丝」—— 第一版只匹配全名，漏了。
    old_name = Config.BOT_NAME
    Config.BOT_NAME = "天童爱丽丝"
    try:
        for raw, expect_left in (
            ("@天童爱丽丝 在吗", "在吗"),
            ("@爱丽丝 在吗", "在吗"),          # ← 简称，第一版漏掉的那种
            ("@丽丝 在吗", "在吗"),
        ):
            out = sanitize_outgoing(raw)
            assert "@" not in out, f"{raw!r} 没清洗干净：{out!r}"
            assert expect_left in out, (raw, out)
    finally:
        Config.BOT_NAME = old_name
    for tpl in ("[CQ:at,qq={}]", "[cq:at,qq={}]"):
        out = sanitize_outgoing(tpl.format(Config.BOT_QQ) + " 有人吗")
        assert str(Config.BOT_QQ) not in out, (tpl, out)
        assert "有人吗" in out, out
    # 正常的 @群友 必须保留（不能误伤合法用法）
    keep = sanitize_outgoing("[CQ:at,qq=888888] 你说的那个我试了")
    assert "888888" in keep, keep

    sched, sent, _ = _fresh("sanitize")
    Config.PROACTIVE_GROUPS = [GID]
    sched._last_activity = lambda g: _day(13, 0)
    sched.register(FakeTask(name="at_all", parts=["@全体成员 快来玩！"]))
    sched.state.task_state(GID, "at_all")["last_run"] = 0
    await sched.tick(now=_day(14))
    assert sent and "@全体成员" not in sent[0][1], f"@全体成员 没被清洗：{sent}"
    assert "快来玩" in sent[0][1], sent
    print(f"   发送内容已清洗：{sent[0][1]!r}")
    print("   @自己（全名/简称/CQ码）已清洗，@群友 保留")
    print("✅ test_sanitize_at_all PASS")


async def _run_all():
    test_trigger_basics()
    test_cron_boundaries()
    test_quiet_hours_and_work_hours()
    await test_daily_limits()
    await test_cooldown_and_gates()
    await test_d15_and_maintenance_task()
    await test_backoff_on_no_reply()
    await test_empty_and_error()
    await test_fingerprint_dedup()
    await test_dry_run_semantics()
    await test_manual_run_records()
    await test_manual_run_respects_d15_and_quiet_hours()
    await test_sanitize_at_all()
    await test_future_last_run_not_stuck()
    await test_group_config_priority()
    await test_register_new_task_without_touching_others()


if __name__ == "__main__":
    print("=== P2a 调度框架回归测试 ===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：触发/打扰预算/D15/退避/干跑语义/配置优先级 都符合设计")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
