"""Read-only real-market inputs for autonomous *paper* accounts.

History selection uses only closed bars; a future execution/mark bar is kept
separate. Quote polling cannot place orders. No credentials, caller URLs, LLMs,
synthetic securities or inferred historical limit prices enter this module.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import fmean
from typing import Callable, Iterator

from fastapi import HTTPException
import httpx

from . import historical_seats, market_history

VERSION = "paper-market/1"
SHANGHAI = timezone(timedelta(hours=8))
CODE_PATTERN = r"(?:000|001|002|003|600|601|603|605)\d{3}"
QUOTE_URL = "https://qt.gtimg.cn/q="
HISTORY_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService.getKLineData"
MAX_QUOTE_BYTES, MAX_HISTORY_BYTES = 200_000, 250_000
MAX_QUOTE_CODES, MAX_HISTORY_CACHE = 50, 100
_history_cache: OrderedDict[tuple[str, str], dict] = OrderedDict()


class MarketDataError(Exception):
    """Safe public error: never includes provider bodies or local file paths."""


class SourceBlocked(MarketDataError):
    """A denied/rate-limited source must not be retried via another route."""


def now_local() -> datetime:
    return datetime.now(SHANGHAI)


def _now(value: datetime | None) -> datetime:
    value = value or now_local()
    if value.tzinfo is None:
        raise ValueError("An explicit current timezone is required")
    return value.astimezone(SHANGHAI)


def eligible(code: str, name: str | None = None) -> bool:
    return isinstance(code, str) and re.fullmatch(CODE_PATTERN, code) is not None and (
        name is None or isinstance(name, str) and bool(name.strip()) and "ST" not in name.upper() and "退" not in name)


def symbol(code: str) -> str:
    if not eligible(code):
        raise ValueError("Unsupported mainboard code")
    return ("sh" if code.startswith("6") else "sz") + code


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=3)
def _rule_version(seat_id: str) -> str:
    root = Path(__file__).resolve().parent
    names = ["paper_market.py", "historical_seats.py", "market_history.py",
             "agent.py" if seat_id == "baseline" else f"seat_{seat_id}.py"]
    if seat_id == "buffett":
        names.extend(["historical_fundamentals.py", "buffett_verified.py"])
    elif seat_id == "livermore":
        names.extend(["livermore_data.py", "livermore_reference.py"])
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in names)).hexdigest()


def universe_info() -> dict:
    """Fingerprint ordered public archive summaries without loading all bars."""
    value = {"available": False, "total": 0, "fingerprint": hashlib.sha256(b"").hexdigest(),
             "decision_date": market_history.CUTOFF, "simulation_date": market_history.REPLAY_DAY,
             "ordering": "eligible security code ascending; never future-return ranking"}
    if not market_history.status().get("available"):
        return value
    digest, total = hashlib.sha256(), 0
    try:
        with closing(market_history.connection()) as conn:
            cursor = conn.execute(f"SELECT code,name,sha256 FROM stocks WHERE {market_history.ELIGIBLE_SQL} ORDER BY code")
            while rows := cursor.fetchmany(100):
                for row in rows:
                    if eligible(row["code"], row["name"]):
                        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                        total += 1
    except sqlite3.Error:
        raise MarketDataError("本地历史股票目录暂不可读，未改用合成股票。") from None
    return {**value, "available": total > 0, "total": total, "fingerprint": digest.hexdigest()}


def read_universe_page(offset: int = 0, limit: int = 20) -> list[dict]:
    """Only small directory summaries, no signal evaluation or network I/O."""
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 100_000 or not 1 <= limit <= 20:
        raise ValueError("Invalid live directory page")
    if not market_history.status().get("available"):
        return []
    try:
        with closing(market_history.connection()) as conn:
            rows = conn.execute(f"SELECT code,name FROM stocks WHERE {market_history.ELIGIBLE_SQL} ORDER BY code LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(row) for row in rows if eligible(row["code"], row["name"])]
    except sqlite3.Error:
        raise MarketDataError("实时轮询股票目录暂不可读。") from None


def _bar(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
        raise ValueError("Missing daily row")
    if date.fromisoformat(raw["date"]).isoformat() != raw["date"]:
        raise ValueError("Invalid daily date")
    fields = ("open", "high", "low", "close", "volume")
    if not all(type(raw.get(k)) in (float, int) and math.isfinite(raw[k]) for k in fields):
        raise ValueError("Nonfinite daily field")
    op, hi, lo, cl, vol = (raw[k] for k in fields)
    if not 0 < lo <= min(op, cl) <= max(op, cl) <= hi <= 1_000_000 or not 0 < vol <= 1e15:
        raise ValueError("Invalid daily prices/volume")
    return {"date": raw["date"], **{k: raw[k] for k in fields},
            "limit_up": None, "limit_down": None, "suspended": False}


def _case_from_row(row: sqlite3.Row) -> dict:
    """One archive connection, one gzip at a time; same checks as its reader."""
    with gzip.open(market_history.directory() / "daily" / f"{row['code']}.json.gz", "rb") as handle:
        raw = handle.read(2_000_001)
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ValueError("Historical checksum mismatch")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or payload.get("code") != row["code"]
            or payload.get("data_mode") != "historical" or payload.get("adjustment") != "none"
            or payload.get("volume_unit") != "shares"):
        raise ValueError("Archive profile mismatch")
    rows = payload["bars"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("Invalid archive count")
    bars = [_bar(value) for value in rows]
    dates = [value["date"] for value in bars]
    if dates != sorted(set(dates)) or any(not "2024-11-01" <= day <= market_history.REPLAY_DAY for day in dates):
        raise ValueError("Invalid archive ordering/range")
    return {**market_history.sample(row["code"], row["name"]), "bars": bars,
            "source": str(payload.get("source", "unknown")), "archive_sha256": row["sha256"],
            "downloaded_at": payload.get("downloaded_at", "")}


def _plan(case: dict, seat_id: str, snapshot: dict | None = None) -> dict:
    ev = snapshot if snapshot is not None else historical_seats.read_evidence(case, seat_id)
    plan = historical_seats.propose(case, seat_id, ev)
    missing = []
    if ev.get("metrics", {}).get("data_valid") is not True:
        missing.extend(ev.get("metrics", {}).get("data_issues", []) or ["决策日前有效日线不足。"])
    if seat_id == "livermore":
        missing.extend(ev.get("livermore", {}).get("decision_missing", ["缺少可核验的利弗莫尔决策日证据。"]))
    elif seat_id == "buffett":
        missing.extend(ev.get("fundamentals", {}).get("issues", [])[:6])
    value = {**plan.model_dump(), "reason": plan.rationale, "eligible": plan.action == "simulate" and not missing,
             "missing": missing, "rule_version": _rule_version(seat_id), "seat_id": seat_id}
    if seat_id == "buffett":
        details = ev.get("fundamentals", {})
        value["financial_evidence_hash"] = _digest({key: details.get(key) for key in ("reports", "assessment", "checks", "original_verification")})
    elif seat_id == "livermore":
        details = ev.get("livermore", {})
        value.update(entry_policy="livermore-auction/v1", requires_opening_auction=True,
                     livermore_evidence_hash=_digest({key: details.get(key) for key in ("checks", "decision_missing", "decision_source_hash", "reference")}))
    return value


def iter_historical_candidates(day: str = "2026-11-13", cancel: Callable[[], bool] | None = None) -> Iterator[dict]:
    """Scan the complete eligible archive in deterministic, non-hindsight order."""
    if day != market_history.REPLAY_DAY:
        raise ValueError("This archive supports only its labeled simulation day")
    if not market_history.status().get("available"):
        raise MarketDataError("尚无已下载的真实历史档案，无法自动模拟。")
    try:
        with closing(market_history.connection()) as conn:
            cursor = conn.execute(f"SELECT code,name,sha256 FROM stocks WHERE {market_history.ELIGIBLE_SQL} ORDER BY code")
            while rows := cursor.fetchmany(100):
                for row in rows:
                    if cancel is not None and cancel():
                        return
                    if not eligible(row["code"], row["name"]):
                        continue
                    fingerprint = row["sha256"] if isinstance(row["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) else _digest(dict(row))
                    item = {"code": row["code"], "name": row["name"], "source_hash": fingerprint,
                            "decision_date": market_history.CUTOFF, "history_end": market_history.CUTOFF,
                            "simulation_date": day, "execution_bar": None,
                            "selection_policy": "code_ascending_no_future_return_sort", "plans": {}}
                    try:
                        case = _case_from_row(row)
                        prior = [b for b in case["bars"] if b["date"] <= market_history.CUTOFF]
                        item["history_end"] = prior[-1]["date"] if prior else ""
                        item["prior_close"] = prior[-1]["close"] if prior else None
                        item["plans"] = {seat: _plan(case, seat) for seat in historical_seats.SEAT_IDS}
                        item["execution_bar"] = next((deepcopy(b) for b in case["bars"] if b["date"] == day), None)
                        item["source"] = case["source"]
                    except (OSError, ValueError, TypeError, KeyError, HTTPException):
                        reason = "该股票历史数据校验未通过，未生成订单或补入合成行情。"
                        item["plans"] = {seat: {"action": "hold", "reason": reason, "rationale": reason,
                                               "eligible": False, "missing": [reason], "rule_version": _rule_version(seat),
                                               "entry_low": None, "entry_high": None, "position_pct": None,
                                               "stop_loss": None, "take_profit": None, "seat_id": seat}
                                         for seat in historical_seats.SEAT_IDS}
                    yield item
    except sqlite3.Error:
        raise MarketDataError("历史股票扫描暂时无法继续，已停止；不会回退虚构数据。") from None


def market_open(value: datetime | None = None) -> bool:
    local = _now(value)
    clock = local.time().replace(tzinfo=None)
    return local.weekday() < 5 and (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15))


def parse_quotes(content: bytes, codes: list[str], *, now: datetime | None = None) -> list[dict]:
    """Parse bounded Tencent public response, never execute its JavaScript.

    The source quote exposes its own timestamp. Display status is deliberately
    distinct from executable: a current closing quote must not fill a new order.
    Undocumented price-limit fields are not promoted into verified limits.
    """
    local = _now(now)
    if len(content) > MAX_QUOTE_BYTES or len(codes) > MAX_QUOTE_CODES or not all(eligible(c) for c in codes):
        raise ValueError("Invalid quote bounds")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("gb18030")
    found, result = set(), []
    pattern = r'v_((?:sh|sz)\d{6})\s*=\s*"([^"\r\n]*)"\s*;?'
    for match in re.finditer(pattern, text):
        raw_symbol, body = match.groups()
        code = raw_symbol[2:]
        if code not in codes or code in found or symbol(code) != raw_symbol:
            continue
        fields = body.split("~")
        if len(fields) < 35 or fields[2] != code or not eligible(code, fields[1]):
            continue
        try:
            asof = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
            values = [float(fields[k]) for k in (3, 4, 5, 33, 34, 6)]
            price, previous, opening, high, low, lots = values
            if not all(math.isfinite(v) for v in values) or not 0 < low <= min(opening, price) <= max(opening, price) <= high <= 1e6 or previous <= 0 or not 0 < lots <= 1e13:
                continue
            bid, ask = float(fields[9]), float(fields[19])
            if not all(math.isfinite(v) and 0 <= v <= 1e6 for v in (bid, ask)):
                continue
        except (ValueError, OverflowError, IndexError):
            continue
        age = (local - asof).total_seconds()
        # A short, separate closing-observation window can update marks and
        # existing-position exits. It never permits a newly opened position,
        # and a zero closing order book is not presented as verified liquidity.
        closing_only = (local.weekday() < 5 and asof.date() == local.date()
                        and time(15) <= local.time().replace(tzinfo=None) <= time(15, 1, 30)
                        and time(15) <= asof.time().replace(tzinfo=None) <= time(15, 0, 59)
                        and 0 <= age <= 60)
        reason = ""
        if asof.date() != local.date():
            reason = "行情并非当前本地交易日，不能用于今日模拟成交。"
        elif age < 0:
            reason = "行情时间晚于当前时间，不能用于模拟成交。"
        elif age > 60:
            reason = "行情超过60秒未更新，暂停模拟撮合。"
        elif closing_only:
            reason = "仅收盘观察：可用于已有持仓估值或按规则估算退出，不允许新增买入；不代表真实盘口深度。"
        elif not market_open(local) or not market_open(asof):
            reason = "非连续交易时段，仅更新展示，不补造盘前或收盘后的成交。"
        elif bid <= 0 or ask <= 0 or bid > ask:
            reason = "缺少有效买卖报价或盘口异常，不推断可成交。"
        found.add(code)
        result.append({"code": code, "name": fields[1].strip()[:100], "price": price,
                       "prev_close": previous, "open": opening, "high": high, "low": low,
                       "volume_shares": lots * 100, "volume_unit": "shares", "bid": bid, "ask": ask,
                       "date": asof.date().isoformat(), "asof": asof.isoformat(timespec="seconds"),
                       "timestamp": asof.isoformat(timespec="seconds"), "source": "tencent-public-quote",
                       "source_url": QUOTE_URL + raw_symbol, "limit_up": None, "limit_down": None,
                       "freshness_seconds": age, "executable": not reason, "closing_only": closing_only, "rejection_reason": reason,
                       "source_hash": hashlib.sha256(match.group(0).encode()).hexdigest()})
    return result


async def _read_bounded(client: httpx.AsyncClient, url: str, *, maximum: int, params: dict | None = None) -> bytes:
    async with client.stream("GET", url, params=params) as response:
        if response.status_code in {401, 403, 429} or response.is_redirect:
            raise SourceBlocked(f"行情源拒绝、限频或重定向（HTTP {response.status_code}），已停止，未绕过。")
        response.raise_for_status()
        body = bytearray()
        async for block in response.aiter_bytes(chunk_size=16_384):
            body.extend(block)
            if len(body) > maximum:
                raise MarketDataError("行情响应超过容量限制，未采用。")
        return bytes(body)


async def fetch_quotes(codes: list[str], *, transport: httpx.AsyncBaseTransport | None = None, now: datetime | None = None) -> dict:
    if not isinstance(codes, list) or not 1 <= len(codes) <= MAX_QUOTE_CODES or any(not eligible(code) for code in codes) or len(set(codes)) != len(codes):
        raise ValueError("Supply one to fifty distinct eligible codes")
    local = _now(now)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                                     headers={"User-Agent": "SmartTradingAssistant-PersonalPaperResearch/1.0"}, transport=transport) as client:
            content = await asyncio.wait_for(_read_bounded(client, QUOTE_URL + ",".join(map(symbol, codes)), maximum=MAX_QUOTE_BYTES), timeout=12)
        # A quote can legitimately be printed while the HTTP request is in
        # flight. Compare freshness to receipt time, not request start time.
        local = _now(now)
        quotes = parse_quotes(content, codes, now=local)
    except (httpx.HTTPError, asyncio.TimeoutError):
        raise MarketDataError("公开行情连接失败，未推断成交，也未自动切换未知来源。") from None
    except (UnicodeError, ValueError):
        raise MarketDataError("公开行情格式校验失败，未推断成交。") from None
    return {"quotes": quotes, "missing_codes": [code for code in codes if code not in {q['code'] for q in quotes}],
            "source": "tencent-public-quote", "checked_at": local.isoformat(timespec="seconds"),
            "notice": "仅为带来源时间的公开行情；模拟成交仍须有冻结预案、更新后的下一条报价及账户风控。"}


def parse_recent_history(content: bytes, code: str, *, now: datetime | None = None) -> dict:
    local = _now(now)
    if not eligible(code) or len(content) > MAX_HISTORY_BYTES:
        raise ValueError("Invalid history bounds")
    text = content.decode("utf-8")
    # Strictly parse the known assignment wrapper; never eval remote JS.
    match = re.search(r"var\s+_data\s*=\s*\((\[.*\])\)\s*;?\s*$", text, re.S)
    if match is None:
        raise ValueError("Unknown daily history wrapper")
    raw = json.loads(match.group(1))
    if not isinstance(raw, list) or not 1 <= len(raw) <= 130:
        raise ValueError("Invalid history count")
    bars = []
    for value in raw:
        if not isinstance(value, dict) or not isinstance(value.get("day"), str):
            raise ValueError("Invalid daily history row")
        day = value["day"]
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Invalid history date")
        if day >= local.date().isoformat():
            continue  # Today's partial daily OHLC must not be signal evidence.
        record = {"date": day, **{key: float(value[key]) for key in ("open", "high", "low", "close", "volume")}}
        bars.append(_bar(record))  # Sina volume is shares, not lots.
    dates = [bar["date"] for bar in bars]
    if not bars or dates != sorted(set(dates)):
        raise ValueError("No ordered pre-today history")
    return {"code": code, "bars": bars[-130:], "decision_date": dates[-1],
            "source": "sina-public-daily", "source_url": HISTORY_URL, "adjustment": "none", "volume_unit": "shares",
            "source_hash": _digest({"code": code, "bars": bars}), "retrieved_at": local.isoformat(timespec="seconds")}


async def fetch_recent_history(code: str, *, transport: httpx.AsyncBaseTransport | None = None, now: datetime | None = None) -> dict:
    if not eligible(code):
        raise ValueError("Unsupported mainboard code")
    local = _now(now)
    key = (code, local.date().isoformat())
    # Injected transports are always isolated, deterministic test requests.
    if transport is None and key in _history_cache:
        _history_cache.move_to_end(key)
        return deepcopy(_history_cache[key])
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                                     headers={"User-Agent": "SmartTradingAssistant-PersonalPaperResearch/1.0"}, transport=transport) as client:
            content = await asyncio.wait_for(_read_bounded(client, HISTORY_URL, maximum=MAX_HISTORY_BYTES,
                                          params={"symbol": symbol(code), "scale": "240", "ma": "no", "datalen": "130"}), timeout=12)
        result = parse_recent_history(content, code, now=local)
    except (httpx.HTTPError, asyncio.TimeoutError):
        raise MarketDataError("前序日线来源连接失败，未生成实时买入信号。") from None
    except (UnicodeError, ValueError, KeyError, TypeError):
        raise MarketDataError("前序日线校验失败，未生成实时买入信号。") from None
    if transport is None:
        _history_cache[key] = deepcopy(result)
        _history_cache.move_to_end(key)
        while len(_history_cache) > MAX_HISTORY_CACHE:
            _history_cache.popitem(last=False)
    return result


def _snapshot(case: dict) -> dict:
    """Latest CLOSED history only, never a quote's current high/low/volume."""
    bars = deepcopy(case["bars"][-120:])
    closes = [b["close"] for b in bars]
    mean_volume = fmean(b["volume"] for b in bars[-6:-1]) if len(bars) >= 6 else 0
    issues = [] if len(bars) >= 20 and bars[-1]["date"] == case["decision_date"] else ["前序已收盘日线不足20根或截止日缺失。"]
    case_id = case["id"]
    return {"case_id": case_id, "symbol": case["symbol"], "name": case["name"], "decision_date": case["decision_date"],
            "data_mode": "historical", "bars": bars, "limitations": [],
            "metrics": {"data_valid": not issues, "data_issues": issues, "bar_count": len(bars), "tradable_count": len(bars),
                        "last_close": closes[-1] if closes else None,
                        "sma5": fmean(closes[-5:]) if len(bars) >= 5 else None,
                        "sma20": fmean(closes[-20:]) if len(bars) >= 20 else None,
                        "return_5d_pct": (closes[-1] / closes[-6] - 1) * 100 if len(bars) >= 6 else None,
                        "volume_ratio_5d": bars[-1]["volume"] / mean_volume if mean_volume else None},
            "evidence_ids": [f"{case_id}:metrics", *[f"{case_id}:bar:{b['date']}" for b in bars]]}


