import io

import pytest

from roadroute.geometry import GeometryError, iter_pieces, parse_coordinates

HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?><wfs:FeatureCollection '
    'xmlns:wfs="http://www.opengis.net/wfs" xmlns:gis="http://gagnaveita.vegagerdin.is/geoserver" '
    'xmlns:gml="http://www.opengis.net/gml">'
)
FOOTER = "</wfs:FeatureCollection>"


def feature(idbutur="904020037", vegnr="509", srs="http://www.opengis.net/gml/srs/epsg.xml#3057",
            coords="352444.1301,428611.0052,0 352429.6478,428630.686,0"):
    # Mirrors a real feature from the export (trimmed to the fields we use).
    return (
        "<gml:featureMember><gis:faerdferlar2017_1 fid='x.1'>"
        f"<gis:STUTTNAFNLEIDAR>Hringvegur-Akranesvegur</gis:STUTTNAFNLEIDAR>"
        f"<gis:IDBUTUR>{idbutur}</gis:IDBUTUR><gis:VEGNR>{vegnr}</gis:VEGNR>"
        f"<gis:SHAPE><gml:LineString srsName='{srs}'>"
        f"<gml:coordinates decimal='.' cs=',' ts=' '>{coords}</gml:coordinates>"
        "</gml:LineString></gis:SHAPE></gis:faerdferlar2017_1></gml:featureMember>"
    )


def parse(xml: str):
    return list(iter_pieces(io.BytesIO(xml.encode("utf-8"))))


def test_parses_a_real_shaped_feature():
    (piece,) = parse(HEADER + feature() + FOOTER)
    assert piece.segment_id == "904020037"
    assert piece.road_no == "509"
    assert piece.name == "Hringvegur-Akranesvegur"
    assert piece.points == ((352444.1301, 428611.0052), (352429.6478, 428630.686))


def test_z_coordinate_is_dropped():
    assert parse_coordinates("1.5,2.5,0 3,4,7") == ((1.5, 2.5), (3.0, 4.0))


def test_several_features_keep_their_ids():
    xml = HEADER + feature(idbutur="1") + feature(idbutur="2") + feature(idbutur="1") + FOOTER
    assert [p.segment_id for p in parse(xml)] == ["1", "2", "1"]


def test_wrong_projection_is_rejected():
    bad = feature(srs="http://www.opengis.net/gml/srs/epsg.xml#4326")
    with pytest.raises(GeometryError, match="3057"):
        parse(HEADER + bad + FOOTER)


def test_malformed_coordinate_tuple_is_rejected():
    with pytest.raises(GeometryError):
        parse(HEADER + feature(coords="123456") + FOOTER)


def test_feature_without_idbutur_is_rejected():
    xml = HEADER + feature().replace("<gis:IDBUTUR>904020037</gis:IDBUTUR>", "") + FOOTER
    with pytest.raises(GeometryError, match="IDBUTUR"):
        parse(xml)


def test_single_point_lines_are_ignored():
    assert parse(HEADER + feature(coords="1,2,0") + FOOTER) == []


# --- level-of-detail duplicates -------------------------------------------------------------

from roadroute.geometry import collapse_duplicate_lines, line_length  # noqa: E402


def test_coarse_copies_of_the_same_stretch_are_dropped_keeping_the_most_detailed():
    coarse = ((0.0, 0.0), (1000.0, 0.0))
    medium = ((0.0, 0.0), (500.0, 20.0), (1000.0, 0.0))
    detailed = ((0.0, 0.0), (250.0, 15.0), (500.0, 20.0), (750.0, 15.0), (1000.0, 0.0))
    assert collapse_duplicate_lines([coarse, medium, detailed]) == [detailed]


def test_copy_stored_in_the_opposite_direction_is_still_a_duplicate():
    forward = ((0.0, 0.0), (400.0, 10.0), (1000.0, 0.0))
    backward = ((1000.0, 0.0), (0.0, 0.0))
    assert collapse_duplicate_lines([backward, forward]) == [forward]


def test_genuinely_different_paths_between_the_same_endpoints_are_both_kept():
    direct = ((0.0, 0.0), (1000.0, 0.0))
    loop_road = ((0.0, 0.0), (500.0, 800.0), (1000.0, 0.0))  # ~1.9x longer
    assert len(collapse_duplicate_lines([direct, loop_road])) == 2


def test_different_endpoints_are_never_merged_and_input_is_not_mutated():
    a = ((0.0, 0.0), (10.0, 0.0))
    b = ((10.0, 0.0), (20.0, 0.0))
    lines = [a, b]
    assert collapse_duplicate_lines(lines) == [a, b]
    assert lines == [a, b]


def test_line_length():
    assert line_length(((0.0, 0.0), (3.0, 4.0), (3.0, 10.0))) == 11.0
