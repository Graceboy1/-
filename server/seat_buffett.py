"""Buffett-inspired value discipline on original, synthetic financial cases.

This is a transparent teaching rule set, not Buffett's actual model, a trained
persona, or an investment recommendation. All thresholds are application demo
choices. The cash-flow proxy below is not a full owner-earnings calculation.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import math
import re
from typing import Any
from urllib.parse import urlsplit

from . import domain


_D = Decimal
_TICK = _D("0.01")
_REPORT_FIELDS = (
    "operating_cashflow", "maintenance_capex", "net_profit", "roe_pct",
    "debt_to_equity", "shares_outstanding",
)
_FIELD_LABELS = {
    "operating_cashflow": "经营活动现金流", "maintenance_capex": "维持性资本开支",
    "net_profit": "归母净利润", "roe_pct": "净资产收益率", "debt_to_equity": "负债权益比",
    "shares_outstanding": "同期总股数",
}
_FROZEN_FIELDS = ("action", "entry_low", "entry_high", "position_pct", "stop_loss", "take_profit")
_DISCOUNT = _D("0.10")
_TERMINAL = _D("0.02")
_SAFETY = _D("0.30")
_FORECAST_YEARS = 5


def _day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _number(value: Any) -> Decimal | None:
    if type(value) not in (int, float, Decimal):
        return None
    try:
        number = _D(str(value))
    except (ValueError, ArithmeticError):
        return None
    return number if number.is_finite() and abs(number) <= _D("1000000000000") else None


def _cashflow(report: dict) -> Decimal:
    return (_D(str(report["operating_cashflow"])) - _D(str(report["maintenance_capex"]))) / _D(str(report["shares_outstanding"]))


def _dcf(per_share: Decimal, growth: Decimal) -> Decimal:
    annual = per_share
    present = _D(0)
    for year in range(1, _FORECAST_YEARS + 1):
        annual *= 1 + growth
        present += annual / (1 + _DISCOUNT) ** year
    return present + annual * (1 + _TERMINAL) / (_DISCOUNT - _TERMINAL) / (1 + _DISCOUNT) ** _FORECAST_YEARS


def _field_provenance(raw: Any) -> dict:
    """Expose bounded public proof fields, never arbitrary vendor metadata.

    The original-document reader is responsible for matching proof to values.
    This serializer is not an alternate route to grant PIT verification.
    """
    if not isinstance(raw, dict):
        return {}
    result = {}
    for field in (*_REPORT_FIELDS, "reported_capex", "total_liabilities", "total_equity"):
        proof = raw.get(field)
        if not isinstance(proof, dict) or proof.get("verification_status") != "matched_original":
            continue
        source, digest, page = proof.get("source_url"), proof.get("document_sha256"), proof.get("page")
        if not isinstance(source, str) or len(source) > 1000 or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None or type(page) is not int or not 1 <= page <= 10_000:
            continue
        try:
            url = urlsplit(source)
            allowed = url.scheme == "https" and url.hostname in {"static.cninfo.com.cn", "www.cninfo.com.cn", "www.sse.com.cn", "disc.static.szse.cn", "www.szse.cn"} and not url.username and not url.password and not url.port
        except ValueError:
            allowed = False
        if not allowed:
            continue
        clean = {"source_url": source, "document_sha256": digest, "page": page,
                 "verification_status": "matched_original"}
        for key, limit in (("quote", 500), ("unit", 80)):
            value = proof.get(key)
            if isinstance(value, str):
                clean[key] = value[:limit]
        for key in ("scale", "value"):
            value = proof.get(key)
            if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e16:
                clean[key] = value
        result[field] = clean
    return result


def _fundamentals(case: dict, cutoff: date | None, last_close: Any) -> dict:
    issues: list[str] = []
    invalid = False
    reports: list[dict] = []
    assessment = None

    def issue(message: str, *, malformed: bool = False) -> None:
        nonlocal invalid
        if message not in issues:
            issues.append(message)
        invalid = invalid or malformed

    raw_reports = case.get("financial_reports")
    structure_valid = True
    require_pit = case.get("require_point_in_time", False)
    if type(require_pit) is not bool:
        issue("历史时点核验开关格式无效。", malformed=True)
        require_pit = True
    if cutoff is None:
        issue("决策截止日期无效，不能核验财务证据。", malformed=True)
    elif raw_reports is None:
        issue("缺少截至决策日已公开的年度财务报告。")
    elif not isinstance(raw_reports, list):
        issue("年度财务报告列表格式无效。", malformed=True)
    else:
        for raw in raw_reports:
            if not isinstance(raw, dict):
                issue("财务报告记录格式无效。", malformed=True)
                continue
            available = _day(raw.get("available_on"))
            if available is not None and available >= cutoff:
                # Date-only publication cannot prove availability before the
                # decision session's close. Admit same-day data only after a
                # future implementation supplies an auditable intraday time.
                continue
            if available is None:
                issue("财务报告缺少有效公告日期，不能确定当时是否可知。", malformed=True)
                continue
            period = _day(raw.get("period_end"))
            if period is None or (period.month, period.day) != (12, 31) or period > available:
                issue("已公告报告的年度截止日无效或晚于公告日期。", malformed=True)
                continue
            public = {"period_end": period.isoformat(), "available_on": available.isoformat()}
            for name in _REPORT_FIELDS:
                value = raw.get(name)
                number = _number(value)
                public[name] = float(number) if number is not None else None
                if value is None:
                    issue(f"{period.year} 年财报缺少{_FIELD_LABELS[name]}，不使用更旧年份替代。")
                elif number is None:
                    issue(f"{period.year} 年财报的{_FIELD_LABELS[name]}不是有效有限数值。", malformed=True)
                elif (name == "shares_outstanding" and number < _D("0.000001")) or (name in {"maintenance_capex", "debt_to_equity"} and number < 0) or (name == "roe_pct" and abs(number) > 1000):
                    issue(f"{period.year} 年财报的{_FIELD_LABELS[name]}超出有效范围。", malformed=True)
            if "point_in_time_verified" in raw or require_pit:
                public["point_in_time_verified"] = raw.get("point_in_time_verified") is True
            proof = _field_provenance(raw.get("field_evidence"))
            if proof:
                public["field_evidence"] = proof
            reports.append(public)

    reports.sort(key=lambda report: report["period_end"])
    years = [report["period_end"][:4] for report in reports]
    if len(years) != len(set(years)):
        issue("存在同一年度的重复报告，本版不猜测重述版本。", malformed=True)
    # Retain only a bounded number of public annual reports; never downgrade to
    # an older three-year subset when the latest report is incomplete.
    reports = reports[-5:]
    recent = reports[-3:]
    if len(recent) < 3:
        structure_valid = False
        issue("至少需要最近三个连续完整年度的已公开财务报告。")
    elif any(int(right["period_end"][:4]) - int(left["period_end"][:4]) != 1 for left, right in zip(recent, recent[1:])):
        structure_valid = False
        issue("最近三个年度不连续，不跳过缺年或回退到更旧组合。")
    if reports and cutoff and (cutoff - _day(reports[-1]["period_end"])).days > 550:
        structure_valid = False
        issue("最新年度报告的年度截止日距决策日超过 550 天，财务证据过旧。")

    pit_valid = not require_pit or structure_valid and all(row.get("point_in_time_verified") is True for row in recent)
    if require_pit and structure_valid and not pit_valid:
        issue("原始披露数值已核对，历史更正版本仍待核验。")
    # Capture financial quality before assessing the user's business evidence.
    # A missing moat must not erase independently known financial facts.
    financial_issues = list(issues)
    financial_valid = not invalid and not financial_issues

    raw_assessment = case.get("business_assessment")
    if not isinstance(raw_assessment, dict):
        issue("缺少能力圈与护城河的人工证据说明。", malformed=raw_assessment is not None)
    else:
        available = _day(raw_assessment.get("available_on"))
        if available is not None and cutoff is not None and available >= cutoff:
            issue("截至决策日没有可用的能力圈与护城河说明。")
        elif available is None or cutoff is None:
            issue("业务评估缺少有效的可用日期。", malformed=True)
        else:
            note = raw_assessment.get("note")
            circle = raw_assessment.get("within_circle")
            moat = raw_assessment.get("moat_supported")
            assessment = {"available_on": available.isoformat(), "within_circle": circle if type(circle) is bool else None,
                          "moat_supported": moat if type(moat) is bool else None,
                          "note": note.strip()[:500] if isinstance(note, str) else ""}
            if circle is None or moat is None or not assessment["note"]:
                issue("能力圈、护城河或人工说明证据不完整。")
            if (circle is not None and type(circle) is not bool) or (moat is not None and type(moat) is not bool) or (note is not None and not isinstance(note, str)):
                issue("业务评估的确认标记或说明格式无效。", malformed=True)

    assessment_valid = bool(assessment is not None and type(assessment["within_circle"]) is bool
                            and type(assessment["moat_supported"]) is bool and assessment["note"])
    status = "invalid" if invalid else "insufficient" if issues else "valid"
    valuation = {"owner_cashflow_proxy_per_share": None, "base_cashflow_proxy_per_share": None,
                 "conservative_value": None, "base_value": None, "buy_ceiling": None,
                 "margin_of_safety_pct": None, "discount_rate_pct": 10.0,
                 "conservative_growth_pct": 0.0, "base_growth_pct": 3.0,
                 "terminal_growth_pct": 2.0, "forecast_years": _FORECAST_YEARS}
    def ready(*fields: str) -> bool:
        return not invalid and structure_valid and all(all(row.get(field) is not None for field in fields) for row in recent)

    cashflow_ready = ready("operating_cashflow", "maintenance_capex", "shares_outstanding") and pit_valid
    cashflows = [_cashflow(row) for row in recent] if cashflow_ready else []
    if cashflows and financial_valid and not invalid:
        floor_cashflow = min(cashflows)
        average_cashflow = sum(cashflows) / len(cashflows)
        valuation.update(owner_cashflow_proxy_per_share=float(floor_cashflow), base_cashflow_proxy_per_share=float(average_cashflow))
        if floor_cashflow > 0:
            low_value = _dcf(floor_cashflow, _D(0))
            base_value = _dcf(average_cashflow, _D("0.03"))
            ceiling = (low_value * (1 - _SAFETY)).quantize(_TICK, rounding=ROUND_FLOOR)
            price = _number(last_close)
            valuation.update(conservative_value=round(float(low_value), 4), base_value=round(float(base_value), 4),
                             buy_ceiling=float(ceiling), margin_of_safety_pct=round(float((1 - price / low_value) * 100), 4) if price is not None and price > 0 else None)

    checks = []

    def check(code: str, label: str, passed: bool, detail: str) -> None:
        checks.append({"code": code, "label": label, "passed": passed, "detail": detail})

    check("history", "年度财务证据", financial_valid and not invalid, "最近三年完整连续、已公告且不过旧；金额和股数均以百万为单位。" if financial_valid and not invalid else "；".join(financial_issues[:3]) or "财务或人工记录格式异常，整体阻断。")
    check("profit", "持续正利润", ready("net_profit") and all(row["net_profit"] > 0 for row in recent), "最近三年净利润均须为正；本项只核对已接入的利润字段，不代替完整历史核验。")
    check("cashflow", "正现金流代理", cashflow_ready and all(value > 0 for value in cashflows), "最近三年经营现金流减维持性资本开支均须为正；不是完整的所有者收益。")
    check("roe", "资本回报", ready("roe_pct") and all(row["roe_pct"] >= 15 for row in recent), "演示门槛：最近三年 ROE 均不低于 15%，不是巴菲特的固定公式。")
    check("leverage", "负债约束", ready("debt_to_equity") and all(row["debt_to_equity"] <= 0.5 for row in recent), "演示门槛：最近三年债务/股东权益均不超过 0.5。")
    check("circle", "能力圈确认", not invalid and assessment_valid and assessment["within_circle"] is True, "须有决策日前的人工业务理解确认；不由 K 线推断。")
    check("moat", "护城河证据", not invalid and assessment_valid and assessment["moat_supported"] is True, "须有决策日前的人工竞争优势说明；合成案例中的确认仅为演示假设。")
    price = _number(last_close)
    safe_price = valuation["buy_ceiling"]
    margin_ok = financial_valid and not invalid and price is not None and price > 0 and safe_price is not None and safe_price >= 0.01 and price <= _D(str(safe_price))
    check("margin", "保守安全边际", margin_ok, "演示门槛：收盘价不高于保守估值的 70%；买入上限向下取整到分。")
    return {"status": status, "issues": issues, "reports": reports, "assessment": assessment,
            "financial_valid": financial_valid and not invalid, "assessment_valid": assessment_valid and not invalid,
            "valuation": valuation, "checks": checks, "eligible": all(item["passed"] for item in checks)}


def seat_evidence(case: dict) -> dict:
    snapshot = domain.evidence(case)
    details = _fundamentals(case, _day(case.get("decision_date")), snapshot["metrics"].get("last_close"))
    snapshot["fundamentals"] = details
    case_id = snapshot["case_id"]
    references = [f"{case_id}:fundamentals"]
    references.extend(f"{case_id}:financial:{row['period_end']}" for row in details["reports"])
    if details["assessment"] is not None:
        references.append(f"{case_id}:business-assessment")
    if details["valuation"]["conservative_value"] is not None:
        references.append(f"{case_id}:valuation")
    snapshot["evidence_ids"].extend(dict.fromkeys(references))
    return snapshot


def make_plan(snapshot: dict) -> domain.Plan:
    details = snapshot.get("fundamentals", {})
    refs = [ref for ref in snapshot.get("evidence_ids", []) if ":bar:" not in ref]
    metrics = snapshot.get("metrics", {})
    if not metrics.get("data_valid"):
        return domain.Plan(action="hold", rationale="行情证据无效，不生成价值入场参数。", evidence_ids=refs)
    if details.get("eligible") is not True:
        failed = [item["label"] for item in details.get("checks", []) if not item["passed"]]
        reason = "财务证据异常" if details.get("status") == "invalid" else "暂不满足价值买入条件"
        return domain.Plan(action="hold", rationale=f"{reason}：{'、'.join(failed) or '证据不足'}。保留现金，不以更乐观估值替代。", evidence_ids=refs)
    ceiling = details["valuation"]["buy_ceiling"]
    return domain.Plan(
        action="simulate", evidence_ids=refs,
        rationale=("公开财务与人工业务假设通过本版价值纪律。按最近三年最低现金流代理的保守 DCF 保留至少 30% 安全边际，"
                   f"仅在下一演示交易日开盘不高于 {ceiling:.2f} 元时尝试最多 10% 含费虚拟仓位。"
                   "不设日内价格止损、止盈或短期强制卖出；短期回放只能显示未实现估值，不验证长期投资成效。"),
        entry_low=0.01, entry_high=float(ceiling), position_pct=10.0,
        stop_loss=None, take_profit=None,
    )


def check_plan(plan: domain.Plan, snapshot: dict) -> dict:
    base = domain.validate_plan(plan, snapshot, use_price_thresholds=False)
    details = snapshot.get("fundamentals", {})
    frozen = make_plan(snapshot)
    required_refs = {ref for ref in frozen.evidence_ids if ":bar:" not in ref}
    checks = [*base["checks"], {
        "code": "buffett_financial_validity", "label": "已公开财务数据质量",
        "passed": details.get("status") in {"valid", "insufficient"},
        "detail": "不足时只能观望；已公开但格式、数值或日期异常时拦截整个回放。",
    }, {
        "code": "buffett_eligibility", "label": "价值买入门槛",
        "passed": details.get("eligible") is True or plan.action == "hold",
        "detail": "现金流、质量、业务理解和安全边际须同时通过；不满足则保留现金。",
    }, {
        "code": "buffett_frozen", "label": "冻结价值纪律",
        "passed": all(getattr(plan, name) == getattr(frozen, name) for name in _FROZEN_FIELDS),
        "detail": "动作、向下取整的安全价格上限、10% 含费仓位及无短期价格退出必须与证据规则一致。",
    }, {
        "code": "buffett_financial_refs", "label": "价值证据引用",
        "passed": required_refs <= set(plan.evidence_ids),
        "detail": "须引用公开财报、业务评估及实际使用的估值证据；不得只引用行情代替财务。",
    }]
    return {"ok": all(item["passed"] for item in checks), "checks": checks}


POLICY = domain.ReplayPolicy(max_holding_sessions=None, use_price_thresholds=False)


def _bar(day: date, opening: Decimal, close: Decimal, previous: Decimal) -> dict:
    high = max(opening, close) + _D("0.05")
    low = min(opening, close) - _D("0.05")
    return {"date": day.isoformat(), "open": float(opening), "high": float(high), "low": float(low),
            "close": float(close), "volume": 500000, "suspended": False,
            "limit_up": float((previous * _D("1.10")).quantize(_TICK, rounding=ROUND_HALF_UP)),
            "limit_down": float((previous * _D("0.90")).quantize(_TICK, rounding=ROUND_HALF_UP))}


def _case(case_id: str, index: int, label: str, *, price: str = "20.00", opening: str | None = None) -> dict:
    cutoff = date(2026, 10, 23)
    days = []
    cursor = cutoff
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    ending = _D(price)
    previous = ending - _D("0.80")
    bars = []
    for i, day in enumerate(days):
        close = ending - _D(39 - i) * _D("0.02")
        bars.append(_bar(day, close - _D("0.01"), close, previous))
        previous = close
    cursor = cutoff + timedelta(days=1)
    while len(bars) < 60:
        if cursor.weekday() < 5:
            op = _D(opening) if len(bars) == 40 and opening else previous + _D("0.02")
            close = op + _D("0.01")
            bars.append(_bar(cursor, op, close, previous))
            previous = close
        cursor += timedelta(days=1)
    financials = [{"period_end": f"{year}-12-31", "available_on": f"{year + 1}-04-20",
                   "operating_cashflow": cfo, "maintenance_capex": capex, "net_profit": profit,
                   "roe_pct": roe, "debt_to_equity": debt, "shares_outstanding": 100.0}
                  for year, cfo, capex, profit, roe, debt in (
                      (2023, 350.0, 80.0, 220.0, 18.0, 0.30),
                      (2024, 390.0, 90.0, 250.0, 19.5, 0.25),
                      (2025, 420.0, 100.0, 270.0, 20.0, 0.20))]
    return {"id": case_id, "label": label, "symbol": f"DEMO-BF{index:02d}",
            "name": f"价值风格合成样例{index:02d}", "decision_date": cutoff.isoformat(),
            "description": "原始合成年报、人工业务假设与日线；只演示价值纪律，不对应真实上市公司或收益。",
            "bars": bars, "financial_reports": financials,
            "business_assessment": {"available_on": "2026-10-20", "within_circle": True,
                                    "moat_supported": True, "note": "合成业务假设：产品和收入来源可理解，并有经人工确认的持续竞争优势；不是实际企业结论。"}}


CASES = {
    "buffett-value": _case("buffett-value", 1, "质量与安全边际通过"),
    "buffett-expensive": _case("buffett-expensive", 2, "优质但价格偏贵", price="35.00"),
    "buffett-cashflow": _case("buffett-cashflow", 3, "利润与现金流不匹配"),
    "buffett-missing": _case("buffett-missing", 4, "最新财务字段缺失"),
    "buffett-invalid": _case("buffett-invalid", 5, "公开财报数值异常"),
    "buffett-gap": _case("buffett-gap", 6, "开盘超过安全价格", price="21.50", opening="23.00"),
}
CASES["buffett-cashflow"]["financial_reports"][-1]["operating_cashflow"] = 90.0
del CASES["buffett-missing"]["financial_reports"][-1]["maintenance_capex"]
CASES["buffett-invalid"]["financial_reports"][-1]["shares_outstanding"] = 0.0
