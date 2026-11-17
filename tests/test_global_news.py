"""Isolated news-reader checks: generated feeds, temporary SQLite, no network."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from xml.sax.saxutils import escape

from fastapi.testclient import TestClient
import httpx
import pytest

from server import access, global_news as news, main


NOW = datetime(2026, 11, 16, 8, 0, 0, tzinfo=timezone.utc)
SOURCE = news.Source("fixture-rss", "测试全球新闻源", "https://news.example.test/feed.xml", ("news.example.test",))
SINA = news.Source("fixture-sina", "测试全球财经", "https://finance.example.test/feed", ("finance.example.test",), kind="sina")


@pytest.fixture(autouse=True)
def news_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_ACCOUNT_DB", str(tmp_path / "unused-accounts.sqlite3"))
    monkeypatch.setenv("CONTEST_NEWS_DB", str(tmp_path / "news.sqlite3"))
    monkeypatch.setenv("CONTEST_NEWS_AUTO_REFRESH", "0")
    monkeypatch.setattr(news, "utcnow", lambda: NOW)
    monkeypatch.setattr(news, "SOURCES", (SOURCE,))
    monkeypatch.setattr(news, "_refresh_task", None)
    monkeypatch.setattr(news, "_scheduler_task", None)
    monkeypatch.setattr(news, "_last_trigger", 0.0)
    # Most tests do not need market data; never inspect the user's real index.
    monkeypatch.setattr(news.news_relations, "catalog", lambda: {"stocks": [], "stock_links": []})

    async def deny_network(_self, _request):
        raise AssertionError("A news unit test tried to use real HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)


@pytest.fixture
def client(monkeypatch):
    workspace = access.WorkspaceSession("news-fixture-only", "news-fixture-user", ready=True)
    monkeypatch.setitem(main.app.dependency_overrides, access.require_ready, lambda: workspace)
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as value:
        yield value


def rss_item(*, title="日本原油新闻", url="https://news.example.test/article/1", date=None, summary="短摘要"):
    date = date if date is not None else format_datetime(NOW - timedelta(minutes=1))
    return f"<item><title>{escape(title)}</title><link>{escape(url)}</link><description>{escape(summary)}</description><pubDate>{escape(date)}</pubDate></item>"


def rss(*items):
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>" + "".join(items) + "</channel></rss>").encode()


def article(index=0, *, title=None, published_at=None, source=SOURCE):
    url = f"https://{source.hosts[0]}/article/{index}"
    return {"id": "live-" + hashlib.sha256(url.encode()).hexdigest()[:24], "source_id": source.id,
            "url": url, "title": title or f"新闻{index}", "summary": "仅供观察的短摘要",
            "published_at": published_at or news.stamp(NOW - timedelta(minutes=1, seconds=index)),
            "geo": None, "location_basis": "没有明确地名，不猜测。"}


def test_rss_strips_html_and_keeps_only_bounded_publisher_text():
    payload = rss(rss_item(title="<b>日本原油</b>", summary="<script>do-not-display</script><b>需求</b> &amp; 供应 " + "量" * 200))
    result = news.parse_feed(payload, SOURCE, now=NOW)
    assert len(result) == 1
    assert result[0]["title"] == "日本原油"
    assert "do-not-display" not in result[0]["summary"] and "<" not in result[0]["summary"]
    assert result[0]["summary"].startswith("需求 & 供应") and len(result[0]["summary"]) == 160
    assert result[0]["published_at"] == "2026-11-16T07:59:00+00:00"
    assert result[0]["source_id"] == SOURCE.id and result[0]["id"].startswith("live-")


def test_atom_and_foreign_excerpt_support():
    source = news.Source("english", "Foreign publisher", SOURCE.url, SOURCE.hosts, "en")
    content = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>Japan oil update</title><link href="https://news.example.test/atom"/>
    <updated>2026-11-16T15:55:00+08:00</updated><summary>{'word ' * 100}</summary>
    </entry></feed>""".encode()
    result = news.parse_feed(content, source, now=NOW)
    assert len(result) == 1 and result[0]["url"].endswith("/atom")
    assert result[0]["published_at"] == "2026-11-16T07:55:00+00:00"
    assert len(result[0]["summary"].split()) <= 24


