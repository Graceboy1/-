"""Local registration and legacy migration use disposable account databases only."""
from __future__ import annotations

import hashlib
import sqlite3
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from server import access, main
from server.agent import ModelConfig


ORIGIN = "http://127.0.0.1:3001"
ROOT = "/contest-api"
OWNER = "原机主_A.1"
PASSWORD = "test-only-password-123"
GUEST = "新用户_B.2"
GUEST_PASSWORD = "test-only-guest-456"
LEGACY_SALT = b"public-test-salt"


@pytest.fixture
def world(tmp_path, monkeypatch):
    database = tmp_path / "registration.sqlite3"
    monkeypatch.setenv("CONTEST_ACCOUNT_DB", str(database))
    monkeypatch.setattr(access, "_sessions", OrderedDict())
    monkeypatch.setattr(main, "runs", OrderedDict())
    monkeypatch.setattr(main, "model_runs", 0)
    for bucket in ("_login_attempts", "_registration_attempts", "_probe_attempts"):
        monkeypatch.setattr(access, bucket, deque())
    for setting in ("CONTEST_LLM_BASE_URL", "CONTEST_LLM_MODEL", "CONTEST_LLM_API_KEY"):
        monkeypatch.delenv(setting, raising=False)
    return Starlette(routes=[Mount(ROOT, app=main.app)]), database


def client_for(app):
    return TestClient(app, base_url=ORIGIN)


def post(client, path, body=None):
    return client.post(ROOT + path, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}, json=body)


def register(client, username=OWNER, password=PASSWORD, **changes):
    return post(client, "/workspace/register", {"username": username, "password": password,
                                               "confirm_password": password, **changes})


def login(client, username=OWNER, password=PASSWORD):
    return post(client, "/workspace/login", {"username": username, "password": password})


def seed_legacy(database):
    # Public test credentials only; never open the installed account database.
    digest = access._password_hash(PASSWORD, LEGACY_SALT)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE owner (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, "
                     "salt BLOB NOT NULL, digest BLOB NOT NULL)")
        conn.execute("INSERT INTO owner VALUES(1,?,?,?)", (OWNER, LEGACY_SALT, digest))
    return digest


def test_registration_persists_independent_accounts_and_starts_not_ready(world):
    app, database = world
    with client_for(app) as owner, client_for(app) as guest:
        public = owner.get(ROOT + "/workspace/status").json()
        assert public["registration_available"] and not public["account_exists"]
        assert public["username"] is None and not public["server_model_available"]
        assert not database.exists()
        response = register(owner)
        assert response.status_code == 200, response.text
        assert response.json()["authenticated"] and not response.json()["ready"]
        assert response.json()["username"] == OWNER and not response.json()["ai_available"]
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "path=/contest-api" in response.headers["set-cookie"].lower()
        assert owner.get(ROOT + "/cases").status_code == 403
        assert post(owner, "/workspace/offline").json()["ready"]
        assert register(guest, GUEST, GUEST_PASSWORD).status_code == 200
        assert guest.get(ROOT + "/cases").status_code == 403
        assert post(guest, "/workspace/offline").status_code == 200
        assert not guest.get(ROOT + "/cases").json()["ai_available"]
        assert post(owner, "/workspace/logout").status_code == 200
        relogin = login(owner)
        assert relogin.status_code == 200 and not relogin.json()["ready"]
    with client_for(app) as reopened:
        assert login(reopened, GUEST, GUEST_PASSWORD).status_code == 200
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT username,salt,digest,is_owner FROM accounts ORDER BY username").fetchall()
        assert {row[0] for row in rows} == {OWNER, GUEST}
        assert {row[0]: row[3] for row in rows} == {OWNER: 1, GUEST: 0}
        assert all(len(row[1]) == 16 and len(row[2]) == 32 for row in rows)
        assert rows[0][1] != rows[1][1]
    assert PASSWORD.encode() not in database.read_bytes() and GUEST_PASSWORD.encode() not in database.read_bytes()


