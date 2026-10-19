"""Master Ekspedisi: the carriers / forwarders Paket Pengiriman can use (kept apart from suppliers)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user, require_admin
from backend.database import create_forwarder, delete_forwarder, list_forwarders, update_forwarder
from backend.schemas import ForwarderRequest

router = APIRouter()


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/forwarders")
async def forwarders_list(active_only: bool = False):
    return await _run(list_forwarders, not active_only)


@router.post("/forwarders", status_code=201)
async def forwarders_create(req: ForwarderRequest, user: dict = Depends(get_current_user)):
    return await _run(create_forwarder, req.model_dump())


@router.put("/forwarders/{forwarder_id}")
async def forwarders_update(forwarder_id: int, req: ForwarderRequest, user: dict = Depends(require_admin)):
    return await _run(update_forwarder, forwarder_id, req.model_dump())


@router.delete("/forwarders/{forwarder_id}")
async def forwarders_delete(forwarder_id: int, user: dict = Depends(require_admin)):
    return await _run(delete_forwarder, forwarder_id)
