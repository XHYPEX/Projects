import asyncio
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user, require_admin
from backend.database import (
    get_supplier,
    PREORDER_STATUSES,
    PO_LINE_CANCELLED,
    cancel_preorder,
    cancel_preorder_items,
    get_preorder_cancel_check,
    preview_preorder_cancel,
    add_preorder_payment,
    get_receipt_payments,
    po_invoice_payment,
    split_plate,
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
    PreorderCancelRequest,
    PreorderSpecialCancelRequest,
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


def _require_invoice_plate(plate: str) -> None:
    """A published PO gets its invoice at once, and an invoice needs a plate the cashier accepts."""
    if split_plate(plate) is None:
        raise HTTPException(status_code=422, detail="Plat mobil harus berformat seperti B 1234 ABC (kode wilayah, nomor, huruf).")


def _po_error(e: ValueError) -> HTTPException:
    msg = str(e)
    status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Stok", "Plat", "Uang muka")) else 409
    return HTTPException(status_code=status, detail=msg)


def _validate_po_date(raw: str) -> str:
    try:
        return parse_ymd(raw).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal PO harus berformat YYYY-MM-DD.")


def _validate_status(status: str, allow_cancelled: bool = False) -> str:
    if status == PO_LINE_CANCELLED and allow_cancelled:
        return status
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
            _validate_status(item.status, allow_cancelled=True)
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
        reserved_qty=row.get("reserved_qty") or 0,
        coming_qty=row.get("coming_qty") or 0,
        shortfall_qty=row.get("shortfall_qty") or 0,
        cancelled_at=row.get("cancelled_at"),
        cancelled_by=row.get("cancelled_by"),
        cancel_kind=row.get("cancel_kind"),
        cancel_reason=row.get("cancel_reason"),
    )


async def _detail(preorder_id: str) -> PreorderDetail:
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_preorder, preorder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    items = await loop.run_in_executor(None, get_preorder_items, preorder_id)
    history = await loop.run_in_executor(None, get_preorder_history, preorder_id)
    if row.get("receipt_id"):
        # Money is taken on the invoice; the PO shows the invoice's payments.
        payments = [
            {"id": p["id"], "paid_at": p["paid_at"], "amount": p["amount"], "note": p["note"],
             "created_by": p["created_by"], "method": p["method"]}
            for p in await loop.run_in_executor(None, get_receipt_payments, row["receipt_id"])
        ]
    else:
        payments = await loop.run_in_executor(None, get_preorder_payments, preorder_id)
    allocations = await loop.run_in_executor(None, get_preorder_allocations, preorder_id)
    def _free() -> int:
        with _conn() as con:
            return preorder_unallocated_dp(con, preorder_id)
    dp_free = await loop.run_in_executor(None, _free)
    cancel_check = await loop.run_in_executor(None, get_preorder_cancel_check, preorder_id)
    return PreorderDetail(
        **row,
        cancel_check=cancel_check,
        allocations=[PreorderAllocationOut(**a) for a in allocations],
        dp_unallocated=dp_free,
        items=[_row_to_item(i) for i in items],
        history=[PreorderHistoryOut(**h) for h in history],
        payments=[PreorderPaymentOut(**p) for p in payments],
    )


async def _require_supplier(loop, supplier_id: int | None) -> None:
    if supplier_id is None:
        return
    sup = await loop.run_in_executor(None, get_supplier, supplier_id)
    if sup is None or not sup["is_active"]:
        raise HTTPException(status_code=422, detail="Supplier tidak ditemukan atau tidak aktif.")


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
    if req.deposit_method not in ("cash", "transfer", "qris", "debit"):
        raise HTTPException(status_code=422, detail="Metode pembayaran harus salah satu dari: cash, transfer, qris, debit.")
    if not req.is_draft:
        _require_invoice_plate(plate_full)

    items = _validate_items(req.items)
    if req.deposit > sum(i["quantity"] * i["unit_price"] for i in items):
        raise HTTPException(status_code=422, detail="Uang muka melebihi total pesanan.")
    notes = req.notes.strip() if req.notes else None

    preorder_id = str(uuid4())
    loop = asyncio.get_event_loop()
    await _require_supplier(loop, req.supplier_id)
    try:
        await loop.run_in_executor(
            None, lambda: create_preorder(
                preorder_id, customer_name, customer_phone, items, req.deposit, notes, user["username"],
                plate_full, po_date, req.is_draft, deposit_method=req.deposit_method, supplier_id=req.supplier_id,
            ),
        )
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


