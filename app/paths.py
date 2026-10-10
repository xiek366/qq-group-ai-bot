"""
项目路径常量 —— 全部从文件位置推导，**不依赖进程的工作目录**
============================================================

为什么要单独放一个模块：

云端是 systemd 拉起进程的，工作目录由 unit 里的 `WorkingDirectory=` 决定。
少写这一行（或者从别处手动 `python -m app.main` 启动）时，
`Path("data/memory")` 这种相对路径就会把记忆写到 `/data/memory`、`/root/data/memory`
之类的地方去 —— 表现就是「重启后记忆/画像全没了」，而且很难查。

所以：所有数据/配置路径都从这里取，基于 `__file__` 推导出项目根。
不管从哪个目录启动、用不用 systemd，路径都指向同一份数据。

    PROJECT_ROOT/data/memory/<群号>.json
    PROJECT_ROOT/data/group_style/<群号>.json
    PROJECT_ROOT/data/stickers/{index.json,stk_*.jpg}
    PROJECT_ROOT/data/aliases/<群号>.json
    PROJECT_ROOT/config/{persona.txt,face_dict.json,aliases.json}
"""

from __future__ import annotations

import logging
from pathlib import Path

# app/paths.py → parent = app/ → parent.parent = 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# ---------- 数据目录 ----------
DATA_DIR: Path = PROJECT_ROOT / "data"
MEMORY_DIR: Path = DATA_DIR / "memory"          # 旧版群对话历史（兼容读取 + 迁移源）
STREAM_DIR: Path = DATA_DIR / "stream"          # 【P0】统一对话流 <群ID>.jsonl
STYLE_DIR: Path = DATA_DIR / "group_style"      # 风格画像 + 待总结消息
STICKER_DIR: Path = DATA_DIR / "stickers"       # 表情包库 + index.json
ALIAS_DIR: Path = DATA_DIR / "aliases"          # 群友外号表
IMAGE_DIR: Path = DATA_DIR / "images"           # 生图临时文件
CACHE_DIR: Path = DATA_DIR / "cache"            # 各类缓存
VISION_CACHE_DIR: Path = CACHE_DIR / "vision"   # 【P0】图片描述缓存（按 MD5）
WEATHER_CACHE_DIR: Path = CACHE_DIR / "weather"  # 【P2b】天气缓存（城市 + 日期）
NEWS_CACHE_DIR: Path = DATA_DIR / "news"        # 【P2c】资讯源状态（指纹去重/冷却/条件请求）
NEWS_IMAGE_DIR: Path = DATA_DIR / "news_images"  # 【P2c】每日一图的本地图片
QUOTES_STATE_PATH: Path = CACHE_DIR / "quotes.json"   # 【P2c】每日一句台词池的使用状态
STATS_DIR: Path = DATA_DIR / "stats"            # 【P0】健康卡统计数据
SUMMARY_DIR: Path = DATA_DIR / "summary"        # 【P1】滚动摘要要点 + 用户画像卡
STATE_DIR: Path = DATA_DIR / "state"            # 【P1】群运行时状态（当前话题）
SCHEDULE_DIR: Path = DATA_DIR / "schedule"      # 【P2a】任务状态（每群一个文件：预算 + 每任务进度）
GROUP_CONFIG_DIR: Path = DATA_DIR / "group_config"   # 【P2a】单群覆盖配置 <群ID>.json
TASK_LOG_DIR: Path = DATA_DIR / "tasks"         # 【P2a】任务执行记录（面板用）

# ---------- 配置目录 ----------
CONFIG_DIR: Path = PROJECT_ROOT / "config"
PERSONA_PATH: Path = CONFIG_DIR / "persona.txt"            # 原完整人设（557 行，留作素材，代码优先用核心版）
PERSONA_CORE_PATH: Path = CONFIG_DIR / "persona_core.txt"  # 【P1】常驻核心人设（缺失 → 自动回退到上面那份）
SCENES_DIR: Path = CONFIG_DIR / "scenes"                   # 【P1】场景模板（chat/icebreak/vision_reply/...）
FACE_DICT_PATH: Path = CONFIG_DIR / "face_dict.json"
LEGACY_ALIASES_PATH: Path = CONFIG_DIR / "aliases.json"   # 旧版外号表（兜底兼容）

_DATA_SUBDIRS = ("memory", "stream", "group_style", "stickers", "aliases", "images",
                 "cache", "stats", "summary", "state", "schedule", "group_config", "tasks")


def ensure_dir(path: Path) -> Path:
    """确保目录存在（幂等），返回原路径便于链式使用"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def warn_legacy_relative_data(logger: logging.Logger) -> None:
    """启动时检查「相对工作目录」下的旧数据目录，防止换路径后以为记忆丢了。

    老版本的 data/ 是相对路径（跟着进程工作目录走）。云端如果调过启动方式、
    或者补上了 systemd 的 WorkingDirectory，数据就"看起来没了"。
    这里只提示 + 给出搬迁命令，不自动搬（避免误动用户数据）。
    """
    legacy = Path.cwd() / "data"
    try:
        if legacy.resolve() == DATA_DIR.resolve() or not legacy.is_dir():
            return
    except OSError:
        return

    found = []
    for sub in _DATA_SUBDIRS:
        d = legacy / sub
        try:
            if d.is_dir() and any(d.iterdir()):
                found.append(sub)
        except OSError:
            continue
    if not found:
        return

    logger.warning("=" * 60)
    logger.warning(f"[Paths] 检测到旧位置的数据目录（相对进程工作目录）: {legacy}")
    logger.warning(f"[Paths] 当前实际使用的数据目录: {DATA_DIR}")
    logger.warning("[Paths] 如果那份数据才是你要的，先停服务再搬迁，例如：")
    for sub in found:
        logger.warning(f"    mkdir -p {DATA_DIR / sub} && mv {legacy / sub}/* {DATA_DIR / sub}/")
    logger.warning("[Paths] 搬完 `systemctl restart qqbot` 即可。")
    logger.warning("=" * 60)
