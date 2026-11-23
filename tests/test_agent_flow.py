"""端到端测试：用 Mock 的 LLM + Mock SSH 验证 Agent 循环、安全分级与事件流。

运行：python tests/test_agent_flow.py
不依赖真实 LLM / 真实服务器。
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import agent as agent_mod
import config as config_store
import session as session_store

EVENTS: list[dict] = []


async def emit(ev):
    EVENTS.append(ev)


# ---------- Mock LLM：按脚本返回 ----------
class MockLLM:
    def __init__(self, script):
        self.script = script  # list of turns
        self.i = 0

    async def chat_stream(self, user_id, messages, tools=None, model=None):
        self.last_model = model
        if self.i >= len(self.script):
            yield {"type": "content", "delta": "（脚本结束）"}
            return
        turn = self.script[self.i]
        self.i += 1
        for piece in turn.get("content", []):
            yield {"type": "content", "delta": piece}
        if turn.get("tool_calls"):
            yield {"type": "tool_call", "tool_calls": turn["tool_calls"]}


# ---------- Mock SSH ----------
class MockSSH:
    calls: list[tuple] = []

    def exec_command(self, server_id, cmd, timeout=30):
        MockSSH.calls.append((server_id, cmd))
        return {"exit_code": 0, "stdout": f"[mock output for: {cmd}]", "stderr": "", "duration_ms": 5}


TEST_USER_ID: int | None = None


def setup_env():
    global TEST_USER_ID
    # 用临时 config（隔离真实配置；llm/servers 已迁入数据库，config 只剩 auth/ssh 选项）
    os.environ["SHELLPILOT_CONFIG"] = os.path.join(HERE, "test_config.json")
    config_store.CONFIG_PATH = os.environ["SHELLPILOT_CONFIG"]
    if os.path.exists(config_store.CONFIG_PATH):
        os.remove(config_store.CONFIG_PATH)
    cfg = json.loads(json.dumps(config_store.DEFAULT_CONFIG))
    with open(config_store.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    # 会话存储使用独立测试数据库，避免污染真实数据
    os.environ["SHELLPILOT_PG_DSN"] = "host=127.0.0.1 port=5432 dbname=shellpilot_test user=postgres password=123456"
    session_store.DSN = os.environ["SHELLPILOT_PG_DSN"]
    session_store._initialized = False

    # 测试用户 + 其名下的测试服务器（服务器已按用户隔离，Agent 只能操作本人服务器）
    user = session_store.get_user_by_name("agent-test")
    if not user:
        user = session_store.create_user("agent-test", "not-a-real-password")
    TEST_USER_ID = int(user["id"])
    session_store.delete_server("srv1", TEST_USER_ID)
    session_store.create_server(TEST_USER_ID, {
        "id": "srv1", "name": "测试服务器", "host": "10.0.0.1", "port": 22,
        "username": "root", "password": "x", "auth_type": "password",
    })

    agent_mod.ssh_manager = MockSSH()


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f"  {detail}" if detail and not cond else ""))
    return cond


async def test_safe_command_flow():
    """只读命令 → 自动执行，无需确认，最终给出结论。"""
    EVENTS.clear()
    MockSSH.calls.clear()
    mock = MockLLM([
        {"content": ["我来检查 nginx 状态。\n"],
         "tool_calls": [{"id": "c1", "name": "run_command",
                         "arguments": json.dumps({"cmd": "systemctl status nginx", "description": "查看 nginx 状态"})}]},
        {"content": ["## 现象\nnginx 正常运行。"]},
    ])
    agent_mod.llm_client = mock
    sess = session_store.create_session(TEST_USER_ID, title="t1", server_id="srv1")
    ag = agent_mod.AgentSession(sess["id"], emit, "srv1", model="test-model", user_id=TEST_USER_ID)
    ag.load_history()
    await ag.run("检查 nginx", "srv1")

    types = [e["type"] for e in EVENTS]
    ok = True
    ok &= check("model: 模型选择透传到 LLM", getattr(mock, "last_model", None) == "test-model", str(getattr(mock, "last_model", None)))
    ok &= check("safe: 发出 tool_call_start", "tool_call_start" in types)
    ok &= check("safe: 无确认请求", "confirm_required" not in types)
    ok &= check("safe: 命令被执行", len(MockSSH.calls) == 1, str(MockSSH.calls))
    ok &= check("safe: 输出 tool_call_end", "tool_call_end" in types)
    ok &= check("safe: 有 final_answer", "final_answer" in types)
    ok &= check("safe: 有 agent_done", "agent_done" in types)
    start = next(e for e in EVENTS if e["type"] == "tool_call_start")
    ok &= check("safe: 风险等级=safe", start["risk"] == "safe", start["risk"])
    return ok


async def test_dangerous_requires_confirm():
    """危险命令 → 发确认请求，用户拒绝则不执行。"""
    EVENTS.clear()
    MockSSH.calls.clear()
    mock = MockLLM([
        {"content": [],
         "tool_calls": [{"id": "c2", "name": "run_command",
                         "arguments": json.dumps({"cmd": "systemctl restart nginx", "description": "重启 nginx"})}]},
        {"content": ["好的，未重启。"]},
    ])
    agent_mod.llm_client = mock
    sess = session_store.create_session(TEST_USER_ID, title="t2", server_id="srv1")
    ag = agent_mod.AgentSession(sess["id"], emit, "srv1", user_id=TEST_USER_ID)
    ag.load_history()

    async def approve_later():
        # 等确认请求出现，然后拒绝
        for _ in range(200):
            if any(e["type"] == "confirm_required" for e in EVENTS):
                ag.handle_confirm("c2", False)
                return
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ag.run("重启 nginx", "srv1"))
    await approve_later()
    await task

    types = [e["type"] for e in EVENTS]
    ok = True
    ok &= check("dangerous: 发出 confirm_required", "confirm_required" in types)
    ok &= check("dangerous: 拒绝后未执行", len(MockSSH.calls) == 0, str(MockSSH.calls))
    end = next((e for e in EVENTS if e["type"] == "tool_call_end"), None)
    ok &= check("dangerous: tool_call_end 标记失败", end is not None and end.get("success") is False)
    return ok


async def test_forbidden_blocked():
    """绝对禁止命令 → 直接拒绝，不发确认。"""
    EVENTS.clear()
    MockSSH.calls.clear()
    mock = MockLLM([
        {"content": [],
         "tool_calls": [{"id": "c3", "name": "run_command",
                         "arguments": json.dumps({"cmd": "rm -rf /"})}]},
        {"content": ["无法执行该命令。"]},
    ])
    agent_mod.llm_client = mock
    sess = session_store.create_session(TEST_USER_ID, title="t3", server_id="srv1")
    ag = agent_mod.AgentSession(sess["id"], emit, "srv1", user_id=TEST_USER_ID)
    ag.load_history()
    await ag.run("删库", "srv1")

    types = [e["type"] for e in EVENTS]
    ok = True
    ok &= check("forbidden: 无确认请求", "confirm_required" not in types)
    ok &= check("forbidden: 未执行", len(MockSSH.calls) == 0, str(MockSSH.calls))
    end = next((e for e in EVENTS if e["type"] == "tool_call_end"), None)
    ok &= check("forbidden: 返回拒绝信息", end is not None and "拒绝" in str(end.get("result", "")))
    return ok


async def test_max_iterations():
    """无限工具调用 → 达到上限后强制收敛。"""
    EVENTS.clear()
    MockSSH.calls.clear()
    turns = []
    for i in range(30):
        turns.append({"content": [],
                      "tool_calls": [{"id": f"x{i}", "name": "run_command",
                                      "arguments": json.dumps({"cmd": "df -h"})}]})
    agent_mod.llm_client = MockLLM(turns)
    sess = session_store.create_session(TEST_USER_ID, title="t4", server_id="srv1")
    ag = agent_mod.AgentSession(sess["id"], emit, "srv1", user_id=TEST_USER_ID)
    ag.load_history()
    await ag.run("一直跑", "srv1")

    ok = check("max_iter: 执行轮次被限制", len(MockSSH.calls) <= 15, str(len(MockSSH.calls)))
    ok &= check("max_iter: 有报错提示收敛", any(e["type"] == "error" for e in EVENTS))
    ok &= check("max_iter: 有 agent_done", any(e["type"] == "agent_done" for e in EVENTS))
    return ok


async def test_high_level_tools():
    """高层工具封装生成正确命令。"""
    EVENTS.clear()
    MockSSH.calls.clear()
    mock = MockLLM([
        {"content": [],
         "tool_calls": [
             {"id": "t1", "name": "check_port", "arguments": json.dumps({"port": 8080})},
             {"id": "t2", "name": "get_service_status", "arguments": json.dumps({"service": "nginx"})},
             {"id": "t3", "name": "check_resources", "arguments": "{}"},
         ]},
        {"content": ["完成"]},
    ])
    agent_mod.llm_client = mock
    sess = session_store.create_session(TEST_USER_ID, title="t5", server_id="srv1")
    ag = agent_mod.AgentSession(sess["id"], emit, "srv1", user_id=TEST_USER_ID)
    ag.load_history()
    await ag.run("检查", "srv1")

    cmds = [c[1] for c in MockSSH.calls]
    ok = True
    ok &= check("tools: check_port 生成 ss 命令", any("ss -tlnp" in c and "8080" in c for c in cmds), str(cmds))
    ok &= check("tools: get_service_status 生成 systemctl", any("systemctl status nginx" in c for c in cmds))
    ok &= check("tools: check_resources 生成 df/free", any("df -h" in c and "free -h" in c for c in cmds))
    return ok


async def main():
    setup_env()
    results = []
    results.append(await test_safe_command_flow())
    results.append(await test_dangerous_requires_confirm())
    results.append(await test_forbidden_blocked())
    results.append(await test_max_iterations())
    results.append(await test_high_level_tools())

    print()
    if all(results):
        print(f"ALL {len(results)} TESTS PASSED")
        sys.exit(0)
    else:
        print(f"{results.count(False)} TEST(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # 清理测试产物
        import psycopg as _pg
        with _pg.connect(os.environ["SHELLPILOT_PG_DSN"]) as _c:
            with _c.cursor() as _cur:
                _cur.execute("TRUNCATE TABLE sessions, messages, servers RESTART IDENTITY CASCADE")
        cfg_path = os.path.join(HERE, "test_config.json")
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
