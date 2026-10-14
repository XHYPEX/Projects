import asyncio
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.database import (
    PREORDER_STATUSES,
    add_preorder_payment,
    apply_preorder_dp,
    create_receipt,
    get_preorder_allocations,
    preorder_convertible_items,
    preorder_unallocated_dp,
    _conn,
    _preorder_item_logistics,
    create_preorder,
    get_all_preorders,
    get_preorder,
    get_preorder_history,
    get_preorder_items,
    get_preorder_payments,
    parse_ymd,
    set_preorder_item_status,
    set_preorder_status,
    update_preorder,
)
from backend.schemas import (
    PreorderAllocationOut,
    PreorderConvertRequest,
    PreorderConvertResponse,
    PreorderDetail,
    PreorderHistoryOut,
    PreorderItemOut,
    PreorderItemRequest,
    PreorderItemStatusRequest,
    PreorderOut,
    PreorderPaymentOut,
    PreorderPaymentRequest,
    PreorderRequest,
    PreorderStatusRequest,
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


def _validate_plate(raw: str) -> str:
    # Stored upper-cased with single spaces, the way plates are read off a car.
    plate = " ".join((raw or "").upper().split())
    if not plate:
        raise HTTPException(status_code=422, detail="Plat mobil wajib diisi.")
    return plate


def _validate_po_date(raw: str) -> str:
    try:
        return parse_ymd(raw).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal PO harus berformat YYYY-MM-DD.")


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
        try:
            _preorder_item_logistics(item.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
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
        receipt_id=row.get("receipt_id"),
        supplier_pos=row.get("supplier_pos") or [],
        ship_route=row.get("ship_route"),
        priority=row.get("priority") or "normal",
        eta_date=row.get("eta_date"),
    )


async def _detail(preorder_id: str) -> PreorderDetail:
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_preorder, preorder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    items = await loop.run_in_executor(None, get_preorder_items, preorder_id)
    history = await loop.run_in_executor(None, get_preorder_history, preorder_id)
    payments = await loop.run_in_executor(None, get_preorder_payments, preorder_id)
    allocations = await loop.run_in_executor(None, get_preorder_allocations, preorder_id)
    def _free() -> int:
        with _conn() as con:
            return preorder_unallocated_dp(con, preorder_id)
    dp_free = await loop.run_in_executor(None, _free)
    return PreorderDetail(
        **row,
        allocations=[PreorderAllocationOut(**a) for a in allocations],
        dp_unallocated=dp_free,
        items=[_row_to_item(i) for i in items],
        history=[PreorderHistoryOut(**h) for h in history],
        payments=[PreorderPaymentOut(**p) for p in payments],
    )


@router.post("/preorders", response_model=PreorderDetail, status_code=201)
async def create_new_preorder(req: PreorderRequest, user: dict = Depends(get_current_user)):
    customer_name = req.customer_name.strip()
    if not customer_name:
        raise HTTPException(status_code=422, detail="Nama pelanggan wajib diisi.")
    customer_phone = _validate_phone(req.customer_phone)
    plate_full = _validate_plate(req.plate_full)
    po_date = _validate_po_date(req.po_date) if req.po_date else None
    if not req.items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu baris barang.")
    if req.deposit < 0:
        raise HTTPException(status_code=422, detail="Uang muka tidak boleh negatif.")

    items = _validate_items(req.items)
    if req.deposit > sum(i["quantity"] * i["unit_price"] for i in items):
        raise HTTPException(status_code=422, detail="Uang muka melebihi total pesanan.")
    notes = req.notes.strip() if req.notes else None

    preorder_id = str(uuid4())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, create_preorder, preorder_id, customer_name, customer_phone, items,
        req.deposit, notes, user["username"], plate_full, po_date, req.is_draft,
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
    current = await loop.run_in_executor(None, get_preorder, preorder_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    if req.expected_updated_at and current.get("updated_at") and req.expected_updated_at != current["updated_at"]:
        raise HTTPException(status_code=409, detail="Data ini baru saja diubah oleh pengguna lain. Muat ulang halaman lalu ulangi perubahan Anda.")

    customer_name: str | None = None
    if req.customer_name is not None:
        customer_name = req.customer_name.strip()
        if not customer_name:
            raise HTTPException(status_code=422, detail="Nama pelanggan wajib diisi.")
    customer_phone = _validate_phone(req.customer_phone) if req.customer_phone is not None else None
    plate_full = _validate_plate(req.plate_full) if req.plate_full is not None else None
    po_date = _validate_po_date(req.po_date) if req.po_date is not None else None

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
        None,
        lambda: update_preorder(
            preorder_id, customer_name=customer_name, customer_phone=customer_phone, items=items,
            notes=notes, update_notes=update_notes, changed_by=user["username"],
            is_draft=req.is_draft, plate_full=plate_full, po_date=po_date,
        ),
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


@router.patch("/preorders/{preorder_id}/status", response_model=PreorderDetail)
async def update_preorder_status(
    preorder_id: str, req: PreorderStatusRequest, user: dict = Depends(get_current_user)
):
    status = _validate_status(req.status)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, set_preorder_status, preorder_id, status, user["username"])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/payments", response_model=PreorderDetail, status_code=201)
async def add_payment(
    preorder_id: str, req: PreorderPaymentRequest, user: dict = Depends(get_current_user)
):
    if req.amount <= 0:
        raise HTTPException(status_code=422, detail="Nominal pembayaran harus lebih dari 0.")
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_preorder, preorder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    if req.amount > row["remaining"]:
        raise HTTPException(
            status_code=422,
            detail=f"Pembayaran melebihi sisa tagihan (sisa Rp {row['remaining']:,}).".replace(",", "."),
        )
    if req.method not in ("cash", "transfer", "qris", "debit"):
        raise HTTPException(status_code=422, detail="Metode pembayaran harus salah satu dari: cash, transfer, qris, debit.")
    note = req.note.strip() if req.note and req.note.strip() else None
    await loop.run_in_executor(None, add_preorder_payment, preorder_id, req.amount, note, user["username"], req.method)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/convert", response_model=PreorderConvertResponse, status_code=201)
async def convert_to_receipt(
    preorder_id: str, req: PreorderConvertRequest, user: dict = Depends(get_current_user)
):
    """PO Customer -> nota. Takes the lines that have arrived, opens a nota (Siap Pembayaran)
    and applies the PO's unallocated DP to it once, through the allocation ledger."""
    loop = asyncio.get_event_loop()
    po = await loop.run_in_executor(None, get_preorder, preorder_id)
    if po is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    lines = await loop.run_in_executor(None, preorder_convertible_items, preorder_id, req.item_ids)
    if req.item_ids is not None and len(lines) != len(set(req.item_ids)):
        raise HTTPException(status_code=409, detail="Ada item yang belum sampai di toko atau sudah masuk nota.")
    if not lines:
        raise HTTPException(status_code=409, detail="Belum ada item PO yang sampai di toko dan belum masuk nota.")

    parts = (po["plate_full"] or "").split()
    region = (req.plate_region or (parts[0] if len(parts) == 3 else "")).upper()
    number = req.plate_number or (parts[1] if len(parts) == 3 else "")
    suffix = (req.plate_suffix or (parts[2] if len(parts) == 3 else "")).upper()
    if not (region and number and suffix):
        raise HTTPException(status_code=422, detail="Plat nomor belum ada di PO ini. Isi plat untuk membuat nota.")
    from backend.routes.receipts import NUMBER_RE, PLATE_CODES, SUFFIX_RE
    if region not in PLATE_CODES or not NUMBER_RE.match(number) or not SUFFIX_RE.match(suffix):
        raise HTTPException(status_code=422, detail="Plat nomor tidak valid.")

    items = [{
        "product_name": l["product_name"], "quantity": l["quantity"], "unit_price": l["unit_price"],
        "sku": l.get("sku"), "item_type": "barang", "line_discount": 0, "warranty_date": None,
    } for l in lines]
    receipt_id = str(uuid4())
    try:
        result = await loop.run_in_executor(
            None, create_receipt, receipt_id, region, number, suffix, items, 0, 0, None,
            po["customer_phone"], po["customer_name"], None, "cash", None, None, None,
            user["username"], "belum", None,
        )
        dp = await loop.run_in_executor(
            None, apply_preorder_dp, preorder_id, receipt_id, [l["id"] for l in lines], user["username"]
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return PreorderConvertResponse(
        receipt_id=receipt_id, receipt_no=result["receipt_no"], total=result["total"],
        dp_applied=dp, amount_due=max(result["total"] - dp, 0),
    )


# There is deliberately no DELETE /preorders/{preorder_id} route. A preorder
# someone wants off the active list is archived via
# PATCH /preorders/{preorder_id} {"is_draft": true} instead -- preorders are
# never deleted. Do not re-add a delete route here.
