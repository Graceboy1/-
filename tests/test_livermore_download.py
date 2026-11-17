"""Offline source-contract tests: no production data, auth or external requests."""
from __future__ import annotations

from datetime import date, timedelta
import gzip
import json
from pathlib import Path

import httpx
import pytest

from server import livermore_data, market_history
from tools import download_livermore_data as tool

D, E = "2026-11-12", "2026-11-13"
FRONTEND = f'var a="{tool.PUBLIC_UT}",n="wz.ztzt";x={{url:"getTopicZTPool"}};x=(Number(p)/1e3).toFixed(2);'


def source(rows=None, *, rc=0, total=None, qdate=20261117):
    rows = [{"c": "600000", "n": "测试股份", "p": 11000}] if rows is None else rows
    return json.dumps({"rc": rc, "data": {"tc": len(rows) if total is None else total, "qdate": qdate, "pool": rows}}).encode()


def meta(raw, day=D):
    return {"source_url": tool.PUBLIC_POOL, "source_query": tool.pool_query(day), "requested_date": day,
            "source_response_hash": tool.digest(raw), "retrieved_at": "2026-11-17T01:00:00+00:00"}


def bars():
    days, day = [], date.fromisoformat(D)
    while len(days) < 120:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    result = [{"date": day, "open": 10., "high": 11., "low": 9., "close": 10., "volume": 1000.} for day in reversed(days)]
    result[0]["high"] = 100.
    result[-1]["close"] = 11.
    result.append({"date": E, "open": 11.33, "high": 1000., "low": 11., "close": 11.33, "volume": 2000.})
    return result


@pytest.fixture
def local(tmp_path, monkeypatch):
    cases = {"600000": {"bars": bars()}, "002148": {"bars": bars()}}
    monkeypatch.setattr(market_history, "load_case", lambda case_id: cases[case_id.removeprefix("history-20261113-")])
    monkeypatch.setattr(livermore_data, "load_decision", lambda code, day: {"status": "missing", "limits": None, "missing": ["缺精确限价与证券状态"]})
    monkeypatch.setattr(livermore_data, "load_execution", lambda code, day: {"status": "missing", "auction": None, "index_auction": None, "region": None, "missing": ["缺真实竞价"]})
    stocks = [{"code": "600000", "name": "测试股份"}, {"code": "002148", "name": "第二股份"}]
    return tmp_path, stocks, cases


@pytest.mark.parametrize("code", ["000001", "001001", "002148", "003001", "600000", "601001", "603001", "605001"])
def test_mainboard_scope(code):
    assert tool.valid_scope(code, "测试股份")


@pytest.mark.parametrize("code,name", [("300001", "普通"), ("301001", "普通"), ("688001", "普通"), ("920001", "普通"), ("60000", "普通"), ("600000", "*ST测试"), ("600000", "退市测试"), ("600000", "  "), (600000, "普通")])
def test_excluded_scope(code, name):
    assert not tool.valid_scope(code, name)


@pytest.mark.parametrize("decision,execution", [(E, D), ("2026-11-11", E), (D, "2026-11-16"), ("20261112", E)])
def test_dates_not_silently_swapped(decision, execution):
    with pytest.raises(ValueError):
        tool.validate_dates(decision, execution)


def test_archive_date_pair_and_public_query():
    tool.validate_dates(D, E)
    query = tool.pool_query(D)
    assert query["date"] == "20261112" and query["pagesize"] == "1000"
    assert "token" not in query and "key" not in query


@pytest.mark.parametrize("raw", [b"{}", b'{"rc":102,"data":null}', b"not json", b'{"rc":0,"data":{"tc":0,"pool":null}}', source(total=2), source(rc=102), source(rc=False), source([{"c": "600000", "n": "测试", "p": -1}]), source([{"c": "600000", "n": "测试", "p": 1, "date": E}])])
def test_unavailable_or_wrong_day_not_empty(raw):
    with pytest.raises(tool.SourceError):
        tool.parse_pool(raw, D)


