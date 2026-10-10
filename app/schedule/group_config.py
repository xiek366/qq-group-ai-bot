"""
群配置（P2a-7）
===============

取值顺序（ROADMAP §4.9）：**单群覆盖 > 环境变量 > 代码默认**

- **环境变量**：现有 `ICEBREAK_*` / `PROACTIVE_GROUPS` / `QUIET_HOURS` / `DAILY_TOTAL_LIMIT`
  继续作为全局默认（用户已经配好的东西不能因为上新框架就失效）；
- **单群覆盖**：`data/group_config/<群ID>.json`，形如

    {
      "quiet_hours": ["23:00", "08:00"],
      "daily_total_limit": 8,
      "tasks": {
        "icebreak": {"enabled": true,
                     "trigger": {"type": "idle", "minutes": 45},
                     "cooldown_minutes": 90,
                     "backoff": {"on_no_reply": true, "factor": 2, "max_level": 3}}
      }
    }

  缺省项一律继承全局；文件不存在就用全局默认（**所以 P2a 上线不需要你新建任何文件**）。

**D15 不在这个文件里**：主动打扰范围仍由 `PROACTIVE_GROUPS`（环境变量）统一控制，
调度器只对它做一道判断 —— 免得"某个群是不是允许主动说话"出现两处真相。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from app.config import Config
from app.paths import GROUP_CONFIG_DIR, ensure_dir

logger = logging.getLogger(__name__)


def env_task_defaults() -> dict:
    """环境变量 → 任务默认配置（现有 ICEBREAK_* / WEATHER_* / WATER_* 全部映射进来，保证行为可延续）"""
    return {
        "icebreak": {
            "enabled": Config.ICEBREAK_ENABLED,
            "trigger": {"type": "idle", "minutes": Config.ICEBREAK_IDLE_MINUTES},
            "cooldown_minutes": Config.ICEBREAK_COOLDOWN_MINUTES,
            "min_group_size": Config.ICEBREAK_MIN_GROUP_SIZE,
            # Q1 的答复：去掉随机概率，改成"冷却 + 无人应答退避"
            "work_hours": [Config.ICEBREAK_WORK_START, Config.ICEBREAK_WORK_END],
            "backoff": {"on_no_reply": True, "factor": 2, "max_level": 3},
        },
        "summary_job": {
            "enabled": Config.SUMMARY_ENABLED,
            "trigger": {"type": "interval", "minutes": Config.SUMMARY_COOLDOWN_MINUTES},
            "cooldown_minutes": Config.SUMMARY_COOLDOWN_MINUTES,
            "min_messages": Config.SUMMARY_MIN_MESSAGES,
        },
        # 【P2b】天气播报与喝水提醒：环境变量只给"全局默认"，
        # 真正决定"这个群播不播"的是群配置里的 weather.city（未绑城市 → 任务自己跳过）
        "weather_morning": {
            "enabled": Config.WEATHER_ENABLED,
            "trigger": {"type": "cron", "at": Config.WEATHER_BROADCAST_AT},
        },
        "water_reminder": {
            "enabled": Config.WATER_REMINDER_ENABLED,
            "trigger": {"type": "interval", "minutes": Config.WATER_REMINDER_INTERVAL_MINUTES},
            "daily_limit": Config.WATER_REMINDER_DAILY_LIMIT,
            "require_active_within_minutes": Config.WATER_REMINDER_ACTIVE_WITHIN,
        },
        # 【P2c】资讯 / 每日一句 / 每日一图（时刻可在群配置里逐项覆盖）
        "news_digest": {
            "enabled": Config.NEWS_ENABLED,
            "trigger": {"type": "cron", "at": Config.NEWS_DIGEST_AT},
        },
        "daily_quote": {
            "enabled": True,
            "trigger": {"type": "cron", "at": Config.DAILY_QUOTE_AT},
        },
        "daily_image": {
            "enabled": Config.NEWS_ENABLED,
            "trigger": {"type": "cron", "at": Config.DAILY_IMAGE_AT},
        },
    }


def normalize_quiet_hours(value) -> str:
    """静默时段统一成 "HH:MM-HH:MM" 字符串。

    配置文件里允许写成 `["23:00", "08:00"]`（ROADMAP 示例的形式），环境变量里是
    `"23:00-08:00"` —— 这里统一成后者，调度器只认一种形式，避免两处解析逻辑不一致。
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{value[0]}-{value[1]}"
    return str(value or "")


class GroupConfigStore:
    """读取 `data/group_config/<群ID>.json`，并与环境变量默认值合并"""

    def __init__(self, config_dir: Optional[Path] = None):
        self._dir = ensure_dir(config_dir or GROUP_CONFIG_DIR)
        self._lock = threading.RLock()
        self._cache: dict[int, dict] = {}
        self._env_defaults = env_task_defaults()

    def invalidate(self, group_id: Optional[int] = None) -> None:
        with self._lock:
            if group_id is None:
                self._cache.clear()
            else:
                self._cache.pop(group_id, None)

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.json"

    def _load_file(self, group_id: int) -> dict:
        path = self._path(group_id)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.error(f"[Schedule] 群配置 {path} 解析失败（按默认处理）: {e}")
            return {}

    def get(self, group_id: int) -> dict:
        """返回该群的有效配置：{group_id, quiet_hours, daily_total_limit, tasks:{name:cfg}}"""
        with self._lock:
            cached = self._cache.get(group_id)
        if cached is not None:
            return cached

        raw = self._load_file(group_id)
        tasks: dict[str, dict] = {}
        for name, defaults in self._env_defaults.items():
            merged = dict(defaults)
            override = (raw.get("tasks") or {}).get(name) or {}
            for k, v in override.items():
                if k in ("trigger", "backoff") and isinstance(v, dict):
                    base = dict(merged.get(k) or {})
                    base.update(v)
                    merged[k] = base
                else:
                    merged[k] = v
            tasks[name] = merged

        cfg = {
            "group_id": group_id,
            "quiet_hours": normalize_quiet_hours(raw.get("quiet_hours") or Config.QUIET_HOURS),
            "daily_total_limit": int(raw.get("daily_total_limit", Config.DAILY_TOTAL_LIMIT)),
            "weather": dict(raw.get("weather") or {}),      # 【P2b】{"city": "北京", "broadcast_at": "08:00"}
            "tasks": tasks,
        }
        with self._lock:
            self._cache[group_id] = cfg
        return cfg

    def task_config(self, group_id: int, task_name: str) -> dict:
        return dict((self.get(group_id).get("tasks") or {}).get(task_name) or {})

    def weather_city(self, group_id: int) -> str:
        """本群绑定的天气城市（群配置里的 weather.city；没绑返回空串）"""
        return str(((self.get(group_id).get("weather") or {}).get("city")) or "").strip()
