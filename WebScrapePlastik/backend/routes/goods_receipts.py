"""Goods Receipt (menu Barang Masuk): the wizard's endpoints. Posting and void stay on
/inventory/inbound-documents/{id}/post|void, so there is still one posting path."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import database as db
from backend import goods_receipt as gr
from backend.auth import get_current_user, require_admin

router = APIRouter()


class GrLine(BaseModel):
    spo_item_id: int
    qty_in: int
    cost_price: int
    qty_bad: int = 0
    bad_condition: str | None = None
    bad_note: str | None = None
    check_note: str | None = None
    price_reason: str | None = None
    spo_package_id: int | None = None


class GrDraft(BaseModel):
    supplier_id: int
    supplier_po_ids: list[str]
    received_date: str
    received_time: str | None = None
    officer: str | None = None
    arrival_method: str | None = None
    surat_jalan_no: str | None = None
    courier: str | None = None
    tracking_no: str | None = None
    package_count: str | None = None
    notes: str | None = None
    supplier_invoice_no: str | None = None
    items: list[GrLine]


class NoteRequest(BaseModel):
    note: str | None = None


class ApproveRequest(BaseModel):
    reason: str
    over_prices: dict[str, int | None] = {}


class CodeRequest(BaseModel):
    code: str | None = None
    reason: str | None = None


class PrintLine(BaseModel):
    master_item_id: int
    qty: int


class PrintRequest(BaseModel):
    kind: str = "cetak"
    inbound_document_id: int | None = None
    lines: list[PrintLine]


class ToleranceRequest(BaseModel):
    tolerance_pct: float


async def _run(fn, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/goods-receipts")
async def list_goods_receipts(status: str | None = None, supplier_id: int | None = None, q: str | None = None,
                              date_from: str | None = None, date_to: str | None = None):
    if status is not None and status not in ("draft", "menunggu_persetujuan", "posted", "void"):
        raise HTTPException(status_code=422, detail="Status tidak dikenal.")
    return await _run(gr.list_receipts, status, supplier_id, (q or "").strip() or None, date_from or None, date_to or None)


@router.get("/goods-receipts/sources")
async def gr_sources(supplier_id: int | None = None, include_po: str | None = None):
    return await _run(gr.gr_sources, supplier_id, include_po)


@router.get("/goods-receipts/po/{po_id}")
async def gr_po(po_id: str):
    """'Terima Barang' on a PO page opens the wizard with that PO: which supplier it belongs to."""
    return await _run(gr.po_supplier, po_id)


@router.post("/goods-receipts", status_code=201)
async def create_gr(req: GrDraft, user: dict = Depends(get_current_user)):
    doc_id = await _run(gr.save_draft, None, req.model_dump(), user["username"])
    return await _run(gr.detail, doc_id)


@router.get("/goods-receipts/{document_id}")
async def get_gr(document_id: int):
    return await _run(gr.detail, document_id)


@router.put("/goods-receipts/{document_id}")
async def update_gr(document_id: int, req: GrDraft, user: dict = Depends(get_current_user)):
    await _run(gr.save_draft, document_id, req.model_dump(), user["username"])
    return await _run(gr.detail, document_id)


@router.post("/goods-receipts/{document_id}/check")
async def check_gr(document_id: int, user: dict = Depends(get_current_user)):
    return await _run(gr.check_goods, document_id, user["username"])


@router.post("/goods-receipts/{document_id}/ack-price")
async def ack_price(document_id: int, user: dict = Depends(get_current_user)):
    return await _run(gr.ack_price, document_id, user["username"])


@router.post("/goods-receipts/{document_id}/request-approval")
async def request_approval(document_id: int, req: NoteRequest, user: dict = Depends(get_current_user)):
    return await _run(gr.request_approval, document_id, req.note, user["username"])


@router.post("/goods-receipts/{document_id}/approve", dependencies=[Depends(require_admin)])
async def approve(document_id: int, req: ApproveRequest, user: dict = Depends(get_current_user)):
    return await _run(gr.approve, document_id, req.reason, req.over_prices, user["username"])


@router.post("/goods-receipts/{document_id}/reject", dependencies=[Depends(require_admin)])
async def reject(document_id: int, req: NoteRequest, user: dict = Depends(get_current_user)):
    return await _run(gr.reject, document_id, req.note or "", user["username"])


@router.post("/goods-receipts/{document_id}/recreate", status_code=201)
async def recreate(document_id: int, user: dict = Depends(get_current_user)):
    new_id = await _run(gr.recreate, document_id, user["username"])
    return await _run(gr.detail, new_id)


class ReturnLine(BaseModel):
    inbound_item_id: int
    qty: int
    reason_code: str | None = None
    reason_detail: str | None = None


class ReturnRequest(BaseModel):
    method: str = "sales"
    note: str | None = None
    lines: list[ReturnLine]


@router.get("/goods-receipts/{document_id}/return-lines")
async def return_lines(document_id: int):
    return await _run(gr.return_lines, document_id)


@router.post("/goods-receipts/{document_id}/return", dependencies=[Depends(require_admin)])
async def return_to_supplier(document_id: int, req: ReturnRequest, user: dict = Depends(get_current_user)):
    try:
        return await _run(gr.return_to_supplier, document_id, req.model_dump(), user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/goods-receipts/{document_id}/integration")
async def integration(document_id: int):
    return await _run(gr.integration, document_id)


@router.get("/goods-receipts/{document_id}/barcode-items")
async def barcode_items(document_id: int):
    return await _run(gr.barcode_items, document_id)


# --- Kode Manual & labels ---------------------------------------------------------------------------------------


@router.get("/item-codes/check")
async def check_code(master_item_id: int, code: str):
    return await _run(gr.check_code, master_item_id, code)


@router.get("/item-codes/find")
async def find_code(code: str):
    return await _run(gr.find_item_for_reprint, code)


@router.get("/item-codes/{master_item_id}")
async def code_info(master_item_id: int):
    return await _run(gr.code_info, master_item_id)


@router.put("/item-codes/{master_item_id}", dependencies=[Depends(require_admin)])
async def set_code(master_item_id: int, req: CodeRequest, user: dict = Depends(get_current_user)):
    await _run(gr.set_manual_code, master_item_id, req.code, req.reason, user["username"])
    return await _run(gr.code_info, master_item_id)


@router.post("/barcode-prints", status_code=201)
async def record_prints(req: PrintRequest, user: dict = Depends(get_current_user)):
    return await _run(gr.record_prints, [l.model_dump() for l in req.lines], req.kind, req.inbound_document_id, user["username"])


@router.get("/barcode-prints")
async def list_prints(master_item_id: int | None = None, inbound_document_id: int | None = None, limit: int = 50):
    return await _run(gr.list_prints, master_item_id, inbound_document_id, max(1, min(limit, 200)))


# --- settings ---------------------------------------------------------------------------------------------------


@router.get("/settings/goods-receipt")
async def get_settings():
    return {"tolerance_pct": await _run(db.gr_price_tolerance)}


@router.put("/settings/goods-receipt", dependencies=[Depends(require_admin)])
async def put_settings(req: ToleranceRequest, user: dict = Depends(get_current_user)):
    if not 0 <= req.tolerance_pct <= 100:
        raise HTTPException(status_code=422, detail="Toleransi harus antara 0 dan 100%.")
    await _run(db.set_setting, db.GR_PRICE_TOLERANCE_KEY, f"{req.tolerance_pct:g}", user["username"])
    return {"tolerance_pct": req.tolerance_pct}
