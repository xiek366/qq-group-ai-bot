"""
Handler 分发链路冒烟测试（不依赖 NapCat / LLM / 网络）
=====================================================
把「解析后的群消息」直接喂给 MessageHandler.handle()，
用桩件替掉 OneBot 和 LLM，验证异步改造后各条分支仍然通：

  /ping  /help  /clear  @机器人聊天  /叫我  /我的外号  /别叫我了  /表情

这些分支全部经过 run_blocking（磁盘 I/O）和 await（异步发送），
所以能挡住「漏了 await」「run_blocking 参数传错」这类回归。

运行: python tests/test_handler_dispatch.py
（必须在项目根目录运行）
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bot.handler import MessageHandler
from app.bot.message import parse_message
from app.config import Config
from app.memory.memory import GroupMemory
from app.util.blocking import shutdown_blocking

TEST_GID = 100000002
TEST_UID = 888888


class StubOneBot:
    """只记录发出去的消息"""

    ws_connected = True

    def __init__(self, members=None):
        self.sent: list[tuple[int, str]] = []
        self._members = members or []

    async def send_group_message(self, group_id, text):
        self.sent.append((group_id, text))

    async def get_group_members(self, group_id):
        return self._members


class StubLLM:
    """把收到的 messages 记下来，固定回一句"""

    def __init__(self, reply="收到啦～"):
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.reply

    async def chat_with_images(self, image_urls, prompt=""):
        return "一只猫"


def _event(text: str, at: bool = False):
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": str(Config.BOT_QQ)}})
    segs.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": TEST_GID,
        "message_id": 1,
        "message": segs,
        "sender": {"user_id": TEST_UID, "nickname": "小明"},
    }


async def _make_handler(members=None):
    onebot = StubOneBot(members=members)
    handler = MessageHandler(
        onebot=onebot,
        llm_provider=StubLLM(),
        search_provider=None,
        memory=GroupMemory(),
        persona="你是测试用 AI 群友。",
    )
    return onebot, handler


async def _feed(handler, onebot, text, at=False):
    onebot.sent.clear()
    parsed = parse_message(_event(text, at=at))
    assert parsed is not None, f"解析失败: {text}"
    await handler.handle(parsed)
    return list(onebot.sent)


async def main():
    Path("data/aliases").mkdir(parents=True, exist_ok=True)
    Config.CHAT_REPLY_CHANCE = 0  # 关掉随机插嘴，保证断言稳定

    # 【P0-4/P2a】会落盘的单例（指标 / 风格画像 / 调度状态）统一指到临时目录，
    # 别把测试流量写进生产统计 data/stats 与任务状态 data/schedule
    import shutil as _shutil
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from helpers_isolate import isolate_writable_state, reset_writable_state
    tmp = Path("data/_tmp_dispatch")
    _shutil.rmtree(tmp, ignore_errors=True)
    isolate_writable_state(tmp)

    onebot, handler = await _make_handler()
    try:
        # /ping
        sent = await _feed(handler, onebot, "/ping")
        assert sent and "pong" in sent[0][1], sent

        # /help
        sent = await _feed(handler, onebot, "/help")
        assert sent and "/搜" in sent[0][1], sent

        # @机器人 → 走 LLM（同时验证 memory 写入 / system prompt 构造）
        sent = await _feed(handler, onebot, "你好", at=True)
        assert sent and "收到啦" in sent[0][1], sent
        # 【P0-1】现在**所有**消息都进统一对话流，不再只有 @bot 的那几轮：
        # /ping + /help 两条命令 + 本次提问 + bot 回复 = 4 条
        entries = handler.memory.stream.window(TEST_GID, limit=20)
        kinds = [e["kind"] for e in entries]
        assert kinds == ["command", "command", "chat", "reply"], kinds
        assert entries[-1]["role"] == "assistant" and entries[-1]["text"] == "收到啦～", entries[-1]
        assert entries[2]["nick"] == "小明", entries[2]

        # 外号：设置 → 查看 → 清空（全是 run_blocking 走的磁盘写）
        sent = await _feed(handler, onebot, "/叫我 明明")
        assert sent and "明明" in sent[0][1], sent
        sent = await _feed(handler, onebot, "/我的外号")
        assert sent and "明明" in sent[0][1], sent
        sent = await _feed(handler, onebot, "/别叫我了")
        assert sent and "不叫外号" in sent[0][1], sent

        # /表情（真实表情库，至少回一条）
        sent = await _feed(handler, onebot, "/表情")
        assert sent, "表情包命令没有任何回复"

        # /clear（删记忆文件，run_blocking 路径）
        sent = await _feed(handler, onebot, "/clear")
        assert sent and "清空" in sent[0][1], sent
        assert handler.memory.size(TEST_GID) == 0

        print("✅ test_handler_dispatch PASS（6 条命令 + @聊天 全通）")
    finally:
        if handler._scheduler_task:
            handler._scheduler_task.cancel()
            try:
                await handler._scheduler_task
            except (asyncio.CancelledError, Exception):
                pass


def _cleanup():
    for path in (
        Path("data/memory") / f"{TEST_GID}.json",
        Path("data/stream") / f"{TEST_GID}.jsonl",
        Path("data/aliases") / f"{TEST_GID}.json",
        Path("data/group_style") / f"{TEST_GID}.json",
    ):
        try:
            path.unlink()
        except OSError:
            pass
    # /clear 会留 .bak-<时间戳> 备份，一并清掉
    for bak in Path("data/stream").glob(f"{TEST_GID}.jsonl.bak-*"):
        try:
            bak.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    print("=== Handler 分发链路冒烟测试 ===\n")
    try:
        asyncio.run(main())
    finally:
        _cleanup()
        import os as _os
        import shutil as _shutil
        import sys as _sys
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from helpers_isolate import reset_writable_state
        _shutil.rmtree(Path("data/_tmp_dispatch"), ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
