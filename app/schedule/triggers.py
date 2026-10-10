"""
触发类型（P2a-2）
=================

| type | 语义 | 用途 |
|---|---|---|
| `idle` | 群静默 N 分钟后 | 破冰 |
| `interval` | 距上次执行 ≥ N 分钟（可叠加时段窗） | 喝水/久坐提醒 |
| `cron` | 固定时刻（可指定星期） | 8 点播报、每日一句/图、周报 |
| `event` | 事件驱动（P2a 只留接口，P2d 的欢迎用它） | 新人欢迎 |

所有时间判断都走 `app.util.clock`（= `Config.TIMEZONE`），**不看服务器 TZ** —— 云端 UTC 机器
上直接用 `datetime.now()` 会让"早上 8 点播报"变成下午 4 点。

迟到处理：cron 任务如果错过了触发时刻（服务重启、预算拦下、静默时段），
`CRON_CATCH_UP_MINUTES`（默认 90 分钟）内还允许补发，超过就跳过 —— 免得半夜突然收到早报。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.config import Config
from app.util import clock

logger = logging.getLogger(__name__)


def parse_hm(text: str) -> Optional[tuple[int, int]]:
    """'08:00' → (8, 0)；非法返回 None"""
    try:
        hh, mm = str(text).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def in_quiet_hours(now_ts: Optional[float] = None, spec: str = "") -> bool:
    """是否处于静默时段，支持跨午夜（如 "23:00-08:00"）

    spec 为空时用 `Config.QUIET_HOURS`；解析不出来就当"不静默"（宁可多发，也不要因为配置写错全天静默）。
    """
    spec = (spec or Config.QUIET_HOURS or "").strip()
    if not spec or "-" not in spec:
        return False
    start_s, end_s = spec.split("-", 1)
    start, end = parse_hm(start_s), parse_hm(end_s)
    if not start or not end:
        logger.warning(f"[Schedule] QUIET_HOURS 格式不对（应为 HH:MM-HH:MM）: {spec!r}，已忽略")
        return False

    dt = clock.from_timestamp(now_ts if now_ts is not None else time.time())
    minutes = dt.hour * 60 + dt.minute
    s, e = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if s == e:
        return False                      # 起止相同 = 不静默
    if s < e:
        return s <= minutes < e
    return minutes >= s or minutes < e    # 跨午夜


def in_window(now_ts: Optional[float], window: Optional[list]) -> bool:
    """是否处于 [start, end] 时段窗内（如 ["10:00","18:00"]）；为空表示不限制"""
    if not window or len(window) != 2:
        return True
    start, end = parse_hm(window[0]), parse_hm(window[1])
    if not start or not end:
        return True
    dt = clock.from_timestamp(now_ts if now_ts is not None else time.time())
    minutes = dt.hour * 60 + dt.minute
    s, e = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if s <= e:
        return s <= minutes <= e
    return minutes >= s or minutes <= e   # 跨午夜时段窗


@dataclass
class Trigger:
    """触发基类：只回答"现在到点了吗"，不关心内容与发送"""

    type: str = ""

    def due(self, *, now: float, last_run: float, idle_minutes: float, task_state: dict) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        return self.type

    def next_eta(self, *, now: float, last_run: float, idle_minutes: float) -> Optional[str]:
        """人类可读的"大概什么时候会触发"（面板用；不保证精确）"""
        return None


@dataclass
class IdleTrigger(Trigger):
    """群静默 N 分钟后触发（破冰用的就是它）"""

    minutes: int = 30
    type: str = "idle"

    def due(self, *, now, last_run, idle_minutes, task_state) -> bool:
        return idle_minutes >= self.minutes

    def describe(self) -> str:
        return f"静默 {self.minutes} 分钟"

    def next_eta(self, *, now, last_run, idle_minutes) -> Optional[str]:
        remain = max(0, self.minutes - idle_minutes)
        return f"约 {remain:.0f} 分钟后（若继续保持安静）"


@dataclass
class IntervalTrigger(Trigger):
    """距上次执行 ≥ N 分钟（可叠加时段窗）"""

    minutes: int = 75
    window: Optional[list] = None
    type: str = "interval"

    def due(self, *, now, last_run, idle_minutes, task_state) -> bool:
        if not in_window(now, self.window):
            return False
        if not last_run:
            return True                   # 从没跑过（今天首次）→ 可以跑
        return (now - last_run) >= self.minutes * 60

    def describe(self) -> str:
        w = f"，时段 {self.window[0]}–{self.window[1]}" if self.window else ""
        return f"每 {self.minutes} 分钟{w}"

    def next_eta(self, *, now, last_run, idle_minutes) -> Optional[str]:
        if not last_run:
            return "随时（今天还没跑过）"
        remain = max(0, self.minutes * 60 - (now - last_run)) / 60
        return f"约 {remain:.0f} 分钟后"


@dataclass
class CronTrigger(Trigger):
    """固定时刻触发（可指定星期，1=周一 … 7=周日，与 ROADMAP 一致）"""

    at: str = "08:00"
    weekday: Optional[int] = None
    type: str = "cron"

    def _target_today(self, now: float) -> Optional[datetime]:
        hm = parse_hm(self.at)
        if not hm:
            return None
        dt = clock.from_timestamp(now)
        return dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)

    def due(self, *, now, last_run, idle_minutes, task_state) -> bool:
        """判定顺序：星期对 → 今天到点了 → 今天还没跑过 → 迟到不超过容忍值"""
        dt = clock.from_timestamp(now)
        if self.weekday and dt.isoweekday() != int(self.weekday):
            return False
        target = self._target_today(now)
        if target is None:
            return False
        if dt < target:
            return False                                  # 还没到点
        late_minutes = (dt - target).total_seconds() / 60
        if late_minutes > Config.CRON_CATCH_UP_MINUTES:
            return False                                  # 迟到太久 → 不补（避免半夜补早报）
        # 今天已经跑过就不再跑（跨天由 state 的 day 字段保证）
        if last_run:
            last_dt = clock.from_timestamp(last_run)
            if last_dt.date() == dt.date() and last_dt >= target:
                return False
        return True

    def describe(self) -> str:
        wd = f"周{self.weekday} " if self.weekday else ""
        return f"{wd}{self.at}"

    def next_eta(self, *, now, last_run, idle_minutes) -> Optional[str]:
        target = self._target_today(now)
        dt = clock.from_timestamp(now)
        if target is None:
            return None
        if self.weekday and dt.isoweekday() != int(self.weekday):
            days = (int(self.weekday) - dt.isoweekday()) % 7 or 7
            return f"{days} 天后的 {self.at}"
        if dt < target:
            return f"今天 {self.at}"
        return f"明天 {self.at}"


@dataclass
class EventTrigger(Trigger):
    """事件驱动（P2a 只留接口：P2d 的 welcome 会真正用它）"""

    event: str = ""
    type: str = "event"

    def due(self, *, now, last_run, idle_minutes, task_state) -> bool:
        return False                      # 事件型任务由事件层直接调用 Task.run

    def describe(self) -> str:
        return f"事件 {self.event}"


def build_trigger(spec: dict) -> Optional[Trigger]:
    """从配置 dict 造触发器（配置里的 trigger 段）"""
    if not isinstance(spec, dict):
        return None
    t = (spec.get("type") or "").lower()
    if t == "idle":
        return IdleTrigger(minutes=int(spec.get("minutes", 30)))
    if t == "interval":
        return IntervalTrigger(minutes=int(spec.get("minutes", 60)), window=spec.get("window"))
    if t == "cron":
        return CronTrigger(at=str(spec.get("at", "08:00")), weekday=spec.get("weekday"))
    if t == "event":
        return EventTrigger(event=str(spec.get("event", "")))
    logger.warning(f"[Schedule] 未知 trigger 类型: {spec!r}")
    return None
