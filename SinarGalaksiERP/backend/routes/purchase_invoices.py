import asyncio
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from backend.auth import get_current_user, require_admin
from backend.database import (
    shop_today,
    allocate_spo_dp,
    get_payment_row,
    set_payment_proof,
    verify_payment_proof,
    reverse_dp_allocation,
    compute_payment_status,
    get_purchase_invoice_payments,
    create_purchase_invoice,
    delete_purchase_invoice,
    get_master_item,
    get_purchase_invoice,
    get_purchase_invoice_items,
    get_purchase_payable_summary,
    get_supplier,
    list_po_received_for_faktur,
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
    PurchaseInvoicePaymentOut,
    PurchaseInvoicePaymentRequest,
    PurchaseInvoiceRequest,
    DpAllocateRequest,
    DpReverseRequest,
    InvoiceExceptionRequest,
    InvoiceHoldRequest,
    InvoiceVerifyRequest,
    VoidRequest,
)

from backend import invoice_verify, payment_proofs

router = APIRouter()

MAX_TAX_RATE = 100.0
_TERMS_PATTERN = re.compile(r"^NET\s*(\d{1,3})$", re.IGNORECASE)


# --- helpers ----------------------------------------------------------------


def _raise_from_value_error(e: ValueError) -> None:
    msg = str(e)
    if "tidak ditemukan" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


_DATE_FIELD_LABELS = {"invoice_date": "Tanggal faktur", "due_date": "Tanggal jatuh tempo"}


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{_DATE_FIELD_LABELS.get(field, field)} tidak valid, gunakan format YYYY-MM-DD.")


def _resolve_due_date(invoice_date: str, due_date: str | None, payment_terms: str) -> str:
    """Explicit due_date always wins. Otherwise "NET <n>" is expanded into a real
    date so the list/overdue query never has to parse free-text terms."""
    inv = _parse_iso_date(invoice_date, "invoice_date")
    if due_date:
        due = _parse_iso_date(due_date, "due_date")
        if due < inv:
            raise HTTPException(status_code=422, detail="Tanggal jatuh tempo tidak boleh sebelum tanggal faktur.")
        return due.isoformat()
    match = _TERMS_PATTERN.match(payment_terms.strip())
    if match is None:
        raise HTTPException(
            status_code=422,
            detail="Tanggal jatuh tempo wajib diisi jika termin pembayaran bukan berformat 'NET <hari>'.",
        )
    return (inv + timedelta(days=int(match.group(1)))).isoformat()


