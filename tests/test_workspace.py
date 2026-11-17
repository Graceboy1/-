from __future__ import annotations

from collections import OrderedDict, deque

from fastapi.testclient import TestClient
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from server import access, main
from server.agent import ModelConfig, ModelFailure, offline_plan


PASSWORD = "test-only-pass-123"
KEY = "test-only-api-key-456"
LOGIN = {"username": "本地测试", "password": PASSWORD}
PROVIDER = {"base_url": "https://api.deepseek.com", "model": "test-chat", "api_key": KEY}
ROOT = "/contest-api"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    database = tmp_path / "workspace.sqlite3"
    monkeypatch.setenv("CONTEST_ACCOUNT_DB", str(database))
    monkeypatch.setattr(access, "_sessions", OrderedDict())
    monkeypatch.setattr(access, "_login_attempts", deque())
    monkeypatch.setattr(access, "_registration_attempts", deque())
    monkeypatch.setattr(access, "_probe_attempts", deque())
    for setting in ("CONTEST_LLM_BASE_URL", "CONTEST_LLM_MODEL", "CONTEST_LLM_API_KEY"):
        monkeypatch.delenv(setting, raising=False)
    proxy_shape = Starlette(routes=[Mount(ROOT, app=main.app)])
    return proxy_shape, database


def make_client(app):
    return TestClient(app, base_url="http://127.0.0.1:3001")


def post(client, path, **kwargs):
    return client.post(ROOT + path, headers={"Origin": "http://127.0.0.1:3001", "Sec-Fetch-Site": "same-origin"}, **kwargs)


def test_setup_offline_login_and_run_ownership(workspace):
    app, database = workspace
    with make_client(app) as owner, make_client(app) as other:
        assert owner.get(ROOT + "/workspace/status").json()["account_exists"] is False
        assert owner.get(ROOT + "/cases").status_code == 401
        setup = post(owner, "/workspace/setup", json=LOGIN)
        assert setup.status_code == 200
        assert setup.json()["authenticated"] and not setup.json()["ready"]
        assert "httponly" in setup.headers["set-cookie"].lower()
        assert "samesite=strict" in setup.headers["set-cookie"].lower()
        assert "path=/contest-api" in setup.headers["set-cookie"].lower()
        assert PASSWORD.encode() not in database.read_bytes()
        assert post(other, "/workspace/setup", json=LOGIN).status_code == 409
        assert owner.get(ROOT + "/cases").status_code == 403
        assert post(owner, "/workspace/offline").json()["ready"]
        assert not owner.get(ROOT + "/cases").json()["ai_available"]
        run = post(owner, "/analyze", json={"case_id": "normal-fill"}).json()["run_id"]
        assert post(owner, "/replay", json={"run_id": run}).status_code == 200
        assert post(other, "/workspace/login", json={**LOGIN, "password": "not-correct-123"}).status_code == 401
        assert post(other, "/workspace/login", json=LOGIN).status_code == 200
        assert post(other, "/workspace/offline").status_code == 200
        assert post(other, "/replay", json={"run_id": run}).status_code == 404
        assert post(owner, "/workspace/logout").json()["authenticated"] is False
        assert owner.get(ROOT + "/cases").status_code == 401


def test_model_key_stays_in_session_and_only_verified_model_enables_ai(workspace, monkeypatch):
    app, database = workspace

    async def verify(**fields):
        assert fields["api_key"] == KEY and fields["model"] == PROVIDER["model"]
        return ModelConfig(PROVIDER["base_url"], PROVIDER["model"], KEY)

    async def reject(**_fields):
        raise ModelFailure("模型未通过验证。", 502)

    async def analyze(config, snapshot):
        assert config.key == KEY
        return offline_plan(snapshot), [{"step": "model", "title": "测试替身", "detail": "没有实际联网"}]

    with make_client(app) as client:
        assert post(client, "/workspace/model", json=PROVIDER).status_code == 401
        assert post(client, "/workspace/setup", json=LOGIN).status_code == 200
        monkeypatch.setattr(access, "verify_workspace_model", reject)
        assert post(client, "/workspace/model", json=PROVIDER).status_code == 502
        assert not client.get(ROOT + "/workspace/status").json()["ready"]
        monkeypatch.setattr(access, "verify_workspace_model", verify)
        result = post(client, "/workspace/model", json=PROVIDER)
        assert result.status_code == 200
        assert result.json()["ready"] and result.json()["ai_available"] and KEY not in result.text
        assert KEY.encode() not in database.read_bytes()
        assert client.get(ROOT + "/cases").json()["model"] == PROVIDER["model"]
        monkeypatch.setattr(main, "generate_plan", analyze)
        monkeypatch.setattr(main, "model_runs", 0)
        result = post(client, "/analyze", json={"case_id": "normal-fill", "mode": "ai"})
        assert result.status_code == 200 and result.json()["mode"] == "ai" and KEY not in result.text
        assert post(client, "/workspace/logout").status_code == 200
        assert not post(client, "/workspace/login", json=LOGIN).json()["ai_available"]


def test_reject_foreign_origin_and_do_not_echo_invalid_credentials(workspace):
    app, _database = workspace
    with make_client(app) as client:
        for headers in ({"Origin": "http://127.0.0.1:3002"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.example"}):
            assert client.post(ROOT + "/workspace/setup", headers=headers, json=LOGIN).status_code in {400, 403}
        invalid = post(client, "/workspace/setup", json={**LOGIN, "password": {"secret": PASSWORD}})
        assert invalid.status_code == 422 and PASSWORD not in invalid.text
        assert not client.get(ROOT + "/workspace/status").json()["account_exists"]
