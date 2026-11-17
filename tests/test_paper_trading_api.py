"""Authenticated portfolio API tests: disposable books, no vendor/model calls."""
from __future__ import annotations

from datetime import datetime, timezone
import time

from fastapi.testclient import TestClient
import pytest

from server import access, main, paper_engine as engine, paper_market as market, paper_trading as paper


def candidate(code="000001"):
    hold = {"action": "hold", "reason": "测试证据缺失，保留现金。", "eligible": False, "missing": ["测试缺口"]}
    return {"code": code, "name": "单元测试股票" + code, "source_hash": "a" * 64,
            "decision_date": "2026-11-12", "history_end": "2026-11-12",
            "plans": {"baseline": {"action": "simulate", "reason": "测试价格区间条件满足", "eligible": True, "missing": [],
                       "entry_low": 9.95, "entry_high": 10.15, "position_pct": 15., "stop_loss": 9.8, "take_profit": 10.6,
                       "rule_version": "fixture/1"}, "livermore": hold, "buffett": hold},
            "execution_bar": {"date": "2026-11-13", "open": 10., "high": 10.6, "low": 9.8, "close": 10.2, "volume": 100000.}}


@pytest.fixture
def client(monkeypatch):
    workspace = access.WorkspaceSession("paper-api-test", "paper-test-owner", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: workspace)
    monkeypatch.setattr(market, "universe_info", lambda: {"available": True, "total": 2})
    monkeypatch.setattr(market, "iter_historical_candidates", lambda day, cancel=None: iter([candidate(), candidate("000002")]))
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as value:
        yield value, workspace


def completed(client):
    for _ in range(150):
        response = client.get("/paper/dashboard")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["engine"]["status"] not in {"running", "idle"}:
            return result
        time.sleep(.01)
    pytest.fail("Small in-memory fixture portfolio did not finish")


def test_get_only_shows_three_initial_books_and_never_starts(client):
    c, _ = client
    result = c.get("/paper/dashboard").json()
    assert result["engine"]["status"] == "idle"
    assert len(result["accounts"]) == 3
    assert all(row["equity"] == 100000 for row in result["accounts"])
    assert result["orders"]["items"] == result["positions"] == []
    assert not paper._jobs


def test_readonly_dashboard_includes_livermore_coverage_and_live_restriction(client, monkeypatch):
    from server import livermore_data
    c, _ = client
    receipt = livermore_data.integration_summary()
    receipt.update(universe_stocks=100, history_120_stocks=90, low_position_stocks=20,
                   source_limit_up_event_stocks=2, status="reference_only")
    monkeypatch.setattr(livermore_data, "integration_summary", lambda: receipt)
    result = c.get("/paper/dashboard").json()
    assert result["livermore_data"] == receipt
    assert result["orders"]["total"] == 0 and not paper._jobs
    live = c.get("/paper/dashboard?mode=live").json()
    account = next(row for row in live["accounts"] if row["seat_id"] == "livermore")
    assert account["missing"] == [livermore_data.LIVE_LIMITATION]
    assert live["orders"]["total"] == 0 and not paper._jobs


def test_one_start_scans_multiple_stocks_and_exposes_real_arithmetic_fields(client):
    c, _ = client
    assert c.post("/paper/start", json={"mode": "historical"}).json()["accepted"]
    data = completed(c)
    assert data["engine"]["status"] == "completed", data
    assert data["trading_date"] == "2026-11-13" and data["simulation"] is True
    assert data["orders"]["total"] == 2
    assert {row["code"] for row in data["orders"]["items"]} == {"000001", "000002"}
    assert all(row["quantity"] % 100 == 0 and row["fees"] >= 5 for row in data["orders"]["items"])
    assert all(row["timestamp"].startswith("2026-11-13") and row["execution_mode"] for row in data["orders"]["items"])
    base = next(row for row in data["accounts"] if row["seat_id"] == "baseline")
    assert base["position_count"] == base["buy_count"] == 2
    assert base["cash"] >= 0 and base["realized_pnl"] == 0
    assert round(base["equity"] - 100000, 2) == round(base["daily_pnl"], 2)
    assert all(row["available_quantity"] == 0 for row in data["positions"])
    assert all(row["seat_id"] == "baseline" for row in data["positions"])
    assert data["decisions"]["items"]


def test_finished_historical_start_is_idempotent_and_mode_books_are_separate(client):
    c, _ = client
    c.post("/paper/start", json={"mode": "historical"})
    original = completed(c)
    retry = c.post("/paper/start", json={"mode": "historical"}).json()
    assert retry["accepted"] is False
    later = c.get("/paper/dashboard").json()
    assert [row["id"] for row in later["orders"]["items"]] == [row["id"] for row in original["orders"]["items"]]
    live = c.get("/paper/dashboard?mode=live").json()
    assert live["orders"]["total"] == 0 and live["positions"] == []
    assert all(row["equity"] == 100000 for row in live["accounts"])


