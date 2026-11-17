from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from server import main
from server.agent import ModelConfig, ModelFailure, generate_plan
from server.domain import evidence, validate_plan
from server.fixtures import CASES


def api(method, path, **kwargs):
    # Exercise the existing strategy API contract with a ready session. Cookie,
    # setup and session isolation are tested independently with the real gate.
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1:3001") as client:
            return await client.request(method, path, **kwargs)
    original = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.require_ready] = lambda: SimpleNamespace(session_id="engine-test", username="engine-test-owner", model_config=None)
    try:
        return asyncio.run(request())
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original)


def offline(case_id):
    response = api("POST", "/analyze", json={"case_id": case_id, "mode": "offline"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("case_id,status", [
    ("normal-fill", "completed"), ("limit-up-no-fill", "no_fill"),
    ("delayed-exit", "completed"), ("invalid-data", "blocked"),
])
def test_frozen_offline_workflow_has_no_forward_bars(case_id, status):
    case_list = api("GET", "/cases?source=examples").json()
    assert all("bars" not in case for case in case_list["cases"])
    analysis = offline(case_id)
    assert analysis["mode"] == "offline"
    assert analysis["model"] == "离线规则"
    snapshot = analysis["evidence"]
    assert all(row["date"] <= snapshot["decision_date"] for row in snapshot["bars"])
    assert "events" not in analysis
    record = {key: value for key, value in analysis.items() if key not in {"run_id", "trace", "decision_hash"}}
    expected = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert analysis["decision_hash"] == expected
    response = api("POST", "/replay", json={"run_id": analysis["run_id"]})
    assert response.status_code == 200
    assert response.json()["status"] == status
    if status == "completed":
        events = {event["type"]: event["date"] for event in response.json()["events"]}
        assert events["sell"] > events["buy"] > snapshot["decision_date"]


def test_replay_cannot_accept_a_replacement_plan_and_expires():
    analysis = offline("normal-fill")
    run_id = analysis["run_id"]
    assert api("POST", "/replay", json={"run_id": run_id, "position_pct": 100}).status_code == 422
    stored = main.runs[run_id]
    main.runs[run_id] = replace(stored, expires=0)
    assert api("POST", "/replay", json={"run_id": run_id}).status_code == 404


def test_ai_without_config_is_unavailable_not_disguised_offline(monkeypatch):
    monkeypatch.delenv("CONTEST_LLM_API_KEY", raising=False)
    assert api("GET", "/cases").json()["ai_available"] is False
    assert api("POST", "/analyze", json={"case_id": "normal-fill", "mode": "ai"}).status_code == 503


def test_browser_cross_site_post_is_blocked():
    assert api("POST", "/analyze", json={"case_id": "normal-fill"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def model_response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


@pytest.mark.parametrize("ref,ok", [("sample:metrics", True), ("sample:bar:2026-10-26", False), ("limit-up-no-fill:metrics", False)])
def test_real_protocol_uses_neutral_ids_pre_cutoff_evidence_and_server_risk(ref, ok):
    requests = []
    snapshot = evidence(CASES["limit-up-no-fill"])

    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        assert "limit-up-no-fill" not in json.dumps(body)
        assert "2026-10-26" not in json.dumps(body)
        if len(requests) == 1:
            assert body["tool_choice"]["function"]["name"] == "get_history_evidence"
            return model_response({"content": None, "tool_calls": [{"type": "function", "id": "call-1", "function": {"name": "get_history_evidence", "arguments": '{"case_id":"sample"}'}}]})
        tool = body["messages"][-1]
        assert tool["role"] == "tool" and tool["tool_call_id"] == "call-1"
        returned = json.loads(tool["content"])
        assert returned["case_id"] == "sample"
        assert all(row["date"] <= returned["decision_date"] for row in returned["bars"])
        assert body["response_format"]["type"] == "json_object"
        return model_response({"content": json.dumps({"action": "hold", "rationale": "数据不足以确认。", "evidence_ids": [ref]}, ensure_ascii=False)})

    plan, trace = asyncio.run(generate_plan(ModelConfig("https://provider.example/v1", "test-model", "test-key"), snapshot, transport=httpx.MockTransport(handle)))
    assert len(requests) == 2 and trace[0]["step"] == "tool"
    assert validate_plan(plan, snapshot)["ok"] is ok


def test_provider_cannot_request_unrelated_tools():
    def handle(_request):
        return model_response({"tool_calls": [{"type": "function", "id": "1", "function": {"name": "fetch_url", "arguments": "{}"}}]})

    with pytest.raises(ModelFailure, match="未获授权"):
        asyncio.run(generate_plan(ModelConfig("https://provider.example", "test-model", "test-key"), evidence(CASES["normal-fill"]), transport=httpx.MockTransport(handle)))


def test_invalid_model_result_is_not_silently_replaced():
    def handle(request):
        if "tools" in json.loads(request.content):
            return model_response({"tool_calls": [{"type": "function", "id": "1", "function": {"name": "get_history_evidence", "arguments": '{"case_id":"sample"}'}}]})
        return model_response({"content": "not JSON"})

    with pytest.raises(ModelFailure, match="不符合预案格式"):
        asyncio.run(generate_plan(ModelConfig("https://provider.example", "test-model", "test-key"), evidence(CASES["normal-fill"]), transport=httpx.MockTransport(handle)))
