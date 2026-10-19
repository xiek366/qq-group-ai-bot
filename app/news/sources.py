"""
资讯源（P2c-1 / P2c-2）
======================

**零新依赖**：RSS 用标准库 `xml.etree.ElementTree` 解析（轻量化红线），
B站专栏用已有的 `httpx` 打 JSON 接口。

两类源，同一个输出结构（`NewsItem`）：

| 源类型 | 地址 | 实测结论（ROADMAP 附录 A） |
|---|---|---|
| `rss` | 机核 `https://www.gcores.com/rss` | ✅ RSS 2.0，有 `thumb` 封面 |
| `rss` | 游研社 `https://www.yystv.cn/rss/feed` | ✅ RSS 2.0，有 `category`，图在 `description` 里 |
| `bilibili_article` | `api.bilibili.com/x/space/article?mid=<UID>` | ✅ 无需 key/签名，但要**浏览器 UA + Referer**，且按 IP 限速 |

**解析要宽容**：真实 feed 里字段缺失、CDATA、命名空间、畸形条目都很常见 ——
缺字段就留空、坏条目跳过，绝不因为一条脏数据让整个源失败（否则"某天某源没更新"会变成"全都不发"）。
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Optional, Protocol

import httpx

from app.config import Config
from app.news.models import NewsItem

logger = logging.getLogger(__name__)

# 浏览器 UA：B站 必需（A.7 实测：缺了容易被风控），RSS 站点也更友好
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def _abs_url(u: str) -> str:
    """把协议相对 URL 补全：`//i0.hdslb.com/x.jpg` → `https://i0.hdslb.com/x.jpg`

    **为什么必须有**（2026-10-08 线上踩到）：B站与游研社的图片经常给"协议相对"地址，
    而 `NewsItem.has_image` 要求以 `http` 开头 —— 不补全的话这些图会被当成"没图"，
    每日一图就会一直报"没有带图的新资讯可发"。
    """
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    return u


def _local(tag: str) -> str:
    """去掉 XML 命名空间前缀：`{http://purl.org/rss/1.0/}title` → `title`"""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _text(elem) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _parse_time(raw: str) -> float:
    """RFC822（RSS 常见）/ 秒级时间戳字符串 → Unix 时间戳；失败返回 0"""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    if raw.isdigit():
        v = float(raw)
        return v / 1000 if v > 1e11 else v          # B站给的是毫秒
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(raw[:19], fmt))
        except Exception:
            continue
    return 0.0


def parse_rss(xml_text: str, *, source: str, source_id: str, limit: int = 20) -> list[NewsItem]:
    """解析 RSS 2.0 → NewsItem 列表（坏条目跳过，不让一条脏数据毁掉整个源）"""
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error(f"[News] {source_id} RSS 解析失败: {e}")
        return []

    # 找到所有 item（RSS 2.0：channel/item；RDF：直接 item）
    nodes = [el for el in root.iter() if _local(el.tag) == "item"]
    for node in nodes[:limit]:
        try:
            title = link = guid = pub = img = category = desc = ""
            for child in node:
                name = _local(child.tag)
                if name == "title":
                    title = _text(child)
                elif name == "link":
                    link = _text(child)
                elif name == "guid":
                    guid = _text(child)
                elif name == "pubdate":
                    pub = _text(child)
                elif name == "category":
                    category = category or _text(child)
                elif name in ("description", "encoded", "summary"):
                    desc = desc or _text(child)
                elif name in ("thumb", "thumbnail", "image"):
                    img = img or _text(child)
                elif name == "enclosure":
                    img = img or (child.attrib.get("url") or "")
                elif name in ("content",) and child.attrib.get("url"):
                    img = img or child.attrib["url"]

            if not title:
                continue
            if not img:
                m = _IMG_SRC_RE.search(desc or "")
                if m:
                    img = m.group(1)
            # 摘要去掉 HTML 标签，只给 LLM 看要点（正文一律不推送）
            summary = re.sub(r"<[^>]+>", " ", desc or "")
            summary = re.sub(r"\s+", " ", summary).strip()[:300]

            items.append(NewsItem(
                source=source, source_id=source_id, title=title[:160],
                link=_abs_url(link or guid), pub_time=_parse_time(pub), image=_abs_url(img),
                category=category[:20], summary=summary, guid=guid,
            ))
        except Exception as e:                       # 单条脏数据不影响其它条目
            logger.debug(f"[News] {source_id} 跳过一条异常 item: {e}")
            continue
    logger.info(f"[News] {source_id} 解析到 {len(items)} 条")
    return items


def parse_bilibili_articles(payload: dict, *, source: str, source_id: str,
                            limit: int = 20) -> list[NewsItem]:
    """解析 B站 `x/space/article` 返回 → NewsItem 列表"""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.warning(f"[News] {source_id} 接口返回异常: code={payload.get('code') if isinstance(payload, dict) else '?'}")
        return []
    articles = ((payload.get("data") or {}).get("articles")) or []
    items: list[NewsItem] = []
    for a in articles[:limit]:
        try:
            title = (a.get("title") or "").strip()
            cid = a.get("id")
            if not title or not cid:
                continue
            images = a.get("image_urls") or []
            items.append(NewsItem(
                source=source, source_id=source_id, title=title[:160],
                link=f"https://www.bilibili.com/read/cv{cid}",
                pub_time=_parse_time(str(a.get("publish_time") or "")),
                image=_abs_url((images[0] if images else "") or ""),
                summary=(a.get("summary") or "")[:300],
                guid=f"cv{cid}",
            ))
        except Exception as e:
            logger.debug(f"[News] {source_id} 跳过一条异常专栏: {e}")
            continue
    logger.info(f"[News] {source_id} 解析到 {len(items)} 条专栏")
    return items


class NewsSource(Protocol):
    source_id: str
    display_name: str

    async def fetch(self, *, etag: str = "", last_modified: str = "") -> tuple[list[NewsItem], str, str, bool]:
        """返回 (items, etag, last_modified, not_modified)"""
        ...


class RssSource:
    """通用 RSS 2.0 源"""

    def __init__(self, source_id: str, display_name: str, url: str,
                 client: Optional[httpx.AsyncClient] = None, limit: int = 20):
        self.source_id = source_id
        self.display_name = display_name
        self.url = url
        self._client = client
        self._limit = limit

    async def fetch(self, *, etag: str = "", last_modified: str = ""):
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        client = self._client or httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT, follow_redirects=True)
        try:
            resp = await client.get(self.url, headers=headers)
            if resp.status_code == 304:
                return [], etag, last_modified, True
            if resp.status_code != 200:
                logger.warning(f"[News] {self.source_id} HTTP {resp.status_code}")
                return [], etag, last_modified, False
            items = parse_rss(resp.text, source=self.display_name,
                              source_id=self.source_id, limit=self._limit)
            return (items, resp.headers.get("ETag", ""),
                    resp.headers.get("Last-Modified", ""), False)
        except httpx.TimeoutException:
            logger.warning(f"[News] {self.source_id} 请求超时（{Config.HTTP_TIMEOUT}s）")
            return [], etag, last_modified, False
        except Exception as e:
            logger.warning(f"[News] {self.source_id} 请求失败: {e}")
            return [], etag, last_modified, False
        finally:
            if self._client is None:
                try:
                    await client.aclose()
                except Exception:
                    pass


class BilibiliArticleSource:
    """B站专栏源（按 UP 主 mid 订阅）

    实测要点（A.7）：无需 key / 无需签名，但**必须带浏览器 UA + Referer**，且按 IP 限速 ——
    所以冷却与退避在 service 层统一处理，这里只负责一次请求。
    """

    def __init__(self, mid: str, display_name: str = "",
                 client: Optional[httpx.AsyncClient] = None, limit: int = 20):
        self.mid = str(mid)
        self.source_id = f"bili_{self.mid}"
        self.display_name = display_name or f"B站·{self.mid}"
        self._client = client
        self._limit = limit

    async def fetch(self, *, etag: str = "", last_modified: str = ""):
        url = "https://api.bilibili.com/x/space/article"
        params = {"mid": self.mid, "ps": min(self._limit, 12), "pn": 1, "sort": "publish_time"}
        headers = {
            "User-Agent": BROWSER_UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }
        client = self._client or httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT, follow_redirects=True)
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[News] {self.source_id} HTTP {resp.status_code}（B站风控敏感，稍后重试）")
                return [], etag, last_modified, False
            items = parse_bilibili_articles(resp.json(), source=self.display_name,
                                            source_id=self.source_id, limit=self._limit)
            return items, "", "", False
        except httpx.TimeoutException:
            logger.warning(f"[News] {self.source_id} 请求超时")
            return [], etag, last_modified, False
        except Exception as e:
            logger.warning(f"[News] {self.source_id} 请求失败: {e}")
            return [], etag, last_modified, False
        finally:
            if self._client is None:
                try:
                    await client.aclose()
                except Exception:
                    pass


# 预置源（Q1 的答复：RSS 两个 + B站三个）
PRESET_RSS = (
    ("gcores", "机核", "https://www.gcores.com/rss"),
    ("yystv", "游研社", "https://www.yystv.cn/rss/feed"),
)
PRESET_BILI = (
    ("2072586344", "B站·Gal周报"),
    ("5441521", "B站·MoeSekai情报局"),
    ("3546730752641311", "B站·KALPA官方"),
    # 2026-10-19 用户追加：两个日更/周更级源，充实非机核产量
    ("2024069799", "B站·萌村百科"),      # 日更：二次元图鉴/壁纸（对每日一图友好）
    ("10003900", "B站·术力口百科姬"),    # 低频：Voca.wiki 官方，术力口向
)


# ===== COLG 论坛（bbs.colg.cn，DNF 玩家社区）=====
# 站方关闭了 Discuz 自带 RSS（forum.php?mod=rss → "RSS Disabled"），只能走 HTML 解析。
# 板块页（forum-<fid>-1.html）是服务端渲染的 Discuz 列表，含 回复/浏览 数 —— 正好用来做质量过滤。
# 实测（2026-10-12）：fid=171 国服心情咖啡屋 中位回复 60 / 浏览 3125；
#                    fid=170 韩版搬运区 回复中位数 0，全是短问答 → 默认不启用。
COLG_DEFAULT_FIDS = (171,)   # 171 = DNF心情咖啡屋（国服主讨论区）
COLG_DISPLAY = "COLG论坛"

_TBODY_RE = re.compile(r'<tbody id="normalthread_(\d+)"[^>]*>(.*?)</tbody>', re.S)


def _to_int(s: str) -> int:
    """"1,234" / "3.2万" → int（COLG 数字列带逗号，浏览量可能给"万"）"""
    s = (s or "").replace(",", "").strip()
    if s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    return int(s) if s.isdigit() else 0


def parse_colg_forum(html: str, *, limit: int = 25, now: Optional[float] = None) -> list[dict]:
    """COLG 板块页 HTML → [{tid,title,link,pub,replies,views}]（纯函数，可离线测试）

    只解析 `normalthread_` tbody（stickthread_ 是置顶公告，不当资讯推）。
    时间取 dbdateline（Unix 秒，thread_<tid>_new_show 锚点上）；官方活动帖会把
    dbdateline 写成未来时间（活动结束时间），pub > now 的按"首见时间"兜底。
    解析不出时间的同样用 now 兜底 —— 48h 新鲜度线按首见算，指纹去重保证一条只推一次。
    """
    now = now if now is not None else time.time()
    out: list[dict] = []
    for m in _TBODY_RE.finditer(html):
        tid, block = m.group(1), m.group(2)
        # 标题：th 里指向本帖的链接（跳过纯数字文本 = num 列的回复数链接）
        title = ""
        for am in re.finditer(
                r'<a[^>]+href="(?:thread-' + tid + r'-\d+-\d+\.html|[^"]*viewthread&amp;tid=' +
                tid + r')[^"]*"[^>]*>((?:<[^>]+>)*)([^<]{4,160})', block):
            cand = am.group(2).strip()
            if cand and not cand.replace(",", "").isdigit():
                title = cand
                break
        if not title:
            continue
        # 发表时间：优先 dbdateline Unix 秒；否则 by 列的 "2026-5-18 15:56" 字符串
        pub = 0.0
        dm = re.search(r'dbdateline="(\d{10})"', block)
        if dm:
            pub = float(dm.group(1))
        else:
            bm = re.search(
                r'<td class="by">.*?<em[^>]*>(?:<span[^>]*>)?(\d{4})-(\d{1,2})-(\d{1,2})'
                r'(?:\s+(\d{1,2}):(\d{2}))?', block, re.S)
            if bm:
                y, mo, d = int(bm.group(1)), int(bm.group(2)), int(bm.group(3))
                hh, mi = int(bm.group(4) or 12), int(bm.group(5) or 0)
                try:
                    pub = time.mktime((y, mo, d, hh, mi, 0, 0, 0, -1))
                except (OverflowError, ValueError):
                    pub = 0.0
        if pub > now + 3600:          # 未来时间戳（活动帖套路）→ 不可信，用首见时间
            pub = 0.0
        # 回复/浏览：num 列 <a>回复数</a><em>浏览数</em>
        replies = views = 0
        nm = re.search(
            r'<td class="num">\s*<a[^>]*>([\d,万.]+)</a>\s*(?:<em[^>]*>([\d,万.]+)</em>)?', block)
        if nm:
            replies = _to_int(nm.group(1))
            views = _to_int(nm.group(2)) if nm.group(2) else 0
        out.append({
            "tid": tid, "title": title[:160],
            "link": f"https://bbs.colg.cn/thread-{tid}-1-1.html",
            "pub": pub if pub > 0 else now,
            "replies": replies, "views": views,
        })
        if len(out) >= limit:
            break
    return out


class ColgSource:
    """COLG 论坛源（HTML 解析 + 质量过滤）

    质量过滤（用户要求 2026-10-12）：论坛灌水帖多，用板块页自带的 回复/浏览 数挡掉
    低质帖 —— 回复和浏览是"或"的关系（0 回复但 5000 浏览的围观帖有价值，
    20 回复但 100 浏览的小圈子讨论也有价值），两个都低才丢。
    """

    source_id = "colg"
    display_name = COLG_DISPLAY

    def __init__(self, fids, min_replies: int = 5, min_views: int = 500,
                 client: Optional[httpx.AsyncClient] = None, limit: int = 25):
        self.fids = [int(f) for f in fids] or list(COLG_DEFAULT_FIDS)
        self.min_replies = max(0, int(min_replies))
        self.min_views = max(0, int(min_views))
        self._client = client
        self._limit = limit

    async def fetch(self, *, etag: str = "", last_modified: str = ""):
        """依次抓各板块第一页（Discuz 动态页不支持 ETag 条件请求，冷却由 service 层管）"""
        collected: list[NewsItem] = []
        client = self._client or httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT, follow_redirects=True)
        try:
            for fid in self.fids:
                url = f"https://bbs.colg.cn/forum-{fid}-1.html"
                try:
                    resp = await client.get(url, headers={"User-Agent": BROWSER_UA})
                    if resp.status_code != 200:
                        logger.warning(f"[News] colg fid={fid} HTTP {resp.status_code}")
                        continue
                    rows = parse_colg_forum(resp.text, limit=self._limit)
                except httpx.TimeoutException:
                    logger.warning(f"[News] colg fid={fid} 请求超时")
                    continue
                except Exception as e:
                    logger.warning(f"[News] colg fid={fid} 解析失败: {e}")
                    continue
                for row in rows:
                    # 质量闸：回复、浏览都低于阈值 → 丢弃（低质灌水帖）
                    if row["replies"] < self.min_replies and row["views"] < self.min_views:
                        continue
                    collected.append(NewsItem(
                        source=COLG_DISPLAY, source_id=self.source_id,
                        title=row["title"], link=row["link"], pub_time=row["pub"],
                        image="", category=f"fid{fid}", summary="",
                        guid=f"colg{row['tid']}",
                    ))
            logger.info(f"[News] colg 解析到 {len(collected)} 条（质量闸 回复≥{self.min_replies} 或 浏览≥{self.min_views}）")
            return collected, "", "", False
        except Exception as e:
            logger.warning(f"[News] colg 拉取失败: {e}")
            return [], "", "", False
        finally:
            if self._client is None:
                try:
                    await client.aclose()
                except Exception:
                    pass


def build_sources(client: Optional[httpx.AsyncClient] = None, limit: int = 20) -> list:
    """按配置造源列表（可用 NEWS_RSS_SOURCES / NEWS_BILI_MIDS 覆盖或裁剪）"""
    out: list = []

    want_rss = [s.strip() for s in (Config.NEWS_RSS_SOURCES or "").split(",") if s.strip()]
    for sid, name, url in PRESET_RSS:
        if want_rss and sid not in want_rss:
            continue
        out.append(RssSource(sid, name, url, client=client, limit=limit))

    want_bili = [s.strip() for s in (Config.NEWS_BILI_MIDS or "").split(",") if s.strip()]
    for mid, name in PRESET_BILI:
        if want_bili and mid not in want_bili:
            continue
        out.append(BilibiliArticleSource(mid, name, client=client, limit=limit))

    # COLG 论坛（HTML 解析源）：NEWS_COLG_FIDS 留空 = 预置板块（国服咖啡屋）；
    # 填了 = 只用填的 fid（如 "171,170"），NEWS_COLG_ENABLED=false 整体关闭
    if Config.NEWS_COLG_ENABLED:
        fids = [f.strip() for f in (Config.NEWS_COLG_FIDS or "").replace("，", ",").split(",")
                if f.strip().isdigit()]
        out.append(ColgSource(
            fids or COLG_DEFAULT_FIDS,
            min_replies=Config.NEWS_COLG_MIN_REPLIES,
            min_views=Config.NEWS_COLG_MIN_VIEWS,
            client=client, limit=25,
        ))

    return out
