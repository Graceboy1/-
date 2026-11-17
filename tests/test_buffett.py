from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
import json

import pytest

from server import seat_buffett as buffett
from server.domain import replay
from test_api_agent import api


def snapshot(case_id="buffett-value", case=None):
    return buffett.seat_evidence(case if case is not None else buffett.CASES[case_id])


def run(case_id="buffett-value", *, case=None, plan_changes=None, validation=None):
    case = case if case is not None else buffett.CASES[case_id]
    ev = snapshot(case=case)
    plan = buffett.make_plan(ev)
    if plan_changes:
        plan = plan.model_copy(update=plan_changes)
    return replay(case, plan, validation if validation is not None else buffett.check_plan(plan, ev),
                  policy=buffett.POLICY, validator=buffett.check_plan,
                  evidence_reader=buffett.seat_evidence, include_bars=True)


@pytest.mark.parametrize("case_id,status,action,allowed,outcome", [
    ("buffett-value", "valid", "simulate", True, "pending"),
    ("buffett-expensive", "valid", "hold", True, "hold"),
    ("buffett-cashflow", "valid", "hold", True, "hold"),
    ("buffett-missing", "insufficient", "hold", True, "hold"),
    ("buffett-invalid", "invalid", "hold", False, "blocked"),
    ("buffett-gap", "valid", "simulate", True, "no_fill"),
])
def test_value_cases_are_independent_and_conservative(case_id, status, action, allowed, outcome):
    ev = snapshot(case_id)
    plan = buffett.make_plan(ev)
    assert ev["fundamentals"]["status"] == status
    assert plan.action == action
    assert buffett.check_plan(plan, ev)["ok"] is allowed
    assert run(case_id)["status"] == outcome
    assert plan.stop_loss is None and plan.take_profit is None


