import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz

from backend.config import SHOP_UTC_OFFSET_MINUTES
from backend.seed_brands import SEED_BRANDS

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent.parent / "data" / "scraper.db")))

LOW_STOCK_THRESHOLD = 5

_STOPWORDS = {"DAN", "THE", "OF", "FOR", "DENGAN", "UNTUK"}

# A valid customer phone: an optional leading +, then 7-15 digits once separators
# are stripped. The one definition shared by the receipt routes, the customer
# routes and the receipts->customers backfill below.
PHONE_RE = re.compile(r"^\+?\d{7,15}$")
_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def normalize_phone(raw: str | None) -> str:
    """Strip spaces, dashes, dots and parens so '(0812) 3456-7890' and '081234567890'
    validate the same way; the cleaned form is what gets stored and compared."""
    return re.sub(r"[\s\-.()]", "", raw or "")


def parse_ymd(raw: str) -> date:
    """Strict YYYY-MM-DD. date.fromisoformat alone also accepts '20261009' and ISO
    week dates on Python 3.11+, which a user typing a date never means. Raises
    ValueError for anything else, including a real-looking impossible day."""
    if not _YMD_RE.match(raw or ""):
        raise ValueError(f"Not a YYYY-MM-DD date: {raw!r}")
    return date.fromisoformat(raw)


def shop_today(offset_minutes: int | None = None) -> date:
    """Today's calendar date on the shop's wall clock. Timestamps are stored in
    UTC, so 'what day is it' needs an explicit offset (backend/config.py)."""
    offset = SHOP_UTC_OFFSET_MINUTES if offset_minutes is None else offset_minutes
    return (datetime.now(timezone.utc) + timedelta(minutes=offset)).date()


