"""
话题状态（P1-3）
================

**解决什么**：破冰和主动插嘴现在是无状态的 —— 它抛出一个话题后，既记不住自己起了什么头，
也不知道这个话题还有没有人接着聊。P1 先把"当前话题"**记下来并注入上下文**；
P2 的跟进机制（无人应答就换个角度补一句）才有依据。

**谁写它**（两个来源，都不额外花钱）：
1. 滚动摘要的【当前话题】段（`ConversationSummary.run()` 之后由 handler 落进来）；
2. 主动发言时（破冰 / 插嘴）把"自己刚起的话头"直接记下来。

**过期**：超过 `TOPIC_STALE_MINUTES`（默认 90 分钟）没人提，就不再注入 —— 免得它拿一个
早就凉了的话题硬往上凑。

**文件**：`data/state/<群ID>.json`（P2a 的调度状态也会放这个文件，所以留了 version 字段）

    {"version": 1, "group_id": 100000004,
     "topic": {"title": "…20 字…", "opened_by": "100000008", "opened_nick": "制作者",
               "opened_at": 1791…, "last_activity": 1791…, "status": "open"}}

线程约定：文件读写是阻塞的，异步侧用 `run_blocking`；本模块方法都是同步的。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import Config
from app.paths import STATE_DIR, ensure_dir

logger = logging.getLogger(__name__)

# touch() 的落盘节流：每条消息都会调它，内存每次都更新，磁盘最多 5 分钟写一次
_TOUCH_SAVE_THROTTLE_SECONDS = 300.0


class TopicState:
    """按群隔离的"当前话题" """

    def __init__(self, state_dir: Optional[Path] = None):
        self._dir = ensure_dir(state_dir or STATE_DIR)
        self._lock = threading.RLock()
        self._cache: dict[int, dict] = {}
        self._last_touch_save: dict[int, float] = {}

    # ---------- 文件 ----------

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.json"

    @staticmethod
    def _blank(group_id: int) -> dict:
        return {"version": 1, "group_id": group_id, "topic": None}

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
                    state.update({k: raw[k] for k in raw if k in state})
            except Exception as e:
                logger.warning(f"[Topic] 群 {group_id} 读取失败，从空开始: {e}")
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
            logger.error(f"[Topic] 群 {group_id} 落盘失败: {e}")

    # ---------- 写 ----------

    def set_topic(
        self,
        group_id: int,
        title: str,
        by_uid=None,
        by_nick: str = "",
        ts: Optional[float] = None,
    ) -> Optional[dict]:
        """记录/替换当前话题（标题为空则视为清除）"""
        if not Config.TOPIC_ENABLED:
            return None
        title = (title or "").strip()[:40]
        # 注意：必须用 `is None` 判断，不能写 `ts or time.time()` ——
        # 那样传 ts=0（测试里用来伪造"很久以前"）会被当成"没传"（踩过）。
        now = time.time() if ts is None else ts
        state = self.load(group_id)
        if not title:
            if state.get("topic"):
                state["topic"] = None
                self.save(group_id, state)
            return None
        state["topic"] = {
            "title": title,
            "opened_by": str(by_uid) if by_uid is not None else "",
            "opened_nick": by_nick or "",
            "opened_at": now,
            "last_activity": now,
            "status": "open",
        }
        with self._lock:
            self._cache[group_id] = state
        self.save(group_id, state)
        logger.info(f"[Topic] 群 {group_id} 当前话题 → {title!r}")
        return state["topic"]

    def touch(self, group_id: int, ts: Optional[float] = None) -> None:
        """有人继续聊 → 刷新活跃时间（话题不换，只是不过期）

        这个方法会在**每条群消息**上被调用，所以落盘做了节流（默认 5 分钟一次）：
        内存里每次都更新，磁盘只在超时后才写，避免把对话流写成"每条消息一次 IO"。
        """
        now = time.time() if ts is None else ts
        state = self.load(group_id)
        topic = state.get("topic")
        if not topic:
            return
        topic["last_activity"] = now
        with self._lock:
            self._cache[group_id] = state
            last_save = self._last_touch_save.get(group_id, 0.0)
            should_save = (now - last_save) >= _TOUCH_SAVE_THROTTLE_SECONDS or last_save == 0.0
            if should_save:
                self._last_touch_save[group_id] = now
        if should_save:
            self.save(group_id, state)

    # ---------- 读 ----------

    def get(self, group_id: int) -> Optional[dict]:
        return self.load(group_id).get("topic")

    def is_stale(self, group_id: int, minutes: Optional[int] = None) -> bool:
        topic = self.get(group_id)
        if not topic:
            return True
        limit = (minutes if minutes is not None else Config.TOPIC_STALE_MINUTES) * 60
        return (time.time() - topic.get("last_activity", 0)) > limit

    def prompt_text(self, group_id: int) -> str:
        """给 system prompt 用的一段话；不适用/已过期返回空串"""
        if not Config.TOPIC_ENABLED:
            return ""
        topic = self.get(group_id)
        if not topic or self.is_stale(group_id):
            return ""
        age_min = max(0, int((time.time() - topic.get("opened_at", time.time())) / 60))
        who = topic.get("opened_nick") or ""
        by = f"，由 {who} 提起" if who else ""
        return (
            f"【当前话题】{topic['title']}（{age_min} 分钟前{by}）\n"
            f"有人接着聊时，顺着这个话题说；如果对方换了话题，就跟着换，不要硬拉回来。"
        )

    def clear(self, group_id: int) -> None:
        state = self._blank(group_id)
        with self._lock:
            self._cache[group_id] = state
        self.save(group_id, state)
        logger.info(f"[Topic] 群 {group_id} 话题状态已清空")

    def stats(self, group_id: int) -> dict:
        topic = self.get(group_id) or {}
        return {
            "title": topic.get("title", ""),
            "age_minutes": round((time.time() - topic["opened_at"]) / 60, 1) if topic.get("opened_at") else None,
            "stale": self.is_stale(group_id),
        }
