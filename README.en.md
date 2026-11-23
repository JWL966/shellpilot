# ShellPilot

> Drive your Linux servers by chat. An AI agent that investigates over SSH and only takes action after you say yes.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[中文](README.md) | **English**

ShellPilot is a web-based conversational remote-ops tool. Describe the problem in plain language, and an AI agent connects over SSH, plans its own investigation steps, runs diagnostic commands, and reports a conclusion — then fixes things after your confirmation.

```
You: why won't nginx start?
AI:  ❯ systemctl status nginx        [read-only · auto]
     ❯ journalctl -u nginx -n 100    [read-only · auto]
     ❯ ss -tlnp | grep :80           [read-only · auto]
     → Conclusion: port 80 is held by httpd, so nginx failed to bind…
You: then stop httpd and restart nginx
AI:  ❯ systemctl stop httpd          [dangerous · waiting for confirmation]
```

> The web UI is currently Chinese-only; this English README describes the same system.

## Features

- **Conversational ops**: the agent plans steps on its own (status → logs → processes → ports → resources) and loops until it reaches a conclusion. Once it has diagnosed the problem you can ask it to act on it.
- **Multi-server management**: add, edit and delete hosts in the UI; password or private-key auth; one-click connectivity test.
- **Three-tier command policy**:
  - `SAFE` — read-only commands (`ps`, `journalctl`, `tail`, `ss`, `df`, read-only `docker`/`kubectl`/`git`, …) run **automatically**
  - `DANGEROUS` — `systemctl restart`, `kill`, `rm`, `chmod`, … require an **explicit confirmation in the UI**
  - `FORBIDDEN` — `rm -rf /`, `mkfs`, writing to a disk with `dd`, fork bombs, … are **blocked outright**
  - Unknown commands are treated as dangerous by default; for chained commands (`&&`, `;`, `|`) the strictest level wins
- **Live view**: WebSocket streams the agent's reasoning, every command, and its full output as collapsible cards
- **Bring your own LLM**: any OpenAI Chat Completions–compatible endpoint (DeepSeek, Qwen, Kimi, self-hosted…). Set base URL, key and a model list in Settings; switch models from a dropdown above the chat box
- **Persistent sessions**: conversations and a command audit trail live in PostgreSQL — revisit and continue any session later
- **Chronological rendering**: the final answer always appears after the commands that produced it
- **Multi-turn context**: the full session history (including command output) is sent to the model by default, which suits large-context models; you can set a budget in Settings to trim whole turns instead

## Quick start

Requirements: Python 3.10+, PostgreSQL 13+ (sessions and audit log).

Tables are created automatically, but the database itself is not — create it first:

```bash
createdb shellpilot                  # or create it with psql / pgAdmin
pip install -r requirements.txt
cp config.example.json config.json   # Windows: copy config.example.json config.json
python server/app.py
```

The startup log prints the server address; **the first start creates an admin account automatically** (accounts live in the database `users` table; the password is stored hashed and the plaintext is printed only this once):

```
ShellPilot 启动: http://127.0.0.1:8765
首次启动已生成管理员账号（密码只加密存储，明文仅此一次，请妥善保存）:
  用户名: admin
  密码:   xxxxxxxxxxxxxxxx
```

Open http://127.0.0.1:8765 in a browser and sign in with those credentials. Forgot the password? Run `python server/app.py --reset-password` on the server (the old password and that account's sessions are invalidated immediately). You can change the password in **Settings → 账号安全** after logging in.

**Multi-user**: the login page has a "register" link (open by default; disable via `auth.allow_registration`). Each account has **fully isolated** LLM config, server list, chat history and password — a newly registered user starts blank and enters their own API key and servers; the admin cannot see or touch other users' data. Legacy single-user data (account/LLM/servers in config.json) migrates into the database automatically on first start and is assigned to the admin.

> Detailed setup, background/service running, stop & restart, backup and troubleshooting: [DEPLOY.md](DEPLOY.md) (Chinese).

1. **Settings** → enter API base URL, API key, default model and optional model list (one per line) → **Test connection** → **Save**
2. **Servers** → add a Linux host → **Test connection** to confirm SSH is reachable
3. **Chat** → pick the target server and model at the top → describe your problem

