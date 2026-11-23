# ShellPilot 设计方案

> AI 远程运维助手 —— 通过自然语言对话连接 Linux 服务器（SSH），自主执行命令排查与操作

## 1. 项目概述与目标

### 1.1 背景
运维人员排查服务器问题时通常需要手动 SSH 登录、逐条敲命令看日志/进程/端口/磁盘，费时费力且依赖经验。本工具将这一过程对话化、自动化：用户用中文描述问题（如"我的 nginx 打不开了，帮我看看"），AI 自主规划排查步骤、通过 SSH 执行命令、分析输出，最终给出诊断结论与修复建议。

### 1.2 核心目标
1. **对话式排障**：自然语言 → AI 自主执行排查命令 → 诊断结论
2. **多服务器管理**：可视化管理多台 Linux 服务器（SSH 连接信息）
3. **安全可控**：只读排查命令自动执行；危险操作（重启服务、杀进程、改配置、删文件等）需人工确认
4. **实时可视化**：Web 界面实时展示 AI 思考过程、执行的命令与输出
5. **LLM 可配置**：兼容 OpenAI 协议的任意 LLM 服务（DeepSeek/通义/Kimi/私有部署等），配置页面自行填写 API 地址与密钥

### 1.3 非目标（本期不做）
- 不做多用户/权限系统（单用户本地工具）
- 不做定时巡检/告警（后续可扩展）
- 不做 Windows 服务器支持（仅 Linux）
- 不做服务器文件管理器/在线编辑（仅命令执行）

---

## 2. 技术选型与开源参考

### 2.1 技术栈
| 层 | 选型 | 理由 |
|---|---|---|
| 后端语言 | Python 3.12 | 环境已有，生态成熟 |
| Web 框架 | aiohttp | 轻量、原生 async、支持 WebSocket，适合长连接推送 |
| SSH | paramiko | Python 事实标准 SSH 库，支持密码/密钥认证 |
| LLM 调用 | httpx/aiohttp 直连 | 兼容 OpenAI Chat Completions 协议，无需绑定 SDK |
| 前端 | 原生 HTML/CSS/JS | 零构建、单文件夹部署，配合 WebSocket 实时更新 |
| 配置存储 | config.json（项目根目录） | 单用户工具，简单透明 |
| 会话存储 | server/sessions/（JSON 文件） | 便于回看历史会话，无需数据库 |

### 2.2 开源项目参考
以下开源项目在架构/交互设计上提供了参考（均为业界成熟的 AI Agent + 工具调用范式）：

1. **OpenHands（原 OpenDevin）** —— AI Agent 自主执行命令完成软件工程任务的完整架构参考：
   - Agent Loop（观察 → 思考 → 行动 → 观察）驱动
   - 事件流（Event Stream）解耦 Agent、工具执行器、前端 UI
   - 参考点：Agent 循环结构、Action/Observation 事件协议

2. **Claude Code / Claude Agent SDK** —— 当前会话即运行在此类 harness 之上，其"工具调用 + 权限确认"的交互范式直接借鉴：
   - 工具调用（tool_use）与确认机制（permission prompt）
   - 系统提示词中嵌入工具使用规范与安全边界
   - 参考点：危险操作确认交互、命令安全分级思想

3. **ShellGPT / interpreter（OpenInterpreter）** —— LLM 生成 shell 命令并执行的最小化闭环参考：
   - LLM 输出结构化命令 → 本地执行 → 输出回灌 LLM → 循环
   - 参考点：工具 schema 设计、执行输出截断与回灌策略

4. **LangChain/LangGraph ReAct Agent** —— ReAct（Reasoning + Acting）范式的标准实现：
   - Thought → Action → Observation 循环
   - 参考点：Agent 系统提示词模板、最大迭代轮次保护

> 以上项目均为 Apache-2.0 / MIT 协议，仅作架构与交互设计参考，不直接复制代码。

