"""
P2c 回归测试：资讯模块 / 话题库 / 三个新任务 / 效果统计
=====================================================
覆盖 DEVLOG [P2c-0] §4 的测试计划：

解析
  1. RSS 解析：标准项 / CDATA 标题 / description 内嵌图 / thumb / 缺 pubDate / **坏条目跳过**
  2. 命名空间前缀、RDF 式 item、非 XML → 不崩
  3. B站专栏解析：正常 / code≠0 / 缺 image_urls / 缺 id / 毫秒时间戳换算
  4. 时间解析：RFC822 / 秒级 / 毫秒级 / 解析不了 → 0

频控与去重（A.6/A.8）
  5. 指纹稳定且区分来源
  6. 每源冷却：没到时间不再请求；连续失败 → 冷却翻倍退避
  7. 条件请求：304 → 不算失败、也不更新条目
  8. 全部源失败 → 返回空（**本次不发**）
  9. 已推送的不再被选中（跨天去重）；mark_pushed 落盘可被新实例读到

图片
 10. 下载：非图片 content-type / 超大 / 超时 / HTTP 500 → None（不落地）
 11. 正常图片 → 落盘 + CQ 码格式正确

话题库与任务
 12. 话题库：先选"没用过的"、避开本群最近用过的、mark_used 生效
 13. 每日一句：池子轮播 / should_generate_today 的 N 天判定 / LLM 失败回落池子
 14. 资讯任务：文案含标题+点评+链接+来源（**不含正文**）；没有新条目 → 跳过
 15. 每日一图：拿到图 → 带 CQ 图 + 来源链接；取不到图 → 跳过（不发占位）
 16. 效果统计：按内容类型统计 sent/replied 与 reply_rate

运行: python tests/test_p2c_news.py
（全部状态写临时目录）
"""
import asyncio
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.context.topic_pool import PRESET_QUOTES, PRESET_TOPICS, TopicPool
from app.news import NewsItem, NewsService, parse_bilibili_articles, parse_rss
from app.news.image import to_cq_image
from app.news.models import SourceState
from app.news.sources import _parse_time
from app.schedule.task import TaskContext, TaskSkip
from app.schedule.tasks import (
    DailyImageTask,
    DailyQuoteTask,
    NewsDigestTask,
    format_news,
    make_rotating_source,
    should_generate_today,
)

TMP = Path("data/_tmp_p2c_test")
GID = 100000080

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>《某某》公开新预告</title>
    <link>https://www.gcores.com/articles/1</link>
    <guid>gc-1</guid>
    <pubDate>Tue, 07 Oct 2026 10:00:00 +0800</pubDate>
    <thumb>https://image.gcores.com/a.jpg</thumb>
    <author>机核</author>
  </item>
  <item>
    <title><![CDATA[游研社：某作销量破百万]]></title>
    <link>https://www.yystv.cn/n/2</link>
    <pubDate>Tue, 07 Oct 2026 09:00:00 +0800</pubDate>
    <category>业界</category>
    <description><![CDATA[<p>摘要文字</p><img src="https://img.yystv.cn/b.png"/>]]></description>
  </item>
  <item><title></title><link>坏条目</link></item>
