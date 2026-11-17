"""Generated public-archive fixtures; no account files, network, or real PDFs."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os

import pytest

from server import buffett_verified as verified, historical_fundamentals as finance
from server import historical_seats, paper_market, seat_buffett

CODE = "600519"


@pytest.fixture(autouse=True)
def public_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "public-market"))
    verified._pdf_hash.cache_clear()


def _save(value):
    root = verified.directory()
    root.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    (root / f"{CODE}.json.gz").write_bytes(gzip.compress(raw))
    (root / "index.json").write_text(json.dumps({"schema": verified.SCHEMA, "files": {CODE: hashlib.sha256(raw).hexdigest()}}), encoding="utf-8")


def fixture():
    originals = finance.directory() / "originals"
    originals.mkdir(parents=True)
    docs, rows = {}, []
    for year in (2023, 2024, 2025):
        identity = str(1234500000 + year)
        period, available = f"{year}-12-31", f"{year+1}-04-01"
        raw = f"%PDF generated-unit-test-{year}".encode()
        path = originals / CODE / f"{identity}.pdf"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        url = f"https://static.cninfo.com.cn/finalpage/{available}/{identity}.PDF"
        docs[f"{CODE}:{year}"] = {"code": CODE, "period_end": period, "publication_date": available,
                                   "status": "downloaded", "revision_or_correction": False,
                                   "announcement_id": identity, "file": f"{CODE}/{identity}.pdf",
                                   "source_url": url, "sha256": digest, "size": len(raw)}
        values = {"operating_cashflow": 10, "net_profit": 2, "roe_pct": 18,
                  "total_liabilities": 4, "total_equity": 20, "debt_to_equity": .2,
                  "shares_outstanding": 100, "reported_capex": 1}
        proofs = {}
        for field, value in values.items():
            unit = "%" if field == "roe_pct" else "倍" if field == "debt_to_equity" else "百万股" if field == "shares_outstanding" else "百万元"
            proofs[field] = {"page": 10, "quote": f"Generated {field}: {value}", "unit": unit,
                             "scale": 1, "reported_value": value, "value": value,
                             "document_sha256": digest, "source_url": url, "verification_status": "matched_original"}
        rows.append({"period_end": period, "available_on": available, "point_in_time_verified": False,
                     "maintenance_capex": None, **values, "field_evidence": proofs, "issues": []})
    (originals / "manifest.json").write_text(json.dumps({"schema": "buffett-originals/v1", "decision_date": finance.CUTOFF, "reports": docs}), encoding="utf-8")
    value = {"schema": verified.SCHEMA, "code": CODE, "decision_date": finance.CUTOFF,
             "verified_reports": rows, "point_in_time_verified": False, "business_assessment": None}
    _save(value)
    return value


def test_missing_reader_does_not_create_data_or_approve_anything():
    assert verified.load_verified(CODE)["status"] == "missing"
    assert not verified.directory().exists()


def test_admitted_original_fields_keep_pit_and_human_unapproved():
    fixture()
    value = verified.load_verified(CODE)
    assert value["status"] == "partial_verified" and len(value["verified_reports"]) == 3
    assert value["point_in_time_verified"] is False and value["business_assessment"] is None
    for row in value["verified_reports"]:
        assert row["maintenance_capex"] is None and row["point_in_time_verified"] is False
        assert row["shares_outstanding"] == 100 and len(row["field_evidence"]) == 8


def test_explicit_hundred_million_yuan_and_large_raw_yuan_are_normalized_not_rejected():
    value = fixture()
    first = value["verified_reports"][0]["field_evidence"]["net_profit"]
    first.update(unit="亿元", scale=100, reported_value=.02)
    second = value["verified_reports"][1]
    second["net_profit"] = 2_000_000
    second["field_evidence"]["net_profit"].update(unit="元", scale=1e-6, reported_value=2_000_000_000_000, value=2_000_000)
    _save(value)
    reports = verified.load_verified(CODE)["verified_reports"]
    assert reports[0]["net_profit"] == 2 and reports[1]["net_profit"] == 2_000_000


@pytest.mark.parametrize("code", ["../600519", "688001", "300001", "920001", None])
def test_invalid_security_never_selects_another_profile(code):
    fixture()
    assert verified.load_verified(code)["verified_reports"] == []


@pytest.mark.parametrize("kind", ["no_proof", "wrong_value", "wrong_unit", "wrong_scale", "wrong_hash", "wrong_source", "wrong_page", "long_quote", "fake_ratio", "fake_maintenance", "fake_human", "fake_pit", "duplicate"])
def test_tampered_or_unproven_financial_fields_fail_closed(kind):
    value = fixture()
    row = value["verified_reports"][0]
    proof = row["field_evidence"]["net_profit"]
    if kind == "no_proof":
        del row["field_evidence"]["net_profit"]
    elif kind == "wrong_value":
        row["net_profit"] = 99
    elif kind == "wrong_unit":
        proof["unit"] = "股"
    elif kind == "wrong_scale":
        proof["scale"] = 1000
    elif kind == "wrong_hash":
        proof["document_sha256"] = "0" * 64
    elif kind == "wrong_source":
        proof["source_url"] = "https://bad.example/report.pdf"
    elif kind == "wrong_page":
        proof["page"] = 0
    elif kind == "long_quote":
        proof["quote"] = "x" * 501
    elif kind == "fake_ratio":
        row["total_equity"] = None
        row["field_evidence"].pop("total_equity")
    elif kind == "fake_maintenance":
        row["maintenance_capex"] = 0
    elif kind == "fake_human":
        value["business_assessment"] = {"moat_supported": True}
    elif kind == "fake_pit":
        row["point_in_time_verified"] = True
    else:
        value["verified_reports"].append(copy.deepcopy(row))
    _save(value)
    assert verified.load_verified(CODE)["status"] == "invalid"


def test_original_pdf_change_invalidates_even_a_previously_cached_digest():
    fixture()
    assert verified.load_verified(CODE)["status"] == "partial_verified"
    path = next((finance.directory() / "originals" / CODE).glob("*.pdf"))
    previous = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + b"x")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    assert verified.load_verified(CODE)["status"] == "invalid"


def test_artifact_hash_mismatch_does_not_fall_back_to_vendor_numbers():
    fixture()
    (verified.directory() / f"{CODE}.json.gz").write_bytes(gzip.compress(b"{}"))
    assert verified.load_verified(CODE)["verified_reports"] == []


def test_cutoff_never_discloses_same_day_original_report():
    fixture()
    rows = verified.load_verified(CODE, "2026-04-01")["verified_reports"]
    assert len(rows) == 2 and all(row["available_on"] < "2026-04-01" for row in rows)


def test_partial_original_rows_reach_real_seat_checks_but_cannot_generate_a_buy(monkeypatch):
    fixture()
    monkeypatch.setattr(historical_seats, "_financial_profile", lambda _code: {"financial_reports": [], "reports": [], "business_assessment": None})
    snapshot = {"decision_date": finance.CUTOFF, "metrics": {"last_close": 10}, "evidence_ids": [], "limitations": []}
    historical_seats._value_evidence({"id": "history-test", "symbol": CODE}, snapshot)
    details = snapshot["fundamentals"]
    checks = {row["code"]: row["passed"] for row in details["checks"]}
    assert len(details["reports"]) == 3 and checks["profit"] and checks["roe"] and checks["leverage"]
    assert not checks["history"] and not checks["cashflow"] and not checks["circle"] and not checks["moat"]
    assert details["eligible"] is False and details["valuation"]["conservative_value"] is None
    assert details["original_verification"]["field_count"] == 24


def test_main_summary_uses_real_saved_counts_and_never_claims_ready():
    fixture()
    (finance.directory() / "coverage.json").write_text(json.dumps({"schema": "financial-coverage/v1", "decision_date": finance.CUTOFF,
                                                                "eligible_stocks": 3049, "annual_reference_rows": 9147,
                                                                "complete_three_year_reference_stocks": 3015}), encoding="utf-8")
    summary = verified.integration_summary()
    assert summary["scope"] == "whole_universe" and summary["reference_stocks"] == 3049
    assert summary["verified_stocks"] == 1 and summary["verified_report_count"] == 3 and summary["verified_field_count"] == 24
    assert summary["strict_pit_stocks"] == 0 and summary["manual_confirmation_available"] is False


def test_buffett_decision_fingerprint_changes_with_financial_facts(monkeypatch):
    monkeypatch.setattr(historical_seats, "propose", lambda *_args: type("Plan", (), {"rationale": "观望", "action": "hold", "model_dump": lambda self: {"action": "hold"}})())
    one = {"metrics": {"data_valid": True}, "fundamentals": {"reports": [{"net_profit": 2}], "issues": []}}
    two = copy.deepcopy(one)
    two["fundamentals"]["reports"][0]["net_profit"] = 3
    assert paper_market._plan({}, "buffett", one)["financial_evidence_hash"] != paper_market._plan({}, "buffett", two)["financial_evidence_hash"]
