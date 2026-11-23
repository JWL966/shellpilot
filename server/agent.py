"""Agent 引擎：ReAct 循环 + 工具调度 + 安全分级 + 事件流。"""
import asyncio
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from config import get_llm_config_raw, list_servers
from llm_client import LLMError, llm_client
from safety import RiskLevel, classify_command
from session import append_message, get_server, get_session
from ssh_manager import SSHError, ssh_manager

# 事件回调：async def emit(event: dict) -> None
EmitFn = Callable[[dict], Awaitable[None]]

SYSTEM_PROMPT = """你是一位资深 Linux SRE 运维专家，负责通过 SSH 远程排查服务器上的服务故障。

# 你的能力
你可以调用以下工具在目标服务器上执行命令并获取信息：
- run_command(server_id, cmd, description): 在服务器上执行 shell 命令
- read_file(server_id, path, tail_lines): 读取文件末尾 N 行
- get_service_status(service, server_id): 查看服务状态与最近日志
- check_port(port, server_id): 检查端口占用
- check_resources(server_id): 查看 CPU/内存/磁盘
- list_services(server_id): 列出服务单元

# 行为规范
1. 先观察再动手：优先只读排查（看状态、看日志、看进程、看端口、看资源），不要上来就改配置或重启服务。
2. 逐步推理：每执行一条命令，基于输出判断下一步；不要一次抛出大量命令。
3. 命令要精确：grep 时用 `| head -50` 限定行数；tail 用 `-n 200` 限定；find 限定目录深度。
4. 给出结论时必须附证据（引用命令输出的关键行），按"现象 → 原因 → 证据 → 修复建议 → 预防建议"格式输出。
5. 如果不确定，明确说明"不确定"并给出进一步排查建议，不要编造。
6. 危险操作（重启服务、杀进程、改配置、安装/卸载包等）会触发用户确认，请在调用前用 description 说明意图；用户拒绝后不要重复尝试同一操作。
7. 绝对禁止执行破坏性命令（rm -rf /、mkfs、dd 写盘、fork bomb 等）。
8. 排查完成后给出最终诊断结论，不要无休止地执行命令；已有足够证据时就收敛。

# 输出格式（最终结论用 Markdown）
## 现象
## 原因
## 证据
## 修复建议
## 预防建议
"""

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在指定 Linux 服务器上执行 shell 命令，返回退出码、stdout、stderr。",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "要执行的 shell 命令"},
                    "server_id": {"type": "string", "description": "服务器 ID；省略则使用当前会话默认服务器"},
                    "description": {"type": "string", "description": "本次命令的意图说明（用于审计与危险确认展示）"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取服务器上指定文件的末尾 N 行（等价 tail -n）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "server_id": {"type": "string", "description": "服务器 ID"},
                    "tail_lines": {"type": "integer", "description": "读取末尾行数，默认 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "description": "查看 systemd 服务状态与最近日志（systemctl status + journalctl）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "服务名，如 nginx、docker、mysql"},
                    "server_id": {"type": "string", "description": "服务器 ID"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_port",
            "description": "检查端口占用情况（ss -tlnp）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "description": "端口号"},
                    "server_id": {"type": "string", "description": "服务器 ID"},
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_resources",
            "description": "查看 CPU、内存、磁盘使用情况。",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "服务器 ID"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "列出 systemd 服务单元及其运行状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "服务器 ID"},
                },
                "required": [],
            },
        },
    },
]

_CONFIRM_TIMEOUT = 300  # 危险确认等待秒数

# Agent 内部固定参数（不再暴露为可配置项）
MAX_ITERATIONS = 15      # 单轮对话最多工具调用轮次，防死循环
CMD_TIMEOUT = 30         # 单条 SSH 命令超时（秒）
OUTPUT_MAX_CHARS = 8000  # 单条命令输出回灌 LLM 的截断长度
# 历史裁剪预算由 config.json 的 llm.history_budget_chars 控制；0 = 不裁剪（大上下文模型）


