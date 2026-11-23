"""HTTP API 与 WebSocket 路由。"""
import asyncio
import json
from typing import Any

from aiohttp import web, WSMsgType

import config as config_store
import session as session_store
from agent import AgentSession
from auth import (
    clear_session_cookie,
    create_session as create_auth_session,
    drop_other_sessions,
    drop_session as drop_auth_session,
    session_token_from,
    set_session_cookie,
)
from llm_client import LLMError, llm_client
from ssh_manager import ssh_manager


# ---------- REST ----------

async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ---------- 认证 ----------

async def login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    if not config_store.verify_credentials(username, password):
        await asyncio.sleep(1.0)  # 拖慢暴力破解（异步休眠，不阻塞事件循环）
        return web.json_response({"error": "用户名或密码不正确"}, status=401)
    token = create_auth_session()
    resp = web.json_response({"ok": True, "username": config_store.admin_username()})
    set_session_cookie(resp, token)
    return resp


async def logout(request: web.Request) -> web.Response:
    drop_auth_session(session_token_from(request))
    resp = web.json_response({"ok": True})
    clear_session_cookie(resp)
    return resp


async def change_password(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    try:
        config_store.change_password(
            str(body.get("old_password") or ""), str(body.get("new_password") or ""))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    # 密码已变：注销其他会话，保留当前会话避免自己被登出
    drop_other_sessions(session_token_from(request))
    return web.json_response({"ok": True})


# 服务器管理
async def list_servers(request: web.Request) -> web.Response:
    return web.json_response({"servers": config_store.list_servers()})


async def add_server(request: web.Request) -> web.Response:
    body = await request.json()
    required = ("name", "host", "username")
    if not all(body.get(k) for k in required):
        return web.json_response({"error": "缺少必填字段: name/host/username"}, status=400)
    server = config_store.add_server(body)
    return web.json_response({"server": server})


async def update_server(request: web.Request) -> web.Response:
    server_id = request.match_info["id"]
    body = await request.json()
    updated = config_store.update_server(server_id, body)
    if not updated:
        return web.json_response({"error": "服务器不存在"}, status=404)
    return web.json_response({"server": updated})


async def delete_server(request: web.Request) -> web.Response:
    server_id = request.match_info["id"]
    ssh_manager.close(server_id)
    ok = config_store.delete_server(server_id)
    return web.json_response({"success": ok})


async def test_server(request: web.Request) -> web.Response:
    server_id = request.match_info["id"]
    if not config_store.get_server_raw(server_id):
        return web.json_response({"error": "服务器不存在"}, status=404)
    result = await asyncio.to_thread(ssh_manager.test_connection, server_id)
    return web.json_response(result)


async def reset_host_key(request: web.Request) -> web.Response:
    """删除某服务器已记录的 SSH 主机指纹（对方重装系统后重置用）。"""
    server_id = request.match_info["id"]
    srv = config_store.get_server_raw(server_id)
    if not srv:
        return web.json_response({"error": "服务器不存在"}, status=404)
    ssh_manager.close(server_id)
    removed = await asyncio.to_thread(
        ssh_manager.forget_host_key, srv["host"], int(srv.get("port", 22))
    )
    return web.json_response({
        "success": removed,
        "message": "已删除指纹，下次连接将重新记录" if removed else "该主机暂无已记录的指纹",
    })


# LLM 配置
async def get_llm(request: web.Request) -> web.Response:
    return web.json_response({
        "llm": config_store.get_llm_config(),
        "warnings": config_store.llm_warnings(),
    })


async def update_llm(request: web.Request) -> web.Response:
    body = await request.json()
    errs = config_store.validate_llm_updates(body)
    if errs:
        return web.json_response({"error": "；".join(errs)}, status=400)
    result = config_store.update_llm_config(body)
    return web.json_response({"llm": result, "warnings": config_store.llm_warnings()})


async def test_llm(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    result = await llm_client.test_connection(model=body.get("model"))
    return web.json_response(result)


# 会话
async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response({"sessions": session_store.list_sessions()})


async def get_session(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    session = session_store.get_session(sid)
    if not session:
        return web.json_response({"error": "会话不存在"}, status=404)
    return web.json_response({"session": session})


async def delete_session(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    ok = session_store.delete_session(sid)
    return web.json_response({"success": ok})


async def create_session(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    session = session_store.create_session(
        title=body.get("title", "新会话"),
        server_id=body.get("server_id"),
    )
    return web.json_response({"session": session})


# ---------- WebSocket ----------

class WSManager:
    """管理单个 WebSocket 连接上的 Agent 会话。"""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.agent: AgentSession | None = None
        self.task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def emit(self, event: dict[str, Any]):
        try:
            await self.ws.send_json(event)
        except ConnectionError:
            pass


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    manager = WSManager(ws)

    async def emit(event: dict):
        await manager.emit(event)

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    break
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await emit({"type": "error", "message": "无效的 JSON"})
                continue

            msg_type = data.get("type")
            if msg_type == "user_message":
                session_id = data.get("session_id")
                content = data.get("content", "").strip()
                server_id = data.get("server_id")
                model = data.get("model")
                if not content:
                    await emit({"type": "error", "message": "消息内容为空"})
                    continue
                if manager.busy:
                    await emit({"type": "error", "message": "上一个任务仍在执行，请等待完成或先停止"})
                    continue

                if session_id and not session_store.get_session(session_id):
                    await emit({"type": "error", "message": "会话不存在，已新建会话"})
                    session_id = None
                if not session_id:
                    session = session_store.create_session(server_id=server_id)
                    session_id = session["id"]
                await emit({"type": "session_start", "session_id": session_id})

                agent = AgentSession(session_id, emit, server_id, model=model)
                agent.load_history()
                manager.agent = agent
                # 异步执行 Agent，WebSocket 继续接收确认/取消消息
                manager.task = asyncio.create_task(_run_agent(manager, agent, content, server_id))

            elif msg_type == "confirm":
                if manager.agent and manager.busy:
                    manager.agent.handle_confirm(data.get("tool_call_id", ""), bool(data.get("approved")))
                else:
                    await emit({"type": "error", "message": "没有正在进行的 Agent 会话"})

            elif msg_type == "cancel":
                if manager.agent and manager.busy:
                    manager.agent.cancel()
                    await emit({"type": "cancelled", "message": "已请求停止"})
                else:
                    await emit({"type": "cancelled", "message": "当前没有正在执行的任务"})

            else:
                await emit({"type": "error", "message": f"未知消息类型: {msg_type}"})
    finally:
        if manager.agent:
            manager.agent.cancel()
        if manager.task and not manager.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(manager.task), timeout=5)
            except Exception:
                pass
        ssh_manager.close_all()
    return ws


async def _run_agent(manager: "WSManager", agent: AgentSession, content: str, server_id: str | None):
    try:
        await agent.run(content, server_id)
    except Exception as e:
        try:
            await manager.emit({"type": "error", "message": f"Agent 运行异常: {e}"})
            await manager.emit({"type": "agent_done"})
        except Exception:
            pass


# ---------- 路由注册 ----------

def setup_routes(app: web.Application):
    app.router.add_get("/api/health", health)

    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/auth/password", change_password)

    app.router.add_get("/api/servers", list_servers)
    app.router.add_post("/api/servers", add_server)
    app.router.add_put("/api/servers/{id}", update_server)
    app.router.add_delete("/api/servers/{id}", delete_server)
    app.router.add_post("/api/servers/{id}/test", test_server)
    app.router.add_delete("/api/servers/{id}/hostkey", reset_host_key)

    app.router.add_get("/api/config/llm", get_llm)
    app.router.add_put("/api/config/llm", update_llm)
    app.router.add_post("/api/config/llm/test", test_llm)

    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{id}", get_session)
    app.router.add_delete("/api/sessions/{id}", delete_session)

    app.router.add_get("/ws", websocket_handler)
