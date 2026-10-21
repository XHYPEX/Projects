"""Goods Receipt (Barang Masuk) — the one way goods from a Purchase Order become stock.

A GR is an inbound document whose lines all come from the Purchase Orders picked for it (one or more POs of the
same supplier); nothing is typed in by hand. While it is a draft the received qty, the good / damaged split and the
actual purchase price can change. Posting it is the single moment stock, quarantine and the PO's received qty
move (database.post_inbound_document); everything here only prepares, checks or reports.

Rules the owner set (2026-10-21):
  * qty beyond what a PO still expects needs an Admin (K-11): the excess is general stock at a price the Admin
    confirms, never counted on the PO and never given to a Sales Order;
  * a price off the PO needs a reason and a confirmation, and beyond the tolerance (default 5%) an Admin;
  * a posted GR is final — corrections are an Admin void plus a new draft made from it;
  * every SKU has one Kode Manual (typed by an Admin) that its labels encode; printing never touches stock.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from backend import database as db

ARRIVAL_METHODS = {"diantar_supplier": "Diantar langsung supplier", "ekspedisi": "Lewat ekspedisi / kurir",
                   "ambil_sendiri": "Diambil sendiri"}
BAD_CONDITIONS = {"rusak": "Rusak", "tidak_sesuai": "Tidak sesuai"}
OPEN_PO = ("dipesan", "sebagian_diterima")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,29}$")


def _text(v) -> str | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def _int(v, label: str, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{label} harus bilangan bulat.")
    if v < lo:
        raise ValueError(f"{label} tidak boleh kurang dari {lo}.")
    return v


def _left(spi) -> int:
    return max(spi["quantity"] - spi["qty_cancelled"] - spi["qty_received"], 0)


def _validate_when(received_date: str | None, received_time: str | None) -> tuple[str, str]:
    try:
        rd = date.fromisoformat(received_date or "")
    except ValueError:
        raise ValueError("Tanggal terima tidak valid, gunakan format YYYY-MM-DD.")
    today = db.shop_today()
    if rd > today:
        raise ValueError("Tanggal terima tidak boleh di masa depan.")
    if (today - rd).days > 30:
        raise ValueError("Tanggal terima tidak boleh lebih dari 30 hari yang lalu.")
    t = (received_time or "").strip()
    try:
        datetime.strptime(t, "%H:%M")
    except ValueError:
        raise ValueError("Jam tiba wajib diisi (format JJ:MM).")
    return rd.isoformat(), t


# --- sources: suppliers and their open POs -----------------------------------------------------------------


def _open_po_lines(con: sqlite3.Connection, po_id: str) -> list[dict]:
    out = []
    for i in db._spo_items(con, po_id):
        left = _left(i)
        packages = [{"id": p["id"], "package_no": p["package_no"], "forwarder_name": p.get("forwarder_name"),
                     "remaining": next(l["remaining"] for l in p["lines"] if l["item_id"] == i["id"])}
                    for p in db._spo_packages(con, po_id)
                    if not p["cancelled"] and any(l["item_id"] == i["id"] and l["remaining"] > 0 for l in p["lines"])]
        mi = con.execute("SELECT sku, unit, manual_code, sell_price FROM master_items WHERE id=?",
                         (i["master_item_id"],)).fetchone() if i["master_item_id"] else None
        out.append({
            "spo_item_id": i["id"], "master_item_id": i["master_item_id"], "product_name": i["product_name"],
            "sku": (mi["sku"] if mi else None) or i.get("sku"), "unit": (mi["unit"] if mi else None) or i.get("unit") or "PCS",
            "manual_code": mi["manual_code"] if mi else None, "sell_price": mi["sell_price"] if mi else None,
            "qty_po": i["quantity"] - i["qty_cancelled"], "qty_received": i["qty_received"], "qty_left": left,
            "po_price": db._po_net_price(i), "customer_po_number": i.get("customer_po_number"),
            "customer_name": i.get("customer_name"), "item_status": i.get("item_status"), "packages": packages,
        })
    return out


def gr_sources(supplier_id: int | None = None, include_po: str | None = None) -> dict:
    """Step 1 'Pilih PO': suppliers that still have goods to receive, and (for one supplier) those POs with their
    lines. `include_po` (comma-separated) keeps POs in the list: the one 'Terima Barang' was pressed on, or a draft's own."""
    with db._conn() as con:
        sups = [dict(r) for r in con.execute(
            """SELECT s.id, s.name, COUNT(DISTINCT sp.id) AS po_count FROM supplier_pos sp JOIN suppliers s ON s.id = sp.supplier_id
               WHERE sp.status IN ('dipesan','sebagian_diterima')
                 AND EXISTS (SELECT 1 FROM supplier_po_items spi WHERE spi.supplier_po_id = sp.id
                             AND spi.quantity - spi.qty_cancelled - spi.qty_received > 0)
               GROUP BY s.id ORDER BY s.name""")]
        pos = []
        keep = [p for p in (include_po or "").split(",") if p]
        if supplier_id is not None:
            for po in con.execute(
                    f"SELECT * FROM supplier_pos WHERE supplier_id=? AND (status IN ('dipesan','sebagian_diterima') "
                    f"OR id IN ({','.join('?' * len(keep)) or 'NULL'})) ORDER BY created_at, id", (supplier_id, *keep)).fetchall():
                lines = _open_po_lines(con, po["id"])
                waiting = [l for l in lines if l["qty_left"] > 0]
                if not waiting and po["id"] not in keep:
                    continue
                drafts = [dict(r) for r in con.execute(
                    """SELECT d.id, d.doc_number FROM inbound_documents d JOIN inbound_document_pos p ON p.document_id = d.id
                       WHERE p.supplier_po_id=? AND d.status='draft'""", (po["id"],))]
                pos.append({
                    "id": po["id"], "po_number": po["po_number"], "status": po["status"], "created_at": po["created_at"],
                    "order_status": db._spo_order_status(dict(po)),
                    "received_any": any(l["qty_received"] for l in lines),
                    "lines": lines, "qty_left": sum(l["qty_left"] for l in lines),
                    "value_left": sum(l["qty_left"] * l["po_price"] for l in lines),
                    "summary": ", ".join(f"{l['product_name']} · {l['qty_left']} {l['unit']}" for l in waiting[:3])
                               + (f" +{len(waiting) - 3} lagi" if len(waiting) > 3 else ""),
                    "open_drafts": drafts,
                })
        return {"suppliers": sups, "pos": pos, "tolerance_pct": db.gr_price_tolerance(con)}


