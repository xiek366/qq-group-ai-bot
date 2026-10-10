"""
P2a 端到端干跑验证：调度框架 + 手动入口
=======================================
把 P2a 的交付串起来（**不联网、不发真实群消息**，全桩件），判定标准是 DEVLOG [P2a-0] §5：

  ① handler 启动后确实注册了 icebreak / summary_job 两个任务（加任务不用改 handler）
  ② `/任务 列表`（管理员）→ **私聊**收到任务状态，群里一条不发
  ③ `/任务 试跑 icebreak` → 私聊收到生成内容；**群里零打扰、状态文件零变化**
  ④ `/任务 执行 icebreak` → 真的发到群里了，并且记了账（last_run/当日次数/预算）
  ⑤ 非管理员用 `/任务` → 被拒绝
  ⑥ D15：tick 一轮，主动任务只落在白名单群（测试群/放置群被拦）
  ⑦ 静默时段：整轮 tick 一条主动消息都不发
  ⑧ summary_job 是维护型（不受 D15 限制、不吃打扰预算），跑通一次摘要

运行: python tests/test_p2a_e2e.py
（全部数据写临时目录，不碰生产数据）
"""
import asyncio
import json
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

PROD_GID = 100000004          # 实例群：唯一允许主动打扰
TEST_GID = 100000005         # 测试群：不允许
IDLE_GID = 100000006          # 放置群：不允许
ADMIN_UID = 100000008
TMP = Path("data/_tmp_p2a_e2e")

SUMMARY_REPLY = """【要点】
1. 大家在约周末开黑

【成员】
小明: 喜欢原神

【当前话题】
周末开黑
"""


class StubOneBot:
    ws_connected = True

    def __init__(self):
        self.group_sent: list = []
        self.private_sent: list = []
        self.private_ok = True

    async def send_group_message(self, gid, text):
        self.group_sent.append((gid, text))

    async def send_private_message(self, uid, text):
        if self.private_ok:
            self.private_sent.append((uid, text))
        return self.private_ok

    async def get_group_members(self, gid):
        return [{"user_id": i, "nickname": f"u{i}", "card": ""} for i in range(10)]


class StubLLM:
    """既能当闲聊模型，也能当摘要模型"""

    def __init__(self):
        self.chat_calls = 0
        self.summary_calls = 0

    async def chat(self, messages, system_prompt="", **kwargs):
        content = messages[0].get("content", "") if messages else ""
        if "【本次任务】" in content and "长期记忆" in content:
            self.summary_calls += 1
            return SUMMARY_REPLY
        if "安静了" in content:            # 破冰话题包
            return "话说你们最近在玩什么游戏呀？"
        self.chat_calls += 1
        return "收到啦～"


def _event(segments, gid, uid=ADMIN_UID, nick="小红"):
    return {"post_type": "message", "message_type": "group", "group_id": gid,
            "message_id": 1, "message": segments,
            "sender": {"user_id": uid, "nickname": nick}}


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _state_file(gid: int) -> Path:
    return TMP / "schedule" / f"{gid}.json"


