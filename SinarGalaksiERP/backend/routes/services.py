"""Service / Garansi: customer units in for service, warranty claim or a trip to the factory, plus Master
Service. The unit is the customer's (custody register, no stock). The factory's cost is admin-only."""

import asyncio
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend.auth import get_current_user
from backend.database import (
    SVC_COST_BEARERS, SVC_COST_KINDS, SVC_DECISIONS, SVC_FEE, SVC_HANDLING, SVC_LOCATIONS, SVC_MEDIA, SVC_NO_INVOICE_REASONS,
    SVC_PHOTO_STAGES, SVC_REPLACE_MODES, SVC_RESULTS, SVC_SHIP_MODES, SVC_STAGES, SVC_STOP_KINDS, SVC_STOP_SOURCES, SVC_WARRANTY,
    SVC_WARRANTY_BASIS,
    add_service_note, add_service_photo, confirm_service_out, create_service_fee_receipt, decide_service_swap, delete_service_override,
    delete_service_photo, post_service_swap,
    get_service_order, get_service_photo, handover_service, list_service_orders, list_service_profiles, mark_service_at_factory,
    receive_service_back, receive_service_order, record_internal_result, record_service_approval, record_service_diagnosis,
    record_service_replacement, record_service_return_shipment, save_service_order, save_service_override, save_service_profile,
    save_service_shipment, service_customer_summary, service_invoice_search, service_print_text, service_serial_item_ids, service_serial_skus,
    set_service_handling, set_service_profile_status, stop_service_order,
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


@router.get("/services/meta")
async def svc_meta():
    return {"stages": SVC_STAGES, "locations": SVC_LOCATIONS, "results": SVC_RESULTS, "warranty": SVC_WARRANTY,
            "warranty_basis": SVC_WARRANTY_BASIS, "fee": SVC_FEE, "handling": SVC_HANDLING, "no_invoice_reasons": SVC_NO_INVOICE_REASONS,
            "ship_modes": SVC_SHIP_MODES, "cost_bearers": SVC_COST_BEARERS, "decisions": SVC_DECISIONS, "cost_kinds": SVC_COST_KINDS,
            "stop_kinds": SVC_STOP_KINDS, "stop_sources": SVC_STOP_SOURCES, "media": SVC_MEDIA, "replace_modes": SVC_REPLACE_MODES,
            "photo_stages": SVC_PHOTO_STAGES}


@router.get("/services")
async def svc_list(tab: str = "semua", query: str | None = None, stage: str | None = None, warranty: str | None = None,
                   fee: str | None = None, handling: str | None = None, supplier_id: int | None = None, page: int = 1):
    if tab not in ("semua", "perlu_tindakan", "aktif", "selesai", "dibatalkan", "aftersales"):
        raise HTTPException(status_code=422, detail="Tab tidak dikenal.")
    return await _run(list_service_orders, tab, query, stage or None, warranty or None, fee or None, handling or None, supplier_id, page)


@router.get("/services/invoice-search")
async def svc_invoice_search(q: str = ""):
    return await _run(service_invoice_search, q)


@router.get("/services/customer-summary")
async def svc_customer_summary(phone: str = ""):
    return await _run(service_customer_summary, phone)


@router.get("/services/profiles")
async def svc_profiles(include_inactive: bool = False):
    return await _run(list_service_profiles, include_inactive)


@router.post("/services/profiles", status_code=201)
async def svc_profile_create(body: dict, user: dict = Depends(get_current_user)):
    return await _run(save_service_profile, None, body, user)


@router.put("/services/profiles/{profile_id}")
async def svc_profile_update(profile_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(save_service_profile, profile_id, body, user)


@router.post("/services/profiles/{profile_id}/status")
async def svc_profile_status(profile_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(set_service_profile_status, profile_id, bool(body.get("active")), user)


@router.post("/services/profiles/{profile_id}/overrides", status_code=201)
async def svc_override_add(profile_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(save_service_override, profile_id, body, user)


@router.delete("/services/overrides/{override_id}")
async def svc_override_delete(override_id: int, user: dict = Depends(get_current_user)):
    return await _run(delete_service_override, override_id, user)


@router.get("/services/serial-items")
async def svc_serial_items():
    return {"ids": await _run(service_serial_item_ids), "skus": await _run(service_serial_skus)}


@router.post("/services", status_code=201)
async def svc_create(body: dict, user: dict = Depends(get_current_user)):
    return await _run(save_service_order, None, body, user)


@router.get("/services/photos/{photo_id}")
async def svc_photo(photo_id: int):
    ph = await _run(get_service_photo, photo_id)
    return FileResponse(ph["path"], media_type=ph["mime"])


@router.delete("/services/photos/{photo_id}")
async def svc_photo_delete(photo_id: int, user: dict = Depends(get_current_user)):
    return await _run(delete_service_photo, photo_id, user)


@router.get("/services/{order_id}")
async def svc_get(order_id: int):
    return await _run(get_service_order, order_id)


@router.put("/services/{order_id}")
async def svc_update(order_id: int, body: dict, user: dict = Depends(get_current_user)):
    res = await _run(save_service_order, order_id, body, user)
    return await _run(get_service_order, order_id) if "stage" not in res else res


@router.post("/services/{order_id}/photos", status_code=201)
async def svc_photo_add(order_id: int, body: dict, user: dict = Depends(get_current_user)):
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
    await _run(add_service_photo, order_id, body.get("stage") or "", body.get("filename") or "", mime, data, user)
    return await _run(get_service_order, order_id)


@router.post("/services/{order_id}/print/{doc}")
async def svc_print(order_id: int, doc: str, user: dict = Depends(get_current_user)):
    return await _run(service_print_text, order_id, doc, user)


@router.post("/services/{order_id}/fee-receipt")
async def svc_fee_receipt(order_id: int, user: dict = Depends(get_current_user)):
    return await _run(create_service_fee_receipt, order_id, user)


_ACTIONS = {
    "receive": receive_service_order,
    "handling": set_service_handling,
    "internal": record_internal_result,
    "shipment": save_service_shipment,
    "out": confirm_service_out,
    "at-factory": mark_service_at_factory,
    "diagnosis": record_service_diagnosis,
    "approval": record_service_approval,
    "replacement": record_service_replacement,
    "swap-decide": decide_service_swap,
    "swap-post": post_service_swap,
    "return-shipment": record_service_return_shipment,
    "back": receive_service_back,
    "stop": stop_service_order,
    "handover": handover_service,
    "note": lambda oid, b, u: add_service_note(oid, b.get("note") or "", u),
}


@router.post("/services/{order_id}/{action}")
async def svc_action(order_id: int, action: str, body: dict, user: dict = Depends(get_current_user)):
    fn = _ACTIONS.get(action)
    if fn is None:
        raise HTTPException(status_code=404, detail="Aksi tidak dikenal.")
    return await _run(fn, order_id, body, user)
