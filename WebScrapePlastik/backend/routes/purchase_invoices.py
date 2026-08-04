import asyncio
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import (
    compute_payment_status,
    create_purchase_invoice,
    delete_purchase_invoice,
    get_master_item,
    get_purchase_invoice,
    get_purchase_invoice_items,
    get_purchase_payable_summary,
    get_supplier,
    list_purchase_invoices,
    post_purchase_invoice,
    record_purchase_invoice_payment,
    update_purchase_invoice,
    void_purchase_invoice,
)
from backend.schemas import (
    VALID_PAYMENT_STATUSES,
    VALID_PURCHASE_INVOICE_STATUSES,
    PayableSummaryOut,
    PurchaseInvoiceDetail,
    PurchaseInvoiceItemOut,
    PurchaseInvoiceOut,
    PurchaseInvoicePaymentRequest,
    PurchaseInvoiceRequest,
    VoidRequest,
)

router = APIRouter()

MAX_TAX_RATE = 100.0
_TERMS_PATTERN = re.compile(r"^NET\s*(\d{1,3})$", re.IGNORECASE)


# --- helpers ----------------------------------------------------------------


def _raise_from_value_error(e: ValueError) -> None:
    msg = str(e)
    if "not found" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field}, expected YYYY-MM-DD")


def _resolve_due_date(invoice_date: str, due_date: str | None, payment_terms: str) -> str:
    """Explicit due_date always wins. Otherwise "NET <n>" is expanded into a real
    date so the list/overdue query never has to parse free-text terms."""
    inv = _parse_iso_date(invoice_date, "invoice_date")
    if due_date:
        due = _parse_iso_date(due_date, "due_date")
        if due < inv:
            raise HTTPException(status_code=422, detail="due_date cannot be before invoice_date")
        return due.isoformat()
    match = _TERMS_PATTERN.match(payment_terms.strip())
    if match is None:
        raise HTTPException(
            status_code=422,
            detail="due_date is required when payment_terms is not in 'NET <days>' form",
        )
    return (inv + timedelta(days=int(match.group(1)))).isoformat()


async def _validate_items(items: list) -> list[dict]:
    if not items:
        raise HTTPException(status_code=422, detail="At least one line item is required")
    loop = asyncio.get_event_loop()
    validated: list[dict] = []
    for idx, item in enumerate(items, start=1):
        name = (item.product_name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail=f"Line {idx}: product_name must not be empty")
        if item.qty <= 0:
            raise HTTPException(status_code=422, detail=f"Line {idx}: qty must be a positive integer")
        if item.unit_price < 0:
            raise HTTPException(status_code=422, detail=f"Line {idx}: unit_price must be >= 0")
        sku = (item.sku or "").strip() or None
        if item.master_item_id is not None:
            master = await loop.run_in_executor(None, get_master_item, item.master_item_id)
            if master is None:
                raise HTTPException(
                    status_code=422, detail=f"Line {idx}: master_item_id {item.master_item_id} not found"
                )
            # Snapshot from the master record rather than trusting the client's
            # copy — the printed invoice must match what the catalogue said.
            name, sku = master["name"], master["sku"]
        validated.append(
            {
                "master_item_id": item.master_item_id,
                "product_name": name,
                "sku": sku,
                "unit": (item.unit or "").strip(),
                "qty": item.qty,
                "unit_price": item.unit_price,
            }
        )
    return validated


async def _validate_header(req: PurchaseInvoiceRequest) -> str:
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"supplier_id {req.supplier_id} not found")
    if req.discount < 0:
        raise HTTPException(status_code=422, detail="discount must be >= 0")
    if not 0 <= req.tax_rate <= MAX_TAX_RATE:
        raise HTTPException(status_code=422, detail=f"tax_rate must be between 0 and {MAX_TAX_RATE:g}")
    if not req.payment_terms.strip():
        raise HTTPException(status_code=422, detail="payment_terms must not be empty")
    return _resolve_due_date(req.invoice_date, req.due_date, req.payment_terms)


