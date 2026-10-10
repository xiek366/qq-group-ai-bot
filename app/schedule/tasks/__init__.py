"""P2a/P2b/P2c 任务实现（每个任务一个文件；新增任务只需在这里加文件 + 注册一行）"""

from app.schedule.tasks.daily_image import DailyImageTask
from app.schedule.tasks.daily_quote import DailyQuoteTask, should_generate_today
from app.schedule.tasks.icebreak import IcebreakTask, make_icebreak_source, make_rotating_source
from app.schedule.tasks.news_digest import NewsDigestTask, format_news
from app.schedule.tasks.summary_job import SummaryJob
from app.schedule.tasks.water_reminder import WaterReminderTask, pick_text
from app.schedule.tasks.weather_morning import WeatherMorningTask

__all__ = [
    "IcebreakTask", "make_icebreak_source", "make_rotating_source", "SummaryJob",
    "WeatherMorningTask", "WaterReminderTask", "pick_text",
    "NewsDigestTask", "format_news", "DailyQuoteTask", "should_generate_today", "DailyImageTask",
]
