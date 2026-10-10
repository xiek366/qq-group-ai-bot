# 开发记录（DEVLOG）

> 本文件是**动工前的必填项**：每次开始开发前先在这里写清范围、设计、待确认问题、测试计划与验收标准，
> 然后才动代码。开发过程中把实际改动、测试结果、偏差追加到当次记录里。

---

## 工作约定（用户要求，2026-10-07 确立）

1. **动工前先写记录**：每次开工前在本文档新增/更新一节（范围、设计、待确认、测试计划、验收标准）。
2. **不确定就反馈**：遇到拿不准的技术点或需求，先问用户，不自行假设。
3. **拿不准的需求反复确认**：宁可多问几轮，也不做偏。
4. **完成的定义 = 开发 + 测试 + 验证**：三项齐了才算完成，缺一项视为未完成。
5. 记录要能自证：写清"改了什么、为什么、怎么测的、结果如何"，便于回溯与接手。
6. **每次改动都要能完整同步到云端**（2026-10-07 追加）：按 [DEPLOY_SYNC.md](DEPLOY_SYNC.md) 的流程发布；
   若改动涉及**数据结构/文件布局变化**，必须在当次记录里写明**迁移步骤、验证方式、回滚方法**；
   涉及**新配置项**要同步更新 `.env.example`，涉及数据/日志变化要同步更新 [DEPLOY.md](DEPLOY.md) 的路径表与关键词表；
   需要用户在服务器上手工做的事，单独列"服务器侧操作"一节。
7. **发布交付格式 = 文件替换清单**（2026-10-07 用户确认）：用户**不做备份/rsync/同步工具**，只做"替换文件"。
   所以每次发布我必须给出：**服务器路径 / 操作（🆕新建 or 覆盖）/ 本地来源 / 替换后一条 `grep -c` 自检命令与期望值**；
   若需要"删除/改名"服务器文件（逐文件替换做不到），单独列「需要手动删除」。

### 记录模板

```md
## [阶段-编号] 标题

- 日期：
- 状态：规划中 / 开发中 / 待验证 / 已完成 / 已阻塞
- 关联：ROADMAP 章节 / 决策编号

### 1. 范围
（这次做什么、明确不做什么）

### 2. 设计要点
（数据结构、文件、接口、关键取舍）

### 3. 待确认问题
（编号 Q1..Qn，每个都写上我的倾向与理由；用户答复后回填）

### 4. 测试计划
（单测 / 干跑 / 真实验证，各自的判定标准）

### 5. 验收标准
（可判定的通过条件）

### 6. 实施记录
（实际改动文件清单、命令、结果、偏差与原因）

### 7. 复盘
（踩到的坑、遗留问题、下一阶段要注意什么）
```

---

## [P0-0] P0 阶段开工准备

- 日期：2026-10-07
- 状态：**P0 开发+单测+干跑验证已完成；仅剩"部署到服务器 + 测试群真实验证"待用户配合**
- 关联：ROADMAP 第 9 节 P0、D15/D18/D19、附录 A

### 1. 范围

P0「地基」共四项，本次一并做完：

| 编号 | 内容 | 为什么是 P0 |
|---|---|---|
| P0-1 | **统一对话流**：所有群消息（含 bot 自己的发言）写入 `data/stream/<群ID>.jsonl`，带 `ts/role/user_id/nick/kind`；`memory.get_messages()` 改为从流里取窗口 | 直接解决"忘记说过的话、接不上话题"；也是 P2 效果统计与周报的数据基础 |
| P0-2 | **消息时间戳 + 相对时间** | 让 bot 能区分"刚才"和"昨天"，是"延续话题"的前提 |
| P0-3 | **视觉管道加固**：下载 → 校验 → （可选压缩）→ base64 内联 → 失败重试 → 失败话术 → MD5 缓存 → 结果写入对话流 | 修"看图返回空、答非所问"；顺带让"刚才那张图"可被引用 |
| P0-4 | **结构化日志 + 面板健康卡** | 没有度量就没有后续优化依据 |

**明确不做**（留给后续阶段）：不做调度器/定时任务（P2a）、不做消息摘要（P1）、不做 RSS/B站源（P2c）、
不改人设文件结构（P1 再精简）。

### 2. 设计要点（初稿）

**对话流文件**：`data/stream/<群ID>.jsonl`，一行一条 JSON，append-only：

```json
{"ts": 1791234567.12, "role": "user", "user_id": 888888, "nick": "小明", "kind": "chat", "text": "…"}
{"ts": 1791234570.01, "role": "assistant", "kind": "reply", "text": "…"}
```

- `kind` 取值：`chat`（普通群聊）/ `reply`（@bot 或 /问 的回复）/ `proactive`（插嘴、破冰）/ `vision`（看图）/ `command`（命令类）
- 写入与读取都走 `app/util/blocking.py` 的单 worker 线程，**不在事件循环里做文件 I/O**（沿用 P0 之前已确立的约定）
- 窗口读取：从文件尾部倒着读 N 条（N 由 token 预算决定），不做全文件解析
- 保留期：30 天（D18），由 `cleanup` 任务在 P3 落地前先手工/脚本清理

**上下文组装**：在现有 `memory.get_messages()` 的位置替换为"从对话流取最近窗口"，行为对上层透明。

**时间注入**（我的倾向，可改）：不逐条打时间戳（省 token），只在**跨度较大时**插入标记：
上一条与当前条间隔 > 1 小时 → 当前条前缀 `[昨天 14:30]` / `[3 小时前]`；同时在 system prompt 里给出"当前时间"。

**视觉管道**：`app/vision/` 新模块，负责下载/校验/内联/缓存/重试/失败话术；`llm.chat_with_images` 退化为"纯调用"。

**面板健康卡**：新增 `/api/health`，指标：每群消息数（今日/7日）、bot 发言数、被回应率、
LLM 调用次数与失败率、平均延迟、token 用量估算、视觉成功率、对话流文件大小。

### 3. 待确认问题（✅ 已全部答复，2026-10-07）

| 编号 | 问题 | 我的倾向 | 用户答复 |
|---|---|---|
| Q1 | 视觉加固要不要引入 **Pillow** 做本地压缩/转码？ | 不引入（保持 4 个依赖）：只做魔数校验 + 大小上限 + base64 内联，超限改走 URL 或放弃 | ✅ **不引入 Pillow** |
| Q2 | 现有 `data/memory/*.json`（3 个群的历史）如何进入新对话流？ | 一次性迁移：按原顺序转成 stream 条目（时间戳用文件 mtime 兜底），旧文件备份为 `.migrated` | ✅ **一次性迁移** |
| Q3 | 测试群 100000005 能否用于验证**聊天链路**（对话流/看图）？D15 限定的是"定时/主动功能"，我理解被动响应不受限 | 可以在测试群验证（只在被 @ 时响应，不主动打扰） | ✅ **可以在测试群验证** |

**Q1 的连带约束（已写进实现）**：没有 Pillow，因此

- 图片校验只做「魔数（JPEG/PNG/GIF/WebP）+ 文件大小上限」，**不做像素级尺寸解析**；
- 超大图（> 阈值）不压缩，直接**降级为 URL 方式**提交，URL 也失败就放弃并走"没看清"话术；
- 若日后日志显示大图失败频繁，再作为独立决策评估是否引入 Pillow（并记进 ROADMAP §10）。

### 4. 测试计划

| 层级 | 内容 | 判定标准 |
|---|---|---|
| 单测 | 对话流读写（追加/倒读窗口/并发写不丢数据）、时间标记生成、视觉校验与缓存、健康卡统计 | `tests/` 下新增用例全绿，且不影响现有 5 个测试文件 |
| 干跑 | 用桩 LLM/桩 OneBot 跑完整分发链路（沿用 `test_handler_dispatch.py` 的桩件思路） | 消息正确写入对话流；看图失败走"没看清"话术；不猜内容 |
| 真实验证 | 在测试群 100000005（若 Q3 通过）发几条消息 + 一张图 | 日志与对话流文件对得上；`/看` 能正常回复；无异常堆栈 |

### 5. 验收标准（对齐 ROADMAP 第 9 节 P0 行）

1. 破冰/回复后有人接话，bot 能引用**自己刚说过的内容**（对话流含 assistant 条目）；
2. 同一张图第二次发**不重复请求**视觉接口（MD5 缓存命中）；
3. 识别失败时给明确话术且**绝不猜图片内容**；
4. 重启后对话流不丢，窗口读取正常；
5. 面板健康卡能看到上述指标；
6. 全部单测通过，`python -m compileall` 无错。

### 6. 实施记录

#### 6.1 P0-1 统一对话流 + P0-2 时间戳（**已完成**，2026-10-07）

**改动文件**

| 文件 | 改动 |
|---|---|
| `app/paths.py` | 新增 `STREAM_DIR`、`CACHE_DIR`、`VISION_CACHE_DIR`、`STATS_DIR`（为 P0-3/P0-4 预留），`_DATA_SUBDIRS` 同步 |
| `app/context/stream.py` | **新增**：`ConversationStream`（append-only JSONL、倒读窗口、时间标记、旧记忆迁移、备份式 clear） |
| `app/memory/memory.py` | **重写为兼容门面**：`add_user/add_assistant/get_messages/size/clear` 全部委托给对话流；新增 `total_entries()` |
| `app/main.py` | 启动期显式调用 `memory.stream.migrate_all()` 并打日志 |
| `app/bot/handler.py` | ①所有群消息入流（含命令，见 `_remember_group_msg`）；②新增 `_record_bot_message()`，在 **6 处**发言点入流（看图/破冰×3/插嘴/搜索）；③`_handle_chat` 不再重复写用户消息；④`get_messages` 改走 `run_blocking`；⑤system prompt 注入【当前时间】 |
| `tests/test_conversation_stream.py` | **新增**：7 个用例（见下） |
| `tests/test_handler_dispatch.py` | 断言更新为"命令+提问+回复=4 条且 kind 顺序正确"；清理补 `.jsonl` 与 `.bak-*` |
| `tests/test_async_nonblocking.py`、`tests/test_cloud_config.py` | 断言/清理从 `data/memory` 改到 `data/stream` |

**行为变化（需要知道的）**

1. **对话流是唯一事实来源**：`data/memory/*.json` 变成只读的历史遗留，启动时迁移后改名 `.migrated`；新数据全在 `data/stream/<群ID>.jsonl`。
2. **bot 自己的每句话都入流**（含破冰、插嘴、看图回复）——这是"能接着自己的话说"的前提。
3. **窗口是最近 `MAX_HISTORY × 2` 条**（语义与老实现一致，`.env` 里是 10 → 20 条），但**内容不再只有 @bot 的轮次**，而是群里所有消息。
4. **时间标记稀疏**：间隔 < 1 小时的连续消息不打标记；1~24 小时打 `[N小时前]`；≥24 小时或窗口第一条（已过去 1 小时以上）打 `[MM-DD HH:MM]`；system prompt 里另有【当前时间】锚点。

**命令与结果**

```
python -m compileall -q app tests          → exit 0
python tests/test_conversation_stream.py   → 7/7 PASS
python tests/test_phase1.py                → PASS
python tests/test_async_nonblocking.py     → PASS
python tests/test_handler_dispatch.py      → PASS
python tests/test_cloud_config.py          → PASS
python tests/test_llm_payload.py           → PASS
```

**偏差与处理（都记录下来）**

| # | 现象 | 处理 |
|---|---|---|
| 1 | `test_handler_dispatch` 断言 `memory.size()==2` 失败（实得 4） | 属**断言过时**：现在命令也入流。已把断言改成校验 kind 序列 `["command","command","chat","reply"]`，比原来更有信息量 |
| 2 | `test_cloud_config` 断言 `data/memory/<gid>.json` 存在失败 | 同上，改断言 `data/stream/<gid>.jsonl` |
| 3 | 我最初把时间标记的期望写错（以为小间隔之外都不标） | 属**测试期望写错**，代码行为符合设计（大间隔打锚点是有意的）。已修正断言并补"小间隔不打标"用例 |
| 4 | 测试跑完残留 `data/stream/100000002.jsonl.bak-<ts>` | `/clear` 会留备份，测试清理漏了；已补 `.bak-*` 清理并手工清掉残留。**这正是"验证"环节该抓的东西** |
| 5 | 迁移若放在 `GroupMemory.__init__`，测试构造实例就会动生产数据 | 改为 main 启动期显式迁移 + `stream.window()` 懒迁移兜底；测试可控 |

**已知遗留（交给后续子项）**

- 面板 `memory.size()` 首次统计会读一遍对话流文件（之后走缓存）→ **P0-4 用 metrics 计数彻底解决**（每次 append 时在 worker 线程里累加）。
- 图片校验不做像素级尺寸解析（Q1 决策的代价），极端大图失败时会被记为"没看清"。

#### 6.3 P0-3 视觉管道（**已完成**，2026-10-07）

**改动文件**

| 文件 | 改动 |
|---|---|
| `app/vision/pipeline.py` | **新增**：下载（逐张隔离）→ 魔数校验 → MD5 拼键查缓存 → ≤4MB 转 base64 内联 / 超限退回 URL → 调模型 → 写缓存；失败返回 `reason`（`all_failed` / `empty_response` / `llm_error` / `no_image` / `llm_missing`） |
| `app/vision/cache.py` | **新增**：一图一文件的描述缓存（键 = 图片 MD5 + 提示词版本），默认 30 天 TTL，带 `purge_expired()` 供 P3 的 cleanup 调用 |
| `app/vision/__init__.py` | 门面导出 |
| `app/config.py` | 新增 `VISION_DOWNLOAD_TIMEOUT` / `VISION_INLINE_MAX_MB` / `VISION_INLINE_TOTAL_MB` / `VISION_CACHE_DAYS` |
| `app/bot/handler.py` | 两处看图调用改走管道（`/看` 与 @bot 看图）；新增 `_record_image_description()` 把描述写进对话流 |
| `.env.example` | 补上 4 个视觉新配置 |
| `tests/test_vision_pipeline.py` | **新增**：8 个用例 |

**行为变化**

- 以前"URL 丢给模型 → 空描述 → bot 瞎聊"；现在**先下载再内联**，模型不再需要去抓图，
  顺带把"局域网 URL / 临时签名 URL 抓不到"这一类失败从根上消掉；
- 同一张图第二次发**不请求模型**（缓存），这是最直接的省钱点；
- 失败原因可查（日志里有 `reason` 与每张图的跳过原因），handler 走"没看清"话术，**不猜内容**；
- 识别成功的描述以 `[图片识别结果] …` 入流，之后能引用"刚才那张图"。

**测试证据**：`tests/test_vision_pipeline.py` 8/8 PASS（含"大图退回 URL""坏图跳过""空结果不写缓存"）。

#### 6.4 P0-4 指标与面板健康卡（**已完成**，2026-10-07）

**改动文件**

| 文件 | 改动 |
|---|---|
| `app/metrics.py` | **新增**：每群消息数 / bot 发言数 / **被回应率** / LLM 调用与失败率 / 平均延迟 / token / 视觉命中率；按天保留 7 天；原子落盘 |
| `app/context/stream.py` | `append()` 里埋点 `metrics.record_message()`（运行在单 worker 线程，允许批量落盘） |
| `app/llm/provider.py` | `chat()` 成功/超时/HTTP 错误/非 200 四处埋点 `_record_llm()` |
| `app/vision/pipeline.py` | 四处结局埋点 `_record_vision()`（命中/全失败/空结果/成功） |
| `app/dashboard/server.py` | 新增 `GET /api/health`；抽出 `build_app()` 使面板**可被测试**（不必真监听 8080） |
| `app/dashboard/index.html` | 新增健康卡（今日概览 / LLM·视觉健康度 / 每群被回应率），15 秒刷新 |
| `tests/test_metrics.py` | **新增**：8 个用例 |

**口径（写死并测试过）**

- **被回应率** = bot 发言后 **10 分钟内**有人说话；**同一句话只记一次**（否则多人群聊会把比率刷高）。
- `record_llm` / `record_vision` 由事件循环调用 → **只改内存、绝不落盘**（有专门测试守着这条约定，
  防止以后有人手滑把磁盘 I/O 又带回事件循环）。

**测试证据**：`tests/test_metrics.py` 8/8 PASS；`tests/test_llm_payload.py` 增加 `test_provider_records_metrics`
证明真实 provider 的埋点闭环（干跑用桩模型不经过 provider，必须在别处证明）。

**环境问题（已处理）**：本地 `qqbot` 环境**没装 aiohttp**（`requirements.txt` 里明明有，`main.py` 也有
"未安装就跳过面板"的兜底）。为了能本地验证 `/api/health`，已在该环境安装 `aiohttp>=3.10.0` 对齐依赖。
**生产（云端）本来就装了，无影响。**

