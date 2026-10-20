"""Cash Receipt: a read-only report of the money customers have paid the shop.

There is no table of "cash receipts" that anything writes to. The report is read off the two places a
customer payment is recorded: `receipt_payments` (money on an invoice, incl. refunds and reversals) and
`preorder_payments` (a Sales Order's DP *before* it has an invoice). A DP that has been carried onto an
invoice exists in both tables; it is shown once, under the number it was first given.

The only thing stored here is the receipt number (`cash_receipt_numbers`), handed out the first time a
payment is seen so that it never changes afterwards. Nothing in this module edits a payment.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta

from backend import database as db
from backend.config import SHOP_UTC_OFFSET_MINUTES

METHOD_LABELS = {"cash": "Tunai", "transfer": "Transfer Bank", "qris": "QRIS", "debit": "Kartu Debit"}
STATUS_LABELS = {"berhasil": "Berhasil", "pending": "Pending", "gagal": "Gagal", "dibatalkan": "Dibatalkan",
                 "refund": "Refund", "pembalikan": "Pembalikan"}
KIND_LABELS = {"masuk": "Penerimaan", "dp": "Penerimaan DP", "refund": "Refund Customer", "pembalikan": "Pembatalan Pembayaran"}
SORTS = {"paid_at", "amount", "customer", "no", "method", "status"}


def _conn() -> sqlite3.Connection:
    return db._conn()


def _ensure(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS cash_receipt_numbers (
               src TEXT NOT NULL,
               src_id INTEGER NOT NULL,
               no TEXT NOT NULL,
               created_at TEXT NOT NULL,
               PRIMARY KEY (src, src_id)
           )"""
    )


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)
    except ValueError:
        return None


def _dest(method: str, account: str | None) -> str:
    if method == "cash":
        return "Kas Toko"
    if method == "qris":
        return "QRIS"
    if method == "debit":
        return "Kartu Debit / EDC"
    return (account or "").strip() or "Rekening belum dicatat"


def _customer_code(cid: int | None) -> str | None:
    return f"CUST-{cid:04d}" if cid else None


