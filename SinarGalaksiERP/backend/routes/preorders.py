import asyncio
import base64
import binascii
import re
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from backend import payment_proofs
from backend.auth import get_current_user, require_admin
from backend.routes.quotations import guard_conversion, record_conversion
from backend.database import (
    decide_price_request,
    price_actor,
    peek_next_po_no,
    get_supplier,
    PREORDER_STATUSES,
    PO_LINE_CANCELLED,
    cancel_preorder,
    cancel_preorder_items,
    get_preorder_cancel_check,
    preview_preorder_cancel,
    add_preorder_payment,
    confirm_preorder_payment,
    fail_preorder_payment,
    get_receipt_payments,
    open_followup_invoice,
    po_invoice_payment,
    split_preorder_invoice,
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
    delete_preorder_draft,
    get_preorder,
    get_preorder_history,
    get_preorder_items,
    get_preorder_payments,
    parse_ymd,
    set_preorder_item_status,
    set_so_payment_proof,
    get_so_payment_proof,
    handover_preorder_item_partial,
    set_preorder_status,
    update_preorder,
)
from backend.schemas import (
    PreorderPartialHandoverRequest,
    PriceDecisionRequest,
    PaymentFailRequest,
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
    PreorderReasonRequest,
    PreorderSplitRequest,
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


def _check_proof(content_type: str, data: bytes) -> tuple[bytes, str]:
    if content_type not in payment_proofs.PROOF_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Format bukti transfer harus JPEG, PNG, WebP, atau PDF.")
    if not data:
        raise HTTPException(status_code=422, detail="File bukti transfer kosong.")
    if len(data) > payment_proofs.MAX_PROOF_BYTES:
        raise HTTPException(status_code=413, detail="Ukuran bukti transfer maksimal 5 MB.")
    if not payment_proofs.matches_content_type(content_type, data):
        raise HTTPException(status_code=422, detail="Isi file bukti tidak cocok dengan formatnya.")
    return data, content_type


def _decode_proof(raw: dict) -> tuple[bytes, str]:
    try:
        data = base64.b64decode(str(raw.get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="File bukti transfer rusak.")
    return _check_proof(str(raw.get("content_type") or "").lower(), data)


def _validate_plate(raw: str) -> str:
    # Stored upper-cased with single spaces, the way plates are read off a car.
    # Owner 25-10-2026: the plate may stay empty (WhatsApp / out-of-town orders); it can be filled in later.
    return " ".join((raw or "").upper().split())


def _require_invoice_plate(plate: str) -> None:
    """A published PO gets its invoice at once; a plate given must be one the cashier accepts (empty is fine)."""
    if plate and split_plate(plate) is None:
        raise HTTPException(status_code=422, detail="Plat mobil harus berformat seperti B 1234 ABC (kode wilayah, nomor, huruf).")


def _po_error(e: ValueError) -> HTTPException:
    msg = str(e)
    status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Stok", "Plat", "Uang muka", "Bukti", "Harga", "Alasan", "Ada harga")) else 409
    return HTTPException(status_code=status, detail=msg)


def _validate_po_date(raw: str) -> str:
    try:
        return parse_ymd(raw).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal Sales Order harus berformat YYYY-MM-DD.")


def _validate_status(status: str, allow_cancelled: bool = False) -> str:
    if status == PO_LINE_CANCELLED and allow_cancelled:
        return status
    if status not in PREORDER_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Status '{status}' tidak dikenal. Pilihan: {', '.join(PREORDER_STATUSES)}.",
        )
    return status


def _require_prices(items: list[dict]) -> None:
    """Owner 25-10-2026: a published Sales Order may not carry goods at Rp 0 (a free-text supplier line slipped
    through without a price). A Jasa line may be free; a draft may still be unpriced; cancelled lines are history."""
    unpriced = [i["product_name"] for i in items
                if (i.get("source") or "") != "jasa" and i.get("status") != "dibatalkan" and (i.get("unit_price") or 0) <= 0]
    if unpriced:
        raise HTTPException(status_code=422, detail=f"Harga jual belum diisi untuk: {', '.join(unpriced[:5])}"
                            + (f" dan {len(unpriced) - 5} barang lain" if len(unpriced) > 5 else "")
                            + ". Isi harganya, atau simpan sebagai draft dulu.")


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
        source=row.get("source"),
        supplier_id=row.get("supplier_id"),
        supplier_name=row.get("supplier_name"),
        reserved_qty=row.get("reserved_qty") or 0,
        coming_qty=row.get("coming_qty") or 0,
        shortfall_qty=row.get("shortfall_qty") or 0,
        cancelled_at=row.get("cancelled_at"),
        cancelled_by=row.get("cancelled_by"),
        cancel_kind=row.get("cancel_kind"),
        cancel_reason=row.get("cancel_reason"),
        note=row.get("note"),
        list_price=row.get("list_price"), price_state=row.get("price_state"), price_reason=row.get("price_reason"),
        price_requested_by=row.get("price_requested_by"), price_requested_at=row.get("price_requested_at"),
        price_approved_by=row.get("price_approved_by"), price_approved_at=row.get("price_approved_at"),
        price_below_cost=bool(row.get("price_below_cost")),
        master_price=row.get("master_price"), avg_cost_price=row.get("avg_cost_price") or 0,
    )


