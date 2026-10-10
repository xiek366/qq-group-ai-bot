"""任务调度框架（P2a）

三层解耦：`Scheduler`（什么时候做）→ `Task`（做什么/能不能做）→ `ContentSource`（内容从哪来）。

对外只需要两样东西：

    from app.schedule import Scheduler, Task, CallableSource
    sched = Scheduler(send=..., group_ids=..., last_activity=..., member_count=...)
    sched.register(IcebreakTask(handler_like))
    sched.start()
"""

from app.schedule.group_config import GroupConfigStore, env_task_defaults
from app.schedule.scheduler import Scheduler
from app.schedule.state import TaskStateStore, fingerprint, today_key
from app.schedule.task import (
    PRIORITY_BROADCAST,
    PRIORITY_DAILY,
    PRIORITY_EVENT,
    PRIORITY_ICEBREAK,
    PRIORITY_MAINTENANCE,
    PRIORITY_REMINDER,
    CallableSource,
    ContentSource,
    Task,
    TaskContext,
    TaskResult,
)
from app.schedule.triggers import (
    CronTrigger,
    EventTrigger,
    IdleTrigger,
    IntervalTrigger,
    Trigger,
    build_trigger,
    in_quiet_hours,
    in_window,
)

__all__ = [
    "Scheduler", "Task", "TaskContext", "TaskResult", "CallableSource", "ContentSource",
    "TaskStateStore", "GroupConfigStore", "env_task_defaults", "fingerprint", "today_key",
    "Trigger", "IdleTrigger", "IntervalTrigger", "CronTrigger", "EventTrigger", "build_trigger",
    "in_quiet_hours", "in_window",
    "PRIORITY_EVENT", "PRIORITY_BROADCAST", "PRIORITY_DAILY", "PRIORITY_REMINDER",
    "PRIORITY_ICEBREAK", "PRIORITY_MAINTENANCE",
]
