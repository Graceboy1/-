"""Collect public limit-up *references* and audit the existing whole-market archive.

No historical auction, exchange limit price, security status, verification flag,
or trade is manufactured here. Verified local evidence has a separate importer.
HTTP access denials, rate limits and redirects terminate this source immediately.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import market_history  # noqa: E402

VERSION = "livermore-data-integration/1"
REFERENCE_SCHEMA = "livermore-limit-up-reference/v1"
INDEX_SCHEMA = "livermore-reference-index/v1"
PUBLIC_JS = "https://quote.eastmoney.com/ztb/newstatic/build/detail.js"
PUBLIC_POOL = "https://push2ex.eastmoney.com/getTopicZTPool"
# Anonymous static-client identifier, published in PUBLIC_JS, not an account key.
PUBLIC_UT = "7eea3edcaed734bea9cbfc24409ed989"
MAX_SOURCE_BYTES = 2_000_000
MAX_SOURCE_ROWS = 1000
SCOPE = re.compile(r"(?:000|001|002|003|600|601|603|605)\d{3}\Z")


class SourceError(ValueError):
    """A failed source is not evidence of an empty limit-up pool."""


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def universe() -> list[dict]:
    """The same read-only, current main-board/non-ST scope as the application."""
    with closing(market_history.connection()) as conn:
        return [dict(row) for row in conn.execute(
            f"SELECT code,name FROM stocks WHERE {market_history.ELIGIBLE_SQL} ORDER BY code"
        ).fetchall()]


def valid_scope(code, name) -> bool:
    return isinstance(code, str) and SCOPE.fullmatch(code) is not None and isinstance(name, str) and bool(name.strip()) and "ST" not in name.upper() and "退" not in name


def validate_dates(decision_date: str, execution_date: str) -> None:
    # This archive has a fixed cutoff. A later/current pool must never silently
    # replace the decision day's historical evidence.
    if date.fromisoformat(decision_date).isoformat() != decision_date or date.fromisoformat(execution_date).isoformat() != execution_date:
        raise ValueError("Dates must use YYYY-MM-DD")
    if decision_date != market_history.CUTOFF or execution_date != market_history.REPLAY_DAY or decision_date >= execution_date:
        raise ValueError("Requested dates do not match the installed historical archive")


def verify_public_frontend(javascript: str) -> None:
    if f'var a="{PUBLIC_UT}",n="wz.ztzt"' not in javascript or 'url:"getTopicZTPool"' not in javascript:
        raise SourceError("Public frontend parameters changed; no pool request made")
    if not re.search(r"Number\(\w+\)/1e3\)\.toFixed\(2\)", javascript):
        raise SourceError("Published price formatter could not be confirmed")


def response_body(client: httpx.Client, url: str, params: dict | None = None) -> tuple[bytes, dict]:
    response = client.get(url, params=params or {})
    if response.status_code in (401, 403, 429) or response.is_redirect:
        raise SourceError(f"Source access stopped: HTTP {response.status_code}; no retries or alternate domains")
    if response.status_code != 200:
        raise SourceError(f"Source HTTP {response.status_code}; no successful empty pool inferred")
    raw = response.content
    if not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise SourceError("Source response exceeds the bounded size or is empty")
    return raw, {"source_url": url, "source_query": params or {}, "response_url": str(response.url),
                 "http_status": response.status_code, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                 "source_response_hash": digest(raw), "bytes": len(raw)}


def pool_query(day: str) -> dict:
    return {"ut": PUBLIC_UT, "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": str(MAX_SOURCE_ROWS), "sort": "fbt:asc", "date": day.replace("-", "")}


def parse_pool(raw: bytes, requested_date: str) -> tuple[list[dict], object]:
    try:
        value = json.loads(raw)
        data = value.get("data")
        if type(value.get("rc")) is not int or value["rc"] != 0 or not isinstance(data, dict) or not isinstance(data.get("pool"), list):
            raise SourceError("Public pool returned an error or unavailable data, not a confirmed empty pool")
        rows, total = data["pool"], data.get("tc")
        if type(total) is not int or total != len(rows) or not 0 <= total <= MAX_SOURCE_ROWS:
            raise SourceError("Incomplete, oversized or inconsistent pool; no pagination guess")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("c"), str) or not re.fullmatch(r"\d{6}", row["c"]) or row["c"] in seen:
                raise SourceError("Invalid or duplicate pool security")
            seen.add(row["c"])
            if not isinstance(row.get("n"), str) or not row["n"].strip() or type(row.get("p")) not in (int, float) or not math.isfinite(row["p"]) or row["p"] <= 0:
                raise SourceError("Invalid source price/name")
            for key in ("date", "trade_date", "requested_date"):
                if key in row and str(row[key]).replace("-", "") != requested_date.replace("-", ""):
                    raise SourceError("Pool record explicitly belongs to a different date")
        for key in ("date", "trade_date", "requested_date"):
            if key in data and str(data[key]).replace("-", "") != requested_date.replace("-", ""):
                raise SourceError("Pool explicitly belongs to a different date")
        return rows, data.get("qdate")
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise SourceError("Malformed public pool response") from exc


def crosscheck_reference(raw: bytes, metadata: dict, day: str, stocks: list[dict]) -> dict:
    rows, qdate = parse_pool(raw, day)
    names = {row["code"]: row["name"] for row in stocks if valid_scope(row["code"], row["name"])}
    matches, mismatches, excluded, unavailable = [], [], [], []
    for row in rows:
        code = row["c"]
        if code not in names or not valid_scope(code, row["n"]):
            excluded.append(code)
            continue
        try:
            case = market_history.load_case("history-20261113-" + code)
            bar = next((bar for bar in case["bars"] if bar["date"] == day), None)
            if bar is None:
                unavailable.append(code)
                continue
        except Exception:
            # A corrupt archive can never acquire credibility from the source.
            unavailable.append(code)
            continue
        reported, close = Decimal(str(row["p"])) / 1000, Decimal(str(bar["close"]))
        if reported != close:
            mismatches.append({"code": code, "reported_price": float(reported), "archive_close": float(close)})
            continue
        matches.append({"code": code, "name": names[code], "date": day,
                        "reported_price": float(reported), "archive_close": float(close),
                        "price_matches_archive": True, "source_reported_limit_up": True,
                        "verification_status": "source_reference_only", "source_url": PUBLIC_POOL,
                        "source_hash": metadata["source_response_hash"]})
    return {"schema": REFERENCE_SCHEMA, "requested_date": day, "server_qdate_raw": qdate,
            "source_url": PUBLIC_POOL, "source_query": metadata["source_query"],
            "source_response_hash": metadata["source_response_hash"], "retrieved_at": metadata["retrieved_at"],
            "verification_status": "source_reference_only", "trusted": False,
            "price_unit": "yuan", "price_field_mapping": "public frontend Number(p)/1e3, not exchange limit price",
            "total_source_rows": len(rows), "excluded_codes": excluded, "unavailable_archive_codes": unavailable,
            "price_mismatches": mismatches, "rows": sorted(matches, key=lambda row: row["code"])}


def persist_references(profiles: list[dict], raw_records: list[tuple[bytes, dict]], output: Path) -> dict:
    """Commit the public-reference index last; a failed source keeps the old index."""
    files = {}
    for raw, metadata in raw_records:
        label = metadata["source_query"].get("date", "frontend")
        stem = f"eastmoney-{label}-{metadata['source_response_hash']}"
        atomic_bytes(output / "raw" / f"{stem}.body", raw)
        atomic_bytes(output / "raw" / f"{stem}.metadata.json", canonical(metadata))
    for profile in profiles:
        day = profile["requested_date"]
        raw = canonical(profile)
        name = f"reference-{day}-{digest(raw)[:12]}.json.gz"
        atomic_bytes(output / name, gzip.compress(raw, mtime=0))
        files[day] = {"path": name, "sha256": digest(raw)}
    value = {"schema": INDEX_SCHEMA, "files": files, "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_bytes(output / "references-index.json", canonical(value))
    return value


def collect_references(stocks: list[dict], decision_date: str, execution_date: str, client: httpx.Client, output: Path) -> dict:
    validate_dates(decision_date, execution_date)
    js, js_metadata = response_body(client, PUBLIC_JS)
    verify_public_frontend(js.decode("utf-8"))
    records, profiles = [(js, js_metadata)], []
    for day in (decision_date, execution_date):
        raw, metadata = response_body(client, PUBLIC_POOL, pool_query(day))
        metadata["requested_date"] = day
        profile = crosscheck_reference(raw, metadata, day, stocks)
        records.append((raw, metadata))
        profiles.append(profile)
    return persist_references(profiles, records, output)


def read_reference(day: str, output: Path) -> dict | None:
    try:
        index_path = output / "references-index.json"
        if index_path.stat().st_size > 100_000:
            return None
        index = json.loads(index_path.read_bytes())
        if index.get("schema") != INDEX_SCHEMA:
            return None
        item = index["files"][day]
        if not re.fullmatch(r"reference-" + re.escape(day) + r"-[a-f0-9]{12}\.json\.gz", item.get("path", "")):
            return None
        with gzip.open(output / item["path"], "rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES or digest(raw) != item["sha256"]:
            return None
        profile = json.loads(raw)
        if profile.get("schema") != REFERENCE_SCHEMA or profile.get("requested_date") != day or profile.get("trusted") is not False or profile.get("verification_status") != "source_reference_only":
            return None
        rows = profile.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_SOURCE_ROWS:
            return None
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not valid_scope(row.get("code"), row.get("name")) or row["code"] in seen or row.get("date") != day or row.get("price_matches_archive") is not True or row.get("source_reported_limit_up") is not True or row.get("verification_status") != "source_reference_only":
                return None
            price = row.get("reported_price")
            if type(price) not in (int, float) or not math.isfinite(price) or price <= 0 or price != row.get("archive_close") or row.get("source_hash") != profile.get("source_response_hash"):
                return None
            seen.add(row["code"])
        return profile
    except (OSError, ValueError, KeyError, TypeError):
        return None


def scan_coverage(stocks: list[dict], decision_date: str, execution_date: str, output: Path) -> dict:
    from server import livermore_data
    validate_dates(decision_date, execution_date)
    reference = read_reference(decision_date, output)
    ref_codes = {row["code"] for row in (reference or {}).get("rows", [])}
    totals = {key: 0 for key in ("history_120_stocks", "low_position_stocks", "verified_limit_stocks",
              "verified_security_status_stocks", "verified_stock_auction_stocks", "verified_index_auction_stocks",
              "regional_confirmation_stocks", "candidate_stocks", "execution_ready_stocks")}
    samples, archive_failed = [], 0
    for stock in sorted(stocks, key=lambda stock: stock["code"]):
        code, name = stock["code"], stock["name"]
        if not valid_scope(code, name):
            continue
        try:
            case = market_history.load_case("history-20261113-" + code)
        except Exception:
            archive_failed += 1
            continue
        bars = [bar for bar in case["bars"] if bar["date"] <= decision_date][-120:]
        enough = len(bars) == 120 and bars[-1]["date"] == decision_date
        position, low_position = None, False
        missing = []
        if enough:
            totals["history_120_stocks"] += 1
            high = max(Decimal(str(bar["high"])) for bar in bars)
            low = min(Decimal(str(bar["low"])) for bar in bars)
            if high > low:
                position = (Decimal(str(bars[-1]["close"])) - low) / (high - low)
                low_position = position < Decimal("0.10")
                totals["low_position_stocks"] += int(low_position)
            else:
                missing.append("120日价格区间无定义。")
        else:
            missing.append("缺少120根有效日线或决策日收盘。")
        decision = livermore_data.load_decision(code, decision_date)
        execution = livermore_data.load_execution(code, execution_date)
        exact = decision.get("status") == "verified" and isinstance(decision.get("limits"), dict)
        limits = decision.get("limits") or {}
        totals["verified_limit_stocks"] += int(exact)
        totals["verified_security_status_stocks"] += int(exact)
        auction = execution.get("auction") is not None and execution.get("status") != "invalid"
        index = execution.get("index_auction") is not None and execution.get("status") != "invalid"
        region = execution.get("region") is not None and execution.get("status") != "invalid"
        totals["verified_stock_auction_stocks"] += int(auction)
        totals["verified_index_auction_stocks"] += int(index)
        totals["regional_confirmation_stocks"] += int(region)
        confirmed = None
        if exact and bars:
            confirmed = Decimal(str(bars[-1]["close"])) == Decimal(str(limits["limit_up"]))
        state_ok = exact and not limits.get("is_st") and not limits.get("suspended") and valid_scope(code, limits.get("security_name"))
        candidate = enough and low_position and confirmed is True and state_ok
        ready = candidate and execution.get("status") == "verified"
        totals["candidate_stocks"] += int(candidate)
        totals["execution_ready_stocks"] += int(ready)
        missing.extend(decision.get("missing") or [])
        missing.extend(execution.get("missing") or [])
        sample = {"code": code, "name": name, "position_120_pct": round(float(position * 100), 6) if position is not None else None,
                  "limit_up_confirmed": confirmed, "limit_price_verified": exact, "auction_verified": auction,
                  "source_limit_up_reference": code in ref_codes, "missing_fields": missing}
        # Only bounded samples stay in memory, prioritizing useful low-position
        # and source-reference rows without retaining all daily histories.
        priority = (int(candidate), int(code in ref_codes), int(low_position))
        samples.append((priority, sample))
        samples.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[1]["code"]))
        del samples[20:]
    known = any(totals[key] for key in ("verified_limit_stocks", "verified_stock_auction_stocks", "verified_index_auction_stocks", "regional_confirmation_stocks"))
    execution_reference = read_reference(execution_date, output)
    before = {row["code"]: row["reported_price"] for row in (reference or {}).get("rows", [])}
    changes = [{"code": row["code"], "decision_date": decision_date, "decision_reported_price": before[row["code"]],
                "execution_date": execution_date, "execution_reported_price": row["reported_price"]}
               for row in (execution_reference or {}).get("rows", []) if row["code"] in before and before[row["code"]] != row["reported_price"]][:20]
    universe_count = sum(valid_scope(row["code"], row["name"]) for row in stocks)
    remaining = []
    if totals["verified_limit_stocks"] < universe_count:
        remaining.append("历史精确涨跌停与当时证券状态尚未覆盖全范围，需独立合规来源。")
    if any(totals[key] < universe_count for key in ("verified_stock_auction_stocks", "verified_index_auction_stocks", "regional_confirmation_stocks")):
        remaining.append("真实9:25股票竞价成交额、指数竞价与区域确认尚未覆盖全范围，需授权档案或明确人工证据。")
    if archive_failed:
        remaining.append(f"{archive_failed}只股票日线未通过已有档案加载器校验。")
    return {"schema": "livermore-coverage/v1", "version": VERSION, "scope": "whole_universe",
            "decision_date": decision_date, "execution_date": execution_date,
            "status": "partial_connected" if known else "reference_only" if ref_codes else "unavailable",
            "universe_stocks": universe_count,
            **totals, "source_limit_up_event_stocks": len(ref_codes), "archive_failed_stocks": archive_failed,
            "remaining": remaining,
            "limitations": ["公共涨停池仅作来源参考，绝不等同交易所精确限价。", "当前名单非历史时点成分重建。", "候选与执行数据就绪不等于已成交，实际入口仍须复核竞价、预算、流动性与T+1规则。", "server_qdate_raw不是池日期证明；按请求日期保存并逐股校验同日日K。"],
            "reference_date_price_changes": changes, "samples": [sample for _, sample in samples],
            "updated_at": datetime.now(timezone.utc).isoformat()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-date", default=market_history.CUTOFF)
    parser.add_argument("--execution-date", default=market_history.REPLAY_DAY)
    parser.add_argument("--dry-run", action="store_true", help="Read the public stock directory only; no network or writes")
    parser.add_argument("--refresh-coverage", action="store_true", help="Rebuild coverage from local evidence without network")
    args = parser.parse_args(argv)
    validate_dates(args.decision_date, args.execution_date)
    stocks = universe()
    if args.dry_run:
        print(json.dumps({"dry_run": True, "scope": "whole_universe", "universe_stocks": len(stocks), "decision_date": args.decision_date, "execution_date": args.execution_date}, ensure_ascii=False))
        return 0
    output = market_history.directory() / "livermore"
    if not args.refresh_coverage:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, headers={"User-Agent": "SmartTradingAssistant-PersonalHistoricalResearch/1.0"}) as client:
            collect_references(stocks, args.decision_date, args.execution_date, client, output)
    coverage = scan_coverage(stocks, args.decision_date, args.execution_date, output)
    atomic_bytes(output / "coverage.json", canonical(coverage))
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SourceError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"利弗莫尔数据未完成更新：{exc}", file=sys.stderr)
        raise SystemExit(1)
