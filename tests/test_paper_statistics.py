"""Complete-ledger statistics on generated temporary books, never user data."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from server import paper_engine as engine
from server import paper_statistics as stats

DAY = "2026-11-13"
USER = "performance-fixture"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_PAPER_DB", str(tmp_path / "statistics.sqlite3"))
    monkeypatch.setattr(engine, "utcnow", lambda: datetime(2026, 11, 20, 12, tzinfo=timezone.utc))


def create(day=DAY, user=USER, mode="historical"):
    return engine.create_session(user, mode, day)


def order(side, *, code="600000", day=DAY, price=1000, quantity=100, fee=500,
          seat="baseline", user=USER, mode="historical", timestamp=None):
    with engine.connection() as conn:
        conn.execute("INSERT INTO orders(owner,mode,seat,day,code,name,side,event_time,captured_at,price,qty,amount,fee,reason,decision_id,source_hash,policy_hash,execution_assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (engine._owner(user), mode, seat, day, code, "生成统计股票", side,
                      timestamp or day + ("T09:30:00+08:00" if side == "buy" else "T15:00:00+08:00"),
                      "2026-11-20T12:00:00+00:00", price, quantity, price * quantity, fee,
                      "生成数据，仅供测试", str(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
                      "a" * 64, "b" * 64, "generated_estimate"))


def cash(value, *, seat="baseline", user=USER, mode="historical"):
    with engine.connection() as conn:
        conn.execute("UPDATE accounts SET cash=? WHERE owner=? AND mode=? AND seat=?", (value, engine._owner(user), mode, seat))


def snapshot(day, equity=100000, *, seat="baseline", user=USER, mode="historical", malformed=None):
    value = {"account": {"seat_id": seat, "cash": equity, "market_value": 0,
                         "equity": equity, "unrealized_pnl": 0, "realized_pnl": 0}, "positions": []}
    if malformed is not None:
        value = malformed
    with engine.connection() as conn:
        conn.execute("INSERT OR REPLACE INTO day_snapshots VALUES(?,?,?,?,?)", (engine._owner(user), mode, seat, day, json.dumps(value)))


def base(result):
    return next(row for row in result["accounts"] if row["seat_id"] == "baseline")


def performance(**changes):
    return stats.performance(USER, as_of=DAY, **changes)


def test_not_started_has_three_independent_unknown_returns_not_fabricated_win_rates():
    result = performance()
    assert len(result["accounts"]) == 3
    for account in result["accounts"]:
        assert account["status"] == "not_started" and account["equity"] is None
        assert account["total_return_pct"] is account["win_rate_pct"] is account["profit_factor"] is None
        assert account["closed_trade_count"] == account["buy_count"] == 0
        assert account["equity_curve"] == []
    assert result["scope"] == "all_saved_ledger_through_as_of"


def test_open_position_floating_profit_never_counts_as_a_winning_closed_trade():
    candidate = {"code": "600000", "name": "生成测试股票", "source_hash": "a" * 64,
                 "decision_date": "2026-11-12", "history_end": "2026-11-12",
                 "plans": {"baseline": {"action": "simulate", "eligible": True, "reason": "生成价格区间",
                            "entry_low": 9.9, "entry_high": 10.2, "position_pct": 15., "stop_loss": 9.8, "take_profit": 10.6, "missing": []}},
                 "execution_bar": {"date": DAY, "open": 10., "high": 11., "low": 9., "close": 10.5, "volume": 10000.}}
    engine.run_historical(USER, [candidate], DAY, total=1)
    result = base(performance())
    assert result["is_final"] is True and result["buy_count"] == 1 and result["sell_count"] == 0
    assert result["realized_pnl"] == 0 and result["unrealized_pnl"] == 695
    assert result["total_pnl"] == result["period_pnl"] == 695
    assert result["win_rate_pct"] is result["profit_factor"] is None
    assert result["closed_trade_count"] == result["win_count"] == 0


def test_empty_started_book_keeps_no_closed_sample_metrics():
    create()
    result = base(performance())
    assert result["total_pnl"] == 0 and result["total_return_pct"] == 0
    assert result["win_rate_pct"] is result["profit_factor"] is result["average_win"] is None
    assert result["is_final"] is False and any("计算时点" in warning for warning in result["warnings"])


def test_closed_profit_loss_and_flat_match_fees_in_integer_cents():
    create()
    for code, sale in (("600000", 1200), ("600001", 900), ("600002", 1010)):
        order("buy", code=code)
        order("sell", code=code, price=sale)
    cash(10_008_000)
    result = base(performance())
    assert (result["win_count"], result["loss_count"], result["flat_count"]) == (1, 1, 1)
    assert result["closed_trade_count"] == result["buy_count"] == result["sell_count"] == 3
    assert result["win_rate_pct"] == pytest.approx(100 / 3)
    assert result["realized_pnl"] == result["total_pnl"] == 80
    assert result["fees"] == 30
    assert result["average_win"] == result["max_closed_profit"] == 190
    assert result["average_loss"] == result["max_closed_loss"] == -110
    assert result["profit_factor"] == pytest.approx(190 / 110)


def test_complete_ledger_not_only_twenty_visible_rows_and_trade_detail_is_bounded():
    create()
    for index in range(60):
        code = str(600100 + index)
        order("buy", code=code)
        order("sell", code=code, price=1020)
    cash(10_060_000)
    result = base(performance())
    assert result["buy_count"] == result["sell_count"] == result["closed_trade_count"] == 60
    assert result["fees"] == 600 and result["realized_pnl"] == 600
    assert result["win_rate_pct"] == 100 and result["profit_factor"] is None
    assert len(result["closed_trades"]) == stats.MAX_CLOSED_TRADES == 50
    assert result["closed_trades"][0]["code"] == "600159"


def test_all_loss_factor_is_zero_but_no_loss_factor_is_unknown_not_infinity():
    create(); order("buy"); order("sell", price=900)
    result = base(performance())
    assert result["profit_factor"] == 0 and result["average_win"] is None
    assert result["win_rate_pct"] == 0 and result["loss_count"] == 1


def test_partial_sale_realizes_money_but_is_not_a_complete_roundtrip():
    create(); order("buy", quantity=200)
    order("sell", quantity=100, price=1100)
    result = base(performance())
    assert result["realized_pnl"] == 92.5 and result["closed_trade_count"] == 0
    assert result["win_rate_pct"] is None
    order("sell", quantity=100, price=1200)
    result = base(performance())
    assert result["realized_pnl"] == 285 and result["closed_trade_count"] == 1
    trade = result["closed_trades"][0]
    assert trade["quantity"] == 200 and trade["exit_price"] == 11.5 and trade["pnl_net"] == 285
    assert trade["return_pct"] == pytest.approx(285 / 2005 * 100)


def test_fifo_preserves_all_cost_cents_when_one_sell_closes_two_buy_lots():
    create(); order("buy", price=1000); order("buy", price=1100)
    order("sell", quantity=200, price=1200, fee=501)
    result = base(performance())
    assert result["closed_trade_count"] == 2
    assert result["realized_pnl"] == 284.99
    assert sum(trade["pnl_net"] for trade in result["closed_trades"]) == pytest.approx(284.99)


def test_period_start_preserves_prior_buy_cost_but_counts_only_period_orders_and_fees():
    create("2026-11-12"); order("buy", day="2026-11-12")
    create(DAY); order("sell", price=1200)
    result = base(performance(start_date=DAY))
    assert result["buy_count"] == 0 and result["sell_count"] == 1 and result["fees"] == 5
    assert result["closed_trade_count"] == 1 and result["realized_pnl"] == 190
    assert result["closed_trades"][0]["holding_calendar_days"] == 1


def test_old_date_never_reads_future_sell_or_current_account_equity():
    create(); order("buy"); snapshot(DAY, 100100)
    create("2026-11-17"); order("sell", day="2026-11-17", price=1200); cash(99_999_999)
    result = base(performance())
    assert result["data_as_of"] == DAY and result["equity"] == 100100
    assert result["buy_count"] == 1 and result["sell_count"] == result["closed_trade_count"] == 0
    assert result["realized_pnl"] == 0 and result["win_rate_pct"] is None
    assert all(point["date"] <= DAY for point in result["equity_curve"])


def test_missing_asof_snapshot_uses_labeled_prior_snapshot_not_today():
    create(); snapshot(DAY, 99900); create("2026-11-17"); cash(99_999_999)
    result = base(stats.performance(USER, as_of="2026-11-14"))
    assert result["data_as_of"] == DAY and result["equity"] == 99900
    assert any("最近已保存日期" in warning for warning in result["warnings"])


def test_valuation_older_than_ledger_cannot_claim_latest_total_return():
    create(); snapshot(DAY, 99900)
    create("2026-11-14"); order("buy", day="2026-11-14")
    create("2026-11-17")
    result = base(stats.performance(USER, as_of="2026-11-14"))
    assert result["status"] == "unavailable" and result["equity"] is None
    assert result["total_return_pct"] is None and any("估值快照早于" in warning for warning in result["warnings"])


def test_daily_drawdown_includes_opening_capital_even_with_one_losing_day():
    create(); cash(9_797_200)
    result = base(performance())
    assert result["equity_curve"][0] == {"date": DAY, "kind": "opening", "equity": 100000., "return_pct": 0.}
    assert result["equity_curve"][-1]["equity"] == 97972
    assert result["max_drawdown_pct"] == pytest.approx(2.028)
    assert result["drawdown_peak_date"] == result["drawdown_trough_date"] == DAY


def test_daily_drawdown_peaks_recovery_and_period_initial_value():
    create("2026-11-10"); snapshot("2026-11-10", 120000)
    create("2026-11-11"); snapshot("2026-11-11", 90000)
    create(DAY); cash(11_000_000)
    result = base(performance())
    assert result["max_drawdown_pct"] == 25
    assert result["drawdown_peak_date"] == "2026-11-10" and result["drawdown_trough_date"] == "2026-11-11"


def test_curve_payload_is_bounded_but_max_drawdown_uses_omitted_old_points():
    first = date(2025, 1, 1); last = first + timedelta(days=399)
    create(first.isoformat())
    with engine.connection() as conn:
        owner = engine._owner(USER)
        conn.execute("INSERT INTO sessions VALUES(?,?,?,'completed',?,?,0,0,'')", (owner, "historical", last.isoformat(), "2026-11-20T12:00:00+00:00", "2026-11-20T12:00:00+00:00"))
        for i in range(400):
            day = (first + timedelta(days=i)).isoformat()
            equity = 200000 if i == 1 else 80000 if i == 2 else 100000
            account = {"seat_id": "baseline", "cash": equity, "market_value": 0, "equity": equity, "unrealized_pnl": 0}
            conn.execute("INSERT INTO day_snapshots VALUES(?,?,?,?,?)", (owner, "historical", "baseline", day, json.dumps({"account": account, "positions": []})))
    result = base(stats.performance(USER, as_of=last.isoformat()))
    assert len(result["equity_curve"]) == 366 and result["equity_curve_truncated"] is True
    assert result["equity_curve_total_points"] == 401
    assert result["max_drawdown_pct"] == 60
    assert result["drawdown_trough_date"] == (first + timedelta(days=2)).isoformat()


def test_unmatched_sell_does_not_invent_missing_cost_or_profit():
    create(); order("sell", price=1200)
    result = base(performance())
    assert result["realized_pnl"] is None and result["closed_trade_count"] is None
    assert result["ledger_integrity"] == "incomplete" and result["verified_closed_trade_count"] == 0
    assert result["win_rate_pct"] is None and any("缺失成本" in warning for warning in result["warnings"])


@pytest.mark.parametrize("bad", [{"account": []}, {"account": 1}, {"account": None}, {"account": {"seat_id": "baseline", "equity": float("nan")}}])
def test_malformed_snapshots_are_not_500_or_nonfinite_json(bad):
    create(); snapshot(DAY, malformed=bad); create("2026-11-17")
    result = performance()
    assert base(result)["equity"] is None and base(result)["warnings"]
    json.dumps(result, allow_nan=False)


def test_invalid_order_timestamp_or_arithmetic_is_excluded_not_counted_profit():
    create(); order("buy"); order("sell", price=1200)
    with engine.connection() as conn:
        conn.execute("UPDATE orders SET event_time='2026-11-17T15:00:00+08:00' WHERE side='sell'")
    result = base(performance())
    assert result["sell_count"] == 0 and result["closed_trade_count"] is None
    assert any("无法核对的订单" in warning for warning in result["warnings"])


@pytest.mark.parametrize("problem", ["unmatched_sell", "invalid_order"])
def test_incomplete_ledger_cannot_promote_a_verified_winning_subset_as_full_statistics(problem):
    create(); order("buy"); order("sell", price=1200)
    if problem == "unmatched_sell":
        order("sell", code="600001", price=900)
    else:
        order("buy", code="600001")
        with engine.connection() as conn:
            conn.execute("UPDATE orders SET amount=1 WHERE code='600001'")
    before = engine.dashboard(USER)["orders"]
    result = base(performance())
    assert result["ledger_integrity"] == "incomplete"
    assert result["verified_closed_trade_count"] == 1 and len(result["closed_trades"]) == 1
    assert result["closed_trades"][0]["pnl_net"] == 190
    for field in ("realized_pnl", "period_realized_pnl", "closed_trade_count", "win_count", "loss_count", "flat_count",
                  "win_rate_pct", "profit_factor", "average_win", "average_loss", "max_closed_profit", "max_closed_loss"):
        assert result[field] is None, field
    assert result["unmatched_sell_count"] == (1 if problem == "unmatched_sell" else 0)
    assert result["excluded_order_count"] == (1 if problem == "invalid_order" else 0)
    assert any("完整账本校验未通过" in warning for warning in result["warnings"])
    assert engine.dashboard(USER)["orders"] == before


def test_owner_mode_and_seat_queries_cannot_mix_private_books():
    create(); order("buy"); order("sell", price=1200)
    other = stats.performance("different-owner", as_of=DAY)
    assert all(account["buy_count"] == 0 for account in other["accounts"])
    live = stats.performance(USER, "live", as_of=DAY)
    assert all(account["buy_count"] == 0 for account in live["accounts"])
    filtered = performance(seat_id="buffett")
    assert len(filtered["accounts"]) == 1 and filtered["accounts"][0]["seat_id"] == "buffett"
    assert filtered["accounts"][0]["closed_trade_count"] == 0


@pytest.mark.parametrize("changes", [{"as_of": "2026-02-31"}, {"start_date": "2026-11-14"}, {"seat_id": "other"}, {"mode": "broker"}, {"as_of": "../private"}])
def test_invalid_statistics_queries_are_rejected(changes):
    options = {"as_of": DAY, **changes}
    with pytest.raises(ValueError):
        stats.performance(USER, **options)
