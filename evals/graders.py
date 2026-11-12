"""Deterministic checks on an agent run. No LLM judge: each check is code you can read.

A check takes the AgentResult and returns (passed, detail). Prefer checks on behaviour
(which tools ran, what the answer is grounded in) over exact wording: the same correct
answer can be phrased many ways.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from roadroute_agent import AgentResult

ROUTE_TOOL = "get_route_conditions"


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[AgentResult], tuple[bool, str]]

    def __call__(self, result: AgentResult) -> tuple[bool, str]:
        return self.fn(result)


def _ok_route_payloads(result: AgentResult) -> list[dict[str, Any]]:
    payloads = []
    for call in result.tool_calls:
        if call.name == ROUTE_TOOL and not call.is_error:
            try:
                payloads.append(json.loads(call.result))
            except ValueError:
                pass
    return payloads


def finished() -> Check:
    return Check("finished", lambda r: (r.ok, f"stop={r.stop}"))


def route_tool_succeeded() -> Check:
    def fn(r: AgentResult) -> tuple[bool, str]:
        n = len(_ok_route_payloads(r))
        return n > 0, f"{n} successful route calls"

    return Check("route_tool_succeeded", fn)


def _clock_variants(iso: str) -> set[str]:
    """'2026-11-06T13:11Z' -> {'13:11', '1:11'} (24h, and 12h without the leading zero)."""
    match = re.search(r"T(\d{2}):(\d{2})", iso)
    if not match:
        return set()
    hh, mm = int(match.group(1)), match.group(2)
    return {f"{hh:02d}:{mm}", f"{hh}:{mm}", f"{(hh % 12) or 12}:{mm}", f"{hh:02d}.{mm}", f"{hh}.{mm}"}


def states_data_time() -> Check:
    """The answer mentions at least one of the data_as_of clock times."""

    def fn(r: AgentResult) -> tuple[bool, str]:
        times: set[str] = set()
        for payload in _ok_route_payloads(r):
            for value in (payload.get("data_as_of") or {}).values():
                if isinstance(value, str):
                    times |= _clock_variants(value)
        if not times:
            return False, "no data_as_of in tool results"
        found = sorted(t for t in times if re.search(rf"(?<![\d:.]){re.escape(t)}(?![\d])", r.answer))
        return bool(found), f"found {found}" if found else f"none of {sorted(times)} in answer"

    return Check("states_data_time", fn)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_KM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:km|kílómetr)", re.IGNORECASE)


def km_figures_grounded(tolerance_km: float = 1.5) -> Check:
    """Every 'N km' in the answer is within tolerance of a number in some tool result.

    Catches invented distances. Rounding (379.8 -> 'about 380 km') passes.
    """

    def fn(r: AgentResult) -> tuple[bool, str]:
        claimed = [float(m.replace(",", ".")) for m in _KM.findall(r.answer)]
        if not claimed:
            return True, "no km figures"
        known: list[float] = []
        for call in r.tool_calls:
            known += [float(n.replace(",", ".")) for n in _NUMBER.findall(call.result)]
        ungrounded = [c for c in claimed if not any(abs(c - k) <= tolerance_km for k in known)]
        return not ungrounded, f"ungrounded: {ungrounded}" if ungrounded else f"{len(claimed)} figures grounded"

    return Check("km_figures_grounded", fn)


def no_shifted_local_time() -> Check:
    """Iceland is UTC+0 all year. Catches '13:11 UTC (14:11 local time)', a mistake seen in runs."""

    def fn(r: AgentResult) -> tuple[bool, str]:
        wrong: set[str] = set()
        for payload in _ok_route_payloads(r):
            for value in (payload.get("data_as_of") or {}).values():
                match = isinstance(value, str) and re.search(r"T(\d{2}):(\d{2})", value)
                if match:
                    hh, mm = int(match.group(1)), match.group(2)
                    for shifted in ((hh + 1) % 24, (hh - 1) % 24):
                        wrong |= {f"{shifted:02d}:{mm}", f"{shifted}:{mm}"}
        found = sorted(t for t in wrong if re.search(rf"(?<![\d:.]){re.escape(t)}(?![\d])", r.answer))
        return not found, f"shifted times: {found}" if found else "no shifted times"

    return Check("no_shifted_local_time", fn)


def official_source() -> Check:
    def fn(r: AgentResult) -> tuple[bool, str]:
        hit = re.search(r"umferdin\.is|1777", r.answer)
        return bool(hit), "points to umferdin.is / 1777" if hit else "no official source"

    return Check("official_source", fn)


def mentions(name: str, pattern: str) -> Check:
    def fn(r: AgentResult) -> tuple[bool, str]:
        hit = re.search(pattern, r.answer, re.IGNORECASE)
        return bool(hit), f"matched {hit.group(0)!r}" if hit else f"no match for /{pattern}/"

    return Check(name, fn)


def not_mentions(name: str, pattern: str) -> Check:
    def fn(r: AgentResult) -> tuple[bool, str]:
        hit = re.search(pattern, r.answer, re.IGNORECASE)
        return not hit, f"found {hit.group(0)!r}" if hit else "absent"

    return Check(name, fn)


def min_route_calls(n: int) -> Check:
    """At least n successful route lookups (e.g. one per leg of a multi-leg trip)."""

    def fn(r: AgentResult) -> tuple[bool, str]:
        got = len(_ok_route_payloads(r))
        return got >= n, f"{got} successful route calls (need {n})"

    return Check(f"at_least_{n}_route_calls", fn)


def no_route_to(*places: str) -> Check:
    """No successful route lookup ended at these places.

    For a wrong suggestion: if the user asks for Hveravellir and the tool suggests Hveragerði,
    quietly answering for Hveragerði is worse than saying the place isn't covered.
    """

    def fn(r: AgentResult) -> tuple[bool, str]:
        hits = sorted({
            p["destination"]["place"]
            for p in _ok_route_payloads(r)
            if isinstance(p.get("destination"), dict) and p["destination"].get("place") in places
        })
        return not hits, f"routed to {hits}" if hits else "no route to a wrong suggestion"

    return Check("no_route_to_wrong_suggestion", fn)


def does_not_open_with_yes() -> Check:
    """For a 'no' case: the first words must not be an unqualified yes."""

    def fn(r: AgentResult) -> tuple[bool, str]:
        opening = re.sub(r"^[\s*_#>]+", "", r.answer)[:40]
        bad = re.match(r"(yes|já)\b", opening, re.IGNORECASE)
        return not bad, f"opens with {opening!r}"

    return Check("does_not_open_with_yes", fn)
