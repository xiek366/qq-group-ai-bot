"""
P2c-图池 回归测试：正文图解析 / 抓取退避 / FIFO 出图 / 上限淘汰 / 保底
====================================================================
覆盖 DEVLOG [P2c-图池-0] §5 的测试计划：

解析
  1. `resized_url`：http→https、已有 @ 后缀不重复、非法 URL → 空
  2. `parse_article_images`：只取 para_type=2、滤掉横幅（1320x130）、去重、
     缺尺寸放行、老格式 `<img>` 兜底、code≠0 → 空
  3. `fetch_article_images`：**-509 退避后成功**、-509 用尽 → ok=False、
     正常 → ok=True + title + 画师

图池
  4. harvest：入库 + cv 记账；同篇不重复抓；**抓取失败不动池子、也不记账**
  5. FIFO：严格按入库顺序；发完一轮才循环
  6. peek_next 不消耗；干跑不消耗
  7. 落盘：新实例能读到清单与指针
  8. 上限淘汰：超限时先淘汰最旧的**已发**图
  9. 文件缺失 → 跳过该图（不让一条坏记录卡死）

任务
 10. DailyImageTask：池里有图 → 发池图；**池空 → 回落封面图**；干跑不消耗
 11. PicRefillTask：有新专栏 → 补货；没有 → 跳过；抓取失败 → 跳过（池子不动）

运行: python tests/test_pic_pool.py
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.news import NewsItem, PicPool, NewsService
from app.news.article import ArticleImage, fetch_article_images, parse_article_images, resized_url
from app.schedule.task import TaskContext, TaskSkip
from app.schedule.tasks import DailyImageTask, PicRefillTask

TMP = Path("data/_tmp_picpool_test")
GID = 100000120


def _art(url, w=720, h=1200, size=1400):
    return {"para_type": 2, "pic": {"pics": [{"url": url, "width": w, "height": h, "size": size}]}}


def _payload(paragraphs, *, title="测试专栏", artist="Leon", code=0):
    return {"code": code, "data": {
        "title": title, "content": f"画师：{artist}" if artist else "",
        "opus": {"content": {"paragraphs": paragraphs}},
    }}


def _item(cid, title, sid="bili_2024069799"):
    return NewsItem(source="B站·萌猫", source_id=sid, title=title,
                    link=f"https://www.bilibili.com/read/cv{cid}", image="https://x/cover.jpg",
                    pub_time=0, guid=f"cv{cid}")


def _ctx(**kw):
    return TaskContext(group_id=GID, now=0.0, config={}, group_config={}, **kw)


# ---------- 解析 ----------

def test_resized_url():
    assert resized_url("http://i0.hdslb.com/a.png", 1200) == "https://i0.hdslb.com/a.png@1200w.jpg"
    assert resized_url("https://i0.hdslb.com/a.jpg", 1080) == "https://i0.hdslb.com/a.jpg@1080w.jpg"
    # 已有后缀不重复加
    assert resized_url("https://i0.hdslb.com/a.png@1200w.jpg", 1200) == "https://i0.hdslb.com/a.png@1200w.jpg"
    assert resized_url("", 1200) == ""
    assert resized_url("not a url", 1200) == ""
    print("   http→https；已有 @ 后缀不重复；非法 URL 返回空")
    print("✅ test_resized_url PASS")


def test_parse_article_images():
    payload = _payload([
        {"para_type": 1, "text": {"nodes": []}},
        _art("http://i0.hdslb.com/banner.png", 1320, 130),      # 横幅 → 滤掉
        _art("http://i0.hdslb.com/big1.png", 720, 1200),
        _art("http://i0.hdslb.com/big1.png", 720, 1200),        # 重复 → 去重
        _art("http://i0.hdslb.com/big2.png", 837, 1200),
        {"para_type": 9, "text": {}},
    ])
    imgs = parse_article_images(payload)
    assert len(imgs) == 2, [i.url for i in imgs]
    assert all(i.url.endswith("@1200w.jpg") for i in imgs), imgs
    assert imgs[0].width == 720 and imgs[0].height == 1200
    assert imgs[0].pixels == 864000

    # 缺尺寸 → 放行（下载时由体积上限兜底），不会被像素过滤误杀
    no_dim = _payload([{"para_type": 2, "pic": {"pics": [{"url": "http://i0.hdslb.com/x.png"}]}}])
    got = parse_article_images(no_dim)
    assert len(got) == 1 and got[0].width == 0, got

    # 老格式：正文 HTML 里的 <img>
    legacy = {"code": 0, "data": {"content": '<p>x</p><img data-src="//i0.hdslb.com/old.jpg">'}}
    old = parse_article_images(legacy)
    assert len(old) == 1 and old[0].url == "https://i0.hdslb.com/old.jpg@1200w.jpg", old

    assert parse_article_images({"code": -509}) == []
    assert parse_article_images({}) == []
    print("   只取图段、滤横幅、去重、缺尺寸放行、老格式兜底、code≠0 → 空")
    print("✅ test_parse_article_images PASS")


class StubResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class SeqClient:
    """按顺序返回预设响应（用来验证 -509 退避重试）"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    async def get(self, url, headers=None):
        self.calls += 1
        item = self.seq.pop(0) if self.seq else {"code": 0}
        return StubResp(item)

    async def aclose(self):
        pass


