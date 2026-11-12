"""Plan a route and return a compact, model-friendly summary of conditions on it."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .conditions import Condition, ConditionsSnapshot
from .costs import BLOCKED, OK, PROFILES
from .gazetteer import Place
from .network import OutsideNetworkError, RoadNetwork, to_isn93
from .router import Route, shortest_route

NO_DATA = "NO_DATA"
# A start/end point is snapped to a road whose cost multiplier is below this, i.e. one
# that is passable and not flagged as unknown or out of service (those are >= 2-3x).
_USABLE_MULTIPLIER = 3.0
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

    def plan(self, origin: Place, destination: Place, vehicle: str = "standard") -> dict[str, Any]:
        model = PROFILES.get(vehicle)
        if model is None:
            raise ValueError(f"unknown vehicle profile {vehicle!r}; use one of {sorted(PROFILES)}")
        net, snapshot = self.network, self._conditions()
        multipliers = [model.multiplier(info.road_no, snapshot.get(info.id)) for info in net.seg_info]
        start, start_gap = self._snap(origin, multipliers)
        goal, goal_gap = self._snap(destination, multipliers)
        route = shortest_route(net, start, goal, multipliers)
        direct = self._direct_route(start, goal)

        result: dict[str, Any] = {
            "origin": {"place": origin.name, "snapped_m": round(start_gap)},
            "destination": {"place": destination.name, "snapped_m": round(goal_gap)},
            "vehicle": model.name,
            "route_found": route is not None,
        }
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
                blocked.append((leg.length_m, cond.name if cond else info.name))
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
