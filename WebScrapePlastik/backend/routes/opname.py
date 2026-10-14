import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend import opname as op
from backend.auth import get_current_user, require_admin
from backend.schemas import OpnameOut, OpnameRequest, OpnameResolveRequest

router = APIRouter()


def _raise(e: ValueError):
    msg = str(e)
    low = msg.lower()
    if "tidak ditemukan" in low:
        raise HTTPException(status_code=404, detail=msg)
    if any(k in low for k in ("sudah dikirim", "hanya draft", "hanya opname", "stok berubah", "tidak bisa diubah", "sudah diproses")):
        raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=422, detail=msg)


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


def _items(req: OpnameRequest) -> list[dict]:
    return [i.model_dump() for i in req.items]


@router.get("/stock-opnames/summary")
async def opname_summary():
    return await _run(op.opname_summary)


@router.get("/stock-opnames/number-preview")
async def opname_number_preview(day: str):
    return {"opname_no": await _run(op.number_preview, day)}


@router.get("/stock-opnames/reasons")
async def opname_reasons():
    return {"reasons": list(op.REASONS)}


@router.get("/stock-opnames/item-activity/{master_item_id}")
async def opname_item_activity(master_item_id: int):
    def work():
        with op._conn() as con:
            if con.execute("SELECT 1 FROM master_items WHERE id=?", (master_item_id,)).fetchone() is None:
                return None
            return op._activity_counts(con, master_item_id)
    row = await _run(work)
    if row is None:
        raise HTTPException(status_code=404, detail="Barang tidak ditemukan.")
    return row


@router.get("/stock-opnames", response_model=list[OpnameOut])
async def list_stock_opnames(status: str | None = None, q: str | None = None):
    if status and status not in op.STATUSES:
        raise HTTPException(status_code=422, detail="Status tidak dikenal.")
    return await _run(op.list_opnames, status, (q or "").strip() or None)


@router.get("/stock-opnames/{opname_id}", response_model=OpnameOut)
async def get_stock_opname(opname_id: int):
    row = await _run(op.get_opname, opname_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Stock opname tidak ditemukan.")
    return row


@router.post("/stock-opnames", response_model=OpnameOut, status_code=201)
async def create_stock_opname(req: OpnameRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.create_opname, req.officer, req.opname_at, req.location, req.note, _items(req), req.submit, user["username"])
    except ValueError as e:
        _raise(e)


@router.patch("/stock-opnames/{opname_id}", response_model=OpnameOut)
async def update_stock_opname(opname_id: int, req: OpnameRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.update_opname, opname_id, req.officer, req.opname_at, req.location, req.note, _items(req), req.submit, user["username"])
    except ValueError as e:
        _raise(e)


@router.post("/stock-opnames/{opname_id}/submit", response_model=OpnameOut)
async def submit_stock_opname(opname_id: int, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.submit_opname, opname_id, user["username"])
    except ValueError as e:
        _raise(e)


@router.post("/stock-opnames/{opname_id}/approve", response_model=OpnameOut, dependencies=[Depends(require_admin)])
async def approve_stock_opname(opname_id: int, req: OpnameResolveRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.approve_opname, opname_id, user["username"], req.note, req.accept_moved)
    except ValueError as e:
        _raise(e)


@router.post("/stock-opnames/{opname_id}/reject", response_model=OpnameOut, dependencies=[Depends(require_admin)])
async def reject_stock_opname(opname_id: int, req: OpnameResolveRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.close_opname, opname_id, "ditolak", user["username"], req.note)
    except ValueError as e:
        _raise(e)


@router.post("/stock-opnames/{opname_id}/cancel", response_model=OpnameOut)
async def cancel_stock_opname(opname_id: int, req: OpnameResolveRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(op.close_opname, opname_id, "dibatalkan", user["username"], req.note)
    except ValueError as e:
        _raise(e)
