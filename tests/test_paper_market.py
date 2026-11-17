"""Generated prices and mock HTTP only; no account, API Key or live trades."""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
import sqlite3

import httpx
import pytest

from server import historical_seats, market_history
from server import paper_market as market

NOW = datetime(2026, 11, 16, 10, 0, 30, tzinfo=market.SHANGHAI)


@pytest.fixture(autouse=True)
def no_network_or_user_finance(monkeypatch):
    market._history_cache.clear()

    async def deny(_self, _request):
        raise AssertionError("A paper-market unit test tried to use real network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)
    monkeypatch.setattr(historical_seats, "_financial_profile", lambda code: {
        "financial_reports": [], "reports": [], "business_assessment": None,
        "status": "missing", "provenance": {}, "limitations": ["Generated test: no private financial file"]})


def bars(*, future=True, count=130):
    day = date(2026, 11, 12)
    days = []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    values = [{"date": day, "open": 10., "high": 11., "low": 9., "close": 10., "volume": 100000.}
              for day in reversed(days)]
    if future:
        values.append({"date": "2026-11-13", "open": 10., "high": 11., "low": 9., "close": 10.2, "volume": 200000.})
    return values


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "generated-market"
    (root / "daily").mkdir(parents=True)
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(root))
    with sqlite3.connect(root / "index.sqlite3") as conn:
        conn.execute("CREATE TABLE stocks(code TEXT PRIMARY KEY,name TEXT,status TEXT,sha256 TEXT)")
    (root / "manifest.json").write_text(json.dumps({"schema": "market-history/v1", "end_date": "2026-11-13", "downloaded": 2}), encoding="utf-8")

    def add(code="600000", name="生成行情甲", *, values=None):
        payload = {"code": code, "source": "generated-public-test", "data_mode": "historical", "adjustment": "none",
                   "volume_unit": "shares", "downloaded_at": "2026-11-16T00:00:00+00:00", "bars": values if values is not None else bars()}
        raw = json.dumps(payload).encode()
        with gzip.open(root / "daily" / f"{code}.json.gz", "wb") as f:
            f.write(raw)
        with sqlite3.connect(root / "index.sqlite3") as conn:
            conn.execute("INSERT OR REPLACE INTO stocks VALUES(?,?,'ok',?)", (code, name, hashlib.sha256(raw).hexdigest()))
        return payload

    add("600000", "生成行情甲")
    add("000001", "生成行情乙")
    return root, add


@pytest.mark.parametrize("code,name,wanted", [
    ("600000", "正常", True), ("000001", "正常", True), ("001001", "正常", True),
    ("002001", "正常", True), ("003001", "正常", True), ("601001", "正常", True),
    ("603001", "正常", True), ("605001", "正常", True),
    ("300001", "正常", False), ("688001", "正常", False), ("920001", "正常", False),
    ("600000", "*ST样本", False), ("600000", "退市样本", False),
    ("600000", "", False), ("600000/a", "正常", False), (600000, "正常", False),
])
def test_stock_scope(code, name, wanted):
    assert market.eligible(code, name) is wanted


def test_universe_stream_and_small_live_page_are_filtered_and_fingerprinted(archive):
    _, add = archive
    add("300001", "创业测试")
    add("688001", "科创测试")
    add("600005", "ST测试")
    initial = market.universe_info()
    assert initial["total"] == 2 and initial["available"]
    assert len(initial["fingerprint"]) == 64 and initial == market.universe_info()
    assert [v["code"] for v in market.read_universe_page(limit=1)] == ["000001"]
    assert [v["code"] for v in market.read_universe_page(offset=1)] == ["600000"]
    candidates = list(market.iter_historical_candidates())
    assert [v["code"] for v in candidates] == ["000001", "600000"]
    assert all(v["decision_date"] == v["history_end"] == "2026-11-12" for v in candidates)
    assert all(v["execution_bar"]["date"] == "2026-11-13" for v in candidates)
    assert all(set(v["plans"]) == {"baseline", "livermore", "buffett"} for v in candidates)
    add("600006", "生成第三只")
    assert market.universe_info()["fingerprint"] != initial["fingerprint"]


