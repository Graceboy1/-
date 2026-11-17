"""Individual known facts are not erased by a different missing value.

All values below are disposable synthetic test evidence, not real company data.
"""
from copy import deepcopy
import json

import pytest

from server import seat_buffett as buffett


def case():
    return deepcopy(buffett.CASES["buffett-value"])


def details(value):
    return buffett._fundamentals(value, buffett._day(value["decision_date"]), 20)


def passed(value):
    return {item["code"]: item["passed"] for item in value["checks"]}


def test_missing_manual_confirmation_does_not_erase_independent_financial_checks():
    value = case()
    value.pop("business_assessment")
    result = details(value)
    checks = passed(result)
    assert result["status"] == "insufficient"
    assert result["financial_valid"] is True and result["assessment_valid"] is False
    assert all(checks[key] for key in ("history", "profit", "cashflow", "roe", "leverage", "margin"))
    assert not checks["circle"] and not checks["moat"] and not result["eligible"]
    assert buffett.make_plan({"fundamentals": result, "metrics": {"data_valid": True}}).action == "hold"


@pytest.mark.parametrize("missing", ["maintenance_capex", "shares_outstanding", "operating_cashflow"])
def test_missing_cashflow_component_does_not_erase_known_profit_roe_and_debt(missing):
    value = case()
    value["financial_reports"][-1][missing] = None
    result = details(value)
    checks = passed(result)
    assert all(checks[key] for key in ("profit", "roe", "leverage"))
    assert not checks["history"] and not checks["cashflow"] and not checks["margin"]
    assert result["valuation"]["conservative_value"] is None and not result["eligible"]
    assert missing not in "；".join(result["issues"])


@pytest.mark.parametrize("missing,failed", [("net_profit", "profit"), ("roe_pct", "roe"), ("debt_to_equity", "leverage")])
def test_each_quality_field_is_independently_required(missing, failed):
    value = case()
    value["financial_reports"][-1][missing] = None
    result = details(value)
    checks = passed(result)
    assert not checks[failed] and not checks["history"] and not checks["margin"]
    assert all(checks[key] for key in {"profit", "roe", "leverage"} - {failed})
    assert result["valuation"]["conservative_value"] is None and not result["eligible"]


@pytest.mark.parametrize("verified", [None, False, "true", 1])
def test_original_number_matches_without_pit_review_cannot_enable_history_dcf_or_trade(verified):
    value = case()
    value["require_point_in_time"] = True
    for report in value["financial_reports"]:
        report["point_in_time_verified"] = verified
    result = details(value)
    checks = passed(result)
    assert all(checks[key] for key in ("profit", "roe", "leverage"))
    assert not checks["history"] and not checks["cashflow"] and not checks["margin"]
    assert result["status"] == "insufficient" and result["financial_valid"] is False
    assert result["valuation"]["conservative_value"] is None and not result["eligible"]
    assert "历史更正版本仍待核验" in "；".join(result["issues"])


def test_complete_verified_pit_and_manual_records_retain_exact_original_economics():
    original = details(case())
    value = case()
    value["require_point_in_time"] = True
    for report in value["financial_reports"]:
        report["point_in_time_verified"] = True
    result = details(value)
    assert result["financial_valid"] and result["assessment_valid"] and result["eligible"]
    assert result["valuation"] == original["valuation"]
    assert passed(result) == passed(original)
    assert result["valuation"]["buy_ceiling"] == 22.12


@pytest.mark.parametrize("alteration", ["gap", "missing_year", "stale", "same_day", "future"])
def test_bad_history_cannot_borrow_older_or_future_reports_for_independent_checks(alteration):
    value = case()
    if alteration == "gap":
        value["financial_reports"][1].update(period_end="2021-12-31", available_on="2022-04-20")
    elif alteration == "missing_year":
        value["financial_reports"].pop()
    elif alteration == "stale":
        for report, year in zip(value["financial_reports"], (2022, 2023, 2024)):
            report.update(period_end=f"{year}-12-31", available_on=f"{year + 1}-04-20")
    else:
        value["financial_reports"][-1]["available_on"] = value["decision_date"] if alteration == "same_day" else "2026-11-01"
    result = details(value)
    assert result["status"] == "insufficient" and not result["financial_valid"]
    assert not any(passed(result)[key] for key in ("history", "profit", "cashflow", "roe", "leverage", "margin"))
    assert not result["eligible"] and result["valuation"]["conservative_value"] is None


@pytest.mark.parametrize("alteration", ["duplicate", "empty_string", "nonfinite", "bad_shares", "bad_assessment", "bad_pit_requirement"])
def test_malformed_record_keeps_original_whole_evidence_block(alteration):
    value = case()
    if alteration == "duplicate":
        value["financial_reports"].append(deepcopy(value["financial_reports"][-1]))
    elif alteration == "empty_string":
        value["financial_reports"][-1]["maintenance_capex"] = ""
    elif alteration == "nonfinite":
        value["financial_reports"][-1]["net_profit"] = float("nan")
    elif alteration == "bad_shares":
        value["financial_reports"][-1]["shares_outstanding"] = 0
    elif alteration == "bad_assessment":
        value["business_assessment"]["moat_supported"] = "yes"
    else:
        value["require_point_in_time"] = "true"
    result = details(value)
    assert result["status"] == "invalid" and not result["eligible"]
    assert not any(passed(result).values())
    assert result["valuation"]["conservative_value"] is None
    assert json.dumps(result, allow_nan=False)


def test_provenance_is_known_field_bounded_whitelist_and_does_not_grant_pit():
    value = case()
    value["require_point_in_time"] = True
    proof = {"source_url": "https://static.cninfo.com.cn/finalpage/2026-04-20/12345678.PDF",
             "document_sha256": "a" * 64, "page": 8, "quote": "公开字段" * 300, "unit": "百万元",
             "scale": 1_000_000, "value": 270, "verification_status": "matched_original",
             "private_vendor_token": "DO-NOT-EXPOSE"}
    value["financial_reports"][-1]["field_evidence"] = {"net_profit": proof, "private_token": proof,
                                                        "roe_pct": {**proof, "source_url": "javascript:alert(1)"}}
    result = details(value)
    public = result["reports"][-1]["field_evidence"]
    assert set(public) == {"net_profit"} and len(public["net_profit"]["quote"]) == 500
    assert public["net_profit"]["page"] == 8 and public["net_profit"]["document_sha256"] == "a" * 64
    assert "DO-NOT-EXPOSE" not in json.dumps(result) and "javascript" not in json.dumps(result)
    assert result["reports"][-1]["point_in_time_verified"] is False
    assert not result["eligible"] and result["valuation"]["conservative_value"] is None