</channel></rss>"""

BILI_SAMPLE = {"code": 0, "data": {"articles": [
    {"id": 123456, "title": "Gal周报 第263期",
     "image_urls": ["https://i0.hdslb.com/x.jpg"], "publish_time": 1791000000000},
    {"id": 0, "title": "", "image_urls": []},
    {"id": 999, "title": "无封面的一期", "image_urls": []},
]}}


class StubSource:
    """可控的资讯源（记录调用次数）"""

    def __init__(self, sid="s1", items=None, not_modified=False, fail=False):
        self.source_id = sid
        self.display_name = sid
        self.items = items if items is not None else []
        self.not_modified = not_modified
        self.fail = fail
        self.calls = 0

    async def fetch(self, *, etag="", last_modified=""):
        self.calls += 1
        if self.fail:
            return [], etag, last_modified, False
        if self.not_modified:
            return [], etag, last_modified, True
        return list(self.items), "etag-1", "lm-1", False


def _item(title="标题", link="https://x/1", img="https://x/1.jpg", hours_ago=1.0,
          source="机核", sid="gcores", pub=None) -> NewsItem:
    import time
    return NewsItem(source=source, source_id=sid, title=title, link=link,
                    pub_time=pub if pub is not None else time.time() - hours_ago * 3600,
                    image=img, guid=link)


def _fresh_service(name="a", sources=None) -> NewsService:
    tmp = TMP / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return NewsService(sources=sources if sources is not None else [], state_path=tmp / "state.json")


# ---------- 解析 ----------

def test_parse_rss():
    items = parse_rss(RSS_SAMPLE, source="机核", source_id="gcores")
    assert len(items) == 2, f"坏条目应该被跳过，只留 2 条：{len(items)}"
    a, b = items
    assert a.title == "《某某》公开新预告" and a.image.endswith("a.jpg")
    assert a.pub_time > 0 and a.source_id == "gcores"
    assert b.title.startswith("游研社") and b.category == "业界"
    assert b.image.endswith("b.png"), f"要从 description 里抠出内嵌图：{b.image}"
    assert "摘要文字" in b.summary and "<p>" not in b.summary, b.summary

    # 协议相对 URL（`//host/x.jpg`）必须补全成 https —— 否则会被当成"没图"，
    # 每日一图就会一直报"没有带图的新资讯可发"（2026-10-08 线上踩到）
    proto = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>协议相对图</title><link>//www.yystv.cn/n/9</link>
      <description><![CDATA[<img src="//img.yystv.cn/p.png"/>]]></description></item>
    </channel></rss>"""
    p = parse_rss(proto, source="游研社", source_id="yystv")[0]
    assert p.image == "https://img.yystv.cn/p.png", p.image
    assert p.link == "https://www.yystv.cn/n/9", p.link
    assert p.has_image, "补全后必须算作有图"

    # 非 XML 不崩
    assert parse_rss("not xml at all", source="x", source_id="x") == []
    assert parse_rss("", source="x", source_id="x") == []
    print("✅ test_parse_rss PASS")


def test_parse_rss_namespace_and_rdf():
    rdf = """<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns="http://purl.org/rss/1.0/">
      <item><title>带命名空间的标题</title><link>https://n/1</link></item>
    </rdf:RDF>"""
    items = parse_rss(rdf, source="x", source_id="x")
    assert len(items) == 1 and items[0].title == "带命名空间的标题", items
    print("✅ test_parse_rss_namespace_and_rdf PASS")


def test_parse_bilibili():
    items = parse_bilibili_articles(BILI_SAMPLE, source="B站·Gal周报", source_id="bili_1")
    assert len(items) == 2, items
    a, b = items
    assert a.link == "https://www.bilibili.com/read/cv123456"
    assert a.guid == "cv123456" and a.image.endswith("x.jpg")
    assert a.pub_time > 1e9, "毫秒时间戳要换算成秒"
    assert b.image == "" and not b.has_image
    # code != 0 → 空
    assert parse_bilibili_articles({"code": -352, "data": {}}, source="x", source_id="x") == []
    assert parse_bilibili_articles({"code": 0}, source="x", source_id="x") == []
    print("✅ test_parse_bilibili PASS")


def test_parse_time():
    assert _parse_time("Tue, 07 Oct 2026 10:00:00 +0800") > 1e9
    assert _parse_time("1791000000") == 1791000000.0
    assert _parse_time("1791000000000") == 1791000000.0
    assert _parse_time("2026-10-08 12:00:00") > 1e9
    assert _parse_time("乱七八糟") == 0.0 and _parse_time("") == 0.0
    print("✅ test_parse_time PASS")


# ---------- 频控与去重 ----------

async def test_fingerprint_and_dedup():
    a = _item(link="https://x/1")
    b = _item(link="https://x/1")
    c = _item(link="https://x/2")
    d = _item(link="https://x/1", sid="yystv", source="游研社")
    assert a.fingerprint == b.fingerprint and a.fingerprint != c.fingerprint
    assert a.fingerprint != d.fingerprint, "不同来源的同链接要区分"

    svc = _fresh_service("dedup", [StubSource("s1", [a, c])])
    await svc.refresh()
    assert len(svc.fresh_items()) == 2
    svc.mark_pushed(a)
    fresh = svc.fresh_items()
    assert len(fresh) == 1 and fresh[0].link == "https://x/2", fresh
    # 落盘：新实例仍记得"推过了"
    svc2 = NewsService(sources=[], state_path=svc._path)
    assert svc2.has_pushed(a), "去重状态应该落盘"
    print("✅ test_fingerprint_and_dedup PASS")


