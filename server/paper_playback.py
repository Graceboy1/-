"""Owner-bound, read-only order/day-K playback and bounded quote snapshots.

This is a daily-candle review of saved paper orders, never exchange tick data.
Selecting or playing an order cannot change accounts, replay fills or request a
provider. New live observations retain one latest OHLCV snapshot per day/code.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re

from fastapi import HTTPException

from . import market_history, paper_engine as engine, paper_market

CN = timezone(timedelta(hours=8))
MAX_TRADES = 50
MAX_MARKERS = 500
MAX_BARS = 260
MAX_OBSERVATIONS = 10_000
RETENTION_DAYS = 30
HASH = re.compile(r"[a-f0-9]{64}\Z")


class PlaybackMissing(Exception):
    """The same public 404 applies to unknown, foreign and filtered orders."""


def _day(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid playback date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise ValueError("Invalid playback date") from None
    return value


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _bar(row: dict, *, source: str, source_hash: str, asof=None, complete=True, status="已保存日线") -> dict:
    if not isinstance(row, dict):
        raise ValueError("Invalid saved candle")
    day = _day(row.get("date"))
    fields = ("open", "high", "low", "close", "volume")
    if not all(type(row.get(key)) in (int, float) and math.isfinite(row[key]) for key in fields):
        raise ValueError("Invalid saved candle numbers")
    op, hi, lo, cl, volume = (row[key] for key in fields)
    if not .01 <= lo <= min(op, cl) <= max(op, cl) <= hi <= 1e6 or not 0 < volume <= 1e15:
        raise ValueError("Invalid saved candle range")
    if not isinstance(source_hash, str) or not HASH.fullmatch(source_hash):
        raise ValueError("Missing saved candle source")
    return {"date": day, **{key: row[key] for key in fields},
            "source": str(source)[:80], "source_hash": source_hash, "asof": asof,
            "complete": complete is True, "bar_status": str(status)[:120], "suspended": False,
            "limit_up": None, "limit_down": None}


def _ensure_observations(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS replay_daily (owner TEXT NOT NULL, day TEXT NOT NULL, code TEXT NOT NULL, bar_json TEXT NOT NULL, asof TEXT NOT NULL, source_hash TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,day,code))")
    conn.execute("CREATE INDEX IF NOT EXISTS replay_daily_owner_day ON replay_daily(owner,day)")


def capture_quote(username: str, quote: dict) -> bool:
    """Capture only a newly accepted live observation, never synthesize ticks.

