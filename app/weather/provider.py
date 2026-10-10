"""
天气 provider（P2b-1）
=====================

**已决策（ROADMAP §5.2）**：本期用 **Open-Meteo** —— 免费、免注册、返回 JSON、
日级预报自带降水概率/温度区间/风速/UV，正好够拼"事实 + 注意事项"两段。

**为什么把城市写死在代码里**（4 个预置城市 + 经纬度）：走地理编码会多一次请求、
还会遇到城市名拼写/编码问题。用户只会关心这几个城市，硬编码最稳。
将来要更多城市时，再打开地理编码兜底（provider 层已留扩展点）。

provider 是可插拔的：将来想要"穿衣指数"这类现成结论，加一个 `QWeatherProvider` 即可
（`WEATHER_PROVIDER=qweather`），service 层不用改。

**网络与降级**：一次 HTTP GET，超时沿用 `Config.HTTP_TIMEOUT`；
任何异常/字段缺失都不抛给上层 —— 返回 `None` 或"尽力填"，由 service 决定"不播报"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

from app.config import Config

logger = logging.getLogger(__name__)

# 预置城市（ROADMAP §5.4）：经纬度直接写死，零地理编码请求
CITIES: dict[str, tuple[float, float]] = {
    "北京": (39.90, 116.41),
    "哈尔滨": (45.80, 126.53),
    "上海": (31.23, 121.47),
    "济南": (36.65, 117.12),
}

CITY_ALIASES = {
    "beijing": "北京", "bj": "北京",
    "harbin": "哈尔滨", "heb": "哈尔滨",
    "shanghai": "上海", "sh": "上海",
    "jinan": "济南", "jn": "济南",
}

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code → 中文（只保留常见档位，够播报用）
_WMO = (
    (0, "晴"), (1, "大致晴朗"), (2, "多云"), (3, "阴"),
    (45, "有雾"), (48, "雾凇"),
    (51, "小毛毛雨"), (53, "毛毛雨"), (55, "大毛毛雨"),
    (56, "冻毛毛雨"), (57, "强冻毛毛雨"),
    (61, "小雨"), (63, "中雨"), (65, "大雨"),
    (66, "冻雨"), (67, "强冻雨"),
    (71, "小雪"), (73, "中雪"), (75, "大雪"), (77, "米雪"),
    (80, "阵雨"), (81, "强阵雨"), (82, "暴雨"),
    (85, "阵雪"), (86, "强阵雪"),
    (95, "雷阵雨"), (96, "雷阵雨伴冰雹"), (99, "强雷暴伴冰雹"),
)


def describe_code(code) -> str:
    """WMO 天气代码 → 中文描述（未知代码返回空串，由调用方兜底）"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ""
    for k, name in _WMO:
        if c == k:
            return name
    # 区间兜底
    if 1 <= c <= 3:
        return "多云"
    if 51 <= c <= 57:
        return "毛毛雨"
    if 61 <= c <= 67:
        return "雨"
    if 71 <= c <= 77:
        return "雪"
    if 80 <= c <= 82:
        return "阵雨"
    if 95 <= c <= 99:
        return "雷雨"
    return ""


def resolve_city(name: str, default: str = "") -> str:
    """城市名 → 预置城市；支持英文/简写；未收录返回默认值（可能是空串）"""
    raw = (name or "").strip()
    if not raw:
        return default
    if raw in CITIES:
        return raw
    hit = CITY_ALIASES.get(raw.lower())
    if hit:
        return hit
    # 允许"北京市"这种写法
    for city in CITIES:
        if raw.startswith(city) or city in raw:
            return city
    return default


def available_cities() -> list[str]:
    return list(CITIES)


@dataclass
class WeatherDaily:
    """一天的预报（字段可能缺失 → None，渲染时逐个判断）"""

    city: str
    date: str = ""
    code: Optional[int] = None
    desc: str = ""
    tmax: Optional[float] = None
    tmin: Optional[float] = None
    precip_prob: Optional[float] = None
    wind_max: Optional[float] = None          # km/h（Open-Meteo 默认单位）
    uv_max: Optional[float] = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """至少要有温度，否则这条播报没有意义"""
        return self.tmax is not None or self.tmin is not None


class WeatherProvider(Protocol):
    name: str

    async def fetch_daily(self, city: str) -> Optional[WeatherDaily]:
        ...


class OpenMeteoProvider:
    """Open-Meteo 日级预报（免 key）"""

    name = "open-meteo"

    def __init__(self, timeout: Optional[float] = None, client: Optional[httpx.AsyncClient] = None):
        self._timeout = timeout or Config.HTTP_TIMEOUT
        self._client = client

    def _params(self, lat: float, lon: float) -> dict:
        return {
            "latitude": lat,
            "longitude": lon,
            "daily": ",".join((
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "wind_speed_10m_max", "uv_index_max",
            )),
            "timezone": Config.TIMEZONE,
            "forecast_days": 2,          # 今天 + 明天（只播今天，多取一天便于排查）
            "wind_speed_unit": "kmh",
        }

    async def fetch_daily(self, city: str) -> Optional[WeatherDaily]:
        if city not in CITIES:
            logger.warning(f"[Weather] 未收录的城市: {city!r}（预置：{', '.join(CITIES)}）")
            return None
        lat, lon = CITIES[city]
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(_OPEN_METEO_URL, params=self._params(lat, lon))
            if resp.status_code != 200:
                logger.error(f"[Weather] Open-Meteo HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            data = resp.json()
        except httpx.TimeoutException:
            logger.error(f"[Weather] Open-Meteo 请求超时（{self._timeout}s，城市 {city}）")
            return None
        except Exception as e:
            logger.error(f"[Weather] Open-Meteo 请求失败: {e}")
            return None
        finally:
            if self._client is None:
                try:
                    await client.aclose()
                except Exception:
                    pass

        return self._parse(city, data)

    @staticmethod
    def _parse(city: str, data: dict) -> Optional[WeatherDaily]:
        daily = (data or {}).get("daily") or {}
        times = daily.get("time") or []
        if not times:
            logger.warning(f"[Weather] 返回里没有 daily.time（城市 {city}）")
            return None

        def _at(key: str, idx: int = 0):
            vals = daily.get(key) or []
            if idx < len(vals):
                return vals[idx]
            return None

        code = _at("weather_code")
        d = WeatherDaily(
            city=city,
            date=str(_at("time") or ""),
            code=int(code) if isinstance(code, (int, float)) else None,
            tmax=_at("temperature_2m_max"),
            tmin=_at("temperature_2m_min"),
            precip_prob=_at("precipitation_probability_max"),
            wind_max=_at("wind_speed_10m_max"),
            uv_max=_at("uv_index_max"),
            raw=daily,
        )
        d.desc = describe_code(d.code)
        if not d.ok:
            logger.warning(f"[Weather] 解析后没有温度数据（城市 {city}）")
            return None
        return d


def create_provider(name: str = "", **kwargs) -> WeatherProvider:
    """按配置造 provider（可插拔；将来加 qweather 就在这里分支）"""
    name = (name or Config.WEATHER_PROVIDER or "open-meteo").lower()
    if name in ("open-meteo", "openmeteo", "open_meteo"):
        return OpenMeteoProvider(**kwargs)
    logger.warning(f"[Weather] 未知 provider {name!r}，回退到 open-meteo")
    return OpenMeteoProvider(**kwargs)
