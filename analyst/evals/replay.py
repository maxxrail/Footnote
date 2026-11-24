"""Record real model responses once, replay them later with no API key.

The CI smoke eval replays a committed recording: the agent's model calls and the judge's
scores for each item, in the order they were made. Replay is exact as long as the tools
return what they returned when recording (same seed corpus, same embedder). If a run
asks for more calls than were recorded, `ReplayExhausted` is raised, so a behaviour
change can't silently pass.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from analyst.evals.graders import Judge, JudgeResult
from analyst.llm.client import LLMClient


class ReplayExhausted(RuntimeError):
    pass


class Recording:
    """{item id: [{"kind": "llm"|"judge", "data": ...}, ...]} with a lock for threads."""

    def __init__(
        self,
        data: dict[str, list[dict[str, Any]]] | None = None,
        meta: dict[str, Any] | None = None,
    ):
        self.items: dict[str, list[dict[str, Any]]] = data or {}
        self.meta: dict[str, Any] = meta or {}
        self._lock = threading.Lock()

    def append(self, item_id: str, kind: str, data: Any) -> None:
        with self._lock:
            self.items.setdefault(item_id, []).append({"kind": kind, "data": data})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"meta": self.meta, "items": dict(sorted(self.items.items()))}
        path.write_text(json.dumps(body, indent=1, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Recording:
        body = json.loads(path.read_text(encoding="utf-8"))
        return cls(body["items"], body.get("meta", {}))


class RecordingLLM:
    def __init__(self, inner: LLMClient, recording: Recording, item_id: str) -> None:
        self.inner, self.recording, self.item_id = inner, recording, item_id
        self.model = inner.model

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        resp = self.inner.create(system=system, messages=messages, tools=tools)
        self.recording.append(self.item_id, "llm", resp.model_dump(mode="json"))
        return resp


class ReplayLLM:
    def __init__(self, recording: Recording, item_id: str, kind: str = "llm") -> None:
        self.queue = [e["data"] for e in recording.items.get(item_id, []) if e["kind"] == kind]
        self.item_id = item_id
        self.model = recording.meta.get("model", "replay")

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        if not self.queue:
            raise ReplayExhausted(f"{self.item_id}: no more recorded model responses")
        data = self.queue.pop(0)
        try:
            return BetaMessage.model_validate(data)
        except ValueError:  # pragma: no cover - recordings from the non-beta endpoint
            return Message.model_validate(data)


class RecordingJudge:
    def __init__(self, inner: Judge, recording: Recording, item_id: str) -> None:
        self.inner, self.recording, self.item_id = inner, recording, item_id
        self.model = inner.model

    def score(self, prompt: str) -> tuple[JudgeResult, object]:
        result, usage = self.inner.score(prompt)
        dumped = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else None
        self.recording.append(
            self.item_id, "judge", {"result": result.model_dump(), "usage": dumped}
        )
        return result, usage


class ReplayJudge:
    def __init__(self, recording: Recording, item_id: str) -> None:
        self.queue = [e["data"] for e in recording.items.get(item_id, []) if e["kind"] == "judge"]
        self.item_id = item_id
        self.model = recording.meta.get("judge_model", "replay")

    def score(self, prompt: str) -> tuple[JudgeResult, object]:
        if not self.queue:
            raise ReplayExhausted(f"{self.item_id}: no more recorded judge scores")
        data = self.queue.pop(0)
        usage = SimpleNamespace(**data["usage"]) if data.get("usage") else None
        return JudgeResult.model_validate(data["result"]), usage
