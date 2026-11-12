"""Turn a segment's condition into an edge-cost multiplier for routing.

Everything numeric here is a *heuristic starting point*, not Vegagerðin
policy. The status vocabulary below is what appears in the feed today
(GREIDFAERT, OTHEKKT, EKKI_I_THJONUSTU, OFAERT_ANNAD, OFAERT_VEDUR,
FAERT_FJALLABILUM, LOKAD, plus the secondary code ALLUR_AKSTUR_BANN).
Unrecognised codes are never silently treated as fine: they get a penalty and
are reported as "unknown" so a new code shows up in the tool output.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from .conditions import Condition

OK, CAUTION, BLOCKED, UNKNOWN = "ok", "caution", "blocked", "unknown"

_STATUS_MULTIPLIER = {
    "GREIDFAERT": 1.0,        # easily passable
    "OTHEKKT": 3.0,           # condition not known
    "EKKI_I_THJONUSTU": 8.0,  # not in service; avoid unless nothing else reaches
}
_PLOW_MULTIPLIER = {
    "EKKI_MOKAD": 2.0,   # not cleared
    "VOR_HAUST": 1.5,    # cleared in spring/autumn only
    "TILFALLANDI": 1.3,  # cleared ad hoc
    "FLUGMOKSTUR": 1.3,  # cleared for scheduled flights
}


@dataclass(frozen=True)
class CostModel:
    name: str = "standard"
    blocked_prefixes: tuple[str, ...] = ("OFAERT", "LOKAD", "ALLUR_AKSTUR_BANN")
    blocked_exact: frozenset[str] = frozenset({"FAERT_FJALLABILUM"})
    status_multiplier: Mapping[str, float] = field(default_factory=lambda: dict(_STATUS_MULTIPLIER))
    plow_multiplier: Mapping[str, float] = field(default_factory=lambda: dict(_PLOW_MULTIPLIER))
    unknown_status_multiplier: float = 2.0
    missing_condition_multiplier: float = 2.0
    side_road_multiplier: float = 1.1  # road numbers >= 100 (heuristic)

    # -- classification -------------------------------------------------
    def _is_blocked_code(self, code: str | None) -> bool:
        return bool(code) and (
            code in self.blocked_exact or code.startswith(self.blocked_prefixes)
        )

    def kind(self, condition: Condition | None) -> str:
        if condition is None:
            return UNKNOWN
        if self._is_blocked_code(condition.status) or self._is_blocked_code(condition.status_extra):
            return BLOCKED
        if condition.status == "GREIDFAERT":
            return OK
        if condition.status in self.status_multiplier:
            return CAUTION
        return UNKNOWN

    # -- routing weight -------------------------------------------------
    def multiplier(self, road_no: str | None, condition: Condition | None) -> float | None:
        """Cost multiplier for the segment, or ``None`` if it must not be used."""
        if condition is None:
            m = self.missing_condition_multiplier
        else:
            kind = self.kind(condition)
            if kind == BLOCKED:
                return None
            m = self.status_multiplier.get(condition.status, self.unknown_status_multiplier)
            m *= self.plow_multiplier.get(condition.plow_rule or "", 1.0)
        if road_no and road_no.isdigit() and int(road_no) >= 100:
            m *= self.side_road_multiplier
        return m


STANDARD = CostModel()
FOUR_BY_FOUR = replace(
    STANDARD,
    name="4x4",
    blocked_exact=frozenset(),
    status_multiplier={**_STATUS_MULTIPLIER, "FAERT_FJALLABILUM": 1.5},
)

PROFILES: dict[str, CostModel] = {"standard": STANDARD, "4x4": FOUR_BY_FOUR}
