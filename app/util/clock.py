"""
统一时间源 —— 显式时区，不看服务器的 TZ
========================================

云端（阿里云 Ubuntu）默认时区常常是 UTC。项目里有两处按「本地时间」做决策：

  1. 破冰的工作时间窗（ICEBREAK_WORK_START ~ ICEBREAK_WORK_END，默认 8~23 点）
  2. `/爱丽丝 几点了` 的报时工具

直接 `datetime.now()` 会跟着服务器 TZ 走：UTC 服务器上窗口整体偏 8 小时，
Agent 会在凌晨三点起来破冰。所以这里统一用 Config.TIMEZONE 算时间。

zoneinfo 在 Linux 上是标准库自带的；Windows 本地开发没有 IANA 数据库，
退化到固定偏移（Asia/Shanghai → UTC+8），保证本地跑测试也不会炸。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from app.config import Config

logger = logging.getLogger(__name__)

# 拿不到 IANA 数据库时用的固定偏移兜底（小时）
_FALLBACK_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Urumqi": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "UTC": 0,
}

_tz_cache: dict[str, dt.tzinfo] = {}


def _resolve_tz(name: str) -> dt.tzinfo:
    if name in _tz_cache:
        return _tz_cache[name]

    tz: Optional[dt.tzinfo] = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(name)
    except Exception:
        offset = _FALLBACK_OFFSETS.get(name)
        if offset is not None:
            tz = dt.timezone(dt.timedelta(hours=offset), name)
            logger.info(f"[Clock] 本机无 IANA 时区库，{name} 退化为 UTC{offset:+d}")
        else:
            tz = dt.datetime.now().astimezone().tzinfo
            logger.warning(
                f"[Clock] 无法解析时区 {name}，退回系统本地时区；"
                f"请在 .env 里把 TIMEZONE 设成正确的值"
            )
    _tz_cache[name] = tz
    return tz


def tz() -> dt.tzinfo:
    """当前配置的时区对象"""
    return _resolve_tz(Config.TIMEZONE)


def now() -> dt.datetime:
    """带时区的当前时间（默认 Asia/Shanghai）"""
    return dt.datetime.now(tz())


def from_timestamp(ts: float) -> dt.datetime:
    """把 Unix 时间戳转成本时区的 datetime（给日志格式化用）"""
    return dt.datetime.fromtimestamp(ts, tz())


def tz_label() -> str:
    """给用户看的时区标签，如 Asia/Shanghai (UTC+8)"""
    off = now().utcoffset() or dt.timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    label = f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")
    return f"{Config.TIMEZONE} ({label})"


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
