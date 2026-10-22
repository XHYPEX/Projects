"""Credit Note Supplier (Blueprint Credit Note Supplier, 22-10-2026).

A credit note is the money side of a supplier return the supplier agreed to settle as credit. Returning the
goods, the supplier's agreement, the credit note and its use on a faktur are separate steps:

    draft → review (Menunggu Verifikasi) → disetujui → aktif (Posted) → reversed
    draft / review / disetujui → ditolak ;  draft → dibatalkan

Only a posted note ('aktif') counts. Posting makes supplier credit available; nothing on any faktur changes yet.
Allocation moves credit onto one faktur (supplier_credit_allocations + a 'kredit' payment row, so the faktur's
original total never changes and its outstanding is total − paid). A refund is money coming IN from the supplier
against the credit, never a supplier payment. Corrections never edit a posted note: allocations are undone with
counter rows and the note is reversed (Owner approves), optionally followed by a new draft.

    AVAILABLE = posted amount − net allocations − active refunds
    SUPPLIER NET = Σ faktur outstanding − Σ credit available   (allocation leaves it unchanged)

Stock never moves here: quarantine and dispatch belong to the Retur Supplier / Barang Masuk modules.
Rows written before this module ('aktif' / 'dibatalkan' without lines) are read the same way.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from backend import database as db

CN_STATUS = {"draft": "Draft", "review": "Menunggu Verifikasi", "disetujui": "Disetujui", "ditolak": "Ditolak",
             "aktif": "Posted", "reversed": "Reversed", "dibatalkan": "Dibatalkan"}
LIVE = ("draft", "review", "disetujui", "aktif")
OPEN = ("draft", "review", "disetujui")
ALLOC_LABELS = {"none": "Belum Dialokasikan", "partial": "Sebagian Dialokasikan", "full": "Teralokasi Penuh"}
CHECKS = {"bukti": "Nomor / bukti Credit Note supplier sudah sesuai",
          "nominal": "Nominal sama dengan keputusan supplier (bukan otomatis harga PO)",
          "faktur": "Faktur sumber dan supplier sudah benar"}
REFUND_METHODS = {"transfer": "Transfer bank", "cash": "Tunai"}
FILE_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
FILE_MAX_BYTES = 8 * 1024 * 1024


class Conflict(ValueError):
    """The request is valid but the document moved on (already posted, balance changed): HTTP 409."""


def _err(code: str, text: str) -> str:
    return f"{text} ({code})"


def _rp(n) -> str:
    return "Rp" + db._sr_rupiah(n)


def migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS supplier_credit_note_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
            kind TEXT NOT NULL DEFAULT 'barang',
            return_item_id INTEGER REFERENCES supplier_return_items(id),
            inbound_item_id INTEGER,
            master_item_id INTEGER,
            sku TEXT, product_name TEXT NOT NULL, unit TEXT,
            qty INTEGER NOT NULL DEFAULT 0,
            unit_price INTEGER NOT NULL DEFAULT 0,
            tax_rate REAL NOT NULL DEFAULT 0,
            subtotal INTEGER NOT NULL DEFAULT 0,
            tax INTEGER NOT NULL DEFAULT 0,
            amount INTEGER NOT NULL DEFAULT 0,
            entitled INTEGER NOT NULL DEFAULT 0,
            -- what the shop gives up on this line (supplier credited less and the rest is not claimed again)
            waived INTEGER NOT NULL DEFAULT 0,
            waive_reason TEXT,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_scni_note ON supplier_credit_note_items(credit_note_id);
        CREATE INDEX IF NOT EXISTS idx_scni_ret_item ON supplier_credit_note_items(return_item_id);
        CREATE TABLE IF NOT EXISTS supplier_credit_note_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
            kind TEXT NOT NULL, note TEXT, amount INTEGER,
            before_json TEXT, after_json TEXT,
            by_user TEXT, at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scne_note ON supplier_credit_note_events(credit_note_id);
        CREATE TABLE IF NOT EXISTS supplier_credit_note_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
            refund_id INTEGER,
            filename TEXT NOT NULL, mime TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scnf_note ON supplier_credit_note_files(credit_note_id);
        -- Money the supplier paid back against a posted credit note: cash IN (Kas Masuk), not a supplier payment.
        CREATE TABLE IF NOT EXISTS supplier_credit_refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_number TEXT NOT NULL UNIQUE,
            credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            amount INTEGER NOT NULL CHECK (amount > 0),
            received_at TEXT NOT NULL,
            method TEXT NOT NULL, account TEXT, reference_no TEXT, note TEXT,
            status TEXT NOT NULL DEFAULT 'aktif',
            created_by TEXT, created_at TEXT NOT NULL,
            cancelled_by TEXT, cancelled_at TEXT, cancel_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scr_note ON supplier_credit_refunds(credit_note_id);
        CREATE TABLE IF NOT EXISTS supplier_credit_reversals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rev_number TEXT NOT NULL UNIQUE,
            credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
            reason TEXT NOT NULL, evidence TEXT,
            make_replacement INTEGER NOT NULL DEFAULT 0, correction_amount INTEGER,
            status TEXT NOT NULL DEFAULT 'menunggu',
            impact_json TEXT,
            requested_by TEXT, requested_at TEXT NOT NULL,
            decided_by TEXT, decided_at TEXT, decision_note TEXT,
            new_credit_note_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_scrv_note ON supplier_credit_reversals(credit_note_id);
    """)
    have = {r["name"] for r in con.execute("PRAGMA table_info(supplier_credit_notes)").fetchall()}
    for col, typ in (("purchase_invoice_id", "INTEGER"), ("subtotal", "INTEGER NOT NULL DEFAULT 0"), ("tax_amount", "INTEGER NOT NULL DEFAULT 0"),
                     ("submitted_by", "TEXT"), ("submitted_at", "TEXT"), ("reviewed_by", "TEXT"), ("reviewed_at", "TEXT"), ("checklist", "TEXT"),
                     ("review_note", "TEXT"), ("posted_by", "TEXT"), ("posted_at", "TEXT"), ("rejected_by", "TEXT"), ("rejected_at", "TEXT"),
                     ("reject_reason", "TEXT"), ("corrects_id", "INTEGER"), ("reversed_by", "TEXT"), ("reversed_at", "TEXT"),
                     ("reversal_id", "INTEGER"), ("source", "TEXT"), ("updated_at", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE supplier_credit_notes ADD COLUMN {col} {typ}")
    have = {r["name"] for r in con.execute("PRAGMA table_info(supplier_credit_allocations)").fetchall()}
    for col, typ in (("reverses_id", "INTEGER"), ("note", "TEXT"), ("cross_invoice", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:
            con.execute(f"ALTER TABLE supplier_credit_allocations ADD COLUMN {col} {typ}")
    if not con.execute("SELECT 1 FROM app_migrations WHERE name='cn_blueprint_backfill'").fetchone():
        # Notes recorded before this module were posted the moment they were written.
        con.execute("""UPDATE supplier_credit_notes SET posted_by=COALESCE(posted_by, created_by), posted_at=COALESCE(posted_at, created_at),
                       subtotal=CASE WHEN subtotal=0 THEN amount ELSE subtotal END, updated_at=COALESCE(updated_at, created_at),
                       source=COALESCE(source, CASE WHEN claim_id IS NOT NULL THEN 'pembatalan_po' ELSE 'retur' END)""")
        for r in con.execute("""SELECT c.id, (SELECT a.invoice_id FROM supplier_credit_allocations a WHERE a.credit_note_id=c.id ORDER BY a.id LIMIT 1) AS inv,
                                       r.purchase_invoice_id AS ret_inv FROM supplier_credit_notes c LEFT JOIN supplier_returns r ON r.id=c.return_id
                                WHERE c.purchase_invoice_id IS NULL""").fetchall():
            if r["inv"] or r["ret_inv"]:
                con.execute("UPDATE supplier_credit_notes SET purchase_invoice_id=? WHERE id=?", (r["inv"] or r["ret_inv"], r["id"]))
        con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('cn_blueprint_backfill', ?)", (db._now(),))


# ---------------------------------------------------------------- balances

def _allocated(con: sqlite3.Connection, cn_id: int) -> int:
    return db._sr_cn_allocated(con, cn_id)


def _refunded(con: sqlite3.Connection, cn_id: int) -> int:
    return con.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_credit_refunds WHERE credit_note_id=? AND status='aktif'",
                       (cn_id,)).fetchone()["n"]


def available(con: sqlite3.Connection, cn: dict) -> int:
    if cn["status"] != "aktif" or cn["kind"] != "kredit":
        return 0
    return max(cn["amount"] - _allocated(con, cn["id"]) - _refunded(con, cn["id"]), 0)


def total_available(con: sqlite3.Connection, supplier_id: int | None = None) -> int:
    rows = con.execute("SELECT * FROM supplier_credit_notes WHERE kind='kredit' AND status='aktif'" + (" AND supplier_id=?" if supplier_id else ""),
                       (supplier_id,) if supplier_id else ()).fetchall()
    return sum(available(con, dict(r)) for r in rows)


def supplier_position(con: sqlite3.Connection, supplier_id: int) -> dict:
    out = con.execute("""SELECT COALESCE(SUM(MAX(total - amount_paid, 0)), 0) AS n, COUNT(CASE WHEN total > amount_paid THEN 1 END) AS c
                         FROM purchase_invoices WHERE supplier_id=? AND status='posted'""", (supplier_id,)).fetchone()
    credit = total_available(con, supplier_id)
    return {"outstanding": out["n"], "open_invoices": out["c"], "credit_available": credit, "net": out["n"] - credit}


def _alloc_state(cn: dict, allocated: int, refunded: int) -> str:
    used = allocated + refunded
    if used <= 0:
        return "none"
    return "full" if used >= cn["amount"] else "partial"


# ---------------------------------------------------------------- helpers

def _get(con: sqlite3.Connection, cn_id: int) -> dict:
    row = con.execute("SELECT * FROM supplier_credit_notes WHERE id=?", (cn_id,)).fetchone()
    if row is None or row["kind"] != "kredit":
        raise LookupError("Credit Note tidak ditemukan.")
    return dict(row)


def _lines(con: sqlite3.Connection, cn_id: int) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM supplier_credit_note_items WHERE credit_note_id=? ORDER BY position, id", (cn_id,))]


def _event(con: sqlite3.Connection, cn_id: int, kind: str, note: str, by: str | None, amount: int | None = None,
           before: dict | None = None, after: dict | None = None) -> None:
    con.execute("""INSERT INTO supplier_credit_note_events (credit_note_id, kind, note, amount, before_json, after_json, by_user, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cn_id, kind, note, amount, json.dumps(before, ensure_ascii=False) if before else None,
                 json.dumps(after, ensure_ascii=False) if after else None, by, db._now()))
    con.execute("UPDATE supplier_credit_notes SET updated_at=? WHERE id=?", (db._now(), cn_id))


def _return_event(con: sqlite3.Connection, cn: dict, note: str, by: str | None) -> None:
    if cn.get("return_id"):
        db._sr_event(con, cn["return_id"], "keuangan", note, by)
        db._sr_refresh(con, cn["return_id"], by)


def _require_admin(user: dict, what: str) -> None:
    if (user or {}).get("role") != "admin":
        raise PermissionError(_err("ERR-S06", f"Akun Anda tidak memiliki izin untuk {what}."))


def _require_owner(user: dict, what: str) -> None:
    if (user or {}).get("role") != "admin" or not (user or {}).get("is_owner"):
        raise PermissionError(_err("ERR-S06", f"Hanya Owner yang dapat {what}."))


def _date(v, label: str) -> str:
    s = db._sr_text(v) or db.shop_today().isoformat()
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"{label} harus berformat YYYY-MM-DD.")
    return s[:10]


def _split(amount: int, rate: float) -> tuple[int, int]:
    """A line's amount (incl. PPN) as subtotal + tax that add up exactly."""
    sub = int(round(amount / (1 + float(rate or 0) / 100))) if rate else amount
    return sub, amount - sub


def _entitlements(con: sqlite3.Connection, ret: dict, exclude_cn: int | None = None) -> dict:
    """What the supplier agreed to credit on one return and what is still free: per kredit line, the shipping claim,
    and the pool (the whole claim less every other live note, refund and older note without lines)."""
    rate = db._sr_rate(con, ret)
    items = db._sr_items(con, ret["id"])
    used: dict[tuple, dict] = {}
    for r in con.execute(
            """SELECT ci.kind, ci.return_item_id, SUM(ci.qty) AS q, SUM(ci.amount + ci.waived) AS v FROM supplier_credit_note_items ci
               JOIN supplier_credit_notes c ON c.id = ci.credit_note_id
               WHERE c.return_id=? AND c.status IN ('draft','review','disetujui','aktif') AND c.id<>? GROUP BY ci.kind, ci.return_item_id""",
            (ret["id"], exclude_cn or 0)):
        used[(r["kind"], r["return_item_id"])] = {"qty": r["q"] or 0, "value": r["v"] or 0}
    lineless = con.execute(
        """SELECT COALESCE(SUM(c.amount), 0) AS n FROM supplier_credit_notes c WHERE c.return_id=? AND c.status='aktif' AND c.id<>?
           AND NOT EXISTS (SELECT 1 FROM supplier_credit_note_items ci WHERE ci.credit_note_id = c.id)""", (ret["id"], exclude_cn or 0)).fetchone()["n"]
    lines = []
    for i in items:
        if i["settlement"] != "kredit" or i["qty_approved"] <= 0:
            continue
        ent = db._sr_value(i["qty_approved"], i["unit_cost"], rate)
        u = used.get(("barang", i["id"]), {"qty": 0, "value": 0})
        lines.append({"kind": "barang", "return_item_id": i["id"], "inbound_item_id": i["inbound_item_id"], "master_item_id": i["master_item_id"],
                      "sku": i.get("sku"), "unit": i.get("unit"), "product_name": i["product_name"], "qty_approved": i["qty_approved"],
                      "qty_left": max(i["qty_approved"] - u["qty"], 0), "unit_price": i["unit_cost"], "tax_rate": rate,
                      "entitled": ent, "remaining": max(ent - u["value"], 0)})
    ship = ret.get("shipping_claim_approved") or 0
    if ship:
        u = used.get(("ongkir", None), {"qty": 0, "value": 0})
        lines.append({"kind": "ongkir", "return_item_id": None, "inbound_item_id": None, "master_item_id": None, "sku": None, "unit": None,
                      "product_name": "Klaim ongkos kirim retur", "qty_approved": 1, "qty_left": 1, "unit_price": ship, "tax_rate": 0,
                      "entitled": ship, "remaining": max(ship - u["value"], 0)})
    claim = db._sr_money(con, ret, items)["claim_value"]
    other = sum(u["value"] for u in used.values())
    refunds = con.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_credit_notes WHERE return_id=? AND kind='refund' AND status='aktif'",
                          (ret["id"],)).fetchone()["n"]
    return {"lines": lines, "pool": max(claim - other - lineless - refunds, 0), "claim": claim, "rate": rate}


def _number(con: sqlite3.Connection) -> str:
    return db._sr_number(con, "CN-SUP", "supplier_credit_notes", "cn_number")


def create_draft(con: sqlite3.Connection, *, supplier_id: int, return_id: int | None, invoice_id: int | None, lines: list[dict],
                 by: str | None, note: str | None, source: str, corrects_id: int | None = None) -> dict:
    """Inserts a Draft note with its lines (already validated by the caller). No AP or stock effect."""
    now = db._now()
    sub = sum(ln["subtotal"] for ln in lines)
    tax = sum(ln["tax"] for ln in lines)
    cn_id = con.execute(
        """INSERT INTO supplier_credit_notes (cn_number, kind, supplier_id, return_id, amount, issued_at, note, status, created_by, created_at,
               purchase_invoice_id, subtotal, tax_amount, source, corrects_id, updated_at)
           VALUES (?, 'kredit', ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_number(con), supplier_id, return_id, sub + tax, db.shop_today().isoformat(), note, by, now, invoice_id, sub, tax, source,
         corrects_id, now)).lastrowid
    for pos, ln in enumerate(lines):
        con.execute(
            """INSERT INTO supplier_credit_note_items (credit_note_id, kind, return_item_id, inbound_item_id, master_item_id, sku, product_name, unit,
                   qty, unit_price, tax_rate, subtotal, tax, amount, entitled, waived, waive_reason, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cn_id, ln["kind"], ln.get("return_item_id"), ln.get("inbound_item_id"), ln.get("master_item_id"), ln.get("sku"), ln["product_name"],
             ln.get("unit"), ln["qty"], ln["unit_price"], ln["tax_rate"], ln["subtotal"], ln["tax"], ln["amount"], ln["entitled"],
             ln.get("waived", 0), ln.get("waive_reason"), pos))
    cn = _get(con, cn_id)
    _event(con, cn_id, "draft", f"Draft Credit Note {cn['cn_number']} dibuat {_rp(cn['amount'])}" + (f" — {note}" if note else ""), by, cn["amount"])
    return cn


def _line_from_ent(e: dict, amount: int | None = None) -> dict:
    amt = e["remaining"] if amount is None else amount
    sub, tax = _split(amt, e["tax_rate"])
    return {**e, "qty": e["qty_left"] or e["qty_approved"], "amount": amt, "subtotal": sub, "tax": tax, "entitled": e["remaining"], "waived": 0}


# ---------------------------------------------------------------- create / edit

def create_from_return(return_id: int, user: dict, note: str | None = None) -> dict:
    """Admin: a Draft note for whatever the supplier agreed to credit on this return and is not on another note yet.
    An open draft of the same return is returned instead of a second one."""
    _require_admin(user, "membuat Credit Note")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = db._sr_get(con, return_id)
        open_ = con.execute("SELECT id FROM supplier_credit_notes WHERE return_id=? AND kind='kredit' AND status IN ('draft','review','disetujui') ORDER BY id LIMIT 1",
                            (return_id,)).fetchone()
        if open_:
            return {"id": open_["id"], "existing": True}
        if ret["doc_status"] not in ("diproses", "selesai"):
            raise ValueError(_err("ERR-C01", "Retur belum diproses. Credit Note dibuat setelah retur disetujui dan keputusan supplier dicatat."))
        ent = _entitlements(con, ret)
        if not ent["lines"]:
            raise ValueError(_err("ERR-C01", "Retur baru disetujui secara internal. Persetujuan nilai kredit dari supplier belum tercatat."))
        lines = [_line_from_ent(e) for e in ent["lines"] if e["remaining"] > 0]
        total = sum(ln["amount"] for ln in lines)
        if not lines or ent["pool"] <= 0:
            raise ValueError(_err("ERR-C02", "Seluruh nilai kredit yang disetujui supplier untuk retur ini sudah dibuatkan Credit Note."))
        if total > ent["pool"]:  # older notes without lines already took part of the claim
            over = total - ent["pool"]
            for ln in reversed(lines):
                cut = min(over, ln["amount"])
                ln.update(amount=ln["amount"] - cut, entitled=ln["entitled"])
                ln["subtotal"], ln["tax"] = _split(ln["amount"], ln["tax_rate"])
                over -= cut
            lines = [ln for ln in lines if ln["amount"] > 0]
        inv = ret["purchase_invoice_id"]
        if inv and not con.execute("SELECT 1 FROM purchase_invoices WHERE id=? AND status<>'void'", (inv,)).fetchone():
            inv = None
        cn = create_draft(con, supplier_id=ret["supplier_id"], return_id=return_id, invoice_id=inv, lines=lines, by=by,
                          note=db._sr_text(note) or f"Dari retur {ret['return_number']}", source="retur")
        db._sr_event(con, return_id, "keuangan", f"Draft Credit Note {cn['cn_number']} {_rp(cn['amount'])} dibuat; menunggu verifikasi dan posting", by)
    return {"id": cn["id"], "existing": False}


def update_draft(cn_id: int, data: dict, user: dict) -> dict:
    _require_admin(user, "mengubah Credit Note")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] == "aktif":
            raise Conflict(_err("ERR-C08", "Credit Note yang telah diposting tidak dapat diubah langsung. Ajukan koreksi atau reversal."))
        if cn["status"] != "draft":
            raise Conflict("Hanya Draft yang bisa diubah. Kembalikan ke Draft dulu.")
        before = {"amount": cn["amount"], "supplier_cn_no": cn["supplier_cn_no"], "issued_at": cn["issued_at"], "purchase_invoice_id": cn["purchase_invoice_id"]}
        supplier_cn_no = db._sr_text(data.get("supplier_cn_no"), 80) if "supplier_cn_no" in data else cn["supplier_cn_no"]
        if supplier_cn_no:
            dup = con.execute("""SELECT cn_number FROM supplier_credit_notes WHERE supplier_id=? AND id<>? AND kind='kredit'
                                 AND UPPER(TRIM(supplier_cn_no))=UPPER(?) AND status IN ('draft','review','disetujui','aktif')""",
                              (cn["supplier_id"], cn_id, supplier_cn_no)).fetchone()
            if dup:
                raise ValueError(f"Nomor Credit Note supplier {supplier_cn_no} sudah dipakai di {dup['cn_number']}.")
        issued = _date(data.get("issued_at"), "Tanggal efektif") if "issued_at" in data else cn["issued_at"]
        inv_id = cn["purchase_invoice_id"]
        if "purchase_invoice_id" in data:
            inv_id = int(data["purchase_invoice_id"]) if data.get("purchase_invoice_id") else None
            if inv_id:
                inv = con.execute("SELECT supplier_id, status FROM purchase_invoices WHERE id=?", (inv_id,)).fetchone()
                if inv is None or inv["status"] == "void":
                    raise ValueError("Faktur sumber tidak ditemukan.")
                if inv["supplier_id"] != cn["supplier_id"]:
                    raise ValueError(_err("ERR-C04", "Credit Note tidak dapat digunakan untuk faktur supplier lain."))
        note = db._sr_text(data.get("note"), 1000) if "note" in data else cn["note"]
        lines = _lines(con, cn_id)
        if data.get("lines") is not None:
            ret = db._sr_get(con, cn["return_id"]) if cn["return_id"] else None
            ent = _entitlements(con, ret, cn_id) if ret else {"lines": [], "pool": None}
            free = {(e["kind"], e["return_item_id"]): e for e in ent["lines"]}
            by_id = {ln["id"]: ln for ln in lines}
            for d in data["lines"]:
                ln = by_id.get(int(d.get("id") or 0))
                if ln is None:
                    raise ValueError("Baris Credit Note tidak ditemukan.")
                amt = int(d.get("amount") or 0)
                if amt < 0:
                    raise ValueError("Nominal tidak boleh minus.")
                e = free.get((ln["kind"], ln["return_item_id"]))
                room = e["remaining"] if e else ln["entitled"]
                if amt > room:
                    raise ValueError(_err("ERR-C02", f"“{ln['product_name']}”: nominal kredit melebihi nilai yang disetujui supplier atau masih tersedia "
                                                     f"untuk dikreditkan (maksimum {_rp(room)})."))
                waive = bool(d.get("waive")) and amt < room
                reason = db._sr_text(d.get("waive_reason"))
                if waive and not reason:
                    raise ValueError(f"“{ln['product_name']}”: tulis alasan sisa {_rp(room - amt)} tidak diklaim lagi.")
                qty = int(d.get("qty") if d.get("qty") is not None else ln["qty"])
                if qty < 0:
                    raise ValueError("Qty tidak boleh minus.")
                sub, tax = _split(amt, ln["tax_rate"])
                ln.update(amount=amt, subtotal=sub, tax=tax, qty=qty, entitled=room, waived=room - amt if waive else 0, waive_reason=reason if waive else None)
            total = sum(ln["amount"] for ln in lines)
            if ent.get("pool") is not None and total + sum(ln["waived"] for ln in lines) > ent["pool"]:
                raise ValueError(_err("ERR-C02", f"Total kredit melebihi sisa klaim retur yang disetujui supplier ({_rp(ent['pool'])})."))
            for ln in lines:
                con.execute("""UPDATE supplier_credit_note_items SET amount=?, subtotal=?, tax=?, qty=?, entitled=?, waived=?, waive_reason=? WHERE id=?""",
                            (ln["amount"], ln["subtotal"], ln["tax"], ln["qty"], ln["entitled"], ln["waived"], ln["waive_reason"], ln["id"]))
        sub, tax = sum(ln["subtotal"] for ln in lines), sum(ln["tax"] for ln in lines)
        con.execute("""UPDATE supplier_credit_notes SET supplier_cn_no=?, issued_at=?, purchase_invoice_id=?, note=?, subtotal=?, tax_amount=?, amount=?
                       WHERE id=?""", (supplier_cn_no, issued, inv_id, note, sub, tax, sub + tax, cn_id))
        after = {"amount": sub + tax, "supplier_cn_no": supplier_cn_no, "issued_at": issued, "purchase_invoice_id": inv_id}
        if after != before or data.get("lines") is not None:
            _event(con, cn_id, "ubah", "Draft diperbarui" + (f": nominal {_rp(before['amount'])} → {_rp(sub + tax)}" if before["amount"] != sub + tax else ""),
                   by, sub + tax, before, after)
    return detail(cn_id, user)


def _check_ready(con: sqlite3.Connection, cn: dict) -> list[dict]:
    """Hard errors that block submit / approve / post, re-read under the same lock."""
    lines = _lines(con, cn["id"])
    errs = []
    if not (cn["supplier_cn_no"] or "").strip():
        errs.append({"code": "ERR-C05", "message": "Nomor/bukti Credit Note dari supplier belum lengkap. Lengkapi sebelum Posting."})
    if cn["amount"] <= 0 or not lines:
        errs.append({"code": "ERR-C02", "message": "Nominal Credit Note harus lebih dari 0."})
    if sum(ln["amount"] for ln in lines) != cn["amount"] or sum(ln["subtotal"] + ln["tax"] for ln in lines) != cn["amount"] \
            or cn["subtotal"] + cn["tax_amount"] != cn["amount"]:
        errs.append({"code": "ERR-C10", "message": "Jumlah baris, pajak, dan total Credit Note tidak cocok. Periksa perhitungan dan pembulatan."})
    if cn["return_id"]:
        ret = db._sr_get(con, cn["return_id"])
        ent = _entitlements(con, ret, cn["id"])
        free = {(e["kind"], e["return_item_id"]): e["remaining"] for e in ent["lines"]}
        for ln in lines:
            if ln["amount"] + ln["waived"] > free.get((ln["kind"], ln["return_item_id"]), 0):
                errs.append({"code": "ERR-C02", "message": f"“{ln['product_name']}”: nominal kredit melebihi nilai yang disetujui supplier atau masih tersedia untuk dikreditkan."})
                break
        if cn["amount"] + sum(ln["waived"] for ln in lines) > ent["pool"]:
            errs.append({"code": "ERR-C02", "message": "Nominal kredit melebihi nilai yang disetujui supplier atau masih tersedia untuk dikreditkan."})
    if cn["purchase_invoice_id"]:
        inv = con.execute("SELECT supplier_id, status FROM purchase_invoices WHERE id=?", (cn["purchase_invoice_id"],)).fetchone()
        if inv and inv["supplier_id"] != cn["supplier_id"]:
            errs.append({"code": "ERR-C04", "message": "Credit Note tidak dapat digunakan untuk faktur supplier lain."})
    return errs


def _raise_first(errs: list[dict]) -> None:
    if errs:
        raise ValueError(_err(errs[0]["code"], errs[0]["message"]))


def submit(cn_id: int, note: str | None, user: dict) -> dict:
    _require_admin(user, "mengajukan verifikasi Credit Note")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] != "draft":
            raise Conflict("Hanya Draft yang bisa diajukan verifikasi.")
        _raise_first(_check_ready(con, cn))
        con.execute("UPDATE supplier_credit_notes SET status='review', submitted_by=?, submitted_at=? WHERE id=?", (by, db._now(), cn_id))
        _event(con, cn_id, "ajukan", "Diajukan untuk verifikasi" + (f": {note.strip()}" if (note or "").strip() else ""), by, cn["amount"],
               {"status": "draft"}, {"status": "review"})
    return detail(cn_id, user)


def approve(cn_id: int, data: dict, user: dict) -> dict:
    _require_admin(user, "menyetujui Credit Note")
    by = user.get("username")
    checks = [c for c in (data.get("checks") or []) if c in CHECKS]
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] != "review":
            raise Conflict("Credit Note ini tidak sedang menunggu verifikasi.")
        missing = [CHECKS[k] for k in CHECKS if k not in checks]
        if missing:
            raise ValueError("Centang semua pemeriksaan: " + "; ".join(missing) + ".")
        _raise_first(_check_ready(con, cn))
        note = db._sr_text(data.get("note"), 500)
        con.execute("""UPDATE supplier_credit_notes SET status='disetujui', reviewed_by=?, reviewed_at=?, checklist=?, review_note=? WHERE id=?""",
                    (by, db._now(), json.dumps(checks), note, cn_id))
        _event(con, cn_id, "setujui", "Diverifikasi dan disetujui" + (f": {note}" if note else ""), by, cn["amount"],
               {"status": "review"}, {"status": "disetujui"})
    return detail(cn_id, user)


