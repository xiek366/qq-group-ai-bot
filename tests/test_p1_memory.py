"""
P1 记忆分层回归测试：滚动摘要 / 画像卡 / 话题状态 / 预算组装器
==============================================================
覆盖（对应 DEVLOG [P1-0] §4 测试计划）：

摘要与画像
  1. 三段输出正常解析（要点 / 成员 / 当前话题）
  2. **畸形输出**：没有段落标题 → 兜底把列表行当要点；完全没内容 → 保留旧记忆不写坏
  3. 要点合并与上限（超出滚进 archived，不外泄无限增长）
  4. 画像卡按 QQ 存、注入时"当前说话人优先"、上限生效
  5. 触发：40 条才排期、同群不并发、冷却生效
  6. 落盘/重载一致

话题状态
  7. 记录 / 刷新 / 过期不注入 / 关闭开关则不写

预算组装器
  8. 不超预算；超了先丢 LOW（最长的先丢），MUST 永不丢
  9. 硬上限截断；窗口保底条数；窗口按最新优先保留
 10. 统计行里能看到各段占用（"人设占用预算可见"）

运行: python tests/test_p1_memory.py
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
from app.context.assembler import LOW, MUST, HIGH, ContextAssembler, Section, text_chars
from app.context.summary import ConversationSummary, parse_summary
from app.context.topic import TopicState

TMP = Path("data/_tmp_p1_test")
GID = 100000030


class StubLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.last_prompt = ""

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_prompt = messages[0]["content"]
        return self.reply


def _fresh() -> tuple[ConversationSummary, TopicState, Path]:
    shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "summary").mkdir(parents=True, exist_ok=True)
    (TMP / "state").mkdir(parents=True, exist_ok=True)
    return (ConversationSummary(summary_dir=TMP / "summary"),
            TopicState(state_dir=TMP / "state"),
            TMP)


def _entries(n: int, start_ts: float = 1000.0, nick: str = "小明", uid: int = 888888):
    out = []
    for i in range(n):
        out.append({"ts": start_ts + i, "role": "user", "kind": "chat",
                    "text": f"第{i}条消息", "nick": nick, "user_id": uid})
    return out


# ---------- 摘要与画像 ----------

def test_parse_wellformed():
    text = """【要点】
1. 大家在约周末开黑
2. 小明说他在练原神深渊

【成员】
小明: 喜欢原神，最近在打深渊

【当前话题】
周末开黑安排"""
    out = parse_summary(text)
    assert out["bullets"] == ["大家在约周末开黑", "小明说他在练原神深渊"], out
    assert out["people"] == {"小明": "喜欢原神，最近在打深渊"}, out
    assert out["topic"] == "周末开黑安排", out
    print("✅ test_parse_wellformed PASS")


def test_parse_malformed_and_empty():
    # 没有段落标题 → 兜底把列表行当要点
    out = parse_summary("- 第一条\n- 第二条\n随便一句")
    assert out["bullets"][:2] == ["第一条", "第二条"], out

    # 完全没内容 → 三段全空（调用方据此保留旧记忆）
    out2 = parse_summary("")
    assert out2 == {"bullets": [], "people": {}, "topic": ""}, out2

    # 话题写"无" → 视为没有话题
    assert parse_summary("【当前话题】\n无")["topic"] == ""
    print("✅ test_parse_malformed_and_empty PASS")


async def test_run_merges_and_caps():
    summary, _, _ = _fresh()
    Config.SUMMARY_KEEP_BULLETS = 2
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    entries = _entries(10, start_ts=1000.0)

    llm = StubLLM("""【要点】
1. 新要点A
2. 新要点B
3. 新要点C

