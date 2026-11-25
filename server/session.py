"""会话与消息审计日志 + 多用户数据隔离：PostgreSQL 存储。

表结构：
  sessions  — 会话（id, user_id, title, server_id, created_at, updated_at）
  messages  — 消息/审计（id, session_id, seq, role, content, tool_call_id, name,
               arguments, result, exit_code, success, risk, ts, extra）
  users     — 登录账号（id, username, password_hash, created_at, last_login_at）
  servers   — 每用户的服务器列表（id, user_id, name, host, port, username,
               auth_type, password, passphrase, key_path, key_content, os, extra）
  user_llm  — 每用户的 LLM 配置（user_id, base_url, api_key, model, models,
               timeout, history_budget_chars）
"""
import json
import os
import time
import uuid
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

# 连接串：可用环境变量覆盖（SHELLPILOT_PG_DSN 优先，兼容旧名 YCAI_PG_DSN）
DSN = os.environ.get(
    "SHELLPILOT_PG_DSN",
    os.environ.get(
        "YCAI_PG_DSN",
        "host=127.0.0.1 port=5432 dbname=shellpilot user=postgres password=123456",
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新会话',
    server_id   TEXT,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_call_id TEXT,
    name         TEXT,
    arguments    JSONB,
    result       TEXT,
    exit_code    INTEGER,
    success      BOOLEAN,
    risk         TEXT,
    ts           DOUBLE PRECISION NOT NULL,
    extra        JSONB
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    last_login_at DOUBLE PRECISION
);
-- 旧库升级：sessions 补 user_id 列（历史会话随后由启动迁移划给管理员）
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INTEGER;
-- 每用户的服务器列表（凭据入库存放；迁移自旧版 config.json）
CREATE TABLE IF NOT EXISTS servers (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    port        INTEGER NOT NULL DEFAULT 22,
    username    TEXT NOT NULL DEFAULT '',
    auth_type   TEXT NOT NULL DEFAULT 'password',
    password    TEXT,
    passphrase  TEXT,
    key_path    TEXT,
    key_content TEXT,
    os          TEXT,
    extra       JSONB,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_servers_user ON servers(user_id);
-- 每用户的 LLM 配置（迁移自旧版 config.json）
CREATE TABLE IF NOT EXISTS user_llm (
    user_id              INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    base_url             TEXT NOT NULL DEFAULT '',
    api_key              TEXT NOT NULL DEFAULT '',
    model                TEXT NOT NULL DEFAULT '',
    models               JSONB NOT NULL DEFAULT '[]',
    timeout              INTEGER NOT NULL DEFAULT 120,
    history_budget_chars BIGINT NOT NULL DEFAULT 0,
    updated_at           DOUBLE PRECISION NOT NULL
);
"""

_initialized = False


def _connect() -> psycopg.Connection:
    conn = psycopg.connect(DSN, connect_timeout=5, row_factory=dict_row)
    return conn


def init_db() -> None:
    """建表（幂等），服务启动时调用一次。"""
    global _initialized
    if _initialized:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
    _initialized = True


# ---------- 用户 ----------

def count_users() -> int:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            return int(cur.fetchone()["n"])


def get_user_by_name(username: str) -> Optional[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            return cur.fetchone()


def create_user(username: str, password_hash: str) -> Optional[dict[str, Any]]:
    """创建用户；用户名已存在（唯一冲突）返回 None。"""
    init_db()
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,%s) RETURNING *",
                    (username, password_hash, time.time()),
                )
                return cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None


def update_user_password(user_id: int, password_hash: str) -> None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (password_hash, user_id))


def touch_last_login(user_id: int) -> None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = %s WHERE id = %s",
                        (time.time(), user_id))


def get_user_profile(user_id: int) -> Optional[dict[str, Any]]:
    """账号页展示的用户信息（不含密码哈希）。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, created_at, last_login_at FROM users WHERE id = %s",
                (user_id,))
            row = cur.fetchone()
    if not row:
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id = %s", (user_id,))
            n_sessions = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM servers WHERE user_id = %s", (user_id,))
            n_servers = int(cur.fetchone()["n"])
    return {
        "id": row["id"],
        "username": row["username"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "session_count": n_sessions,   # 历史会话数
        "server_count": n_servers,     # 我的服务器数
    }


# ---------- 服务器（按用户隔离）----------

# servers 表的固定列；其余字段收进 extra JSONB
_SERVER_COLS = ("name", "host", "port", "username", "auth_type",
                "password", "passphrase", "key_path", "key_content", "os")


def _server_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {"id": row["id"]}
    for k in _SERVER_COLS:
        if row.get(k) is not None:
            d[k] = row[k]
    if row.get("extra"):
        d.update(row["extra"])
    return d


def count_servers() -> int:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM servers")
            return int(cur.fetchone()["n"])


def list_servers(user_id: int) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM servers WHERE user_id = %s ORDER BY created_at, id",
                        (user_id,))
            rows = cur.fetchall()
    return [_server_row_to_dict(r) for r in rows]


def get_server(server_id: str, user_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """按 id 取服务器；给 user_id 时校验归属（非本人返回 None）。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("SELECT * FROM servers WHERE id = %s", (server_id,))
            else:
                cur.execute("SELECT * FROM servers WHERE id = %s AND user_id = %s",
                            (server_id, user_id))
            row = cur.fetchone()
    return _server_row_to_dict(row) if row else None


def create_server(user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """插入服务器记录。id 已存在会抛 UniqueViolation（调用方先 delete 保证幂等）。"""
    init_db()
    d = dict(data)
    sid = str(d.pop("id") or uuid.uuid4().hex[:12])
    cols: dict[str, Any] = {"id": sid}
    for k in _SERVER_COLS:
        v = d.pop(k, None)
        if k == "port":
            try:
                v = int(v) if v not in (None, "") else 22
            except (TypeError, ValueError):
                v = 22
        elif k == "auth_type" and v is None:
            v = "password"  # 非空约束兜底（本机模式由 config 层写 "none"）
        cols[k] = v
    extra = d or None  # 剩余未知字段存 extra
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO servers
                   (id, user_id, name, host, port, username, auth_type,
                    password, passphrase, key_path, key_content, os, extra,
                    created_at, updated_at)
                   VALUES (%(id)s,%(user_id)s,%(name)s,%(host)s,%(port)s,%(username)s,
                           %(auth_type)s,%(password)s,%(passphrase)s,%(key_path)s,
                           %(key_content)s,%(os)s,%(extra)s,%(ts)s,%(ts)s)""",
                {"user_id": int(user_id), "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
                 "ts": time.time(), **cols},
            )
    return get_server(sid)


def update_server(user_id: int, server_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """更新本人服务器（归属不符返回 None）。updates 与现有记录合并后整行重写。"""
    current = get_server(server_id, user_id)
    if not current:
        return None
    merged = dict(current)
    merged.pop("id", None)
    merged.update(dict(updates))
    merged.pop("id", None)
    # 切回 SSH 后，本机模式暂存的原认证方式已消费——丢弃继承的标记，防回写 extra；
    # host=None 是 config 层清掉的固化残留（本机→SSH 必须重填地址），恢复为
    # 空串占位以满足 host 非空约束（连接时会因地址无效而报错提示用户重填）
    if updates.get("_drop_auth_restore"):
        merged.pop("_auth_type_restore", None)
    updates.pop("_drop_auth_restore", None)
    d = dict(merged)
    cols: dict[str, Any] = {}
    for k in _SERVER_COLS:
        v = d.pop(k, None)
        if k == "port":
            try:
                v = int(v) if v not in (None, "") else 22
            except (TypeError, ValueError):
                v = 22
        if k == "auth_type" and v is None:
            v = "password"  # 非空约束兜底
        cols[k] = v
    extra = d or None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE servers SET name=%(name)s, host=%(host)s, port=%(port)s,
                       username=%(username)s, auth_type=%(auth_type)s, password=%(password)s,
                       passphrase=%(passphrase)s, key_path=%(key_path)s,
                       key_content=%(key_content)s, os=%(os)s, extra=%(extra)s,
                       updated_at=%(ts)s
                   WHERE id=%(id)s AND user_id=%(user_id)s""",
                {"id": server_id, "user_id": int(user_id),
                 "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
                 "ts": time.time(), **cols},
            )
    return get_server(server_id, user_id)


def delete_server(server_id: str, user_id: int) -> bool:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM servers WHERE id = %s AND user_id = %s",
                        (server_id, user_id))
            return cur.rowcount > 0


# ---------- 用户 LLM 配置（按用户隔离）----------

_LLM_FIELDS = ("base_url", "api_key", "model", "models", "timeout", "history_budget_chars")


def get_user_llm(user_id: int) -> Optional[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_llm WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "base_url": row["base_url"] or "",
        "api_key": row["api_key"] or "",
        "model": row["model"] or "",
        "models": list(row.get("models") or []),
        "timeout": row["timeout"] or 120,
        "history_budget_chars": row["history_budget_chars"] or 0,
    }


def upsert_user_llm(user_id: int, cfg: dict[str, Any]) -> None:
    init_db()
    vals = {
        "user_id": int(user_id),
        "base_url": str(cfg.get("base_url") or ""),
        "api_key": str(cfg.get("api_key") or ""),
        "model": str(cfg.get("model") or ""),
        "models": json.dumps([str(m) for m in (cfg.get("models") or [])], ensure_ascii=False),
        "timeout": cfg.get("timeout") or 120,
        "history_budget_chars": cfg.get("history_budget_chars") or 0,
        "ts": time.time(),
    }
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_llm
                   (user_id, base_url, api_key, model, models, timeout, history_budget_chars, updated_at)
                   VALUES (%(user_id)s,%(base_url)s,%(api_key)s,%(model)s,%(models)s,
                           %(timeout)s,%(history_budget_chars)s,%(ts)s)
                   ON CONFLICT (user_id) DO UPDATE SET
                       base_url=%(base_url)s, api_key=%(api_key)s, model=%(model)s,
                       models=%(models)s, timeout=%(timeout)s,
                       history_budget_chars=%(history_budget_chars)s, updated_at=%(ts)s""",
                vals,
            )


def backfill_sessions_owner(admin_user_id: int) -> int:
    """旧库升级：把没有归属的历史会话划给管理员，返回补写的行数。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET user_id = %s WHERE user_id IS NULL",
                        (int(admin_user_id),))
            return cur.rowcount


# ---------- 会话 ----------

def create_session(user_id: int, title: str = "新会话",
                   server_id: Optional[str] = None) -> dict[str, Any]:
    init_db()
    session = {
        "id": uuid.uuid4().hex[:16],
        "title": title,
        "server_id": server_id,
        "created_at": time.time(),
        "updated_at": time.time(),
        "messages": [],
    }
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (id, user_id, title, server_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (session["id"], int(user_id), session["title"], session["server_id"],
                 session["created_at"], session["updated_at"]),
            )
    return session


def get_session(session_id: str, user_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """取会话（含消息）；给 user_id 时校验归属（非本人返回 None）。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
            else:
                cur.execute("SELECT * FROM sessions WHERE id = %s AND user_id = %s",
                            (session_id, user_id))
            s = cur.fetchone()
            if not s:
                return None
            cur.execute("SELECT * FROM messages WHERE session_id = %s ORDER BY seq", (session_id,))
            rows = cur.fetchall()
    messages = []
    for r in rows:
        m: dict[str, Any] = {"role": r["role"]}
        if r["content"] is not None:
            m["content"] = r["content"]
        if r["tool_call_id"]:
            m["tool_call_id"] = r["tool_call_id"]
        if r["name"]:
            m["name"] = r["name"]
        if r["arguments"] is not None:
            m["arguments"] = r["arguments"]
        if r["result"] is not None:
            m["content"] = r["result"]  # 兼容前端渲染：tool 消息的结果放 content
        if r["exit_code"] is not None:
            m["exit_code"] = r["exit_code"]
        if r["success"] is not None:
            m["success"] = r["success"]
        if r["risk"]:
            m["risk"] = r["risk"]
        if r["ts"] is not None:
            m["ts"] = r["ts"]
        if r["extra"]:
            m.update(r["extra"])
        # assistant 的 tool_calls 原样存于 extra
        messages.append(m)
    return {
        "id": s["id"], "user_id": s.get("user_id"),
        "title": s["title"], "server_id": s["server_id"],
        "created_at": s["created_at"], "updated_at": s["updated_at"],
        "messages": messages,
    }


def save_session(session: dict[str, Any]) -> None:
    """兼容旧调用；新架构消息逐条追加，这里仅更新标题/时间。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET title=%s, server_id=%s, updated_at=%s WHERE id=%s",
                (session.get("title", "新会话"), session.get("server_id"),
                 time.time(), session["id"]),
            )


def append_message(session_id: str, message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """追加一条消息到会话。返回会话对象（保持旧接口语义）。"""
    init_db()
    m = dict(message)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sessions WHERE id = %s", (session_id,))
            if not cur.fetchone():
                return None
            cur.execute("SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM messages WHERE session_id = %s", (session_id,))
            seq = cur.fetchone()["next_seq"]

            extra: dict[str, Any] = {}
            if "tool_calls" in m:
                extra["tool_calls"] = m.pop("tool_calls")

            args = m.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}

            cur.execute(
                """INSERT INTO messages
                   (session_id, seq, role, content, tool_call_id, name, arguments,
                    result, exit_code, success, risk, ts, extra)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (session_id, seq,
                 m.get("role", "user"),
                 m.get("content"),
                 m.get("tool_call_id"),
                 m.get("name"),
                 json.dumps(args, ensure_ascii=False) if args is not None else None,
                 m.get("result"),
                 m.get("exit_code"),
                 m.get("success"),
                 m.get("risk"),
                 m.get("ts", time.time()),
                 json.dumps(extra, ensure_ascii=False) if extra else None),
            )

            # 首条用户消息作为标题
            if m.get("role") == "user":
                cur.execute("SELECT title FROM sessions WHERE id = %s", (session_id,))
                title = cur.fetchone()["title"]
                if title in ("新会话", "", None):
                    cur.execute("UPDATE sessions SET title=%s WHERE id=%s",
                                ((m.get("content") or "")[:40] or "新会话", session_id))
            cur.execute("UPDATE sessions SET updated_at=%s WHERE id=%s", (time.time(), session_id))
    return get_session(session_id)


def list_sessions(user_id: int) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.title, s.server_id, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.user_id = %s
                GROUP BY s.id
                ORDER BY s.updated_at DESC
            """, (user_id,))
            rows = cur.fetchall()
    return [
        {
            "id": r["id"], "title": r["title"], "server_id": r["server_id"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def delete_session(session_id: str, user_id: Optional[int] = None) -> bool:
    """删除会话；给 user_id 时只能删本人的。"""
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            else:
                cur.execute("DELETE FROM sessions WHERE id = %s AND user_id = %s",
                            (session_id, user_id))
            return cur.rowcount > 0
