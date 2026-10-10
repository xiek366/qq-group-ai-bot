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
)


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

    return out
