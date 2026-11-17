"""CNINFO archive regression tests: MockTransport and disposable public data only."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from tools import download_buffett_originals as originals

CODE = "000333"
YEARS = [2023, 2024, 2025]
CUTOFF = "2026-11-12"
RETRIEVED = "2026-11-17T02:00:00+00:00"
PDF = b"%PDF-1.7\n% disposable test fixture, no securities facts\n%%EOF\n"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    monkeypatch.setattr(originals.time, "sleep", lambda _seconds: None)

    def deny(_self, _request):
        raise AssertionError("Original-report test attempted live network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


def announcement(year=2025, identity=None, **changes):
    identity = identity or str(1200000000 + year)
    published = f"{year+1}-04-01"
    return {"secCode": CODE, "announcementId": identity,
            "announcementTitle": f"<em>测试企业</em>{year}年年度报告",
            "announcementTime": int(datetime.fromisoformat(published+"T00:00:00+08:00").timestamp()*1000),
            "adjunctUrl": f"finalpage/{published}/{identity}.PDF", "adjunctSize": 1,
            **changes}


def search(*rows, total=None, totalpages=0):
    return {"announcements": list(rows), "totalAnnouncement": len(rows) if total is None else total,
            "totalpages": totalpages}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def metadata(year=2025, **changes):
    return {**originals._announcement(announcement(year), CODE, YEARS, CUTOFF), "status": "available", **changes}


def test_query_forms_fix_sources_public_scope_and_separate_sse_szse():
    form = originals.query_form(CODE, YEARS, CUTOFF, 1)
    assert form["searchkey"] == CODE and form["stock"] == "" and form["pageSize"] == "30"
    assert form["seDate"] == "2024-01-01~2026-11-12" and form["column"] == "szse"
    assert originals.query_form("600519", YEARS, CUTOFF, 1)["column"] == "sse"


def test_exact_security_chinese_full_annuals_only_and_date_is_shanghai():
    wrong = announcement(secCode="600519")
    summary = announcement(announcementTitle="测试企业2025年年度报告摘要")
    english = announcement(announcementTitle="测试企业2025年年度报告（英文版）")
    query = announcement(announcementTitle="关于2025年年度报告问询函的回复")
    same_day = announcement(announcementTime=int(datetime.fromisoformat(CUTOFF+"T00:00:00+08:00").timestamp()*1000))
    rows = [announcement(year) for year in YEARS] + [wrong, summary, english, query, same_day]
    with client(lambda _request: httpx.Response(200, json=search(*rows))) as source:
        result = originals.discover(source, CODE, YEARS, CUTOFF)
    assert all(item["status"] == "available" for item in result.values())
    assert result[2025]["publication_date"] == "2026-04-01"
    assert "<em>" not in result[2025]["title"]
    assert result[2025]["source_url"].startswith(originals.STATIC_ORIGIN)


def test_totalpages_zero_uses_total_announcement_to_fetch_all_pages():
    calls = []
    first = [announcement(secCode="600519", identity=str(1300000000+i)) for i in range(30)]

    def handler(request):
        assert str(request.url) == originals.QUERY_URL and request.method == "POST"
        page = int(parse_qs(request.content.decode())["pageNum"][0])
        calls.append(page)
        return httpx.Response(200, json=search(*(first if page == 1 else [announcement()]), total=31))

    with client(handler) as source:
        result = originals.discover(source, CODE, YEARS, CUTOFF)
    assert calls == [1, 2] and result[2025]["status"] == "available"
    assert result[2023]["status"] == "missing"


@pytest.mark.parametrize("payload", [
    {}, {"announcements": [], "totalAnnouncement": 601, "totalpages": 0},
    search(announcement(), total=31), search(announcement(), totalpages=2),
    {"announcements": [], "totalAnnouncement": True, "totalpages": 0},
])
def test_malformed_truncated_or_oversized_pagination_is_not_unique_evidence(payload):
    with client(lambda _request: httpx.Response(200, json=payload)) as source:
        with pytest.raises(ValueError):
            originals.discover(source, CODE, YEARS, CUTOFF)


def test_same_year_distinct_versions_and_lone_revision_are_ambiguous():
    for rows in [
        [announcement(), announcement(identity="1234567891")],
        [announcement(announcementTitle="测试企业2025年年度报告（修订版）")],
        [announcement(), announcement(identity="1234567892", announcementTitle="关于2025年年度报告更正的公告")],
    ]:
        with client(lambda _request: httpx.Response(200, json=search(*rows))) as source:
            result = originals.discover(source, CODE, YEARS, CUTOFF)
        assert result[2025]["status"] == "ambiguous" and len(result[2025]["candidates"]) == len(rows)


@pytest.mark.parametrize("path", [
    "https://evil.invalid/report.pdf", "http://static.cninfo.com.cn/finalpage/2026-04-01/1200002025.PDF",
    "https://static.cninfo.com.cn@evil.invalid/finalpage/2026-04-01/1200002025.PDF",
    "https://static.cninfo.com.cn:443/finalpage/2026-04-01/1200002025.PDF",
    "../secret.PDF", "finalpage/2026-04-01/1234567890.PDF", "finalpage/2026-99-01/1200002025.PDF",
    "finalpage/2026-04-01/1200002025.PDF?next=evil", "//evil.invalid/report.PDF",
])
def test_document_urls_cannot_escape_fixed_https_host_or_identity(path):
    with pytest.raises(ValueError):
        originals._pdf_url(path, "1200002025")


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 429])
def test_protected_or_redirected_source_stops_without_following(status, tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://evil.invalid/"})

    with client(handler) as source:
        code, value = originals.run(source, [CODE, "600519"], YEARS, CUTOFF, tmp_path, retrieved_at=RETRIEVED)
    assert code == 2 and value["source_status"] == "blocked" and calls == [originals.QUERY_URL]
    assert not list(tmp_path.rglob("*.pdf"))


def test_pdf_refusal_after_discovery_also_stops_every_further_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=search(announcement())) if request.method == "POST" else httpx.Response(429)

    with client(handler) as source:
        code, value = originals.run(source, [CODE, "600519"], YEARS, CUTOFF, tmp_path, retrieved_at=RETRIEVED)
    assert code == 2 and calls == ["POST", "GET"] and value["source_status"] == "blocked"


def test_valid_pdf_saves_hash_and_unverified_metadata_and_reuses_without_network(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})

    item, budget = metadata(), originals.Budget()
    with client(handler) as source:
        result = originals.archive_pdf(source, tmp_path, item, budget, retrieved_at=RETRIEVED)
        second = originals.archive_pdf(source, tmp_path, item, budget, retrieved_at=RETRIEVED)
    assert len(calls) == 1 and not result["reused"] and second["reused"]
    assert result["sha256"] == hashlib.sha256(PDF).hexdigest() and result["size"] == len(PDF)
    assert result["verification_status"] == "downloaded_original_not_field_verified"
    assert (tmp_path / result["file"]).read_bytes() == PDF
    saved = json.loads((tmp_path / result["metadata_file"]).read_text("utf-8"))
    assert saved["announcement_id"] == item["announcement_id"] and saved["publication_date"] == "2026-04-01"
    assert not any(key in saved for key in ("financial_reports", "business_assessment", "maintenance_capex"))


def test_hash_conflict_is_preserved_without_retry_or_overwrite(tmp_path):
    item = metadata()
    with client(lambda _request: httpx.Response(200, content=PDF)) as source:
        saved = originals.archive_pdf(source, tmp_path, item, originals.Budget(), retrieved_at=RETRIEVED)
    target = tmp_path / saved["file"]
    target.write_bytes(PDF+b"user-owned-change")
    calls = []
    with client(lambda request: calls.append(request) or httpx.Response(200, content=PDF)) as source:
        with pytest.raises(originals.ArchiveConflict):
            originals.archive_pdf(source, tmp_path, item, originals.Budget(), retrieved_at=RETRIEVED)
    assert not calls and target.read_bytes() == PDF+b"user-owned-change"


@pytest.mark.parametrize("body", [b"<html>verification required</html>", b"%PDF-1.7\ntruncated"])
def test_html_and_incomplete_documents_do_not_become_originals(body, tmp_path):
    with client(lambda _request: httpx.Response(200, content=body)) as source:
        with pytest.raises(ValueError):
            originals.archive_pdf(source, tmp_path, metadata(), originals.Budget(), retrieved_at=RETRIEVED)
    assert not list(tmp_path.rglob("*.pdf"))


def test_response_per_pdf_and_total_budgets_are_bounded(tmp_path, monkeypatch):
    with client(lambda _request: httpx.Response(200, content=PDF)) as source:
        with pytest.raises(originals.BudgetExceeded):
            originals.archive_pdf(source, tmp_path, metadata(), originals.Budget(len(PDF)-1), retrieved_at=RETRIEVED)
    monkeypatch.setattr(originals, "MAX_PDF_BYTES", len(PDF)-1)
    with client(lambda _request: httpx.Response(200, content=PDF)) as source:
        with pytest.raises(originals.BudgetExceeded):
            originals.archive_pdf(source, tmp_path, metadata(declared_size_kb=0), originals.Budget(), retrieved_at=RETRIEVED)
    monkeypatch.setattr(originals, "MAX_QUERY_BYTES", 10)
    with client(lambda _request: httpx.Response(200, json=search())) as source:
        with pytest.raises(originals.BudgetExceeded):
            originals.discover(source, CODE, YEARS, CUTOFF)


def make_universe(root, name="测试企业", status="ok", code=CODE):
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root/"index.sqlite3") as conn:
        conn.execute("CREATE TABLE stocks(code TEXT,name TEXT,status TEXT)")
        conn.execute("INSERT INTO stocks VALUES(?,?,?)", (code,name,status))


def test_dry_run_checks_public_universe_without_network_or_output_files(tmp_path, monkeypatch, capsys):
    root = tmp_path / "public-market"
    make_universe(root)
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(root))
    assert originals.main(["--codes", CODE, "--dry-run"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["dry_run"] and value["codes"] == [CODE] and value["years"] == YEARS
    assert not (root/"fundamentals").exists()


@pytest.mark.parametrize("name,status,code", [("*ST测试", "ok", CODE), ("退测试", "ok", CODE), ("测试", "failed", CODE), ("测试", "ok", "688001")])
def test_ineligible_public_universe_does_not_download(tmp_path, monkeypatch, name, status, code):
    root = tmp_path / "public-market"
    make_universe(root,name,status,code)
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(root))
    assert originals.main(["--codes", code, "--dry-run"]) == 1
    assert not (root/"fundamentals").exists()


def test_run_manifest_records_download_missing_ambiguous_and_keeps_strict_evidence_separate(tmp_path):
    rows = [announcement(2023), announcement(2024), announcement(2024, identity="1234567899")]

    def handler(request):
        return httpx.Response(200, json=search(*rows)) if request.method == "POST" else httpx.Response(200, content=PDF)

    with client(handler) as source:
        code, value = originals.run(source, [CODE], YEARS, CUTOFF, tmp_path, retrieved_at=RETRIEVED)
    assert code == 1 and value["source_status"] == "completed_with_gaps"
    assert value["reports"][f"{CODE}:2023"]["verification_status"] == originals.VERIFICATION
    assert value["reports"][f"{CODE}:2024"]["status"] == "ambiguous"
    assert value["reports"][f"{CODE}:2025"]["status"] == "missing"
    assert len(list(tmp_path.rglob("*.pdf"))) == 1
    assert json.loads((tmp_path/"manifest.json").read_text("utf-8"))["decision_date"] == CUTOFF


def stock_identity(**changes):
    return {"code": CODE, "category": "A股", "delisted": "false", "orgId": "verified_source_123", **changes}


def test_only_successful_empty_search_resolves_verified_identity_then_exact_stock():
    calls = []

    def handler(request):
        url, form = str(request.url), parse_qs(request.content.decode(), keep_blank_values=True)
        calls.append((url, form))
        if url == originals.IDENTITY_URL:
            assert form == {"keyWord": [CODE], "maxNum": ["10"]}
            return httpx.Response(200, json=[stock_identity()])
        assert url == originals.QUERY_URL
        if form["stock"] == [""]:
            assert form["searchkey"] == [CODE]
            return httpx.Response(200, json=search())
        assert form["stock"] == [f"{CODE},verified_source_123"] and form["searchkey"] == [""]
        return httpx.Response(200, json=search(*(announcement(year, orgId="verified_source_123") for year in YEARS)))

    with client(handler) as source:
        result = originals.discover(source, CODE, YEARS, CUTOFF)
    assert len(calls) == 3 and all(row["status"] == "available" for row in result.values())
    proof = result[2025]["discovery_identity"]
    assert proof["org_id"] == "verified_source_123" and proof["source_url"] == originals.IDENTITY_URL
    assert len(proof["response_sha256"]) == 64


def test_nonempty_search_never_guesses_or_resolves_an_organization():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) == originals.QUERY_URL
        return httpx.Response(200, json=search(announcement(announcementTitle="2025年年度报告摘要")))

    with client(handler) as source:
        result = originals.discover(source, CODE, YEARS, CUTOFF)
    assert calls == [originals.QUERY_URL] and result[2025]["status"] == "missing"


@pytest.mark.parametrize("identity", [
    stock_identity(code="600519"), stock_identity(category="债券"), stock_identity(delisted="true"),
    stock_identity(delisted=None), stock_identity(orgId=None), stock_identity(orgId="guessed,unsafe"),
])
def test_unverified_stock_identity_never_becomes_an_exact_stock_query(identity):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[identity] if str(request.url) == originals.IDENTITY_URL else search())

    with client(handler) as source:
        result = originals.discover(source, CODE, YEARS, CUTOFF)
    assert calls == [originals.QUERY_URL, originals.IDENTITY_URL]
    assert all(row["status"] == "missing" for row in result.values())


def test_conflicting_official_organization_ids_are_not_arbitrarily_selected():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[stock_identity(), stock_identity(orgId="other_source_456")] if str(request.url) == originals.IDENTITY_URL else search())

    with client(handler) as source:
        with pytest.raises(ValueError, match="Ambiguous"):
            originals.discover(source, CODE, YEARS, CUTOFF)
    assert calls == [originals.QUERY_URL, originals.IDENTITY_URL]


@pytest.mark.parametrize("status", [302, 401, 403, 429])
def test_identity_source_refusal_stops_without_exact_query_or_other_source(status):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status) if str(request.url) == originals.IDENTITY_URL else httpx.Response(200, json=search())

    with client(handler) as source:
        with pytest.raises(originals.SourceBlocked):
            originals.discover(source, CODE, YEARS, CUTOFF)
    assert calls == [originals.QUERY_URL, originals.IDENTITY_URL]


def test_exact_query_rejects_a_different_organization_even_when_stock_code_matches():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == originals.IDENTITY_URL:
            return httpx.Response(200, json=[stock_identity()])
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        return httpx.Response(200, json=search() if form["stock"] == [""] else search(announcement(orgId="wrong_org")))

    with client(handler) as source:
        with pytest.raises(ValueError, match="organization"):
            originals.discover(source, CODE, YEARS, CUTOFF)
    assert len(calls) == 3
