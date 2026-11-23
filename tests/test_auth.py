"""认证与安全加固测试：自起完整应用（隔离配置 + 测试数据库），验证登录认证各层。

覆盖：登录（错误凭据/正确凭据/环境变量密码）、会话 Cookie 与 Bearer、
退出登录、修改密码、Origin 校验（CSWSH / 跨站简单请求 / 登录 CSRF）、
Host 校验（DNS rebinding）、base_url 入口校验。
运行：python tests/test_auth.py   （需本机 PostgreSQL 可用）
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER_DIR)

TMP_DIR = tempfile.mkdtemp(prefix="sp_auth_test_")
CFG_PATH = os.path.join(TMP_DIR, "config.json")
# 测试专用数据库（与 session.py 默认连接参数一致，仅库名不同）
PG_BASE = "host=127.0.0.1 port=5432 user=postgres password=123456"

with open(CFG_PATH, "w", encoding="utf-8") as f:
    json.dump({
        "llm": {"base_url": "", "api_key": "", "model": "", "models": [],
                "timeout": 60, "history_budget_chars": 0},
        "auth": {"username": "admin", "password_hash": ""},
        "ssh": {"host_key_policy": "tofu"},
        "servers": [],
    }, f, ensure_ascii=False)

# 必须在 import server 模块之前设置（config.py 在 import 时读取）
os.environ["SHELLPILOT_CONFIG"] = CFG_PATH
os.environ["SHELLPILOT_PG_DSN"] = PG_BASE + " dbname=shellpilot_test"
os.environ["SHELLPILOT_KNOWN_HOSTS"] = os.path.join(TMP_DIR, "known_hosts")
os.environ.pop("SHELLPILOT_PASSWORD", None)
os.environ.pop("SHELLPILOT_TOKEN", None)  # 旧变量不应再影响

import aiohttp
from aiohttp.test_utils import TestClient, TestServer


def ensure_test_db():
    import psycopg
    try:
        with psycopg.connect(PG_BASE + " dbname=shellpilot_test", autocommit=True) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError:
        with psycopg.connect(PG_BASE + " dbname=postgres", autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'shellpilot_test'").fetchone()
            if not row:
                conn.execute("CREATE DATABASE shellpilot_test")


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f"   [{detail}]" if detail and not cond else ""))
    return cond


async def ws_try(client: TestClient, headers: dict) -> int:
    """走 TestClient 握手（带上 Cookie jar），返回 HTTP 状态。"""
    try:
        await client.ws_connect("/ws", headers=headers)
        return 101
    except aiohttp.WSServerHandshakeError as e:
        return e.status


async def ws_try_raw(url: str, headers: dict) -> int:
    """独立 ClientSession 握手（不共享 Cookie jar），返回 HTTP 状态。"""
    try:
        async with aiohttp.ClientSession() as s:
            await s.ws_connect(url, headers=headers)
            return 101
    except aiohttp.WSServerHandshakeError as e:
        return e.status


async def login(client: TestClient, username: str, password: str):
    return await client.post("/api/login", json={"username": username, "password": password})


async def main() -> int:
    ensure_test_db()
    from app import create_app
    import config as config_store

    client = TestClient(TestServer(create_app()))
    await client.start_server()
    ok = True
    created_server_id = None
    try:
        # 重置出已知密码（也覆盖了 reset_password 逻辑）
        PW = config_store.reset_password()
        origin = f"http://{client.host}:{client.port}"

        # ---- 未登录访问（必须在登录前测，Cookie jar 此时尚为空）----
        r = await client.get("/api/sessions")
        ok &= check("未登录访问 API -> 401", r.status == 401, str(r.status))
        r = await client.get("/", allow_redirects=False)
        ok &= check("未登录访问首页 -> 302 /login",
                    r.status == 302 and r.headers.get("Location") == "/login",
                    f"{r.status} {r.headers.get('Location')}")
        r = await client.get("/login")
        body = await r.text()
        ok &= check("登录页公开可访问", r.status == 200 and "ShellPilot 登录" in body, str(r.status))

        # ---- 登录 ----
        r = await login(client, "admin", "wrong-password")
        ok &= check("错误密码登录 -> 401", r.status == 401, str(r.status))
        r = await login(client, "nobody", PW)
        ok &= check("错误用户名登录 -> 401", r.status == 401, str(r.status))
        r = await login(client, "admin", PW)
        set_cookie = r.headers.get("Set-Cookie", "")
        ok &= check("正确凭据登录 -> 200 并下发 HttpOnly Cookie",
                    r.status == 200 and "sp_session=" in set_cookie and "HttpOnly" in set_cookie,
                    f"{r.status} {set_cookie}")

        # ---- 会话 Cookie / Bearer ----
        r = await client.get("/api/servers")
        ok &= check("Cookie 会话访问 API -> 200", r.status == 200, str(r.status))
        r = await client.get("/", allow_redirects=False)
        ok &= check("已登录访问首页 -> 200", r.status == 200, str(r.status))

        # 取当前会话令牌（用 Bearer 方式验证脚本场景）
        cookies = client.session.cookie_jar.filter_cookies(client.make_url("/"))
        bearer = cookies["sp_session"].value if "sp_session" in cookies else None
        r = await client.get("/api/servers",
                             headers={"Authorization": f"Bearer {bearer}"})
        ok &= check("Bearer 会话令牌访问 API -> 200", r.status == 200, str(r.status))

        # ---- Origin / CSRF（含登录接口）----
        r = await client.post("/api/servers",
                              headers={"Origin": "http://evil.example"},
                              json={"name": "x", "host": "1.2.3.4", "username": "root"})
        ok &= check("跨站 POST（evil Origin + Cookie）-> 403", r.status == 403, str(r.status))
        r = await client.post("/api/servers",
                              headers={"Origin": "null"},
                              json={"name": "x", "host": "1.2.3.4", "username": "root"})
        ok &= check("Origin: null -> 403", r.status == 403, str(r.status))
        r = await client.post("/api/servers",
                              headers={"Origin": origin, "Content-Type": "text/plain"},
                              data=json.dumps({"name": "鉴权测试机", "host": "127.0.0.1",
                                               "port": 2200, "username": "root",
                                               "password": "x", "auth_type": "password"}),
                              )
        ok &= check("同源 text/plain 简单请求（带 Cookie）-> 正常创建",
                    r.status == 200, f"{r.status} {await r.text()}")
        created_server_id = (await r.json()).get("server", {}).get("id")

        # ---- DNS rebinding ----
        r = await client.get("/api/health", headers={"Host": "evil.example"})
        ok &= check("伪造 Host -> 403（健康检查也拦）", r.status == 403, str(r.status))
        r = await client.get("/api/health", headers={"Host": f"127.0.0.1:{client.port}"})
        ok &= check("正常 Host 健康检查 -> 200", r.status == 200, str(r.status))

        # ---- base_url 入口校验 ----
        r = await client.put("/api/config/llm", json={"base_url": "ftp://x/v1"})
        ok &= check("base_url 非 http(s) -> 400", r.status == 400, str(r.status))
        r = await client.put("/api/config/llm",
                             json={"base_url": "http://user:pass@evil.com/v1"})
        ok &= check("base_url 内嵌凭据 -> 400", r.status == 400, str(r.status))
        r = await client.put("/api/config/llm",
                             json={"base_url": "http://198.51.100.9:3000/v1"})
        data = await r.json()
        ok &= check("base_url 明文 HTTP 非回环 -> 保存成功但带告警",
                    r.status == 200 and any("明文" in w for w in data.get("warnings", [])),
                    f"{r.status} {data.get('warnings')}")
        r = await client.put("/api/config/llm", json={"timeout": "abc"})
        ok &= check("timeout 非数字 -> 400", r.status == 400, str(r.status))
        r = await client.get("/api/config/llm")
        data = await r.json()
        ok &= check("GET 配置带 warnings 字段", "warnings" in data, str(data.keys()))

        # ---- WebSocket ----
        st = await ws_try(client, {"Origin": "http://evil.example"})
        ok &= check("跨站 WS 握手（CSWSH）-> 403", st == 403, str(st))
        st = await ws_try_raw(f"ws://{client.host}:{client.port}/ws", {"Origin": origin})
        ok &= check("同源 WS 但未登录 -> 401", st == 401, str(st))
        st = await ws_try(client, {"Origin": origin,
                                   "Authorization": f"Bearer {bearer}"})
        ok &= check("同源 WS + 会话 -> 101 握手成功", st == 101, str(st))

        # ---- 退出登录 ----
        r = await client.post("/api/logout")
        ok &= check("退出登录 -> 200", r.status == 200, str(r.status))
        r = await client.get("/api/servers")
        ok &= check("退出后 API -> 401", r.status == 401, str(r.status))
        # Bearer 会话令牌也应失效
        async with aiohttp.ClientSession() as raw:
            r = await raw.get(f"{client.make_url('/api/servers')}",
                              headers={"Authorization": f"Bearer {bearer}"})
            ok &= check("退出后 Bearer 会话令牌 -> 401", r.status == 401, str(r.status))

        # ---- 环境变量密码优先 ----
        os.environ["SHELLPILOT_PASSWORD"] = "env-pass-12345678"
        r = await login(client, "admin", "env-pass-12345678")
        ok &= check("SHELLPILOT_PASSWORD 环境变量密码可登录", r.status == 200, str(r.status))
        del os.environ["SHELLPILOT_PASSWORD"]

        # ---- 修改密码（当前有效密码是 PW，env 变量已删除）----
        r = await client.post("/api/auth/password",
                              json={"old_password": "wrong", "new_password": "new-pass-8888"})
        ok &= check("原密码错误 -> 400", r.status == 400, str(r.status))
        r = await client.post("/api/auth/password",
                              json={"old_password": PW, "new_password": "short"})
        ok &= check("新密码过短 -> 400", r.status == 400, str(r.status))
        r = await client.post("/api/auth/password",
                              json={"old_password": PW, "new_password": "new-pass-8888"})
        ok &= check("修改密码 -> 200", r.status == 200, str(r.status))
        r = await client.get("/api/servers")
        ok &= check("修改后当前会话仍有效 -> 200", r.status == 200, str(r.status))
        # 旧密码不能再登录
        r = await login(client, "admin", PW)
        ok &= check("旧密码登录 -> 401", r.status == 401, str(r.status))
        r = await login(client, "admin", "new-pass-8888")
        ok &= check("新密码登录 -> 200", r.status == 200, str(r.status))

        print()
        print("ALL AUTH TESTS PASSED" if ok else "SOME TESTS FAILED")
        return 0 if ok else 1
    finally:
        if created_server_id:
            try:
                await client.delete(f"/api/servers/{created_server_id}")
            except Exception:
                pass
        await client.close()
        shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
