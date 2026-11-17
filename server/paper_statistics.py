"""Read-only performance of complete saved paper ledgers, not UI pages.

Money is reconciled in integer cents. Open positions never enter closed-trade
win rates. Equity drawdown uses saved daily/latest-mark snapshots, not imagined
intraday values. No vendor, model, or broker requests are made here.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math

from . import paper_engine as engine

VERSION = "paper-performance/1"
MAX_CURVE_POINTS = 366
MAX_CLOSED_TRADES = 50
LIMITATIONS = [
    "只统计本账号、本模式截至所选日期的全部已保存模拟订单，不受账本20条分页限制；不合并三个独立账户的收益率。",
    "胜率、平均盈亏及盈亏比只使用已全部卖出的买入批次，含买卖费用；尚未平仓的浮盈亏不计为胜负。",
    "最大回撤按已保存的日快照及最新有效估值计算，不是完整盘中最大回撤；缺失日期不插值、不补造收益。",
    "样本不足时统计值留空，不以单日样本计算年化、夏普或证明策略有效；无亏损时盈亏因子留空，不返回无穷大。",
    "历史开盘/实时后续报价成交均为纸面估算；未重建分红送转、实际排队、盘口冲击及真实滑点。",
]


def _day(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid performance date")
    return value


def _money(value) -> int | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        raw = Decimal(str(value)) * 100
        rounded = raw.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return int(rounded) if abs(raw - rounded) <= Decimal("0.000001") else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _json_object(value) -> dict:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 200_000:
        raise ValueError("Invalid performance snapshot")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Invalid performance snapshot")
    return parsed


def _warn(warnings: list[str], message: str):
    if message not in warnings:
        warnings.append(message)


def _account(conn, owner, mode, seat, as_of, warnings):
    """Use current state only for the latest actual session, never old queries."""
    latest = conn.execute("SELECT MAX(day) FROM sessions WHERE owner=? AND mode=?", (owner, mode)).fetchone()[0]
    if latest is not None and latest <= as_of:
        row = conn.execute("SELECT * FROM accounts WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()
        if row:
            marked = conn.execute("SELECT COALESCE(SUM(qty*mark),0),COALESCE(SUM(qty*mark-cost),0) FROM positions WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()
            nav = conn.execute("SELECT opening_nav FROM daily_nav WHERE owner=? AND mode=? AND seat=? AND day=?", (owner, mode, seat, latest)).fetchone()
            value = {"cash": row["cash"] / 100, "market_value": marked[0] / 100,
                     "equity": (row["cash"] + marked[0]) / 100, "unrealized_pnl": marked[1] / 100,
                     "opening_equity": nav[0] / 100 if nav else None}
            if latest != as_of:
                _warn(warnings, f"所选日期没有账户更新，权益采用最近实际记录 {latest}，未用未来价格补齐。")
            return latest, value
    row = conn.execute("SELECT day,value_json FROM day_snapshots WHERE owner=? AND mode=? AND seat=? AND day<=? ORDER BY day DESC LIMIT 1", (owner, mode, seat, as_of)).fetchone()
    if row:
        try:
            value = _json_object(row["value_json"])["account"]
            if not isinstance(value, dict) or value.get("seat_id") != seat:
                raise ValueError()
            if row["day"] != as_of:
                _warn(warnings, f"所选日期缺少快照，权益仅展示最近已保存日期 {row['day']}。")
            return row["day"], value
        except (ValueError, TypeError, KeyError):
            _warn(warnings, "所选日期的账户快照无法核对，未使用当前账户倒填历史。")
    return None, None


def _ledger(conn, owner, mode, seat, as_of, start, warnings):
    """Stream all prior lots, matching closed buy batches by FIFO in cents."""
    lots = defaultdict(deque)
    closed = deque(maxlen=MAX_CLOSED_TRADES)
    result = {"buy_count": 0, "sell_count": 0, "fees_cents": 0, "realized_cents": 0,
              "period_realized_cents": 0, "closed_trade_count": 0, "win_count": 0,
              "loss_count": 0, "flat_count": 0, "wins_cents": 0, "losses_cents": 0,
              "max_profit_cents": None, "max_loss_cents": None, "latest_order_day": None,
              "closed_trades": closed, "unmatched_sells": 0, "excluded_orders": 0}
    rows = conn.execute("SELECT * FROM orders WHERE owner=? AND mode=? AND seat=? AND day<=? ORDER BY day,julianday(event_time),id", (owner, mode, seat, as_of))
    for row in rows:
        try:
            day = _day(row["day"])
            if not isinstance(row["event_time"], str):
                raise ValueError()
            stamp = datetime.fromisoformat(row["event_time"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.astimezone(engine.CN).date().isoformat() != day:
                raise ValueError()
            if not engine.CODE.fullmatch(row["code"]) or row["side"] not in {"buy", "sell"}:
                raise ValueError()
            if any(type(row[key]) is not int for key in ("qty", "price", "amount", "fee")) or row["qty"] <= 0 or row["price"] <= 0 or row["fee"] < 0 or row["amount"] != row["price"] * row["qty"]:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            result["excluded_orders"] += 1
            _warn(warnings, "账本存在日期、数量或金额无法核对的订单，已排除该项统计，未猜测收益。")
            continue
        result["latest_order_day"] = day
        in_period = day >= start
        if in_period:
            result[row["side"] + "_count"] += 1
            result["fees_cents"] += row["fee"]
        if row["side"] == "buy":
            lots[row["code"]].append({"name": row["name"], "quantity": row["qty"], "remaining": row["qty"],
                                      "cost": row["amount"] + row["fee"], "remaining_cost": row["amount"] + row["fee"],
                                      "pnl": 0, "sold_amount": 0, "entry_price": row["price"], "entry_date": day})
            continue
        remaining, remaining_fee = row["qty"], row["fee"]
        while remaining and lots[row["code"]]:
            lot = lots[row["code"]][0]
            take = min(remaining, lot["remaining"])
            buy_cost = lot["remaining_cost"] * take // lot["remaining"]
            sell_fee = remaining_fee * take // remaining
            net = row["price"] * take - sell_fee - buy_cost
            lot["remaining"] -= take
            lot["remaining_cost"] -= buy_cost
            lot["pnl"] += net
            lot["sold_amount"] += row["price"] * take
            remaining -= take
            remaining_fee -= sell_fee
            result["realized_cents"] += net
            if in_period:
                result["period_realized_cents"] += net
            if lot["remaining"] == 0:
                lots[row["code"]].popleft()
                if in_period:
                    pnl = lot["pnl"]
                    result["closed_trade_count"] += 1
                    kind = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
                    result[kind + "_count"] += 1
                    if pnl > 0:
                        result["wins_cents"] += pnl
                        result["max_profit_cents"] = max(result["max_profit_cents"] or 0, pnl)
                    elif pnl < 0:
                        result["losses_cents"] += pnl
                        result["max_loss_cents"] = min(result["max_loss_cents"] or 0, pnl)
                    closed.append({"code": row["code"], "name": lot["name"], "quantity": lot["quantity"],
                                   "entry_date": lot["entry_date"], "exit_date": day,
                                   "entry_price": lot["entry_price"] / 100,
                                   "exit_price": lot["sold_amount"] / lot["quantity"] / 100,
                                   "pnl_net": pnl / 100, "return_pct": pnl / lot["cost"] * 100,
                                   "holding_calendar_days": (date.fromisoformat(day) - date.fromisoformat(lot["entry_date"])).days})
        if remaining:
            result["unmatched_sells"] += 1
            _warn(warnings, "存在无法与已保存买入配对的卖出，未把缺失成本推算为盈利；完整已实现损益可能不可核对。")
    return result


def _curve(conn, owner, mode, seat, as_of, start, data_as_of, state, warnings):
    nav = conn.execute("SELECT day,opening_nav FROM daily_nav WHERE owner=? AND mode=? AND seat=? AND day>=? AND day<=? ORDER BY day LIMIT 1", (owner, mode, seat, start, as_of)).fetchone()
    if nav is None:
        return {"equity_curve": [], "max_drawdown_pct": None, "drawdown_peak_date": None,
                "drawdown_trough_date": None, "period_opening_cents": None,
                "equity_curve_total_points": 0, "equity_curve_truncated": False}
    opening = nav["opening_nav"]
    if type(opening) is not int or opening <= 0:
        _warn(warnings, "区间起始权益缺失或异常，不能计算区间收益与回撤。")
        return {"equity_curve": [], "max_drawdown_pct": None, "drawdown_peak_date": None,
                "drawdown_trough_date": None, "period_opening_cents": None,
                "equity_curve_total_points": 0, "equity_curve_truncated": False}
    first = {"date": nav["day"], "kind": "opening", "equity": opening / 100, "return_pct": 0.0}
    points = deque(maxlen=MAX_CURVE_POINTS - 1)
    peak, peak_day, worst, worst_peak, worst_trough, count = opening, nav["day"], 0.0, None, None, 1
    for row in conn.execute("SELECT day,value_json FROM day_snapshots WHERE owner=? AND mode=? AND seat=? AND day>=? AND day<=? ORDER BY day", (owner, mode, seat, start, as_of)):
        try:
            value = _json_object(row["value_json"])["account"]
            if not isinstance(value, dict):
                raise ValueError("Invalid account curve snapshot")
            cents = _money(value["equity"]) if value.get("seat_id") == seat else None
        except (ValueError, TypeError, KeyError):
            cents = None
        if data_as_of == row["day"] and state is not None:
            cents = _money(state.get("equity"))
        if cents is None or cents < 0:
            _warn(warnings, "部分日快照无法核对，曲线跳过缺失值，不进行插值。")
            continue
        points.append({"date": row["day"], "kind": "daily_snapshot", "equity": cents / 100,
                       "return_pct": (cents / opening - 1) * 100})
        count += 1
        if cents > peak:
            peak, peak_day = cents, row["day"]
        dd = (peak - cents) / peak * 100
        if dd > worst:
            worst, worst_peak, worst_trough = dd, peak_day, row["day"]
    if data_as_of and state is not None and start <= data_as_of <= as_of and (not points or points[-1]["date"] != data_as_of):
        cents = _money(state.get("equity"))
        if cents is not None and cents >= 0:
            points.append({"date": data_as_of, "kind": "daily_snapshot", "equity": cents / 100,
                           "return_pct": (cents / opening - 1) * 100, "provisional": True})
            count += 1
            if cents > peak:
                peak, peak_day = cents, data_as_of
            dd = (peak - cents) / peak * 100
            if dd > worst:
                worst, worst_peak, worst_trough = dd, peak_day, data_as_of
    truncated = count > MAX_CURVE_POINTS
    if truncated:
        _warn(warnings, "曲线仅传回起始点与最近365个有效日快照；最大回撤和交易统计仍使用完整区间。")
    return {"equity_curve": [first, *points], "max_drawdown_pct": worst if count > 1 else None,
            "drawdown_peak_date": worst_peak, "drawdown_trough_date": worst_trough,
            "period_opening_cents": opening, "equity_curve_total_points": count,
            "equity_curve_truncated": truncated}


def performance(username: str, mode: str = "historical", as_of: str | None = None,
                start_date: str | None = None, seat_id: str | None = None) -> dict:
    as_of = _day(as_of or ("2026-11-13" if mode == "historical" else engine.utcnow().astimezone(engine.CN).date().isoformat()))
    owner, mode, as_of = engine._params(username, mode, as_of)
    if seat_id is not None and seat_id not in engine.SEATS:
        raise ValueError("Invalid statistics seat")
    if start_date is not None:
        start_date = _day(start_date)
        if start_date > as_of:
            raise ValueError("Invalid performance range")
    accounts = []
    with engine.connection() as conn:
        earliest = conn.execute("SELECT MIN(day) FROM sessions WHERE owner=? AND mode=? AND day<=?", (owner, mode, as_of)).fetchone()[0]
        start = start_date or earliest or as_of
        for seat in engine.SEATS:
            if seat_id is not None and seat != seat_id:
                continue
            warnings = []
            data_as_of, state = _account(conn, owner, mode, seat, as_of, warnings)
            if state is not None:
                state = {field: (_money(state.get(field)) / 100 if _money(state.get(field)) is not None else None)
                         for field in ("cash", "market_value", "equity", "unrealized_pnl")}
                if state["equity"] is None:
                    _warn(warnings, "账户估值数值无法核对，相关统计留空。")
                    state = None
            ledger = _ledger(conn, owner, mode, seat, as_of, start, warnings)
            if data_as_of and ledger["latest_order_day"] and ledger["latest_order_day"] > data_as_of:
                _warn(warnings, "估值快照早于已有订单，无法确认所选日期的总权益；未将旧估值冒充最新权益。")
                state = None
            curve = _curve(conn, owner, mode, seat, as_of, start, data_as_of, state, warnings)
            equity = _money(state.get("equity")) if state else None
            session = conn.execute("SELECT status FROM sessions WHERE owner=? AND mode=? AND day=?", (owner, mode, data_as_of)).fetchone() if data_as_of else None
            final = bool(mode == "historical" and session and session["status"] == "completed")
            if state and not final:
                _warn(warnings, "本次按计算时点已保存的账户及最新有效估值统计，扫描/交易日可能未结束，不是最终官方日结。")
            count = ledger["closed_trade_count"]
            complete = ledger["unmatched_sells"] == 0 and ledger["excluded_orders"] == 0
            if not complete:
                _warn(warnings, "完整账本校验未通过：全账本已平仓数量、胜负、胜率、盈亏因子及平均/最大闭仓盈亏均留空；下方明细仅保留可核对的买卖配对子集，不能代表完整交易表现。买卖条数与费用也仅为有效订单小计。")
            realized = ledger["realized_cents"] / 100 if complete else None
            item = {"seat_id": seat, "status": "available" if state else "unavailable" if earliest else "not_started",
                    "ledger_integrity": "complete" if complete else "incomplete",
                    "excluded_order_count": ledger["excluded_orders"], "unmatched_sell_count": ledger["unmatched_sells"],
                    "verified_closed_trade_count": count,
                    "data_as_of": data_as_of, "is_final": final, "initial_cash": engine.INITIAL_CENTS / 100,
                    **{field: state.get(field) if state else None for field in ("cash", "market_value", "equity", "unrealized_pnl")},
                    "total_pnl": (equity - engine.INITIAL_CENTS) / 100 if equity is not None else None,
                    "total_return_pct": (equity / engine.INITIAL_CENTS - 1) * 100 if equity is not None else None,
                    "period_pnl": (equity - curve["period_opening_cents"]) / 100 if equity is not None and curve["period_opening_cents"] and data_as_of >= start else None,
                    "period_return_pct": (equity / curve["period_opening_cents"] - 1) * 100 if equity is not None and curve["period_opening_cents"] and data_as_of >= start else None,
                    "realized_pnl": realized, "period_realized_pnl": ledger["period_realized_cents"] / 100 if realized is not None else None,
                    "fees": ledger["fees_cents"] / 100, "buy_count": ledger["buy_count"], "sell_count": ledger["sell_count"],
                    **{field: ledger[field] if complete else None for field in ("closed_trade_count", "win_count", "loss_count", "flat_count")},
                    "win_rate_pct": ledger["win_count"] / count * 100 if complete and count else None,
                    "profit_factor": ledger["wins_cents"] / abs(ledger["losses_cents"]) if complete and ledger["losses_cents"] < 0 else None,
                    "average_win": ledger["wins_cents"] / ledger["win_count"] / 100 if complete and ledger["win_count"] else None,
                    "average_loss": ledger["losses_cents"] / ledger["loss_count"] / 100 if complete and ledger["loss_count"] else None,
                    "max_closed_profit": ledger["max_profit_cents"] / 100 if complete and ledger["max_profit_cents"] is not None else None,
                    "max_closed_loss": ledger["max_loss_cents"] / 100 if complete and ledger["max_loss_cents"] is not None else None,
                    **{field: value for field, value in curve.items() if field != "period_opening_cents"},
                    "closed_trades": list(reversed(ledger["closed_trades"])), "warnings": warnings}
            accounts.append(item)
    return {"version": VERSION, "mode": mode, "as_of": as_of, "start_date": start,
            "scope": "all_saved_ledger_through_as_of", "accounts": accounts, "limitations": list(LIMITATIONS)}
