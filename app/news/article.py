"""
专栏正文图抓取（P2c-图池）
=========================

**为什么需要它**：B站专栏列表接口（`x/space/article`）与 `viewinfo` **都只给 1 张头图**，
所以"每篇专栏只能产出 1 张图"——测试几次就把图池消耗完了。

**正文图在哪**（2026-10-19 实测跑通）：`x/article/view?id=<cv>` 返回的
`data.opus.content.paragraphs[]` 里 `para_type == 2` 的段落 → `pic.pics[]`，
每个元素带 `url / width / height / size(KB)`。一篇图鉴专栏能产出 **6~12 张**。

**两个必须处理的现实问题**：

1. **原图巨大**：Pixiv 向的原图 15~17MB（3000x6000）。B站图床支持缩放后缀
   —— `@1200w.jpg` 实测把 15MB 压到 **220KB**，且 `?x-oss-process=` 那种写法**无效**（被忽略）。
   所以统一请求 `@<PIC_POOL_MAX_WIDTH>w.jpg`。
2. **-509 风控很凶**：连续 2~3 次就 `code=-509 请求过于频繁`。所以本模块
   **串行 + 指数退避重试**，绝不并发；抓过的 cv 号由 `PicPool` 永久记账，不重复抓。

**过滤**：只收"大图"（像素 ≥ `PIC_POOL_MIN_PIXELS`，默认 40 万），
自动跳过 1320x130 这类横幅/分割线/二维码。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import Config
from app.news.sources import BROWSER_UA

logger = logging.getLogger(__name__)

ARTICLE_VIEW_URL = "https://api.bilibili.com/x/article/view"

# 正文 HTML 里的图片（老格式专栏的兜底路径）
_IMG_TAG = re.compile(r"""<img[^>]+?(?:data-src|src)=["']([^"']+)["']""", re.I)
# 正文文本里的画师信息（"画师：Leon"）——纯美图虽不署名，出处仍可追溯
_ARTIST = re.compile(r"画师[：:]\s*([^\n#]{1,40})")

# 【广告图过滤】2026-10-19 用户反馈：萌村百科最后一图是"扫码关注公众号"的广告图，
# 尺寸 1200x620 / 74 万像素，**能过 40 万像素阈值**，所以必须靠内容判定。
# 规则：图片**紧邻的文字**里出现这些词 → 判为广告图丢掉（作品图旁边只有"画师/作品id"）。
AD_KEYWORDS = (
    "二维码", "扫码", "扫一扫", "长按识别", "识别二维码",
    "公众号", "关注", "加群", "群号", "微信", "vx", "威信", "QQ群",
    "投稿", "商务", "合作", "推广", "广告", "赞助", "淘宝", "店铺", "购买",
)


def _is_ad_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(k.lower() in t for k in AD_KEYWORDS)


def _para_text(para: dict) -> str:
    """段落文本（结构：text.nodes[].word.words）"""
    try:
        nodes = ((para.get("text") or {}).get("nodes")) or []
        return "".join((n.get("word") or {}).get("words") or "" for n in nodes)
    except Exception:
        return ""

ERR_FREQ = -509          # 请求过于频繁
ERR_RISK = -352          # 风控校验失败（wbi/机器人检测）—— 与 -509 一样会随时间缓解
# 这两类错误都是"过一会儿可能就好"，必须**退避重试**；其它错误码直接放弃。
_RETRYABLE = {ERR_FREQ, ERR_RISK}
_ERR_NAME = {ERR_FREQ: "-509 请求过于频繁", ERR_RISK: "-352 风控校验"}


@dataclass
class ArticleImage:
    """正文里的一张图（`url` 已带缩放后缀）"""

    url: str
    width: int = 0
    height: int = 0
    size_kb: float = 0.0
    artist: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


def resized_url(url: str, width: int) -> str:
    """把 B站图床 URL 改写成缩放版本：`xxx.png` → `xxx.png@1200w.jpg`

    - 已是 `@...` 后缀的不重复加；
    - `http://` 统一升到 `https://`（图床两者都支持，https 更稳）。
    """
    url = (url or "").strip()
    if not url:
        return ""
    # 协议相对（`//host/x.jpg`）与 http 都统一升到 https —— 图床两者都支持，https 更稳。
    # ⚠️ 协议相对这一支是测试逼出来的：老格式专栏的 <img src="//..."> 会因为没有
    #    显式 scheme 被当成非法 URL 直接丢掉（静默丢图，很难发现）。
    if url.startswith("//"):
        url = "https://" + url[2:]
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://"):
        return ""
    if "@" in url.rsplit("/", 1)[-1]:
        return url
    width = max(200, int(width or 1200))
    return f"{url}@{width}w.jpg"


def parse_article_images(payload: dict, *, max_width: int = 0,
                         min_pixels: int = 0) -> list[ArticleImage]:
    """解析 `x/article/view` 返回 → 正文图列表（纯函数，可离线测试）

    优先走新格式 `data.opus.content.paragraphs`；没有再兜底解析 `data.content` 的 `<img>`。
    """
    max_width = max_width or Config.PIC_POOL_MAX_WIDTH
    min_pixels = min_pixels or Config.PIC_POOL_MIN_PIXELS
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    data = payload.get("data") or {}
    opus = data.get("opus") or {}
    paragraphs = ((opus.get("content") or {}).get("paragraphs")) or []

    def _nearest_text(idx: int, step: int, window: int = 3) -> str:
        """向 idx 的前/后找最近的非图片段落文本（最多看 window 段）"""
        j, seen_n = idx + step, 0
        while 0 <= j < len(paragraphs) and seen_n < window:
            p = paragraphs[j]
            if isinstance(p, dict) and p.get("para_type") != 2:
                t = _para_text(p)
                if t.strip():
                    return t
            j += step
            seen_n += 1
        return ""

    def _nearest_artist(idx: int, window: int = 6) -> str:
        """向 idx 之前找最近的「画师：X」（每张作品图上方都有署名）"""
        j, seen_n = idx - 1, 0
        while 0 <= j < len(paragraphs) and seen_n < window:
            p = paragraphs[j]
            if isinstance(p, dict) and p.get("para_type") != 2:
                m = _ARTIST.search(_para_text(p))
                if m:
                    return m.group(1).strip()
            j -= 1
            seen_n += 1
        return ""

    found: list[ArticleImage] = []
    for idx, para in enumerate(paragraphs):
        if not isinstance(para, dict) or para.get("para_type") != 2:
            continue
        # 【广告图过滤】紧邻的前后文字里出现"公众号/二维码/扫码…" → 这张是广告图
        # （萌村百科每篇最后一张就是"扫码关注公众号"，1200x620 / 74 万像素能过像素阈值）
        prev_text = _nearest_text(idx, -1)
        next_text = _nearest_text(idx, +1)
        if _is_ad_text(prev_text) or _is_ad_text(next_text):
            logger.info(f"[PicPool] 跳过广告图（相邻文字：{(prev_text or next_text)[:40]!r}）")
            continue
        artist = _nearest_artist(idx)
        for pic in ((para.get("pic") or {}).get("pics") or []):
            if not isinstance(pic, dict):
                continue
            url = resized_url(pic.get("url") or "", max_width)
            if not url:
                continue
            found.append(ArticleImage(
                url=url,
                width=int(pic.get("width") or 0),
                height=int(pic.get("height") or 0),
                size_kb=float(pic.get("size") or 0),
                artist=artist,
            ))

    # 兜底：老格式正文 HTML
    if not found:
        for raw in _IMG_TAG.findall(str(data.get("content") or "")):
            url = resized_url(raw, max_width)
            if url:
                found.append(ArticleImage(url=url))

    # 去重（同一张图在多个段落里出现的情况）
    seen: set[str] = set()
    uniq: list[ArticleImage] = []
    for img in found:
        if img.url in seen:
            continue
        seen.add(img.url)
        uniq.append(img)

    # 只留大图：有尺寸的按像素过滤；没尺寸的（老格式）放行，交给下载时的体积上限兜底
    big = [i for i in uniq if not i.pixels or i.pixels >= min_pixels]
    dropped = len(uniq) - len(big)
    if dropped:
        logger.info(f"[PicPool] 跳过 {dropped} 张小图/横幅（< {min_pixels} 像素）")
    return big


def parse_artist(payload: dict) -> str:
    """从正文文本里抠画师名（拿不到就空串）"""
    try:
        content = str(((payload.get("data") or {}).get("content")) or "")
    except Exception:
        return ""
    m = _ARTIST.search(content)
    return m.group(1).strip() if m else ""


async def fetch_article_images(
    cid: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    max_retries: int = 3,
    retry_wait: float = 10.0,
    max_width: int = 0,
    min_pixels: int = 0,
) -> tuple[list[ArticleImage], dict]:
    """拉一篇专栏的正文图 → (图片列表, 元信息{title, artist, ok})

    **-509 时指数退避重试**（10s → 20s → 30s → 40s），其它错误直接放弃。
    返回的 `ok=False` 表示"接口层失败"（调用方**不要**把它记成"已抓过"）。
    """
    cid = str(cid or "").strip().lstrip("cv")
    if not cid.isdigit():
        return [], {"ok": False, "error": f"非法 cv 号: {cid!r}"}
    url = f"{ARTICLE_VIEW_URL}?id={cid}"
    headers = {"User-Agent": BROWSER_UA, "Referer": f"https://www.bilibili.com/read/cv{cid}"}

    own = client is None
    client = client or httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT, follow_redirects=True)
    try:
        wait = retry_wait
        last_code = None
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, headers=headers)
            except httpx.TimeoutException:
                logger.warning(f"[PicPool] cv{cid} 请求超时（第 {attempt + 1} 次）")
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
                continue
            except Exception as e:
                logger.warning(f"[PicPool] cv{cid} 请求失败: {e}")
                return [], {"ok": False, "error": str(e)}

            try:
                payload = resp.json()
            except Exception:
                logger.warning(f"[PicPool] cv{cid} 返回非 JSON（HTTP {resp.status_code}）")
                return [], {"ok": False, "error": f"HTTP {resp.status_code} 非 JSON"}

            code = payload.get("code")
            if code == 0:
                imgs = parse_article_images(payload, max_width=max_width, min_pixels=min_pixels)
                title = str((payload.get("data") or {}).get("title") or "")
                artist = parse_artist(payload)
                logger.info(f"[PicPool] cv{cid} 正文图 {len(imgs)} 张"
                            f"（{title[:24]!r} 画师={artist or '未标注'}）")
                return imgs, {"ok": True, "title": title, "artist": artist}
            if code in _RETRYABLE:
                last_code = code
                logger.warning(f"[PicPool] cv{cid} {_ERR_NAME.get(code, code)} → {wait:.0f}s 后重试"
                               f"（{attempt + 1}/{max_retries}）")
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
                continue
            logger.warning(f"[PicPool] cv{cid} 接口返回 code={code} {payload.get('message')}")
            return [], {"ok": False, "error": f"code={code}"}
        logger.warning(f"[PicPool] cv{cid} 重试 {max_retries} 次仍被限流/风控，本轮放弃")
        return [], {"ok": False, "error": f"限流/风控（最后 code={last_code}）"}
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass
