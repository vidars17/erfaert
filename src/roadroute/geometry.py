"""Parse the Vegagerðin WFS export of layer ``gis:faerdferlar2017_1``.

The export holds one GML ``LineString`` per *piece* of road, in the Icelandic
ISN93 / Lambert 1993 projection (EPSG:3057, metres). Roughly eight pieces make
up one condition segment; they share the segment's ``IDBUTUR`` key, which is
also the key (``IdButur``) of the live conditions feed.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from os import PathLike
from typing import BinaryIO, Iterator, Sequence

NS_GIS = "http://gagnaveita.vegagerdin.is/geoserver"
NS_GML = "http://www.opengis.net/gml"
FEATURE_TAG = f"{{{NS_GIS}}}faerdferlar2017_1"
EXPECTED_EPSG = "3057"


class GeometryError(ValueError):
    """The geometry file does not look like the export we expect."""


@dataclass(frozen=True)
class Piece:
    segment_id: str
    name: str
    road_no: str | None
    points: tuple[tuple[float, float], ...]  # ISN93 (x, y) in metres


def _field(feature: ET.Element, tag: str) -> str | None:
    child = feature.find(f"{{{NS_GIS}}}{tag}")
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def parse_coordinates(text: str) -> tuple[tuple[float, float], ...]:
    """Parse GML ``x,y,z x,y,z ...``; the z value (always 0 here) is dropped."""
    points = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            raise GeometryError(f"bad coordinate tuple: {token!r}")
        points.append((float(parts[0]), float(parts[1])))
    return tuple(points)


def iter_pieces(source: str | PathLike[str] | BinaryIO) -> Iterator[Piece]:
    """Stream pieces out of the export without loading the whole file."""
    if isinstance(source, PathLike):
        source = str(source)
    for _, element in ET.iterparse(source, events=("end",)):
        if element.tag != FEATURE_TAG:
            continue
        line = element.find(f".//{{{NS_GML}}}LineString")
        coords = None if line is None else line.find(f"{{{NS_GML}}}coordinates")
        if line is None or coords is None or not coords.text:
            element.clear()  # not a line feature; nothing to route on
            continue
        srs = line.get("srsName", "")
        if EXPECTED_EPSG not in srs:
            raise GeometryError(f"expected EPSG:{EXPECTED_EPSG} geometry, got srsName={srs!r}")
        segment_id = _field(element, "IDBUTUR")
        if segment_id is None:
            raise GeometryError("feature without IDBUTUR")
        points = parse_coordinates(coords.text)
        if len(points) >= 2:
            yield Piece(
                segment_id=segment_id,
                name=_field(element, "STUTTNAFNLEIDAR") or segment_id,
                road_no=_field(element, "VEGNR"),
                points=points,
            )
        element.clear()


Line = tuple[tuple[float, float], ...]


def line_length(points: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def collapse_duplicate_lines(lines: Sequence[Line], tolerance: float = 0.15) -> list[Line]:
    """Drop repeated copies of the same stretch of road.

    The export stores each stretch several times at different levels of detail
    (the same endpoints with, say, 4, 13, 46 and 125 vertices). Left in, the
    router would always pick the coarsest and shortest copy and under-report
    distances. Within one segment, a line is a duplicate when another line
    with *more* vertices has the same endpoints and a length within
    ``tolerance``. Lines that share endpoints but follow genuinely different
    paths (length differs by more than ``tolerance``) are both kept.
    """
    kept: list[Line] = []
    seen: dict[tuple, list[float]] = {}
    for line in sorted(lines, key=lambda ln: -len(ln)):  # stable: ties keep input order
        a, b = line[0], line[-1]
        key = (a, b) if a <= b else (b, a)
        length = line_length(line)
        if any(abs(length - other) <= tolerance * max(length, other) for other in seen.get(key, [])):
            continue
        seen.setdefault(key, []).append(length)
        kept.append(line)
    return kept
