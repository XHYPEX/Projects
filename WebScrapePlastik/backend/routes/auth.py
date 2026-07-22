import asyncio

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response

from backend.auth import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    generate_session_token,
    get_current_user,
    hash_password,
    hash_token,
    session_expiry,
    set_session_cookie,
    verify_password,
)
from backend.database import (
    count_users,
    create_session_row,
    create_user,
    delete_session,
    get_user_by_username,
)
from backend.schemas import LoginRequest, SetupRequest, SetupRequiredOut, UserOut

router = APIRouter()


def _row_to_user(row: dict) -> UserOut:
    return UserOut(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def _start_session(response: Response, user_id: int) -> None:
    token = generate_session_token()
    create_session_row(hash_token(token), user_id, session_expiry())
    set_session_cookie(response, token)


@router.get("/auth/setup-required", response_model=SetupRequiredOut)
async def setup_required():
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(None, count_users)
    return SetupRequiredOut(setup_required=n == 0)


@router.post("/auth/setup", response_model=UserOut, status_code=201)
async def setup(req: SetupRequest, response: Response):
    if not req.username.strip():
        raise HTTPException(status_code=422, detail="username must not be empty")
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(None, count_users)
    if n != 0:
        raise HTTPException(status_code=409, detail="Setup already completed")
    password_hash = hash_password(req.password)
    try:
        row = await loop.run_in_executor(None, create_user, req.username.strip(), password_hash, "admin")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _start_session(response, row["id"])
    return _row_to_user(row)


@router.post("/auth/login", response_model=UserOut)
async def login(req: LoginRequest, response: Response):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_user_by_username, req.username)
    if row is None or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not row["is_active"]:
        raise HTTPException(status_code=401, detail="Account is deactivated")
    _start_session(response, row["id"])
    return _row_to_user(row)


@router.post("/auth/logout", status_code=204)
async def logout(response: Response, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)):
    if session_token:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, delete_session, hash_token(session_token))
    clear_session_cookie(response)
    return Response(status_code=204)


@router.get("/auth/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_user_by_username, user["username"])
    if row is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _row_to_user(row)
