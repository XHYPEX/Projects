"""Dashboard Sales Order and Dashboard Purchase Order: GET only, monitoring views over existing documents."""
import asyncio

from fastapi import APIRouter, Depends

from backend.auth import get_current_user

from backend import order_dashboards as od

router = APIRouter(prefix="/dashboards")


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


@router.get("/sales-orders")
async def sales_orders_route():
    return await _run(od.so_dashboard)


@router.get("/purchase-orders")
async def purchase_orders_route():
    return await _run(od.po_dashboard)


@router.get("/overview")
async def overview_route(user: dict = Depends(get_current_user)):
    data = await _run(od.overview)
    if user.get("role") != "admin":
        # Owner 28-10-2026: the month's cash in and sales are for admins only (like Cash Receipt).
        data.update(cash_month=0, cash_prev=0, sales_month=0, sales_prev=0)
    return data