启动时会把 `data/memory/<群ID>.json` 迁进 `data/stream/<群ID>.jsonl`，并把旧文件改名为
`<群ID>.json.migrated`。如果迁移后发现问题，**回滚步骤**：

```bash
systemctl stop qqbot                       # 或本地 Ctrl+C
cd /root/qqbot/data                         # 本地则是项目根/data
mkdir -p _stream_backup && mv stream/*.jsonl _stream_backup/   # 挪走新对话流
cd memory && for f in *.json.migrated; do mv "$f" "${f%.migrated}"; done   # 旧文件改回来
systemctl start qqbot
```

要点：**旧文件不会被覆盖或删除**，只是改名；对话流是新增文件，删掉即回到旧行为。
（被 `/clear` 清过的群，对话流会留 `.bak-<时间戳>` 备份，同样可复原。）

#### 6.5 干跑验证与回归（**已完成**，2026-10-07）

新增 `tests/test_p0_e2e.py`，把 P0 的四件事串起来跑一遍（全桩件、不联网、不发真实群消息）：

| 检查点 | 结果 |
|---|---|
| ① 普通群聊入流（kind=chat）且不误触主动插嘴 | ✅ |
| ② @bot 一问一答：用户消息 + bot 回复都在流里（kind=reply） | ✅ |
| ③ 看图：描述入流（kind=vision）+ 回复正常发出 | ✅ |
| ④ 同一张图第二次发 → 命中缓存（视觉模型调用次数仍为 1） | ✅ |
| ⑤ 换一张新图 → 正常发起新请求 | ✅ |
| ⑥ 命令入流（kind=command） | ✅ |
| ⑦ 指标：群消息 9 / bot 4 / 看图 3（命中 1） | ✅ |
| ⑧ 健康卡数据（`build_health_payload`）：结构、体积、每群指标 | ✅ |
| ⑨ HTTP 层（真 aiohttp） | ⏭ 本地不可用 → 改用**测试替身**验证接线（路由已注册 / 401 vs 200 / payload 透传）✅；真实验证留服务器 curl |
| ⑩ 重新读取同一份对话流（模拟重启）窗口与 messages 正确 | ✅ |
| ⑪ **看图失败**：提示词明确要求"如实说没看清、不许猜内容"，且未写入假描述 | ✅ |

**全量回归**：9 个测试文件（`test_phase1` / `test_async_nonblocking` / `test_handler_dispatch` /
`test_cloud_config` / `test_llm_payload` / `test_conversation_stream` / `test_vision_pipeline` /
`test_metrics` / `test_p0_e2e`）**连跑两遍全部 exit=0**；`compileall` 无错；
数据目录审计无测试残留（`data/stream`、`data/stats` 均为空）。

**本轮验证抓出的三个新问题（都已修）**

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 6 | `test_p0_e2e` 第①步**偶发失败**：普通群聊冒出一条 `kind=proactive` 的 bot 发言 | 我把 `CHAT_REPLY_CHANCE=0` 设在第一条消息之后，第一条走了 15% 概率的主动插嘴 | 移到任何消息之前；断言改成"不该有 assistant 条目"。**顺带确认了 P0-1 生效**：bot 的主动发言确实进流 |
| 7 | 本地 `qqbot` 环境**装不了 aiohttp** | `D:\anaconda3\envs\qqbot` 在会话沙箱下不可写（`PermissionError`），直连 PyPI 也卡住 | **不绕路重试**：把健康卡的数据组装抽成 `app/dashboard/health.py`（不依赖 aiohttp），本地即可验证；HTTP 路由与鉴权留给服务器 curl 验证 |
| 9 | 试过 `pip install --target <工作区内目录>` 仍失败 | pip 在**工作区临时目录**里也拿到 `Errno 13 Permission denied`（连它自己下载的 `.whl` 都打不开），并留下 10 个**删不掉**的空目录 `.tmp_pip/pip-*` | 确认这是本会话的**硬边界**，停止尝试。已在 `test_p0_e2e.py` 用 **aiohttp 测试替身**验证"我的接线"（路由注册 + token 鉴权 + payload 透传），并在输出里明确标注"替身不验证 aiohttp 本身，服务器仍需 curl 复核" |
| 8 | 测试把生产统计写脏（`data/stats/summary.json`）+ 残留 `data/group_style/<测试群>.json` | 对话流 `append` 会记指标，而部分测试没重定向 metrics 单例 | 四个测试统一把 metrics 单例指向临时文件并在 finally 清理；已删除被写脏的 `summary.json` |
| 10 | `.tmp_pip/` 下 10 个空目录**我删不掉**（`Access is denied`，`Remove-Item` 与 `cmd rmdir` 都失败） | pip 创建的目录带受限 ACL / 句柄未释放，超出本会话权限 | 记录在此：**需要你在自己的终端里执行一次** `Remove-Item -Recurse -Force .tmp_pip`（纯空目录，无数据） |

**待服务器验证（需要你配合，1 条命令）**

> 本地验证到什么程度：**接线层已用测试替身验证**（路由注册、401/200、payload 透传）；
> 未验证的是 aiohttp 真实行为与浏览器里的健康卡渲染。下面这条命令能一次补齐。

```bash
# 部署并重启后（NapCat 不用动）：
curl -s "http://127.0.0.1:8080/api/health?token=$DASHBOARD_TOKEN" | head -c 400
# 期望：JSON 里有 metrics / streams / stream_total_bytes；错误 token 返回 401
# 再看一眼浏览器：面板顶部应出现三张卡（今日概览 / LLM·视觉健康度 / 每群被回应率）
```

**本地缺 aiohttp 的两种解法**（任选）：① 你在本机 `pip install aiohttp` 后重跑 `test_p0_e2e.py`，
第⑨步会自动从"跳过"变成真实 HTTP 校验；② 不管它，到服务器上按上面 curl 验证。

> 跑测试的小坑：测试会往 stderr 打日志（`[Vision] …`），在 PowerShell 里
> `& python tests/xxx.py 2>&1 | Select-Object` 会被当成错误记录，**pwsh 自身返回 1**，
> 看起来像测试失败。判断真实结果请用 `& python tests/xxx.py *> $null; $LASTEXITCODE`
> （python 的退出码才是准的）。

### 7. 复盘

**做得顺的地方**

- "先写记录再动工"确实有用：Q1（不引入 Pillow）这条决策直接决定了视觉管道的形态
  （不压缩 → 超限退回 URL），如果边写边猜，很可能先引入依赖再回退。
- 端到端干跑（`test_p0_e2e.py`）价值最高：它抓出了偶发断言、测试污染、以及"桩模型不经过 provider 埋点"
  这类只有串起来跑才会暴露的问题。单测全绿并不等于链路正确。

**踩过的坑（写下来避免重犯）**

1. **测试会动生产数据**：对话流 append 会记指标、handler 会喂风格画像 —— 任何"构造真实
   `MessageHandler` / `GroupMemory` / `ConversationStream`"的测试，都必须把**数据目录与 metrics 单例**
   都指向临时位置，并在 finally 清理。本轮修了三处。
2. **概率型逻辑必须先在测试里关掉**（`CHAT_REPLY_CHANCE=0` 要设在任何消息之前），否则断言偶发失败。
3. **断言要断言"设计意图"而不是"当前巧合"**：`memory.size()==2` 在 P0-1 之后变成 4 是对的，
   改成校验 `kind` 序列后更有信息量。
4. 环境差异（本地缺 aiohttp）不要用"绕路重试"解决，优先**把逻辑抽到不依赖该环境的地方**（`health.py`）。

**遗留（交给 P1/P2）**

- ~~HTTP 层（`/api/health`）与面板健康卡的真实渲染，需要在服务器上确认一次（本地缺 aiohttp）。~~
  → ✅ **已闭环（2026-10-07 深夜）**：服务器 `curl /api/health` 返回 200/401 正确、数据结构完整；
  面板三张卡（今日概览 / LLM·视觉健康度 / 每群被回应率）用户目视确认正常。本地缺 aiohttp 的限制
  至此不再影响验收。
- 对话流的 30 天保留期（D18）目前只是设计，清理逻辑要在 P3 的 `cleanup` 任务里落地；
  在此之前对话流会一直增长（只有几条/天时无所谓）。
- `metrics` 的"被回应率"只看"10 分钟内是否有人说话"，不区分"接的是不是这个话题"——P2 的话题
  效果统计会做得更细。

---

## [运维-0] 本地 ↔ 云端同步规范（**已产出**，2026-10-07）

- 关联：用户要求"每次都要能同步到云端"→ 固化为制度（工作约定第 6 条）
- 状态：**已完成**

### 范围

1. 产出 [DEPLOY_SYNC.md](DEPLOY_SYNC.md)：三类文件分类（代码同步 / 服务器独占 / 本地垃圾）、
   逐项同步清单、6 步发布流程（自检 → dry-run 预览 → 服务器备份 → rsync → 重启看日志 → 验证回填）、
   数据结构变更与回滚、验证清单、常见坑、发布记录表。
2. 校正 [DEPLOY.md](DEPLOY.md)：数据路径表补上 `stream/`、`cache/vision/`、`stats/` 与 `.migrated` 留底；
   日志关键词表把过时的 `[Memory] 加载群…` 换成 P0 实际会打的行；顶部加双文档指引。
3. 加固 [.gitignore](.gitignore)：补 `data/`、`/_*.py`、`/_*.bat`、`*.log`、`.tmp_pip/`，
   这样即使将来 `git init`，群聊数据与含明文 token 的排查脚本也不会入库。

### 结论（回答"源码要不要为云端改"）

**不需要改代码**。路径（`app/paths.py` 基于 `__file__` 的绝对路径）、时区（`Config.TIMEZONE`）、
面板监听地址（默认 `127.0.0.1`）、新数据目录（`stream`/`cache/vision`/`stats` 自动创建）都已经是环境无关的。
同步过程只有三件事：**别覆盖 `.env` 与 `data/`**、**首次发布跑一次旧记忆迁移**、**每次跑验证清单**。

### 遗留

- 发布记录表（DEPLOY_SYNC.md §8）需要你每次发布后回填一行；首次发布的验证结果还没填。
- 若你希望把 6 步流程做成脚本（Windows 侧 `deploy.ps1`，需要本机有 rsync/WSL），说一声我加。

---

## [P0-hotfix-1] 线上日志刷 `Logging error`（**已修复，待重新发布**）

- 日期：2026-10-07
- 状态：**开发+测试已完成；等重新同步到服务器验证**
- 关联：P0-2（日志时间戳按 `Config.TIMEZONE`）、[DEPLOY_SYNC.md](DEPLOY_SYNC.md) §4

### 1. 现象（服务器 journalctl）

```
--- Logging error ---
TypeError: setup_logging.<locals>.<lambda>() takes 1 positional argument but 2 were given
  File "/usr/lib/python3.10/logging/__init__.py", line 615, in formatTime
    ct = self.converter(record.created)
```

每打一条日志就刷一段这种栈（bot 本身还在跑，WebSocket 也连上了，但日志被污染、时间戳全部丢失）。

### 2. 根因

我把 lambda 直接赋给了**类属性** `logging.Formatter.converter`：

```python
logging.Formatter.converter = lambda ts: clock.from_timestamp(ts).timetuple()   # ❌
```

Python 里**普通函数挂在类上会触发描述符绑定**，于是 `self.converter(record.created)`
实际以 `(self, ts)` 两个参数调用 → `TypeError`。

补充一个容易忽略的细节：logging 默认的 `converter = time.localtime` 是**内置函数**，
内置函数不参与绑定，所以默认值一直是安全的——只有换成 Python 函数/lambda 才会踩这个坑。

### 3. 修复

```python
def _log_time_converter(created: float):
    return clock.from_timestamp(created).timetuple()

logging.Formatter.converter = staticmethod(_log_time_converter)   # ✅ 用 staticmethod 包一层
```

（`app/main.py` 里同时留了注释说明"为什么必须 staticmethod"，避免以后又被改回去。）

### 4. 为什么本地测试没抓到（**这才是要记的教训**）

- 测试里的 logging 格式串是 `"%(levelname)s %(message)s"`，**不含 `%(asctime)s`**；
  `formatTime()` 只在格式化 asctime 时才被调用 → 这条路径**从未被执行过**。
- `setup_logging()` 只在真正跑 bot 时调用；本地我只跑了单测 + `compileall` + `import app.main`，
  而 import 不执行 `setup_logging()`。

**结论：只测"函数对不对"不够，还要测"配置装上去之后真的打一条日志"。**

### 5. 回归测试（新增 `tests/test_logging_setup.py`，4 项）

| 用例 | 作用 |
|---|---|
| `test_converter_is_staticmethod` | 结构断言（查 `__dict__` 里的原始属性，因为 staticmethod 经类访问会解包） |
| `test_asctime_actually_formats` | **真打一条带 asctime 的日志**：格式化成功、时间戳等于本时区当前时间、stderr 无 `Logging error` |
| `test_negative_control_plain_function_breaks` | **对照组**：故意换回普通函数，必须复现 `--- Logging error ---`，证明测试真的抓得住 |
| `test_setup_logging_is_idempotent` | 重复调用不叠加 handler、仍能正常格式化 |

### 6. 顺带发现（重要）

- **服务器 Python 是 3.10**（`/usr/lib/python3.10/...`），不再是文档里写的 3.11+。
  已做一轮 3.11+ 专属 API 排查（`asyncio.timeout` / `TaskGroup` / `except*` / `StrEnum` /
  `typing.Self` / `tomllib` / `datetime.UTC` / `itertools.batched` …）：**未发现使用** ✅。
- 面板 token 早期是弱口令（11 位、可猜，已于 2026-10-07 轮换为随机串），且**会打进启动日志**。当前面板只监听 `127.0.0.1`，
  风险可控；建议后续换成随机串，并考虑把日志里的 token 打码（可作为 P1 的小项）。

### 7. 待你做

按 [DEPLOY_SYNC.md](DEPLOY_SYNC.md) 第 3 节发布这次热修（只需同步 `app/main.py` 与 `tests/`），
重启后确认日志**不再出现 `--- Logging error ---`**、时间戳按 `Asia/Shanghai` 正常显示。

### 8. 服务器验证结果（✅ 已通过，2026-10-07 21:01）

用户贴回的 `journalctl -u qqbot -n 60` 证据：

```
2026-10-07 21:01:40 [INFO] Bot - 🤖 QQ群 AI 聊天机器人 MVP 启动中...
2026-10-07 21:01:40 [INFO] Bot -    timezone   : Asia/Shanghai (UTC+8)（服务器本地时间 21:01）
2026-10-07 21:01:40 [INFO] Bot -    python     : 3.10.12 / Linux
2026-10-07 21:01:40 [INFO] root - [Init] 表情字典已预热: 194 条
2026-10-07 21:01:40 [INFO] root - [Init] 旧记忆迁移：0 个群（其余已迁移或无旧数据）
2026-10-07 21:01:41 [INFO] app.bot.onebot - [OneBot] ✅ WebSocket 已连接
```

**结论**：

- ✅ 时间戳正常、**不再有 `Logging error`** → 热修生效；
- ✅ 启动自检行显示 `data: /root/qqbot/data`、`timezone: Asia/Shanghai`、`python 3.10.12`，
  与本地设计一致（服务器本地时间本来就是 UTC+8，说明时区这条在服务器上是"一致"而非"纠正"）；
- ✅ P0 的三条启动日志都在：`[Init] 表情字典已预热: 194 条`、`[Init] 旧记忆迁移`、`[StateRestore] 回填 3 个群`；
- ✅ 20:59 那条日志里能看到它**已经在测试群 100000005 被动回复**（`[OneBot] → 群 100000005`），
  符合 D15（定时/主动功能只在实例群，被动响应不受限）。

**由这次日志新发现的两件事（都不是 bug，但要知道）**

| # | 现象 | 判断 | 处理建议 |
|---|---|---|---|
| 11 | systemd 报 `Ignoring invalid environment assignment '#=== OneBot / NapCat 配置 ===': /root/qqbot/.env`，**只有第一条注释行**被报 | 大概率是 `.env` **开头有 UTF-8 BOM**（第一个字符不是 `#`，所以没被当注释跳过；后面的注释行都正常忽略了）。纯提示，不影响运行：Python 侧由 `python-dotenv` 自己读 `.env`，配置值全部正确加载 | 诊断：`file /root/qqbot/.env`、`hexdump -C /root/qqbot/.env \| head -1`；若确认 BOM/CRLF：`sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' /root/qqbot/.env` 后 restart |
| 12 | `[Init] Search 已加载: SearXNG →` 后面是空的 | 服务器上 **`SEARXNG_URL` 没配**，搜索会走代码里的公共实例列表兜底 | 能用就不用管；若 `/搜` 经常失败，考虑 `.env` 里填一个自建/稳定实例地址 |

