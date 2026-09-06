import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent.parent / "data" / "scraper.db")))

LOW_STOCK_THRESHOLD = 5

_STOPWORDS = {"DAN", "THE", "OF", "FOR", "DENGAN", "UNTUK"}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        # WAL mode requires shared-memory mmap between the .db/-wal/-shm files, which
        # breaks across the host/container filesystem boundary on Docker Desktop for
        # Mac bind mounts (observed as sqlite3.OperationalError: disk I/O error as soon
        # as anything touches the file from outside the running container). The
        # rollback journal has no such requirement and this app has no concurrent-writer
        # throughput need that would justify the tradeoff.
        con.execute("PRAGMA journal_mode=DELETE")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                keyword TEXT NOT NULL,
                city TEXT NOT NULL,
                kecamatan_list TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                name TEXT,
                address TEXT,
                phone TEXT,
                lat REAL,
                lng REAL
            );
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                created_at TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY,
                plate_region TEXT NOT NULL,
                plate_number TEXT NOT NULL,
                plate_suffix TEXT NOT NULL,
                plate_full TEXT NOT NULL,
                customer_phone TEXT NOT NULL DEFAULT '',
                customer_name TEXT,
                subtotal INTEGER NOT NULL,
                discount INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_plate_full ON receipts(plate_full);
            CREATE INDEX IF NOT EXISTS idx_receipts_created_at ON receipts(created_at);
            CREATE TABLE IF NOT EXISTS receipt_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL REFERENCES receipts(id),
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                warranty_date TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt_id ON receipt_items(receipt_id);

            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_units_name_normalized ON units(name_normalized);
            CREATE TABLE IF NOT EXISTS master_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE,
                sku_prefix TEXT NOT NULL,
                sku_seq INTEGER NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                brand_id INTEGER NOT NULL REFERENCES brands(id),
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                unit TEXT NOT NULL,
                last_cost_price INTEGER NOT NULL DEFAULT 0,
                avg_cost_price INTEGER NOT NULL DEFAULT 0,
                sell_price INTEGER NOT NULL DEFAULT 0,
                stock_qty INTEGER NOT NULL DEFAULT 0,
                first_received_date TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(supplier_id, brand_id, name_normalized)
            );
            CREATE INDEX IF NOT EXISTS idx_master_items_sku_prefix ON master_items(sku_prefix);
            CREATE INDEX IF NOT EXISTS idx_master_items_name_normalized ON master_items(name_normalized);
            CREATE TABLE IF NOT EXISTS inbound_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_number TEXT NOT NULL UNIQUE,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                received_date TEXT NOT NULL,
                supplier_invoice_no TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                total_value INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_inbound_documents_status ON inbound_documents(status);
            CREATE TABLE IF NOT EXISTS inbound_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES inbound_documents(id),
                master_item_id INTEGER REFERENCES master_items(id),
                pending_brand_id INTEGER REFERENCES brands(id),
                pending_product_name TEXT,
                pending_unit TEXT,
                pending_sell_price INTEGER,
                qty_in INTEGER NOT NULL,
                cost_price INTEGER NOT NULL,
                qty_remaining INTEGER,
                received_date TEXT,
                UNIQUE(document_id, master_item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_inbound_items_document_id ON inbound_items(document_id);
            CREATE INDEX IF NOT EXISTS idx_inbound_items_master_item_id ON inbound_items(master_item_id);
            CREATE TABLE IF NOT EXISTS stock_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_item_id INTEGER NOT NULL REFERENCES master_items(id),
                type TEXT NOT NULL,
                qty INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                ref_type TEXT,
                ref_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stock_ledger_master_item_id ON stock_ledger(master_item_id);

            CREATE TABLE IF NOT EXISTS purchase_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                supplier_invoice_no TEXT,
                supplier_name_snapshot TEXT NOT NULL,
                supplier_phone_snapshot TEXT,
                supplier_address_snapshot TEXT,
                supplier_contact_snapshot TEXT,
                invoice_date TEXT NOT NULL,
                payment_terms TEXT NOT NULL DEFAULT 'NET 30',
                due_date TEXT NOT NULL,
                subtotal INTEGER NOT NULL DEFAULT 0,
                discount INTEGER NOT NULL DEFAULT 0,
                tax_rate REAL NOT NULL DEFAULT 0,
                tax_amount INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                amount_paid INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','void')),
                notes TEXT,
                void_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_purchase_invoices_status ON purchase_invoices(status);
            CREATE INDEX IF NOT EXISTS idx_purchase_invoices_supplier_id ON purchase_invoices(supplier_id);
            CREATE INDEX IF NOT EXISTS idx_purchase_invoices_due_date ON purchase_invoices(due_date);
            CREATE TABLE IF NOT EXISTS purchase_invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id),
                master_item_id INTEGER REFERENCES master_items(id),
                product_name TEXT NOT NULL,
                sku TEXT,
                unit TEXT NOT NULL DEFAULT '',
                qty INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                line_total INTEGER NOT NULL,
                line_no INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_invoice_id ON purchase_invoice_items(invoice_id);

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff' CHECK (role IN ('admin','staff')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

            -- Audit trail. Written by the ActivityLog middleware for every
            -- mutating API call, so coverage does not depend on remembering to
            -- instrument each new route. Append-only by intent: nothing in the
            -- app updates or deletes rows here.
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                action TEXT NOT NULL,
                entity TEXT,
                entity_id TEXT,
                summary TEXT,
                payload TEXT,
                ip TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_log_user_id ON activity_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON activity_log(entity, entity_id);
        """)

        # --- Additive migration: receipt start time ---
        # created_at is when the sale was saved; started_at is when the cashier
        # opened the tab and began entering it. Live databases already hold
        # receipts, so this is an ALTER guarded by a PRAGMA check rather than an
        # edit to the CREATE TABLE above. Existing rows keep NULL and fall back
        # to created_at when displayed.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "started_at" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN started_at TEXT")

        # --- Additive migration: quick-add brand/supplier support (§7.2) ---
        # data/scraper.db already has live supplier/brand rows, so this uses
        # ALTER TABLE ADD COLUMN guarded by PRAGMA table_info checks (SQLite has
        # no ADD COLUMN IF NOT EXISTS) rather than editing the CREATE TABLE
        # blocks above, which only apply to a fresh DB.
        for table in ("suppliers", "brands"):
            existing_cols = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            if "name_normalized" not in existing_cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN name_normalized TEXT")
            if "is_system" not in existing_cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
            if "source" not in existing_cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'master'")
            if "created_by" not in existing_cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN created_by TEXT")

            for row in con.execute(f"SELECT id, name FROM {table} WHERE name_normalized IS NULL OR name_normalized = ''").fetchall():
                con.execute(
                    f"UPDATE {table} SET name_normalized=? WHERE id=?",
                    (_normalize_name(row["name"]), row["id"]),
                )

            con.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_name_normalized ON {table}(name_normalized)"
            )

        supplier_cols = {row["name"] for row in con.execute("PRAGMA table_info(suppliers)").fetchall()}
        if "phone" not in supplier_cols:
            con.execute("ALTER TABLE suppliers ADD COLUMN phone TEXT")

        # --- Additive migration: supplier contact details (purchase invoice header) ---
        # A purchase invoice prints the supplier's contact block, so those details
        # live on the supplier master rather than being retyped per invoice. Live
        # rows predate these columns and stay NULL until someone fills them in.
        for supplier_col in ("address", "contact_person", "npwp"):
            if supplier_col not in supplier_cols:
                con.execute(f"ALTER TABLE suppliers ADD COLUMN {supplier_col} TEXT")

        # --- Additive migration: receipt payment status ---
        # data/scraper.db already has live receipt rows created before this
        # feature existed, so those are backfilled to 'done' (already-completed
        # sales) rather than left at the new column's 'pending' default, which
        # would otherwise make them vanish from Dashboard revenue once
        # get_sales_summary() filters on status='done'.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "status" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            con.execute("UPDATE receipts SET status='done' WHERE status='pending'")

        # --- Additive migration: down payment (amount_paid) ---
        # 'done' already meant "fully paid" by convention before this column
        # existed, so those rows are backfilled to amount_paid=total to keep
        # that invariant true; 'pending'/'void' rows have no payment history to
        # infer from and default to 0 (nothing recorded as paid yet).
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "amount_paid" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN amount_paid INTEGER NOT NULL DEFAULT 0")
            con.execute("UPDATE receipts SET amount_paid=total WHERE status='done'")

        # --- Additive migration: customer contact (phone required going forward, name optional) ---
        # Pre-existing receipts were created before contact capture existed, so they have no
        # number to backfill — they stay at '' (empty) and are grandfathered in. New receipts
        # are required to carry a phone at the routes/frontend layer, not by a DB constraint.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "customer_phone" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN customer_phone TEXT NOT NULL DEFAULT ''")
        if "customer_name" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN customer_name TEXT")

        seed_normalized = _normalize_name("Tanpa Merk")
        existing_seed = con.execute("SELECT 1 FROM brands WHERE code=?", ("NOB",)).fetchone()
        if existing_seed is None:
            con.execute(
                """INSERT INTO brands (code, name, is_active, created_at, name_normalized, is_system, source)
                   VALUES (?, ?, 1, ?, ?, 1, 'master')""",
                ("NOB", "Tanpa Merk", _now(), seed_normalized),
            )

        # --- Seed: initial Satuan (unit) master list ---
        # Only the starting values requested when this master was introduced;
        # admins manage the list from here on via the Master Satuan tab.
        for seed_unit_name in ("PCS", "SET"):
            unit_seed_normalized = _normalize_name(seed_unit_name)
            existing_unit_seed = con.execute(
                "SELECT 1 FROM units WHERE name_normalized=?", (unit_seed_normalized,)
            ).fetchone()
            if existing_unit_seed is None:
                con.execute(
                    "INSERT INTO units (name, name_normalized, is_active, created_at) VALUES (?, ?, 1, ?)",
                    (seed_unit_name, unit_seed_normalized, _now()),
                )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(job_id: str, keyword: str, city: str, kecamatan: list[str]) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO jobs (id, keyword, city, kecamatan_list, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, keyword, city, json.dumps(kecamatan), _now()),
        )


def update_job_status(
    job_id: str,
    status: str,
    progress: int = 0,
    error: str | None = None,
    completed_at: str | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE jobs SET status=?, progress=?, error=?, completed_at=? WHERE id=?",
            (status, progress, error, completed_at, job_id),
        )


def insert_place(job_id: str, name: str, address: str, phone: str, lat: float, lng: float) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO places (job_id, name, address, phone, lat, lng) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, name, address, phone, lat, lng),
        )


def insert_log(job_id: str, message: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO job_logs (job_id, created_at, message) VALUES (?, ?, ?)",
            (job_id, _now(), message),
        )


def fail_orphaned_jobs(error_message: str) -> int:
    with _conn() as con:
        rows = con.execute(
            "SELECT id FROM jobs WHERE status IN ('pending', 'running')"
        ).fetchall()
        orphaned_ids = [row["id"] for row in rows]
        if not orphaned_ids:
            return 0

        completed_at = _now()
        con.executemany(
            "UPDATE jobs SET status='failed', error=?, completed_at=? WHERE id=?",
            [(error_message, completed_at, job_id) for job_id in orphaned_ids],
        )
        con.executemany(
            "INSERT INTO job_logs (job_id, created_at, message) VALUES (?, ?, ?)",
            [(job_id, completed_at, f"✗ {error_message}") for job_id in orphaned_ids],
        )

    return len(orphaned_ids)


def get_job(job_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["kecamatan_list"] = json.loads(d["kecamatan_list"])
        return d


def get_all_jobs() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["kecamatan_list"] = json.loads(d["kecamatan_list"])
            result.append(d)
        return result


def get_places(job_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM places WHERE job_id=?", (job_id,)).fetchall()
        return [dict(r) for r in rows]


def get_logs_since(job_id: str, since_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM job_logs WHERE job_id=? AND id>? ORDER BY id ASC",
            (job_id, since_id),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_job(job_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM places WHERE job_id=?", (job_id,))
        con.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def _compute_receipt_status(total: int, amount_paid: int, requested_status: str | None) -> str:
    """'void' is the only status a caller can force directly — pending/done are
    always derived from whether amount_paid covers total. The routes layer
    already rejects any client-sent status other than 'void'/None; this stays
    defensive about it rather than trusting that."""
    if requested_status == "void":
        return "void"
    return "done" if amount_paid >= total else "pending"


def _sanitize_started_at(value: str | None, created_at: str) -> str:
    """started_at is the one timestamp the browser supplies, so it cannot be
    trusted the way created_at can. A value that is unparseable, in the future,
    or implausibly old (a machine with a wrong clock, or a tab left open
    overnight) falls back to the server's own created_at rather than recording a
    time the sale did not happen at. It is never editable after this point."""
    if not value:
        return created_at
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return created_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # A little slack for clock skew between the browser and the server.
    if parsed > now + timedelta(minutes=5):
        return created_at
    if parsed < now - timedelta(hours=24):
        return created_at
    return parsed.isoformat()


def create_receipt(
    receipt_id: str,
    plate_region: str,
    plate_number: str,
    plate_suffix: str,
    items: list[dict],
    discount: int,
    amount_paid: int = 0,
    status: str | None = None,
    customer_phone: str = "",
    customer_name: str | None = None,
    started_at: str | None = None,
) -> dict:
    plate_full = f"{plate_region} {plate_number} {plate_suffix}"
    subtotal = sum(item["quantity"] * item["unit_price"] for item in items)
    total = subtotal - discount
    final_status = _compute_receipt_status(total, amount_paid, status)
    created_at = _now()
    started_at = _sanitize_started_at(started_at, created_at)

    with _conn() as con:
        con.execute(
            """INSERT INTO receipts
               (id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, status, amount_paid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, final_status, amount_paid,
            ),
        )
        con.executemany(
            """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (receipt_id, item["product_name"], item["quantity"], item["unit_price"], item.get("warranty_date"))
                for item in items
            ],
        )

    return {
        "subtotal": subtotal, "discount": discount, "total": total, "status": final_status,
        "amount_paid": amount_paid, "customer_phone": customer_phone, "customer_name": customer_name,
    }


def get_receipt(receipt_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(row) if row is not None else None


def get_all_receipts(plate_query: str | None = None, status: str | None = None) -> list[dict]:
    where_clauses = []
    params: list = []
    if plate_query:
        where_clauses.append("plate_full LIKE ?")
        params.append(f"%{plate_query.upper()}%")
    if status:
        where_clauses.append("status=?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with _conn() as con:
        rows = con.execute(
            f"""SELECT *,
                   (SELECT COUNT(*) FROM receipt_items WHERE receipt_items.receipt_id = receipts.id) AS item_count
               FROM receipts {where_sql} ORDER BY created_at DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_receipt(
    receipt_id: str,
    plate_region: str | None = None,
    plate_number: str | None = None,
    plate_suffix: str | None = None,
    items: list[dict] | None = None,
    discount: int | None = None,
    amount_paid: int | None = None,
    status: str | None = None,
    customer_phone: str | None = None,
    customer_name: str | None = None,
    update_customer_name: bool = False,
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Receipt {receipt_id} not found")
        existing = dict(row)

        new_plate_region = plate_region if plate_region is not None else existing["plate_region"]
        new_plate_number = plate_number if plate_number is not None else existing["plate_number"]
        new_plate_suffix = plate_suffix if plate_suffix is not None else existing["plate_suffix"]
        new_plate_full = f"{new_plate_region} {new_plate_number} {new_plate_suffix}"

        new_customer_phone = customer_phone if customer_phone is not None else existing["customer_phone"]
        # name is nullable, so "clear it" (None) is distinct from "leave unchanged" — the route
        # sets update_customer_name only when the field was actually present in the request.
        new_customer_name = customer_name if update_customer_name else existing["customer_name"]

        new_amount_paid = amount_paid if amount_paid is not None else existing["amount_paid"]

        if items is not None:
            con.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
            con.executemany(
                """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (receipt_id, item["product_name"], item["quantity"], item["unit_price"], item.get("warranty_date"))
                    for item in items
                ],
            )
            effective_items = items
        elif discount is not None:
            effective_items = [dict(r) for r in con.execute(
                "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
            ).fetchall()]
        else:
            effective_items = None

        if effective_items is not None:
            new_subtotal = sum(item["quantity"] * item["unit_price"] for item in effective_items)
            new_discount = discount if discount is not None else existing["discount"]
            new_total = new_subtotal - new_discount
        else:
            new_subtotal = existing["subtotal"]
            new_discount = existing["discount"]
            new_total = existing["total"]

        # Void is sticky and has no unvoid path — once a receipt is voided, only
        # a fresh explicit status="void" request is honored (a no-op); editing
        # amount_paid or items afterward must not accidentally revive it to a
        # computed pending/done.
        if existing["status"] == "void" and status != "void":
            new_status = "void"
        else:
            new_status = _compute_receipt_status(new_total, new_amount_paid, status)

        con.execute(
            """UPDATE receipts SET plate_region=?, plate_number=?, plate_suffix=?, plate_full=?,
               customer_phone=?, customer_name=?, subtotal=?, discount=?, total=?, status=?, amount_paid=? WHERE id=?""",
            (
                new_plate_region, new_plate_number, new_plate_suffix, new_plate_full,
                new_customer_phone, new_customer_name,
                new_subtotal, new_discount, new_total, new_status, new_amount_paid, receipt_id,
            ),
        )

        updated = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(updated)


def get_receipt_items(receipt_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC",
            (receipt_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_receipt(receipt_id: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT 1 FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Receipt {receipt_id} not found")
        con.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
        con.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))


def get_sales_summary(date_from: str, date_to: str) -> dict:
    with _conn() as con:
        rows = con.execute(
            """SELECT date(created_at) AS day, SUM(total) AS revenue, COUNT(*) AS receipt_count
               FROM receipts
               WHERE date(created_at) BETWEEN ? AND ? AND status='done'
               GROUP BY date(created_at)
               ORDER BY day ASC""",
            (date_from, date_to),
        ).fetchall()

    by_day = {
        row["day"]: {"date": row["day"], "revenue": row["revenue"], "receipt_count": row["receipt_count"]}
        for row in rows
    }

    total_revenue = sum(d["revenue"] for d in by_day.values())
    receipt_count = sum(d["receipt_count"] for d in by_day.values())

    daily: list[dict] = []
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while current <= end:
        key = current.isoformat()
        daily.append(by_day.get(key, {"date": key, "revenue": 0, "receipt_count": 0}))
        current += timedelta(days=1)

    top_days = sorted(by_day.values(), key=lambda d: d["revenue"], reverse=True)[:5]

    return {
        "total_revenue": total_revenue,
        "receipt_count": receipt_count,
        "daily": daily,
        "top_days": top_days,
    }


# ---------------------------------------------------------------------------
# Barang Masuk / Master Barang (inventory & procurement) module
# ---------------------------------------------------------------------------


def _generate_entity_code(con: sqlite3.Connection, table: str, name: str) -> str:
    """Must run inside the caller's already-open transaction — that's what makes
    the collision check race-safe (SQLite serializes concurrent writers at the
    transaction level under PRAGMA journal_mode=DELETE)."""
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    base = (letters[:3] or "").ljust(3, "X") if letters else "XXX"
    code = base
    n = 2
    while con.execute(f"SELECT 1 FROM {table} WHERE code=?", (code,)).fetchone() is not None:
        code = f"{base}{n}"
        n += 1
    return code


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9 ]", "", name.upper())
    words = [w for w in cleaned.split() if w not in _STOPWORDS]
    return " ".join(words)


def _compute_product_segment(name_normalized: str) -> str:
    words = name_normalized.split()[:3]
    segment = "".join(w[:3] for w in words)[:8]
    return segment if segment else "PRD"


def _next_sku(con: sqlite3.Connection, supplier_code: str, brand_code: str, segment: str) -> tuple[str, str, int]:
    """Takes an already-open connection — never opens its own. Must run inside the
    same transaction as the INSERT INTO master_items it is generating a SKU for.
    Do NOT give this its own _conn() call; that would defeat the race-safety."""
    prefix = f"{supplier_code}-{brand_code}-{segment}"
    row = con.execute(
        "SELECT COALESCE(MAX(sku_seq),0)+1 AS next_seq FROM master_items WHERE sku_prefix=?",
        (prefix,),
    ).fetchone()
    seq = row["next_seq"]
    if seq > 999:
        raise ValueError(f"SKU sequence exhausted for prefix {prefix} (999 reached) — cannot allocate a new SKU")
    sku = f"{prefix}-{seq:03d}"
    return sku, prefix, seq


_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,10}$")
_FUZZY_SEARCH_MIN_SCORE = 60  # rapidfuzz WRatio cutoff — below this, treat as unrelated noise


def _create_quick_add_entity(
    table: str,
    name: str,
    code: str | None,
    source: str,
    phone: str | None = None,
    address: str | None = None,
    contact_person: str | None = None,
    npwp: str | None = None,
) -> dict:
    """Shared insert path for create_supplier/create_brand — same validation and
    insert shape for both tables, only the table name differs. `phone`/`address`/
    `contact_person`/`npwp` are suppliers-only columns (brands have no such
    fields) — ignored for brands."""
    entity_label = "Supplier" if table == "suppliers" else "Brand"
    name_normalized = _normalize_name(name)
    created_at = _now()
    with _conn() as con:
        dup = con.execute(
            f"SELECT 1 FROM {table} WHERE name_normalized=? AND is_active=1", (name_normalized,)
        ).fetchone()
        if dup is not None:
            raise ValueError(f"{entity_label} '{name}' already exists")

        if code is not None:
            code = code.strip().upper()
            if not _CODE_PATTERN.match(code):
                raise ValueError("code must be 2-10 alphanumeric characters")
            existing_code = con.execute(f"SELECT 1 FROM {table} WHERE code=?", (code,)).fetchone()
            if existing_code is not None:
                raise ValueError(f"Code '{code}' is already in use")
        else:
            code = _generate_entity_code(con, table, name)

        if table == "suppliers":
            cur = con.execute(
                """INSERT INTO suppliers
                   (code, name, is_active, created_at, name_normalized, is_system, source,
                    phone, address, contact_person, npwp)
                   VALUES (?, ?, 1, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    code,
                    name,
                    created_at,
                    name_normalized,
                    source,
                    (phone or "").strip() or None,
                    (address or "").strip() or None,
                    (contact_person or "").strip() or None,
                    (npwp or "").strip() or None,
                ),
            )
        else:
            cur = con.execute(
                """INSERT INTO brands (code, name, is_active, created_at, name_normalized, is_system, source)
                   VALUES (?, ?, 1, ?, ?, 0, ?)""",
                (code, name, created_at, name_normalized, source),
            )
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def _update_quick_add_entity(
    table: str,
    entity_id: int,
    name: str | None,
    is_active: bool | None,
    phone: str | None = None,
    address: str | None = None,
    contact_person: str | None = None,
    npwp: str | None = None,
) -> dict:
    entity_label = "Supplier" if table == "suppliers" else "Brand"
    supplier_details = (phone, address, contact_person, npwp)
    with _conn() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"{entity_label} {entity_id} not found")
        if row["is_system"] and (name is not None or is_active is not None or any(d is not None for d in supplier_details)):
            raise ValueError(f"System {entity_label.lower()} cannot be modified")
        new_name = name if name is not None else row["name"]
        new_name_normalized = _normalize_name(new_name) if name is not None else row["name_normalized"]
        if name is not None:
            dup = con.execute(
                f"SELECT 1 FROM {table} WHERE name_normalized=? AND is_active=1 AND id!=?",
                (new_name_normalized, entity_id),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"{entity_label} '{name}' already exists")
        new_active = int(is_active) if is_active is not None else row["is_active"]

        if table == "suppliers":
            def _merged(value: str | None, column: str) -> str | None:
                return (value.strip() or None) if value is not None else row[column]

            con.execute(
                """UPDATE suppliers
                   SET name=?, name_normalized=?, is_active=?, phone=?, address=?, contact_person=?, npwp=?
                   WHERE id=?""",
                (
                    new_name,
                    new_name_normalized,
                    new_active,
                    _merged(phone, "phone"),
                    _merged(address, "address"),
                    _merged(contact_person, "contact_person"),
                    _merged(npwp, "npwp"),
                    entity_id,
                ),
            )
        else:
            con.execute(
                "UPDATE brands SET name=?, name_normalized=?, is_active=? WHERE id=?",
                (new_name, new_name_normalized, new_active, entity_id),
            )
        updated = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        return dict(updated)


def _entity_in_use(con: sqlite3.Connection, table: str, entity_id: int) -> bool:
    """True if any master item or document still references this supplier/brand —
    both have NOT NULL FK columns pointing at these tables, and SQLite here runs
    without FK enforcement, so a delete would silently orphan them."""
    if table == "suppliers":
        row = con.execute(
            "SELECT 1 FROM master_items WHERE supplier_id=? "
            "UNION SELECT 1 FROM inbound_documents WHERE supplier_id=? "
            "UNION SELECT 1 FROM purchase_invoices WHERE supplier_id=? LIMIT 1",
            (entity_id, entity_id, entity_id),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT 1 FROM master_items WHERE brand_id=? "
            "UNION SELECT 1 FROM inbound_items WHERE pending_brand_id=? LIMIT 1",
            (entity_id, entity_id),
        ).fetchone()
    return row is not None


def _delete_quick_add_entity(table: str, entity_id: int) -> None:
    entity_label = "Supplier" if table == "suppliers" else "Brand"
    with _conn() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"{entity_label} {entity_id} not found")
        if row["is_system"]:
            raise ValueError(f"System {entity_label.lower()} cannot be deleted")
        if _entity_in_use(con, table, entity_id):
            raise ValueError(f"{entity_label} is still in use and cannot be deleted")
        con.execute(f"DELETE FROM {table} WHERE id=?", (entity_id,))