async def _detail(preorder_id: str) -> PreorderDetail:
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_preorder, preorder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    items = await loop.run_in_executor(None, get_preorder_items, preorder_id)
    history = await loop.run_in_executor(None, get_preorder_history, preorder_id)
    if row.get("receipt_id"):
        # Money is taken on the invoice; the PO shows the payments of all its invoices (K-19: usually one).
        many = len([i for i in row.get("invoices", []) if i["status"] != "void"]) > 1
        payments = []
        for inv in row.get("invoices", []):
            if inv["status"] == "void" and not many:
                continue
            for p in await loop.run_in_executor(None, get_receipt_payments, inv["id"]):
                payments.append({"id": p["id"], "paid_at": p["paid_at"], "amount": p["amount"], "note": p["note"],
                                 "created_by": p["created_by"], "method": p["method"], "kind": p.get("kind"),
                                 "state": p.get("state") or "sah", "confirmed_by": p.get("confirmed_by"),
                                 "confirmed_at": p.get("confirmed_at"), "fail_reason": p.get("fail_reason"),
                                 "receipt_no": inv["receipt_no"] if many else None, "receipt_id": inv["id"],
                                 "has_proof": bool(p.get("proof_file")), "proof_name": p.get("proof_name")})
        payments.sort(key=lambda x: (x["paid_at"], x["id"]))
    else:
        payments = [{**p, "has_proof": bool(p.get("proof_file"))}
                    for p in await loop.run_in_executor(None, get_preorder_payments, preorder_id)]
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
    if not req.is_draft:
        _require_prices(items)
    if req.deposit > sum(i["quantity"] * i["unit_price"] for i in items):
        raise HTTPException(status_code=422, detail="Uang muka melebihi total pesanan.")
    notes = req.notes.strip() if req.notes else None

    preorder_id = str(uuid4())
    loop = asyncio.get_event_loop()
    await _require_supplier(loop, req.supplier_id)
    await guard_conversion(req.quotation_id)
    proof = None
    if req.deposit_proof and req.deposit > 0 and req.deposit_method in ("transfer", "qris"):
        proof = (await loop.run_in_executor(None, payment_proofs.save_proof, *_decode_proof(req.deposit_proof)),
                 str(req.deposit_proof.get("name") or "")[:120] or None)
    try:
        await loop.run_in_executor(
            None, lambda: create_preorder(
                preorder_id, customer_name, customer_phone, items, req.deposit, notes, user["username"],
                plate_full, po_date, req.is_draft, deposit_method=req.deposit_method, supplier_id=req.supplier_id,
                deposit_confirmed=req.deposit_confirmed, price_actor=price_actor(user), order_source=req.order_source,
                deposit_proof=proof,
            ),
        )
    except ValueError as e:
        if proof:
            await loop.run_in_executor(None, payment_proofs.delete_proof_file, proof[0])
        raise _po_error(e)
    detail = await _detail(preorder_id)
    await record_conversion(req.quotation_id, "so", preorder_id, detail.po_number, user)
    return detail


@router.get("/preorders", response_model=list[PreorderOut])
async def list_preorders(query: str | None = None, status: str | None = None, draft: bool = False,
                         include_draft: bool = False):
    if status:
        _validate_status(status, allow_cancelled=True)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_all_preorders, query, status, None if include_draft else draft)
    return [PreorderOut(**r) for r in rows]


