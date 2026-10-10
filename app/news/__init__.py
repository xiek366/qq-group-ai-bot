"""资讯模块（P2c）：RSS + B站专栏 → 统一 NewsItem；频控/去重/配图下载"""

from app.news.image import download_image, to_cq_image
from app.news.models import NewsItem, SourceState
from app.news.service import NewsService
from app.news.sources import (
    PRESET_BILI,
    PRESET_RSS,
    BilibiliArticleSource,
    NewsSource,
    RssSource,
    build_sources,
    parse_bilibili_articles,
    parse_rss,
)

__all__ = [
    "NewsItem", "SourceState", "NewsService",
    "NewsSource", "RssSource", "BilibiliArticleSource", "build_sources",
    "parse_rss", "parse_bilibili_articles", "PRESET_RSS", "PRESET_BILI",
    "download_image", "to_cq_image",
]
