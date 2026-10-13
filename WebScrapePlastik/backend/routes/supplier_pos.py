import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.database import (
    SPO_ORDER_STATUSES,
    SPO_PAYMENT_STATUSES,
    SUPPLIER_PO_STATUSES,
    add_supplier_po_payment,
    cancel_supplier_po,
    cancel_supplier_po_item,
    create_supplier_po,
    get_all_supplier_pos,
    get_supplier_po,
    get_supplier_po_candidates,
    get_supplier_po_summary,
    issue_supplier_po,
    parse_ymd,
    receive_supplier_po,
    set_supplier_po_stage,
    set_supplier_po_tracking,
    update_supplier_po,
    update_supplier_po_invoice,
)
from backend.schemas import (
    SupplierPoCancelItemRequest,
    SupplierPoCandidatePage,
    SupplierPoDetail,
    SupplierPoInvoiceFields,
    SupplierPoItemRequest,
    SupplierPoOut,
    SupplierPoPaymentRequest,
    SupplierPoReceiveRequest,
    SupplierPoRequest,
    SupplierPoStageRequest,
    SupplierPoSummary,
    SupplierPoTrackingRequest,
    SupplierPoUpdateRequest,
)

router = APIRouter()

CANDIDATE_PROGRESS = ("belum", "sebagian", "semua")


def _validate_po_date(raw: str) -> str:
    try:
        return parse_ymd(raw).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal PO harus berformat YYYY-MM-DD.")


def _text(raw: str | None) -> str | None:
    return raw.strip() if raw and raw.strip() else None


def _clean_items(items: list[SupplierPoItemRequest]) -> list[dict]:
    cleaned = []
    for item in items:
        name = item.product_name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Nama barang wajib diisi.")
        payload = item.model_dump()
        payload["product_name"] = name
        payload["unit"] = _text(item.unit)
        payload["sku"] = _text(item.sku)
        cleaned.append(payload)
    return cleaned


def _header(req: SupplierPoInvoiceFields, only_sent: bool) -> dict:
    """The invoice fields of a request. On an edit only the ones actually sent come back, so an
    omitted field is left alone while an explicit null clears it."""
    out = {}
    for key in ("supplier_invoice_no", "payment_terms_days", "due_date", "est_arrival_date",
                "discount_pct", "shipping_cost", "tax_rate"):
        if only_sent and key not in req.model_fields_set:
            continue
        value = getattr(req, key)
        out[key] = _text(value) if isinstance(value, str) else value
    return out


