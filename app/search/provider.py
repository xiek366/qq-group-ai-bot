"""
SearXNG 搜索 Provider
----------------------
SearXNG 是免费开源的元搜索引擎，不需要 API Key。
公共实例列表: https://searx.space/

返回统一格式:
  [{"title": "...", "url": "...", "snippet": "..."}, ...]

如果公共实例都不可用，可以在 .env 里配置 SEARXNG_URL 指向自己的实例。
"""

import logging
from typing import Optional

from app.config import Config
from app.search.base import SearchProvider

logger = logging.getLogger(__name__)

# 一些常见的公共 SearXNG 实例（会依次尝试）
# 来源: https://searx.space/ + https://alex.envs.net/searxng.html
# 选响应快、支持 JSON API 的
PUBLIC_INSTANCES = [
    "https://searx.travnewmatic.com",
    "https://search.inetol.net",
    "https://searx.ovh",
    "https://lantern.co.im",
    "https://searx.envs.net/searxng",
    "https://searxng.cz",
    "https://sx.vern.cc/searxng",
]


class SearXNGProvider(SearchProvider):
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or Config.SEARXNG_URL or "").rstrip("/")
        self.client = None  # 延迟初始化

    def _get_client(self):
        if self.client is None:
            import httpx  # 延迟导入
            self.client = httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT)
        return self.client

    async def search(self, query: str) -> list[dict]:
        # 先试用户配置的，再试公共实例
        urls_to_try = []
        if self.base_url:
            urls_to_try.append(self.base_url)
        urls_to_try.extend(u for u in PUBLIC_INSTANCES if u not in urls_to_try)

        for base_url in urls_to_try:
            results = await self._try_one(base_url, query)
            if results is not None:
                return results

        logger.warning("[Search] 所有 SearXNG 实例都失败了")
        # 抛异常让上层知道"服务不可用"，区别于"没搜着"
        raise RuntimeError("所有 SearXNG 实例均不可用")

    async def _try_one(self, base_url: str, query: str) -> Optional[list[dict]]:
        """尝试单个 SearXNG 实例，成功返回结果列表，失败返回 None"""
        import httpx  # 延迟导入
        client = self._get_client()

        try:
            resp = await client.get(
                f"{base_url}/search",
                params={
                    "q": query,
                    "format": "json",
                    "language": "zh-CN",
                    "categories": "general",
                },
            )
            if resp.status_code != 200:
                logger.debug(f"[Search] {base_url} HTTP {resp.status_code}")
                return None

            data = resp.json()
            raw_results = data.get("results", [])

            formatted = []
            for r in raw_results[:10]:
                formatted.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": (r.get("content") or "")[:300],
                })

            logger.info(f"[Search] {base_url} 查询='{query}' → {len(formatted)} 条结果")
            return formatted

        except httpx.TimeoutException:
            logger.debug(f"[Search] {base_url} 超时")
            return None
        except httpx.HTTPError as e:
            logger.debug(f"[Search] {base_url} HTTP错误: {e}")
            return None
        except Exception as e:
            logger.debug(f"[Search] {base_url} 解析错误: {e}")
            return None
