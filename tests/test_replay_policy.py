from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from server.domain import DEFAULT_POLICY, EntryDecision, Plan, ReplayPolicy, evidence, replay, validate_plan
from server.fixtures import CASES, _bar


SEAT = ReplayPolicy(max_holding_sessions=1, use_price_thresholds=False, limit_up_deferrals=1,
                    force_next_open_on_limit_down=True)


def _plan(case, **changes):
    return Plan.model_validate({
        "action": "simulate", "rationale": "席位规则的合成样例。",
        "evidence_ids": [f"{case['id']}:metrics"],
        "entry_low": 9.95, "entry_high": 10.15, "position_pct": 10,
        **changes,
    })


def _simulate(case, policy=SEAT, **changes):
    plan = _plan(case, **changes)
    validation = validate_plan(plan, evidence(case), use_price_thresholds=policy.use_price_thresholds)
    return replay(case, plan, validation, policy=policy)


def _replace_after_entry(case, rows):
    copy = deepcopy(case)
    copy["bars"] = copy["bars"][:25] + rows
    return copy


def test_policy_and_entry_decisions_are_frozen_and_defaults_compatible():
    assert DEFAULT_POLICY == ReplayPolicy()
    with pytest.raises(FrozenInstanceError):
        DEFAULT_POLICY.max_holding_sessions = 1
    outcome = EntryDecision(Decimal("5000"), "允许试单")
    with pytest.raises(FrozenInstanceError):
        outcome.budget = Decimal("10000")


def test_seat_requires_no_price_thresholds_but_keeps_frozen_entry_constraints():
    case = CASES["normal-fill"]
    ev = evidence(case)
    assert not validate_plan(_plan(case), ev)["ok"]
    assert validate_plan(_plan(case), ev, use_price_thresholds=False)["ok"]
    for change in ({"stop_loss": 9.80}, {"take_profit": 10.60}, {"position_pct": 20.1}, {"entry_low": 11.0}):
        plan = _plan(case, **change)
        assert not validate_plan(plan, ev, use_price_thresholds=False)["ok"]
        assert replay(case, plan, {"ok": True}, policy=SEAT)["status"] == "blocked"


def test_policy_first_post_buy_close_can_exit_without_stop_or_target():
    result = _simulate(CASES["normal-fill"])
    assert result["status"] == "completed"
    assert result["events"][-1]["type"] == "sell"
    assert result["events"][-1]["date"] == "2026-10-27"
    assert result["metrics"]["holding_sessions"] == 1
    assert result["metrics"]["max_holding_sessions"] == 1
    assert not result["metrics"]["sold_at_open"]


def test_up_limit_closing_defers_once_not_indefinitely():
    case = _replace_after_entry(CASES["normal-fill"], [
        _bar("2026-10-27", 10.50, 10.69, 10.45, 10.69, 1_500_000, 9.72),
        _bar("2026-10-28", 11.20, 11.76, 10.96, 11.76, 1_500_000, 10.69),
    ])
    result = _simulate(case)
    assert result["status"] == "completed"
    assert result["events"][-2]["type"] == "exit_deferred"
    assert "涨停" in result["events"][-2]["detail"]
    assert result["events"][-1]["date"] == "2026-10-28"
    assert result["events"][-1]["price"] == 11.76
    assert result["metrics"]["used_limit_up_deferrals"] == 1


def test_down_limit_switches_to_next_tradeable_open_and_waits_through_halts_and_down_limit_open():
    case = _replace_after_entry(CASES["normal-fill"], [
        _bar("2026-10-27", 9.10, 9.15, 8.75, 8.75, 70_000, 9.72),
        _bar("2026-10-28", 0, 0, 0, 0, 0, 8.75, suspended=True),
        _bar("2026-10-29", 7.88, 8.40, 7.88, 8.12, 400_000, 8.75),
        _bar("2026-10-30", 8.02, 8.40, 7.90, 8.39, 500_000, 8.12),
    ])
    result = _simulate(case)
    assert result["status"] == "completed"
    assert [row["type"] for row in result["events"][2:]] == ["exit_deferred", "exit_deferred", "exit_deferred", "sell"]
    assert result["events"][-1]["date"] == "2026-10-30"
    assert result["events"][-1]["price"] == 8.02
    assert result["metrics"]["sold_at_open"]
    assert result["metrics"]["holding_sessions"] == 4


