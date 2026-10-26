"""Stok Karantina: goods in the shop that may not be sold. Anyone logged in can look and ask for a
write-off; releasing to stock and moving stock in are admin work; the final write-off is the Owner's."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user, require_admin, require_owner
from backend.database import (
    decide_quarantine_writeoff,
    get_quarantine_lot,
    list_quarantine,
    list_quarantine_writeoffs,
    quarantine_from_stock,
    quarantine_to_secondhand,
    release_quarantine,
    request_quarantine_writeoff,
    review_quarantine_writeoff,
)
from backend.schemas import QuarantineDecisionRequest, QuarantineFromStockRequest, QuarantineQtyRequest

router = APIRouter()


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/quarantine")
async def quarantine_list(view: str = "aktif", query: str | None = None):
    if view not in ("aktif", "kandidat", "selesai", "semua"):
        raise HTTPException(status_code=422, detail="view harus aktif, kandidat, selesai, atau semua.")
    return await _run(list_quarantine, view, query or None)


@router.get("/quarantine/writeoffs")
async def quarantine_writeoffs(status: str | None = None):
    return await _run(list_quarantine_writeoffs, status)


@router.post("/quarantine", dependencies=[Depends(require_admin)])
async def quarantine_add(req: QuarantineFromStockRequest, user: dict = Depends(get_current_user)):
    return await _run(quarantine_from_stock, req.master_item_id, req.qty, req.condition, req.note, user["username"])


@router.get("/quarantine/{lot_id}")
async def quarantine_detail(lot_id: int):
    return await _run(get_quarantine_lot, lot_id)


@router.post("/quarantine/{lot_id}/release", dependencies=[Depends(require_admin)])
async def quarantine_release(lot_id: int, req: QuarantineQtyRequest, user: dict = Depends(get_current_user)):
    return await _run(release_quarantine, lot_id, req.qty, req.reason, user["username"])


@router.post("/quarantine/{lot_id}/secondhand", dependencies=[Depends(require_admin)])
async def quarantine_secondhand(lot_id: int, body: dict, user: dict = Depends(get_current_user)):
    try:
        qty, price = int(body.get("qty") or 0), int(body.get("sell_price") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Jumlah dan harga harus angka.")
    try:
        return await _run(quarantine_to_secondhand, lot_id, qty, price, body.get("note") or "", user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/quarantine/{lot_id}/writeoffs")
async def quarantine_writeoff_request(lot_id: int, req: QuarantineQtyRequest, user: dict = Depends(get_current_user)):
    return await _run(request_quarantine_writeoff, lot_id, req.qty, req.reason, user)


@router.post("/quarantine/writeoffs/{writeoff_id}/review", dependencies=[Depends(require_admin)])
async def quarantine_writeoff_review(writeoff_id: int, req: QuarantineDecisionRequest, user: dict = Depends(get_current_user)):
    return await _run(review_quarantine_writeoff, writeoff_id, req.approve, req.note, user["username"])


@router.post("/quarantine/writeoffs/{writeoff_id}/decide", dependencies=[Depends(require_owner)])
async def quarantine_writeoff_decide(writeoff_id: int, req: QuarantineDecisionRequest, user: dict = Depends(get_current_user)):
    return await _run(decide_quarantine_writeoff, writeoff_id, req.approve, req.note, user["username"])
