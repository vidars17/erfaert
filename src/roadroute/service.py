"""Plan a route and return a compact, model-friendly summary of conditions on it."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

from .conditions import Condition, ConditionsSnapshot
from .costs import BLOCKED, OK, PROFILES
from .gazetteer import Place
from .network import OutsideNetworkError, RoadNetwork, to_isn93
from .router import Route, shortest_route
from .weather import PathPoint

NO_DATA = "NO_DATA"
# A start/end point is snapped to a road whose cost multiplier is below this, i.e. one
# that is passable and not flagged as unknown or out of service (those are >= 2-3x).
_USABLE_MULTIPLIER = 3.0
# Detours around penalised (open) roads shorter than this aren't worth a sentence in the answer.
_MIN_REPORTED_DETOUR_KM = 1.0
_NOTES = [
    "All times are UTC, which is also Iceland's local time (no daylight saving): 13:11Z is 13:11 in Iceland.",
    "Current conditions only, not a forecast; they can change within hours.",
    "Lowest-cost path under condition heuristics, not turn-by-turn navigation.",
]


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


class RoutePlanner:
    def __init__(
        self,
        network: RoadNetwork,
        conditions: Callable[[], ConditionsSnapshot],
        *,
        max_snap_m: float = 5000.0,
    ) -> None:
        self.network = network
        self._conditions = conditions
        self.max_snap_m = max_snap_m
        self._direct: dict[tuple[int, int], Route | None] = {}

    def _snap(self, place: Place, multipliers: list[float | None]) -> tuple[int, float]:
        """Nearest node on a usable road; any road if none is usable nearby.

        Without the preference, a town-centre point can snap onto a municipal street
        that is "out of service" and the route would open with a bogus warning.
        """
        adj = self.network.adj

        def usable(node: int) -> bool:
            return any(
                seg >= 0 and (m := multipliers[seg]) is not None and m < _USABLE_MULTIPLIER
                for _, _, seg in adj[node]
            )

        x, y = to_isn93(place.lat, place.lon)
        try:
            return self.network.nearest_node(x, y, max_m=self.max_snap_m, accept=usable)
        except OutsideNetworkError:
            pass
        try:
            return self.network.nearest_node(x, y, max_m=self.max_snap_m)
        except OutsideNetworkError as exc:
            raise OutsideNetworkError(f"{place.name}: {exc}") from exc

    def _direct_route(self, a: int, b: int) -> Route | None:
        """Shortest path ignoring all conditions (cached: it never changes)."""
        key = (a, b)
        if key not in self._direct:
            if len(self._direct) > 256:
                self._direct.clear()
            self._direct[key] = shortest_route(self.network, a, b, [1.0] * len(self.network.seg_ids))
        return self._direct[key]

    def _solve(self, origin: Place, destination: Place, vehicle: str, max_axle_tonnes: float | None):
        model = PROFILES.get(vehicle)
        if model is None:
            raise ValueError(f"unknown vehicle profile {vehicle!r}; use one of {sorted(PROFILES)}")
        if max_axle_tonnes is not None and not (0 < max_axle_tonnes <= 100):
            raise ValueError("max_axle_tonnes must be a number of tonnes between 0 and 100")
        model = model.for_axle_load(max_axle_tonnes)
        snapshot = self._conditions()
        multipliers = [model.multiplier(info.road_no, snapshot.get(info.id)) for info in self.network.seg_info]
        start, start_gap = self._snap(origin, multipliers)
        goal, goal_gap = self._snap(destination, multipliers)
        route = shortest_route(self.network, start, goal, multipliers)
        return model, snapshot, start, start_gap, goal, goal_gap, route

    def route_path(
        self,
        origin: Place,
        destination: Place,
        vehicle: str = "standard",
        max_axle_tonnes: float | None = None,
    ) -> tuple[float, list[PathPoint]] | None:
        """The same route plan() picks, as (total_km, geometry points); None if no route.

        Used by the weather tool, so weather is reported along exactly the route whose road
        conditions the other tool reports.
        """
        *_, route = self._solve(origin, destination, vehicle, max_axle_tonnes)
        if route is None:
            return None
        net, snapshot = self.network, self._conditions()
        points: list[PathPoint] = []
        km, segment_name = 0.0, ""
        for i, node in enumerate(route.nodes):
            if i > 0:
                seg = route.edge_segments[i - 1]
                km += math.dist(net.nodes[route.nodes[i - 1]], net.nodes[node]) / 1000
                if seg >= 0:
                    info = net.seg_info[seg]
                    cond = snapshot.get(info.id)
                    segment_name = cond.name if cond else info.name
            x, y = net.nodes[node]
            points.append(PathPoint(x, y, km, segment_name))
        # The first point takes the name of the first real segment.
        first = next((p.segment for p in points if p.segment), "")
        points = [PathPoint(p.x, p.y, p.km, p.segment or first) for p in points]
        # Straight-line node distances under-count curvy roads; scale to the real length.
        if points and points[-1].km > 0:
            scale = route.length_m / 1000 / points[-1].km
            points = [PathPoint(p.x, p.y, p.km * scale, p.segment) for p in points]
        return round(route.length_m / 1000, 1), points

    def plan(
        self,
        origin: Place,
        destination: Place,
        vehicle: str = "standard",
        max_axle_tonnes: float | None = None,
    ) -> dict[str, Any]:
        net = self.network
        model, snapshot, start, start_gap, goal, goal_gap, route = self._solve(
            origin, destination, vehicle, max_axle_tonnes)
        direct = self._direct_route(start, goal)

        result: dict[str, Any] = {
            "origin": {"place": origin.name, "snapped_m": round(start_gap)},
            "destination": {"place": destination.name, "snapped_m": round(goal_gap)},
            "vehicle": model.name,
            "route_found": route is not None,
        }
        if max_axle_tonnes is not None:
            result["max_axle_tonnes"] = max_axle_tonnes
        notes = list(_NOTES)
        for place, gap in ((origin, start_gap), (destination, goal_gap)):
            if gap > 1000:
                notes.append(f"{place.name} is about {gap / 1000:.1f} km from the nearest mapped road.")
        refresh_error = getattr(self._conditions, "last_error", None)
        if refresh_error:
            notes.append(f"Conditions refresh failed ({refresh_error}); using the last good copy.")

        used = {leg.segment_idx for leg in route.legs} if route else set()
        blocked_direct = self._blocked_on_direct(direct, route, model, snapshot, used)
        if blocked_direct:
            result["shorter_route_blocked"] = blocked_direct
        avoided = self._avoided_on_direct(direct, route, model, snapshot, used)
        if avoided:
            result["shorter_route_avoided"] = avoided

        if route is None:
            result["reason"] = (
                "No passable route under current conditions: every connection uses a "
                "segment that is closed, impassable or not suitable for this vehicle."
            )
            result["data_as_of"] = {"feed_generated": _iso(snapshot.generated_at)}
            result["notes"] = notes
            return result

        rows: list[list[Any]] = []
        issues: list[dict[str, Any]] = []
        segment_ids: list[str] = []
        for leg in route.legs:
            info = net.seg_info[leg.segment_idx]
            cond = snapshot.get(info.id)
            segment_ids.append(info.id)
            name = cond.name if cond else info.name
            km = round(leg.length_m / 1000, 1)
            rows.append([name, km, cond.status if cond else NO_DATA])
            issue = self._issue(name, km, cond, model.kind(cond))
            if issue:
                issues.append(issue)

        oldest, newest = snapshot.registered_range(segment_ids)
        result.update(
            total_km=round(route.length_m / 1000, 1),
            segment_count=len(rows),
            issues=issues,
            segment_columns=["name", "km", "status"],
            segments=rows,
            data_as_of={
                "feed_generated": _iso(snapshot.generated_at),
                "oldest_segment_registered": _iso(oldest),
                "newest_segment_registered": _iso(newest),
            },
            notes=notes,
        )
        return result

    @staticmethod
    def _issue(name: str, km: float, cond: Condition | None, kind: str) -> dict[str, Any] | None:
        if cond is None:
            return {"segment": name, "km": km, "status": NO_DATA, "kind": "unknown",
                    "meaning": "no condition record for this segment"}
        extra_differs = cond.status_extra not in (None, cond.status)
        if kind == OK and not (cond.hindrance_en or cond.notes_en or extra_differs):
            return None
        return _clean({
            "segment": name,
            "km": km,
            "status": cond.status,
            "kind": kind,
            "meaning": cond.description_en,
            "extra_status": cond.status_extra if cond.status_extra != cond.status else None,
            "restriction": cond.hindrance_en,
            "notes": cond.notes_en,
            "plow_rule": cond.plow_rule,
            "registered_at": _iso(cond.registered_at),
        })

    def _blocked_on_direct(
        self,
        direct: Route | None,
        route: Route | None,
        model,
        snapshot: ConditionsSnapshot,
        used: set[int],
    ) -> dict[str, Any] | None:
        if direct is None:
            return None
        if route is not None and {leg.segment_idx for leg in direct.legs} == used:
            return None
        blocked = []
        for leg in direct.legs:
            info = self.network.seg_info[leg.segment_idx]
            cond = snapshot.get(info.id)
            if model.kind(cond) == BLOCKED:
                name = cond.name if cond else info.name
                if model.over_axle_limit(cond):
                    name += f" (axle limit {cond.axle_limit_t:g} t)"
                blocked.append((leg.length_m, name))
        if not blocked:
            return None
        blocked.sort(reverse=True)
        out: dict[str, Any] = {
            "direct_km": round(direct.length_m / 1000, 1),
            "blocked_count": len(blocked),
            "blocked_examples": [name for _, name in blocked[:6]],
        }
        if route is not None:
            out["extra_km_for_detour"] = round((route.length_m - direct.length_m) / 1000, 1)
        return out

    def _avoided_on_direct(
        self,
        direct: Route | None,
        route: Route | None,
        model,
        snapshot: ConditionsSnapshot,
        used: set[int],
    ) -> dict[str, Any] | None:
        """Explain a detour taken to avoid segments that are passable but penalised.

        Without this, a route around an "unknown condition" pass comes back longer than
        expected with no reason given. Only open (not blocked) segments the chosen route
        skips are listed; blocked ones are reported by _blocked_on_direct.
        """
        if direct is None or route is None:
            return None
        if {leg.segment_idx for leg in direct.legs} == used:
            return None
        avoided = []
        for leg in direct.legs:
            if leg.segment_idx in used:
                continue
            info = self.network.seg_info[leg.segment_idx]
            cond = snapshot.get(info.id)
            penalty = model.condition_penalty(cond)
            if penalty is None or penalty <= 1.0:
                continue
            avoided.append((penalty * leg.length_m, _clean({
                "segment": cond.name if cond else info.name,
                "status": cond.status if cond else NO_DATA,
                "meaning": cond.description_en if cond else "no condition record for this segment",
                "plow_rule": cond.plow_rule if cond and model.plow_multiplier.get(cond.plow_rule or "", 1.0) > 1 else None,
            })))
        extra_km = (route.length_m - direct.length_m) / 1000
        if not avoided or extra_km < _MIN_REPORTED_DETOUR_KM:
            return None  # differs only by road class, or by too little to matter
        avoided.sort(key=lambda item: item[0], reverse=True)
        return {
            "direct_km": round(direct.length_m / 1000, 1),
            "extra_km_for_detour": round(extra_km, 1),
            "why": "The shorter route uses segments that are open but have unknown or poor "
                   "conditions; this route avoids them.",
            "avoided_count": len(avoided),
            "avoided_examples": [entry for _, entry in avoided[:4]],
        }