【成员】
小明: 喜欢原神
""")
    state = await summary.run(llm, GID, entries)
    assert state is not None and llm.calls == 1
    assert state["bullets"] == ["新要点A", "新要点B"], state["bullets"]
    assert state["archived"] == ["新要点C"], state["archived"]
    assert state["people"]["888888"]["note"] == "喜欢原神"
    assert state["summarized_upto_ts"] == 1009.0, state["summarized_upto_ts"]

    # 增量：只有新条目才进下一批，且冷却=0 时能立刻再跑
    more = _entries(5, start_ts=2000.0)
    llm2 = StubLLM("【要点】\n1. 又聊了新东西\n\n【成员】\n小明: 在练角色\n")
    st2 = await summary.run(llm2, GID, entries + more)
    assert st2["summarized_upto_ts"] == 2004.0, st2["summarized_upto_ts"]
    assert "又聊了新东西" in st2["bullets"], st2["bullets"]
    print(f"   要点={st2['bullets']} 归档={st2['archived']} 画像={list(st2['people'])}")
    print("✅ test_run_merges_and_caps PASS")


async def test_run_keeps_old_on_garbage():
    summary, _, _ = _fresh()
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    entries = _entries(5)
    await summary.run(StubLLM("【要点】\n1. 好要点\n"), GID, entries)
    before = summary.load(GID)["bullets"]
    assert before == ["好要点"]

    # 第二次返回垃圾 → 不该清空已有记忆
    st = await summary.run(StubLLM("（我今天不想干活）"), GID, _entries(5, start_ts=3000.0))
    assert st is None, "垃圾输出应该返回 None 保留旧值"
    assert summary.load(GID)["bullets"] == before, summary.load(GID)["bullets"]
    print("✅ test_run_keeps_old_on_garbage PASS")


async def test_people_prompt_and_preference():
    summary, _, _ = _fresh()
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    Config.PEOPLE_PROMPT_LIMIT = 5
    entries = _entries(3, nick="小明", uid=111) + _entries(3, start_ts=1100, nick="小红", uid=222)
    await summary.run(StubLLM("【成员】\n小明: 喜欢猫\n小红: 喜欢狗\n"), GID, entries)

    text = summary.people_text(GID)
    assert "小明" in text and "小红" in text, text
    # 当前说话人优先且带标注
    text_cur = summary.people_text(GID, current_qq=222)
    assert text_cur.splitlines()[0].startswith("- 小红"), text_cur
    assert "就是他/她在说话" in text_cur, text_cur
    # 上限生效
    assert len(summary.people_text(GID, limit=1).splitlines()) == 1
    assert summary.stats(GID)["people"] == 2
    print("✅ test_people_prompt_and_preference PASS")


def test_prime_backlog():
    """历史积压要垫进触发计数 —— 否则重启后积压几十条还要再等 40 条，
    群消息少 + 常重启的群会永远不摘要（实测踩到过）。"""
    summary, _, _ = _fresh()
    Config.SUMMARY_ENABLED = True
    Config.SUMMARY_MIN_MESSAGES = 40
    assert not summary.is_primed(GID)
    # 模拟：积压 39 条 + 本次新来 1 条 = 40 → 应该排期
    summary.prime(GID, 39)
    assert summary.is_primed(GID)
    assert summary.pending_count(GID) == 39
    summary.note_activity(GID, 1)
    assert summary.should_schedule(GID), "积压 39 + 新 1 条应该达到门槛"
    # 重复 prime 不该重复累加
    summary.prime(GID, 39)
    assert summary.pending_count(GID) == 40, summary.pending_count(GID)
    print("✅ test_prime_backlog PASS")


def test_schedule_gating():
    summary, _, _ = _fresh()
    Config.SUMMARY_ENABLED = True
    Config.SUMMARY_MIN_MESSAGES = 40
    assert not summary.should_schedule(GID)
    summary.note_activity(GID, 39)
    assert not summary.should_schedule(GID), "39 条不该排期"
    summary.note_activity(GID, 1)
    assert summary.should_schedule(GID), "40 条应该排期"
    summary.mark_scheduled(GID)
    assert not summary.should_schedule(GID), "已排期时不该重复排"
    assert summary.pending_count(GID) == 0
    summary.release(GID)
    assert not summary.should_schedule(GID), "清零后不该立刻再排"

    Config.SUMMARY_ENABLED = False
    summary.note_activity(GID, 100)
    assert not summary.should_schedule(GID), "开关关掉就不排期"
    Config.SUMMARY_ENABLED = True
    print("✅ test_schedule_gating PASS")


async def test_persist_and_reload():
    summary, _, _ = _fresh()
    Config.SUMMARY_COOLDOWN_MINUTES = 0
    await summary.run(StubLLM("【要点】\n1. 落盘要点\n\n【成员】\n小明: 备注\n"), GID, _entries(3))
    reloaded = ConversationSummary(summary_dir=TMP / "summary")
    st = reloaded.load(GID)
    assert st["bullets"] == ["落盘要点"], st["bullets"]
    assert st["people"]["888888"]["note"] == "备注", st["people"]
    raw = json.loads((TMP / "summary" / f"{GID}.json").read_text(encoding="utf-8"))
    assert raw["bullets"] == ["落盘要点"]
    print("✅ test_persist_and_reload PASS")


async def test_cooldown_blocks_second_run():
    summary, _, _ = _fresh()
    Config.SUMMARY_COOLDOWN_MINUTES = 10
    await summary.run(StubLLM("【要点】\n1. 第一次\n"), GID, _entries(3))
    st2 = await summary.run(StubLLM("【要点】\n1. 第二次\n"), GID, _entries(3, start_ts=5000))
    assert st2 is None, "冷却期内不该再跑"
    print("✅ test_cooldown_blocks_second_run PASS")


# ---------- 话题状态 ----------

def test_topic_lifecycle():
    _, topic, _ = _fresh()
    Config.TOPIC_ENABLED = True
    Config.TOPIC_STALE_MINUTES = 90

    assert topic.prompt_text(GID) == "", "没话题就不该注入"
    topic.set_topic(GID, "周末开黑安排", by_uid=888888, by_nick="小明")
    text = topic.prompt_text(GID)
    assert "周末开黑安排" in text and "小明" in text, text

    # 过期 → 不再注入
    old = time.time() - 91 * 60
    topic.set_topic(GID, "早就凉了", by_uid=1, ts=old)
    assert topic.is_stale(GID)
    assert topic.prompt_text(GID) == "", "过期话题不该注入"

    # touch 刷新活跃 → 又可用
    topic.touch(GID)
    assert not topic.is_stale(GID) and topic.prompt_text(GID) != ""

    # 关掉开关 → 不写也不注入
    Config.TOPIC_ENABLED = False
    assert topic.set_topic(GID, "不该写进去") is None
    assert topic.prompt_text(GID) == ""
    Config.TOPIC_ENABLED = True
    print("✅ test_topic_lifecycle PASS")


# ---------- 预算组装器 ----------

def test_compose_within_budget_and_drop_order():
    asm = ContextAssembler(max_chars=1000)
    sections = [
        Section("人设", "核心人设" * 20, MUST),          # 80
        Section("要点", "要点" * 100, HIGH),              # 200
        Section("风格", "风格" * 300, LOW),               # 300
        Section("称呼", "称呼" * 200, LOW),               # 200
    ]
    res = asm.compose_system(sections, budget=400)
    assert text_chars(res.text) <= 400, text_chars(res.text)
    assert "人设" in res.stats, res.stats          # MUST 永不丢
    assert "风格" in res.dropped or "称呼" in res.dropped, res.dropped
    # 丢的是"最长的 LOW"
    assert res.dropped[0] == "风格", res.dropped
    print(f"   丢弃={res.dropped} 截断={res.truncated} 占用={text_chars(res.text)}/400")
    print("✅ test_compose_within_budget_and_drop_order PASS")


def test_hard_cap_and_must_survives():
    asm = ContextAssembler(max_chars=500)
    sections = [
        Section("人设", "长" * 900, MUST, hard_cap=200),
        Section("要点", "要" * 100, HIGH),
    ]
    res = asm.compose_system(sections, budget=300)
    assert res.stats["人设"] <= 200, res.stats           # 硬上限生效（省略号算在内）
    assert "人设" in res.truncated, res.truncated
    assert "人设" not in res.dropped, "MUST 段绝不能被丢"
    assert res.stats.get("要点", 0) == 100, res.stats    # HIGH 段在预算够时保留
    assert res.stats["合计"] <= 300, res.stats           # 预算内（分隔符不计入）
    assert "超预算" not in res.stats, res.stats           # 不该误报超预算

    # 边界：预算小到连 MUST 都装不下 → 不丢 MUST，但必须告警并在统计里标注超预算
    res2 = asm.compose_system(sections, budget=120)
    assert "人设" not in res2.dropped, "MUST 段绝不能被丢（宁可超预算）"
    assert res2.stats.get("超预算", 0) > 0, res2.stats
    assert res2.stats["人设"] <= 201, res2.stats
    print(f"   超预算标注={res2.stats.get('超预算')} 字（MUST 仍保留）")
    print("✅ test_hard_cap_and_must_survives PASS")


def test_trim_messages_budget_and_floor():
    asm = ContextAssembler(max_chars=1000, min_entries=3)
    msgs = [{"role": "user", "content": "字" * 100} for _ in range(10)]   # 每条 100 字
    kept, used = asm.trim_messages(msgs, budget=350)
    assert len(kept) == 3, f"预算 350 只够 3 条：{len(kept)}"
    assert used <= 350, used

    # 预算极紧也要保底 min_entries 条
    kept2, used2 = asm.trim_messages(msgs, budget=10)
    assert len(kept2) == 3, f"保底条数失效：{len(kept2)}"

    # 保留的是最新的那几条
    msgs2 = [{"role": "user", "content": f"第{i}条"} for i in range(10)]
    kept3, _ = asm.trim_messages(msgs2, budget=30)
    assert kept3[-1]["content"] == "第9条", kept3
    print(f"   预算350→保留{len(kept)}条/占用{used}；预算10→保底{len(kept2)}条")
    print("✅ test_trim_messages_budget_and_floor PASS")


def test_stats_line_visible():
    asm = ContextAssembler(max_chars=6000)
    res = asm.compose_system([Section("人设", "核" * 420, MUST), Section("要点", "点" * 300, HIGH)])
    line = ContextAssembler.format_stats(res.stats, res.dropped, res.truncated,
                                         window_entries=18, window_chars=2140)
    assert "人设=" in line and "要点=" in line and "窗口=18条/2140字" in line, line
    print(f"   [Context] {line}")
    print("✅ test_stats_line_visible PASS")


async def _run_all():
    test_parse_wellformed()
    test_parse_malformed_and_empty()
    await test_run_merges_and_caps()
    await test_run_keeps_old_on_garbage()
    await test_people_prompt_and_preference()
    test_prime_backlog()
    test_schedule_gating()
    await test_persist_and_reload()
    await test_cooldown_blocks_second_run()
    test_topic_lifecycle()
    test_compose_within_budget_and_drop_order()
    test_hard_cap_and_must_survives()
    test_trim_messages_budget_and_floor()
    test_stats_line_visible()


if __name__ == "__main__":
    print("=== P1 记忆分层回归测试 ===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：摘要/画像/话题/预算组装器就绪")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
