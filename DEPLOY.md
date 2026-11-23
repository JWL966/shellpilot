# ShellPilot 启动与部署文档

面向：在本机（Windows）运行、以及部署到 Linux 服务器长期运行。

> 所有命令均在 Git Bash 下验证过（项目根目录为克隆下来的 `shellpilot` 目录）。
> 若使用 cmd / PowerShell，注意路径分隔符与 `taskkill` 参数：cmd 用 `/PID`，Git Bash 用 `//PID`。

---

## 0. 三分钟快速启动（已部署过的机器）

```bash
cd shellpilot
python server/app.py
```

浏览器打开 <http://127.0.0.1:8765>。

首次部署（新机器）请按第 1～3 节依次执行。

---

## 1. 环境要求

| 组件 | 要求 | 本机实测 |
|---|---|---|
| Python | ≥ 3.10 | 3.12.10 |
| PostgreSQL | ≥ 12 | 18.3（服务名 `postgresql-x64-18`，端口 5432） |
| 浏览器 | Chrome / Edge 等现代浏览器 | — |

Python 依赖（`requirements.txt`）：

| 包 | 作用 | 本机实测版本 |
|---|---|---|
| `aiohttp` | Web 服务 + WebSocket + 调 LLM | 3.14.5 |
| `paramiko` | SSH 连接与命令执行 | 5.1.0 |
| `psycopg[binary]` | PostgreSQL 驱动 | 3.3.9 |

---

## 2. 首次部署（Windows 本机）

### 2.1 安装 Python 依赖

```bash
cd E:/shellpilot

# 建议用虚拟环境（可选）
python -m venv .venv
source .venv/Scripts/activate      # Git Bash；cmd 用 .venv\Scripts\activate.bat

pip install -r requirements.txt

# 首次运行前生成配置文件（含密钥，已被 .gitignore 排除）
cp config.example.json config.json      # cmd: copy config.example.json config.json
```

验证：

```bash
python -c "import aiohttp, paramiko, psycopg; print('deps ok')"
```

### 2.2 准备 PostgreSQL

程序启动时**只自动建表，不会自动建库**（`server/session.py:60` 的 `init_db()` 执行 `CREATE TABLE IF NOT EXISTS`）。
所以第一次必须先创建数据库 `shellpilot`。

`psql` 未加入 PATH 时用完整路径（`C:\Program Files\PostgreSQL\18\bin\psql.exe`）：

```bash
"C:/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -h 127.0.0.1 -p 5432 \
  -c "CREATE DATABASE shellpilot;"
```

或者不用 psql，直接用 Python 建库：

```bash
python - <<'PY'
import psycopg
with psycopg.connect("host=127.0.0.1 port=5432 dbname=postgres user=postgres password=123456", autocommit=True) as c:
    with c.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname='shellpilot'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE shellpilot")
            print("created shellpilot")
        else:
            print("already exists")
PY
```

确认服务在跑：

```bash
sc query postgresql-x64-18 | grep STATE
# 应为 STATE : 4  RUNNING
```

> 生产环境请把密码改成环境变量传入的方式，不要硬编码在文档/命令历史里（见 3.2 节）。

### 2.3 启动服务

```bash
python server/app.py
```

预期输出（**首次启动自动创建管理员账号（写入数据库 `users` 表），密码只加密存储，明文仅打印这一次**）：

```
ShellPilot 启动: http://127.0.0.1:8765
首次启动已生成管理员账号（密码只加密存储，明文仅此一次，请妥善保存）:
  用户名: admin
  密码:   p--Qxxxxxxxxxxxx
浏览器打开上面地址登录使用。忘记密码: python server/app.py --reset-password
```

旧版本升级（config.json 里存有 `auth.username` / `auth.password_hash`）会打印「已把旧版管理员账号迁移到数据库用户表，原密码继续有效」，账号密码原样迁入 users 表，无需重置。若 config.json 里还有旧版 `llm` 配置或 `servers` 列表，会看到类似下面的迁移输出（迁给管理员，随后从文件清除）：