---

## [P0-hotfix-2] 主动打扰范围落地（D15）+ 两处指标修正（**已开发+测试，待发布**）

- 日期：2026-10-07
- 状态：**开发+测试完成；需要你在服务器 `.env` 加一行后发布**
- 关联：ROADMAP 决策 D15（定时/主动功能只在实例群）、D18

### 1. 为什么要做（线上日志暴露）

用户贴回的日志里出现：

```
[OneBot] 群 100000006 成员缓存: 0 人
```

这行来自 `_idle_watcher` 的**群规模检查** —— 说明**破冰 watcher 正在遍历所有群**。
也就是说：用户要求的"定时/主动功能只在实例群 100000004"**当时并没有在代码里落地**（原计划放到 P2a 调度器）。
测试群 100000005 与放置群 100000006 都有被破冰/插嘴打扰的风险。

### 2. 改动

| 文件 | 改动 |
|---|---|
| `app/config.py` | 新增 `PROACTIVE_GROUPS`（逗号分隔群号，支持中文逗号；留空 = 不限制，兼容旧行为） |
| `app/bot/handler.py` | 新增 `_proactive_allowed(group_id)`；在三处主动路径前拦截：`_idle_watcher`（破冰）、`_maybe_casual_reply`（插嘴）、`_handle_look_only`（裸图自动回复） |
| `app/main.py` | 启动时打印主动打扰范围；未配置时打 **WARNING**（避免"以为只在实例群，其实全群都在打扰"） |
| `app/dashboard/health.py` | **指标修正**：只有对话流、没有指标的群（迁移来的历史）也会出现在每群卡片里（带 `imported_entries`） |
| `app/llm/provider.py` | **指标修正**：`_post_vision` 每次真实 HTTP 尝试都记一次 `llm.*` —— 看图也花 token，不记会让面板的调用数/延迟/token 偏小 |
| `.env.example` | 新增 `PROACTIVE_GROUPS=100000004` 及说明 |
| `tests/test_proactive_scope.py` | **新增 6 项**：名单内插嘴正常 / 名单外插嘴被拦 / 名单外裸图被拦但 `/看` 正常 / 破冰只落在实例群 / 名单为空退回旧行为 / 被动响应不受限 |

**边界说明（写清楚免得误会）**：

- **被限制**的只有"主动消息"：破冰、主动插嘴、没 @ 也发图的自动回复；
- **不受限制**的被动响应：`@bot`、`/问`、`/搜`、`/看`、所有命令 —— 任何群都能用（这也是为什么测试群依旧能用来验证）。

### 3. 服务器侧操作（需要你做）

在 `/root/qqbot/.env` 里加一行（这是"服务器侧操作"，不随代码同步）：

```bash
echo 'PROACTIVE_GROUPS=100000004' >> /root/qqbot/.env
```

启动日志里应出现：`[Init] 主动打扰范围: 100000004（其余群只做被动响应：@bot / /问 / /看 / 命令）`。
若没配，会看到 WARNING 提示所有群都会被主动打扰。

### 4. 测试证据

```
tests/test_proactive_scope.py → 6/6 PASS
（其中破冰一项断言：called 列表里只有 100000004，测试群与放置群从未出现）
全量回归 11/11 通过；数据目录审计干净
```

### 5. 服务器验证结果（✅ 2026-10-07 21:19）

5 个文件按「替换清单」上传，自检命令全部命中期望值：

```
app/config.py : PROACTIVE_GROUPS = 2 (期望 2)
app/bot/handler.py : _proactive_allowed = 4 (期望 4)
app/main.py : 主动打扰范围 = 1 (期望 1)
app/dashboard/health.py : imported_entries = 1 (期望 1)
app/llm/provider.py : _record_llm = 9 (期望 9)
```

```
[Init] 主动打扰范围: 100000004（其余群只做被动响应：@bot / /问 / /看 / 命令）
[Init] 旧记忆迁移：0 个群
```

`/api/health`：三群对话流齐全 `100000004:23 / 100000006:20 / 100000005:27`（总 21043 B），
每群卡片也是三群都在（`user_msgs=0` 是因为进程刚重启、还没统计到新消息；
`stream_entries` 是对话流总条数）。**健康卡修复生效**。

**一个必须记住的行为变化**：用户在测试群发了两次裸图，日志是

```
[Vision-only] group=100000005 不在主动打扰名单，跳过
```

→ **这是 D15 的预期行为**（裸图自动回复算主动消息，测试群不在名单里）。
副作用：**测试群不再能验证"裸图自动回复"这条路**，要测看图请在测试群用 `/看` + 图（被动响应，不受限制），
或把该群临时加进 `PROACTIVE_GROUPS`。已把这条日志加进 [DEPLOY.md](DEPLOY.md) 的关键词表，避免以后误判成故障。

### 6. 顺带修正（已改代码，随下次发布）

`build_health_payload` 里给"只有对话流、没有指标的群"补的字段名原为 `imported_entries`，
但它取的是**对话流总条数**（迁移之后还会继续追加消息，无法区分），语义不准 → 已改名为 `stream_entries`
（面板未使用该字段，改名对 UI 无影响，测试同步更新）。

### 7. 后续补充（2026-10-07 22:4x，随下次发布）

**启动横幅升级为「配置自查面板」**（`app/main.py` 抽出 `log_startup_banner()`）：
把 `Thinking`（思考模式 + 是否影响 temperature）、`Chat`（概率/冷却/关键词）、`Vision`（思考开关/内联上限/缓存天数）、
`Proactive`（主动打扰范围）、`Dashboard`（token）都打出来。

**动机**：当晚为了确认 `DASHBOARD_TOKEN` 是否生效，来回折腾了两轮（因为旧横幅不打印它，
只能靠 `[Dashboard] 已启动` 那行里间接看）；用户贴 `.env` 核对配置又导致真实 key 泄露一次。
以后看一行日志就能自查，不需要贴任何配置。新增回归测试 `test_startup_banner_reports_key_settings`。

**下次发布清单（本地已就绪，等 P1 一起给）**：

| 文件 | 内容 | 自检 |
|---|---|---|
| `app/dashboard/health.py` | `imported_entries` → `stream_entries` | `grep -c stream_entries app/dashboard/health.py` → 2 |
| `app/main.py` | 启动横幅自查面板 | `grep -c "Thinking   :" app/main.py` → 1 |
| `tests/test_p0_e2e.py` / `tests/test_logging_setup.py` | 对应测试（可选） | — |

---

## [P1-0] P1 阶段规划：记忆分层（**规划中，等 Q1~Q3 答复后动工**）

- 日期：2026-10-07
- 状态：**规划中**
- 关联：ROADMAP 第 9 节 P1、D14/D18/D19、`[P0-hotfix-2]` §7 的待发布清单

### 1. 范围

| 编号 | 内容 | 解决什么 |
|---|---|---|
| P1-1 | **滚动摘要**：窗口滑出的对话压成 ≤5 条要点，长期保存 | 20 条窗口一滑，早先说过的事就没了 |
| P1-2 | **用户画像卡**：每人一句（称呼/喜好/最近提过的事） | bot 记不住"谁是谁、谁喜欢什么" |
| P1-3 | **话题状态**：当前话题 / 谁提起 / 何时 / 是否还热 | 破冰与插嘴现在是无状态的，接不上自己的话 |
| P1-4 | **上下文预算组装器**：显式分配字符预算，超了就压最老的 | 现在人设+画像+称呼+窗口无限叠加，没有预算概念 |
| P1-5 | **人设拆分**：`persona.txt` 原文不动，新增常驻核心 + 场景模板 | 557 行常驻吃掉大量预算（实测 `tokens_in≈4400`） |

**明确不做**：不做检索式记忆（P3）、不做话题效果统计（P2c）、不做调度器（P2a）、不改 `persona.txt` 原文。

### 2. 设计要点（初稿）

**数据文件**（都按群隔离、原子写、走 `run_blocking`）

| 文件 | 内容 |
|---|---|
| `data/summary/<群ID>.json` | `{"version":1, "bullets":[…≤5 条要点…], "archived":[…更早要点…], "summarized_upto_ts":…, "people":{qq:{nick,note,last_seen,msgs}}, "updated_at":…}` |
| `data/state/<群ID>.json` | `{"version":1, "topic":{"title","opened_by","opened_at","last_activity","status"}}`（P2a 的调度状态也放这个文件，避免以后再迁一次） |

**新增模块**

- `app/context/summary.py` —— `ConversationSummary`：`should_run()`（触发判定）、`run(llm)`（一次 LLM 调用产出【要点】+【成员】两段，容错解析 + 失败保留旧值）、`bullets_text()` / `people_text()`（给 prompt 用）
- `app/context/topic.py` —— `TopicState`：`observe(...)`（记录/更新）、`open_topic(...)`（主动发言时写入）、`prompt_text()`
- `app/context/assembler.py` —— `ContextAssembler`：把"人设核心 + 场景模板 + 长期要点 + 画像卡 + 群风格 + 称呼表 + 即时窗口"按预算拼成最终 prompt/messages，并返回**各段占用统计**
- `app/persona.py` —— `load_core()` / `load_scene(name)`：读 `config/persona_core.txt` 与 `config/scenes/*.txt`；**缺失时回退到原 `persona.txt`**（保证任何情况都不会没有人设）

**触发方式**：每收一条群消息后检查 `should_run()`，满足则 `asyncio.create_task(...)` 后台跑（与现有风格画像同一套路），**不阻塞回复**。

**预算口径**：以**字符**为单位（没有 tokenizer，不假装精确），`CONTEXT_MAX_CHARS` 默认 6000；
固定段（人设核心/场景/要点/画像/风格/称呼）优先，剩余给即时窗口；窗口**保底 N 条**（默认 6），
不足则按"从最老开始丢"的顺序压缩。每次组装打一行日志：

```
[Context] group=100000004 人设=420字 要点=310字 画像=240字 风格=150字 称呼=90字 窗口=18条/2140字 合计=3350/6000
```

**验收关键**（对齐 ROADMAP）：连续聊 50 轮后仍能回答"一开始你说的那个"；**人设占用预算可见**。

### 3. 待确认问题（Q1~Q3，✅ 已答复 2026-10-07）

| 编号 | 问题 | 用户答复 | 落到实现上的约束 |
|---|---|---|---|
| Q1 | 窗口多大 | ✅ **让预算自动决定**：`MAX_HISTORY` 只当上限，实际条数由组装器按字符预算动态定，保底 6 条 | 组装器必须能"按预算裁剪窗口"，且保底条数不可被裁掉 |
| Q2 | 摘要触发阈值 | ✅ **40 条 / 10 分钟** | `SUMMARY_MIN_MESSAGES=40`、`SUMMARY_COOLDOWN_MINUTES=10` |
| Q3 | 群友能否要求删除自己的记录 | ✅ **留到 P3**（与 TTL/隐私留存一起做） | P1 不做 `/forget`；但数据结构里要预留"按 QQ 可定位"（画像卡本来就按 QQ 存，已满足） |

### 4. 测试计划

| 层级 | 内容 | 判定 |
|---|---|---|
| 单测 | 摘要解析（含畸形/半截输出）、要点与画像的合并与上限、触发条件（条数/冷却）、预算组装（不超预算、保底条数、超长单条截断）、人设加载回退、场景占位符替换 | 新增用例全绿 |
| 干跑 | 造 60 条消息 → 触发摘要（桩 LLM）→ 断言要点/画像落盘 + 被注入 prompt + 预算日志格式；模拟"50 轮之后"检查 prompt 里仍带早期要点 | `test_p1_e2e.py` 通过 |
| 真实验证 | 实例群观察一次摘要日志（`[Summary]`）与面板/文件；测试群用 `/看` 之外的方式验证不受影响 | 用户贴日志 |

### 5. 验收标准

1. 群安静后重启，**早先的要点仍在**（`data/summary/<群ID>.json`）；
2. 摘要与画像**只在后台跑**，不拖慢回复（回复延迟无显著变化）；
3. 组装器日志能看到各段占用；人设占比从 ~40% 降到 ~10%；
4. 注入内容不含未转义占位符、不超预算；
5. 人设核心缺失时**自动回退**到原 `persona.txt`（不会变成没人设）；
6. 全量单测通过（现有 11 个文件 + 新增）。

### 6. 实施记录

#### 6.1 P1-5 人设拆分（**已完成**，2026-10-07）

**为什么先做这个**：它自包含、可独立验证，而且是"预算"的前提——原 557 行整份常驻，
实测 `tokens_in≈4400`，人设是上下文成本的大头；不先瘦身，后面的摘要/画像挤进来也没空间。

**改动文件**

| 文件 | 改动 |
|---|---|
| `app/paths.py` | 新增 `SUMMARY_DIR`、`STATE_DIR`（P1 数据目录）与 `PERSONA_CORE_PATH`、`SCENES_DIR` |
| `app/persona.py` | **新增**：`load_core()` / `load_scene(name, **kw)` / `available_scenes()` / `describe()` / `reset_cache()`；只替换白名单占位符，未知 `{xxx}` 原样保留 |
| `config/persona_core.txt` | **新增**：常驻核心人设（**467 字**，从 557 行提炼：性格/语气/称呼/长度/底线/禁止） |
| `config/scenes/*.txt` | **新增 5 个场景模板**：`chat` / `icebreak` / `vision_reply` / `image_fail` / `summary` |
| `app/config.py` | `load_persona()` 改为走核心；新增 P1 配置项（`SUMMARY_*` / `PEOPLE_*` / `CONTEXT_*` / `TOPIC_*`） |
| `app/main.py` | 启动横幅新增 `Persona` 行（来源 / 常驻字数 / 场景数）→ 让"人设占用预算"可见 |
| `tests/test_persona_scenes.py` | **新增 7 项** |

**原 `config/persona.txt` 一个字节都没动**——它是素材与回退目标。想恢复旧行为：删掉 `persona_core.txt` 即可。

**测试证据**

```
tests/test_persona_scenes.py → 7/7 PASS
  · 核心优先且显著更短（临时样本压缩到 1%）
  · 核心缺失 → 自动回退完整人设；只有注释 → 也回退
  · 场景渲染：多占位符 / 未知占位符原样保留 / 空花括号保留
  · 场景缺失 → 空串（调用方兜底）；缓存与 reset
  · 真实项目：人设核心 467 字、5 个场景齐全
全量回归 12/12 通过
```

**横幅预览（本地）**

```
   Thinking   : enabled（temperature 不生效，官方限制；想省钱/提速可设 disabled）
   Chat       : 概率 15% / 冷却 60s / 关键词 爱丽丝,AI,游戏,电脑,代码,编程,原神
   Vision     : 思考关 / 内联≤4.0MB / 缓存 30 天
   Proactive  : 未限制（所有群都会被主动打扰）
   Persona    : persona_core.txt / 467 字常驻 / 场景模板 5 个 ['chat', 'icebreak', 'image_fail', 'summary', 'vision_reply']
```

**验证环节又抓到一个我自己制造的污染（已修）**：`tests/test_proactive_scope.py` 用的是**真实群号**
（100000004 等），而 `MessageHandler` 内部构造的 `StyleProfile` 会往 `data/group_style/<群号>.json`
写测试消息 → 审计发现 `group_style` 从 3 个文件变成 4 个（多出 `100000004.json`）。
处理：测试里把 `app.style.profile._DATA_DIR` 指到临时目录 + 删除残留文件；
连跑两遍全量测试后数据目录仍然干净（group_style 3、stream 0、stats 0）。
**教训（写进复盘）**：用"真实 ID 当文档"的测试必须把所有会落盘的模块都隔离，不能只隔离 memory/metrics。

#### 6.2 P1-1 ~ P1-4 记忆分层（**已完成**，2026-10-07）

**新增模块**

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `app/context/summary.py` | 滚动摘要 + 用户画像卡 | 热路径**只加内存计数**；`run()` 一次 LLM 调用产出【要点】【成员】【当前话题】三段（省一半钱）；解析器宽容，**垃圾输出保留旧记忆**；要点 ≤5 条（超出滚进 `archived`）；画像按 QQ 存、上限 `PEOPLE_MAX` |
| `app/context/topic.py` | 当前话题 | 两个写入源：摘要抽出的【当前话题】+ 主动发言；`touch()` 刷新活跃且**落盘节流 5 分钟**；超过 `TOPIC_STALE_MINUTES` 不再注入 |
| `app/context/assembler.py` | 上下文预算 | MUST/HIGH/LOW 三级；超预算先丢"最长的 LOW"，再把 HIGH 截断到预算 40%；窗口从最新往旧塞、保底 `CONTEXT_MIN_ENTRIES` 条；输出各段占用统计 |
| `app/persona.py` | 人设核心 + 场景模板 | 见 6.1；本阶段把 `chat/icebreak/vision_reply/image_fail/summary` 全部接上 |

