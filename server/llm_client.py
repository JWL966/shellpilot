"""LLM 客户端：兼容 OpenAI Chat Completions 协议（DeepSeek/通义/Kimi/OpenAI 等）。

每个用户有独立的 LLM 配置（base_url / api_key / 模型），调用时传入 user_id。
"""
import json
from typing import Any, AsyncIterator, Optional

import aiohttp

from config import get_llm_config_raw, resolve_model


class LLMError(Exception):
    pass


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
        """非流式请求，返回完整响应 JSON。"""
        await self._ensure_session()
        cfg = get_llm_config_raw(user_id)
        try:
            async with self._session.post(
                self._endpoint(user_id),
                headers=self._headers(user_id),
                json=self._payload(user_id, messages, tools, stream=False, model=model),
                timeout=aiohttp.ClientTimeout(total=cfg.get("timeout", 120)),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise LLMError(f"LLM 请求失败 HTTP {resp.status}: {body[:500]}")
                data = json.loads(body)
                return data
        except aiohttp.ClientError as e:
            raise LLMError(f"LLM 网络错误: {e}") from e

    async def chat_stream(self, user_id: int, messages: list[dict], tools: Optional[list] = None,
                          model: Optional[str] = None) -> AsyncIterator[dict]:
        """流式请求，yield 增量事件：
        {"type": "content", "delta": "..."} 或 {"type": "tool_call", "tool_calls": [...]}（最终聚合）。
        """
        await self._ensure_session()
        cfg = get_llm_config_raw(user_id)
        tool_call_acc: dict[int, dict] = {}
        try:
            async with self._session.post(
                self._endpoint(user_id),
                headers=self._headers(user_id),
                json=self._payload(user_id, messages, tools, stream=True, model=model),
                timeout=aiohttp.ClientTimeout(total=cfg.get("timeout", 120)),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise LLMError(f"LLM 请求失败 HTTP {resp.status}: {body[:500]}")
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
        except aiohttp.ClientError as e:
            raise LLMError(f"LLM 网络错误: {e}") from e

    async def test_connection(self, user_id: int, model: Optional[str] = None) -> dict[str, Any]:
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
                return {"success": False, "error": f"HTTP {resp.status}: {body[:300]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


llm_client = LLMClient()
