"""
调度器（P2a-1 / P2a-4）
=======================

单个后台协程，每 `SCHEDULER_TICK_SECONDS`（默认 30 秒）扫一遍"任务 × 群"，判断该不该跑。

**判断顺序**（对应 ROADMAP §4.4，任何一步不通过就记一条 skip 原因然后放弃）：

```
1) enabled?             任务开关（群配置 > 环境变量）
2) D15 主动打扰名单?     不在 PROACTIVE_GROUPS 里的群，主动任务一律不跑（维护型任务不受限）
3) 静默时段 / 工作时段?  按 Config.TIMEZONE 算，支持跨午夜
4) 每日上限?            每任务上限 + 每群每日总预算（D18：默认 8 条）
5) 冷却?                距上次执行够久了吗
6) 前置条件?            群规模 / 群最近是否活跃（死群不打扰）
7) 退避?                上次主动消息没人答 → 门槛翻倍（30 → 60 → 120 分钟）
8) trigger 到点?
9) 同群互斥             同一轮里一个群只跑一个主动任务，且两次主动消息至少隔 1 分钟
10) 执行                内容源返回空 → 放弃；失败 → 记录，绝不重试轰炸
```

**它不认识任何具体业务**：任务通过 `register()` 注册，内容由 `ContentSource` 提供。
外部依赖（发消息、群列表、群活跃时间、群成员数）全部以**回调**注入 ——
调度器不 import handler，也能脱离 bot 单测（干跑验证就是这么做的）。

**任务记录**：每次执行往 `data/tasks/<群ID>.jsonl` 追加一行，面板与 `/任务 列表` 都读它；
文件超过 200KB 时按 `TASK_LOG_KEEP_DAYS` 顺带裁剪一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.paths import TASK_LOG_DIR, ensure_dir
from app.schedule.group_config import GroupConfigStore
from app.schedule.state import TaskStateStore, today_key
from app.schedule.task import Task, TaskContext, TaskResult
from app.schedule.triggers import in_quiet_hours
from app.util import clock as _clock

logger = logging.getLogger(__name__)

# 群成员数的缓存时长：多任务都要用它（破冰的群规模检查），但没必要每 tick 都问 NapCat
_MEMBER_CACHE_TTL = 600.0


class Scheduler:
    """任务调度器（每个进程一个）"""

    def __init__(
        self,
        *,
        state: Optional[TaskStateStore] = None,
        group_config: Optional[GroupConfigStore] = None,
        send: Optional[Callable[[int, str], Awaitable[None]]] = None,
        group_ids: Optional[Callable[[], list]] = None,
        last_activity: Optional[Callable[[int], float]] = None,
        member_count: Optional[Callable[[int], Awaitable[int]]] = None,
        task_log_dir: Optional[Path] = None,
    ):
        self.tasks: dict[str, Task] = {}
        self.state = state or TaskStateStore()
        self.group_config = group_config or GroupConfigStore()
        self._send = send
        self._group_ids = group_ids or (lambda: [])
        self._last_activity = last_activity or (lambda gid: 0.0)
        self._member_count = member_count
        self._log_dir = ensure_dir(task_log_dir or TASK_LOG_DIR)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 每群"上次主动消息"的时间（同群互斥用）
        self._last_proactive: dict = {}
        # 群成员数缓存：{群号: (人数, 取数时间)}
        self._members: dict = {}

    # ---------- 注册与生命周期 ----------

    def register(self, task: Task) -> Task:
        if not task.trigger:
            task.trigger = task.build_trigger(task.defaults)
        self.tasks[task.name] = task
        # 注意别写成 f"（{task.label}（{trigger.describe()}））" —— 日志里会出现双重括号
        logger.info(
            f"[Schedule] 已注册任务: {task.name}"
            f"（{task.label or task.name} ｜ {task.describe()} ｜ 优先级 {task.priority}）"
        )
        return task

    def start(self) -> Optional[asyncio.Task]:
        if not Config.SCHEDULER_ENABLED:
            logger.info("[Schedule] 调度器已禁用（SCHEDULER_ENABLED=false）")
            return None
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        logger.info(
            f"[Schedule] 调度器已启动：{len(self.tasks)} 个任务 / tick={Config.SCHEDULER_TICK_SECONDS}s / "
            f"静默 {Config.QUIET_HOURS} / 每群每日上限 {Config.DAILY_TOTAL_LIMIT} 条"
        )
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # 起来先缓一下，别和启动流程抢 IO
        await asyncio.sleep(min(5, Config.SCHEDULER_TICK_SECONDS))
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Schedule] tick 异常: {e}")
            await asyncio.sleep(Config.SCHEDULER_TICK_SECONDS)

    # ---------- 外部世界（回调注入） ----------

    def _known_groups(self) -> list:
        try:
            return sorted({int(g) for g in (self._group_ids() or [])})
        except Exception as e:
            logger.warning(f"[Schedule] 取群列表失败: {e}")
            return []

    def _idle_minutes(self, group_id: int, now: float) -> float:
        last = self._last_activity(group_id) or 0.0
        return max(0.0, (now - last) / 60) if last else 0.0

    async def _member_count_cached(self, group_id: int, now: float) -> Optional[int]:
        """群成员数（带 10 分钟缓存；取不到返回 None，调用方自行兜底）"""
        hit = self._members.get(group_id)
        if hit and (now - hit[1]) < _MEMBER_CACHE_TTL:
            return hit[0]
        if self._member_count is None:
            return None
        try:
            n = await self._member_count(group_id)
        except Exception:
            n = None
        if isinstance(n, int):
            self._members[group_id] = (n, now)
        return n

    # ---------- tick ----------

    async def tick(self, now: Optional[float] = None) -> list:
        """扫一遍并执行到期任务；返回本轮摘要（测试/干跑直接调用它）"""
        now = now if now is not None else time.time()
        results: list[dict] = []
        ordered = sorted(self.tasks.values(), key=lambda t: t.priority)

        for group_id in self._known_groups():
            group_cfg = self.group_config.get(group_id)
            if self.settle_backoff(group_id, now):
                self.state.save(group_id)
            proactive_done = False

            for task in ordered:
                cfg = task.effective_config(group_cfg)
                if not cfg.get("enabled"):
                    continue
                # 【D15】主动打扰名单
                if task.is_proactive and Config.PROACTIVE_GROUPS and group_id not in Config.PROACTIVE_GROUPS:
                    self.state.record_skip(group_id, task.name, "不在主动打扰名单")
                    continue

                reason = await self._gate(group_id, task, cfg, group_cfg, now)
                if reason:
                    if reason != "not_due":            # 未到点不记，避免日志噪音
                        self.state.record_skip(group_id, task.name, reason)
                    continue

                # 同群互斥：本轮只跑一个主动任务，且与上次主动消息至少隔 1 分钟
                if task.is_proactive:
                    last = self._last_proactive.get(group_id, 0.0)
                    if proactive_done or (last and (now - last) < 60):
                        self.state.record_skip(group_id, task.name, "同群互斥（刚有过主动消息）")
                        continue

                res = await self._execute(task, group_id, cfg, group_cfg, now, dry_run=False)
                results.append({"group_id": group_id, "task": task.name,
                                "status": res.status, "reason": res.reason})
                if res.status in ("sent", "done") and task.is_proactive:
                    self._last_proactive[group_id] = now
                    proactive_done = True
        return results

    async def _gate(self, group_id: int, task: Task, cfg: dict, group_cfg: dict, now: float) -> str:
        """返回非空 = 被拦下的原因（"not_due" 表示只是没到点，不算被拦）"""
        # 静默时段（跨午夜由 in_quiet_hours 处理）
        if in_quiet_hours(now, group_cfg.get("quiet_hours") or ""):
            return "静默时段"
        # 任务自定义工作时段（破冰沿用 ICEBREAK_WORK_START/END）
        wh = cfg.get("work_hours")
        if wh and len(wh) == 2:
            hour = _clock.from_timestamp(now).hour
            if not (int(wh[0]) <= hour < int(wh[1])):
                return f"不在工作时段 {wh[0]}:00-{wh[1]}:00"
        # 每任务每日上限
        limit = int(cfg.get("daily_limit") or 0)
        if limit and self.state.count_today(group_id, task.name, now) >= limit:
            return f"已达任务每日上限 {limit}"
        # 每群每日总预算（最后一道闸）
        total_limit = int(group_cfg.get("daily_total_limit") or 0)
        if task.is_proactive and total_limit and self.state.budget_used_today(group_id, now) >= total_limit:
            return f"已达每群每日总上限 {total_limit}"
        # 冷却（冷却中视为"没到点"：既不执行，也不写"被拦"）
        cooldown = int(cfg.get("cooldown_minutes") or 0)
        ts = self.state.task_state(group_id, task.name)
        last_run = float(ts.get("last_run") or 0.0)
        # 脏数据/时钟回拨保护：last_run 落在未来时，`now - last_run` 会变负数 →
        # 任务是**永远卡在冷却里**。宁可当成"没跑过"。
        if last_run > now + 60:
            logger.warning(
                f"[Schedule] 任务 {task.name} 群 {group_id} 的 last_run 在未来"
                f"（{(last_run - now) / 60:.1f} 分钟后），按未执行处理"
            )
            last_run = 0.0
            ts["last_run"] = 0.0
        if cooldown and last_run and (now - last_run) < cooldown * 60:
            return "not_due"
        # 【P2c-hotfix-4】连续失败的指数退避（2026-10-08 线上事故）
        # 原来任务失败不推进 last_run → interval/cron 每个 tick 都重试；
        # 配合 LLM 15 秒超时就变成"45 秒一轮猛打 LLM"，把自己打成被限流。
        # 现在：失败也推进 last_run（record_error 里做），这里再按连续失败次数放大间隔。
        fail_count = int(ts.get("fail_count", 0) or 0)
        if fail_count > 0 and last_run:
            base_min = max(int(Config.TASK_FAIL_BACKOFF_BASE_MINUTES),
                           int(cfg.get("cooldown_minutes") or 0),
                           int((cfg.get("trigger") or {}).get("minutes") or 0))
            wait_min = min(base_min * (2 ** min(fail_count - 1, 5)),
                           int(Config.TASK_FAIL_BACKOFF_MAX_MINUTES))
            waited = (now - last_run) / 60
            if waited < wait_min:
                return f"上次失败，退避中（连续 {fail_count} 次，还需 {wait_min - waited:.0f} 分钟）"
        # 群规模
        min_size = int(cfg.get("min_group_size") or 0)
        if min_size:
            members = await self._member_count_cached(group_id, now)
            if members is not None and members < min_size:
                return f"群人数不足（{members} < {min_size}）"
        # 群活跃前置（提醒类任务：死群不打扰）
        active_within = int(cfg.get("require_active_within_minutes") or 0)
        if active_within and (now - (self._last_activity(group_id) or 0)) > active_within * 60:
            return f"群最近 {active_within} 分钟不活跃"
        # 退避 + 触发器判定
        trigger = task.build_trigger(cfg)
        if trigger is None:
            return "无有效触发器"
        idle = self._backoff_gate(group_id, task, cfg, self._idle_minutes(group_id, now))
        if not trigger.due(now=now, last_run=last_run, idle_minutes=idle, task_state=ts):
            return "not_due"
        return ""

    # ---------- 退避（"没人理我"的自我克制） ----------

    def _backoff_gate(self, group_id: int, task: Task, cfg: dict, idle_minutes: float) -> float:
        """把退避级别折算成"等效静默分钟数"。

        没人应答 → 门槛翻倍，所以等效静默时间要按 factor^level **缩小**，
        这样 `IdleTrigger` 自身的判定逻辑完全不用改。
        """
        bo = cfg.get("backoff") or {}
        if not bo.get("on_no_reply"):
            return idle_minutes
        level = int(self.state.task_state(group_id, task.name).get("backoff_level") or 0)
        if not level:
            return idle_minutes
        factor = max(2, int(bo.get("factor", 2)))
        return idle_minutes / (factor ** level)

    def settle_backoff(self, group_id: int, now: Optional[float] = None) -> bool:
        """有群友说话 → 结束"等待应答"状态；返回是否真的有变化（避免每 tick 都写盘）

        退避设置以**已注册任务的有效配置**为准（而不是只看群配置文件）——
        否则任务自身 `defaults` 里声明的 backoff 会被忽略（干跑测试抓到过）。
        """
        now = now if now is not None else time.time()
        last_activity = self._last_activity(group_id) or 0.0
        group_cfg = self.group_config.get(group_id)
        changed = False
        for name, task in self.tasks.items():
            ts = self.state.task_state(group_id, name)
            pending = float(ts.get("pending_check_ts") or 0.0)
            if not pending:
                continue
            if last_activity > pending:
                self.state.reset_backoff(group_id, name)
                changed = True
                continue
            if (now - pending) >= Config.NO_REPLY_GRACE_MINUTES * 60:
                bo = (task.effective_config(group_cfg).get("backoff") or {})
                if bo.get("on_no_reply"):
                    level = self.state.bump_backoff(group_id, name, int(bo.get("max_level", 3)))
                    changed = True
                    logger.info(
                        f"[Schedule] 任务 {name} 群 {group_id} 上次没人应答 → 退避级别 {level}"
                        f"（门槛 ×{max(2, int(bo.get('factor', 2))) ** level}）"
                    )
                else:
                    ts["pending_check_ts"] = 0.0        # 不参与退避的任务直接清掉
                    changed = True
        return changed

    # ---------- 执行 ----------

    async def _execute(self, task: Task, group_id: int, cfg: dict, group_cfg: dict,
                       now: float, *, dry_run: bool, manual: bool = False) -> TaskResult:
        ctx = TaskContext(
            group_id=group_id, now=now, dry_run=dry_run, config=cfg,
            state=dict(self.state.task_state(group_id, task.name)),
            group_config=group_cfg, idle_minutes=self._idle_minutes(group_id, now),
            extra={"send": self._send, "scheduler": self},
        )
        res = await task.execute(ctx)
        self._log_task(group_id, task.name, res, manual=manual)
        if not dry_run:
            # ⚠️ 必须把 tick 的 now 传下去：否则 last_run 记的是真实时钟、而调度用的是传入时间，
            # 冷却/当日计数会错位（干跑测试就是这么抓到它的）。
            if res.status in ("sent", "done"):
                self.state.record_run(group_id, task.name, result=res.status, content=res.content,
                                      fingerprint_value=res.fingerprint_value, now=now,
                                      count_budget=task.is_proactive)
                # 【P2c-8】按"内容类型"（= 任务名）记一条主动消息，用于统计"哪类内容有人接"
                if res.status == "sent" and task.is_proactive:
                    try:
                        from app import metrics as _metrics
                        _metrics.get().record_proactive(group_id, task.name)
                    except Exception as e:
                        logger.debug(f"[Schedule] 指标记录失败（不影响发送）: {e}")
            elif res.status == "empty":
                self.state.record_skip(group_id, task.name, res.reason or "内容为空", now=now)
            elif res.status == "error":
                # ⚠️ 必须传 now：失败也要推进 last_run，否则下一个 tick 立刻重试（线上事故根因）
                self.state.record_error(group_id, task.name, res.reason, now=now)
            self.state.save(group_id)
        return res

    # ---------- 手动触发 / 干跑（命令与面板共用，不复制逻辑） ----------

    async def run_task(self, task_name: str, group_id: int, *, dry_run: bool = False,
                       force: bool = False, now: Optional[float] = None) -> TaskResult:
        """手动跑一个任务。

        - `dry_run=True`（试跑）：只生成内容 —— 不发送、不写状态、不计预算、不动冷却；
        - `dry_run=False`（立即执行）：真发一次并正常记账（用于补播）。

        **立即执行也要过安全闸门**（2026-10-08 修）：以前它谁都不问，结果管理员在测试群里
        `/任务 执行 icebreak` 就把破冰内容真发进了不该被打扰的群。现在：
          · **D15 白名单是硬闸门**（主动任务在非白名单群一律拒绝，`force` 也无效）；
          · 静默时段默认拒绝，除非显式 `force=True`（管理员半夜补播的合法场景）；
          · 每日上限/冷却**不拦**手动执行 —— "补播"本来就是自动化被限额挡住时的兜底。
        """
        task = self.tasks.get(task_name)
        if task is None:
            return TaskResult(status="error", reason=f"未知任务: {task_name}")
        group_cfg = self.group_config.get(group_id)
        cfg = task.effective_config(group_cfg)
        now = now if now is not None else time.time()

        if not dry_run:
            blocked = self._manual_gate(task, group_id, group_cfg, now, force=force)
            if blocked:
                logger.warning(f"[Schedule] 手动执行被拒：任务 {task_name} 群 {group_id} → {blocked}")
                self.state.record_skip(group_id, task_name, blocked, now=now)
                self.state.save(group_id)
                return TaskResult(status="blocked", reason=blocked)

        res = await self._execute(task, group_id, cfg, group_cfg, now,
                                 dry_run=dry_run, manual=True)
        if not dry_run and res.status in ("sent", "done") and task.is_proactive:
            self._last_proactive[group_id] = now
        return res

    def _manual_gate(self, task: Task, group_id: int, group_cfg: dict,
                     now: float, *, force: bool = False) -> str:
        """手动"立即执行"的安全闸门；返回非空 = 拒绝原因"""
        # 【D15】硬闸门：主动任务绝不在非白名单群发送（手动也不行，force 也不放行）
        if task.is_proactive and Config.PROACTIVE_GROUPS and group_id not in Config.PROACTIVE_GROUPS:
            return (f"该群不在主动打扰名单（PROACTIVE_GROUPS）里，"
                    f"主动任务不允许手动执行 —— 这是硬限制，用 force 也不行")
        # 静默时段：默认拒绝，管理员显式 force 才放行（半夜补播的合法场景）
        quiet = group_cfg.get("quiet_hours") or ""
        if task.is_proactive and in_quiet_hours(now, quiet) and not force:
            return (f"当前处于静默时段（{quiet}）；确认要补发请用「/任务 执行 {task.name} 强制」")
        return ""

    # ---------- 状态展示（命令 / 面板） ----------

    def status(self, group_id: Optional[int] = None) -> dict:
        gids = [group_id] if group_id else self._known_groups()
        now = time.time()
        out = {}
        for gid in gids:
            group_cfg = self.group_config.get(gid)
            idle = self._idle_minutes(gid, now)
            items = {}
            for name, task in sorted(self.tasks.items(), key=lambda kv: kv[1].priority):
                cfg = task.effective_config(group_cfg)
                ts = self.state.task_state(gid, name)
                trigger = task.build_trigger(cfg)
                last_run = float(ts.get("last_run") or 0)
                items[name] = {
                    "label": task.label or name,
                    "proactive": task.is_proactive,
                    "enabled": bool(cfg.get("enabled")),
                    "trigger": trigger.describe() if trigger else "无",
                    "next_eta": (trigger.next_eta(now=now, last_run=last_run,
                                                  idle_minutes=self._backoff_gate(gid, task, cfg, idle))
                                 if trigger else None),
                    "last_run": last_run,
                    "count_today": self.state.count_today(gid, name, now),
                    "daily_limit": int(cfg.get("daily_limit") or 0),
                    "cooldown_minutes": int(cfg.get("cooldown_minutes") or 0),
                    "backoff_level": int(ts.get("backoff_level") or 0),
                    "last_result": ts.get("last_result") or "",
                    "last_skip_reason": ts.get("last_skip_reason") or "",
                }
            out[gid] = {
                "idle_minutes": round(idle, 1),
                "budget_used": self.state.budget_used_today(gid, now),
                "budget_limit": int(group_cfg.get("daily_total_limit") or 0),
                "quiet_hours": group_cfg.get("quiet_hours"),
                "in_quiet": in_quiet_hours(now, group_cfg.get("quiet_hours") or ""),
                "proactive_allowed": (not Config.PROACTIVE_GROUPS) or (gid in Config.PROACTIVE_GROUPS),
                "tasks": items,
            }
        return out

    def recent_log(self, group_id: int, limit: int = 20) -> list:
        path = self._log_dir / f"{group_id}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    # ---------- 内部 ----------

    def _log_task(self, group_id: int, task_name: str, res: TaskResult, manual: bool = False) -> None:
        entry = {
            "ts": time.time(),
            "day": today_key(),
            "task": task_name,
            "status": res.status,
            "reason": res.reason,
            "manual": manual,
            "parts": len(res.parts),
            "preview": (res.parts[0][:80] if res.parts else ""),
            "fingerprint": res.fingerprint_value,
            "elapsed": round(res.elapsed, 2),
        }
        try:
            path = self._log_dir / f"{group_id}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if path.stat().st_size > 200_000:
                self._prune_log(path)
        except OSError as e:
            logger.warning(f"[Schedule] 任务记录写入失败: {e}")

    @staticmethod
    def _prune_log(path: Path) -> None:
        try:
            cutoff = time.time() - Config.TASK_LOG_KEEP_DAYS * 86400
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("ts", 0) >= cutoff:
                        kept.append(line)
                except Exception:
                    continue
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except OSError as e:
            logger.warning(f"[Schedule] 任务记录裁剪失败: {e}")
