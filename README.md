# ShellPilot

> 用自然语言驾驶你的 Linux 服务器：AI 通过 SSH 自主排查故障、执行修复操作，危险命令必须你点头。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**中文** | [English](README.en.md)

基于 Web 的对话式 AI 远程运维工具：用自然语言描述服务器问题，AI 通过 SSH 自主执行排查命令，给出诊断结论，并在你确认后动手修复。

```
你：帮我查一下 nginx 为什么起不来了
AI：❯ systemctl status nginx        [只读·自动执行]
    ❯ journalctl -u nginx -n 100    [只读·自动执行]
    ❯ ss -tlnp | grep :80           [只读·自动执行]
    → 结论：80 端口被 httpd 占用导致 nginx 启动失败…
你：那就把 httpd 停掉，再重启 nginx
AI：❯ systemctl stop httpd          [危险·等待确认]
```

## 功能

- **对话式运维**：AI 自主规划步骤（状态 → 日志 → 进程 → 端口 → 资源），循环执行直到得出结论；诊断之后可以继续让它动手处理
- **多服务器管理**：可视化增删改查，支持密码 / 私钥认证，一键测试连通性
- **连接本机**：无需 SSH，一条「本机」配置即可让 AI 直接在本机执行命令（Windows 经 PowerShell，Linux 经 bash）
- **Windows 服务器**：SSH 连接 Windows 主机（需安装 OpenSSH Server）后，AI 自动切换 PowerShell/cmd 命令（Get-Service / netstat -ano / sc query…），中文输出自动解码
- **三级安全策略**：
  - `SAFE` 只读命令（`ps`/`journalctl`/`tail`/`ss`/`df`/`Get-Service`/`tasklist`/`netstat`/只读 `docker`/`kubectl`/`git`…）**自动执行**
  - `DANGEROUS` 危险操作（`systemctl restart`/`kill`/`rm`/`chmod`/`Remove-Item`/`taskkill`/`reg add`/`net stop`…）**弹窗确认后执行**
  - `FORBIDDEN` 绝对禁止（`rm -rf /`/`mkfs`/`dd` 写盘/fork bomb/`format`/`diskpart`/`Format-Volume`/`wevtutil cl`…）**直接拦截**
  - 未知命令一律按危险处理；多命令（`&&`/`;`/`|`）取下最严格级别
- **实时可视化**：WebSocket 推送 AI 思考过程、每条命令与完整输出（可折叠卡片）
- **LLM 可配置**：任意兼容 OpenAI Chat Completions 协议的服务（DeepSeek / 通义 / Kimi / 私有部署…），在设置页填 Base URL + Key + 可选模型列表；对话输入框上方下拉框可随时切换模型
- **会话持久化**：对话与命令审计日志存 PostgreSQL，可随时回看与续聊（原 JSON 文件存储已废弃）
- **时间顺序展示**：AI 最终回复固定排在所有命令执行记录之后，流式思考过程不会占用最终位置
- **多轮上下文**：同一会话的历史（含命令输出）默认全量发给模型（适合大上下文模型）；可在设置页配置上限后按整轮裁剪

## 快速开始

前置条件：Python 3.10+、PostgreSQL 13+（会话与审计日志使用）。数据库可自动建表，但需先建库：

```bash
createdb shellpilot          # 或用 psql / pgAdmin 建库
pip install -r requirements.txt
cp config.example.json config.json   # Windows: copy config.example.json config.json
python server/app.py
```

启动日志会打印服务地址；**首次启动会自动创建管理员账号**（账号存于数据库 `users` 表，密码只加密存储，明文仅打印这一次）：

```
ShellPilot 启动: http://127.0.0.1:8765
首次启动已生成管理员账号（密码只加密存储，明文仅此一次，请妥善保存）:
  用户名: admin
  密码:   xxxxxxxxxxxxxxxx
```

