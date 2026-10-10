"""
人设与场景模板加载（P1-5）
==========================

**为什么要拆**：原 `config/persona.txt` 有 557 行，每次调用都要把整份塞进 system prompt，
实测 `tokens_in≈4400` —— 人设吃掉了上下文预算的大半，反而把"真正的对话记忆"挤没了。

现在分两层：

| 层 | 文件 | 何时用 |
|---|---|---|
| 常驻核心 | `config/persona_core.txt`（约 400 字） | 每轮都注入 |
| 场景模板 | `config/scenes/<name>.txt` | 只在对应场景注入（破冰 / 看图 / 摘要 …） |

**安全兜底**：核心文件缺失或为空时，自动回退读原 `persona.txt`（绝不让 bot 变成"没有人设"）。
改动只发生在读取层，原文件一个字节都没动 —— 你想恢复旧行为，删掉 `persona_core.txt` 即可。

模板占位符用 `{name}`；渲染时**只替换认识的名字**，未知的花括号原样保留
（避免把 prompt 里正常的 `{}` 吃掉，也避免 KeyError 把整轮回复搞崩）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.paths import PERSONA_CORE_PATH, PERSONA_PATH, SCENES_DIR

logger = logging.getLogger(__name__)

# 允许的占位符名字（不在白名单里的花括号原样保留）
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_core_cache: Optional[str] = None
_scene_cache: dict[str, str] = {}


def load_core() -> str:
    """常驻核心人设；缺失/为空时回退到原 persona.txt；都没有就用一句兜底"""
    global _core_cache
    if _core_cache is not None:
        return _core_cache

    for path, label in ((PERSONA_CORE_PATH, "核心人设"), (PERSONA_PATH, "原人设(全文)")):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                # 去掉以 # 开头的说明注释行（核心文件顶部有）
                body = "\n".join(
                    line for line in text.splitlines()
                    if not line.lstrip().startswith("#")
                ).strip()
                if body:
                    _core_cache = body
                    if path is PERSONA_PATH:
                        logger.warning(
                            f"[Persona] 未找到 {PERSONA_CORE_PATH.name}，已回退使用完整人设"
                            f"（{len(body)} 字，会占用较多上下文预算；见 DEVLOG [P1-5]）"
                        )
                    else:
                        logger.info(f"[Persona] 已加载{label}: {len(body)} 字")
                    return _core_cache
        except Exception as e:
            logger.error(f"[Persona] 读取 {path} 失败: {e}")

    _core_cache = "你是一个 QQ 群里的 AI 群友。说话自然、简洁、像普通群友，不要编造不确定的信息。"
    logger.warning("[Persona] 人设文件都读不到，已使用最小兜底人设")
    return _core_cache


def load_scene(name: str, **kwargs) -> str:
    """加载并渲染场景模板；文件不存在返回空串（调用方自行决定是否用兜底文案）

    只替换 kwargs 里给出的占位符，其余 `{xxx}` 原样保留。
    """
    raw = _scene_cache.get(name)
    if raw is None:
        path = SCENES_DIR / f"{name}.txt"
        try:
            raw = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            if not raw:
                logger.warning(f"[Persona] 场景模板缺失: {path}（本次不注入该场景说明）")
        except Exception as e:
            logger.error(f"[Persona] 读取场景模板 {path} 失败: {e}")
            raw = ""
        _scene_cache[name] = raw

    if not raw or not kwargs:
        return raw or ""

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        return str(kwargs[key]) if key in kwargs else m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, raw).strip()


def reset_cache() -> None:
    """清缓存（测试与热更新用）"""
    global _core_cache
    _core_cache = None
    _scene_cache.clear()


def available_scenes() -> list[str]:
    if not SCENES_DIR.is_dir():
        return []
    return sorted(p.stem for p in SCENES_DIR.glob("*.txt"))


def describe() -> dict:
    """给启动横幅/健康卡用的概览"""
    core = load_core()
    return {
        "core_chars": len(core),
        "core_source": PERSONA_CORE_PATH.name if PERSONA_CORE_PATH.exists() else PERSONA_PATH.name,
        "scenes": available_scenes(),
    }
