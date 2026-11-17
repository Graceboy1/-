"""Bounded, read-only annual financial references for real historical stocks.

The public vendor currently returns historical report rows, not certified
point-in-time versions. They remain reference data; missing maintenance capex,
share counts and human business assessments are never invented to pass rules.
"""
from __future__ import annotations

from datetime import date
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CUTOFF = "2026-11-12"
SCHEMA = "historical-fundamentals/v1"
MAX_FILE_BYTES = 250_000
MAX_INDEX_BYTES = 1_000_000
MAX_PROFILES = 6_000
EXPECTED_YEARS = ("2023-12-31", "2024-12-31", "2025-12-31")
CODE_PATTERN = r"(?:000|001|002|003|600|601|603|605)\d{3}"
SOURCE = "东方财富公开年度财务数据"
SOURCE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

TABLES = {
    "cashflow": {"reportName": "RPT_DMSK_FN_CASHFLOW", "date_field": "REPORT_DATE",
                 "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET"},
    "income": {"reportName": "RPT_DMSK_FN_INCOME", "date_field": "REPORT_DATE",
               "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,PARENT_NETPROFIT"},
    "balance": {"reportName": "RPT_DMSK_FN_BALANCE", "date_field": "REPORT_DATE",
                "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,TOTAL_LIABILITIES,TOTAL_EQUITY"},
    "indicators": {"reportName": "RPT_LICO_FN_CPD", "date_field": "REPORTDATE",
                   "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,NOTICE_DATE,UPDATE_DATE,WEIGHTAVG_ROE"},
}
LIMITATIONS = [
    "当前检索的历史年报字段不是不可变的当时版本，尚未核对原始披露文件及后续重述；仅作财务参考，不证明无未来信息。",
    "购建固定资产、无形资产和其他长期资产支付的现金是总资本开支，不等于维持性资本开支；未知maintenance_capex保留空值。",
    "未取得可靠的同期总股数，不用净利润/EPS、当前股本或股本金额估算；不据此计算每股现金流或估值。",
    "负债/权益采用报表总负债除以总权益，包含非有息负债，不是有息债务比率。",
    "能力圈和护城河须人工核对，新闻、日线或公司知名度不能代替确认。",
]


def directory() -> Path:
    archive = Path(os.environ.get("CONTEST_MARKET_DATA_DIR") or ROOT / "data" / "market-history" / "2026-11-13")
    return archive.resolve() / "fundamentals"


def _day(value) -> str | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value) is None:
        return None
    # Vendor date-time is date precision here. Never invent an intraday release.
    part = value[:10]
    try:
        return part if date.fromisoformat(part).isoformat() == part else None
    except ValueError:
        return None


