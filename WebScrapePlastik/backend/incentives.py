"""Karyawan, penugasan teknisi / sales per baris nota, progres kerja per item, and the Insentif module.

The Kasir only records WHO did the work (teknisi, possibly several per line) and WHO sold it (a sales utama on
the nota, overridable per line). Everything about money for those people lives here and is served only on the
admin-only /api/incentives routes; nothing in the receipt payloads carries a rate or an amount.

Owner rules (2026-10-21):
  * a Master Insentif rule is per SKU, with a teknisi rate and/or a sales rate, as dated versions. A version
    starts on its effective date and runs until the next one; versions are never edited, a new one is added;
  * when a line of a nota is "disahkan" (the nota leaves Draft, or the line is added to a running nota) the
    version in force that day is snapshotted on it; a later change in the master never touches it;
  * the rate is a POOL per line and role: several technicians share it (default equally, an Admin may set e.g.
    70:30), it is never paid once per technician;
  * basis = the line's net price after its own discount and its share of the nota discount, in whole rupiah;
  * teknisi becomes approvable when the item's work is Selesai AND the nota is Lunas, sales when the nota is
    Lunas (admin setting). Admins see and manage everything; approving, rejecting and deciding corrections is
    for an Admin with the Owner flag;
  * after approval amounts only move through ledger entries: corrections, return reversals, cancellation;
  * a return reverses the sales share pro rata; the teknisi share is kept once the installation was Selesai.

Money is integer rupiah; rates are basis points (3% = 300, 3.5% = 350), so nothing is a float.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta

from backend import database as db

ROLES = {"teknisi": "Teknisi", "sales": "Sales"}
WORK_STATES = {"antrian": "Antrian", "dikerjakan": "Sedang Dikerjakan", "selesai": "Selesai", "batal": "Dibatalkan"}
STATUS_LABELS = {
    "estimasi": "Estimasi", "belum_syarat": "Belum memenuhi syarat", "menunggu": "Menunggu approval",
    "disetujui": "Disetujui", "ditolak": "Ditolak", "batal": "Dibatalkan",
}
SOURCE_LABELS = {"master": "Aturan master", "tambahan": "Penambahan item nota", "koreksi": "Koreksi resmi"}
ENTRY_LABELS = {"persetujuan": "Persetujuan", "koreksi": "Koreksi", "reversal": "Reversal retur", "batal": "Pembatalan"}
POLICY_TECH_KEY, POLICY_SALES_KEY = "incentive_policy_teknisi", "incentive_policy_sales"
POLICY_TECH = {"selesai_lunas": "Pekerjaan item selesai dan nota lunas", "lunas": "Nota lunas", "selesai": "Pekerjaan item selesai"}
POLICY_SALES = {"lunas": "Nota lunas", "simpan": "Nota disimpan (bukan draft)"}
START_KEY = "incentive_start_at"
BP = 10000  # 100% in basis points


class Forbidden(Exception):
    """Raised when the user may not do this (route -> 403)."""


# ── schema ────────────────────────────────────────────────────────────────────

def migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            phone TEXT,
            is_technician INTEGER NOT NULL DEFAULT 0,
            is_sales INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER UNIQUE REFERENCES users(id),
            is_active INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            created_at TEXT NOT NULL, created_by TEXT, updated_at TEXT NOT NULL, updated_by TEXT);
        -- Per line of a nota: its sales override and the progress of its work. A separate table (not columns on
        -- receipt_items) because editing a nota deletes and re-inserts its lines under the same ids.
        CREATE TABLE IF NOT EXISTS receipt_item_work (
            receipt_item_id INTEGER PRIMARY KEY,
            receipt_id TEXT NOT NULL,
            sales_employee_id INTEGER REFERENCES employees(id),
            work_status TEXT,
            work_started_at TEXT, work_done_at TEXT, work_done_by TEXT,
            updated_at TEXT, updated_by TEXT);
        CREATE INDEX IF NOT EXISTS idx_receipt_item_work_receipt ON receipt_item_work(receipt_id);
        CREATE TABLE IF NOT EXISTS receipt_item_technicians (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT NOT NULL,
            receipt_item_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            position INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL, added_by TEXT,
            UNIQUE (receipt_item_id, employee_id));
        CREATE INDEX IF NOT EXISTS idx_rit_receipt ON receipt_item_technicians(receipt_id);
        CREATE INDEX IF NOT EXISTS idx_rit_employee ON receipt_item_technicians(employee_id);

        CREATE TABLE IF NOT EXISTS incentive_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_item_id INTEGER NOT NULL UNIQUE REFERENCES master_items(id),
            created_at TEXT NOT NULL, created_by TEXT);
        CREATE TABLE IF NOT EXISTS incentive_rule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL REFERENCES incentive_rules(id),
            master_item_id INTEGER NOT NULL,
            version_no INTEGER NOT NULL,
            effective_from TEXT NOT NULL,
            tech_rate_bp INTEGER,
            sales_rate_bp INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            calc_type TEXT NOT NULL DEFAULT 'persen',
            basis TEXT NOT NULL DEFAULT 'harga_bersih',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT,
            cancelled_at TEXT, cancelled_by TEXT, cancel_reason TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_irv_rule_date ON incentive_rule_versions(rule_id, effective_from)
            WHERE cancelled_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_irv_item ON incentive_rule_versions(master_item_id, effective_from);

        -- The snapshot: one row per line of a nota and role, taken when the line is disahkan.
        CREATE TABLE IF NOT EXISTS incentive_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT NOT NULL,
            receipt_item_id INTEGER NOT NULL,
            master_item_id INTEGER,
            role TEXT NOT NULL CHECK (role IN ('teknisi','sales')),
            rule_version_id INTEGER,
            rate_bp INTEGER NOT NULL,
            orig_rate_bp INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'master',
            snapshot_at TEXT NOT NULL, snapshot_date TEXT NOT NULL,
            line_qty INTEGER NOT NULL DEFAULT 0,
            excluded_qty INTEGER NOT NULL DEFAULT 0,
            base_full INTEGER NOT NULL DEFAULT 0,
            base_amount INTEGER NOT NULL DEFAULT 0,
            pool_amount INTEGER NOT NULL DEFAULT 0,
            split_custom INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'aktif',
            cancel_reason TEXT, cancelled_at TEXT,
            approval_state TEXT, approved_by TEXT, approved_at TEXT, decide_note TEXT,
            frozen_base_full INTEGER,
            UNIQUE (receipt_item_id, role));
        CREATE INDEX IF NOT EXISTS idx_incentive_lines_receipt ON incentive_lines(receipt_id);
        CREATE TABLE IF NOT EXISTS incentive_shares (
            line_id INTEGER NOT NULL REFERENCES incentive_lines(id),
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            share_bp INTEGER NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (line_id, employee_id));
        CREATE INDEX IF NOT EXISTS idx_incentive_shares_employee ON incentive_shares(employee_id);
        -- The ledger of final money: approval, then corrections / reversals / cancellation as signed rows.
        CREATE TABLE IF NOT EXISTS incentive_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_id INTEGER NOT NULL REFERENCES incentive_lines(id),
            receipt_id TEXT NOT NULL,
            employee_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            kind TEXT NOT NULL,
            amount INTEGER NOT NULL,
            request_id INTEGER, return_id INTEGER,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_incentive_entries_line ON incentive_entries(line_id);
        CREATE INDEX IF NOT EXISTS idx_incentive_entries_employee ON incentive_entries(employee_id);
        CREATE TABLE IF NOT EXISTS incentive_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            line_id INTEGER,
            receipt_id TEXT NOT NULL,
            receipt_item_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            old_rate_bp INTEGER, new_rate_bp INTEGER NOT NULL,
            old_pool INTEGER NOT NULL DEFAULT 0, new_pool INTEGER NOT NULL DEFAULT 0,
            shares_json TEXT, before_json TEXT, after_json TEXT,
            reason TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'menunggu',
            requested_by TEXT, requested_at TEXT NOT NULL,
            decided_by TEXT, decided_at TEXT, decide_note TEXT);
        CREATE INDEX IF NOT EXISTS idx_incentive_requests_state ON incentive_requests(state);
        CREATE TABLE IF NOT EXISTS incentive_reversals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id INTEGER NOT NULL,
            line_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            kept INTEGER NOT NULL DEFAULT 0,
            policy TEXT,
            created_at TEXT NOT NULL, created_by TEXT,
            UNIQUE (return_id, line_id));
        CREATE TABLE IF NOT EXISTS incentive_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT, line_id INTEGER, master_item_id INTEGER,
            kind TEXT NOT NULL, note TEXT, by_user TEXT, at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_incentive_events_line ON incentive_events(line_id);
        CREATE INDEX IF NOT EXISTS idx_incentive_events_receipt ON incentive_events(receipt_id);
    """)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(receipts)").fetchall()}
    if "sales_employee_id" not in cols:
        con.execute("ALTER TABLE receipts ADD COLUMN sales_employee_id INTEGER REFERENCES employees(id)")
    if "remarks" not in cols:
        con.execute("ALTER TABLE receipts ADD COLUMN remarks TEXT")
    # Notas from before this module never get a snapshot: their lines were disahkan without any rule.
    con.execute("INSERT OR IGNORE INTO app_settings (key, value, updated_by, updated_at) VALUES (?, ?, 'sistem', ?)",
                (START_KEY, db._now(), db._now()))


# ── small helpers ─────────────────────────────────────────────────────────────

def _text(v, limit: int | None = None) -> str | None:
    v = " ".join(v.split()) if isinstance(v, str) else v
    if v and limit and len(v) > limit:
        raise ValueError(f"Maksimal {limit} karakter.")
    return v or None


def _today() -> str:
    return db.shop_today().isoformat()


