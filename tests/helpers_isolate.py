"""
测试隔离工具（**不是测试文件**，文件名不带 test_ 前缀，所以不会被当用例收集）
==========================================================================
**为什么存在**：`MessageHandler` 一构造就会创建 `StyleProfile` / `Metrics` 等会落盘的模块，
它们默认指向仓库里的真实数据目录。用真实群号（当文档）跑测试时，
一不小心就把测试消息写进了 `data/group_style/<真实群号>.json`。

这个坑踩过两次（2026-10-07 的 `100000004.json`、`100000040.json`），所以统一在这里收口：

    from helpers_isolate import isolate_writable_state
    isolate_writable_state(TMP)      # 在构造 MessageHandler **之前**调用

凡是将来新增"会写 data/ 的单例"，都在这里加一行补丁。
"""
from __future__ import annotations

import sys
from pathlib import Path


def isolate_writable_state(tmp: Path):
    """把所有"会落盘的全局单例"指到临时目录，返回 tmp"""
    tmp.mkdir(parents=True, exist_ok=True)

    # 1) 群风格画像：data/group_style/<群号>.json（最容易漏的一个）
    from app.style import profile as style_mod
    style_mod._DATA_DIR = tmp / "group_style"

    # 2) 运行指标：data/stats/summary.json
    from app import metrics as metrics_mod
    metrics_mod._instance = metrics_mod.Metrics(path=tmp / "metrics.json", save_every=999)

    # 3) 【P2a】调度框架的三个目录 —— MessageHandler 一构造就会建 Scheduler，
    #    不做这一步，用真实群号跑的测试会把任务状态/执行记录写进真实 data/
    from app.schedule import group_config as sched_cfg
    from app.schedule import scheduler as sched_sched
    from app.schedule import state as sched_state
    sched_state.SCHEDULE_DIR = tmp / "schedule"
    sched_cfg.GROUP_CONFIG_DIR = tmp / "group_config"
    sched_sched.TASK_LOG_DIR = tmp / "tasks"

    # 4) 【P1】长期记忆与话题状态 —— handler 内部自己 new 了实例，也得指到临时目录
    from app.context import summary as ctx_summary
    from app.context import topic as ctx_topic
    ctx_summary.SUMMARY_DIR = tmp / "summary"
    ctx_topic.STATE_DIR = tmp / "state"

    # 5) 【P2c】资讯状态 / 话题库 / 台词池 —— 同样是 handler 内部自建的实例
    #    （漏了它们，测试会把"话题使用次数"写进真实 data/cache）
    from app.context import topic_pool as ctx_topics
    from app.news import service as news_service
    ctx_topics.CACHE_DIR = tmp / "cache"
    news_service.NEWS_CACHE_DIR = tmp / "news"
    try:
        import app.bot.handler as handler_mod
        handler_mod.QUOTES_STATE_PATH = tmp / "cache" / "quotes.json"
    except Exception:
        pass

    return tmp


def reset_writable_state():
    """测试结束时还原（主要是别让单例残留影响同一进程里的其他测试）"""
    from app import metrics as metrics_mod
    from app.context import summary as ctx_summary
    from app.context import topic as ctx_topic
    from app.context import topic_pool as ctx_topics
    from app.news import service as news_service
    from app.paths import (
        CACHE_DIR, GROUP_CONFIG_DIR, NEWS_CACHE_DIR, QUOTES_STATE_PATH,
        SCHEDULE_DIR, STATE_DIR, STYLE_DIR, SUMMARY_DIR, TASK_LOG_DIR,
    )
    from app.schedule import group_config as sched_cfg
    from app.schedule import scheduler as sched_sched
    from app.schedule import state as sched_state
    from app.style import profile as style_mod

    style_mod._DATA_DIR = STYLE_DIR
    metrics_mod._instance = None
    sched_state.SCHEDULE_DIR = SCHEDULE_DIR
    sched_cfg.GROUP_CONFIG_DIR = GROUP_CONFIG_DIR
    sched_sched.TASK_LOG_DIR = TASK_LOG_DIR
    ctx_summary.SUMMARY_DIR = SUMMARY_DIR
    ctx_topic.STATE_DIR = STATE_DIR
    ctx_topics.CACHE_DIR = CACHE_DIR
    news_service.NEWS_CACHE_DIR = NEWS_CACHE_DIR
    try:
        import app.bot.handler as handler_mod
        handler_mod.QUOTES_STATE_PATH = QUOTES_STATE_PATH
    except Exception:
        pass