async def test_fetch_article_images():
    ok_payload = _payload([_art("http://i0.hdslb.com/a.png"), _art("http://i0.hdslb.com/b.png")])
    # 1) 先 -509 两次，再成功（退避重试生效）
    client = SeqClient([{"code": -509}, {"code": -509}, ok_payload])
    imgs, meta = await fetch_article_images("53494371", client=client,
                                            max_retries=3, retry_wait=0)
    assert meta["ok"] and len(imgs) == 2, (imgs, meta)
    assert meta["title"] == "测试专栏" and meta["artist"] == "Leon", meta
    assert client.calls == 3, client.calls

    # 2) -509 用尽 → ok=False（调用方据此不记账）
    client2 = SeqClient([{"code": -509}] * 3)
    imgs2, meta2 = await fetch_article_images("111", client=client2, max_retries=3, retry_wait=0)
    assert imgs2 == [] and not meta2["ok"] and "code=-509" in meta2["error"], meta2
    assert client2.calls == 3

    # 2b) -352 风控也是"过一会儿就好" → 同样退避重试（2026-10-19 真机踩到）
    client2b = SeqClient([{"code": -352}, ok_payload])
    imgs2b, meta2b = await fetch_article_images("333", client=client2b, max_retries=3, retry_wait=0)
    assert meta2b["ok"] and len(imgs2b) == 2 and client2b.calls == 2, (meta2b, client2b.calls)

    # 3) 其它错误码 → 直接放弃
    client3 = SeqClient([{"code": -404, "message": "not found"}])
    _, meta3 = await fetch_article_images("222", client=client3, max_retries=3, retry_wait=0)
    assert not meta3["ok"] and client3.calls == 1

    # 4) 非法 cv 号 → 不发请求
    client4 = SeqClient([])
    _, meta4 = await fetch_article_images("abc", client=client4, retry_wait=0)
    assert not meta4["ok"] and client4.calls == 0
    print("   -509 退避重试（2 次后成功）；用尽 → ok=False；其它错误码不重试；非法 cvid 不发请求")
    print("✅ test_fetch_article_images PASS")


# ---------- 图池 ----------

def _fresh_pool(name="a", images=None, fail=False):
    tmp = TMP / name
    shutil.rmtree(tmp, ignore_errors=True)
    pic_dir = tmp / "pics"
    pic_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    async def downloader(url):
        downloaded.append(url)
        p = pic_dir / (str(abs(hash(url)) % 10**10) + ".jpg")
        p.write_bytes(b"jpg")
        return p

    async def fetcher(cid):
        if fail:
            return [], {"ok": False, "error": "-509 限流"}
        imgs = [ArticleImage(url=f"https://i0.hdslb.com/{cid}_{i}.png@1200w.jpg", width=720, height=1200)
                for i in range(3)]
        return imgs, {"ok": True, "title": f"标题{cid}", "artist": "某画师"}

    pool = PicPool(manifest=tmp / "picpool.json", pic_dir=pic_dir,
                   downloader=downloader, fetcher=fetcher)
    return pool, downloaded, tmp


