import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Env var {name} belum di-set. Cek file .env kamu.")
    return value


TELEGRAM_API_ID = int(_require("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = _require("TELEGRAM_API_HASH")

TARGET_BOT_TOKEN = _require("TARGET_BOT_TOKEN")

ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

MIN_MESSAGE_LENGTH = int(os.getenv("MIN_MESSAGE_LENGTH", "15"))

# Opsional: chat pribadi (DM ke bot, atau grup admin) buat nerima alert kalau bot crash/disconnect.
# Kosongkan kalau belum mau pakai alerting.
_alert_chat_raw = os.getenv("ALERT_CHAT_ID", "").strip()
ALERT_CHAT_ID: int | str | None = None
if _alert_chat_raw:
    try:
        ALERT_CHAT_ID = int(_alert_chat_raw)
    except ValueError:
        ALERT_CHAT_ID = _alert_chat_raw

SESSION_PATH = "session/userbot"
SYSTEM_PROMPT_PATH = "prompts/system_prompt.txt"

# Log ditulis ke file DAN tetap ke stdout (jadi `journalctl -u bottelegram` tetap jalan).
# Di-rotate biar gak makan disk VPS tanpa batas.
LOG_PATH = os.getenv("LOG_PATH", "logs/bot.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
MESSAGE_MAP_PATH = "session/sent_message_map.json"
MESSAGE_MAP_MAX_ENTRIES = 5000

# Tiap kali system_prompt.txt di-save lewat config_app.py, versi lama di-backup
# ke sini dulu supaya bisa di-lihat/di-pulihkan lagi lewat UI.
PROMPT_HISTORY_DIR = "prompts/history"
PROMPT_HISTORY_MAX_ENTRIES = 20

# Session Telethon TERPISAH khusus dipakai config_app.py, supaya gak bentrok
# (SQLite locking) sama session userbot.py kalau dua-duanya jalan bersamaan.
CONFIGAPP_SESSION_PATH = "session/configapp"

# Login (username/password) buat config_app.py -- WAJIB diisi kalau app ini di-expose
# ke internet (lewat nginx+domain), bukan cuma diakses via SSH tunnel. Kosong = config_app.py
# nolak start (lihat pengecekan di config_app.py).
CONFIG_APP_USERNAME = os.getenv("CONFIG_APP_USERNAME", "")
CONFIG_APP_PASSWORD = os.getenv("CONFIG_APP_PASSWORD", "")
# Random secret buat nanda-tangani session cookie login. Generate sekali pakai:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
CONFIG_APP_SECRET_KEY = os.getenv("CONFIG_APP_SECRET_KEY", "")

BOT_SERVICE_NAME = "bottelegram"

# Daftar pasangan grup/channel sumber -> channel/grup tujuan (lihat routes.json.example).
# Diatur paling gampang lewat config_app.py, atau edit routes.json manual.
ROUTES_PATH = "routes.json"
MAX_ROUTES = 5


@dataclass
class Route:
    source_chat: int | str
    target_chat: int | str
    sender_whitelist: set[int] = field(default_factory=set)


def _normalize_chat(value) -> int | str:
    """ID numerik (mis. -1001234567890) di-parse jadi int, username (@nama) tetap str."""
    if isinstance(value, int):
        return value
    value = str(value).strip()
    try:
        return int(value)
    except ValueError:
        return value


def load_routes() -> list[Route]:
    """Dipanggil oleh userbot.py saat startup. Sengaja tidak dijalankan otomatis saat
    import, supaya config_app.py (yang butuh baca .env tapi belum tentu punya routes.json
    di run pertama) tetap bisa jalan."""
    if not os.path.exists(ROUTES_PATH):
        raise RuntimeError(
            f"{ROUTES_PATH} belum ada. Copy dari routes.json.example lalu isi minimal 1 "
            "pasangan source/target, atau atur lewat config_app.py."
        )
    with open(ROUTES_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{ROUTES_PATH} harus berisi list minimal 1 pasangan source/target.")
    if len(raw) > MAX_ROUTES:
        raise RuntimeError(f"{ROUTES_PATH} berisi {len(raw)} pasangan, maksimum {MAX_ROUTES}.")

    routes = []
    for i, entry in enumerate(raw):
        try:
            source_chat = _normalize_chat(entry["source_chat"])
            target_chat = _normalize_chat(entry["target_chat"])
        except KeyError as e:
            raise RuntimeError(f"{ROUTES_PATH} entry #{i}: field {e} wajib diisi.")
        sender_whitelist = {int(x) for x in entry.get("sender_whitelist", [])}
        routes.append(Route(source_chat, target_chat, sender_whitelist))
    return routes
