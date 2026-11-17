"""Three seat adapters over one real, pre-cutoff historical price archive.

Synthetic fixtures remain useful for isolated engine tests, never as a fallback
for a missing real field. Price data, financial availability and order-execution
evidence have independent validity: a valid candle is not an executable order.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from . import domain, market_history, seat_buffett, seat_livermore, livermore_data, livermore_reference
from .agent import offline_plan

SEAT_IDS = ("baseline", "livermore", "buffett")
EXECUTION_GAPS = (
    "缺少当时证券状态与精确历史涨跌停价，不能按当前主板名单推定历史交易限制。",
    "缺少完整停复牌日历及真实竞价记录，不能用全天成交量代替竞价量。",
)


def _seat(seat_id: str) -> str:
    if seat_id not in SEAT_IDS:
        raise HTTPException(404, "未找到这个历史研究席位。")
    return seat_id


def _financial_profile(code: str) -> dict:
    # Reader only: selecting a stock never downloads data or calls a model.
    try:
        from .historical_fundamentals import load_profile
    except ImportError:
        return {"status": "missing", "financial_reports": [], "reports": [],
                "business_assessment": None, "provenance": {},
                "limitations": ["该股票暂无本地已核对的真实历史年度财报；不使用合成财报补齐。"]}
    try:
        profile = load_profile(code)
        if not isinstance(profile, dict):
            raise ValueError("Invalid local financial profile")
        return profile
    except (OSError, ValueError, TypeError):
        return {"status": "error", "financial_reports": [], "reports": [],
                "business_assessment": None, "provenance": {},
                "limitations": ["真实财报缓存校验未通过，未使用错误数据或合成财报补齐。"]}


def _published_rows(rows, cutoff: str) -> list[dict]:
    """Do not disclose a same-day/late-published report through raw details."""
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        available = seat_buffett._day(row.get("available_on"))
        period = seat_buffett._day(row.get("period_end"))
        if available is not None and period is not None and period <= available < date.fromisoformat(cutoff):
            result.append(deepcopy(row))
    return sorted(result, key=lambda row: row["period_end"])[-5:]


def _value_evidence(case: dict, snapshot: dict) -> None:
    profile = _financial_profile(case["symbol"])
    reports = _published_rows(profile.get("financial_reports"), snapshot["decision_date"])
    reports = [row for row in reports if profile.get("point_in_time_verified") is True or row.get("point_in_time_verified") is True]
    # Original-field checks are an independent lane, not an upgrade of vendor
    # references. They can explain known factors while their false PIT flag
    # continues to block the full financial/valuation/entry gate.
    from .buffett_verified import load_verified
    checked = load_verified(case["symbol"], snapshot["decision_date"])
    original_rows = _published_rows(checked.get("verified_reports"), snapshot["decision_date"])
    using_partial = not reports and bool(original_rows)
    if using_partial:
        reports = original_rows
    # A downloader cannot manufacture the user's circle of competence or a
    # moat confirmation. No artificial confirmations are injected here.
    financial_case = {**case, "financial_reports": reports, "require_point_in_time": using_partial,
                      "business_assessment": profile.get("business_assessment")}
    details = seat_buffett._fundamentals(financial_case, seat_buffett._day(snapshot["decision_date"]),
                                        snapshot["metrics"].get("last_close"))
    limitations = [str(value)[:500] for value in profile.get("limitations", [])[:10] if isinstance(value, str)]
    details.update(raw_reports=_published_rows(profile.get("reports"), snapshot["decision_date"]),
                   data_coverage=deepcopy(profile.get("data_coverage") or {}),
                   data_provenance=deepcopy(profile.get("provenance") or {}),
                   limitations=limitations,
                   data_status="partial_verified" if using_partial else profile.get("status", "missing"),
                   original_verification={"status": checked["status"], "report_count": len(original_rows),
                                          "field_count": sum(len(row.get("field_evidence", {})) for row in original_rows),
                                          "point_in_time_verified": False, "issues": checked.get("issues", [])})
    snapshot["fundamentals"] = details
    refs = [f"{case['id']}:fundamentals", *[
        f"{case['id']}:financial:{row['period_end']}" for row in details["reports"]]]
    if details["assessment"] is not None:
        refs.append(f"{case['id']}:business-assessment")
    if details["valuation"]["conservative_value"] is not None:
        refs.append(f"{case['id']}:valuation")
    snapshot["evidence_ids"].extend(ref for ref in refs if ref not in snapshot["evidence_ids"])
    snapshot["limitations"].extend(limitations)


def _livermore_evidence(case: dict, snapshot: dict) -> None:
    metrics = snapshot["metrics"]
    bars = snapshot["bars"]
    metrics.update(range_position_120_pct=None, range_high_120=None, range_low_120=None,
                   closed_at_limit_up=None, eligible=None, excluded_by_name=False,
                   candidate_reason="真实历史证据不足，保持观望。")
    reasons = []
    position_ok = None
    if not metrics["data_valid"]:
        position_ok = False
        reasons.append("缺少有效决策日行情或至少20根日线")
    elif len(bars) < 120:
        position_ok = False
        metrics["eligible"] = False
        reasons.append("不足120根真实日线，不缩短窗口替代原席位门槛")
    else:
        recent = bars[-120:]
        high = max(Decimal(str(row["high"])) for row in recent)
        low = min(Decimal(str(row["low"])) for row in recent)
        metrics.update(range_high_120=float(high), range_low_120=float(low))
        if high == low:
            position_ok = False
            metrics["eligible"] = False
            reasons.append("120日最高最低价相同，区间位置无定义")
        else:
            position = (Decimal(str(metrics["last_close"])) - low) / (high - low)
            metrics["range_position_120_pct"] = round(float(position * 100), 6)
            if position >= Decimal("0.10"):
                position_ok = False
                metrics["eligible"] = False
                reasons.append(f"120日区间位置{float(position * 100):.4f}%，未严格低于10%")
            else:
                position_ok = True
                reasons.append(f"真实120日区间位置{float(position * 100):.4f}%，价格位置门槛满足")
    # The archive deliberately contains null limits. Never synthesize a 10%
    # price and mistake it for the exchange's historical limit or a verified
    # limit-up event (corporate actions/ST status may change the real value).
    decision = livermore_data.load_decision(case["symbol"], snapshot["decision_date"])
    reference = livermore_reference.load_reference(case["symbol"], snapshot["decision_date"], metrics.get("last_close"))
    limits = decision.get("limits")
    security_ok = None
    if limits is not None:
        security_ok = not (limits["is_st"] or limits["suspended"] or "ST" in limits["security_name"].upper() or "退" in limits["security_name"])
        metrics["excluded_by_name"] = not security_ok
        if metrics.get("last_close") is not None:
            metrics["closed_at_limit_up"] = Decimal(str(metrics["last_close"])) == Decimal(str(limits["limit_up"]))
        metrics.update(limit_up=limits["limit_up"], limit_down=limits["limit_down"])
        reasons.append("决策日精确限价和证券状态已有来源档案")
        if not security_ok:
            reasons.append("当时证券状态不可入场（ST、退市标识或停牌）")
        if metrics["closed_at_limit_up"] is False:
            reasons.append("决策日收盘没有达到来源档案中的精确涨停价")
    else:
        reasons.extend(decision["missing"])
        reasons.append("收盘封涨停状态待核验；不把涨约10%直接判成涨停")
    if reference.get("status") == "reference_match":
        reasons.append("来源涨停池价格已与同日日线核对，仅作参考，不替代精确限价与历史状态")
    checks = {"history_and_position": position_ok, "historical_security": security_ok,
              "closed_at_known_limit": metrics["closed_at_limit_up"]}
    metrics["eligible"] = False if any(value is False for value in checks.values()) else True if all(value is True for value in checks.values()) else None
    # Execution facts belong to the later entry gate. Do not read next-day
    # auction/index/region records into this frozen decision-time snapshot.
    snapshot["livermore"] = {"checks": checks, "decision_missing": decision["missing"],
                             "decision_source_hash": decision["source_hash"], "reference": reference,
                             "entry_policy": "livermore-auction/v1", "execution_stage": "checked_separately_at_entry"}
    reasons.append("次日入场另核真实9:25竞价成交额、指数竞价及区域确认；缺项时保留现金")
    metrics["candidate_reason"] = "；".join(reasons) + "。"


def read_evidence(case: dict, seat_id: str) -> dict:
    _seat(seat_id)
    snapshot = market_history.read_evidence(case)
    snapshot.update(name=case["name"], seat_id=seat_id,
                    execution={"status": "unavailable", "ready": False, "mode": "historical_observation",
                               "missing": list(EXECUTION_GAPS),
                               "note": "真实日线用于席位研究和后续观察，不补造竞价、成交或持仓。"})
    snapshot["limitations"] = [
        "真实日线不复权；金额和成交量单位见各字段，不自动构造缺失字段。",
        "当前名单排除ST/退及非主板，但不是历史时点成分与证券状态重建。",
        *EXECUTION_GAPS,
    ]
    if seat_id == "livermore":
        _livermore_evidence(case, snapshot)
    elif seat_id == "buffett":
        _value_evidence(case, snapshot)
    return snapshot


def propose(case: dict, seat_id: str, snapshot: dict | None = None) -> domain.Plan:
    _seat(seat_id)
    snapshot = snapshot if snapshot is not None else read_evidence(case, seat_id)
    propose_plan = {"baseline": offline_plan, "livermore": seat_livermore.make_plan, "buffett": seat_buffett.make_plan}[seat_id]
    plan = propose_plan(snapshot)
    # Wording is historical, not a claim that a synthetic scenario was read.
    return plan.model_copy(update={"rationale": plan.rationale.replace("下一演示交易日", "下一历史交易日")})


def validate(case: dict, seat_id: str, plan: domain.Plan, snapshot: dict | None = None) -> dict:
    _seat(seat_id)
    snapshot = snapshot if snapshot is not None else read_evidence(case, seat_id)
    validator = {"baseline": domain.validate_plan, "livermore": seat_livermore.check_plan, "buffett": seat_buffett.check_plan}[seat_id]
    value = validator(plan, snapshot)
    checks = [*value["checks"], {
        "code": "execution_data", "label": "真实成交依据",
        "passed": plan.action == "hold",
        "detail": "本预案仅观望，不尝试买入；可观察真实后续价格，不计算成交收益。" if plan.action == "hold" else "；".join(EXECUTION_GAPS) + "条件预案已保存，但真实历史撮合被拦截。",
    }]
    return {"ok": all(check["passed"] for check in checks), "checks": checks}


def replay(case: dict, seat_id: str, plan: domain.Plan, validation: dict, *, include_bars: bool = False) -> dict:
    """Observe a real future session only after a valid frozen hold decision.

