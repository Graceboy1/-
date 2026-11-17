"""Disposable fixture archives; no real accounts, data downloads or model calls."""
from __future__ import annotations

from datetime import date, timedelta
import gzip
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from server import access, main, market_history
from tools.download_market_history import connect, eligible, parse_eastmoney, parse_sina, parse_tencent


@pytest.fixture
def archive(tmp_path, monkeypatch):
    directory = tmp_path / "market"
    directory.mkdir()
    (directory / "daily").mkdir()
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(directory))
    rows = []
    day = date(2026, 9, 1)
    while day <= date(2026, 11, 13):
        if day.weekday() < 5:
            rows.append({"date": day.isoformat(), "open": 10., "high": 11., "low": 9., "close": 10.5, "volume": 100000., "amount": None})
        day += timedelta(days=1)
    # Deliberately dramatic future price: any evidence/metric leakage is visible.
    rows[-1].update(open=100., high=120., low=90., close=110.)
    payload = {"code": "600000", "name": "测试价格样本", "source": "tencent", "data_mode": "historical",
               "adjustment": "none", "volume_unit": "shares", "downloaded_at": "2026-11-16T00:00:00Z", "bars": rows}
    raw = json.dumps(payload).encode()
    with gzip.open(directory / "daily" / "600000.json.gz", "wb") as handle:
        handle.write(raw)
    with connect(directory) as conn:
        conn.execute("INSERT INTO stocks(code,name,market,status,first_date,last_date,bar_count,sha256) VALUES(?,?,?,?,?,?,?,?)",
                     ("600000", payload["name"], "SH", "ok", rows[0]["date"], rows[-1]["date"], len(rows), hashlib.sha256(raw).hexdigest()))
    (directory / "manifest.json").write_text(json.dumps({"schema": "market-history/v1", "end_date": "2026-11-13", "downloaded": 1, "source": "tencent", "status": "complete"}), encoding="utf-8")
    return directory, payload


@pytest.fixture
def client(archive, monkeypatch):
    workspace = access.WorkspaceSession("fixture-only-session", "history-test-user", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: workspace)
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as value:
        yield value


def test_market_is_authenticated(archive):
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as client:
        assert client.get("/market/status").status_code == 401
        assert client.get("/market/stocks").status_code == 401
        assert client.get("/market/stocks/600000").status_code == 401


def test_catalog_pagination_search_and_no_bars(client):
    result = client.get("/market/stocks?q=600&limit=1").json()
    assert result["total"] == 1 and len(result["items"]) == 1
    assert "bars" not in result["items"][0] and result["items"][0]["sample"]["seat_id"] == "baseline"
    assert client.get("/market/stocks?q=%").json()["total"] == 0
    assert client.get("/market/stocks?offset=1").json()["items"] == []
    assert client.get("/market/stocks?limit=51").status_code == 422
    assert client.get("/market/stocks?offset=10001").status_code == 422


def test_evidence_never_contains_friday_or_outcome_metrics(client):
    result = client.get("/market/stocks/600000").json()
    assert result["evidence"]["data_mode"] == "historical"
    assert result["evidence"]["metrics"]["last_close"] == 10.5
    assert result["evidence"]["metrics"]["return_5d_pct"] == 0
    assert all(bar["date"] <= "2026-11-12" for bar in result["evidence"]["bars"])
    assert "observed_bars" not in result and not result["revealed"]
    assert all(bar["limit_up"] is None and bar["limit_down"] is None for bar in result["evidence"]["bars"])
    future = client.get("/market/stocks/600000?reveal=true").json()
    assert future["observed_bars"][0]["close"] == 110
    assert all(b["date"] == "2026-11-13" for b in future["observed_bars"])
    assert future["evidence"] == result["evidence"]


def test_invalid_codes_rejected_and_all_seats_can_read_real_stocks(client):
    for code in ("688001", "300001", "920001", "anything", "600000.json.gz"):
        assert client.get("/market/stocks/" + code).status_code == 404
    for seat in ("livermore", "buffett", "baseline"):
        response = client.post("/analyze", json={"case_id": "history-20261113-600000", "seat_id": seat})
        assert response.status_code == 200, response.text
        frozen = response.json()
        assert frozen["seat"]["id"] == frozen["evidence"]["seat_id"] == seat
        assert frozen["evidence"]["symbol"] == "600000"
        assert frozen["research_context"]["stock"]["label"] == "测试价格样本"
        assert frozen["research_context"]["data_mode"] == "historical"
        assert all(bar["date"] <= "2026-11-12" for bar in frozen["evidence"]["bars"])
    assert client.get("/market/stocks/600000?seat_id=unknown").status_code == 422


def test_formal_catalog_contains_the_same_real_stock_for_all_seats(client):
    catalog = client.get("/cases").json()
    assert catalog["data_source"] == "historical"
    assert len(catalog["cases"]) == 3
    assert {sample["seat_id"] for sample in catalog["cases"]} == {"baseline", "livermore", "buffett"}
    assert all(sample["symbol"] == "600000" and sample["name"] == "测试价格样本" for sample in catalog["cases"])
    assert all(sample["id"] == "history-20261113-600000" and "bars" not in sample for sample in catalog["cases"])
    assert not any("DEMO" in sample["symbol"] for sample in catalog["cases"])