> The server listens on `127.0.0.1:8765` only. Every endpoint (API, WebSocket, static assets) requires an authenticated session. For remote access, put it behind a reverse proxy with HTTPS and review the auth/Origin behavior described under "Security notes".

## Project layout

```
shellpilot/
├── server/                 # backend (Python + aiohttp + paramiko)
│   ├── app.py              # entry point: app wiring, access control, static files, startup
│   ├── auth.py             # login auth + session management + Origin/Host validation (CSWSH/CSRF/DNS-rebinding)
│   ├── routes.py           # REST API + WebSocket routes
│   ├── agent.py            # ReAct loop, tool definitions and dispatch
│   ├── safety.py           # command risk classification (allow / deny / forbid)
│   ├── ssh_manager.py      # SSH connection pool, command execution, host-key pinning (TOFU)
│   ├── llm_client.py       # streaming LLM client (OpenAI protocol)
│   ├── config.py           # registration/SSH-policy options + per-user LLM/server business logic
│   └── session.py          # PostgreSQL storage: accounts, per-user LLM/servers, sessions & audit log
├── web/                    # frontend (plain HTML/CSS/JS, no build step)
│   ├── index.html
│   ├── css/style.css
│   └── js/{app,chat,servers,settings}.js
├── tests/                  # tests
│   ├── test_agent_flow.py  # agent loop and safety policy (mock LLM/SSH, no real env needed)
│   ├── test_auth.py        # auth & access control (spins up an isolated app; needs local PostgreSQL)
│   └── test_integration.py # end-to-end (requires a running app.py)
├── DEPLOY.md              # setup / deployment / operations guide (Chinese)
├── config.example.json     # template — copy to config.json
├── config.json             # created at runtime (holds secrets, never commit)
└── requirements.txt
```

## Configuration

`config.json` holds only global options; **LLM config and the server list are stored per user in the database** (edited via the Settings / Servers pages). The `llm` / `servers` sections in the file are a one-time migration source when upgrading from an older version, and are cleared after migration:

| Key | Meaning | Default |
|---|---|---|
| `auth.allow_registration` | whether open registration is enabled (shows the register link; `false` = only the admin creates accounts server-side) | `true` |
| `ssh.host_key_policy` | SSH host-key policy: `tofu` (pin on first connect, reject on change) / `strict` / `auto` (no check) | `tofu` |

Per-user LLM settings (`base_url` / `api_key` / `model` / `models` / `timeout` / `history_budget_chars`) and the server-list fields keep their meaning — they just moved from config.json into the `user_llm` / `servers` tables, one copy per user.

> Agent internals such as temperature, max_tokens and iteration count are constants in `server/agent.py` and `server/llm_client.py`, not settings. Saving an empty or `********` API key leaves the stored key unchanged.

Environment variables: `SHELLPILOT_CONFIG` (config file path), `SHELLPILOT_PG_DSN` (PostgreSQL DSN, default `host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456`), `SHELLPILOT_PASSWORD` (initial admin password when the users table is empty; not written to disk, ignored once the account exists), `SHELLPILOT_KNOWN_HOSTS` (SSH known-hosts path, default `~/.shellpilot/known_hosts`), `SHELLPILOT_HOST_KEY_POLICY` (override the host-key policy). The legacy names `YCAI_CONFIG` / `YCAI_PG_DSN` are still accepted.

## Database

Accounts, per-user LLM/server config, sessions and messages all live in the `shellpilot` database (tables are created on first start):

- `users` — login accounts (username / PBKDF2 password hash / last login)
- `servers` — each user's own server list (including SSH credentials, isolated by `user_id`)
- `user_llm` — each user's own LLM config (base URL / API key / models, isolated by `user_id`)
- `sessions` — metadata (id, owner, title, target server, timestamps)
- `messages` — every message plus command audit data (role, content, tool arguments, output, exit code, risk level, timestamp), ordered by `seq` for full replay

> Older versions kept server credentials and the LLM key in `config.json`; on first start after upgrading they are migrated into the database (under the admin account) and removed from the file. SSH passwords / keys and the LLM key now live only in the database; API responses always mask them.

## Tools available to the agent

