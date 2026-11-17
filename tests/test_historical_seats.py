"""Real-price seat adapters with disposable archives and no provider calls."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import gzip
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from server import access, domain, historical_seats as history, main, market_history
from tools.download_market_history import connect


def real_case(*, close=10.0, future_close=120.0, count=130):
    days = []
    day = date(2026, 11, 12)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    days.reverse()
    bars = [{"date": day, "open": close, "high": close + 1., "low": close - 1., "close": close,
             "volume": 100000., "limit_up": None, "limit_down": None, "suspended": False} for day in days]
    bars.append({"date": "2026-11-13", "open": future_close, "high": future_close + 1., "low": future_close - 1.,
                 "close": future_close, "volume": 123456., "limit_up": None, "limit_down": None, "suspended": False})
    return {**market_history.sample("600000", "真实价格研究样本"), "bars": bars,
            "source": "test-public-source", "downloaded_at": "2026-11-16T00:00:00Z", "archive_sha256": "a" * 64}


@pytest.fixture(autouse=True)
def no_financial_network(monkeypatch):
    monkeypatch.setattr(history, "_financial_profile", lambda code: {
        "status": "missing", "financial_reports": [], "reports": [], "business_assessment": None,
        "provenance": {}, "limitations": ["暂无此股票的本地真实财报。"]})


def profile(*, verified=True, maintenance=20.):
    rows = [{"period_end": f"{year}-12-31", "available_on": f"{year + 1}-04-30", "operating_cashflow": 300.,
             "maintenance_capex": maintenance, "net_profit": 100., "roe_pct": 20., "debt_to_equity": .2,
             "shares_outstanding": 100., "point_in_time_verified": verified} for year in (2023, 2024, 2025)]
    return {"status": "loaded", "point_in_time_verified": verified, "financial_reports": rows,
            "reports": [{**row, "reported_capex": 30.} for row in rows],
            "business_assessment": {"available_on": "2026-10-01", "within_circle": True, "moat_supported": True,
                                    "note": "仅测试中明确提供的人工业务证据，不由下载器生成。"},
            "provenance": {"source": "generated-test-only"}, "limitations": ["测试明确提供核对证据。"]}


@pytest.mark.parametrize("seat_id", history.SEAT_IDS)
def test_each_seat_reads_the_same_real_code_name_and_pre_cutoff_prices(seat_id):
    case = real_case()
    ev = history.read_evidence(case, seat_id)
    assert ev["symbol"] == "600000" and ev["name"] == case["name"]
    assert ev["seat_id"] == seat_id and ev["data_mode"] == "historical"
    assert ev["metrics"]["data_valid"] and ev["metrics"]["last_close"] == 10.
    assert len(ev["bars"]) == 120 and ev["bars"][-1]["date"] == "2026-11-12"
    assert all(b["date"] <= "2026-11-12" and b["limit_up"] is None and b["limit_down"] is None for b in ev["bars"])
    assert ev["execution"]["status"] == "unavailable" and ev["execution"]["missing"]
    assert "DEMO" not in json.dumps(ev, ensure_ascii=False)


@pytest.mark.parametrize("seat_id", history.SEAT_IDS)
def test_future_price_and_future_missing_fields_do_not_change_decision(seat_id):
    first = real_case(future_close=100.)
    second = real_case(future_close=1000.)
    assert history.read_evidence(first, seat_id) == history.read_evidence(second, seat_id)


def test_livermore_calculates_real_120_day_position_without_inventing_a_limit_up():
    case = real_case()
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["range_position_120_pct"] == 50.
    assert ev["metrics"]["eligible"] is False and ev["metrics"]["closed_at_limit_up"] is None
    assert "涨约10%" in ev["metrics"]["candidate_reason"]
    assert history.propose(case, "livermore", ev).action == "hold"


def test_livermore_low_position_does_not_replace_unknown_limits_or_auction():
    case = real_case()
    case["bars"][-2].update(open=9.1, close=9.1, high=9.2)
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["range_position_120_pct"] == 5.
    assert ev["metrics"]["eligible"] is None
    assert ev["metrics"]["closed_at_limit_up"] is None
    assert "价格位置门槛满足" in ev["metrics"]["candidate_reason"]
    assert "竞价" in history.propose(case, "livermore", ev).rationale


def test_livermore_does_not_reduce_lookback_when_only_20_bars_are_available():
    case = real_case(count=25)
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["data_valid"]
    assert ev["metrics"]["range_position_120_pct"] is None
    assert "不足120根" in history.propose(case, "livermore", ev).rationale


def test_livermore_verified_decision_unlocks_conditional_plan_not_auction_data(monkeypatch):
    from server import livermore_data, paper_market
    case = real_case()
    case["bars"][-2].update(open=9.1, close=9.1, high=9.2)
    evidence = {"status": "verified", "limits": {"limit_up": 9.1, "limit_down": 7.45,
                "security_name": "历史证券名称", "is_st": False, "suspended": False},
                "missing": [], "source_hash": "c" * 64}
    monkeypatch.setattr(livermore_data, "load_decision", lambda code, day: deepcopy(evidence))
    monkeypatch.setattr(livermore_data, "load_execution", lambda *args: pytest.fail("Future auction entered signal"))
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["eligible"] is True and ev["metrics"]["closed_at_limit_up"] is True
    plan = paper_market._plan(case, "livermore", ev)
    assert plan["action"] == "simulate" and plan["eligible"] is True and plan["missing"] == []
    assert plan["requires_opening_auction"] is True and len(plan["livermore_evidence_hash"]) == 64
    # A decision is not an execution permission; future price mutations cannot
    # change its evidence or hash. The account's later independent gate owns it.
    changed = deepcopy(case)
    changed["bars"][-1].update(open=200, high=201, low=199, close=200)
    assert paper_market._plan(changed, "livermore") == plan


@pytest.mark.parametrize("field,value", [("is_st", True), ("suspended", True), ("security_name", "*ST历史")])
def test_livermore_historical_status_blocks_even_with_valid_low_position(monkeypatch, field, value):
    case = real_case()
    case["bars"][-2].update(open=9.1, close=9.1, high=9.2)
    limits = {"limit_up": 9.1, "limit_down": 7.45, "security_name": "历史名称", "is_st": False, "suspended": False, field: value}
    monkeypatch.setattr(history.livermore_data, "load_decision", lambda *args: {
        "status": "verified", "limits": limits, "missing": [], "source_hash": "d" * 64})
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["eligible"] is False
    assert history.propose(case, "livermore", ev).action == "hold"


def test_livermore_pool_reference_never_substitutes_exact_limit(monkeypatch):
    case = real_case()
    case["bars"][-2].update(open=9.1, close=9.1, high=9.2)
    monkeypatch.setattr(history.livermore_reference, "load_reference", lambda *args: {
        "status": "reference_match", "source_reported_limit_up": True, "reported_price": 9.1,
        "verification_status": "source_reference_only", "source_hash": "e" * 64})
    monkeypatch.setattr(history.livermore_data, "load_decision", lambda *args: {
        "status": "missing", "limits": None, "missing": ["缺少精确限价"], "source_hash": None})
    ev = history.read_evidence(case, "livermore")
    assert ev["metrics"]["eligible"] is None and ev["metrics"]["closed_at_limit_up"] is None
    assert "仅作参考" in ev["metrics"]["candidate_reason"]
    assert history.propose(case, "livermore", ev).action == "hold"


def test_buffett_missing_financials_keep_real_price_and_explain_hold():
    case = real_case()
    ev = history.read_evidence(case, "buffett")
    assert ev["metrics"]["last_close"] == 10.
    assert ev["fundamentals"]["status"] == "insufficient"
    assert ev["fundamentals"]["reports"] == [] and ev["fundamentals"]["raw_reports"] == []
    assert ev["fundamentals"]["assessment"] is None
    plan = history.propose(case, "buffett", ev)
    assert plan.action == "hold" and history.validate(case, "buffett", plan, ev)["ok"]


def test_current_retrieved_unverified_financials_are_display_only_not_historical_decision(monkeypatch):
    value = profile(verified=False)
    monkeypatch.setattr(history, "_financial_profile", lambda code: value)
    case = real_case(close=5.)
    ev = history.read_evidence(case, "buffett")
    assert len(ev["fundamentals"]["raw_reports"]) == 3
    assert ev["fundamentals"]["reports"] == []
    assert ev["fundamentals"]["data_provenance"]["source"] == "generated-test-only"
    assert history.propose(case, "buffett", ev).action == "hold"


def test_buffett_forwards_reference_coverage_without_promoting_it_to_trade_evidence(monkeypatch):
    value = profile(verified=False)
    coverage = {"expected_years": ["2023-12-31", "2024-12-31", "2025-12-31"], "reference_year_count": 3,
                "items": [{"key": "annual_reports", "label": "年度参考", "status": "reference_only",
                           "available_count": 3, "required_count": 3, "detail": "参考下载齐全，不等于原始版本已核验。"}]}
    value["data_coverage"] = coverage
    monkeypatch.setattr(history, "_financial_profile", lambda code: value)
    ev = history.read_evidence(real_case(close=5.), "buffett")
    assert ev["fundamentals"]["data_coverage"] == coverage
    assert ev["fundamentals"]["data_coverage"] is not coverage
    assert ev["fundamentals"]["reports"] == [] and len(ev["fundamentals"]["raw_reports"]) == 3
    assert not ev["fundamentals"]["eligible"]
    assert history.propose(real_case(close=5.), "buffett", ev).action == "hold"
    ev["fundamentals"]["data_coverage"]["items"][0]["available_count"] = 0
    assert coverage["items"][0]["available_count"] == 3


def test_verified_financials_reuse_original_value_rules_but_cannot_bypass_execution_gaps(monkeypatch):
    value = profile()
    monkeypatch.setattr(history, "_financial_profile", lambda code: value)
    case = real_case(close=5.)
    ev = history.read_evidence(case, "buffett")
    assert len(ev["fundamentals"]["reports"]) == 3
    assert ev["fundamentals"]["eligible"]
    plan = history.propose(case, "buffett", ev)
    assert plan.action == "simulate" and plan.position_pct == 10.
    assert "下一历史交易日" in plan.rationale
    checked = history.validate(case, "buffett", plan, ev)
    assert not checked["ok"] and checked["checks"][-1]["code"] == "execution_data"
    result = history.replay(case, "buffett", plan, checked, include_bars=True)
    assert result["status"] == "blocked" and "replay_bars" not in result
    assert not any(e["type"] == "buy" for e in result["events"])


def test_reported_total_capex_never_fills_unknown_maintenance_capex(monkeypatch):
    value = profile(maintenance=None)
    monkeypatch.setattr(history, "_financial_profile", lambda code: value)
    case = real_case()
    ev = history.read_evidence(case, "buffett")
    assert ev["fundamentals"]["raw_reports"][0]["reported_capex"] == 30.
    assert all(r["maintenance_capex"] is None for r in ev["fundamentals"]["reports"])
    assert ev["fundamentals"]["valuation"]["conservative_value"] is None
    assert history.propose(case, "buffett", ev).action == "hold"


@pytest.mark.parametrize("available", ["2026-11-12", "2026-11-13", "2027-04-30", None, "bad-date"])
def test_same_day_future_or_undated_financials_are_not_in_evidence_or_raw_details(monkeypatch, available):
    value = profile()
    for row in value["financial_reports"]:
        row["available_on"] = available
    for row in value["reports"]:
        row["available_on"] = available
    monkeypatch.setattr(history, "_financial_profile", lambda code: value)
    ev = history.read_evidence(real_case(), "buffett")
    assert ev["fundamentals"]["reports"] == [] and ev["fundamentals"]["raw_reports"] == []


def test_baseline_can_generate_real_price_preplan_but_missing_execution_blocks_order():
    case = real_case()
    ev = history.read_evidence(case, "baseline")
    plan = history.propose(case, "baseline", ev)
    assert plan.entry_low == 9.95 and plan.entry_high == 10.15
    checked = history.validate(case, "baseline", plan, ev)
    assert not checked["ok"]
    result = history.replay(case, "baseline", plan, {"ok": True, "checks": []}, include_bars=True)
    assert result["status"] == "blocked" and "replay_bars" not in result
    assert result["metrics"]["shares"] == 0


@pytest.mark.parametrize("seat_id", history.SEAT_IDS)
def test_hold_replay_observes_real_prices_only_when_explicit_and_never_opens_position(seat_id):
    case = real_case()
    ev = history.read_evidence(case, seat_id)
    if seat_id == "baseline":
        plan = domain.Plan(action="hold", rationale="测试观望，不下单。", evidence_ids=[f"{case['id']}:metrics"])
    else:
        plan = history.propose(case, seat_id, ev)
    checked = history.validate(case, seat_id, plan, ev)
    assert checked["ok"]
    hidden = history.replay(case, seat_id, plan, checked)
    revealed = history.replay(case, seat_id, plan, checked, include_bars=True)
    assert hidden["status"] == revealed["status"] == "hold" and "replay_bars" not in hidden
    assert revealed["replay_bars"][0]["date"] == "2026-11-13" and revealed["replay_bars"][0]["close"] == 120.
    assert revealed["metrics"]["shares"] == revealed["metrics"]["return_pct"] == 0
    assert all(e["type"] != "buy" for e in revealed["events"])
    assert revealed["observation_only"]


def test_invalid_hold_and_preexisting_failed_validation_cannot_reveal_future_prices():
    case = real_case()
    plan = domain.Plan(action="hold", rationale="参数不合法", evidence_ids=[f"{case['id']}:metrics"], position_pct=5.)
    checked = history.validate(case, "baseline", plan)
    assert not checked["ok"]
    result = history.replay(case, "baseline", plan, checked, include_bars=True)
    assert result["status"] == "blocked" and "replay_bars" not in result


@pytest.mark.parametrize("function", [history.read_evidence, history.propose])
def test_unknown_seats_do_not_fall_back_to_baseline(function):
    with pytest.raises(Exception) as error:
        function(real_case(), "unregistered")
    assert error.value.status_code == 404


@pytest.fixture
def archive_client(tmp_path, monkeypatch):
    directory = tmp_path / "real-market"
    directory.mkdir()
    (directory / "daily").mkdir()
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(directory))
    case = real_case()
    payload = {"code": "600000", "name": case["name"], "source": "tencent", "data_mode": "historical", "adjustment": "none",
               "volume_unit": "shares", "downloaded_at": case["downloaded_at"], "bars": case["bars"]}
    raw = json.dumps(payload).encode()
    with gzip.open(directory / "daily" / "600000.json.gz", "wb") as handle:
        handle.write(raw)
    with connect(directory) as conn:
        conn.execute("INSERT INTO stocks(code,name,market,status,first_date,last_date,bar_count,sha256) VALUES(?,?,?,?,?,?,?,?)",
                     ("600000", case["name"], "SH", "ok", case["bars"][0]["date"], "2026-11-13", len(case["bars"]), hashlib.sha256(raw).hexdigest()))
    workspace = access.WorkspaceSession("test-real-seats", "test-real-seats-user", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: workspace)
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as client:
        yield client, directory


@pytest.mark.parametrize("seat_id", history.SEAT_IDS)
def test_market_catalog_and_detail_retain_real_identity_for_requested_seat(archive_client, seat_id):
    client, _ = archive_client
    page = client.get(f"/market/stocks?seat_id={seat_id}").json()
    assert page["items"][0]["sample"]["seat_id"] == seat_id
    assert page["items"][0]["sample"]["symbol"] == "600000"
    response = client.get(f"/market/stocks/600000?seat_id={seat_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["sample"]["seat_id"] == detail["evidence"]["seat_id"] == seat_id
    assert detail["evidence"]["symbol"] == "600000" and detail["evidence"]["metrics"]["last_close"] == 10.
    assert "observed_bars" not in detail


def test_market_rejects_unregistered_seat_instead_of_silently_changing_rules(archive_client):
    client, _ = archive_client
    assert client.get("/market/stocks?seat_id=wrong").status_code == 422
    assert client.get("/market/stocks/600000?seat_id=wrong").status_code == 422


@pytest.mark.parametrize("bad_name", ["ST测试", "*st测试", "退市测试", " "])
def test_market_catalog_and_direct_lookup_both_exclude_st_delisted_or_missing_names(archive_client, bad_name):
    client, directory = archive_client
    with connect(directory) as conn:
        conn.execute("UPDATE stocks SET name=? WHERE code='600000'", (bad_name,))
    assert client.get("/market/stocks").json()["total"] == 0
    assert client.get("/market/stocks/600000?seat_id=buffett").status_code == 404