def _shop_midnight_utc_iso(day: date, offset_minutes: int) -> str:
    """The UTC instant at which `day` starts on the shop's wall clock, as an ISO
    string in the same shape created_at uses, so it compares correctly as text."""
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return local_midnight.astimezone(timezone.utc).isoformat()


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
            -- Master pelanggan. The phone is the identity (UNIQUE, stored normalised
            -- like receipts.customer_phone). Receipts keep their own phone/name
            -- snapshot and merely point here via receipts.customer_id.
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name TEXT,
                phone TEXT NOT NULL UNIQUE,
                email TEXT,
                birth_date TEXT,
                address TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                created_at TEXT NOT NULL,
                payment_method TEXT NOT NULL DEFAULT 'cash',
                customer_id INTEGER REFERENCES customers(id)
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_plate_full ON receipts(plate_full);
            CREATE INDEX IF NOT EXISTS idx_receipts_created_at ON receipts(created_at);
            CREATE TABLE IF NOT EXISTS receipt_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL REFERENCES receipts(id),
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                warranty_date TEXT,
                master_item_id INTEGER REFERENCES master_items(id)
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt_id ON receipt_items(receipt_id);

            -- Preorder (pesanan barang yang belum ada di toko). Deliberately
            -- separate from receipts and the stock ledger: nothing here moves
            -- inventory or revenue. The order is only a promise until the goods
            -- arrive, at which point Barang Masuk / Kasir take over as usual.
            CREATE TABLE IF NOT EXISTS preorders (
                id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                deposit INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_draft INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_preorders_created_at ON preorders(created_at);
            CREATE TABLE IF NOT EXISTS preorder_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preorder_id TEXT NOT NULL REFERENCES preorders(id),
                master_item_id INTEGER,
                product_name TEXT NOT NULL,
                unit TEXT,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pesanan_dibuat',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_preorder_items_preorder_id ON preorder_items(preorder_id);
            -- One row per status change. product_name is snapshotted rather than
            -- joined so the timeline still reads correctly after the line item is
            -- renamed or deleted.
            CREATE TABLE IF NOT EXISTS preorder_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preorder_id TEXT NOT NULL,
                preorder_item_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                status TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_preorder_status_history_preorder_id
                ON preorder_status_history(preorder_id);

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
                barcode TEXT,
                image_path TEXT,
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

        # --- Additive migration: payment method ---
        # data/scraper.db already has live receipt rows created before this
        # feature existed. Every sale before now was taken in cash (transfer/QRIS
        # didn't exist as an option at the register), so the NOT NULL DEFAULT
        # 'cash' backfills them correctly with no UPDATE needed.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "payment_method" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'cash'")

        # --- Additive migration: receipt -> master customer link ---
        # data/scraper.db already holds receipts that carry only a phone/name
        # snapshot. customer_id is nullable and the snapshot columns are never
        # touched; the backfill below fills in the link (and creates the master
        # rows it needs) for every receipt that has a usable phone. The index
        # goes after the ALTER because a pre-existing receipts table has no such
        # column until this point.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "customer_id" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_receipts_customer_id ON receipts(customer_id)")
        _backfill_receipt_customers(con)

        # --- Additive migration: receipt line -> master item link (stock movement) ---
        # data/scraper.db already has live receipt_items rows created before a sale
        # could be attributed to a specific product. Those stay NULL rather than
        # being guessed at — a historical sale that can't be linked to a master
        # item must never move stock (see _receipt_item_holdings).
        receipt_item_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipt_items)").fetchall()}
        if "master_item_id" not in receipt_item_cols:
            con.execute("ALTER TABLE receipt_items ADD COLUMN master_item_id INTEGER REFERENCES master_items(id)")

        # --- Additive migration: preorder draft/archive flag ---
        # data/scraper.db already has live preorder rows created before drafts
        # existed. They're all still active orders, not parked ones, so the
        # NOT NULL DEFAULT 0 backfills them correctly with no UPDATE needed.
        preorder_cols = {row["name"] for row in con.execute("PRAGMA table_info(preorders)").fetchall()}
        if "is_draft" not in preorder_cols:
            con.execute("ALTER TABLE preorders ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")

        # --- Additive migration: in-store CODE128 barcode (10 digits) ---
        # data/scraper.db already has live master items with no barcode, or
        # with a stale 13-digit EAN-13 value from an earlier revision of this
        # feature (that symbology didn't scan reliably; see
        # _generate_barcode_for_id). No label had gone to print with an EAN-13
        # value, so those are regenerated outright rather than edited in place
        # -- a barcode has to exist, in the current format, on every item
        # before a cashier can scan any of them. Anything not already exactly
        # 10 digits (NULL, or a leftover 13-digit value) is regenerated; that
        # same check makes re-running this a no-op once every row is current.
        master_item_cols = {row["name"] for row in con.execute("PRAGMA table_info(master_items)").fetchall()}
        if "barcode" not in master_item_cols:
            con.execute("ALTER TABLE master_items ADD COLUMN barcode TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_master_items_barcode ON master_items(barcode)")
        for stale_row in con.execute(
            "SELECT id FROM master_items WHERE barcode IS NULL OR length(barcode) != 10"
        ).fetchall():
            con.execute(
                "UPDATE master_items SET barcode=? WHERE id=?",
                (_generate_barcode_for_id(stale_row["id"]), stale_row["id"]),
            )

        # --- Additive migration: optional product photo ---
        # Nullable, no backfill: every existing item simply has no photo. Holds
        # only the generated filename inside PRODUCT_IMAGE_DIR (backend/config.py),
        # never a path, so the data folder can move without rewriting rows.
        if "image_path" not in master_item_cols:
            con.execute("ALTER TABLE master_items ADD COLUMN image_path TEXT")

        seed_normalized = _normalize_name("Tanpa Merk")
        existing_seed = con.execute("SELECT 1 FROM brands WHERE code=?", ("NOB",)).fetchone()
        if existing_seed is None:
            con.execute(
                """INSERT INTO brands (code, name, is_active, created_at, name_normalized, is_system, source)
                   VALUES (?, ?, 1, ?, ?, 1, 'master')""",
                ("NOB", "Tanpa Merk", _now(), seed_normalized),
            )

        # --- Seed: initial Master Merek list ---
        # The shop's own brand list, so a fresh install isn't an empty master.
        # Matched on name_normalized, not code: an admin may have renamed or
        # recoded a brand, and re-seeding must never resurrect or duplicate it.
        # is_system stays 0 -- unlike "Tanpa Merk" these are ordinary brands an
        # admin can rename, deactivate or delete.
        for seed_brand_name in SEED_BRANDS:
            brand_seed_normalized = _normalize_name(seed_brand_name)
            existing_brand_seed = con.execute(
                "SELECT 1 FROM brands WHERE name_normalized=?", (brand_seed_normalized,)
            ).fetchone()
            if existing_brand_seed is None:
                con.execute(
                    """INSERT INTO brands (code, name, is_active, created_at, name_normalized, is_system, source)
                       VALUES (?, ?, 1, ?, ?, 0, 'master')""",
                    (
                        _generate_entity_code(con, "brands", seed_brand_name),
                        seed_brand_name,
                        _now(),
                        brand_seed_normalized,
                    ),
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


def _customer_full_name(first_name: str, last_name: str | None) -> str:
    return f"{first_name} {last_name}" if last_name else first_name


def _split_customer_name(full_name: str) -> tuple[str, str | None]:
    """'Budi Santoso Putra' -> ('Budi', 'Santoso Putra'): split on the first space.
    Only used where a single free-text name has to become first/last (the
    receipt auto-create and the backfill); the master form captures them apart."""
    parts = full_name.split(None, 1)
    return parts[0], (" ".join(parts[1].split()) if len(parts) > 1 else None)


def _placeholder_first_name(phone: str) -> str:
    """first_name for a customer created from a phone number alone (the cashier
    left the name blank, or a historical receipt never had one). The last four
    digits keep two such customers tellable apart in a list."""
    return f"Pelanggan {phone[-4:]}"


def _is_placeholder_customer(row) -> bool:
    return row["last_name"] is None and row["first_name"] == _placeholder_first_name(row["phone"])


def _backfill_receipt_customers(con: sqlite3.Connection) -> dict:
    """One-time (and safe to re-run) link of historical receipts to master
    customers. Runs inside init_db's transaction.

    Every receipt with no customer_id and a usable phone is grouped by its
    normalised phone. A customer with that phone is reused if one exists;
    otherwise one is created, named after the most recent receipt for that phone
    that carries a name (first/last split on the first space), or the placeholder
    when none ever did. Its created_at is the date of that phone's first receipt.

    Only receipts.customer_id is written: the phone/name snapshot on each receipt
    is left exactly as it was. Receipts with an empty phone (grandfathered from
    before contact capture) or a phone that fails PHONE_RE stay unlinked -- a
    master row must always satisfy the same rules as one made through the API.
    Re-running finds nothing left to link, so it is a no-op."""
    rows = con.execute(
        """SELECT id, customer_phone, customer_name, created_at FROM receipts
           WHERE customer_id IS NULL AND customer_phone != ''
           ORDER BY created_at ASC, rowid ASC"""
    ).fetchall()

    by_phone: dict[str, dict] = {}
    skipped = 0
    for row in rows:
        phone = normalize_phone(row["customer_phone"])
        if not PHONE_RE.match(phone):
            skipped += 1
            continue
        entry = by_phone.setdefault(phone, {"receipt_ids": [], "name": None, "first_seen": row["created_at"]})
        entry["receipt_ids"].append(row["id"])
        name = " ".join((row["customer_name"] or "").split())
        if name:
            # Oldest-first order means the last non-empty name seen is the newest.
            entry["name"] = name

    created = 0
    linked = 0
    for phone, entry in by_phone.items():
        master = con.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
        if master is None:
            first_name, last_name = (
                _split_customer_name(entry["name"]) if entry["name"] else (_placeholder_first_name(phone), None)
            )
            cur = con.execute(
                """INSERT INTO customers (first_name, last_name, phone, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (first_name, last_name, phone, entry["first_seen"], entry["first_seen"]),
            )
            customer_id = cur.lastrowid
            created += 1
        else:
            customer_id = master["id"]
        con.executemany(
            "UPDATE receipts SET customer_id=? WHERE id=?",
            [(customer_id, receipt_id) for receipt_id in entry["receipt_ids"]],
        )
        linked += len(entry["receipt_ids"])

    return {"customers_created": created, "receipts_linked": linked, "receipts_skipped": skipped}


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


def _resolve_master_item_id(con: sqlite3.Connection, sku: str | None) -> int | None:
    """Best-effort sku -> master_items.id lookup for a receipt line. A SKU that
    doesn't resolve (typo, hand-typed line, product not in master data) is not
    an error — the line still saves, it simply doesn't move stock."""
    if not sku or not sku.strip():
        return None
    row = con.execute("SELECT id FROM master_items WHERE sku=?", (sku.strip().upper(),)).fetchone()
    return row["id"] if row is not None else None


def _receipt_item_holdings(items: list[dict]) -> dict[int, int]:
    """Sum of quantity per master_item_id across a set of receipt items, skipping
    lines with no master item link. This is the 'holding' referenced throughout
    the stock-movement code: what a non-void receipt currently has taken off the
    shelf, per product."""
    holdings: dict[int, int] = {}
    for item in items:
        master_item_id = item.get("master_item_id")
        if master_item_id is None:
            continue
        holdings[master_item_id] = holdings.get(master_item_id, 0) + item["quantity"]
    return holdings


def _apply_receipt_stock_delta(
    con: sqlite3.Connection, receipt_id: str, before: dict[int, int], after: dict[int, int]
) -> None:
    """The single place a sale moves stock. A non-void receipt HOLDS
    sum(quantity) per master_item_id; a void or deleted receipt holds NOTHING.
    Every receipt mutation (create, item/quantity edit, void, delete) computes
    its holding before and after and calls this once — that one invariant
    covers all of those sites without special-casing any of them.

    Overselling is allowed by design (a stale count must never block a real
    sale at the register), so unlike create_stock_adjustment this never raises
    on negative stock — it always applies the delta.

    ref_id is left NULL: stock_ledger.ref_id is INTEGER but receipt ids are TEXT
    UUIDs, so it can't hold one — the receipt id goes in `note` instead."""
    touched_ids = sorted(set(before) | set(after))
    now = _now()
    for master_item_id in touched_ids:
        # Positive when the receipt's holding shrank (stock returns, e.g. a void
        # or a quantity decrease); negative when it grew (a sale, or a quantity
        # increase) — this is the change applied to master_items.stock_qty.
        stock_delta = before.get(master_item_id, 0) - after.get(master_item_id, 0)
        if stock_delta == 0:
            continue
        mi = con.execute("SELECT stock_qty FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if mi is None:
            # Master item was deleted after this line was sold against it — nothing left to move.
            continue
        new_stock_qty = mi["stock_qty"] + stock_delta
        con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_stock_qty, master_item_id))
        con.execute(
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at)
               VALUES (?, ?, ?, ?, 'receipt', NULL, ?, ?)""",
            (master_item_id, "OUT" if stock_delta < 0 else "IN", stock_delta, new_stock_qty, receipt_id, now),
        )


def _resolve_receipt_customer(
    con: sqlite3.Connection, customer_id: int | None, phone: str | None, name: str | None
) -> tuple[int | None, str, str | None]:
    """Decide which master customer a receipt belongs to and what phone/name
    snapshot it stores. Returns (customer_id, phone, name). Runs inside the
    caller's transaction so an auto-created customer commits or rolls back with
    the receipt it was created for. `phone` is already normalised and validated
    by the route; `phone`/`name` of None or '' mean "the request did not say".

    - customer_id given: that customer must exist. The snapshot is synced from
      it for whatever the request left out -- except a placeholder name, which is
      not worth copying onto a receipt.
    - no customer_id: link to the customer holding that phone, or create one
      from phone + name so the master grows from cashier use. The snapshot is
      stored exactly as typed.

    Either way, when the customer only has the placeholder name and this receipt
    supplies a real one, the master takes it -- otherwise the typed name would
    never show up, because listings display the master's name for a linked
    receipt."""
    phone = phone or ""
    name = name or None

    if customer_id is not None:
        master = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if master is None:
            raise ValueError("Pelanggan tidak ditemukan.")
        if name is None and not _is_placeholder_customer(master):
            name = _customer_full_name(master["first_name"], master["last_name"])
        phone = phone or master["phone"]
    elif not phone:
        # Grandfathered shape: the routes never let a new receipt through without a phone.
        return None, "", name
    else:
        master = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if master is None:
            first_name, last_name = _split_customer_name(name) if name else (_placeholder_first_name(phone), None)
            now = _now()
            # OR IGNORE + re-select rather than check-then-insert: two tabs saving
            # the first receipt for a brand-new phone must both end up linked to
            # the one row, not have the second hit the UNIQUE constraint and fail
            # the sale.
            con.execute(
                """INSERT OR IGNORE INTO customers (first_name, last_name, phone, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (first_name, last_name, phone, now, now),
            )
            master = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()

    if name and _is_placeholder_customer(master):
        first_name, last_name = _split_customer_name(name)
        con.execute(
            "UPDATE customers SET first_name=?, last_name=?, updated_at=? WHERE id=?",
            (first_name, last_name, _now(), master["id"]),
        )
    return master["id"], phone, name


# Name shown for a receipt's customer: the master's name when linked, else the
# receipt's own snapshot. Kept in SQL so list, detail and search share one rule.
_CUSTOMER_FULL_NAME_SQL = "TRIM(c.first_name || ' ' || COALESCE(c.last_name, ''))"
_RECEIPT_SELECT = f"""SELECT r.*,
        CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_full_name
    FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id"""


def create_receipt(
    receipt_id: str,
    plate_region: str,
    plate_number: str,
    plate_suffix: str,
    items: list[dict],
    discount: int,
    amount_paid: int = 0,
    status: str | None = None,
    customer_phone: str | None = "",
    customer_name: str | None = None,
    started_at: str | None = None,
    payment_method: str = "cash",
    customer_id: int | None = None,
) -> dict:
    plate_full = f"{plate_region} {plate_number} {plate_suffix}"
    subtotal = sum(item["quantity"] * item["unit_price"] for item in items)
    total = subtotal - discount
    final_status = _compute_receipt_status(total, amount_paid, status)
    created_at = _now()
    started_at = _sanitize_started_at(started_at, created_at)

    with _conn() as con:
        customer_id, customer_phone, customer_name = _resolve_receipt_customer(
            con, customer_id, customer_phone, customer_name
        )
        con.execute(
            """INSERT INTO receipts
               (id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, status, amount_paid, payment_method, customer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, final_status, amount_paid, payment_method,
                customer_id,
            ),
        )
        resolved_items = [
            {**item, "master_item_id": _resolve_master_item_id(con, item.get("sku"))} for item in items
        ]
        con.executemany(
            """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    receipt_id, item["product_name"], item["quantity"], item["unit_price"], item.get("warranty_date"),
                    item["master_item_id"],
                )
                for item in resolved_items
            ],
        )

        # A brand-new receipt has no prior holding; a receipt created straight
        # into 'void' (the only status a caller can set directly) holds nothing.
        after_holdings = _receipt_item_holdings(resolved_items) if final_status != "void" else {}
        _apply_receipt_stock_delta(con, receipt_id, {}, after_holdings)

        customer_full_name = con.execute(
            _RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)
        ).fetchone()["customer_full_name"]

    return {
        "subtotal": subtotal, "discount": discount, "total": total, "status": final_status,
        "amount_paid": amount_paid, "customer_phone": customer_phone, "customer_name": customer_name,
        "payment_method": payment_method, "customer_id": customer_id, "customer_full_name": customer_full_name,
    }


