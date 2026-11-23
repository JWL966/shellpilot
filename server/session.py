"""会话与消息审计日志：PostgreSQL 存储（替代原 JSON 文件）。

表结构：
  sessions  — 会话（id, title, server_id, created_at, updated_at）
  messages  — 消息/审计（id, session_id, seq, role, content, tool_call_id, name,
               arguments, result, exit_code, success, risk, ts, extra）

对外 API 与原 session.py 保持一致，调用方无需改动。
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


# ---------- 会话 ----------

def create_session(title: str = "新会话", server_id: Optional[str] = None) -> dict[str, Any]:
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
                "INSERT INTO sessions (id, title, server_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s)",
                (session["id"], session["title"], session["server_id"],
                 session["created_at"], session["updated_at"]),
            )
    return session


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
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
        "id": s["id"], "title": s["title"], "server_id": s["server_id"],
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


def list_sessions() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.title, s.server_id, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY s.updated_at DESC
            """)
            rows = cur.fetchall()
    return [
        {
            "id": r["id"], "title": r["title"], "server_id": r["server_id"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def delete_session(session_id: str) -> bool:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            return cur.rowcount > 0
