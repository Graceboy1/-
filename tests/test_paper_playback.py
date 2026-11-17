"""Daily playback uses generated candles and disposable private paper books."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import sqlite3

import pytest

from server import market_history, paper_engine as engine, paper_market
from server import paper_playback as replay

DAY = "2026-11-13"
NOW = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "utcnow", lambda: NOW)
    monkeypatch.setenv("CONTEST_PAPER_DB", str(tmp_path / "paper.sqlite3"))
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "no-price-archive"))
    paper_market._history_cache.clear()
    yield
    paper_market._history_cache.clear()


def candidate(code="600000"):
    hold = {"action": "hold", "reason": "测试缺少必要证据", "eligible": False, "missing": ["测试缺口"]}
    return {"code": code, "name": "测试股票" + code, "source_hash": "a" * 64,
            "decision_date": "2026-11-12", "history_end": "2026-11-12",
            "prior_close": 10.1,
            "plans": {"baseline": {"action": "simulate", "reason": "测试区间条件", "eligible": True, "missing": [],
                       "entry_low": 9.9, "entry_high": 10.2, "position_pct": 15., "stop_loss": 9.8,
                       "take_profit": 10.6, "rule_version": "generated/1"}, "livermore": hold, "buffett": hold},
            "execution_bar": {"date": DAY, "open": 10., "high": 11., "low": 9., "close": 10.5, "volume": 100000.}}


def order_for(user="owner", code="600000"):
    engine.run_historical(user, [candidate(code)], total=1)
    return engine.dashboard(user)["orders"]["items"][0]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "price-archive"
    (root / "daily").mkdir(parents=True)
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(root))
    rows = [{"date": day, "open": 10., "high": 11., "low": 9., "close": 10.5, "volume": 100000.}
            for day in ("2026-11-11", "2026-11-12", DAY)]
    payload = {"code": "600000", "name": "测试股票600000", "source": "generated-source",
               "data_mode": "historical", "adjustment": "none", "volume_unit": "shares",
               "downloaded_at": "2026-11-16T01:00:00+00:00", "bars": rows}
    raw = json.dumps(payload).encode()
    (root / "daily" / "600000.json.gz").write_bytes(gzip.compress(raw))
    with sqlite3.connect(root / "index.sqlite3") as conn:
        conn.execute("CREATE TABLE stocks(code TEXT PRIMARY KEY,name TEXT,status TEXT,sha256 TEXT)")
        conn.execute("INSERT INTO stocks VALUES(?,?,'ok',?)", ("600000", payload["name"], hashlib.sha256(raw).hexdigest()))
    return root, rows


def quote(code="600000", instant=NOW, **changes):
    q = {"code": code, "name": "测试股票" + code, "source": "tencent-public-quote", "source_hash": "b" * 64,
         "timestamp": instant.isoformat(), "date": instant.astimezone(engine.CN).date().isoformat(),
         "open": 10.1, "high": 11., "low": 9., "price": 10.1, "prev_close": 10.1,
         "volume_shares": 100000., "bid": 10.1, "ask": 10.2, "executable": True, "closing_only": False}
    q.update(changes)
    return q


def live_setup(monkeypatch, *, place_order=True):
    clock = [NOW - timedelta(seconds=10)]
    monkeypatch.setattr(engine, "utcnow", lambda: clock[0])
    engine.begin_scan("owner", "live", "2026-11-16", 1)
    if place_order:
        c = candidate()
        c["decision_date"] = c["history_end"] = DAY
        engine.record_live_candidate("owner", "2026-11-16", c)
    clock[0] = NOW
    q = quote()
    assert engine.apply_quote("owner", q, now=NOW)["accepted"]
    return clock, q


def test_empty_ledger_does_not_access_a_stock_archive_or_create_replay_table(monkeypatch):
    def forbidden(*args):
        raise AssertionError("No owned order should read price files")
    monkeypatch.setattr(market_history, "load_case", forbidden)
    result = replay.playback("owner")
    assert result["selection"] is None and result["bars"] == result["markers"] == []
    assert result["trade_total"] == 0 and result["timeframe"] == "1d"
    with engine.connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_daily'").fetchone() is None


def test_owned_historical_order_maps_to_validated_actual_day_candles(archive):
    order = order_for()
    before = deepcopy(engine.dashboard("owner"))
    result = replay.playback("owner", order_id=order["id"])
    assert result["selection"]["id"] == result["selection"]["order_id"] == order["id"]
    assert result["selection"]["name"] == "测试股票600000"
    assert result["selection"]["quantity"] == order["shares"]
    assert result["selection"]["fees"] == order["fee"]
    assert result["has_trade_day"] is True and result["timeframe"] == "1d"
    assert [bar["date"] for bar in result["bars"]] == ["2026-11-11", "2026-11-12", DAY]
    assert result["markers"][0]["bar_index"] == 2 and result["markers"][0]["matched_bar"]
    assert len(result["source_hash"]) == 64
    assert all("owner" not in row for row in [*result["trades"], *result["markers"]])
    assert engine.dashboard("owner") == before, "Playback must not recompute orders or positions"


@pytest.mark.parametrize("kwargs", [
    {"mode": "broker"}, {"as_of": "2026-02-31"}, {"as_of": "2099-01-01"},
    {"seat_id": "unknown"}, {"code": "300001"}, {"code": "688001"}, {"code": "920001"},
    {"code": "../600000"}, {"order_id": 0}, {"order_id": True}, {"order_id": "1"},
])
def test_invalid_or_unsupported_filters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        replay.playback("owner", **kwargs)


def test_foreign_missing_wrong_mode_wrong_seat_and_future_orders_are_uniformly_not_found(archive):
    order = order_for("owner-a")
    queries = [("owner-b", {}), ("owner-a", {"mode": "live"}), ("owner-a", {"seat_id": "buffett"}),
               ("owner-a", {"code": "600001"}), ("owner-a", {"as_of": "2026-11-12"})]
    for user, filters in queries:
        with pytest.raises(replay.PlaybackMissing):
            replay.playback(user, order_id=order["id"], **filters)
    with pytest.raises(replay.PlaybackMissing):
        replay.playback("owner-a", order_id=order["id"] + 99999)
    assert replay.playback("owner-b", code="600000")["selection"] is None


def test_chart_cutoff_never_contains_days_later_than_selected_as_of(archive):
    order_for()
    assert replay.playback("owner", as_of="2026-11-12")["bars"] == []
    result = replay.playback("owner", as_of=DAY)
    assert all(bar["date"] <= DAY for bar in result["bars"])
    assert all(marker["date"] <= DAY for marker in result["markers"])


def test_missing_or_corrupt_price_archive_keeps_orders_but_never_invents_candles(archive):
    order_for()
    root, _ = archive
    (root / "daily" / "600000.json.gz").write_bytes(gzip.compress(b"{}"))
    result = replay.playback("owner")
    assert result["selection"] and not result["has_trade_day"]
    assert result["bars"] == [] and result["markers"][0]["matched_bar"] is False
    assert result["markers"][0]["bar_index"] is None
    assert any("未通过校验" in text for text in result["limitations"])


def test_dropdown_is_bounded_but_explicit_owned_order_beyond_it_still_works(archive):
    original = order_for()
    owner = engine._owner("owner")
    with engine.connection() as conn:
        stored = dict(conn.execute("SELECT * FROM orders WHERE id=?", (original["id"],)).fetchone())
        columns = [key for key in stored if key != "id"]
        for i in range(60):
            value = {**stored, "decision_id": f"generated-{i}"}
            conn.execute("INSERT INTO orders(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(value[key] for key in columns))
        last = conn.execute("SELECT MAX(id) FROM orders WHERE owner=?", (owner,)).fetchone()[0]
    result = replay.playback("owner", order_id=last)
    assert result["trade_total"] == 61 and len(result["trades"]) == 50
    assert result["selection"]["order_id"] == last
    assert any(marker["order_id"] == last for marker in result["markers"])


def test_new_live_day_without_saved_candle_does_not_mislabel_old_archive(archive, monkeypatch):
    live_setup(monkeypatch)
    result = replay.playback("owner", mode="live", as_of="2026-11-16")
    assert result["selection"]["date"] == "2026-11-16"
    assert result["bars"] == [] and not result["has_trade_day"]
    assert any("不把较早" in text for text in result["limitations"])


def test_capture_forming_daily_snapshot_and_join_it_to_owned_order(archive, monkeypatch):
    _, q = live_setup(monkeypatch)
    assert replay.capture_quote("owner", q)
    result = replay.playback("owner", "live", "2026-11-16")
    assert result["has_trade_day"] and result["bars"][-1]["date"] == "2026-11-16"
    last = result["bars"][-1]
    assert last["volume"] == 100000 and last["close"] == q["price"]
    assert last["complete"] is False and "形成中" in last["bar_status"]
    assert result["markers"][-1]["matched_bar"]
    assert result["last_available_date"] == "2026-11-16"


def test_capture_does_not_expose_unowned_stock_or_cross_owner_observation(monkeypatch):
    _, q = live_setup(monkeypatch, place_order=False)
    assert replay.capture_quote("owner", q)
    assert replay.playback("owner", "live", "2026-11-16")["bars"] == []
    assert replay.playback("other", "live", "2026-11-16")["bars"] == []


def test_quote_capture_is_monotonic_and_no_duplicate_rows(monkeypatch):
    clock, q = live_setup(monkeypatch)
    assert replay.capture_quote("owner", q)
    assert not replay.capture_quote("owner", q)
    clock[0] += timedelta(seconds=5)
    new = quote(instant=clock[0], price=10.3, source_hash="c" * 64)
    assert replay.capture_quote("owner", new)
    assert not replay.capture_quote("owner", q)
    with engine.connection() as conn:
        rows = conn.execute("SELECT * FROM replay_daily").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["bar_json"])["close"] == 10.3


@pytest.mark.parametrize("change", [
    {"source": "untrusted"}, {"code": "688001"}, {"name": "*ST测试"}, {"source_hash": "missing"},
    {"volume_shares": 0}, {"volume_shares": float("nan")}, {"price": float("inf")},
    {"high": 9.}, {"open": None}, {"executable": False}, {"timestamp": "2026-11-16T10:00:00"},
    {"timestamp": "2026-11-16T02:00:01+00:00"}, {"timestamp": "2026-11-16T01:58:00+00:00"},
])
def test_invalid_untrusted_or_stale_observations_are_not_saved(monkeypatch, change):
    _, q = live_setup(monkeypatch)
    q.update(change)
    assert replay.capture_quote("owner", q) is False
    with engine.connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_daily'").fetchone()
        assert not exists or conn.execute("SELECT COUNT(*) FROM replay_daily").fetchone()[0] == 0


def test_capture_requires_enabled_owner_session_and_will_not_accept_pre_start_quote(monkeypatch):
    assert not replay.capture_quote("owner", quote())
    clock, q = live_setup(monkeypatch)
    assert not replay.capture_quote("other", q)
    engine.stop_session("owner", "live", "2026-11-16")
    assert not replay.capture_quote("owner", q)
    engine.begin_scan("owner", "live", "2026-11-16")
    assert not replay.capture_quote("owner", q), "A resumed worker must await a later quote"


def test_closing_snapshot_is_not_called_official_day_k_and_accepts_only_newer_closing_print(monkeypatch):
    clock, _ = live_setup(monkeypatch)
    clock[0] = datetime(2026, 11, 16, 7, 0, 30, tzinfo=timezone.utc)
    first = quote(instant=clock[0] - timedelta(seconds=30), closing_only=True, executable=False, bid=0, ask=0)
    assert replay.capture_quote("owner", first)
    later = quote(instant=clock[0] - timedelta(seconds=10), closing_only=True, executable=False, bid=0, ask=0, price=10.4)
    assert replay.capture_quote("owner", later)
    result = replay.playback("owner", "live", "2026-11-16")
    assert result["bars"][-1]["complete"] is True
    assert result["bars"][-1]["bar_status"] == "收盘报价快照（非官方日K）"
    assert result["bars"][-1]["close"] == 10.4
    clock[0] = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
    assert not replay.capture_quote("owner", later)


def test_live_closed_cache_can_fill_missing_day_without_any_network(archive, monkeypatch):
    clock, _ = live_setup(monkeypatch)
    clock[0] = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
    cached = {"code": "600000", "adjustment": "none", "volume_unit": "shares", "source": "sina-public-daily",
              "source_hash": "d" * 64, "retrieved_at": clock[0].isoformat(),
              "bars": [{"date": "2026-11-16", "open": 10.1, "high": 11., "low": 9., "close": 10.4, "volume": 9999.}]}
    paper_market._history_cache[("600000", "2026-11-17")] = cached
    result = replay.playback("owner", "live", "2026-11-16")
    assert result["bars"][-1]["date"] == "2026-11-16" and result["bars"][-1]["source"] == "sina-public-daily"
    assert result["bars"][-1]["complete"] is True
    assert cached["bars"][0]["close"] == 10.4


def test_current_day_complete_cache_cannot_reveal_a_later_close(archive, monkeypatch):
    _, q = live_setup(monkeypatch)
    paper_market._history_cache[("600000", "2026-11-17")] = {"code": "600000", "adjustment": "none", "volume_unit": "shares",
        "source_hash": "c" * 64, "bars": [{"date": "2026-11-16", "open": 10., "high": 90., "low": 9., "close": 80., "volume": 9999.}]}
    assert replay.capture_quote("owner", q)
    result = replay.playback("owner", "live", "2026-11-16")
    assert result["bars"][-1]["close"] == q["price"] and not result["bars"][-1]["complete"]


def test_observation_retention_is_owner_scoped_and_bounded(monkeypatch):
    _, q = live_setup(monkeypatch)
    monkeypatch.setattr(replay, "MAX_OBSERVATIONS", 2)
    for code in ("600000", "600001", "600002"):
        assert replay.capture_quote("owner", quote(code=code))
    with engine.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM replay_daily WHERE owner=?", (engine._owner("owner"),)).fetchone()[0] == 2
        old = json.dumps({"ignored": True})
        conn.execute("INSERT INTO replay_daily VALUES(?,?,?,?,?,?,0)", (engine._owner("other"), "2020-01-01", "600000", old, "2020-01-01T00:00:00+00:00", "a" * 64))
        conn.execute("INSERT INTO replay_daily VALUES(?,?,?,?,?,?,0)", (engine._owner("owner"), "2020-01-01", "600000", old, "2020-01-01T00:00:00+00:00", "a" * 64))
    assert replay.capture_quote("owner", quote(code="600003"))
    with engine.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM replay_daily WHERE owner=? AND day='2020-01-01'", (engine._owner("owner"),)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM replay_daily WHERE owner=? AND day='2020-01-01'", (engine._owner("other"),)).fetchone()[0] == 1
