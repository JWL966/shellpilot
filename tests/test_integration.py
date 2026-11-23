"""端到端集成测试：Mock LLM HTTP 服务 + 真实 aiohttp 应用 + WebSocket 客户端。

覆盖：登录认证、流式 tool_call 解析、危险命令确认（拒绝/同意）、事件序列、
SSH 失败降级、Origin 校验。
运行（先设登录密码并启动服务）：
  SHELLPILOT_PASSWORD=test1234 python server/app.py
  SHELLPILOT_PASSWORD=test1234 python tests/test_integration.py
"""
import asyncio
import json
import os
import sys

import aiohttp
from aiohttp import web

MOCK_PORT = 8899
APP = "http://127.0.0.1:8765"
ORIGIN = APP  # 同源 Origin
USERNAME = "admin"


def _load_password() -> str:
    return os.environ.get("SHELLPILOT_PASSWORD", "").strip()


PASSWORD = _load_password()


async def login(s: aiohttp.ClientSession) -> bool:
    """用环境变量里的密码登录，Cookie 存入该 session 的 jar。"""
    if not PASSWORD:
        return False
    async with s.post(f"{APP}/api/login",
                      headers={"Origin": ORIGIN},
                      json={"username": USERNAME, "password": PASSWORD}) as r:
        return r.status == 200


def new_session() -> aiohttp.ClientSession:
    """测试专用会话：目标地址是 IP，需 unsafe CookieJar 才会保存 Cookie
    （aiohttp 客户端默认拒收 IP host 的 Cookie；真实浏览器不受此影响）。"""
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f"   [{detail}]" if detail and not cond else ""))
    return cond

# Mock LLM 状态：每轮返回什么
MOCK_STATE = {"turn": 0, "scenario": "dangerous_reject"}


