import asyncio
import re
from datetime import date
from uuid import uuid4

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from backend.auth import get_current_user, require_admin
from backend.product_images import matches_content_type
from backend.routes.quotations import guard_conversion, record_conversion
from backend.database import (
    merge_followup_invoice,
    confirm_receipt_payment,
    hand_over_receipt,
    peek_next_receipt_no,
    get_handover_file,
    get_handover_files,
    set_work_stage,
    undo_handover,
    fail_receipt_payment,
    list_pending_payments,
    refund_receipt_overpay,
    reopen_closed_receipt,
    return_receipt_item,
    get_receipt_returns,
    reverse_receipt_payment,
    get_receipt_preorders,
    PHONE_RE,
    RECEIPT_PAYMENT_CLAUSES,
    RECEIPT_SORTS,
    RECEIPT_WORK_STATUSES,
    count_receipts,
    create_receipt,
    decide_discount_request,
    decide_price_request,
    price_actor,
    find_archived_skus,
    delete_receipt,
    delete_transfer_name,
    list_transfer_names,
    get_all_receipts,
    get_customer,
    get_receipt,
    add_receipt_payment,
    change_receipt_work_status,
    get_receipt_events,
    get_receipt_items,
    get_receipt_payments,
    get_receipt_summary,
    normalize_phone,
    parse_ymd,
    settle_receipt,
    update_receipt,
)
from backend.schemas import (
    PriceDecisionRequest,
    ReceiptHandoverRequest,
    ReceiptWorkStageRequest,
    PaymentFailRequest,
    ReceiptPaymentReverseRequest,
    ReceiptRefundRequest,
    ReceiptReopenRequest,
    ReceiptReturnOut,
    ReceiptReturnRequest,
    VALID_PAYMENT_METHODS,
    ReceiptDetail,
    ReceiptEventOut,
    ReceiptExtra,
    ReceiptItemOut,
    ReceiptItemRequest,
    ReceiptOut,
    ReceiptPaymentOut,
    ReceiptPaymentRequest,
    ReceiptRequest,
    ReceiptResponse,
    ReceiptSettleRequest,
    ReceiptUpdateRequest,
    ReceiptWorkStatusRequest,
    TransferSenderOut,
)
from plate_codes import PLATE_CODES

router = APIRouter()

NUMBER_RE = re.compile(r"^\d{1,4}$")
SUFFIX_RE = re.compile(r"^[A-Za-z]{1,3}$")

# GET /receipts returned every receipt, unbounded. This default keeps a bare call
# generous for a shop's history while no longer growing without limit; the real
# match count is in the X-Total-Count header.
DEFAULT_RECEIPT_LIMIT = 200
MAX_RECEIPT_LIMIT = 1000


def _clean_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _has_installation(items: list[dict] | None) -> bool:
    """A nota with work on the car (a line with a technician) needs the plate and the customer's phone once that work
    starts; a walk-in purchase (owner, 21-10-2026: "orang umum", motor, belanja biasa) may leave both empty, and so
    may a Draft or a Nota saved with only a DP (owner, 22-10-2026)."""
    return any((i.get("_assign") or {}).get("technician_ids") for i in items or [])


def _check_plate(region: str | None, number: str | None, suffix: str | None, required: bool) -> tuple[str, str, str]:
    region, number, suffix = (region or "").strip().upper(), (number or "").strip(), (suffix or "").strip().upper()
    if not (region or number or suffix):
        if required:
            raise HTTPException(status_code=422, detail="No. plat wajib diisi untuk mulai pengerjaan (ada barang yang dipasang teknisi).")
        return "", "", ""
    if region not in PLATE_CODES:
        raise HTTPException(status_code=422, detail=f"Kode wilayah plat tidak dikenal: {region or '-'}")
    if not NUMBER_RE.match(number):
        raise HTTPException(status_code=422, detail="Nomor plat harus 1-4 digit.")
    if not SUFFIX_RE.match(suffix):
        raise HTTPException(status_code=422, detail="Huruf belakang plat harus 1-3 huruf.")
    return region, number, suffix


