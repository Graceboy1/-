"""Archives use only disposable DBs and dependency-injected authenticated owners."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from server import main, research_store


def request(method, path, *, owner="archive-test-owner", session="archive-test-session", **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://127.0.0.1:3001") as client:
            return await client.request(method, path, **kwargs)
    overrides = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.require_ready] = lambda: SimpleNamespace(
        session_id=session, username=owner, model_config=None)
    try:
        return asyncio.run(run())
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)


def start(**kwargs):
    response = request("POST", "/research/start", json={"news_id": "g1", "stock_id": "s1", "seat_id": "baseline"}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def freeze(draft=None, **kwargs):
    body = {"case_id": "normal-fill", "seat_id": "baseline", "mode": "offline"}
    if draft:
        body["research_id"] = draft["record_id"]
    response = request("POST", "/analyze", json=body, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def detail(record_id, **kwargs):
    response = request("GET", f"/research/records/{record_id}", **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def test_news_start_is_exact_canonical_draft_not_client_snapshot():
    draft = start()
    assert draft["status"] == "draft" and draft["case_id"] == "normal-fill"
    context = draft["context"]
    assert context["stock"]["id"] == "s1" and context["news"]["id"] == "g1"
    assert context["data_mode"] == "synthetic" and context["rule"]["case_id"] == "normal-fill"
    assert len(context["evidence_hash_at_start"]) == 64
    saved = detail(draft["record_id"])
    assert saved["analysis"] is None and saved["replay"] is None and not saved["replay_available"]
    forged = request("POST", "/research/start", json={"news_id": "g1", "stock_id": "s1", "seat_id": "baseline", "context": {"title": "forged"}})
    assert forged.status_code == 422


@pytest.mark.parametrize("body", [
    {"news_id": "g1", "stock_id": "s4", "seat_id": "baseline"},
    {"news_id": "g1", "stock_id": "s1", "seat_id": "invented"},
    {"news_id": "g1", "stock_id": "s1", "seat_id": "baseline", "sector_id": "compute"},
    {"news_id": "bad-news", "stock_id": "s1", "seat_id": "baseline"},
])
def test_start_rejects_unregistered_news_stock_seat_or_sector(body):
    assert request("POST", "/research/start", json=body).status_code == 422
    assert request("GET", "/research/records").json()["total"] == 0


def test_frozen_snapshot_and_replay_are_durable_and_owner_scoped(monkeypatch):
    draft = start()
    analysis = freeze(draft)
    record_id = draft["record_id"]
    assert analysis["research_id"] == record_id
    assert len(analysis["rule_version"]) == 64
    assert analysis["research_context"] == draft["context"]
    before = detail(record_id)
    assert before["status"] == "frozen" and before["replay_available"]
    assert before["analysis"] == analysis and before["replay"] is None
    assert all(bar["date"] <= analysis["evidence"]["decision_date"] for bar in before["analysis"]["evidence"]["bars"])
    assert "replay_bars" not in json.dumps(before)
    result = request("POST", "/replay", json={"run_id": analysis["run_id"], "include_bars": True})
    assert result.status_code == 200, result.text
    archived = detail(record_id)
    assert archived["replay"] == result.json() and archived["status"] == "replayed"

    # A fresh process has no live run cache. Disk record remains exact and read-only.
    monkeypatch.setattr(main, "runs", main.OrderedDict())
    reopened = detail(record_id, session="new-login-session")
    assert not reopened["replay_available"]
    assert reopened["analysis"] == analysis and reopened["replay"] == result.json()
    assert request("POST", "/replay", session="new-login-session", json={"run_id": analysis["run_id"]}).status_code == 404
    assert request("GET", f"/research/records/{record_id}", owner="other-owner").status_code == 404
    assert request("GET", "/research/records", owner="other-owner").json()["total"] == 0


def test_archive_store_module_reload_does_not_lose_data():
    row = research_store.create_draft("unit-owner", "normal-fill", "baseline", {"stock": {"label": "测试"}})
    # A separately imported store has no shared module-level records cache.
    spec = importlib.util.spec_from_file_location("fresh_research_store", research_store.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.get_record("unit-owner", row["record_id"])["context"] == {"stock": {"label": "测试"}}


def test_expired_run_does_not_erase_saved_record():
    analysis = freeze(start())
    main.runs[analysis["run_id"]] = replace(main.runs[analysis["run_id"]], expires=0)
    assert not detail(analysis["research_id"])["replay_available"]
    assert request("POST", "/replay", json={"run_id": analysis["run_id"]}).status_code == 404
    assert detail(analysis["research_id"])["analysis"] == analysis


def test_frozen_record_never_overwrites_and_new_study_preserves_prior_record():
    first = start()
    analysis = freeze(first)
    body = {"case_id": "normal-fill", "seat_id": "baseline", "research_id": first["record_id"]}
    assert request("POST", "/analyze", json=body).status_code == 409
    second = start()
    assert second["record_id"] != first["record_id"]
    assert detail(first["record_id"])["analysis"] == analysis
    assert detail(second["record_id"])["analysis"] is None
    wrong = request("POST", "/analyze", json={**body, "research_id": second["record_id"], "case_id": "limit-up-no-fill"})
    assert wrong.status_code == 409


def test_draft_ownership_checked_before_generation(monkeypatch):
    draft = start()
    called = []
    async def model(*args):
        called.append(args)
        raise AssertionError("not allowed to call model")
    monkeypatch.setattr(main, "generate_plan", model)
    result = request("POST", "/analyze", owner="different-owner", json={"case_id": "normal-fill", "seat_id": "baseline", "research_id": draft["record_id"], "mode": "ai"})
    assert result.status_code == 404 and not called


@pytest.mark.parametrize("mutation", ["evidence", "rules"])
def test_stale_draft_is_not_relabelled_as_current_analysis(monkeypatch, mutation):
    draft = start()
    if mutation == "evidence":
        changed = deepcopy(main.SEATS["baseline"].cases["normal-fill"])
        changed["bars"][0]["volume"] += 1
        monkeypatch.setitem(main.SEATS["baseline"].cases, "normal-fill", changed)
    else:
        monkeypatch.setattr(main, "_rules_version", lambda seat_id, **_kwargs: "0" * 64)
    response = request("POST", "/analyze", json={"case_id": "normal-fill", "seat_id": "baseline", "research_id": draft["record_id"]})
    assert response.status_code == 409
    assert detail(draft["record_id"])["status"] == "draft"


def test_pagination_is_owner_scoped_summary_only_and_filtered_by_shanghai_day(monkeypatch):
    monkeypatch.setattr(research_store, "_now", lambda: ("2026-11-13T16:10:00+00:00", "2026-11-14"))
    for _ in range(3):
        freeze(start())
    freeze(owner="other-owner", session="other-session")
    result = request("GET", "/research/records?limit=2&offset=0&date=2026-11-14&seat_id=baseline").json()
    assert result["total"] == 3 and len(result["items"]) == 2
    assert all(not {"analysis", "replay", "context"}.intersection(row) for row in result["items"])
    assert all(row["stock_label"] == "电网样本甲" and row["news_title"] for row in result["items"])
    assert all(row["has_replay"] is False for row in result["items"])
    second = request("GET", "/research/records?limit=2&offset=2&date=2026-11-14").json()
    assert len(second["items"]) == 1
    assert {row["record_id"] for row in result["items"]}.isdisjoint(row["record_id"] for row in second["items"])
    assert request("GET", "/research/records?date=2026-11-13").json()["total"] == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "offset=-1", "offset=100001", "date=2026-02-30", "date=2026-2-3", "date=2026-01-01%27%20OR%201=1--", "seat_id=unknown"])
def test_archive_query_is_bounded_and_parameterized(query):
    assert request("GET", "/research/records?" + query).status_code == 422


def test_new_study_does_not_disclose_keys_passwords_or_session_tokens(monkeypatch, tmp_path):
    analysis = freeze(start(), session="must-never-be-persisted-session")
    content = (tmp_path / "research-tests.sqlite3").read_bytes()
    assert b"must-never-be-persisted-session" not in content
    assert b"archive-test-owner" not in content
    assert b"model_config" not in content and b"api_key" not in content and b"password" not in content
    assert analysis["research_id"].encode() in content


def test_storage_failure_is_safe_and_not_false_success(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEST_RESEARCH_DB", str(tmp_path / "missing-parent" / "secret-db-name.sqlite3"))
    response = request("POST", "/analyze", json={"case_id": "normal-fill"})
    assert response.status_code == 503
    assert "secret-db-name" not in response.text and "run_id" not in response.json()


def test_first_recorded_replay_is_not_rewritten_by_later_fixture_changes(monkeypatch):
    analysis = freeze(start())
    body = {"run_id": analysis["run_id"], "include_bars": True}
    first = request("POST", "/replay", json=body)
    assert first.status_code == 200
    case = deepcopy(main.SEATS["baseline"].cases["normal-fill"])
    day = first.json()["replay_bars"][0]["date"]
    next(row for row in case["bars"] if row["date"] == day)["volume"] += 1
    monkeypatch.setitem(main.SEATS["baseline"].cases, "normal-fill", case)
    # Same fills/metrics but changed chart must not return a contradictory archive.
    assert request("POST", "/replay", json=body).status_code == 409
    assert detail(analysis["research_id"])["replay"] == first.json()


def test_chart_upgrade_allowed_but_later_nonchart_request_never_erases_saved_chart():
    analysis = freeze(start())
    body = {"run_id": analysis["run_id"]}
    original = request("POST", "/replay", json=body)
    assert original.status_code == 200 and "replay_bars" not in original.json()
    chart = request("POST", "/replay", json={**body, "include_bars": True})
    assert chart.status_code == 200 and chart.json()["replay_bars"]
    assert request("POST", "/replay", json=body).json() == original.json()
    assert detail(analysis["research_id"])["replay"] == chart.json()


def test_snapshot_size_is_bounded_and_failed_freeze_keeps_existing_records():
    with pytest.raises(research_store.ArchiveError):
        research_store.create_draft("owner", "case", "seat", {"large": "x" * research_store.MAX_CONTEXT_BYTES})
    assert request("GET", "/research/records").json()["total"] == 0


def test_archive_routes_retain_real_authentication_gate():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1:3001") as client:
            assert (await client.get("/research/records")).status_code == 401
            assert (await client.get("/research/records/" + "0" * 32)).status_code == 401
            assert (await client.post("/research/start", json={"news_id": "g1", "stock_id": "s1", "seat_id": "baseline"})).status_code == 401
    asyncio.run(run())
