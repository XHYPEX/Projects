"""Retur Supplier: staff draft and submit, admin approves; goods leave once at handover; the supplier's
answer is settled by replacement goods, debit notes or refunds. Money fields are admin-only."""

import asyncio
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from backend.auth import get_current_user
from backend.database import (
    SR_COST_BEARERS, SR_DOC_STATUS, SR_HANDOVER, SR_METHODS, SR_PHOTO_STAGES, SR_REASONS, SR_REJECT_FATES,
    SR_SETTLEMENTS, SR_TYPES, QUARANTINE_CONDITIONS,
    add_supplier_return_photo, allocate_supplier_credit, approve_supplier_return, cancel_supplier_credit,
    cancel_supplier_return, confirm_supplier_return_out, delete_supplier_return, delete_supplier_return_photo, flag_supplier_return_problem,
    get_supplier_return, get_supplier_return_photo, list_supplier_returns, mark_supplier_return_received,
    receive_supplier_return_rejected, receive_supplier_return_replacement, record_supplier_return_credit,
    record_supplier_return_decision, return_supplier_return_to_draft, save_supplier_return, submit_supplier_return,
    peek_supplier_return_number, supplier_return_candidates, supplier_return_inbound_lines, supplier_return_print_text, supplier_return_sources,
)
from backend.product_images import matches_content_type

router = APIRouter()


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _for(user: dict, data: dict) -> dict:
    """Staff see the return but not what it is worth to the supplier (the cost middleware zeroes unit_cost /
    cost_value; debit notes and claim amounts are dropped here)."""
    if user.get("role") == "admin" or not isinstance(data, dict) or "items" not in data:
        return data
    events = [{**e, "note": "Penyelesaian keuangan dicatat admin"} if e.get("kind") == "keuangan" else e
              for e in data.get("events", [])]
    return {**data, "credits": [], "claim_value": 0, "settled_value": 0, "claim_left": 0, "open_invoices": [],
            "shipping_claim_approved": 0, "events": events}


@router.get("/supplier-returns/meta")
async def sr_meta():
    return {"types": SR_TYPES, "settlements": SR_SETTLEMENTS, "methods": SR_METHODS, "cost_bearers": SR_COST_BEARERS,
            "reasons": SR_REASONS, "reject_fates": SR_REJECT_FATES, "photo_stages": SR_PHOTO_STAGES,
            "doc_status": SR_DOC_STATUS, "handover": SR_HANDOVER, "conditions": QUARANTINE_CONDITIONS}


@router.get("/supplier-returns/next-number")
async def sr_next_number():
    """Preview of the automatic return number; the real one is assigned when the draft is first saved."""
    return {"return_number": await _run(peek_supplier_return_number)}


@router.get("/supplier-returns")
async def sr_list(tab: str = "semua", query: str | None = None, supplier_id: int | None = None, page: int = 1,
                  method: str | None = None, status: str | None = None, user: dict = Depends(get_current_user)):
    if tab not in ("semua", "perlu_tindakan", "aktif", "selesai", "dibatalkan"):
        raise HTTPException(status_code=422, detail="Tab tidak dikenal.")
    data = await _run(list_supplier_returns, tab, query, supplier_id, page, method or None, status or None)
    if user.get("role") != "admin":
        for r in data["items"]:
            r.update(claim_value=0, settled_value=0, claim_left=0)
    return data


@router.get("/supplier-returns/candidates")
async def sr_candidates(supplier_id: int | None = None):
    return await _run(supplier_return_candidates, supplier_id)


@router.get("/supplier-returns/sources")
async def sr_sources(supplier_id: int):
    return await _run(supplier_return_sources, supplier_id)


@router.get("/supplier-returns/inbound/{document_id}")
async def sr_inbound_lines(document_id: int, exclude_return: int | None = None):
    return await _run(supplier_return_inbound_lines, document_id, exclude_return)


@router.post("/supplier-returns", status_code=201)
async def sr_create(body: dict, user: dict = Depends(get_current_user)):
    return _for(user, await _run(save_supplier_return, None, body, user))


@router.get("/supplier-returns/photos/{photo_id}")
async def sr_photo(photo_id: int):
    ph = await _run(get_supplier_return_photo, photo_id)
    return FileResponse(ph["path"], media_type=ph["mime"])