**数据文件**：`data/summary/<群ID>.json`（要点+画像）、`data/state/<群ID>.json`（话题；P2a 的调度状态也放这里）

**接入点（8 处）**

1. `MessageHandler.__init__` 建 `summary` / `topic` / `assembler`；
2. `_remember_group_msg`：消息入流 → `summary.note_activity()`（纯内存）→ 够 40 条就 `create_task(_summarize_memory)`；再 `topic.touch()`（阻塞调用走 `run_blocking`）；
3. `_summarize_memory`：读大窗口 → `summary.run()` → 抽出的话题落进 `TopicState` → `finally` 必 `release`；
4. `_build_system_prompt` 改为"分段 + 组装器"，并打 `[Context]` 占用日志；
5. `_handle_chat`：先组装 system prompt，**用剩余预算裁对话窗口**，再复用同一个 prompt 调 LLM（不重复组装）；
6. `_icebreak_llm` 改用 `scenes/icebreak.txt`；
7. `_handle_look_only` 改用 `scenes/vision_reply.txt`，且看图场景**不再注入 chat 场景说明**；
8. 看图失败提示改用 `scenes/image_fail.txt`。

**干跑验证（`tests/test_p1_e2e.py`，6 项全过）**

```
① 42 条消息 → 后台摘要触发 1 次；要点 2 条、画像 1 人
② 长期要点与群友画像已注入 system prompt（= 滑出窗口也还记得）
③ 当前话题已记录并注入：【当前话题】显卡和周末开黑（0 分钟前）
④ [Context] group=100000040 人设=8 时间=23 要点=67 画像=28 话题=62 场景=123 窗口=20条/349字 合计=660/6000
⑤ 模拟重启：新实例重新读盘，要点与画像仍在（验收标准 1）
⑥ 预算 300 字（极紧）：LOW 段被丢、人设仍在，回复正常
```

**效果对照（关键数字）**：P0 时代一次调用 `tokens_in≈4400`（人设整份 557 行常驻）；
现在 system prompt 各段合计 **660 字**（本地干跑；服务器上线后看真实 `[Context]` 行），
其中人设只占 8~467 字 —— 验收标准 3「人设占用预算可见」由 `[Context]` 日志直接兑现。

**开发过程中抓到并修掉的 7 个问题**（都是真问题，不是形式主义）

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `ts or time.time()`：传 `ts=0` 被当成"没传" | 伪造"很久以前"的话题测不出来，过期逻辑形同虚设 | 改 `ts is None` 判断 |
| 2 | `_clip()` 把省略号加在硬上限**之外** | 每次稳定超预算 1 字，日志刷"超预算"告警 | 省略号算进 cap |
| 3 | `stats["合计"]` 把段落间的 `\n\n` 也算进去 | 300 字预算明明够却报"超预算" | 合计按各段之和计 |
| 4 | 解析器过于宽容：单行散文也当要点 | 模型胡扯一句就被写进长期记忆 | 只认列表项；单行散文判为解析失败 |
| 5 | 编辑 `_handle_chat` 时 `else` 挂到了 `try` 上 | **会丢掉整个对话窗口**（回复退化成"只看当前一句"） | 恢复 `if/elif/else` 结构，并使 `_sys_res` 显式初始化 |
| 6 | 看图场景被注入了 chat 场景说明 | 提示词自相矛盾（"有人在 @ 你" + 图片描述） | `trigger=look` 时 `scene=""`，e2e 加断言防回归 |
| 7 | 测试又往 `data/group_style/` 写真实文件（第二次） | 污染真实数据目录 | 抽 `tests/helpers_isolate.py` 统一隔离，三个 e2e/干跑都改用它 |

**遗留与后续建议**

- **成本**：群风格画像（30 条触发）与长期记忆摘要（40 条触发）会各调一次 LLM。同一条消息流上
  两次调用，量不大但可以合并成一次 —— 记在 P2 里考虑。
- **窗口上限**：服务器 `MAX_HISTORY=10` → 窗口最多 20 条，而现在预算（6000 字）只用掉 660 字。
  Q1 的答复是"让预算自动决定"，所以建议把 `MAX_HISTORY` 调到 **20**（=40 条），让预算去做裁剪。
  这是**服务器 `.env` 的一行改动**，见发布清单的"服务器侧操作"。
- **话题的主动写入**：破冰/插嘴产出内容时把"自己起的话头"记进 `TopicState`（P1-3 只做了摘要抽取 +
  注入，主动写入留给 P2 的跟进机制一起做，避免半成品）。

#### 6.3 P1 修订（服务器实测后发现的三个问题，2026-10-07 深夜）

**修订-1：人设缺 markdown 禁令**

服务器真实回复里出现了 `🍖 **烤...**` 与"最近群里聊过这些："的清单式汇报，且 `reply_len=297`（人设写的是 20~150 字）。
查证：**原文 557 行人设里从来没有禁止 markdown 的条款**（`**` 那些只是人设文档自己的排版），属于人设缺项。

修法：`config/persona_core.txt` 新增【排版】条款（禁止 `**粗体**`/`#`/`-`/表格，要分点用 1️⃣2️⃣3️⃣ 且最多 2~3 条）；
`config/scenes/chat.txt` 新增"对方问'你还记得吗'时用 1~2 句说要点，不要罗列清单、不要汇报腔"。

**修订-2：摘要触发会「饿死」（真 bug）**

服务器日志显示 `[Context] … 窗口=37条 → 39条`，即对话流里已积压 39 条，但 `[Summary]` 迟迟不出现。
根因：触发计数只统计"**本进程启动后**收到多少条"，重启即归零。后果很实在 ——
**群消息少 + bot 每天重启的群，摘要永远不会跑，记忆分层等于没生效**。

修法：每个群在本进程第一次出现消息时，用一次快速尾读算出"历史积压条数"垫进计数
（`ConversationSummary.prime()`，每群每进程只算一次；失败按 0 处理不影响回复）。
新增回归测试 `test_prime_backlog`（积压 39 + 新 1 条 = 达标；重复 prime 不重复累加）。

**修订-3：自检命令的期望值写错（我的错）**

清单里 `grep -c SUMMARY_ENABLED app/config.py → 2` 是错的：`grep -c` 数的是**行数**，
而那一行里恰好出现 2 次（变量名 + `os.getenv("SUMMARY_ENABLED", …)`），正确期望值是 **1**。
已在 6.4 修正整张表，并把"`grep -c` 数行不数次数"写进 DEPLOY_SYNC §7-17。

**本批要替换的文件（4 个）**

| 服务器路径 | 操作 | 内容 | 自检（行数） |
|---|---|---|---|
| `config/persona_core.txt` | 覆盖（467→591 字） | 禁 markdown | `grep -c markdown config/persona_core.txt` → 1 |
| `config/scenes/chat.txt` | 覆盖 | "还记得吗"场景规则 | `grep -c 汇报腔 config/scenes/chat.txt` → 1 |
| `app/context/summary.py` | 覆盖（15KB） | `prime()` 积压垫入 + `is_primed()` | `grep -c "def prime" app/context/summary.py` → 1 |
| `app/bot/handler.py` | 覆盖（83KB，建议拖拽） | 首次见群时统计积压 | `grep -c is_primed app/bot/handler.py` → 1 |

**改完预期看到的新日志**：`[Summary] 群 100000004 历史积压 39 条未摘要，已计入触发计数`
—— 之后再来 1 条消息就会立刻触发一次摘要（不用再等 40 条）。

**服务器实测（2026-10-07 23:37，✅ 全部生效）**

```
23:37:51 [Summary] 群 100000004 历史积压 46 条未摘要，已计入触发计数      ← prime 修复生效
23:37:51 [Context] group=100000004 人设=517 … 场景=194 窗口=40条/2402字 合计=3356/6000
                                                                          ← 新核心人设 + 新 chat 场景都已替换；窗口打满 MAX_HISTORY 上限
23:37:53 [Chat] elapsed=1.32s history_len=40 reply_len=126                ← 126 字，回到人设要求的 20~150
23:37:54 [Summary] group=100000004 摘要完成：入参 46 条 → 要点 5 条（归档 0）、画像 4 人、话题='无'
```

`话题='无'` 是**预期行为**：模型判定当时只是零散闲聊 → 不写话题状态 → 也就没有 `[Topic]` 行；
等群里有明确话题时才会出现。**P1-1/P1-2 至此在真机闭环**（积压 → 摘要 → 落盘）。

**修订-4：日志噪声与"技术抱怨"（同日深夜，按实测回复追加）**

1. `[DEBUG] segments detail: [...]` 是**每条消息都打**的调试残留（INFO 级）→ 降级为 `logger.debug`
   并加 `isEnabledFor(DEBUG)` 前置判断。**这个改动在 `app/bot/handler.py` 里，随下一批（P2）发布**，
   不单独让用户再传一次 83KB 文件。
2. 真实回复里出现"可是十七七七、发的图爱丽丝这边还是没收到呀"——bot 主动抱怨技术问题，
   且把窗口里一条旧消息的话题续了上来（记忆是生效的，但措辞很糟）。根因是**人设没禁止这类话**。
   已在 `config/persona_core.txt` 新增【另外】段（"没收到图/模块没装"一律别说，除非本轮明确识别失败）；
   核心字数 591 → 718。

#### 6.4 P1 发布清单（P0 遗留 + P1 全部，**已发布并实测通过**）

> 交付格式按工作约定第 7 条：只给"替换哪个文件"，不给备份/rsync 操作。

**必需（9 个）**

> ⚠️ 自检命令一律用 `grep -c`，它数的是**匹配行数**，不是出现次数。
> 例：`app/config.py` 里 `SUMMARY_ENABLED` 只占 1 行（该行里出现 2 次：变量名 + `os.getenv` 字符串），
> 所以期望值是 **1** 而不是 2 —— 第一版清单我按"出现次数"写，导致服务器自检时虚报不一致（已修正）。

| 服务器路径 | 操作 | 自检命令（期望值 = 行数） |
|---|---|---|
| `app/paths.py` | 覆盖 | `grep -c SUMMARY_DIR app/paths.py` → 1 |
| `app/config.py` | 覆盖 | `grep -c SUMMARY_ENABLED app/config.py` → 1 |
| `app/persona.py` | 🆕 新建 | `grep -c "def load_scene" app/persona.py` → 1 |
| `app/context/summary.py` | 🆕 新建 | `grep -c "class ConversationSummary" app/context/summary.py` → 1 |
| `app/context/topic.py` | 🆕 新建 | `grep -c "class TopicState" app/context/topic.py` → 1 |
| `app/context/assembler.py` | 🆕 新建 | `grep -c "class ContextAssembler" app/context/assembler.py` → 1 |
| `app/bot/handler.py` | 覆盖（82KB，建议拖拽覆盖而不是手动粘贴） | `grep -c _collect_sections app/bot/handler.py` → 3 |
| `config/persona_core.txt` | 🆕 新建 | `grep -c 天童爱丽丝 config/persona_core.txt` → 2 |
| `config/scenes/summary.txt` | 🆕 新建 | `grep -c 当前话题 config/scenes/summary.txt` → 1 |

**强烈建议（6 个，都是小文件）**

| 服务器路径 | 操作 | 自检 |
|---|---|---|
| `app/main.py` | 覆盖（启动横幅 = 配置自查面板） | `grep -c "Persona    :" app/main.py` → 2 |
| `app/dashboard/health.py` | 覆盖（字段改名 `stream_entries`） | `grep -c stream_entries app/dashboard/health.py` → 2 |
| `config/scenes/chat.txt` | 🆕 新建 | — |
| `config/scenes/icebreak.txt` | 🆕 新建 | — |
| `config/scenes/vision_reply.txt` | 🆕 新建 | — |
| `config/scenes/image_fail.txt` | 🆕 新建 | — |

**可选**：`tests/**`（`helpers_isolate.py`🆕、`test_persona_scenes.py`🆕、`test_p1_memory.py`🆕、`test_p1_e2e.py`🆕、`test_p0_e2e.py`、`test_logging_setup.py`、`test_proactive_scope.py`）

**服务器侧操作**

```bash
# 1) 让预算真正起作用（现在窗口被 MAX_HISTORY=10 卡在 20 条）
sed -i 's/^MAX_HISTORY=.*/MAX_HISTORY=20/' /root/qqbot/.env
# 2) 重启
systemctl restart qqbot
```

**验收（部署后）**

```bash
# 启动横幅应出现 Persona 行（人设核心已生效）
journalctl -u qqbot -n 40 --no-pager | grep -E "Persona|Thinking|Chat |Proactive"
# 起来后 @ 它聊几句，观察上下文占用（人设/要点/画像/窗口各占多少）
journalctl -u qqbot -f | grep -E "\[Context\]|\[Summary\]|\[Topic\]"
# 攒够 40 条群消息后应出现一次摘要（要点/画像/话题）
grep -c . /root/qqbot/data/summary/*.json 2>/dev/null; ls /root/qqbot/data/summary/ /root/qqbot/data/state/
```

**红线**：不要替换 `/root/qqbot/.env`；不要碰 `/root/qqbot/data/**`（新目录会自动创建）。

---

## [P2a-0] P2a 阶段规划：调度框架（**规划中，等 Q1~Q4 答复后动工**）

- 日期：2026-10-07
- 状态：**规划中**
- 关联：ROADMAP §4（4.1~4.10）、决策 D15 / D18、`[P1-0]` 的遗留项

### 1. 范围

**做**（P2a 全部内容）

| 编号 | 内容 |
|---|---|
| P2a-1 | `scheduler / Task / ContentSource` 三层解耦（`app/schedule/`） |
| P2a-2 | 触发类型：`idle` / `interval` / `cron` / `event`（P2a 先实现前三种，`event` 只留接口） |
| P2a-3 | 任务状态层：每（群 × 任务）记录 上次执行 / 当日次数 / 退避级别 / 内容指纹 |
| P2a-4 | 打扰控制：每任务每日上限 + 每群每日总上限 + 静默时段 + 冷却 + 群规模/活跃前置 + 退避 |
| P2a-5 | **破冰迁入框架**（`_idle_watcher` 拆成 `icebreak` 任务；内容源沿用现有逻辑） |
| P2a-6 | **手动触发 / 干跑**：`/任务 列表丨试跑丨执行` + 面板任务卡片与按钮 |
| P2a-7 | 与现有配置兼容：`单群覆盖 > 环境变量 > 代码默认`（ROADMAP §4.9） |

**不做**（明确划界，避免半成品）
天气播报与喝水提醒（P2b）、RSS/每日一句/每日一图/话题轮播/效果统计（P2c）、
周报与新人欢迎与事件层扩展（P2d）、检索式记忆与 TTL（P3）。

### 2. 设计要点（初稿）

**模块结构**

```
app/schedule/
  __init__.py
  scheduler.py      # Scheduler：单后台协程，30s tick，遍历「任务 × 群」
  task.py           # Task 基类 + TaskContext + TaskResult（内容/发送/记录）
  triggers.py       # IdleTrigger / IntervalTrigger / CronTrigger（按群时区算）
  state.py          # TaskStateStore：data/state/<群ID>.json 的 tasks 段（与 P1 话题同文件）
  registry.py       # 任务注册表（名字 → Task 实例，供命令/面板/调度共用）
  sources.py        # ContentSource 协议 + 现有破冰内容源包装（LLM 话题包 / 热搜兜底）
  tasks/icebreak.py # 破冰任务（从 handler._idle_watcher + _icebreak_* 迁移）
```

**为什么任务状态放 `data/state/<群ID>.json`**：P1 建了这个文件（当前话题已在里面），
P2a 加 `tasks` 段即可，**不再新开一套目录**，也避免以后再迁一次（`version` 字段已在）。

**调度主循环（每 tick）**

```
for group in 已知群（有对话流或配置文件的群）:
    for task in registry（按优先级排序）:
        1) enabled?（群级开关 + 任务级开关）
        2) trigger 到点?（idle / interval / cron）
        3) 能跑吗?（静默时段 / 任务每日上限 / 群每日总上限 / 冷却 / 退避 / 群规模 / 群活跃前置）
        4) 同群同 tick 互斥（一个群一次只跑一个主动任务）+ 播报类优先级
        5) content_source.render(ctx) → 空则放弃（绝不硬发）
        6) 发送（复用 handler 的 _send_group / 分条间隔）
        7) 记录（时间/结果/内容指纹/话题标记）+ 更新状态/冷却/退避
```

