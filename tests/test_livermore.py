from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

import pytest

from server import domain
from server.seat_livermore import CASES, POLICY, _entry_band, check_plan, decide_entry, make_plan, seat_evidence


NORMAL = CASES["livermore-normal"]
OPEN_DAY = "2026-10-26"


def _run(case_id):
    case = CASES[case_id]
    snapshot = seat_evidence(case)
    plan = make_plan(snapshot)
    checked = check_plan(plan, snapshot)
    assert checked["ok"], checked["checks"]
    return domain.replay(case, plan, checked, policy=POLICY, validator=check_plan, evidence_reader=seat_evidence)


def _decision_bar(case):
    return next(row for row in case["bars"] if row["date"] == case["decision_date"])


def _auction(**changes):
    return {"open": 11.33, **NORMAL["auction_snapshot"][OPEN_DAY], **changes}


@pytest.mark.parametrize("case_id", CASES)
def test_fixture_bars_are_valid_and_follow_previous_close_limits(case_id):
    bars = CASES[case_id]["bars"]
    assert [bar["date"] for bar in bars] == sorted({bar["date"] for bar in bars})
    for bar in bars:
        assert domain._bar_error(bar) is None
    for previous, current in zip(bars, bars[1:]):
        close = Decimal(str(previous["close"]))
        for field, multiplier in (("limit_up", "1.10"), ("limit_down", "0.90")):
            expected = (close * Decimal(multiplier)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            assert Decimal(str(current[field])) == expected


def test_seat_evidence_contains_no_future_auction_or_future_bars():
    case = deepcopy(NORMAL)
    snapshot = seat_evidence(case)
    metrics = snapshot["metrics"]
    assert metrics["tradable_count"] == 120
    assert metrics["closed_at_limit_up"] is True
    assert metrics["range_position_120_pct"] == pytest.approx(7.284768)
    assert metrics["eligible"] is True
    assert all(bar["date"] <= case["decision_date"] for bar in snapshot["bars"])
    assert not any(OPEN_DAY in ref for ref in snapshot["evidence_ids"])
    case["description"] = "private outcome"
    case["auction_snapshot"][OPEN_DAY].update(auction_amount=1, index_change_pct=-50, regional_ok=False)
    case["bars"][-1].update(high=999, close=999, volume=10**15, private_outcome=True)
    assert seat_evidence(case) == snapshot


def test_exact_ten_percent_is_valid_observation_without_downgrade():
    case = CASES["livermore-no-candidate"]
    snapshot = seat_evidence(case)
    assert snapshot["metrics"]["range_position_120_pct"] == 10
    assert snapshot["metrics"]["eligible"] is False
    plan = make_plan(snapshot)
    assert plan.action == "hold"
    assert plan.entry_low is plan.entry_high is plan.position_pct is None
    assert check_plan(plan, snapshot)["ok"]
    assert _run(case["id"])["status"] == "hold"


@pytest.mark.parametrize("name", ["*ST合成标的", "st样例", "退市样例", ""])
def test_st_delisting_and_missing_name_do_not_become_candidates(name):
    case = {**NORMAL, "name": name}
    ev = seat_evidence(case)
    assert ev["metrics"]["excluded_by_name"]
    assert ev["metrics"]["eligible"] is False
    assert make_plan(ev).action == "hold"


def test_candidate_needs_120_tradable_bars_and_a_genuine_limit_close():
    short_case = {**NORMAL, "bars": NORMAL["bars"][1:]}
    short = seat_evidence(short_case)
    assert short["metrics"]["tradable_count"] == 119
    assert short["metrics"]["range_position_120_pct"] is None
    assert make_plan(short).action == "hold"
    case = deepcopy(NORMAL)
    _decision_bar(case)["close"] = 10.99
    ev = seat_evidence(case)
    assert ev["metrics"]["closed_at_limit_up"] is False
    assert make_plan(ev).action == "hold"


def test_buy_band_respects_the_exchange_tick_without_rounding_above_seven_percent():
    ev = seat_evidence(NORMAL)
    plan = make_plan(ev)
    assert (plan.entry_low, plan.entry_high, plan.position_pct) == (11.01, 11.77, 5.0)
    assert plan.stop_loss is plan.take_profit is None
    assert _entry_band(Decimal("10.10")) == (Decimal("10.11"), Decimal("10.80"))


@pytest.mark.parametrize("changes", [{"position_pct": 5.01}, {"entry_low": 11.0},
                                     {"entry_high": 11.78}, {"stop_loss": 10.0}, {"take_profit": 12.0}])
def test_frozen_parameters_cannot_be_replaced(changes):
    ev = seat_evidence(NORMAL)
    plan = make_plan(ev).model_copy(update=changes)
    checks = check_plan(plan, ev)
    assert not checks["ok"]
    assert "livermore_frozen" in {check["code"] for check in checks["checks"] if not check["passed"]}
    forged = domain.replay(NORMAL, plan, {"ok": True}, policy=POLICY, validator=check_plan, evidence_reader=seat_evidence)
    assert forged["status"] == "blocked"


@pytest.mark.parametrize(("opening", "expected_rule", "budget"), [
    (10.99, "R2", None), (11.0, "R2", None), (11.01, "R1", Decimal("5000")),
    (11.77, "R1", Decimal("5000")), (11.78, "R4", None),
    (12.04, "R4", None), (12.05, "R3", None),
])
def test_auction_band_and_rejection_codes(opening, expected_rule, budget):
    plan = make_plan(seat_evidence(NORMAL))
    choice = decide_entry(NORMAL, plan, _auction(open=opening))
    assert choice.budget == budget
    assert choice.reason.startswith(expected_rule)


def test_auction_index_and_turnover_boundaries_at_equal_threshold():
    plan = make_plan(seat_evidence(NORMAL))
    assert decide_entry(NORMAL, plan, _auction(auction_amount=100_000)).budget == Decimal("5000")
    assert decide_entry(NORMAL, plan, _auction(auction_amount=99_999)).reason.startswith("R0")
    assert decide_entry(NORMAL, plan, _auction(index_change_pct=-3, auction_amount=70_000)).budget == Decimal("3500")
    assert decide_entry(NORMAL, plan, _auction(index_change_pct=-3, regional_ok=False)).budget is None


def test_intraday_volume_cannot_substitute_for_missing_auction_amount():
    case = deepcopy(NORMAL)
    case["auction_snapshot"][OPEN_DAY].pop("auction_amount")
    ev = seat_evidence(case)
    plan = make_plan(ev)
    result = domain.replay(case, plan, check_plan(plan, ev), policy=POLICY, validator=check_plan, evidence_reader=seat_evidence)
    assert result["status"] == "no_fill"
    assert "缺少有效竞价成交额" in result["reason"]


def test_normal_replay_is_t_plus_one_and_accounts_for_both_side_fees():
    outcome = _run("livermore-normal")
    assert outcome["status"] == "completed"
    assert [e["type"] for e in outcome["events"]] == ["buy", "t_plus_one", "sell"]
    buy, _, sell = outcome["events"]
    assert (buy["date"], buy["price"]) == (OPEN_DAY, 11.33)
    assert (sell["date"], sell["price"]) == ("2026-10-27", 11.74)
    assert outcome["metrics"]["shares"] == 400
    assert outcome["metrics"]["fees"] == 12.45
    assert outcome["metrics"]["realized_pnl"] == 151.55


@pytest.mark.parametrize(("case_id", "code"), [("livermore-r2", "R2"), ("livermore-r0", "R0")])
def test_rejected_auction_stays_uninvested(case_id, code):
    out = _run(case_id)
    assert out["status"] == "no_fill"
    assert out["reason"].startswith(code)
    assert out["metrics"]["ending_cash"] == 100000


def test_reduced_gate_uses_3500_including_fees():
    out = _run("livermore-reduced")
    assert out["status"] == "completed"
    assert "2/3" in out["events"][0]["detail"] and "3500 元" in out["events"][0]["detail"]
    assert out["metrics"]["shares"] == 300


def test_z1_defers_once_even_if_next_close_is_at_limit_up():
    out = _run("livermore-z1")
    assert [e["type"] for e in out["events"]] == ["buy", "t_plus_one", "exit_deferred", "sell"]
    assert (out["events"][-1]["date"], out["events"][-1]["price"]) == ("2026-10-28", 13.77)


def test_sell_after_frozen_limit_down_uses_next_tradable_open():
    out = _run("livermore-limit-down")
    assert [e["type"] for e in out["events"]] == ["buy", "t_plus_one", "exit_deferred", "sell"]
    assert (out["events"][-1]["date"], out["events"][-1]["price"]) == ("2026-10-28", 9.31)
    assert "虚拟开盘卖出" in out["events"][-1]["detail"]
