"""Live road conditions from Vegagerðin (``faerd2017_1``).

The feed is a flat list of ``Faerd2017`` records, one per condition segment.
Timestamps are *registration* times: per Vegagerðin's DATEX II notes a
condition is updated when it changes, so an old ``registered_at`` means
"unchanged since", not necessarily "stale". We report timestamps and leave
the interpretation to the caller.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from os import PathLike
from typing import Callable, Iterable, Mapping

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "http://gagnaveita.vegagerdin.is/api/faerd2017_1"
# Shown wherever results are presented (CC BY 4.0 attribution; see DATA_LICENSE.md).
ATTRIBUTION = (
    "Road data: Vegagerðin (faerd2017_1, faerdferlar2017_1), CC BY 4.0, "
    "https://creativecommons.org/licenses/by/4.0/. Not endorsed by Vegagerðin."
)
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


class ConditionsError(RuntimeError):
    """Conditions could not be fetched or parsed."""


@dataclass(frozen=True)
class Condition:
    segment_id: str
    name: str
    short_name: str | None
    status: str  # AstandYfirbord, e.g. GREIDFAERT
    status_extra: str | None  # AstandVidbotaruppl: a secondary status code
    description_en: str | None
    description_is: str | None
    hindrance_en: str | None  # Asthungi*: e.g. axle weight limits
    notes_en: str | None  # Frkv*: always empty in samples seen so far
    plow_rule: str | None
    registered_at: datetime | None  # DagsSkrad
    changed_at: datetime | None  # DagsButurBreyttist
    axle_limit_t: float | None = None  # from AsthungiAudkenni, e.g. "5TONN" -> 5.0


@dataclass(frozen=True)
class ConditionsSnapshot:
    generated_at: datetime | None  # DagsKeyrtUt: when the feed was produced
    by_segment: Mapping[str, Condition] = field(default_factory=dict)

    def get(self, segment_id: str) -> Condition | None:
        return self.by_segment.get(segment_id)

    def __len__(self) -> int:
        return len(self.by_segment)

    def registered_range(
        self, segment_ids: Iterable[str]
    ) -> tuple[datetime | None, datetime | None]:
        stamps = [
            c.registered_at
            for sid in segment_ids
            if (c := self.by_segment.get(sid)) is not None and c.registered_at
        ]
        return (min(stamps), max(stamps)) if stamps else (None, None)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_AXLE_CODE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*TONN\s*$", re.IGNORECASE)
_AXLE_TEXT = re.compile(r"axle weight limit\s+(\d+(?:[.,]\d+)?)\s*t", re.IGNORECASE)


def _parse_axle_limit(code: str | None, text_en: str | None) -> float | None:
    """Axle weight limit in tonnes. Prefers the code ("5TONN"); falls back to the English text."""
    for pattern, value in ((_AXLE_CODE, code), (_AXLE_TEXT, text_en)):
        match = value and pattern.search(value)
        if match:
            return float(match.group(1).replace(",", "."))
    return None


def _record_fields(record: ET.Element) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for child in record:
        tag = child.tag.rsplit("}", 1)[-1]
        nil = child.get(_XSI_NIL) == "true"
        out[tag] = None if nil or child.text is None else child.text.strip() or None
    return out


def parse_conditions(xml: bytes | str) -> ConditionsSnapshot:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ConditionsError(f"conditions XML is not well-formed: {exc}") from exc
    by_segment: dict[str, Condition] = {}
    generated: list[datetime] = []
    for record in root:
        f = _record_fields(record)
        segment_id, status = f.get("IdButur"), f.get("AstandYfirbord")
        if not segment_id or not status:
            continue  # malformed record; skip rather than fail the whole feed
        if (stamp := _parse_time(f.get("DagsKeyrtUt"))) is not None:
            generated.append(stamp)
        name = f.get("FulltNafnButs") or f.get("StuttNafnButs") or segment_id
        by_segment[segment_id] = Condition(
            segment_id=segment_id,
            name=name,
            short_name=f.get("StuttNafnButs"),
            status=status,
            status_extra=f.get("AstandVidbotaruppl"),
            description_en=f.get("AstandLysingEn"),
            description_is=f.get("AstandLysing"),
            hindrance_en=f.get("AsthungiLysingEn"),
            notes_en=f.get("FrkvLysingEn"),
            plow_rule=f.get("Snjomokstursregla"),
            registered_at=_parse_time(f.get("DagsSkrad")),
            changed_at=_parse_time(f.get("DagsButurBreyttist")),
            axle_limit_t=_parse_axle_limit(f.get("AsthungiAudkenni"), f.get("AsthungiLysingEn")),
        )
    if not by_segment:
        raise ConditionsError("conditions XML contained no usable records")
    return ConditionsSnapshot(
        generated_at=max(generated) if generated else None, by_segment=by_segment
    )


def load_conditions(path: str | PathLike[str]) -> ConditionsSnapshot:
    with open(path, "rb") as handle:
        return parse_conditions(handle.read())


def fetch_conditions(
    url: str = DEFAULT_URL,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> ConditionsSnapshot:
    """Download and parse the live feed."""
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        # The service negotiates content: without this header it answers in JSON.
        response = client.get(url, headers={"Accept": "application/xml"})
        response.raise_for_status()
        return parse_conditions(response.content)
    except httpx.HTTPError as exc:
        raise ConditionsError(f"could not fetch {url}: {exc}") from exc
    finally:
        if owns_client:
            client.close()


class CachedConditions:
    """Re-fetch at most once per ``ttl_s``; fall back to the last good copy.

    A failed refresh returns the previous snapshot rather than breaking the
    agent, and records the error in ``last_error`` so callers can surface it.
    """

    def __init__(
        self,
        fetch: Callable[[], ConditionsSnapshot],
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._ttl, self._clock = fetch, ttl_s, clock
        self._snapshot: ConditionsSnapshot | None = None
        self._fetched_at = 0.0
        self.last_error: str | None = None

    def __call__(self) -> ConditionsSnapshot:
        now = self._clock()
        if self._snapshot is not None and now - self._fetched_at < self._ttl:
            return self._snapshot
        try:
            self._snapshot = self._fetch()
            self._fetched_at = now
            self.last_error = None
        except ConditionsError as exc:
            self.last_error = str(exc)
            log.warning("conditions refresh failed: %s", exc)
            if self._snapshot is None:
                raise
        return self._snapshot
