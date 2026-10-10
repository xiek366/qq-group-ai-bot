"""
短期上下文记忆 —— 现在是「统一对话流」的兼容门面
================================================

**P0-1 改造说明（2026-10-07）**

老实现：每个群在内存里维护一个 list，每次写入都把整个 `data/memory/<群ID>.json`
重写一遍（读-改-写），而且**只有 @bot 的那几轮**会被记下来 —— 群里平时的聊天、
bot 自己主动说的话（插嘴、破冰、看图）都不在记忆里，所以 bot 接不上话、说完就忘。

新实现：事实来源（single source of truth）变成 `app/context/stream.py` 的
**append-only 对话流** `data/stream/<群ID>.jsonl`，所有消息与 bot 发言都在里面。
本类保留原来的四个方法作为**兼容门面**，内部全部委托给对话流：

| 方法 | 现在的行为 |
|---|---|
| `add_user(...)` | 追加一条 `role=user` 到对话流，可带 `kind` |
| `add_assistant(...)` | 追加一条 `role=assistant` 到对话流，可带 `kind` |
| `get_messages(...)` | 从对话流倒读最近 `MAX_HISTORY × 2` 条，转成 OpenAI messages（含稀疏时间标记） |
| `clear(...)` | 对话流改名备份（`.bak-<时间戳>`）后清空 |
| `size(...)` | 对话流条目数（走缓存） |

这样 handler / 面板的调用点一行都不用改，而"写的和读的"终于是同一份数据了。
旧记忆在启动时由 `ConversationStream.migrate_all()` 一次性迁移（旧文件改名 `.migrated` 留底）。

线程约定：写操作是阻塞的，异步侧用 `await run_blocking(memory.add_user, ...)` 调用。
"""

import logging
from typing import Optional

from app.config import Config
from app.context.stream import ConversationStream

logger = logging.getLogger(__name__)


class GroupMemory:
    """按群隔离的记忆（兼容门面，底层是 append-only 对话流）"""

    def __init__(
        self,
        max_history: Optional[int] = None,
        persist: bool = True,
        stream: Optional[ConversationStream] = None,
    ):
        self.max_history = max_history or Config.MAX_HISTORY
        self._persist = persist
        self.stream = stream or ConversationStream()
        # 注意：旧记忆迁移**不在这里做**（构造函数的副作用会让测试/面板误触生产数据），
        # 改由 main.build_handler() 在启动期显式调用 stream.migrate_all()；
        # 另外 stream.window() 在该群没有对话流文件时会懒迁移一次作为兜底。

    # ---------- 写 ----------

    def add_user(
        self,
        group_id: int,
        user_id: int,
        nickname: str,
        content: str,
        kind: str = "chat",
    ):
        """追加一条用户消息（阻塞；异步侧用 run_blocking 调用）"""
        self.stream.append(
            group_id, "user", content,
            user_id=user_id, nick=nickname, kind=kind,
        )

    def add_assistant(self, group_id: int, content: str, kind: str = "reply"):
        """追加一条机器人发言（阻塞；异步侧用 run_blocking 调用）"""
        self.stream.append(group_id, "assistant", content, kind=kind)

    # ---------- 读 ----------

    def get_messages(self, group_id: int) -> list[dict]:
        """最近窗口 → OpenAI messages（user 会把昵称拼进 content，让 LLM 知道谁在说话）"""
        entries = self.stream.window(group_id, limit=self.max_history * 2)
        return ConversationStream.to_messages(entries)

    def size(self, group_id: int) -> int:
        """当前窗口里的条目数（面板沿用老语义：不超过 MAX_HISTORY×2）"""
        return min(self.stream.count(group_id), self.max_history * 2)

    def total_entries(self, group_id: int) -> int:
        """对话流里的总条目数（健康卡用，不受窗口限制）"""
        return self.stream.count(group_id)

    def clear(self, group_id: int):
        """清空某群记忆（对话流改名备份）"""
        self.stream.clear(group_id)