def make_live_candidate(code: str, name: str, history: dict, quote: dict) -> dict:
    if not eligible(code, name):
        raise ValueError("Unsupported live security")
    if quote.get("name") is not None:
        if not eligible(code, quote["name"]):
            raise ValueError("Quote name is now excluded")
        name = quote["name"]
    raw_bars = history.get("bars") if isinstance(history, dict) else None
    if not isinstance(raw_bars, list) or not 1 <= len(raw_bars) <= 130:
        raise ValueError("Missing bounded closed history")
    bars = [_bar(row) for row in raw_bars]
    dates = [b["date"] for b in bars]
    if dates != sorted(set(dates)) or history.get("code") != code or quote.get("code") != code:
        raise ValueError("Mismatched history/quote")
    quote_day = date.fromisoformat(quote["date"])
    previous = quote_day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    gaps = []
    if dates[-1] != previous.isoformat():
        gaps.append("前序日线未覆盖上一工作日；未取得实际交易日历时不跳过缺日假定完整。")
    if any(day >= quote_day.isoformat() for day in dates):
        raise ValueError("Current/future daily bar cannot become a live signal")
    if history.get("adjustment") != "none" or history.get("volume_unit") != "shares":
        raise ValueError("Unknown live history units or adjustment")
    if not isinstance(quote.get("prev_close"), (float, int)) or not math.isfinite(quote["prev_close"]) or abs(bars[-1]["close"] - quote["prev_close"]) > 0.005:
        gaps.append("报价前收与前序日线收盘不一致，可能复权、除权或数据缺口，暂停入场。")
    if quote.get("executable") is not True:
        gaps.append(str(quote.get("rejection_reason") or "当前报价不可执行。"))
    case = {"id": "live-" + code, "symbol": code, "name": name, "decision_date": dates[-1], "bars": bars}
    snapshot = _snapshot(case)
    plans = {}
    for seat in historical_seats.SEAT_IDS:
        ev = deepcopy(snapshot)
        if seat == "livermore":
            historical_seats._livermore_evidence(case, ev)
        elif seat == "buffett":
            historical_seats._value_evidence(case, ev)
        plan = _plan(case, seat, ev)
        plan["missing"] = [*plan["missing"], *gaps]
        if plan["missing"]:
            plan.update(action="hold", eligible=False, entry_low=None, entry_high=None, position_pct=None,
                        stop_loss=None, take_profit=None, reason="；".join(plan["missing"]))
        plans[seat] = plan
    return {"code": code, "name": name, "source_hash": _digest({"code": code, "bars": bars}),
            "source": history.get("source", "unknown"), "decision_date": dates[-1], "history_end": dates[-1],
            "prior_close": bars[-1]["close"], "reference_close": bars[-1]["close"],
            "simulation_date": quote_day.isoformat(), "plans": plans, "execution_bar": None,
            "selection_policy": "rolling_code_scan_closed_history_only", "status": "ready" if not gaps else "blocked_not_ready",
            "missing": gaps}


def build_live_candidates(day: str | None = None, today_date: str | None = None, cancel: Callable[[], bool] | None = None) -> Iterator[dict]:
    """Compatibility view: fixed historical archive is NOT today's live signal.

    Live workers should obtain fresh closed history + quote and call
    make_live_candidate. This iterator intentionally blocks archive-only use.
    """
    selected_day = today_date or day or now_local().date().isoformat()
    date.fromisoformat(selected_day)
    for item in iter_historical_candidates(cancel=cancel):
        item["simulation_date"] = selected_day
        item["execution_bar"] = None
        item["status"] = "blocked_not_ready"
        item["missing"] = ["固定历史档案不能自动冒充今日前序日线；须先刷新已收盘日线及当前报价。"]
        for plan in item["plans"].values():
            plan.update(action="hold", eligible=False, entry_low=None, entry_high=None, position_pct=None,
                        stop_loss=None, take_profit=None, missing=[*plan["missing"], *item["missing"]],
                        reason="；".join(item["missing"]))
        yield item
