"""
OpenAI-compatible LLM Provider
-------------------------------
支持 DeepSeek、GPT、Qwen、GLM 等任何兼容 OpenAI API 的模型。
通过 httpx 调用，带超时、重试、日志。

支持 Function Calling（tool_calls）：
  1. 传 tools=[{type: "function", function: {...}}]
  2. 传 tool_call_back=async def(name, args) -> str
  3. provider 自动执行 tool → 喂回结果 → 继续推理，直到 LLM 返回最终文本

关于「思考模式」（DeepSeek 等模型默认开启）：
  * 思维链（reasoning_content）与正式回答（content）**共用同一份 max_tokens**，
    预算给小了就会出现「思考没写完 → content 为空」，所以 max_tokens 要留够。
  * 思考模式下 temperature 不生效（官方明确不支持）。
  * 带 tools 的请求，后续轮次必须把 reasoning_content 回传，否则 400。
  以上三点由下面的 thinking_params() 和 tool 循环里的回传逻辑兜住。
"""

import json
import logging
import time
from typing import Any, Optional

import httpx

from app.config import Config
from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Agent loop 最大轮次（防止 LLM 无限调用工具）
_MAX_TOOL_ROUNDS = 5


def _record_llm(ok: bool, latency: float = 0.0, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """【P0-4】记 LLM 调用指标（事件循环里调用 → metrics 只改内存、不落盘）"""
    try:
        from app import metrics
        metrics.get().record_llm(ok, latency, tokens_in, tokens_out)
    except Exception:      # 指标不该影响主流程
        pass


def thinking_params(disabled: bool = False) -> dict[str, Any]:
    """构造思考模式相关参数（直接放进请求体，不是 SDK 的 extra_body）。

    disabled=True → 关闭思考模式（看图等只需一段输出的场景）。
    Config.LLM_THINKING=disabled → 全局关闭（群聊更省钱、更快，温度也能生效）。
    """
    if disabled or Config.LLM_THINKING == "disabled":
        return {"thinking": {"type": "disabled"}}
    params: dict[str, Any] = {}
    if Config.LLM_REASONING_EFFORT:
        params["reasoning_effort"] = Config.LLM_REASONING_EFFORT
    return params


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = (base_url or Config.LLM_BASE_URL).rstrip("/")
        self.model = model or Config.LLM_MODEL
        self.client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=Config.HTTP_TIMEOUT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self.client

    # ---------- chat（带 Agent loop）----------
    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        system_prompt: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        tool_call_back: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """`timeout`：本次调用的超时秒数（不传用全局 `HTTP_TIMEOUT`）。

        【P2c-hotfix-4】摘要任务一次要喂 40+ 条消息，比闲聊大得多，
        沿用 15 秒会稳定超时 —— 这类"大请求"可以自己放宽。
        """
        # 处理 system_prompt
        working_messages = list(messages)
        if system_prompt:
            # 如果 messages 里第一个已经是 system，替换；否则插到最前
            if working_messages and working_messages[0].get("role") == "system":
                working_messages[0] = {"role": "system", "content": system_prompt}
            else:
                working_messages.insert(0, {"role": "system", "content": system_prompt})

        client = self._get_client()
        url = f"{self.base_url}/chat/completions"

        # Agent loop —— 最多 _MAX_TOOL_ROUNDS 轮
        for round_idx in range(_MAX_TOOL_ROUNDS):
            # 构造 payload
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": working_messages,
                "temperature": temperature,
                "max_tokens": Config.LLM_MAX_TOKENS,
            }
            payload.update(thinking_params())
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            # 发请求
            start = time.time()
            eff_timeout = float(timeout or Config.HTTP_TIMEOUT)
            try:
                resp = await client.post(url, json=payload, timeout=eff_timeout)
                elapsed = time.time() - start
            except httpx.TimeoutException:
                logger.error(f"[LLM] 请求超时 ({eff_timeout:g}s)")
                _record_llm(False)
                raise RuntimeError("LLM API 超时")
            except httpx.HTTPError as e:
                logger.error(f"[LLM] HTTP 错误: {e}")
                _record_llm(False)
                raise RuntimeError(f"LLM HTTP 错误: {e}")

            if resp.status_code != 200:
                logger.error(f"[LLM] HTTP {resp.status_code}: {resp.text[:300]}")
                _record_llm(False)
                raise RuntimeError(f"LLM API 返回 HTTP {resp.status_code}")

            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]

            usage = data.get("usage", {})
            # 【P0-4】记一次成功调用（只改内存，不落盘）
            _record_llm(
                True, elapsed,
                usage.get("prompt_tokens") or 0,
                usage.get("completion_tokens") or 0,
            )
            logger.info(
                f"[LLM] model={self.model} elapsed={elapsed:.2f}s "
                f"tokens_in={usage.get('prompt_tokens')} "
                f"tokens_out={usage.get('completion_tokens')}"
            )

            # 检查是否 tool_calls
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # === 最终文本回复 ===
                content = message.get("content", "") or ""
                if isinstance(content, list):  # 少数网关会返回 block 数组
                    content = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                    )
                content = content.strip()
                if not content:
                    # content 为空时把关键信息打全，否则只有一句"空"没法排查：
                    # finish_reason=length → max_tokens 被思维链吃光（最常见）
                    # reasoning_content 有内容 → 模型还在思考就被截断了
                    reasoning = message.get("reasoning_content") or ""
                    logger.warning(
                        f"[LLM] content 为空！finish_reason={choice.get('finish_reason')} "
                        f"max_tokens={Config.LLM_MAX_TOKENS} "
                        f"tokens_out={usage.get('completion_tokens')} "
                        f"thinking={Config.LLM_THINKING} "
                        f"reasoning_len={len(reasoning)} message_keys={list(message.keys())}"
                    )
                if len(content) > Config.MAX_REPLY_LENGTH:
                    content = content[: Config.MAX_REPLY_LENGTH - 3] + "..."
                return content

            # === LLM 请求调用工具 ===
            if tool_call_back is None:
                raise RuntimeError(
                    f"LLM 请求调用工具但未提供 tool_call_back: "
                    f"{[tc.get('function', {}).get('name') for tc in tool_calls]}"
                )

            # 把 assistant 的 tool_calls 消息加入对话历史。
            # ★ 带 tools 的请求在思考模式下必须把 reasoning_content 一起回传，
            #   否则下一轮 API 会直接返回 400（官方文档：思考模式 / 工具调用）。
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content", "") or "",
                "tool_calls": tool_calls,
            }
            if message.get("reasoning_content"):
                assistant_msg["reasoning_content"] = message["reasoning_content"]
            working_messages.append(assistant_msg)

            # 逐个执行 tool_call
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func_info = tc.get("function", {})
                func_name = func_info.get("name", "")
                try:
                    func_args = json.loads(func_info.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    func_args = {}

                logger.info(f"[LLM] 调工具: {func_name} args={func_args}")

                # 执行回调
                try:
                    tool_result = await tool_call_back(func_name, func_args)
                except Exception as e:
                    logger.error(f"[LLM] 工具 {func_name} 执行失败: {e}")
                    tool_result = f"工具执行失败: {e}"

                # 把工具结果加入对话
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": str(tool_result),
                })

            # 继续下一轮 loop，让 LLM 根据工具结果生成最终回复

        # 超过最大轮次 —— 兜底
        logger.warning(f"[LLM] 超过 {_MAX_TOOL_ROUNDS} 轮 tool loop，兜底")
        return "让我想想……（思考轮次太多，暂时没答案 😣）"

    # ---------- summarize_search ----------
    async def summarize_search(self, query: str, results: list[dict]) -> str:
        result_texts = []
        for i, r in enumerate(results[:8], 1):
            result_texts.append(
                f"[{i}] {r.get('title', '无标题')}\n"
                f"    URL: {r.get('url', '')}\n"
                f"    摘要: {r.get('snippet', '')[:200]}"
            )
        result_block = "\n\n".join(result_texts)

        prompt = f"""以下内容来自搜索结果。请根据搜索结果回答用户问题。

要求：
1. 不要编造搜索结果中不存在的信息。
2. 如果搜索结果不足以回答问题，明确说明。
3. 回答尽量简洁（100~300 字）。
4. 回答末尾列出参考来源的编号，格式：来源：1、3

--- 搜索结果 ---
{result_block}
--- 搜索结果结束 ---

用户问题：{query}"""

        messages = [{"role": "user", "content": prompt}]
        return await self.chat(messages, temperature=0.3)

    # ---------- 图像理解 ----------
    async def chat_with_images(
        self,
        image_urls: list[str],
        prompt: str = "请用 2~3 句话描述这张图片的内容。如果图片里有文字请也识别出来。",
        temperature: float = 0.5,
    ) -> str:
        """
        让 deepseek-flash 看图并返回描述。

        Args:
            image_urls: OneBot image segment 里的 url 列表（模型会自己下载）
            prompt: 给视觉模型的指令
            temperature: 随机性（思考模式关闭时才真正生效）

        Returns:
            图片描述文本（失败时返回空字符串）
        """
        if not image_urls:
            return ""
        # deepseek-flash 支持 content 数组：text + image_url
        content_blocks: list[dict] = [{"type": "text", "text": prompt}]
        for url in image_urls[:6]:  # 最多看 6 张图
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "low"},
            })

        messages = [{"role": "user", "content": content_blocks}]
        url = f"{self.base_url}/chat/completions"

        # 看图只要一段描述，不需要思维链：
        # 思考模式下思维链会吃掉 max_tokens，导致 content 为空（曾线上踩过），
        # 所以默认把思考关掉，并把预算放宽。
        def build_payload(disable_thinking: bool) -> dict:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": Config.VISION_MAX_TOKENS,
            }
            if disable_thinking:
                payload["thinking"] = {"type": "disabled"}
            return payload

        disable_thinking = Config.VISION_THINKING_DISABLED
        resp = await self._post_vision(url, build_payload(disable_thinking), disable_thinking)

        # 有些网关不认 thinking 字段会返回 400 → 去掉再试一次
        if resp is not None and resp.status_code == 400 and disable_thinking:
            logger.warning("[Vision] 请求带 thinking 参数被拒（400），去掉后重试一次")
            resp = await self._post_vision(url, build_payload(False), False)

        if resp is None:
            return ""
        if resp.status_code != 200:
            logger.error(f"[Vision] HTTP {resp.status_code}: {resp.text[:300]}")
            return ""

        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        content_field = message.get("content", "") or ""
        # 多模态 API 有时 content 是 list 格式！
        if isinstance(content_field, list):
            description = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content_field
            ).strip()
            logger.warning(f"[Vision] content 是 list 格式！提取后: {description[:80]}")
        else:
            description = content_field.strip()

        usage = data.get("usage", {})
        logger.info(
            f"[Vision] model={self.model} imgs={len(image_urls)} "
            f"elapsed={getattr(resp, '_elapsed', 0):.2f}s "
            f"tokens={usage.get('prompt_tokens')}/{usage.get('completion_tokens')} "
            f"finish={choice.get('finish_reason')} thinking_disabled={disable_thinking}"
        )

        if not description:
            # 把真正有用的线索打全：finish_reason=length 说明预算又被吃光了，
            # reasoning_len>0 说明模型还在思考，message_keys 能看出网关多返回了哪些字段
            reasoning = message.get("reasoning_content") or ""
            logger.warning(
                f"[Vision] description 为空！finish_reason={choice.get('finish_reason')} "
                f"tokens_out={usage.get('completion_tokens')}/{Config.VISION_MAX_TOKENS} "
                f"reasoning_len={len(reasoning)} message_keys={list(message.keys())}"
            )
            # 还有预算被思维链吃光的可能（比如网关忽略了 thinking=disabled）→ 翻倍再试一次
            if choice.get("finish_reason") == "length" and usage.get("completion_tokens"):
                logger.warning("[Vision] 疑似被 max_tokens 截断，放宽预算重试一次")
                retry_payload = build_payload(True)
                retry_payload["max_tokens"] = Config.VISION_MAX_TOKENS * 2
                resp2 = await self._post_vision(url, retry_payload, True)
                if resp2 is not None and resp2.status_code == 200:
                    data2 = resp2.json()
                    msg2 = data2["choices"][0]["message"]
                    field2 = msg2.get("content", "") or ""
                    if isinstance(field2, list):
                        field2 = " ".join(
                            b.get("text", "") if isinstance(b, dict) else str(b) for b in field2
                        )
                    description = field2.strip()
                    if description:
                        logger.info(f"[Vision] 重试成功，描述长度 {len(description)}")

        return description.strip()

    async def _post_vision(self, url: str, payload: dict, disable_thinking: bool):
        """发一次视觉请求；网络异常返回 None

        【P0-4】每一次真实的 HTTP 尝试都在这里记一次 LLM 指标 ——
        看图也是 LLM 调用（要花 token），不记的话面板上的"调用总数/延迟/token 用量"会偏小。
        注意这个方法会被重试逻辑调用多次，所以计数口径是"每次尝试"。
        """
        start = time.time()
        try:
            client = self._get_client()
            resp = await client.post(url, json=payload)
        except httpx.TimeoutException:
            logger.error(f"[Vision] 请求超时 ({Config.HTTP_TIMEOUT}s)")
            _record_llm(False)
            return None
        except httpx.HTTPError as e:
            logger.error(f"[Vision] HTTP 错误: {e}")
            _record_llm(False)
            return None
        elapsed = time.time() - start
        if resp.status_code == 200:
            try:
                usage = resp.json().get("usage", {}) or {}
            except Exception:
                usage = {}
            _record_llm(
                True, elapsed,
                usage.get("prompt_tokens") or 0,
                usage.get("completion_tokens") or 0,
            )
        else:
            _record_llm(False)
        try:
            resp._elapsed = elapsed                # 给日志用，不改 httpx 对象语义
        except Exception:
            pass
        return resp