def test_force_open_does_not_infer_exit_from_incomplete_following_data():
    case = _replace_after_entry(CASES["normal-fill"], [
        _bar("2026-10-27", 9.10, 9.15, 8.75, 8.75, 70_000, 9.72),
    ])
    result = _simulate(case)
    assert result["status"] == "pending"
    assert result["metrics"]["is_open"]
    assert result["metrics"]["mark_price"] == 8.75
    assert result["metrics"]["realized_pnl"] == 0


def test_auction_callback_only_receives_knowable_fields_and_can_reduce_budget():
    case = deepcopy(CASES["normal-fill"])
    case["auction_snapshot"] = {
        "2026-10-26": {"auction_amount": 350000, "index_change_pct": 0.5, "regional_ok": True, "extra_future": 42},
        "2026-10-27": {"auction_amount": 999999},
    }

    def gate(known_case, plan, entry):
        assert set(known_case) == {"id", "symbol", "name", "decision_date", "bars"}
        assert max(row["date"] for row in known_case["bars"]) == case["decision_date"]
        assert set(entry) == {"date", "open", "limit_up", "limit_down", "suspended", "auction_amount", "index_change_pct", "regional_ok"}
        assert entry["auction_amount"] == 350000 and entry["index_change_pct"] == 0.5 and entry["regional_ok"]
        plan.position_pct = 100  # Callback copy cannot change the frozen plan.
        return EntryDecision(Decimal("5000"), "竞价通过，降档")

    result = _simulate(case, replace(SEAT, entry_decider=gate))
    assert result["status"] == "completed"
    assert result["metrics"]["entry_budget"] == 5000
    assert result["metrics"]["shares"] == 400
    assert "竞价通过" in result["events"][0]["detail"]


def test_auction_failure_does_not_use_day_volume_and_keeps_seat_reason():
    case = deepcopy(CASES["limit-up-no-fill"])

    def gate(_case, _plan, entry):
        assert "volume" not in entry and "auction_amount" not in entry
        return EntryDecision(None, "R3：拒绝涨停价开盘")

    result = _simulate(case, replace(SEAT, entry_decider=gate))
    assert result["status"] == "no_fill"
    assert result["reason"].startswith("R3")


@pytest.mark.parametrize("budget", [Decimal("10000.01"), Decimal("20001"), Decimal("NaN"), Decimal("0"), Decimal("-1")])
def test_callback_cannot_raise_frozen_or_global_budget(budget):
    policy = replace(SEAT, entry_decider=lambda *_: EntryDecision(budget, "伪造额度"))
    result = _simulate(CASES["normal-fill"], policy)
    assert result["status"] == "no_fill"
    assert "额度" not in result["reason"] and "预算无效" in result["reason"]


def test_new_fees_apply_to_each_side_and_reduce_lot_budget():
    policy = replace(SEAT, commission_rate=Decimal("0.001"), transfer_rate=Decimal("0.0001"), sell_tax_rate=Decimal("0.001"))
    result = _simulate(CASES["normal-fill"], policy)
    # 900 shares; buy 9036 -> 9.04 + 0.90; sell 9585 -> 9.59 + .96 + 9.59.
    assert result["metrics"]["shares"] == 900
    assert result["metrics"]["fees"] == 30.08
    assert result["metrics"]["realized_pnl"] == 518.92
    assert result["metrics"]["transfer_rate"] == 0.0001
    assert _simulate(CASES["normal-fill"], policy, position_pct=1.009)["status"] == "no_fill"


def test_real_seat_validator_rereads_enhanced_evidence_before_replay():
    case = CASES["normal-fill"]
    plan = _plan(case)

    def read(case):
        ev = evidence(case)
        ev["metrics"]["seat_gate"] = "closed"
        return ev

    def recheck(_plan, ev):
        assert ev["metrics"]["seat_gate"] == "closed"
        return {"ok": False, "checks": [{"passed": False, "label": "席位否决"}]}

    result = replay(case, plan, {"ok": True}, policy=SEAT, validator=recheck, evidence_reader=read, include_bars=True)
    assert result["status"] == "blocked"
    assert "replay_bars" not in result
    assert "席位否决" in result["reason"]
