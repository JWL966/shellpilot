"""应用入口：aiohttp 装配 + 访问控制 + 静态文件 + 启动。"""
import os
import sys

from aiohttp import web

# 让 server/ 目录下的模块可直接 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth as auth_gate
import config as config_store
from routes import setup_routes
from ssh_manager import ssh_manager
from llm_client import llm_client
from session import init_db

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


async def index(request: web.Request) -> web.StreamResponse:
    # 未登录 → 跳转登录页；已登录 → 主界面
    if not auth_gate.session_ok(request):
        raise web.HTTPFound("/login")
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def login_page(request: web.Request) -> web.Response:
    # 已登录再访问 /login → 直接进主界面
    if auth_gate.session_ok(request):
        raise web.HTTPFound("/")
    return web.Response(text=auth_gate.LOGIN_PAGE,
                        content_type="text/html", charset="utf-8")


async def on_cleanup(app: web.Application):
    ssh_manager.close_all()
    await llm_client.close()


def create_app() -> web.Application:
    init_db()  # 初始化 PostgreSQL 表结构
    app = web.Application(middlewares=[auth_gate.access_middleware])
    setup_routes(app)

    # 静态文件（同样受访问控制保护；登录页为内联样式，不依赖它们）
    app.router.add_static("/css/", os.path.join(WEB_DIR, "css"), show_index=False)
    app.router.add_static("/js/", os.path.join(WEB_DIR, "js"), show_index=False)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)

    app.on_cleanup.append(on_cleanup)
    return app


def _cli_reset_password() -> int:
    """--reset-password：不启动 Web 服务，重置密码并打印新凭据。"""
    password = config_store.reset_password()
    auth_gate.drop_all_sessions()  # 旧会话全部失效
    print(f"已重置登录密码（旧密码与所有登录会话已失效）：")
    print(f"  用户名: {config_store.admin_username()}")
    print(f"  新密码: {password}")
    return 0


if __name__ == "__main__":
    if "--reset-password" in sys.argv:
        sys.exit(_cli_reset_password())
    host = "127.0.0.1"
    port = 8765
    password, created = config_store.ensure_password()
    print(f"ShellPilot 启动: http://{host}:{port}", flush=True)
    if created:
        print("首次启动已生成管理员账号（密码只加密存储，明文仅此一次，请妥善保存）:", flush=True)
        print(f"  用户名: {config_store.admin_username()}", flush=True)
        print(f"  密码:   {password}", flush=True)
    elif os.environ.get("SHELLPILOT_PASSWORD", "").strip():
        print("登录密码来自环境变量 SHELLPILOT_PASSWORD", flush=True)
    print("浏览器打开上面地址登录使用。忘记密码: python server/app.py --reset-password", flush=True)
    for w in config_store.llm_warnings():
        print(f"[配置告警] {w}", flush=True)
    web.run_app(create_app(), host=host, port=port, print=None)