def _delete_all_quick_add_entities(table: str) -> dict:
    """Deletes every deletable row in one transaction, skipping system entities
    and any still referenced elsewhere. Used by the 'delete all' bulk action."""
    with _conn() as con:
        rows = con.execute(f"SELECT id, is_system FROM {table}").fetchall()
        deleted = 0
        skipped = 0
        for row in rows:
            if row["is_system"] or _entity_in_use(con, table, row["id"]):
                skipped += 1
                continue
            con.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
            deleted += 1
    return {"deleted_count": deleted, "skipped_count": skipped}


def _search_quick_add_entities(table: str, query: str, active_only: bool, limit: int) -> list[dict]:
    with _conn() as con:
        if active_only:
            rows = con.execute(f"SELECT * FROM {table} WHERE is_active=1").fetchall()
        else:
            rows = con.execute(f"SELECT * FROM {table}").fetchall()

    scored = []
    for row in rows:
        d = dict(row)
        d["score"] = fuzz.WRatio(query, d["name"])
        if d["score"] >= _FUZZY_SEARCH_MIN_SCORE:
            scored.append(d)
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:limit]


# --- Suppliers -------------------------------------------------------------


def create_supplier(
    name: str,
    code: str | None = None,
    source: str = "quick_add",
    phone: str | None = None,
    address: str | None = None,
    contact_person: str | None = None,
    npwp: str | None = None,
) -> dict:
    return _create_quick_add_entity("suppliers", name, code, source, phone, address, contact_person, npwp)