def test_market_detail_is_seat_specific_but_never_uses_future_prices(client):
    for seat in ("baseline", "livermore", "buffett"):
        detail = client.get(f"/market/stocks/600000?seat_id={seat}").json()
        assert detail["sample"]["seat_id"] == detail["evidence"]["seat_id"] == seat
        assert detail["evidence"]["metrics"]["last_close"] == 10.5
        assert detail["evidence"]["execution"]["status"] == "unavailable"
        assert detail["evidence"]["execution"]["missing"]
        assert all(bar["date"] <= "2026-11-12" for bar in detail["evidence"]["bars"])
        if seat == "buffett":
            assert detail["evidence"]["fundamentals"]["reports"] == []
            assert not detail["evidence"]["fundamentals"]["eligible"]
        if seat == "livermore":
            assert detail["evidence"]["metrics"]["closed_at_limit_up"] is None


def test_no_archive_is_an_empty_real_catalog_not_fake_stocks(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "missing"))
    catalog = client.get("/cases").json()
    assert catalog["data_source"] == "historical" and catalog["cases"] == []
    assert catalog["archive"]["available"] is False
    examples = client.get("/cases?source=examples").json()
    assert examples["data_source"] == "synthetic" and examples["cases"]


@pytest.mark.parametrize("seat", ["buffett", "livermore"])
def test_real_seat_hold_is_recorded_without_fabricating_fills(client, seat):
    frozen = client.post("/analyze", json={"case_id": "history-20261113-600000", "seat_id": seat}).json()
    assert frozen["plan"]["action"] == "hold"
    replayed = client.post("/replay", json={"run_id": frozen["run_id"], "include_bars": True}).json()
    assert replayed["status"] == "hold"
    assert not any(event["type"] in {"buy", "sell"} for event in replayed["events"])
    assert replayed["metrics"]["open_shares"] == 0
    assert [bar["date"] for bar in replayed.get("replay_bars", [])] == ["2026-11-13"]
    record = client.get("/research/records/" + frozen["research_id"]).json()
    assert record["seat_id"] == seat and record["analysis"]["evidence"]["symbol"] == "600000"
    assert record["replay"]["status"] == "hold"


def test_real_preplan_saved_and_execution_fail_closed(client):
    result = client.post("/analyze", json={"case_id": "history-20261113-600000", "seat_id": "baseline"})
    assert result.status_code == 200, result.text
    frozen = result.json()
    assert frozen["evidence"]["metrics"]["last_close"] == 10.5
    assert frozen["research_context"]["data_mode"] == "historical"
    assert not frozen["research_context"]["stock"]["synthetic"]
    assert not frozen["validation"]["ok"]
    assert any(c["code"] == "execution_data" and not c["passed"] for c in frozen["validation"]["checks"])
    replay = client.post("/replay", json={"run_id": frozen["run_id"], "include_bars": True}).json()
    assert replay["status"] == "blocked" and "replay_bars" not in replay
    assert not any(event["type"] == "buy" for event in replay["events"])
    detail = client.get("/research/records/" + frozen["research_id"]).json()
    assert detail["analysis"]["evidence"]["data_mode"] == "historical"


def test_hash_mismatch_refuses_data(client, archive):
    directory, _ = archive
    with gzip.open(directory / "daily" / "600000.json.gz", "wb") as handle:
        handle.write(b"{}")
    assert client.get("/market/stocks/600000").status_code == 503


def test_missing_market_does_not_generate_fake_records(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "absent"))
    assert market_history.status()["available"] is False
    with pytest.raises(Exception) as error:
        market_history.load_case("history-20261113-600000")
    assert error.value.status_code == 503


@pytest.mark.parametrize("code,name,wanted", [("600000", "测试", True),("000001", "测试", True),("300001", "测试", False),("688001", "测试", False),("920001", "测试", False),("600000", "*ST测试", False),("600001", "退市测试", False)])
def test_universe_filter(code, name, wanted):
    assert eligible(code, name) is wanted


def test_vendor_units_missing_fields_and_future_filter():
    tencent = parse_tencent({"sh600000": {"day": [["2026-11-13", "10", "10.5", "11", "9", "123"],["2026-11-16", "10", "10.5", "11", "9", "1"]]}}, "sh600000")
    sina = parse_sina('var _data=([{"day":"2026-11-13","open":"10","high":"11","low":"9","close":"10.5","volume":"12300"}]);')
    assert tencent[0]["volume"] == sina[0]["volume"] == 12300
    assert tencent[0]["amount"] is None and sina[0]["amount"] is None
    assert len(tencent) == 1
    with pytest.raises(ValueError):
        parse_tencent({"sh600000": {"day": [["2026-11-13", "10", "10.5", "8", "9", "123"]]}}, "sh600000")
