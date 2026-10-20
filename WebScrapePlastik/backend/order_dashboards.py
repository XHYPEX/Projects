"""Dashboard Sales Order and Dashboard Purchase Order: read-only monitoring views.

Nothing here changes a document. Each row is read off the modules that own it (preorders / preorder_items /
receipts for a Sales Order; supplier_pos / supplier_po_items / spo_packages / inbound_documents for a Purchase
Order) and gets one stage, a few warning flags and the follow-up actions the page lists under "Perlu Tindakan".
The headline numbers are added up in the browser from these rows, so a tile and the table can never disagree.

Sales Order:
- Active = published, not cancelled, and its invoice(s) not yet closed (Lunas + Pengerjaan Selesai).
- One stage per order: menunggu_po → dalam_pengiriman → tiba_sebagian → siap → lain (all installed, invoice
  still open) → selesai (invoice closed). A line is "arrived" at sampai_di_toko / terpasang, as everywhere else.
- Late = a line that has not arrived whose ETA (package → PO line → PO → SO line) is before today.

Purchase Order:
- Active = issued (dipesan / sebagian_diterima). Drafts and cancelled POs are listed but never counted active.
- Value is the goods value after line discounts (before PPN and ongkir), split into received / on the road /
  not received yet, so the three parts add up to the total shown.
"""
from __future__ import annotations

import sqlite3

from backend import database as db

ARRIVED = db.PO_ARRIVED_STATUSES
CANCELLED = db.PO_LINE_CANCELLED

SO_STAGE_LABELS = {
    "menunggu_po": "Menunggu PO", "dalam_pengiriman": "Dalam Pengiriman", "tiba_sebagian": "Diterima Sebagian",
    "siap": "Barang Siap", "lain": "Status Lain", "selesai": "Selesai",
}
SO_SOURCE_LABELS = {"supplier": "Supplier", "stok": "Stok", "jasa": "Jasa", None: "Otomatis"}
PO_ORDER_LABELS = {
    "draft": "Draft", "menunggu_konfirmasi": "Menunggu Konfirmasi", "dikonfirmasi": "Dikonfirmasi",
    "diproses": "Diproses", "dalam_pengiriman": "Dalam Pengiriman", "diterima_sebagian": "Diterima Sebagian",
    "diterima_lengkap": "Diterima Lengkap", "dibatalkan": "Dibatalkan",
}
PO_SHIP_LABELS = {"belum_dikirim": "Menunggu Kirim", "diproses": "Diproses", "dalam_pengiriman": "Dalam Pengiriman",
                  "tiba": "Sudah Tiba"}
PO_RECEIPT_LABELS = {"belum": "Belum Diterima", "sebagian": "Diterima Sebagian", "lengkap": "Diterima Lengkap"}
PKG_STATUS_LABELS = {"menunggu_konfirmasi": "Menunggu Konfirmasi", "dikonfirmasi": "Dikonfirmasi", "diproses": "Diproses",
                     "dalam_pengiriman": "Dalam Pengiriman", "tiba_di_hub": "Tiba di Hub", "diterima": "Diterima",
                     "diterima_sebagian": "Diterima Sebagian", "dibatalkan": "Dibatalkan"}


def _conn() -> sqlite3.Connection:
    return db._conn()


def _qty_text(qty: int, unit: str | None) -> str:
    return f"{qty} {(unit or 'PCS').upper()}"


def _ymd(value: str | None) -> str | None:
    return value[:10] if value else None


# ── Sales Order ──────────────────────────────────────────────────────────────

def _so_line_links(con: sqlite3.Connection) -> dict[int, list[dict]]:
    """preorder_item_id → its live Purchase Order lines, each with the best ETA known for what is still open."""
    pkg_eta: dict[int, str] = {}
    for r in con.execute(
            """SELECT pl.item_id, MAX(p.eta_date) AS eta FROM spo_package_lines pl JOIN spo_packages p ON p.id = pl.package_id
               WHERE p.cancelled = 0 AND p.eta_date IS NOT NULL GROUP BY pl.item_id"""):
        pkg_eta[r["item_id"]] = r["eta"]
    out: dict[int, list[dict]] = {}
    for r in con.execute(
            """SELECT spi.id AS spo_item_id, spi.preorder_item_id, spi.quantity - spi.qty_cancelled AS qty, spi.qty_received,
                      spi.eta_date, sp.id AS po_id, sp.po_number, sp.status AS po_status, sp.est_arrival_date
               FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
               WHERE spi.preorder_item_id IS NOT NULL AND sp.status <> 'batal' AND spi.quantity - spi.qty_cancelled > 0
               ORDER BY sp.created_at"""):
        d = dict(r)
        d["eta"] = pkg_eta.get(d["spo_item_id"]) or d["eta_date"] or d["est_arrival_date"]
        out.setdefault(d["preorder_item_id"], []).append(d)
    return out