async def test_cooldown_and_backoff():
    src = StubSource("s1", [_item()])
    svc = _fresh_service("cool", [src])
    await svc.refresh()
    assert src.calls == 1
    await svc.refresh()          # 冷却中 → 不再请求
    assert src.calls == 1, f"冷却期内不该再请求：{src.calls}"
    await svc.refresh(force=True)
    assert src.calls == 2

    # 连续失败 → 冷却翻倍
    st = SourceState(source_id="s1", last_fetch=1000.0, fail_streak=0)
    assert st.cooldown_remaining(45, 1000.0) == 45 * 60
    st.fail_streak = 1
    assert st.cooldown_remaining(45, 1000.0) == 45 * 60 * 2
    st.fail_streak = 3
    assert st.cooldown_remaining(45, 1000.0) == 45 * 60 * 8
    st.fail_streak = 9
    assert st.cooldown_remaining(45, 1000.0) == 45 * 60 * 8, "退避上限 ×8"
    print("   冷却 45 分钟；失败退避 ×2/×4/×8（封顶 ×8）")
    print("✅ test_cooldown_and_backoff PASS")


async def test_all_sources_fail():
    svc = _fresh_service("allfail", [StubSource("s1", fail=True), StubSource("s2", fail=True)])
    got = await svc.refresh()
    assert got == [] and svc.fresh_items() == [], "全挂 → 没有内容可发"
    assert svc.pick_for_digest() is None and svc.pick_image_item() is None
    print("✅ test_all_sources_fail PASS")


async def test_image_items_reserved_for_daily_image():
    """资讯任务只发文字 → 别吃掉带图的条目，否则 19:30 的每日一图会"没图可发" """
    import time
    now = time.time()
    img_item = _item(title="带图的最新条目", img="https://x/a.jpg", pub=now - 60, link="https://x/a")
    plain_item = _item(title="不带图的条目", img="", pub=now - 120, link="https://x/b")
    svc = NewsService(sources=[StubSource("s1", [img_item, plain_item])],
                      state_path=TMP / "reserve" / "state.json")
    await svc.refresh()
    # 资讯任务应该选"不带图"的那条（即使带图的更新）
    picked = svc.pick_for_digest(now=now)
    assert picked is not None and picked.title == "不带图的条目", picked
    svc.mark_pushed(picked)
    # 每日一图仍然拿得到带图的那条
    img_pick = svc.pick_image_item(now=now)
    assert img_pick is not None and img_pick.title == "带图的最新条目", img_pick
    # 只剩带图条目时，资讯任务也不会空手
    svc2 = NewsService(sources=[StubSource("s1", [img_item])], state_path=TMP / "reserve2" / "state.json")
    await svc2.refresh()
    assert svc2.pick_for_digest(now=now) is not None, "只有带图条目时也要能用"
    print("   资讯/破冰优先取不带图条目；带图的留给每日一图；退无可退也不会空手")
    print("✅ test_image_items_reserved_for_daily_image PASS")


async def test_items_persist_and_cooldown_bypass():
    """【P2c-hotfix-2】重启后必须还能选到条目（线上就是"冷却还在、条目没了"导致一直跳过）"""
    tmp = TMP / "persist"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state.json"
    item = _item(title="带图的资讯", img="https://x/1.jpg")
    src = StubSource("s1", [item])
    svc = NewsService(sources=[src], state_path=state)
    await svc.refresh()
    assert src.calls == 1
    assert svc.pick_image_item() is not None

    # 模拟重启：新实例、**同样的 state 文件、空源列表**（连源都没配也不该崩）
    svc2 = NewsService(sources=[], state_path=state)
    assert svc2.pick_image_item() is not None, "重启后应该能从磁盘恢复条目"
    assert svc2.all_items()[0].title == "带图的资讯"
    assert svc2.all_items()[0].image == "https://x/1.jpg"

    # 手里没条目时，冷却不该拦住取数
    svc3 = NewsService(sources=[StubSource("s1", [item])], state_path=tmp / "s3.json")
    svc3.state_of("s1").last_fetch = 9e9          # 假装"刚拉过、冷却中"
    got = await svc3.refresh()
    assert got, "没有缓存条目时应该忽略冷却、强制拉一轮"
    # 手里有条目时，冷却仍然生效
    src4 = StubSource("s1", [item])
    svc4 = NewsService(sources=[src4], state_path=tmp / "s4.json")
    await svc4.refresh()
    assert src4.calls == 1
    await svc4.refresh()
    assert src4.calls == 1, "有内容时冷却必须生效（别打爆源站）"
    print("   条目落盘可恢复；无内容时绕过冷却、有内容时冷却生效")
    print("✅ test_items_persist_and_cooldown_bypass PASS")


