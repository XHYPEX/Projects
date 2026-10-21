"""Master Karyawan, Proses Pengerjaan and the Insentif module.

`work_router` is open to every logged-in user (Kasir work: pick people, record progress, no money in it).
`router` is mounted behind require_admin: every incentive number lives there, so a Kasir account can't reach
them even by typing the URL. Approving, rejecting and deciding corrections also need the Owner flag."""
import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from backend import incentives as inc
from backend.auth import get_current_user, require_admin, require_owner
from backend.xlsx_min import build_xlsx

work_router = APIRouter()
router = APIRouter()


async def _run(fn, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
    except inc.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class EmployeeIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    is_technician: bool | None = None
    is_sales: bool | None = None
    user_id: int | None = None
    note: str | None = None
    is_active: bool | None = None


class WorkIn(BaseModel):
    status: str


class VersionIn(BaseModel):
    master_item_id: int
    effective_from: str
    tech_rate: str | float | None = None
    sales_rate: str | float | None = None
    is_active: bool = True
    reason: str | None = None


class ReasonIn(BaseModel):
    reason: str | None = None


class SplitIn(BaseModel):
    shares: dict[str, str | float]


class LinesIn(BaseModel):
    line_ids: list[int]
    reason: str | None = None
    note: str | None = None


class RequestIn(BaseModel):
    line_id: int | None = None
    receipt_item_id: int | None = None
    role: str | None = None
    new_rate: str | float | None = None
    shares: dict[str, str | float] | None = None
    reason: str | None = None


class DecideIn(BaseModel):
    note: str | None = None


class PolicyIn(BaseModel):
    teknisi: str
    sales: str


# ── Master Karyawan (list for everyone, edits for admins) ────────────────────

@work_router.get("/employees")
async def list_employees(active: bool = False, role: str | None = None, q: str | None = None, user: dict = Depends(get_current_user)):
    admin = user["role"] == "admin"
    rows = await _run(inc.list_employees, active or not admin, role, q)
    if admin:
        return rows
    # The Kasir only needs who can be picked.
    return [{"id": r["id"], "name": r["name"], "is_technician": r["is_technician"], "is_sales": r["is_sales"], "is_active": r["is_active"]}
            for r in rows]


@work_router.post("/employees", status_code=201, dependencies=[Depends(require_admin)])
async def create_employee(req: EmployeeIn, user: dict = Depends(get_current_user)):
    return await _run(inc.create_employee, req.model_dump(), user["username"])


@work_router.patch("/employees/{employee_id}", dependencies=[Depends(require_admin)])
async def update_employee(employee_id: int, req: EmployeeIn, user: dict = Depends(get_current_user)):
    return await _run(inc.update_employee, employee_id, req.model_dump(exclude_unset=True), user["username"])


# ── Proses Pengerjaan ─────────────────────────────────────────────────────────

@work_router.get("/work-orders")
async def work_orders(scope: str = "aktif", q: str | None = None):
    return await _run(inc.work_orders, scope, q)


@work_router.get("/work-orders/{receipt_id}")
async def work_order(receipt_id: str):
    return await _run(inc.work_order_detail, receipt_id)


@work_router.post("/receipts/{receipt_id}/items/{item_id}/work")
async def set_item_work(receipt_id: str, item_id: int, req: WorkIn, user: dict = Depends(get_current_user)):
    return await _run(inc.set_item_work, receipt_id, item_id, req.status, user)


# ── Insentif (admin only; Owner for decisions) ────────────────────────────────

@router.get("/incentives/settings")
async def get_settings():
    policy = await _run(inc.get_policy)
    return {"policy": policy, "teknisi_options": inc.POLICY_TECH, "sales_options": inc.POLICY_SALES}


@router.put("/incentives/settings")
async def put_settings(req: PolicyIn, user: dict = Depends(require_owner)):
    return {"policy": await _run(inc.set_policy, req.teknisi, req.sales, user["username"])}


@router.get("/incentives/rules")
async def list_rules(q: str | None = None, status: str | None = None, category_id: int | None = None):
    return await _run(inc.list_rules, q, status, category_id)


@router.get("/incentives/rules/export.xlsx")
async def export_rules(q: str | None = None, status: str | None = None, category_id: int | None = None):
    data = await _run(inc.list_rules, q, status, category_id, 5000)
    rows = [["Nama Produk", "SKU", "Kategori", "Teknisi", "Sales", "Dasar Hitung", "Berlaku Mulai", "Status", "Versi Berikutnya"]]
    for r in data["rows"]:
        cur, up = r["current"], r["upcoming"]
        rows.append([r["name"], r["sku"] or "", r["category_name"] or "", r["tech_rate_label"], r["sales_rate_label"],
                     "Harga bersih" if cur else "", cur["effective_from"] if cur else "", r["rule_state_label"],
                     f"V{up['version_no']} mulai {up['effective_from']}: teknisi {up['tech_rate_label']}, sales {up['sales_rate_label']}" if up else ""])
    return _xlsx(rows, "Master Insentif", "master-insentif")


@router.get("/incentives/rules/item/{master_item_id}")
async def rule_detail(master_item_id: int):
    return await _run(inc.rule_detail, master_item_id)


@router.post("/incentives/rules", status_code=201)
async def add_version(req: VersionIn, user: dict = Depends(get_current_user)):
    return await _run(inc.add_version, req.model_dump(), user["username"])


@router.post("/incentives/rules/versions/{version_id}/cancel")
async def cancel_version(version_id: int, req: ReasonIn, user: dict = Depends(get_current_user)):
    return await _run(inc.cancel_version, version_id, req.reason, user["username"])


@router.get("/incentives/monitoring")
async def monitoring(date_from: str | None = None, date_to: str | None = None, basis: str = "nota", employee_id: int | None = None,
                     role: str | None = None, status: str | None = None):
    return await _run(inc.monitoring, date_from, date_to, basis, employee_id, role, status)


@router.get("/incentives/monitoring/export.xlsx")
async def export_monitoring(date_from: str | None = None, date_to: str | None = None, basis: str = "nota", employee_id: int | None = None,
                            role: str | None = None, status: str | None = None):
    data = await _run(inc.monitoring, date_from, date_to, basis, employee_id, role, status)
    rows = [[f"Rekap Insentif {data['from']} s/d {data['to']} ({data['basis_label']})"], [],
            ["Karyawan", "Peran", "Asal Pekerjaan", "Nominal", "Status"]]
    for p in data["employees"]:
        rows.append([p["name"], p["role_label"], p["source_text"], p["amount"], p["status_label"]])
    rows += [[], ["Rincian per baris"],
             ["Tanggal Nota", "No. Nota", "Produk", "Peran", "Penerima", "Porsi", "Dasar", "Tarif", "Pool", "Nominal", "Status", "Sumber"]]
    for ln in data["lines"]:
        for s in ln["shares"]:
            amount = s["final"] if s["final"] is not None else (0 if ln["status"] in ("batal", "ditolak") else s["amount"])
            rows.append([ln["nota_date"], ln["receipt_no"], ln["product_name"], ln["role_label"], s["name"], s["share_label"], ln["base_amount"],
                         ln["rate_label"], ln["pool_amount"], amount, ln["status_label"], ln["source_label"]])
    return _xlsx(rows, "Rekap Insentif", f"rekap-insentif-{data['from']}-{data['to']}", bold_rows=(0, 2))


def _xlsx(rows, sheet: str, name: str, bold_rows=(0,)) -> Response:
    body = build_xlsx(rows, sheet, bold_rows=bold_rows)
    return Response(content=body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{datetime.now():%Y%m%d}.xlsx"'})


@router.get("/incentives/employees/{employee_id}")
async def employee_detail(employee_id: int, date_from: str | None = None, date_to: str | None = None, basis: str = "nota", role: str | None = None):
    return await _run(inc.employee_detail, employee_id, date_from, date_to, basis, role)


@router.get("/incentives/invoices/{receipt_id}")
async def invoice_detail(receipt_id: str):
    return await _run(inc.invoice_detail, receipt_id)


@router.post("/incentives/lines/{line_id}/split")
async def set_split(line_id: int, req: SplitIn, user: dict = Depends(get_current_user)):
    return await _run(inc.set_split, line_id, req.shares, user["username"])


@router.post("/incentives/approve")
async def approve(req: LinesIn, user: dict = Depends(require_owner)):
    return await _run(inc.approve_lines, req.line_ids, user["username"], req.note)


@router.post("/incentives/reject")
async def reject(req: LinesIn, user: dict = Depends(require_owner)):
    return await _run(inc.reject_lines, req.line_ids, req.reason, user["username"])


@router.post("/incentives/lines/{line_id}/reopen")
async def reopen(line_id: int, req: ReasonIn, user: dict = Depends(require_owner)):
    return await _run(inc.reopen_line, line_id, req.reason, user["username"])


@router.get("/incentives/requests")
async def list_requests(state: str | None = None):
    return await _run(inc.list_requests, state)


@router.get("/incentives/requests/{request_id}")
async def get_request(request_id: int):
    return await _run(inc.get_request, request_id)


@router.post("/incentives/requests", status_code=201)
async def create_request(req: RequestIn, user: dict = Depends(get_current_user)):
    return await _run(inc.create_request, req.model_dump(), user["username"])


@router.post("/incentives/requests/{request_id}/approve")
async def approve_request(request_id: int, req: DecideIn, user: dict = Depends(require_owner)):
    return await _run(inc.decide_request, request_id, True, req.note, user["username"])


@router.post("/incentives/requests/{request_id}/reject")
async def reject_request(request_id: int, req: DecideIn, user: dict = Depends(require_owner)):
    return await _run(inc.decide_request, request_id, False, req.note, user["username"])


@router.get("/incentives/journal")
async def journal():
    return await _run(inc.journal)
