"""Persistent, owner-scoped paper accounts. No broker, network or model calls.

The historical execution model is intentionally an *estimate* at a real next
session opening price. It does not claim a queued exchange order was filled.
Research freezes remain separate and unchanged. All monetary arithmetic is in
integer cents; a single SQLite transaction owns each account/ledger change.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import zlib

from . import livermore_data

ROOT = Path(__file__).resolve().parents[1]
SEATS = ("baseline", "livermore", "buffett")
MODES = ("historical", "live")
INITIAL_CENTS = 10_000_000
MAX_POSITIONS = 5
MAX_STOCK_PCT = Decimal("15")
MAX_EVENT_SAMPLES = 100
VERSION = "paper-account/1"
CN = timezone(timedelta(hours=8))
_lock = threading.RLock()
_database_context = threading.local()
CODE = re.compile(r"(?:000|001|002|003|600|601|603|605)\d{3}\Z")
POLICY = {"version": VERSION, "initial_cash": 100000, "lot_size": 100,
          "max_positions": MAX_POSITIONS, "max_stock_pct": 15,
          "commission_pct": .03, "min_commission": 5, "sell_tax_pct": .05,
          "t_plus_one": True, "historical_fill": "next_session_open_estimate",
          "live_fill": "next_observed_quote_estimate", "no_broker": True}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


class PaperError(Exception):
    """Safe storage failure, with no raw paths or database contents."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def _owner(username: str) -> str:
    if not isinstance(username, str) or not username:
        raise ValueError("A verified account is required")
    return hashlib.sha256(("paper-owner/1:" + username).encode()).hexdigest()


def _params(username, mode, day):
    if mode not in MODES or not isinstance(day, str):
        raise ValueError("Invalid paper account mode/date")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Invalid paper account date") from None
    return _owner(username), mode, day


def db_path() -> Path:
    bound = getattr(_database_context, "path", None)
    if bound is not None:
        return bound
    return Path(os.environ.get("CONTEST_PAPER_DB") or ROOT / ".paper-accounts.sqlite3").resolve()


