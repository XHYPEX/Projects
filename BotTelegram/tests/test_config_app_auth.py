"""Tests for config_app.py's login/session auth gate.

config_app.py is meant to be exposed publicly (behind nginx+TLS) so a real user
can reach it without SSH access, so every route except /login and /api/login must
require an authenticated session. Uses config_app_module/api_client_anon from
conftest.py -- a config_app import isolated from real project files, with
CONFIG_APP_USERNAME/PASSWORD set to test-admin/test-password (see conftest.py).
"""

import pytest


def test_index_redirects_to_login_when_unauthenticated(api_client_anon):
    res = api_client_anon.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/login"


def test_protected_api_returns_401_when_unauthenticated(api_client_anon):
    res = api_client_anon.get("/api/prompt")
    assert res.status_code == 401


def test_login_with_correct_credentials_grants_access(api_client_anon, config_app_module):
    res = api_client_anon.post(
        "/api/login",
        json={
            "username": config_app_module.config.CONFIG_APP_USERNAME,
            "password": config_app_module.config.CONFIG_APP_PASSWORD,
        },
    )
    assert res.status_code == 200

    res = api_client_anon.get("/api/prompt")
    assert res.status_code == 200

    res = api_client_anon.get("/", follow_redirects=False)
    assert res.status_code == 200


def test_login_with_wrong_password_is_rejected(api_client_anon, config_app_module):
    res = api_client_anon.post(
        "/api/login",
        json={"username": config_app_module.config.CONFIG_APP_USERNAME, "password": "not-the-password"},
    )
    assert res.status_code == 401

    res = api_client_anon.get("/api/prompt")
    assert res.status_code == 401


def test_login_with_wrong_username_is_rejected(api_client_anon, config_app_module):
    res = api_client_anon.post(
        "/api/login",
        json={"username": "not-the-admin", "password": config_app_module.config.CONFIG_APP_PASSWORD},
    )
    assert res.status_code == 401


def test_repeated_failed_logins_are_rate_limited(api_client_anon):
    for _ in range(5):
        res = api_client_anon.post("/api/login", json={"username": "x", "password": "wrong"})
        assert res.status_code == 401

    res = api_client_anon.post("/api/login", json={"username": "x", "password": "wrong"})
    assert res.status_code == 429


def test_logout_revokes_access(api_client):
    # api_client fixture starts out logged in.
    assert api_client.get("/api/prompt").status_code == 200

    res = api_client.post("/api/logout")
    assert res.status_code == 200

    assert api_client.get("/api/prompt").status_code == 401


def test_config_app_refuses_to_start_without_credentials(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    monkeypatch.setenv("TARGET_BOT_TOKEN", "123456:ABC-test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CONFIG_APP_USERNAME", "")
    monkeypatch.setenv("CONFIG_APP_PASSWORD", "")
    monkeypatch.setenv("CONFIG_APP_SECRET_KEY", "")

    for name in ("config_app", "config"):
        sys.modules.pop(name, None)

    with pytest.raises(RuntimeError, match="CONFIG_APP_USERNAME"):
        importlib.import_module("config_app")

    for name in ("config_app", "config"):
        sys.modules.pop(name, None)
