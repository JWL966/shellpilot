"""应用入口：aiohttp 装配 + 静态文件 + 启动。"""
import os
import sys

from aiohttp import web

# 让 server/ 目录下的模块可直接 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routes import setup_routes
from ssh_manager import ssh_manager
from llm_client import llm_client
from session import init_db

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def on_cleanup(app: web.Application):
    ssh_manager.close_all()
    await llm_client.close()


def create_app() -> web.Application:
    init_db()  # 初始化 PostgreSQL 表结构
    app = web.Application()
    setup_routes(app)

    # 静态文件
    app.router.add_static("/css/", os.path.join(WEB_DIR, "css"), show_index=False)
    app.router.add_static("/js/", os.path.join(WEB_DIR, "js"), show_index=False)
    app.router.add_get("/", index)

    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 8765
    print(f"ShellPilot 启动: http://{host}:{port}")
    web.run_app(create_app(), host=host, port=port, print=None)
