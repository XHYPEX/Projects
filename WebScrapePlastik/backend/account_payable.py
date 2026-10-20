"""Account Payable: a read-only control view over what the shop owes its suppliers.

Nothing here records money or creates documents. Every figure is read off the modules that already own it:
`purchase_invoices` + `purchase_invoice_payments` (Faktur Pembelian: cash payments, DP carried from the PO,
return credit), `spo_payments` (DP paid on a PO), `supplier_credit_notes` / `supplier_refund_claims`
(Retur Supplier and cancelled PO) and the PO / Goods Receipt rows. Paying, allocating and verifying stay in
those modules; this page only links to them.

No double counting: a faktur's "paid" is the sum of its own payment rows (cash + DP + credit). DP that has not
been carried onto a faktur and credit that has not been allocated are shown as separate balances, never
netted into the outstanding figure.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, timedelta

from backend import database as db
from backend import invoice_verify

PAY_KIND_LABELS = {"bayar": "Pembayaran", "batal": "Pembatalan pembayaran", "dp": "Uang muka (DP) dari PO",
                   "dp_batal": "DP dikembalikan ke PO", "kredit": "Nota kredit retur"}
PAY_STATUS_LABELS = {"belum": "Belum bayar", "sebagian": "Sebagian", "lunas": "Lunas"}


def _conn() -> sqlite3.Connection:
    return db._conn()


def _due_state(due: str | None, outstanding: int, today: date) -> dict:
    if outstanding <= 0:
        return {"state": "lunas", "label": "Lunas", "days": None}
    try:
        d = (date.fromisoformat(due) - today).days
    except (TypeError, ValueError):
        return {"state": "ok", "label": "Tanpa jatuh tempo", "days": None}
    if d < 0:
        return {"state": "overdue", "label": f"Lewat {-d} hari", "days": d}
    if d == 0:
        return {"state": "today", "label": "Hari ini", "days": 0}
    if d <= 7:
        return {"state": "soon", "label": f"{d} hari lagi", "days": d}
    return {"state": "ok", "label": f"{d} hari lagi", "days": d}


def _bucket(days: int | None) -> str | None:
    if days is None:
        return None
    if days > 0:
        return "not_due"
    if days == 0:
        return "today"
    return "overdue_1_30" if days >= -30 else "overdue_30_plus"


def _rows(con: sqlite3.Connection, today: date, statuses: tuple = ("posted",)) -> list[dict]:
    marks = ",".join("?" for _ in statuses)
    rows = [dict(r) for r in con.execute(
        f"""SELECT pi.id, pi.invoice_number, pi.supplier_invoice_no, pi.supplier_id, s.name AS supplier_name,
                   pi.supplier_po_id, sp.po_number, pi.invoice_date, pi.due_date, pi.total, pi.amount_paid, pi.status,
                   pi.payment_terms
            FROM purchase_invoices pi JOIN suppliers s ON s.id = pi.supplier_id
            LEFT JOIN supplier_pos sp ON sp.id = pi.supplier_po_id
            WHERE pi.status IN ({marks}) ORDER BY pi.due_date ASC, pi.id ASC""", statuses).fetchall()]
    parts: dict[int, dict[str, int]] = {}
    for r in con.execute("SELECT invoice_id, kind, SUM(amount) AS n FROM purchase_invoice_payments GROUP BY invoice_id, kind").fetchall():
        parts.setdefault(r["invoice_id"], {})[r["kind"]] = r["n"]
    for r in rows:
        p = parts.get(r["id"], {})
        r["paid"] = r["amount_paid"]
        r["outstanding"] = max(r["total"] - r["amount_paid"], 0)
        r["dp"] = p.get("dp", 0) + p.get("dp_batal", 0)
        r["cash"] = p.get("bayar", 0) + p.get("batal", 0)
        r["credit"] = p.get("kredit", 0)
        r["pay_status"] = db.compute_payment_status(r["total"], r["amount_paid"])
        r["pay_status_label"] = PAY_STATUS_LABELS[r["pay_status"]]
        r.update({"due_state": None, "due_label": None, "days": None})
        info = _due_state(r["due_date"], r["outstanding"], today)
        r["due_state"], r["due_label"], r["days"] = info["state"], info["label"], info["days"]
        r["bucket"] = _bucket(r["days"]) if r["outstanding"] > 0 else None
    return rows


def _buckets(rows: list[dict]) -> dict:
    out = {"not_due": 0, "today": 0, "overdue_1_30": 0, "overdue_30_plus": 0}
    for r in rows:
        if r["bucket"]:
            out[r["bucket"]] += r["outstanding"]
    out["total"] = sum(out.values())
    return out


def _month_back(today: date, n: int) -> str:
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _balances(con: sqlite3.Connection) -> dict:
    """DP paid on a PO that has not been carried onto a faktur, and credit notes not yet allocated: kept
    out of the outstanding total on purpose (they are assets of the shop, not part of the faktur)."""
    dp_free = con.execute(
        """SELECT COALESCE(SUM(p.amount - p.applied_amount), 0) AS n FROM spo_payments p
           JOIN supplier_pos o ON o.id = p.supplier_po_id WHERE o.status <> 'batal' AND p.amount - p.applied_amount > 0""").fetchone()["n"]
    credit_free = 0
    for cn in con.execute("SELECT id, amount FROM supplier_credit_notes WHERE kind='kredit' AND status='aktif'").fetchall():
        credit_free += max(cn["amount"] - db._sr_cn_allocated(con, cn["id"]), 0)
    claims = con.execute("SELECT COALESCE(SUM(amount - settled_amount), 0) AS n FROM supplier_refund_claims WHERE status='menunggu'").fetchone()["n"]
    return {"dp_free": dp_free, "credit_free": credit_free, "refund_claims_open": claims}


def dashboard() -> dict:
    today = db.shop_today()
    with _conn() as con:
        rows = _rows(con, today)
        drafts = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS t FROM purchase_invoices WHERE status='draft'").fetchone()
        verify = con.execute(
            """SELECT SUM(CASE WHEN verify_status IN ('belum') THEN 1 ELSE 0 END) AS waiting,
                      SUM(CASE WHEN verify_status IN ('ditahan','diajukan') THEN 1 ELSE 0 END) AS held,
                      SUM(CASE WHEN verify_status = 'diajukan' THEN 1 ELSE 0 END) AS ask_admin
               FROM purchase_invoices WHERE status='draft' AND supplier_po_id IS NOT NULL""").fetchone()
        balances = _balances(con)
        months = [_month_back(today, i) for i in range(4, -1, -1)]
        billed = {m: 0 for m in months}
        paid = {m: 0 for m in months}
        for r in con.execute("SELECT substr(invoice_date, 1, 7) AS m, SUM(total) AS n FROM purchase_invoices WHERE status='posted' GROUP BY m").fetchall():
            if r["m"] in billed:
                billed[r["m"]] = r["n"]
        for r in con.execute(
                """SELECT substr(p.paid_at, 1, 7) AS m, SUM(p.amount) AS n FROM purchase_invoice_payments p
                   JOIN purchase_invoices i ON i.id = p.invoice_id WHERE i.status='posted' GROUP BY m""").fetchall():
            if r["m"] in paid:
                paid[r["m"]] = r["n"]
    open_rows = [r for r in rows if r["outstanding"] > 0]
    b = _buckets(rows)
    due_today = [r for r in open_rows if r["bucket"] == "today"]
    overdue = [r for r in open_rows if r["bucket"] in ("overdue_1_30", "overdue_30_plus")]
    attention = [r for r in open_rows if r["due_state"] in ("overdue", "today", "soon")] or open_rows
    return {
        "as_of": today.isoformat(),
        "outstanding": b["total"], "open_count": len(open_rows), "posted_count": len(rows),
        "buckets": b,
        "not_due_count": sum(1 for r in open_rows if r["bucket"] == "not_due"),
        "due_today_count": len(due_today), "due_today_names": sorted({r["supplier_name"] for r in due_today}),
        "overdue_count": len(overdue), "overdue_total": b["overdue_1_30"] + b["overdue_30_plus"],
        "overdue_names": sorted({r["supplier_name"] for r in overdue}),
        "draft_count": drafts["n"], "draft_total": drafts["t"],
        "verify_waiting": verify["waiting"] or 0, "verify_held": verify["held"] or 0, "verify_ask_admin": verify["ask_admin"] or 0,
        "priority": [{"invoice_number": r["invoice_number"], "id": r["id"], "state": r["due_state"], "label": r["due_label"]}
                     for r in open_rows if r["due_state"] in ("overdue", "today")][:6],
        "trend": [{"month": m, "billed": billed[m], "paid": paid[m]} for m in months],
        "attention": attention[:8],
        "balances": balances,
    }


def invoices(filters: dict, sort: str = "due_date", desc: bool = False, page: int = 1, page_size: int = 25) -> dict:
    today = db.shop_today()
    with _conn() as con:
        rows = _rows(con, today)
        suppliers = [dict(r) for r in con.execute("SELECT DISTINCT s.id, s.name FROM purchase_invoices pi JOIN suppliers s ON s.id = pi.supplier_id WHERE pi.status='posted' ORDER BY s.name").fetchall()]
    f = filters
    q = (f.get("q") or "").lower().split()
    out = []
    for r in rows:
        if f.get("supplier_id") and r["supplier_id"] != int(f["supplier_id"]):
            continue
        if f.get("pay_status") and r["pay_status"] != f["pay_status"]:
            continue
        if f.get("outstanding") and r["outstanding"] <= 0:
            continue
        due = f.get("due")
        if due == "overdue" and r["bucket"] not in ("overdue_1_30", "overdue_30_plus"):
            continue
        if due == "today" and r["bucket"] != "today":
            continue
        if due == "week" and not (r["outstanding"] > 0 and r["days"] is not None and 0 <= r["days"] <= 7):
            continue
        if due == "month" and not (r["outstanding"] > 0 and r["days"] is not None and 0 <= r["days"] <= 30):
            continue
        if f.get("date_from") and (r["invoice_date"] or "") < f["date_from"]:
            continue
        if f.get("date_to") and (r["invoice_date"] or "") > f["date_to"]:
            continue
        if f.get("invoice") and f["invoice"].lower() not in f"{r['invoice_number']} {r['supplier_invoice_no'] or ''}".lower():
            continue
        if f.get("po") and f["po"].lower() not in (r["po_number"] or "").lower():
            continue
        hay = f"{r['invoice_number']} {r['supplier_invoice_no'] or ''} {r['supplier_name']} {r['po_number'] or ''}".lower()
        if q and not all(w in hay for w in q):
            continue
        out.append(r)
    keys = {"due_date": lambda r: (r["due_date"] or "", r["id"]), "invoice_date": lambda r: (r["invoice_date"] or "", r["id"]),
            "supplier": lambda r: r["supplier_name"].lower(), "total": lambda r: r["total"], "outstanding": lambda r: r["outstanding"],
            "invoice_number": lambda r: r["invoice_number"]}
    out.sort(key=keys.get(sort, keys["due_date"]), reverse=desc)
    total = len(out)
    summary = {"count": total, "total": sum(r["total"] for r in out), "paid": sum(r["paid"] for r in out),
               "outstanding": sum(r["outstanding"] for r in out)}
    if page_size and page_size > 0:
        out = out[(max(page, 1) - 1) * page_size: max(page, 1) * page_size]
    return {"items": out, "total": total, "page": page, "page_size": page_size, "summary": summary, "suppliers": suppliers}


def invoice_detail(invoice_id: int) -> dict | None:
    today = db.shop_today()
    with _conn() as con:
        inv = con.execute(
            """SELECT pi.*, s.name AS supplier_name, s.code AS supplier_code, sp.po_number, sp.status AS po_status
               FROM purchase_invoices pi JOIN suppliers s ON s.id = pi.supplier_id
               LEFT JOIN supplier_pos sp ON sp.id = pi.supplier_po_id WHERE pi.id=?""", (invoice_id,)).fetchone()
        if inv is None:
            return None
        inv = dict(inv)
        row = next((r for r in _rows(con, today, (inv["status"],)) if r["id"] == invoice_id), None) or {}
        items = [dict(r) for r in con.execute("SELECT * FROM purchase_invoice_items WHERE invoice_id=? ORDER BY line_no, id", (invoice_id,)).fetchall()]
        pays = [dict(r) for r in con.execute("SELECT * FROM purchase_invoice_payments WHERE invoice_id=? ORDER BY paid_at ASC, id ASC", (invoice_id,)).fetchall()]
        cn_by_no = {r["cn_number"]: dict(r) for r in con.execute(
            """SELECT c.*, r.return_number FROM supplier_credit_notes c LEFT JOIN supplier_returns r ON r.id = c.return_id
               WHERE c.cn_number IS NOT NULL""").fetchall()}
        for p in pays:
            p["kind_label"] = PAY_KIND_LABELS.get(p["kind"], p["kind"])
            cn = cn_by_no.get(p.get("reference_no")) if p["kind"] == "kredit" else None
            p["credit_note"] = {"cn_number": cn["cn_number"], "return_id": cn["return_id"], "return_number": cn.get("return_number")} if cn else None
        po = grs = None
        matching: list[dict] = []
        if inv["supplier_po_id"]:
            po = {"id": inv["supplier_po_id"], "po_number": inv["po_number"], "status": inv["po_status"]}
            grs = [dict(r) for r in con.execute(
                """SELECT r.id, r.gr_number, r.received_date, r.surat_jalan_no,
                          (SELECT COALESCE(SUM(l.quantity), 0) FROM spo_receipt_lines l WHERE l.receipt_id = r.id) AS qty
                   FROM spo_receipts r WHERE r.supplier_po_id=? ORDER BY r.received_date, r.id""", (inv["supplier_po_id"],)).fetchall()]
            matching = _matching(con, inv["supplier_po_id"], items)
        returns = {}
        for p in pays:
            if p["credit_note"] and p["credit_note"]["return_id"]:
                returns[p["credit_note"]["return_id"]] = p["credit_note"]["return_number"]
    detail = {k: inv[k] for k in ("id", "invoice_number", "supplier_invoice_no", "supplier_id", "supplier_name", "supplier_code",
                                  "invoice_date", "due_date", "payment_terms", "subtotal", "discount", "tax_amount", "shipping_cost",
                                  "total", "status", "notes", "void_reason")}
    return {**detail, "paid": row.get("paid", inv["amount_paid"]), "outstanding": row.get("outstanding", max(inv["total"] - inv["amount_paid"], 0)),
            "dp": row.get("dp", 0), "cash": row.get("cash", 0), "credit": row.get("credit", 0),
            "pay_status": row.get("pay_status"), "pay_status_label": row.get("pay_status_label"),
            "due_state": row.get("due_state"), "due_label": row.get("due_label"),
            "verify_status": inv["verify_status"], "verify_label": invoice_verify.STATUS_LABELS.get(inv["verify_status"], inv["verify_status"]),
            "verified_by": inv["verified_by"], "verified_at": inv["verified_at"], "hold_note": inv["hold_note"], "exception_reason": inv["exception_reason"],
            "po": po, "grs": grs or [], "items": items, "payments": pays, "matching": matching,
            "returns": [{"id": k, "return_number": v} for k, v in returns.items()],
            "can_pay": inv["status"] == "posted" and inv["total"] - inv["amount_paid"] > 0}


def _matching(con: sqlite3.Connection, po_id: str, items: list[dict]) -> list[dict]:
    return invoice_verify.match_lines(con, po_id, items)


def due_view(month: str | None = None) -> dict:
    today = db.shop_today()
    with _conn() as con:
        rows = [r for r in _rows(con, today) if r["outstanding"] > 0]
    ym = month or today.strftime("%Y-%m")
    y, m = int(ym[:4]), int(ym[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    days: dict[str, dict] = {}
    for r in rows:
        if r["due_date"] and first.isoformat() <= r["due_date"] < nxt.isoformat():
            d = days.setdefault(r["due_date"], {"count": 0, "total": 0, "overdue": False})
            d["count"] += 1
            d["total"] += r["outstanding"]
            d["overdue"] = d["overdue"] or r["due_state"] == "overdue"
    week = [r for r in rows if r["days"] is not None and 0 < r["days"] <= 7]
    month_ = [r for r in rows if r["days"] is not None and 8 <= r["days"] <= 30]
    over = [r for r in rows if r["due_state"] == "overdue"]
    td = [r for r in rows if r["due_state"] == "today"]
    s = lambda xs: sum(r["outstanding"] for r in xs)
    return {
        "as_of": today.isoformat(), "month": ym, "weekday_first": first.weekday(), "days_in_month": (nxt - first).days,
        "calendar": days,
        "kpi": {"overdue": {"total": s(over), "count": len(over), "max_days": max((-r["days"] for r in over), default=0)},
                "today": {"total": s(td), "count": len(td)}, "week": {"total": s(week), "count": len(week)},
                "month": {"total": s(month_), "count": len(month_)}, "all": {"total": s(rows), "count": len(rows)}},
        "priority": sorted(rows, key=lambda r: (r["due_date"] or "", r["id"])),
    }


def dp_view() -> dict:
    today = db.shop_today()
    with _conn() as con:
        used: dict[int, list[dict]] = {}
        for r in con.execute(
                """SELECT p.spo_payment_id, p.invoice_id, p.amount, i.invoice_number FROM purchase_invoice_payments p
                   JOIN purchase_invoices i ON i.id = p.invoice_id WHERE p.kind='dp' AND p.spo_payment_id IS NOT NULL""").fetchall():
            used.setdefault(r["spo_payment_id"], []).append({"invoice_id": r["invoice_id"], "invoice_number": r["invoice_number"], "amount": r["amount"]})
        rows = [dict(r) for r in con.execute(
            """SELECT p.id, p.supplier_po_id, p.paid_at, p.amount, p.applied_amount, p.method, p.reference_no, p.bank_account, p.note,
                      p.created_by, o.po_number, o.status AS po_status, s.name AS supplier_name, o.supplier_id
               FROM spo_payments p JOIN supplier_pos o ON o.id = p.supplier_po_id JOIN suppliers s ON s.id = o.supplier_id
               ORDER BY p.paid_at DESC, p.id DESC""").fetchall()]
    for r in rows:
        r["invoices"] = used.get(r["id"], [])
        r["free"] = max(r["amount"] - r["applied_amount"], 0)
        if r["po_status"] == "batal":
            r["status"], r["status_label"] = "batal", "PO dibatalkan"
        elif r["applied_amount"] >= r["amount"]:
            r["status"], r["status_label"] = "teralokasi", "Teralokasi"
        elif r["applied_amount"] > 0:
            r["status"], r["status_label"] = "sebagian", "Sebagian"
        else:
            r["status"], r["status_label"] = "tersedia", "Belum dialokasikan"
    live = [r for r in rows if r["po_status"] != "batal"]
    return {"as_of": today.isoformat(), "items": rows,
            "kpi": {"paid": sum(r["amount"] for r in live), "applied": sum(r["applied_amount"] for r in live),
                    "free": sum(r["free"] for r in live), "cancelled": sum(r["amount"] for r in rows if r["po_status"] == "batal"),
                    "count": len(rows)}}


def credit_view() -> dict:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT c.id, c.cn_number, c.supplier_cn_no, c.kind, c.amount, c.issued_at, c.status, c.return_id, c.supplier_po_id, c.claim_id,
                      c.method, c.reference_no, s.name AS supplier_name, r.return_number, o.po_number
               FROM supplier_credit_notes c JOIN suppliers s ON s.id = c.supplier_id
               LEFT JOIN supplier_returns r ON r.id = c.return_id LEFT JOIN supplier_pos o ON o.id = c.supplier_po_id
               WHERE c.kind='kredit' ORDER BY c.issued_at DESC, c.id DESC""").fetchall()]
        for r in rows:
            allocs = [dict(a) for a in con.execute(
                """SELECT a.invoice_id, i.invoice_number, SUM(a.amount) AS amount FROM supplier_credit_allocations a
                   JOIN purchase_invoices i ON i.id = a.invoice_id WHERE a.credit_note_id=? GROUP BY a.invoice_id HAVING SUM(a.amount) <> 0""", (r["id"],)).fetchall()]
            r["allocations"] = allocs
            r["allocated"] = sum(a["amount"] for a in allocs)
            r["free"] = max(r["amount"] - r["allocated"], 0) if r["status"] == "aktif" else 0
            r["status_label"] = "Dibatalkan" if r["status"] != "aktif" else ("Siap dialokasikan" if r["free"] else "Teralokasi")
    live = [r for r in rows if r["status"] == "aktif"]
    return {"items": rows, "kpi": {"total": sum(r["amount"] for r in live), "allocated": sum(r["allocated"] for r in live),
                                   "free": sum(r["free"] for r in live), "count": len(live)}}


