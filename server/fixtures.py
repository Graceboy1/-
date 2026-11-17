"""Original, synthetic OHLCV scenarios. No real security or licensed data."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


_DECISION = date(2026, 10, 23)


def _price(value: Decimal | float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _bar(day, opening, high, low, close, volume, previous_close, *, suspended=False):
    previous = Decimal(str(previous_close))
    return {
        "date": day.isoformat() if isinstance(day, date) else day,
        "open": _price(opening), "high": _price(high), "low": _price(low), "close": _price(close),
        "volume": volume, "limit_up": _price(previous * Decimal("1.10")),
        "limit_down": _price(previous * Decimal("0.90")), "suspended": suspended,
    }


def _history():
    days = []
    day = _DECISION
    while len(days) < 24:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    previous = Decimal("9.52")
    rows = []
    for i, day in enumerate(reversed(days)):
        close = Decimal("9.54") + Decimal("0.02") * i
        rows.append(_bar(day, close - Decimal("0.03"), close + Decimal("0.12"),
                         close - Decimal("0.11"), close, 600_000 + i * 15_000, previous))
        previous = close
    return rows


def _case(case_id, number, label, description, future, *, broken_history=False):
    bars = _history()
    if broken_history:
        bars[-1]["high"] = 9.98  # Deliberate OHLC inconsistency for a fail-closed demo.
    return {
        "id": case_id, "label": label, "symbol": f"DEMO{number:03}",
        "name": f"合成演示{number:02}", "decision_date": _DECISION.isoformat(),
        "description": description, "bars": [*bars, *future],
    }


CASES = {
    "normal-fill": _case("normal-fill", 1, "正常成交 · T+1 退出",
        "合成场景：次日尝试开盘成交；即使买入当天走弱，也不允许当天卖出。", [
            _bar("2026-10-26", 10.04, 10.27, 9.35, 9.72, 1_350_000, 10),
            _bar("2026-10-27", 9.75, 10.68, 9.68, 10.65, 1_500_000, 9.72),
            _bar("2026-10-28", 10.62, 10.83, 10.53, 10.71, 930_000, 10.65),
            _bar("2026-10-29", 10.69, 10.90, 10.60, 10.77, 850_000, 10.71),
        ]),
    "limit-up-no-fill": _case("limit-up-no-fill", 2, "封板拒单 · 不追价",
        "合成场景：首个买入时点遇到涨停价，日线不能证明排队可成交。", [
            _bar("2026-10-26", 11, 11, 11, 11, 42_000, 10),
            _bar("2026-10-27", 10.94, 11.12, 10.65, 10.83, 1_100_000, 11),
        ]),
    "delayed-exit": _case("delayed-exit", 3, "退出受阻 · 停牌 / 跌停",
        "合成场景：买入后遇到停牌和跌停，未能退出时如实保留持仓。", [
            _bar("2026-10-26", 10.02, 10.19, 9.95, 10.05, 1_210_000, 10),
            _bar("2026-10-27", 0, 0, 0, 0, 0, 10.05, suspended=True),
            _bar("2026-10-28", 9.05, 9.05, 9.05, 9.05, 35_000, 10.05),
            _bar("2026-10-29", 9.19, 9.94, 9.11, 9.90, 1_300_000, 9.05),
        ]),
    "invalid-data": _case("invalid-data", 4, "异常数据 · 风控拒绝",
        "合成场景：决策日最高价小于收盘价，风控拦截计划，不虚构修复数据。", [
            _bar("2026-10-26", 10.04, 10.27, 9.35, 9.72, 1_350_000, 10),
        ], broken_history=True),
}