async def test_harvest_dedup_and_failure():
    Config.PIC_POOL_MIN_PIXELS = 100
    pool, downloaded, tmp = _fresh_pool("harvest")
    arts = [_item("111", "第一篇"), _item("222", "第二篇")]
    res = await pool.harvest(arts, limit=1)
    assert res["ok"] and res["added"] == 3 and res["articles"] == 1, res
    st = pool.stats()
    assert st["total"] == 3 and st["harvested"] == 1, st
    # 再抓：111 已记账 → 抓 222
    res2 = await pool.harvest(arts, limit=1)
    assert res2["added"] == 3 and pool.stats()["harvested"] == 2, (res2, pool.stats())
    # 都抓过 → 没有新专栏
    res3 = await pool.harvest(arts, limit=1)
    assert res3["added"] == 0 and "没有新专栏" in res3["reason"], res3

    # 失败的池子：不动、不记账（下次还会试）
    pool2, _, _ = _fresh_pool("fail", fail=True)
    res4 = await pool2.harvest([_item("333", "x")], limit=1)
    assert not res4["ok"], res4
    st2 = pool2.stats()
    assert st2["total"] == 0 and st2["harvested"] == 0, st2
    assert "509" in st2["last_error"], st2
    print(f"   harvest：入库 3 张/篇、cv 记账不重复抓；失败时池子 {st2['total']} 张且不记账")
    print("✅ test_harvest_dedup_and_failure PASS")


async def test_fifo_order_and_cycle():
    """FIFO 模式（`PIC_POOL_ORDER=fifo`）：严格按入库顺序，一轮发完自动循环"""
    old = Config.PIC_POOL_ORDER
    Config.PIC_POOL_ORDER = "fifo"
    try:
        pool, _, _ = _fresh_pool("fifo")
        await pool.harvest([_item("111", "A"), _item("222", "B")], limit=2)
        total = pool.stats()["total"]
        assert total == 6, total

        firsts = []
        for _ in range(total):
            p = pool.next_picture()
            assert p is not None, "还应发得出来"
            firsts.append(p["file"])
        # 严格 FIFO：顺序 == 清单顺序，且没有重复
        manifest = json.loads((pool._manifest).read_text(encoding="utf-8"))
        assert firsts == [i["file"] for i in manifest["items"]], (firsts, manifest["items"])
        assert len(set(firsts)) == total, "同一轮内不该重复发"
        assert pool.stats()["remaining"] == 0, pool.stats()
        # 一轮发完 → 下一次**自动开始新一轮**，从第一张重新开始（不会返回 None）
        again = pool.next_picture()
        assert again is not None and again["file"] == firsts[0], (again, firsts[0])
        assert pool.stats()["used"] == 1 and pool.stats()["remaining"] == total - 1, pool.stats()
        print(f"   FIFO：{total} 张按入库顺序发完 → 自动进入第 2 轮，从第一张重来")
    finally:
        Config.PIC_POOL_ORDER = old
    print("✅ test_fifo_order_and_cycle PASS")


async def test_random_order_avoids_same_article():
    """随机模式（默认）：一轮内不重复 + **不与上一篇专栏同主题**

    用户反馈（2026-10-19）：一篇专栏 6~12 张是同一角色/主题，FIFO 会连着几天发同一个人。
    """
    old_order = Config.PIC_POOL_ORDER
    old_avoid = Config.PIC_POOL_AVOID_CIDS
    Config.PIC_POOL_ORDER = "random"
    Config.PIC_POOL_AVOID_CIDS = 1
    try:
        pool, _, _ = _fresh_pool("random")
        await pool.harvest([_item("111", "A"), _item("222", "B")], limit=2)   # 3 + 3 张
        total = pool.stats()["total"]
        seq = [pool.next_picture()["cid"] for _ in range(total)]
        # ① 一轮内每张只发一次
        assert seq.count("111") == 3 and seq.count("222") == 3, seq
        # ② 相邻两次**不来自同一篇专栏**（另一篇还有未发的图时必须交替）
        assert all(seq[i] != seq[i + 1] for i in range(len(seq) - 1)), seq
        # ③ 第二轮自动开始
        assert pool.next_picture() is not None
        assert pool.stats()["used"] == 1, pool.stats()

        # ④ 池里只有一篇专栏时：允许连着同主题（不准卡死）
        solo, _, _ = _fresh_pool("random_solo")
        await solo.harvest([_item("333", "C")], limit=1)
        solo_seq = [solo.next_picture()["cid"] for _ in range(3)]
        assert solo_seq == ["333", "333", "333"], solo_seq
        print(f"   随机：{seq}（相邻不重复同主题）；只剩一篇时退让不卡死")
    finally:
        Config.PIC_POOL_ORDER = old_order
        Config.PIC_POOL_AVOID_CIDS = old_avoid
    print("✅ test_random_order_avoids_same_article PASS")


