"""
P0 端到端干跑验证
=================
把 P0 的四件事串起来跑一遍（**不联网、不发真实群消息**，全部用桩件）：

  1. 群聊消息 → 统一对话流（含命令、bot 自己的发言）
  2. 看图：VisionPipeline（桩下载器 + 桩模型）→ 描述入流 → 第二次发同一张图命中缓存
  3. 指标：每群消息数 / bot 发言数 / LLM 调用 / 看图缓存命中
  4. 面板 `/api/health` 真起一个临时 server 请求（验证 JSON 结构可用）

判定标准就是 DEVLOG 里 P0 的验收标准 1~5（第 6 条是单测全绿，另行执行）。

运行: python tests/test_p0_e2e.py
（必须在项目根目录运行；全部数据写临时目录，不碰生产数据）
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_isolate import isolate_writable_state, reset_writable_state
from app.bot.handler import MessageHandler
from app.bot.message import parse_message
from app.config import Config
from app.context.stream import ConversationStream
from app.memory.memory import GroupMemory
from app.util.blocking import run_blocking, shutdown_blocking
from app.vision.cache import VisionCache
from app.vision.pipeline import VisionPipeline

TEST_GID = 100000020
TMP = Path("data/_tmp_p0_e2e")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
CAT_URL = "https://example.com/cat.png"
DOG_URL = "https://example.com/dog.png"
IMG_BYTES = {CAT_URL: PNG, DOG_URL: PNG + b"\x01"}


# ---------- 桩件 ----------

class StubOneBot:
    ws_connected = True

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_group_message(self, group_id, text):
        self.sent.append((group_id, text))

    async def get_group_members(self, group_id):
        return []


class StubLLM:
    """同时充当聊天模型与视觉模型，记录调用次数与最后一次的 system prompt"""

    def __init__(self):
        self.chat_calls = 0
        self.vision_calls = 0
        self.last_system_prompt = ""

    async def chat(self, messages, system_prompt="", **kwargs):
        self.chat_calls += 1
        self.last_system_prompt = system_prompt or ""
        return "收到啦～"

    async def chat_with_images(self, image_urls, prompt="", **kwargs):
        self.vision_calls += 1
        return "一只橘猫趴在键盘上，旁边有一杯咖啡"


def _event(segments, gid=TEST_GID):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": gid,
        "message_id": 1,
        "message": segments,
        "sender": {"user_id": 888888, "nickname": "小明"},
    }


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _image(url):
    return {"type": "image", "data": {"url": url, "file": "x.png", "sub_type": 0, "summary": ""}}


async def main():
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    # ★ 必须在任何消息之前关掉"主动插嘴"，否则普通群聊会随机触发 proactive 回复，
    #   让下面的断言变得偶发失败（第一版就踩了这个坑）
    Config.CHAT_REPLY_CHANCE = 0

    # 指标单例 / 风格画像都指向临时文件（否则会写进真实的 data/stats、data/group_style）
    isolate_writable_state(TMP)

    onebot = StubOneBot()
    llm = StubLLM()
    handler = MessageHandler(onebot=onebot, llm_provider=llm, search_provider=None,
                             memory=GroupMemory(persist=False, stream=ConversationStream(
                                 stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")),
                             persona="测试人设")
    if handler._scheduler_task:
        handler._scheduler_task.cancel()
    # 视觉管道换成桩：不联网
    handler.vision = VisionPipeline(
        cache=VisionCache(cache_dir=TMP / "vcache", ttl_days=30),
        fetcher=lambda u: _fetch(u),
    )

    async def feed(segments):
        onebot.sent.clear()
        parsed = parse_message(_event(segments))
        assert parsed, f"解析失败: {segments}"
        await handler.handle(parsed)
        return list(onebot.sent)

    stream = handler.memory.stream

    # ---------- 1) 普通群聊（没 @bot，也应该进对话流）----------
    await feed([_text("大家早上好")])
    entries = stream.window(TEST_GID, limit=20)
    assert entries and entries[0]["kind"] == "chat", entries
    assert entries[0]["nick"] == "小明" and entries[0]["user_id"] == 888888
    assert entries[0]["text"] == "大家早上好"
    # 已关掉主动插嘴 → 这条不该引来 bot 发言
    assert all(e["role"] == "user" for e in entries), entries
    print("   ① 普通群聊已入流（kind=chat），且未触发主动插嘴")

    # ---------- 2) @bot 聊天：用户消息 + bot 回复都要在 ----------
    await feed([{"type": "at", "data": {"qq": str(Config.BOT_QQ)}}, _text("你好")])
    entries = stream.window(TEST_GID, limit=20)
    kinds = [e["kind"] for e in entries]
    assert kinds == ["chat", "chat", "reply"], kinds
    assert entries[2]["role"] == "assistant" and entries[2]["text"] == "收到啦～", entries[2]
    print("   ② @bot 一问一答：用户消息 + bot 回复都在流里（kind=reply）")

    # ---------- 3) 看图：描述入流 + 回复入流 ----------
    await feed([_text("/看"), _image(CAT_URL)])
    entries = stream.window(TEST_GID, limit=20)
    vision_user = [e for e in entries if e["kind"] == "vision" and e["role"] == "user"]
    assert vision_user, f"图片识别结果没入流: {[e['kind'] for e in entries]}"
    assert vision_user[-1]["text"].startswith("[图片识别结果] 一只橘猫"), vision_user[-1]
    assert llm.vision_calls == 1, llm.vision_calls
    assert any("收到啦" in t for _, t in onebot.sent), onebot.sent
    print("   ③ 看图：描述进对话流（kind=vision），回复正常发出")

    # ---------- 4) 同一张图再发一次 → 命中缓存，不再请求模型 ----------
    await feed([_text("/看"), _image(CAT_URL)])
    assert llm.vision_calls == 1, f"缓存没生效，看图被调用了 {llm.vision_calls} 次"
    print("   ④ 同一张图第二次发：命中缓存（视觉模型调用次数仍是 1）")

    # 换一张图应该重新请求
    await feed([_text("/看"), _image(DOG_URL)])
    assert llm.vision_calls == 2, llm.vision_calls
    print("   ⑤ 换一张新图：正常发起新请求")

    # ---------- 5) 命令也入流 ----------
    await feed([_text("/ping")])
    entries = stream.window(TEST_GID, limit=40)
    assert any(e["kind"] == "command" and e["text"] == "/ping" for e in entries), entries[-3:]
    print("   ⑥ 命令入流（kind=command）")

    # 模拟"被回应"：bot 发言后 10 分钟内有人说话
    total_bot = sum(1 for e in entries if e["role"] == "assistant")
    assert total_bot >= 1, total_bot

    # ---------- 6) 指标 ----------
    from app import metrics
    snap = metrics.get().snapshot()
    g = next(x for x in snap["groups"] if x["group_id"] == str(TEST_GID))
    assert g["user_msgs"] >= 5 and g["bot_msgs"] >= 3, g
    # 注意：这里用的是**桩模型**，不经过真实 provider 的 _record_llm 埋点，
    # 所以 llm.calls 保持 0 是正常的 —— provider → metrics 的埋点由
    # tests/test_llm_payload.py::test_provider_records_metrics 单独证明。
    assert snap["llm"]["calls"] == 0, snap["llm"]
    assert snap["vision"]["calls"] == 3 and snap["vision"]["cache_hits"] == 1, snap["vision"]
    print(f"   ⑦ 指标：群消息 {g['user_msgs']} / bot {g['bot_msgs']} / "
          f"看图 {snap['vision']['calls']}（命中 {snap['vision']['cache_hits']}）"
          f" ｜ llm.calls=0（桩模型，埋点在 provider 层另行验证）")

    # ---------- 7) 面板健康卡数据（不依赖 aiohttp，本地可测）----------
    from app.dashboard.health import build_health_payload, find_group

    payload = await build_health_payload(handler)
    assert "metrics" in payload and "streams" in payload, list(payload.keys())
    assert payload["stream_total_bytes"] > 0, payload["stream_total_bytes"]
    assert any(int(s["group_id"]) == TEST_GID for s in payload["streams"]), payload["streams"]
    pg = find_group(payload, TEST_GID)
    assert pg and pg["user_msgs"] >= 5, pg
    print(f"   ⑧ 健康卡数据：群消息 {pg['user_msgs']} / bot {pg['bot_msgs']} / "
          f"对话流 {payload['stream_total_bytes']}B / 每群 {len(payload['streams'])} 条")

    # 迁移进来的历史（直接写文件、不经过 append → 不计入指标）也必须出现在"每群"卡片里，
    # 否则面板会对一个明明有对话的群显示"暂无数据"
    ghost_gid = TEST_GID + 999
    ghost = TMP / "stream" / f"{ghost_gid}.jsonl"
    ghost.write_text(
        json.dumps({"ts": 1.0, "role": "user", "kind": "chat", "text": "迁移来的历史"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    payload_ghost = await build_health_payload(handler)
    g2 = find_group(payload_ghost, ghost_gid)
    assert g2 and g2["stream_entries"] == 1, f"迁移群没出现在健康卡里: {g2}"
    ghost.unlink()
    print(f"   ⑧b 只有对话流、没有指标的群（如迁移来的历史）也会列出："
          f"group={ghost_gid} stream_entries={g2['stream_entries']}")

    # ---------- 9) HTTP 层：优先用真 aiohttp；本环境装不了就用测试替身验证"接线" ----------
    Config.DASHBOARD_TOKEN = "e2e-token"
    try:
        from aiohttp.test_utils import TestClient, TestServer     # noqa: F401
        has_aiohttp = True
    except ModuleNotFoundError:
        has_aiohttp = False

    if has_aiohttp:
        from app.dashboard.server import build_app
        app = build_app(onebot, handler, 0.0)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/health?token=e2e-token")
            assert resp.status == 200, resp.status
            data = await resp.json()
            assert data["metrics"]["vision"]["cache_hits"] == 1, data["metrics"]["vision"]
            bad = await client.get("/api/health?token=wrong")
            assert bad.status == 401, bad.status
        print("   ⑨ HTTP 层（真实 aiohttp）：/api/health → 200，错误 token → 401")
    else:
        await _check_dashboard_wiring(onebot, handler)
        print("   ⑨ HTTP 层（测试替身）：/api/health 路由已注册、token 鉴权正确、payload 透传")
        print("      ⚠️ 替身只验证「我的接线」，不验证 aiohttp 本身 → 服务器上仍需 curl 复核：")
        print("         curl -s 'http://127.0.0.1:8080/api/health?token=$DASHBOARD_TOKEN'")

    # ---------- 8) 重启后不丢（重新读同一份流）----------
    stream2 = ConversationStream(stream_dir=TMP / "stream", legacy_memory_dir=TMP / "none")
    win = stream2.window(TEST_GID, limit=100)
    assert len(win) == len(stream.window(TEST_GID, limit=100)) > 0
    msgs = ConversationStream.to_messages(win)
    assert msgs and msgs[0]["role"] == "user" and msgs[0]["content"].startswith("小明: ")
    print("   ⑩ 重新读取同一份对话流：窗口与 messages 正确（模拟重启）")

    # ---------- 9) 看图失败：必须给"没看清"的话术，且绝不猜内容（验收标准 3）----------
    before = len(stream.window(TEST_GID, limit=200))
    # 用一个下载不到的新 URL → 管道 all_failed → handler 走 vision_failed 分支
    await feed([_text("/看"), _image("https://example.com/broken.png")])
    after_entries = stream.window(TEST_GID, limit=200)
    assert not any(e["text"].startswith("[图片识别结果]") for e in after_entries[before:]), \
        "识别失败却写入了图片描述"
    # 【P1-5】文案改由 config/scenes/image_fail.txt 提供，所以断言"意图"而不是旧措辞：
    # 必须①要求如实说没看清/再发一次 ②明令不许猜内容
    assert "没看清" in llm.last_system_prompt and "再发一次" in llm.last_system_prompt, \
        llm.last_system_prompt[-260:]
    assert "不要猜测" in llm.last_system_prompt or "不要凭空猜测" in llm.last_system_prompt, \
        llm.last_system_prompt[-260:]
    # 看图场景不该混入 chat 场景说明（曾经抓到互相干扰）
    assert "有人在群里 @ 你" not in llm.last_system_prompt, "看图时不该注入 chat 场景说明"
    assert onebot.sent, "失败了也该给用户一句实话，而不是静默"
    print("   ⑪ 看图失败：提示词明确要求「如实说没看清、不许猜内容」，且未写入假描述")

    print("\n🎉 P0 干跑验证通过（统一对话流 / 看图缓存与降级 / 指标 / 健康卡）")


async def _fetch(url):
    await asyncio.sleep(0)
    return IMG_BYTES.get(url)


async def _check_dashboard_wiring(onebot, handler):
    """用最小 aiohttp 替身验证面板接线。

    为什么需要它：本机 `qqbot` 环境没装 aiohttp，而这个会话的沙箱**不允许装包**
    （pip 在 site-packages 与工作区临时目录上都被 Errno 13 拒掉）。于是用一个 30 行的替身
    把 `from aiohttp import web` 顶掉，只验证**我自己的接线**：
    路由有没有注册、token 校验对不对、payload 有没有透传。

    **它不能替代真实验证** —— aiohttp 自身的行为不在这里验证，服务器上仍要 curl 复核一次。
    """
    import sys
    import types

    saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web", "app.dashboard.server")}

    class _Resp:
        def __init__(self, payload=None, status=200):
            self.payload = payload
            self.status = status

    class _Router:
        def __init__(self):
            self.routes = []

        def add_get(self, path, handler):
            self.routes.append(("GET", path, handler))

        def add_post(self, path, handler):
            self.routes.append(("POST", path, handler))

    class _App:
        def __init__(self):
            self.router = _Router()

    fake_aiohttp = types.ModuleType("aiohttp")
    fake_web = types.ModuleType("aiohttp.web")
    fake_web.Application = _App
    fake_web.Request = object
    fake_web.Response = _Resp
    fake_web.StreamResponse = _Resp

    def _json_response(payload, status=200):
        return _Resp(payload, status)

    fake_web.json_response = _json_response
    fake_aiohttp.web = fake_web
    sys.modules["aiohttp"] = fake_aiohttp
    sys.modules["aiohttp.web"] = fake_web
    sys.modules.pop("app.dashboard.server", None)

    try:
        from app.dashboard import server as srv

        app = srv.build_app(onebot, handler, 0.0)
        routes = {(m, p) for m, p, _ in app.router.routes}
        assert ("GET", "/api/health") in routes, routes
        assert ("GET", "/api/status") in routes and ("POST", "/api/clear_memory") in routes, routes

        class _Req:
            def __init__(self, token):
                self.query = {"token": token}

        bad = await srv.api_health(_Req("wrong-token"))
        assert bad.status == 401, bad.status

        good = await srv.api_health(_Req(Config.DASHBOARD_TOKEN))
        assert good.status == 200, good.status
        assert "metrics" in good.payload and "streams" in good.payload, list(good.payload.keys())
        assert good.payload["metrics"]["vision"]["cache_hits"] == 1, good.payload["metrics"]["vision"]
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


if __name__ == "__main__":
    print("=== P0 端到端干跑验证 ===")
    try:
        asyncio.run(main())
    finally:
        # 兜底清理：即使隔离失效，也不要在真实数据目录留下痕迹
        for p in (Path("data/group_style") / f"{TEST_GID}.json",
                  Path("data/stream") / f"{TEST_GID}.jsonl"):
            try:
                p.unlink()
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
        reset_writable_state()       # 别把临时指标/画像目录留给同进程的后续测试
        shutdown_blocking(wait=False)
