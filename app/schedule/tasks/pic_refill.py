"""
凌晨补货任务（P2c-图池）
=======================

`cron` 触发（默认 04:00）。**刻意与 19:30 的「每日一图」分开**：

- B站 `x/article/view` 的 **-509 风控很凶**（连续 2~3 次就限流），必须低频、串行、慢速；
- 补货失败也只是"这次没补上"，**绝不影响当天发图**（池子照用旧的）；
- 放在凌晨还能避开群活跃时段，失败也无人察觉。

一次最多抓 `PIC_POOL_ARTICLES_PER_RUN` 篇（默认 1 篇，产量 6~12 张/篇，够发一周多）。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import Config
from app.news import NewsService
from app.news.picpool import PicPool
from app.schedule.task import PRIORITY_MAINTENANCE, Task, TaskContext, TaskSkip

logger = logging.getLogger(__name__)


class PicRefillTask(Task):
    """从图鉴 UP 的最新专栏里抓正文图入池（不发送任何消息）"""

    name = "pic_refill"
    label = "图池补货"
    priority = PRIORITY_MAINTENANCE
    # 不发送消息：只在日志里留痕（silent_ok）
    is_proactive = False
    silent_ok = True
    # 产出只进日志/面板，**不发到群里**（凌晨 4 点往群里发"补货 +6 张"毫无意义）
    no_send = True
    trigger_spec = {"type": "cron", "at": "04:00"}
    defaults = {
        "enabled": True,
        # 4 小时冷却：**失败也会写 last_run**，所以一晚上最多重试两三次
        # （B站 -509/-352 都是 IP 级，重试太勤没用，还会加重风控）
        "cooldown_minutes": 240,
        "daily_limit": 1,
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, pool: PicPool, service: Optional[NewsService] = None):
        super().__init__()
        self.pool = pool
        self.service = service
        self.trigger_spec = {"type": "cron", "at": Config.PIC_POOL_REFILL_AT}

    async def render(self, ctx: TaskContext) -> list:
        if not Config.PIC_POOL_ENABLED:
            raise TaskSkip("图池功能已关闭")
        if ctx.dry_run:
            # 试跑：只报告"本来会抓哪篇"，不刷新、不请求专栏接口、不写图池、不记账
            articles = self.service.all_items() if self.service is not None else []
            limit = max(1, int(Config.PIC_POOL_ARTICLES_PER_RUN or 1))
            picks = self.pool.candidates(articles)[:limit]
            st = self.pool.stats()
            if not picks:
                return [f"试跑：暂无新专栏可抓；池子 {st['total']} 张 / 剩 {st['remaining']} 张"]
            desc = "、".join(f"cv{cid}《{title}》" for cid, title, _sid in picks)
            return [f"试跑：本应抓取 {desc}（不请求接口、不写图池）；"
                    f"池子 {st['total']} 张 / 剩 {st['remaining']} 张"]
        articles = []
        if self.service is not None:
            try:
                await self.service.refresh()
            except Exception as e:
                logger.warning(f"[PicPool] 补货前刷新资讯失败（用缓存继续）: {e}")
            articles = self.service.all_items()
        res = await self.pool.harvest(articles, limit=Config.PIC_POOL_ARTICLES_PER_RUN)
        if not res.get("ok"):
            # 接口层失败（-509/网络）：池子没动，安静跳过
            raise TaskSkip(f"补货失败（池子保持不变）：{res.get('error')}")
        if not res.get("added"):
            raise TaskSkip(res.get("reason") or "本次没有新增图片")
        st = self.pool.stats()
        logger.info(f"[PicPool] 补货完成：新增 {res['added']} 张，池子 "
                    f"{st['total']} 张 / 已发 {st['used']} / 剩 {st['remaining']} / {st['disk_mb']}MB")
        # 不发送，但把结果写进任务日志便于面板查看
        return [f"图池补货：+{res['added']} 张（池子 {st['total']} 张 / 剩 {st['remaining']}）"]
