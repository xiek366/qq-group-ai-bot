"""
资讯数据模型（P2c-1）
====================

**所有来源统一成同一个 item 结构**（ROADMAP A.7 的落地要点）：
RSS（机核/游研社）与 B站专栏（api.bilibili.com）解析后都是这个 `NewsItem`，
于是下游的"资讯推送"与"每日一图"完全不用区分来源。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class NewsItem:
    """一条资讯（只保留"标题 + 链接 + 来源 + 时间 + 可选配图"）"""

    source: str                      # 显示用的来源名（机核 / 游研社 / B站·Gal周报）
    source_id: str                   # 源标识（gcores / yystv / bili_2072586344）
    title: str
    link: str
    pub_time: float = 0.0            # Unix 时间戳（解析不出来就是 0）
    image: str = ""                  # 封面/内嵌图 URL（可能为空）
    category: str = ""               # 游研社的 category 之类
    summary: str = ""                # 摘要（**不推送正文**，只用于给 LLM 判断要点）
    guid: str = ""

    @property
    def fingerprint(self) -> str:
        """去重指纹：优先 guid，其次链接，最后标题 —— 用于跨天去重（A.6 要求）"""
        raw = self.guid or self.link or self.title
        return hashlib.sha1(f"{self.source_id}\u0001{raw}".encode("utf-8")).hexdigest()[:16]

    @property
    def has_image(self) -> bool:
        """有可用图片（协议相对地址 `//host/x.jpg` 已在解析时补全成 https）"""
        return bool(self.image and self.image.startswith(("http://", "https://")))

    def age_hours(self, now: float) -> float:
        if not self.pub_time:
            return 999.0
        return max(0.0, (now - self.pub_time) / 3600)


@dataclass
class SourceState:
    """单个源的运行状态（冷却 / 退避 / 条件请求缓存）—— 落 `data/news/sources.json`"""

    source_id: str
    last_fetch: float = 0.0
    last_ok: float = 0.0
    fail_streak: int = 0
    etag: str = ""
    last_modified: str = ""
    last_error: str = ""
    items_seen: int = 0
    extra: dict = field(default_factory=dict)

    def cooldown_remaining(self, cooldown_minutes: int, now: float) -> float:
        """还要等多少秒才能再拉这个源（退避：连续失败时冷却翻倍，最多 ×8）"""
        if not self.last_fetch:
            return 0.0
        factor = min(8, 2 ** min(self.fail_streak, 3))
        wait = cooldown_minutes * 60 * factor
        return max(0.0, wait - (now - self.last_fetch))
