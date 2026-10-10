"""
运行指标（P0-4）
================

**为什么要它**：没有度量就没有优化依据 —— "哪类内容有人接""哪种失败最多""是不是变慢了"
这些都得先能看见。这一版只做**能直接说明问题**的几个指标，不追求全：

| 指标 | 口径 |
|---|---|
| 每群消息数 / bot 发言数 | 按天累计（保留最近 7 天）+ 累计总量 |
| **被回应率** | bot 发言后 **10 分钟内**有人说话，记一次"被回应"；同一句只记一次 |
| LLM 调用 / 失败率 / 平均延迟 / token 用量 | 全局累计 |
| 视觉调用 / 缓存命中率 / 失败数 / 内联数 | 全局累计 |

**线程约定**：`record_message` 由对话流的单 worker 线程调用（那里做批量落盘是安全的）；
`record_llm` / `record_vision` 由事件循环调用 —— 所以它们**只改内存、不落盘**，
避免把磁盘 I/O 带回事件循环（这是前面几轮辛苦换来的底线）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.paths import STATS_DIR, ensure_dir

logger = logging.getLogger(__name__)

# bot 发言后多久内有人说话算"被回应"
REPLY_WINDOW_SECONDS = 600
# 按天数据保留天数
KEEP_DAYS = 7


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


class Metrics:
    def __init__(self, path: Optional[Path] = None, save_every: int = 10):
        self._path = Path(path) if path else (ensure_dir(STATS_DIR) / "summary.json")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._save_every = max(1, save_every)
        self._dirty = 0
        self._data = self._blank()
        # group_id -> 上一条 bot 发言的时间（等待"被回应"判定；只存内存，重启后重来）
        self._pending_bot: dict[int, float] = {}
        # group_id -> 那条 bot 发言的"内容类型"（主动消息专用，用于按类型统计被回应率）
        self._pending_kind: dict[int, str] = {}
        self.load()

    # ---------- 结构 ----------

    @staticmethod
    def _blank() -> dict:
        return {
            "version": 1,
            "since": time.time(),
            "llm": {"calls": 0, "failures": 0, "latency_sum": 0.0, "tokens_in": 0, "tokens_out": 0},
            "vision": {"calls": 0, "cache_hits": 0, "failures": 0, "inlined": 0},
            "groups": {},
            "daily": {},
            "by_kind": {},          # 【P2c-8】内容类型 -> {sent, replied}
        }

    def _group(self, group_id: int) -> dict:
        return self._data["groups"].setdefault(
            str(group_id), {"user_msgs": 0, "bot_msgs": 0, "bot_responded": 0}
        )

    def _kind(self, kind: str) -> dict:
        """按"内容类型"聚合（P2c-8：面板看"哪类内容有人接"）"""
        return self._data["by_kind"].setdefault(
            kind or "other", {"sent": 0, "replied": 0}
        )

    def record_proactive(self, group_id: int, kind: str) -> None:
        """记录一条**主动消息**及其内容类型（任务名，如 icebreak / weather_morning / news_digest）。

        之后若群里在 REPLY_WINDOW_SECONDS 内有人说话，就算这条内容"被接了"——
        于是可以比较"哪类内容有人接"（这是个启发式：不区分对方接的是不是这个话题，
        和 `reply_rate` 的口径一致）。
        """
        with self._lock:
            self._kind(kind)["sent"] += 1
            self._pending_kind[group_id] = kind or "other"
            self._dirty += 1

    def _day(self, day: Optional[str] = None) -> dict:
        d = self._data["daily"].setdefault(
            day or _today(), {"user_msgs": 0, "bot_msgs": 0, "llm_calls": 0, "vision_calls": 0}
        )
        return d

    # ---------- 记录 ----------

    def record_message(self, group_id: int, role: str) -> None:
        """记录一条对话流消息（由单 worker 线程调用，允许批量落盘）"""
        now = time.time()
        with self._lock:
            g = self._group(group_id)
            day = self._day()
            if role == "assistant":
                g["bot_msgs"] += 1
                day["bot_msgs"] += 1
                self._pending_bot[group_id] = now
            else:
                g["user_msgs"] += 1
                day["user_msgs"] += 1
                ts = self._pending_bot.get(group_id)
                if ts and (now - ts) <= REPLY_WINDOW_SECONDS:
                    g["bot_responded"] += 1
                    self._pending_bot.pop(group_id, None)   # 同一句只记一次
                    kind = self._pending_kind.pop(group_id, "other")
                    self._kind(kind)["replied"] += 1
            self._dirty += 1
            should_save = self._dirty >= self._save_every
        if should_save:
            self.save()

    def record_llm(self, ok: bool, latency: float = 0.0,
                   tokens_in: int = 0, tokens_out: int = 0) -> None:
        """记录一次 LLM 调用（事件循环调用 → 只改内存）"""
        with self._lock:
            llm = self._data["llm"]
            llm["calls"] += 1
            if not ok:
                llm["failures"] += 1
            llm["latency_sum"] += float(latency or 0.0)
            llm["tokens_in"] += int(tokens_in or 0)
            llm["tokens_out"] += int(tokens_out or 0)
            self._day()["llm_calls"] += 1

    def record_vision(self, *, cached: bool = False, ok: bool = False, inlined: int = 0) -> None:
        """记录一次看图（事件循环调用 → 只改内存）"""
        with self._lock:
            v = self._data["vision"]
            v["calls"] += 1
            if cached:
                v["cache_hits"] += 1
            if not ok:
                v["failures"] += 1
            v["inlined"] += int(inlined or 0)
            self._day()["vision_calls"] += 1

    # ---------- 读取 ----------

    def snapshot(self) -> dict:
        """给面板用的快照（只读内存，不碰磁盘）"""
        with self._lock:
            data = json.loads(json.dumps(self._data))     # 深拷贝，避免调用方改到内部状态
            pending = len(self._pending_bot)

        llm = data["llm"]
        calls = llm["calls"] or 0
        llm_out = {
            **llm,
            "failure_rate": round(llm["failures"] / calls, 4) if calls else 0.0,
            "avg_latency": round(llm["latency_sum"] / calls, 3) if calls else 0.0,
        }
        vision = data["vision"]
        vcalls = vision["calls"] or 0
        vision_out = {
            **vision,
            "hit_rate": round(vision["cache_hits"] / vcalls, 4) if vcalls else 0.0,
            "failure_rate": round(vision["failures"] / vcalls, 4) if vcalls else 0.0,
        }
        groups = []
        for gid, g in sorted(data["groups"].items(), key=lambda kv: -kv[1]["user_msgs"]):
            bot = g["bot_msgs"] or 0
            groups.append({
                "group_id": gid,
                **g,
                "reply_rate": round(g["bot_responded"] / bot, 4) if bot else 0.0,
            })
        days = [{"date": d, **v} for d, v in sorted(data["daily"].items())[-KEEP_DAYS:]]
        # 【P2c-8】按内容类型聚合"哪类内容有人接"（只统计发过 ≥1 条的类型）
        by_kind = []
        for kind, kv in (data.get("by_kind") or {}).items():
            sent = kv.get("sent", 0) or 0
            if not sent:
                continue
            by_kind.append({
                "kind": kind,
                "sent": sent,
                "replied": kv.get("replied", 0),
                "reply_rate": round(kv.get("replied", 0) / sent, 4),
            })
        by_kind.sort(key=lambda x: (-x["sent"], x["kind"]))
        return {
            "since": data["since"],
            "llm": llm_out,
            "vision": vision_out,
            "groups": groups,
            "days": days,
            "by_kind": by_kind,
            "pending_reply_checks": pending,
        }

    # ---------- 落盘 ----------

    def save(self) -> None:
        """原子写盘（在 worker 线程或退出时调用；别在事件循环里调）"""
        with self._lock:
            self._prune_daily()
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            self._dirty = 0
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"[Metrics] 落盘失败: {e}")

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Metrics] 读取失败，从零开始: {e}")
            return
        if not isinstance(raw, dict):
            return
        blank = self._blank()
        with self._lock:
            for key in ("llm", "vision"):
                if isinstance(raw.get(key), dict):
                    blank[key].update({k: v for k, v in raw[key].items() if k in blank[key]})
            for key in ("groups", "daily"):
                if isinstance(raw.get(key), dict):
                    blank[key] = raw[key]
            if raw.get("since"):
                blank["since"] = raw["since"]
            self._data = blank

    def _prune_daily(self) -> None:
        """只保留最近 KEEP_DAYS 天"""
        daily = self._data.get("daily", {})
        if len(daily) <= KEEP_DAYS:
            return
        for key in sorted(daily.keys())[:-KEEP_DAYS]:
            daily.pop(key, None)


# ---------- 全局单例 ----------

_instance: Optional[Metrics] = None


def get() -> Metrics:
    global _instance
    if _instance is None:
        _instance = Metrics()
    return _instance
