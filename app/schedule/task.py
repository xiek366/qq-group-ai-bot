"""
任务与内容源（P2a-1）
=====================

**三层解耦**（ROADMAP §4.2）：

```
Scheduler（什么时候做） → Task（做什么、能不能做） → ContentSource（内容从哪来）
```

- `Task` 只声明：名字、优先级、触发器、默认开关/冷却/上限、以及"取内容 + 发送"的两步；
- `ContentSource` 只负责 `render(ctx) -> list[str]`，返回空列表 = 本次放弃（**绝不硬发**）；
- 调度器不认识任何具体业务，加新任务 = 新写一个 Task 子类 + 注册一行，**不用改 handler**（验收标准 1）。

**干跑（dry-run）语义**（ROADMAP §4.10，也是 Q2 的答复）：
`Task.run(ctx, dry_run=True)` 只取内容、不发送、不写状态、不计预算、不动冷却 ——
所以试跑可以随便跑，不会干扰真实调度。结果交给调用方（命令层走私聊管理员）。
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from app.schedule.state import fingerprint
from app.schedule.triggers import Trigger, build_trigger

logger = logging.getLogger(__name__)

# 任务优先级（数字越小越优先；ROADMAP §4.8）
PRIORITY_EVENT = 10       # 欢迎（事件型，即时）
PRIORITY_BROADCAST = 20   # 天气等播报
PRIORITY_DAILY = 30       # 每日一句 / 每日图
PRIORITY_REMINDER = 40    # 喝水 / 久坐
PRIORITY_ICEBREAK = 50    # 破冰（最不着急）
PRIORITY_MAINTENANCE = 90 # 摘要等维护型（不产生"打扰"，但占同一把互斥锁）


@dataclass
class TaskContext:
    """任务执行时能拿到的一切（不直接碰 handler，保持解耦）"""

    group_id: int
    now: float
    dry_run: bool = False
    config: dict = field(default_factory=dict)      # 该任务在该群的有效配置（群覆盖 > env > 默认）
    state: dict = field(default_factory=dict)       # 该任务在该群的状态快照
    group_config: dict = field(default_factory=dict)
    idle_minutes: float = 0.0
    extra: dict = field(default_factory=dict)       # 调度器/命令层塞进来的东西（如 handler 引用）

    def flag(self, name: str, default=None):
        return self.config.get(name, default)


@dataclass
class TaskResult:
    """一次执行的结果（调度器据此记账）"""

    status: str = "skipped"        # sent | skipped | empty | error | dry_run
    parts: list = field(default_factory=list)       # 实际发送（或干跑生成）的内容片段
    reason: str = ""               # skipped/error 的原因
    fingerprint_value: str = ""
    elapsed: float = 0.0
    sent_ok: bool = False

    @property
    def content(self) -> str:
        return "\n".join(self.parts)


class ContentSource(Protocol):
    """内容源协议：返回 1~3 条短句；空列表 = 本次放弃"""

    name: str

    async def render(self, ctx: TaskContext) -> list:
        ...


class TaskSkip(Exception):
    """内容源/任务主动表示"这次没什么可做的"（不是错误）。

    抛出后本次执行记为 `skipped`：不发送、不更新 `last_run`、不占预算 ——
    这样 interval 触发器下次仍会正常评估。用于"没有新内容需要摘要"这类情况。
    """


# 主动消息里**绝对不能出现**的东西（模型偶尔会自己加 @全体成员，那会炸群）
_FORBIDDEN_PATTERNS = (
    ("[CQ:at,qq=all]", ""),
    ("[cq:at,qq=all]", ""),
    ("[CQ:at,qq=0]", ""),
    ("@全体成员", ""),
    ("@所有人", ""),
    ("@all", ""),
)


def sanitize_outgoing(text: str) -> str:
    """清理即将发出的主动消息：去掉"炸群"和"@自己"的内容。

    **为什么必须在框架层做**：破冰/播报的文案是 LLM 生成的，实测它干过两件事
    （2026-10-08 两次试跑）：
      1. 自己加上 `@全体成员` —— 真发出去就是炸群事故；
      2. 在@ 自己（`@爱丽丝 …`）—— 看着像 bug；如果生成的是真 CQ 码
         `[CQ:at,qq=<bot>]`，**bot 会把自己勾进来，触发自身的 @-处理，可能形成自我回复循环**。

    这两件事不属于任何单个任务的职责，所以放在 `Task.execute()` 里统一清洗，
    所有任务（含试跑）自动受保护 —— 试跑也清洗，是为了让管理员在私聊里
    看到的就是"真要发出去的样子"。
    """
    if not text:
        return text

    from app.config import Config

    bad_list = list(_FORBIDDEN_PATTERNS)
    bot_qq = str(Config.BOT_QQ or "").strip()
    bot_name = str(Config.BOT_NAME or "").strip()
    if bot_qq:
        for tpl in ("[CQ:at,qq={}]", "[cq:at,qq={}]"):
            bad_list.append((tpl.format(bot_qq), ""))
        bad_list.append((f"@{bot_qq}", ""))
    if bot_name:
        # ⚠️ 只匹配全名是不够的：机器人叫「天童爱丽丝」，模型却常写简称「@爱丽丝」
        # （2026-10-08 服务器实测漏掉了）。把名字的**所有长度≥2 的后缀**都当候选：
        # 天童爱丽丝 → 天童爱丽丝 / 童爱丽丝 / 爱丽丝 / 丽丝
        candidates = {bot_name}
        for i in range(1, len(bot_name) - 1):
            suffix = bot_name[i:]
            if len(suffix) >= 2:
                candidates.add(suffix)
        # 长的先替换，避免先删短名留下多余字（例如「@天童爱丽丝」留下「天童」）
        for name in sorted(candidates, key=len, reverse=True):
            bad_list.append((f"@{name}", ""))

    out = text
    for bad, good in bad_list:
        out = out.replace(bad, good)
    # 清理被删空之后留下的空白
    out = "\n".join(line.strip() for line in out.splitlines())
    return out.strip()


class CallableSource:
    """把一个 async 函数包装成内容源（现有破冰逻辑就能这样接进来，不必重写）"""

    def __init__(self, name: str, func: Callable[[TaskContext], Awaitable[list]]):
        self.name = name
        self._func = func

    async def render(self, ctx: TaskContext) -> list:
        parts = await self._func(ctx)
        return [p for p in (parts or []) if p]


class Task:
    """任务基类。

    子类最少只需实现 `render(ctx) -> list[str]`；
    想自定义发送（比如带图片）就覆写 `deliver(ctx, parts) -> bool`。
    """

    name: str = "task"
    label: str = ""                     # 面板显示名
    priority: int = PRIORITY_DAILY
    trigger_spec: dict = {}             # 默认触发器（会被群配置覆盖）
    source: Optional[ContentSource] = None
    # 是否会"主动打扰"群：受 D15 的 PROACTIVE_GROUPS 白名单与每日总预算约束。
    # 维护型任务（如摘要）设为 False —— 它不产生打扰，但不该受"打扰白名单"限制。
    is_proactive: bool = True
    # 干完活但**没有内容要发**的任务（如摘要、清理）设为 True：
    # 否则它每次都会被判成"内容为空"→ 不记 last_run → interval 触发器每 tick 重复触发。
    silent_ok: bool = False
    # 【P2c-图池】**产出只进日志/面板、绝不发到群里**的任务设为 True（如凌晨补货）。
    # 与 `is_proactive=False` 的区别：后者只豁免"打扰白名单/预算"，仍然会发送；
    # 本标志才是"这个任务根本不发消息"。干跑（/任务 试跑）仍会返回内容给管理员看。
    no_send: bool = False
    # 默认参数（会被群配置 / 环境变量覆盖）
    defaults: dict = {
        "enabled": True,
        "cooldown_minutes": 0,
        "daily_limit": 0,               # 0 = 不限（只受群总预算约束）
        "min_group_size": 0,            # 0 = 不检查
        "require_active_within_minutes": 0,   # >0 时要求群最近这么分钟内有人说话
        "backoff": {"on_no_reply": False, "factor": 2, "max_level": 3},
    }

    def __init__(self, name: str = "", label: str = ""):
        if name:
            self.name = name
        if label:
            self.label = label
        # ⚠️ defaults 定义在类上，但实例化时必须**深拷贝一份**：
        # 否则任何一处 `task.defaults["x"] = ...`（配置覆盖、测试脚手架）都会改到类属性，
        # 污染同类任务的其他实例（踩过：一个测试改了 cooldown，另一个测试莫名进入冷却）。
        self.defaults = copy.deepcopy(type(self).defaults)
        self.trigger = build_trigger(self.trigger_spec)

    # ---------- 配置 ----------

    def effective_config(self, group_config: dict) -> dict:
        """合并默认值 + 群配置里该任务的段（群覆盖 > 代码默认；env 由调用方提前合进 group_config）"""
        cfg = dict(self.defaults)
        cfg["trigger"] = dict(self.trigger_spec)
        override = (group_config.get("tasks") or {}).get(self.name) or {}
        for k, v in override.items():
            if k == "trigger" and isinstance(v, dict):
                merged = dict(cfg["trigger"])
                merged.update(v)
                cfg["trigger"] = merged
            elif k == "backoff" and isinstance(v, dict):
                merged = dict(cfg.get("backoff") or {})
                merged.update(v)
                cfg["backoff"] = merged
            else:
                cfg[k] = v
        return self.refine_config(cfg, group_config)

    def refine_config(self, cfg: dict, group_config: dict) -> dict:
        """让任务把"群配置里与本任务相关的字段"映射进有效配置（默认什么都不做）。

        例子：`weather_morning` 的播报时刻写在群配置的 `weather.broadcast_at`（ROADMAP §4.3 的形式），
        而触发器读的是 `cfg["trigger"]["at"]` —— 由该任务覆写本方法完成映射，
        这样调度器不需要认识任何具体业务字段。
        """
        return cfg

    def build_trigger(self, cfg: dict) -> Optional[Trigger]:
        return build_trigger(cfg.get("trigger") or self.trigger_spec)

    # ---------- 执行 ----------

    async def render(self, ctx: TaskContext) -> list:
        """默认走 content source；子类可覆写"""
        if self.source is None:
            return []
        return await self.source.render(ctx)

    async def deliver(self, ctx: TaskContext, parts: list) -> bool:
        """默认分条发送（复用注入进来的发送器）；返回是否发送成功"""
        sender = ctx.extra.get("send")
        if sender is None:
            logger.error(f"[Schedule] 任务 {self.name} 没有发送器，放弃")
            return False
        for i, part in enumerate(parts):
            if i:
                import asyncio
                await asyncio.sleep(1.2)        # 分条之间留点间隔，别像刷屏
            await sender(ctx.group_id, part)
        return True

    async def execute(self, ctx: TaskContext) -> TaskResult:
        """完整跑一次：取内容 → 发送 → 记账前的指纹计算。

        **试跑（ctx.dry_run=True）在此处就返回**：不发送、不写状态（状态由调度器负责写，
        调度器看到 dry_run 会跳过所有记账）。
        """
        start = time.time()
        try:
            parts = await self.render(ctx)
        except TaskSkip as e:
            logger.debug(f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 主动跳过: {e}")
            return TaskResult(status="empty", reason=str(e) or "任务主动跳过",
                              elapsed=time.time() - start)
        except Exception as e:
            logger.error(f"[Schedule] 任务 {self.name} 取内容失败: {e}")
            return TaskResult(status="error", reason=str(e)[:200], elapsed=time.time() - start)

        if not parts:
            if self.silent_ok:
                # 干完活了，只是没有内容要发（摘要/清理这类维护任务）
                logger.info(f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 已执行（无内容需发送）")
                return TaskResult(status="done", reason="无内容需发送", elapsed=time.time() - start)
            # 内容源返回空 = 本次放弃（ROADMAP §4.4 第 4 步：宁可不发）
            logger.info(f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 内容为空 → 放弃本次")
            return TaskResult(status="empty", reason="内容源返回空", elapsed=time.time() - start)

        # 所有任务的内容都过一遍安全清洗（去掉 @全体成员 之类），
        # 试跑也清洗 —— 这样管理员在私聊里看到的就是"真要发出去的样子"。
        cleaned = []
        for part in parts:
            fixed = sanitize_outgoing(part)
            if fixed != part:
                logger.warning(f"[Schedule] 任务 {self.name} 内容含 @全体成员 之类，已清洗")
            if fixed:
                cleaned.append(fixed)
        parts = cleaned
        if not parts:
            return TaskResult(status="empty", reason="清洗后没有可发送内容",
                              elapsed=time.time() - start)

        fp = fingerprint(self.name, ctx.group_id, parts[0][:80])
        if ctx.dry_run:
            logger.info(f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 试跑：生成 {len(parts)} 条（未发送）")
            return TaskResult(status="dry_run", parts=parts, fingerprint_value=fp,
                              elapsed=time.time() - start)

        # 【P2c-图池】维护任务：产出只进日志/面板，**绝不发到群里**（干跑仍返回给管理员看）
        if self.no_send and not ctx.dry_run:
            logger.info(
                f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 已执行（维护任务，不发送）：{parts[0][:60]}"
            )
            return TaskResult(status="done", parts=parts, reason="维护任务不发送",
                              fingerprint_value=fp, elapsed=time.time() - start)

        try:
            ok = await self.deliver(ctx, parts)
        except Exception as e:
            logger.error(f"[Schedule] 任务 {self.name} 发送失败: {e}")
            return TaskResult(status="error", reason=str(e)[:200], parts=parts,
                              fingerprint_value=fp, elapsed=time.time() - start)

        logger.info(
            f"[Schedule] 任务 {self.name} 群 {ctx.group_id} 已执行：{len(parts)} 条，"
            f"用时 {time.time() - start:.2f}s"
        )
        return TaskResult(status="sent" if ok else "error", parts=parts,
                          fingerprint_value=fp, elapsed=time.time() - start, sent_ok=ok)

    # ---------- 展示 ----------

    def describe(self, cfg: Optional[dict] = None) -> str:
        cfg = cfg or self.defaults
        trig = self.build_trigger(cfg)
        return f"{self.label or self.name}（{trig.describe() if trig else '无触发器'}）"
