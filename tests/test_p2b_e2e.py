"""
P2b 端到端干跑验证：天气播报 + 喝水提醒
=======================================
用真实 handler + 调度框架跑一遍（**不联网、不发真实群消息**），判定标准是 DEVLOG [P2b-0] §5：

  ① 08:00 整点：绑了城市的实例群收到播报，且**未绑城市的群一条都不发**
  ② 播报内容随天气变化（换一份数据 → 注意事项跟着变）
  ③ 同城多群只请求一次 API（缓存生效），不同城市互不串
  ④ API 失败 → 一条都不发（绝不发占位内容）
  ⑤ 12:00 群活跃 → 喝水/久坐提醒发出；**一天不超过 4 条**，且文案交替
  ⑥ 死群（最近没人说话）不提醒
  ⑦ `/天气 北京` 在**测试群**也能用（被动响应，不受 D15 限制）；未收录城市给明确提示
  ⑧ 天气 API 失败时 `/天气` 明确说"查不到"，不猜

运行: python tests/test_p2b_e2e.py
（全部缓存/状态写临时目录，不碰生产数据）
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_isolate import isolate_writable_state, reset_writable_state
from app.bot.handler import MessageHandler
from app.bot.message import parse_message
from app.config import Config
from app.context.stream import ConversationStream
from app.memory.memory import GroupMemory
from app.util import clock
from app.util.blocking import shutdown_blocking
from app.weather import WeatherDaily, WeatherService

PROD_GID = 100000004          # 实例群：绑北京，主动功能全开
TEST_GID = 100000005         # 测试群：绑北京但主动全关（靠命令验证）
NOCITY_GID = 100000070        # 未绑城市的群
ADMIN_UID = 100000008
TMP = Path("data/_tmp_p2b_e2e")


class StubOneBot:
    ws_connected = True

    def __init__(self):
        self.group_sent = []
        self.private_sent = []

    async def send_group_message(self, gid, text):
        self.group_sent.append((gid, text))

    async def send_private_message(self, uid, text):
        self.private_sent.append((uid, text))
        return True

    async def get_group_members(self, gid):
        return [{"user_id": i, "nickname": f"u{i}", "card": ""} for i in range(10)]


class StubLLM:
    def __init__(self):
        self.calls = 0
        self.polish_calls = 0

    async def chat(self, messages, system_prompt="", **kwargs):
        self.calls += 1
        content = messages[0].get("content", "") if messages else ""
        if "收尾" in content or "天气" in content:
            self.polish_calls += 1
            return "早安！今天也要元气满满地冒险哦 ✨"
        return "收到啦～"


class StubProvider:
    """可切换返回值的天气 provider：seed 控制"天气"，fail 控制"API 挂了" """

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.seed = 0

    async def fetch_daily(self, city):
        self.calls += 1
        if self.fail:
            return None
        if self.seed == 0:
            # 典型"要带伞 + 温差大 + 大风"的天气
            return WeatherDaily(city=city, date=WeatherService.today(), code=61, desc="小雨",
                                tmax=22.0, tmin=11.0, precip_prob=70, wind_max=33.0, uv_max=3.0)
        # 换成"高温暴晒"的天气 → 注意事项应该完全不同
        return WeatherDaily(city=city, date=WeatherService.today(), code=0, desc="晴",
                            tmax=35.0, tmin=30.0, precip_prob=0, wind_max=8.0, uv_max=9.0)


def _event(segments, gid, uid=ADMIN_UID, nick="小红"):
    return {"post_type": "message", "message_type": "group", "group_id": gid,
            "message_id": 1, "message": segments,
            "sender": {"user_id": uid, "nickname": nick}}


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _day(hour: int, minute: int = 0) -> float:
    return clock.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


async def main():
    shutil.rmtree(TMP, ignore_errors=True)
    isolate_writable_state(TMP)
    Config.CHAT_REPLY_CHANCE = 0
    Config.PROACTIVE_GROUPS = [PROD_GID, NOCITY_GID]   # 两个群都允许主动，但只有 PROD 绑了城市
    Config.ADMIN_QQ = str(ADMIN_UID)
    Config.WEATHER_ENABLED = True
    Config.WEATHER_CITY = ""                           # 不用全局兜底，靠群配置
    Config.WEATHER_POLISH = True
    Config.WATER_REMINDER_ENABLED = True
    Config.WATER_REMINDER_DAILY_LIMIT = 4
    Config.DAILY_TOTAL_LIMIT = 20                      # 本用例要连发多条提醒，别被总预算挡住
    Config.QUIET_HOURS = "23:00-08:00"

    # 群城市绑定（真实部署时这个文件由服务器侧创建）
    (TMP / "group_config").mkdir(parents=True, exist_ok=True)
    import json
    (TMP / "group_config" / f"{PROD_GID}.json").write_text(
        json.dumps({"weather": {"city": "北京", "broadcast_at": "08:00"}}, ensure_ascii=False),
        encoding="utf-8")
    (TMP / "group_config" / f"{TEST_GID}.json").write_text(
        json.dumps({"weather": {"city": "北京"}}, ensure_ascii=False), encoding="utf-8")

    provider = StubProvider()
    llm = StubLLM()
    onebot = StubOneBot()
    handler = MessageHandler(
        onebot=onebot, llm_provider=llm, search_provider=None,
        memory=GroupMemory(persist=False, stream=ConversationStream(
            stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")),
        persona="测试人设：爱丽丝")
    if handler._scheduler_task:
        handler._scheduler_task.cancel()
    # 天气服务换成桩（不联网），其余走真实逻辑
    handler.weather = WeatherService(provider=provider, cache_dir=TMP / "weather")
    for name, task in handler.scheduler.tasks.items():
        if hasattr(task, "service"):
            task.service = handler.weather
        if hasattr(task, "_polish"):
            task._polish = handler._polish_weather_finish
    sched = handler.scheduler

    async def feed(segments, gid):
        onebot.group_sent.clear()
        parsed = parse_message(_event(segments, gid))
        assert parsed, f"解析失败: {segments}"
        await handler.handle(parsed)
        return list(onebot.group_sent)

    # 让调度器认识这三个群（活跃时间来自真实消息处理）
    for gid in (PROD_GID, TEST_GID, NOCITY_GID):
        await feed([_text("早啊")], gid)
    onebot.group_sent.clear()
    active_ts = _day(11, 30)                 # "群最后活跃时间"：11:30
    sched._last_activity = lambda g: active_ts
    sched._members.clear()
    sched._last_proactive.clear()
    for gid in (PROD_GID, TEST_GID, NOCITY_GID):
        sched.state.clear(gid)

    # ---------- ① 08:00 播报：只有绑了城市的群发 ----------
    results = await sched.tick(now=_day(8, 0))
    got = {g for g, _ in onebot.group_sent}
    assert PROD_GID in got, f"实例群应该收到播报：{results}"
    assert TEST_GID not in got, "测试群不该收到（D15 主动名单）"
    assert NOCITY_GID not in got, f"未绑城市的群不该收到：{onebot.group_sent}"
    assert "未绑定城市" in sched.state.task_state(NOCITY_GID, "weather_morning")["last_skip_reason"], \
        sched.state.task_state(NOCITY_GID, "weather_morning")["last_skip_reason"]
    broadcast = dict(onebot.group_sent)[PROD_GID]
    assert "北京" in broadcast and "伞" in broadcast, broadcast
    assert "元气满满" in broadcast, f"收尾应该用 LLM 润色结果：{broadcast}"
    assert provider.calls == 1, f"同城只该请求一次：{provider.calls}"
    print(f"   ① 08:00 播报 → 只发到 {sorted(got)}；未绑城市的群被跳过（API 请求 {provider.calls} 次）")

    # ---------- ② 内容随天气变化 ----------
    provider.seed = 1                        # 换成高温暴晒
    provider.calls = 0
    # ⚠️ 必须换一个全新的缓存目录：同一天同城**本来就该命中缓存**（上一步已经存了雨天数据），
    #    这里相当于"第二天/换了数据源"，用来验证注意事项确实跟着天气走。
    day2_service = WeatherService(provider=provider, cache_dir=TMP / "weather_day2")
    for name, task in sched.tasks.items():
        if hasattr(task, "service"):
            task.service = day2_service
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(9, 0))         # 09:00 在 cron 容忍窗口内（迟到 ≤90 分）
    hot = dict(onebot.group_sent).get(PROD_GID, "")
    assert "防暑" in hot and "伞" not in hot, f"高温天气的注意事项应该不同：{hot}"
    print(f"   ② 换天气数据 → 注意事项跟着变（高温 → 防暑；雨天 → 带伞）")

    # ---------- ③ 缓存：同城不重复请求 ----------
    provider.seed = 0
    provider.calls = 0
    cache_dir = TMP / "weather_cache_test"
    fresh = WeatherService(provider=provider, cache_dir=cache_dir)
    d1 = await fresh.fetch_daily("北京")
    assert d1 is not None and provider.calls == 1, provider.calls
    d2 = await fresh.fetch_daily("北京")
    assert d2 is not None and provider.calls == 1, f"同进程内第二次应命中内存缓存：{provider.calls}"
    other = await fresh.fetch_daily("上海")
    assert other is not None and other.city == "上海" and provider.calls == 2, provider.calls
    # 另一个实例（模拟"另一个群/重启"）读同一份磁盘缓存 → 不再请求（同城多群只请求一次）
    another = WeatherService(provider=provider, cache_dir=cache_dir)
    assert another.get_daily("北京") is not None
    await another.fetch_daily("北京")
    assert provider.calls == 2, f"已有当日磁盘缓存不该再请求：{provider.calls}"
    print(f"   ③ 缓存：北京 1 次、上海 1 次；换实例读磁盘缓存也不再请求（互不串）")

    # ---------- ④ API 失败 → 一条都不发 ----------
    provider.fail = True
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    fresh2 = WeatherService(provider=provider, cache_dir=TMP / "weather_fail")
    for name, task in sched.tasks.items():
        if hasattr(task, "service"):
            task.service = fresh2
    # 用 08:30：在 cron 的 90 分钟容忍窗口内（10:00 已超出 → 会被判成"没到点"而不是"取数失败"）
    await sched.tick(now=_day(8, 30))
    # 注意：这个点喝水提醒不会发（窗口 10:00 起），所以只断言"天气相关的一条都不发"
    weather_sent = [(g, t) for g, t in onebot.group_sent if "℃" in t or "北京" in t or "伞" in t]
    assert not weather_sent, f"天气 API 挂了不该发播报：{weather_sent}"
    assert sched.state.task_state(PROD_GID, "weather_morning")["last_skip_reason"].startswith("取不到")
    provider.fail = False
    print("   ④ 天气 API 失败 → 播报一条都不发（不硬发占位内容）")

    # ---------- ⑤ 12:00 喝水提醒：发出且交替，一天 ≤4 ----------
    # ⚠️ 这个时间窗里 P2c 的每日一句/资讯等任务也可能触发，所以只统计"提醒文案"
    from app.schedule.tasks.water_reminder import SIT_TEXTS, WATER_TEXTS

    def _is_reminder(text: str) -> bool:
        return any(pool in text for pool in WATER_TEXTS + SIT_TEXTS)

    for name, task in sched.tasks.items():
        if hasattr(task, "service"):
            task.service = handler.weather
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    sched._last_activity = lambda g: _day(12, 0)      # 群很活跃
    onebot.group_sent.clear()
    sent_texts = []
    for i in range(5):                                 # 连跑 5 轮 tick，间隔都够
        now = _day(12, 0) + i * 90 * 60
        sched._last_activity = lambda g, _n=now: _n - 60   # 每轮都"刚刚有人说话"
        await sched.tick(now=now)
        sent_texts += [t for g, t in onebot.group_sent if g == PROD_GID and _is_reminder(t)]
        onebot.group_sent.clear()
    assert len(sent_texts) == 4, f"每日上限 4 条，实际 {len(sent_texts)}：{sent_texts}"
    assert sent_texts[0] != sent_texts[1], "文案应该轮换，不要连着重复"
    water_like = sum(1 for t in sent_texts if any(k in t for k in ("水", "HP 回满", "补水")))
    assert 0 < water_like < len(sent_texts), f"喝水与久坐应该交替：{sent_texts}"
    assert sched.state.count_today(PROD_GID, "water_reminder", now=_day(12, 0)) == 4, \
        f"当日计数应该是 4：{sched.state.count_today(PROD_GID, 'water_reminder', now=_day(12, 0))}"
    # 第 5 次被拦的原因可能是"已达上限"或"同群互斥"（同一时刻别的任务先发了），
    # 所以这里只断言"计数封顶"，原因由 test_p2a_schedule 的上限用例专测。
    print(f"   ⑤ 12:00 提醒：共 {len(sent_texts)} 条（上限 4），文案交替 → {sent_texts[0][:18]}… / {sent_texts[1][:18]}…")

    # ---------- ⑥ 死群不提醒 ----------
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    sched._last_activity = lambda g: _day(6, 0)        # 6 小时前才有人说话
    onebot.group_sent.clear()
    await sched.tick(now=_day(13, 0))
    # 这一刻破冰可能该触发（群安静够久），所以只断言"提醒文案一条都没发"
    reminder_sent = [(g, t) for g, t in onebot.group_sent
                     if any(pool in t for pool in WATER_TEXTS + SIT_TEXTS)]
    assert not reminder_sent, f"死群不该收到提醒：{reminder_sent}"
    assert sched.state.task_state(PROD_GID, "water_reminder")["last_skip_reason"].startswith("群最近")
    print("   ⑥ 死群（6 小时没人说话）→ 不提醒")

    # ---------- ⑦ /天气 在测试群可用（被动响应） ----------
    sent = await feed([_text("/天气 北京")], TEST_GID)
    assert sent and "北京" in sent[0][1], sent
    assert "℃" in sent[0][1], sent
    sent = await feed([_text("/天气 上海")], TEST_GID)
    assert sent and "上海" in sent[0][1], sent
    # 不带参数 → 用本群绑定的城市（测试群绑了北京）
    sent = await feed([_text("/天气")], TEST_GID)
    assert sent and "北京" in sent[0][1], sent
    # 未收录城市 → 明确提示，不猜
    sent = await feed([_text("/天气 火星")], TEST_GID)
    assert sent and "还没收录" in sent[0][1], sent
    print("   ⑦ /天气 在测试群可用（被动响应不受 D15 限制）；未收录城市给明确提示")

    # ---------- ⑧ /天气 取数失败 → 明确说查不到 ----------
    fail_weather = WeatherService(provider=StubProvider_fail(), cache_dir=TMP / "weather_cmd_fail")
    handler.weather = fail_weather
    sent = await feed([_text("/天气 济南")], TEST_GID)
    assert sent and "查不到" in sent[0][1], sent
    print("   ⑧ /天气 取数失败 → 明确回「查不到」，不猜")

    print("\n🎉 P2b 干跑验证通过（播报 / 城市绑定 / 缓存隔离 / 降级 / 提醒节流 / 命令复用）")


class StubProvider_fail:
    async def fetch_daily(self, city):
        return None


if __name__ == "__main__":
    print("=== P2b 端到端干跑验证 ===")
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
