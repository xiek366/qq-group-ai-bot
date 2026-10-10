# QQ Bot 运维速查

> 服务器：阿里云 Ubuntu 22.04 LTS，2核2G
> 项目根：`/root/qqbot`
> NapCat 根：`/root/Napcat`
> **运行方式：项目内 venv** —— `systemd` 执行的是 `/root/qqbot/.venv/bin/python app/main.py`
> （装依赖要用它：`/root/qqbot/.venv/bin/pip install X`，并同步写进 `requirements.txt`）
> **服务器 Python：3.10**（`/usr/lib/python3.10`）—— 代码需保持 3.10 兼容，不要用 3.11+ 专属 API
>
> 📌 **本地代码怎么同步上云**：见 [DEPLOY_SYNC.md](DEPLOY_SYNC.md)（同步清单 + 6 步发布流程 + 验证清单 + 回滚）
> 📌 **每次改动的开发记录**：见 [DEVLOG.md](DEVLOG.md)

---

## 一、服务状态

```bash
# 两个服务一眼看
systemctl status napcat qqbot --no-pager

# 只看 qqbot（日常最常查）
systemctl status qqbot --no-pager

# 失败了看原因
journalctl -u qqbot -n 30 --no-pager
```

状态列含义：
| Active 值 | 含义 |
|---|---|
| `active (running)` | ✅ 正常运行 |
| `active (auto-restart)` | ❌ 崩了 systemd 正在拉（看日志找根因） |
| `inactive (dead)` | ❌ 没在跑（手动 `systemctl start`） |
| `failed` | ❌ 崩了且不再重启 |

---

## 二、日志命令（按场景）

### 2.1 实时跟踪（最常用）

```bash
# qqbot 主日志（群消息、LLM 调用、WS 连接、破冰、表情包——全在这）
journalctl -u qqbot -f

# NapCat 日志（QQ 登录状态、OneBot 适配器、腾讯底层报错）
journalctl -u napcat -f

# 两个一起看
journalctl -u qqbot -u napcat -f --output=short
```

Ctrl+C 退出跟踪。

### 2.2 查最近 N 行（不实时刷新）

```bash
journalctl -u qqbot -n 50 --no-pager        # 最近 50 行
journalctl -u napcat -n 20 --no-pager       # 最近 20 行
journalctl -u qqbot --since "10 min ago" --no-pager   # 近 10 分钟
```

### 2.3 按关键词过滤

```bash
# 查特定群的消息
journalctl -u qqbot -f | grep "group=100000006"

# 查 WS 连接状态
journalctl -u qqbot --no-pager | grep -i "WebSocket\|WS"

# 查破冰
journalctl -u qqbot --no-pager | grep -i "icebreak\|破冰"

# 查表情包
journalctl -u qqbot --no-pager | grep -i "sticker\|表情"

# 查看图
journalctl -u qqbot --no-pager | grep -i "vision\|Look-bind\|看图"

# 查 LLM 调用
journalctl -u qqbot --no-pager | grep -i "LLM\|deepseek"

# 查报错（ERROR + 异常堆栈）
journalctl -u qqbot -p err --no-pager
```

### 2.4 按级别过滤

| 级别 | 用法 | 含义 |
|---|---|---|
| ERROR | `-p err` | 真实的 bug（应该经常 0 条） |
| WARNING | `-p warning` | 警告，可能影响功能 |
| INFO | 默认 | 正常业务日志（最常看） |

```bash
journalctl -u qqbot -p err --no-pager        # 只看错误
journalctl -u qqbot -p warning --no-pager    # 只看警告
journalctl -u napcat -n 100 -p err --no-pager
```

### 2.5 导出日志

```bash
# 导出到本地文件（VSCode 远程管理器直接双击打开）
journalctl -u qqbot --since "1 hour ago" --no-pager > /root/qqbot_recent.log
journalctl -u napcat -n 1000 --no-pager > /root/napcat_recent.log

# 实时追 + 同时存文件
journalctl -u qqbot -f -o short | tee -a /root/qqbot_live.log
```

---

## 三、日志里的常见关键词（查问题时搜这个）

### NapCat (`journalctl -u napcat`)

| 关键词 | 含义 | 正常吗 |
|---|---|---|
| `OneBot11 适配器初始化完成` | OneBot 加载了 HTTP + WS | ✅ 每次启动都应该有 |
| `[HTTP Server Adapter] Start On 127.0.0.1:3000` | HTTP 端口起来了 | ✅ 扫码登录后出现 |
| `[WebSocket Server Adapter] Start On 127.0.0.1:3001` | WS 端口起来了 | ✅ 扫码登录后出现 |
| `发送 -> 群聊` | NapCat 正在往外发消息 | ✅ |
| `接收 <- 群聊` | NapCat 收到群消息 | ✅ |
| `时间同步完成，偏差` | 和腾讯服务器对时 | ✅ 偏差 < 500ms 都正常 |
| `BuglyManager` / `NativeCrashHandler` open file error | 腾讯崩溃上报组件找不到历史 crash 文件 | ✅ 无害 |
| `LongTask` | Chromium 主线程卡顿（QQ 是 Electron 写的） | ✅ 2 核 2G 偶尔出现正常 |

### QQBot (`journalctl -u qqbot`)

