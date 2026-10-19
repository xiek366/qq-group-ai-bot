"""
消息 Handler —— 消息分发中心
------------------------------
把解析后的群消息分发给对应处理逻辑。

支持:
  /ping          → pong
  /问 xxx        → AI 问答（带上下文）
  @机器人        → AI 聊天（带上下文）
  /搜 xxx        → 搜索 + LLM 总结

不引入复杂框架，保持朴素。
"""

import asyncio
import datetime as dt
import json
import logging
import random
import re
import os
import time
from collections import deque
from typing import Optional, TYPE_CHECKING

from app.config import Config
from app.paths import LEGACY_ALIASES_PATH, MEMORY_DIR, QUOTES_STATE_PATH, STYLE_DIR
from app.sticker.manager import StickerManager
from app.style.profile import StyleProfile
from app.alias.manager import AliasManager
from app.util import clock
from app.util.blocking import run_blocking
from app.vision import VisionPipeline
# 【P1】记忆分层：滚动摘要 + 画像卡、话题状态、上下文预算组装器
from app import persona
from app.context.assembler import HIGH, LOW, MUST, ContextAssembler, Section
from app.context.summary import ConversationSummary
from app.context.topic import TopicState
from app.schedule import Scheduler
from app.schedule.task import sanitize_outgoing
from app.schedule.tasks import (
    DailyImageTask,
    DailyQuoteTask,
    IcebreakTask,
    NewsDigestTask,
    SummaryJob,
    WaterReminderTask,
    WeatherMorningTask,
    make_rotating_source,
)
from app.context.topic_pool import PRESET_QUOTES, TopicPool
from app.news import NewsService
from app.weather import WeatherService, available_cities as weather_cities, resolve_city
# from app.image.provider import  # 生图暂时关闭 create_provider as create_image_provider, save_image as save_local_image

if TYPE_CHECKING:
    from app.bot.onebot import OneBotClient
    from app.llm.base import LLMProvider
    from app.search.base import SearchProvider
    from app.memory.memory import GroupMemory
    # from app.image.provider import  # 生图暂时关闭 ImageProvider

logger = logging.getLogger(__name__)

