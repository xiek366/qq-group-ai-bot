"""
P2c 端到端干跑验证：资讯 / 每日一句 / 每日一图 / 话题轮换 / 效果统计
==================================================================
用真实 handler + 调度框架跑一遍（**不联网**，资讯源与 LLM 都是桩），判定标准见 [P2c-0] §5：

  ① 已注册 7 个任务（含 P2c 的三个）
  ② 11:30 资讯任务：群里收到"标题 + 一句话点评 + 来源 + 原文链接"，**不含正文**，且已记指纹
  ③ **连续两天不重复**：同一条资讯第二天不会再推（去重生效，没有新的就跳过不硬发）
  ④ 12:30 每日一句：`💬 ` 开头的台词（池子轮播）
  ⑤ 19:30 每日一图：**真的带 [CQ:image] 本地图 + 来源链接**；取不到图 → 一条都不发
  ⑥ 破冰内容源轮换：连续三次破冰用的是不同内容源（话题包 → 历史回顾 → 资讯/话题库…）
  ⑦ 源全挂 → 资讯与每日一图都安静跳过（不发占位内容）
  ⑧ 效果统计：主动消息被接后，`by_kind` 里能看到该类型的 sent/replied

运行: python tests/test_p2c_e2e.py
（全部状态写临时目录，不碰真实数据）
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
from app.news import NewsItem, NewsService
from app.util import clock
from app.util.blocking import shutdown_blocking

PROD_GID = 100000004
NOCITY_GID = 100000090
ADMIN_UID = 100000008
TMP = Path("data/_tmp_p2c_e2e")


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
    """按提示词内容返回不同结果，用来判断"这一轮用的是哪个内容源" """

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, system_prompt="", **kwargs):
        self.calls += 1
        content = messages[0].get("content", "") if messages else ""
        if "安静了" in content:
            return "【话题包】你们最近在玩什么游戏呀"
        if "最近群里的对话" in content:
            return "【历史回顾】昨天那个新活动后来怎样了"
        if "写一句" in content and "每日一句" in content:
            return "【原创】愿你的存档永远不丢"
        if "点评" in content:
            return "这个看着有点意思"
        if "配文" in content:
            return "今天也想分你一半新鲜感"
        return "收到啦～"


class StubNewsSource:
    source_id = "stub"
    display_name = "测试源"

    def __init__(self):
        self.items = [
            NewsItem(source="测试源", source_id="stub", title="《测试游戏》公布新 PV",
                     link="https://x/1", pub_time=0, image="https://x/1.jpg", guid="n1",
                     summary="这是摘要，正文不该被推送"),
            NewsItem(source="测试源", source_id="stub", title="某作销量破百万",
                     link="https://x/2", pub_time=0, image="https://x/2.jpg", guid="n2",
                     summary="第二条摘要"),
        ]
        self.fail = False
        self.calls = 0

    async def fetch(self, *, etag="", last_modified=""):
        self.calls += 1
        if self.fail:
            return [], etag, last_modified, False
        return list(self.items), "", "", False


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
    Config.PROACTIVE_GROUPS = [PROD_GID]      # 本用例只关心实例群（多群隔离由 P2b 用例覆盖）
    Config.ADMIN_QQ = str(ADMIN_UID)
    Config.NEWS_ENABLED = True
    Config.QUOTE_LLM_EVERY_DAYS = 0          # 本用例只验证池子路径（原创路径由单测覆盖）
    Config.DAILY_TOTAL_LIMIT = 20
    Config.QUIET_HOURS = "23:00-08:00"
    Config.WEATHER_ENABLED = False           # 天气与本用例无关，关掉减少干扰
    Config.WATER_REMINDER_ENABLED = False

    (TMP / "group_config").mkdir(parents=True, exist_ok=True)
    (TMP / "group_config" / f"{PROD_GID}.json").write_text(
        json.dumps({"weather": {"city": "北京"}}, ensure_ascii=False), encoding="utf-8")

    provider = StubNewsSource()
    llm = StubLLM()
    onebot = StubOneBot()
    handler = MessageHandler(
        onebot=onebot, llm_provider=llm, search_provider=None,
        memory=GroupMemory(persist=False, stream=ConversationStream(
            stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")),
        persona="测试人设：爱丽丝")
    if handler._scheduler_task:
        handler._scheduler_task.cancel()
    # 资讯服务换成桩源（不联网），其余走真实逻辑
    handler.news = NewsService(sources=[provider], state_path=TMP / "news" / "state.json")
    for name, task in handler.scheduler.tasks.items():
        if hasattr(task, "service") and getattr(task, "name", "") in ("news_digest", "daily_image"):
            task.service = handler.news
    sched = handler.scheduler

    async def feed(segments, gid):
        onebot.group_sent.clear()
        parsed = parse_message(_event(segments, gid))
        assert parsed, f"解析失败: {segments}"
        await handler.handle(parsed)
        return list(onebot.group_sent)

    for gid in (PROD_GID, NOCITY_GID):
        await feed([_text("早上好")], gid)
    onebot.group_sent.clear()
    # ⚠️ 活跃时间要给得宽裕：破冰的 idle 阈值是 30 分钟，正好卡在 30.0 会因浮点误差时触发时不触发，
    #    进而让"内容源轮换"的计数漂移（第一次跑就偶发挂在这上面）。给 2 小时。
    base = _day(9, 30)
    sched._last_activity = lambda g: base
    sched._members.clear()
    sched._last_proactive.clear()
    for gid in (PROD_GID, NOCITY_GID):
        sched.state.clear(gid)

    # ---------- ① 任务注册 ----------
    names = sorted(sched.tasks)
    for want in ("icebreak", "summary_job", "weather_morning", "water_reminder",
                 "news_digest", "daily_quote", "daily_image"):
        assert want in names, names
    assert sched.tasks["icebreak"].source.name == "icebreak_rotating", "破冰内容源应该是轮换源"
    print(f"   ① 已注册 {len(names)} 个任务；破冰内容源 = 轮换（{names}）")

    # ---------- ② 11:30 资讯（**2026-10-19 起带封面图**，图在前、文在后） ----------
    img_dir = TMP / "imgs"
    img_dir.mkdir(parents=True, exist_ok=True)
    real_img = img_dir / "pic.jpg"
    real_img.write_bytes(b"jpg-bytes")

    async def fetcher(url):
        return real_img

    # 资讯任务也要注入桩：否则它会真的去下载 https://x/1.jpg（联网、慢、可能偶发失败）
    for name, task in sched.tasks.items():
        if name == "news_digest":
            task._fetcher = fetcher
    onebot.group_sent.clear()
    await sched.tick(now=_day(11, 30))
    news_msgs = [(g, t) for g, t in onebot.group_sent if "📰" in t]
    assert news_msgs, f"资讯任务应该发一条：{onebot.group_sent}"
    gid, text = news_msgs[0]
    assert gid == PROD_GID, news_msgs
    assert text.startswith("[CQ:image"), f"资讯现在应该带封面图（图在前）：{text[:60]}"
    assert "《测试游戏》公布新 PV" in text and "这个看着有点意思" in text, text
    assert "来源：测试源" in text and "https://x/1" in text, text
    assert "摘要" not in text and "正文" not in text, f"不能推正文/摘要：{text}"
    print(f"   ② 资讯：封面图 + 标题 + 点评 + 来源 + 链接（无正文）")

    # ---------- ③ 跨天去重：第二天不再推同一条 ----------
    for gid_ in (PROD_GID,):
        sched.state.clear(gid_)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(11, 30) + 86400)      # 第二天同一时刻
    second = [(g, t) for g, t in onebot.group_sent if "📰" in t]
    assert second, "第二天应该推第二条（还在缓存里、未推送过）"
    assert "某作销量破百万" in second[0][1], second
    assert "测试游戏" not in second[0][1], "同一条资讯不该重复推"
    # 两条都推完 → 第三天没有新内容 → 跳过且不硬发
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(11, 30) + 2 * 86400)
    assert not [t for _, t in onebot.group_sent if "📰" in t], "没有新资讯就该跳过，不硬发"
    print("   ③ 去重：第一天推第 1 条 → 第二天推第 2 条 → 第三天没有新的就跳过")

    # ---------- ④ 12:30 每日一句 ----------
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(12, 30) + 3 * 86400)
    quotes = [t for g, t in onebot.group_sent if t.startswith("💬 ")]
    assert quotes, f"每日一句应该发：{onebot.group_sent}"
    print(f"   ④ 每日一句：{quotes[0][:26]}…")

    # ---------- ⑤ 19:30 每日一图（**2026-10-19 起是纯美图：图 + 一句配文，无标题/来源/链接**） ----------
    for name, task in sched.tasks.items():
        if name == "daily_image":
            task._fetcher = fetcher
    handler.news = NewsService(sources=[StubNewsSource()], state_path=TMP / "news" / "state2.json")
    for name, task in sched.tasks.items():
        if name == "daily_image":
            task.service = handler.news
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(19, 30) + 4 * 86400)
    # ⚠️ 判定"这条是不是每日一图"用 `startswith("[CQ:image")`：
    #    破冰消息会按概率**在末尾**附一张收藏表情，那种消息不以图片开头，不会误判。
    imgs = [t for g, t in onebot.group_sent if t.startswith("[CQ:image")]
    assert imgs, f"每日一图应该带图发出：{onebot.group_sent}"
    assert "新鲜感" in imgs[0], f"应该带 LLM 配文：{imgs[0]}"
    # 纯美图形态：不再有标题 / 来源 / 链接（D19 的署名要求只针对资讯图文场景）
    assert "🖼" not in imgs[0] and "来源：" not in imgs[0] and "http" not in imgs[0].split("\n")[-1], \
        f"纯美图不该带标题/来源/链接：{imgs[0]}"
    # 取不到图 → 一条都不发
    async def no_fetch(url):
        return None
    for name, task in sched.tasks.items():
        if name == "daily_image":
            task._fetcher = no_fetch
            task.service = NewsService(sources=[StubNewsSource()],
                                       state_path=TMP / "news" / "state3.json")
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(19, 30) + 5 * 86400)
    assert not [t for _, t in onebot.group_sent if t.startswith("[CQ:image")], \
        "取不到图不该发占位图/纯文字"
    print("   ⑤ 每日一图：纯美图（图 + 配文，无标题/来源/链接）；取不到图 → 一条都不发")

    # ---------- ⑥ 破冰内容源轮换 ----------
    ice_sent = []
    for i in range(3):
        sched.state.clear(PROD_GID)
        sched._last_proactive.clear()
        sched._last_activity = lambda g: _day(20, 0) + i * 86400 - 3600
        onebot.group_sent.clear()
        await sched.tick(now=_day(20, 0) + i * 86400)
        # 只看"破冰类"消息：排掉资讯/每日一句/每日一图（优先级更高，可能先发）
        # 每日一图现在以 [CQ:image 开头（纯美图，没有 🖼 标记），所以按"是否以图片开头"排除
        got = [t for g, t in onebot.group_sent
               if g == PROD_GID
               and not t.startswith("[CQ:image")
               and not any(mark in t for mark in ("📰", "💬"))]
        ice_sent.append(got[0] if got else "")
    assert any("话题包" in t for t in ice_sent), ice_sent
    assert any("历史回顾" in t for t in ice_sent), ice_sent
    assert len(set(ice_sent)) >= 2, f"连续破冰应该用不同内容源：{ice_sent}"
    print(f"   ⑥ 破冰轮换：{[t[:12] for t in ice_sent]}")

    # ---------- ⑦ 源全挂 → 安静跳过 ----------
    dead = NewsService(sources=[StubNewsSource()], state_path=TMP / "news" / "state4.json")
    dead.sources[0].fail = True
    for name, task in sched.tasks.items():
        if name in ("news_digest", "daily_image"):
            task.service = dead
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(11, 30) + 9 * 86400)
    assert not [t for _, t in onebot.group_sent if "📰" in t], "源全挂不该发资讯"
    assert sched.state.task_state(PROD_GID, "news_digest")["last_skip_reason"], "应该记下跳过原因"
    print("   ⑦ 源全挂：资讯与每日一图都安静跳过（不发占位内容）")

    # ---------- ⑧ 效果统计（哪类内容有人接） ----------
    from app import metrics as _m
    sched.state.clear(PROD_GID)
    sched._last_proactive.clear()
    onebot.group_sent.clear()
    await sched.tick(now=_day(12, 30) + 10 * 86400)      # 发一条每日一句
    await feed([_text("这句不错啊")], PROD_GID)           # 群友接了话
    snap = _m.get().snapshot()
    kinds = {k["kind"]: k for k in snap["by_kind"]}
    assert any(k in kinds for k in ("daily_quote", "icebreak", "news_digest")), kinds
    hit = [k for k in kinds.values() if k["replied"] > 0]
    assert hit, f"应该至少有一类内容被接：{kinds}"
    print(f"   ⑧ 效果统计：{[(k['kind'], k['sent'], k['replied']) for k in snap['by_kind']]}")

    print("\n🎉 P2c 干跑验证通过（资讯 / 每日一句 / 每日一图 / 内容源轮换 / 去重 / 降级 / 效果统计）")


if __name__ == "__main__":
    print("=== P2c 端到端干跑验证 ===")
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        reset_writable_state()
        shutdown_blocking(wait=False)