@pytest.mark.parametrize("value,expected", [
    ("Mon, 16 Nov 2026 15:30:00 +0800", "2026-11-16T07:30:00+00:00"),
    ("2026-11-16T07:30:00Z", "2026-11-16T07:30:00+00:00"),
    ("2026-11-09T08:00:00+00:00", "2026-11-09T08:00:00+00:00"),
    ("2026-11-16T08:20:00Z", None),
    ("2026-11-09T07:59:59Z", None),
    ("2026-11-16T07:30:00", None),
    ("2026-11-16", None),
    ("not a date", None),
    (None, None),
])
def test_publication_requires_an_explicit_timezone_and_recent_nonfuture_date(value, expected):
    assert news.published(value, NOW) == expected


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "file:///secret", "https://news.example.test.evil.test/a",
    "https://evilnews.example.test/a", "https://user:password@news.example.test/a",
    "https://news.example.test:8443/a", "//news.example.test/a", "/relative",
])
def test_links_cannot_escape_the_source_allowlist(value):
    assert news.article_url(value, SOURCE) is None


def test_duplicate_tracking_urls_share_one_stable_identity():
    rows = news.parse_feed(rss(
        rss_item(url="http://news.example.test/article/1?a=1&utm_source=x&fbclid=tracking#part"),
        rss_item(url="https://news.example.test/article/1?a=1&utm_medium=y"),
    ), SOURCE, now=NOW)
    assert len(rows) == 1 and rows[0]["url"] == "https://news.example.test/article/1?a=1"
    same = news.parse_feed(rss(rss_item(url=rows[0]["url"])), SOURCE, now=NOW)
    assert same[0]["id"] == rows[0]["id"]


@pytest.mark.parametrize("content", [
    b"<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///private'>]><rss><channel/></rss>",
    b"<!ENTITY xxe 'secret'><rss><channel/></rss>",
    b"<html><body>Not a feed</body></html>",
])
def test_nonfeeds_and_external_entity_declarations_are_rejected(content):
    with pytest.raises(ValueError):
        news.parse_feed(content, SOURCE, now=NOW)


def test_feed_size_and_item_limits_are_bounded():
    with pytest.raises(ValueError):
        news.parse_feed(b"x" * (news.MAX_FEED_BYTES + 1), SOURCE, now=NOW)
    rows = news.parse_feed(rss(*(rss_item(url=f"https://news.example.test/{i}") for i in range(105))), SOURCE, now=NOW)
    assert len(rows) == news.MAX_ITEMS_PER_SOURCE == 100
    with pytest.raises(ValueError):
        news.parse_feed(rss(*(rss_item() for _ in range(1001))), SOURCE, now=NOW)


def test_sina_uses_article_ctime_never_the_response_timestamp():
    good_time = int((NOW - timedelta(minutes=10)).timestamp())
    rows = [{"title": "日本原油更新", "url": "https://finance.example.test/world/1?utm_source=x", "ctime": str(good_time), "intro": "<b>短摘要</b>"},
            {"title": "没有发布时间", "url": "https://finance.example.test/world/2"},
            {"title": "过期", "url": "https://finance.example.test/world/3", "ctime": str(good_time - 8 * 86400)},
            {"title": "未来", "url": "https://finance.example.test/world/4", "ctime": str(good_time + 3600)},
            {"title": "重复", "url": "https://finance.example.test/world/1?utm_medium=y", "ctime": str(good_time)}]
    content = json.dumps({"result": {"status": {"code": 0}, "timestamp": "2099-01-01T00:00:00Z", "data": rows}}).encode()
    parsed = news.parse_sina(content, SINA, now=NOW)
    assert len(parsed) == 1
    assert parsed[0]["published_at"] == "2026-11-16T07:50:00+00:00"
    assert parsed[0]["url"] == "https://finance.example.test/world/1" and parsed[0]["summary"] == "短摘要"


