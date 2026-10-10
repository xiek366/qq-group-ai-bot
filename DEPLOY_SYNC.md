# 本地 → 云端 同步与发布说明（DEPLOY_SYNC）

> 适用场景：本地 Windows 工作区 `C:\Users\93046\Desktop\work\qq_bot` → 阿里云 `/root/qqbot`
> 配套文档：[DEPLOY.md](DEPLOY.md)（服务状态/日志排障/踩坑红线）、[DEVLOG.md](DEVLOG.md)（每次改动的开发记录）
>
> **核心原则一句话**：**代码以本地为准，数据以服务器为准，配置各管各的。**

---

## 0. 先回答"源码要不要为云端改"

**不需要。** 现在的代码已经做成"环境无关"：

| 曾经会出问题的地方 | 现在的做法 |
|---|---|
| 数据路径 | 全部由 `app/paths.py` 从 `__file__` 推导（绝对路径），**不看进程工作目录**，systemd 少写 `WorkingDirectory` 也不会写错地方 |
| 时区 | 破冰时间窗/报时/日志时间戳统一走 `Config.TIMEZONE`（默认 `Asia/Shanghai`），不看服务器 TZ |
| 面板监听 | 默认 `127.0.0.1`，云端走 SSH 隧道，不暴露公网 |
| 新目录 | `data/stream`、`data/cache/vision`、`data/stats` 都由代码**自动创建**，不需要手工建 |
| 依赖 | `requirements.txt` 没变（httpx / websockets / python-dotenv / aiohttp），服务器上已装 |

所以同步这件事只有三件事要做：**别覆盖服务器上的 `.env` 和 `data/`**、**首次发布跑一次旧记忆迁移**、**每次发布跑一遍验证清单**。

---

## 1. 三类文件（先记住这个分类，比记命令重要）

| 类别 | 内容 | 方向 |
|---|---|---|
| **A 代码与内容** | `app/`、`config/`、`tests/`、`requirements.txt`、`.env.example`、`*.md` | 本地 → 云端，每次发布同步 |
| **B 服务器独占** | `.env`（真 key/真 token）、`data/`（stream / memory / group_style / stickers / aliases / cache / stats / images）、`/root/Napcat/**` | **绝不同步**，只在服务器上存在 |
| **C 本地工具与垃圾** | `_*.py` / `_*.bat`（**`_test_token.py` 里有明文 token**）、`bot_*.log`、`__pycache__/`、`.vscode/`、`.tmp_pip/`、`.docx` | 传过去只有坏处，排除 |

> 一句话记忆：**`app config tests` + 几个 md 传上去；`.env`、`data`、`_` 开头的东西留在这边。**

---

## 2. 同步清单（逐项）

| 路径 | 传？ | 说明 |
|---|---|---|
| `app/**` | ✅ | 所有业务代码（含 `context/`、`vision/`、`metrics.py`、`dashboard/health.py`） |
| `config/**` | ✅ | `persona.txt`、`face_dict.json`、`aliases.json`。**注意**：若你在服务器上改过 persona，先按第 3.2 步把它拉回来，否则会被本地覆盖 |
| `tests/**` | ✅ | 服务器上也可以跑（`python tests/test_p0_e2e.py`），方便验证 |
| `requirements.txt` | ✅ | 依赖清单 |
| `.env.example` | ✅ | 配置模板（新增项会同步过去，便于你对照补 `.env`） |
| `*.md`（DEPLOY / DEVLOG / ROADMAP / DEPLOY_SYNC） | ✅ | 文档 |
| `.env` | ❌ | 服务器上是**生产配置**（真 API Key、真 token、面板 token）。本地那份是开发用的 |
| `data/**` | ❌ | 服务器的运行时数据：对话流、旧记忆、画像、表情包库、外号表、缓存、统计。**覆盖它等于清空 bot 的记忆** |
| `data/stickers/**` | ❌ | 79 张群友表情（版权+体积），只在服务器 |
| `_*.py` / `_*.bat` | ❌ | Windows 排查脚本；`_test_token.py` 含明文 token |
| `bot_stdout.log` / `bot_stderr.log` | ❌ | 本地运行残留 |
| `__pycache__/`、`.vscode/`、`.tmp_pip/` | ❌ | 无意义或环境相关 |
| `QQ群 AI 聊天机器人 MVP 开发任务书.docx` | ⚪ | 传不传都行（建议留在本地） |

