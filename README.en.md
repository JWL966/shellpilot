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

Then open http://127.0.0.1:8765

> Detailed setup, background/service running, stop & restart, backup and troubleshooting: [DEPLOY.md](DEPLOY.md) (Chinese).

1. **Settings** → enter API base URL, API key, default model and optional model list (one per line) → **Test connection** → **Save**
2. **Servers** → add a Linux host → **Test connection** to confirm SSH is reachable
3. **Chat** → pick the target server and model at the top → describe your problem

> The server listens on `127.0.0.1:8765` only. For remote access, put it behind a reverse proxy with HTTPS and authentication.

## Project layout

```
shellpilot/
├── server/                 # backend (Python + aiohttp + paramiko)
│   ├── app.py              # entry point: app wiring, static files, startup
│   ├── routes.py           # REST API + WebSocket routes
│   ├── agent.py            # ReAct loop, tool definitions and dispatch
│   ├── safety.py           # command risk classification (allow / deny / forbid)
│   ├── ssh_manager.py      # SSH connection pool and command execution
│   ├── llm_client.py       # streaming LLM client (OpenAI protocol)
│   ├── config.py           # config.json read/write
│   └── session.py          # PostgreSQL session and audit-log storage
├── web/                    # frontend (plain HTML/CSS/JS, no build step)
│   ├── index.html
│   ├── css/style.css
│   └── js/{app,chat,servers,settings}.js
├── tests/                  # tests
│   ├── test_agent_flow.py  # agent loop and safety policy (mock LLM/SSH, no real env needed)
│   └── test_integration.py # end-to-end (requires a running app.py)
├── DEPLOY.md              # setup / deployment / operations guide (Chinese)
├── config.example.json     # template — copy to config.json
├── config.json             # created at runtime (holds secrets, never commit)
└── requirements.txt
```

## Configuration

`config.json` is created on first run and can also be edited by hand:

| Key | Meaning | Default |
|---|---|---|
| `llm.base_url` | OpenAI-compatible endpoint (e.g. `https://api.deepseek.com/v1`) | `https://api.deepseek.com/v1` |
| `llm.api_key` | API key | empty |
| `llm.model` | fallback/default model name, e.g. `deepseek-chat` | `deepseek-chat` |
| `llm.models` | selectable models shown in the chat dropdown | `[]` |
| `llm.timeout` | request timeout in seconds | `120` |
| `llm.history_budget_chars` | max characters of history replayed to the model; `0` = send everything | `0` |

> Agent internals such as temperature, max_tokens and iteration count are constants in `server/agent.py` and `server/llm_client.py`, not settings. Saving an empty or `********` API key leaves the stored key unchanged.

Environment variables: `SHELLPILOT_CONFIG` (config file path) and `SHELLPILOT_PG_DSN` (PostgreSQL DSN, default `host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456`). The legacy names `YCAI_CONFIG` / `YCAI_PG_DSN` are still accepted.

## Database

Sessions and messages live in the `shellpilot` database in two tables:

- `sessions` — metadata (id, title, target server, timestamps)
- `messages` — every message plus command audit data (role, content, tool arguments, output, exit code, risk level, timestamp), ordered by `seq` for full replay

> `config.json` (server credentials + LLM key) stays a local file; the database only holds conversations and command logs. Don't share either of them.

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

# integration tests (mock LLM HTTP server; start `python server/app.py` first)
python tests/test_integration.py
```

> The integration test temporarily points the LLM at a mock server and restores the original base URL/model/timeout afterwards. It never reads or modifies your real API key. Back up `config.json` first anyway.

## Security notes

- SSH passwords / keys and the LLM key are stored only server-side in `config.json`; API responses mask them
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
