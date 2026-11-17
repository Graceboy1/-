"""Bounded local Livermore evidence, separated by decision and entry time.

Source records must already have been supplied by an explicitly authorized
importer. Structural checks and hashes do not prove a vendor license, exchange
queue priority, or real execution. GET readers never fetch, approve, or trade.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import zlib
from urllib.parse import parse_qsl, urlsplit

from . import domain, market_history, seat_livermore

SCHEMA = "livermore-evidence/v1"
MAX_INDEX_BYTES, MAX_PROFILE_BYTES, MAX_PROFILES, MAX_RECORDS = 1_000_000, 250_000, 6000, 30
CN = timezone(timedelta(hours=8))
CODE = re.compile(r"(?:000|001|002|003|600|601|603|605)\d{3}")
GROUPS = ("limits", "auctions", "index_auctions", "regions")
HOSTS = {"www.sse.com.cn", "query.sse.com.cn", "www.szse.cn", "docs.static.szse.cn",
         "disc.static.szse.cn", "api.tushare.pro", "datacenter-web.eastmoney.com",
         "push2ex.eastmoney.com", "qt.gtimg.cn"}
LIMIT_MISSING = "缺少决策日精确涨跌停价与当时证券状态的来源记录。"
EXECUTION_MISSING = {"auctions": "缺少当日9:25真实开盘竞价价格与成交额。",
                     "index_auctions": "缺少当日9:25指数竞价涨跌记录。",
                     "regions": "缺少开盘前已形成的明确区域风险确认。"}
LIVE_LIMITATION = "利弗莫尔实时新增买入未开放：Z1顺延和跌停后开盘退出状态机尚未完整实现；原持仓仅估值。"


def directory() -> Path:
    return market_history.directory() / "livermore"


def _day(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid evidence date")
    return value


def _code(value) -> str:
    if not isinstance(value, str) or CODE.fullmatch(value) is None:
        raise ValueError("Unsupported mainboard code")
    return value


def _stamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Missing evidence timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone is required")
    return parsed.astimezone(CN)


def _numeric(value, *, positive=False, maximum=1e15) -> bool:
    return type(value) in (int, float) and abs(value) <= maximum and math.isfinite(value) and (not positive or value > 0)


def _price(value) -> bool:
    return _numeric(value, positive=True, maximum=1e6) and Decimal(str(value)) == Decimal(str(value)).quantize(Decimal(".01"))


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _source(value) -> bool:
    if not isinstance(value, str) or len(value) > 1500 or any(ord(char) < 33 for char in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname in HOSTS and not url.username and not url.password
                and not url.port and not url.fragment and bool(url.path or url.hostname)
                and not any(key.casefold() in {"token", "api_key", "apikey", "key", "access_token", "authorization", "secret"}
                            for key, _ in parse_qsl(url.query)))
    except ValueError:
        return False


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json(path: Path, maximum: int) -> dict:
    if path.stat().st_size > maximum:
        raise ValueError("Oversized evidence metadata")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid evidence metadata")
    return value


def _index() -> dict:
    value = _json(directory() / "index.json", MAX_INDEX_BYTES)
    files = value.get("files")
    if value.get("schema") != SCHEMA or not isinstance(files, dict) or len(files) > MAX_PROFILES:
        raise ValueError("Invalid evidence index")
    for code, digest in files.items():
        _code(code)
        if not _sha(digest):
            raise ValueError("Invalid evidence checksum")
    return files


def _profile(code: str) -> dict | None:
    files = _index()
    if code not in files:
        return None
    root = directory().resolve()
    path = (root / f"{code}.json.gz").resolve()
    if not path.is_relative_to(root) or path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError("Invalid evidence file")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES or hashlib.sha256(raw).hexdigest() != files[code]:
        raise ValueError("Evidence checksum mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("code") != code:
        raise ValueError("Evidence security mismatch")
    records = value.get("records")
    if not isinstance(records, dict) or any(not isinstance(records.get(group, []), list) or len(records.get(group, [])) > MAX_RECORDS for group in GROUPS):
        raise ValueError("Invalid bounded evidence records")
    return value


def _validate_record(group: str, row: dict, code: str) -> dict:
    if not isinstance(row, dict) or row.get("code", code) != code:
        raise ValueError("Wrong evidence identity")
    day = _day(row.get("date"))
    expected_status = "human_confirmed" if group == "regions" else "source_verified"
    if row.get("verification_status") != expected_status or not _source(row.get("source_url")) or not _sha(row.get("source_hash")):
        raise ValueError("Unverified evidence provenance")
    result = {"date": day, "source_url": row["source_url"], "source_hash": row["source_hash"], "verification_status": expected_status}
    if group == "regions":
        available = _stamp(row.get("available_at"))
        if available > datetime.combine(date.fromisoformat(day), time(9, 25), CN) or type(row.get("regional_ok")) is not bool:
            raise ValueError("Unavailable or ambiguous regional confirmation")
        if any(not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > maximum for key, maximum in (("note", 500), ("reviewed_by", 100))):
            raise ValueError("Missing regional review")
        result.update(available_at=available.isoformat(timespec="seconds"), regional_ok=row["regional_ok"], note=row["note"].strip(), reviewed_by=row["reviewed_by"].strip())
        return result
    asof = _stamp(row.get("asof"))
    if asof.date().isoformat() != day:
        raise ValueError("Evidence day differs from source timestamp")
    result["asof"] = asof.isoformat(timespec="seconds")
    if group == "limits":
        if not _price(row.get("limit_up")) or not _price(row.get("limit_down")) or row["limit_down"] >= row["limit_up"]:
            raise ValueError("No exact supplied price limits")
        if any(type(row.get(key)) is not bool for key in ("is_st", "suspended")) or not isinstance(row.get("security_name"), str) or not row["security_name"].strip() or len(row["security_name"]) > 100:
            raise ValueError("Historical security status is missing")
        result.update(limit_up=row["limit_up"], limit_down=row["limit_down"], security_name=row["security_name"].strip(), is_st=row["is_st"], suspended=row["suspended"])
        return result
    if row.get("phase") != "opening_uncrossed" or asof.time().replace(tzinfo=None) != time(9, 25):
        raise ValueError("Not a final 9:25 opening auction observation")
    result["phase"] = "opening_uncrossed"
    if group == "auctions":
        if not _price(row.get("open")) or not _numeric(row.get("auction_amount"), positive=True) or row.get("amount_unit") != "yuan":
            raise ValueError("Missing exact auction amount in yuan")
        result.update(open=row["open"], auction_amount=row["auction_amount"], amount_unit="yuan")
        volume = row.get("auction_volume_shares")
        if volume is not None:
            if not _numeric(volume, positive=True) or not float(volume).is_integer():
                raise ValueError("Invalid auction share count")
            if abs(Decimal(str(row["open"])) * Decimal(str(volume)) - Decimal(str(row["auction_amount"]))) > Decimal(".01"):
                raise ValueError("Auction amount does not match uncrossed price/volume")
            result["auction_volume_shares"] = int(volume)
    elif group == "index_auctions":
        if row.get("symbol") not in {"000001.SH", "399001.SZ"} or not _numeric(row.get("change_pct"), maximum=1000) or row["change_pct"] < -100:
            raise ValueError("Unknown auction benchmark or percentage")
        result.update(symbol=row["symbol"], change_pct=row["change_pct"])
    else:
        raise ValueError("Unknown evidence group")
    return result


def _select(profile: dict, group: str, day: str, code: str) -> dict | None:
    selected = []
    for row in profile["records"].get(group, []):
        if not isinstance(row, dict):
            raise ValueError("Invalid evidence record")
        record_day = _day(row.get("date"))
        if record_day == day:
            selected.append(row)
    if len(selected) > 1:
        raise ValueError("Duplicate evidence day/version; no version selected")
    return _validate_record(group, selected[0], code) if selected else None


def load_decision(code: str, day: str) -> dict:
    """Never read the subsequent day's auction into the candidate signal."""
    result = {"status": "missing", "limits": None, "missing": [LIMIT_MISSING], "source_hash": None}
    try:
        code, day = _code(code), _day(day)
        if not (directory() / "index.json").is_file():
            return result
        profile = _profile(code)
        if profile is None:
            return result
        limits = _select(profile, "limits", day, code)
        if limits is not None:
            result.update(status="verified", limits=limits, missing=[], source_hash=_digest(limits))
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        result.update(status="invalid", missing=["历史涨跌停与证券状态档案校验未通过，未用推算值替代。"])
    return result