浏览器打开 http://127.0.0.1:8765 ，用上面的账号登录。忘记密码可在服务端执行 `python server/app.py --reset-password` 重置（旧密码与该账号所有登录会话立即失效）。登录后可在顶栏 **账号** 页修改密码（修改后强制重新登录）。

**多用户**：登录页提供「注册新账号」入口（默认开放，可用配置 `auth.allow_registration` 关闭）。每个账号有**完全独立**的 LLM 配置、服务器列表、会话历史与密码——新注册用户从空白配置开始，自己填 API Key、添加自己的服务器；管理员看不到也不动别人的配置。旧版本（账号/LLM/服务器存于 config.json）首次启动时自动迁入数据库并划归管理员。

> 完整的环境准备、数据库建库、后台常驻、停止/重启、备份恢复与故障排查，见 [DEPLOY.md](DEPLOY.md)。

1. 进入 **设置** → 填写 API Base URL、API Key、模型名称与可选模型列表（每行一个）→ 点「测试连接」→「保存」
2. 进入 **服务器管理** → 添加服务器（或选「本机」连接方式）→ 点「测试连接」确认可达
3. 回到 **对话** → 顶部选择目标服务器与使用模型 → 描述你的问题

### 连接本机 / Windows 服务器

- **本机模式**：添加服务器时「连接方式」选 **本机（免 SSH）**，只需填名称——命令直接在运行 ShellPilot 的这台电脑上以子进程执行（Windows 经 PowerShell、Linux 经 bash），系统类型自动识别，安全分级照常生效。适合让 AI 直接排查 ShellPilot 所在机器。
- **远程 Windows**：在目标 Windows 机器上以管理员身份安装并启动 OpenSSH Server：
  ```powershell
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  Set-Service sshd -StartupType Automatic
  Start-Service sshd
  ```
  然后像添加 Linux 服务器一样填地址与凭据，「系统类型」选 **Windows**（不确定可先留 Linux 并点「测试连接」，会自动探测并回写）。AI 会自动使用 PowerShell/cmd 命令（`Get-Service`、`netstat -ano`、`sc query` 等），中文 GBK 输出自动解码。
- 默认 shell 为 cmd.exe；复杂查询 AI 会自动用 `powershell -NoProfile -Command "Get-..."` 形式执行，只读查询自动放行，修改类操作仍需确认。

> 服务默认仅监听 `127.0.0.1:8765`，不对外网开放；所有接口（含 WebSocket 与静态资源）都要求登录认证。如需远程访问，请自行加反向代理 + HTTPS，并了解认证与 Origin 校验的行为（见下文「安全说明」）。

## 目录结构

```
shellpilot/
├── server/                 # 后端（Python + aiohttp + paramiko）
│   ├── app.py              # 入口：应用装配、访问控制、静态文件、启动
│   ├── auth.py             # 登录认证 + 会话管理 + Origin/Host 校验（防 CSWSH/CSRF/DNS rebinding）
│   ├── routes.py           # REST API + WebSocket 路由
│   ├── agent.py            # ReAct Agent 循环、工具定义与调度
│   ├── safety.py           # 命令安全分级（白名单/黑名单/绝对禁止）
│   ├── ssh_manager.py      # SSH 连接池、命令执行、主机指纹校验（TOFU）
│   ├── llm_client.py       # LLM 流式调用（OpenAI 协议）
│   ├── config.py           # 注册开关/SSH 策略读写 + 每用户 LLM/服务器配置的业务层
│   └── session.py          # PostgreSQL 存储：账号、每用户 LLM/服务器、会话与审计日志
├── web/                    # 前端（原生 HTML/CSS/JS，零构建）
│   ├── index.html
│   ├── css/style.css
│   └── js/{app,chat,servers,settings}.js
├── tests/                  # 测试
│   ├── test_agent_flow.py  # Agent 循环与安全策略（Mock LLM/SSH，无需真实环境）
│   ├── test_auth.py        # 鉴权与访问控制（自起隔离服务，需本机 PostgreSQL）
│   └── test_integration.py # 端到端（需先启动 app.py）
├── DEPLOY.md              # 启动 / 部署 / 运维手册
├── config.json             # 运行时生成（含密钥，勿提交）
└── requirements.txt
```