def _row_to_invoice(row: dict) -> PurchaseInvoiceOut:
    outstanding = row["total"] - row["amount_paid"]
    return PurchaseInvoiceOut(
        id=row["id"],
        invoice_number=row["invoice_number"],
        supplier_id=row["supplier_id"],
        supplier_name=row.get("supplier_name"),
        supplier_invoice_no=row["supplier_invoice_no"],
        supplier_name_snapshot=row["supplier_name_snapshot"],
        supplier_phone_snapshot=row["supplier_phone_snapshot"],
        supplier_address_snapshot=row["supplier_address_snapshot"],
        supplier_contact_snapshot=row["supplier_contact_snapshot"],
        supplier_npwp=row.get("supplier_npwp"),
        invoice_date=row["invoice_date"],
        payment_terms=row["payment_terms"],
        due_date=row["due_date"],
        subtotal=row["subtotal"],
        discount=row["discount"],
        tax_rate=row["tax_rate"],
        tax_amount=row["tax_amount"],
        total=row["total"],
        amount_paid=row["amount_paid"],
        outstanding=outstanding,
        payment_status=compute_payment_status(row["total"], row["amount_paid"]),
        is_overdue=row["status"] == "posted" and outstanding > 0 and row["due_date"] < date.today().isoformat(),
        status=row["status"],
        notes=row["notes"],
        void_reason=row["void_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_item(row: dict) -> PurchaseInvoiceItemOut:
    return PurchaseInvoiceItemOut(
        id=row["id"],
        invoice_id=row["invoice_id"],
        master_item_id=row["master_item_id"],
        product_name=row["product_name"],
        sku=row["sku"],
        unit=row["unit"],
        qty=row["qty"],
        unit_price=row["unit_price"],
        line_total=row["line_total"],
        line_no=row["line_no"],
        current_sku=row.get("current_sku"),
        current_stock_qty=row.get("current_stock_qty"),
    )


async def _detail(invoice_id: int) -> PurchaseInvoiceDetail:
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_purchase_invoice, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Purchase invoice {invoice_id} not found")
    items = await loop.run_in_executor(None, get_purchase_invoice_items, invoice_id)
    base = _row_to_invoice(row)
    return PurchaseInvoiceDetail(**base.model_dump(), items=[_row_to_item(i) for i in items])


# --- endpoints ---------------------------------------------------------------


@router.get("/purchase-invoices/summary", response_model=PayableSummaryOut)
async def payable_summary():
    loop = asyncio.get_event_loop()
    return PayableSummaryOut(**await loop.run_in_executor(None, get_purchase_payable_summary))


@router.post("/purchase-invoices", response_model=PurchaseInvoiceDetail, status_code=201)
async def create_invoice(req: PurchaseInvoiceRequest):
    due_date = await _validate_header(req)
    items = await _validate_items(req.items)
    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(
            None,
            create_purchase_invoice,
            req.supplier_id,
            req.invoice_date,
            due_date,
            req.payment_terms.strip(),
            (req.supplier_invoice_no or "").strip() or None,
            (req.notes or "").strip() or None,
            req.discount,
            req.tax_rate,
            items,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(row["id"])


@router.get("/purchase-invoices", response_model=list[PurchaseInvoiceOut])
async def list_invoices(
    status: str | None = None,
    supplier_id: int | None = None,
    payment_status: str | None = None,
    search: str | None = None,
):
    if status is not None and status not in VALID_PURCHASE_INVOICE_STATUSES:
        raise HTTPException(
            status_code=422, detail="status must be one of: " + ", ".join(sorted(VALID_PURCHASE_INVOICE_STATUSES))
        )
    if payment_status is not None and payment_status not in VALID_PAYMENT_STATUSES:
        raise HTTPException(
            status_code=422, detail="payment_status must be one of: " + ", ".join(sorted(VALID_PAYMENT_STATUSES))
        )
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(
        None, list_purchase_invoices, status, supplier_id, payment_status, (search or "").strip() or None
    )
    return [_row_to_invoice(r) for r in rows]


@router.get("/purchase-invoices/{invoice_id}", response_model=PurchaseInvoiceDetail)
async def get_invoice(invoice_id: int):
    return await _detail(invoice_id)


@router.put("/purchase-invoices/{invoice_id}", response_model=PurchaseInvoiceDetail)
async def put_invoice(invoice_id: int, req: PurchaseInvoiceRequest):
    due_date = await _validate_header(req)
    items = await _validate_items(req.items)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            update_purchase_invoice,
            invoice_id,
            req.supplier_id,
            req.invoice_date,
            due_date,
            req.payment_terms.strip(),
            (req.supplier_invoice_no or "").strip() or None,
            (req.notes or "").strip() or None,
            req.discount,
            req.tax_rate,
            items,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/post", response_model=PurchaseInvoiceDetail)
async def post_invoice(invoice_id: int):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, post_purchase_invoice, invoice_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/void", response_model=PurchaseInvoiceDetail, dependencies=[Depends(require_admin)])
async def void_invoice(invoice_id: int, req: VoidRequest):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="reason must not be empty")
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, void_purchase_invoice, invoice_id, req.reason.strip())
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/payments", response_model=PurchaseInvoiceDetail)
async def add_payment(invoice_id: int, req: PurchaseInvoicePaymentRequest):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, record_purchase_invoice_payment, invoice_id, req.amount)
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.delete("/purchase-invoices/{invoice_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_invoice(invoice_id: int):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_purchase_invoice, invoice_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)
