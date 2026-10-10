"""
管理面板后端 —— 基于 aiohttp 的轻量 async HTTP server
================================================

提供：
  GET  /                → 静态前端 index.html
  GET  /api/status      → 机器人状态总览
  GET  /api/groups      → 群列表详情
  GET  /api/messages    → 最近 N 条消息
  GET  /api/env         → 环境配置（脱敏）
  GET  /api/events      → SSE 实时事件流（群消息）
  POST /api/clear_memory → 清空某个群记忆（{group_id: int}）

鉴权：所有 /api/* 路由需要 ?token=xxx 匹配 Config.DASHBOARD_TOKEN
静态文件 / 也需要 token（通过 URL query 或 ?token=xxx）
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from app.config import Config
from app.util.blocking import run_blocking

logger = logging.getLogger("Dashboard")

# SSE 订阅者：每个 SSE 连接一个 asyncio.Queue
_sse_subscribers: list[asyncio.Queue] = []
_sse_lock: asyncio.Lock = asyncio.Lock()

# 引用注入点 —— main.py 启动 dashboard 时传入
_onebot = None
_handler = None
_start_time = 0.0


def _check_token(request: web.Request) -> bool:
    """简单鉴权：URL query 里必须带正确的 token"""
    token = request.query.get("token", "")
    return token == Config.DASHBOARD_TOKEN


# ---------- API Handlers ----------

async def api_status(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    h = _handler
    ob = _onebot
    uptime = time.time() - _start_time if _start_time else 0
    # 算一下总记忆条数
    total_memory = 0
    if h and h.memory:
        for gid in h.status["groups"]:
            total_memory += gid.get("memory_count", 0)
    # 算下破冰距上次多久
    groups_with_status = h.status["groups"] if h else []
    icebreak_info = []
    for g in groups_with_status:
        idle = (time.time() - g["last_active_ts"]) / 60 if g["last_active_ts"] else 999
        since_ib = (time.time() - g["last_icebreak_ts"]) / 60 if g["last_icebreak_ts"] else -1
        icebreak_info.append({
            "id": g["id"],
            "idle_minutes": round(idle, 1),
            "last_icebreak_minutes_ago": round(since_ib, 1) if since_ib >= 0 else None,
        })
    data = {
        "ws_connected": ob.ws_connected if ob else False,
        "uptime_seconds": round(uptime, 1),
        "bot_name": Config.BOT_NAME,
        "bot_qq": Config.BOT_QQ,
        "llm_model": Config.LLM_MODEL,
        "group_count": len(groups_with_status),
        "stickers_total": h.sticker.count() if h and h.sticker else 0,
        "style_groups": h.style.count() if h and h.style else 0,
        "total_memory_entries": total_memory,
        "icebreak_info": icebreak_info,
        "server_time": int(time.time()),
    }
    return web.json_response(data)


async def api_groups(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"groups": _handler.status["groups"] if _handler else []})


async def api_messages(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    limit = int(request.query.get("limit", "100"))
    limit = max(1, min(limit, 200))
    msgs = (_handler.message_log if _handler else [])[-limit:]
    return web.json_response({"messages": msgs})


async def api_health(request: web.Request) -> web.Response:
    """【P0-4/P2a】健康卡 + 任务面板数据：指标、各群对话流体积、每个任务的调度状态

    数据组装在 app/dashboard/health.py（不依赖 aiohttp，可单独测试），这里只做鉴权与包装。
    """
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    from app.dashboard.health import build_health_payload
    return web.json_response(await build_health_payload(_handler))


async def api_task(request: web.Request) -> web.Response:
    """【P2a】任务面板的动作入口：`action=list|dry|run`、`group=<群号>`、`task=<任务名>`

    与 `/任务 试跑丨执行` 命令**走同一套 Task 实现**（`Scheduler.run_task`），不复制逻辑。
    试跑（dry）只生成内容，不发送、不写状态、不计预算。
    """
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    sched = getattr(_handler, "scheduler", None)
    if sched is None:
        return web.json_response({"error": "调度器未启用"}, status=503)

    try:
        data = await request.json()
    except Exception:
        data = {}
    action = str(data.get("action") or "list")
    task_name = str(data.get("task") or "")
    try:
        group_id = int(data.get("group") or 0)
    except (TypeError, ValueError):
        group_id = 0

    if action == "list":
        return web.json_response({"ok": True, "tasks": sched.status(group_id or None),
                                  "recent": sched.recent_log(group_id, 20) if group_id else []})
    if action not in ("dry", "run"):
        return web.json_response({"error": f"未知动作: {action}"}, status=400)
    if not group_id or not task_name:
        return web.json_response({"error": "需要 group 与 task 参数"}, status=400)

    res = await sched.run_task(task_name, group_id, dry_run=(action == "dry"),
                               force=bool(data.get("force")))
    return web.json_response({
        "ok": res.status in ("sent", "dry_run", "done"),
        "status": res.status,
        "reason": res.reason,
        "parts": res.parts,
        "elapsed": round(res.elapsed, 2),
        "dry_run": action == "dry",
    })


async def api_env(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    # 从 WS URL 提取 host，拼 HTTP 端口（WS 和 HTTP 端口可能不同）
    from urllib.parse import urlparse
    _parsed = urlparse(Config.ONEBOT_WS_URL)
    _host = _parsed.hostname or "127.0.0.1"
    onebot_http_url = f"http://{_host}:{Config.ONEBOT_HTTP_PORT}"
    data = {
        "onebot_ws_url": Config.ONEBOT_WS_URL,
        "onebot_http_url": onebot_http_url,
        "bot_name": Config.BOT_NAME,
        "bot_qq": Config.BOT_QQ,
        "llm_base_url": Config.LLM_BASE_URL,
        "llm_model": Config.LLM_MODEL,
        "llm_api_key_set": bool(Config.LLM_API_KEY),
        "search_url": Config.SEARXNG_URL,
        "reply_chance": Config.CHAT_REPLY_CHANCE,
        "reply_cooldown": Config.CHAT_COOLDOWN,
        "chat_keywords": Config.CHAT_KEYWORDS,
        "chat_ignore_keywords": Config.CHAT_IGNORE_KEYWORDS,
        "max_history": Config.MAX_HISTORY,
        "dashboard_enabled": Config.DASHBOARD_ENABLED,
        "dashboard_port": Config.DASHBOARD_PORT,
    }
    return web.json_response(data)


async def api_clear_memory(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        gid = int(body.get("group_id", 0))
    except Exception:
        return web.json_response({"error": "invalid request"}, status=400)
    if not gid or not _handler or not _handler.memory:
        return web.json_response({"error": "bad request"}, status=400)
    # 删文件 + 写盘是阻塞操作，别让面板请求把事件循环卡住
    await run_blocking(_handler.memory.clear, gid)
    logger.info(f"[Dashboard] 手动清空群 {gid} 记忆")
    return web.json_response({"ok": True, "group_id": gid})


# ---------- SSE ----------

async def api_events(request: web.Request) -> web.Response:
    """Server-Sent Events 实时消息流"""
    if not _check_token(request):
        return web.Response(status=401)
    queue: asyncio.Queue = asyncio.Queue()
    async with _sse_lock:
        _sse_subscribers.append(queue)
    logger.info(f"[Dashboard] SSE 订阅者加入，当前 {len(_sse_subscribers)} 人")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        while True:
            msg = await queue.get()
            data = json.dumps(msg, ensure_ascii=False)
            await response.write(f"data: {data}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        async with _sse_lock:
            if queue in _sse_subscribers:
                _sse_subscribers.remove(queue)
        logger.info(f"[Dashboard] SSE 订阅者退出，剩余 {len(_sse_subscribers)} 人")


async def _broadcast(msg: dict):
    """handler 注入的回调：把一条消息推给所有 SSE 订阅者"""
    if not _sse_subscribers:
        return
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    async with _sse_lock:
        subs = list(_sse_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except Exception:
            dead.append(q)
    if dead:
        async with _sse_lock:
            for d in dead:
                if d in _sse_subscribers:
                    _sse_subscribers.remove(d)


# ---------- 静态文件 ----------

async def index_handler(request: web.Request) -> web.Response:
    """返回 index.html"""
    if not _check_token(request):
        # 没 token 也返回一个提示页，让用户加 token
        return web.Response(
            text="<h1>QQ Bot 管理面板</h1><p>请在 URL 后加 <code>?token=你的面板token</code></p>",
            content_type="text/html",
        )
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return web.Response(text="index.html not found", status=500)
    return web.FileResponse(html_path)


# ---------- 启动入口 ----------

async def start_dashboard(onebot, handler, start_time: float):
    """
    启动管理面板（asyncio 协程，与 onebot.start() 并行跑）。
    
    用法（在 main.py 里）:
        asyncio.create_task(start_dashboard(onebot, handler, start_time))
    """
def build_app(onebot, handler, start_time: float) -> web.Application:
    """组装面板的 aiohttp 应用（不启动监听）。

    拆出来是为了**可测试**：端到端测试可以直接拿这个 app 起一个临时 server
    去请求 /api/health，而不必真的监听 8080。
    """
    global _onebot, _handler, _start_time
    _onebot = onebot
    _handler = handler
    _start_time = start_time

    # 给 handler 注入 SSE 广播回调
    if handler:
        handler._sse_broadcast = _broadcast

    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/groups", api_groups)
    app.router.add_get("/api/messages", api_messages)
    app.router.add_get("/api/env", api_env)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/clear_memory", api_clear_memory)
    app.router.add_post("/api/task", api_task)
    return app


async def start_dashboard(onebot, handler, start_time: float):
    """
    启动管理面板（asyncio 协程，与 onebot.start() 并行跑）。

    用法（在 main.py 里）:
        asyncio.create_task(start_dashboard(onebot, handler, start_time))
    """
    app = build_app(onebot, handler, start_time)

    # 监听地址来自配置，默认 127.0.0.1（云端走 SSH 隧道，别暴露公网）
    host = Config.DASHBOARD_HOST
    port = Config.DASHBOARD_PORT
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            f"[Dashboard] ⚠️ 面板绑定在 {host}（非本机），公网可直接访问！"
            f"请确认云安全组已限制来源 IP，或把 DASHBOARD_HOST 改回 127.0.0.1 走 SSH 隧道"
        )
    if len(Config.DASHBOARD_TOKEN) < 8:
        logger.warning(
            f"[Dashboard] ⚠️ 面板 token 只有 {len(Config.DASHBOARD_TOKEN)} 位，容易被撞开；"
            f"建议在 .env 里把 DASHBOARD_TOKEN 换成 12 位以上随机串"
        )

    logger.info(f"[Dashboard] 🖥 管理面板已启动: http://{host}:{port}/?token={Config.DASHBOARD_TOKEN}")

    try:
        # 保持运行直到被取消
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("[Dashboard] 正在关闭...")
    finally:
        await runner.cleanup()
