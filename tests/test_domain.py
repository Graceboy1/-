from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from server.domain import Plan, evidence, replay, validate_plan
from server.fixtures import CASES


def _plan(case, **overrides):
    fields = {
        "action": "simulate", "rationale": "依据已完成的合成日线，进行条件化虚拟回放。",
        "evidence_ids": [f"{case['id']}:metrics", f"{case['id']}:bar:{case['decision_date']}"],
        "entry_low": 9.95, "entry_high": 10.15, "position_pct": 10,
        "stop_loss": 9.80, "take_profit": 10.60,
    }
    return Plan.model_validate({**fields, **overrides})


def _run(case_id, **plan_overrides):
    case = CASES[case_id]
    plan = _plan(case, **plan_overrides)
    return replay(case, plan, validate_plan(plan, evidence(case)))


def _failed(result):
    return {check["code"] for check in result["checks"] if not check["passed"]}


def test_evidence_excludes_future_bars_metrics_ids_and_result_hints():
    case = deepcopy(CASES["normal-fill"])
    ev = evidence(case)
    assert ev["metrics"]["data_valid"]
    assert ev["metrics"]["bar_count"] == 24
    assert ev["metrics"]["last_close"] == 10.0
    assert max(row["date"] for row in ev["bars"]) == case["decision_date"]
    assert all(set(row) == {"date", "open", "high", "low", "close", "volume", "limit_up", "limit_down", "suspended"} for row in ev["bars"])
    assert all("2026-10-26" not in ref for ref in ev["evidence_ids"])
    assert set(ev) == {"case_id", "symbol", "decision_date", "bars", "metrics", "evidence_ids"}

    case["description"] = "FUTURE OUTCOME MUST NOT LEAK"
    case["bars"][-1].update(close=123.45, volume=999, future_result="TARGET_HIT")
    case["bars"][0]["future_result"] = "ALSO_NOT_ALLOWED"
    assert evidence(case) == ev
    case["bars"] = [row for row in case["bars"] if row["date"] <= case["decision_date"]]
    assert evidence(case) == ev


def test_anomalous_data_stays_serializable_and_blocks_plan():
    case = deepcopy(CASES["normal-fill"])
    case["bars"][0]["volume"] = float("nan")
    ev = evidence(case)
    assert json.dumps(ev, allow_nan=False)
    assert ev["bars"][0]["volume"] is None
    assert not ev["metrics"]["data_valid"]
    assert "data_validity" in _failed(validate_plan(_plan(case), ev))


def test_invalid_fixture_is_blocked_without_exposing_future_trade_events():
    case = CASES["invalid-data"]
    ev = evidence(case)
    assert not ev["metrics"]["data_valid"]
    assert any("OHLC" in issue for issue in ev["metrics"]["data_issues"])
    assert _failed(validate_plan(_plan(case), ev)) == {"data_validity"}
    output = _run("invalid-data")
    assert output["status"] == "blocked"
    assert [event["type"] for event in output["events"]] == ["blocked"]
    assert output["metrics"]["ending_cash"] == 100000


def test_no_decision_day_or_insufficient_history_is_not_eligible():
    case = deepcopy(CASES["normal-fill"])
    case["bars"] = [row for row in case["bars"][8:] if row["date"] != case["decision_date"]]
    ev = evidence(case)
    assert not ev["metrics"]["data_valid"]
    assert len(ev["metrics"]["data_issues"]) >= 2


@pytest.mark.parametrize("overrides", [{"live_order": True}, {"position_pct": True}, {"entry_high": float("inf")}, {"stop_loss": "9.8"}])
def test_plan_rejects_unknown_fields_and_invalid_numeric_types(overrides):
    with pytest.raises(ValidationError):
        _plan(CASES["normal-fill"], **overrides)


@pytest.mark.parametrize(("overrides", "failure_code"), [
    ({"evidence_ids": []}, "evidence_refs"),
    ({"evidence_ids": ["normal-fill:bar:2026-10-26"]}, "evidence_refs"),
    ({"evidence_ids": ["delayed-exit:metrics"]}, "evidence_refs"),
    ({"evidence_ids": ["normal-fill:metrics", "normal-fill:metrics"]}, "evidence_refs"),
    ({"position_pct": 20.01}, "position_cap"),
    ({"position_pct": 0}, "position_cap"),
    ({"stop_loss": 10}, "price_order"),
    ({"take_profit": 10.1}, "price_order"),
    ({"entry_high": 9.9}, "price_order"),
    ({"entry_high": None}, "required_parameters"),
    ({"action": "hold"}, "hold_no_trade"),
    ({"rationale": "   "}, "rationale"),
])
def test_risk_gates_fail_closed_even_if_passed_a_forged_validation(overrides, failure_code):
    case = CASES["normal-fill"]
    plan = _plan(case, **overrides)
    validation = validate_plan(plan, evidence(case))
    assert not validation["ok"]
    assert failure_code in _failed(validation)
    assert replay(case, plan, {"ok": True})["status"] == "blocked"