# ===== 工具定义（Function Calling 格式）=====
# 这些工具会注册给 LLM，LLM 判断合适时会自动调用
_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "联网搜索信息。当用户的问题需要实时信息（如最新新闻、天气、价格、版本号）或你不确定答案时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "获取当前日期时间。当用户问'现在几点'、'今天几号'、'今天星期几'等时间相关问题时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "执行简单的数学计算（加减乘除、括号、幂运算）。当用户需要计算时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，如 '123 * 456' 或 '(1 + 2) * 3'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roll",
            "description": "掷骰子。当用户想玩骰子、抽签、抽卡概率模拟时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {
                        "type": "integer",
                        "description": "骰子面数，默认 6。常见值: 4, 6, 8, 10, 12, 20, 100",
                    },
                    "count": {
                        "type": "integer",
                        "description": "骰子数量，默认 1",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate",
            "description": "翻译文本。支持中英日韩互译。当用户要求翻译、或发了外文需要解释时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要翻译的文本"},
                    "target_lang": {
                        "type": "string",
                        "description": "目标语言代码: zh(中文), en(英文), ja(日文), ko(韩文)。默认自动判断",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nickname",
            "description": (
                "管理【当前说话人自己】的外号。只有当说话人明确要求'给我设个外号/叫我XX/以后别叫我XX/我有哪些外号'时调用。"
                "只能操作说话人自己的外号，不能给别的人设或删外号。"
                "action: set=新增外号, remove=删除一个外号, list=查看自己的外号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "操作类型: set / remove / list",
                        "enum": ["set", "remove", "list"],
                    },
                    "alias": {
                        "type": "string",
                        "description": "外号内容。action=set 或 remove 时必填；action=list 时不用填。",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "__DISABLED__": True,  # 生图暂时关闭
        "type": "function",
        "function": {
            "name": "draw",
            "description": "AI 画图。把文字描述变成图片（二次元风格）。当用户说'画一张'、'画个'、'给我画'、'生图'等要求画图时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "要画什么的详细描述，最好是英文（如 'chibi Ganyu from Genshin Impact, blue hair, cute'），中文也可以。",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]

# ===== 表情关键词 → face_id 反向映射（Bot 发消息时自动替换）=====
# 选高频 + 不容易误替换的词
_FACE_KEYWORDS: dict[str, str] = {
    "微笑": "14",
    "笑了": "14",
    "开心": "14",
    "得意": "67",
    "厉害": "67",
    "赞": "173",
    "鼓掌": "173",
    "同意": "173",
    "加油": "173",
    "恭喜": "99",
    "晚安": "93",
    "拜拜": "73",
    "再见": "73",
    "委屈": "6",
    "难过": "6",
    "伤心": "6",
    "尴尬": "24",
    "汗": "24",
    "无语": "24",
    "发呆": "5",
    "懵": "5",
    "偷笑": "4",
    "害羞": "97",
    "么么哒": "2",
    "爱你": "66",
    "比心": "66",
    "怒": "161",
    "生气": "161",
    "赞啊": "173",
    "牛": "173",
    "666": "173",
    "吃瓜": "211",
    "思考": "183",
    "疑问": "183",
    "困": "75",
    "睡觉": "75",
    "无聊": "39",
    "摊手": "39",
}


class MessageHandler:
    def __init__(
        self,
        onebot: "OneBotClient",
        llm_provider: Optional["LLMProvider"] = None,
        search_provider: Optional["SearchProvider"] = None,
        memory: Optional["GroupMemory"] = None,
        persona: str = "",
    ):
        self.onebot = onebot
        self.llm = llm_provider
        self.search = search_provider
        self.memory = memory
        self.persona = persona
        self.sticker = StickerManager()  # 表情包库
        self.style = StyleProfile()     # 群风格画像
        self.alias = AliasManager()     # 群友外号表
        self.vision = VisionPipeline()  # 【P0-3】视觉管道（下载/校验/内联/缓存/降级）
        # 【P1】记忆分层
        self.summary = ConversationSummary()   # 滚动摘要要点 + 用户画像卡（data/summary/<群>.json）
        self.topic = TopicState()              # 当前话题（data/state/<群>.json）
        self.assembler = ContextAssembler()    # 上下文预算组装器
        # self.image = create_image_provider()  # 生图暂时关闭

        # ===== 【P2a】调度框架：破冰与摘要都变成了"任务" =====
        # 原来这里是一个 while True 的 _idle_watcher 协程，自己判断时机、自己发送。
        # 现在时机判断交给 Scheduler（触发器 + 冷却 + 退避 + 每日预算 + D15 白名单），
        # 本类只提供"内容生成"与"发送器"，任务实现见 app/schedule/tasks/。
        self.scheduler = Scheduler(
            send=self._send_proactive,
            group_ids=self._scheduler_group_ids,
            last_activity=lambda gid: self._last_active_time.get(gid, 0.0),
            member_count=self._scheduler_member_count,
        )
        self.scheduler.register(IcebreakTask(self._gen_icebreak_topic, self._gen_icebreak_hot))
        self.scheduler.register(SummaryJob(self._run_summary_job))
        # 【P2b】天气播报 + 喝水/久坐提醒（服务层共用：播报与 /天气 走同一份逻辑）
        self.weather = WeatherService()
        self.scheduler.register(WeatherMorningTask(self.weather, polish=self._polish_weather_finish))
        self.scheduler.register(WaterReminderTask())
        # 【P2c】资讯 / 每日一句 / 每日一图 + 话题库 + 破冰内容源轮换
        self.news = NewsService()
        self.topic_pool = TopicPool()
        self.quote_pool = TopicPool(path=QUOTES_STATE_PATH, topics=list(PRESET_QUOTES))
        # 【P2c-图鉴分拣】图鉴型 UP（NEWS_GALLERY_MIDS）不进资讯流，专属每日一图
        from app.schedule.tasks.news_digest import gallery_source_ids
        self._gallery_ids = gallery_source_ids(Config.NEWS_GALLERY_MIDS)
        self.scheduler.register(NewsDigestTask(self.news, commenter=self._comment_news))
        self.scheduler.register(DailyQuoteTask(self.quote_pool, generator=self._gen_quote))
        self.scheduler.register(DailyImageTask(self.news, captioned=self._caption_image))
        # 破冰内容源改成轮换（ROADMAP §4.5）：话题包 → 历史回顾 → 资讯 → 话题库 → 热搜兜底
        self.scheduler.tasks["icebreak"].source = make_rotating_source([
            ("LLM话题包", self._gen_icebreak_topic),
            ("群历史回顾", self._gen_history_review),
            ("资讯", self._gen_icebreak_news),
            ("话题库", self._gen_topic_pool),
            ("热搜兜底", self._gen_icebreak_hot),
        ])
        self._scheduler_task = None

        # ===== 帮助文本 =====
        self._HELP_TEXT = (
            "📖 爱丽丝的命令列表：\n"
            "  /ping              连通性测试\n"
            "  /help              显示本帮助\n"
            "  /clear             清空对话历史\n"
            "  /问 <问题>         AI 问答（带上下文）\n"
            "  /搜 <关键词>       联网搜索 + AI 总结\n"
            "  /看 + 图片         看图（手机端不用 @）\n"
            "  /天气 [城市]       查天气（不填就用本群绑定的城市）\n"
            "  /资讯              看一条最新游戏/动漫资讯\n"
            "  /任务 [列表|试跑|执行] 定时任务管理（仅管理员）\n"
            "  /表情 或 /sticker   随机发一个收藏的表情包\n"
            "  /叫我 <外号>        给你自己设一个外号（最多 3 个）\n"
            "  /别叫我 <外号>      删掉某个外号\n"
            "  /别叫我了           清空你自己的全部外号\n"
            "  /我的外号           看看自己有哪些外号\n"
            "  @爱丽丝 <问题>     直接聊天（会自动判断要不要搜索/计算/翻译）\n"
            "\n"
            "💡 小彩蛋：\n"
            "  · @爱丽丝 骰子 / roll → 自动掷骰子\n"
            "  · @爱丽丝 翻译 xxx   → 自动翻译\n"
            "  · @爱丽丝 几点了      → 自动报时\n"
            "  · 群友发表情包自动收藏，回复偶尔会带表情～"
        )

        # ===== 主动闲聊状态 =====
        # 每个群上次主动回复的时间: group_id → timestamp
        self._last_active_time: dict[int, float] = {}     # 群最后一条消息（破冰用，所有消息都更新）
        self._last_bot_reply_time: dict[int, float] = {}   # Bot 最后一次回复（冷却用，只有 Bot 回复才更新）
        self._last_icebreak: dict[int, float] = {}      # 每次破冰的时间戳
        # 每个群最近的群聊消息（用来喂给 LLM 当上下文）: group_id → deque[{nick, text}]
        self._group_recent: dict[int, deque[dict]] = {}

        # ===== /看 会话绑定 =====
        # 用户发 "/看" 不带图片 → 记录 "等这个用户下一张图"，防止群里其他人插话或误识图
        # group_id → {"user_id": int, "expires_at": float}
        self._pending_look: dict[int, dict] = {}

        # ===== 工具调用时的"谁在说话"上下文 =====
        # tool_call_back 签名由 provider 固定，传不进额外参数，
        # 所以在调 llm.chat 前把 {group_id, user_id} 存这里，工具执行时读。
        self._speaker_ctx: dict = {}

        # ===== 面板消息日志 =====
        self._message_log: deque[dict] = deque(maxlen=200)   # 最近 200 条消息摘要，供面板读取
        self._sse_broadcast = None   # Dashboard 启动后会注入一个回调: (msg_dict) -> None

        # ===== 从持久化文件回填状态（重启后破冰不漏群）=====
        self._restore_group_state_from_files()

    # ---------- 从持久化文件回填状态 ----------
    def _restore_group_state_from_files(self):
        """
        启动时扫描 data/memory/ 和 data/group_style/ 下的 *.json，
        用文件 mtime 回填 _last_active_time。
        这样重启前沉默的群也能被 _idle_watcher 扫到，不会漏破冰。
        """
        import os as _os
        import time as _time

        # 绝对路径：换工作目录 / systemd 少写 WorkingDirectory 也不会找错地方
        dirs_priority = [
            (MEMORY_DIR, 1.0),    # 记忆文件 mtime 最准确（最后一条消息写入时更新）
            (STYLE_DIR, 0.9),     # 画像 mtime 兜底（权重略低）
        ]
        for d, weight in dirs_priority:
            if not _os.path.isdir(d):
                continue
            for fname in _os.listdir(d):
                if not fname.endswith(".json"):
                    continue
                gid_str = fname.replace(".json", "")
                if not gid_str.isdigit():
                    continue
                gid = int(gid_str)
                fpath = _os.path.join(d, fname)
                try:
                    mtime = _os.path.getmtime(fpath)
                except OSError:
                    continue
                # 取最晚的那个（记忆可能比画像更新）
                existing = self._last_active_time.get(gid, 0)
                if mtime > existing:
                    # 加一点权重偏移（memory 目录优先）
                    self._last_active_time[gid] = mtime * weight + _time.time() * (1 - weight) if weight < 1.0 else mtime
        if self._last_active_time:
            logger.info(f"[StateRestore] 从持久化文件回填了 {len(self._last_active_time)} 个群的活跃时间")

    # ---------- 面板只读状态暴露 ----------
    @property
    def status(self) -> dict:
        """给管理面板读的状态字典（只读快照）"""
        import time as _t
        groups = []
        for gid in sorted(self._last_active_time.keys()):
            groups.append({
                "id": gid,
                "last_active_ts": self._last_active_time.get(gid, 0),
                "last_bot_reply_ts": self._last_bot_reply_time.get(gid, 0),
                "last_icebreak_ts": self._last_icebreak.get(gid, 0),
                "memory_count": self.memory.size(gid) if self.memory else 0,
                "memory_max": self.memory.max_history if self.memory else 0,
                "style_summary": (self.style.get_summary(gid)[:100] if self.style else ""),
                "style_pending": (self.style.pending_count(gid) if self.style else 0),
                "recent_msg_count": len(self._group_recent.get(gid, [])),
            })
        return {
            "groups": groups,
            "stickers_total": self.sticker.count(),
            "icebreak_enabled": Config.ICEBREAK_ENABLED,
            "icebreak_watch_interval": Config.ICEBREAK_WATCH_INTERVAL,
            "icebreak_min_group_size": Config.ICEBREAK_MIN_GROUP_SIZE,
            "reply_chance": Config.CHAT_REPLY_CHANCE,
            "reply_cooldown": Config.CHAT_COOLDOWN,
            "message_log_count": len(self._message_log),
        }

    @property
    def message_log(self) -> list[dict]:
        """最近 200 条消息摘要（从旧到新）"""
        return list(self._message_log)

    # ---------- 主入口 ----------
    async def handle(self, parsed: dict):
        """处理一条解析后的群消息"""
        group_id = parsed["group_id"]
        command = parsed["command"]
        is_at_bot = parsed["is_at_bot"]

        # 0.5) 原始 segment 明细：只在 DEBUG 级别输出（以前是 INFO，每条消息都刷一行，日志很吵）
        raw_msg = parsed.get("_raw_msg", [])
        if raw_msg and logger.isEnabledFor(logging.DEBUG):
            detail = []
            for seg in raw_msg:
                if not isinstance(seg, dict):
                    continue
                t = seg.get("type", "?")
                d = seg.get("data", {})
                file_short = os.path.basename(d.get("file", ""))
                url_short = (d.get("url", "") or "")[-50:]
                summary = d.get("summary", "")[:30]
                detail.append({
                    "type": t,
                    "sub_type": d.get("sub_type", "?"),
                    "file": file_short[:50],
                    "url_end": url_short,
                    "summary": summary,
                })
            logger.debug(f"[DEBUG] segments detail: {detail}")

        # 0) 先收藏群友发的自定义表情
        sticker_infos = parsed.get("sticker_infos", [])
        for st in sticker_infos:
            asyncio.create_task(self.sticker.collect(
                st, group_id=group_id,
                sender_id=parsed["user_id"],
                sender_nick=parsed["nickname"],
            ))
        if sticker_infos:
            logger.info(f"[Sticker] group={group_id} 收到 {len(sticker_infos)} 个自定义表情")

        logger.info(
            f"[group={group_id}] [user={parsed['user_id']}] "
            f"[nick={parsed['nickname']}] "
            f"cmd={command} at={is_at_bot} msg={parsed['raw_text']}"
        )

        # ★ 面板消息日志（在任何 return 之前 push，保证全量记录）
        _log_entry = {
            "ts": time.time(),
            "group_id": group_id,
            "user_id": parsed["user_id"],
            "nickname": parsed["nickname"],
            "command": command or "",
            "is_at_bot": is_at_bot,
            "raw_text": parsed["raw_text"][:120],
        }
        self._message_log.append(_log_entry)
        if self._sse_broadcast:
            try:
                import asyncio as _aio
                _aio.create_task(self._sse_broadcast(_log_entry))
            except Exception:
                pass

        # ★ 风格画像：所有消息都收集（必须在 return 之前！）
        await self._remember_group_msg(parsed)

        # ★ 破冰：所有消息都更新"群最后活跃时间"（也必须在 return 之前！）
        self._last_active_time[group_id] = time.time()

        # 1) /ping
        if command in ("/ping", "/p"):
            await self._send_group(group_id, "pong 🏓")
            return

        # 2) /help —— 帮助
        if command in ("/help", "/h", "/帮助", "帮助"):
            await self._send_group(group_id, self._HELP_TEXT)
            return

        # 3) /clear —— 清空上下文
        if command in ("/clear", "/c", "/清空"):
            if self.memory:
                await run_blocking(self.memory.clear, group_id)   # 删文件是阻塞操作
                await self._send_group(group_id, "记忆清空啦 🧹")
            else:
                await self._send_group(group_id, "没开记忆功能呢")
            return

        # 4) /问 xxx
        if command in ("/ask", "/问", "/question"):
            await self._handle_chat(group_id, parsed, trigger="ask")
            return  # 修复：原来漏了 return，会继续走到 _maybe_casual_reply 造成双回复

        # /看 —— 看图专用命令（手机端不用 @ 机器人）
        if command in ("/看", "/look", "/看图", "/看看", "/view"):
            image_urls = parsed.get("image_urls", [])
            if not image_urls:
                # 用户只发了 /看 不带图片 → 记录会话绑定，等他下一张图
                gid = group_id
                uid = parsed.get("user_id", 0)
                self._pending_look[gid] = {
                    "user_id": uid,
                    "expires_at": time.time() + 120,  # 2 分钟内有效
                }
                logger.info(f"[Look-bind] group={gid} 等待 user={uid} 下一张图片 (2min)")
                await self._send_group(group_id, "发张图片给我看看呀 📸（2 分钟内发给我哦～）")
            else:
                # /看 + 图片 → 立即识图，不走冷却
                self._pending_look.pop(group_id, None)  # 清掉待绑定状态
                await self._handle_chat(group_id, parsed, trigger="look")
            return

        # 5) /搜 xxx
        if command in ("/search", "/搜", "/查"):
            await self._handle_search(group_id, parsed)
            return

        # 5.5) 【P2a】/任务 —— 定时任务的手动入口（仅管理员）
        # 测试群不开定时任务，所以"试跑/执行/查看状态"是调内容质量与上线验证的唯一通道。
        if command in ("/任务", "/task", "/tasks"):
            await self._cmd_task(group_id, parsed)
            return

        # 5.6) 【P2b】/天气 [城市] —— 被动响应，任何群都能用（不受 D15 限制）
        if command in ("/天气", "/weather", "/tianqi"):
            await self._handle_weather(group_id, parsed)
            return

        # 5.7) 【P2c】/资讯 —— 手动看一条最新游戏/动漫资讯（被动响应）
        if command in ("/资讯", "/news", "/zixun"):
            await self._handle_news(group_id, parsed)
            return

        # 生图暂时关闭（/画 命令）
        # if command in ("/draw", "/画", "/生图", "/image"):
        #     await self._handle_draw(group_id, parsed)
        #     return

                # 8) /sticker /表情 /e —— 随机发一个收藏的表情包
        if command in ("/sticker", "/表情", "/e"):
            await self._send_random_sticker(group_id)
            return

        # 8.5) 外号相关命令
        if command in ("/叫我", "/callme"):
            await self._cmd_alias_set_self(group_id, parsed)
            return

        if command in ("/别叫我", "/dontcallme"):
            await self._cmd_alias_remove_self(group_id, parsed)
            return

        if command in ("/别叫我了", "/clearalias"):
            await self._cmd_alias_clear_self(group_id, parsed)
            return

        if command in ("/我的外号", "/myalias"):
            await self._cmd_alias_list_self(group_id, parsed)
            return

        # 管理员专用（仅开发者）
        if command in ("/设外号", "/setalias"):
            await self._cmd_alias_admin_set(group_id, parsed)
            return

        if command in ("/删外号", "/delalias"):
            await self._cmd_alias_admin_remove(group_id, parsed)
            return

        # 9) @机器人 → 聊天
        if is_at_bot:
            await self._handle_chat(group_id, parsed, trigger="at")
            return

        # 9.5) 没 @ 没命令，但发了图片 → 分两种情况
        image_urls = parsed.get("image_urls", [])
        if image_urls:
            pending = self._pending_look.get(group_id)
            if pending:
                # 有会话绑定 → 只认那个用户，且不走冷却
                uid = parsed.get("user_id", 0)
                now = time.time()
                if now > pending["expires_at"]:
                    # 超时了，清掉绑定
                    self._pending_look.pop(group_id, None)
                    logger.info(f"[Look-bind] group={group_id} 会话绑定超时，降级为裸图冷却")
                elif uid != pending["user_id"]:
                    # 不是那个用户发的图 → 忽略，让等的那个人发
                    logger.info(f"[Look-bind] group={group_id} 忽略其他人的图 (期望 user={pending['user_id']}, 收到 user={uid})")
                    return
                else:
                    # 命中！清绑定 + 识图 + 不走冷却
                    self._pending_look.pop(group_id, None)
                    logger.info(f"[Look-bind] group={group_id} user={uid} 命中会话绑定，开始识图（不走冷却）")
                    await self._handle_chat(group_id, parsed, trigger="look")
                    return
            # 没命中绑定 → 普通裸发图片（走冷却）
            await self._handle_look_only(group_id, parsed)
            return

        # 6) 普通消息 → 判断要不要主动插嘴
        await self._maybe_casual_reply(group_id, parsed)

    async def _handle_look_only(self, group_id: int, parsed: dict):
        """只发了图片（没 @ 没命令）→ 看图 + LLM 简单回复（带冷却防刷屏）"""
        image_urls = parsed.get("image_urls", [])
        if not image_urls or self.llm is None:
            return
        # 【D15】裸图自动回复也算主动行为：不在名单里的群不打扰（要识图请用 /看）
        if not self._proactive_allowed(group_id):
            logger.info(f"[Vision-only] group={group_id} 不在主动打扰名单，跳过")
            return
        # 冷却检查 —— 和主动闲聊共用冷却，避免 Bot 连续刷屏
        cooldown = Config.CHAT_COOLDOWN
        last = self._last_bot_reply_time.get(group_id, 0)
        if time.time() - last < cooldown:
            logger.info(f"[Vision-only] group={group_id} 冷却中，跳过")
            return
        try:
            logger.info(f"[Vision] group={group_id} 只发了 {len(image_urls)} 张图，开始看图")
            vr = await self.vision.describe(image_urls, llm=self.llm)
            desc = vr.description
            if not desc:
                logger.warning(
                    f"[Vision-only] group={group_id} 看图失败 reason={vr.reason} "
                    f"跳过={vr.skipped} 跳过本条"
                )
                return
            logger.info(
                f"[Vision-only] group={group_id} 看图成功"
                f"（{'缓存' if vr.cached else '新请求'}，内联 {vr.inlined}）"
            )
            await self._record_image_description(group_id, parsed, desc)
            prompt = persona.load_scene("vision_reply", description=desc) or (
                f"以下是一张群友发的图片内容描述：\n{desc}\n\n"
                f"请你以爱丽丝的身份，用 1~2 句话自然地回应这张图，像朋友聊天一样。"
            )
            reply = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                # 看图场景不加 chat 场景说明（避免"@你/用 /问"这类无关要求混进来）
                system_prompt=await self._build_system_prompt(
                    group_id=group_id, speaker_id=parsed.get("user_id"), scene="",
                ),
            )
            reply = (reply or "").strip()
            if not reply:
                logger.warning(f"[Vision-only] group={group_id} LLM 返回空回复")
                return
            self._last_bot_reply_time[group_id] = time.time()  # 更新冷却
            reply = await self._auto_mention(group_id, reply)
            await self._record_bot_message(group_id, reply, kind="vision")
            await self._send_group(group_id, reply)
            logger.info(f"[Vision-only] group={group_id} reply_len={len(reply)}")
        except Exception as e:
            import traceback
            logger.error(f"[Vision-only] 看图失败: {e}")
            logger.error(traceback.format_exc())

    # ---------- 主动闲聊（插嘴）----------
    async def _remember_group_msg(self, parsed: dict):
        """把一条普通群消息存进 per-group deque + 喂给风格画像收集器"""
        gid = parsed["group_id"]
        nick = parsed["nickname"]
        text = parsed["content"] or parsed["raw_text"]
        is_command = bool(parsed.get("command"))

        # 【P0-1】所有群消息都进统一对话流（含命令）——
        # 这是"接得上话"的地基：以前只有 @bot 的那几轮进记忆，
        # 群里平时的聊天、以及 bot 自己的发言都不在任何记录里。
        if self.memory:
            await run_blocking(
                self.memory.add_user,
                gid, parsed["user_id"], nick, text,
                kind="command" if is_command else "chat",
            )

        if gid not in self._group_recent:
            self._group_recent[gid] = deque(maxlen=30)
        self._group_recent[gid].append({"nick": nick, "text": parsed["raw_text"]})

        # 喂给风格画像收集器：它要把 pending 追加写回 data/group_style/*.json，
        # 是实打实的阻塞 I/O，丢到线程里做，别钉住事件循环
        if self.style and self.llm:
            await run_blocking(self.style.add_message, gid, nick, parsed["raw_text"])
            if self.style.should_summarize(gid):
                # 开后台任务，不阻塞当前消息处理
                asyncio.create_task(self._summarize_style(gid))

        # 【P2a】长期记忆摘要已迁入调度框架（summary_job 任务，interval 触发器）——
        # 这里不再有"内存计数 + create_task"那套触发逻辑，避免两套触发同时跑。
        # 摘要任务自己会读对话流、判断有没有新内容（>= SUMMARY_MIN_MESSAGES 条才动）。

        # 【P1-3】有人继续聊 → 刷新当前话题的活跃时间（话题内部有落盘节流，不会每条消息都写盘）
        if self.topic and Config.TOPIC_ENABLED:
            await run_blocking(self.topic.touch, gid)

    async def _record_bot_message(self, group_id: int, text: str, kind: str = "reply"):
        """【P0-1】把 bot 自己的发言也写进统一对话流。

        以前只有"用户消息 + @bot 的回复"进记忆，主动插嘴/破冰/看图回复都不在，
        于是 bot 抛出话题后有人接话，它根本不知道自己在说什么——现在补上。

        kind: reply（@bot、/问）/ proactive（插嘴、破冰）/ vision（看图）/ search（搜索总结）
        """
        if not self.memory or not text:
            return
        try:
            await run_blocking(self.memory.add_assistant, group_id, text, kind=kind)
        except Exception as e:      # 记录失败不能影响正常发送
            logger.warning(f"[Stream] 记录 bot 发言失败（不影响发送）: {e}")

    def _proactive_allowed(self, group_id: int) -> bool:
        """【D15】这个群允许收"主动消息"吗？

        主动消息 = 破冰 / 主动插嘴 / 没 @ 也发图的自动回复。
        **被动响应（@bot、/问、/看、命令）不走这个判断**，任何群都能用。
        `Config.PROACTIVE_GROUPS` 留空 = 不限制（兼容旧行为）。
        """
        allowed = Config.PROACTIVE_GROUPS
        return (not allowed) or (group_id in allowed)

    async def _record_image_description(self, group_id: int, parsed: dict, description: str):
        """【P0-3】把图片识别结果写进对话流。

        以前描述用完就丢，所以"接着刚才那张图聊"是不可能的——窗口里只有"[图片]"。
        这里以**发言人**的身份追加一条带 `[图片识别结果]` 前缀的条目，
        模型既能看到这张图讲了什么，也能分清它是识别结果而不是群友原话。
        """
        if not self.memory or not description:
            return
        try:
            await run_blocking(
                self.memory.stream.append,
                group_id, "user", f"[图片识别结果] {description}",
                user_id=parsed.get("user_id"),
                nick=parsed.get("nickname", ""),
                kind="vision",
            )
        except Exception as e:
            logger.warning(f"[Stream] 记录图片描述失败（不影响回复）: {e}")

    async def _summarize_style(self, group_id: int):
        """后台任务：攒够消息 → 调 LLM → 更新风格画像"""
        try:
            profile = await self.style.summarize_with_llm(self.llm, group_id)
            if profile:
                await run_blocking(self.style.update, group_id, profile)
        except Exception as e:
            logger.error(f"[Style] 总结异常 group={group_id}: {e}")

    # ---------- 【P2c】资讯 / 话题 / 历史回顾 的内容生成 ----------

    async def _gen_history_review(self, ctx) -> str:
        """群历史回顾：把最近的真实对话交给 LLM，让它"想起"某个话题并追问后续。

        **只能引用真实出现过的内容**（scene 里写死了这条），否则就是编造群友说过的话。
        """
        if self.memory is None or self.llm is None:
            return ""
        entries = await run_blocking(self.memory.stream.window, ctx.group_id, 30)
        # 太短的窗口没有"历史"可言，别硬凑
        if len(entries) < 8:
            return ""
        lines = []
        for e in entries[-30:]:
            who = e.get("nick") or ("爱丽丝" if e.get("role") == "assistant" else "群友")
            text = (e.get("text") or "").strip().replace("\n", " ")
            if text:
                lines.append(f"{who}: {text[:80]}")
        if not lines:
            return ""
        prompt = persona.load_scene("history_review", chat="\n".join(lines)) or (
            "群里冷场了。下面是最近的群聊，请引用其中真实提到过的事，用 1~2 句把话头递出去"
            "（不超过 40 字，不许编造没出现过的内容）：\n" + "\n".join(lines)
        )
        reply = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=await self._build_system_prompt(group_id=ctx.group_id, scene=""),
            temperature=0.9,
        )
        return (reply or "").strip()

    async def _gen_icebreak_news(self, ctx) -> str:
        """资讯作为破冰素材：标题（事实）+ 一句钩子（问题）。

        ROADMAP §4.5 对资讯的定位是"事实 + 看法 + 问题"——作为**破冰**素材时最需要的是
        "能让人接话的问题"，所以这里用一句固定钩子（不额外调 LLM，省成本）；
        "看法"那一句由独立的资讯任务（`news_digest`）用 LLM 点评承担。
        """
        if not Config.NEWS_ENABLED:
            return ""
        try:
            await self.news.refresh()
        except Exception as e:
            logger.debug(f"[Icebreak] 资讯刷新失败（跳过该内容源）: {e}")
        item = self.news.pick_for_digest(exclude_source_ids=self._gallery_ids)
        if item is None:
            return ""
        if not ctx.dry_run:
            self.news.mark_pushed(item)
        hook = "有人关注这个吗？"
        if item.link:
            return f"刚刷到一条：{item.title}\n{hook}\n{item.link}"
        return f"刚刷到一条：{item.title}\n{hook}"

    async def _gen_topic_pool(self, ctx) -> str:
        """话题库轮播：选一条最少用过的（并避开本群最近用过的）"""
        topic = self.topic_pool.pick(ctx.group_id)
        if not topic:
            return ""
        self.topic_pool.mark_used(ctx.group_id, topic["id"])
        logger.info(f"[Icebreak] 话题库命中 {topic['id']}（{topic.get('type')}）")
        return topic["text"]

    async def _comment_news(self, item) -> str:
        """给一条资讯写一句点评（供资讯任务与每日一图共用）"""
        if self.llm is None:
            return ""
        prompt = persona.load_scene(
            "news_comment", title=item.title, source=item.source, summary=item.summary or "（无）"
        )
        if not prompt:
            return ""
        reply = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=f"{self.persona}\n\n只输出一句话，不要任何解释。",
            temperature=0.8,
        )
        text = (reply or "").strip().splitlines()[0] if (reply or "").strip() else ""
        return text[:60]

    async def _gen_quote(self) -> str:
        """LLM 原创一句（每日一句偶尔用；失败由任务回落到台词池）"""
        if self.llm is None:
            return ""
        prompt = persona.load_scene("quote") or "写一句游戏/动漫向的鼓励，不超过 40 字，只输出这一句。"
        reply = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=f"{self.persona}\n\n只输出一句话，不要任何解释。",
            temperature=1.0,
        )
        text = (reply or "").strip().splitlines()[0] if (reply or "").strip() else ""
        return text[:60]

    async def _caption_image(self, item) -> str:
        """每日一图的配文"""
        if self.llm is None:
            return ""
        prompt = persona.load_scene("image_caption", title=item.title, source=item.source)
        if not prompt:
            return ""
        reply = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=f"{self.persona}\n\n只输出一句话，不要任何解释。",
            temperature=0.9,
        )
        text = (reply or "").strip().splitlines()[0] if (reply or "").strip() else ""
        return text[:40]

    async def _handle_news(self, group_id: int, parsed: dict):
        """`/资讯`：手动看一条最新资讯（被动响应，任何群可用；与定时任务同一份逻辑）"""
        if not Config.NEWS_ENABLED:
            await self._send_group(group_id, "资讯功能现在关着呢～")
            return
        try:
            await self.news.refresh()
        except Exception as e:
            logger.warning(f"[News] /资讯 刷新失败: {e}")
        item = self.news.pick_for_digest(exclude_source_ids=self._gallery_ids)
        if item is None:
            await self._send_group(group_id, "这会儿没刷到新的游戏/动漫消息，晚点再来看看吧 🥺")
            return
        comment = await self._comment_news(item)
        from app.schedule.tasks.news_digest import format_news
        self.news.mark_pushed(item)
        await self._send_group(group_id, format_news(item, comment))

    # ---------- 【P2b】天气 ----------

    async def _polish_weather_finish(self, city: str, data) -> str:
        """给天气播报润色一句收尾（Q1：模板 + LLM 润色一句）。

        - 只让 LLM 写**收尾那一句**，天气事实与注意事项一律由代码拼（准确、可测）；
        - 任何失败都返回空串 → 任务会回落到模板句（播报不会因此失败）。
        """
        if self.llm is None:
            return ""
        prompt = (
            f"今天是 {city} 的天气：{data.tmin:.0f}~{data.tmax:.0f}℃、{data.desc or '未知'}。\n"
            "请以爱丽丝（活泼的游戏宅少女）的口吻，写一句不超过 20 字的收尾祝福/叮嘱，"
            "不要重复上面的天气数字，不要用 emoji 之外的符号，不要 markdown，不要 @ 任何人。"
        ) if (data.tmin is not None and data.tmax is not None) else (
            f"今天是 {city} 的天气：{data.desc or '未知'}。\n"
            "请以爱丽丝的口吻写一句不超过 20 字的收尾祝福，不要 markdown，不要 @ 任何人。"
        )
        try:
            reply = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=f"{self.persona}\n\n只输出那一句话，不要任何解释。",
                temperature=0.9,
            )
        except Exception as e:
            logger.warning(f"[Weather] 收尾句生成失败（回落模板）: {e}")
            return ""
        text = (reply or "").strip().splitlines()[0] if (reply or "").strip() else ""
        return text[:40]

    async def _handle_weather(self, group_id: int, parsed: dict):
        """`/天气 [城市]`：默认用本群绑定城市；只认预置城市（避免拼写/编码问题）"""
        if not Config.WEATHER_ENABLED:
            await self._send_group(group_id, "天气功能现在关着呢～")
            return

        args = (parsed.get("content") or "").replace("/天气", "").replace("/weather", "").strip()
        group_city = self.scheduler.group_config.weather_city(group_id)
        city = resolve_city(args, default=resolve_city(group_city or Config.WEATHER_CITY, default=""))
        if not city:
            await self._send_group(
                group_id,
                f"想看哪里的天气呀？可以这样问我：/天气 北京\n（目前支持：{weather_cities()}）",
            )
            return
        if args and city != resolve_city(args, default=""):
            # 用户写了个没收录的城市
            await self._send_group(
                group_id, f"「{args}」这个城市我还没收录呢～ 现在支持：{weather_cities()}"
            )
            return

        data = await self.weather.fetch_daily(city)
        if data is None:
            # 降级：明确说查不到，绝不猜（ROADMAP §5.5）
            await self._send_group(group_id, f"天气服务暂时查不到 {city} 的数据，等会儿再问我一次吧 🥺")
            return
        await self._send_group(group_id, self.weather.render_command(city, data))

    # ---------- 【P2a】调度框架的接线（把 handler 的能力以回调形式注入，框架不反向依赖） ----------

    def start_scheduler(self) -> None:
        """启动调度器（在事件循环里调用；main.py 启动流程会调一次）"""
        self._scheduler_task = self.scheduler.start()

    async def stop_scheduler(self) -> None:
        await self.scheduler.stop()

    async def _send_proactive(self, group_id: int, text: str) -> None:
        """调度器专用的发送器：先记进对话流（这样 bot 记得自己抛过什么），再发出去"""
        await self._record_bot_message(group_id, text, kind="proactive")
        await self._send_group(group_id, text)

    def _scheduler_group_ids(self) -> list:
        """调度器关心的群：有活跃时间记录的群（= 对话流里出现过的群）"""
        return list(self._last_active_time.keys())

    async def _scheduler_member_count(self, group_id: int) -> Optional[int]:
        try:
            members = await self.onebot.get_group_members(group_id)
            return len(members)
        except Exception:
            return None

    async def _run_summary_job(self, group_id: int) -> bool:
        """summary_job 的执行体：跑一次长期记忆摘要，返回"是否真的做了事"。

        摘要逻辑仍然是 P1 的 `ConversationSummary.run()`，这里只负责
        "读窗口 → 判断有没有新内容 → 调摘要 → 把抽出的话题写进 TopicState"。
        """
        if not self.summary or not self.llm:
            return False
        entries = await run_blocking(
            self.memory.stream.window, group_id, max(Config.SUMMARY_BATCH_MAX * 3, 120)
        )
        fresh = self.summary.unsummarized(group_id, entries)
        if len(fresh) < Config.SUMMARY_MIN_MESSAGES:
            return False
        state = await self.summary.run(self.llm, group_id, entries)
        if not state:
            return False
        parsed_topic = self.summary.pop_topic_hint(group_id)
        if parsed_topic and self.topic:
            await run_blocking(self.topic.set_topic, group_id, parsed_topic)
        return True

    async def _summarize_memory(self, group_id: int):
        """（兼容旧调用路径）后台跑一次长期记忆摘要"""
        try:
            await self._run_summary_job(group_id)
        except Exception as e:
            logger.error(f"[Summary] 群 {group_id} 后台摘要异常: {e}")
        finally:
            self.summary.release(group_id)

    # ---------- 破冰内容生成（【P2a】已迁入调度框架，这里只负责"产出文本"）----------
    #
    # 迁移前这些方法自己判断时机并直接发送；现在"什么时候做/能不能做"由 Scheduler 决定
    # （触发器 + 冷却 + 退避 + 每日预算 + D15 白名单），这里只返回文本，发送交给框架。
    # 好处：干跑（/任务 试跑）能复用同一份逻辑，不需要复制任何生成代码。

    async def _gen_icebreak_topic(self, ctx) -> str:
        """LLM 话题包（破冰主力）：贴合群风格，一句话带钩子"""
        group_id = ctx.group_id
        idle_minutes = ctx.idle_minutes
        style_summary = self.style.get_summary(group_id) or "暂无明显风格"
        prompt = persona.load_scene("icebreak", idle_minutes=f"{idle_minutes:.0f}") or (
            f"这个群已经安静了 {idle_minutes:.0f} 分钟，请想一个有趣的话题来破冰："
            f"一句话、不超过 35 字、贴合群风格（{style_summary}）。"
        )
        prompt = f"{prompt}\n\n（这个群的说话风格画像：{style_summary}）"
        reply = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=await self._build_system_prompt(group_id=group_id, scene=""),
            temperature=0.9,      # 破冰要创造性
        )
        return (reply or "").strip()

    async def _gen_icebreak_hot(self, ctx) -> str:
        """热搜兜底：LLM 给不出内容时，挑一条轻松的热搜"""
        if self.search is None:
            return ""
        try:
            results = await self.search.search("今日热搜 好玩")
            if not results:
                return ""
            r = random.choice(results[:5])
            title = (r.get("title") or "")[:50]
            snippet = (r.get("snippet") or "")[:80]
            if not title:
                return ""
            return f"[CQ:face,id=201] 刚刷到个好玩的：{title}\n{snippet}"
        except Exception as e:
            logger.debug(f"[Icebreak] 热搜兜底跳过: {e}")
            return ""

    async def _maybe_casual_reply(self, group_id: int, parsed: dict):
        """判断要不要主动插嘴"""
        # 【D15】不在主动打扰名单里的群，只做被动响应
        if not self._proactive_allowed(group_id):
            return
        chance = Config.CHAT_REPLY_CHANCE
        if chance <= 0:
            return  # 关闭了

        text = parsed["raw_text"].strip()
        if not text or len(text) <= 1:
            return  # 太短的消息跳过（可能是表情/标点）

        # ① 命中忽略关键词 → 必定跳过
        if any(kw in text for kw in Config.CHAT_IGNORE_KEYWORDS):
            logger.debug(f"[Casual] 跳过（命中忽略词）: {text[:30]}")
            return

        # ② 冷却检查
        cooldown = Config.CHAT_COOLDOWN
        last = self._last_bot_reply_time.get(group_id, 0)
        if time.time() - last < cooldown:
            return

        # ③ 命中关键词 → 100% 回复
        hit_keyword = next((kw for kw in Config.CHAT_KEYWORDS if kw in text), None)
        force_reply = hit_keyword is not None

        if not force_reply:
            # ④ 普通消息：走概率
            if random.randint(1, 100) > chance:
                return

        if self.llm is None:
            return  # 没配 LLM 就别插嘴了

        # ⑤ 调 LLM 生成回复
        try:
            reply = await self._casual_chat(group_id, parsed, force_reply, hit_keyword)
        except Exception as e:
            logger.error(f"[Casual] LLM 失败: {e}")
            return

        if not reply:
            return

        # ⑥ 发送
        self._last_bot_reply_time[group_id] = time.time()
        tag = "关键词触发" if force_reply else f"概率{chance}%"
        logger.info(f"[Casual][{tag}] group={group_id} → {reply[:50]}")
        reply = await self._auto_mention(group_id, reply)
        await self._record_bot_message(group_id, reply, kind="proactive")
        await self._send_group(group_id, reply)

    async def _casual_chat(
        self, group_id: int, parsed: dict, force_reply: bool, hit_keyword: Optional[str]
    ) -> str:
        """调 LLM 生成一句自然的群聊插嘴"""
        # 构造简短的 system prompt
        sys = (
            f"{self.persona}\n\n"
            "--- 以下是额外指令 ---\n"
            "你现在在QQ群里自然地接一下话，不要用 @ 任何人。\n"
            "回复必须非常简短（1-2 句话，20-60 字），像普通群友一样。\n"
            "不要说'作为AI'、'我是'、'大家好'之类的开场白。\n"
            "不要用 Markdown，不要用表情符号以外的特殊格式。\n"
        )

        # 最近群聊上下文
        recent = list(self._group_recent.get(group_id, []))[-Config.CHAT_MAX_CONTEXT:]
        context_lines = []
        for m in recent:
            context_lines.append(f"{m['nick']}: {m['text']}")
        context_text = "\n".join(context_lines)

        keyword_hint = f"（刚才有人提到了'{hit_keyword}'，可以顺着聊）" if hit_keyword else ""

        user_prompt = (
            f"最近群里在聊这些：\n{context_text}\n\n"
            f"请自然地接一句话，符合你的人设{keyword_hint}。"
        )

        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.llm.chat(
            messages=messages,
            temperature=0.9,  # 更有创造性
            system_prompt="",  # 已经塞在 messages[0] 里了
        )
        return reply.strip()

    # ---------- 聊天 ----------
    async def _handle_chat(self, group_id: int, parsed: dict, trigger: str):
        content = parsed["content"] if trigger == "at" else parsed["args"]
        content = content.strip()

        # trigger=look 可能只有图片没有文字（会话绑定 / 裸图 /看+图），content 空是正常的
        image_urls = parsed.get("image_urls", [])
        has_image = len(image_urls) > 0

        if not content and not has_image:
            await self._send_group(group_id, "嗯？你说什么？再说一次 🤔")
            return
        if not content and has_image and trigger != "look":
            # @bot + 只有图片（没有文字）也走视觉，不算错误
            pass  # content 空但有图，后面视觉会处理，不挡

        if self.llm is None:
            await self.onebot.send_group_message(
                group_id,
                "[占位回复] LLM 模块未配置，请在 .env 设置 LLM_API_KEY",
            )
            return

        # 1. 用户消息的写入**已经不在**这里做了：
        #    【P0-1】handle() → _remember_group_msg() 已经把**所有**群消息写进统一对话流，
        #    这里再写一次就会重复。本函数只负责追加 bot 自己的回复。

        # 1.5 视觉：有图片就先看图（@机器人 / 会话绑定 / /看+图 都触发）
        #     走 VisionPipeline：下载 → 校验 → 内联 → 查缓存 → 失败降级（P0-3）
        image_description = ""
        if has_image and self.llm is not None:
            try:
                logger.info(f"[Vision] group={group_id} 收到 {len(image_urls)} 张图，开始看图")
                vr = await self.vision.describe(image_urls, llm=self.llm)
                image_description = vr.description
                if image_description:
                    logger.info(
                        f"[Vision] 描述（{'缓存命中' if vr.cached else '新请求'}，"
                        f"内联 {vr.inlined}/{vr.images_used}）: {image_description[:80]}..."
                    )
                    # 图片描述将通过 system_prompt 注入；同时写进对话流，供后续引用（"刚才那张图"）
                    await self._record_image_description(group_id, parsed, image_description)
                else:
                    logger.warning(
                        f"[Vision] group={group_id} 识别失败 reason={vr.reason} 跳过={vr.skipped}"
                        f" → 本次按「没看清」处理"
                    )
            except Exception as e:
                logger.error(f"[Vision] 看图失败: {e}")

        # 有图但没拿到描述 → 明确告诉模型"没看清"，别让它顺着旧上下文瞎编
        vision_failed = has_image and not image_description
        if vision_failed:
            logger.info(f"[Vision] group={group_id} vision_failed=True（图片未识别成功）")

        # 2. 组装 messages
        # trigger=look + 有图 → 显式把图片描述作为 user 消息拼进去，让 LLM 知道"现在该看图了"
        # 不能让 history 里的旧上下文干扰它（它会顺着历史聊游戏、勇者，忘了看图）
        if trigger == "look" and has_image and image_description:
            messages = [{"role": "user", "content": f"群友发了一张图片，以下是准确描述：\n{image_description}"}]
            logger.info(f"[Chat-look] 显式注入图片描述为 user 消息，历史上下文已跳过")
        elif trigger == "look" and vision_failed:
            messages = [{"role": "user", "content": "群友发了一张图片，但这次没能识别出内容。"}]
            logger.info(f"[Chat-look] 未识别成功，跳过历史上下文，如实告知")
        elif self.memory:
            # 从统一对话流倒读窗口（文件 I/O → 丢到线程里，别钉住事件循环）
            messages = await run_blocking(self.memory.get_messages, group_id)
        else:
            messages = [{"role": "user", "content": content}]

        # 【P1-4】窗口按预算裁剪：先算 system prompt 实际占了多少，剩下的才给对话窗口；
        # 从最新往旧塞，塞不下就停（保底 CONTEXT_MIN_ENTRIES 条，最近的话不能丢）。
        # Q1 的答复就是"让预算自动决定窗口多大"，所以这里不再固定条数。
        _sys_res = None
        # 看图场景（trigger=look）不注入 chat 场景说明：那时用户消息里已经带了图片描述与要求，
        # 再塞"有人在 @ 你 / 用 /问 跟你说话"只会互相干扰（干跑时抓到过这个矛盾）。
        _scene = "" if trigger == "look" else "chat"
        try:
            _sys_sections = await self._collect_sections(
                group_id=group_id, speaker_id=parsed.get("user_id"),
                image_description=image_description, vision_failed=vision_failed,
                scene=_scene,
            )
            _sys_res = self.assembler.compose_system(_sys_sections)
            _window_budget = max(400, Config.CONTEXT_MAX_CHARS - _sys_res.stats.get("合计", 0))
            messages, _win_chars = self.assembler.trim_messages(messages, budget=_window_budget)
            if Config.CONTEXT_LOG:
                logger.info(
                    f"[Context] group={group_id} "
                    + ContextAssembler.format_stats(
                        _sys_res.stats, _sys_res.dropped, _sys_res.truncated,
                        window_entries=len(messages), window_chars=_win_chars,
                    )
                )
        except Exception as e:      # 预算裁剪失败不该让回复失败，退回原始窗口
            logger.warning(f"[Context] 预算裁剪失败，使用完整窗口: {e}")

        # 3. 调用 LLM（带 Function Calling 工具）
        start = time.time()
        # 记录"谁在说话"，供 nickname 工具读取（tool_call_back 签名固定，传不进参数）
        self._speaker_ctx = {
            "group_id": group_id,
            "user_id": parsed.get("user_id"),
            "nick": parsed.get("nickname", ""),
        }
        try:
            # 构造可用的 tools —— 如果 search 模块没配置，就不注册 search 工具
            available_tools = list(_TOOLS)
            if self.search is None:
                available_tools = [t for t in available_tools if t["function"]["name"] != "search"]

            reply = await self.llm.chat(
                messages=messages,
                temperature=0.7,
                # 复用上面已经组装好的 system prompt（避免重复组装、重复打日志）
                system_prompt=(
                    _sys_res.text if _sys_res is not None
                    else await self._build_system_prompt(
                        image_description, group_id=group_id, speaker_id=parsed.get("user_id"),
                        vision_failed=vision_failed, scene=_scene,
                    )
                ),
                tools=available_tools,
                tool_call_back=self._tool_call_back,
            )
            elapsed = time.time() - start
            logger.info(
                f"[Chat] group={group_id} elapsed={elapsed:.2f}s "
                f"history_len={len(messages)} reply_len={len(reply)}"
            )
        except Exception as e:
            logger.error(f"[Chat] LLM 失败: {e}")
            await self.onebot.send_group_message(
                group_id, "刚才脑子抽了一下，AI没接上，稍后再试试 😣"
            )
            return
        finally:
            self._speaker_ctx = {}

        # 4. 记录回复到 memory
        if self.memory:
            await run_blocking(
                self.memory.add_assistant, group_id, reply,
                kind="vision" if has_image else "reply",
            )

        # 5. 发送（自动 @ 群成员）
        reply = await self._auto_mention(group_id, reply)
        # 正式回复也占用主动插嘴的 60 秒冷却：不然 /问、@bot 刚回完，
        # 下一条普通消息又立刻按概率插嘴，Bot 会显得话痨
        self._last_bot_reply_time[group_id] = time.time()
        await self._send_group(group_id, reply)

    # ---------- 搜索 ----------
    async def _handle_search(self, group_id: int, parsed: dict):
        query = parsed["args"].strip()
        if not query:
            await self._send_group(group_id, "搜什么？给个关键词吧 🔍")
            return

        if self.search is None:
            await self.onebot.send_group_message(
                group_id,
                "[占位回复] 搜索模块未配置，请在 .env 设置 SEARXNG_URL 或 SEARCH_API_KEY",
            )
            return

        try:
            results = await self.search.search(query)
        except Exception as e:
            logger.error(f"[Search] 异常: {e}")
            await self._send_group(group_id, "搜索没搜到，换个关键词试试？😵")
            return

        if not results:
            await self._send_group(group_id, "搜索没搜到，换个关键词试试？🤔")
            return

        # 交给 LLM 总结
        if self.llm:
            try:
                summary = await self.llm.summarize_search(query, results)
            except Exception as e:
                logger.error(f"[Search] LLM 总结失败: {e}")
                summary = self._format_results_fallback(query, results)
        else:
            summary = self._format_results_fallback(query, results)

        summary = await self._auto_mention(group_id, summary)
        await self._record_bot_message(group_id, summary, kind="search")
        await self._send_group(group_id, summary)

    @staticmethod
    def _format_results_fallback(query: str, results: list) -> str:
        """LLM 不可用时的简单格式"""
        lines = [f"关于「{query}」找到以下结果：\n"]
        for i, r in enumerate(results[:5], 1):
            title = r.get("title", "无标题")
            url = r.get("url", "")
            lines.append(f"{i}. {title}")
            if url:
                lines.append(f"   {url}")
            snip = r.get("snippet", "")
            if snip:
                lines.append(f"   {snip[:100]}")
            lines.append("")
        return "\n".join(lines)

    # ---------- 外号管理 ----------
    @staticmethod
    def _is_admin(user_id) -> bool:
        return bool(Config.ADMIN_QQ) and str(user_id) == str(Config.ADMIN_QQ)

    # ---------- 【P2a】/任务：定时任务的手动入口（仅管理员） ----------

    async def _cmd_task(self, group_id: int, parsed: dict):
        """`/任务 列表`、`/任务 试跑 <名>`、`/任务 执行 <名>`

        - 结果一律**私聊管理员**（Q2 的答复）：群里零打扰，手机上也方便看；
          私聊失败才退回群里说一句（否则管理员会以为没反应）。
        - 试跑 = 生成内容但不发送、不写状态、不计预算、不动冷却；
        - 执行 = 真发一次并正常记账（用于补播）。
        """
        user_id = parsed.get("user_id")
        if not self._is_admin(user_id):
            await self._send_group(group_id, "这个命令只有制作者能用哦～")
            return

        args = (parsed.get("content") or "").strip().split()
        # 去掉命令词本身
        if args and args[0] in ("/任务", "/task", "/tasks"):
            args = args[1:]
        action = args[0] if args else "列表"
        name = args[1] if len(args) > 1 else ""
        force = any(a in ("强制", "force", "-f") for a in args[2:])

        if action in ("列表", "list", "ls"):
            text = self._task_list_text(group_id)
        elif action in ("试跑", "dry", "dryrun", "试运行"):
            text = await self._task_run_text(group_id, name, dry_run=True)
        elif action in ("执行", "run", "立即执行"):
            text = await self._task_run_text(group_id, name, dry_run=False, force=force)
        elif action in ("重载", "reload"):
            self.scheduler.group_config.invalidate()
            self.scheduler.state._cache.pop(group_id, None)
            text = "群配置与任务状态缓存已重载 ✅"
        else:
            text = (
                "用法：\n"
                "  /任务 列表              看每个任务的状态\n"
                "  /任务 试跑 <任务名>     生成内容但不发送（调质量用）\n"
                "  /任务 执行 <任务名>     立即真发一次（补播用）\n"
                "  /任务 执行 <任务名> 强制 静默时段也发（需明确确认）\n"
                f"可用任务：{', '.join(sorted(self.scheduler.tasks))}"
            )

        ok = await self.onebot.send_private_message(user_id, text)
        if not ok:
            # 私聊不通（没加好友/被限制）→ 退回群里，避免"发了命令没反应"
            await self._send_group(group_id, text)

    def _task_list_text(self, group_id: int) -> str:
        st = self.scheduler.status(group_id).get(group_id) or {}
        lines = [
            f"📋 任务状态 · 群 {group_id}",
            f"  空闲 {st.get('idle_minutes', 0):.0f} 分钟 ｜ 静默时段 {st.get('quiet_hours')}"
            f"{'（当前处于静默）' if st.get('in_quiet') else ''}",
            f"  今日主动消息 {st.get('budget_used', 0)}/{st.get('budget_limit', 0)} 条"
            f" ｜ 允许主动打扰：{'是' if st.get('proactive_allowed') else '否'}",
            "",
        ]
        import datetime as _dt
        for name, t in (st.get("tasks") or {}).items():
            last = t.get("last_run") or 0
            last_s = (clock.from_timestamp(last).strftime("%m-%d %H:%M") if last else "从未")
            limit = t.get("daily_limit") or "∞"
            lines.append(
                f"· {name}（{t.get('label')}）{'✅' if t.get('enabled') else '⛔关闭'}"
                f"\n    触发：{t.get('trigger')} ｜ 下次：{t.get('next_eta') or '—'}"
                f"\n    上次：{last_s}（{t.get('last_result') or '无记录'}）"
                f" ｜ 今日 {t.get('count_today')}/{limit}"
                f" ｜ 退避 {t.get('backoff_level')}"
                + (f"\n    最近被拦：{t.get('last_skip_reason')}" if t.get("last_skip_reason") else "")
            )
        return "\n".join(lines)

    async def _task_run_text(self, group_id: int, name: str, *, dry_run: bool,
                             force: bool = False) -> str:
        if not name:
            return f"要指定任务名：{', '.join(sorted(self.scheduler.tasks))}"
        if name not in self.scheduler.tasks:
            return f"没有这个任务：{name}（可用：{', '.join(sorted(self.scheduler.tasks))}）"
        res = await self.scheduler.run_task(name, group_id, dry_run=dry_run, force=force)
        head = f"🧪 试跑 {name}（不消耗推送库存）" if dry_run else f"🚀 立即执行 {name}"
        if res.status == "dry_run":
            body = "\n\n".join(res.parts) if res.parts else "（没有生成内容）"
            return f"{head}（未发送）· {res.elapsed:.2f}s\n————\n{body}"
        if res.status == "sent":
            return f"{head} ✅ 已发送 {len(res.parts)} 条（{res.elapsed:.2f}s）\n————\n{res.content[:400]}"
        if res.status == "done":
            return f"{head} ✅ 已完成（无需发送内容）（{res.elapsed:.2f}s）"
        if res.status == "blocked":
            return f"{head} ⛔ 已被安全闸门拦下：\n{res.reason}"
        if res.status == "empty":
            return f"{head} ⏭ 本次跳过：{res.reason or '内容源没有内容'}"
        return f"{head} ❌ 失败：{res.reason or '未知错误'}"

    @staticmethod
    def _extract_at_qqs(parsed: dict) -> list[str]:
        """从原始 segment 里取出被 @ 的 QQ 号（排除 @全体）"""
        qqs: list[str] = []
        for seg in parsed.get("_raw_msg", []):
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq and qq != "all":
                qqs.append(qq)
        return qqs

    async def _alias_forbidden_names(self, group_id: int, self_qq) -> set[str]:
        """群内**其他人**的昵称/群名片（排除自己 & Bot），用于重名校验"""
        names: set[str] = set()
        bot_qq = str(Config.BOT_QQ) if Config.BOT_QQ else ""
        self_qq = str(self_qq)
        try:
            members = await self.onebot.get_group_members(group_id)
        except Exception as e:
            logger.warning(f"[Alias] 获取群成员失败: {e}")
            return names
        for m in members or []:
            uid = str(m.get("user_id", ""))
            if uid == bot_qq or uid == self_qq:
                continue
            for name in (m.get("nickname") or "", m.get("card") or ""):
                if name:
                    names.add(name)
        return names

    async def _cmd_alias_set_self(self, group_id: int, parsed: dict):
        alias = parsed["args"].strip()
        if not alias:
            await self._send_group(group_id, "想让我叫你什么呀？用法：/叫我 小明")
            return
        forbidden = await self._alias_forbidden_names(group_id, parsed["user_id"])
        _, msg = await run_blocking(
            self.alias.add,
            group_id, parsed["user_id"], alias,
            nick=parsed["nickname"],
            forbidden_names=forbidden,
            bot_name=Config.BOT_NAME,
            max_per_user=Config.ALIAS_MAX_PER_USER,
        )
        await self._send_group(group_id, msg)

    async def _cmd_alias_remove_self(self, group_id: int, parsed: dict):
        alias = parsed["args"].strip()
        if not alias:
            await self._send_group(group_id, "要删哪个外号？用法：/别叫我 小明（全删用 /别叫我了）")
            return
        _, msg = await run_blocking(self.alias.remove, group_id, parsed["user_id"], alias)
        await self._send_group(group_id, msg)

    async def _cmd_alias_clear_self(self, group_id: int, parsed: dict):
        _, msg = await run_blocking(self.alias.remove, group_id, parsed["user_id"], "")
        await self._send_group(group_id, msg)

    async def _cmd_alias_list_self(self, group_id: int, parsed: dict):
        mine = await run_blocking(self.alias.list_aliases, group_id, parsed["user_id"])
        if not mine:
            await self._send_group(group_id, "你还没设外号呢～ 用 /叫我 小明 设一个吧")
            return
        await self._send_group(group_id, "你的外号：" + "、".join(mine))

    async def _cmd_alias_admin_set(self, group_id: int, parsed: dict):
        if not self._is_admin(parsed["user_id"]):
            await self._send_group(group_id, "这个命令只有开发者能用哦～")
            return
        qqs = self._extract_at_qqs(parsed)
        # 从参数里去掉 @xxx 占位，剩下的就是外号
        alias = re.sub(r"@\S+", " ", parsed["args"]).strip()
        if not qqs:
            await self._send_group(group_id, "要 @ 一个人哦，用法：/设外号 @小明 明明")
            return
        if not alias:
            await self._send_group(group_id, "还要给个外号，用法：/设外号 @小明 明明")
            return
        target = qqs[0]
        # 取目标昵称（仅作为展示快照）
        nick = ""
        try:
            for m in await self.onebot.get_group_members(group_id) or []:
                if str(m.get("user_id", "")) == target:
                    nick = m.get("nickname") or m.get("card") or ""
                    break
        except Exception:
            pass
        forbidden = await self._alias_forbidden_names(group_id, target)
        ok, msg = await run_blocking(
            self.alias.add,
            group_id, target, alias,
            nick=nick,
            forbidden_names=forbidden,
            bot_name=Config.BOT_NAME,
            max_per_user=Config.ALIAS_MAX_PER_USER,
        )
        await self._send_group(
            group_id, f"好嘞，以后就叫 TA「{alias}」啦～" if ok else msg
        )

    async def _cmd_alias_admin_remove(self, group_id: int, parsed: dict):
        if not self._is_admin(parsed["user_id"]):
            await self._send_group(group_id, "这个命令只有开发者能用哦～")
            return
        qqs = self._extract_at_qqs(parsed)
        alias = re.sub(r"@\S+", " ", parsed["args"]).strip()
        if not qqs:
            await self._send_group(group_id, "要 @ 一个人哦，用法：/删外号 @小明 明明")
            return
        _, msg = await run_blocking(self.alias.remove, group_id, qqs[0], alias)
        await self._send_group(group_id, msg)

    # ---------- System Prompt 构造 ----------
    async def _collect_sections(
        self,
        *,
        group_id: int = 0,
        speaker_id: Optional[int] = None,
        image_description: str = "",
        vision_failed: bool = False,
        scene: str = "chat",
    ) -> list:
        """【P1-4】把 system prompt 拆成"带优先级的段"，交给预算组装器拼。

        以前是一路 `base += ...` 拼字符串，谁都不知道最后有多长、超了该丢谁。
        现在每段带优先级：MUST（人设/时间/开发者/图片）永不丢；HIGH（长期记忆/画像/话题）
        尽量留、不够就截断；LOW（风格/称呼/场景说明）预算紧时整段丢弃。
        """
        sections = [
            Section("人设", self.persona or "", MUST, hard_cap=1500),
        ]

        # 当前时间：对话流里只有"间隔够大"的消息才带相对时间，所以给个绝对锚点
        _now = clock.now()
        sections.append(Section(
            "时间",
            f"【当前时间】{_now.strftime('%Y-%m-%d %H:%M')} "
            f"{clock.WEEKDAY_CN[_now.weekday()]}（{clock.tz_label()}）",
            MUST,
        ))

        # 开发者身份
        if Config.ADMIN_QQ:
            admin_label = Config.ADMIN_NAME or f"QQ {Config.ADMIN_QQ}"
            text = (
                "【你的开发者】\n"
                f"群里的「{admin_label}」（QQ: {Config.ADMIN_QQ}）是你的开发者，"
                "也就是写你、维护你的人。你认得他，记得这件事。"
            )
            if speaker_id is not None and str(speaker_id) == str(Config.ADMIN_QQ):
                text += (
                    "\n→ 注意：现在正在跟你说话的人就是他本人。"
                    "对他可以更亲近、更随意一点（像对自己人），但别过度谄媚、别一直道谢；"
                    "他如果问起你自己的配置、功能、日志、代码这类事，照实回答，不要糊弄。"
                )
            sections.append(Section("开发者", text, MUST))

        # 【P1-1】长期记忆要点：窗口滑出去的对话压成的要点
        if group_id and self.summary:
            bullets = await run_blocking(self.summary.bullets_text, group_id)
            if bullets:
                sections.append(Section(
                    "要点",
                    "【长期记忆：你们之前聊过这些】\n" + bullets +
                    "\n（这些是更早的重点，需要时自然接上，不要生硬复述，也不要假装刚知道）",
                    HIGH, hard_cap=900,
                ))
            # 【P1-2】用户画像卡：当前说话人优先
            people = await run_blocking(
                self.summary.people_text, group_id, Config.PEOPLE_PROMPT_LIMIT, speaker_id
            )
            if people:
                sections.append(Section(
                    "画像", "【群友画像】\n" + people, HIGH, hard_cap=500,
                ))

        # 【P1-3】当前话题
        if group_id and self.topic:
            topic_text = await run_blocking(self.topic.prompt_text, group_id)
            if topic_text:
                sections.append(Section("话题", topic_text, HIGH, hard_cap=240))

        # 群风格画像（原有）
        if group_id and self.style:
            style_summary = self.style.get_summary(group_id)
            if style_summary:
                sections.append(Section(
                    "风格",
                    "【重要：这个群的说话风格】\n"
                    f"{style_summary}\n\n"
                    "请在回复中参考这种风格：使用相似的语气、句长和表达方式，"
                    "让你的发言看起来自然融入这个群，而不是一个机器人。",
                    LOW, hard_cap=600,
                ))

        # 群友称呼表（原有）
        if group_id and self.alias:
            view = await run_blocking(
                self.alias.get_prompt_view, group_id, Config.ALIAS_PROMPT_LIMIT
            )
            if view:
                lines = []
                for item in view:
                    nick = item["nick"]
                    aliases = "、".join(item["aliases"][:Config.ALIAS_MAX_PER_USER])
                    lines.append(f"- {nick}：希望被叫「{aliases}」")
                sections.append(Section(
                    "称呼",
                    "【群友称呼】\n以下群友给自己设了外号，主动提到他们时请优先用这些称呼：\n"
                    + "\n".join(lines),
                    LOW, hard_cap=400,
                ))

        # 场景说明（【P1-5】从 config/scenes/ 读，不再写死在代码里）
        if scene:
            scene_text = persona.load_scene(scene)
            if scene_text:
                sections.append(Section("场景", scene_text, LOW, hard_cap=400))

        # 图片相关（有图就必留，否则模型会"看不见"或编内容）
        if image_description:
            sections.append(Section(
                "图片",
                "【重要：关于刚刚用户发的图片】\n"
                "你拥有图像识别能力！你的视觉技能已经看过这张图片，以下是图片的准确描述：\n"
                f"{image_description}\n\n"
                "请基于上面的描述回答用户问题。绝对不要说'看不到'、'黑屏'、'加载不出来'之类的话！\n"
                "就当你亲眼看到了这张图一样，自然地聊！",
                MUST,
            ))
        elif vision_failed:
            sections.append(Section(
                "图片失败",
                persona.load_scene("image_fail") or
                "【重要】群友发了一张图，但这次没识别出内容。请如实说没看清、让对方再发一次，绝对不要猜测内容。",
                MUST,
            ))

        return sections

    async def _build_system_prompt(
        self,
        image_description: str = "",
        group_id: int = 0,
        speaker_id: Optional[int] = None,
        vision_failed: bool = False,
        scene: str = "chat",
    ) -> str:
        """按预算拼出最终 system prompt（P1-4 起走组装器）"""
        sections = await self._collect_sections(
            group_id=group_id, speaker_id=speaker_id,
            image_description=image_description, vision_failed=vision_failed, scene=scene,
        )
        res = self.assembler.compose_system(sections)
        if Config.CONTEXT_LOG:
            logger.info(
                f"[Context] group={group_id} {ContextAssembler.format_stats(res.stats, res.dropped, res.truncated)}"
            )
        return res.text

    # ---------- Function Calling 工具执行器 ----------
    async def _tool_call_back(self, name: str, args: dict) -> str:
        """
        provider.chat() 的回调。LLM 请求调用工具时会被自动调起。
        返回工具执行结果（字符串）。
        """
        if name == "search":
            return await self._tool_search(args)
        elif name == "current_time":
            return self._tool_current_time()
        elif name == "calc":
            return self._tool_calc(args)
        elif name == "roll":
            return self._tool_roll(args)
        elif name == "translate":
            return await self._tool_translate(args)
        elif name == "nickname":
            return await self._tool_nickname(args)
