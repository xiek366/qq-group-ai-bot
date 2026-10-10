"""
QQ群 AI 聊天机器人 —— 主入口
============================
启动 OneBot WebSocket 客户端，接收群消息，分发给 handler。

启动前准备:
  1. 复制 .env.example 为 .env 并填入配置
  2. pip install -r requirements.txt
  3. 启动 NapCat / OneBot
  4. python -m app.main
"""

import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

# Windows 下 PowerShell 与 ProactorEventLoop 冲突，用 SelectEventLoop 绕过
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 同时兼容两种启动方式:
#   python -m app.main      (模块方式，项目根自动在 path 里)
#   python app/main.py      (脚本直接运行，此时项目根不在 path，需要补上)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import paths
from app.bot.onebot import OneBotClient
from app.bot.handler import MessageHandler
from app.bot.message import parse_message
from app.config import Config, load_persona
from app.llm.provider import OpenAICompatibleProvider
from app.search.provider import SearXNGProvider
from app.memory.memory import GroupMemory
from app.util import clock
from app.util.blocking import shutdown_blocking


def _log_time_converter(created: float):
    """把日志时间戳换成本地时区（Config.TIMEZONE）的 struct_time。

    ⚠️ 这个函数**必须用 staticmethod 包一层**再挂到 `logging.Formatter.converter`：
    直接赋一个普通函数会被当成方法绑定，于是 `self.converter(record.created)`
    会传进 (self, ts) 两个参数 → 每打一条日志就抛
    `TypeError: ... takes 1 positional argument but 2 were given`，
    logging 只能把 "--- Logging error ---" 打到 stderr（2026-10-07 线上踩过）。
    """
    return clock.from_timestamp(created).timetuple()


def setup_logging():
    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")
    # 日志时间戳也按 Config.TIMEZONE 走：
    # 云端服务器常是 UTC，不统一的话日志时间会比业务时间早 8 小时，查问题时对不上
    logging.Formatter.converter = staticmethod(_log_time_converter)
    # 屏蔽第三方库的噪音日志
    for name in ("websockets", "httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # aiohttp 访问日志降级（INFO 太吵，ERROR 才值得看）
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def log_environment():
    """启动自检 —— 云端最常见的几个坑（工作目录/时区/面板暴露）一眼可见

    出问题时用户只要看 journalctl 开头这几行就能定位，不用猜。
    """
    import platform

    log = logging.getLogger("Bot")
    log.info(f"   cwd        : {os.getcwd()}")
    log.info(f"   project    : {paths.PROJECT_ROOT}")
    log.info(f"   data       : {paths.DATA_DIR}")
    log.info(f"   timezone   : {clock.tz_label()}（服务器本地时间 {dt.datetime.now().strftime('%H:%M')}）")
    log.info(f"   python     : {platform.python_version()} / {platform.system()}")

    if Path.cwd().resolve() != paths.PROJECT_ROOT:
        log.warning(
            f"[Paths] 进程工作目录不是项目根（{os.getcwd()}）—— "
            f"数据路径已按项目根解析，但建议在 systemd unit 里加 WorkingDirectory={paths.PROJECT_ROOT}"
        )
    paths.warn_legacy_relative_data(log)


def build_handler():
    """组装所有模块，返回 MessageHandler 实例"""
    persona = load_persona()
    memory = GroupMemory(max_history=Config.MAX_HISTORY)

    # 【D15】主动/定时打扰的群范围 —— 启动时明确打出来，避免"以为只在实例群、其实全群都在打扰"
    if Config.PROACTIVE_GROUPS:
        logging.info(
            f"[Init] 主动打扰范围: {', '.join(str(g) for g in Config.PROACTIVE_GROUPS)}"
            f"（其余群只做被动响应：@bot / /问 / /看 / 命令）"
        )
    else:
        logging.warning(
            "[Init] ⚠️ PROACTIVE_GROUPS 未配置：**所有群**都会收到主动消息"
            "（破冰 / 主动插嘴 / 裸图自动回复）。建议在 .env 里只填实例群"
        )

    # 预热表情字典：parse_message 里是懒加载，
    # 不预热的话第一条群消息会在事件循环里同步读 config/face_dict.json
    from app.bot.message import preload_face_dict
    logging.info(f"[Init] 表情字典已预热: {preload_face_dict()} 条")

    # LLM —— 如果没配 API Key 就置为 None，handler 会给占位回复
    llm = None
    if Config.LLM_API_KEY:
        try:
            llm = OpenAICompatibleProvider()
            logging.info(f"[Init] LLM 已加载: {Config.LLM_MODEL}")
        except Exception as e:
            logging.error(f"[Init] LLM 加载失败: {e}")

    # Search
    search = SearXNGProvider()
    logging.info(f"[Init] Search 已加载: SearXNG → {Config.SEARXNG_URL}")

    # 【P0-1】旧记忆一次性迁移进统一对话流（幂等；旧文件会改名 .migrated 留底）
    try:
        migrated = memory.stream.migrate_all()
        logging.info(f"[Init] 旧记忆迁移：{migrated} 个群（其余已迁移或无旧数据）")
    except Exception as e:
        logging.error(f"[Init] 旧记忆迁移失败（不影响启动）: {e}")

    # OneBotClient —— 先占位，后面再设置 ws
    onebot = OneBotClient(on_group_message=None)

    handler = MessageHandler(
        onebot=onebot,
        llm_provider=llm,
        search_provider=search,
        memory=memory,
        persona=persona,
    )
    # 把回调补上
    onebot.on_group_message = _make_callback(handler)

    return onebot, handler


def _make_callback(handler: MessageHandler):
    """把 async handler.handle 包成 async 回调（OneBotClient 里会 await 它）"""

    async def callback(event: dict):
        parsed = parse_message(event)
        if parsed is None:
            return
        await handler.handle(parsed)

    return callback


def log_startup_banner(log: logging.Logger):
    """启动横幅 —— 同时也是**配置自查面板**。

    2026-10-07 的教训：改了 `.env` 却没法从日志确认是否生效（面板 token 那次就白折腾了一轮）。
    所以这里把关键开关都打出来：思考模式（影响 temperature 与费用）、接话概率与关键词、
    视觉参数、主动打扰范围。看日志就能确认配置，不用再登服务器翻文件。
    """
    log.info("=" * 60)
    log.info("🤖 QQ群 AI 聊天机器人 MVP 启动中...")
    log.info(f"   OneBot WS  : {Config.ONEBOT_WS_URL}")
    log.info(f"   Bot Name   : {Config.BOT_NAME}")
    log.info(f"   Bot QQ     : {Config.BOT_QQ or '(未配置)'}")
    log.info(f"   LLM        : {Config.LLM_MODEL if Config.LLM_API_KEY else '(未配置)'}")

    thinking_off = Config.LLM_THINKING == "disabled"
    log.info(
        f"   Thinking   : {Config.LLM_THINKING}"
        + ("（temperature 生效）" if thinking_off else "（temperature 不生效，官方限制；想省钱/提速可设 disabled）")
    )
    log.info(
        f"   Chat       : 概率 {Config.CHAT_REPLY_CHANCE}% / 冷却 {Config.CHAT_COOLDOWN}s / "
        f"关键词 {','.join(Config.CHAT_KEYWORDS) if Config.CHAT_KEYWORDS else '(无)'}"
    )
    log.info(
        f"   Vision     : 思考{'关' if Config.VISION_THINKING_DISABLED else '开'} / "
        f"内联≤{Config.VISION_INLINE_MAX_MB}MB / 缓存 {Config.VISION_CACHE_DAYS} 天"
    )
    log.info(
        f"   Proactive  : "
        + (", ".join(str(g) for g in Config.PROACTIVE_GROUPS) if Config.PROACTIVE_GROUPS
           else "未限制（所有群都会被主动打扰）")
    )
    # 【P1-5】人设拆分后，这里能直接看出"常驻人设有多少字"——它以前是上下文预算的大头
    try:
        from app.persona import describe as _persona_desc
        _pd = _persona_desc()
        log.info(
            f"   Persona    : {_pd['core_source']} / {_pd['core_chars']} 字常驻 / "
            f"场景模板 {len(_pd['scenes'])} 个 {_pd['scenes']}"
        )
    except Exception as e:      # 人设信息打不出来不该影响启动
        log.warning(f"   Persona    : (读取失败 {e})")
    log.info(f"   Max History: {Config.MAX_HISTORY}")
    log.info(f"   Log Level  : {Config.LOG_LEVEL}")
    if Config.DASHBOARD_ENABLED:
        log.info(f"   Dashboard  : http://{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}/?token={Config.DASHBOARD_TOKEN}")
    log.info("-" * 60)
    log_environment()
    log.info("=" * 60)


async def main():
    setup_logging()
    logger = logging.getLogger("Bot")
    _start_time = time.time()

    log_startup_banner(logger)

    import asyncio
    onebot, handler = build_handler()
    # 【P2a】启动调度框架（破冰 / 摘要等任务在里面）
    handler.start_scheduler()

    tasks = []
    # 主任务：OneBot WS 客户端
    tasks.append(asyncio.create_task(onebot.start(), name="onebot"))
    # 后台：管理面板（可选）
    if Config.DASHBOARD_ENABLED:
        try:
            from app.dashboard.server import start_dashboard
            tasks.append(asyncio.create_task(
                start_dashboard(onebot, handler, _start_time), name="dashboard"
            ))
        except ImportError:
            logger.warning("[Init] aiohttp 未安装，跳过管理面板。pip install aiohttp")

    try:
        # 跑着，直到 Ctrl+C
        await asyncio.gather(*tasks, return_exceptions=False)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在退出...")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await handler.stop_scheduler()      # 【P2a】停调度器（它自己的协程不在 tasks 里）
        await onebot.stop()
        # 等已经排队的写盘任务做完再退，避免丢最后几条记忆/画像
        shutdown_blocking(wait=True)
        logger.info("已退出。👋")


if __name__ == "__main__":
    import asyncio  # 延迟导入
    asyncio.run(main())
