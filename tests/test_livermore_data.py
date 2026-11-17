"""Strict local evidence tests; generated fixtures are not source market facts."""
from copy import deepcopy
import gzip
import hashlib
import json

import pytest

from server import livermore_data as ld, market_history


CODE, DECISION, EXECUTION = "000001", "2026-11-12", "2026-11-13"


def record(day, **fields):
    return {"date": day, "source_url": "https://www.sse.com.cn/public/fixture",
            "source_hash": "a" * 64, "verification_status": "source_verified", **fields}


def profile():
    return {"schema": ld.SCHEMA, "code": CODE, "records": {
        "limits": [record(DECISION, asof=f"{DECISION}T15:00:00+08:00", limit_up=11.0,
                          limit_down=9.0, security_name="测试股票", is_st=False, suspended=False)],
        "auctions": [record(EXECUTION, asof=f"{EXECUTION}T09:25:00+08:00", phase="opening_uncrossed",
                            open=11.33, auction_amount=175_000, amount_unit="yuan")],
        "index_auctions": [record(EXECUTION, asof=f"{EXECUTION}T09:25:00+08:00", phase="opening_uncrossed",
                                  symbol="000001.SH", change_pct=0.4)],
        "regions": [record(EXECUTION, available_at=f"{EXECUTION}T09:24:59+08:00",
                           regional_ok=True, note="测试专用人工确认，不是投资判断。", reviewed_by="fixture-reviewer",
                           verification_status="human_confirmed")],
    }}


def save(value=None):
    return ld.save_profile(CODE, profile() if value is None else value)


def unsafe_store(value):
    """Bypass importer only to exercise hostile/on-disk corruption readers."""
    root = ld.directory()
    root.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    (root / f"{CODE}.json.gz").write_bytes(gzip.compress(raw))
    (root / "index.json").write_text(json.dumps({"schema": ld.SCHEMA, "files": {CODE: hashlib.sha256(raw).hexdigest()}}))


def gate(**kwargs):
    return ld.entry_gate(CODE, DECISION, EXECUTION, 11.0, kwargs.pop("opening_price", 11.33), **kwargs)


def test_missing_reader_does_not_create_directory():
    assert not ld.directory().exists()
    assert ld.load_decision(CODE, DECISION)["status"] == "missing"
    result = ld.load_execution(CODE, EXECUTION)
    assert result["status"] == "missing" and len(result["missing"]) == 3
    assert not ld.directory().exists()


def test_valid_decision_keeps_false_security_flags_and_only_whitelist():
    value = profile()
    value["records"]["limits"][0]["credential"] = "never-persist-this"
    saved = save(value)
    found = ld.load_decision(CODE, DECISION)
    assert found["status"] == "verified" and found["missing"] == []
    assert found["limits"]["is_st"] is False and found["limits"]["suspended"] is False
    assert "credential" not in found["limits"]
    assert "credential" not in saved["records"]["limits"][0]
    assert len(found["source_hash"]) == 64


def test_execution_fully_verified():
    save()
    result = ld.load_execution(CODE, EXECUTION)
    assert result["status"] == "verified"
    assert result["auction"]["auction_amount"] == 175_000
    assert result["index_auction"]["change_pct"] == 0.4
    assert result["region"]["regional_ok"] is True


@pytest.mark.parametrize("day", ["2026-11-11", "2026-11-13"])
def test_decision_does_not_borrow_neighboring_day(day):
    save()
    assert ld.load_decision(CODE, day)["status"] == "missing"


@pytest.mark.parametrize("day", ["2026-11-12", "2026-11-16"])
def test_execution_does_not_borrow_neighboring_day(day):
    save()
    result = ld.load_execution(CODE, day)
    assert result["status"] == "missing" and len(result["missing"]) == 3


def test_decision_does_not_consume_invalid_future_auction():
    value = profile()
    value["records"]["auctions"][0]["auction_amount"] = "secret-invalid-future"
    unsafe_store(value)
    assert ld.load_decision(CODE, DECISION)["status"] == "verified"
    assert ld.load_execution(CODE, EXECUTION)["status"] == "invalid"