def get_receipt(receipt_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(row) if row is not None else None


_LIKE_ESCAPE = " ESCAPE '\\'"
# A search box that holds only digits and phone punctuation is also tried as a
# phone number; anything with letters in it is a name or a plate, never a phone.
_PHONE_QUERY_RE = re.compile(r"^\+?[\d\s\-.()]+$")


def _like_escape(text: str) -> str:
    """Make the user's own % _ \\ literal inside a LIKE pattern (see _LIKE_ESCAPE)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_contains(text: str) -> str:
    return f"%{_like_escape(text)}%"


def _plate_clause(query: str) -> tuple[str, list] | None:
    """Plate search that ignores case, spaces and punctuation on both sides:
    'KT 1234 AB', 'kt1234ab' and '1234' all find plate_full 'KT 1234 AB'."""
    squashed = re.sub(r"[^A-Za-z0-9]", "", query).upper()
    if not squashed:
        return None
    return "REPLACE(r.plate_full, ' ', '') LIKE ?", [f"%{squashed}%"]


def _customer_clause(query: str) -> tuple[str, list] | None:
    """Customer search: case-insensitive substring over the linked master
    customer's full name and the receipt's own customer_name, plus the phone
    digits (receipt snapshot and master) when the query looks like a number."""
    text = " ".join(query.split())
    if not text:
        return None
    like = _like_contains(text)
    parts = [
        "r.customer_name LIKE ?" + _LIKE_ESCAPE,
        f"{_CUSTOMER_FULL_NAME_SQL} LIKE ?" + _LIKE_ESCAPE,
    ]
    params: list = [like, like]
    if _PHONE_QUERY_RE.match(text):
        digits = re.sub(r"\D", "", text)
        if digits:
            parts += ["REPLACE(r.customer_phone, '+', '') LIKE ?", "REPLACE(c.phone, '+', '') LIKE ?"]
            params += [f"%{digits}%", f"%{digits}%"]
    return "(" + " OR ".join(parts) + ")", params


def _receipt_filters(
    plate_query: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    customer: str | None,
    q: str | None,
    tz_offset_minutes: int | None,
) -> tuple[str, list]:
    """WHERE clause shared by the receipt list and its total count. Every given
    filter is ANDed; `q` is the one-box search, matching plate OR customer."""
    clauses: list[str] = []
    params: list = []

    if plate_query:
        plate = _plate_clause(plate_query)
        if plate:
            clauses.append(plate[0])
            params += plate[1]
    if customer:
        cust = _customer_clause(customer)
        if cust:
            clauses.append(cust[0])
            params += cust[1]
    if q:
        either = [c for c in (_plate_clause(q), _customer_clause(q)) if c]
        if either:
            clauses.append("(" + " OR ".join(c[0] for c in either) + ")")
            for c in either:
                params += c[1]
    if status:
        clauses.append("r.status=?")
        params.append(status)

    # Day boundaries are the shop's local midnights converted to UTC, because
    # created_at is stored as a UTC ISO string -- comparing those strings keeps
    # the created_at index usable and is correct for same-format timestamps.
    offset = SHOP_UTC_OFFSET_MINUTES if tz_offset_minutes is None else tz_offset_minutes
    if date_from:
        clauses.append("r.created_at >= ?")
        params.append(_shop_midnight_utc_iso(date.fromisoformat(date_from), offset))
    if date_to:
        clauses.append("r.created_at < ?")
        params.append(_shop_midnight_utc_iso(date.fromisoformat(date_to) + timedelta(days=1), offset))

    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def get_all_receipts(
    plate_query: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    tz_offset_minutes: int | None = None,
) -> list[dict]:
    where_sql, params = _receipt_filters(plate_query, status, date_from, date_to, customer, q, tz_offset_minutes)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params = params + [limit]

    with _conn() as con:
        # rowid breaks created_at ties so the newest-first order is stable.
        rows = con.execute(
            f"""SELECT r.*,
                   CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_full_name,
                   (SELECT COUNT(*) FROM receipt_items ri WHERE ri.receipt_id = r.id) AS item_count
               FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id
               {where_sql} ORDER BY r.created_at DESC, r.rowid DESC{limit_sql}""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def count_receipts(
    plate_query: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    q: str | None = None,
    tz_offset_minutes: int | None = None,
) -> int:
    """How many receipts the same filters match before any limit, so a client
    can tell 'these are all of them' from 'these are the newest N'."""
    where_sql, params = _receipt_filters(plate_query, status, date_from, date_to, customer, q, tz_offset_minutes)
    with _conn() as con:
        return con.execute(
            f"SELECT COUNT(*) AS n FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id {where_sql}",
            params,
        ).fetchone()["n"]


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
    payment_method: str | None = None,
    customer_id: int | None = None,
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

        # Master customer link. The snapshot above is only rewritten as a side effect of
        # re-pointing the receipt, never to chase later edits of the master record:
        #  - a customer_id different from the current one re-points it and syncs the snapshot
        #    for whatever the request did not state itself;
        #  - the same customer_id (a client echoing it back on every save) changes nothing;
        #  - a phone that differs from the stored one -- or any phone on a receipt that was
        #    never linked -- is resolved like a new receipt's: link to that phone's customer,
        #    or create one;
        #  - otherwise the link is left alone.
        new_customer_id = existing["customer_id"]
        if customer_id is not None:
            if customer_id != existing["customer_id"]:
                new_customer_id, new_customer_phone, new_customer_name = _resolve_receipt_customer(
                    con,
                    customer_id,
                    customer_phone,
                    customer_name if update_customer_name else None,
                )
        elif customer_phone is not None and (
            customer_phone != existing["customer_phone"] or existing["customer_id"] is None
        ):
            new_customer_id, new_customer_phone, new_customer_name = _resolve_receipt_customer(
                con, None, customer_phone, new_customer_name
            )

        new_amount_paid = amount_paid if amount_paid is not None else existing["amount_paid"]
        new_payment_method = payment_method if payment_method is not None else existing["payment_method"]

        # Stock: snapshot what this receipt currently holds, before anything below
        # changes — a void receipt holds nothing regardless of what its old line
        # items say, so this stays a plain [] rather than a query in that case.
        existing_items = [dict(r) for r in con.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
        ).fetchall()]
        before_holdings = _receipt_item_holdings(existing_items) if existing["status"] != "void" else {}

        if items is not None:
            resolved_items = [
                {**item, "master_item_id": _resolve_master_item_id(con, item.get("sku"))} for item in items
            ]
            con.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
            con.executemany(
                """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        receipt_id, item["product_name"], item["quantity"], item["unit_price"],
                        item.get("warranty_date"), item["master_item_id"],
                    )
                    for item in resolved_items
                ],
            )
            effective_items = resolved_items
        elif discount is not None:
            effective_items = existing_items
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
               customer_phone=?, customer_name=?, customer_id=?, subtotal=?, discount=?, total=?, status=?,
               amount_paid=?, payment_method=? WHERE id=?""",
            (
                new_plate_region, new_plate_number, new_plate_suffix, new_plate_full,
                new_customer_phone, new_customer_name, new_customer_id,
                new_subtotal, new_discount, new_total, new_status, new_amount_paid, new_payment_method, receipt_id,
            ),
        )

        # Stock: holding after this change is nothing if the receipt ends up void
        # (covers a fresh void here, and keeps a receipt that was already void
        # from re-applying anything even if its items/discount were also sent in
        # the same request); otherwise it's whatever items are in effect now —
        # the ones just written, or the untouched existing ones.
        after_items = effective_items if effective_items is not None else existing_items
        after_holdings = _receipt_item_holdings(after_items) if new_status != "void" else {}
        _apply_receipt_stock_delta(con, receipt_id, before_holdings, after_holdings)

        updated = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(updated)