```
已把 2 条历史会话划归管理员账号
已把旧版 config.json 里的 1 台服务器迁入数据库（管理员名下），config.json 中已清除
已把旧版 config.json 里的 LLM 配置迁入数据库（管理员名下），config.json 中已清除
```

此时前台阻塞：**关闭终端 = 停止服务**。需要常驻见第 6 节。

### 2.4 验证启动成功

```bash
curl -s http://127.0.0.1:8765/api/health
# {"status":"ok"}   （健康检查无需登录）

# 其他接口一律要求登录会话；未登录应返回 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/config/llm
# 401

# 登录拿会话 Cookie（失败登录有 1 秒延迟，属正常反暴力破解行为）
curl -s -c cookies.txt -X POST -H "Content-Type: application/json" \
     -d '{"username":"admin","password":"启动日志里的密码"}' \
     http://127.0.0.1:8765/api/login
# {"ok": true, "username": "admin"}

curl -s -b cookies.txt http://127.0.0.1:8765/api/config/llm
# 能看到 base_url / model / models / has_api_key，api_key 已脱敏为 ********
```

浏览器打开 <http://127.0.0.1:8765>，未登录会跳转到登录页，用启动日志里的账号登录。忘记密码：`python server/app.py --reset-password`（旧密码与该账号所有会话立即失效）。然后：

1. **设置** 页填 API Base URL（须含 `/v1`）、API Key、默认模型名、可选模型列表（每行一个）→「测试连接」→「保存」；顺手在 **账号安全** 里把随机密码改成自己的
2. **服务器管理** 页添加 Linux 主机 →「测试连接」确认 SSH 可达（首次连接会记录主机指纹）
3. **对话** 页选择目标服务器与模型 → 提问

**多用户注册**：登录页右下有「注册新账号」入口，默认开放。给同事开账号直接让他们自助注册即可；不想对外开放时，把 `config.json` 里 `auth.allow_registration` 改为 `false` 并重启（登录页的注册入口随之隐藏）。每个账号的 **LLM 配置、服务器列表、会话历史完全按用户隔离**——新用户从空白配置开始，自己填 API Key、加自己的服务器；密码与会话也相互独立。Agent 执行命令前会校验服务器归属，无法操作别人的服务器。**公网部署务必关闭注册。**

---

## 3. 配置说明

`config.json` 只存全局选项（注册开关、SSH 指纹策略）；**LLM 配置与服务器列表已按用户隔离存入数据库**（在设置页 / 服务器管理页修改）。文件里遗留的 `llm` / `servers` 两段仅在旧版本升级时作为一次性迁移来源（迁给管理员），迁移完成后自动清空，**文件不再含任何密钥**。

### 3.1 LLM 配置（每用户一份，存数据库 `user_llm` 表）

字段含义不变，在 **设置** 页修改（`server/config.py` 业务层 + `server/session.py` 存储）：

| 字段 | 说明 |
|---|---|
| `base_url` | OpenAI 兼容接口地址，**必须包含 `/v1`**（或网关对应路径） |
| `api_key` | 走 `Authorization: Bearer <key>` |
| `model` | 默认/兜底模型；不在 `models` 里时也会自动出现在下拉框首位 |
| `models` | 对话页「使用模型」下拉框的候选列表 |
| `timeout` | 单次 LLM 请求超时（秒），范围 10～600 |
| `history_budget_chars` | 历史上下文字符上限；**0 = 不裁剪**（大上下文模型推荐），正数则超限后按整轮丢弃最旧内容 |

服务器列表同理，每用户一份（`servers` 表，含 SSH 凭据，接口返回时脱敏）。注册的新用户看到的是空白配置，需要自己填写——互不可见、互不影响。

### 3.2 上下文（历史消息）策略

同一会话的每一轮对话，都会把**之前所有轮次**（用户提问、AI 回复、工具调用参数与命令输出）一起发给模型——这是多轮追问能生效的前提。相关机制：

