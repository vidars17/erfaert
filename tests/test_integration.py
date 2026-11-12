"""Checks against the real Vegagerðin geometry export.

Skipped unless the geometry file is available, because it is 13 MB. Point
``ROADROUTE_GEOMETRY_XML`` at it, or place it at ``data/faerdferlar2017_1.xml``.
The conditions are the committed snapshot from 2026-11-06, so results are stable.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import CONDITIONS_SNAPSHOT
from roadroute.conditions import load_conditions
from roadroute.gazetteer import Gazetteer
from roadroute.network import RoadNetwork, to_isn93
from roadroute.service import RoutePlanner

GEOMETRY = Path(os.environ.get("ROADROUTE_GEOMETRY_XML", "data/faerdferlar2017_1.xml"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not GEOMETRY.exists(), reason=f"real geometry file not found at {GEOMETRY}"),
]

gazetteer = Gazetteer()


@pytest.fixture(scope="module")
def real_network() -> RoadNetwork:
    return RoadNetwork.from_geometry_xml(GEOMETRY)


@pytest.fixture(scope="module")
def snapshot():
    return load_conditions(CONDITIONS_SNAPSHOT)


@pytest.fixture(scope="module")
def planner(real_network, snapshot):
    return RoutePlanner(real_network, lambda: snapshot)


def route(planner, a, b, **kw):
    return planner.plan(gazetteer.resolve(a), gazetteer.resolve(b), **kw)


def test_every_condition_segment_has_geometry_and_vice_versa(real_network, snapshot):
    assert set(real_network.seg_ids) == set(snapshot.by_segment)
    assert len(real_network.seg_ids) == 708


def test_almost_the_whole_network_is_one_connected_component(real_network):
    assert real_network.main_share >= 0.99
    # A handful of segments have gaps in Vegagerðin's own data (e.g. Oddsskarðsvegur);
    # this guards against the number silently growing, e.g. after a snapping change.
    assert len(real_network.disconnected_segments) <= 10


def test_level_of_detail_duplicates_were_collapsed(real_network):
    # The export stores each stretch ~4 times; summed raw length was ~39,800 km. Collapsed it is
    # ~10,200 km, which is the right order for Iceland's Vegagerðin road network.
    total_km = sum(info.length_m for info in real_network.seg_info) / 1000
    assert 8_000 < total_km < 13_000
    assert real_network.dropped_duplicates > 4_000


def test_every_gazetteer_place_is_near_a_road(real_network):
    far = {}
    for name in gazetteer.names:
        place = gazetteer.resolve(name)
        _, dist = real_network.nearest_node(*to_isn93(place.lat, place.lon), max_m=20_000)
        if dist > 3_000:
            far[name] = round(dist)
    assert not far, f"gazetteer entries more than 3 km from a mapped road: {far}"


def test_reykjavik_to_akureyri_follows_the_ring_road(planner):
    result = route(planner, "Reykjavík", "Akureyri")
    names = [row[0] for row in result["segments"]]
    assert 350 < result["total_km"] < 420
    assert "Hvalfjarðargöng" in names and "Holtavörðuheiði" in names and "Öxnadalsheiði: Fremri-Kot - Þverá í Öxnadal" in names
    assert names.index("Hvalfjarðargöng") < names.index("Holtavörðuheiði")
    assert not any("Sprengisandsleið" in n for n in names)
    assert all(row[2] == "GREIDFAERT" for row in result["segments"])  # on 2026-11-06
    # Starting in central Reykjavík must not snap onto an out-of-service municipal street.
    assert result["segments"][0][0] != "Lækjargata - Njarðargata"
    assert not any(issue["kind"] != "ok" for issue in result["issues"])


def test_ring_road_gap_at_blonduos_is_bridged_by_svinvetningabraut(planner):
    # In this data layer the Ring Road (road 1) has a ~24 km gap between Blönduós and
    # Vatnsskarð that Svínvetningabraut (road 731) fills, including a 5-ton axle limit.
    result = route(planner, "Reykjavík", "Akureyri")
    assert any("Svínvetningabraut" in row[0] for row in result["segments"])
    restrictions = [i["restriction"] for i in result["issues"] if "restriction" in i]
    assert restrictions == ["Axle weight limit 5 tons"]


def test_standard_cars_are_not_sent_across_the_highlands(planner):
    result = route(planner, "Reykjavík", "Egilsstaðir")
    names = [row[0] for row in result["segments"]]
    assert not any("Sprengisandsleið" in n or n.startswith("Austurleið") for n in names)
    assert result["total_km"] > 600  # the direct highland line was ~533 km
    blocked = result["shorter_route_blocked"]
    assert blocked["blocked_count"] >= 10 and blocked["extra_km_for_detour"] > 50


def test_closing_a_mountain_pass_changes_the_route(planner, real_network, snapshot):
    baseline = route(planner, "Reykjavík", "Akureyri")
    pass_id = next(c.segment_id for c in snapshot.by_segment.values() if c.name == "Holtavörðuheiði")
    closed = {**snapshot.by_segment, pass_id: replace(snapshot.by_segment[pass_id], status="LOKAD")}
    closed_planner = RoutePlanner(real_network, lambda: replace(snapshot, by_segment=closed))
    result = route(closed_planner, "Reykjavík", "Akureyri")
    if result["route_found"]:
        assert "Holtavörðuheiði" not in [row[0] for row in result["segments"]]
        assert result["total_km"] > baseline["total_km"]
    else:
        assert result["shorter_route_blocked"]["blocked_examples"][0] == "Holtavörðuheiði"