def _so_closed(con: sqlite3.Connection) -> dict[str, str | None]:
    """preorder_id → closed_at for orders whose every live invoice is Lunas + Pengerjaan Selesai."""
    out = {}
    for r in con.execute(
            """SELECT preorder_id, COUNT(*) AS n, SUM(status='done' AND work_status='selesai') AS closed, MAX(closed_at) AS at
               FROM receipts WHERE preorder_id IS NOT NULL AND status <> 'void' GROUP BY preorder_id"""):
        if r["n"] and r["closed"] == r["n"]:
            out[r["preorder_id"]] = r["at"]
    return out


def _so_work_status(con: sqlite3.Connection) -> dict[str, str]:
    return {r["preorder_id"]: r["work_status"] for r in con.execute(
        "SELECT preorder_id, work_status FROM receipts WHERE preorder_id IS NOT NULL AND status <> 'void' AND invoice_seq = 1")}


def _arrived(item: dict) -> bool:
    """At the shop, or (a stock line) fully held from the shelf with nothing still to come."""
    return item["status"] in ARRIVED or item.get("_allocated", False)


def _so_stage(live: list[dict], closed: bool) -> str:
    if closed:
        return "selesai"
    if not live:
        return "lain"
    if all(i["status"] == "terpasang" for i in live):
        return "lain"
    arrived = sum(1 for i in live if _arrived(i))
    if arrived == len(live):
        return "siap"
    if arrived:
        return "tiba_sebagian"
    if any(i["status"] == "pesanan_dibuat" for i in live):
        return "menunggu_po"
    return "dalam_pengiriman"