def load_execution(code: str, day: str) -> dict:
    result = {"status": "missing", "auction": None, "index_auction": None, "region": None,
              "missing": list(EXECUTION_MISSING.values()), "source_hash": None}
    try:
        code, day = _code(code), _day(day)
        if not (directory() / "index.json").is_file():
            return result
        profile = _profile(code)
        if profile is None:
            return result
        result["missing"] = []
        for group, key in (("auctions", "auction"), ("index_auctions", "index_auction"), ("regions", "region")):
            value = _select(profile, group, day, code)
            result[key] = value
            if value is None:
                result["missing"].append(EXECUTION_MISSING[group])
        result["status"] = "missing" if result["missing"] else "verified"
        result["source_hash"] = _digest({key: result[key] for key in ("auction", "index_auction", "region")})
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        result.update(status="invalid", missing=["开盘竞价、指数或区域记录校验未通过，不采用相邻日或总成交量补齐。"], source_hash=None)
    return result


def entry_gate(code: str, decision_day: str, day: str, previous_close, opening_price, *,
               mode="historical", frozen_at=None, quote_asof=None) -> dict:
    """Recheck original entry discipline, without accessing any account/order."""
    result = {"allowed": False, "budget_yuan": None, "reason": "竞价证据不足，不创建模拟买单。", "missing": [], "source_hash": None}
    try:
        code, decision_day, day = _code(code), _day(decision_day), _day(day)
        if not decision_day < day or mode not in {"historical", "live"} or not _price(previous_close) or not _price(opening_price):
            raise ValueError("Invalid entry dates or prices")
        decision = load_decision(code, decision_day)
        if decision["status"] != "verified":
            result.update(missing=decision["missing"], reason="；".join(decision["missing"]))
            return result
        limits = decision["limits"]
        if limits["is_st"] or limits["suspended"] or "ST" in limits["security_name"].upper() or "退" in limits["security_name"]:
            result["reason"] = "决策日证券处于排除范围，不创建模拟买单。"
            return result
        if Decimal(str(previous_close)) != Decimal(str(limits["limit_up"])):
            result["reason"] = "决策日收盘未与已提供的精确涨停价一致，不创建买单。"
            return result
        execution = load_execution(code, day)
        result["source_hash"] = _digest({"decision": decision["source_hash"], "execution": execution["source_hash"]})
        if execution["status"] != "verified":
            result.update(missing=execution["missing"], reason="；".join(execution["missing"]))
            return result
        auction = execution["auction"]
        if Decimal(str(auction["open"])) != Decimal(str(opening_price)):
            result["reason"] = "真实竞价开盘价与用于模拟入场的开盘价不一致，不回填或追价。"
            return result
        if mode == "live":
            frozen, quote = _stamp(frozen_at), _stamp(quote_asof)
            cutoff = datetime.combine(date.fromisoformat(day), time(9, 25), CN)
            if not frozen <= cutoff or not quote.date().isoformat() == day or not time(9, 30) <= quote.time().replace(tzinfo=None) <= time(9, 30, 30) or _stamp(auction["asof"]) > quote:
                result["reason"] = "实时竞价只接受不晚于9:25冻结与9:30首30秒观察，不补做早前竞价交易。"
                return result
        low, high = seat_livermore._entry_band(Decimal(str(previous_close)))
        plan = domain.Plan(action="simulate", rationale="已提供明确竞价来源，独立复核原入场门槛。", evidence_ids=[], entry_low=float(low), entry_high=float(high), position_pct=5, stop_loss=None, take_profit=None)
        case = {"decision_date": decision_day, "bars": [{"date": decision_day, "close": previous_close}]}
        outcome = seat_livermore.decide_entry(case, plan, {"open": auction["open"], "auction_amount": auction["auction_amount"], "index_change_pct": execution["index_auction"]["change_pct"], "regional_ok": execution["region"]["regional_ok"]})
        result.update(allowed=outcome.budget is not None, budget_yuan=float(outcome.budget) if outcome.budget is not None else None,
                      reason=outcome.reason.replace("合成竞价成交额", "已提供竞价成交额"))
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        result.update(reason="竞价入场日期、价格或冻结时点无效，未创建买单。", missing=["入场数据校验未通过。"])
    return result