def update_supplier(
    supplier_id: int,
    name: str | None = None,
    is_active: bool | None = None,
    phone: str | None = None,
    address: str | None = None,
    contact_person: str | None = None,
    npwp: str | None = None,
) -> dict:
    return _update_quick_add_entity(
        "suppliers", supplier_id, name, is_active, phone, address, contact_person, npwp
    )


def get_supplier(supplier_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        return dict(row) if row is not None else None


def get_all_suppliers(active_only: bool = False) -> list[dict]:
    with _conn() as con:
        if active_only:
            rows = con.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name ASC").fetchall()
        else:
            rows = con.execute("SELECT * FROM suppliers ORDER BY name ASC").fetchall()
        return [dict(r) for r in rows]


def search_suppliers(query: str, active_only: bool = True, limit: int = 8) -> list[dict]:
    return _search_quick_add_entities("suppliers", query, active_only, limit)


def preview_supplier_code(name: str) -> str:
    with _conn() as con:
        return _generate_entity_code(con, "suppliers", name)


def delete_supplier(supplier_id: int) -> None:
    _delete_quick_add_entity("suppliers", supplier_id)


def delete_all_suppliers() -> dict:
    return _delete_all_quick_add_entities("suppliers")


# --- Brands ------------------------------------------------------------


def create_brand(name: str, code: str | None = None, source: str = "quick_add") -> dict:
    return _create_quick_add_entity("brands", name, code, source)


def update_brand(brand_id: int, name: str | None = None, is_active: bool | None = None) -> dict:
    return _update_quick_add_entity("brands", brand_id, name, is_active)


def get_brand(brand_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
        return dict(row) if row is not None else None


def get_all_brands(active_only: bool = False) -> list[dict]:
    with _conn() as con:
        if active_only:
            rows = con.execute("SELECT * FROM brands WHERE is_active=1 ORDER BY name ASC").fetchall()
        else:
            rows = con.execute("SELECT * FROM brands ORDER BY name ASC").fetchall()
        return [dict(r) for r in rows]


def search_brands(query: str, active_only: bool = True, limit: int = 8) -> list[dict]:
    return _search_quick_add_entities("brands", query, active_only, limit)


def preview_brand_code(name: str) -> str:
    with _conn() as con:
        return _generate_entity_code(con, "brands", name)


def delete_brand(brand_id: int) -> None:
    _delete_quick_add_entity("brands", brand_id)


def delete_all_brands() -> dict:
    return _delete_all_quick_add_entities("brands")


# --- Units (Satuan) ---------------------------------------------------------
# Deliberately simpler than brands/suppliers: no code (units don't feed the
# SKU), no source/is_system tracking (nothing here is a locked default) —
# just a name and active flag. master_items.unit / inbound_items.pending_unit
# store the unit's name as plain text rather than a foreign key (pre-existing
# schema shape), so "in use" is checked by name match, not id.


def create_unit(name: str) -> dict:
    name_normalized = _normalize_name(name)
    created_at = _now()
    with _conn() as con:
        dup = con.execute(
            "SELECT 1 FROM units WHERE name_normalized=? AND is_active=1", (name_normalized,)
        ).fetchone()
        if dup is not None:
            raise ValueError(f"Satuan '{name}' already exists")
        cur = con.execute(
            "INSERT INTO units (name, name_normalized, is_active, created_at) VALUES (?, ?, 1, ?)",
            (name.strip(), name_normalized, created_at),
        )
        row = con.execute("SELECT * FROM units WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def update_unit(unit_id: int, name: str | None = None, is_active: bool | None = None) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unit {unit_id} not found")
        new_name = name.strip() if name is not None else row["name"]
        new_name_normalized = _normalize_name(new_name) if name is not None else row["name_normalized"]
        if name is not None:
            dup = con.execute(
                "SELECT 1 FROM units WHERE name_normalized=? AND is_active=1 AND id!=?",
                (new_name_normalized, unit_id),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"Satuan '{name}' already exists")
        new_active = int(is_active) if is_active is not None else row["is_active"]
        con.execute(
            "UPDATE units SET name=?, name_normalized=?, is_active=? WHERE id=?",
            (new_name, new_name_normalized, new_active, unit_id),
        )
        updated = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(updated)


def get_unit(unit_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(row) if row is not None else None


def get_unit_by_name(name: str) -> dict | None:
    """Case-insensitive lookup against active units — used to validate a
    submitted unit string (from Tambah Barang Baru, item edit, or a Barang
    Masuk pending-product row) against the Satuan master before it's saved."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM units WHERE UPPER(name)=UPPER(?) AND is_active=1", (name.strip(),)
        ).fetchone()
        return dict(row) if row is not None else None


def get_all_units(active_only: bool = False) -> list[dict]:
    with _conn() as con:
        if active_only:
            rows = con.execute("SELECT * FROM units WHERE is_active=1 ORDER BY name ASC").fetchall()
        else:
            rows = con.execute("SELECT * FROM units ORDER BY name ASC").fetchall()
        return [dict(r) for r in rows]


def _unit_in_use(con: sqlite3.Connection, unit_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM master_items WHERE UPPER(unit)=UPPER(?) "
        "UNION SELECT 1 FROM inbound_items WHERE UPPER(pending_unit)=UPPER(?) LIMIT 1",
        (unit_name, unit_name),
    ).fetchone()
    return row is not None


def delete_unit(unit_id: int) -> None:
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unit {unit_id} not found")
        if _unit_in_use(con, row["name"]):
            raise ValueError("Satuan is still in use and cannot be deleted")
        con.execute("DELETE FROM units WHERE id=?", (unit_id,))


def delete_all_units() -> dict:
    with _conn() as con:
        rows = con.execute("SELECT id, name FROM units").fetchall()
        deleted = 0
        skipped = 0
        for row in rows:
            if _unit_in_use(con, row["name"]):
                skipped += 1
                continue
            con.execute("DELETE FROM units WHERE id=?", (row["id"],))
            deleted += 1
    return {"deleted_count": deleted, "skipped_count": skipped}


# --- Duplicate lookups / SKU preview ---------------------------------------


def _find_exact_duplicate_master_item(
    con: sqlite3.Connection, supplier_id: int, brand_id: int, name_normalized: str
) -> dict | None:
    row = con.execute(
        "SELECT * FROM master_items WHERE supplier_id=? AND brand_id=? AND name_normalized=?",
        (supplier_id, brand_id, name_normalized),
    ).fetchone()
    return dict(row) if row is not None else None


def find_exact_duplicate_master_item(supplier_id: int, brand_id: int, product_name: str) -> dict | None:
    name_normalized = _normalize_name(product_name)
    with _conn() as con:
        return _find_exact_duplicate_master_item(con, supplier_id, brand_id, name_normalized)


def find_cross_supplier_master_items(supplier_id: int, brand_id: int, product_name: str) -> list[dict]:
    """BR-06: same brand + same normalized name, different supplier."""
    name_normalized = _normalize_name(product_name)
    with _conn() as con:
        rows = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.brand_id=? AND mi.name_normalized=? AND mi.supplier_id != ?""",
            (brand_id, name_normalized, supplier_id),
        ).fetchall()
        return [dict(r) for r in rows]


def preview_sku(supplier_id: int, brand_id: int, product_name: str) -> dict:
    """Read-only preview, not reserved — a preview/post race is possible but
    cosmetic-only since real allocation is serialized inside post_inbound_document."""
    name_normalized = _normalize_name(product_name)
    segment = _compute_product_segment(name_normalized)
    with _conn() as con:
        supplier = con.execute("SELECT code FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError(f"Supplier {supplier_id} not found")
        brand = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
        if brand is None:
            raise ValueError(f"Brand {brand_id} not found")
        prefix = f"{supplier['code']}-{brand['code']}-{segment}"
        row = con.execute(
            "SELECT COALESCE(MAX(sku_seq),0)+1 AS next_seq FROM master_items WHERE sku_prefix=?",
            (prefix,),
        ).fetchone()
        next_seq = row["next_seq"]
        sku = f"{prefix}-{next_seq:03d}"
        return {"segment": segment, "prefix": prefix, "next_seq": next_seq, "sku": sku}


# --- Master items ------------------------------------------------------


def create_master_item(
    supplier_id: int, brand_id: int, name: str, unit: str, sell_price: int = 0, cost_price: int = 0
) -> dict:
    name_normalized = _normalize_name(name)
    with _conn() as con:
        supplier = con.execute("SELECT code FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError(f"Supplier {supplier_id} not found")
        brand = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
        if brand is None:
            raise ValueError(f"Brand {brand_id} not found")
        dup = _find_exact_duplicate_master_item(con, supplier_id, brand_id, name_normalized)
        if dup is not None:
            raise ValueError(f"Duplicate product: SKU {dup['sku']} already exists for this supplier and brand")

        segment = _compute_product_segment(name_normalized)
        sku, prefix, seq = _next_sku(con, supplier["code"], brand["code"], segment)
        created_at = _now()
        cur = con.execute(
            """INSERT INTO master_items
               (sku, sku_prefix, sku_seq, supplier_id, brand_id, name, name_normalized, unit,
                last_cost_price, avg_cost_price, sell_price, stock_qty, first_received_date, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 1, ?)""",
            (
                sku, prefix, seq, supplier_id, brand_id, name, name_normalized, unit,
                cost_price, cost_price, sell_price, created_at,
            ),
        )
        row = con.execute("SELECT * FROM master_items WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def update_master_item(
    master_item_id: int, name: str | None = None, sell_price: int | None = None, unit: str | None = None
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Master item {master_item_id} not found")
        new_name = name if name is not None else row["name"]
        new_name_normalized = _normalize_name(new_name) if name is not None else row["name_normalized"]
        new_sell_price = sell_price if sell_price is not None else row["sell_price"]
        new_unit = unit if unit is not None else row["unit"]
        try:
            con.execute(
                "UPDATE master_items SET name=?, name_normalized=?, sell_price=?, unit=? WHERE id=?",
                (new_name, new_name_normalized, new_sell_price, new_unit, master_item_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"Cannot rename: another product with this name already exists for this supplier and brand"
            )
        updated = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        return dict(updated)


def delete_master_item(master_item_id: int) -> None:
    """Hard-deletes a master item. Only allowed when it has no receipt or stock
    history — deleting a referenced product would orphan stock_ledger rows and
    corrupt costing. Products that have ever been received should be kept."""
    with _conn() as con:
        row = con.execute("SELECT id FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Master item {master_item_id} not found")
        in_use = con.execute(
            "SELECT 1 FROM inbound_items WHERE master_item_id=? LIMIT 1", (master_item_id,)
        ).fetchone() or con.execute(
            "SELECT 1 FROM stock_ledger WHERE master_item_id=? LIMIT 1", (master_item_id,)
        ).fetchone()
        if in_use is not None:
            raise ValueError(
                "Barang memiliki riwayat barang masuk atau pergerakan stok dan tidak dapat dihapus"
            )
        con.execute("DELETE FROM master_items WHERE id=?", (master_item_id,))


def get_master_item(master_item_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.id=?""",
            (master_item_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_master_item_by_sku(sku: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.sku=? AND mi.is_active=1""",
            (sku,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_master_items(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    supplier_id: int | None = None,
    brand_id: int | None = None,
    is_active: bool | None = None,
) -> dict:
    offset = (page - 1) * page_size
    where_clauses = []
    params: list = []
    if search:
        where_clauses.append("(mi.name LIKE ? OR mi.sku LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if supplier_id is not None:
        where_clauses.append("mi.supplier_id=?")
        params.append(supplier_id)
    if brand_id is not None:
        where_clauses.append("mi.brand_id=?")
        params.append(brand_id)
    if is_active is not None:
        where_clauses.append("mi.is_active=?")
        params.append(1 if is_active else 0)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with _conn() as con:
        total_row = con.execute(f"SELECT COUNT(*) AS c FROM master_items mi {where_sql}", params).fetchone()
        total = total_row["c"]
        rows = con.execute(
            f"""SELECT mi.*, s.name AS supplier_name, b.name AS brand_name
                FROM master_items mi
                JOIN suppliers s ON s.id = mi.supplier_id
                JOIN brands b ON b.id = mi.brand_id
                {where_sql}
                ORDER BY mi.created_at DESC
                LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def get_master_item_batch_history(master_item_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT ii.id AS inbound_item_id, ii.document_id, d.doc_number, d.status AS document_status,
                      ii.qty_in, ii.qty_remaining, ii.cost_price, ii.received_date
               FROM inbound_items ii
               JOIN inbound_documents d ON d.id = ii.document_id
               WHERE ii.master_item_id=? AND ii.received_date IS NOT NULL
               ORDER BY ii.received_date DESC, ii.id DESC""",
            (master_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_master_items_for_export() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT mi.sku, mi.name, s.name AS supplier_name, b.name AS brand_name, mi.unit,
                      mi.stock_qty, mi.avg_cost_price, mi.last_cost_price, mi.sell_price, mi.is_active
               FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               ORDER BY mi.sku ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_inventory_overview() -> dict:
    with _conn() as con:
        active_sku_count = con.execute("SELECT COUNT(*) AS c FROM master_items WHERE is_active=1").fetchone()["c"]
        total_value_row = con.execute("SELECT SUM(stock_qty * avg_cost_price) AS v FROM master_items").fetchone()
        total_inventory_value = total_value_row["v"] or 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        inbound_doc_count_30d = con.execute(
            "SELECT COUNT(*) AS c FROM inbound_documents WHERE created_at >= ?", (cutoff,)
        ).fetchone()["c"]
        low_stock_rows = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.is_active=1 AND mi.stock_qty <= ? ORDER BY mi.stock_qty ASC""",
            (LOW_STOCK_THRESHOLD,),
        ).fetchall()

        # Pending invoices: receipts not yet fully paid (amount_paid < total).
        pending_totals = con.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(total - amount_paid), 0) AS outstanding "
            "FROM receipts WHERE status='pending'"
        ).fetchone()
        pending_rows = con.execute(
            """SELECT id, plate_full, customer_name, total, amount_paid,
                      (total - amount_paid) AS outstanding, created_at
               FROM receipts WHERE status='pending'
               ORDER BY created_at DESC LIMIT 5"""
        ).fetchall()

        # Latest incoming products from posted Barang Masuk documents.
        latest_incoming_rows = con.execute(
            """SELECT ii.qty_in, ii.received_date, mi.name AS product_name, mi.sku,
                      s.name AS supplier_name
               FROM inbound_items ii
               JOIN inbound_documents d ON d.id = ii.document_id
               JOIN master_items mi ON mi.id = ii.master_item_id
               JOIN suppliers s ON s.id = d.supplier_id
               WHERE d.status='posted' AND ii.master_item_id IS NOT NULL
               ORDER BY ii.received_date DESC, ii.id DESC LIMIT 5"""
        ).fetchall()

        # Top-selling products (last 30 days, by units sold) from paid receipts.
        sales_cutoff = (date.today() - timedelta(days=30)).isoformat()
        top_selling_rows = con.execute(
            """SELECT ri.product_name,
                      SUM(ri.quantity) AS units_sold,
                      SUM(ri.quantity * ri.unit_price) AS revenue
               FROM receipt_items ri
               JOIN receipts r ON r.id = ri.receipt_id
               WHERE r.status='done' AND date(r.created_at) >= ?
               GROUP BY ri.product_name
               ORDER BY units_sold DESC, revenue DESC LIMIT 5""",
            (sales_cutoff,),
        ).fetchall()

        # Sales this calendar month (paid receipts).
        month_start = date.today().replace(day=1).isoformat()
        sales_month = con.execute(
            "SELECT COALESCE(SUM(total), 0) AS revenue, COUNT(*) AS c "
            "FROM receipts WHERE status='done' AND date(created_at) >= ?",
            (month_start,),
        ).fetchone()

        return {
            "active_sku_count": active_sku_count,
            "total_inventory_value": total_inventory_value,
            "inbound_doc_count_30d": inbound_doc_count_30d,
            "low_stock_threshold": LOW_STOCK_THRESHOLD,
            "low_stock_items": [dict(r) for r in low_stock_rows],
            "pending_invoice_count": pending_totals["c"],
            "pending_invoice_outstanding": pending_totals["outstanding"],
            "pending_invoices": [dict(r) for r in pending_rows],
            "latest_incoming": [dict(r) for r in latest_incoming_rows],
            "top_selling": [dict(r) for r in top_selling_rows],
            "sales_month_revenue": sales_month["revenue"],
            "sales_month_count": sales_month["c"],
        }


def autocomplete_master_items(
    query: str, supplier_id: int | None = None, brand_id: int | None = None, limit: int = 20
) -> list[dict]:
    where_clauses = ["mi.is_active=1", "mi.name LIKE ?"]
    params: list = [f"%{query}%"]
    if supplier_id is not None:
        where_clauses.append("mi.supplier_id=?")
        params.append(supplier_id)
    if brand_id is not None:
        where_clauses.append("mi.brand_id=?")
        params.append(brand_id)
    where_sql = " AND ".join(where_clauses)
    with _conn() as con:
        rows = con.execute(
            f"""SELECT mi.*, s.name AS supplier_name, b.name AS brand_name
                FROM master_items mi
                JOIN suppliers s ON s.id = mi.supplier_id
                JOIN brands b ON b.id = mi.brand_id
                WHERE {where_sql}
                ORDER BY mi.name ASC LIMIT ?""",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]


# --- Inbound documents ------------------------------------------------------


def _next_doc_number(con: sqlite3.Connection, today_str: str) -> str:
    prefix = f"IN-{today_str}-"
    row = con.execute(
        "SELECT doc_number FROM inbound_documents WHERE doc_number LIKE ? ORDER BY doc_number DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = int(row["doc_number"].rsplit("-", 1)[-1]) + 1 if row is not None else 1
    return f"{prefix}{seq:04d}"


def _insert_inbound_items(con: sqlite3.Connection, document_id: int, items: list[dict]) -> None:
    con.executemany(
        """INSERT INTO inbound_items
           (document_id, master_item_id, pending_brand_id, pending_product_name, pending_unit,
            pending_sell_price, qty_in, cost_price, qty_remaining, received_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
        [
            (
                document_id,
                item.get("master_item_id"),
                item.get("pending_brand_id"),
                item.get("pending_product_name"),
                item.get("pending_unit"),
                item.get("pending_sell_price"),
                item["qty_in"],
                item["cost_price"],
            )
            for item in items
        ],
    )


def create_inbound_document(
    supplier_id: int,
    received_date: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    items: list[dict],
) -> dict:
    created_at = _now()
    total_value = sum(i["qty_in"] * i["cost_price"] for i in items)
    with _conn() as con:
        today_str = date.today().strftime("%Y%m%d")
        doc_number = _next_doc_number(con, today_str)
        cur = con.execute(
            """INSERT INTO inbound_documents
               (doc_number, supplier_id, received_date, supplier_invoice_no, notes, status, total_value, created_at)
               VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (doc_number, supplier_id, received_date, supplier_invoice_no, notes, total_value, created_at),
        )
        document_id = cur.lastrowid
        _insert_inbound_items(con, document_id, items)
        row = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(row)


def update_inbound_document(
    document_id: int,
    supplier_id: int,
    received_date: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    items: list[dict],
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"Inbound document {document_id} not found")
        if row["status"] != "draft":
            raise ValueError(
                f"Cannot edit inbound document {document_id}: status is '{row['status']}', only draft documents can be edited"
            )
        total_value = sum(i["qty_in"] * i["cost_price"] for i in items)
        con.execute(
            "UPDATE inbound_documents SET supplier_id=?, received_date=?, supplier_invoice_no=?, notes=?, total_value=? WHERE id=?",
            (supplier_id, received_date, supplier_invoice_no, notes, total_value, document_id),
        )
        con.execute("DELETE FROM inbound_items WHERE document_id=?", (document_id,))
        _insert_inbound_items(con, document_id, items)
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


def get_inbound_document(document_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT d.*, s.name AS supplier_name FROM inbound_documents d
               JOIN suppliers s ON s.id = d.supplier_id WHERE d.id=?""",
            (document_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_inbound_document_items(document_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT ii.*, mi.sku AS resolved_sku, mi.name AS resolved_name,
                      mi.brand_id AS resolved_brand_id, mi.unit AS resolved_unit,
                      mi.sell_price AS resolved_sell_price, rb.name AS resolved_brand_name,
                      b.name AS pending_brand_name
               FROM inbound_items ii
               LEFT JOIN master_items mi ON mi.id = ii.master_item_id
               LEFT JOIN brands rb ON rb.id = mi.brand_id
               LEFT JOIN brands b ON b.id = ii.pending_brand_id
               WHERE ii.document_id=?
               ORDER BY ii.id ASC""",
            (document_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_inbound_documents(status: str | None = None, supplier_id: int | None = None) -> list[dict]:
    with _conn() as con:
        query = """SELECT d.*, s.name AS supplier_name FROM inbound_documents d
                   JOIN suppliers s ON s.id = d.supplier_id WHERE 1=1"""
        params: list = []
        if status:
            query += " AND d.status=?"
            params.append(status)
        if supplier_id is not None:
            query += " AND d.supplier_id=?"
            params.append(supplier_id)
        query += " ORDER BY d.created_at DESC"
        rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def _recompute_master_item_costing(con: sqlite3.Connection, master_item_id: int) -> dict:
    """Recomputes avg_cost_price from SUM(qty_remaining*cost_price)/SUM(qty_remaining)
    across this item's posted, still-remaining batches. Returns {avg_cost_price, total_qty}."""
    agg = con.execute(
        """SELECT SUM(ii.qty_remaining) AS total_qty, SUM(ii.qty_remaining * ii.cost_price) AS total_value
           FROM inbound_items ii
           JOIN inbound_documents d ON d.id = ii.document_id
           WHERE ii.master_item_id=? AND d.status='posted' AND ii.qty_remaining > 0""",
        (master_item_id,),
    ).fetchone()
    total_qty = agg["total_qty"] or 0
    total_value = agg["total_value"] or 0
    avg_cost_price = (total_value // total_qty) if total_qty > 0 else 0
    return {"avg_cost_price": avg_cost_price, "total_qty": total_qty}


def post_inbound_document(document_id: int) -> dict:
    with _conn() as con:
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Inbound document {document_id} not found")
        if header["status"] != "draft":
            raise ValueError(
                f"Cannot post inbound document {document_id}: status is '{header['status']}', only draft documents can be posted"
            )

        # Step 2: re-validate received_date, even though it may have already been
        # checked at creation time (it can go stale between draft creation and posting).
        received_date = header["received_date"]
        try:
            rd = date.fromisoformat(received_date)
        except ValueError:
            raise ValueError(f"Invalid received_date '{received_date}' on document {document_id}")
        today = date.today()
        if rd > today:
            raise ValueError(f"received_date {received_date} is in the future")
        if (today - rd).days > 30:
            raise ValueError(f"received_date {received_date} is more than 30 days in the past")

        # Step 3: fetch line items, reject if empty.
        items = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=?", (document_id,)).fetchall()]
        if not items:
            raise ValueError(f"Cannot post inbound document {document_id}: it has no line items")

        # Step 4: flip status to posted FIRST — the avg-cost recompute below filters
        # on d.status='posted' and must see this document's own new rows.
        con.execute("UPDATE inbound_documents SET status='posted' WHERE id=?", (document_id,))

        # Step 5: resolve unresolved new-product lines into real master items + SKUs.
        for item in items:
            if item["master_item_id"] is None:
                brand_id = item["pending_brand_id"]
                product_name = item["pending_product_name"]
                unit = item["pending_unit"]
                sell_price = item["pending_sell_price"] or 0
                name_normalized = _normalize_name(product_name)

                # Defensive re-check (race guard) — another concurrent post may have
                # just created this exact product under this same supplier+brand.
                dup = _find_exact_duplicate_master_item(con, header["supplier_id"], brand_id, name_normalized)
                if dup is not None:
                    master_item_id = dup["id"]
                else:
                    supplier_row = con.execute(
                        "SELECT code FROM suppliers WHERE id=?", (header["supplier_id"],)
                    ).fetchone()
                    if supplier_row is None:
                        raise ValueError(f"Supplier {header['supplier_id']} not found")
                    brand_row = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
                    if brand_row is None:
                        raise ValueError(f"Brand {brand_id} not found")
                    segment = _compute_product_segment(name_normalized)
                    sku, prefix, seq = _next_sku(con, supplier_row["code"], brand_row["code"], segment)
                    created_at = _now()
                    cur = con.execute(
                        """INSERT INTO master_items
                           (sku, sku_prefix, sku_seq, supplier_id, brand_id, name, name_normalized, unit,
                            last_cost_price, avg_cost_price, sell_price, stock_qty, first_received_date, is_active, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, NULL, 1, ?)""",
                        (
                            sku, prefix, seq, header["supplier_id"], brand_id, product_name, name_normalized, unit,
                            sell_price, created_at,
                        ),
                    )
                    master_item_id = cur.lastrowid

                con.execute("UPDATE inbound_items SET master_item_id=? WHERE id=?", (master_item_id, item["id"]))
                item["master_item_id"] = master_item_id

        # Step 6: stamp qty_remaining/received_date — this is the moment each row
        # becomes a real batch.
        for item in items:
            con.execute(
                "UPDATE inbound_items SET qty_remaining=?, received_date=? WHERE id=?",
                (item["qty_in"], received_date, item["id"]),
            )

        # Step 7+8: recompute costing per distinct master item touched, write ledger.
        touched_ids = sorted(set(item["master_item_id"] for item in items))
        for master_item_id in touched_ids:
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
            doc_items_for_mi = [i for i in items if i["master_item_id"] == master_item_id]
            qty_in_total = sum(i["qty_in"] for i in doc_items_for_mi)
            this_doc_cost_price = doc_items_for_mi[-1]["cost_price"]

            costing = _recompute_master_item_costing(con, master_item_id)
            new_stock_qty = mi["stock_qty"] + qty_in_total
            first_received = mi["first_received_date"] or received_date

            con.execute(
                "UPDATE master_items SET avg_cost_price=?, stock_qty=?, last_cost_price=?, first_received_date=? WHERE id=?",
                (costing["avg_cost_price"], new_stock_qty, this_doc_cost_price, first_received, master_item_id),
            )
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at)
                   VALUES (?, 'IN', ?, ?, 'inbound_document', ?, NULL, ?)""",
                (master_item_id, qty_in_total, new_stock_qty, document_id, _now()),
            )

        # Step 9: recompute total_value from the actual posted items.
        total_row = con.execute(
            "SELECT SUM(qty_in * cost_price) AS total FROM inbound_items WHERE document_id=?", (document_id,)
        ).fetchone()
        con.execute(
            "UPDATE inbound_documents SET total_value=? WHERE id=?", (total_row["total"] or 0, document_id)
        )

        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


def void_inbound_document(document_id: int, reason: str) -> dict:
    with _conn() as con:
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Inbound document {document_id} not found")
        if header["status"] == "void":
            raise ValueError(f"Inbound document {document_id} is already void")

        if header["status"] == "draft":
            con.execute("UPDATE inbound_documents SET status='void' WHERE id=?", (document_id,))
            updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
            return dict(updated)

        # Posted void: reject unless every item's stock is fully intact.
        items = [
            dict(r)
            for r in con.execute(
                """SELECT ii.*, mi.sku FROM inbound_items ii
                   JOIN master_items mi ON mi.id = ii.master_item_id
                   WHERE ii.document_id=?""",
                (document_id,),
            ).fetchall()
        ]
        for item in items:
            if item["qty_remaining"] != item["qty_in"]:
                raise ValueError(
                    f"Cannot void document {document_id}: item SKU {item['sku']} has already had stock "
                    f"consumed (qty_remaining={item['qty_remaining']} != qty_in={item['qty_in']})"
                )

        for item in items:
            master_item_id = item["master_item_id"]
            con.execute("UPDATE inbound_items SET qty_remaining=0 WHERE id=?", (item["id"],))
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
            new_stock_qty = mi["stock_qty"] - item["qty_in"]
            costing = _recompute_master_item_costing(con, master_item_id)
            con.execute(
                "UPDATE master_items SET stock_qty=?, avg_cost_price=? WHERE id=?",
                (new_stock_qty, costing["avg_cost_price"], master_item_id),
            )
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at)
                   VALUES (?, 'VOID', ?, ?, 'inbound_document', ?, ?, ?)""",
                (master_item_id, -item["qty_in"], new_stock_qty, document_id, reason, _now()),
            )

        con.execute("UPDATE inbound_documents SET status='void' WHERE id=?", (document_id,))
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


# --- Stock adjustments / ledger ---------------------------------------


def create_stock_adjustment(master_item_id: int, qty_delta: int, reason: str) -> dict:
    with _conn() as con:
        mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if mi is None:
            raise ValueError(f"Master item {master_item_id} not found")
        new_stock_qty = mi["stock_qty"] + qty_delta
        if new_stock_qty < 0:
            raise ValueError(
                f"Adjustment would result in negative stock ({new_stock_qty}) for SKU {mi['sku']}"
            )
        con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_stock_qty, master_item_id))
        con.execute(
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at)
               VALUES (?, 'ADJUSTMENT', ?, ?, 'manual', NULL, ?, ?)""",
            (master_item_id, qty_delta, new_stock_qty, reason, _now()),
        )
        return {"master_item_id": master_item_id, "qty_delta": qty_delta, "stock_qty": new_stock_qty}


def get_stock_ledger(master_item_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM stock_ledger WHERE master_item_id=? ORDER BY id ASC", (master_item_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Purchase invoices (faktur pembelian / accounts payable) ----------------
# A supplier's bill, recorded as-is. Deliberately does NOT touch stock or
# costing — goods movement stays the inbound document's job (§ Barang Masuk),
# so an invoice can be entered before, after, or without a matching delivery.


def _next_purchase_invoice_number(con: sqlite3.Connection, today_str: str) -> str:
    prefix = f"FP-{today_str}-"
    row = con.execute(
        "SELECT invoice_number FROM purchase_invoices WHERE invoice_number LIKE ? ORDER BY invoice_number DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = int(row["invoice_number"].rsplit("-", 1)[-1]) + 1 if row is not None else 1
    return f"{prefix}{seq:04d}"


def compute_purchase_invoice_totals(items: list[dict], discount: int, tax_rate: float) -> dict:
    """Single source of truth for invoice money. Always recomputed server-side
    from the line items — the client's totals are display-only and never stored."""
    subtotal = sum(int(i["qty"]) * int(i["unit_price"]) for i in items)
    discount = max(0, min(int(discount), subtotal))
    taxable = subtotal - discount
    tax_amount = int(round(taxable * float(tax_rate) / 100))
    return {
        "subtotal": subtotal,
        "discount": discount,
        "tax_rate": float(tax_rate),
        "tax_amount": tax_amount,
        "total": taxable + tax_amount,
    }


def compute_payment_status(total: int, amount_paid: int) -> str:
    """'lunas' | 'sebagian' | 'belum' — derived, never stored, so it can't drift
    away from amount_paid the way a written-down column would."""
    if amount_paid <= 0:
        return "belum"
    if amount_paid >= total:
        return "lunas"
    return "sebagian"


def _insert_purchase_invoice_items(con: sqlite3.Connection, invoice_id: int, items: list[dict]) -> None:
    con.executemany(
        """INSERT INTO purchase_invoice_items
           (invoice_id, master_item_id, product_name, sku, unit, qty, unit_price, line_total, line_no)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                invoice_id,
                item.get("master_item_id"),
                item["product_name"],
                item.get("sku"),
                item.get("unit") or "",
                int(item["qty"]),
                int(item["unit_price"]),
                int(item["qty"]) * int(item["unit_price"]),
                line_no,
            )
            for line_no, item in enumerate(items, start=1)
        ],
    )


def _supplier_snapshot(con: sqlite3.Connection, supplier_id: int) -> dict:
    """Contact details are copied onto the invoice at save time so a later edit
    to the supplier master can't silently rewrite an already-printed document."""
    row = con.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if row is None:
        raise ValueError(f"Supplier {supplier_id} not found")
    return {
        "supplier_name_snapshot": row["name"],
        "supplier_phone_snapshot": row["phone"],
        "supplier_address_snapshot": row["address"],
        "supplier_contact_snapshot": row["contact_person"],
    }


def create_purchase_invoice(
    supplier_id: int,
    invoice_date: str,
    due_date: str,
    payment_terms: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    discount: int,
    tax_rate: float,
    items: list[dict],
) -> dict:
    now = _now()
    totals = compute_purchase_invoice_totals(items, discount, tax_rate)
    with _conn() as con:
        snapshot = _supplier_snapshot(con, supplier_id)
        invoice_number = _next_purchase_invoice_number(con, date.today().strftime("%Y%m%d"))
        cur = con.execute(
            """INSERT INTO purchase_invoices
               (invoice_number, supplier_id, supplier_invoice_no, supplier_name_snapshot,
                supplier_phone_snapshot, supplier_address_snapshot, supplier_contact_snapshot,
                invoice_date, payment_terms, due_date, subtotal, discount, tax_rate, tax_amount,
                total, amount_paid, status, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'draft', ?, ?, ?)""",
            (
                invoice_number,
                supplier_id,
                supplier_invoice_no,
                snapshot["supplier_name_snapshot"],
                snapshot["supplier_phone_snapshot"],
                snapshot["supplier_address_snapshot"],
                snapshot["supplier_contact_snapshot"],
                invoice_date,
                payment_terms,
                due_date,
                totals["subtotal"],
                totals["discount"],
                totals["tax_rate"],
                totals["tax_amount"],
                totals["total"],
                notes,
                now,
                now,
            ),
        )
        invoice_id = cur.lastrowid
        _insert_purchase_invoice_items(con, invoice_id, items)
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(row)


def update_purchase_invoice(
    invoice_id: int,
    supplier_id: int,
    invoice_date: str,
    due_date: str,
    payment_terms: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    discount: int,
    tax_rate: float,
    items: list[dict],
) -> dict:
    totals = compute_purchase_invoice_totals(items, discount, tax_rate)
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Purchase invoice {invoice_id} not found")
        if row["status"] != "draft":
            raise ValueError(
                f"Cannot edit purchase invoice {invoice_id}: status is '{row['status']}', "
                "only draft invoices can be edited"
            )
        snapshot = _supplier_snapshot(con, supplier_id)
        con.execute(
            """UPDATE purchase_invoices
               SET supplier_id=?, supplier_invoice_no=?, supplier_name_snapshot=?,
                   supplier_phone_snapshot=?, supplier_address_snapshot=?, supplier_contact_snapshot=?,
                   invoice_date=?, payment_terms=?, due_date=?, subtotal=?, discount=?, tax_rate=?,
                   tax_amount=?, total=?, notes=?, updated_at=?
               WHERE id=?""",
            (
                supplier_id,
                supplier_invoice_no,
                snapshot["supplier_name_snapshot"],
                snapshot["supplier_phone_snapshot"],
                snapshot["supplier_address_snapshot"],
                snapshot["supplier_contact_snapshot"],
                invoice_date,
                payment_terms,
                due_date,
                totals["subtotal"],
                totals["discount"],
                totals["tax_rate"],
                totals["tax_amount"],
                totals["total"],
                notes,
                _now(),
                invoice_id,
            ),
        )
        con.execute("DELETE FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,))
        _insert_purchase_invoice_items(con, invoice_id, items)
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def get_purchase_invoice(invoice_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT pi.*, s.name AS supplier_name, s.code AS supplier_code, s.npwp AS supplier_npwp
               FROM purchase_invoices pi
               JOIN suppliers s ON s.id = pi.supplier_id
               WHERE pi.id=?""",
            (invoice_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_purchase_invoice_items(invoice_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT pii.*, mi.sku AS current_sku, mi.stock_qty AS current_stock_qty
               FROM purchase_invoice_items pii
               LEFT JOIN master_items mi ON mi.id = pii.master_item_id
               WHERE pii.invoice_id=?
               ORDER BY pii.line_no ASC, pii.id ASC""",
            (invoice_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_purchase_invoices(
    status: str | None = None,
    supplier_id: int | None = None,
    payment_status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    with _conn() as con:
        query = """SELECT pi.*, s.name AS supplier_name FROM purchase_invoices pi
                   JOIN suppliers s ON s.id = pi.supplier_id WHERE 1=1"""
        params: list = []
        if status:
            query += " AND pi.status=?"
            params.append(status)
        if supplier_id is not None:
            query += " AND pi.supplier_id=?"
            params.append(supplier_id)
        if search:
            query += """ AND (pi.invoice_number LIKE ? OR IFNULL(pi.supplier_invoice_no,'') LIKE ?
                              OR s.name LIKE ?)"""
            params.extend([f"%{search}%"] * 3)
        query += " ORDER BY pi.invoice_date DESC, pi.id DESC"
        rows = [dict(r) for r in con.execute(query, params).fetchall()]

    # payment_status is derived, so it can't be pushed into the SQL WHERE above.
    if payment_status:
        rows = [
            r for r in rows if compute_payment_status(r["total"], r["amount_paid"]) == payment_status
        ]
    return rows


def post_purchase_invoice(invoice_id: int) -> dict:
    """draft -> posted. Locks the invoice against edits and makes it count as a
    real payable. No stock or costing side effects by design."""
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Purchase invoice {invoice_id} not found")
        if row["status"] != "draft":
            raise ValueError(f"Purchase invoice {invoice_id} is already '{row['status']}'")
        item_count = con.execute(
            "SELECT COUNT(*) AS n FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,)
        ).fetchone()["n"]
        if item_count == 0:
            raise ValueError(f"Cannot post purchase invoice {invoice_id}: it has no line items")
        con.execute(
            "UPDATE purchase_invoices SET status='posted', updated_at=? WHERE id=?", (_now(), invoice_id)
        )
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def void_purchase_invoice(invoice_id: int, reason: str) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Purchase invoice {invoice_id} not found")
        if row["status"] == "void":
            raise ValueError(f"Purchase invoice {invoice_id} is already void")
        if row["amount_paid"] > 0:
            raise ValueError(
                f"Cannot void purchase invoice {invoice_id}: {row['amount_paid']} already recorded as paid. "
                "Reverse the payment first."
            )
        con.execute(
            "UPDATE purchase_invoices SET status='void', void_reason=?, updated_at=? WHERE id=?",
            (reason, _now(), invoice_id),
        )
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def delete_purchase_invoice(invoice_id: int) -> None:
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Purchase invoice {invoice_id} not found")
        if row["status"] != "draft":
            raise ValueError(
                f"Cannot delete purchase invoice {invoice_id}: status is '{row['status']}'. "
                "Void it instead so the number stays on record."
            )
        con.execute("DELETE FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,))
        con.execute("DELETE FROM purchase_invoices WHERE id=?", (invoice_id,))


def record_purchase_invoice_payment(invoice_id: int, amount: int) -> dict:
    """Adds to amount_paid. A negative amount reverses an over-recorded payment;
    the running total is clamped to [0, total] either way."""
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Purchase invoice {invoice_id} not found")
        if row["status"] != "posted":
            raise ValueError(
                f"Cannot record payment on purchase invoice {invoice_id}: status is '{row['status']}', "
                "only posted invoices accept payments"
            )
        if amount == 0:
            raise ValueError("Payment amount must not be zero")
        new_paid = row["amount_paid"] + amount
        if new_paid < 0:
            raise ValueError("Payment reversal is larger than the amount recorded as paid")
        if new_paid > row["total"]:
            raise ValueError(
                f"Payment exceeds the outstanding balance ({row['total'] - row['amount_paid']})"
            )
        con.execute(
            "UPDATE purchase_invoices SET amount_paid=?, updated_at=? WHERE id=?",
            (new_paid, _now(), invoice_id),
        )
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def get_purchase_payable_summary() -> dict:
    """Totals across posted, non-void invoices — what the shop still owes."""
    today = date.today().isoformat()
    with _conn() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT total, amount_paid, due_date FROM purchase_invoices WHERE status='posted'"
            ).fetchall()
        ]
    outstanding = sum(r["total"] - r["amount_paid"] for r in rows)
    unpaid = [r for r in rows if r["total"] - r["amount_paid"] > 0]
    overdue = [r for r in unpaid if r["due_date"] < today]
    return {
        "posted_count": len(rows),
        "unpaid_count": len(unpaid),
        "outstanding_total": outstanding,
        "overdue_count": len(overdue),
        "overdue_total": sum(r["total"] - r["amount_paid"] for r in overdue),
    }


# --- Users / sessions ---------------------------------------------------


def count_users() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username: str, password_hash: str, role: str = "staff") -> dict:
    username_normalized = username.strip().lower()
    created_at = _now()
    with _conn() as con:
        dup = con.execute("SELECT 1 FROM users WHERE username_normalized=?", (username_normalized,)).fetchone()
        if dup is not None:
            raise ValueError(f"Username '{username}' already exists")
        cur = con.execute(
            """INSERT INTO users (username, username_normalized, password_hash, role, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (username.strip(), username_normalized, password_hash, role, created_at, created_at),
        )
        row = con.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_user_by_username(username: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE username_normalized=?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row is not None else None


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row is not None else None


def list_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def _active_admin_count(con: sqlite3.Connection, excluding_user_id: int | None = None) -> int:
    if excluding_user_id is None:
        row = con.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1").fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1 AND id!=?",
            (excluding_user_id,),
        ).fetchone()
    return row["n"]


def update_user_role(user_id: int, role: str) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"User {user_id} not found")
        if row["role"] == "admin" and role != "admin" and row["is_active"] and _active_admin_count(con, user_id) == 0:
            raise ValueError("Cannot demote the last remaining admin")
        con.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, _now(), user_id))
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(updated)


def set_user_active(user_id: int, is_active: bool) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"User {user_id} not found")
        if not is_active and row["role"] == "admin" and row["is_active"] and _active_admin_count(con, user_id) == 0:
            raise ValueError("Cannot deactivate the last remaining admin")
        con.execute("UPDATE users SET is_active=?, updated_at=? WHERE id=?", (int(is_active), _now(), user_id))
        if not is_active:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(updated)


def reset_user_password(user_id: int, password_hash: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"User {user_id} not found")
        con.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (password_hash, _now(), user_id))
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def create_session_row(session_id: str, user_id: int, expires_at: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, _now(), expires_at),
        )


def get_session(session_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT s.id, s.user_id, s.expires_at, u.username, u.role, u.is_active
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.id=?""",
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def delete_session(session_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def delete_sessions_for_user(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions() -> int:
    with _conn() as con:
        cur = con.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        return cur.rowcount


# ── Activity log ─────────────────────────────────────────────────────────────
def insert_activity(row: dict) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO activity_log
               (created_at, user_id, username, method, path, status_code,
                action, entity, entity_id, summary, payload, ip)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row.get("created_at") or _now(),
                row.get("user_id"),
                row.get("username"),
                row["method"],
                row["path"],
                row["status_code"],
                row["action"],
                row.get("entity"),
                row.get("entity_id"),
                row.get("summary"),
                row.get("payload"),
                row.get("ip"),
            ),
        )


def get_activity(
    page: int = 1,
    page_size: int = 50,
    username: str | None = None,
    entity: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    failures_only: bool = False,
) -> dict:
    where = []
    params: list = []
    if username:
        where.append("username = ?")
        params.append(username)
    if entity:
        where.append("entity = ?")
        params.append(entity)
    if failures_only:
        where.append("status_code >= 400")
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        # Callers pass a plain date; compare against the end of that day so the
        # whole day is included rather than only its midnight timestamp.
        where.append("created_at <= ?")
        params.append(date_to + "T23:59:59.999999+00:00")
    if search:
        where.append("(action LIKE ? OR summary LIKE ? OR path LIKE ? OR username LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) AS n FROM activity_log {clause}", params).fetchone()["n"]
        rows = con.execute(
            f"""SELECT * FROM activity_log {clause}
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_activity_actors() -> list[str]:
    """Distinct usernames present in the log, for the filter dropdown."""
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT username FROM activity_log WHERE username IS NOT NULL ORDER BY username"
        ).fetchall()
        return [r["username"] for r in rows]


def purge_activity_before(cutoff_iso: str) -> int:
    """Retention hook. Not called automatically -- the log is small and the
    history is the point -- but available for a scheduled trim."""
    with _conn() as con:
        cur = con.execute("DELETE FROM activity_log WHERE created_at < ?", (cutoff_iso,))
        return cur.rowcount
