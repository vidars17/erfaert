"""Weather forecasts along a route, for a planned departure time.

Data: the Icelandic Met Office's (Veðurstofa Íslands) station forecast service at
https://xmlweather.vedur.is (``type=forec``). It gives point forecasts per weather station:
hourly for about two days, then every 3 to 6 hours, up to about 10 days ahead. It uses the same
station ids as the observation API, so the stations matched to a route by ``weather.py`` work here
unchanged.

The idea: given a departure time, estimate when the driver reaches each station along the route
(constant average speed, no stops) and report that station's forecast for that hour. A driver
leaving Reykjavík at 08:00 gets Holtavörðuheiði's forecast for about 10:00, not for 08:00.

Limits worth knowing:
- Road STATUS cannot be forecast: Vegagerðin publishes current conditions only.
- The forecast has mean wind but no gusts, and no road surface temperature, so ice and wind
  flags are rougher than the observation tool's.
- Forecasts get less reliable the further ahead they look.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .weather import PathPoint, Station, pick_stations, stations_along

log = logging.getLogger(__name__)

XML_URL = "https://xmlweather.vedur.is/"
ATTRIBUTION = (
    "Weather forecasts: Veðurstofa Íslands (Icelandic Met Office), xmlweather.vedur.is. "
    "Not endorsed by Veðurstofa Íslands."
)

# Heuristics chosen for this project, not official guidance. The forecast gives MEAN wind; gusts
# in Iceland are often 1.5-2x the mean, so these sit lower than the observation tool's gust levels.
WIND_HIGH_SIDED_MS = 13.0
WIND_ALL_VEHICLES_MS = 20.0
DEFAULT_SPEED_KMH = 70.0  # average including slower stretches; no stops
_BATCH = 20  # station ids per request


class ForecastError(RuntimeError):
    """Forecasts could not be fetched or parsed."""


@dataclass(frozen=True)
class ForecastStep:
    time: datetime  # UTC
    temp_c: float | None
    wind_ms: float | None  # mean wind
    wind_from: str | None
    weather: str | None  # e.g. "Light snow"
    precip_mm: float | None  # since the previous step
    dew_point_c: float | None


@dataclass(frozen=True)
class StationForecast:
    station_id: int
    issued_at: datetime | None
    steps: tuple[ForecastStep, ...]

    def at(self, moment: datetime) -> ForecastStep | None:
        """The step nearest to ``moment`` (None if there are no steps)."""
        return min(self.steps, key=lambda s: abs(s.time - moment), default=None)


# -- parsing and fetching -----------------------------------------------------------------------

def _num(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_forecasts(xml: bytes | str) -> dict[int, StationForecast]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ForecastError(f"forecast XML is not well-formed: {exc}") from exc
    out: dict[int, StationForecast] = {}
    for station in root.findall("station"):
        try:
            sid = int(station.get("id", ""))
        except ValueError:
            continue
        if station.get("valid") != "1" or (station.findtext("err") or "").strip():
            continue
        steps = []
        for f in station.findall("forecast"):
            when = _time(f.findtext("ftime"))
            if when is None:
                continue
            steps.append(ForecastStep(
                time=when, temp_c=_num(f.findtext("T")), wind_ms=_num(f.findtext("F")),
                wind_from=(f.findtext("D") or None), weather=(f.findtext("W") or None),
                precip_mm=_num(f.findtext("R")), dew_point_c=_num(f.findtext("TD")),
            ))
        if steps:
            out[sid] = StationForecast(sid, _time(station.findtext("atime")), tuple(sorted(steps, key=lambda s: s.time)))
    return out


def fetch_forecasts(
    station_ids: Iterable[int], client: httpx.Client | None = None, timeout: float = 20.0,
) -> dict[int, StationForecast]:
    ids = sorted(set(station_ids))
    if not ids:
        return {}
    owns = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    out: dict[int, StationForecast] = {}
    try:
        for i in range(0, len(ids), _BATCH):
            params = {"op_w": "xml", "type": "forec", "lang": "en", "view": "xml", "time": "1h",
                      "params": "T;F;D;W;R;TD", "ids": ";".join(str(s) for s in ids[i:i + _BATCH])}
            response = client.get(XML_URL, params=params)
            response.raise_for_status()
            out.update(parse_forecasts(response.content))
    except httpx.HTTPError as exc:
        raise ForecastError(f"could not fetch forecasts: {exc}") from exc
    finally:
        if owns:
            client.close()
    return out


class CachedForecasts:
    """Per-station cache: only stations not fetched in the last ``ttl_s`` are requested again.

    Forecasts are issued every few hours; 30 minutes keeps repeated questions cheap. On failure,
    stations that were cached before are still served.
    """

    def __init__(self, fetch: Callable[[list[int]], dict[int, StationForecast]] = fetch_forecasts,
                 ttl_s: float = 1800.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch, self._ttl, self._clock = fetch, ttl_s, clock
        self._cache: dict[int, tuple[float, StationForecast | None]] = {}
        self.last_error: str | None = None

    def __call__(self, station_ids: Sequence[int]) -> dict[int, StationForecast]:
        now = self._clock()
        missing = [s for s in station_ids if s not in self._cache or now - self._cache[s][0] >= self._ttl]
        if missing:
            try:
                fetched = self._fetch(missing)
                self.last_error = None
                for sid in missing:
                    self._cache[sid] = (now, fetched.get(sid))  # remember "no forecast" too
            except ForecastError as exc:
                self.last_error = str(exc)
                log.warning("forecast refresh failed: %s", exc)
                if not any(s in self._cache for s in station_ids):
                    raise
        return {s: f for s in station_ids if s in self._cache and (f := self._cache[s][1]) is not None}


# -- along the route --------------------------------------------------------------------------

def _wind_warning(step: ForecastStep) -> str | None:
    if step.wind_ms is not None and step.wind_ms >= WIND_ALL_VEHICLES_MS:
        return f"mean wind {step.wind_ms:.0f} m/s: dangerous for all vehicles"
    if step.wind_ms is not None and step.wind_ms >= WIND_HIGH_SIDED_MS:
        return f"mean wind {step.wind_ms:.0f} m/s, gusts likely stronger: risky for high-sided vehicles and trailers"
    return None


def _snow_or_sleet(step: ForecastStep) -> bool:
    text = (step.weather or "").lower()
    if "snow" in text or "sleet" in text:
        return True
    return (step.precip_mm or 0) > 0 and step.temp_c is not None and step.temp_c <= 1


def _ice_possible(step: ForecastStep) -> bool:
    return step.temp_c is not None and -3 <= step.temp_c <= 2


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if moment else None


class RouteForecast:
    def __init__(
        self,
        stations: Callable[[], list[Station]],
        forecasts: Callable[[Sequence[int]], Mapping[int, StationForecast]],
        *,
        speed_kmh: float = DEFAULT_SPEED_KMH,
        max_station_km: float = 6.0,
        max_stations: int = 12,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._stations, self._forecasts = stations, forecasts
        self.speed_kmh, self.max_station_km, self.max_stations = speed_kmh, max_station_km, max_stations
        self.clock = clock

    def summarise(self, path: Sequence[PathPoint], departure: datetime) -> dict[str, Any]:
        now = self.clock()
        if departure < now - timedelta(hours=1):
            raise ValueError(f"departure_time {_iso(departure)} is in the past (now {_iso(now)})")

        found = stations_along(self._stations(), path, max_station_km=self.max_station_km)
        # Forecasts for nearby stations are much alike: keep the passes, then road stations
        # spread along the route (same choice as the observation tool).
        chosen = pick_stations(found, self.max_stations,
                               is_road=lambda f: f[0].road_station, elevation=lambda f: f[0].elevation_m)

        forecasts = self._forecasts([s.id for s, _, _ in chosen])
        horizon = max((fc.steps[-1].time for fc in forecasts.values()), default=None)
        total_km = path[-1].km if path else 0.0
        arrival = departure + timedelta(hours=total_km / self.speed_kmh)
        if horizon is not None and arrival > horizon:
            raise ValueError(f"the forecast only reaches {_iso(horizon)}; departure_time {_iso(departure)} is too far ahead")

        rows = []
        for station, point, _ in chosen:
            fc = forecasts.get(station.id)
            if fc is None:
                continue
            eta = departure + timedelta(hours=point.km / self.speed_kmh)
            step = fc.at(eta)
            if step is None or abs(step.time - eta) > timedelta(hours=3, minutes=1):
                continue
            rows.append((station, point, eta, step, fc.issued_at))

        icy = [r for r in rows if _ice_possible(r[3])]
        snowy = [r for r in rows if _snow_or_sleet(r[3])]
        winds = [r for r in rows if r[3].wind_ms is not None]
        temps = [r for r in rows if r[3].temp_c is not None]
        issued = [r[4] for r in rows if r[4]]
        summary: dict[str, Any] = {
            "departure": _iso(departure),
            "estimated_arrival": _iso(arrival),
            "hours_ahead": round((departure - now).total_seconds() / 3600, 1),
            "stations_with_forecast": len(rows),
            "wind_warnings": [
                {"station": s.name, "km_from_start": round(p.km), "near_segment": p.segment,
                 "at": _iso(st.time), "warning": _wind_warning(st)}
                for s, p, _, st, _ in rows if _wind_warning(st)
            ],
            "ice_possible_at": len(icy),
            "snow_or_sleet_at": len(snowy),
        }
        if icy:
            summary["ice_possible_between_km"] = [round(icy[0][1].km), round(icy[-1][1].km)]
        if snowy:
            summary["snow_or_sleet_between_km"] = [round(snowy[0][1].km), round(snowy[-1][1].km)]
        if winds:
            s, p, _, st, _ = max(winds, key=lambda r: r[3].wind_ms)
            summary["strongest_wind"] = {"station": s.name, "km_from_start": round(p.km), "wind_ms": st.wind_ms, "at": _iso(st.time)}
        if temps:
            s, p, _, st, _ = min(temps, key=lambda r: r[3].temp_c)
            summary["coldest"] = {"station": s.name, "km_from_start": round(p.km), "temp_c": st.temp_c, "at": _iso(st.time)}

        notes = [
            "This is a FORECAST for the planned trip, not current conditions. It gets less reliable "
            "the further ahead it looks.",
            "Road status (open, closed, slippery) cannot be forecast: Vegagerðin publishes current "
            "conditions only.",
            f"Arrival times at each station assume an average {self.speed_kmh:.0f} km/h with no stops.",
            "The forecast has mean wind only (no gusts) and no road surface temperature.",
            f"Flags are this project's heuristics, not official guidance: wind warnings at mean wind "
            f"{WIND_HIGH_SIDED_MS:.0f} and {WIND_ALL_VEHICLES_MS:.0f} m/s; ice possible at -3 to 2 °C; "
            "snow or sleet from the weather text, or precipitation at 1 °C or below.",
            "Times are UTC, which is Iceland's local time.",
        ]
        if getattr(self._forecasts, "last_error", None):
            notes.append(f"Forecast refresh failed ({self._forecasts.last_error}); some forecasts may be older.")

        return {
            "summary": summary,
            "forecast_issued": _iso(max(issued)) if issued else None,
            "station_columns": ["station", "km_from_start", "near_segment", "road_station", "eta",
                                "forecast_for", "temp_c", "wind_ms", "wind_from", "weather", "precip_mm"],
            "stations": [
                [s.name, round(p.km), p.segment, s.road_station, _iso(eta), _iso(st.time), st.temp_c,
                 st.wind_ms, st.wind_from, st.weather, st.precip_mm]
                for s, p, eta, st, _ in rows
            ],
            "notes": notes,
        }