def _so_dashboard_row(row: dict, items: list[dict], links: dict[int, list[dict]], closed: bool, closed_at: str | None,
                      work_status: str | None, today: str) -> dict:
    live = [i for i in items if i["status"] != CANCELLED]
    stage = _so_stage(live, closed)
    lines = []
    for i in live:
        ls = links.get(i["id"], [])
        here = _arrived(i)
        arrived_qty = i["quantity"] if here else min(i.get("_posted", 0), i["quantity"])
        etas = [l["eta"] for l in ls if l["eta"] and l["qty_received"] < l["qty"]] or ([i["eta_date"]] if i.get("eta_date") else [])
        eta = max(etas) if etas else None
        lines.append({"id": i["id"], "name": i["product_name"], "qty": i["quantity"], "unit": i.get("unit"),
                      "arrived_qty": arrived_qty, "status": i["status"], "arrived": here, "source": i.get("source"),
                      "supplier_name": i.get("supplier_name"), "eta": eta,
                      "late": bool(eta and eta < today and not here),
                      "missing_supplier": i.get("source") == "supplier" and i.get("supplier_id") is None and not here,
                      "draft_pos": [{"id": l["po_id"], "po_number": l["po_number"]} for l in ls if l["po_status"] == "draft"]
                                   if not here else []})
    pos: list[dict] = []
    for i in live:
        for l in links.get(i["id"], []):
            if not any(p["id"] == l["po_id"] for p in pos):
                pos.append({"id": l["po_id"], "po_number": l["po_number"], "status": l["po_status"]})
    suppliers = sorted({ln["supplier_name"] for ln in lines if ln["supplier_name"]})
    sources = {ln["source"] for ln in lines}
    all_stock = bool(lines) and sources == {"stok"}
    late_lines = [ln for ln in lines if ln["late"]]
    pending_etas = [ln["eta"] for ln in lines if not ln["arrived"] and ln["eta"]]

    if len(lines) == 1:
        ln = lines[0]
        summary = f"{ln['name']} {_qty_text(ln['qty'], ln['unit'])}"
        progress = {"done": ln["arrived_qty"], "total": ln["qty"], "unit": "alokasi" if all_stock else (ln["unit"] or "PCS").upper()}
    else:
        summary = f"{len(lines)} jenis" + (f" / {len(suppliers)} supplier" if suppliers else "")
        progress = {"done": sum(1 for ln in lines if ln["arrived"]), "total": len(lines),
                    "unit": "alokasi" if all_stock else "jenis"}

    if stage in ("siap", "lain", "selesai"):
        eta_text, eta = ("Siap stok" if all_stock else "Sudah tiba"), None
    else:
        eta = max(pending_etas) if pending_etas else None
        eta_text = None if eta else "Belum ada"

    missing = [ln for ln in lines if ln["missing_supplier"]]
    if stage == "menunggu_po":
        status_key, status_label = ("menunggu_supplier", "Menunggu supplier") if missing else ("menunggu_po", "Menunggu PO")
    elif stage == "dalam_pengiriman":
        status_key, status_label = "dalam_pengiriman", "Dalam pengiriman"
    elif stage == "tiba_sebagian":
        status_key, status_label = "tiba_sebagian", "Tiba sebagian"
    elif stage == "siap":
        status_key, status_label = ("dialokasikan", "Dialokasikan") if all_stock else ("siap", "Barang siap")
    elif stage == "selesai":
        status_key, status_label = "selesai", "Selesai"
    else:
        status_key, status_label = ("menunggu_lunas", "Menunggu pelunasan") if row["remaining"] > 0 else ("menunggu_selesai", "Menunggu selesai")

    actions: list[dict] = []
    if stage != "selesai":
        if missing:
            actions.append({"kind": "lengkapi_supplier", "button": "Lengkapi Supplier",
                            "text": f"{', '.join(ln['name'] for ln in missing)} belum ditentukan supplier — pilih supplier sebelum membuat PO"})
        if late_lines:
            ln = late_lines[0]
            actions.append({"kind": "terlambat", "button": "Lihat Pengadaan", "eta": ln["eta"],
                            "text": f"{ln['name']} melewati ETA — {ln['arrived_qty']}/{_qty_text(ln['qty'], ln['unit'])} diterima"})
        drafts = [(ln, p) for ln in lines for p in ln["draft_pos"]]
        if drafts:
            ln, p = drafts[0]
            actions.append({"kind": "po_draft", "button": "Lihat PO", "po_id": p["id"],
                            "text": f"{ln['name']} pada {p['po_number']} belum dikirim ke supplier — perlu follow-up"})
        if any(i.get("price_state") == "pending" for i in live):
            actions.append({"kind": "harga_khusus", "button": "Lihat SO", "text": "Harga khusus menunggu persetujuan admin"})
        if row.get("pending_amount"):
            actions.append({"kind": "dana_pending", "button": "Lihat SO", "amount": row["pending_amount"],
                            "text": "Pembayaran transfer/QRIS menunggu konfirmasi dana masuk"})
        if stage == "siap" and work_status not in ("pengerjaan", "selesai"):
            what = summary if len(lines) == 1 else f"{len(lines)} jenis barang"
            actions.append({"kind": "hubungi_customer", "button": "Hubungi Customer",
                            "text": f"{what} siap — hubungi customer untuk pemasangan / pengambilan"})

    due = row["invoice_total"] if row["invoice_total"] is not None else row["total"]
    return {
        "id": row["id"], "so_number": row["po_number"], "so_date": row["po_date"], "customer_name": row["customer_name"],
        "customer_phone": row["customer_phone"], "plate": row["plate_full"], "receipt_no": row["receipt_no"],
        "summary": summary, "item_names": [ln["name"] for ln in lines],
        "source_label": "Campuran" if len(sources) > 1 else SO_SOURCE_LABELS.get(next(iter(sources), None), "—") if lines else "—",
        "pos": pos, "missing_supplier": bool(missing), "suppliers": suppliers,
        "progress": progress, "eta": eta, "eta_text": eta_text, "late": bool(late_lines),
        "stage": stage, "stage_label": SO_STAGE_LABELS[stage], "status_key": status_key, "status_label": status_label,
        "line_statuses": sorted({ln["status"] for ln in lines}),
        "total": due, "paid": row["deposit"], "remaining": row["remaining"], "payment_status": row["payment_status"],
        "actions": actions, "closed_at": _ymd(closed_at),
    }


