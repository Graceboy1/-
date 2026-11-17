"""Fetch bounded public annual references for the downloaded mainboard universe.

This is deliberately not a trading/PIT importer: original-report verification,
actual share counts, maintenance capex and human business evidence stay missing.
The command reads the public price directory only, never account or API secrets.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from server import historical_fundamentals as finance
from tools.download_historical_fundamentals import SourceBlocked

YEARS = (2023, 2024, 2025)
PAGE_SIZE = 500
MAX_PAGES = 20
MAX_RESPONSE = 2_000_000
MAX_GROUP_BYTES = 20_000_000
GROUP_SCHEMA = "financial-bulk-group/v1"
DELAY = .4
CORE_FIELDS = ("operating_cashflow", "reported_capex", "net_profit", "roe_pct",
               "total_liabilities", "total_equity", "debt_to_equity")


def eligible_universe() -> dict[str, str]:
    """The sole SQL input is the public, dated stock directory, opened read-only."""
    path = finance.directory().parent / "index.sqlite3"
    if not path.is_file():
        raise ValueError("Download the dated public stock directory first")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT code,name FROM stocks WHERE status='ok' ORDER BY code").fetchall()
    result = {code: name for code, name in rows
              if isinstance(code, str) and re.fullmatch(finance.CODE_PATTERN, code)
              and isinstance(name, str) and name.strip() and "ST" not in name.upper() and "退" not in name}
    if not result or len(result) > finance.MAX_PROFILES:
        raise ValueError("Empty or oversized eligible public mainboard universe")
    return result


def request_params(table: str, year: int, page: int) -> dict:
    if table not in finance.TABLES or type(year) is not int or year not in YEARS or type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError("Invalid bounded annual query")
    spec = finance.TABLES[table]
    annual_filter = f'({spec["date_field"]}=\'{year}-12-31\')'
    if table == "indicators":
        # CPD also covers Hong Kong securities. This vendor's explicit A-share
        # type filter avoids treating foreign identifiers as malformed A codes.
        # It is a market-universe filter, never a choice of report version.
        annual_filter += '(SECURITY_TYPE_CODE="058001001")'
    return {"reportName": spec["reportName"], "columns": spec["columns"],
            "filter": annual_filter,
            "pageSize": str(PAGE_SIZE), "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE", "sortTypes": "1", "source": "WEB", "client": "WEB"}


def fetch_page(client: httpx.Client, table: str, year: int, page: int) -> tuple[list[dict], int, int, dict]:
    params = request_params(table, year, page)
    with client.stream("GET", finance.SOURCE_URL, params=params) as response:
        if response.status_code in {401, 403, 429} or response.is_redirect:
            raise SourceBlocked(f"HTTP {response.status_code}: source refused/redirected; stopped without bypass")
        response.raise_for_status()
        body = bytearray()
        for block in response.iter_bytes(chunk_size=16_384):
            body.extend(block)
            if len(body) > MAX_RESPONSE:
                raise ValueError("Oversized public financial response")
    value = json.loads(body)
    result = value.get("result") if isinstance(value, dict) and value.get("success") is True else None
    if not isinstance(result, dict):
        raise ValueError("Financial source did not return a successful result")
    rows, pages, count = result.get("data"), result.get("pages"), result.get("count")
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(pages) is not int or type(count) is not int:
        raise ValueError("Invalid financial pagination metadata")
    if not 0 <= count <= PAGE_SIZE * MAX_PAGES or not 0 <= pages <= MAX_PAGES or pages != math.ceil(count / PAGE_SIZE):
        raise ValueError("Unbounded or inconsistent financial pagination")
    if page > max(1, pages) or len(rows) != min(PAGE_SIZE, max(0, count - (page - 1) * PAGE_SIZE)):
        raise ValueError("Incomplete financial result page")
    spec = finance.TABLES[table]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("SECURITY_CODE"), str) or not re.fullmatch(r"\d{6}", row["SECURITY_CODE"]):
            raise ValueError("Invalid source security identity")
        if finance._day(row.get(spec["date_field"])) != f"{year}-12-31":
            raise ValueError("Source returned another reporting period")
    return rows, pages, count, {"url": finance.SOURCE_URL, "params": params,
                               "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "response_sha256": hashlib.sha256(body).hexdigest()}


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _group_path(directory: Path, table: str, year: int) -> Path:
    query = {"source_url": finance.SOURCE_URL, "cutoff": finance.CUTOFF,
             "schema": GROUP_SCHEMA, "params": request_params(table, year, 1)}
    signature = hashlib.sha256(_canonical(query)).hexdigest()[:16]
    return directory / f"{table}-{year}-{signature}.json.gz"


def _validate_group(value: dict, table: str, year: int) -> dict:
    if not isinstance(value, dict) or value.get("schema") != GROUP_SCHEMA or value.get("source_url") != finance.SOURCE_URL or value.get("cutoff") != finance.CUTOFF or value.get("table") != table or value.get("year") != year or value.get("query") != request_params(table, year, 1):
        raise ValueError("Mismatched public annual staging group")
    rows, count, pages, requests = value.get("rows"), value.get("count"), value.get("pages"), value.get("requests")
    if not isinstance(rows, list) or type(count) is not int or type(pages) is not int or not 0 <= count <= PAGE_SIZE * MAX_PAGES or pages != math.ceil(count / PAGE_SIZE) or len(rows) != count:
        raise ValueError("Incomplete public annual staging group")
    if not isinstance(requests, list) or len(requests) != max(1, pages):
        raise ValueError("Incomplete annual staging provenance")
    for index, source in enumerate(requests, 1):
        if not isinstance(source, dict) or source.get("url") != finance.SOURCE_URL or source.get("params") != request_params(table, year, index) or not isinstance(source.get("response_sha256"), str) or re.fullmatch(r"[a-f0-9]{64}", source["response_sha256"]) is None:
            raise ValueError("Mismatched annual staging source request")
    spec = finance.TABLES[table]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("SECURITY_CODE"), str) or not re.fullmatch(r"\d{6}", row["SECURITY_CODE"]) or finance._day(row.get(spec["date_field"])) != f"{year}-12-31":
            raise ValueError("Invalid staged financial row")
    return value


def _load_group(directory: Path | None, table: str, year: int) -> dict | None:
    if directory is None:
        return None
    path = _group_path(directory, table, year)
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_GROUP_BYTES:
        raise ValueError("Oversized public staging file")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_GROUP_BYTES + 1)
    if len(raw) > MAX_GROUP_BYTES:
        raise ValueError("Oversized expanded public staging file")
    envelope = json.loads(raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict) or envelope.get("sha256") != hashlib.sha256(_canonical(envelope["data"])).hexdigest():
        raise ValueError("Public staging checksum mismatch")
    return _validate_group(envelope["data"], table, year)


def _save_group(directory: Path | None, table: str, year: int, value: dict):
    if directory is None:
        return
    _validate_group(value, table, year)
    raw = _canonical({"data": value, "sha256": hashlib.sha256(_canonical(value)).hexdigest()})
    if len(raw) > MAX_GROUP_BYTES:
        raise ValueError("Oversized annual public staging group")
    directory.mkdir(parents=True, exist_ok=True)
    path = _group_path(directory, table, year)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(gzip.compress(raw, mtime=0))
    temporary.replace(path)


def _collect_group(client: httpx.Client, table: str, year: int, *, pause, progress) -> dict:
    expected = None
    page, all_rows, requests = 1, [], []
    while True:
        rows, pages, count, source = fetch_page(client, table, year, page)
        if expected is None:
            expected = (pages, count)
        elif expected != (pages, count):
            raise ValueError("Financial pagination changed during collection; old cache preserved")
        all_rows.extend(rows)
        requests.append(source)
        progress(f"{table} {year}: page {page}/{max(1,pages)}, source rows {count}", flush=True)
        pause(DELAY)
        if page >= pages:
            break
        page += 1
    return _validate_group({"schema": GROUP_SCHEMA, "source_url": finance.SOURCE_URL, "cutoff": finance.CUTOFF,
                            "table": table, "year": year, "query": request_params(table, year, 1),
                            "rows": all_rows, "pages": pages, "count": count, "requests": requests}, table, year)


def collect(client: httpx.Client, universe: dict[str, str], *, pause=time.sleep, progress=print,
            cache_directory: Path | None = None) -> tuple[dict, dict]:
    """Collect all groups before modifying any indexed reference profile."""
    tables = {code: {name: [] for name in finance.TABLES} for code in universe}
    provenance = {code: [] for code in universe}
    for table in finance.TABLES:
        for year in YEARS:
            group = _load_group(cache_directory, table, year)
            if group is None:
                group = _collect_group(client, table, year, pause=pause, progress=progress)
                _save_group(cache_directory, table, year, group)
            else:
                progress(f"{table} {year}: reused complete checksummed public staging ({group['count']} rows)", flush=True)
            frequencies = Counter(row["SECURITY_CODE"] for row in group["rows"])
            ambiguous = {code for code, count in frequencies.items() if count > 1}
            for code in ambiguous & universe.keys():
                provenance[code].append({"url": finance.SOURCE_URL, "params": request_params(table, year, 1),
                                         "exclusion_reason": "ambiguous_security_period_versions", "table": table,
                                         "year": year, "excluded_source_rows": frequencies[code],
                                         "response_sha256": [item["response_sha256"] for item in group["requests"]]})
            if ambiguous:
                progress(f"{table} {year}: excluded {len(ambiguous)} ambiguous security/year groups; no version selected", flush=True)
            for index, row in enumerate(group["rows"]):
                code = row["SECURITY_CODE"]
                if code not in universe or code in ambiguous:
                    continue
                name = row.get("SECURITY_NAME_ABBR")
                if not isinstance(name, str) or "ST" in name.upper() or "退" in name:
                    continue
                selected = finance.parse_table({"success": True, "result": {"data": [row]}}, table, code)
                tables[code][table].extend(selected)
                if selected:
                    provenance[code].append(group["requests"][index // PAGE_SIZE])
    return tables, provenance


def _index(target: Path) -> dict:
    path = target / "index.json"
    if not path.is_file():
        return {"schema": finance.SCHEMA, "files": {}}
    if path.stat().st_size > finance.MAX_INDEX_BYTES:
        raise ValueError("Financial index is too large")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") != finance.SCHEMA or not isinstance(value.get("files"), dict) or len(value["files"]) > finance.MAX_PROFILES:
        raise ValueError("Invalid financial index; old files preserved")
    if any(not isinstance(code, str) or not re.fullmatch(finance.CODE_PATTERN, code) or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) for code, digest in value["files"].items()):
        raise ValueError("Invalid indexed financial identity/checksum")
    return value


def _load_existing(target: Path, code: str, digest: str) -> tuple[dict, dict]:
    path = target / f"{code}.json.gz"
    if path.stat().st_size > finance.MAX_FILE_BYTES:
        raise ValueError("Oversized existing financial profile")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(finance.MAX_FILE_BYTES + 1)
    if len(raw) > finance.MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Existing financial profile checksum mismatch; not replaced")
    value = json.loads(raw)
    if value.get("schema") != finance.SCHEMA or value.get("code") != code or value.get("decision_date") != finance.CUTOFF:
        raise ValueError("Mismatched existing financial profile; not replaced")
    profile = finance.normalize(code, value["tables"], retrieved_at=value["retrieved_at"], requests=value.get("requests"))
    return value, profile


def _quality(profile: dict) -> tuple[int, int, int]:
    rows = profile["reports"]
    return (len(rows), sum(len(row["report_types"]) for row in rows),
            sum(row.get(key) is not None for row in rows for key in CORE_FIELDS))


def _write_json(path: Path, value: dict, maximum: int):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > maximum:
        raise ValueError("Oversized financial metadata")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def save_batch(tables: dict, requests: dict, universe: dict[str, str], *, retrieved_at: str) -> dict:
    """Each profile and final index are atomically replaced; no annual synthesis.

    Already-complete valid caches are retained. Old partial rows are merged by
    table/year without dropping any earlier row. All candidate profiles are
    validated first. Existing indexed files get a small immediate index commit;
    the thousands of new profiles share one final index commit.
    """
    target = finance.directory()
    index = _index(target)
    pending, profiles = {}, {}
    preserved = 0
    for code in universe:
        incoming = tables[code]
        old_value = old_profile = None
        if code in index["files"]:
            old_value, old_profile = _load_existing(target, code, index["files"][code])
            if len(old_profile["reports"]) == 3 and all(len(row["report_types"]) == len(finance.TABLES) and all(row.get(key) is not None for key in CORE_FIELDS) for row in old_profile["reports"]):
                profiles[code] = old_profile
                preserved += 1
                continue
            merged = {}
            for table, spec in finance.TABLES.items():
                by_period = {finance._day(row[spec["date_field"]]): row for row in incoming[table]}
                # Do not blend different versions of a row or overwrite the
                # earlier cached annual values merely because a new fetch ran.
                by_period.update({finance._day(row[spec["date_field"]]): row for row in old_value["tables"][table]})
                merged[table] = [by_period[period] for period in sorted(by_period)]
            incoming = merged
        source_requests = [*(old_value.get("requests", []) if old_value else []), *requests[code]]
        profile = finance.normalize(code, incoming, retrieved_at=retrieved_at, requests=source_requests)
        if not profile["reports"]:
            profiles[code] = old_profile or profile
            continue
        if old_profile and _quality(profile) <= _quality(old_profile):
            profiles[code] = old_profile
            preserved += 1
            continue
        value = {"schema": finance.SCHEMA, "code": code, "decision_date": finance.CUTOFF,
                 "retrieved_at": retrieved_at, "tables": incoming, "requests": source_requests}
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(raw) > finance.MAX_FILE_BYTES:
            raise ValueError("Oversized individual financial profile")
        pending[code] = raw
        profiles[code] = profile
    if len(set(index["files"]) | set(pending)) > finance.MAX_PROFILES:
        raise ValueError("Financial index capacity exceeded")
    # Prove the final index size before modifying profile files.
    final_index = {**index, "files": {**index["files"], **{code: hashlib.sha256(raw).hexdigest() for code, raw in pending.items()}}, "updated_at": retrieved_at}
    if len(json.dumps(final_index, ensure_ascii=False, sort_keys=True).encode()) > finance.MAX_INDEX_BYTES:
        raise ValueError("Final financial index capacity exceeded")
    target.mkdir(parents=True, exist_ok=True)
    for code, raw in pending.items():
        path = target / f"{code}.json.gz"
        was_indexed = code in index["files"]
        backup = path.read_bytes() if was_indexed else None
        previous = index["files"].get(code)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(gzip.compress(raw, mtime=0))
        temporary.replace(path)
        index["files"][code] = hashlib.sha256(raw).hexdigest()
        index["updated_at"] = retrieved_at
        if was_indexed:
            try:
                _write_json(target / "index.json", index, finance.MAX_INDEX_BYTES)
            except (OSError, ValueError):
                temporary.write_bytes(backup)
                temporary.replace(path)
                index["files"][code] = previous
                raise
    _write_json(target / "index.json", index, finance.MAX_INDEX_BYTES)
    summary = coverage(profiles, universe, retrieved_at=retrieved_at)
    summary.update(saved_profiles=len(pending), preserved_existing_profiles=preserved)
    rejected = [{"code": code, "table": item["table"], "year": item["year"], "excluded_source_rows": item["excluded_source_rows"]}
                for code, records in requests.items() for item in records
                if item.get("exclusion_reason") == "ambiguous_security_period_versions"]
    summary.update(ambiguous_source_group_count=len(rejected), source_ambiguity_samples=rejected[:200],
                   source_ambiguity_samples_truncated=len(rejected)>200)
    _write_json(target / "coverage.json", summary, 2_000_000)
    return summary


def coverage(profiles: dict, universe: dict[str, str], *, retrieved_at: str) -> dict:
    complete, annual_rows, with_reports = 0, 0, 0
    missing = []
    for code, name in universe.items():
        rows = profiles[code]["reports"]
        annual_rows += len(rows)
        with_reports += bool(rows)
        good = len(rows) == 3 and all(len(row["report_types"]) == len(finance.TABLES) and all(row.get(key) is not None for key in CORE_FIELDS) for row in rows)
        complete += good
        if not good:
            absent = [str(year) for year in YEARS if not any(row["period_end"] == f"{year}-12-31" for row in rows)]
            missing.append({"code": code, "name": name, "annual_rows": len(rows), "missing_years": absent,
                            "incomplete_reports": [{"period_end": row["period_end"], "missing_tables": sorted(set(finance.TABLES)-set(row["report_types"])),
                                                    "missing_fields": [key for key in CORE_FIELDS if row.get(key) is None]} for row in rows if len(row["report_types"]) != len(finance.TABLES) or any(row.get(key) is None for key in CORE_FIELDS)]})
    return {"schema": "financial-coverage/v1", "decision_date": finance.CUTOFF, "retrieved_at": retrieved_at,
            "years": list(YEARS), "eligible_stocks": len(universe), "stocks_with_annual_references": with_reports,
            "annual_reference_rows": annual_rows, "complete_three_year_reference_stocks": complete,
            "strict_pit_verified_stocks": 0, "strategy_ready_stocks": 0, "incomplete_stocks": missing,
            "remaining_required_evidence": ["原始年报及截至决策日的版本核验", "可靠同期总股数", "有证据的维持性资本开支", "人工能力圈与护城河确认"],
            "limitations": list(finance.LIMITATIONS)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show local universe and request bounds without network or writes")
    args = parser.parse_args(argv)
    try:
        universe = eligible_universe()
        print(f"Eligible mainboard stocks: {len(universe)}; annual years: {YEARS}; table/year groups: {len(finance.TABLES)*len(YEARS)}; max pages per group: {MAX_PAGES}", flush=True)
        if args.dry_run:
            return 0
        # Validate existing public metadata before asking the source for data.
        _index(finance.directory())
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                          headers={"User-Agent": "SmartTradingAssistant-PersonalHistoricalResearch/1.0"}) as client:
            tables, requests = collect(client, universe, cache_directory=finance.directory() / ".bulk-staging")
        result = save_batch(tables, requests, universe, retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print(json.dumps({key: value for key, value in result.items() if key not in {"incomplete_stocks", "limitations"}}, ensure_ascii=False), flush=True)
        return 0
    except SourceBlocked as exc:
        print(f"Stopped source: {exc}; indexed cache preserved", flush=True)
        return 2
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("Financial source/archive validation unavailable; no synthetic fields, orders, or model requests", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
