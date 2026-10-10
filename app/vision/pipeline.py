"""
视觉管道（P0-3）
================

**它解决什么**：老实现把 NapCat 给的图片 URL 直接丢给模型就完事了。问题一串：

  - URL 可能是**局域网地址**或带临时签名，模型服务器抓不到 → 返回空描述 → bot 只能瞎聊；
  - 一次最多 6 张图，**一张有问题整次请求受影响**，没有逐张隔离；
  - 同一张图反复发就反复请求，重复计费、重复失败；
  - 失败后没有任何降级路径，用户看到的是"没看清"或者更糟——模型开始编。

现在的流程：

    图片 URL
      → 下载（超时 + 大小上限，逐张隔离，坏的跳过）
      → 魔数校验（JPEG/PNG/GIF/WebP；不看文件名，看真实字节）
      → 算 MD5 → 查缓存（命中就直接用，不再请求）
      → 能内联的就转成 base64 data URL（消除"模型抓不到 URL"这一整类问题）
        太大的单图 / 总量超限 → 退回用原始 URL
      → 调模型（思考模式已关、预算已放宽，见 llm/provider.py）
      → 成功则写缓存；失败则返回空描述 + 原因，由 handler 走"没看清"话术
      → 描述由 handler 写进对话流，之后可以引用"刚才那张图"

设计取舍（对应 DEVLOG §P0-0 Q1）：
  - **不引入 Pillow**：不做压缩/转码，超限的图直接退回 URL；
  - 不解析像素尺寸（极端的超 8192px 图会让 API 自己报错，届时看日志再定）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.vision.cache import VisionCache

logger = logging.getLogger(__name__)

# 魔数 → MIME（按真实字节判断格式，不信文件名）
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),      # RIFF....WEBP
]

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 32 * 1024 * 1024      # 官方单图上限 32 MiB

DEFAULT_PROMPT = "请用 2~3 句话描述这张图片的内容。如果图片里有文字请也识别出来。"


@dataclass
class VisionResult:
    """一次看图的结果（供 handler 判断与日志）"""

    description: str = ""
    ok: bool = False
    cached: bool = False
    reason: str = ""                     # 失败原因：no_image/all_failed/empty_response/llm_missing...
    images_total: int = 0
    images_used: int = 0
    inlined: int = 0
    skipped: list[str] = field(default_factory=list)   # 被跳过的图片原因

    def __bool__(self) -> bool:
        return self.ok


def sniff_mime(data: bytes) -> str:
    """按魔数判断图片格式；不是支持的图片就返回空串"""
    if not data:
        return ""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return ""


def _record_vision(*, cached: bool = False, ok: bool = False, inlined: int = 0) -> None:
    """【P0-4】记看图指标（事件循环里调用 → metrics 只改内存）"""
    try:
        from app import metrics
        metrics.get().record_vision(cached=cached, ok=ok, inlined=inlined)
    except Exception:      # 指标不该影响主流程
        pass


async def _default_fetcher(url: str) -> Optional[bytes]:
    """默认下载器：httpx，带超时。失败返回 None（逐张隔离，不抛给上层）"""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=Config.VISION_DOWNLOAD_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"[Vision] 下载失败 HTTP {resp.status_code}: …{url[-40:]}")
                return None
            return resp.content
    except Exception as e:
        logger.warning(f"[Vision] 下载异常 {type(e).__name__}: …{url[-40:]}")
        return None


class VisionPipeline:
    def __init__(
        self,
        cache: Optional[VisionCache] = None,
        fetcher: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
    ):
        self.cache = cache or VisionCache()
        self._fetch = fetcher or _default_fetcher
        # 指标（P0-4 的 metrics 会从这里取；这里先留内部计数，避免强依赖）
        self.stats = {"calls": 0, "cache_hits": 0, "ok": 0, "failed": 0, "inlined": 0}

    # ---------- 主入口 ----------

    async def describe(
        self,
        image_urls: list[str],
        prompt: str = DEFAULT_PROMPT,
        llm=None,
    ) -> VisionResult:
        urls = [u for u in dict.fromkeys(image_urls or []) if u][:MAX_IMAGES]
        result = VisionResult(images_total=len(urls))
        if not urls:
            result.reason = "no_image"
            return result
        if llm is None:
            result.reason = "llm_missing"
            return result

        self.stats["calls"] += 1

        # 1) 逐张下载（并发，互不影响）
        import asyncio

        fetched = await asyncio.gather(*[self._fetch(u) for u in urls], return_exceptions=True)
        good: list[tuple[str, bytes, str]] = []      # (原始 url, 字节, mime)
        for url, data in zip(urls, fetched):
            if isinstance(data, Exception) or not data:
                result.skipped.append("download_failed")
                continue
            if len(data) > MAX_IMAGE_BYTES:
                result.skipped.append("too_large")
                continue
            mime = sniff_mime(data)
            if not mime:
                result.skipped.append("bad_format")
                continue
            good.append((url, data, mime))

        if not good:
            result.reason = "all_failed"
            self.stats["failed"] += 1
            _record_vision(ok=False)
            logger.warning(f"[Vision] 没有可用图片（共 {len(urls)} 张，跳过原因 {result.skipped}）")
            return result

        md5s = [hashlib.md5(d).hexdigest() for _, d, _ in good]
        result.images_used = len(good)

        # 2) 查缓存（命中就完全不请求模型）
        key = VisionCache.key_for(md5s, prompt)
        cached = self.cache.get(key)
        if cached:
            result.description = cached
            result.ok = True
            result.cached = True
            self.stats["cache_hits"] += 1
            self.stats["ok"] += 1
            _record_vision(cached=True, ok=True)
            logger.info(f"[Vision] 缓存命中（{len(good)} 张图）→ {cached[:40]}…")
            return result

        # 3) 组装入参：能内联就内联，超限退回 URL
        inline_max = int(Config.VISION_INLINE_MAX_MB * 1024 * 1024)
        total_max = int(Config.VISION_INLINE_TOTAL_MB * 1024 * 1024)
        payload_urls: list[str] = []
        total = 0
        for url, data, mime in good:
            if len(data) <= inline_max and total + len(data) <= total_max:
                b64 = base64.b64encode(data).decode("ascii")
                payload_urls.append(f"data:{mime};base64,{b64}")
                total += len(data)
                result.inlined += 1
            else:
                payload_urls.append(url)      # 降级：让模型自己去下载

        self.stats["inlined"] += result.inlined
        logger.info(
            f"[Vision] 发起识别：{len(good)} 张（内联 {result.inlined}，URL {len(good) - result.inlined}）"
        )

        # 4) 调模型
        start = time.time()
        try:
            description = (await llm.chat_with_images(payload_urls, prompt=prompt) or "").strip()
        except Exception as e:
            logger.error(f"[Vision] 调用失败: {e}")
            result.reason = "llm_error"
            self.stats["failed"] += 1
            _record_vision(ok=False, inlined=result.inlined)
            return result
        elapsed = time.time() - start

        if not description:
            result.reason = "empty_response"
            self.stats["failed"] += 1
            _record_vision(ok=False, inlined=result.inlined)
            logger.warning(f"[Vision] 模型返回空描述（{elapsed:.2f}s，{len(good)} 张图）")
            return result

        # 5) 写缓存
        self.cache.put(key, description, md5s)
        result.description = description
        result.ok = True
        self.stats["ok"] += 1
        _record_vision(ok=True, inlined=result.inlined)
        logger.info(f"[Vision] 识别成功（{elapsed:.2f}s，{len(description)} 字）")
        return result
