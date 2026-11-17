"""Independent Livermore account gate; only disposable local evidence/accounts."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import sqlite3
import zlib

import pytest

from server import livermore_data as ld, paper_engine as p


CODE, DAY, DECISION = "600000", "2026-11-13", "2026-11-12"
INITIAL = datetime(2026, 11, 13, 1, 20, tzinfo=timezone.utc)
ORIGINAL_DECISION_LOADER = p._load_livermore_candidate


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    current = [INITIAL]
    monkeypatch.setattr(p, "utcnow", lambda: current[0])
    return current


@pytest.fixture(autouse=True)
def trusted_archive_contract(monkeypatch):
    """Isolate budget/time tests; separate tests use an actual gzip+index."""
    def load(code):
        value = candidate()
        case = {"symbol": code, "name": value["name"], "decision_date": DECISION, "archive_sha256": "b" * 64}
        snapshot = {"decision_date": DECISION, "metrics": {"data_valid": True, "eligible": True,
                    "range_position_120_pct": 8.0, "last_close": 11.0}}
        return case, snapshot, deepcopy(value["plans"]["livermore"])
    monkeypatch.setattr(p, "_load_livermore_candidate", load)


def provenance(day, **fields):
    return {"date": day, "source_url": "https://www.sse.com.cn/test-only", "source_hash": "a" * 64,
            "verification_status": "source_verified", **fields}


def evidence(*, region=True, amount=175_000, opening=11.33):
    return {"schema": ld.SCHEMA, "code": CODE, "records": {
        "limits": [provenance(DECISION, asof=DECISION+"T15:00:00+08:00", limit_up=11.0, limit_down=9.0,
                              security_name="测试公司", is_st=False, suspended=False)],
        "auctions": [provenance(DAY, asof=DAY+"T09:25:00+08:00", phase="opening_uncrossed", open=opening,
                                auction_amount=amount, amount_unit="yuan")],
        "index_auctions": [provenance(DAY, asof=DAY+"T09:25:00+08:00", phase="opening_uncrossed",
                                      symbol="000001.SH", change_pct=0.4)],
        "regions": [provenance(DAY, available_at=DAY+"T09:24:00+08:00", regional_ok=region,
                               note="测试人工确认", reviewed_by="fixture", verification_status="human_confirmed")],
    }}


def candidate(**changes):
    plan = {"action": "simulate", "eligible": True, "reason": "冻结候选测试", "entry_low": 11.01,
            "entry_high": 11.77, "position_pct": 5., "stop_loss": None, "take_profit": None,
            "missing": [], "rule_version": "fixture/1", "auction_verified": True,
            "entry_policy": "livermore-auction/v1", "requires_opening_auction": True,
            "livermore_evidence_hash": "c" * 64}
    result = {"code": CODE, "name": "测试公司", "source_hash": "b" * 64, "decision_date": DECISION,
              "history_end": DECISION, "prior_close": 11.0,
              "plans": {"baseline": {"action": "hold"}, "livermore": plan, "buffett": {"action": "hold"}},
              "execution_bar": {"date": DAY, "open": 11.33, "high": 11.5, "low": 11.0, "close": 11.4, "volume": 1_000_000}}
    result.update(changes)
    return result


def historical(value=None):
    p.begin_scan("owner", "historical", DAY, 1)
    return p.process_candidate("owner", "historical", DAY, candidate() if value is None else value)


def dash(mode="historical", **fields):
    return p.dashboard("owner", mode, day=DAY, **fields)


def account(mode="historical", seat="livermore"):
    return next(value for value in dash(mode)["accounts"] if value["seat_id"] == seat)


def live_setup(clock, *, frozen=None, value=None):
    if frozen is not None:
        clock[0] = frozen
    p.begin_scan("owner", "live", DAY)
    p.record_live_candidate("owner", DAY, candidate() if value is None else value)
    clock[0] = datetime(2026, 11, 13, 1, 30, 1, tzinfo=timezone.utc)


def quote(clock, **changes):
    result = {"code": CODE, "timestamp": clock[0].isoformat(), "price": 11.33, "prev_close": 11.0,
              "open": 11.33, "high": 11.5, "low": 11.0, "volume_shares": 1_000_000,
              "bid": 11.32, "ask": 11.34, "executable": True}
    result.update(changes)
    return result


def test_hist_fake_plan_verified_flag_never_substitutes_evidence():
    result = historical()
    assert result["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0 and account()["cash"] == 100000


@pytest.mark.parametrize("group", ["limits", "auctions", "index_auctions", "regions"])
def test_hist_partial_evidence_never_creates_order(group):
    value = evidence()
    value["records"][group] = []
    ld.save_profile(CODE, value)
    assert historical()["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


@pytest.mark.parametrize("region,cap,shares", [(True, 5000, 400), (False, 3500, 300)])
def test_hist_verified_gate_caps_budget_and_preserves_decision_fingerprint(region, cap, shares):
    ld.save_profile(CODE, evidence(region=region))
    value = candidate()
    before = deepcopy(value)
    assert historical(value)["outcomes"]["livermore"] == "buy"
    order = dash()["orders"]["items"][0]
    assert order["shares"] == shares and order["amount"] + order["fee"] <= cap
    assert order["decision_id"] == p._decision_id(before, "livermore", before["plans"]["livermore"])
    assert order["source_hash"] == before["source_hash"] and "执行证据指纹" in order["reason"]
    assert value == before


@pytest.mark.parametrize("amount,expected", [(100_000, "buy"), (99_999, "data_rejected")])
def test_hist_five_percent_limit_uses_real_auction_amount_not_day_volume(amount, expected):
    ld.save_profile(CODE, evidence(amount=amount))
    assert historical()["outcomes"]["livermore"] == expected


def test_hist_missing_prior_close_rejected_not_inferred_from_execution_bar():
    ld.save_profile(CODE, evidence())
    value = candidate()
    value.pop("prior_close")
    assert historical(value)["outcomes"]["livermore"] == "data_rejected"


def test_hist_gate_never_uses_future_close_for_allocation():
    ld.save_profile(CODE, evidence())
    value = candidate()
    value["execution_bar"].update(high=1000., close=999.)
    historical(value)
    order = dash()["orders"]["items"][0]
    assert order["shares"] == 400 and order["amount"] + order["fee"] <= 5000


def test_hist_changed_plan_budget_is_rejected_instead_of_mutating_frozen_plan():
    ld.save_profile(CODE, evidence())
    value = candidate()
    value["plans"]["livermore"]["position_pct"] = 3.
    assert historical(value)["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


def test_hist_existing_low_cash_remains_lower_than_gate_cap():
    ld.save_profile(CODE, evidence())
    p.begin_scan("owner", "historical", DAY, 1)
    with p.connection() as conn:
        conn.execute("UPDATE accounts SET cash=200000 WHERE seat='livermore'")
    p.process_candidate("owner", "historical", DAY, candidate())
    order = dash()["orders"]["items"][0]
    assert order["shares"] == 100 and order["amount"] + order["fee"] <= 2000


@pytest.mark.parametrize("cap", [None, True, 1_500_000, -1, 350_000.0])
def test_direct_buy_cannot_forget_or_forge_livermore_gate_budget(cap):
    p.begin_scan("owner", "historical", DAY)
    value = candidate()
    with p.connection() as conn:
        kind, _ = p._buy(conn, p._owner("owner"), "historical", DAY, "livermore", value,
                         value["plans"]["livermore"], 1133, DAY+"T09:30:00+08:00", 1140,
                         DAY+"T15:00:00+08:00", "test_only", budget_cap_cents=cap)
    assert kind == "data_rejected" and dash()["orders"]["total"] == 0


@pytest.mark.parametrize("bad", [{"budget_yuan": 15000}, {"allowed": "true"}, {"source_hash": "bad"}])
def test_gate_result_itself_is_checked_before_account_mutation(monkeypatch, bad):
    answer = {"allowed": True, "budget_yuan": 5000, "source_hash": "a" * 64, "reason": "test"}
    answer.update(bad)
    monkeypatch.setattr(ld, "entry_gate", lambda *args, **kwargs: answer)
    assert historical()["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


def test_other_seats_do_not_call_livermore_gate(monkeypatch):
    value = candidate()
    value["plans"]["baseline"] = deepcopy(value["plans"]["livermore"])
    value["plans"]["baseline"]["position_pct"] = 15.
    value["plans"]["livermore"] = {"action": "hold"}
    monkeypatch.setattr(ld, "entry_gate", lambda *args, **kwargs: pytest.fail("not a Livermore entry"))
    historical(value)
    order = dash()["orders"]["items"][0]
    assert order["seat_id"] == "baseline" and order["amount"] > 5000


def test_completed_old_account_and_orders_not_recomputed():
    ld.save_profile(CODE, evidence())
    p.run_historical("owner", [candidate()], DAY, total=1)
    before = dash()
    # Later imports are not a reason to rewrite a past ledger.
    changed = evidence(amount=1)
    ld.save_profile(CODE, changed)
    p.run_historical("owner", [candidate()], DAY, total=1)
    after = dash()
    assert after["orders"] == before["orders"] and after["accounts"] == before["accounts"]


def test_live_verified_first_observation_is_blocked_until_complete_exit_policy(clock):
    ld.save_profile(CODE, evidence())
    live_setup(clock)
    result = p.apply_quote("owner", quote(clock), now=clock[0])
    assert result["outcomes"]["livermore"] == "data_rejected"
    assert dash("live")["orders"]["total"] == 0
    assert "Z1" in dash("live")["events"]["items"][0]["reason"]


def test_live_verified_reduced_budget_still_blocked_until_exit_policy(clock):
    ld.save_profile(CODE, evidence(region=False))
    live_setup(clock)
    assert p.apply_quote("owner", quote(clock), now=clock[0])["outcomes"]["livermore"] == "data_rejected"
    assert dash("live")["orders"]["total"] == 0


def test_live_never_backfills_auction_after_930_plan_freeze(clock):
    ld.save_profile(CODE, evidence())
    live_setup(clock, frozen=datetime(2026, 11, 13, 1, 30, tzinfo=timezone.utc))
    result = p.apply_quote("owner", quote(clock), now=clock[0])
    assert result["outcomes"]["livermore"] == "data_rejected"
    assert dash("live")["orders"]["total"] == 0 and account("live")["cash"] == 100000


def test_live_never_buys_after_first_thirty_seconds(clock):
    ld.save_profile(CODE, evidence())
    live_setup(clock)
    clock[0] += timedelta(seconds=30)
    assert p.apply_quote("owner", quote(clock), now=clock[0])["outcomes"]["livermore"] == "data_rejected"
    assert dash("live")["orders"]["total"] == 0


@pytest.mark.parametrize("change", [{"open": 11.34}, {"open": None}, {"prev_close": 10.99}])
def test_live_opening_or_frozen_previous_close_mismatch_is_rejected(clock, change):
    ld.save_profile(CODE, evidence())
    live_setup(clock)
    assert p.apply_quote("owner", quote(clock, **change), now=clock[0])["outcomes"]["livermore"] == "data_rejected"
    assert dash("live")["orders"]["total"] == 0


def test_live_exit_does_not_silently_use_generic_deadline_instead_of_z1(clock):
    live_setup(clock)
    # Represent a pre-upgrade position directly; new live entry is disabled.
    with p.connection() as conn:
        conn.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p._owner("owner"), "live", "livermore", CODE, "测试公司", 100, 113800, 1133,
                      DAY, 1133, p.stamp(INITIAL), None, None, "d" * 64, "b" * 64))
        conn.execute("UPDATE accounts SET cash=cash-113800 WHERE seat='livermore'")
    p.stop_session("owner", "live", DAY)
    clock[0] = datetime(2026, 11, 16, 1, 20, tzinfo=timezone.utc)
    p.begin_scan("owner", "live", "2026-11-16")
    clock[0] = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)
    result = p.apply_quote("owner", quote(clock, price=11.7, high=11.8, bid=11.69, ask=11.71), now=clock[0])
    assert result["accepted"]
    view = p.dashboard("owner", "live", day="2026-11-16")
    assert view["orders"]["total"] == 0 and len(view["positions"]) == 1
    assert view["positions"][0]["mark_price"] == 11.7
    assert "Z1" in view["events"]["items"][0]["reason"]
    assert p.dashboard("owner", "live", day=DAY)["orders"]["total"] == 0


@pytest.mark.parametrize("field,value", [
    ("entry_low", 10.), ("entry_high", 12.), ("position_pct", 15.), ("stop_loss", 9.),
    ("take_profit", 12.), ("rule_version", "tampered"), ("livermore_evidence_hash", "d" * 64),
    ("entry_policy", "bypass"), ("requires_opening_auction", False),
])
def test_hist_every_frozen_parameter_is_rechecked(field, value):
    ld.save_profile(CODE, evidence())
    value_candidate = candidate()
    value_candidate["plans"]["livermore"][field] = value
    assert historical(value_candidate)["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


@pytest.mark.parametrize("change", [
    {"prior_close": 10.99}, {"source_hash": "d" * 64}, {"history_end": "2026-11-11"},
    {"decision_date": "2026-11-11"}, {"name": "冒名股票"},
])
def test_hist_caller_identity_date_and_previous_close_cannot_replace_archive(change):
    ld.save_profile(CODE, evidence())
    assert historical(candidate(**change))["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


def test_hist_unknown_original_archive_is_rejected(monkeypatch):
    ld.save_profile(CODE, evidence())
    monkeypatch.setattr(p, "_load_livermore_candidate", ORIGINAL_DECISION_LOADER)
    assert historical()["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0


@pytest.mark.parametrize("error", [EOFError("truncated fixture"), zlib.error("corrupt fixture")])
def test_hist_damaged_original_gzip_is_rejected_without_worker_failure(monkeypatch, error):
    ld.save_profile(CODE, evidence())
    def corrupt(_code):
        raise error
    monkeypatch.setattr(p, "_load_livermore_candidate", corrupt)
    assert historical()["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0 and account()["cash"] == 100000


@pytest.mark.parametrize("budget", [350_000, 500_000])
def test_direct_live_buy_is_blocked_even_with_an_explicit_gate_budget(clock, budget):
    live_setup(clock)
    value = candidate()
    with p.connection() as conn:
        kind, reason = p._buy(conn, p._owner("owner"), "live", DAY, "livermore", value,
                              value["plans"]["livermore"], 1133, p.stamp(clock[0]), 1133,
                              p.stamp(clock[0]), "test_only", budget_cap_cents=budget)
    assert kind == "data_rejected" and "Z1" in reason
    assert dash("live")["orders"]["total"] == 0


def actual_archive(*, high_position):
    """Authoritative local gzip + indexed checksum, not candidate metrics."""
    from server import market_history
    root = market_history.directory()
    (root / "daily").mkdir(parents=True, exist_ok=True)
    dates, current = [], date.fromisoformat(DECISION)
    while len(dates) < 120:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current -= timedelta(days=1)
    bars = [{"date": day, "open": 10., "high": 12. if high_position else 25., "low": 10.,
             "close": 10., "volume": 100000.} for day in sorted(dates)]
    bars[-1].update(open=10.5, high=11., low=10., close=11.)
    payload = {"code": CODE, "source": "generated-test", "data_mode": "historical", "adjustment": "none",
               "volume_unit": "shares", "downloaded_at": DAY+"T18:00:00+08:00", "bars": bars}
    raw = json.dumps(payload).encode()
    (root / "daily" / (CODE+".json.gz")).write_bytes(gzip.compress(raw))
    digest = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(root / "index.sqlite3") as conn:
        conn.execute("CREATE TABLE stocks(code TEXT PRIMARY KEY,name TEXT,status TEXT,sha256 TEXT)")
        conn.execute("INSERT INTO stocks VALUES(?,?,'ok',?)", (CODE, "测试公司", digest))
    return digest


def test_real_high_position_archive_blocks_forged_simulate_with_complete_auction(monkeypatch):
    ld.save_profile(CODE, evidence())
    digest = actual_archive(high_position=True)
    monkeypatch.setattr(p, "_load_livermore_candidate", ORIGINAL_DECISION_LOADER)
    case, ev, expected = ORIGINAL_DECISION_LOADER(CODE)
    assert ev["metrics"]["range_position_120_pct"] == 50.0 and expected["action"] == "hold"
    fake = candidate(source_hash=digest, position_120_pct=1.)
    assert historical(fake)["outcomes"]["livermore"] == "data_rejected"
    assert dash()["orders"]["total"] == 0 and account()["cash"] == 100000


def test_real_low_position_archive_and_exact_regenerated_plan_can_pass_historical(monkeypatch):
    ld.save_profile(CODE, evidence())
    digest = actual_archive(high_position=False)
    monkeypatch.setattr(p, "_load_livermore_candidate", ORIGINAL_DECISION_LOADER)
    _, ev, expected = ORIGINAL_DECISION_LOADER(CODE)
    assert ev["metrics"]["range_position_120_pct"] < 10 and expected["action"] == "simulate"
    value = candidate(source_hash=digest)
    value["plans"]["livermore"] = expected
    assert historical(value)["outcomes"]["livermore"] == "buy"
    assert dash()["orders"]["items"][0]["amount"] + dash()["orders"]["items"][0]["fee"] <= 5000

