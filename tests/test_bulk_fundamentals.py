"""Disposable public-directory fixtures only; never request the live source."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import sqlite3

import httpx
import pytest

from server import historical_fundamentals as finance
from tools import download_all_fundamentals as bulk
from tools import download_historical_fundamentals as single

RETRIEVED = "2026-11-17T02:00:00+00:00"
CODES = {"000333": "测试主板甲", "600519": "测试主板乙"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "public-market"))
    monkeypatch.setattr(finance, "MAX_PROFILES", 6000, raising=False)
    monkeypatch.setattr(finance, "MAX_INDEX_BYTES", 1_000_000)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("Attempted live HTTP"))


def row(table, code="000333", year=2025, **overrides):
    return {"SECURITY_CODE": code, "SECURITY_NAME_ABBR": CODES.get(code, "测试其他板"),
            finance.TABLES[table]["date_field"]: f"{year}-12-31 00:00:00",
            "NOTICE_DATE": f"{year+1}-04-01 00:00:00", "UPDATE_DATE": f"{year+1}-04-01 00:00:00",
            "NETCASH_OPERATE": 20_000_000, "CONSTRUCT_LONG_ASSET": 3_000_000,
            "PARENT_NETPROFIT": 12_000_000, "WEIGHTAVG_ROE": 20.0,
            "TOTAL_LIABILITIES": 4_000_000, "TOTAL_EQUITY": 20_000_000, **overrides}


def tables(code="000333"):
    return {table: [row(table, code, year) for year in bulk.YEARS] for table in finance.TABLES}


def result(rows, *, count=None, pages=None):
    count = len(rows) if count is None else count
    return {"success": True, "result": {"data": rows, "count": count,
                                         "pages": math.ceil(count / bulk.PAGE_SIZE) if pages is None else pages}}


def make_market(rows=None):
    root = finance.directory().parent
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "index.sqlite3") as conn:
        conn.execute("CREATE TABLE stocks(code TEXT,name TEXT,status TEXT)")
        conn.executemany("INSERT INTO stocks VALUES(?,?,?)", rows or [(code, name, "ok") for code, name in CODES.items()])


def test_universe_is_exact_public_downloaded_mainboard_nonst_only():
    make_market([("000333", "测试主板甲", "ok"), ("600519", "测试主板乙", "ok"),
                 ("300001", "创业板", "ok"), ("688001", "科创板", "ok"), ("920001", "北交所", "ok"),
                 ("600001", "*ST测试", "ok"), ("000002", "退测试", "ok"), ("000003", "失败下载", "error")])
    assert bulk.eligible_universe() == CODES


def test_dry_run_uses_no_network_and_creates_no_financial_cache(capsys):
    make_market()
    assert bulk.main(["--dry-run"]) == 0
    assert "Eligible mainboard stocks: 2" in capsys.readouterr().out
    assert not finance.directory().exists()


@pytest.mark.parametrize("values", [("wrong", 2025, 1), ("income", 2022, 1), ("income", 2025, 0), ("income", 2025, 21), ("income", True, 1)])
def test_query_bounds_reject_unknown_table_year_or_page(values):
    with pytest.raises(ValueError):
        bulk.request_params(*values)


def test_query_is_one_annual_group_with_single_quoted_date_and_bounded_page():
    value = bulk.request_params("balance", 2025, 2)
    assert value["filter"] == "(REPORT_DATE='2025-12-31')"
    assert value["pageSize"] == "500" and value["pageNumber"] == "2"
    assert value["columns"] == finance.TABLES["balance"]["columns"]


def test_indicator_cpd_filters_explicit_vendor_ashare_type_not_hong_kong_or_report_version():
    value = bulk.request_params("indicators", 2023, 1)
    assert value["filter"] == '(REPORTDATE=\'2023-12-31\')(SECURITY_TYPE_CODE="058001001")'
    assert "ISNEW" not in value["filter"] and "DATATYPE" not in value["filter"]
    assert "SECURITY_TYPE_CODE" not in bulk.request_params("balance", 2023, 1)["filter"]


@pytest.mark.parametrize("status", [401, 403, 429, 302])
def test_refusal_or_redirect_stops_source_without_retry_or_any_cache_write(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.invalid/"})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(bulk.SourceBlocked):
            bulk.collect(client, CODES, pause=lambda *_: None, progress=lambda *_a, **_k: None)
    assert len(calls) == 1
    assert not finance.directory().exists()


@pytest.mark.parametrize("change", [
    {"success": False}, {"result": {"data": [], "pages": 1, "count": 0}},
    {"result": {"data": [], "pages": 21, "count": 10500}},
    {"result": {"data": [], "pages": True, "count": 1}},
    {"result": {"data": [], "pages": 1, "count": 1}},
    {"result": {"data": [row("income", year=2024)], "pages": 1, "count": 1}},
    {"result": {"data": [row("income", code="../600519")], "pages": 1, "count": 1}},
])
def test_invalid_shape_pagination_wrong_period_or_identity_fail_closed(change):
    value = {**result([row("income")]), **change}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))) as client:
        with pytest.raises(ValueError):
            bulk.fetch_page(client, "income", 2025, 1)


def test_response_body_is_bounded(monkeypatch):
    monkeypatch.setattr(bulk, "MAX_RESPONSE", 50)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 51))) as client:
        with pytest.raises(ValueError, match="Oversized"):
            bulk.fetch_page(client, "income", 2025, 1)


def test_all_year_tables_paginate_and_only_keep_eligible_pre_cutoff_annual_rows(monkeypatch):
    monkeypatch.setattr(bulk, "PAGE_SIZE", 2)
    calls, pauses = [], []
    def handle(request):
        p = dict(request.url.params)
        calls.append(p)
        table = next(name for name, spec in finance.TABLES.items() if spec["reportName"] == p["reportName"])
        year = int(p["filter"].split("'")[1][:4])
        rows = [row(table, "000333", year), row(table, "300001", year), row(table, "600519", year)]
        page = int(p["pageNumber"])
        return httpx.Response(200, json=result(rows[(page-1)*2:page*2], count=3, pages=2))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        collected, requests = bulk.collect(client, CODES, pause=pauses.append, progress=lambda *_a, **_k: None)
    assert len(calls) == 24 and pauses == [bulk.DELAY] * 24
    assert set(collected) == set(CODES)
    assert all(len(collected[code][table]) == 3 for code in CODES for table in finance.TABLES)
    assert all(len(requests[code]) == 12 for code in CODES)
    assert all(item["url"] == finance.SOURCE_URL and len(item["response_sha256"]) == 64 for item in requests["000333"])


def test_duplicate_security_across_pages_excludes_all_its_versions_without_guessing(monkeypatch):
    monkeypatch.setattr(bulk, "PAGE_SIZE", 2)
    def handle(request):
        p = dict(request.url.params)
        table = next(name for name, spec in finance.TABLES.items() if spec["reportName"] == p["reportName"])
        year = int(p["filter"].split("'")[1][:4])
        page = int(p["pageNumber"])
        rows = [row(table, "000333", year), row(table, "600519", year)] if page == 1 else [row(table, "000333", year)]
        return httpx.Response(200, json=result(rows, count=3, pages=2))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        data, source = bulk.collect(client, CODES, pause=lambda *_: None, progress=lambda *_a, **_k: None)
    assert all(data["000333"][table] == [] for table in finance.TABLES)
    assert all(len(data["600519"][table]) == 3 for table in finance.TABLES)
    assert len(source["000333"]) == 12
    assert all(item["exclusion_reason"] == "ambiguous_security_period_versions" and item["excluded_source_rows"] == 2 for item in source["000333"])
    assert not finance.directory().exists()


def mock_group_response(request):
    p = dict(request.url.params)
    table = next(name for name, spec in finance.TABLES.items() if spec["reportName"] == p["reportName"])
    year = int(p["filter"].split("'")[1][:4])
    return httpx.Response(200, json=result([row(table, code, year) for code in CODES]))


def test_complete_public_groups_are_checksummed_and_reused_without_network(tmp_path):
    cache = tmp_path / "public-staging"
    with httpx.Client(transport=httpx.MockTransport(mock_group_response)) as client:
        first = bulk.collect(client, CODES, pause=lambda *_: None, progress=lambda *_a, **_k: None, cache_directory=cache)
    assert len(list(cache.glob("*.json.gz"))) == 12
    def reject(_):
        pytest.fail("Cached complete group made a source request")
    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        second = bulk.collect(client, CODES, pause=lambda *_: None, progress=lambda *_a, **_k: None, cache_directory=cache)
    assert first == second


def test_source_failure_keeps_completed_group_but_never_stages_partial_group(tmp_path):
    cache = tmp_path / "public-staging"
    calls = []
    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(429)
        return mock_group_response(request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(bulk.SourceBlocked):
            bulk.collect(client, CODES, pause=lambda *_: None, progress=lambda *_a, **_k: None, cache_directory=cache)
    assert len(calls) == 2 and len(list(cache.glob("*.json.gz"))) == 1
    assert not (finance.directory() / "index.json").exists()
    assert bulk._load_group(cache, "cashflow", 2023)["count"] == 2
    assert bulk._load_group(cache, "cashflow", 2024) is None


def test_corrupt_public_staging_hash_fails_closed_without_source_or_profile_write(tmp_path):
    cache = tmp_path / "public-staging"
    with httpx.Client(transport=httpx.MockTransport(mock_group_response)) as client:
        group = bulk._collect_group(client, "cashflow", 2023, pause=lambda *_: None, progress=lambda *_a, **_k: None)
    bulk._save_group(cache, "cashflow", 2023, group)
    path = bulk._group_path(cache, "cashflow", 2023)
    value = json.loads(gzip.decompress(path.read_bytes()))
    value["data"]["rows"][0]["NETCASH_OPERATE"] = 999_000_000
    path.write_bytes(gzip.compress(json.dumps(value).encode()))
    with pytest.raises(ValueError, match="checksum"):
        bulk._load_group(cache, "cashflow", 2023)
    assert not (finance.directory() / "index.json").exists()


def test_staging_identity_page_completeness_and_wrong_source_not_accepted(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(mock_group_response)) as client:
        group = bulk._collect_group(client, "cashflow", 2023, pause=lambda *_: None, progress=lambda *_a, **_k: None)
    for changes in ({"year": 2024}, {"source_url": "https://example.invalid/"}, {"rows": []}, {"requests": []}):
        with pytest.raises(ValueError):
            bulk._save_group(tmp_path, "cashflow", 2023, {**group, **changes})


def test_batch_saves_checksummed_references_and_explicit_strict_gaps():
    value = bulk.save_batch({code: tables(code) for code in CODES}, {code: [] for code in CODES}, CODES, retrieved_at=RETRIEVED)
    assert value["eligible_stocks"] == 2 and value["complete_three_year_reference_stocks"] == 2
    assert value["annual_reference_rows"] == 6 and value["strict_pit_verified_stocks"] == value["strategy_ready_stocks"] == 0
    assert value["saved_profiles"] == 2
    assert json.loads((finance.directory() / "coverage.json").read_bytes())["strategy_ready_stocks"] == 0
    for code in CODES:
        profile = finance.load_profile(code)
        assert profile["status"] == "reference_only" and len(profile["reports"]) == 3
        assert profile["financial_reports"] == [] and profile["business_assessment"] is None
        assert all(row["maintenance_capex"] is None and row["shares_outstanding"] is None for row in profile["reports"])


def test_complete_old_profiles_are_byte_for_byte_preserved_against_missing_new_data():
    single.save("000333", tables(), [], retrieved_at="2026-11-16T00:00:00+00:00")
    path = finance.directory() / "000333.json.gz"
    old = path.read_bytes()
    incoming = {"000333": {table: [] for table in finance.TABLES}, "600519": tables("600519")}
    value = bulk.save_batch(incoming, {code: [] for code in CODES}, CODES, retrieved_at=RETRIEVED)
    assert path.read_bytes() == old
    assert value["preserved_existing_profiles"] == 1 and value["saved_profiles"] == 1
    assert len(finance.load_profile("000333")["reports"]) == 3


def test_partial_cached_year_is_kept_and_missing_years_added_without_blending_values():
    prior = {table: [row(table, year=2023)] for table in finance.TABLES}
    single.save("000333", prior, [], retrieved_at="2026-11-16T00:00:00+00:00")
    incoming = tables()
    incoming["income"][0]["PARENT_NETPROFIT"] = 99_000_000
    result = bulk.save_batch({"000333": incoming}, {"000333": []}, {"000333": CODES["000333"]}, retrieved_at=RETRIEVED)
    assert result["complete_three_year_reference_stocks"] == 1
    profile = finance.load_profile("000333")
    assert profile["reports"][0]["net_profit"] == 12
    assert len(profile["reports"]) == 3


def test_validation_failure_before_commit_preserves_existing_bytes_and_index():
    single.save("000333", tables(), [], retrieved_at=RETRIEVED)
    path = finance.directory() / "000333.json.gz"
    old, old_index = path.read_bytes(), (finance.directory() / "index.json").read_bytes()
    invalid = tables("600519")
    invalid["income"] = [row("income", code="000333")]
    with pytest.raises(ValueError):
        bulk.save_batch({"000333": tables(), "600519": invalid}, {code: [] for code in CODES}, CODES, retrieved_at=RETRIEVED)
    assert path.read_bytes() == old
    assert (finance.directory() / "index.json").read_bytes() == old_index
    assert not (finance.directory() / "600519.json.gz").exists()


def test_coverage_does_not_count_partial_fields_as_complete_or_as_buy_eligibility():
    data = tables()
    data["cashflow"][-1]["NETCASH_OPERATE"] = None
    profile = finance.normalize("000333", data, retrieved_at=RETRIEVED)
    value = bulk.coverage({"000333": profile}, {"000333": CODES["000333"]}, retrieved_at=RETRIEVED)
    assert value["complete_three_year_reference_stocks"] == 0 and value["strategy_ready_stocks"] == 0
    assert value["incomplete_stocks"][0]["incomplete_reports"][0]["missing_fields"] == ["operating_cashflow"]


def test_index_larger_than_original_hundred_profile_limit_is_supported():
    codes = {f"600{number:03d}": f"测试主板{number}" for number in range(101)}
    value = bulk.save_batch({code: tables(code) for code in codes}, {code: [] for code in codes}, codes, retrieved_at=RETRIEVED)
    assert value["saved_profiles"] == 101 and len(json.loads((finance.directory() / "index.json").read_bytes())["files"]) == 101
    assert len(finance.load_profile("600100")["reports"]) == 3


def test_late_publication_is_discarded_and_missing_annual_year_is_reported():
    data = tables()
    for table in data:
        data[table][-1]["NOTICE_DATE"] = finance.CUTOFF
    result = bulk.save_batch({"000333": data}, {"000333": []}, {"000333": CODES["000333"]}, retrieved_at=RETRIEVED)
    assert result["annual_reference_rows"] == 2
    assert result["incomplete_stocks"][0]["missing_years"] == ["2025"]
