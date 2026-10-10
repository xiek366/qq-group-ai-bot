# 天童爱丽丝 · QQ 群 AI 聊天机器人

一个跑在自己服务器上的**轻量 QQ 群 AI 群友**。基于 [NapCat](https://github.com/NapNeko/NapCatQQ) 提供的 OneBot 11 接口接入 QQ，用 OpenAI 兼容接口（默认 DeepSeek）驱动对话，不依赖 Agent 框架、不依赖数据库，全部状态以 JSON / JSONL 落盘。

项目本身是个玩具，但把「像群友一样聊天」这件事做完整了：有记忆分层、有群风格画像、有定时主动发言、有管理面板。

## 功能

**聊天**

- `@机器人` 或 `/问` 触发对话，带群内上下文
- Function Calling 自动判断要不要联网搜索、报时、算数、掷骰、翻译
- 看图：`/看` 命令或直接发图，走独立的视觉管道
- 长回复自动拆成多条发送，语气自然不生硬

**记忆分层**

- **统一对话流**：群消息与 Bot 自己的发言都写入 `data/stream/<群号>.jsonl`，是状态层的唯一数据源
- **滚动摘要**：攒够消息后异步提炼长期要点与成员画像卡
- **话题状态**：记录当前聊到哪，用于上下文注入与过期管理
- **上下文预算组装器**：把 system prompt 拆成 `MUST / HIGH / LOW` 三档分配字符预算，超预算按优先级裁剪，而不是简单截断

**群风格画像**

自动收集群里的说话风格（常用语、emoji 频率、平均句长），定期用 LLM 提炼成画像并注入提示词。旧画像会作为长期基线参与滚动合并，避免风格被近期话题带偏。

**群友外号绑定**

成员可以给让 Bot 称呼自己特定的外号：

- 成员自己：`/叫我 XX`、`/别叫我 XX`、`/别叫我了`、`/我的外号`
- 管理员：`/设外号 @某人 XX`、`/删外号 @某人 [XX]`
- 也支持自然语言（走 Function Calling），但工具侧强制只作用于说话人自己
- 校验：长度 2~8、不含标点与 CQ 码字符、不与群内昵称或 Bot 名冲突、先到先得、每人上限 3 个

**定时任务框架**

三层抽象：`Scheduler`（什么时候做）→ `Task`（做什么、能不能做）→ `ContentSource`（内容从哪来）。外部世界全部通过回调注入，因此可以脱离 Bot 干跑。

已内置 7 个任务：

| 任务 | 触发 | 内容 |
|---|---|---|
| `icebreak` | 群安静 30 分钟后 | 冷场破冰（LLM 话题包 / 表情包 / 热搜兜底） |
| `summary_job` | 间隔 | 维护型，滚动摘要 |
| `weather_morning` | 每日 08:00 | 天气播报（Open-Meteo） |
| `water_reminder` | 间隔 75 分钟 | 喝水提醒 |
| `news_digest` | 定时 | RSS + B站专栏资讯摘要 |
| `daily_quote` | 每日 12:30 | 每日一句 |
| `daily_image` | 每日 19:30 | 每日一图 |

配套的闸门与风控：主动打扰白名单（只允许指定群收到主动消息，被动响应不受限）、静默/工作时段、任务日上限、群日总预算、同群互斥、**失败指数退避**（失败 1 次等 10 分、2 次 20 分、3 次 40 分…上限 60 分）。

**管理面板**

`aiohttp` 后端 + 单文件前端（Tailwind CDN + 原生 JS），token 鉴权，SSE 实时消息流。可以看到连接状态、每群被回应率、LLM 与视觉的健康度、对话流大小，并能试跑 / 手动触发定时任务。

**其它**

- 表情包：自动收藏群友发的表情包（MD5 去重、跳过超大文件），回复时概率性附带
- 群内置表情（face segment）会转成中文含义再喂给模型
- 指标观测：LLM 调用/失败/延迟/token、视觉命中率、每群被回应率，按天保留

## 架构

```
QQ ←→ NapCat (OneBot 11)
         │  WebSocket 收事件
         ▼
      OneBotClient ──► parse_message ──► MessageHandler
                                            │
              ┌─────────────────────────────┼─────────────────────────────┐
              ▼                             ▼                             ▼
        llm/provider                   context/*                    schedule/*
     (OpenAI 兼容 + 工具调用)    对话流 / 摘要 / 话题 / 预算组装   Scheduler / Task / ContentSource
              │                             │                             │
              ▼                             ▼                             ▼
        search/vision                 data/stream,summary,state      news / weather / icebreak…
              │                             │                             │
              └─────────────► _send_group（清洗 → 分段 → 表情 → 发送）◄─────┘
                                            │
                                       OneBot HTTP API
```

## 目录结构

```
app/
  main.py            入口：装配依赖、并行跑 OneBot 与面板
  config.py          全部配置从 .env 读取
  paths.py           所有数据目录的唯一来源（从 __file__ 推导，与 cwd 无关）
  metrics.py         指标记录与落盘
  persona.py         人设加载（常驻核心 + 场景片段）
  bot/
    onebot.py        OneBot WS 客户端 + HTTP 发送
    message.py       OneBot message segment 解析
    handler.py       消息分发中心（命令、聊天、看图、主动发言）
  context/           记忆分层：stream / summary / topic / topic_pool / assembler
  memory/            旧记忆 API 的兼容门面，已委托给 context/stream
  llm/               LLM provider（OpenAI 兼容）
  search/            SearXNG 搜索
  vision/            图片下载 → 校验 → 内联/降级 → 描述缓存
  alias/             群友外号管理
  sticker/           表情包收藏与随机挑选
  style/             群风格画像
  schedule/          调度框架与 7 个任务
  news/ weather/     资讯源与天气服务
  dashboard/         管理面板后端与前端
config/              人设、表情词典、场景片段、外号示例（可入库）
data/                运行时数据：对话流/摘要/状态/表情包/缓存/统计（绝不入库）
tests/               单元与端到端测试（含桩件，不依赖真实网络）
```

## 快速开始

**1. 准备 NapCat**

按 [NapCat 官方文档](https://github.com/NapNeko/NapCatQQ) 安装并登录一个 **QQ 小号**（不要用主号），开启 OneBot 11 的 WebSocket 服务端（默认 `3001`）与 HTTP 服务端（默认 `3000`）。

**2. 安装依赖**

```bash
pip install -r requirements.txt
```

需要 Python 3.10+。

**3. 配置**

```bash
cp .env.example .env
```

然后填 `.env`：至少要有 `LLM_API_KEY`、`BOT_QQ`、`ONEBOT_ACCESS_TOKEN`（若 NapCat 开了 token）、以及 `PROACTIVE_GROUPS`（允许被主动打扰的群，留空则所有群都会被主动打扰）。

`.env` 已在 `.gitignore` 中，**绝不要提交**。

**4. 运行**

```bash
python -m app.main
# 或者
python app/main.py
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `/ping` `/help` `/clear` | 连通性 / 帮助 / 清空本群对话历史 |
| `/问 <问题>` | AI 问答（带上下文） |
| `/搜 <关键词>` | 联网搜索 + AI 总结 |
| `/看` + 图片 | 看图（手机端不用 @） |
| `/表情` | 随机发一个已收藏的表情包 |
| `/天气 [城市]` | 天气查询 |
| `/资讯` | 手动拉一次资讯 |
| `/任务 列表 / 试跑 / 执行` | 查看与手动触发定时任务 |
| `/叫我` `/别叫我` `/我的外号` | 管理自己的外号 |

别名：`/h`、`/p`、`/c`、`/查`、`/e`、`/sticker`。

## 配置

全部环境变量见 [.env.example](.env.example)，按注释分组：OneBot、LLM、视觉、搜索、机器人、主动打扰范围、时区、外号、闲聊、破冰、管理面板。

几个容易踩的点：

- **`TIMEZONE`**：云服务器默认 UTC，破冰时段与报时都按这个变量算，必须配
- **`PROACTIVE_GROUPS`**：定时/主动功能只对这里列出的群生效；`@bot`、`/问` 等被动响应不受限制
- **`DASHBOARD_HOST`**：默认只监听 `127.0.0.1`，云端建议走 SSH 隧道，不要直接暴露公网
- **`LLM_MAX_TOKENS`**：小了会出现「思考没写完 → content 为空」，别低于 1024

## 测试

```bash
python -m pytest tests/ -q
```

测试使用桩件隔离网络与文件系统，不会真的连 QQ 或调用 LLM。

## 部署

目标环境 Ubuntu 22.04，最低 2 核 2G。用 systemd 托管两个服务：`napcat` 与 `qqbot`。

⚠️ **重启 NapCat 会丢失 QQ 登录态，需要重新扫码**——改代码后只重启 `qqbot` 即可：

```bash
systemctl restart qqbot
```

更细的部署与排障记录见 [DEPLOY.md](DEPLOY.md)。

## 文档

| 文件 | 内容 |
|---|---|
| [ROADMAP.md](ROADMAP.md) | 分阶段规划、技术选型决策、待办 |
| [DEVLOG.md](DEVLOG.md) | 开发日志：每个阶段做了什么、踩了什么坑 |
| [DEPLOY.md](DEPLOY.md) | 部署与排障手册 |
| [DEPLOY_SYNC.md](DEPLOY_SYNC.md) | 本地↔服务器同步规则与发布记录 |

## 免责声明

- 本项目是个人玩具项目，**请使用专用 QQ 小号**运行，使用主号存在被风控或封禁的风险
- 群聊记录、成员昵称等数据会落盘在 `data/`，请自行评估隐私与合规问题
- 定时主动发言会真实打扰群成员，建议先用测试群验证，并通过 `PROACTIVE_GROUPS` 限制范围