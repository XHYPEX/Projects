import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Env var {name} belum di-set. Cek file .env kamu.")
    return value


def _parse_chat(name: str) -> int | str:
    """ID numerik (mis. -1001234567890) di-parse jadi int, username (@nama) tetap str."""
    value = _require(name)
    try:
        return int(value)
    except ValueError:
        return value


TELEGRAM_API_ID = int(_require("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = _require("TELEGRAM_API_HASH")
SOURCE_CHAT = _parse_chat("SOURCE_CHAT")

TARGET_BOT_TOKEN = _require("TARGET_BOT_TOKEN")
TARGET_CHAT = _parse_chat("TARGET_CHAT")

ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

MIN_MESSAGE_LENGTH = int(os.getenv("MIN_MESSAGE_LENGTH", "15"))

SESSION_PATH = "session/userbot"
SYSTEM_PROMPT_PATH = "prompts/system_prompt.txt"