def _validate_items(items: list[ReceiptItemRequest]) -> list[dict]:
    validated: list[dict] = []
    for item in items:
        if item.quantity <= 0:
            raise HTTPException(status_code=422, detail="Jumlah harus berupa bilangan bulat positif.")
        if item.unit_price < 0:
            raise HTTPException(status_code=422, detail="Harga satuan harus >= 0.")
        if item.item_type not in ("barang", "jasa"):
            raise HTTPException(status_code=422, detail="Jenis item harus barang atau jasa.")
        if item.line_discount < 0 or item.line_discount > item.quantity * item.unit_price:
            raise HTTPException(status_code=422, detail="Diskon item tidak boleh melebihi harga item.")
        if not item.product_name.strip():
            raise HTTPException(status_code=422, detail="Nama produk wajib diisi.")
        if item.warranty_date:
            try:
                date.fromisoformat(item.warranty_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Tanggal garansi tidak valid, gunakan format YYYY-MM-DD.")
        row = item.model_dump()
        row["serial_no"] = (row.get("serial_no") or "").strip().upper()[:80] or None
        # Only the assignment fields the client actually sent are applied (see database._assignments).
        row["_assign"] = {k: row.pop(k) for k in ("technician_ids", "sales_employee_id") if k in item.model_fields_set}
        row.pop("technician_ids", None)
        row.pop("sales_employee_id", None)
        validated.append(row)
    return validated


async def _reject_archived(loop, items: list[dict], only_new: bool) -> None:
    """A product that has been archived stays on old notas but cannot be sold again."""
    skus = {i["sku"] for i in items if i.get("sku") and i.get("item_type") != "jasa" and not (only_new and i.get("id"))}
    bad = await loop.run_in_executor(None, find_archived_skus, sorted(skus)) if skus else []
    if bad:
        raise HTTPException(status_code=422, detail=f"Barang {', '.join(bad)} sudah diarsipkan dan tidak bisa dijual. Aktifkan kembali di Master Barang.")


def _row_to_item(row: dict) -> ReceiptItemOut:
    return ReceiptItemOut(
        id=row["id"],
        product_name=row["product_name"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        warranty_date=row["warranty_date"],
        serial_no=row.get("serial_no"),
        sku=row.get("sku"),
        unit=row.get("unit"),
        item_type=row.get("item_type") or "barang",
        line_discount=row.get("line_discount") or 0,
        added_at=row.get("added_at"),
        added_by=row.get("added_by"),
        added_during_work=bool(row.get("added_during_work")),
        preorder_item_id=row.get("preorder_item_id"),
        returned_qty=row.get("returned_qty") or 0,
        list_price=row.get("list_price"), price_state=row.get("price_state"), price_reason=row.get("price_reason"),
        price_requested_by=row.get("price_requested_by"), price_requested_at=row.get("price_requested_at"),
        price_approved_by=row.get("price_approved_by"), price_approved_at=row.get("price_approved_at"),
        price_below_cost=bool(row.get("price_below_cost")),
        master_price=row.get("master_price"), avg_cost_price=row.get("avg_cost_price") or 0,
        technicians=row.get("technicians") or [], sales_employee_id=row.get("sales_employee_id"),
        sales_employee_name=row.get("sales_employee_name"), work_status=row.get("work_status"), work_done_at=row.get("work_done_at"),
    )


EXTRA_MAX = {"work_notes": 1000, "cashier_name": 80, "estimated_finish_at": 40, "vehicle_make": 60,
             "vehicle_model": 60, "vehicle_color": 40, "vehicle_year": 8, "remarks": 500}


def _override_reason(req, user: dict) -> str | None:
    """Using stock reserved for a Sales Order customer needs an admin and a reason."""
    reason = _clean_name(getattr(req, "stock_override_reason", None))
    if not reason:
        return None
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang boleh memakai stok yang dicadangkan.")
    if len(reason) < 3 or len(reason) > 200:
        raise HTTPException(status_code=422, detail="Alasan izin stok 3-200 karakter.")
    return reason


def _clean_extra(req: ReceiptExtra, only_sent: bool) -> dict:
    """The work-order fields, trimmed and length-checked. On edit only the fields the client
    actually sent are returned, so an omitted field is left alone while an explicit null clears it."""
    out: dict = {}
    for key, limit in EXTRA_MAX.items():
        if only_sent and key not in req.model_fields_set:
            continue
        value = _clean_name(getattr(req, key))
        if value and len(value) > limit:
            raise HTTPException(status_code=422, detail=f"{key} maksimal {limit} karakter.")
        out[key] = value
    if not only_sent or "sales_employee_id" in req.model_fields_set:
        out["sales_employee_id"] = req.sales_employee_id
    return out


def _clean_sender(raw: str | None) -> str | None:
    name = " ".join((raw or "").split())
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="Nama pengirim / rekening maksimal 100 karakter.")
    return name or None


def _refuse_if_paid(receipt: dict) -> None:
    """A paid (Lunas) nota is final: it has been printed and handed over, so it is never
    edited, voided or deleted afterwards."""
    if receipt["status"] == "done":
        raise HTTPException(status_code=409, detail="Nota sudah lunas dan tidak bisa diubah lagi.")


def _refuse_if_closed(receipt: dict) -> None:
    """Lunas + Pengerjaan Selesai closes the transaction. A nota that is paid but still being
    worked on (or worked on but unpaid) stays editable, so extras can still be added."""
    if receipt["status"] == "done" and receipt["work_status"] == "selesai":
        raise HTTPException(status_code=409, detail="Nota sudah lunas dan selesai (ditutup), tidak bisa diubah lagi.")


async def _require_customer(loop, customer_id: int, allow_inactive: bool = False) -> None:
    customer = await loop.run_in_executor(None, get_customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=422, detail="Pelanggan tidak ditemukan.")
    if not customer["is_active"] and not allow_inactive:
        raise HTTPException(
            status_code=422,
            detail="Pelanggan ini nonaktif. Aktifkan kembali di Master Pelanggan atau pilih pelanggan lain.",
        )


@router.post("/receipts", response_model=ReceiptResponse, status_code=201)
async def create_new_receipt(req: ReceiptRequest, user: dict = Depends(get_current_user)):
    if not req.items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu item.")
    if req.status is not None and req.status != "void":
        raise HTTPException(
            status_code=422,
            detail="Status hanya boleh diatur ke 'void' — pending/done dihitung otomatis dari amount_paid dibanding total.",
        )
    if req.amount_paid < 0:
        raise HTTPException(status_code=422, detail="Jumlah dibayar harus >= 0.")
    if req.work_status not in ("draft", "belum", "pengerjaan", "selesai"):
        raise HTTPException(status_code=422, detail="work_status harus draft, belum, pengerjaan atau selesai.")
    if req.work_status == "draft" and req.amount_paid > 0:
        raise HTTPException(status_code=422, detail="Nota Draft belum bisa menerima pembayaran.")
    if req.payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )

    loop = asyncio.get_event_loop()
    # The phone is required unless a master customer is picked, in which case it
    # defaults to that customer's number (see _resolve_receipt_customer).
    customer_phone = normalize_phone(req.customer_phone)
    installing = _has_installation(_validate_items(req.items)) and req.work_status in ("pengerjaan", "selesai")
    plate_region, plate_number, plate_suffix = _check_plate(req.plate_region, req.plate_number, req.plate_suffix, installing)
    if not customer_phone and req.customer_id is None and installing:
        raise HTTPException(status_code=422, detail="Nomor HP wajib diisi untuk mulai pengerjaan (ada barang yang dipasang teknisi).")
    if customer_phone and not PHONE_RE.match(customer_phone):
        raise HTTPException(status_code=422, detail="Nomor HP harus 7-15 digit (boleh diawali tanda +).")
    customer_name = _clean_name(req.customer_name)
    if req.customer_id is not None:
        await _require_customer(loop, req.customer_id)

    items = _validate_items(req.items)
    await _reject_archived(loop, items, only_new=False)

    if req.discount < 0:
        raise HTTPException(status_code=422, detail="Diskon harus >= 0.")

    subtotal = sum(i["quantity"] * i["unit_price"] - i["line_discount"] for i in items)
    if req.discount > subtotal:
        raise HTTPException(status_code=422, detail="Diskon tidak boleh melebihi subtotal.")
    # A customer may pay in part: the rest is settled later with Tambah Pembayaran. What is
    # paid now can be anything from nothing up to the total, never more.
    if req.status != "void" and req.amount_paid > subtotal - req.discount:
        raise HTTPException(status_code=422, detail="Jumlah dibayar tidak boleh melebihi total.")
    transfer_sender = _clean_sender(req.transfer_sender)
    transfer_account = _clean_sender(req.transfer_account)
    takes_money_now = req.amount_paid > 0 and req.status != "void"
    if req.payment_method == "transfer" and takes_money_now and not transfer_sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")
    if req.payment_method == "transfer" and takes_money_now and not transfer_account:
        raise HTTPException(status_code=422, detail="Rekening penerima transfer wajib dipilih.")

    await guard_conversion(req.quotation_id)
    receipt_id = str(uuid4())
    try:
        result = await loop.run_in_executor(
            None, create_receipt, receipt_id, plate_region, plate_number, plate_suffix, items, req.discount,
            req.amount_paid, req.status, customer_phone, customer_name, req.started_at, req.payment_method,
            req.customer_id, transfer_sender, transfer_account, user["username"], req.work_status,
            _clean_extra(req, False), _override_reason(req, user), req.payment_confirmed, price_actor(user),
            req.discount_reason, req.allow_discount_below_cost, req.allow_discount_stack,
        )
    except ValueError as e:
        # The customer was deleted between the check above and the save.
        raise HTTPException(status_code=422, detail=str(e))
    await record_conversion(req.quotation_id, "kasir", receipt_id, result.get("receipt_no"), user)
    return ReceiptResponse(receipt_id=receipt_id, **result)


