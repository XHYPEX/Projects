"""Three-Way Matching and Verifikasi Faktur.

A faktur that was made from a Purchase Order is checked against that PO and its Goods Receipts before it
can be posted (become debt):

  * every faktur line is a product of the PO,
  * the quantity billed does not exceed what has been received (Goods Receipt),
  * the unit price is the price the posted Goods Receipt confirmed (or the PO price, after its line discount),
  * the supplier is the PO's supplier, and the supplier's faktur number is not used twice.

Everything matches -> any user can verify it (a four-point checklist is recorded). Something differs -> the
faktur cannot be verified: it is held for clarification, or an Admin approves the difference as an
exception, with a reason. Verification only gates posting; it never moves stock or money. Faktur without a
PO are not matched and post as before.
"""
from __future__ import annotations

import sqlite3

from backend import database as db

CHECKLIST = (
    ("supplier_dokumen", "Supplier dan dokumen faktur telah diverifikasi"),
    ("qty_cocok", "Jumlah barang cocok dengan penerimaan (Goods Receipt)"),
    ("harga_diskon", "Harga beli dan diskon telah diperiksa"),
    ("tidak_duplikat", "Tidak ditemukan faktur duplikat"),
)
STATUS_LABELS = {"belum": "Belum diverifikasi", "terverifikasi": "Terverifikasi", "ditahan": "Ditahan (klarifikasi)",
                 "diajukan": "Menunggu persetujuan Admin", "pengecualian": "Disetujui sebagai pengecualian"}


def _key(master_id, name) -> tuple:
    return ("m", master_id) if master_id else ("n", (name or "").strip().lower())


def match_lines(con: sqlite3.Connection, po_id: str, items: list[dict]) -> list[dict]:
    """PO ↔ Goods Receipt ↔ Faktur, one row per product. For checking only."""
    po_groups: dict[tuple, dict] = {}
    over = db.po_over_qty(con, po_id)
    for p in con.execute("SELECT * FROM supplier_po_items WHERE supplier_po_id=? ORDER BY position, id", (po_id,)).fetchall():
        g = po_groups.setdefault(_key(p["master_item_id"], p["product_name"]),
                                 {"name": p["product_name"], "qty_po": 0, "qty_gr": 0, "qty_over": 0, "prices": set(), "gr_prices": set()})
        g["qty_po"] += p["quantity"] - (p["qty_cancelled"] or 0)
        # An excess the Admin accepted on the Goods Receipt may be billed too (it was received, just not ordered).
        g["qty_gr"] += p["qty_received"] + over.get(p["id"], 0)
        g["qty_over"] += over.get(p["id"], 0)
        g["prices"].add(db._po_net_price(p))
    # The price the posted Goods Receipt confirmed is the operational reference: a faktur at that price is not
    # flagged again for the PO-vs-GR difference already confirmed there.
    for b in db.po_billable_lines(con, po_id):
        g = po_groups.get(_key(b["master_item_id"], b["product_name"]))
        if g is not None:
            g["prices"].add(b["unit_price"])
            g["gr_prices"].add(b["unit_price"])
    inv_groups: dict[tuple, dict] = {}
    for it in items:
        g = inv_groups.setdefault(_key(it.get("master_item_id"), it["product_name"]), {"name": it["product_name"], "qty": 0, "prices": []})
        g["qty"] += it["qty"]
        g["prices"].append(it["unit_price"])
    # A faktur line is matched by master item when it has one, otherwise by name; a name match also catches a PO line without master.
    out = []
    for key, g in inv_groups.items():
        po = po_groups.get(key)
        if po is None:
            by_name = next((v for k, v in po_groups.items() if v["name"].strip().lower() == g["name"].strip().lower()), None)
            po = by_name
        price_inv = g["prices"][0]
        if po is None:
            out.append({"product": g["name"], "qty_po": None, "qty_gr": None, "qty_inv": g["qty"], "price_po": None, "price_inv": price_inv,
                        "result": "tidak_di_po", "note": "Barang ini tidak ada di PO", "diff_value": g["qty"] * price_inv})
            continue
        notes = []
        diff = 0
        if g["qty"] > po["qty_gr"]:
            notes.append(f"Qty faktur melebihi barang diterima ({g['qty']} > {po['qty_gr']})")
            diff += (g["qty"] - po["qty_gr"]) * price_inv
        if g["qty"] > po["qty_po"] + po["qty_over"]:
            notes.append("Qty faktur melebihi PO")
        bad_price = [pr for pr in g["prices"] if pr not in po["prices"]]
        ref = sorted(po["gr_prices"]) or sorted(po["prices"])
        price_ref = ref[0] if ref else 0
        if bad_price:
            notes.append(f"Harga faktur beda dari harga Goods Receipt / PO (Rp {bad_price[0]:,} vs Rp {price_ref:,})".replace(",", "."))
            diff += abs(bad_price[0] - price_ref) * min(g["qty"], po["qty_gr"] or g["qty"])
        out.append({"product": g["name"], "qty_po": po["qty_po"], "qty_over": po["qty_over"], "qty_gr": po["qty_gr"], "qty_inv": g["qty"], "price_po": price_ref,
                    "price_inv": price_inv, "result": "selisih" if notes else "sesuai", "note": "; ".join(notes), "diff_value": diff})
    return out