**干跑与手动执行**（ROADMAP §4.10 的硬要求：测试群不开定时，必须有手动入口）

- 三者共用同一个 `Task.run(ctx, dry_run=...)`，**不复制任何生成逻辑**；
- 试跑：不发送、不写"已执行"状态、不计预算、不动冷却（否则会干扰真实调度）；
- 执行：真发一次，正常计预算与状态；
- 权限：仅 `ADMIN_QQ`。

### 3. 待确认问题（Q1~Q4，✅ 已答复 2026-10-07）

| 编号 | 问题 | 用户答复 | 落到实现上的约束 |
|---|---|---|---|
| Q1 | 破冰用什么触发控制 | ✅ **冷却 + 退避**（按 ROADMAP：idle 30 分 + 冷却 60 分 + 无人应答 30→60→120 分退避） | **去掉随机概率**；触发行为必须可解释、可预测；退避级别落进任务状态 |
| Q2 | 干跑结果发去哪 | ✅ **私聊管理员** | `app/bot/onebot.py` 需新增 `send_private_msg`；干跑内容走私聊，群里零打扰 |
| Q3 | 打扰预算是否现在落地 | ✅ **按 D18 落地**：每群每日合计 ≤8 条、静默 23:00–08:00 | 每任务每日上限 + 每群每日总上限 + 静默时段都要有单测证据 |
| Q4 | P1 的摘要是否迁入框架 | ✅ **迁入**（`summary_job`） | 摘要**逻辑不动**，只把触发入口换成框架；面板可见"上次摘要/要点数/画像数" |

**内容源的顺带决策（无需再问，D3 已定）**：破冰内容源改为 **LLM 话题包为主 + 热搜兜底**，
**去掉"表情包 + 配文"作为独立破冰手段**（D3）；"回复时按概率附一张表情"这条能力保留、不动。

### 4. 测试计划

| 层级 | 内容 | 判定 |
|---|---|---|
| 单测 | 触发判定（idle/interval/cron 含跨天、按群时区、cron 边界）、打扰预算（任务上限/群总上限/静默时段/冷却/退避）、状态读写与当日计数重置、指纹去重、配置合并优先级（群覆盖 > env > 默认） | 新增用例全绿 |
| 单测 | 干跑语义：不发送、不写状态、不计预算、不动冷却；执行语义：反之 | 断言状态文件前后差异 |
| 干跑 | `test_p2a_e2e.py`：注册假任务 → tick 到点 → 走完 8 步 → 断言发送/记录/状态；模拟"超预算被跳过"、"静默时段被跳过"、"内容源返回空 → 不发"；破冰任务在测试群被 D15 拦住、在实例群可被 `/任务 试跑` 触发 | 全过 |
| 真实验证 | 服务器：`/任务 列表` 看状态；`/任务 试跑 icebreak` 看内容质量；实例群等一次真实破冰 | 用户贴日志 |

### 5. 验收标准

1. **加一个新任务无需改 handler**（只在 `tasks/` 加文件 + 注册一行）；
2. `daily_total_limit` 与静默时段**确实拦得住**（有单测与干跑证据）；
3. 破冰行为与迁移前**等价或更好**（同样会破冰，且不会更频繁）；
4. 面板能看到每个任务的上次执行 / 当日次数 / 退避级别 / 下次预计时间，并有「试跑」「立即执行」按钮；
5. `/任务 试跑 icebreak` 在**测试群**可用（不打扰群），`/任务 执行` 只对管理员生效；
6. 全量测试通过（现有 14 个文件 + 新增）。

### 6. 实施记录

#### 6.1 框架核心（P2a-1 ~ P2a-4、P2a-7）（**已完成**，2026-10-07 深夜）

**新增模块**（`app/schedule/`，6 个文件 + 包导出）

| 文件 | 职责 |
|---|---|
| `state.py` | `TaskStateStore`：`data/schedule/<群ID>.json`（budget + tasks 两段）；`today_key()` / `fingerprint()` |
| `triggers.py` | `IdleTrigger` / `IntervalTrigger` / `CronTrigger` / `EventTrigger` + `in_quiet_hours()` / `in_window()` |
| `task.py` | `Task` 基类、`TaskContext`、`TaskResult`、`ContentSource` 协议、`CallableSource`、优先级常量 |
| `group_config.py` | `GroupConfigStore`：`data/group_config/<群ID>.json`，取值顺序 = 单群覆盖 > 环境变量 > 代码默认 |
| `scheduler.py` | `Scheduler`：tick 主循环、10 步判断、退避、任务记录、`run_task()`（手动/干跑）、`status()` |
| `__init__.py` | 对外导出（`Scheduler, Task, CallableSource, ...`） |

**关键设计决定（含一次自我纠错）**

- **任务状态独立成文件**：原计划沿用 P1 的 `data/state/<群ID>.json`，但那个文件由 `TopicState` **整体覆写** ——
  两个写者共用一个文件会互相抹掉对方数据。改为 `data/schedule/<群ID>.json`，各写各的，零耦合。
- **环境变量继续当全局默认**：`ICEBREAK_*` 全部映射进任务默认配置（用户已配好的东西不因上新框架失效）；
  单群 `data/group_config/<群ID>.json` 可逐项覆盖；**上线不需要新建任何文件**。
- **Q1 落地**：破冰去掉随机概率，改为"冷却 + 无人应答退避"（30 → 60 → 120 分钟），级别落进任务状态。
- **干跑语义**：`Task.execute(dry_run=True)` 只取内容；记账全由调度器负责，`dry_run` 时一步都不写。
- **D15 只留一处真相**：主动白名单仍只用 `PROACTIVE_GROUPS`；维护型任务（`is_proactive=False`）不受它限制。

**测试证据**

```
tests/test_p2a_schedule.py → 14/14 PASS
  触发：idle/interval/cron（到点/没到点/当天已跑/迟到超容忍/星期不匹配）+ 静默时段跨午夜 + 时段窗
  打扰控制：任务每日上限 2 → 发 2 条；群总上限 1 → 发 1 条；冷却中记"没到点"而非"被拦"
  前置条件：群人数不足 / 群不活跃（死群不打扰）
  D15：主动消息只到白名单群，维护消息照常到所有群
  退避：没人应答 → 门槛翻倍（31 分钟静默不再破冰）；有人说话 → 清零
  执行：内容为空/异常 → 不发送并分别记 empty/error
  干跑：内容生成了，但发送数、状态文件字节、预算、冷却**全部不变**
  手动执行：真发 + 记账；未知任务 → error
  脏数据保护：last_run 落在未来不会被永久卡死
  配置优先级：群覆盖（quiet/上限/idle/冷却）> 环境变量（最小群人数）> 默认
  可扩展性：新增 cron 任务只需"Task 子类 + register"，handler 一行都不用改（验收标准 1）
全量回归 15/15 全绿（连跑两遍）；数据目录审计干净
```

**开发中抓到的 4 个真问题**

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `_execute` 记账时没把 tick 的 `now` 传下去 | `last_run` 记真实时钟、调度用传入时间 → **冷却/当日计数错位** | `record_run/record_skip` 显式传 `now` |
| 2 | `settle_backoff` 从群配置文件读退避设置 | 任务自身 `defaults` 里的 backoff 被忽略 → 退避永不生效 | 改读**已注册任务的有效配置** |
| 3 | `Task.defaults` 是可变类属性 | 一处 `task.defaults["x"]=...` 污染同类所有实例（表现为"莫名进入冷却"） | `__init__` 里 `deepcopy` 一份 |
| 4 | `last_run` 落在未来（时钟回拨/脏数据） | `now - last_run` 为负 → 任务**永远卡在冷却里** | 超 now+60s 一律按"没跑过"处理并告警 |

**另外**：`quiet_hours` 在配置文件里是数组、环境变量里是字符串，统一在 `GroupConfigStore` 归一化成
`"HH:MM-HH:MM"`，避免两处解析不一致。

#### 6.2 破冰迁入 + 摘要迁入 + 手动入口 + 面板（P2a-5 / P2a-5b / P2a-6）（**已完成**，2026-10-08 凌晨）

**破冰迁入（P2a-5）**

- `app/schedule/tasks/icebreak.py`：`IcebreakTask`（`is_proactive=True`，idle 30 分 / 冷却 60 分 /
  每日 ≤4 / 最小群 3 人 / 工作时段 8-23 / 无人应答退避 ×2，最多 3 级）；
  内容源 = **LLM 话题包为主 → 热搜兜底**（D3：去掉"表情包+配文"作为独立破冰手段），两个都没内容就放弃。
- handler 侧：删掉 `_idle_watcher` / `_icebreak` / `_icebreak_sticker` / `_icebreak_search`，
  把 `_icebreak_llm` 改成**只产出文本**的 `_gen_icebreak_topic(ctx)`，另加 `_gen_icebreak_hot(ctx)`；
  发送与记账全部交给框架 —— 于是**干跑复用同一份生成逻辑，零复制**。

**摘要迁入（P2a-5b，Q4）**

- `app/schedule/tasks/summary_job.py`：`is_proactive=False`（不受 D15 限制、不占打扰预算）、
  `silent_ok=True`（干完活没有内容要发，不算"内容为空"）；摘要逻辑仍是 P1 的 `ConversationSummary.run()`。
- handler 侧：删掉内存计数 + `create_task` 那套触发（避免两套触发并存），
  改由 `_run_summary_job(gid)` 提供执行体（读窗口 → 判有没有新内容 → 调摘要 → 话题落 TopicState）。
- 新增 `TaskSkip` 信号：没有新内容时**主动跳过**（不更新 `last_run`），而不是记成 error 或 done。

**手动入口（P2a-6）**

- `/任务 列表`、`/任务 试跑 <名>`、`/任务 执行 <名>`、`/任务 重载`，**仅管理员**；
  结果一律**私聊管理员**（`OneBotClient.send_private_message` 新增），私聊失败才退回群，避免"发了命令没反应"。
- 面板：健康卡 payload 新增 `tasks`（每群每任务的启用/触发器/下次预计/上次结果/当日次数/退避/最近被拦），
  新增 `POST /api/task`（`action=list|dry|run`）与任务卡片（**试跑**／**立即执行**按钮）。
  面板按钮与命令**走同一个 `Scheduler.run_task`**，不复制逻辑。

**测试证据**

```
tests/test_p2a_e2e.py → 10 项全过：
  ① 注册了 icebreak（主动）+ summary_job（维护）
  ② /任务 列表 → 私聊收到状态，群里零打扰
  ③ /任务 试跑 → 私聊拿到内容；群里零打扰、状态文件零变化
  ④ /任务 执行 → 真发到群 + 记账（last_run/当日次数/预算）+ 破冰内容进对话流
  ⑤ 非管理员 → 被拒绝
  ⑥ D15：tick 一轮，主动消息只到实例群
  ⑦ 静默时段（03:00）：一条主动消息都没发
  ⑧ summary_job 在测试群也跑通，且不占打扰预算
  ⑨ 健康卡带上任务数据（4 群 × 2 任务，字段齐全）
  ⑨b /api/task：401 鉴权 / list / dry / 缺参数 400 全部正确（用 aiohttp 替身验证接线）
全量回归 16/16 全绿（连跑两遍）；数据目录审计干净
```

**本轮抓到的 3 个问题**

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | 摘要迁入后，`Task` 没有"干完活但没内容要发"的语义 | 摘要会被判成"内容为空"→ 不记 `last_run` → **每个 tick 重复触发** | 新增 `silent_ok` + `done` 状态；"没新内容"用 `TaskSkip` 表示 |
| 2 | **测试隔离不完整**：`MessageHandler` 内部自建 `Scheduler`/`Summary`/`Topic`，把状态写进真实 `data/schedule`、`data/summary`、`data/state` | 测试污染真实数据（审计抓到 12 个文件，含真实群号） | `helpers_isolate` 补齐这 5 个模块级目录补丁；两个老测试（`test_async_nonblocking`/`test_handler_dispatch`）改用统一 helper |
| 3 | 测试里"群很久没说话"用 `last_activity=0` 表达 | `_idle_minutes` 把 0 视为"没有活跃记录"（保守当不空闲）→ 破冰永不触发，误判为框架 bug | 测试改用 `day - 3600`；语义本身正确，写进注释 |

#### 6.3 发布清单（P2a，**待用户替换**）

**必需（新增 9 个文件 + 覆盖 5 个）**

| 服务器路径 | 操作 | 说明 | 自检（期望值 = 行数） |
|---|---|---|---|
| `app/schedule/__init__.py` | 🆕 新建 | 框架导出 | `grep -c Scheduler app/schedule/__init__.py` → 5 |
| `app/schedule/state.py` | 🆕 新建 | 任务状态（`data/schedule/<群>.json`） | `grep -c "class TaskStateStore" app/schedule/state.py` → 1 |
| `app/schedule/triggers.py` | 🆕 新建 | idle/interval/cron/event | `grep -c "class CronTrigger" app/schedule/triggers.py` → 1 |
| `app/schedule/task.py` | 🆕 新建 | Task/TaskContext/ContentSource | `grep -c "class TaskSkip" app/schedule/task.py` → 1 |
| `app/schedule/scheduler.py` | 🆕 新建 | 调度主循环 | `grep -c "class Scheduler" app/schedule/scheduler.py` → 1 |
| `app/schedule/group_config.py` | 🆕 新建 | 单群覆盖配置 | `grep -c "class GroupConfigStore" app/schedule/group_config.py` → 1 |
| `app/schedule/tasks/__init__.py` | 🆕 新建 | 任务导出 | `grep -c IcebreakTask app/schedule/tasks/__init__.py` → 2 |
| `app/schedule/tasks/icebreak.py` | 🆕 新建 | 破冰任务 | `grep -c "class IcebreakTask" app/schedule/tasks/icebreak.py` → 1 |
| `app/schedule/tasks/summary_job.py` | 🆕 新建 | 摘要任务 | `grep -c "class SummaryJob" app/schedule/tasks/summary_job.py` → 1 |
| `app/paths.py` | 覆盖 | 新增 `SCHEDULE_DIR`/`GROUP_CONFIG_DIR`/`TASK_LOG_DIR` | `grep -c SCHEDULE_DIR app/paths.py` → 1 |
| `app/config.py` | 覆盖 | 新增 `SCHEDULER_*`/`QUIET_HOURS`/`DAILY_TOTAL_LIMIT` 等 | `grep -c SCHEDULER_ENABLED app/config.py` → 1 |
| `app/bot/handler.py` | 覆盖（**84KB，建议拖拽**） | 破冰/摘要迁入、`/任务` 命令、调度器接线、`[DEBUG]` 日志降噪 | `grep -cE "_collect_sections\|_cmd_task" app/bot/handler.py` → 5 |
| `app/bot/onebot.py` | 覆盖 | 新增 `send_private_message` | `grep -c send_private_message app/bot/onebot.py` → 1 |
| `app/main.py` | 覆盖 | 启动调度器 + 退出时停止 | `grep -c start_scheduler app/main.py` → 1 |
| `app/dashboard/health.py` | 覆盖 | payload 增加 `tasks` | `grep -c '"tasks"' app/dashboard/health.py` → 1 |
| `app/dashboard/server.py` | 覆盖 | 新增 `POST /api/task` | `grep -c api_task app/dashboard/server.py` → 2 |
| `app/dashboard/index.html` | 覆盖 | 任务卡片 + 试跑/立即执行按钮 | `grep -c renderTasks app/dashboard/index.html` → 2 |

**可选**：`tests/**`（`test_p2a_schedule.py`🆕、`test_p2a_e2e.py`🆕、`helpers_isolate.py` 等）

**服务器侧操作**：无需新增配置（环境变量继续当全局默认）。若想收紧打扰，可在 `.env` 里加：

```bash
# 可选：每群每天主动消息总上限（默认 8）、静默时段（默认 23:00-08:00）
grep -q '^DAILY_TOTAL_LIMIT=' /root/qqbot/.env || echo 'DAILY_TOTAL_LIMIT=8' >> /root/qqbot/.env
grep -q '^QUIET_HOURS=' /root/qqbot/.env || echo 'QUIET_HOURS=23:00-08:00' >> /root/qqbot/.env
systemctl restart qqbot
```

**部署后验收（在实例群或私聊里操作）**

```bash
journalctl -u qqbot -n 40 --no-pager | grep -E "\[Schedule\]|调度器已启动|已注册任务"
#   期望：[Schedule] 调度器已启动：2 个任务 / tick=30s / 静默 23:00-08:00 / 每群每日上限 8 条
#        [Schedule] 已注册任务: icebreak（静默 30 分钟，优先级 50）
#        [Schedule] 已注册任务: summary_job（每 10 分钟，优先级 90）
```

