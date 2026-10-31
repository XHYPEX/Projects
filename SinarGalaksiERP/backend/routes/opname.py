import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend import opname as op, warehouses
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


@router.get("/stock-opnames/load-items")
async def opname_load_items(location: str | None = None, limit: int = 500, warehouse_id: int | None = None, rack_id: int | None = None):
    """Active master items in one gudang / rak (Master Gudang), on one shelf typed as text (substring match, items
    without a gudang), or the whole shop when nothing is given: the starting list of a per-location / full count."""
    limit = max(1, min(limit, 1000))

    def work():
        where, params = ["m.is_active=1"], []
        if warehouse_id is not None:
            where.append("m.warehouse_id=?")
            params.append(warehouse_id)
            if rack_id is not None:
                where.append("m.rack_id=?")
                params.append(rack_id)
        elif (location or "").strip():
            where.append("UPPER(IFNULL(m.location,'')) LIKE ?")
            params.append(f"%{location.strip().upper()}%")
        with op._conn() as con:
            total = con.execute(f"SELECT COUNT(*) AS c FROM master_items m WHERE {' AND '.join(where)}", params).fetchone()["c"]
            rows = con.execute(
                f"""SELECT m.id, m.sku, m.name, m.unit, m.location, m.stock_qty, b.name AS brand_name,
                           c.name AS category_name, pc.name AS category_parent_name
                    FROM master_items m
                    LEFT JOIN brands b ON b.id = m.brand_id
                    LEFT JOIN categories c ON c.id = m.category_id
                    LEFT JOIN categories pc ON pc.id = c.parent_id
                    WHERE {' AND '.join(where)} ORDER BY IFNULL(m.location,''), m.name LIMIT ?""",
                (*params, limit),
            ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}
    return await _run(work)


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
        _, _, label = await _run(warehouses.check_pair, req.warehouse_id, req.rack_id)
        row = await _run(op.create_opname, req.officer, req.opname_at, label or req.location, req.note, _items(req), req.submit, user["username"])
        if req.warehouse_id:
            await _run(warehouses.stamp_opname, row["id"], req.warehouse_id, req.rack_id)
            row = await _run(op.get_opname, row["id"])
        return row
    except ValueError as e:
        _raise(e)


@router.patch("/stock-opnames/{opname_id}", response_model=OpnameOut)
async def update_stock_opname(opname_id: int, req: OpnameRequest, user: dict = Depends(get_current_user)):
    try:
        cur = await _run(op.get_opname, opname_id)
        keep = (cur.get("warehouse_id"), cur.get("rack_id")) if cur else (None, None)
        _, _, label = await _run(warehouses.check_pair, req.warehouse_id, req.rack_id, keep)
        await _run(op.update_opname, opname_id, req.officer, req.opname_at, label or req.location, req.note, _items(req), req.submit, user["username"])
        await _run(warehouses.stamp_opname, opname_id, req.warehouse_id, req.rack_id)
        if not req.warehouse_id and req.location:
            await _run(_keep_text_location, opname_id, req.location)
        return await _run(op.get_opname, opname_id)
    except ValueError as e:
        _raise(e)


def _keep_text_location(opname_id: int, location: str) -> None:
    """An opname saved with only typed text (made before Master Gudang) keeps that text."""
    with op._conn() as con:
        con.execute("UPDATE stock_opnames SET location=? WHERE id=?", (location.strip() or None, opname_id))


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