@router.get("/preorders", response_model=list[PreorderOut])
async def list_preorders(query: str | None = None, status: str | None = None, draft: bool = False):
    if status:
        _validate_status(status, allow_cancelled=True)
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
    will_be_draft = current["is_draft"] if req.is_draft is None else req.is_draft
    if not will_be_draft and (plate_full is not None or current["is_draft"]):
        _require_invoice_plate(plate_full if plate_full is not None else current["plate_full"])

    set_supplier = "supplier_id" in req.model_fields_set
    if set_supplier:
        await _require_supplier(loop, req.supplier_id)
    try:
        await loop.run_in_executor(
            None,
            lambda: update_preorder(
                preorder_id, customer_name=customer_name, customer_phone=customer_phone, items=items,
                notes=notes, update_notes=update_notes, changed_by=user["username"],
                is_draft=req.is_draft, plate_full=plate_full, po_date=po_date,
                set_supplier=set_supplier, supplier_id=req.supplier_id,
            ),
        )
    except ValueError as e:
        raise _po_error(e)
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
        raise _po_error(e)
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
        raise _po_error(e)
    return await _detail(preorder_id)


@router.get("/preorders/{preorder_id}/cancel-preview")
async def cancel_preview_route(preorder_id: str, items: str | None = None, _admin: dict = Depends(require_admin)):
    """Money a cancellation would move. `items` = comma-separated line ids (Pembatalan Khusus)."""
    ids = None
    if items is not None:
        try:
            ids = [int(x) for x in items.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="Daftar barang tidak valid.")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, preview_preorder_cancel, preorder_id, ids)
    except ValueError as e:
        raise _po_error(e)


@router.post("/preorders/{preorder_id}/cancel", response_model=PreorderDetail)
async def cancel_preorder_route(preorder_id: str, req: PreorderCancelRequest, user: dict = Depends(require_admin)):
    """K-13 Batalkan (customer request, admin only): shop-stock lines any time before installation; indent
    lines only within 24 hours and before their Purchase Order is issued."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: cancel_preorder(
            preorder_id, req.reason, req.refund_amount, req.refund_method, user["username"], req.item_ids))
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/cancel-items", response_model=PreorderDetail)
async def cancel_preorder_items_route(
    preorder_id: str, req: PreorderSpecialCancelRequest, user: dict = Depends(require_admin)
):
    """K-13 Pembatalan Khusus: the shop or supplier failed the customer on these lines (admin only)."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: cancel_preorder_items(
            preorder_id, req.item_ids, req.category, req.reason, req.refund_method, user["username"]))
    except ValueError as e:
        raise _po_error(e)
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
    if row.get("receipt_id"):
        if req.method == "transfer" and not ((req.transfer_sender or "").strip() and (req.transfer_account or "").strip()):
            raise HTTPException(status_code=422, detail="Untuk transfer, isi nama pengirim dan rekening tujuan.")
        try:
            await loop.run_in_executor(
                None, po_invoice_payment, preorder_id, req.amount, req.method, note, user["username"],
                (req.transfer_sender or "").strip() or None, (req.transfer_account or "").strip() or None,
            )
        except ValueError as e:
            raise _po_error(e)
    else:
        await loop.run_in_executor(None, add_preorder_payment, preorder_id, req.amount, note, user["username"], req.method)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/convert", response_model=PreorderConvertResponse, status_code=201)
async def convert_to_receipt(
    preorder_id: str, req: PreorderConvertRequest, user: dict = Depends(get_current_user)
):
    """Sales Order -> nota. Takes the lines that have arrived, opens a nota (Siap Pembayaran)
    and applies the PO's unallocated DP to it once, through the allocation ledger."""
    loop = asyncio.get_event_loop()
    po = await loop.run_in_executor(None, get_preorder, preorder_id)
    if po is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    if po.get("receipt_id"):
        raise HTTPException(status_code=409, detail="PO ini sudah punya invoice. Buka invoicenya untuk pembayaran.")
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