def test_legacy_migration_keeps_original_hash_name_and_research_owner(world):
    app, database = world
    original_digest = seed_legacy(database)
    with client_for(app) as owner, client_for(app) as guest:
        assert owner.get(ROOT + "/workspace/status").json()["account_exists"]
        assert login(owner).status_code == 200
        assert post(owner, "/workspace/offline").status_code == 200
        analysis = post(owner, "/analyze", {"case_id": "normal-fill"})
        assert analysis.status_code == 200, analysis.text
        frozen = analysis.json()
        record_id = frozen["research_id"]
        assert owner.get(ROOT + "/research/records").json()["total"] == 1
        assert register(guest, OWNER, "other-password-678").status_code == 409
        assert register(guest, GUEST, GUEST_PASSWORD).status_code == 200
        assert post(guest, "/workspace/offline").status_code == 200
        assert guest.get(ROOT + "/research/records").json()["total"] == 0
        assert guest.get(ROOT + "/research/records/" + record_id).status_code == 404
        assert post(guest, "/replay", {"run_id": frozen["run_id"]}).status_code == 404
        assert post(owner, "/workspace/logout").status_code == 200
        assert login(owner).status_code == 200
        assert post(owner, "/workspace/offline").status_code == 200
        detail = owner.get(ROOT + "/research/records/" + record_id)
        assert detail.status_code == 200 and detail.json()["analysis"] == frozen
        assert not detail.json()["replay_available"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT username,salt,digest FROM owner WHERE id=1").fetchone() == (OWNER, LEGACY_SALT, original_digest)
        assert conn.execute("SELECT salt,digest,is_owner FROM accounts WHERE username=?", (OWNER,)).fetchone() == (LEGACY_SALT, original_digest, 1)
    # Archive ownership still hashes the exact original username; no remapping.
    with sqlite3.connect(database.parent / "research-tests.sqlite3") as conn:
        row = conn.execute("SELECT owner FROM research_records WHERE record_id=?", (record_id,)).fetchone()
        assert row == (hashlib.sha256(("research-owner/v1:" + OWNER).encode("utf-8")).hexdigest(),)


def test_duplicate_registration_and_legacy_setup_never_reset_password(world):
    app, database = world
    original_digest = seed_legacy(database)
    with client_for(app) as client:
        assert register(client, OWNER, "replacement-password-123").status_code == 409
        assert post(client, "/workspace/setup", {"username": OWNER, "password": "replacement-password-123"}).status_code == 409
        assert post(client, "/workspace/setup", {"username": GUEST, "password": GUEST_PASSWORD}).status_code == 409
        assert login(client, OWNER, "replacement-password-123").status_code == 401
        assert login(client).status_code == 200
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT digest FROM owner").fetchone() == (original_digest,)
        assert conn.execute("SELECT digest FROM accounts WHERE username=?", (OWNER,)).fetchone() == (original_digest,)
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone() == (1,)


@pytest.mark.parametrize("changes", [
    {"password": "short", "confirm_password": "short"},
    {"confirm_password": "different-password-321"},
    {"username": "invalid name"},
    {"username": "' OR 1=1 --"},
    {"password": {"secret": PASSWORD}},
    {"is_owner": True},
])
def test_registration_validation_is_safe_and_does_not_create_accounts(world, changes):
    app, database = world
    with client_for(app) as client:
        result = register(client, **changes)
        assert result.status_code == 422, result.text
        assert PASSWORD not in result.text and "different-password-321" not in result.text
        assert "invalid name" not in result.text and "OR 1=1" not in result.text
        assert not client.get(ROOT + "/workspace/status").json()["account_exists"]
        assert not database.exists()


def test_registration_rate_limit_has_retry_after_and_cannot_reset_existing_account(world):
    app, _database = world
    with client_for(app) as client:
        assert register(client).status_code == 200
        for _ in range(4):
            assert register(client).status_code == 409
        response = register(client, GUEST, GUEST_PASSWORD)
        assert response.status_code == 429 and 1 <= int(response.headers["retry-after"]) <= 61
        assert PASSWORD not in response.text and GUEST not in response.text
        assert login(client).status_code == 200


def test_registration_rejects_cross_origin_and_never_echoes_credentials(world):
    app, database = world
    with client_for(app) as client:
        body = {"username": OWNER, "password": PASSWORD, "confirm_password": PASSWORD}
        for headers in ({"Origin": "http://127.0.0.1:3002"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.example"}):
            response = client.post(ROOT + "/workspace/register", headers=headers, json=body)
            assert response.status_code in {400, 403}
            assert PASSWORD not in response.text and OWNER not in response.text
        assert not database.exists()


def test_unknown_username_and_wrong_password_share_generic_failure_and_hash_work(world, monkeypatch):
    app, _database = world
    with client_for(app) as client:
        assert register(client).status_code == 200
        calls = []
        original = access._password_hash

        def record_hash(password, salt):
            calls.append(salt)
            return original(password, salt)

        monkeypatch.setattr(access, "_password_hash", record_hash)
        wrong = login(client, OWNER, "wrong-password-123")
        unknown = login(client, "不存在的账号", "wrong-password-123")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json() == {"detail": "账号或密码不正确。"}
        assert len(calls) == 2 and len(calls[0]) == len(calls[1]) == 16
        assert calls[1] == b"\x00" * 16


def test_migration_collision_fails_closed_without_overwriting_legacy_credentials(world):
    app, database = world
    original_digest = seed_legacy(database)
    conflicting = b"not-the-owner-hash".ljust(32, b"-")
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE accounts (username TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL)")
        conn.execute("INSERT INTO accounts VALUES(?,?,?)", (OWNER, b"conflict-salt-123", conflicting))
    with client_for(app) as client:
        result = client.get(ROOT + "/workspace/status")
        assert result.status_code == 503 and OWNER not in result.text
        assert login(client).status_code == 503
        assert register(client, GUEST, GUEST_PASSWORD).status_code == 503
        assert not access._sessions
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT digest FROM owner").fetchone() == (original_digest,)
        assert conn.execute("SELECT digest FROM accounts").fetchone() == (conflicting,)
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone() == (1,)


@pytest.mark.parametrize("legacy", [False, True])
def test_only_original_owner_can_use_server_model_and_sessions_do_not_share_keys(world, monkeypatch, legacy):
    app, database = world
    if legacy:
        seed_legacy(database)
    for setting, value in {"CONTEST_LLM_BASE_URL": "https://api.deepseek.com", "CONTEST_LLM_MODEL": "private-owner-model", "CONTEST_LLM_API_KEY": "test-only-owner-key"}.items():
        monkeypatch.setenv(setting, value)
    calls = []

    async def verify(**fields):
        calls.append(fields)
        if fields["use_server_config"]:
            return ModelConfig.from_env()
        return ModelConfig(fields["base_url"], fields["model"], fields["api_key"])

    monkeypatch.setattr(access, "verify_workspace_model", verify)
    with client_for(app) as owner, client_for(app) as guest:
        public = guest.get(ROOT + "/workspace/status")
        assert not public.json()["server_model_available"]
        assert "private-owner-model" not in public.text and "test-only-owner-key" not in public.text
        result = login(owner) if legacy else register(owner)
        assert result.status_code == 200 and result.json()["server_model_available"]
        owner_configured = post(owner, "/workspace/model", {"use_server_config": True})
        assert owner_configured.status_code == 200 and owner_configured.json()["ai_available"]
        assert register(guest, GUEST, GUEST_PASSWORD).status_code == 200
        status = guest.get(ROOT + "/workspace/status")
        assert not status.json()["server_model_available"] and not status.json()["ready"]
        assert status.json()["model"] is None and not status.json()["ai_available"]
        assert "private-owner-model" not in status.text and "test-only-owner-key" not in status.text
        forbidden = post(guest, "/workspace/model", {"use_server_config": True})
        assert forbidden.status_code == 403 and len(calls) == 1
        assert "test-only-owner-key" not in forbidden.text and "private-owner-model" not in forbidden.text
        own_model = {"base_url": "https://api.deepseek.com", "model": "guest-model", "api_key": "test-only-guest-key"}
        assert post(guest, "/workspace/model", own_model).status_code == 200
        assert guest.get(ROOT + "/workspace/status").json()["model"] == "guest-model"
        assert owner.get(ROOT + "/workspace/status").json()["model"] == "private-owner-model"
        assert post(guest, "/workspace/logout").status_code == 200
        relogin = login(guest, GUEST, GUEST_PASSWORD)
        assert not relogin.json()["server_model_available"] and relogin.json()["model"] is None
        assert not relogin.json()["ready"] and not relogin.json()["ai_available"]
        assert post(guest, "/workspace/offline").status_code == 200
    assert b"test-only-owner-key" not in database.read_bytes() and b"test-only-guest-key" not in database.read_bytes()


def test_concurrent_duplicate_registration_only_creates_one_account(world):
    app, database = world

    def attempt(_):
        with client_for(app) as client:
            return register(client).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(attempt, range(3)))
    assert sorted(statuses) == [200, 409, 409]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT username,is_owner FROM accounts").fetchall() == [(OWNER, 1)]
