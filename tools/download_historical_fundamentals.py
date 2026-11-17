"""Download a small explicit set of public annual references, never credentials.

Default is six stocks already present in the dated price archive. The source is
not a licensed PIT feed. Missing fields remain missing and do not create orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from server import historical_fundamentals as finance

DEFAULT_CODES = ("600519", "000858", "000333", "600887", "600900", "600406")
MAX_RESPONSE = 500_000


class SourceBlocked(Exception):
    pass


def fetch(client: httpx.Client, code: str, table: str) -> tuple[list[dict], dict]:
    params = finance.request_params(table, code)
    with client.stream("GET", finance.SOURCE_URL, params=params) as response:
        if response.status_code in {401, 403, 429}:
            raise SourceBlocked(f"HTTP {response.status_code}: source refused or rate limited; stopped without bypass")
        if response.is_redirect:
            raise SourceBlocked("Source redirected; stopped without following an unverified service")
        response.raise_for_status()
        body = bytearray()
        for block in response.iter_bytes(chunk_size=16_384):
            body.extend(block)
            if len(body) > MAX_RESPONSE:
                raise ValueError("Oversized financial response")
        payload = json.loads(body)
    return finance.parse_table(payload, table, code), {"url": finance.SOURCE_URL, "params": params,
                                                       "response_sha256": hashlib.sha256(body).hexdigest()}


def save(code: str, tables: dict, requests: list[dict], *, retrieved_at: str) -> dict:
    profile = finance.normalize(code, tables, retrieved_at=retrieved_at, requests=requests)
    if not profile["reports"]:
        raise ValueError("No pre-cutoff annual rows; existing cache was not overwritten")
    value = {"schema": finance.SCHEMA, "code": code, "decision_date": finance.CUTOFF,
             "retrieved_at": retrieved_at, "tables": tables, "requests": requests}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > finance.MAX_FILE_BYTES:
        raise ValueError("Oversized financial archive")
    target = finance.directory()
    target.mkdir(parents=True, exist_ok=True)
    index_path = target / "index.json"
    index = json.loads(index_path.read_text("utf-8")) if index_path.is_file() else {"schema": finance.SCHEMA, "files": {}}
    if index.get("schema") != finance.SCHEMA or not isinstance(index.get("files"), dict) or len(index["files"]) >= finance.MAX_PROFILES and code not in index["files"]:
        raise ValueError("Invalid or full financial index")
    temporary = target / f"{code}.json.gz.tmp"
    temporary.write_bytes(gzip.compress(raw, mtime=0))
    temporary.replace(target / f"{code}.json.gz")
    index["files"][code] = hashlib.sha256(raw).hexdigest()
    index["updated_at"] = retrieved_at
    index_tmp = target / "index.json.tmp"
    index_tmp.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    index_tmp.replace(index_path)
    return profile


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", default=list(DEFAULT_CODES), help="At most ten explicit downloaded mainboard codes")
    args = parser.parse_args(argv)
    codes = list(dict.fromkeys(args.codes))
    if not 1 <= len(codes) <= 10:
        parser.error("Supply one to ten explicit codes, not the whole market")
    # Read only the public stock archive; no account/research/model file access.
    market_root = finance.directory().parent
    index_path = market_root / "index.sqlite3"
    if not index_path.is_file():
        parser.error("Download the dated price archive first")
    with sqlite3.connect(index_path.as_uri() + "?mode=ro", uri=True) as conn:
        for code in codes:
            finance.request_params("cashflow", code)
            row = conn.execute("SELECT name FROM stocks WHERE code=? AND status='ok'", (code,)).fetchone()
            if row is None or "ST" in row[0].upper() or "退" in row[0]:
                parser.error("A requested code is outside the eligible downloaded archive")
    successes = failures = 0
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                      headers={"User-Agent": "SmartTradingAssistant-PersonalHistoricalResearch/1.0"}) as client:
        for code in codes:
            try:
                tables, requests = {}, []
                for table in finance.TABLES:
                    rows, provenance = fetch(client, code, table)
                    tables[table] = rows
                    requests.append(provenance)
                    time.sleep(.35)  # Sequential, modest public-source load.
                profile = save(code, tables, requests, retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                successes += 1
                print(f"{code}: saved {len(profile['reports'])} annual reference rows; strict gaps remain explicit", flush=True)
            except SourceBlocked as exc:
                print(f"Stopped source: {exc}", flush=True)
                return 2
            except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError):
                failures += 1
                print(f"{code}: financial source/validation unavailable; old cache kept, no synthetic fill", flush=True)
    print(f"Financial reference download: {successes} saved, {failures} unavailable; no model or order requests")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
