"""Original single-security synthetic demo of the supplied package's v2.1 core rules.

This is not the complete strategy, a market-wide scanner, or a trained or
endorsed model. No original scripts, market data, or account records run here.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from . import domain


_D = Decimal
_TICK = _D("0.01")
_LOOKBACK = 120
_POSITION_LIMIT = _D("0.10")
_MAX_OPEN_GAIN = _D("0.07")
_QUEUE_GAIN = _D("0.095")
_MIN_INDEX_GAIN_PCT = _D("-3")
_MAX_AUCTION_SHARE = _D("0.05")
_FULL_BUDGET = _D("5000")
_REDUCED_BUDGET = _D("3500")
_FROZEN_FIELDS = ("action", "entry_low", "entry_high", "position_pct", "stop_loss", "take_profit")


def _decimal(value: Any) -> Decimal | None:
    if type(value) not in (int, float, Decimal):
        return None
    number = _D(str(value))
    return number if number.is_finite() else None


def _limit_price(previous_close: Decimal, multiplier: str) -> Decimal:
    return (previous_close * _D(multiplier)).quantize(_TICK, rounding=ROUND_HALF_UP)


def _entry_band(previous_close: Decimal) -> tuple[Decimal, Decimal]:
    # On the synthetic one-cent grid the open left endpoint becomes the
    # following tick. Floor the right endpoint, never round it above +7%.
    return previous_close + _TICK, (previous_close * (1 + _MAX_OPEN_GAIN)).quantize(_TICK, rounding=ROUND_FLOOR)


def seat_evidence(case: dict) -> dict:
    snapshot = domain.evidence(case)
    metrics = snapshot["metrics"]
    name = case.get("name")
    excluded = not isinstance(name, str) or not name.strip() or "ST" in name.upper() or "退" in name
    metrics.update(
        range_position_120_pct=None, range_high_120=None, range_low_120=None,
        closed_at_limit_up=None, excluded_by_name=excluded, eligible=None,
        candidate_reason="行情质量不足，无法核验候选。",
    )
    if not metrics["data_valid"]:
        return snapshot

    bars = [row for row in snapshot["bars"] if not row["suspended"]]
    latest = bars[-1]
    previous_close = _D(str(bars[-2]["close"]))
    close = _D(str(latest["close"]))
    limit = _limit_price(previous_close, "1.10")
    # The demo only generates ordinary 10%-limit bars. Validate the last
    # session's supplied limit against its previous tradable close as well.
    limit_consistent = _D(str(latest["limit_up"])) == limit
    limit_hit = limit_consistent and close == limit
    metrics["closed_at_limit_up"] = limit_hit
    position = None
    if len(bars) >= _LOOKBACK:
        recent = bars[-_LOOKBACK:]
        high = max(_D(str(row["high"])) for row in recent)
        low = min(_D(str(row["low"])) for row in recent)
        metrics.update(range_high_120=float(high), range_low_120=float(low))
        if high > low:
            position = (close - low) / (high - low)
            metrics["range_position_120_pct"] = round(float(position * 100), 6)

    if excluded:
        reason = "ST、退市标识或缺失标的名称，不纳入候选。"
    elif not limit_consistent:
        reason = "决策日涨停价与前收按 10% 计算的演示价不一致，不纳入候选。"
    elif not limit_hit:
        reason = "决策日收盘没有封涨停，不纳入候选。"
    elif len(bars) < _LOOKBACK:
        reason = "不足 120 根有效交易日线，不以更短区间降格筛选。"
    elif position is None:
        reason = "120 日最高与最低价相同，区间位置无定义。"
    elif position >= _POSITION_LIMIT:
        reason = f"120 日区间位置为 {position:.4%}，没有严格低于 10%，不降格或替补。"
    else:
        reason = f"决策日收盘涨停，120 日区间位置 {position:.4%} < 10%，且无 ST 或退市标识。"
        metrics.update(eligible=True, candidate_reason=reason)
        return snapshot
    metrics.update(eligible=False, candidate_reason=reason)
    return snapshot


def make_plan(snapshot: dict) -> domain.Plan:
    metrics = snapshot["metrics"]
    references = [f"{snapshot['case_id']}:metrics", f"{snapshot['case_id']}:bar:{snapshot['decision_date']}"]
    references = [ref for ref in references if ref in snapshot["evidence_ids"]]
    previous_close = _decimal(metrics.get("last_close"))
    if metrics.get("eligible") is not True or previous_close is None or previous_close <= 0:
        return domain.Plan(action="hold", rationale=metrics["candidate_reason"], evidence_ids=references)
    low, high = _entry_band(previous_close)
    if low > high:
        return domain.Plan(action="hold", rationale="入场区间内无有效价格刻度，观望。", evidence_ids=references)
    return domain.Plan(
        action="simulate", evidence_ids=references,
        rationale=(metrics["candidate_reason"] + " 下一演示交易日先检查竞价门槛与量能；"
                   "含费观察仓最多 5%，按 T+1 收盘规则退出，不设价格止损止盈。"),
        entry_low=float(low), entry_high=float(high), position_pct=5.0,
        stop_loss=None, take_profit=None,
    )


def check_plan(plan: domain.Plan, snapshot: dict) -> dict:
    base = domain.validate_plan(plan, snapshot, use_price_thresholds=False)
    frozen = make_plan(snapshot)
    metrics = snapshot["metrics"]
    checks = [*base["checks"], {
        "code": "livermore_candidate", "label": "v2.1 候选门槛",
        "passed": metrics.get("eligible") is True or plan.action == "hold",
        "detail": metrics["candidate_reason"] + (" 本预案观望，不尝试入场。" if plan.action == "hold" else ""),
    }, {
        "code": "livermore_frozen", "label": "v2.1 冻结参数",
        "passed": all(getattr(plan, field) == getattr(frozen, field) for field in _FROZEN_FIELDS),
        "detail": "动作、5% 含费仓位上限、入场刻度区间及无价格止损止盈的约束须与席位预案一致。",
    }]
    return {"ok": all(check["passed"] for check in checks), "checks": checks}


def decide_entry(case: dict, plan: domain.Plan, entry: dict) -> domain.EntryDecision:
    """Use the previous close and whitelisted opening-auction fields only."""
    # Replay supplies a decision-time-only case and a separate opening view.
    cutoff = case["decision_date"]
    previous = next((row for row in reversed(case["bars"]) if row["date"] == cutoff), {})
    previous_close = _decimal(previous.get("close"))
    opening = _decimal(entry.get("open"))
    if plan.action != "simulate" or previous_close is None or previous_close <= 0 or opening is None or opening <= 0:
        return domain.EntryDecision(None, "缺少有效前收、竞价开盘或模拟预案，不入场。")
    # Price comparisons against products avoid a division-rounding edge at
    # the inclusive and exclusive boundaries of the frozen band.
    gain = (opening - previous_close) / previous_close
    if opening <= previous_close:
        return domain.EntryDecision(None, f"R2：竞价涨幅 {gain:+.4%} ≤ 0%，平开或低开拒单。")
    if opening >= previous_close * (1 + _QUEUE_GAIN):
        return domain.EntryDecision(None, f"R3：竞价涨幅 {gain:+.4%} ≥ 9.5%，不假定排队可成交。")
    if opening > previous_close * (1 + _MAX_OPEN_GAIN):
        return domain.EntryDecision(None, f"R4：竞价涨幅 {gain:+.4%} > 7%，不追价。")

    auction_amount = _decimal(entry.get("auction_amount"))
    index_pct = _decimal(entry.get("index_change_pct"))
    region_ok = entry.get("regional_ok")
    if auction_amount is None or auction_amount <= 0 or index_pct is None or type(region_ok) is not bool:
        return domain.EntryDecision(None, "缺少有效竞价成交额、指数竞价涨跌或明确区域确认，保守拒单。")
    index_ok = index_pct > _MIN_INDEX_GAIN_PCT
    # B is the single-security adaptation of the original multi-pick gate.
    count = int(index_ok) + 1 + int(region_ok)
    detail = f"A指数 {'通过' if index_ok else '未通过'}／B单票竞价带通过／C区域 {'通过' if region_ok else '未通过'}"
    if count <= 1:
        return domain.EntryDecision(None, f"竞价门槛 {count}/3（{detail}），观望，不建仓。")
    budget = _FULL_BUDGET if count == 3 else _REDUCED_BUDGET
    if budget > auction_amount * _MAX_AUCTION_SHARE:
        share = budget / auction_amount
        return domain.EntryDecision(None, f"R0：预算占合成竞价成交额 {share:.4%} > 5%，拒单。")
    return domain.EntryDecision(budget, f"R1：竞价门槛 {count}/3（{detail}），含费观察仓预算 {budget:g} 元。")


POLICY = domain.ReplayPolicy(
    max_holding_sessions=1, use_price_thresholds=False, limit_up_deferrals=1,
    force_next_open_on_limit_down=True, commission_rate=_D("0.00025"),
    transfer_rate=_D("0.00001"), sell_tax_rate=_D("0.0005"), entry_decider=decide_entry,
)


# The following weekday sequence is a synthetic session calendar, not a
# reconstruction of exchange sessions or a record for a named security.
_DECISION_DAY = date(2026, 10, 23)
_OPEN_DAY = "2026-10-26"


def _bar(day, opening, high, low, close, previous_close, volume=800_000):
    previous = _D(str(previous_close))
    return {
        "date": day.isoformat() if isinstance(day, date) else day,
        "open": float(opening), "high": float(high), "low": float(low), "close": float(close),
        "volume": volume, "suspended": False,
        "limit_up": float(_limit_price(previous, "1.10")),
        "limit_down": float(_limit_price(previous, "0.90")),
    }


def _history(peak_close: str) -> list[dict]:
    sessions = []
    day = _DECISION_DAY
    while len(sessions) < _LOOKBACK:
        if day.weekday() < 5:
            sessions.append(day)
        day -= timedelta(days=1)
    sessions.reverse()
    peak = _D(peak_close)
    previous = peak + _D("0.08")
    bars = []
    for i, day in enumerate(sessions[:-1]):
        close = (peak - (peak - 10) * i / (_LOOKBACK - 2)).quantize(_TICK, rounding=ROUND_HALF_UP)
        bars.append(_bar(day, close + _D("0.02"), close + _D("0.08"), close - _D("0.10"), close, previous))
        previous = close
    bars.append(_bar(_DECISION_DAY, 10.10, 11.00, 9.92, 11.00, previous, 1_440_000))
    return bars


def _case(case_id, n, label, description, *, entry_open=11.33, auction_amount=175_000,
          regional_ok=True, peak_close="24.92", exit_bars=()):
    entry = _bar(_OPEN_DAY, entry_open, 11.50, 10.90, 11.38, 11.00, 1_200_000)
    ordinary_exit = _bar("2026-10-27", 11.40, 11.80, 11.26, 11.74, 11.38, 960_000)
    return {
        "id": case_id, "label": label, "symbol": f"DEMO-LV{n:02d}", "name": f"利弗莫尔合成样例{n:02d}",
        "decision_date": _DECISION_DAY.isoformat(), "description": description,
        "bars": [*_history(peak_close), entry, *(exit_bars or (ordinary_exit,))],
        "auction_snapshot": {_OPEN_DAY: {"auction_amount": auction_amount, "index_change_pct": 0.4, "regional_ok": regional_ok}},
    }


CASES = {
    "livermore-normal": _case("livermore-normal", 1, "低位涨停 · 竞价通过", "合成单标的：检验 120 日位置、竞价门槛与隔日收盘退出。"),
    "livermore-r2": _case("livermore-r2", 2, "平开拒单 · R2 边界", "合成单标的：验证入场带的开端点严格排除 0%。", entry_open=11.00),
    "livermore-no-candidate": _case("livermore-no-candidate", 3, "未入候选 · 区间边界", "合成单标的：验证 120 日区间位置等于 10% 时仍应观望。", peak_close="20.82"),
    "livermore-z1": _case("livermore-z1", 4, "涨停顺延 · Z1", "合成单标的：验证到期收盘涨停仅顺延一次。", exit_bars=(
        _bar("2026-10-27", 11.49, 12.52, 11.42, 12.52, 11.38),
        _bar("2026-10-28", 12.62, 13.77, 12.60, 13.77, 12.52),
    )),
    "livermore-r0": _case("livermore-r0", 5, "竞价量能不足 · R0", "合成单标的：预算占竞价成交额超过 5%，不拿全天成交量补足。", auction_amount=90_000),
    "livermore-limit-down": _case("livermore-limit-down", 6, "封跌停后 · 开盘退出", "合成单标的：到期收盘封跌停不假设卖出，随后验证可成交开盘退出。", exit_bars=(
        _bar("2026-10-27", 10.24, 10.24, 10.24, 10.24, 11.38),
        _bar("2026-10-28", 9.31, 9.95, 9.28, 9.80, 10.24),
    )),
    "livermore-reduced": _case("livermore-reduced", 7, "两项门槛 · 降至观察仓", "合成单标的：仅通过两项竞价门槛时，以 3,500 元含费预算回放。", regional_ok=False),
}
