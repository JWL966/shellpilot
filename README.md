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
- **三级安全策略**：
  - `SAFE` 只读命令（`ps`/`journalctl`/`tail`/`ss`/`df`/只读 `docker`/`kubectl`/`git`…）**自动执行**
  - `DANGEROUS` 危险操作（`systemctl restart`/`kill`/`rm`/`chmod`…）**弹窗确认后执行**
  - `FORBIDDEN` 绝对禁止（`rm -rf /`/`mkfs`/`dd` 写盘/fork bomb…）**直接拦截**
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

浏览器打开 http://127.0.0.1:8765

> 完整的环境准备、数据库建库、后台常驻、停止/重启、备份恢复与故障排查，见 [DEPLOY.md](DEPLOY.md)。

1. 进入 **设置** → 填写 API Base URL、API Key、模型名称与可选模型列表（每行一个）→ 点「测试连接」→「保存」
2. 进入 **服务器管理** → 添加 Linux 服务器 → 点「测试连接」确认 SSH 可达
3. 回到 **对话** → 顶部选择目标服务器与使用模型 → 描述你的问题

> 服务默认仅监听 `127.0.0.1:8765`，不对外网开放。如需远程访问，请自行加反向代理 + HTTPS + 鉴权。

## 目录结构

```
shellpilot/
├── server/                 # 后端（Python + aiohttp + paramiko）
│   ├── app.py              # 入口：应用装配、静态文件、启动
│   ├── routes.py           # REST API + WebSocket 路由
│   ├── agent.py            # ReAct Agent 循环、工具定义与调度
│   ├── safety.py           # 命令安全分级（白名单/黑名单/绝对禁止）
│   ├── ssh_manager.py      # SSH 连接池与命令执行
│   ├── llm_client.py       # LLM 流式调用（OpenAI 协议）
│   ├── config.py           # 配置读写（config.json）
│   └── session.py          # PostgreSQL 会话与审计日志存储
├── web/                    # 前端（原生 HTML/CSS/JS，零构建）
│   ├── index.html
│   ├── css/style.css
│   └── js/{app,chat,servers,settings}.js
├── tests/                  # 测试
│   ├── test_agent_flow.py  # Agent 循环与安全策略（Mock LLM/SSH，无需真实环境）
│   └── test_integration.py # 端到端（需先启动 app.py）
├── DEPLOY.md              # 启动 / 部署 / 运维手册
├── config.json             # 运行时生成（含密钥，勿提交）
└── requirements.txt
```

## 配置项

`config.json` 由程序自动生成，也可直接编辑：

| 配置 | 说明 | 默认 |
|---|---|---|
| `llm.base_url` | OpenAI 协议接口地址（如 `https://api.deepseek.com/v1`） | `https://api.deepseek.com/v1` |
| `llm.api_key` | API 密钥 | 空 |
| `llm.model` | 兜底/默认模型名，如 `deepseek-chat` | `deepseek-chat` |
| `llm.models` | 可选模型列表；对话页下拉框据此切换 | `[]` |
| `llm.timeout` | 请求超时秒 | `120` |
| `llm.history_budget_chars` | 回灌模型的历史字符上限，`0` = 全量发送 | `0` |

> 温度、max_tokens、迭代轮次等 Agent 内部参数已固化为代码常量（`server/agent.py`、`server/llm_client.py`），不作为配置暴露。API Key 留空或 `********` 保存表示不修改原密钥。

可用环境变量 `SHELLPILOT_CONFIG` 指定配置文件路径；`SHELLPILOT_PG_DSN` 指定 PostgreSQL 连接串（默认 `host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456`）。旧变量名 `YCAI_CONFIG` / `YCAI_PG_DSN` 仍然兼容。

## 数据库

会话与消息存于 PostgreSQL 的 `shellpilot` 库（首次启动自动建库表），两张表：

- `sessions`：会话元数据（id / 标题 / 目标服务器 / 时间戳）
- `messages`：每条消息与命令审计（角色 / 内容 / 工具参数 / 命令输出 / 退出码 / 风险等级 / 时间戳），按 `seq` 排序保存，支持完整回放

> `config.json`（服务器凭据 + LLM Key）仍是本地文件；数据库只存对话和命令日志。两者都不要外传。

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

# 集成测试（Mock LLM HTTP 服务；需先启动 python server/app.py）
python tests/test_integration.py
```

> 集成测试会临时把 LLM 指向 Mock 服务、结束后恢复原 Base URL/模型/超时；全程不读取、不修改真实 API Key。测试前仍建议备份 `config.json`。

## 安全说明

- SSH 密码 / 私钥 / LLM Key 只存于服务端 `config.json`，前端接口返回时脱敏
- 危险操作确认有 300 秒超时，超时自动取消
- 所有执行过的命令与输出都持久化在 PostgreSQL，可审计回查
- 安全分级是**规则白名单制**：不在白名单内的命令一律要求确认（宁可多问，不可误执行）

## 后续可扩展

- 定时巡检 / 告警推送
- 多服务器并行排查（如集群问题）
- 排障知识库（把历史结论沉淀为可复用经验）
- 命令执行回滚 / 变更审批流