def _sanitize_history(messages: list[dict]) -> list[dict]:
    """从会话存储还原 LLM 消息，剔除存储专用字段（ts/name/arguments 等），
    并保证 assistant.tool_calls 与 tool 消息严格成对。

    OpenAI 协议要求：带 tool_calls 的 assistant 消息后面必须紧跟每个
    tool_call_id 对应的 tool 消息。用户中途「停止」时，可能只执行了部分
    工具就中断，存储里就会留下没有响应的 tool_call。这类孤儿必须在发给
    模型前剔除，否则下一次对话会直接 400 报错、整个会话报废。
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role in ("user", "system"):
            out.append({"role": role, "content": m.get("content", "")})
        elif role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            tcs = m.get("tool_calls") or []
            if tcs:
                item["tool_calls"] = tcs
            if item["content"] or tcs:
                out.append(item)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })

    # ---- 修复成对关系 ----
    # 1) 收集所有实际存在的 tool 响应 id
    answered = {m["tool_call_id"] for m in out if m["role"] == "tool" and m.get("tool_call_id")}
    # 2) 剔除没有响应的 tool_call（含被取消、参数解析失败后跳过的）
    for m in out:
        if m["role"] != "assistant" or not m.get("tool_calls"):
            continue
        kept = [tc for tc in m["tool_calls"] if tc.get("id") in answered]
        if len(kept) != len(m["tool_calls"]):
            if kept:
                m["tool_calls"] = kept
            else:
                m.pop("tool_calls", None)
    # 3) 剔除找不到对应 tool_calls 的 tool 消息（例如 assistant 已被清空）
    valid_ids = {tc.get("id") for m in out if m["role"] == "assistant"
                 for tc in (m.get("tool_calls") or [])}
    out = [m for m in out if m["role"] != "tool" or m.get("tool_call_id") in valid_ids]
    # 4) 内容为空的 assistant 消息直接丢弃（避免无意义空轮）
    out = [m for m in out if not (m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls"))]
    return out


def _msg_chars(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        n += len(str(tc.get("function", {}).get("arguments") or "")) + 20
    return n


def _trim_history(history: list[dict], budget: int) -> tuple[list[dict], int]:
    """按字符预算裁剪历史，以「用户消息」为轮次边界整轮保留，
    绝不切断 assistant.tool_calls 与 tool 响应的配对。

    budget <= 0 表示不裁剪（大上下文模型如 1M 可全量发送）。
    当前这一轮（最后一条 user 之后的所有消息）永远完整保留；
    预算不够时从最老的轮次开始丢弃。
    返回 (裁剪后的消息列表, 被省略的轮次数)。
    """
    if budget <= 0 or not history:
        return history, 0

    total = sum(_msg_chars(m) for m in history)
    if total <= budget:
        return history, 0

    # 按 user 消息切分为轮次
    turns: list[list[dict]] = []
    for m in history:
        if m["role"] == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)

    keep: list[list[dict]] = [turns[-1]]          # 当前轮必留
    used = sum(_msg_chars(m) for m in turns[-1])
    for turn in reversed(turns[:-1]):
        turn_cost = sum(_msg_chars(m) for m in turn)
        if used + turn_cost > budget:
            break
        keep.insert(0, turn)
        used += turn_cost

    kept = [m for turn in keep for m in turn]
    return kept, len(turns) - len(keep)


_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]*$")
_SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9._@:-]+$")


def _safe_path(path: str) -> str:
    """校验文件路径：必须是绝对路径且不含 shell 元字符。"""
    path = (path or "").strip()
    if not path or not _SAFE_PATH_RE.match(path) or ".." in path:
        return ""
    return path


def _safe_service(service: str) -> str:
    """校验服务名：仅允许字母数字与 . _ - @ : 。"""
    service = (service or "").strip()
    if not service or not _SAFE_SERVICE_RE.match(service) or len(service) > 128:
        return ""
    return service


def _shell_quote(s: str) -> str:
    """POSIX shell 单引号转义。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


