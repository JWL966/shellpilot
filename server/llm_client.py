"""LLM 客户端：兼容 OpenAI Chat Completions 协议（DeepSeek/通义/Kimi/OpenAI 等）。

每个用户有独立的 LLM 配置（base_url / api_key / 模型），调用时传入 user_id。
"""
import asyncio
import json
from typing import Any, AsyncIterator, Optional

import aiohttp

from config import get_llm_config_raw, resolve_model


class LLMError(Exception):
    pass


# 网络类瞬态错误（连接超时/中断/服务端过载）自动重试的次数与退避基数。
# 公网链路间歇丢包（如"信号灯超时时间已到"）重试一次通常即可恢复。
LLM_RETRY_ATTEMPTS = 3
LLM_RETRY_BASE_DELAY = 1.0


def _is_transient(err: Exception) -> bool:
    """是否值得重试：网络层错误/超时/网关类 5xx。HTTP 401/400 等确定性失败不重试。"""
    if isinstance(err, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    if isinstance(err, LLMError):
        # HTTP 502/503/504/429（网关/过载）带在错误文本里
        return any(code in str(err) for code in ("HTTP 502", "HTTP 503", "HTTP 504", "HTTP 429"))
    return False


class LLMClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    def _headers(self, user_id: int) -> dict[str, str]:
        cfg = get_llm_config_raw(user_id)
        return {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }

    def _endpoint(self, user_id: int) -> str:
        cfg = get_llm_config_raw(user_id)
        base = cfg["base_url"].rstrip("/")
        return f"{base}/chat/completions"

    def _payload(self, user_id: int, messages: list[dict], tools: Optional[list] = None,
                 stream: bool = False, model: Optional[str] = None) -> dict:
        cfg = get_llm_config_raw(user_id)
        payload: dict[str, Any] = {
            "model": resolve_model(user_id, model) or cfg["model"],
            "messages": messages,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def chat(self, user_id: int, messages: list[dict], tools: Optional[list] = None,
                   model: Optional[str] = None) -> dict:
        """非流式请求，返回完整响应 JSON。瞬态网络错误自动重试。"""
        await self._ensure_session()
        cfg = get_llm_config_raw(user_id)
        payload = self._payload(user_id, messages, tools, stream=False, model=model)
        timeout = aiohttp.ClientTimeout(total=cfg.get("timeout", 120))

        last_err: Optional[Exception] = None
        for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
            try:
                async with self._session.post(
                    self._endpoint(user_id),
                    headers=self._headers(user_id),
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise LLMError(f"LLM 请求失败 HTTP {resp.status}: {body[:500]}")
                    return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError, LLMError) as e:
                last_err = e
                if attempt < LLM_RETRY_ATTEMPTS and _is_transient(e):
                    await asyncio.sleep(LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue
                if isinstance(e, LLMError):
                    raise
                raise LLMError(f"LLM 网络错误: {e}") from e
        raise LLMError(f"LLM 网络错误: {last_err}")

    async def chat_stream(self, user_id: int, messages: list[dict], tools: Optional[list] = None,
                          model: Optional[str] = None) -> AsyncIterator[dict]:
        """流式请求，yield 增量事件：
        {"type": "content", "delta": "..."} 或 {"type": "tool_call", "tool_calls": [...]}（最终聚合）。

        首字节前的瞬态网络错误自动重试；一旦开始收到流式数据就不再重试
        （重发会导致内容重复，且此时链路已证明可用）。
        """
        await self._ensure_session()
        cfg = get_llm_config_raw(user_id)
        tool_call_acc: dict[int, dict] = {}
        payload = self._payload(user_id, messages, tools, stream=True, model=model)
        timeout = aiohttp.ClientTimeout(total=cfg.get("timeout", 120))

        last_err: Optional[Exception] = None
        for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
            try:
                resp = await self._session.post(
                    self._endpoint(user_id),
                    headers=self._headers(user_id),
                    json=payload,
                    timeout=timeout,
                )
                if resp.status != 200:
                    body = await resp.text()
                    resp.release()
                    raise LLMError(f"LLM 请求失败 HTTP {resp.status}: {body[:500]}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 尚未拿到响应头：安全重试（无内容重复风险）
                last_err = e
                if attempt < LLM_RETRY_ATTEMPTS:
                    await asyncio.sleep(LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue
                raise LLMError(f"LLM 网络错误: {e}") from e

            # 已拿到响应头，开始消费流；此阶段失败不重试（避免内容重复）
            try:
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        yield {"type": "content", "delta": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tool_call_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] += fn["name"]  # 流式 name 可能分片
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
                if tool_call_acc:
                    tool_calls = [
                        {"id": acc["id"] or f"call_{idx}", "name": acc["name"], "arguments": acc["arguments"]}
                        for idx, acc in sorted(tool_call_acc.items())
                    ]
                    yield {"type": "tool_call", "tool_calls": tool_calls}
                return  # 本轮成功，结束
            except aiohttp.ClientError as e:
                # 流中途断开：重试会导致已 yield 的内容重复，交给上层按错误处理
                raise LLMError(f"LLM 流式传输中断: {e}") from e

    async def test_connection(self, user_id: int, model: Optional[str] = None) -> dict[str, Any]:
        """测试连接：带一次重试，网络抖动不至于直接报失败。"""
        last_err: Optional[str] = None
        for attempt in range(1, 3):
            try:
                await self._ensure_session()
                cfg = get_llm_config_raw(user_id)
                async with self._session.post(
                    self._endpoint(user_id),
                    headers=self._headers(user_id),
                    json={"model": resolve_model(user_id, model) or cfg["model"],
                          "messages": [{"role": "user", "content": "ping"}]},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        return {"success": True, "output": "LLM 连接成功"}
                    last_err = f"HTTP {resp.status}: {body[:300]}"
            except Exception as e:
                last_err = str(e)
            if attempt < 2:
                await asyncio.sleep(1.0)
        return {"success": False, "error": last_err or "未知错误"}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


llm_client = LLMClient()