@pytest.mark.parametrize("group", ["auctions", "index_auctions", "regions"])
def test_missing_groups_report_individual_gap(group):
    value = profile()
    value["records"][group] = []
    save(value)
    result = ld.load_execution(CODE, EXECUTION)
    assert result["status"] == "missing"
    assert result["missing"] == [ld.EXECUTION_MISSING[group]]


@pytest.mark.parametrize("url", [
    "https://evil.example/source", "http://www.sse.com.cn/source", "file:///C:/data.json",
    "https://user:password@www.sse.com.cn/source", "https://www.sse.com.cn:443/source",
    "https://www.sse.com.cn.evil.example/source", "//www.sse.com.cn/source",
    "https://www.sse.com.cn/source?token=secret", "https://www.sse.com.cn/source#private",
    "https://www.sse.com.cn/\nsource",
])
def test_import_rejects_arbitrary_or_secret_source_urls(url):
    value = profile()
    value["records"]["limits"][0]["source_url"] = url
    with pytest.raises(ValueError):
        save(value)
    assert not ld.directory().exists()


@pytest.mark.parametrize("change", [
    {"limit_up": 11.001}, {"limit_up": True}, {"limit_up": float("inf")},
    {"limit_up": "11.00"}, {"limit_down": 11.0}, {"is_st": 0}, {"suspended": None},
    {"asof": "2026-11-12T15:00:00"}, {"asof": "2026-11-13T15:00:00+08:00"},
    {"source_hash": "not-a-checksum"}, {"verification_status": "guessed"}, {"code": "600000"},
])
def test_limit_record_rejects_ambiguous_or_bad_fields(change):
    value = profile()
    value["records"]["limits"][0].update(change)
    with pytest.raises(ValueError):
        save(value)


@pytest.mark.parametrize("change", [
    {"amount_unit": "wan_yuan"}, {"auction_amount": 0}, {"auction_amount": True},
    {"auction_amount": float("nan")}, {"auction_amount": "175000"},
    {"open": 11.331}, {"phase": "intraday"}, {"asof": "2026-11-13T09:30:00+08:00"},
    {"asof": "2026-11-13T09:25:01+08:00"}, {"auction_volume_shares": 12.5},
    {"auction_volume_shares": 1_000},
])
def test_auction_units_phase_amount_and_share_consistency(change):
    value = profile()
    value["records"]["auctions"][0].update(change)
    with pytest.raises(ValueError):
        save(value)


def test_total_volume_is_never_used_as_auction_amount():
    value = profile()
    row = value["records"]["auctions"][0]
    row.pop("auction_amount")
    row["volume"] = 10**12
    unsafe_store(value)
    result = ld.load_execution(CODE, EXECUTION)
    assert result["status"] == "invalid" and gate()["allowed"] is False


def test_auction_share_count_is_optional_but_exact_when_supplied():
    value = profile()
    value["records"]["auctions"][0].update(auction_volume_shares=20_000, auction_amount=226_600)
    save(value)
    assert ld.load_execution(CODE, EXECUTION)["auction"]["auction_volume_shares"] == 20_000


@pytest.mark.parametrize("change", [{"symbol": "unknown"}, {"change_pct": True}, {"change_pct": -101}])
def test_index_requires_known_benchmark_and_finite_change(change):
    value = profile()
    value["records"]["index_auctions"][0].update(change)
    with pytest.raises(ValueError):
        save(value)


@pytest.mark.parametrize("change", [
    {"available_at": "2026-11-13T09:25:01+08:00"}, {"available_at": "2026-11-13T09:24:59"},
    {"regional_ok": 1}, {"reviewed_by": ""}, {"note": ""}, {"verification_status": "source_verified"},
])
def test_region_confirmation_requires_prior_explicit_human_evidence(change):
    value = profile()
    value["records"]["regions"][0].update(change)
    with pytest.raises(ValueError):
        save(value)


