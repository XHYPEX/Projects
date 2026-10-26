import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, HTTPException

from backend.database import get_session

SESSION_COOKIE_NAME = "session_token"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60  # 12 hours, fixed
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

PASSWORD_MAX_LENGTH = 72  # bcrypt silently truncates beyond this


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


async def get_current_user(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)) -> dict:
    if not session_token:
        raise HTTPException(status_code=401, detail="Belum login. Silakan login terlebih dahulu.")
    session = get_session(hash_token(session_token))
    if session is None:
        raise HTTPException(status_code=401, detail="Belum login. Silakan login terlebih dahulu.")
    if session["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=401, detail="Sesi berakhir. Silakan login kembali.")
    if not session["is_active"]:
        raise HTTPException(status_code=401, detail="Akun sudah dinonaktifkan.")
    return {"id": session["user_id"], "username": session["username"], "role": session["role"],
            "is_owner": bool(session.get("is_owner")), "can_post_inbound": bool(session.get("can_post_inbound"))}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang diizinkan.")
    return user


async def require_inbound_poster(user: dict = Depends(get_current_user)) -> dict:
    """K-22: posting Barang Masuk only for users given that permission (Pengguna page)."""
    if not user.get("can_post_inbound"):
        raise HTTPException(status_code=403, detail="Anda belum diberi izin posting Barang Masuk. Minta admin mengaktifkannya di halaman Pengguna.")
    return user


async def require_owner(user: dict = Depends(get_current_user)) -> dict:
    """Final approval of losses (quarantine write-off): an admin carrying the Owner flag."""
    if user["role"] != "admin" or not user.get("is_owner"):
        raise HTTPException(status_code=403, detail="Hanya Owner yang boleh menyetujui ini.")
    return user