@router.get("/preorders/next-number")
async def preorder_next_number(po_date: str | None = None):
    """Preview of the automatic SO number for the form; the real number is assigned when saved."""
    if po_date:
        try:
            date.fromisoformat(po_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Tanggal tidak valid, gunakan format YYYY-MM-DD.")
    return {"po_number": await asyncio.get_event_loop().run_in_executor(None, peek_next_po_no, po_date or None)}


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
    if not will_be_draft and items is not None:
        _require_prices(items)
    elif not will_be_draft and current["is_draft"]:  # publishing a saved draft as it is
        _require_prices(await loop.run_in_executor(None, get_preorder_items, preorder_id))
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
                set_supplier=set_supplier, supplier_id=req.supplier_id, price_actor=price_actor(user),
                order_source=req.order_source,
            ),
        )
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


async def _price_decision(preorder_id: str, item_id: int, approve: bool, req: PriceDecisionRequest, user: dict) -> PreorderDetail:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, decide_price_request, "preorder", preorder_id, item_id, approve, req.note, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/items/{item_id}/price/approve", response_model=PreorderDetail)
async def approve_preorder_price(preorder_id: str, item_id: int, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """K-22 (admin): approve a special price a staff member asked for on this Sales Order line."""
    return await _price_decision(preorder_id, item_id, True, req, user)


@router.post("/preorders/{preorder_id}/items/{item_id}/price/reject", response_model=PreorderDetail)
async def reject_preorder_price(preorder_id: str, item_id: int, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """K-22 (admin): refuse it; the line goes back to the Master Barang price (the invoice follows)."""
    return await _price_decision(preorder_id, item_id, False, req, user)


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


@router.post("/preorders/{preorder_id}/items/{item_id}/handover-partial", response_model=PreorderDetail)
async def handover_preorder_item_partial_route(
    preorder_id: str, item_id: int, req: PreorderPartialHandoverRequest, user: dict = Depends(get_current_user)
):
    """UAT-010: Serah Terima Sebagian, part of a line goes to Terpasang and the rest waits."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_preorder, preorder_id) is None:
        raise HTTPException(status_code=404, detail="Preorder tidak ditemukan.")
    try:
        await loop.run_in_executor(None, handover_preorder_item_partial, preorder_id, item_id, req.qty, user["username"])
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


@router.put("/preorders/{preorder_id}/payments/{payment_id}/proof", response_model=PreorderDetail)
async def put_so_payment_proof(preorder_id: str, payment_id: int, request: Request, src: str = "nota", name: str | None = None,
                               user: dict = Depends(get_current_user)):
    """Raw-body upload of a payment's transfer proof (JPEG / PNG / WebP / PDF, 5 MB). src = nota (an invoice payment)
    or so (the Sales Order's own, before it has an invoice)."""
    loop = asyncio.get_event_loop()
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > payment_proofs.MAX_PROOF_BYTES:
            raise HTTPException(status_code=413, detail="Ukuran bukti transfer maksimal 5 MB.")
    body, ctype = _check_proof(request.headers.get("content-type", "").split(";")[0].strip().lower(), bytes(data))
    filename = await loop.run_in_executor(None, payment_proofs.save_proof, body, ctype)
    try:
        previous = await loop.run_in_executor(None, set_so_payment_proof, preorder_id, payment_id, src != "so", filename, name, user["username"])
    except ValueError as e:
        await loop.run_in_executor(None, payment_proofs.delete_proof_file, filename)
        raise HTTPException(status_code=404 if "tidak ditemukan" in str(e) else 422, detail=str(e))
    await loop.run_in_executor(None, payment_proofs.delete_proof_file, previous)
    return await _detail(preorder_id)


@router.get("/preorders/{preorder_id}/payments/{payment_id}/proof")
async def get_so_payment_proof_route(preorder_id: str, payment_id: int, src: str = "nota"):
    loop = asyncio.get_event_loop()
    stored = await loop.run_in_executor(None, get_so_payment_proof, preorder_id, payment_id, src != "so")
    path = await loop.run_in_executor(None, payment_proofs.proof_file, stored)
    if path is None:
        raise HTTPException(status_code=404, detail="Pembayaran ini belum punya bukti transfer.")
    return FileResponse(path, media_type=payment_proofs.media_type_for(stored),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"})


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
            preorder_id, req.reason, req.refund_amount, req.refund_method, user["username"], req.item_ids, req.refund_proof))
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
            preorder_id, req.item_ids, req.category, req.reason, req.refund_method, user["username"], req.refund_proof))
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
    remaining = row["remaining"]
    if req.receipt_id and row.get("invoice_count", 1) > 1:
        inv = next((i for i in row.get("invoices", []) if i["id"] == req.receipt_id and i["status"] != "void"), None)
        if inv is None:
            raise HTTPException(status_code=422, detail="Invoice itu bukan milik Sales Order ini.")
        remaining = max(inv["total"] - inv["amount_paid"], 0)
    elif row.get("invoice_count", 1) > 1:
        raise HTTPException(status_code=422, detail="Sales Order ini punya beberapa invoice. Pilih invoice yang menerima pembayaran.")
    if req.amount > remaining:
        raise HTTPException(
            status_code=422,
            detail=f"Pembayaran melebihi sisa tagihan (sisa Rp {remaining:,}).".replace(",", "."),
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
                req.receipt_id, req.confirmed,
            )
        except ValueError as e:
            raise _po_error(e)
    else:
        await loop.run_in_executor(None, add_preorder_payment, preorder_id, req.amount, note, user["username"], req.method, req.confirmed)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/payments/{payment_id}/confirm", response_model=PreorderDetail)
