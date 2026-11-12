import json

import pytest

from builders import A, C, S1, S2, S3, S4, make_snapshot, place_at
from roadroute.network import OutsideNetworkError
from roadroute.service import RoutePlanner


def plan(network, snapshot=None, origin=A, destination=C, vehicle="standard", **kwargs):
    snapshot = snapshot or make_snapshot()
    return RoutePlanner(network, lambda: snapshot, **kwargs).plan(origin, destination, vehicle)


def test_clear_route_summary(network):
    result = plan(network)
    assert result["route_found"] is True
    assert result["total_km"] == pytest.approx(20.0, abs=0.1)
    assert result["segment_count"] == 2
    assert result["issues"] == []
    assert result["segment_columns"] == ["name", "km", "status"]
    assert [row[0] for row in result["segments"]] == ["Hringvegur: A - B", "Hringvegur: B - C"]
    assert all(row[2] == "GREIDFAERT" for row in result["segments"])
    assert "shorter_route_blocked" not in result
    assert result["data_as_of"] == {
        "feed_generated": "2026-11-06T13:11Z",
        "oldest_segment_registered": "2026-11-06T02:55Z",
        "newest_segment_registered": "2026-11-06T05:34Z",
    }
    assert any("not a forecast" in note for note in result["notes"])


def test_impassable_segment_triggers_a_detour_and_says_so(network):
    result = plan(network, make_snapshot({S2: {"status": "OFAERT_ANNAD"}}))
    assert [row[0] for row in result["segments"]] == ["Bypass: A - D", "Bypass: D - C"]
    assert result["total_km"] == pytest.approx(31.2, abs=0.1)
    blocked = result["shorter_route_blocked"]
    assert blocked["blocked_count"] == 1
    assert blocked["blocked_examples"] == ["Hringvegur: B - C"]
    assert blocked["extra_km_for_detour"] == pytest.approx(11.2, abs=0.2)
    assert blocked["direct_km"] == pytest.approx(20.0, abs=0.1)


def test_no_route_when_every_connection_is_blocked(network):
    snapshot = make_snapshot({S2: {"status": "LOKAD"}, S4: {"status": "OFAERT_VEDUR"}})
    result = plan(network, snapshot)
    assert result["route_found"] is False
    assert "No passable route" in result["reason"]
    assert result["shorter_route_blocked"]["blocked_examples"] == ["Hringvegur: B - C"]
    assert "segments" not in result and "total_km" not in result


def test_cautionary_segment_is_listed_in_issues_when_there_is_no_alternative(network):
    snapshot = make_snapshot({S1: {"status": "OTHEKKT", "description_en": "Not known"},
                              S3: {"status": "OFAERT_ANNAD"}})
    result = plan(network, snapshot)
    (issue,) = result["issues"]
    assert issue["segment"] == "Hringvegur: A - B"
    assert issue["status"] == "OTHEKKT" and issue["kind"] == "caution"
    assert issue["meaning"] == "Not known"
    assert issue["registered_at"] == "2026-11-06T02:55Z"
    assert "restriction" not in issue  # empty fields are omitted to save tokens


def test_weight_restriction_is_reported_even_on_a_passable_road(network):
    result = plan(network, make_snapshot({S2: {"hindrance_en": "Axle weight limit 7 tons"}}))
    (issue,) = result["issues"]
    assert issue["kind"] == "ok" and issue["restriction"] == "Axle weight limit 7 tons"


def test_unrecognised_status_codes_are_surfaced(network):
    result = plan(network, make_snapshot({S1: {"status": "BRAND_NEW_CODE"}}))
    (issue,) = result["issues"]
    assert issue["kind"] == "unknown" and issue["status"] == "BRAND_NEW_CODE"


def test_segment_without_a_condition_record_is_flagged(network):
    result = plan(network, make_snapshot(drop={S1}))
    assert result["segments"][0][2] == "NO_DATA"
    assert result["issues"][0]["status"] == "NO_DATA"


def test_vehicle_profile_changes_the_route(network):
    snapshot = make_snapshot({S2: {"status": "FAERT_FJALLABILUM"}})
    standard = plan(network, snapshot, vehicle="standard")
    four_by_four = plan(network, snapshot, vehicle="4x4")
    assert standard["segments"][0][0].startswith("Bypass")
    assert four_by_four["vehicle"] == "4x4"
    assert four_by_four["segments"][1][0] == "Hringvegur: B - C"
    assert four_by_four["issues"][0]["kind"] == "caution"


def test_unknown_vehicle_profile_is_rejected(network):
    with pytest.raises(ValueError, match="vehicle"):
        plan(network, vehicle="hovercraft")


def test_far_away_location_raises_with_the_place_name(network):
    with pytest.raises(OutsideNetworkError, match="Nowhere"):
        plan(network, destination=place_at("Nowhere", 300_000, 300_000))


def test_distance_to_the_road_is_reported_and_noted(network):
    off_road = place_at("Off-road farm", 10_000, 2_000)
    result = plan(network, destination=off_road)
    assert result["destination"]["snapped_m"] == pytest.approx(2000, abs=10)
    assert any("Off-road farm" in note and "km from the nearest" in note for note in result["notes"])


def test_failed_conditions_refresh_is_visible_to_the_model(network):
    snapshot = make_snapshot()

    class Flaky:
        last_error = "timeout"

        def __call__(self):
            return snapshot

    result = RoutePlanner(network, Flaky()).plan(A, C)
    assert any("refresh failed (timeout)" in note for note in result["notes"])


def test_result_is_compact_json(network):
    text = json.dumps(plan(network), ensure_ascii=False, separators=(",", ":"))
    assert len(text) < 1500  # a two-segment route should cost a few hundred tokens


# --- snapping prefers usable roads ---------------------------------------------------------------

from builders import S10, make_stub_piece  # noqa: E402
from roadroute.network import RoadNetwork  # noqa: E402
from builders import make_pieces  # noqa: E402


def stub_network():
    return RoadNetwork(make_pieces() + [make_stub_piece()], snap_m=3.0)


def test_start_point_is_not_snapped_onto_an_out_of_service_street():
    # 40 m below A, the nearest road is the municipal stub, which the feed marks out of service.
    start = place_at("Town centre", 0, -40)
    snapshot = make_snapshot({S10: {"status": "EKKI_I_THJONUSTU", "plow_rule": "UMSJON_SVEITARFELAG"}})
    result = RoutePlanner(stub_network(), lambda: snapshot).plan(start, C)
    assert [row[0] for row in result["segments"]] == ["Hringvegur: A - B", "Hringvegur: B - C"]
    assert result["issues"] == []  # no bogus "caution" about a street the traveller never needs
    assert result["origin"]["snapped_m"] == pytest.approx(40, abs=2)


def test_falls_back_to_the_nearest_road_when_nothing_usable_is_near():
    # Every segment closed: no usable node exists, so we still snap (and report no route)
    # instead of failing with a confusing "outside network" error.
    closed = {sid: {"status": "LOKAD"} for sid in (S1, S2, S3, S4, S10)}
    result = RoutePlanner(stub_network(), lambda: make_snapshot(closed)).plan(A, C)
    assert result["route_found"] is False
