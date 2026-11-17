"""Annual references use generated reports/temp files; no live finance fetches."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json

import httpx
import pytest

from server import historical_fundamentals as finance
from tools import download_historical_fundamentals as download


CODE = "600519"
RETRIEVED = "2026-11-16T08:00:00+00:00"


def row(table, year=2025, **changes):
    return {"SECURITY_CODE": CODE, "SECURITY_NAME_ABBR": "真实企业测试资料",
            finance.TABLES[table]["date_field"]: f"{year}-12-31 00:00:00",
            "NOTICE_DATE": f"{year + 1}-04-01 00:00:00",
            "NETCASH_OPERATE": 20_000_000, "CONSTRUCT_LONG_ASSET": 3_000_000,
            "PARENT_NETPROFIT": 15_000_000, "WEIGHTAVG_ROE": 18.5,
            "TOTAL_LIABILITIES": 4_000_000, "TOTAL_EQUITY": 20_000_000,
            **changes}


def tables():
    return {key: [row(key, year) for year in (2023, 2024, 2025)] for key in finance.TABLES}


def payload(*rows):
    return {"success": True, "result": {"data": list(rows)}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "dated-archive"))

    def deny_http(_self, _request):
        raise AssertionError("Financial test attempted live HTTP")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_http)


def save_fixture(code=CODE, value=None):
    return download.save(code, value or tables(), [], retrieved_at=RETRIEVED)


def test_normalized_annual_reference_uses_millions_and_explicit_unverifiable_gaps():
    p = finance.normalize(CODE, tables(), retrieved_at=RETRIEVED)
    assert p["status"] == "reference_only"
    assert p["point_in_time_verified"] is False
    assert p["financial_reports"] == [] and p["business_assessment"] is None
    assert [r["period_end"] for r in p["reports"]] == ["2023-12-31", "2024-12-31", "2025-12-31"]
    r = p["reports"][-1]
    assert r["operating_cashflow"] == 20 and r["reported_capex"] == 3 and r["net_profit"] == 15
    assert r["roe_pct"] == 18.5 and r["debt_to_equity"] == .2
    assert r["maintenance_capex"] is None and r["shares_outstanding"] is None
    assert r["conservative_total_capex_proxy"] == 17
    assert r["amount_unit"] == "million_yuan" and r["shares_unit"] == "million_shares"
    assert r["point_in_time_verified"] is False and r["restatement_status"] == "not_verified"
    assert "not interest-bearing debt" in r["debt_basis"]
    assert "不能代替确认" in " ".join(p["limitations"])


def test_data_coverage_separates_complete_reference_downloads_from_strict_gaps():
    profile = finance.normalize(CODE, tables(), retrieved_at=RETRIEVED)
    coverage = profile["data_coverage"]
    assert coverage["expected_years"] == ["2023-12-31", "2024-12-31", "2025-12-31"]
    assert coverage["reference_year_count"] == 3
    items = {item["key"]: item for item in coverage["items"]}
    for key in ("annual_reports", "operating_cashflow", "net_profit", "roe_pct", "debt_to_equity", "reported_capex"):
        assert items[key]["status"] == "reference_only"
        assert items[key]["available_count"] == items[key]["required_count"] == 3
    for key in ("shares_outstanding", "maintenance_capex"):
        assert items[key]["status"] == "missing" and items[key]["available_count"] == 0
        assert items[key]["required_count"] == 3
    assert items["point_in_time"]["status"] == "unverified"
    assert items["point_in_time"]["available_count"] == 0
    for key in ("within_circle", "moat_supported"):
        assert items[key]["status"] == "manual_required"
        assert items[key]["available_count"] == 0 and items[key]["required_count"] == 1
    assert profile["financial_reports"] == [] and profile["business_assessment"] is None
    assert profile["point_in_time_verified"] is False


def test_data_coverage_counts_negative_or_weak_financials_as_available_not_as_passes():
    value = tables()
    for name, rows in value.items():
        for record in rows:
            record.update(NETCASH_OPERATE=-2_000_000, PARENT_NETPROFIT=-1_000_000,
                          WEIGHTAVG_ROE=1.0, TOTAL_LIABILITIES=200_000_000)
    coverage = finance.normalize(CODE, value, retrieved_at=RETRIEVED)["data_coverage"]
    assert coverage["reference_year_count"] == 3
    items = {item["key"]: item for item in coverage["items"]}
    for key in ("operating_cashflow", "net_profit", "roe_pct", "debt_to_equity"):
        assert items[key]["status"] == "reference_only" and items[key]["available_count"] == 3
    assert all(item["status"] != "passed" for item in items.values())


def test_partial_tables_or_null_reference_values_report_exact_missing_counts():
    value = tables()
    value["income"] = [record for record in value["income"] if not record["REPORT_DATE"].startswith("2024-")]
    value["cashflow"][-1]["NETCASH_OPERATE"] = None
    coverage = finance.normalize(CODE, value, retrieved_at=RETRIEVED)["data_coverage"]
    assert coverage["reference_year_count"] == 1
    items = {item["key"]: item for item in coverage["items"]}
    for key in ("annual_reports", "operating_cashflow", "net_profit"):
        assert items[key]["status"] == "missing" and items[key]["available_count"] == 2
        assert items[key]["required_count"] == 3
    assert items["roe_pct"]["available_count"] == 3
    assert items["point_in_time"]["available_count"] == 0


def test_absent_profile_has_a_full_missing_coverage_checklist_without_invented_data():
    profile = finance.load_profile(CODE)
    coverage = profile["data_coverage"]
    assert coverage["reference_year_count"] == 0 and len(coverage["expected_years"]) == 3
    assert len(coverage["items"]) == 11
    assert all(item["available_count"] == 0 for item in coverage["items"])
    assert all(item["status"] == ("manual_required" if item["key"] in {"within_circle", "moat_supported"} else "missing")
               for item in coverage["items"])
    assert profile["reports"] == profile["financial_reports"] == []
    assert profile["business_assessment"] is None and not finance.directory().exists()


def test_merged_availability_uses_latest_source_report_publication():
    value = tables()
    value["balance"][-1]["NOTICE_DATE"] = "2026-05-10 00:00:00"
    assert finance.normalize(CODE, value, retrieved_at=RETRIEVED)["reports"][-1]["available_on"] == "2026-05-10"


@pytest.mark.parametrize("notice", ["2026-11-12 00:00:00", "2026-11-13", "2027-01-01", None, "invalid", "2025-01-01"])
def test_same_day_future_missing_or_preperiod_notice_not_admitted(notice):
    assert finance.parse_table(payload(row("cashflow", NOTICE_DATE=notice)), "cashflow", CODE) == []


@pytest.mark.parametrize("update", ["2026-11-12", "2026-11-13", "2027-01-01", "invalid", "2025-01-01"])
def test_postcutoff_or_invalid_restatement_marker_not_admitted(update):
    assert finance.parse_table(payload(row("indicators", UPDATE_DATE=update)), "indicators", CODE) == []


def test_before_cutoff_update_does_not_claim_original_version_verified():
    value = tables()
    value["indicators"][-1]["UPDATE_DATE"] = "2026-11-01 00:00:00"
    p = finance.normalize(CODE, value, retrieved_at=RETRIEVED)
    assert len(p["reports"]) == 3 and p["financial_reports"] == []
    assert p["provenance"]["point_in_time_verified"] is False


@pytest.mark.parametrize("report_date", ["2026-12-31", "2022-12-31", "2025-09-30", "2025-12-31garbage", "2025-13-31", "bad", None])
def test_only_expected_annual_periods_are_references(report_date):
    assert finance.parse_table(payload(row("cashflow", REPORT_DATE=report_date)), "cashflow", CODE) == []


@pytest.mark.parametrize("bad", [None, [], {}, {"success": False, "result": {"data": []}}, {"success": True, "result": None}, {"success": True, "result": {"data": "rows"}}])
def test_invalid_source_shapes_fail_closed(bad):
    with pytest.raises(ValueError):
        finance.parse_table(bad, "cashflow", CODE)


def test_oversized_page_duplicate_year_and_other_security_rejected():
    for value in (payload(*[row("income")] * 21), payload(row("income"), row("income")), payload(row("income", SECURITY_CODE="000858"))):
        with pytest.raises(ValueError):
            finance.parse_table(value, "income", CODE)


@pytest.mark.parametrize("value", [None, True, "20000000", float("nan"), float("inf"), 1e18])
def test_missing_or_invalid_numbers_are_null_not_zero_or_estimates(value):
    data = tables()
    data["cashflow"][-1]["NETCASH_OPERATE"] = value
    assert finance.normalize(CODE, data, retrieved_at=RETRIEVED)["reports"][-1]["operating_cashflow"] is None


def test_invalid_capex_and_zero_equity_do_not_create_proxy_or_ratio():
    data = tables()
    data["cashflow"][-1]["CONSTRUCT_LONG_ASSET"] = -1
    data["balance"][-1]["TOTAL_EQUITY"] = 0
    r = finance.normalize(CODE, data, retrieved_at=RETRIEVED)["reports"][-1]
    assert r["reported_capex"] is None and r["conservative_total_capex_proxy"] is None and r["debt_to_equity"] is None


def test_year_with_undefined_leverage_is_not_labeled_complete_reference():
    data = tables()
    data["balance"][-1]["TOTAL_EQUITY"] = 0
    p = finance.normalize(CODE, data, retrieved_at=RETRIEVED)
    assert p["data_coverage"]["reference_year_count"] == 2
    leverage = next(item for item in p["data_coverage"]["items"] if item["key"] == "debt_to_equity")
    assert leverage["available_count"] == 2 and leverage["status"] == "missing"


@pytest.mark.parametrize("code", ["../600519", "300001", "688001", "920001", "600519.SH", "60051", 600519, None])
def test_invalid_or_excluded_code_does_not_read_cache_or_build_request(code):
    assert finance.load_profile(code)["status"] == "invalid"
    with pytest.raises(ValueError):
        finance.request_params("cashflow", code)


def test_params_have_explicit_allowlist_small_page_and_only_one_code():
    p = finance.request_params("cashflow", CODE)
    assert p["filter"] == '(SECURITY_CODE="600519")'
    assert p["pageSize"] == "20" and p["pageNumber"] == "1"
    assert p["reportName"] == "RPT_DMSK_FN_CASHFLOW" and "NETCASH_OPERATE" in p["columns"]
    with pytest.raises(ValueError):
        finance.request_params("not-a-financial-table", CODE)


def test_reader_does_not_create_missing_directories_or_make_network_request():
    assert finance.load_profile(CODE)["status"] == "missing"
    assert not finance.directory().exists()
    assert finance.profile_catalog() == []


def test_save_and_read_checksums_return_real_references_not_decision_evidence():
    saved = save_fixture()
    assert finance.load_profile(CODE) == saved
    assert finance.profile_catalog() == [{"code": CODE, "status": "reference_only", "report_count": 3}]
    assert finance.load_profile("000858")["status"] == "missing"
    assert finance.load_profile(CODE)["financial_reports"] == []


def test_hash_mismatch_does_not_use_other_or_synthetic_reports():
    save_fixture()
    (finance.directory() / f"{CODE}.json.gz").write_bytes(gzip.compress(b'{}'))
    p = finance.load_profile(CODE)
    assert p["status"] == "invalid" and p["reports"] == [] and p["financial_reports"] == []
    assert finance.profile_catalog() == []


@pytest.mark.parametrize("bad", ["[]", "{}", "null", "not json", json.dumps({"schema": finance.SCHEMA, "files": {CODE: "not-a-hash"}})])
def test_corrupt_indexes_never_return_financials(bad):
    root = finance.directory()
    root.mkdir(parents=True)
    (root / "index.json").write_text(bad, "utf-8")
    assert finance.load_profile(CODE)["status"] == "invalid"
    assert finance.profile_catalog() == []


def test_oversized_index_is_bounded():
    root = finance.directory()
    root.mkdir(parents=True)
    (root / "index.json").write_bytes(b" " * (finance.MAX_INDEX_BYTES + 1))
    assert finance.load_profile(CODE)["status"] == "invalid" and finance.profile_catalog() == []


def test_oversized_decompressed_body_is_bounded():
    save_fixture()
    (finance.directory() / f"{CODE}.json.gz").write_bytes(gzip.compress(b" " * (finance.MAX_FILE_BYTES + 1)))
    assert finance.load_profile(CODE)["status"] == "invalid"


def test_matching_hash_does_not_bypass_security_date_or_schema_validation():
    save_fixture()
    path = finance.directory() / f"{CODE}.json.gz"
    value = json.loads(gzip.decompress(path.read_bytes()))
    value["code"] = "000858"
    raw = json.dumps(value).encode()
    path.write_bytes(gzip.compress(raw))
    index = json.loads((finance.directory() / "index.json").read_bytes())
    index["files"][CODE] = hashlib.sha256(raw).hexdigest()
    (finance.directory() / "index.json").write_text(json.dumps(index), "utf-8")
    assert finance.load_profile(CODE)["status"] == "invalid"


def test_catalog_reads_at_most_forty_validated_profiles(monkeypatch):
    root = finance.directory()
    root.mkdir(parents=True)
    (root / "index.json").write_text(json.dumps({"schema": finance.SCHEMA, "files": {f"000{i:03}": "0" * 64 for i in range(60)}}), "utf-8")
    read = []

    def fake(code):
        read.append(code)
        return {"status": "reference_only", "reports": [{}]}

    monkeypatch.setattr(finance, "load_profile", fake)
    result = finance.profile_catalog()
    assert len(read) == 40 and len(result) == 40


def test_market_sized_index_can_exceed_one_hundred_profiles_but_shortcuts_stay_bounded(monkeypatch):
    save_fixture()
    index_path = finance.directory() / "index.json"
    index = json.loads(index_path.read_bytes())
    index["files"].update({f"000{i:03}": "0" * 64 for i in range(101)})
    index_path.write_text(json.dumps(index), "utf-8")
    assert len(index["files"]) > 100
    assert finance.MAX_PROFILES == 6000 and finance.MAX_INDEX_BYTES == 1_000_000
    assert finance.load_profile(CODE)["data_coverage"]["reference_year_count"] == 3
    calls = []

    def fake(code):
        calls.append(code)
        return {"status": "reference_only", "reports": [{}]}

    monkeypatch.setattr(finance, "load_profile", fake)
    assert len(finance.profile_catalog()) == len(calls) == 40


def test_profile_capacity_is_bounded_without_removing_per_file_hash_or_size_gates():
    save_fixture()
    index_path = finance.directory() / "index.json"
    index = json.loads(index_path.read_bytes())
    index["files"].update({f"{i:06d}": "0" * 64 for i in range(finance.MAX_PROFILES)})
    assert len(index["files"]) == finance.MAX_PROFILES + 1
    index_path.write_text(json.dumps(index), "utf-8")
    assert index_path.stat().st_size < finance.MAX_INDEX_BYTES
    assert finance.load_profile(CODE)["status"] == "invalid" and finance.profile_catalog() == []
    assert finance.MAX_FILE_BYTES == 250_000


def test_downloader_fetches_only_allowlisted_endpoint_and_captures_body_hash():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload(row("cashflow")))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, meta = download.fetch(client, CODE, "cashflow")
    assert len(calls) == 1 and len(rows) == 1
    assert str(calls[0].url).startswith(finance.SOURCE_URL)
    assert "authorization" not in calls[0].headers
    assert meta["url"] == finance.SOURCE_URL and len(meta["response_sha256"]) == 64


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429])
def test_denied_or_redirected_source_is_stopped_without_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.example.test/data"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(download.SourceBlocked):
            download.fetch(client, CODE, "cashflow")
    assert len(calls) == 1


def test_empty_download_does_not_overwrite_last_good_cache():
    save_fixture()
    previous = (finance.directory() / f"{CODE}.json.gz").read_bytes()
    with pytest.raises(ValueError):
        download.save(CODE, {key: [] for key in finance.TABLES}, [], retrieved_at=RETRIEVED)
    assert (finance.directory() / f"{CODE}.json.gz").read_bytes() == previous