@router.delete("/supplier-returns/photos/{photo_id}")
async def sr_photo_delete(photo_id: int, user: dict = Depends(get_current_user)):
    return _for(user, await _run(delete_supplier_return_photo, photo_id, user))


@router.post("/supplier-returns/credits/{cn_id}/allocate")
async def sr_allocate(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return _for(user, await _run(allocate_supplier_credit, cn_id, int(body.get("invoice_id") or 0), int(body.get("amount") or 0), user))


@router.post("/supplier-returns/credits/{cn_id}/cancel")
async def sr_credit_cancel(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return _for(user, await _run(cancel_supplier_credit, cn_id, body.get("reason") or "", user))


@router.get("/supplier-returns/{return_id}")
async def sr_get(return_id: int, user: dict = Depends(get_current_user)):
    return _for(user, await _run(get_supplier_return, return_id))


@router.delete("/supplier-returns/{return_id}", status_code=204, response_class=Response)
async def sr_delete(return_id: int, user: dict = Depends(get_current_user)):
    """An untouched draft only (see supplier_return_deletable); anything else is cancelled."""
    await _run(delete_supplier_return, return_id, user)
    return Response(status_code=204)


@router.put("/supplier-returns/{return_id}")
async def sr_update(return_id: int, body: dict, user: dict = Depends(get_current_user)):
    return _for(user, await _run(save_supplier_return, return_id, body, user))


@router.post("/supplier-returns/{return_id}/photos", status_code=201)
async def sr_photo_add(return_id: int, body: dict, user: dict = Depends(get_current_user)):
    raw = body.get("data") or ""
    mime = body.get("mime") or ""
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        mime = head[5:].split(";")[0] or mime
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="Isi foto tidak valid.")
    if not matches_content_type(mime, data):
        raise HTTPException(status_code=422, detail="File bukan foto JPG, PNG, atau WEBP yang valid.")
    await _run(add_supplier_return_photo, return_id, body.get("stage") or "", body.get("item_id"), body.get("filename") or "",
               mime, data, user)
    return _for(user, await _run(get_supplier_return, return_id))


_ACTIONS = {
    "submit": lambda rid, b, u: submit_supplier_return(rid, b.get("note"), u),
    "approve": lambda rid, b, u: approve_supplier_return(rid, bool(b.get("shortage_ack")), b.get("note"), u),
    "to-draft": lambda rid, b, u: return_supplier_return_to_draft(rid, b.get("note") or "", u),
    "cancel": lambda rid, b, u: cancel_supplier_return(rid, b.get("reason") or "", bool(b.get("keep_in_quarantine")), u),
    "handover": lambda rid, b, u: confirm_supplier_return_out(rid, b, u),
    "received": lambda rid, b, u: mark_supplier_return_received(rid, b.get("received_date"), u),
    "problem": lambda rid, b, u: flag_supplier_return_problem(rid, b.get("note"), u),
    "decision": lambda rid, b, u: record_supplier_return_decision(rid, b, u),
    "replacement": lambda rid, b, u: receive_supplier_return_replacement(rid, b, u),
    "rejected-back": lambda rid, b, u: receive_supplier_return_rejected(rid, b, u),
    "credit": lambda rid, b, u: record_supplier_return_credit(rid, b, u),
}


@router.post("/supplier-returns/{return_id}/print/{doc}")
async def sr_print(return_id: int, doc: str, user: dict = Depends(get_current_user)):
    return await _run(supplier_return_print_text, return_id, doc, user)


@router.post("/supplier-returns/{return_id}/{action}")
async def sr_action(return_id: int, action: str, body: dict, user: dict = Depends(get_current_user)):
    fn = _ACTIONS.get(action)
    if fn is None:
        raise HTTPException(status_code=404, detail="Aksi tidak dikenal.")
    if action == "replacement" and not user.get("can_post_inbound"):
        # The replacement goods are posted as their own Barang Masuk (K-22).
        raise HTTPException(status_code=403, detail="Penerimaan barang pengganti memposting Barang Masuk. Anda belum diberi izin posting Barang Masuk.")
    return _for(user, await _run(fn, return_id, body, user))
