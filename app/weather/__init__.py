"""天气模块（P2b）：provider + service（播报与 `/天气` 命令共用同一份逻辑）"""

from app.weather.provider import (
    CITIES,
    OpenMeteoProvider,
    WeatherDaily,
    WeatherProvider,
    available_cities,
    create_provider,
    describe_code,
    resolve_city,
)
from app.weather.service import WeatherService, fact_line, notes_for

__all__ = [
    "CITIES", "WeatherDaily", "WeatherProvider", "OpenMeteoProvider", "create_provider",
    "describe_code", "resolve_city", "available_cities",
    "WeatherService", "notes_for", "fact_line",
]