def send_back(cn_id: int, note: str, user: dict) -> dict:
    _require_admin(user, "mengembalikan Credit Note ke Draft")
    by = user.get("username")
    if not (note or "").strip():
        raise ValueError("Tulis apa yang perlu diperbaiki.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] not in ("review", "disetujui"):
            raise Conflict("Hanya Credit Note yang sedang diverifikasi / disetujui yang bisa dikembalikan ke Draft.")
        con.execute("UPDATE supplier_credit_notes SET status='draft', reviewed_by=NULL, reviewed_at=NULL, checklist=NULL WHERE id=?", (cn_id,))
        _event(con, cn_id, "kembali_draft", f"Dikembalikan ke Draft: {note.strip()}", by, None, {"status": cn["status"]}, {"status": "draft"})
    return detail(cn_id, user)


def reject(cn_id: int, reason: str, user: dict) -> dict:
    _require_admin(user, "menolak Credit Note")
    by = user.get("username")
    if not (reason or "").strip():
        raise ValueError("Alasan penolakan wajib diisi.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] not in OPEN:
            raise Conflict("Credit Note ini sudah diposting atau ditutup.")
        new = "dibatalkan" if cn["status"] == "draft" else "ditolak"
        con.execute("UPDATE supplier_credit_notes SET status=?, rejected_by=?, rejected_at=?, reject_reason=? WHERE id=?",
                    (new, by, db._now(), reason.strip()[:500], cn_id))
        _event(con, cn_id, "tolak" if new == "ditolak" else "batal", f"{'Ditolak' if new == 'ditolak' else 'Draft dibatalkan'}: {reason.strip()}", by,
               None, {"status": cn["status"]}, {"status": new})
        _return_event(con, cn, f"Credit Note {cn['cn_number']} {'ditolak' if new == 'ditolak' else 'dibatalkan'}: {reason.strip()}", by)
    return detail(cn_id, user)


def post(cn_id: int, user: dict) -> dict:
    """Approved → Posted, exactly once. Makes supplier credit available; no faktur and no stock changes."""
    _require_admin(user, "memposting Credit Note")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] == "aktif":
            raise Conflict(_err("ERR-C06", "Credit Note ini sudah diposting. Posting kedua tidak dilakukan."))
        if cn["status"] != "disetujui":
            raise Conflict("Credit Note harus diverifikasi dan disetujui sebelum diposting.")
        _raise_first(_check_ready(con, cn))
        now = db._now()
        cur = con.execute("UPDATE supplier_credit_notes SET status='aktif', posted_by=?, posted_at=? WHERE id=? AND status='disetujui'", (by, now, cn_id))
        if cur.rowcount != 1:
            raise Conflict(_err("ERR-C06", "Credit Note ini sudah diposting. Posting kedua tidak dilakukan."))
        pos = supplier_position(con, cn["supplier_id"])
        _event(con, cn_id, "posting", f"Diposting {_rp(cn['amount'])}; kredit supplier tersedia naik {_rp(cn['amount'])} "
                                      f"(saldo kredit {_rp(pos['credit_available'])}). Faktur belum berubah sampai dialokasikan.",
               by, cn["amount"], {"status": "disetujui"}, {"status": "aktif"})
        if cn["purchase_invoice_id"]:
            con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, ?, 'kredit', ?)",
                        (cn["purchase_invoice_id"], now, by, f"Credit Note {cn['cn_number']} {_rp(cn['amount'])} diposting; menunggu alokasi"))
        _return_event(con, cn, f"Credit Note {cn['cn_number']} {_rp(cn['amount'])} diposting (No. CN supplier {cn['supplier_cn_no']})", by)
    return detail(cn_id, user)