def test_valid_empty_pool_is_distinct_from_unavailable():
    assert tool.parse_pool(source([]), D) == ([], 20261117)


def test_duplicate_pool_rejected():
    row = {"c": "600000", "n": "测试", "p": 11000}
    with pytest.raises(tool.SourceError):
        tool.parse_pool(source([row, row]), D)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_source_stop_has_no_retries(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.invalid"}, content=b"denied")
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(tool.SourceError):
            tool.response_body(client, tool.PUBLIC_POOL)
    assert len(calls) == 1


@pytest.mark.parametrize("frontend", ["", FRONTEND.replace(tool.PUBLIC_UT, "changed"), FRONTEND.replace("/1e3", "/100")])
def test_frontend_contract_change_stops_before_pool(frontend):
    with pytest.raises(tool.SourceError):
        tool.verify_public_frontend(frontend)


def test_public_frontend_price_formatter():
    tool.verify_public_frontend(FRONTEND)


def test_crosscheck_matches_only_same_day_archive(local):
    _, stocks, _ = local
    raw = source()
    result = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    assert result["server_qdate_raw"] == 20261117
    assert result["requested_date"] == D and result["trusted"] is False
    assert result["rows"][0]["reported_price"] == 11.
    assert result["rows"][0]["source_hash"] == tool.digest(raw)
    assert result["rows"][0]["verification_status"] == "source_reference_only"
    assert "limit_up" not in result["rows"][0]


def test_wrong_day_price_not_promoted_by_qdate(local):
    _, stocks, _ = local
    raw = source([{ "c": "600000", "n": "测试股份", "p": 11330}])
    result = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    assert not result["rows"] and result["price_mismatches"][0]["code"] == "600000"


def test_scope_and_bad_archive_excluded_from_reference(local, monkeypatch):
    _, stocks, _ = local
    raw = source([{"c": "300001", "n": "创业板", "p": 10000}, {"c": "600000", "n": "*ST测试", "p": 11000}, {"c": "002148", "n": "普通", "p": 11000}])
    monkeypatch.setattr(market_history, "load_case", lambda value: (_ for _ in ()).throw(ValueError("checksum mismatch")))
    result = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    assert result["rows"] == []
    assert set(result["excluded_codes"]) == {"300001", "600000"}
    assert result["unavailable_archive_codes"] == ["002148"]


def test_collect_two_dates_only_and_metadata_body(local):
    output, stocks, _ = local
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if request.url.path.endswith("detail.js"):
            return httpx.Response(200, text=FRONTEND)
        return httpx.Response(200, content=source([{ "c": "600000", "n": "测试股份", "p": 11000 if request.url.params["date"] == "20261112" else 11330}]))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = tool.collect_references(stocks, D, E, client, output)
    assert len(calls) == 3
    assert set(result["files"]) == {D, E}
    assert len(list((output / "raw").glob("*.body"))) == 3
    reference = tool.read_reference(D, output)
    assert reference["rows"][0]["reported_price"] == 11.
    assert tool.read_reference(E, output)["rows"][0]["reported_price"] == 11.33
    assert not (output / "index.json").exists()  # reserved strict-evidence importer
    assert not any(p.name.startswith(("limits", "auctions")) for p in output.rglob("*"))


def test_failed_second_date_preserves_index(local):
    output, stocks, _ = local
    old = b'{"old":"do-not-replace"}'
    (output / "references-index.json").write_bytes(old)
    def handle(request):
        if request.url.path.endswith("detail.js"):
            return httpx.Response(200, text=FRONTEND)
        return httpx.Response(200, content=source() if request.url.params["date"] == "20261112" else b'{"rc":102,"data":null}')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(tool.SourceError):
            tool.collect_references(stocks, D, E, client, output)
    assert (output / "references-index.json").read_bytes() == old


def test_immutable_versions_keep_old_index_valid(local, monkeypatch):
    output, stocks, _ = local
    raw = source()
    profile = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    tool.persist_references([profile], [(raw, meta(raw))], output)
    original_index = (output / "references-index.json").read_bytes()
    old_file = json.loads(original_index)["files"][D]["path"]
    actual_atomic = tool.atomic_bytes
    def fail_index(path, value):
        if path.name == "references-index.json":
            raise OSError("simulated index write failure")
        actual_atomic(path, value)
    monkeypatch.setattr(tool, "atomic_bytes", fail_index)
    profile = {**profile, "retrieved_at": "new snapshot"}
    with pytest.raises(OSError):
        tool.persist_references([profile], [], output)
    assert (output / "references-index.json").read_bytes() == original_index
    assert (output / old_file).is_file() and tool.read_reference(D, output) is not None


def test_reference_hash_corruption_not_read(local):
    output, stocks, _ = local
    raw = source()
    profile = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    index = tool.persist_references([profile], [], output)
    (output / index["files"][D]["path"]).write_bytes(gzip.compress(b"{}"))
    assert tool.read_reference(D, output) is None


def test_reference_profile_cannot_be_moved_to_other_day(local):
    output, stocks, _ = local
    raw = source()
    profile = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    tool.persist_references([profile], [], output)
    assert tool.read_reference(E, output) is None


def test_coverage_no_future_bars_no_synthetic_verification(local):
    output, stocks, _ = local
    raw = source()
    profile = tool.crosscheck_reference(raw, meta(raw), D, stocks)
    tool.persist_references([profile], [], output)
    result = tool.scan_coverage(stocks, D, E, output)
    assert result["history_120_stocks"] == result["low_position_stocks"] == 2
    assert result["source_limit_up_event_stocks"] == 1 and result["status"] == "reference_only"
    assert result["candidate_stocks"] == result["execution_ready_stocks"] == result["verified_limit_stocks"] == 0
    assert all(sample["position_120_pct"] < 3 for sample in result["samples"])
    assert all(sample["limit_up_confirmed"] is None for sample in result["samples"])


def test_coverage_counts_only_strict_valid_evidence(local, monkeypatch):
    output, stocks, _ = local
    monkeypatch.setattr(livermore_data, "load_decision", lambda code, day: {"status": "verified", "limits": {"limit_up": 11., "is_st": False, "suspended": False, "security_name": "普通股份"}, "missing": []})
    monkeypatch.setattr(livermore_data, "load_execution", lambda code, day: {"status": "verified", "auction": {"open": 11.33}, "index_auction": {}, "region": {}, "missing": []})
    result = tool.scan_coverage(stocks, D, E, output)
    assert result["candidate_stocks"] == result["execution_ready_stocks"] == 2
    assert result["verified_limit_stocks"] == result["verified_security_status_stocks"] == 2


def test_coverage_strict_position_boundary(local):
    output, stocks, cases = local
    # (11-9)/(29-9) == 10%, strict boundary must not pass.
    cases["600000"]["bars"][0]["high"] = 29.
    result = tool.scan_coverage(stocks, D, E, output)
    assert result["low_position_stocks"] == 1


def test_coverage_bad_execution_groups_not_counted(local, monkeypatch):
    output, stocks, _ = local
    monkeypatch.setattr(livermore_data, "load_execution", lambda code, day: {"status": "invalid", "auction": {"untrusted": True}, "index_auction": {}, "region": {}, "missing": ["校验失败"]})
    result = tool.scan_coverage(stocks, D, E, output)
    assert result["verified_stock_auction_stocks"] == result["verified_index_auction_stocks"] == result["regional_confirmation_stocks"] == 0


def test_dry_run_no_network_no_writes(local, monkeypatch, capsys):
    output, stocks, _ = local
    monkeypatch.setattr(tool, "universe", lambda: stocks)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(market_history, "directory", lambda: output)
    assert tool.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["universe_stocks"] == 2
    assert list(output.iterdir()) == []


def test_refresh_coverage_offline(local, monkeypatch):
    output, stocks, _ = local
    monkeypatch.setattr(tool, "universe", lambda: stocks)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(market_history, "directory", lambda: output)
    assert tool.main(["--refresh-coverage"]) == 0
    assert (output / "livermore" / "coverage.json").is_file()
