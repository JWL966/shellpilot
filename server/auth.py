"""访问控制：登录认证 + 多用户注册 + 会话管理 + Origin/Host 校验。

威胁模型（服务只监听 127.0.0.1，但浏览器就是"外网"入口）：
1. 跨站 WebSocket 劫持（CSWSH）：用户浏览任意网页时，该网页可以连
   ws://127.0.0.1:8765/ws —— WS 握手不受同源策略约束，且 aiohttp 默认
   不校验 Origin。攻击者借 AI 在你的服务器上执行命令、读密钥文件。
2. CSRF：跨站"简单请求"（Content-Type: text/plain）无需 CORS 预检即可
   打到本机 API。有了会话 Cookie 后这类攻击更经典，必须拦截。
3. DNS rebinding：攻击者域名解析到 127.0.0.1，浏览器视角构成同源。

认证流程（本模块）：
- 用户存于 PostgreSQL 的 users 表（server/session.py）；登录页 /login、
  注册页 /register，登录/注册成功后服务端建立会话并下发 HttpOnly Cookie。
- 首次启动 users 表为空时自动创建管理员账号（见 config.ensure_admin_user）。
- 会话存于内存（重启即失效，重新登录即可），30 天滑动过期，绑定用户 id。
- 退出登录 / 修改密码会注销会话；Origin/Host 校验独立于认证，
  对登录/注册接口同样生效（登录 CSRF 也是 CSRF）。
"""
import ipaddress
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlsplit

from aiohttp import web

import config as config_store

COOKIE_NAME = "sp_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
SESSION_TTL = 30 * 24 * 3600

# 内存会话表：token -> (user_id, 过期时间戳)（单进程事件循环内访问，无并发问题）
_sessions: dict[str, tuple[int, float]] = {}


# ---------- 会话管理 ----------

def create_session(user_id: int) -> str:
    now = time.time()
    for tok in [t for t, (_, exp) in _sessions.items() if exp < now]:
        _sessions.pop(tok, None)  # 顺手清理过期会话
    token = secrets.token_urlsafe(32)
    _sessions[token] = (int(user_id), now + SESSION_TTL)
    return token


def session_ok(request) -> bool:
    token = session_token_from(request)
    if not token:
        return False
    entry = _sessions.get(token)
    if entry is None or entry[1] < time.time():
        _sessions.pop(token, None)
        return False
    user_id, _ = entry
    _sessions[token] = (user_id, time.time() + SESSION_TTL)  # 滑动过期
    return True


def current_user_id(request) -> Optional[int]:
    """当前会话绑定的用户 id；未登录返回 None（中间件已保证登录态，防御用）。"""
    entry = _sessions.get(session_token_from(request))
    return entry[0] if entry else None


def get_user(user_id: int) -> Optional[dict]:
    """取某用户的会话信息（user_id、过期时间）。"""
    for tok, (uid, exp) in _sessions.items():
        if uid == int(user_id):
            return {"token": tok, "user_id": uid, "expiry": exp}
    return None


def session_count_for(user_id: int) -> int:
    """某用户当前的活跃会话数（账号页展示用）。"""
    now = time.time()
    return sum(1 for _, (uid, exp) in _sessions.items() if uid == int(user_id) and exp >= now)


def sessions_from_token(token: str) -> Optional[tuple[int, float]]:
    """按令牌取会话条目 (user_id, expiry)。"""
    return _sessions.get(token)


def session_token_from(request) -> str:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):  # 供脚本/测试用登录后的会话令牌
        return auth[7:].strip()
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def drop_session(token: str) -> None:
    if token:
        _sessions.pop(token, None)


def drop_other_user_sessions(user_id: int, keep_token: str) -> None:
    """注销指定用户的其他会话（修改密码后调用；当前会话保留避免自己被登出）。"""
    for tok, (uid, _) in list(_sessions.items()):
        if uid == int(user_id) and tok != keep_token:
            _sessions.pop(tok, None)


def drop_all_sessions() -> None:
    _sessions.clear()


def set_session_cookie(response: web.Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Strict",
        path="/",
    )


def clear_session_cookie(response: web.Response) -> None:
    response.del_cookie(COOKIE_NAME, path="/")


# ---------- Origin / Host 校验 ----------

