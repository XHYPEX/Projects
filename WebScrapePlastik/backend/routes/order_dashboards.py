"""Dashboard Sales Order and Dashboard Purchase Order: GET only, monitoring views over existing documents."""
import asyncio

from fastapi import APIRouter

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
async def overview_route():
    return await _run(od.overview)