def _load_entries(con: sqlite3.Connection) -> list[dict]:
    """Every payment event as one normalised row, without numbers yet."""
    pp_rows = [dict(r) for r in con.execute(
        """SELECT p.*, o.po_number, o.customer_name, o.customer_phone, o.plate_full, o.receipt_id AS so_receipt_id
           FROM preorder_payments p JOIN preorders o ON o.id = p.preorder_id ORDER BY p.id ASC""").fetchall()]
    rp_rows = [dict(r) for r in con.execute(
        """SELECT p.*, r.receipt_no, r.customer_name, r.customer_phone, r.plate_full, r.customer_id, r.preorder_id,
                  r.status AS receipt_status, o.po_number
           FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id
           LEFT JOIN preorders o ON o.id = r.preorder_id
           WHERE COALESCE(p.kind, '') <> 'pindah' ORDER BY p.id ASC""").fetchall()]
    reversed_ids = {r["reversed_of"] for r in rp_rows if r.get("reversed_of")}

    # A DP carried onto an invoice appears there as an ordinary payment: pair it with the Sales Order payment it came from.
    pp_by_so: dict[str, list[dict]] = {}
    for p in pp_rows:
        if p["state"] in ("sah", "dipindah"):
            pp_by_so.setdefault(p["preorder_id"], []).append({"row": p, "left": p["amount"]})
    origin: dict[int, dict] = {}
    for r in rp_rows:
        so_id = r.get("preorder_id")
        if not so_id or r["amount"] <= 0 or r.get("reversed_of") or r.get("kind"):
            continue
        pool = pp_by_so.get(so_id) or []
        looks_dp = (r.get("note") or "").startswith("DP dari") or any(c["row"]["paid_at"] == r["paid_at"] for c in pool)
        if not looks_dp:
            continue
        for c in pool:
            if c["left"] >= r["amount"] and (c["row"]["method"] or "cash") == (r["method"] or "cash"):
                c["left"] -= r["amount"]
                origin[r["id"]] = c["row"]
                break

    entries: list[dict] = []
    for r in rp_rows:
        amount = r["amount"]
        if r.get("reversed_of"):
            kind = "pembalikan"
        elif r.get("kind") == "refund":
            kind = "refund"
        elif amount > 0:
            kind = "masuk"
        else:
            kind = "pembalikan"
        state = r.get("state") or "sah"
        if kind == "masuk":
            status = "dibatalkan" if r["id"] in reversed_ids else {"sah": "berhasil", "pending": "pending", "gagal": "gagal"}.get(state, "berhasil")
        else:
            status = kind
        org = origin.get(r["id"])
        method = r.get("method") or "cash"
        entries.append({
            "src": "rp", "src_id": r["id"], "key": f"rp-{r['id']}", "canon": ("pp", org["id"]) if org else ("rp", r["id"]),
            "paid_at": r["paid_at"], "kind": kind, "is_dp": bool(org), "amount": abs(amount), "method": method,
            "dest": _dest(method, r.get("transfer_account")), "account": r.get("transfer_account") if method == "transfer" else None,
            "status": status, "state": state, "customer_name": r.get("customer_name") or "", "customer_phone": r.get("customer_phone"),
            "customer_id": r.get("customer_id"), "plate": r.get("plate_full"),
            "doc_type": "invoice", "doc_id": r["receipt_id"], "doc_no": r.get("receipt_no"),
            "so_id": r.get("preorder_id"), "so_no": r.get("po_number"),
            "invoice_id": r["receipt_id"], "invoice_no": r.get("receipt_no"), "invoice_status": r.get("receipt_status"),
            "cashier": r.get("created_by"), "note": r.get("note"), "proof_ref": r.get("proof_ref"), "sender": r.get("transfer_sender"),
            "confirmed_by": r.get("confirmed_by"), "confirmed_at": r.get("confirmed_at"), "fail_reason": r.get("fail_reason"),
            "reversed_of_key": f"rp-{r['reversed_of']}" if r.get("reversed_of") else None,
        })
    for p in pp_rows:
        if p["state"] not in ("sah", "pending", "gagal") or p.get("so_receipt_id"):
            continue  # moved / already on an invoice: the invoice's row stands for it
        method = p.get("method") or "cash"
        entries.append({
            "src": "pp", "src_id": p["id"], "key": f"pp-{p['id']}", "canon": ("pp", p["id"]),
            "paid_at": p["paid_at"], "kind": "masuk", "is_dp": True, "amount": p["amount"], "method": method,
            "dest": _dest(method, None), "account": None,
            "status": {"sah": "berhasil", "pending": "pending", "gagal": "gagal"}[p["state"]], "state": p["state"],
            "customer_name": p.get("customer_name") or "", "customer_phone": p.get("customer_phone"), "customer_id": None,
            "plate": p.get("plate_full"), "doc_type": "so", "doc_id": p["preorder_id"], "doc_no": p.get("po_number"),
            "so_id": p["preorder_id"], "so_no": p.get("po_number"), "invoice_id": None, "invoice_no": None, "invoice_status": None,
            "cashier": p.get("created_by"), "note": p.get("note"), "proof_ref": None, "sender": None,
            "confirmed_by": p.get("confirmed_by"), "confirmed_at": p.get("confirmed_at"), "fail_reason": p.get("fail_reason"),
            "reversed_of_key": None,
        })
    return entries


def _assign_numbers(con: sqlite3.Connection, entries: list[dict]) -> dict[tuple, str]:
    """Number every payment the first time it is seen. A DP carried onto an invoice keeps its Sales Order number."""
    existing = {(r["src"], r["src_id"]): r["no"] for r in con.execute("SELECT src, src_id, no FROM cash_receipt_numbers").fetchall()}
    need = []
    seen: set[tuple] = set()
    for e in sorted(entries, key=lambda x: (x["paid_at"] or "", x["src"], x["src_id"])):
        if e["canon"] not in existing and e["canon"] not in seen:
            need.append(e)
            seen.add(e["canon"])
    if not need:
        return existing
    con.execute("BEGIN IMMEDIATE")
    try:
        existing = {(r["src"], r["src_id"]): r["no"] for r in con.execute("SELECT src, src_id, no FROM cash_receipt_numbers").fetchall()}
        counters: dict[str, int] = {}
        for e in need:
            if e["canon"] in existing:
                continue
            local = _local(e["paid_at"]) or datetime.utcnow()
            prefix = "RF" if e["kind"] in ("refund", "pembalikan") else "CR"
            stem = f"{prefix}-{local:%Y%m%d}-"
            if stem not in counters:
                counters[stem] = con.execute("SELECT COUNT(*) AS n FROM cash_receipt_numbers WHERE no LIKE ?", (stem + "%",)).fetchone()["n"]
            counters[stem] += 1
            no = f"{stem}{counters[stem]:03d}"
            con.execute("INSERT INTO cash_receipt_numbers (src, src_id, no, created_at) VALUES (?, ?, ?, ?)",
                        (e["canon"][0], e["canon"][1], no, db._now()))
            existing[e["canon"]] = no
        con.commit()
    except Exception:
        con.rollback()
        raise
    return existing