def test_historical_selection_is_lazy_and_cancellation_stops_before_another_load(archive, monkeypatch):
    calls = []
    original = market._case_from_row
    monkeypatch.setattr(market, "_case_from_row", lambda row: (calls.append(row["code"]), original(row))[1])
    cancelled = False
    stream = market.iter_historical_candidates(cancel=lambda: cancelled)
    assert calls == []
    assert next(stream)["code"] == "000001"
    assert calls == ["000001"]
    cancelled = True
    assert list(stream) == [] and calls == ["000001"]


def test_strategies_use_real_prior_prices_but_do_not_fill_missing_seat_evidence(archive):
    item = next(market.iter_historical_candidates())
    assert item["plans"]["baseline"]["action"] == "simulate"
    assert item["plans"]["baseline"]["eligible"] is True
    assert item["plans"]["baseline"]["entry_low"] == 9.95
    for seat in ("livermore", "buffett"):
        assert item["plans"][seat]["action"] == "hold"
        assert item["plans"][seat]["missing"] and not item["plans"][seat]["eligible"]
    assert all(len(p["rule_version"]) == 64 for p in item["plans"].values())


def test_future_prices_do_not_change_any_seat_selection_or_entry_band(archive):
    _, add = archive
    before = next(market.iter_historical_candidates())
    changed = bars()
    changed[-1].update(open=100., high=120., low=90., close=110.)
    add("000001", "生成行情乙", values=changed)
    after = next(market.iter_historical_candidates())
    assert before["plans"] == after["plans"]
    assert before["execution_bar"]["open"] != after["execution_bar"]["open"]


def test_bad_history_checksum_produces_a_hold_record_not_fake_data(archive):
    root, _ = archive
    (root / "daily" / "000001.json.gz").write_bytes(b"not-gzip")
    first, second = list(market.iter_historical_candidates())
    assert first["execution_bar"] is None
    assert all(plan["action"] == "hold" and plan["missing"] for plan in first["plans"].values())
    assert second["plans"]["baseline"]["action"] == "simulate"


def test_no_future_bar_remains_missing_and_unsupported_simulation_day_rejected(archive):
    _, add = archive
    add("000001", "生成行情乙", values=bars(future=False))
    assert next(market.iter_historical_candidates())["execution_bar"] is None
    with pytest.raises(ValueError):
        list(market.iter_historical_candidates("2026-11-16"))


def test_absent_archive_is_not_replaced_with_a_demo():
    assert not market.universe_info()["available"] and market.read_universe_page() == []
    with pytest.raises(market.MarketDataError):
        list(market.iter_historical_candidates())


@pytest.mark.parametrize("offset,limit", [(-1, 10), (0, 21), (0, 0), (True, 1), (100001, 1)])
def test_live_directory_bounds(offset, limit):
    with pytest.raises(ValueError):
        market.read_universe_page(offset, limit)


def quote_body(*, code="600000", name="公开行情样本", stamp="20261116100020", **updates):
    fields = [""] * 35
    for index, value in {1: name, 2: code, 3: "10.2", 4: "10.1", 5: "10", 6: "123", 9: "10.1", 19: "10.3", 30: stamp, 33: "11", 34: "9", **{int(k): v for k, v in updates.items()}}.items():
        fields[index] = str(value)
    return f'v_{market.symbol(code)}="' + "~".join(fields) + '";'


def test_quote_fields_and_lot_units_use_source_timestamp_not_request_time():
    result = market.parse_quotes(quote_body().encode("gb18030"), ["600000"], now=NOW)
    assert len(result) == 1
    q = result[0]
    assert q["executable"] and q["name"] == "公开行情样本"
    assert q["date"] == "2026-11-16" and q["asof"].endswith("10:00:20+08:00")
    assert q["volume_shares"] == 12300 and q["bid"] == 10.1 and q["ask"] == 10.3
    assert q["limit_up"] is None and q["limit_down"] is None
    assert q["freshness_seconds"] == 10 and len(q["source_hash"]) == 64


