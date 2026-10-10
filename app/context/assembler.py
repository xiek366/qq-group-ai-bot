"""
上下文预算组装器（P1-4）
========================

**解决什么**：以前 system prompt 是"人设 + 开发者 + 群风格 + 称呼表 + 图片描述"一路拼下去，
窗口又是固定 `MAX_HISTORY × 2` 条 —— 两者都没有预算概念。人设一胖（557 行）就把预算吃光，
真正的对话记忆反而被挤掉。

现在把两件事都变成**显式的字符预算**（没有 tokenizer，所以不假装精确，用字符数 + 保守系数）：

1. `compose_system(sections, budget)`：按**优先级**拼 system prompt；
   超预算时从最低优先级里挑最长的丢/截断，**必须保留的段永不丢**；
2. `trim_messages(messages, budget, min_entries)`：窗口从**最新往旧**塞，塞不下就停；
   但至少保留 `min_entries` 条（Q1 答复：保底 6 条，不能因为预算紧就把最近对话也砍没）。

优先级约定：

| 级别 | 含义 | 例子 |
|---|---|---|
| `MUST` | 永不丢弃（超预算也只截断到硬上限） | 人设核心、当前时间 |
| `HIGH` | 尽量保留，预算不足时按长度截断 | 长期要点、用户画像卡 |
| `LOW` | 预算不足时整段丢弃（丢最长的优先） | 群风格画像、群友称呼表、场景补充说明 |

每次组装返回统计信息，handler 会打一行 `[Context] …`——这就是验收标准里说的
"**人设占用预算可见**"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MUST = 0
HIGH = 1
LOW = 2

# 估算用：1 个汉字按 1 计，1 个英文字符按 0.35 计（粗略但方向正确）
_CJK_RANGE = ((0x3000, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))


def text_chars(text: str) -> int:
    """粗略字符量：中文/emoji 按 1，ASCII 按 0.35（用于预算比较）"""
    if not text:
        return 0
    total = 0.0
    for ch in text:
        code = ord(ch)
        if code < 128:
            total += 0.35
        else:
            total += 1.0
    return int(total + 0.5)


@dataclass
class Section:
    name: str
    text: str
    priority: int = HIGH
    hard_cap: int = 0          # >0 时，无论预算多宽松都截断到这个字符数

    def chars(self) -> int:
        return text_chars(self.text)


@dataclass
class ComposeResult:
    text: str
    stats: dict = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


class ContextAssembler:
    """把若干段文本按预算拼成 system prompt；并裁剪对话窗口"""

    def __init__(self, max_chars: int = 0, min_entries: int = 0):
        from app.config import Config
        self.max_chars = max_chars or Config.CONTEXT_MAX_CHARS
        self.min_entries = min_entries or Config.CONTEXT_MIN_ENTRIES

    # ---------- system prompt ----------

    def compose_system(self, sections: list[Section], budget: int = 0) -> ComposeResult:
        budget = budget or self.max_chars

        # 1) 先应用硬上限
        prepared: list[Section] = []
        truncated: list[str] = []
        for s in sections:
            if not s.text:
                continue
            if s.hard_cap and s.chars() > s.hard_cap:
                prepared.append(Section(s.name, self._clip(s.text, s.hard_cap), s.priority, s.hard_cap))
                truncated.append(s.name)
            else:
                prepared.append(s)

        # 2) 超预算 → 先丢 LOW（最长的先丢），再截断 HIGH
        dropped: list[str] = []
        total = sum(s.chars() for s in prepared)
        if total > budget:
            lows = sorted([s for s in prepared if s.priority == LOW], key=lambda s: -s.chars())
            for s in lows:
                if total <= budget:
                    break
                prepared.remove(s)
                dropped.append(s.name)
                total -= s.chars()

        if total > budget:
            highs = sorted([s for s in prepared if s.priority == HIGH], key=lambda s: -s.chars())
            for s in highs:
                if total <= budget:
                    break
                # 每个 HIGH 段最多留 40% 预算，超出部分截断
                cap = max(120, int(budget * 0.4))
                if s.chars() <= cap:
                    continue
                idx = prepared.index(s)
                new_text = self._clip(s.text, cap)
                prepared[idx] = Section(s.name, new_text, s.priority, s.hard_cap)
                truncated.append(s.name)
                total = sum(x.chars() for x in prepared)

        text = "\n\n".join(s.text.strip() for s in prepared if s.text.strip())
        # 「合计」按**各段之和**算：段之间的 \n\n 分隔符（每个约 0.7 字）不计入，
        # 否则 300 字预算会因 2 个换行被误判成"超预算"，天天刷无意义的告警。
        total_chars = sum(s.chars() for s in prepared)
        stats = {"合计": total_chars, "预算": budget}
        for s in prepared:
            stats[s.name] = s.chars()
        # 极端情况：MUST 段本身就把预算吃超了（人设太长 / 预算调太小）。
        # 这时不能丢 MUST（丢了就没有人设），但必须让运维看得见 —— 打警告并在统计里标出来。
        if total_chars > budget:
            over = total_chars - budget
            stats["超预算"] = over
            logger.warning(
                f"[Context] 预算不足：仅必留段落就占 {total_chars} 字 > 预算 {budget} 字（超 {over}）。"
                f"建议精简 config/persona_core.txt 或调大 CONTEXT_MAX_CHARS"
            )
        return ComposeResult(text=text, stats=stats, dropped=dropped, truncated=truncated)

    # ---------- 对话窗口 ----------

    def trim_messages(self, messages: list[dict], budget: int, min_entries: int = 0) -> tuple[list[dict], int]:
        """从最新往旧塞，返回 (裁剪后的 messages, 占用字符数)

        `messages` 是正序（旧 → 新）。保底 `min_entries` 条一定会保留（即使超预算）。
        """
        min_entries = min_entries or self.min_entries
        if not messages:
            return [], 0

        scored = [(m, text_chars(str(m.get("content", "")))) for m in messages]
        kept: list[tuple[dict, int]] = []
        used = 0
        for m, n in reversed(scored):                 # 从最新开始
            if kept and used + n > budget and len(kept) >= min_entries:
                break
            kept.append((m, n))
            used += n
        kept.reverse()
        return [m for m, _ in kept], used

    # ---------- 工具 ----------

    @staticmethod
    def _clip(text: str, cap: int) -> str:
        """按估算字符量截断到 cap（省略号也算在 cap 之内，否则会稳定超出 1 字）"""
        if text_chars(text) <= cap:
            return text
        inner = max(0, cap - 1)          # 给结尾的 … 留 1 字
        out = []
        used = 0.0
        for ch in text:
            cost = 0.35 if ord(ch) < 128 else 1.0
            if used + cost > inner:
                break
            out.append(ch)
            used += cost
        return "".join(out).rstrip() + "…"

    @staticmethod
    def format_stats(stats: dict, dropped: list[str], truncated: list[str], window_entries: int = 0,
                     window_chars: int = 0) -> str:
        """一行给日志看（验收标准：人设占用预算可见）"""
        parts = [f"{k}={v}" for k, v in stats.items() if k not in ("合计", "预算")]
        if window_entries:
            parts.append(f"窗口={window_entries}条/{window_chars}字")
        line = " ".join(parts)
        total = stats.get("合计", 0) + window_chars
        line += f" 合计={total}/{stats.get('预算', 0)}"
        if dropped:
            line += f" 丢弃={','.join(dropped)}"
        if truncated:
            line += f" 截断={','.join(truncated)}"
        return line
