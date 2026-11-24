"""`LLMClient` protocol, the Anthropic implementation, and token cost accounting.

The agent loop talks only to `LLMClient.create(...)`, which returns an Anthropic
`Message`-shaped object (`content`, `stop_reason`, `usage`, `model`). Tests pass
`analyst.llm.fake.ScriptedLLM`; the CLI passes `AnthropicLLM`.

Request settings for the real client:
- model from `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`); adaptive thinking is the
  model's default, so `thinking` is not sent. Effort comes from `AGENT_EFFORT`.
- Server-side refusal fallback (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`):
  if a safety classifier declines, the API re-runs the request on a fallback model in the
  same call. `Message.model` then names the model that answered, and cost uses its price.
- The system prompt and tool list never change between turns, so they are cached
  (`cache_control` on the system block; tools render before it and are cached with it).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

# USD per million tokens: input, output, cache write (5-minute), cache read.
# Source: Anthropic's published API prices (claude-api skill table, cached 2026-11-03).
PRICES: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10"), Decimal("2.5"), Decimal("0.10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("2.5"), Decimal("0.20")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("5"), Decimal("0.20")),
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.125"), Decimal("0.01")),
}
MILLION = Decimal(1_000_000)


@dataclass
class Usage:
    """Tokens and dollars summed over every LLM call in a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal | None = Decimal(0)  # None once any call used a model with no price
    calls: int = 0

    def add(self, model: str, usage: Any) -> None:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_write_tokens += cw
        self.cache_read_tokens += cr
        self.calls += 1
        c = cost_usd(model, inp, out, cw, cr)
        self.cost_usd = None if c is None or self.cost_usd is None else self.cost_usd + c


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0
) -> Decimal | None:
    """Dollar cost of one call, or None if the model has no known price."""
    if model not in PRICES:
        return None
    pi, po, pw, pr = PRICES[model]
    return (
        Decimal(input_tokens) * pi
        + Decimal(output_tokens) * po
        + Decimal(cache_write) * pw
        + Decimal(cache_read) * pr
    ) / MILLION


class LLMClient(Protocol):
    model: str

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        """One Messages API call. Returns an object shaped like `anthropic.types.Message`."""
        ...


class AnthropicLLM:
    """The real client. Needs `ANTHROPIC_API_KEY` (or another credential the SDK finds)."""

    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import anthropic

        from analyst.config import get_settings

        settings = get_settings()
        self.model = model or settings.anthropic_model
        self.effort = effort or settings.agent_effort
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:  # pragma: no cover - live only
        return self._client.beta.messages.create(  # type: ignore[call-overload]
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