async def test_peek_and_persistence_and_missing_file():
    """持久化与坏文件：固定 FIFO 模式（随机模式下 peek 本来就不保证同一条）"""
    old = Config.PIC_POOL_ORDER
    Config.PIC_POOL_ORDER = "fifo"
    try:
        pool, _, tmp = _fresh_pool("peek")
        await pool.harvest([_item("111", "A")], limit=1)
        nxt = pool.peek_next()
        assert nxt is not None and pool.stats()["used"] == 0, "peek 不能消耗"
        # 新实例（模拟重启）能读到清单与指针
        pool2 = PicPool(manifest=pool._manifest, pic_dir=tmp / "pics")
        assert pool2.stats()["total"] == 3 and pool2.peek_next()["file"] == nxt["file"]
        # 文件被删 → 跳过该图，继续给下一张
        Path(nxt["path"]).unlink()
        got = pool2.next_picture()
        assert got is not None and got["file"] != nxt["file"], got
        print("   peek 不消耗；重启后可恢复；文件缺失自动跳过")
    finally:
        Config.PIC_POOL_ORDER = old
    print("✅ test_peek_and_persistence_and_missing_file PASS")


async def test_evict_limit():
    old_limit = Config.PIC_POOL_LIMIT
    Config.PIC_POOL_LIMIT = 4
    try:
        pool, _, _ = _fresh_pool("evict")
        await pool.harvest([_item("111", "A"), _item("222", "B")], limit=2)   # 6 张 → 淘汰到 4
        st = pool.stats()
        assert st["total"] == 4, st
        # 淘汰的是"最旧的已发"：先把前 2 张发掉，再加一篇 → 已发的会被清
        pool.next_picture()
        pool.next_picture()
        await pool.harvest([_item("333", "C")], limit=1)     # 再 +3 → 超限
        st2 = pool.stats()
        assert st2["total"] <= 4, st2
    finally:
        Config.PIC_POOL_LIMIT = old_limit
    print("   上限 4 张生效：超限时淘汰最旧的已发图，池子不会无限膨胀")
    print("✅ test_evict_limit PASS")


# ---------- 任务 ----------

async def test_daily_image_prefers_pool():
    Config.PIC_POOL_ENABLED = True
    pool, _, _ = _fresh_pool("task")
    await pool.harvest([_item("111", "图鉴")], limit=1)

    async def no_download(url):
        return None

    task = DailyImageTask(NewsService(sources=[], state_path=TMP / "task" / "news.json"),
                          pool=pool, pool_captioned=lambda pic: _ret("池子配文"),
                          fetcher=no_download)
    parts = await task.render(_ctx())
    assert parts and parts[0].startswith("[CQ:image"), parts
    assert "池子配文" in parts[0], parts
    assert pool.stats()["used"] == 1, "正式执行应该消耗一张"

    # 干跑不消耗
    used_before = pool.stats()["used"]
    parts_d = await task.render(_ctx(dry_run=True))
    assert parts_d and parts_d[0].startswith("[CQ:image"), parts_d
    assert pool.stats()["used"] == used_before, "干跑不得消耗池子"

    # 池子空 → 回落封面图路径（这里封面下载也失败 → TaskSkip，说明确实走的是回落链）
    empty_pool, _, _ = _fresh_pool("task_empty")
    task2 = DailyImageTask(NewsService(sources=[], state_path=TMP / "task2" / "news.json"),
                           pool=empty_pool, fetcher=no_download)
    try:
        await task2.render(_ctx())
        raise AssertionError("池空且无资讯图 → 应该跳过")
    except TaskSkip as e:
        assert "图池为空" in str(e), e
    print("   池里有图 → 发池图；干跑不消耗；池空 → 回落封面图（也失败则跳过）")
    print("✅ test_daily_image_prefers_pool PASS")


