"""
喝水 / 久坐提醒任务（P2b-4）
===========================

**合并成一个任务**（ROADMAP §4.6：不要拆两个）：文案池里"喝水"和"久坐"两类交替出现，
用一个 interval 触发器 + 时段窗 + 群活跃前置 + 每日上限控制打扰。

按 D18 的默认值（Q3 用户确认）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `interval` | 75 分钟 | 两次提醒的最小间隔 |
| `window` | 10:00–18:00 | 只在这个时段内提醒（半夜不打扰） |
| `daily_limit` | 4 条 | 一天最多提醒几次 |
| `require_active_within_minutes` | 30 分钟 | **死群不打扰**：群最近没人说话就跳过 |

另外它还要跟"每群每日 ≤8 条主动消息总预算"共享额度（框架层自动处理）。

**文案纯模板、零 LLM 成本**：这类提醒本来就该稳定、可预期；
轮换靠任务状态里的当日执行次数取模 —— 不需要额外存状态。
"""

from __future__ import annotations

import logging

from app.config import Config
from app.schedule.task import PRIORITY_REMINDER, Task, TaskContext
from app.schedule.triggers import parse_hm

logger = logging.getLogger(__name__)

# 文案池：(类别, 文案)。两类交替出现，避免一天四遍都是"喝水"。
WATER_TEXTS = (
    "该喝水啦！这杯下去，HP 回满 💧",
    "提醒一下：水杯里的进度条该涨一涨了～",
    "勇者，补水时间到！别等口渴才想起 🚰",
    "喝口水吧，不然大脑要开始掉帧了",
)
SIT_TEXTS = (
    "坐太久啦，起来走两步，给关节上个润滑油 🦴",
    "起来活动一下吧！久坐会让 HP 缓慢下降的",
    "肩膀脖子是不是僵了？站起来伸个懒腰～",
    "该离开椅子 5 分钟了，顺便看看窗外",
)


def pick_text(count: int) -> str:
    """按当日已发次数轮换：偶数喝水、奇数久坐，各自池内循环"""
    idx = max(0, int(count))
    if idx % 2 == 0:
        return WATER_TEXTS[(idx // 2) % len(WATER_TEXTS)]
    return SIT_TEXTS[(idx // 2) % len(SIT_TEXTS)]


def window_list() -> list:
    """`WATER_REMINDER_WINDOW`（"10:00-18:00"）→ ["10:00", "18:00"]；解析不了返回空（=不限制）"""
    raw = (Config.WATER_REMINDER_WINDOW or "").strip()
    if "-" not in raw:
        return []
    start, end = raw.split("-", 1)
    if not parse_hm(start) or not parse_hm(end):
        logger.warning(f"[Water] WATER_REMINDER_WINDOW 格式不对: {raw!r}（应为 HH:MM-HH:MM），本次不限制时段")
        return []
    return [start.strip(), end.strip()]


class WaterReminderTask(Task):
    """喝水 + 久坐提醒（按 D18：75 分 / 10:00–18:00 / 每日 ≤4 / 群活跃才提醒）"""

    name = "water_reminder"
    label = "喝水久坐提醒"
    priority = PRIORITY_REMINDER
    is_proactive = True
    trigger_spec = {"type": "interval", "minutes": 75}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 0,             # interval 本身就是间隔控制，不再叠冷却
        "daily_limit": 4,
        "min_group_size": 0,
        "require_active_within_minutes": 30,
        "backoff": {"on_no_reply": False},  # 提醒不需要"没人回应就退避"
    }

    def __init__(self):
        super().__init__()
        self.trigger_spec = {"type": "interval", "minutes": Config.WATER_REMINDER_INTERVAL_MINUTES,
                             "window": window_list()}
        self.defaults["enabled"] = Config.WATER_REMINDER_ENABLED
        self.defaults["daily_limit"] = Config.WATER_REMINDER_DAILY_LIMIT
        self.defaults["require_active_within_minutes"] = Config.WATER_REMINDER_ACTIVE_WITHIN

    async def render(self, ctx: TaskContext) -> list:
        # 用"今天已发几条"轮换文案：偶数喝水 / 奇数久坐
        count = int((ctx.state or {}).get("count", 0) or 0)
        return [pick_text(count)]
