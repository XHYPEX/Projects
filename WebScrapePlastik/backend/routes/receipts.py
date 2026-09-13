import asyncio
import re
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import create_receipt, delete_receipt, get_all_receipts, get_receipt, get_receipt_items, update_receipt
from backend.schemas import (
    VALID_PAYMENT_METHODS,
    ReceiptDetail,
    ReceiptItemOut,
    ReceiptItemRequest,
    ReceiptOut,
    ReceiptRequest,
    ReceiptResponse,
    ReceiptUpdateRequest,
)
from plate_codes import PLATE_CODES

router = APIRouter()

NUMBER_RE = re.compile(r"^\d{1,4}$")
SUFFIX_RE = re.compile(r"^[A-Za-z]{1,3}$")
# permissive: an optional leading +, then 7–15 digits, once separators are stripped.
PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def _normalize_phone(raw: str) -> str:
    """Strip spaces, dashes, dots and parens so '(0812) 3456-7890' and '081234567890'
    validate the same way; the cleaned form is what gets stored."""
    return re.sub(r"[\s\-.()]", "", raw or "")


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

    customer_phone = _normalize_phone(req.customer_phone)
    if not customer_phone:
        raise HTTPException(status_code=422, detail="Nomor HP wajib diisi.")
    if not PHONE_RE.match(customer_phone):
        raise HTTPException(status_code=422, detail="Nomor HP harus 7-15 digit (boleh diawali tanda +).")
    customer_name = _clean_name(req.customer_name)

    items = _validate_items(req.items)

    if req.discount < 0:
        raise HTTPException(status_code=422, detail="Diskon harus >= 0.")

    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    if req.discount > subtotal:
        raise HTTPException(status_code=422, detail="Diskon tidak boleh melebihi subtotal.")

    receipt_id = str(uuid4())
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, create_receipt, receipt_id, plate_region, req.plate_number, plate_suffix, items, req.discount,
        req.amount_paid, req.status, customer_phone, customer_name, req.started_at, req.payment_method,
    )
    return ReceiptResponse(receipt_id=receipt_id, **result)


@router.get("/receipts", response_model=list[ReceiptOut])
async def list_receipts(plate: str | None = None, status: str | None = None):
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_all_receipts, plate, status)
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
        customer_phone = _normalize_phone(req.customer_phone)
        if not customer_phone:
            raise HTTPException(status_code=422, detail="Nomor HP wajib diisi.")
        if not PHONE_RE.match(customer_phone):
            raise HTTPException(status_code=422, detail="Nomor HP harus 7-15 digit (boleh diawali tanda +).")
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
    )
    result_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**updated, item_count=len(result_items), items=[_row_to_item(i) for i in result_items])


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