The caller first asks the trading engine to apply its complete execution gates.
This writer repeats source/time/session/OHLC checks and persists no credentials.
An observation failure must not cause the caller to retry an already saved fill.
"""
    owner = engine._owner(username)
    if not isinstance(quote, dict) or quote.get("source") != "tencent-public-quote":
        return False
    code = quote.get("code")
    if not isinstance(code, str) or not engine.CODE.fullmatch(code):
        return False
    name = quote.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip() or "ST" in name.upper() or "退" in name):
        return False
    instant = _instant(quote.get("timestamp", quote.get("asof")))
    now = engine.utcnow()
    if instant is None or now.tzinfo is None:
        return False
    local = instant.astimezone(CN)
    current = now.astimezone(CN)
    age = (now - instant).total_seconds()
    if local.date() != current.date() or current.weekday() >= 5 or not 0 <= age <= 90:
        return False
    clock = lambda value: value.time().replace(tzinfo=None)
    during = lambda value: time(9, 30) <= clock(value) <= time(11, 30) or time(13) <= clock(value) <= time(15)
    closing = quote.get("closing_only") is True and time(15) <= clock(current) <= time(15, 1, 30) and time(15) <= clock(local) <= time(15, 0, 59) and age <= 60
    if quote.get("closing_only") is True and not closing:
        return False
    if not closing and (quote.get("executable") is not True or not during(local) or not during(current)):
        return False
    source_hash = quote.get("source_hash", "")
    raw = {"date": local.date().isoformat(), "open": quote.get("open"), "high": quote.get("high"),
           "low": quote.get("low"), "close": quote.get("price"), "volume": quote.get("volume_shares")}
    try:
        bar = _bar(raw, source=quote["source"], source_hash=source_hash,
                   asof=instant.isoformat(timespec="microseconds"), complete=closing,
                   status="收盘报价快照（非官方日K）" if closing else "当日形成中报价快照（非完整日K）")
    except ValueError:
        return False
    day = bar["date"]
    with engine.connection() as conn:
        session = engine._session(conn, owner, "live", day)
        if not session or not session["enabled"]:
            return False
        started = _instant(session["started_at"])
        if started is None:
            return False
        active = max(started, _instant(session.get("active_since")) or started)
        if instant <= active:
            return False
        _ensure_observations(conn)
        previous = conn.execute("SELECT asof,complete FROM replay_daily WHERE owner=? AND day=? AND code=?", (owner, day, code)).fetchone()
        if previous and (instant <= (_instant(previous["asof"]) or instant) or previous["complete"] and not closing):
            return False
        conn.execute("INSERT INTO replay_daily VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,day,code) DO UPDATE SET bar_json=excluded.bar_json,asof=excluded.asof,source_hash=excluded.source_hash,complete=excluded.complete",
                     (owner, day, code, json.dumps(bar, ensure_ascii=False, allow_nan=False), bar["asof"], source_hash, int(closing)))
        conn.execute("DELETE FROM replay_daily WHERE owner=? AND day<?", (owner, (current.date() - timedelta(days=RETENTION_DAYS)).isoformat()))
        conn.execute("DELETE FROM replay_daily WHERE owner=? AND rowid IN (SELECT rowid FROM replay_daily WHERE owner=? ORDER BY day DESC,asof DESC LIMIT -1 OFFSET ?)", (owner, owner, MAX_OBSERVATIONS))
    return True


def _summary(row) -> dict:
    value = dict(row)
    return {"id": value["id"], "order_id": value["id"], "seat_id": value["seat"], "code": value["code"],
            "name": value["name"], "date": value["day"], "timestamp": value["event_time"], "side": value["side"],
            "price": value["price"] / 100, "quantity": value["qty"], "amount": value["amount"] / 100,
            "fees": value["fee"] / 100, "reason": value["reason"], "execution_mode": value["execution_assumption"],
            "source_hash": value["source_hash"], "policy_hash": value["policy_hash"]}


def _empty(mode, as_of):
    return {"mode": mode, "as_of": as_of, "timeframe": "1d", "selection": None, "trades": [], "trade_total": 0,
            "bars": [], "markers": [], "marker_total": 0, "context_start": None, "last_available_date": None,
            "has_trade_day": False, "source_hash": None,
            "limitations": ["暂无可回放的本账号模拟订单；不会补入示例股票或交易。", "回放单位为真实日K，不是分钟线或交易所逐笔成交。"]}


def _cached_bars(code, as_of):
    """Copy only existing in-process public daily cache. Never fetch a provider."""
    try:
        entries = list(paper_market._history_cache.copy().values())
    except RuntimeError:
        return []
    result = []
    for value in entries[-paper_market.MAX_HISTORY_CACHE:]:
        if not isinstance(value, dict) or value.get("code") != code or value.get("adjustment") != "none" or value.get("volume_unit") != "shares":
            continue
        rows = deepcopy(value.get("bars"))
        if not isinstance(rows, list) or len(rows) > 130:
            continue
        for row in rows:
            try:
                bar = _bar(row, source=value.get("source", "sina-public-daily"), source_hash=value.get("source_hash", ""),
                           asof=value.get("retrieved_at"), status="已缓存公开日线")
            except ValueError:
                continue
            if bar["date"] <= as_of and bar["date"] < engine.utcnow().astimezone(CN).date().isoformat():
                result.append(bar)
    return result


def playback(username: str, mode: str = "historical", as_of: str | None = None,
             order_id: int | None = None, seat_id: str | None = None, code: str | None = None) -> dict:
    as_of = _day(as_of or (market_history.REPLAY_DAY if mode == "historical" else engine.utcnow().astimezone(CN).date().isoformat()))
    owner, mode, as_of = engine._params(username, mode, as_of)
    current_day = engine.utcnow().astimezone(CN).date().isoformat()
    if seat_id not in {None, *engine.SEATS} or code is not None and (not isinstance(code, str) or not engine.CODE.fullmatch(code)):
        raise ValueError("Invalid playback seat or code")
    if order_id is not None and (type(order_id) is not int or order_id <= 0):
        raise ValueError("Invalid playback order")
    if as_of > current_day:
        raise ValueError("Future playback is not available")
    clauses, params = ["owner=?", "mode=?", "day<=?"], [owner, mode, as_of]
    if seat_id is not None:
        clauses.append("seat=?"); params.append(seat_id)
    if code is not None:
        clauses.append("code=?"); params.append(code)
    where = " AND ".join(clauses)
    observations = []
    with engine.connection() as conn:
        trade_total = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM orders WHERE {where} ORDER BY day,event_time,id LIMIT ?", (*params, MAX_TRADES)).fetchall()
        if order_id is not None:
            selected = conn.execute(f"SELECT * FROM orders WHERE {where} AND id=?", (*params, order_id)).fetchone()
            if selected is None:
                raise PlaybackMissing()
        else:
            selected = rows[0] if rows else None
        if selected is None:
            return _empty(mode, as_of)
        selection = _summary(selected)
        marker_params = (owner, mode, selection["seat_id"], selection["code"], as_of)
        marker_where = "owner=? AND mode=? AND seat=? AND code=? AND day<=?"
        marker_total = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {marker_where}", marker_params).fetchone()[0]
        marker_rows = conn.execute(f"SELECT * FROM orders WHERE {marker_where} ORDER BY day,event_time,id LIMIT ?", (*marker_params, MAX_MARKERS)).fetchall()
        if selected["id"] not in {row["id"] for row in marker_rows}:
            marker_rows = [*marker_rows[:MAX_MARKERS - 1], selected]
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_daily'").fetchone()
        if mode == "live" and exists:
            observations = conn.execute("SELECT bar_json FROM replay_daily WHERE owner=? AND code=? AND day<=? ORDER BY day DESC LIMIT ?", (owner, selection["code"], as_of, MAX_BARS)).fetchall()

    limitations = ["回放按日K逐根展示，不含分钟线或交易所逐笔成交；日内高低先后顺序不可由日K推断。",
                   "买卖标记来自已保存模拟订单，成交方式为纸面估算，不代表券商真实成交或排队证明。",
                   "当前价格不复权；分红送转、真实滑点及历史证券状态并未完整重建。"]
    merged = {}
    try:
        case = market_history.load_case("history-20261113-" + selection["code"])
        for row in case["bars"]:
            if row["date"] <= as_of and (mode != "live" or row["date"] < current_day):
                merged[row["date"]] = _bar(row, source=case["source"], source_hash=case["archive_sha256"],
                                           asof=case.get("downloaded_at"), status="已校验本地历史日线")
    except (HTTPException, ValueError, OSError, KeyError, TypeError):
        limitations.append("该股票的本地历史日线暂不可读或未通过校验，未以模拟K线补齐。")
    if mode == "live":
        for bar in _cached_bars(selection["code"], as_of):
            # Complete public daily data may supersede a once-forming snapshot.
            merged[bar["date"]] = bar
        for observation in observations:
            try:
                raw = json.loads(observation["bar_json"])
                bar = _bar(raw, source=raw["source"], source_hash=raw["source_hash"], asof=raw.get("asof"),
                           complete=raw.get("complete") is True, status=raw.get("bar_status", "已保存报价日快照"))
            except (ValueError, KeyError, TypeError):
                continue
            if bar["date"] <= as_of and (bar["date"] not in merged or not merged[bar["date"]]["complete"]):
                merged[bar["date"]] = bar
        limitations.append("实时当日日K来自本账号启用后保存的最新报价快照；形成中或收盘报价快照都不是官方最终日K，不能据此重建分钟或逐笔路径。")
    all_bars = [merged[day] for day in sorted(merged)]
    has_trade_day = selection["date"] in merged
    if has_trade_day:
        trade_index = next(index for index, bar in enumerate(all_bars) if bar["date"] == selection["date"])
        bars = all_bars[max(0, trade_index - 120):max(0, trade_index - 120) + MAX_BARS]
    else:
        bars = []
        limitations.append("所选交易日期缺少已保存日K；不把较早的历史K线冒充这笔交易当日行情。订单明细仍按原账本保留。")
    if len(all_bars) > MAX_BARS:
        limitations.append(f"单次图表最多展示{MAX_BARS}根日K，以所选交易附近区间为准。")
    if marker_total > MAX_MARKERS:
        limitations.append(f"图表最多展示{MAX_MARKERS}个交易标记；完整成交总数仍在账本中保留。")
    indices = {bar["date"]: index for index, bar in enumerate(bars)}
    markers = [{**_summary(row), "type": row["side"], "bar_index": indices.get(row["day"]),
                "matched_bar": row["day"] in indices} for row in marker_rows]
    return {"mode": mode, "as_of": as_of, "timeframe": "1d", "selection": selection,
            "trades": [_summary(row) for row in rows], "trade_total": trade_total,
            "bars": bars, "markers": markers, "marker_total": marker_total,
            "context_start": bars[0]["date"] if bars else None,
            "last_available_date": bars[-1]["date"] if bars else None, "has_trade_day": has_trade_day,
            "limitations": limitations, "source_hash": _digest([{key: bar[key] for key in ("date", "source_hash", "asof")} for bar in bars]) if bars else None}
