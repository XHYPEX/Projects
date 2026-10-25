"""Debit Note Supplier: Draft → verifikasi → posting → alokasi / refund → reversal. Admin only (the whole router
sits behind require_admin); approving a reversal additionally needs the Owner flag, checked in the module."""
import asyncio
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend import credit_notes as cn
from backend.auth import get_current_user
from backend.database import parse_ymd
from backend.product_images import matches_content_type

router = APIRouter(prefix="/credit-notes")


async def _run(fn, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except cn.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _dates(*ds):
    for d in ds:
        if d:
            try:
                parse_ymd(d)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))


@router.get("")
async def cn_list(tab: str = "semua", status: str | None = None, supplier_id: int | None = None, date_from: str | None = None,
                  date_to: str | None = None, q: str | None = None):
    if tab not in ("semua", "proses", "tersedia", "teralokasi", "ditutup"):
        raise HTTPException(status_code=422, detail="Tab tidak dikenal.")
    _dates(date_from, date_to)
    return await _run(cn.list_notes, {"tab": tab, "status": status or None, "supplier_id": supplier_id, "date_from": date_from,
                                      "date_to": date_to, "q": q or None})


@router.get("/audit")
async def cn_audit(cn_id: int | None = None, supplier_id: int | None = None, kind: str | None = None, user: str | None = None,
                   date_from: str | None = None, date_to: str | None = None, q: str | None = None):
    _dates(date_from, date_to)
    return await _run(cn.audit, {"cn_id": cn_id, "supplier_id": supplier_id, "kind": kind or None, "user": user or None,
                                 "date_from": date_from, "date_to": date_to, "q": q or None})


@router.get("/returns")
async def cn_returns():
    return await _run(cn.creditable_returns)


@router.post("/from-return/{return_id}")
async def cn_from_return(return_id: int, body: dict | None = None, user: dict = Depends(get_current_user)):
    return await _run(cn.create_from_return, return_id, user, (body or {}).get("note"))


@router.get("/files/{file_id}")
async def cn_file(file_id: int):
    f = await _run(cn.get_file, file_id)
    return FileResponse(f["path"], media_type=f["mime"], filename=f["filename"])


@router.post("/reversals/{reversal_id}/decide")
async def cn_reversal_decide(reversal_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.decide_reversal, reversal_id, bool(body.get("approve")), body.get("note"), user)


@router.get("/{cn_id}")
async def cn_detail(cn_id: int, user: dict = Depends(get_current_user)):
    return await _run(cn.detail, cn_id, user)


@router.put("/{cn_id}")
async def cn_update(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    if "issued_at" in body and body["issued_at"]:
        _dates(body["issued_at"])
    return await _run(cn.update_draft, cn_id, body, user)


@router.post("/{cn_id}/submit")
async def cn_submit(cn_id: int, body: dict | None = None, user: dict = Depends(get_current_user)):
    return await _run(cn.submit, cn_id, (body or {}).get("note"), user)


@router.post("/{cn_id}/approve")
async def cn_approve(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.approve, cn_id, body, user)


@router.post("/{cn_id}/send-back")
async def cn_send_back(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.send_back, cn_id, body.get("note") or "", user)


@router.post("/{cn_id}/reject")
async def cn_reject(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.reject, cn_id, body.get("reason") or "", user)


@router.post("/{cn_id}/post")
async def cn_post(cn_id: int, user: dict = Depends(get_current_user)):
    return await _run(cn.post, cn_id, user)


@router.post("/{cn_id}/allocate")
async def cn_allocate(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.allocate, cn_id, body, user)


@router.post("/{cn_id}/allocations/{allocation_id}/cancel")
async def cn_unallocate(cn_id: int, allocation_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.unallocate, cn_id, allocation_id, body.get("reason") or "", user)


@router.post("/{cn_id}/refunds")
async def cn_refund(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    if body.get("received_at"):
        _dates(body["received_at"])
    return await _run(cn.record_refund, cn_id, body, user)


@router.post("/{cn_id}/refunds/{refund_id}/cancel")
async def cn_refund_cancel(cn_id: int, refund_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.cancel_refund, cn_id, refund_id, body.get("reason") or "", user)


@router.post("/{cn_id}/reversal")
async def cn_reversal(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(cn.request_reversal, cn_id, body, user)


@router.post("/{cn_id}/files", status_code=201)
async def cn_file_add(cn_id: int, body: dict, user: dict = Depends(get_current_user)):
    mime = (body.get("mime") or "").lower()
    raw = body.get("data") or ""
    if "," in raw and raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="File tidak valid.")
    if mime != "application/pdf" and not matches_content_type(mime, data):
        raise HTTPException(status_code=422, detail="Isi file tidak cocok dengan jenisnya.")
    if mime == "application/pdf" and not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Isi file bukan PDF.")
    refund_id = int(body["refund_id"]) if body.get("refund_id") else None
    return await _run(cn.add_file, cn_id, body.get("filename") or "", mime, data, user, refund_id)


@router.delete("/{cn_id}/files/{file_id}")
async def cn_file_delete(cn_id: int, file_id: int, user: dict = Depends(get_current_user)):
    return await _run(cn.delete_file, cn_id, file_id, user)
