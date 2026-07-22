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
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = get_session(hash_token(session_token))
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if session["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=401, detail="Session expired")
    if not session["is_active"]:
        raise HTTPException(status_code=401, detail="Account deactivated")
    return {"id": session["user_id"], "username": session["username"], "role": session["role"]}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
