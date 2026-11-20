"""Weather observations along a route, from the Icelandic Met Office (Veðurstofa Íslands).

Data: https://api.vedur.is/weather (CC BY 4.0). Two calls cover the whole country:

- ``/stations?active=true``: every station with coordinates (changes rarely; cached 24 h)
- ``/observations/aws/10min/latest``: the latest 10-minute reading from every automatic
  station in one ~160 KB response (cached 5 min)

Matching stations to the route happens locally: walk the route geometry, sample a point every
few kilometres, and take the nearest station within a few kilometres of each sample. That is
more precise than the API's 12 forecast regions, which are far larger than a mountain pass.

About 110 of the stations belong to Vegagerðin and sit on the roads themselves (on the passes,
in particular); they measure road surface temperature, which is what matters for ice.

These are OBSERVATIONS: what the weather is now, not a forecast.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .network import to_isn93

log = logging.getLogger(__name__)

API_URL = "https://api.vedur.is/weather"
ATTRIBUTION = (
    "Weather observations: Veðurstofa Íslands (Icelandic Met Office), api.vedur.is, CC BY 4.0. "
    "Not endorsed by Veðurstofa Íslands."
)

# Flag thresholds. These are heuristics chosen for this project, not official guidance.
GUST_HIGH_SIDED_MS = 15.0  # strong enough to matter for camper vans, trailers, lorries
GUST_ALL_VEHICLES_MS = 25.0  # dangerous for any vehicle
STALE_AFTER = timedelta(hours=2)


class WeatherError(RuntimeError):
    """Weather data could not be fetched or parsed."""


@dataclass(frozen=True)
class Station:
    id: int
    name: str
    lat: float
    lon: float
    elevation_m: float | None
    owner: str | None
    x: float  # ISN93 metres, for distance calculations
    y: float

    @property
    def road_station(self) -> bool:
        return "Vegagerðin" in (self.owner or "")


@dataclass(frozen=True)
class Observation:
    station_id: int
    time: datetime  # UTC
    air_temp_c: float | None
    road_temp_c: float | None
    wind_ms: float | None
    gust_ms: float | None
    wind_from: str | None  # compass point, e.g. "NE"
    humidity_pct: float | None


# -- parsing ------------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_stations(rows: Iterable[Mapping[str, Any]]) -> list[Station]:
    """Automatic stations ("sj") with coordinates. Other types don't report to the AWS feed."""
    stations = []
    for row in rows:
        if row.get("type") != "sj" or row.get("ending") is not None:
            continue
        lat, lon = _num(row.get("lat")), _num(row.get("lon"))
        if lat is None or lon is None or not isinstance(row.get("station"), int):
            continue
        x, y = to_isn93(lat, lon)
        stations.append(Station(
            id=row["station"], name=str(row.get("name") or row["station"]), lat=lat, lon=lon,
            elevation_m=_num(row.get("ele")), owner=row.get("owner"), x=x, y=y,
        ))
    return stations


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_observations(rows: Iterable[Mapping[str, Any]]) -> dict[int, Observation]:
    out: dict[int, Observation] = {}
    for row in rows:
        station, when = row.get("station"), _parse_time(row.get("time"))
        if not isinstance(station, int) or when is None:
            continue
        out[station] = Observation(
            station_id=station, time=when,
            air_temp_c=_num(row.get("t")), road_temp_c=_num(row.get("t0")),
            wind_ms=_num(row.get("f")), gust_ms=_num(row.get("fg")),
            wind_from=row.get("d_txt") if isinstance(row.get("d_txt"), str) else None,
            humidity_pct=_num(row.get("rh")),
        )
    return out


# -- fetching -----------------------------------------------------------------------------

def _get_json(path: str, params: Sequence[tuple[str, str]], client: httpx.Client | None, timeout: float) -> Any:
    owns = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.get(f"{API_URL}{path}", params=list(params))
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError(f"could not fetch {path}: {exc}") from exc
    finally:
        if owns:
            client.close()


