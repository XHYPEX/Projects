"""Penawaran Harga (Sales) and Pengaturan Perusahaan. Every signed-in user works on quotations; approving a special
price and changing the shop identity or its bank accounts are Admin only (checked here and in the modules)."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend import company
from backend import quotations as qt
from backend.auth import get_current_user, require_admin

router = APIRouter()


async def _run(fn, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except qt.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── Pengaturan Perusahaan ─────────────────────────────────────────────────────
@router.get("/company-settings")
async def company_get():
    return await _run(company.settings)


@router.put("/company-settings")
async def company_put(body: dict, user: dict = Depends(require_admin)):
    return await _run(company.save_settings, body, user)


@router.get("/company-bank-accounts")
async def bank_list(all: bool = False, user: dict = Depends(get_current_user)):
    return await _run(company.list_accounts, all and user["role"] == "admin")


@router.post("/company-bank-accounts", status_code=201)
async def bank_add(body: dict, user: dict = Depends(require_admin)):
    return await _run(company.save_account, None, body, user)


@router.put("/company-bank-accounts/{account_id}")
async def bank_put(account_id: int, body: dict, user: dict = Depends(require_admin)):
    return await _run(company.save_account, account_id, body, user)


# ── Penawaran Harga ───────────────────────────────────────────────────────────
@router.get("/quotations")
async def quote_list(q: str | None = None, recipient_type: str | None = None, status: str | None = None, month: str | None = None):
    return await _run(qt.list_quotes, {"q": q, "recipient_type": recipient_type, "status": status, "month": month})


@router.get("/quotations/next-number")
async def quote_next_number():
    return {"quote_number": await _run(qt.next_number)}


@router.get("/quotations/templates")
async def quote_template(template: str = "ramah", recipient: str = ""):
    if template not in qt.TEMPLATES:
        raise HTTPException(status_code=422, detail="Template naskah tidak dikenal.")
    return await _run(qt.default_texts, template, recipient)


@router.get("/quotations/products")
async def quote_products(q: str | None = None, category_id: int | None = None, inactive: bool = False):
    return await _run(qt.products, q, category_id, inactive)


@router.post("/quotations", status_code=201)
async def quote_create(body: dict, user: dict = Depends(get_current_user)):
    body.pop("source_quote_id", None)
    return await _run(qt.create, body, user)


@router.get("/quotations/{qid}")
async def quote_detail(qid: int, user: dict = Depends(get_current_user)):
    return await _run(qt.detail, qid, user)


@router.put("/quotations/{qid}")
async def quote_update(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.update_draft, qid, body, user)


@router.get("/quotations/{qid}/preview")
async def quote_preview(qid: int, theme: str | None = None):
    return await _run(qt.preview, qid, theme)


@router.post("/quotations/{qid}/issue")
async def quote_issue(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.issue, qid, body.get("token"), user, body.get("theme"))


@router.get("/quotations/{qid}/versions/{vid}")
async def quote_version(qid: int, vid: int):
    return await _run(qt.version, qid, vid)


@router.post("/quotations/{qid}/versions/{vid}/printed")
async def quote_printed(qid: int, vid: int, user: dict = Depends(get_current_user)):
    return await _run(qt.log_print, qid, vid, user)


@router.post("/quotations/{qid}/lines/{line_id}/price-decision")
async def quote_price_decision(qid: int, line_id: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.decide_price, qid, line_id, bool(body.get("approve")), body.get("note"), user)


@router.post("/quotations/{qid}/discount-decision")
async def quote_discount_decision(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.decide_discount, qid, bool(body.get("approve")), body.get("note"), user)


@router.post("/quotations/{qid}/sent")
async def quote_sent(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.mark_sent, qid, body, user)


@router.put("/quotations/{qid}/follow-up")
async def quote_follow_up(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.set_follow_up, qid, body, user)


@router.post("/quotations/{qid}/decision")
async def quote_decision(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.decide, qid, body, user)


@router.post("/quotations/{qid}/revisions")
async def quote_revise(qid: int, user: dict = Depends(get_current_user)):
    return await _run(qt.revise, qid, user)


@router.post("/quotations/{qid}/revisions/cancel")
async def quote_revise_cancel(qid: int, user: dict = Depends(get_current_user)):
    return await _run(qt.cancel_revision, qid, user)


@router.post("/quotations/{qid}/duplicate", status_code=201)
async def quote_duplicate(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.duplicate, qid, body, user)


@router.post("/quotations/{qid}/cancel")
async def quote_cancel(qid: int, body: dict, user: dict = Depends(get_current_user)):
    return await _run(qt.cancel, qid, body.get("reason") or "", user)


# ── used by Kasir / Sales Order when they save a document made from a quotation ──
async def guard_conversion(qid: int | None) -> None:
    """Refuse before saving: the quotation must be accepted, issued and not converted yet."""
    if qid:
        await _run(qt.check_convertible, qid)


async def record_conversion(qid: int | None, target_type: str, target_id: str, target_no: str | None, user: dict) -> None:
    """After the nota / SO is saved. It already exists, so a race lost here is not an error for that save."""
    if qid:
        try:
            await _run(qt.link_conversion, qid, target_type, target_id, target_no, user)
        except HTTPException:
            pass