# ---------------------------------------------------------------- allocation / refund

def _invoice(con: sqlite3.Connection, invoice_id: int) -> dict:
    inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError("Faktur pembelian tidak ditemukan.")
    return dict(inv)


def _pending_reversal(con: sqlite3.Connection, cn_id: int):
    return con.execute("SELECT rev_number FROM supplier_credit_reversals WHERE credit_note_id=? AND status='menunggu'", (cn_id,)).fetchone()


def allocate(cn_id: int, data: dict, user: dict) -> dict:
    """Admin: apply posted credit to one or more posted faktur of the same supplier, all or nothing. Each row is capped
    by the credit still available and the faktur's outstanding (re-read under the lock)."""
    _require_admin(user, "mengalokasikan Credit Note")
    by = user.get("username")
    rows = [r for r in (data.get("allocations") or []) if int(r.get("amount") or 0) != 0]
    note = db._sr_text(data.get("note"), 300)
    if not rows:
        raise ValueError("Isi nominal alokasi.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] != "aktif":
            raise Conflict("Credit Note harus berstatus Posted untuk dialokasikan.")
        if _pending_reversal(con, cn_id):
            raise Conflict("Credit Note sedang diajukan reversal; alokasi ditahan sampai Owner memutuskan.")
        left = available(con, cn)
        msgs = []
        seen = set()
        for r in rows:
            inv_id, amount = int(r.get("invoice_id") or 0), int(r.get("amount") or 0)
            if inv_id in seen:
                raise ValueError("Satu faktur hanya boleh muncul sekali.")
            seen.add(inv_id)
            inv = _invoice(con, inv_id)
            if inv["supplier_id"] != cn["supplier_id"]:
                raise ValueError(_err("ERR-C04", f"Credit Note {cn['cn_number']} tidak dapat digunakan untuk faktur supplier lain."))
            if inv["status"] != "posted":
                raise ValueError(f"Faktur {inv['invoice_number']} belum diposting atau sudah dibatalkan.")
            due = max(inv["total"] - inv["amount_paid"], 0)
            if amount <= 0:
                raise ValueError("Nominal alokasi harus lebih dari 0.")
            if due <= 0:
                raise ValueError(_err("ERR-A01", f"Faktur {inv['invoice_number']} sudah lunas. Kredit tetap tersedia untuk faktur lain atau refund."))
            if amount > min(left, due):
                raise Conflict(_err("ERR-A01", f"Nilai alokasi melebihi kredit tersedia atau sisa hutang invoice. Maksimum saat ini {_rp(min(left, due))}."))
            cross = bool(cn["purchase_invoice_id"]) and inv_id != cn["purchase_invoice_id"]
            if cross and not note:
                raise ValueError(_err("ERR-A05", "Alokasi ke invoice lain memerlukan persetujuan Finance dan supplier yang sama. Tulis alasannya."))
            now = db._now()
            con.execute("""INSERT INTO supplier_credit_allocations (credit_note_id, invoice_id, amount, created_by, created_at, note, cross_invoice)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", (cn_id, inv_id, amount, by, now, note, int(cross)))
            con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid + ?, updated_at=? WHERE id=?", (amount, now, inv_id))
            db._pi_payment_row(con, inv_id, amount, "kredit", by, reference_no=cn["cn_number"], note=f"Credit Note {cn['cn_number']}")
            con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, ?, 'kredit', ?)",
                        (inv_id, now, by, f"Credit Note {cn['cn_number']} dialokasikan {_rp(amount)}; sisa tagihan {_rp(due - amount)}"))
            left -= amount
            msgs.append(f"{_rp(amount)} ke {inv['invoice_number']} (sisa tagihan {_rp(due)} → {_rp(due - amount)})")
        _event(con, cn_id, "alokasi", "Dialokasikan " + "; ".join(msgs) + f". Kredit tersisa {_rp(left)}" + (f". Catatan: {note}" if note else ""),
               by, sum(int(r["amount"]) for r in rows))
        _return_event(con, cn, f"Credit Note {cn['cn_number']}: dialokasikan " + "; ".join(msgs), by)
    return detail(cn_id, user)


def unallocate(cn_id: int, allocation_id: int, reason: str, user: dict) -> dict:
    """Admin: undo one allocation with a counter row (the faktur owes that amount again)."""
    _require_admin(user, "membatalkan alokasi Credit Note")
    by = user.get("username")
    if not (reason or "").strip():
        raise ValueError("Alasan pembatalan alokasi wajib diisi.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        a = con.execute("SELECT * FROM supplier_credit_allocations WHERE id=? AND credit_note_id=?", (allocation_id, cn_id)).fetchone()
        if a is None or a["amount"] <= 0:
            raise ValueError("Alokasi tidak ditemukan.")
        if con.execute("SELECT 1 FROM supplier_credit_allocations WHERE reverses_id=?", (allocation_id,)).fetchone():
            raise Conflict("Alokasi ini sudah dibatalkan.")
        if cn["status"] != "aktif":
            raise Conflict("Credit Note ini tidak aktif.")
        inv = _invoice(con, a["invoice_id"])
        if inv["status"] != "posted":
            raise ValueError(f"Faktur {inv['invoice_number']} sudah tidak aktif; alokasinya tidak bisa dibalik.")
        now = db._now()
        con.execute("""INSERT INTO supplier_credit_allocations (credit_note_id, invoice_id, amount, created_by, created_at, note, reverses_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""", (cn_id, a["invoice_id"], -a["amount"], by, now, reason.strip()[:300], allocation_id))
        con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid - ?, updated_at=? WHERE id=?", (a["amount"], now, a["invoice_id"]))
        db._pi_payment_row(con, a["invoice_id"], -a["amount"], "kredit", by, reference_no=cn["cn_number"], note=f"Alokasi {cn['cn_number']} dibatalkan")
        con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, ?, 'kredit', ?)",
                    (a["invoice_id"], now, by, f"Alokasi Credit Note {cn['cn_number']} {_rp(a['amount'])} dibatalkan: {reason.strip()}"))
        _event(con, cn_id, "batal_alokasi", f"Alokasi {_rp(a['amount'])} ke {inv['invoice_number']} dibatalkan: {reason.strip()}", by, -a["amount"])
        _return_event(con, cn, f"Credit Note {cn['cn_number']}: alokasi {_rp(a['amount'])} ke {inv['invoice_number']} dibatalkan", by)
    return detail(cn_id, user)


def record_refund(cn_id: int, data: dict, user: dict) -> dict:
    """Admin: the supplier really paid the credit back (Kas Masuk). Reduces available credit; faktur untouched."""
    _require_admin(user, "mencatat refund supplier")
    by = user.get("username")
    amount = int(data.get("amount") or 0)
    method = data.get("method")
    if method not in REFUND_METHODS:
        raise ValueError(_err("ERR-F01", "Pilih metode penerimaan refund (transfer / tunai)."))
    account = db._sr_text(data.get("account"), 120)
    ref = db._sr_text(data.get("reference_no"), 120)
    if method == "transfer" and not (account and ref):
        raise ValueError(_err("ERR-F01", "Bukti penerimaan refund belum lengkap: isi rekening penerima dan nomor referensi transfer."))
    if method == "cash" and not ref:
        raise ValueError(_err("ERR-F01", "Bukti penerimaan refund belum lengkap: isi nomor kwitansi / nama penerima uang."))
    received = _date(data.get("received_at"), "Tanggal terima")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] != "aktif":
            raise Conflict("Refund hanya untuk Credit Note yang sudah diposting.")
        if _pending_reversal(con, cn_id):
            raise Conflict("Credit Note sedang diajukan reversal; refund ditahan sampai Owner memutuskan.")
        left = available(con, cn)
        if amount <= 0 or amount > left:
            raise Conflict(_err("ERR-F01", f"Bukti penerimaan refund belum lengkap atau saldo kredit tidak mencukupi. Kredit tersedia {_rp(left)}."))
        number = db._sr_number(con, "KM-SUP", "supplier_credit_refunds", "refund_number")
        rid = con.execute("""INSERT INTO supplier_credit_refunds (refund_number, credit_note_id, supplier_id, amount, received_at, method, account,
                                 reference_no, note, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (number, cn_id, cn["supplier_id"], amount, received, method, account, ref, db._sr_text(data.get("note"), 300), by,
                           db._now())).lastrowid
        _event(con, cn_id, "refund", f"Refund diterima {_rp(amount)} ({REFUND_METHODS[method]}{', ' + account if account else ''}, ref {ref}) — "
                                     f"Kas Masuk {number}; kredit tersisa {_rp(left - amount)}", by, amount)
        _return_event(con, cn, f"Credit Note {cn['cn_number']}: refund {_rp(amount)} diterima ({number})", by)
    out = detail(cn_id, user)
    out["refund_id"] = rid
    return out


def cancel_refund(cn_id: int, refund_id: int, reason: str, user: dict) -> dict:
    _require_admin(user, "membatalkan refund")
    by = user.get("username")
    if not (reason or "").strip():
        raise ValueError("Alasan pembatalan wajib diisi.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        r = con.execute("SELECT * FROM supplier_credit_refunds WHERE id=? AND credit_note_id=?", (refund_id, cn_id)).fetchone()
        if r is None or r["status"] != "aktif":
            raise Conflict("Refund tidak ditemukan atau sudah dibatalkan.")
        con.execute("UPDATE supplier_credit_refunds SET status='dibatalkan', cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
                    (by, db._now(), reason.strip()[:300], refund_id))
        _event(con, cn_id, "batal_refund", f"Refund {r['refund_number']} {_rp(r['amount'])} dibatalkan: {reason.strip()}", by, -r["amount"])
        _return_event(con, cn, f"Credit Note {cn['cn_number']}: refund {r['refund_number']} dibatalkan", by)
    return detail(cn_id, user)


# ---------------------------------------------------------------- reversal

def _impact(con: sqlite3.Connection, cn: dict) -> dict:
    per: dict[int, int] = {}
    for a in con.execute("SELECT invoice_id, amount FROM supplier_credit_allocations WHERE credit_note_id=?", (cn["id"],)):
        per[a["invoice_id"]] = per.get(a["invoice_id"], 0) + a["amount"]
    invoices = []
    for inv_id, amt in per.items():
        if amt <= 0:
            continue
        inv = _invoice(con, inv_id)
        due = max(inv["total"] - inv["amount_paid"], 0)
        invoices.append({"invoice_id": inv_id, "invoice_number": inv["invoice_number"], "amount": amt, "due_before": due, "due_after": due + amt,
                         "status": inv["status"]})
    return {"invoices": invoices, "allocated": sum(i["amount"] for i in invoices), "refunded": _refunded(con, cn["id"]),
            "available": available(con, cn)}


def request_reversal(cn_id: int, data: dict, user: dict) -> dict:
    _require_admin(user, "mengajukan reversal Credit Note")
    by = user.get("username")
    reason = db._sr_text(data.get("reason"), 500)
    if not reason:
        raise ValueError("Alasan koreksi / reversal wajib diisi.")
    replace = bool(data.get("make_replacement"))
    corr = int(data.get("correction_amount") or 0) if replace else None
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] != "aktif":
            raise Conflict("Reversal hanya untuk Credit Note yang sudah diposting." if cn["status"] != "reversed" else "Credit Note ini sudah di-reversal.")
        if _pending_reversal(con, cn_id):
            raise Conflict("Sudah ada pengajuan reversal yang menunggu Owner.")
        if corr is not None and (corr < 0 or corr > cn["amount"]):
            raise ValueError(f"Nilai koreksi harus antara Rp0 dan {_rp(cn['amount'])}.")
        impact = _impact(con, cn)
        number = db._sr_number(con, "REV-CN", "supplier_credit_reversals", "rev_number")
        rid = con.execute("""INSERT INTO supplier_credit_reversals (rev_number, credit_note_id, reason, evidence, make_replacement, correction_amount,
                                 impact_json, requested_by, requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (number, cn_id, reason, db._sr_text(data.get("evidence"), 500), int(replace), corr, json.dumps(impact), by, db._now())).lastrowid
        _event(con, cn_id, "ajukan_reversal", f"Reversal {number} diajukan: {reason}" + (f"; CN pengganti diusulkan {_rp(corr)}" if replace else "")
               + " — menunggu persetujuan Owner", by, -cn["amount"])
    return detail(cn_id, user)


def decide_reversal(reversal_id: int, approve_: bool, note: str | None, user: dict) -> dict:
    """Owner: approve runs the whole reversal atomically (counter rows for every allocation, note → Reversed,
    optional replacement Draft); reject leaves the note as it was."""
    _require_owner(user, "menyetujui reversal Credit Note")
    by = user.get("username")
    note = db._sr_text(note, 500)
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        rv = con.execute("SELECT * FROM supplier_credit_reversals WHERE id=?", (reversal_id,)).fetchone()
        if rv is None:
            raise LookupError("Pengajuan reversal tidak ditemukan.")
        rv = dict(rv)
        if rv["status"] != "menunggu":
            raise Conflict("Pengajuan reversal ini sudah diputuskan.")
        cn = _get(con, rv["credit_note_id"])
        now = db._now()
        if not approve_:
            if not note:
                raise ValueError("Tulis alasan penolakan.")
            con.execute("UPDATE supplier_credit_reversals SET status='ditolak', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                        (by, now, note, reversal_id))
            _event(con, cn["id"], "tolak_reversal", f"Reversal {rv['rev_number']} ditolak Owner: {note}", by)
            return detail(cn["id"], user)
        if cn["status"] != "aktif":
            raise Conflict("Credit Note ini sudah tidak aktif.")
        if _refunded(con, cn["id"]):
            raise Conflict(_err("ERR-C09", "Credit Note memiliki alokasi atau refund aktif. Selesaikan koreksi terkait sebelum reversal (batalkan refund dulu)."))
        impact = _impact(con, cn)
        for i in impact["invoices"]:
            if i["status"] != "posted":
                raise Conflict(_err("ERR-C09", f"Faktur {i['invoice_number']} sudah tidak aktif; alokasinya tidak bisa dibalik otomatis."))
        for i in impact["invoices"]:
            con.execute("""INSERT INTO supplier_credit_allocations (credit_note_id, invoice_id, amount, created_by, created_at, note)
                           VALUES (?, ?, ?, ?, ?, ?)""", (cn["id"], i["invoice_id"], -i["amount"], by, now, f"Reversal {rv['rev_number']}"))
            con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid - ?, updated_at=? WHERE id=?", (i["amount"], now, i["invoice_id"]))
            db._pi_payment_row(con, i["invoice_id"], -i["amount"], "kredit", by, reference_no=cn["cn_number"], note=f"Reversal {rv['rev_number']}")
            con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, ?, 'kredit', ?)",
                        (i["invoice_id"], now, by, f"Reversal {rv['rev_number']}: alokasi {cn['cn_number']} {_rp(i['amount'])} dibalik; "
                                                    f"sisa tagihan {_rp(i['due_before'])} → {_rp(i['due_after'])}"))
        con.execute("""UPDATE supplier_credit_notes SET status='reversed', reversed_by=?, reversed_at=?, reversal_id=?, cancelled_by=?, cancelled_at=?,
                       cancel_reason=? WHERE id=?""", (by, now, reversal_id, by, now, rv["reason"], cn["id"]))
        new_id = None
        if rv["make_replacement"]:
            lines = _lines(con, cn["id"])
            target = rv["correction_amount"] if rv["correction_amount"] is not None else cn["amount"]
            base = sum(ln["amount"] for ln in lines) or 1
            fresh, given = [], 0
            for k, ln in enumerate(lines):
                amt = target - given if k == len(lines) - 1 else int(round(ln["amount"] * target / base))
                given += amt
                sub, tax = _split(amt, ln["tax_rate"])
                fresh.append({**ln, "amount": amt, "subtotal": sub, "tax": tax, "entitled": ln["amount"] + ln["waived"], "waived": 0, "waive_reason": None})
            if fresh:
                new = create_draft(con, supplier_id=cn["supplier_id"], return_id=cn["return_id"], invoice_id=cn["purchase_invoice_id"], lines=fresh, by=by,
                                   note=f"Koreksi dari {cn['cn_number']} (reversal {rv['rev_number']})", source="koreksi", corrects_id=cn["id"])
                new_id = new["id"]
        con.execute("UPDATE supplier_credit_reversals SET status='disetujui', decided_by=?, decided_at=?, decision_note=?, new_credit_note_id=? WHERE id=?",
                    (by, now, note, new_id, reversal_id))
        txt = (f"Reversal {rv['rev_number']} disetujui Owner dan dijalankan: {cn['cn_number']} {_rp(cn['amount'])} → Reversed"
               + ("; alokasi dibalik " + ", ".join(f"{i['invoice_number']} {_rp(i['amount'])}" for i in impact["invoices"]) if impact["invoices"] else "")
               + (f"; draft pengganti dibuat" if new_id else "") + (f". Catatan: {note}" if note else ""))
        _event(con, cn["id"], "reversal", txt, by, -cn["amount"], {"status": "aktif"}, {"status": "reversed"})
        _return_event(con, cn, txt, by)
    return detail(cn["id"], user)


# ---------------------------------------------------------------- files

def _upload_dir(cn_id: int) -> Path:
    d = db.DB_PATH.parent / "uploads" / "credit-notes" / str(cn_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_file(cn_id: int, filename: str, mime: str, data: bytes, user: dict, refund_id: int | None = None) -> dict:
    _require_admin(user, "melampirkan bukti Credit Note")
    if mime not in FILE_MIMES:
        raise ValueError("Bukti harus JPG, PNG, WEBP, atau PDF.")
    if not data or len(data) > FILE_MAX_BYTES:
        raise ValueError("Ukuran bukti maksimal 8 MB.")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        if cn["status"] in ("reversed", "dibatalkan", "ditolak"):
            raise Conflict("Credit Note ini sudah ditutup.")
        name = f"{uuid.uuid4().hex}{FILE_MIMES[mime]}"
        path = _upload_dir(cn_id) / name
        path.write_bytes(data)
        fid = con.execute("""INSERT INTO supplier_credit_note_files (credit_note_id, refund_id, filename, mime, path, size, created_by, created_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (cn_id, refund_id, (filename or name)[:120], mime, str(path), len(data), by, db._now())).lastrowid
        _event(con, cn_id, "lampiran", f"Bukti dilampirkan: {(filename or name)[:120]}" + (" (refund)" if refund_id else ""), by)
    return {"id": fid}


def get_file(file_id: int) -> dict:
    with db._conn() as con:
        row = con.execute("SELECT * FROM supplier_credit_note_files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise LookupError("Lampiran tidak ditemukan.")
    return dict(row)


def delete_file(cn_id: int, file_id: int, user: dict) -> dict:
    _require_admin(user, "menghapus lampiran")
    by = user.get("username")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = _get(con, cn_id)
        row = con.execute("SELECT * FROM supplier_credit_note_files WHERE id=? AND credit_note_id=?", (file_id, cn_id)).fetchone()
        if row is None:
            raise LookupError("Lampiran tidak ditemukan.")
        if cn["status"] != "draft":
            raise Conflict(_err("ERR-S01", "Dokumen ini sudah digunakan oleh transaksi lain dan tidak dapat dihapus. Gunakan pembatalan atau koreksi yang sesuai."))
        con.execute("DELETE FROM supplier_credit_note_files WHERE id=?", (file_id,))
        _event(con, cn_id, "lampiran", f"Lampiran {row['filename']} dihapus dari Draft", by)
    try:
        Path(row["path"]).unlink()
    except OSError:
        pass
    return detail(cn_id, user)


# ---------------------------------------------------------------- reads

def _row_summary(con: sqlite3.Connection, c: dict) -> dict:
    allocated = _allocated(con, c["id"])
    refunded = _refunded(con, c["id"])
    avail = available(con, c)
    st = _alloc_state(c, allocated, refunded) if c["status"] == "aktif" else None
    return {**c, "allocated": allocated, "refunded": refunded, "available": avail, "status_label": CN_STATUS.get(c["status"], c["status"]),
            "alloc_state": st, "alloc_label": ALLOC_LABELS.get(st) if st else None}


def list_notes(filters: dict) -> dict:
    with db._conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT c.*, s.name AS supplier_name, r.return_number, pi.invoice_number, pi.supplier_invoice_no,
                      (SELECT GROUP_CONCAT(DISTINCT i.invoice_number) FROM supplier_credit_allocations a JOIN purchase_invoices i ON i.id = a.invoice_id
                        WHERE a.credit_note_id = c.id AND a.amount > 0) AS alloc_invoices,
                      (SELECT 1 FROM supplier_credit_reversals v WHERE v.credit_note_id = c.id AND v.status='menunggu') AS reversal_pending
               FROM supplier_credit_notes c JOIN suppliers s ON s.id = c.supplier_id
               LEFT JOIN supplier_returns r ON r.id = c.return_id LEFT JOIN purchase_invoices pi ON pi.id = c.purchase_invoice_id
               WHERE c.kind='kredit' ORDER BY c.created_at DESC, c.id DESC""")]
        rows = [_row_summary(con, r) for r in rows]
        today = db.shop_today().isoformat()
        month = today[:7]
        posted = [r for r in rows if r["status"] == "aktif"]
        kpi = {"posted_total": sum(r["amount"] for r in posted), "posted_count": len(posted),
               "available": sum(r["available"] for r in posted), "available_count": sum(1 for r in posted if r["available"] > 0),
               "allocated": sum(r["allocated"] for r in posted), "refunded": sum(r["refunded"] for r in posted),
               "open_count": sum(1 for r in rows if r["status"] in OPEN), "open_total": sum(r["amount"] for r in rows if r["status"] in OPEN),
               "review_count": sum(1 for r in rows if r["status"] in ("review", "disetujui")),
               "reversal_pending": sum(1 for r in rows if r["reversal_pending"]),
               "month_posted": sum(r["amount"] for r in posted if (r["posted_at"] or "")[:7] == month)}
        creditable = creditable_returns(con)
        suppliers = [dict(r) for r in con.execute(
            "SELECT DISTINCT s.id, s.name FROM supplier_credit_notes c JOIN suppliers s ON s.id = c.supplier_id WHERE c.kind='kredit' ORDER BY s.name")]
    f = filters or {}
    tab = f.get("tab") or "semua"
    out = []
    for r in rows:
        if tab == "proses" and r["status"] not in OPEN:
            continue
        if tab == "tersedia" and not (r["status"] == "aktif" and r["available"] > 0):
            continue
        if tab == "teralokasi" and not (r["status"] == "aktif" and r["available"] == 0):
            continue
        if tab == "ditutup" and r["status"] not in ("reversed", "dibatalkan", "ditolak"):
            continue
        if f.get("status") and r["status"] != f["status"]:
            continue
        if f.get("supplier_id") and r["supplier_id"] != int(f["supplier_id"]):
            continue
        d = (r["issued_at"] or "")[:10]
        if f.get("date_from") and d < f["date_from"]:
            continue
        if f.get("date_to") and d > f["date_to"]:
            continue
        if f.get("q"):
            hay = " ".join(str(x or "") for x in (r["cn_number"], r["supplier_cn_no"], r["supplier_name"], r["return_number"], r["invoice_number"],
                                                    r["supplier_invoice_no"], r["alloc_invoices"])).lower()
            if f["q"].strip().lower() not in hay:
                continue
        out.append(r)
    sp = None
    if f.get("supplier_id"):
        with db._conn() as con:
            sp = supplier_position(con, int(f["supplier_id"]))
    return {"items": out, "kpi": kpi, "creditable_returns": creditable, "suppliers": suppliers, "supplier_position": sp,
            "as_of": db.shop_today().isoformat()}


def creditable_returns(con: sqlite3.Connection | None = None) -> list[dict]:
    """Returns whose supplier-agreed credit is not fully on a note yet (source for 'Buat dari Retur')."""
    own = con is None
    con = con or db._conn()
    try:
        out = []
        for r in con.execute("""SELECT r.*, s.name AS supplier_name FROM supplier_returns r JOIN suppliers s ON s.id = r.supplier_id
                                WHERE r.doc_status IN ('diproses','selesai') AND EXISTS (SELECT 1 FROM supplier_return_items i
                                WHERE i.return_id = r.id AND i.settlement='kredit' AND i.qty_approved > 0) ORDER BY r.id DESC""").fetchall():
            r = dict(r)
            ent = _entitlements(con, r)
            free = min(sum(e["remaining"] for e in ent["lines"]), ent["pool"])
            draft = con.execute("SELECT id, cn_number FROM supplier_credit_notes WHERE return_id=? AND kind='kredit' AND status IN ('draft','review','disetujui')",
                                (r["id"],)).fetchone()
            if free > 0 or draft:
                out.append({"id": r["id"], "return_number": r["return_number"], "return_date": r["return_date"], "supplier_id": r["supplier_id"],
                            "supplier_name": r["supplier_name"], "handover_status": r["handover_status"], "claim": ent["claim"], "free": free,
                            "open_draft": dict(draft) if draft else None})
        return out
    finally:
        if own:
            con.close()


def detail(cn_id: int, user: dict | None = None) -> dict:
    with db._conn() as con:
        c = _get(con, cn_id)
        sup = con.execute("SELECT * FROM suppliers WHERE id=?", (c["supplier_id"],)).fetchone()
        c = _row_summary(con, c)
        lines = _lines(con, cn_id)
        ret = items = None
        ent = None
        if c["return_id"]:
            ret = db._sr_get(con, c["return_id"])
            items = db._sr_items(con, c["return_id"])
            if c["status"] in OPEN:
                ent = _entitlements(con, ret, cn_id)
                free = {(e["kind"], e["return_item_id"]): e["remaining"] for e in ent["lines"]}
                for ln in lines:
                    ln["room"] = free.get((ln["kind"], ln["return_item_id"]), 0)
        allocs = [dict(a) for a in con.execute(
            """SELECT a.*, i.invoice_number, i.supplier_invoice_no, i.total AS invoice_total, i.amount_paid AS invoice_paid, i.status AS invoice_status,
                      (SELECT 1 FROM supplier_credit_allocations b WHERE b.reverses_id = a.id) AS undone
               FROM supplier_credit_allocations a JOIN purchase_invoices i ON i.id = a.invoice_id WHERE a.credit_note_id=? ORDER BY a.id""", (cn_id,))]
        refunds = [dict(r) for r in con.execute("SELECT * FROM supplier_credit_refunds WHERE credit_note_id=? ORDER BY id", (cn_id,))]
        files = [{k: f[k] for k in ("id", "refund_id", "filename", "mime", "size", "created_by", "created_at")}
                 for f in con.execute("SELECT * FROM supplier_credit_note_files WHERE credit_note_id=? ORDER BY id", (cn_id,))]
        events = [dict(e) for e in con.execute("SELECT * FROM supplier_credit_note_events WHERE credit_note_id=? ORDER BY id DESC", (cn_id,))]
        reversals = [dict(v) for v in con.execute(
            """SELECT v.*, n.cn_number AS new_cn_number FROM supplier_credit_reversals v LEFT JOIN supplier_credit_notes n ON n.id = v.new_credit_note_id
               WHERE v.credit_note_id=? ORDER BY v.id DESC""", (cn_id,))]
        for v in reversals:
            v["impact"] = json.loads(v.pop("impact_json") or "{}")
        corrects = con.execute("SELECT id, cn_number, status FROM supplier_credit_notes WHERE id=?", (c["corrects_id"],)).fetchone() if c["corrects_id"] else None
        corrected_by = [dict(r) for r in con.execute("SELECT id, cn_number, status FROM supplier_credit_notes WHERE corrects_id=?", (cn_id,))]
        invoices = [dict(r) for r in con.execute(
            """SELECT i.id, i.invoice_number, i.supplier_invoice_no, i.invoice_date, i.due_date, i.total, i.amount_paid, i.status,
                      MAX(i.total - i.amount_paid, 0) AS outstanding, sp.po_number
               FROM purchase_invoices i LEFT JOIN supplier_pos sp ON sp.id = i.supplier_po_id
               WHERE i.supplier_id=? AND (i.status='posted' OR i.id=?) ORDER BY (i.id = ?) DESC, i.due_date, i.id""",
            (c["supplier_id"], c["purchase_invoice_id"] or 0, c["purchase_invoice_id"] or 0))]
        for i in invoices:
            i["is_origin"] = i["id"] == c["purchase_invoice_id"]
            i["pay_status"] = db.compute_payment_status(i["total"], i["amount_paid"])
            parts = {r["kind"]: r["n"] for r in con.execute("SELECT kind, SUM(amount) AS n FROM purchase_invoice_payments WHERE invoice_id=? GROUP BY kind", (i["id"],))}
            i["paid_cash"] = parts.get("bayar", 0) + parts.get("batal", 0) + parts.get("dp", 0) + parts.get("dp_batal", 0)
            i["paid_credit"] = parts.get("kredit", 0)
        origin = next((i for i in invoices if i["is_origin"]), None)
        position = supplier_position(con, c["supplier_id"])
        chain = _chain(con, c, ret, items, origin)
    is_admin = (user or {}).get("role") == "admin"
    is_owner = is_admin and bool((user or {}).get("is_owner"))
    pending = next((v for v in reversals if v["status"] == "menunggu"), None)
    checks = []
    if c["status"] in OPEN:
        with db._conn() as con:
            checks = _check_ready(con, c)
    out = {
        **c, "supplier_name": sup["name"] if sup else None, "supplier_code": sup["code"] if sup else None, "supplier_phone": sup["phone"] if sup and "phone" in sup.keys() else None,
        "supplier_address": sup["address"] if sup and "address" in sup.keys() else None,
        "lines": lines, "allocations": allocs, "refunds": refunds, "files": files, "events": events, "reversals": reversals,
        "pending_reversal": pending, "corrects": dict(corrects) if corrects else None, "corrected_by": corrected_by,
        "invoices": invoices, "origin_invoice": origin, "position": position, "chain": chain,
        "return": {"id": ret["id"], "return_number": ret["return_number"], "return_date": ret["return_date"], "doc_status": ret["doc_status"],
                   "doc_status_label": db.SR_DOC_STATUS.get(ret["doc_status"]), "handover_status": ret["handover_status"],
                   "handover_label": db.SR_HANDOVER.get(ret["handover_status"]), "rma_number": ret["rma_number"], "decided_at": ret["decided_at"],
                   "decision_channel": ret["decision_channel"], "origin": ret["origin"],
                   "items": [{k: i.get(k) for k in ("id", "product_name", "sku", "unit", "qty", "qty_approved", "qty_rejected", "unit_cost", "settlement",
                                                   "reason_code", "reject_reason", "inbound_item_id")} | {"reason_label": db.SR_REASONS.get(i["reason_code"] or "", "")}
                             for i in items]} if ret else None,
        "pool": ent["pool"] if ent else None, "claim": ent["claim"] if ent else None,
        "check_errors": checks, "checks_catalog": CHECKS, "checklist": json.loads(c["checklist"] or "[]"),
        "refund_methods": REFUND_METHODS,
        "can": {
            "edit": is_admin and c["status"] == "draft",
            "submit": is_admin and c["status"] == "draft",
            "approve": is_admin and c["status"] == "review",
            "send_back": is_admin and c["status"] in ("review", "disetujui"),
            "reject": is_admin and c["status"] in OPEN,
            "post": is_admin and c["status"] == "disetujui" and not checks,
            "allocate": is_admin and c["status"] == "aktif" and c["available"] > 0 and not pending,
            "refund": is_admin and c["status"] == "aktif" and c["available"] > 0 and not pending,
            "request_reversal": is_admin and c["status"] == "aktif" and not pending,
            "decide_reversal": is_owner and bool(pending),
        },
    }
    return out


def _chain(con: sqlite3.Connection, c: dict, ret: dict | None, items: list[dict] | None, origin: dict | None) -> dict:
    """PO → GR → Faktur → Retur → CN → Alokasi, with the figures each module owns (Gambar 16)."""
    po = gr = None
    po_id = (ret or {}).get("supplier_po_id")
    if not po_id and origin:
        row = con.execute("SELECT supplier_po_id FROM purchase_invoices WHERE id=?", (origin["id"],)).fetchone()
        po_id = row["supplier_po_id"] if row else None
    if po_id:
        p = con.execute("SELECT id, po_number, status, (SELECT COALESCE(SUM(quantity), 0) FROM supplier_po_items WHERE supplier_po_id = supplier_pos.id) AS qty "
                        "FROM supplier_pos WHERE id=?", (po_id,)).fetchone()
        po = dict(p) if p else None
    doc_id = (ret or {}).get("inbound_document_id")
    if doc_id:
        g = con.execute("SELECT id, doc_number, received_date, status, (SELECT COALESCE(SUM(qty_in), 0) FROM inbound_items WHERE document_id = inbound_documents.id) AS qty "
                        "FROM inbound_documents WHERE id=?", (doc_id,)).fetchone()
        gr = dict(g) if g else None
    ret_qty = sum(i["qty"] for i in items or [])
    out_qty = ret_qty if ret and ret["handover_status"] in db.SR_OUT else 0
    return {"po": po, "gr": gr, "invoice": origin,
            "return": {"id": ret["id"], "return_number": ret["return_number"], "qty": ret_qty, "qty_out": out_qty,
                       "handover_label": db.SR_HANDOVER.get(ret["handover_status"])} if ret else None,
            "credit": {"amount": c["amount"], "allocated": c["allocated"], "refunded": c["refunded"], "available": c["available"], "status_label": c["status_label"]}}


def audit(filters: dict) -> dict:
    f = filters or {}
    where, params = ["1=1"], []
    if f.get("cn_id"):
        where.append("e.credit_note_id=?"); params.append(int(f["cn_id"]))
    if f.get("supplier_id"):
        where.append("c.supplier_id=?"); params.append(int(f["supplier_id"]))
    if f.get("kind"):
        where.append("e.kind=?"); params.append(f["kind"])
    if f.get("user"):
        where.append("e.by_user=?"); params.append(f["user"])
    if f.get("date_from"):
        where.append("substr(e.at,1,10)>=?"); params.append(f["date_from"])
    if f.get("date_to"):
        where.append("substr(e.at,1,10)<=?"); params.append(f["date_to"])
    if f.get("q"):
        where.append("(LOWER(c.cn_number) LIKE ? OR LOWER(COALESCE(e.note,'')) LIKE ? OR LOWER(s.name) LIKE ?)")
        params += [f"%{f['q'].strip().lower()}%"] * 3
    with db._conn() as con:
        rows = [dict(r) for r in con.execute(
            f"""SELECT e.*, c.cn_number, c.supplier_id, s.name AS supplier_name FROM supplier_credit_note_events e
                JOIN supplier_credit_notes c ON c.id = e.credit_note_id JOIN suppliers s ON s.id = c.supplier_id
                WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT 1000""", params)]
        users = [r["by_user"] for r in con.execute("SELECT DISTINCT by_user FROM supplier_credit_note_events WHERE by_user IS NOT NULL ORDER BY by_user")]
    return {"items": rows, "users": users, "kinds": EVENT_KINDS}


EVENT_KINDS = {"draft": "Draft dibuat", "ubah": "Draft diubah", "lampiran": "Lampiran", "ajukan": "Diajukan verifikasi", "setujui": "Disetujui",
               "kembali_draft": "Dikembalikan ke Draft", "tolak": "Ditolak", "batal": "Dibatalkan", "posting": "Posting",
               "alokasi": "Alokasi", "batal_alokasi": "Batal alokasi", "refund": "Refund diterima", "batal_refund": "Refund dibatalkan",
               "ajukan_reversal": "Reversal diajukan", "tolak_reversal": "Reversal ditolak", "reversal": "Reversal"}
