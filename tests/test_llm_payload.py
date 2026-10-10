"""
LLM 请求体 / 返回值处理回归测试
==============================
钉住线上踩过的坑（deepseek-flash 思考模式默认开启）：

  1. 看图的请求必须关掉思考模式，并且 max_tokens 不能太小
     —— 否则思维链把预算吃光，返回的 content 是空字符串
  2. 带 tools 的多轮请求，assistant 消息必须把 reasoning_content 一起回传
     —— 官方文档明确：不回传会 400
  3. content 为空时不能再静默返回 ''，要打出 finish_reason 等排查线索

运行: python tests/test_llm_payload.py
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.llm.provider import OpenAICompatibleProvider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    """记录每次请求体，按顺序返回预设响应"""

    def __init__(self, responses: list[FakeResponse]):
        self.payloads: list[dict] = []
        self.timeouts: list = []
        self._responses = list(responses)

    async def post(self, url, json=None, headers=None, timeout=None):
        # 真接口带 per-call timeout（P2c-hotfix-4），假客户端要跟上，否则测不到真实链路
        self.payloads.append(json)
        self.timeouts.append(timeout)
        return self._responses.pop(0)


def _provider_with(responses: list[FakeResponse]) -> tuple[OpenAICompatibleProvider, FakeClient]:
    p = OpenAICompatibleProvider(api_key="test", base_url="https://api.deepseek.com", model="deepseek-flash")
    client = FakeClient(responses)
    p._get_client = lambda: client      # type: ignore[method-assign]
    return p, client


async def test_vision_disables_thinking_and_has_budget():
    """看图请求：thinking=disabled + max_tokens 不低于配置值"""
    Config.VISION_THINKING_DISABLED = True
    resp = FakeResponse({
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "一只橘猫趴在键盘上。"},
        }],
        "usage": {"prompt_tokens": 249, "completion_tokens": 20},
    })
    p, client = _provider_with([resp])

    out = await p.chat_with_images(["https://example.com/cat.jpg"])
    assert out == "一只橘猫趴在键盘上。", out

    payload = client.payloads[0]
    assert payload["thinking"] == {"type": "disabled"}, payload.get("thinking")
    assert payload["max_tokens"] >= 512, payload["max_tokens"]
    # 图片确实以 image_url 块传进去了
    blocks = payload["messages"][0]["content"]
    assert any(b["type"] == "image_url" for b in blocks), blocks
    print(f"   vision payload: thinking={payload['thinking']} max_tokens={payload['max_tokens']}")
    print("✅ test_vision_disables_thinking_and_has_budget PASS")


async def test_vision_retries_when_truncated():
    """预算被思维链吃光（finish_reason=length + 空 content）→ 自动放宽预算重试"""
    Config.VISION_THINKING_DISABLED = True
    truncated = FakeResponse({
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "", "reasoning_content": "让我想想……" * 20},
        }],
        "usage": {"prompt_tokens": 249, "completion_tokens": 512},
    })
    ok = FakeResponse({
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "图里是一只猫。"},
        }],
        "usage": {"prompt_tokens": 249, "completion_tokens": 30},
    })
    p, client = _provider_with([truncated, ok])

    out = await p.chat_with_images(["https://example.com/cat.jpg"])
    assert out == "图里是一只猫。", out
    assert len(client.payloads) == 2, "截断后没有重试"
    assert client.payloads[1]["max_tokens"] > client.payloads[0]["max_tokens"], "重试没有放宽预算"
    print(f"   重试预算: {client.payloads[0]['max_tokens']} → {client.payloads[1]['max_tokens']}")
    print("✅ test_vision_retries_when_truncated PASS")


async def test_vision_drops_thinking_on_400():
    """网关不认 thinking 字段返回 400 → 去掉该字段再试一次（兼容第三方中转）"""
    Config.VISION_THINKING_DISABLED = True
    bad = FakeResponse({"error": {"message": "unknown field: thinking"}}, status_code=400)
    ok = FakeResponse({
        "choices": [{"finish_reason": "stop", "message": {"content": "看到了。"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
    })
    p, client = _provider_with([bad, ok])

    out = await p.chat_with_images(["https://example.com/cat.jpg"])
    assert out == "看到了。", out
    assert "thinking" in client.payloads[0], "第一次应该带 thinking"
    assert "thinking" not in client.payloads[1], "400 后应该去掉 thinking 重试"
    print("✅ test_vision_drops_thinking_on_400 PASS")


async def test_tool_loop_echoes_reasoning_content():
    """工具调用轮次必须把 reasoning_content 回传，否则官方 API 会 400"""
    Config.LLM_THINKING = "enabled"
    Config.LLM_MAX_TOKENS = 2048

    round1 = FakeResponse({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "用户问天气，我需要调搜索工具",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"expression": "1+1"}'},
                }],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    })
    round2 = FakeResponse({
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "等于 2"}}],
        "usage": {"prompt_tokens": 150, "completion_tokens": 10},
    })
    p, client = _provider_with([round1, round2])

    async def tool_back(name, args):
        return "计算结果: 2"

    out = await p.chat(
        messages=[{"role": "user", "content": "1+1 等于几"}],
        tools=[{"type": "function", "function": {"name": "calc", "parameters": {}}}],
        tool_call_back=tool_back,
    )
    assert out == "等于 2", out
    assert len(client.payloads) == 2, "工具循环没有进入第二轮"

    msgs = client.payloads[1]["messages"]
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    assert assistant_msg.get("reasoning_content") == "用户问天气，我需要调搜索工具", assistant_msg
    assert any(m.get("role") == "tool" for m in msgs), msgs
    print(f"   第二轮 assistant 消息字段: {sorted(assistant_msg.keys())}")
    print("✅ test_tool_loop_echoes_reasoning_content PASS")


async def test_empty_content_is_logged_not_silent():
    """content 为空时要有排查线索（finish_reason / reasoning_len）"""
    import io

    Config.LLM_THINKING = "enabled"
    resp = FakeResponse({
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "", "reasoning_content": "想很久"},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2048},
    })
    p, _ = _provider_with([resp])

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    llm_logger = logging.getLogger("app.llm.provider")
    llm_logger.addHandler(handler)
    try:
        out = await p.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        llm_logger.removeHandler(handler)

    assert out == "", repr(out)
    text = stream.getvalue()
    assert "finish_reason=length" in text, text
    assert "reasoning_len=3" in text, text
    print(f"   日志: {text.strip().splitlines()[-1][:120]}")
    print("✅ test_empty_content_is_logged_not_silent PASS")


async def test_provider_records_metrics():
    """【P0-4】真实 provider 的埋点：成功与失败都要记进指标

    干跑用的是桩模型（不经过 provider），所以这条闭环必须在这里证明。
    """
    import shutil
    from pathlib import Path

    from app import metrics as metrics_mod

    tmp = Path("data/_tmp_llm_metrics")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    metrics_mod._instance = metrics_mod.Metrics(path=tmp / "m.json", save_every=999)

    try:
        ok_resp = FakeResponse({
            "choices": [{"finish_reason": "stop", "message": {"content": "好的"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })
        p, _ = _provider_with([ok_resp])
        assert await p.chat(messages=[{"role": "user", "content": "hi"}]) == "好的"

        bad = FakeResponse({"error": "boom"}, status_code=500)
        p2, _ = _provider_with([bad])
        try:
            await p2.chat(messages=[{"role": "user", "content": "hi"}])
            raise AssertionError("500 应该抛异常")
        except RuntimeError:
            pass

        snap = metrics_mod.get().snapshot()
        llm = snap["llm"]
        assert llm["calls"] == 2, llm
        assert llm["failures"] == 1, llm
        assert llm["tokens_in"] == 120 and llm["tokens_out"] == 30, llm

        # 看图也是 LLM 调用（要花 token），必须同样计入 llm.* ——
        # 否则面板上的"调用总数/延迟/token 用量"会漏掉视觉那条线
        vision_resp = FakeResponse({
            "choices": [{"finish_reason": "stop", "message": {"content": "一只猫"}}],
            "usage": {"prompt_tokens": 249, "completion_tokens": 26},
        })
        p3, _ = _provider_with([vision_resp])
        assert await p3.chat_with_images(["data:image/png;base64,AAAA"]) == "一只猫"
        llm2 = metrics_mod.get().snapshot()["llm"]
        assert llm2["calls"] == 3, f"看图没计入 LLM 调用: {llm2}"
        assert llm2["tokens_in"] == 120 + 249 and llm2["tokens_out"] == 30 + 26, llm2

        print(f"   provider 埋点：calls={llm2['calls']} failures={llm2['failures']} "
              f"tokens={llm2['tokens_in']}/{llm2['tokens_out']}（含一次看图）")
        print("✅ test_provider_records_metrics PASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        metrics_mod._instance = None


async def _run_all():
    await test_vision_disables_thinking_and_has_budget()
    await test_vision_retries_when_truncated()
    await test_vision_drops_thinking_on_400()
    await test_tool_loop_echoes_reasoning_content()
    await test_empty_content_is_logged_not_silent()
    await test_provider_records_metrics()


if __name__ == "__main__":
    print("=== LLM 请求体/返回值处理回归测试 ===\n")
    asyncio.run(_run_all())
    print("\n🎉 全部通过：看图不再返回空描述，工具调用会回传 reasoning_content")
