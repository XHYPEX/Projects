"""Tests for the phone/OTP/2FA account-login flow added to config_app.py.

This is the flow a non-technical end user drives from the browser to log the
forwarder bot into their own Telegram account, with no SSH access: submit a phone
number, receive a code via Telegram, submit it (and a 2FA password if the account
has one), and the bot restarts running as that account.

Since we can't hit real Telegram, `config_app.TelegramClient` is monkeypatched with
a fake that mimics Telethon's constructor (writes a `<path>.session` file, like the
real SQLiteSession does) and its send_code_request/sign_in/disconnect coroutines.
`_stop_bot_service`/`_restart_bot_service` are stubbed too (see the autouse fixture
in test_config_app.py's sibling below) since there's no real systemctl/sudo in CI.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError


class FakeLoginClient:
    """Stands in for telethon.TelegramClient during the login flow.

    correct_code/correct_password (test-controlled) decide whether sign_in succeeds;
    that's enough to drive both the happy path and the failure paths below.
    """

    def __init__(self, session_path, api_id, api_hash, *, correct_code="12345", correct_password=None):
        self.session_path = session_path
        self.correct_code = correct_code
        self.correct_password = correct_password
        # Real TelegramClient(...) opens/creates the sqlite session file immediately.
        with open(session_path + ".session", "w", encoding="utf-8") as f:
            f.write("fake-session-bytes")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="fake-phone-code-hash")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != self.correct_password:
                raise Exception("SRP password mismatch (fake)")
            return SimpleNamespace(id=999, first_name="Test")
        if code != self.correct_code:
            raise PhoneCodeInvalidError(request=None)
        if self.correct_password is not None:
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=999, first_name="Test")


@pytest.fixture(autouse=True)
def _stub_service_calls(config_app_module, monkeypatch):
    monkeypatch.setattr(config_app_module, "_stop_bot_service", lambda: (True, None))
    monkeypatch.setattr(config_app_module, "_restart_bot_service", lambda: (True, None))


def _install_fake_client(monkeypatch, config_app_module, **kwargs):
    def factory(session_path, api_id, api_hash):
        return FakeLoginClient(session_path, api_id, api_hash, **kwargs)

    monkeypatch.setattr(config_app_module, "TelegramClient", factory)


def _seed_real_session(config_app_module, content=b"old-real-session-bytes"):
    real_path = config_app_module.config.SESSION_PATH + ".session"
    with open(real_path, "wb") as f:
        f.write(content)
    return real_path


def test_start_requires_login(api_client_anon):
    res = api_client_anon.post("/api/telegram-login/start", json={"phone": "+6281234567890"})
    assert res.status_code == 401


def test_start_rejects_empty_phone(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module)
    res = api_client.post("/api/telegram-login/start", json={"phone": "   "})
    assert res.status_code == 400


def test_start_sends_code_and_returns_login_id(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module)

    res = api_client.post("/api/telegram-login/start", json={"phone": "+6281234567890"})
    assert res.status_code == 200
    body = res.json()
    assert body["awaiting"] == "code"
    assert body["login_id"]
    assert body["login_id"] in config_app_module._pending_logins


def test_happy_path_no_2fa_replaces_real_session_and_restarts(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module, correct_code="54321")
    old_real_path = _seed_real_session(config_app_module)

    login_id = api_client.post(
        "/api/telegram-login/start", json={"phone": "+6281234567890"}
    ).json()["login_id"]

    res = api_client.post(
        "/api/telegram-login/verify-code", json={"login_id": login_id, "code": "54321"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body == {"done": True, "restarted": True, "restart_error": None}

    # The real session file now holds what the fake login client "logged in" with,
    # not the old bytes -- and the pending session + login state are cleaned up.
    with open(old_real_path, "rb") as f:
        assert f.read() == b"fake-session-bytes"
    assert not os.path.exists(config_app_module._pending_session_path() + ".session")
    assert login_id not in config_app_module._pending_logins


def test_wrong_code_is_rejected_and_login_stays_pending(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module, correct_code="54321")
    _seed_real_session(config_app_module)

    login_id = api_client.post(
        "/api/telegram-login/start", json={"phone": "+6281234567890"}
    ).json()["login_id"]

    res = api_client.post(
        "/api/telegram-login/verify-code", json={"login_id": login_id, "code": "00000"}
    )
    assert res.status_code == 400
    # Still pending -- the user gets to retry the code without restarting the whole flow.
    assert login_id in config_app_module._pending_logins


def test_2fa_flow_asks_for_password_then_finalizes(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module, correct_code="54321", correct_password="s3cret")
    old_real_path = _seed_real_session(config_app_module)

    login_id = api_client.post(
        "/api/telegram-login/start", json={"phone": "+6281234567890"}
    ).json()["login_id"]

    res = api_client.post(
        "/api/telegram-login/verify-code", json={"login_id": login_id, "code": "54321"}
    )
    assert res.status_code == 200
    assert res.json() == {"login_id": login_id, "awaiting": "password"}
    assert config_app_module._pending_logins[login_id].awaiting == "password"

    res = api_client.post(
        "/api/telegram-login/verify-password", json={"login_id": login_id, "password": "wrong"}
    )
    assert res.status_code == 400
    assert login_id in config_app_module._pending_logins  # still there, can retry

    res = api_client.post(
        "/api/telegram-login/verify-password", json={"login_id": login_id, "password": "s3cret"}
    )
    assert res.status_code == 200
    assert res.json() == {"done": True, "restarted": True, "restart_error": None}
    with open(old_real_path, "rb") as f:
        assert f.read() == b"fake-session-bytes"


def test_cancel_discards_pending_login_and_restarts_old_session(api_client, config_app_module, monkeypatch):
    _install_fake_client(monkeypatch, config_app_module)
    old_real_path = _seed_real_session(config_app_module, content=b"untouched-old-session")

    login_id = api_client.post(
        "/api/telegram-login/start", json={"phone": "+6281234567890"}
    ).json()["login_id"]
    assert os.path.exists(config_app_module._pending_session_path() + ".session")

    res = api_client.post("/api/telegram-login/cancel", json={"login_id": login_id})
    assert res.status_code == 200
    assert res.json() == {"cancelled": True, "restarted": True, "restart_error": None}

    assert login_id not in config_app_module._pending_logins
    assert not os.path.exists(config_app_module._pending_session_path() + ".session")
    # The real (old) session was never touched by a cancelled login.
    with open(old_real_path, "rb") as f:
        assert f.read() == b"untouched-old-session"


def test_verify_code_with_unknown_login_id_returns_404(api_client):
    res = api_client.post(
        "/api/telegram-login/verify-code", json={"login_id": "does-not-exist", "code": "12345"}
    )
    assert res.status_code == 404
