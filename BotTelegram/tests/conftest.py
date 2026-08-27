import contextlib
import importlib
import json
import sys

import pytest

# Modules that read config/env at import time (or module scope) and must be
# re-imported fresh for every test so state (routes_by_chat_id, sent_message_map,
# ...) never leaks between tests and never touches the real .env/routes.json/session
# files used by the actual deployed bot.
_RELOAD_MODULES = ("userbot", "llm", "state", "config")


@pytest.fixture
def userbot_module(monkeypatch, tmp_path):
    """Import userbot.py fresh, isolated from real project files.

    Sets fake required env vars, points config's file paths at a tmp_path
    sandbox (routes.json, system prompt, message map, telethon session), then
    imports config -> llm -> state -> userbot in the right order so every
    module-level side effect (config.load_routes(), reading the system
    prompt, loading the message map, constructing TelegramClient/Bot) runs
    against throwaway fixtures instead of the developer's real bot state.
    """
    for name in _RELOAD_MODULES:
        sys.modules.pop(name, None)

    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    monkeypatch.setenv("TARGET_BOT_TOKEN", "123456:ABC-test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ALERT_CHAT_ID", "")
    monkeypatch.setenv("MIN_MESSAGE_LENGTH", "15")

    routes_path = tmp_path / "routes.json"
    routes_path.write_text(
        json.dumps([{"source_chat": -100111, "target_chat": -100222}]),
        encoding="utf-8",
    )
    prompt_path = tmp_path / "system_prompt.txt"
    prompt_path.write_text("Test system prompt.", encoding="utf-8")
    message_map_path = tmp_path / "sent_message_map.json"
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    config = importlib.import_module("config")
    monkeypatch.setattr(config, "ROUTES_PATH", str(routes_path))
    monkeypatch.setattr(config, "SYSTEM_PROMPT_PATH", str(prompt_path))
    monkeypatch.setattr(config, "MESSAGE_MAP_PATH", str(message_map_path))
    monkeypatch.setattr(config, "SESSION_PATH", str(session_dir / "userbot"))
    # Kalau enggak, tiap run pytest bikin logs/bot.log beneran di folder repo.
    monkeypatch.setattr(config, "LOG_PATH", str(tmp_path / "logs" / "bot.log"))

    userbot = importlib.import_module("userbot")

    yield userbot

    for name in _RELOAD_MODULES:
        sys.modules.pop(name, None)


_CONFIG_APP_RELOAD_MODULES = ("config_app", "config")


@pytest.fixture
def config_app_module(monkeypatch, tmp_path):
    """Import config_app.py fresh, isolated from real project files.

    Same rationale as userbot_module: config_app.py reads config.SYSTEM_PROMPT_PATH
    / config.ROUTES_PATH / config.PROMPT_HISTORY_DIR into module-level constants at
    import time, so config's paths must be patched to a tmp_path sandbox *before*
    config_app is imported. The real FastAPI lifespan (which logs into Telegram via
    Telethon) is replaced with a no-op so tests never touch the network.
    """
    for name in _CONFIG_APP_RELOAD_MODULES:
        sys.modules.pop(name, None)

    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    monkeypatch.setenv("TARGET_BOT_TOKEN", "123456:ABC-test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CONFIG_APP_USERNAME", "test-admin")
    monkeypatch.setenv("CONFIG_APP_PASSWORD", "test-password")
    monkeypatch.setenv("CONFIG_APP_SECRET_KEY", "test-secret-key-not-for-production")

    routes_path = tmp_path / "routes.json"
    prompt_path = tmp_path / "system_prompt.txt"
    prompt_path.write_text("Original prompt v1", encoding="utf-8")
    history_dir = tmp_path / "history"
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    config = importlib.import_module("config")
    monkeypatch.setattr(config, "ROUTES_PATH", str(routes_path))
    monkeypatch.setattr(config, "SYSTEM_PROMPT_PATH", str(prompt_path))
    monkeypatch.setattr(config, "PROMPT_HISTORY_DIR", str(history_dir))
    monkeypatch.setattr(config, "SESSION_PATH", str(session_dir / "userbot"))

    config_app = importlib.import_module("config_app")

    @contextlib.asynccontextmanager
    async def noop_lifespan(app):
        yield

    config_app.app.router.lifespan_context = noop_lifespan

    yield config_app

    for name in _CONFIG_APP_RELOAD_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def api_client_anon(config_app_module):
    """An unauthenticated client -- for testing the login/auth gate itself."""
    from fastapi.testclient import TestClient

    with TestClient(config_app_module.app) as client:
        yield client


@pytest.fixture
def api_client(api_client_anon, config_app_module):
    """A client already logged in with the fixture's CONFIG_APP_USERNAME/PASSWORD --
    what nearly every test wants, since it's exercising routes/prompt behavior, not auth."""
    res = api_client_anon.post(
        "/api/login",
        json={
            "username": config_app_module.config.CONFIG_APP_USERNAME,
            "password": config_app_module.config.CONFIG_APP_PASSWORD,
        },
    )
    assert res.status_code == 200, res.text
    return api_client_anon