def fetch_stations(client: httpx.Client | None = None, timeout: float = 15.0) -> list[Station]:
    rows = _get_json("/stations", [("active", "true")], client, timeout)
    if not isinstance(rows, list):
        raise WeatherError("unexpected /stations response")
    return parse_stations(rows)


def fetch_latest(client: httpx.Client | None = None, timeout: float = 15.0) -> dict[int, Observation]:
    rows = _get_json("/observations/aws/10min/latest", [("parameters", "basic")], client, timeout)
    if not isinstance(rows, list):
        raise WeatherError("unexpected observations response")
    return parse_observations(rows)


class Cached:
    """Re-fetch at most once per ``ttl_s``; on failure keep serving the last good value."""

    def __init__(self, fetch: Callable[[], Any], ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._clock = fetch, ttl_s, clock
        self._value: Any = None
        self._at = 0.0
        self.last_error: str | None = None

    def __call__(self) -> Any:
        now = self._clock()
        if self._value is not None and now - self._at < self._ttl:
            return self._value
        try:
            self._value, self._at, self.last_error = self._fetch(), now, None
        except WeatherError as exc:
            self.last_error = str(exc)
            log.warning("weather refresh failed: %s", exc)
            if self._value is None:
                raise
        return self._value


# -- along the route ----------------------------------------------------------------------

@dataclass(frozen=True)
class PathPoint:
    x: float
    y: float
    km: float  # distance from the start of the route
    segment: str  # name of the route segment at this point


def sample_path(points: Sequence[PathPoint], spacing_km: float) -> list[PathPoint]:
    """Points every ``spacing_km`` along the path (plus the first and last)."""
    if not points:
        return []
    samples, next_km = [points[0]], spacing_km
    for a, b in zip(points, points[1:]):
        while b.km >= next_km > a.km:
            f = (next_km - a.km) / (b.km - a.km) if b.km > a.km else 0.0
            samples.append(PathPoint(a.x + f * (b.x - a.x), a.y + f * (b.y - a.y), next_km,
                                     b.segment if f > 0.5 else a.segment))
            next_km += spacing_km
    if points[-1].km - samples[-1].km > 1e-6:
        samples.append(points[-1])
    return samples


def _gust_flag(obs: Observation) -> str | None:
    if obs.gust_ms is not None and obs.gust_ms >= GUST_ALL_VEHICLES_MS:
        return f"gusts {obs.gust_ms:.0f} m/s: dangerous for all vehicles"
    if obs.gust_ms is not None and obs.gust_ms >= GUST_HIGH_SIDED_MS:
        return f"gusts {obs.gust_ms:.0f} m/s: risky for high-sided vehicles and trailers"
    return None


def _ice_possible(obs: Observation) -> bool:
    if obs.road_temp_c is not None:
        return obs.road_temp_c <= 0
    return (obs.air_temp_c is not None and -3 <= obs.air_temp_c <= 2
            and (obs.humidity_pct or 0) >= 90)


def _has_reading(obs: Observation) -> bool:
    return any(v is not None for v in (obs.air_temp_c, obs.road_temp_c, obs.wind_ms, obs.gust_ms))


def _spread(rows: list, n: int) -> list:
    """n items spread evenly through rows (rows are in driving order)."""
    if n <= 0 or not rows:
        return []
    if n >= len(rows):
        return list(rows)
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if moment else None


def _round(value: float | None, digits: int = 0) -> float | int | None:
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


class RouteWeather:
    """Find stations along a route and summarise their latest observations."""

    def __init__(
        self,
        stations: Callable[[], list[Station]],
        observations: Callable[[], Mapping[int, Observation]],
        *,
        spacing_km: float = 1.0,
        max_station_km: float = 6.0,
        max_stations: int = 14,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._stations, self._observations = stations, observations
        self.spacing_km, self.max_station_km, self.max_stations = spacing_km, max_station_km, max_stations
        self.clock = clock

    def stations_along(self, path: Sequence[PathPoint]) -> list[tuple[Station, PathPoint, float]]:
        """(station, nearest sample point, distance from route in km), in driving order."""
        best: dict[int, tuple[Station, PathPoint, float]] = {}
        stations = self._stations()
        limit_m = self.max_station_km * 1000
        for point in sample_path(path, self.spacing_km):
            for station in stations:
                d = math.hypot(station.x - point.x, station.y - point.y)
                if d <= limit_m and (station.id not in best or d < best[station.id][2] * 1000):
                    best[station.id] = (station, point, d / 1000)
        return sorted(best.values(), key=lambda item: item[1].km)

    def summarise(self, path: Sequence[PathPoint]) -> dict[str, Any]:
        observations = self._observations()
        now = self.clock()
        rows, without_data, stale = [], 0, 0
        for station, point, dist_km in self.stations_along(path):
            obs = observations.get(station.id)
            if obs is None or not _has_reading(obs):
                without_data += 1
            elif now - obs.time > STALE_AFTER:
                stale += 1
            else:
                rows.append((station, point, obs))

        if len(rows) > self.max_stations:
            # Keep every gust warning, then road stations (on the road, measure the road surface)
            # spread along the route, then fill with the rest.
            gusty = [r for r in rows if _gust_flag(r[2])]
            road = [r for r in rows if r not in gusty and r[0].road_station]
            other = [r for r in rows if r not in gusty and not r[0].road_station]
            keep = gusty[: self.max_stations]
            keep += _spread(road, self.max_stations - len(keep))
            keep += _spread(other, self.max_stations - len(keep))
            rows = sorted(keep, key=lambda r: r[1].km)

        icy = [r for r in rows if _ice_possible(r[2])]
        with_road_temp = [r for r in rows if r[2].road_temp_c is not None]
        gusts = [r for r in rows if r[2].gust_ms is not None]
        summary: dict[str, Any] = {
            "stations_reporting": len(rows),
            "ice_possible_at": len(icy),
            "gust_warnings": [
                {"station": s.name, "km_from_start": round(p.km), "near_segment": p.segment, "warning": _gust_flag(o)}
                for s, p, o in rows if _gust_flag(o)
            ],
        }
        if icy:
            summary["ice_possible_between_km"] = [round(icy[0][1].km), round(icy[-1][1].km)]
        if with_road_temp:
            s, p, o = min(with_road_temp, key=lambda r: r[2].road_temp_c)
            summary["coldest_road_surface"] = {"station": s.name, "km_from_start": round(p.km), "road_surface_c": round(o.road_temp_c, 1)}
        if gusts:
            s, p, o = max(gusts, key=lambda r: r[2].gust_ms)
            summary["strongest_gust"] = {"station": s.name, "km_from_start": round(p.km), "gust_ms": round(o.gust_ms, 1)}

        times = [r[2].time for r in rows]
        notes = [
            "Observations from automatic weather stations, not a forecast.",
            "Times are UTC, which is Iceland's local time.",
            "ice_possible: road surface at or below 0 °C, or (no road sensor) air -3 to 2 °C with humidity 90% or more. "
            f"Gust warnings at {GUST_HIGH_SIDED_MS:.0f} and {GUST_ALL_VEHICLES_MS:.0f} m/s. "
            "These thresholds are this project's heuristics, not official guidance.",
        ]
        if stale:
            notes.append(f"Left out {stale} station(s) whose latest reading is over 2 hours old.")
        if without_data:
            notes.append(f"No usable reading from {without_data} nearby station(s).")
        refresh_error = getattr(self._observations, "last_error", None)
        if refresh_error:
            notes.append(f"Weather refresh failed ({refresh_error}); using the last good copy.")

        return {
            "summary": summary,
            "station_columns": ["station", "km_from_start", "near_segment", "elevation_m", "road_station",
                                "observed_at", "air_c", "road_surface_c", "wind_ms", "gust_ms", "wind_from"],
            "stations": [
                [s.name, round(p.km), p.segment, _round(s.elevation_m), s.road_station, _iso(o.time),
                 _round(o.air_temp_c, 1), _round(o.road_temp_c, 1), _round(o.wind_ms, 1),
                 _round(o.gust_ms, 1), o.wind_from]
                for s, p, o in rows
            ],
            "observed": {"oldest": _iso(min(times)) if times else None, "newest": _iso(max(times)) if times else None},
            "notes": notes,
        }
