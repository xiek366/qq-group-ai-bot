"""
资讯推送任务（P2c-3）
====================

`cron` 触发（默认 11:30，**刻意避开 12:30 的每日一句**）；
内容 = 一条**还没推过**的资讯 → 让 LLM 写一句点评 → 组装成
`标题 + 一句话点评 + 原文链接 + 来源`（D19/A.6：**绝不搬运正文**）。

降级链（任一环断了就安静跳过，绝不硬发）：
  1. 源全挂 / 没有新条目 → `TaskSkip`；
  2. LLM 点评失败 → 用标题 + 链接兜底（**不编内容**）。

推过的条目会记指纹（`NewsService.mark_pushed`），所以**连续几天不会推同一条**。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.news import NewsItem, NewsService
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


class NewsDigestTask(Task):
    """午间一条资讯（标题 + 一句话点评 + 原文链接）"""

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
                 commenter: Optional[Callable[[NewsItem], Awaitable[str]]] = None):
        super().__init__()
        self.service = service
        self._commenter = commenter
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

        item = self.service.pick_for_digest()
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

        self.service.mark_pushed(item)
        return [format_news(item, comment)]
