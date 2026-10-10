"""
LLM Provider 抽象基类
----------------------
所有 LLM 实现都继承这个类，保证接口统一。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        system_prompt: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        tool_call_back: Optional[Any] = None,
    ) -> str:
        """
        调用 LLM 聊天（支持 Function Calling）。

        Args:
            messages: OpenAI 格式的消息列表
            temperature: 0.0 ~ 2.0
            system_prompt: 可选的 system prompt
            tools: 可选的工具定义列表（OpenAI function calling 格式）
            tool_call_back: 可选的 callable，签名 async def(name, args) -> str
                当 LLM 请求调用工具时，provider 会调这个函数执行工具，
                然后把结果自动喂回 LLM 继续推理，直到 LLM 返回纯文本最终回复。
                如果为 None，遇到 tool_call 会抛异常。

        Returns:
            LLM 返回的最终纯文本
        """
        raise NotImplementedError

    @abstractmethod
    async def summarize_search(
        self,
        query: str,
        results: list[dict],
    ) -> str:
        """
        让 LLM 根据搜索结果回答用户问题。
        """
        raise NotImplementedError
