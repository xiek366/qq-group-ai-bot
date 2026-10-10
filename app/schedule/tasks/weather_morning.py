"""
早间天气播报任务（P2b-2）
=========================

`cron` 触发（默认 08:00，可按群覆盖）；内容源 = `WeatherService`。

**两条硬规则**（ROADMAP §5.4 / §5.5）：

- **没绑城市的群不播报** —— 宁可没有，也不要发错城市的天气（`TaskSkip` 安静跳过）；
- **取数失败不播报** —— 绝不发占位内容，也不在当天补发（8 点后补早报很怪）。

收尾句（Q1 的答复）：**模板 + 一次 LLM 润色**。润色失败一律回落到模板 ——
天气是每天固定的栏目，稳定比花哨重要。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.schedule.task import PRIORITY_BROADCAST, Task, TaskContext, TaskSkip
from app.weather import WeatherDaily, WeatherService, resolve_city

logger = logging.getLogger(__name__)


class WeatherMorningTask(Task):
    """每天早上的天气播报（天气 + 注意事项 + 游戏化收尾）"""

    name = "weather_morning"
    label = "早间天气"
    priority = PRIORITY_BROADCAST          # 播报类优先于提醒与破冰（ROADMAP §4.8）
    is_proactive = True
    trigger_spec = {"type": "cron", "at": "08:00"}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 0,
        "daily_limit": 1,                  # 一天一条
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "backoff": {"on_no_reply": False},  # 播报是固定栏目，不因为没人回应就退避
    }

    def __init__(self, service: WeatherService,
                 polish: Optional[Callable[[str, WeatherDaily], Awaitable[str]]] = None):
        """`polish` 由 handler 注入（它有 LLM）：输入事实文本，返回润色后的收尾句；失败返回空串"""
        super().__init__()
        self.service = service
        self._polish = polish

    def refine_config(self, cfg: dict, group_config: dict) -> dict:
        """群配置里的 `weather.broadcast_at` → 触发器的 `at`（每个群可以播报时间不同）"""
        at = str(((group_config or {}).get("weather") or {}).get("broadcast_at") or "").strip()
        if at:
            trig = dict(cfg.get("trigger") or {})
            trig["type"] = trig.get("type") or "cron"
            trig["at"] = at
            cfg["trigger"] = trig
        return cfg

    def city_for(self, ctx: TaskContext) -> str:
        """本群绑定的城市（群配置 > 环境变量默认）；未绑定返回空串"""
        weather_cfg = (ctx.group_config or {}).get("weather") or {}
        return resolve_city(weather_cfg.get("city") or Config.WEATHER_CITY, default="")

    async def render(self, ctx: TaskContext) -> list:
        if not Config.WEATHER_ENABLED:
            raise TaskSkip("天气功能已关闭")
        city = self.city_for(ctx)
        if not city:
            # 未绑城市 → 安静跳过（不播报总比发错城市好）
            raise TaskSkip("本群未绑定城市（group_config 的 weather.city）")

        data = await self.service.fetch_daily(city)
        if data is None:
            raise TaskSkip(f"取不到 {city} 的天气（API 失败/超时）→ 本次不播报")

        finish = ""
        if Config.WEATHER_POLISH and self._polish is not None:
            try:
                finish = (await self._polish(city, data) or "").strip()
            except Exception as e:      # 润色失败不影响播报
                logger.warning(f"[Weather] 收尾句润色失败（回落模板）: {e}")
        return [self.service.render_broadcast(city, data, finish=finish)]
