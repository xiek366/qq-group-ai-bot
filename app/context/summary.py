"""
滚动摘要 + 用户画像卡（P1-1 / P1-2）
====================================

**解决什么**：对话流窗口只有 `MAX_HISTORY × 2` 条（服务器上是 20 条），一滑过去就忘了。
现在把"滑出窗口的对话"定期压成 ≤5 条长期要点，再把"谁是谁、喜欢什么、最近提过什么"存成画像卡，
两样都在下一轮对话里注入 system prompt —— 于是"上周说过的事"和"这个人是谁"都还在。

**触发方式**（对应 DEVLOG Q2 的答复：40 条 / 10 分钟）
- 热路径（每条群消息）**只碰内存计数**：`note_activity()` → `should_schedule()`，不做任何磁盘 I/O；
- 真正摘要时 `run()`：读对话流窗口 → 挑出"上次摘要之后"的条目 → 一次 LLM 调用
  （用 `config/scenes/summary.txt`）**同时产出【要点】【成员】【当前话题】** → 合并落盘。
- 同一群同时只跑一个摘要任务（`_running` 标记）。

**为什么一次调用产出三段**：省一半钱。代价是要解析半结构化输出 —— 所以解析器写得很宽容
（没识别出段落就把列表行当要点；整段解析失败就保留旧值并告警，绝不写坏数据）。

**文件**：`data/summary/<群ID>.json`

    {"version": 1, "bullets": ["…≤5 条…"], "archived": ["…更早的要点，仍保留但不注入…"],
     "summarized_upto_ts": 1791…, "people": {"100000008": {"nick": "制作者", "note": "…",
     "last_seen": 1791…, "msgs": 12}}, "updated_at": 1791…}

线程约定：所有文件读写都是阻塞调用，异步侧用 `run_blocking` 包；`run()` 本身是 async（要 await LLM），
内部的文件操作通过 `app.util.blocking` 的线程池执行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app import persona
from app.config import Config
from app.paths import SUMMARY_DIR, ensure_dir
from app.util.blocking import run_blocking

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^【(要点|成员|当前话题)】\s*$")
_BULLET_RE = re.compile(r"^\s*(?:\d+[.、)）]|[-*•])\s*(.+?)\s*$")
_PERSON_RE = re.compile(r"^\s*([^:：]{1,24})\s*[:：]\s*(.+?)\s*$")


def parse_summary(text: str) -> dict:
    """宽容解析 LLM 的三段输出 → {"bullets": [...], "people": {nick: note}, "topic": str}

    容错策略：
      - 认不出段落标题时，把"像列表项"的行全部当要点（宁可少记，不要丢光）
      - 成员行必须是 `昵称: 说明` 形式，否则忽略
      - 话题取【当前话题】下的第一行；"无"/"没有" 视为没有话题
    """
    out: dict[str, Any] = {"bullets": [], "people": {}, "topic": ""}
    if not text:
        return out

    section = None
    loose_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _SECTION_RE.match(line.strip())
        if m:
            section = m.group(1)
            continue
        stripped = line.strip()
        if not stripped:
            continue

        if section == "要点":
            b = _BULLET_RE.match(stripped)
            out["bullets"].append((b.group(1) if b else stripped)[:60])
        elif section == "成员":
            p = _PERSON_RE.match(stripped)
            if p:
                nick, note = p.group(1).strip(), p.group(2).strip()[:40]
                if nick and note and not note.startswith(("一句话", "写这个人")):
                    out["people"][nick] = note
        elif section == "当前话题":
            if not out["topic"]:
                out["topic"] = stripped[:40]
        else:
            loose_lines.append(stripped)

    # 完全没有段落标题（模型没按格式来）→ 兜底：只认"像列表项"的行；
    # 如果连列表项都没有、且只有孤零零一行散文，就判定为解析失败（返回全空），
    # 让调用方保留旧记忆 —— 宁可这次不记，也不要把垃圾写进长期记忆。
    if not out["bullets"] and not out["people"] and not out["topic"]:
        listed = []
        for stripped in loose_lines:
            b = _BULLET_RE.match(stripped)
            if b:
                listed.append(b.group(1)[:60])
        if listed:
            out["bullets"] = listed[: Config.SUMMARY_KEEP_BULLETS * 2]
        elif len(loose_lines) >= 2:
            out["bullets"] = [ln[:60] for ln in loose_lines[: Config.SUMMARY_KEEP_BULLETS]]

    if out["topic"] in ("无", "没有", "暂无", "None", "无。", "（无）"):
        out["topic"] = ""

    out["bullets"] = out["bullets"][: Config.SUMMARY_KEEP_BULLETS * 2]
    return out


class ConversationSummary:
    """按群隔离的长期要点 + 画像卡"""

    def __init__(self, summary_dir: Optional[Path] = None):
        self._dir = ensure_dir(summary_dir or SUMMARY_DIR)
        self._lock = threading.RLock()
        self._cache: dict[int, dict] = {}
        # 自上次摘要以来累计的消息数（只在内存里）
        self._pending: dict[int, int] = {}
        self._running: set[int] = set()
        # 是否已经把"历史积压"算进计数（每个群每进程只做一次）
        self._primed: set[int] = set()
        # 最近一次摘要解析出的"当前话题"（由 handler 取走并落到 TopicState）
        self._topic_hint: dict[int, str] = {}

    def pop_topic_hint(self, group_id: int) -> str:
        """取走并清除最近一次摘要里抽出的当前话题（空串表示这次没有话题）"""
        with self._lock:
            return self._topic_hint.pop(group_id, "")

    def is_primed(self, group_id: int) -> bool:
        with self._lock:
            return group_id in self._primed

    def prime(self, group_id: int, backlog: int) -> None:
        """首次见到某个群时，把"历史遗留的未摘要条数"算进计数。

        为什么需要：触发计数原本只统计"本进程启动后收到多少条"，重启即归零。
        实测就出现过——对话流里已经积压 39 条，却要再等 40 条新消息才会摘要；
        如果群一天发不到 40 条、bot 又每天重启，**摘要会永远不跑**（记忆分层等于没生效）。
        所以每个群第一次出现消息时，先用一次快速尾读算出积压量垫进去（每群每进程只算一次）。
        """
        with self._lock:
            if group_id in self._primed:
                return
            self._primed.add(group_id)
            self._pending[group_id] = self._pending.get(group_id, 0) + max(0, int(backlog))

    # ---------- 触发（热路径只碰内存） ----------

    def note_activity(self, group_id: int, n: int = 1) -> None:
        with self._lock:
            self._pending[group_id] = self._pending.get(group_id, 0) + n

    def should_schedule(self, group_id: int) -> bool:
        if not Config.SUMMARY_ENABLED:
            return False
        with self._lock:
            return (
                self._pending.get(group_id, 0) >= Config.SUMMARY_MIN_MESSAGES
                and group_id not in self._running
            )

    def mark_scheduled(self, group_id: int) -> None:
        """接单：清零计数 + 标记运行中（避免同一群并发摘要）"""
        with self._lock:
            self._pending[group_id] = 0
            self._running.add(group_id)
            self._primed.add(group_id)      # 已经处理过这个群了

    def release(self, group_id: int) -> None:
        with self._lock:
            self._running.discard(group_id)

    def pending_count(self, group_id: int) -> int:
        with self._lock:
            return self._pending.get(group_id, 0)

    # ---------- 持久化 ----------

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.json"

    @staticmethod
    def _blank(group_id: int) -> dict:
        return {
            "version": 1,
            "group_id": group_id,
            "bullets": [],
            "archived": [],
            "summarized_upto_ts": 0.0,
            "people": {},
            "updated_at": 0.0,
        }

    def load(self, group_id: int) -> dict:
        """读（带内存缓存，阻塞调用）"""
        with self._lock:
            cached = self._cache.get(group_id)
        if cached is not None:
            return cached
        state = self._blank(group_id)
        path = self._path(group_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state.update({k: raw[k] for k in raw if k in state})
            except Exception as e:
                logger.warning(f"[Summary] 群 {group_id} 读取失败，从空开始: {e}")
        with self._lock:
            self._cache[group_id] = state
        return state

    def save(self, group_id: int, state: Optional[dict] = None) -> None:
        state = state if state is not None else self.load(group_id)
        path = self._path(group_id)
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.error(f"[Summary] 群 {group_id} 落盘失败: {e}")

    # ---------- 摘要主流程 ----------

    @staticmethod
    def _format_batch(entries: list[dict]) -> str:
        lines = []
        for e in entries:
            who = e.get("nick") or ("爱丽丝" if e.get("role") == "assistant" else f"user_{e.get('user_id', '?')}")
            text = (e.get("text") or "").strip().replace("\n", " ")
            if text:
                lines.append(f"{who}: {text[:200]}")
        return "\n".join(lines)

    def unsummarized(self, group_id: int, entries: list[dict]) -> list[dict]:
        upto = self.load(group_id).get("summarized_upto_ts", 0.0)
        return [e for e in entries if (e.get("ts") or 0) > upto]

    async def run(self, llm, group_id: int, entries: list[dict]) -> Optional[dict]:
        """跑一次摘要。返回新状态；未执行/失败返回 None。

        `entries` 应是从对话流读出的**足够长**的窗口（建议 ≥ SUMMARY_BATCH_MAX 条），
        本方法自己筛出"上次摘要之后"的部分。
        """
        state = self.load(group_id)
        fresh = self.unsummarized(group_id, entries)
        if not fresh:
            logger.debug(f"[Summary] 群 {group_id} 没有新内容需要摘要")
            return None

        last = state.get("updated_at") or 0
        gap_min = (time.time() - last) / 60 if last else 999
        if last and gap_min < Config.SUMMARY_COOLDOWN_MINUTES:
            logger.debug(f"[Summary] 群 {group_id} 冷却中（{gap_min:.1f} 分钟）")
            return None

        batch = fresh[: Config.SUMMARY_BATCH_MAX]
        prompt = persona.load_scene(
            "summary",
            chat=self._format_batch(batch),
            max_bullets=Config.SUMMARY_KEEP_BULLETS,
            old_bullets="\n".join(f"- {b}" for b in state.get("bullets") or []) or "（还没有）",
        )
        if not prompt:
            logger.warning("[Summary] 摘要模板缺失，跳过本次摘要（见 config/scenes/summary.txt）")
            return None

        try:
            reply = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                system_prompt="你是一个负责整理群聊记忆的助手，只输出要求的三段内容。",
                # 【P2c-hotfix-4】摘要请求很大（一次 40+ 条消息），用全局 15 秒会稳定超时 → 单独放宽
                timeout=Config.SUMMARY_LLM_TIMEOUT,
            )
        except Exception as e:
            logger.error(f"[Summary] 群 {group_id} LLM 调用失败: {e}")
            return None

        parsed = parse_summary(reply or "")
        if not parsed["bullets"] and not parsed["people"] and not parsed["topic"]:
            logger.warning(f"[Summary] 群 {group_id} 解析不出任何内容，保留旧记忆：{ (reply or '')[:80]!r}")
            return None

        # --- 合并要点（LLM 已被要求合并旧要点；这里再做一次兜底去重与裁剪）---
        merged: list[str] = []
        for b in parsed["bullets"] + (state.get("bullets") or []):
            b = b.strip()
            if b and not any(b in x or x in b for x in merged):
                merged.append(b)
        archived = list(state.get("archived") or [])
        if len(merged) > Config.SUMMARY_KEEP_BULLETS:
            archived = merged[Config.SUMMARY_KEEP_BULLETS:] + archived
            merged = merged[: Config.SUMMARY_KEEP_BULLETS]
        state["bullets"] = merged
        state["archived"] = archived[: Config.SUMMARY_KEEP_BULLETS * 2]

        # --- 合并画像卡（按 QQ 存，昵称只作展示）---
        people = dict(state.get("people") or {})
        nick_to_qq: dict[str, str] = {}
        for e in batch:
            if e.get("role") == "user" and e.get("nick") and e.get("user_id") is not None:
                nick_to_qq.setdefault(str(e["nick"]), str(e["user_id"]))
        now = time.time()
        for nick, note in parsed["people"].items():
            qq = nick_to_qq.get(nick)
            if not qq:
                continue
            old = people.get(qq) or {}
            people[qq] = {
                "nick": nick,
                "note": note,
                "last_seen": now,
                "msgs": int(old.get("msgs", 0)) + 1,
            }
        # 只留最近活跃的 PEOPLE_MAX 个
        if len(people) > Config.PEOPLE_MAX:
            keep = sorted(people.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)
            people = dict(keep[: Config.PEOPLE_MAX])
        state["people"] = people

        state["summarized_upto_ts"] = max(e.get("ts") or 0 for e in batch)
        state["updated_at"] = now
        with self._lock:
            self._cache[group_id] = state
            if parsed["topic"]:
                self._topic_hint[group_id] = parsed["topic"]
        await run_blocking(self.save, group_id, state)

        logger.info(
            f"[Summary] group={group_id} 摘要完成：入参 {len(batch)} 条 → "
            f"要点 {len(state['bullets'])} 条（归档 {len(state['archived'])}）、"
            f"画像 {len(people)} 人、话题={(parsed['topic'] or '无')!r}"
        )
        return state

    # ---------- 给 prompt 用 ----------

    def bullets_text(self, group_id: int) -> str:
        bullets = self.load(group_id).get("bullets") or []
        if not bullets:
            return ""
        return "\n".join(f"- {b}" for b in bullets)

    def people_text(self, group_id: int, limit: Optional[int] = None, current_qq=None) -> str:
        people = self.load(group_id).get("people") or {}
        if not people:
            return ""
        limit = limit or Config.PEOPLE_PROMPT_LIMIT
        items = sorted(
            people.items(),
            key=lambda kv: (str(kv[0]) != str(current_qq), -kv[1].get("last_seen", 0)),
        )
        lines = []
        for qq, info in items[:limit]:
            who = info.get("nick") or qq
            mark = "（就是他/她在说话）" if str(qq) == str(current_qq) else ""
            lines.append(f"- {who}{mark}：{info.get('note', '')}")
        return "\n".join(lines)

    def stats(self, group_id: int) -> dict:
        st = self.load(group_id)
        return {
            "bullets": len(st.get("bullets") or []),
            "archived": len(st.get("archived") or []),
            "people": len(st.get("people") or {}),
            "updated_at": st.get("updated_at", 0.0),
            "pending": self.pending_count(group_id),
        }

    def clear(self, group_id: int, keep_people: bool = False) -> None:
        """清空某群长期记忆（/clear 时一并调用；画像默认一起清）"""
        state = self._blank(group_id)
        if keep_people:
            state["people"] = (self.load(group_id).get("people") or {})
        with self._lock:
            self._cache[group_id] = state
        self.save(group_id, state)
        logger.info(f"[Summary] 群 {group_id} 长期记忆已清空（保留画像={keep_people}）")
