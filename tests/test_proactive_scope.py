"""
主动打扰范围（D15）回归测试
============================
要求（用户明确指定）：**定时/主动功能只在实例群 100000004 使用，
测试群 100000005 与放置群 100000006 一律不打扰**；被动响应不受限制。

覆盖：
  1. 名单内的群：主动插嘴正常触发
  2. 名单外的群：主动插嘴被拦住（普通消息无人应答）
  3. 名单外的群：**裸图自动回复**被拦住（要识图得显式 /看，那是被动响应）
  4. 名单外的群：**破冰**被拦住（【P2a】起由调度框架判定，用真实 tick 验证）
  5. 名单为空 = 不限制（兼容旧行为）
  6. 被动响应在任何群都能用：@bot 有回复、/ping 有回复

运行: python tests/test_proactive_scope.py
（全部数据写临时目录，不碰生产数据）
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
from app.vision.cache import VisionCache
from app.vision.pipeline import VisionPipeline

PROD_GID = 100000004      # 实例群（唯一允许主动打扰）
TEST_GID = 100000005     # 测试群（不允许）
IDLE_GID = 100000006      # 放置群（不允许）
TMP = Path("data/_tmp_scope_test")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
IMG_URL = "https://example.com/a.png"


class StubOneBot:
    ws_connected = True

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_group_message(self, group_id, text):
        self.sent.append((group_id, text))

    async def get_group_members(self, group_id):
        return [{"user_id": 1, "nickname": "甲", "card": ""},
                {"user_id": 2, "nickname": "乙", "card": ""},
                {"user_id": 3, "nickname": "丙", "card": ""}]


class StubLLM:
    async def chat(self, messages, **kwargs):
        return "收到啦～"

    async def chat_with_images(self, image_urls, prompt="", **kwargs):
        return "一只猫"


def _event(segments, gid):
    return {
        "post_type": "message", "message_type": "group", "group_id": gid,
        "message_id": 1, "message": segments,
        "sender": {"user_id": 888888, "nickname": "小明"},
    }


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _image(url=IMG_URL):
    return {"type": "image", "data": {"url": url, "file": "a.png", "sub_type": 0, "summary": ""}}


def _isolate_style_dir():
    """把风格画像也指到临时目录。

    ⚠️ 本测试**故意使用真实群号**（100000004/100000005/100000006）当文档，
    但 MessageHandler 里构造的 StyleProfile 会往 `data/group_style/<群号>.json` 写东西 ——
    不隔离就会污染真实数据（本地审计抓到过一次多出来的 100000004.json）。
    现在统一走 tests/helpers_isolate.py，将来新增会落盘的单例也只需在那里加一行。
    """
    isolate_writable_state(TMP)


def _make_handler() -> tuple[StubOneBot, MessageHandler]:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    _isolate_style_dir()

    onebot = StubOneBot()
    handler = MessageHandler(
        onebot=onebot, llm_provider=StubLLM(), search_provider=None,
        memory=GroupMemory(persist=False, stream=ConversationStream(
            stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")),
        persona="测试人设",
    )
    if handler._scheduler_task:
        handler._scheduler_task.cancel()
    handler.vision = VisionPipeline(
        cache=VisionCache(cache_dir=TMP / "vcache", ttl_days=30),
        fetcher=lambda u: _fetch(u),
    )
    return onebot, handler


async def _fetch(url):
    return PNG if url == IMG_URL else None


async def _feed(handler, onebot, gid, segments):
    onebot.sent.clear()
    parsed = parse_message(_event(segments, gid))
    assert parsed, segments
    await handler.handle(parsed)
    return list(onebot.sent)


async def main():
    # 只允许实例群主动打扰；插嘴概率拉满，确保"能触发就一定触发"
    Config.PROACTIVE_GROUPS = [PROD_GID]
    Config.CHAT_REPLY_CHANCE = 100
    Config.CHAT_COOLDOWN = 0
    Config.CHAT_KEYWORDS = []

    onebot, handler = _make_handler()
    try:
        # ---------- 1) 名单内的群：主动插嘴正常 ----------
        sent = await _feed(handler, onebot, PROD_GID, [_text("今天天气不错啊")])
        assert sent, "实例群应该被主动插嘴"
        print("   ① 实例群：主动插嘴正常触发")

        # ---------- 2) 名单外的群：主动插嘴被拦住 ----------
        sent = await _feed(handler, onebot, TEST_GID, [_text("今天天气不错啊")])
        assert not sent, f"测试群不该被主动插嘴，但它发了：{sent}"
        sent = await _feed(handler, onebot, IDLE_GID, [_text("今天天气也不错")])
        assert not sent, f"放置群不该被主动插嘴，但它发了：{sent}"
        print("   ② 测试群/放置群：主动插嘴被拦住（普通消息无人应答）")

        # ---------- 3) 名单外的群：裸图自动回复被拦住 ----------
        sent = await _feed(handler, onebot, TEST_GID, [_image()])
        assert not sent, f"测试群不该自动接图，但它发了：{sent}"
        # 但显式 /看 是**被动响应**，必须照常工作
        sent = await _feed(handler, onebot, TEST_GID, [_text("/看"), _image()])
        assert sent and "收到啦" in sent[0][1], f"测试群 /看 不该被限制：{sent}"
        print("   ③ 测试群：裸图被拦住，但 /看（被动响应）正常工作")

        # ---------- 4) 名单外的群：破冰被拦住（【P2a】改由调度框架判定）----------
        # 破冰已迁入调度框架：真正决定"哪个群能破冰"的是 Scheduler 的 D15 判断。
        # 这里用真实注册的 icebreak 任务跑一轮 tick，断言只有实例群被破冰。
        from app.schedule.task import PRIORITY_ICEBREAK, CallableSource, Task

        fired: list[int] = []

        class _ProbeIcebreak(Task):
            name = "icebreak_probe"
            label = "破冰探针"
            priority = 10            # 比真实 icebreak 更优先，确保本轮由它代表"破冰"
            is_proactive = True
            trigger_spec = {"type": "idle", "minutes": 1}
            defaults = {"enabled": True, "cooldown_minutes": 0, "daily_limit": 0,
                        "min_group_size": 0, "require_active_within_minutes": 0,
                        "work_hours": [0, 24], "backoff": {"on_no_reply": False}}

            def __init__(self):
                super().__init__()
                self.source = CallableSource("probe", self._r)

            async def _r(self, ctx):
                fired.append(ctx.group_id)
                return ["该破冰啦"]

        # 用 handler 真正的调度器（它已经注册了 icebreak + summary_job），再挂一个探针
        sched = handler.scheduler
        sched.register(_ProbeIcebreak())
        sched._last_activity = lambda g: 0.0            # 所有群都"早就安静"
        sched._member_count = lambda g: _count(10)
        sched._members.clear()
        sched._last_proactive.clear()
        sched.state.clear(PROD_GID)                     # 清掉状态，避免冷却/退避干扰
        sched.state.clear(TEST_GID)
        sched.state.clear(IDLE_GID)

        # 挑一个白天的时间戳（避开静默时段），让 D15 成为唯一变量
        day = clock.now().replace(hour=14, minute=0, second=0, microsecond=0).timestamp()
        # ⚠️ 活跃时间不能给 0：实现把 `0` 视为"没有活跃记录"（保守当不空闲）。
        # 给一个"1 小时前"的时间戳才表示"群早就安静了"。
        sched._last_activity = lambda g: day - 3600
        Config.PROACTIVE_GROUPS = [PROD_GID]
        await sched.tick(now=day)

        assert PROD_GID in fired, f"实例群应该被破冰：{fired}"
        assert TEST_GID not in fired, f"测试群不该被破冰：{fired}"
        assert IDLE_GID not in fired, f"放置群不该被破冰：{fired}"
        assert sched.state.task_state(TEST_GID, "icebreak_probe")["last_skip_reason"] == "不在主动打扰名单"
        # 真实注册的冰破任务也必须是"主动型"，否则 D15 对它就失效了
        assert sched.tasks["icebreak"].is_proactive is True
        print(f"   ④ 破冰只落在实例群（调度框架判定）：fired={fired}")

        # ---------- 5) 名单为空 = 不限制（兼容旧行为）----------
        Config.PROACTIVE_GROUPS = []
        onebot2, handler2 = _make_handler()
        try:
            sent = await _feed(handler2, onebot2, TEST_GID, [_text("随便说点什么")])
            assert sent, "名单为空时应该所有群都能主动说话"
            print("   ⑤ 名单为空：退回「所有群都可主动」的旧行为")
        finally:
            if handler2._scheduler_task:
                handler2._scheduler_task.cancel()

        # ---------- 6) 被动响应不受限制 ----------
        Config.PROACTIVE_GROUPS = [PROD_GID]
        sent = await _feed(handler, onebot, TEST_GID, [{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("你好")])
        assert sent and "收到啦" in sent[0][1], f"@bot 不该被限制：{sent}"
        sent = await _feed(handler, onebot, TEST_GID, [_text("/ping")])
        assert sent and "pong" in sent[0][1], f"/ping 不该被限制：{sent}"
        sent = await _feed(handler, onebot, IDLE_GID, [_text("/ping")])
        assert sent, "放置群的命令也不该被限制"
        print("   ⑥ 被动响应（@bot / /ping / /看）在任何群都正常")

        print("\n🎉 全部通过：主动打扰只发生在配置允许的群，被动响应不受影响")
    finally:
        if handler._scheduler_task:
            handler._scheduler_task.cancel()
        from app import metrics as _m
        shutil.rmtree(TMP, ignore_errors=True)
        _m._instance = None


if __name__ == "__main__":
    print("=== 主动打扰范围（D15）回归测试 ===")
    try:
        asyncio.run(main())
    finally:
        shutdown_blocking(wait=False)
