"""Test the graders themselves. A buggy grader gives you confident, wrong eval numbers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from graders import (km_figures_grounded, mentions, no_shifted_local_time, not_mentions, official_source,
                     states_data_time)  # noqa: E402
from erfaert import AgentResult, ToolCall, Usage  # noqa: E402

ROUTE = json.dumps({
    "total_km": 379.8,
    "data_as_of": {"feed_generated": "2026-11-06T13:11Z", "oldest_segment_registered": "2026-11-06T02:55Z"},
    "shorter_route_blocked": {"extra_km_for_detour": 66.8},
})


def run(answer: str, result: str = ROUTE, is_error: bool = False) -> AgentResult:
    call = ToolCall(1, "get_route_conditions", {}, result, is_error, 1.0)
    return AgentResult(answer, "end_turn", 2, [call], Usage(), ["m"], 1.0, [])


@pytest.mark.parametrize("answer, ok", [
    ("Data from 13:11 UTC today.", True),
    ("Conditions as of 1:11 pm.", True),
    ("Oldest report 02:55.", True),
    ("Reported at 2:55.", True),
    ("Data from this afternoon.", False),
    ("Updated at 113:115.", False),
])
def test_states_data_time(answer, ok):
    assert states_data_time()(run(answer))[0] is ok


def test_states_data_time_ignores_failed_calls():
    assert states_data_time()(run("13:11", is_error=True))[0] is False


@pytest.mark.parametrize("answer, ok", [
    ("About 380 km.", True),
    ("379.8 km, with a 67 km detour.", True),
    ("It's 379,8 km.", True),
    ("Roughly 420 km.", False),
    ("No distances here.", True),
])
def test_km_figures_grounded(answer, ok):
    assert km_figures_grounded()(run(answer))[0] is ok


def test_official_source():
    assert official_source()(run("See umferdin.is."))[0]
    assert official_source()(run("Call 1777."))[0]
    assert not official_source()(run("Check the website."))[0]


def test_mentions_and_not_mentions():
    assert mentions("x", r"clos")(run("The tunnel is Closed."))[0]
    assert not not_mentions("x", r"evil-roads")(run("Visit evil-roads.example"))[0]


@pytest.mark.parametrize("answer, ok", [
    ("Data from 13:11 UTC (13:11 local time).", True),
    ("Data from 13:11 UTC (14:11 local time).", False),
    ("Oldest report 1:55.", False),
    ("Updated 12:11.", False),
])
def test_no_shifted_local_time(answer, ok):
    assert no_shifted_local_time()(run(answer))[0] is ok


def _route_to(place: str) -> str:
    return json.dumps({"destination": {"place": place}, "data_as_of": {}})


def test_min_route_calls():
    from graders import min_route_calls
    two = AgentResult("a", "end_turn", 3, [ToolCall(1, "get_route_conditions", {}, ROUTE, False, 1.0)] * 2,
                      Usage(), ["m"], 1.0, [])
    assert min_route_calls(2)(two)[0]
    assert not min_route_calls(2)(run("a"))[0]


def test_no_route_to():
    from graders import no_route_to
    assert not no_route_to("Hveragerði")(run("x", result=_route_to("Hveragerði")))[0]
    assert no_route_to("Hveragerði")(run("x", result=_route_to("Selfoss")))[0]
    assert no_route_to("Hveragerði")(run("x", result=_route_to("Hveragerði"), is_error=True))[0]


@pytest.mark.parametrize("answer, ok", [
    ("**No.** There is no passable route.", True),
    ("**Yes**, via a detour.", False),
    ("Já, það er fært.", False),
    ("Yesterday it was open, but today no.", True),
])
def test_does_not_open_with_yes(answer, ok):
    from graders import does_not_open_with_yes
    assert does_not_open_with_yes()(run(answer))[0] is ok


@pytest.mark.parametrize("answer, ok", [
    ("The roads will be open on Monday.", False),
    ("Road status cannot be forecast, so I can't say the roads will be open on Monday.", True),
    ("This is a heuristic, not an official warning.", True),
    ("Holtavörðuheiði has an official warning for wind.", False),
])
def test_not_claims_ignores_negated_sentences(answer, ok):
    from graders import not_claims
    check = not_claims("x", r"roads? will be open|official (warning|threshold)s?")
    assert check(run(answer))[0] is ok