## 配置项

`config.json` 只存全局选项；**LLM 配置与服务器列表已按用户存数据库**（设置页 / 服务器管理页修改），文件里的 `llm` / `servers` 两段仅在从旧版本升级时作为一次性迁移来源，迁移完成后自动清空：

| 配置 | 说明 | 默认 |
|---|---|---|
| `auth.allow_registration` | 是否开放注册（登录页显示注册入口；`false` 时仅管理员在服务端建号） | `true` |
| `ssh.host_key_policy` | SSH 主机指纹校验：`tofu`（首次记录，变更拒绝）/ `strict` / `auto`（不校验） | `tofu` |

每用户的 LLM 配置（`base_url` / `api_key` / `model` / `models` / `timeout` / `history_budget_chars`）与服务器列表字段含义不变，只是存储位置从 config.json 移到了数据库的 `user_llm` / `servers` 表，且每个用户各一份。

> 温度、max_tokens、迭代轮次等 Agent 内部参数已固化为代码常量（`server/agent.py`、`server/llm_client.py`），不作为配置暴露。API Key 留空或 `********` 保存表示不修改原密钥。

可用环境变量 `SHELLPILOT_CONFIG` 指定配置文件路径；`SHELLPILOT_PG_DSN` 指定 PostgreSQL 连接串（默认 `host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456`）；`SHELLPILOT_PASSWORD` 指定首次建管理员账号时的初始密码（不落盘；账号已存在时无效）；`SHELLPILOT_KNOWN_HOSTS` 指定 SSH 指纹库路径（默认 `~/.shellpilot/known_hosts`）；`SHELLPILOT_HOST_KEY_POLICY` 覆盖指纹校验策略。旧变量名 `YCAI_CONFIG` / `YCAI_PG_DSN` 仍然兼容。

## 数据库

账号、每用户的 LLM/服务器配置、会话与消息都存于 PostgreSQL 的 `shellpilot` 库（首次启动自动建表）：

- `users`：登录账号（用户名 / PBKDF2 密码哈希 / 最后登录时间）
- `servers`：每用户自己的服务器列表（含 SSH 凭据，按 `user_id` 隔离）
- `user_llm`：每用户自己的 LLM 配置（Base URL / API Key / 模型，按 `user_id` 隔离）
- `sessions`：会话元数据（id / 归属用户 / 标题 / 目标服务器 / 时间戳）
- `messages`：每条消息与命令审计（角色 / 内容 / 工具参数 / 命令输出 / 退出码 / 风险等级 / 时间戳），按 `seq` 排序保存，支持完整回放

> 旧版本把服务器凭据和 LLM Key 存在 `config.json`；升级后首次启动会自动把它们迁入数据库（管理员名下），`config.json` 中随即清除。SSH 密码 / 私钥 / LLM Key 从此只存数据库，前端接口返回时一律脱敏。

## AI 可用的工具

| 工具 | 说明 |
|---|---|
| `run_command` | 执行任意 shell 命令（受安全分级约束） |
| `read_file` | 读取文件末尾 N 行（等价 `tail -n`，防大文件） |
| `get_service_status` | 服务状态 + 最近 100 行日志（`systemctl status` + `journalctl`） |
| `check_port` | 端口占用（`ss -tlnp`） |
| `check_resources` | CPU / 内存 / 磁盘 |
| `list_services` | 列出 systemd 服务单元 |

高层工具减少模型拼命令出错的概率，模型也可直接用 `run_command` 灵活执行。

## 测试

