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
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

import config

ROUTES_PATH = config.ROUTES_PATH
SENDER_FETCH_LIMIT = 300

PROMPT_PATH = config.SYSTEM_PROMPT_PATH
PROMPT_HISTORY_DIR = config.PROMPT_HISTORY_DIR
PROMPT_HISTORY_MAX_ENTRIES = config.PROMPT_HISTORY_MAX_ENTRIES
_HISTORY_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(-[0-9]+)?$")

if not (config.CONFIG_APP_USERNAME and config.CONFIG_APP_PASSWORD and config.CONFIG_APP_SECRET_KEY):
    raise RuntimeError(
        "CONFIG_APP_USERNAME, CONFIG_APP_PASSWORD, dan CONFIG_APP_SECRET_KEY wajib diisi di .env "
        "sebelum config_app.py dijalankan -- app ini gak boleh punya akses tanpa login, "
        "apalagi kalau di-expose ke internet lewat nginx/domain. Generate secret key dengan:\n"
        '  python3 -c "import secrets; print(secrets.token_hex(32))"'
    )

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
app.add_middleware(SessionMiddleware, secret_key=config.CONFIG_APP_SECRET_KEY, same_site="lax")

_failed_logins: dict[str, list[float]] = {}
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 300


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_RATE_WINDOW_SECONDS]
    _failed_logins[ip] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT


def _record_failed_login(ip: str) -> None:
    _failed_logins.setdefault(ip, []).append(time.time())


def require_login(request: Request) -> bool:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Belum login.")
    return True


class LoginPayload(BaseModel):
    username: str
    password: str


@app.get("/login")
async def login_page():
    return FileResponse("webapp/login.html")


@app.post("/api/login")
async def api_login(payload: LoginPayload, request: Request):
    ip = _client_ip(request)
    if _login_rate_limited(ip):
        raise HTTPException(
            status_code=429, detail="Terlalu banyak percobaan gagal. Coba lagi beberapa menit lagi."
        )

    valid = hmac.compare_digest(payload.username, config.CONFIG_APP_USERNAME) and hmac.compare_digest(
        payload.password, config.CONFIG_APP_PASSWORD
    )
    if not valid:
        _record_failed_login(ip)
        raise HTTPException(status_code=401, detail="Username/password salah.")

    request.session["authenticated"] = True
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


class RoutePayload(BaseModel):
    source_chat: int
    target_chat: int
    sender_whitelist: list[int] = []


class ConfigPayload(BaseModel):
    routes: list[RoutePayload]


class PromptPayload(BaseModel):
    content: str


def _run_systemctl(*args: str) -> tuple[bool, str | None]:
    """Jalankan systemctl lewat sudo -- butuh sudoers rule NOPASSWD khusus buat user
    yang jalanin config_app.py (lihat deploy/setup.sh), soalnya restart/stop service
    butuh privilege root yang gak dipunyai user biasa/system user."""
    try:
        subprocess.run(["sudo", "systemctl", *args], check=True, timeout=15)
        return True, None
    except FileNotFoundError:
        return False, "sudo/systemctl tidak ada di sistem ini (bukan Linux/VPS) -- kelola service manual."
    except Exception as e:
        return False, f"Gagal jalankan 'systemctl {' '.join(args)}' ({e}) -- kelola service manual."


def _restart_bot_service() -> tuple[bool, str | None]:
    """Restart layanan bot supaya userbot.py (proses terpisah) baca ulang
    routes.json / prompts/system_prompt.txt yang baru saja di-save."""
    return _run_systemctl("restart", config.BOT_SERVICE_NAME)


def _stop_bot_service() -> tuple[bool, str | None]:
    """Stop bot sebelum ganti akun Telegram, supaya session/userbot.session gak
    lagi dipegang proses lain waktu kita timpa dengan session akun baru."""
    return _run_systemctl("stop", config.BOT_SERVICE_NAME)


