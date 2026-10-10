"""
每日一句任务（P2c-4）
====================

`cron` 触发（默认 12:30）。Q2 的答复：**台词池为主 + LLM 偶尔原创**。

- 池子在 `app/context/topic_pool.py` 的 `PRESET_QUOTES`（游戏/动漫/业界，短句级，版权安全）；
- 轮播复用 `TopicPool`（按使用次数选最少用过的，并避开本群最近用过的）；
- **LLM 原创**：每 `QUOTE_LLM_EVERY_DAYS` 天允许一次（用"年内第几天 % N"决定，可预测、可测）；
  LLM 失败或 `N=0` → 一律用池子，所以这个任务**永远有内容可发、不依赖 LLM**。

状态落 `data/cache/quotes.json`（与话题库同结构、不同文件，互不干扰）。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.context.topic_pool import PRESET_QUOTES, TopicPool
from app.schedule.task import PRIORITY_DAILY, Task, TaskContext, TaskSkip
from app.util import clock

logger = logging.getLogger(__name__)


def should_generate_today(now=None) -> bool:
    """今天是否轮到"LLM 原创"（每 N 天一次；N<=0 表示完全不用 LLM）"""
    n = int(Config.QUOTE_LLM_EVERY_DAYS or 0)
    if n <= 0:
        return False
    day = clock.from_timestamp(now) if now else clock.now()
    return day.timetuple().tm_yday % n == 0


class DailyQuoteTask(Task):
    """每天一句游戏/动漫向的话（池子轮播，偶尔 LLM 原创）"""

    name = "daily_quote"
    label = "每日一句"
    priority = PRIORITY_DAILY
    is_proactive = True
    trigger_spec = {"type": "cron", "at": "12:30"}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 0,
        "daily_limit": 1,
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, pool: Optional[TopicPool] = None,
                 generator: Optional[Callable[[], Awaitable[str]]] = None):
        """`generator` 由 handler 注入（它有 LLM）；失败一律回落池子"""
        super().__init__()
        self.pool = pool or TopicPool(path=None, topics=list(PRESET_QUOTES))
        self._generator = generator
        self.trigger_spec = {"type": "cron", "at": Config.DAILY_QUOTE_AT}

    async def render(self, ctx: TaskContext) -> list:
        if should_generate_today() and self._generator is not None:
            try:
                text = (await self._generator() or "").strip()
                if text:
                    return [f"💬 {text}"]
            except Exception as e:
                logger.warning(f"[Quote] LLM 原创失败（回落池子）: {e}")

        topic = self.pool.pick(ctx.group_id)
        if not topic:
            raise TaskSkip("台词池是空的")
        self.pool.mark_used(ctx.group_id, topic["id"])
        return [f"💬 {topic['text']}"]
