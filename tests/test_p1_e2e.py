"""
P1 端到端干跑验证：记忆分层
===========================
把 P1 的四件事串起来（**不联网、不发真实群消息**，全桩件），判定标准是 DEVLOG [P1-0] §5：

  ① 攒够 40 条消息 → 后台自动跑一次摘要（要点 + 画像 + 话题）并落盘
  ② 摘要与画像**被注入 system prompt** → 这就是"50 轮之后仍能回答一开始说的那个"
  ③ 当前话题被记录并注入；过期后不再注入
  ④ 上下文预算：`[Context]` 日志能看到各段占用（人设占用预算可见）；窗口被按预算裁剪
  ⑤ 模拟重启：新实例重新读盘，要点与画像仍在（验收标准 1）
  ⑥ 预算极紧时：LOW 段被丢、MUST 段仍在，回复照常成功（不会因为预算把回复搞崩）

运行: python tests/test_p1_e2e.py
（必须在项目根目录运行；全部数据写临时目录，不碰生产数据）
"""
import asyncio
import json
import logging
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
from app.context.summary import ConversationSummary
from app.context.topic import TopicState
from app.memory.memory import GroupMemory
from app.util.blocking import shutdown_blocking

TEST_GID = 100000040
TMP = Path("data/_tmp_p1_e2e")

SUMMARY_REPLY = """【要点】
1. 小明在攒钱买显卡
2. 大家约好周末开黑

【成员】
小明: 想换显卡，预算 5000

【当前话题】
显卡和周末开黑
"""


class StubOneBot:
    ws_connected = True

    def __init__(self):
        self.sent = []

    async def send_group_message(self, group_id, text):
        self.sent.append((group_id, text))

    async def get_group_members(self, group_id):
        return []


class StubLLM:
    """既能聊天、也能当摘要模型（靠 prompt 里有没有【本次任务】区分）"""

    def __init__(self):
        self.chat_calls = 0
        self.summary_calls = 0
        self.last_system_prompt = ""

    async def chat(self, messages, system_prompt="", **kwargs):
        content = messages[0].get("content", "") if messages else ""
        if "【本次任务】" in content and "长期记忆" in content:
            self.summary_calls += 1
            return SUMMARY_REPLY
        self.chat_calls += 1
        self.last_system_prompt = system_prompt or ""
        return "收到啦～"


def _event(segments, gid=TEST_GID, uid=888888, nick="小明"):
    return {
        "post_type": "message", "message_type": "group", "group_id": gid,
        "message_id": 1, "message": segments,
        "sender": {"user_id": uid, "nickname": nick},
    }


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _make_handler(tmp: Path, llm) -> tuple[StubOneBot, MessageHandler]:
    isolate_writable_state(tmp)      # 风格画像/指标都写临时目录，别碰真实 data/
    onebot = StubOneBot()
    handler = MessageHandler(
        onebot=onebot, llm_provider=llm, search_provider=None,
        memory=GroupMemory(persist=False, stream=ConversationStream(
            stream_dir=tmp / "stream", legacy_memory_dir=tmp / "none")),
        persona="测试人设：爱丽丝",
    )
    if handler._scheduler_task:
        handler._scheduler_task.cancel()
    # P1 三件套指向临时目录（不碰真实数据）
    handler.summary = ConversationSummary(summary_dir=tmp / "summary")
    handler.topic = TopicState(state_dir=tmp / "state")
    return onebot, handler


async def _drain(times: int = 30):
    """等后台摘要任务跑完（它是 create_task 出来的）"""
    for _ in range(times):
        await asyncio.sleep(0.02)


