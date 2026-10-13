from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from paperflow.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from paperflow.core.events.bus import EventBus
from paperflow.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

# 常见 OpenAI 兼容模型的最大 context window（token 数），用于计算 context 水位
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
}

_DEFAULT_CONTEXT_WINDOW = 128_000

_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

log = logging.getLogger(__name__)


# 返回指定模型的最大 context window token 数
def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)


_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将 Anthropic 风格的内容块列表（text/tool_use/tool_result/thinking）转换为 OpenAI 消息片段
def _blocks_to_openai(role: str, content: list[Any]) -> list[dict[str, Any]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "thinking":
            # OpenAI 兼容接口不支持回传 thinking 块，直接丢弃
            continue
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
        elif btype == "tool_result":
            # tool_result 在 OpenAI 协议里是独立的 role=tool 消息，必须紧跟在 assistant 之后
            result_content = block.get("content", "")
            if not isinstance(result_content, str):
                result_content = json.dumps(result_content, ensure_ascii=False)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": result_content,
                }
            )

    messages: list[dict[str, Any]] = []
    joined = "".join(text_parts)

    # 纯 tool_result 的 user 消息会被拆成多条 role=tool 消息
    if role == "user" and tool_messages and not joined:
        return tool_messages

    if tool_calls:
        messages.append({"role": "assistant", "content": joined, "tool_calls": tool_calls})
    else:
        messages.append({"role": role, "content": joined})

    # 混合内容（既有文本又有 tool_result）时，把 tool 消息追加在后面保持配对
    messages.extend(tool_messages)
    return messages


# 将项目内部的 Anthropic 风格消息列表转换为 OpenAI Chat Completions 消息列表
def convert_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role = str(msg.get("role", "user"))
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        elif isinstance(content, list):
            out.extend(_blocks_to_openai(role, content))
        else:
            out.append({"role": role, "content": str(content)})

    # 合并相邻的同角色纯文本消息，避免部分服务端拒绝连续同角色输入
    merged: list[dict[str, Any]] = []
    for m in out:
        if (
            merged
            and merged[-1].get("role") == m.get("role") == "user"
            and isinstance(merged[-1].get("content"), str)
            and isinstance(m.get("content"), str)
            and "tool_calls" not in merged[-1]
        ):
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{m['content']}"
        else:
            merged.append(m)
    return merged


# 将 Anthropic 风格的 tool schema 转换为 OpenAI function calling 格式
def convert_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for schema in tool_schemas:
        name = schema.get("name")
        if not name:
            continue
        parameters = schema.get("input_schema") or {"type": "object", "properties": {}}
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": str(schema.get("description", "")),
                    "parameters": parameters,
                },
            }
        )
    return tools


# 将 OpenAI 的 finish_reason 映射为项目内部使用的 stop_reason
def _map_finish_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


class OpenAiCompatProvider:
    # 初始化 OpenAI 兼容 provider，读取 base_url 与 api_key；client 可在测试时注入
    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        if client is None and not resolved_key:
            raise SystemExit("OPENAI_API_KEY (or ANTHROPIC_API_KEY) not set")

        resolved_base = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.openai.com/v1"
        )
        self._base_url = resolved_base.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(600.0, connect=30.0),
        )
        self._owns_client = client is None

    # 关闭内部创建的 HTTP 客户端，释放连接池
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # 流式调用 OpenAI 兼容接口，逐 token 发布事件并返回 LlmResponse；网络中断时自动重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": convert_messages(messages, system or _SYSTEM_PROMPT),
        }
        tools = convert_tools(tool_schemas)
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_raw: dict[str, Any] = {}

        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            text_parts = []
            tool_acc = {}
            finish_reason = None
            usage_raw = {}
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise httpx.HTTPStatusError(
                            f"LLM API {response.status_code}: {body[:800]}",
                            request=response.request,
                            response=response,
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        if isinstance(chunk.get("usage"), dict):
                            usage_raw = chunk["usage"]

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}

                        piece = delta.get("content")
                        if piece:
                            if attempt == 1:
                                await bus.publish(
                                    LlmTokenEvent(run_id=run_id, token=str(piece), ts=_now())
                                )
                            text_parts.append(str(piece))

                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            slot = tool_acc.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = str(tc["id"])
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = str(fn["name"])
                            if fn.get("arguments"):
                                slot["arguments"] += str(fn["arguments"])
                    break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                if attempt == _MAX_STREAM_RETRIES:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        _MAX_STREAM_RETRIES, run_id, step, exc,
                    )
                    raise
                delay = _RETRY_BACKOFF_S[attempt - 1]
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s — retrying in %.0fs",
                    attempt, _MAX_STREAM_RETRIES, run_id, step, exc, delay,
                )
                await asyncio.sleep(delay)

        tool_calls: list[ToolCallBlock] = []
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            if not slot["name"]:
                continue
            raw_args = slot["arguments"].strip() or "{}"
            try:
                parsed = json.loads(raw_args)
                if not isinstance(parsed, dict):
                    parsed = {"value": parsed}
            except json.JSONDecodeError:
                log.warning("tool %s returned non-JSON arguments: %s", slot["name"], raw_args)
                parsed = {"_raw": raw_args}
            tool_calls.append(
                ToolCallBlock(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    input=parsed,
                )
            )

        input_tokens = int(usage_raw.get("prompt_tokens") or 0)
        output_tokens = int(usage_raw.get("completion_tokens") or 0)
        cache_read = int(usage_raw.get("prompt_cache_hit_tokens") or 0)
        context_pct = input_tokens / _context_window(self._model)

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=0,
                context_pct=context_pct,
                ts=_now(),
            )
        )

        return LlmResponse(
            stop_reason=_map_finish_reason(finish_reason, bool(tool_calls)),
            tool_calls=tool_calls,
            text="".join(text_parts),
            thinking_blocks=[],
            usage=UsageStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=0,
                context_pct=context_pct,
            ),
        )
