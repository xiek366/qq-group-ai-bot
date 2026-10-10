"""
阻塞调用的异步桥接 —— 别让磁盘 I/O 钉住事件循环
================================================

记忆 / 风格画像 / 表情包 / 外号都用 JSON 文件持久化，
`Path.read_text()`、`write_bytes()`、`os.replace()`、`hashlib` 这些全是**阻塞**调用。
直接在协程里调用它们，事件循环就被钉住了：同一时刻
其他群的消息、WebSocket 心跳、管理面板的 SSE 推送都得排队等磁盘。

这里提供一个专用线程池：

  * **单 worker** —— 所有磁盘操作串行执行。这一点很关键：持久化普遍是
    「读文件 → 改内存 → 整个文件写回」，多线程并发会互相覆盖丢数据；
    单 worker 让这些读-改-写序列天然排成一队，不需要额外的文件锁。
  * `run_blocking()` —— 协程里 `await` 它，事件循环立刻空出来干别的事。

用法：
    from app.util.blocking import run_blocking

    await run_blocking(self.memory.add_user, group_id, uid, nick, content)
    await run_blocking(self.style.update, group_id, profile)

注意：
  * 需要**正在运行**的事件循环；纯同步代码（启动期、测试）请直接调用原函数。
  * 传进 worker 的函数必须是普通同步函数，不能是协程函数。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# max_workers=1：磁盘 I/O 串行化，避免并发读-改-写把 JSON 写坏
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocking-io")


async def run_blocking(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """在专用线程里执行阻塞函数，并 await 它的结果（异常原样抛出）。"""
    loop = asyncio.get_running_loop()
    call: Callable[[], T] = partial(fn, *args, **kwargs)
    return await loop.run_in_executor(_executor, call)


def shutdown_blocking(wait: bool = True) -> None:
    """退出时收线程池。wait=True 会把已经排队的写盘任务做完，避免丢数据。"""
    try:
        _executor.shutdown(wait=wait)
    except Exception as e:  # pragma: no cover - 退出路径，出错也不该影响退出
        logger.warning(f"[Blocking] 线程池关闭失败: {e}")
