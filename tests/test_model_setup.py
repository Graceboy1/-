from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server import model_setup
from server.agent import ModelFailure, generate_plan


def verify(base_url="https://api.deepseek.com/v1", model="test-model", api_key="test-secret", **kwargs):
    return asyncio.run(model_setup.verify_workspace_model(base_url=base_url, model=model, api_key=api_key, **kwargs))


@pytest.mark.parametrize("url", [
    "http://api.deepseek.com/v1",
    "http://localhost:9000/v1",
    "https://127.0.0.1/v1",
    "https://169.254.169.254/v1",
    "https://api.deepseek.com:444/v1",
    "https://api.deepseek.com@127.0.0.1/v1",
    "https://user:password@api.deepseek.com/v1",
    "https://api.deepseek.com.example/v1",
    "https://api.deepseek.com./v1",
    "https://api.deepseek.com/v1?override=target",
    "https://api.deepseek.com/v1#",
    "https://api.deepseek.com/v1/../other",
    "https://api.deepseek.com/v1/chat/completions",
    "https://api.deepseek.com\\@127.0.0.1/v1",
    "https://api.deepseek.\ncom/v1",
])
def test_browser_addresses_not_on_exact_allowlist_never_dial(monkeypatch, url):
    def must_not_dial(*_args, **_kwargs):
        pytest.fail("invalid URL sent to HTTP adapter")

    monkeypatch.setattr(model_setup, "generate_plan", must_not_dial)
    with pytest.raises(ModelFailure) as failure:
        verify(base_url=url)
    assert failure.value.status == 400


@pytest.mark.parametrize("ref,expected_ok", [("sample:metrics", True), ("sample:bar:2026-10-26", False)])
def test_probe_exercises_tool_and_structured_plan_then_independent_validation(monkeypatch, ref, expected_ok):
    calls = []

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert b"test-secret" not in request.content
        assert b"normal-fill" not in request.content
        assert b"2026-10-26" not in request.content
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            message = {"content": None, "tool_calls": [{
                "type": "function", "id": "probe-1", "function": {
                    "name": "get_history_evidence", "arguments": '{"case_id":"sample"}',
                },
            }]}
            assert body["tool_choice"]["function"]["name"] == "get_history_evidence"
        else:
            tool_reply = json.loads(body["messages"][-1]["content"])
            assert tool_reply["case_id"] == "sample"
            assert all(bar["date"] <= tool_reply["decision_date"] for bar in tool_reply["bars"])
            assert body["response_format"] == {"type": "json_object"}
            message = {"content": json.dumps({"action": "hold", "rationale": "探针测试。", "evidence_ids": [ref]})}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async def mocked(config, snapshot):
        return await generate_plan(config, snapshot, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(model_setup, "generate_plan", mocked)
    if expected_ok:
        config = verify("https://API.DeepSeek.com:443/v1/")
        assert config.base_url == "https://api.deepseek.com/v1" and config.model == "test-model"
        assert "test-secret" not in repr(config)
    else:
        with pytest.raises(ModelFailure, match="没有通过独立校验"):
            verify()
    assert len(calls) == 2


def test_provider_error_does_not_expose_sensitive_text(monkeypatch):
    async def unexpected(config, _snapshot):
        raise RuntimeError(f"Internal exception accidentally referenced {config.key}")

    monkeypatch.setattr(model_setup, "generate_plan", unexpected)
    with pytest.raises(ModelFailure) as failure:
        verify()
    assert failure.value.status == 502
    assert "test-secret" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_only_server_presets_may_target_trusted_local_model(monkeypatch):
    monkeypatch.setenv("CONTEST_LLM_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("CONTEST_LLM_MODEL", "server-model")
    monkeypatch.setenv("CONTEST_LLM_API_KEY", "server-secret")

    async def local_model(config, snapshot):
        assert config.base_url == "http://127.0.0.1:9000/v1"
        assert config.key == "server-secret"
        return model_setup.BASELINE.propose(snapshot), []

    monkeypatch.setattr(model_setup, "generate_plan", local_model)
    assert asyncio.run(model_setup.verify_workspace_model(use_server_config=True)).model == "server-model"
    with pytest.raises(ModelFailure, match="不要同时提交"):
        verify(use_server_config=True)