---

## 3. 标准发布流程

> **默认走「方式 B：逐文件替换」**（2026-10-07 用户确认：备份/rsync 那套他做不了）。
> 方式 A（rsync 全量同步）保留在 3.A，供以后有条件时使用。

### 3.B 【默认】逐文件替换

我（开发侧）每次发布都会给出这样一张**文件替换清单**，你照着替换即可：

| 服务器路径 | 操作 | 本地来源 | 必须? | 替换后自检命令（期望值） |
|---|---|---|---|---|
| `/root/qqbot/app/config.py` | 覆盖 | 同路径 | ✅ | `grep -c PROACTIVE_GROUPS app/config.py` → `2` |

清单里的每一行都要能"一眼验证"：给出一条 `grep -c` 命令和期望数字，替换完你自己就能确认是否传对了。

**你的操作步骤（3 步）**

1. **替换文件**：用你最顺手的方式（VSCode 远程打开 `/root/qqbot/<路径>` → 用本地文件内容整体覆盖；或 WinSCP/拖拽覆盖）。
   清单标 🆕 的是**新文件**，需要在服务器上新建（连同目录）。
2. **跑自检命令**：清单里每条 `grep` 的返回值要和期望一致；不一致说明文件没传全（常见原因：编辑器把它存成了别的编码，或者只粘了一半）。
3. **重启 + 看日志**：`systemctl restart qqbot && journalctl -u qqbot -n 40 --no-pager`
   （⚠️ 只重启 qqbot，绝不动 napcat）

**红线（每次都不能碰）**

- ❌ 不要替换 `/root/qqbot/.env`（服务器上的真 key/token 只属于服务器；需要改时我会单独写"服务器侧操作"给你一条命令）
- ❌ 不要碰 `/root/qqbot/data/**`（对话流、记忆、画像、表情库、缓存、统计都在这儿）
- ❌ 不要传 `_*.py` / `_*.bat`（本地排查脚本，其中一个含明文 token）

**关于"删除/改名"**：逐文件替换没有"删除"能力。如果某次发布需要删掉或改名服务器上的文件，
我会在清单下面单独列一节「需要手动删除」，不会漏掉。

---

### 3.A 【备选】rsync 全量同步（需要本机有 rsync + SSH）

#### 3.1 准备（本地）

```powershell
# ① 自检：语法 + 全量测试（必须全绿才发布）
& "D:\anaconda3\envs\qqbot\python.exe" -m compileall -q app tests
foreach ($t in Get-ChildItem tests\test_*.py) { & "D:\anaconda3\envs\qqbot\python.exe" $t.FullName *> $null; "$($t.Name) -> exit=$LASTEXITCODE" }
```

```powershell
# ② 差异预览（dry-run）：先看清楚"哪些文件会被传上去"
#    -n = 只演练不真传；-i = 逐项打印
rsync -avni `
  --exclude '.env' --exclude 'data/' --exclude '__pycache__/' `
  --exclude '_*.py' --exclude '_*.bat' --exclude '*.log' `
  --exclude '.vscode/' --exclude '.tmp_pip/' --exclude 'tests/data/' `
  --exclude '*.docx' `
  ./ root@<你的IP>:/root/qqbot/
```

看输出（`-i` 的标记含义）：

| 标记 | 含义 |
|---|---|
| `>f+++++++++` | 云端新增文件 |
| `>f.st......` | 内容有更新（`s`=大小变、`t`=时间变） |
| `.d..t......` | 只有目录时间变（无害） |
| `*deleting` | 只有加了 `--delete` 才会出现（见 3.5 的警告） |

> **看到 `config/persona.txt` 要上传、但你这次没改过它** → 说明服务器上那份被改过，先拉回来：
> `rsync -avz root@<IP>:/root/qqbot/config/persona.txt ./config/`

### 3.2 服务器备份（方式 A 用；方式 B 可跳过）

```bash
TS=$(date +%F_%H%M)
mkdir -p /root/backups
tar czf /root/backups/qqbot_$TS.tar.gz -C /root qqbot \
  --exclude='qqbot/data/stickers' --exclude='qqbot/__pycache__'