def settle_receipt(receipt_id: str, payment_method: str | None = None) -> dict:
    """Mark a pending receipt paid in full: the remaining balance is recorded as
    paid (amount_paid = total), which makes the computed status 'done'. Touches
    only the payment columns -- items, totals and stock are exactly as they were.
    A void receipt stays void and an already-done one has nothing left to settle,
    so both are refused rather than silently re-saved."""
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Struk tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Struk sudah dibatalkan (void), jadi tidak bisa ditandai selesai.")
        if row["status"] == "done":
            raise ValueError("Struk ini sudah selesai (lunas), tidak ada sisa pembayaran.")
        # status='pending' in the WHERE makes this a compare-and-set: a double click (or
        # two tills) that both read 'pending' above cannot both win -- the loser matches
        # no row and is told the receipt is already done instead of reporting a second success.
        cur = con.execute(
            "UPDATE receipts SET amount_paid=?, status=?, payment_method=? WHERE id=? AND status='pending'",
            (
                row["total"],
                _compute_receipt_status(row["total"], row["total"], None),
                payment_method if payment_method is not None else row["payment_method"],
                receipt_id,
            ),
        )
        if cur.rowcount == 0:
            raise ValueError("Struk ini sudah selesai (lunas), tidak ada sisa pembayaran.")
        updated = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(updated)


