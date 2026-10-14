"""Stock Opname (hitung stok fisik). A session lists items with the counted quantity; stock only moves when an
admin approves it, and then through the stock ledger like every other movement."""
import sqlite3
from datetime import datetime

from backend.database import _conn, _now, get_master_item_usage

REASONS = ("Susut / Hilang", "Rusak", "Salah catat / input", "Ditemukan lebih", "Retur belum tercatat", "Lainnya")
STATUSES = ("draft", "menunggu_verifikasi", "disetujui", "ditolak", "dibatalkan")


def _next_no(con: sqlite3.Connection, day: str) -> str:
    prefix = f"OPN-{day.replace('-', '')}-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(opname_no, ?) AS INTEGER)) AS n FROM stock_opnames WHERE opname_no LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:03d}"


def number_preview(day: str) -> str:
    with _conn() as con:
        return _next_no(con, day)


def _activity_counts(con: sqlite3.Connection, master_item_id: int) -> dict:
    """What is still in flight for an item: the warning panel on the form."""
    kasir = con.execute(
        """SELECT COUNT(DISTINCT ri.receipt_id) AS c FROM receipt_items ri JOIN receipts r ON r.id = ri.receipt_id
           WHERE ri.master_item_id=? AND r.work_status != 'draft'""", (master_item_id,)
    ).fetchone()["c"]
    kasir_open = con.execute(
        """SELECT COUNT(DISTINCT ri.receipt_id) AS c FROM receipt_items ri JOIN receipts r ON r.id = ri.receipt_id
           WHERE ri.master_item_id=? AND r.status='pending'""", (master_item_id,)
    ).fetchone()["c"]
    po_cust = con.execute(
        """SELECT COUNT(DISTINCT preorder_id) AS c FROM preorder_items
           WHERE master_item_id=? AND status<>'terpasang' AND receipt_id IS NULL""", (master_item_id,)
    ).fetchone()["c"]
    po_sup = con.execute(
        """SELECT COUNT(DISTINCT spi.supplier_po_id) AS c FROM supplier_po_items spi
           JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
           WHERE spi.master_item_id=? AND sp.status IN ('dipesan','sebagian_diterima')""", (master_item_id,)
    ).fetchone()["c"]
    po_cust_qty = con.execute(
        """SELECT COALESCE(SUM(quantity),0) AS c FROM preorder_items
           WHERE master_item_id=? AND status<>'terpasang' AND receipt_id IS NULL""", (master_item_id,)
    ).fetchone()["c"]
    po_sup_qty = con.execute(
        """SELECT COALESCE(SUM(MAX(spi.quantity - spi.qty_received, 0)),0) AS c FROM supplier_po_items spi
           JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
           WHERE spi.master_item_id=? AND sp.status IN ('dipesan','sebagian_diterima')""", (master_item_id,)
    ).fetchone()["c"]
    return {"kasir_total": kasir, "kasir_terbuka": kasir_open, "po_customer_aktif": po_cust, "po_supplier_aktif": po_sup,
            "po_customer_qty": po_cust_qty, "po_supplier_qty": po_sup_qty}


