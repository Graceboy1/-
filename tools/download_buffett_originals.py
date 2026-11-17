"""Archive a bounded set of original annual reports, not verified financial facts.

Only public CNINFO announcement search and its fixed static PDF host are used.
Downloading a report does not confirm its values, historical restatement status,
maintenance capital expenditure, circle of competence, or competitive moat.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from server import historical_fundamentals as finance

SCHEMA = "buffett-originals/v1"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
IDENTITY_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
STATIC_ORIGIN = "https://static.cninfo.com.cn/"
DEFAULT_CODES = ("600519", "000858", "000333", "600887", "600900", "600406")
DEFAULT_YEARS = (2023, 2024, 2025)
PAGE_SIZE = 30
MAX_PAGES = 20
MAX_QUERY_BYTES = 1_000_000
MAX_PDF_BYTES = 20_000_000
MAX_TOTAL_BYTES = 500_000_000
MAX_MANIFEST_BYTES = 2_000_000
INTERVAL = .4
CN = timezone(timedelta(hours=8))
VERIFICATION = "downloaded_original_not_field_verified"
LIMITATIONS = [
    "这里只归档原始披露文件；下载成功不等于财务字段、原始历史版本或后续重述已核验。",
    "未修改严格financial_reports、point_in_time_verified、能力圈或护城河人工确认。",
    "维持性资本开支未推断；不将总资本开支、折旧或零值冒充维持性资本开支。",
    "同年多份或修订/更正报告保留歧义，不自动挑选更有利版本。",
]


class SourceBlocked(RuntimeError):
    """An explicit source refusal must stop the entire source, not be retried."""


class ArchiveConflict(ValueError):
    """Existing originals must not be replaced by different or unverifiable bytes."""


class BudgetExceeded(ValueError):
    pass


def _day(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("Invalid cutoff date")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid cutoff date")
    return value


def options(codes, years, cutoff):
    cutoff = _day(cutoff)
    codes = list(dict.fromkeys(codes))
    years = sorted(set(years))
    if not 1 <= len(codes) <= 10 or any(not isinstance(code, str) or re.fullmatch(finance.CODE_PATTERN, code) is None for code in codes):
        raise ValueError("Supply one to ten explicit mainboard codes")
    if not 1 <= len(years) <= 3 or any(type(year) is not int or not 2000 <= year < int(cutoff[:4]) for year in years):
        raise ValueError("Supply one to three completed financial years")
    return codes, years, cutoff


def validate_universe(codes, market_root: Path):
    index = market_root / "index.sqlite3"
    if not index.is_file():
        raise ValueError("Download the dated public stock archive first")
    with sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for code in codes:
            row = conn.execute("SELECT name FROM stocks WHERE code=? AND status='ok'", (code,)).fetchone()
            if row is None or not isinstance(row[0], str) or "ST" in row[0].upper() or "退" in row[0]:
                raise ValueError("Requested stock is outside the eligible public archive")


def query_form(code, years, cutoff, page):
    options([code], years, cutoff)
    if type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError("Invalid announcement page")
    return {"pageNum": str(page), "pageSize": str(PAGE_SIZE),
            "column": "sse" if code.startswith("6") else "szse", "tabName": "fulltext",
            "stock": "", "searchkey": code, "secid": "", "plate": "",
            "category": "category_ndbg_szsh;", "trade": "",
            "seDate": f"{min(years)+1}-01-01~{cutoff}", "sortName": "time", "sortType": "asc", "isHLtitle": "true"}


def _check_response(response):
    if response.status_code in {401, 403, 429}:
        raise SourceBlocked(f"HTTP {response.status_code}; source stopped without bypass")
    if response.is_redirect:
        raise SourceBlocked("Source redirected; stopped without following it")
    response.raise_for_status()


def _read_response(response, limit):
    _check_response(response)
    length = response.headers.get("Content-Length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise BudgetExceeded("Response exceeded the bounded download budget")
    result = bytearray()
    for chunk in response.iter_bytes(chunk_size=16_384):
        result.extend(chunk)
        if len(result) > limit:
            raise BudgetExceeded("Response exceeded the bounded download budget")
    return bytes(result)


def _pdf_url(path, announcement_id):
    if not isinstance(path, str) or not isinstance(announcement_id, str) or re.fullmatch(r"\d{6,20}", announcement_id) is None:
        raise ValueError("Invalid announcement document identity")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "static.cninfo.com.cn" or parsed.query or parsed.fragment:
            raise ValueError("Unapproved original-document source")
        path = parsed.path.lstrip("/")
    match = re.fullmatch(r"finalpage/(\d{4}-\d{2}-\d{2})/(\d{6,20})\.[Pp][Dd][Ff]", path)
    if match is None or match[2] != announcement_id:
        raise ValueError("Invalid original-document path")
    _day(match[1])
    return STATIC_ORIGIN + path


def _announcement(row, code, years, cutoff):
    if not isinstance(row, dict) or row.get("secCode") != code:
        return None
    title = row.get("announcementTitle")
    if not isinstance(title, str) or len(title) > 1000:
        return None
    title = re.sub(r"<[^>]*>", "", html.unescape(title)).strip()
    if re.search(r"摘要|英文|english", title, re.I):
        return None
    matches = list(re.finditer(r"(20\d{2})\s*年?\s*(?:年度报告|年报)", title))
    if len(matches) != 1 or int(matches[0][1]) not in years:
        return None
    year = int(matches[0][1])
    revision = bool(re.search(r"修订|修正|更正|更新|补充", title))
    tail = title[matches[0].end():].strip()
    if not revision and (tail not in {"", "全文", "（全文）", "(全文)"} or re.search(r"关于|问询|提示|议案|决议|审议|审计", title[:matches[0].start()])):
        return None
    timestamp = row.get("announcementTime")
    if type(timestamp) is not int:
        return None
    try:
        published = datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(CN).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None
    if not f"{year}-12-31" < published < cutoff:
        return None
    announcement_id = row.get("announcementId")
    url = _pdf_url(row.get("adjunctUrl"), announcement_id)
    size = row.get("adjunctSize")
    if type(size) not in (int, float) or not math.isfinite(size) or size < 0:
        size = None
    return {"code": code, "year": year, "period_end": f"{year}-12-31",
            "announcement_id": announcement_id, "title": title, "publication_date": published,
            "source_url": url, "declared_size_kb": size, "revision_or_correction": revision}


def resolve_identity(client: httpx.Client, code):
    """Use the public stock identity search, never an inferred orgId formula."""
    if not isinstance(code, str) or re.fullmatch(finance.CODE_PATTERN, code) is None:
        raise ValueError("Invalid stock identity query")
    with client.stream("POST", IDENTITY_URL, data={"keyWord": code, "maxNum": "10"}) as response:
        body = _read_response(response, MAX_QUERY_BYTES)
    rows = json.loads(body)
    if not isinstance(rows, list) or len(rows) > 10:
        raise ValueError("Invalid public stock identity response")
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("code") != code or row.get("category") != "A股":
            continue
        delisted = row.get("delisted")
        if not (delisted is False or type(delisted) is str and delisted == "false"):
            continue
        org_id = row.get("orgId")
        if isinstance(org_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", org_id):
            identities.add(org_id)
    if len(identities) > 1:
        raise ValueError("Ambiguous public stock organization identity")
    if not identities:
        return None
    return {"code": code, "org_id": identities.pop(), "source_url": IDENTITY_URL,
            "response_sha256": hashlib.sha256(body).hexdigest()}


def _search_pages(client: httpx.Client, code, years, cutoff, identity=None):
    seen, total, pages = {}, None, 1
    for page in range(1, MAX_PAGES + 1):
        form = query_form(code, years, cutoff, page)
        if identity is not None:
            form.update(stock=f"{code},{identity['org_id']}", searchkey="")
        with client.stream("POST", QUERY_URL, data=form) as response:
            body = _read_response(response, MAX_QUERY_BYTES)
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("announcements"), (list, type(None))):
            raise ValueError("Invalid announcement search response")
        rows = payload.get("announcements") or []
        found = payload.get("totalAnnouncement")
        advertised_pages = payload.get("totalpages", 0)
        if type(found) is not int or found < 0 or len(rows) > PAGE_SIZE or type(advertised_pages) is not int or advertised_pages < 0:
            raise ValueError("Invalid announcement pagination")
        if total is None:
            total = found
            pages = max(1, math.ceil(found / PAGE_SIZE))
            if advertised_pages not in {0, pages}:
                raise ValueError("Inconsistent announcement page count")
            if pages > MAX_PAGES:
                raise ValueError("Announcement search exceeded its page cap")
        elif total != found:
            raise ValueError("Announcement search changed during pagination")
        expected_count = min(PAGE_SIZE, max(0, found - (page - 1) * PAGE_SIZE))
        if len(rows) != expected_count:
            raise ValueError("Incomplete announcement search page")
        for row in rows:
            if identity is not None and isinstance(row, dict) and row.get("secCode") == code and row.get("orgId") != identity["org_id"]:
                raise ValueError("Announcement organization does not match verified public identity")
            item = _announcement(row, code, years, cutoff)
            if item is None:
                continue
            if identity is not None:
                item["discovery_identity"] = identity
            key = item["announcement_id"]
            if key in seen and seen[key] != item:
                raise ValueError("Conflicting metadata for one announcement")
            seen[key] = item
        if page >= pages:
            break
        time.sleep(INTERVAL)
    return seen, total


def discover(client: httpx.Client, code, years, cutoff):
    """A successful empty text search may use verified exact stock identity.

    Refusal, redirect, malformed data or nonempty searches never trigger a
    different request path. Both normal searches use CNINFO's same query API.
    """
    seen, total = _search_pages(client, code, years, cutoff)
    if total == 0:
        time.sleep(INTERVAL)
        identity = resolve_identity(client, code)
        if identity is not None:
            time.sleep(INTERVAL)
            seen, _ = _search_pages(client, code, years, cutoff, identity)
    result = {}
    for year in years:
        candidates = sorted((item for item in seen.values() if item["year"] == year), key=lambda item: (item["publication_date"], item["announcement_id"]))
        if not candidates:
            result[year] = {"code": code, "year": year, "period_end": f"{year}-12-31", "status": "missing", "reason": "No unique complete Chinese annual report found before the cutoff"}
        elif len(candidates) != 1 or candidates[0]["revision_or_correction"]:
            result[year] = {"code": code, "year": year, "period_end": f"{year}-12-31", "status": "ambiguous", "candidates": candidates,
                            "reason": "Multiple versions or revision/correction require explicit document review"}
        else:
            result[year] = {**candidates[0], "status": "available"}
    return result


def _read_json(path: Path, limit=MAX_MANIFEST_BYTES):
    if path.stat().st_size > limit:
        raise ValueError("Oversized archive metadata")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid archive metadata")
    return value


def _save_json(path: Path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("Oversized archive metadata")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Budget:
    def __init__(self, maximum=MAX_TOTAL_BYTES):
        self.maximum, self.used = maximum, 0

    def charge(self, size):
        if self.used + size > self.maximum:
            raise BudgetExceeded("Originals exceeded the total archive budget")
        self.used += size


def archive_pdf(client: httpx.Client, target: Path, item: dict, budget: Budget, *, retrieved_at: str):
    """A matching cached hash can be reused; different existing bytes are kept."""
    code, identity = item["code"], item["announcement_id"]
    if re.fullmatch(finance.CODE_PATTERN, code) is None or _pdf_url(item["source_url"], identity) != item["source_url"]:
        raise ValueError("Invalid archive identity")
    folder = target / code
    pdf, sidecar = folder / f"{identity}.pdf", folder / f"{identity}.json"
    expected = None
    if sidecar.is_file():
        expected = _read_json(sidecar)
        if (expected.get("schema") != SCHEMA or any(expected.get(key) != item.get(key) for key in ("code", "announcement_id", "period_end", "publication_date", "source_url", "title"))
                or expected.get("verification_status") != VERIFICATION or not isinstance(expected.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", expected["sha256"]) is None
                or type(expected.get("size")) is not int or not 0 < expected["size"] <= MAX_PDF_BYTES):
            raise ArchiveConflict("Existing original metadata cannot be verified; kept unchanged")
    if pdf.exists():
        if not pdf.is_file() or expected is None or pdf.stat().st_size > MAX_PDF_BYTES:
            raise ArchiveConflict("Existing original cannot be verified; kept unchanged")
        raw = pdf.read_bytes()
        if (len(raw) != expected["size"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                or not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-4096:]):
            raise ArchiveConflict("Existing original hash differs; kept unchanged")
        budget.charge(len(raw))
        return {**expected, "status": "downloaded", "reused": True,
                "file": f"{code}/{identity}.pdf", "metadata_file": f"{code}/{identity}.json"}
    declared = item.get("declared_size_kb")
    if type(declared) in (int, float) and declared * 1024 > MAX_PDF_BYTES:
        raise BudgetExceeded("Declared original exceeds the per-document budget")
    limit = min(MAX_PDF_BYTES, budget.maximum - budget.used)
    if limit <= 0:
        raise BudgetExceeded("Originals exceeded the total archive budget")
    with client.stream("GET", item["source_url"]) as response:
        raw = _read_response(response, limit)
    if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-4096:]:
        raise ValueError("Document response is not a complete recognizable PDF")
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and (expected["sha256"] != digest or expected["size"] != len(raw)):
        raise ArchiveConflict("Source original differs from saved metadata; existing metadata kept")
    budget.charge(len(raw))
    metadata = {"schema": SCHEMA, **item, "status": "downloaded", "sha256": digest,
                "size": len(raw), "retrieved_at": retrieved_at, "verification_status": VERIFICATION}
    folder.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also preserves an original written by another process.
    try:
        with pdf.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        raise ArchiveConflict("An original appeared during download; not overwritten") from None
    _save_json(sidecar, metadata)
    return {**metadata, "reused": False, "file": f"{code}/{identity}.pdf", "metadata_file": f"{code}/{identity}.json"}


def _manifest(target, cutoff):
    path = target / "manifest.json"
    if path.is_file():
        value = _read_json(path)
        if (value.get("schema") != SCHEMA or value.get("decision_date") != cutoff or not isinstance(value.get("reports"), dict)
                or any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value["reports"].items())):
            raise ArchiveConflict("Existing original manifest does not match the requested cutoff")
        return value
    return {"schema": SCHEMA, "decision_date": cutoff, "source": "巨潮资讯网原始披露文件",
            "reports": {}, "limitations": list(LIMITATIONS)}


def run(client, codes, years, cutoff, target, *, retrieved_at):
    codes, years, cutoff = options(codes, years, cutoff)
    target = Path(target)
    value, budget = _manifest(target, cutoff), Budget()
    target.mkdir(parents=True, exist_ok=True)
    failures, stopped = 0, False
    for code in codes:
        try:
            reports = discover(client, code, years, cutoff)
            for year, item in reports.items():
                key = f"{code}:{year}"
                if item["status"] == "available":
                    try:
                        item = archive_pdf(client, target, item, budget, retrieved_at=retrieved_at)
                        print(f"{code} {year}: original saved; financial fields NOT verified", flush=True)
                    except ArchiveConflict:
                        item = {**item, "status": "conflict", "reason": "Existing original/metadata conflict; files kept unchanged"}
                    except (httpx.HTTPError, ValueError, OSError):
                        item = {**item, "status": "unavailable", "reason": "Original source or PDF validation unavailable; no substitute created"}
                    time.sleep(INTERVAL)
                failures += item["status"] != "downloaded"
                value["reports"][key] = item
                value["updated_at"] = retrieved_at
                _save_json(target / "manifest.json", value)
        except SourceBlocked:
            stopped = True
            value["source_status"] = "blocked"
            value["updated_at"] = retrieved_at
            _save_json(target / "manifest.json", value)
            break
        except (httpx.HTTPError, ValueError, OSError):
            failures += len(years)
            for year in years:
                key = f"{code}:{year}"
                # An interrupted discovery must not discard an earlier archive.
                if value["reports"].get(key, {}).get("status") != "downloaded":
                    value["reports"][key] = {"code": code, "year": year, "period_end": f"{year}-12-31", "status": "unavailable", "reason": "Announcement discovery or archive validation unavailable"}
            value["updated_at"] = retrieved_at
            _save_json(target / "manifest.json", value)
        if not stopped:
            time.sleep(INTERVAL)
    if not stopped:
        value["source_status"] = "completed_with_gaps" if failures else "completed"
    value["operation_bytes"] = budget.used
    value["updated_at"] = retrieved_at
    _save_json(target / "manifest.json", value)
    return 2 if stopped else 1 if failures else 0, value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", default=list(DEFAULT_CODES))
    parser.add_argument("--years", nargs="+", type=int, default=list(DEFAULT_YEARS))
    parser.add_argument("--cutoff", default=finance.CUTOFF)
    parser.add_argument("--dry-run", action="store_true", help="Validate the local public universe and print a plan without network or writes")
    args = parser.parse_args(argv)
    try:
        codes, years, cutoff = options(args.codes, args.years, args.cutoff)
        target = finance.directory() / "originals"
        validate_universe(codes, target.parent.parent)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "codes": codes, "years": years, "decision_date": cutoff,
                              "target": str(target), "query_url": QUERY_URL, "max_pdf_bytes": MAX_PDF_BYTES,
                              "max_total_bytes": MAX_TOTAL_BYTES, "limitations": LIMITATIONS}, ensure_ascii=False))
            return 0
        with httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
                          headers={"User-Agent": "SmartTradingAssistant-PersonalOriginalReportArchive/1.0"}) as client:
            result, manifest = run(client, codes, years, cutoff, target, retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        statuses = [manifest["reports"].get(f"{code}:{year}", {}).get("status", "not_attempted") for code in codes for year in years]
        print(f"Originals: {statuses.count('downloaded')} downloaded/reused, {len(statuses)-statuses.count('downloaded')} missing/ambiguous/unavailable; strict financial and human evidence unchanged")
        return result
    except (ValueError, OSError, sqlite3.Error):
        print("Original archive setup/validation unavailable; no credentials or synthetic financial facts used", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
