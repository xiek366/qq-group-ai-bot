"""
破冰任务（P2a-5）—— 从 handler 的 `_idle_watcher` + `_icebreak_*` 迁移进来
==========================================================================

**迁移前**：handler 里一个 `while True` 协程，每 5 分钟扫所有群，自己判断工作时间、概率、
空闲时长、群规模、冷却，然后按 70/20/10 概率挑三种策略之一，**顺手把消息发出去**。

**迁移后**：

- "什么时候做 / 能不能做"交给调度器（触发器 + 打扰预算 + 退避 + D15），本文件只管"内容"；
- 内容源按 ROADMAP §4.5 / D3 调整为 **LLM 话题包为主 → 热搜兜底**（去掉"表情包+配文"作为独立破冰手段）；
- 只**生成文本并返回**，发送由框架统一处理 —— 因此干跑（试跑）能复用同一份逻辑，不必复制代码。

依赖注入：两个生成器由 handler 提供（它才有 LLM 提示词上下文、群风格画像、搜索模块），
所以本文件不 import handler，调度框架保持解耦。
"""

from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable, Optional

from app.schedule.task import (
    PRIORITY_ICEBREAK,
    CallableSource,
    Task,
    TaskContext,
)

logger = logging.getLogger(__name__)


def make_icebreak_source(
    generate_topic: Optional[Callable[[TaskContext], Awaitable[str]]] = None,
    generate_hot: Optional[Callable[[TaskContext], Awaitable[str]]] = None,
) -> CallableSource:
    """LLM 话题包为主，热搜兜底（两个都给不出内容 → 返回空 → 本次放弃，绝不硬发）"""

    async def _render(ctx: TaskContext) -> list:
        if generate_topic is not None:
            try:
                text = (await generate_topic(ctx) or "").strip()
            except Exception as e:
                logger.error(f"[Icebreak] 生成话题失败: {e}")
                text = ""
            if text:
                return [text]
        if generate_hot is not None:
            try:
                hot = (await generate_hot(ctx) or "").strip()
            except Exception as e:
                logger.warning(f"[Icebreak] 热搜兜底也失败: {e}")
                hot = ""
            if hot:
                return [hot]
        logger.info("[Icebreak] 两种内容源都没给出内容 → 本次不破冰")
        return []

    return CallableSource("icebreak_mixed", _render)


def make_rotating_source(named_sources: list) -> CallableSource:
    """【P2c】破冰内容源**轮换**（ROADMAP §4.5）。

    `named_sources` 是 `[(标签, 生成函数), ...]`，按顺序轮着用；轮到的那种给不出内容时，
    **依次尝试后面的**（所以顺序本身就是优先级 + 兜底链：
    话题包 → 群历史回顾 → 资讯 → 话题库 → 热搜）。

    每个生成函数返回 `str` 或 `list[str]` 都行（统一规整成一条文本）。
    """
    counter = {"i": 0}

    async def _render(ctx: TaskContext) -> list:
        if not named_sources:
            return []
        start = counter["i"] % len(named_sources)
        for step in range(len(named_sources)):
            idx = (start + step) % len(named_sources)
            label, fn = named_sources[idx]
            try:
                result = fn(ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as e:                 # 单个源失败不该让破冰整个失败
                logger.warning(f"[Icebreak] 内容源「{label}」失败: {e}")
                continue
            if isinstance(result, str):
                text = result.strip()
            else:
                parts = [str(p).strip() for p in (result or []) if str(p).strip()]
                text = "\n".join(parts).strip()
            if text:
                counter["i"] = (idx + 1) % len(named_sources)
                logger.info(f"[Icebreak] 本次内容源 = {label}")
                return [text]
        logger.info("[Icebreak] 所有内容源都没给出内容 → 本次不破冰")
        return []

    return CallableSource("icebreak_rotating", _render)


class IcebreakTask(Task):
    """冷场抛话题（触发器默认 idle 30 分钟；冷却/退避/工作时段都由配置与框架负责）"""

    name = "icebreak"
    label = "冷场破冰"
    priority = PRIORITY_ICEBREAK
    is_proactive = True
    trigger_spec = {"type": "idle", "minutes": 30}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 60,          # 【Q1】不再用随机概率，改为冷却 + 退避
        "daily_limit": 4,                # 就算群一直没人说话，一天最多 4 次
        "min_group_size": 3,             # 2 人群不破冰（沿用旧默认）
        "require_active_within_minutes": 0,
        "work_hours": [8, 23],           # 沿用 ICEBREAK_WORK_START/END 的环境变量默认
        "backoff": {"on_no_reply": True, "factor": 2, "max_level": 3},
    }

    def __init__(self, generate_topic=None, generate_hot=None, source=None):
        """两种用法：给两个生成函数（话题包 + 热搜兜底），或直接给一个 `source`（P2c 的轮换源）"""
        super().__init__()
        self.source = source if source is not None else make_icebreak_source(generate_topic, generate_hot)