@pytest.mark.parametrize("payload", [None, [], {"result": None}, {"result": {"status": None, "data": []}}, {}, {"result": {"status": {"code": 1}, "data": []}}, {"result": {"status": {"code": 0}, "data": {}}}])
def test_invalid_sina_responses_are_not_treated_as_empty_success(payload):
    with pytest.raises(ValueError):
        news.parse_sina(json.dumps(payload).encode(), SINA, now=NOW)


def test_sina_ignores_malformed_individual_rows_without_inventing_fields():
    good = {"title": "有效新闻", "url": "https://finance.example.test/valid", "ctime": str(int(NOW.timestamp()))}
    payload = {"result": {"status": {"code": 0}, "data": [None, 5, "text", {}, {**good, "ctime": "invalid"}, good]}}
    result = news.parse_sina(json.dumps(payload).encode(), SINA, now=NOW)
    assert len(result) == 1 and result[0]["title"] == "有效新闻" and result[0]["summary"] == ""


def test_sina_response_size_and_item_count_are_bounded():
    with pytest.raises(ValueError):
        news.parse_sina(b"x" * (news.MAX_FEED_BYTES + 1), SINA, now=NOW)
    payload = {"result": {"status": {"code": 0}, "data": [{} for _ in range(1001)]}}
    with pytest.raises(ValueError):
        news.parse_sina(json.dumps(payload).encode(), SINA, now=NOW)


def test_empty_cache_never_contains_synthetic_fallback():
    result = news.feed()
    assert result["news"] == [] and result["meta"]["status"] == "empty"
    assert result["news_mode"] == "live" and result["preview_only"] is False
    assert result["model_called"] is False and result["meta"]["total"] == 0
    assert result["stocks"] == [] and result["stock_links"] == []


def test_refresh_streams_only_an_allowlisted_feed_and_saves_provenance():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=rss(rss_item()), headers={"ETag": '"fixture-v1"', "Last-Modified": "Mon, 16 Nov 2026 07:55:00 GMT"})

    result = asyncio.run(news.refresh_once(transport=httpx.MockTransport(respond)))
    assert calls == [SOURCE.url] and result["status"] == "live"
    payload = news.feed()
    assert len(payload["news"]) == 1 and payload["news"][0]["synthetic"] is False
    assert payload["news"][0]["source"] == SOURCE.label
    assert all(link["verified"] is False and link["direction"] == "unknown" for link in payload["news_links"])
    assert news.source_rows()[SOURCE.id]["etag"] == '"fixture-v1"'


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_denial_stops_that_source_without_retry(status):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(status, text="Do not copy private response bodies")

    transport = httpx.MockTransport(respond)
    asyncio.run(news.refresh_once(transport=transport))
    asyncio.run(news.refresh_once(transport=transport))
    assert calls == [SOURCE.url]
    source = news.source_rows()[SOURCE.id]
    assert source["status"] == "blocked" and source["blocked"] == 1
    assert "Do not copy" not in source["error"] and news.cache_meta()["partial"] is True


def test_conditional_304_keeps_saved_articles_without_parsing_a_body(monkeypatch):
    news.save_result(SOURCE, articles=[article()], etag='"v1"', modified="Mon, 16 Nov 2026 07:55:00 GMT")

    def forbidden_parser(*_args, **_kwargs):
        raise AssertionError("A 304 response must not be parsed as an article")

    monkeypatch.setattr(news, "parse_feed", forbidden_parser)

    def respond(request):
        assert request.headers["If-None-Match"] == '"v1"'
        assert request.headers["If-Modified-Since"] == "Mon, 16 Nov 2026 07:55:00 GMT"
        return httpx.Response(304)

    meta = asyncio.run(news.refresh_once(transport=httpx.MockTransport(respond)))
    assert meta["status"] == "live" and news.feed()["meta"]["total"] == 1