# ---------- 图片 ----------

class StubHttp:
    def __init__(self, status=200, ctype="image/jpeg", data=b"x" * 100, exc=None,
                 statuses=None):
        """statuses：按顺序返回的状态码（用来验证"403 → 去掉 Referer 重试"）"""
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = data
        self._exc = exc
        self._statuses = list(statuses or [])
        self.seen_headers: list = []

    async def get(self, url, **kwargs):
        self.seen_headers.append(dict(kwargs.get("headers") or {}))
        if self._exc:
            raise self._exc
        if self._statuses:
            self.status_code = self._statuses.pop(0)
        return self

    async def aclose(self):
        pass


async def test_download_image():
    from app.news.image import download_image, origin_of
    import httpx as _httpx

    assert origin_of("https://image.gcores.com/a.jpg") == "https://image.gcores.com/"
    assert origin_of("") == "" and origin_of("not a url") == ""

    tmp = TMP / "img"
    shutil.rmtree(tmp, ignore_errors=True)
    # 正常（默认 Referer = 图片自己站点，不再硬编码 B站）
    cli = StubHttp()
    p = await download_image("https://image.gcores.com/a.jpg", dest_dir=tmp, client=cli)
    assert p is not None and p.exists() and p.suffix == ".jpg", p
    assert cli.seen_headers[0].get("Referer") == "https://image.gcores.com/", cli.seen_headers
    # 自定义 Referer（文章页站点）优先
    cli2 = StubHttp()
    await download_image("https://image.gcores.com/b.jpg", dest_dir=tmp, client=cli2,
                         referer="https://www.gcores.com/")
    assert cli2.seen_headers[0]["Referer"] == "https://www.gcores.com/", cli2.seen_headers
    # 403 → 去掉 Referer 重试并成功（2026-10-08 线上就是被 403 挡住的）
    cli3 = StubHttp(statuses=[403, 200])
    p3 = await download_image("https://i0.hdslb.com/c.jpg", dest_dir=tmp, client=cli3)
    assert p3 is not None and p3.exists(), p3
    assert len(cli3.seen_headers) == 2 and "Referer" in cli3.seen_headers[0]
    assert "Referer" not in cli3.seen_headers[1], cli3.seen_headers
    # 403 两次 → None
    cli4 = StubHttp(statuses=[403, 403])
    assert await download_image("https://i0.hdslb.com/d.jpg", dest_dir=tmp, client=cli4) is None
    # 非图片
    assert await download_image("https://x/b.html", dest_dir=tmp,
                               client=StubHttp(ctype="text/html")) is None
    # 超大
    assert await download_image("https://x/c.jpg", dest_dir=tmp, max_mb=0.00001,
                               client=StubHttp(data=b"y" * 100000)) is None
    # HTTP 500 / 超时
    assert await download_image("https://x/d.jpg", dest_dir=tmp, client=StubHttp(status=500)) is None
    assert await download_image("https://x/e.jpg", dest_dir=tmp,
                               client=StubHttp(exc=_httpx.TimeoutException("t"))) is None
    # 非 http 链接
    assert await download_image("file:///etc/passwd", dest_dir=tmp, client=StubHttp()) is None
    # CQ 码格式
    cq = to_cq_image(Path("C:/a/b.png"))
    assert cq.startswith("[CQ:image,file=file:///") and "\\" not in cq, cq
    print(f"   下载正常图片 → {p.name}；403 会去掉 Referer 重试；非图片/超大/500/超时全部 None")
    print("✅ test_download_image PASS")


# ---------- 话题库与任务 ----------

def test_topic_pool_rotation():
    tmp = TMP / "topics"
    shutil.rmtree(tmp, ignore_errors=True)
    pool = TopicPool(path=tmp / "topics.json")
    assert len(pool.topics) == len(PRESET_TOPICS) == 30
    assert len(PRESET_QUOTES) == 20

    first = pool.pick(GID)
    pool.mark_used(GID, first["id"])
    second = pool.pick(GID)
    assert second["id"] != first["id"], "不该连着抽同一条"
    # 优先抽"没用过的"：把前 3 条标记用过，再抽应该都不是它们（最近用过的排最后）
    for t in pool.topics[:3]:
        pool.mark_used(GID, t["id"])
    third = pool.pick(GID)
    assert third["id"] not in {first["id"], second["id"]}
    # 落盘
    pool2 = TopicPool(path=tmp / "topics.json")
    assert pool2.stats()["used"] >= 3
    # 按类型过滤
    only = pool.pick(GID, kind="冷知识")
    assert only and only["type"] == "冷知识", only
    print(f"   话题库 {pool.stats()['total']} 条，已用 {pool.stats()['used']}；轮播与类型过滤 OK")
    print("✅ test_topic_pool_rotation PASS")