| 机制 | 位置 | 说明 |
|---|---|---|
| 历史加载 | `server/agent.py` `load_history()` | 每轮从 PostgreSQL 读出整个会话历史 |
| 配对修复 | `server/agent.py` `_sanitize_history()` | 剔除没有响应的 `tool_call`，防止 OpenAI 协议 400 |
| 长度裁剪 | `server/agent.py` `_trim_history()` | 受 `llm.history_budget_chars` 控制，**默认 0 = 不裁剪** |
| 中断补齐 | `server/agent.py` `_abort_remaining()` | 用户点「停止」时，为未执行的工具写占位响应 |

**默认不裁剪**：`history_budget_chars: 0` 时每轮把该会话全部历史原样发送，适合 1M 上下文这类大窗口模型。

只有在你主动填入正数（字符数）时才会裁剪，规则是：

- **按整轮**丢弃最旧的对话，当前这一轮永远完整保留，不会切断 `tool_calls` 与 `tool` 响应的配对。
- 被省略时会在请求里插入一条 system 提示，告知模型早期轮次已省略。
- 历史体积的主要来源是**命令输出**（单条上限 `OUTPUT_MAX_CHARS = 8000` 字符）。
- 字符数与 token 的粗略换算：中文约 1.5 字/token，英文与日志约 4 字符/token。
- 界面左侧的历史会话记录是**完整保存在数据库**的，裁剪只影响发给模型的上下文，不影响回看。

> 即使不裁剪，历史也只会随对话变长。如果模型真的拒收（返回 400 / 上下文超限），
> 在设置页把「历史上下文上限」填一个正数（如 `800000`）即可开启按轮裁剪。

### 3.3 密钥保存语义

接口保存时的密钥三态（`server/config.py:106`）：

- 传 `null` → 清除密钥
- 传空串或 `********` → **保持原密钥不变**（页面回显的就是 `********`）
- 传其他值 → 覆盖

### 3.4 环境变量

| 变量 | 作用 | 默认值 |
|---|---|---|
| `SHELLPILOT_CONFIG` | 指定配置文件路径 | `<项目根>/config.json` |
| `SHELLPILOT_PG_DSN` | PostgreSQL 连接串 | `host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456` |
| `SHELLPILOT_PASSWORD` | 首次建管理员账号时的初始密码（不落盘；users 表已有账号时忽略；适合脚本化部署） | users 表为空时生成随机密码并打印 |
| `SHELLPILOT_KNOWN_HOSTS` | SSH 主机指纹库路径 | `~/.shellpilot/known_hosts` |
| `SHELLPILOT_HOST_KEY_POLICY` | 指纹校验策略 `tofu`/`strict`/`auto` | 读 `ssh.host_key_policy`（`tofu`） |
| `SHELLPILOT_ALLOWED_HOSTS` | 额外放行的 Host 域名（逗号分隔） | 空（仅回环/localhost/IP 字面量） |
| `SHELLPILOT_ALLOWED_ORIGINS` | 额外放行的 Origin（逗号分隔，反代场景用） | 空（仅同源） |

用环境变量启动示例（避免密码写进配置）：

```bash
SHELLPILOT_PG_DSN="host=127.0.0.1 port=5432 dbname=shellpilot user=shellpilot_app password=***" \
python server/app.py
```

### 3.5 监听地址与端口

`server/app.py` 当前是**硬编码**：

```python
host = "127.0.0.1"
port = 8765
```

- 只监听本机回环，外网访问不到（这是安全默认值，别轻易改）。
- 需要换端口，改这两行即可（或改成读环境变量）。
- 所有接口（除 `/api/health` 与登录/注册页）都要求**登录会话**；并有 Origin / Host 校验（详见第 4 节与 README 安全说明）。
- 需要远程访问：不要直接把 `host` 改成 `0.0.0.0`，用反向代理 + HTTPS，见第 4.3 节。

---

