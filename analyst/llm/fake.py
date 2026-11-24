"""A scripted fake LLM for tests: it replays predetermined turns and records every request.

Each scripted turn is a list of blocks built with `tool_call(...)` and `text(...)`. A turn
may also be a callable that sees the request (messages so far) and returns blocks, so a
test can react to tool output (e.g. cite the ref a tool returned).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any

from anthropic.types import Message

Block = dict[str, Any]
Turn = Sequence[Block] | Callable[[list[dict[str, Any]]], Sequence[Block]]

_ids = itertools.count(1)


def tool_call(name: str, **input: Any) -> Block:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": input}


def text(t: str) -> Block:
    return {"type": "text", "text": t}


class ScriptedLLM:
    model = "fake-model"

    def __init__(self, turns: Sequence[Turn], input_tokens: int = 100, output_tokens: int = 20):
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Message:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.turns:
            raise AssertionError("ScriptedLLM ran out of turns")
        turn = self.turns.pop(0)
        blocks = list(turn(messages) if callable(turn) else turn)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return Message.model_validate(
            {
                "id": f"msg_{next(_ids):04d}",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": blocks,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            }
        )


def last_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool_result blocks of the latest user message (helper for callable turns)."""
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            return [
                b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
    return []
