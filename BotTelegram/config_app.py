"""
App lokal buat konfigurasi BotTelegram: pilih channel/grup sumber, channel/grup
tujuan, dan filter sender di grup sumber -- tanpa perlu edit .env manual.

Pakai session Telethon TERPISAH dari userbot.py (session/configapp), jadi aman
dijalankan bersamaan dengan bot yang lagi live. Run pertama kali bakal minta
login interaktif (nomor HP + OTP) di terminal ini.

Jalankan: python3 config_app.py
Lalu buka: http://127.0.0.1:8000
(Kalau di VPS, akses lewat SSH tunnel: ssh -L 8000:localhost:8000 user@vps)
"""

import datetime
import json
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telethon import TelegramClient

import config

ROUTES_PATH = config.ROUTES_PATH
SENDER_FETCH_LIMIT = 300

PROMPT_PATH = config.SYSTEM_PROMPT_PATH
PROMPT_HISTORY_DIR = config.PROMPT_HISTORY_DIR
PROMPT_HISTORY_MAX_ENTRIES = config.PROMPT_HISTORY_MAX_ENTRIES
_HISTORY_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(-[0-9]+)?$")

telethon_client: TelegramClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telethon_client
    telethon_client = TelegramClient(
        config.CONFIGAPP_SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )
    await telethon_client.start()
    yield
    await telethon_client.disconnect()


app = FastAPI(lifespan=lifespan)


class RoutePayload(BaseModel):
    source_chat: int
    target_chat: int
    sender_whitelist: list[int] = []


class ConfigPayload(BaseModel):
    routes: list[RoutePayload]


class PromptPayload(BaseModel):
    content: str


def _restart_bot_service() -> tuple[bool, str | None]:
    """Restart layanan bot supaya userbot.py (proses terpisah) baca ulang
    routes.json / prompts/system_prompt.txt yang baru saja di-save."""
    try:
        subprocess.run(["systemctl", "restart", "bottelegram"], check=True, timeout=15)
        return True, None
    except FileNotFoundError:
        return False, "systemctl tidak ada di sistem ini (bukan Linux/VPS) -- restart bot manual."
    except Exception as e:
        return False, f"Gagal auto-restart service ({e}) -- restart bot manual."


@app.get("/")
async def index():
    return FileResponse("webapp/index.html")


@app.get("/api/dialogs")
async def api_dialogs():
    dialogs = []
    async for d in telethon_client.iter_dialogs():
        if d.is_user:
            continue  # cuma mau grup/channel, bukan DM personal
        dialogs.append(
            {
                "id": d.id,
                "name": d.name,
                "username": getattr(d.entity, "username", None),
                "type": type(d.entity).__name__,
            }
        )
    return dialogs


