"""
健康卡与任务面板数据组装（P0-4 / P2a）
=====================================

**为什么单独一个文件**：`server.py` 顶部 `from aiohttp import web` —— 本地开发环境没装 aiohttp 时
整个面板模块都 import 不了，健康卡的逻辑就没法测。把"算数据"和"发 HTTP"分开之后：

  - 这里只依赖 metrics + 对话流 + 调度器状态，**不依赖 aiohttp**，任何环境都能测；
  - `server.py` 只做鉴权 + `web.json_response()`，是薄薄一层。

指标本身由 `app.metrics` 维护（纯内存读取）；对话流体积需要 stat 文件，走 `run_blocking`，
不在事件循环里做 I/O。
"""

from __future__ import annotations

import time
from typing import Optional

from app.util.blocking import run_blocking


async def build_health_payload(handler) -> dict:
    """组装健康卡 JSON（handler 可以为 None）"""
    from app import metrics

    snap = metrics.get().snapshot()

    streams: list[dict] = []
    if handler is not None and getattr(handler, "memory", None):
        stream = handler.memory.stream
        # 只统计"指标里出现过的群" + "对话流文件已存在的群"，避免扫描无关目录
        gids = {int(g["group_id"]) for g in snap["groups"] if str(g["group_id"]).isdigit()}
        gids |= set(stream.groups())
        for gid in sorted(gids):
            try:
                streams.append(await run_blocking(stream.stats, gid))
            except Exception:
                continue

    # 让对话流里存在、但指标里还没有记录的群也出现在"每群"卡片里。
    # 典型场景：迁移进来的历史（走的是直接写文件，不经过 append，所以不计入指标），
    # 否则面板会显示"暂无数据"，让人误以为这个群没有任何对话。
    # 注意字段名是 `stream_entries`（= 对话流里的总条数），不是"迁移了多少条"——
    # 迁移后还会继续追加新消息，两者无法从文件里区分。
    known = {str(g["group_id"]) for g in snap["groups"]}
    for st in streams:
        gid = str(st.get("group_id"))
        if gid not in known:
            snap["groups"].append({
                "group_id": gid, "user_msgs": 0, "bot_msgs": 0, "bot_responded": 0,
                "reply_rate": 0.0, "stream_entries": st.get("entries", 0),
            })
            known.add(gid)
    snap["groups"].sort(key=lambda g: -(g.get("user_msgs") or 0))

    # 【P2a】任务面板数据：每个任务的启用状态/触发器/下次预计/上次结果/当日次数/退避，
    # 外加最近几条执行记录。试跑与立即执行按钮都走 /api/task（同一套 Task 实现）。
    tasks: dict = {}
    if handler is not None and getattr(handler, "scheduler", None):
        try:
            for gid, info in handler.scheduler.status().items():
                tasks[str(gid)] = info
        except Exception as e:                      # 面板数据取不到不该让整个健康卡失败
            logger.warning(f"[Dashboard] 任务状态收集失败: {e}")

    return {
        "metrics": snap,
        "streams": streams,
        "stream_total_bytes": sum(s.get("bytes", 0) for s in streams),
        "tasks": tasks,
        "server_time": int(time.time()),
    }


def summarize_for_log(payload: dict) -> str:
    """一行摘要，方便启动/排障时打日志"""
    m = payload.get("metrics", {})
    llm = m.get("llm", {})
    vis = m.get("vision", {})
    return (
        f"llm_calls={llm.get('calls', 0)} fail={llm.get('failures', 0)} "
        f"vision={vis.get('calls', 0)} 命中={vis.get('cache_hits', 0)} "
        f"stream={payload.get('stream_total_bytes', 0)}B"
    )


def find_group(payload: dict, group_id: int) -> Optional[dict]:
    """从快照里取某个群的指标（测试与排障用）"""
    for g in payload.get("metrics", {}).get("groups", []):
        if str(g.get("group_id")) == str(group_id):
            return g
    return None