def test_owner_isolation_paging_and_seat_filter(client, monkeypatch):
    c, _ = client
    c.post("/paper/start", json={"mode": "historical"}); completed(c)
    page = c.get("/paper/dashboard?seat_id=baseline&limit=1&offset=1").json()
    assert len(page["orders"]["items"]) == 1 and page["orders"]["total"] == 2
    assert all(row["seat_id"] == "baseline" for row in page["positions"])
    other = access.WorkspaceSession("other-paper-session", "other-paper-owner", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: other)
    data = c.get("/paper/dashboard").json()
    assert data["orders"]["total"] == 0 and data["positions"] == []


def test_live_after_close_waits_without_backfilling_or_calling_sources(client, monkeypatch):
    c, workspace = client
    closed = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(paper, "utcnow", lambda: closed)
    monkeypatch.setattr(engine, "utcnow", lambda: closed)
    async def forbidden(*args, **kwargs):
        raise AssertionError("A closed-session fixture attempted a market request")
    monkeypatch.setattr(market, "fetch_quotes", forbidden)
    assert c.post("/paper/start", json={"mode": "live"}).json()["accepted"]
    for _ in range(100):
        result = c.get("/paper/dashboard?mode=live").json()
        if result["engine"]["status"] == "waiting":
            break
        time.sleep(.01)
    assert result["engine"]["status"] == "waiting"
    assert result["trading_date"] == result["calendar_date"] == "2026-11-16"
    assert result["orders"]["total"] == 0
    assert c.post("/paper/start", json={"mode": "live"}).json()["accepted"] is False
    assert c.post("/paper/stop", json={"mode": "live"}).json()["accepted"]
    assert paper._jobs[paper._key(workspace.username, "live")].stop.is_set()


@pytest.mark.parametrize("url", ["/paper/dashboard?mode=other", "/paper/dashboard?date=2026-02-31", "/paper/dashboard?date=2026-11-16", "/paper/dashboard?mode=live&date=2099-01-01", "/paper/dashboard?seat_id=unknown", "/paper/dashboard?limit=21"])
def test_bad_queries_are_bounded(client, url):
    assert client[0].get(url).status_code == 422


def test_no_client_backdate_or_arbitrary_money_and_no_cross_origin_mutation(client):
    c, _ = client
    for extra in ({"date": "2026-11-13"}, {"username": "victim"}, {"capital": 99999999}, {"mode": "broker"}):
        assert c.post("/paper/start", json={"mode": "live", **extra}).status_code == 422
    assert c.post("/paper/start", json={"mode": "historical"}, headers={"Origin": "https://other.example"}).status_code == 403


def test_missing_archive_is_not_a_fake_portfolio(client, monkeypatch):
    monkeypatch.setattr(market, "universe_info", lambda: {"available": False, "total": 0})
    assert client[0].post("/paper/start", json={"mode": "historical"}).status_code == 409


def test_paper_routes_require_a_ready_authenticated_session():
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as c:
        assert c.get("/paper/dashboard").status_code == 401
        assert c.post("/paper/start", json={"mode": "historical"}).status_code == 401
        assert c.post("/paper/stop", json={"mode": "historical"}).status_code == 401
        assert c.get("/paper/performance").status_code == 401
        assert c.get("/paper/playback").status_code == 401


def test_statistics_read_the_full_ledger_without_turning_open_positions_into_wins(client):
    c, _ = client
    c.post("/paper/start", json={"mode": "historical"}); completed(c)
    assert len(c.get("/paper/dashboard?limit=1").json()["orders"]["items"]) == 1
    response = c.get("/paper/performance")
    assert response.status_code == 200, response.text
    data = response.json()
    base = next(row for row in data["accounts"] if row["seat_id"] == "baseline")
    assert data["as_of"] == "2026-11-13" and base["buy_count"] == 2
    assert base["closed_trade_count"] == base["win_count"] == base["loss_count"] == 0
    assert base["win_rate_pct"] is None and base["profit_factor"] is None
    assert base["realized_pnl"] == 0 and base["unrealized_pnl"] > 0
    assert base["fees"] >= 10 and base["equity_curve"]


def test_playback_is_owned_get_only_and_never_runs_a_trade(client, monkeypatch):
    c, _ = client
    c.post("/paper/start", json={"mode": "historical"}); book = completed(c)
    order_id = book["orders"]["items"][0]["id"]
    result = c.get(f"/paper/playback?order_id={order_id}")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["timeframe"] == "1d" and data["selection"]["order_id"] == order_id
    assert data["selection"]["code"] in {"000001", "000002"}
    assert data["bars"] == [] and data["has_trade_day"] is False  # No archive fixture here; never invent candles.
    assert c.get("/paper/dashboard").json()["orders"] == book["orders"]
    other = access.WorkspaceSession("another", "other-replay-owner", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: other)
    assert c.get(f"/paper/playback?order_id={order_id}").status_code == 404
    stats = c.get("/paper/performance").json()
    assert all(row["buy_count"] == row["sell_count"] == 0 for row in stats["accounts"])


@pytest.mark.parametrize("path", ["/paper/performance?date=2026-11-16", "/paper/performance?mode=live&date=2099-01-01", "/paper/performance?start_date=2026-11-14", "/paper/playback?order_id=-1", "/paper/playback?code=688001", "/paper/playback?date=2026-11-16"])
def test_replay_and_statistics_queries_are_bounded(client, path):
    assert client[0].get(path).status_code == 422