ls -lh /root/backups/qqbot_$TS.tar.gz     # 记下这个文件名，回滚时用
```

（排除了表情包图片，只备份代码+数据索引；需要连图一起备份时去掉 `--exclude`。）

### 3.3 正式同步

把 3.1 的命令去掉 `-n`：

```bash
rsync -avz \
  --exclude '.env' --exclude 'data/' --exclude '__pycache__/' \
  --exclude '_*.py' --exclude '_*.bat' --exclude '*.log' \
  --exclude '.vscode/' --exclude '.tmp_pip/' --exclude 'tests/data/' \
  --exclude '*.docx' \
  ./ root@<你的IP>:/root/qqbot/
```

### 3.4 重启 + 看日志

```bash
systemctl restart qqbot          # ⚠️ 只重启 qqbot，绝不动 napcat（动它要重新扫码）
journalctl -u qqbot -n 60 --no-pager
```

### 3.5 验证 + 回填记录

按第 5 节跑一遍验证清单，然后把结果填进第 8 节的发布记录表。

> **关于 `--delete`**：默认**不要加**。它会把云端"本地没有的文件"删掉（比如我删掉某个模块时很有用），
> 但配合错 excludes 就可能删掉你不想要的东西，而且被 `--exclude` 保护的文件默认不受影响、
> 加了 `--delete-excluded` 就会连它们一起删。要用就先 `-n` 演练，确认输出里没有意外删除项。

---

## 4. 涉及数据结构变更时（以 P0 对话流为例）

**判断标准**：改动是否会让**已有数据被读/写的方式变化**？P0 就属于这种——记忆从
`data/memory/<群ID>.json` 改成 append-only 的 `data/stream/<群ID>.jsonl`。

**代码侧的行为（已实现，无需你操作）**：首次启动时自动迁移，并把旧文件改名留底：

```
[Init] 旧记忆迁移：N 个群（其余已迁移或无旧数据）
[Stream] 群 100000004 旧记忆已迁移 42 条 → 100000004.jsonl
```

**发布前必做**：第 3.2 步的 `tar` 备份**必须包含 `data/`**（上面的命令已包含）。

**验证迁移成功**：

```bash
ls -l /root/qqbot/data/stream/             # 出现 <群ID>.jsonl
ls -l /root/qqbot/data/memory/             # 旧文件变成 <群ID>.json.migrated
wc -l /root/qqbot/data/stream/*.jsonl      # 行数应与你迁移前的记忆条数对得上
```

**回滚**（详见 DEVLOG §6.2）：

```bash
systemctl stop qqbot
cd /root/qqbot/data
mkdir -p _stream_backup && mv stream/*.jsonl _stream_backup/     # 挪走新对话流
cd memory && for f in *.json.migrated; do mv "$f" "${f%.migrated}"; done   # 旧文件改回来
systemctl start qqbot
```

---

## 5. 验证清单（每次发布都跑，5 分钟）

| # | 检查 | 命令 | 期望 |
|---|---|---|---|
| 1 | 服务在跑 | `systemctl status qqbot --no-pager \| head -5` | `active (running)` |
| 2 | WS 连上 NapCat | `journalctl -u qqbot -n 80 --no-pager \| grep "WebSocket"` | `✅ WebSocket 已连接` |
| 3 | 面板起来 | `... \| grep "Dashboard"` | `管理面板已启动: http://127.0.0.1:8080` |
| 4 | **数据路径正确** | `journalctl -u qqbot -n 80 --no-pager \| grep -E "cwd|project|data \|timezone"` | `data: /root/qqbot/data`、`timezone: Asia/Shanghai` |
| 5 | 健康卡接口 | `curl -s "http://127.0.0.1:8080/api/health?token=$DASHBOARD_TOKEN" \| head -c 300` | JSON 含 `metrics` / `streams` / `stream_total_bytes`（token 错应 401）；**每群卡片里应能看到有对话流的群**（哪怕它的指标还是 0） |
| 5b | 主动打扰范围 | `journalctl -u qqbot -n 80 --no-pager \| grep "主动打扰范围"` | `[Init] 主动打扰范围: 100000004（其余群只做被动响应…）`；若出现 WARNING 说明 `.env` 没配 `PROACTIVE_GROUPS` |
| 6 | 对话流在工作 | `ls -l /root/qqbot/data/stream/` | 有 `<群ID>.jsonl` 且在增长 |
| 7 | 看错误 | `journalctl -u qqbot -p err --no-pager \| tail -20` | 没有新的异常堆栈 |
| 8 | 群内体感（测试群 100000005） | 发普通消息 / @它一句 / 发一张图 | 它会回；日志出现 `[Vision] 发起识别`，同图再发出现 `缓存命中` |

> 面板 token 取法：`grep -oP '(?<=^DASHBOARD_TOKEN=).*' /root/qqbot/.env`

---

## 6. 回滚

**方式 B（逐文件替换）的轻量备份**：替换前先复制一份代码目录，出问题一条命令还原：

```bash
cd /root/qqbot
cp -a app app.bak-$(date +%m%d_%H%M)      # 只备份代码目录，很小、很快
# 回滚：
rm -rf app && mv app.bak-<刚才的时间戳> app && systemctl restart qqbot
```

| 粒度 | 做法 |
|---|---|
| **整体回退到发布前** | `systemctl stop qqbot && tar xzf /root/backups/qqbot_<时间戳>.tar.gz -C /root && systemctl start qqbot` |
| **只回退代码（方式 B）** | 上面的 `cp -a` / `mv` 两行 |
| **只回退数据布局** | 第 4 节末尾的"回滚"命令 |
| **只回退某几个文件** | 用备份包里的路径覆盖：`tar xzf .../qqbot_<TS>.tar.gz -C /root qqbot/app/bot/handler.py` |

---

## 7. 常见坑（踩过或差点踩）

1. **`.env` 被覆盖**：服务器上的 Key/token 会被本地开发值冲掉 → 同步命令里 `--exclude '.env'` 是硬性要求。
2. **`data/` 被覆盖 = 记忆清空**：对话流、画像、外号、表情索引全没了 → 同理必须排除。
3. **`_test_token.py` 里有明文 OneBot token**：绝对不能传上服务器（更不能进任何仓库）。
4. **`--delete` 用错**：见 3.5 的警告，先演练再决定。
5. **Windows 没有 rsync**：三选一 —— ① Git for Windows 自带的 Git Bash 里跑上面的命令；② WSL；③ 退化为 `scp`/`tar` 或 VSCode 的 SFTP 拖拽（三种方式都要遵守第 2 节的清单）。
6. **`--exclude 'data/'` 的写法**：带斜杠表示只排除根下的 `data/` 目录，够用；不要写成 `--exclude 'data'`（会连带排掉任何同名路径）。
7. **权限**：`/root/qqbot/.env` 建议 `chmod 600`；整个项目属主是 root（systemd 以 root 跑）。
8. **编码/换行**：Python 与 JSON 文件保持 UTF-8（不带 BOM），不要用记事本另存为 ANSI，否则中文日志/人设会乱码。
9. **重启范围**：只 `systemctl restart qqbot`；**不要** `restart napcat`（QQ 登录态会丢，得重新扫码）。
10. **首次发布后**：确认 `data/stream/` 有文件、`data/memory/` 出现 `.json.migrated`，否则说明迁移没跑到（看第 4 节日志）。
11. **`.env` 别带 BOM**：systemd 的 `EnvironmentFile` 会把带 BOM 的首行当成"无效的环境变量赋值"，每次重启刷一条
    `Ignoring invalid environment assignment '...'`（无害，但值得清掉）。诊断与修复：

    ```bash
    file /root/qqbot/.env                     # 期望 "UTF-8 Unicode text"，不是 "...with BOM"
    hexdump -C /root/qqbot/.env | head -1     # 开头应是 23 20（"# "）；出现 ef bb bf 就是 BOM
    sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' /root/qqbot/.env   # 去 BOM + 去 CRLF
    ```
12. **`SEARXNG_URL` 留空是允许的**：搜索会走代码内置的公共实例列表兜底；若 `/搜` 频繁失败，
    在 `.env` 里填一个稳定的实例地址（自建更佳）。

---

## 8. 发布记录表（每次发布回填一行）

| 日期 | 内容（DEVLOG 章节 / 一句话） | 是否涉及数据迁移 | 备份文件 | 验证结果 | 备注 |
|---|---|---|---|---|---|
| 2026-10-07 | P0 全量：统一对话流、视觉管道、指标与健康卡（DEVLOG §6） | ✅ 是（memory → stream，三个群全部迁移完成：23/20/27 条） | 未做（逐文件替换方式） | ✅ **全部确认**：`/api/health` 200、错 token 401、三群对话流齐全、**面板三张卡（今日概览 / LLM·视觉健康度 / 每群被回应率）用户目视确认通过**（2026-10-07 深夜） | 首次发布 |
| 2026-10-07 | P0-hotfix-1：日志 `Logging error`（DEVLOG §[P0-hotfix-1]） | ❌ 否 | 未做 | ✅ 已通过（21:01 日志：时间戳正常、无 Logging error） | 服务器 Python 3.10.12 |
| 2026-10-07 | P0-hotfix-2：主动打扰范围（D15）+ 两处指标修正（DEVLOG §[P0-hotfix-2]） | ❌ 否 | 未做 | ✅ **全部确认**：5 个文件自检全中；`主动打扰范围: 100000004` 生效；测试群裸图被正确拦下；同图缓存命中已在生产验证；三群对话流 23/20/27 条；面板健康卡已目视确认 | — |
| 2026-10-07 | 配置调整：轮换 `LLM_API_KEY`（两次，第一次 key 又贴到会话里了）、`CHAT_KEYWORDS=爱丽丝`、`DASHBOARD_TOKEN` 换 16 位随机串、`LLM_THINKING=disabled`、删除废弃的 `SEARCH_*` | ❌ 否（纯 `.env`） | `.env.bak-1007` | ✅ 已通过：横幅显示新 token、`[LLM]` 调用成功（`tokens_in=4436/tokens_out=49`，1.07s）、`主动打扰范围: 100000004` | 改密钥务必用 `read -s` 写法（见 §7-14） |
| 2026-10-07 | **P1 记忆分层**：滚动摘要 + 用户画像卡 + 话题状态 + 上下文预算组装器 + 人设拆分（DEVLOG §[P1-0] 6.1/6.2） | ❌ 否（`data/summary/`、`data/state/` 代码自动创建） | 未做 | ✅ **已发布并实测**：9 个必需文件自检全中；横幅 `Persona: persona_core.txt / 467 字常驻 / 场景 5 个`；真实聊天 `[Context] group=100000004 人设=425 时间=23 开发者=141 称呼=79 场景=123 窗口=25条/1766字 合计=2557/6000`；`tokens_in=2921`（P0 同场景约 4400）；窗口 25 条说明 `MAX_HISTORY=20` 已生效 | `[Summary]`/`[Topic]` 要攒够 40 条才出现，验证法见 §7-16 |
| 2026-10-07 | **P1 修订-1**：人设补 markdown 禁令 + "还记得吗"场景规则（`persona_core.txt`、`scenes/chat.txt`）<br>**P1 修订-2**：摘要触发"饿死"修复（`summary.py` 的 `prime()` + `handler.py` 统计积压） | ❌ 否 | 未做 | ✅ **已发布并实测**：`[Summary] 群 100000004 历史积压 46 条未摘要，已计入触发计数` → `摘要完成：入参 46 条 → 要点 5 条、画像 4 人`；`人设=517 场景=194` 说明两个配置文件都已替换 | 话题='无' 属正常（零散闲聊不写话题） |
| 2026-10-07 | **P1 修订-4**：`config/persona_core.txt` 新增"不要主动抱怨技术问题"（591→718 字）；`app/bot/handler.py` 的 `[DEBUG] segments detail` 降为 DEBUG 级 | ❌ 否 | 未做 | ⏳ 人设文件待替换（1 个）；**handler.py 的日志降噪已随 P2a 批次发出** | 起因：回复里出现"发的图爱丽丝这边还是没收到呀" |
| 2026-10-08 | **P2a 调度框架**：scheduler/Task/ContentSource 三层解耦、任务状态与打扰预算（每群每日 ≤8 + 静默 23:00-08:00）、破冰与摘要迁入、`/任务 列表丨试跑丨执行`（私聊管理员）、面板任务卡片与按钮（DEVLOG §[P2a-0] 6.1/6.2） | ❌ 否（新增 `data/schedule`、`data/group_config`、`data/tasks`，代码自动创建） | 未做 | ✅ 已发布：17 个文件自检全中；`[Schedule] 调度器已启动：2 个任务 / tick=30s / 静默 23:00-08:00 / 每群每日上限 8 条`；`/任务 列表`、`试跑`、`执行` 私聊均正常 | 见下一行：手动执行曾绕过 D15（用户抓到） |
| 2026-10-08 | **P2a-hotfix-1/2/3**：手动 `/任务 执行` 绕过 D15 把破冰发进测试群 → `_manual_gate()`（D15 硬拦、静默需「强制」）；模型会 @全体成员 / @自己（含简称 `@爱丽丝`）→ `sanitize_outgoing()` 清洗 + `_send_group()` 出站兜底 | ❌ 否 | 未做 | ✅ hotfix-1 已验证（测试群被硬拦）；✅ hotfix-3 本地用真实配置复现通过；⏳ 5 个文件待替换（`app/schedule/scheduler.py`、`app/schedule/task.py`、`app/bot/handler.py`、`app/dashboard/server.py`、`config/scenes/icebreak.txt`） | 清单见 DEVLOG `[P2a-hotfix-1]` §6~§8 |
| 2026-10-08 | **P2b 天气播报 + 喝水提醒**：`WeatherService`（Open-Meteo / 4 预置城市 / 缓存 / 规则引擎 / 降级）+ `weather_morning`（cron 08:00）+ `water_reminder`（75 分 / 10:00–18:00 / ≤4 / 群活跃前置）+ `/天气` 命令（DEVLOG §[P2b-0] 6.1/6.2） | ❌ 否（新增 `data/cache/weather`） | 未做 | ⏳ 与 P2c 合并发布（本地 18 个文件全绿、P2b 干跑 8/8） | 见下一行：已合并成一批 |
| 2026-10-08 | **P2c 资讯生态**：RSS（机核/游研社）+ B站专栏（Gal周报/MoeSekai情报局/KALPA官方）、资讯→标题+点评+来源链接、每日一句（12:30）、每日一图（19:30，检索配图带来源）、话题库（30 条）+台词池（20 条）轮播、破冰内容源轮换、效果统计「哪类内容有人接」（面板卡片）（DEVLOG §[P2c-0] 6.1/6.2） | ❌ 否（新增 `data/news`、`data/news_images`，代码自动创建） | 未做 | ✅ **已部署**：7 个任务全部注册、调度器已启动；**5 个资讯源在服务器上全部可用**（机核 20 条 / 游研社 12 条 / Gal周报 12 条 / MoeSekai 7 条 / KALPA 有内容）；`/任务 试跑 news_digest` 正常出文案 | **与 P2a-hotfix / P2b 合并成一批：25 个文件**（13 新建 + 12 覆盖）+ 1 条服务器侧操作（给实例群绑城市），清单见 DEVLOG `[P2c-0]` 6.2 |
| 2026-10-08 | **P2c-hotfix-1**（服务器实测发现）：配图下载把 Referer 硬编码成 B站 → 抓机核图床被 **403 防盗链**。修：默认 Referer 取图片/文章页自己的站点 + 失败时去 Referer 重试<br>**P2c-hotfix-2**（用户实测发现）：资讯条目只在内存、冷却却落盘 → **重启后连条目都没有**，任务一直报"没有带图的新资讯"；修：条目落盘 + 无内容时绕过冷却 + 跳过原因带诊断数字<br>**P2c-hotfix-3**：资讯任务只发文字却优先吃掉带图条目 → 改为优先取不带图条目，把图留给每日一图 | ❌ 否（建议删一次 `data/news/state.json` 免等冷却） | 未做 | ✅ **hotfix-1/2/3 已在服务器验证**：`/任务 试跑 daily_image` 收到真图 + 配文 + `来源：机核` + 原文链接；**5 个源真实图床全部下载成功**（机核 51KB / 游研社 150KB / B站 3.7MB·2.5MB·709KB） | 清单见 DEVLOG `[P2c-0]` §7/§8/§9（6 个文件） |
| 2026-10-08 | **P2c-hotfix-4**（**线上事故**）：LLM 超时后 `record_error` 不推进 `last_run` → 任务**每 45 秒重试一次**（tick 30s + 超时 15s）连续猛打 LLM；摘要大请求用全局 15 秒超时稳定超时，形成恶性循环。修：失败推进 `last_run` + 指数退避（10→20→40…上限 60 分）+ 摘要超时单独放宽到 45 秒 | ❌ 否 | 未做 | ⏳ 待替换 5 个文件（`schedule/state.py`、`schedule/scheduler.py`、`config.py`、`llm/provider.py`、`context/summary.py`）；新增 5 项回归测试 | 止血法见 DEVLOG `[P2c-0]` §10（群配置里临时关 summary_job） |

> 未验证完的那次发布**不要删备份**。

---

## 9. 后续开发也这么做（我这边执行的约定）

1. **每次改完代码**，我都在 [DEVLOG.md](DEVLOG.md) 里写清：改了哪些文件、**是否涉及数据结构变化**、
   迁移与**回滚**步骤、测试与验证结果。
2. **每次发布都以「文件替换清单」交付**（2026-10-07 起，用户确认走方式 B）：一张表，包含
   **服务器路径 / 操作（🆕新建 or 覆盖）/ 本地来源 / 替换后一条自检命令与期望值**；
   不要求你做备份、rsync 或任何同步工具的操作。
3. **需要"删除/改名"服务器文件时**（逐文件替换做不到），我单独列一节「需要手动删除」，并说明原因。
4. **需要你在服务器上手工做别的**（改 `.env`、装依赖、加 systemd 配置）时，单独列「服务器侧操作」，
   给成可直接复制的单行命令。
5. **涉及新配置项**时同步更新 `.env.example`；涉及数据/日志布局变化时同步更新 [DEPLOY.md](DEPLOY.md)
   的路径表与关键词表。
6. **每次发布后**请把结果填进第 8 节的发布记录表（只需一行）。

---

## 10. `.env` 配置清单（服务器上手工维护，**永不随代码同步**）

> 代码实际读取 **47 个**环境变量，其余写了也没用。下面按"要不要动"分类，避免瞎改。
> 改完一律 `systemctl restart qqbot` 生效。

### 必须配（缺了会出问题）

| 键 | 说明 |
|---|---|
| `ONEBOT_WS_URL` | 默认 `ws://127.0.0.1:3001`；WS 端 |
| `ONEBOT_ACCESS_TOKEN` | 必须与 NapCat 配置一致（三处一致的红线见 DEPLOY.md §八） |
| `LLM_API_KEY` | DeepSeek Key。**只放在服务器上，绝不贴到聊天/截图/工单里** |
| `LLM_MODEL` | 需要看图就得是带视觉的模型（如 `deepseek-flash`） |
| `BOT_QQ` | 判断"是否 @ 机器人"；填错会导致它不认 @ |
| `PROACTIVE_GROUPS` | **主动打扰白名单**（破冰 / 插嘴 / 裸图自动回复）。留空 = 所有群都被打扰 |

### 建议配（影响体感/安全）

| 键 | 建议 | 为什么 |
|---|---|---|
| `CHAT_KEYWORDS` | 只放**特有词**（如 `爱丽丝`），别放 `游戏/电脑/AI/原神` 这类高频词 | 关键词命中是 **100% 必定接话**，宽泛词会让实例群变得很吵 |
| `CHAT_REPLY_CHANCE` | 15 偏活跃，安静一点用 5~8 | 普通消息的接话概率 |
| `DASHBOARD_TOKEN` | 换成 16 位随机串（`openssl rand -hex 8`） | 默认/弱口令容易被猜；改了之后面板 URL 也要跟着换 |
| `TIMEZONE` | 显式写 `Asia/Shanghai` | 默认值就是它；写出来便于自查（破冰时间窗、报时、日志时间戳都按它算） |
| `ADMIN_QQ` / `ADMIN_NAME` | 填你自己的 QQ 与称呼 | 它会在提示词里记得"谁写的我" |

### 可选调优（不写就用默认，默认都合适）

| 键 | 默认 | 什么时候改 |
|---|---|---|
| `LLM_THINKING` | `enabled` | 觉得回复慢/想省钱改 `disabled`（注意：思考模式下 `temperature` 不生效） |
| `LLM_MAX_TOKENS` | `2048` | 一般不用动（太小会出现"思考没写完 → 空回复"） |
| `VISION_THINKING_DISABLED` | `true` | 看图表思维链，建议保持 |
| `VISION_INLINE_MAX_MB` / `VISION_INLINE_TOTAL_MB` | `4` / `12` | 大图识别失败多时再调 |
| `VISION_CACHE_DAYS` | `30` | 图片描述缓存保留天数 |
| `MAX_HISTORY` | `20`（你现在是 10） | 一次性喂给模型的对话条数 = 该值 ×2。**P1 起预算会自动裁剪窗口**，所以这个值只是上限，调大到 20（=40 条）能让预算物尽其用 |
| `ICEBREAK_*` | 见 `.env.example` | 破冰时段/间隔/概率/最小群人数 |

### 已废弃（写了也没用，建议删掉）

| 键 | 说明 |
|---|---|
| `SEARCH_API_KEY` | 代码不读取（搜索走 SearXNG，不需要 key） |
| `SEARCH_PROVIDER` | 同上 |

### 不要配（有安全含义）

| 键 | 原因 |
|---|---|
| `DASHBOARD_HOST=0.0.0.0` | 会把面板暴露到公网。默认 `127.0.0.1` 走 SSH 隧道才是对的 |

### 常见坑

13. **绝对不要把 `.env` 整份贴到任何对话、工单、截图或仓库里**（2026-10-07 发生过一次：
    贴出的内容含真实 `LLM_API_KEY`，按泄露处理 → 去平台重置 key，再 `sed` 更新服务器 `.env`）。
    要核对配置时只贴键名：`grep -o '^[A-Z_]*=' /root/qqbot/.env`，或只看值的长度：
    `awk -F= '/^[A-Z_]+=/{printf "%s = <%d 字符>\n", $1, length($2)}' /root/qqbot/.env`。
14. **改密钥绝不要写在命令行里**：`sed -i 's|^LLM_API_KEY=.*|LLM_API_KEY=sk-xxx|' .env` 这种写法，
    key 会同时进入服务器的 `bash history` 和你的终端记录（一旦粘贴出去就泄露）。
    **固定用这个不回显的写法**（`read -s` 不显示输入，`sed` 里只有变量名）：

    ```bash
    read -rsp "新的 LLM_API_KEY: " K; echo
    sed -i "s|^LLM_API_KEY=.*|LLM_API_KEY=$K|" /root/qqbot/.env
    unset K && systemctl restart qqbot
    ```

    改完用一条命令确认格式对不对（只看长度）：`LLM_API_KEY` 应为 **35 字符**（`sk-` + 32 位）。
15. **改完 `.env` 一定要重启**且**从日志确认生效**：`systemctl restart qqbot` 后看启动横幅 ——
    横幅现在会把 `Thinking` / `Chat 概率与关键词` / `Vision` 参数 / `Proactive 主动打扰范围` /
    `Persona`（人设来源与字数）/ `Dashboard token` 都打出来（2026-10-07 就是因为横幅没打这些，改完 token 无法自查，白折腾一轮）。
16. **`[Summary]` / `[Topic]` 日志要攒够 40 条群消息才出现**（`SUMMARY_MIN_MESSAGES`），重启后计数从零开始。
    想马上验证这条路，临时把门槛降下来跑一次再改回去：

    ```bash
    cd /root/qqbot
    grep -q '^SUMMARY_MIN_MESSAGES=' .env && sed -i 's/^SUMMARY_MIN_MESSAGES=.*/SUMMARY_MIN_MESSAGES=3/' .env \
      || echo 'SUMMARY_MIN_MESSAGES=3' >> .env
    grep -q '^SUMMARY_COOLDOWN_MINUTES=' .env && sed -i 's/^SUMMARY_COOLDOWN_MINUTES=.*/SUMMARY_COOLDOWN_MINUTES=0/' .env \
      || echo 'SUMMARY_COOLDOWN_MINUTES=0' >> .env
    systemctl restart qqbot
    # 然后在群里发 3~4 条消息，观察：
    journalctl -u qqbot -f | grep -E "\[Summary\]|\[Topic\]|\[Context\]"
    # 验证完恢复默认
    sed -i 's/^SUMMARY_MIN_MESSAGES=.*/SUMMARY_MIN_MESSAGES=40/' .env
    sed -i 's/^SUMMARY_COOLDOWN_MINUTES=.*/SUMMARY_COOLDOWN_MINUTES=10/' .env
    systemctl restart qqbot
    ```

    产出的记忆文件在 `data/summary/<群ID>.json`（要点 + 画像）与 `data/state/<群ID>.json`（当前话题）。
17. **`grep -c` 数的是行数，不是出现次数**：写自检命令时期望值要按行算
    （例：`SUMMARY_ENABLED` 定义那一行里出现 2 次，`grep -c` 仍返回 1）。第一版清单按"出现次数"写，
    害得服务器自检时虚报不一致，白查了一轮。