## 4. 访问方式

### 4.1 本机访问

浏览器打开 `http://127.0.0.1:8765`，用启动日志里的账号登录即可。

### 4.2 局域网访问

改 `server/app.py` 的 `host` 为 `0.0.0.0`，重启后同网段设备访问 `http://<本机IP>:8765` 并登录。
**仅限受信内网**；Windows 防火墙需放行该端口：

```powershell
New-NetFirewallRule -DisplayName "ShellPilot 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

局域网注意事项：

- 访问方 Host 变成 `<本机IP>`（IP 字面量），登录认证照常生效，无需额外配置。
- 若通过**域名**访问本机（如 `shellpilot.lan`），默认会被 Host 校验拒绝（防 DNS rebinding），需放行：
  `set SHELLPILOT_ALLOWED_HOSTS=shellpilot.lan` 后重启。
- 登录密码与 Cookie 走明文 HTTP 传输，受信内网可接受；跨不可信网络请走 4.3 的反代 + HTTPS。

### 4.3 公网访问（务必加保护）

工具具备"在服务器上执行命令"的能力，**绝不能裸奔在公网**。最低要求：

1. 反向代理：Nginx / Caddy 终止 TLS
2. 访问鉴权：**内置登录认证已默认启用**；仍建议再加 `auth_basic` 或前置 OAuth / SSO 作为第二道防线
3. WebSocket 必须透传（`/ws` 需要 `Upgrade` 头）
4. 防火墙只放行 443
5. 反代改变了浏览器看到的 `Origin`/`Host`，需放行对应白名单，否则浏览器请求会被 403：
   ```bash
   export SHELLPILOT_ALLOWED_HOSTS=ops.example.com
   export SHELLPILOT_ALLOWED_ORIGINS=https://ops.example.com
   ```

Nginx 片段：

```nginx
server {
    listen 443 ssl;
    server_name ops.example.com;

    ssl_certificate     /etc/nginx/certs/ops.crt;
    ssl_certificate_key /etc/nginx/certs/ops.key;

    auth_basic           "ShellPilot";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass         http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_read_timeout 3600s;
    }
}
```

---

## 5. 停止 / 重启（Windows）

### 5.1 查端口占用

```bash
netstat -ano | grep ":8765.*LISTENING"
#   TCP    127.0.0.1:8765   0.0.0.0:0   LISTENING   39284
```

最后一列是 PID。

### 5.2 停止

```bash
taskkill //PID 39284 //F        # Git Bash
# cmd: taskkill /PID 39284 /F
```

一行版（Git Bash）：

```bash
PID=$(netstat -ano | grep ":8765.*LISTENING" | awk '{print $5}' | head -1); \
[ -n "$PID" ] && taskkill //PID "$PID" //F || echo "服务未在运行"
```

### 5.3 重启

```bash
# 停旧进程后再启动新代码
PID=$(netstat -ano | grep ":8765.*LISTENING" | awk '{print $5}' | head -1); \
[ -n "$PID" ] && taskkill //PID "$PID" //F
cd shellpilot && python server/app.py
```

> `pkill -f "server/app.py"` 在 Windows 上不可靠，用上面的 netstat + taskkill。

---

## 6. 后台常驻运行

### 6.1 Windows：后台启动 + 日志落盘

```bash
powershell -Command "Start-Process -FilePath 'python' -ArgumentList 'server/app.py' -WorkingDirectory 'E:\shellpilot' -RedirectStandardOutput 'E:\shellpilot\server.out.log' -RedirectStandardError 'E:\shellpilot\server.err.log' -WindowStyle Hidden"
```

看日志：

```bash
tail -f E:/shellpilot/server.out.log
```

> `*.log` 已在 `.gitignore` 中；日志可能含命令输出（甚至敏感信息），注意权限。

### 6.2 Windows：开机自启（任务计划）

```powershell
$action = New-ScheduledTaskAction -Execute "python" `
  -Argument "server/app.py" -WorkingDirectory "E:\shellpilot"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ShellPilot" -Action $action -Trigger $trigger `
  -Settings $settings -Description "ShellPilot AI 远程运维助手" -Force
