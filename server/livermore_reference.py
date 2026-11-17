"""Read dated, price-cross-checked limit-up pool references, not exact limits.

The publisher's qdate is retained as raw metadata, never substituted for the
requested event day. Absence from a pool is not a verified 'not limit-up' fact.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import gzip
import hashlib
import json
import math
import re
import zlib

from . import market_history

INDEX_SCHEMA = "livermore-reference-index/v1"
SCHEMA = "livermore-limit-up-reference/v1"
SOURCE_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
MAX_BYTES = 2_000_000
CODE = re.compile(r"(?:000|001|002|003|600|601|603|605)\d{3}")


def _day(value):
    return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None and date.fromisoformat(value).isoformat() == value


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _price(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 < value <= 1_000_000


@lru_cache(maxsize=2)
def _read(path: str, size: int, modified: int, expected: str, day: str) -> dict:
    from pathlib import Path
    if size > MAX_BYTES:
        raise ValueError("Oversized reference archive")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Reference checksum mismatch")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("requested_date") != day
            or value.get("trusted") is not False or value.get("verification_status") != "source_reference_only"
            or value.get("source_url") != SOURCE_URL or not _hash(value.get("source_response_hash"))
            or type(value.get("total_source_rows")) is not int or not 0 <= value["total_source_rows"] <= 1000
            or not isinstance(value.get("rows"), list) or len(value["rows"]) > value["total_source_rows"]):
        raise ValueError("Invalid source reference metadata")
    rows = {}
    for row in value["rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("code"), str) or CODE.fullmatch(row["code"]) is None
                or row["code"] in rows or row.get("date") != day or not isinstance(row.get("name"), str)
                or not row["name"].strip() or len(row["name"]) > 100 or "ST" in row["name"].upper() or "退" in row["name"]
                or not _price(row.get("reported_price")) or not _price(row.get("archive_close"))
                or abs(row["reported_price"] - row["archive_close"]) > .005
                or row.get("price_matches_archive") is not True or row.get("source_reported_limit_up") is not True
                or row.get("verification_status") != "source_reference_only" or row.get("source_url") != SOURCE_URL
                or row.get("source_hash") != value["source_response_hash"]):
            raise ValueError("Invalid reference security/date/price")
        rows[row["code"]] = {key: row[key] for key in ("code", "name", "date", "reported_price", "archive_close", "source_reported_limit_up", "verification_status", "source_url", "source_hash")}
    current = Path(path).stat()
    if current.st_size != size or current.st_mtime_ns != modified:
        raise ValueError("Reference changed during reading")
    return {"rows": rows, "requested_date": day, "server_qdate_raw": value.get("server_qdate_raw"),
            "source_hash": value["source_response_hash"]}


def load_reference(code: str, day: str, archive_close=None) -> dict:
    result = {"status": "missing", "source_reported_limit_up": None, "reported_price": None,
              "verification_status": "source_reference_only", "source_url": None, "source_hash": None}
    try:
        if not isinstance(code, str) or CODE.fullmatch(code) is None or not _day(day):
            raise ValueError("Invalid reference query")
        root = market_history.directory() / "livermore"
        index = root / "references-index.json"
        if not index.is_file():
            return result
        if index.stat().st_size > 50_000:
            raise ValueError("Oversized reference index")
        value = json.loads(index.read_bytes())
        if value.get("schema") != INDEX_SCHEMA or not isinstance(value.get("files"), dict) or len(value["files"]) > 60:
            raise ValueError("Invalid reference index")
        item = value["files"].get(day)
        if item is None:
            return result
        if not isinstance(item, dict) or not _hash(item.get("sha256")) or item.get("path") not in {f"reference-{day}.json.gz", f"reference-{day}-{item['sha256'][:12]}.json.gz"}:
            raise ValueError("Invalid dated reference identity")
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Reference left archive")
        stat = path.stat()
        pool = _read(str(path), stat.st_size, stat.st_mtime_ns, item["sha256"], day)
        row = pool["rows"].get(code)
        if row is None:
            return {**result, "status": "not_reported", "source_hash": pool["source_hash"]}
        if archive_close is not None and (not _price(archive_close) or abs(archive_close - row["archive_close"]) > .005):
            raise ValueError("Reference no longer matches decision close")
        return {**row, "status": "reference_match", "server_qdate_raw": pool["server_qdate_raw"]}
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {**result, "status": "invalid"}