在 QQ 里（**仅管理员可用**）：

```
/任务 列表            → 私聊收到每个任务的状态（下次预计、上次结果、今日次数、退避级别）
/任务 试跑 icebreak   → 私聊收到一条破冰内容；群里没有任何消息
/任务 执行 icebreak   → 群里真发一条（补播用）
```

**新增目录**（代码自动创建，`data/` 依旧不要手动碰）：`data/schedule/`、`data/group_config/`、`data/tasks/`

---

## [P2a-hotfix-1] 手动 `/任务 执行` 绕过 D15，把破冰发进了测试群（**用户抓到的，已修**）

- 日期：2026-10-08
- 状态：**开发+测试完成，待发布**（3 个文件）
- 关联：D15、ROADMAP §4.10、`[P2a-0]` 6.2

### 1. 现象（用户截图 + 实测）

管理员在**测试群 100000005** 依次发了 `/任务 列表`、`/任务 试跑 icebreak`、`/任务 执行 icebreak`，
结果**破冰内容"魔法使之夜的青子线，你们第一个选的谁？"真的发进了测试群**。

### 2. 根因

`Scheduler.run_task()`（手动入口）只做了"执行 + 记账"，**完全没有走 `tick()` 里那套闸门** ——
既不检查 D15 主动名单，也不检查静默时段。而自动路径（`tick` → `_gate`）是好的：
截图里测试群那行就写着 `最近被拦：不在主动打扰名单`。

**设计错误在我**：DEVLOG 里写"执行 = 真发一次（用于补播）"，却没想清楚
"手动"不等于"可以突破安全边界"。

### 3. 修复

| 闸门 | 手动「执行」 | 说明 |
|---|---|---|
| **D15 主动名单** | ⛔ **硬拦**（`force` 也不放行） | 测试群/放置群永远不该收到主动消息，这是用户明确要求 |
| 静默时段 | ⛔ 默认拦，提示 `确认要补发请用「/任务 执行 <名> 强制」` | 管理员半夜补播是合法场景，但要**显式确认** |
| 每日上限 / 冷却 | ✅ 不拦 | "补播"本来就是自动化被限额挡住时的兜底 |
| 试跑（dry_run） | ✅ 不受限 | 它不发送，怎么跑都不打扰 |

同时新增 `run_task(..., now=...)`，让测试与面板能指定时间（不必 monkey-patch `time.time`）。

### 4. 顺手修掉的第二个隐患：**模型会生成 `@全体成员`**

截图里的试跑内容是 `@全体成员 今晚有人打魔夜吗，我卡在第七关半小时了……` ——
如果那次是"执行"，就是一次**真·炸群**。

修法：在 `Task.execute()` 里对所有任务内容做统一清洗（`sanitize_outgoing()`），
去掉 `[CQ:at,qq=all]` / `[CQ:at,qq=0]` / `@全体成员` / `@所有人` / `@all`。
放在框架层而不是某个任务里 —— 因为这是"所有主动消息"的共同底线，而且**试跑也清洗**，
这样管理员在私聊里看到的就是"真要发出去的样子"。

### 5. 测试证据

```
tests/test_p2a_schedule.py → 16/16（新增 2 个）
  · test_manual_run_respects_d15_and_quiet_hours：非白名单群被拒（force 也无效）、
    静默时段默认拒绝且提示里有「强制」、force 后放行、试跑不受限
  · test_sanitize_at_all：@全体成员/[CQ:at,qq=all]/@所有人/@all 全部被去掉，
    且经过真实 tick 发送链路验证
tests/test_p2a_e2e.py → 12 项（新增 ④b/④c）
  ④b 测试群 `/任务 执行 icebreak 强制` → 被 D15 拦下，未发送、未记账
  ④c 清洗生效
全量 16/16 全绿（连跑两遍）；数据目录审计干净
```

### 6. 发布清单（3 个文件，覆盖）

| 服务器路径 | 内容 | 自检（期望值 = 行数） |
|---|---|---|
| `app/schedule/scheduler.py` | `_manual_gate()`：D15 硬拦 + 静默时段需强制；`run_task` 支持 `force`/`now` | `grep -c "_manual_gate" app/schedule/scheduler.py` → 2 |
| `app/schedule/task.py` | `sanitize_outgoing()` + 在 `execute()` 里统一清洗 | `grep -c "sanitize_outgoing" app/schedule/task.py` → 2 |
| `app/bot/handler.py` | `/任务 执行 <名> 强制`；被拦时显示原因 | `grep -c "强制" app/bot/handler.py` → 2 |
| `app/dashboard/server.py` | `/api/task` 支持 `force` 字段（面板同理受同一闸门约束） | `grep -c "force" app/dashboard/server.py` → 1 |

**部署后的预期行为**：

```
/test 群：/任务 执行 icebreak          → ⛔ 该群不在主动打扰名单（PROACTIVE_GROUPS）里，主动任务不允许手动执行
/test 群：/任务 执行 icebreak 强制      → ⛔ 同上（D15 是硬闸门，force 也不放行）
实例群  ：/任务 执行 icebreak          → 白天正常发送；静默时段提示用「强制」
实例群  ：/任务 试跑 icebreak          → 正常生成（不发送），内容里的 @全体成员 已被清掉
```

### 7. [P2a-hotfix-2] 模型会 @ 它自己（同一批修复，用户截图发现）

**现象**：hotfix-1 部署后，测试群试跑出来的内容是

```
@爱丽丝 原神今天那个新活动你们打了吗？我卡在第二关了诶……
```

**为什么必须修**（不只是"看着怪"）：如果模型某次生成的是**真 CQ 码** `[CQ:at,qq=100000007]`，
那么这条主动消息会把 bot 自己 @ 出来 → 触发自身的 @-处理分支 → **可能形成自我回复循环**。

**修法**（两层）：

1. `sanitize_outgoing()` 扩展：除 `@全体成员` 那批之外，再清掉
   `[CQ:at,qq=<BOT_QQ>]`（两种大小写）、`@<BOT_QQ>`、`@<BOT_NAME>`；
2. `config/scenes/icebreak.txt` 明确要求"**不要 @ 任何人**（含 @全体成员 / @你自己 / @群友）"，
   从源头降低生成概率。

**测试**：`test_sanitize_at_all` 扩展为同时覆盖"@全体成员"与"@自己"（明文 + CQ 码两种形式）。
全量 16/16 全绿（连跑两遍）。

**本批发布（2 个文件，覆盖；与 hotfix-1 的 4 个文件一起传即可，共 5 个去重后）**

| 服务器路径 | 内容 | 自检（期望值 = 行数） |
|---|---|---|
| `app/schedule/task.py` | `sanitize_outgoing()` 增加"@自己"清洗（hotfix-1 + hotfix-2 + hotfix-3 都在这个文件里） | `grep -c "bot_name" app/schedule/task.py` → 5 |
| `config/scenes/icebreak.txt` | 破冰模板新增"不要 @ 任何人" | `grep -c "不要 @ 任何人" config/scenes/icebreak.txt` → 1 |

### 8. [P2a-hotfix-3] 简称没被清掉（**用户用确定性检查抓到的**）

**现象**：用户按建议跑了确定性检查（直接调 `sanitize_outgoing`），结果：

```
'@全体成员 快来'            -> '快来'         ✅
'@爱丽丝 在吗'              -> '@爱丽丝 在吗'  ❌ 没清掉
'[CQ:at,qq=100000007] 喂' -> '喂'           ✅
```

**根因**：服务器上 `BOT_NAME=天童爱丽丝`，而清洗只匹配**全名** `@天童爱丽丝`；
模型实际写的是**简称** `@爱丽丝` → 匹配不到。

**修法**：把 `BOT_NAME` 的**所有长度≥2 的后缀**都加入候选（长名先替换）：
`天童爱丽丝 → 天童爱丽丝 / 童爱丽丝 / 爱丽丝 / 丽丝`。

**顺带加固（防御纵深）**：`_send_group()` 是**所有出站消息的最后一道闸门**，
在那里也调用一次 `sanitize_outgoing()` —— 这样闲聊回复、表情配文、命令回复
这些不经过 `Task` 的路径同样受保护。只删"@全体成员 / @自己"，**不会**误伤正常的 @群友。

**测试**：
- `test_sanitize_at_all` 扩展为覆盖全名 / 简称 / CQ 码三种形式，并新增一条
  **"正常 @群友必须保留"** 的反向断言（防止清洗过度）；
- 本地用真实配置复现验证：`BOT_NAME=天童爱丽丝` 时 `@爱丽丝`、`@天童爱丽丝` 都被清掉，
  `[CQ:at,qq=888888]` 原样保留。
- 全量 16/16 全绿（连跑两遍）；P2a 干跑 12 项全过；数据审计干净。

**本批最终要传的文件（5 个，覆盖）**：`app/schedule/scheduler.py`、`app/schedule/task.py`、
`app/bot/handler.py`、`app/dashboard/server.py`、`config/scenes/icebreak.txt`
（hotfix-1/2/3 的改动都合并在 `task.py` 与 `handler.py` 里，不需要为每一轮单独传）

---

## [P2b-0] P2b 阶段规划：天气播报 + 喝水提醒（**规划中，等 Q1~Q4 答复后动工**）

- 日期：2026-10-08
- 状态：**规划中**
- 关联：ROADMAP §5（5.1~5.6）、§4.6 任务清单、D15/D18、`[P2a-0]`

### 1. 范围

| 编号 | 内容 | 说明 |
|---|---|---|
| P2b-1 | `WeatherService`：provider（Open-Meteo）+ 规则引擎（注意事项）+ 缓存 + 降级 | 一个服务两个入口，**绝不写两份逻辑**（5.1） |
| P2b-2 | `weather_morning` 任务（cron 每早，默认 08:00） | 复用 P2a 框架；内容 = 事实 + 注意事项 + 收尾（5.3） |
| P2b-3 | `/天气 [城市]` 命令（被动响应，任何群可用） | 任意群都能查；不带参数用本群绑定城市（5.4） |
| P2b-4 | `water_reminder` 任务（interval + 时段窗 + 群活跃前置 + 每日上限） | 喝水与久坐**合并为一个任务**，文案池轮换（4.6） |
| P2b-5 | 群城市绑定（`data/group_config/<群ID>.json` 的 `weather.city`） | 未绑城市的群**不播报**（宁可没有，也不要发错城市） |

**不做**：RSS/每日一句/每日一图/话题轮播（P2c）、周报与欢迎（P2d）、和风天气生活指数（将来 `WEATHER_PROVIDER=qweather` 再切）。

### 2. 设计要点（初稿）

**模块**

```
app/weather/
  __init__.py
  provider.py    # CITIES（4 个预置城市 + 经纬度）、resolve_city()、WeatherProvider 协议、OpenMeteoProvider
  service.py     # WeatherService：缓存 + 规则引擎 + 两种渲染（播报 / 命令）
app/schedule/tasks/weather_morning.py   # cron 任务
app/schedule/tasks/water_reminder.py    # interval 任务（喝水/久坐合并）
```

**数据**：`data/cache/weather/<城市>_<日期>.json`（缓存键 = 城市 + 日期；同城多群只请求一次）

**配置**（沿用 P2a 的"单群覆盖 > 环境变量 > 代码默认"）

- 环境变量默认：`WEATHER_ENABLED`、`WEATHER_PROVIDER=open-meteo`、`WEATHER_CITY`（全局兜底城市，默认空=不播报）、
  `WATER_REMINDER_ENABLED`、`WATER_REMINDER_WINDOW`、`WATER_REMINDER_INTERVAL_MINUTES`、`WATER_REMINDER_DAILY_LIMIT`
- 单群覆盖：`data/group_config/<群ID>.json` → `{"weather": {"city": "北京", "broadcast_at": "08:00"}}`
- **群绑定（ROADMAP 已定）**：100000004 实例群→北京（全开）、100000005 测试群→北京（全关，靠手动验证）、100000006 不绑

**播报内容（3 条短句，5.3）**

1. **事实**：气温区间 + 降水概率 + 风力（代码拼，不经 LLM）
2. **注意事项**：代码规则映射（阈值见 5.3，具体取值待 Q4 确认）
3. **收尾**：一句游戏化口吻 + 轻钩子（**是否 LLM 润色待 Q1 确认**）

**降级（5.5，硬要求）**

- API 失败/超时 → **本次不播报**（绝不硬发占位内容），只记日志；
- 当天 8 点后**不补发**（补发早报很怪）；
- `/天气` 失败 → 明确回"天气服务暂时查不到"，不猜。

### 3. 待确认问题（Q1~Q4，✅ 已答复 2026-10-08）

| 编号 | 问题 | 用户答复 | 落到实现上的约束 |
|---|---|---|---|
| Q1 | 收尾句怎么做 | ✅ **模板 + LLM 润色一句** | 事实/注意事项由代码拼；收尾调一次 LLM（每群每天 1 次），**LLM 挂了必须回落到模板** |
| Q2 | 实例群绑定城市 | ✅ **北京**（按原计划） | 100000004 → 北京；测试群 100000005 也绑北京但定时全关 |
| Q3 | 喝水提醒时段/频率 | ✅ **按 D18**：每 75 分 / 10:00–18:00 / 每日 ≤4 / 群最近 30 分钟活跃 | 计入每群每日 ≤8 条的打扰总额度 |
| Q4 | 注意事项阈值 | ✅ **按 ROADMAP §5.3** | 降雨≥40% 带伞、温差≥8℃ 加衣、最高≥33℃ 防暑、最低≤5℃ 保暖、UV≥6 防晒、风大提醒 |

### 4. 测试计划

| 层级 | 内容 | 判定 |
|---|---|---|
| 单测 | 城市解析（4 个预置 / 未知城市 / 未绑定）、provider 解析（桩 HTTP 响应，含字段缺失）、**缓存命中不发第二次请求**、缓存跨天失效、API 失败→返回 None、**规则引擎每个阈值**（降雨/温差/高温/低温/UV/风）、播报三段结构、`/天气` 文案 | 全绿 |
| 单测 | `water_reminder` 文案池轮换（喝水/久坐交替、不重复）、时段窗、群活跃前置、每日上限 | 全绿 |
| 干跑 | `test_p2b_e2e.py`：注册两个新任务 → `tick(08:00)` 时**只有绑了城市且在主动名单的群**收到播报；未绑城市的群跳过；API 失败时一条都不发；`tick(12:00)` 且群活跃 → 喝水提醒发出且当天 ≤4；`/天气` 命令在测试群也能用（被动响应） | 全过 |
| 真实验证 | 服务器：`/天气 北京` 看真实数据；`/任务 试跑 weather_morning` 看播报文案质量；实例群等一次真实 8 点播报 | 用户贴日志/截图 |

### 5. 验收标准

1. **8 点准时播报，且内容随天气变化**（不同天气 → 不同注意事项；同一天同城只请求一次 API）；
2. **多群不同城市互不串**（缓存按城市隔离，群里发的是自己城市的天气）；
3. **API 挂了不发**（有单测与干跑证据，绝不发占位内容）；
4. **提醒只在群活跃时出现**，且**一天 ≤ 设定上限**、只在时段窗内；
5. **未绑城市的群不播报**；
6. `/天气` 在任何群（含测试群/放置群）可用 —— 它是被动响应，不受 D15 限制；
7. 全量测试通过（现有 16 个文件 + 新增）。

### 6. 实施记录

#### 6.1 天气服务 + 两个任务（P2b-1 ~ P2b-5）（**已完成**，2026-10-08）

**新增模块**

| 文件 | 职责 |
|---|---|
| `app/weather/provider.py` | `CITIES`（4 个预置城市 + 经纬度）、`resolve_city()`（支持英文/简写/带"市"）、`describe_code()`（WMO→中文）、`OpenMeteoProvider`（可插拔，将来加 QWeather） |
| `app/weather/service.py` | `WeatherService`：缓存（`data/cache/weather/<城市>_<日期>.json`）+ 规则引擎 `notes_for()` + 两种渲染 `render_broadcast()` / `render_command()` |
| `app/schedule/tasks/weather_morning.py` | 早间播报任务（cron，默认 08:00，可按群覆盖 `broadcast_at`） |
| `app/schedule/tasks/water_reminder.py` | 喝水/久坐提醒（interval 75 分、窗口 10:00–18:00、每日 ≤4、群活跃前置），**文案池零 LLM 成本** |

**关键设计**

