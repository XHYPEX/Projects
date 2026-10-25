"""Account Payable: read-only control view of supplier debt. GET only; paying, allocating and verifying stay in
Faktur Pembelian, Purchase Order and Retur Supplier."""
import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend import account_payable as ap
from backend.database import parse_ymd

router = APIRouter(prefix="/account-payable")


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


def _filters(supplier_id, pay_status, due, date_from, date_to, q, invoice, po, outstanding) -> dict:
    for d in (date_from, date_to):
        if d:
            try:
                parse_ymd(d)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
    return {"supplier_id": supplier_id, "pay_status": pay_status, "due": due, "date_from": date_from, "date_to": date_to,
            "q": q, "invoice": invoice, "po": po, "outstanding": outstanding}


@router.get("/dashboard")
async def dashboard_route():
    return await _run(ap.dashboard)


@router.get("/invoices")
async def invoices_route(
    supplier_id: int | None = None, pay_status: str | None = Query(None, pattern="^(belum|sebagian|lunas)$"),
    due: str | None = Query(None, pattern="^(overdue|today|week|month)$"), date_from: str | None = None, date_to: str | None = None,
    q: str | None = None, invoice: str | None = None, po: str | None = None, outstanding: bool = False,
    sort: str = "due_date", dir: str = "asc", page: int = 1, page_size: int = Query(25, ge=0, le=5000),
):
    f = _filters(supplier_id, pay_status, due, date_from, date_to, q, invoice, po, outstanding)
    return await _run(ap.invoices, f, sort, dir == "desc", page, page_size)


@router.get("/invoices/{invoice_id}")
async def invoice_detail_route(invoice_id: int):
    data = await _run(ap.invoice_detail, invoice_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Faktur tidak ditemukan.")
    return data


@router.get("/due")
async def due_route(month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")):
    return await _run(ap.due_view, month)


@router.get("/dp")
async def dp_route():
    return await _run(ap.dp_view)


@router.get("/dp/{spo_payment_id}/targets")
async def dp_targets_route(spo_payment_id: int):
    data = await _run(ap.dp_targets, spo_payment_id)
    if data is None:
        raise HTTPException(status_code=404, detail="DP supplier tidak ditemukan.")
    return data


@router.get("/invoices/{invoice_id}/dp-options")
async def dp_options_route(invoice_id: int):
    data = await _run(ap.dp_options, invoice_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Faktur tidak ditemukan.")
    return data


@router.get("/credits")
async def credits_route():
    return await _run(ap.credit_view)


@router.get("/credits/{cn_id}")
async def credit_detail_route(cn_id: int):
    data = await _run(ap.credit_detail, cn_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Nota debit tidak ditemukan.")
    return data


@router.get("/refunds/{claim_id}")
async def refund_detail_route(claim_id: int):
    data = await _run(ap.refund_detail, claim_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Klaim refund tidak ditemukan.")
    return data


@router.get("/refunds")
async def refunds_route():
    return await _run(ap.refund_view)


@router.get("/reconciliation")
async def reconciliation_route():
    return await _run(ap.reconciliation)


@router.get("/aging")
async def aging_route():
    return await _run(ap.aging)


@router.get("/export")
async def export_route(
    kind: str = Query("invoices", pattern="^(invoices|aging)$"), supplier_id: int | None = None,
    pay_status: str | None = Query(None, pattern="^(belum|sebagian|lunas)$"), due: str | None = Query(None, pattern="^(overdue|today|week|month)$"),
    date_from: str | None = None, date_to: str | None = None, q: str | None = None, invoice: str | None = None, po: str | None = None,
    outstanding: bool = False, format: str = Query("csv", pattern="^(csv|xlsx)$"),
):
    f = _filters(supplier_id, pay_status, due, date_from, date_to, q, invoice, po, outstanding)
    if format == "xlsx":
        data = await _run(ap.export_xlsx, kind, f)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="aging-payable-{kind}.xlsx"'})
    body = await _run(ap.export_csv, kind, f)
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="account-payable-{kind}.csv"'})