```

（需要以管理员身份执行。也可用 `nssm install ShellPilot` 更省事。）

### 6.3 Linux：systemd

```bash
# 1) 依赖装进虚拟环境
cd /opt/shellpilot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 建库（只建一次）
sudo -u postgres createdb shellpilot

# 3) 服务单元
sudo tee /etc/systemd/system/shellpilot.service >/dev/null <<'EOF'
[Unit]
Description=ShellPilot AI Remote Ops Assistant
After=network.target postgresql.service

[Service]
Type=simple
User=shellpilot
WorkingDirectory=/opt/shellpilot
Environment=SHELLPILOT_PG_DSN=host=127.0.0.1 port=5432 dbname=shellpilot user=shellpilot_app password=CHANGE_ME
ExecStart=/opt/shellpilot/.venv/bin/python server/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now shellpilot
sudo systemctl status shellpilot
journalctl -u shellpilot -f
```

Linux 下如果要监听局域网，同样改 `server/app.py` 的 `host`，或加 Nginx 反代。

---

## 7. 数据库运维

### 7.1 表结构

启动时自动创建（`server/session.py` 的 `SCHEMA`，由 `init_db()` 执行）：

- `users`：登录账号（`id / username / password_hash / created_at / last_login_at`）
- `servers`：每用户的服务器列表（`id / user_id / host / port / username / auth_type / password / key_path / key_content / ...`，按 `user_id` 隔离）
- `user_llm`：每用户的 LLM 配置（`user_id / base_url / api_key / model / models / timeout / history_budget_chars`）
- `sessions`：会话（`id / user_id / title / server_id / created_at / updated_at`）
- `messages`：消息与命令审计（`session_id / seq / role / content / tool_call_id / name / arguments / result / exit_code / success / risk / ts / extra`）

> 注意：`CREATE TABLE IF NOT EXISTS` **不会给已存在的表补新列**。本次多用户升级用的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（sessions 补 user_id）会自动执行；此后若再改表结构，需手工 `ALTER TABLE` 或迁移（升级前先备份）。

### 7.2 备份 / 恢复

```bash
# 备份（自定义格式，推荐）
"C:/Program Files/PostgreSQL/18/bin/pg_dump.exe" -U postgres -h 127.0.0.1 -p 5432 \
  -d shellpilot -F c -f "E:/backup/shellpilot_$(date +%Y%m%d_%H%M).dump"

# 恢复（目标库需先存在）
"C:/Program Files/PostgreSQL/18/bin/pg_restore.exe" -U postgres -h 127.0.0.1 -p 5432 \
  -d shellpilot -c "E:/backup/shellpilot_20261123_1200.dump"
```

> **数据库现在包含全部敏感数据**：SSH 密码/私钥（`servers` 表）、LLM API Key（`user_llm` 表）与命令输出。迁移完成后 `config.json` 不再含密钥，备份文件按敏感数据管理（限权限、不外传）。

### 7.3 清理历史会话

```bash
python - <<'PY'
import psycopg
dsn = "host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456"
with psycopg.connect(dsn) as c:
    with c.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE updated_at < now() - interval '180 days'")
        print("deleted sessions:", cur.rowcount)
