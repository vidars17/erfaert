"""Weather along a route: parsing, station matching, summaries, caching and the tool."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from builders import A, C, make_snapshot, xy
from roadroute import Gazetteer, RoutePlanner, Toolbox
from roadroute.weather import (
    Cached,
    Observation,
    PathPoint,
    RouteWeather,
    Station,
    WeatherError,
    fetch_latest,
    parse_observations,
    parse_stations,
    sample_path,
)

DATA = Path(__file__).parent / "data"
NOW = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)


def station(sid: int, dx: float, dy: float, *, road: bool = True, name: str | None = None) -> Station:
    x, y = xy(dx, dy)
    return Station(sid, name or f"S{sid}", 0.0, 0.0, 100.0, "Vegagerðin" if road else "Veðurstofa Íslands", x, y)


def obs(sid: int, *, minutes_ago: int = 10, air=2.0, road=1.0, wind=5.0, gust=8.0, rh=80.0) -> Observation:
    return Observation(sid, NOW - timedelta(minutes=minutes_ago), air, road, wind, gust, "N", rh)


def straight_path(km: float = 20.0, step_km: float = 1.0) -> list[PathPoint]:
    """A path heading east from A, like the synthetic main line A-B-C."""
    n = int(km / step_km)
    return [PathPoint(*xy(i * step_km * 1000, 0), i * step_km, "Hringvegur: A - B" if i * step_km < 10 else "Hringvegur: B - C")
            for i in range(n + 1)]


def weather(stations, observations, **kwargs) -> RouteWeather:
    return RouteWeather(lambda: stations, lambda: observations, clock=lambda: NOW, **kwargs)


# -- parsing (real API responses, saved 2026-11-20) -----------------------------------------

def test_parse_stations_snapshot():
    rows = json.loads((DATA / "vedur_stations.snapshot.json").read_text(encoding="utf-8"))
    stations = parse_stations(rows)
    assert 250 < len(stations) <= len(rows)
    assert all(s.x and s.y for s in stations)
    holta = next(s for s in stations if s.id == 32097)
    assert holta.name == "Holtavörðuheiði" and holta.road_station


def test_parse_stations_skips_other_types_and_closed_stations():
    rows = [
        {"station": 1, "name": "Synop", "type": "sk", "lat": 64.1, "lon": -21.9, "ending": None},
        {"station": 2, "name": "Closed", "type": "sj", "lat": 64.1, "lon": -21.9, "ending": 2020},
        {"station": 3, "name": "No coords", "type": "sj", "lat": None, "lon": -21.9, "ending": None},
        {"station": 4, "name": "Good", "type": "sj", "lat": 64.1, "lon": -21.9, "ending": None, "owner": "Vegagerðin"},
    ]
    assert [s.id for s in parse_stations(rows)] == [4]


def test_parse_observations_snapshot():
    rows = json.loads((DATA / "vedur_aws_10min_latest.snapshot.json").read_text(encoding="utf-8"))
    observations = parse_observations(rows)
    o = observations[32097]
    assert o.time == datetime(2026, 11, 20, 14, 10, tzinfo=timezone.utc)
    assert o.road_temp_c == pytest.approx(-4.59) and o.wind_from == "N"


def test_parse_observations_tolerates_missing_values():
    rows = [{"station": 9, "time": "2026-11-20T14:10:00", "t": None, "fg": 12.5, "d_txt": None},
            {"station": "bad", "time": "2026-11-20T14:10:00"},
            {"station": 10, "time": "not a time"}]
    out = parse_observations(rows)
    assert list(out) == [9] and out[9].gust_ms == 12.5 and out[9].air_temp_c is None


def test_fetch_latest_sends_parameters_and_wraps_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"station": 9, "time": "2026-11-20T14:10:00", "t": 1.0}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert 9 in fetch_latest(client=client)
    assert seen["url"].endswith("/observations/aws/10min/latest?parameters=basic")

    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(WeatherError, match="could not fetch"):
        fetch_latest(client=failing)


# -- geometry --------------------------------------------------------------------------------

def test_sample_path_spacing():
    samples = sample_path(straight_path(20), spacing_km=5)
    assert [round(s.km) for s in samples] == [0, 5, 10, 15, 20]


def test_stations_along_keeps_nearby_stations_in_driving_order():
    stations = [station(1, 15000, 2000), station(2, 2000, -1000), station(3, 10000, 30000)]
    found = weather(stations, {}).stations_along(straight_path(20))
    assert [s.id for s, _, _ in found] == [2, 1], "station 3 is 30 km off the route"
    (_, point, dist_km) = found[0]
    assert dist_km == pytest.approx(1.0, abs=0.1) and point.segment == "Hringvegur: A - B"


# -- summaries -------------------------------------------------------------------------------

def test_summary_flags_gusts_and_ice():
    stations = [station(1, 3000, 0), station(2, 12000, 0)]
    observations = {1: obs(1, road=-2.5, gust=10.0), 2: obs(2, road=3.0, gust=27.0)}
    out = weather(stations, observations).summarise(straight_path(20))
    s = out["summary"]
    assert s["stations_reporting"] == 2 and s["ice_possible_at"] == 1
    assert s["coldest_road_surface"] == {"station": "S1", "km_from_start": 3, "road_surface_c": -2.5}
    assert s["strongest_gust"]["gust_ms"] == 27.0
    (warning,) = s["gust_warnings"]
    assert warning["station"] == "S2" and "dangerous for all vehicles" in warning["warning"]
    assert out["stations"][0][0] == "S1" and out["station_columns"][7] == "road_surface_c"


def test_air_temperature_and_humidity_stand_in_for_a_missing_road_sensor():
    stations = [station(1, 3000, 0, road=False)]
    out = weather(stations, {1: obs(1, road=None, air=0.5, rh=95)}).summarise(straight_path(10))
    assert out["summary"]["ice_possible_at"] == 1


def test_stale_and_empty_readings_are_left_out_and_counted():
    stations = [station(1, 3000, 0), station(2, 6000, 0), station(3, 9000, 0)]
    observations = {1: obs(1, minutes_ago=180),
                    2: Observation(2, NOW, None, None, None, None, None, None),
                    3: obs(3)}
    out = weather(stations, observations).summarise(straight_path(10))
    assert [row[0] for row in out["stations"]] == ["S3"]
    assert any("over 2 hours old" in n for n in out["notes"])
    assert any("No usable reading from 1" in n for n in out["notes"])


def test_cap_keeps_gusts_then_road_stations():
    stations = [station(i, i * 1000, 0, road=(i % 2 == 0)) for i in range(1, 21)]
    observations = {i: obs(i, gust=30.0 if i == 7 else 5.0) for i in range(1, 21)}
    out = weather(stations, observations, max_stations=5).summarise(straight_path(20))
    names = [row[0] for row in out["stations"]]
    assert len(names) == 5
    assert "S7" in names, "the gusty (non-road) station is always kept"
    assert sum(int(n[1:]) % 2 == 0 for n in names) == 4, "the rest are road stations"


def test_failed_refresh_is_noted():
    source = Cached(lambda: (_ for _ in ()).throw(WeatherError("down")), ttl_s=0)
    source._value, source.last_error = {1: obs(1)}, "down"
    out = RouteWeather(lambda: [station(1, 3000, 0)], source, clock=lambda: NOW).summarise(straight_path(10))
    assert any("refresh failed" in n for n in out["notes"])


def test_cached_serves_last_good_value_on_failure():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] > 1:
            raise WeatherError("down")
        return "fresh"

    clock = {"t": 0.0}
    cached = Cached(fetch, ttl_s=10, clock=lambda: clock["t"])
    assert cached() == "fresh"
    clock["t"] = 100
    assert cached() == "fresh" and cached.last_error == "down"


def test_cached_raises_when_it_has_nothing():
    with pytest.raises(WeatherError):
        Cached(lambda: (_ for _ in ()).throw(WeatherError("down")), ttl_s=10)()


# -- planner and tool ------------------------------------------------------------------------

def test_route_path_follows_the_planned_route(network):
    planner = RoutePlanner(network, lambda: make_snapshot())
    total_km, path = planner.route_path(A, C)
    assert total_km == pytest.approx(20.0, abs=0.1)
    assert path[0].km == 0 and path[-1].km == pytest.approx(total_km, abs=0.01)
    assert path[0].segment == "Hringvegur: A - B" and path[-1].segment == "Hringvegur: B - C"


def test_route_path_none_when_no_route(network):
    snapshot = make_snapshot({"1002": {"status": "LOKAD"}, "1004": {"status": "LOKAD"}})
    assert RoutePlanner(network, lambda: snapshot).route_path(A, C) is None


def _toolbox(network, weather_service=None, snapshot=None):
    snapshot = snapshot or make_snapshot()
    gazetteer = Gazetteer({"Aville": (A.lat, A.lon), "Cville": (C.lat, C.lon)}, aliases={})
    return Toolbox(RoutePlanner(network, lambda: snapshot), gazetteer, weather_service)


def test_weather_tool_only_offered_when_configured(network):
    assert "get_route_weather" not in [d["name"] for d in _toolbox(network).definitions]
    with_weather = _toolbox(network, weather([], {}))
    assert "get_route_weather" in [d["name"] for d in with_weather.definitions]
    out = json.loads(_toolbox(network).call("get_route_weather", {"origin": "Aville", "destination": "Cville"}))
    assert out["error"] == "unknown_tool"


def test_weather_tool_end_to_end(network):
    service = weather([station(1, 15000, 500)], {1: obs(1, road=-1.0)})
    out = json.loads(_toolbox(network, service).call(
        "get_route_weather", {"origin": "Aville", "destination": "Cville"}))
    assert out["route_found"] is True and out["total_km"] == pytest.approx(20.0, abs=0.1)
    assert out["stations"][0][:3] == ["S1", 15, "Hringvegur: B - C"]
    assert out["summary"]["ice_possible_at"] == 1


def test_weather_tool_reports_unavailable_weather(network):
    def broken():
        raise WeatherError("api.vedur.is timed out")

    service = RouteWeather(lambda: [station(1, 15000, 500)], broken, clock=lambda: NOW)
    out = json.loads(_toolbox(network, service).call(
        "get_route_weather", {"origin": "Aville", "destination": "Cville"}))
    assert out["error"] == "weather_unavailable" and "timed out" in out["message"]


def test_weather_tool_without_route(network):
    snapshot = make_snapshot({"1002": {"status": "LOKAD"}, "1004": {"status": "LOKAD"}})
    out = json.loads(_toolbox(network, weather([], {}), snapshot).call(
        "get_route_weather", {"origin": "Aville", "destination": "Cville"}))
    assert out["route_found"] is False and "stations" not in out