def refund_view() -> dict:
    claims = db.list_refund_claims()
    with _conn() as con:
        received = [dict(r) for r in con.execute(
            """SELECT c.id, c.amount, c.issued_at, c.method, c.reference_no, c.status, c.return_id, c.claim_id, s.name AS supplier_name,
                      r.return_number, cl.claim_no, o.po_number
               FROM supplier_credit_notes c JOIN suppliers s ON s.id = c.supplier_id
               LEFT JOIN supplier_returns r ON r.id = c.return_id LEFT JOIN supplier_refund_claims cl ON cl.id = c.claim_id
               LEFT JOIN supplier_pos o ON o.id = c.supplier_po_id
               WHERE c.kind='refund' ORDER BY c.issued_at DESC, c.id DESC""").fetchall()]
    live = [c for c in claims if c["status"] != "dibatalkan"]
    return {"claims": claims, "received": received,
            "kpi": {"claimed": sum(c["amount"] for c in live), "settled": sum(c["settled_amount"] for c in live),
                    "open": sum(c["left"] for c in claims), "open_count": sum(1 for c in claims if c["status"] == "menunggu")}}


def reconciliation() -> dict:
    """Things worth a second look. Each row says what is odd and opens the document; nothing is changed here."""
    today = db.shop_today()
    out: list[dict] = []
    with _conn() as con:
        rows = _rows(con, today)
        flagged: set[int] = set()
        for r in rows:
            if not r["supplier_po_id"]:
                continue
            items = [dict(i) for i in con.execute("SELECT * FROM purchase_invoice_items WHERE invoice_id=?", (r["id"],)).fetchall()]
            bad = [m for m in _matching(con, r["supplier_po_id"], items) if m["result"] != "sesuai"]
            if bad:
                flagged.add(r["id"])
                qty_over = [m for m in bad if m["qty_gr"] is not None and m["qty_inv"] > m["qty_gr"]]
                value = sum((m["qty_inv"] - m["qty_gr"]) * m["price_inv"] for m in qty_over) or sum(abs((m["price_inv"] - (m["price_po"] or 0)) * m["qty_inv"]) for m in bad if m["price_po"] is not None)
                out.append({"kind": "selisih", "category": "Faktur melebihi GR / beda dari PO" if qty_over else "Selisih harga / barang di luar PO",
                            "ref_type": "invoice", "ref_id": r["id"], "number": r["invoice_number"], "supplier": r["supplier_name"],
                            "value": value, "detail": "; ".join(sorted({m["note"] for m in bad if m["note"]}))[:200], "status": "Perlu review"})
        seen: dict[tuple, list[dict]] = {}
        for r in con.execute("SELECT pi.*, s.name AS supplier_name FROM purchase_invoices pi JOIN suppliers s ON s.id = pi.supplier_id WHERE pi.status<>'void'").fetchall():
            key = (r["supplier_id"], (r["supplier_invoice_no"] or "").strip().lower())
            if key[1]:
                seen.setdefault(key, []).append(dict(r))
        for key, group in seen.items():
            if len(group) > 1:
                for g in group:
                    flagged.add(g["id"])
                    out.append({"kind": "duplikat", "category": "Nomor faktur supplier sama dipakai lebih dari satu kali", "ref_type": "invoice", "ref_id": g["id"],
                                "number": g["invoice_number"], "supplier": g["supplier_name"], "value": g["total"],
                                "detail": f"Faktur supplier {g['supplier_invoice_no']} muncul {len(group)}x", "status": "Cek duplikat"})
        for r in con.execute(
                """SELECT pi.id, pi.invoice_number, pi.total, pi.created_at, s.name AS supplier_name FROM purchase_invoices pi
                   JOIN suppliers s ON s.id = pi.supplier_id WHERE pi.status='draft' ORDER BY pi.created_at""").fetchall():
            age = (today - date.fromisoformat(r["created_at"][:10])).days
            if age >= 7:
                out.append({"kind": "draft", "category": "Faktur draft belum diposting", "ref_type": "invoice", "ref_id": r["id"], "number": r["invoice_number"],
                            "supplier": r["supplier_name"], "value": r["total"], "detail": f"Draft sudah {age} hari; belum masuk saldo utang", "status": "Ditahan"})
        for r in con.execute(
                """SELECT pi.id, pi.invoice_number, pi.total, pi.verify_status, pi.hold_note, s.name AS supplier_name FROM purchase_invoices pi
                   JOIN suppliers s ON s.id = pi.supplier_id WHERE pi.status='draft' AND pi.verify_status IN ('ditahan','diajukan') ORDER BY pi.id""").fetchall():
            out.append({"kind": "tahan", "category": "Faktur ditahan untuk klarifikasi" if r["verify_status"] == "ditahan" else "Menunggu persetujuan pengecualian Admin",
                        "ref_type": "invoice", "ref_id": r["id"], "number": r["invoice_number"], "supplier": r["supplier_name"], "value": r["total"],
                        "detail": (r["hold_note"] or "")[:200], "status": "Ditahan" if r["verify_status"] == "ditahan" else "Perlu Admin"})
        for r in con.execute(
                """SELECT o.id, o.po_number, s.name AS supplier_name, SUM(p.amount - p.applied_amount) AS free FROM spo_payments p
                   JOIN supplier_pos o ON o.id = p.supplier_po_id JOIN suppliers s ON s.id = o.supplier_id
                   WHERE o.status IN ('sebagian_diterima','diterima') AND p.amount - p.applied_amount > 0 GROUP BY o.id""").fetchall():
            has_faktur = con.execute("SELECT 1 FROM purchase_invoices WHERE supplier_po_id=? AND status<>'void'", (r["id"],)).fetchone()
            out.append({"kind": "dp", "category": "DP belum diperhitungkan ke faktur" if has_faktur else "Barang sudah diterima, belum ada faktur (DP menunggu)",
                        "ref_type": "po", "ref_id": r["id"], "number": r["po_number"], "supplier": r["supplier_name"], "value": r["free"],
                        "detail": "DP PO belum terpakai", "status": "Perlu keputusan"})
        for c in con.execute("SELECT c.id, c.cn_number, c.amount, c.supplier_id, s.name AS supplier_name FROM supplier_credit_notes c JOIN suppliers s ON s.id = c.supplier_id WHERE c.kind='kredit' AND c.status='aktif'").fetchall():
            free = c["amount"] - db._sr_cn_allocated(con, c["id"])
            owes = any(r["supplier_id"] == c["supplier_id"] and r["outstanding"] > 0 for r in rows)
            if free > 0 and owes:
                out.append({"kind": "kredit", "category": "Nota kredit belum dialokasikan padahal ada faktur terbuka", "ref_type": "credit", "ref_id": c["id"],
                            "number": c["cn_number"], "supplier": c["supplier_name"], "value": free, "detail": "Saldo kredit siap dipotongkan", "status": "Perlu alokasi"})
        for cl in con.execute("SELECT cl.*, s.name AS supplier_name, o.po_number FROM supplier_refund_claims cl JOIN suppliers s ON s.id = cl.supplier_id JOIN supplier_pos o ON o.id = cl.supplier_po_id WHERE cl.status='menunggu'").fetchall():
            out.append({"kind": "refund", "category": "Refund / kredit supplier belum diterima", "ref_type": "po", "ref_id": cl["supplier_po_id"], "number": cl["claim_no"],
                        "supplier": cl["supplier_name"], "value": cl["amount"] - cl["settled_amount"], "detail": f"Dari pembatalan {cl['po_number']}", "status": "Menunggu"})
        matched = sum(1 for r in rows if r["supplier_po_id"] and r["id"] not in flagged)
    count = lambda k: sum(1 for x in out if x["kind"] == k)
    return {"as_of": today.isoformat(), "items": out,
            "kpi": {"cocok": matched, "selisih": count("selisih"), "duplikat": count("duplikat"), "draft": count("draft"), "tahan": count("tahan"),
                    "alokasi": count("dp") + count("kredit"), "refund": count("refund")}}


