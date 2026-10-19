import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user, hash_password
from backend.database import (
    create_user,
    get_user_by_id,
    list_users,
    reset_user_password,
    set_user_active,
    set_user_owner,
    set_user_post_inbound,
    update_user_role,
)
from backend.schemas import PasswordResetRequest, UserCreateRequest, UserOut, UserUpdateRequest

router = APIRouter()

_VALID_ROLES = {"admin", "staff"}


def _row_to_user(row: dict) -> UserOut:
    return UserOut(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        is_owner=bool(row.get("is_owner")),
        can_post_inbound=bool(row.get("can_post_inbound")),
    )


def _raise_from_value_error(e: ValueError) -> None:
    msg = str(e)
    if "tidak ditemukan" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


@router.get("/users", response_model=list[UserOut])
async def list_all_users():
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, list_users)
    return [_row_to_user(r) for r in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_new_user(req: UserCreateRequest):
    if not req.username.strip():
        raise HTTPException(status_code=422, detail="Username wajib diisi.")
    if req.role not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Peran harus salah satu dari: {', '.join(sorted(_VALID_ROLES))}.")
    loop = asyncio.get_event_loop()
    password_hash = hash_password(req.password)
    try:
        row = await loop.run_in_executor(None, create_user, req.username.strip(), password_hash, req.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _row_to_user(row)


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(user_id: int, req: UserUpdateRequest, me: dict = Depends(get_current_user)):
    if req.role is not None and req.role not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Peran harus salah satu dari: {', '.join(sorted(_VALID_ROLES))}.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_user_by_id, user_id) is None:
        raise HTTPException(status_code=404, detail=f"Pengguna {user_id} tidak ditemukan.")
    try:
        row = None
        if req.role is not None:
            row = await loop.run_in_executor(None, update_user_role, user_id, req.role)
        if req.is_active is not None:
            row = await loop.run_in_executor(None, set_user_active, user_id, req.is_active)
        if req.is_owner is not None:
            row = await loop.run_in_executor(None, set_user_owner, user_id, req.is_owner, me)
        if req.can_post_inbound is not None:
            row = await loop.run_in_executor(None, set_user_post_inbound, user_id, req.can_post_inbound)
    except ValueError as e:
        _raise_from_value_error(e)
    if row is None:
        row = await loop.run_in_executor(None, get_user_by_id, user_id)
    return _row_to_user(row)


@router.post("/users/{user_id}/reset-password", status_code=204)
async def reset_password(user_id: int, req: PasswordResetRequest):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_user_by_id, user_id) is None:
        raise HTTPException(status_code=404, detail=f"Pengguna {user_id} tidak ditemukan.")
    password_hash = hash_password(req.new_password)
    await loop.run_in_executor(None, reset_user_password, user_id, password_hash)