def save_profile(code: str, value: dict) -> dict:
    """Explicit local import only; readers do not call this writing function."""
    code = _code(code)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("code") != code or not isinstance(value.get("records"), dict):
        raise ValueError("Invalid explicit import")
    records = {}
    for group in GROUPS:
        rows = value["records"].get(group, [])
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
            raise ValueError("Oversized import group")
        clean = [_validate_record(group, row, code) for row in rows]
        if len({row["date"] for row in clean}) != len(clean):
            raise ValueError("Duplicate import dates/versions")
        records[group] = sorted(clean, key=lambda row: row["date"])
    safe = {"schema": SCHEMA, "code": code, "records": records}
    raw = _canonical(safe)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Oversized profile")
    root = directory()
    files = _index() if (root / "index.json").is_file() else {}
    if code not in files and len(files) >= MAX_PROFILES:
        raise ValueError("Full evidence index")
    files = {**files, code: hashlib.sha256(raw).hexdigest()}
    encoded = _canonical({"schema": SCHEMA, "files": files})
    if len(encoded) > MAX_INDEX_BYTES:
        raise ValueError("Oversized evidence index")
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{code}.json.gz"
    # Restore the prior valid profile if an index commit fails; no old accepted
    # file is replaced by a malformed import.
    old = target.read_bytes() if target.is_file() else None
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(gzip.compress(raw, mtime=0))
    temporary.replace(target)
    try:
        index_temp = root / "index.json.tmp"
        index_temp.write_bytes(encoded)
        index_temp.replace(root / "index.json")
    except OSError:
        if old is not None:
            temporary.write_bytes(old)
            temporary.replace(target)
        raise
    return safe