async def test_daily_quote_task():
    tmp = TMP / "quote"
    shutil.rmtree(tmp, ignore_errors=True)
    pool = TopicPool(path=tmp / "q.json", topics=list(PRESET_QUOTES))
    task = DailyQuoteTask(pool)
    ctx = TaskContext(group_id=GID, now=0.0, config={}, group_config={})
    parts = await task.render(ctx)
    assert parts and parts[0].startswith("💬 "), parts
    first = parts[0]

    # LLM 原创日：generator 成功 → 用原创
    old_n = Config.QUOTE_LLM_EVERY_DAYS
    Config.QUOTE_LLM_EVERY_DAYS = 1          # 每天都允许原创
    try:
        task2 = DailyQuoteTask(pool, generator=lambda: _ret("「自己写的」一句原创"))
        parts2 = await task2.render(ctx)
        assert "自己写的" in parts2[0], parts2

        async def boom():
            raise RuntimeError("LLM 挂了")
        task3 = DailyQuoteTask(pool, generator=boom)
        parts3 = await task3.render(ctx)
        assert parts3 and parts3[0].startswith("💬 "), "LLM 失败必须回落池子"
        assert "自己写的" not in parts3[0]
    finally:
        Config.QUOTE_LLM_EVERY_DAYS = old_n

    # 【修复】原来断言写死了"今天不该原创"——should_generate_today 判断的是运行日
    # （年积日 %4），写测试那天 281%4≠0 恰好通过，之后每 4 天（288/292…）就会撞红。
    # 改成注入固定日期：2026-10-07 年积日 280（280%4==0 轮到原创），10-08 年积日 281（不轮到）
    _d280 = _dt.datetime(2026, 10, 7, 12, 0, 0).timestamp()
    _d281 = _dt.datetime(2026, 10, 8, 12, 0, 0).timestamp()
    assert should_generate_today(now=_d280) is True, "年积日 280（280%4==0）应轮到 LLM 原创"
    assert should_generate_today(now=_d281) is False, "年积日 281（281%4≠0）不该原创"
    print(f"   每日一句：池子轮播 + LLM 原创（失败回落）OK；样例 {first[:26]}…")
    print("✅ test_daily_quote_task PASS")


async def _ret(v):
    return v


async def test_news_digest_task():
    tmp = TMP / "digest"
    shutil.rmtree(tmp, ignore_errors=True)
    item = _item(title="《测试游戏》公布新 PV", link="https://x/1", img="https://x/1.jpg",
                 source="机核", sid="gcores")
    svc = NewsService(sources=[StubSource("s1", [item])], state_path=tmp / "state.json")
    seen = {}

    async def commenter(it):
        seen["item"] = it
        return "这pv看着挺对味的"

    task = NewsDigestTask(svc, commenter=commenter)
    parts = await task.render(TaskContext(group_id=GID, now=0.0, config={}, group_config={}))
    text = parts[0]
    assert "《测试游戏》公布新 PV" in text and "这pv看着挺对味的" in text, text
    assert "来源：机核" in text and "https://x/1" in text, text
    assert "摘要" not in text, "不能把正文/摘要塞进推送"
    assert seen["item"] is item
    assert svc.has_pushed(item), "推过的要记指纹（跨天去重）"

    # 再跑一次：没有新条目 → TaskSkip
    try:
        await task.render(TaskContext(group_id=GID, now=0.0, config={}, group_config={}))
        raise AssertionError("没有新条目应该跳过")
    except TaskSkip as e:
        assert "没有可推的新资讯" in str(e), e
    print(f"   资讯文案：{text.splitlines()[0][:28]}… （含点评+来源+链接，不含正文）")
    print("✅ test_news_digest_task PASS")


