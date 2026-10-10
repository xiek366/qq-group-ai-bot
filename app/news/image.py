"""
配图下载（P2c-5）
================

每日一图要把资讯的封面图**下载到本地再发**：QQ 侧发外链图片经常被拦/加载不出来，
本地文件用 `[CQ:image,file=file:///...]` 最稳（群表情包早就是这么发的）。

三条硬约束：

1. **体积上限**（`NEWS_IMAGE_MAX_MB`）：超了直接放弃，不要把几百 MB 的图往服务器上拉；
2. **类型校验**：`content-type` 必须是 `image/*`，否则当失败（避免下回一个 HTML 错误页）；
3. **失败返回 None**，由调用方跳过本次（取不到图就不发，绝不发占位图）。

落盘目录：`data/news_images/`（文件名带 URL 哈希，同图不会重复下载）。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.paths import NEWS_IMAGE_DIR, ensure_dir
from app.news.sources import BROWSER_UA

logger = logging.getLogger(__name__)

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


def _ext_for(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    path_ext = Path(urlparse(url).path).suffix.lower()
    return path_ext if path_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else ".jpg"


def origin_of(url: str) -> str:
    """取 URL 的站点根：`https://image.gcores.com/a.jpg` → `https://image.gcores.com/`"""
    try:
        p = urlparse(url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return ""


async def download_image(url: str, *, dest_dir: Optional[Path] = None,
                         max_mb: float = 0.0, client: Optional[httpx.AsyncClient] = None,
                         referer: str = "") -> Optional[Path]:
    """下载图片到本地；失败/超限/非图片 → None（调用方跳过本次）

    **防盗链处理**（2026-10-08 线上踩到）：原来无条件发 `Referer: https://www.bilibili.com/`，
    结果抓机核图床（image.gcores.com）时被 403 —— 外站 Referer 会触发防盗链。现在：
      1. 默认 Referer 取**图片自己所在站点**的根（`referer` 参数可覆盖，比如文章页的站点）；
      2. 如果仍然 401/403，**去掉 Referer 再试一次**（不少 CDN 只要不是外站 referer 就放行）。
    """
    if not url or not url.startswith("http"):
        return None
    dest = ensure_dir(dest_dir or NEWS_IMAGE_DIR)
    limit = int((max_mb or Config.NEWS_IMAGE_MAX_MB) * 1024 * 1024)

    own = client is None
    client = client or httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT, follow_redirects=True)

    async def _get(ref: str):
        headers = {"User-Agent": BROWSER_UA}
        if ref:
            headers["Referer"] = ref
        return await client.get(url, headers=headers)

    try:
        ref = referer or origin_of(url)
        resp = await _get(ref)
        if resp.status_code in (401, 403):
            logger.info(f"[News] 图片 {resp.status_code}（Referer={ref or '无'}）→ 去掉 Referer 重试一次")
            resp = await _get("")
        if resp.status_code != 200:
            logger.warning(f"[News] 图片 HTTP {resp.status_code}: {url[:80]}")
            return None
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.lower().startswith("image/"):
            logger.warning(f"[News] 不是图片（{ctype!r}），放弃: {url[:80]}")
            return None
        data = resp.content
        if not data:
            return None
        if limit and len(data) > limit:
            logger.warning(f"[News] 图片过大（{len(data) / 1048576:.1f}MB > {max_mb or Config.NEWS_IMAGE_MAX_MB}MB），放弃")
            return None

        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + _ext_for(url, ctype)
        path = dest / name
        if not path.exists():
            path.write_bytes(data)
        logger.info(f"[News] 配图已就绪: {path.name}（{len(data) / 1024:.0f}KB，来自 {urlparse(url).netloc}）")
        return path
    except httpx.TimeoutException:
        logger.warning(f"[News] 图片下载超时: {url[:80]}")
        return None
    except Exception as e:
        logger.warning(f"[News] 图片下载失败: {e}")
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass


def to_cq_image(path: Path) -> str:
    """本地文件 → CQ 图片码（与表情包发送方式保持一致）"""
    return f"[CQ:image,file=file:///{str(path).replace(chr(92), '/')}]"