async def confirm_draft_payment(preorder_id: str, payment_id: int, user: dict = Depends(get_current_user)):
    """K-18: a draft order's transfer/QRIS DP has been seen in the account."""
    try:
        await asyncio.get_event_loop().run_in_executor(None, confirm_preorder_payment, preorder_id, payment_id, user["username"])
    except ValueError as e:
        raise HTTPException(status_code=404 if "tidak ditemukan" in str(e) else 409, detail=str(e))
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/payments/{payment_id}/fail", response_model=PreorderDetail, dependencies=[Depends(require_admin)])
async def fail_draft_payment(preorder_id: str, payment_id: int, req: PaymentFailRequest, user: dict = Depends(get_current_user)):
    try:
        await asyncio.get_event_loop().run_in_executor(None, fail_preorder_payment, preorder_id, payment_id, req.reason, user["username"])
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "tidak ditemukan" in msg else 422 if msg.startswith("Alasan") else 409, detail=msg)
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
        raise HTTPException(status_code=409, detail="Sales Order ini sudah punya invoice. Buka invoicenya untuk pembayaran.")
    lines = await loop.run_in_executor(None, preorder_convertible_items, preorder_id, req.item_ids)
    if req.item_ids is not None and len(lines) != len(set(req.item_ids)):
        raise HTTPException(status_code=409, detail="Ada item yang belum sampai di toko atau sudah masuk nota.")
    if not lines:
        raise HTTPException(status_code=409, detail="Belum ada barang Sales Order yang sampai di toko dan belum masuk nota.")

    parts = (po["plate_full"] or "").split()
    region = (req.plate_region or (parts[0] if len(parts) == 3 else "")).upper()
    number = req.plate_number or (parts[1] if len(parts) == 3 else "")
    suffix = (req.plate_suffix or (parts[2] if len(parts) == 3 else "")).upper()
    if not (region and number and suffix):
        raise HTTPException(status_code=422, detail="Plat nomor belum ada di Sales Order ini. Isi plat untuk membuat nota.")
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


# DELETE only removes a draft that never took money (owner 25-10-2026). Anything
# else is archived via PATCH {"is_draft": true} or cancelled, never deleted.
@router.delete("/preorders/{preorder_id}", status_code=204, response_class=Response)
async def delete_preorder_route(preorder_id: str, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_preorder_draft, preorder_id)
    except ValueError as e:
        raise _po_error(e)
    return Response(status_code=204)


# ---- K-19: Sales Order with several invoices (admin) ----

@router.post("/preorders/{preorder_id}/invoices/split", response_model=PreorderDetail)
async def split_invoice_route(preorder_id: str, req: PreorderSplitRequest, user: dict = Depends(require_admin)):
    """Moves lines that are not installed yet to a new Invoice Lanjutan; their DP follows."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, split_preorder_invoice, preorder_id, req.item_ids, req.reason, req.dp_move, user["username"])
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)


@router.post("/preorders/{preorder_id}/invoices/followup", response_model=PreorderDetail)
async def followup_invoice_route(preorder_id: str, req: PreorderReasonRequest, user: dict = Depends(require_admin)):
    """An empty Invoice Lanjutan, for lines added after the older invoices were closed."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, open_followup_invoice, preorder_id, req.reason, user["username"])
    except ValueError as e:
        raise _po_error(e)
    return await _detail(preorder_id)
