"""Synthetic road network used by the unit tests.

Layout (metres east/north of a reference point near 64°N 21°W)::

              D (10000, 12000)
             /  \\             bypass: 1003 + 1004, ~31.2 km, road numbers >= 100
            /    \\
    A ----- B ----- C          main line: 1001 + 1002, ~20 km, the Ring Road (no. 1)
  (0,0)  (10000,0) (20000,0)

    I ---- J   (100000, 100000)   an isolated island segment (9009), not connected

The pieces of different segments meet 0.2-1.1 m apart on purpose, so the
snapping logic is exercised by every test that builds the network.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from pyproj import Transformer

from roadroute.conditions import Condition, ConditionsSnapshot
from roadroute.gazetteer import Place
from roadroute.geometry import Piece
from roadroute.network import to_isn93

ORIGIN = to_isn93(64.0, -21.0)
_INVERSE = Transformer.from_crs("EPSG:3057", "EPSG:4326", always_xy=True)

S1, S2, S3, S4, S9, S10 = "1001", "1002", "1003", "1004", "9009", "1010"
NAMES = {
    S1: "Hringvegur: A - B",
    S2: "Hringvegur: B - C",
    S3: "Bypass: A - D",
    S4: "Bypass: D - C",
    S9: "Island road",
    S10: "Municipal street",
}
ROAD_NOS = {S1: "1", S2: "1", S3: "550", S4: "550", S9: "99", S10: "0"}
FEED_TIME = datetime(2026, 11, 6, 13, 11, 51, tzinfo=timezone.utc)
REGISTERED = {
    S1: datetime(2026, 11, 6, 2, 55, tzinfo=timezone.utc),
    S2: datetime(2026, 11, 6, 5, 34, tzinfo=timezone.utc),
    S3: datetime(2026, 11, 6, 2, 55, tzinfo=timezone.utc),
    S4: datetime(2026, 11, 6, 2, 55, tzinfo=timezone.utc),
    S9: datetime(2026, 11, 6, 2, 55, tzinfo=timezone.utc),
    S10: datetime(2026, 11, 6, 2, 55, tzinfo=timezone.utc),
}


def xy(dx: float, dy: float) -> tuple[float, float]:
    return (round(ORIGIN[0] + dx, 1), round(ORIGIN[1] + dy, 1))


def place_at(name: str, dx: float, dy: float) -> Place:
    lon, lat = _INVERSE.transform(*xy(dx, dy))
    return Place(name, lat, lon)


def _piece(seg: str, *coords: tuple[float, float]) -> Piece:
    return Piece(seg, NAMES[seg], ROAD_NOS[seg], tuple(xy(*c) for c in coords))


def make_pieces() -> list[Piece]:
    return [
        # 1001: two pieces sharing an exact vertex
        _piece(S1, (0, 0), (5000, 0)),
        _piece(S1, (5000, 0), (10000, 0)),
        # 1002 starts 1.1 m from the end of 1001 (needs snapping)
        _piece(S2, (10001.0, 0.5), (15000, 0)),
        _piece(S2, (15000, 0), (20000, 0)),
        _piece(S3, (0, 0), (10000, 12000)),
        # 1004 starts 0.5 m from the end of 1003 and ends 0.2 m from 1002
        _piece(S4, (10000.4, 12000.3), (20000.2, 0)),
        _piece(S9, (100000, 100000), (105000, 100000)),
    ]


def make_stub_piece() -> Piece:
    """A 50 m municipal street hanging off A (not part of the default network)."""
    return _piece(S10, (0, -50), (0, 0))


def make_condition(segment_id: str, **overrides) -> Condition:
    base = Condition(
        segment_id=segment_id,
        name=NAMES[segment_id],
        short_name=NAMES[segment_id],
        status="GREIDFAERT",
        status_extra=None,
        description_en="Easily passable",
        description_is="Greiðfært",
        hindrance_en=None,
        notes_en=None,
        plow_rule="7X",
        registered_at=REGISTERED[segment_id],
        changed_at=None,
    )
    return replace(base, **overrides)


def make_snapshot(
    overrides: dict[str, dict] | None = None, drop: set[str] | None = None
) -> ConditionsSnapshot:
    overrides = overrides or {}
    by_segment = {
        sid: make_condition(sid, **overrides.get(sid, {}))
        for sid in NAMES
        if sid not in (drop or set())
    }
    return ConditionsSnapshot(generated_at=FEED_TIME, by_segment=by_segment)


A = place_at("A", 0, 0)
B = place_at("B", 10000, 0)
C = place_at("C", 20000, 0)
D = place_at("D", 10000, 12000)
