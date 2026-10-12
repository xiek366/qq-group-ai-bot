"""
全局配置 —— 从 .env 读取，使用 python-dotenv。
所有 API Key 都通过环境变量注入，不硬编码。

如果 ONEBOT_ACCESS_TOKEN 在 .env 里没填，会自动尝试从 NapCat 配置文件里读取
（Windows 和 Linux 的常见安装路径都会扫）。
"""
import json
import os
from dotenv import load_dotenv

from app import paths

load_dotenv()


def _auto_load_napcat_token() -> str:
    """扫描常见 NapCat 安装路径，自动读取 OneBot token（Windows + Linux 都试）"""
    # 1. 先看 .env 里有没有指定 NapCat 配置目录
    explicit = os.getenv("NAPCAT_CONFIG_DIR", "")
    search_dirs = []
    if explicit:
        search_dirs.append(explicit)
    # 2. 常见默认路径
    search_dirs += [
        # --- Windows 本地开发 ---
        r"C:\NapCat\Shell\config",
        r"C:\NapCat\config",
        r"D:\NapCat\Shell\config",
        r"D:\NapCat\NapCat.Shell.Windows.Node\config",
        os.path.expanduser(r"~\Documents\Tencent Files\NapCat\config"),
        # --- Linux 云端（布局见 DEPLOY.md）---
        "/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config",
        "/root/NapCat/opt/QQ/resources/app/app_launcher/napcat/config",
        "/opt/QQ/resources/app/app_launcher/napcat/config",
        os.path.expanduser("~/Napcat/opt/QQ/resources/app/app_launcher/napcat/config"),
    ]
    # 3. 逐个找 onebot11_*.json
    import glob
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        matches = glob.glob(os.path.join(d, "onebot11_*.json"))
        for cf in matches:
            try:
                with open(cf, encoding="utf-8") as f:
                    data = json.load(f)
                for ws in data.get("network", {}).get("websocketServers", []):
                    tok = ws.get("token", "")
                    if tok:
                        print(f"[Config] 自动读取 NapCat token: {tok} (来自 {cf})")
                        return tok
            except Exception:
                continue
    return ""


