"""Read original-document field checks without promoting them to PIT approval.

The offline verifier owns extraction. This reader binds each admitted value to
its indexed artifact and the archived official PDF; missing/corrupt evidence is
never replaced by a vendor reference or an optimistic business assumption.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from . import historical_fundamentals as finance

SCHEMA = "buffett-verified/v1"
MAX_MANIFEST = 2_000_000
MAX_PDF = 20_000_000
MAX_PAGES = 800
FIELDS = ("operating_cashflow", "net_profit", "roe_pct", "total_liabilities",
          "total_equity", "debt_to_equity", "shares_outstanding", "reported_capex")
MONEY_UNITS = {"元": 1e-6, "千元": .001, "万元": .01, "百万元": 1.0, "亿元": 100.0}
SHARE_UNITS = {"股": 1e-6, "万股": .01, "百万股": 1.0}


def directory() -> Path:
    return finance.directory() / "verified"


def _json(path: Path, limit: int) -> dict:
    if path.stat().st_size > limit:
        raise ValueError("Oversized evidence metadata")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid evidence metadata")
    return value


def _index() -> dict:
    value = _json(directory() / "index.json", finance.MAX_INDEX_BYTES)
    files = value.get("files")
    if value.get("schema") != SCHEMA or not isinstance(files, dict) or len(files) > finance.MAX_PROFILES:
        raise ValueError("Invalid verified index")
    for code, digest in files.items():
        if not isinstance(code, str) or re.fullmatch(finance.CODE_PATTERN, code) is None or not _sha(digest):
            raise ValueError("Invalid verified security/checksum")
    return files


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _number(value, maximum=1e12) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= maximum


def _close(left, right) -> bool:
    return _number(left) and _number(right) and abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.00000001") * max(Decimal(1), abs(Decimal(str(right))))


def _source(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.netloc == "static.cninfo.com.cn"
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/finalpage/\d{4}-\d{2}-\d{2}/\d{6,20}\.[Pp][Dd][Ff]", parsed.path) is not None)


@lru_cache(maxsize=32)
def _pdf_hash(path: str, size: int, modified_ns: int) -> str:
    # Cache only a short digest, not the PDF bytes; a changed file invalidates it.
    if not 0 < size <= MAX_PDF:
        raise ValueError("Invalid PDF size")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        if stream.read(4) != b"%PDF":
            raise ValueError("Not a PDF")
        stream.seek(0)
        while block := stream.read(1_048_576):
            digest.update(block)
    current = Path(path).stat()
    if current.st_size != size or current.st_mtime_ns != modified_ns:
        raise ValueError("PDF changed while reading")
    return digest.hexdigest()


def _document(manifest: dict, code: str, period: str, available: str) -> dict:
    if manifest.get("schema") != "buffett-originals/v1" or manifest.get("decision_date") != finance.CUTOFF:
        raise ValueError("Original manifest does not match the archive")
    records = manifest.get("reports")
    if not isinstance(records, dict):
        raise ValueError("Invalid original document index")
    item = records.get(f"{code}:{period[:4]}")
    if (not isinstance(item, dict) or item.get("code") != code or item.get("period_end") != period
            or item.get("publication_date") != available or item.get("status") != "downloaded"
            or item.get("revision_or_correction") is not False or not _sha(item.get("sha256"))
            or not _source(item.get("source_url"))):
        raise ValueError("Original publication identity is not verified")
    identity = item.get("announcement_id")
    if not isinstance(identity, str) or re.fullmatch(r"\d{6,20}", identity) is None or item.get("file") != f"{code}/{identity}.pdf":
        raise ValueError("Invalid original document path")
    if urlsplit(item["source_url"]).path.rsplit("/", 1)[-1].lower() != f"{identity}.pdf":
        raise ValueError("Mismatched original announcement")
    root = (finance.directory() / "originals").resolve()
    path = (root / item["file"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Original document left its archive")
    stat = path.stat()
    if stat.st_size != item.get("size") or _pdf_hash(str(path), stat.st_size, stat.st_mtime_ns) != item["sha256"]:
        raise ValueError("Original PDF checksum mismatch")
    return item


def _field(row: dict, field: str, document: dict):
    value = row.get(field)
    if value is None:
        return None, None
    proof = (row.get("field_evidence") or {}).get(field)
    if (not _number(value) or not isinstance(proof, dict) or proof.get("verification_status") != "matched_original"
            or proof.get("document_sha256") != document["sha256"] or proof.get("source_url") != document["source_url"]
            or type(proof.get("page")) is not int or not 1 <= proof["page"] <= MAX_PAGES
            or not isinstance(proof.get("quote"), str) or not 0 < len(proof["quote"]) <= 500
            or not _close(proof.get("value"), value) or not _number(proof.get("reported_value"), 1e16)
            or not _number(proof.get("scale"))):
        raise ValueError("A financial value has no matching document proof")
    units = SHARE_UNITS if field == "shares_outstanding" else {"%": 1.0} if field == "roe_pct" else {"倍": 1.0} if field == "debt_to_equity" else MONEY_UNITS
    scale = units.get(proof.get("unit"))
    if scale is None or not _close(scale, proof["scale"]) or not _close(proof["reported_value"] * scale, value):
        raise ValueError("Financial evidence unit/scale mismatch")
    if field in {"reported_capex", "total_liabilities", "debt_to_equity"} and value < 0 or field == "shares_outstanding" and value <= 0:
        raise ValueError("Financial field outside its domain")
    return value, {key: proof[key] for key in ("page", "quote", "unit", "scale", "reported_value", "value", "document_sha256", "source_url", "verification_status")}


def _empty(status="missing", detail="尚无通过原文逐字段核对的本地财报。") -> dict:
    return {"status": status, "verified_reports": [], "point_in_time_verified": False,
            "business_assessment": None, "issues": [detail]}


def load_verified(code: str, cutoff: str = finance.CUTOFF) -> dict:
    """Admit only matched original facts, never a caller's true/PIT flags."""
    if not isinstance(code, str) or re.fullmatch(finance.CODE_PATTERN, code) is None:
        return _empty("invalid", "股票范围不符合本地主板档案。")
    if finance._day(cutoff) != cutoff:
        return _empty("invalid", "财报判断日期无效。")
    if not (directory() / "index.json").is_file():
        return _empty()
    try:
        files = _index()
        if code not in files:
            return _empty()
        path = directory() / f"{code}.json.gz"
        if path.stat().st_size > finance.MAX_FILE_BYTES:
            raise ValueError("Oversized verified profile")
        with gzip.open(path, "rb") as stream:
            raw = stream.read(finance.MAX_FILE_BYTES + 1)
        if len(raw) > finance.MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != files[code]:
            raise ValueError("Verified profile checksum mismatch")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("code") != code
                or value.get("decision_date") != finance.CUTOFF or value.get("business_assessment") is not None
                or value.get("point_in_time_verified") is not False):
            raise ValueError("Unsupported verified profile or approval claims")
        source_rows = value.get("verified_reports")
        if not isinstance(source_rows, list) or len(source_rows) > 5:
            raise ValueError("Invalid verified report count")
        manifest = _json(finance.directory() / "originals" / "manifest.json", MAX_MANIFEST)
        reports, seen = [], set()
        for row in source_rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid verified report")
            period, available = row.get("period_end"), row.get("available_on")
            if finance._day(period) != period or period not in finance.EXPECTED_YEARS or finance._day(available) != available or not period <= available:
                raise ValueError("Invalid report/publication dates")
            if available >= cutoff:
                continue
            if period in seen or row.get("point_in_time_verified") is not False or row.get("maintenance_capex") is not None:
                raise ValueError("Duplicate version or unsupported financial approval")
            seen.add(period)
            document = _document(manifest, code, period, available)
            report = {"period_end": period, "available_on": available, "maintenance_capex": None,
                      "point_in_time_verified": False, "verification_kind": "original_values_cross_checked",
                      "field_evidence": {}, "issues": [str(issue)[:500] for issue in row.get("issues", [])[:20] if isinstance(issue, str)]}
            for field in FIELDS:
                number, proof = _field(row, field, document)
                report[field] = number
                if proof is not None:
                    report["field_evidence"][field] = proof
            if report["debt_to_equity"] is not None:
                debt, equity = report["total_liabilities"], report["total_equity"]
                if debt is None or equity is None or equity <= 0 or not _close(debt / equity, report["debt_to_equity"]):
                    raise ValueError("Debt ratio has no matching original components")
            if report["field_evidence"]:
                reports.append(report)
        return {"status": "partial_verified" if reports else "missing", "verified_reports": sorted(reports, key=lambda row: row["period_end"]),
                "point_in_time_verified": False, "business_assessment": None,
                "issues": ["已核对的是原文中的具体字段，历史更正版本与人工业务确认尚未完成。"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        return _empty("invalid", "原文财报档案、字段证明或文件指纹校验未通过，未接入该项数据。")


def integration_summary() -> dict:
    """Bounded public-data status for the main whole-universe workbench."""
    result = {"version": "buffett-data-integration/1", "scope": "whole_universe", "decision_date": finance.CUTOFF,
              "status": "unavailable", "reference_stocks": 0, "reference_year_rows": 0, "reference_complete_stocks": 0,
              "verified_stocks": 0, "verified_report_count": 0, "verified_field_count": 0, "strict_pit_stocks": 0,
              "manual_confirmation_available": False, "samples": [],
              "remaining": ["历史更正版本核验", "可靠同期股数（未核对的股票）", "维持性资本开支", "人工能力圈与护城河确认"],
              "limitations": ["原文字段核对不等于全部买入门槛通过，未确认项目继续阻止交易。", "当前数据接入不重算或覆盖过去的模拟订单。"]}
    try:
        coverage = _json(finance.directory() / "coverage.json", MAX_MANIFEST)
        counts = [coverage.get(key) for key in ("eligible_stocks", "annual_reference_rows", "complete_three_year_reference_stocks")]
        if (coverage.get("schema") != "financial-coverage/v1" or coverage.get("decision_date") != finance.CUTOFF
                or any(type(count) is not int or count < 0 for count in counts)
                or counts[0] > finance.MAX_PROFILES or counts[1] > counts[0] * 3 or counts[2] > counts[0]):
            raise ValueError("Invalid reference coverage")
        result.update(status="reference_only", reference_stocks=counts[0], reference_year_rows=counts[1], reference_complete_stocks=counts[2])
    except (OSError, ValueError, TypeError):
        pass
    try:
        files = _index()
        # A summary is intentionally bounded; no report corpus is loaded into
        # the browser or kept in memory. Current original archive has 6 stocks.
        for code in sorted(files)[:20]:
            profile = load_verified(code)
            reports = profile["verified_reports"]
            if not reports:
                continue
            fields = sum(len(row["field_evidence"]) for row in reports)
            result["verified_stocks"] += 1
            result["verified_report_count"] += len(reports)
            result["verified_field_count"] += fields
            result["samples"].append({"code": code, "report_count": len(reports), "field_count": fields, "pit_verified": False,
                                      "missing_fields": [field for field in ("operating_cashflow", "net_profit", "roe_pct", "debt_to_equity", "shares_outstanding", "maintenance_capex") if any(row.get(field) is None for row in reports)]})
        if len(files) > 20:
            result["limitations"].append("接入概览仅计本次校验的前20只原文档案，不冒充全量原文核验数量。")
        if result["verified_stocks"]:
            result["status"] = "partial_verified"
    except (OSError, ValueError, TypeError):
        result["limitations"].append("原文核验索引不可用，未用参考数据冒充核验结果。")
    return result
