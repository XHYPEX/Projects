import asyncio
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.database import (
    PREORDER_STATUSES,
    create_preorder,
    get_all_preorders,
    get_preorder,
    get_preorder_history,
    get_preorder_items,
    set_preorder_item_status,
    update_preorder,
)
from backend.schemas import (
    PreorderDetail,
    PreorderHistoryOut,
    PreorderItemOut,
    PreorderItemRequest,
    PreorderItemStatusRequest,
    PreorderOut,
    PreorderRequest,
    PreorderUpdateRequest,
)

router = APIRouter()

# Same permissive rule the cashier uses: an optional leading +, then 7-15 digits
# once separators are stripped.
PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def _normalize_phone(raw: str) -> str:
    return re.sub(r"[\s\-.()]", "", raw or "")


def _validate_phone(raw: str) -> str:
    phone = _normalize_phone(raw)
    if not phone:
        raise HTTPException(status_code=422, detail="Nomor HP pelanggan wajib diisi.")
    if not PHONE_RE.match(phone):
        raise HTTPException(
            status_code=422,
            detail="Nomor HP pelanggan harus 7-15 digit (boleh diawali tanda +).",
        )
    return phone


def _validate_status(status: str) -> str:
    if status not in PREORDER_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Status '{status}' tidak dikenal. Pilihan: {', '.join(PREORDER_STATUSES)}.",
        )
    return status


def _validate_items(items: list[PreorderItemRequest]) -> list[dict]:
    validated: list[dict] = []
    for item in items:
        if not item.product_name.strip():
            raise HTTPException(status_code=422, detail="Nama barang wajib diisi.")
        if item.quantity <= 0:
            raise HTTPException(status_code=422, detail="Jumlah harus bilangan bulat positif.")
        if item.unit_price < 0:
            raise HTTPException(status_code=422, detail="Harga satuan tidak boleh negatif.")
        if item.status is not None:
            _validate_status(item.status)
        payload = item.model_dump()
        payload["product_name"] = item.product_name.strip()
        payload["unit"] = item.unit.strip() if item.unit else None
        validated.append(payload)
    return validated


def _row_to_item(row: dict) -> PreorderItemOut:
    return PreorderItemOut(
        id=row["id"],
        master_item_id=row["master_item_id"],
        sku=row.get("sku"),
        product_name=row["product_name"],
        unit=row["unit"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        status=row["status"],
    )


async def _detail(preorder_id: str) -> PreorderDetail:
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_preorder, preorder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    items = await loop.run_in_executor(None, get_preorder_items, preorder_id)
    history = await loop.run_in_executor(None, get_preorder_history, preorder_id)
    return PreorderDetail(
        **row,
        items=[_row_to_item(i) for i in items],
        history=[PreorderHistoryOut(**h) for h in history],
    )


@router.post("/preorders", response_model=PreorderDetail, status_code=201)
async def create_new_preorder(req: PreorderRequest, user: dict = Depends(get_current_user)):
    customer_name = req.customer_name.strip()
    if not customer_name:
        raise HTTPException(status_code=422, detail="Nama pelanggan wajib diisi.")
    customer_phone = _validate_phone(req.customer_phone)
    if not req.items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu baris barang.")
    if req.deposit < 0:
        raise HTTPException(status_code=422, detail="Uang muka tidak boleh negatif.")

    items = _validate_items(req.items)
    notes = req.notes.strip() if req.notes else None

    preorder_id = str(uuid4())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, create_preorder, preorder_id, customer_name, customer_phone, items,
        req.deposit, notes, user["username"],
    )
    return await _detail(preorder_id)


@router.get("/preorders", response_model=list[PreorderOut])
async def list_preorders(query: str | None = None, status: str | None = None, draft: bool = False):
    if status:
        _validate_status(status)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_all_preorders, query, status, draft)
    return [PreorderOut(**r) for r in rows]


@router.get("/preorders/{preorder_id}", response_model=PreorderDetail)
async def get_preorder_detail(preorder_id: str):
    return await _detail(preorder_id)


@router.patch("/preorders/{preorder_id}", response_model=PreorderDetail)
async def update_preorder_route(
    preorder_id: str, req: PreorderUpdateRequest, user: dict = Depends(get_current_user)
):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_preorder, preorder_id) is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")

    customer_name: str | None = None
    if req.customer_name is not None:
        customer_name = req.customer_name.strip()
        if not customer_name:
            raise HTTPException(status_code=422, detail="Nama pelanggan wajib diisi.")
    customer_phone = _validate_phone(req.customer_phone) if req.customer_phone is not None else None
    if req.deposit is not None and req.deposit < 0:
        raise HTTPException(status_code=422, detail="Uang muka tidak boleh negatif.")

    items: list[dict] | None = None
    if req.items is not None:
        if not req.items:
            raise HTTPException(status_code=422, detail="Minimal harus ada satu baris barang.")
        items = _validate_items(req.items)

    # Only touch notes when the client actually sent the field, so an omitted
    # field leaves the stored note intact while an explicit null clears it.
    update_notes = "notes" in req.model_fields_set
    notes = (req.notes.strip() if req.notes else None) if update_notes else None

    await loop.run_in_executor(
        None, update_preorder, preorder_id, customer_name, customer_phone, items,
        req.deposit, notes, update_notes, user["username"], req.is_draft,
    )
    return await _detail(preorder_id)


@router.patch("/preorders/{preorder_id}/items/{item_id}/status", response_model=PreorderDetail)
async def update_preorder_item_status(
    preorder_id: str,
    item_id: int,
    req: PreorderItemStatusRequest,
    user: dict = Depends(get_current_user),
):
    status = _validate_status(req.status)
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_preorder, preorder_id) is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    try:
        await loop.run_in_executor(
            None, set_preorder_item_status, preorder_id, item_id, status, user["username"]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await _detail(preorder_id)


# There is deliberately no DELETE /preorders/{preorder_id} route. A preorder
# someone wants off the active list is archived via
# PATCH /preorders/{preorder_id} {"is_draft": true} instead -- preorders are
# never deleted. Do not re-add a delete route here.