class Config:
    # ===== OneBot / NapCat =====
    ONEBOT_WS_URL: str = os.getenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
    ONEBOT_HTTP_PORT: int = int(os.getenv("ONEBOT_HTTP_PORT", "3000"))
    # 优先级: .env 显式配置 > 自动从 NapCat 配置读取 > 空
    ONEBOT_ACCESS_TOKEN: str = (
        os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()
        or _auto_load_napcat_token()
    )

    # ===== LLM =====
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")

    # --- 生成参数 ---
    # max_tokens 要留够：思考模式（默认开启）的思维链也吃这份预算，
    # 给小了会出现「思维链没写完就被截断 → content 为空」。
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    # 思考模式开关（enabled / disabled）。
    # 注意：思考模式下 temperature 不生效（官方明确不支持），
    # 群聊场景想要"便宜、快、温度可控"就设 disabled。
    LLM_THINKING: str = os.getenv("LLM_THINKING", "enabled").strip().lower()
    # 思考强度（low/high/max），留空用服务端默认
    LLM_REASONING_EFFORT: str = os.getenv("LLM_REASONING_EFFORT", "").strip()

    # --- 看图（视觉）参数 ---
    # 看图只需要一段描述，不需要思维链；关掉思考更省更快，也避免 content 为空
    VISION_THINKING_DISABLED: bool = (
        os.getenv("VISION_THINKING_DISABLED", "true").strip().lower() == "true"
    )
    VISION_MAX_TOKENS: int = int(os.getenv("VISION_MAX_TOKENS", "1024"))

    # --- 【P0-3】视觉管道参数 ---
    # 下载图片的超时（秒）。比 HTTP_TIMEOUT 宽一点，腾讯图床偶尔慢
    VISION_DOWNLOAD_TIMEOUT: float = float(os.getenv("VISION_DOWNLOAD_TIMEOUT", "20"))
    # 单图超过这个大小就不内联，退回用 URL 交给模型自己下载（Q1 决策：不引入 Pillow 压缩）
    VISION_INLINE_MAX_MB: float = float(os.getenv("VISION_INLINE_MAX_MB", "4"))
    # 一次请求内联的总字节上限（官方请求体上限 48MiB，这里留足余量）
    VISION_INLINE_TOTAL_MB: float = float(os.getenv("VISION_INLINE_TOTAL_MB", "12"))
    # 图片描述的缓存天数（同一张图重复发就不再请求视觉接口）
    VISION_CACHE_DAYS: int = int(os.getenv("VISION_CACHE_DAYS", "30"))

    # ===== 搜索 =====
    SEARXNG_URL: str = os.getenv("SEARXNG_URL", "https://searxng.site")

    # ===== 图片生成 =====
    # pollinations = 免费 / 即梦 = 付费 / "" = 关闭图片功能
    IMAGE_PROVIDER: str = os.getenv("IMAGE_PROVIDER", "pollinations")
    IMAGE_MODEL: str = os.getenv("IMAGE_MODEL", "flux-anime")  # flux-anime=二次元

    # ===== 机器人 =====
    BOT_NAME: str = os.getenv("BOT_NAME", "AI助手")
    BOT_QQ: str = os.getenv("BOT_QQ", "")  # 机器人自己的 QQ 号

    # ===== 开发者 / 管理员 =====
    # 填了 ADMIN_QQ，Bot 就知道这个号是它的开发者（只做认知层，不给额外权限）
    ADMIN_QQ: str = os.getenv("ADMIN_QQ", "")
    ADMIN_NAME: str = os.getenv("ADMIN_NAME", "")   # 开发者昵称/称呼，仅用于提示词

    # ===== 群友外号 =====
    ALIAS_MAX_PER_USER: int = int(os.getenv("ALIAS_MAX_PER_USER", "3"))    # 每人最多几个外号
    ALIAS_PROMPT_LIMIT: int = int(os.getenv("ALIAS_PROMPT_LIMIT", "15"))   # 注入提示词的人数上限

    MAX_HISTORY: int = int(os.getenv("MAX_HISTORY", "20"))
    MAX_REPLY_LENGTH: int = int(os.getenv("MAX_REPLY_LENGTH", "500"))

    # ===== 【D15】主动/定时打扰的群范围 =====
    # 只允许这些群收到"主动消息"：破冰、主动插嘴、没 @ 也发图的自动回复。
    # 被动响应（@bot、/问、/看、命令）**不受此限制**，任何群都能用。
    # 留空 = 不限制（所有群都会被主动打扰）—— 部署时建议只填实例群。
    # 支持中文逗号，非数字项会被忽略。
    PROACTIVE_GROUPS: list[int] = [
        int(g)
        for g in os.getenv("PROACTIVE_GROUPS", "").replace("，", ",").split(",")
        if g.strip().isdigit()
    ]

    # ===== 闲聊（主动插嘴）=====
    CHAT_REPLY_CHANCE: int = int(os.getenv("CHAT_REPLY_CHANCE", "15"))
    CHAT_COOLDOWN: int = int(os.getenv("CHAT_COOLDOWN", "60"))
    CHAT_KEYWORDS: list[str] = [
        k.strip() for k in os.getenv("CHAT_KEYWORDS", "").split(",") if k.strip()
    ]

    # ===== 破冰（冷场自动抛话题）=====
    ICEBREAK_ENABLED: bool = os.getenv("ICEBREAK_ENABLED", "true").lower() == "true"
    ICEBREAK_IDLE_MINUTES: int = int(os.getenv("ICEBREAK_IDLE_MINUTES", "30"))
    ICEBREAK_COOLDOWN_MINUTES: int = int(os.getenv("ICEBREAK_COOLDOWN_MINUTES", "60"))
    ICEBREAK_WATCH_INTERVAL: int = int(os.getenv("ICEBREAK_WATCH_INTERVAL", "300"))
    ICEBREAK_CHANCE: float = float(os.getenv("ICEBREAK_CHANCE", "0.6"))
    ICEBREAK_WORK_START: int = int(os.getenv("ICEBREAK_WORK_START", "8"))
    ICEBREAK_WORK_END: int = int(os.getenv("ICEBREAK_WORK_END", "23"))
    ICEBREAK_MIN_GROUP_SIZE: int = int(os.getenv("ICEBREAK_MIN_GROUP_SIZE", "3"))
    CHAT_IGNORE_KEYWORDS: list[str] = [
        k.strip() for k in os.getenv("CHAT_IGNORE_KEYWORDS", "").split(",") if k.strip()
    ]
    CHAT_MAX_CONTEXT: int = int(os.getenv("CHAT_MAX_CONTEXT", "5"))

    # ===== 人设 =====
    # 路径全部由 app/paths.py 推导（基于文件位置，不看工作目录）
    PERSONA_PATH: str = str(paths.PERSONA_PATH)

    # ===== 【P1】记忆分层：滚动摘要 / 用户画像卡 / 上下文预算 =====
    # 摘要触发：攒够这么多条"还没进过摘要"的消息，且距上次 ≥ 冷却分钟，就后台跑一次
    SUMMARY_ENABLED: bool = os.getenv("SUMMARY_ENABLED", "true").lower() == "true"
    SUMMARY_MIN_MESSAGES: int = int(os.getenv("SUMMARY_MIN_MESSAGES", "40"))
    SUMMARY_COOLDOWN_MINUTES: int = int(os.getenv("SUMMARY_COOLDOWN_MINUTES", "10"))
    # 单次最多摘要多少条（防止一次塞太多把 prompt 撑爆）
    SUMMARY_BATCH_MAX: int = int(os.getenv("SUMMARY_BATCH_MAX", "60"))
    # 长期要点保留条数（超出后最老的滚进 archived，仍保留但不再注入）
    SUMMARY_KEEP_BULLETS: int = int(os.getenv("SUMMARY_KEEP_BULLETS", "5"))
    # 用户画像卡：每群最多留多少人 / 注入 prompt 的最多人数
    PEOPLE_MAX: int = int(os.getenv("PEOPLE_MAX", "20"))
    PEOPLE_PROMPT_LIMIT: int = int(os.getenv("PEOPLE_PROMPT_LIMIT", "8"))
    # 上下文预算（**字符**为单位；没有 tokenizer，不假装精确）
    CONTEXT_MAX_CHARS: int = int(os.getenv("CONTEXT_MAX_CHARS", "6000"))
    # 无论预算多紧，至少保留这么多条最近对话
    CONTEXT_MIN_ENTRIES: int = int(os.getenv("CONTEXT_MIN_ENTRIES", "6"))
    # 话题状态（当前话题记录与注入）
    TOPIC_ENABLED: bool = os.getenv("TOPIC_ENABLED", "true").lower() == "true"
    TOPIC_STALE_MINUTES: int = int(os.getenv("TOPIC_STALE_MINUTES", "90"))
    # 每次组装上下文都打一行占用日志（人设/要点/画像/窗口各占多少）
    CONTEXT_LOG: bool = os.getenv("CONTEXT_LOG", "true").lower() == "true"

    # ===== 【P2c】资讯 / 每日一句 / 每日一图 / 话题库 =====
    NEWS_ENABLED: bool = os.getenv("NEWS_ENABLED", "true").lower() == "true"
    # 源清单：留空 = 用预置（机核/游研社 + B站三个 UP）；想裁剪就写 id，逗号分隔
    #   RSS 的 id：gcores / yystv       B站的 id 就是 UP 主 mid
    NEWS_RSS_SOURCES: str = os.getenv("NEWS_RSS_SOURCES", "")
    NEWS_BILI_MIDS: str = os.getenv("NEWS_BILI_MIDS", "")
    # 每个源的冷却分钟数（A.6 建议 30~60；连续失败会翻倍退避，最多 ×8）
    NEWS_COOLDOWN_MINUTES: int = int(os.getenv("NEWS_COOLDOWN_MINUTES", "45"))
    # 只推这么新的条目（小时）；太旧的不推，避免"补发上周的新闻"
    NEWS_MAX_AGE_HOURS: int = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))
    NEWS_IMAGE_MAX_MB: float = float(os.getenv("NEWS_IMAGE_MAX_MB", "8"))
    # COLG 论坛源（bbs.colg.cn，HTML 解析，无 RSS）：质量闸 = 回复≥N 或 浏览≥M（都低才丢）
    # FIDS 留空 = 预置 171（国服心情咖啡屋）；要加韩版填 "171,170"
    NEWS_COLG_ENABLED: bool = os.getenv("NEWS_COLG_ENABLED", "true").lower() == "true"
    NEWS_COLG_FIDS: str = os.getenv("NEWS_COLG_FIDS", "")
    NEWS_COLG_MIN_REPLIES: int = int(os.getenv("NEWS_COLG_MIN_REPLIES", "5"))
    NEWS_COLG_MIN_VIEWS: int = int(os.getenv("NEWS_COLG_MIN_VIEWS", "500"))
    # 任务时刻：资讯 11:30 / 每日一句 12:30 / 每日一图 19:30（错开，别挤在一起）
    NEWS_DIGEST_AT: str = os.getenv("NEWS_DIGEST_AT", "11:30")
    DAILY_QUOTE_AT: str = os.getenv("DAILY_QUOTE_AT", "12:30")
    DAILY_IMAGE_AT: str = os.getenv("DAILY_IMAGE_AT", "19:30")
    # 每日一句：台词池为主，LLM 偶尔原创（Q2）—— 每 N 天允许一次原创，0 = 完全不用 LLM
    QUOTE_LLM_EVERY_DAYS: int = int(os.getenv("QUOTE_LLM_EVERY_DAYS", "4"))
    # 【P2c-hotfix-4】任务连续失败时的指数退避（2026-10-08 线上事故：失败不退避 → 每 tick 重试）
    TASK_FAIL_BACKOFF_BASE_MINUTES: int = int(os.getenv("TASK_FAIL_BACKOFF_BASE_MINUTES", "10"))
    TASK_FAIL_BACKOFF_MAX_MINUTES: int = int(os.getenv("TASK_FAIL_BACKOFF_MAX_MINUTES", "60"))
    # 摘要任务的 LLM 超时（**单独放宽**：它一次要喂 40+ 条消息，比闲聊大得多，
    # 沿用全局 15 秒会稳定超时 —— 线上 10:17 那一串超时就是这么来的）
    SUMMARY_LLM_TIMEOUT: float = float(os.getenv("SUMMARY_LLM_TIMEOUT", "45"))

    # ===== 【P2b】天气播报与提醒 =====
    WEATHER_ENABLED: bool = os.getenv("WEATHER_ENABLED", "true").lower() == "true"
    # provider 可插拔：open-meteo（无需 key，本期采用）| qweather（将来要生活指数时再切）
    WEATHER_PROVIDER: str = os.getenv("WEATHER_PROVIDER", "open-meteo")
    # 全局兜底城市；留空 = 没有单群绑定的群一律不播报（宁可没有，也不要发错城市）
    WEATHER_CITY: str = os.getenv("WEATHER_CITY", "")
    WEATHER_BROADCAST_AT: str = os.getenv("WEATHER_BROADCAST_AT", "08:00")
    # 收尾句是否用 LLM 润色一次（Q1：开，但 LLM 失败必须回落到模板）
    WEATHER_POLISH: bool = os.getenv("WEATHER_POLISH", "true").lower() == "true"
    # 喝水/久坐提醒（Q3：按 D18）
    WATER_REMINDER_ENABLED: bool = os.getenv("WATER_REMINDER_ENABLED", "true").lower() == "true"
    WATER_REMINDER_INTERVAL_MINUTES: int = int(os.getenv("WATER_REMINDER_INTERVAL_MINUTES", "75"))
    WATER_REMINDER_WINDOW: str = os.getenv("WATER_REMINDER_WINDOW", "10:00-18:00")
    WATER_REMINDER_DAILY_LIMIT: int = int(os.getenv("WATER_REMINDER_DAILY_LIMIT", "4"))
    WATER_REMINDER_ACTIVE_WITHIN: int = int(os.getenv("WATER_REMINDER_ACTIVE_WITHIN", "30"))

    # ===== 【P2a】调度框架：tick、打扰预算、静默时段 =====
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
    # 调度器多久扫一遍（秒）。30 秒足够：最小粒度是分钟级
    SCHEDULER_TICK_SECONDS: int = int(os.getenv("SCHEDULER_TICK_SECONDS", "30"))
    # 静默时段（按群时区）：这段时间内不主动说话，支持跨午夜
    QUIET_HOURS: str = os.getenv("QUIET_HOURS", "23:00-08:00")
    # 【D18】所有主动消息合计的每群每日上限（最后一道闸）
    DAILY_TOTAL_LIMIT: int = int(os.getenv("DAILY_TOTAL_LIMIT", "8"))
    # cron 任务迟到超过这么多分钟就不补了（避免服务重启后半夜突然播报早报）
    CRON_CATCH_UP_MINUTES: int = int(os.getenv("CRON_CATCH_UP_MINUTES", "90"))
    # 主动消息发出后多久没人说话算"无人应答"（用于退避）
    NO_REPLY_GRACE_MINUTES: int = int(os.getenv("NO_REPLY_GRACE_MINUTES", "20"))
    # 任务执行记录保留天数（面板展示 + 便于回溯；D18 定的是 14 天）
    TASK_LOG_KEEP_DAYS: int = int(os.getenv("TASK_LOG_KEEP_DAYS", "14"))

    # ===== 时区 =====
    # 破冰的工作时间窗、报时工具都用它算时间。
    # 云端服务器常常是 UTC，靠这个配置纠正，别依赖服务器 TZ。
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Shanghai")

    # ===== 网络超时（秒） =====
    HTTP_TIMEOUT: float = 15.0
    WS_RECONNECT_INTERVAL: float = 5.0

    # ===== 日志 =====
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ===== 管理面板 =====
    DASHBOARD_ENABLED: bool = os.getenv("DASHBOARD_ENABLED", "true").lower() == "true"
    # 默认只监听本机：云端走 SSH 隧道访问，别把面板暴露到公网。
    # 确实需要公网直连时再改成 0.0.0.0，并且务必配强 token + 云安全组限制来源。
    DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))
    DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "admin")


# 人设内容 —— 【P1-5】优先用常驻核心 config/persona_core.txt（约 400 字），
# 缺失时由 app/persona.py 自动回退到完整的 persona.txt，保证不会"没有人设"。
def load_persona() -> str:
    from app.persona import load_core
    return load_core()