def test_false_regional_confirmation_is_valid_not_missing():
    value = profile()
    value["records"]["regions"][0]["regional_ok"] = False
    save(value)
    assert ld.load_execution(CODE, EXECUTION)["region"]["regional_ok"] is False
    assert gate()["budget_yuan"] == 3500


@pytest.mark.parametrize("group", ld.GROUPS)
def test_duplicate_same_day_versions_fail_closed(group):
    value = profile()
    value["records"][group].append(deepcopy(value["records"][group][0]))
    with pytest.raises(ValueError):
        save(value)
    unsafe_store(value)
    reader = ld.load_decision if group == "limits" else ld.load_execution
    assert reader(CODE, DECISION if group == "limits" else EXECUTION)["status"] == "invalid"


def test_checksum_tampering_rejected():
    save()
    path = ld.directory() / f"{CODE}.json.gz"
    value = json.loads(gzip.decompress(path.read_bytes()))
    value["records"]["limits"][0]["limit_up"] = 12.0
    path.write_bytes(gzip.compress(json.dumps(value).encode()))
    assert ld.load_decision(CODE, DECISION)["status"] == "invalid"


def test_truncated_gzip_returns_invalid_without_raising():
    save()
    path = ld.directory() / f"{CODE}.json.gz"
    path.write_bytes(path.read_bytes()[:12])
    assert ld.load_execution(CODE, EXECUTION)["status"] == "invalid"


def test_failed_import_preserves_previous_valid_profile():
    save()
    original = (ld.directory() / f"{CODE}.json.gz").read_bytes()
    bad = profile()
    bad["records"]["limits"][0]["is_st"] = None
    with pytest.raises(ValueError):
        save(bad)
    assert (ld.directory() / f"{CODE}.json.gz").read_bytes() == original
    assert ld.load_decision(CODE, DECISION)["status"] == "verified"


@pytest.mark.parametrize("code", ["../000001", "C:/000001", "688001", "300001", "920001", 1])
def test_path_traversal_and_excluded_codes_are_invalid(code):
    assert ld.load_decision(code, DECISION)["status"] == "invalid"


@pytest.mark.parametrize("day", ["20261112", "2026-2-12", "2026-02-30", None])
def test_date_validation_is_strict(day):
    assert ld.load_execution(CODE, day)["status"] == "invalid"


def test_record_count_and_expanded_size_are_bounded(monkeypatch):
    value = profile()
    value["records"]["limits"] *= 31
    with pytest.raises(ValueError):
        save(value)
    save()
    monkeypatch.setattr(ld, "MAX_PROFILE_BYTES", 100)
    assert ld.load_decision(CODE, DECISION)["status"] == "invalid"


def test_original_full_budget_gate_does_not_trade_or_create_account_files():
    save()
    before = {p.name for p in ld.directory().iterdir()}
    result = gate()
    assert result["allowed"] is True and result["budget_yuan"] == 5000
    assert "R1" in result["reason"] and len(result["source_hash"]) == 64
    assert before == {p.name for p in ld.directory().iterdir()}


@pytest.mark.parametrize("amount,allowed", [(100_000, True), (99_999, False)])
def test_exact_five_percent_auction_liquidity_budget(amount, allowed):
    value = profile()
    value["records"]["auctions"][0]["auction_amount"] = amount
    save(value)
    result = gate()
    assert result["allowed"] is allowed
    if not allowed:
        assert "R0" in result["reason"] and "合成" not in result["reason"]


def test_both_negative_index_and_region_reject_even_with_valid_evidence():
    value = profile()
    value["records"]["index_auctions"][0]["change_pct"] = -3
    value["records"]["regions"][0]["regional_ok"] = False
    save(value)
    assert gate()["allowed"] is False and "1/3" in gate()["reason"]


@pytest.mark.parametrize("opening,reason", [(11.0, "R2"), (10.9, "R2"), (11.78, "R4"), (12.05, "R3")])
def test_original_opening_price_gates_preserved(opening, reason):
    value = profile()
    value["records"]["auctions"][0]["open"] = opening
    save(value)
    result = gate(opening_price=opening)
    assert result["allowed"] is False and reason in result["reason"]