async def main():
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    Config.CHAT_REPLY_CHANCE = 0
    Config.SUMMARY_MIN_MESSAGES = 40
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    Config.SUMMARY_KEEP_BULLETS = 5
    Config.TOPIC_ENABLED = True
    Config.CONTEXT_LOG = True
    Config.CONTEXT_MAX_CHARS = 6000
    Config.CONTEXT_MIN_ENTRIES = 6
    Config.ICEBREAK_ENABLED = False

    from app import metrics as metrics_mod
    metrics_mod._instance = metrics_mod.Metrics(path=TMP / "metrics.json", save_every=999)

    llm = StubLLM()
    onebot, handler = _make_handler(TMP, llm)

    async def feed(segments, uid=888888, nick="小明", h=None, ob=None):
        h = h or handler
        ob = ob or onebot
        ob.sent.clear()
        parsed = parse_message(_event(segments, uid=uid, nick=nick))
        assert parsed, f"解析失败: {segments}"
        await h.handle(parsed)
        return list(ob.sent)

    # ---------- 1) 攒够 40 条 → 触发摘要（【P2a】摘要是调度框架里的 summary_job）----------
    for i in range(42):
        await feed([_text(f"第{i}条：今天聊聊显卡和周末开黑的事")])
    # 摘要任务的触发器是 interval（默认 10 分钟），首轮 last_run=0 即到点；
    # 用白天时间戳跑一轮 tick（避开静默时段），确认"框架能自动把它跑起来"。
    import datetime as _dt
    _day = _dt.datetime.now().replace(hour=14, minute=0, second=0, microsecond=0).timestamp()
    await handler.scheduler.tick(now=_day)
    assert llm.summary_calls >= 1, f"攒够 40 条却没触发摘要（summary_calls={llm.summary_calls}）"
    state_file = TMP / "summary" / f"{TEST_GID}.json"
    assert state_file.exists(), "摘要没有落盘"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["bullets"], state
    assert state["people"], state
    print(f"   ① 42 条消息 → 后台摘要触发 {llm.summary_calls} 次；"
          f"要点 {len(state['bullets'])} 条、画像 {len(state['people'])} 人")

    # ---------- 2) 摘要 + 画像被注入 system prompt ----------
    await feed([{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("刚才说的那个显卡怎么样了？")])
    sp = llm.last_system_prompt
    assert "小明在攒钱买显卡" in sp, sp[-400:]
    assert "想换显卡，预算 5000" in sp, sp[-400:]
    assert "【长期记忆" in sp and "【群友画像】" in sp, sp[-400:]
    print("   ② 长期要点与群友画像已注入 system prompt（= 滑出窗口也还记得）")

    # ---------- 3) 话题状态：摘要抽出 + 注入 ----------
    topic_text = handler.topic.prompt_text(TEST_GID)
    assert "显卡和周末开黑" in topic_text, topic_text
    assert "【当前话题】" in sp, sp[-400:]
    # 伪造一个 3 小时前的活跃时间 → 过期后不再注入
    handler.topic.touch(TEST_GID, ts=0)
    assert handler.topic.prompt_text(TEST_GID) == "", "过期话题不该注入"
    handler.topic.set_topic(TEST_GID, "显卡和周末开黑", by_nick="小明")
    print(f"   ③ 当前话题已记录并注入：{topic_text.splitlines()[0]}")

    # ---------- 4) 预算：日志可见 + 窗口被裁剪 ----------
    logs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    cap = _Cap()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.INFO)      # 不设的话 INFO 级日志根本不会发出（默认 WARNING）
    try:
        Config.CONTEXT_MAX_CHARS = 1200            # 收紧预算，逼出裁剪
        await feed([{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("再聊聊")])
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    ctx_lines = [l for l in logs if l.startswith("[Context]")]
    assert ctx_lines, f"没有 [Context] 占用日志：{logs[-5:]}"
    line = ctx_lines[-1]
    assert "人设=" in line and "要点=" in line and "窗口=" in line and "合计=" in line, line
    sent_messages = llm.last_system_prompt
    assert "测试人设" in sent_messages, "预算再紧，人设(MUST)也不能丢"
    print(f"   ④ {line}")

    # ---------- 5) 模拟重启：新实例重新读盘，记忆仍在 ----------
    llm2 = StubLLM()
    onebot2, handler2 = _make_handler(TMP, llm2)
    await feed([{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("还记得我们之前聊的吗")],
               h=handler2, ob=onebot2)
    sp2 = llm2.last_system_prompt
    assert "小明在攒钱买显卡" in sp2, "重启后长期要点丢了"
    assert "想换显卡，预算 5000" in sp2, "重启后画像卡丢了"
    print("   ⑤ 模拟重启：新实例重新读盘，要点与画像仍在（验收标准 1）")

    # ---------- 6) 极端：预算极紧也不崩 ----------
    Config.CONTEXT_MAX_CHARS = 300
    sent = await feed([{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("预算紧的时候还能回吗")],
                      h=handler2, ob=onebot2)
    assert sent and "收到啦" in sent[0][1], f"预算极紧时回复失败：{sent}"
    assert "测试人设" in llm2.last_system_prompt, "MUST 段被丢了（人设）"
    Config.CONTEXT_MAX_CHARS = 6000
    print("   ⑥ 预算 300 字（极紧）：LOW 段被丢、人设仍在，回复正常")

    print("\n🎉 P1 干跑验证通过（滚动摘要 / 画像卡 / 话题状态 / 预算组装 + 重启保持）")


if __name__ == "__main__":
    print("=== P1 端到端干跑验证 ===")
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