- **一个服务两个入口**（§5.1）：`/天气` 命令与定时播报共用 `WeatherService`，绝不写两份逻辑；
- **规则引擎不经 LLM**（§5.3 / Q4）：阈值全部代码化、可单测；注意事项最多 3 条（避免播报变长）；
- **收尾句模板 + 一次 LLM 润色**（Q1）：润色失败一律回落模板，播报不受影响；
- **未绑城市不播报**（§5.4）：任务 `TaskSkip` 安静跳过 —— 宁可没有，也不要发错城市；
- **失败不播报、不补发**（§5.5）：API 挂了返回 None → 跳过；`/天气` 则明确说"查不到"；
- **`Task.refine_config()` 钩子**：群配置里的 `weather.broadcast_at` 映射到触发器 `at`，
  调度器不需要认识任何具体业务字段。

**测试证据**

```
tests/test_p2b_weather.py → 13/13 PASS
  城市解析（4 预置 / 英文 / 简写 / "北京市" / 未收录）、WMO 代码映射
  provider 解析（正常 / 缺温度 / 空 daily / 可选字段缺失）
  provider 网络失败（HTTP 500 / 超时 / JSON 炸 / 未收录城市不发请求）→ 全部 None，不抛给上层
  缓存（落盘可被新实例读到 / 命中不再请求 / 城市隔离 / 跨天失效 / **失败不写缓存**）
  规则阈值边界（≥40% 伞、≥8℃ 添衣、≥33℃ 防暑、≤5℃ 保暖、UV≥6、风≥30km/h）+ 最多 3 条
  事实句拼装、播报与命令渲染
  weather_morning：未绑城市跳过 / API 失败跳过 / 全局兜底城市可用 / 润色失败回落模板
  water_reminder：喝水久坐交替、窗口解析、默认值来自 Config、per-group broadcast_at 覆盖

tests/test_p2b_e2e.py → 8 项全过
  ① 08:00 只播报到绑了城市的群（未绑城市的群被跳过；同城只请求 1 次 API）
  ② 换天气数据 → 注意事项跟着变（雨天带伞 ↔ 高温防暑）
  ③ 缓存：北京 1 次 / 上海 1 次；换实例读磁盘缓存也不再请求（互不串）
  ④ 天气 API 失败 → 播报一条都不发
  ⑤ 12:00 提醒共 4 条（上限 4）、喝水与久坐交替
  ⑥ 死群（6 小时没人说话）→ 不提醒
  ⑦ /天气 在测试群可用（被动响应不受 D15 限制）；未收录城市给明确提示
  ⑧ /天气 取数失败 → 明确回"查不到"，不猜
全量 18/18 全绿（连跑两遍）；数据目录审计干净
```

**本轮抓到的 2 个问题**

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | provider 返回的城市名可能与请求不符 | 缓存文件名按请求城市、内容却是别人的天气 → **张冠李戴** | `fetch_daily` 强制以请求城市为准并告警 |
| 2 | 测试里还原 `WeatherService.today` 时把 staticmethod 解包成了普通函数 | `TypeError: today() takes 0 positional arguments but 1 was given` | 从 `__dict__` 取 staticmethod 对象再还原（**和 P0 日志 converter 是同一个坑**） |

#### 6.2 发布清单（P2b，**待用户替换 + 一条服务器侧操作**）

**新增 5 个文件**

| 服务器路径 | 操作 | 自检（期望值 = 行数） |
|---|---|---|
| `app/weather/__init__.py` | 🆕 新建 | `grep -c available_cities app/weather/__init__.py` → 2 |
| `app/weather/provider.py` | 🆕 新建 | `grep -c "class OpenMeteoProvider" app/weather/provider.py` → 1 |
| `app/weather/service.py` | 🆕 新建 | `grep -c "class WeatherService" app/weather/service.py` → 1 |
| `app/schedule/tasks/weather_morning.py` | 🆕 新建 | `grep -c "class WeatherMorningTask" app/schedule/tasks/weather_morning.py` → 1 |
| `app/schedule/tasks/water_reminder.py` | 🆕 新建 | `grep -c "class WaterReminderTask" app/schedule/tasks/water_reminder.py` → 1 |

**覆盖 5 个文件**

| 服务器路径 | 内容 | 自检 |
|---|---|---|
| `app/paths.py` | 新增 `WEATHER_CACHE_DIR` | `grep -c WEATHER_CACHE_DIR app/paths.py` → 1 |
| `app/config.py` | `WEATHER_*` / `WATER_REMINDER_*` | `grep -c WEATHER_ENABLED app/config.py` → 1 |
| `app/schedule/tasks/__init__.py` | 导出两个新任务 | `grep -c WeatherMorningTask app/schedule/tasks/__init__.py` → 2 |
| `app/schedule/group_config.py` | `weather` 段 + `weather_city()` + 两个任务的 env 默认 | `grep -c weather_city app/schedule/group_config.py` → 1 |
| `app/schedule/task.py` | 新增 `refine_config()` 钩子 | `grep -c refine_config app/schedule/task.py` → 2 |
| `app/bot/handler.py` | `/天气` 命令 + 收尾句润色 + 注册两个任务 | `grep -c "_handle_weather" app/bot/handler.py` → 2 |

**服务器侧操作（1 条，创建实例群的城市绑定）**

> ⚠️ 这一步必须做：**未绑城市的群不播报**（宁可没有，也不要发错城市）。
> 这是**新建文件**，不会覆盖你已有的数据。

```bash
mkdir -p /root/qqbot/data/group_config
cat > /root/qqbot/data/group_config/100000004.json <<'EOF'
{
  "weather": { "city": "北京", "broadcast_at": "08:00" }
}
EOF
cat /root/qqbot/data/group_config/100000004.json   # 确认写入
systemctl restart qqbot
```

（想给别的群也开天气：同样格式建 `<群号>.json` 即可，无需改代码。）

**部署后验收**

```bash
# 启动日志应看到 4 个任务
journalctl -u qqbot -n 40 --no-pager | grep -E "\[Schedule\]"
# 期望：已注册任务: icebreak / summary_job / weather_morning / water_reminder

# 直接问天气（任何群可用，包括测试群）
#   /天气            → 用本群绑定城市（实例群=北京）
#   /天气 哈尔滨      → 指定城市
#   /天气 火星        → 明确提示"还没收录"

# 试跑播报（不发送，结果私聊管理员）
#   /任务 试跑 weather_morning
# 期望私聊里出现：🌤 北京 · 10月X日 / 温度事实 / ⚠️ 注意事项 / 一句收尾
```

**新增目录**（代码自动创建）：`data/cache/weather/`

---

## [P2c-0] P2c 阶段规划：资讯 / 每日一句 / 每日一图 / 话题轮播 / 效果统计（**规划中，等 Q1~Q4**）

- 日期：2026-10-08
- 状态：**规划中**
- 关联：ROADMAP §4.5 / §4.6、附录 A.5~A.9、D3/D18/D19、`[P2a-0]`、`[P2b-0]`

### 1. 范围

| 编号 | 内容 | 依赖 |
|---|---|---|
| P2c-1 | **RSS 资讯模块**：机核 `https://www.gcores.com/rss`、游研社 `https://www.yystv.cn/rss/feed`（实测可用，字段见 A.5） | — |
| P2c-2 | **B站专栏源**：`api.bilibili.com/x/space/article?mid=<UID>`（无需 key/签名，但要浏览器 UA + Referer，A.7） | Q1 决定启用哪些 UP |
| P2c-3 | **资讯 → "标题 + 一句话点评 + 原文链接"**（D19：不搬运正文、必带来源） | LLM |
| P2c-4 | **每日一句**（cron 12:30，游戏/动漫向） | 话题库 + LLM（Q2） |
| P2c-5 | **每日一图**（cron 19:30）：取当天资讯的封面/内嵌图 → 下载到本地 → 发送 + 来源链接；取不到就跳过 | P2c-1/2 |
| P2c-6 | **群历史回顾**：从统一对话流翻"今早那个话题后来怎样了" | 统一对话流（P0） |
| P2c-7 | **话题库轮播**：话题池 + 使用计数，避免连着重复 | Q4 |
| P2c-8 | **效果统计**：按"内容类型"聚合被回应率，面板可见"哪类内容有人接" | metrics（P0） |
| P2c-9 | 搜索降为兜底：破冰内容源顺序里排最后 | — |

**不做**：视频订阅（需 wbi 签名 + 风控风险，A.8 结论）、自建 RSSHub、爬虫（A.6 红线）、周报与欢迎（P2d）、检索式记忆（P3）。

### 2. 设计要点（初稿）

**模块**

```
app/news/
  __init__.py
  models.py     # NewsItem（source/title/link/pub_time/image/category/summary）
  sources.py    # NewsSource 协议 + RssSource（stdlib ElementTree，零新依赖）+ BilibiliArticleSource
  service.py    # NewsService：拉取 + 缓存 + 每源冷却/退避 + 指纹去重 + 选图
  image.py      # 图片下载（大小上限、超时、落 data/cache/news_image/）
app/schedule/tasks/
  daily_quote.py   # 每日一句（cron 12:30）
  daily_image.py   # 每日一图（cron 19:30，带来源链接）
  news_digest.py   # 资讯任务（形态待 Q3 定）
app/context/topics.py  # 话题库（池子 + 使用计数轮播）+ 群历史回顾内容源
```

**资讯 → 文案**（D19 + A.6）：`标题` + `一句话点评（LLM）` + `原文链接` + `来源：机核/游研社/B站`。
**绝不搬运正文**；图片保留原站水印并注明来源。

**频控与降级**（A.6/A.8 硬要求）：

- 每个源**独立冷却**（30~60 分钟）与失败退避；同一分钟内不重复请求；**启动时不并发拉所有源**；
- 条件请求（ETag/Last-Modified）→ 多数请求 304；
- 某源没更新/挂了 → 换下一个源；**全挂则本次不发**（绝不发占位内容）；
- 指纹（`guid`/`link`/`cv号`）**跨天去重**，避免连续几天推同一条。

**破冰内容源改为轮换**（§4.5）：LLM 话题包 → 群历史回顾 → 资讯 → 话题库 → 热搜兜底；
每类记录使用次数与"被回应"结果，供 P2c-8 统计。

### 3. 待确认问题（Q1~Q4，✅ 已答复 2026-10-08）

| 编号 | 问题 | 用户答复 | 落到实现上的约束 |
|---|---|---|---|
| Q1 | B站专栏源启用哪些 UP | ✅ **上三个**：2072586344（Gal周报）、5441521（MoeSekai情报局）、3546730752641311（KALPA官方） | 三个源都要**独立冷却 + 失败退避**（B站按 IP 限速，见 A.8 的频控观测） |
| Q2 | 每日一句怎么做 | ✅ **台词池 + LLM 偶尔原创** | 池子为主（轮播不重复）；每周挑 1~2 天让 LLM 原创一句补充 |
| Q3 | 资讯形态 | ✅ **独立资讯任务（午间）+ 参与破冰轮换** | 新增 `news_digest` 任务（11:30，避开 12:30 的每日一句）；破冰内容源里也放资讯 |
| Q4 | 话题库怎么建 | ✅ **预置池子 + 轮播** | 我预置约 30 条（带类型标签），按使用次数轮播，**不接 LLM 补库** |

### 4. 测试计划

| 层级 | 内容 | 判定 |
|---|---|---|
| 单测 | RSS 解析（桩 XML：标准项 / 缺 thumb / 缺 pubDate / CDATA / 畸形项跳过）、B站 JSON 解析（正常 / code≠0 / 缺 image_urls / 时间戳换算）、指纹去重、每源冷却与失败退避、条件请求头、**全挂→空**、图片下载（大小上限 / 非图片 content-type / 超时 → None）、话题库轮播不重复、文案组装（标题+点评+链接+来源） | 全绿 |
| 干跑 | `test_p2c_e2e.py`：注册新任务 → `tick(12:30)` 每日一句发出且带来源；`tick(19:30)` 每日一图**真的带图片 CQ 码 + 来源链接**；取不到图 → 跳过；资讯任务只发标题+点评+链接（**不含正文**）；连续两天同一条资讯不重复；效果统计按类型聚合 | 全过 |
| 真实验证 | 服务器：`/任务 试跑 daily_quote`、`/任务 试跑 daily_image`、`/任务 试跑 news_digest` 看文案与图片；面板看"哪类内容有人接" | 用户贴截图 |

### 5. 验收标准

1. **连续多天内容不重复**（指纹去重有单测 + 干跑证据）；
2. **面板能看到"哪类内容有人接"**（按内容类型聚合被回应率）；
3. **每日一图能取到图并带来源链接，取不到就跳过**（不留占位图、不发纯文字充数）；
4. 资讯只发"标题 + 一句话点评 + 原文链接"，**不含正文**；
5. 某源挂了能自动换源，**全挂则不发**；
6. 每源限频生效（30~60 分钟冷却 + 失败退避，不触发 B站风控）；
7. 全量测试通过（现有 18 个文件 + 新增）。

### 6. 实施记录

#### 6.1 资讯 / 每日一句 / 每日一图 / 话题轮播 / 效果统计（P2c-1 ~ P2c-8）（**已完成**，2026-10-08）

**新增模块**