@contextmanager
def connection():
    try:
        with _lock:
            path = db_path()
            if not path.exists():
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,
                  started_at TEXT NOT NULL,updated_at TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,
                  processed INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY(owner,mode,day));
                CREATE TABLE IF NOT EXISTS accounts(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,
                  cash INTEGER NOT NULL,realized INTEGER NOT NULL DEFAULT 0,fees INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(owner,mode,seat));
                CREATE TABLE IF NOT EXISTS daily_nav(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,day TEXT NOT NULL,opening_nav INTEGER NOT NULL,
                  PRIMARY KEY(owner,mode,seat,day));
                CREATE TABLE IF NOT EXISTS positions(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,code TEXT NOT NULL,name TEXT NOT NULL,
                  qty INTEGER NOT NULL,cost INTEGER NOT NULL,entry_price INTEGER NOT NULL,entry_day TEXT NOT NULL,
                  mark INTEGER NOT NULL,mark_at TEXT NOT NULL,stop INTEGER,take INTEGER,
                  decision_id TEXT NOT NULL,source_hash TEXT NOT NULL,
                  PRIMARY KEY(owner,mode,seat,code));
                CREATE TABLE IF NOT EXISTS orders(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,
                  day TEXT NOT NULL,code TEXT NOT NULL,name TEXT NOT NULL,side TEXT NOT NULL,
                  event_time TEXT NOT NULL,captured_at TEXT NOT NULL,price INTEGER NOT NULL,qty INTEGER NOT NULL,
                  amount INTEGER NOT NULL,fee INTEGER NOT NULL,reason TEXT NOT NULL,decision_id TEXT NOT NULL,
                  source_hash TEXT NOT NULL,policy_hash TEXT NOT NULL,execution_assumption TEXT NOT NULL,
                  UNIQUE(owner,mode,seat,day,code,side,decision_id));
                CREATE INDEX IF NOT EXISTS order_owner_date ON orders(owner,mode,day,id);
                CREATE TABLE IF NOT EXISTS processed(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,code TEXT NOT NULL,
                  PRIMARY KEY(owner,mode,day,code));
                CREATE TABLE IF NOT EXISTS counts(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,seat TEXT NOT NULL,kind TEXT NOT NULL,count INTEGER NOT NULL,
                  PRIMARY KEY(owner,mode,day,seat,kind));
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,
                  seat TEXT NOT NULL,code TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_owner_date ON events(owner,mode,day,id);
                CREATE TABLE IF NOT EXISTS decisions(
                  owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,seat TEXT NOT NULL,code TEXT NOT NULL,
                  candidate_json TEXT NOT NULL,created_at TEXT NOT NULL,last_quote_at TEXT,
                  PRIMARY KEY(owner,mode,day,seat,code));
                CREATE TABLE IF NOT EXISTS session_meta(owner TEXT NOT NULL,mode TEXT NOT NULL,day TEXT NOT NULL,value_json TEXT NOT NULL,PRIMARY KEY(owner,mode,day));
                CREATE TABLE IF NOT EXISTS day_snapshots(owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,day TEXT NOT NULL,value_json TEXT NOT NULL,PRIMARY KEY(owner,mode,seat,day));
                CREATE TABLE IF NOT EXISTS position_sessions(owner TEXT NOT NULL,mode TEXT NOT NULL,seat TEXT NOT NULL,code TEXT NOT NULL,day TEXT NOT NULL,PRIMARY KEY(owner,mode,seat,code,day));
                """)
                # executescript commits its schema transaction; start the one
                # accounting transaction only after schema preparation.
                conn.execute("BEGIN IMMEDIATE")
                yield conn
    except (sqlite3.Error, OSError) as exc:
        raise PaperError("模拟账户存储暂不可用，操作未完成。") from exc


def _session(conn, owner, mode, day):
    row = conn.execute("SELECT * FROM sessions WHERE owner=? AND mode=? AND day=?", (owner, mode, day)).fetchone()
    if not row:
        return None
    value = dict(row)
    value.pop("owner", None)
    value["simulation_date"] = value.pop("day")
    extra = conn.execute("SELECT value_json FROM session_meta WHERE owner=? AND mode=? AND day=?", (owner, mode, day)).fetchone()
    if extra:
        value.update(json.loads(extra[0]))
    value["enabled"] = value["status"] in ({"running", "waiting", "blocked"} if mode == "live" else {"running"})
    return value


def _equity(conn, owner, mode, seat) -> int:
    cash = conn.execute("SELECT cash FROM accounts WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()[0]
    value = conn.execute("SELECT COALESCE(SUM(qty*mark),0) FROM positions WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()[0]
    return cash + value


def _snapshot_day(conn, owner, mode, day):
    """Freeze account/position summaries for later calendar-date queries."""
    for row in conn.execute("SELECT * FROM accounts WHERE owner=? AND mode=?", (owner, mode)).fetchall():
        seat = row["seat"]
        held = conn.execute("SELECT * FROM positions WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchall()
        equity = row["cash"]+sum(p["qty"]*p["mark"] for p in held)
        nav = conn.execute("SELECT opening_nav FROM daily_nav WHERE owner=? AND mode=? AND seat=? AND day=?", (owner, mode, seat, day)).fetchone()
        opening = nav[0] if nav else None
        counts = {value["kind"]: value["count"] for value in conn.execute("SELECT kind,count FROM counts WHERE owner=? AND mode=? AND day=? AND seat=?", (owner, mode, day, seat))}
        account = {"seat_id": seat, "initial_cash": INITIAL_CENTS/100, "cash": row["cash"]/100,
                   "market_value": (equity-row["cash"])/100, "equity": equity/100,
                   "opening_equity": opening/100 if opening is not None else None,
                   "daily_pnl": (equity-opening)/100 if opening is not None else None,
                   "daily_return_pct": (equity/opening-1)*100 if opening else None,
                   "total_pnl": (equity-INITIAL_CENTS)/100, "total_return_pct": (equity/INITIAL_CENTS-1)*100,
                   "realized_pnl": row["realized"]/100,"unrealized_pnl":sum(p["mark"]*p["qty"]-p["cost"] for p in held)/100,
                   "fees":row["fees"]/100,"position_count":len(held),"counts":counts}
        positions = [{"seat_id": seat,"code":p["code"],"name":p["name"],"shares":p["qty"],"entry_date":p["entry_day"],
                      "entry_price":p["entry_price"]/100,"cost_including_fees":p["cost"]/100,"mark_price":p["mark"]/100,
                      "mark_at":p["mark_at"],"market_value":p["mark"]*p["qty"]/100,
                      "unrealized_pnl":(p["mark"]*p["qty"]-p["cost"])/100,"sellable_shares":p["qty"] if p["entry_day"]<day else 0,
                      "source_hash":p["source_hash"]} for p in held]
        conn.execute("INSERT OR REPLACE INTO day_snapshots VALUES(?,?,?,?,?)", (owner,mode,seat,day,json.dumps({"account":account,"positions":positions},ensure_ascii=False,allow_nan=False)))


def create_session(username: str, mode: str, day: str) -> dict:
    owner, mode, day = _params(username, mode, day)
    if mode == "live" and day != utcnow().astimezone(CN).date().isoformat():
        raise ValueError("实时模拟只能从本机当前交易日期启用，不能补造过去交易。")
    with connection() as conn:
        existing = _session(conn, owner, mode, day)
        if existing:
            return existing
        now = stamp()
        for seat in SEATS:
            conn.execute("INSERT OR IGNORE INTO accounts(owner,mode,seat,cash) VALUES(?,?,?,?)", (owner, mode, seat, INITIAL_CENTS))
            conn.execute("INSERT INTO daily_nav VALUES(?,?,?,?,?)", (owner, mode, seat, day, _equity(conn, owner, mode, seat)))
        conn.execute("INSERT INTO sessions(owner,mode,day,status,started_at,updated_at) VALUES(?,?,?,'ready',?,?)", (owner, mode, day, now, now))
        return _session(conn, owner, mode, day)


def begin_scan(username: str, mode: str, day: str, total: int = 0) -> dict:
    create_session(username, mode, day)
    owner, mode, day = _params(username, mode, day)
    if type(total) is not int or not 0 <= total <= 10000:
        raise ValueError("Invalid bounded universe size")
    with connection() as conn:
        row = _session(conn, owner, mode, day)
        if row["status"] == "completed":
            return row
        if mode == "live" and row["status"] not in {"running", "waiting"}:
            prior = conn.execute("SELECT value_json FROM session_meta WHERE owner=? AND mode=? AND day=?", (owner,mode,day)).fetchone()
            meta = json.loads(prior[0]) if prior else {}
            meta["active_since"] = stamp()
            conn.execute("INSERT OR REPLACE INTO session_meta VALUES(?,?,?,?)", (owner,mode,day,json.dumps(meta)))
        conn.execute("UPDATE sessions SET status='running',total=MAX(total,?),updated_at=?,detail='' WHERE owner=? AND mode=? AND day=?", (total, stamp(), owner, mode, day))
        return _session(conn, owner, mode, day)


def finish_scan(username: str, mode: str, day: str, status: str = "completed", detail: str = "") -> dict:
    owner, mode, day = _params(username, mode, day)
    if status not in {"completed", "stopped", "error", "waiting"}:
        raise ValueError("Invalid scan status")
    with connection() as conn:
        row = _session(conn, owner, mode, day)
        if row and status == "completed" and row["status"] != "running":
            return row
        if row and row["status"] in {"stopped", "paused"} and status != "stopped":
            return row
        if row and row["status"] != "completed":
            conn.execute("UPDATE sessions SET status=?,updated_at=?,detail=? WHERE owner=? AND mode=? AND day=?", (status, stamp(), str(detail)[:500], owner, mode, day))
            _snapshot_day(conn, owner, mode, day)
        return _session(conn, owner, mode, day)


def stop_session(username: str, mode: str, day: str) -> dict:
    return finish_scan(username, mode, day, "stopped", "已暂停自动模拟，原订单与持仓保留。")


def update_session(username: str, mode: str, day: str, **fields) -> dict:
    owner, mode, day = _params(username, mode, day)
    allowed = {"status", "detail", "message", "total", "processed", "last_run", "last_quote_at", "scan_cursor", "reason"}
    if set(fields)-allowed:
        raise ValueError("Unsupported session state field")
    with connection() as conn:
        row = _session(conn, owner, mode, day)
        if row is None or mode == "historical" and row["status"] == "completed":
            return row
        for key in ("total", "processed"):
            if key in fields and (type(fields[key]) is not int or not 0 <= fields[key] <= 10000):
                raise ValueError("Invalid bounded progress")
        if "status" in fields and fields["status"] not in {"ready", "running", "waiting", "blocked", "paused", "stopped", "error", "completed"}:
            raise ValueError("Invalid session status")
        updates = {key: fields[key] for key in ("status", "total", "processed") if key in fields}
        if "detail" in fields or "message" in fields:
            updates["detail"] = str(fields.get("detail", fields.get("message")))[:500]
        updates["updated_at"] = stamp()
        conn.execute("UPDATE sessions SET " + ",".join(key+"=?" for key in updates) + " WHERE owner=? AND mode=? AND day=?", (*updates.values(), owner, mode, day))
        existing = conn.execute("SELECT value_json FROM session_meta WHERE owner=? AND mode=? AND day=?", (owner, mode, day)).fetchone()
        extra = json.loads(existing[0]) if existing else {}
        extra.update({key: str(value)[:120] for key, value in fields.items() if key in {"last_run", "last_quote_at", "scan_cursor", "reason"}})
        conn.execute("INSERT OR REPLACE INTO session_meta VALUES(?,?,?,?)", (owner, mode, day, json.dumps(extra, ensure_ascii=False)))
        return _session(conn, owner, mode, day)


def recover_after_restart() -> int:
    """Stop orphaned workers; never reset money, decisions or order history."""
    with connection() as conn:
        return conn.execute("UPDATE sessions SET status='paused',updated_at=?,detail='服务重启，自动任务已暂停；账户和成交保留，请主动恢复。' WHERE status IN ('running','waiting','blocked')", (stamp(),)).rowcount


def _cents(value) -> int | None:
    if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= 1_000_000:
        return None
    amount = Decimal(str(value)) * 100
    rounded = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rounded) if abs(amount-rounded) <= Decimal("0.000001") else None


def _fee(amount: int, *, sell=False) -> int:
    commission = max(500, int((Decimal(amount)*Decimal("0.0003")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    tax = int((Decimal(amount)*Decimal("0.0005")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if sell else 0
    return commission + tax


def _event(conn, owner, mode, day, seat, code, name, kind, reason):
    conn.execute("INSERT INTO counts VALUES(?,?,?,?,?,1) ON CONFLICT(owner,mode,day,seat,kind) DO UPDATE SET count=count+1", (owner, mode, day, seat, kind))
    count = conn.execute("SELECT COUNT(*) FROM events WHERE owner=? AND mode=? AND day=? AND seat=?", (owner, mode, day, seat)).fetchone()[0]
    if count < MAX_EVENT_SAMPLES:
        conn.execute("INSERT INTO events(owner,mode,day,seat,code,name,kind,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (owner, mode, day, seat, code[:20], name[:100], kind, str(reason)[:800], stamp()))


def _candidate_error(candidate: dict, day: str) -> str | None:
    code, name = candidate.get("code"), candidate.get("name")
    if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(name, str) or not name.strip() or "ST" in name.upper() or "退" in name:
        return "代码或名称不符合沪深主板非ST范围。"
    try:
        decision = date.fromisoformat(candidate.get("decision_date", ""))
        history = date.fromisoformat(candidate.get("history_end", ""))
        if not history <= decision < date.fromisoformat(day):
            return "行情历史与决策时点不一致，不能使用模拟日或未来行情选股。"
    except (ValueError, TypeError):
        return "缺少有效决策日期与历史截止日期。"
    if not isinstance(candidate.get("source_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", candidate["source_hash"]):
        return "缺少可核对的行情来源指纹。"
    return None


def _bar_error(bar, day):
    if not isinstance(bar, dict) or bar.get("date") != day:
        return "模拟交易日没有有效真实日线，不补造停牌或价格。"
    prices = [_cents(bar.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in prices):
        return "真实日线价格无效或未对齐分刻度。"
    op, hi, lo, cl = prices
    if not 0 < lo <= min(op, cl) <= max(op, cl) <= hi or type(bar.get("volume")) not in (int, float) or not math.isfinite(bar["volume"]) or bar["volume"] <= 0:
        return "真实日线OHLC或成交量无效，不推断成交。"
    if hi == lo:
        return "一字价格日线无法证明可成交，保守拒绝模拟入场。"
    return None


def _decision_id(candidate, seat, plan):
    return hashlib.sha256(json.dumps({"code": candidate["code"], "seat": seat, "decision_date": candidate["decision_date"], "source_hash": candidate["source_hash"], "plan": plan}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _buy(conn, owner, mode, day, seat, candidate, plan, price: int, event_time: str, mark: int, mark_at: str, assumption: str,
         *, budget_cap_cents: int | None = None, execution_reason: str = ""):
    code, name = candidate["code"], candidate["name"]
    if seat == "livermore" and mode == "live":
        return "data_rejected", "利弗莫尔实时Z1及跌停后次开退出尚未完整实现，禁止新开仓；原持仓仅估值。"
    # The data gate is independent of caller/model-provided eligibility flags.
    # A future caller that forgets the gate cannot accidentally restore the
    # generic 15% budget for this seat.
    if seat == "livermore" and (type(budget_cap_cents) is not int or budget_cap_cents not in {350_000, 500_000}):
        return "data_rejected", "利弗莫尔缺少独立竞价门控预算，不创建模拟买单。"
    if budget_cap_cents is not None and (type(budget_cap_cents) is not int or budget_cap_cents <= 0):
        return "data_rejected", "独立入场预算无效，不创建模拟买单。"
    if conn.execute("SELECT 1 FROM positions WHERE owner=? AND mode=? AND seat=? AND code=?", (owner, mode, seat, code)).fetchone():
        return "already_held", "该股票已有持仓，不重复加仓。"
    count = conn.execute("SELECT COUNT(*) FROM positions WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()[0]
    if count >= MAX_POSITIONS:
        return "position_cap", "已达5只持仓上限，仍继续扫描并记录其他规则判断。"
    low, high = _cents(plan.get("entry_low")), _cents(plan.get("entry_high"))
    pct = plan.get("position_pct")
    if low is None or high is None or low > high or type(pct) not in (int, float) or not math.isfinite(pct) or not 0 < pct <= 20:
        return "rejected", "预案入场范围或仓位无效，不创建订单。"
    if not low <= price <= high:
        return "price_rejected", "真实开盘/当前报价不在冻结入场区间，不追价或回填盘中低点。"
    account = conn.execute("SELECT * FROM accounts WHERE owner=? AND mode=? AND seat=?", (owner, mode, seat)).fetchone()
    # Starting capital, not gains from this execution day's future close, sets
    # the allocation. An earlier stock's closing P&L never sizes later buys.
    budget = min(account["cash"], int(Decimal(INITIAL_CENTS)*min(MAX_STOCK_PCT, Decimal(str(pct)))/100))
    if budget_cap_cents is not None:
        budget = min(budget, budget_cap_cents)
    qty = budget // (price*100)*100
    while qty and price*qty + _fee(price*qty) > budget:
        qty -= 100
    if not qty:
        return "cash_rejected", "剩余虚拟现金含费用不足100股，不借款或透支。"
    amount, fee = price*qty, _fee(price*qty)
    decision_id = _decision_id(candidate, seat, plan)
    if conn.execute("SELECT 1 FROM orders WHERE owner=? AND mode=? AND seat=? AND day=? AND code=? AND side='buy' AND decision_id=?", (owner, mode, seat, day, code, decision_id)).fetchone():
        return "duplicate", "这条冻结预案已经记录过模拟买入。"
    conn.execute("UPDATE accounts SET cash=cash-?,fees=fees+? WHERE owner=? AND mode=? AND seat=?", (amount+fee, fee, owner, mode, seat))
    conn.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (owner, mode, seat, code, name, qty, amount+fee, price, day, mark, mark_at, _cents(plan.get("stop_loss")), _cents(plan.get("take_profit")), decision_id, candidate["source_hash"]))
    reason = str(plan.get("reason") or "按冻结条件进行虚拟入场。")
    if execution_reason:
        # Execution provenance is recorded separately, not inserted into the
        # immutable decision plan/source fingerprint.
        reason = reason[:400] + " " + execution_reason[:300]
    if mode == "live":
        reason = reason[:600]+f" 报价估算买入价{price/100:.2f}元，观察成交价{mark/100:.2f}元，价差{(price-mark)/100:+.2f}元/股；盘口深度与真实成交未验证。"
    conn.execute("INSERT INTO orders(owner,mode,seat,day,code,name,side,event_time,captured_at,price,qty,amount,fee,reason,decision_id,source_hash,policy_hash,execution_assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (owner, mode, seat, day, code, name, "buy", event_time, stamp(), price, qty, amount, fee, reason[:800], decision_id, candidate["source_hash"], POLICY_HASH, assumption))
    return "buy", f"按真实价格估算模拟买入{qty}股；费用{fee/100:.2f}元，当日不可卖出。"


def _load_livermore_candidate(code):
    """Read authoritative public archive, never a caller-supplied metric/plan."""
    from . import historical_seats, market_history, paper_market

    case = market_history.load_case("history-20261113-" + code)
    snapshot = historical_seats.read_evidence(case, "livermore")
    return case, snapshot, paper_market._plan(case, "livermore", snapshot)


def _livermore_entry(candidate, plan, day, opening_price, *, mode, frozen_at=None, quote_asof=None):
    """Run the local evidence gate again immediately before account mutation."""
    if mode == "live":
        return None, "利弗莫尔实时Z1及跌停后次开退出尚未完整实现，禁止新开仓；原持仓仅估值。"
    # A signed/hash-valid auction says nothing about the 120-day candidate.
    # Independently reload the indexed daily archive and regenerate its plan;
    # no caller's eligible=True or self-reported low position can authorize it.
    try:
        from fastapi import HTTPException

        case, snapshot, expected = _load_livermore_candidate(candidate["code"])
        cutoff = case["decision_date"]
        metrics = snapshot["metrics"]
        position = metrics.get("range_position_120_pct")
        prior = _cents(metrics.get("last_close"))
        if (case["symbol"] != candidate["code"] or candidate["name"] != case["name"]
                or cutoff != candidate["decision_date"] or snapshot["decision_date"] != cutoff
                or candidate["history_end"] != cutoff or not cutoff < day
                or candidate["source_hash"] != case["archive_sha256"]
                or prior is None or _cents(candidate.get("prior_close")) != prior
                or metrics.get("data_valid") is not True or metrics.get("eligible") is not True
                or type(position) not in (int, float) or not math.isfinite(position) or not 0 <= position < 10
                or expected.get("action") != "simulate" or expected.get("eligible") is not True or expected.get("missing")):
            return None, "独立重读历史档案后，原决策日、120日低位候选或前收不一致，不创建买单。"
        fields = ("action", "entry_low", "entry_high", "position_pct", "stop_loss", "take_profit",
                  "rule_version", "livermore_evidence_hash", "entry_policy", "requires_opening_auction")
        if any(key not in plan or key not in expected or plan[key] != expected[key] for key in fields):
            return None, "利弗莫尔预案与独立重算的冻结参数或证据/规则版本不一致，不创建买单。"
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, ArithmeticError, HTTPException):
        return None, "利弗莫尔原始历史档案不可核验，拒绝采用外部自报候选或计划。"
    gate = livermore_data.entry_gate(candidate["code"], candidate["decision_date"], day,
                                    candidate.get("prior_close"), opening_price, mode=mode,
                                    frozen_at=frozen_at, quote_asof=quote_asof)
    cap = _cents(gate.get("budget_yuan"))
    if gate.get("allowed") is not True or cap not in {350_000, 500_000}:
        return None, str(gate.get("reason") or "独立竞价证据与预算未通过，不创建买单。")[:600]
    reason = str(gate.get("reason") or "独立竞价门控通过。")[:200]
    fingerprint = gate.get("source_hash")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        return None, "独立竞价门控缺少执行证据指纹，不创建买单。"
    return cap, reason + f" 执行证据指纹：{fingerprint}。"


def process_candidate(username: str, mode: str, day: str, candidate: dict) -> dict:
    owner, mode, day = _params(username, mode, day)
    if mode != "historical" or not isinstance(candidate, dict):
        raise ValueError("Historical candidate required")
    code, name = str(candidate.get("code", ""))[:20], str(candidate.get("name", ""))[:100]
    with connection() as conn:
        session = _session(conn, owner, mode, day)
        if not session or session["status"] != "running":
            return {"accepted": False, "reason": "not_running"}
        if not conn.execute("INSERT OR IGNORE INTO processed VALUES(?,?,?,?)", (owner, mode, day, code)).rowcount:
            return {"accepted": False, "reason": "duplicate"}
        error = _candidate_error(candidate, day)
        bar = candidate.get("execution_bar")
        bar_error = _bar_error(bar, day)
        outcomes = {}
        for seat in SEATS:
            plan = (candidate.get("plans") or {}).get(seat) or {}
            reason = plan.get("reason") or "缺少该席位的可执行预案。"
            if error:
                kind, reason = "data_rejected", error
            elif plan.get("action") != "simulate" or plan.get("eligible") is False or plan.get("missing"):
                kind = "hold"
                if plan.get("missing"):
                    reason = str(reason) + " 缺失：" + "；".join(str(value) for value in plan["missing"][:5])
            elif bar_error:
                kind, reason = "data_rejected", bar_error
            else:
                cap, execution_reason = (None, "")
                if seat == "livermore":
                    cap, execution_reason = _livermore_entry(candidate, plan, day, bar["open"], mode=mode)
                if seat == "livermore" and cap is None:
                    kind, reason = "data_rejected", execution_reason
                else:
                    kind, reason = _buy(conn, owner, mode, day, seat, candidate, plan, _cents(bar["open"]), day+"T09:30:00+08:00", _cents(bar["close"]), day+"T15:00:00+08:00", "historical_open_estimate", budget_cap_cents=cap, execution_reason=execution_reason)
            _event(conn, owner, mode, day, seat, code, name, kind, reason)
            outcomes[seat] = kind
        conn.execute("UPDATE sessions SET processed=processed+1,updated_at=? WHERE owner=? AND mode=? AND day=?", (stamp(), owner, mode, day))
        return {"accepted": True, "outcomes": outcomes}


def run_historical(username: str, candidates, day="2026-11-13", total=0, stop_check=None) -> dict:
    # A worker may outlive cancellation of its awaiting coroutine. Bind the
    # chosen DB once so a later configuration/test-context change cannot make
    # an in-flight scan write to another user's/default runtime database.
    previous = getattr(_database_context, "path", None)
    _database_context.path = db_path()
    try:
        return _run_historical_bound(username, candidates, day, total, stop_check)
    finally:
        _database_context.path = previous


def _run_historical_bound(username, candidates, day, total, stop_check):
    value = begin_scan(username, "historical", day, total)
    if value["status"] == "completed":
        return value
    try:
        for candidate in candidates:
            if stop_check and stop_check():
                return stop_session(username, "historical", day)
            accepted = process_candidate(username, "historical", day, candidate)
            if not accepted["accepted"] and accepted.get("reason") == "not_running":
                with connection() as conn:
                    return _session(conn, _owner(username), "historical", day)
    except Exception:
        finish_scan(username, "historical", day, "error", "扫描中断，已记录订单保留；继续后不会重复成交。")
        raise
    if stop_check and stop_check():
        return stop_session(username, "historical", day)
    with connection() as conn:
        state = _session(conn, _owner(username), "historical", day)
    if state["status"] != "running":
        return state
    if state["total"] and state["processed"] < state["total"]:
        return finish_scan(username, "historical", day, "error", "目录尚未扫描完成，已保存的订单保留；可继续扫描，不重复成交。")
    return finish_scan(username, "historical", day, "completed", "已按固定代码顺序扫描真实股票；模拟开盘估算成交，收盘只估值，未违反T+1卖出。")


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def set_live_decisions(username: str, candidates: list[dict], day: str) -> int:
    """Freeze at most fifty real, pre-day candidate plans for a future quote.

    This performs no fill. A separately supplied fresh quote must follow the
    decision creation time; missing data remains hold rather than guessed.
    """
    owner, mode, day = _params(username, "live", day)
    if not isinstance(candidates, list) or len(candidates) > 50:
        raise ValueError("At most fifty live decision candidates")
    inserted = 0
    with connection() as conn:
        session = _session(conn, owner, mode, day)
        if not session or not session["enabled"]:
            return 0
        for candidate in candidates:
            if _candidate_error(candidate, day):
                continue
            for seat in SEATS:
                plan = (candidate.get("plans") or {}).get(seat) or {}
                safe = {key: candidate[key] for key in ("code", "name", "decision_date", "history_end", "source_hash")}
                safe["prior_close"] = candidate.get("prior_close", candidate.get("reference_close"))
                safe["plan"] = plan
                body = json.dumps(safe, ensure_ascii=False, allow_nan=False)
                if len(body.encode()) > 12000:
                    continue
                inserted += conn.execute("INSERT OR IGNORE INTO decisions(owner,mode,day,seat,code,candidate_json,created_at) VALUES(?,?,?,?,?,?,?)", (owner, mode, day, seat, candidate["code"], body, stamp())).rowcount
        return inserted


def record_live_candidate(username: str, day: str, candidate: dict, created_at=None) -> int:
    # A caller cannot set an earlier timestamp to make an already observed
    # quote appear to follow a fresh plan. Creation is always server-now.
    if created_at is not None:
        value = _parse_time(created_at)
        if value is None or abs((utcnow()-value).total_seconds()) > 2:
            raise ValueError("Decision time must be current; no backdating")
    return set_live_decisions(username, [candidate], day)


def _sell(conn, owner, day, seat, position, price, event_time, reason, reference_price=None):
    if position["entry_day"] >= day:
        return False
    amount = price*position["qty"]
    fee = _fee(amount, sell=True)
    if reference_price is not None:
        reason += f" 模拟卖价{price/100:.2f}元，观察成交价{reference_price/100:.2f}元，价差{(price-reference_price)/100:+.2f}元/股；并非真实成交证明。"
    conn.execute("UPDATE accounts SET cash=cash+?,realized=realized+?,fees=fees+? WHERE owner=? AND mode='live' AND seat=?", (amount-fee, amount-fee-position["cost"], fee, owner, seat))
    conn.execute("INSERT INTO orders(owner,mode,seat,day,code,name,side,event_time,captured_at,price,qty,amount,fee,reason,decision_id,source_hash,policy_hash,execution_assumption) VALUES(?,'live',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (owner, seat, day, position["code"], position["name"], "sell", event_time, stamp(), price, position["qty"], amount, fee, reason, position["decision_id"], position["source_hash"], POLICY_HASH, "live_last_quote_estimate"))
    conn.execute("DELETE FROM positions WHERE owner=? AND mode='live' AND seat=? AND code=?", (owner, seat, position["code"]))
    _event(conn, owner, "live", day, seat, position["code"], position["name"], "sell", reason)
    return True


def apply_quote(username: str, quote: dict, *, now: datetime | None = None) -> dict:
    """One fresh, chronological live observation; never replay old quotes now.

    A last-price paper fill is an explicitly approximate simulator, not proof
    that a real broker order could obtain that price or queue position.
    """
    now = now or utcnow()
    instant = _parse_time(quote.get("timestamp", quote.get("asof"))) if isinstance(quote, dict) else None
    code = quote.get("code", "") if isinstance(quote, dict) else ""
    price = _cents(quote.get("price")) if isinstance(quote, dict) else None
    if instant is None or not isinstance(code, str) or not CODE.fullmatch(code) or price is None:
        return {"accepted": False, "reason": "invalid_quote"}
    local, current = instant.astimezone(CN), now.astimezone(CN)
    clock = lambda value: value.time().replace(tzinfo=None)
    session_time = lambda value: value.weekday() < 5 and (time(9,30) <= clock(value) <= time(11,30) or time(13) <= clock(value) <= time(15))
    age = (now-instant).total_seconds()
    closing = quote.get("closing_only") is True and local.date()==current.date() and current.weekday()<5 and time(15)<=clock(current)<=time(15,1,30) and time(15)<=clock(local)<=time(15,0,59) and 0<=age<=60
    if quote.get("closing_only") is True and not closing:
        return {"accepted": False, "reason": "invalid_closing_observation"}
    if local.date() != current.date() or not closing and (not session_time(local) or not session_time(current)) or not 0 <= age <= 90:
        return {"accepted": False, "reason": "closed_or_stale_quote"}
    volume = quote.get("volume_shares", quote.get("volume"))
    if quote.get("executable") is False and not closing:
        return {"accepted": False, "reason": "quote_not_executable"}
    if type(volume) not in (int, float) or not math.isfinite(volume) or volume <= 0:
        return {"accepted": False, "reason": "invalid_volume"}
    if _cents(quote.get("high")) is None or _cents(quote.get("low")) is None or quote["high"] <= quote["low"] or not quote["low"] <= quote["price"] <= quote["high"]:
        return {"accepted": False, "reason": "one_price_or_invalid_range"}
    day = local.date().isoformat()
    owner, mode, day = _params(username, "live", day)
    with connection() as conn:
        session = _session(conn, owner, mode, day)
        if not session or not session["enabled"] or instant <= max(_parse_time(session["started_at"]),_parse_time(session.get("active_since")) or _parse_time(session["started_at"])):
            return {"accepted": False, "reason": "not_running_or_before_start"}
        # A held security must still be marked even if no new buy decision is
        # present for it today. Reject backwards marks using persisted times.
        positions = conn.execute("SELECT * FROM positions WHERE owner=? AND mode='live' AND code=?", (owner, code)).fetchall()
        for position in positions:
            if instant <= (_parse_time(position["mark_at"]) or instant-timedelta(seconds=1)):
                continue
            conn.execute("UPDATE positions SET mark=?,mark_at=? WHERE owner=? AND mode='live' AND seat=? AND code=?", (price, stamp(instant), owner, position["seat"], code))
            conn.execute("INSERT OR IGNORE INTO position_sessions VALUES(?,'live',?,?,?)", (owner, position["seat"], code, day))
            # Original research uses closing-price exits. Do not silently turn
            # its protection/target into an intraday stop-loss strategy.
            if local.hour == 15 and local.minute == 0 and position["entry_day"] < day:
                if position["seat"] == "livermore":
                    # Generic deadline selling is not the original Z1 / next
                    # executable-open policy. Until its persisted state machine
                    # exists, marking an old holding must not invent an exit.
                    _event(conn, owner, mode, day, position["seat"], code, position["name"], "hold",
                           "利弗莫尔Z1涨停顺延及跌停后可交易开盘退出尚未完整接入；仅更新估值，不套用通用到期卖出。")
                    continue
                stop, take = position["stop"], position["take"]
                observed = conn.execute("SELECT COUNT(*) FROM position_sessions WHERE owner=? AND mode='live' AND seat=? AND code=? AND day>?", (owner, position["seat"], code, position["entry_day"])).fetchone()[0]
                max_sessions = {"baseline": 3, "livermore": 1, "buffett": None}[position["seat"]]
                threshold = position["seat"] == "baseline" and (stop is not None and price <= stop or take is not None and price >= take)
                deadline = max_sessions is not None and observed >= max_sessions
                if threshold or deadline:
                    bid = _cents(quote.get("bid"))
                    sell_price = bid if bid is not None and _cents(quote["low"]) <= bid <= _cents(quote["high"]) else price
                    lower = _cents(quote.get("limit_down"))
                    if lower is None or sell_price > lower:
                        _sell(conn, owner, day, position["seat"], position, sell_price, stamp(instant), "收盘报价触发保护/目标或最长已观察持有交易日，按T+1报价估算模拟卖出。", reference_price=price)
        rows = conn.execute("SELECT * FROM decisions WHERE owner=? AND mode='live' AND day=? AND code=?", (owner, day, code)).fetchall()
        outcomes = {}
        for row in rows:
            previous = _parse_time(row["last_quote_at"])
            if instant <= _parse_time(row["created_at"]) or previous and instant <= previous:
                continue
            candidate = json.loads(row["candidate_json"])
            plan = candidate.pop("plan")
            prior = _cents(candidate.get("prior_close"))
            supplied_prior = _cents(quote.get("prev_close"))
            if prior is None or supplied_prior != prior:
                kind, reason = "data_rejected", "当前报价前收与冻结历史末收不一致，可能除权或数据过旧，不创建买入。"
            elif local.time().replace(tzinfo=None) >= time(15):
                kind, reason = "hold", "收盘后报价只更新估值或处理已持仓收盘规则，不新开仓。"
            elif plan.get("action") != "simulate" or plan.get("eligible") is False or plan.get("missing"):
                kind, reason = "hold", plan.get("reason") or "席位证据不足，观望。"
            else:
                # Public limit bounds are optional but never fabricated. If
                # supplied, prices at either boundary are conservatively denied.
                upper, lower = _cents(quote.get("limit_up")), _cents(quote.get("limit_down"))
                if upper is not None and price >= upper or lower is not None and price <= lower:
                    kind, reason = "price_rejected", "报价触及提供的涨跌停边界，不假定可排队成交。"
                else:
                    ask = _cents(quote.get("ask"))
                    buy_price = ask if ask is not None and _cents(quote["low"]) <= ask <= _cents(quote["high"]) else price
                    same_day = conn.execute("SELECT 1 FROM orders WHERE owner=? AND mode='live' AND seat=? AND day=? AND code=? AND side='buy'", (owner,row["seat"],day,code)).fetchone()
                    if same_day:
                        kind, reason = "already_held", "同一股票当天已尝试成交，不在卖出后回转重复买入。"
                    else:
                        cap, execution_reason = (None, "")
                        if row["seat"] == "livermore":
                            cap, execution_reason = _livermore_entry(candidate, plan, day, quote.get("open"), mode=mode,
                                                                    frozen_at=row["created_at"], quote_asof=quote.get("timestamp", quote.get("asof")))
                        if row["seat"] == "livermore" and cap is None:
                            kind, reason = "data_rejected", execution_reason
                        else:
                            kind, reason = _buy(conn, owner, mode, day, row["seat"], candidate, plan, buy_price, stamp(instant), price, stamp(instant), "live_ask_quote_estimate" if buy_price!=price else "live_last_quote_estimate", budget_cap_cents=cap, execution_reason=execution_reason)
            conn.execute("UPDATE decisions SET last_quote_at=? WHERE owner=? AND mode='live' AND day=? AND seat=? AND code=?", (stamp(instant), owner, day, row["seat"], code))
            _event(conn, owner, mode, day, row["seat"], code, candidate["name"], kind, reason)
            outcomes[row["seat"]] = kind
        conn.execute("UPDATE sessions SET updated_at=? WHERE owner=? AND mode='live' AND day=?", (stamp(), owner, day))
        _snapshot_day(conn, owner, mode, day)
        return {"accepted": True, "outcomes": outcomes}


def dashboard(username: str, mode="historical", day: str | None = None, seat_id: str | None = None, limit=20, offset=0) -> dict:
    if mode not in MODES or seat_id is not None and seat_id not in SEATS or type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("Invalid dashboard filter")
    owner = _owner(username)
    with connection() as conn:
        if day is None:
            row = conn.execute("SELECT day FROM sessions WHERE owner=? AND mode=? ORDER BY day DESC LIMIT 1", (owner, mode)).fetchone()
            day = utcnow().astimezone(CN).date().isoformat() if mode == "live" else "2026-11-13"
        _params(username, mode, day)
        session = _session(conn, owner, mode, day)
        rows = conn.execute("SELECT * FROM accounts WHERE owner=? AND mode=? ORDER BY seat", (owner, mode)).fetchall()
        latest = conn.execute("SELECT MAX(day) FROM sessions WHERE owner=? AND mode=?", (owner,mode)).fetchone()[0]
        accounts, positions = [], []
        if not rows:
            accounts = [{"seat_id": seat,"initial_cash":100000.0,"cash":100000.0,"market_value":0.0,"equity":100000.0,"opening_equity":None,"daily_pnl":None,"daily_return_pct":None,"total_pnl":0.0,"total_return_pct":0.0,"realized_pnl":0.0,"unrealized_pnl":0.0,"fees":0.0,"position_count":0,"counts":{}} for seat in SEATS]
        for row in rows:
            seat = row["seat"]
            if latest is not None and day < latest:
                saved = conn.execute("SELECT value_json FROM day_snapshots WHERE owner=? AND mode=? AND seat=? AND day=?", (owner,mode,seat,day)).fetchone()
                if saved:
                    frozen = json.loads(saved[0])
                    accounts.append(frozen["account"])
                    if seat_id is None or seat_id == seat:
                        positions.extend(frozen["positions"])
                else:
                    accounts.append({"seat_id":seat,"initial_cash":100000.0,"cash":None,"market_value":None,"equity":None,"opening_equity":None,"daily_pnl":None,"daily_return_pct":None,"total_pnl":None,"total_return_pct":None,"realized_pnl":None,"unrealized_pnl":None,"fees":None,"position_count":0,"counts":{},"snapshot_status":"unavailable"})
                continue
            equity = _equity(conn, owner, mode, seat)
            nav = conn.execute("SELECT opening_nav FROM daily_nav WHERE owner=? AND mode=? AND seat=? AND day=?", (owner, mode, seat, day)).fetchone()
            opening = nav[0] if nav else None
            held = conn.execute("SELECT * FROM positions WHERE owner=? AND mode=? AND seat=? ORDER BY code", (owner, mode, seat)).fetchall()
            unrealized = sum(position["mark"]*position["qty"]-position["cost"] for position in held)
            counts = {value["kind"]: value["count"] for value in conn.execute("SELECT kind,count FROM counts WHERE owner=? AND mode=? AND day=? AND seat=?", (owner, mode, day, seat))}
            accounts.append({"seat_id": seat, "initial_cash": INITIAL_CENTS/100, "cash": row["cash"]/100,
                             "market_value": (equity-row["cash"])/100, "equity": equity/100,
                             "opening_equity": opening/100 if opening is not None else None, "daily_pnl": (equity-opening)/100 if opening is not None else None,
                             "daily_return_pct": (equity/opening-1)*100 if opening else None,
                             "total_pnl": (equity-INITIAL_CENTS)/100, "total_return_pct": (equity/INITIAL_CENTS-1)*100,
                             "realized_pnl": row["realized"]/100, "unrealized_pnl": unrealized/100,
                             "fees": row["fees"]/100, "position_count": len(held), "counts": counts})
            for position in held:
                if seat_id is None or seat_id == seat:
                    positions.append({"seat_id": seat, "code": position["code"], "name": position["name"], "shares": position["qty"],
                                      "entry_date": position["entry_day"], "entry_price": position["entry_price"]/100,
                                      "cost_including_fees": position["cost"]/100, "mark_price": position["mark"]/100,
                                      "mark_at": position["mark_at"], "market_value": position["mark"]*position["qty"]/100,
                                      "unrealized_pnl": (position["mark"]*position["qty"]-position["cost"])/100,
                                      "sellable_shares": position["qty"] if position["entry_day"] < day else 0,
                                      "source_hash": position["source_hash"]})
        suffix = " AND seat=?" if seat_id else ""
        params = (owner, mode, day, *((seat_id,) if seat_id else ()))
        order_total = conn.execute("SELECT COUNT(*) FROM orders WHERE owner=? AND mode=? AND day=?"+suffix, params).fetchone()[0]
        orders = []
        for row in conn.execute("SELECT * FROM orders WHERE owner=? AND mode=? AND day=?"+suffix+" ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)):
            value = dict(row)
            value.pop("owner")
            value["seat_id"] = value.pop("seat")
            for key in ("price", "amount", "fee"):
                value[key] /= 100
            value["shares"] = value.pop("qty")
            orders.append(value)
        event_total = conn.execute("SELECT COUNT(*) FROM events WHERE owner=? AND mode=? AND day=?"+suffix, params).fetchone()[0]
        events = []
        for row in conn.execute("SELECT * FROM events WHERE owner=? AND mode=? AND day=?"+suffix+" ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)):
            value = dict(row)
            value.pop("owner")
            value["seat_id"] = value.pop("seat")
            events.append(value)
        return {"mode": mode, "simulation_date": day, "session": session, "accounts": accounts, "positions": positions,
                "orders": {"items": orders, "total": order_total, "limit": limit, "offset": offset},
                "events": {"items": events, "total": event_total, "limit": limit, "offset": offset},
                "policy": POLICY, "policy_hash": POLICY_HASH,
                "notice": "全部为独立虚拟账户；历史开盘或实时后续报价估算成交，不代表券商真实成交。历史模拟日期不冒充今日；浮盈亏未实现，同日买入不能卖出。",
                "event_samples_bounded": MAX_EVENT_SAMPLES,
                "daily_pnl_formula": "当前虚拟权益减本模拟日启用时冻结的起始权益，含买卖费用；实时启用晚于开盘时不是全天市场收益。"}