def _attribute_outflows(entries: list[dict]) -> None:
    """A refund or reversal has no account of its own: it goes back out of the account the money came in to."""
    by_key = {e["key"]: e for e in entries}
    for e in entries:
        if e["kind"] == "masuk" or e["method"] != "transfer" or e["account"]:
            continue
        src = by_key.get(e["reversed_of_key"]) if e["reversed_of_key"] else next(
            (x for x in entries if x["kind"] == "masuk" and x["invoice_id"] == e["invoice_id"] and x["method"] == "transfer" and x["account"]), None)
        if src:
            e["dest"], e["account"] = src["dest"], src["account"]


def _all(con: sqlite3.Connection) -> list[dict]:
    _ensure(con)
    entries = _load_entries(con)
    _attribute_outflows(entries)
    numbers = _assign_numbers(con, entries)
    for e in entries:
        e["no"] = numbers.get(e["canon"])
        e.pop("canon", None)
        loc = _local(e["paid_at"])
        e["paid_local"] = loc.strftime("%Y-%m-%d %H:%M") if loc else None
        e["paid_date"] = loc.strftime("%Y-%m-%d") if loc else None
        conf = _local(e.get("confirmed_at"))
        e["confirmed_local"] = conf.strftime("%Y-%m-%d %H:%M") if conf else None
        e["method_label"] = METHOD_LABELS.get(e["method"], e["method"])
        e["status_label"] = STATUS_LABELS.get(e["status"], e["status"])
        e["kind_label"] = KIND_LABELS["dp"] if (e["kind"] == "masuk" and e["is_dp"]) else KIND_LABELS.get(e["kind"], e["kind"])
        e["customer_code"] = _customer_code(e.get("customer_id"))
        e["signed"] = e["amount"] if e["kind"] == "masuk" else -e["amount"]
    return entries


def _matches(e: dict, f: dict) -> bool:
    if f.get("date_from") and (e["paid_date"] or "") < f["date_from"]:
        return False
    if f.get("date_to") and (e["paid_date"] or "") > f["date_to"]:
        return False
    if f.get("method") and e["method"] != f["method"]:
        return False
    if f.get("account") and e["dest"] != f["account"]:
        return False
    if f.get("status") and e["status"] != f["status"]:
        return False
    if f.get("cashier") and (e["cashier"] or "") != f["cashier"]:
        return False
    if f.get("doc"):
        needle = f["doc"].lower()
        if needle not in " ".join(str(e.get(k) or "") for k in ("no", "invoice_no", "so_no")).lower():
            return False
    if f.get("invoice") and f["invoice"].lower() not in (e["invoice_no"] or "").lower():
        return False
    if f.get("so") and f["so"].lower() not in (e["so_no"] or "").lower():
        return False
    q = (f.get("q") or "").strip().lower()
    if q:
        hay = " ".join(str(e.get(k) or "") for k in ("customer_name", "customer_phone", "plate", "no", "invoice_no", "so_no")).lower()
        if not all(w in hay for w in q.split()):
            return False
    return True


