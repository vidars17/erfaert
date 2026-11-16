"""Agent settings in one place, so evals can sweep them (model, effort, caps)."""
from __future__ import annotations

from dataclasses import dataclass

# Haiku is the development default (cheap); run final evals on Sonnet/Opus for comparison.
DEFAULT_MODEL = "claude-haiku-5-5"

# Models that support the server-side refusal fallback (beta). On these the agent asks the
# API to re-run a declined request on a fallback model it picks ("default"), instead of the
# run simply stopping with stop_reason "refusal". Haiku has no server-side fallback.
FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class AgentConfig:
    model: str = DEFAULT_MODEL
    # Thinking can't be switched off on current models; effort is the cost lever.
    # "low" suits this task: a few tool calls, then a short explanation.
    effort: str = "low"
    max_tokens: int = 16000
    # Hard stop for a loop that keeps calling tools. A normal run needs 2-3 model calls.
    max_iterations: int = 6
    # Parallel tool calls from one model turn are run concurrently, up to this many.
    max_parallel_tools: int = 4
    use_fallbacks: bool = True
    prompt_caching: bool = True

    @property
    def fallbacks_enabled(self) -> bool:
        return self.use_fallbacks and self.model in FALLBACK_MODELS