# elif name == "draw":  # 生图暂时关闭\n#     return await self._tool_draw(args)
        else:
            return f"未知工具: {name}"

    async def _tool_search(self, args: dict) -> str:
        query = args.get("query", "").strip()
        if not query:
            return "搜索关键词为空"
        if self.search is None:
            return "搜索功能未配置"
        try:
            results = await self.search.search(query)
        except RuntimeError:
            # 所有实例挂了 → 明确告诉 LLM "别再试了"
            return "搜索服务当前不可用（所有在线搜索引擎都连不上了），请告诉用户稍后再试或回答自己知道的内容。"
        except Exception as e:
            logger.error(f"[Tool/search] 异常: {e}")
            return f"搜索遇到未知错误: {e}，请告诉用户稍后再试。"
        if not results:
            return f"搜索成功但没找到关于「{query}」的相关结果。"
        # 返回精简格式供 LLM 二次总结
        lines = []
        for r in results[:6]:
            title = r.get("title", "无标题")
            snippet = r.get("snippet", "")[:150]
            url = r.get("url", "")
            lines.append(f"- {title}\n  摘要: {snippet}\n  链接: {url}")
        return "\n".join(lines)

    @staticmethod
    def _tool_current_time() -> str:
        now = clock.now()   # 带 Config.TIMEZONE，不看服务器 TZ
        weekday_cn = clock.WEEKDAY_CN[now.weekday()]
        return (
            f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"{weekday_cn}（{now.strftime('%H:%M')}，{clock.tz_label()}）"
        )

    @staticmethod
    def _tool_calc(args: dict) -> str:
        expr = args.get("expression", "").strip()
        if not expr:
            return "表达式为空"
        # 安全：只允许数字、运算符、括号、空格、小数点
        if not re.match(r"^[\d+\-*/().%\s^]+$", expr):
            return f"表达式包含不支持的字符: {expr}"
        # Python 的 ^ 是 XOR，换成 ** 是幂
        py_expr = expr.replace("^", "**")
        try:
            result = eval(py_expr, {"__builtins__": {}}, {})
            return f"计算结果: {expr} = {result}"
        except Exception as e:
            return f"计算失败: {e}（表达式: {expr}）"

    # ---------- roll ----------
    @staticmethod
    def _tool_roll(args: dict) -> str:
        sides = int(args.get("sides", 6))
        count = int(args.get("count", 1))
        if sides < 2 or sides > 10000:
            return f"骰子面数不合理: {sides}（范围 2~10000）"
        if count < 1 or count > 20:
            return f"骰子数量不合理: {count}（范围 1~20）"
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        if count == 1:
            return f"🎲 骰出了 {rolls[0]} 点（1d{sides}）"
        return f"🎲 骰出: {' + '.join(str(r) for r in rolls)} = {total}（{count}d{sides}）"

    # ---------- translate（免费 MyMemory API，无需 key）----------
    async def _tool_translate(self, args: dict) -> str:
        import httpx as _httpx
        text = args.get("text", "").strip()
        target = args.get("target_lang", "").strip() or "auto"
        if not text:
            return "文本为空"
        # 语言代码映射: zh → zh-CN, en → en, ja → ja, ko → ko
        lang_map = {"zh": "zh-CN", "zh-CN": "zh-CN", "en": "en", "ja": "ja", "ko": "ko"}
        target_code = lang_map.get(target, "auto")
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": text, "langpair": f"auto|{target_code}"},
                )
                if resp.status_code != 200:
                    return f"翻译服务暂时不可用（HTTP {resp.status_code}）"
                data = resp.json()
                translated = data.get("responseData", {}).get("translatedText", "")
                detected = data.get("responseData", {}).get("detectedLanguage", "")
                if not translated:
                    return f"翻译结果为空（原文: {text}）"
                lang_label = {"zh-CN": "中文", "en": "英文", "ja": "日文", "ko": "韩文"}.get(target_code, target_code)
                detected_label = lang_map.get(detected, detected)
                return f"【{detected_label} → {lang_label}】{translated}"
        except Exception as e:
            logger.error(f"[Tool/translate] 异常: {e}")
            return f"翻译失败: {e}"

    # 生图相关三个方法已暂时关闭\n    
    async def _tool_nickname(self, args: dict) -> str:
        """外号工具 —— 只能操作【当前说话人自己】的外号（安全边界）"""
        ctx = self._speaker_ctx or {}
        group_id = ctx.get("group_id")
        qq = ctx.get("user_id")
        if not group_id or qq is None:
            return "现在拿不到说话人的身份信息，操作失败"

        action = (args.get("action") or "").strip().lower()
        alias = (args.get("alias") or "").strip()

        if action == "list":
            mine = await run_blocking(self.alias.list_aliases, group_id, qq)
            if not mine:
                return "说话人还没有设置任何外号"
            return "说话人当前的外号：" + "、".join(mine)

        if action == "set":
            if not alias:
                return "没给外号内容，请让说话人说明想被叫什么"
            forbidden = await self._alias_forbidden_names(group_id, qq)
            _, msg = await run_blocking(
                self.alias.add,
                group_id, qq, alias,
                nick=ctx.get("nick", ""),
                forbidden_names=forbidden,
                bot_name=Config.BOT_NAME,
                max_per_user=Config.ALIAS_MAX_PER_USER,
            )
            return msg

        if action == "remove":
            if not alias:
                return "没给要删的外号内容"
            _, msg = await run_blocking(self.alias.remove, group_id, qq, alias)
            return msg

        return f"未知操作: {action}"

    async def _send_random_sticker(self, group_id: int, count: int = 1):
        """从全池表情包里随机挑几个发出去（不按群过滤，跨群也能发）"""
        picks = self.sticker.random(group_id=None, limit=count, prefer_same_group=False)
        if not picks:
            await self._send_group(group_id, "😶 还没收藏到任何表情包呢，多发几个自定义表情让我存存～")
            return
        for p in picks:
            cq = StickerManager.make_cq_image(p["path"])
            await self.onebot.send_group_message(group_id, cq)
        nick = picks[0].get("sender_nick", "")
        logger.info(f"[Sticker] 发出 {len(picks)} 个 (来源: {nick}) → 群 {group_id}")

    # ---------- 自动 @ 群成员 ----------
    async def _auto_mention(self, group_id: int, text: str) -> str:
        """
        扫描文本，把出现的群成员昵称（+外号）替换成 [CQ:at] 段。
        规则:
          - 优先匹配长名字（避免 "小明" 先于 "小明明"）
          - 跳过 Bot 自己
          - 只替换独立出现的名字（前后是标点/空格/开头结尾）
          - 不替换已经在 CQ 段里的
          - 外号优先用新表 data/aliases/{gid}.json（外号→QQ 直连）
          - 兜底兼容旧 config/aliases.json
        """
        if not text or not self.onebot:
            return text

        try:
            members = await self.onebot.get_group_members(group_id)
        except Exception as e:
            logger.warning(f"[Mention] 获取群成员失败: {e}")
            return text

        if not members:
            return text

        # 先做 user_id → 真实昵称 的映射（用来匹配外号映射表里的 key）
        bot_qq = str(Config.BOT_QQ) if Config.BOT_QQ else ""
        nick_to_uid: dict[str, str] = {}  # 昵称 → user_id
        for m in members:
            uid = str(m.get("user_id", ""))
            if uid == bot_qq:
                continue
            nick = m.get("nickname") or ""
            card = m.get("card") or ""
            if nick:
                nick_to_uid[nick] = uid
            if card and card != nick:
                nick_to_uid[card] = uid

        # 收集所有可匹配的名字（昵称 + 群名片 + 外号，去重）
        names_to_match: list[tuple[str, str]] = []  # [(user_id, display_name), ...]
        seen_names: set[str] = set()

        for m in members:
            uid = str(m.get("user_id", ""))
            if uid == bot_qq:
                continue
            for name in (m.get("nickname") or "", m.get("card") or ""):
                if len(name) >= 2 and name not in seen_names:
                    seen_names.add(name)
                    names_to_match.append((uid, name))

        # 优先：新外号表（data/aliases/{gid}.json），外号 → QQ 直连
        # 首次访问要读磁盘文件，丢到线程里取
        for alias, qq in (await run_blocking(self.alias.alias_to_qq, group_id)).items():
            alias = alias.strip()
            if len(alias) >= 2 and alias not in seen_names:
                seen_names.add(alias)
                names_to_match.append((qq, alias))
                logger.info(f"[Mention] 外号(新表)加载: '{alias}' → QQ {qq}")

        # 兜底：旧 config/aliases.json（新表已优先占位，同名不会覆盖）
        aliases = await run_blocking(self._load_aliases)
        for real_nick, alias_list in aliases.items():
            uid = nick_to_uid.get(real_nick)
            if not uid:
                continue  # 外号对应的人不在群里，跳过
            for alias in alias_list:
                alias = alias.strip()
                if len(alias) >= 2 and alias not in seen_names:
                    seen_names.add(alias)
                    names_to_match.append((uid, alias))
                    logger.info(f"[Mention] 外号(旧表)加载: '{alias}' → @{real_nick}")

        if not names_to_match:
            return text

        # 按名字长度倒序（长的先匹配）
        names_to_match.sort(key=lambda x: -len(x[1]))

        # 先把已有的 [CQ:at,xxx] 段保护起来，避免误伤
        _PROTECTED = "\x00CQAT\x00"
        placeholders: list[str] = []

        def _save_cqat(m: re.Match) -> str:
            placeholders.append(m.group(0))
            return f"{_PROTECTED}{len(placeholders) - 1}{_PROTECTED}"

        protected_text = re.sub(r"\[CQ:at[^\]]*\]", _save_cqat, text)

        # 逐个替换名字
        result = protected_text
        for user_id, name in names_to_match:
            pattern = rf'(?<![\u4e00-\u9fff])({re.escape(name)})(?![\u4e00-\u9fff])'
            uid_str = str(user_id)

            def _make_replacer(uid, disp):
                def _replacer(m):
                    return f"[CQ:at,qq={uid},display={disp}]"
                return _replacer

            new_result = re.sub(pattern, _make_replacer(uid_str, name), result)
            if new_result != result:
                logger.debug(f"[Mention] 替换 '{name}' → @{name}")
                result = new_result

        # 还原保护的 CQ 段
        def _restore(m: re.Match) -> str:
            idx = int(m.group(1))
            return placeholders[idx]

        final = re.sub(rf"{_PROTECTED}(\d+){_PROTECTED}", _restore, result)

        return final

    @staticmethod
    def _load_aliases() -> dict[str, list[str]]:
        """加载 config/aliases.json，返回 {真实昵称: [外号1, 外号2, ...]}"""
        import json as _json
        aliases_path = LEGACY_ALIASES_PATH
        try:
            if aliases_path.exists():
                with open(aliases_path, encoding="utf-8") as f:
                    data = _json.load(f)
                # 过滤掉 "_" 开头的注释字段
                return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.warning(f"[Mention] 加载 aliases.json 失败: {e}")
        return {}

    # ---------- 统一发送（自动分段 + 表情替换）----------
    async def _send_group(self, group_id: int, text: str, max_len: int = 350):
        """
        发送群消息。自动替换关键词为 QQ 表情 + 长消息分段。

        【P2a-hotfix-2 加固】这里是**所有出站消息的最后一道闸门**，
        所以在这里再做一次安全清洗（去掉 @全体成员 / @机器人自己）。
        任务内容在 `Task.execute()` 里已经洗过一次 —— 这么做是"防御纵深"：
        闲聊回复、表情配文、命令回复这些不经过 Task 的路径同样受保护。
        只删"@全体成员"和"@自己"，**不会**动正常的 @群友（那是合法用法）。
        """
        text = text.strip()
        if not text:
            return
        text = sanitize_outgoing(text)
        if not text:
            logger.warning("[Send] 清洗后内容为空，取消发送（原本可能只有 @全体成员）")
            return
        # 自动替换关键词为 QQ face 表情
        text = self._replace_faces(text)
        # 概率性 append 一个收藏的表情包（让 Bot 更像真人）
        sticker_cq = self._maybe_append_sticker(group_id)
        # 短消息直接发
        if len(text) <= max_len:
            payload = (text + "\n" + sticker_cq) if sticker_cq else text
            await self.onebot.send_group_message(group_id, payload)
            return
        # 长消息按段落切分
        paragraphs = text.split("\n")
        current = ""
        parts: list[str] = []
        for para in paragraphs:
            if len(current) + len(para) + 1 <= max_len:
                current = (current + "\n" + para).strip() if current else para
            else:
                if current:
                    parts.append(current)
                # 处理超长段落：按字符硬切
                while len(para) > max_len:
                    parts.append(para[:max_len])
                    para = para[max_len:]
                current = para
        if current:
            parts.append(current)

        # 发第一段
        await self.onebot.send_group_message(group_id, parts[0])
        # 后续段加衔接
        for i, part in enumerate(parts[1:], 1):
            suffix = f"...（{i + 1}/{len(parts)}）" if len(parts) > 2 else "..."
            # 最后一段 append 表情包
            if sticker_cq and i == len(parts) - 1:
                part = part + "\n" + sticker_cq
            # 必须用 await asyncio.sleep：time.sleep 会把整个事件循环一起睡死
            # （其他群的消息、WS 心跳、面板 SSE 全都要陪着等）
            await asyncio.sleep(0.3)  # 避免刷屏太快被风控
            await self.onebot.send_group_message(group_id, f"{suffix}\n{part}")

    # ---------- 自动插表情包 ----------
    def _maybe_append_sticker(self, group_id: int, prob: float = 0.25) -> str:
        """
        概率性返回一个随机表情包的 CQ 码，让 Bot 回复像真人一样带表情。
        prob: 触发概率（0~1），默认 25%。
        返回: CQ 码字符串（如 '[CQ:image,file=file:///...]'），或空串。
        """
        if self.sticker is None:
            return ""
        if self.sticker.count() == 0:
            return ""
        if random.random() > prob:
            return ""
        # 避免重复加（回复里已经有 CQ:image 了就跳过）
        # 调用方需要先检查文本，但这里也做个保险
        # 全池随机，不按群过滤（让 Bot 回复时的表情更丰富）
        pic = self.sticker.random(group_id=None, limit=1, prefer_same_group=False)
        if not pic:
            return ""
        pic = pic[0]  # random 返回 list，取第一个
        cq = self.sticker.make_cq_image(pic["path"])
        logger.info(f"[Sticker] 自动插表情: {pic.get('filename','?')[:40]} → group={group_id}")
        return cq

    @staticmethod
    def _replace_faces(text: str) -> str:
        """
        把文本里的关键词替换成 [CQ:face,id=xxx]。
        规则:
          - 长关键词优先（避免先替换 "牛" 再在 "牛666" 里重复替换）
          - 只替换独立出现的词（前后是标点/空格/开头结尾）
          - 不碰已经在 CQ 段里的
        """
        if not text or not _FACE_KEYWORDS:
            return text
        # 按词长从长到短排序
        sorted_words = sorted(_FACE_KEYWORDS.keys(), key=len, reverse=True)
        result = text
        for word in sorted_words:
            face_id = _FACE_KEYWORDS[word]
            # 独立匹配：前后是 \W（非单词字符）或开头结尾
            pattern = rf'(?<!\[CQ:)(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])'
            replacement = f'[CQ:face,id={face_id}]'
            result = re.sub(pattern, replacement, result)
        return result


