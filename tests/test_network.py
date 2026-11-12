import gzip
import json

import pytest

from builders import S1, S2, S3, S4, S9, ORIGIN, make_pieces, place_at
from roadroute.network import NetworkError, OutsideNetworkError, RoadNetwork, to_isn93


def test_small_gaps_between_pieces_are_snapped_shut(network):
    # 1001/1002, 1003/1004 and 1004/1002 meet 0.2-1.1 m apart; only the island stays separate.
    # 9 nodes on the joined road network, 2 on the island segment.
    assert network.component_sizes == [9, 2]
    assert network.join_count >= 3
    assert network.main_share == pytest.approx(9 / 11)


def test_snap_distance_is_a_real_tolerance():
    tight = RoadNetwork(make_pieces(), snap_m=0.1)
    assert len(tight.component_sizes) > 2  # the 1.1 m and 0.5 m gaps stay open


def test_segment_lengths(network):
    info = {i.id: i for i in network.seg_info}
    assert info[S1].length_m == pytest.approx(10_000, abs=0.5)
    assert info[S3].length_m == pytest.approx(15_620.5, abs=1)
    assert info[S1].road_no == "1" and info[S3].road_no == "550"


def test_nearest_node_only_considers_the_main_network(network):
    x, y = ORIGIN[0] + 100_000, ORIGIN[1] + 100_000  # standing on the island
    with pytest.raises(OutsideNetworkError):
        network.nearest_node(x, y, max_m=5_000)
    node, dist = network.nearest_node(x, y, max_m=200_000)
    assert network.component[node] == network.main_component
    assert dist > 100_000  # nowhere near the island segment itself


def test_nearest_node_reports_distance(network):
    x, y = ORIGIN[0] + 10_000, ORIGIN[1] + 2_000  # 2 km north of vertex B
    _, dist = network.nearest_node(x, y)
    assert dist == pytest.approx(2_000, abs=5)


def test_to_isn93_matches_a_known_point():
    # Reykjavík lies roughly at x≈356 km, y≈407 km in ISN93.
    x, y = to_isn93(64.1466, -21.9426)
    assert 350_000 < x < 362_000 and 400_000 < y < 412_000


def test_save_and_load_round_trip(network, tmp_path):
    path = tmp_path / "net.json.gz"
    network.save(path)
    loaded = RoadNetwork.load(path)
    assert loaded.seg_ids == network.seg_ids
    assert len(loaded.nodes) == len(network.nodes)
    assert loaded.join_count == network.join_count
    assert [round(i.length_m, 3) for i in loaded.seg_info] == [round(i.length_m, 3) for i in network.seg_info]


def test_load_rejects_wrong_version(tmp_path):
    path = tmp_path / "old.json.gz"
    with gzip.open(path, "wt") as handle:
        json.dump({"version": 0, "crs": "EPSG:3057", "segments": []}, handle)
    with pytest.raises(NetworkError, match="rebuild"):
        RoadNetwork.load(path)


def test_load_rejects_garbage(tmp_path):
    path = tmp_path / "junk.json.gz"
    path.write_bytes(b"not gzip")
    with pytest.raises(NetworkError):
        RoadNetwork.load(path)


def test_empty_input_is_an_error():
    with pytest.raises(NetworkError):
        RoadNetwork([])


# --- duplicates, disconnected segments, filtered snapping -------------------------------------

from builders import make_stub_piece  # noqa: E402
from roadroute.geometry import Piece, line_length  # noqa: E402


def test_level_of_detail_duplicates_are_collapsed_so_distances_are_not_understated():
    pieces = make_pieces()
    # A coarse straight copy of the same 1003 stretch is already straight; add a *detailed*
    # copy that wiggles (longer) and a coarse one, both with the same endpoints.
    wiggly = Piece(S3, "x", "550", tuple(
        [(ORIGIN[0] + 0.0, ORIGIN[1] + 0.0), (ORIGIN[0] + 2500.0, ORIGIN[1] + 3400.0),
         (ORIGIN[0] + 5000.0, ORIGIN[1] + 5900.0), (ORIGIN[0] + 7500.0, ORIGIN[1] + 9000.0),
         (ORIGIN[0] + 10000.0, ORIGIN[1] + 12000.0)]))
    net = RoadNetwork(pieces + [wiggly], snap_m=3.0)
    assert net.dropped_duplicates == 1
    info = {i.id: i for i in net.seg_info}
    straight, detailed = 15_620.5, line_length(wiggly.points)
    assert detailed > straight + 10  # the detailed copy really is longer
    # The detailed copy wins, so the segment is not understated by using the coarse one.
    assert info[S3].length_m == pytest.approx(detailed, abs=1)


def test_duplicates_survive_a_save_load_round_trip_without_changing_the_graph(tmp_path):
    net = RoadNetwork(make_pieces() + make_pieces(), snap_m=3.0)  # every piece stored twice
    assert net.dropped_duplicates == len(make_pieces())
    path = tmp_path / "n.json.gz"
    net.save(path)
    again = RoadNetwork.load(path)
    assert again.dropped_duplicates == 0  # the saved file is already collapsed
    assert len(again.nodes) == len(net.nodes) and again.seg_ids == net.seg_ids


def test_segments_cut_off_from_the_main_network_are_listed(network):
    assert [s.id for s in network.disconnected_segments] == [S9]


def test_nearest_node_can_be_filtered():
    stub = RoadNetwork(make_pieces() + [make_stub_piece()], snap_m=3.0)
    stub_idx = stub.seg_index["1010"]
    on_stub_only = lambda n: all(seg == stub_idx for _, _, seg in stub.adj[n] if seg >= 0)  # noqa: E731
    not_stub = lambda n: any(seg != stub_idx for _, _, seg in stub.adj[n] if seg >= 0)  # noqa: E731
    x, y = xy_of(0, -40)
    far_end, d_far = stub.nearest_node(x, y)  # unfiltered: the stub's free end, 10 m away
    assert d_far == pytest.approx(10, abs=0.5) and on_stub_only(far_end)
    node, dist = stub.nearest_node(x, y, accept=not_stub)  # filtered: back at A, 40 m away
    assert dist == pytest.approx(40, abs=0.5) and not_stub(node)


def xy_of(dx, dy):
    return ORIGIN[0] + dx, ORIGIN[1] + dy
