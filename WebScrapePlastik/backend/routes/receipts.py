import asyncio
import re
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import create_receipt, delete_receipt, get_all_receipts, get_receipt, get_receipt_items, update_receipt
from backend.schemas import (
    VALID_RECEIPT_STATUSES,
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


def _validate_items(items: list[ReceiptItemRequest]) -> list[dict]:
    validated: list[dict] = []
    for item in items:
        if item.quantity <= 0:
            raise HTTPException(status_code=422, detail="quantity must be a positive integer")
        if item.unit_price < 0:
            raise HTTPException(status_code=422, detail="unit_price must be >= 0")
        if not item.product_name.strip():
            raise HTTPException(status_code=422, detail="product_name must not be empty")
        if item.warranty_date:
            try:
                date.fromisoformat(item.warranty_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid warranty_date, expected YYYY-MM-DD")
        validated.append(item.model_dump())
    return validated


def _row_to_item(row: dict) -> ReceiptItemOut:
    return ReceiptItemOut(
        id=row["id"],
        product_name=row["product_name"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
        warranty_date=row["warranty_date"],
    )


@router.post("/receipts", response_model=ReceiptResponse, status_code=201)
async def create_new_receipt(req: ReceiptRequest):
    plate_region = req.plate_region.upper()
    plate_suffix = req.plate_suffix.upper()

    if plate_region not in PLATE_CODES:
        raise HTTPException(status_code=422, detail=f"Unknown plate region code: {req.plate_region}")
    if not NUMBER_RE.match(req.plate_number):
        raise HTTPException(status_code=422, detail="Plate number must be 1-4 digits")
    if not SUFFIX_RE.match(plate_suffix):
        raise HTTPException(status_code=422, detail="Plate suffix must be 1-3 letters")
    if not req.items:
        raise HTTPException(status_code=422, detail="At least one line item is required")
    if req.status not in VALID_RECEIPT_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(VALID_RECEIPT_STATUSES)}")

    items = _validate_items(req.items)

    if req.discount < 0:
        raise HTTPException(status_code=422, detail="discount must be >= 0")

    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    if req.discount > subtotal:
        raise HTTPException(status_code=422, detail="discount cannot exceed subtotal")

    receipt_id = str(uuid4())
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, create_receipt, receipt_id, plate_region, req.plate_number, plate_suffix, items, req.discount, req.status
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
        raise HTTPException(status_code=404, detail="Receipt not found")
    items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**row, item_count=len(items), items=[_row_to_item(i) for i in items])


@router.patch("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def update_receipt_route(receipt_id: str, req: ReceiptUpdateRequest):
    loop = asyncio.get_event_loop()
    existing = await loop.run_in_executor(None, get_receipt, receipt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Receipt not found")

    plate_region: str | None = None
    plate_suffix: str | None = None
    if req.plate_region is not None:
        plate_region = req.plate_region.upper()
        if plate_region not in PLATE_CODES:
            raise HTTPException(status_code=422, detail=f"Unknown plate region code: {req.plate_region}")
    if req.plate_number is not None and not NUMBER_RE.match(req.plate_number):
        raise HTTPException(status_code=422, detail="Plate number must be 1-4 digits")
    if req.plate_suffix is not None:
        plate_suffix = req.plate_suffix.upper()
        if not SUFFIX_RE.match(plate_suffix):
            raise HTTPException(status_code=422, detail="Plate suffix must be 1-3 letters")

    if req.items is not None and not req.items:
        raise HTTPException(status_code=422, detail="At least one line item is required")
    if req.status is not None and req.status not in VALID_RECEIPT_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(VALID_RECEIPT_STATUSES)}")
    if req.discount is not None and req.discount < 0:
        raise HTTPException(status_code=422, detail="discount must be >= 0")

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
            raise HTTPException(status_code=422, detail="discount cannot exceed subtotal")

    updated = await loop.run_in_executor(
        None,
        update_receipt,
        receipt_id,
        plate_region,
        req.plate_number,
        plate_suffix,
        items,
        req.discount,
        req.status,
    )
    result_items = await loop.run_in_executor(None, get_receipt_items, receipt_id)
    return ReceiptDetail(**updated, item_count=len(result_items), items=[_row_to_item(i) for i in result_items])


@router.delete("/receipts/{receipt_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_receipt_route(receipt_id: str):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_receipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    await loop.run_in_executor(None, delete_receipt, receipt_id)
    return Response(status_code=204)


@router.get("/plate-codes")
async def get_plate_codes():
    return PLATE_CODES
