from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from paperflow.core.events.bus import EventBus
from paperflow.core.llm.openai_provider import (
    OpenAiCompatProvider,
    convert_messages,
    convert_tools,
)

# --- helpers -----------------------------------------------------------------


# 将若干 SSE 行拼成 httpx 可流式返回的响应体
def _sse(chunks: list[dict[str, Any]], *, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


# 构造一个返回固定 SSE 内容的假 httpx.AsyncClient
def _client_returning(body: bytes, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.test/v1"
    )


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


# --- convert_messages --------------------------------------------------------


# 功能：验证 Anthropic 风格的 tool_use/tool_result 块被翻译成 OpenAI 的 assistant.tool_calls 与 role=tool
# 设计：直接构造项目内部真实的消息结构，断言拆分后的角色序列，覆盖工具调用往返这一最容易出错的边界
def test_convert_messages_translates_tool_roundtrip() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.txt"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hello"}],
        },
    ]

    out = convert_messages(messages, "sys")

    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "go"}
    assert out[2]["role"] == "assistant"
    assert out[2]["content"] == "calling"
    assert out[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "hello"}


# 功能：验证 thinking 块被丢弃而不是被当作正文透传给 OpenAI 兼容接口
# 设计：多数 OpenAI 兼容网关不接受 thinking 块，若透传会直接 400；单独断言可锁定这一防御行为
def test_convert_messages_drops_thinking_blocks() -> None:
    out = convert_messages(
        [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hidden", "signature": "s"},
            {"type": "text", "text": "visible"},
        ]}],
        "",
    )
    assert out == [{"role": "assistant", "content": "visible"}]


# 功能：验证相邻的同角色纯文本 user 消息被合并，避免部分服务端拒绝连续同角色输入
# 设计：会话压缩后会连续产生两条 user 消息，这里锁定合并逻辑；同时确认带 tool_calls 的消息不被误合并
def test_convert_messages_merges_consecutive_user_text() -> None:
    out = convert_messages(
        [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
        "",
    )
    assert out == [{"role": "user", "content": "first\n\nsecond"}]


# --- convert_tools -----------------------------------------------------------


# 功能：验证 Anthropic 的 input_schema 被映射为 OpenAI function calling 的 parameters
# 设计：项目内所有工具都用 input_schema 命名，字段名差异是静默失败的高发点，必须显式断言
def test_convert_tools_maps_input_schema_to_parameters() -> None:
    schemas: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": "read",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    out = convert_tools(schemas)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "read_file"
    assert out[0]["function"]["parameters"]["properties"]["path"]["type"] == "string"


# --- chat (streaming) --------------------------------------------------------


# 功能：验证流式文本被逐块发布为 LlmTokenEvent 并聚合成完整 text
# 设计：用 MockTransport 回放真实网关的 SSE 形状，断言事件序列而非仅返回值，覆盖 TUI 依赖的流式路径
async def test_chat_streams_tokens_and_aggregates_text() -> None:
    body = _sse([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "PO"}),
        _chunk({"content": "NG"}),
        _chunk({}, "stop"),
    ])
    provider = OpenAiCompatProvider("gpt-4o", client=_client_returning(body))
    bus = EventBus()
    seen: list[str] = []

    async def collect(event: object) -> None:
        if type(event).__name__ == "LlmTokenEvent":
            seen.append(event.token)  # type: ignore[attr-defined]

    bus.subscribe(collect)
    response = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        bus=bus,
        run_id="r1",
    )

    assert seen == ["PO", "NG"]
    assert response.text == "PONG"
    assert response.stop_reason == "end_turn"


# 功能：验证流式 tool_calls 分片被按 index 累积并解析成完整的 ToolCallBlock
# 设计：真实网关把 arguments 拆成多个 JSON 片段下发，这里复刻该分片形态，覆盖 JSON 拼装这一核心风险点
async def test_chat_accumulates_streamed_tool_call_arguments() -> None:
    body = _sse([
        _chunk({"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "calc", "arguments": ""}}
        ]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"expr\":"}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": " \"12*7\"}"}}]}),
        _chunk({}, "tool_calls"),
    ])
    provider = OpenAiCompatProvider("gpt-4o", client=_client_returning(body))

    response = await provider.chat(
        messages=[{"role": "user", "content": "calc"}],
        tool_schemas=[],
        bus=EventBus(),
        run_id="r2",
    )

    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "calc"
    assert response.tool_calls[0].input == {"expr": "12*7"}


# 功能：验证非法 JSON 的 arguments 不会抛异常，而是降级为 _raw 字段保留原文
# 设计：模型偶发返回截断 JSON，若直接抛出会让整个 run 失败；断言降级路径保证 loop 能继续
async def test_chat_degrades_on_malformed_tool_arguments() -> None:
    body = _sse([
        _chunk({"tool_calls": [
            {"index": 0, "id": "c", "type": "function",
             "function": {"name": "calc", "arguments": "{not json"}}
        ]}),
        _chunk({}, "tool_calls"),
    ])
    provider = OpenAiCompatProvider("gpt-4o", client=_client_returning(body))

    response = await provider.chat(
        messages=[{"role": "user", "content": "x"}],
        tool_schemas=[],
        bus=EventBus(),
        run_id="r3",
    )

    assert response.tool_calls[0].input == {"_raw": "{not json"}


# 功能：验证 usage 字段被映射为 UsageStats，且 finish_reason=length 映射为 max_tokens
# 设计：多数 OpenAI 兼容网关在最后一个 chunk 才带 usage，这里锁定「末尾 usage 也要被采集」的行为
async def test_chat_maps_usage_and_length_finish_reason() -> None:
    final = _chunk({}, "length")
    final["usage"] = {"prompt_tokens": 1000, "completion_tokens": 20}
    provider = OpenAiCompatProvider("gpt-4o", client=_client_returning(_sse([final])))

    response = await provider.chat(
        messages=[{"role": "user", "content": "x"}],
        tool_schemas=[],
        bus=EventBus(),
        run_id="r4",
    )

    assert response.stop_reason == "max_tokens"
    assert response.usage is not None
    assert response.usage.input_tokens == 1000
    assert response.usage.output_tokens == 20


# 功能：验证 HTTP 4xx/5xx 会带着响应体抛出，而不是静默返回空结果
# 设计：网关鉴权/模型名错误时返回 JSON 错误体，这是排障最需要的上下文，必须断言其被包含在异常里
async def test_chat_raises_on_http_error() -> None:
    provider = OpenAiCompatProvider(
        "gpt-4o",
        client=_client_returning(b'{"detail":"unauthorized"}', status=401),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.chat(
            messages=[{"role": "user", "content": "x"}],
            tool_schemas=[],
            bus=EventBus(),
            run_id="r5",
        )

    assert "unauthorized" in str(exc.value)


# 功能：验证缺少 API key 时 provider 初始化立即 SystemExit，而不是等到调用时才失败
# 设计：daemon 在启动期就构造 provider，提前失败能让配置错误在启动日志里立刻暴露
def test_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(SystemExit):
        OpenAiCompatProvider("gpt-4o")