def match_invoice(con: sqlite3.Connection, invoice_id: int) -> dict:
    inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError("Faktur pembelian tidak ditemukan.")
    inv = dict(inv)
    base = {"invoice_id": invoice_id, "status": inv["status"], "verify_status": inv["verify_status"]}
    if not inv["supplier_po_id"]:
        return {**base, "mode": "tanpa_po", "result": "tanpa_po", "lines": [], "checks": [], "outstanding_po": [], "summary": {}}
    po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (inv["supplier_po_id"],)).fetchone()
    items = [dict(r) for r in con.execute("SELECT * FROM purchase_invoice_items WHERE invoice_id=? ORDER BY line_no, id", (invoice_id,)).fetchall()]
    lines = match_lines(con, inv["supplier_po_id"], items)
    outstanding = []
    for p in con.execute("SELECT * FROM supplier_po_items WHERE supplier_po_id=?", (inv["supplier_po_id"],)).fetchall():
        left = p["quantity"] - (p["qty_cancelled"] or 0) - p["qty_received"]
        if left > 0:
            price = p["est_unit_price"] - round(p["est_unit_price"] * (p["discount_pct"] or 0) / 100)
            outstanding.append({"product": p["product_name"], "qty": left, "value": left * price})
    dup = con.execute(
        "SELECT invoice_number FROM purchase_invoices WHERE supplier_id=? AND status<>'void' AND id<>? AND TRIM(IFNULL(supplier_invoice_no,''))<>'' "
        "AND UPPER(TRIM(supplier_invoice_no))=UPPER(TRIM(?))", (inv["supplier_id"], invoice_id, inv["supplier_invoice_no"] or "")).fetchone()
    bad_qty = [l for l in lines if l["result"] == "tidak_di_po" or (l["qty_gr"] is not None and l["qty_inv"] > l["qty_gr"]) or (l["qty_po"] is not None and l["qty_inv"] > l["qty_po"] + (l.get("qty_over") or 0))]
    bad_price = [l for l in lines if l["result"] == "selisih" and "Harga" in l["note"]]
    checks = [
        {"key": "supplier", "label": "Supplier faktur sama dengan supplier PO", "ok": po is not None and po["supplier_id"] == inv["supplier_id"]},
        {"key": "po", "label": "PO masih aktif (sudah diterbitkan, belum dibatalkan)", "ok": po is not None and po["status"] in ("dipesan", "sebagian_diterima", "diterima")},
        {"key": "qty", "label": "Qty faktur tidak melebihi barang yang diterima", "ok": not bad_qty},
        {"key": "harga", "label": "Harga sesuai harga Goods Receipt / PO", "ok": not bad_price},
        {"key": "duplikat", "label": "Nomor faktur supplier belum pernah dicatat", "ok": dup is None},
    ]
    ok = all(c["ok"] for c in checks)
    summary = {"invoice_total": inv["total"], "gr_value": sum(b["qty"] * b["unit_price"] for b in db.po_billable_lines(con, inv["supplier_po_id"])),
               "po_outstanding": sum(o["value"] for o in outstanding), "diff_value": sum(l["diff_value"] for l in lines),
               "po_number": con.execute("SELECT po_number FROM supplier_pos WHERE id=?", (inv["supplier_po_id"],)).fetchone()["po_number"] if po else None}
    # a stored 'terverifikasi' only counts while the documents still match what was verified
    effective = inv["verify_status"]
    if effective == "terverifikasi" and not ok:
        effective = "perlu_ulang"
    return {**base, "mode": "po", "result": "cocok" if ok else "selisih", "lines": lines, "checks": checks, "outstanding_po": outstanding,
            "summary": summary, "effective_status": effective}