PY
```

`messages` 通过外键 `ON DELETE CASCADE` 自动跟删。

也可以在前端侧边栏逐个删除。

---

## 8. 测试

### 8.1 单元测试（不连真实 LLM / 服务器）

```bash
python tests/test_agent_flow.py
# ALL 5 TESTS PASSED
```

### 8.2 鉴权测试（自起隔离服务，不碰真实配置）

```bash
python tests/test_auth.py
# ALL AUTH TESTS PASSED
```

- 自动创建临时配置与 `shellpilot_test` 测试库（不存在时自建），跑完即删
- 覆盖：登录（错误/正确）、注册（成功自动登录/重名/弱密码/注册开关）、多用户会话隔离、会话 Cookie 与 Bearer、退出登录、修改密码、Origin/Host 校验、base_url 入口校验
- 失败登录有 1 秒延迟，测试会因此多花几秒，属正常

### 8.3 集成测试（需要服务已在跑）

```bash
SHELLPILOT_PASSWORD=test1234 python server/app.py   # 终端 A（users 表为空时以此密码建管理员）
SHELLPILOT_PASSWORD=test1234 python tests/test_integration.py   # 终端 B
# 注意：若测试库已有旧 admin 且密码不同，先用 --reset-password 重置或清空 users 表
# ALL INTEGRATION TESTS PASSED
```

它会：

- 起一个 Mock LLM（端口 8899）
- 把**运行中服务的** `base_url` 临时指向 Mock，跑完恢复 `base_url / model / models / timeout`
- **全程不读写真实 API Key**（不传 `api_key` 字段）
- 自己创建的测试服务器与会话会在结束时删除

⚠️ 若测试进程中途被杀，配置可能停留在 Mock 地址。恢复方法：在「设置」页重新保存一次正确 Base URL / 模型（密钥回显 `********` 会保持不变）。LLM 配置存数据库（`user_llm` 表），改 `config.json` 无效。

---

## 9. 常见故障排查

### 9.1 启动报 `Address already in use` / 端口被占

```bash
netstat -ano | grep ":8765.*LISTENING"
taskkill //PID <PID> //F
```

### 9.2 启动报数据库连接失败

报错形如 `connection failed ... database "shellpilot" does not exist` / 密码错误：

1. 服务是否在跑：`sc query postgresql-x64-18`
2. 库是否存在：第 2.2 节建库
3. 连接串是否正确：检查 `SHELLPILOT_PG_DSN` 或 `server/session.py:20` 的默认值

> 注：Windows 下 PostgreSQL 的中文报错直接打印可能乱码，看 `type` 与关键字即可（`does not exist` / `password`）。

### 9.3 对话一直"模型未返回内容，请检查 LLM 配置或换个说法重试"

按顺序排查：

1. **设置** 页点「测试连接」——失败时看 HTTP 状态：
   - `401`：Key 错/过期 → 重填
   - `404`：Base URL 少了 `/v1` 或路径不对
   - `503 model_not_found`：模型名不在网关支持列表 → 换模型
2. 模型名是否与网关一致（如 `deepseek-v4.1-flash`、`kimi-k3`）
3. `timeout` 是否太小（慢模型可调到 300）
4. 网关是否要求特殊鉴权头（本工具只发 `Authorization: Bearer`）

### 9.4 SSH 连接失败

```text
SSH 连接失败: ...
```

排查：主机可达（`ping`/`telnet <ip> 22`）、端口、用户名、密码/私钥格式（支持 Ed25519 / ECDSA / RSA，PEM 或 OpenSSH 格式）。

主机指纹采用 **TOFU** 策略（首次连接记录到 `~/.shellpilot/known_hosts`，之后指纹变化即拒绝，防中间人）。若服务器确实**重装过系统**导致指纹不符被拒，可在「服务器管理」→ 该服务器卡片 →「重置指纹」，或删掉指纹文件里对应条目后重连；没有重装却报指纹变化，请先排查网络环境。

### 9.5 页面打不开 / 样式丢失 / 404

`server/app.py:15` 依赖项目目录结构（`web/` 与 `server/` 同级）。不要把 `web/` 移走或只复制 `server/` 目录。
若是反向代理，确认 `/css/`、`/js/`、`/ws` 都没被拦截。

### 9.6 WebSocket 反复"连接断开，重连中…"

前端地址是自动推导的 `ws://<当前host>/ws`。经过反代时必须透传 `Upgrade`/`Connection` 头（见 4.3），并放宽读超时。

### 9.7 危险命令没弹确认 / 只读命令却要确认