| Tool | Purpose |
|---|---|
| `run_command` | run any shell command (subject to the safety policy) |
| `read_file` | read the last N lines of a file (`tail -n`, safe for huge files) |
| `get_service_status` | service status + last 100 log lines (`systemctl status` + `journalctl`) |
| `check_port` | which process holds a port (`ss -tlnp`) |
| `check_resources` | CPU / memory / disk |
| `list_services` | list systemd units |

The high-level tools reduce the chance of the model composing a broken command; it can also fall back to `run_command` for anything else.

## Tests

```bash
# unit tests (mock LLM + mock SSH, no real servers)
python tests/test_agent_flow.py

# auth & access control (isolated app + test database, never touches real config)
python tests/test_auth.py

# integration tests (mock LLM HTTP server; start the app and the test with the
# same SHELLPILOT_PASSWORD)
SHELLPILOT_PASSWORD=test1234 python server/app.py
SHELLPILOT_PASSWORD=test1234 python tests/test_integration.py
```

> The integration test temporarily points the LLM at a mock server and restores the original base URL/model/timeout afterwards. It never reads or modifies your real API key. Back up `config.json` first anyway.

## Security notes

**Authentication & access control** (`server/auth.py`):

- **Login auth**: accounts live in the PostgreSQL `users` table; when it is empty on first start an admin account is created (default name `admin`) with a random password stored as a PBKDF2 hash — the plaintext is printed exactly once (or set `SHELLPILOT_PASSWORD` beforehand). Every API call, WebSocket handshake and static asset requires an authenticated session; only `/api/health` and the login/register pages are open (neither returns sensitive data)
- **Registration & multi-user**: the "register" link on the login page is enabled by default (`auth.allow_registration: false` turns it off; the register page then stays visible but rejects submissions). Each account's **LLM config, server list and chat history are fully isolated per user** — a new user starts blank with their own API key and servers; sessions and passwords are independent too, and changing a password only drops that account's other sessions. The agent verifies server ownership before running any tool. **For public deployments, disable registration**
- **Sessions**: login sets an HttpOnly + SameSite=Strict session cookie (30-day sliding expiry); scripts can use `Authorization: Bearer <session token>` instead. Logout invalidates the session immediately; changing the password drops that account's other sessions
- **CSWSH protection**: WebSocket handshakes carrying an `Origin` must be same-origin — a malicious web page can no longer hijack your browser's connection to the local WS and run commands through the agent
- **CSRF protection**: state-changing requests (POST/PUT/DELETE, including the login endpoint itself) carrying an `Origin` must be same-origin, blocking cross-site "simple requests"
- **DNS-rebinding protection**: the `Host` header must be a loopback address, `localhost` or an IP literal — attacker domains resolving to 127.0.0.1 are rejected. LAN hostnames can be allow-listed via `SHELLPILOT_ALLOWED_HOSTS` (comma-separated); reverse-proxy origins via `SHELLPILOT_ALLOWED_ORIGINS`
- **Brute-force mitigation**: failed logins are delayed by a fixed 1 second; passwords are hashed with PBKDF2-SHA256 (200k iterations)
- **Secret-entry validation**: saving the LLM config rejects base URLs with embedded credentials or invalid schemes; plaintext-HTTP public gateways are allowed but flagged with a warning
- **Host-key TOFU**: the first SSH connection pins the host key to `~/.shellpilot/known_hosts`; any later mismatch is rejected (protects passwords from MITM). If the server was genuinely reinstalled, reset its fingerprint from the **Servers** page

**Other**:

- SSH passwords / keys and the LLM key are stored only in the PostgreSQL database (per user, `servers` / `user_llm` tables); API responses mask them. `config.json` holds no secrets after the one-time migration
- Confirming a dangerous command times out after 300 seconds and cancels itself
- Every command and its output is persisted in PostgreSQL for audit
- Classification is an **allow-list**: anything not explicitly read-only requires confirmation — ask too often rather than run once too many

## Roadmap

- Scheduled health checks and alerting
- Parallel investigation across multiple servers (e.g. cluster-wide issues)
- A troubleshooting knowledge base that turns past conclusions into reusable experience
- Command rollback / change-approval workflow

## License

MIT — see [LICENSE](LICENSE).