async def _run(fn, *args, **kwargs):
    """Runs a database call off the event loop, turning the storage layer's
    ValueError (a rule was broken) into 422 and LookupError into 404."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# Declared before /supplier-pos/{id} so "candidates" and "summary" are not read as ids.
@router.get("/supplier-pos/candidates", response_model=SupplierPoCandidatePage)
async def list_candidates(
    query: str | None = None,
    progress: str = "belum",
    exclude_po: str | None = None,
    limit: int = 10,
    offset: int = 0,
):
    """The "Ambil Barang dari PO Customer" picker: customer preorder lines with
    quantity still unordered. `exclude_po` is the draft being edited."""
    if progress not in CANDIDATE_PROGRESS:
        raise HTTPException(status_code=422, detail=f"progress harus salah satu dari: {', '.join(CANDIDATE_PROGRESS)}.")
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(status_code=422, detail="limit harus 1-100 dan offset tidak boleh negatif.")
    return await _run(get_supplier_po_candidates, query or None, progress, exclude_po, limit, offset)


@router.get("/supplier-pos/summary", response_model=SupplierPoSummary)
async def supplier_po_summary():
    return await _run(get_supplier_po_summary)


@router.get("/supplier-pos", response_model=list[SupplierPoOut])
async def list_supplier_pos(
    query: str | None = None, status: str | None = None, order_status: str | None = None,
    payment_status: str | None = None,
):
    if status and status not in SUPPLIER_PO_STATUSES:
        raise HTTPException(status_code=422, detail=f"Status '{status}' tidak dikenal. Pilihan: {', '.join(SUPPLIER_PO_STATUSES)}.")
    if order_status and order_status not in SPO_ORDER_STATUSES:
        raise HTTPException(status_code=422, detail=f"order_status harus salah satu dari: {', '.join(SPO_ORDER_STATUSES)}.")
    if payment_status and payment_status not in SPO_PAYMENT_STATUSES:
        raise HTTPException(status_code=422, detail=f"payment_status harus salah satu dari: {', '.join(SPO_PAYMENT_STATUSES)}.")
    return await _run(get_all_supplier_pos, query or None, status or None, order_status or None, payment_status or None)


@router.post("/supplier-pos", response_model=SupplierPoDetail, status_code=201)
async def create_new_supplier_po(req: SupplierPoRequest, user: dict = Depends(get_current_user)):
    po_date = _validate_po_date(req.po_date) if req.po_date else None
    return await _run(
        create_supplier_po, str(uuid4()), req.supplier_id, _clean_items(req.items),
        po_date=po_date, notes=_text(req.notes), issue=req.issue, created_by=user["username"],
        header=_header(req, False), initial_paid=req.paid_amount,
    )


@router.get("/supplier-pos/{supplier_po_id}", response_model=SupplierPoDetail)
async def get_supplier_po_detail(supplier_po_id: str):
    po = await _run(get_supplier_po, supplier_po_id)
    if po is None:
        raise HTTPException(status_code=404, detail="PO supplier tidak ditemukan.")
    return po


@router.patch("/supplier-pos/{supplier_po_id}", response_model=SupplierPoDetail)
async def update_supplier_po_route(
    supplier_po_id: str, req: SupplierPoUpdateRequest, user: dict = Depends(get_current_user)
):
    update_notes = "notes" in req.model_fields_set
    return await _run(
        update_supplier_po, supplier_po_id, supplier_id=req.supplier_id,
        po_date=_validate_po_date(req.po_date) if req.po_date is not None else None,
        items=_clean_items(req.items) if req.items is not None else None,
        notes=_text(req.notes) if update_notes else None, update_notes=update_notes,
        header=_header(req, True), updated_by=user["username"],
    )


@router.patch("/supplier-pos/{supplier_po_id}/invoice", response_model=SupplierPoDetail)
async def update_supplier_po_invoice_route(
    supplier_po_id: str, req: SupplierPoInvoiceFields, user: dict = Depends(get_current_user)
):
    """Faktur data of an issued PO (invoice no, termin, jatuh tempo, discount, PPN, shipping)."""
    return await _run(update_supplier_po_invoice, supplier_po_id, _header(req, True), user["username"])


@router.post("/supplier-pos/{supplier_po_id}/issue", response_model=SupplierPoDetail)
async def issue_supplier_po_route(supplier_po_id: str, user: dict = Depends(get_current_user)):
    return await _run(issue_supplier_po, supplier_po_id, user["username"])


@router.post("/supplier-pos/{supplier_po_id}/stage", response_model=SupplierPoDetail)
async def set_stage_route(supplier_po_id: str, req: SupplierPoStageRequest, user: dict = Depends(get_current_user)):
    return await _run(set_supplier_po_stage, supplier_po_id, req.to, req.note, user["username"])


@router.put("/supplier-pos/{supplier_po_id}/tracking", response_model=SupplierPoDetail)
async def set_tracking_route(
    supplier_po_id: str, req: SupplierPoTrackingRequest, user: dict = Depends(get_current_user)
):
    lines = [{**l.model_dump(), "tracking_no": _text(l.tracking_no), "courier": _text(l.courier),
              "eta_date": _text(l.eta_date)} for l in req.items]
    return await _run(set_supplier_po_tracking, supplier_po_id, lines, user["username"])


@router.post("/supplier-pos/{supplier_po_id}/receive", response_model=SupplierPoDetail)
async def receive_supplier_po_route(
    supplier_po_id: str, req: SupplierPoReceiveRequest, user: dict = Depends(get_current_user)
):
    return await _run(
        receive_supplier_po, supplier_po_id, [line.model_dump() for line in req.items], user["username"],
        received_date=_text(req.received_date), surat_jalan_no=_text(req.surat_jalan_no),
        warehouse_location=_text(req.warehouse_location), note=_text(req.note), create_inbound=req.create_inbound,
    )


@router.post("/supplier-pos/{supplier_po_id}/payments", response_model=SupplierPoDetail, status_code=201)
async def add_payment_route(
    supplier_po_id: str, req: SupplierPoPaymentRequest, user: dict = Depends(get_current_user)
):
    return await _run(add_supplier_po_payment, supplier_po_id, req.amount, req.method, _text(req.note), user["username"])


@router.post("/supplier-pos/{supplier_po_id}/items/{item_id}/cancel", response_model=SupplierPoDetail)
async def cancel_item_route(
    supplier_po_id: str, item_id: int, req: SupplierPoCancelItemRequest, user: dict = Depends(get_current_user)
):
    return await _run(
        cancel_supplier_po_item, supplier_po_id, item_id, req.quantity, req.reason, req.action,
        _text(req.note), user["username"],
    )


@router.post("/supplier-pos/{supplier_po_id}/cancel", response_model=SupplierPoDetail)
async def cancel_supplier_po_route(supplier_po_id: str, user: dict = Depends(get_current_user)):
    return await _run(cancel_supplier_po, supplier_po_id, user["username"])


# There is deliberately no DELETE route: a PO that should not stand is cancelled
# (status 'batal'), which keeps the record and frees its quantities.
