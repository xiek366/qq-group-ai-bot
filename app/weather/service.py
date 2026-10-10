"""
天气服务（P2b-1）：缓存 + 规则引擎 + 两种渲染
=============================================

一个服务、两个入口（ROADMAP §5.1）：**定时播报**（`weather_morning`）与**命令**（`/天气`）
共用这里的同一份逻辑，绝不写两遍。

三件事：

1. **缓存**（§5.5）：键 = `城市 + 日期`，落 `data/cache/weather/<城市>_<日期>.json`。
   同一天同城多群只请求一次 API。跨天自然失效（文件名带日期）。
2. **规则引擎**（§5.3 / Q4）：由**代码**把数值映射成注意事项，不经 LLM —— 阈值可测、可解释：
   降雨概率 ≥40% 带伞 ｜ 昼夜温差 ≥8℃ 加衣 ｜ 最高 ≥33℃ 防暑 ｜ 最低 ≤5℃ 保暖 ｜ UV ≥6 防晒 ｜ 风大提醒
3. **渲染**：`render_broadcast()` 给定时播报（事实 + 注意事项 + 收尾），
   `render_command()` 给 `/天气`（更紧凑，带明天）。

降级（§5.5 硬要求）：API 失败 → 返回 None → **本次不播报**，绝不发占位内容；`/天气` 则明确说"查不到"。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import Config
from app.paths import WEATHER_CACHE_DIR, ensure_dir
from app.util import clock
from app.weather.provider import (
    CITIES,
    WeatherDaily,
    WeatherProvider,
    create_provider,
    describe_code,
    resolve_city,
)

logger = logging.getLogger(__name__)

# 注意事项阈值（Q4：按 ROADMAP §5.3）
PRECIP_UMBRELLA = 40.0     # 降水概率 ≥40% → 带伞
TEMP_RANGE_CLOTHES = 8.0   # 昼夜温差 ≥8℃ → 加衣
HOT_TEMP = 33.0            # 最高温 ≥33℃ → 防暑补水
COLD_TEMP = 5.0            # 最低温 ≤5℃ → 保暖
UV_SUNSCREEN = 6.0         # UV ≥6 → 防晒
WIND_STRONG = 30.0         # 风速 ≥30km/h（约 5 级）→ 风大提醒


def notes_for(d: WeatherDaily) -> list[str]:
    """数值 → 注意事项（纯规则，可单测）。返回 0~3 条，按重要程度排序。"""
    notes: list[str] = []
    if d.precip_prob is not None and d.precip_prob >= PRECIP_UMBRELLA:
        notes.append(f"降水概率 {d.precip_prob:.0f}%，出门带把伞")
    if d.tmax is not None and d.tmin is not None and (d.tmax - d.tmin) >= TEMP_RANGE_CLOTHES:
        notes.append(f"昼夜温差 {(d.tmax - d.tmin):.0f}℃，早晚记得添件衣服")
    if d.tmax is not None and d.tmax >= HOT_TEMP:
        notes.append(f"最高 {d.tmax:.0f}℃，注意防暑、多补水")
    if d.tmin is not None and d.tmin <= COLD_TEMP:
        notes.append(f"最低 {d.tmin:.0f}℃，注意保暖别硬扛")
    if d.uv_max is not None and d.uv_max >= UV_SUNSCREEN:
        notes.append(f"紫外线指数 {d.uv_max:.0f}，记得防晒")
    if d.wind_max is not None and d.wind_max >= WIND_STRONG:
        notes.append(f"风有点大（{d.wind_max:.0f}km/h），注意高空坠物、骑车小心")
    return notes[:3]                      # 最多 3 条，避免播报变长


def fact_line(d: WeatherDaily) -> str:
    """事实句：气温区间 + 天气 + 降水概率 + 风力"""
    parts = []
    if d.tmin is not None and d.tmax is not None:
        parts.append(f"{d.tmin:.0f}~{d.tmax:.0f}℃")
    elif d.tmax is not None:
        parts.append(f"最高 {d.tmax:.0f}℃")
    elif d.tmin is not None:
        parts.append(f"最低 {d.tmin:.0f}℃")
    if d.desc:
        parts.append(d.desc)
    if d.precip_prob is not None:
        parts.append(f"降水概率 {d.precip_prob:.0f}%")
    if d.wind_max is not None:
        parts.append(f"风 {d.wind_max:.0f}km/h" if d.wind_max >= WIND_STRONG else "风不大")
    return "，".join(parts) if parts else "今天的天气数据不完整"


# 收尾句模板池（Q1：LLM 润色可用不上时回落这里）
_FINISH_TEMPLATES = (
    "今天也要好好冒险哦～",
    "准备好了就出发吧！",
    "愿今天的运气值拉满 ✨",
    "记得照顾好自己，勇者！",
)


class WeatherService:
    """缓存 + 规则 + 渲染（播报与命令共用）"""

    def __init__(self, provider: Optional[WeatherProvider] = None,
                 cache_dir: Optional[Path] = None):
        self.provider = provider or create_provider()
        self._dir = ensure_dir(cache_dir or WEATHER_CACHE_DIR)
        self._lock = threading.RLock()
        self._mem: dict[str, WeatherDaily] = {}

    # ---------- 取数据（带缓存） ----------

    @staticmethod
    def today() -> str:
        return clock.now().strftime("%Y-%m-%d")

    def _path(self, city: str, day: str) -> Path:
        return self._dir / f"{city}_{day}.json"

    @staticmethod
    def _to_dict(d: WeatherDaily) -> dict:
        return {
            "city": d.city, "date": d.date, "code": d.code, "desc": d.desc,
            "tmax": d.tmax, "tmin": d.tmin, "precip_prob": d.precip_prob,
            "wind_max": d.wind_max, "uv_max": d.uv_max,
        }

    @staticmethod
    def _from_dict(raw: dict) -> WeatherDaily:
        d = WeatherDaily(city=raw.get("city", ""), date=raw.get("date", ""))
        d.code = raw.get("code")
        d.desc = raw.get("desc") or describe_code(d.code)
        d.tmax = raw.get("tmax")
        d.tmin = raw.get("tmin")
        d.precip_prob = raw.get("precip_prob")
        d.wind_max = raw.get("wind_max")
        d.uv_max = raw.get("uv_max")
        return d

    def get_daily(self, city: str, *, use_cache: bool = True) -> Optional[WeatherDaily]:
        """同步读缓存（命中就返回）；没命中返回 None（真正的网络请求在 `fetch_daily`）"""
        city = resolve_city(city, default="")
        if not city:
            return None
        key = f"{city}_{self.today()}"
        if use_cache:
            with self._lock:
                hit = self._mem.get(key)
            if hit is not None:
                return hit
            path = self._path(city, self.today())
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    d = self._from_dict(raw)
                    if d.ok:
                        with self._lock:
                            self._mem[key] = d
                        logger.debug(f"[Weather] 缓存命中 {city}")
                        return d
                except Exception as e:
                    logger.warning(f"[Weather] 缓存读取失败（{path.name}）: {e}")
        return None

    async def fetch_daily(self, city: str, *, force: bool = False) -> Optional[WeatherDaily]:
        """取当天天气：先看缓存，没有才请求 API 并写缓存。失败返回 None（调用方据此不播报）。"""
        city = resolve_city(city, default="")
        if not city:
            return None
        if not force:
            cached = self.get_daily(city)
            if cached is not None:
                return cached

        d = await self.provider.fetch_daily(city)
        if d is None or not d.ok:
            logger.warning(f"[Weather] {city} 取数失败 → 本次不使用（绝不发占位内容）")
            return None
        # 防串城市：缓存文件名按请求的城市生成，内容里的 city 必须一致，
        # 否则会出现"上海_2026-10-08.json 里装着北京的天气"这种事故。
        if d.city != city:
            logger.warning(f"[Weather] provider 返回的城市({d.city!r})与请求({city!r})不一致，已按请求纠正")
            d.city = city
        self._save(city, d)
        return d

    def _save(self, city: str, d: WeatherDaily) -> None:
        key = f"{city}_{self.today()}"
        with self._lock:
            self._mem[key] = d
        path = self._path(city, self.today())
        try:
            tmp = path.with_suffix(".json.tmp")
            payload = {"fetched_at": time.time(), "day": self.today(), **self._to_dict(d)}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"[Weather] 缓存写入失败: {e}")

    # ---------- 渲染 ----------

    @staticmethod
    def finish_line(seed: int = 0) -> str:
        return _FINISH_TEMPLATES[seed % len(_FINISH_TEMPLATES)]

    def render_broadcast(self, city: str, d: WeatherDaily, finish: str = "") -> str:
        """早间播报：事实 + 注意事项 + 收尾（3 行短句，一条消息发出去）"""
        now = clock.now()
        lines = [f"🌤 {city} · {now.month}月{now.day}日", fact_line(d)]
        notes = notes_for(d)
        if notes:
            lines.append("⚠️ " + "；".join(notes))
        lines.append(finish or self.finish_line(now.day))
        return "\n".join(lines)

    def render_command(self, city: str, d: WeatherDaily, tomorrow: Optional[WeatherDaily] = None) -> str:
        """`/天气` 回复：比播报更紧凑，附带明天"""
        lines = [f"🌤 {city}", fact_line(d)]
        notes = notes_for(d)
        if notes:
            lines.append("⚠️ " + "；".join(notes))
        if tomorrow is not None and tomorrow.ok:
            t = []
            if tomorrow.tmin is not None and tomorrow.tmax is not None:
                t.append(f"{tomorrow.tmin:.0f}~{tomorrow.tmax:.0f}℃")
            if tomorrow.desc:
                t.append(tomorrow.desc)
            if tomorrow.precip_prob is not None:
                t.append(f"降水概率 {tomorrow.precip_prob:.0f}%")
            if t:
                lines.append("明天：" + "，".join(t))
        return "\n".join(lines)

    @staticmethod
    def city_list_text() -> str:
        return "、".join(CITIES)
