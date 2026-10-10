"""
每日一图任务（P2c-5）
====================

`cron` 触发（默认 19:30）。Q3/ROADMAP 的形态：**检索当天资讯的配图**，而不是生成图片 ——
天然游戏/动漫向、零额外 API 成本。

流程与降级（ROADMAP §5/A.6 的硬要求）：

```
挑一条"还没推过、带封面图、足够新"的资讯
  → 下载图片到本地（体积上限 / 必须 image/* / 超时 → 放弃）
  → 发 [CQ:image] + 配文（标题 + 来源 + 原文链接）   ← D19：必带来源
  → 标记该资讯已推（跨天去重）
```

**任何一个环节失败就跳过本次**（`TaskSkip`）：取不到图**绝不发纯文字充数**，也不发占位图。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.news import NewsItem, NewsService, download_image, to_cq_image
from app.news.image import origin_of
from app.schedule.task import PRIORITY_DAILY, Task, TaskContext, TaskSkip

logger = logging.getLogger(__name__)


class DailyImageTask(Task):
    """每日一图：取当天资讯的配图 + 来源链接"""

    name = "daily_image"
    label = "每日一图"
    priority = PRIORITY_DAILY
    is_proactive = True
    trigger_spec = {"type": "cron", "at": "19:30"}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 0,
        "daily_limit": 1,
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, service: NewsService,
                 fetcher: Optional[Callable[[str], Awaitable[Optional[Path]]]] = None,
                 captioned: Optional[Callable[[NewsItem], Awaitable[str]]] = None):
        """`fetcher` 可注入桩（测试不联网）；**给 None 时走内置下载器**（它才会带正确的 Referer 防盗链）"""
        super().__init__()
        self.service = service
        self._fetcher = fetcher
        self._captioned = captioned
        self.trigger_spec = {"type": "cron", "at": Config.DAILY_IMAGE_AT}

    async def render(self, ctx: TaskContext) -> list:
        if not Config.NEWS_ENABLED:
            raise TaskSkip("资讯功能已关闭")
        try:
            await self.service.refresh()
        except Exception as e:
            logger.warning(f"[News] 刷新失败（继续用缓存）: {e}")

        item = self.service.pick_image_item()
        if item is None:
            # 带上诊断数字：一眼能看出是"源里没图"还是"都推过了"还是"都太旧了"
            detail = ""
            try:
                detail = self.service.diagnose()
            except Exception:
                pass
            raise TaskSkip("没有带图的新资讯可发（取不到就跳过，不发占位图）" + (f"｜{detail}" if detail else ""))

        try:
            if self._fetcher is not None:
                path = await self._fetcher(item.image)
            else:
                # 防盗链：优先用**文章页所在站点**当 Referer（图床一般校验自家站点），
                # 拿不到再用图片自己的站点（download_image 内部还会在 403 时去掉 Referer 重试）
                ref = origin_of(item.link) or origin_of(item.image)
                path = await download_image(item.image, referer=ref)
        except Exception as e:
            logger.warning(f"[News] 配图下载异常: {e}")
            path = None
        if not path:
            raise TaskSkip(f"配图下载失败 → 跳过（来源 {item.source}）")

        caption = ""
        if self._captioned is not None:
            try:
                caption = (await self._captioned(item) or "").strip()
            except Exception as e:
                logger.warning(f"[News] 配文生成失败（用标题兜底）: {e}")

        # 图片 + 配文（图片在前，QQ 里显示更自然）
        lines = [to_cq_image(Path(path))]
        head = f"🖼 {item.title}"
        if caption:
            head += f"\n{caption}"
        lines.append(head)
        lines.append(f"来源：{item.source}" + (f"\n{item.link}" if item.link else ""))
        self.service.mark_pushed(item)
        return ["\n".join(lines)]