@pytest.mark.parametrize("stamp,now,word", [
    ("20261116095929", NOW, "60秒"),
    ("20261116100031", NOW, "晚于"),
    ("20261113100020", NOW, "当前本地交易日"),
    ("20261116120000", NOW.replace(hour=12, minute=0, second=10), "非连续交易"),
    ("20261116150001", NOW.replace(hour=15, minute=0, second=10), "仅收盘观察"),
    ("20261114100020", NOW.replace(day=14), "非连续交易"),
])
def test_stale_future_wrong_day_closed_session_quotes_cannot_execute(stamp, now, word):
    q = market.parse_quotes(quote_body(stamp=stamp).encode(), ["600000"], now=now)[0]
    assert q["executable"] is False and word in q["rejection_reason"]


@pytest.mark.parametrize("updates", [{"3": "nan"}, {"6": "-1"}, {"2": "600001"}, {"1": "ST异常"}, {"33": "8"}, {"30": "not-a-date"}])
def test_invalid_or_now_st_quotes_do_not_produce_a_usable_quote(updates):
    assert market.parse_quotes(quote_body(**updates).encode(), ["600000"], now=NOW) == []


def test_missing_or_crossed_book_disables_execution_without_inventing_liquidity():
    q = market.parse_quotes(quote_body(**{"9": "0"}).encode(), ["600000"], now=NOW)[0]
    assert not q["executable"] and "买卖报价" in q["rejection_reason"]
    q = market.parse_quotes(quote_body(**{"9": "10.4"}).encode(), ["600000"], now=NOW)[0]
    assert not q["executable"]


@pytest.mark.parametrize("stamp,now", [
    ("20261116150000", NOW.replace(hour=15, minute=0, second=0)),
    ("20261116150020", NOW.replace(hour=15, minute=0, second=30)),
    ("20261116150059", NOW.replace(hour=15, minute=1, second=30)),
])
def test_closing_observation_accepts_current_closing_price_but_never_new_buys(stamp, now):
    quote = market.parse_quotes(quote_body(stamp=stamp, **{"9": "0", "19": "0"}).encode(), ["600000"], now=now)[0]
    assert quote["closing_only"] is True and quote["executable"] is False
    assert "仅收盘观察" in quote["rejection_reason"] and "不允许新增买入" in quote["rejection_reason"]
    assert quote["bid"] == 0 and quote["ask"] == 0 and quote["price"] == 10.2


@pytest.mark.parametrize("stamp,now", [
    ("20261116150059", NOW.replace(hour=15, minute=1, second=31)),
    ("20261116150000", NOW.replace(hour=19, minute=0, second=0)),
    ("20261116150000", NOW.replace(hour=15, minute=1, second=1)),
    ("20261116145959", NOW.replace(hour=15, minute=0, second=30)),
    ("20261116150031", NOW.replace(hour=15, minute=0, second=30)),
])
def test_stale_preclose_future_or_grace_expired_quotes_are_not_closing_observations(stamp, now):
    quote = market.parse_quotes(quote_body(stamp=stamp).encode(), ["600000"], now=now)[0]
    assert quote["closing_only"] is False and quote["executable"] is False


def test_closing_window_does_not_expand_the_regular_market_session():
    assert market.market_open(NOW.replace(hour=15, minute=0, second=0)) is True
    assert market.market_open(NOW.replace(hour=15, minute=0, second=1)) is False


def test_quote_http_is_fixed_bounded_and_only_parsed_never_evaluated():
    calls = []
    def reply(request):
        calls.append(request)
        assert request.url.host == "qt.gtimg.cn" and request.url.scheme == "https"
        assert "sh600000" in str(request.url) and "Authorization" not in request.headers
        return httpx.Response(200, content=quote_body().encode())
    result = asyncio.run(market.fetch_quotes(["600000"], transport=httpx.MockTransport(reply), now=NOW))
    assert len(calls) == 1 and len(result["quotes"]) == 1 and result["missing_codes"] == []
    assert result["checked_at"].endswith("10:00:30+08:00")


