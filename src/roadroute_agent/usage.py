"""Token accounting from the ``usage`` field of every response, plus a cost estimate.

Measure, don't guess: the API reports exact token counts on each response. Cost is derived
from a price table that WILL go stale, so treat the dollar figure as an estimate and check
https://platform.claude.com/docs/en/about-claude/pricing when it matters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# USD per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
# Prices as of 2026-11. The Haiku cache-read rate is an assumption (10% of input).
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-fable-5-2": (8.00, 40.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25


def _int(usage: Any, name: str) -> int:
    return int(getattr(usage, name, 0) or 0)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    def add(self, usage: Any, model: str) -> None:
        """Add one response's usage. ``model`` is the model that served it."""
        inp, out = _int(usage, "input_tokens"), _int(usage, "output_tokens")
        write, read = _int(usage, "cache_creation_input_tokens"), _int(usage, "cache_read_input_tokens")
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_creation_input_tokens += write
        self.cache_read_input_tokens += read
        price = PRICES.get(model)
        if price is None:
            self.unpriced_models.add(model)
            return
        p_in, p_out, p_read = price
        self.cost_usd += (
            inp * p_in + write * p_in * CACHE_WRITE_MULTIPLIER + read * p_read + out * p_out
        ) / 1_000_000

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def summary(self) -> str:
        text = (
            f"{self.calls} model calls, {self.total_input_tokens:,} input tokens "
            f"({self.cache_read_input_tokens:,} from cache), {self.output_tokens:,} output tokens, "
            f"~${self.cost_usd:.4f}"
        )
        if self.unpriced_models:
            text += f" (no price for {', '.join(sorted(self.unpriced_models))})"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }
