"""Explained detours around penalised roads, and weight-aware routing.

Synthetic network (see builders.py): A-B-C main line (S1, S2, 20 km) and a bypass
A-D-C (S3, S4, ~31.2 km, side road so x1.1).
"""
from __future__ import annotations

import json

import pytest

from builders import A, C, S1, S2, S3, make_snapshot
from roadroute import Gazetteer, RoutePlanner, Toolbox
from roadroute.conditions import _parse_axle_limit, parse_conditions
from roadroute.costs import BLOCKED, STANDARD


def plan(network, snapshot, **kwargs):
    return RoutePlanner(network, lambda: snapshot).plan(A, C, **kwargs)


# -- parsing ---------------------------------------------------------------------------

@pytest.mark.parametrize("code, text, expected", [
    ("5TONN", "Axle weight limit 5 tons", 5.0),
    ("7TONN", None, 7.0),
    (None, "Axle weight limit 11,5 tons", 11.5),
    ("SOMETHING_ELSE", None, None),
    (None, None, None),
])
def test_parse_axle_limit(code, text, expected):
    assert _parse_axle_limit(code, text) == expected


def test_feed_axle_code_is_parsed():
    xml = (
        '<ArrayOfFaerd2017 xmlns="http://schemas.datacontract.org/2004/07/VgApi.Models"><Faerd2017>'
        "<IdButur>1</IdButur><AstandYfirbord>GREIDFAERT</AstandYfirbord>"
        "<AsthungiAudkenni>5TONN</AsthungiAudkenni><AsthungiLysingEn>Axle weight limit 5 tons</AsthungiLysingEn>"
        "</Faerd2017></ArrayOfFaerd2017>"
    )
    assert parse_conditions(xml).get("1").axle_limit_t == 5.0


# -- cost model ------------------------------------------------------------------------

def test_axle_load_over_the_limit_blocks_the_segment():
    cond = make_snapshot({S2: {"axle_limit_t": 7.0}}).get(S2)
    assert STANDARD.kind(cond) != BLOCKED, "no load given: not blocked"
    assert STANDARD.for_axle_load(7.0).kind(cond) != BLOCKED, "at the limit is allowed"
    assert STANDARD.for_axle_load(9.0).kind(cond) == BLOCKED
    assert STANDARD.for_axle_load(9.0).multiplier("1", cond) is None


# -- weight-aware routing --------------------------------------------------------------

def test_heavy_vehicle_is_routed_around_a_weight_limit(network):
    snapshot = make_snapshot({S2: {"axle_limit_t": 7.0, "hindrance_en": "Axle weight limit 7 tons"}})

    car = plan(network, snapshot)
    assert [row[0] for row in car["segments"]] == ["Hringvegur: A - B", "Hringvegur: B - C"]
    assert "max_axle_tonnes" not in car

    lorry = plan(network, snapshot, max_axle_tonnes=9)
    assert [row[0] for row in lorry["segments"]] == ["Bypass: A - D", "Bypass: D - C"]
    assert lorry["max_axle_tonnes"] == 9
    blocked = lorry["shorter_route_blocked"]
    assert blocked["blocked_examples"] == ["Hringvegur: B - C (axle limit 7 t)"]
    assert blocked["extra_km_for_detour"] == pytest.approx(11.2, abs=0.2)


def test_light_vehicle_keeps_the_weight_limited_road(network):
    snapshot = make_snapshot({S2: {"axle_limit_t": 7.0}})
    result = plan(network, snapshot, max_axle_tonnes=5)
    assert [row[0] for row in result["segments"]] == ["Hringvegur: A - B", "Hringvegur: B - C"]


def test_no_route_when_every_road_is_too_weak(network):
    snapshot = make_snapshot({S1: {"axle_limit_t": 5.0}, S3: {"axle_limit_t": 5.0}})
    result = plan(network, snapshot, max_axle_tonnes=9)
    assert result["route_found"] is False


@pytest.mark.parametrize("bad", [0, -3, 500])
def test_rejects_silly_axle_loads(network, bad):
    with pytest.raises(ValueError):
        plan(network, make_snapshot(), max_axle_tonnes=bad)


# -- explained detours around penalised (open) roads -----------------------------------

def test_detour_around_unknown_conditions_is_explained(network):
    # OTHEKKT x3 makes the 10 km main segment cost 30: main 40 vs bypass ~34.3, so the bypass wins.
    snapshot = make_snapshot({S2: {"status": "OTHEKKT", "description_en": "Conditions not known"}})
    result = plan(network, snapshot)
    assert [row[0] for row in result["segments"]] == ["Bypass: A - D", "Bypass: D - C"]
    assert "shorter_route_blocked" not in result, "nothing is closed"
    avoided = result["shorter_route_avoided"]
    assert avoided["direct_km"] == pytest.approx(20.0, abs=0.1)
    assert avoided["extra_km_for_detour"] == pytest.approx(11.2, abs=0.2)
    assert avoided["avoided_count"] == 1
    (example,) = avoided["avoided_examples"]
    assert example == {"segment": "Hringvegur: B - C", "status": "OTHEKKT", "meaning": "Conditions not known"}


def test_unploughed_road_is_reported_with_its_plow_rule(network):
    snapshot = make_snapshot({S2: {"status": "OTHEKKT", "plow_rule": "EKKI_MOKAD"}})
    (example,) = plan(network, snapshot)["shorter_route_avoided"]["avoided_examples"]
    assert example["plow_rule"] == "EKKI_MOKAD"


def test_no_avoided_report_when_the_direct_route_is_taken(network):
    result = plan(network, make_snapshot())
    assert "shorter_route_avoided" not in result


def test_blocked_and_avoided_are_reported_separately(network):
    snapshot = make_snapshot({S1: {"status": "OTHEKKT"}, S2: {"status": "LOKAD"}})
    result = plan(network, snapshot)
    assert result["shorter_route_blocked"]["blocked_examples"] == ["Hringvegur: B - C"]
    assert [e["segment"] for e in result["shorter_route_avoided"]["avoided_examples"]] == ["Hringvegur: A - B"]


# -- tool layer ------------------------------------------------------------------------

def _toolbox(network, snapshot):
    gazetteer = Gazetteer({"Aville": (A.lat, A.lon), "Cville": (C.lat, C.lon)}, aliases={})
    return Toolbox(RoutePlanner(network, lambda: snapshot), gazetteer)


def test_tool_passes_axle_load_through(network):
    snapshot = make_snapshot({S2: {"axle_limit_t": 7.0}})
    out = json.loads(_toolbox(network, snapshot).call(
        "get_route_conditions", {"origin": "Aville", "destination": "Cville", "max_axle_tonnes": 9}))
    assert out["max_axle_tonnes"] == 9.0
    assert "shorter_route_blocked" in out


@pytest.mark.parametrize("bad", ["9", True, [9]])
def test_tool_rejects_non_numeric_axle_load(network, bad):
    out = json.loads(_toolbox(network, make_snapshot()).call(
        "get_route_conditions", {"origin": "Aville", "destination": "Cville", "max_axle_tonnes": bad}))
    assert out["error"] == "invalid_arguments"