async def mock_chat(request: web.Request):
    body = await request.json()
    MockState.last_messages = body.get("messages", [])
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)

    tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]

    if not tool_msgs:
        # 第一轮：发出危险命令工具调用
        chunk = {
            "choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_test_1", "type": "function",
                "function": {"name": "run_command",
                             "arguments": json.dumps({"cmd": "systemctl restart nginx",
                                                      "description": "重启 nginx 服务"})},
            }]}}]
        }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
    else:
        # 第二轮：基于工具结果给结论
        text = "## 现象\n命令未被执行。\n## 原因\n用户取消或执行失败。"
        for piece in [text[:10], text[10:]]:
            chunk = {"choices": [{"delta": {"content": piece}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp


class MockState:
    last_messages = []


async def run_scenario(approve: bool, server_id: str = None):
    """连接 WebSocket，发消息，处理确认，收集事件序列。"""
    events = []
    async with new_session() as s:
        if not await login(s):
            print("登录失败：请以 SHELLPILOT_PASSWORD=<密码> 同时启动服务与本测试")
            return events
        async with s.ws_connect(f"{APP}/ws", headers={"Origin": ORIGIN}) as ws:
            await ws.send_json({"type": "user_message", "content": "重启 nginx 排查",
                                "session_id": None, "server_id": server_id})
            confirmed = False
            for _ in range(40):
                m = await asyncio.wait_for(ws.receive_json(), timeout=30)
                events.append(m)
                if m.get("type") == "confirm_required" and not confirmed:
                    confirmed = True
                    await ws.send_json({"type": "confirm", "tool_call_id": m["tool_call_id"],
                                        "approved": approve})
                if m.get("type") in ("agent_done",):
                    break
    return events


async def main():
    if not PASSWORD:
        print("请设置 SHELLPILOT_PASSWORD 环境变量（与启动服务时一致），例如：")
        print("  SHELLPILOT_PASSWORD=test1234 python server/app.py")
        print("  SHELLPILOT_PASSWORD=test1234 python tests/test_integration.py")
        return 2

    # 1. 启动 mock LLM
    app = web.Application()
    app.router.add_post("/v1/chat/completions", mock_chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", MOCK_PORT)
    await site.start()

    try:
        # 0. 鉴权检查：未登录 / 跨站请求必须被拒绝
        auth_ok = True
        async with new_session() as s:
            async with s.get(f"{APP}/api/sessions") as r:
                auth_ok &= check("鉴权: 未登录访问 API -> 401", r.status == 401, str(r.status))
            async with s.post(f"{APP}/api/servers", headers={"Origin": "http://evil.example"},
                              json={"name": "x", "host": "1.2.3.4", "username": "root"}) as r:
                auth_ok &= check("鉴权: 跨站 POST -> 403", r.status == 403, str(r.status))
            try:
                async with s.ws_connect(f"{APP}/ws",
                                        headers={"Origin": "http://evil.example"}) as ws:
                    await ws.close()
                    auth_ok &= check("鉴权: 跨站 WS -> 拒绝", False, "握手竟然成功")
            except aiohttp.WSServerHandshakeError as e:
                auth_ok &= check("鉴权: 跨站 WS -> 拒绝", e.status == 403, str(e.status))
        if not auth_ok:
            print("鉴权检查未通过，跳过后续场景")
            return 1

        # 登录一次验证凭据可用
        async with new_session() as s:
            if not await login(s):
                print("登录失败：SHELLPILOT_PASSWORD 与服务不一致？")
                return 1

        # 备份现有配置与会话，测试结束后恢复
        async with new_session() as s:
            await login(s)
            async with s.get(f"{APP}/api/config/llm") as r:
                original_llm = (await r.json())["llm"]
            async with s.get(f"{APP}/api/servers") as r:
                original_servers = (await r.json())["servers"]
            async with s.get(f"{APP}/api/sessions") as r:
                original_sessions = {x["id"] for x in (await r.json())["sessions"]}

        async with new_session() as s:
            if not await login(s):
                print("登录失败：SHELLPILOT_PASSWORD 与服务不一致？")
                return 1
            # 2. 配置 LLM 指向 mock
            # 不传 api_key：保留运行中的真实密钥，仅把 base_url 指向 mock
            async with s.put(f"{APP}/api/config/llm", json={
                "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
                "model": "mock-model", "timeout": 60,
            }) as r:
                assert r.status == 200, await r.text()

            # 3. 添加测试服务器（不可达，仅用于确认流程）
            async with s.post(f"{APP}/api/servers", json={
                "name": "集成测试机", "host": "127.0.0.1", "port": 2200,
                "username": "root", "password": "x", "auth_type": "password",
            }) as r:
                server = (await r.json())["server"]

        # ---- 场景 A：用户拒绝危险操作 ----
        ev = await run_scenario(approve=False, server_id=server['id'])
        kinds = [e["type"] for e in ev]
        ok = True
        ok &= check("A: 会话开始", "session_start" in kinds, str(kinds))
        ok &= check("A: 工具开始事件", "tool_call_start" in kinds)
        ok &= check("A: 请求确认", "confirm_required" in kinds)
        confirm_ev = next(e for e in ev if e["type"] == "confirm_required")
        ok &= check("A: 确认事件含命令", "systemctl restart nginx" in confirm_ev.get("command", ""))
        end_ev = next((e for e in ev if e["type"] == "tool_call_end"), None)
        ok &= check("A: 拒绝后工具标记失败", end_ev is not None and end_ev.get("success") is False,
                    str(end_ev))
        ok &= check("A: 有最终回答", "final_answer" in kinds)
        ok &= check("A: 有 agent_done", "agent_done" in kinds)
        ev_a = ok

        # ---- 场景 B：用户同意 -> SSH 失败应被捕获 ----
        MockState.last_messages = []
        ev2 = await run_scenario(approve=True, server_id=server['id'])
        kinds2 = [e["type"] for e in ev2]
        ok2 = True
        ok2 &= check("B: 请求确认", "confirm_required" in kinds2)
        end2 = next((e for e in ev2 if e["type"] == "tool_call_end"), None)
        ok2 &= check("B: 同意后执行（SSH 失败被捕获）", end2 is not None and
                     ("失败" in str(end2.get("result", "")) or end2.get("success") is False), str(end2))
        ok2 &= check("B: 无未捕获异常", not any("Agent 异常" in str(e.get("message", "")) for e in ev2))
        ok2 &= check("B: 有 agent_done", "agent_done" in kinds2)

        print()
        all_ok = ev_a and ok2 and auth_ok
        print("ALL INTEGRATION TESTS PASSED" if all_ok else "SOME TESTS FAILED")
        return 0 if all_ok else 1
    finally:
        # 恢复原配置：删除测试服务器、还原 LLM 设置、删除测试会话
        try:
            async with new_session() as s:
                await login(s)
                async with s.get(f"{APP}/api/servers") as r:
                    now_servers = (await r.json())["servers"]
                known = {x["id"] for x in original_servers}
                for sv in now_servers:
                    if sv["id"] not in known:
                        await s.delete(f"{APP}/api/servers/{sv['id']}")
                restore = {k: original_llm.get(k) for k in
                           ("base_url", "api_key", "model", "models", "timeout")}
                # 设置页接口把脱敏掩码当成“不修改”，这里直接省略 api_key 以保留原密钥
                restore.pop("api_key", None)
                await s.put(f"{APP}/api/config/llm", json=restore)
                async with s.get(f"{APP}/api/sessions") as r:
                    now_sessions = (await r.json())["sessions"]
                for sess in now_sessions:
                    if sess["id"] not in original_sessions:
                        await s.delete(f"{APP}/api/sessions/{sess['id']}")
        except Exception as e:
            print(f"（清理测试数据失败：{e}）")
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