No execution branch is enabled until the missing historical order evidence is
supplied. In particular a real candle is never decorated with invented limits
just to pass the synthetic engine's validator.
"""
    checked = validate(case, seat_id, plan)
    passed = validation.get("ok") is True and checked["ok"] and plan.action == "hold"
    reason = "观望，不创建模拟订单；揭示的是真实后续日线，不是成交回放或收益证明。" if passed else "历史执行被拦截：" + "、".join(dict.fromkeys(
        check["label"] for value in (validation, checked) for check in value.get("checks", []) if not check["passed"]))
    event = {"date": case["decision_date"], "type": "hold" if passed else "blocked", "price": None, "detail": reason}
    result = {"status": "hold" if passed else "blocked", "reason": reason, "events": [event],
              "data_mode": "historical", "observation_only": True,
              "metrics": {"starting_cash": float(domain.STARTING_CASH), "ending_cash": float(domain.STARTING_CASH),
                          "ending_equity": float(domain.STARTING_CASH), "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                          "return_pct": 0.0, "fees": 0.0, "shares": 0, "open_shares": 0,
                          "holding_sessions": 0, "is_open": False, "mark_price": None,
                          "execution_note": "未执行任何买卖。虚拟现金未变，不代表策略回测收益；一日后续样本也不足以完成T+1退出。"}}
    if include_bars and passed:
        # The archive loader already validates and bounds every stored candle;
        # expose only the configured post-cutoff observation window.
        result["replay_bars"] = [deepcopy(row) for row in case["bars"]
                                 if case["decision_date"] < row["date"] <= market_history.REPLAY_DAY][-120:]
    return result
