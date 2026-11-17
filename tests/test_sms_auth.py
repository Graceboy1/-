"""Retired SMS entry points cannot load providers or change local accounts."""
from __future__ import annotations

import builtins
from collections import OrderedDict, deque

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from server import access, main


ORIGIN = "http://127.0.0.1:3001"
ROOT = "/contest-api/workspace"


@pytest.mark.parametrize("authenticated", [False, True])
def test_retired_sms_routes_return_404_without_loading_sms_modules(tmp_path, monkeypatch, authenticated):
    database = tmp_path / "sms-retired.sqlite3"
    monkeypatch.setenv("CONTEST_ACCOUNT_DB", str(database))
    monkeypatch.setattr(access, "_sessions", OrderedDict())
    for bucket in ("_login_attempts", "_registration_attempts", "_probe_attempts"):
        monkeypatch.setattr(access, bucket, deque())

    original_import = builtins.__import__

    def reject_sms_import(name, globals=None, locals=None, fromlist=(), level=0):
        sms_modules = {"sms_auth", "sms_provider"}
        if name.rsplit(".", 1)[-1] in sms_modules or any(item in sms_modules for item in (fromlist or ())):
            pytest.fail("Removed SMS routes must not load legacy SMS modules")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_sms_import)
    proxy = Starlette(routes=[Mount("/contest-api", app=main.app)])
    with TestClient(proxy, base_url=ORIGIN) as client:
        headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
        if authenticated:
            response = client.post(ROOT + "/setup", headers=headers,
                                   json={"username": "本地账号", "password": "test-retired-sms-123"})
            assert response.status_code == 200
        assert client.get(ROOT + "/sms/status").status_code == 404
        for path in ("send", "verify"):
            response = client.post(ROOT + "/sms/" + path, headers=headers,
                                   json={"phone": "13900000123", "code": "041756", "purpose": "login"})
            assert response.status_code == 404
            assert "13900000123" not in response.text and "041756" not in response.text
        assert database.exists() is authenticated