| 关键词 | 含义 | 正常吗 |
|---|---|---|
| `✅ WebSocket 已连接` | WS 连上 NapCat | ✅ 每次启动应该在 5 秒内出现 |
| `❌ WebSocket 连接失败` | NapCat 没起来或 token 错 | ❌ 查 NapCat 状态 |
| `[group=xxx] cmd=xxx` | 收到某群的某命令 | ✅ |
| `[Vision]` / `[Vision-only]` | 看图请求 | ✅ |
| `[Look-bind]` | `/看` 会话绑定（等用户下一张图） | ✅ |
| `[Casual]` | 主动插嘴（概率触发） | ✅ |
| `[Icebreak]` / `[破冰]` | 冷场破冰（30 分钟静默触发） | ✅ |
| `[Sticker] 发出 N 个` | 表情包发送 | ✅ |
| `[Style]` | 风格画像总结 | ✅ 攒够 30 条触发 |
| `[Init] 旧记忆迁移：N 个群` | **P0 起**：启动时把 `data/memory/*.json` 迁进对话流 | ✅ 首次启动有；之后是 0 |
| `[Stream] 群 xxx 旧记忆已迁移 N 条` | 某个群的迁移完成 | ✅ 只在首次出现 |
| `[Vision] 发起识别：N 张（内联 X，URL Y）` | **P0 起**：图片已下载并内联后交给模型 | ✅ |
| `[Vision] 缓存命中` | 同一张图重复发，不再请求模型 | ✅ 省钱点 |
| `[Vision] 识别失败 reason=... 跳过=[...]` | 看图失败及原因（`all_failed`/`empty_response`/…） | ⚠️ 偶发正常，连续出现要查 |
| `[Vision-only] group=xxx 不在主动打扰名单，跳过` | **D15 起**：该群不在 `PROACTIVE_GROUPS` 里，裸图不再自动回复 | ✅ 正常（想测看图请用 `/看` + 图，那是被动响应） |
| `[Init] 主动打扰范围: xxx` | **D15 起**：启动时声明哪些群允许主动消息 | ✅ 没这行而是 WARNING 就说明 `.env` 没配 `PROACTIVE_GROUPS` |
| `[Init] 表情字典已预热` | **P0 起**：启动预热，避免第一条消息读盘 | ✅ |
| `[StateRestore] 回填 N 个群活跃时间` | 启动时按文件 mtime 回填破冰状态 | ✅ |
| `[StateRestore] 回填 N 个群活跃时间` | 启动时从文件 mtime 回填破冰状态 | ✅ 修重启漏群 bug 的关键 |
| `[Dashboard] 管理面板已启动` | 8080 面板起来了 | ✅ |
| `ConnectionRefusedError 3001` | NapCat 还没起好或 QQ 掉线 | ⚠️ 5 秒后自动重连应该能成功 |

---

## 四、日志盘占

```bash
journalctl --disk-usage               # 当前 journal 总占多少
df -h                                 # 总磁盘用量
journalctl --vacuum-size=50M          # 只保留 50M（安全，手动清）
```

默认 journald 按日期+大小自动清理，40G 盘只用了 19%——**不用管**。只有跑了半年以上才可能需要手动清。

---

## 五、重启 / 停止 / 启动

```bash
# ⭐ 日常重启机器人（NapCat 不动，不丢 QQ 会话）
systemctl restart qqbot

# 重启 NapCat（⚠️ 重启后必须重新扫码！QQ 登录态丢了）
systemctl restart napcat

# 先停再启
systemctl stop qqbot napcat
systemctl start napcat qqbot

# 两个一起看启动日志
journalctl -u napcat -u qqbot -f -n 30
```

---

## 六、数据路径

| 用途 | 路径 |
|---|---|
| **统一对话流（P0 起的事实来源）** | `/root/qqbot/data/stream/<群ID>.jsonl`（append-only，一行一条 JSON） |
| 旧记忆（迁移后留底） | `/root/qqbot/data/memory/<群ID>.json.migrated` |
| 风格画像 + pending | `/root/qqbot/data/group_style/<群ID>.json` |
| 群友外号表 | `/root/qqbot/data/aliases/<群ID>.json` |
| 表情包库 | `/root/qqbot/data/stickers/index.json` + 图片 |
| 图片描述缓存（P0 起） | `/root/qqbot/data/cache/vision/<哈希>.json` |
| 运行指标（P0 起，健康卡数据源） | `/root/qqbot/data/stats/summary.json` |
| OneBot 配置 | `/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/onebot11_100000007.json` |
| WebUI token | `/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json` |
| NapCat 二维码 | `/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png` |
| 系统服务文件 | `/etc/systemd/system/napcat.service` / `/etc/systemd/system/qqbot.service` |
| 发布备份 | `/root/backups/qqbot_<时间戳>.tar.gz`（见 DEPLOY_SYNC.md 第 3.2 步） |

> **本地 ↔ 云端怎么同步**：见 [DEPLOY_SYNC.md](DEPLOY_SYNC.md)（同步清单 / 6 步发布流程 / 验证清单 / 回滚）。

---

## 七、面板

```
# 本地 SSH 隧道转发 8080 → 浏览器访问
# VSCode Ports 面板自动转发，看 Forwarded Address 栏的端口号
http://127.0.0.1:<转发端口>/?token=<你的_DASHBOARD_TOKEN>
```

面板里能看：WS 状态卡（绿=连红=断）、实时消息流（SSE 推送）、群列表、记忆/画像状态、手动清空某群记忆。

---

## 八、踩坑红线（别碰）

1. **不要随便 `systemctl restart napcat`** — 重启 NapCat = QQ 登录态丢 = 必须重新扫码
2. **重启服务器后必须重扫** — reboot 两个服务都自启，但 NapCat 进程重启 = 登录态丢
3. **token 要三处一致** — NapCat WebUI 网络配置 + `onebot11_100000007.json` + `/root/qqbot/.env`（ONEBOT_ACCESS_TOKEN）
4. **改代码后只需要 `systemctl restart qqbot`** — NapCat 不动，QQ 会话保留
5. **不要手动 kill NapCat 进程** — 会触发 systemd 自动拉起，但拉起后登录态还是丢