async def _validate_items(items: list) -> list[dict]:
    if not items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu baris barang.")
    loop = asyncio.get_event_loop()
    validated: list[dict] = []
    for idx, item in enumerate(items, start=1):
        name = (item.product_name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail=f"Baris {idx}: nama barang wajib diisi.")
        if item.qty <= 0:
            raise HTTPException(status_code=422, detail=f"Baris {idx}: jumlah harus bilangan bulat positif.")
        if item.unit_price < 0:
            raise HTTPException(status_code=422, detail=f"Baris {idx}: harga satuan tidak boleh negatif.")
        sku = (item.sku or "").strip() or None
        if item.master_item_id is not None:
            master = await loop.run_in_executor(None, get_master_item, item.master_item_id)
            if master is None:
                raise HTTPException(
                    status_code=422, detail=f"Baris {idx}: barang dengan ID {item.master_item_id} tidak ditemukan."
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
        raise HTTPException(status_code=422, detail=f"Supplier dengan ID {req.supplier_id} tidak ditemukan.")
    if req.discount < 0:
        raise HTTPException(status_code=422, detail="Diskon tidak boleh negatif.")
    if not 0 <= req.tax_rate <= MAX_TAX_RATE:
        raise HTTPException(status_code=422, detail=f"Tarif pajak harus antara 0 dan {MAX_TAX_RATE:g}.")
    if not req.payment_terms.strip():
        raise HTTPException(status_code=422, detail="Termin pembayaran wajib diisi.")
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
        shipping_cost=row.get("shipping_cost") or 0,
        shipping_dpp=row.get("shipping_dpp") or 0,
        shipping_tax=row.get("shipping_tax") or 0,
        total=row["total"],
        amount_paid=row["amount_paid"],
        outstanding=outstanding,
        payment_status=compute_payment_status(row["total"], row["amount_paid"]),
        is_overdue=row["status"] == "posted" and outstanding > 0 and row["due_date"] < shop_today().isoformat(),
        status=row["status"],
        notes=row["notes"],
        void_reason=row["void_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        supplier_po_id=row.get("supplier_po_id"),
        supplier_po_number=row.get("supplier_po_number"),
        verify_status=row.get("verify_status") or "belum",
        verified_by=row.get("verified_by"),
        verified_at=row.get("verified_at"),
        verify_note=row.get("verify_note"),
        exception_reason=row.get("exception_reason"),
        hold_note=row.get("hold_note"),
        gr_docs=row.get("gr_docs") or [],
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
        raise HTTPException(status_code=404, detail=f"Faktur pembelian {invoice_id} tidak ditemukan.")
    items = await loop.run_in_executor(None, get_purchase_invoice_items, invoice_id)
    payments = await loop.run_in_executor(None, get_purchase_invoice_payments, invoice_id)
    base = _row_to_invoice(row)
    return PurchaseInvoiceDetail(**base.model_dump(), items=[_row_to_item(i) for i in items],
                                 payments=[PurchaseInvoicePaymentOut(**{**p, "has_proof": bool(p.get("proof_file"))}) for p in payments])


# --- endpoints ---------------------------------------------------------------


@router.get("/purchase-invoices/summary", response_model=PayableSummaryOut)
async def payable_summary():
    loop = asyncio.get_event_loop()
    return PayableSummaryOut(**await loop.run_in_executor(None, get_purchase_payable_summary))


@router.get("/purchase-invoices/goods-receipt")
async def goods_receipt_for_faktur(supplier_id: int | None = None):
    """Received goods of a supplier that no faktur covers yet (feeds 'Ambil dari Goods Receipt')."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, list_po_received_for_faktur, supplier_id)


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
            req.shipping_cost,
            req.shipping_dpp,
            req.shipping_tax,
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
            status_code=422, detail=f"Status harus salah satu dari: {', '.join(sorted(VALID_PURCHASE_INVOICE_STATUSES))}."
        )
    if payment_status is not None and payment_status not in VALID_PAYMENT_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"Status pembayaran harus salah satu dari: {', '.join(sorted(VALID_PAYMENT_STATUSES))}."
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
            req.shipping_cost,
            req.shipping_dpp,
            req.shipping_tax,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/post", response_model=PurchaseInvoiceDetail)
async def post_invoice(invoice_id: int, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, post_purchase_invoice, invoice_id, user["username"])
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.get("/purchase-invoices/{invoice_id}/matching")
async def invoice_matching(invoice_id: int):
    """Three-Way Matching (PO ↔ Goods Receipt ↔ Faktur) with the verification state and its trail."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, invoice_verify.status, invoice_id)
    except ValueError as e:
        _raise_from_value_error(e)


@router.post("/purchase-invoices/{invoice_id}/verify")
async def verify_invoice(invoice_id: int, req: InvoiceVerifyRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, invoice_verify.verify, invoice_id, req.checklist, req.note, user["username"])
    except ValueError as e:
        _raise_from_value_error(e)


@router.post("/purchase-invoices/{invoice_id}/hold")
async def hold_invoice(invoice_id: int, req: InvoiceHoldRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, invoice_verify.hold, invoice_id, req.note, user["username"], req.ask_admin)
    except ValueError as e:
        _raise_from_value_error(e)


@router.post("/purchase-invoices/{invoice_id}/exception", dependencies=[Depends(require_admin)])
async def approve_invoice_exception(invoice_id: int, req: InvoiceExceptionRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, invoice_verify.approve_exception, invoice_id, req.reason, user["username"])
    except ValueError as e:
        _raise_from_value_error(e)


@router.post("/purchase-invoices/{invoice_id}/dp-allocation", response_model=PurchaseInvoiceDetail, dependencies=[Depends(require_admin)])
async def allocate_dp(invoice_id: int, req: DpAllocateRequest, user: dict = Depends(get_current_user)):
    """Alokasi DP Supplier (Admin): carries an unused supplier DP onto this faktur. No new cash is recorded."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, allocate_spo_dp, req.spo_payment_id, invoice_id, req.amount, user["username"])
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/dp-allocation/{row_id}/reverse", response_model=PurchaseInvoiceDetail, dependencies=[Depends(require_admin)])
async def reverse_dp(invoice_id: int, row_id: int, req: DpReverseRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, reverse_dp_allocation, row_id, req.reason, user["username"])
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/void", response_model=PurchaseInvoiceDetail, dependencies=[Depends(require_admin)])
async def void_invoice(invoice_id: int, req: VoidRequest):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="Alasan wajib diisi.")
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, void_purchase_invoice, invoice_id, req.reason.strip())
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


@router.post("/purchase-invoices/{invoice_id}/payments", response_model=PurchaseInvoiceDetail)
async def add_payment(invoice_id: int, req: PurchaseInvoicePaymentRequest, user: dict = Depends(get_current_user)):
    if req.amount < 0 and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang bisa membatalkan pembayaran.")
    text = lambda v: (v or "").strip() or None
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: record_purchase_invoice_payment(
            invoice_id, req.amount, text(req.method), text(req.paid_date), text(req.reference_no), text(req.note),
            user["username"], text(req.source_account), text(req.dest_account)))
    except ValueError as e:
        _raise_from_value_error(e)
    return await _detail(invoice_id)


async def _read_proof_body(request: Request) -> bytes:
    too_big = HTTPException(status_code=413, detail="Ukuran bukti maksimal 5 MB.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > payment_proofs.MAX_PROOF_BYTES:
        raise too_big
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > payment_proofs.MAX_PROOF_BYTES:
            raise too_big
    return bytes(data)


def _payment_of_invoice(row: dict | None, invoice_id: int) -> dict:
    if row is None or row["invoice_id"] != invoice_id:
        raise HTTPException(status_code=404, detail="Pembayaran tidak ditemukan.")
    return row


@router.put("/purchase-invoices/{invoice_id}/payments/{payment_id}/proof", response_model=PurchaseInvoiceDetail)
async def put_payment_proof(invoice_id: int, payment_id: int, request: Request, name: str | None = None, user: dict = Depends(get_current_user)):
    """Raw-body upload of the proof (JPEG / PNG / WebP / PDF, 5 MB). Any logged-in user may attach it; only Admin verifies."""
    loop = asyncio.get_event_loop()
    _payment_of_invoice(await loop.run_in_executor(None, get_payment_row, payment_id), invoice_id)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in payment_proofs.PROOF_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Format bukti harus JPEG, PNG, WebP, atau PDF.")
    data = await _read_proof_body(request)
    if not data:
        raise HTTPException(status_code=422, detail="File bukti kosong.")
    if not payment_proofs.matches_content_type(content_type, data):
        raise HTTPException(status_code=422, detail="Isi file tidak cocok dengan format yang dipilih.")
    filename = await loop.run_in_executor(None, payment_proofs.save_proof, data, content_type)
    try:
        _row, previous = await loop.run_in_executor(None, set_payment_proof, payment_id, filename, name, user["username"])
    except ValueError as e:
        await loop.run_in_executor(None, payment_proofs.delete_proof_file, filename)
        _raise_from_value_error(e)
    await loop.run_in_executor(None, payment_proofs.delete_proof_file, previous)
    return await _detail(invoice_id)


@router.get("/purchase-invoices/{invoice_id}/payments/{payment_id}/proof")
async def get_payment_proof(invoice_id: int, payment_id: int):
    loop = asyncio.get_event_loop()
    row = _payment_of_invoice(await loop.run_in_executor(None, get_payment_row, payment_id), invoice_id)
    path = await loop.run_in_executor(None, payment_proofs.proof_file, row.get("proof_file"))
    if path is None:
        raise HTTPException(status_code=404, detail="Pembayaran ini belum punya bukti.")
    return FileResponse(path, media_type=payment_proofs.media_type_for(row["proof_file"]),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"})


@router.post("/purchase-invoices/{invoice_id}/payments/{payment_id}/proof/verify", response_model=PurchaseInvoiceDetail, dependencies=[Depends(require_admin)])
async def verify_payment_proof_route(invoice_id: int, payment_id: int, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    _payment_of_invoice(await loop.run_in_executor(None, get_payment_row, payment_id), invoice_id)
    try:
        await loop.run_in_executor(None, verify_payment_proof, payment_id, user["username"])
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