def po_supplier(po_id: str) -> dict:
    with db._conn() as con:
        po = con.execute("SELECT id, supplier_id, status, po_number FROM supplier_pos WHERE id=?", (po_id,)).fetchone()
        if po is None:
            raise LookupError("Purchase Order tidak ditemukan.")
        return dict(po)


# --- draft --------------------------------------------------------------------------------------------------


def _fingerprints(rows: list[dict]) -> dict[str, set]:
    return {
        "qty": {(r["spo_item_id"], r["qty_in"], r.get("qty_bad") or 0, r["cost_price"]) for r in rows},
        "price": {(r["spo_item_id"], r["cost_price"], (r.get("price_reason") or "").strip()) for r in rows},
        "check": {(r["spo_item_id"], r["qty_in"], r.get("qty_bad") or 0, r.get("bad_condition"), (r.get("bad_note") or "").strip(),
                   (r.get("check_note") or "").strip()) for r in rows},
    }


def save_draft(document_id: int | None, data: dict, by: str | None) -> int:
    """Create or update a GR draft. Lines must all be lines of the picked POs; qty may exceed the PO here (posting
    then needs an Admin). Changing quantities or prices undoes an earlier check / confirmation / approval."""
    supplier_id = data.get("supplier_id")
    po_ids = list(dict.fromkeys(data.get("supplier_po_ids") or []))
    if not po_ids:
        raise ValueError("Pilih minimal satu Purchase Order.")
    received_date, received_time = _validate_when(data.get("received_date"), data.get("received_time"))
    officer = _text(data.get("officer"))
    if not officer:
        raise ValueError("Isi siapa yang menerima barang.")
    method = data.get("arrival_method") or "diantar_supplier"
    if method not in ARRIVAL_METHODS:
        raise ValueError("Cara barang datang tidak dikenal.")
    items = data.get("items") or []
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone() is None:
            raise ValueError("Supplier tidak ditemukan.")
        pos = {}
        for pid in po_ids:
            po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (pid,)).fetchone()
            if po is None:
                raise ValueError("Purchase Order tidak ditemukan.")
            if po["supplier_id"] != supplier_id:
                raise ValueError(f"PO {po['po_number']} milik supplier lain. Satu Goods Receipt hanya untuk satu supplier; "
                                 "buat GR terpisah untuk supplier berbeda.")
            if po["status"] not in OPEN_PO:
                raise ValueError(f"PO {po['po_number']} belum diterbitkan atau sudah selesai/dibatalkan.")
            pos[pid] = po
        rows, seen = [], set()
        for it in items:
            sid = it.get("spo_item_id")
            spi = con.execute("SELECT * FROM supplier_po_items WHERE id=?", (sid,)).fetchone() if sid else None
            if spi is None or spi["supplier_po_id"] not in pos:
                raise ValueError("Barang Goods Receipt harus berasal dari Purchase Order yang dipilih.")
            if sid in seen:
                raise ValueError(f"“{spi['product_name']}” tercantum dua kali.")
            seen.add(sid)
            if not spi["master_item_id"]:
                raise ValueError(f"“{spi['product_name']}” di PO belum terhubung ke Master Barang. Hubungkan dulu di PO.")
            qty = _int(it.get("qty_in"), f"Qty datang {spi['product_name']}", 0)
            if qty == 0:
                continue  # did not arrive this time
            bad = _int(it.get("qty_bad") or 0, f"Qty rusak {spi['product_name']}", 0)
            if bad > qty:
                raise ValueError(f"Qty rusak “{spi['product_name']}” melebihi qty datang.")
            cond = it.get("bad_condition") or None
            if cond is not None and cond not in BAD_CONDITIONS:
                raise ValueError("Jenis masalah harus Rusak atau Tidak sesuai.")
            cost = _int(it.get("cost_price"), f"Harga beli {spi['product_name']}", 0)
            pkg = it.get("spo_package_id") or None
            rows.append({
                "spo_item_id": sid, "master_item_id": spi["master_item_id"], "qty_in": qty, "cost_price": cost,
                "qty_bad": bad, "bad_condition": cond if bad else None, "bad_note": _text(it.get("bad_note")) if bad else None,
                "check_note": _text(it.get("check_note")), "price_reason": _text(it.get("price_reason")),
                "po_unit_price": db._po_net_price(spi), "spo_package_id": int(pkg) if pkg else None,
            })
        if not rows:
            raise ValueError("Isi qty datang minimal untuk satu barang.")
        header = (supplier_id, received_date, _text(data.get("supplier_invoice_no")), _text(data.get("notes")),
                  sum(r["qty_in"] * r["cost_price"] for r in rows), po_ids[0], _text(data.get("surat_jalan_no")), officer,
                  received_time, method, _text(data.get("courier")), _text(data.get("tracking_no")), _text(data.get("package_count")))
        if document_id is None:
            number = db._next_gr_number(con, db.shop_today().strftime("%Y%m%d"))
            cur = con.execute(
                """INSERT INTO inbound_documents (doc_number, supplier_id, received_date, supplier_invoice_no, notes, total_value,
                       supplier_po_id, surat_jalan_no, officer, received_time, arrival_method, courier, tracking_no, package_count,
                       status, created_at, created_by, source, flow)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, 'po_supplier', 'gr')""",
                (number, *header, db._now(), by))
            document_id = cur.lastrowid
            db._inbound_event(con, document_id, "created", "Draft Goods Receipt disimpan", by)
            before = None
        else:
            doc = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
            if doc is None:
                raise LookupError("Goods Receipt tidak ditemukan.")
            if doc["status"] != "draft":
                raise ValueError("Goods Receipt yang sudah diposting / dibatalkan tidak bisa diubah. Koreksi lewat void + buat ulang.")
            old = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=?", (document_id,))]
            if doc["flow"] != "gr" and any(not r["spo_item_id"] for r in old):
                raise ValueError("Draft sistem lama ini tidak berasal dari baris PO, jadi tidak bisa diubah di Goods Receipt. "
                                 "Periksa lalu posting, atau batalkan.")
            before = (_fingerprints(old), doc)
            con.execute(
                """UPDATE inbound_documents SET supplier_id=?, received_date=?, supplier_invoice_no=?, notes=?, total_value=?,
                       supplier_po_id=?, surat_jalan_no=?, officer=?, received_time=?, arrival_method=?, courier=?, tracking_no=?,
                       package_count=?, flow='gr', source='po_supplier' WHERE id=?""", (*header, document_id))
            con.execute("DELETE FROM inbound_items WHERE document_id=?", (document_id,))
            con.execute("DELETE FROM inbound_document_pos WHERE document_id=?", (document_id,))
        con.executemany("INSERT INTO inbound_document_pos (document_id, supplier_po_id) VALUES (?, ?)",
                        [(document_id, p) for p in po_ids])
        con.executemany(
            """INSERT INTO inbound_items (document_id, master_item_id, qty_in, cost_price, spo_item_id, spo_package_id,
                   qty_bad, bad_condition, bad_note, check_note, price_reason, po_unit_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(document_id, r["master_item_id"], r["qty_in"], r["cost_price"], r["spo_item_id"], r["spo_package_id"], r["qty_bad"],
              r["bad_condition"], r["bad_note"], r["check_note"], r["price_reason"], r["po_unit_price"]) for r in rows])
        if before is not None:
            fp_old, doc = before
            fp_new = _fingerprints(rows)
            resets = []
            if fp_old["check"] != fp_new["check"] and doc["reviewed_at"]:
                con.execute("UPDATE inbound_documents SET reviewed_by=NULL, reviewed_at=NULL WHERE id=?", (document_id,))
                resets.append("pemeriksaan barang")
            if fp_old["price"] != fp_new["price"] and doc["price_ack_at"]:
                con.execute("UPDATE inbound_documents SET price_ack_by=NULL, price_ack_at=NULL WHERE id=?", (document_id,))
                resets.append("konfirmasi harga")
            if fp_old["qty"] != fp_new["qty"] and doc["approval_state"]:
                con.execute("""UPDATE inbound_documents SET approval_state=NULL, approval_requested_by=NULL, approval_requested_at=NULL,
                               approval_note=NULL, approval_by=NULL, approval_at=NULL, approval_reason=NULL WHERE id=?""", (document_id,))
                resets.append("persetujuan Admin")
            db._inbound_event(con, document_id, "updated", "Draft diperbarui"
                              + (f"; {', '.join(resets)} perlu diulang karena qty/harga berubah" if resets else ""), by)
        return document_id


def _needs(con: sqlite3.Connection, doc, rows: list[dict]) -> dict:
    """What still stands between this draft and posting, per line: excess over the PO and price differences."""
    tol = db.gr_price_tolerance(con)
    over, price = [], []
    for r in rows:
        if not r.get("spo_item_id") or r.get("_spi") is None:
            continue
        spi = r["_spi"]
        left = _left(spi)
        po_price = db._po_net_price(spi)
        if r["qty_in"] > left:
            over.append({"line_id": r["id"], "product_name": spi["product_name"], "po_number": r["_po_number"], "qty_left": left,
                         "qty_in": r["qty_in"], "qty_over": r["qty_in"] - left, "cost_price": r["cost_price"],
                         "approved_qty": r.get("over_approved_qty") or 0, "approved_price": r.get("over_cost_price")})
        if r["cost_price"] != po_price:
            price.append({"line_id": r["id"], "product_name": spi["product_name"], "po_number": r["_po_number"],
                          "po_price": po_price, "cost_price": r["cost_price"], "diff": r["cost_price"] - po_price,
                          "qty": r["qty_in"], "impact": (r["cost_price"] - po_price) * r["qty_in"],
                          "pct": round((r["cost_price"] - po_price) * 100 / po_price, 1) if po_price else None,
                          "over_tolerance": db.gr_price_over_tolerance(po_price, r["cost_price"], tol),
                          "reason": r.get("price_reason")})
    admin = bool(over) or any(p["over_tolerance"] for p in price)
    return {"over": over, "price": price, "tolerance_pct": tol, "needs_admin": admin,
            "price_confirm": bool(price)}


def detail(document_id: int) -> dict:
    with db._conn() as con:
        doc = con.execute(
            """SELECT d.*, s.name AS supplier_name FROM inbound_documents d JOIN suppliers s ON s.id = d.supplier_id
               WHERE d.id=?""", (document_id,)).fetchone()
        if doc is None:
            raise LookupError("Goods Receipt tidak ditemukan.")
        rows = []
        for r in con.execute(
                """SELECT ii.*, mi.name, mi.sku, mi.unit, mi.manual_code, mi.sell_price FROM inbound_items ii
                   LEFT JOIN master_items mi ON mi.id = ii.master_item_id WHERE ii.document_id=? ORDER BY ii.id""", (document_id,)):
            r = dict(r)
            spi = con.execute(
                """SELECT spi.*, sp.po_number AS _po_number, p.po_number AS customer_po_number, p.customer_name
                   FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                   LEFT JOIN preorder_items pi ON pi.id = spi.preorder_item_id LEFT JOIN preorders p ON p.id = pi.preorder_id
                   WHERE spi.id=?""", (r["spo_item_id"],)).fetchone() if r["spo_item_id"] else None
            r["_spi"] = spi
            r["_po_number"] = spi["_po_number"] if spi else None
            r.update({
                "supplier_po_id": spi["supplier_po_id"] if spi else None, "po_number": r["_po_number"],
                "product_name": r["name"] or (spi["product_name"] if spi else r.get("pending_product_name")),
                "qty_po": (spi["quantity"] - spi["qty_cancelled"]) if spi else None,
                "qty_received_po": spi["qty_received"] if spi else None, "qty_left": _left(spi) if spi else None,
                "po_price": db._po_net_price(spi) if spi else None,
                "customer_po_number": spi["customer_po_number"] if spi else None,
                "customer_name": spi["customer_name"] if spi else None,
                "packages": [], "is_bad": (r.get("condition") or "baik") in db.BAD_RECEIVE_CONDITIONS,
            })
            if spi and doc["status"] == "draft":
                r["packages"] = next((l["packages"] for l in _open_po_lines(con, spi["supplier_po_id"])
                                      if l["spo_item_id"] == spi["id"]), [])
            rows.append(r)
        needs = _needs(con, doc, rows) if doc["status"] == "draft" else None
        for r in rows:
            r.pop("_spi", None)
            r.pop("_po_number", None)
        po_ids = db.inbound_document_po_ids(con, document_id)
        pos = [dict(r) for r in con.execute(
            f"SELECT id, po_number, status FROM supplier_pos WHERE id IN ({','.join('?' * len(po_ids))})", po_ids)] if po_ids else []
        events = [dict(r) for r in con.execute(
            "SELECT * FROM inbound_events WHERE document_id=? ORDER BY at, id", (document_id,))]
        prints = con.execute("SELECT COALESCE(SUM(qty), 0) AS n FROM barcode_prints WHERE inbound_document_id=?",
                             (document_id,)).fetchone()["n"]
        out = dict(doc)
        out.update({"lines": rows, "pos": pos, "needs": needs, "events": events, "labels_printed": prints,
                    "arrival_label": ARRIVAL_METHODS.get(doc["arrival_method"] or "", None),
                    "legacy": None if doc["flow"] == "gr" else _legacy_info(con, doc, rows)})
        return out


# --- check, price confirmation, approval ---------------------------------------------------------------------


def _draft_gr(con: sqlite3.Connection, document_id: int):
    doc = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
    if doc is None:
        raise LookupError("Goods Receipt tidak ditemukan.")
    if doc["status"] != "draft":
        raise ValueError("Hanya Goods Receipt draft yang bisa diproses.")
    if doc["flow"] != "gr":
        raise ValueError("Draft sistem lama: periksa lalu posting, atau batalkan.")
    return doc


def check_goods(document_id: int, by: str | None) -> dict:
    """'Periksa Barang': qty baik + qty rusak = qty datang on every line, and every damaged unit has a kind of
    problem and a reason. Marks the draft as checked (editing quantities clears it)."""
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_gr(con, document_id)
        rows = [dict(r) for r in con.execute(
            """SELECT ii.*, mi.name FROM inbound_items ii LEFT JOIN master_items mi ON mi.id = ii.master_item_id
               WHERE ii.document_id=?""", (document_id,))]
        for r in rows:
            if r["qty_bad"] and (not r["bad_condition"] or not (r["bad_note"] or "").strip()):
                raise ValueError(f"“{r['name']}”: pilih jenis masalah dan isi alasan untuk {r['qty_bad']} barang rusak.")
        good = sum(r["qty_in"] - r["qty_bad"] for r in rows)
        bad = sum(r["qty_bad"] for r in rows)
        con.execute("UPDATE inbound_documents SET reviewed_by=?, reviewed_at=? WHERE id=?", (by, db._now(), document_id))
        db._inbound_event(con, document_id, "reviewed", f"Barang diperiksa: {good} layak jual, {bad} karantina", by)
    return detail(document_id)


def ack_price(document_id: int, by: str | None) -> dict:
    """'Saya Pahami': the person confirms each price that differs from its PO, with its reason; logged per line."""
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_gr(con, document_id)
    d = detail(document_id)
    diffs = d["needs"]["price"]
    if not diffs:
        raise ValueError("Tidak ada harga yang berbeda dari PO.")
    missing = [p["product_name"] for p in diffs if not (p["reason"] or "").strip()]
    if missing:
        raise ValueError("Isi alasan perubahan harga untuk: " + ", ".join(missing) + ".")
    with db._conn() as con:
        con.execute("UPDATE inbound_documents SET price_ack_by=?, price_ack_at=? WHERE id=?", (by, db._now(), document_id))
        db._inbound_event(con, document_id, "price_ack", "Selisih harga dikonfirmasi: " + "; ".join(
            f"{p['product_name']} ({p['po_number']}) PO Rp {p['po_price']:,} → GR Rp {p['cost_price']:,} "
            f"({p['diff']:+,}/unit, dampak {p['impact']:+,}), alasan: {p['reason']}".replace(",", ".") for p in diffs), by)
    return detail(document_id)


def request_approval(document_id: int, note: str | None, by: str | None) -> dict:
    d = detail(document_id)
    if d["status"] != "draft" or d["flow"] != "gr":
        raise ValueError("Hanya Goods Receipt draft yang bisa dimintakan persetujuan.")
    if not d["needs"]["needs_admin"]:
        raise ValueError("Tidak ada kelebihan qty atau selisih harga di atas toleransi yang perlu disetujui Admin.")
    if d["approval_state"] == "pending":
        raise ValueError("Permintaan persetujuan sudah dikirim, menunggu Admin.")
    parts = [f"kelebihan {o['qty_over']} pcs {o['product_name']} ({o['po_number']})" for o in d["needs"]["over"]]
    parts += [f"harga {p['product_name']} {p['pct']:+g}% dari PO" for p in d["needs"]["price"] if p["over_tolerance"]]
    with db._conn() as con:
        con.execute("""UPDATE inbound_documents SET approval_state='pending', approval_requested_by=?, approval_requested_at=?,
                       approval_note=?, approval_by=NULL, approval_at=NULL, approval_reason=NULL WHERE id=?""",
                    (by, db._now(), _text(note), document_id))
        db._inbound_event(con, document_id, "approval_request", "Minta persetujuan Admin: " + "; ".join(parts)
                          + (f". Catatan: {note.strip()}" if _text(note) else ""), by)
    return detail(document_id)


def approve(document_id: int, reason: str, over_prices: dict, by: str | None) -> dict:
    """Admin approves the excess (each at a purchase price the Admin states — never assumed free) and/or prices beyond
    the tolerance. Nothing moves until the GR is posted."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan persetujuan wajib diisi (minimal 3 karakter).")
    d = detail(document_id)
    if d["status"] != "draft" or d["flow"] != "gr":
        raise ValueError("Hanya Goods Receipt draft yang bisa disetujui.")
    needs = d["needs"]
    if not needs["needs_admin"]:
        raise ValueError("Tidak ada yang perlu disetujui Admin.")
    notes = []
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        for o in needs["over"]:
            raw = (over_prices or {}).get(str(o["line_id"]), (over_prices or {}).get(o["line_id"]))
            if raw is None or raw == "":
                raise ValueError(f"Isi harga beli untuk kelebihan {o['product_name']} (boleh 0 bila memang gratis, tetapi harus diisi).")
            price = _int(raw, f"Harga kelebihan {o['product_name']}", 0)
            con.execute("UPDATE inbound_items SET over_approved_qty=?, over_cost_price=? WHERE id=?",
                        (o["qty_over"], price, o["line_id"]))
            notes.append(f"kelebihan {o['qty_over']} pcs {o['product_name']} ({o['po_number']}: sesuai PO {o['qty_left']}, "
                         f"datang {o['qty_in']}) @ Rp {price:,}".replace(",", "."))
        notes += [f"harga {p['product_name']} PO Rp {p['po_price']:,} → GR Rp {p['cost_price']:,} ({p['pct']:+g}%)".replace(",", ".")
                  for p in needs["price"] if p["over_tolerance"]]
        con.execute("UPDATE inbound_documents SET approval_state='approved', approval_by=?, approval_at=?, approval_reason=? WHERE id=?",
                    (by, db._now(), reason, document_id))
        db._inbound_event(con, document_id, "approved", "Disetujui Admin: " + "; ".join(notes) + f". Alasan: {reason}", by)
    return detail(document_id)


