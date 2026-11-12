import pytest

from builders import A, C, D, S1, S2, S3, S4, S9, ORIGIN
from roadroute.network import to_isn93
from roadroute.router import shortest_route


def nodes(network, *places):
    return [network.nearest_node(*to_isn93(p.lat, p.lon))[0] for p in places]


def multipliers(network, **by_id):
    """All 1.0, with overrides keyed by segment id (None blocks)."""
    out = [1.0] * len(network.seg_ids)
    for sid, value in by_id.items():
        out[network.seg_index[sid.removeprefix("s")]] = value
    return out


def ids(network, route):
    return [network.seg_ids[leg.segment_idx] for leg in route.legs]


def test_picks_the_main_line_and_merges_pieces_into_legs(network):
    a, c = nodes(network, A, C)
    route = shortest_route(network, a, c, multipliers(network))
    assert ids(network, route) == [S1, S2]  # four pieces, two legs
    assert route.length_m == pytest.approx(20_000, abs=5)
    assert route.legs[0].length_m == pytest.approx(10_000, abs=1)


def test_detours_over_the_bypass_when_a_segment_is_blocked(network):
    a, c = nodes(network, A, C)
    route = shortest_route(network, a, c, multipliers(network, s1002=None))
    assert ids(network, route) == [S3, S4]
    assert route.length_m == pytest.approx(31_241, abs=10)


def test_returns_none_when_every_path_is_blocked(network):
    a, c = nodes(network, A, C)
    assert shortest_route(network, a, c, multipliers(network, s1002=None, s1004=None)) is None


def test_penalties_can_outweigh_distance(network):
    a, c = nodes(network, A, C)
    route = shortest_route(network, a, c, multipliers(network, s1001=4.0))
    assert ids(network, route) == [S3, S4]  # 40 000 cost on the main line vs ~31 241 on the bypass
    assert route.cost == pytest.approx(route.length_m, rel=0.01)  # bypass costs its length


def test_cost_and_length_differ_when_multipliers_apply(network):
    a, c = nodes(network, A, C)
    route = shortest_route(network, a, c, multipliers(network, s1001=2.0))
    assert ids(network, route) == [S1, S2]
    assert route.cost == pytest.approx(route.length_m + 10_000, abs=10)


def test_same_start_and_goal_is_a_zero_length_route(network):
    (a,) = nodes(network, A)
    route = shortest_route(network, a, a, multipliers(network))
    assert route.legs == () and route.length_m == 0.0


def test_routes_can_start_or_end_mid_network(network):
    a, d = nodes(network, A, D)
    route = shortest_route(network, a, d, multipliers(network))
    assert ids(network, route) == [S3]