async def main():
    shutil.rmtree(TMP, ignore_errors=True)
    isolate_writable_state(TMP)
    Config.CHAT_REPLY_CHANCE = 0          # 关掉随机插嘴，保证断言稳定
    Config.PROACTIVE_GROUPS = [PROD_GID]
    Config.ADMIN_QQ = str(ADMIN_UID)
    Config.SUMMARY_MIN_MESSAGES = 5
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    Config.DAILY_TOTAL_LIMIT = 8
    Config.QUIET_HOURS = "23:00-08:00"

    onebot = StubOneBot()
    llm = StubLLM()
    handler = MessageHandler(
        onebot=onebot, llm_provider=llm, search_provider=None,
        memory=GroupMemory(persist=False, stream=ConversationStream(
            stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")),
        persona="测试人设：爱丽丝")
    if handler._scheduler_task:
        handler._scheduler_task.cancel()

    async def feed(segments, gid=PROD_GID, uid=ADMIN_UID, nick="小红"):
        onebot.group_sent.clear()
        parsed = parse_message(_event(segments, gid, uid=uid, nick=nick))
        assert parsed, f"解析失败: {segments}"
        await handler.handle(parsed)
        return list(onebot.group_sent)

    day = clock.now().replace(hour=14, minute=0, second=0, microsecond=0).timestamp()

    # ---------- ① 任务已注册（加任务不用改 handler） ----------
    names = sorted(handler.scheduler.tasks)
    assert "icebreak" in names and "summary_job" in names, names
    assert handler.scheduler.tasks["icebreak"].is_proactive is True
    assert handler.scheduler.tasks["summary_job"].is_proactive is False
    assert handler.scheduler.tasks["summary_job"].silent_ok is True
    print(f"   ① 已注册任务：{names}（破冰=主动型，摘要=维护型）")

    # 先让群"活跃过"，调度器才知道有这个群
    await feed([_text("大家好啊")])
    onebot.group_sent.clear()

    # ---------- ② /任务 列表 → 私聊，群里零打扰 ----------
    await feed([_text("/任务 列表")])
    assert not onebot.group_sent, f"列表不该发到群里：{onebot.group_sent}"
    assert onebot.private_sent and onebot.private_sent[-1][0] == ADMIN_UID
    listing = onebot.private_sent[-1][1]
    assert "任务状态" in listing and "icebreak" in listing and "summary_job" in listing, listing[:200]
    assert "今日主动消息" in listing
    print("   ② /任务 列表 → 私聊收到任务状态，群里零打扰")

    # ---------- ③ /任务 试跑 → 生成但不发送、状态零变化 ----------
    before = _state_file(PROD_GID).read_text(encoding="utf-8") if _state_file(PROD_GID).exists() else ""
    await feed([_text("/任务 试跑 icebreak")])
    assert not onebot.group_sent, f"试跑不该发到群里：{onebot.group_sent}"
    trial = onebot.private_sent[-1][1]
    assert "试跑" in trial and "未发送" in trial, trial
    assert "游戏" in trial, f"试跑应该带上生成的内容：{trial}"
    after = _state_file(PROD_GID).read_text(encoding="utf-8") if _state_file(PROD_GID).exists() else ""
    assert before == after, "试跑不该改动任务状态"
    print("   ③ /任务 试跑 → 私聊拿到内容，群里零打扰、状态零变化")

    # ---------- ④ /任务 执行 → 真发 + 记账 ----------
    # 带「强制」是因为这条用例可能跑在静默时段（深夜）：静默时段的拒绝由单测专门覆盖，
    # 这里要验的是"手动执行这条链路能真发 + 记账"。
    await feed([_text("/任务 执行 icebreak 强制")])
    assert onebot.group_sent and PROD_GID == onebot.group_sent[-1][0], onebot.group_sent
    st = json.loads(_state_file(PROD_GID).read_text(encoding="utf-8"))
    ice = st["tasks"]["icebreak"]
    assert ice["last_result"] == "sent" and ice["count"] == 1, ice
    assert st["budget"]["total"] == 1, st["budget"]
    assert "已发送" in onebot.private_sent[-1][1]
    # 破冰内容也进了对话流（bot 记得自己抛过什么）
    kinds = [e["kind"] for e in handler.memory.stream.window(PROD_GID, limit=10)]
    assert "proactive" in kinds, kinds
    print("   ④ /任务 执行 → 真发到群里 + 记账（last_run/当日次数/预算）+ 进对话流")

    # ---------- ④b 事故回归：测试群里手动执行也必须被拒（连「强制」都不行）----------
    budget_before = json.loads(_state_file(TEST_GID).read_text(encoding="utf-8"))["budget"]["total"] \
        if _state_file(TEST_GID).exists() else 0
    sent = await feed([_text("/任务 执行 icebreak 强制")], gid=TEST_GID)
    assert not sent, f"D15 是硬闸门：测试群手动执行也不该发送，实际 {sent}"
    blocked_msg = onebot.private_sent[-1][1]
    assert "主动打扰名单" in blocked_msg and "拦下" in blocked_msg, blocked_msg
    budget_after = json.loads(_state_file(TEST_GID).read_text(encoding="utf-8"))["budget"]["total"] \
        if _state_file(TEST_GID).exists() else 0
    assert budget_after == budget_before, "被拒的执行不该记预算"
    print("   ④b 测试群 /任务 执行（含强制）→ 被 D15 拦下，未发送、未记账")

    # ---------- ④c 内容清洗：@全体成员 绝不能出现在主动消息里 ----------
    from app.schedule.task import sanitize_outgoing
    assert "@全体成员" not in sanitize_outgoing("@全体成员 快来玩")
    assert "all" not in sanitize_outgoing("[CQ:at,qq=all] 快来玩").lower()
    print("   ④c 主动消息清洗：@全体成员 / [CQ:at,qq=all] 会被去掉")

    # ---------- ⑤ 非管理员被拒 ----------
    sent = await feed([_text("/任务 列表")], uid=888888, nick="路人")
    assert sent and "只有制作者" in sent[0][1], sent
    print("   ⑤ 非管理员用 /任务 → 被拒绝")

    # ---------- ⑥ D15：tick 只动白名单群 ----------
    for gid in (PROD_GID, TEST_GID, IDLE_GID):
        await feed([_text("随便聊聊")], gid=gid)
    handler.scheduler._last_activity = lambda g: day - 3600     # 全部"已安静 1 小时"
    handler.scheduler._members.clear()
    handler.scheduler._last_proactive.clear()
    for gid in (PROD_GID, TEST_GID, IDLE_GID):
        handler.scheduler.state.clear(gid)
    onebot.group_sent.clear()
    await handler.scheduler.tick(now=day)
    active = {g for g, _ in onebot.group_sent}
    assert PROD_GID in active, f"实例群应该收到主动消息：{onebot.group_sent}"
    assert TEST_GID not in active and IDLE_GID not in active, f"D15 失效：{onebot.group_sent}"
    print(f"   ⑥ D15：主动消息只到 {sorted(active)}（测试群/放置群被拦）")

    # ---------- ⑦ 静默时段：一条主动消息都不发 ----------
    for gid in (PROD_GID, TEST_GID, IDLE_GID):
        handler.scheduler.state.clear(gid)
    handler.scheduler._last_proactive.clear()
    onebot.group_sent.clear()
    night = clock.now().replace(hour=3, minute=0, second=0, microsecond=0).timestamp()
    await handler.scheduler.tick(now=night)
    assert not onebot.group_sent, f"静默时段不该有主动消息：{onebot.group_sent}"
    assert handler.scheduler.state.task_state(PROD_GID, "icebreak")["last_skip_reason"] == "静默时段"
    print("   ⑦ 静默时段（03:00）：主动消息一条都没发")

    # ---------- ⑧ summary_job：维护型，不受 D15 限制、不吃打扰预算 ----------
    for i in range(6):
        await feed([_text(f"第{i}条：周末开黑的事再确认一下")], gid=TEST_GID)
    budget_before = json.loads(_state_file(TEST_GID).read_text(encoding="utf-8"))["budget"]["total"]
    await handler.scheduler.tick(now=day)
    state = json.loads((TMP / "summary" / f"{TEST_GID}.json").read_text(encoding="utf-8"))
    assert state["bullets"], state
    budget_after = json.loads(_state_file(TEST_GID).read_text(encoding="utf-8"))["budget"]["total"]
    assert budget_after == budget_before, "维护型任务不该消耗打扰预算"
    assert llm.summary_calls >= 1
    print(f"   ⑧ summary_job 在测试群也跑通（要点 {len(state['bullets'])} 条），且不占打扰预算")

    # ---------- ⑨ 面板：任务数据进健康卡 + /api/task 路由已注册 ----------
    from app.dashboard.health import build_health_payload
    payload = await build_health_payload(handler)
    assert "tasks" in payload, list(payload)
    assert str(PROD_GID) in payload["tasks"], list(payload["tasks"])
    pinfo = payload["tasks"][str(PROD_GID)]
    assert "icebreak" in pinfo["tasks"], pinfo["tasks"]
    assert "budget_used" in pinfo and "budget_limit" in pinfo, pinfo
    ice_card = pinfo["tasks"]["icebreak"]
    for key in ("enabled", "trigger", "next_eta", "last_run", "count_today", "backoff_level"):
        assert key in ice_card, f"任务卡片缺少字段 {key}: {ice_card}"
    print(f"   ⑨ 健康卡带上了任务数据（{len(payload['tasks'])} 个群 × "
          f"{len(pinfo['tasks'])} 个任务）")

    # 路由注册：本地没装 aiohttp，用测试替身验证"我的接线"（真机仍需 curl 复核）
    import sys as _sys
    import types as _types
    saved = {k: _sys.modules.get(k) for k in ("aiohttp", "aiohttp.web", "app.dashboard.server")}

    class _Resp:
        def __init__(self, payload=None, status=200):
            self.payload = payload
            self.status = status

    class _Router:
        def __init__(self):
            self.routes = []

        def add_get(self, path, handler):
            self.routes.append(("GET", path, handler))

        def add_post(self, path, handler):
            self.routes.append(("POST", path, handler))

    class _App:
        def __init__(self):
            self.router = _Router()

    fake_aiohttp = _types.ModuleType("aiohttp")
    fake_web = _types.ModuleType("aiohttp.web")
    fake_web.Application = _App
    fake_web.Request = object
    fake_web.Response = _Resp
    fake_web.StreamResponse = _Resp
    fake_web.json_response = lambda payload, status=200: _Resp(payload, status)
    fake_aiohttp.web = fake_web
    _sys.modules["aiohttp"] = fake_aiohttp
    _sys.modules["aiohttp.web"] = fake_web
    _sys.modules.pop("app.dashboard.server", None)
    try:
        from app.dashboard import server as srv
        app = srv.build_app(onebot, handler, 0.0)
        routes = {(m, p) for m, p, _ in app.router.routes}
        assert ("POST", "/api/task") in routes, routes
        assert ("GET", "/api/health") in routes, routes

        class _Req:
            def __init__(self, token, body=None):
                self.query = {"token": token}
                self._body = body or {}

            async def json(self):
                return self._body

        bad = await srv.api_task(_Req("wrong-token"))
        assert bad.status == 401, bad.status
        listed = await srv.api_task(_Req(Config.DASHBOARD_TOKEN, {"action": "list", "group": PROD_GID}))
        assert listed.status == 200 and "tasks" in listed.payload, listed.payload
        dry = await srv.api_task(_Req(Config.DASHBOARD_TOKEN,
                                      {"action": "dry", "group": PROD_GID, "task": "icebreak"}))
        assert dry.status == 200 and dry.payload["status"] == "dry_run", dry.payload
        assert dry.payload["parts"], dry.payload
        missing = await srv.api_task(_Req(Config.DASHBOARD_TOKEN, {"action": "run"}))
        assert missing.status == 400, missing.status
        print("   ⑨b /api/task：401 鉴权、list、dry（返回内容但不发送）、缺参数 400 全部正确")
    finally:
        for key, mod in saved.items():
            if mod is None:
                _sys.modules.pop(key, None)
            else:
                _sys.modules[key] = mod

    print("\n🎉 P2a 干跑验证通过（调度框架 / D15 / 打扰预算 / 手动入口 三件套 / 摘要迁入 / 面板）")


if __name__ == "__main__":
    print("=== P2a 端到端干跑验证 ===")
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