@pytest.mark.parametrize("failure", ["timeout", "network", "invalid", "server", "redirect", "oversize"])
def test_failed_update_retains_old_cache_and_reports_stale(monkeypatch, failure):
    old = NOW - timedelta(minutes=30)
    monkeypatch.setattr(news, "utcnow", lambda: old)
    news.save_result(SOURCE, articles=[article()])
    monkeypatch.setattr(news, "utcnow", lambda: NOW)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail", request=request)
        if failure == "network":
            raise httpx.ConnectError("private transport detail", request=request)
        if failure == "invalid":
            return httpx.Response(200, content=b"not a feed")
        if failure == "server":
            return httpx.Response(500, text="private body")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://unknown.example.test/private"})
        return httpx.Response(200, content=b"x" * (news.MAX_FEED_BYTES + 1))

    meta = asyncio.run(news.refresh_once(transport=httpx.MockTransport(respond)))
    assert calls == [SOURCE.url]
    assert meta["status"] == "stale" and meta["partial"] is True
    assert meta["updated_at"] == news.stamp(old) and meta["checked_at"] == news.stamp(NOW)
    assert news.feed()["meta"]["total"] == 1
    assert "private" not in meta["sources"][0]["error"]


def test_seven_day_and_5000_article_bounds_apply_on_disk():
    records = [article(i) for i in range(5005)]
    records.append(article(99999, published_at=news.stamp(NOW - timedelta(days=8))))
    news.save_result(SOURCE, articles=records)
    with closing(news.connect()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        oldest = conn.execute("SELECT MIN(published_at) FROM articles").fetchone()[0]
    assert count == news.MAX_ROWS == 5000
    assert oldest >= news.stamp(NOW - timedelta(days=7))
    assert news.feed(limit=40)["meta"]["total"] == 5000
    assert news.feed(limit=40)["meta"]["has_more"] is True


def test_pagination_and_literal_search_are_bounded_and_ordered():
    rows = [article(0, title="日本原油100%"), article(1, title="芯片_需求"), article(2, title="路径\\新闻"), article(3)]
    news.save_result(SOURCE, articles=rows)
    first, second = news.feed(limit=2), news.feed(limit=2, offset=2)
    assert len(first["news"]) == len(second["news"]) == 2
    assert first["meta"]["total"] == 4 and first["meta"]["has_more"] is True
    assert second["meta"]["has_more"] is False
    assert {item["id"] for item in first["news"]}.isdisjoint(item["id"] for item in second["news"])
    for query, expected in [("%", "日本原油100%"), ("_", "芯片_需求"), ("\\", "路径\\新闻")]:
        result = news.feed(q=query)
        assert result["meta"]["total"] == 1 and result["news"][0]["title"] == expected


def test_candidates_require_local_mainboard_nonst_names_and_never_a_seat_pass(tmp_path, monkeypatch):
    database = tmp_path / "market-index.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE stocks(code TEXT,name TEXT,status TEXT)")
        conn.executemany("INSERT INTO stocks VALUES(?,?,'ok')", [("600000", "本地名称"), ("600001", "*ST测试"), ("600002", "退市测试"), ("300001", "创业测试"), ("688001", "科创测试")])

    def market_connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    codes = ["600000", "600001", "600002", "600003", "300001", "688001", "920001"]
    monkeypatch.setattr(news.market_history, "connection", market_connection)
    monkeypatch.setattr(news.news_relations, "catalog", lambda: {
        "stocks": [{"code": code, "id": "a" + code, "label": "不可信缓存名称", "rule_results": {"baseline": {"eligible": True}}} for code in codes],
        "stock_links": [{"stock_id": "a" + code, "sector_id": "energy"} for code in codes],
    })
    result = news.candidate_catalog()
    assert len(result["stocks"]) == 1 and result["stocks"][0]["code"] == "600000"
    assert result["stocks"][0]["label"] == "本地名称" and result["stocks"][0]["rule_results"] == {}
    assert result["stocks"][0]["synthetic"] is False
    assert result["stock_links"] == [{"stock_id": "a600000", "sector_id": "energy"}]