def test_valid_hold_does_not_attempt_an_order():
    case = CASES["normal-fill"]
    plan = Plan(action="hold", rationale="演示观望。", evidence_ids=["normal-fill:metrics"])
    validation = validate_plan(plan, evidence(case))
    result = replay(case, plan, validation)
    assert validation["ok"] and result["status"] == "hold"
    assert [event["type"] for event in result["events"]] == ["hold"]
    assert not result["metrics"]["is_open"]


def test_normal_trade_uses_100_share_lots_fees_and_only_t_plus_one_exit():
    output = _run("normal-fill")
    assert output["status"] == "completed"
    buy, restriction, sell = output["events"]
    assert (buy["type"], buy["date"], buy["price"]) == ("buy", "2026-10-26", 10.04)
    assert restriction["type"] == "t_plus_one"
    assert (sell["type"], sell["date"], sell["price"]) == ("sell", "2026-10-27", 10.65)
    m = output["metrics"]
    assert m["shares"] == 900 and m["open_shares"] == 0
    assert m["fees"] == 14.79 and m["realized_pnl"] == 534.21
    assert m["ending_cash"] == m["ending_equity"] == 100534.21
    assert m["holding_sessions"] == 1 and not m["is_open"]


def test_next_session_limit_up_is_not_filled_even_when_inside_plan_range():
    output = _run("limit-up-no-fill", entry_high=11.1, take_profit=11.5)
    assert output["status"] == "no_fill"
    assert len(output["events"]) == 1 and output["events"][0]["price"] == 11
    assert "涨停" in output["reason"]
    assert output["metrics"]["shares"] == 0 and output["metrics"]["fees"] == 0


def test_price_outside_range_does_not_get_a_later_second_attempt():
    output = _run("normal-fill", entry_high=10.0)
    assert output["status"] == "no_fill"
    assert "不在" in output["reason"]
    assert [event["date"] for event in output["events"]] == ["2026-10-26"]


def test_cash_allocation_includes_fees_when_rounding_to_lots():
    assert _run("normal-fill", position_pct=1.004)["status"] == "no_fill"
    output = _run("normal-fill", position_pct=1.01)
    assert output["status"] == "completed"
    assert output["metrics"]["shares"] == 100


def test_upper_allocation_boundary_is_inclusive():
    case = CASES["normal-fill"]
    plan = _plan(case, position_pct=20)
    assert validate_plan(plan, evidence(case))["ok"]
    output = _run("normal-fill", position_pct=20)
    assert output["metrics"]["shares"] == 1900


def test_intraday_high_does_not_trigger_exit_and_max_holding_counts_later_sessions():
    target_after_high = _run("normal-fill", take_profit=10.67)
    assert target_after_high["events"][2]["type"] == "monitor"
    assert target_after_high["events"][-1]["date"] == "2026-10-28"
    duration_exit = _run("normal-fill", take_profit=11.5)
    assert duration_exit["events"][-1]["date"] == "2026-10-29"
    assert duration_exit["metrics"]["holding_sessions"] == 3
    assert "最长持有期" in duration_exit["reason"]


def test_suspended_and_limit_down_exits_are_deferred_to_valid_close():
    output = _run("delayed-exit")
    assert output["status"] == "completed"
    assert [event["type"] for event in output["events"]] == ["buy", "t_plus_one", "exit_deferred", "exit_deferred", "sell"]
    assert output["events"][2]["price"] is None
    assert output["events"][3]["price"] == 9.05
    assert output["events"][-1]["date"] == "2026-10-29"
    assert output["events"][-1]["price"] == 9.9
    assert "保护价" in output["reason"]


def test_incomplete_replay_keeps_position_and_separates_unrealized_pnl():
    case = deepcopy(CASES["delayed-exit"])
    case["bars"].pop()
    plan = _plan(case)
    output = replay(case, plan, validate_plan(plan, evidence(case)))
    assert output["status"] == "pending"
    assert output["metrics"]["is_open"] and output["metrics"]["open_shares"] > 0
    assert output["metrics"]["mark_price"] == 9.05
    assert output["metrics"]["realized_pnl"] == 0
    assert output["metrics"]["unrealized_pnl"] < 0


def test_future_price_data_anomaly_is_not_used_for_an_exit():
    case = deepcopy(CASES["normal-fill"])
    case["bars"][-3]["high"] = 1
    plan = _plan(case)
    validation = validate_plan(plan, evidence(case))
    assert validation["ok"]
    output = replay(case, plan, validation)
    assert output["status"] == "pending"
    assert output["metrics"]["mark_price"] == 9.72
    assert output["events"][-1]["type"] == "exit_deferred"