def integration_summary() -> dict:
    """Read an offline coverage receipt; never scan prices or create orders."""
    result = {"version": "livermore-data-integration/1", "scope": "whole_universe", "decision_date": market_history.CUTOFF,
              "execution_date": market_history.REPLAY_DAY, "status": "unavailable", "universe_stocks": 0,
              "history_120_stocks": 0, "low_position_stocks": 0, "source_limit_up_event_stocks": 0,
              "verified_limit_stocks": 0, "verified_security_status_stocks": 0, "verified_stock_auction_stocks": 0,
              "verified_index_auction_stocks": 0, "regional_confirmation_stocks": 0,
              "candidate_stocks": 0, "execution_ready_stocks": 0, "samples": [],
              "remaining": [LIMIT_MISSING, *EXECUTION_MISSING.values()],
              "limitations": [LIVE_LIMITATION, "公开来源字段与本地指纹校验不等于真实排队成交证明。", "没有竞价数据时不使用全天成交量、日开盘价或盘中涨幅推算。"]}
    try:
        receipt = _json(directory() / "coverage.json", MAX_INDEX_BYTES)
        if receipt.get("schema") != "livermore-coverage/v1" or receipt.get("decision_date") != market_history.CUTOFF or receipt.get("execution_date") != market_history.REPLAY_DAY:
            raise ValueError("Wrong coverage dates")
        keys = ("universe_stocks", "history_120_stocks", "low_position_stocks", "source_limit_up_event_stocks", "verified_limit_stocks", "verified_security_status_stocks", "verified_stock_auction_stocks", "verified_index_auction_stocks", "regional_confirmation_stocks", "candidate_stocks", "execution_ready_stocks")
        if any(type(receipt.get(key)) is not int or not 0 <= receipt[key] <= MAX_PROFILES for key in keys):
            raise ValueError("Invalid coverage counts")
        if any(receipt[key] > receipt["universe_stocks"] for key in keys[1:]):
            raise ValueError("Impossible coverage")
        clean = {key: receipt[key] for key in keys}
        state = receipt.get("status")
        if state not in {"partial_connected", "reference_only", "unavailable"}:
            state = "partial_connected" if receipt["universe_stocks"] else "unavailable"
        clean["status"] = state
        for key in ("remaining", "limitations"):
            values = receipt.get(key, result[key])
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(value, str) or len(value) > 1000 for value in values):
                raise ValueError("Invalid coverage notes")
            clean[key] = list(values)
        samples = receipt.get("samples", [])
        if not isinstance(samples, list) or len(samples) > 20:
            raise ValueError("Oversized coverage details")
        clean_samples = []
        for row in samples:
            if not isinstance(row, dict):
                raise ValueError("Invalid coverage sample")
            code = _code(row.get("code"))
            position = row.get("position_120_pct")
            if position is not None and (not _numeric(position, maximum=100) or position < 0):
                raise ValueError("Invalid coverage position")
            confirmed = row.get("limit_up_confirmed")
            if confirmed is not None and type(confirmed) is not bool:
                raise ValueError("Invalid coverage confirmation")
            if any(type(row.get(key)) is not bool for key in ("limit_price_verified", "auction_verified")):
                raise ValueError("Invalid coverage state")
            fields = row.get("missing_fields")
            if not isinstance(fields, list) or len(fields) > 30 or any(not isinstance(field, str) or len(field) > 500 for field in fields):
                raise ValueError("Invalid coverage missing fields")
            sample = {"code": code, "position_120_pct": position, "limit_up_confirmed": confirmed,
                      "limit_price_verified": row["limit_price_verified"], "auction_verified": row["auction_verified"], "missing_fields": list(fields)}
            if "name" in row:
                if not isinstance(row["name"], str) or len(row["name"]) > 100:
                    raise ValueError("Invalid sample security name")
                sample["name"] = row["name"]
            clean_samples.append(sample)
        if len({row["code"] for row in clean_samples}) != len(clean_samples):
            raise ValueError("Duplicate coverage sample")
        clean["samples"] = clean_samples
        result.update(clean)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        pass
    result["limitations"] = [LIVE_LIMITATION, *[note for note in result["limitations"] if note != LIVE_LIMITATION]][:30]
    return result
