import os
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Env var {name} belum di-set. Cek file .env kamu.")
    return value


def _optional_time(name: str) -> time | None:
    raw = os.getenv(name, "").strip()
    return time.fromisoformat(raw) if raw else None


# "meta" = Cloud API langsung, "kirimdev" = lewat Kirimdev (bisa coexistence dengan WhatsApp Business app).
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
if WHATSAPP_PROVIDER not in ("meta", "kirimdev"):
    raise RuntimeError(f"WHATSAPP_PROVIDER harus 'meta' atau 'kirimdev', bukan {WHATSAPP_PROVIDER!r}")
_meta = _require if WHATSAPP_PROVIDER == "meta" else lambda name: os.getenv(name, "")
_kirim = _require if WHATSAPP_PROVIDER == "kirimdev" else lambda name: os.getenv(name, "")

# --- WhatsApp Cloud API (developers.facebook.com > app kamu > WhatsApp > API Setup) ---
WA_ACCESS_TOKEN = _meta("WA_ACCESS_TOKEN")
WA_PHONE_NUMBER_ID = _meta("WA_PHONE_NUMBER_ID")
# Bebas kamu tentukan; harus sama dengan yang diisi di dashboard Meta waktu daftar webhook.
WA_VERIFY_TOKEN = _meta("WA_VERIFY_TOKEN")
# App Settings > Basic > App Secret. Dipakai buat verifikasi signature webhook.
WA_APP_SECRET = _meta("WA_APP_SECRET")

# --- Kirimdev (dashboard Kirimdev) ---
KIRIM_API_KEY = _kirim("KIRIM_API_KEY")
# ID nomor WhatsApp di Kirimdev, dipakai di URL /v1/{phone_id}/messages.
KIRIM_PHONE_ID = _kirim("KIRIM_PHONE_ID")
# Diisi otomatis sama register_kirimdev_webhook.py (initial_secret cuma dikasih sekali).
# Selama kosong, semua webhook ditolak; restart server setelah keisi.
KIRIM_WEBHOOK_SECRET = os.getenv("KIRIM_WEBHOOK_SECRET", "")
WA_GRAPH_VERSION = os.getenv("WA_GRAPH_VERSION", "v23.0")

# --- Onboarding /onboard (opsional, buat nyambungin nomor WhatsApp Business app / coexistence) ---
# App ID ada di App Settings > Basic. Config ID dari Facebook Login for Business > Configurations
# (variasi "WhatsApp Embedded Signup"). ONBOARD_KEY string acak bebas: buka /onboard?key=<ONBOARD_KEY>.
META_APP_ID = os.getenv("META_APP_ID", "")
FB_LOGIN_CONFIG_ID = os.getenv("FB_LOGIN_CONFIG_ID", "")
ONBOARD_KEY = os.getenv("ONBOARD_KEY", "")

# --- Google Calendar ---
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials/service-account.json")
# Email/ID kalender tujuan. Kalender ini harus di-share ke email service account
# dengan izin "Make changes to events".
GOOGLE_CALENDAR_ID = _require("GOOGLE_CALENDAR_ID")

# --- Aturan booking ---
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))
DEFAULT_DURATION_MINUTES = int(os.getenv("DEFAULT_DURATION_MINUTES", "60"))
# Opsional, format HH:MM. Kosongkan dua-duanya kalau gak mau batasi jam.
BUSINESS_HOUR_START = _optional_time("BUSINESS_HOUR_START")
BUSINESS_HOUR_END = _optional_time("BUSINESS_HOUR_END")

STATE_DB_PATH = os.getenv("STATE_DB_PATH", "data/state.db")
