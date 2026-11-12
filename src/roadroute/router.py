"""Dijkstra over the road graph with per-segment cost multipliers."""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Sequence

from .network import JOIN, RoadNetwork


@dataclass(frozen=True)
class Leg:
    segment_idx: int
    length_m: float


@dataclass(frozen=True)
class Route:
    legs: tuple[Leg, ...]
    length_m: float  # real distance, independent of the cost multipliers
    cost: float  # weighted cost the search minimised


def shortest_route(
    network: RoadNetwork,
    start: int,
    goal: int,
    multipliers: Sequence[float | None],
) -> Route | None:
    """Cheapest path from node ``start`` to ``goal``.

    ``multipliers[i]`` scales the length of segment ``i``; ``None`` forbids it.
    Returns ``None`` when the goal cannot be reached under those rules.
    """
    if start == goal:
        return Route((), 0.0, 0.0)
    adj = network.adj
    best = {start: 0.0}
    came: dict[int, tuple[int, int, float]] = {}  # node -> (previous node, segment, edge length)
    settled: set[int] = set()
    heap = [(0.0, start)]
    while heap:
        cost, u = heapq.heappop(heap)
        if u in settled:
            continue
        settled.add(u)
        if u == goal:
            break
        for v, length, seg in adj[u]:
            if v in settled:
                continue
            m = 1.0 if seg == JOIN else multipliers[seg]
            if m is None:
                continue
            new_cost = cost + length * m
            if new_cost < best.get(v, float("inf")):
                best[v] = new_cost
                came[v] = (u, seg, length)
                heapq.heappush(heap, (new_cost, v))
    if goal not in settled:
        return None

    edges: list[tuple[int, float]] = []
    node = goal
    while node != start:
        previous, seg, length = came[node]
        edges.append((seg, length))
        node = previous
    edges.reverse()

    legs: list[Leg] = []
    for seg, length in edges:
        if seg == JOIN:
            continue
        if legs and legs[-1].segment_idx == seg:
            legs[-1] = Leg(seg, legs[-1].length_m + length)
        else:
            legs.append(Leg(seg, length))
    return Route(tuple(legs), sum(length for _, length in edges), best[goal])