async def test_daily_image_task():
    tmp = TMP / "image_task"
    shutil.rmtree(tmp, ignore_errors=True)
    item = _item(title="某作新图公开", link="https://x/9", img="https://x/9.jpg", source="游研社", sid="yystv")
    svc = NewsService(sources=[StubSource("s1", [item])], state_path=tmp / "state.json")
    img_dir = tmp / "imgs"
    img_dir.mkdir(parents=True, exist_ok=True)
    real = img_dir / "a.jpg"
    real.write_bytes(b"jpg")

    async def fetcher(url):
        return real if url else None

    async def captioned(it):
        return "今天也想把这股新鲜感分你们一半"

    task = DailyImageTask(svc, fetcher=fetcher, captioned=captioned)
    parts = await task.render(TaskContext(group_id=GID, now=0.0, config={}, group_config={}))
    text = parts[0]
    assert text.startswith("[CQ:image,file=file:///"), text
    assert "某作新图公开" in text and "来源：游研社" in text and "https://x/9" in text, text
    assert "新鲜感" in text, text

    # 取不到图 → 跳过（不发纯文字充数）
    async def no_fetch(url):
        return None
    item2 = _item(title="第二张", link="https://x/10", img="https://x/10.jpg")
    svc2 = NewsService(sources=[StubSource("s2", [item2])], state_path=tmp / "s2.json")
    task2 = DailyImageTask(svc2, fetcher=no_fetch)
    try:
        await task2.render(TaskContext(group_id=GID, now=0.0, config={}, group_config={}))
        raise AssertionError("取不到图应该跳过")
    except TaskSkip as e:
        assert "配图下载失败" in str(e), e
    print("   每日一图：带 CQ 图 + 来源 + 链接；取不到图 → 跳过")
    print("✅ test_daily_image_task PASS")


async def test_rotating_icebreak_source():
    calls: list[str] = []

    async def a(ctx):
        calls.append("a")
        return ""            # 空 → 应该轮到下一个

    async def b(ctx):
        calls.append("b")
        return "B 的内容"

    async def c(ctx):
        calls.append("c")
        return ["C 的内容"]

    src = make_rotating_source([("A", a), ("B", b), ("C", c)])
    ctx = TaskContext(group_id=GID, now=0.0, config={}, group_config={})
    parts = await src.render(ctx)
    assert parts == ["B 的内容"], parts
    assert calls == ["a", "b"], calls
    # 下一次从 C 开始（轮换）
    calls.clear()
    parts2 = await src.render(ctx)
    assert parts2 == ["C 的内容"] and calls == ["c"], (parts2, calls)
    # 全部为空 → 空
    async def empty(ctx):
        return ""
    src2 = make_rotating_source([("X", empty)])
    assert await src2.render(ctx) == []
    print("   破冰内容源轮换：空的自动跳过下一个；成功后续用下一种")
    print("✅ test_rotating_icebreak_source PASS")


def test_effect_metrics_by_kind():
    from app import metrics as M
    tmp = TMP / "metrics"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    m = M.Metrics(path=tmp / "m.json", save_every=999)
    m.record_proactive(GID, "icebreak")
    m.record_message(GID, "assistant")       # 主动消息本身也是一条 bot 消息
    m.record_message(GID, "user")            # 10 分钟内有人说话 → 被接
    m.record_proactive(GID, "weather_morning")
    m.record_message(GID, "assistant")
    snap = m.snapshot()
    by_kind = {k["kind"]: k for k in snap["by_kind"]}
    assert by_kind["icebreak"]["sent"] == 1 and by_kind["icebreak"]["replied"] == 1, by_kind
    assert by_kind["weather_morning"]["sent"] == 1 and by_kind["weather_morning"]["replied"] == 0, by_kind
    assert by_kind["icebreak"]["reply_rate"] == 1.0
    print(f"   按内容类型统计：{[(k['kind'], k['sent'], k['replied']) for k in snap['by_kind']]}")
    print("✅ test_effect_metrics_by_kind PASS")


async def _run_all():
    test_parse_rss()
    test_parse_rss_namespace_and_rdf()
    test_parse_bilibili()
    test_parse_time()
    await test_fingerprint_and_dedup()
    await test_cooldown_and_backoff()
    await test_all_sources_fail()
    await test_image_items_reserved_for_daily_image()
    await test_items_persist_and_cooldown_bypass()
    await test_download_image()
    test_topic_pool_rotation()
    await test_daily_quote_task()
    await test_news_digest_task()
    await test_daily_image_task()
    await test_rotating_icebreak_source()
    test_effect_metrics_by_kind()


if __name__ == "__main__":
    print("=== P2c 回归测试（资讯 / 话题库 / 三个任务 / 效果统计）===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：解析/频控/去重/图片/话题轮播/任务/统计 都符合设计")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
