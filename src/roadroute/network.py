"""Road graph built from the Vegagerðin geometry pieces.

Two properties of the export shape this module:

* Each stretch of road is stored several times at different levels of detail,
  so duplicates are collapsed first (see ``collapse_duplicate_lines``).
* Pieces of *different* segments usually meet a few decimetres to metres apart,
  so we snap: any piece endpoint within ``snap_m`` of a vertex of another piece
  is joined to it with a short edge. Snapping to any vertex (not only
  endpoints) also connects T-junctions. The 10 m default was chosen from a
  sweep on the real data: 3 m leaves 15 segments cut off, 10 m connects the
  worst offenders, and 25 m starts producing thousands of joins that risk
  linking roads which are only near each other.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from os import PathLike
from typing import Callable, Iterable

from pyproj import Transformer

from .geometry import Piece, collapse_duplicate_lines, iter_pieces

FORMAT_VERSION = 1
JOIN = -1  # segment index used for the short snap edges
_NEAREST_CELL_M = 500.0


class NetworkError(ValueError):
    """The saved network file is unusable."""


class OutsideNetworkError(ValueError):
    """A location is too far from any road in the network."""


@dataclass(frozen=True)
class SegmentInfo:
    id: str
    name: str
    road_no: str | None
    length_m: float


@lru_cache(maxsize=1)
def _to_isn93() -> Transformer:
    return Transformer.from_crs("EPSG:4326", "EPSG:3057", always_xy=True)


def to_isn93(lat: float, lon: float) -> tuple[float, float]:
    x, y = _to_isn93().transform(lon, lat)
    return x, y


def _round_points(points: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    # 0.1 m precision keeps the saved file small and makes save/load lossless.
    return tuple((round(x, 1), round(y, 1)) for x, y in points)


class RoadNetwork:
    def __init__(self, pieces: Iterable[Piece], snap_m: float = 10.0) -> None:
        self.snap_m = snap_m
        by_segment: dict[str, list[tuple[tuple[float, float], ...]]] = defaultdict(list)
        names: dict[str, str] = {}
        road_nos: dict[str, str | None] = {}
        for piece in pieces:
            by_segment[piece.segment_id].append(_round_points(piece.points))
            names.setdefault(piece.segment_id, piece.name)
            road_nos.setdefault(piece.segment_id, piece.road_no)
        if not by_segment:
            raise NetworkError("no geometry pieces supplied")
        self.dropped_duplicates = 0
        collapsed: dict[str, list[tuple[tuple[float, float], ...]]] = {}
        for sid, lines in by_segment.items():
            collapsed[sid] = collapse_duplicate_lines(lines)
            self.dropped_duplicates += len(lines) - len(collapsed[sid])
        self._pieces = collapsed
        self._names, self._road_nos = names, road_nos
        self.seg_ids: list[str] = list(self._pieces)
        self.seg_index: dict[str, int] = {sid: i for i, sid in enumerate(self.seg_ids)}
        self.join_count = 0
        self._build()

    # ---- construction --------------------------------------------------
    @classmethod
    def from_geometry_xml(cls, path: str | PathLike[str], snap_m: float = 10.0) -> "RoadNetwork":
        return cls(iter_pieces(path), snap_m=snap_m)

    def _build(self) -> None:
        node_of: dict[tuple[float, float], int] = {}
        nodes: list[tuple[float, float]] = []
        adj: list[list[tuple[int, float, int]]] = []
        lengths = [0.0] * len(self.seg_ids)

        def node(point: tuple[float, float]) -> int:
            idx = node_of.get(point)
            if idx is None:
                idx = node_of[point] = len(nodes)
                nodes.append(point)
                adj.append([])
            return idx

        endpoints: list[int] = []
        for seg_idx, seg_id in enumerate(self.seg_ids):
            for points in self._pieces[seg_id]:
                ids = [node(p) for p in points]
                for a, b in zip(ids, ids[1:]):
                    if a == b:
                        continue
                    w = math.dist(nodes[a], nodes[b])
                    adj[a].append((b, w, seg_idx))
                    adj[b].append((a, w, seg_idx))
                    lengths[seg_idx] += w
                endpoints += [ids[0], ids[-1]]

        # Snap piece endpoints to nearby vertices of other pieces.
        cell = self.snap_m
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (x, y) in enumerate(nodes):
            grid[(int(x // cell), int(y // cell))].append(i)
        joined: set[tuple[int, int]] = set()
        for e in set(endpoints):
            ex, ey = nodes[e]
            cx, cy = int(ex // cell), int(ey // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((cx + dx, cy + dy), ()):
                        if other == e:
                            continue
                        pair = (e, other) if e < other else (other, e)
                        if pair in joined:
                            continue
                        d = math.dist(nodes[e], nodes[other])
                        if d <= self.snap_m:
                            joined.add(pair)
                            adj[e].append((other, d, JOIN))
                            adj[other].append((e, d, JOIN))
        self.join_count = len(joined)

        self.nodes, self.adj = nodes, adj
        self.seg_info: list[SegmentInfo] = [
            SegmentInfo(sid, self._names[sid], self._road_nos[sid], lengths[i])
            for i, sid in enumerate(self.seg_ids)
        ]
        self._label_components()
        self._index_main_component()
        self._find_disconnected()

    def _label_components(self) -> None:
        label = [-1] * len(self.nodes)
        sizes: list[int] = []
        for start in range(len(self.nodes)):
            if label[start] != -1:
                continue
            comp, count, stack = len(sizes), 0, [start]
            label[start] = comp
            while stack:
                u = stack.pop()
                count += 1
                for v, _, _ in self.adj[u]:
                    if label[v] == -1:
                        label[v] = comp
                        stack.append(v)
            sizes.append(count)
        self.component = label
        self.component_sizes = sorted(sizes, reverse=True)
        self.main_component = sizes.index(max(sizes))

    def _find_disconnected(self) -> None:
        """Segments with no vertex in the main network cannot be routed to or through."""
        touching: set[int] = set()
        for node, edges in enumerate(self.adj):
            if self.component[node] == self.main_component:
                touching.update(seg for _, _, seg in edges if seg != JOIN)
        self.disconnected_segments: list[SegmentInfo] = [
            info for i, info in enumerate(self.seg_info) if i not in touching
        ]

    def _index_main_component(self) -> None:
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (x, y) in enumerate(self.nodes):
            if self.component[i] == self.main_component:
                grid[(int(x // _NEAREST_CELL_M), int(y // _NEAREST_CELL_M))].append(i)
        self._grid = grid

    # ---- queries -------------------------------------------------------
    @property
    def main_share(self) -> float:
        return self.component_sizes[0] / len(self.nodes)

    def nearest_node(
        self,
        x: float,
        y: float,
        max_m: float = 5000.0,
        accept: Callable[[int], bool] | None = None,
    ) -> tuple[int, float]:
        """Nearest vertex of the main connected network, with its distance.

        ``accept`` optionally filters candidate nodes (e.g. "lies on a usable road").
        """
        cx, cy = int(x // _NEAREST_CELL_M), int(y // _NEAREST_CELL_M)
        best: tuple[float, int] | None = None
        ring = 0
        while True:
            if best is not None and best[0] <= ring * _NEAREST_CELL_M:
                break
            if ring * _NEAREST_CELL_M > max_m:
                break
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    if max(abs(gx - cx), abs(gy - cy)) != ring:
                        continue
                    for i in self._grid.get((gx, gy), ()):
                        if accept is not None and not accept(i):
                            continue
                        d = math.dist(self.nodes[i], (x, y))
                        if best is None or d < best[0]:
                            best = (d, i)
            ring += 1
        if best is None or best[0] > max_m:
            raise OutsideNetworkError(
                f"no road within {max_m / 1000:.1f} km of the requested location"
            )
        return best[1], best[0]

    # ---- persistence ---------------------------------------------------
    def save(self, path: str | PathLike[str]) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "crs": "EPSG:3057",
            "snap_m": self.snap_m,
            "segments": [
                {
                    "id": sid,
                    "name": self._names[sid],
                    "road_no": self._road_nos[sid],
                    "pieces": [[list(p) for p in piece] for piece in self._pieces[sid]],
                }
                for sid in self.seg_ids
            ],
        }
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, path: str | PathLike[str]) -> "RoadNetwork":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise NetworkError(f"cannot read network file {path}: {exc}") from exc
        if payload.get("version") != FORMAT_VERSION or payload.get("crs") != "EPSG:3057":
            raise NetworkError(
                f"unsupported network file (version={payload.get('version')!r}, "
                f"crs={payload.get('crs')!r}); rebuild it with `roadroute build`"
            )
        pieces = (
            Piece(seg["id"], seg["name"], seg["road_no"], tuple(map(tuple, points)))
            for seg in payload["segments"]
            for points in seg["pieces"]
        )
        return cls(pieces, snap_m=payload["snap_m"])
