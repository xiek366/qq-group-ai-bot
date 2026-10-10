"""
任务状态存储（P2a-3）
====================

每个（群 × 任务）需要记住四件事才能正确调度：

| 字段 | 用途 |
|---|---|
| `last_run` | interval / cron 的"上次执行"，也是冷却计时起点 |
| `day` + `count` | 当日执行次数（跨天自动归零）→ 每任务每日上限 |
| `backoff_level` | 连续"无人应答"的退避级别（Q1：30 → 60 → 120 分钟） |
| `fingerprints` | 最近发过的内容指纹，用于**跨天去重**（连续几天别推同一条） |

**存在哪**：`data/schedule/<群ID>.json`，**独立于 P1 的 `data/state/<群ID>.json`**。

> 为什么不合到一个文件里：`TopicState` 保存时是把自己内存里的整份 dict 写回文件，
> 它并不知道 `tasks` / `budget` 的存在 —— 两边共用一个文件就会**互相抹掉对方的数据**。
> 所以任务状态单独一个文件（ROADMAP 里本来也是 `schedule/`），各写各的，零耦合。

文件形如：

    {"version": 1, "group_id": 100000004,
     "budget": {"day": "2026-10-07", "total": 3},
     "tasks": {
        "icebreak": {"last_run": 1791…, "day": "2026-10-07", "count": 2,
                     "backoff_level": 1, "last_result": "sent",
                     "fingerprints": ["a1b2c3…"], "last_content": "…" ,
                     "pending_check_ts": 1791…}
     }}

线程约定：同步阻塞方法；异步侧用 `run_blocking` 包。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.paths import SCHEDULE_DIR, ensure_dir
from app.util import clock

logger = logging.getLogger(__name__)

# 内容指纹只留最近这么多条（够跨天去重就行，别让状态文件无限长大）
_MAX_FINGERPRINTS = 30


def today_key(now: Optional[float] = None) -> str:
    """按群时区算"今天是哪天"（跨天计数与 cron 判定都用它）"""
    dt = clock.from_timestamp(now if now is not None else time.time())
    return dt.strftime("%Y-%m-%d")


def fingerprint(*parts: str) -> str:
    """内容指纹：用于跨天去重（同样的内容不再重复发）"""
    raw = "\u0001".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class TaskStateStore:
    """`data/schedule/<群ID>.json` 的读写（budget + tasks 两段；话题状态在 P1 的另一个文件里）"""

    def __init__(self, schedule_dir: Optional[Path] = None):
        self._dir = ensure_dir(schedule_dir or SCHEDULE_DIR)
        self._lock = threading.RLock()
        self._cache: dict[int, dict] = {}

    # ---------- 文件 ----------

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.json"

    @staticmethod
    def _blank(group_id: int) -> dict:
        return {"version": 1, "group_id": group_id,
                "budget": {"day": today_key(), "total": 0}, "tasks": {}}

    def load(self, group_id: int) -> dict:
        with self._lock:
            cached = self._cache.get(group_id)
        if cached is not None:
            return cached
        state = self._blank(group_id)
        path = self._path(group_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in ("budget", "tasks", "version", "group_id"):
                        if key in raw:
                            state[key] = raw[key]
            except Exception as e:
                logger.warning(f"[Schedule] 群 {group_id} 状态读取失败，从空开始: {e}")
        if not isinstance(state.get("tasks"), dict):
            state["tasks"] = {}
        if not isinstance(state.get("budget"), dict):
            state["budget"] = {"day": today_key(), "total": 0}
        with self._lock:
            self._cache[group_id] = state
        return state

    def save(self, group_id: int, state: Optional[dict] = None) -> None:
        state = state if state is not None else self.load(group_id)
        path = self._path(group_id)
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.error(f"[Schedule] 群 {group_id} 状态落盘失败: {e}")

    # ---------- 任务状态 ----------

    def task_state(self, group_id: int, task_name: str) -> dict:
        state = self.load(group_id)
        ts = state["tasks"].get(task_name)
        if not isinstance(ts, dict):
            ts = {}
        ts.setdefault("last_run", 0.0)
        ts.setdefault("day", "")
        ts.setdefault("count", 0)
        ts.setdefault("backoff_level", 0)
        ts.setdefault("fingerprints", [])
        ts.setdefault("last_result", "")
        ts.setdefault("last_content", "")
        ts.setdefault("pending_check_ts", 0.0)
        ts.setdefault("last_error", "")
        state["tasks"][task_name] = ts
        return ts

    def count_today(self, group_id: int, task_name: str, now: Optional[float] = None) -> int:
        ts = self.task_state(group_id, task_name)
        return int(ts.get("count", 0)) if ts.get("day") == today_key(now) else 0

    def budget_used_today(self, group_id: int, now: Optional[float] = None) -> int:
        state = self.load(group_id)
        b = state.get("budget") or {}
        return int(b.get("total", 0)) if b.get("day") == today_key(now) else 0

    # ---------- 写入（都由调度器在"真的发了/真的跳过了"之后调用） ----------

    def record_run(self, group_id: int, task_name: str, *, result: str, content: str = "",
                   fingerprint_value: str = "", count_budget: bool = True,
                   now: Optional[float] = None) -> dict:
        """记一次执行。`count_budget=False` 用于"试跑"以外的特殊场景（试跑根本不调这里）"""
        ts = self.task_state(group_id, task_name)
        now = now if now is not None else time.time()
        day = today_key(now)
        ts["last_run"] = now
        ts["last_result"] = result
        ts["last_error"] = ""
        ts["fail_count"] = 0        # 成功即清零：失败退避只针对"连续失败"
        if ts.get("day") != day:
            ts["day"] = day
            ts["count"] = 0
        ts["count"] = int(ts.get("count", 0)) + 1
        if content:
            ts["last_content"] = content[:200]
        if fingerprint_value:
            fps = [f for f in (ts.get("fingerprints") or []) if f != fingerprint_value]
            fps.append(fingerprint_value)
            ts["fingerprints"] = fps[-_MAX_FINGERPRINTS:]
        # 记下"什么时候开始等人回应"，供下一 tick 判断退避
        ts["pending_check_ts"] = now

        state = self.load(group_id)
        if count_budget:
            b = state.get("budget") or {"day": day, "total": 0}
            if b.get("day") != day:
                b = {"day": day, "total": 0}
            b["total"] = int(b.get("total", 0)) + 1
            state["budget"] = b
        with self._lock:
            self._cache[group_id] = state
        return ts

    def record_skip(self, group_id: int, task_name: str, reason: str, now: Optional[float] = None) -> None:
        """被拦下时不计数、不写 last_run（否则会把冷却/退避搞乱），只在状态里留个原因便于排查"""
        ts = self.task_state(group_id, task_name)
        ts["last_skip_reason"] = reason
        ts["last_skip_ts"] = now if now is not None else time.time()

    def record_error(self, group_id: int, task_name: str, error: str,
                     now: Optional[float] = None) -> None:
        """记录一次执行失败。

        ⚠️ **必须同时推进 `last_run`**（2026-10-08 线上事故）：
        原来只写 `last_error`，于是 interval/cron 触发器认为"这任务还没到过点" →
        **每个 tick 都重试**。配合 15 秒的 LLM 超时，就变成"45 秒一轮、永不停止"地
        猛打 LLM —— 既烧额度，又可能把自己打成被限流，形成恶性循环。
        现在：失败也推进 last_run（按正常冷却间隔重试），并累加 `fail_count` 供指数退避。
        """
        ts = self.task_state(group_id, task_name)
        ts["last_result"] = "error"
        ts["last_error"] = str(error)[:200]
        ts["last_run"] = now if now is not None else time.time()
        ts["fail_count"] = int(ts.get("fail_count", 0) or 0) + 1

    def bump_backoff(self, group_id: int, task_name: str, max_level: int) -> int:
        ts = self.task_state(group_id, task_name)
        ts["backoff_level"] = min(int(ts.get("backoff_level", 0)) + 1, max_level)
        ts["pending_check_ts"] = 0.0
        return ts["backoff_level"]

    def reset_backoff(self, group_id: int, task_name: str) -> None:
        ts = self.task_state(group_id, task_name)
        if ts.get("backoff_level"):
            logger.info(f"[Schedule] 群 {group_id} 任务 {task_name} 有人应答 → 退避清零")
        ts["backoff_level"] = 0
        ts["pending_check_ts"] = 0.0

    def seen_fingerprint(self, group_id: int, task_name: str, fp: str) -> bool:
        return fp in (self.task_state(group_id, task_name).get("fingerprints") or [])

    def all_states(self, group_id: int) -> dict:
        return dict(self.load(group_id).get("tasks") or {})

    def clear(self, group_id: int) -> None:
        state = self._blank(group_id)
        with self._lock:
            self._cache[group_id] = state
        self.save(group_id, state)