def reject(document_id: int, note: str, by: str | None) -> dict:
    note = (note or "").strip()
    if len(note) < 3:
        raise ValueError("Tulis alasan penolakan / koreksi yang diminta.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _draft_gr(con, document_id)
        con.execute("UPDATE inbound_documents SET approval_state='rejected', approval_by=?, approval_at=?, approval_reason=? WHERE id=?",
                    (by, db._now(), note, document_id))
        db._inbound_event(con, document_id, "rejected", f"Ditolak Admin, koreksi qty/harga: {note}", by)
    return detail(document_id)


# --- corrections: a new draft from a voided GR -----------------------------------------------------------------


def recreate(document_id: int, by: str | None) -> int:
    with db._conn() as con:
        doc = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if doc is None:
            raise LookupError("Goods Receipt tidak ditemukan.")
        if doc["status"] != "void":
            raise ValueError("Buat ulang hanya dari Goods Receipt yang sudah dibatalkan (void).")
        rows = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=? ORDER BY id", (document_id,))]
        if not rows or any(not r["spo_item_id"] for r in rows):
            raise ValueError("Dokumen ini tidak berasal dari baris PO, jadi tidak bisa dibuat ulang sebagai Goods Receipt.")
        po_ids = [p for p in db.inbound_document_po_ids(con, document_id)
                  if con.execute("SELECT status FROM supplier_pos WHERE id=?", (p,)).fetchone()["status"] in OPEN_PO]
        lines: dict[int, dict] = {}
        for r in rows:
            spo_po = con.execute("SELECT supplier_po_id FROM supplier_po_items WHERE id=?", (r["spo_item_id"],)).fetchone()[0]
            if spo_po not in po_ids:
                continue
            ln = lines.setdefault(r["spo_item_id"], {"spo_item_id": r["spo_item_id"], "qty_in": 0, "qty_bad": 0, "cost_price": None,
                                                     "bad_condition": None, "bad_note": r["bad_note"], "check_note": r["check_note"],
                                                     "price_reason": r["price_reason"], "spo_package_id": r["spo_package_id"]})
            ln["qty_in"] += r["qty_in"]
            if (r["condition"] or "baik") in db.BAD_RECEIVE_CONDITIONS:
                ln["qty_bad"] += r["qty_in"]
                ln["bad_condition"] = ln["bad_condition"] or r["condition"]
            if not r["is_over"] and ln["cost_price"] is None:
                ln["cost_price"] = r["cost_price"]
        for ln in lines.values():
            if ln["cost_price"] is None:
                ln["cost_price"] = next(r["cost_price"] for r in rows if r["spo_item_id"] == ln["spo_item_id"])
    if not lines:
        raise ValueError("Semua PO dari dokumen ini sudah selesai atau dibatalkan, tidak ada yang bisa dibuat ulang.")
    received = doc["received_date"]
    if (db.shop_today() - date.fromisoformat(received)).days > 30:
        received = db.shop_today().isoformat()
    new_id = save_draft(None, {
        "supplier_id": doc["supplier_id"], "supplier_po_ids": po_ids, "received_date": received,
        "received_time": doc["received_time"] or "00:00", "officer": doc["officer"] or by, "arrival_method": doc["arrival_method"],
        "surat_jalan_no": doc["surat_jalan_no"], "courier": doc["courier"], "tracking_no": doc["tracking_no"],
        "package_count": doc["package_count"], "notes": doc["notes"], "supplier_invoice_no": doc["supplier_invoice_no"],
        "items": list(lines.values()),
    }, by)
    with db._conn() as con:
        new_no = con.execute("SELECT doc_number FROM inbound_documents WHERE id=?", (new_id,)).fetchone()[0]
        con.execute("UPDATE inbound_documents SET recreated_from=? WHERE id=?", (document_id, new_id))
        db._inbound_event(con, new_id, "recreated", f"Dibuat ulang dari {doc['doc_number']} (void) untuk koreksi", by)
        db._inbound_event(con, document_id, "recreated", f"Koreksi dibuat ulang sebagai draft {new_no}", by)
    return new_id


# --- drafts from before Goods Receipt -------------------------------------------------------------------------


def _legacy_info(con: sqlite3.Connection, doc, rows: list[dict]) -> dict:
    """What posting or cancelling an older draft would do, and anything that looks like the same goods recorded twice."""
    findings: list[dict] = []
    receipts = [dict(r) for r in con.execute(
        "SELECT r.*, sp.po_number FROM spo_receipts r JOIN supplier_pos sp ON sp.id = r.supplier_po_id WHERE r.inbound_document_id=?",
        (doc["id"],))]
    if doc["source"] == "retur_pengganti":
        kind = "retur"
    elif receipts:
        kind = "po_page"
    elif rows and all(r.get("spo_item_id") for r in rows):
        kind = "po_lines"
    else:
        kind = "manual"
    qty = sum(r["qty_in"] for r in rows)
    if kind == "po_page":
        for rc in receipts:
            n = con.execute("SELECT COALESCE(SUM(quantity), 0) FROM spo_receipt_lines WHERE receipt_id=?", (rc["id"],)).fetchone()[0]
            findings.append({"level": "info", "text": f"Qty sudah tercatat di PO {rc['po_number']} lewat penerimaan {rc['gr_number'] or '-'} "
                             f"({n} pcs). Posting hanya menambah stok {qty} pcs, tidak menambah qty PO lagi."})
            findings.append({"level": "info", "text": f"Membatalkan draft ini juga membatalkan penerimaan {rc['gr_number'] or '-'} "
                             f"di PO {rc['po_number']}, sehingga sisa PO kembali {n} pcs."})
    elif kind == "manual":
        findings.append({"level": "info", "text": "Draft manual tanpa PO dari sistem lama. Penerimaan manual sudah tidak tersedia; "
                         "draft ini masih boleh diposting setelah diperiksa, atau dibatalkan."})
    elif kind == "po_lines":
        findings.append({"level": "info", "text": "Draft dari form lama yang sudah berisi baris PO. Bisa dibuka dan dilanjutkan di Goods Receipt."})
    # The same products posted by another document since this draft was made look like a double receipt.
    mids = sorted({r["master_item_id"] for r in rows if r.get("master_item_id")})
    if mids:
        dupes = con.execute(
            f"""SELECT DISTINCT d.doc_number, d.received_date, mi.name FROM inbound_items ii
                JOIN inbound_documents d ON d.id = ii.document_id JOIN master_items mi ON mi.id = ii.master_item_id
                WHERE d.status='posted' AND d.supplier_id=? AND d.id<>? AND ii.master_item_id IN ({','.join('?' * len(mids))})
                  AND d.received_date >= date(?, '-14 day') ORDER BY d.received_date""",
            (doc["supplier_id"], doc["id"], *mids, doc["received_date"])).fetchall()
        for x in dupes:
            findings.append({"level": "warn", "text": f"{x['name']} dari supplier yang sama sudah diposting di {x['doc_number']} "
                             f"({x['received_date']}). Pastikan ini bukan barang yang sama agar stok tidak dobel."})
    return {"kind": kind, "findings": findings,
            "has_warning": any(f["level"] == "warn" for f in findings)}


# --- list ----------------------------------------------------------------------------------------------------


def list_receipts(status: str | None = None, supplier_id: int | None = None, q: str | None = None,
                  date_from: str | None = None, date_to: str | None = None) -> dict:
    where, params = ["1=1"], []
    if status == "menunggu_persetujuan":
        where.append("d.status='draft' AND d.approval_state='pending'")
    elif status:
        where.append("d.status=?")
        params.append(status)
    if supplier_id is not None:
        where.append("d.supplier_id=?")
        params.append(supplier_id)
    if date_from:
        where.append("d.received_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("d.received_date <= ?")
        params.append(date_to)
    if q:
        where.append("""(d.doc_number LIKE ? OR s.name LIKE ? OR IFNULL(d.surat_jalan_no,'') LIKE ? OR IFNULL(d.tracking_no,'') LIKE ?
                        OR d.id IN (SELECT p.document_id FROM inbound_document_pos p JOIN supplier_pos sp ON sp.id = p.supplier_po_id
                                    WHERE sp.po_number LIKE ?)
                        OR d.id IN (SELECT ii.document_id FROM inbound_items ii JOIN master_items m ON m.id = ii.master_item_id
                                    WHERE m.name LIKE ? OR m.sku LIKE ? OR IFNULL(m.manual_code,'') LIKE ?))""")
        params += [f"%{q}%"] * 8
    with db._conn() as con:
        rows = [dict(r) for r in con.execute(
            f"""SELECT d.*, s.name AS supplier_name,
                       (SELECT COUNT(DISTINCT ii.master_item_id) FROM inbound_items ii WHERE ii.document_id = d.id) AS item_count,
                       (SELECT COALESCE(SUM(ii.qty_in), 0) FROM inbound_items ii WHERE ii.document_id = d.id) AS qty_total,
                       (SELECT COALESCE(SUM(ii.qty_in * ii.cost_price), 0) FROM inbound_items ii WHERE ii.document_id = d.id) AS value_now,
                       (SELECT COALESCE(SUM(b.qty), 0) FROM barcode_prints b WHERE b.inbound_document_id = d.id) AS labels_printed
                FROM inbound_documents d JOIN suppliers s ON s.id = d.supplier_id
                WHERE {' AND '.join(where)} ORDER BY d.created_at DESC, d.id DESC""", params)]
        for r in rows:
            r["po_numbers"] = [x[0] for x in con.execute(
                """SELECT DISTINCT sp.po_number FROM supplier_pos sp WHERE sp.id IN (
                       SELECT supplier_po_id FROM inbound_document_pos WHERE document_id=?
                       UNION SELECT spi.supplier_po_id FROM inbound_items ii JOIN supplier_po_items spi ON spi.id = ii.spo_item_id
                             WHERE ii.document_id=?) ORDER BY sp.po_number""", (r["id"], r["id"]))]
            if r["status"] == "draft":
                r["total_value"] = r["value_now"]
        month = db.shop_today().strftime("%Y-%m")
        k = con.execute(
            """SELECT SUM(substr(received_date,1,7)=? AND status<>'void') AS month_total,
                      SUM(substr(received_date,1,7)=? AND status='posted') AS month_posted,
                      SUM(substr(received_date,1,7)=? AND status='draft') AS month_draft,
                      SUM(status='draft') AS drafts, SUM(status='draft' AND approval_state='pending') AS pending,
                      COALESCE(SUM(CASE WHEN substr(received_date,1,7)=? AND status='posted' THEN total_value END), 0) AS month_value
               FROM inbound_documents""", (month, month, month, month)).fetchone()
        kpis = {key: k[key] or 0 for key in k.keys()}
    return {"rows": rows, "kpis": kpis}


# --- Kode Manual ---------------------------------------------------------------------------------------------


def normalize_code(code: str | None) -> str:
    return re.sub(r"\s+", "", (code or "").strip().upper())


def _code_owner(con: sqlite3.Connection, code: str, item_id: int | None) -> dict | None:
    """Who else already answers to this code at the cashier: another SKU's Kode Manual, alias, SKU or barcode."""
    checks = (
        ("kode manual", "SELECT id, name, sku FROM master_items WHERE UPPER(manual_code)=? AND id<>?"),
        ("kode lama (alias)", """SELECT mi.id, mi.name, mi.sku FROM item_code_aliases a JOIN master_items mi ON mi.id = a.master_item_id
                                 WHERE UPPER(a.code)=? AND mi.id<>?"""),
        ("SKU", "SELECT id, name, sku FROM master_items WHERE UPPER(sku)=? AND id<>?"),
        ("barcode", "SELECT id, name, sku FROM master_items WHERE UPPER(barcode)=? AND id<>?"),
    )
    for kind, sql in checks:
        row = con.execute(sql, (code, item_id or 0)).fetchone()
        if row is not None:
            return {"kind": kind, "item_id": row["id"], "name": row["name"], "sku": row["sku"]}
    return None


def _suggest_code(con: sqlite3.Connection, item) -> str | None:
    sku = normalize_code(item["sku"])
    if CODE_RE.match(sku or "") and _code_owner(con, sku, item["id"]) is None:
        return sku
    stem = re.sub(r"[^A-Z0-9]", "", (item["name"] or "").upper())[:3] or "KD"
    for n in range(1, 1000):
        cand = f"{stem}-{n:03d}"
        if _code_owner(con, cand, item["id"]) is None:
            return cand
    return None


def check_code(master_item_id: int, code: str) -> dict:
    code_n = normalize_code(code)
    with db._conn() as con:
        item = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if item is None:
            raise LookupError("Barang tidak ditemukan.")
        if not CODE_RE.match(code_n):
            return {"ok": False, "code": code_n, "error": "Kode 2–30 karakter: huruf, angka, titik, minus, atau garis bawah.",
                    "conflict": None, "suggestion": _suggest_code(con, item)}
        owner = _code_owner(con, code_n, master_item_id)
        return {"ok": owner is None, "code": code_n, "error": None, "conflict": owner,
                "suggestion": _suggest_code(con, item) if owner else None}


def set_manual_code(master_item_id: int, code: str | None, reason: str | None, by: str | None) -> dict:
    """Admin only (enforced by the route). One Kode Manual per SKU. Replacing a code that has been printed keeps
    the old one as an alias, so labels already on the shelf still scan, and it can never go to another SKU."""
    code_n = normalize_code(code) or None
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        item = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if item is None:
            raise LookupError("Barang tidak ditemukan.")
        old = item["manual_code"]
        if code_n == old:
            return dict(item)
        printed = old and con.execute("SELECT 1 FROM barcode_prints WHERE code=? LIMIT 1", (old,)).fetchone()
        if code_n is None:
            if printed:
                raise ValueError("Kode yang sudah pernah dicetak tidak bisa dihapus, hanya diganti (kode lama tetap terbaca sebagai alias).")
        else:
            if not CODE_RE.match(code_n):
                raise ValueError("Kode 2–30 karakter: huruf, angka, titik, minus, atau garis bawah.")
            owner = _code_owner(con, code_n, master_item_id)
            if owner is not None:
                raise ValueError(f"Kode {code_n} sudah dipakai sebagai {owner['kind']} {owner['name']} ({owner['sku']}).")
        if old and len((reason or "").strip()) < 3:
            raise ValueError("Alasan penggantian kode wajib diisi (minimal 3 karakter).")
        if printed:
            con.execute("INSERT OR IGNORE INTO item_code_aliases (code, master_item_id, created_at, created_by) VALUES (?, ?, ?, ?)",
                        (old, master_item_id, db._now(), by))
        if code_n:
            con.execute("DELETE FROM item_code_aliases WHERE UPPER(code)=? AND master_item_id=?", (code_n, master_item_id))
        con.execute("UPDATE master_items SET manual_code=?, updated_at=?, updated_by=? WHERE id=?",
                    (code_n, db._now(), by, master_item_id))
        con.execute("INSERT INTO item_code_history (master_item_id, old_code, new_code, reason, by_user, at) VALUES (?, ?, ?, ?, ?, ?)",
                    (master_item_id, old, code_n, _text(reason) or ("Kode pertama" if not old else None), by, db._now()))
        return dict(con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone())


def code_info(master_item_id: int) -> dict:
    with db._conn() as con:
        item = con.execute("SELECT id, name, sku, manual_code, sell_price, barcode FROM master_items WHERE id=?",
                           (master_item_id,)).fetchone()
        if item is None:
            raise LookupError("Barang tidak ditemukan.")
        return {**dict(item),
                "aliases": [dict(r) for r in con.execute(
                    "SELECT code, created_at, created_by FROM item_code_aliases WHERE master_item_id=? ORDER BY created_at", (master_item_id,))],
                "history": [dict(r) for r in con.execute(
                    "SELECT * FROM item_code_history WHERE master_item_id=? ORDER BY at DESC, id DESC", (master_item_id,))],
                "prints": [dict(r) for r in con.execute(
                    """SELECT b.*, d.doc_number FROM barcode_prints b LEFT JOIN inbound_documents d ON d.id = b.inbound_document_id
                       WHERE b.master_item_id=? ORDER BY b.at DESC, b.id DESC LIMIT 50""", (master_item_id,))]}


# --- barcode labels ------------------------------------------------------------------------------------------


def barcode_items(document_id: int) -> dict:
    """'Pilih Produk untuk Barcode': each product a posted GR put on the shelf, once, with its Kode Manual. Goods
    bought for a customer's Sales Order are handed over as they are, so they are left out of the suggested count."""
    with db._conn() as con:
        doc = con.execute("SELECT d.*, s.name AS supplier_name FROM inbound_documents d JOIN suppliers s ON s.id = d.supplier_id "
                          "WHERE d.id=?", (document_id,)).fetchone()
        if doc is None:
            raise LookupError("Goods Receipt tidak ditemukan.")
        if doc["status"] != "posted":
            raise ValueError("Barcode dicetak setelah Goods Receipt diposting.")
        items: dict[int, dict] = {}
        for r in con.execute(
                """SELECT ii.*, mi.name, mi.sku, mi.unit, mi.manual_code, mi.sell_price, sp.po_number, spi.preorder_item_id
                   FROM inbound_items ii JOIN master_items mi ON mi.id = ii.master_item_id
                   LEFT JOIN supplier_po_items spi ON spi.id = ii.spo_item_id LEFT JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                   WHERE ii.document_id=? ORDER BY ii.id""", (document_id,)):
            if (r["condition"] or "baik") in db.BAD_RECEIVE_CONDITIONS:
                continue
            it = items.setdefault(r["master_item_id"], {
                "master_item_id": r["master_item_id"], "name": r["name"], "sku": r["sku"], "unit": r["unit"],
                "manual_code": r["manual_code"], "sell_price": r["sell_price"], "po_numbers": [], "qty_good": 0, "qty_so": 0})
            it["qty_good"] += r["qty_in"]
            if r["preorder_item_id"] and not r["is_over"]:
                it["qty_so"] += r["qty_in"]
            if r["po_number"] and r["po_number"] not in it["po_numbers"]:
                it["po_numbers"].append(r["po_number"])
        for it in items.values():
            it["printed"] = con.execute("SELECT COALESCE(SUM(qty), 0) FROM barcode_prints WHERE inbound_document_id=? AND master_item_id=?",
                                        (document_id, it["master_item_id"])).fetchone()[0]
            it["suggested"] = max(it["qty_good"] - it["qty_so"], 0)
        return {"document_id": doc["id"], "doc_number": doc["doc_number"], "supplier_name": doc["supplier_name"],
                "received_date": doc["received_date"], "items": list(items.values())}


def record_prints(lines: list[dict], kind: str, document_id: int | None, by: str | None) -> list[dict]:
    """Logs a print (or reprint) per SKU and returns what to put on the labels. Printing never moves stock and
    never creates a SKU or a code: a SKU without a Kode Manual is refused until an Admin gives it one."""
    if kind not in ("cetak", "cetak_ulang"):
        raise ValueError("Jenis cetak tidak dikenal.")
    if not lines:
        raise ValueError("Pilih minimal satu produk untuk dicetak.")
    out = []
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if document_id is not None:
            d = con.execute("SELECT status FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
            if d is None or d["status"] != "posted":
                raise ValueError("Barcode dicetak dari Goods Receipt yang sudah diposting.")
        missing = []
        for ln in lines:
            qty = _int(ln.get("qty"), "Jumlah label", 1)
            if qty > 1000:
                raise ValueError("Maksimal 1000 label per produk sekali cetak.")
            item = con.execute("SELECT id, name, sku, manual_code, sell_price FROM master_items WHERE id=?",
                               (ln.get("master_item_id"),)).fetchone()
            if item is None:
                raise ValueError("Barang tidak ditemukan.")
            if not item["manual_code"]:
                missing.append(item["name"])
                continue
            out.append({"master_item_id": item["id"], "name": item["name"], "sku": item["sku"], "code": item["manual_code"],
                        "sell_price": item["sell_price"], "qty": qty})
        if missing:
            raise ValueError("Belum punya Kode Manual: " + ", ".join(missing) + ". Minta Admin mengisinya dulu.")
        now = db._now()
        con.executemany(
            "INSERT INTO barcode_prints (master_item_id, code, qty, kind, inbound_document_id, by_user, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(o["master_item_id"], o["code"], o["qty"], kind, document_id, by, now) for o in out])
        if document_id is not None:
            db._inbound_event(con, document_id, "printed", ("Cetak ulang " if kind == "cetak_ulang" else "Cetak ")
                              + ", ".join(f"{o['qty']} label {o['code']}" for o in out), by)
    return out


def list_prints(master_item_id: int | None = None, document_id: int | None = None, limit: int = 50) -> list[dict]:
    where, params = ["1=1"], []
    if master_item_id is not None:
        where.append("b.master_item_id=?")
        params.append(master_item_id)
    if document_id is not None:
        where.append("b.inbound_document_id=?")
        params.append(document_id)
    with db._conn() as con:
        return [dict(r) for r in con.execute(
            f"""SELECT b.*, mi.name, mi.sku, d.doc_number FROM barcode_prints b JOIN master_items mi ON mi.id = b.master_item_id
                LEFT JOIN inbound_documents d ON d.id = b.inbound_document_id
                WHERE {' AND '.join(where)} ORDER BY b.at DESC, b.id DESC LIMIT ?""", (*params, limit))]


def find_item_for_reprint(code: str) -> dict:
    """'Cetak Ulang': find a product by Kode Manual, old code, SKU or barcode."""
    with db._conn() as con:
        c = normalize_code(code)
        row = con.execute(
            """SELECT id FROM master_items WHERE UPPER(manual_code)=? OR UPPER(sku)=? OR barcode=?
               UNION SELECT master_item_id FROM item_code_aliases WHERE UPPER(code)=? LIMIT 1""", (c, c, c, c)).fetchone()
        if row is None:
            raise LookupError(f"Produk dengan kode {c} tidak ditemukan.")
        info = code_info(row[0])
        info["last_gr"] = (lambda r: dict(r) if r else None)(con.execute(
            """SELECT d.id, d.doc_number, d.received_date FROM inbound_items ii JOIN inbound_documents d ON d.id = ii.document_id
               WHERE ii.master_item_id=? AND d.status='posted' ORDER BY d.received_date DESC, d.id DESC LIMIT 1""", (row[0],)).fetchone())
        return info


# --- integration view ------------------------------------------------------------------------------------------


def integration(document_id: int) -> dict:
    """'Integrasi GR, PO, Stok & Barcode': how this receipt ties the documents together. Read-only."""
    d = detail(document_id)
    with db._conn() as con:
        po_rows = []
        for po in d["pos"]:
            for i in con.execute("SELECT * FROM supplier_po_items WHERE supplier_po_id=? ORDER BY position, id", (po["id"],)):
                mine = [l for l in d["lines"] if l["spo_item_id"] == i["id"]]
                if not mine:
                    continue
                po_rows.append({"po_number": po["po_number"], "product_name": i["product_name"],
                                "qty_po": i["quantity"] - i["qty_cancelled"],
                                "qty_gr": sum(l["qty_in"] for l in mine if not l["is_over"]),
                                "qty_over": sum(l["qty_in"] for l in mine if l["is_over"]),
                                "qty_left": _left(i)})
        stock_in = con.execute("SELECT COALESCE(SUM(qty), 0) FROM stock_ledger WHERE ref_type='inbound_document' AND ref_id=? AND type='IN'",
                               (document_id,)).fetchone()[0]
        fakturs = []
        for po in d["pos"]:
            f = con.execute("SELECT invoice_number, status, verify_status FROM purchase_invoices WHERE supplier_po_id=? AND status<>'void' "
                            "ORDER BY id DESC LIMIT 1", (po["id"],)).fetchone()
            fakturs.append({"po_number": po["po_number"], **(dict(f) if f else {"invoice_number": None, "status": None, "verify_status": None})})
        codes = {l["master_item_id"] for l in d["lines"] if l.get("manual_code") and not l["is_bad"]}
    good = sum(l["qty_in"] for l in d["lines"] if not l["is_bad"])
    bad = sum(l["qty_in"] for l in d["lines"] if l["is_bad"])
    return {"doc_number": d["doc_number"], "status": d["status"], "supplier_name": d["supplier_name"], "pos": d["pos"],
            "po_rows": po_rows, "qty_po_total": sum(r["qty_po"] for r in po_rows),
            "qty_left_total": sum(r["qty_left"] for r in po_rows), "qty_gr": good + bad, "qty_good": good, "qty_bad": bad,
            "stock_in": stock_in, "codes": len(codes), "labels_printed": d["labels_printed"], "fakturs": fakturs}