def so_dashboard() -> dict:
    today = db.shop_today().isoformat()
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM preorders WHERE is_draft=0 ORDER BY po_date DESC, created_at DESC")]
        items_by: dict[str, list[dict]] = {}
        for it in con.execute(f"{db._PREORDER_ITEMS_SQL} ORDER BY pi.position ASC, pi.id ASC"):
            items_by.setdefault(it["preorder_id"], []).append(dict(it))
        paid = db._preorder_paid_by_order(con)
        invoices = db.po_invoices(con, [r["receipt_id"] for r in rows])
        draft_pend = db._preorder_pending_by_order(con)
        links = _so_line_links(con)
        closed = _so_closed(con)
        work = _so_work_status(con)
        # A stock line the shelf already holds counts as ready even if its status has not caught up yet (stock
        # that came back through a path that does not re-check, e.g. a voided nota); same reading as the SO detail.
        held = db._reservation_lines(con)
        allocated = {ln["id"] for ln in held if ln["status"] == "pesanan_dibuat" and ln["source"] != "supplier"
                     and ln["coming"] == 0 and ln["reserved"] >= ln["quantity"]}
        # K-10: goods count as arrived once their Barang Masuk is posted, not when they are only received.
        posted = {ln["id"]: ln["posted"] for ln in held}
    out = []
    for r in rows:
        items = [{**i, "_allocated": i["id"] in allocated, "_posted": posted.get(i["id"], 0)} for i in items_by.get(r["id"], [])]
        base = db._preorder_row({**r, "draft_pending": draft_pend.get(r["id"], 0)}, items, paid.get(r["id"], 0),
                                invoices.get(r["receipt_id"]))
        if base["procurement_status"] == "dibatalkan":
            continue
        out.append(_so_dashboard_row(base, items, links, r["id"] in closed, closed.get(r["id"]), work.get(r["id"]), today))
    return {"today": today, "rows": out, "stage_labels": SO_STAGE_LABELS,
            "line_status_labels": db.PREORDER_STATUS_LABELS}


# ── Purchase Order ───────────────────────────────────────────────────────────

def _net_price(item: dict) -> float:
    return item["est_unit_price"] * (1 - (item.get("discount_pct") or 0) / 100)