def test_entry_price_mismatch_is_not_backfilled():
    save()
    result = gate(opening_price=11.34)
    assert result["allowed"] is False and "不回填" in result["reason"]


@pytest.mark.parametrize("change", [{"is_st": True}, {"suspended": True}, {"security_name": "*ST测试"}])
def test_historical_security_exclusions_block_entry(change):
    value = profile()
    value["records"]["limits"][0].update(change)
    save(value)
    assert gate()["allowed"] is False


def test_decision_close_must_equal_source_limit_price():
    save()
    result = ld.entry_gate(CODE, DECISION, EXECUTION, 10.99, 11.33)
    assert result["allowed"] is False and "精确涨停价" in result["reason"]


def test_execution_cannot_be_on_or_before_decision():
    save()
    assert ld.entry_gate(CODE, EXECUTION, EXECUTION, 11.0, 11.33)["allowed"] is False


@pytest.mark.parametrize("frozen", ["09:24:59", "09:25:00"])
def test_live_entry_requires_frozen_plan_and_first_thirty_seconds(frozen):
    save()
    result = gate(mode="live", frozen_at=f"{EXECUTION}T{frozen}+08:00", quote_asof=f"{EXECUTION}T09:30:01+08:00")
    assert result["allowed"] is True


@pytest.mark.parametrize("frozen,quote", [
    ("09:25:01", "09:30:00"), ("09:30:00", "09:30:01"),
    ("09:24:59", "09:30:31"), ("09:24:59", "09:29:59"),
])
def test_live_entry_never_backfills_after_auction_or_quote_window(frozen, quote):
    save()
    result = gate(mode="live", frozen_at=f"{EXECUTION}T{frozen}+08:00", quote_asof=f"{EXECUTION}T{quote}+08:00")
    assert result["allowed"] is False and "不补做" in result["reason"]


def test_live_naive_or_neighbor_date_quotes_rejected():
    save()
    assert not gate(mode="live", frozen_at=f"{EXECUTION}T09:24:59", quote_asof=f"{EXECUTION}T09:30:00+08:00")["allowed"]
    assert not gate(mode="live", frozen_at=f"{EXECUTION}T09:24:59+08:00", quote_asof="2026-11-16T09:30:00+08:00")["allowed"]


def coverage():
    result = ld.integration_summary()
    result.update(schema="livermore-coverage/v1", status="reference_only", universe_stocks=100,
                  history_120_stocks=90, low_position_stocks=10, source_limit_up_event_stocks=1,
                  samples=[{"code": CODE, "name": "测试股票", "position_120_pct": 8.0, "limit_up_confirmed": True,
                            "limit_price_verified": False, "auction_verified": False, "missing_fields": ["精确涨停价"]}])
    return result


def put_coverage(value):
    ld.directory().mkdir(parents=True, exist_ok=True)
    (ld.directory() / "coverage.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_summary_reads_only_bounded_receipt_not_price_universe(monkeypatch):
    put_coverage(coverage())
    monkeypatch.setattr(market_history, "load_profile", lambda *_: pytest.fail("must not scan prices"), raising=False)
    monkeypatch.setattr(ld, "_profile", lambda *_: pytest.fail("must not scan evidence"))
    result = ld.integration_summary()
    assert result["status"] == "reference_only" and result["universe_stocks"] == 100
    assert result["samples"][0]["limit_price_verified"] is False


@pytest.mark.parametrize("change", [
    {"universe_stocks": 6001}, {"candidate_stocks": 101}, {"verified_limit_stocks": True},
    {"decision_date": "2026-11-11"}, {"execution_date": "2026-11-16"},
    {"samples": [None]}, {"remaining": [123]},
])
def test_invalid_coverage_receipt_fails_closed_without_partial_counts(change):
    value = coverage()
    value.update(change)
    put_coverage(value)
    result = ld.integration_summary()
    assert result["status"] == "unavailable" and result["universe_stocks"] == 0