def _items(con: sqlite3.Connection, opname_id: int) -> list[dict]:
    rows = con.execute(
        """SELECT oi.*, mi.sku, mi.name, mi.unit, mi.stock_qty AS current_qty, mi.location AS item_location,
                  mi.image_path, c.name AS category_name
           FROM stock_opname_items oi JOIN master_items mi ON mi.id = oi.master_item_id
           LEFT JOIN categories c ON c.id = mi.category_id
           WHERE oi.opname_id=? ORDER BY oi.id ASC""", (opname_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["activity"] = _activity_counts(con, d["master_item_id"])
        out.append(d)
    return out


def _header(con: sqlite3.Connection, opname_id: int) -> dict | None:
    r = con.execute("SELECT * FROM stock_opnames WHERE id=?", (opname_id,)).fetchone()
    return dict(r) if r else None


def get_opname(opname_id: int) -> dict | None:
    with _conn() as con:
        h = _header(con, opname_id)
        if h is None:
            return None
        h["items"] = _items(con, opname_id)
        _decorate(h)
        return h


def _decorate(h: dict) -> None:
    items = h["items"]
    h["item_count"] = len(items)
    h["total_diff"] = sum((i["diff_qty"] if i["diff_qty"] is not None else 0) for i in items)
    h["has_moved"] = any(h["status"] == "menunggu_verifikasi" and i["current_qty"] != i["system_qty"] for i in items)


def list_opnames(status: str | None = None, q: str | None = None) -> list[dict]:
    with _conn() as con:
        sql, params = "SELECT * FROM stock_opnames", []
        where = []
        if status:
            where.append("status=?"); params.append(status)
        if q:
            where.append("(opname_no LIKE ? OR officer LIKE ? OR location LIKE ? OR id IN "
                         "(SELECT oi.opname_id FROM stock_opname_items oi JOIN master_items mi ON mi.id = oi.master_item_id "
                         "WHERE mi.name LIKE ? OR mi.sku LIKE ?))")
            params += [f"%{q}%"] * 5
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT 500"
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        for h in rows:
            h["items"] = _items(con, h["id"])
            _decorate(h)
        return rows


def opname_summary() -> dict:
    with _conn() as con:
        c = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM stock_opnames GROUP BY status")}
        month = datetime.now().strftime("%Y-%m")
        approved_month = con.execute(
            "SELECT COUNT(*) AS n FROM stock_opnames WHERE status='disetujui' AND resolved_at LIKE ?", (month + "%",)
        ).fetchone()["n"]
    return {"draft": c.get("draft", 0), "menunggu_verifikasi": c.get("menunggu_verifikasi", 0),
            "disetujui": c.get("disetujui", 0), "disetujui_bulan_ini": approved_month,
            "ditolak": c.get("ditolak", 0), "total": sum(c.values())}


def _validate_items(con: sqlite3.Connection, items: list[dict], require_counts: bool) -> list[dict]:
    if not items:
        raise ValueError("Pilih minimal satu barang untuk dihitung.")
    seen, out = set(), []
    for it in items:
        mid = it["master_item_id"]
        if mid in seen:
            raise ValueError("Barang yang sama tidak boleh muncul dua kali dalam satu opname.")
        seen.add(mid)
        mi = con.execute("SELECT id, sku, name, stock_qty FROM master_items WHERE id=?", (mid,)).fetchone()
        if mi is None:
            raise ValueError(f"Barang {mid} tidak ditemukan.")
        counted = it.get("counted_qty")
        if counted is not None and counted < 0:
            raise ValueError(f"Stok fisik {mi['sku']} tidak boleh negatif.")
        if require_counts:
            if counted is None:
                raise ValueError(f"Isi stok fisik untuk {mi['sku']} sebelum dikirim.")
            if counted != mi["stock_qty"] and not (it.get("reason") or "").strip():
                raise ValueError(f"Alasan selisih wajib diisi untuk {mi['sku']}.")
        reason = (it.get("reason") or "").strip() or None
        if reason and reason not in REASONS:
            raise ValueError(f"Alasan selisih tidak dikenal: {reason}.")
        out.append({"master_item_id": mid, "counted_qty": counted, "reason": reason,
                    "note": (it.get("note") or "").strip() or None, "system_qty": mi["stock_qty"]})
    return out


def _write_items(con: sqlite3.Connection, opname_id: int, items: list[dict]) -> None:
    con.execute("DELETE FROM stock_opname_items WHERE opname_id=?", (opname_id,))
    for it in items:
        diff = None if it["counted_qty"] is None else it["counted_qty"] - it["system_qty"]
        con.execute(
            """INSERT INTO stock_opname_items (opname_id, master_item_id, system_qty, counted_qty, diff_qty, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (opname_id, it["master_item_id"], it["system_qty"], it["counted_qty"], diff, it["reason"], it["note"]),
        )


def create_opname(officer: str, opname_at: str | None, location: str | None, note: str | None,
                  items: list[dict], submit: bool, by: str | None) -> dict:
    if not (officer or "").strip():
        raise ValueError("Petugas wajib diisi.")
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = _validate_items(con, items, require_counts=submit)
        at = (opname_at or now).strip()
        day = at[:10]
        cur = con.execute(
            """INSERT INTO stock_opnames (opname_no, opname_at, officer, location, note, status, created_by, created_at,
                                          submitted_by, submitted_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_next_no(con, day), at, officer.strip(), (location or "").strip() or None, (note or "").strip() or None,
             "menunggu_verifikasi" if submit else "draft", by, now, by if submit else None, now if submit else None, now),
        )
        _write_items(con, cur.lastrowid, rows)
        oid = cur.lastrowid
    return get_opname(oid)


def update_opname(opname_id: int, officer: str, opname_at: str | None, location: str | None, note: str | None,
                  items: list[dict], submit: bool, by: str | None) -> dict:
    if not (officer or "").strip():
        raise ValueError("Petugas wajib diisi.")
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        h = _header(con, opname_id)
        if h is None:
            raise ValueError("Stock opname tidak ditemukan.")
        if h["status"] != "draft":
            raise ValueError("Hanya draft yang bisa diubah. Opname ini sudah dikirim atau diproses.")
        rows = _validate_items(con, items, require_counts=submit)
        con.execute(
            """UPDATE stock_opnames SET officer=?, opname_at=?, location=?, note=?, status=?, submitted_by=?, submitted_at=?,
                      updated_at=? WHERE id=?""",
            (officer.strip(), (opname_at or h["opname_at"]), (location or "").strip() or None,
             (note or "").strip() or None, "menunggu_verifikasi" if submit else "draft",
             by if submit else None, now if submit else None, now, opname_id),
        )
        _write_items(con, opname_id, rows)
    return get_opname(opname_id)


def submit_opname(opname_id: int, by: str | None) -> dict:
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        h = _header(con, opname_id)
        if h is None:
            raise ValueError("Stock opname tidak ditemukan.")
        if h["status"] != "draft":
            raise ValueError("Opname ini sudah dikirim atau diproses.")
        items = [dict(r) for r in con.execute("SELECT * FROM stock_opname_items WHERE opname_id=?", (opname_id,))]
        _validate_items(con, [{"master_item_id": i["master_item_id"], "counted_qty": i["counted_qty"],
                               "reason": i["reason"], "note": i["note"]} for i in items], require_counts=True)
        # Re-snapshot the system quantity at the moment of sending.
        for i in items:
            sys_q = con.execute("SELECT stock_qty FROM master_items WHERE id=?", (i["master_item_id"],)).fetchone()["stock_qty"]
            con.execute("UPDATE stock_opname_items SET system_qty=?, diff_qty=? WHERE id=?",
                        (sys_q, i["counted_qty"] - sys_q, i["id"]))
        con.execute("UPDATE stock_opnames SET status='menunggu_verifikasi', submitted_by=?, submitted_at=?, updated_at=? WHERE id=?",
                    (by, now, now, opname_id))
    return get_opname(opname_id)


def approve_opname(opname_id: int, by: str | None, note: str | None, accept_moved: bool) -> dict:
    """Posts the counted quantities. If an item's stock moved after the opname was sent (a sale, a receipt), the
    count may be stale: refuse unless the admin explicitly accepts, then adjust from the *current* stock."""
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        h = _header(con, opname_id)
        if h is None:
            raise ValueError("Stock opname tidak ditemukan.")
        if h["status"] != "menunggu_verifikasi":
            raise ValueError("Hanya opname yang menunggu verifikasi yang bisa disetujui.")
        items = [dict(r) for r in con.execute(
            """SELECT oi.*, mi.sku, mi.stock_qty AS current_qty FROM stock_opname_items oi
               JOIN master_items mi ON mi.id = oi.master_item_id WHERE oi.opname_id=?""", (opname_id,))]
        moved = [i for i in items if i["current_qty"] != i["system_qty"]]
        if moved and not accept_moved:
            lines = ", ".join(f"{i['sku']} (sistem {i['system_qty']} → sekarang {i['current_qty']})" for i in moved)
            raise ValueError(f"Stok berubah sejak opname dikirim: {lines}. Periksa dulu, lalu setujui dengan konfirmasi stok terbaru.")
        for i in items:
            delta = i["counted_qty"] - i["current_qty"]
            if delta == 0:
                con.execute("UPDATE stock_opname_items SET applied_delta=0 WHERE id=?", (i["id"],))
                continue
            con.execute("UPDATE master_items SET stock_qty=?, updated_at=?, updated_by=? WHERE id=?",
                        (i["counted_qty"], now, by, i["master_item_id"]))
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'ADJUSTMENT', ?, ?, 'stock_opname', ?, ?, ?, ?)""",
                (i["master_item_id"], delta, i["counted_qty"], opname_id,
                 f"Stock opname: {i['reason'] or 'sesuai hitung'}", now, by),
            )
            con.execute("UPDATE stock_opname_items SET applied_delta=? WHERE id=?", (delta, i["id"]))
        con.execute(
            "UPDATE stock_opnames SET status='disetujui', resolved_by=?, resolved_at=?, resolution_note=?, updated_at=? WHERE id=?",
            (by, now, (note or "").strip() or None, now, opname_id),
        )
    return get_opname(opname_id)


def close_opname(opname_id: int, status: str, by: str | None, note: str | None) -> dict:
    """ditolak (admin, from menunggu_verifikasi) or dibatalkan (draft or menunggu). No stock effect."""
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        h = _header(con, opname_id)
        if h is None:
            raise ValueError("Stock opname tidak ditemukan.")
        allowed = ("menunggu_verifikasi",) if status == "ditolak" else ("draft", "menunggu_verifikasi")
        if h["status"] not in allowed:
            raise ValueError("Status opname ini tidak bisa diubah ke " + status + ".")
        if status == "ditolak" and not (note or "").strip():
            raise ValueError("Alasan penolakan wajib diisi.")
        con.execute(
            "UPDATE stock_opnames SET status=?, resolved_by=?, resolved_at=?, resolution_note=?, updated_at=? WHERE id=?",
            (status, by, now, (note or "").strip() or None, now, opname_id),
        )
    return get_opname(opname_id)
