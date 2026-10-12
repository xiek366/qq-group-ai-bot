"""
资讯服务（P2c-1）：拉取编排 + 频控 + 去重 + 选条
==============================================

把"什么时候拉、拉哪个源、哪条还没发过"这些事收在一处，两个下游（资讯任务、每日一图）共用：

- **频控**（A.6/A.8 硬要求）：每源独立冷却（`NEWS_COOLDOWN_MINUTES`），
  连续失败时冷却**翻倍退避**（最多 ×8）；同一进程内不会并发拉同一个源；
  **启动后第一次 `refresh` 会依次串行拉取**（不并发，避免 B站风控）；
- **条件请求**：缓存 ETag/Last-Modified，多数请求应返回 304（几 KB 而已）；
- **降级**：单源失败 → 换下一个源；全部失败 → 返回空 → 调用方**本次不发**（绝不发占位内容）；
- **去重**：按 `NewsItem.fingerprint` 记"已推送"，跨天不重复推同一条（A.6 要求）；
- **来源轮换**（2026-10-12）：选条时优先避开最近推送过的来源——机核日更多篇、
  全带图，"全局最新优先"会让它垄断资讯与每日一图（实测 48h 候选池里机核占 91%）。

状态落 `data/news/state.json`（源冷却/退避/ETag + 已推送指纹 + 最近推送来源），进程重启也不丢。
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
from app.news.models import NewsItem, SourceState
from app.news.sources import build_sources
from app.paths import NEWS_CACHE_DIR, ensure_dir

logger = logging.getLogger(__name__)

_MAX_SEEN = 600          # 已推送指纹最多记这么多（够跨天去重，别让状态文件长太大）
_MAX_RECENT_SOURCES = 10  # 最近推送来源最多记这么多（轮换避让只看最近 2 条）


class NewsService:
    """资讯源管理器（拉取 + 频控 + 去重）"""

    def __init__(self, sources: Optional[list] = None, state_path: Optional[Path] = None):
        self.sources = sources if sources is not None else build_sources()
        self._path = (state_path or (ensure_dir(NEWS_CACHE_DIR) / "state.json"))
        ensure_dir(self._path.parent)
        self._lock = threading.RLock()
        self._states: dict[str, SourceState] = {}
        self._seen: list[str] = []
        self._recent_sources: list[str] = []   # 最近推送的来源（来源轮换用）
        self._items: list[NewsItem] = []
        self._loaded = False
        self._last_refresh = 0.0

    # ---------- 状态持久化 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for sid, st in (raw.get("sources") or {}).items():
                s = SourceState(source_id=sid)
                for k, v in (st or {}).items():
                    if hasattr(s, k):
                        setattr(s, k, v)
                self._states[sid] = s
            self._seen = list(raw.get("seen") or [])
            self._recent_sources = list(raw.get("recent_sources") or [])
            # 【P2c-hotfix-2】条目也要恢复：原来只落盘"冷却状态"，重启后条目是空的，
            # 而冷却还没过 → 一个源都不拉 → 手里 0 条 → 任务报"没有可推的新资讯"（线上踩到）
            for it in (raw.get("items") or []):
                try:
                    self._items.append(NewsItem(**it))
                except TypeError:
                    continue
        except Exception as e:
            logger.warning(f"[News] 状态读取失败（从空开始）: {e}")

    def save(self) -> None:
        self._load()
        payload = {
            "sources": {sid: {
                "last_fetch": s.last_fetch, "last_ok": s.last_ok, "fail_streak": s.fail_streak,
                "etag": s.etag, "last_modified": s.last_modified, "last_error": s.last_error,
                "items_seen": s.items_seen,
            } for sid, s in self._states.items()},
            "seen": self._seen[-_MAX_SEEN:],
            "recent_sources": self._recent_sources[-_MAX_RECENT_SOURCES:],
            # 只存最近的若干条（去重用的 seen 已经单独存了，这里够选条就行）
            "items": [self._to_dict(i) for i in self._items[:100]],
        }
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"[News] 状态写入失败: {e}")

    @staticmethod
    def _to_dict(i: NewsItem) -> dict:
        return {
            "source": i.source, "source_id": i.source_id, "title": i.title, "link": i.link,
            "pub_time": i.pub_time, "image": i.image, "category": i.category,
            "summary": i.summary, "guid": i.guid,
        }

    def state_of(self, source_id: str) -> SourceState:
        self._load()
        with self._lock:
            if source_id not in self._states:
                self._states[source_id] = SourceState(source_id=source_id)
            return self._states[source_id]

    # ---------- 拉取 ----------

    async def refresh(self, *, force: bool = False, now: Optional[float] = None) -> list[NewsItem]:
        """依次拉所有"已到冷却时间"的源；返回本次拿到的**全部**条目（含已推送过的）"""
        self._load()
        now = now if now is not None else time.time()
        collected: list[NewsItem] = []
        # 【P2c-hotfix-2】手里一条内容都没有时，冷却不该拦住取数（否则重启后会"空转"到冷却结束）
        fresh_start = not self._items
        if fresh_start and not force:
            logger.info("[News] 当前没有缓存条目 → 本次忽略冷却，强制拉一轮")
        for src in self.sources:
            st = self.state_of(src.source_id)
            if not force and not fresh_start:
                wait = st.cooldown_remaining(Config.NEWS_COOLDOWN_MINUTES, now)
                if wait > 0:
                    logger.debug(f"[News] {src.source_id} 冷却中（还要 {wait / 60:.0f} 分钟）")
                    continue
            st.last_fetch = now
            try:
                items, etag, last_modified, not_modified = await src.fetch(
                    etag=st.etag, last_modified=st.last_modified)
            except Exception as e:                      # 源自己已经兜底，这里是双保险
                items, etag, last_modified, not_modified = [], "", "", False
                logger.warning(f"[News] {src.source_id} 异常: {e}")

            if not_modified:
                st.fail_streak = 0
                st.last_ok = now
                st.last_error = ""
                continue
            if items:
                st.fail_streak = 0
                st.last_ok = now
                st.last_error = ""
                st.etag = etag or st.etag
                st.last_modified = last_modified or st.last_modified
                st.items_seen = len(items)
                collected.extend(items)
            else:
                st.fail_streak += 1
                st.last_error = "本次没有拿到内容"
                logger.warning(f"[News] {src.source_id} 本次无内容（连续失败 {st.fail_streak} 次 → 冷却退避）")

        with self._lock:
            if collected:
                # 新条目排在前面，保留最近 200 条够用
                self._items = collected + [i for i in self._items
                                           if i.fingerprint not in {c.fingerprint for c in collected}]
                self._items = self._items[:200]
            self._last_refresh = now
        self.save()
        return collected

    # ---------- 选条 ----------

    def all_items(self) -> list[NewsItem]:
        return list(self._items)

    def has_pushed(self, item: NewsItem) -> bool:
        self._load()
        return item.fingerprint in self._seen

    def fresh_items(self, *, max_age_hours: float = 48.0, limit: int = 5,
                    need_image: bool = False, now: Optional[float] = None) -> list[NewsItem]:
        """还没推送过、且发布时间足够新的条目（按时间倒序）"""
        self._load()
        now = now if now is not None else time.time()
        out = []
        for it in self._items:
            if self.has_pushed(it):
                continue
            if need_image and not it.has_image:
                continue
            if it.pub_time and it.age_hours(now) > max_age_hours:
                continue
            out.append(it)
        out.sort(key=lambda i: i.pub_time, reverse=True)
        return out[:limit]

    def pick_for_digest(self, *, now: Optional[float] = None) -> Optional[NewsItem]:
        """资讯任务/破冰用：**优先不带图的条目**，把带图的留给「每日一图」。

        为什么反过来（2026-10-08 观察线上成功案例后改的）：
        资讯任务与破冰资讯源**只发文字**（标题 + 点评 + 链接），根本不发那张图 ——
        所以让它们吃掉带图的条目是纯浪费，会让 19:30 的每日一图"没图可发"。
        实在只有带图条目时也会用（不会因此空手），只是顺序上让位。
        """
        items = self.fresh_items(limit=10, now=now)
        if not items:
            return None
        plain = [i for i in items if not i.has_image]
        return self._prefer_other_sources(plain or items)[0]

    def pick_image_item(self, *, now: Optional[float] = None) -> Optional[NewsItem]:
        """每日一图用：必须带图，且尽量新"""
        items = self.fresh_items(need_image=True, limit=10, now=now)
        return self._prefer_other_sources(items)[0] if items else None

    def _prefer_other_sources(self, items: list[NewsItem]) -> list[NewsItem]:
        """来源轮换：优先返回「最近 2 次没推过」的来源的条目（保持时间倒序）。

        背景（2026-10-12 实测）：机核日更多篇且全带图，"全局最新优先"会让它
        占据 ~91% 的推送位；B站专栏周更、游研社 feed 更新慢，完全没有出场机会。
        避让窗口取最近 2 次：机核最多连发 2 条就让位，其余源有新鲜内容时优先出场；
        其余源没有新内容时自然回落机核（它本来就是产量主力，不算委屈）。
        """
        if len(items) <= 1:
            return items
        avoid = set(self._recent_sources[-2:])
        if not avoid:
            return items
        others = [i for i in items if i.source_id not in avoid]
        return others or items

    def mark_pushed(self, *items: NewsItem) -> None:
        self._load()
        with self._lock:
            for it in items:
                fp = it.fingerprint
                if fp not in self._seen:
                    self._seen.append(fp)
                # 记录来源供轮换避让（同一条重复标记不重复计）
                if not self._recent_sources or self._recent_sources[-1] != it.source_id:
                    self._recent_sources.append(it.source_id)
            self._seen = self._seen[-_MAX_SEEN:]
            self._recent_sources = self._recent_sources[-_MAX_RECENT_SOURCES:]
        self.save()

    # ---------- 展示 ----------

    def diagnose(self, *, now: Optional[float] = None) -> str:
        """一句话诊断（用在"跳过"的原因里）：缓存几条、带图几条、其中未推送几条

        这样 `/任务 试跑 daily_image` 的跳过原因就能直接告诉我们是
        "源里就没有图" / "都推过了" / "都太旧了"，不用另写脚本查。
        """
        self._load()
        now = now if now is not None else time.time()
        total = len(self._items)
        with_img = [i for i in self._items if i.has_image]
        unpushed = [i for i in self._items if not self.has_pushed(i)]
        fresh_img = self.fresh_items(need_image=True, limit=999, now=now)
        max_age = Config.NEWS_MAX_AGE_HOURS
        too_old = [i for i in with_img if i.pub_time and i.age_hours(now) > max_age]
        return (f"缓存 {total} 条 / 带图 {len(with_img)} 条 / 未推送 {len(unpushed)} 条 / "
                f"可用（带图+未推+{max_age}h 内）{len(fresh_img)} 条"
                + (f"（其中 {len(too_old)} 条因超过 {max_age}h 被排除）" if too_old else ""))

    def status(self) -> dict:
        self._load()
        now = time.time()
        return {
            "sources": {sid: {
                "last_ok": s.last_ok,
                "fail_streak": s.fail_streak,
                "cooldown_minutes": round(s.cooldown_remaining(Config.NEWS_COOLDOWN_MINUTES, now) / 60, 1),
                "items_seen": s.items_seen,
                "last_error": s.last_error,
            } for sid, s in self._states.items()},
            "cached_items": len(self._items),
            "pushed_total": len(self._seen),
            "last_refresh": self._last_refresh,
            "diagnose": self.diagnose(now=now),
        }