| 文件 | 职责 |
|---|---|
| `app/news/models.py` | `NewsItem`（所有源统一结构）+ `SourceState`（冷却/退避/ETag + **退避 ×2/×4/×8 封顶**） |
| `app/news/sources.py` | `RssSource`（**stdlib ElementTree，零新依赖**）+ `BilibiliArticleSource`（带浏览器 UA + Referer）+ 两个解析函数 + 预置源清单 |
| `app/news/service.py` | `NewsService`：每源冷却/失败退避、条件请求（304）、**指纹去重**、选条（资讯/配图） |
| `app/news/image.py` | 配图下载（体积上限 / 必须 image/* / 超时→None / 落 `data/news_images/`）+ CQ 码 |
| `app/context/topic_pool.py` | 话题池（**30 条预置**，带类型标签）+ 台词池（**20 条**）+ 轮播（按使用次数、避开本群最近用过） |
| `app/schedule/tasks/news_digest.py` | 资讯任务（cron 11:30）：标题 + 一句话点评 + 来源 + 原文链接 |
| `app/schedule/tasks/daily_quote.py` | 每日一句（cron 12:30）：池子为主 + 每 N 天一次 LLM 原创 |
| `app/schedule/tasks/daily_image.py` | 每日一图（cron 19:30）：资讯配图 → 本地 → CQ 图 + 来源链接 |
| `config/scenes/*.txt` | 4 个新场景模板：`history_review` / `news_comment` / `quote` / `image_caption` |

**关键设计**

- **零新依赖**：RSS 用标准库解析（轻量化红线）；只有 4 个新场景模板是纯文本；
- **破冰内容源改为轮换**（§4.5）：`make_rotating_source()` —— 话题包 → 群历史回顾 → 资讯 → 话题库 → 热搜兜底，
  **轮到的那种给不出内容就依次尝试后面的**（顺序本身就是优先级 + 兜底链）；
- **降级贯穿到底**：源全挂 / 没有新条目 / 取不到图 → `TaskSkip` → 本次不发（绝不发占位内容或退化纯文字）；
- **版权红线**（D19/A.6）：只发"标题 + 一句话点评 + 原文链接 + 来源"，**摘要只给 LLM 看、不进推送**；图片带来源；
- **效果统计**（P2c-8）：`metrics.record_proactive(gid, 任务名)` → `by_kind` 聚合 `sent/replied/reply_rate`，
  面板新增「🎯 哪类内容有人接」卡片。

**测试证据**

```
tests/test_p2c_news.py → 14/14 PASS
  RSS 解析（CDATA / 内嵌图 / thumb / 坏条目跳过 / 命名空间 / RDF / 非 XML 不崩）
  B站解析（正常 / code≠0 / 缺封面 / 缺 id / 毫秒时间戳换算）、时间解析 5 种形态
  指纹去重（来源敏感、落盘可被新实例读到）、每源冷却、**失败退避 ×2/×4/×8 封顶**、全挂→空
  图片下载（正常 / 非图片 / 超大 / 500 / 超时 / 非 http → None）、CQ 码格式
  话题库轮播（不重复 / 类型过滤 / 落盘）、每日一句（池子 + LLM 原创 + 失败回落）
  资讯任务（文案含标题+点评+来源+链接、**不含摘要/正文**、推过就跳过）
  每日一图（CQ 图 + 来源链接；取不到图 → 跳过）
  破冰轮换源（空的跳下一个）、效果统计 by_kind

tests/test_p2c_e2e.py → 8 项全过
  ① 注册 7 个任务；破冰内容源 = 轮换源
  ② 11:30 资讯：标题+点评+来源+链接（无正文）
  ③ 去重：第一天第 1 条 → 第二天第 2 条 → 第三天没新的就跳过
  ④ 12:30 每日一句（💬 池子文案）
  ⑤ 19:30 每日一图：本地 CQ 图 + 来源链接；取不到图 → 一条都不发（也不退化成纯文字）
  ⑥ 破冰轮换：连续三次用到不同内容源
  ⑦ 源全挂 → 资讯与每日一图都安静跳过
  ⑧ 效果统计 by_kind：daily_quote 1 条被接 1 次
全量 20/20 全绿（**连跑三遍**；P2c 干跑单独连跑 5 遍稳定）；数据目录审计干净
```

**本轮抓到的 4 个问题**

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `TopicPool(path=...)` 自定义路径时没建父目录 | 传了 path 的实例（台词池）**写盘全失败**，使用计数不生效 | `__init__` 里一律 `ensure_dir(path.parent)` |
| 2 | 测试隔离又漏了 3 个新落盘点（资讯状态 / 话题库 / 台词池） | 测试把"话题使用次数"写进真实 `data/cache`（审计抓到 3 个文件） | 统一 helper 里补齐这 3 个补丁；**这已是第三次同类问题**，以后新增任何落盘模块都先加进 helper |
| 3 | 干跑里 `_last_activity` 正好卡在 idle 阈值 30.0 分钟 | 浮点误差导致"这次破冰、下次不破冰" → 内容源轮换计数漂移，偶发失败 | 活跃时间给宽裕（2 小时前） |
| 4 | 用 `[CQ:image` 判定"每日一图" | 破冰消息会按概率**附一张收藏表情**（也是 CQ 图片）→ 误判成"占位图"，偶发失败 | 改用每日一图专属标记 `🖼`；轮换断言也排掉其它任务的消息 |

#### 6.2 发布清单（P2c，**与 P2a-hotfix / P2b 合并成一批**）

> 说明：P2a 的 3 个 hotfix、P2b、P2c 三批的改动**互相重叠**（都在 `handler.py`/`scheduler.py`/`config.py` 里），
> 所以这里合成一张总表，**一次传完**即可（25 个文件：13 新建 + 12 覆盖）。

**🆕 新建（13 个）**

| 服务器路径 | 自检（`grep -c` 期望值） |
|---|---|
| `app/news/__init__.py` | `NewsService` → 2 |
| `app/news/models.py` | `class NewsItem` → 1 |
| `app/news/sources.py` | `class BilibiliArticleSource` → 1 |
| `app/news/service.py` | `class NewsService` → 1 |
| `app/news/image.py` | `def download_image` → 1 |
| `app/context/topic_pool.py` | `class TopicPool` → 1 |
| `app/schedule/tasks/news_digest.py` | `class NewsDigestTask` → 1 |
| `app/schedule/tasks/daily_quote.py` | `class DailyQuoteTask` → 1 |
| `app/schedule/tasks/daily_image.py` | `class DailyImageTask` → 1 |
| `config/scenes/history_review.txt` | `不要 @ 任何人` → 1 |
| `config/scenes/news_comment.txt` | `一句` → 4 |
| `config/scenes/quote.txt` | `每日一句` → 1 |
| `config/scenes/image_caption.txt` | `配文` → 1 |

**✏️ 覆盖（11 个）**

| 服务器路径 | 内容（含哪几批的改动） | 自检 |
|---|---|---|
| `app/paths.py` | P2b `WEATHER_CACHE_DIR` + P2c `NEWS_*`/`QUOTES_STATE_PATH` | `grep -c NEWS_CACHE_DIR app/paths.py` → 1 |
| `app/config.py` | P2b `WEATHER_*`/`WATER_*` + P2c `NEWS_*`/`*_AT` | `grep -c NEWS_ENABLED app/config.py` → 1 |
| `app/metrics.py` | P2c `by_kind` + `record_proactive` | `grep -c by_kind app/metrics.py` → 7 |
| `app/schedule/task.py` | P2a-hotfix `sanitize_outgoing` + `refine_config` | `grep -c sanitize_outgoing app/schedule/task.py` → 2 |
| `app/schedule/scheduler.py` | P2a-hotfix `_manual_gate` + P2c `record_proactive` | `grep -c _manual_gate app/schedule/scheduler.py` → 2 |
| `app/schedule/tasks/__init__.py` | P2b/P2c 新任务导出 | `grep -c make_rotating_source app/schedule/tasks/__init__.py` → 2 |
| `app/schedule/tasks/icebreak.py` | P2c 轮换源 | `grep -c make_rotating_source app/schedule/tasks/icebreak.py` → 1 |
| `app/schedule/group_config.py` | P2b weather 段 + P2c 三个任务的 env 默认 | `grep -c news_digest app/schedule/group_config.py` → 2 |
| `app/bot/handler.py` | P2b `/天气` + P2c `/资讯`/轮换/服务 + P2a-hotfix `/任务 强制` | `grep -c _handle_news app/bot/handler.py` → 2 |
| `app/dashboard/server.py` | P2a-hotfix `/api/task` 的 `force` | `grep -c force app/dashboard/server.py` → 1 |
| `app/dashboard/index.html` | P2a 任务卡片 + P2c「哪类内容有人接」卡片 | `grep -c by_kind app/dashboard/index.html` → 1 |
| `config/scenes/icebreak.txt` | P2a-hotfix-2"不要 @ 任何人" | `grep -c "不要 @ 任何人" config/scenes/icebreak.txt` → 1 |

**服务器侧操作（2 条）**

```bash
# ① 【P2b 必需】给实例群绑城市，否则天气不播报
mkdir -p /root/qqbot/data/group_config
cat > /root/qqbot/data/group_config/100000004.json <<'EOF'
{
  "weather": { "city": "北京", "broadcast_at": "08:00" }
}
EOF

# ② 重启
systemctl restart qqbot
```

**部署后验收**

```bash
journalctl -u qqbot -n 40 --no-pager | grep "\[Schedule\]"
# 期望看到 7 个任务：icebreak / summary_job / weather_morning / water_reminder
#                  / news_digest / daily_quote / daily_image
```

QQ 里（被动命令，任何群可用）：

```
/天气             → 北京天气（实例群）
/资讯             → 一条最新游戏/动漫资讯（标题+点评+来源+链接）
/任务 试跑 news_digest   → 私聊看资讯文案
/任务 试跑 daily_quote   → 私聊看每日一句
/任务 试跑 daily_image   → 私聊看配图（真下载，带来源链接）
```

面板上会多出「🎯 哪类内容有人接」卡片（需要先有主动消息数据）。

**新增目录**（代码自动创建）：`data/news/`、`data/news_images/`

### 7. [P2c-hotfix-1] 配图下载被 403（**服务器实测发现，已修**）

**现象**（2026-10-08 01:43 服务器日志）：`/任务 试跑 daily_image` 时报

```
[News] 图片 HTTP 403: https://image...
```

**根因**：`download_image()` 把 `Referer` **硬编码成 `https://www.bilibili.com/`** ——
抓机核图床（`image.gcores.com`）时属于"外站 Referer"，被防盗链挡下。

**修法（三层）**：

1. 默认 Referer 改为**图片自己所在站点的根**（`origin_of(url)`）；
2. `DailyImageTask` 优先用**文章页所在站点**当 Referer（图床一般校验自家站点）；
3. 若仍 401/403 → **去掉 Referer 再试一次**（不少 CDN 只要不是外站 referer 就放行）。

**顺带**：`DailyImageTask` 默认不再包一个 `lambda url: download_image(url)`
（那样会绕过带 Referer 的逻辑），改为 `_fetcher=None` 时走内置下载器。

**测试**：`test_download_image` 扩展 —— 默认 Referer 取图片站点、自定义 Referer 优先、
**403 → 去 Referer 重试成功**、403 两次 → None。全量 20/20 连跑三遍全绿。

**本批发布补充**：只需替换 `app/news/image.py` 与 `app/schedule/tasks/daily_image.py` 两个文件
（如果你已经传过 P2c 那批；没传就一起传）。自检：

```bash
grep -c origin_of app/news/image.py                 # 期望 2
grep -c referer app/schedule/tasks/daily_image.py   # 期望 4
```

**服务器实测的额外好消息**：5 个资讯源在服务器上**全部可用**且解析正常 ——
机核 20 条 / 游研社 12 条 / Gal周报 12 条 / MoeSekai情报局 7 条 / KALPA官方 也有内容；
7 个任务全部注册成功、调度器已启动。

### 8. [P2c-hotfix-2] 重启后"没有带图的新资讯可发"（**架构性缺陷，已修**）

**现象**（替换 hotfix-1 的文件、重启后重新试跑）：

```
🧪 试跑 daily_image
⏭ 本次跳过：没有带图的新资讯可发（取不到就跳过，不发占位图）
```

而替换之前那次日志是 `[News] 图片 HTTP 403` —— 说明**原来是能选到条目的**，重启后反而"连条目都没有了"。

**根因**（两条线索串起来才看出来）：

| 状态 | 存在哪 | 重启后 |
|---|---|---|
| 源的冷却/退避/ETag（`last_fetch`） | **落盘** `data/news/state.json` | ✅ 还在 → 45 分钟内不再拉 |
| 拉到的条目（`_items`） | **只在内存** | ❌ 清空 |

→ 重启后 `refresh()` 因为冷却**一个源都不拉**，手里 0 条 → 任务只能报"没有可发"，
一直空转到冷却结束。**"持久化了一半状态"造成的死锁**，只有真机重启才会暴露。

**修法（两条一起上）**：

1. **条目也落盘**（`state.json` 存最近 100 条），重启后立刻可用；
2. **手里一条内容都没有时，冷却不该拦住取数**（`fresh_start` 判定）—— 有内容时才严格冷却；
   这样即使 state 文件被删/损坏也不会空转。

**顺带加诊断**：`NewsService.diagnose()` 输出
"缓存 N 条 / 带图 M 条 / 未推送 K 条 / 可用 X 条（含因超过 48h 被排除的数量）"，
并接进 `news_digest` / `daily_image` 的跳过原因 —— **下次一眼看出卡在哪一环**，不用再写脚本查。

**一个必须记录的教训**：我一开始**猜**是"协议相对 URL（`//host/x.jpg`）被 `has_image` 排除"，
差点就这么交出去。**先拉真实源验证**之后发现：63 条**全部都有图**，且全是绝对 https 地址 ——
假设是错的。协议相对补全本身值得做（已加 + 单测），但它**不是**这次的原因。
**教训：猜出来的根因必须先拿真实数据证伪，再动手。**

**真机验证（本地跑真实源 + 真实图床）**：

| 源 | Referer | 结果 |
|---|---|---|
| 机核 | `https://www.gcores.com/` | ✅ 51KB（**正是线上 403 的那张**） |
| 游研社 | `https://www.yystv.cn/` | ✅ 150KB |
| B站 Gal周报 | `https://www.bilibili.com/` | ✅ 3.7MB |
| B站 MoeSekai | `https://www.bilibili.com/` | ✅ 2.5MB |
| B站 KALPA | `https://www.bilibili.com/` | ✅ 709KB |

**测试**：新增 `test_items_persist_and_cooldown_bypass`（重启后能从磁盘恢复条目、
无内容时绕过冷却、有内容时冷却仍生效）；`test_parse_rss` 增加协议相对 URL 补全断言。
全量 20/20 连跑三遍全绿，数据目录干净。

**本批要替换的 6 个文件**（hotfix-1 + hotfix-2 合并）：

| 服务器路径 | 自检（`grep -c`） |
|---|---|
| `app/news/image.py` | `origin_of` → 2 |
| `app/news/service.py` | `fresh_start` → 3；`def diagnose` → 1 |
| `app/news/models.py` | `https://` → 1 |
| `app/news/sources.py` | `_abs_url` → 3 |
| `app/schedule/tasks/daily_image.py` | `referer` → 4；`diagnose` → 1 |
| `app/schedule/tasks/news_digest.py` | `diagnose` → 1 |

**替换后建议清一次资讯状态**（否则那 45 分钟冷却还在，得干等）：

```bash
systemctl stop qqbot
rm -f /root/qqbot/data/news/state.json     # 只删状态文件，不丢任何原始数据
systemctl start qqbot
# 然后 QQ 里：/任务 试跑 daily_image   → 这次应该能收到真图
```

**✅ 2026-10-08 用户实测通过**：`/任务 试跑 daily_image` 收到真图（机核《恸哭幽光》制作人专访），
配文"背负着罪也要往前冲，这才是少女的浪漫啊。"、`来源：机核`、原文链接 —— **四件齐全**。

### 9. [P2c-hotfix-3] 预留带图条目给「每日一图」（**观察成功案例后主动修**）

看到上面那张成功截图后，意识到一个**还没爆但迟早会爆**的设计冲突：

`pick_for_digest()` 原来**优先挑带图条目**（当时的理由是"图文效果更好"），
但资讯任务与破冰资讯源**只发文字**（标题 + 点评 + 链接）——**根本不发那张图**。
于是它们白吃掉带图条目，把 19:30 的每日一图逼到"没图可发"（正是刚修完的那个报错形态）。

**修法**：`pick_for_digest()` 改为**优先挑不带图的条目**，把带图的留给每日一图；
实在只剩带图条目时也会用（不会因此空手）。

**测试**：新增 `test_image_items_reserved_for_daily_image` ——
"带图的更新、不带图的更旧"时，资讯任务要选**不带图**那条；每日一图仍拿得到带图那条；
只剩带图条目时资讯任务不空手。全量 20/20 连跑两遍全绿。

**因此 `app/news/service.py` 的最终自检值**：`fresh_start` → 3、`plain = ` → 1、`def diagnose` → 1。

---

### 10. [P2c-hotfix-4] 事故：任务失败后每 45 秒重试一次，猛打 LLM（**线上事故，已修**）

**现象**（2026-10-08 10:17~10:24 服务器日志）：`[Summary] LLM 调用失败: LLM API 超时` 每 **45 秒**一条，
连续十几次不停；同时 `[Casual] LLM 失败` 也开始出现。

**关键推理**：45 秒 = **调度 tick 30 秒 + LLM 超时 15 秒** —— 这个固定周期说明
**任务失败后没有退避，每个 tick 都在重试**。

**根因**（两处叠加）：

| # | 问题 | 说明 |
|---|---|---|
| 1 | `state.record_error()` **不推进 `last_run`** | interval/cron 触发器只看 `last_run`，失败不写 → 任务永远"到点了" → 每 tick 重试 |
| 2 | 摘要的 LLM 超时用了全局 15 秒 | 摘要一次要喂 40+ 条消息，比闲聊大得多 → **稳定超时** → 触发问题 1 的死循环 |

第 2 点还形成恶性循环：每 45 秒一个大请求打向 LLM → 把自己打成被限流 → 闲聊也开始超时。

**修法（三处）**：

1. `record_error(..., now=)` **推进 `last_run`** 并累加 `fail_count`；`record_run` 清零 `fail_count`；
2. 调度器新增**失败指数退避**：`max(TASK_FAIL_BACKOFF_BASE_MINUTES=10, 任务冷却/间隔) × 2^(连续失败-1)`，
   上限 `TASK_FAIL_BACKOFF_MAX_MINUTES=60` 分钟 —— 失败 1 次等 10 分、2 次 20 分、3 次 40 分…
   被拦时状态里写明"上次失败，退避中（连续 N 次，还需 M 分钟）"；
3. LLM 支持**每次调用单独指定超时**（`chat(..., timeout=)`），摘要用 `SUMMARY_LLM_TIMEOUT=45` 秒。

**立即止血（不等部署也能做）**：在群配置里临时关掉摘要任务（无需改代码）：

```bash
cat > /root/qqbot/data/group_config/100000004.json <<'EOF'
{
  "weather": { "city": "北京", "broadcast_at": "08:00" },
  "tasks": { "summary_job": { "enabled": false } }
}
EOF
systemctl restart qqbot
```

（修好后把 `"summary_job": {"enabled": false}` 这段删掉、再重启即可恢复。）

**测试**：新增 `tests/test_task_failure_backoff.py`（5 项）——
失败推进 `last_run`、失败后连续 5 个 tick 都不重试、退避指数放大、上限 60 分钟、
LLM 每次调用可传超时、摘要用放宽后的超时。全量 **21/21** 连跑两遍全绿。

**本批要替换的 5 个文件**（在 §8 那 6 个之外，另有重叠）：

| 服务器路径 | 自检（`grep -c`） |
|---|---|
| `app/schedule/state.py` | `fail_count` → 3 |
| `app/schedule/scheduler.py` | `TASK_FAIL_BACKOFF_MAX_MINUTES` → 1 |
| `app/config.py` | `SUMMARY_LLM_TIMEOUT` → 1 |
| `app/llm/provider.py` | `eff_timeout` → 3 |
| `app/context/summary.py` | `SUMMARY_LLM_TIMEOUT` → 1 |
