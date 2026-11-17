"""Isolated virtual accounts: no broker, provider, real account or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from server import paper_engine as p

DAY = "2026-11-13"
NOW = datetime(2026,11,16,1,31,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv("CONTEST_PAPER_DB",str(tmp_path/"paper.sqlite3"))
    monkeypatch.setattr(p,"utcnow",lambda:NOW)


def candidate(code="600000", **changes):
    plan={"action":"simulate","eligible":True,"reason":"真实决策日前日线条件预案","entry_low":9.9,"entry_high":10.2,"position_pct":15.,"stop_loss":9.8,"take_profit":10.6,"rule_version":"unit/v1","missing":[]}
    value={"code":code,"name":"真实测试股票","source_hash":"a"*64,"decision_date":"2026-11-12","history_end":"2026-11-12",
           "plans":{"baseline":plan,"livermore":{"action":"hold","reason":"缺竞价","missing":["竞价"]},"buffett":{"action":"hold","reason":"缺财报","missing":["时点财报"]}},
           "execution_bar":{"date":DAY,"open":10.,"high":11.,"low":9.,"close":10.5,"volume":10000.}}
    value.update(changes)
    return value


def base(d):
    return next(a for a in d["accounts"] if a["seat_id"]=="baseline")


def run(*items,user="owner"):
    p.run_historical(user,items,DAY,total=len(items))
    return p.dashboard(user)


def test_dashboard_is_three_idle_templates_not_automatic_trades():
    d=p.dashboard("owner")
    assert len(d["accounts"])==3 and d["session"] is None
    assert all(a["cash"]==100000 and a["daily_pnl"] is None for a in d["accounts"])
    assert not d["positions"] and d["orders"]["total"]==0


def test_real_data_paper_buy_with_exact_fees_and_unrealized_only():
    d=run(candidate())
    assert d["session"]["status"]=="completed" and d["session"]["processed"]==1
    assert d["simulation_date"]==DAY and d["orders"]["total"]==1
    o=d["orders"]["items"][0]
    assert o["code"]=="600000" and o["name"]=="真实测试股票" and o["side"]=="buy"
    assert o["shares"]==1400 and o["price"]==10 and o["amount"]==14000 and o["fee"]==5
    assert o["event_time"]==DAY+"T09:30:00+08:00" and o["execution_assumption"]=="historical_open_estimate"
    a=base(d)
    assert a["cash"]==85995 and a["market_value"]==14700 and a["equity"]==100695
    assert a["realized_pnl"]==0 and a["unrealized_pnl"]==695 and a["daily_pnl"]==695
    assert a["total_pnl"]==a["realized_pnl"]+a["unrealized_pnl"]
    assert d["positions"][0]["sellable_shares"]==0
    assert d["positions"][0]["cost_including_fees"]==14005


def test_all_seats_independent_and_missing_rules_never_become_fake_buys():
    d=run(candidate())
    assert [a["position_count"] for a in d["accounts"] if a["seat_id"]!="baseline"]==[0,0]
    assert all(a["cash"]==100000 for a in d["accounts"] if a["seat_id"]!="baseline")
    assert {e["seat_id"] for e in d["events"]["items"]}==set(p.SEATS)
    assert all(o["seat_id"]=="baseline" for o in d["orders"]["items"])


def test_owner_and_modes_are_isolated():
    run(candidate(),user="owner-a")
    assert p.dashboard("owner-b")["orders"]["total"]==0
    assert p.dashboard("owner-a","live")["orders"]["total"]==0
    assert p.dashboard("owner-b")["positions"]==[]


def test_repeat_scan_resume_and_candidate_cannot_duplicate_orders_or_money():
    c=candidate();first=run(c,c)
    again=run(c)
    assert first["orders"]==again["orders"] and base(first)==base(again)
    assert again["session"]["processed"]==1
    assert not p.process_candidate("owner","historical",DAY,c)["accepted"]


def test_resuming_stopped_scan_preserves_cash_and_continues_new_codes():
    p.begin_scan("owner","historical",DAY,2)
    p.process_candidate("owner","historical",DAY,candidate())
    p.stop_session("owner","historical",DAY)
    assert not p.process_candidate("owner","historical",DAY,candidate("600001"))["accepted"]
    d=run(candidate(),candidate("600001"))
    assert d["orders"]["total"]==2 and d["session"]["processed"]==2


def test_historical_completed_session_cannot_be_rewritten_by_state_update():
    run(candidate())
    state=p.update_session("owner","historical",DAY,status="running",processed=0)
    assert state["status"]=="completed" and state["processed"]==1


def test_generator_cancel_is_stopped_not_completed_and_resumes_without_duplicate_orders():
    canceled=[False]
    def source():
        yield candidate()
        canceled[0]=True
    state=p.run_historical("owner",source(),DAY,total=2,stop_check=lambda:canceled[0])
    assert state["status"]=="stopped" and state["processed"]==1
    d=run(candidate(),candidate("600001"))
    assert d["session"]["status"]=="completed" and d["orders"]["total"]==2


def test_stop_during_generator_and_incomplete_iterator_do_not_fake_completion():
    def source():
        yield candidate()
        p.stop_session("owner","historical",DAY)
        yield candidate("600001")
    state=p.run_historical("owner",source(),DAY,total=2)
    assert state["status"]=="stopped" and state["processed"]==1
    assert p.finish_scan("owner","historical",DAY)["status"]=="stopped"
    incomplete=p.run_historical("other",[candidate()],DAY,total=2)
    assert incomplete["status"]=="error" and incomplete["processed"]==1


def test_inflight_scan_pins_its_database_configuration(monkeypatch,tmp_path):
    original=p.db_path()
    def source():
        yield candidate()
        monkeypatch.setenv("CONTEST_PAPER_DB",str(tmp_path/"another.sqlite3"))
        yield candidate("600001")
    p.run_historical("owner",source(),DAY,total=2)
    assert p.dashboard("owner")["orders"]["total"]==0
    monkeypatch.setenv("CONTEST_PAPER_DB",str(original))
    assert p.dashboard("owner")["orders"]["total"]==2


def test_all_universe_scanned_after_five_position_limit_and_audit_samples_bounded():
    d=run(*(candidate(str(600000+i)) for i in range(125)))
    assert d["session"]["processed"]==125 and d["orders"]["total"]==5
    a=base(d);assert a["position_count"]==5 and a["cash"]>=0 and a["counts"]["position_cap"]==120
    assert d["events"]["total"]==300
    assert len(d["events"]["items"])==20
    assert all(o["shares"]%100==0 for o in p.dashboard("owner",limit=50)["orders"]["items"])


@pytest.mark.parametrize("change",[
    {"code":"300001"},{"code":"688001"},{"code":"920001"},{"name":"*ST测试"},{"name":"退市测试"},
    {"source_hash":"not-a-hash"},{"history_end":DAY},{"decision_date":DAY},{"execution_bar":None},
    {"execution_bar":{"date":DAY,"open":10,"high":10,"low":10,"close":10,"volume":500}},
    {"execution_bar":{"date":DAY,"open":10,"high":11,"low":9,"close":10.5,"volume":0}},
    {"execution_bar":{"date":DAY,"open":10.005,"high":11,"low":9,"close":10.5,"volume":500}},
    {"execution_bar":{"date":DAY,"open":11,"high":12,"low":10,"close":11.5,"volume":500}},
])
def test_invalid_or_unfillable_candidates_do_not_create_orders(change):
    d=run(candidate(**change));assert d["orders"]["total"]==0 and base(d)["cash"]==100000


def test_future_close_never_changes_allocation_or_selection_order():
    a=candidate();b=candidate("600001");a["execution_bar"]["close"]=500.;a["execution_bar"]["high"]=501.
    d=run(a,b)
    assert sorted(o["shares"] for o in d["orders"]["items"])==[1400,1400]


def test_filters_are_bounded_and_owner_private_fields_are_not_returned():
    run(candidate(),candidate("600001"))
    d=p.dashboard("owner",seat_id="baseline",limit=1,offset=1)
    assert len(d["orders"]["items"])==1 and d["orders"]["total"]==2
    assert "owner" not in d["orders"]["items"][0] and "owner" not in d["session"]
    for opts in ({"limit":51},{"offset":-1},{"seat_id":"anything"},{"mode":"broker"}):
        with pytest.raises(ValueError):p.dashboard("owner",**opts)


def test_restart_pauses_orphaned_live_and_historical_jobs_without_deleting_orders():
    p.begin_scan("owner","historical",DAY);p.process_candidate("owner","historical",DAY,candidate())
    p.begin_scan("owner","live","2026-11-16")
    assert p.recover_after_restart()==2
    assert p.dashboard("owner")["session"]["status"]=="paused"
    assert p.dashboard("owner")["orders"]["total"]==1
    assert not p.dashboard("owner","live")["session"]["enabled"]


def live_candidate():
    return candidate(decision_date="2026-11-13",history_end="2026-11-13",prior_close=10.)


def quote(at,**changes):
    value={"code":"600000","name":"真实测试股票","timestamp":at.isoformat(),"price":10.,"prev_close":10.,"open":9.99,"high":10.2,"low":9.8,"volume_shares":100000,"executable":True,"bid":9.99,"ask":10.01}
    value.update(changes);return value


def setup_live(monkeypatch):
    current=[NOW]
    monkeypatch.setattr(p,"utcnow",lambda:current[0])
    p.begin_scan("owner","live","2026-11-16")
    p.record_live_candidate("owner","2026-11-16",live_candidate())
    current[0]+=timedelta(seconds=30)
    return current


def test_live_waits_for_new_quote_after_decision_and_uses_spread_estimate(monkeypatch):
    current=setup_live(monkeypatch)
    assert not p.apply_quote("owner",quote(NOW),now=current[0])["accepted"]
    assert p.apply_quote("owner",quote(current[0]),now=current[0])["outcomes"]["baseline"]=="buy"
    d=p.dashboard("owner","live")
    assert d["orders"]["total"]==1 and d["orders"]["items"][0]["price"]==10.01
    assert d["orders"]["items"][0]["execution_assumption"]=="live_ask_quote_estimate"
    assert d["positions"][0]["sellable_shares"]==0
    before=deepcopy(d["orders"]);p.record_live_candidate("owner","2026-11-16",live_candidate())
    p.apply_quote("owner",quote(current[0]),now=current[0]);assert p.dashboard("owner","live")["orders"]==before


@pytest.mark.parametrize("offset,changes",[
    (-91,{}),(1,{}),(0,{"executable":False}),(0,{"volume_shares":0}),(0,{"high":10.,"low":10.}),
    (0,{"prev_close":9.99}),(0,{"timestamp":"2026-11-16T19:00:00+08:00"}),
])
def test_live_stale_future_closed_invalid_or_split_quotes_cannot_trade(monkeypatch,offset,changes):
    current=setup_live(monkeypatch);at=current[0]+timedelta(seconds=offset)
    p.apply_quote("owner",quote(at,**changes),now=current[0])
    assert p.dashboard("owner","live")["orders"]["total"]==0


def test_live_pause_and_backdating_are_rejected(monkeypatch):
    current=setup_live(monkeypatch)
    with pytest.raises(ValueError):p.create_session("owner","live","2026-11-13")
    with pytest.raises(ValueError):p.record_live_candidate("owner","2026-11-16",live_candidate(),created_at=(NOW-timedelta(minutes=1)).isoformat())
    p.stop_session("owner","live","2026-11-16")
    assert not p.apply_quote("owner",quote(current[0]),now=current[0])["accepted"]


def test_resuming_live_does_not_accept_an_observation_from_before_resume(monkeypatch):
    current=setup_live(monkeypatch)
    p.stop_session("owner","live","2026-11-16")
    old_quote=quote(current[0])
    current[0]+=timedelta(seconds=30)
    p.begin_scan("owner","live","2026-11-16")
    assert not p.apply_quote("owner",old_quote,now=current[0])["accepted"]
    current[0]+=timedelta(seconds=30)
    assert p.apply_quote("owner",quote(current[0]),now=current[0])["outcomes"]["baseline"]=="buy"


def test_t_plus_one_and_next_day_closing_sell_keeps_old_day_snapshot(monkeypatch):
    current=setup_live(monkeypatch);p.apply_quote("owner",quote(current[0]),now=current[0])
    current[0]=datetime(2026,11,16,7,tzinfo=timezone.utc)
    p.apply_quote("owner",quote(current[0],price=10.8,high=10.9,ask=10.8,bid=10.79),now=current[0])
    assert p.dashboard("owner","live")["orders"]["total"]==1
    assert p.dashboard("owner","live")["accounts"][0]["realized_pnl"]==0
    p.stop_session("owner","live","2026-11-16")
    old=p.dashboard("owner","live",day="2026-11-16")
    current[0]=datetime(2026,11,17,1,31,tzinfo=timezone.utc)
    p.begin_scan("owner","live","2026-11-17")
    current[0]=datetime(2026,11,17,7,tzinfo=timezone.utc)
    p.apply_quote("owner",quote(current[0],price=10.8,high=10.9,ask=10.8,bid=10.79),now=current[0])
    d=p.dashboard("owner","live")
    assert d["orders"]["total"]==1 and d["orders"]["items"][0]["side"]=="sell"
    assert d["positions"]==[] and base(d)["realized_pnl"]>0
    assert base(d)["counts"]["sell"]==1
    restored=p.dashboard("owner","live",day="2026-11-16")
    assert base(restored)==base(old) and restored["positions"]==old["positions"]
    assert restored["orders"]["items"][0]["side"]=="buy"


def test_fresh_closing_only_quote_sells_t_plus_one_position_but_never_buys(monkeypatch):
    current=setup_live(monkeypatch)
    p.apply_quote("owner",quote(current[0]),now=current[0])
    p.stop_session("owner","live","2026-11-16")
    current[0]=datetime(2026,11,17,1,31,tzinfo=timezone.utc)
    p.begin_scan("owner","live","2026-11-17")
    fresh=live_candidate();fresh.update(code="600001",decision_date="2026-11-16",history_end="2026-11-16")
    p.record_live_candidate("owner","2026-11-17",fresh)
    closing=datetime(2026,11,17,7,tzinfo=timezone.utc);current[0]=closing+timedelta(seconds=30)
    response=p.apply_quote("owner",quote(closing,price=10.8,high=10.9,bid=10.79,ask=10.8,executable=False,closing_only=True),now=current[0])
    assert response["accepted"]
    p.apply_quote("owner",quote(closing,code="600001",executable=False,closing_only=True),now=current[0])
    d=p.dashboard("owner","live")
    assert d["orders"]["total"]==1 and d["orders"]["items"][0]["side"]=="sell" and not d["positions"]
    assert base(d)["counts"]["sell"]==1


def test_closing_only_still_cannot_sell_t_zero(monkeypatch):
    current=setup_live(monkeypatch);p.apply_quote("owner",quote(current[0]),now=current[0])
    closing=datetime(2026,11,16,7,tzinfo=timezone.utc);current[0]=closing+timedelta(seconds=30)
    p.apply_quote("owner",quote(closing,price=10.8,high=10.9,bid=10.79,ask=10.8,executable=False,closing_only=True),now=current[0])
    d=p.dashboard("owner","live")
    assert d["orders"]["total"]==1 and d["orders"]["items"][0]["side"]=="buy"
    assert d["positions"][0]["sellable_shares"]==0


@pytest.mark.parametrize("offset",[61,91,14400])
def test_closing_only_flag_does_not_authorize_old_or_after_hours_fills(monkeypatch,offset):
    current=setup_live(monkeypatch);p.apply_quote("owner",quote(current[0]),now=current[0])
    closing=datetime(2026,11,16,7,tzinfo=timezone.utc);current[0]=closing+timedelta(seconds=offset)
    result=p.apply_quote("owner",quote(closing,executable=False,closing_only=True),now=current[0])
    assert not result["accepted"] and p.dashboard("owner","live")["orders"]["total"]==1