### 2.3 Agent 范式选择：ReAct + 工具调用（Function Calling）
采用标准 **ReAct Agent Loop**：

```
用户提问
  ↓
[Agent Loop] LLM(系统提示 + 历史 + 工具结果) → 生成工具调用或最终回答
  ↓ 工具调用
安全检查（只读？危险？）
  ↓
  ├─ 只读命令 → 直接通过 SSH 执行 → 输出回灌 LLM → 继续循环
  └─ 危险命令 → 前端弹出确认 → 用户同意 → 执行 → 输出回灌 → 继续循环
  ↓
LLM 输出最终诊断结论 → 展示给用户
```

关键设计：
- **工具协议**：采用 OpenAI Function Calling 协议（tool_choice=auto），LLM 输出 `{"name": "run_command", "arguments": {"cmd": "...", "server": "..."}}`，后端解析执行后将结果作为 `tool` 消息回灌
- **迭代保护**：单次会话最多 N 轮工具调用（默认 15 轮），防止无限循环
- **输出截断**：单条命令输出超过 8000 字符自动截断（保留头尾），防止上下文爆炸

---

## 3. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│  浏览器 (web/)                                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                 │
│  │ 聊天界面  │ │ 服务器管理│ │ LLM 设置 │                 │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘                 │
│       │ HTTP/REST + WebSocket                          │
└───────┼────────────┼────────────┼───────────────────────┘
        ↓            ↓            ↓
