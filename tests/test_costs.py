import pytest

from builders import S1, make_condition
from roadroute.costs import BLOCKED, CAUTION, FOUR_BY_FOUR, OK, STANDARD, UNKNOWN


@pytest.mark.parametrize(
    "status", ["OFAERT_ANNAD", "OFAERT_VEDUR", "LOKAD", "FAERT_FJALLABILUM"]
)
def test_blocked_statuses_for_a_standard_car(status):
    cond = make_condition(S1, status=status)
    assert STANDARD.kind(cond) == BLOCKED
    assert STANDARD.multiplier("1", cond) is None


def test_mountain_roads_open_up_for_4x4():
    cond = make_condition(S1, status="FAERT_FJALLABILUM")
    assert FOUR_BY_FOUR.kind(cond) == CAUTION
    assert FOUR_BY_FOUR.multiplier("1", cond) == pytest.approx(1.5)
    # ...but a closure is a closure for everyone.
    assert FOUR_BY_FOUR.kind(make_condition(S1, status="OFAERT_ANNAD")) == BLOCKED


def test_total_driving_ban_in_the_secondary_status_blocks_the_segment():
    cond = make_condition(S1, status="GREIDFAERT", status_extra="ALLUR_AKSTUR_BANN")
    assert STANDARD.kind(cond) == BLOCKED
    assert FOUR_BY_FOUR.kind(cond) == BLOCKED


def test_ok_and_cautionary_statuses():
    assert STANDARD.kind(make_condition(S1)) == OK
    assert STANDARD.multiplier("1", make_condition(S1)) == 1.0
    for status, expected in (("OTHEKKT", 3.0), ("EKKI_I_THJONUSTU", 8.0)):
        cond = make_condition(S1, status=status)
        assert STANDARD.kind(cond) == CAUTION
        assert STANDARD.multiplier("1", cond) == pytest.approx(expected)


def test_unrecognised_status_is_penalised_and_flagged_not_trusted():
    cond = make_condition(S1, status="SOME_NEW_CODE")
    assert STANDARD.kind(cond) == UNKNOWN
    assert STANDARD.multiplier("1", cond) == pytest.approx(2.0)


def test_missing_condition_record_is_flagged_and_penalised():
    assert STANDARD.kind(None) == UNKNOWN
    assert STANDARD.multiplier("1", None) == pytest.approx(2.0)


def test_side_roads_and_unploughed_roads_cost_more():
    ok = make_condition(S1)
    assert STANDARD.multiplier("99", ok) == 1.0
    assert STANDARD.multiplier("550", ok) == pytest.approx(1.1)
    assert STANDARD.multiplier(None, ok) == 1.0
    unploughed = make_condition(S1, plow_rule="EKKI_MOKAD")
    assert STANDARD.multiplier("1", unploughed) == pytest.approx(2.0)
    assert STANDARD.multiplier("1", make_condition(S1, plow_rule="2X")) == 1.0