def _summary(rows: list[dict]) -> dict:
    gross = {m: 0 for m in METHOD_LABELS}
    refund = {m: 0 for m in METHOD_LABELS}
    count_in = {m: 0 for m in METHOD_LABELS}
    gross_n = refund_n = 0
    pending_n = pending_total = 0
    by_account: dict[str, int] = {}
    by_cashier: dict[str, int] = {}
    for e in rows:
        if e["kind"] == "masuk":
            if e["status"] in ("berhasil", "dibatalkan"):
                gross[e["method"]] = gross.get(e["method"], 0) + e["amount"]
                count_in[e["method"]] = count_in.get(e["method"], 0) + 1
                gross_n += 1
                by_account[e["dest"]] = by_account.get(e["dest"], 0) + e["amount"]
                by_cashier[e["cashier"] or "—"] = by_cashier.get(e["cashier"] or "—", 0) + e["amount"]
            elif e["status"] == "pending":
                pending_n += 1
                pending_total += e["amount"]
        elif e["state"] == "sah":
            refund[e["method"]] = refund.get(e["method"], 0) + e["amount"]
            refund_n += 1
            by_account[e["dest"]] = by_account.get(e["dest"], 0) - e["amount"]
    g, rf = sum(gross.values()), sum(refund.values())
    return {
        "count": len(rows), "gross": g, "refund": rf, "net": g - rf, "gross_count": gross_n, "refund_count": refund_n,
        "by_method": {m: {"gross": gross.get(m, 0), "refund": refund.get(m, 0), "net": gross.get(m, 0) - refund.get(m, 0), "count": count_in.get(m, 0)} for m in METHOD_LABELS},
        "pending_count": pending_n, "pending_total": pending_total,
        "by_account": [{"name": k, "net": v} for k, v in sorted(by_account.items(), key=lambda kv: -kv[1])],
        "by_cashier": [{"name": k, "gross": v} for k, v in sorted(by_cashier.items(), key=lambda kv: -kv[1])],
    }


def _sort(rows: list[dict], sort: str, desc: bool) -> list[dict]:
    keys = {
        "paid_at": lambda e: (e["paid_at"] or "", e["src_id"]), "amount": lambda e: e["signed"],
        "customer": lambda e: (e["customer_name"] or "").lower(), "no": lambda e: e["no"] or "",
        "method": lambda e: e["method_label"], "status": lambda e: e["status_label"],
    }
    return sorted(rows, key=keys.get(sort, keys["paid_at"]), reverse=desc)


def list_cash_receipts(filters: dict, sort: str = "paid_at", desc: bool = True, page: int = 1, page_size: int = 25) -> dict:
    with _conn() as con:
        entries = _all(con)
    rows = [e for e in entries if _matches(e, filters)]
    summary = _summary(rows)
    rows = _sort(rows, sort if sort in SORTS else "paid_at", desc)
    total = len(rows)
    if page_size and page_size > 0:
        page = max(page, 1)
        rows = rows[(page - 1) * page_size: page * page_size]
    return {
        "items": rows, "total": total, "page": page, "page_size": page_size, "summary": summary,
        "options": {"accounts": sorted({e["dest"] for e in entries}), "cashiers": sorted({e["cashier"] for e in entries if e["cashier"]})},
    }


def export_csv(filters: dict, sort: str = "paid_at", desc: bool = True) -> str:
    data = list_cash_receipts(filters, sort, desc, 1, 0)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["No. Receipt", "Tanggal", "Customer", "No. HP", "No. Invoice", "No. SO", "No. Plat", "Jenis", "Metode",
                "Rekening / Kas Tujuan", "Nominal", "Arah", "Status", "Petugas", "Catatan"])
    for e in data["items"]:
        w.writerow([e["no"], e["paid_local"], e["customer_name"], e["customer_phone"] or "", e["invoice_no"] or "", e["so_no"] or "",
                    e["plate"] or "", e["kind_label"], e["method_label"], e["dest"], e["amount"],
                    "Masuk" if e["kind"] == "masuk" else "Keluar", e["status_label"], e["cashier"] or "", e["note"] or ""])
    s = data["summary"]
    w.writerow([])
    w.writerow(["Penerimaan bruto", s["gross"]])
    w.writerow(["Refund / pembalikan", s["refund"]])
    w.writerow(["Penerimaan bersih", s["net"]])
    return buf.getvalue()


def _doc_summary(con: sqlite3.Connection, entries: list[dict]) -> dict:
    valid_in = sum(e["amount"] for e in entries if e["kind"] == "masuk" and e["status"] in ("berhasil", "dibatalkan"))
    out = sum(e["amount"] for e in entries if e["kind"] != "masuk" and e["state"] == "sah")
    return {"gross": valid_in, "refund": out, "net": valid_in - out,
            "pending": sum(e["amount"] for e in entries if e["status"] == "pending")}