def get_receipt_items(receipt_id: str) -> list[dict]:
    with _conn() as con:
        # LEFT JOIN so a line with no master item link (unresolved/hand-typed
        # SKU, or a historical row from before this link existed) still returns,
        # just with sku=NULL — reopening it for edit keeps that link, or lack of
        # one, as-is.
        rows = con.execute(
            """SELECT ri.*, mi.sku AS sku FROM receipt_items ri
               LEFT JOIN master_items mi ON mi.id = ri.master_item_id
               WHERE ri.receipt_id=? ORDER BY ri.id ASC""",
            (receipt_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_receipt(receipt_id: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Receipt {receipt_id} not found")
        existing = dict(row)

        # Stock: deleting a receipt that still held stock returns it, same as a
        # void — but a receipt already void holds nothing, so this is a no-op
        # for it (no double-restore).
        if existing["status"] != "void":
            items = [dict(r) for r in con.execute(
                "SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)
            ).fetchall()]
            before_holdings = _receipt_item_holdings(items)
            _apply_receipt_stock_delta(con, receipt_id, before_holdings, {})

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
# Master Pelanggan (customers)
# ---------------------------------------------------------------------------
# Deliberately not "one function per table with is_system/code" like suppliers
# and brands: a customer is identified by phone, has no generated code, and no
# row is system-owned. Delete is blocked while a receipt points at the customer
# (same rule as suppliers/brands: SQLite runs here without FK enforcement, so a
# delete would silently orphan the receipts); deactivating is the way to retire
# someone who has history.

_CUSTOMER_SELECT = """SELECT c.*,
        (SELECT COUNT(*) FROM receipts r WHERE r.customer_id = c.id) AS receipt_count,
        (SELECT MAX(r.created_at) FROM receipts r WHERE r.customer_id = c.id) AS last_visit
    FROM customers c"""


def _customer_row(row) -> dict:
    d = dict(row)
    d["full_name"] = _customer_full_name(d["first_name"], d["last_name"])
    return d


def _duplicate_phone_message(phone: str, holder) -> str:
    name = _customer_full_name(holder["first_name"], holder["last_name"])
    if holder["is_active"]:
        return f"Nomor HP {phone} sudah terdaftar atas nama {name}."
    return f"Nomor HP {phone} sudah terdaftar atas nama {name} (nonaktif). Aktifkan kembali pelanggan tersebut atau pakai nomor lain."


def _customer_search_clause(text: str) -> tuple[str, list]:
    """Name (first, last, or the two together), email, or phone digits contain
    `text`, case-insensitively for ASCII (SQLite LIKE). `text` is non-empty."""
    like = _like_contains(text)
    parts = [
        "c.first_name LIKE ?" + _LIKE_ESCAPE,
        "COALESCE(c.last_name, '') LIKE ?" + _LIKE_ESCAPE,
        f"{_CUSTOMER_FULL_NAME_SQL} LIKE ?" + _LIKE_ESCAPE,
        "COALESCE(c.email, '') LIKE ?" + _LIKE_ESCAPE,
    ]
    params: list = [like, like, like, like]
    phone = normalize_phone(text)
    if phone:
        parts.append("c.phone LIKE ?" + _LIKE_ESCAPE)
        params.append(_like_contains(phone))
    return "(" + " OR ".join(parts) + ")", params


def create_customer(
    first_name: str,
    last_name: str | None,
    phone: str,
    email: str | None = None,
    birth_date: str | None = None,
    address: str | None = None,
) -> dict:
    now = _now()
    with _conn() as con:
        holder = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if holder is not None:
            raise ValueError(_duplicate_phone_message(phone, holder))
        try:
            cur = con.execute(
                """INSERT INTO customers
                   (first_name, last_name, phone, email, birth_date, address, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (first_name, last_name, phone, email, birth_date, address, now, now),
            )
        except sqlite3.IntegrityError:
            # Lost a race with another writer between the check above and the insert.
            raise ValueError(f"Nomor HP {phone} sudah terdaftar atas nama pelanggan lain.")
        row = con.execute(_CUSTOMER_SELECT + " WHERE c.id=?", (cur.lastrowid,)).fetchone()
        return _customer_row(row)


def get_customer(customer_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(_CUSTOMER_SELECT + " WHERE c.id=?", (customer_id,)).fetchone()
        return _customer_row(row) if row is not None else None


def update_customer(customer_id: int, changes: dict) -> dict:
    """`changes` holds only the fields the request actually sent (already cleaned
    and validated by the route), so an omitted field stays as it is while an
    explicit None clears a nullable one."""
    with _conn() as con:
        row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise ValueError("Pelanggan tidak ditemukan.")
        merged = {**dict(row), **changes}

        if merged["phone"] != row["phone"]:
            holder = con.execute(
                "SELECT * FROM customers WHERE phone=? AND id!=?", (merged["phone"], customer_id)
            ).fetchone()
            if holder is not None:
                raise ValueError(_duplicate_phone_message(merged["phone"], holder))

        try:
            con.execute(
                """UPDATE customers SET first_name=?, last_name=?, phone=?, email=?, birth_date=?, address=?,
                   is_active=?, updated_at=? WHERE id=?""",
                (
                    merged["first_name"], merged["last_name"], merged["phone"], merged["email"],
                    merged["birth_date"], merged["address"], int(bool(merged["is_active"])), _now(), customer_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Nomor HP {merged['phone']} sudah terdaftar atas nama pelanggan lain.")
        updated = con.execute(_CUSTOMER_SELECT + " WHERE c.id=?", (customer_id,)).fetchone()
        return _customer_row(updated)


def list_customers(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    is_active: bool | None = None,
) -> dict:
    offset = (page - 1) * page_size
    where_clauses: list[str] = []
    params: list = []
    text = " ".join((search or "").split())
    if text:
        clause, clause_params = _customer_search_clause(text)
        where_clauses.append(clause)
        params += clause_params
    if is_active is not None:
        where_clauses.append("c.is_active=?")
        params.append(1 if is_active else 0)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) AS n FROM customers c {where_sql}", params).fetchone()["n"]
        rows = con.execute(
            f"""{_CUSTOMER_SELECT} {where_sql}
                ORDER BY c.first_name COLLATE NOCASE, c.last_name COLLATE NOCASE, c.id
                LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()
        return {"items": [_customer_row(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def autocomplete_customers(query: str, limit: int = 8) -> list[dict]:
    """Small rows (id, full_name, phone) for the cashier's picker. Active
    customers only -- a deactivated one must not be pickable -- with names or
    numbers that START with the query listed ahead of ones that merely contain it."""
    text = " ".join(query.split())
    clause, params = _customer_search_clause(text)
    prefix = f"{_like_escape(text)}%"
    phone_prefix = f"{_like_escape(normalize_phone(text))}%" if normalize_phone(text) else prefix
    with _conn() as con:
        rows = con.execute(
            f"""SELECT c.id, {_CUSTOMER_FULL_NAME_SQL} AS full_name, c.phone
                FROM customers c
                WHERE c.is_active=1 AND {clause}
                ORDER BY CASE WHEN {_CUSTOMER_FULL_NAME_SQL} LIKE ?{_LIKE_ESCAPE}
                                OR c.phone LIKE ?{_LIKE_ESCAPE} THEN 0 ELSE 1 END,
                         c.first_name COLLATE NOCASE, c.last_name COLLATE NOCASE, c.id
                LIMIT ?""",
            params + [prefix, phone_prefix, limit],
        ).fetchall()
        return [dict(r) for r in rows]


def delete_customer(customer_id: int) -> None:
    with _conn() as con:
        row = con.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise ValueError("Pelanggan tidak ditemukan.")
        in_use = con.execute("SELECT COUNT(*) AS n FROM receipts WHERE customer_id=?", (customer_id,)).fetchone()["n"]
        if in_use:
            raise ValueError(
                f"Pelanggan masih dipakai oleh {in_use} nota dan tidak bisa dihapus. "
                "Nonaktifkan saja jika sudah tidak dipakai."
            )
        con.execute("DELETE FROM customers WHERE id=?", (customer_id,))


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
# In-store barcodes, CODE128 symbology: the shop's original CODE128-encoded SKU
# labels came out with bars too thin for its 203dpi thermal printer/scanner
# combo to read reliably. Switching the *value* to EAN-13 didn't fix it either
# (and CODE128 support on that scanner was confirmed working via a label from
# another shop) — the real fix is a shorter, even-length all-digit payload, so
# CODE128's Code C subset can pack two digits per 11 modules instead of falling
# back to a wider subset. A 10-digit code is what buys the noticeably wider
# bars this whole exercise is for.
#
# Digit count is load-bearing: it must stay EVEN (Code C requires an
# even-length numeric payload) and distinct from a real product's own EAN-8 (8
# digits) or EAN-13 (13 digits) so a scanned real barcode is never mistaken for
# one of ours. Do not "tidy" this to 9 or 11 digits.
#
# No trailing check digit: CODE128 carries its own internal checksum, so an
# extra EAN-style check digit here would just be dead weight on the label.


def _generate_barcode_for_id(master_item_id: int) -> str:
    """Deterministic 10-digit CODE128 value from a master_items.id: "20" + id
    zero-padded to 8 digits. Deriving it from the row id — rather than a
    separate counter — makes it unique with no extra state, and reproducible
    if it ever needs regenerating."""
    if master_item_id >= 10**8:
        raise ValueError(f"master_item_id {master_item_id} too large to encode in an 8-digit barcode segment")
    return "20" + str(master_item_id).zfill(8)


def create_master_item(
    supplier_id: int, brand_id: int, name: str, unit: str, sell_price: int = 0, cost_price: int = 0,
    stock_qty: int = 0,
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?)""",
            (
                sku, prefix, seq, supplier_id, brand_id, name, name_normalized, unit,
                cost_price, cost_price, sell_price, stock_qty, created_at,
            ),
        )
        # The row id isn't known until after INSERT, so the barcode is generated
        # and written back in the same transaction as the insert.
        master_item_id = cur.lastrowid
        con.execute(
            "UPDATE master_items SET barcode=? WHERE id=?",
            (_generate_barcode_for_id(master_item_id), master_item_id),
        )
        if stock_qty > 0:
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at)
                   VALUES (?, 'ADJUSTMENT', ?, ?, 'manual', NULL, 'Stok awal', ?)""",
                (master_item_id, stock_qty, stock_qty, created_at),
            )
        row = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
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


def delete_master_item(master_item_id: int) -> str | None:
    """Hard-deletes a master item. Only allowed when it has no receipt or stock
    history — deleting a referenced product would orphan stock_ledger rows and
    corrupt costing. Products that have ever been received should be kept.

    Returns the deleted row's image filename (or None) so the caller can remove
    the file once the delete has actually committed; read in the same transaction
    as the DELETE (BEGIN IMMEDIATE) so a concurrent photo replace can't slip a
    new file past it."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT id, image_path FROM master_items WHERE id=?", (master_item_id,)).fetchone()
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
        return row["image_path"]


def set_master_item_image_path(master_item_id: int, image_path: str | None) -> str | None:
    """Points the item at a new photo file (None clears it) and returns the
    filename it pointed at before, so the caller can delete the replaced file.
    BEGIN IMMEDIATE takes the write lock before the read, so two concurrent
    replaces can't both see the same "previous" and leave a file orphaned."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT image_path FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Master item {master_item_id} not found")
        con.execute("UPDATE master_items SET image_path=? WHERE id=?", (image_path, master_item_id))
        return row["image_path"]


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


def get_master_item_by_barcode(barcode: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.barcode=? AND mi.is_active=1""",
            (barcode,),
        ).fetchone()
        return dict(row) if row is not None else None


def get_master_item_by_code(code: str) -> dict | None:
    """What the cashier's scanner/keyboard actually sends could be either the
    10-digit CODE128 barcode or a hand-typed/old SKU. A barcode is only ever
    numeric, so it's tried first when the code looks like one; SKU is always
    the fallback, which keeps old SKU-encoded labels and typed SKUs resolving
    unchanged."""
    if code.isdigit():
        by_barcode = get_master_item_by_barcode(code)
        if by_barcode is not None:
            return by_barcode
    return get_master_item_by_sku(code)


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

        # Open preorders. The order-level stage is derived from its line items
        # (see _preorder_overall_status), so this is assembled in Python rather
        # than filtered in SQL -- the same approach get_all_preorders takes.
        preorder_items_by_order: dict[str, list[dict]] = {}
        for preorder_item in con.execute(
            "SELECT * FROM preorder_items ORDER BY position ASC, id ASC"
        ).fetchall():
            preorder_items_by_order.setdefault(preorder_item["preorder_id"], []).append(dict(preorder_item))
        preorders_open = [
            _preorder_row(dict(r), preorder_items_by_order.get(r["id"], []))
            for r in con.execute("SELECT * FROM preorders ORDER BY created_at DESC").fetchall()
        ]
        # Fully installed orders are finished business and drop off the dashboard.
        preorders_open = [r for r in preorders_open if r["status"] != PREORDER_STATUSES[-1]]

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
            "preorder_open_count": len(preorders_open),
            "preorder_outstanding": sum(r["remaining"] for r in preorders_open),
            # Goods already sitting in the shop waiting to be installed -- the
            # stage that actually needs someone to act.
            "preorder_ready_count": sum(
                1 for r in preorders_open
                if any(i["status"] == "sampai_di_toko" for i in preorder_items_by_order.get(r["id"], []))
            ),
            "preorders": preorders_open[:5],
        }


def autocomplete_master_items(
    query: str, supplier_id: int | None = None, brand_id: int | None = None, limit: int = 20
) -> list[dict]:
    """Matches against name OR sku OR barcode (barcode may be NULL on a row that
    somehow missed the backfill -- LIKE/`=` against NULL is simply never true, so
    it never breaks the filter or the sort). LIKE is SQLite's default
    case-insensitive-for-ASCII behaviour, same as the plain name-only match this
    replaced; the CASE keys use UPPER() to keep the same case-insensitivity
    explicit for the `=` comparisons, which aren't case-insensitive by default.

    Ordered so a typeahead never buries what you typed verbatim: exact
    barcode/SKU match first, then a SKU-prefix match, then everything else
    (substring name/sku/barcode hits), alphabetically by name within each
    group -- all as a SQL sort key rather than a Python sort."""
    like = f"%{query}%"
    where_clauses = ["mi.is_active=1", "(mi.name LIKE ? OR mi.sku LIKE ? OR mi.barcode LIKE ?)"]
    params: list = [like, like, like]
    if supplier_id is not None:
        where_clauses.append("mi.supplier_id=?")
        params.append(supplier_id)
    if brand_id is not None:
        where_clauses.append("mi.brand_id=?")
        params.append(brand_id)
    where_sql = " AND ".join(where_clauses)

    order_params = [query, query, f"{query}%"]
    with _conn() as con:
        rows = con.execute(
            f"""SELECT mi.*, s.name AS supplier_name, b.name AS brand_name
                FROM master_items mi
                JOIN suppliers s ON s.id = mi.supplier_id
                JOIN brands b ON b.id = mi.brand_id
                WHERE {where_sql}
                ORDER BY
                    CASE
                        WHEN UPPER(mi.barcode)=UPPER(?) OR UPPER(mi.sku)=UPPER(?) THEN 0
                        WHEN UPPER(mi.sku) LIKE UPPER(?) THEN 1
                        ELSE 2
                    END,
                    mi.name ASC
                LIMIT ?""",
            params + order_params + [limit],
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
                    con.execute(
                        "UPDATE master_items SET barcode=? WHERE id=?",
                        (_generate_barcode_for_id(master_item_id), master_item_id),
                    )

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
            # Race-guard only — the route already checks existence before calling this,
            # so this is not the normal path a cashier/admin would ever see.
            raise ValueError(f"Barang dengan id {master_item_id} tidak ditemukan.")
        new_stock_qty = mi["stock_qty"] + qty_delta
        if new_stock_qty < 0:
            raise ValueError(
                f"Stok tidak mencukupi untuk SKU {mi['sku']} (sisa akan menjadi {new_stock_qty})."
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


# --- Preorders -------------------------------------------------------------
# A preorder is a customer promise, not a transaction: it never touches
# stock_ledger, master_items.stock_qty or the sales summary. Each line item
# carries its own stage, because a single order routinely has one item already
# sitting in the shop while another is still with the supplier.

PREORDER_STATUSES = (
    "pesanan_dibuat",
    "sudah_dipesankan",
    "dalam_pengiriman",
    "sampai_di_toko",
    "terpasang",
)

PREORDER_STATUS_LABELS = {
    "pesanan_dibuat": "Pesanan dibuat",
    "sudah_dipesankan": "Sudah dipesankan",
    "dalam_pengiriman": "Dalam pengiriman",
    "sampai_di_toko": "Sampai di toko",
    "terpasang": "Terpasang",
}


def _preorder_overall_status(item_statuses: list[str]) -> str:
    """The order as a whole is only as far along as its *least* advanced line —
    an order with a pipe still in transit is not 'sampai di toko' just because
    the fittings arrived. An order with no items falls back to the first stage."""
    if not item_statuses:
        return PREORDER_STATUSES[0]
    return min(item_statuses, key=lambda s: PREORDER_STATUSES.index(s) if s in PREORDER_STATUSES else 0)


def _preorder_row(row: dict, items: list[dict]) -> dict:
    total = sum(i["quantity"] * i["unit_price"] for i in items)
    deposit = row["deposit"]
    return {
        **row,
        "total": total,
        # Overpayment would otherwise show as a negative balance owing.
        "remaining": max(total - deposit, 0),
        "item_count": len(items),
        "status": _preorder_overall_status([i["status"] for i in items]),
    }


def _insert_preorder_item(con: sqlite3.Connection, preorder_id: str, item: dict, position: int, now: str) -> int:
    cur = con.execute(
        """INSERT INTO preorder_items
           (preorder_id, master_item_id, product_name, unit, quantity, unit_price, status, position, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            preorder_id, item.get("master_item_id"), item["product_name"], item.get("unit"),
            item["quantity"], item["unit_price"], item.get("status") or PREORDER_STATUSES[0],
            position, now,
        ),
    )
    item_id = cur.lastrowid
    _log_preorder_status(con, preorder_id, item_id, item["product_name"],
                         item.get("status") or PREORDER_STATUSES[0], now, item.get("changed_by"))
    return item_id


def _log_preorder_status(
    con: sqlite3.Connection, preorder_id: str, item_id: int, product_name: str,
    status: str, changed_at: str, changed_by: str | None,
) -> None:
    con.execute(
        """INSERT INTO preorder_status_history
           (preorder_id, preorder_item_id, product_name, status, changed_at, changed_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (preorder_id, item_id, product_name, status, changed_at, changed_by),
    )


def create_preorder(
    preorder_id: str,
    customer_name: str,
    customer_phone: str,
    items: list[dict],
    deposit: int = 0,
    notes: str | None = None,
    changed_by: str | None = None,
) -> dict:
    now = _now()
    with _conn() as con:
        con.execute(
            """INSERT INTO preorders
               (id, customer_name, customer_phone, deposit, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (preorder_id, customer_name, customer_phone, deposit, notes, now, now),
        )
        for position, item in enumerate(items):
            _insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by}, position, now)
        row = dict(con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone())
        stored = [dict(r) for r in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,)
        ).fetchall()]
    return _preorder_row(row, stored)


def get_preorder(preorder_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            return None
        items = [dict(r) for r in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,)
        ).fetchall()]
    return _preorder_row(dict(row), items)


def get_preorder_items(preorder_id: str) -> list[dict]:
    """Carries the linked SKU along so the detail view can show which lines are
    tied to Master Barang; it is null for a free-text line."""
    with _conn() as con:
        rows = con.execute(
            """SELECT preorder_items.*, master_items.sku AS sku
               FROM preorder_items
               LEFT JOIN master_items ON master_items.id = preorder_items.master_item_id
               WHERE preorder_id=? ORDER BY position ASC, preorder_items.id ASC""",
            (preorder_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_preorder_history(preorder_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM preorder_status_history WHERE preorder_id=?
               ORDER BY changed_at DESC, id DESC""",
            (preorder_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_preorders(query: str | None = None, status: str | None = None, draft: bool = False) -> list[dict]:
    """`status` filters on the computed order-level stage, so it is applied in
    Python rather than SQL — the value does not exist as a column. `draft` is a
    real column, so it's applied in SQL: False (the default) is the active
    list, True is the parked/archived one — the two never mix."""
    where_clauses = ["is_draft=?"]
    params: list = [1 if draft else 0]
    if query:
        where_clauses.append(
            "(customer_name LIKE ? OR customer_phone LIKE ? OR id IN "
            "(SELECT preorder_id FROM preorder_items WHERE product_name LIKE ?))"
        )
        like = f"%{query}%"
        params.extend([like, like, like])
    where_sql = "WHERE " + " AND ".join(where_clauses)

    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM preorders {where_sql} ORDER BY created_at DESC", params
        ).fetchall()]
        items_by_order: dict[str, list[dict]] = {}
        for item in con.execute(
            "SELECT * FROM preorder_items ORDER BY position ASC, id ASC"
        ).fetchall():
            items_by_order.setdefault(item["preorder_id"], []).append(dict(item))

    result = [_preorder_row(row, items_by_order.get(row["id"], [])) for row in rows]
    if status:
        result = [r for r in result if r["status"] == status]
    return result


def update_preorder(
    preorder_id: str,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    items: list[dict] | None = None,
    deposit: int | None = None,
    notes: str | None = None,
    update_notes: bool = False,
    changed_by: str | None = None,
    is_draft: bool | None = None,
) -> dict:
    """Items are matched by id rather than deleted and re-inserted (the pattern
    receipts use), because a line item owns its stage and its status history —
    re-creating the row on every edit would silently reset both."""
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            raise ValueError(f"Preorder {preorder_id} not found")
        existing = dict(row)

        con.execute(
            """UPDATE preorders SET customer_name=?, customer_phone=?, deposit=?, notes=?, updated_at=?,
               is_draft=? WHERE id=?""",
            (
                customer_name if customer_name is not None else existing["customer_name"],
                customer_phone if customer_phone is not None else existing["customer_phone"],
                deposit if deposit is not None else existing["deposit"],
                notes if update_notes else existing["notes"],
                now,
                int(is_draft) if is_draft is not None else existing["is_draft"],
                preorder_id,
            ),
        )

        if items is not None:
            current = {
                r["id"]: dict(r) for r in con.execute(
                    "SELECT * FROM preorder_items WHERE preorder_id=?", (preorder_id,)
                ).fetchall()
            }
            kept: set[int] = set()
            for position, item in enumerate(items):
                item_id = item.get("id")
                if item_id is not None and item_id in current:
                    kept.add(item_id)
                    prev = current[item_id]
                    new_status = item.get("status") or prev["status"]
                    con.execute(
                        """UPDATE preorder_items SET master_item_id=?, product_name=?, unit=?,
                           quantity=?, unit_price=?, status=?, position=? WHERE id=?""",
                        (
                            item.get("master_item_id"), item["product_name"], item.get("unit"),
                            item["quantity"], item["unit_price"], new_status, position, item_id,
                        ),
                    )
                    if new_status != prev["status"]:
                        _log_preorder_status(con, preorder_id, item_id, item["product_name"],
                                             new_status, now, changed_by)
                else:
                    kept.add(_insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by}, position, now))

            for stale_id in set(current) - kept:
                con.execute("DELETE FROM preorder_status_history WHERE preorder_item_id=?", (stale_id,))
                con.execute("DELETE FROM preorder_items WHERE id=?", (stale_id,))

        updated = dict(con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone())
        stored = [dict(r) for r in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,)
        ).fetchall()]
    return _preorder_row(updated, stored)


def set_preorder_item_status(
    preorder_id: str, item_id: int, status: str, changed_by: str | None = None
) -> dict:
    now = _now()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM preorder_items WHERE id=? AND preorder_id=?", (item_id, preorder_id)
        ).fetchone()
        if row is None:
            raise ValueError(f"Preorder item {item_id} not found")
        item = dict(row)
        if item["status"] != status:
            con.execute("UPDATE preorder_items SET status=? WHERE id=?", (status, item_id))
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
            _log_preorder_status(con, preorder_id, item_id, item["product_name"], status, now, changed_by)
        updated = dict(con.execute("SELECT * FROM preorder_items WHERE id=?", (item_id,)).fetchone())
    return updated


# Preorders are archived as drafts (is_draft=1 via update_preorder), never
# deleted -- there is deliberately no delete_preorder function. Do not re-add one.
