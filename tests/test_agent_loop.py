"""Unit tests for the agent loop against a scripted fake model: no network, no API key, free.

These check the loop's mechanics (message shapes, guards, accounting). Whether the real
model behaves well is a separate question, answered by the evals in evals/.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from roadroute_agent import Agent, AgentConfig
from roadroute_agent.loop import END_TURN, MAX_ITERATIONS, MAX_TOKENS, REFUSAL
from roadroute_agent.prompts import SYSTEM_PROMPT

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def text(t: str):
    return SimpleNamespace(type="text", text=t)


def tool_use(name: str, input: dict, id: str):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def reply(stop_reason: str, *content, model: str = "claude-haiku-5-5", inp: int = 1000, out: int = 100):
    usage = SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage, model=model)


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **params):
        # Snapshot the message list: the loop keeps appending to the same list object.
        self.calls.append({**params, "messages": list(params["messages"])})
        if not self.replies:
            raise AssertionError("the loop called the model more times than scripted")
        return self.replies.pop(0)


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)
        self.beta = SimpleNamespace(messages=self.messages)


class FakeTools:
    definitions = [{"name": "get_route_conditions", "description": "d", "input_schema": {"type": "object"}}]

    def __init__(self, results=None, delay_s: float = 0.0):
        self.results = results or {}
        self.delay_s = delay_s
        self.calls: list[tuple[str, dict]] = []
        self.threads: set[int] = set()

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        self.threads.add(threading.get_ident())
        time.sleep(self.delay_s)
        return self.results.get(arguments.get("destination"), json.dumps({"total_km": 10.0}))


def make_agent(replies, tools=None, **config):
    client = FakeClient(replies)
    agent = Agent(client, tools or FakeTools(), AgentConfig(**config), clock=lambda: NOW)
    return agent, client


def test_answers_directly_without_tools():
    agent, client = make_agent([reply("end_turn", text("Hello."))])
    result = agent.run("hi")
    assert result.stop == END_TURN and result.ok
    assert result.answer == "Hello."
    assert result.iterations == 1 and result.tool_calls == []


def test_runs_tool_and_returns_result_with_matching_id():
    agent, client = make_agent([
        reply("tool_use", text("Checking."), tool_use("get_route_conditions", {"origin": "A", "destination": "B"}, "tu_1")),
        reply("end_turn", text("Yes, 10 km.")),
    ])
    result = agent.run("A to B?")
    assert result.answer == "Yes, 10 km."
    assert [c.name for c in result.tool_calls] == ["get_route_conditions"]

    second = client.messages.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    (tool_result,) = second[2]["content"]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu_1"
    assert tool_result["content"] == json.dumps({"total_km": 10.0})
    assert tool_result["is_error"] is False


def test_assistant_content_is_appended_unchanged():
    """Thinking blocks etc. must go back to the API exactly as received."""
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = reply("tool_use", thinking, tool_use("get_route_conditions", {"destination": "B"}, "tu_1"))
    agent, client = make_agent([first, reply("end_turn", text("ok"))])
    agent.run("q")
    assert client.messages.calls[1]["messages"][1]["content"] is first.content


def test_parallel_tool_calls_run_concurrently_and_return_in_one_message():
    tools = FakeTools(delay_s=0.2)
    agent, client = make_agent(
        [
            reply(
                "tool_use",
                tool_use("get_route_conditions", {"destination": "B"}, "tu_1"),
                tool_use("get_route_conditions", {"destination": "C"}, "tu_2"),
                tool_use("get_route_conditions", {"destination": "D"}, "tu_3"),
            ),
            reply("end_turn", text("done")),
        ],
        tools=tools,
    )
    started = time.perf_counter()
    result = agent.run("q")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, "three 0.2 s tools should overlap, not take 0.6 s"
    assert len(tools.threads) > 1
    results_msg = client.messages.calls[1]["messages"][2]
    assert results_msg["role"] == "user"
    assert [r["tool_use_id"] for r in results_msg["content"]] == ["tu_1", "tu_2", "tu_3"]
    assert [c.input["destination"] for c in result.tool_calls] == ["B", "C", "D"]


def test_tool_error_is_flagged_for_the_model():
    error = json.dumps({"error": "unknown_place", "suggestions": ["Akureyri"]})
    tools = FakeTools(results={"Akureyrii": error})
    agent, client = make_agent(
        [
            reply("tool_use", tool_use("get_route_conditions", {"destination": "Akureyrii"}, "tu_1")),
            reply("tool_use", tool_use("get_route_conditions", {"destination": "Akureyri"}, "tu_2")),
            reply("end_turn", text("ok")),
        ],
        tools=tools,
    )
    result = agent.run("q")
    assert [c.is_error for c in result.tool_calls] == [True, False]
    assert client.messages.calls[1]["messages"][2]["content"][0]["is_error"] is True
    assert result.ok


def test_iteration_cap_stops_a_runaway_loop():
    looping = [reply("tool_use", tool_use("get_route_conditions", {"destination": "B"}, f"tu_{i}")) for i in range(10)]
    agent, client = make_agent(looping, max_iterations=3)
    result = agent.run("q")
    assert result.stop == MAX_ITERATIONS and not result.ok
    assert len(client.messages.calls) == 3
    assert result.answer == ""


@pytest.mark.parametrize("stop_reason, expected", [("max_tokens", MAX_TOKENS), ("refusal", REFUSAL)])
def test_abnormal_stops_are_reported(stop_reason, expected):
    agent, _ = make_agent([reply(stop_reason, text("partial"))])
    result = agent.run("q")
    assert result.stop == expected and not result.ok
    assert result.answer == "partial"


def test_pause_turn_resends_and_continues():
    agent, client = make_agent([reply("pause_turn", text("...")), reply("end_turn", text("done"))])
    result = agent.run("q")
    assert result.answer == "done" and len(client.messages.calls) == 2


def test_usage_is_summed_and_priced():
    agent, _ = make_agent([
        reply("tool_use", tool_use("get_route_conditions", {"destination": "B"}, "tu_1"), inp=2000, out=100),
        reply("end_turn", text("ok"), inp=3000, out=300),
    ])
    usage = agent.run("q").usage
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (2, 5000, 400)
    # Haiku 5.5: $0.10 / $0.50 per million
    assert usage.cost_usd == pytest.approx((5000 * 0.10 + 400 * 0.50) / 1e6)


def test_request_shape():
    agent, client = make_agent([reply("end_turn", text("ok"))], model="claude-haiku-5-5", effort="low")
    agent.run("Can I drive?")
    params = client.messages.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["system"] == SYSTEM_PROMPT
    assert params["output_config"] == {"effort": "low"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in params, "Haiku has no server-side fallback"
    first_user = params["messages"][0]["content"]
    assert "2026-11-06 15:00 UTC" in first_user[0]["text"]
    assert first_user[1]["text"] == "Can I drive?"


def test_system_prompt_has_no_per_run_content():
    """A timestamp in the system prompt would break prompt caching on every run."""
    assert "2026" not in SYSTEM_PROMPT


def test_fallbacks_on_supported_models():
    agent, client = make_agent([reply("end_turn", text("ok"), model="claude-sonnet-5")], model="claude-sonnet-5-5")
    result = agent.run("q")
    params = client.messages.calls[0]
    assert params["fallbacks"] == "default"
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    assert result.models_used == ["claude-sonnet-5"], "records the model that actually served the turn"


def test_works_with_the_real_toolbox(network):
    """End to end with roadroute's Toolbox on the synthetic network."""
    from builders import A, C, FEED_TIME, make_snapshot
    from roadroute import Gazetteer, RoutePlanner, Toolbox

    gazetteer = Gazetteer({"Aville": (A.lat, A.lon), "Cville": (C.lat, C.lon)}, aliases={})
    snapshot = make_snapshot()
    toolbox = Toolbox(RoutePlanner(network, lambda: snapshot), gazetteer)
    agent, client = make_agent(
        [
            reply("tool_use", tool_use("get_route_conditions", {"origin": "Aville", "destination": "Cville"}, "tu_1")),
            reply("end_turn", text("ok")),
        ],
        tools=toolbox,
    )
    result = agent.run("q")
    payload = json.loads(result.tool_calls[0].result)
    assert not result.tool_calls[0].is_error
    assert payload["route_found"] is True
    assert payload["data_as_of"]["feed_generated"] == FEED_TIME.strftime("%Y-%m-%dT%H:%MZ")
