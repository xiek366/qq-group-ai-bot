"""
COLG 论坛源测试（离线，不联网）
================================
fixture 从 2026-10-12 真实板块页（forum-171-1.html）结构裁剪而来，覆盖：
  解析（tid/标题/回复/浏览/万单位）· 置顶排除 · 隐藏行跳过 ·
  未来时间戳兜底 · 字符串时间解析 · 质量闸 · build_sources 接线

运行: python tests/test_news_colg.py （必须在项目根目录运行）
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.news.models import NewsItem
from app.news.sources import ColgSource, build_sources, parse_colg_forum

NOW = 1791870000.0   # 固定"当前时间"，保证断言稳定


def _row(tid, title, db="", by_time="", replies="0", views="0", stick=False):
    """按 COLG 真实结构拼一个 tbody 行"""
    kind = "stickthread" if stick else "normalthread"
    db_attr = f' dbdateline="{db}"' if db else ""
    by = (
        f'<td class="by"><cite><a href="home.php?mod=space&amp;uid=1">作者</a></cite>'
        f'<em><span>{by_time}</span></em></td>' if by_time else ""
    )
    return f'''<tbody id="{kind}_{tid}">
<tr><td class="icn"><a id="thread_folder_{tid}"{db_attr} href="forum.php?mod=viewthread&amp;tid={tid}&amp;extra=page%5F1" title="x" target="_blank"><div class="sprite_folder_common"></div></a></td>
<th><em>[<a rel="nofollow" href="forum.php?mod=forumdisplay&amp;fid=171">闲聊</a>]</em>
<a href="forum.php?mod=viewthread&amp;tid={tid}&amp;extra=page%5F1" onclick="x">{title}</a></th>
{by}
<td class="num"><a href="thread-{tid}-1-1.html" class="xi2">{replies}</a><em>{views}</em></td>
<td class="dfsj_by"><cite><a href="home.php?mod=space&amp;uid=2">路人</a></cite><em><a href="forum.php?mod=viewthread&amp;tid={tid}">2026-10-12 12:00</a></em></td>
</tr>
</tbody>'''


FIXTURE = (
    # 1. 正常热帖：dbdateline Unix 秒 + 万单位浏览
    _row(9609361, "米歇尔攻坚战审判模式前瞻", db=str(int(NOW) - 3600),
         replies="1,234", views="3.2万")
    # 2. 置顶公告 → 必须被排除
    + _row(9458276, "【公告】版规", db=str(int(NOW) - 7200), stick=True)
    # 3. 隐藏 JS wrapper（无 th 标题）→ 跳过
    + '<tbody id="normalthread_9649870" style="display: none;"><tr>'
      '<td class="icn"><a id="thread_folder_9649870" dbdateline="2026-9-10 10:25" '
      'href="forum.php?mod=viewthread&amp;tid=9649870" title="新窗口打开"></a></td></tr></tbody>'
    # 4. 无 dbdateline → 用 by 列字符串时间
    + _row(9651111, "今天测到的有趣 builds", by_time="2026-10-12 08:30", replies="30", views="900")
    # 5. 未来 dbdateline（活动帖把结束时间写进去）→ 兜底成 now
    + _row(9652222, "【活动】盖盖的月饼工坊", db=str(int(NOW) + 86400 * 30),
           replies="999", views="5万")
    # 6. 低质帖：回复 1 浏览 50 → 质量闸丢弃
    + _row(9653333, "随手一发水贴", replies="1", views="50")
)


class StubResp:
    status_code = 200
    text = FIXTURE


class StubClient:
    async def get(self, url, **kw):
        return StubResp()

    async def aclose(self):
        pass


async def main():
    # 1) 解析：置顶排除 + 隐藏行跳过 → 剩 4 条
    rows = parse_colg_forum(FIXTURE, now=NOW)
    tids = [r["tid"] for r in rows]
    assert tids == ["9609361", "9651111", "9652222", "9653333"], tids
    hot = rows[0]
    assert hot["title"] == "米歇尔攻坚战审判模式前瞻", hot
    assert hot["replies"] == 1234 and hot["views"] == 32000, hot        # 逗号 + 万单位
    assert abs(hot["pub"] - (NOW - 3600)) < 5, hot
    # 2) 字符串时间解析
    assert rows[1]["tid"] == "9651111"
    assert rows[1]["pub"] > 0 and rows[1]["pub"] < NOW, rows[1]
    # 3) 未来时间戳兜底为首见时间
    assert abs(rows[2]["pub"] - NOW) < 5, rows[2]
    print(f"1) parse_colg_forum 解析/排除/兜底 ✅  ({len(rows)} 条)")

    # 4) 质量闸：回复≥5 或 浏览≥500 → "随手一发水贴"(1/50) 被丢
    src = ColgSource([171], min_replies=5, min_views=500, client=StubClient())
    items, etag, lm, nm = await src.fetch()
    titles = [i.title for i in items]
    assert "随手一发水贴" not in titles, titles
    assert len(items) == 3, titles
    assert all(isinstance(i, NewsItem) for i in items)
    assert items[0].source_id == "colg" and items[0].source == "COLG论坛"
    assert items[0].link == "https://bbs.colg.cn/thread-9609361-1-1.html", items[0]
    assert items[0].guid == "colg9609361"
    print(f"2) ColgSource 质量闸 ✅  (4 解析 → {len(items)} 条入池，低质 1 条被滤)")

    # 5) build_sources 接线：默认配置下包含 colg 源
    saved = (Config.NEWS_COLG_ENABLED, Config.NEWS_COLG_FIDS)
    try:
        Config.NEWS_COLG_ENABLED = True
        Config.NEWS_COLG_FIDS = "171,170"
        sources = build_sources()
        colgs = [s for s in sources if getattr(s, "source_id", "") == "colg"]
        assert len(colgs) == 1 and colgs[0].fids == [171, 170], colgs
        Config.NEWS_COLG_ENABLED = False
        assert not [s for s in build_sources() if getattr(s, "source_id", "") == "colg"]
    finally:
        Config.NEWS_COLG_ENABLED, Config.NEWS_COLG_FIDS = saved
    print("3) build_sources 接线/开关 ✅")

    print("\n✅ test_news_colg PASS")


if __name__ == "__main__":
    asyncio.run(main())