def get_cash_receipt(key: str) -> dict | None:
    """One payment, with everything around it: its customer and invoice, the other payments of the same
    document, the refunds that point back at it and the invoice's payment history."""
    with _conn() as con:
        entries = _all(con)
        me = next((e for e in entries if e["key"] == key), None)
        if me is None:
            return None
        related_keys = {e["key"] for e in entries if (me["invoice_id"] and e["invoice_id"] == me["invoice_id"])
                        or (me["so_id"] and e["so_id"] == me["so_id"])}
        siblings = [e for e in entries if e["key"] in related_keys and e["key"] != key]
        siblings.sort(key=lambda e: (e["paid_at"] or "", e["src_id"]))
        invoice = None
        if me["invoice_id"]:
            r = con.execute("SELECT id, receipt_no, total, amount_paid, status, work_status, plate_full FROM receipts WHERE id=?",
                            (me["invoice_id"],)).fetchone()
            invoice = dict(r) if r else None
        events = []
        if me["invoice_id"]:
            events = [dict(r) for r in con.execute(
                "SELECT at, by_user, kind, note FROM receipt_events WHERE receipt_id=? AND kind IN ('payment','refund','created','void') ORDER BY at ASC, id ASC",
                (me["invoice_id"],)).fetchall()]
            for ev in events:
                loc = _local(ev["at"])
                ev["at_local"] = loc.strftime("%Y-%m-%d %H:%M") if loc else None
        return {"item": me, "siblings": siblings, "invoice": invoice, "events": events,
                "reversal_of": next((e for e in entries if e["key"] == me["reversed_of_key"]), None) if me["reversed_of_key"] else None,
                "reversals": [e for e in entries if e["reversed_of_key"] == key],
                "document": _doc_summary(con, [me] + siblings)}


def get_cash_receipt_group(doc_type: str, doc_id: str) -> dict | None:
    """All payments of one invoice or Sales Order (an order and its invoice are one story) for the
    "one invoice, several payments", "DP carried to the invoice" and "refund" views."""
    with _conn() as con:
        entries = _all(con)
        so_id = invoice_id = None
        if doc_type == "so":
            so_id = doc_id
            r = con.execute("SELECT receipt_id FROM preorders WHERE id=?", (doc_id,)).fetchone()
            if r is None:
                return None
            invoice_id = r["receipt_id"]
        else:
            invoice_id = doc_id
            r = con.execute("SELECT preorder_id FROM receipts WHERE id=?", (doc_id,)).fetchone()
            if r is None:
                return None
            so_id = r["preorder_id"]
        rows = [e for e in entries if (invoice_id and e["invoice_id"] == invoice_id) or (so_id and e["so_id"] == so_id)]
        rows.sort(key=lambda e: (e["paid_at"] or "", e["src_id"]))
        invoice = so = None
        if invoice_id:
            r = con.execute("SELECT id, receipt_no, total, amount_paid, status, work_status, customer_name, plate_full, created_at FROM receipts WHERE id=?",
                            (invoice_id,)).fetchone()
            invoice = dict(r) if r else None
            if invoice:
                invoice["items"] = [dict(i) for i in con.execute(
                    "SELECT product_name, quantity, unit_price FROM receipt_items WHERE receipt_id=? ORDER BY id LIMIT 6", (invoice_id,)).fetchall()]
        if so_id:
            r = con.execute("""SELECT o.id, o.po_number, o.customer_name, o.plate_full, o.is_draft,
                                      (SELECT COALESCE(SUM(quantity*unit_price),0) FROM preorder_items WHERE preorder_id=o.id AND status<>'dibatalkan') AS total,
                                      (SELECT COUNT(*) FROM preorder_items WHERE preorder_id=o.id) AS item_count
                               FROM preorders o WHERE o.id=?""", (so_id,)).fetchone()
            so = dict(r) if r else None
        refund_refs = [{"key": e["key"], "no": e["no"]} for e in rows if e["kind"] == "masuk" and e["status"] in ("berhasil", "dibatalkan")]
        return {"invoice": invoice, "so": so, "items": rows, "summary": _doc_summary(con, rows), "payment_refs": refund_refs}