def _event(con, kind: str, note: str, by: str | None, *, receipt_id=None, line_id=None, master_item_id=None) -> None:
    con.execute("INSERT INTO incentive_events (receipt_id, line_id, master_item_id, kind, note, by_user, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt_id, line_id, master_item_id, kind, note, by, db._now()))


def pct_to_bp(value) -> int | None:
    """'3', '3,5', 3.5 -> 300, 350. None / '' -> None (no rate for that role). Max two decimals, 0 < rate <= 100."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Tarif tidak valid.")
    s = str(value).strip().replace(",", ".").rstrip("%").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,2})?", s):
        raise ValueError("Tarif harus angka persen, maksimal dua desimal, tidak boleh negatif (mis. 3 atau 3,5).")
    whole, _, frac = s.partition(".")
    bp = int(whole) * 100 + int((frac + "00")[:2])
    if bp <= 0 or bp > BP:
        raise ValueError("Tarif harus lebih dari 0% dan paling besar 100%.")
    return bp


def bp_label(bp: int | None) -> str:
    if bp is None:
        return "—"
    whole, frac = divmod(bp, 100)
    return f"{whole}%" if not frac else f"{whole},{frac:02d}".rstrip("0") + "%"


def _pct_of(base: int, bp: int) -> int:
    """base x bp / 10000 rounded half up, in whole rupiah (base and bp are non-negative)."""
    return (base * bp * 2 + BP) // (2 * BP)


def _split(pool: int, shares: list[tuple[int, int]]) -> dict[int, int]:
    """Pool over (employee_id, share_bp) pairs; the rounding remainder goes to the largest share (first on a tie),
    so the parts always add up to the pool exactly."""
    if not shares:
        return {}
    out = {emp: pool * bp // BP for emp, bp in shares}
    rest = pool - sum(out.values())
    if rest:
        top = max(shares, key=lambda s: s[1])[0]
        out[top] += rest
    return out


def _equal_bps(n: int) -> list[int]:
    if n <= 0:
        return []
    each = BP // n
    return [each + (BP - each * n if i == 0 else 0) for i in range(n)]


def get_policy(con=None) -> dict:
    tech = db.get_setting(POLICY_TECH_KEY, "selesai_lunas", con)
    sales = db.get_setting(POLICY_SALES_KEY, "lunas", con)
    return {"teknisi": tech if tech in POLICY_TECH else "selesai_lunas", "sales": sales if sales in POLICY_SALES else "lunas"}


def set_policy(teknisi: str, sales: str, by: str) -> dict:
    if teknisi not in POLICY_TECH or sales not in POLICY_SALES:
        raise ValueError("Pilihan syarat tidak dikenal.")
    with db._conn() as con:
        before = get_policy(con)
        for key, val in ((POLICY_TECH_KEY, teknisi), (POLICY_SALES_KEY, sales)):
            con.execute("INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE "
                        "SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at", (key, val, by, db._now()))
        _event(con, "kebijakan", f"Syarat approval: teknisi {POLICY_TECH[before['teknisi']]} -> {POLICY_TECH[teknisi]}; "
                                 f"sales {POLICY_SALES[before['sales']]} -> {POLICY_SALES[sales]}", by)
    return get_policy()


# ── Master Karyawan ───────────────────────────────────────────────────────────

def _employee_out(r) -> dict:
    d = dict(r)
    d["is_technician"], d["is_sales"], d["is_active"] = bool(d["is_technician"]), bool(d["is_sales"]), bool(d["is_active"])
    return d


def list_employees(active_only: bool = False, role: str | None = None, q: str | None = None) -> list[dict]:
    sql = ("SELECT e.*, u.username AS username FROM employees e LEFT JOIN users u ON u.id = e.user_id WHERE 1=1")
    args: list = []
    if active_only:
        sql += " AND e.is_active=1"
    if role == "teknisi":
        sql += " AND e.is_technician=1"
    elif role == "sales":
        sql += " AND e.is_sales=1"
    if q:
        sql += " AND (e.name LIKE ? OR e.phone LIKE ?)"
        args += [f"%{q.strip()}%"] * 2
    sql += " ORDER BY e.is_active DESC, e.name COLLATE NOCASE"
    with db._conn() as con:
        return [_employee_out(r) for r in con.execute(sql, args).fetchall()]


def _check_employee_fields(con, name, phone, is_technician, is_sales, user_id, own_id=None):
    name = _text(name)
    if not name or len(name) < 2 or len(name) > 60:
        raise ValueError("Nama karyawan 2-60 karakter.")
    phone = db.normalize_phone(phone or "") or None
    if phone and not db.PHONE_RE.match(phone):
        raise ValueError("Nomor HP harus 7-15 digit.")
    if not is_technician and not is_sales:
        raise ValueError("Pilih minimal satu peran: Teknisi atau Sales.")
    key = name.upper()
    dup = con.execute("SELECT id FROM employees WHERE name_key=? AND id IS NOT ?", (key, own_id)).fetchone()
    if dup:
        raise ValueError(f"Karyawan bernama {name} sudah ada. Pakai nama yang membedakan (mis. {name} B).")
    if user_id is not None:
        if not con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise ValueError("Akun login yang ditautkan tidak ditemukan.")
        if con.execute("SELECT 1 FROM employees WHERE user_id=? AND id IS NOT ?", (user_id, own_id)).fetchone():
            raise ValueError("Akun login itu sudah ditautkan ke karyawan lain.")
    return name, key, phone


def create_employee(data: dict, by: str) -> dict:
    with db._conn() as con:
        name, key, phone = _check_employee_fields(con, data.get("name"), data.get("phone"), bool(data.get("is_technician")),
                                                  bool(data.get("is_sales")), data.get("user_id"))
        now = db._now()
        cur = con.execute(
            """INSERT INTO employees (name, name_key, phone, is_technician, is_sales, user_id, is_active, note, created_at, created_by,
                   updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (name, key, phone, int(bool(data.get("is_technician"))), int(bool(data.get("is_sales"))), data.get("user_id"),
             _text(data.get("note"), 300), now, by, now, by))
        return _employee_out(con.execute("SELECT e.*, NULL AS username FROM employees e WHERE id=?", (cur.lastrowid,)).fetchone())


def update_employee(employee_id: int, data: dict, by: str) -> dict:
    with db._conn() as con:
        row = con.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        if row is None:
            raise LookupError("Karyawan tidak ditemukan.")
        cur = dict(row)
        merged = {**cur, **{k: v for k, v in data.items() if k in ("name", "phone", "is_technician", "is_sales", "user_id", "note", "is_active")}}
        name, key, phone = _check_employee_fields(con, merged["name"], merged["phone"], bool(merged["is_technician"]),
                                                  bool(merged["is_sales"]), merged["user_id"], employee_id)
        con.execute("""UPDATE employees SET name=?, name_key=?, phone=?, is_technician=?, is_sales=?, user_id=?, is_active=?, note=?,
                       updated_at=?, updated_by=? WHERE id=?""",
                    (name, key, phone, int(bool(merged["is_technician"])), int(bool(merged["is_sales"])), merged["user_id"],
                     int(bool(merged["is_active"])), _text(merged.get("note"), 300), db._now(), by, employee_id))
        return _employee_out(con.execute("SELECT e.*, u.username FROM employees e LEFT JOIN users u ON u.id=e.user_id WHERE e.id=?",
                                         (employee_id,)).fetchone())


# ── Kasir: assignments and work progress ──────────────────────────────────────

def _assert_employee(con, emp_id, role: str, existing: set[int]) -> None:
    row = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if row is None:
        raise ValueError("Karyawan yang dipilih tidak ditemukan.")
    if emp_id in existing:
        return  # already on this line: kept even if the person has since been deactivated
    if not row["is_active"]:
        raise ValueError(f"{row['name']} sudah nonaktif dan tidak bisa dipilih untuk penugasan baru.")
    if role == "teknisi" and not row["is_technician"]:
        raise ValueError(f"{row['name']} bukan teknisi.")
    if role == "sales" and not row["is_sales"]:
        raise ValueError(f"{row['name']} bukan sales.")


def _locked_lines(con, item_id: int) -> dict[str, bool]:
    return {r["role"]: True for r in con.execute(
        "SELECT role FROM incentive_lines WHERE receipt_item_id=? AND state='aktif' AND approval_state='disetujui'", (item_id,)).fetchall()}


def apply_assignments(con, receipt_id: str, assign: dict, by: str | None) -> None:
    """assign = {"sales_employee_id"?: int|None, "items": {item_id: {"technician_ids"?: [...], "sales_employee_id"?: int|None}}}.
    Only keys present are changed."""
    rec = con.execute("SELECT sales_employee_id FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if "sales_employee_id" in assign:
        new = assign["sales_employee_id"]
        if new != rec["sales_employee_id"]:
            if new is not None:
                _assert_employee(con, new, "sales", {rec["sales_employee_id"]} if rec["sales_employee_id"] else set())
            con.execute("UPDATE receipts SET sales_employee_id=? WHERE id=?", (new, receipt_id))
    now = db._now()
    for item_id, a in (assign.get("items") or {}).items():
        item = con.execute("SELECT id, product_name FROM receipt_items WHERE id=? AND receipt_id=?", (item_id, receipt_id)).fetchone()
        if item is None:
            continue
        locked = _locked_lines(con, item_id)
        work = con.execute("SELECT * FROM receipt_item_work WHERE receipt_item_id=?", (item_id,)).fetchone()
        if "sales_employee_id" in a:
            new = a["sales_employee_id"]
            old = work["sales_employee_id"] if work else None
            if new != old:
                if locked.get("sales"):
                    raise ValueError(f"Sales untuk {item['product_name']} sudah dikunci back-office. Minta Admin melakukan koreksi.")
                if new is not None:
                    _assert_employee(con, new, "sales", {old} if old else set())
                con.execute("""INSERT INTO receipt_item_work (receipt_item_id, receipt_id, sales_employee_id, updated_at, updated_by)
                               VALUES (?, ?, ?, ?, ?) ON CONFLICT(receipt_item_id) DO UPDATE SET sales_employee_id=excluded.sales_employee_id,
                               updated_at=excluded.updated_at, updated_by=excluded.updated_by""", (item_id, receipt_id, new, now, by))
        if "technician_ids" in a and a["technician_ids"] is not None:
            ids = [int(x) for x in a["technician_ids"]]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Teknisi dipilih dua kali pada {item['product_name']}.")
            if len(ids) > 6:
                raise ValueError("Maksimal 6 teknisi per item.")
            old_ids = [r["employee_id"] for r in con.execute(
                "SELECT employee_id FROM receipt_item_technicians WHERE receipt_item_id=? ORDER BY position, id", (item_id,)).fetchall()]
            if ids == old_ids:
                continue
            if locked.get("teknisi"):
                raise ValueError(f"Teknisi untuk {item['product_name']} sudah dikunci back-office. Minta Admin melakukan koreksi.")
            for emp in ids:
                _assert_employee(con, emp, "teknisi", set(old_ids))
            for gone in set(old_ids) - set(ids):
                con.execute("DELETE FROM receipt_item_technicians WHERE receipt_item_id=? AND employee_id=?", (item_id, gone))
            for pos, emp in enumerate(ids):
                con.execute("""INSERT INTO receipt_item_technicians (receipt_id, receipt_item_id, employee_id, position, added_at, added_by)
                               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(receipt_item_id, employee_id) DO UPDATE SET position=excluded.position""",
                            (receipt_id, item_id, emp, pos, now, by))
            names = [r["name"] for r in con.execute(
                "SELECT e.name FROM receipt_item_technicians t JOIN employees e ON e.id=t.employee_id WHERE t.receipt_item_id=? ORDER BY t.position",
                (item_id,)).fetchall()]
            # A line with technicians has work to track; the first assignment puts it in the queue.
            if ids and (work is None or not work["work_status"]):
                con.execute("""INSERT INTO receipt_item_work (receipt_item_id, receipt_id, work_status, updated_at, updated_by)
                               VALUES (?, ?, 'antrian', ?, ?) ON CONFLICT(receipt_item_id) DO UPDATE SET work_status='antrian',
                               updated_at=excluded.updated_at, updated_by=excluded.updated_by""", (item_id, receipt_id, now, by))
            elif not ids and work is not None and work["work_status"] in ("antrian",):
                con.execute("UPDATE receipt_item_work SET work_status=NULL WHERE receipt_item_id=?", (item_id,))
            if old_ids or ids:
                db._log_receipt_event(con, receipt_id, "assign",
                                      f"Teknisi {item['product_name']}: " + (", ".join(names) if names else "dikosongkan"), by)


def item_assignments(con, receipt_id: str) -> dict[int, dict]:
    """item_id -> {technicians: [{id, name, is_active}], sales_employee_id, sales_name, work_status, ...} for the Kasir."""
    out: dict[int, dict] = {}
    for r in con.execute("""SELECT w.*, e.name AS sales_name, e.is_active AS sales_active FROM receipt_item_work w
                            LEFT JOIN employees e ON e.id = w.sales_employee_id WHERE w.receipt_id=?""", (receipt_id,)).fetchall():
        out[r["receipt_item_id"]] = {"technicians": [], "sales_employee_id": r["sales_employee_id"], "sales_name": r["sales_name"],
                                     "work_status": r["work_status"], "work_done_at": r["work_done_at"], "work_done_by": r["work_done_by"],
                                     "work_started_at": r["work_started_at"]}
    for r in con.execute("""SELECT t.receipt_item_id, e.id, e.name, e.is_active FROM receipt_item_technicians t
                            JOIN employees e ON e.id = t.employee_id WHERE t.receipt_id=? ORDER BY t.position, t.id""", (receipt_id,)).fetchall():
        out.setdefault(r["receipt_item_id"], {"technicians": [], "sales_employee_id": None, "sales_name": None, "work_status": None,
                                              "work_done_at": None, "work_done_by": None, "work_started_at": None})
        out[r["receipt_item_id"]]["technicians"].append({"id": r["id"], "name": r["name"], "is_active": bool(r["is_active"])})
    return out


def set_item_work(receipt_id: str, item_id: int, status: str, user: dict) -> dict:
    """Progress of one line's work: Antrian -> Sedang Dikerjakan -> Selesai (or Dibatalkan, admin only). Payment is
    untouched. The first line started moves a nota that is Belum Dikerjakan into Dalam Pengerjaan."""
    if status not in WORK_STATES:
        raise ValueError("Status pekerjaan tidak dikenal.")
    if status == "batal" and user["role"] != "admin":
        raise Forbidden("Hanya admin yang boleh membatalkan pekerjaan item.")
    by = user["username"]
    with db._conn() as con:
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if rec is None:
            raise LookupError("Nota tidak ditemukan.")
        if rec["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        if rec["work_status"] == "draft":
            raise ValueError("Nota masih Draft. Simpan nota dulu sebelum mencatat pekerjaan.")
        if db._is_closed(rec):
            raise ValueError("Nota sudah lunas dan selesai (ditutup).")
        item = con.execute("SELECT * FROM receipt_items WHERE id=? AND receipt_id=?", (item_id, receipt_id)).fetchone()
        if item is None:
            raise LookupError("Item tidak ditemukan di nota ini.")
        has_tech = con.execute("SELECT 1 FROM receipt_item_technicians WHERE receipt_item_id=?", (item_id,)).fetchone()
        if not has_tech and status in ("dikerjakan", "selesai"):
            raise ValueError(f"Pilih teknisi untuk {item['product_name']} dulu sebelum mencatat progres pekerjaan.")
        work = con.execute("SELECT * FROM receipt_item_work WHERE receipt_item_id=?", (item_id,)).fetchone()
        cur = (work["work_status"] if work else None) or "antrian"
        if cur == status:
            raise ValueError("Pekerjaan item sudah berstatus itu.")
        if cur == "selesai" and _locked_lines(con, item_id).get("teknisi"):
            raise ValueError("Pekerjaan ini sudah disetujui back-office; perubahan lewat koreksi Admin.")
        now = db._now()
        started = (work["work_started_at"] if work else None) or (now if status in ("dikerjakan", "selesai") else None)
        done_at, done_by = (now, by) if status == "selesai" else (None, None)
        con.execute("""INSERT INTO receipt_item_work (receipt_item_id, receipt_id, work_status, work_started_at, work_done_at, work_done_by,
                           updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(receipt_item_id) DO UPDATE SET work_status=excluded.work_status, work_started_at=excluded.work_started_at,
                           work_done_at=excluded.work_done_at, work_done_by=excluded.work_done_by, updated_at=excluded.updated_at,
                           updated_by=excluded.updated_by""",
                    (item_id, receipt_id, status, started, done_at, done_by, now, by))
        db._log_receipt_event(con, receipt_id, "item_work", f"Pekerjaan {item['product_name']}: {WORK_STATES[status]}", by)
        if status in ("dikerjakan", "selesai") and rec["work_status"] in ("belum", "menunggu"):
            con.execute("UPDATE receipts SET work_status='pengerjaan', work_stage=COALESCE(work_stage, 'persiapan') WHERE id=?", (receipt_id,))
            db._log_receipt_event(con, receipt_id, "status", "Status: Dalam Pengerjaan — pekerjaan item dimulai", by)
        sync_receipt(con, receipt_id, by)
    return work_order_detail(receipt_id)


def finish_assigned_items(con, receipt_id: str, by: str | None) -> list[str]:
    """Pengerjaan Selesai on the whole nota: every line that has technicians and is not finished yet is marked
    Selesai with it (logged), because the nota's own status says the work is done."""
    rows = con.execute("""SELECT ri.id, ri.product_name, w.work_status FROM receipt_items ri
                          JOIN receipt_item_technicians t ON t.receipt_item_id = ri.id
                          LEFT JOIN receipt_item_work w ON w.receipt_item_id = ri.id
                          WHERE ri.receipt_id=? GROUP BY ri.id""", (receipt_id,)).fetchall()
    now, done = db._now(), []
    for r in rows:
        if r["work_status"] in ("selesai", "batal"):
            continue
        con.execute("""INSERT INTO receipt_item_work (receipt_item_id, receipt_id, work_status, work_started_at, work_done_at, work_done_by,
                           updated_at, updated_by) VALUES (?, ?, 'selesai', ?, ?, ?, ?, ?)
                       ON CONFLICT(receipt_item_id) DO UPDATE SET work_status='selesai', work_started_at=COALESCE(work_started_at, excluded.work_started_at),
                           work_done_at=excluded.work_done_at, work_done_by=excluded.work_done_by, updated_at=excluded.updated_at,
                           updated_by=excluded.updated_by""", (r["id"], receipt_id, now, now, by, now, by))
        done.append(r["product_name"])
    if done:
        db._log_receipt_event(con, receipt_id, "item_work", "Pekerjaan ditandai selesai bersama nota: " + ", ".join(done), by)
    return done


# ── the engine ────────────────────────────────────────────────────────────────

def _local_day(iso: str | None) -> str | None:
    return db._shop_local_date(iso) if iso else None


def _version_on(con, master_item_id: int, day: str):
    return con.execute("""SELECT * FROM incentive_rule_versions WHERE master_item_id=? AND cancelled_at IS NULL AND effective_from<=?
                          ORDER BY effective_from DESC, id DESC LIMIT 1""", (master_item_id, day)).fetchone()


def _discount_alloc(items: list, discount: int) -> dict[int, int]:
    nets = {it["id"]: max(db._line_total(dict(it)), 0) for it in items}
    total = sum(nets.values())
    if not discount or total <= 0:
        return {i: 0 for i in nets}
    out = {i: discount * n // total for i, n in nets.items()}
    rest = discount - sum(out.values())
    if rest:
        out[max(nets, key=lambda i: nets[i])] += rest
    return out


def _cancel_line(con, line, why: str, by: str | None) -> None:
    con.execute("UPDATE incentive_lines SET state='batal', cancel_reason=?, cancelled_at=? WHERE id=? AND state='aktif'",
                (why, db._now(), line["id"]))
    if line["approval_state"] == "disetujui":
        for r in con.execute("SELECT employee_id, SUM(amount) AS s FROM incentive_entries WHERE line_id=? GROUP BY employee_id",
                             (line["id"],)).fetchall():
            if r["s"]:
                con.execute("""INSERT INTO incentive_entries (line_id, receipt_id, employee_id, role, kind, amount, note, created_by, created_at)
                               VALUES (?, ?, ?, ?, 'batal', ?, ?, ?, ?)""",
                            (line["id"], line["receipt_id"], r["employee_id"], line["role"], -r["s"], why, by, db._now()))
    _event(con, "batal", why, by, receipt_id=line["receipt_id"], line_id=line["id"])


def _recipients(con, line, rec, work: dict | None) -> list[int]:
    if line["role"] == "teknisi":
        return [r["employee_id"] for r in con.execute(
            "SELECT employee_id FROM receipt_item_technicians WHERE receipt_item_id=? ORDER BY position, id", (line["receipt_item_id"],)).fetchall()]
    emp = (work or {}).get("sales_employee_id") or rec["sales_employee_id"]
    return [emp] if emp else []


def _target_amounts(base_full: int, line_qty: int, excluded: int, rate_bp: int, shares: list[tuple[int, int]]) -> tuple[int, int, dict]:
    remaining = max(line_qty - excluded, 0)
    base = (base_full * remaining * 2 + line_qty) // (2 * line_qty) if line_qty > 0 else 0
    pool = _pct_of(base, rate_bp)
    return base, pool, _split(pool, shares)


def _write_shares(con, line_id: int, shares: list[tuple[int, int]], amounts: dict) -> None:
    con.execute("DELETE FROM incentive_shares WHERE line_id=?", (line_id,))
    for pos, (emp, bp) in enumerate(shares):
        con.execute("INSERT INTO incentive_shares (line_id, employee_id, share_bp, amount, position) VALUES (?, ?, ?, ?, ?)",
                    (line_id, emp, bp, amounts.get(emp, 0), pos))


def _current_shares(con, line_id: int) -> list[tuple[int, int]]:
    return [(r["employee_id"], r["share_bp"]) for r in con.execute(
        "SELECT employee_id, share_bp FROM incentive_shares WHERE line_id=? ORDER BY position", (line_id,)).fetchall()]


def _post_diff(con, line, target: dict, kind: str, by: str | None, note: str, *, request_id=None, return_id=None) -> int:
    """Ledger rows that move what each person holds on this line to `target`. Returns the net change."""
    held = {r["employee_id"]: r["s"] for r in con.execute(
        "SELECT employee_id, SUM(amount) AS s FROM incentive_entries WHERE line_id=? GROUP BY employee_id", (line["id"],)).fetchall()}
    net = 0
    for emp in sorted(set(held) | set(target)):
        diff = target.get(emp, 0) - (held.get(emp) or 0)
        if diff:
            con.execute("""INSERT INTO incentive_entries (line_id, receipt_id, employee_id, role, kind, amount, request_id, return_id, note,
                               created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (line["id"], line["receipt_id"], emp, line["role"], kind, diff, request_id, return_id, note, by, db._now()))
            net += diff
    return net


def sync_receipt(con, receipt_id: str, by: str | None, assign: dict | None = None) -> None:
    """Brings a nota's incentive rows in line with the nota: called inside every transaction that changes its lines,
    discount, status or assignments. Takes the snapshot of lines that are disahkan now, recomputes lines that are not
    approved yet, cancels lines whose item is gone or whose nota is void. Never touches stock or payments."""
    rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if rec is None:
        return
    # Lines a Sales Order split / merge moved to another invoice take their rows along instead of being "deleted".
    for table in ("incentive_lines", "receipt_item_work", "receipt_item_technicians", "incentive_requests"):
        con.execute(f"""UPDATE {table} SET receipt_id=(SELECT ri.receipt_id FROM receipt_items ri WHERE ri.id={table}.receipt_item_id)
                        WHERE (receipt_id=? OR receipt_item_id IN (SELECT id FROM receipt_items WHERE receipt_id=?))
                          AND EXISTS (SELECT 1 FROM receipt_items ri WHERE ri.id={table}.receipt_item_id AND ri.receipt_id<>{table}.receipt_id)""",
                    (receipt_id, receipt_id))
    con.execute("""UPDATE incentive_entries SET receipt_id=(SELECT l.receipt_id FROM incentive_lines l WHERE l.id=incentive_entries.line_id)
                   WHERE line_id IN (SELECT id FROM incentive_lines WHERE receipt_id=?) AND receipt_id<>?""", (receipt_id, receipt_id))
    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id", (receipt_id,)).fetchall()]
    ids = {it["id"] for it in items}
    for table in ("receipt_item_work", "receipt_item_technicians"):
        stale = [r["receipt_item_id"] for r in con.execute(f"SELECT receipt_item_id FROM {table} WHERE receipt_id=?", (receipt_id,)).fetchall()
                 if r["receipt_item_id"] not in ids]
        for sid in stale:
            con.execute(f"DELETE FROM {table} WHERE receipt_item_id=?", (sid,))
    if assign:
        apply_assignments(con, receipt_id, assign, by)
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    lines = [dict(r) for r in con.execute("SELECT * FROM incentive_lines WHERE receipt_id=?", (receipt_id,)).fetchall()]
    for ln in lines:
        if ln["state"] == "aktif" and ln["receipt_item_id"] not in ids:
            _cancel_line(con, ln, "Item dihapus dari nota", by)
    if rec["status"] == "void":
        for ln in lines:
            if ln["state"] == "aktif" and ln["receipt_item_id"] in ids:
                _cancel_line(con, ln, "Nota dibatalkan (void)", by)
        return
    if rec["work_status"] == "draft":
        return
    start = db.get_setting(START_KEY, None, con) or ""
    have = {(ln["receipt_item_id"], ln["role"]) for ln in lines}
    today, now = _today(), db._now()
    for it in items:
        if it["item_type"] == "jasa" or it["master_item_id"] is None or (it.get("added_at") or rec["created_at"]) < start:
            continue
        ver = None
        for role, col in (("teknisi", "tech_rate_bp"), ("sales", "sales_rate_bp")):
            if (it["id"], role) in have:
                continue
            ver = ver or _version_on(con, it["master_item_id"], today)
            if ver is None or not ver["is_active"] or not ver[col]:
                continue
            source = "tambahan" if it.get("added_during_work") else "master"
            cur = con.execute("""INSERT INTO incentive_lines (receipt_id, receipt_item_id, master_item_id, role, rule_version_id, rate_bp,
                                     orig_rate_bp, source, snapshot_at, snapshot_date, line_qty) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                              (receipt_id, it["id"], it["master_item_id"], role, ver["id"], ver[col], ver[col], source, now, today, it["quantity"]))
            _event(con, "snapshot", f"{ROLES[role]} {bp_label(ver[col])} dari versi V{ver['version_no']} ({SOURCE_LABELS[source]})", by,
                   receipt_id=receipt_id, line_id=cur.lastrowid)
    recompute(con, receipt_id)


def recompute(con, receipt_id: str) -> None:
    rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    items = {r["id"]: dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)).fetchall()}
    alloc = _discount_alloc(list(items.values()), rec["discount"] or 0)
    works = {r["receipt_item_id"]: dict(r) for r in con.execute("SELECT * FROM receipt_item_work WHERE receipt_id=?", (receipt_id,)).fetchall()}
    for ln in [dict(r) for r in con.execute(
            "SELECT * FROM incentive_lines WHERE receipt_id=? AND state='aktif' AND COALESCE(approval_state,'')<>'disetujui'", (receipt_id,)).fetchall()]:
        it = items.get(ln["receipt_item_id"])
        if it is None:
            continue
        base_full = max(db._line_total(it) - alloc.get(it["id"], 0), 0)
        recips = _recipients(con, ln, rec, works.get(it["id"]))
        prev = _current_shares(con, ln["id"])
        if ln["split_custom"] and sorted(e for e, _ in prev) == sorted(recips):
            shares = prev
        else:
            if ln["split_custom"]:
                _event(con, "porsi", "Porsi kembali dibagi rata karena teknisi berubah", None, receipt_id=receipt_id, line_id=ln["id"])
            shares = list(zip(recips, _equal_bps(len(recips))))
            con.execute("UPDATE incentive_lines SET split_custom=0 WHERE id=?", (ln["id"],))
        base, pool, amounts = _target_amounts(base_full, it["quantity"], min(ln["excluded_qty"], it["quantity"]), ln["rate_bp"], shares)
        con.execute("UPDATE incentive_lines SET line_qty=?, base_full=?, base_amount=?, pool_amount=? WHERE id=?",
                    (it["quantity"], base_full, base, pool, ln["id"]))
        _write_shares(con, ln["id"], shares, amounts)


def on_item_returned(con, receipt_id: str, item_id: int, return_id: int, qty: int, by: str | None) -> None:
    """Retur Penjualan of `qty` units of a line (called inside the return's own transaction). Sales: reversed pro rata.
    Teknisi: kept when that line's installation was Selesai, otherwise reversed. Each return is applied once."""
    work = con.execute("SELECT work_status FROM receipt_item_work WHERE receipt_item_id=?", (item_id,)).fetchone()
    installed = bool(work and work["work_status"] == "selesai")
    for ln in [dict(r) for r in con.execute(
            "SELECT * FROM incentive_lines WHERE receipt_item_id=? AND state='aktif'", (item_id,)).fetchall()]:
        if con.execute("SELECT 1 FROM incentive_reversals WHERE return_id=? AND line_id=?", (return_id, ln["id"])).fetchone():
            continue
        keep = ln["role"] == "teknisi" and installed
        policy = "pemasangan selesai, insentif teknisi dipertahankan" if keep else "dibalik sebanding qty retur"
        con.execute("INSERT INTO incentive_reversals (return_id, line_id, qty, kept, policy, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (return_id, ln["id"], qty, int(keep), policy, db._now(), by))
        _event(con, "retur", f"Retur {qty} unit: {policy}", by, receipt_id=receipt_id, line_id=ln["id"])
        if keep:
            continue
        excluded = min(ln["excluded_qty"] + qty, ln["line_qty"])
        con.execute("UPDATE incentive_lines SET excluded_qty=? WHERE id=?", (excluded, ln["id"]))
        if ln["approval_state"] == "disetujui":
            _, _, target = _target_amounts(ln["frozen_base_full"], ln["line_qty"], excluded, ln["rate_bp"], _current_shares(con, ln["id"]))
            _post_diff(con, ln, target, "reversal", by, f"Retur {qty} unit", return_id=return_id)
    recompute(con, receipt_id)


def on_receipt_deleted(con, receipt_id: str) -> None:
    if con.execute("SELECT 1 FROM incentive_entries WHERE receipt_id=?", (receipt_id,)).fetchone():
        raise ValueError("Insentif nota ini sudah disetujui, jadi nota tidak bisa dihapus. Void saja.")
    line_ids = [r["id"] for r in con.execute("SELECT id FROM incentive_lines WHERE receipt_id=?", (receipt_id,)).fetchall()]
    for lid in line_ids:
        con.execute("DELETE FROM incentive_shares WHERE line_id=?", (lid,))
    for table in ("incentive_lines", "receipt_item_work", "receipt_item_technicians", "incentive_events"):
        con.execute(f"DELETE FROM {table} WHERE receipt_id=?", (receipt_id,))
    con.execute("DELETE FROM incentive_requests WHERE receipt_id=?", (receipt_id,))


# ── status of a line ──────────────────────────────────────────────────────────

def _status(line: dict, policy: dict) -> tuple[str, str]:
    """(code, reason) for a line row joined with its nota (r_status, r_work_status) and item work (work_status)."""
    if line["state"] == "batal":
        return "batal", line.get("cancel_reason") or ""
    if line["approval_state"] == "disetujui":
        if line["line_qty"] and line["excluded_qty"] >= line["line_qty"]:
            return "batal", "Direversal retur"
        return "disetujui", ""
    if line["approval_state"] == "ditolak":
        return "ditolak", line.get("decide_note") or ""
    if line["line_qty"] and line["excluded_qty"] >= line["line_qty"]:
        return "batal", "Seluruh qty diretur"
    if not line.get("recipients"):
        return "belum_syarat", "Teknisi belum dipilih" if line["role"] == "teknisi" else "Sales belum dipilih"
    paid = line["r_status"] == "done"
    if line["role"] == "teknisi":
        done = line.get("work_status") == "selesai"
        need = policy["teknisi"]
        if line.get("work_status") == "batal":
            return "belum_syarat", "Pekerjaan item dibatalkan"
        ok = (done and paid) if need == "selesai_lunas" else (paid if need == "lunas" else done)
        missing = [m for m, cond in (("pekerjaan belum selesai", need != "lunas" and not done), ("nota belum lunas", need != "selesai" and not paid)) if cond]
    else:
        ok = paid if policy["sales"] == "lunas" else True
        missing = [] if ok else ["nota belum lunas"]
    return ("menunggu", "") if ok else ("estimasi", ", ".join(missing).capitalize())


_LINE_SELECT = """SELECT l.*, r.receipt_no, r.created_at AS r_created_at, r.status AS r_status, r.work_status AS r_work_status, r.total AS r_total,
        r.amount_paid AS r_amount_paid, r.plate_full, r.vehicle_make, r.vehicle_model, r.closed_at AS r_closed_at,
        ri.product_name, ri.quantity AS item_qty, ri.unit_price, ri.line_discount, ri.returned_qty,
        mi.sku, w.work_status, w.work_done_at, w.work_started_at
    FROM incentive_lines l
    JOIN receipts r ON r.id = l.receipt_id
    LEFT JOIN receipt_items ri ON ri.id = l.receipt_item_id
    LEFT JOIN master_items mi ON mi.id = l.master_item_id
    LEFT JOIN receipt_item_work w ON w.receipt_item_id = l.receipt_item_id"""


def _load_lines(con, where: str, args: tuple) -> list[dict]:
    lines = [dict(r) for r in con.execute(_LINE_SELECT + " WHERE " + where + " ORDER BY r.created_at, l.receipt_item_id, l.role DESC", args).fetchall()]
    if not lines:
        return lines
    ids = [ln["id"] for ln in lines]
    marks = ",".join("?" * len(ids))
    shares: dict[int, list] = {}
    for s in con.execute(f"""SELECT s.*, e.name FROM incentive_shares s JOIN employees e ON e.id = s.employee_id
                             WHERE s.line_id IN ({marks}) ORDER BY s.position""", ids).fetchall():
        shares.setdefault(s["line_id"], []).append(dict(s))
    held: dict[tuple, int] = {}
    kinds: dict[int, dict] = {}
    for e in con.execute(f"SELECT line_id, employee_id, kind, SUM(amount) AS s FROM incentive_entries WHERE line_id IN ({marks}) "
                         "GROUP BY line_id, employee_id, kind", ids).fetchall():
        held[(e["line_id"], e["employee_id"])] = held.get((e["line_id"], e["employee_id"]), 0) + e["s"]
        kinds.setdefault(e["line_id"], {}).setdefault(e["kind"], 0)
        kinds[e["line_id"]][e["kind"]] += e["s"]
    pending = {r["line_id"] for r in con.execute(
        f"SELECT line_id FROM incentive_requests WHERE state='menunggu' AND line_id IN ({marks})", ids).fetchall()}
    policy = get_policy(con)
    for ln in lines:
        ln["shares"] = shares.get(ln["id"], [])
        ln["recipients"] = [s["employee_id"] for s in ln["shares"]]
        if ln["approval_state"] == "disetujui" or ln["state"] == "batal":
            # Final money is the ledger; people who were removed later still show with their negative rows.
            seen = {s["employee_id"] for s in ln["shares"]}
            for (lid, emp), amt in held.items():
                if lid == ln["id"] and emp not in seen:
                    name = con.execute("SELECT name FROM employees WHERE id=?", (emp,)).fetchone()
                    ln["shares"].append({"line_id": lid, "employee_id": emp, "share_bp": 0, "amount": 0, "name": name["name"] if name else "?"})
            for s in ln["shares"]:
                s["final"] = held.get((ln["id"], s["employee_id"]), 0)
            ln["final_total"] = sum(s["final"] for s in ln["shares"])
        else:
            for s in ln["shares"]:
                s["final"] = None
            ln["final_total"] = None
        ln["entry_kinds"] = kinds.get(ln["id"], {})
        ln["status"], ln["status_reason"] = _status(ln, policy)
        ln["status_label"] = STATUS_LABELS[ln["status"]]
        ln["rate_label"] = bp_label(ln["rate_bp"])
        ln["role_label"] = ROLES[ln["role"]]
        ln["source_label"] = SOURCE_LABELS.get(ln["source"], ln["source"])
        if ln["rate_bp"] != ln["orig_rate_bp"]:
            ln["source_label"] += " · dikoreksi"
        ln["has_pending_request"] = ln["id"] in pending
        ln["effective_total"] = ln["final_total"] if ln["final_total"] is not None else (0 if ln["status"] in ("batal", "ditolak") else ln["pool_amount"])
        ln["nota_date"] = _local_day(ln["r_created_at"])
        ln["work_date"] = _local_day(ln["work_done_at"])
        ln["approve_date"] = _local_day(ln["approved_at"])
    return lines


# ── Master Insentif ───────────────────────────────────────────────────────────

def _version_out(v, today: str, used: dict) -> dict:
    d = dict(v)
    d["tech_rate_label"], d["sales_rate_label"] = bp_label(v["tech_rate_bp"]), bp_label(v["sales_rate_bp"])
    d["used_count"] = used.get(v["id"], 0)
    return d


def _rule_state(versions: list[dict], today: str) -> dict:
    live = [v for v in versions if not v["cancelled_at"]]
    current = next((v for v in sorted(live, key=lambda v: (v["effective_from"], v["id"]), reverse=True) if v["effective_from"] <= today), None)
    upcoming = sorted((v for v in live if v["effective_from"] > today), key=lambda v: v["effective_from"])
    if not live:
        state = "belum_ada"
    elif current is None:
        state = "terjadwal"
    elif current["is_active"] and (current["tech_rate_bp"] or current["sales_rate_bp"]):
        state = "aktif"
    else:
        state = "nonaktif"
    return {"state": state, "current": current, "upcoming": upcoming[0] if upcoming else None}


RULE_STATE_LABELS = {"aktif": "Aktif", "nonaktif": "Nonaktif", "terjadwal": "Terjadwal", "belum_ada": "Belum ada aturan"}


def list_rules(q: str | None = None, status: str | None = None, category_id: int | None = None, limit: int = 5000) -> dict:
    today = _today()
    with db._conn() as con:
        sql = """SELECT mi.id, mi.name, mi.sku, mi.unit, mi.sell_price, mi.category_id, c.name AS category_name, mi.is_active
                 FROM master_items mi LEFT JOIN categories c ON c.id = mi.category_id WHERE 1=1"""
        args: list = []
        if q:
            sql += " AND (mi.name LIKE ? OR mi.sku LIKE ? OR COALESCE(mi.manual_code,'') LIKE ?)"
            args += [f"%{q.strip()}%"] * 3
        if category_id:
            sql += " AND mi.category_id=?"
            args.append(category_id)
        items = [dict(r) for r in con.execute(sql + " ORDER BY mi.name COLLATE NOCASE", args).fetchall()]
        versions: dict[int, list] = {}
        for v in con.execute("SELECT * FROM incentive_rule_versions ORDER BY effective_from, id").fetchall():
            versions.setdefault(v["master_item_id"], []).append(dict(v))
        rows, kpi = [], {"aktif": 0, "teknisi": 0, "sales": 0, "tanpa": 0, "teknisi_rates": set(), "sales_rates": set()}
        for it in items:
            st = _rule_state(versions.get(it["id"], []), today)
            cur = st["current"]
            active = st["state"] == "aktif"
            if active:
                kpi["aktif"] += 1
                if cur["tech_rate_bp"]:
                    kpi["teknisi"] += 1
                    kpi["teknisi_rates"].add(cur["tech_rate_bp"])
                if cur["sales_rate_bp"]:
                    kpi["sales"] += 1
                    kpi["sales_rates"].add(cur["sales_rate_bp"])
            else:
                kpi["tanpa"] += 1
            if status and status != st["state"]:
                continue
            rows.append({**it, "rule_state": st["state"], "rule_state_label": RULE_STATE_LABELS[st["state"]],
                         "current": {k: cur[k] for k in ("id", "version_no", "effective_from", "tech_rate_bp", "sales_rate_bp", "is_active", "basis")} if cur else None,
                         "tech_rate_label": bp_label(cur["tech_rate_bp"]) if cur and cur["is_active"] else "—",
                         "sales_rate_label": bp_label(cur["sales_rate_bp"]) if cur and cur["is_active"] else "—",
                         "upcoming": {k: st["upcoming"][k] for k in ("version_no", "effective_from", "tech_rate_bp", "sales_rate_bp", "is_active")} | {
                             "tech_rate_label": bp_label(st["upcoming"]["tech_rate_bp"]), "sales_rate_label": bp_label(st["upcoming"]["sales_rate_bp"])}
                         if st["upcoming"] else None,
                         "version_count": len(versions.get(it["id"], []))})
        # Products with a rule first (aktif, terjadwal, nonaktif), then the rest of Master Barang.
        rank = {"aktif": 0, "terjadwal": 1, "nonaktif": 2, "belum_ada": 3}
        rows.sort(key=lambda r: (rank[r["rule_state"]], r["name"].lower()))
        kpi["teknisi_rates"] = " dan ".join(bp_label(b) for b in sorted(kpi["teknisi_rates"], reverse=True))
        kpi["sales_rates"] = " dan ".join(bp_label(b) for b in sorted(kpi["sales_rates"], reverse=True))
        return {"rows": rows[:limit], "total": len(rows), "kpi": kpi, "today": today}


def rule_detail(master_item_id: int) -> dict:
    today = _today()
    with db._conn() as con:
        item = con.execute("SELECT id, name, sku, unit, sell_price FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if item is None:
            raise LookupError("Barang tidak ditemukan.")
        vs = [dict(v) for v in con.execute("SELECT * FROM incentive_rule_versions WHERE master_item_id=? ORDER BY effective_from, id",
                                           (master_item_id,)).fetchall()]
        used = {r["rule_version_id"]: r["n"] for r in con.execute(
            "SELECT rule_version_id, COUNT(DISTINCT receipt_id) AS n FROM incentive_lines WHERE master_item_id=? GROUP BY rule_version_id",
            (master_item_id,)).fetchall()}
        st = _rule_state(vs, today)
        out = [_version_out(v, today, used) for v in vs]
        for v in out:
            if v["cancelled_at"]:
                v["note"] = "Dibatalkan"
            elif st["current"] and v["id"] == st["current"]["id"]:
                v["note"] = "Berlaku sekarang · untuk nota baru"
            elif v["effective_from"] > today:
                v["note"] = "Terjadwal · untuk nota baru"
            else:
                v["note"] = "Riwayat · snapshot nota lama"
        notas = [dict(r) for r in con.execute(
            """SELECT l.rule_version_id, l.role, l.rate_bp, l.base_amount, l.pool_amount, r.id AS receipt_id, r.receipt_no, r.created_at,
                      ri.unit_price, ri.quantity
               FROM incentive_lines l JOIN receipts r ON r.id=l.receipt_id LEFT JOIN receipt_items ri ON ri.id=l.receipt_item_id
               WHERE l.master_item_id=? AND l.state='aktif' ORDER BY r.created_at DESC LIMIT 20""", (master_item_id,)).fetchall()]
        for n in notas:
            n["rate_label"] = bp_label(n["rate_bp"])
        events = [dict(r) for r in con.execute("SELECT * FROM incentive_events WHERE master_item_id=? ORDER BY at DESC, id DESC LIMIT 50",
                                               (master_item_id,)).fetchall()]
        return {"item": dict(item), "versions": out, "state": st["state"], "state_label": RULE_STATE_LABELS[st["state"]],
                "current_id": st["current"]["id"] if st["current"] else None, "notas": notas, "events": events, "today": today}


def add_version(data: dict, by: str) -> dict:
    """A new version of a product's rule, starting on effective_from (today or later: a change never reaches back into
    notas already disahkan). Two live versions can't start on the same day."""
    master_item_id = data.get("master_item_id")
    reason = _text(data.get("reason"))
    if not reason or len(reason) < 5:
        raise ValueError("Alasan perubahan wajib diisi (minimal 5 karakter).")
    if len(reason) > 300:
        raise ValueError("Alasan maksimal 300 karakter.")
    try:
        eff = db.parse_ymd((data.get("effective_from") or "").strip()).isoformat()
    except ValueError:
        raise ValueError("Tanggal mulai berlaku tidak valid.")
    today = _today()
    if eff < today:
        raise ValueError("Tanggal mulai berlaku tidak boleh sebelum hari ini. Nota lama diubah lewat Koreksi Insentif.")
    if eff > (db.shop_today() + timedelta(days=366)).isoformat():
        raise ValueError("Tanggal mulai berlaku paling jauh satu tahun ke depan.")
    is_active = bool(data.get("is_active", True))
    tech = pct_to_bp(data.get("tech_rate")) if is_active else None
    sales = pct_to_bp(data.get("sales_rate")) if is_active else None
    if is_active and tech is None and sales is None:
        raise ValueError("Isi tarif teknisi dan/atau sales, atau pilih status Nonaktif.")
    with db._conn() as con:
        item = con.execute("SELECT id, name FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if item is None:
            raise ValueError("Pilih produk dari Master Barang.")
        rule = con.execute("SELECT * FROM incentive_rules WHERE master_item_id=?", (master_item_id,)).fetchone()
        if rule is None:
            if not is_active:
                raise ValueError("Produk ini belum punya aturan; tidak ada yang perlu dinonaktifkan.")
            rid = con.execute("INSERT INTO incentive_rules (master_item_id, created_at, created_by) VALUES (?, ?, ?)",
                              (master_item_id, db._now(), by)).lastrowid
        else:
            rid = rule["id"]
        if con.execute("SELECT 1 FROM incentive_rule_versions WHERE rule_id=? AND effective_from=? AND cancelled_at IS NULL", (rid, eff)).fetchone():
            raise ValueError(f"Sudah ada versi yang mulai berlaku {eff}. Batalkan versi itu dulu bila ingin menggantinya (periode tidak boleh bertabrakan).")
        prev = _version_on(con, master_item_id, eff)
        if prev is not None and bool(prev["is_active"]) == is_active and prev["tech_rate_bp"] == tech and prev["sales_rate_bp"] == sales:
            raise ValueError("Tarif dan status sama dengan versi yang berlaku pada tanggal itu; tidak ada yang berubah.")
        no = con.execute("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM incentive_rule_versions WHERE rule_id=?", (rid,)).fetchone()["n"]
        vid = con.execute("""INSERT INTO incentive_rule_versions (rule_id, master_item_id, version_no, effective_from, tech_rate_bp, sales_rate_bp,
                                 is_active, reason, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (rid, master_item_id, no, eff, tech, sales, int(is_active), reason, db._now(), by)).lastrowid
        before = (f"teknisi {bp_label(prev['tech_rate_bp'])}, sales {bp_label(prev['sales_rate_bp'])}" + ("" if prev["is_active"] else " (nonaktif)")) if prev else "tidak ada"
        after = f"teknisi {bp_label(tech)}, sales {bp_label(sales)}" if is_active else "nonaktif"
        _event(con, "versi", f"V{no} mulai {eff}: {before} -> {after}. Alasan: {reason}", by, master_item_id=master_item_id)
    return {"version_id": vid, **rule_detail(master_item_id)}


def cancel_version(version_id: int, reason: str | None, by: str) -> dict:
    reason = _text(reason)
    if not reason or len(reason) < 5:
        raise ValueError("Alasan pembatalan wajib diisi (minimal 5 karakter).")
    with db._conn() as con:
        v = con.execute("SELECT * FROM incentive_rule_versions WHERE id=?", (version_id,)).fetchone()
        if v is None:
            raise LookupError("Versi tidak ditemukan.")
        if v["cancelled_at"]:
            raise ValueError("Versi ini sudah dibatalkan.")
        if con.execute("SELECT 1 FROM incentive_lines WHERE rule_version_id=?", (version_id,)).fetchone():
            raise ValueError("Versi ini sudah dipakai nota (snapshot), jadi tidak bisa dibatalkan. Buat versi baru dengan tanggal mulai berikutnya.")
        con.execute("UPDATE incentive_rule_versions SET cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=?", (db._now(), by, reason, version_id))
        _event(con, "versi_batal", f"V{v['version_no']} (mulai {v['effective_from']}) dibatalkan: {reason}", by, master_item_id=v["master_item_id"])
        return rule_detail(v["master_item_id"])


# ── monitoring / rincian / detail invoice ─────────────────────────────────────

_BASIS_COL = {"nota": "date(r.created_at, ?)", "kerja": "date(w.work_done_at, ?)", "persetujuan": "date(l.approved_at, ?)"}
BASIS_LABELS = {"nota": "Tanggal nota", "kerja": "Tanggal pekerjaan selesai", "persetujuan": "Tanggal persetujuan"}


def _period_where(basis: str, date_from: str, date_to: str) -> tuple[str, list]:
    if basis not in _BASIS_COL:
        raise ValueError("Dasar periode tidak dikenal.")
    off = f"{'+' if db.SHOP_UTC_OFFSET_MINUTES >= 0 else '-'}{abs(db.SHOP_UTC_OFFSET_MINUTES)} minutes"
    col = _BASIS_COL[basis]
    return f"{col} BETWEEN ? AND ?", [off, date_from, date_to]


def _range(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    today = _today()
    try:
        f = db.parse_ymd(date_from).isoformat() if date_from else today
        t = db.parse_ymd(date_to).isoformat() if date_to else f
    except ValueError:
        raise ValueError("Tanggal tidak valid, gunakan format YYYY-MM-DD.")
    if f > t:
        f, t = t, f
    if (date.fromisoformat(t) - date.fromisoformat(f)).days > 370:
        raise ValueError("Rentang tanggal paling lama satu tahun.")
    return f, t


def _rows_for_employees(lines: list[dict]) -> list[dict]:
    """One row per line x person (the unit of every rincian)."""
    out = []
    for ln in lines:
        for s in ln["shares"]:
            amount = s["final"] if s["final"] is not None else (0 if ln["status"] in ("batal", "ditolak") else s["amount"])
            out.append({"line": ln, "employee_id": s["employee_id"], "name": s["name"], "share_bp": s["share_bp"], "amount": amount,
                        "computed": s["amount"]})
    return out


def monitoring(date_from=None, date_to=None, basis: str = "nota", employee_id: int | None = None, role: str | None = None,
               status: str | None = None) -> dict:
    f, t = _range(date_from, date_to)
    where, args = _period_where(basis, f, t)
    if role in ROLES:
        where += " AND l.role=?"
        args.append(role)
    with db._conn() as con:
        lines = _load_lines(con, where, tuple(args))
        pending_requests = con.execute("SELECT COUNT(*) AS n FROM incentive_requests WHERE state='menunggu'").fetchone()["n"]
    if status:
        lines = [ln for ln in lines if ln["status"] == status]
    rows = _rows_for_employees(lines)
    if employee_id:
        rows = [r for r in rows if r["employee_id"] == employee_id]
        lines = [ln for ln in lines if any(s["employee_id"] == employee_id for s in ln["shares"])]
    live = [ln for ln in lines if ln["status"] not in ("batal", "ditolak")]
    kpi = {
        "total": sum(ln["effective_total"] for ln in live),
        "teknisi": sum(ln["effective_total"] for ln in live if ln["role"] == "teknisi"),
        "sales": sum(ln["effective_total"] for ln in live if ln["role"] == "sales"),
        "menunggu": sum(ln["pool_amount"] for ln in lines if ln["status"] == "menunggu"),
        "menunggu_nota": len({ln["receipt_id"] for ln in lines if ln["status"] == "menunggu"}),
        "disetujui": sum(ln["final_total"] or 0 for ln in lines if ln["approval_state"] == "disetujui"),
        "koreksi_retur": sum(v for ln in lines for k, v in ln["entry_kinds"].items() if k in ("koreksi", "reversal", "batal")),
        "estimasi": sum(ln["pool_amount"] for ln in lines if ln["status"] in ("estimasi", "belum_syarat")),
        "nota_count": len({ln["receipt_id"] for ln in live}),
        "pending_requests": pending_requests,
    }
    people: dict[tuple, dict] = {}
    for r in rows:
        ln = r["line"]
        key = (r["employee_id"], ln["role"])
        p = people.setdefault(key, {"employee_id": r["employee_id"], "name": r["name"], "role": ln["role"], "role_label": ROLES[ln["role"]],
                                    "amount": 0, "sources": [], "statuses": set(), "notas": set(), "lines": 0})
        p["amount"] += r["amount"]
        p["lines"] += 1
        p["notas"].add(ln["receipt_no"])
        p["statuses"].add(ln["status"])
        short = (ln["product_name"] or "").split(" ")[0].title() if ln["product_name"] else "?"
        p["sources"].append(short + (f" {r['share_bp'] // 100}%" if ln["role"] == "teknisi" and len(ln["shares"]) > 1 else ""))
    order = ["menunggu", "estimasi", "belum_syarat", "disetujui", "ditolak", "batal"]
    employees = []
    for p in people.values():
        st = next((s for s in order if s in p["statuses"]), "estimasi")
        employees.append({**p, "statuses": sorted(p["statuses"]), "status": st, "status_label": STATUS_LABELS[st],
                          "notas": sorted(p["notas"]), "source_text": " + ".join(dict.fromkeys(p["sources"]))})
    employees.sort(key=lambda p: (p["role"] != "teknisi", -p["amount"], p["name"]))
    return {"from": f, "to": t, "basis": basis, "basis_label": BASIS_LABELS[basis], "kpi": kpi, "employees": employees,
            "lines": [_line_public(ln) for ln in lines]}


def _line_public(ln: dict) -> dict:
    keep = ("id", "receipt_id", "receipt_no", "receipt_item_id", "product_name", "sku", "role", "role_label", "rate_bp", "rate_label",
            "orig_rate_bp", "source", "source_label", "base_full", "base_amount", "pool_amount", "line_qty", "excluded_qty", "split_custom",
            "status", "status_label", "status_reason", "approval_state", "approved_by", "approved_at", "final_total", "effective_total",
            "work_status", "work_done_at", "nota_date", "work_date", "approve_date", "r_status", "r_work_status", "plate_full",
            "has_pending_request", "entry_kinds", "snapshot_date", "cancel_reason", "unit_price", "item_qty", "line_discount", "r_closed_at")
    d = {k: ln.get(k) for k in keep}
    d["shares"] = [{"employee_id": s["employee_id"], "name": s["name"], "share_bp": s["share_bp"], "share_label": bp_label(s["share_bp"]),
                    "amount": s["amount"], "final": s["final"]} for s in ln["shares"]]
    d["orig_rate_label"] = bp_label(ln["orig_rate_bp"])
    d["work_label"] = WORK_STATES.get(ln.get("work_status") or "", "")
    return d


def employee_detail(employee_id: int, date_from=None, date_to=None, basis: str = "nota", role: str | None = None) -> dict:
    f, t = _range(date_from, date_to)
    where, args = _period_where(basis, f, t)
    where += " AND l.id IN (SELECT line_id FROM incentive_shares WHERE employee_id=?)"
    args.append(employee_id)
    if role in ROLES:
        where += " AND l.role=?"
        args.append(role)
    with db._conn() as con:
        emp = con.execute("SELECT e.*, u.username FROM employees e LEFT JOIN users u ON u.id=e.user_id WHERE e.id=?", (employee_id,)).fetchone()
        if emp is None:
            raise LookupError("Karyawan tidak ditemukan.")
        lines = _load_lines(con, where, tuple(args))
        hist = {}
        if lines:
            marks = ",".join("?" * len(lines))
            for e in con.execute(f"SELECT * FROM incentive_events WHERE line_id IN ({marks}) ORDER BY at, id", [ln["id"] for ln in lines]).fetchall():
                hist.setdefault(e["line_id"], []).append(dict(e))
        sales_main = {}
        if lines:
            marks = ",".join("?" * len({ln["receipt_id"] for ln in lines}))
            for r in con.execute(f"""SELECT r.id, e.name AS sales_name, r.cashier_name FROM receipts r LEFT JOIN employees e ON e.id=r.sales_employee_id
                                     WHERE r.id IN ({marks})""", list({ln["receipt_id"] for ln in lines})).fetchall():
                sales_main[r["id"]] = dict(r)
    rows = []
    for ln in lines:
        mine = next((s for s in ln["shares"] if s["employee_id"] == employee_id), None)
        if mine is None:
            continue
        amount = mine["final"] if mine["final"] is not None else (0 if ln["status"] in ("batal", "ditolak") else mine["amount"])
        rows.append({**_line_public(ln), "my_share_bp": mine["share_bp"], "my_share_label": bp_label(mine["share_bp"]), "my_amount": amount,
                     "history": hist.get(ln["id"], []), "sales_main": (sales_main.get(ln["receipt_id"]) or {}).get("sales_name"),
                     "cashier_name": (sales_main.get(ln["receipt_id"]) or {}).get("cashier_name")})
    live = [r for r in rows if r["status"] not in ("batal", "ditolak")]
    statuses = {r["status"] for r in live}
    st = next((s for s in ("menunggu", "estimasi", "belum_syarat", "disetujui") if s in statuses), None)
    return {"employee": _employee_out(emp), "from": f, "to": t, "basis": basis, "rows": rows,
            "total": sum(r["my_amount"] for r in live), "eligible": len([r for r in live if r["status"] in ("menunggu", "disetujui")]),
            "nota_count": len({r["receipt_id"] for r in live}), "status": st, "status_label": STATUS_LABELS.get(st, "Belum ada")}


def invoice_detail(receipt_id: str) -> dict:
    with db._conn() as con:
        rec = con.execute(db._RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        if rec is None:
            raise LookupError("Nota tidak ditemukan.")
        lines = _load_lines(con, "l.receipt_id=?", (receipt_id,))
        events = [dict(r) for r in con.execute("SELECT * FROM incentive_events WHERE receipt_id=? ORDER BY at, id", (receipt_id,)).fetchall()]
        entries = [dict(r) for r in con.execute("""SELECT x.*, e.name FROM incentive_entries x JOIN employees e ON e.id=x.employee_id
                                                   WHERE x.receipt_id=? ORDER BY x.created_at, x.id""", (receipt_id,)).fetchall()]
        requests = [dict(r) for r in con.execute("SELECT * FROM incentive_requests WHERE receipt_id=? ORDER BY requested_at DESC", (receipt_id,)).fetchall()]
        reversals = [dict(r) for r in con.execute("""SELECT v.*, l.role, ri.product_name FROM incentive_reversals v JOIN incentive_lines l ON l.id=v.line_id
                                                     LEFT JOIN receipt_items ri ON ri.id=l.receipt_item_id WHERE l.receipt_id=? ORDER BY v.id""",
                                                  (receipt_id,)).fetchall()]
        last_pay = con.execute("SELECT MAX(paid_at) AS at FROM receipt_payments WHERE receipt_id=? AND state='sah'", (receipt_id,)).fetchone()["at"]
        work_done = con.execute("SELECT MAX(work_done_at) AS at FROM receipt_item_work WHERE receipt_id=?", (receipt_id,)).fetchone()["at"]
        sales = con.execute("SELECT name FROM employees WHERE id=?", (rec["sales_employee_id"],)).fetchone() if rec["sales_employee_id"] else None
        items_without = [dict(r) for r in con.execute(
            """SELECT ri.id, ri.product_name, ri.item_type, ri.master_item_id FROM receipt_items ri WHERE ri.receipt_id=?
               AND ri.id NOT IN (SELECT receipt_item_id FROM incentive_lines WHERE receipt_id=?)""", (receipt_id, receipt_id)).fetchall()]
    for e in entries:
        e["kind_label"] = ENTRY_LABELS.get(e["kind"], e["kind"])
    pub = [_line_public(ln) for ln in lines]
    live = [ln for ln in pub if ln["status"] not in ("batal", "ditolak")]
    return {"receipt": {"id": rec["id"], "receipt_no": rec["receipt_no"], "plate_full": rec["plate_full"], "vehicle_make": rec["vehicle_make"],
                        "vehicle_model": rec["vehicle_model"], "status": rec["status"], "work_status": rec["work_status"], "total": rec["total"],
                        "amount_paid": rec["amount_paid"], "created_at": rec["created_at"], "closed_at": rec["closed_at"],
                        "last_paid_at": last_pay, "work_done_at": work_done, "sales_name": sales["name"] if sales else None,
                        "cashier_name": rec["cashier_name"], "customer_name": rec["customer_full_name"]},
            "lines": pub, "events": events, "entries": entries, "requests": requests, "reversals": reversals, "items_without_rule": items_without,
            "pool_teknisi": sum(ln["effective_total"] for ln in live if ln["role"] == "teknisi"),
            "pool_sales": sum(ln["effective_total"] for ln in live if ln["role"] == "sales"),
            "total": sum(ln["effective_total"] for ln in live),
            "approvable": [ln["id"] for ln in pub if ln["status"] == "menunggu"]}


# ── admin actions: split, approve, reject, corrections ───────────────────────

def _line_for_update(con, line_id: int):
    ln = con.execute("SELECT * FROM incentive_lines WHERE id=?", (line_id,)).fetchone()
    if ln is None:
        raise LookupError("Baris insentif tidak ditemukan.")
    return dict(ln)


def _parse_shares(shares: dict, recips: list[int]) -> list[tuple[int, int]]:
    out = []
    for emp in recips:
        raw = shares.get(str(emp), shares.get(emp))
        bp = pct_to_bp(raw)
        if bp is None:
            raise ValueError("Isi porsi setiap teknisi (lebih dari 0%).")
        out.append((emp, bp))
    extra = {int(k) for k in shares} - set(recips)
    if extra:
        raise ValueError("Porsi hanya untuk teknisi yang tercatat pada item ini.")
    total = sum(bp for _, bp in out)
    if total != BP:
        raise ValueError(f"Total porsi harus tepat 100% (sekarang {bp_label(total)}).")
    return out


def set_split(line_id: int, shares: dict, by: str) -> dict:
    with db._conn() as con:
        ln = _line_for_update(con, line_id)
        if ln["role"] != "teknisi":
            raise ValueError("Porsi hanya diatur untuk insentif teknisi.")
        if ln["state"] != "aktif":
            raise ValueError("Baris insentif ini sudah dibatalkan.")
        if ln["approval_state"] == "disetujui":
            raise ValueError("Insentif ini sudah disetujui. Perubahan porsi diajukan lewat Koreksi.")
        recips = [r["employee_id"] for r in con.execute(
            "SELECT employee_id FROM receipt_item_technicians WHERE receipt_item_id=? ORDER BY position, id", (ln["receipt_item_id"],)).fetchall()]
        if len(recips) < 2:
            raise ValueError("Porsi hanya bisa diatur bila item dikerjakan lebih dari satu teknisi.")
        parsed = _parse_shares(shares, recips)
        names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM employees").fetchall()}
        con.execute("DELETE FROM incentive_shares WHERE line_id=?", (line_id,))
        for pos, (emp, bp) in enumerate(parsed):
            con.execute("INSERT INTO incentive_shares (line_id, employee_id, share_bp, amount, position) VALUES (?, ?, ?, 0, ?)", (line_id, emp, bp, pos))
        con.execute("UPDATE incentive_lines SET split_custom=1 WHERE id=?", (line_id,))
        _event(con, "porsi", "Porsi diatur: " + ", ".join(f"{names.get(e, e)} {bp_label(b)}" for e, b in parsed), by,
               receipt_id=ln["receipt_id"], line_id=line_id)
        recompute(con, ln["receipt_id"])
        receipt_id = ln["receipt_id"]
    return invoice_detail(receipt_id)


def approve_lines(line_ids: list[int], by: str, note: str | None = None) -> dict:
    if not line_ids:
        raise ValueError("Pilih baris insentif yang akan disetujui.")
    receipts = set()
    with db._conn() as con:
        loaded = {ln["id"]: ln for ln in _load_lines(con, f"l.id IN ({','.join('?' * len(line_ids))})", tuple(line_ids))}
        for lid in line_ids:
            ln = loaded.get(lid)
            if ln is None:
                raise LookupError("Baris insentif tidak ditemukan.")
            if ln["status"] != "menunggu":
                raise ValueError(f"{ln['product_name']} ({ROLES[ln['role']]}) belum bisa disetujui: {ln['status_label']}"
                                 + (f" — {ln['status_reason']}" if ln["status_reason"] else "") + ".")
            if ln["has_pending_request"]:
                raise ValueError(f"{ln['product_name']} ({ROLES[ln['role']]}) masih punya pengajuan koreksi yang menunggu. Putuskan dulu.")
        now = db._now()
        for lid in line_ids:
            ln = loaded[lid]
            cur = con.execute("""UPDATE incentive_lines SET approval_state='disetujui', approved_by=?, approved_at=?, decide_note=?, frozen_base_full=base_full
                                 WHERE id=? AND state='aktif' AND approval_state IS NULL""", (by, now, _text(note, 300), lid))
            if cur.rowcount == 0:
                raise ValueError("Baris insentif baru saja berubah. Muat ulang lalu coba lagi.")
            for s in ln["shares"]:
                if s["amount"]:
                    con.execute("""INSERT INTO incentive_entries (line_id, receipt_id, employee_id, role, kind, amount, note, created_by, created_at)
                                   VALUES (?, ?, ?, ?, 'persetujuan', ?, ?, ?, ?)""",
                                (lid, ln["receipt_id"], s["employee_id"], ln["role"], s["amount"], _text(note, 300), by, now))
            _event(con, "setuju", f"Disetujui: {db_rp(ln['pool_amount'])} ({ln['rate_label']} dari {db_rp(ln['base_amount'])})", by,
                   receipt_id=ln["receipt_id"], line_id=lid)
            receipts.add(ln["receipt_id"])
    return {"approved": len(line_ids), "receipts": sorted(receipts)}


def reject_lines(line_ids: list[int], reason: str | None, by: str) -> dict:
    reason = _text(reason)
    if not reason or len(reason) < 5:
        raise ValueError("Alasan penolakan wajib diisi (minimal 5 karakter).")
    with db._conn() as con:
        for lid in line_ids:
            ln = _line_for_update(con, lid)
            if ln["state"] != "aktif" or ln["approval_state"]:
                raise ValueError("Hanya insentif yang belum diputuskan yang bisa ditolak.")
            con.execute("UPDATE incentive_lines SET approval_state='ditolak', approved_by=?, approved_at=?, decide_note=? WHERE id=?",
                        (by, db._now(), reason, lid))
            _event(con, "tolak", f"Ditolak: {reason}", by, receipt_id=ln["receipt_id"], line_id=lid)
    return {"rejected": len(line_ids)}


def reopen_line(line_id: int, reason: str | None, by: str) -> dict:
    reason = _text(reason)
    if not reason or len(reason) < 5:
        raise ValueError("Alasan wajib diisi (minimal 5 karakter).")
    with db._conn() as con:
        ln = _line_for_update(con, line_id)
        if ln["approval_state"] != "ditolak":
            raise ValueError("Hanya insentif yang ditolak yang bisa dibuka kembali.")
        con.execute("UPDATE incentive_lines SET approval_state=NULL, approved_by=NULL, approved_at=NULL, decide_note=NULL WHERE id=?", (line_id,))
        _event(con, "buka", f"Penolakan dibatalkan: {reason}", by, receipt_id=ln["receipt_id"], line_id=line_id)
        recompute(con, ln["receipt_id"])
        return invoice_detail(ln["receipt_id"])


def db_rp(n: int | None) -> str:
    return "Rp " + f"{int(n or 0):,}".replace(",", ".")


def _names(con) -> dict[int, str]:
    return {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM employees").fetchall()}


def create_request(data: dict, by: str) -> dict:
    """Koreksi Insentif (Admin): a new rate and/or split for one line of an old nota, or an incentive for a line that
    had none ('tambah'). Nothing changes until an Owner approves it; then the line changes and, if it was already
    approved, the difference is written as new ledger rows."""
    reason = _text(data.get("reason"))
    if not reason or len(reason) < 5:
        raise ValueError("Alasan koreksi wajib diisi (minimal 5 karakter).")
    if len(reason) > 300:
        raise ValueError("Alasan maksimal 300 karakter.")
    new_bp = pct_to_bp(data.get("new_rate"))
    with db._conn() as con:
        names = _names(con)
        if data.get("line_id"):
            ln = _line_for_update(con, int(data["line_id"]))
            if ln["state"] != "aktif":
                raise ValueError("Baris insentif ini sudah dibatalkan.")
            if con.execute("SELECT 1 FROM incentive_requests WHERE line_id=? AND state='menunggu'", (ln["id"],)).fetchone():
                raise ValueError("Baris ini masih punya pengajuan koreksi yang menunggu.")
            loaded = _load_lines(con, "l.id=?", (ln["id"],))[0]
            new_bp = new_bp if new_bp is not None else ln["rate_bp"]
            cur_shares = _current_shares(con, ln["id"])
            recips = [e for e, _ in cur_shares] or _recipients(con, ln, con.execute("SELECT * FROM receipts WHERE id=?", (ln["receipt_id"],)).fetchone(),
                                                                  dict(con.execute("SELECT * FROM receipt_item_work WHERE receipt_item_id=?", (ln["receipt_item_id"],)).fetchone() or {}))
            shares = _parse_shares(data["shares"], recips) if data.get("shares") else (cur_shares or list(zip(recips, _equal_bps(len(recips)))))
            if new_bp == ln["rate_bp"] and shares == cur_shares:
                raise ValueError("Tidak ada perubahan tarif atau porsi.")
            base_full = ln["frozen_base_full"] if ln["approval_state"] == "disetujui" else ln["base_full"]
            _, old_pool, old_amounts = _target_amounts(base_full, ln["line_qty"], ln["excluded_qty"], ln["rate_bp"], cur_shares)
            _, new_pool, new_amounts = _target_amounts(base_full, ln["line_qty"], ln["excluded_qty"], new_bp, shares)
            if ln["approval_state"] == "disetujui":
                old_amounts = {s["employee_id"]: s["final"] for s in loaded["shares"]}
                old_pool = sum(old_amounts.values())
            kind, receipt_id, item_id, role, line_id, old_bp = "koreksi", ln["receipt_id"], ln["receipt_item_id"], ln["role"], ln["id"], ln["rate_bp"]
        else:
            item_id, role = int(data.get("receipt_item_id") or 0), data.get("role")
            if role not in ROLES:
                raise ValueError("Pilih peran insentif.")
            if new_bp is None:
                raise ValueError("Isi tarif yang diusulkan.")
            item = con.execute("SELECT * FROM receipt_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise LookupError("Item nota tidak ditemukan.")
            rec = con.execute("SELECT * FROM receipts WHERE id=?", (item["receipt_id"],)).fetchone()
            if rec["status"] == "void" or rec["work_status"] == "draft":
                raise ValueError("Nota ini void atau masih draft.")
            if con.execute("SELECT 1 FROM incentive_lines WHERE receipt_item_id=? AND role=?", (item_id, role)).fetchone():
                raise ValueError("Item ini sudah punya insentif untuk peran itu; ajukan koreksi pada barisnya.")
            if con.execute("SELECT 1 FROM incentive_requests WHERE receipt_item_id=? AND role=? AND state='menunggu'", (item_id, role)).fetchone():
                raise ValueError("Sudah ada pengajuan yang menunggu untuk item dan peran ini.")
            work = con.execute("SELECT * FROM receipt_item_work WHERE receipt_item_id=?", (item_id,)).fetchone()
            recips = _recipients(con, {"role": role, "receipt_item_id": item_id}, rec, dict(work) if work else None)
            if not recips:
                raise ValueError("Pilih " + ("teknisi" if role == "teknisi" else "sales") + " pada item ini dulu (di Kasir).")
            shares = _parse_shares(data["shares"], recips) if data.get("shares") else list(zip(recips, _equal_bps(len(recips))))
            items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (rec["id"],)).fetchall()]
            base_full = max(db._line_total(dict(item)) - _discount_alloc(items, rec["discount"] or 0).get(item_id, 0), 0)
            excluded = item["returned_qty"] if role == "sales" else 0
            old_pool, old_amounts = 0, {}
            _, new_pool, new_amounts = _target_amounts(base_full, item["quantity"], excluded, new_bp, shares)
            kind, receipt_id, line_id, old_bp = "tambah", rec["id"], None, None
        before = [{"employee_id": e, "name": names.get(e, "?"), "amount": a} for e, a in old_amounts.items()]
        after = [{"employee_id": e, "name": names.get(e, "?"), "share_bp": b, "share_label": bp_label(b), "amount": new_amounts.get(e, 0)} for e, b in shares]
        rid = con.execute("""INSERT INTO incentive_requests (kind, line_id, receipt_id, receipt_item_id, role, old_rate_bp, new_rate_bp, old_pool, new_pool,
                                 shares_json, before_json, after_json, reason, requested_by, requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (kind, line_id, receipt_id, item_id, role, old_bp, new_bp, old_pool, new_pool, json.dumps(shares), json.dumps(before),
                           json.dumps(after), reason, by, db._now())).lastrowid
        _event(con, "ajukan", f"Koreksi diajukan: {bp_label(old_bp)} -> {bp_label(new_bp)}, pool {db_rp(old_pool)} -> {db_rp(new_pool)}. {reason}", by,
               receipt_id=receipt_id, line_id=line_id)
    return get_request(rid)


def _request_out(r) -> dict:
    d = dict(r)
    d["before"], d["after"], d["shares"] = json.loads(r["before_json"] or "[]"), json.loads(r["after_json"] or "[]"), json.loads(r["shares_json"] or "[]")
    d["old_rate_label"], d["new_rate_label"] = bp_label(r["old_rate_bp"]), bp_label(r["new_rate_bp"])
    d["diff"] = r["new_pool"] - r["old_pool"]
    d["role_label"] = ROLES.get(r["role"], r["role"])
    return d


def get_request(request_id: int) -> dict:
    with db._conn() as con:
        r = con.execute("""SELECT q.*, rc.receipt_no, ri.product_name, ri.unit_price, ri.quantity FROM incentive_requests q
                           JOIN receipts rc ON rc.id=q.receipt_id LEFT JOIN receipt_items ri ON ri.id=q.receipt_item_id WHERE q.id=?""", (request_id,)).fetchone()
        if r is None:
            raise LookupError("Pengajuan tidak ditemukan.")
        return _request_out(r)


def list_requests(state: str | None = None, limit: int = 200) -> list[dict]:
    sql = """SELECT q.*, rc.receipt_no, ri.product_name, ri.unit_price, ri.quantity FROM incentive_requests q
             JOIN receipts rc ON rc.id=q.receipt_id LEFT JOIN receipt_items ri ON ri.id=q.receipt_item_id"""
    args: list = []
    if state:
        sql += " WHERE q.state=?"
        args.append(state)
    with db._conn() as con:
        return [_request_out(r) for r in con.execute(sql + " ORDER BY q.state='menunggu' DESC, q.requested_at DESC LIMIT ?", (*args, limit)).fetchall()]


def decide_request(request_id: int, approve: bool, note: str | None, by: str) -> dict:
    note = _text(note, 300)
    if not approve and (not note or len(note) < 5):
        raise ValueError("Alasan penolakan wajib diisi (minimal 5 karakter).")
    with db._conn() as con:
        q = con.execute("SELECT * FROM incentive_requests WHERE id=?", (request_id,)).fetchone()
        if q is None:
            raise LookupError("Pengajuan tidak ditemukan.")
        if q["state"] != "menunggu":
            raise ValueError("Pengajuan ini sudah diputuskan.")
        now = db._now()
        cur = con.execute("UPDATE incentive_requests SET state=?, decided_by=?, decided_at=?, decide_note=? WHERE id=? AND state='menunggu'",
                          ("disetujui" if approve else "ditolak", by, now, note, request_id))
        if cur.rowcount == 0:
            raise ValueError("Pengajuan ini baru saja diputuskan orang lain.")
        shares = [(int(e), int(b)) for e, b in json.loads(q["shares_json"] or "[]")]
        if not approve:
            _event(con, "koreksi_tolak", f"Koreksi ditolak: {note}", by, receipt_id=q["receipt_id"], line_id=q["line_id"])
        elif q["kind"] == "tambah":
            if con.execute("SELECT 1 FROM incentive_lines WHERE receipt_item_id=? AND role=?", (q["receipt_item_id"], q["role"])).fetchone():
                raise ValueError("Item ini sudah punya insentif untuk peran itu.")
            item = con.execute("SELECT * FROM receipt_items WHERE id=?", (q["receipt_item_id"],)).fetchone()
            if item is None:
                raise ValueError("Item nota sudah tidak ada.")
            line_id = con.execute("""INSERT INTO incentive_lines (receipt_id, receipt_item_id, master_item_id, role, rule_version_id, rate_bp, orig_rate_bp,
                                         source, snapshot_at, snapshot_date, line_qty, excluded_qty, split_custom)
                                     VALUES (?, ?, ?, ?, NULL, ?, ?, 'koreksi', ?, ?, ?, ?, ?)""",
                                  (q["receipt_id"], q["receipt_item_id"], item["master_item_id"], q["role"], q["new_rate_bp"], q["new_rate_bp"],
                                   now, _today(), item["quantity"], item["returned_qty"] if q["role"] == "sales" else 0, int(len(shares) > 1))).lastrowid
            _write_shares(con, line_id, shares, {})
            con.execute("UPDATE incentive_requests SET line_id=? WHERE id=?", (line_id, request_id))
            _event(con, "koreksi_setuju", f"Insentif ditambahkan lewat koreksi: {bp_label(q['new_rate_bp'])}. {q['reason']}", by,
                   receipt_id=q["receipt_id"], line_id=line_id)
            recompute(con, q["receipt_id"])
        else:
            ln = _line_for_update(con, q["line_id"])
            if ln["state"] != "aktif":
                raise ValueError("Baris insentif ini sudah dibatalkan.")
            con.execute("UPDATE incentive_lines SET rate_bp=?, split_custom=? WHERE id=?", (q["new_rate_bp"], int(len(shares) > 1), ln["id"]))
            if ln["approval_state"] == "disetujui":
                _, pool, target = _target_amounts(ln["frozen_base_full"], ln["line_qty"], ln["excluded_qty"], q["new_rate_bp"], shares)
                _write_shares(con, ln["id"], shares, target)
                con.execute("UPDATE incentive_lines SET pool_amount=? WHERE id=?", (pool, ln["id"]))
                diff = _post_diff(con, ln, target, "koreksi", by, q["reason"], request_id=request_id)
                text = f"Koreksi disetujui: penyesuaian {'+' if diff >= 0 else '-'}{db_rp(abs(diff))}"
            else:
                _write_shares(con, ln["id"], shares, {})
                recompute(con, q["receipt_id"])
                text = "Koreksi disetujui sebelum approval: tarif/porsi baris diganti"
            _event(con, "koreksi_setuju", f"{text} ({bp_label(q['old_rate_bp'])} -> {bp_label(q['new_rate_bp'])}). {q['reason']}", by,
                   receipt_id=q["receipt_id"], line_id=ln["id"])
    return get_request(request_id)


def journal(limit: int = 300) -> dict:
    """Every ledger row that is not a plain approval: corrections, return reversals, cancellations."""
    with db._conn() as con:
        rows = con.execute("""SELECT x.*, e.name, r.receipt_no, ri.product_name, l.rate_bp FROM incentive_entries x
                              JOIN employees e ON e.id=x.employee_id JOIN receipts r ON r.id=x.receipt_id
                              JOIN incentive_lines l ON l.id=x.line_id LEFT JOIN receipt_items ri ON ri.id=l.receipt_item_id
                              WHERE x.kind<>'persetujuan' ORDER BY x.created_at DESC, x.id DESC LIMIT ?""", (limit,)).fetchall()
        rev = con.execute("""SELECT v.*, l.role, l.receipt_id, r.receipt_no, ri.product_name FROM incentive_reversals v
                             JOIN incentive_lines l ON l.id=v.line_id JOIN receipts r ON r.id=l.receipt_id
                             LEFT JOIN receipt_items ri ON ri.id=l.receipt_item_id ORDER BY v.id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind_label"], d["role_label"], d["rate_label"] = ENTRY_LABELS.get(r["kind"], r["kind"]), ROLES.get(r["role"], r["role"]), bp_label(r["rate_bp"])
        out.append(d)
    return {"entries": out, "reversals": [dict(r) | {"role_label": ROLES.get(r["role"], r["role"])} for r in rev]}


# ── Proses Pengerjaan (any user, no money) ────────────────────────────────────

def _work_rows(con, receipt_ids: list[str]) -> dict[str, list]:
    if not receipt_ids:
        return {}
    marks = ",".join("?" * len(receipt_ids))
    out: dict[str, list] = {}
    techs: dict[int, list] = {}
    for t in con.execute(f"""SELECT t.receipt_item_id, e.id, e.name FROM receipt_item_technicians t JOIN employees e ON e.id=t.employee_id
                             WHERE t.receipt_id IN ({marks}) ORDER BY t.position, t.id""", receipt_ids).fetchall():
        techs.setdefault(t["receipt_item_id"], []).append({"id": t["id"], "name": t["name"]})
    for r in con.execute(f"""SELECT ri.id, ri.receipt_id, ri.product_name, ri.quantity, ri.item_type, ri.unit_price, ri.line_discount,
                                    ri.added_at, ri.added_by, ri.added_during_work, ri.returned_qty, mi.sku,
                                    w.work_status, w.work_started_at, w.work_done_at, w.work_done_by, w.sales_employee_id, e.name AS sales_name
                             FROM receipt_items ri LEFT JOIN master_items mi ON mi.id=ri.master_item_id
                             LEFT JOIN receipt_item_work w ON w.receipt_item_id=ri.id LEFT JOIN employees e ON e.id=w.sales_employee_id
                             WHERE ri.receipt_id IN ({marks}) ORDER BY ri.id""", receipt_ids).fetchall():
        d = dict(r)
        d["technicians"] = techs.get(r["id"], [])
        d["work_label"] = WORK_STATES.get(r["work_status"] or "", "")
        out.setdefault(r["receipt_id"], []).append(d)
    return out


def work_orders(scope: str = "aktif", q: str | None = None) -> dict:
    sql = db._RECEIPT_SELECT + " WHERE r.status<>'void' AND r.work_status<>'draft'"
    args: list = []
    if scope == "aktif":
        sql += " AND NOT (r.status='done' AND r.work_status='selesai')"
    elif scope == "selesai":
        sql += " AND r.work_status='selesai'"
    if q:
        sql += " AND (r.plate_full LIKE ? OR r.receipt_no LIKE ? OR r.customer_name LIKE ? OR c.first_name LIKE ?)"
        args += [f"%{q.strip()}%"] * 4
    sql += " ORDER BY r.created_at DESC LIMIT 200"
    with db._conn() as con:
        recs = [dict(r) for r in con.execute(sql, args).fetchall()]
        rows = _work_rows(con, [r["id"] for r in recs])
        sales = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM employees").fetchall()}
    out = []
    for r in recs:
        items = rows.get(r["id"], [])
        jobs = [i for i in items if i["technicians"]]
        if not r.get("has_job"):
            continue  # a walk-in purchase has no work to follow
        out.append({"id": r["id"], "receipt_no": r["receipt_no"], "plate_full": r["plate_full"], "vehicle_make": r["vehicle_make"],
                    "vehicle_model": r["vehicle_model"], "customer_name": r["customer_full_name"], "total": r["total"],
                    "amount_paid": r["amount_paid"], "status": r["status"], "work_status": r["work_status"], "created_at": r["created_at"],
                    "item_count": len(items), "job_count": len(jobs), "jobs_done": len([j for j in jobs if j["work_status"] == "selesai"]),
                    "technicians": sorted({t["name"] for j in jobs for t in j["technicians"]}),
                    "sales_name": sales.get(r["sales_employee_id"])})
    counts = {"antrian": 0, "dikerjakan": 0, "selesai": 0}
    for items in rows.values():
        for i in items:
            if i["technicians"] and i["work_status"] in counts:
                counts[i["work_status"]] += 1
    return {"rows": out, "counts": counts}


def work_order_detail(receipt_id: str) -> dict:
    with db._conn() as con:
        rec = con.execute(db._RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        if rec is None:
            raise LookupError("Nota tidak ditemukan.")
        items = _work_rows(con, [receipt_id]).get(receipt_id, [])
        pays = [dict(p) for p in con.execute("SELECT * FROM receipt_payments WHERE receipt_id=? ORDER BY paid_at, id", (receipt_id,)).fetchall()]
        sales = con.execute("SELECT name FROM employees WHERE id=?", (rec["sales_employee_id"],)).fetchone() if rec["sales_employee_id"] else None
        events = [dict(e) for e in con.execute("SELECT * FROM receipt_events WHERE receipt_id=? AND kind IN ('created','item_work','status','assign','item_added','payment') "
                                               "ORDER BY at DESC, id DESC LIMIT 40", (receipt_id,)).fetchall()]
    r = dict(rec)
    return {"receipt": {k: r.get(k) for k in ("id", "receipt_no", "plate_full", "vehicle_make", "vehicle_model", "vehicle_color", "vehicle_year",
                                               "customer_full_name", "customer_phone", "total", "amount_paid", "status", "work_status", "work_stage",
                                               "created_at", "closed_at", "pending_amount", "payment_method", "handed_over_at", "cashier_name",
                                               "work_notes", "return_total")}
            | {"sales_name": sales["name"] if sales else None},
            "items": items, "payments": pays, "events": events}