@app.get("/api/senders")
async def api_senders(chat_id: int):
    try:
        entity = await telethon_client.get_entity(chat_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Chat gak ditemukan: {e}")

    counts: dict[int, int] = {}
    infos: dict[int, dict] = {}

    async for msg in telethon_client.iter_messages(entity, limit=SENDER_FETCH_LIMIT):
        if msg.sender_id is None:
            continue
        counts[msg.sender_id] = counts.get(msg.sender_id, 0) + 1
        if msg.sender_id not in infos:
            sender = await msg.get_sender()
            if sender is None:
                continue
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            full_name = (first + " " + last).strip() or getattr(sender, "title", "") or str(msg.sender_id)
            infos[msg.sender_id] = {
                "name": full_name,
                "username": getattr(sender, "username", None),
            }

    result = []
    for sender_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        info = infos.get(sender_id, {"name": str(sender_id), "username": None})
        result.append(
            {
                "id": sender_id,
                "name": info["name"],
                "username": info["username"],
                "message_count": count,
            }
        )
    return result


@app.get("/api/config")
async def api_get_config():
    if not os.path.exists(ROUTES_PATH):
        return {"routes": []}
    with open(ROUTES_PATH, "r", encoding="utf-8") as f:
        routes = json.load(f)
    return {"routes": routes}


@app.post("/api/config")
async def api_save_config(payload: ConfigPayload):
    if not payload.routes:
        raise HTTPException(status_code=400, detail="Minimal harus ada 1 pasangan source/target.")
    if len(payload.routes) > config.MAX_ROUTES:
        raise HTTPException(
            status_code=400,
            detail=f"Maksimum {config.MAX_ROUTES} pasangan source/target.",
        )
    source_ids = [r.source_chat for r in payload.routes]
    if len(set(source_ids)) != len(source_ids):
        raise HTTPException(
            status_code=400,
            detail="Tiap pasangan harus punya grup sumber yang beda -- ada duplikat.",
        )

    routes = [
        {
            "source_chat": r.source_chat,
            "target_chat": r.target_chat,
            "sender_whitelist": r.sender_whitelist,
        }
        for r in payload.routes
    ]
    with open(ROUTES_PATH, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)

    restarted, restart_error = _restart_bot_service()
    return {"saved": True, "restarted": restarted, "restart_error": restart_error}


def _read_prompt() -> str:
    if not os.path.exists(PROMPT_PATH):
        return ""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _history_path(entry_id: str) -> str:
    if not _HISTORY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="ID history tidak valid.")
    return os.path.join(PROMPT_HISTORY_DIR, f"{entry_id}.txt")


def _backup_current_prompt() -> None:
    """Simpan isi prompt yang lagi aktif ke prompts/history/ sebelum ditimpa,
    supaya versi lama gak hilang dan bisa dipulihkan lagi lewat UI."""
    if not os.path.exists(PROMPT_PATH):
        return
    os.makedirs(PROMPT_HISTORY_DIR, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    entry_id = stamp
    n = 1
    while os.path.exists(os.path.join(PROMPT_HISTORY_DIR, f"{entry_id}.txt")):
        entry_id = f"{stamp}-{n}"
        n += 1
    shutil.copyfile(PROMPT_PATH, os.path.join(PROMPT_HISTORY_DIR, f"{entry_id}.txt"))

    # Prune entri terlama kalau kelebihan, supaya folder history gak membengkak.
    entries = sorted(f for f in os.listdir(PROMPT_HISTORY_DIR) if f.endswith(".txt"))
    excess = len(entries) - PROMPT_HISTORY_MAX_ENTRIES
    for name in entries[:excess]:
        os.remove(os.path.join(PROMPT_HISTORY_DIR, name))


@app.get("/api/prompt")
async def api_get_prompt():
    return {"content": _read_prompt()}


@app.post("/api/prompt")
async def api_save_prompt(payload: PromptPayload):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Prompt tidak boleh kosong.")

    _backup_current_prompt()
    with open(PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(payload.content)

    restarted, restart_error = _restart_bot_service()
    return {"saved": True, "restarted": restarted, "restart_error": restart_error}


@app.get("/api/prompt/history")
async def api_prompt_history():
    if not os.path.isdir(PROMPT_HISTORY_DIR):
        return []
    entries = sorted(
        (f[: -len(".txt")] for f in os.listdir(PROMPT_HISTORY_DIR) if f.endswith(".txt")),
        reverse=True,
    )
    return [{"id": entry_id} for entry_id in entries]


@app.get("/api/prompt/history/{entry_id}")
async def api_prompt_history_entry(entry_id: str):
    path = _history_path(entry_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Versi history tidak ditemukan.")
    with open(path, "r", encoding="utf-8") as f:
        return {"id": entry_id, "content": f.read()}


@app.post("/api/prompt/history/{entry_id}/restore")
async def api_restore_prompt(entry_id: str):
    path = _history_path(entry_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Versi history tidak ditemukan.")

    _backup_current_prompt()
    shutil.copyfile(path, PROMPT_PATH)

    restarted, restart_error = _restart_bot_service()
    return {"saved": True, "restarted": restarted, "restart_error": restart_error}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