@pytest.mark.parametrize("status", [401, 403, 429, 302])
def test_quote_source_denial_or_redirect_is_explicit_and_never_retried(status):
    calls = []
    def reply(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid/"}, text="private-provider-body")
    with pytest.raises(market.SourceBlocked) as error:
        asyncio.run(market.fetch_quotes(["600000"], transport=httpx.MockTransport(reply), now=NOW))
    assert len(calls) == 1 and "private-provider-body" not in str(error.value)


def test_quote_freshness_is_checked_on_receipt_not_before_the_request(monkeypatch):
    moments = iter([NOW, NOW + timedelta(seconds=3)])
    monkeypatch.setattr(market, "now_local", lambda: next(moments))
    body = quote_body(stamp="20261116100032").encode()
    result = asyncio.run(market.fetch_quotes(["600000"], transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))))
    assert result["quotes"][0]["executable"] is True
    assert result["quotes"][0]["freshness_seconds"] == 1
    assert result["checked_at"].endswith("10:00:33+08:00")


def test_oversized_quotes_and_invalid_request_codes_never_execute():
    with pytest.raises(market.MarketDataError):
        asyncio.run(market.fetch_quotes(["600000"], transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 200001)), now=NOW))
    for codes in ([], ["688001"], ["600000", "600000"], ["http://untrusted.invalid"]):
        with pytest.raises(ValueError):
            asyncio.run(market.fetch_quotes(codes, now=NOW))


def sina_body(rows):
    return ("/*<script>ignored, never executed</script>*/\nvar _data=(" + json.dumps(rows) + ");").encode()


def sina_rows(count=25, end=date(2026, 11, 13)):
    rows = []
    day = end
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append({"day": day.isoformat(), "open": "10", "high": "11", "low": "9", "close": "10.1", "volume": "12345"})
        day -= timedelta(days=1)
    return list(reversed(rows))


def test_live_history_discards_today_and_future_without_multiplying_sina_share_volume():
    rows = sina_rows() + [{"day": "2026-11-16", "open": "50", "high": "60", "low": "40", "close": "55", "volume": "500000"}]
    result = market.parse_recent_history(sina_body(rows), "600000", now=NOW)
    assert len(result["bars"]) == 25 and result["decision_date"] == "2026-11-13"
    assert result["bars"][-1]["volume"] == 12345 and result["bars"][-1]["close"] == 10.1
    assert all(row["date"] < NOW.date().isoformat() for row in result["bars"])
    assert len(result["source_hash"]) == 64 and result["adjustment"] == "none"


@pytest.mark.parametrize("change", ["duplicate", "unordered", "bad-price", "bad-wrapper", "oversized", "empty"])
def test_live_history_rejects_corrupt_or_unbounded_inputs(change):
    rows = sina_rows()
    if change == "duplicate": rows.append(rows[-1])
    if change == "unordered": rows.reverse()
    if change == "bad-price": rows[-1]["volume"] = "nan"
    body = sina_body(rows)
    if change == "bad-wrapper": body = b'evil([{"day":"2026-11-13"}])'
    if change == "oversized": body = b"x" * (market.MAX_HISTORY_BYTES + 1)
    if change == "empty": body = sina_body([])
    with pytest.raises(ValueError):
        market.parse_recent_history(body, "600000", now=NOW)


def test_fetch_recent_history_uses_only_the_fixed_sina_daily_source():
    calls = []
    def reply(request):
        calls.append(request)
        assert request.url.host == "quotes.sina.cn" and request.url.params["symbol"] == "sh600000"
        assert request.url.params["scale"] == "240" and request.url.params["datalen"] == "130"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=sina_body(sina_rows()))
    result = asyncio.run(market.fetch_recent_history("600000", transport=httpx.MockTransport(reply), now=NOW))
    assert len(calls) == 1 and result["decision_date"] == "2026-11-13"