┌─────────────────────────────────────────────────────────┐
│  后端 (server/ · aiohttp)                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │ HTTP API 层 (REST + WS)                          │   │
│  │  /api/servers  /api/config  /api/chat  /ws       │   │
│  └───────────────────────┬──────────────────────────┘   │
│                          ↓                              │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Agent 引擎 (ReAct Loop)                          │   │
│  │  - 系统提示词组装（含安全规则/服务器上下文）        │   │
│  │  - LLM 请求（OpenAI 协议，流式/非流式）           │   │
│  │  - 工具调用解析与调度                              │   │
│  │  - 安全分级（只读/危险命令识别）                   │   │
│  │  - 会话状态管理（历史、轮次）                      │   │
│  └──────┬───────────────────────────────┬───────────┘   │
│         ↓                               ↓               │
│  ┌──────────────┐               ┌──────────────────┐    │
│  │ SSH 管理器    │               │ 配置/会话存储     │    │
│  │ (paramiko)   │               │ config.json      │    │
│  │ 连接池/复用   │               │ sessions/*.json  │    │
│  └──────┬───────┘               └──────────────────┘    │
└─────────┼───────────────────────────────────────────────┘
          ↓ SSH
   ┌──────────────┐
   │ Linux 服务器   │
   └──────────────┘
```

### 3.1 目录结构
```
shellpilot/
├── server/
│   ├── app.py              # 入口：aiohttp 应用装配、路由注册、启动
│   ├── config.py           # 配置读写（config.json）
│   ├── ssh_manager.py      # SSH 连接管理（paramiko 封装、连接复用、命令执行）
│   ├── llm_client.py       # LLM 调用（OpenAI 协议，流式支持）
│   ├── agent.py            # ReAct Agent 循环、工具调度、安全分级
│   ├── safety.py           # 命令安全分类（只读/危险/未知规则引擎）
│   ├── session.py          # 会话管理（历史存储、恢复）
│   └── routes.py           # HTTP API 与 WebSocket 路由
├── web/
│   ├── index.html          # 单页应用主入口
│   ├── css/style.css       # 样式（深色主题终端风）
│   └── js/
│       ├── app.js          # 路由切换、状态管理
│       ├── chat.js         # 聊天界面、WebSocket 通信
│       ├── servers.js      # 服务器管理页
│       └── settings.js     # LLM 设置页
├── config.json             # 运行时生成（含 LLM 密钥，勿外传）
├── server/sessions/        # 会话历史 JSON（运行时生成）
├── requirements.txt
└── README.md
```

---

## 4. 核心模块设计

### 4.1 SSH 管理器（ssh_manager.py）
- **连接管理**：`SSHManager` 单例，按 `server_id` 缓存连接；执行命令前检查连接活性（发送 `echo ok` 探活），失败自动重连一次
- **认证**：支持密码认证与私钥认证（私钥路径 + 可选 passphrase）
- **命令执行**：`exec_command(cmd, timeout=30)` 返回 `(exit_code, stdout, stderr)`；超时自动关闭通道
- **并发**：同一服务器串行执行（避免命令交叉干扰），不同服务器可并发
- **安全**：SSH 密码/私钥仅存于服务端 `config.json`，前端 API 返回时脱敏（仅返回 `has_password`/`has_key` 布尔值）

### 4.2 LLM 客户端（llm_client.py）
- **协议**：OpenAI Chat Completions（`POST {base_url}/chat/completions`），兼容 DeepSeek/通义/Kimi/OpenAI/私有 vLLM 等
- **配置项**：`base_url`、`api_key`、`model`、`temperature`、`max_tokens`、`timeout`
- **流式**：支持 SSE 流式返回（`stream=true`），将增量内容实时推送到前端（打字机效果）
- **工具调用**：请求体携带 `tools`（function schema），解析响应中的 `tool_calls`
- **错误处理**：网络错误/鉴权失败/限流 → 友好错误信息推送到前端，终止本轮 Agent 循环

### 4.3 Agent 引擎（agent.py）—— 核心循环
```
loop (最多 max_iterations=15):
    messages = [system_prompt] + history + tool_results
    response = llm.chat(messages, tools=AGENT_TOOLS)
    if response.tool_calls:
        for tc in response.tool_calls:
            if tc.name == "run_command":
                risk = safety.classify(tc.arguments.cmd)
                if risk == SAFE:      result = ssh.exec(...)
                elif risk == DANGEROUS: await ws.send(confirm_request); result = wait_user_confirm()
                else (UNKNOWN):       treat as DANGEROUS
            push event to ws (tool_start / tool_output / tool_end)
        history.append(assistant_msg, tool_msgs)
    else:
        emit final_answer to ws; break
```

**工具定义（AGENT_TOOLS）**：
| 工具 | 参数 | 说明 |
|---|---|---|
| `run_command` | `server`(可选,默认当前), `cmd`, `description`(意图说明) | 在服务器执行 shell 命令 |
| `read_file` | `server`, `path`, `tail_lines`(可选) | 读取文件（末尾 N 行，等价 tail，防大文件爆炸） |
| `list_services` | `server`(可选) | 列出 systemd 服务状态（封装 `systemctl list-units`） |
| `get_service_status` | `service`, `server`(可选) | 查看服务状态+最近日志（封装 `systemctl status` + `journalctl`） |
| `check_port` | `port`, `server`(可选) | 检查端口占用（封装 `ss -tlnp`） |
| `check_resources` | `server`(可选) | 查看 CPU/内存/磁盘（封装 `top/df/free`） |

> 上述高层工具均为 `run_command` 的便捷封装，减少 LLM 拼命令出错概率；LLM 也可直接调用 `run_command` 执行任意命令。

**系统提示词要点**：
- 角色：资深 Linux 运维专家（SRE），负责排障
- 行为规范：先观察再动手；优先只读排查；给出结论时附证据（引用命令输出）；不确定时说明
- 工具使用规范：每次调用工具后基于输出继续推理；命令尽量精确（grep 限定行数、tail -n 限定）
- 安全边界：绝不执行破坏性命令（rm -rf /、mkfs、dd、fork bomb 等）；危险操作必须经用户确认
- 输出格式：诊断结论用 Markdown（现象 → 原因 → 证据 → 修复建议 → 预防建议）

### 4.4 安全分级（safety.py）
基于规则引擎 + 正则白名单/黑名单分类：

| 级别 | 判定 | 处理 |
|---|---|---|
| `SAFE`（只读） | 匹配只读命令白名单：`ls/cat/head/tail/grep/ps/top/df/free/ss/netstat/systemctl status/journalctl/uptime/whoami/id/hostname/uname/w/last/find(不带-delete)/wc/du/df/ping/dig/nslookup/curl(仅GET且非下载)` 等 | 自动执行 |
| `DANGEROUS` | 匹配危险命令黑名单：`rm/rmdir/mv(到/dev/null)/dd/mkfs/shutdown/reboot/halt/poweroff/kill/killall/pkill/systemctl start/stop/restart/enable/disable/mount/umount/chmod/chown/useradd/userdel/passwd/iptables/ufw/sed -i/tee(写)/crontab -r/echo >(重定向写)/truncate/reboot` 等 | 弹出确认（展示完整命令+影响说明） |
| `UNKNOWN` | 既不匹配白名单也不匹配黑名单 | 按 DANGEROUS 处理（安全兜底） |
| `FORBIDDEN`（绝对禁止） | `rm -rf /`、`mkfs`、`dd of=/dev/`、fork bomb `:(){:|:&};:`、`chmod -R 777 /`、改 `/etc/passwd`、`> /dev/sda` 等 | 直接拒绝执行并告知用户，不允许确认放行 |

**多命令识别**：命令中含 `&&`/`;`/`|`/`|` 时拆分后逐段判定，**取最严格级别**。

### 4.5 会话管理（session.py）
- 每个会话 `session_id`（UUID），存储为 `server/sessions/{session_id}.json`
- 保存：用户消息、AI 回复、工具调用记录（命令/输出/风险级别/是否确认）、时间戳
- 支持：新建会话、历史会话列表、恢复会话继续对话（重新加载历史到 Agent 上下文）

---

## 5. API 设计（REST + WebSocket）

### 5.1 REST API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/servers` | 获取服务器列表（密码脱敏） |
| POST | `/api/servers` | 添加服务器 |
| PUT | `/api/servers/{id}` | 更新服务器 |
| DELETE | `/api/servers/{id}` | 删除服务器 |
| POST | `/api/servers/{id}/test` | 测试 SSH 连接 |
| GET | `/api/config/llm` | 获取 LLM 配置（密钥脱敏） |
| PUT | `/api/config/llm` | 保存 LLM 配置 |
| POST | `/api/config/llm/test` | 测试 LLM 连通性 |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}` | 会话详情（历史） |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/api/chat` | 发起对话（返回 session_id，实际执行走 WebSocket） |

### 5.2 WebSocket（`/ws`）
**客户端 → 服务端**：
```json
{"type": "user_message", "session_id": "...", "content": "...", "server_id": "可选,指定目标服务器"}
{"type": "confirm", "tool_call_id": "...", "approved": true}
{"type": "cancel", "session_id": "..."}
```

**服务端 → 客户端**（事件流）：
```json
{"type": "session_start", "session_id": "..."}
{"type": "assistant_thinking", "content": "..."}        // LLM 流式文本
{"type": "tool_call_start", "tool_call_id": "...", "name": "run_command", "arguments": {...}, "risk": "safe|dangerous"}
{"type": "command_output", "tool_call_id": "...", "stream": "..."}   // 命令输出流式/分块
{"type": "confirm_required", "tool_call_id": "...", "command": "...", "reason": "危险操作说明"}
{"type": "tool_call_end", "tool_call_id": "...", "exit_code": 0, "success": true}
{"type": "final_answer", "content": "诊断结论 Markdown"}
{"type": "error", "message": "..."}
{"type": "agent_done"}
```

---

## 6. 前端设计

### 6.1 布局（单页应用）
```
┌────────────────────────────────────────────────────────┐
│  顶部导航栏：🤖 ShellPilot   [对话] [服务器管理] [设置]     │
├──────────┬─────────────────────────────────────────────┤
│ 侧边栏    │ 主内容区                                    │
│ 会话列表  │  ┌────────────────────────────────────────┐ │
│ + 新建会话│  │ 聊天气泡 / 工具调用卡片 / 确认弹窗       │ │
│ 历史会话  │  │                                        │ │
│          │  │  AI: 我来检查一下 nginx...               │ │
│          │  │  ┌ 🛠 run_command (安全) ─────────────┐  │ │
│          │  │  │ $ systemctl status nginx            │  │ │
│          │  │  │ ● nginx.service - ...               │  │ │
│          │  │  └────────────────────────────────────┘  │ │
│          │  │  ┌ ⚠️ 需要确认 ────────────────────────┐  │ │
│          │  │  │ systemctl restart nginx             │  │ │
│          │  │  │ [确认执行] [取消]                    │  │ │
│          │  │  └────────────────────────────────────┘  │ │
│          │  └────────────────────────────────────────┘ │
│          │  [输入框：描述你的问题...        发送/停止]    │
└──────────┴─────────────────────────────────────────────┘
```

### 6.2 交互细节
- **工具调用卡片**：可折叠展示命令与输出（默认折叠，点击展开全文）；输出等宽字体、深色背景
- **危险确认**：内联卡片（非模态弹窗）展示命令+风险说明，按钮"确认执行/取消"，30 秒无操作自动取消
- **Markdown 渲染**：AI 回复支持 Markdown（引入 marked.js CDN 或自实现轻量渲染）
- **流式打字机**：`assistant_thinking` 事件增量渲染
- **服务器选择**：聊天输入框上方可选择目标服务器（默认"自动"——AI 自行选择或指定）

### 6.3 样式风格
- 深色终端风格（背景 `#0d1117`，卡片 `#161b22`，强调色 `#58a6ff`）
- 命令输出区域：等宽字体（`Consolas/monospace`）、绿色高亮（终端感）
- 中文界面

---

## 7. 安全设计

1. **命令分级执行**（见 4.4）：只读自动 / 危险确认 / 禁止拒绝
2. **凭据安全**：SSH 密码与 LLM API Key 存于服务端 `config.json`，前端获取时脱敏；不写日志
3. **本地绑定**：服务仅监听 `127.0.0.1:8765`，不对外网开放（如需远程访问请自行加 HTTPS 反代+鉴权）
4. **超时保护**：SSH 命令默认 30s 超时（可配置）；Agent 循环最多 15 轮工具调用
5. **输出截断**：单命令输出 >8000 字符截断，防止上下文溢出
6. **审计日志**：所有执行过的命令记录在会话 JSON 中，可回查

---

## 8. 实施计划（按阶段实现）

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **P1 基础框架** | 后端骨架：config/ssh/llm 模块 + aiohttp 启动 + 健康检查 | `server/` 全部模块骨架可运行 |
| **P2 Agent 引擎** | ReAct 循环 + 工具定义 + 安全分级 + WebSocket 事件流 | `agent.py`/`safety.py` 核心完成 |
| **P3 前端界面** | 聊天界面 + 工具卡片 + 确认交互 + 服务器管理 + 设置页 | `web/` 完整可用 |
| **P4 联调与测试** | 端到端联调、安全规则测试、异常处理完善 | 可实际排障使用的工具 |

---

## 9. 风险与权衡

| 风险 | 应对 |
|---|---|
| LLM 拼命令出错/误判 | 高层工具封装（list_services 等）减少自由发挥；安全分级兜底 |
| 危险命令误判为安全 | 白名单机制 + UNKNOWN 按 DANGEROUS 处理 + FORBIDDEN 绝对拦截 |
| 上下文超长 | 输出截断、历史消息保留最近 N 轮、迭代次数上限 |
| SSH 连接不稳定 | 自动重连一次、命令超时保护、错误友好提示 |
| LLM 服务不可用 | 连通性测试入口、错误提示、支持随时切换模型配置 |

---

*本方案完成后进入实现阶段，按 P1→P2→P3→P4 顺序开发。*