def _number(value, *, million=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e16 or (nonnegative and value < 0):
        return None
    return value / 1_000_000 if million else value


def source_page(code: str) -> str:
    return f"https://emweb.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?code={'SH' if code.startswith('6') else 'SZ'}{code}&type=web"


def request_params(table: str, code: str) -> dict:
    if table not in TABLES or not isinstance(code, str) or re.fullmatch(CODE_PATTERN, code) is None:
        raise ValueError("Unsupported table or mainboard code")
    spec = TABLES[table]
    return {"reportName": spec["reportName"], "columns": spec["columns"],
            "filter": f'(SECURITY_CODE="{code}")', "pageSize": "20", "pageNumber": "1",
            "sortColumns": spec["date_field"], "sortTypes": "-1", "source": "WEB", "client": "WEB"}


def parse_table(payload: dict, table: str, code: str, *, cutoff: str = CUTOFF) -> list[dict]:
    """Select only exact-code annual rows made public before the decision day.

    A same-day date alone does not prove availability at the decision close.
    Current/recent UPDATE_DATE values are excluded instead of silently treating
    a restated row as the original report.
    """
    if table not in TABLES or _day(cutoff) != cutoff:
        raise ValueError("Invalid report query")
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("Financial source returned no valid result")
    rows = payload["result"].get("data")
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError("Financial source returned an oversized or invalid page")
    spec = TABLES[table]
    fields = spec["columns"].split(",")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("SECURITY_CODE") != code:
            raise ValueError("Mismatched financial security")
        period = _day(row.get(spec["date_field"]))
        notice = _day(row.get("NOTICE_DATE"))
        updated = _day(row.get("UPDATE_DATE")) if row.get("UPDATE_DATE") is not None else None
        if not period or not period.endswith("-12-31") or not "2023-12-31" <= period <= "2025-12-31":
            continue
        if not notice or notice >= cutoff or notice < period:
            continue
        if row.get("UPDATE_DATE") is not None and (not updated or updated >= cutoff or updated < notice):
            continue
        if period in seen:
            # Multiple versions are ambiguous, even if one looks more complete.
            raise ValueError("Duplicate annual financial report versions")
        seen.add(period)
        result.append({key: row.get(key) for key in fields})
    return sorted(result, key=lambda row: row[spec["date_field"]])


def _data_coverage(reports: list[dict]) -> dict:
    """Describe downloaded evidence, never business quality or trade approval.

    A negative profit or low ROE is still an available reference value. Missing
    original-version checks, maintenance spending, actual share counts and the
    user's business assessment remain separate from these public references.
    """
    years = {row["period_end"]: row for row in reports if row.get("period_end") in EXPECTED_YEARS}
    required = len(EXPECTED_YEARS)

    def count_fields(*fields: str) -> int:
        return sum(all(row.get(field) is not None for field in fields) for row in years.values())

    def reference(key: str, label: str, available: int, detail: str) -> dict:
        return {"key": key, "label": label, "status": "reference_only" if available == required else "missing",
                "available_count": available, "required_count": required, "detail": detail}

    four_tables = sum(set(TABLES) <= set(row.get("report_types", [])) for row in years.values())
    reference_fields = ("operating_cashflow", "reported_capex", "net_profit", "roe_pct", "total_liabilities", "total_equity", "debt_to_equity")
    complete_years = sum(set(TABLES) <= set(row.get("report_types", [])) and
                         all(row.get(field) is not None for field in reference_fields) for row in years.values())
    items = [
        reference("annual_reports", "三年四表年度参考", four_tables,
                  "需要2023、2024、2025年度的现金流、利润、资产负债和指标四表；四表齐全仅表示参考覆盖，不等于取得原始年报版本。"),
        reference("operating_cashflow", "经营活动现金流", count_fields("operating_cashflow"),
                  "金额单位为百万元；数值为零或负数仍属于已取得的数据，是否满足正现金流条件由策略另行判断。"),
        reference("net_profit", "归母净利润", count_fields("net_profit"),
                  "金额单位为百万元；不把亏损年份当成数据缺失，也不根据最新盈利补造其他年度。"),
        reference("roe_pct", "加权平均净资产收益率", count_fields("roe_pct"),
                  "单位为百分数，18表示18%；已取得不等于通过最近三年ROE门槛。"),
        reference("debt_to_equity", "总负债 / 权益比", count_fields("total_liabilities", "total_equity", "debt_to_equity"),
                  "保存总负债、总权益及可定义时的比值，不等于有息债务；权益非正时不强算负债比。"),
        reference("reported_capex", "报表总资本开支", count_fields("reported_capex"),
                  "购建固定资产、无形资产和其他长期资产支付的现金仅作总资本开支参考，不冒充维持性资本开支。"),
        reference("shares_outstanding", "可靠同期总股数", count_fields("shares_outstanding"),
                  "需要可核对的同期总股数，单位为百万股；不使用当前股本、净利润/EPS或股本金额推算。"),
        reference("maintenance_capex", "维持性资本开支", count_fields("maintenance_capex"),
                  "需单独核对维持现有经营所需的资本开支；总资本开支代理不能自动补入此字段。"),
        {"key": "point_in_time", "label": "原始公告版本核验",
         "status": "unverified" if years else "missing", "available_count": 0, "required_count": required,
         "detail": "需核对决策日前的原始披露文件、更正版本和字段来源；旧公告日期或当前下载成功不构成历史时点核验。"},
        {"key": "within_circle", "label": "能力圈人工确认", "status": "manual_required",
         "available_count": 0, "required_count": 1,
         "detail": "需用户说明是否理解这门生意及其风险；下载器不会自动勾选，也不能将今日判断伪装成历史时点已知。"},
        {"key": "moat_supported", "label": "护城河人工证据", "status": "manual_required",
         "available_count": 0, "required_count": 1,
         "detail": "需竞争优势的来源、持续性及失效风险说明；品牌知名度、新闻或价格上涨不能代替人工确认。"},
    ]
    return {"expected_years": list(EXPECTED_YEARS), "reference_year_count": complete_years, "items": items}


def normalize(code: str, tables: dict[str, list[dict]], *, retrieved_at: str, requests: list[dict] | None = None) -> dict:
    if not isinstance(code, str) or re.fullmatch(CODE_PATTERN, code) is None or not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("Invalid financial archive")
    by_table = {}
    for name, rows in tables.items():
        # Validate persisted data again; storage is never a shortcut around the
        # report-date, publication-date and exact-security gates.
        valid = parse_table({"success": True, "result": {"data": rows}}, name, code)
        by_table[name] = {_day(row[TABLES[name]["date_field"]]): row for row in valid}
    periods = sorted(set().union(*(set(rows) for rows in by_table.values())))
    reports = []
    for period in periods:
        cash = by_table["cashflow"].get(period, {})
        income = by_table["income"].get(period, {})
        balance = by_table["balance"].get(period, {})
        indicators = by_table["indicators"].get(period, {})
        observed = [row for row in (cash, income, balance, indicators) if row]
        # Use the last publication among every value included in this merged
        # row; never borrow a publication date from a different report type.
        available = max(_day(row["NOTICE_DATE"]) for row in observed)
        liabilities = _number(balance.get("TOTAL_LIABILITIES"), million=True, nonnegative=True)
        equity = _number(balance.get("TOTAL_EQUITY"), million=True)
        ratio = liabilities / equity if liabilities is not None and equity is not None and equity > 0 else None
        operating = _number(cash.get("NETCASH_OPERATE"), million=True)
        capex = _number(cash.get("CONSTRUCT_LONG_ASSET"), million=True, nonnegative=True)
        raw = {"period_end": period, "available_on": available,
               "operating_cashflow": operating, "reported_capex": capex,
               "net_profit": _number(income.get("PARENT_NETPROFIT"), million=True),
               "roe_pct": _number(indicators.get("WEIGHTAVG_ROE")),
               "debt_to_equity": ratio, "total_liabilities": liabilities, "total_equity": equity,
               "shares_outstanding": None, "maintenance_capex": None,
               "conservative_total_capex_proxy": operating - capex if operating is not None and capex is not None else None,
               "amount_unit": "million_yuan", "shares_unit": "million_shares",
               "source_url": source_page(code), "source": SOURCE,
               "point_in_time_verified": False, "restatement_status": "not_verified",
               "report_types": sorted(name for name, values in by_table.items() if period in values),
               "debt_basis": "total_liabilities / total_equity; not interest-bearing debt"}
        reports.append(raw)
    return {"status": "reference_only" if reports else "empty", "reports": reports,
            "data_coverage": _data_coverage(reports),
            # A historical period + early NOTICE_DATE do not prove these exact
            # downloaded values were available then. No currently fetched row
            # enters the original PIT decision rules without original-version
            # verification. Raw references remain visible for investigation.
            "financial_reports": [], "business_assessment": None, "point_in_time_verified": False,
            "provenance": {"source": SOURCE, "source_url": source_page(code), "retrieved_at": retrieved_at,
                           "decision_date": CUTOFF, "point_in_time_verified": False,
                           "amount_unit": "million_yuan", "shares_unit": "million_shares",
                           "requests": requests or []}, "limitations": list(LIMITATIONS)}


def _missing(status="missing", detail="本机尚未下载该股票的年度财务参考，不能以合成财报替代。") -> dict:
    return {"status": status, "reports": [], "financial_reports": [], "business_assessment": None,
            "data_coverage": _data_coverage([]),
            "point_in_time_verified": False,
            "provenance": {"source": SOURCE, "decision_date": CUTOFF, "point_in_time_verified": False},
            "limitations": [detail, *LIMITATIONS]}


def load_profile(code: str) -> dict:
    """Read one small checksummed local profile; never fetch or persist here."""
    if not isinstance(code, str) or re.fullmatch(CODE_PATTERN, code) is None:
        return _missing("invalid", "仅允许已下载范围内的沪深主板股票代码。")
    root = directory()
    index = root / "index.json"
    if not index.is_file():
        return _missing()
    try:
        if index.stat().st_size > MAX_INDEX_BYTES:
            raise ValueError("Financial index too large")
        entries = json.loads(index.read_bytes())
        if not isinstance(entries, dict) or entries.get("schema") != SCHEMA or not isinstance(entries.get("files"), dict) or len(entries["files"]) > MAX_PROFILES:
            raise ValueError("Invalid financial index")
        expected = entries["files"].get(code)
        if expected is None:
            return _missing()
        if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError("Invalid checksum")
        path = root / f"{code}.json.gz"
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Financial file too large")
        with gzip.open(path, "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Financial hash mismatch")
        value = json.loads(raw)
        if value.get("schema") != SCHEMA or value.get("code") != code or value.get("decision_date") != CUTOFF:
            raise ValueError("Financial archive mismatch")
        return normalize(code, value["tables"], retrieved_at=value["retrieved_at"], requests=value.get("requests"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return _missing("invalid", "该股票财务参考的校验未通过；未读取合成财报或猜测缺失字段。")


def profile_catalog() -> list[dict]:
    """At most forty locally verified reference shortcuts, never a buy list."""
    index = directory() / "index.json"
    if not index.is_file():
        return []
    try:
        if index.stat().st_size > MAX_INDEX_BYTES:
            return []
        value = json.loads(index.read_bytes())
        if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("files"), dict) or len(value["files"]) > MAX_PROFILES:
            return []
        codes = sorted(code for code in value["files"] if isinstance(code, str) and re.fullmatch(CODE_PATTERN, code))
        result = []
        for code in codes[:40]:
            profile = load_profile(code)
            if profile["status"] == "reference_only" and profile["reports"]:
                result.append({"code": code, "status": "reference_only", "report_count": len(profile["reports"])})
        return result
    except (OSError, ValueError, TypeError):
        return []