def test_source_blocked_daily_history_is_not_replaced_with_another_feed():
    calls = []
    def reply(request):
        calls.append(request)
        return httpx.Response(403, text="denied")
    with pytest.raises(market.SourceBlocked):
        asyncio.run(market.fetch_recent_history("600000", transport=httpx.MockTransport(reply), now=NOW))
    assert len(calls) == 1


def test_recent_history_cache_is_daily_and_bounded_without_background_downloads(monkeypatch):
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    async def read(_client, _url, **kwargs):
        calls.append(kwargs["params"]["symbol"])
        return sina_body(sina_rows())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(market, "_read_bounded", read)
    first = asyncio.run(market.fetch_recent_history("600000", now=NOW))
    first["bars"][0]["close"] = -100
    cached = asyncio.run(market.fetch_recent_history("600000", now=NOW))
    assert len(calls) == 1 and cached["bars"][0]["close"] > 0
    for index in range(1, 101):
        asyncio.run(market.fetch_recent_history(str(600000 + index), now=NOW))
    assert len(market._history_cache) == 100
    assert ("600000", "2026-11-16") not in market._history_cache
    asyncio.run(market.fetch_recent_history("600100", now=NOW + timedelta(days=1)))
    assert len(market._history_cache) == 100 and calls[-1] == "sh600100"


def live_parts():
    history = market.parse_recent_history(sina_body(sina_rows()), "600000", now=NOW)
    quote = market.parse_quotes(quote_body().encode(), ["600000"], now=NOW)[0]
    return history, quote


def test_live_plan_uses_only_latest_closed_prices_and_preserves_missing_seat_constraints():
    history, quote = live_parts()
    value = market.make_live_candidate("600000", "目录真名", history, quote)
    assert value["name"] == quote["name"] and value["decision_date"] == "2026-11-13"
    assert value["simulation_date"] == "2026-11-16" and value["prior_close"] == 10.1
    assert value["plans"]["baseline"]["action"] == "simulate"
    assert value["plans"]["baseline"]["entry_low"] == 10.05
    assert all(value["plans"][seat]["action"] == "hold" and value["plans"][seat]["missing"] for seat in ("livermore", "buffett"))
    changed_quote = {**quote, "price": 1000., "high": 2000., "volume_shares": 1e12}
    after = market.make_live_candidate("600000", "目录真名", history, changed_quote)
    assert value["plans"] == after["plans"] and value["source_hash"] == after["source_hash"]


@pytest.mark.parametrize("change", ["stale-history", "prevclose", "not-executable"])
def test_live_signal_never_rebrands_stale_archive_or_inconsistent_close_as_current(change):
    history, quote = live_parts()
    if change == "stale-history": history["bars"] = history["bars"][:-1]
    if change == "prevclose": quote["prev_close"] = 11.0
    if change == "not-executable": quote["executable"] = False
    result = market.make_live_candidate("600000", "目录真名", history, quote)
    assert result["status"] == "blocked_not_ready"
    assert all(plan["action"] == "hold" and plan["missing"] for plan in result["plans"].values())


def test_current_daily_ohlc_or_wrong_security_cannot_enter_a_live_plan():
    history, quote = live_parts()
    bad = deepcopy(history)
    bad["bars"][-1]["date"] = "2026-11-16"
    with pytest.raises(ValueError): market.make_live_candidate("600000", "真实名", bad, quote)
    with pytest.raises(ValueError): market.make_live_candidate("600001", "真实名", history, quote)
    with pytest.raises(ValueError): market.make_live_candidate("600000", "真实名", history, {**quote, "name": "ST新名称"})


def test_fixed_archive_live_compatibility_path_is_explicitly_not_ready(archive):
    result = next(market.build_live_candidates(today_date="2026-11-16"))
    assert result["simulation_date"] == "2026-11-16" and result["execution_bar"] is None
    assert result["status"] == "blocked_not_ready"
    assert all(plan["action"] == "hold" for plan in result["plans"].values())
