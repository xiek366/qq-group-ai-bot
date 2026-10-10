"""
滚动摘要任务（P2a-5b，Q4 的答复：**迁入框架**）
============================================

**摘要逻辑一个字都没动** —— 还是 P1 的 `ConversationSummary.run()`（一次 LLM 调用产出
要点/画像/话题）。这里只做两件事：

1. 把触发入口从"内存计数 + 后台任务"换成框架的 `interval` 触发器（每 10 分钟检查一次）；
2. 用 `silent_ok = True` 声明"这个任务干完活但**没有内容要发**"——
   否则框架会把它判成"内容为空"，不记 `last_run`，于是每个 tick 都会重复触发。

同时它 `is_proactive = False`：摘要不打扰群，因此**不受 D15 主动白名单限制**，
测试群也能正常积累记忆（这对验证很有用），且不占用"每日打扰预算"。
"""

from __future__ import annotations

import logging

from app.config import Config
from app.schedule.task import PRIORITY_MAINTENANCE, Task, TaskContext, TaskSkip

logger = logging.getLogger(__name__)


class SummaryJob(Task):
    """攒够消息就压缩长期记忆（要点 + 画像 + 话题）"""

    name = "summary_job"
    label = "长期记忆摘要"
    priority = PRIORITY_MAINTENANCE
    is_proactive = False          # 不发消息给群，不算打扰
    silent_ok = True              # 干完活没有内容要发 —— 不是"内容为空"
    trigger_spec = {"type": "interval", "minutes": 10}
    defaults = {
        "enabled": True,
        "cooldown_minutes": 10,
        "daily_limit": 0,         # 不设每日上限：摘要次数由"消息量"决定
        "min_group_size": 0,
        "require_active_within_minutes": 0,
        "min_messages": 40,       # 沿用 SUMMARY_MIN_MESSAGES
        "backoff": {"on_no_reply": False},
    }

    def __init__(self, run_summary):
        """`run_summary(group_id)` 由 handler 注入：读对话流 → 调 ConversationSummary → 落盘"""
        super().__init__()
        self._run_summary = run_summary
        self.defaults["min_messages"] = Config.SUMMARY_MIN_MESSAGES
        self.defaults["cooldown_minutes"] = Config.SUMMARY_COOLDOWN_MINUTES
        self.trigger_spec = {"type": "interval", "minutes": Config.SUMMARY_COOLDOWN_MINUTES}

    async def render(self, ctx: TaskContext) -> list:
        """真正的"取内容"其实是"干活"：跑一次摘要，然后返回空列表（silent_ok 接住）"""
        try:
            changed = await self._run_summary(ctx.group_id)
        except Exception as e:
            logger.error(f"[Summary] 任务执行失败 group={ctx.group_id}: {e}")
            raise
        if not changed:
            # 没有新的未摘要内容 → 主动跳过（不更新 last_run，下次仍会正常评估）
            raise TaskSkip("暂无需要摘要的内容")
        return []
