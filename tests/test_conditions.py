from datetime import datetime, timezone

import httpx
import pytest

from conftest import CONDITIONS_SNAPSHOT
from roadroute.conditions import (
    CachedConditions,
    ConditionsError,
    fetch_conditions,
    load_conditions,
    parse_conditions,
)

MINIMAL = """<ArrayOfFaerd2017 xmlns:i="http://www.w3.org/2001/XMLSchema-instance"
  xmlns="http://schemas.datacontract.org/2004/07/VgApi.Models">
<Faerd2017><AstandLysing>Greiðfært</AstandLysing><AstandLysingEn>Easily passable</AstandLysingEn>
<AstandVidbotaruppl i:nil="true" /><AstandYfirbord>GREIDFAERT</AstandYfirbord>
<AsthungiLysingEn>Axle weight limit 7 tons</AsthungiLysingEn>
<DagsButurBreyttist>2023-10-05T10:56:19Z</DagsButurBreyttist>
<DagsKeyrtUt>2026-11-06T13:11:51Z</DagsKeyrtUt><DagsSkrad>2026-11-06T02:55:01Z</DagsSkrad>
<FrkvLysingEn i:nil="true" /><FulltNafnButs>Hringvegur: Breiðdalsvík - Streiti</FulltNafnButs>
<IdButur>911080037</IdButur><Snjomokstursregla>7X</Snjomokstursregla>
<StuttNafnButs>Breiðdalsvík-Streiti</StuttNafnButs></Faerd2017>
</ArrayOfFaerd2017>"""


def test_parses_a_record_with_nil_and_filled_fields():
    snap = parse_conditions(MINIMAL)
    c = snap.get("911080037")
    assert c.name == "Hringvegur: Breiðdalsvík - Streiti"
    assert c.status == "GREIDFAERT" and c.plow_rule == "7X"
    assert c.status_extra is None and c.notes_en is None  # xsi:nil -> None
    assert c.hindrance_en == "Axle weight limit 7 tons"
    assert c.registered_at == datetime(2026, 11, 6, 2, 55, 1, tzinfo=timezone.utc)
    assert snap.generated_at == datetime(2026, 11, 6, 13, 11, 51, tzinfo=timezone.utc)


def test_real_snapshot_fixture():
    snap = load_conditions(CONDITIONS_SNAPSHOT)
    assert len(snap) == 708
    statuses = {c.status for c in snap.by_segment.values()}
    assert {"GREIDFAERT", "OTHEKKT", "EKKI_I_THJONUSTU", "OFAERT_ANNAD", "LOKAD"} <= statuses
    closed = [c for c in snap.by_segment.values() if c.status_extra == "ALLUR_AKSTUR_BANN"]
    assert len(closed) == 1 and closed[0].status == "LOKAD"


def test_registered_range():
    snap = load_conditions(CONDITIONS_SNAPSHOT)
    ids = list(snap.by_segment)[:50]
    lo, hi = snap.registered_range(ids)
    assert lo <= hi
    assert snap.registered_range([]) == (None, None)
    assert snap.registered_range(["no-such-id"]) == (None, None)


@pytest.mark.parametrize("bad", ["<not-xml", "<ArrayOfFaerd2017/>", "<a><b/></a>"])
def test_bad_or_empty_feeds_raise(bad):
    with pytest.raises(ConditionsError):
        parse_conditions(bad)


def test_records_missing_key_fields_are_skipped():
    xml = MINIMAL.replace("<IdButur>911080037</IdButur>", "")
    with pytest.raises(ConditionsError):  # the only record was unusable
        parse_conditions(xml)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_conditions_parses_the_response():
    client = _client(lambda request: httpx.Response(200, content=MINIMAL.encode("utf-8")))
    assert len(fetch_conditions("http://example.test/feed", client=client)) == 1


def test_fetch_conditions_wraps_http_errors():
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(ConditionsError, match="could not fetch"):
        fetch_conditions("http://example.test/feed", client=client)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_cache_respects_ttl():
    clock, calls = FakeClock(), []
    snap = parse_conditions(MINIMAL)
    cache = CachedConditions(lambda: calls.append(1) or snap, ttl_s=60, clock=clock)
    cache(), cache()
    assert len(calls) == 1
    clock.now = 61
    cache()
    assert len(calls) == 2


def test_failed_refresh_falls_back_to_last_good_copy():
    clock, snap = FakeClock(), parse_conditions(MINIMAL)
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise ConditionsError("boom")
        return snap

    cache = CachedConditions(fetch, ttl_s=10, clock=clock)
    assert cache() is snap and cache.last_error is None
    clock.now, state["fail"] = 20, True
    assert cache() is snap
    assert cache.last_error == "boom"


def test_first_fetch_failure_propagates():
    def fetch():
        raise ConditionsError("down")

    with pytest.raises(ConditionsError):
        CachedConditions(fetch)()
