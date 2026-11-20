"""Forecasts along a route: parsing, fetching, caching, arrival times, flags and the tool."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from builders import A, C, make_snapshot, xy
from roadroute import Gazetteer, RoutePlanner, Toolbox
from roadroute.forecast import (
    CachedForecasts,
    ForecastError,
    ForecastStep,
    RouteForecast,
    StationForecast,
    fetch_forecasts,
    parse_forecasts,
)
from roadroute.weather import PathPoint, Station, spread

DATA = Path(__file__).parent / "data"
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def station(sid: int, dx: float, *, road: bool = True, ele: float = 100.0) -> Station:
    x, y = xy(dx, 500)
    return Station(sid, f"S{sid}", 0.0, 0.0, ele, "Vegagerðin" if road else "Veðurstofa Íslands", x, y)


def hourly(sid: int, start: datetime, hours: int = 72, **values) -> StationForecast:
    defaults = dict(temp_c=5.0, wind_ms=4.0, wind_from="S", weather="Overcast", precip_mm=0.0, dew_point_c=1.0)
    defaults.update(values)
    steps = tuple(ForecastStep(time=start + timedelta(hours=h), **defaults) for h in range(hours))
    return StationForecast(sid, start, steps)


def path(km: float = 140.0) -> list[PathPoint]:
    return [PathPoint(*xy(k * 1000, 0), float(k), "Hringvegur") for k in range(int(km) + 1)]


def forecaster(stations, forecasts: dict, **kwargs) -> RouteForecast:
    return RouteForecast(lambda: stations, lambda ids: {i: forecasts[i] for i in ids if i in forecasts},
                         clock=lambda: NOW, **kwargs)


# -- parsing and fetching ----------------------------------------------------------------------

def test_parse_saved_forecasts():
    forecasts = parse_forecasts((DATA / "vedur_forecasts.snapshot.xml").read_bytes())
    holta = forecasts[32097]
    assert holta.issued_at == datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    assert len(holta.steps) > 50 and holta.steps[0].time < holta.steps[-1].time
    assert holta.steps[0].temp_c is not None and holta.steps[0].wind_from


def test_parse_skips_invalid_stations():
    xml = """<forecasts>
      <station id="1" valid="1"><name>Good</name><atime>2026-11-20 12:00:00</atime><err></err>
        <forecast><ftime>2026-11-20 15:00:00</ftime><T>-2</T><F>7</F><D>N</D><W>Light snow</W><R>0.4</R></forecast>
      </station>
      <station id="2" valid="0"><name>Bad</name><err>Station not found</err></station>
      <station id="x" valid="1"></station>
    </forecasts>"""
    out = parse_forecasts(xml)
    assert list(out) == [1]
    step = out[1].steps[0]
    assert (step.temp_c, step.wind_ms, step.weather, step.precip_mm) == (-2.0, 7.0, "Light snow", 0.4)


def test_parse_rejects_garbage():
    with pytest.raises(ForecastError):
        parse_forecasts("not xml")


def test_fetch_batches_ids_and_sends_parameters():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, content=b"<forecasts></forecasts>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_forecasts(range(1, 46), client=client)
    assert len(seen) == 3, "45 ids in batches of 20"
    assert seen[0]["type"] == "forec" and seen[0]["time"] == "1h" and seen[0]["ids"].startswith("1;2;3")


def test_fetch_wraps_http_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ForecastError, match="could not fetch"):
        fetch_forecasts([1], client=client)


# -- caching -------------------------------------------------------------------------------------

def test_cache_fetches_only_missing_or_expired_stations():
    calls = []
    clock = {"t": 0.0}

    def fetch(ids):
        calls.append(sorted(ids))
        return {i: hourly(i, NOW) for i in ids if i != 3}  # station 3 has no forecast

    cache = CachedForecasts(fetch, ttl_s=100, clock=lambda: clock["t"])
    assert set(cache([1, 2, 3])) == {1, 2}
    assert set(cache([1, 2, 3])) == {1, 2}
    assert calls == [[1, 2, 3]], "second call served from cache, including 'no forecast' for 3"
    cache([2, 4])
    assert calls[-1] == [4]
    clock["t"] = 200
    cache([1])
    assert calls[-1] == [1]


def test_cache_serves_old_values_when_refresh_fails():
    clock = {"t": 0.0}
    state = {"fail": False}

    def fetch(ids):
        if state["fail"]:
            raise ForecastError("down")
        return {i: hourly(i, NOW) for i in ids}

    cache = CachedForecasts(fetch, ttl_s=10, clock=lambda: clock["t"])
    cache([1])
    clock["t"], state["fail"] = 100, True
    assert set(cache([1])) == {1} and cache.last_error == "down"
    with pytest.raises(ForecastError):
        cache([9])


# -- along the route -----------------------------------------------------------------------------

def test_each_station_gets_the_forecast_for_when_the_driver_arrives():
    stations = [station(1, 0), station(2, 70000), station(3, 140000)]
    forecasts = {1: hourly(1, NOW, temp_c=5.0), 2: hourly(2, NOW), 3: hourly(3, NOW)}
    departure = NOW + timedelta(hours=10)
    out = forecaster(stations, forecasts, speed_kmh=70).summarise(path(140), departure)
    rows = {row[0]: row for row in out["stations"]}
    assert rows["S1"][4] == "2026-11-21T01:00Z"  # eta at km 0
    assert rows["S2"][4] == "2026-11-21T02:00Z"  # 70 km at 70 km/h
    assert rows["S3"][5] == "2026-11-21T03:00Z"  # forecast_for: nearest step to the eta
    s = out["summary"]
    assert s["departure"] == "2026-11-21T01:00Z" and s["estimated_arrival"] == "2026-11-21T03:00Z"
    assert s["hours_ahead"] == 10.0
    assert out["forecast_issued"] == "2026-11-20T15:00Z"


def test_flags_wind_snow_and_ice():
    stations = [station(1, 10000), station(2, 50000), station(3, 100000)]
    forecasts = {
        1: hourly(1, NOW, wind_ms=22.0),
        2: hourly(2, NOW, temp_c=0.0, precip_mm=1.2, weather="Overcast"),
        3: hourly(3, NOW, wind_ms=14.0, weather="Light snow", temp_c=4.0),
    }
    s = forecaster(stations, forecasts).summarise(path(140), NOW + timedelta(hours=2))["summary"]
    warnings = {w["station"]: w["warning"] for w in s["wind_warnings"]}
    assert "dangerous for all vehicles" in warnings["S1"]
    assert "high-sided" in warnings["S3"]
    assert s["snow_or_sleet_at"] == 2, "S2 by temperature and precipitation, S3 by the weather text"
    assert s["ice_possible_at"] == 1 and s["ice_possible_between_km"] == [50, 50]
    assert s["strongest_wind"]["station"] == "S1" and s["coldest"]["station"] == "S2"


def test_departure_in_the_past_is_rejected():
    with pytest.raises(ValueError, match="in the past"):
        forecaster([station(1, 0)], {1: hourly(1, NOW)}).summarise(path(10), NOW - timedelta(hours=3))


def test_departure_beyond_the_forecast_is_rejected():
    with pytest.raises(ValueError, match="only reaches"):
        forecaster([station(1, 0)], {1: hourly(1, NOW, hours=24)}).summarise(path(10), NOW + timedelta(days=5))


def test_stations_without_a_nearby_step_are_left_out():
    sparse = StationForecast(2, NOW, (ForecastStep(NOW, 1, 1, "N", None, 0, None),
                                      ForecastStep(NOW + timedelta(hours=48), 1, 1, "N", None, 0, None)))
    out = forecaster([station(1, 0), station(2, 5000)], {1: hourly(1, NOW), 2: sparse}).summarise(
        path(10), NOW + timedelta(hours=24))
    assert [row[0] for row in out["stations"]] == ["S1"]


def test_station_choice_keeps_the_passes_and_both_ends():
    stations = [station(i, i * 10000, ele=900.0 if i == 7 else 50.0) for i in range(15)]
    forecasts = {i: hourly(i, NOW) for i in range(15)}
    names = [row[0] for row in forecaster(stations, forecasts, max_stations=5).summarise(
        path(140), NOW + timedelta(hours=1))["stations"]]
    assert len(names) == 5 and "S7" in names, "the highest station is kept"
    assert names[0] == "S0" and names[-1] == "S14", "both ends of the route are covered"


def test_spread_includes_first_and_last():
    assert spread(list(range(10)), 3) == [0, 4, 9] or spread(list(range(10)), 3) == [0, 5, 9]
    assert spread(list(range(10)), 2) == [0, 9]
    assert spread([1, 2], 5) == [1, 2]


# -- the tool -------------------------------------------------------------------------------------

def _toolbox(network, forecast=None, snapshot=None):
    snapshot = snapshot or make_snapshot()
    gazetteer = Gazetteer({"Aville": (A.lat, A.lon), "Cville": (C.lat, C.lon)}, aliases={})
    return Toolbox(RoutePlanner(network, lambda: snapshot), gazetteer, forecast=forecast)


def test_forecast_tool_end_to_end(network):
    service = forecaster([station(1, 15000)], {1: hourly(1, NOW, temp_c=-1.0)})
    out = json.loads(_toolbox(network, service).call("get_route_forecast", {
        "origin": "Aville", "destination": "Cville", "departure_time": "2026-11-21T09:00"}))
    assert out["route_found"] is True and out["total_km"] == pytest.approx(20.0, abs=0.1)
    assert out["summary"]["departure"] == "2026-11-21T09:00Z"
    assert out["stations"][0][0] == "S1" and out["summary"]["ice_possible_at"] == 1


def test_forecast_tool_offered_only_when_configured(network):
    assert "get_route_forecast" not in [d["name"] for d in _toolbox(network).definitions]
    tool = next(d for d in _toolbox(network, forecaster([], {})).definitions if d["name"] == "get_route_forecast")
    assert "departure_time" in tool["input_schema"]["required"]
    assert "origin" in tool["input_schema"]["properties"], "shares the route arguments"


@pytest.mark.parametrize("value, message", [
    (None, "required"), ("next monday", "ISO 8601"), ("2026-11-19T08:00", "in the past"),
])
def test_forecast_tool_reports_bad_departures(network, value, message):
    args = {"origin": "Aville", "destination": "Cville"}
    if value is not None:
        args["departure_time"] = value
    out = json.loads(_toolbox(network, forecaster([station(1, 1000)], {1: hourly(1, NOW)})).call("get_route_forecast", args))
    assert out["error"] == "invalid_arguments" and message in out["message"]


def test_forecast_tool_reports_unavailable_service(network):
    def broken(ids):
        raise ForecastError("xmlweather.vedur.is timed out")

    service = RouteForecast(lambda: [station(1, 1000)], broken, clock=lambda: NOW)
    out = json.loads(_toolbox(network, service).call("get_route_forecast", {
        "origin": "Aville", "destination": "Cville", "departure_time": "2026-11-21T09:00"}))
    assert out["error"] == "forecast_unavailable"
