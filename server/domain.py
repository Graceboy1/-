"""Deterministic, synthetic-trading evidence, gates, and daily-bar replay."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


STARTING_CASH = Decimal("100000.00")
MAX_POSITION_PCT = 20.0
LOT_SIZE = 100
MAX_HOLD_SESSIONS = 3
COMMISSION_RATE = Decimal("0.0003")
MIN_COMMISSION = Decimal("5.00")
SELL_TAX_RATE = Decimal("0.0005")
_TICK = Decimal("0.01")
_EPSILON = 1e-8
_PRICE_FIELDS = ("open", "high", "low", "close", "limit_up", "limit_down")
_TRADE_FIELDS = ("entry_low", "entry_high", "position_pct", "stop_loss", "take_profit")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    action: Literal["simulate", "hold"]
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(max_length=64)
    entry_low: float | None = None
    entry_high: float | None = None
    position_pct: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class EntryDecision:
    """A seat may reduce the frozen allocation, or reject the attempted entry."""

    budget: Decimal | None
    reason: str


@dataclass(frozen=True)
class ReplayPolicy:
    """Trusted simulation settings; not model-controlled fields."""

    max_holding_sessions: int | None = MAX_HOLD_SESSIONS
    use_price_thresholds: bool = True
    limit_up_deferrals: int = 0
    force_next_open_on_limit_down: bool = False
    commission_rate: Decimal = COMMISSION_RATE
    transfer_rate: Decimal = Decimal("0")
    sell_tax_rate: Decimal = SELL_TAX_RATE
    entry_decider: Callable[[dict, Plan, dict], EntryDecision] | None = None

    def __post_init__(self) -> None:
        if self.max_holding_sessions is not None and (type(self.max_holding_sessions) is not int or self.max_holding_sessions < 1):
            raise ValueError("max_holding_sessions must be positive or None")
        if type(self.limit_up_deferrals) is not int or self.limit_up_deferrals < 0:
            raise ValueError("limit_up_deferrals must be a non-negative integer")
        for rate in (self.commission_rate, self.transfer_rate, self.sell_tax_rate):
            if not isinstance(rate, Decimal) or not rate.is_finite() or not 0 <= rate <= 1:
                raise ValueError("fee rates must be finite Decimal fractions in [0, 1]")


DEFAULT_POLICY = ReplayPolicy()


def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _bar_error(row: dict) -> str | None:
    if not _is_date(row.get("date")):
        return "日期不是有效的 YYYY-MM-DD"
    if type(row.get("suspended")) is not bool:
        return "缺少明确的停牌状态"
    if not all(_number(row.get(key)) and row[key] >= 0 for key in (*_PRICE_FIELDS, "volume")):
        return "价格或成交量不是有限的非负数"
    if row["volume"] > 10**15 or any(row[key] > 10**6 for key in _PRICE_FIELDS):
        return "价格或成交量超出演示数据范围"
    if any(0 < row[key] < 0.01 or abs(row[key] * 100 - round(row[key] * 100)) > 1e-6 for key in _PRICE_FIELDS):
        return "演示行情价格未对齐 0.01 元刻度"
    if row["suspended"]:
        if row["volume"] or any(row[key] for key in ("open", "high", "low", "close")):
            return "停牌日应使用零量、零 OHLC 占位"
        return None
    op, hi, lo, cl, up, down = (row[key] for key in _PRICE_FIELDS)
    if not 0 < lo <= min(op, cl) <= max(op, cl) <= hi:
        return "OHLC 价格关系错误"
    if not 0 < down < up or lo < down - _EPSILON or hi > up + _EPSILON:
        return "行情价格与当日涨跌停区间矛盾"
    if row["volume"] <= 0:
        return "非停牌日没有有效成交量"
    return None


def _public_bar(row: dict) -> dict:
    """Shared whitelist for evidence and retrospective replay candles."""
    return {
        **{field: row.get(field) if _number(row.get(field)) else None for field in (*_PRICE_FIELDS, "volume")},
        "date": row["date"],
        "suspended": row["suspended"] if type(row.get("suspended")) is bool else None,
    }


def evidence(case: dict) -> dict:
    """Return a whitelist of copied, decision-time-only fields, never future outcomes."""
    case_id = case["id"]
    cutoff = case["decision_date"]
    cutoff_valid = _is_date(cutoff)
    bars = []
    for row in case["bars"]:
        if not (cutoff_valid and isinstance(row, dict) and isinstance(row.get("date"), str) and row["date"] <= cutoff):
            continue
        bars.append(_public_bar(row))
    issues = []
    if not cutoff_valid:
        issues.append("决策截止日期无效")
    dates = [row["date"] for row in bars]
    if dates != sorted(set(dates)):
        issues.append("决策前日线的日期不严格递增或有重复")
    if not bars or dates[-1] != cutoff:
        issues.append("缺少决策日已收盘数据")

    tradable = []
    for row in bars:
        issue = _bar_error(row)
        if issue:
            issues.append(f"{row['date']}：{issue}")
        elif not row["suspended"]:
            tradable.append(row)
    if len(tradable) < 20:
        issues.append("至少需要 20 根有效交易日线")
    if bars and bars[-1]["suspended"]:
        issues.append("决策日停牌，不能依赖旧收盘价生成交易计划")

    last_close = tradable[-1]["close"] if tradable else None
    closes = [row["close"] for row in tradable]
    mean_volume = fmean(row["volume"] for row in tradable[-6:-1]) if len(tradable) >= 6 else 0
    metrics = {
        "bar_count": len(bars), "tradable_count": len(tradable), "last_close": last_close,
        "sma5": round(fmean(closes[-5:]), 4) if len(closes) >= 5 else None,
        "sma20": round(fmean(closes[-20:]), 4) if len(closes) >= 20 else None,
        "return_5d_pct": round((last_close / closes[-6] - 1) * 100, 4) if len(closes) >= 6 else None,
        "volume_ratio_5d": round(tradable[-1]["volume"] / mean_volume, 4) if mean_volume else None,
        "data_valid": not issues, "data_issues": issues,
    }
    return {
        "case_id": case_id, "symbol": case["symbol"], "decision_date": cutoff,
        "bars": bars, "metrics": metrics,
        "evidence_ids": [f"{case_id}:metrics", *[f"{case_id}:bar:{day}" for day in dict.fromkeys(dates)]],
    }


def validate_plan(plan: Plan, evidence: dict, *, use_price_thresholds: bool = True) -> dict:
    checks: list[dict] = []

    def add(code: str, label: str, passed: bool, detail: str) -> None:
        checks.append({"code": code, "label": label, "passed": passed, "detail": detail})

    available_ids = set(evidence.get("evidence_ids", []))
    used_ids = set(plan.evidence_ids)
    refs_ok = bool(used_ids) and used_ids <= available_ids and len(used_ids) == len(plan.evidence_ids)
    add("evidence_refs", "证据引用", refs_ok,
        "引用均来自当前样例的决策截止前证据。" if refs_ok else "须引用本样例至少一条、无重复的决策时证据，不能引用未来或其他样例。")
    metrics = evidence.get("metrics", {})
    valid = metrics.get("data_valid") is True
    add("data_validity", "行情数据质量", valid,
        "至少 20 根有效日线、决策日收盘完整。" if valid else "；".join(metrics.get("data_issues", [])[:3]) or "缺少有效行情证据。")
    add("rationale", "决策说明", bool(plan.rationale.strip()), "必须说明模拟计划或观望的理由。")

    if plan.action == "hold":
        no_parameters = all(getattr(plan, field) is None for field in _TRADE_FIELDS)
        add("hold_no_trade", "观望无交易参数", no_parameters, "观望不得附带仓位、入场、保护或目标价格。")
    else:
        values = [getattr(plan, field) for field in (_TRADE_FIELDS if use_price_thresholds else _TRADE_FIELDS[:3])]
        complete = all(_number(value) and value > 0 for value in values)
        add("required_parameters", "虚拟交易参数", complete,
            "模拟需要正数入场区间、仓位、保护价与目标价。" if use_price_thresholds else "模拟需要正数入场区间和仓位。")
        allocation_ok = _number(plan.position_pct) and 0 < plan.position_pct <= MAX_POSITION_PCT
        add("position_cap", "虚拟仓位上限", allocation_ok, f"含买入费用的虚拟仓位必须大于 0% 且不超过 {MAX_POSITION_PCT:g}%。")
        if use_price_thresholds:
            prices_ok = complete and plan.stop_loss < plan.entry_low <= plan.entry_high < plan.take_profit
            add("price_order", "价格关系", prices_ok, "保护价 < 入场下限 ≤ 入场上限 < 目标价。")
        else:
            add("price_order", "价格关系", complete and plan.entry_low <= plan.entry_high, "入场下限 ≤ 入场上限。")
            add("no_price_thresholds", "席位退出纪律", plan.stop_loss is None and plan.take_profit is None,
                "退出按固定席位政策执行，计划不得附带止损止盈价格。")

    add("settlement", "日线回放约束", True,
        "仅尝试下一交易日开盘买入；买入当日不得卖出，随后只按收盘判定退出，不推断日内成交。"
        if use_price_thresholds else "仅尝试下一交易日开盘买入；买入当日不得卖出，随后按席位退出纪律判定。")
    return {"ok": all(check["passed"] for check in checks), "checks": checks}


def _money(value: Decimal) -> Decimal:
    return value.quantize(_TICK, rounding=ROUND_HALF_UP)


def _fees(notional: Decimal, policy: ReplayPolicy, *, sell: bool = False) -> Decimal:
    commission = _money(max(MIN_COMMISSION, notional * policy.commission_rate))
    transfer = _money(notional * policy.transfer_rate)
    return commission + transfer + (_money(notional * policy.sell_tax_rate) if sell else 0)


def _entry_inputs(case: dict, ev: dict, entry: dict) -> tuple[dict, dict]:
    # The callback must not see full-day high/low/close/volume or any later bar.
    known_case = {key: case[key] for key in ("id", "symbol", "name", "decision_date") if key in case}
    known_case["bars"] = [dict(row) for row in ev["bars"]]
    known_entry = {key: entry[key] for key in ("date", "open", "limit_up", "limit_down", "suspended")}
    snapshots = case.get("auction_snapshot")
    auction = snapshots.get(entry["date"]) if isinstance(snapshots, dict) else None
    if isinstance(auction, dict):
        if _number(auction.get("auction_amount")) and auction["auction_amount"] > 0:
            known_entry["auction_amount"] = auction["auction_amount"]
        if _number(auction.get("index_change_pct")):
            known_entry["index_change_pct"] = auction["index_change_pct"]
        if type(auction.get("regional_ok")) is bool:
            known_entry["regional_ok"] = auction["regional_ok"]
    return known_case, known_entry


def replay(
    case: dict, plan: Plan, validation: dict, *, policy: ReplayPolicy = DEFAULT_POLICY,
    validator: Callable[[Plan, dict], dict] = validate_plan,
    evidence_reader: Callable[[dict], dict] = evidence,
    include_bars: bool = False,
) -> dict:
    """Simulate one virtual position; never submit an order or infer intraday fills."""
    base_evidence = evidence(case)
    ev = base_evidence if evidence_reader is evidence else evidence_reader(case)
    checked = validate_plan(plan, ev, use_price_thresholds=policy.use_price_thresholds)
    extra_check = validator(plan.model_copy(deep=True), ev) if validator is not validate_plan and checked["ok"] else checked
    decision_date = case["decision_date"]
    events = []
    observed_bars: list[dict] = []
    cash = STARTING_CASH
    shares = holding_sessions = 0
    fees = realized = buy_cost = Decimal("0.00")
    mark = None
    is_open = False
    budget_used = None
    used_limit_up_deferrals = 0
    sold_at_open = False

    def event(day: str, kind: str, price: float | None, detail: str) -> None:
        events.append({"date": day, "type": kind, "price": price, "detail": detail})

    def finish(status: str, reason: str) -> dict:
        value = _money(mark * shares) if is_open and mark is not None else Decimal("0.00")
        equity = _money(cash + value)
        unrealized = _money(value - buy_cost) if is_open else Decimal("0.00")
        fee_note = f"虚拟费率：双边佣金 {(policy.commission_rate * 100).normalize():f}%，每笔最低 5 元；"
        if policy.transfer_rate:
            fee_note += f"双边过户费 {(policy.transfer_rate * 100).normalize():f}%；"
        fee_note += f"卖出另计 {(policy.sell_tax_rate * 100).normalize():f}%。不代表实际收费。"
        result = {"status": status, "reason": reason, "events": events, "metrics": {
            "starting_cash": float(STARTING_CASH), "ending_cash": float(cash), "ending_equity": float(equity),
            "realized_pnl": float(realized), "unrealized_pnl": float(unrealized),
            "return_pct": float((equity / STARTING_CASH - 1) * 100), "fees": float(fees),
            "shares": shares, "open_shares": shares if is_open else 0, "holding_sessions": holding_sessions,
            "is_open": is_open, "mark_price": float(mark) if is_open and mark is not None else None,
            "lot_size": LOT_SIZE, "max_holding_sessions": policy.max_holding_sessions,
            "commission_rate": float(policy.commission_rate), "min_commission": float(MIN_COMMISSION),
            "transfer_rate": float(policy.transfer_rate), "sell_tax_rate": float(policy.sell_tax_rate),
            "entry_budget": float(budget_used) if budget_used is not None else None,
            "limit_up_deferrals": policy.limit_up_deferrals, "used_limit_up_deferrals": used_limit_up_deferrals,
            "sold_at_open": sold_at_open,
            "fee_note": fee_note,
            "execution_note": "按夹具交易日及开盘/收盘撮合；不假设涨跌停队列成交，不模拟盘中触价、滑点或盘口深度。",
        }}
        if include_bars and status != "blocked":
            result["replay_bars"] = observed_bars
        return result

    if validation.get("ok") is not True or not checked["ok"] or extra_check.get("ok") is not True:
        failed = list(dict.fromkeys(check["label"] for result in (checked, extra_check) for check in result.get("checks", []) if not check["passed"]))
        reason = "风控未放行" + ("：" + "、".join(failed) if failed else "，未获得通过的校验结果")
        event(decision_date, "blocked", None, reason)
        return finish("blocked", reason)
    if plan.action == "hold":
        event(decision_date, "hold", None, "观望，不创建虚拟交易。")
        return finish("hold", "计划选择观望，不尝试成交。")

    if any(not isinstance(row, dict) or not _is_date(row.get("date")) for row in case["bars"]):
        event(decision_date, "blocked", None, "存在无法定位到交易日的日线，不跳过未知的买卖时点。")
        return finish("blocked", "回放数据有无效日期。")
    future = [row for row in case["bars"] if row["date"] > decision_date]
    if not future:
        return finish("pending", "夹具暂无决策日后的交易日，尚未尝试入场。")
    dates = [row["date"] for row in future]
    if dates != sorted(set(dates)):
        event(decision_date, "blocked", None, "回放交易日顺序异常，不推断成交。")
        return finish("blocked", "回放交易日乱序或重复。")

    entry = future[0]
    opening = entry.get("open")
    error = _bar_error(entry)
    # Only display valid rows visited by this run; never return the rest of the fixture.
    if include_bars and error is None:
        observed_bars.append(_public_bar(entry))
    rejection = None
    entry_reason = ""
    frozen_budget = STARTING_CASH * Decimal(str(plan.position_pct)) / 100
    global_budget = STARTING_CASH * Decimal(str(MAX_POSITION_PCT)) / 100
    budget = min(frozen_budget, global_budget)
    if error:
        rejection = "首个交易日数据异常：" + error
    elif entry["suspended"]:
        rejection = "首个交易日停牌，本计划不顺延买入。"
    elif policy.entry_decider is not None:
        known_case, known_entry = _entry_inputs(case, base_evidence, entry)
        try:
            decision = policy.entry_decider(known_case, plan.model_copy(deep=True), known_entry)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            decision = EntryDecision(None, "竞价证据缺失或无效，席位拒绝入场。")
        if not isinstance(decision, EntryDecision):
            rejection = "席位未返回有效竞价判定。"
        elif decision.budget is None:
            rejection = decision.reason or "席位拒绝入场。"
        elif not isinstance(decision.budget, Decimal) or not decision.budget.is_finite() or not 0 < decision.budget <= budget:
            rejection = "席位预算无效或超过冻结仓位/全局上限，拒绝入场。"
        else:
            budget, entry_reason = decision.budget, decision.reason
    if rejection is None:
        if opening >= entry["limit_up"] - _EPSILON:
            rejection = "开盘已到涨停价，日线无法证明排队成交，保守拒单。"
        elif opening <= entry["limit_down"] + _EPSILON:
            rejection = "开盘已到跌停价，按演示风控规则不入场。"
        elif not plan.entry_low - _EPSILON <= opening <= plan.entry_high + _EPSILON:
            rejection = "开盘价不在计划入场区间内，不追价也不回头补买。"
    if rejection:
        event(entry["date"], "no_fill", float(opening) if _number(opening) and opening > 0 else None, rejection)
        return finish("no_fill", rejection)

    price = Decimal(str(opening))
    budget_used = budget
    shares = int(budget // (price * LOT_SIZE)) * LOT_SIZE
    while shares and price * shares + _fees(price * shares, policy) > budget:
        shares -= LOT_SIZE
    if shares == 0:
        reason = f"计划仓位含费用不足以买入 {LOT_SIZE} 股。"
        event(entry["date"], "no_fill", float(opening), reason)
        return finish("no_fill", reason)

    notional = _money(price * shares)
    fees = _fees(notional, policy)
    buy_cost = notional + fees
    cash -= buy_cost
    mark = Decimal(str(entry["close"]))
    is_open = True
    event(entry["date"], "buy", float(price), (f"{entry_reason}；" if entry_reason else "") + f"虚拟开盘买入 {shares} 股，买入费用 {fees:.2f} 元。")
    event(entry["date"], "t_plus_one", None, "买入当日不能卖出；同日高低点或收盘触发不成交。")

    pending_exit = None
    exit_at_next_open = False
    for row in future[1:]:
        error = _bar_error(row)
        if error:
            event(row["date"], "exit_deferred", None, "回放数据异常，不推断成交：" + error)
            return finish("pending", "退出证据异常，保留未平仓状态并以最近有效收盘估值。")
        if include_bars:
            observed_bars.append(_public_bar(row))
        holding_sessions += 1
        day = row["date"]
        deadline = policy.max_holding_sessions is not None and holding_sessions >= policy.max_holding_sessions
        if row["suspended"]:
            if deadline and not pending_exit:
                pending_exit = "达到最长持有期"
            event(day, "exit_deferred", None, "停牌无有效收盘，不可退出。")
            continue

        close = row["close"]
        mark = Decimal(str(close))
        if exit_at_next_open:
            if row["open"] <= row["limit_down"] + _EPSILON:
                event(day, "exit_deferred", float(row["open"]), "开盘仍在跌停价，不推断成交，继续顺延开盘退出。")
                continue
            exit_price = row["open"]
            sold_at_open = True
        else:
            if not pending_exit:
                if policy.use_price_thresholds and close <= plan.stop_loss + _EPSILON:
                    pending_exit = "收盘触发保护价"
                elif policy.use_price_thresholds and close >= plan.take_profit - _EPSILON:
                    pending_exit = "收盘达到目标价"
                elif deadline:
                    pending_exit = "达到最长持有期"
            if not pending_exit:
                detail = ("收盘未触发保护、目标或最长持有期。" if policy.use_price_thresholds else
                          "价值持有观察：本版没有自动退出条件，后续基本面须另行复核。" if policy.max_holding_sessions is None else
                          "未到席位计划退出期限。")
                event(day, "monitor", float(close), detail)
                continue
            if close <= row["limit_down"] + _EPSILON:
                exit_at_next_open = policy.force_next_open_on_limit_down
                detail = "；顺延到下一可交易日开盘。" if exit_at_next_open else "。"
                event(day, "exit_deferred", float(close), pending_exit + "，但收盘封跌停，不假定卖出" + detail)
                continue
            if close >= row["limit_up"] - _EPSILON and used_limit_up_deferrals < policy.limit_up_deferrals:
                used_limit_up_deferrals += 1
                event(day, "exit_deferred", float(close), f"收盘触及涨停价，按席位规则顺延（{used_limit_up_deferrals}/{policy.limit_up_deferrals}）。")
                continue
            exit_price = close

        proceeds = _money(Decimal(str(exit_price)) * shares)
        sell_fee = _fees(proceeds, policy, sell=True)
        fees += sell_fee
        cash += proceeds - sell_fee
        realized = cash - STARTING_CASH
        is_open = False
        session = "开盘" if sold_at_open else "收盘"
        event(day, "sell", float(exit_price), f"{pending_exit}，虚拟{session}卖出 {shares} 股，卖出费用 {sell_fee:.2f} 元。")
        return finish("completed", pending_exit + "；已按保守日线口径完成虚拟回放。")

    return finish("pending", "回放截止仍未满足可成交退出条件；持仓按最后有效收盘估值，收益尚未实现。")