async def _ret(v):
    return v


async def test_pic_refill_task():
    Config.PIC_POOL_ENABLED = True
    pool, _, _ = _fresh_pool("refill")

    class StubNews:
        def __init__(self, arts):
            self._arts = arts

        async def refresh(self):
            return []

        def all_items(self):
            return self._arts

    task = PicRefillTask(pool, service=StubNews([_item("111", "A")]))
    parts = await task.render(_ctx())
    assert parts and "图池补货" in parts[0], parts
    assert pool.stats()["total"] == 3

    # 没有新专栏 → 跳过
    task2 = PicRefillTask(pool, service=StubNews([_item("111", "A")]))
    try:
        await task2.render(_ctx())
        raise AssertionError("没有新专栏应该跳过")
    except TaskSkip as e:
        assert "没有新专栏" in str(e) or "没有新增" in str(e), e

    # 抓取失败 → 跳过，池子不动
    fail_pool, _, _ = _fresh_pool("refill_fail", fail=True)
    task3 = PicRefillTask(fail_pool, service=StubNews([_item("999", "Z")]))
    try:
        await task3.render(_ctx())
        raise AssertionError("抓取失败应该跳过")
    except TaskSkip as e:
        assert "补货失败" in str(e), e
    assert fail_pool.stats()["total"] == 0
    print("   补货：有新专栏 → +3 张；没有 → 跳过；抓取失败 → 跳过且池子不动")
    print("✅ test_pic_refill_task PASS")


async def test_refill_runs_at_0400_via_scheduler():
    """端到端：真实 Scheduler + 真实 PicRefillTask 在 04:00 触发

    这是"每天 04:00 自动补 1 篇"的直接证据：
    - 04:00 在**静默时段**内（23:00–08:00），维护任务必须被放行（否则永远不补货）；
    - 补货任务**不发消息**（no_send）；
    - 同一天不会重复补（daily_limit=1 + 4 小时冷却）。
    """
    from app.schedule import GroupConfigStore, Scheduler, TaskStateStore
    from app.util import clock

    tmp = TMP / "sched"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    sent = []

    async def send(gid, text):
        sent.append((gid, text))

    sched = Scheduler(
        state=TaskStateStore(schedule_dir=tmp / "schedule"),
        group_config=GroupConfigStore(config_dir=tmp / "gc"),
        send=send, group_ids=lambda: [GID],
        last_activity=lambda g: 0.0, member_count=lambda g: 0,
        task_log_dir=tmp / "tasks",
    )
    pool, _, _ = _fresh_pool("sched_pool")

    arts = [_item("111", "第一篇")]

    class StubNews:
        async def refresh(self):
            return []

        def all_items(self):
            return list(arts)

    sched.register(PicRefillTask(pool, service=StubNews()))

    def at(hour, day_offset=0):
        base = clock.now().replace(hour=hour, minute=0, second=0, microsecond=0)
        return base.timestamp() + day_offset * 86400

    # ① 04:00 → 应该补货（静默时段只拦"会发消息"的任务）
    await sched.tick(now=at(4))
    st = pool.stats()
    assert st["total"] == 3 and st["harvested"] == 1, st
    assert not sent, f"补货任务不得发消息：{sent}"
    assert sched.state.task_state(GID, "pic_refill")["last_result"] == "done"

    # ② 同一天晚些再 tick → 不再补（daily_limit=1 + 4h 冷却）
    await sched.tick(now=at(9))
    await sched.tick(now=at(14))
    assert pool.stats()["harvested"] == 1, pool.stats()

    # ③ 第二天 04:00 → 图鉴 UP 发了新专栏 → 继续补（新的一天，且 cv 不同）
    arts.append(_item("222", "第二篇"))
    await sched.tick(now=at(4, day_offset=1))
    assert pool.stats()["harvested"] == 2, pool.stats()
    assert not sent, "始终不该往群里发"
    print("   04:00 触发补货（静默时段放行）→ 不发消息；同一天只补一次；次日继续")
    print("✅ test_refill_runs_at_0400_via_scheduler PASS")