def _allowlisted(env_name: str) -> set:
    raw = os.environ.get(env_name, "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _split_hostname(host_header: str) -> str:
    if not host_header:
        return ""
    try:
        return (urlsplit(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return ""


def host_allowed(request) -> bool:
    """Host 必须是回环 / localhost / IP 字面量 / 显式白名单域名（防 DNS rebinding）。"""
    hostname = _split_hostname(request.headers.get("Host", ""))
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    if hostname in _allowlisted("SHELLPILOT_ALLOWED_HOSTS"):
        return True
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return True
    except ValueError:
        return False


def origin_allowed(request) -> bool:
    """带 Origin 的请求必须同源；无 Origin（curl/测试脚本）放行，认证仍必查。"""
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return True
    if origin.lower() == "null":  # file:// 或沙箱页面
        return False
    allow = {a.lower().rstrip("/") for a in _allowlisted("SHELLPILOT_ALLOWED_ORIGINS")}
    if origin.lower().rstrip("/") in allow:
        return True
    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.netloc:
        return False
    return u.netloc.lower() == (request.host or "").lower()


# ---------- 登录页（内联样式，不依赖受保护的静态资源） ----------

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · ShellPilot</title>
<style>
  body{background:#0d1117;color:#e6edf3;font-family:system-ui,-apple-system,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:36px;
        width:360px;box-sizing:border-box}
  .logo{font-size:26px;color:#58a6ff;text-align:center}
  h1{font-size:17px;font-weight:600;text-align:center;margin:6px 0 26px;color:#e6edf3}
  label{display:block;font-size:12.5px;color:#8b949e;margin:14px 0 6px}
  input{width:100%;box-sizing:border-box;background:#0d1117;color:#e6edf3;
        border:1px solid #30363d;border-radius:7px;padding:9px 12px;font-size:13.5px;
        outline:none;transition:border-color .15s}
  input:focus{border-color:#58a6ff}
  button{width:100%;margin-top:22px;background:#238636;color:#fff;border:none;
         border-radius:7px;padding:10px;cursor:pointer;font-size:14px}
  button:hover{background:#2ea043}
  button:disabled{opacity:.6;cursor:default}
  .err{color:#f85149;font-size:12.5px;margin-top:14px;min-height:17px;text-align:center}
  .hint{color:#8b949e;font-size:12px;margin-top:18px;line-height:1.7;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#9672;</div>
  <h1>ShellPilot 登录</h1>
  <form id="f">
    <label for="u">用户名</label>
    <input id="u" type="text" autocomplete="username" autofocus>
    <label for="p">密码</label>
    <input id="p" type="password" autocomplete="current-password">
    <button type="submit" id="b">登 录</button>
    <div class="err" id="e"></div>
  </form>
  <div style="text-align:center;margin-top:6px;font-size:12.5px">
    <a id="reg-link" href="/register" style="color:#58a6ff">注册新账号</a>
  </div>
  <div class="hint">首次启动的账号密码在服务启动日志里<br>忘记密码可在服务端执行 python server/app.py --reset-password</div>
</div>
<script>
  // 注册开关关闭时隐藏注册入口
  fetch('/api/auth/options').then(r => r.json()).then(o => {
    if (o && o.allow_registration === false) {
      const link = document.getElementById('reg-link');
      if (link) link.style.display = 'none';
    }
  }).catch(() => {});
  document.getElementById('f').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = document.getElementById('b');
    const err = document.getElementById('e');
    err.textContent = '';
    btn.disabled = true;
    try {
      const resp = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('u').value.trim(),
          password: document.getElementById('p').value,
        }),
      });
      if (resp.ok) { location.href = '/'; return; }
      err.textContent = (await resp.json().catch(() => ({}))).error || '登录失败';
    } catch (e) {
      err.textContent = '网络错误: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  });
</script>
</body>
</html>"""


REGISTER_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>注册 · ShellPilot</title>
<style>
  body{background:#0d1117;color:#e6edf3;font-family:system-ui,-apple-system,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:36px;
        width:360px;box-sizing:border-box}
  .logo{font-size:26px;color:#58a6ff;text-align:center}
  h1{font-size:17px;font-weight:600;text-align:center;margin:6px 0 26px;color:#e6edf3}
  label{display:block;font-size:12.5px;color:#8b949e;margin:14px 0 6px}
  input{width:100%;box-sizing:border-box;background:#0d1117;color:#e6edf3;
        border:1px solid #30363d;border-radius:7px;padding:9px 12px;font-size:13.5px;
        outline:none;transition:border-color .15s}
  input:focus{border-color:#58a6ff}
  button{width:100%;margin-top:22px;background:#238636;color:#fff;border:none;
         border-radius:7px;padding:10px;cursor:pointer;font-size:14px}
  button:hover{background:#2ea043}
  button:disabled{opacity:.6;cursor:default}
  .err{color:#f85149;font-size:12.5px;margin-top:14px;min-height:17px;text-align:center}
  .hint{color:#8b949e;font-size:12px;margin-top:18px;line-height:1.7;text-align:center}
  .links{text-align:center;margin-top:8px;font-size:12.5px}
  .links a{color:#58a6ff}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#9672;</div>
  <h1>注册 ShellPilot 账号</h1>
  <form id="f">
    <label for="u">用户名（1~32 字符，不含空格）</label>
    <input id="u" type="text" autocomplete="username" autofocus>
    <label for="p">密码（至少 8 位）</label>
    <input id="p" type="password" autocomplete="new-password">
    <label for="p2">确认密码</label>
    <input id="p2" type="password" autocomplete="new-password">
    <button type="submit" id="b">注 册</button>
    <div class="err" id="e"></div>
  </form>
  <div class="links">已有账号？<a href="/login">去登录</a></div>
  <div class="hint">注册后即可登录使用；密码经 PBKDF2 加密存储</div>
</div>
<script>
  fetch('/api/auth/options').then(r => r.json()).then(o => {
    if (o && o.allow_registration === false) {
      document.getElementById('e').textContent = '管理员已关闭注册，请联系管理员开通账号';
      document.getElementById('b').disabled = true;
      document.getElementById('u').disabled = true;
      document.getElementById('p').disabled = true;
      document.getElementById('p2').disabled = true;
    }
  }).catch(() => {});
  document.getElementById('f').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = document.getElementById('b');
    const err = document.getElementById('e');
    const pw = document.getElementById('p').value;
    err.textContent = '';
    if (pw !== document.getElementById('p2').value) {
      err.textContent = '两次输入的密码不一致';
      return;
    }
    btn.disabled = true;
    try {
      const resp = await fetch('/api/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('u').value.trim(),
          password: pw,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) { location.href = '/'; return; }
      err.textContent = data.error || '注册失败';
    } catch (e) {
      err.textContent = '网络错误: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  });
</script>
</body>
</html>"""


# ---------- 中间件 ----------

def _wants_json(request) -> bool:
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _deny(request, msg: str, status: int) -> web.Response:
    # API / WS 握手 / JSON 期望：返回状态码，绝不重定向
    #（WS 不支持重定向，客户端会带着 Upgrade 头循环跟随）
    if request.path.startswith("/api/") or _wants_json(request) or _is_ws(request):
        return web.json_response({"error": msg}, status=status)
    if request.method in ("GET", "HEAD"):
        raise web.HTTPFound("/login")
    return web.Response(text=msg, status=status,
                        content_type="text/plain", charset="utf-8")


def _is_ws(request) -> bool:
    return request.headers.get("Upgrade", "").lower() == "websocket"


@web.middleware
async def access_middleware(request, handler):
    # 1) Host 校验：防 DNS rebinding（对登录页/健康检查同样生效）
    if not host_allowed(request):
        return _deny(request, "Host 不被允许（防 DNS rebinding）", 403)

    # 2) Origin 校验：拦截 CSWSH 与跨站请求（含登录接口的登录 CSRF）。
    #    浏览器一定带 Origin；curl/测试脚本不带则放行，但认证仍必查。
    if _is_ws(request) and not origin_allowed(request):
        return _deny(request, "WebSocket 来源不被允许", 403)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_allowed(request):
        return _deny(request, "跨站请求被拒绝（Origin 不匹配）", 403)

    # 3) 公开端点：健康检查、登录页/注册页、登录/注册接口、页面选项
    if request.path == "/api/health":
        return await handler(request)
    if request.method == "GET" and request.path in ("/login", "/register", "/"):
        return await handler(request)  # /login、/register 渲染页面；/ 由 index 决定跳转
    if request.path == "/api/login" and request.method == "POST":
        return await handler(request)
    if request.path == "/api/register" and request.method == "POST":
        return await handler(request)
    if request.path == "/api/auth/options" and request.method == "GET":
        return await handler(request)

    # 4) 其余一律要求已登录会话（含静态资源与 /ws）
    if not session_ok(request):
        return _deny(request, "未登录或会话已过期", 401)
    return await handler(request)