def _event(con: sqlite3.Connection, invoice_id: int, kind: str, note: str | None, by: str | None) -> None:
    con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, ?, ?, ?)", (invoice_id, db._now(), by, kind, note))
    po = con.execute("SELECT supplier_po_id, invoice_number FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if po and po["supplier_po_id"]:
        db._spo_event(con, po["supplier_po_id"], "invoice", f"Faktur {po['invoice_number']}: {note or kind}", by)


def _draft_po_invoice(con: sqlite3.Connection, invoice_id: int) -> dict:
    inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError("Faktur pembelian tidak ditemukan.")
    if inv["status"] != "draft":
        raise ValueError("Verifikasi hanya untuk faktur yang masih draft.")
    if not inv["supplier_po_id"]:
        raise ValueError("Faktur ini tidak dibuat dari Purchase Order, jadi tidak perlu dicocokkan. Langsung diposting saja.")
    return dict(inv)


def verify(invoice_id: int, checklist: dict, note: str | None, by: str | None) -> dict:
    missing = [label for key, label in CHECKLIST if not (checklist or {}).get(key)]
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_po_invoice(con, invoice_id)
        m = match_invoice(con, invoice_id)
        if m["result"] != "cocok":
            raise ValueError("Faktur belum dapat diverifikasi karena ada selisih dengan PO / Goods Receipt. "
                             "Tahan faktur untuk klarifikasi, atau minta Admin menyetujui pengecualian.")
        if missing:
            raise ValueError("Lengkapi semua butir pemeriksaan sebelum verifikasi.")
        con.execute("UPDATE purchase_invoices SET verify_status='terverifikasi', verify_note=?, verified_by=?, verified_at=?, "
                    "exception_reason=NULL, hold_note=NULL, updated_at=? WHERE id=?",
                    ((note or "").strip() or None, by, db._now(), db._now(), invoice_id))
        _event(con, invoice_id, "verify", "Terverifikasi: 3 dokumen cocok" + (f". {note.strip()}" if (note or "").strip() else ""), by)
    return status(invoice_id)


def hold(invoice_id: int, note: str, by: str | None, ask_admin: bool = False) -> dict:
    note = (note or "").strip()
    if len(note) < 3:
        raise ValueError("Tulis catatan klarifikasi (minimal 3 karakter).")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_po_invoice(con, invoice_id)
        st = "diajukan" if ask_admin else "ditahan"
        con.execute("UPDATE purchase_invoices SET verify_status=?, hold_note=?, verified_by=NULL, verified_at=NULL, verify_note=NULL, "
                    "exception_reason=NULL, updated_at=? WHERE id=?", (st, note, db._now(), invoice_id))
        _event(con, invoice_id, "hold" if not ask_admin else "ask_admin",
               ("Ditahan untuk klarifikasi supplier" if not ask_admin else "Diajukan ke Admin sebagai pengecualian") + f": {note}", by)
    return status(invoice_id)


def approve_exception(invoice_id: int, reason: str, by: str | None) -> dict:
    """Admin only (enforced by the route): the difference is accepted knowingly and the reason is kept."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan persetujuan pengecualian wajib diisi (minimal 5 karakter).")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_po_invoice(con, invoice_id)
        m = match_invoice(con, invoice_id)
        if m["result"] == "cocok":
            raise ValueError("Faktur ini cocok dengan PO / Goods Receipt, tidak perlu pengecualian. Verifikasi saja.")
        diffs = "; ".join(f"{l['product']}: {l['note']}" for l in m["lines"] if l["note"])[:300]
        con.execute("UPDATE purchase_invoices SET verify_status='pengecualian', exception_reason=?, verified_by=?, verified_at=?, hold_note=NULL, "
                    "updated_at=? WHERE id=?", (reason, by, db._now(), db._now(), invoice_id))
        _event(con, invoice_id, "exception", f"Selisih disetujui Admin sebagai pengecualian. Alasan: {reason}. Selisih: {diffs}", by)
    return status(invoice_id)


def require_verified(con: sqlite3.Connection, inv: dict) -> None:
    """The gate in post_purchase_invoice: a faktur from a PO is posted only after it has been verified."""
    if not inv.get("supplier_po_id"):
        return
    st = inv.get("verify_status") or "belum"
    if st == "pengecualian":
        return
    if st == "terverifikasi":
        if match_invoice(con, inv["id"])["result"] == "cocok":
            return
        raise ValueError("Hasil pencocokan faktur berubah sejak diverifikasi (barang diterima atau PO berubah). Verifikasi ulang sebelum posting.")
    if st in ("ditahan", "diajukan"):
        raise ValueError("Faktur ini sedang ditahan untuk klarifikasi" + (" dan menunggu persetujuan Admin" if st == "diajukan" else "") + ". Selesaikan dulu sebelum posting.")
    raise ValueError("Faktur dari Purchase Order harus diverifikasi dulu (Three-Way Matching) sebelum diposting.")


def events(invoice_id: int) -> list[dict]:
    with db._conn() as con:
        return [dict(r) for r in con.execute("SELECT at, by_user, kind, note FROM purchase_invoice_events WHERE invoice_id=? ORDER BY at, id", (invoice_id,)).fetchall()]


def status(invoice_id: int) -> dict:
    """Everything the verification panel needs: the matching, the stored state and the trail."""
    with db._conn() as con:
        m = match_invoice(con, invoice_id)
        inv = dict(con.execute("SELECT verify_status, verify_note, verified_by, verified_at, exception_reason, hold_note FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone())
    return {**m, **inv, "status_label": STATUS_LABELS.get(m.get("effective_status") or inv["verify_status"], "Perlu verifikasi ulang"),
            "checklist": [{"key": k, "label": l} for k, l in CHECKLIST], "events": events(invoice_id),
            "gate": m["mode"] == "po" and m["status"] == "draft"}
