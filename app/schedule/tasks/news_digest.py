"""
资讯推送任务（P2c-3）
====================

`cron` 触发（默认 11:30，**刻意避开 12:30 的每日一句**）；
内容 = 一条**还没推过**的资讯 → 让 LLM 写一句点评 → 组装成
`标题 + 一句话点评 + 原文链接 + 来源`（D19/A.6：**绝不搬运正文**）。

【P2c-图鉴分拣】图鉴型 UP（NEWS_GALLERY_MIDS）的条目排除出资讯流，专属每日一图；
带图条目（机核/游研社/B站专栏封面）**下载封面随资讯一起发**——
图文一体，不再"文字归资讯、图归每日一图"地割裂。

降级链（任一环断了就安静跳过/降级，绝不硬发）：
  1. 源全挂 / 没有新条目 → `TaskSkip`；
  2. LLM 点评失败 → 用标题 + 链接兜底（**不编内容**）；
  3. 封面下载失败 → 降级为纯文字资讯（资讯价值在文字）。

推过的条目会记指纹（`NewsService.mark_pushed`），所以**连续几天不会推同一条**。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.news import NewsItem, NewsService, download_image, to_cq_image
from app.news.image import origin_of
from app.schedule.task import PRIORITY_DAILY, Task, TaskContext, TaskSkip

logger = logging.getLogger(__name__)


def format_news(item: NewsItem, comment: str = "", with_source: bool = True) -> str:
    """组装资讯文案：标题 + 点评 + 链接 + 来源（正文一律不搬）"""
    lines = [f"📰 {item.title}"]
    comment = (comment or "").strip()
    if comment:
        lines.append(comment if len(comment) <= 120 else comment[:120] + "…")
    if with_source:
        lines.append(f"来源：{item.source}")
    if item.link:
        lines.append(item.link)
    return "\n".join(lines)


def gallery_source_ids(cfg: str) -> set[str]:
    """NEWS_GALLERY_MIDS（逗号分隔 mid）→ {"bili_<mid>", ...}"""
    return {f"bili_{m.strip()}" for m in (cfg or "").replace("，", ",").split(",")
            if m.strip().isdigit()}


class NewsDigestTask(Task):
    """午间一条资讯（封面图 + 标题 + 一句话点评 + 原文链接）"""

    name = "news_digest"
    label = "资讯推送"
    priority = PRIORITY_DAILY
    is_proactive = True
    trigger_spec = {"type": "cron", "at": "11:30"}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 0,
        "daily_limit": 1,
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, service: NewsService,
                 commenter: Optional[Callable[[NewsItem], Awaitable[str]]] = None,
                 fetcher: Optional[Callable[[str], Awaitable]] = None):
        """`fetcher` 可注入桩（测试不联网）；None 时走内置下载器（带防盗链 Referer 策略）"""
        super().__init__()
        self.service = service
        self._commenter = commenter
        self._fetcher = fetcher
        self._gallery_ids = gallery_source_ids(Config.NEWS_GALLERY_MIDS)
        self.trigger_spec = {"type": "cron", "at": Config.NEWS_DIGEST_AT}

    def refine_config(self, cfg: dict, group_config: dict) -> dict:
        return cfg

    async def render(self, ctx: TaskContext) -> list:
        if not Config.NEWS_ENABLED:
            raise TaskSkip("资讯功能已关闭")
        # 先刷新（尊重每源冷却；没到冷却时间就只读缓存）
        try:
            await self.service.refresh()
        except Exception as e:
            logger.warning(f"[News] 刷新失败（继续用缓存）: {e}")

        item = self.service.pick_for_digest(exclude_source_ids=self._gallery_ids)
        if item is None:
            detail = ""
            try:
                detail = self.service.diagnose()
            except Exception:
                pass
            raise TaskSkip("没有可推的新资讯（源挂了 / 都推过了 / 太旧）" + (f"｜{detail}" if detail else ""))

        comment = ""
        if self._commenter is not None:
            try:
                comment = (await self._commenter(item) or "").strip()
            except Exception as e:
                logger.warning(f"[News] 点评生成失败（用标题+链接兜底）: {e}")

        # 【P2c-图鉴分拣】带图条目 → 下载封面随资讯一起发（图在前）；失败降级纯文字
        parts: list[str] = []
        if item.has_image:
            path = None
            try:
                ref = origin_of(item.link) or origin_of(item.image)
                if self._fetcher is not None:
                    path = await self._fetcher(item.image)
                else:
                    path = await download_image(item.image, referer=ref)
            except Exception as e:
                logger.warning(f"[News] 资讯封面下载失败（降级纯文字）: {e}")
                path = None
            if path:
                parts.append(to_cq_image(path))

        parts.append(format_news(item, comment))
        self.service.mark_pushed(item)
        return ["\n".join(parts)]
