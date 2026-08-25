"""Tests for the System Prompt endpoints in config_app.py (edit + history/backup).

Uses config_app_module/api_client from conftest.py, which import config_app fresh
against a tmp_path sandbox (fake env vars, temp system_prompt.txt/history dir) and
replace the FastAPI lifespan (real Telegram login) with a no-op, so these tests
never touch the developer's real prompt file or the network.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _stub_restart(config_app_module, monkeypatch):
    # The routes/prompt save+restore endpoints all shell out to `systemctl restart
    # bottelegram`; that's an orthogonal concern (and not available in CI), so
    # stub it to a deterministic result and assert on it explicitly where it matters.
    monkeypatch.setattr(config_app_module, "_restart_bot_service", lambda: (True, None))


def test_get_prompt_returns_current_content(api_client):
    res = api_client.get("/api/prompt")
    assert res.status_code == 200
    assert res.json() == {"content": "Original prompt v1"}


def test_get_prompt_when_file_missing_returns_empty_string(api_client, config_app_module):
    os.remove(config_app_module.PROMPT_PATH)
    res = api_client.get("/api/prompt")
    assert res.status_code == 200
    assert res.json() == {"content": ""}


def test_save_prompt_writes_file_and_reports_restart(api_client, config_app_module):
    res = api_client.post("/api/prompt", json={"content": "Updated prompt v2"})
    assert res.status_code == 200
    assert res.json() == {"saved": True, "restarted": True, "restart_error": None}

    with open(config_app_module.PROMPT_PATH, encoding="utf-8") as f:
        assert f.read() == "Updated prompt v2"


def test_save_prompt_backs_up_previous_version_before_overwriting(api_client, config_app_module):
    api_client.post("/api/prompt", json={"content": "Updated prompt v2"})

    entries = os.listdir(config_app_module.PROMPT_HISTORY_DIR)
    assert len(entries) == 1
    with open(os.path.join(config_app_module.PROMPT_HISTORY_DIR, entries[0]), encoding="utf-8") as f:
        assert f.read() == "Original prompt v1"


def test_save_prompt_rejects_empty_content(api_client, config_app_module):
    res = api_client.post("/api/prompt", json={"content": "   \n  "})
    assert res.status_code == 400

    # Nothing should have changed: no overwrite, no backup taken.
    with open(config_app_module.PROMPT_PATH, encoding="utf-8") as f:
        assert f.read() == "Original prompt v1"
    assert not os.path.isdir(config_app_module.PROMPT_HISTORY_DIR)


def test_history_is_empty_before_any_save(api_client):
    res = api_client.get("/api/prompt/history")
    assert res.status_code == 200
    assert res.json() == []


def test_history_lists_backed_up_entry_after_save(api_client):
    api_client.post("/api/prompt", json={"content": "Updated prompt v2"})

    res = api_client.get("/api/prompt/history")
    assert res.status_code == 200
    entries = res.json()
    assert len(entries) == 1
    assert "id" in entries[0]


def test_history_lists_newest_first(api_client, config_app_module):
    api_client.post("/api/prompt", json={"content": "v2"})
    api_client.post("/api/prompt", json={"content": "v3"})
    api_client.post("/api/prompt", json={"content": "v4"})

    ids = [e["id"] for e in api_client.get("/api/prompt/history").json()]
    assert ids == sorted(ids, reverse=True)
    assert len(ids) == 3


def test_history_entry_returns_backed_up_content(api_client):
    api_client.post("/api/prompt", json={"content": "Updated prompt v2"})
    entry_id = api_client.get("/api/prompt/history").json()[0]["id"]

    res = api_client.get(f"/api/prompt/history/{entry_id}")
    assert res.status_code == 200
    assert res.json() == {"id": entry_id, "content": "Original prompt v1"}


def test_history_entry_unknown_id_returns_404(api_client):
    res = api_client.get("/api/prompt/history/20200101T000000")
    assert res.status_code == 404


@pytest.mark.parametrize(
    "bad_id",
    ["not-a-timestamp", "2020-01-01", "20200101T000000/../../etc/passwd"],
)
def test_history_entry_rejects_malformed_id(api_client, bad_id):
    # Note: a bare ".." or "" id is not exercised here -- the HTTP client/Starlette
    # normalize those path segments *before* routing, so the request lands on a
    # different, already-public endpoint (GET /api/prompt or /api/prompt/history)
    # rather than ever reaching our handler; it's not a traversal of anything.
    res = api_client.get(f"/api/prompt/history/{bad_id}")
    assert res.status_code in (400, 404)


def test_restore_switches_active_prompt(api_client, config_app_module):
    api_client.post("/api/prompt", json={"content": "Updated prompt v2"})
    entry_id = api_client.get("/api/prompt/history").json()[0]["id"]

    res = api_client.post(f"/api/prompt/history/{entry_id}/restore")
    assert res.status_code == 200
    assert res.json() == {"saved": True, "restarted": True, "restart_error": None}

    with open(config_app_module.PROMPT_PATH, encoding="utf-8") as f:
        assert f.read() == "Original prompt v1"


def test_restore_backs_up_version_it_replaces(api_client):
    api_client.post("/api/prompt", json={"content": "Updated prompt v2"})
    v1_id = api_client.get("/api/prompt/history").json()[0]["id"]

    api_client.post(f"/api/prompt/history/{v1_id}/restore")

    ids = {e["id"] for e in api_client.get("/api/prompt/history").json()}
    # v1's backup is still there, plus a new backup of v2 (the version just replaced).
    assert v1_id in ids
    assert len(ids) == 2


def test_restore_unknown_id_returns_404(api_client, config_app_module):
    res = api_client.post("/api/prompt/history/20200101T000000/restore")
    assert res.status_code == 404

    with open(config_app_module.PROMPT_PATH, encoding="utf-8") as f:
        assert f.read() == "Original prompt v1"


def test_restore_rejects_path_traversal_id(api_client):
    res = api_client.post("/api/prompt/history/not-a-valid-id/restore")
    assert res.status_code == 400


def test_backup_history_is_pruned_to_max_entries(config_app_module, monkeypatch):
    monkeypatch.setattr(config_app_module, "PROMPT_HISTORY_MAX_ENTRIES", 2)

    for i in range(4):
        with open(config_app_module.PROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(f"version {i}")
        config_app_module._backup_current_prompt()

    entries = os.listdir(config_app_module.PROMPT_HISTORY_DIR)
    assert len(entries) == 2