def _optional_date(raw: str | None, field: str) -> str | None:
    """A blank value means 'not filtering'; anything else must be a real YYYY-MM-DD."""
    if raw is None or not raw.strip():
        return None
    try:
        parse_ymd(raw.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} tidak valid, gunakan format YYYY-MM-DD.")
    return raw.strip()


@router.get("/receipts", response_model=list[ReceiptOut])
async def list_receipts(
    response: Response,
    plate: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    q: str | None = None,
    limit: int = DEFAULT_RECEIPT_LIMIT,
    tz_offset: int | None = None,
    payment: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    work: str | None = None,
):
    """Newest first. Filters combine with AND; `q` alone is the one-box search
    (plate OR customer name OR phone). date_from/date_to are inclusive calendar
    days on the shop's clock -- see SHOP_UTC_OFFSET_MINUTES in backend/config.py;
    tz_offset (minutes east of UTC, e.g. 420) overrides it for this request so a
    browser can ask for exactly its own local days."""
    date_from = _optional_date(date_from, "date_from")
    date_to = _optional_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from tidak boleh setelah date_to.")
    if limit < 1 or limit > MAX_RECEIPT_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit harus antara 1 dan {MAX_RECEIPT_LIMIT}.")
    if tz_offset is not None and not -720 <= tz_offset <= 840:
        raise HTTPException(status_code=422, detail="tz_offset harus antara -720 dan 840 menit.")
    if payment is not None and payment not in RECEIPT_PAYMENT_CLAUSES:
        raise HTTPException(
            status_code=422, detail=f"payment harus salah satu dari: {', '.join(RECEIPT_PAYMENT_CLAUSES)}."
        )
    if work is not None and work not in RECEIPT_WORK_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"work harus salah satu dari: {', '.join(RECEIPT_WORK_STATUSES)}."
        )
    if sort not in RECEIPT_SORTS:
        raise HTTPException(status_code=422, detail=f"sort harus salah satu dari: {', '.join(RECEIPT_SORTS)}.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset tidak boleh negatif.")

    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(
        None,
        lambda: get_all_receipts(
            plate, status, date_from, date_to, customer, q, limit, tz_offset,
            payment=payment, sort=sort, offset=offset, work=work,
        ),
    )
    total = await loop.run_in_executor(
        None, lambda: count_receipts(plate, status, date_from, date_to, customer, q, tz_offset, payment, work)
    )
    response.headers["X-Total-Count"] = str(total)
    return [ReceiptOut(**r) for r in rows]


