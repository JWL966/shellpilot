"""认证与安全加固测试：自起完整应用（隔离配置 + 测试数据库），验证登录认证各层。

覆盖：登录（错误凭据/正确凭据）、注册（成功自动登录/重名/弱密码/注册开关）、
多用户会话隔离、会话 Cookie 与 Bearer、退出登录、修改密码、
Origin 校验（CSWSH / 跨站简单请求 / 登录 CSRF）、Host 校验（DNS rebinding）、
base_url 入口校验。
运行：python tests/test_auth.py   （需本机 PostgreSQL 可用）
"""
import asyncio
import json
import os
import re
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


def reset_test_users():
    """测试库跨运行持久，先清空用户表保证用例确定性（admin 由 reset_password 重建）。"""
    import psycopg
    with psycopg.connect(os.environ["SHELLPILOT_PG_DSN"], autocommit=True) as conn:
        conn.execute("DELETE FROM users")


def check(name, cond, detail=""):
    cond = bool(cond)
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
    reset_test_users()
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

        # ---- 注册 ----
        r = await client.get("/register")
        body = await r.text()
        ok &= check("注册页公开可访问", r.status == 200 and "注册 ShellPilot 账号" in body, str(r.status))
        r = await client.get("/api/auth/options")
        data = await r.json()
        ok &= check("options 接口返回注册开关",
                    r.status == 200 and data.get("allow_registration") is True, str(data))
        r = await client.post("/api/register",
                              json={"username": "alice", "password": "alice-pass-123"})
        ok &= check("注册成功 -> 200 并自动登录",
                    r.status == 200 and "sp_session=" in (r.headers.get("Set-Cookie") or ""),
                    f"{r.status} {await r.text()}")
        r = await client.get("/api/servers")
        ok &= check("注册后的会话可直接访问 API -> 200", r.status == 200, str(r.status))
        r = await client.post("/api/register",
                              json={"username": "alice", "password": "another-pass-123"})
        ok &= check("重复用户名 -> 400", r.status == 400, str(r.status))
        r = await client.post("/api/register",
                              json={"username": "bob", "password": "short"})
        ok &= check("注册密码过短 -> 400", r.status == 400, str(r.status))
        r = await client.post("/api/register",
                              json={"username": "has space", "password": "valid-pass-123"})
        ok &= check("用户名含空格 -> 400", r.status == 400, str(r.status))
        r = await client.post("/api/register",
                              json={"username": "", "password": "valid-pass-123"})
        ok &= check("空用户名 -> 400", r.status == 400, str(r.status))
        # 新用户退出后用密码登录
        r = await client.post("/api/logout")
        r = await login(client, "alice", "alice-pass-123")
        ok &= check("新注册用户可登录 -> 200", r.status == 200, str(r.status))
        r = await login(client, "alice", "wrong-pass-999")
        ok &= check("新用户错误密码 -> 401", r.status == 401, str(r.status))

        # 多用户会话隔离：admin 在独立会话里改密码，不影响 alice 的会话
        cookies = client.session.cookie_jar.filter_cookies(client.make_url("/"))
        alice_bearer = cookies["sp_session"].value if "sp_session" in cookies else None
        r = await client.get("/api/servers")
        ok &= check("alice 登录后 API -> 200", r.status == 200, str(r.status))
        async with aiohttp.ClientSession() as raw:
            r = await raw.post(f"{client.make_url('/api/login')}",
                               json={"username": "admin", "password": PW})
            m = re.search(r"sp_session=([^;]+)", r.headers.get("Set-Cookie", ""))
            admin_token = m.group(1) if m else ""
            ok &= check("独立会话 admin 登录 -> 200", r.status == 200 and admin_token,
                        f"{r.status} {r.headers.get('Set-Cookie', '')}")
            r = await raw.post(f"{client.make_url('/api/auth/password')}",
                               headers={"Authorization": f"Bearer {admin_token}"},
                               json={"old_password": PW, "new_password": "admin-new-pw-8888"})
            ok &= check("admin 改密码 -> 200", r.status == 200, f"{r.status} {await r.text()}")
        r = await client.get("/api/servers")
        ok &= check("alice 的会话不受 admin 改密码影响 -> 200", r.status == 200, str(r.status))
        ok &= check("取得了 alice 的会话令牌", bool(alice_bearer), repr(alice_bearer))

        # ---- 注册开关 ----
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg_data = json.load(f)
        cfg_data["auth"]["allow_registration"] = False
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, ensure_ascii=False)
        r = await client.get("/api/auth/options")
        data = await r.json()
        ok &= check("关闭注册后 options -> false",
                    r.status == 200 and data.get("allow_registration") is False, str(data))
        r = await client.post("/api/register",
                              json={"username": "carol", "password": "carol-pass-123"})
        ok &= check("关闭注册后注册 -> 403", r.status == 403, str(r.status))
        r = await client.get("/register")
        body = await r.text()
        ok &= check("注册页仍可访问（提交时提示关闭）", r.status == 200, str(r.status))
        cfg_data["auth"]["allow_registration"] = True
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, ensure_ascii=False)

        # ---- 管理员账号保障（ensure_admin_user 幂等）----
        pw2, status2 = config_store.ensure_admin_user()
        ok &= check("ensure_admin_user 表非空时 -> exists", status2 == "exists" and pw2 == "",
                    f"{status2} {pw2}")

        # ---- 修改密码（admin，alice 会话已验证隔离）----
        r = await login(client, "admin", "admin-new-pw-8888")
        ok &= check("admin 新密码登录 -> 200", r.status == 200, str(r.status))
        r = await client.post("/api/auth/password",
                              json={"old_password": "admin-new-pw-8888", "new_password": "admin-final-9999"})
        ok &= check("admin 再次修改密码 -> 200", r.status == 200, str(r.status))
        r = await login(client, "admin", "admin-final-9999")
        ok &= check("admin 最终密码登录 -> 200", r.status == 200, str(r.status))

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