def po_dashboard() -> dict:
    today = db.shop_today().isoformat()
    month = today[:7]
    pos = db.get_all_supplier_pos()
    with _conn() as con:
        items_by: dict[str, list[dict]] = {}
        for it in con.execute("SELECT * FROM supplier_po_items ORDER BY position, id"):
            items_by.setdefault(it["supplier_po_id"], []).append(dict(it))
        pkgs_by: dict[str, list[dict]] = {}
        for p in db._spo_packages(con):
            pkgs_by.setdefault(p["supplier_po_id"], []).append(p)
        refs_by: dict[str, list[dict]] = {}
        for r in con.execute(
                """SELECT DISTINCT spi.supplier_po_id AS po_id, p.id, p.po_number, p.customer_name FROM supplier_po_items spi
                   JOIN preorder_items pi ON pi.id = spi.preorder_item_id JOIN preorders p ON p.id = pi.preorder_id
                   ORDER BY p.po_number"""):
            refs_by.setdefault(r["po_id"], []).append({"id": r["id"], "so_number": r["po_number"], "customer_name": r["customer_name"]})
        gr_draft: dict[str, list[dict]] = {}
        gr_posted: dict[str, str] = {}
        for r in con.execute(
                "SELECT id, doc_number, supplier_po_id, status, posted_at, received_date FROM inbound_documents WHERE supplier_po_id IS NOT NULL"):
            if r["status"] == "draft":
                gr_draft.setdefault(r["supplier_po_id"], []).append({"id": r["id"], "doc_number": r["doc_number"]})
            elif r["status"] == "posted":
                at = _ymd(r["posted_at"]) or r["received_date"]
                if at and at > gr_posted.get(r["supplier_po_id"], ""):
                    gr_posted[r["supplier_po_id"]] = at
        phones = {r["id"]: r["phone"] for r in con.execute("SELECT id, phone FROM suppliers")}

    out = []
    for po in pos:
        if po["status"] == "batal":
            continue
        items = [i for i in items_by.get(po["id"], []) if i["quantity"] - i["qty_cancelled"] > 0]
        pkgs = [p for p in pkgs_by.get(po["id"], []) if not p["cancelled"]]
        issued = po["status"] in ("dipesan", "sebagian_diterima")
        summary = db._spo_pkg_summary(pkgs)

        value = received = moving = 0.0
        open_etas: list[str] = []
        for i in items:
            net = _net_price(i)
            qty = i["quantity"] - i["qty_cancelled"]
            value += qty * net
            received += min(i["qty_received"], qty) * net
            e = summary.get(i["id"])
            left = max(qty - i["qty_received"], 0)
            if e:
                moving += min(e["moving"], left) * net
                open_etas += e["etas"]
            if left > (e["open"] if e else 0):
                eta = i.get("eta_date") or po.get("est_arrival_date")
                if eta:
                    open_etas.append(eta)
        eta = min(open_etas) if open_etas and issued else None
        late = bool(issued and eta and eta < today)

        packages = [{"id": p["id"], "package_no": p["package_no"], "forwarder": p.get("forwarder_name") or "",
                     "status": p["status"], "state": p["state"], "eta": p["eta_date"], "tracking_no": p["tracking_no"],
                     "open": p["open"], "late": bool(p["open"] and p["eta_date"] and p["eta_date"] < today)} for p in pkgs]
        if po["receipt_status"] == "lengkap":
            ship_key, ship_label = "tiba", "Sudah Tiba"
        elif late:
            ship_key, ship_label = "terlambat", "Terlambat"
        elif po["shipping_status"]:
            ship_key, ship_label = po["shipping_status"], PO_SHIP_LABELS.get(po["shipping_status"], po["shipping_status"])
        else:
            ship_key, ship_label = None, "—"

        actions: list[dict] = []
        phone = phones.get(po["supplier_id"]) or ""
        if po["status"] == "draft":
            actions.append({"kind": "draft", "button": "Lihat PO", "text": "PO masih draft — belum dikirim ke supplier"})
        # Receiving can fill the PO before its Goods Receipt is posted, so this is checked on a fully received PO too.
        for d in gr_draft.get(po["id"], []):
            actions.append({"kind": "gr_draft", "button": "Buka GR", "inbound_id": d["id"],
                            "text": f"Goods Receipt {d['doc_number']} masih draft — belum diposting ke stok"})
        if issued:
            if late:
                actions.append({"kind": "terlambat", "button": "Lihat Tracking", "eta": eta,
                                "text": "Pengiriman melewati ETA"})
            for p in packages:
                if p["open"] and p["status"] == "tiba_di_hub":
                    actions.append({"kind": "tiba_hub", "button": "Terima Barang",
                                    "text": f"Paket {p['package_no']} sudah tiba di hub{' ' + p['forwarder'] if p['forwarder'] else ''} — perlu diterima"})
            for p in packages:
                if p["open"] and p["status"] == "dalam_pengiriman" and not (p["tracking_no"] or "").strip():
                    actions.append({"kind": "tanpa_resi", "button": "Hubungi Supplier", "phone": phone,
                                    "text": f"Resi paket {p['package_no']} belum diisi oleh supplier"})
            if po["order_status"] == "menunggu_konfirmasi":
                actions.append({"kind": "konfirmasi", "button": "Hubungi Supplier", "phone": phone,
                                "text": "Supplier belum memberikan konfirmasi pesanan"})

        out.append({
            "id": po["id"], "po_number": po["po_number"], "po_date": po["po_date"], "supplier_id": po["supplier_id"],
            "supplier_name": po["supplier_name_snapshot"], "supplier_phone": phone, "so_refs": refs_by.get(po["id"], []),
            "item_count": len(items), "qty_total": po["qty_total"], "qty_received": po["qty_received"],
            "item_names": [i["product_name"] for i in items], "grand_total": po["grand_total"],
            "value": round(value), "value_received": round(received), "value_moving": round(moving),
            "order_status": po["order_status"], "order_label": PO_ORDER_LABELS.get(po["order_status"], po["order_status"]),
            "shipping_status": po["shipping_status"], "ship_key": ship_key, "ship_label": ship_label,
            "receipt_status": po["receipt_status"], "packages": packages,
            "eta": eta, "late": late, "active": issued, "completed_at": gr_posted.get(po["id"]) if po["status"] == "diterima" else None,
            "actions": actions,
        })
    return {"today": today, "month": month, "rows": out, "order_labels": PO_ORDER_LABELS, "ship_labels": PO_SHIP_LABELS,
            "receipt_labels": PO_RECEIPT_LABELS}