class AgentSession:
    """一个会话的 Agent 循环执行器（绑定一个用户：LLM 配置与服务器均按用户隔离）。"""
    def __init__(self, session_id: str, emit: EmitFn, default_server_id: Optional[str] = None,
                 model: Optional[str] = None, user_id: Optional[int] = None):
        self.session_id = session_id
        self.emit = emit
        self.default_server_id = default_server_id
        self.model = model
        self.user_id = user_id
        self.history: list[dict] = []
        self._pending_confirms: dict[str, asyncio.Future] = {}
        self._cancelled = False

    def load_history(self):
        session = get_session(self.session_id, self.user_id)
        if session:
            self.history = _sanitize_history(session.get("messages", []))
            if not self.default_server_id:
                self.default_server_id = session.get("server_id")

    def cancel(self):
        self._cancelled = True
        for fut in self._pending_confirms.values():
            if not fut.done():
                fut.set_result({"approved": False, "reason": "用户取消"})

    def handle_confirm(self, tool_call_id: str, approved: bool):
        fut = self._pending_confirms.get(tool_call_id)
        if fut and not fut.done():
            fut.set_result({"approved": approved})

    # ---------- 工具执行 ----------

    async def _exec_command(self, server_id: str, cmd: str, description: str, tool_call_id: str) -> dict:
        """执行命令，含安全分级与确认流程。返回工具结果 dict。"""
        risk, reason = classify_command(cmd)

        if risk == RiskLevel.FORBIDDEN:
            await self.emit({
                "type": "tool_call_end", "tool_call_id": tool_call_id,
                "success": False, "risk": "forbidden",
                "result": f"命令被安全策略拒绝（绝对禁止）：{reason}",
            })
            return {"error": f"命令被安全策略拒绝：{reason}"}

        if risk == RiskLevel.DANGEROUS:
            await self.emit({
                "type": "confirm_required", "tool_call_id": tool_call_id,
                "command": cmd, "reason": description or reason,
            })
            fut = asyncio.get_event_loop().create_future()
            self._pending_confirms[tool_call_id] = fut
            try:
                result = await asyncio.wait_for(fut, timeout=_CONFIRM_TIMEOUT)
            except asyncio.TimeoutError:
                result = {"approved": False, "reason": "确认超时"}
            finally:
                self._pending_confirms.pop(tool_call_id, None)

            if not result.get("approved"):
                msg = f"用户拒绝执行该危险操作。{result.get('reason', '')}"
                await self.emit({
                    "type": "tool_call_end", "tool_call_id": tool_call_id,
                    "success": False, "risk": "dangerous", "result": msg,
                })
                return {"error": msg}

        # 执行
        try:
            exec_result = await asyncio.to_thread(ssh_manager.exec_command, server_id, cmd, CMD_TIMEOUT)
        except SSHError as e:
            await self.emit({
                "type": "tool_call_end", "tool_call_id": tool_call_id,
                "success": False, "result": str(e),
            })
            return {"error": str(e)}

        max_chars = OUTPUT_MAX_CHARS
        stdout = exec_result["stdout"]
        stderr = exec_result["stderr"]
        if len(stdout) > max_chars:
            stdout = stdout[: max_chars // 2] + f"\n...[输出截断，共 {len(stdout)} 字符]...\n" + stdout[-max_chars // 2:]
        if len(stderr) > max_chars // 2:
            stderr = stderr[: max_chars // 2] + f"\n...[stderr 截断]...\n" + stderr[-200:]

        result_text = f"exit_code={exec_result['exit_code']} duration={exec_result['duration_ms']}ms\n--- stdout ---\n{stdout}"
        if stderr.strip():
            result_text += f"\n--- stderr ---\n{stderr}"

        await self.emit({
            "type": "tool_call_end", "tool_call_id": tool_call_id,
            "success": exec_result["exit_code"] == 0,
            "exit_code": exec_result["exit_code"],
            "result": result_text,
        })
        return {"output": result_text, "exit_code": exec_result["exit_code"]}

    def _abort_remaining(self, remaining: list[dict]):
        """用户中断后，为尚未执行的工具补齐占位响应。

        必须补齐：assistant 消息里已声明了这些 tool_call，如果缺少对应的
        tool 消息，下一轮请求会被 OpenAI 协议校验拒绝（400），整个会话就废了。
        """
        for tc in remaining:
            result = {"error": "用户取消了本次任务，该命令未执行"}
            self.history.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })
            append_message(self.session_id, {
                "role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                "content": json.dumps(result, ensure_ascii=False),
                "success": False, "ts": time.time(),
            })

    async def _dispatch_tool(self, name: str, args: dict, tool_call_id: str) -> dict:
        server_id = args.get("server_id") or self.default_server_id
        if not server_id:
            return {"error": "未指定服务器，且会话没有默认服务器。请在下拉框中选择目标服务器。"}

        # 服务器归属校验：只能操作当前用户自己的服务器（多用户隔离）
        if self.user_id is None or not get_server(str(server_id), self.user_id):
            return {"error": "服务器不存在或无权访问（服务器按用户隔离）"}

        if name == "run_command":
            cmd = args.get("cmd", "")
            if not isinstance(cmd, str) or not cmd.strip():
                return {"error": "cmd 参数为空"}
            if len(cmd) > 4000:
                return {"error": "命令过长（上限 4000 字符）"}
            return await self._exec_command(server_id, cmd, args.get("description", ""), tool_call_id)

        elif name == "read_file":
            path = _safe_path(args.get("path", ""))
            if not path:
                return {"error": "path 参数非法（必须是普通绝对路径，不能含 shell 元字符）"}
            tail = max(1, min(int(args.get("tail_lines", 200) or 200), 5000))
            cmd = f"tail -n {tail} {_shell_quote(path)}"
            return await self._exec_command(server_id, cmd, f"读取文件 {path}", tool_call_id)

        elif name == "get_service_status":
            svc = _safe_service(args.get("service", ""))
            if not svc:
                return {"error": "service 参数非法（只能是字母数字和 ._-:@）"}
            cmd = (f"systemctl status {svc} --no-pager -l | head -60; "
                   f"echo '--- journal ---'; journalctl -u {svc} -n 100 --no-pager | tail -100")
            return await self._exec_command(server_id, cmd, f"查看服务 {svc} 状态与日志", tool_call_id)

        elif name == "check_port":
            try:
                port = int(args.get("port"))
            except (TypeError, ValueError):
                return {"error": "port 参数非法"}
            if not (1 <= port <= 65535):
                return {"error": "port 超出范围（1-65535）"}
            cmd = f"ss -tlnp | grep ':{port} ' || echo '端口 {port} 未被监听'"
            return await self._exec_command(server_id, cmd, f"检查端口 {port}", tool_call_id)

        elif name == "check_resources":
            cmd = "echo '--- CPU/MEM ---'; top -bn1 | head -15; echo '--- DISK ---'; df -h; echo '--- FREE ---'; free -h"
            return await self._exec_command(server_id, cmd, "查看系统资源", tool_call_id)

        elif name == "list_services":
            cmd = "systemctl list-units --type=service --all --no-pager | head -80"
            return await self._exec_command(server_id, cmd, "列出服务单元", tool_call_id)

        else:
            return {"error": f"未知工具: {name}"}

    # ---------- 主循环 ----------

    async def run(self, user_message: str, server_id: Optional[str] = None):
        if server_id:
            self.default_server_id = server_id

        self.history.append({"role": "user", "content": user_message})
        append_message(self.session_id, {"role": "user", "content": user_message, "ts": time.time()})

        try:
            await self._agent_loop()
        except LLMError as e:
            await self.emit({"type": "error", "message": str(e)})
        except Exception as e:
            await self.emit({"type": "error", "message": f"Agent 异常: {e}"})
        finally:
            await self.emit({"type": "agent_done"})

    async def _agent_loop(self):
        max_iter = MAX_ITERATIONS

        for _ in range(max_iter):
            if self._cancelled:
                await self.emit({"type": "error", "message": "用户已取消"})
                return

            # 每轮重建消息列表（system + 历史）；预算为 0 时全量发送
            budget = int(get_llm_config_raw(self.user_id).get("history_budget_chars") or 0)
            trimmed, omitted = _trim_history(self.history, budget)
            messages = [{"role": "system", "content": self._build_system()}]
            if omitted:
                messages.append({
                    "role": "system",
                    "content": f"（上下文提示：为控制长度，更早的 {omitted} 轮对话已省略，"
                               f"仅保留最近若干轮。若需回顾早期内容，请让用户在界面左侧历史会话中查看。）",
                })
            messages += trimmed

            assistant_text_parts: list[str] = []
            tool_calls: list[dict] = []

            async for event in llm_client.chat_stream(self.user_id, messages, tools=AGENT_TOOLS, model=self.model):
                if self._cancelled:
                    await self.emit({"type": "error", "message": "用户已取消"})
                    return
                if event["type"] == "content":
                    assistant_text_parts.append(event["delta"])
                    await self.emit({"type": "assistant_thinking", "content": event["delta"]})
                elif event["type"] == "tool_call":
                    tool_calls = event["tool_calls"]

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": "".join(assistant_text_parts) or ""}

            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in tool_calls
                ]
                self.history.append(assistant_msg)
                append_message(self.session_id, dict(assistant_msg, ts=time.time()))

                for idx, tc in enumerate(tool_calls):
                    if self._cancelled:
                        # 中断时补齐未执行工具的占位响应，避免存储里留下孤儿 tool_call
                        self._abort_remaining(tool_calls[idx:])
                        return
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                        tool_result = {"error": f"工具参数解析失败: {tc['arguments'][:200]}"}
                        self.history.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(tool_result, ensure_ascii=False)})
                        append_message(self.session_id, {
                            "role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                            "arguments": {}, "content": json.dumps(tool_result, ensure_ascii=False),
                            "success": False, "ts": time.time(),
                        })
                        continue

                    # 发出工具开始事件（含风险预判）
                    risk, reason = classify_command(args.get("cmd", "")) if tc["name"] == "run_command" else (RiskLevel.SAFE, "")
                    await self.emit({
                        "type": "tool_call_start", "tool_call_id": tc["id"],
                        "name": tc["name"], "arguments": args,
                        "risk": risk.value if tc["name"] == "run_command" else "safe",
                    })

                    tool_result = await self._dispatch_tool(tc["name"], args, tc["id"])

                    self.history.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    })
                    append_message(self.session_id, {
                        "role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                        "arguments": args, "content": json.dumps(tool_result, ensure_ascii=False),
                        "ts": time.time(),
                    })
                continue  # 继续下一轮推理

            # 无工具调用 → 最终回答
            final_text = "".join(assistant_text_parts)
            if not final_text.strip():
                final_text = "（模型未返回内容，请检查 LLM 配置或换个说法重试）"
            self.history.append({"role": "assistant", "content": final_text})
            append_message(self.session_id, {"role": "assistant", "content": final_text, "ts": time.time()})
            await self.emit({"type": "final_answer", "content": final_text})
            return

        await self.emit({"type": "error", "message": f"已达最大迭代次数（{max_iter}），强制收敛"})

    def _build_system(self) -> str:
        servers = list_servers(self.user_id)
        server_lines = "\n".join(
            f"- id={s['id']} name={s.get('name') or s['host']} host={s['host']} ({s.get('os', 'linux')})"
            for s in servers
        )
        default_hint = f"\n当前会话默认服务器: {self.default_server_id}" if self.default_server_id else ""
        return f"{SYSTEM_PROMPT}\n\n# 可用服务器\n{server_lines or '（未配置服务器，请提示用户在"服务器管理"页面添加）'}{default_hint}"


# ---------- 简易封装（供路由层使用）----------

async def run_agent_turn(session_id: str, user_message: str, emit: EmitFn,
                         default_server_id: Optional[str] = None,
                         model: Optional[str] = None,
                         user_id: Optional[int] = None) -> AgentSession:
    agent = AgentSession(session_id, emit, default_server_id, model=model, user_id=user_id)
    agent.load_history()
    await agent.run(user_message, default_server_id)
    return agent