安全分级在 `server/safety.py`，规则白名单制：**不在白名单的未知命令一律按危险处理**。
需要放行某类只读命令就往白名单加正则；先跑单元测试确认没误判。

### 9.8 忘记登录密码 / 打开页面要求登录

- 打开页面被重定向到登录页、或接口全部 401：正常行为，用启动日志里打印的账号登录。
- 忘记密码：服务端执行 `python server/app.py --reset-password`，会生成新随机密码并打印（旧密码与该账号所有已登录会话立即失效）。
- 脚本化部署可用环境变量：启动时 `set SHELLPILOT_PASSWORD=你的密码`（仅首次建管理员账号时生效；账号已存在请用 `--reset-password`）。
- 登录失败有 1 秒延迟再返回，是防暴力破解设计，不是卡死。
- 注册入口不见了/无法注册：检查 `config.json` 的 `auth.allow_registration` 是否为 `false`（关闭后登录页隐藏注册入口，注册页提交返回 403）。

---

## 10. 升级 / 更新流程

1. 备份：`config.json` + 数据库（见 7.2）
2. 拉取/覆盖代码（保持 `web/`、`server/`、`tests/` 目录结构）
3. 更新依赖：`pip install -r requirements.txt`
4. 若有表结构变更：按变更说明执行 `ALTER TABLE`（本次多用户升级的 sessions 补列 / 新建 servers、user_llm 表会自动完成）
5. 重启服务（第 5.3 节）
6. 验证：`/api/health`、设置页「测试连接」、发一条只读排查消息

回滚：恢复备份代码 + `config.json` + 数据库 dump，重启。

---

## 11. 安全检查清单

- [ ] `config.json` 不入库、不外传，权限仅限服务账号可读（迁移后只含注册开关/SSH 策略，无密钥）
- [ ] **数据库按敏感数据管理**：SSH 凭据、LLM API Key、密码哈希、命令输出都在库里，限权限、不外传
- [ ] 首次启动后立即登录并修改随机密码（设置 → 账号安全）
- [ ] 公网部署关闭注册：`config.json` 里 `auth.allow_registration: false`（虽然各用户数据已隔离，仍避免陌生人乱注册）
- [ ] 服务只监听 `127.0.0.1`；公网访问必须走反代 + HTTPS（登录认证已内置）
- [ ] 数据库使用专用账号（非 `postgres` 超级用户）并限制来源
- [ ] SSH 使用最小权限账号（不建议 root），保持主机指纹 TOFU 校验开启
- [ ] 危险操作确认机制保持开启（默认 300 秒超时自动取消）
- [ ] 定期清理/归档历史会话；备份文件按敏感数据保管
- [ ] 变更前先备份数据库（见 7.2）

---

## 附：目录与关键文件速查

```
E:\shellpilot\
├── server\app.py            # 入口（host/port 硬编码在此；--reset-password 也在这）
├── server\auth.py           # 登录认证、会话管理、Origin/Host 校验（登录页 HTML 在此）
├── server\routes.py         # REST + WebSocket（/api/login、/ws 入口）
├── server\agent.py          # ReAct 循环（MAX_ITERATIONS=15, CMD_TIMEOUT=30）
├── server\safety.py         # 命令风险分级
├── server\ssh_manager.py    # SSH 连接池 + 主机指纹 TOFU
├── server\llm_client.py     # LLM 调用（模型选择在此生效）
├── server\config.py         # config.json 读写（密码哈希、模型列表、密钥三态）
├── server\session.py        # PostgreSQL 表结构与读写（DSN 在 20 行）
├── web\                     # 前端静态资源（不可分离）
├── tests\test_agent_flow.py # 单元测试
├── tests\test_auth.py       # 鉴权测试（临时配置 + 独立库 shellpilot_test）
├── tests\test_integration.py# 集成测试（需服务在跑，SHELLPILOT_PASSWORD 登录）
├── config.json              # 运行时生成，含密钥
└── requirements.txt
```