def aging() -> dict:
    today = db.shop_today()
    with _conn() as con:
        rows = [r for r in _rows(con, today) if r["outstanding"] > 0]
    sup: dict[int, dict] = {}
    for r in rows:
        s = sup.setdefault(r["supplier_id"], {"supplier_id": r["supplier_id"], "supplier": r["supplier_name"], "not_due": 0, "today": 0,
                                              "overdue_1_30": 0, "overdue_30_plus": 0, "total": 0, "count": 0})
        s[r["bucket"]] += r["outstanding"]
        s["total"] += r["outstanding"]
        s["count"] += 1
    items = sorted(sup.values(), key=lambda s: -s["total"])
    b = _buckets(rows)
    return {"as_of": today.isoformat(), "items": items, "totals": b, "open_count": len(rows),
            "composition": {k: (round(b[k] * 100 / b["total"], 1) if b["total"] else 0) for k in ("not_due", "today", "overdue_1_30", "overdue_30_plus")}}


def export_csv(kind: str, filters: dict | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if kind == "aging":
        a = aging()
        w.writerow([f"Aging utang supplier per {a['as_of']}"])
        w.writerow(["Supplier", "Belum jatuh tempo", "Jatuh tempo hari ini", "Overdue 1-30 hari", "Overdue >30 hari", "Total outstanding"])
        for s in a["items"]:
            w.writerow([s["supplier"], s["not_due"], s["today"], s["overdue_1_30"], s["overdue_30_plus"], s["total"]])
        t = a["totals"]
        w.writerow(["TOTAL", t["not_due"], t["today"], t["overdue_1_30"], t["overdue_30_plus"], t["total"]])
    else:
        data = invoices(filters or {}, "due_date", False, 1, 0)
        w.writerow(["No. Faktur", "Faktur Supplier", "Supplier", "No. PO", "Tanggal Faktur", "Jatuh Tempo", "Total Tagihan", "DP", "Dibayar", "Kredit", "Sisa", "Status", "Jatuh Tempo (ket.)"])
        for r in data["items"]:
            w.writerow([r["invoice_number"], r["supplier_invoice_no"] or "", r["supplier_name"], r["po_number"] or "", r["invoice_date"], r["due_date"],
                        r["total"], r["dp"], r["cash"], r["credit"], r["outstanding"], r["pay_status_label"], r["due_label"]])
        s = data["summary"]
        w.writerow([])
        w.writerow(["Total tagihan", s["total"]])
        w.writerow(["Total dibayar", s["paid"]])
        w.writerow(["Total outstanding", s["outstanding"]])
    return buf.getvalue()