def test_api_news_routes_require_login():
    with TestClient(main.app, base_url="http://127.0.0.1:3001") as value:
        assert value.get("/news/feed").status_code == 401
        assert value.get("/news/status").status_code == 401
        assert value.post("/news/refresh").status_code == 401


def test_api_empty_cache_bounds_and_no_fake_news(client):
    response = client.get("/news/feed")
    assert response.status_code == 200 and response.json()["news"] == []
    assert response.json()["news_mode"] == "live"
    assert client.get("/news/status").json()["status"] == "empty"
    for params in ({"limit": 0}, {"limit": 41}, {"offset": -1}, {"offset": 10001}, {"q": "x" * 101}):
        assert client.get("/news/feed", params=params).status_code == 422


def test_api_unreadable_cache_returns_explicit_failure_not_synthetic_data(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_NEWS_DB", str(tmp_path / "missing-parent" / "news.sqlite3"))
    assert client.get("/news/feed").status_code == 503
    assert client.get("/news/status").status_code == 503


def test_api_refresh_only_requests_the_shared_background_task(client, monkeypatch):
    calls = []
    monkeypatch.setattr(news, "begin_refresh", lambda: calls.append(True) or {"accepted": True, "status": "updating", "retry_after": 2})
    response = client.post("/news/refresh")
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert calls == [True]


def test_repeated_refresh_shares_one_task_then_obeys_cooldown(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(news, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exercise():
        gate, calls = asyncio.Event(), []

        async def fake_refresh():
            calls.append(True)
            await gate.wait()
            return {"status": "live"}

        monkeypatch.setattr(news, "refresh_once", fake_refresh)
        try:
            first = news.begin_refresh()
            task = news._refresh_task
            assert first["accepted"] is True
            assert news.begin_refresh()["status"] == "updating" and news._refresh_task is task
            await asyncio.sleep(0)
            assert calls == [True]
            gate.set()
            await task
            cooldown = news.begin_refresh()
            assert cooldown["accepted"] is False and cooldown["status"] == "cooldown"
            assert 0 < cooldown["retry_after"] <= news.REFRESH_SECONDS
            clock[0] += news.REFRESH_SECONDS + 1
            assert news.begin_refresh()["accepted"] is True
            await news._refresh_task
            assert calls == [True, True]
        finally:
            await news.stop()

    asyncio.run(exercise())


def test_scheduler_is_singleton_and_stop_cancels_all_pending_tasks(monkeypatch):
    monkeypatch.setenv("CONTEST_NEWS_AUTO_REFRESH", "1")

    async def exercise():
        gate, started = asyncio.Event(), asyncio.Event()

        async def fake_refresh():
            started.set()
            await gate.wait()
            return {}

        monkeypatch.setattr(news, "refresh_once", fake_refresh)
        await news.start()
        scheduler = news._scheduler_task
        await news.start()
        assert news._scheduler_task is scheduler
        await asyncio.wait_for(started.wait(), timeout=1)
        refresh = news._refresh_task
        await news.stop()
        assert scheduler.cancelled() and refresh.cancelled()
        assert news._scheduler_task is None and news._refresh_task is None and news._last_trigger == 0

    asyncio.run(exercise())


def test_scheduler_can_be_disabled_without_any_network_task():
    async def exercise():
        await news.start()
        assert news._scheduler_task is None and news._refresh_task is None
        await news.stop()

    asyncio.run(exercise())
