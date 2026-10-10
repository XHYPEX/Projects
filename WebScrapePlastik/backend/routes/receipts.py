import asyncio
import re
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import (
    PHONE_RE,
    count_receipts,
    create_receipt,
    delete_receipt,
    delete_transfer_sender,
    list_transfer_senders,
    get_all_receipts,
    get_customer,
    get_receipt,
    get_receipt_items,
    normalize_phone,
    parse_ymd,
    settle_receipt,
    update_receipt,
)
from backend.schemas import (
    VALID_PAYMENT_METHODS,
    ReceiptDetail,
    ReceiptItemOut,
    ReceiptItemRequest,
    ReceiptOut,
    ReceiptRequest,
    ReceiptResponse,
    ReceiptSettleRequest,
    ReceiptUpdateRequest,
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
        if not item.product_name.strip():
            raise HTTPException(status_code=422, detail="Nama produk wajib diisi.")
        if item.warranty_date:
            try:
                date.fromisoformat(item.warranty_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Tanggal garansi tidak valid, gunakan format YYYY-MM-DD.")
        validated.append(item.model_dump())
    return validated


def _row_to_item(row: dict) -> ReceiptItemOut:
    return ReceiptItemOut(
        id=row["id"],
        product_name=row["product_name"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        warranty_date=row["warranty_date"],
        sku=row.get("sku"),
        unit=row.get("unit"),
    )


def _clean_sender(raw: str | None) -> str | None:
    name = " ".join((raw or "").split())
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="Nama pengirim maksimal 100 karakter.")
    return name or None


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
async def create_new_receipt(req: ReceiptRequest):
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

    if req.discount < 0:
        raise HTTPException(status_code=422, detail="Diskon harus >= 0.")

    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    if req.discount > subtotal:
        raise HTTPException(status_code=422, detail="Diskon tidak boleh melebihi subtotal.")
    # New sales are paid in full at the register; there are no down payments.
    if req.status != "void" and req.amount_paid != subtotal - req.discount:
        raise HTTPException(status_code=422, detail="Jumlah dibayar harus sama dengan total.")
    transfer_sender = _clean_sender(req.transfer_sender)
    if req.payment_method == "transfer" and not transfer_sender:
        raise HTTPException(status_code=422, detail="Nama pengirim transfer wajib diisi.")

    receipt_id = str(uuid4())
    try:
        result = await loop.run_in_executor(
            None, create_receipt, receipt_id, plate_region, req.plate_number, plate_suffix, items, req.discount,
            req.amount_paid, req.status, customer_phone, customer_name, req.started_at, req.payment_method,
            req.customer_id, transfer_sender,
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

    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(
        None, get_all_receipts, plate, status, date_from, date_to, customer, q, limit, tz_offset
    )
    total = await loop.run_in_executor(
        None, count_receipts, plate, status, date_from, date_to, customer, q, tz_offset
    )
    response.headers["X-Total-Count"] = str(total)
    return [ReceiptOut(**r) for r in rows]


@router.get("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def get_receipt_detail(receipt_id: str):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_receipt, receipt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**row, item_count=len(items), items=[_row_to_item(i) for i in items])


@router.patch("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def update_receipt_route(receipt_id: str, req: ReceiptUpdateRequest):
    loop = asyncio.get_event_loop()
    existing = await loop.run_in_executor(None, get_receipt, receipt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")

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
        effective_subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    elif req.discount is not None:
        existing_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
        effective_subtotal = sum(i["quantity"] * i["unit_price"] for i in existing_items)
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
        )
    except ValueError as e:
        # The customer was deleted between the check above and the save.
        raise HTTPException(status_code=422, detail=str(e))
    result_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**updated, item_count=len(result_items), items=[_row_to_item(i) for i in result_items])


@router.post("/receipts/{receipt_id}/settle", response_model=ReceiptDetail)
async def settle_receipt_route(receipt_id: str, req: ReceiptSettleRequest | None = None):
    """'Tandai Selesai': record the remaining balance of a pending receipt as paid.
    Only the payment columns change -- never items, totals or stock."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    payment_method = req.payment_method if req is not None else None
    if payment_method is not None and payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(
            status_code=422, detail=f"Metode pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_METHODS))}."
        )
    try:
        updated = await loop.run_in_executor(None, settle_receipt, receipt_id, payment_method)
    except ValueError as e:
        # Deleted between the check above and the settle -> 404; void/already done -> 409.
        raise HTTPException(status_code=404 if "tidak ditemukan" in str(e) else 409, detail=str(e))
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**updated, item_count=len(items), items=[_row_to_item(i) for i in items])


@router.delete("/receipts/{receipt_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_receipt_route(receipt_id: str):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Struk tidak ditemukan.")
    await loop.run_in_executor(None, delete_receipt, receipt_id)
    return Response(status_code=204)


@router.get("/plate-codes")
async def get_plate_codes():
    return PLATE_CODES


@router.get("/transfer-senders", response_model=list[TransferSenderOut])
async def get_transfer_senders():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, list_transfer_senders)


@router.delete("/transfer-senders/{sender_id}", status_code=204, response_class=Response)
async def remove_transfer_sender(sender_id: int):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_transfer_sender, sender_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)