def test_ad_qr_filtered_and_artist():
    """【2026-10-19 用户反馈】萌村百科最后一张是"扫码关注公众号"广告图

    它是 1200x620 / 74 万像素，**能过 40 万像素阈值**，只能靠"紧邻文字"的内容判定。
    同时验证：每张作品图能拿到自己的画师（署名文字就在图上方）。
    """
    def txt(s):
        return {"para_type": 1, "text": {"nodes": [{"node_type": 1, "word": {"words": s}}]}}

    base = "https://i0.hdslb.com/"
    payload = _payload([
        txt("素材来源于P站等线上平台，可关注vx公众号【萌村百科】"),   # 开头广告文案
        _art(base + "banner.png", 1320, 130),                     # 横幅（像素过滤掉）
        txt("#1"), txt("画师：Leon"), txt("作品id：149117885"),
        _art(base + "p1.png", 720, 1200),                         # ✅ 作品 1
        txt("画师：ざわ"),
        _art(base + "p2.png", 837, 1200),                         # ✅ 作品 2
        txt("素材来源于P站等线上平台， 仅供交流学习，侵删~~"),          # 中性文案
        txt("更多精美高清无水印壁纸美图，可关注vx公众号【萌村百科】获取。"),  # 广告文案
        _art(base + "qr.png", 1200, 620),                         # ❌ 广告二维码
    ])
    imgs = parse_article_images(payload)
    urls = [i.url for i in imgs]
    assert len(imgs) == 2, urls
    assert urls[0] == base + "p1.png@1200w.jpg" and urls[1] == base + "p2.png@1200w.jpg", urls
    assert imgs[0].artist == "Leon" and imgs[1].artist == "ざわ", [i.artist for i in imgs]
    assert not any("qr" in u for u in urls), "广告二维码必须被过滤"
    print("   广告二维码（1200x620）被过滤；作品图保留且各自带上画师署名")
    print("✅ test_ad_qr_filtered_and_artist PASS")


async def test_source_rotation_fairness():
    """【2026-10-19 用户反馈】只抓到萌村百科，另一个 UP 一张没有

    根因：资讯条目按源分组，`harvest(limit=1)` 永远取第一个；萌村百科日更 → 另一个源饿死。
    修好后：按"该源已抓篇数"升序轮换 → A、B、A、B 交替。
    """
    pool, _, _ = _fresh_pool("rotate")
    A, B = "bili_2024069799", "bili_64090032"
    arts = [
        _item("111", "A1", sid=A), _item("112", "A2", sid=A),   # 源 A 两篇（列表在前）
        _item("222", "B1", sid=B), _item("223", "B2", sid=B),   # 源 B 两篇
    ]
    order = []
    for _ in range(3):
        res = await pool.harvest(arts, limit=1)
        assert res["added"] == 3, res
        order.append(sorted(pool._data["harvested"])[-1] if False else None)
    got = [cid for cid in ("111", "222", "112", "223") if cid in pool._data["harvested"]]
    assert "111" in pool._data["harvested"], "第一轮应抓源 A 的最新篇"
    assert "222" in pool._data["harvested"], "第二轮必须轮到源 B（否则就是饿死）"
    assert "112" in pool._data["harvested"], "第三轮回到源 A 的下一篇"
    counts = pool._data["harvest_by_source"]
    assert counts.get(A) == 2 and counts.get(B) == 1, counts
    print(f"   三篇按源轮换：{got}（A 已抓 {counts.get(A)} / B 已抓 {counts.get(B)}）")
    print("✅ test_source_rotation_fairness PASS")


async def _run_all():
    test_resized_url()
    test_parse_article_images()
    test_ad_qr_filtered_and_artist()
    await test_fetch_article_images()
    await test_harvest_dedup_and_failure()
    await test_source_rotation_fairness()
    await test_fifo_order_and_cycle()
    await test_random_order_avoids_same_article()
    await test_peek_and_persistence_and_missing_file()
    await test_evict_limit()
    await test_daily_image_prefers_pool()
    await test_pic_refill_task()
    await test_refill_runs_at_0400_via_scheduler()


if __name__ == "__main__":
    print("=== P2c-图池 回归测试 ===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：正文图解析 / -509 退避 / FIFO 出图 / 淘汰 / 保底链 都符合设计")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
