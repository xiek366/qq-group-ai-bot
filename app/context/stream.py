"""
统一对话流 —— 一处写入，处处可读
================================

**为什么要它**：之前只有「@bot 的那几轮」会进 memory，群里平时的聊天、
以及 bot 自己主动说的话（插嘴、破冰、看图回复）都不在任何记忆里。
结果就是 bot 接不上话、说完就忘、无法延续话题。

现在改成：**所有群消息 + bot 自己的全部发言**都追加写进
`data/stream/<群ID>.jsonl`，一行一条 JSON，append-only，永不重写整份文件
（旧 memory 是"读整份 → 改 → 写回整份"，既慢又容易写坏）。

每行结构：

    {"ts": 1791234567.123, "role": "user", "user_id": 888888,
     "nick": "小明", "kind": "chat", "text": "…"}

`kind` 取值：

| kind | 含义 |
|---|---|
| `chat` | 普通群聊 |
| `command` | 命令（/ping /help /看 等） |
| `reply` | @bot、/问 的回复 |
| `proactive` | 主动插嘴、破冰 |
| `vision` | 看图回复 |
| `search` | 搜索总结 |

**线程约定**：所有文件读写都是阻塞调用，异步侧必须用
`await run_blocking(stream.append, ...)`；读写统一走 `app.util.blocking` 的单 worker 线程，
因此不存在并发写同一文件的问题（与 style/sticker 的做法一致）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from app.paths import MEMORY_DIR, STREAM_DIR, ensure_dir

logger = logging.getLogger(__name__)

# 单条文本上限（防止超长消息把窗口撑爆）
_MAX_TEXT = 4000
# 倒读窗口时最多回读的字节数（保护内存）
_MAX_TAIL_BYTES = 512 * 1024
# 时间标记阈值：间隔超过 1 小时才打标记（省 token）
_GAP_MARK_SECONDS = 3600


def _tail_lines(path: Path, limit: int, max_bytes: int = _MAX_TAIL_BYTES) -> list[str]:
    """从文件尾部倒着读，返回最后 limit 行（不足则返回全部）。

    关键点：不做全文件解析，只回读必要的字节块；如果回读时停在了文件中间，
    第一行可能是被截断的半行，必须丢掉。
    """
    if limit <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 64 * 1024
        buf = b""
        pos = size
        while pos > 0 and buf.count(b"\n") <= limit and len(buf) < max_bytes:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf

    text = buf.decode("utf-8", "replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if pos > 0 and lines:
        lines = lines[1:]          # 丢掉可能被截断的首行
    return lines[-limit:]


def _abs_time(ts: float) -> str:
    """绝对时间标记，如 10-06 14:30"""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _time_marker(prev_ts: Optional[float], ts: Optional[float], now: float, first: bool) -> str:
    """按"间隔够大才标记"的策略生成时间前缀（P0-2）。

    - 窗口第一条且已经过去 1 小时以上 → 给绝对时间锚点
    - 与上一条间隔 < 1 小时 → 不打标记（省 token）
    - 间隔 1~24 小时 → `[N小时前]`
    - 间隔 ≥ 24 小时 → `[MM-DD HH:MM]`
    """
    if not ts:
        return ""
    if first:
        return f"[{_abs_time(ts)}]" if (now - ts) >= _GAP_MARK_SECONDS else ""
    if prev_ts is None:
        return ""
    gap = ts - prev_ts
    if gap < _GAP_MARK_SECONDS:
        return ""
    if gap < 24 * 3600:
        return f"[{int(gap // 3600)}小时前]"
    return f"[{_abs_time(ts)}]"


class ConversationStream:
    """按群隔离的 append-only 对话流"""

    def __init__(
        self,
        stream_dir: Optional[Path] = None,
        legacy_memory_dir: Optional[Path] = None,
    ):
        self._dir = ensure_dir(stream_dir or STREAM_DIR)
        self._legacy_dir = legacy_memory_dir or MEMORY_DIR
        # group_id -> 条目数（面板用，避免每次统计都读一遍文件）
        self._counts: dict[int, int] = {}
        self._migrated: set[int] = set()

    # ---------- 路径 ----------

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.jsonl"

    # ---------- 写 ----------

    def append(
        self,
        group_id: int,
        role: str,
        text: str,
        *,
        user_id: Optional[int] = None,
        nick: str = "",
        kind: str = "chat",
        ts: Optional[float] = None,
    ) -> dict:
        """追加一条（阻塞；异步侧用 run_blocking 调用）"""
        entry: dict[str, Any] = {
            "ts": round(ts if ts is not None else time.time(), 3),
            "role": "assistant" if role == "assistant" else "user",
            "kind": kind,
            "text": str(text or "")[:_MAX_TEXT],
        }
        if user_id is not None:
            entry["user_id"] = user_id
        if nick:
            entry["nick"] = nick

        path = self._path(group_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._counts[group_id] = self._counts.get(group_id, 0) + 1

        # 【P0-4】顺带记指标（本函数运行在单 worker 线程里，允许批量落盘；
        # 失败也不影响消息写入）
        try:
            from app import metrics
            metrics.get().record_message(group_id, entry["role"])
        except Exception as e:      # pragma: no cover - 指标不该影响主流程
            logger.debug(f"[Stream] 记录指标失败: {e}")
        return entry

    def clear(self, group_id: int) -> str:
        """清空某群对话流（不删除，改名成 .bak-<时间戳> 备份）"""
        path = self._path(group_id)
        backup = ""
        if path.exists():
            backup = str(path.with_suffix(f".jsonl.bak-{int(time.time())}"))
            try:
                os.replace(path, backup)
            except OSError as e:
                logger.error(f"[Stream] 群 {group_id} 备份失败: {e}")
                backup = ""
        self._counts[group_id] = 0
        logger.info(f"[Stream] 群 {group_id} 对话流已清空（备份: {backup or '无'}）")
        return backup

    # ---------- 读 ----------

    def window(self, group_id: int, limit: int = 20) -> list[dict]:
        """取最近 limit 条（正序返回；阻塞）"""
        path = self._path(group_id)
        if not path.exists():
            self._migrate_if_needed(group_id)
            if not path.exists():
                return []
        entries: list[dict] = []
        for line in _tail_lines(path, limit):
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    def count(self, group_id: int) -> int:
        """条目数（首次统计读一遍文件，之后走缓存）"""
        if group_id in self._counts:
            return self._counts[group_id]
        path = self._path(group_id)
        n = 0
        if path.exists():
            try:
                with open(path, "rb") as f:
                    n = sum(1 for _ in f)
            except OSError:
                n = 0
        self._counts[group_id] = n
        return n

    def groups(self) -> list[int]:
        """当前有对话流的群号"""
        out = []
        if not self._dir.exists():
            return out
        for p in self._dir.iterdir():
            name = p.name
            if not name.endswith(".jsonl"):
                continue
            gid = name[: -len(".jsonl")]
            if gid.isdigit():
                out.append(int(gid))
        return sorted(out)

    def stats(self, group_id: int) -> dict:
        """给面板用的轻量统计"""
        path = self._path(group_id)
        size = path.stat().st_size if path.exists() else 0
        return {"group_id": group_id, "entries": self.count(group_id), "bytes": size}

    # ---------- 转成 OpenAI messages ----------

    @staticmethod
    def to_messages(entries: list[dict], now: Optional[float] = None) -> list[dict]:
        """把流条目转成 LLM 用的 messages（含昵称前缀与稀疏时间标记）"""
        now = time.time() if now is None else now
        messages: list[dict] = []
        prev_ts: Optional[float] = None
        for i, e in enumerate(entries):
            text = e.get("text", "")
            marker = _time_marker(prev_ts, e.get("ts"), now, first=(i == 0))
            if marker:
                text = f"{marker} {text}"
            if e.get("role") == "assistant":
                messages.append({"role": "assistant", "content": text})
            else:
                name = e.get("nick") or f"user_{e.get('user_id', '?')}"
                messages.append({"role": "user", "content": f"{name}: {text}"})
            if e.get("ts"):
                prev_ts = e["ts"]
        return messages

    # ---------- 旧记忆迁移（一次性） ----------

    def migrate_all(self) -> int:
        """扫描旧 memory 目录，把所有群一次性迁进对话流；返回迁移的群数"""
        if not self._legacy_dir.is_dir():
            return 0
        migrated = 0
        for p in sorted(self._legacy_dir.glob("*.json")):
            gid = p.stem
            if not gid.isdigit():
                continue
            if self._migrate_if_needed(int(gid)):
                migrated += 1
        if migrated:
            logger.info(f"[Stream] 旧记忆迁移完成：{migrated} 个群")
        return migrated

    def _migrate_if_needed(self, group_id: int) -> bool:
        """把 data/memory/<gid>.json 迁进对话流；旧文件改名 .migrated 留底"""
        if group_id in self._migrated:
            return False
        self._migrated.add(group_id)

        legacy = self._legacy_dir / f"{group_id}.json"
        if not legacy.exists():
            return False
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Stream] 群 {group_id} 旧记忆解析失败，跳过迁移: {e}")
            return False
        if not isinstance(raw, list) or not raw:
            return False

        # 旧条目没有时间戳 → 用文件 mtime 往前倒推（每条间隔 1 分钟），
        # 保证顺序正确、且相对时间标记不会全是"同一秒"
        try:
            base = legacy.stat().st_mtime
        except OSError:
            base = time.time()
        total = len(raw)
        lines = []
        for i, old in enumerate(raw):
            if not isinstance(old, dict):
                continue
            role = "assistant" if old.get("role") == "assistant" else "user"
            entry = {
                "ts": round(base - (total - i) * 60, 3),
                "role": role,
                "kind": "reply" if role == "assistant" else "chat",
                "text": str(old.get("content", ""))[:_MAX_TEXT],
            }
            if old.get("user_id") is not None:
                entry["user_id"] = old["user_id"]
            if old.get("nickname"):
                entry["nick"] = old["nickname"]
            lines.append(json.dumps(entry, ensure_ascii=False))

        path = self._path(group_id)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            legacy.rename(legacy.with_suffix(".json.migrated"))
        except OSError as e:
            logger.error(f"[Stream] 群 {group_id} 迁移失败: {e}")
            return False

        self._counts.pop(group_id, None)
        logger.info(f"[Stream] 群 {group_id} 旧记忆已迁移 {len(lines)} 条 → {path.name}")
        return True
