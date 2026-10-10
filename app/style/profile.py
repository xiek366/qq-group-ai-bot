"""群风格画像管理 —— 按群持久化、LLM 定期提炼、注入 system prompt"""

from __future__ import annotations

import json
import os
import threading
import time
import logging
from collections import deque
from pathlib import Path
from typing import Optional

from app.paths import STYLE_DIR, ensure_dir
from app.util.blocking import run_blocking

logger = logging.getLogger("app.style")

# 绝对路径（项目根/data/group_style），不跟进程工作目录走 —— 见 app/paths.py
_DATA_DIR = STYLE_DIR

# 触发 LLM 总结的阈值
MIN_MESSAGES_BEFORE_PROFILE = 30   # 至少攒 30 条群消息才第一次生成画像
PROFILE_UPDATE_THRESHOLD = 50      # 攒够 50 条新的 → 再更新一次画像
PROFILE_COOLDOWN = 60 * 60         # 两次总结之间至少间隔 1 小时（避免太频繁）


class StyleProfile:
    """
    每个群一份的"说话风格画像"。

    数据结构（存 JSON）：
    {
      "group_id": 100000005,
      "summary": "本群说话风格偏轻松，喜欢用 emoji，常见话题是编程和摸鱼。",
      "signals": {
        "avg_sentence_len": 12,
        "emoji_per_msg": 0.8,
        "meme_ratio": 0.15,
        "common_phrases": ["666", "离谱", "摸了"]
      },
      "collected_msg_count": 120,      # 累计总结过的消息总数（滚动累加）
      "last_updated": 1748123456       # Unix timestamp
    }
    """

    def __init__(self):
        # group_id -> {pending_messages: deque, last_profile: dict|None, last_summary_time: float}
        self._groups: dict[int, dict] = {}
        # 只保护**内存状态**（外层字典 / 队列），绝不跨文件 I/O 持锁：
        # 事件循环线程也会读这些字段，持锁时间必须停留在微秒级。
        # 文件读写本身由 app.util.blocking 的单 worker 串行化。
        self._lock = threading.RLock()
        ensure_dir(_DATA_DIR)   # 目录在这里建，不在 import 时建
        self._load_all()

    # ---------- 消息收集 ----------

    def add_message(self, group_id: int, nickname: str, text: str):
        """收集一条群消息（供后续 LLM 总结）——同时写内存 + 追加写磁盘

        阻塞（读-改-写整个 JSON 文件），异步侧请用
        `await run_blocking(style.add_message, ...)` 调用。
        """
        state = self._ensure(group_id)
        entry = {
            "nick": nickname,
            "text": text[:200],  # 太长的截断，避免喂给 LLM 时炸 token
            "ts": int(time.time()),
        }
        with self._lock:
            state["pending_messages"].append(entry)
        # 立即追加到磁盘，防止重启丢失
        self._append_pending(group_id, entry)

    def pending_count(self, group_id: int) -> int:
        """这个群还有多少条没总结的消息"""
        state = self._groups.get(group_id)
        if not state:
            return 0
        return len(state["pending_messages"])

    def should_summarize(self, group_id: int) -> bool:
        """是否到了该调用 LLM 总结的时候"""
        state = self._ensure(group_id)
        n_pending = len(state["pending_messages"])
        last = state.get("last_summary_time", 0)
        cooldown_ok = (time.time() - last) >= PROFILE_COOLDOWN

        has_existing = state["last_profile"] is not None
        threshold = PROFILE_UPDATE_THRESHOLD if has_existing else MIN_MESSAGES_BEFORE_PROFILE

        return n_pending >= threshold and cooldown_ok

    def pop_pending(self, group_id: int) -> list[dict]:
        """拿走所有待总结的消息（调用完 LLM 后调用）——同时清空磁盘 pending

        阻塞；异步侧请用 `await run_blocking(style.pop_pending, gid)`。
        """
        state = self._ensure(group_id)
        with self._lock:
            msgs = list(state["pending_messages"])
            state["pending_messages"].clear()
        # 清空磁盘里的 pending（画像保留）
        self._write_pending(group_id, [])
        return msgs

    # ---------- 画像存取 ----------

    def get(self, group_id: int) -> Optional[dict]:
        """拿到最新的风格画像（可能是 None = 还没总结过）"""
        state = self._groups.get(group_id)
        if not state:
            return None
        return state["last_profile"]

    def get_summary(self, group_id: int) -> str:
        """方便方法：直接拿 summary 字符串，没有就返回空串"""
        p = self.get(group_id)
        return (p or {}).get("summary", "")

    async def summarize_with_llm(self, llm, group_id: int) -> Optional[dict]:
        """调 LLM 把最近攒的群聊消息总结成风格画像。返回画像 dict 或 None

        已有旧画像时会把它一起喂给 LLM 做「加权合并」（旧画像权重更高），
        避免画像每次都被最近这几十条消息带偏、只反映"近期风格"。
        """
        state = self._ensure(group_id)
        old_profile = state.get("last_profile")

        # pop_pending 要改内存 + 重写磁盘文件，丢到线程里做
        pending = await run_blocking(self.pop_pending, group_id)
        if not pending:
            return None

        chat_lines = []
        for m in pending:
            chat_lines.append(f"{m['nick']}: {m['text']}")
        chat_text = "\n".join(chat_lines)

        schema = (
            "输出严格的 JSON（不要加 markdown 代码块，不要加解释），格式如下：\n"
            "{\n"
            '  "summary": "一句话总结这个群的说话风格（语气、常用词、话题偏好、句子长短等），100字以内",\n'
            '  "common_phrases": ["最常出现的口语/口头禅/梗，3-5个"],\n'
            '  "emoji_frequency": "high/medium/low",\n'
            '  "avg_sentence_len": 数字（大致平均句长）\n'
            "}\n\n"
        )

        if old_profile:
            # 把旧画像当成"长期基线"一起喂进去，让 LLM 做滚动合并，而不是直接用新消息覆盖
            old_view = {
                k: old_profile.get(k)
                for k in ("summary", "common_phrases", "emoji_frequency", "avg_sentence_len")
            }
            prompt = (
                "你是一个群聊分析师，正在维护某个群的「长期说话风格画像」。\n"
                "你的任务是：以【已有画像】为基线做滚动更新，把【新增消息】合并进来。\n\n"
                "合并原则（很重要，必须遵守）：\n"
                "1. 已有画像代表长期稳定风格，权重更高；不要被最近少数消息带偏。\n"
                "2. 只有当新增消息里出现反复且方向一致的证据（超过半数消息都如此）时，"
                "才修改旧画像的对应特征；否则原样保留。\n"
                "3. common_phrases 优先保留旧的常用语；新口头禅必须反复出现才加入，总数控制在 3-5 个。\n"
                "4. summary 要概括「长期风格」，不要写成最近在聊什么话题的流水账。\n"
                "5. avg_sentence_len / emoji_frequency 取长期倾向，新增消息只做微调。\n\n"
                + schema
                + f"【已有画像（权重高，长期基线）】\n{json.dumps(old_view, ensure_ascii=False)}\n\n"
                f"【新增群聊消息（{len(pending)}条，权重低）】\n{chat_text}"
            )
        else:
            prompt = (
                "你是一个群聊分析师。请分析以下群聊消息，总结这个群的说话风格。\n"
                + schema
                + f"群聊消息（{len(pending)}条）：\n{chat_text}"
            )

        try:
            raw = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            if not raw:
                return None
            import re as _re
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            m = _re.search(r"\{[\s\S]*\}", text)
            if not m:
                logger.warning(f"[Style] LLM 输出不是合法 JSON: {text[:100]}")
                return None
            profile = json.loads(m.group(0))
            profile["group_id"] = group_id
            # 画像现在是累积的，条数也跟着累加
            prev_total = (old_profile or {}).get("collected_msg_count", 0)
            profile["collected_msg_count"] = prev_total + len(pending)
            profile["last_updated"] = int(time.time())
            logger.info(
                f"[Style] group={group_id} "
                f"{'已合并旧画像' if old_profile else '首版画像'} "
                f"+{len(pending)}条（累计{profile['collected_msg_count']}条）"
            )
            return profile
        except Exception as e:
            logger.error(f"[Style] LLM 总结失败 group={group_id}: {e}")
            return None

    def update(self, group_id: int, profile: dict):
        """保存 LLM 生成的新画像（阻塞；异步侧请用 run_blocking 调用）"""
        state = self._ensure(group_id)
        with self._lock:
            state["last_profile"] = profile
            state["last_summary_time"] = int(time.time())
        self._save(group_id)
        logger.info(
            f"[Style] group={group_id} 画像已更新: "
            f"{profile.get('summary', '')[:60]}..."
        )

    # ---------- 持久化 ----------

    def _ensure(self, group_id: int) -> dict:
        with self._lock:
            state = self._groups.get(group_id)
        if state is not None:
            return state

        # 缓存未命中才读磁盘 —— 文件 I/O 一律放在锁外，别让持锁时间被磁盘拖长
        disk = self._load(group_id)
        with self._lock:
            state = self._groups.get(group_id)  # 另一个线程可能已加载好
            if state is None:
                if disk:
                    state = {
                        "pending_messages": deque(disk.get("pending_messages", []), maxlen=500),
                        "last_profile": disk.get("profile"),
                        "last_summary_time": disk.get("profile", {}).get("last_updated", 0) if disk.get("profile") else 0,
                    }
                else:
                    state = {
                        "pending_messages": deque(maxlen=500),
                        "last_profile": None,
                        "last_summary_time": 0,
                    }
                self._groups[group_id] = state
            return state

    def _file_path(self, group_id: int) -> Path:
        return _DATA_DIR / f"{group_id}.json"

    def _append_pending(self, group_id: int, entry: dict):
        """把一条新消息追加到磁盘（如果文件存在且是旧格式，先迁移）"""
        try:
            path = self._file_path(group_id)
            # 读现有文件
            if path.exists():
                disk = json.loads(path.read_text(encoding="utf-8"))
            else:
                disk = {}
            # 迁移旧格式：旧版直接存画像 dict，新版嵌套为 {"profile": ..., "pending_messages": [...]}
            if "pending_messages" not in disk and "group_id" in disk:
                disk = {"profile": disk, "pending_messages": []}
            disk.setdefault("pending_messages", []).append(entry)
            # 内存 maxlen=500，磁盘也限制一下
            if len(disk["pending_messages"]) > 500:
                disk["pending_messages"] = disk["pending_messages"][-500:]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[Style] 追加 pending 失败 group={group_id}: {e}")

    def _write_pending(self, group_id: int, pending: list):
        """整体覆盖磁盘 pending 数组（LLM 总结完清空时用）"""
        try:
            path = self._file_path(group_id)
            if path.exists():
                disk = json.loads(path.read_text(encoding="utf-8"))
            else:
                disk = {}
            if "pending_messages" not in disk and "group_id" in disk:
                disk = {"profile": disk, "pending_messages": []}
            disk["pending_messages"] = pending[-500:]  # 兜底：限制 500
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[Style] 写 pending 失败 group={group_id}: {e}")

    def _save(self, group_id: int):
        """保存完整状态（画像 + pending 一起写）"""
        state = self._groups.get(group_id)
        if not state:
            return
        path = self._file_path(group_id)
        disk = {}
        # 保留磁盘上已有的 pending（如果内存里刚被 pop 了，磁盘也已经被 _write_pending 清空了）
        if path.exists():
            try:
                disk = json.loads(path.read_text(encoding="utf-8"))
                if "pending_messages" not in disk:
                    disk["pending_messages"] = []
            except Exception:
                disk = {"pending_messages": []}
        disk["profile"] = state.get("last_profile")
        disk["pending_messages"] = list(state["pending_messages"])
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[Style] 保存画像失败 group={group_id}: {e}")

    def _load(self, group_id: int) -> Optional[dict]:
        """加载磁盘文件——兼容旧版格式（直接存画像 dict）和新版嵌套格式"""
        path = self._file_path(group_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 旧格式：{"group_id": xxx, "summary": "...", ...} → 直接是画像
            if "group_id" in raw and "profile" not in raw and "pending_messages" not in raw:
                return {"profile": raw, "pending_messages": []}
            return raw  # 新版嵌套格式
        except Exception as e:
            logger.error(f"[Style] 加载画像失败 group={group_id}: {e}")
            return None

    def _load_all(self):
        """启动时扫描一下磁盘上的画像文件"""
        if not _DATA_DIR.exists():
            return
        count = 0
        for fname in os.listdir(_DATA_DIR):
            if fname.endswith(".json"):
                gid = int(fname.replace(".json", ""))
                # 触发 lazy load
                self._ensure(gid)
                count += 1
        if count:
            logger.info(f"[Style] 启动加载了 {count} 个群的风格画像")

    def count(self) -> int:
        """有多少个群已经有画像了"""
        with self._lock:
            states = list(self._groups.values())  # 迭代期间可能有线程写入新群
        return sum(1 for s in states if s["last_profile"])