@app.get("/")
async def index(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login")
    return FileResponse("webapp/index.html")


@app.get("/api/dialogs")
async def api_dialogs(_: bool = Depends(require_login)):
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
async def api_senders(chat_id: int, _: bool = Depends(require_login)):
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
async def api_get_config(_: bool = Depends(require_login)):
    if not os.path.exists(ROUTES_PATH):
        return {"routes": []}
    with open(ROUTES_PATH, "r", encoding="utf-8") as f:
        routes = json.load(f)
    return {"routes": routes}


@app.post("/api/config")
async def api_save_config(payload: ConfigPayload, _: bool = Depends(require_login)):
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
async def api_get_prompt(_: bool = Depends(require_login)):
    return {"content": _read_prompt()}


@app.post("/api/prompt")
async def api_save_prompt(payload: PromptPayload, _: bool = Depends(require_login)):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Prompt tidak boleh kosong.")

    _backup_current_prompt()
    with open(PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(payload.content)

    restarted, restart_error = _restart_bot_service()
    return {"saved": True, "restarted": restarted, "restart_error": restart_error}


@app.get("/api/prompt/history")
async def api_prompt_history(_: bool = Depends(require_login)):
    if not os.path.isdir(PROMPT_HISTORY_DIR):
        return []
    entries = sorted(
        (f[: -len(".txt")] for f in os.listdir(PROMPT_HISTORY_DIR) if f.endswith(".txt")),
        reverse=True,
    )
    return [{"id": entry_id} for entry_id in entries]


@app.get("/api/prompt/history/{entry_id}")
async def api_prompt_history_entry(entry_id: str, _: bool = Depends(require_login)):
    path = _history_path(entry_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Versi history tidak ditemukan.")
    with open(path, "r", encoding="utf-8") as f:
        return {"id": entry_id, "content": f.read()}


@app.post("/api/prompt/history/{entry_id}/restore")
async def api_restore_prompt(entry_id: str, _: bool = Depends(require_login)):
    path = _history_path(entry_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Versi history tidak ditemukan.")

    _backup_current_prompt()
    shutil.copyfile(path, PROMPT_PATH)

    restarted, restart_error = _restart_bot_service()
    return {"saved": True, "restarted": restarted, "restart_error": restart_error}


# --- Login akun Telegram (ganti nomor HP userbot.py) lewat browser, tanpa perlu SSH ---
#
# Alurnya: stop bottelegram.service dulu (biar session/userbot.session bebas dipegang),
# lalu login pakai TelegramClient TERPISAH ke file session sementara (*.pending.session).
# Begitu login sukses (kode + password 2FA kalau ada), file session sementara itu
# menggantikan session/userbot.session yang asli, baru service di-restart.
#
# State login yang lagi berjalan (client Telethon yang masih connect, nunggu kode/password)
# disimpan di memori proses ini, di-key pakai login_id acak -- BUKAN di session cookie,
# soalnya objek TelegramClient gak bisa diserialize ke cookie.

PENDING_LOGIN_TTL_SECONDS = 600


@dataclass
class _PendingLogin:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    created_at: float
    awaiting: str  # "code" atau "password"


_pending_logins: dict[str, _PendingLogin] = {}


def _pending_session_path() -> str:
    return config.SESSION_PATH + ".pending"


def _remove_pending_session_files() -> None:
    base = _pending_session_path() + ".session"
    for path in (base, base + "-journal", base + "-wal", base + "-shm"):
        if os.path.exists(path):
            os.remove(path)


async def _discard_pending(login_id: str) -> None:
    pending = _pending_logins.pop(login_id, None)
    if pending is not None:
        try:
            await pending.client.disconnect()
        except Exception:
            pass
    _remove_pending_session_files()


async def _sweep_expired_logins() -> None:
    now = time.time()
    expired = [lid for lid, p in _pending_logins.items() if now - p.created_at > PENDING_LOGIN_TTL_SECONDS]
    for lid in expired:
        await _discard_pending(lid)


class PhoneLoginStartPayload(BaseModel):
    phone: str


class PhoneLoginCodePayload(BaseModel):
    login_id: str
    code: str


class PhoneLoginPasswordPayload(BaseModel):
    login_id: str
    password: str


class PhoneLoginCancelPayload(BaseModel):
    login_id: str


async def _finalize_login(login_id: str) -> dict:
    pending = _pending_logins.pop(login_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Sesi login tidak ditemukan atau sudah kedaluwarsa.")

    await pending.client.disconnect()

    pending_file = _pending_session_path() + ".session"
    real_file = config.SESSION_PATH + ".session"
    os.replace(pending_file, real_file)
    for ext in ("-journal", "-wal", "-shm"):
        stray = _pending_session_path() + ".session" + ext
        if os.path.exists(stray):
            os.remove(stray)

    restarted, restart_error = _restart_bot_service()
    return {"done": True, "restarted": restarted, "restart_error": restart_error}


@app.post("/api/telegram-login/start")
async def api_telegram_login_start(payload: PhoneLoginStartPayload, _: bool = Depends(require_login)):
    await _sweep_expired_logins()

    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Nomor HP wajib diisi (format internasional, mis. +6281234567890).")

    _stop_bot_service()
    _remove_pending_session_files()

    login_client = TelegramClient(_pending_session_path(), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    try:
        await login_client.connect()
        sent = await login_client.send_code_request(phone)
    except Exception as e:
        await login_client.disconnect()
        _remove_pending_session_files()
        raise HTTPException(status_code=400, detail=f"Gagal kirim kode OTP: {e}")

    login_id = secrets.token_urlsafe(24)
    _pending_logins[login_id] = _PendingLogin(
        client=login_client,
        phone=phone,
        phone_code_hash=sent.phone_code_hash,
        created_at=time.time(),
        awaiting="code",
    )
    return {"login_id": login_id, "awaiting": "code"}


@app.post("/api/telegram-login/verify-code")
async def api_telegram_login_verify_code(payload: PhoneLoginCodePayload, _: bool = Depends(require_login)):
    await _sweep_expired_logins()
    pending = _pending_logins.get(payload.login_id)
    if pending is None or pending.awaiting != "code":
        raise HTTPException(status_code=404, detail="Sesi login tidak ditemukan atau sudah kedaluwarsa. Mulai ulang.")

    try:
        await pending.client.sign_in(
            phone=pending.phone, code=payload.code.strip(), phone_code_hash=pending.phone_code_hash
        )
    except SessionPasswordNeededError:
        pending.awaiting = "password"
        return {"login_id": payload.login_id, "awaiting": "password"}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        raise HTTPException(status_code=400, detail=f"Kode salah atau kedaluwarsa: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal verifikasi kode: {e}")

    return await _finalize_login(payload.login_id)


@app.post("/api/telegram-login/verify-password")
async def api_telegram_login_verify_password(payload: PhoneLoginPasswordPayload, _: bool = Depends(require_login)):
    pending = _pending_logins.get(payload.login_id)
    if pending is None or pending.awaiting != "password":
        raise HTTPException(status_code=404, detail="Sesi login tidak ditemukan atau sudah kedaluwarsa. Mulai ulang.")

    try:
        await pending.client.sign_in(password=payload.password)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Password 2FA salah: {e}")

    return await _finalize_login(payload.login_id)


@app.post("/api/telegram-login/cancel")
async def api_telegram_login_cancel(payload: PhoneLoginCancelPayload, _: bool = Depends(require_login)):
    await _discard_pending(payload.login_id)
    # Bot di-stop waktu /start dipanggil -- nyalain lagi pakai session lama yang belum disentuh.
    restarted, restart_error = _restart_bot_service()
    return {"cancelled": True, "restarted": restarted, "restart_error": restart_error}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
