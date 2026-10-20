"""Cash Receipt: read-only payment report. GET only; there is nothing here that changes a payment."""
import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend import cash_receipts as cr
from backend.database import parse_ymd

router = APIRouter()


def _filters(date_from, date_to, q, invoice, so, method, account, status, cashier, doc=None) -> dict:
    for d in (date_from, date_to):
        if d:
            try:
                parse_ymd(d)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
    return {"date_from": date_from, "date_to": date_to, "q": q, "invoice": invoice, "so": so,
            "method": method, "account": account, "status": status, "cashier": cashier, "doc": doc}


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


@router.get("/cash-receipts")
async def list_route(
    date_from: str | None = None, date_to: str | None = None, q: str | None = None, invoice: str | None = None,
    so: str | None = None, method: str | None = None, account: str | None = None, status: str | None = None,
    cashier: str | None = None, doc: str | None = None, sort: str = "paid_at", dir: str = "desc", page: int = 1,
    page_size: int = Query(25, ge=0, le=5000),
):
    f = _filters(date_from, date_to, q, invoice, so, method, account, status, cashier, doc)
    return await _run(cr.list_cash_receipts, f, sort, dir != "asc", page, page_size)


@router.get("/cash-receipts/export")
async def export_route(
    date_from: str | None = None, date_to: str | None = None, q: str | None = None, invoice: str | None = None,
    so: str | None = None, method: str | None = None, account: str | None = None, status: str | None = None,
    cashier: str | None = None, doc: str | None = None, sort: str = "paid_at", dir: str = "desc",
):
    f = _filters(date_from, date_to, q, invoice, so, method, account, status, cashier, doc)
    body = await _run(cr.export_csv, f, sort, dir != "asc")
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="laporan-cash-receipt.csv"'})


@router.get("/cash-receipts/group")
async def group_route(type: str = Query(..., pattern="^(invoice|so)$"), id: str = Query(..., min_length=1)):
    data = await _run(cr.get_cash_receipt_group, type, id)
    if data is None:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan.")
    return data


@router.get("/cash-receipts/{key}")
async def detail_route(key: str):
    data = await _run(cr.get_cash_receipt, key)
    if data is None:
        raise HTTPException(status_code=404, detail="Cash Receipt tidak ditemukan.")
    return data