```bash
# 单元测试（Mock LLM + Mock SSH，不连真实服务器）
python tests/test_agent_flow.py

# 鉴权与访问控制（自起隔离服务 + 测试数据库，不碰真实配置）
python tests/test_auth.py

# Windows / 本机模式（安全分级矩阵 + 命令构造 + 本机子进程执行 + Agent 分流）
python tests/test_windows_local.py

# 集成测试（Mock LLM HTTP 服务；先以 SHELLPILOT_PASSWORD=test1234 启动 app.py，
# 再以同名环境变量运行本测试）
SHELLPILOT_PASSWORD=test1234 python tests/test_integration.py
```

> 集成测试会临时把 LLM 指向 Mock 服务、结束后恢复原 Base URL/模型/超时；全程不读取、不修改真实 API Key。测试前仍建议备份 `config.json`。

## 安全说明

**认证与访问控制**（`server/auth.py`）：

- **登录认证**：账号存于 PostgreSQL `users` 表，首次启动 users 表为空时自动创建管理员（用户名默认 `admin`，密码随机生成、PBKDF2 加密存储、明文只打印一次；也可用 `SHELLPILOT_PASSWORD` 指定）。所有 API、WebSocket 与静态资源都要求登录会话；`/api/health`、登录/注册页除外（不返回敏感数据）
- **注册与多用户**：登录页「注册新账号」默认开放（`auth.allow_registration: false` 可关闭，关闭后注册页仍可见但无法提交）。各账号的**LLM 配置、服务器列表、会话历史完全按用户隔离**——注册用户看到的是空白配置，需要自己填 API Key、添加服务器；密码与会话也相互独立，修改密码只注销自己的其他会话。Agent 执行工具前会校验服务器归属，无法操作别人的服务器。**公网部署建议关闭注册**
- **会话管理**：登录成功下发 HttpOnly + SameSite=Strict 会话 Cookie（30 天滑动过期）；脚本可改用 `Authorization: Bearer <会话令牌>`。退出登录立即失效；修改密码注销该账号的其他会话
- **CSWSH 防护**：带 `Origin` 的 WebSocket 握手必须同源，恶意网页无法借你的浏览器劫持本机 WS 执行命令
- **CSRF 防护**：带 `Origin` 的写请求（POST/PUT/DELETE，含登录接口）必须同源，拦截"简单请求"式跨站调用
- **DNS rebinding 防护**：`Host` 只接受回环地址、`localhost` 与 IP 字面量；解析到你本机的攻击域名会被拒绝。局域网域名可用 `SHELLPILOT_ALLOWED_HOSTS`（逗号分隔）放行，反代场景用 `SHELLPILOT_ALLOWED_ORIGINS` 放行对应来源
- **暴力破解缓解**：登录失败固定延迟 1 秒再响应；密码哈希使用 PBKDF2-SHA256（20 万次迭代）
- **密钥入口校验**：保存 LLM 配置时拒绝内嵌凭据的 `base_url` 与非法协议；明文 HTTP 的公网网关允许保存但会给出告警
- **主机指纹 TOFU**：首次 SSH 连接记录主机指纹到 `~/.shellpilot/known_hosts`，之后指纹变化即拒绝连接（防中间人窃取密码）；服务器确实重装过系统时，可在「服务器管理」里点「重置指纹」

**其他**：

- SSH 密码 / 私钥 / LLM Key 只存于 PostgreSQL 数据库（按用户隔离的 `servers` / `user_llm` 表），前端接口返回时脱敏；一次性迁移后 `config.json` 不再含任何密钥
- 危险操作确认有 300 秒超时，超时自动取消
- 所有执行过的命令与输出都持久化在 PostgreSQL，可审计回查
- 安全分级是**规则白名单制**：不在白名单内的命令一律要求确认（宁可多问，不可误执行）

## 后续可扩展

- 定时巡检 / 告警推送
- 多服务器并行排查（如集群问题）
- 排障知识库（把历史结论沉淀为可复用经验）
- 命令执行回滚 / 变更审批流
