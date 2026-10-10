"""
SearchProvider 抽象基类
------------------------
所有搜索实现都继承这个类。
"""
from abc import ABC, abstractmethod


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[dict]:
        """
        执行搜索。

        Returns:
            list of {"title": str, "url": str, "snippet": str}
            空列表表示没有结果或搜索失败。
        """
        raise NotImplementedError