@router.get("/receipts/next-number")
async def next_receipt_number():
    """Preview of the automatic nota number (PJSG/YY/MM/NNNN) for a new nota."""
    return {"receipt_no": await asyncio.get_event_loop().run_in_executor(None, peek_next_receipt_no)}


@router.get("/receipts/summary")
async def receipt_summary(tz_offset: int | None = None):
    """Counts for the Riwayat Invoice header tiles. Declared before /receipts/{receipt_id}
    so "summary" is not read as a receipt id."""
    if tz_offset is not None and not -720 <= tz_offset <= 840:
        raise HTTPException(status_code=422, detail="tz_offset harus antara -720 dan 840 menit.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_receipt_summary, tz_offset)


@router.get("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def get_receipt_detail(receipt_id: str):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_receipt, receipt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**row, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.patch("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def update_receipt_route(receipt_id: str, req: ReceiptUpdateRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    existing = await loop.run_in_executor(None, get_receipt, receipt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    if req.expected_revision and existing.get("revision") and req.expected_revision != existing["revision"]:
        raise HTTPException(
            status_code=409,
            detail="Nota ini baru saja diubah oleh pengguna lain. Muat ulang nota lalu ulangi perubahan Anda.",
        )
    void_reason = (req.void_reason or "").strip()
    if req.status == "void":
        # Cancelling a nota is audited: a reason is always required, and once money has been taken
        # (including a Lunas nota that was already handed over) only an admin may do it. The payment
        # is reversed on the record and the event log keeps who, when, why and how much to give back.
        if existing["status"] != "void":
            if len(void_reason) < 3:
                raise HTTPException(status_code=422, detail="Alasan void wajib diisi (minimal 3 karakter).")
            # A closed nota (Lunas + Pengerjaan Selesai) is final; an admin reopens it first, which is logged.
            if existing["status"] == "done" and existing.get("work_status") == "selesai":
                raise HTTPException(status_code=409, detail="Nota sudah ditutup (Lunas + Pengerjaan Selesai). Buka Kembali dulu (admin) sebelum void.")
            # K-22: void is an admin action. Staff may only discard a Draft nota (no stock taken, no money).
            if user.get("role") != "admin" and (existing.get("work_status") != "draft" or (existing["amount_paid"] or 0) > 0):
                raise HTTPException(status_code=403, detail="Hanya admin yang boleh mem-void nota. Staff hanya bisa membatalkan nota Draft yang belum dibayar.")
    else:
        _refuse_if_closed(existing)

    installing = (_has_installation(_validate_items(req.items)) if req.items else False) \
        and existing.get("work_status") in ("pengerjaan", "selesai")
    plate_region: str | None = None
    plate_suffix: str | None = None
    plate_number: str | None = None
    if req.plate_region is not None or req.plate_number is not None or req.plate_suffix is not None:
        plate_region, plate_number, plate_suffix = _check_plate(
            req.plate_region if req.plate_region is not None else existing["plate_region"],
            req.plate_number if req.plate_number is not None else existing["plate_number"],
            req.plate_suffix if req.plate_suffix is not None else existing["plate_suffix"], installing)

    if req.items is not None and not req.items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu item.")
    if req.status is not None and req.status != "void":
        raise HTTPException(
            status_code=422,
            detail="Status hanya boleh diatur ke 'void' — pending/done dihitung otomatis dari amount_paid dibanding total.",
        )
    if req.discount is not None and req.discount < 0:
        raise HTTPException(status_code=422, detail="Diskon harus >= 0.")
    if existing["work_status"] == "draft" and (req.amount_paid or 0) > 0:
        raise HTTPException(status_code=422, detail="Nota Draft belum bisa menerima pembayaran.")
    if req.amount_paid is not None and req.amount_paid < 0:
        raise HTTPException(status_code=422, detail="Jumlah dibayar harus >= 0.")
    if req.payment_method is not None and req.payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )

    customer_phone: str | None = None
    if req.customer_phone is not None:
        customer_phone = normalize_phone(req.customer_phone)
        if not customer_phone:
            if req.customer_id is None and installing:
                raise HTTPException(status_code=422, detail="Nomor HP wajib diisi untuk mulai pengerjaan (ada barang yang dipasang teknisi).")
            # Blank next to a picked customer: take that customer's number. Blank on a walk-in nota: no phone.
            customer_phone = None if req.customer_id is not None else ""
        elif not PHONE_RE.match(customer_phone):
            raise HTTPException(status_code=422, detail="Nomor HP harus 7-15 digit (boleh diawali tanda +).")
    if req.customer_id is not None:
        # Echoing back the receipt's current customer is always fine, even if they
        # have since been deactivated; only picking a *different* one is checked.
        await _require_customer(loop, req.customer_id, allow_inactive=req.customer_id == existing["customer_id"])
    # name is nullable: only touch it if the client actually sent the field (fields_set),
    # so an omitted field leaves the stored name intact while an explicit null clears it.
    update_customer_name = "customer_name" in req.model_fields_set
    customer_name = _clean_name(req.customer_name) if update_customer_name else None

    items: list[dict] | None = None
    if req.items is not None:
        items = _validate_items(req.items)
        await _reject_archived(loop, items, only_new=True)
        effective_subtotal = sum(i["quantity"] * i["unit_price"] - i["line_discount"] for i in items)
    elif req.discount is not None:
        existing_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
        effective_subtotal = sum(i["quantity"] * i["unit_price"] - (i.get("line_discount") or 0) for i in existing_items)
    else:
        effective_subtotal = None

    if effective_subtotal is not None:
        effective_discount = req.discount if req.discount is not None else existing["discount"]
        if effective_discount > effective_subtotal:
            raise HTTPException(status_code=422, detail="Diskon tidak boleh melebihi subtotal.")

    update_transfer_sender = "transfer_sender" in req.model_fields_set
    transfer_sender = _clean_sender(req.transfer_sender) if update_transfer_sender else None
    effective_method = req.payment_method or existing["payment_method"]
    effective_sender = transfer_sender if update_transfer_sender else existing.get("transfer_sender")
    # Sender / account are asked only when this save records money; editing items of a nota paid by a confirmed
    # transfer (Pembayaran Nota) must not trip over the nota's stored payment fields.
    changes_payment = req.payment_method is not None or (req.amount_paid is not None and req.amount_paid != (existing["amount_paid"] or 0))
    if effective_method == "transfer" and req.status != "void" and changes_payment and not effective_sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")
    update_transfer_account = "transfer_account" in req.model_fields_set
    transfer_account = _clean_sender(req.transfer_account) if update_transfer_account else None
    effective_account = transfer_account if update_transfer_account else existing.get("transfer_account")
    if effective_method == "transfer" and req.status != "void" and changes_payment and not effective_account:
        raise HTTPException(status_code=422, detail="Rekening penerima transfer wajib dipilih.")

    try:
        updated = await loop.run_in_executor(
            None,
            update_receipt,
            receipt_id,
            plate_region,
            plate_number,
            plate_suffix,
            items,
            req.discount,
            req.amount_paid,
            req.status,
            customer_phone,
            customer_name,
            update_customer_name,
            req.payment_method,
            req.customer_id,
            transfer_sender,
            update_transfer_sender,
            transfer_account,
            update_transfer_account,
            user["username"],
            _clean_extra(req, True),
            void_reason or None,
            _override_reason(req, user),
            req.payment_confirmed,
            req.refund_method,
            req.refund_proof,
            price_actor(user),
            req.discount_reason,
            req.allow_discount_below_cost,
            req.allow_discount_stack,
        )
    except ValueError as e:
        # The customer was deleted between the check above and the save, or the new total
        # would drop below what has already been paid.
        raise HTTPException(status_code=422, detail=str(e))
    result_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(result_items), items=[_row_to_item(i) for i in result_items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


async def _price_decision(receipt_id: str, item_id: int, approve: bool, req: PriceDecisionRequest, user: dict) -> ReceiptDetail:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, decide_price_request, "receipt", receipt_id, item_id, approve, req.note, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    receipt = await loop.run_in_executor(None, get_receipt, receipt_id)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**receipt, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


async def _discount_decision(receipt_id: str, approve: bool, req: PriceDecisionRequest, user: dict) -> ReceiptDetail:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, decide_discount_request, receipt_id, approve, req.note, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    receipt = await loop.run_in_executor(None, get_receipt, receipt_id)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**receipt, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/discount/approve", response_model=ReceiptDetail)
async def approve_receipt_discount(receipt_id: str, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """Diskon nota (admin): approve the discount a staff member asked for on this nota."""
    return await _discount_decision(receipt_id, True, req, user)


@router.post("/receipts/{receipt_id}/discount/reject", response_model=ReceiptDetail)
async def reject_receipt_discount(receipt_id: str, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """Diskon nota (admin): reject it; the nota goes back to its last approved discount."""
    return await _discount_decision(receipt_id, False, req, user)


@router.post("/receipts/{receipt_id}/items/{item_id}/price/approve", response_model=ReceiptDetail)
async def approve_receipt_price(receipt_id: str, item_id: int, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """K-22 (admin): approve a special price a staff member asked for on this nota."""
    return await _price_decision(receipt_id, item_id, True, req, user)


@router.post("/receipts/{receipt_id}/items/{item_id}/price/reject", response_model=ReceiptDetail)
async def reject_receipt_price(receipt_id: str, item_id: int, req: PriceDecisionRequest, user: dict = Depends(get_current_user)):
    """K-22 (admin): refuse it; the line goes back to the Master Barang price and the bill is recomputed."""
    return await _price_decision(receipt_id, item_id, False, req, user)


@router.post("/receipts/{receipt_id}/settle", response_model=ReceiptDetail)
async def settle_receipt_route(receipt_id: str, req: ReceiptSettleRequest, user: dict = Depends(get_current_user)):
    """'Lunasi' (K-07): record the remaining balance of a pending receipt as paid.
    Only the payment columns change -- never items, totals, stock or the work status (Pengerjaan)."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    payment_method = req.payment_method
    if payment_method is not None and payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )
    try:
        updated = await loop.run_in_executor(None, settle_receipt, receipt_id, payment_method, req.amount, user["username"], req.confirmed)
    except ValueError as e:
        # Deleted between the check above and the settle -> 404; wrong amount -> 422;
        # void/already done -> 409.
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith("Jumlah") else 409
        raise HTTPException(status_code=status, detail=msg)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.get("/receipts/{receipt_id}/payments", response_model=list[ReceiptPaymentOut])
async def list_receipt_payments(receipt_id: str):
    """Riwayat Pembayaran: every payment taken against the nota, oldest first."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return [ReceiptPaymentOut(**p) for p in await loop.run_in_executor(None, get_receipt_payments, receipt_id)]


@router.post("/receipts/{receipt_id}/payments", response_model=ReceiptDetail, status_code=201)
async def add_receipt_payment_route(
    receipt_id: str, req: ReceiptPaymentRequest, user: dict = Depends(get_current_user)
):
    """'Pembayaran Nota': take a full or partial payment against a pending nota. Never
    touches items, totals or stock."""
    loop = asyncio.get_event_loop()
    if req.method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )
    note = _clean_name(req.note)
    if note and len(note) > 200:
        raise HTTPException(status_code=422, detail="Catatan pembayaran maksimal 200 karakter.")
    sender = _clean_sender(req.transfer_sender)
    account = _clean_sender(req.transfer_account)
    if req.method == "transfer" and not sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")
    if req.method == "transfer" and not account:
        raise HTTPException(status_code=422, detail="Rekening penerima transfer wajib dipilih.")
    try:
        updated = await loop.run_in_executor(
            None, add_receipt_payment, receipt_id, req.amount, req.method, note, sender, account, user["username"], req.confirmed
        )
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith("Jumlah") else 409
        raise HTTPException(status_code=status, detail=msg)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/work-status", response_model=ReceiptDetail)
async def change_work_status_route(
    receipt_id: str, req: ReceiptWorkStatusRequest, user: dict = Depends(get_current_user)
):
    """Move a nota to Belum Dikerjakan, Dalam Pengerjaan or Pengerjaan Selesai. Leaving Draft takes the stock."""
    loop = asyncio.get_event_loop()
    note = _clean_name(req.note)
    if note and len(note) > 200:
        raise HTTPException(status_code=422, detail="Catatan maksimal 200 karakter.")
    try:
        updated = await loop.run_in_executor(
            None, change_receipt_work_status, receipt_id, req.to, note, user["username"], _override_reason(req, user)
        )
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Stok", "Status tujuan", "No. plat", "Nomor HP")) else 409
        raise HTTPException(status_code=status, detail=msg)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/work-stage", response_model=ReceiptDetail)
async def set_work_stage_route(receipt_id: str, req: ReceiptWorkStageRequest, user: dict = Depends(get_current_user)):
    """K-06: Persiapan / Pemasangan / QC while a nota is Dalam Pengerjaan. Information only."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, set_work_stage, receipt_id, req.stage, user["username"])
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "tidak ditemukan" in msg else 422 if msg.startswith("Tahap") else 409, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


def _decode_data_url(raw: str, kind: str) -> dict:
    mime = ""
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        mime = head[5:].split(";")[0]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="Isi lampiran serah terima tidak valid.")
    if not matches_content_type(mime, data):
        raise HTTPException(status_code=422, detail="Lampiran bukan gambar JPG, PNG, atau WEBP yang valid.")
    return {"kind": kind, "mime": mime, "data": data}


@router.post("/receipts/{receipt_id}/handover", response_model=ReceiptDetail)
async def hand_over_receipt_route(receipt_id: str, req: ReceiptHandoverRequest, user: dict = Depends(get_current_user)):
    """K-08/K-09 Serah Terima Kendaraan. With a balance owed only an admin may, with a reason. May carry
    the receiver's name, photos of the vehicle and the receiver's signature."""
    loop = asyncio.get_event_loop()
    files = [_decode_data_url(p, "foto") for p in req.photos] + ([_decode_data_url(req.signature, "tanda_tangan")] if req.signature else [])
    try:
        updated = await loop.run_in_executor(None, lambda: hand_over_receipt(
            receipt_id, req.note, req.reason, user["username"], user.get("role") == "admin",
            receiver_name=req.receiver_name, files=files))
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 403 if msg.startswith("Hanya admin") else 422 if msg.startswith("Alasan") else 409
        raise HTTPException(status_code=status, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.get("/receipts/{receipt_id}/handover-files")
async def list_handover_files_route(receipt_id: str):
    """Photos and signature taken at Serah Terima (only those of the current, not cancelled, handover)."""
    return await asyncio.get_event_loop().run_in_executor(None, get_handover_files, receipt_id)


@router.get("/receipts/handover-files/{file_id}")
async def handover_file_route(file_id: int):
    try:
        f = await asyncio.get_event_loop().run_in_executor(None, get_handover_file, file_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(f["path"], media_type=f["mime"])


@router.post("/receipts/{receipt_id}/handover/undo", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def undo_handover_route(receipt_id: str, req: ReceiptReopenRequest, user: dict = Depends(get_current_user)):
    """Admin: a handover recorded by mistake."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, undo_handover, receipt_id, req.reason, user["username"])
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "tidak ditemukan" in msg else 422 if msg.startswith("Alasan") else 409, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.get("/receipts/{receipt_id}/events", response_model=list[ReceiptEventOut])
async def list_receipt_events(receipt_id: str):
    """Catatan & Riwayat: the nota's timeline (created, status moves, items added, void)."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return [ReceiptEventOut(**e) for e in await loop.run_in_executor(None, get_receipt_events, receipt_id)]


@router.post("/receipts/{receipt_id}/payments/{payment_id}/reverse", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def reverse_receipt_payment_route(
    receipt_id: str, payment_id: int, req: ReceiptPaymentReverseRequest, user: dict = Depends(get_current_user)
):
    """Batalkan Pembayaran (admin): writes a negative row, never deletes the original."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, reverse_receipt_payment, receipt_id, payment_id, req.reason, user["username"])
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith("Alasan") else 409
        raise HTTPException(status_code=status, detail=msg)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/payments/{payment_id}/confirm", response_model=ReceiptDetail)
async def confirm_receipt_payment_route(receipt_id: str, payment_id: int, user: dict = Depends(get_current_user)):
    """K-18: the transfer/QRIS money has been seen in the account. Staff and admin may confirm; who/when is kept."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, confirm_receipt_payment, receipt_id, payment_id, user["username"])
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "tidak ditemukan" in msg else 409, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.post("/receipts/{receipt_id}/payments/{payment_id}/fail", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def fail_receipt_payment_route(receipt_id: str, payment_id: int, req: PaymentFailRequest, user: dict = Depends(get_current_user)):
    """K-18 (admin): the money never arrived; the Pending payment becomes Gagal."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, fail_receipt_payment, receipt_id, payment_id, req.reason, user["username"])
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "tidak ditemukan" in msg else 422 if msg.startswith("Alasan") else 409, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.get("/payments/pending")
async def pending_payments_route():
    """K-18: transfer/QRIS payments still waiting to be seen in the account (notas and draft Sales Orders)."""
    return await asyncio.get_event_loop().run_in_executor(None, list_pending_payments)


def _detail(updated: dict, items: list, payments: list) -> ReceiptDetail:
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/refund", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def refund_receipt_route(receipt_id: str, req: ReceiptRefundRequest, user: dict = Depends(get_current_user)):
    """Kembalikan Kelebihan Bayar (admin). Only ever hands back what the nota is overpaid."""
    if req.method not in VALID_PAYMENT_METHODS:
        raise HTTPException(status_code=422, detail=f"Metode harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}.")
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, refund_receipt_overpay, receipt_id, req.amount, req.method, req.reason, user["username"], req.proof_ref)
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Jumlah", "Alasan", "Bukti")) else 409
        raise HTTPException(status_code=status, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.post("/receipts/{receipt_id}/reopen", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def reopen_receipt_route(receipt_id: str, req: ReceiptReopenRequest, user: dict = Depends(get_current_user)):
    """Buka Kembali (admin): revise a closed nota; reason and user are kept in its event log."""
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(None, reopen_closed_receipt, receipt_id, req.reason, user["username"])
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith("Alasan") else 409
        raise HTTPException(status_code=status, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.post("/receipts/{receipt_id}/returns", response_model=ReceiptDetail, dependencies=[Depends(require_admin)])
async def return_receipt_item_route(receipt_id: str, req: ReceiptReturnRequest, user: dict = Depends(get_current_user)):
    """Retur Barang (admin): works on a closed nota too; see return_receipt_item."""
    if req.method not in VALID_PAYMENT_METHODS:
        raise HTTPException(status_code=422, detail=f"Metode harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}.")
    loop = asyncio.get_event_loop()
    try:
        updated = await loop.run_in_executor(
            None, lambda: return_receipt_item(
                receipt_id, req.item_id, req.quantity, req.destination,
                req.refund_amount, req.method, req.reason, user["username"],
                kind=req.kind, reason_code=req.reason_code, restock_qty=req.restock_qty,
                supplier_qty=req.supplier_qty, damaged_qty=req.damaged_qty, proof_ref=req.proof_ref,
                owner_ok=bool(user.get("is_owner")),
            ),
        )
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Jumlah", "Alasan", "Tujuan", "Jenis", "Bukti")) else 409
        raise HTTPException(status_code=status, detail=msg)
    return _detail(updated, await loop.run_in_executor(None, get_receipt_items, receipt_id),
                   await loop.run_in_executor(None, get_receipt_payments, receipt_id))


@router.get("/receipts/{receipt_id}/returns", response_model=list[ReceiptReturnOut])
async def list_receipt_returns(receipt_id: str):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return [ReceiptReturnOut(**r) for r in await loop.run_in_executor(None, get_receipt_returns, receipt_id)]


@router.get("/receipts/{receipt_id}/preorders")
async def list_receipt_preorders(receipt_id: str):
    """Terkait Sales Order: pesanan yang menjadi sumber barang atau DP nota ini."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return await loop.run_in_executor(None, get_receipt_preorders, receipt_id)


@router.post("/receipts/{receipt_id}/merge-back", dependencies=[Depends(require_admin)])
async def merge_back_route(receipt_id: str, body: dict, user: dict = Depends(get_current_user)):
    """K-19: folds an Invoice Lanjutan back into its Sales Order's main invoice (lines and money), then voids it."""
    loop = asyncio.get_event_loop()
    try:
        po = await loop.run_in_executor(None, merge_followup_invoice, receipt_id, str(body.get("reason") or ""), user["username"])
    except ValueError as e:
        raise HTTPException(status_code=404 if "tidak ditemukan" in str(e) else 409, detail=str(e))
    return {"preorder_id": po["id"], "po_number": po["po_number"]}


@router.delete("/receipts/{receipt_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_receipt_route(receipt_id: str):
    loop = asyncio.get_event_loop()
    existing = await loop.run_in_executor(None, get_receipt, receipt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    _refuse_if_paid(existing)
    try:
        await loop.run_in_executor(None, delete_receipt, receipt_id)
    except ValueError as e:
        raise HTTPException(status_code=404 if "tidak ditemukan" in str(e) else 409, detail=str(e))
    return Response(status_code=204)


@router.get("/plate-codes")
async def get_plate_codes():
    return PLATE_CODES


@router.get("/transfer-senders", response_model=list[TransferSenderOut])
async def get_transfer_senders():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, list_transfer_names, "sender")


@router.get("/transfer-accounts", response_model=list[TransferSenderOut])
async def get_transfer_accounts():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, list_transfer_names, "account")


async def _remove_transfer_name(kind: str, entry_id: int) -> Response:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_transfer_name, kind, entry_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


@router.delete("/transfer-senders/{entry_id}", status_code=204, response_class=Response)
async def remove_transfer_sender(entry_id: int):
    return await _remove_transfer_name("sender", entry_id)


@router.delete("/transfer-accounts/{entry_id}", status_code=204, response_class=Response)
async def remove_transfer_account(entry_id: int):
    return await _remove_transfer_name("account", entry_id)
