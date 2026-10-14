import asyncio
import re
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import get_current_user, require_admin
from backend.database import (
    get_receipt_preorders,
    PHONE_RE,
    RECEIPT_PAYMENT_CLAUSES,
    RECEIPT_SORTS,
    RECEIPT_WORK_STATUSES,
    count_receipts,
    create_receipt,
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
        validated.append(item.model_dump())
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
        sku=row.get("sku"),
        unit=row.get("unit"),
        item_type=row.get("item_type") or "barang",
        line_discount=row.get("line_discount") or 0,
        added_at=row.get("added_at"),
        added_by=row.get("added_by"),
        added_during_work=bool(row.get("added_during_work")),
    )


EXTRA_MAX = {"work_notes": 1000, "cashier_name": 80, "estimated_finish_at": 40, "vehicle_make": 60,
             "vehicle_model": 60, "vehicle_color": 40, "vehicle_year": 8}


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
    plate_region = req.plate_region.upper()
    plate_suffix = req.plate_suffix.upper()

    if plate_region not in PLATE_CODES:
        raise HTTPException(status_code=422, detail=f"Kode wilayah plat tidak dikenal: {req.plate_region}")
    if not NUMBER_RE.match(req.plate_number):
        raise HTTPException(status_code=422, detail="Nomor plat harus 1-4 digit.")
    if not SUFFIX_RE.match(plate_suffix):
        raise HTTPException(status_code=422, detail="Huruf belakang plat harus 1-3 huruf.")
    if not req.items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu item.")
    if req.status is not None and req.status != "void":
        raise HTTPException(
            status_code=422,
            detail="Status hanya boleh diatur ke 'void' — pending/done dihitung otomatis dari amount_paid dibanding total.",
        )
    if req.amount_paid < 0:
        raise HTTPException(status_code=422, detail="Jumlah dibayar harus >= 0.")
    if req.work_status not in ("draft", "pengerjaan", "siap_bayar"):
        raise HTTPException(status_code=422, detail="work_status harus draft, pengerjaan atau siap_bayar.")
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
    if not customer_phone and req.customer_id is None:
        raise HTTPException(status_code=422, detail="Nomor HP wajib diisi.")
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
    if req.status != "void" and req.work_status != "siap_bayar" and req.amount_paid >= subtotal - req.discount and subtotal - req.discount > 0:
        raise HTTPException(status_code=422, detail="Pelunasan baru bisa setelah pengerjaan selesai (Lanjut ke Pembayaran).")
    transfer_sender = _clean_sender(req.transfer_sender)
    transfer_account = _clean_sender(req.transfer_account)
    takes_money_now = req.amount_paid > 0 and req.status != "void"
    if req.payment_method == "transfer" and takes_money_now and not transfer_sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")
    if req.payment_method == "transfer" and takes_money_now and not transfer_account:
        raise HTTPException(status_code=422, detail="Rekening penerima transfer wajib dipilih.")

    receipt_id = str(uuid4())
    try:
        result = await loop.run_in_executor(
            None, create_receipt, receipt_id, plate_region, req.plate_number, plate_suffix, items, req.discount,
            req.amount_paid, req.status, customer_phone, customer_name, req.started_at, req.payment_method,
            req.customer_id, transfer_sender, transfer_account, user["username"], req.work_status,
            _clean_extra(req, False),
        )
    except ValueError as e:
        # The customer was deleted between the check above and the save.
        raise HTTPException(status_code=422, detail=str(e))
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
            if (existing["amount_paid"] or 0) > 0 and user.get("role") != "admin":
                raise HTTPException(status_code=403, detail="Hanya admin yang boleh membatalkan nota yang sudah ada pembayarannya.")
    else:
        _refuse_if_paid(existing)

    plate_region: str | None = None
    plate_suffix: str | None = None
    if req.plate_region is not None:
        plate_region = req.plate_region.upper()
        if plate_region not in PLATE_CODES:
            raise HTTPException(status_code=422, detail=f"Kode wilayah plat tidak dikenal: {req.plate_region}")
    if req.plate_number is not None and not NUMBER_RE.match(req.plate_number):
        raise HTTPException(status_code=422, detail="Nomor plat harus 1-4 digit.")
    if req.plate_suffix is not None:
        plate_suffix = req.plate_suffix.upper()
        if not SUFFIX_RE.match(plate_suffix):
            raise HTTPException(status_code=422, detail="Huruf belakang plat harus 1-3 huruf.")

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
            if req.customer_id is None:
                raise HTTPException(status_code=422, detail="Nomor HP wajib diisi.")
            customer_phone = None  # blank next to a picked customer: take that customer's number
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
    if effective_method == "transfer" and req.status != "void" and not effective_sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")
    update_transfer_account = "transfer_account" in req.model_fields_set
    transfer_account = _clean_sender(req.transfer_account) if update_transfer_account else None
    effective_account = transfer_account if update_transfer_account else existing.get("transfer_account")
    if effective_method == "transfer" and req.status != "void" and not effective_account:
        raise HTTPException(status_code=422, detail="Rekening penerima transfer wajib dipilih.")

    try:
        updated = await loop.run_in_executor(
            None,
            update_receipt,
            receipt_id,
            plate_region,
            req.plate_number,
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
        )
    except ValueError as e:
        # The customer was deleted between the check above and the save, or the new total
        # would drop below what has already been paid.
        raise HTTPException(status_code=422, detail=str(e))
    result_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(result_items), items=[_row_to_item(i) for i in result_items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.post("/receipts/{receipt_id}/settle", response_model=ReceiptDetail)
async def settle_receipt_route(receipt_id: str, req: ReceiptSettleRequest, user: dict = Depends(get_current_user)):
    """'Tandai Selesai': record the remaining balance of a pending receipt as paid.
    Only the payment columns change -- never items, totals or stock."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    payment_method = req.payment_method
    if payment_method is not None and payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )
    try:
        updated = await loop.run_in_executor(None, settle_receipt, receipt_id, payment_method, req.amount, user["username"])
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
            None, add_receipt_payment, receipt_id, req.amount, req.method, note, sender, account, user["username"]
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
    """Move a nota to Dalam Pengerjaan or Siap Pembayaran. Leaving Draft takes the stock."""
    loop = asyncio.get_event_loop()
    note = _clean_name(req.note)
    if note and len(note) > 200:
        raise HTTPException(status_code=422, detail="Catatan maksimal 200 karakter.")
    try:
        updated = await loop.run_in_executor(
            None, change_receipt_work_status, receipt_id, req.to, note, user["username"]
        )
    except ValueError as e:
        msg = str(e)
        status = 404 if "tidak ditemukan" in msg else 422 if msg.startswith(("Stok", "Status tujuan")) else 409
        raise HTTPException(status_code=status, detail=msg)
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    payments = await loop.run_in_executor(None, get_receipt_payments, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items],
                         payments=[ReceiptPaymentOut(**p) for p in payments])


@router.get("/receipts/{receipt_id}/events", response_model=list[ReceiptEventOut])
async def list_receipt_events(receipt_id: str):
    """Catatan & Riwayat: the nota's timeline (created, status moves, items added, void)."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return [ReceiptEventOut(**e) for e in await loop.run_in_executor(None, get_receipt_events, receipt_id)]


@router.get("/receipts/{receipt_id}/preorders")
async def list_receipt_preorders(receipt_id: str):
    """Terkait PO Customer: pesanan yang menjadi sumber barang atau DP nota ini."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    return await loop.run_in_executor(None, get_receipt_preorders, receipt_id)


@router.delete("/receipts/{receipt_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_receipt_route(receipt_id: str):
    loop = asyncio.get_event_loop()
    existing = await loop.run_in_executor(None, get_receipt, receipt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    _refuse_if_paid(existing)
    await loop.run_in_executor(None, delete_receipt, receipt_id)
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
