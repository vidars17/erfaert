"""The agent loop, written out by hand (no framework, no SDK tool runner).

    send messages ──► model replies
        ▲                 │ stop_reason == "tool_use"?
        │                 ▼
    append tool_result ◄─ run the requested tools (concurrently)

Repeat until the model stops asking for tools, or a guard trips (iteration cap,
max_tokens, refusal). Every response's full ``content`` is appended back unchanged:
it carries thinking blocks the API needs to see again on the next call.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .config import FALLBACK_BETA, AgentConfig
from .prompts import SYSTEM_PROMPT, context_preamble
from .usage import Usage

log = logging.getLogger(__name__)

# Why a run ended. Only END_TURN means the model finished normally.
END_TURN = "end_turn"
MAX_ITERATIONS = "max_iterations"
MAX_TOKENS = "max_tokens"
REFUSAL = "refusal"


class ToolRunner(Protocol):
    """Anything with roadroute.Toolbox's shape: tool schemas plus a call() that never raises."""

    definitions: list[dict[str, Any]]

    def call(self, name: str, arguments: dict[str, Any] | None) -> str: ...


@dataclass
class ToolCall:
    iteration: int
    name: str
    input: dict[str, Any]
    result: str
    is_error: bool
    duration_ms: float


@dataclass
class AgentResult:
    answer: str
    stop: str  # END_TURN, MAX_ITERATIONS, MAX_TOKENS or REFUSAL
    iterations: int
    tool_calls: list[ToolCall]
    usage: Usage
    models_used: list[str]  # differs from the configured model if a fallback served a turn
    duration_s: float
    messages: list[dict[str, Any]] = field(repr=False)

    @property
    def ok(self) -> bool:
        return self.stop == END_TURN

    def to_dict(self) -> dict[str, Any]:
        """A JSON-friendly record of the run, for logs and eval reports."""
        return {
            "answer": self.answer,
            "stop": self.stop,
            "iterations": self.iterations,
            "models_used": self.models_used,
            "duration_s": round(self.duration_s, 2),
            "usage": self.usage.to_dict(),
            "tool_calls": [
                {
                    "iteration": c.iteration,
                    "name": c.name,
                    "input": c.input,
                    "is_error": c.is_error,
                    "duration_ms": round(c.duration_ms, 1),
                    "result": c.result,
                }
                for c in self.tool_calls
            ],
        }


def _is_error_result(text: str) -> bool:
    """roadroute tools signal failure with a JSON object that has an "error" key."""
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and "error" in payload


def _text_of(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


class Agent:
    def __init__(
        self,
        client: Any,
        tools: ToolRunner,
        config: AgentConfig | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """``client`` is an ``anthropic.Anthropic()`` (or a test double with the same shape)."""
        self.client = client
        self.tools = tools
        self.config = config or AgentConfig()
        self.clock = clock

    # -- one model call ------------------------------------------------------------------

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        cfg = self.config
        params: dict[str, Any] = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.tools.definitions,
            messages=messages,
            output_config={"effort": cfg.effort},
        )
        if cfg.prompt_caching:
            # Automatic caching: the API places the breakpoint on the last cacheable block,
            # so each call in the loop reuses the prefix the previous call wrote.
            params["cache_control"] = {"type": "ephemeral"}
        if cfg.fallbacks_enabled:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **params
            )
        return self.client.messages.create(**params)

    # -- tools ---------------------------------------------------------------------------

    def _run_one(self, iteration: int, block: Any) -> ToolCall:
        started = time.perf_counter()
        arguments = block.input if isinstance(block.input, dict) else {}
        result = self.tools.call(block.name, arguments)
        return ToolCall(
            iteration=iteration,
            name=block.name,
            input=arguments,
            result=result,
            is_error=_is_error_result(result),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def _run_tools(self, iteration: int, blocks: list[Any]) -> list[ToolCall]:
        if len(blocks) == 1:
            return [self._run_one(iteration, blocks[0])]
        workers = min(len(blocks), self.config.max_parallel_tools)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map() keeps the original order, so results line up with their tool_use ids.
            return list(pool.map(lambda b: self._run_one(iteration, b), blocks))

    # -- the loop ------------------------------------------------------------------------

    def run(self, question: str) -> AgentResult:
        started = time.perf_counter()
        usage = Usage()
        tool_calls: list[ToolCall] = []
        models_used: list[str] = []
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": context_preamble(self.clock())},
                    {"type": "text", "text": question},
                ],
            }
        ]

        def finish(answer: str, stop: str, iterations: int) -> AgentResult:
            result = AgentResult(
                answer=answer,
                stop=stop,
                iterations=iterations,
                tool_calls=tool_calls,
                usage=usage,
                models_used=models_used,
                duration_s=time.perf_counter() - started,
                messages=messages,
            )
            log.info("run finished: stop=%s iterations=%d %s", stop, iterations, usage.summary())
            return result

        for iteration in range(1, self.config.max_iterations + 1):
            response = self._create(messages)
            served_by = getattr(response, "model", None) or self.config.model
            if served_by not in models_used:
                models_used.append(served_by)
            usage.add(response.usage, served_by)
            # Append the whole content list unchanged (thinking blocks included).
            messages.append({"role": "assistant", "content": response.content})

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            log.info(
                "iteration %d: stop_reason=%s tools=%s in=%s out=%s",
                iteration,
                response.stop_reason,
                [b.name for b in tool_blocks],
                getattr(response.usage, "input_tokens", "?"),
                getattr(response.usage, "output_tokens", "?"),
            )

            if response.stop_reason == "tool_use" and tool_blocks:
                calls = self._run_tools(iteration, tool_blocks)
                tool_calls.extend(calls)
                # All results go back in ONE user message, matched by tool_use_id.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": call.result,
                                "is_error": call.is_error,
                            }
                            for block, call in zip(tool_blocks, calls)
                        ],
                    }
                )
                continue
            if response.stop_reason == "pause_turn":
                continue  # the API paused a long turn; sending the history again resumes it
            if response.stop_reason == "refusal":
                return finish(_text_of(response.content), REFUSAL, iteration)
            if response.stop_reason == "max_tokens":
                return finish(_text_of(response.content), MAX_TOKENS, iteration)
            # end_turn / stop_sequence: the model is done.
            return finish(_text_of(response.content), END_TURN, iteration)

        return finish("", MAX_ITERATIONS, self.config.max_iterations)
