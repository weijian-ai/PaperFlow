from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperflow.core.compact.compactor import Compactor
from paperflow.core.context import ExecutionContext
from paperflow.core.events.bus import EventBus
from paperflow.core.llm.types import LlmResponse, UsageStats


def _stub_provider(summary: str = "## 1. Original Goal\nTest\n## 2. Completed Steps\n- done") -> Any:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=summary,
        usage=UsageStats(input_tokens=100, output_tokens=30),
    ))
    return provider


def _make_messages(n: int = 5) -> list[dict[str, Any]]:
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": "user message " + "x" * 200})
        msgs.append({"role": "assistant", "content": "assistant reply " + "y" * 200})
    return msgs


# 鍔熻兘锛氶獙璇?compact_messages 鎴愬姛鏃?provider.chat 琚皟鐢ㄤ竴娆′笖涓嶄紶宸ュ叿 schema
# 璁捐锛歴tub provider 杩斿洖闈炵┖鎽樿锛屾柇瑷€ chat 璋冪敤涓€娆★紝tool_schemas=[]
def test_compact_messages_calls_provider(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    messages = _make_messages()

    result = asyncio.run(
        compactor.compact_messages(messages, provider)
    )

    assert result is not None
    provider.chat.assert_called_once()
    call_kwargs = provider.chat.call_args
    assert call_kwargs.kwargs.get("tool_schemas") == [] or call_kwargs.args[1] == []


# 鍔熻兘锛氶獙璇?compact_messages 杩斿洖鐨勬憳瑕佹枃鏈潵鑷?provider 鍝嶅簲
# 璁捐锛歴tub provider 杩斿洖鍥哄畾鎽樿瀛楃涓诧紝鏂█ result.summary_text 绛変簬璇ュ瓧绗︿覆
def test_compact_messages_returns_summary(tmp_path: Path) -> None:
    expected = "## 1. Original Goal\nDo X\n## 2. Completed\n- step one"
    provider = _stub_provider(summary=expected)
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")

    result = asyncio.run(
        compactor.compact_messages(_make_messages(), provider)
    )

    assert result is not None
    assert result.summary_text == expected


# 鍔熻兘锛氶獙璇?compact() 灏?context.messages 鏇挎崲涓轰袱鏉℃憳瑕佹秷鎭
# 璁捐锛氳皟鐢?compact() 鍚庢柇瑷€ messages 闀垮害涓?2锛宺ole 鍒嗗埆涓?user/assistant
def test_compact_replaces_context_messages(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    assert len(ctx.messages) == 2
    assert ctx.messages[0]["role"] == "user"
    assert ctx.messages[1]["role"] == "assistant"


# 鍔熻兘锛氶獙璇?compact() 鍦?session 鐩綍鍐欏叆 summary_*.md 鏂囦欢
# 璁捐锛氫娇鐢?tmp_path锛岃皟鐢?compact() 鍚庢鏌ョ洰褰曞唴鏄惁瀛樺湪 summary_ 寮€澶寸殑鏂囦欢
def test_compact_writes_summary_file(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    summary_files = list(tmp_path.glob("summary_*.md"))
    assert len(summary_files) == 1


# 鍔熻兘锛氶獙璇?compact() 鎴愬姛鍚庡彂甯?ContextCompactedEvent 浜嬩欢
# 璁捐锛氳闃?EventBus锛屾敹闆嗕簨浠讹紝鏂█鏀跺埌绫诲瀷涓?context.compacted 鐨勪簨浠?def test_compact_publishes_event(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    received: list[Any] = []

    async def handler(event: Any) -> None:
        received.append(event)

    bus.subscribe(handler)
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    types = [getattr(e, "type", None) for e in received]
    assert "context.compacted" in types


# 鍔熻兘锛氶獙璇?provider 鎶涘紓甯告椂 context.messages 淇濇寔涓嶅彉
# 璁捐锛歴tub provider.chat 鎶?RuntimeError锛屾柇瑷€ compact() 杩斿洖 None 涓?messages 鏈淇敼
def test_compact_failure_preserves_context(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("LLM error"))
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    original_messages = _make_messages()
    ctx.messages = list(original_messages)

    result = asyncio.run(compactor.compact(ctx, provider))

    assert result is None
    assert ctx.messages == original_messages