def test_per_share_cashflow_and_dcf_use_consistent_million_units():
    ev = snapshot()
    details = ev["fundamentals"]
    value = details["valuation"]
    assert value["owner_cashflow_proxy_per_share"] == 2.7
    assert value["base_cashflow_proxy_per_share"] == pytest.approx((2.7 + 3.0 + 3.2) / 3)
    dcf = sum(Decimal("2.7") / Decimal("1.1") ** year for year in range(1, 6))
    dcf += Decimal("2.7") * Decimal("1.02") / Decimal("0.08") / Decimal("1.1") ** 5
    ceiling = (dcf * Decimal("0.7")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
    assert value["conservative_value"] == round(float(dcf), 4)
    assert value["buy_ceiling"] == float(ceiling) == 22.12
    assert value["base_value"] > value["conservative_value"]
    assert value["margin_of_safety_pct"] > 30
    assert value["forecast_years"] == 5
    assert value["discount_rate_pct"] == 10 and value["terminal_growth_pct"] == 2
    assert buffett.make_plan(ev).entry_high == value["buy_ceiling"]
    assert buffett.make_plan(ev).position_pct == 10


def test_future_financials_future_prices_and_private_fields_do_not_leak():
    case = deepcopy(buffett.CASES["buffett-value"])
    original = snapshot(case=case)
    for report in case["financial_reports"]:
        report["private_vendor_token"] = "DO-NOT-EXPOSE"
    case["financial_reports"].append({"available_on": "2026-11-01", "period_end": "not-a-period",
                                      "net_profit": float("nan"), "future_label": "FUTURE-THESIS"})
    case["business_assessment"]["future_thesis"] = "DO-NOT-EXPOSE"
    case["description"] = "FORWARD-OUTCOME"
    for bar in case["bars"]:
        if bar["date"] > case["decision_date"]:
            bar.update(close=999.0, private_signal="FUTURE-THESIS")
    actual = snapshot(case=case)
    assert actual == original
    serialized = json.dumps(actual, allow_nan=False)
    assert "DO-NOT-EXPOSE" not in serialized and "FUTURE-THESIS" not in serialized
    assert all(row["available_on"] <= case["decision_date"] and row["period_end"] <= case["decision_date"] for row in actual["fundamentals"]["reports"])
    assert all(bar["date"] <= case["decision_date"] for bar in actual["bars"])


def test_missing_latest_field_does_not_fall_back_to_older_good_years():
    case = deepcopy(buffett.CASES["buffett-missing"])
    for year in (2021, 2022):
        older = deepcopy(case["financial_reports"][0])
        older.update(period_end=f"{year}-12-31", available_on=f"{year + 1}-04-20")
        case["financial_reports"].insert(0, older)
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "insufficient"
    assert ev["fundamentals"]["reports"][-1]["maintenance_capex"] is None
    assert ev["fundamentals"]["valuation"]["conservative_value"] is None
    assert buffett.make_plan(ev).action == "hold"
    assert run(case=case)["status"] == "hold"


@pytest.mark.parametrize("field,value", [
    ("operating_cashflow", float("nan")), ("operating_cashflow", float("inf")),
    ("net_profit", True), ("roe_pct", "18.0"), ("roe_pct", 1001),
    ("maintenance_capex", -1), ("debt_to_equity", -0.1),
    ("shares_outstanding", 0), ("shares_outstanding", 1e30),
])
def test_malformed_public_financials_block_even_a_forged_pass(field, value):
    case = deepcopy(buffett.CASES["buffett-value"])
    case["financial_reports"][-1][field] = value
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "invalid"
    assert json.dumps(ev, allow_nan=False)
    assert not buffett.check_plan(buffett.make_plan(ev), ev)["ok"]
    result = run(case=case, validation={"ok": True})
    assert result["status"] == "blocked" and "replay_bars" not in result


@pytest.mark.parametrize("alteration", ["missing", "gap", "stale", "unpublished"])
def test_absent_incomplete_stale_or_unpublished_history_is_only_observation(alteration):
    case = deepcopy(buffett.CASES["buffett-value"])
    if alteration == "missing":
        del case["financial_reports"]
    elif alteration == "gap":
        case["financial_reports"][1].update(period_end="2021-12-31", available_on="2022-04-20")
    elif alteration == "stale":
        for index, year in enumerate((2022, 2023, 2024)):
            case["financial_reports"][index].update(period_end=f"{year}-12-31", available_on=f"{year + 1}-04-20")
    else:
        case["financial_reports"][-1]["available_on"] = "2026-11-01"
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "insufficient"
    assert run(case=case)["status"] == "hold"


@pytest.mark.parametrize("alteration", ["duplicate", "bad_availability", "bad_period", "early_publication", "quarter"])
def test_bad_publication_metadata_and_duplicate_reports_are_not_guessed(alteration):
    case = deepcopy(buffett.CASES["buffett-value"])
    latest = case["financial_reports"][-1]
    if alteration == "duplicate":
        case["financial_reports"].append(deepcopy(latest))
    elif alteration == "bad_availability":
        latest["available_on"] = "2026-02-30"
    elif alteration == "bad_period":
        latest["period_end"] = "2025-02-30"
    elif alteration == "early_publication":
        latest["available_on"] = "2025-12-01"
    else:
        latest["period_end"] = "2025-09-30"
    assert snapshot(case=case)["fundamentals"]["status"] == "invalid"
    assert run(case=case)["status"] == "blocked"


@pytest.mark.parametrize("field,value", [("net_profit", 0), ("net_profit", -10), ("roe_pct", 14.99), ("debt_to_equity", 0.5001)])
def test_financial_weakness_is_valid_evidence_but_not_a_buy(field, value):
    case = deepcopy(buffett.CASES["buffett-value"])
    case["financial_reports"][-1][field] = value
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "valid" and not ev["fundamentals"]["eligible"]
    assert run(case=case)["status"] == "hold"


@pytest.mark.parametrize("field", ["within_circle", "moat_supported"])
def test_business_confirmation_is_manual_not_inferred_from_price(field):
    case = deepcopy(buffett.CASES["buffett-value"])
    case["business_assessment"][field] = False
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "valid"
    assert not ev["fundamentals"]["eligible"]
    assert run(case=case)["status"] == "hold"


def test_future_assessment_note_is_not_visible_or_used():
    case = deepcopy(buffett.CASES["buffett-value"])
    case["business_assessment"].update(available_on="2026-11-01", note="FUTURE-BUSINESS-NOTE")
    ev = snapshot(case=case)
    assert ev["fundamentals"]["assessment"] is None
    assert ev["fundamentals"]["status"] == "insufficient"
    assert "FUTURE-BUSINESS-NOTE" not in json.dumps(ev)
    assert run(case=case)["status"] == "hold"


@pytest.mark.parametrize("record", ["financial", "assessment"])
def test_date_only_same_day_publications_cannot_prove_preclose_availability(record):
    case = deepcopy(buffett.CASES["buffett-value"])
    if record == "financial":
        case["financial_reports"][-1]["available_on"] = case["decision_date"]
        case["financial_reports"][-1]["operating_cashflow"] = 987654.0
    else:
        case["business_assessment"].update(available_on=case["decision_date"], note="AFTER-CLOSE-NOTE")
    ev = snapshot(case=case)
    assert ev["fundamentals"]["status"] == "insufficient"
    assert all(row["available_on"] < case["decision_date"] for row in ev["fundamentals"]["reports"])
    assert "987654" not in json.dumps(ev) and "AFTER-CLOSE-NOTE" not in json.dumps(ev)
    assert run(case=case)["status"] == "hold"


def test_value_replay_has_no_artificial_short_term_exit_or_realized_profit():
    result = run()
    assert result["status"] == "pending" and result["metrics"]["is_open"]
    assert result["metrics"]["holding_sessions"] == 19
    assert result["metrics"]["max_holding_sessions"] is None
    assert result["metrics"]["realized_pnl"] == 0
    assert result["metrics"]["open_shares"] > 0
    assert result["metrics"]["unrealized_pnl"] != 0
    assert {event["type"] for event in result["events"]} == {"buy", "t_plus_one", "monitor"}
    assert len(result["replay_bars"]) == 20
    assert "尚未实现" in result["reason"]


@pytest.mark.parametrize("changes", [
    {"entry_high": 22.13}, {"position_pct": 10.1}, {"entry_low": 0.02},
    {"stop_loss": 19.0}, {"take_profit": 30.0}, {"action": "hold"},
    {"evidence_ids": ["buffett-value:metrics"]},
])
def test_frozen_economic_fields_and_financial_citations_cannot_be_overridden(changes):
    assert run(plan_changes=changes, validation={"ok": True})["status"] == "blocked"


def test_replay_rechecks_original_financials_instead_of_trusting_a_frozen_pass():
    case = deepcopy(buffett.CASES["buffett-value"])
    ev = snapshot(case=case)
    plan = buffett.make_plan(ev)
    validation = buffett.check_plan(plan, ev)
    case["financial_reports"][-1]["net_profit"] = -1
    result = replay(case, plan, validation, policy=buffett.POLICY,
                    validator=buffett.check_plan, evidence_reader=buffett.seat_evidence, include_bars=True)
    assert result["status"] == "blocked" and "replay_bars" not in result


def test_value_api_is_offline_only_and_does_not_mix_livermore_cases():
    response = api("POST", "/analyze", json={"seat_id": "buffett", "case_id": "buffett-value", "mode": "offline"})
    assert response.status_code == 200, response.text
    frozen = response.json()
    assert frozen["seat"]["id"] == "buffett" and frozen["seat"]["modes"] == ["offline"]
    assert frozen["execution_policy"]["max_holding_sessions"] is None
    assert frozen["execution_policy"]["uses_price_thresholds"] is False
    assert frozen["evidence"]["fundamentals"]["eligible"]
    assert "replay_bars" not in frozen and "events" not in frozen
    result = api("POST", "/replay", json={"run_id": frozen["run_id"], "include_bars": True})
    assert result.status_code == 200 and result.json()["status"] == "pending"
    assert result.json()["seat_id"] == "buffett"
    assert api("POST", "/analyze", json={"seat_id": "buffett", "case_id": "buffett-value", "mode": "ai"}).status_code == 422
    assert api("POST", "/analyze", json={"seat_id": "buffett", "case_id": "livermore-normal"}).status_code == 404
    catalog = api("GET", "/cases?source=examples").json()
    public = [case for case in catalog["cases"] if case["seat_id"] == "buffett"]
    assert len(public) == 6 and all("financial_reports" not in case and "bars" not in case for case in public)
