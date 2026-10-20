import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user, require_admin
from backend.database import get_notices, get_sales_summary, list_cash_outs, list_pending_prices, list_price_events
from backend.schemas import SalesSummaryOut

router = APIRouter()


@router.get("/sales/summary", response_model=SalesSummaryOut)
async def sales_summary(date_from: str, date_to: str):
    try:
        date.fromisoformat(date_from)
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal awal tidak valid, gunakan format YYYY-MM-DD.")
    try:
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal akhir tidak valid, gunakan format YYYY-MM-DD.")
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Tanggal awal tidak boleh setelah tanggal akhir.")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_sales_summary, date_from, date_to)
    return SalesSummaryOut(**result)


@router.get("/cash-outs", dependencies=[Depends(require_admin)])
async def cash_outs(date_from: str, date_to: str, method: str | None = None, kind: str = "semua"):
    """K-20 Kas Keluar (admin): every refund in the range, with proof, who paid it out and the nota; totals per method."""
    for label, raw in (("awal", date_from), ("akhir", date_to)):
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Tanggal {label} tidak valid, gunakan format YYYY-MM-DD.")
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Tanggal awal tidak boleh setelah tanggal akhir.")
    if method and method not in ("cash", "transfer", "qris", "debit", "giro", "lainnya"):
        raise HTTPException(status_code=422, detail="Metode harus salah satu dari: cash, transfer, qris, debit, giro, lainnya.")
    if kind not in ("semua", "refund", "supplier"):
        raise HTTPException(status_code=422, detail="Jenis harus salah satu dari: semua, refund, supplier.")
    return await asyncio.get_event_loop().run_in_executor(None, list_cash_outs, date_from, date_to, method or None, kind)


@router.get("/price-requests/pending", dependencies=[Depends(require_admin)])
async def price_requests_pending():
    """K-22 (admin): special prices staff asked for, waiting for approval."""
    return await asyncio.get_event_loop().run_in_executor(None, list_pending_prices)


@router.get("/price-events")
async def price_events(scope: str, doc_id: str):
    """K-22: every request / approval / rejection on one nota or Sales Order, newest first."""
    if scope not in ("receipt", "preorder"):
        raise HTTPException(status_code=422, detail="scope harus receipt atau preorder.")
    return await asyncio.get_event_loop().run_in_executor(None, list_price_events, scope, doc_id)


@router.get("/notices")
async def notices(user: dict = Depends(get_current_user)):
    """Notice Center: what needs attention, computed from the documents (read-only). Supplier debt is admin-only."""
    return await asyncio.get_event_loop().run_in_executor(None, get_notices, user.get("role") == "admin")
