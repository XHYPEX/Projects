import json
import os
import re
import sqlite3
import threading
from fractions import Fraction
import uuid
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
        raise ValueError(f"Bukan tanggal berformat YYYY-MM-DD: {raw!r}")
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
            -- One row per payment taken against a receipt ("Pembayaran Nota"): a customer
            -- can pay in instalments. receipts.amount_paid stays as the running total (every
            -- list, filter and summary reads it) and always equals the sum of these rows;
            -- an adjustment row (negative allowed) keeps that true when an edit changes
            -- amount_paid directly. Payments never touch stock.
            CREATE TABLE IF NOT EXISTS receipt_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL REFERENCES receipts(id),
                paid_at TEXT NOT NULL,
                amount INTEGER NOT NULL,
                method TEXT NOT NULL DEFAULT 'cash',
                transfer_sender TEXT,
                transfer_account TEXT,
                note TEXT,
                created_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_payments_receipt_id ON receipt_payments(receipt_id);

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
            -- Down payments, one row per payment: a customer usually pays an
            -- order off in instalments. preorders.deposit predates this table
            -- and is no longer read (see _preorder_row).
            CREATE TABLE IF NOT EXISTS preorder_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preorder_id TEXT NOT NULL REFERENCES preorders(id),
                paid_at TEXT NOT NULL,
                amount INTEGER NOT NULL,
                note TEXT,
                created_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_preorder_payments_preorder_id ON preorder_payments(preorder_id);
            -- Ledger of how much of a Sales Order's down payment was applied to which
            -- final nota. Unallocated DP = SUM(preorder_payments) - SUM(this table), so a
            -- DP can never be used twice.
            CREATE TABLE IF NOT EXISTS preorder_payment_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preorder_id TEXT NOT NULL REFERENCES preorders(id),
                receipt_id TEXT NOT NULL REFERENCES receipts(id),
                amount INTEGER NOT NULL,
                allocated_at TEXT NOT NULL,
                allocated_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ppa_preorder_id ON preorder_payment_allocations(preorder_id);
            CREATE INDEX IF NOT EXISTS idx_ppa_receipt_id ON preorder_payment_allocations(receipt_id);

            -- Purchase Order: purchase orders to a supplier, built from customer preorder
            -- lines. Like preorders they never touch stock; see the Supplier POs section.
            CREATE TABLE IF NOT EXISTS supplier_pos (
                id TEXT PRIMARY KEY,
                po_number TEXT NOT NULL UNIQUE,
                po_date TEXT NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                supplier_name_snapshot TEXT NOT NULL,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','dipesan','sebagian_diterima','diterima','batal')),
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_supplier_pos_created_at ON supplier_pos(created_at);
            CREATE TABLE IF NOT EXISTS supplier_po_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                preorder_item_id INTEGER REFERENCES preorder_items(id),
                master_item_id INTEGER,
                product_name TEXT NOT NULL,
                unit TEXT,
                quantity INTEGER NOT NULL,
                est_unit_price INTEGER NOT NULL DEFAULT 0,
                qty_received INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_supplier_po_items_po ON supplier_po_items(supplier_po_id);
            CREATE INDEX IF NOT EXISTS idx_supplier_po_items_line ON supplier_po_items(preorder_item_id);

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
            -- Master Kategori: two levels (kategori utama -> subkategori). Never deleted, only
            -- archived, so a product always keeps pointing at a real row.
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                parent_id INTEGER REFERENCES categories(id),
                status TEXT NOT NULL DEFAULT 'aktif' CHECK (status IN ('aktif','nonaktif','arsip')),
                icon TEXT,
                description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(parent_id, name_normalized)
            );
            CREATE INDEX IF NOT EXISTS idx_categories_parent_id ON categories(parent_id);
            -- Stock Opname: a counted-stock session that only changes stock once an admin approves it.
            -- Draft -> menunggu_verifikasi -> disetujui / ditolak (or dibatalkan). Nothing is deleted.
            CREATE TABLE IF NOT EXISTS stock_opnames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opname_no TEXT NOT NULL UNIQUE,
                opname_at TEXT NOT NULL,
                officer TEXT NOT NULL,
                location TEXT,
                kind TEXT NOT NULL DEFAULT 'barang_tertentu',
                note TEXT,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','menunggu_verifikasi','disetujui','ditolak','dibatalkan')),
                created_by TEXT,
                created_at TEXT NOT NULL,
                submitted_by TEXT,
                submitted_at TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                resolution_note TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stock_opnames_status ON stock_opnames(status);
            CREATE TABLE IF NOT EXISTS stock_opname_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opname_id INTEGER NOT NULL REFERENCES stock_opnames(id),
                master_item_id INTEGER NOT NULL REFERENCES master_items(id),
                system_qty INTEGER NOT NULL,
                counted_qty INTEGER,
                diff_qty INTEGER,
                reason TEXT,
                note TEXT,
                applied_delta INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_stock_opname_items_opname ON stock_opname_items(opname_id);
            CREATE INDEX IF NOT EXISTS idx_stock_opname_items_item ON stock_opname_items(master_item_id);
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
        for supplier_col in ("address", "contact_person", "npwp", "email"):
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

        # --- Additive migration: printed nota number (receipt_no) ---
        # The id is a UUID, which means nothing on paper. Existing receipts are numbered in
        # the order they were saved, so the printed series matches the shop's history.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "receipt_no" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN receipt_no TEXT")
        for unnumbered in con.execute(
            "SELECT id, created_at FROM receipts WHERE receipt_no IS NULL ORDER BY created_at ASC, rowid ASC"
        ).fetchall():
            con.execute(
                "UPDATE receipts SET receipt_no=? WHERE id=?",
                (_next_receipt_no(con, unnumbered["created_at"]), unnumbered["id"]),
            )
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_receipt_no ON receipts(receipt_no)")

        # --- Additive migration: transfer sender ---
        # For a transfer payment, the name on the account the money came from, so it can be
        # matched against the bank statement. NULL for cash/QRIS and for older receipts.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        if "transfer_sender" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN transfer_sender TEXT")
        # ...and which of the shop's own accounts it was paid into.
        if "transfer_account" not in receipt_cols:
            con.execute("ALTER TABLE receipts ADD COLUMN transfer_account TEXT")
        # Sender names the cashier can pick from. Filled as transfers are saved; the cashier
        # removes stale ones from the payment pop-up.
        for pick_list in _TRANSFER_LISTS.values():
            con.execute(
                f"""CREATE TABLE IF NOT EXISTS {pick_list} (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       name TEXT NOT NULL,
                       name_key TEXT NOT NULL UNIQUE,
                       created_at TEXT NOT NULL
                   )"""
            )

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

        # --- Additive migration: PO number, PO date, car plate, payments, old status ---
        # Live preorders predate all of these. Each is backfilled from what the
        # row already knows: the PO date is the day it was created, the number
        # follows creation order, the plate stays empty (it was never captured),
        # and a legacy single deposit becomes one "DP awal" payment.
        if "po_number" not in preorder_cols:
            con.execute("ALTER TABLE preorders ADD COLUMN po_number TEXT")
        if "po_date" not in preorder_cols:
            con.execute("ALTER TABLE preorders ADD COLUMN po_date TEXT")
        if "plate_full" not in preorder_cols:
            con.execute("ALTER TABLE preorders ADD COLUMN plate_full TEXT NOT NULL DEFAULT ''")
        for legacy in con.execute(
            "SELECT id, created_at FROM preorders WHERE po_date IS NULL OR po_number IS NULL "
            "ORDER BY created_at ASC, rowid ASC"
        ).fetchall():
            local_date = _shop_local_date(legacy["created_at"])
            con.execute(
                "UPDATE preorders SET po_date=COALESCE(po_date, ?), po_number=COALESCE(po_number, ?) WHERE id=?",
                (local_date, _next_po_no(con, local_date), legacy["id"]),
            )
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_preorders_po_number ON preorders(po_number)")
        con.execute(
            """INSERT INTO preorder_payments (preorder_id, paid_at, amount, note, created_by)
               SELECT id, created_at, deposit, 'DP awal', NULL FROM preorders
               WHERE deposit > 0 AND id NOT IN (SELECT preorder_id FROM preorder_payments)"""
        )
        poi_cols = {row["name"] for row in con.execute("PRAGMA table_info(preorder_items)").fetchall()}
        if "receipt_id" not in poi_cols:
            con.execute("ALTER TABLE preorder_items ADD COLUMN receipt_id TEXT")
        for col, ddl in (("ship_route", "TEXT"), ("priority", "TEXT NOT NULL DEFAULT 'normal'"), ("eta_date", "TEXT"),
                         ("cost_hint", "INTEGER NOT NULL DEFAULT 0")):
            if col not in poi_cols:
                con.execute(f"ALTER TABLE preorder_items ADD COLUMN {col} {ddl}")
        history_cols = {row["name"] for row in con.execute("PRAGMA table_info(preorder_status_history)").fetchall()}
        if "old_status" not in history_cols:
            con.execute("ALTER TABLE preorder_status_history ADD COLUMN old_status TEXT")

        # --- Additive migration: receipts' paid amount becomes a payment row ---
        # Every receipt with money in so far gets one "Pembayaran awal" row, dated when the
        # receipt was saved, so the history starts out agreeing with amount_paid. Only
        # receipts with no payment rows yet are touched, which makes this safe to rerun.
        con.execute(
            """INSERT INTO receipt_payments
                   (receipt_id, paid_at, amount, method, transfer_sender, transfer_account, note, created_by)
               SELECT id, created_at, amount_paid, payment_method, transfer_sender, transfer_account,
                      'Pembayaran awal', NULL
               FROM receipts
               WHERE amount_paid > 0 AND id NOT IN (SELECT receipt_id FROM receipt_payments)"""
        )

        # --- Additive migration: nota workflow (Kasir) ---
        # receipts.status stays the payment/void state (pending/done/void) that every list
        # and total reads; work_status is the shop-floor stage on top of it:
        # draft -> belum -> pengerjaan -> selesai (independent of payment). A draft holds no stock;
        # stock is taken when the nota leaves draft. Legacy rows: paid -> selesai, the rest selesai.
        receipt_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        for col, ddl in (
            ("work_status", "TEXT NOT NULL DEFAULT 'belum'"),
            ("work_notes", "TEXT"),
            ("cashier_name", "TEXT"),
            ("estimated_finish_at", "TEXT"),
            ("vehicle_make", "TEXT"),
            ("vehicle_model", "TEXT"),
            ("vehicle_color", "TEXT"),
            ("vehicle_year", "TEXT"),
            ("draft_saved_at", "TEXT"),
        ):
            if col not in receipt_cols:
                con.execute(f"ALTER TABLE receipts ADD COLUMN {col} {ddl}")
                if col == "work_status":
                    con.execute("UPDATE receipts SET work_status='selesai' WHERE status='done'")
        # Payment and work are separate statuses now (Belum Bayar / Sebagian / Lunas, and
        # Belum Dikerjakan / Dalam Pengerjaan / Pengerjaan Selesai). The old "siap_bayar" stage meant
        # "work done, waiting to be paid", which is exactly Pengerjaan Selesai.
        con.execute("UPDATE receipts SET work_status='selesai' WHERE work_status='siap_bayar'")
        item_cols = {row["name"] for row in con.execute("PRAGMA table_info(receipt_items)").fetchall()}
        for col, ddl in (
            ("item_type", "TEXT NOT NULL DEFAULT 'barang'"),
            ("line_discount", "INTEGER NOT NULL DEFAULT 0"),
            ("added_at", "TEXT"),
            ("added_by", "TEXT"),
            ("added_during_work", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in item_cols:
                con.execute(f"ALTER TABLE receipt_items ADD COLUMN {col} {ddl}")
        # K-22 Harga Khusus: a line sold below the Master Barang price carries who asked, why and who approved it.
        # list_price is the price at that moment (harga awal); the difference to what the customer pays is the discount.
        for table in ("receipt_items", "preorder_items"):
            have = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, ddl in (("list_price", "INTEGER"), ("price_state", "TEXT"), ("price_reason", "TEXT"),
                             ("price_requested_by", "TEXT"), ("price_requested_at", "TEXT"), ("price_approved_by", "TEXT"),
                             ("price_approved_at", "TEXT"), ("price_below_cost", "INTEGER NOT NULL DEFAULT 0")):
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        con.execute("""CREATE TABLE IF NOT EXISTS price_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            qty INTEGER NOT NULL,
            list_price INTEGER,
            from_price INTEGER,
            to_price INTEGER,
            discount_total INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            below_cost INTEGER NOT NULL DEFAULT 0,
            by_user TEXT,
            at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_price_events_doc ON price_events(scope, doc_id)")
        con.execute("""CREATE TABLE IF NOT EXISTS receipt_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT NOT NULL REFERENCES receipts(id),
            at TEXT NOT NULL,
            by_user TEXT,
            kind TEXT NOT NULL,
            note TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_receipt_events_receipt_id ON receipt_events(receipt_id)")

        # --- Additive migration: Purchase Order as a purchase invoice ---
        # supplier_pos.status keeps its coarse CHECKed set (draft/dipesan/.../batal); `stage` is the finer
        # step while a PO is out with the supplier (menunggu_konfirmasi -> dalam_pengiriman). Money fields
        # make the PO a purchase invoice: discount, PPN, shipping, and payments tracked in spo_payments.
        spo_cols = {row["name"] for row in con.execute("PRAGMA table_info(supplier_pos)").fetchall()}
        for col, ddl in (
            ("stage", "TEXT NOT NULL DEFAULT 'menunggu_konfirmasi'"),
            ("supplier_invoice_no", "TEXT"),
            ("internal_invoice_no", "TEXT"),
            ("payment_terms_days", "INTEGER"),
            ("due_date", "TEXT"),
            ("est_arrival_date", "TEXT"),
            ("discount_pct", "REAL NOT NULL DEFAULT 0"),
            ("shipping_cost", "INTEGER NOT NULL DEFAULT 0"),
            ("tax_rate", "REAL NOT NULL DEFAULT 0"),
            ("paid_amount", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in spo_cols:
                con.execute(f"ALTER TABLE supplier_pos ADD COLUMN {col} {ddl}")
        for old in con.execute(
            "SELECT id, po_date FROM supplier_pos WHERE internal_invoice_no IS NULL AND status NOT IN ('draft','batal') "
            "ORDER BY created_at, id"
        ).fetchall():
            con.execute("UPDATE supplier_pos SET internal_invoice_no=? WHERE id=?",
                        (_next_internal_invoice_no(con, old["po_date"] or _now()[:10]), old["id"]))
        # --- Additive migration: Master Satuan (keterangan, alasan nonaktif) ---
        unit_cols = {row["name"] for row in con.execute("PRAGMA table_info(units)").fetchall()}
        for col, ddl in (("description", "TEXT"), ("deactivated_reason", "TEXT"), ("updated_at", "TEXT")):
            if col not in unit_cols:
                con.execute(f"ALTER TABLE units ADD COLUMN {col} {ddl}")
        # --- Additive migration: Barang Masuk revision V2 (source PO/manual, surat jalan, officer, audit) ---
        ib_cols = {row["name"] for row in con.execute("PRAGMA table_info(inbound_documents)").fetchall()}
        for col, ddl in (
            ("supplier_po_id", "TEXT"), ("surat_jalan_no", "TEXT"), ("officer", "TEXT"), ("source", "TEXT"),
            ("manual_reason", "TEXT"), ("created_by", "TEXT"), ("reviewed_by", "TEXT"), ("reviewed_at", "TEXT"),
            ("posted_by", "TEXT"), ("posted_at", "TEXT"),
        ):
            if col not in ib_cols:
                con.execute(f"ALTER TABLE inbound_documents ADD COLUMN {col} {ddl}")
        ibi_cols = {row["name"] for row in con.execute("PRAGMA table_info(inbound_items)").fetchall()}
        for col, ddl in (("spo_item_id", "INTEGER"), ("condition", "TEXT"), ("location", "TEXT"), ("spo_package_id", "INTEGER")):
            if col not in ibi_cols:
                con.execute(f"ALTER TABLE inbound_items ADD COLUMN {col} {ddl}")
        _migrate_goods_receipt(con)
        con.execute("""CREATE TABLE IF NOT EXISTS inbound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES inbound_documents(id),
            at TEXT NOT NULL, by_user TEXT, kind TEXT NOT NULL, note TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_inbound_events_doc ON inbound_events(document_id)")
        spoi_cols = {row["name"] for row in con.execute("PRAGMA table_info(supplier_po_items)").fetchall()}
        for col, ddl in (
            ("sku", "TEXT"),
            ("discount_pct", "REAL NOT NULL DEFAULT 0"),
            ("tracking_no", "TEXT"),
            ("courier", "TEXT"),
            ("eta_date", "TEXT"),
            ("qty_cancelled", "INTEGER NOT NULL DEFAULT 0"),
            ("ship_method", "TEXT"),
            ("item_status", "TEXT"),
            ("shipped_date", "TEXT"),
            ("transit_location", "TEXT"),
        ):
            if col not in spoi_cols:
                con.execute(f"ALTER TABLE supplier_po_items ADD COLUMN {col} {ddl}")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS spo_receive_drafts (
                supplier_po_id TEXT PRIMARY KEY REFERENCES supplier_pos(id),
                payload TEXT NOT NULL, saved_by TEXT, saved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                at TEXT NOT NULL, by_user TEXT, kind TEXT NOT NULL, note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_spo_events_po ON spo_events(supplier_po_id);
            CREATE TABLE IF NOT EXISTS spo_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                paid_at TEXT NOT NULL, amount INTEGER NOT NULL, method TEXT NOT NULL DEFAULT 'transfer',
                note TEXT, created_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_spo_payments_po ON spo_payments(supplier_po_id);
            -- One row per Penerimaan Barang; lines hold what arrived per PO line.
            CREATE TABLE IF NOT EXISTS spo_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                received_date TEXT NOT NULL, surat_jalan_no TEXT, warehouse_location TEXT, note TEXT,
                inbound_document_id INTEGER, created_by TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spo_receipt_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL REFERENCES spo_receipts(id),
                item_id INTEGER NOT NULL REFERENCES supplier_po_items(id),
                quantity INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spo_receipts_po ON spo_receipts(supplier_po_id);
            CREATE TABLE IF NOT EXISTS spo_cancellations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                item_id INTEGER NOT NULL REFERENCES supplier_po_items(id),
                product_name TEXT NOT NULL, quantity INTEGER NOT NULL,
                reason TEXT NOT NULL, action TEXT NOT NULL, note TEXT,
                cancelled_by TEXT, cancelled_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spo_cancellations_po ON spo_cancellations(supplier_po_id);
            -- One row per change of an item's shipping status (Update Status Item Pengiriman).
            CREATE TABLE IF NOT EXISTS spo_item_shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                item_id INTEGER NOT NULL REFERENCES supplier_po_items(id),
                at TEXT NOT NULL, by_user TEXT,
                status TEXT NOT NULL, ship_method TEXT, forwarder TEXT, tracking_no TEXT,
                shipped_date TEXT, eta_date TEXT, transit_location TEXT, note TEXT, notify_customer INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_spo_item_shipments_po ON spo_item_shipments(supplier_po_id);
            -- Master Ekspedisi: the carriers / forwarders a shipment can travel with (separate from suppliers).
            CREATE TABLE IF NOT EXISTS forwarders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE, phone TEXT, contact TEXT, note TEXT,
                is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT
            );
            -- Paket Pengiriman: one PO can travel in several packages; a PO line can be split over packages.
            CREATE TABLE IF NOT EXISTS spo_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                package_no TEXT NOT NULL,
                forwarder_id INTEGER REFERENCES forwarders(id), forwarder_name TEXT,
                ship_method TEXT, tracking_no TEXT, shipped_date TEXT, eta_date TEXT, transit_location TEXT,
                status TEXT NOT NULL DEFAULT 'menunggu_konfirmasi', note TEXT,
                cancelled INTEGER NOT NULL DEFAULT 0, cancel_reason TEXT,
                created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_spo_packages_po ON spo_packages(supplier_po_id);
            CREATE TABLE IF NOT EXISTS spo_package_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL REFERENCES spo_packages(id),
                item_id INTEGER NOT NULL REFERENCES supplier_po_items(id),
                quantity INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spo_package_lines_pkg ON spo_package_lines(package_id);
            CREATE TABLE IF NOT EXISTS spo_package_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL REFERENCES spo_packages(id),
                supplier_po_id TEXT NOT NULL,
                at TEXT NOT NULL, by_user TEXT, status TEXT NOT NULL, ship_method TEXT, forwarder_name TEXT,
                tracking_no TEXT, eta_date TEXT, transit_location TEXT, note TEXT, notify_customer INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_spo_package_history_pkg ON spo_package_history(package_id);
            -- Photos of the vehicle and the customer's signature taken at Serah Terima Kendaraan. A handover that is
            -- cancelled voids its files (kept on disk and in this table for the audit trail, hidden from the nota).
            CREATE TABLE IF NOT EXISTS receipt_handover_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL REFERENCES receipts(id),
                kind TEXT NOT NULL, mime TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
                created_by TEXT, created_at TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0, void_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_handover_files_receipt ON receipt_handover_files(receipt_id);
        """)
        # Stok Karantina: goods that are in the shop but may not be sold (damaged / wrong at Terima Barang,
        # customer returns meant for the supplier or broken). master_items.stock_qty stays the sellable
        # stock every check already uses; quarantine_qty sits beside it (physical = both). Each batch is a
        # lot that remembers where it came from, so it can be released, written off or returned later.
        con.executescript("""
            CREATE TABLE IF NOT EXISTS quarantine_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_item_id INTEGER REFERENCES master_items(id),
                product_name TEXT NOT NULL,
                supplier_id INTEGER REFERENCES suppliers(id),
                quantity INTEGER NOT NULL,
                qty_left INTEGER NOT NULL,
                unit_cost INTEGER NOT NULL DEFAULT 0,
                condition TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                source_id TEXT,
                retur_candidate INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quarantine_lots_item ON quarantine_lots(master_item_id);
            CREATE INDEX IF NOT EXISTS idx_quarantine_lots_source ON quarantine_lots(source_type, source_id);
            CREATE TABLE IF NOT EXISTS quarantine_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id INTEGER NOT NULL REFERENCES quarantine_lots(id),
                action TEXT NOT NULL,
                qty INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                by_user TEXT,
                at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quarantine_events_lot ON quarantine_events(lot_id);
            CREATE TABLE IF NOT EXISTS quarantine_writeoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id INTEGER NOT NULL REFERENCES quarantine_lots(id),
                qty INTEGER NOT NULL,
                value INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('diajukan','direkomendasikan','disetujui','ditolak')),
                requested_by TEXT, requested_at TEXT NOT NULL,
                recommended_by TEXT, recommended_at TEXT, recommend_note TEXT,
                decided_by TEXT, decided_at TEXT, decision_note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_quarantine_writeoffs_lot ON quarantine_writeoffs(lot_id);
        """)
        # Retur Supplier: one document per return (RTS-YYYYMMDD-NNN). Goods wait in quarantine, leave stock
        # once at handover, and the supplier's answer is settled by replacement goods, debit notes
        # (allocated to Faktur Pembelian) or refunds.
        con.executescript("""
            CREATE TABLE IF NOT EXISTS supplier_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_number TEXT NOT NULL UNIQUE,
                return_date TEXT NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                return_type TEXT NOT NULL DEFAULT 'bermasalah',
                requested_settlement TEXT NOT NULL DEFAULT 'ganti',
                rma_number TEXT,
                supplier_po_id TEXT,
                inbound_document_id INTEGER,
                purchase_invoice_id INTEGER,
                notes TEXT,
                method TEXT,
                doc_status TEXT NOT NULL DEFAULT 'draft',
                handover_status TEXT NOT NULL DEFAULT 'belum',
                problem_note TEXT,
                courier TEXT, courier_service TEXT, plan_date TEXT,
                ship_address TEXT, ship_recipient TEXT, ship_phone TEXT,
                sales_name TEXT, sales_phone TEXT, sales_company TEXT, pickup_at TEXT,
                cost_bearer TEXT, shipping_estimate INTEGER NOT NULL DEFAULT 0,
                packages TEXT, packing_text TEXT,
                awb TEXT, handed_at TEXT, shipping_cost INTEGER NOT NULL DEFAULT 0, officer TEXT, koli_handed INTEGER,
                taker_name TEXT, taker_id_no TEXT,
                received_by_supplier_at TEXT,
                decided_at TEXT, decision_channel TEXT, shipping_claim_approved INTEGER NOT NULL DEFAULT 0,
                submitted_by TEXT, submitted_at TEXT, approver_note TEXT,
                approved_by TEXT, approved_at TEXT,
                cancelled_by TEXT, cancelled_at TEXT, cancel_reason TEXT,
                completed_at TEXT,
                created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_supplier_returns_supplier ON supplier_returns(supplier_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_returns_awb ON supplier_returns(awb) WHERE awb IS NOT NULL;
            CREATE TABLE IF NOT EXISTS supplier_return_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL REFERENCES supplier_returns(id),
                source TEXT NOT NULL,
                source_lot_id INTEGER,
                quarantine_lot_id INTEGER,
                inbound_item_id INTEGER,
                master_item_id INTEGER,
                product_name TEXT NOT NULL,
                qty INTEGER NOT NULL,
                unit_cost INTEGER NOT NULL DEFAULT 0,
                condition TEXT NOT NULL DEFAULT 'rusak',
                reason_code TEXT,
                reason_detail TEXT,
                test_result TEXT,
                serial_no TEXT,
                qty_approved INTEGER NOT NULL DEFAULT 0,
                qty_rejected INTEGER NOT NULL DEFAULT 0,
                settlement TEXT,
                reject_reason TEXT,
                reject_fate TEXT,
                qty_replaced INTEGER NOT NULL DEFAULT 0,
                qty_rejected_back INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sr_items_return ON supplier_return_items(return_id);
            CREATE INDEX IF NOT EXISTS idx_sr_items_lot ON supplier_return_items(source_lot_id);
            CREATE TABLE IF NOT EXISTS supplier_return_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL REFERENCES supplier_returns(id),
                item_id INTEGER,
                stage TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                created_by TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sr_photos_return ON supplier_return_photos(return_id);
            CREATE TABLE IF NOT EXISTS supplier_return_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL REFERENCES supplier_returns(id),
                kind TEXT NOT NULL,
                note TEXT,
                by_user TEXT,
                at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sr_events_return ON supplier_return_events(return_id);
            CREATE TABLE IF NOT EXISTS supplier_return_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL REFERENCES supplier_returns(id),
                item_id INTEGER NOT NULL REFERENCES supplier_return_items(id),
                qty INTEGER NOT NULL,
                condition TEXT NOT NULL DEFAULT 'baik',
                inbound_document_id INTEGER,
                created_by TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supplier_credit_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cn_number TEXT UNIQUE,
                kind TEXT NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                return_id INTEGER REFERENCES supplier_returns(id),
                amount INTEGER NOT NULL,
                supplier_cn_no TEXT,
                issued_at TEXT NOT NULL,
                method TEXT,
                reference_no TEXT,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'aktif',
                created_by TEXT, created_at TEXT NOT NULL,
                cancelled_by TEXT, cancelled_at TEXT, cancel_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scn_return ON supplier_credit_notes(return_id);
            CREATE TABLE IF NOT EXISTS supplier_credit_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                credit_note_id INTEGER NOT NULL REFERENCES supplier_credit_notes(id),
                invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id),
                amount INTEGER NOT NULL,
                created_by TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sca_note ON supplier_credit_allocations(credit_note_id);
            -- Piutang Refund / Kredit Supplier: what a supplier owes back after agreeing to cancel a Purchase Order that
            -- was already paid. A claim is only a promise (counts for nothing) until the money arrives (refund) or the
            -- supplier's debit note is confirmed (kredit); each confirmation writes a supplier_credit_notes row.
            CREATE TABLE IF NOT EXISTS supplier_refund_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_no TEXT UNIQUE NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
                kind TEXT NOT NULL, amount INTEGER NOT NULL, settled_amount INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'menunggu', note TEXT,
                created_by TEXT, created_at TEXT NOT NULL, closed_by TEXT, closed_at TEXT, close_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_refund_claims_po ON supplier_refund_claims(supplier_po_id);
        """)
        # K-12: supplier debt lives on Faktur Pembelian. Each settlement is a row (cash payment, supplier DP
        # carried over from the PO, or a return credit), so the paid total is always explained.
        con.executescript("""
            CREATE TABLE IF NOT EXISTS purchase_invoice_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id),
                paid_at TEXT NOT NULL,
                amount INTEGER NOT NULL,
                kind TEXT NOT NULL,
                method TEXT,
                reference_no TEXT,
                note TEXT,
                spo_payment_id INTEGER,
                created_by TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pi_payments_invoice ON purchase_invoice_payments(invoice_id);
            -- Who verified, held or approved a faktur, and why (Three-Way Matching trail).
            CREATE TABLE IF NOT EXISTS purchase_invoice_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id),
                at TEXT NOT NULL, by_user TEXT, kind TEXT NOT NULL, note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pi_events_invoice ON purchase_invoice_events(invoice_id);
        """)
        for table, cols in (
            # Shipping on the supplier's faktur, kept apart from the goods: the cost, the DPP it is taxed on
            # (0 = no PPN) and the PPN amount as printed on the faktur.
            ("purchase_invoices", (("supplier_po_id", "TEXT"), ("shipping_cost", "INTEGER NOT NULL DEFAULT 0"),
                                   ("shipping_dpp", "INTEGER NOT NULL DEFAULT 0"), ("shipping_tax", "INTEGER NOT NULL DEFAULT 0"),
                                   # Three-Way Matching: belum | terverifikasi | ditahan | diajukan | pengecualian (Admin approved a mismatch).
                                   ("verify_status", "TEXT NOT NULL DEFAULT 'belum'"), ("verify_note", "TEXT"), ("verified_by", "TEXT"),
                                   ("verified_at", "TEXT"), ("exception_reason", "TEXT"), ("hold_note", "TEXT"))),
            # Supplier DP paid on a PO before its faktur: how much of it has been carried onto the faktur.
            ("spo_payments", (("applied_amount", "INTEGER NOT NULL DEFAULT 0"), ("applied_invoice_id", "INTEGER"))),
            # Alokasi DP manual: a reversal row points at the allocation row it undoes.
            ("purchase_invoice_payments", (("reverses_id", "INTEGER"),)),
            # Pembayaran Supplier: which shop account paid, which supplier account received, the proof file and its check,
            # and the Kas Keluar number (KK-YYYYMMDD-NNN, assigned when the payment is recorded or lazily for older rows).
            ("purchase_invoice_payments", (("source_account", "TEXT"), ("dest_account", "TEXT"), ("proof_file", "TEXT"), ("proof_name", "TEXT"),
                                           ("proof_status", "TEXT"), ("proof_by", "TEXT"), ("proof_at", "TEXT"),
                                           ("verified_by", "TEXT"), ("verified_at", "TEXT"), ("kk_number", "TEXT"))),
            ("spo_payments", (("kk_number", "TEXT"),)),
            # BEKAS (Retur Supplier #9): a second-hand variant is its own master item pointing at the new one.
            ("master_items", (("quarantine_qty", "INTEGER NOT NULL DEFAULT 0"), ("item_condition", "TEXT NOT NULL DEFAULT 'baru'"),
                              ("origin_item_id", "INTEGER"))),
            # Owner: an admin who gives the final approval for losses (quarantine write-off).
            # K-22: posting Barang Masuk is a permission given to specific users (admin or staff).
            ("users", (("is_owner", "INTEGER NOT NULL DEFAULT 0"), ("can_post_inbound", "INTEGER NOT NULL DEFAULT 0"))),
            # Received in bad condition (went to quarantine): not usable for the Sales Order line.
            ("supplier_po_items", (("qty_bad", "INTEGER NOT NULL DEFAULT 0"),)),
            ("spo_receipts", (("gr_number", "TEXT"), ("officer", "TEXT"))),
            ("spo_receipt_lines", (("condition", "TEXT NOT NULL DEFAULT 'baik'"), ("location", "TEXT"), ("package_id", "INTEGER"))),
            ("spo_payments", (("reference_no", "TEXT"), ("bank_account", "TEXT"))),
            # A debit note / refund that came from a cancelled Purchase Order (not a Retur Supplier) names the PO and the claim.
            ("supplier_credit_notes", (("supplier_po_id", "TEXT"), ("claim_id", "INTEGER"))),
            # K-19: kind='pindah' marks the paired rows of a DP moved between two invoices of one Sales Order
            # (they net to zero and are left out of sales figures).
            ("receipt_payments", (("reversed_of", "INTEGER"), ("kind", "TEXT"),
                                  # K-18: sah (counts as money in) | pending (transfer/QRIS not yet in the account) | gagal.
                                  ("state", "TEXT NOT NULL DEFAULT 'sah'"), ("confirmed_by", "TEXT"), ("confirmed_at", "TEXT"),
                                  ("fail_reason", "TEXT"),
                                  # K-20: what proves a refund was handed over (transfer reference / name of the cash receiver).
                                  ("proof_ref", "TEXT"))),
            # K-19: every invoice of a Sales Order points back at it; invoice_seq 1 is the main invoice.
            ("receipts", (("preorder_id", "TEXT"), ("invoice_seq", "INTEGER NOT NULL DEFAULT 1"), ("split_reason", "TEXT"),
                          # K-21: when the nota closed (Lunas + Selesai); kept by the triggers below, whatever path closed it.
                          ("closed_at", "TEXT"),
                          # K-06: what stage of the job a "Dalam Pengerjaan" nota is at (info only; never touches payment).
                          ("work_stage", "TEXT"),
                          # K-08/K-09: the vehicle handed back to the customer, who did it, and any balance it left with.
                          ("handed_over_at", "TEXT"), ("handed_over_by", "TEXT"), ("handover_note", "TEXT"),
                          ("handover_debt", "INTEGER NOT NULL DEFAULT 0"), ("handover_receiver", "TEXT"),
                          # Diskon nota (like Harga Khusus, K-22): staff ask, only an admin approves; kept with the amount it
                          # replaced so a rejection puts the nota back where it was.
                          ("discount_state", "TEXT"), ("discount_reason", "TEXT"), ("discount_requested_by", "TEXT"),
                          ("discount_requested_at", "TEXT"), ("discount_approved_by", "TEXT"), ("discount_approved_at", "TEXT"),
                          ("discount_prev", "INTEGER NOT NULL DEFAULT 0"), ("discount_prev_reason", "TEXT"),
                          ("discount_below_cost", "INTEGER NOT NULL DEFAULT 0"), ("discount_stacked", "INTEGER NOT NULL DEFAULT 0"))),
            ("preorder_payments", (("method", "TEXT NOT NULL DEFAULT 'cash'"), ("state", "TEXT NOT NULL DEFAULT 'sah'"),
                                   ("confirmed_by", "TEXT"), ("confirmed_at", "TEXT"), ("fail_reason", "TEXT"))),
            # Owner 25-10-2026: the transfer screenshot a WhatsApp customer sends (payment_proofs storage), kept on the
            # Sales Order's DP and carried onto the invoice payment it becomes.
            ("preorder_payments", (("proof_file", "TEXT"), ("proof_name", "TEXT"), ("proof_by", "TEXT"), ("proof_at", "TEXT"))),
            ("receipt_payments", (("proof_file", "TEXT"), ("proof_name", "TEXT"), ("proof_by", "TEXT"), ("proof_at", "TEXT"))),
            # K-13: when the order was confirmed (the 24-hour cancellation clock) and how it was cancelled.
            ("preorders", (("published_at", "TEXT"), ("cancelled_at", "TEXT"), ("cancelled_by", "TEXT"),
                           ("cancel_kind", "TEXT"), ("cancel_reason", "TEXT"))),
            # Per line: where the goods come from (supplier | stok | jasa; NULL = decided automatically, the
            # rule before this column) and, for 'supplier', which supplier.
            ("preorder_items", (("source", "TEXT"), ("supplier_id", "INTEGER REFERENCES suppliers(id)"),
                                ("cancelled_at", "TEXT"), ("cancelled_by", "TEXT"),
                                ("cancel_kind", "TEXT"), ("cancel_reason", "TEXT"))),
            # Owner 25-10-2026: a note per line, e.g. the plate number to print on an ordered number plate.
            ("preorder_items", (("note", "TEXT"),)),
        ):
            have = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, ddl in cols:
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # Fakturs paid before payment rows existed get one opening row, so rows always add up to amount_paid.
        con.execute(
            """INSERT INTO purchase_invoice_payments (invoice_id, paid_at, amount, kind, method, note, created_by, created_at)
               SELECT id, updated_at, amount_paid, 'bayar', NULL, 'Saldo pembayaran sebelum riwayat dicatat', 'sistem', updated_at
               FROM purchase_invoices
               WHERE amount_paid <> 0 AND id NOT IN (SELECT DISTINCT invoice_id FROM purchase_invoice_payments)""")
        # Drafts that Penerimaan Barang (Purchase Order) opened already point at their PO.
        con.execute(
            """UPDATE inbound_documents SET
                 supplier_po_id = (SELECT r.supplier_po_id FROM spo_receipts r WHERE r.inbound_document_id = inbound_documents.id),
                 surat_jalan_no = (SELECT r.surat_jalan_no FROM spo_receipts r WHERE r.inbound_document_id = inbound_documents.id),
                 officer = (SELECT COALESCE(r.officer, r.created_by) FROM spo_receipts r WHERE r.inbound_document_id = inbound_documents.id),
                 source = 'po_supplier'
               WHERE supplier_po_id IS NULL AND id IN (SELECT inbound_document_id FROM spo_receipts WHERE inbound_document_id IS NOT NULL)""")

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

        # --- Additive migration: product details + archive info + price history ---
        for col, ddl in (
            ("description", "TEXT"),
            ("min_stock", "INTEGER NOT NULL DEFAULT 5"),
            ("location", "TEXT"),
            ("archived_reason", "TEXT"),
            ("archive_mode", "TEXT"),
            ("archived_note", "TEXT"),
            ("archived_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("category_id", "INTEGER REFERENCES categories(id)"),
            ("updated_by", "TEXT"),
        ):
            if col not in master_item_cols:
                con.execute(f"ALTER TABLE master_items ADD COLUMN {col} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_master_items_category_id ON master_items(category_id)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS idempotency_keys (
                   scope TEXT PRIMARY KEY,
                   status INTEGER,
                   content_type TEXT,
                   body BLOB,
                   created_at TEXT NOT NULL
               )"""
        )
        cancel_cols = {row["name"] for row in con.execute("PRAGMA table_info(spo_cancellations)").fetchall()}
        # A cancellation of an issued PO is a REQUEST until the supplier agrees. Rows from before this
        # existed were applied on the spot, so they default to 'disetujui'.
        for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'disetujui'"), ("resolved_by", "TEXT"),
                         ("resolved_at", "TEXT"), ("resolution_note", "TEXT")):
            if col not in cancel_cols:
                con.execute(f"ALTER TABLE spo_cancellations ADD COLUMN {col} {ddl}")
        receipt_cols_v = {row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()}
        for col in ("void_reason", "voided_by", "voided_at"):
            if col not in receipt_cols_v:
                con.execute(f"ALTER TABLE receipts ADD COLUMN {col} TEXT")
        ledger_cols = {row["name"] for row in con.execute("PRAGMA table_info(stock_ledger)").fetchall()}
        if "created_by" not in ledger_cols:
            con.execute("ALTER TABLE stock_ledger ADD COLUMN created_by TEXT")
        con.execute(
            """CREATE TABLE IF NOT EXISTS master_item_price_history (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   master_item_id INTEGER NOT NULL REFERENCES master_items(id),
                   cost_price INTEGER NOT NULL DEFAULT 0,
                   sell_price INTEGER NOT NULL DEFAULT 0,
                   changed_by TEXT,
                   note TEXT,
                   created_at TEXT NOT NULL
               )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_mi_price_history_item ON master_item_price_history(master_item_id)"
        )
        # One opening row per product that has none yet, so the history never starts empty.
        con.execute(
            """INSERT INTO master_item_price_history (master_item_id, cost_price, sell_price, changed_by, note, created_at)
               SELECT id, avg_cost_price, sell_price, NULL, 'Harga awal', created_at FROM master_items
               WHERE id NOT IN (SELECT master_item_id FROM master_item_price_history)"""
        )

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
        # Satuan names are UPPERCASE. Matching is case-insensitive everywhere, so rewriting the
        # spelling of existing rows is safe; transaction snapshots (receipts, POs) are left as they were.
        con.execute("UPDATE categories SET name=UPPER(name) WHERE name != UPPER(name)")
        con.execute("UPDATE units SET name=UPPER(name) WHERE name != UPPER(name)")
        con.execute("UPDATE master_items SET unit=UPPER(unit) WHERE unit != UPPER(unit)")
        con.execute("UPDATE inbound_items SET pending_unit=UPPER(pending_unit) WHERE pending_unit IS NOT NULL AND pending_unit != UPPER(pending_unit)")

        # --- Additive migration: Sales Order -> Invoice ---
        # Every published Sales Order owns exactly one invoice (receipts row) from the moment it is
        # created; its lines carry preorder_item_id so the invoice follows the PO, and payments are
        # taken on the invoice only.
        if "receipt_id" not in {r["name"] for r in con.execute("PRAGMA table_info(preorders)").fetchall()}:
            con.execute("ALTER TABLE preorders ADD COLUMN receipt_id TEXT")
        # The supplier a Sales Order is bought from. Null = each barang follows its Master Barang supplier.
        if "supplier_id" not in {r["name"] for r in con.execute("PRAGMA table_info(preorders)").fetchall()}:
            con.execute("ALTER TABLE preorders ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id)")
        # Owner, 24-10-2026: where the order came in (walk-in at the shop or by WhatsApp).
        if "order_source" not in {r["name"] for r in con.execute("PRAGMA table_info(preorders)").fetchall()}:
            con.execute("ALTER TABLE preorders ADD COLUMN order_source TEXT NOT NULL DEFAULT 'walk_in'")
        if "preorder_item_id" not in {r["name"] for r in con.execute("PRAGMA table_info(receipt_items)").fetchall()}:
            con.execute("ALTER TABLE receipt_items ADD COLUMN preorder_item_id INTEGER")
        # --- Additive migration: Retur Barang ---
        # returned_qty: how many of a line the customer brought back; returned_stock_qty: how many of
        # those went back on the shelf (the rest went to the supplier or were written off).
        # receipts.return_total: what the returns took off the nota's total (the refunds).
        ri_cols = {r["name"] for r in con.execute("PRAGMA table_info(receipt_items)").fetchall()}
        if "returned_qty" not in ri_cols:
            con.execute("ALTER TABLE receipt_items ADD COLUMN returned_qty INTEGER NOT NULL DEFAULT 0")
        if "returned_stock_qty" not in ri_cols:
            con.execute("ALTER TABLE receipt_items ADD COLUMN returned_stock_qty INTEGER NOT NULL DEFAULT 0")
        if "return_total" not in {r["name"] for r in con.execute("PRAGMA table_info(receipts)").fetchall()}:
            con.execute("ALTER TABLE receipts ADD COLUMN return_total INTEGER NOT NULL DEFAULT 0")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS receipt_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL,
                receipt_item_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                master_item_id INTEGER,
                quantity INTEGER NOT NULL,
                item_value INTEGER NOT NULL,
                refund_amount INTEGER NOT NULL,
                cash_out INTEGER NOT NULL DEFAULT 0,
                method TEXT,
                destination TEXT NOT NULL CHECK (destination IN ('stok','supplier','rusak')),
                reason TEXT NOT NULL,
                returned_by TEXT,
                returned_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_returns_receipt ON receipt_returns(receipt_id);
        """)
        rr_cols = {r["name"] for r in con.execute("PRAGMA table_info(receipt_returns)").fetchall()}
        for col, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'retur'"), ("reason_code", "TEXT"), ("restock_qty", "INTEGER"),
            ("supplier_qty", "INTEGER"), ("damaged_qty", "INTEGER"),
        ):
            if col not in rr_cols:
                con.execute(f"ALTER TABLE receipt_returns ADD COLUMN {col} {ddl}")
        _backfill_po_invoices(con)
        # A published Sales Order got its invoice at the moment it was published.
        con.execute(
            """UPDATE preorders SET published_at=(SELECT r.created_at FROM receipts r WHERE r.id=preorders.receipt_id)
               WHERE published_at IS NULL AND receipt_id IS NOT NULL""")
        con.execute("CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='k01_install_point'").fetchone():
            _migrate_k01_install_point(con)
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k01_install_point', ?)", (_now(),))
        _svc_init(con)
        _veh_init(con)
        con.execute("CREATE INDEX IF NOT EXISTS idx_receipts_preorder_id ON receipts(preorder_id)")
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='k19_receipt_preorder_link'").fetchone():
            con.execute("""UPDATE receipts SET preorder_id=(SELECT p.id FROM preorders p WHERE p.receipt_id=receipts.id)
                           WHERE preorder_id IS NULL""")
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k19_receipt_preorder_link', ?)", (_now(),))
        # K-22 (19-10-2026): admins could post Barang Masuk before the permission existed; keep that for them
        # once, then it is granted per user on the Pengguna page.
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='k22_post_inbound_grant'").fetchone():
            con.execute("UPDATE users SET can_post_inbound=1 WHERE role='admin'")
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k22_post_inbound_grant', ?)", (_now(),))
        # K-20: money handed back is its own kind of row so Kas Keluar can list it. Rows from before the
        # kind existed are recognised by their note; they have no proof and stay that way.
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='k20_refund_kind'").fetchone():
            con.execute("UPDATE receipt_payments SET kind='refund' WHERE kind IS NULL AND amount < 0 AND reversed_of IS NULL AND note LIKE 'Refund%'")
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k20_refund_kind', ?)", (_now(),))
        # K-21: "Penjualan" on the Dashboard counts notas by the month they closed. A trigger stamps/clears
        # closed_at so every path that pays or finishes a nota (and reopen / reversal / void) stays covered.
        _now_sql = "strftime('%Y-%m-%dT%H:%M:%f','now') || '+00:00'"
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_receipts_closed_ins AFTER INSERT ON receipts
            WHEN NEW.status='done' AND NEW.work_status='selesai' AND NEW.closed_at IS NULL
            BEGIN UPDATE receipts SET closed_at = {_now_sql} WHERE id = NEW.id; END""")
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_receipts_closed_set AFTER UPDATE OF status, work_status ON receipts
            WHEN NEW.status='done' AND NEW.work_status='selesai' AND NEW.closed_at IS NULL
            BEGIN UPDATE receipts SET closed_at = {_now_sql} WHERE id = NEW.id; END""")
        con.execute("""CREATE TRIGGER IF NOT EXISTS trg_receipts_closed_clear AFTER UPDATE OF status, work_status ON receipts
            WHEN NOT (NEW.status='done' AND NEW.work_status='selesai') AND NEW.closed_at IS NOT NULL
            BEGIN UPDATE receipts SET closed_at = NULL WHERE id = NEW.id; END""")
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='k21_closed_at'").fetchone():
            # Notas that closed before the column existed: the latest thing that happened to them.
            con.execute("""UPDATE receipts SET closed_at = MAX(
                    created_at,
                    COALESCE((SELECT MAX(p.paid_at) FROM receipt_payments p WHERE p.receipt_id = receipts.id), ''),
                    COALESCE((SELECT MAX(e.at) FROM receipt_events e WHERE e.receipt_id = receipts.id), ''))
                WHERE status='done' AND work_status='selesai' AND closed_at IS NULL""")
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k21_closed_at', ?)", (_now(),))
        # 'Stok Tersedia' lines used to stay at Pesanan dibuat even when the shelf held all of them (the publish
        # check only looked at items that also had a supplier line). Move the ones the shelf covers now, once.
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='stok_lines_ready'").fetchone():
            _promote_shelf_ready_lines(con, "sistem")
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('stok_lines_ready', ?)", (_now(),))
        from backend import incentives
        incentives.migrate(con)
        from backend import credit_notes
        credit_notes.migrate(con)
        from backend import company, quotations
        company.migrate(con)
        quotations.migrate(con)
        sr_cols = {r["name"] for r in con.execute("PRAGMA table_info(supplier_returns)").fetchall()}
        if "origin" not in sr_cols:  # 'barang_masuk' = made from a posted Goods Receipt (potong tagihan)
            con.execute("ALTER TABLE supplier_returns ADD COLUMN origin TEXT")
        # 21-10-2026: a faktur is numbered by the supplier's own invoice number; existing ones that have it take it.
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='faktur_no_from_supplier'").fetchone():
            for r in con.execute("SELECT id FROM purchase_invoices WHERE status<>'void' AND TRIM(COALESCE(supplier_invoice_no,''))<>''").fetchall():
                _sync_faktur_number(con, r["id"])
            con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('faktur_no_from_supplier', ?)", (_now(),))


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


# --- K-22 Harga Khusus: a price below Master Barang, per transaction line ------------------------------------------
# Staff may ask for it, only an admin approves; it never changes the Master Barang price. A line below the shop's
# cost is refused unless an admin explicitly allows it. While a request is Pending the nota / Sales Order cannot take
# payment or be published, so nothing is collected on a price nobody approved. Every step is kept in price_events.
_PRICE_COLS = ("list_price", "price_state", "price_reason", "price_requested_by", "price_requested_at",
               "price_approved_by", "price_approved_at", "price_below_cost")
_NO_PRICE = {**{c: None for c in _PRICE_COLS}, "price_below_cost": 0}


def price_actor(user: dict | None) -> dict | None:
    return {"username": user.get("username"), "is_admin": user.get("role") == "admin"} if user else None


def _price_event(con: sqlite3.Connection, scope: str, doc_id: str, product_name: str, kind: str, qty: int,
                 list_price: int | None, from_price: int | None, to_price: int | None, discount_total: int,
                 reason: str | None, below_cost: int, by: str | None) -> None:
    con.execute(
        """INSERT INTO price_events (scope, doc_id, product_name, kind, qty, list_price, from_price, to_price,
               discount_total, reason, below_cost, by_user, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (scope, doc_id, product_name, kind, qty, list_price, from_price, to_price, discount_total, reason, below_cost, by, _now()))


def _price_review(con: sqlite3.Connection, *, scope: str, doc_id: str, master_item_id: int | None, product_name: str,
                  qty: int, unit_price: int, line_discount: int, prev: dict | None, reason: str | None,
                  allow_below_cost: bool, actor: dict | None) -> dict:
    """The price columns to store for a line. `prev` is the stored line (None for a new one). A line is "special" when
    what the customer pays is under the Master Barang price; its list price is kept from the first request so a later
    change of the master price does not rewrite history. actor=None is an internal caller: nothing is reviewed."""
    was_special = bool(prev and prev.get("price_state"))
    if actor is None:
        return {c: (prev or {}).get(c, _NO_PRICE[c]) for c in _PRICE_COLS} if prev else dict(_NO_PRICE)
    mi = con.execute("SELECT sell_price, avg_cost_price, last_cost_price FROM master_items WHERE id=?", (master_item_id,)).fetchone() \
        if master_item_id else None
    ref = (prev["list_price"] if was_special else (mi["sell_price"] if mi else 0)) or 0
    net_total = qty * unit_price - (line_discount or 0)
    if mi is None or ref <= 0 or net_total >= qty * ref:
        if was_special:
            _price_event(con, scope, doc_id, product_name, "cleared", qty, prev["list_price"], None, unit_price, 0,
                         "Harga kembali ke harga Master Barang", 0, actor["username"])
        return dict(_NO_PRICE)
    cost = (mi["avg_cost_price"] or mi["last_cost_price"] or 0)
    below = 1 if cost > 0 and net_total < qty * cost else 0
    if below and not (actor["is_admin"] and allow_below_cost):
        # Deliberately no numbers here: staff never see the cost.
        raise ValueError(
            f"Harga “{product_name}” terlalu rendah dan diblokir. " + (
                "Harga di bawah modal hanya boleh dengan izin khusus: centang “Izinkan di bawah modal” dan isi alasannya."
                if actor["is_admin"] else "Hubungi Admin untuk harga ini."))
    if was_special and prev["quantity"] and Fraction(prev["quantity"] * prev["unit_price"] - (prev.get("line_discount") or 0), prev["quantity"]) \
            == Fraction(net_total, qty):
        return {c: prev.get(c) for c in _PRICE_COLS}  # same price per unit as before: the approval stands
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError(f"Alasan harga khusus “{product_name}” wajib diisi (misal: hasil negosiasi, pelanggan langganan).")
    now = _now()
    approved = actor["is_admin"]
    cols = {"list_price": ref, "price_state": "approved" if approved else "pending", "price_reason": reason[:300],
            "price_requested_by": actor["username"], "price_requested_at": now,
            "price_approved_by": actor["username"] if approved else None, "price_approved_at": now if approved else None,
            "price_below_cost": below}
    per_unit = round(net_total / qty)
    _price_event(con, scope, doc_id, product_name, "auto_approved" if approved else "request", qty, ref,
                 round((prev["quantity"] * prev["unit_price"] - (prev.get("line_discount") or 0)) / prev["quantity"]) if prev and prev.get("quantity") else ref,
                 per_unit, qty * ref - net_total, cols["price_reason"], below, actor["username"])
    return cols


def _receipt_price_pending(con: sqlite3.Connection, receipt_id: str) -> int:
    """Lines with a special price, plus the nota's own discount, still waiting for an admin."""
    return (con.execute("SELECT COUNT(*) AS n FROM receipt_items WHERE receipt_id=? AND price_state='pending'", (receipt_id,)).fetchone()["n"]
            + con.execute("SELECT COUNT(*) AS n FROM receipts WHERE id=? AND discount_state='pending'", (receipt_id,)).fetchone()["n"])


def _preorder_price_pending(con: sqlite3.Connection, preorder_id: str) -> int:
    return con.execute("SELECT COUNT(*) AS n FROM preorder_items WHERE preorder_id=? AND price_state='pending' AND status<>?",
                       (preorder_id, PO_LINE_CANCELLED)).fetchone()["n"]


_PRICE_PENDING_MSG = "Ada harga khusus atau diskon nota yang menunggu persetujuan Admin. Pembayaran dan penerbitan bisa dilanjutkan setelah disetujui."


_DISC_COLS = ("discount_state", "discount_reason", "discount_requested_by", "discount_requested_at", "discount_approved_by",
              "discount_approved_at", "discount_prev", "discount_prev_reason", "discount_below_cost", "discount_stacked")
_NO_DISC = {"discount_state": None, "discount_reason": None, "discount_requested_by": None, "discount_requested_at": None,
            "discount_approved_by": None, "discount_approved_at": None, "discount_prev": 0, "discount_prev_reason": None,
            "discount_below_cost": 0, "discount_stacked": 0}
DISCOUNT_STAFF_MAX_PCT = 50  # a nota discount above this share of the subtotal is only for an admin


def _discount_review(con: sqlite3.Connection, *, receipt_id: str, items: list[dict], subtotal: int, prev: dict | None,
                     new_discount: int, reason: str | None, allow_below_cost: bool, allow_stack: bool,
                     actor: dict | None) -> dict:
    """The discount columns to store for a nota whose discount is being set. `prev` is the stored receipt (None for a
    new one). Staff's request is Pending (applied, but the nota takes no payment until an admin approves); an admin's is
    approved at once. Never changes Master Barang. actor=None is an internal caller: nothing is reviewed."""
    old = (prev or {}).get("discount") or 0
    stored = {c: (prev or {}).get(c, _NO_DISC[c]) for c in _DISC_COLS}
    if actor is None or new_discount == old:
        return stored
    by = actor["username"]
    if new_discount <= 0:
        if old > 0:
            _price_event(con, "receipt", receipt_id, "Diskon nota", "cleared", 1, subtotal, old, 0, 0, "Diskon nota dihapus", 0, by)
        return dict(_NO_DISC)
    if new_discount > subtotal:
        raise ValueError("Diskon tidak boleh melebihi subtotal.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan diskon nota wajib diisi (misal: pelanggan langganan, paket pemasangan).")
    if new_discount * 100 > subtotal * DISCOUNT_STAFF_MAX_PCT and not actor["is_admin"]:
        raise ValueError(f"Diskon di atas {DISCOUNT_STAFF_MAX_PCT}% dari subtotal hanya bisa diatur Admin.")
    cost = 0
    for i in items:
        if i.get("master_item_id"):
            mi = con.execute("SELECT avg_cost_price, last_cost_price FROM master_items WHERE id=?", (i["master_item_id"],)).fetchone()
            if mi:
                cost += i["quantity"] * (mi["avg_cost_price"] or mi["last_cost_price"] or 0)
    below = 1 if cost > 0 and subtotal - new_discount < cost else 0
    if below and not (actor["is_admin"] and allow_below_cost):
        # No numbers here: staff never see the cost.
        raise ValueError("Diskon nota terlalu besar dan diblokir. " + (
            "Harga akhir di bawah modal hanya boleh dengan izin khusus: centang “Izinkan di bawah modal” dan isi alasannya."
            if actor["is_admin"] else "Hubungi Admin untuk diskon ini."))
    stacked = 1 if any(i.get("price_state") for i in items) else 0
    if stacked and not allow_stack:
        raise ValueError("Nota ini sudah memakai harga khusus. Diskon nota menambah potongan di atasnya; "
                         "centang “Gabungkan dengan harga khusus” bila memang sengaja.")
    now = _now()
    approved = actor["is_admin"]
    was_pending = (prev or {}).get("discount_state") == "pending"
    cols = {"discount_state": "approved" if approved else "pending", "discount_reason": reason[:300],
            "discount_requested_by": by, "discount_requested_at": now,
            "discount_approved_by": by if approved else None, "discount_approved_at": now if approved else None,
            # the amount (and its reason) a rejection goes back to: the last approved one
            "discount_prev": stored["discount_prev"] if was_pending else old,
            "discount_prev_reason": stored["discount_prev_reason"] if was_pending else (stored["discount_reason"] if old > 0 else None),
            "discount_below_cost": below, "discount_stacked": stacked}
    _price_event(con, "receipt", receipt_id, "Diskon nota", "auto_approved" if approved else "request", 1, subtotal, old, new_discount,
                 new_discount, cols["discount_reason"], below, by)
    return cols


def decide_discount_request(receipt_id: str, approve: bool, note: str | None, user: dict) -> dict:
    """Admin only. Approve keeps the discount. Reject puts the nota back at the last approved discount (none if there
    was none) and recomputes the bill; money already paid stays on the record."""
    if user.get("role") != "admin":
        raise PermissionError("Hanya Admin yang dapat menyetujui atau menolak diskon nota.")
    by = user.get("username")
    note = (note or "").strip()[:300] or None
    if not approve and not note:
        raise ValueError("Tulis alasan penolakan diskon nota.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if rec is None:
            raise LookupError("Nota tidak ditemukan.")
        if rec["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        if rec["discount_state"] != "pending":
            raise ValueError("Diskon nota ini tidak sedang menunggu persetujuan.")
        now = _now()
        subtotal = rec["subtotal"]
        if approve:
            con.execute("UPDATE receipts SET discount_state='approved', discount_approved_by=?, discount_approved_at=?, discount_prev=0, "
                        "discount_prev_reason=NULL WHERE id=?", (by, now, receipt_id))
            _price_event(con, "receipt", receipt_id, "Diskon nota", "approved", 1, subtotal, None, rec["discount"], rec["discount"],
                         note or rec["discount_reason"], rec["discount_below_cost"] or 0, by)
            _log_receipt_event(con, receipt_id, "item_changed", f"Diskon nota Rp {rec['discount']:,} DISETUJUI Admin ({by})".replace(",", "."), by)
        else:
            back = rec["discount_prev"] or 0
            cols = dict(_NO_DISC)
            if back > 0:  # the earlier, approved discount stands again
                cols.update(discount_state="approved", discount_reason=rec["discount_prev_reason"], discount_requested_by=None,
                            discount_approved_by="(disetujui sebelumnya)")
            con.execute(f"UPDATE receipts SET discount=?, {', '.join(c + '=?' for c in _DISC_COLS)} WHERE id=?",
                        (back, *cols.values(), receipt_id))
            _price_event(con, "receipt", receipt_id, "Diskon nota", "rejected", 1, subtotal, rec["discount"], back, back, note, 0, by)
            _log_receipt_event(con, receipt_id, "item_changed",
                               f"Diskon nota Rp {rec['discount']:,} DITOLAK Admin ({by}): {note}; kembali ke Rp {back:,}".replace(",", "."), by)
            _recalc_receipt_money(con, receipt_id, by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def _recalc_receipt_money(con: sqlite3.Connection, receipt_id: str, by: str | None) -> None:
    """Totals and the Lunas/Belum status after a price decision. Payments are never touched: a total that drops below
    what was paid becomes Kelebihan Bayar, which an admin refunds with proof (K-20)."""
    rec = dict(con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone())
    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)).fetchall()]
    subtotal = sum(_line_total(i) for i in items)
    discount = min(rec["discount"] or 0, subtotal)
    total = subtotal - discount - (rec.get("return_total") or 0)
    if rec["status"] == "void":
        status = "void"
    elif rec["work_status"] == "draft":
        status = "pending"
    else:
        status = _compute_receipt_status(total, rec["amount_paid"], None)
        if status == "done" and rec["status"] != "done" and _receipt_price_pending(con, receipt_id):
            status = "pending"
    con.execute("UPDATE receipts SET subtotal=?, discount=?, total=?, status=? WHERE id=?", (subtotal, discount, total, status, receipt_id))
    if total < rec["amount_paid"] and total != rec["total"] and status != "void":
        _log_receipt_event(con, receipt_id, "overpaid",
                           f"Total nota turun menjadi Rp {total:,}; kelebihan bayar Rp {rec['amount_paid'] - total:,} perlu dikembalikan ke pelanggan.".replace(",", "."), by)


def decide_price_request(scope: str, doc_id: str, item_id: int, approve: bool, note: str | None, user: dict) -> dict:
    """Admin only. Approve keeps the special price. Reject puts the line back at the Master Barang price (the price
    it was asked at, no discount) and recomputes the bill; money already paid stays on the record."""
    if user.get("role") != "admin":
        raise PermissionError("Hanya Admin yang dapat menyetujui atau menolak harga khusus.")
    by = user.get("username")
    note = (note or "").strip()[:300] or None
    if not approve and not note:
        raise ValueError("Tulis alasan penolakan harga khusus.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if scope == "receipt":
            line = con.execute("SELECT * FROM receipt_items WHERE id=? AND receipt_id=?", (item_id, doc_id)).fetchone()
            rec = con.execute("SELECT status FROM receipts WHERE id=?", (doc_id,)).fetchone()
            if line is None or rec is None:
                raise LookupError("Baris nota tidak ditemukan.")
            if rec["status"] == "void":
                raise ValueError("Nota sudah dibatalkan (void).")
        elif scope == "preorder":
            line = con.execute("SELECT * FROM preorder_items WHERE id=? AND preorder_id=?", (item_id, doc_id)).fetchone()
            if line is None:
                raise LookupError("Baris Sales Order tidak ditemukan.")
        else:
            raise ValueError("Jenis dokumen tidak dikenal.")
        line = dict(line)
        if line["price_state"] != "pending":
            raise ValueError("Harga khusus ini tidak sedang menunggu persetujuan.")
        qty, listp = line["quantity"], line["list_price"] or line["unit_price"]
        net = qty * line["unit_price"] - (line.get("line_discount") or 0)
        now = _now()
        table = "receipt_items" if scope == "receipt" else "preorder_items"
        if approve:
            con.execute(f"UPDATE {table} SET price_state='approved', price_approved_by=?, price_approved_at=? WHERE id=?", (by, now, item_id))
            _price_event(con, scope, doc_id, line["product_name"], "approved", qty, listp, None, round(net / qty), qty * listp - net,
                         note or line["price_reason"], line["price_below_cost"] or 0, by)
            if scope == "preorder":
                con.execute("UPDATE receipt_items SET price_state='approved', price_approved_by=?, price_approved_at=? "
                            "WHERE preorder_item_id=? AND price_state='pending' AND line_discount=0", (by, now, item_id))
                for r in con.execute("SELECT DISTINCT receipt_id FROM receipt_items WHERE preorder_item_id=?", (item_id,)).fetchall():
                    _recalc_receipt_money(con, r["receipt_id"], by)
            else:
                _log_receipt_event(con, doc_id, "item_changed", f"{line['product_name']}: harga khusus DISETUJUI Admin ({by})", by)
                _recalc_receipt_money(con, doc_id, by)
        else:
            _price_event(con, scope, doc_id, line["product_name"], "rejected", qty, listp, round(net / qty), listp, 0, note, 0, by)
            if scope == "receipt":
                if line.get("preorder_item_id"):
                    pi = con.execute("SELECT * FROM preorder_items WHERE id=?", (line["preorder_item_id"],)).fetchone()
                    cols = {c: (pi[c] if pi else _NO_PRICE[c]) for c in _PRICE_COLS}
                    con.execute(f"UPDATE receipt_items SET line_discount=0, {', '.join(c + '=?' for c in _PRICE_COLS)} WHERE id=?",
                                (*cols.values(), item_id))
                else:
                    con.execute(f"UPDATE receipt_items SET unit_price=?, line_discount=0, {', '.join(c + '=?' for c in _PRICE_COLS)} WHERE id=?",
                                (listp, *_NO_PRICE.values(), item_id))
                _log_receipt_event(con, doc_id, "item_changed", f"{line['product_name']}: harga khusus DITOLAK Admin ({by}): {note}; kembali ke harga normal", by)
                _recalc_receipt_money(con, doc_id, by)
            else:
                before = _po_invoice_snapshot(con, doc_id)
                con.execute(f"UPDATE preorder_items SET unit_price=?, {', '.join(c + '=?' for c in _PRICE_COLS)} WHERE id=?",
                            (listp, *_NO_PRICE.values(), item_id))
                con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, doc_id))
                _po_invoice_sync(con, doc_id, by, before)
    return {"scope": scope, "doc_id": doc_id, "item_id": item_id, "state": "approved" if approve else "rejected"}


# --- Notice Center (diagram 26) --------------------------------------------------------------------------------
# Read-only: every notice is worked out from the documents each time the dashboard opens, nothing is stored or edited.
NOTICE_LIMIT = 8


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def get_notices(is_admin: bool = False) -> dict:
    """What needs someone's attention, grouped, most urgent first. Money owed to suppliers is admin-only."""
    today = shop_today().isoformat()
    groups: list[dict] = []

    def add(key: str, severity: str, title: str, hint: str, rows: list[dict]) -> None:
        if rows:
            groups.append({"key": key, "severity": severity, "title": title, "hint": hint, "count": len(rows),
                           "items": rows[:NOTICE_LIMIT], "more": max(len(rows) - NOTICE_LIMIT, 0)})

    with _conn() as con:
        # Sales Order lines that must be bought but have no supplier yet.
        add("no_supplier", "tinggi", "Supplier belum dipilih", "Pilih supplier di Sales Order agar draft Purchase Order bisa dibuat.", [
            {"title": f"{r['po_number']} · {r['customer_name']}", "sub": f"{r['product_name']} × {r['quantity']}",
             "link": {"type": "preorder", "id": r["preorder_id"]}}
            for r in con.execute(
                f"""SELECT pi.preorder_id, pi.product_name, pi.quantity, p.po_number, p.customer_name
                    FROM preorder_items pi JOIN preorders p ON p.id = pi.preorder_id
                    WHERE p.is_draft=0 AND p.cancelled_at IS NULL AND pi.source='supplier' AND pi.supplier_id IS NULL
                      AND pi.status NOT IN ({_placeholders(PO_CLOSED_LINE_STATUSES + PO_ARRIVED_STATUSES)})
                    ORDER BY p.created_at""", (*PO_CLOSED_LINE_STATUSES, *PO_ARRIVED_STATUSES))])

        # Lines with a supplier that are on no Purchase Order at all.
        add("unordered", "tinggi", "Barang belum dipesan", "Barang Sales Order yang belum masuk Purchase Order mana pun.", [
            {"title": f"{r['po_number']} · {r['customer_name']}", "sub": f"{r['product_name']} × {r['quantity']} · {r['supplier_name']}",
             "link": {"type": "preorder", "id": r["preorder_id"]}}
            for r in con.execute(
                f"""SELECT pi.preorder_id, pi.product_name, pi.quantity, p.po_number, p.customer_name, s.name AS supplier_name
                    FROM preorder_items pi JOIN preorders p ON p.id = pi.preorder_id JOIN suppliers s ON s.id = pi.supplier_id
                    WHERE p.is_draft=0 AND p.cancelled_at IS NULL AND pi.source='supplier'
                      AND pi.status NOT IN ({_placeholders(PO_CLOSED_LINE_STATUSES + PO_ARRIVED_STATUSES)})
                      AND NOT EXISTS (SELECT 1 FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                                      WHERE spi.preorder_item_id = pi.id AND sp.status <> 'batal')
                    ORDER BY p.created_at""", (*PO_CLOSED_LINE_STATUSES, *PO_ARRIVED_STATUSES))])

        # Draft Purchase Orders that serve a customer but were never sent to the supplier.
        add("draft_po", "sedang", "Draft Purchase Order belum diterbitkan", "Review lalu terbitkan agar barang benar-benar dipesan.", [
            {"title": r["po_number"], "sub": f"{r['supplier_name_snapshot']} · {r['n']} barang untuk Sales Order · dibuat {_days_between(r['created_at'], today)} hari lalu",
             "link": {"type": "supplier_po", "id": r["id"]}}
            for r in con.execute(
                """SELECT sp.id, sp.po_number, sp.supplier_name_snapshot, sp.created_at, COUNT(*) AS n
                   FROM supplier_pos sp JOIN supplier_po_items spi ON spi.supplier_po_id = sp.id AND spi.preorder_item_id IS NOT NULL
                   WHERE sp.status='draft' GROUP BY sp.id ORDER BY sp.created_at""")])

        # ETA passed and goods still outstanding.
        late = []
        for r in con.execute(
            """SELECT sp.id, sp.po_number, sp.supplier_name_snapshot, spi.product_name,
                      spi.quantity - spi.qty_cancelled - spi.qty_received AS left, COALESCE(spi.eta_date, sp.est_arrival_date) AS eta
               FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
               WHERE sp.status IN ('dipesan','sebagian_diterima') AND spi.quantity - spi.qty_cancelled - spi.qty_received > 0
                 AND COALESCE(spi.eta_date, sp.est_arrival_date) IS NOT NULL AND COALESCE(spi.eta_date, sp.est_arrival_date) < ?
               ORDER BY eta""", (today,)):
            late.append({"title": f"{r['po_number']} · {r['supplier_name_snapshot']}",
                         "sub": f"{r['product_name']} × {r['left']} · ETA {r['eta'][8:10]}/{r['eta'][5:7]}/{r['eta'][:4]}, terlambat {_days_between(r['eta'], today)} hari",
                         "link": {"type": "supplier_po", "id": r["id"]}})
        add("late_shipping", "tinggi", "Pengiriman terlambat", "Hubungi supplier atau perbarui ETA.", late)

        add("partial", "sedang", "Barang datang sebagian", "Sebagian sudah diterima, sisanya masih ditunggu.", [
            {"title": f"{r['po_number']} · {r['supplier_name_snapshot']}", "sub": f"sisa {r['left']} dari {r['total']} unit",
             "link": {"type": "supplier_po", "id": r["id"]}}
            for r in con.execute(
                """SELECT sp.id, sp.po_number, sp.supplier_name_snapshot, SUM(spi.quantity - spi.qty_cancelled) AS total,
                          SUM(spi.quantity - spi.qty_cancelled - spi.qty_received) AS left
                   FROM supplier_pos sp JOIN supplier_po_items spi ON spi.supplier_po_id = sp.id
                   WHERE sp.status='sebagian_diterima' GROUP BY sp.id HAVING left > 0 ORDER BY sp.created_at""")])

        # Goods counted at the PO but the Barang Masuk was never posted: stock has not moved yet (K-10).
        add("inbound_draft", "sedang", "Barang Masuk belum diposting", "Posting agar stok bertambah dan Sales Order ikut maju.", [
            {"title": r["doc_number"], "sub": f"{r['supplier_name']} · tanggal terima {r['received_date']} · {_days_between(r['received_date'], today)} hari lalu",
             "link": {"type": "inbound_doc", "id": r["id"]}}
            for r in con.execute(
                """SELECT d.id, d.doc_number, d.received_date, s.name AS supplier_name FROM inbound_documents d
                   JOIN suppliers s ON s.id = d.supplier_id WHERE d.status='draft' AND d.supplier_po_id IS NOT NULL
                   ORDER BY d.received_date""")])

        # Stock In Allocation: what open Sales Orders need but nobody has ordered or stocked.
        lines = [ln for ln in _reservation_lines(con) if ln["shortfall"] > 0]
        add("shortfall", "tinggi", "Kebutuhan Sales Order belum tertutup", "Stok dan Purchase Order belum cukup untuk barang ini.", [
            {"title": f"{ln['po_number']} · {ln['customer_name']}", "sub": f"{ln['product_name']} kurang {ln['shortfall']} dari {ln['quantity']}",
             "link": {"type": "preorder", "id": ln["preorder_id"]}} for ln in lines])

        reserved = _reserved_by_item(con)
        out_rows, held_rows = [], []
        for r in con.execute("SELECT id, sku, name, stock_qty, min_stock, unit FROM master_items WHERE is_active=1 AND archived_at IS NULL ORDER BY name"):
            held = reserved.get(r["id"], 0)
            if r["stock_qty"] <= 0 and (r["min_stock"] or 0) > 0:
                out_rows.append({"title": r["name"], "sub": f"{r['sku']} · stok habis" + (f" · {held} dicadangkan" if held else ""),
                                 "link": {"type": "master_item", "id": r["id"]}})
            elif held > 0 and r["stock_qty"] - held <= 0:
                held_rows.append({"title": r["name"], "sub": f"{r['sku']} · stok {r['stock_qty']} semuanya dicadangkan untuk Sales Order",
                                  "link": {"type": "master_item", "id": r["id"]}})
        add("stock_out", "tinggi", "Stok habis", "Barang dengan stok minimum yang kini kosong.", out_rows)
        add("stock_held", "sedang", "Stok bebas habis (dicadangkan)", "Seluruh stok sudah dicadangkan; penjualan langsung perlu izin Admin.", held_rows)

        # All goods of a Sales Order have arrived and the work has not started.
        add("ready_install", "info", "Barang siap dipasang", "Semua barang Sales Order sudah sampai; jadwalkan pengerjaan.", [
            {"title": f"{r['receipt_no']} · {r['plate_full']}", "sub": f"{r['customer_name'] or '-'} · {r['po_number'] or 'Sales Order'}",
             "link": {"type": "receipt", "id": r["id"]}}
            for r in con.execute(
                """SELECT r.id, r.receipt_no, r.plate_full, r.customer_name, p.po_number FROM receipts r
                   JOIN preorders p ON p.id = r.preorder_id WHERE r.status<>'void' AND r.work_status='belum' ORDER BY r.created_at""")])

        if is_admin:
            debt_over, debt_soon = [], []
            soon = (shop_today() + timedelta(days=7)).isoformat()
            for r in con.execute(
                """SELECT id, invoice_number, supplier_name_snapshot, due_date, total - amount_paid AS owed FROM purchase_invoices
                   WHERE status='posted' AND total - amount_paid > 0 AND due_date <= ? ORDER BY due_date""", (soon,)):
                row = {"title": f"{r['invoice_number']} · {r['supplier_name_snapshot']}", "link": {"type": "purchase_invoice", "id": r["id"]},
                       "sub": f"sisa Rp {r['owed']:,}".replace(",", ".") + f" · jatuh tempo {r['due_date'][8:10]}/{r['due_date'][5:7]}/{r['due_date'][:4]}"}
                if r["due_date"] < today:
                    row["sub"] += f", lewat {_days_between(r['due_date'], today)} hari"
                    debt_over.append(row)
                else:
                    debt_soon.append(row)
            add("refund_claims", "sedang", "Refund / Kredit Supplier belum diterima", "Uang muka PO yang dibatalkan, menunggu uang masuk atau nota debit supplier.", [
                {"title": f"{r['claim_no']} · {r['supplier_name']}", "link": {"type": "supplier_po", "id": r["supplier_po_id"]},
                 "sub": f"{'Refund' if r['kind'] == 'refund' else 'Kredit'} sisa Rp {r['amount'] - r['settled_amount']:,}".replace(",", ".")
                        + f" · {r['po_number']} · {_days_between(r['created_at'], today)} hari lalu"}
                for r in con.execute(
                    """SELECT c.*, s.name AS supplier_name, p.po_number FROM supplier_refund_claims c JOIN suppliers s ON s.id = c.supplier_id
                       JOIN supplier_pos p ON p.id = c.supplier_po_id WHERE c.status='menunggu' ORDER BY c.created_at""")])
            add("debt_overdue", "tinggi", "Utang supplier lewat jatuh tempo", "Faktur pembelian yang belum lunas dan sudah lewat tempo.", debt_over)
            add("debt_soon", "sedang", "Utang supplier jatuh tempo 7 hari", "Siapkan pembayaran.", debt_soon)

        active = con.execute("SELECT COUNT(*) AS n FROM supplier_pos WHERE status IN ('dipesan','sebagian_diterima')").fetchone()["n"]
        if active:
            groups.append({"key": "active_pos", "severity": "info", "title": "Purchase Order aktif", "hint": "Sedang dipesan atau menunggu barang datang.",
                           "count": active, "items": [], "more": 0, "link": {"type": "supplier_po_list"}})
    order = {"tinggi": 0, "sedang": 1, "info": 2}
    groups.sort(key=lambda g: order[g["severity"]])
    return {"generated_at": _now(), "urgent": sum(g["count"] for g in groups if g["severity"] == "tinggi"), "groups": groups}


def list_pending_prices() -> list[dict]:
    """Every special price waiting for an admin, newest first, with enough to decide without opening the document."""
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT 'receipt' AS scope, ri.receipt_id AS doc_id, r.receipt_no AS doc_no, ri.id AS item_id, ri.product_name, ri.quantity,
                      ri.unit_price, ri.line_discount, ri.list_price, ri.price_reason, ri.price_requested_by, ri.price_requested_at,
                      r.customer_name, r.plate_full
               FROM receipt_items ri JOIN receipts r ON r.id = ri.receipt_id
               WHERE ri.price_state='pending' AND r.status<>'void'
               UNION ALL
               SELECT 'preorder', pi.preorder_id, p.po_number, pi.id, pi.product_name, pi.quantity, pi.unit_price, 0, pi.list_price,
                      pi.price_reason, pi.price_requested_by, pi.price_requested_at, p.customer_name, p.plate_full
               FROM preorder_items pi JOIN preorders p ON p.id = pi.preorder_id
               WHERE pi.price_state='pending' AND pi.status<>? AND p.cancelled_at IS NULL
               ORDER BY price_requested_at DESC""", (PO_LINE_CANCELLED,))]
        for r in rows:
            net = r["quantity"] * r["unit_price"] - (r["line_discount"] or 0)
            r["special_unit_price"] = round(net / r["quantity"])
            r["discount_total"] = r["quantity"] * (r["list_price"] or 0) - net
        # The nota's own discount: shown as one row ("Diskon nota", item_id 0) from the subtotal to the discounted total.
        for d in con.execute(
            """SELECT r.id AS doc_id, r.receipt_no AS doc_no, r.subtotal, r.discount, r.discount_reason, r.discount_requested_by,
                      r.discount_requested_at, r.customer_name, r.plate_full FROM receipts r
               WHERE r.discount_state='pending' AND r.status<>'void'"""):
            rows.append({"scope": "receipt_discount", "doc_id": d["doc_id"], "doc_no": d["doc_no"], "item_id": 0, "product_name": "Diskon nota",
                         "quantity": 1, "unit_price": d["subtotal"], "line_discount": d["discount"], "list_price": d["subtotal"],
                         "price_reason": d["discount_reason"], "price_requested_by": d["discount_requested_by"],
                         "price_requested_at": d["discount_requested_at"] or "", "customer_name": d["customer_name"],
                         "plate_full": d["plate_full"], "special_unit_price": d["subtotal"] - d["discount"], "discount_total": d["discount"]})
        rows.sort(key=lambda r: r["price_requested_at"] or "", reverse=True)
        return rows


def list_price_events(scope: str, doc_id: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM price_events WHERE scope=? AND doc_id=? ORDER BY id DESC", (scope, doc_id)).fetchall()]


# Work stage, independent of payment: draft (parked, no stock, no money) -> belum (Belum Dikerjakan)
# -> pengerjaan (Dalam Pengerjaan) -> selesai (Pengerjaan Selesai). Payment status is derived from
# amount_paid vs total and lives in receipts.status (pending / done / void).
# "menunggu" (Menunggu Barang) is only ever set by a Sales Order invoice whose goods have not all
# arrived; it moves on to "belum" by itself once they have (or by hand).
RECEIPT_WORK_STATUSES = ("draft", "menunggu", "belum", "pengerjaan", "selesai")
_WORK_TRANSITIONS = {
    "draft": {"belum", "pengerjaan", "selesai"},
    "menunggu": {"belum", "pengerjaan", "selesai"},
    "belum": {"pengerjaan", "selesai"},
    "pengerjaan": {"belum", "selesai"},
    "selesai": {"pengerjaan"},
}


def _is_closed(row) -> bool:
    """Lunas and Pengerjaan Selesai: the transaction is closed and read-only."""
    return row["status"] == "done" and row["work_status"] == "selesai"


def _holds_stock(status: str, work_status: str) -> bool:
    """A draft is only a note to self and a void nota is cancelled: neither has taken
    anything off the shelf."""
    return status != "void" and work_status != "draft"


def _line_total(item: dict) -> int:
    return item["quantity"] * item["unit_price"] - (item.get("line_discount") or 0)


def _log_receipt_event(con: sqlite3.Connection, receipt_id: str, kind: str, note: str | None, by: str | None) -> None:
    con.execute(
        "INSERT INTO receipt_events (receipt_id, at, by_user, kind, note) VALUES (?, ?, ?, ?, ?)",
        (receipt_id, _now(), by, kind, note),
    )


def get_receipt_events(receipt_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM receipt_events WHERE receipt_id=? ORDER BY at ASC, id ASC", (receipt_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# "dititipkan": arrived and paid for, the customer asked the shop to keep it for now (owner, 25-10-2026). The goods
# stay In Hand, reserved for the customer, until the line is handed over (terpasang).
PO_ARRIVED_STATUSES = ("sampai_di_toko", "dititipkan", "terpasang")
# K-01: a Sales Order line leaves stock only once it is installed / handed over. Until then the goods
# stay In Hand and are reserved for the customer (see _reservation_lines).
PO_INSTALLED_STATUSES = ("terpasang",)
# K-13: a cancelled Sales Order line. Not one of PREORDER_STATUSES (nobody sets it by hand); it is set
# only by cancel_preorder / cancel_preorder_items, and such a line never moves again.
PO_LINE_CANCELLED = "dibatalkan"
PO_CLOSED_LINE_STATUSES = PO_INSTALLED_STATUSES + (PO_LINE_CANCELLED,)


def _receipt_item_holdings(
    items: list[dict], con: sqlite3.Connection | None = None, counted_statuses: tuple = PO_INSTALLED_STATUSES,
) -> dict[int, int]:
    """Sum of quantity per master_item_id across a set of receipt items, skipping
    lines with no master item link. This is the 'holding' referenced throughout
    the stock-movement code: what a non-void receipt currently has taken off the
    shelf, per product. A line that comes from a Sales Order holds nothing until
    that SO line is installed (terpasang) -- pass `con` so it can look."""
    holdings: dict[int, int] = {}
    for item in items:
        master_item_id = item.get("master_item_id")
        if master_item_id is None or item.get("item_type") == "jasa":
            continue
        if con is not None and item.get("preorder_item_id"):
            po_line = con.execute("SELECT status FROM preorder_items WHERE id=?", (item["preorder_item_id"],)).fetchone()
            if po_line is None or po_line["status"] not in counted_statuses:
                continue
        held = item["quantity"] - (item.get("returned_stock_qty") or 0)
        if held > 0:
            holdings[master_item_id] = holdings.get(master_item_id, 0) + held
    return holdings


class StockReservedError(ValueError):
    """A walk-in sale would take stock that is reserved for a Sales Order customer. An admin may
    override it with a reason."""


def _reservation_lines(con: sqlite3.Connection, master_item_ids=None) -> list[dict]:
    """K-02 Stock In Allocation, worked out from the documents instead of kept as a running counter,
    so it can never drift. Every open Sales Order line (published, not archived, invoice not void,
    not yet installed) wants `quantity − still coming from a supplier`; what is still on a Purchase
    Order (not yet posted in via Barang Masuk) is not on the shelf, so it can't be held. A receipt
    (GR) counts as in stock once its Barang Masuk is posted — both receiving paths link the two.

    The shelf (stock_qty) is shared out oldest Sales Order first, in two passes: goods that arrived
    on a line's own Purchase Order go to that line before anything else, so one customer's delivery
    is never handed to another; then the rest is spread over lines served from ready stock.
    `reserved` is what a line holds; `shortfall` is what it still needs and nobody has ordered."""
    params: list = []
    item_sql = ""
    if master_item_ids is not None:
        ids = sorted({i for i in master_item_ids if i is not None})
        if not ids:
            return []
        item_sql = f" AND pi.master_item_id IN ({_placeholders(ids)})"
        params = list(ids)
    rows = con.execute(
        f"""SELECT pi.id, pi.preorder_id, pi.master_item_id, pi.product_name, pi.quantity, pi.status, pi.source,
                   p.po_number, p.customer_name,
                   COALESCE((SELECT SUM(spi.quantity - spi.qty_cancelled) FROM supplier_po_items spi
                             JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                             WHERE spi.preorder_item_id = pi.id AND sp.status <> 'batal'), 0) AS ordered,
                   COALESCE((SELECT SUM(srl.quantity) FROM spo_receipt_lines srl
                             JOIN spo_receipts sr ON sr.id = srl.receipt_id
                             JOIN inbound_documents d ON d.id = sr.inbound_document_id
                             JOIN supplier_po_items spi ON spi.id = srl.item_id
                             JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                             WHERE spi.preorder_item_id = pi.id AND d.status = 'posted' AND sp.status <> 'batal'
                               AND COALESCE(srl.condition, 'baik') NOT IN ('rusak', 'tidak_sesuai')), 0) AS posted
            FROM preorder_items pi
            JOIN preorders p ON p.id = pi.preorder_id
            LEFT JOIN receipts r ON r.id = p.receipt_id
            WHERE p.is_draft = 0 AND pi.master_item_id IS NOT NULL AND COALESCE(pi.source, '') <> 'jasa'
              AND pi.status NOT IN ({_placeholders(PO_CLOSED_LINE_STATUSES)})
              AND COALESCE(r.status, '') <> 'void'
              AND (pi.receipt_id IS NULL OR pi.receipt_id = p.receipt_id){item_sql}
            ORDER BY p.created_at ASC, pi.position ASC, pi.id ASC""",
        [*PO_CLOSED_LINE_STATUSES, *params],
    ).fetchall()
    lines = [dict(r) for r in rows]
    if not lines:
        return []
    mids = sorted({ln["master_item_id"] for ln in lines})
    pool = {r["id"]: max(r["stock_qty"], 0) for r in con.execute(
        f"SELECT id, stock_qty FROM master_items WHERE id IN ({_placeholders(mids)})", mids).fetchall()}
    for ln in lines:
        coming = max(ln["ordered"] - ln["posted"], 0)
        ln["coming"] = coming
        ln["wanted"] = max(ln["quantity"] - coming, 0)
        ln["arrived"] = min(ln["posted"], ln["wanted"])
        ln["reserved"] = 0
    for part in ("arrived", "ready"):
        for ln in lines:
            # A line set to 'Pesan Supplier' waits for its own goods; it never takes ready stock.
            want = ln["arrived"] if part == "arrived" else (0 if ln["source"] == "supplier" else ln["wanted"] - ln["arrived"])
            got = min(want, pool.get(ln["master_item_id"], 0))
            if got > 0:
                pool[ln["master_item_id"]] -= got
                ln["reserved"] += got
    for ln in lines:
        ln["shortfall"] = ln["wanted"] - ln["reserved"]
    return lines


def _reserved_by_item(con: sqlite3.Connection, master_item_ids=None) -> dict[int, int]:
    out: dict[int, int] = {}
    for ln in _reservation_lines(con, master_item_ids):
        if ln["reserved"]:
            out[ln["master_item_id"]] = out.get(ln["master_item_id"], 0) + ln["reserved"]
    return out


def reserved_qty_by_item(master_item_ids=None) -> dict[int, int]:
    with _conn() as con:
        return _reserved_by_item(con, master_item_ids)


def get_stock_reservations(master_item_id: int) -> list[dict]:
    """Which Sales Orders hold this item, for the item's stock panel."""
    with _conn() as con:
        return [
            {k: ln[k] for k in ("id", "preorder_id", "po_number", "customer_name", "product_name", "quantity",
                                "status", "reserved", "coming", "shortfall")}
            for ln in _reservation_lines(con, [master_item_id]) if ln["reserved"] or ln["coming"] or ln["shortfall"]
        ]


def _walkin_holdings(items: list[dict], con: sqlite3.Connection) -> dict[int, int]:
    """What the receipt's own (non-Sales-Order) lines hold: the part a reservation can block."""
    return _receipt_item_holdings([i for i in items if not i.get("preorder_item_id")], con)


def _apply_receipt_stock_delta(
    con: sqlite3.Connection, receipt_id: str, before: dict[int, int], after: dict[int, int],
    walkin: tuple[dict[int, int], dict[int, int]] | None = None, override_reason: str | None = None,
    by: str | None = None,
) -> None:
    """The single place a sale moves stock. A non-void receipt HOLDS
    sum(quantity) per master_item_id; a void or deleted receipt holds NOTHING.
    Every receipt mutation (create, item/quantity edit, void, delete) computes
    its holding before and after and calls this once — that one invariant
    covers all of those sites without special-casing any of them.

    Overselling is refused: a change that would take a product below zero raises
    ValueError before anything is written, and the caller's transaction rolls back
    the whole save. The receipt row is written first, so this connection already
    holds SQLite's write lock here -- two notas saved at once are checked one after
    the other, and the second sees the first one's sale (first to pay gets the stock).

    ref_id is left NULL: stock_ledger.ref_id is INTEGER but receipt ids are TEXT
    UUIDs, so it can't hold one — the receipt id goes in `note` instead."""
    touched_ids = sorted(set(before) | set(after))
    for master_item_id in touched_ids:
        taken = after.get(master_item_id, 0) - before.get(master_item_id, 0)
        if taken <= 0:
            continue
        mi = con.execute("SELECT name, stock_qty FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if mi is not None and mi["stock_qty"] - taken < 0:
            raise ValueError(
                f"Stok {mi['name']} tidak cukup: tersedia {max(mi['stock_qty'], 0)}, dibutuhkan {taken}."
            )
    # K-02: a walk-in sale may not take what is reserved for a Sales Order customer, unless an admin
    # overrides it with a reason. Sales Order lines themselves are never blocked here: their own
    # reservation is what they consume.
    if walkin is not None:
        w_before, w_after = walkin
        grow = {m: w_after.get(m, 0) - w_before.get(m, 0) for m in set(w_before) | set(w_after)}
        grow = {m: q for m, q in grow.items() if q > 0}
        if grow:
            reserved = _reserved_by_item(con, list(grow))
            conflicts = []
            for mid, q in sorted(grow.items()):
                if not reserved.get(mid):
                    continue
                mi = con.execute("SELECT name, stock_qty FROM master_items WHERE id=?", (mid,)).fetchone()
                free = max(mi["stock_qty"] - reserved[mid], 0)
                if q > free:
                    conflicts.append(f"{mi['name']} (bebas {free}, dicadangkan {reserved[mid]}, diminta {q})")
            if conflicts:
                reason = (override_reason or "").strip()
                if not reason:
                    raise StockReservedError(
                        "Stok sudah dicadangkan untuk pelanggan Sales Order: " + "; ".join(conflicts)
                        + ". Kurangi jumlahnya, atau minta admin mengizinkan dengan alasan."
                    )
                _log_receipt_event(con, receipt_id, "override",
                                   "Izin admin memakai stok yang dicadangkan: " + "; ".join(conflicts)
                                   + f". Alasan: {reason}", by)
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
# `revision` is a cheap fingerprint of everything a second cashier could change under you (money, status,
# lines, payments, timeline). A save that sends the revision it was loaded with is refused (409) when it
# no longer matches, instead of silently overwriting the other person's edit.
_RECEIPT_REVISION_SQL = """
    (SELECT COUNT(*) FROM receipt_events e WHERE e.receipt_id = r.id) || '-' || r.total || '-' || r.amount_paid
    || '-' || r.work_status || '-' || r.status || '-' || r.discount || '-'
    || COALESCE((SELECT SUM(ri.id * 7 + ri.quantity * 131 + ri.unit_price + ri.line_discount * 3 + ri.returned_qty * 17)
                 FROM receipt_items ri WHERE ri.receipt_id = r.id), 0) || '-'
    || (SELECT COUNT(*) FROM receipt_payments p WHERE p.receipt_id = r.id)
    || '-' || (SELECT COUNT(*) FROM receipt_payments p WHERE p.receipt_id = r.id AND p.state = 'pending')
    || '-' || COALESCE(r.work_stage, '') || '-' || COALESCE(r.handed_over_at, '')"""

_RECEIPT_SELECT = f"""SELECT r.*,
        CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_full_name,
        {_RECEIPT_REVISION_SQL} AS revision,
        (SELECT p.po_number FROM preorders p WHERE p.id = r.preorder_id) AS po_number,
        (SELECT COUNT(*) FROM receipts x WHERE x.preorder_id = r.preorder_id AND x.status <> 'void') AS invoice_count,
        (SELECT COALESCE(SUM(p.amount),0) FROM receipt_payments p WHERE p.receipt_id = r.id AND p.state = 'pending') AS pending_amount,
        (SELECT s.service_no FROM service_orders s WHERE s.id = r.service_order_id) AS service_no,
        (SELECT e.name FROM employees e WHERE e.id = r.sales_employee_id) AS sales_employee_name,
        -- A nota with work on the car: a vehicle plate, a technician on a line, or a jasa line. Without any of them
        -- (a walk-in purchase: motor, belanja biasa) the Pengerjaan status means nothing and is not shown.
        (EXISTS (SELECT 1 FROM receipt_item_technicians t WHERE t.receipt_id = r.id)
         OR EXISTS (SELECT 1 FROM receipt_items ji WHERE ji.receipt_id = r.id AND ji.item_type = 'jasa')
         OR r.preorder_id IS NOT NULL OR r.service_order_id IS NOT NULL OR TRIM(r.plate_full) <> '') AS has_job
    FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id"""


RECEIPT_NO_PREFIX = "PJSG"


def _next_receipt_no(con: sqlite3.Connection, created_at: str) -> str:
    """PJSG/YY/MM/NNNN: a counter per calendar month on the shop's wall clock, restarting at
    0001. Called inside the inserting transaction, so two saves can't take the same number."""
    local = datetime.fromisoformat(created_at) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)
    prefix = f"{RECEIPT_NO_PREFIX}/{local:%y}/{local:%m}/"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(receipt_no, ?) AS INTEGER)) AS n FROM receipts WHERE receipt_no LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:04d}"


def peek_next_receipt_no() -> str:
    """The number the next nota will get, shown read-only on a new nota; the real one is assigned when it is saved."""
    with _conn() as con:
        return _next_receipt_no(con, _now())


# Pick lists for a transfer payment: who sent it, and which of the shop's accounts received
# it. Filled as transfers are saved; the cashier removes stale entries from the payment pop-up.
# Keys are fixed here, so the table name in the SQL below never comes from a request.
_TRANSFER_LISTS = {"sender": "transfer_senders", "account": "transfer_accounts"}


def _remember_transfer_name(con: sqlite3.Connection, kind: str, name: str | None) -> None:
    if name and name.strip():
        cleaned = " ".join(name.split())
        con.execute(
            f"INSERT OR IGNORE INTO {_TRANSFER_LISTS[kind]} (name, name_key, created_at) VALUES (?, ?, ?)",
            (cleaned, cleaned.upper(), _now()),
        )


def list_transfer_names(kind: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(f"SELECT id, name FROM {_TRANSFER_LISTS[kind]} ORDER BY name_key ASC").fetchall()
        return [dict(r) for r in rows]


def delete_transfer_name(kind: str, entry_id: int) -> None:
    """Only the pick list: receipts keep the name they were saved with."""
    with _conn() as con:
        cur = con.execute(f"DELETE FROM {_TRANSFER_LISTS[kind]} WHERE id=?", (entry_id,))
        if cur.rowcount == 0:
            raise ValueError(f"Data {entry_id} tidak ditemukan.")


# K-18: money paid by these methods is not "in" until someone has seen it in the account.
PENDING_METHODS = ("transfer", "qris")


def _pay_pending(method: str | None, confirmed: bool | None) -> bool:
    """True when a payment must be recorded as Pending: transfer/QRIS that the cashier has not ticked as received."""
    return method in PENDING_METHODS and not confirmed


def _insert_receipt_payment(
    con: sqlite3.Connection, receipt_id: str, amount: int, method: str, note: str | None,
    created_by: str | None, transfer_sender: str | None = None, transfer_account: str | None = None,
    paid_at: str | None = None, *, state: str = "sah", kind: str | None = None, proof_ref: str | None = None,
) -> None:
    if method != "transfer":
        transfer_sender = transfer_account = None
    now = _now()
    con.execute(
        """INSERT INTO receipt_payments
               (receipt_id, paid_at, amount, method, transfer_sender, transfer_account, note, created_by,
                state, kind, proof_ref, confirmed_by, confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (receipt_id, paid_at or now, amount, method, transfer_sender, transfer_account, note, created_by,
         state, kind, proof_ref, created_by if state == "sah" and method in PENDING_METHODS and amount > 0 else None,
         now if state == "sah" and method in PENDING_METHODS and amount > 0 else None),
    )


def _refund_proof(proof: str | None) -> str:
    """K-20: every refund must say what proves the money was handed back."""
    proof = (proof or "").strip()
    if not proof:
        raise ValueError("Bukti refund wajib diisi (nomor referensi transfer, atau nama penerima untuk tunai).")
    return proof[:200]


def _pending_sum(con: sqlite3.Connection, receipt_id: str) -> int:
    return con.execute("SELECT COALESCE(SUM(amount),0) AS n FROM receipt_payments WHERE receipt_id=? AND state='pending'",
                       (receipt_id,)).fetchone()["n"]


def _fail_pending_payments(con: sqlite3.Connection, receipt_id: str, reason: str, by: str | None) -> None:
    """A nota that is void takes no money: its Pending payments can never arrive against it."""
    rows = con.execute("SELECT id, amount, method FROM receipt_payments WHERE receipt_id=? AND state='pending'", (receipt_id,)).fetchall()
    for r in rows:
        con.execute("UPDATE receipt_payments SET state='gagal', fail_reason=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                    (reason, by, _now(), r["id"]))
        _log_receipt_event(con, receipt_id, "payment", f"Pembayaran pending Rp {r['amount']:,} ({r['method']}) ditandai gagal: {reason}".replace(",", "."), by)


def get_receipt_payments(receipt_id: str) -> list[dict]:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM receipt_payments WHERE receipt_id=? ORDER BY paid_at ASC, id ASC", (receipt_id,)
        ).fetchall()]
    reversed_ids = {r["reversed_of"] for r in rows if r.get("reversed_of")}
    for r in rows:
        r["reversed"] = r["id"] in reversed_ids
    return rows


def reverse_receipt_payment(receipt_id: str, payment_id: int, reason: str, by: str | None = None) -> dict:
    """Batalkan Pembayaran: payments are never deleted. A reversal writes a negative row that points at
    the original (reversed_of), lowers the paid total, and puts a settled nota back to pending. Messages
    starting 'Alasan' are 422, 'tidak ditemukan' is 404, the rest are conflicts."""
    if not reason or not reason.strip():
        raise ValueError("Alasan pembatalan pembayaran wajib diisi.")
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        pay = con.execute("SELECT * FROM receipt_payments WHERE id=? AND receipt_id=?", (payment_id, receipt_id)).fetchone()
        if pay is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if _is_closed(row):
            raise ValueError("Nota sudah ditutup (Lunas + Pengerjaan Selesai). Buka Kembali dulu (admin) sebelum membatalkan pembayaran.")
        if pay["amount"] <= 0:
            raise ValueError("Hanya pembayaran masuk yang bisa dibatalkan.")
        if pay["state"] == "pending":
            raise ValueError("Pembayaran ini masih menunggu konfirmasi. Tandai gagal (admin) bila dananya tidak masuk.")
        if pay["state"] == "gagal":
            raise ValueError("Pembayaran ini sudah ditandai gagal, tidak ada uang yang perlu dibatalkan.")
        if pay["kind"] == "pindah":
            raise ValueError("Ini pemindahan DP antar invoice Sales Order dan tidak bisa dibatalkan satu sisi. Pakai “Gabung Kembali”.")
        if con.execute("SELECT 1 FROM receipt_payments WHERE reversed_of=?", (payment_id,)).fetchone():
            raise ValueError("Pembayaran ini sudah dibatalkan.")
        if row["amount_paid"] < pay["amount"]:
            raise ValueError("Total pembayaran nota lebih kecil dari pembayaran ini, tidak bisa dibatalkan.")
        new_paid = row["amount_paid"] - pay["amount"]
        new_status = _compute_receipt_status(row["total"], new_paid, None)
        cur = con.execute(
            "UPDATE receipts SET amount_paid=?, status=? WHERE id=? AND amount_paid=?",
            (new_paid, new_status, receipt_id, row["amount_paid"]),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        con.execute(
            """INSERT INTO receipt_payments (receipt_id, paid_at, amount, method, transfer_sender, transfer_account, note, created_by, reversed_of)
               VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
            (receipt_id, _now(), -pay["amount"], pay["method"], f"Pembatalan pembayaran: {reason.strip()}", by, payment_id),
        )
        _log_receipt_event(con, receipt_id, "payment_reversed",
                           f"Pembayaran Rp {pay['amount']:,} ({pay['method']}) dibatalkan: {reason.strip()}".replace(",", "."), by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


PAYMENT_WAITING_NOTE = "Menunggu konfirmasi dana masuk"


def confirm_receipt_payment(receipt_id: str, payment_id: int, by: str | None = None) -> dict:
    """K-18: the money has been seen in the account. Pending -> Sah: it counts as paid from this moment
    (paid_at becomes the confirmation time) and the nota's paid total and status follow."""
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        pay = con.execute("SELECT * FROM receipt_payments WHERE id=? AND receipt_id=?", (payment_id, receipt_id)).fetchone()
        if pay is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if pay["state"] != "pending":
            raise ValueError("Pembayaran ini bukan lagi berstatus menunggu konfirmasi.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        now = _now()
        new_paid = row["amount_paid"] + pay["amount"]
        new_status = _compute_receipt_status(row["total"], new_paid, None)
        # The automatic "waiting" note is replaced once the money is seen (the cashier's own note stays).
        con.execute("UPDATE receipt_payments SET note=NULL WHERE id=? AND state='pending' AND note=?", (payment_id, PAYMENT_WAITING_NOTE))
        cur = con.execute("UPDATE receipt_payments SET state='sah', confirmed_by=?, confirmed_at=?, paid_at=? WHERE id=? AND state='pending'",
                          (by, now, now, payment_id))
        if cur.rowcount == 0:
            raise ValueError("Pembayaran ini baru saja diproses orang lain. Muat ulang.")
        con.execute("UPDATE receipts SET amount_paid=?, status=?, payment_method=?, transfer_sender=?, transfer_account=? WHERE id=?",
                    (new_paid, new_status, pay["method"], pay["transfer_sender"], pay["transfer_account"], receipt_id))
        _log_receipt_event(con, receipt_id, "payment",
                           f"Dana masuk dikonfirmasi: Rp {pay['amount']:,} ({pay['method']}).".replace(",", "."), by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def fail_receipt_payment(receipt_id: str, payment_id: int, reason: str, by: str | None = None) -> dict:
    """K-18: the money never arrived. Pending -> Gagal (kept for the record, never counted)."""
    if not reason or not reason.strip():
        raise ValueError("Alasan gagal wajib diisi.")
    with _conn() as con:
        if con.execute("SELECT 1 FROM receipts WHERE id=?", (receipt_id,)).fetchone() is None:
            raise ValueError("Nota tidak ditemukan.")
        pay = con.execute("SELECT * FROM receipt_payments WHERE id=? AND receipt_id=?", (payment_id, receipt_id)).fetchone()
        if pay is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if pay["state"] != "pending":
            raise ValueError("Hanya pembayaran yang menunggu konfirmasi yang bisa ditandai gagal.")
        con.execute("UPDATE receipt_payments SET state='gagal', fail_reason=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                    (reason.strip(), by, _now(), payment_id))
        _log_receipt_event(con, receipt_id, "payment",
                           f"Pembayaran pending Rp {pay['amount']:,} ({pay['method']}) ditandai GAGAL: {reason.strip()}".replace(",", "."), by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def add_receipt_payment(
    receipt_id: str, amount: int, method: str, note: str | None = None,
    transfer_sender: str | None = None, transfer_account: str | None = None, created_by: str | None = None,
    confirmed: bool | None = None,
) -> dict:
    """Takes one payment (full or part) against a pending receipt. The paid total and
    the done/pending status follow from it. Messages starting 'Jumlah' are rule
    breaches (422), 'tidak ditemukan' is a 404, and the rest are conflicts (409), the
    same convention settle_receipt uses. The UPDATE is a compare-and-set on the paid
    total, so two tills taking a payment at once cannot both succeed on a stale balance."""
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void), tidak bisa menerima pembayaran.")
        if row["status"] == "done":
            raise ValueError("Nota ini sudah lunas, tidak ada sisa pembayaran.")
        if row["work_status"] == "draft":
            raise ValueError("Nota masih Draft. Simpan sebagai Belum Dikerjakan atau mulai pengerjaan dulu.")
        if _receipt_price_pending(con, receipt_id):
            raise ValueError(_PRICE_PENDING_MSG)
        remaining = max(row["total"] - row["amount_paid"], 0)
        if amount <= 0:
            raise ValueError("Jumlah pembayaran harus lebih dari 0.")
        if amount > remaining:
            raise ValueError(f"Jumlah pembayaran melebihi sisa tagihan (Rp {remaining:,}).".replace(",", "."))
        if method != "transfer":
            transfer_sender = transfer_account = None
        if _pay_pending(method, confirmed):
            # K-18: not money in yet. Only what is not already waiting for confirmation can be recorded.
            waiting = _pending_sum(con, receipt_id)
            if amount > remaining - waiting:
                raise ValueError(f"Sudah ada pembayaran menunggu konfirmasi Rp {waiting:,}; sisa yang bisa dicatat Rp {max(remaining - waiting, 0):,}. "
                                 "Konfirmasi atau tandai gagal dulu.".replace(",", "."))
            _insert_receipt_payment(con, receipt_id, amount, method, note or "Menunggu konfirmasi dana masuk", created_by,
                                    transfer_sender, transfer_account, state="pending")
            _log_receipt_event(con, receipt_id, "payment", f"Pembayaran Rp {amount:,} ({method}) dicatat PENDING, menunggu dana masuk.".replace(",", "."), created_by)
            _remember_transfer_name(con, "sender", transfer_sender)
            _remember_transfer_name(con, "account", transfer_account)
            return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())
        new_paid = row["amount_paid"] + amount
        new_status = _compute_receipt_status(row["total"], new_paid, None)
        cur = con.execute(
            """UPDATE receipts SET amount_paid=?, status=?, work_status=?, payment_method=?, transfer_sender=?,
               transfer_account=? WHERE id=? AND status='pending' AND amount_paid=?""",
            (new_paid, new_status, row["work_status"], method,
             transfer_sender, transfer_account, receipt_id, row["amount_paid"]),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah (ada pembayaran lain). Muat ulang lalu coba lagi.")
        _insert_receipt_payment(con, receipt_id, amount, method, note, created_by, transfer_sender, transfer_account)
        _remember_transfer_name(con, "sender", transfer_sender)
        _remember_transfer_name(con, "account", transfer_account)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


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
    transfer_sender: str | None = None,
    transfer_account: str | None = None,
    created_by: str | None = None,
    work_status: str = "belum",
    extra: dict | None = None,
    stock_override_reason: str | None = None,
    payment_confirmed: bool | None = None,
    price_actor: dict | None = None,
    discount_reason: str | None = None,
    allow_discount_below_cost: bool = False,
    allow_discount_stack: bool = False,
) -> dict:
    plate_full = " ".join(x for x in (plate_region, plate_number, plate_suffix) if x)  # '' for a walk-in nota
    if payment_method != "transfer":
        transfer_sender = transfer_account = None
    # K-18: an unconfirmed transfer/QRIS is Pending -- recorded, but not counted as paid.
    pending_amount = amount_paid if (amount_paid > 0 and _pay_pending(payment_method, payment_confirmed)
                                     and status != "void" and work_status != "draft") else 0
    amount_paid -= pending_amount
    subtotal = sum(_line_total(item) for item in items)
    total = subtotal - discount
    if work_status == "draft" and status != "void":
        # A draft takes no money and has no balance due yet.
        final_status = "pending"
    else:
        final_status = _compute_receipt_status(total, amount_paid, status)
    extra = extra or {}
    created_at = _now()
    started_at = _sanitize_started_at(started_at, created_at)

    with _conn() as con:
        customer_id, customer_phone, customer_name = _resolve_receipt_customer(
            con, customer_id, customer_phone, customer_name
        )
        receipt_no = _next_receipt_no(con, created_at)
        con.execute(
            """INSERT INTO receipts
               (id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, status, amount_paid, payment_method, customer_id,
                receipt_no, transfer_sender, transfer_account, work_status, work_notes, cashier_name,
                estimated_finish_at, vehicle_make, vehicle_model, vehicle_color, vehicle_year, draft_saved_at, remarks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, final_status, amount_paid, payment_method,
                customer_id, receipt_no, transfer_sender, transfer_account, work_status,
                extra.get("work_notes"), extra.get("cashier_name"), extra.get("estimated_finish_at"),
                extra.get("vehicle_make"), extra.get("vehicle_model"), extra.get("vehicle_color"),
                extra.get("vehicle_year"), created_at if work_status == "draft" else None, extra.get("remarks"),
            ),
        )
        _link_receipt_vehicle(con, receipt_id, created_by)
        _remember_transfer_name(con, "sender", transfer_sender)
        _remember_transfer_name(con, "account", transfer_account)
        if amount_paid > 0 and final_status != "void":
            _insert_receipt_payment(con, receipt_id, amount_paid, payment_method, None, created_by,
                                    transfer_sender, transfer_account, created_at)
        if pending_amount > 0:
            _insert_receipt_payment(con, receipt_id, pending_amount, payment_method, "Menunggu konfirmasi dana masuk", created_by,
                                    transfer_sender, transfer_account, created_at, state="pending")
        resolved_items = [
            {**item, "master_item_id": _resolve_master_item_id(con, item.get("sku"))} for item in items
        ]
        for item in resolved_items:
            item["_price"] = _price_review(
                con, scope="receipt", doc_id=receipt_id, master_item_id=item["master_item_id"], product_name=item["product_name"],
                qty=item["quantity"], unit_price=item["unit_price"], line_discount=item.get("line_discount") or 0, prev=None,
                reason=item.get("price_reason"), allow_below_cost=bool(item.get("allow_below_cost")), actor=price_actor)
        disc = _discount_review(
            con, receipt_id=receipt_id, items=[{**i, "price_state": i["_price"]["price_state"]} for i in resolved_items], subtotal=subtotal,
            prev=None, new_discount=discount, reason=discount_reason, allow_below_cost=allow_discount_below_cost,
            allow_stack=allow_discount_stack, actor=price_actor)
        if (disc["discount_state"] == "pending" or any(i["_price"]["price_state"] == "pending" for i in resolved_items)) \
                and (amount_paid > 0 or pending_amount > 0):
            raise ValueError(_PRICE_PENDING_MSG)
        con.execute(f"UPDATE receipts SET {', '.join(c + '=?' for c in _DISC_COLS)} WHERE id=?", (*disc.values(), receipt_id))
        for item in resolved_items:
            item["_row_id"] = con.execute(
                """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id,
                                              item_type, line_discount, added_at, added_by, added_during_work, serial_no,
                                              list_price, price_state, price_reason, price_requested_by, price_requested_at,
                                              price_approved_by, price_approved_at, price_below_cost)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id, item["product_name"], item["quantity"], item["unit_price"], item.get("warranty_date"),
                    item["master_item_id"], item.get("item_type") or "barang", item.get("line_discount") or 0,
                    created_at, created_by, item.get("serial_no"), *[item["_price"][c] for c in _PRICE_COLS],
                ),
            ).lastrowid

        # A brand-new receipt has no prior holding; a draft or a receipt created straight
        # into 'void' (the only status a caller can set directly) holds nothing.
        after_holdings = (
            _receipt_item_holdings(resolved_items, con) if _holds_stock(final_status, work_status) else {}
        )
        _apply_receipt_stock_delta(con, receipt_id, {}, after_holdings, walkin=({}, after_holdings),
                                   override_reason=stock_override_reason, by=created_by)
        walk_in = not plate_full and not any((i.get("_assign") or {}).get("technician_ids") or i.get("item_type") == "jasa" for i in resolved_items)
        _log_receipt_event(con, receipt_id, "created", "Draft dibuat" if work_status == "draft" else "Nota dibuat (pembelian langsung)" if walk_in else {
            "pengerjaan": "Nota dibuat, pengerjaan dimulai",
            "belum": "Nota dibuat, belum dikerjakan", "selesai": "Nota dibuat, pengerjaan selesai",
        }.get(work_status, "Nota dibuat"), created_by)
        # The lines typed before the first save are listed too, so Riwayat Item shows them.
        if resolved_items:
            _log_receipt_event(con, receipt_id, "item_added",
                               "Item ditambahkan: " + ", ".join(i["product_name"] for i in resolved_items), created_by)
        if work_status == "selesai":
            _incentives().finish_assigned_items(con, receipt_id, created_by)
        _incentives().sync_receipt(con, receipt_id, created_by, _assignments(extra, resolved_items))

        customer_full_name = con.execute(
            _RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)
        ).fetchone()["customer_full_name"]

    return {
        "subtotal": subtotal, "discount": discount, "total": total, "status": final_status,
        "amount_paid": amount_paid, "customer_phone": customer_phone, "customer_name": customer_name,
        "payment_method": payment_method, "customer_id": customer_id, "customer_full_name": customer_full_name,
        "receipt_no": receipt_no, "transfer_sender": transfer_sender, "transfer_account": transfer_account,
        "work_status": work_status,
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


# Payment progress of a receipt, derived from amount_paid vs total. A void receipt
# takes no payment state at all, so it matches none of them. Keys are fixed here, so
# the SQL below never carries request text.
RECEIPT_PAYMENT_CLAUSES = {
    "belum": "(r.status != 'void' AND r.work_status != 'draft' AND r.amount_paid <= 0)",
    "sebagian": "(r.status != 'void' AND r.work_status != 'draft' AND r.amount_paid > 0 AND r.amount_paid < r.total)",
    "lunas": "(r.status != 'void' AND r.amount_paid >= r.total)",
}

# ORDER BY fragments for the receipt list; rowid keeps ties stable.
RECEIPT_SORTS = {
    "newest": "r.created_at DESC, r.rowid DESC",
    "oldest": "r.created_at ASC, r.rowid ASC",
    "total_desc": "r.total DESC, r.created_at DESC, r.rowid DESC",
    "total_asc": "r.total ASC, r.created_at DESC, r.rowid DESC",
}


def _receipt_filters(
    plate_query: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    customer: str | None,
    q: str | None,
    tz_offset_minutes: int | None,
    payment: str | None = None,
    work: str | None = None,
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
        nota_no = ("r.receipt_no LIKE ?" + _LIKE_ESCAPE, [_like_contains(q.strip())]) if "/" in q else None
        either = [c for c in (_plate_clause(q), _customer_clause(q), nota_no) if c]
        if either:
            clauses.append("(" + " OR ".join(c[0] for c in either) + ")")
            for c in either:
                params += c[1]
    if status:
        clauses.append("r.status=?")
        params.append(status)
    if payment:
        clauses.append(RECEIPT_PAYMENT_CLAUSES[payment])
    if work:
        # A void nota has no work stage left to speak of.
        clauses.append("(r.status != 'void' AND r.work_status=?)")
        params.append(work)

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
    payment: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    work: str | None = None,
) -> list[dict]:
    where_sql, params = _receipt_filters(
        plate_query, status, date_from, date_to, customer, q, tz_offset_minutes, payment, work
    )
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ? OFFSET ?"
        params = params + [limit, max(offset, 0)]

    with _conn() as con:
        # rowid breaks created_at ties so the newest-first order is stable.
        rows = con.execute(
            f"""SELECT r.*,
                   CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_full_name,
                   (SELECT COUNT(*) FROM receipt_items ri WHERE ri.receipt_id = r.id) AS item_count,
                   (SELECT p.po_number FROM preorders p WHERE p.id = r.preorder_id) AS po_number,
                   (SELECT COUNT(*) FROM receipts x WHERE x.preorder_id = r.preorder_id AND x.status <> 'void') AS invoice_count,
                   (SELECT COALESCE(SUM(p.amount),0) FROM receipt_payments p WHERE p.receipt_id = r.id AND p.state = 'pending') AS pending_amount,
                   (SELECT s.service_no FROM service_orders s WHERE s.id = r.service_order_id) AS service_no
               FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id
               {where_sql} ORDER BY {RECEIPT_SORTS[sort]}{limit_sql}""",
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
    payment: str | None = None,
    work: str | None = None,
) -> int:
    """How many receipts the same filters match before any limit, so a client
    can tell 'these are all of them' from 'these are the newest N'."""
    where_sql, params = _receipt_filters(
        plate_query, status, date_from, date_to, customer, q, tz_offset_minutes, payment, work
    )
    with _conn() as con:
        return con.execute(
            f"SELECT COUNT(*) AS n FROM receipts r LEFT JOIN customers c ON c.id = r.customer_id {where_sql}",
            params,
        ).fetchone()["n"]


def get_receipt_summary(tz_offset_minutes: int | None = None) -> dict:
    """Header tiles for Riwayat Invoice. "Today" is the shop's calendar day for the
    given offset, the same midnight-to-midnight the date filter uses."""
    offset = SHOP_UTC_OFFSET_MINUTES if tz_offset_minutes is None else tz_offset_minutes
    today = (datetime.now(timezone.utc) + timedelta(minutes=offset)).date()
    start = _shop_midnight_utc_iso(today, offset)
    end = _shop_midnight_utc_iso(today + timedelta(days=1), offset)
    with _conn() as con:
        row = con.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? AND status != 'void' THEN 1 END), 0) AS today_count,
                 COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? AND status = 'done' THEN 1 END), 0) AS done_today,
                 COALESCE(SUM(CASE WHEN status = 'pending' AND work_status != 'draft' THEN 1 END), 0) AS pending_count,
                 COALESCE(SUM(CASE WHEN status = 'pending' AND work_status != 'draft' THEN MAX(total - amount_paid, 0) END), 0) AS pending_outstanding,
                 COALESCE(SUM(CASE WHEN status != 'void' AND work_status = 'draft' THEN 1 END), 0) AS draft_count,
                 COALESCE(SUM(CASE WHEN status != 'void' AND work_status = 'pengerjaan' THEN 1 END), 0) AS pengerjaan_count,
                 COALESCE(SUM(CASE WHEN status != 'void' AND work_status = 'menunggu' THEN 1 END), 0) AS menunggu_count,
                 COALESCE(SUM(CASE WHEN status != 'void' AND work_status = 'belum' THEN 1 END), 0) AS belum_count,
                 COALESCE(SUM(CASE WHEN status != 'void' AND work_status = 'selesai' THEN 1 END), 0) AS selesai_count,
                 COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? AND status = 'void' THEN 1 END), 0) AS void_today
               FROM receipts""",
            (start, end, start, end, start, end),
        ).fetchone()
        return dict(row)


_RECEIPT_EXTRA_FIELDS = (
    "work_notes", "cashier_name", "estimated_finish_at", "vehicle_make", "vehicle_model", "vehicle_color",
    "vehicle_year", "remarks",
)


def _incentives():
    from backend import incentives
    return incentives


def _assignments(extra: dict | None, items: list[dict] | None) -> dict:
    """Who sells and who installs, as sent by the Kasir: the nota's sales utama (when sent) and, per line that was
    written in this save, the fields the client included. Only what was sent is changed."""
    out: dict = {}
    if extra and "sales_employee_id" in extra:
        out["sales_employee_id"] = extra["sales_employee_id"]
    per = {}
    for item in items or []:
        sent = item.get("_assign") or {}
        if sent and item.get("_row_id"):
            per[item["_row_id"]] = sent
    if per:
        out["items"] = per
    return out


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
    transfer_sender: str | None = None,
    update_transfer_sender: bool = False,
    transfer_account: str | None = None,
    update_transfer_account: bool = False,
    changed_by: str | None = None,
    extra: dict | None = None,
    void_reason: str | None = None,
    stock_override_reason: str | None = None,
    payment_confirmed: bool | None = None,
    refund_method: str | None = None,
    refund_proof: str | None = None,
    price_actor: dict | None = None,
    discount_reason: str | None = None,
    allow_discount_below_cost: bool = False,
    allow_discount_stack: bool = False,
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Nota {receipt_id} tidak ditemukan.")
        existing = dict(row)
        extra = extra or {}
        is_draft = existing["work_status"] == "draft"
        if status == "void" and existing["status"] != "void" and existing.get("return_total") is not None \
                and con.execute("SELECT 1 FROM receipt_returns WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Nota ini sudah ada retur barang, jadi tidak bisa di-void. Retur sisa barangnya bila perlu.")
        if status == "void" and existing["status"] != "void" and existing.get("preorder_id") and \
                len(_po_invoices(con, existing["preorder_id"])) > 1:
            raise ValueError("Invoice ini satu dari beberapa invoice Sales Order. Pakai “Gabung Kembali” (invoice lanjutan) "
                             "atau Pembatalan Sales Order; void langsung bisa membuat barang dan uang tidak cocok.")
        # K-20: voiding a nota that holds money is a full refund, and the refund must say how and prove it.
        void_refund = 0
        if status == "void" and existing["status"] != "void" and (existing["amount_paid"] or 0) > 0:
            void_refund = existing["amount_paid"]
            if refund_method not in ("cash", "transfer", "qris", "debit"):
                raise ValueError("Nota ini sudah dibayar. Void berarti refund penuh: pilih metode pengembalian uang.")
            refund_proof = _refund_proof(refund_proof)

        new_plate_region = plate_region if plate_region is not None else existing["plate_region"]
        new_plate_number = plate_number if plate_number is not None else existing["plate_number"]
        new_plate_suffix = plate_suffix if plate_suffix is not None else existing["plate_suffix"]
        new_plate_full = " ".join(x for x in (new_plate_region, new_plate_number, new_plate_suffix) if x)

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
        # K-18: extra money by transfer/QRIS that nobody has confirmed yet is recorded as Pending, not paid.
        pending_delta = 0
        if amount_paid is not None and status != "void" and existing["status"] != "void" \
                and amount_paid > existing["amount_paid"] and _pay_pending(new_payment_method, payment_confirmed):
            pending_delta = amount_paid - existing["amount_paid"]
            new_amount_paid = existing["amount_paid"]
        new_transfer_sender = transfer_sender if update_transfer_sender else existing.get("transfer_sender")
        new_transfer_account = transfer_account if update_transfer_account else existing.get("transfer_account")
        if new_payment_method != "transfer":
            new_transfer_sender = new_transfer_account = None
        _remember_transfer_name(con, "sender", new_transfer_sender)
        _remember_transfer_name(con, "account", new_transfer_account)

        # Stock: snapshot what this receipt currently holds, before anything below
        # changes — a void receipt holds nothing regardless of what its old line
        # items say, so this stays a plain [] rather than a query in that case.
        existing_items = [dict(r) for r in con.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
        ).fetchall()]
        before_holdings = (
            _receipt_item_holdings(existing_items, con)
            if _holds_stock(existing["status"], existing["work_status"]) else {}
        )

        if items is not None:
            resolved_items = [
                {**item, "master_item_id": _resolve_master_item_id(con, item.get("sku"))} for item in items
            ]
            # Lines the client echoes back by id keep who added them and when; anything else is
            # new. A line added once the car is already being worked on is flagged as such.
            old_by_id = {r["id"]: r for r in existing_items}
            # Lines that come from a Sales Order belong to the PO: they are changed (or removed) there,
            # and the invoice follows. Here they must come back untouched apart from discount.
            sent_by_id = {item.get("id"): item for item in resolved_items if item.get("id") in old_by_id}
            for old_row in existing_items:
                if not old_row.get("preorder_item_id"):
                    continue
                sent = sent_by_id.get(old_row["id"])
                if sent is None or sent["product_name"] != old_row["product_name"] or sent["quantity"] != old_row["quantity"] \
                        or sent["unit_price"] != old_row["unit_price"]:
                    raise ValueError(f"Barang {old_row['product_name']} berasal dari Sales Order. Ubah atau batalkan di halaman Sales Order.")
                sent["master_item_id"] = old_row["master_item_id"]
            for old_row in existing_items:
                if not old_row.get("returned_qty"):
                    continue
                sent = sent_by_id.get(old_row["id"])
                if sent is None or sent["quantity"] != old_row["quantity"] or sent["unit_price"] != old_row["unit_price"] \
                        or (sent.get("line_discount") or 0) != (old_row["line_discount"] or 0):
                    raise ValueError(f"Barang {old_row['product_name']} sudah pernah diretur, jadi baris itu tidak bisa diubah atau dihapus.")
                sent["master_item_id"] = old_row["master_item_id"]
            now_iso = _now()
            during_work = 0 if is_draft else 1
            con.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
            added_names: list[str] = []
            changed_notes: list[str] = []
            kept_ids = {item.get("id") for item in resolved_items if item.get("id") in old_by_id}
            for old_id, old_row in old_by_id.items():
                if old_id not in kept_ids:
                    changed_notes.append(f"{old_row['product_name']} dihapus")
            for item in resolved_items:
                old = old_by_id.get(item.get("id"))
                item["_price"] = _price_review(
                    con, scope="receipt", doc_id=receipt_id, master_item_id=item["master_item_id"], product_name=item["product_name"],
                    qty=item["quantity"], unit_price=item["unit_price"], line_discount=item.get("line_discount") or 0, prev=old,
                    reason=item.get("price_reason"), allow_below_cost=bool(item.get("allow_below_cost")), actor=price_actor)
                if old is not None:
                    bits = []
                    if old["product_name"] != item["product_name"]:
                        bits.append(f"nama {old['product_name']} -> {item['product_name']}")
                    if old["quantity"] != item["quantity"]:
                        bits.append(f"qty {old['quantity']} -> {item['quantity']}")
                    if old["unit_price"] != item["unit_price"]:
                        bits.append(f"harga {old['unit_price']:,} -> {item['unit_price']:,}".replace(",", "."))
                    if (old["line_discount"] or 0) != (item.get("line_discount") or 0):
                        bits.append(f"diskon {(old['line_discount'] or 0):,} -> {(item.get('line_discount') or 0):,}".replace(",", "."))
                    if bits:
                        changed_notes.append(f"{item['product_name']}: " + ", ".join(bits))
                    item["_row_id"] = old["id"]
                    con.execute(
                        """INSERT INTO receipt_items (id, receipt_id, product_name, quantity, unit_price, warranty_date,
                               master_item_id, item_type, line_discount, added_at, added_by, added_during_work, preorder_item_id,
                               returned_qty, returned_stock_qty, serial_no,
                               list_price, price_state, price_reason, price_requested_by, price_requested_at,
                               price_approved_by, price_approved_at, price_below_cost)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (old["id"], receipt_id, item["product_name"], item["quantity"], item["unit_price"],
                         item.get("warranty_date"), item["master_item_id"], item.get("item_type") or "barang",
                         item.get("line_discount") or 0, old["added_at"], old["added_by"], old["added_during_work"],
                         old.get("preorder_item_id"), old.get("returned_qty") or 0, old.get("returned_stock_qty") or 0,
                         item.get("serial_no") if "serial_no" in item else old.get("serial_no"),
                         *[item["_price"][c] for c in _PRICE_COLS]),
                    )
                    if item["_price"]["price_state"] and item["_price"] != {c: old.get(c) for c in _PRICE_COLS}:
                        changed_notes.append(f"{item['product_name']}: harga khusus "
                                             + ("DISETUJUI Admin" if item["_price"]["price_state"] == "approved" else "diajukan, menunggu persetujuan Admin"))
                else:
                    item["_row_id"] = con.execute(
                        """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date,
                               master_item_id, item_type, line_discount, added_at, added_by, added_during_work, serial_no,
                               list_price, price_state, price_reason, price_requested_by, price_requested_at,
                               price_approved_by, price_approved_at, price_below_cost)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (receipt_id, item["product_name"], item["quantity"], item["unit_price"],
                         item.get("warranty_date"), item["master_item_id"], item.get("item_type") or "barang",
                         item.get("line_discount") or 0, now_iso, changed_by, during_work, item.get("serial_no"),
                         *[item["_price"][c] for c in _PRICE_COLS]),
                    ).lastrowid
                    added_names.append(item["product_name"])
            # Logged on drafts too: the owner reads Riwayat Item to see who added which line.
            if added_names:
                _log_receipt_event(con, receipt_id, "item_added", "Item ditambahkan: " + ", ".join(added_names), changed_by)
            if changed_notes and not is_draft:
                _log_receipt_event(con, receipt_id, "item_changed", "Item diubah: " + "; ".join(changed_notes), changed_by)
            effective_items = [
                dict(r) for r in con.execute(
                    "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
                ).fetchall()
            ]

        elif discount is not None:
            effective_items = existing_items
        else:
            effective_items = None

        if effective_items is not None:
            new_subtotal = sum(_line_total(item) for item in effective_items)
            new_discount = discount if discount is not None else existing["discount"]
            new_total = new_subtotal - new_discount - (existing.get("return_total") or 0)
        else:
            new_subtotal = existing["subtotal"]
            new_discount = existing["discount"]
            new_total = existing["total"]

        if discount is not None and discount != existing["discount"] and status != "void" and existing["status"] != "void":
            disc = _discount_review(
                con, receipt_id=receipt_id, items=effective_items or [], subtotal=new_subtotal, prev=existing, new_discount=discount,
                reason=discount_reason, allow_below_cost=allow_discount_below_cost, allow_stack=allow_discount_stack, actor=price_actor)
            con.execute(f"UPDATE receipts SET {', '.join(c + '=?' for c in _DISC_COLS)} WHERE id=?", (*disc.values(), receipt_id))
        if discount is not None and discount != existing["discount"] and not is_draft and status != "void":
            _log_receipt_event(
                con, receipt_id, "item_changed",
                f"Diskon nota Rp {existing['discount']:,} -> Rp {discount:,}".replace(",", "."), changed_by,
            )
        # Void is sticky and has no unvoid path — once a receipt is voided, only
        # a fresh explicit status="void" request is honored (a no-op); editing
        # amount_paid or items afterward must not accidentally revive it to a
        # computed pending/done.
        if existing["status"] == "void" and status != "void":
            new_status = "void"
        elif is_draft and status != "void":
            new_status = "pending"
        else:
            new_status = _compute_receipt_status(new_total, new_amount_paid, status)
        # K-22: a price still waiting for an admin cannot take money or make the nota Lunas.
        if new_status != "void" and _receipt_price_pending(con, receipt_id):
            if (amount_paid is not None and amount_paid > existing["amount_paid"]) or pending_delta:
                raise ValueError(_PRICE_PENDING_MSG)
            if new_status == "done" and existing["status"] != "done":
                new_status = "pending"
        new_work_status = existing["work_status"]
        # A total that drops below what has already been paid (an item cancelled after payment) is
        # allowed: the difference is Kelebihan Bayar, kept on the record until an admin refunds it
        # (refund_receipt_overpay). Nothing is refunded or deleted automatically.
        if amount_paid is None and existing["status"] != "void" and status != "void" and new_total < new_amount_paid:
            over = new_amount_paid - new_total
            _log_receipt_event(
                con, receipt_id, "overpaid",
                f"Total nota turun menjadi Rp {new_total:,}; kelebihan bayar Rp {over:,} perlu dikembalikan ke pelanggan.".replace(",", "."),
                changed_by,
            )
        # An edit that states a different paid amount is recorded as an adjustment row, so the
        # payment history keeps adding up to amount_paid.
        if pending_delta and new_status != "void":
            _insert_receipt_payment(
                con, receipt_id, pending_delta, new_payment_method, "Menunggu konfirmasi dana masuk", changed_by,
                new_transfer_sender, new_transfer_account, state="pending",
            )
            _log_receipt_event(con, receipt_id, "payment",
                               f"Pembayaran Rp {pending_delta:,} ({new_payment_method}) dicatat PENDING.".replace(",", "."), changed_by)
        elif amount_paid is not None and amount_paid != existing["amount_paid"] and new_status != "void":
            _insert_receipt_payment(
                con, receipt_id, amount_paid - existing["amount_paid"], new_payment_method,
                "Penyesuaian saat nota diubah", changed_by, new_transfer_sender, new_transfer_account,
            )
        if new_status == "void" and existing["status"] != "void" and void_refund:
            new_amount_paid = 0  # the money has gone back to the customer; see the refund row below

        con.execute(
            """UPDATE receipts SET plate_region=?, plate_number=?, plate_suffix=?, plate_full=?,
               customer_phone=?, customer_name=?, customer_id=?, subtotal=?, discount=?, total=?, status=?,
               amount_paid=?, payment_method=?, transfer_sender=?, transfer_account=?, work_status=?,
               work_notes=?, cashier_name=?, estimated_finish_at=?, vehicle_make=?, vehicle_model=?,
               vehicle_color=?, vehicle_year=?, remarks=?, draft_saved_at=? WHERE id=?""",
            (
                new_plate_region, new_plate_number, new_plate_suffix, new_plate_full,
                new_customer_phone, new_customer_name, new_customer_id,
                new_subtotal, new_discount, new_total, new_status, new_amount_paid, new_payment_method,
                new_transfer_sender, new_transfer_account, new_work_status,
                *(extra.get(k, existing[k]) for k in _RECEIPT_EXTRA_FIELDS),
                _now() if is_draft else existing["draft_saved_at"], receipt_id,
            ),
        )
        _link_receipt_vehicle(con, receipt_id, changed_by)

        # Stock: holding after this change is nothing if the receipt ends up void
        # (covers a fresh void here, and keeps a receipt that was already void
        # from re-applying anything even if its items/discount were also sent in
        # the same request); otherwise it's whatever items are in effect now —
        # the ones just written, or the untouched existing ones.
        after_items = effective_items if effective_items is not None else existing_items
        after_holdings = (
            _receipt_item_holdings(after_items, con) if _holds_stock(new_status, new_work_status) else {}
        )
        held_before = _holds_stock(existing["status"], existing["work_status"])
        held_after = _holds_stock(new_status, new_work_status)
        walkin = (_walkin_holdings(existing_items, con) if held_before else {},
                  _walkin_holdings(after_items, con) if held_after else {})
        _apply_receipt_stock_delta(con, receipt_id, before_holdings, after_holdings, walkin=walkin,
                                   override_reason=stock_override_reason, by=changed_by)
        if new_status == "void" and existing["status"] != "void":
            reason = (void_reason or "").strip() or None
            refund = void_refund
            note = "Nota dibatalkan" + (f": {reason}" if reason else "")
            if refund > 0:
                _insert_receipt_payment(con, receipt_id, -refund, refund_method, "Refund void nota", changed_by,
                                        kind="refund", proof_ref=refund_proof)
                note += f". Refund penuh Rp {refund:,} via {refund_method} (bukti: {refund_proof}).".replace(",", ".")
            _fail_pending_payments(con, receipt_id, "Nota di-void", changed_by)
            con.execute(
                "UPDATE receipts SET void_reason=?, voided_by=?, voided_at=? WHERE id=?",
                (reason, changed_by, _now(), receipt_id),
            )
            _log_receipt_event(con, receipt_id, "void", note, changed_by)

        _incentives().sync_receipt(con, receipt_id, changed_by,
                                   _assignments(extra, resolved_items if items is not None else None))
        updated = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(updated)


def settle_receipt(
    receipt_id: str, payment_method: str | None = None, amount: int | None = None, settled_by: str | None = None,
    confirmed: bool | None = None,
) -> dict:
    """Mark a pending receipt paid in full ("Lunasi"): the remaining balance is recorded as
    paid (amount_paid = total), which makes the computed status 'done'. K-07: payment and work are
    two separate axes, so this touches only the payment columns -- items, totals, stock and the work
    status (Pengerjaan) are exactly as they were; the nota only closes once it is Lunas AND Selesai.
    A void receipt stays void and an already-done one has nothing left to settle,
    so both are refused rather than silently re-saved. `amount`, when given, must be
    exactly the remaining balance -- the receipt is never closed on a short or over payment."""
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Struk tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Struk sudah dibatalkan (void), jadi tidak bisa ditandai selesai.")
        if row["status"] == "done":
            raise ValueError("Struk ini sudah selesai (lunas), tidak ada sisa pembayaran.")
        if row["work_status"] == "draft":
            raise ValueError("Nota masih Draft. Simpan sebagai Belum Dikerjakan atau mulai pengerjaan dulu.")
        if _receipt_price_pending(con, receipt_id):
            raise ValueError(_PRICE_PENDING_MSG)
        remaining = max(row["total"] - row["amount_paid"], 0)
        if amount is not None and amount != remaining:
            raise ValueError(f"Jumlah dibayar harus sama dengan sisa pembayaran (Rp {remaining:,}).".replace(",", "."))
        if _pending_sum(con, receipt_id):
            raise ValueError("Ada pembayaran yang menunggu konfirmasi. Konfirmasi atau tandai gagal dulu sebelum melunasi.")
        method_used = payment_method if payment_method is not None else row["payment_method"]
        if remaining > 0 and _pay_pending(method_used, confirmed):
            _insert_receipt_payment(con, receipt_id, remaining, method_used, "Pelunasan, menunggu konfirmasi dana masuk", settled_by,
                                    row["transfer_sender"], row["transfer_account"], state="pending")
            _log_receipt_event(con, receipt_id, "payment", f"Pelunasan Rp {remaining:,} ({method_used}) dicatat PENDING.".replace(",", "."), settled_by)
            return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())
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
        if remaining > 0:
            _insert_receipt_payment(
                con, receipt_id, remaining, payment_method if payment_method is not None else row["payment_method"],
                "Pelunasan", settled_by, row["transfer_sender"], row["transfer_account"],
            )
        updated = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(updated)


def refund_receipt_overpay(
    receipt_id: str, amount: int, method: str, reason: str, by: str | None = None, proof_ref: str | None = None
) -> dict:
    """Kembalikan Kelebihan Bayar (admin): writes a negative payment row for money handed back, never
    more than the nota is overpaid. Messages: 'tidak ditemukan' -> 404, 'Jumlah'/'Alasan' -> 422, else 409."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan refund wajib diisi (minimal 3 karakter).")
    proof_ref = _refund_proof(proof_ref)
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        over = max(row["amount_paid"] - row["total"], 0)
        if over <= 0:
            raise ValueError("Nota ini tidak punya kelebihan bayar.")
        if amount <= 0:
            raise ValueError("Jumlah refund harus lebih dari 0.")
        if amount > over:
            raise ValueError(f"Jumlah refund melebihi kelebihan bayar (Rp {over:,}).".replace(",", "."))
        cur = con.execute(
            "UPDATE receipts SET amount_paid=? WHERE id=? AND amount_paid=?",
            (row["amount_paid"] - amount, receipt_id, row["amount_paid"]),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        _insert_receipt_payment(con, receipt_id, -amount, method, f"Refund kelebihan bayar: {reason}", by,
                                kind="refund", proof_ref=proof_ref)
        _log_receipt_event(con, receipt_id, "refund",
                           f"Kelebihan bayar Rp {amount:,} ({method}) dikembalikan ke pelanggan: {reason}".replace(",", "."), by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def reopen_closed_receipt(receipt_id: str, reason: str, by: str | None = None) -> dict:
    """Buka Kembali (admin): a closed nota (Lunas + Pengerjaan Selesai) goes back to Dalam Pengerjaan so it
    can be revised. The reason, who and when stay in the nota's event log."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan wajib diisi (minimal 3 karakter).")
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if not _is_closed(row):
            raise ValueError("Nota ini belum ditutup (Lunas dan Pengerjaan Selesai).")
        cur = con.execute(
            "UPDATE receipts SET work_status='pengerjaan', work_stage=COALESCE(work_stage, 'persiapan') "
            "WHERE id=? AND status='done' AND work_status='selesai'",
            (receipt_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        _clear_handover(con, receipt_id, "nota dibuka kembali", by)
        _log_receipt_event(con, receipt_id, "reopen", f"Nota ditutup dibuka kembali untuk revisi: {reason}", by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


RETURN_DESTINATIONS = {"stok": "kembali ke stok", "supplier": "dikembalikan ke supplier", "rusak": "rusak / dibuang"}
RETURN_REASONS = {
    "salah_input": "Salah input kasir", "pelanggan_batal": "Pelanggan batal", "tidak_cocok": "Barang tidak cocok",
    "cacat": "Barang cacat / rusak", "salah_kirim": "Salah kirim / salah barang", "lainnya": "Lainnya",
}


def return_receipt_item(
    receipt_id: str, item_id: int, quantity: int, destination: str | None, refund_amount: int,
    method: str, reason: str, by: str | None = None, *, kind: str = "retur", reason_code: str | None = None,
    restock_qty: int | None = None, supplier_qty: int | None = None, damaged_qty: int | None = None,
    proof_ref: str | None = None, owner_ok: bool = False,
) -> dict:
    """Retur Barang (admin): the customer brings back `quantity` of one barang line, also on a closed nota.
    The line keeps its quantity and is marked returned; the nota's total drops by `refund_amount` (at most
    what those units were sold for -- anything less is kept by the shop as a fee). Whatever is then paid
    beyond the new total is handed back at once as a negative payment, so sales drop on the return date.
    Only destination 'stok' puts the goods back on the shelf. Messages: 'tidak ditemukan' -> 404,
    'Jumlah'/'Alasan'/'Tujuan' -> 422, else 409."""
    reason = (reason or "").strip()
    if kind not in ("retur", "void_item"):
        raise ValueError("Jenis pembatalan tidak valid.")
    if reason_code is not None and reason_code not in RETURN_REASONS:
        raise ValueError("Alasan tidak valid.")
    if kind == "void_item":
        # A mistaken line: the goods never left, so everything goes back on the shelf at full value.
        reason_code = reason_code or "salah_input"
    if reason_code is None and len(reason) < 3:
        raise ValueError("Alasan retur wajib diisi (minimal 3 karakter).")
    if reason_code == "lainnya" and len(reason) < 3:
        raise ValueError("Alasan: isi catatan untuk alasan Lainnya (minimal 3 karakter).")
    reason_text = RETURN_REASONS[reason_code] + (f": {reason}" if reason else "") if reason_code else reason
    if quantity <= 0:
        raise ValueError("Jumlah retur harus lebih dari 0.")
    # Where the returned units go. Either one `destination` for all of them, or a split that adds up.
    if restock_qty is None and supplier_qty is None and damaged_qty is None:
        if kind == "void_item":
            destination = "stok"
        if destination not in RETURN_DESTINATIONS:
            raise ValueError("Tujuan barang retur tidak valid.")
        restock_qty, supplier_qty, damaged_qty = (
            quantity if destination == "stok" else 0, quantity if destination == "supplier" else 0,
            quantity if destination == "rusak" else 0,
        )
    else:
        restock_qty, supplier_qty, damaged_qty = restock_qty or 0, supplier_qty or 0, damaged_qty or 0
        if min(restock_qty, supplier_qty, damaged_qty) < 0 or restock_qty + supplier_qty + damaged_qty != quantity:
            raise ValueError(f"Tujuan: jumlah masuk stok, ke supplier, dan rusak harus berjumlah {quantity}.")
        destination = max((("stok", restock_qty), ("supplier", supplier_qty), ("rusak", damaged_qty)), key=lambda x: x[1])[0]
    with _conn() as con:
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if rec is None:
            raise ValueError("Nota tidak ditemukan.")
        rec = dict(rec)
        if rec["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        if rec["work_status"] in ("draft",):
            raise ValueError("Nota masih Draft, barangnya belum diserahkan.")
        item = con.execute("SELECT * FROM receipt_items WHERE id=? AND receipt_id=?", (item_id, receipt_id)).fetchone()
        if item is None:
            raise ValueError("Barang tidak ditemukan di nota ini.")
        item = dict(item)
        if item["item_type"] == "jasa":
            raise ValueError("Jasa tidak bisa diretur. Gunakan potongan atau refund.")
        if item.get("preorder_item_id"):
            po_line = con.execute("SELECT status FROM preorder_items WHERE id=?", (item["preorder_item_id"],)).fetchone()
            if po_line is None or po_line["status"] not in PO_INSTALLED_STATUSES:
                raise ValueError("Barang Sales Order ini belum terpasang/diserahkan. Batalkan di halaman Sales Order saja.")
        left = item["quantity"] - (item["returned_qty"] or 0)
        if quantity <= 0 or quantity > left:
            raise ValueError(f"Jumlah retur harus 1 sampai {left}.")
        if kind == "retur":
            # GAR-BR-016: a unit is either refunded or replaced under warranty, never both without the Owner.
            claims = con.execute(
                """SELECT o.service_no, o.qty, o.stage,
                          EXISTS(SELECT 1 FROM service_replacements x WHERE x.order_id=o.id AND x.status='diterima') AS replaced
                   FROM service_orders o WHERE o.receipt_item_id=? AND o.service_no IS NOT NULL AND o.stage<>'dibatalkan'""",
                (item_id,)).fetchall()
            busy = [c for c in claims if c["stage"] in SVC_OPEN and not c["replaced"]]
            busy_qty = sum(c["qty"] for c in busy)
            if busy and quantity > left - busy_qty:
                raise ValueError(f"{busy_qty} unit di baris ini sedang dalam service / klaim garansi "
                                 f"({', '.join(c['service_no'] for c in busy)}). Selesaikan atau hentikan servicenya dulu.")
            replaced = [c["service_no"] for c in claims if c["replaced"]]
            if replaced and not owner_ok:
                raise ValueError(f"Barang ini sudah pernah diganti garansi ({', '.join(replaced)}). "
                                 "Retur / refund unit yang sudah diganti hanya boleh disetujui Owner.")
            if replaced:
                reason_text += f" [Owner: unit sudah diganti garansi {', '.join(replaced)}]"
        # What those units were sold for, line discount spread over the line.
        value = _line_total(item) * quantity // item["quantity"]
        if kind == "void_item":
            refund_amount = value
        if refund_amount < 0 or refund_amount > value:
            raise ValueError(f"Jumlah refund harus 0 sampai Rp {value:,}.".replace(",", "."))

        before = _receipt_item_holdings([item], con)
        to_stock = restock_qty
        con.execute(
            "UPDATE receipt_items SET returned_qty=returned_qty+?, returned_stock_qty=returned_stock_qty+? WHERE id=?",
            (quantity, to_stock, item_id),
        )
        item["returned_qty"] = (item["returned_qty"] or 0) + quantity
        item["returned_stock_qty"] = (item["returned_stock_qty"] or 0) + to_stock
        _apply_receipt_stock_delta(con, receipt_id, before, _receipt_item_holdings([item], con))

        new_total = rec["total"] - refund_amount
        cash_out = max(min(rec["amount_paid"] - new_total, refund_amount), 0)
        new_paid = rec["amount_paid"] - cash_out
        cur = con.execute(
            "UPDATE receipts SET total=?, return_total=return_total+?, amount_paid=?, status=? WHERE id=? AND total=? AND amount_paid=?",
            (new_total, refund_amount, new_paid, _compute_receipt_status(new_total, new_paid, None),
             receipt_id, rec["total"], rec["amount_paid"]),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        if cash_out > 0:
            _insert_receipt_payment(con, receipt_id, -cash_out, method, f"Refund retur {item['product_name']}: {reason}", by,
                                    kind="refund", proof_ref=_refund_proof(proof_ref))
        now = _now()
        return_id = con.execute(
            """INSERT INTO receipt_returns (receipt_id, receipt_item_id, product_name, master_item_id, quantity, item_value,
                   refund_amount, cash_out, method, destination, reason, returned_by, returned_at,
                   kind, reason_code, restock_qty, supplier_qty, damaged_qty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, item_id, item["product_name"], item["master_item_id"], quantity, value, refund_amount,
             cash_out, method if cash_out else None, destination, reason_text, by, now,
             kind, reason_code, restock_qty, supplier_qty, damaged_qty),
        ).lastrowid
        # Insentif follows the return (sales pro rata; teknisi kept once installed) -- once per return.
        _incentives().on_item_returned(con, receipt_id, item_id, return_id, quantity, by)
        # Units not put back on the shelf are still in the shop: they wait in Stok Karantina. Those meant
        # for the supplier become a supplier-return candidate (decision 10); nothing is sent automatically.
        if item["master_item_id"] is not None and (supplier_qty or damaged_qty):
            mi = con.execute("SELECT supplier_id, avg_cost_price, last_cost_price FROM master_items WHERE id=?",
                             (item["master_item_id"],)).fetchone()
            if mi is not None:
                cost = mi["avg_cost_price"] or mi["last_cost_price"] or 0
                for qty, cond, cand in ((supplier_qty, "perlu_cek", True), (damaged_qty, "rusak", False)):
                    _quarantine_in(
                        con, master_item_id=item["master_item_id"], product_name=item["product_name"], qty=qty,
                        condition=cond, source_type="retur_customer", source_ref=rec["receipt_no"], source_id=receipt_id,
                        supplier_id=mi["supplier_id"], unit_cost=cost, candidate=cand, note=reason_text, by=by)
        fmt = lambda n: f"Rp {n:,}".replace(",", ".")
        where = ", ".join(f"{n} {RETURN_DESTINATIONS[d]}" for d, n in
                          (("stok", restock_qty), ("supplier", supplier_qty), ("rusak", damaged_qty)) if n)
        label = "Void barang" if kind == "void_item" else "Retur"
        note = f"{label} {item['product_name']} x{quantity} ({where}): {reason_text}. Refund {fmt(refund_amount)}"
        if refund_amount < value:
            note += f", biaya retur {fmt(value - refund_amount)}"
        note += f"; uang dikembalikan {fmt(cash_out)} ({method})." if cash_out else "; mengurangi sisa tagihan."
        _log_receipt_event(con, receipt_id, "return", note, by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def get_receipt_returns(receipt_id: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM receipt_returns WHERE receipt_id=? ORDER BY returned_at ASC, id ASC", (receipt_id,)
        ).fetchall()]


def _install_so_lines(con: sqlite3.Connection, receipt_id: str, by: str | None) -> None:
    """K-01 + rule B: finishing the work on a Sales Order's invoice means its goods are installed /
    handed over. Every arrived line becomes Terpasang, which is the moment it leaves stock (using up
    its reservation). Refused while any line has not arrived yet."""
    po = con.execute(
        "SELECT p.id, p.po_number FROM preorders p JOIN receipts r ON r.preorder_id = p.id WHERE r.id=?", (receipt_id,)).fetchone()
    if po is None:
        return
    # K-19: only this invoice's own lines; a Sales Order split in several invoices finishes one invoice at a time.
    lines = [dict(r) for r in con.execute(
        "SELECT * FROM preorder_items WHERE preorder_id=? AND receipt_id=? ORDER BY position ASC, id ASC", (po["id"], receipt_id)).fetchall()]
    lines = [ln for ln in lines if ln["status"] != PO_LINE_CANCELLED]
    missing = [ln["product_name"] for ln in lines if ln["status"] not in PO_ARRIVED_STATUSES]
    if missing:
        raise ValueError("Masih ada barang Sales Order yang belum datang: " + ", ".join(missing)
                         + ". Pengerjaan belum bisa diselesaikan.")
    todo = [ln for ln in lines if ln["status"] not in PO_INSTALLED_STATUSES]
    if not todo:
        return
    before = _po_invoice_holdings(con, receipt_id)
    now = _now()
    for ln in todo:
        con.execute("UPDATE preorder_items SET status='terpasang' WHERE id=?", (ln["id"],))
        _log_preorder_status(con, po["id"], ln["id"], ln["product_name"], "terpasang", now, by, ln["status"])
    con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, po["id"]))
    # The invoice is being finished by its own Pengerjaan Selesai: its status is set by the caller, not by the sync.
    _so_work_follow.skip = receipt_id
    try:
        _po_invoice_sync(con, po["id"], by, {receipt_id: before})
    finally:
        _so_work_follow.skip = None


# Per request thread: the invoice whose Pengerjaan the Sales Order sync must leave alone (see _install_so_lines).
_so_work_follow = threading.local()


def change_receipt_work_status(
    receipt_id: str, to_status: str, note: str | None = None, changed_by: str | None = None,
    stock_override_reason: str | None = None,
) -> dict:
    """Moves a nota between Belum Dikerjakan / Dalam Pengerjaan / Pengerjaan Selesai, whatever its
    payment state. Leaving Draft is the moment stock is taken off the shelf (and an oversell is
    refused here, rolling the move back). A closed nota (Lunas + Pengerjaan Selesai) is read-only.
    Messages: 'tidak ditemukan' -> 404, 'Stok' -> 422, the rest -> 409."""
    if to_status not in RECEIPT_WORK_STATUSES or to_status in ("draft", "menunggu"):
        raise ValueError("Status tujuan tidak valid.")
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        if _is_closed(row):
            raise ValueError("Nota sudah lunas dan selesai (ditutup), tidak bisa diubah lagi.")
        current = row["work_status"]
        if to_status == current:
            raise ValueError("Nota sudah berstatus itu.")
        if to_status not in _WORK_TRANSITIONS.get(current, set()):
            raise ValueError("Perpindahan status itu tidak diizinkan.")
        # Owner, 22-10-2026: plate and phone may wait until the work on the vehicle really starts.
        if to_status in ("pengerjaan", "selesai") and not row["preorder_id"] and con.execute(
                "SELECT 1 FROM receipt_item_technicians WHERE receipt_id=? LIMIT 1", (receipt_id,)).fetchone():
            if not (row["plate_full"] or "").strip():
                raise ValueError("No. plat wajib diisi untuk mulai pengerjaan (ada barang yang dipasang teknisi). Klik Ubah, isi plat, lalu Simpan Perubahan.")
            if not (row["customer_phone"] or "").strip() and row["customer_id"] is None:
                raise ValueError("Nomor HP wajib diisi untuk mulai pengerjaan (ada barang yang dipasang teknisi). Klik Ubah, isi nomor HP, lalu Simpan Perubahan.")
        items = [dict(r) for r in con.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
        ).fetchall()]
        if current == "draft":
            if not items:
                raise ValueError("Nota belum punya item.")
            _apply_receipt_stock_delta(con, receipt_id, {}, _receipt_item_holdings(items, con),
                                       walkin=({}, _walkin_holdings(items, con)),
                                       override_reason=stock_override_reason, by=changed_by)
        if to_status == "selesai":
            _install_so_lines(con, receipt_id, changed_by)
            _incentives().finish_assigned_items(con, receipt_id, changed_by)
        # K-06: entering Dalam Pengerjaan starts at Persiapan; going back to Belum Dikerjakan forgets the stage.
        new_stage = row["work_stage"]
        if to_status == "pengerjaan" and not new_stage:
            new_stage = "persiapan"
        elif to_status == "belum":
            new_stage = None
        cur = con.execute(
            "UPDATE receipts SET work_status=?, work_stage=?, draft_saved_at=NULL WHERE id=? AND work_status=?",
            (to_status, new_stage, receipt_id, current),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        if current == "selesai":
            _clear_handover(con, receipt_id, "pengerjaan dibuka kembali", changed_by)
        labels = {"belum": "Belum Dikerjakan", "pengerjaan": "Dalam Pengerjaan", "selesai": "Pengerjaan Selesai"}
        text = f"Status: {labels[to_status]}" + (f" — {note}" if note else "")
        _log_receipt_event(con, receipt_id, "status", text, changed_by)
        _incentives().sync_receipt(con, receipt_id, changed_by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


WORK_STAGES = {"persiapan": "Persiapan", "pemasangan": "Pemasangan", "qc": "QC"}


def set_work_stage(receipt_id: str, stage: str, by: str | None = None) -> dict:
    """K-06: the stage of a nota that is Dalam Pengerjaan -- Persiapan, Pemasangan or QC. Information for the
    shop floor: stages need not follow one order, and nothing about payment or stock depends on them.
    Messages: 'tidak ditemukan' -> 404, 'Tahap' -> 422, the rest -> 409."""
    if stage not in WORK_STAGES:
        raise ValueError("Tahap kerja harus salah satu dari: " + ", ".join(WORK_STAGES.values()) + ".")
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError("Nota sudah dibatalkan (void).")
        if row["work_status"] != "pengerjaan":
            raise ValueError("Tahap kerja hanya bisa diatur saat nota berstatus Dalam Pengerjaan.")
        if row["work_stage"] == stage:
            raise ValueError("Nota sudah berada di tahap itu.")
        cur = con.execute("UPDATE receipts SET work_stage=? WHERE id=? AND work_status='pengerjaan'", (stage, receipt_id))
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        _log_receipt_event(con, receipt_id, "stage", f"Tahap kerja: {WORK_STAGES[stage]}", by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def _clear_handover(con: sqlite3.Connection, receipt_id: str, why: str, by: str | None) -> None:
    """The nota is being worked on again, so the vehicle is no longer 'handed over'."""
    row = con.execute("SELECT handed_over_at FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is not None and row["handed_over_at"]:
        con.execute("UPDATE receipts SET handed_over_at=NULL, handed_over_by=NULL, handover_note=NULL, handover_debt=0, handover_receiver=NULL WHERE id=?", (receipt_id,))
        con.execute("UPDATE receipt_handover_files SET voided=1, void_reason=? WHERE receipt_id=? AND voided=0", (why, receipt_id))
        _log_receipt_event(con, receipt_id, "handover", f"Serah terima dibatalkan otomatis: {why}", by)


HANDOVER_MAX_PHOTOS = 4
HANDOVER_FILE_MAX_BYTES = 4 * 1024 * 1024
HANDOVER_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _handover_upload_dir(receipt_id: str) -> Path:
    d = DB_PATH.parent / "uploads" / "handover" / re.sub(r"[^A-Za-z0-9_-]", "_", receipt_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _check_handover_files(files: list[dict] | None) -> list[dict]:
    """Validates what came with a handover: any number up to 4 vehicle photos and one signature. Each file is
    {kind: 'foto'|'tanda_tangan', mime, data: bytes}; the caller already checked the bytes match the type."""
    out = []
    photos = sigs = 0
    for f in files or []:
        kind = f.get("kind")
        if kind not in ("foto", "tanda_tangan"):
            raise ValueError("Jenis lampiran serah terima tidak dikenal.")
        if f.get("mime") not in HANDOVER_MIMES:
            raise ValueError("Lampiran harus berupa gambar JPG, PNG, atau WEBP.")
        if not f.get("data") or len(f["data"]) > HANDOVER_FILE_MAX_BYTES:
            raise ValueError("Ukuran tiap lampiran maksimal 4 MB.")
        photos += kind == "foto"
        sigs += kind == "tanda_tangan"
        out.append(f)
    if photos > HANDOVER_MAX_PHOTOS:
        raise ValueError(f"Foto serah terima maksimal {HANDOVER_MAX_PHOTOS}.")
    if sigs > 1:
        raise ValueError("Tanda tangan hanya satu.")
    return out


def get_handover_files(receipt_id: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, kind, mime, size, created_by, created_at FROM receipt_handover_files "
            "WHERE receipt_id=? AND voided=0 ORDER BY id", (receipt_id,))]


def get_handover_file(file_id: int) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipt_handover_files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise LookupError("Lampiran tidak ditemukan.")
    return dict(row)


def hand_over_receipt(receipt_id: str, note: str | None, reason: str | None, by: str | None, is_admin: bool,
                      receiver_name: str | None = None, files: list[dict] | None = None) -> dict:
    """K-08/K-09 Serah Terima Kendaraan: the vehicle (and goods) are given back to the customer. Recorded apart
    from payment and from closing: only after Pengerjaan Selesai, with who and when. With money still owed
    (a piutang) only an admin may do it, with a reason, and the balance is kept on the record.
    Messages: 'tidak ditemukan' -> 404, 'Alasan' -> 422, 'Hanya admin' -> 403, the rest -> 409."""
    note = (note or "").strip() or None
    reason = (reason or "").strip()
    receiver = (receiver_name or "").strip()[:80] or None
    files = _check_handover_files(files)
    written: list[Path] = []
    try:
        with _conn() as con:
            row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise ValueError("Nota tidak ditemukan.")
            if row["status"] == "void":
                raise ValueError("Nota sudah dibatalkan (void).")
            if row["work_status"] != "selesai":
                raise ValueError("Serah terima hanya bisa dicatat setelah Pengerjaan Selesai.")
            if row["handed_over_at"]:
                raise ValueError("Kendaraan ini sudah dicatat diserahkan.")
            owed = max(row["total"] - row["amount_paid"], 0)
            if owed > 0:
                if not is_admin:
                    raise ValueError("Hanya admin yang boleh menyerahkan kendaraan dengan sisa tagihan (piutang).")
                if len(reason) < 3:
                    raise ValueError("Alasan wajib diisi (minimal 3 karakter) untuk serah terima dengan piutang.")
            now = _now()
            cur = con.execute(
                "UPDATE receipts SET handed_over_at=?, handed_over_by=?, handover_note=?, handover_debt=?, handover_receiver=? "
                "WHERE id=? AND handed_over_at IS NULL AND work_status='selesai'",
                (now, by, (f"{reason}" + (f". {note}" if note else "")) if owed > 0 else note, owed, receiver, receipt_id),
            )
            if cur.rowcount == 0:
                raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
            for f in files:
                path = _handover_upload_dir(receipt_id) / f"{uuid.uuid4().hex}{HANDOVER_MIMES[f['mime']]}"
                path.write_bytes(f["data"])
                written.append(path)
                con.execute(
                    "INSERT INTO receipt_handover_files (receipt_id, kind, mime, path, size, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (receipt_id, f["kind"], f["mime"], str(path), len(f["data"]), by, now))
            text = "Kendaraan diserahkan ke pelanggan" + (f" dengan piutang Rp {owed:,}. Izin admin: {reason}".replace(",", ".") if owed else "")
            if receiver:
                text += f", diterima {receiver}"
            if note:
                text += f" — {note}"
            n_photo = sum(1 for f in files if f["kind"] == "foto")
            if n_photo or any(f["kind"] == "tanda_tangan" for f in files):
                text += " [" + ", ".join(x for x in (f"{n_photo} foto" if n_photo else "", "tanda tangan" if any(f["kind"] == "tanda_tangan" for f in files) else "") if x) + "]"
            _log_receipt_event(con, receipt_id, "handover", text, by)
            return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def undo_handover(receipt_id: str, reason: str, by: str | None = None) -> dict:
    """Admin: a handover recorded by mistake. The reason stays in the nota's history."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan wajib diisi (minimal 3 karakter).")
    with _conn() as con:
        row = con.execute("SELECT handed_over_at FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError("Nota tidak ditemukan.")
        if not row["handed_over_at"]:
            raise ValueError("Nota ini belum dicatat diserahkan.")
        con.execute("UPDATE receipts SET handed_over_at=NULL, handed_over_by=NULL, handover_note=NULL, handover_debt=0, handover_receiver=NULL WHERE id=?", (receipt_id,))
        con.execute("UPDATE receipt_handover_files SET voided=1, void_reason=? WHERE receipt_id=? AND voided=0", (reason, receipt_id))
        _log_receipt_event(con, receipt_id, "handover", f"Serah terima dibatalkan: {reason}", by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def get_receipt_items(receipt_id: str) -> list[dict]:
    with _conn() as con:
        # LEFT JOIN so a line with no master item link (unresolved/hand-typed
        # SKU, or a historical row from before this link existed) still returns,
        # just with sku=NULL — reopening it for edit keeps that link, or lack of
        # one, as-is.
        rows = con.execute(
            """SELECT ri.*, mi.sku AS sku, mi.unit AS unit, mi.sell_price AS master_price, mi.avg_cost_price AS avg_cost_price,
                      (SELECT pi.note FROM preorder_items pi WHERE pi.id = ri.preorder_item_id) AS so_note
               FROM receipt_items ri
               LEFT JOIN master_items mi ON mi.id = ri.master_item_id
               WHERE ri.receipt_id=? ORDER BY ri.id ASC""",
            (receipt_id,),
        ).fetchall()
        # Who installs / sells each line (names only: nothing about incentives is ever part of a nota).
        assigned = _incentives().item_assignments(con, receipt_id)
        out = []
        for r in rows:
            d = dict(r)
            a = assigned.get(d["id"]) or {}
            d["technicians"] = a.get("technicians") or []
            d["sales_employee_id"], d["sales_employee_name"] = a.get("sales_employee_id"), a.get("sales_name")
            d["work_status"], d["work_done_at"] = a.get("work_status"), a.get("work_done_at")
            out.append(d)
        return out


def delete_receipt(receipt_id: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Nota {receipt_id} tidak ditemukan.")
        if row["preorder_id"] or con.execute("SELECT 1 FROM preorders WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Invoice ini milik Sales Order dan tidak bisa dihapus. Void saja bila pesanan batal.")
        if con.execute("SELECT 1 FROM receipt_returns WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Nota ini sudah ada retur barang, jadi tidak bisa dihapus.")
        # Deleting leaves no trace, so it is only for a Draft with no payment; anything else goes through Void.
        if row["work_status"] != "draft":
            raise ValueError("Hanya nota Draft yang bisa dihapus. Nota lain dibatalkan dengan Void.")
        if row["amount_paid"] or con.execute("SELECT 1 FROM receipt_payments WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Nota ini sudah ada pembayaran, jadi tidak bisa dihapus. Batalkan dengan Void.")
        _incentives().on_receipt_deleted(con, receipt_id)
        existing = dict(row)

        # Stock: deleting a receipt that still held stock returns it, same as a
        # void — but a receipt already void holds nothing, so this is a no-op
        # for it (no double-restore).
        if _holds_stock(existing["status"], existing["work_status"]):
            items = [dict(r) for r in con.execute(
                "SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)
            ).fetchall()]
            before_holdings = _receipt_item_holdings(items, con)
            _apply_receipt_stock_delta(con, receipt_id, before_holdings, {})

        con.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
        con.execute("DELETE FROM receipt_events WHERE receipt_id=?", (receipt_id,))
        con.execute("DELETE FROM receipt_payments WHERE receipt_id=?", (receipt_id,))
        con.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))


def list_cash_outs(date_from: str, date_to: str, method: str | None = None, kind: str = "semua") -> dict:
    """K-20 Kas Keluar: money that left the shop in the date range, by the shop's calendar day. Customer refunds
    (money handed back, with proof and the nota) and, since Account Payable, supplier payments: faktur payments
    and PO DP, each with its Kas Keluar number. A DP carried onto a faktur is not new cash and is not listed.
    Totals per method. kind: semua | refund | supplier."""
    shift = f"{SHOP_UTC_OFFSET_MINUTES:+d} minutes"
    items: list[dict] = []
    with _conn() as con:
        if kind in ("semua", "refund"):
            sql = """SELECT p.id, p.paid_at, -p.amount AS amount, p.method, p.note, p.created_by, p.proof_ref,
                            r.id AS receipt_id, r.receipt_no, r.plate_full, r.customer_name, r.preorder_id,
                            (SELECT o.po_number FROM preorders o WHERE o.id = r.preorder_id) AS po_number
                     FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id
                     WHERE p.kind = 'refund' AND p.amount < 0 AND date(p.paid_at, ?) BETWEEN ? AND ?"""
            params: list = [shift, date_from, date_to]
            if method:
                sql += " AND p.method = ?"
                params.append(method)
            for r in con.execute(sql, params).fetchall():
                items.append({**dict(r), "source": "refund", "source_label": "Refund pelanggan"})
        if kind in ("semua", "supplier"):
            _kk_assign(con)
            msql = " AND p.method = ?" if method else ""
            mp = [method] if method else []
            for r in con.execute(
                    f"""SELECT p.id, p.paid_at, p.amount, p.method, p.note, p.created_by, p.reference_no AS proof_ref, p.kk_number, p.proof_status,
                               p.source_account, p.dest_account, i.id AS invoice_id, i.invoice_number, s.name AS supplier_name, o.po_number
                        FROM purchase_invoice_payments p JOIN purchase_invoices i ON i.id = p.invoice_id JOIN suppliers s ON s.id = i.supplier_id
                        LEFT JOIN supplier_pos o ON o.id = i.supplier_po_id
                        WHERE p.kind IN ('bayar','batal') AND date(p.paid_at, ?) BETWEEN ? AND ?{msql}""", [shift, date_from, date_to] + mp).fetchall():
                items.append({**dict(r), "source": "supplier", "source_label": "Pembayaran faktur supplier"})
            for r in con.execute(
                    f"""SELECT p.id, p.paid_at, p.amount, p.method, p.note, p.created_by, p.reference_no AS proof_ref, p.kk_number, p.bank_account AS source_account,
                               o.id AS po_id, o.po_number, s.name AS supplier_name
                        FROM spo_payments p JOIN supplier_pos o ON o.id = p.supplier_po_id JOIN suppliers s ON s.id = o.supplier_id
                        WHERE date(p.paid_at, ?) BETWEEN ? AND ?{msql}""", [shift, date_from, date_to] + mp).fetchall():
                items.append({**dict(r), "source": "dp", "source_label": "DP supplier (uang muka PO)"})
    items.sort(key=lambda i: (i["paid_at"], i["id"]), reverse=True)
    by_method: dict[str, int] = {}
    for i in items:
        by_method[i["method"]] = by_method.get(i["method"], 0) + i["amount"]
    by_source = {k: sum(i["amount"] for i in items if i["source"] == k) for k in ("refund", "supplier", "dp")}
    return {"items": items, "total": sum(i["amount"] for i in items), "count": len(items), "by_method": by_method, "by_source": by_source}


def get_sales_summary(date_from: str, date_to: str) -> dict:
    """Money in per day, by the date each payment was taken (shop's clock): a DP counts on the day it
    was paid, a pelunasan on its own day. Refunds/reversals (negative rows) net out; void notas are left out."""
    shift = f"{SHOP_UTC_OFFSET_MINUTES:+d} minutes"
    with _conn() as con:
        rows = con.execute(
            """SELECT date(p.paid_at, ?) AS day, SUM(p.amount) AS revenue, COUNT(DISTINCT p.receipt_id) AS receipt_count
               FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id
               WHERE r.status != 'void' AND p.state = 'sah' AND COALESCE(p.kind, '') <> 'pindah' AND date(p.paid_at, ?) BETWEEN ? AND ?
               GROUP BY date(p.paid_at, ?)
               ORDER BY day ASC""",
            (shift, shift, date_from, date_to, shift),
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
        used_vehicles = con.execute(
            """SELECT COUNT(*) AS n FROM vehicles v WHERE v.customer_id=? AND (
                   EXISTS (SELECT 1 FROM service_orders s WHERE s.vehicle_id=v.id) OR EXISTS (SELECT 1 FROM preorders p WHERE p.vehicle_id=v.id))""",
            (customer_id,)).fetchone()["n"]
        if used_vehicles:
            raise ValueError("Kendaraan pelanggan ini sudah dipakai di Sales Order atau Service, jadi pelanggan tidak bisa dihapus. "
                             "Nonaktifkan saja jika sudah tidak dipakai.")
        con.execute("DELETE FROM vehicles WHERE customer_id=?", (customer_id,))
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
        raise ValueError(f"Urutan SKU untuk prefiks {prefix} sudah habis (mencapai 999), SKU baru tidak bisa dibuat")
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
    email: str | None = None,
) -> dict:
    """Shared insert path for create_supplier/create_brand — same validation and
    insert shape for both tables, only the table name differs. `phone`/`address`/
    `contact_person`/`npwp` are suppliers-only columns (brands have no such
    fields) — ignored for brands."""
    entity_label = "Supplier" if table == "suppliers" else "Merek"
    name_normalized = _normalize_name(name)
    created_at = _now()
    with _conn() as con:
        dup = con.execute(
            f"SELECT 1 FROM {table} WHERE name_normalized=? AND is_active=1", (name_normalized,)
        ).fetchone()
        if dup is not None:
            raise ValueError(f"{entity_label} '{name}' sudah ada")

        if code is not None:
            code = code.strip().upper()
            if not _CODE_PATTERN.match(code):
                raise ValueError("Kode harus 2-10 karakter huruf atau angka")
            existing_code = con.execute(f"SELECT 1 FROM {table} WHERE code=?", (code,)).fetchone()
            if existing_code is not None:
                raise ValueError(f"Kode '{code}' sudah dipakai")
        else:
            code = _generate_entity_code(con, table, name)

        if table == "suppliers":
            cur = con.execute(
                """INSERT INTO suppliers
                   (code, name, is_active, created_at, name_normalized, is_system, source,
                    phone, address, contact_person, npwp, email)
                   VALUES (?, ?, 1, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
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
                    (email or "").strip() or None,
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
    email: str | None = None,
) -> dict:
    entity_label = "Supplier" if table == "suppliers" else "Merek"
    supplier_details = (phone, address, contact_person, npwp, email)
    with _conn() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"{entity_label} {entity_id} tidak ditemukan")
        if row["is_system"] and (name is not None or is_active is not None or any(d is not None for d in supplier_details)):
            raise ValueError(f"{entity_label} bawaan sistem tidak bisa diubah")
        new_name = name if name is not None else row["name"]
        new_name_normalized = _normalize_name(new_name) if name is not None else row["name_normalized"]
        if name is not None:
            dup = con.execute(
                f"SELECT 1 FROM {table} WHERE name_normalized=? AND is_active=1 AND id!=?",
                (new_name_normalized, entity_id),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"{entity_label} '{name}' sudah ada")
        new_active = int(is_active) if is_active is not None else row["is_active"]

        if table == "suppliers":
            def _merged(value: str | None, column: str) -> str | None:
                return (value.strip() or None) if value is not None else row[column]

            con.execute(
                """UPDATE suppliers
                   SET name=?, name_normalized=?, is_active=?, phone=?, address=?, contact_person=?, npwp=?, email=?
                   WHERE id=?""",
                (
                    new_name,
                    new_name_normalized,
                    new_active,
                    _merged(phone, "phone"),
                    _merged(address, "address"),
                    _merged(contact_person, "contact_person"),
                    _merged(npwp, "npwp"),
                    _merged(email, "email"),
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
        # Every table that names a supplier: documents, Sales Order lines, returns, quarantine, service rules.
        refs = (("master_items", "supplier_id"), ("inbound_documents", "supplier_id"), ("purchase_invoices", "supplier_id"),
                ("supplier_pos", "supplier_id"), ("preorders", "supplier_id"), ("preorder_items", "supplier_id"),
                ("quarantine_lots", "supplier_id"), ("supplier_returns", "supplier_id"), ("supplier_credit_notes", "supplier_id"),
                ("service_profiles", "default_supplier_id"), ("service_profile_overrides", "supplier_id"), ("service_orders", "supplier_id"))
    else:
        refs = (("master_items", "brand_id"), ("inbound_items", "pending_brand_id"), ("service_profile_overrides", "brand_id"))
    row = con.execute(" UNION ".join(f"SELECT 1 FROM {t} WHERE {c}=?" for t, c in refs) + " LIMIT 1", (entity_id,) * len(refs)).fetchone()
    return row is not None


def _delete_quick_add_entity(table: str, entity_id: int) -> None:
    entity_label = "Supplier" if table == "suppliers" else "Merek"
    with _conn() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"{entity_label} {entity_id} tidak ditemukan")
        if row["is_system"]:
            raise ValueError(f"{entity_label} bawaan sistem tidak bisa dihapus")
        if _entity_in_use(con, table, entity_id):
            raise ValueError(f"{entity_label} masih dipakai dan tidak bisa dihapus")
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
    email: str | None = None,
) -> dict:
    return _create_quick_add_entity("suppliers", name, code, source, phone, address, contact_person, npwp, email)


def update_supplier(
    supplier_id: int,
    name: str | None = None,
    is_active: bool | None = None,
    phone: str | None = None,
    address: str | None = None,
    contact_person: str | None = None,
    npwp: str | None = None,
    email: str | None = None,
) -> dict:
    return _update_quick_add_entity(
        "suppliers", supplier_id, name, is_active, phone, address, contact_person, npwp, email
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


def _unit_key(name: str) -> str:
    """Duplicates are judged on the trimmed, upper-cased name: 'pcs', 'PCS ' and 'Pcs' are one unit."""
    return re.sub(r"\s+", " ", (name or "").strip()).upper()


def _unit_clash(con: sqlite3.Connection, name: str, ignore_id: int | None = None) -> dict | None:
    for r in con.execute("SELECT id, name, is_active FROM units").fetchall():
        if r["id"] != ignore_id and _unit_key(r["name"]) == _unit_key(name):
            return dict(r)
    return None


def _clash_message(name: str, clash: dict) -> str:
    if clash["is_active"]:
        return f"Satuan '{name.strip()}' sudah ada."
    return f"Satuan '{name.strip()}' sudah ada tetapi nonaktif. Aktifkan kembali dari daftar."


def create_unit(name: str, description: str | None = None) -> dict:
    name = re.sub(r"\s+", " ", name.strip()).upper()
    if not name:
        raise ValueError("Nama satuan wajib diisi.")
    if len(name) > 30:
        raise ValueError("Nama satuan maksimal 30 karakter.")
    created_at = _now()
    with _conn() as con:
        clash = _unit_clash(con, name)
        if clash is not None:
            raise ValueError(_clash_message(name, clash))
        cur = con.execute(
            "INSERT INTO units (name, name_normalized, is_active, created_at, description, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
            (name, _unit_key(name), created_at, (description or "").strip() or None, created_at),
        )
    return get_all_units_with_id(cur.lastrowid)


def get_all_units_with_id(unit_id: int) -> dict:
    return next(u for u in get_all_units() if u["id"] == unit_id)


def _unit_usage(con: sqlite3.Connection) -> dict[str, dict]:
    """Per upper-cased unit name: how many products use it (all / still active) and whether any document does.
    Units are stored on products and documents as plain text, so this matches by name."""
    use: dict[str, dict] = {}
    for r in con.execute("SELECT UPPER(TRIM(unit)) AS k, COUNT(*) AS n, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS a FROM master_items GROUP BY UPPER(TRIM(unit))").fetchall():
        use[r["k"]] = {"products": r["n"], "active_products": r["a"] or 0, "docs": 0}
    for table, col in (("inbound_items", "pending_unit"), ("preorder_items", "unit"), ("supplier_po_items", "unit"), ("receipt_items", "unit")):
        try:
            rows = con.execute(f"SELECT UPPER(TRIM({col})) AS k, COUNT(*) AS n FROM {table} WHERE {col} IS NOT NULL AND TRIM({col}) != '' GROUP BY UPPER(TRIM({col}))").fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            slot = use.setdefault(r["k"], {"products": 0, "active_products": 0, "docs": 0})
            slot["docs"] += r["n"]
    return use


def get_all_units(active_only: bool = False) -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM units" + (" WHERE is_active=1" if active_only else "") + " ORDER BY name ASC").fetchall()
        usage = _unit_usage(con)
    out = []
    for r in rows:
        u = usage.get(_unit_key(r["name"]), {"products": 0, "active_products": 0, "docs": 0})
        out.append({**dict(r), "product_count": u["products"], "active_product_count": u["active_products"],
                    "in_use": bool(u["products"] or u["docs"])})
    return out


def _unit_same_word(old: str, new: str) -> bool:
    """A rename that only fixes spelling or abbreviation (PCS/PC, ROLL/ROL, METER/MTR) rather than naming a different unit."""
    a, b = re.sub(r"[^A-Z0-9]", "", old.upper()), re.sub(r"[^A-Z0-9]", "", new.upper())
    return bool(a) and bool(b) and (a == b or fuzz.ratio(a, b) >= 60)


def update_unit(unit_id: int, name: str | None = None, is_active: bool | None = None, description: str | None = None,
                reason: str | None = None, confirm_rename: bool = False, fields_set: set | None = None) -> dict:
    fields_set = fields_set if fields_set is not None else {k for k, v in (("name", name), ("is_active", is_active), ("description", description)) if v is not None}
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        if row is None:
            raise ValueError(f"Satuan {unit_id} tidak ditemukan.")
        new_name = re.sub(r"\s+", " ", name.strip()).upper() if name is not None else row["name"]
        if name is not None:
            if not new_name:
                raise ValueError("Nama satuan wajib diisi.")
            if len(new_name) > 30:
                raise ValueError("Nama satuan maksimal 30 karakter.")
            clash = _unit_clash(con, new_name, ignore_id=unit_id)
            if clash is not None:
                raise ValueError(_clash_message(new_name, clash))
        renamed = name is not None and new_name != row["name"]
        if renamed:
            usage = _unit_usage(con).get(_unit_key(row["name"]), {"products": 0, "docs": 0})
            # Changing what a unit MEANS (PCS -> METER) is not a spelling fix: stock, prices and printed notas in the old
            # unit would silently change meaning. Refused for a unit that is in use; make a new unit instead.
            if (usage["products"] > 0 or usage.get("docs", 0) > 0) and not _unit_same_word(row["name"], new_name):
                raise ValueError(
                    f"'{row['name']}' ke '{new_name}' mengubah arti satuan, bukan perbaikan penulisan, dan satuan ini sudah dipakai. "
                    f"Buat satuan baru '{new_name}' dan pilih di barangnya; konversi antar satuan belum didukung.")
            if usage["products"] > 0 and not confirm_rename:
                raise ValueError(
                    f"Satuan '{row['name']}' dipakai {usage['products']} produk. Konfirmasi dulu bahwa perubahan ini hanya perbaikan penulisan, bukan mengganti arti satuan."
                )
            # Products and unposted documents show the new spelling; transaction snapshots (receipts, POs) keep what they were.
            con.execute("UPDATE master_items SET unit=? WHERE UPPER(TRIM(unit))=?", (new_name, _unit_key(row["name"])))
            con.execute("UPDATE inbound_items SET pending_unit=? WHERE UPPER(TRIM(pending_unit))=?", (new_name, _unit_key(row["name"])))
        new_desc = ((description or "").strip() or None) if "description" in fields_set else row["description"]
        new_active = int(is_active) if is_active is not None else row["is_active"]
        new_reason = row["deactivated_reason"]
        if is_active is not None:
            new_reason = None if is_active else ((reason or "").strip() or None)
        con.execute(
            "UPDATE units SET name=?, name_normalized=?, is_active=?, description=?, deactivated_reason=?, updated_at=? WHERE id=?",
            (new_name, _unit_key(new_name), new_active, new_desc, new_reason, _now(), unit_id),
        )
    return get_all_units_with_id(unit_id)


def get_unit(unit_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(row) if row is not None else None


def get_unit_by_name(name: str) -> dict | None:
    """Case-insensitive lookup against active units: used to validate a submitted unit string
    (Tambah Barang, item edit, Barang Masuk) against Master Satuan before it is saved."""
    with _conn() as con:
        for r in con.execute("SELECT * FROM units WHERE is_active=1").fetchall():
            if _unit_key(r["name"]) == _unit_key(name):
                return dict(r)
        return None


def delete_unit(unit_id: int) -> None:
    """Only a unit nothing ever used can go. Anything with history is deactivated instead."""
    with _conn() as con:
        row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        if row is None:
            raise ValueError(f"Satuan {unit_id} tidak ditemukan.")
        u = _unit_usage(con).get(_unit_key(row["name"]))
        if u and (u["products"] or u["docs"]):
            raise ValueError("Satuan sudah dipakai produk atau transaksi, jadi tidak bisa dihapus. Nonaktifkan saja.")
        con.execute("DELETE FROM units WHERE id=?", (unit_id,))


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
            raise ValueError(f"Supplier {supplier_id} tidak ditemukan.")
        brand = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
        if brand is None:
            raise ValueError(f"Merek {brand_id} tidak ditemukan.")
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
        raise ValueError(f"ID barang {master_item_id} terlalu besar untuk dikodekan dalam 8 digit barcode")
    return "20" + str(master_item_id).zfill(8)


def create_master_item(
    supplier_id: int, brand_id: int, name: str, unit: str, sell_price: int = 0, cost_price: int = 0,
    stock_qty: int = 0, description: str | None = None, min_stock: int = LOW_STOCK_THRESHOLD,
    location: str | None = None, created_by: str | None = None, category_id: int | None = None,
    is_active: bool = True,
) -> dict:
    name_normalized = _normalize_name(name)
    with _conn() as con:
        supplier = con.execute("SELECT code FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError(f"Supplier {supplier_id} tidak ditemukan.")
        brand = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
        if brand is None:
            raise ValueError(f"Merek {brand_id} tidak ditemukan.")
        dup = _find_exact_duplicate_master_item(con, supplier_id, brand_id, name_normalized)
        if dup is not None:
            raise ValueError(f"Barang ganda: SKU {dup['sku']} sudah ada untuk supplier dan merek ini")

        segment = _compute_product_segment(name_normalized)
        sku, prefix, seq = _next_sku(con, supplier["code"], brand["code"], segment)
        created_at = _now()
        cur = con.execute(
            """INSERT INTO master_items
               (sku, sku_prefix, sku_seq, supplier_id, brand_id, name, name_normalized, unit,
                last_cost_price, avg_cost_price, sell_price, stock_qty, first_received_date, is_active, created_at,
                description, min_stock, location, updated_at, category_id, updated_by, archived_at, archived_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sku, prefix, seq, supplier_id, brand_id, name, name_normalized, unit,
                cost_price, cost_price, sell_price, stock_qty,
                1 if is_active else 0, created_at, (description or "").strip() or None, min_stock,
                (location or "").strip() or None, created_at, category_id, created_by,
                None if is_active else created_at, None if is_active else "Dinonaktifkan saat barang dibuat",
            ),
        )
        # The row id isn't known until after INSERT, so the barcode is generated
        # and written back in the same transaction as the insert.
        master_item_id = cur.lastrowid
        con.execute(
            "UPDATE master_items SET barcode=? WHERE id=?",
            (_generate_barcode_for_id(master_item_id), master_item_id),
        )
        con.execute(
            """INSERT INTO master_item_price_history (master_item_id, cost_price, sell_price, changed_by, note, created_at)
               VALUES (?, ?, ?, ?, 'Harga awal saat barang ditambahkan', ?)""",
            (master_item_id, cost_price, sell_price, created_by, created_at),
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
    master_item_id: int, name: str | None = None, sell_price: int | None = None, unit: str | None = None,
    description: str | None = None, min_stock: int | None = None, location: str | None = None,
    changed_by: str | None = None, price_note: str | None = None,
    set_category: bool = False, category_id: int | None = None, supplier_id: int | None = None,
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Barang {master_item_id} tidak ditemukan.")
        new_supplier_id = supplier_id if supplier_id is not None else row["supplier_id"]
        new_name = name if name is not None else row["name"]
        new_name_normalized = _normalize_name(new_name) if name is not None else row["name_normalized"]
        new_sell_price = sell_price if sell_price is not None else row["sell_price"]
        new_unit = unit if unit is not None else row["unit"]
        new_desc = ((description or "").strip() or None) if description is not None else row["description"]
        new_min = min_stock if min_stock is not None else row["min_stock"]
        new_loc = ((location or "").strip() or None) if location is not None else row["location"]
        now = _now()
        try:
            con.execute(
                """UPDATE master_items SET name=?, name_normalized=?, sell_price=?, unit=?,
                          description=?, min_stock=?, location=?, updated_at=?, category_id=?, updated_by=?,
                          supplier_id=? WHERE id=?""",
                (new_name, new_name_normalized, new_sell_price, new_unit, new_desc, new_min, new_loc, now,
                 category_id if set_category else row["category_id"], changed_by, new_supplier_id, master_item_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                "Tidak bisa disimpan: sudah ada barang lain dengan nama ini untuk supplier dan merek yang sama"
            )
        if new_sell_price != row["sell_price"]:
            con.execute(
                """INSERT INTO master_item_price_history (master_item_id, cost_price, sell_price, changed_by, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (master_item_id, row["avg_cost_price"], new_sell_price, changed_by,
                 (price_note or "").strip() or "Ubah harga jual", now),
            )
        updated = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        return dict(updated)


def set_master_item_cost(master_item_id: int, cost_price: int, by: str | None) -> dict:
    """Admin fills in / corrects the cost of a product that holds no stock (e.g. created without a price). With stock on
    hand the cost stays the Barang Masuk average, so the value of the inventory never changes without a receipt."""
    if cost_price < 0:
        raise ValueError("Harga modal tidak boleh negatif.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise LookupError(f"Barang {master_item_id} tidak ditemukan.")
        if cost_price == row["avg_cost_price"]:
            return dict(row)
        if (row["stock_qty"] or 0) != 0 or (row["quarantine_qty"] or 0) != 0:
            raise ValueError("Harga modal hanya bisa diubah saat stok 0. Dengan stok, harga modal dihitung otomatis dari Barang Masuk.")
        now = _now()
        con.execute("UPDATE master_items SET avg_cost_price=?, last_cost_price=?, updated_at=?, updated_by=? WHERE id=?",
                    (cost_price, cost_price, now, by, master_item_id))
        con.execute("""INSERT INTO master_item_price_history (master_item_id, cost_price, sell_price, changed_by, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (master_item_id, cost_price, row["sell_price"], by, "Harga modal diisi Admin (stok 0)", now))
        return dict(con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone())


def set_master_item_archived(master_item_id: int, archived: bool, reason: str | None = None,
                             note: str | None = None, by: str | None = None, mode: str = "nonaktif") -> dict:
    """Archive = switch the product off (is_active=0) and remember why; restoring clears that.
    Nothing is deleted, so every receipt, ledger and PO line that points at it stays valid.
    `mode` is what the admin chose: "nonaktif" keeps the product in the master list (just switched off),
    "arsip" also hides it from the default list."""
    if mode not in ("nonaktif", "arsip"):
        raise ValueError("Tindakan pengarsipan harus 'nonaktif' atau 'arsip'.")
    with _conn() as con:
        row = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Barang {master_item_id} tidak ditemukan.")
        now = _now()
        if archived:
            con.execute(
                "UPDATE master_items SET is_active=0, archived_reason=?, archived_note=?, archived_at=?, archive_mode=?, updated_at=?, updated_by=? WHERE id=?",
                ((reason or "").strip() or None, (note or "").strip() or None, now, mode, now, by, master_item_id),
            )
        else:
            con.execute(
                "UPDATE master_items SET is_active=1, archived_reason=NULL, archived_note=NULL, archived_at=NULL, archive_mode=NULL, updated_at=?, updated_by=? WHERE id=?",
                (now, by, master_item_id),
            )
        return dict(con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone())


def get_master_item_usage(master_item_id: int) -> dict:
    """How much history hangs off a product: used by the archive dialog and the detail page tiles."""
    with _conn() as con:
        kasir = con.execute(
            """SELECT COUNT(DISTINCT ri.receipt_id) AS c FROM receipt_items ri
               JOIN receipts r ON r.id = ri.receipt_id
               WHERE ri.master_item_id=? AND r.work_status != 'draft'""",
            (master_item_id,),
        ).fetchone()["c"]
        inbound = con.execute(
            "SELECT COUNT(DISTINCT document_id) AS c FROM inbound_items WHERE master_item_id=?", (master_item_id,)
        ).fetchone()["c"]
        po_supplier = con.execute(
            "SELECT COUNT(DISTINCT supplier_po_id) AS c FROM supplier_po_items WHERE master_item_id=?",
            (master_item_id,),
        ).fetchone()["c"]
        po_customer = 0
        try:
            po_customer = con.execute(
                "SELECT COUNT(DISTINCT preorder_id) AS c FROM preorder_items WHERE master_item_id=?",
                (master_item_id,),
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            pass
        ledger = con.execute(
            "SELECT COUNT(*) AS c FROM stock_ledger WHERE master_item_id=?", (master_item_id,)
        ).fetchone()["c"]
        return {"kasir": kasir, "barang_masuk": inbound, "po_supplier": po_supplier, "po_customer": po_customer, "ledger": ledger}


def get_master_item_price_history(master_item_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM master_item_price_history WHERE master_item_id=? ORDER BY id DESC",
            (master_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_master_item_suppliers(master_item_id: int) -> list[dict]:
    """The product's own supplier (Utama) plus every other supplier it was ever received from."""
    with _conn() as con:
        item = con.execute(
            """SELECT mi.supplier_id, s.name, s.phone, s.address FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id WHERE mi.id=?""",
            (master_item_id,),
        ).fetchone()
        if item is None:
            return []
        rows = con.execute(
            """SELECT d.supplier_id, s.name, s.phone, s.address, ii.cost_price, ii.qty_in, d.received_date
               FROM inbound_items ii
               JOIN inbound_documents d ON d.id = ii.document_id
               JOIN suppliers s ON s.id = d.supplier_id
               WHERE ii.master_item_id=? AND ii.received_date IS NOT NULL
               ORDER BY ii.received_date DESC, ii.id DESC""",
            (master_item_id,),
        ).fetchall()
        out: dict[int, dict] = {
            item["supplier_id"]: {
                "supplier_id": item["supplier_id"], "name": item["name"], "phone": item["phone"],
                "address": item["address"], "is_primary": True, "last_cost_price": None,
                "last_received_date": None,
            }
        }
        for r in rows:
            e = out.setdefault(r["supplier_id"], {
                "supplier_id": r["supplier_id"], "name": r["name"], "phone": r["phone"],
                "address": r["address"], "is_primary": False, "last_cost_price": None,
                "last_received_date": None,
            })
            if e["last_cost_price"] is None:
                e["last_cost_price"] = r["cost_price"]
                e["last_received_date"] = r["received_date"]
        return sorted(out.values(), key=lambda e: (not e["is_primary"], e["name"]))


def get_master_item_purchases(master_item_id: int, limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT ii.received_date, d.doc_number, d.supplier_id, s.name AS supplier_name,
                      ii.qty_in, ii.cost_price, ii.qty_in * ii.cost_price AS total
               FROM inbound_items ii
               JOIN inbound_documents d ON d.id = ii.document_id
               JOIN suppliers s ON s.id = d.supplier_id
               WHERE ii.master_item_id=? AND ii.received_date IS NOT NULL
               ORDER BY ii.received_date DESC, ii.id DESC LIMIT ?""",
            (master_item_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


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
            raise ValueError(f"Barang {master_item_id} tidak ditemukan.")
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
            raise ValueError(f"Barang {master_item_id} tidak ditemukan.")
        con.execute("UPDATE master_items SET image_path=? WHERE id=?", (image_path, master_item_id))
        return row["image_path"]


def get_master_item(master_item_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name, c.name AS category_name,
                      c.parent_id AS category_parent_id, cp.name AS category_parent_name
               FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               LEFT JOIN categories c ON c.id = mi.category_id
               LEFT JOIN categories cp ON cp.id = c.parent_id
               WHERE mi.id=?""",
            (master_item_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def find_archived_skus(skus: list[str]) -> list[str]:
    """Of these SKUs, the ones that belong to an archived (is_active=0) product."""
    if not skus:
        return []
    with _conn() as con:
        marks = ",".join("?" * len(skus))
        rows = con.execute(
            f"SELECT sku FROM master_items WHERE is_active=0 AND sku IN ({marks})", [s.strip().upper() for s in skus]
        ).fetchall()
        return [r["sku"] for r in rows]


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
    # Then the shop's own Kode Manual (what Goods Receipt labels encode), or a code it replaced: labels already
    # on the shelf keep scanning.
    by_manual = _get_master_item_by_manual_code(code)
    if by_manual is not None:
        return by_manual
    return get_master_item_by_sku(code)


def _get_master_item_by_manual_code(code: str) -> dict | None:
    c = re.sub(r"\s+", "", (code or "").strip().upper())
    with _conn() as con:
        row = con.execute(
            """SELECT mi.*, s.name AS supplier_name, b.name AS brand_name FROM master_items mi
               JOIN suppliers s ON s.id = mi.supplier_id
               JOIN brands b ON b.id = mi.brand_id
               WHERE mi.is_active=1 AND (UPPER(mi.manual_code)=?
                     OR mi.id IN (SELECT master_item_id FROM item_code_aliases WHERE UPPER(code)=?))
               ORDER BY UPPER(mi.manual_code)=? DESC LIMIT 1""",
            (c, c, c),
        ).fetchone()
        return dict(row) if row is not None else None


# Stock state of a product, judged against its own minimum stock (min_stock, default 5).
MASTER_STOCK_CLAUSES = {
    "habis": "mi.stock_qty <= 0",
    "menipis": "(mi.stock_qty > 0 AND mi.stock_qty <= mi.min_stock)",
    "aman": "mi.stock_qty > mi.min_stock",
}

# ORDER BY fragments for the Master Barang list; the keys are fixed here so no request text reaches SQL.
MASTER_SORTS = {
    "newest": "mi.created_at DESC, mi.id DESC",
    "oldest": "mi.created_at ASC, mi.id ASC",
    "name_asc": "mi.name COLLATE NOCASE ASC, mi.id ASC",
    "stock_asc": "mi.stock_qty ASC, mi.name COLLATE NOCASE ASC",
    "stock_desc": "mi.stock_qty DESC, mi.name COLLATE NOCASE ASC",
    "sell_asc": "mi.sell_price ASC, mi.name COLLATE NOCASE ASC",
    "sell_desc": "mi.sell_price DESC, mi.name COLLATE NOCASE ASC",
    "margin_desc": "CASE WHEN mi.avg_cost_price > 0 THEN (mi.sell_price - mi.avg_cost_price) * 1.0 / mi.avg_cost_price END DESC, mi.id DESC",
}


def get_master_items_summary(tz_offset_minutes: int | None = None) -> dict:
    """Header tiles of Master Barang. 'diarsipkan' is a product that is switched off; stock counts
    cover active products only, since an archived one is not for sale anyway."""
    month_start = shop_today().replace(day=1).isoformat()
    with _conn() as con:
        row = con.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(is_active = 1), 0) AS aktif,
                      COALESCE(SUM(is_active = 0), 0) AS diarsipkan,
                      COALESCE(SUM(is_active = 1 AND stock_qty <= 0), 0) AS habis,
                      COALESCE(SUM(is_active = 1 AND stock_qty > 0 AND stock_qty <= min_stock), 0) AS menipis,
                      COALESCE(SUM(is_active = 1 AND sell_price <= 0), 0) AS tanpa_harga_jual,
                      COALESCE(SUM(substr(created_at, 1, 10) >= ?), 0) AS baru_bulan_ini
               FROM master_items""",
            (month_start,),
        ).fetchone()
        return dict(row)


def list_master_items(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    supplier_id: int | None = None,
    brand_id: int | None = None,
    is_active: bool | None = None,
    stock_status: str | None = None,
    stock_min: int | None = None,
    stock_max: int | None = None,
    no_price: bool = False,
    sort: str = "newest",
    category_ids: list[int] | None = None,
    archived: bool = False,
) -> dict:
    offset = (page - 1) * page_size
    where_clauses = []
    params: list = []
    if search:
        where_clauses.append("(mi.name LIKE ? OR mi.sku LIKE ? OR IFNULL(mi.manual_code,'') LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if supplier_id is not None:
        where_clauses.append("mi.supplier_id=?")
        params.append(supplier_id)
    if brand_id is not None:
        where_clauses.append("mi.brand_id=?")
        params.append(brand_id)
    if is_active is not None:
        where_clauses.append("mi.is_active=?")
        params.append(1 if is_active else 0)
    if archived:
        where_clauses.append("mi.archive_mode='arsip'")
    elif is_active is None:
        # "Semua status" leaves out products that were fully archived; they have their own filter.
        where_clauses.append("COALESCE(mi.archive_mode, '') <> 'arsip'")
    if stock_status:
        where_clauses.append(MASTER_STOCK_CLAUSES[stock_status])
    if stock_min is not None:
        where_clauses.append("mi.stock_qty >= ?")
        params.append(stock_min)
    if stock_max is not None:
        where_clauses.append("mi.stock_qty <= ?")
        params.append(stock_max)
    if no_price:
        where_clauses.append("mi.sell_price <= 0")
    if category_ids is not None:
        where_clauses.append(f"mi.category_id IN ({','.join('?' * len(category_ids)) or 'NULL'})")
        params.extend(category_ids)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with _conn() as con:
        total_row = con.execute(f"SELECT COUNT(*) AS c FROM master_items mi {where_sql}", params).fetchone()
        total = total_row["c"]
        rows = con.execute(
            f"""SELECT mi.*, s.name AS supplier_name, b.name AS brand_name, c.name AS category_name,
                       c.parent_id AS category_parent_id, cp.name AS category_parent_name
                FROM master_items mi
                JOIN suppliers s ON s.id = mi.supplier_id
                JOIN brands b ON b.id = mi.brand_id
                LEFT JOIN categories c ON c.id = mi.category_id
                LEFT JOIN categories cp ON cp.id = c.parent_id
                {where_sql}
                ORDER BY {MASTER_SORTS[sort]}
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
                      mi.stock_qty, mi.min_stock, mi.location, mi.description,
                      mi.avg_cost_price, mi.last_cost_price, mi.sell_price, mi.is_active
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
               WHERE mi.is_active=1 AND mi.stock_qty <= mi.min_stock ORDER BY mi.stock_qty ASC""",
        ).fetchall()

        # Pending invoices: receipts not yet fully paid (amount_paid < total).
        pending_totals = con.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(total - amount_paid), 0) AS outstanding "
            "FROM receipts WHERE status='pending' AND work_status != 'draft'"
        ).fetchone()
        pending_rows = con.execute(
            """SELECT id, plate_full, customer_name, total, amount_paid,
                      (total - amount_paid) AS outstanding, created_at
               FROM receipts WHERE status='pending' AND work_status != 'draft'
               ORDER BY created_at DESC LIMIT 5"""
        ).fetchall()
        # K-08: finished work the customer has not collected yet.
        ready_handover = [dict(r) for r in con.execute(
            """SELECT id, receipt_no, plate_full, customer_name, total, amount_paid,
                      MAX(total - amount_paid, 0) AS outstanding, created_at
               FROM receipts WHERE status <> 'void' AND work_status = 'selesai' AND handed_over_at IS NULL
               ORDER BY created_at ASC""").fetchall()]

        # Latest incoming products from posted Barang Masuk documents.
        latest_incoming_rows = con.execute(
            """SELECT ii.qty_in, ii.received_date, mi.name AS product_name, mi.sku,
                      s.name AS supplier_name
               FROM inbound_items ii
               JOIN inbound_documents d ON d.id = ii.document_id
               JOIN master_items mi ON mi.id = ii.master_item_id
               JOIN suppliers s ON s.id = d.supplier_id
               WHERE d.status='posted' AND ii.master_item_id IS NOT NULL
               ORDER BY ii.received_date DESC, ii.id DESC LIMIT 20"""
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
               ORDER BY units_sold DESC, revenue DESC LIMIT 20""",
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
        preorder_paid = _preorder_paid_by_order(con)
        draft_pend = _preorder_pending_by_order(con)
        po_rows = [{**dict(r), "draft_pending": draft_pend.get(r["id"], 0)}
                   for r in con.execute("SELECT * FROM preorders ORDER BY created_at DESC").fetchall()]
        po_inv = po_invoices(con, [r["receipt_id"] for r in po_rows])
        preorders_open = [
            _preorder_row(r, preorder_items_by_order.get(r["id"], []), preorder_paid.get(r["id"], 0), po_inv.get(r["receipt_id"]))
            for r in po_rows
        ]
        # Fully installed orders are finished business and drop off the dashboard.
        preorders_open = [r for r in preorders_open if r["status"] not in PO_CLOSED_LINE_STATUSES]

        # Sales this calendar month (paid receipts).
        month_start = date.today().replace(day=1).isoformat()
        shift = f"{SHOP_UTC_OFFSET_MINUTES:+d} minutes"
        sales_month = con.execute(
            "SELECT COALESCE(SUM(p.amount), 0) AS revenue, COUNT(DISTINCT p.receipt_id) AS c "
            "FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id "
            "WHERE r.status != 'void' AND p.state = 'sah' AND COALESCE(p.kind, '') <> 'pindah' AND date(p.paid_at, ?) >= ?",
            (shift, month_start),
        ).fetchone()
        # K-21: the second number -- what was sold, counted when the nota closed (Lunas + Selesai).
        closed_month = con.execute(
            "SELECT COALESCE(SUM(total), 0) AS revenue, COUNT(*) AS c FROM receipts "
            "WHERE status = 'done' AND closed_at IS NOT NULL AND date(closed_at, ?) >= ?",
            (shift, month_start),
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
            "ready_handover": ready_handover,
            "latest_incoming": [dict(r) for r in latest_incoming_rows],
            "top_selling": [dict(r) for r in top_selling_rows],
            "sales_month_revenue": sales_month["revenue"],
            "sales_month_count": sales_month["c"],
            "sales_closed_month_revenue": closed_month["revenue"],
            "sales_closed_month_count": closed_month["c"],
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
    # Every word typed must hit the name, SKU or barcode, in any order, so "KIT WIPER FLUID 500 ML" still finds "KIT WIPER FLUID 500ML".
    where_clauses = ["mi.is_active=1"]
    params: list = []
    for word in (query.split() or [query]):
        like = f"%{word}%"
        where_clauses.append("(mi.name LIKE ? OR mi.sku LIKE ? OR mi.barcode LIKE ? OR IFNULL(mi.manual_code,'') LIKE ?)")
        params += [like, like, like, like]
    if supplier_id is not None:
        where_clauses.append("mi.supplier_id=?")
        params.append(supplier_id)
    if brand_id is not None:
        where_clauses.append("mi.brand_id=?")
        params.append(brand_id)
    where_sql = " AND ".join(where_clauses)

    order_params = [query, query, query, f"{query}%"]
    with _conn() as con:
        rows = con.execute(
            f"""SELECT mi.*, s.name AS supplier_name, b.name AS brand_name
                FROM master_items mi
                JOIN suppliers s ON s.id = mi.supplier_id
                JOIN brands b ON b.id = mi.brand_id
                WHERE {where_sql}
                ORDER BY
                    CASE
                        WHEN UPPER(mi.barcode)=UPPER(?) OR UPPER(mi.sku)=UPPER(?) OR UPPER(IFNULL(mi.manual_code,''))=UPPER(?) THEN 0
                        WHEN UPPER(mi.sku) LIKE UPPER(?) THEN 1
                        ELSE 2
                    END,
                    mi.name ASC
                LIMIT ?""",
            params + order_params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]


# --- Inbound documents ------------------------------------------------------


def _migrate_goods_receipt(con: sqlite3.Connection) -> None:
    """Goods Receipt (2026-10-21): one GR may span several POs of one supplier, a line splits into good / bad and
    PO / excess on posting, and each SKU carries one Kode Manual for its labels."""
    # One document may hold the same product twice (it is on two POs, or part of it is excess), so the old
    # one-row-per-product rule goes. SQLite can only drop a table constraint by rebuilding the table.
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='inbound_items'").fetchone()["sql"]
    if re.search(r"UNIQUE\s*\(\s*document_id\s*,\s*master_item_id\s*\)", sql):
        # Build the copy under a new name and rename it last: renaming the live table first would make
        # SQLite rewrite the other tables' REFERENCES inbound_items(...) to the old name.
        cols = ", ".join(r["name"] for r in con.execute("PRAGMA table_info(inbound_items)").fetchall())
        new_sql = re.sub(r",\s*UNIQUE\s*\(\s*document_id\s*,\s*master_item_id\s*\)", "", sql)
        new_sql = re.sub(r"^CREATE TABLE\s+\"?inbound_items\"?", "CREATE TABLE inbound_items_new", new_sql)
        con.execute("DROP TABLE IF EXISTS inbound_items_new")
        con.execute(new_sql)
        con.execute(f"INSERT INTO inbound_items_new ({cols}) SELECT {cols} FROM inbound_items")
        con.execute("DROP TABLE inbound_items")
        con.execute("ALTER TABLE inbound_items_new RENAME TO inbound_items")
        con.execute("CREATE INDEX IF NOT EXISTS idx_inbound_items_document_id ON inbound_items(document_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_inbound_items_master_item_id ON inbound_items(master_item_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_inbound_items_spo_item ON inbound_items(spo_item_id)")
    ibi_cols = {r["name"] for r in con.execute("PRAGMA table_info(inbound_items)").fetchall()}
    for col, ddl in (
        ("qty_bad", "INTEGER NOT NULL DEFAULT 0"), ("bad_condition", "TEXT"), ("bad_note", "TEXT"), ("check_note", "TEXT"),
        ("price_reason", "TEXT"), ("po_unit_price", "INTEGER"), ("is_over", "INTEGER NOT NULL DEFAULT 0"),
        ("over_approved_qty", "INTEGER NOT NULL DEFAULT 0"), ("over_cost_price", "INTEGER"),
    ):
        if col not in ibi_cols:
            con.execute(f"ALTER TABLE inbound_items ADD COLUMN {col} {ddl}")
    ib_cols = {r["name"] for r in con.execute("PRAGMA table_info(inbound_documents)").fetchall()}
    for col, ddl in (
        ("flow", "TEXT"), ("received_time", "TEXT"), ("arrival_method", "TEXT"), ("courier", "TEXT"), ("tracking_no", "TEXT"),
        ("package_count", "TEXT"), ("price_ack_by", "TEXT"), ("price_ack_at", "TEXT"), ("approval_state", "TEXT"),
        ("approval_requested_by", "TEXT"), ("approval_requested_at", "TEXT"), ("approval_note", "TEXT"),
        ("approval_by", "TEXT"), ("approval_at", "TEXT"), ("approval_reason", "TEXT"), ("recreated_from", "INTEGER"),
    ):
        if col not in ib_cols:
            con.execute(f"ALTER TABLE inbound_documents ADD COLUMN {col} {ddl}")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS inbound_document_pos (
            document_id INTEGER NOT NULL REFERENCES inbound_documents(id),
            supplier_po_id TEXT NOT NULL REFERENCES supplier_pos(id),
            PRIMARY KEY (document_id, supplier_po_id));
        CREATE INDEX IF NOT EXISTS idx_inbound_document_pos_po ON inbound_document_pos(supplier_po_id);
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_by TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS item_code_aliases (
            code TEXT PRIMARY KEY, master_item_id INTEGER NOT NULL REFERENCES master_items(id),
            created_at TEXT NOT NULL, created_by TEXT);
        CREATE TABLE IF NOT EXISTS item_code_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, master_item_id INTEGER NOT NULL REFERENCES master_items(id),
            old_code TEXT, new_code TEXT, reason TEXT, by_user TEXT, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS barcode_prints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, master_item_id INTEGER NOT NULL REFERENCES master_items(id),
            code TEXT NOT NULL, qty INTEGER NOT NULL, kind TEXT NOT NULL, inbound_document_id INTEGER,
            by_user TEXT, at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_barcode_prints_item ON barcode_prints(master_item_id);
        CREATE INDEX IF NOT EXISTS idx_barcode_prints_doc ON barcode_prints(inbound_document_id);
    """)
    mi_cols = {r["name"] for r in con.execute("PRAGMA table_info(master_items)").fetchall()}
    if "manual_code" not in mi_cols:
        con.execute("ALTER TABLE master_items ADD COLUMN manual_code TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_master_items_manual_code ON master_items(manual_code) WHERE manual_code IS NOT NULL")
    # A document made before multi-PO knew its PO only in the header; give it the link row too.
    con.execute("""INSERT OR IGNORE INTO inbound_document_pos (document_id, supplier_po_id)
                   SELECT id, supplier_po_id FROM inbound_documents WHERE supplier_po_id IS NOT NULL""")


def _next_gr_number(con: sqlite3.Connection, day_str: str) -> str:
    """GR-YYYYMMDD-NNN, shared by Goods Receipt documents and the PO receipts they write, so a number is never
    handed out twice. (Older Barang Masuk documents were numbered BM-/IN-… and keep that.)"""
    prefix = f"GR-{day_str}-"
    seq = 0
    for sql in ("SELECT doc_number AS n FROM inbound_documents WHERE doc_number LIKE ?",
                "SELECT gr_number AS n FROM spo_receipts WHERE gr_number LIKE ?"):
        for r in con.execute(sql, (f"{prefix}%",)).fetchall():
            tail = (r["n"] or "").rsplit("-", 1)[-1]
            if tail.isdigit():
                seq = max(seq, int(tail))
    return f"{prefix}{seq + 1:03d}"


def _next_doc_number(con: sqlite3.Connection, today_str: str) -> str:
    """BM-YYYYMMDD-NNN: an inbound document that is not a Goods Receipt from a PO (e.g. replacement goods of a
    Retur Supplier). Older documents were numbered IN-YYYYMMDD-NNNN and keep that."""
    prefix = f"BM-{today_str}-"
    row = con.execute(
        "SELECT doc_number FROM inbound_documents WHERE doc_number LIKE ? ORDER BY doc_number DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = int(row["doc_number"].rsplit("-", 1)[-1]) + 1 if row is not None else 1
    return f"{prefix}{seq:03d}"


def _inbound_event(con: sqlite3.Connection, document_id: int, kind: str, note: str | None, by: str | None) -> None:
    con.execute("INSERT INTO inbound_events (document_id, at, by_user, kind, note) VALUES (?, ?, ?, ?, ?)",
                (document_id, _now(), by, kind, note))


def _insert_inbound_items(con: sqlite3.Connection, document_id: int, items: list[dict]) -> None:
    con.executemany(
        """INSERT INTO inbound_items
           (document_id, master_item_id, pending_brand_id, pending_product_name, pending_unit,
            pending_sell_price, qty_in, cost_price, qty_remaining, received_date, spo_item_id, condition, location,
            spo_package_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)""",
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
                item.get("spo_item_id"),
                item.get("condition"),
                item.get("location"),
                item.get("spo_package_id") if item.get("spo_item_id") else None,
            )
            for item in items
        ],
    )


_INBOUND_META = ("supplier_po_id", "surat_jalan_no", "officer", "source", "manual_reason")


def create_inbound_document(
    supplier_id: int,
    received_date: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    items: list[dict],
    meta: dict | None = None,
    by: str | None = None,
) -> dict:
    created_at = _now()
    total_value = sum(i["qty_in"] * i["cost_price"] for i in items)
    with _conn() as con:
        today_str = date.today().strftime("%Y%m%d")
        doc_number = _next_doc_number(con, today_str)
        cur = con.execute(
            """INSERT INTO inbound_documents
               (doc_number, supplier_id, received_date, supplier_invoice_no, notes, status, total_value, created_at,
                created_by, supplier_po_id, surat_jalan_no, officer, source, manual_reason)
               VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_number, supplier_id, received_date, supplier_invoice_no, notes, total_value, created_at, by,
             *(((meta or {}).get(k) or None) for k in _INBOUND_META)),
        )
        document_id = cur.lastrowid
        _insert_inbound_items(con, document_id, items)
        _inbound_event(con, document_id, "created", "Draft disimpan", by)
        row = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(row)


def get_inbound_document(document_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT d.*, s.name AS supplier_name, sp.po_number AS supplier_po_number,
                      (SELECT COUNT(*) FROM inbound_items ii WHERE ii.document_id = d.id) AS item_count,
                      (SELECT COALESCE(SUM(ii.qty_in), 0) FROM inbound_items ii WHERE ii.document_id = d.id) AS qty_total
               FROM inbound_documents d
               JOIN suppliers s ON s.id = d.supplier_id
               LEFT JOIN supplier_pos sp ON sp.id = d.supplier_po_id WHERE d.id=?""",
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


def list_inbound_documents(
    status: str | None = None, supplier_id: int | None = None, q: str | None = None,
    date_from: str | None = None, date_to: str | None = None, supplier_po_id: str | None = None,
) -> list[dict]:
    with _conn() as con:
        query = """SELECT d.*, s.name AS supplier_name, sp.po_number AS supplier_po_number,
                          (SELECT COUNT(*) FROM inbound_items ii WHERE ii.document_id = d.id) AS item_count,
                          (SELECT COALESCE(SUM(ii.qty_in), 0) FROM inbound_items ii WHERE ii.document_id = d.id) AS qty_total
                   FROM inbound_documents d
                   JOIN suppliers s ON s.id = d.supplier_id
                   LEFT JOIN supplier_pos sp ON sp.id = d.supplier_po_id WHERE 1=1"""
        params: list = []
        if status:
            query += " AND d.status=?"
            params.append(status)
        if supplier_id is not None:
            query += " AND d.supplier_id=?"
            params.append(supplier_id)
        if supplier_po_id:
            query += " AND d.supplier_po_id=?"
            params.append(supplier_po_id)
        if date_from:
            query += " AND d.received_date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND d.received_date <= ?"
            params.append(date_to)
        if q:
            query += (" AND (d.doc_number LIKE ? OR s.name LIKE ? OR IFNULL(sp.po_number,'') LIKE ? OR IFNULL(d.surat_jalan_no,'') LIKE ? "
                      "OR d.id IN (SELECT ii.document_id FROM inbound_items ii JOIN master_items m ON m.id = ii.master_item_id "
                      "WHERE m.name LIKE ? OR m.sku LIKE ?))")
            params.extend([f"%{q}%"] * 6)
        query += " ORDER BY d.created_at DESC, d.id DESC"
        return [dict(r) for r in con.execute(query, params).fetchall()]


def review_inbound_document(document_id: int, by: str | None) -> dict:
    """"Item diperiksa": someone looked at the draft before it is posted. Editing the draft clears it."""
    with _conn() as con:
        row = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError("Hanya dokumen draft yang bisa diperiksa.")
        con.execute("UPDATE inbound_documents SET reviewed_by=?, reviewed_at=? WHERE id=?", (by, _now(), document_id))
        _inbound_event(con, document_id, "reviewed", "Dokumen diperiksa, siap diposting", by)
    return get_inbound_document(document_id)


def get_inbound_extras(document_id: int) -> dict:
    """Everything the detail page shows besides the document itself: audit trail, the PO's fulfilment,
    linked transactions, the stock movements this document made, and the supplier-invoice relation."""
    with _conn() as con:
        doc = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        events = [dict(r) for r in con.execute(
            "SELECT * FROM inbound_events WHERE document_id=? ORDER BY at ASC, id ASC", (document_id,))]
        out: dict = {"events": events, "po": None, "fulfilment": [], "customers": [], "mutations": [], "invoice": None}
        out["mutations"] = [dict(r) for r in con.execute(
            """SELECT l.id, l.master_item_id, m.sku, m.name, l.type, l.qty, l.balance_after, l.created_at
               FROM stock_ledger l JOIN master_items m ON m.id = l.master_item_id
               WHERE l.ref_type='inbound_document' AND l.ref_id=? ORDER BY l.id""", (document_id,))]
        if doc["supplier_po_id"]:
            po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (doc["supplier_po_id"],)).fetchone()
            if po is not None:
                items = _spo_items(con, po["id"])
                po_d = dict(po)
                out["po"] = {"id": po["id"], "po_number": po["po_number"], "status": po["status"],
                             "order_status": _spo_order_status(po_d),
                             "internal_invoice_no": po["internal_invoice_no"], "supplier_invoice_no": po["supplier_invoice_no"]}
                mine = {r["spo_item_id"] for r in con.execute(
                    "SELECT spo_item_id FROM inbound_items WHERE document_id=? AND spo_item_id IS NOT NULL", (document_id,))}
                out["fulfilment"] = [{
                    "item_id": i["id"], "product_name": i["product_name"], "sku": i.get("sku"),
                    "customer_po_number": i.get("customer_po_number"), "customer_name": i.get("customer_name"),
                    "plate_full": i.get("plate_full"), "ship_method": i.get("ship_method"), "item_status": i.get("item_status"),
                    "qty_ordered": i["quantity"] - i["qty_cancelled"], "qty_received": i["qty_received"],
                    "in_this_document": i["id"] in mine,
                } for i in items]
                seen: set = set()
                for i in items:
                    if i.get("customer_po_number") and i["customer_po_number"] not in seen and i.get("preorder_item_id"):
                        seen.add(i["customer_po_number"])
                        pr = con.execute("SELECT id, po_number, customer_name FROM preorders WHERE po_number=?",
                                         (i["customer_po_number"],)).fetchone()
                        if pr is not None:
                            out["customers"].append(dict(pr))
                money = _spo_money(po_d, items)
                out["invoice"] = {"internal_invoice_no": po["internal_invoice_no"], "supplier_invoice_no": po["supplier_invoice_no"],
                                  "grand_total": money["grand_total"], "paid": po["paid_amount"], "outstanding": money["outstanding"]}
        return out


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


def _spo_apply_inbound_receipt(con: sqlite3.Connection, header, lines: list[dict], by: str | None) -> list[str]:
    """Counts a posted Goods Receipt against its Purchase Order(s): one receipt record per PO (all sharing the GR
    number), qty_received / qty_bad, PO events and status. `lines` are the posted rows that count against a PO
    (never the excess). Returns the POs touched."""
    now = _now()
    doc_no = header["doc_number"] or ""
    gr_number = doc_no if doc_no.startswith("GR-") else _next_gr_number(con, header["received_date"].replace("-", ""))
    by_po: dict[str, list[dict]] = {}
    for i in lines:
        po_id = con.execute("SELECT supplier_po_id FROM supplier_po_items WHERE id=?", (i["spo_item_id"],)).fetchone()["supplier_po_id"]
        by_po.setdefault(po_id, []).append(i)
    for po_id, rows in by_po.items():
        cur = con.execute(
            """INSERT INTO spo_receipts (supplier_po_id, received_date, surat_jalan_no, warehouse_location, note,
                                         inbound_document_id, created_by, created_at, gr_number, officer)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (po_id, header["received_date"], header["surat_jalan_no"], header["notes"], header["id"], by, now, gr_number,
             header["officer"] or by),
        )
        total = 0
        for i in rows:
            bad = (i.get("condition") or "baik") in BAD_RECEIVE_CONDITIONS
            con.execute("UPDATE supplier_po_items SET qty_received = qty_received + ?, qty_bad = qty_bad + ? WHERE id=?",
                        (i["qty_in"], i["qty_in"] if bad else 0, i["spo_item_id"]))
            con.execute("INSERT INTO spo_receipt_lines (receipt_id, item_id, quantity, condition, location, package_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (cur.lastrowid, i["spo_item_id"], i["qty_in"], i.get("condition") or "baik", i.get("location"), i.get("spo_package_id")))
            total += i["qty_in"]
        _spo_finish_if_settled(con, po_id)
        _spo_event(con, po_id, "receive",
                   f"Barang diterima {gr_number} ({total} pcs) lewat Goods Receipt {doc_no}"
                   + (f", surat jalan {header['surat_jalan_no']}" if header["surat_jalan_no"] else ""), by)
        _spo_sync_all(con, po_id, by, now)
    return list(by_po)


def _po_net_price(spi) -> int:
    return spi["est_unit_price"] - round(spi["est_unit_price"] * (spi["discount_pct"] or 0) / 100)


def get_setting(key: str, default: str | None = None, con: sqlite3.Connection | None = None) -> str | None:
    if con is None:
        with _conn() as c:
            return get_setting(key, default, c)
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def set_setting(key: str, value: str, by: str | None) -> None:
    with _conn() as con:
        con.execute("INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (key, value, by, _now()))


GR_PRICE_TOLERANCE_KEY = "gr_price_tolerance_pct"
GR_PRICE_TOLERANCE_DEFAULT = 5.0


def gr_price_tolerance(con: sqlite3.Connection | None = None) -> float:
    try:
        return float(get_setting(GR_PRICE_TOLERANCE_KEY, str(GR_PRICE_TOLERANCE_DEFAULT), con))
    except ValueError:
        return GR_PRICE_TOLERANCE_DEFAULT


def gr_price_over_tolerance(po_price: int, actual: int, tolerance_pct: float) -> bool:
    """A GR price differing from the PO by more than the tolerance (either way) needs an Admin."""
    if actual == po_price:
        return False
    if po_price <= 0:
        return True
    return abs(actual - po_price) * 100 > tolerance_pct * po_price


def _gr_line_parts(line: dict, po_qty: int, over_price: int | None) -> list[dict]:
    """How one GR line lands as stock batches: the part that fills the PO and the excess, each split into good and
    bad. Bad units fill the PO part first. Every part keeps the PO line, so the excess still traces back to it."""
    bad = line.get("qty_bad") or 0
    bad_cond = line.get("bad_condition") or line.get("condition")
    if not bad and (line.get("condition") or "baik") in BAD_RECEIVE_CONDITIONS:
        bad, bad_cond = line["qty_in"], line["condition"]  # an older draft: the whole line has one condition
    over = line["qty_in"] - po_qty
    bad_po = min(bad, po_qty)
    bad_over = bad - bad_po
    parts = [
        {"qty_in": po_qty - bad_po, "condition": "baik", "is_over": 0, "cost_price": line["cost_price"]},
        {"qty_in": bad_po, "condition": bad_cond, "is_over": 0, "cost_price": line["cost_price"]},
        {"qty_in": over - bad_over, "condition": "baik", "is_over": 1, "cost_price": over_price},
        {"qty_in": bad_over, "condition": bad_cond, "is_over": 1, "cost_price": over_price},
    ]
    return [p for p in parts if p["qty_in"] > 0]


def _over_permission(accept_over: bool, over_reason: str | None, is_admin: bool) -> str | None:
    """K-11: goods beyond what the Purchase Order still expects are refused unless an admin accepts them as
    general stock and says why. Returns the reason (None when not accepting)."""
    if not accept_over:
        return None
    if not is_admin:
        raise ValueError("Hanya admin yang boleh menerima barang kelebihan (melebihi sisa PO).")
    reason = (over_reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan wajib diisi (minimal 3 karakter) untuk menerima barang kelebihan.")
    return reason


def post_inbound_document(document_id: int, by: str | None = None, accept_over: bool = False,
                          over_reason: str | None = None, is_admin: bool = False) -> dict:
    over_ok = _over_permission(accept_over, over_reason, is_admin)
    with _conn() as con:
        # Take the write lock before reading, so two posts of the same draft can't both see 'draft'.
        con.execute("BEGIN IMMEDIATE")
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if header["status"] != "draft":
            raise ValueError(
                f"Dokumen barang masuk {document_id} tidak bisa diposting: statusnya '{header['status']}', hanya dokumen draft yang bisa diposting"
            )
        if header["source"] == "manual" and not header["reviewed_at"]:
            raise ValueError("Barang Masuk manual (tanpa PO) harus diperiksa dulu sebelum diposting.")
        is_gr = header["flow"] == "gr"
        if is_gr and not header["reviewed_at"]:
            raise ValueError("Selesaikan Periksa Barang (qty baik / rusak) sebelum posting.")

        # Step 2: re-validate received_date, even though it may have already been
        # checked at creation time (it can go stale between draft creation and posting).
        received_date = header["received_date"]
        try:
            rd = date.fromisoformat(received_date)
        except ValueError:
            raise ValueError(f"Tanggal terima '{received_date}' pada dokumen {document_id} tidak valid")
        today = date.today()
        if rd > today:
            raise ValueError(f"Tanggal terima {received_date} ada di masa depan")
        if (today - rd).days > 30:
            raise ValueError(f"Tanggal terima {received_date} lebih dari 30 hari yang lalu")

        # Step 3: fetch line items, reject if empty.
        items = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=? ORDER BY id", (document_id,)).fetchall()]
        if not items:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak bisa diposting: tidak ada baris barang")

        # Each PO line is checked against ITS PO (a GR may cover several POs of the supplier): the PO part may not
        # exceed what the PO still expects, the excess needs an Admin (K-11), a price off the PO needs a reason and
        # a confirmation, and beyond the tolerance an Admin.
        approved = header["approval_state"] == "approved"
        tolerance = gr_price_tolerance(con)
        over_notes: list[str] = []
        price_notes: list[str] = []
        plans: list[tuple[dict, int, int | None]] = []
        for i in items:
            if not i.get("spo_item_id"):
                plans.append((i, i["qty_in"], None))
                continue
            spi = con.execute(
                """SELECT spi.*, sp.status AS po_status, sp.po_number FROM supplier_po_items spi
                   JOIN supplier_pos sp ON sp.id = spi.supplier_po_id WHERE spi.id=?""", (i["spo_item_id"],)).fetchone()
            if spi is None:
                raise ValueError("Baris Purchase Order pada dokumen ini tidak ditemukan.")
            if spi["po_status"] not in ("dipesan", "sebagian_diterima"):
                raise ValueError(f"Purchase Order {spi['po_number']} belum diterbitkan atau sudah selesai/dibatalkan.")
            left = max(spi["quantity"] - spi["qty_cancelled"] - spi["qty_received"], 0)
            po_qty = min(i["qty_in"], left)
            over = i["qty_in"] - po_qty
            over_price = None
            if over > 0:
                if approved and (i.get("over_approved_qty") or 0) >= over and i.get("over_cost_price") is not None:
                    over_price = i["over_cost_price"]
                elif over_ok:
                    over_price = i["cost_price"]
                elif approved and (i.get("over_approved_qty") or 0) > 0:
                    raise ValueError(f"Sisa PO untuk “{spi['product_name']}” berubah sejak disetujui (kelebihan kini {over} pcs). "
                                     "Minta persetujuan Admin lagi.")
                else:
                    raise ValueError(f"Jumlah untuk “{spi['product_name']}” melebihi sisa pesanan PO ({left}). "
                                     "Kelebihan hanya bisa diterima dengan persetujuan Admin.")
                over_notes.append(f"{over} pcs “{spi['product_name']}” ({spi['po_number']}) @ Rp {over_price:,}".replace(",", "."))
            po_price = _po_net_price(spi)
            if is_gr and i["cost_price"] != po_price:
                if not (i.get("price_reason") or "").strip():
                    raise ValueError(f"Isi alasan perubahan harga untuk “{spi['product_name']}”.")
                if not header["price_ack_at"]:
                    raise ValueError("Konfirmasi selisih harga terhadap PO dulu sebelum posting.")
                if gr_price_over_tolerance(po_price, i["cost_price"], tolerance) and not approved:
                    raise ValueError(f"Harga “{spi['product_name']}” berbeda lebih dari {tolerance:g}% dari PO. "
                                     "Perlu persetujuan Admin sebelum posting.")
                price_notes.append(f"{spi['product_name']}: PO Rp {po_price:,} → aktual Rp {i['cost_price']:,} "
                                   f"({i['cost_price'] - po_price:+,}), alasan: {i['price_reason']}".replace(",", "."))
            if po_qty > 0:
                i["spo_package_id"] = _spo_check_receive_package(
                    con, spi["supplier_po_id"], i["spo_item_id"], i.get("spo_package_id"), po_qty)
            else:
                i["spo_package_id"] = None
            plans.append((i, po_qty, over_price))

        # Step 4: flip status to posted FIRST — the avg-cost recompute below filters
        # on d.status='posted' and must see this document's own new rows.
        if con.execute("UPDATE inbound_documents SET status='posted' WHERE id=? AND status='draft'",
                       (document_id,)).rowcount != 1:
            raise ValueError(f"Dokumen barang masuk {document_id} sudah diposting.")

        # Step 4b: a line becomes homogeneous batches (PO part / excess × good / bad). The first part keeps the
        # line's row; the rest are copies of it.
        for i, po_qty, over_price in plans:
            if not i.get("spo_item_id") and not (i.get("qty_bad") or 0):
                continue  # nothing to split on a line outside a PO with one condition
            parts = _gr_line_parts(i, po_qty if i.get("spo_item_id") else i["qty_in"], over_price)
            first, rest = parts[0], parts[1:]
            con.execute("UPDATE inbound_items SET qty_in=?, condition=?, is_over=?, cost_price=?, spo_package_id=? WHERE id=?",
                        (first["qty_in"], first["condition"], first["is_over"], first["cost_price"],
                         i["spo_package_id"] if not first["is_over"] else None, i["id"]))
            for part in rest:
                con.execute(
                    """INSERT INTO inbound_items (document_id, master_item_id, qty_in, cost_price, spo_item_id, spo_package_id,
                           condition, location, qty_bad, bad_condition, bad_note, check_note, price_reason, po_unit_price,
                           is_over, over_approved_qty, over_cost_price)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (document_id, i["master_item_id"], part["qty_in"], part["cost_price"], i.get("spo_item_id"),
                     i["spo_package_id"] if not part["is_over"] else None, part["condition"], i.get("location"),
                     i.get("qty_bad") or 0, i.get("bad_condition"), i.get("bad_note"), i.get("check_note"),
                     i.get("price_reason"), i.get("po_unit_price"), part["is_over"], i.get("over_approved_qty") or 0,
                     i.get("over_cost_price")))
        items = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=? ORDER BY id", (document_id,)).fetchall()]

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
                        raise ValueError(f"Supplier {header['supplier_id']} tidak ditemukan.")
                    brand_row = con.execute("SELECT code FROM brands WHERE id=?", (brand_id,)).fetchone()
                    if brand_row is None:
                        raise ValueError(f"Merek {brand_id} tidak ditemukan.")
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
        # becomes a real batch. A line received damaged / wrong is not a sellable batch: it goes to
        # Stok Karantina instead (decision 3), so it adds nothing to stock or to the average cost.
        for item in items:
            item["bad"] = (item.get("condition") or "baik") in BAD_RECEIVE_CONDITIONS
            con.execute(
                "UPDATE inbound_items SET qty_remaining=?, received_date=? WHERE id=?",
                (0 if item["bad"] else item["qty_in"], received_date, item["id"]),
            )
            if item["bad"]:
                mi_row = con.execute("SELECT name FROM master_items WHERE id=?", (item["master_item_id"],)).fetchone()
                _quarantine_in(
                    con, master_item_id=item["master_item_id"], product_name=mi_row["name"] if mi_row else "?",
                    qty=item["qty_in"], condition=item["condition"], source_type="barang_masuk",
                    source_ref=header["doc_number"], source_id=str(document_id), supplier_id=header["supplier_id"],
                    unit_cost=item["cost_price"], candidate=True, note=None, by=by)

        # Step 7+8: recompute costing per distinct master item touched, write ledger.
        touched_ids = sorted(set(item["master_item_id"] for item in items if not item["bad"]))
        for master_item_id in touched_ids:
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
            doc_items_for_mi = [i for i in items if i["master_item_id"] == master_item_id and not i["bad"]]
            qty_in_total = sum(i["qty_in"] for i in doc_items_for_mi)
            # The excess may be priced differently (even free); the last purchase price is the PO part's.
            this_doc_cost_price = ([i for i in doc_items_for_mi if not i.get("is_over")] or doc_items_for_mi)[-1]["cost_price"]

            costing = _recompute_master_item_costing(con, master_item_id)
            new_stock_qty = mi["stock_qty"] + qty_in_total
            first_received = mi["first_received_date"] or received_date

            con.execute(
                "UPDATE master_items SET avg_cost_price=?, stock_qty=?, last_cost_price=?, first_received_date=? WHERE id=?",
                (costing["avg_cost_price"], new_stock_qty, this_doc_cost_price, first_received, master_item_id),
            )
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'IN', ?, ?, 'inbound_document', ?, NULL, ?, ?)""",
                (master_item_id, qty_in_total, new_stock_qty, document_id, _now(), by),
            )

        # Step 9: recompute total_value from the actual posted items.
        total_row = con.execute(
            "SELECT SUM(qty_in * cost_price) AS total FROM inbound_items WHERE document_id=?", (document_id,)
        ).fetchone()
        con.execute(
            "UPDATE inbound_documents SET total_value=? WHERE id=?", (total_row["total"] or 0, document_id)
        )

        # A document opened from the PO page already counted its receipt (GR) there; one made in
        # Barang Masuk counts it now, when the goods really become stock. The excess never counts.
        po_rows = [i for i in items if i.get("spo_item_id") and not i.get("is_over")]
        touched_pos: list[str] = []
        if po_rows and not con.execute("SELECT 1 FROM spo_receipts WHERE inbound_document_id=?", (document_id,)).fetchone():
            touched_pos = _spo_apply_inbound_receipt(con, header, po_rows, by)
        con.execute("UPDATE inbound_documents SET posted_by=?, posted_at=? WHERE id=?", (by, _now(), document_id))
        if header["supplier_po_id"] and header["supplier_po_id"] not in touched_pos:
            # K-10: the goods are stock now, so the Sales Order lines they were bought for can move on.
            _spo_sync_all(con, header["supplier_po_id"], by, _now())
        # ...and a 'Stok Tersedia' line still waiting for shelf stock of these items may be covered now.
        _promote_shelf_ready_lines(con, by, master_item_ids=touched_ids)
        if over_notes:
            who = f"disetujui {header['approval_by']}: {header['approval_reason']}" if approved else f"Izin admin: {over_ok}"
            note = "Kelebihan dari PO diterima sebagai stok umum: " + ", ".join(over_notes) + f". {who}"
            for po_id in {con.execute("SELECT supplier_po_id FROM supplier_po_items WHERE id=?", (i["spo_item_id"],)).fetchone()[0]
                          for i in items if i.get("is_over")}:
                _spo_event(con, po_id, "over", f"{note} (lewat {header['doc_number']})", by)
            _inbound_event(con, document_id, "over", note, by)
        if price_notes:
            _inbound_event(con, document_id, "price", "Harga beli aktual berbeda dari PO: " + "; ".join(price_notes), by)
        bad_total = sum(i["qty_in"] for i in items if i["bad"])
        _inbound_event(con, document_id, "posted",
                       f"Posting berhasil, stok bertambah {sum(i['qty_in'] for i in items if not i['bad'])} pcs"
                       + (f"; {bad_total} pcs rusak/tidak sesuai masuk Stok Karantina" if bad_total else ""), by)
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


def _reverse_doc_po_receipts(con: sqlite3.Connection, header, by: str | None, why: str) -> None:
    """Takes back every PO receipt this document counted (one per PO for a multi-PO GR), with the damaged goods
    those receipts put in quarantine, so each PO shows again what is really in."""
    for rcpt in con.execute("SELECT * FROM spo_receipts WHERE inbound_document_id=?", (header["id"],)).fetchall():
        _quarantine_cancel_source(con, "terima_barang", str(rcpt["id"]), by,
                                  f"Penerimaan {rcpt['gr_number'] or ''} dibatalkan ({why})")
        for ln in con.execute("SELECT item_id, quantity, condition FROM spo_receipt_lines WHERE receipt_id=?", (rcpt["id"],)).fetchall():
            bad = (ln["condition"] or "baik") in BAD_RECEIVE_CONDITIONS
            con.execute("UPDATE supplier_po_items SET qty_received = MAX(qty_received - ?, 0), qty_bad = MAX(qty_bad - ?, 0) WHERE id=?",
                        (ln["quantity"], ln["quantity"] if bad else 0, ln["item_id"]))
        _spo_finish_if_settled(con, rcpt["supplier_po_id"])
        _spo_event(con, rcpt["supplier_po_id"], "receive", f"Penerimaan {rcpt['gr_number'] or ''} dibatalkan ({why})", by)
        _spo_sync_all(con, rcpt["supplier_po_id"], by, _now())


def inbound_document_po_ids(con: sqlite3.Connection, document_id: int) -> list[str]:
    """Every PO a document refers to: the ones picked for it and the ones its lines belong to."""
    rows = con.execute(
        """SELECT supplier_po_id FROM inbound_document_pos WHERE document_id=?
           UNION SELECT spi.supplier_po_id FROM inbound_items ii JOIN supplier_po_items spi ON spi.id = ii.spo_item_id
                 WHERE ii.document_id=?
           UNION SELECT supplier_po_id FROM inbound_documents WHERE id=? AND supplier_po_id IS NOT NULL""",
        (document_id, document_id, document_id)).fetchall()
    return [r[0] for r in rows]


def void_inbound_document(document_id: int, reason: str, by: str | None = None) -> dict:
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if header["status"] == "void":
            raise ValueError(f"Dokumen barang masuk {document_id} sudah dibatalkan")

        if header["status"] == "draft":
            _inbound_event(con, document_id, "void", f"Dibatalkan: {reason}", by)
            # A draft opened by the old PO-page "Terima Barang" was already counted on its PO there. Cancelling it
            # must take that count back too, or the PO would show goods as received that never became stock.
            _reverse_doc_po_receipts(con, header, by, f"draft {header['doc_number']} dibatalkan")
            con.execute("UPDATE inbound_documents SET status='void' WHERE id=?", (document_id,))
            updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
            return dict(updated)

        # Posted void: a correction of what the shop already relies on, so it is refused while anything downstream
        # still uses this receipt.
        po_ids = inbound_document_po_ids(con, document_id)
        if po_ids:
            inv = con.execute(
                f"SELECT invoice_number FROM purchase_invoices WHERE status='posted' AND supplier_po_id IN ({','.join('?' * len(po_ids))}) LIMIT 1",
                po_ids).fetchone()
            if inv is not None:
                raise ValueError(f"Dokumen {header['doc_number']} tidak bisa dibatalkan: barangnya sudah ditagih di Faktur Pembelian "
                                 f"{inv['invoice_number']} yang sudah diposting. Void faktur itu dulu.")
        ret = con.execute(
            """SELECT r.return_number FROM supplier_return_items i JOIN supplier_returns r ON r.id = i.return_id
               JOIN inbound_items ii ON ii.id = i.inbound_item_id
               WHERE ii.document_id=? AND r.doc_status<>'dibatalkan' LIMIT 1""", (document_id,)).fetchone()
        if ret is not None:
            raise ValueError(f"Dokumen {header['doc_number']} tidak bisa dibatalkan: dipakai di Retur Supplier {ret['return_number']}.")
        _inbound_event(con, document_id, "void", f"Dibatalkan: {reason}", by)

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
        # qty_remaining is never drawn down by sales, so check the live stock instead: taking this
        # document's quantity back out must not leave any item below zero.
        for item in items:
            item["bad"] = (item.get("condition") or "baik") in BAD_RECEIVE_CONDITIONS
        _quarantine_cancel_source(con, "barang_masuk", str(document_id), by, f"Barang Masuk {header['doc_number']} dibatalkan: {reason}")
        taking: dict[int, int] = {}
        for item in items:
            if not item["bad"]:
                taking[item["master_item_id"]] = taking.get(item["master_item_id"], 0) + item["qty_in"]
        for item in items:
            mid = item["master_item_id"]
            if mid not in taking:
                continue
            stock = con.execute("SELECT stock_qty FROM master_items WHERE id=?", (mid,)).fetchone()["stock_qty"]
            if stock < taking[mid]:
                raise ValueError(
                    f"Dokumen {document_id} tidak bisa dibatalkan: stok SKU {item['sku']} tinggal {stock}, "
                    f"sedangkan dokumen ini memasukkan {taking[mid]} (sebagian sudah terjual/terpakai). "
                    "Koreksi lewat Stock Opname atau penyesuaian stok."
                )
            del taking[mid]

        for item in items:
            master_item_id = item["master_item_id"]
            con.execute("UPDATE inbound_items SET qty_remaining=0 WHERE id=?", (item["id"],))
            if item["bad"]:
                continue  # it never entered stock; its quarantine lot was cancelled above
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
            new_stock_qty = mi["stock_qty"] - item["qty_in"]
            costing = _recompute_master_item_costing(con, master_item_id)
            con.execute(
                "UPDATE master_items SET stock_qty=?, avg_cost_price=? WHERE id=?",
                (new_stock_qty, costing["avg_cost_price"], master_item_id),
            )
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'VOID', ?, ?, 'inbound_document', ?, ?, ?, ?)""",
                (master_item_id, -item["qty_in"], new_stock_qty, document_id, reason, _now(), by),
            )

        # The receipts this document counted against its PO(s) are taken back too.
        _reverse_doc_po_receipts(con, header, by, f"Barang Masuk {header['doc_number']} void")

        con.execute("UPDATE inbound_documents SET status='void' WHERE id=?", (document_id,))
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


# --- Stok Karantina ------------------------------------------------------

QUARANTINE_CONDITIONS = {"rusak": "Rusak", "tidak_sesuai": "Tidak sesuai", "perlu_cek": "Perlu dicek"}
QUARANTINE_SOURCES = {
    "terima_barang": "Terima Barang PO", "barang_masuk": "Barang Masuk", "retur_customer": "Retur Pelanggan",
    "manual": "Dipindah dari stok", "retur_supplier": "Disiapkan untuk Retur Supplier",
    "retur_ditolak": "Ditolak supplier, kembali ke toko",
}
# Receiving conditions that keep goods off the sellable shelf.
BAD_RECEIVE_CONDITIONS = ("rusak", "tidak_sesuai")
WRITEOFF_OPEN = ("diajukan", "direkomendasikan")


def _q_event(con: sqlite3.Connection, lot_id: int, action: str, qty: int, note: str | None, by: str | None) -> None:
    con.execute("INSERT INTO quarantine_events (lot_id, action, qty, note, by_user, at) VALUES (?, ?, ?, ?, ?, ?)",
                (lot_id, action, qty, note, by, _now()))


def _quarantine_in(
    con: sqlite3.Connection, *, master_item_id: int | None, product_name: str, qty: int, condition: str,
    source_type: str, source_ref: str | None, source_id: str | None, supplier_id: int | None, unit_cost: int,
    candidate: bool, note: str | None, by: str | None,
) -> int:
    """Puts `qty` units into quarantine as one lot. Never touches stock_qty: the goods were never sellable
    (receiving) or are taken off the shelf by the caller (manual)."""
    if qty <= 0:
        return 0
    cur = con.execute(
        """INSERT INTO quarantine_lots (master_item_id, product_name, supplier_id, quantity, qty_left, unit_cost,
               condition, source_type, source_ref, source_id, retur_candidate, note, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (master_item_id, product_name, supplier_id, qty, qty, unit_cost or 0, condition, source_type, source_ref,
         source_id, 1 if candidate else 0, note, by, _now()),
    )
    if master_item_id is not None:
        con.execute("UPDATE master_items SET quarantine_qty = quarantine_qty + ? WHERE id=?", (qty, master_item_id))
    _q_event(con, cur.lastrowid, "masuk", qty,
             f"Masuk karantina ({QUARANTINE_CONDITIONS.get(condition, condition)}) dari "
             f"{QUARANTINE_SOURCES.get(source_type, source_type)}{' ' + source_ref if source_ref else ''}", by)
    return cur.lastrowid


def _q_lot(con: sqlite3.Connection, lot_id: int) -> dict:
    row = con.execute("SELECT * FROM quarantine_lots WHERE id=?", (lot_id,)).fetchone()
    if row is None:
        raise LookupError("Barang karantina tidak ditemukan.")
    return dict(row)


def _q_pending(con: sqlite3.Connection, lot_id: int, excluding: int | None = None) -> int:
    """Units of a lot already spoken for: open write-off requests plus active supplier returns."""
    return con.execute(
        f"SELECT COALESCE(SUM(qty), 0) AS n FROM quarantine_writeoffs WHERE lot_id=? AND status IN ({_placeholders(WRITEOFF_OPEN)})"
        + (" AND id<>?" if excluding else ""),
        (lot_id, *WRITEOFF_OPEN, *([excluding] if excluding else [])),
    ).fetchone()["n"] + _q_return_hold(con, lot_id)


def _q_return_hold(con: sqlite3.Connection, lot_id: int, excluding_return: int | None = None) -> int:
    """Units of a lot on a supplier return that is submitted or approved but whose goods have not left yet."""
    return con.execute(
        """SELECT COALESCE(SUM(i.qty), 0) AS n FROM supplier_return_items i JOIN supplier_returns r ON r.id = i.return_id
           WHERE i.quarantine_lot_id=? AND r.doc_status IN ('menunggu_persetujuan', 'diproses')
             AND r.handover_status IN ('belum', 'siap', 'menunggu_ambil') AND r.id<>?""",
        (lot_id, excluding_return or 0),
    ).fetchone()["n"]


def _quarantine_take(con: sqlite3.Connection, lot: dict, qty: int, action: str, note: str | None, by: str | None) -> None:
    """Takes `qty` out of a lot (released, written off, cancelled). The caller decides where it goes."""
    con.execute("UPDATE quarantine_lots SET qty_left = qty_left - ? WHERE id=? AND qty_left >= ?", (qty, lot["id"], qty))
    if lot["master_item_id"] is not None:
        con.execute("UPDATE master_items SET quarantine_qty = MAX(quarantine_qty - ?, 0) WHERE id=?",
                    (qty, lot["master_item_id"]))
    _q_event(con, lot["id"], action, qty, note, by)


def _quarantine_cancel_source(con: sqlite3.Connection, source_type: str, source_id: str, by: str | None, why: str) -> None:
    """The document that put goods into quarantine is undone: its lots go too, as long as nothing was
    done with them yet."""
    for lot in [dict(r) for r in con.execute(
            "SELECT * FROM quarantine_lots WHERE source_type=? AND source_id=?", (source_type, source_id))]:
        if lot["qty_left"] != lot["quantity"] or _q_pending(con, lot["id"]):
            raise ValueError(
                f"Barang karantina “{lot['product_name']}” dari dokumen ini sudah diproses (dilepas / dihapus / diajukan hapus). "
                "Selesaikan di halaman Stok Karantina dulu."
            )
        if lot["qty_left"]:
            _quarantine_take(con, lot, lot["qty_left"], "batal", why, by)


def _q_row(con: sqlite3.Connection, lot: dict) -> dict:
    pending = _q_pending(con, lot["id"])
    lot["pending_writeoff_qty"] = pending
    lot["free_qty"] = lot["qty_left"] - pending
    lot["cost_value"] = lot["qty_left"] * (lot["unit_cost"] or 0)
    lot["condition_label"] = QUARANTINE_CONDITIONS.get(lot["condition"], lot["condition"])
    lot["source_label"] = QUARANTINE_SOURCES.get(lot["source_type"], lot["source_type"])
    return lot


_Q_SELECT = """SELECT q.*, mi.sku, mi.unit, mi.item_condition, s.name AS supplier_name FROM quarantine_lots q
               LEFT JOIN master_items mi ON mi.id = q.master_item_id
               LEFT JOIN suppliers s ON s.id = q.supplier_id"""


def list_quarantine(view: str = "aktif", query: str | None = None) -> dict:
    """view: aktif (still in quarantine) | kandidat (active and marked for a supplier return) | selesai."""
    where, params = [], []
    if view == "aktif":
        where.append("q.qty_left > 0")
    elif view == "kandidat":
        where.append("q.qty_left > 0 AND q.retur_candidate = 1")
    elif view == "selesai":
        where.append("q.qty_left = 0")
    if query:
        like = f"%{query.strip()}%"
        where.append("(q.product_name LIKE ? OR mi.sku LIKE ? OR q.source_ref LIKE ? OR s.name LIKE ?)")
        params += [like] * 4
    with _conn() as con:
        rows = [_q_row(con, dict(r)) for r in con.execute(
            _Q_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY q.created_at DESC, q.id DESC", params)]
        active = [dict(r) for r in con.execute("SELECT qty_left, unit_cost, retur_candidate FROM quarantine_lots WHERE qty_left > 0")]
        open_wo = con.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(value), 0) AS v FROM quarantine_writeoffs WHERE status IN ({_placeholders(WRITEOFF_OPEN)})",
            WRITEOFF_OPEN).fetchone()
    return {
        "items": rows,
        "summary": {
            "lots": len(active), "qty": sum(a["qty_left"] for a in active),
            "cost_value": sum(a["qty_left"] * a["unit_cost"] for a in active),
            "candidates": sum(1 for a in active if a["retur_candidate"]),
            "writeoffs_open": open_wo["n"], "writeoffs_cost_value": open_wo["v"],
        },
    }


def get_quarantine_lot(lot_id: int) -> dict:
    with _conn() as con:
        row = con.execute(_Q_SELECT + " WHERE q.id=?", (lot_id,)).fetchone()
        if row is None:
            raise LookupError("Barang karantina tidak ditemukan.")
        lot = _q_row(con, dict(row))
        lot["events"] = [dict(r) for r in con.execute(
            "SELECT * FROM quarantine_events WHERE lot_id=? ORDER BY at ASC, id ASC", (lot_id,))]
        lot["writeoffs"] = [dict(r) for r in con.execute(
            "SELECT id, lot_id, qty, value AS cost_value, reason, status, requested_by, requested_at, recommended_by, "
            "recommended_at, recommend_note, decided_by, decided_at, decision_note FROM quarantine_writeoffs "
            "WHERE lot_id=? ORDER BY requested_at DESC, id DESC", (lot_id,))]
    return lot


def list_quarantine_writeoffs(status: str | None = None) -> list[dict]:
    with _conn() as con:
        sql = """SELECT w.id, w.lot_id, w.qty, w.value AS cost_value, w.reason, w.status, w.requested_by, w.requested_at,
                        w.recommended_by, w.recommended_at, w.recommend_note, w.decided_by, w.decided_at, w.decision_note,
                        q.product_name, q.condition, q.source_type, q.source_ref, q.unit_cost, mi.sku, s.name AS supplier_name
                 FROM quarantine_writeoffs w JOIN quarantine_lots q ON q.id = w.lot_id
                 LEFT JOIN master_items mi ON mi.id = q.master_item_id LEFT JOIN suppliers s ON s.id = q.supplier_id"""
        if status == "terbuka":
            rows = con.execute(sql + f" WHERE w.status IN ({_placeholders(WRITEOFF_OPEN)}) ORDER BY w.requested_at ASC", WRITEOFF_OPEN)
        else:
            rows = con.execute(sql + " ORDER BY w.requested_at DESC LIMIT 200")
        return [dict(r) for r in rows]


def _q_reason(reason: str | None, what: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError(f"Alasan {what} wajib diisi (minimal 3 karakter).")
    return reason


def quarantine_from_stock(master_item_id: int, qty: int, condition: str, note: str | None, by: str | None) -> dict:
    """Admin moves sellable stock into quarantine (found damaged on the shelf)."""
    if condition not in QUARANTINE_CONDITIONS:
        raise ValueError("Kondisi harus Rusak, Tidak sesuai, atau Perlu dicek.")
    note = _q_reason(note, "karantina")
    if qty <= 0:
        raise ValueError("Jumlah harus lebih dari 0.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        mi = con.execute("SELECT * FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if mi is None:
            raise LookupError("Barang tidak ditemukan.")
        if mi["stock_qty"] < qty:
            raise ValueError(f"Stok {mi['name']} hanya {mi['stock_qty']}.")
        new_qty = mi["stock_qty"] - qty
        con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_qty, master_item_id))
        lot_id = _quarantine_in(
            con, master_item_id=master_item_id, product_name=mi["name"], qty=qty, condition=condition,
            source_type="manual", source_ref=None, source_id=None, supplier_id=mi["supplier_id"],
            unit_cost=mi["avg_cost_price"] or mi["last_cost_price"] or 0, candidate=False, note=note, by=by)
        con.execute(
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
               VALUES (?, 'KARANTINA', ?, ?, 'quarantine', ?, ?, ?, ?)""",
            (master_item_id, -qty, new_qty, lot_id, f"Dipindah ke karantina: {note}", _now(), by),
        )
    return get_quarantine_lot(lot_id)


def release_quarantine(lot_id: int, qty: int, reason: str, by: str | None) -> dict:
    """Admin: the goods passed inspection and go back to sellable stock."""
    reason = _q_reason(reason, "pelepasan")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        lot = _q_lot(con, lot_id)
        if lot["master_item_id"] is None:
            raise ValueError("Barang ini tidak tertaut ke Master Barang, jadi tidak bisa masuk stok.")
        free = lot["qty_left"] - _q_pending(con, lot_id)
        if qty <= 0 or qty > free:
            raise ValueError(f"Jumlah yang bisa dilepas 1 sampai {free}.")
        _quarantine_take(con, lot, qty, "lepas", f"Lolos pemeriksaan, kembali ke stok: {reason}", by)
        mi = con.execute("SELECT stock_qty FROM master_items WHERE id=?", (lot["master_item_id"],)).fetchone()
        new_qty = mi["stock_qty"] + qty
        con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_qty, lot["master_item_id"]))
        con.execute(
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
               VALUES (?, 'LEPAS_KARANTINA', ?, ?, 'quarantine', ?, ?, ?, ?)""",
            (lot["master_item_id"], qty, new_qty, lot_id, f"Lepas karantina: {reason}", _now(), by),
        )
        _promote_shelf_ready_lines(con, by, master_item_ids=[lot["master_item_id"]])
    return get_quarantine_lot(lot_id)


SECONDHAND_SKU_SUFFIX = "-BKS"


def quarantine_to_secondhand(lot_id: int, qty: int, sell_price: int, note: str, user: dict) -> dict:
    """Retur Supplier #9 (final 19-10-2026): an Admin inspected quarantined goods and finds them sellable as
    second-hand. They move to the item's BEKAS variant: a separate master item (SKU + '-BKS', condition
    BEKAS, origin_item_id = the new item), created on first use. The lot's value moves with them at its
    unit cost, so inventory value is not counted twice. Only an Admin sets the BEKAS selling price."""
    if (user or {}).get("role") != "admin":
        raise PermissionError("Hanya admin yang bisa menjadikan barang bekas.")
    note = _q_reason(note, "hasil pemeriksaan")
    by = user.get("username")
    sell_price = int(sell_price or 0)
    if sell_price <= 0:
        raise ValueError("Harga jual barang bekas wajib diisi.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        lot = _q_lot(con, lot_id)
        if lot["master_item_id"] is None:
            raise ValueError("Barang ini tidak tertaut ke Master Barang. Tautkan dulu sebelum dijadikan barang bekas.")
        free = lot["qty_left"] - _q_pending(con, lot_id)
        if qty <= 0 or qty > free:
            raise ValueError(f"Jumlah yang bisa dijadikan bekas 1 sampai {free}.")
        origin = dict(con.execute("SELECT * FROM master_items WHERE id=?", (lot["master_item_id"],)).fetchone())
        if origin["item_condition"] == "bekas":
            raise ValueError("Barang ini sudah varian BEKAS. Gunakan Lepas ke Stok bila lolos pemeriksaan.")
        variant = con.execute("SELECT * FROM master_items WHERE origin_item_id=? AND item_condition='bekas'", (origin["id"],)).fetchone()
        now = _now()
        if variant is None:
            sku = f"{origin['sku']}{SECONDHAND_SKU_SUFFIX}"
            if con.execute("SELECT 1 FROM master_items WHERE sku=?", (sku,)).fetchone():
                raise ValueError(f"SKU {sku} sudah dipakai barang lain. Ubah SKU itu dulu.")
            name = f"{origin['name']} (BEKAS)"
            vid = con.execute(
                """INSERT INTO master_items (sku, sku_prefix, sku_seq, supplier_id, brand_id, name, name_normalized, unit, last_cost_price,
                       avg_cost_price, sell_price, stock_qty, first_received_date, is_active, created_at, description, min_stock, location,
                       updated_at, category_id, updated_by, item_condition, origin_item_id)
                   VALUES (?, ?, 0, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, ?, ?, 0, ?, ?, ?, ?, 'bekas', ?)""",
                (sku, sku, origin["supplier_id"], origin["brand_id"], name, _normalize_name(name), origin["unit"], sell_price,
                 shop_today().isoformat(), now, f"Barang bekas dari {origin['sku']} {origin['name']}", origin["location"], now,
                 origin["category_id"], by, origin["id"])).lastrowid
            variant = dict(con.execute("SELECT * FROM master_items WHERE id=?", (vid,)).fetchone())
        else:
            variant = dict(variant)
        unit_cost = lot["unit_cost"] or 0
        _quarantine_take(con, lot, qty, "bekas", f"Lolos pemeriksaan sebagai barang bekas → {variant['sku']}: {note}", by)
        old_qty, old_avg = max(variant["stock_qty"], 0), variant["avg_cost_price"] or 0
        new_qty = variant["stock_qty"] + qty
        new_avg = round((old_qty * old_avg + qty * unit_cost) / (old_qty + qty)) if old_qty + qty else unit_cost
        con.execute("""UPDATE master_items SET stock_qty=?, avg_cost_price=?, last_cost_price=?, sell_price=?, is_active=1,
                           updated_at=?, updated_by=? WHERE id=?""",
                    (new_qty, new_avg, unit_cost, sell_price, now, by, variant["id"]))
        con.execute(
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
               VALUES (?, 'BEKAS_MASUK', ?, ?, 'quarantine', ?, ?, ?, ?)""",
            (variant["id"], qty, new_qty, lot_id,
             f"Dari karantina {origin['sku']} (lot #{lot_id}): {note}", now, by),
        )
    out = get_quarantine_lot(lot_id)
    out["secondhand_item"] = {"id": variant["id"], "sku": variant["sku"], "name": variant["name"], "stock_qty": new_qty, "sell_price": sell_price}
    return out


def request_quarantine_writeoff(lot_id: int, qty: int, reason: str, user: dict) -> dict:
    """Decision 8: staff asks, an admin recommends, the Owner approves. An admin's own request starts
    as recommended."""
    reason = _q_reason(reason, "penghapusan")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        lot = _q_lot(con, lot_id)
        free = lot["qty_left"] - _q_pending(con, lot_id)
        if qty <= 0 or qty > free:
            raise ValueError(f"Jumlah yang bisa diajukan 1 sampai {free}.")
        now = _now()
        admin = user.get("role") == "admin"
        cur = con.execute(
            """INSERT INTO quarantine_writeoffs (lot_id, qty, value, reason, status, requested_by, requested_at,
                   recommended_by, recommended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lot_id, qty, qty * (lot["unit_cost"] or 0), reason, "direkomendasikan" if admin else "diajukan",
             user["username"], now, user["username"] if admin else None, now if admin else None),
        )
        _q_event(con, lot_id, "hapus_ajukan", qty, f"Pengajuan hapus #{cur.lastrowid}: {reason}", user["username"])
    return get_quarantine_lot(lot_id)


def review_quarantine_writeoff(writeoff_id: int, recommend: bool, note: str | None, by: str | None) -> dict:
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        w = con.execute("SELECT * FROM quarantine_writeoffs WHERE id=?", (writeoff_id,)).fetchone()
        if w is None:
            raise LookupError("Pengajuan hapus tidak ditemukan.")
        if w["status"] != "diajukan":
            raise ValueError("Pengajuan ini sudah diperiksa.")
        if not recommend:
            note = _q_reason(note, "penolakan")
        con.execute(
            """UPDATE quarantine_writeoffs SET status=?, recommended_by=?, recommended_at=?, recommend_note=?
                   %s WHERE id=? AND status='diajukan'""" % (", decided_by=?, decided_at=?" if not recommend else ""),
            ("direkomendasikan" if recommend else "ditolak", by, _now(), (note or "").strip() or None,
             *((by, _now()) if not recommend else ()), writeoff_id),
        )
        _q_event(con, w["lot_id"], "hapus_rekomendasi" if recommend else "hapus_tolak", w["qty"],
                 f"Pengajuan hapus #{writeoff_id} {'direkomendasikan admin' if recommend else 'ditolak admin'}"
                 + (f": {note.strip()}" if note and note.strip() else ""), by)
        lot_id = w["lot_id"]
    return get_quarantine_lot(lot_id)


def decide_quarantine_writeoff(writeoff_id: int, approve: bool, note: str | None, by: str | None) -> dict:
    """Owner: the final word. Approved, the units leave quarantine as a loss (they were never in the
    sellable stock, so stock_qty is untouched)."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        w = con.execute("SELECT * FROM quarantine_writeoffs WHERE id=?", (writeoff_id,)).fetchone()
        if w is None:
            raise LookupError("Pengajuan hapus tidak ditemukan.")
        if w["status"] not in WRITEOFF_OPEN:
            raise ValueError("Pengajuan ini sudah diputuskan.")
        if not approve:
            note = _q_reason(note, "penolakan")
        lot = _q_lot(con, w["lot_id"])
        if approve and w["qty"] > lot["qty_left"]:
            raise ValueError(f"Sisa barang di karantina tinggal {lot['qty_left']}.")
        con.execute(
            "UPDATE quarantine_writeoffs SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
            ("disetujui" if approve else "ditolak", by, _now(), (note or "").strip() or None, writeoff_id),
        )
        if approve:
            _quarantine_take(con, lot, w["qty"], "hapus",
                             f"Dihapus (disetujui owner), pengajuan #{writeoff_id}: {w['reason']}"
                             + (f" — {note.strip()}" if note and note.strip() else ""), by)
        else:
            _q_event(con, lot["id"], "hapus_tolak", w["qty"], f"Pengajuan hapus #{writeoff_id} ditolak owner: {note.strip()}", by)
    return get_quarantine_lot(w["lot_id"])


# --- Stock adjustments / ledger ---------------------------------------


def create_stock_adjustment(master_item_id: int, qty_delta: int, reason: str, created_by: str | None = None) -> dict:
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
            """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
               VALUES (?, 'ADJUSTMENT', ?, ?, 'manual', NULL, ?, ?, ?)""",
            (master_item_id, qty_delta, new_stock_qty, reason, _now(), created_by),
        )
        con.execute("UPDATE master_items SET updated_at=?, updated_by=? WHERE id=?", (_now(), created_by, master_item_id))
        if qty_delta > 0:
            _promote_shelf_ready_lines(con, created_by, master_item_ids=[master_item_id])
        return {"master_item_id": master_item_id, "qty_delta": qty_delta, "stock_qty": new_stock_qty}


def get_stock_ledger(master_item_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT sl.*, COALESCE(d.doc_number, r.receipt_no, so.opname_no) AS ref_doc_number,
                      COALESCE(sl.created_by, r.cashier_name, sr.created_by) AS actor,
                      CASE WHEN sl.ref_type='receipt' THEN sl.note END AS ref_receipt_id,
                      sp.po_number AS supplier_po_number, sp.id AS supplier_po_id
               FROM stock_ledger sl
               LEFT JOIN inbound_documents d ON sl.ref_type='inbound_document' AND d.id = sl.ref_id
               LEFT JOIN spo_receipts sr ON sr.inbound_document_id = d.id
               LEFT JOIN supplier_pos sp ON sp.id = sr.supplier_po_id
               LEFT JOIN receipts r ON sl.ref_type='receipt' AND r.id = sl.note
               LEFT JOIN stock_opnames so ON sl.ref_type='stock_opname' AND so.id = sl.ref_id
               WHERE sl.master_item_id=? ORDER BY sl.id ASC""", (master_item_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Purchase invoices (faktur pembelian / accounts payable) ----------------
# A supplier's bill, recorded as-is. Deliberately does NOT touch stock or
# costing — goods movement stays the inbound document's job (§ Barang Masuk),
# so an invoice can be entered before, after, or without a matching delivery.


def _next_purchase_invoice_number(con: sqlite3.Connection, today_str: str) -> str:
    """A placeholder for a draft whose supplier invoice number is not known yet. The faktur number IS the supplier's
    own invoice number (owner, 21-10-2026): _sync_faktur_number replaces this as soon as that number is filled in."""
    prefix = f"DRAFT-{today_str}-"
    row = con.execute(
        "SELECT invoice_number FROM purchase_invoices WHERE invoice_number LIKE ? ORDER BY invoice_number DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = int(row["invoice_number"].rsplit("-", 1)[-1]) + 1 if row is not None else 1
    return f"{prefix}{seq:04d}"


def compute_purchase_invoice_totals(items: list[dict], discount: int, tax_rate: float,
                                    shipping_cost: int = 0, shipping_dpp: int = 0, shipping_tax: int | None = None) -> dict:
    """Single source of truth for invoice money. Always recomputed server-side
    from the line items — the client's totals are display-only and never stored.
    Shipping follows the supplier's faktur: its own cost, the DPP it is taxed on (0 = no PPN on shipping)
    and the PPN amount. PPN is never put on shipping by default; an amount left out is DPP × the rate."""
    subtotal = sum(int(i["qty"]) * int(i["unit_price"]) for i in items)
    discount = max(0, min(int(discount), subtotal))
    taxable = subtotal - discount
    tax_amount = int(round(taxable * float(tax_rate) / 100))
    shipping_cost, shipping_dpp = int(shipping_cost or 0), int(shipping_dpp or 0)
    if shipping_cost < 0 or shipping_dpp < 0 or (shipping_tax or 0) < 0:
        raise ValueError("Ongkir, DPP ongkir, dan PPN ongkir tidak boleh negatif.")
    if shipping_dpp > shipping_cost:
        raise ValueError("DPP ongkir tidak boleh lebih besar dari ongkir.")
    if shipping_tax is None:
        shipping_tax = int(round(shipping_dpp * float(tax_rate) / 100))
    shipping_tax = int(shipping_tax)
    if shipping_tax and not shipping_dpp:
        raise ValueError("Isi DPP ongkir bila faktur supplier mengenakan PPN atas ongkir.")
    return {
        "subtotal": subtotal,
        "discount": discount,
        "tax_rate": float(tax_rate),
        "tax_amount": tax_amount,
        "shipping_cost": shipping_cost,
        "shipping_dpp": shipping_dpp,
        "shipping_tax": shipping_tax,
        "total": taxable + tax_amount + shipping_cost + shipping_tax,
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
        raise ValueError(f"Supplier {supplier_id} tidak ditemukan.")
    return {
        "supplier_name_snapshot": row["name"],
        "supplier_phone_snapshot": row["phone"],
        "supplier_address_snapshot": row["address"],
        "supplier_contact_snapshot": row["contact_person"],
    }


def _check_supplier_invoice_no(con: sqlite3.Connection, supplier_id: int, supplier_invoice_no: str | None,
                               exclude_id: int | None = None) -> None:
    """One supplier's own invoice number is recorded once; a voided faktur frees the number again."""
    no = (supplier_invoice_no or "").strip()
    if not no:
        return
    dup = con.execute(
        "SELECT invoice_number FROM purchase_invoices WHERE supplier_id=? AND status != 'void' "
        "AND UPPER(TRIM(supplier_invoice_no)) = UPPER(?) AND id != ?",
        (supplier_id, no, exclude_id or 0),
    ).fetchone()
    if dup is not None:
        raise ValueError(f"No. faktur supplier {no} sudah dicatat di {dup['invoice_number']}.")


def _sync_faktur_number(con: sqlite3.Connection, invoice_id: int) -> None:
    """The faktur is numbered by the supplier's own invoice number, not by a running number. invoice_number stays
    unique (two suppliers can use the same number, and a voided faktur keeps its own), so a clash gets the supplier
    name added. Everything else refers to the faktur by id, so renaming it is safe."""
    row = con.execute("SELECT id, invoice_number, supplier_invoice_no, supplier_name_snapshot FROM purchase_invoices WHERE id=?",
                      (invoice_id,)).fetchone()
    no = " ".join((row["supplier_invoice_no"] or "").split()) if row else ""
    if not no or row["invoice_number"] == no:
        return
    taken = lambda n: con.execute("SELECT 1 FROM purchase_invoices WHERE invoice_number=? AND id<>?", (n, invoice_id)).fetchone()
    target = no
    if taken(target):
        target = f"{no} · {row['supplier_name_snapshot']}"
    if taken(target):
        target = f"{no} · {row['supplier_name_snapshot']} #{invoice_id}"
    if row["invoice_number"] != target:
        con.execute("UPDATE purchase_invoices SET invoice_number=? WHERE id=?", (target, invoice_id))


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
    shipping_cost: int = 0,
    shipping_dpp: int = 0,
    shipping_tax: int | None = None,
) -> dict:
    now = _now()
    totals = compute_purchase_invoice_totals(items, discount, tax_rate, shipping_cost, shipping_dpp, shipping_tax)
    with _conn() as con:
        snapshot = _supplier_snapshot(con, supplier_id)
        _check_supplier_invoice_no(con, supplier_id, supplier_invoice_no)
        invoice_number = _next_purchase_invoice_number(con, date.today().strftime("%Y%m%d"))
        cur = con.execute(
            """INSERT INTO purchase_invoices
               (invoice_number, supplier_id, supplier_invoice_no, supplier_name_snapshot,
                supplier_phone_snapshot, supplier_address_snapshot, supplier_contact_snapshot,
                invoice_date, payment_terms, due_date, subtotal, discount, tax_rate, tax_amount,
                total, amount_paid, status, notes, created_at, updated_at, shipping_cost, shipping_dpp, shipping_tax)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'draft', ?, ?, ?, ?, ?, ?)""",
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
                totals["shipping_cost"],
                totals["shipping_dpp"],
                totals["shipping_tax"],
            ),
        )
        invoice_id = cur.lastrowid
        _insert_purchase_invoice_items(con, invoice_id, items)
        _sync_faktur_number(con, invoice_id)
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
    shipping_cost: int = 0,
    shipping_dpp: int = 0,
    shipping_tax: int | None = None,
) -> dict:
    totals = compute_purchase_invoice_totals(items, discount, tax_rate, shipping_cost, shipping_dpp, shipping_tax)
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(
                f"Faktur pembelian {invoice_id} tidak bisa diubah: statusnya '{row['status']}', "
                "hanya faktur draft yang bisa diubah"
            )
        snapshot = _supplier_snapshot(con, supplier_id)
        _check_supplier_invoice_no(con, supplier_id, supplier_invoice_no, invoice_id)
        con.execute(
            """UPDATE purchase_invoices
               SET supplier_id=?, supplier_invoice_no=?, supplier_name_snapshot=?,
                   supplier_phone_snapshot=?, supplier_address_snapshot=?, supplier_contact_snapshot=?,
                   invoice_date=?, payment_terms=?, due_date=?, subtotal=?, discount=?, tax_rate=?,
                   tax_amount=?, total=?, notes=?, updated_at=?, shipping_cost=?, shipping_dpp=?, shipping_tax=?
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
                totals["shipping_cost"],
                totals["shipping_dpp"],
                totals["shipping_tax"],
                invoice_id,
            ),
        )
        con.execute("DELETE FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,))
        _insert_purchase_invoice_items(con, invoice_id, items)
        _sync_faktur_number(con, invoice_id)
        if row["supplier_po_id"] and row["verify_status"] != "belum":
            # The faktur changed after it was checked: the checking (or the Admin's exception) no longer covers it.
            con.execute("UPDATE purchase_invoices SET verify_status='belum', verify_note=NULL, verified_by=NULL, verified_at=NULL, "
                        "exception_reason=NULL, hold_note=NULL WHERE id=?", (invoice_id,))
            con.execute("INSERT INTO purchase_invoice_events (invoice_id, at, by_user, kind, note) VALUES (?, ?, NULL, 'reset', ?)",
                        (invoice_id, _now(), "Faktur diubah setelah diverifikasi/ditahan, verifikasi diulang dari awal."))
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def get_purchase_invoice(invoice_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT pi.*, s.name AS supplier_name, s.code AS supplier_code, s.npwp AS supplier_npwp,
                      sp.po_number AS supplier_po_number
               FROM purchase_invoices pi
               JOIN suppliers s ON s.id = pi.supplier_id
               LEFT JOIN supplier_pos sp ON sp.id = pi.supplier_po_id
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
        query = """SELECT pi.*, s.name AS supplier_name, sp.po_number AS supplier_po_number FROM purchase_invoices pi
                   JOIN suppliers s ON s.id = pi.supplier_id LEFT JOIN supplier_pos sp ON sp.id = pi.supplier_po_id WHERE 1=1"""
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
        # Goods Receipts behind a faktur made from a PO: the PO's Barang Masuk documents and what each brought in.
        po_ids = sorted({r["supplier_po_id"] for r in rows if r.get("supplier_po_id")})
        gr_by_po: dict[str, list[dict]] = {}
        if po_ids:
            for g in con.execute(
                    f"""SELECT sr.supplier_po_id, d.id, d.doc_number, d.status, sr.received_date, SUM(srl.quantity) AS qty
                        FROM spo_receipts sr JOIN inbound_documents d ON d.id = sr.inbound_document_id
                        LEFT JOIN spo_receipt_lines srl ON srl.receipt_id = sr.id
                        WHERE sr.supplier_po_id IN ({_placeholders(po_ids)}) AND d.status <> 'void'
                        GROUP BY sr.id ORDER BY sr.received_date, sr.id""", po_ids):
                gr_by_po.setdefault(g["supplier_po_id"], []).append(
                    {"id": g["id"], "doc_number": g["doc_number"], "status": g["status"], "received_date": g["received_date"], "qty": g["qty"] or 0})
        for r in rows:
            r["gr_docs"] = gr_by_po.get(r.get("supplier_po_id"), [])

    # payment_status is derived, so it can't be pushed into the SQL WHERE above.
    if payment_status:
        rows = [
            r for r in rows if compute_payment_status(r["total"], r["amount_paid"]) == payment_status
        ]
    return rows


def post_purchase_invoice(invoice_id: int, by: str | None = None) -> dict:
    """draft -> posted. Locks the invoice against edits and makes it count as a
    real payable. No stock or costing side effects by design."""
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(f"Faktur pembelian {invoice_id} sudah berstatus '{row['status']}'")
        item_count = con.execute(
            "SELECT COUNT(*) AS n FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,)
        ).fetchone()["n"]
        if item_count == 0:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak bisa diposting: tidak ada baris barang")
        from backend import invoice_verify
        invoice_verify.require_verified(con, dict(row))
        if not (row["supplier_invoice_no"] or "").strip():
            raise ValueError("Isi No. Invoice Supplier dulu: nomor itu menjadi nomor faktur. Simpan, lalu posting.")
        _sync_faktur_number(con, invoice_id)
        con.execute(
            "UPDATE purchase_invoices SET status='posted', updated_at=? WHERE id=?", (_now(), invoice_id)
        )
        _apply_po_dp(con, invoice_id, by)
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def void_purchase_invoice(invoice_id: int, reason: str) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError(f"Faktur pembelian {invoice_id} sudah dibatalkan")
        # The supplier DP carried over from the PO goes back to the PO; real payments must be undone first.
        dp_rows = con.execute(
            "SELECT * FROM purchase_invoice_payments WHERE invoice_id=? AND kind IN ('dp', 'dp_batal')", (invoice_id,)).fetchall()
        dp_net = sum(r["amount"] for r in dp_rows)
        if row["amount_paid"] - dp_net > 0:
            raise ValueError(
                f"Faktur pembelian {invoice_id} tidak bisa dibatalkan: sudah tercatat pembayaran {row['amount_paid'] - dp_net}. "
                "Batalkan pembayarannya terlebih dahulu."
            )
        # Net per DP: an allocation already reversed by hand (dp_batal row with the same spo_payment_id) must not be returned twice.
        net_by_dp: dict[int, int] = {}
        for r in dp_rows:
            if r["spo_payment_id"]:
                net_by_dp[r["spo_payment_id"]] = net_by_dp.get(r["spo_payment_id"], 0) + r["amount"]
        for dp_id, net in net_by_dp.items():
            if net > 0:
                con.execute("UPDATE spo_payments SET applied_amount = MAX(applied_amount - ?, 0), applied_invoice_id=NULL WHERE id=?", (net, dp_id))
        if dp_net:
            _pi_payment_row(con, invoice_id, -dp_net, "dp_batal", None, note="Faktur dibatalkan, DP kembali ke PO")
            con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid - ? WHERE id=?", (dp_net, invoice_id))
            row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row["amount_paid"] > 0:
            raise ValueError(
                f"Faktur pembelian {invoice_id} tidak bisa dibatalkan: sudah tercatat pembayaran {row['amount_paid']}. "
                "Batalkan pembayarannya terlebih dahulu."
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
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(
                f"Faktur pembelian {invoice_id} tidak bisa dihapus: statusnya '{row['status']}'. "
                "Batalkan (void) saja agar nomornya tetap tercatat."
            )
        con.execute("DELETE FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,))
        con.execute("DELETE FROM purchase_invoices WHERE id=?", (invoice_id,))


PURCHASE_PAYMENT_KINDS = {"bayar": "Pembayaran", "batal": "Pembatalan pembayaran", "dp": "Uang muka (DP) dari PO",
                          "dp_batal": "DP dikembalikan ke PO", "kredit": "Nota debit retur"}


def _pi_payment_row(con: sqlite3.Connection, invoice_id: int, amount: int, kind: str, by: str | None, *,
                    paid_at: str | None = None, method: str | None = None, reference_no: str | None = None,
                    note: str | None = None, spo_payment_id: int | None = None, reverses_id: int | None = None) -> None:
    now = _now()
    con.execute(
        """INSERT INTO purchase_invoice_payments (invoice_id, paid_at, amount, kind, method, reference_no, note,
               spo_payment_id, created_by, created_at, reverses_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (invoice_id, paid_at or now, amount, kind, method, reference_no, note, spo_payment_id, by, now, reverses_id),
    )


def _kk_assign(con: sqlite3.Connection) -> None:
    """Kas Keluar numbers (KK-YYYYMMDD-NNN, per payment day) for supplier money out: faktur payments (bayar / batal)
    and PO DP. Older rows get theirs here, in payment order, so the numbers never change once given."""
    todo = [("purchase_invoice_payments", r["id"], r["paid_at"]) for r in con.execute(
        "SELECT id, paid_at FROM purchase_invoice_payments WHERE kind IN ('bayar','batal') AND kk_number IS NULL").fetchall()]
    todo += [("spo_payments", r["id"], r["paid_at"]) for r in con.execute("SELECT id, paid_at FROM spo_payments WHERE kk_number IS NULL").fetchall()]
    if not todo:
        return
    todo.sort(key=lambda t: (t[2], t[0], t[1]))
    counters: dict[str, int] = {}
    for tbl in ("purchase_invoice_payments", "spo_payments"):
        for r in con.execute(f"SELECT kk_number FROM {tbl} WHERE kk_number IS NOT NULL").fetchall():
            day = r["kk_number"][3:11]
            counters[day] = max(counters.get(day, 0), int(r["kk_number"][12:] or 0))
    for tbl, rid, paid_at in todo:
        day = (paid_at or _now())[:10].replace("-", "")
        counters[day] = counters.get(day, 0) + 1
        con.execute(f"UPDATE {tbl} SET kk_number=? WHERE id=?", (f"KK-{day}-{counters[day]:03d}", rid))


def record_purchase_invoice_payment(
    invoice_id: int, amount: int, method: str | None = None, paid_date: str | None = None,
    reference_no: str | None = None, note: str | None = None, by: str | None = None,
    source_account: str | None = None, dest_account: str | None = None,
) -> dict:
    """Adds to amount_paid (a row per payment). A negative amount reverses an over-recorded payment;
    the running total stays within [0, total] either way."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "posted":
            raise ValueError(
                f"Pembayaran tidak bisa dicatat pada faktur pembelian {invoice_id}: statusnya '{row['status']}', "
                "hanya faktur yang sudah diposting yang bisa dibayar"
            )
        if amount == 0:
            raise ValueError("Jumlah pembayaran tidak boleh nol")
        new_paid = row["amount_paid"] + amount
        if new_paid < 0:
            raise ValueError("Pembatalan pembayaran melebihi jumlah yang tercatat sudah dibayar")
        if new_paid > row["total"]:
            raise ValueError(
                f"Pembayaran melebihi sisa tagihan ({row['total'] - row['amount_paid']})"
            )
        paid_at = None
        if paid_date:
            try:
                date.fromisoformat(paid_date)
            except ValueError:
                raise ValueError("Tanggal bayar harus berformat YYYY-MM-DD.")
            paid_at = f"{paid_date}T{_now()[11:]}"
        con.execute(
            "UPDATE purchase_invoices SET amount_paid=?, updated_at=? WHERE id=?",
            (new_paid, _now(), invoice_id),
        )
        _pi_payment_row(con, invoice_id, amount, "bayar" if amount > 0 else "batal", by, paid_at=paid_at,
                        method=method, reference_no=reference_no, note=note)
        rid = con.execute("SELECT MAX(id) AS i FROM purchase_invoice_payments WHERE invoice_id=?", (invoice_id,)).fetchone()["i"]
        src = " ".join((source_account or "").split()) or None
        dst = " ".join((dest_account or "").split()) or None
        con.execute("UPDATE purchase_invoice_payments SET source_account=?, dest_account=? WHERE id=?", (src, dst, rid))
        if src:
            _remember_transfer_name(con, "account", src)
        _kk_assign(con)
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def get_purchase_invoice_payments(invoice_id: int) -> list[dict]:
    with _conn() as con:
        _kk_assign(con)
        return [dict(r) for r in con.execute(
            "SELECT * FROM purchase_invoice_payments WHERE invoice_id=? ORDER BY paid_at ASC, id ASC", (invoice_id,))]


def set_payment_proof(payment_id: int, filename: str, original_name: str | None, by: str | None) -> tuple[dict, str | None]:
    """Attach (or replace) the proof of a supplier payment. A verified proof is locked. Returns (row, previous filename)."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_id,)).fetchone()
        if r is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if r["kind"] != "bayar":
            raise ValueError("Bukti hanya untuk pembayaran supplier (bukan DP otomatis, kredit, atau pembatalan).")
        if r["proof_status"] == "terverifikasi":
            raise ValueError("Bukti sudah diverifikasi Admin dan terkunci.")
        con.execute("UPDATE purchase_invoice_payments SET proof_file=?, proof_name=?, proof_status='tersimpan', proof_by=?, proof_at=?, "
                    "verified_by=NULL, verified_at=NULL WHERE id=?", (filename, (original_name or "")[:120] or None, by, _now(), payment_id))
        inv = con.execute("SELECT invoice_number, supplier_po_id FROM purchase_invoices WHERE id=?", (r["invoice_id"],)).fetchone()
        if inv and inv["supplier_po_id"]:
            _spo_event(con, inv["supplier_po_id"], "payment", f"Bukti pembayaran faktur {inv['invoice_number']} diunggah", by)
        return dict(con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_id,)).fetchone()), r["proof_file"]


def verify_payment_proof(payment_id: int, by: str | None) -> dict:
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_id,)).fetchone()
        if r is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if not r["proof_file"]:
            raise ValueError("Belum ada bukti yang diunggah untuk pembayaran ini.")
        if r["proof_status"] == "terverifikasi":
            raise ValueError("Bukti ini sudah diverifikasi.")
        con.execute("UPDATE purchase_invoice_payments SET proof_status='terverifikasi', verified_by=?, verified_at=? WHERE id=?", (by, _now(), payment_id))
        inv = con.execute("SELECT invoice_number, supplier_po_id FROM purchase_invoices WHERE id=?", (r["invoice_id"],)).fetchone()
        if inv and inv["supplier_po_id"]:
            _spo_event(con, inv["supplier_po_id"], "payment", f"Bukti pembayaran faktur {inv['invoice_number']} diverifikasi", by)
        return dict(con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_id,)).fetchone())


def get_payment_row(payment_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_id,)).fetchone()
        return dict(r) if r else None


def _apply_po_dp(con: sqlite3.Connection, invoice_id: int, by: str | None) -> int:
    """K-12: the supplier DP paid on the faktur's PO is carried onto the faktur when it is posted
    (oldest first, never beyond what the faktur still owes). Returns the amount applied."""
    inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None or not inv["supplier_po_id"]:
        return 0
    room = inv["total"] - inv["amount_paid"]
    applied = 0
    for p in con.execute(
            "SELECT * FROM spo_payments WHERE supplier_po_id=? AND amount - applied_amount > 0 ORDER BY paid_at ASC, id ASC",
            (inv["supplier_po_id"],)).fetchall():
        if room <= 0:
            break
        take = min(p["amount"] - p["applied_amount"], room)
        con.execute("UPDATE spo_payments SET applied_amount = applied_amount + ?, applied_invoice_id=? WHERE id=?",
                    (take, invoice_id, p["id"]))
        _pi_payment_row(con, invoice_id, take, "dp", by, paid_at=p["paid_at"], method=p["method"],
                        reference_no=p["reference_no"], note=p["note"], spo_payment_id=p["id"])
        room -= take
        applied += take
    if applied:
        con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid + ?, updated_at=? WHERE id=?",
                    (applied, _now(), invoice_id))
        _spo_event(con, inv["supplier_po_id"], "payment",
                   f"DP supplier Rp {applied:,} diperhitungkan ke Faktur Pembelian {inv['invoice_number']}".replace(",", "."), by)
    return applied


DP_MANUAL_NOTE = "Alokasi DP manual"


def allocate_spo_dp(spo_payment_id: int, invoice_id: int, amount: int, by: str | None = None) -> dict:
    """Alokasi DP Supplier: move (part of) a supplier DP that no faktur has absorbed onto a posted faktur of the SAME
    supplier. No new cash: the DP was already paid, so only the faktur's amount_paid and the DP's applied_amount move.
    The auto carry at posting (_apply_po_dp) stays; this is for the leftover or for another faktur."""
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError("Jumlah alokasi harus lebih dari 0.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute(
            """SELECT p.*, o.supplier_id, o.po_number, o.status AS po_status FROM spo_payments p
               JOIN supplier_pos o ON o.id = p.supplier_po_id WHERE p.id=?""", (spo_payment_id,)).fetchone()
        if p is None:
            raise LookupError("DP supplier tidak ditemukan.")
        inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if inv is None:
            raise LookupError("Faktur pembelian tidak ditemukan.")
        if p["po_status"] == "batal":
            raise ValueError("DP dari PO yang dibatalkan tidak bisa dialokasikan. Uang mukanya diurus lewat klaim refund supplier.")
        if inv["status"] != "posted":
            raise ValueError("DP hanya bisa dialokasikan ke faktur yang sudah diposting.")
        if inv["supplier_id"] != p["supplier_id"]:
            raise ValueError("DP hanya bisa dialokasikan ke faktur supplier yang sama dengan PO-nya.")
        free = p["amount"] - p["applied_amount"]
        room = inv["total"] - inv["amount_paid"]
        if free <= 0:
            raise ValueError("DP ini sudah habis dialokasikan.")
        if room <= 0:
            raise ValueError("Faktur ini sudah lunas, tidak ada sisa tagihan untuk dialokasikan.")
        if amount > free:
            raise ValueError(f"Jumlah alokasi melebihi sisa DP (Rp {free:,}).".replace(",", "."))
        if amount > room:
            raise ValueError(f"Jumlah alokasi melebihi sisa tagihan faktur (Rp {room:,}).".replace(",", "."))
        con.execute("UPDATE spo_payments SET applied_amount = applied_amount + ?, applied_invoice_id=? WHERE id=?", (amount, invoice_id, spo_payment_id))
        _pi_payment_row(con, invoice_id, amount, "dp", by, paid_at=p["paid_at"], method=p["method"], reference_no=p["reference_no"],
                        note=f"{DP_MANUAL_NOTE} dari {p['po_number']}", spo_payment_id=spo_payment_id)
        con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid + ?, updated_at=? WHERE id=?", (amount, _now(), invoice_id))
        _spo_event(con, p["supplier_po_id"], "payment",
                   f"DP Rp {amount:,} dialokasikan manual ke Faktur Pembelian {inv['invoice_number']}".replace(",", "."), by)
        return dict(con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone())


def reverse_dp_allocation(payment_row_id: int, reason: str, by: str | None = None) -> dict:
    """Undo one manual DP allocation: the DP becomes available again and the faktur owes that amount again."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan pembatalan alokasi wajib diisi (minimal 5 karakter).")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM purchase_invoice_payments WHERE id=?", (payment_row_id,)).fetchone()
        if r is None:
            raise LookupError("Baris alokasi tidak ditemukan.")
        if r["kind"] != "dp" or not r["spo_payment_id"] or not (r["note"] or "").startswith(DP_MANUAL_NOTE):
            raise ValueError("Hanya alokasi DP manual yang bisa dibatalkan di sini. DP otomatis kembali saat faktur di-void.")
        if con.execute("SELECT 1 FROM purchase_invoice_payments WHERE reverses_id=?", (payment_row_id,)).fetchone():
            raise ValueError("Alokasi ini sudah dibatalkan.")
        inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (r["invoice_id"],)).fetchone()
        if inv["status"] != "posted":
            raise ValueError("Alokasi hanya bisa dibatalkan selama faktur masih diposting.")
        p = con.execute("SELECT p.*, o.status AS po_status, o.po_number FROM spo_payments p JOIN supplier_pos o ON o.id = p.supplier_po_id WHERE p.id=?",
                        (r["spo_payment_id"],)).fetchone()
        if p["po_status"] == "batal":
            raise ValueError("PO asal DP sudah dibatalkan, alokasi tidak bisa dibalik.")
        con.execute("UPDATE spo_payments SET applied_amount = MAX(applied_amount - ?, 0) WHERE id=?", (r["amount"], r["spo_payment_id"]))
        _pi_payment_row(con, r["invoice_id"], -r["amount"], "dp_batal", by, method=r["method"], reference_no=r["reference_no"],
                        note=f"Alokasi DP dibatalkan: {reason}", spo_payment_id=r["spo_payment_id"], reverses_id=payment_row_id)
        con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid - ?, updated_at=? WHERE id=?", (r["amount"], _now(), r["invoice_id"]))
        _spo_event(con, p["supplier_po_id"], "payment",
                   f"Alokasi DP Rp {r['amount']:,} ke Faktur {inv['invoice_number']} dibatalkan: {reason}".replace(",", "."), by)
        return dict(con.execute("SELECT * FROM purchase_invoices WHERE id=?", (r["invoice_id"],)).fetchone())


def _active_po_faktur(con: sqlite3.Connection, supplier_po_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM purchase_invoices WHERE supplier_po_id=? AND status<>'void' ORDER BY id DESC LIMIT 1",
        (supplier_po_id,)).fetchone()
    return dict(row) if row else None


def po_billable_lines(con: sqlite3.Connection, supplier_po_id: str) -> list[dict]:
    """What a faktur from this PO bills: each PO line's received goods at the price its posted Goods Receipt
    confirmed (an Admin-approved excess included, at its own price), and goods received before Goods Receipt
    existed at the PO price. One entry per line and price."""
    out = []
    for i in _spo_items(con, supplier_po_id):
        po_price = _po_net_price(i)
        received = min(i["quantity"] - i["qty_cancelled"], i["qty_received"])
        by_price: dict[int, int] = {}
        counted = 0
        for r in con.execute(
                """SELECT ii.qty_in, ii.cost_price, ii.is_over,
                          (SELECT COALESCE(SUM(sri.qty), 0) FROM supplier_return_items sri JOIN supplier_returns sr ON sr.id = sri.return_id
                           WHERE sri.inbound_item_id = ii.id AND sr.origin='barang_masuk' AND sr.doc_status<>'dibatalkan'
                             AND sri.settlement IS NULL) AS returned
                   FROM inbound_items ii JOIN inbound_documents d ON d.id = ii.document_id
                   WHERE ii.spo_item_id=? AND d.status='posted' ORDER BY ii.id""", (i["id"],)):
            # Goods sent back from Barang Masuk before the faktur was posted are not billed (no Debit Note needed).
            by_price[r["cost_price"]] = by_price.get(r["cost_price"], 0) + r["qty_in"] - r["returned"]
            if not r["is_over"]:
                counted += r["qty_in"]
        if received > counted:
            by_price[po_price] = by_price.get(po_price, 0) + received - counted
        for price, qty in by_price.items():
            if qty > 0:
                out.append({"spo_item_id": i["id"], "master_item_id": i["master_item_id"], "product_name": i["product_name"],
                            "sku": i.get("sku"), "unit": i.get("unit") or "", "qty": qty, "unit_price": price, "po_price": po_price})
    return out


def po_over_qty(con: sqlite3.Connection, supplier_po_id: str) -> dict[int, int]:
    """Excess an Admin accepted per PO line (posted GRs): billable, though never counted on the PO."""
    return {r[0]: r[1] for r in con.execute(
        """SELECT ii.spo_item_id, SUM(ii.qty_in) FROM inbound_items ii JOIN inbound_documents d ON d.id = ii.document_id
           JOIN supplier_po_items spi ON spi.id = ii.spo_item_id
           WHERE spi.supplier_po_id=? AND d.status='posted' AND ii.is_over=1 GROUP BY ii.spo_item_id""", (supplier_po_id,))}


def create_purchase_invoice_from_po(supplier_po_id: str, by: str | None = None) -> dict:
    """K-12 'Buat Faktur Pembelian' on an issued PO: a draft faktur with the PO's lines (ordered minus
    cancelled, at the PO price after line discount), header discount, PPN and shipping as its own line.
    It is checked and posted on the Faktur Pembelian page; posting turns it into debt and carries the
    PO's DP over. One active faktur per PO."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if po is None:
            raise LookupError("Purchase Order tidak ditemukan.")
        po = dict(po)
        if po["status"] in ("draft", "batal"):
            raise ValueError("Faktur hanya untuk Purchase Order yang sudah diterbitkan.")
        have = _active_po_faktur(con, supplier_po_id)
        if have:
            raise ValueError(f"PO ini sudah punya Faktur Pembelian {have['invoice_number']}.")
        # Three-Way Matching: the faktur bills what has been received (Goods Receipt) at the price the GR confirmed,
        # not what was ordered.
        lines = [{k: b[k] for k in ("master_item_id", "product_name", "sku", "unit", "qty", "unit_price")}
                 for b in po_billable_lines(con, supplier_po_id)]
        if not lines:
            raise ValueError("Belum ada barang yang diterima dari PO ini. Faktur dibuat dari barang yang sudah diterima (Goods Receipt); "
                             "catat penerimaan barang dulu.")
        goods = sum(l["qty"] * l["unit_price"] for l in lines)
        discount = round(goods * (po["discount_pct"] or 0) / 100)
        # The PO keeps shipping outside PPN, so the draft does too (no PPN counted twice). If the supplier's
        # faktur does tax shipping, its DPP and PPN amount are filled in from that paper before posting.
        totals = compute_purchase_invoice_totals(lines, discount, po["tax_rate"] or 0, po["shipping_cost"] or 0, 0, 0)
        today = shop_today()
        days = po["payment_terms_days"] if po["payment_terms_days"] is not None else 30
        due = po["due_date"] or (today + timedelta(days=days)).isoformat()
        snapshot = _supplier_snapshot(con, po["supplier_id"])
        note = f"Dari Purchase Order {po['po_number']}"
        if po["shipping_cost"] and po["tax_rate"]:
            note += ". Ongkir dicatat tanpa PPN seperti di PO; bila faktur supplier mengenakan PPN atas ongkir, isi DPP & PPN ongkir sebelum posting"
        now = _now()
        cur = con.execute(
            """INSERT INTO purchase_invoices
               (invoice_number, supplier_id, supplier_invoice_no, supplier_name_snapshot, supplier_phone_snapshot,
                supplier_address_snapshot, supplier_contact_snapshot, invoice_date, payment_terms, due_date, subtotal,
                discount, tax_rate, tax_amount, total, amount_paid, status, notes, created_at, updated_at, supplier_po_id,
                shipping_cost, shipping_dpp, shipping_tax)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'draft', ?, ?, ?, ?, ?, ?, ?)""",
            (_next_purchase_invoice_number(con, today.strftime("%Y%m%d")), po["supplier_id"], po["supplier_invoice_no"],
             snapshot["supplier_name_snapshot"], snapshot["supplier_phone_snapshot"], snapshot["supplier_address_snapshot"],
             snapshot["supplier_contact_snapshot"], today.isoformat(), f"NET {days}", due, totals["subtotal"],
             totals["discount"], totals["tax_rate"], totals["tax_amount"], totals["total"],
             note, now, now, supplier_po_id, totals["shipping_cost"], totals["shipping_dpp"], totals["shipping_tax"]),
        )
        _insert_purchase_invoice_items(con, cur.lastrowid, lines)
        _sync_faktur_number(con, cur.lastrowid)
        _spo_event(con, supplier_po_id, "invoice", "Draft Faktur Pembelian dibuat dari PO ini", by)
        return dict(con.execute("SELECT * FROM purchase_invoices WHERE id=?", (cur.lastrowid,)).fetchone())


def list_po_received_for_faktur(supplier_id: int | None) -> list[dict]:
    """Goods Receipt waiting for a faktur: this supplier's issued POs that have received goods and no active faktur
    yet, with each received line at its Goods Receipt price. Read-only; 'Ambil' on the Faktur Pembelian page then uses the
    existing create-from-PO flow, so the faktur stays linked to its PO and goes through the same matching.
    With no supplier, every supplier's (the 'Buat dari Goods Receipt' picker on the faktur list)."""
    out = []
    with _conn() as con:
        where, params = ("supplier_id=? AND ", (supplier_id,)) if supplier_id is not None else ("", ())
        pos = con.execute(
            f"SELECT * FROM supplier_pos WHERE {where}status NOT IN ('draft','batal') ORDER BY created_at DESC, id DESC",
            params).fetchall()
        for po in pos:
            po = dict(po)
            if _active_po_faktur(con, po["id"]):
                continue
            lines = [{"product_name": b["product_name"], "unit": b["unit"], "qty": b["qty"], "unit_price": b["unit_price"],
                      "po_price": b["po_price"], "line_total": b["qty"] * b["unit_price"]} for b in po_billable_lines(con, po["id"])]
            if lines:
                out.append({"po_id": po["id"], "po_number": po["po_number"], "status": po["status"],
                            "supplier_name": po["supplier_name_snapshot"],
                            "created_at": po.get("created_at"), "lines": lines, "goods_total": sum(l["line_total"] for l in lines)})
    return out


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
            raise ValueError(f"Username '{username}' sudah ada")
        # The very first account (setup) can post Barang Masuk; everyone after is given it on the Pengguna page.
        first = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        cur = con.execute(
            """INSERT INTO users (username, username_normalized, password_hash, role, is_active, created_at, updated_at, can_post_inbound)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (username.strip(), username_normalized, password_hash, role, created_at, created_at, 1 if first else 0),
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
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
        if row["role"] == "admin" and role != "admin" and row["is_active"] and _active_admin_count(con, user_id) == 0:
            raise ValueError("Admin terakhir tidak bisa diturunkan perannya")
        # An Owner is always an admin: losing admin drops the Owner flag too.
        con.execute("UPDATE users SET role=?, is_owner=CASE WHEN ?='admin' THEN is_owner ELSE 0 END, updated_at=? WHERE id=?",
                    (role, role, _now(), user_id))
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(updated)


def set_user_owner(user_id: int, is_owner: bool, by_user: dict) -> dict:
    """The Owner flag (final approval of losses). Only an admin can carry it. While an active Owner
    exists only an Owner may change it; with none, any admin may grant it (first-time setup)."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
        if is_owner and row["role"] != "admin":
            raise ValueError("Owner harus berperan admin.")
        owners = con.execute("SELECT COUNT(*) AS n FROM users WHERE is_owner=1 AND is_active=1").fetchone()["n"]
        me = con.execute("SELECT is_owner FROM users WHERE id=?", (by_user["id"],)).fetchone()
        if owners and not (me and me["is_owner"]):
            raise ValueError("Hanya Owner yang boleh mengubah status Owner.")
        con.execute("UPDATE users SET is_owner=?, updated_at=? WHERE id=?", (1 if is_owner else 0, _now(), user_id))
        return dict(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def set_user_post_inbound(user_id: int, allowed: bool) -> dict:
    """K-22: who may post Barang Masuk (stock goes up, cost moves). Set by an admin on the Pengguna page."""
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
        con.execute("UPDATE users SET can_post_inbound=?, updated_at=? WHERE id=?", (1 if allowed else 0, _now(), user_id))
        return dict(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def set_user_active(user_id: int, is_active: bool) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
        if not is_active and row["role"] == "admin" and row["is_active"] and _active_admin_count(con, user_id) == 0:
            raise ValueError("Admin terakhir tidak bisa dinonaktifkan")
        con.execute("UPDATE users SET is_active=?, updated_at=? WHERE id=?", (int(is_active), _now(), user_id))
        if not is_active:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(updated)


def reset_user_password(user_id: int, password_hash: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
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
            """SELECT s.id, s.user_id, s.expires_at, u.username, u.role, u.is_active, u.is_owner, u.can_post_inbound
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
    "dititipkan",
    "terpasang",
)

PREORDER_STATUS_LABELS = {
    "pesanan_dibuat": "Pesanan dibuat",
    "sudah_dipesankan": "Sudah dipesankan",
    "dalam_pengiriman": "Dalam pengiriman",
    "sampai_di_toko": "Sampai di toko",
    "dititipkan": "Dititipkan di toko",
    "terpasang": "Terpasang",
    PO_LINE_CANCELLED: "Dibatalkan",
}


def _preorder_overall_status(item_statuses: list[str]) -> str:
    """The order as a whole is only as far along as its *least* advanced line —
    an order with a pipe still in transit is not 'sampai di toko' just because
    the fittings arrived. An order with no items falls back to the first stage."""
    if item_statuses and all(s == PO_LINE_CANCELLED for s in item_statuses):
        return PO_LINE_CANCELLED
    item_statuses = [s for s in item_statuses if s != PO_LINE_CANCELLED]
    if not item_statuses:
        return PREORDER_STATUSES[0]
    return min(item_statuses, key=lambda s: PREORDER_STATUSES.index(s) if s in PREORDER_STATUSES else 0)


def _shop_local_date(created_at: str) -> str:
    """The shop's wall-clock calendar date (YYYY-MM-DD) for a stored UTC timestamp."""
    local = datetime.fromisoformat(created_at) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)
    return f"{local:%Y-%m-%d}"


def _next_po_no(con: sqlite3.Connection, po_date: str) -> str:
    """SO-YYYYMMDD-NNNN: a counter per Sales Order date, restarting at 0001. Called inside the
    inserting transaction, so two saves can't take the same number. Orders numbered before the
    rename keep their PO-YYYYMMDD-NNNN number (it is printed on notas already handed out)."""
    prefix = f"SO-{po_date.replace('-', '')}-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(po_number, ?) AS INTEGER)) AS n FROM preorders WHERE po_number LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:04d}"


def peek_next_po_no(po_date: str | None = None) -> str:
    """The number the next Sales Order saved on `po_date` will get (shown read-only on the form)."""
    with _conn() as con:
        return _next_po_no(con, po_date or shop_today().isoformat())


def _preorder_paid_by_order(con: sqlite3.Connection) -> dict[str, int]:
    return {
        r["preorder_id"]: r["paid"]
        for r in con.execute(
            "SELECT preorder_id, COALESCE(SUM(amount), 0) AS paid FROM preorder_payments WHERE state='sah' GROUP BY preorder_id"
        ).fetchall()
    }


def _preorder_pending_by_order(con: sqlite3.Connection) -> dict[str, int]:
    """K-18: a draft order's own transfer/QRIS DP that is still waiting for confirmation."""
    return {r["preorder_id"]: r["n"] for r in con.execute(
        "SELECT preorder_id, SUM(amount) AS n FROM preorder_payments WHERE state='pending' GROUP BY preorder_id").fetchall()}


SO_ORDER_SOURCES = ("walk_in", "whatsapp")


def _preorder_source(source: str | None) -> str:
    source = source or "walk_in"
    if source not in SO_ORDER_SOURCES:
        raise ValueError("Sumber Sales Order harus Walk-in atau WhatsApp.")
    return source


def _preorder_row(row: dict, items: list[dict], paid: int = 0, invoice: dict | None = None) -> dict:
    """`paid` is the sum of the order's own payments; the legacy preorders.deposit column is ignored.
    Once the PO has its invoice, paid and remaining are the invoice's (payments are taken there)."""
    total = sum(i["quantity"] * i["unit_price"] for i in items if i["status"] != PO_LINE_CANCELLED)
    out = {
        **row,
        "deposit": paid,
        "total": total,
        # Overpayment would otherwise show as a negative balance owing.
        "remaining": max(total - paid, 0),
        "item_count": len(items),
        "item_names": [i["product_name"] for i in items],
        "status": _preorder_overall_status([i["status"] for i in items]),
        "receipt_no": None, "invoice_total": None, "invoice_status": None, "invoice_work_status": None,
        "invoice_count": (invoice or {}).get("invoice_count", 1),
        "pending_amount": (invoice or {}).get("pending_amount", 0) + (row.get("draft_pending") or 0),
    }
    if invoice:
        out.update({
            "deposit": invoice["amount_paid"],
            "remaining": max(invoice["total"] - invoice["amount_paid"], 0),
            "receipt_no": invoice["receipt_no"], "invoice_total": invoice["total"],
            "invoice_status": invoice["status"], "invoice_work_status": invoice["work_status"],
        })
    live = [i for i in items if i["status"] != PO_LINE_CANCELLED]
    out["procurement_status"] = _preorder_procurement_status(row, live)
    due = out["invoice_total"] if out["invoice_total"] is not None else total
    out["payment_status"] = "belum" if out["deposit"] <= 0 else ("lunas" if out["deposit"] >= due else "dp")
    out["missing_supplier_count"] = sum(
        1 for i in live if i.get("source") in PREORDER_BUY_SOURCES and i.get("supplier_id") is None
        and i["status"] not in PO_ARRIVED_STATUSES
    )
    out["line_suppliers"] = sorted({i["supplier_name"] for i in live if i.get("supplier_name")})
    return out


def _preorder_procurement_status(row: dict, live: list[dict]) -> str:
    """One word for the list's 'Status Pengadaan': how far the goods of the order have got."""
    if row.get("cancelled_at") or (not live and row.get("published_at")):
        return "dibatalkan"
    if row.get("is_draft"):
        return "draft"
    if not live:
        return "menunggu"
    if all(i["status"] == "terpasang" for i in live):
        return "selesai"
    arrived = sum(1 for i in live if i["status"] in PO_ARRIVED_STATUSES)
    if arrived == len(live):
        return "siap"
    if arrived:
        return "tiba_sebagian"
    if any(i["status"] != "pesanan_dibuat" for i in live):
        return "dipesan"
    return "menunggu"


PREORDER_PRIORITIES = ("normal", "urgent")


def _preorder_item_logistics(item: dict) -> tuple:
    """(ship_route, priority, eta_date) of a Sales Order line, validated."""
    route = item.get("ship_route") or None
    if route is not None and route not in SPO_SHIP_METHODS:
        raise ValueError(f"Jalur kirim harus salah satu dari: {', '.join(SPO_SHIP_METHODS)}.")
    priority = item.get("priority") or "normal"
    if priority not in PREORDER_PRIORITIES:
        raise ValueError(f"Prioritas harus salah satu dari: {', '.join(PREORDER_PRIORITIES)}.")
    eta = item.get("eta_date") or None
    if eta is not None:
        try:
            date.fromisoformat(eta)
        except ValueError:
            raise ValueError("ETA harus berformat YYYY-MM-DD.")
    return route, priority, eta


# 'campur' (owner 25-10-2026, "Stok + Pesan Supplier"): what free stock covers is held from the shelf, only the
# rest goes on a draft PO to the line's supplier. One line, one invoice row.
PREORDER_SOURCES = ("supplier", "stok", "jasa", "campur")
PREORDER_BUY_SOURCES = ("supplier", "campur")  # sources whose line names the supplier to buy from
PREORDER_SOURCE_LABELS = {"supplier": "Pesan Supplier", "stok": "Stok Tersedia", "jasa": "Manual / Jasa"}


def _preorder_item_source(con: sqlite3.Connection, item: dict) -> tuple[str | None, int | None]:
    """(source, supplier_id) of a Sales Order line, validated. 'stok' needs a Master Barang item; the
    supplier only matters for 'supplier' lines (it may still be empty: then the line waits for one)."""
    source = item.get("source") or None
    if source is not None and source not in PREORDER_SOURCES:
        raise ValueError("Sumber barang harus Pesan Supplier, Stok Tersedia, Stok + Pesan Supplier, atau Manual / Jasa.")
    if source == "stok" and not item.get("master_item_id"):
        raise ValueError(f"“{item['product_name']}”: Stok Tersedia hanya untuk barang dari Master Barang.")
    if source == "campur" and not item.get("master_item_id"):
        raise ValueError(f"“{item['product_name']}”: Stok + Pesan Supplier hanya untuk barang dari Master Barang.")
    supplier_id = item.get("supplier_id") if source in PREORDER_BUY_SOURCES else None
    if supplier_id is not None:
        sup = con.execute("SELECT is_active FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if sup is None or not sup["is_active"]:
            raise ValueError(f"“{item['product_name']}”: supplier tidak ditemukan atau tidak aktif.")
    return source, supplier_id


def _require_stock_free(con: sqlite3.Connection, item: dict, own_line_id: int | None = None) -> None:
    """A line set to 'Stok Tersedia' must be covered by what is free on the shelf right now (stock minus what open
    Sales Orders already hold), otherwise it would be promised from stock that does not exist. A line the order
    already holds counts its own reservation as free. Pick 'Pesan Supplier' (or leave it automatic) for the rest."""
    mid = item.get("master_item_id")
    if item.get("source") != "stok" or not mid:
        return
    mi = con.execute("SELECT stock_qty, unit FROM master_items WHERE id=?", (mid,)).fetchone()
    if mi is None:
        return
    held = _reserved_by_item(con, [mid]).get(mid, 0)
    if own_line_id:
        held -= next((ln["reserved"] for ln in _reservation_lines(con, [mid]) if ln["id"] == own_line_id), 0)
    free = max(max(mi["stock_qty"], 0) - held, 0)
    if item["quantity"] > free:
        raise ValueError(
            f"“{item['product_name']}”: stok yang bebas hanya {free} {mi['unit'] or 'unit'}, kurang dari {item['quantity']} yang dipesan. "
            "Ubah sumbernya ke Stok + Pesan Supplier (sisanya dibuatkan PO), atau kurangi jumlahnya.")


def _forbid_supplier_when_stocked(con: sqlite3.Connection, item: dict) -> None:
    """Owner 25-10-2026: while the warehouse can cover the whole line from free stock, it may not be set to
    'Pesan Supplier' -- take it from stock. A line the shelf can only partly cover may still be ordered."""
    mid = item.get("master_item_id")
    if item.get("source") != "supplier" or not mid:
        return
    mi = con.execute("SELECT stock_qty, unit FROM master_items WHERE id=?", (mid,)).fetchone()
    if mi is None:
        return
    free = max(max(mi["stock_qty"], 0) - _reserved_by_item(con, [mid]).get(mid, 0), 0)
    if free >= item["quantity"]:
        raise ValueError(
            f"“{item['product_name']}”: stok gudang masih ada {free} {mi['unit'] or 'unit'}, cukup untuk {item['quantity']} yang dipesan. "
            "Pilih sumber Stok Tersedia, bukan Pesan Supplier.")


def _insert_preorder_item(con: sqlite3.Connection, preorder_id: str, item: dict, position: int, now: str) -> int:
    _require_stock_free(con, item)
    _forbid_supplier_when_stocked(con, item)
    price = _price_review(
        con, scope="preorder", doc_id=preorder_id, master_item_id=item.get("master_item_id"), product_name=item["product_name"],
        qty=item["quantity"], unit_price=item["unit_price"], line_discount=0, prev=None, reason=item.get("price_reason"),
        allow_below_cost=bool(item.get("allow_below_cost")), actor=item.get("_price_actor"))
    cur = con.execute(
        """INSERT INTO preorder_items
           (preorder_id, master_item_id, product_name, unit, quantity, unit_price, status, position, created_at,
            ship_route, priority, eta_date, source, supplier_id,
            list_price, price_state, price_reason, price_requested_by, price_requested_at, price_approved_by,
            price_approved_at, price_below_cost, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            preorder_id, item.get("master_item_id"), item["product_name"], item.get("unit"),
            item["quantity"], item["unit_price"], item.get("status") or PREORDER_STATUSES[0],
            position, now, *_preorder_item_logistics(item), *_preorder_item_source(con, item),
            *[price[c] for c in _PRICE_COLS], (item.get("note") or "").strip() or None,
        ),
    )
    item_id = cur.lastrowid
    _log_preorder_status(con, preorder_id, item_id, item["product_name"],
                         item.get("status") or PREORDER_STATUSES[0], now, item.get("changed_by"))
    return item_id


def _log_preorder_status(
    con: sqlite3.Connection, preorder_id: str, item_id: int, product_name: str,
    status: str, changed_at: str, changed_by: str | None, old_status: str | None = None,
) -> None:
    """`old_status` is null for a line's first entry (it had no previous stage)."""
    con.execute(
        """INSERT INTO preorder_status_history
           (preorder_id, preorder_item_id, product_name, status, old_status, changed_at, changed_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (preorder_id, item_id, product_name, status, old_status, changed_at, changed_by),
    )


def _general_supplier(con: sqlite3.Connection) -> dict:
    """The catch-all supplier "UMUM" (barang that can be bought anywhere); created on first use."""
    key = _normalize_name("UMUM")
    row = con.execute("SELECT * FROM suppliers WHERE name_normalized=? AND is_active=1", (key,)).fetchone()
    if row is not None:
        return dict(row)
    cur = con.execute(
        """INSERT INTO suppliers (code, name, is_active, created_at, name_normalized, is_system, source)
           VALUES (?, 'UMUM', 1, ?, ?, 0, 'quick_add')""",
        (_generate_entity_code(con, "suppliers", "UMUM"), _now(), key),
    )
    return dict(con.execute("SELECT * FROM suppliers WHERE id=?", (cur.lastrowid,)).fetchone())


def _auto_supplier_pos_for_preorder(con: sqlite3.Connection, preorder_id: str, by: str | None) -> list[str]:
    """A published Sales Order gets draft Purchase Order(s) for what the shop can't supply from stock:
    the quantity that is neither reserved from the shelf nor already on a Purchase Order goes on a new
    draft, one per supplier (barang with no supplier go to UMUM). Lines already at the shop need none.
    Safe to call repeatedly: drafts count as ordered."""
    from uuid import uuid4
    po = con.execute("SELECT po_number, is_draft FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None or po["is_draft"]:
        return []
    groups: dict[int, list[dict]] = {}
    candidates = [r for r in _spo_candidate_rows(con, None, None) if r["preorder_id"] == preorder_id]
    # K-02 / rule A: what the shelf can hold for this order is reserved, only the rest is bought.
    # Older orders keep their claim on the shelf first. A line with no master item can't come from stock.
    res_lines = _reservation_lines(con, [r["master_item_id"] for r in candidates])
    short = {ln["id"]: ln["shortfall"] for ln in res_lines}
    for r in candidates:
        if r["source"] == "supplier":
            # Chosen per line: the whole quantity is bought, and nothing is drafted until a supplier is picked.
            if r["supplier_id"] is None:
                continue
            qty = r["qty_left"]
        elif r["source"] == "campur":
            # Stok + Pesan Supplier: the shelf keeps what it can; only the shortfall is bought, from the line's supplier.
            if r["supplier_id"] is None:
                continue
            qty = min(r["qty_left"], short.get(r["preorder_item_id"], 0))
        else:
            qty = r["qty_left"] if r["master_item_id"] is None else min(r["qty_left"], short.get(r["preorder_item_id"], 0))
        if qty <= 0:
            continue
        sid = r["supplier_id"] if r["supplier_id"] is not None else _general_supplier(con)["id"]
        groups.setdefault(sid, []).append({
            "preorder_item_id": r["preorder_item_id"], "master_item_id": r["master_item_id"],
            "product_name": r["product_name"], "unit": r["unit"], "quantity": qty,
            "est_unit_price": r["est_unit_price"], "discount_pct": 0,
        })
    now, po_date, created = _now(), shop_today().isoformat(), []
    for supplier_id, items in groups.items():
        supplier = _spo_supplier(con, supplier_id)
        _spo_validate_items(con, supplier_id, items, None)
        spo_id = str(uuid4())
        con.execute(
            """INSERT INTO supplier_pos
               (id, po_number, po_date, supplier_id, supplier_name_snapshot, notes, status,
                created_by, created_at, updated_at, discount_pct, shipping_cost, tax_rate)
               VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, 0, 0, 0)""",
            (spo_id, _next_supplier_po_no(con, po_date), po_date, supplier_id, supplier["name"],
             f"Otomatis dari Sales Order {po['po_number']}", by, now, now),
        )
        _spo_replace_items(con, spo_id, items)
        _spo_event(con, spo_id, "created", f"Draft dibuat otomatis dari Sales Order {po['po_number']}", by)
        created.append(spo_id)
    # A service (jasa) needs no goods: it is ready to be done as soon as the order is published.
    jasa = [dict(r) for r in con.execute(
        "SELECT * FROM preorder_items WHERE preorder_id=? AND source='jasa' AND status='pesanan_dibuat'", (preorder_id,))]
    now = _now()
    for ln in jasa:
        con.execute("UPDATE preorder_items SET status='sampai_di_toko' WHERE id=?", (ln["id"],))
        _log_preorder_status(con, preorder_id, ln["id"], ln["product_name"], "sampai_di_toko", now, by, "pesanan_dibuat")
    moved = _promote_shelf_ready_lines(con, by, preorder_id=preorder_id, sync=False)
    if jasa or moved:
        _po_invoice_sync(con, preorder_id, by)
    return created


def _promote_shelf_ready_lines(con: sqlite3.Connection, by: str | None, preorder_id: str | None = None,
                               master_item_ids: list[int] | None = None, sync: bool = True) -> list[str]:
    """A published Sales Order line the shelf now holds completely (K-02 reservation, nothing still to come
    from a supplier) is already at the shop: it moves to sampai_di_toko and waits for installation like a
    delivered one, which also lets its invoice leave Menunggu Barang. Run when an order is published or
    changed (`preorder_id`) and whenever stock is added (`master_item_ids`): a 'Stok Tersedia' line that was
    short at publish becomes ready once the goods are on the shelf. Never moves a line backwards.
    Returns the ids of the orders that changed (their invoices are synced unless sync=False)."""
    if preorder_id is not None:
        master_item_ids = [r[0] for r in con.execute(
            "SELECT DISTINCT master_item_id FROM preorder_items WHERE preorder_id=? AND master_item_id IS NOT NULL", (preorder_id,))]
    if master_item_ids is not None and not master_item_ids:
        return []
    ready = [ln for ln in _reservation_lines(con, master_item_ids)
             if ln["status"] == "pesanan_dibuat" and ln["coming"] == 0 and ln["reserved"] >= ln["quantity"]
             and (preorder_id is None or ln["preorder_id"] == preorder_id)]
    now = _now()
    for ln in ready:
        con.execute("UPDATE preorder_items SET status='sampai_di_toko' WHERE id=? AND status='pesanan_dibuat'", (ln["id"],))
        _log_preorder_status(con, ln["preorder_id"], ln["id"], ln["product_name"], "sampai_di_toko", now, by, "pesanan_dibuat")
    changed = sorted({ln["preorder_id"] for ln in ready})
    if sync:
        for pid in changed:
            _po_invoice_sync(con, pid, by)
    return changed


def create_preorder(
    preorder_id: str,
    customer_name: str,
    customer_phone: str,
    items: list[dict],
    deposit: int = 0,
    notes: str | None = None,
    changed_by: str | None = None,
    plate_full: str = "",
    po_date: str | None = None,
    is_draft: bool = False,
    deposit_method: str = "cash",
    supplier_id: int | None = None,
    deposit_confirmed: bool | None = None,
    price_actor: dict | None = None,
    order_source: str | None = None,
    deposit_proof: tuple[str, str | None] | None = None,
) -> dict:
    """A deposit given at creation is recorded as the first payment ("DP awal"); on a published PO it
    is booked straight onto the PO's invoice, which is created here too. `deposit_proof` = (stored file, original
    name) of the transfer screenshot; a WhatsApp order paying its DP by transfer/QRIS must have one."""
    if deposit > 0 and deposit_method in PENDING_METHODS and _preorder_source(order_source) == "whatsapp" and not deposit_proof:
        raise ValueError("Bukti transfer wajib diunggah untuk DP pesanan WhatsApp.")
    now = _now()
    po_date = po_date or shop_today().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO preorders
               (id, po_number, po_date, plate_full, customer_name, customer_phone, deposit, notes,
                created_at, updated_at, is_draft, supplier_id, order_source)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (preorder_id, _next_po_no(con, po_date), po_date, plate_full, customer_name,
             customer_phone, notes, now, now, int(is_draft), supplier_id, _preorder_source(order_source)),
        )
        if deposit > 0:
            con.execute(
                "INSERT INTO preorder_payments (preorder_id, paid_at, amount, note, created_by, proof_file, proof_name, proof_by, proof_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (preorder_id, now, deposit, "DP awal", changed_by, *(deposit_proof or (None, None)),
                 changed_by if deposit_proof else None, now if deposit_proof else None),
            )
        for position, item in enumerate(items):
            _insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by, "_price_actor": price_actor}, position, now)
        if deposit > 0 and _preorder_price_pending(con, preorder_id):
            raise ValueError(_PRICE_PENDING_MSG)
        if deposit > 0 and deposit_method != "cash":
            con.execute("UPDATE preorder_payments SET method=? WHERE preorder_id=?", (deposit_method, preorder_id))
            # K-18: a transfer/QRIS DP nobody has confirmed yet is Pending; it does not count towards the 30% to publish.
            if _pay_pending(deposit_method, deposit_confirmed):
                con.execute("UPDATE preorder_payments SET state='pending', note='DP awal, menunggu konfirmasi dana masuk' WHERE preorder_id=?",
                            (preorder_id,))
            elif deposit_method in PENDING_METHODS:
                con.execute("UPDATE preorder_payments SET confirmed_by=?, confirmed_at=? WHERE preorder_id=?", (changed_by, now, preorder_id))
        if not is_draft:
            _require_publish_dp(con, preorder_id)
            con.execute("UPDATE preorders SET published_at=? WHERE id=?", (now, preorder_id))
        # A published PO gets its invoice now; the DP lands on it.
        receipt_id = _po_invoice_sync(con, preorder_id, changed_by)
        if receipt_id:
            _po_move_payments_to_invoice(con, preorder_id, receipt_id, changed_by)
        _auto_supplier_pos_for_preorder(con, preorder_id, changed_by)
        _link_preorder_vehicle(con, preorder_id)
    return get_preorder(preorder_id)


_PREORDER_ITEMS_SQL = """SELECT pi.*, s.name AS supplier_name FROM preorder_items pi
                         LEFT JOIN suppliers s ON s.id = pi.supplier_id"""


def get_preorder(preorder_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT *, (SELECT name FROM suppliers WHERE id=preorders.supplier_id) AS supplier_name FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            return None
        items = [dict(r) for r in con.execute(
            f"{_PREORDER_ITEMS_SQL} WHERE pi.preorder_id=? ORDER BY pi.position ASC, pi.id ASC", (preorder_id,)
        ).fetchall()]
        paid = con.execute(
            "SELECT COALESCE(SUM(amount), 0) AS paid FROM preorder_payments WHERE preorder_id=? AND state='sah'", (preorder_id,)
        ).fetchone()["paid"]
        invoice = po_invoices(con, [row["receipt_id"]]).get(row["receipt_id"])
        out = _preorder_row({**dict(row), "draft_pending": _preorder_pending_by_order(con).get(preorder_id, 0)}, items, paid, invoice)
        out["invoices"] = [{
            "id": i["id"], "receipt_no": i["receipt_no"], "invoice_seq": i["invoice_seq"], "total": i["total"],
            "amount_paid": i["amount_paid"], "status": i["status"], "work_status": i["work_status"],
            "split_reason": i["split_reason"], "pending_amount": _pending_sum(con, i["id"]),
            "item_ids": [r["id"] for r in con.execute(
                "SELECT id FROM preorder_items WHERE preorder_id=? AND receipt_id=? AND status<>? ORDER BY position, id",
                (preorder_id, i["id"], PO_LINE_CANCELLED))] if i["status"] != "void" else [],
        } for i in _po_invoices(con, preorder_id, include_void=True)]
    return out


def get_preorder_items(preorder_id: str) -> list[dict]:
    """Carries the linked SKU along so the detail view can show which lines are
    tied to Master Barang; it is null for a free-text line."""
    with _conn() as con:
        rows = con.execute(
            """SELECT preorder_items.*, master_items.sku AS sku, suppliers.name AS supplier_name,
                      master_items.sell_price AS master_price, master_items.avg_cost_price AS avg_cost_price
               FROM preorder_items
               LEFT JOIN master_items ON master_items.id = preorder_items.master_item_id
               LEFT JOIN suppliers ON suppliers.id = preorder_items.supplier_id
               WHERE preorder_id=? ORDER BY position ASC, preorder_items.id ASC""",
            (preorder_id,),
        ).fetchall()
        items = [dict(r) for r in rows]
        # Two-way reference: which supplier orders carry each line (and how much of it has arrived).
        for it in items:
            links = con.execute(
                """SELECT sp.id AS supplier_po_id, sp.po_number, sp.status AS po_status, spi.quantity - spi.qty_cancelled AS qty_ordered,
                          spi.qty_received, spi.id AS spo_item_id
                   FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                   WHERE spi.preorder_item_id=? AND sp.status != 'batal' AND spi.quantity - spi.qty_cancelled > 0
                   ORDER BY sp.created_at ASC""",
                (it["id"],),
            ).fetchall()
            it["supplier_pos"] = [dict(l) for l in links]
            # Paket Pengiriman of each linked PO line: where this line's goods are on the road.
            for link in it["supplier_pos"]:
                link["packages"] = [
                    {"package_no": p["package_no"], "forwarder_name": p["forwarder_name"], "ship_method": p["ship_method"],
                     "state": p["state"], "eta_date": p["eta_date"], "tracking_no": p["tracking_no"], "qty": ln["quantity"],
                     "received": ln["received"]}
                    for p in _spo_packages(con, link["supplier_po_id"]) if not p["cancelled"]
                    for ln in p["lines"] if ln["item_id"] == link["spo_item_id"]]
        held = {ln["id"]: ln for ln in _reservation_lines(con, [it["master_item_id"] for it in items])}
        for it in items:
            ln = held.get(it["id"])
            it["reserved_qty"] = ln["reserved"] if ln else 0
            it["coming_qty"] = ln["coming"] if ln else 0
            it["shortfall_qty"] = ln["shortfall"] if ln else 0
        return items


def get_preorder_history(preorder_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM preorder_status_history WHERE preorder_id=?
               ORDER BY changed_at DESC, id DESC""",
            (preorder_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_preorder_payments(preorder_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM preorder_payments WHERE preorder_id=? ORDER BY paid_at ASC, id ASC",
            (preorder_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_preorder_payment(
    preorder_id: str, amount: int, note: str | None = None, created_by: str | None = None, method: str = "cash",
    confirmed: bool | None = None,
) -> dict:
    now = _now()
    with _conn() as con:
        if con.execute("SELECT 1 FROM preorders WHERE id=?", (preorder_id,)).fetchone() is None:
            raise ValueError(f"Preorder {preorder_id} tidak ditemukan.")
        if _preorder_price_pending(con, preorder_id):
            raise ValueError(_PRICE_PENDING_MSG)
        pending = _pay_pending(method, confirmed)
        cur = con.execute(
            "INSERT INTO preorder_payments (preorder_id, paid_at, amount, note, created_by, method, state, confirmed_by, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (preorder_id, now, amount, (note or "Menunggu konfirmasi dana masuk") if pending else note, created_by, method,
             "pending" if pending else "sah", created_by if (not pending and method in PENDING_METHODS) else None,
             now if (not pending and method in PENDING_METHODS) else None),
        )
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        return dict(con.execute("SELECT * FROM preorder_payments WHERE id=?", (cur.lastrowid,)).fetchone())


def confirm_preorder_payment(preorder_id: str, payment_id: int, by: str | None = None) -> dict:
    """K-18: a draft Sales Order's transfer/QRIS DP has been seen in the account. It counts towards the 30% from now."""
    with _conn() as con:
        pay = con.execute("SELECT * FROM preorder_payments WHERE id=? AND preorder_id=?", (payment_id, preorder_id)).fetchone()
        if pay is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if pay["state"] != "pending":
            raise ValueError("Pembayaran ini bukan lagi berstatus menunggu konfirmasi.")
        now = _now()
        con.execute("UPDATE preorder_payments SET note=NULL WHERE id=? AND note=?", (payment_id, PAYMENT_WAITING_NOTE))
        con.execute("UPDATE preorder_payments SET state='sah', confirmed_by=?, confirmed_at=?, paid_at=? WHERE id=?", (by, now, now, payment_id))
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        return dict(con.execute("SELECT * FROM preorder_payments WHERE id=?", (payment_id,)).fetchone())


def fail_preorder_payment(preorder_id: str, payment_id: int, reason: str, by: str | None = None) -> dict:
    if not reason or not reason.strip():
        raise ValueError("Alasan gagal wajib diisi.")
    with _conn() as con:
        pay = con.execute("SELECT * FROM preorder_payments WHERE id=? AND preorder_id=?", (payment_id, preorder_id)).fetchone()
        if pay is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if pay["state"] != "pending":
            raise ValueError("Hanya pembayaran yang menunggu konfirmasi yang bisa ditandai gagal.")
        con.execute("UPDATE preorder_payments SET state='gagal', fail_reason=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                    (reason.strip(), by, _now(), payment_id))
        return dict(con.execute("SELECT * FROM preorder_payments WHERE id=?", (payment_id,)).fetchone())


def list_pending_payments() -> dict:
    """K-18: every transfer/QRIS payment still waiting for someone to see the money in the account, oldest first --
    on notas and on draft Sales Orders -- with the total."""
    with _conn() as con:
        rows = [dict(r, source="nota") for r in con.execute(
            """SELECT p.id, p.receipt_id AS ref_id, p.paid_at, p.amount, p.method, p.transfer_sender, p.transfer_account, p.note,
                      p.created_by, r.receipt_no AS ref_no, r.plate_full, COALESCE(r.customer_name, '') AS customer_name
               FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id
               WHERE p.state='pending' AND r.status <> 'void'""").fetchall()]
        rows += [dict(r, source="so", transfer_sender=None, transfer_account=None) for r in con.execute(
            """SELECT p.id, p.preorder_id AS ref_id, p.paid_at, p.amount, p.method, p.note, p.created_by,
                      o.po_number AS ref_no, o.plate_full, o.customer_name
               FROM preorder_payments p JOIN preorders o ON o.id = p.preorder_id
               WHERE p.state='pending'""").fetchall()]
    rows.sort(key=lambda r: (r["paid_at"], r["id"]))
    return {"items": rows, "total": sum(r["amount"] for r in rows), "count": len(rows)}


def get_preorder_allocations(preorder_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT a.*, r.receipt_no, r.total AS receipt_total, r.status AS receipt_status
               FROM preorder_payment_allocations a LEFT JOIN receipts r ON r.id = a.receipt_id
               WHERE a.preorder_id=? ORDER BY a.id ASC""", (preorder_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def preorder_unallocated_dp(con: sqlite3.Connection, preorder_id: str) -> int:
    paid = con.execute(
        "SELECT COALESCE(SUM(amount),0) AS n FROM preorder_payments WHERE preorder_id=? AND state='sah'", (preorder_id,)
    ).fetchone()["n"]
    used = con.execute(
        "SELECT COALESCE(SUM(a.amount),0) AS n FROM preorder_payment_allocations a "
        "LEFT JOIN receipts r ON r.id = a.receipt_id WHERE a.preorder_id=? AND COALESCE(r.status,'') <> 'void'",
        (preorder_id,),
    ).fetchone()["n"]
    return max(paid - used, 0)


def preorder_convertible_items(preorder_id: str, item_ids: list[int] | None = None) -> list[dict]:
    """Lines that have arrived (or been fitted) and are not on a nota yet."""
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT pi.*, mi.sku AS sku FROM preorder_items pi
               LEFT JOIN master_items mi ON mi.id = pi.master_item_id
               WHERE pi.preorder_id=? ORDER BY pi.position ASC, pi.id ASC""", (preorder_id,)
        ).fetchall()]
    rows = [r for r in rows if r.get("receipt_id") is None and r["status"] in PO_ARRIVED_STATUSES]
    if item_ids is not None:
        rows = [r for r in rows if r["id"] in set(item_ids)]
    return rows


def apply_preorder_dp(preorder_id: str, receipt_id: str, item_ids: list[int], by: str | None) -> int:
    """One transaction: link the lines to the nota, allocate the unallocated DP (capped at the
    nota's total) and book it as a payment on the nota. Returns the Rp applied."""
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        po = con.execute("SELECT po_number FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        marked = 0
        for iid in item_ids:
            marked += con.execute(
                "UPDATE preorder_items SET receipt_id=? WHERE id=? AND preorder_id=? AND receipt_id IS NULL",
                (receipt_id, iid, preorder_id),
            ).rowcount
        if marked != len(item_ids):
            raise ValueError("Barang Sales Order ini baru saja dipakai di nota lain. Muat ulang lalu coba lagi.")
        amount = min(preorder_unallocated_dp(con, preorder_id), rec["total"])
        if amount > 0:
            con.execute(
                "INSERT INTO preorder_payment_allocations (preorder_id, receipt_id, amount, allocated_at, allocated_by) "
                "VALUES (?, ?, ?, ?, ?)", (preorder_id, receipt_id, amount, now, by),
            )
            new_paid = rec["amount_paid"] + amount
            new_status = _compute_receipt_status(rec["total"], new_paid, None)
            # The DP is booked in the methods it was paid with: oldest payments are used first, and
            # what earlier notas already took is skipped.
            already = con.execute(
                "SELECT COALESCE(SUM(a.amount),0) AS n FROM preorder_payment_allocations a LEFT JOIN receipts r ON r.id=a.receipt_id "
                "WHERE a.preorder_id=? AND a.receipt_id<>? AND COALESCE(r.status,'')<>'void'", (preorder_id, receipt_id),
            ).fetchone()["n"]
            chunks, skip, left = [], already, amount
            for pp in con.execute("SELECT amount, method FROM preorder_payments WHERE preorder_id=? AND state='sah' ORDER BY id ASC", (preorder_id,)):
                avail = pp["amount"]
                if skip >= avail:
                    skip -= avail
                    continue
                avail -= skip
                skip = 0
                take = min(avail, left)
                if take > 0:
                    chunks.append((take, pp["method"] or "cash"))
                    left -= take
                if left <= 0:
                    break
            main_method = chunks[-1][1] if chunks else "cash"
            con.execute(
                "UPDATE receipts SET amount_paid=?, status=?, payment_method=? WHERE id=?",
                (new_paid, new_status, main_method, receipt_id),
            )
            for take, method in chunks:
                _insert_receipt_payment(con, receipt_id, take, method, f"DP dari {po['po_number']}", by)
        _log_receipt_event(con, receipt_id, "created",
                           f"Dibuat dari {po['po_number']}" + (f"; DP Rp {amount:,} dialokasikan".replace(",", ".") if amount else ""), by)
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        return amount


# --- Sales Order -> Invoice ---------------------------------------------------
# A published Sales Order owns one invoice (a receipts row, preorders.receipt_id). Its lines carry
# receipt_items.preorder_item_id and follow the PO: added, changed or removed on the PO, the invoice
# changes with it. Money is only ever taken on the invoice. A PO line's goods are taken off the shelf
# when that line has arrived (see _receipt_item_holdings); until every line has, the invoice sits in
# "Menunggu Barang".

_PLATE_RE = re.compile(r"^([A-Z]{1,2})\s+(\d{1,4})\s+([A-Z]{1,3})$")


def split_plate(plate_full: str | None) -> tuple[str, str, str] | None:
    """'B 1234 ABC' -> ('B', '1234', 'ABC') when it is a plate the cashier accepts, else None."""
    m = _PLATE_RE.match(" ".join((plate_full or "").upper().split()))
    if not m:
        return None
    from plate_codes import PLATE_CODES
    return m.groups() if m.group(1) in PLATE_CODES else None


def _receipt_take_payment(
    con: sqlite3.Connection, receipt_id: str, amount: int, method: str, note: str | None, by: str | None,
    paid_at: str | None = None, transfer_sender: str | None = None, transfer_account: str | None = None,
) -> None:
    """Books a payment on an invoice inside the caller's transaction."""
    row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    new_paid = row["amount_paid"] + amount
    con.execute(
        "UPDATE receipts SET amount_paid=?, status=?, payment_method=? WHERE id=?",
        (new_paid, _compute_receipt_status(row["total"], new_paid, None), method, receipt_id),
    )
    _insert_receipt_payment(con, receipt_id, amount, method, note, by, transfer_sender, transfer_account, paid_at)


def _po_invoice_holdings(con: sqlite3.Connection, receipt_id: str | None) -> dict[int, int]:
    if not receipt_id:
        return {}
    rec = con.execute("SELECT status, work_status FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if rec is None or not _holds_stock(rec["status"], rec["work_status"]):
        return {}
    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)).fetchall()]
    return _receipt_item_holdings(items, con)


def _po_invoice_snapshot(con: sqlite3.Connection, preorder_id: str) -> dict[str, dict[int, int]] | None:
    """What each of the PO's invoices holds now ({invoice id: holdings}), taken before a PO change so the
    stock move can be worked out. None while the PO has no invoice yet."""
    ids = [r["id"] for r in con.execute("SELECT id FROM receipts WHERE preorder_id=? AND status<>'void'", (preorder_id,))]
    return {rid: _po_invoice_holdings(con, rid) for rid in ids} if ids else None


def _po_invoices(con: sqlite3.Connection, preorder_id: str, include_void: bool = False) -> list[dict]:
    """The Sales Order's invoices, main one first (K-19: normally exactly one)."""
    return [dict(r) for r in con.execute(
        f"SELECT * FROM receipts WHERE preorder_id=? {'' if include_void else 'AND status<>' + chr(39) + 'void' + chr(39)} "
        "ORDER BY invoice_seq ASC, created_at ASC", (preorder_id,)).fetchall()]


def _po_create_invoice(con: sqlite3.Connection, po: dict, by: str | None, seq: int = 1,
                       work_status: str = "menunggu", reason: str | None = None) -> str:
    """Opens an invoice for the Sales Order. The first (seq 1) becomes the order's main invoice; a later one
    is an Invoice Lanjutan (K-19) that shares the order, plate and customer."""
    plate = split_plate(po["plate_full"]) if (po["plate_full"] or "").strip() else ("", "", "")
    if plate is None:
        raise ValueError("Plat mobil di Sales Order harus berformat seperti B 1234 ABC supaya invoicenya bisa dibuat.")
    from uuid import uuid4
    receipt_id = str(uuid4())
    created_at = _now()
    customer_id, phone, name = _resolve_receipt_customer(con, None, po["customer_phone"], po["customer_name"])
    con.execute(
        """INSERT INTO receipts
           (id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
            subtotal, discount, total, created_at, started_at, status, amount_paid, payment_method, customer_id,
            receipt_no, work_status, preorder_id, invoice_seq, split_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 'pending', 0, 'cash', ?, ?, ?, ?, ?, ?)""",
        (receipt_id, *plate, " ".join(x for x in plate if x), phone, name, created_at, created_at, customer_id,
         _next_receipt_no(con, created_at), work_status, po["id"], seq, reason),
    )
    if seq == 1:
        con.execute("UPDATE preorders SET receipt_id=? WHERE id=?", (receipt_id, po["id"]))
    _link_receipt_vehicle(con, receipt_id, by)
    if seq == 1:
        _log_receipt_event(con, receipt_id, "created", f"Invoice dibuat dari Sales Order {po['po_number']}", by)
    else:
        _log_receipt_event(con, receipt_id, "created",
                           f"Invoice Lanjutan {seq} dari Sales Order {po['po_number']}" + (f". Alasan: {reason}" if reason else ""), by)
    return receipt_id


def _carry_payment_proof(con: sqlite3.Connection, receipt_id: str, pp) -> None:
    """The invoice payment just booked from a Sales Order payment keeps its transfer proof."""
    if pp["proof_file"]:
        con.execute("""UPDATE receipt_payments SET proof_file=?, proof_name=?, proof_by=?, proof_at=?
                       WHERE id=(SELECT MAX(id) FROM receipt_payments WHERE receipt_id=?)""",
                    (pp["proof_file"], pp["proof_name"], pp["proof_by"], pp["proof_at"], receipt_id))


def set_so_payment_proof(preorder_id: str, payment_id: int, on_invoice: bool, filename: str, original_name: str | None,
                         by: str | None) -> str | None:
    """Attaches (or replaces) the transfer proof of a Sales Order payment: one on its invoice (`on_invoice`) or, before
    the invoice exists, the Sales Order's own. Returns the previous file so the caller can remove it."""
    with _conn() as con:
        if on_invoice:
            row = con.execute("""SELECT p.* FROM receipt_payments p JOIN receipts r ON r.id=p.receipt_id
                                 WHERE p.id=? AND r.preorder_id=?""", (payment_id, preorder_id)).fetchone()
            table = "receipt_payments"
        else:
            row = con.execute("SELECT * FROM preorder_payments WHERE id=? AND preorder_id=?", (payment_id, preorder_id)).fetchone()
            table = "preorder_payments"
        if row is None:
            raise ValueError("Pembayaran tidak ditemukan.")
        if row["amount"] <= 0 or row["method"] not in PENDING_METHODS:
            raise ValueError("Bukti transfer hanya untuk pembayaran transfer atau QRIS.")
        con.execute(f"UPDATE {table} SET proof_file=?, proof_name=?, proof_by=?, proof_at=? WHERE id=?",
                    (filename, (original_name or "")[:120] or None, by, _now(), payment_id))
        if on_invoice:
            _log_receipt_event(con, row["receipt_id"], "payment", f"Bukti transfer pembayaran {_rp(row['amount'])} diunggah", by)
        return row["proof_file"]


def get_so_payment_proof(preorder_id: str, payment_id: int, on_invoice: bool) -> str | None:
    with _conn() as con:
        row = con.execute("""SELECT p.proof_file FROM receipt_payments p JOIN receipts r ON r.id=p.receipt_id
                             WHERE p.id=? AND r.preorder_id=?""" if on_invoice else
                          "SELECT proof_file FROM preorder_payments WHERE id=? AND preorder_id=?", (payment_id, preorder_id)).fetchone()
        return row["proof_file"] if row else None


def _po_move_payments_to_invoice(con: sqlite3.Connection, preorder_id: str, receipt_id: str, by: str | None) -> int:
    """Payments recorded on the PO itself (a draft PO's DP, or a PO from before invoices existed)
    are booked on the invoice once, keeping their date and method, through the allocation ledger."""
    # K-18: DP still waiting for confirmation moves to the invoice as a Pending payment (confirmed there).
    for pp in con.execute("SELECT * FROM preorder_payments WHERE preorder_id=? AND state='pending' ORDER BY id ASC", (preorder_id,)).fetchall():
        _insert_receipt_payment(con, receipt_id, pp["amount"], pp["method"] or "transfer", pp["note"] or "DP, menunggu konfirmasi dana masuk",
                                pp["created_by"], paid_at=pp["paid_at"], state="pending")
        _carry_payment_proof(con, receipt_id, pp)
        con.execute("UPDATE preorder_payments SET state='dipindah' WHERE id=?", (pp["id"],))
    free = preorder_unallocated_dp(con, preorder_id)
    if free <= 0:
        return 0
    already = con.execute(
        "SELECT COALESCE(SUM(a.amount),0) AS n FROM preorder_payment_allocations a LEFT JOIN receipts r ON r.id=a.receipt_id "
        "WHERE a.preorder_id=? AND COALESCE(r.status,'')<>'void'", (preorder_id,),
    ).fetchone()["n"]
    skip, left = already, free
    for pp in con.execute("SELECT * FROM preorder_payments WHERE preorder_id=? AND state='sah' ORDER BY id ASC", (preorder_id,)).fetchall():
        avail = pp["amount"]
        if skip >= avail:
            skip -= avail
            continue
        avail -= skip
        skip = 0
        take = min(avail, left)
        if take > 0:
            _receipt_take_payment(con, receipt_id, take, pp["method"] or "cash", pp["note"] or "Pembayaran PO",
                                  pp["created_by"], pp["paid_at"])
            _carry_payment_proof(con, receipt_id, pp)
            left -= take
        if left <= 0:
            break
    con.execute(
        "INSERT INTO preorder_payment_allocations (preorder_id, receipt_id, amount, allocated_at, allocated_by) VALUES (?, ?, ?, ?, ?)",
        (preorder_id, receipt_id, free, _now(), by),
    )
    return free


def _po_invoice_sync(
    con: sqlite3.Connection, preorder_id: str, by: str | None, before: dict[str, dict[int, int]] | None = None,
) -> str | None:
    """Makes the PO's invoice(s) match the PO: creates the main invoice the first time, adds/changes/removes
    the PO lines, recomputes the totals, moves Menunggu Barang on once everything has arrived, and moves
    stock for lines that have arrived. `before` is {invoice id: holdings} from _po_invoice_snapshot.
    K-19: every line belongs to exactly one invoice (preorder_items.receipt_id); a line that has no
    owner yet (a new one) or whose owner was voided goes to the newest invoice still open. Returns the
    main invoice id (None for a draft PO)."""
    po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None or po["is_draft"]:
        return None
    po = dict(po)
    main_id = po.get("receipt_id")
    created = False
    if not main_id:
        main_id = _po_create_invoice(con, po, by)
        created = True
        before = {**(before or {}), main_id: {}}
    main = con.execute("SELECT * FROM receipts WHERE id=?", (main_id,)).fetchone()
    if main["preorder_id"] is None:
        con.execute("UPDATE receipts SET preorder_id=? WHERE id=?", (preorder_id, main_id))
    if main["status"] == "void":
        return main_id
    invoices = _po_invoices(con, preorder_id)
    open_ones = [i for i in invoices if not _is_closed(i)]
    target = (open_ones or invoices)[-1]
    po_items = [dict(r) for r in con.execute(
        "SELECT * FROM preorder_items WHERE preorder_id=? AND status<>? ORDER BY position ASC, id ASC",
        (preorder_id, PO_LINE_CANCELLED),
    ).fetchall()]
    active_ids = {i["id"] for i in invoices}
    mine: dict[str, list[dict]] = {i["id"]: [] for i in invoices}
    for pi in po_items:
        mine[pi["receipt_id"] if pi.get("receipt_id") in active_ids else target["id"]].append(pi)
    for inv in invoices:
        _po_invoice_sync_one(con, po, inv["id"], mine[inv["id"]], created and inv["id"] == main_id,
                             (before or {}).get(inv["id"]), by)
    return main_id


def _po_invoice_sync_one(con: sqlite3.Connection, po: dict, receipt_id: str, po_items: list[dict], created: bool,
                         before: dict[int, int] | None, by: str | None) -> None:
    preorder_id = po["id"]
    rec = dict(con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone())
    if before is None:
        before = _po_invoice_holdings(con, receipt_id)
    lines = {r["preorder_item_id"]: dict(r) for r in con.execute(
        "SELECT * FROM receipt_items WHERE receipt_id=? AND preorder_item_id IS NOT NULL", (receipt_id,)
    ).fetchall()}
    now = _now()
    added, changed = [], []
    for pi in po_items:
        ln = lines.pop(pi["id"], None)
        if ln is None:
            con.execute(
                """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id,
                       item_type, line_discount, added_at, added_by, added_during_work, preorder_item_id,
                       list_price, price_state, price_reason, price_requested_by, price_requested_at,
                       price_approved_by, price_approved_at, price_below_cost)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (receipt_id, pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"],
                 "jasa" if pi.get("source") == "jasa" else "barang", now, by,
                 0 if created or rec["work_status"] in ("menunggu", "belum") else 1, pi["id"],
                 *[pi.get(c) if c != "price_below_cost" else (pi.get(c) or 0) for c in _PRICE_COLS]),
            )
            added.append(pi["product_name"])
        elif (ln["product_name"], ln["quantity"], ln["unit_price"], ln["master_item_id"]) != \
                (pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"]) or (
                (ln["line_discount"] or 0) == 0 and [ln.get(c) for c in _PRICE_COLS] != [pi.get(c) for c in _PRICE_COLS]):
            if ln["returned_qty"]:
                raise ValueError(f"Barang {ln['product_name']} sudah pernah diretur, jadi tidak bisa diubah lagi di Sales Order.")
            con.execute(
                "UPDATE receipt_items SET product_name=?, quantity=?, unit_price=?, master_item_id=? WHERE id=?",
                (pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"], ln["id"]),
            )
            if not (ln["line_discount"] or 0):
                # The special price belongs to the Sales Order line; the invoice line mirrors it (a discount typed on
                # the nota itself keeps its own request).
                con.execute(f"UPDATE receipt_items SET {', '.join(c + '=?' for c in _PRICE_COLS)} WHERE id=?",
                            (*[pi.get(c) if c != "price_below_cost" else (pi.get(c) or 0) for c in _PRICE_COLS], ln["id"]))
            bits = []
            if ln["quantity"] != pi["quantity"]:
                bits.append(f"qty {ln['quantity']} -> {pi['quantity']}")
            if ln["unit_price"] != pi["unit_price"]:
                bits.append(f"harga {ln['unit_price']:,} -> {pi['unit_price']:,}".replace(",", "."))
            changed.append(f"{pi['product_name']}" + (f": {', '.join(bits)}" if bits else " diubah"))
    for ln in lines.values():
        if ln["returned_qty"]:
            raise ValueError(f"Barang {ln['product_name']} sudah pernah diretur, jadi tidak bisa dihapus dari Sales Order.")
        con.execute("DELETE FROM receipt_items WHERE id=?", (ln["id"],))
        changed.append(f"{ln['product_name']} dibatalkan")
    if (added or changed) and not created and _is_closed(rec):
        raise ValueError("Invoice Sales Order ini sudah lunas dan selesai (ditutup). Minta admin membukanya kembali "
                         "atau membuat Invoice Lanjutan untuk barang tambahan.")
    if po_items:
        con.execute(f"UPDATE preorder_items SET receipt_id=? WHERE id IN ({_placeholders(po_items)})",
                    [receipt_id, *[pi["id"] for pi in po_items]])

    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)).fetchall()]
    subtotal = sum(_line_total(i) for i in items)
    discount = min(rec["discount"] or 0, subtotal)
    total = subtotal - discount - (rec.get("return_total") or 0)
    status = _compute_receipt_status(total, rec["amount_paid"], None)
    if status == "done" and rec["status"] != "done" and _receipt_price_pending(con, receipt_id):
        status = "pending"  # K-22: not Lunas on a price nobody approved yet
    work = rec["work_status"]
    work_note = None
    if work == "menunggu" and po_items and all(pi["status"] in PO_ARRIVED_STATUSES for pi in po_items):
        work, work_note = "belum", "Status: Belum Dikerjakan — semua barang Sales Order sudah sampai"
    # The invoice's Pengerjaan follows the Sales Order lines it bills: every line Terpasang = Pengerjaan Selesai,
    # some Terpasang = Dalam Pengerjaan. (The other way round, Pengerjaan Selesai on the invoice already installs
    # the lines, see _install_so_lines.) A line taken back from Terpasang reopens a finished, not yet closed invoice.
    live = [pi for pi in po_items if pi["status"] != PO_LINE_CANCELLED]
    installed = [pi for pi in live if pi["status"] in PO_INSTALLED_STATUSES]
    if live and work != "draft" and status != "void" and getattr(_so_work_follow, "skip", None) != receipt_id:
        if len(installed) == len(live) and work != "selesai":
            work, work_note = "selesai", "Status: Pengerjaan Selesai — semua barang Sales Order sudah terpasang"
        elif installed and len(installed) < len(live) and work in ("menunggu", "belum"):
            work, work_note = "pengerjaan", "Status: Dalam Pengerjaan — sebagian barang Sales Order sudah terpasang"
        elif len(installed) < len(live) and work == "selesai" and rec["status"] != "done":
            work, work_note = "pengerjaan", "Status: Dalam Pengerjaan — ada barang Sales Order yang belum terpasang lagi"
    if work == "pengerjaan" and rec["work_status"] != "pengerjaan" and not rec.get("work_stage"):
        con.execute("UPDATE receipts SET work_stage='persiapan' WHERE id=?", (receipt_id,))
    con.execute("UPDATE receipts SET subtotal=?, discount=?, total=?, status=?, work_status=? WHERE id=?",
                (subtotal, discount, total, status, work, receipt_id))
    if work == "selesai" and rec["work_status"] != "selesai":
        _incentives().finish_assigned_items(con, receipt_id, by)
    elif rec["work_status"] == "selesai" and work != "selesai":
        _clear_handover(con, receipt_id, "pengerjaan dibuka kembali", by)
    tag = f" (Sales Order {po['po_number']})"
    if added and not created:
        _log_receipt_event(con, receipt_id, "item_added", "Item ditambahkan: " + ", ".join(added) + tag, by)
    if changed:
        _log_receipt_event(con, receipt_id, "item_changed", "Item diubah: " + "; ".join(changed) + tag, by)
    if work != rec["work_status"] and work_note:
        _log_receipt_event(con, receipt_id, "status", work_note, by)
    if total < rec["amount_paid"] and total != rec["total"]:
        _log_receipt_event(
            con, receipt_id, "overpaid",
            f"Total invoice turun menjadi Rp {total:,}; kelebihan bayar Rp {rec['amount_paid'] - total:,} perlu dikembalikan ke pelanggan.".replace(",", "."),
            by,
        )
    _apply_receipt_stock_delta(con, receipt_id, before, _po_invoice_holdings(con, receipt_id))
    _incentives().sync_receipt(con, receipt_id, by)


def po_invoice_payment(
    preorder_id: str, amount: int, method: str, note: str | None, by: str | None,
    transfer_sender: str | None = None, transfer_account: str | None = None, receipt_id: str | None = None,
    confirmed: bool | None = None,
) -> str:
    """'Tambah Pembayaran' from the PO page lands on the PO's invoice. With several invoices (K-19) the caller
    must say which. Returns the invoice id."""
    with _conn() as con:
        row = con.execute("SELECT receipt_id FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            raise ValueError("Preorder tidak ditemukan.")
        invoices = _po_invoices(con, preorder_id) if row["receipt_id"] else []
    if not row["receipt_id"]:
        raise ValueError("Sales Order ini belum punya invoice (masih draft).")
    if receipt_id:
        if receipt_id not in {i["id"] for i in invoices}:
            raise ValueError("Invoice itu bukan milik Sales Order ini.")
    elif len(invoices) > 1:
        raise ValueError("Sales Order ini punya beberapa invoice. Pilih invoice yang menerima pembayaran.")
    else:
        receipt_id = row["receipt_id"]
    add_receipt_payment(receipt_id, amount, method, note, transfer_sender, transfer_account, by, confirmed)
    return receipt_id


def po_invoices(con: sqlite3.Connection, receipt_ids: list[str]) -> dict[str, dict]:
    ids = [r for r in receipt_ids if r]
    if not ids:
        return {}
    out = {r["id"]: dict(r) for r in con.execute(
        f"SELECT id, receipt_no, total, amount_paid, status, work_status, preorder_id, "
        "(SELECT COALESCE(SUM(p.amount),0) FROM receipt_payments p WHERE p.receipt_id = receipts.id AND p.state='pending') AS pending_amount "
        f"FROM receipts WHERE id IN ({_placeholders(ids)})", ids
    ).fetchall()}
    # K-19: a Sales Order with Invoice Lanjutan shows the sum of its live invoices; the main invoice's number.
    pids = [v["preorder_id"] for v in out.values() if v.get("preorder_id")]
    if pids:
        by_po: dict[str, list[dict]] = {}
        for r in con.execute(
                f"SELECT * FROM receipts WHERE preorder_id IN ({_placeholders(pids)}) AND invoice_seq>1 AND status<>'void' ORDER BY invoice_seq",
                pids).fetchall():
            by_po.setdefault(r["preorder_id"], []).append(dict(r))
        for v in out.values():
            more = by_po.get(v.get("preorder_id"), [])
            v["invoice_count"] = 1 + (len(more) if v["status"] != "void" else 0)
            if not more or v["status"] == "void":
                continue
            live = [dict(v), *more]
            v["total"] = sum(i["total"] for i in live)
            v["amount_paid"] = sum(i["amount_paid"] for i in live)
            v["pending_amount"] = sum(_pending_sum(con, i["id"]) for i in live)
            v["status"] = "done" if all(i["status"] == "done" for i in live) else "pending"
            pending = [i for i in live if i["work_status"] != "selesai"]
            v["work_status"] = pending[0]["work_status"] if pending else "selesai"
    return out


# ---- K-19: Sales Order with several invoices (the special flow; the main flow stays 1 SO = 1 invoice) ----
# Three rules keep it from duplicating anything: a Sales Order line belongs to exactly one invoice
# (preorder_items.receipt_id); money only moves between invoices as a paired payment row (kind 'pindah')
# plus allocation-ledger rows, never by editing history; stock leaves once per line at Terpasang, and only
# lines that have not been installed can move, so a move never touches stock.

def _po_transfer_dp(con: sqlite3.Connection, po: dict, from_id: str, to_id: str, amount: int, by: str | None, why: str) -> None:
    """Moves `amount` of paid money from one invoice of the Sales Order to another, in the methods it was
    paid with. Totals and statuses are recomputed by the following _po_invoice_sync."""
    if amount <= 0:
        return
    src = con.execute("SELECT * FROM receipts WHERE id=?", (from_id,)).fetchone()
    dst = con.execute("SELECT * FROM receipts WHERE id=?", (to_id,)).fetchone()
    if amount > src["amount_paid"]:
        raise ValueError(f"DP yang dipindahkan ({_rp(amount)}) melebihi uang yang sudah dibayar di invoice asal ({_rp(src['amount_paid'])}).")
    chunks, left = [], amount
    for r in con.execute(
            """SELECT method, SUM(amount) AS net FROM receipt_payments WHERE receipt_id=? AND state='sah' GROUP BY method
               HAVING SUM(amount) > 0 ORDER BY MIN(id)""", (from_id,)).fetchall():
        take = min(r["net"], left)
        if take > 0:
            chunks.append((take, r["method"]))
            left -= take
        if left <= 0:
            break
    if left > 0:
        chunks.append((left, src["payment_method"] or "cash"))
    now = _now()
    for take, method in chunks:
        con.execute(
            """INSERT INTO receipt_payments (receipt_id, paid_at, amount, method, note, created_by, kind)
               VALUES (?, ?, ?, ?, ?, ?, 'pindah')""",
            (from_id, now, -take, method, f"Dipindahkan ke invoice {dst['receipt_no']} ({po['po_number']}). {why}", by))
        con.execute(
            """INSERT INTO receipt_payments (receipt_id, paid_at, amount, method, note, created_by, kind)
               VALUES (?, ?, ?, ?, ?, ?, 'pindah')""",
            (to_id, now, take, method, f"Dipindahkan dari invoice {src['receipt_no']} ({po['po_number']}). {why}", by))
    con.execute("UPDATE receipts SET amount_paid=amount_paid-? WHERE id=?", (amount, from_id))
    con.execute("UPDATE receipts SET amount_paid=amount_paid+? WHERE id=?", (amount, to_id))
    for rid, amt in ((from_id, -amount), (to_id, amount)):
        con.execute("INSERT INTO preorder_payment_allocations (preorder_id, receipt_id, amount, allocated_at, allocated_by) VALUES (?, ?, ?, ?, ?)",
                    (po["id"], rid, amt, now, by))
    _log_receipt_event(con, from_id, "payment", f"{_rp(amount)} dipindahkan ke invoice {dst['receipt_no']}. {why}", by)
    _log_receipt_event(con, to_id, "payment", f"{_rp(amount)} diterima dari invoice {src['receipt_no']}. {why}", by)


def _so_multi_load(con: sqlite3.Connection, preorder_id: str) -> dict:
    con.execute("BEGIN IMMEDIATE")
    po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None:
        raise ValueError("Sales Order tidak ditemukan.")
    po = dict(po)
    if po.get("cancelled_at"):
        raise ValueError("Sales Order ini sudah dibatalkan.")
    if po["is_draft"] or not po.get("receipt_id"):
        raise ValueError("Sales Order ini belum diterbitkan, jadi belum punya invoice.")
    return po


def _receipt_billed(con: sqlite3.Connection, receipt_id: str) -> int:
    rec = con.execute("SELECT discount, return_total FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    sub = sum(_line_total(dict(r)) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)))
    return sub - min(rec["discount"] or 0, sub) - (rec["return_total"] or 0)


def split_preorder_invoice(preorder_id: str, item_ids: list[int], reason: str, dp_move: int | None, by: str | None) -> dict:
    """Admin: moves whole Sales Order lines that are not installed yet from their invoice to a new Invoice
    Lanjutan. `dp_move` None = whatever the old invoice now has paid beyond its own total follows the lines."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan memisah invoice wajib diisi (minimal 5 huruf).")
    if not item_ids:
        raise ValueError("Pilih barang yang dipindahkan ke invoice lanjutan.")
    if dp_move is not None and dp_move < 0:
        raise ValueError("Jumlah DP yang dipindahkan tidak boleh negatif.")
    with _conn() as con:
        po = _so_multi_load(con, preorder_id)
        invoices = _po_invoices(con, preorder_id)
        by_id = {i["id"]: i for i in invoices}
        lines = {r["id"]: dict(r) for r in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? AND status<>?", (preorder_id, PO_LINE_CANCELLED))}
        picks = []
        for iid in dict.fromkeys(item_ids):
            ln = lines.get(iid)
            if ln is None:
                raise ValueError("Barang tidak ada di Sales Order ini atau sudah dibatalkan.")
            if ln["status"] in PO_INSTALLED_STATUSES:
                raise ValueError(f"“{ln['product_name']}” sudah terpasang dan stoknya sudah keluar; tidak bisa dipindah ke invoice lain. "
                                 "Pakai Retur Barang bila perlu.")
            if ln.get("receipt_id") not in by_id:
                raise ValueError(f"“{ln['product_name']}” belum punya invoice aktif.")
            picks.append(ln)
        owners = {ln["receipt_id"] for ln in picks}
        if len(owners) != 1:
            raise ValueError("Pilih barang dari satu invoice saja per pemisahan.")
        src_id = next(iter(owners))
        src = by_id[src_id]
        pick_ids = {ln["id"] for ln in picks}
        if not [ln for ln in lines.values() if ln.get("receipt_id") == src_id and ln["id"] not in pick_ids]:
            raise ValueError("Invoice asal harus tetap memegang minimal satu barang. Untuk memindahkan semuanya, batalkan atau void Sales Order biasa.")
        if con.execute(f"SELECT 1 FROM receipt_items WHERE receipt_id=? AND returned_qty>0 AND preorder_item_id IN ({_placeholders(picks)})",
                       [src_id, *pick_ids]).fetchone():
            raise ValueError("Ada barang yang sudah pernah diretur; tidak bisa dipindah ke invoice lain.")
        before = _po_invoice_snapshot(con, preorder_id)
        seq = 1 + max(r["invoice_seq"] for r in _po_invoices(con, preorder_id, include_void=True))
        work = "menunggu" if any(ln["status"] not in PO_ARRIVED_STATUSES for ln in picks) else "belum"
        new_id = _po_create_invoice(con, po, by, seq, work, reason)
        con.execute(f"UPDATE receipt_items SET receipt_id=? WHERE receipt_id=? AND preorder_item_id IN ({_placeholders(picks)})",
                    [new_id, src_id, *pick_ids])
        con.execute(f"UPDATE preorder_items SET receipt_id=? WHERE id IN ({_placeholders(picks)})", [new_id, *pick_ids])
        excess = max(src["amount_paid"] - _receipt_billed(con, src_id), 0)
        move = excess if dp_move is None else dp_move
        if move > src["amount_paid"]:
            raise ValueError(f"DP yang dipindahkan ({_rp(move)}) melebihi uang yang sudah dibayar di invoice asal ({_rp(src['amount_paid'])}).")
        _po_transfer_dp(con, po, src_id, new_id, move, by, f"Pisah invoice: {reason}")
        _po_invoice_sync(con, preorder_id, by, before)
        names = ", ".join(ln["product_name"] for ln in picks)
        new_no = con.execute("SELECT receipt_no FROM receipts WHERE id=?", (new_id,)).fetchone()["receipt_no"]
        _log_receipt_event(con, src_id, "split", f"Dipisah ke invoice lanjutan {new_no}: {names}. Alasan: {reason}", by)
        _log_receipt_event(con, new_id, "split", f"Dipisah dari invoice {src['receipt_no']}: {names}", by)
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (_now(), preorder_id))
    return get_preorder(preorder_id)


def open_followup_invoice(preorder_id: str, reason: str, by: str | None) -> dict:
    """Admin: an empty Invoice Lanjutan. New Sales Order lines go there while every older invoice is closed."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan membuat invoice lanjutan wajib diisi (minimal 5 huruf).")
    with _conn() as con:
        po = _so_multi_load(con, preorder_id)
        seq = 1 + max(r["invoice_seq"] for r in _po_invoices(con, preorder_id, include_void=True))
        new_id = _po_create_invoice(con, po, by, seq, "menunggu", reason)
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (_now(), preorder_id))
        _log_receipt_event(con, po["receipt_id"], "split", f"Invoice lanjutan dibuka: {reason}", by)
    return get_preorder(preorder_id)


def merge_followup_invoice(receipt_id: str, reason: str, by: str | None) -> dict:
    """Admin: folds an Invoice Lanjutan back into the main invoice (lines and paid money), then voids it."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan wajib diisi (minimal 5 huruf).")
    with _conn() as con:
        rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if rec is None or not rec["preorder_id"]:
            raise ValueError("Ini bukan invoice Sales Order.")
        preorder_id = rec["preorder_id"]
        po = _so_multi_load(con, preorder_id)
        if rec["status"] == "void":
            raise ValueError("Invoice ini sudah di-void.")
        if rec["id"] == po["receipt_id"]:
            raise ValueError("Invoice utama tidak bisa digabung ke invoice lain.")
        main = con.execute("SELECT * FROM receipts WHERE id=?", (po["receipt_id"],)).fetchone()
        if main["status"] == "void" or _is_closed(main):
            raise ValueError("Invoice utama sudah ditutup atau di-void. Buka kembali dulu (admin) sebelum menggabungkan.")
        if con.execute("""SELECT 1 FROM preorder_items WHERE preorder_id=? AND receipt_id=? AND status IN ('terpasang')""",
                       (preorder_id, receipt_id)).fetchone():
            raise ValueError("Invoice ini sudah punya barang terpasang (stok sudah keluar), jadi tidak bisa digabung kembali.")
        if con.execute("SELECT 1 FROM receipt_returns WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Invoice ini sudah ada retur barang, jadi tidak bisa digabung kembali.")
        before = _po_invoice_snapshot(con, preorder_id)
        con.execute("UPDATE receipt_items SET receipt_id=? WHERE receipt_id=?", (main["id"], receipt_id))
        con.execute("UPDATE preorder_items SET receipt_id=? WHERE preorder_id=? AND receipt_id=?", (main["id"], preorder_id, receipt_id))
        _po_transfer_dp(con, po, receipt_id, main["id"], max(rec["amount_paid"], 0), by, f"Gabung kembali: {reason}")
        con.execute("UPDATE receipts SET subtotal=0, discount=0, total=0, status='void', void_reason=?, voided_by=?, voided_at=? WHERE id=?",
                    (f"Digabung kembali ke invoice {main['receipt_no']}: {reason}", by, _now(), receipt_id))
        _po_invoice_sync(con, preorder_id, by, before)
        _log_receipt_event(con, receipt_id, "void", f"Digabung kembali ke invoice {main['receipt_no']}. Alasan: {reason}", by)
        _log_receipt_event(con, main["id"], "split", f"Invoice lanjutan {rec['receipt_no']} digabung kembali. Alasan: {reason}", by)
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (_now(), preorder_id))
    return get_preorder(preorder_id)


def _migrate_k01_install_point(con: sqlite3.Connection) -> None:
    """One-time switch to K-01 (Sales Order goods leave stock when installed, not when they arrive).
    Invoices whose work is already finished had their goods installed: those lines are recorded as
    Terpasang and keep their stock movement. On every other invoice, arrived-but-not-installed goods
    are put back In Hand (a ledger IN per item) — they are physically still on the shelf — and from
    then on are reserved for that customer, so Available does not change."""
    rids = [r["receipt_id"] for r in con.execute(
        """SELECT DISTINCT ri.receipt_id FROM receipt_items ri JOIN receipts r ON r.id = ri.receipt_id
           WHERE ri.preorder_item_id IS NOT NULL AND r.status <> 'void' AND r.work_status <> 'draft'""").fetchall()]
    now = _now()
    for rid in rids:
        rec = con.execute("SELECT work_status FROM receipts WHERE id=?", (rid,)).fetchone()
        items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (rid,)).fetchall()]
        if rec["work_status"] == "selesai":
            for it in items:
                if not it.get("preorder_item_id"):
                    continue
                line = con.execute("SELECT * FROM preorder_items WHERE id=?", (it["preorder_item_id"],)).fetchone()
                if line is not None and line["status"] in ("sampai_di_toko", "dititipkan"):
                    con.execute("UPDATE preorder_items SET status='terpasang' WHERE id=?", (line["id"],))
                    _log_preorder_status(con, line["preorder_id"], line["id"], line["product_name"], "terpasang",
                                         now, "sistem", line["status"])
            continue
        old = _receipt_item_holdings(items, con, PO_ARRIVED_STATUSES)
        new = _receipt_item_holdings(items, con)
        if old != new:
            _apply_receipt_stock_delta(con, rid, old, new)
            _log_receipt_event(con, rid, "status",
                               "Aturan stok baru: barang Sales Order yang sudah di toko tetapi belum terpasang "
                               "dikembalikan ke stok dan dicadangkan untuk pelanggan ini.", "sistem")


def _backfill_po_invoices(con: sqlite3.Connection) -> None:
    """Startup: a published PO without an invoice gets one, and what was paid on the PO moves onto it.
    A PO whose lines were already turned into a nota the old way, or whose plate the cashier would not
    accept, is left as it was (it keeps working through the old flow)."""
    for po in con.execute("SELECT * FROM preorders WHERE is_draft=0 AND receipt_id IS NULL").fetchall():
        if con.execute("SELECT 1 FROM preorder_items WHERE preorder_id=? AND receipt_id IS NOT NULL", (po["id"],)).fetchone():
            continue
        if split_plate(po["plate_full"]) is None:
            continue
        if not con.execute("SELECT 1 FROM preorder_items WHERE preorder_id=?", (po["id"],)).fetchone():
            continue
        con.execute("SAVEPOINT po_backfill")
        try:
            rid = _po_invoice_sync(con, po["id"], "sistem")
            _po_move_payments_to_invoice(con, po["id"], rid, "sistem")
            con.execute("RELEASE po_backfill")
        except ValueError:
            con.execute("ROLLBACK TO po_backfill")
            con.execute("RELEASE po_backfill")


def get_all_preorders(query: str | None = None, status: str | None = None, draft: bool | None = False) -> list[dict]:
    """`status` filters on the computed order-level stage, so it is applied in
    Python rather than SQL — the value does not exist as a column. `draft` is a
    real column, so it's applied in SQL: False (the default) is the active
    list, True is the parked/archived one, None is both (the list's "Semua")."""
    where_clauses = [] if draft is None else ["is_draft=?"]
    params: list = [] if draft is None else [1 if draft else 0]
    if query:
        where_clauses.append(
            "(customer_name LIKE ? OR customer_phone LIKE ? OR po_number LIKE ? OR plate_full LIKE ? OR id IN "
            "(SELECT preorder_id FROM preorder_items WHERE product_name LIKE ?))"
        )
        like = f"%{query}%"
        params.extend([like, like, like, like, like])
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT *, (SELECT name FROM suppliers WHERE id=preorders.supplier_id) AS supplier_name FROM preorders {where_sql} ORDER BY created_at DESC", params
        ).fetchall()]
        items_by_order: dict[str, list[dict]] = {}
        for item in con.execute(
            f"{_PREORDER_ITEMS_SQL} ORDER BY pi.position ASC, pi.id ASC"
        ).fetchall():
            items_by_order.setdefault(item["preorder_id"], []).append(dict(item))
        paid_by_order = _preorder_paid_by_order(con)
        invoices = po_invoices(con, [r["receipt_id"] for r in rows])
        draft_pend = _preorder_pending_by_order(con)
        rows = [{**r, "draft_pending": draft_pend.get(r["id"], 0)} for r in rows]

    result = [_preorder_row(row, items_by_order.get(row["id"], []), paid_by_order.get(row["id"], 0),
                            invoices.get(row["receipt_id"])) for row in rows]
    if status:
        result = [r for r in result if r["status"] == status]
    return result


def delete_preorder_draft(preorder_id: str) -> None:
    """Owner 25-10-2026: a draft Sales Order that never took money can be deleted outright. One that
    received any payment must be cancelled instead, so the refund stays on the cash books."""
    with _conn() as con:
        po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if not po:
            raise ValueError("Sales Order tidak ditemukan.")
        if not po["is_draft"]:
            raise ValueError("Hanya Sales Order berstatus Draft yang bisa dihapus.")
        if con.execute("SELECT 1 FROM preorder_payments WHERE preorder_id=? LIMIT 1", (preorder_id,)).fetchone():
            raise ValueError("Draft ini sudah menerima pembayaran. Batalkan Sales Order-nya agar refund tetap tercatat.")
        if po["receipt_id"] or con.execute("SELECT 1 FROM receipts WHERE preorder_id=? LIMIT 1", (preorder_id,)).fetchone():
            raise ValueError("Draft ini sudah punya invoice, jadi tidak bisa dihapus.")
        lines = [dict(r) for r in con.execute("SELECT * FROM preorder_items WHERE preorder_id=?", (preorder_id,))]
        for ln in lines:
            if con.execute(
                """SELECT 1 FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                   WHERE spi.preorder_item_id=? AND sp.status <> 'draft' LIMIT 1""", (ln["id"],)
            ).fetchone():
                raise ValueError(f"Barang “{ln['product_name']}” sudah tercatat di Purchase Order. Batalkan Sales Order-nya saja.")
        for ln in lines:
            _release_preorder_line_from_pos(con, ln, None)
        con.execute("DELETE FROM preorder_status_history WHERE preorder_id=?", (preorder_id,))
        con.execute("DELETE FROM preorder_items WHERE preorder_id=?", (preorder_id,))
        con.execute("DELETE FROM preorders WHERE id=?", (preorder_id,))


def _release_preorder_line_from_pos(con: sqlite3.Connection, line: dict, by: str | None, changing: bool = False) -> None:
    """A Sales Order line is being removed. Draft Purchase Orders are only plans, so the line is taken
    off them (an emptied draft is cancelled). Once a PO has gone to the supplier the goods are really
    coming, so the removal is refused until that PO item is cancelled the proper way."""
    refs = con.execute(
        """SELECT spi.id, spi.supplier_po_id, spi.quantity, COALESCE(spi.qty_cancelled, 0) AS qty_cancelled,
                  sp.status, sp.po_number
           FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
           WHERE spi.preorder_item_id=?""",
        (line["id"],),
    ).fetchall()
    for r in refs:
        if r["status"] not in ("draft", "batal") and r["quantity"] - r["qty_cancelled"] > 0:
            raise ValueError(
                f"Barang “{line['product_name']}” sudah dipesan di Purchase Order {r['po_number']}. "
                + ("Sumber/supplier-nya tidak bisa diganti lagi; batalkan item itu di Purchase Order dulu."
                   if changing else "Batalkan item itu di Purchase Order dulu, baru hapus dari Sales Order.")
            )
    for r in refs:
        if r["status"] != "draft":
            continue
        con.execute("DELETE FROM supplier_po_items WHERE id=?", (r["id"],))
        left = con.execute("SELECT COUNT(*) AS n FROM supplier_po_items WHERE supplier_po_id=?",
                           (r["supplier_po_id"],)).fetchone()["n"]
        if left == 0:
            con.execute("UPDATE supplier_pos SET status='batal', updated_at=? WHERE id=?", (_now(), r["supplier_po_id"]))
        _spo_event(con, r["supplier_po_id"], "cancel" if left == 0 else "edit",
                   f"Baris “{line['product_name']}” " + ("sumber/supplier-nya diganti di Sales Order" if changing else "dihapus dari Sales Order")
                   + (" — draft kosong, dibatalkan" if left == 0 else ""), by)


# Owner 25-10-2026: no minimum DP any more (was 30%, K-03); any DP, or none, may publish a Sales Order.
SO_MIN_DP_PERCENT = 0


def _require_publish_dp(con: sqlite3.Connection, preorder_id: str) -> None:
    """K-03: a Sales Order is published only once the customer has paid at least SO_MIN_DP_PERCENT
    of its total as down payment. No exceptions; until then it stays a draft. Checked at the moment of
    publishing only, so later edits to a published order are not blocked by it."""
    if _preorder_price_pending(con, preorder_id):
        raise ValueError(_PRICE_PENDING_MSG)
    total = con.execute(
        "SELECT COALESCE(SUM(quantity*unit_price),0) AS t FROM preorder_items WHERE preorder_id=?", (preorder_id,)
    ).fetchone()["t"]
    need = -(-total * SO_MIN_DP_PERCENT // 100)
    paid = con.execute(
        "SELECT COALESCE(SUM(amount),0) AS n FROM preorder_payments WHERE preorder_id=? AND state='sah'", (preorder_id,)
    ).fetchone()["n"]
    inv = con.execute(
        "SELECT r.amount_paid FROM preorders p JOIN receipts r ON r.id=p.receipt_id WHERE p.id=? AND r.status<>'void'",
        (preorder_id,),
    ).fetchone()
    if inv is not None:
        paid = max(paid, inv["amount_paid"] or 0)
    if paid < need:
        rp = lambda n: f"{n:,}".replace(",", ".")
        raise ValueError(
            f"Uang muka (DP) minimal {SO_MIN_DP_PERCENT}% dari total sebelum Sales Order diterbitkan: "
            f"minimal Rp {rp(need)}, tercatat Rp {rp(paid)}. Simpan sebagai draft dulu, atau tambah DP."
        )


def update_preorder(
    preorder_id: str,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    update_notes: bool = False,
    changed_by: str | None = None,
    is_draft: bool | None = None,
    plate_full: str | None = None,
    po_date: str | None = None,
    set_supplier: bool = False,
    supplier_id: int | None = None,
    price_actor: dict | None = None,
    order_source: str | None = None,
) -> dict:
    """Items are matched by id rather than deleted and re-inserted (the pattern
    receipts use), because a line item owns its stage and its status history —
    re-creating the row on every edit would silently reset both. Payments are
    never touched here; they only grow through add_preorder_payment."""
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            raise ValueError(f"Preorder {preorder_id} tidak ditemukan.")
        existing = dict(row)
        if existing.get("cancelled_at"):
            raise ValueError("Sales Order ini sudah dibatalkan dan tidak bisa diubah lagi.")
        before = _po_invoice_snapshot(con, preorder_id)
        if is_draft and not existing["is_draft"] and existing.get("receipt_id"):
            inv = con.execute("SELECT status, work_status FROM receipts WHERE id=?", (existing["receipt_id"],)).fetchone()
            if inv is not None and inv["status"] != "void" and not _is_closed(inv):
                raise ValueError("Sales Order ini punya invoice yang masih aktif. Selesaikan atau void invoicenya dulu sebelum diarsipkan.")

        con.execute(
            """UPDATE preorders SET customer_name=?, customer_phone=?, plate_full=?, po_date=?, notes=?,
               updated_at=?, is_draft=?, supplier_id=?, order_source=? WHERE id=?""",
            (
                customer_name if customer_name is not None else existing["customer_name"],
                customer_phone if customer_phone is not None else existing["customer_phone"],
                plate_full if plate_full is not None else existing["plate_full"],
                po_date if po_date is not None else existing["po_date"],
                notes if update_notes else existing["notes"],
                now,
                int(is_draft) if is_draft is not None else existing["is_draft"],
                supplier_id if set_supplier else existing.get("supplier_id"),
                _preorder_source(order_source) if order_source is not None else (existing.get("order_source") or "walk_in"),
                preorder_id,
            ),
        )
        new_plate = split_plate(plate_full) if plate_full else None
        if new_plate and plate_full != existing["plate_full"]:
            # A plate typed later on the Sales Order reaches its open invoices that still have none.
            for inv in con.execute("SELECT id FROM receipts WHERE preorder_id=? AND status<>'void' AND TRIM(plate_full)=''",
                                   (preorder_id,)).fetchall():
                con.execute("UPDATE receipts SET plate_region=?, plate_number=?, plate_suffix=?, plate_full=? WHERE id=?",
                            (*new_plate, " ".join(new_plate), inv["id"]))
                _link_receipt_vehicle(con, inv["id"], changed_by)

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
                    if prev["status"] == PO_LINE_CANCELLED:
                        continue  # a cancelled line is kept exactly as it was
                    new_status = item.get("status") or prev["status"]
                    if new_status == PO_LINE_CANCELLED:
                        raise ValueError("Barang hanya bisa dibatalkan lewat tombol Batalkan Sales Order / Pembatalan Khusus.")
                    source, supplier_id = _preorder_item_source(con, item)
                    if source == "stok" and (prev.get("source") != "stok" or item["quantity"] > prev["quantity"]
                                             or item.get("master_item_id") != prev.get("master_item_id")):
                        _require_stock_free(con, {**item, "source": source}, item_id)
                    if source == "supplier" and prev.get("source") != "supplier":
                        _forbid_supplier_when_stocked(con, {**item, "source": source})
                    if (prev.get("source"), prev.get("supplier_id")) != (source, supplier_id) and prev["status"] not in PO_CLOSED_LINE_STATUSES:
                        # Draft Purchase Orders made for the old choice are dropped (an issued one refuses).
                        _release_preorder_line_from_pos(con, prev, changed_by, changing=True)
                    price = _price_review(
                        con, scope="preorder", doc_id=preorder_id, master_item_id=item.get("master_item_id"),
                        product_name=item["product_name"], qty=item["quantity"], unit_price=item["unit_price"], line_discount=0,
                        prev=prev, reason=item.get("price_reason"), allow_below_cost=bool(item.get("allow_below_cost")), actor=price_actor)
                    con.execute(
                        """UPDATE preorder_items SET master_item_id=?, product_name=?, unit=?,
                           quantity=?, unit_price=?, status=?, position=?, ship_route=?, priority=?, eta_date=?,
                           source=?, supplier_id=?, list_price=?, price_state=?, price_reason=?, price_requested_by=?,
                           price_requested_at=?, price_approved_by=?, price_approved_at=?, price_below_cost=?, note=? WHERE id=?""",
                        (
                            item.get("master_item_id"), item["product_name"], item.get("unit"),
                            item["quantity"], item["unit_price"], new_status, position,
                            *_preorder_item_logistics(item), source, supplier_id, *[price[c] for c in _PRICE_COLS],
                            (item.get("note") or "").strip() or None, item_id,
                        ),
                    )
                    if new_status != prev["status"]:
                        _log_preorder_status(con, preorder_id, item_id, item["product_name"],
                                             new_status, now, changed_by, prev["status"])
                else:
                    kept.add(_insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by, "_price_actor": price_actor}, position, now))

            for stale_id in set(current) - kept:
                if current[stale_id]["status"] == PO_LINE_CANCELLED:
                    raise ValueError(f"Barang “{current[stale_id]['product_name']}” sudah dibatalkan dan tetap tercatat; tidak bisa dihapus.")
                _release_preorder_line_from_pos(con, current[stale_id], changed_by)
                con.execute("DELETE FROM preorder_status_history WHERE preorder_item_id=?", (stale_id,))
                con.execute("DELETE FROM preorder_items WHERE id=?", (stale_id,))

        if existing["is_draft"] and is_draft is False:
            _require_publish_dp(con, preorder_id)
            con.execute("UPDATE preorders SET published_at=COALESCE(published_at, ?) WHERE id=?", (now, preorder_id))
        # A published PO's invoice follows (and a draft that is published now gets its invoice).
        receipt_id = _po_invoice_sync(con, preorder_id, changed_by, before)
        if receipt_id:
            _po_move_payments_to_invoice(con, preorder_id, receipt_id, changed_by)
        _auto_supplier_pos_for_preorder(con, preorder_id, changed_by)
        _link_preorder_vehicle(con, preorder_id)
    return get_preorder(preorder_id)


def set_preorder_item_status(
    preorder_id: str, item_id: int, status: str, changed_by: str | None = None
) -> dict:
    now = _now()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM preorder_items WHERE id=? AND preorder_id=?", (item_id, preorder_id)
        ).fetchone()
        if row is None:
            raise ValueError(f"Item Sales Order {item_id} tidak ditemukan.")
        item = dict(row)
        if item["status"] == PO_LINE_CANCELLED:
            raise ValueError(f"Barang “{item['product_name']}” sudah dibatalkan; statusnya tidak bisa diubah.")
        if item["status"] != status:
            before = _po_invoice_snapshot(con, preorder_id)
            con.execute("UPDATE preorder_items SET status=? WHERE id=?", (status, item_id))
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
            _log_preorder_status(con, preorder_id, item_id, item["product_name"], status, now,
                                 changed_by, item["status"])
            _po_invoice_sync(con, preorder_id, changed_by, before)
        updated = dict(con.execute("SELECT * FROM preorder_items WHERE id=?", (item_id,)).fetchone())
    return updated


def handover_preorder_item_partial(preorder_id: str, item_id: int, qty: int, changed_by: str | None = None) -> dict:
    """UAT-010: hands over part of one Sales Order line (2 of 4 units now, the rest later). The line is split: a new
    line with `qty` units goes straight to Terpasang (its stock leaves through the invoice), the original keeps the
    rest and its Purchase Order link, so goods still coming arrive on it as before. Only units already held for the
    line at the shop can be handed over. Technicians, sales and work state are copied to the new invoice line."""
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM preorder_items WHERE id=? AND preorder_id=?", (item_id, preorder_id)).fetchone()
        if row is None:
            raise ValueError(f"Item Sales Order {item_id} tidak ditemukan.")
        item = dict(row)
        if item["status"] in PO_CLOSED_LINE_STATUSES:
            raise ValueError(f"“{item['product_name']}” sudah terpasang atau dibatalkan.")
        if item["master_item_id"] is None or item.get("source") == "jasa":
            raise ValueError("Serah terima sebagian hanya untuk barang dari Master Barang.")
        if not 1 <= qty < item["quantity"]:
            raise ValueError(f"Jumlah serah terima harus 1 sampai {item['quantity'] - 1}; untuk semua unit, ubah status baris ke Terpasang.")
        held = next((ln["reserved"] for ln in _reservation_lines(con, [item["master_item_id"]]) if ln["id"] == item_id), 0)
        if qty > held:
            raise ValueError(f"Baru {held} unit “{item['product_name']}” yang ada di toko untuk baris ini.")
        old_ri = con.execute("SELECT * FROM receipt_items WHERE preorder_item_id=?", (item_id,)).fetchone()
        if old_ri is not None and (old_ri["returned_qty"] or old_ri["line_discount"]):
            raise ValueError(f"“{item['product_name']}” punya retur atau diskon baris di invoice, jadi tidak bisa dipecah.")
        before = _po_invoice_snapshot(con, preorder_id)
        cols = [c for c in item if c not in ("id", "quantity", "status")]
        cur = con.execute(
            f"INSERT INTO preorder_items ({', '.join(cols)}, quantity, status) VALUES ({_placeholders(cols)}, ?, 'terpasang')",
            [item[c] for c in cols] + [qty])
        new_id = cur.lastrowid
        con.execute("UPDATE preorder_items SET quantity=? WHERE id=?", (item["quantity"] - qty, item_id))
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        _log_preorder_status(con, preorder_id, new_id, item["product_name"], "terpasang", now, changed_by, item["status"])
        _po_invoice_sync(con, preorder_id, changed_by, before)
        new_ri = con.execute("SELECT * FROM receipt_items WHERE preorder_item_id=?", (new_id,)).fetchone()
        if old_ri is not None and new_ri is not None:
            # Same line of work: the split part keeps who installed / sold it and when it was added.
            con.execute("UPDATE receipt_items SET added_at=?, added_by=?, added_during_work=? WHERE id=?",
                        (old_ri["added_at"], old_ri["added_by"], old_ri["added_during_work"], new_ri["id"]))
            for table in ("receipt_item_work", "receipt_item_technicians"):
                tcols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})") if r["name"] not in ("id", "receipt_item_id")]
                con.execute(f"INSERT OR IGNORE INTO {table} (receipt_item_id, {', '.join(tcols)}) "
                            f"SELECT ?, {', '.join(tcols)} FROM {table} WHERE receipt_item_id=?", (new_ri["id"], old_ri["id"]))
            _log_receipt_event(con, new_ri["receipt_id"], "item_changed",
                               f"Serah terima sebagian: {qty} dari {item['quantity']} {item.get('unit') or 'unit'} {item['product_name']} "
                               f"terpasang, sisa {item['quantity'] - qty} menunggu", changed_by)
            _incentives().sync_receipt(con, new_ri["receipt_id"], changed_by)
        return dict(con.execute("SELECT * FROM preorder_items WHERE id=?", (new_id,)).fetchone())


def set_preorder_status(preorder_id: str, status: str, changed_by: str | None = None) -> int:
    """Moves every line that is not already at `status` there ("Ubah Status PO"),
    logging each one. Returns how many lines changed."""
    now = _now()
    changed = 0
    with _conn() as con:
        if con.execute("SELECT 1 FROM preorders WHERE id=?", (preorder_id,)).fetchone() is None:
            raise ValueError(f"Preorder {preorder_id} tidak ditemukan.")
        before = _po_invoice_snapshot(con, preorder_id)
        for item in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? AND status<>? AND status<>?",
            (preorder_id, status, PO_LINE_CANCELLED),
        ).fetchall():
            con.execute("UPDATE preorder_items SET status=? WHERE id=?", (status, item["id"]))
            _log_preorder_status(con, preorder_id, item["id"], item["product_name"], status, now,
                                 changed_by, item["status"])
            changed += 1
        if changed:
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
            _po_invoice_sync(con, preorder_id, changed_by, before)
    return changed


# --- K-13: cancelling a Sales Order (official policy, decided 2026-10-18) -----------------------
# Rules are per line. An INDENT line (ordered from a supplier: free text, on any Purchase Order, or
# still needing goods the shelf can't cover) may be cancelled at the customer's request only within
# 24 hours of the order being published AND while no Purchase Order carrying it went to the
# supplier, whichever ends first; past that it is blocked: nothing is cancelled, refunded or
# released. A SHOP-STOCK line (served entirely from the shelf) may be cancelled by an admin at any
# time until it is installed / handed over. When the shop or the supplier
# fails the customer (cannot supply, wrong goods, broken delivery terms) an admin may still cancel
# the affected lines (Pembatalan Khusus): the customer gets back in full what they paid beyond the
# lines they keep, and issued Purchase Orders and stock are left for the admin to settle by hand.
# Installed (Terpasang) lines are never cancelled; they stay billed. Admin only, reason mandatory,
# every step logged on the Sales Order, its invoice and the Purchase Orders involved.

SO_CANCEL_WINDOW_HOURS = 24
SO_FAULT_CATEGORIES = {
    "gagal_memenuhi": "Toko/supplier gagal memenuhi pesanan",
    "barang_salah": "Barang yang dikirim salah / tidak sesuai",
    "pelanggaran_pengiriman": "Melanggar ketentuan pengiriman yang disepakati",
}
SO_REFUND_METHODS = ("cash", "transfer", "qris", "debit")


def _rp(n: int) -> str:
    return f"Rp {n:,}".replace(",", ".")


def _so_issued_po_refs(con: sqlite3.Connection, line_ids: list[int]) -> list[dict]:
    """Purchase Orders already sent to the supplier that still carry some of these lines."""
    if not line_ids:
        return []
    return [dict(r) for r in con.execute(
        f"""SELECT spi.preorder_item_id, spi.product_name, sp.id AS supplier_po_id, sp.po_number, sp.status
            FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
            WHERE spi.preorder_item_id IN ({_placeholders(line_ids)}) AND sp.status NOT IN ('draft','batal')
              AND spi.quantity - COALESCE(spi.qty_cancelled, 0) > 0
            ORDER BY sp.po_number""", line_ids).fetchall()]


def _so_line_kinds(con: sqlite3.Connection, lines: list[dict]) -> dict[int, str]:
    """'inden' for a line that comes (or came) from a supplier, 'stok' for one the shelf covers alone."""
    held = {ln["id"]: ln for ln in _reservation_lines(con, [ln["master_item_id"] for ln in lines])}
    ids = [ln["id"] for ln in lines]
    on_po = {r["preorder_item_id"] for r in con.execute(
        f"""SELECT DISTINCT spi.preorder_item_id FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
            WHERE spi.preorder_item_id IN ({_placeholders(ids)}) AND sp.status <> 'batal'""", ids)} if ids else set()
    out = {}
    for ln in lines:
        h = held.get(ln["id"])
        if ln.get("source") in ("stok", "jasa"):
            supplier = ln["id"] in on_po  # chosen from stock / a service; only a PO makes it indent
        elif ln.get("source") == "supplier":
            supplier = True
        else:
            supplier = ln["master_item_id"] is None or ln["id"] in on_po or (h is not None and (h["coming"] or h["shortfall"]))
        out[ln["id"]] = "inden" if supplier else "stok"
    return out


def _so_cancel_check(con: sqlite3.Connection, po: dict, lines: list[dict]) -> dict:
    """Which lines 'Batalkan' (customer request) may cancel right now, and why the others can't.
    `lines` in the result: one entry per open line {id, product_name, type, allowed, reason}."""
    out = {"allowed": False, "reasons": [], "deadline": None, "special_allowed": False, "lines": []}
    if po.get("cancelled_at"):
        out["reasons"].append("Sales Order ini sudah dibatalkan.")
        return out
    if po["is_draft"]:
        out["reasons"].append("Sales Order masih draft; cukup diarsipkan.")
        return out
    if not po.get("receipt_id") or not _po_invoices(con, po["id"]):
        out["reasons"].append("Invoice Sales Order ini sudah di-void.")
        return out
    open_lines = [ln for ln in lines if ln["status"] not in PO_CLOSED_LINE_STATUSES]
    if not open_lines:
        out["reasons"].append("Tidak ada barang yang bisa dibatalkan: semua sudah terpasang atau dibatalkan.")
        return out
    out["special_allowed"] = True
    kinds = _so_line_kinds(con, open_lines)
    published = po.get("published_at") or po["created_at"]
    deadline = datetime.fromisoformat(published) + timedelta(hours=SO_CANCEL_WINDOW_HOURS)
    expired = datetime.now(timezone.utc) > deadline
    local = deadline + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)
    issued: dict[int, list[str]] = {}
    for r in _so_issued_po_refs(con, [ln["id"] for ln in open_lines]):
        issued.setdefault(r["preorder_item_id"], []).append(r["po_number"])
    if any(k == "inden" for k in kinds.values()):
        out["deadline"] = deadline.isoformat()
    for ln in open_lines:
        why = []
        if kinds[ln["id"]] == "inden":
            if expired:
                why.append(f"barang inden, sudah lewat {SO_CANCEL_WINDOW_HOURS} jam sejak dikonfirmasi (batas {local:%d/%m/%Y %H:%M})")
            if ln["id"] in issued:
                why.append(f"Purchase Order {', '.join(sorted(set(issued[ln['id']])))} sudah diterbitkan ke supplier")
        reason = "; ".join(why) or None
        out["lines"].append({"id": ln["id"], "product_name": ln["product_name"], "type": kinds[ln["id"]],
                             "allowed": not why, "reason": reason})
        if why:
            out["reasons"].append(f"{ln['product_name']}: {reason}")
    out["allowed"] = any(x["allowed"] for x in out["lines"])
    return out


def get_preorder_cancel_check(preorder_id: str) -> dict | None:
    with _conn() as con:
        po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if po is None:
            return None
        lines = [dict(r) for r in con.execute("SELECT * FROM preorder_items WHERE preorder_id=?", (preorder_id,))]
        return _so_cancel_check(con, dict(po), lines)


def _so_mark_lines_cancelled(con: sqlite3.Connection, po: dict, lines: list[dict], kind: str, reason: str,
                             by: str | None, now: str) -> None:
    for ln in lines:
        con.execute(
            """UPDATE preorder_items SET status=?, cancelled_at=?, cancelled_by=?, cancel_kind=?, cancel_reason=?
               WHERE id=? AND status=?""",
            (PO_LINE_CANCELLED, now, by, kind, reason, ln["id"], ln["status"]),
        )
        _log_preorder_status(con, po["id"], ln["id"], ln["product_name"], PO_LINE_CANCELLED, now, by, ln["status"])


def _so_drop_from_draft_pos(con: sqlite3.Connection, po: dict, line: dict, by: str | None) -> None:
    """Draft Purchase Orders are only plans: the cancelled line is taken off them."""
    for r in con.execute(
        """SELECT spi.id, spi.supplier_po_id FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
           WHERE spi.preorder_item_id=? AND sp.status='draft'""", (line["id"],)).fetchall():
        con.execute("DELETE FROM supplier_po_items WHERE id=?", (r["id"],))
        left = con.execute("SELECT COUNT(*) AS n FROM supplier_po_items WHERE supplier_po_id=?",
                           (r["supplier_po_id"],)).fetchone()["n"]
        if left == 0:
            con.execute("UPDATE supplier_pos SET status='batal', updated_at=? WHERE id=?", (_now(), r["supplier_po_id"]))
        _spo_event(con, r["supplier_po_id"], "cancel" if left == 0 else "edit",
                   f"Sales Order {po['po_number']} dibatalkan: “{line['product_name']}” dihapus dari draft"
                   + (" — draft kosong, dibatalkan" if left == 0 else ""), by)


def _so_cancel_invoice(con: sqlite3.Connection, po: dict, drop: list[dict]) -> str:
    """K-19: the invoice a cancellation settles on. A Sales Order with one invoice: that one. With several, the
    cancelled lines must all sit on the same invoice, because the refund comes out of that invoice's money."""
    invoices = _po_invoices(con, po["id"])
    if len(invoices) <= 1:
        return po["receipt_id"]
    owners = {ln.get("receipt_id") for ln in drop}
    if len(owners) != 1 or next(iter(owners)) not in {i["id"] for i in invoices}:
        raise ValueError("Sales Order ini punya beberapa invoice. Pilih barang dari satu invoice saja per pembatalan "
                         "(uang refund diambil dari invoice itu).")
    return next(iter(owners))


def _so_settle_invoice(con: sqlite3.Connection, po: dict, before: dict | None, refund: int, method: str,
                       fee: int, note: str, by: str | None, receipt_id: str | None = None, proof: str | None = None) -> dict:
    """After lines are cancelled: the invoice drops them, keeps a cancellation fee if any, pays back
    `refund`, and closes when nothing is left to do. `receipt_id` is the invoice the money belongs to."""
    main_id = _po_invoice_sync(con, po["id"], by, before)
    receipt_id = receipt_id or main_id
    if fee > 0:
        con.execute(
            """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id,
                   item_type, line_discount, added_at, added_by, added_during_work, preorder_item_id)
               VALUES (?, ?, 1, ?, NULL, NULL, 'jasa', 0, ?, ?, 1, NULL)""",
            (receipt_id, f"BIAYA PEMBATALAN SALES ORDER {po['po_number']}", fee, _now(), by),
        )
        _po_invoice_sync(con, po["id"], by)
    rec = dict(con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone())
    if refund > 0:
        cur = con.execute("UPDATE receipts SET amount_paid=? WHERE id=? AND amount_paid=?",
                          (rec["amount_paid"] - refund, receipt_id, rec["amount_paid"]))
        if cur.rowcount == 0:
            raise ValueError("Invoice ini baru saja berubah. Muat ulang lalu coba lagi.")
        _insert_receipt_payment(con, receipt_id, -refund, method, f"Refund pembatalan Sales Order {po['po_number']}", by,
                                kind="refund", proof_ref=_refund_proof(proof))
        _log_receipt_event(con, receipt_id, "refund", f"{_rp(refund)} ({method}) dikembalikan ke pelanggan. {note}", by)
        rec["amount_paid"] -= refund
    status = _compute_receipt_status(rec["total"], rec["amount_paid"], None)
    con.execute("UPDATE receipts SET status=? WHERE id=?", (status, receipt_id))
    left = con.execute("SELECT status FROM preorder_items WHERE preorder_id=? AND receipt_id=? AND status<>?",
                       (po["id"], receipt_id, PO_LINE_CANCELLED)).fetchall()
    if all(r["status"] in PO_INSTALLED_STATUSES for r in left):
        if rec["total"] == 0 and rec["amount_paid"] == 0:
            con.execute("UPDATE receipts SET status='void', void_reason=?, voided_by=?, voided_at=? WHERE id=?",
                        (note, by, _now(), receipt_id))
            _log_receipt_event(con, receipt_id, "void", f"Invoice ditutup (void): {note}", by)
        elif rec["work_status"] != "selesai":
            con.execute("UPDATE receipts SET work_status='selesai' WHERE id=?", (receipt_id,))
            _log_receipt_event(con, receipt_id, "status", f"Status: Pengerjaan Selesai — {note}", by)
    _log_receipt_event(con, receipt_id, "status", note, by)
    return dict(con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone())


def _so_load_for_cancel(con: sqlite3.Connection, preorder_id: str) -> tuple[dict, list[dict]]:
    con.execute("BEGIN IMMEDIATE")
    po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None:
        raise ValueError("Sales Order tidak ditemukan.")
    lines = [dict(r) for r in con.execute(
        "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,))]
    return dict(po), lines


def _so_amounts(con: sqlite3.Connection, po: dict, keep_lines: list[dict], receipt_id: str | None = None) -> tuple[int, int]:
    """(paid on the invoice, what the invoice will still bill once only `keep_lines` remain)."""
    receipt_id = receipt_id or po["receipt_id"]
    rec = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    keep_ids = {ln["id"] for ln in keep_lines}
    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,))]
    billed = sum(_line_total(i) for i in items if i["preorder_item_id"] is None or i["preorder_item_id"] in keep_ids)
    billed = max(billed - min(rec["discount"] or 0, billed) - (rec["return_total"] or 0), 0)
    return rec["amount_paid"], billed


def preview_preorder_cancel(preorder_id: str, item_ids: list[int] | None = None) -> dict:
    """What a cancellation would do to the money, for the confirmation dialog. `item_ids` None = the
    whole order (customer request); a list = those lines (Pembatalan Khusus)."""
    with _conn() as con:
        po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if po is None:
            raise ValueError("Sales Order tidak ditemukan.")
        po = dict(po)
        lines = [dict(r) for r in con.execute("SELECT * FROM preorder_items WHERE preorder_id=?", (preorder_id,))]
        check = _so_cancel_check(con, po, lines)
        if not po.get("receipt_id"):
            return {"check": check, "paid": 0, "billed_after": 0, "refund_max": 0}
        open_ids = {ln["id"] for ln in lines if ln["status"] not in PO_CLOSED_LINE_STATUSES}
        drop = open_ids if item_ids is None else open_ids & set(item_ids)
        keep = [ln for ln in lines if ln["status"] != PO_LINE_CANCELLED and ln["id"] not in drop]
        rid = _so_cancel_invoice(con, po, [ln for ln in lines if ln["id"] in drop])
        paid, billed = _so_amounts(con, po, keep, rid)
        return {"check": check, "paid": paid, "billed_after": billed, "refund_max": max(paid - billed, 0), "receipt_id": rid}


def cancel_preorder(preorder_id: str, reason: str, refund_amount: int, refund_method: str,
                    by: str | None = None, item_ids: list[int] | None = None, refund_proof: str | None = None) -> dict:
    """Batalkan at the customer's request (admin). `item_ids` None = every line the policy allows;
    a list = those lines, each of which must be allowed (shop-stock lines any time before they are
    installed; indent lines only inside the 24-hour window and before their Purchase Order is
    issued). Draft Purchase Orders lose the lines, the shelf stock held for them is free again, and
    the admin decides how much of the money beyond the lines that stay goes back (the rest is kept
    as a cancellation fee on the invoice). The order itself is cancelled once nothing is left open."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Alasan pembatalan wajib diisi (minimal 5 huruf).")
    if refund_method not in SO_REFUND_METHODS:
        raise ValueError("Metode refund harus salah satu dari: cash, transfer, qris, debit.")
    now = _now()
    with _conn() as con:
        po, lines = _so_load_for_cancel(con, preorder_id)
        check = _so_cancel_check(con, po, lines)
        if not check["allowed"] and not check["lines"]:
            raise ValueError(check["reasons"][0])
        if not check["allowed"]:
            raise ValueError((
                "Tidak ada barang yang boleh dibatalkan: " + "; ".join(r.rstrip(".") for r in check["reasons"]) + ". "
                + ("Bila toko atau supplier gagal memenuhi pesanan, admin dapat memakai Pembatalan Khusus."
                   if check["special_allowed"] else "")).strip())
        rule = {x["id"]: x for x in check["lines"]}
        by_id = {ln["id"]: ln for ln in lines}
        if item_ids is None:
            drop = [by_id[x["id"]] for x in check["lines"] if x["allowed"]]
        else:
            drop = []
            for iid in dict.fromkeys(item_ids):
                x = rule.get(iid)
                if x is None:
                    raise ValueError("Barang itu tidak ada, sudah terpasang, atau sudah dibatalkan.")
                if not x["allowed"]:
                    raise ValueError(f"“{x['product_name']}” tidak bisa dibatalkan: {x['reason']}.")
                drop.append(by_id[iid])
            if not drop:
                raise ValueError("Pilih barang yang dibatalkan.")
        drop_ids = {ln["id"] for ln in drop}
        keep = [ln for ln in lines if ln["status"] != PO_LINE_CANCELLED and ln["id"] not in drop_ids]
        rid = _so_cancel_invoice(con, po, drop)
        paid, billed = _so_amounts(con, po, keep, rid)
        over = max(paid - billed, 0)
        if refund_amount < 0 or refund_amount > over:
            raise ValueError(f"Jumlah refund harus 0 sampai {_rp(over)} (uang yang sudah dibayar di luar barang yang tetap ditagih)." if over
                             else "Tidak ada uang yang bisa dikembalikan: pembayaran masih dipakai untuk barang yang tidak dibatalkan.")
        before = _po_invoice_snapshot(con, preorder_id)
        _so_mark_lines_cancelled(con, po, drop, "pelanggan", reason, by, now)
        for ln in drop:
            _so_drop_from_draft_pos(con, po, ln, by)
        if not any(ln["status"] not in PO_CLOSED_LINE_STATUSES for ln in keep):
            con.execute("""UPDATE preorders SET cancelled_at=?, cancelled_by=?, cancel_kind='pelanggan', cancel_reason=?,
                           updated_at=? WHERE id=?""", (now, by, reason, now, preorder_id))
        else:
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        fee = over - refund_amount
        note = (f"Sales Order {po['po_number']}: dibatalkan atas permintaan pelanggan — "
                f"{', '.join(ln['product_name'] for ln in drop)}. Alasan: {reason}. "
                f"Refund {_rp(refund_amount)}" + (f", biaya pembatalan {_rp(fee)}" if fee else ""))
        _so_settle_invoice(con, po, before, refund_amount, refund_method, fee, note, by, rid, refund_proof)
    return get_preorder(preorder_id)


def cancel_preorder_items(preorder_id: str, item_ids: list[int], category: str, reason: str,
                          refund_method: str, by: str | None = None, refund_proof: str | None = None) -> dict:
    """Pembatalan Khusus (admin): the shop or the supplier failed the customer on these lines. They are
    cancelled whatever the 24-hour window or issued Purchase Orders say, and the customer gets back in
    full what they paid beyond the lines they keep. Issued Purchase Orders are NOT cancelled and no
    stock is released by this: a line whose goods already sit on the shelf must be dealt with first
    (returned to the supplier or handed over), and each issued PO is flagged for the admin."""
    reason = (reason or "").strip()
    if category not in SO_FAULT_CATEGORIES:
        raise ValueError("Pilih alasan pembatalan khusus: " + ", ".join(SO_FAULT_CATEGORIES.values()) + ".")
    if len(reason) < 10:
        raise ValueError("Jelaskan kejadiannya (minimal 10 huruf): apa yang gagal, kapan, dan buktinya.")
    if refund_method not in SO_REFUND_METHODS:
        raise ValueError("Metode refund harus salah satu dari: cash, transfer, qris, debit.")
    if not item_ids:
        raise ValueError("Pilih barang yang dibatalkan.")
    now = _now()
    with _conn() as con:
        po, lines = _so_load_for_cancel(con, preorder_id)
        check = _so_cancel_check(con, po, lines)
        if not check["special_allowed"]:
            raise ValueError("Tidak bisa: " + "; ".join(check["reasons"]))
        by_id = {ln["id"]: ln for ln in lines}
        drop = []
        for iid in dict.fromkeys(item_ids):
            ln = by_id.get(iid)
            if ln is None:
                raise ValueError("Barang tidak ada di Sales Order ini.")
            if ln["status"] in PO_INSTALLED_STATUSES:
                raise ValueError(f"“{ln['product_name']}” sudah terpasang; pakai Retur Barang di invoice.")
            if ln["status"] == PO_LINE_CANCELLED:
                raise ValueError(f"“{ln['product_name']}” sudah dibatalkan.")
            drop.append(ln)
        held = {r["id"]: r["reserved"] for r in _reservation_lines(con, [ln["master_item_id"] for ln in drop])}
        for ln in drop:
            if held.get(ln["id"], 0) > 0:
                raise ValueError(
                    f"“{ln['product_name']}”: {held[ln['id']]} barang untuk pelanggan ini sudah ada di toko. "
                    "Selesaikan barangnya dulu (retur ke supplier lewat Purchase Order / Barang Masuk, atau serahkan ke pelanggan).")
        drop_ids = {ln["id"] for ln in drop}
        keep = [ln for ln in lines if ln["status"] != PO_LINE_CANCELLED and ln["id"] not in drop_ids]
        rid = _so_cancel_invoice(con, po, drop)
        paid, billed = _so_amounts(con, po, keep, rid)
        refund = max(paid - billed, 0)
        issued = _so_issued_po_refs(con, list(drop_ids))
        before = _po_invoice_snapshot(con, preorder_id)
        label = SO_FAULT_CATEGORIES[category]
        _so_mark_lines_cancelled(con, po, drop, category, reason, by, now)
        for ln in drop:
            _so_drop_from_draft_pos(con, po, ln, by)
        for ref in issued:
            _spo_event(con, ref["supplier_po_id"], "so_cancelled",
                       f"PERLU TINDAKAN: Sales Order {po['po_number']} membatalkan “{ref['product_name']}” "
                       f"(Pembatalan Khusus: {label}). Barang ini tidak lagi untuk pelanggan; ajukan pembatalan item ke supplier, "
                       "atau terima sebagai stok toko.", by)
        whole = not any(ln["status"] not in PO_CLOSED_LINE_STATUSES for ln in keep)
        if whole:
            con.execute("""UPDATE preorders SET cancelled_at=?, cancelled_by=?, cancel_kind=?, cancel_reason=?,
                           updated_at=? WHERE id=?""", (now, by, category, reason, now, preorder_id))
        else:
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        names = ", ".join(ln["product_name"] for ln in drop)
        note = (f"Pembatalan Khusus Sales Order {po['po_number']} ({label}): {names}. {reason}. "
                f"Refund penuh {_rp(refund)}"
                + (f". PO yang sudah terbit tidak dibatalkan otomatis: {', '.join(sorted({r['po_number'] for r in issued}))}"
                   if issued else ""))
        _so_settle_invoice(con, po, before, refund, refund_method, 0, note, by, rid, refund_proof)
    return get_preorder(preorder_id)


# Preorders are archived as drafts (is_draft=1 via update_preorder), never
# deleted -- there is deliberately no delete_preorder function. Do not re-add one.


# --- Supplier POs (Purchase Order) --------------------------------------------
# A purchase order to a supplier, built from the lines customers have preordered or
# typed in by hand, and kept as a purchase invoice (discount, PPN, shipping, payments).
# It never touches stock_ledger or master_items.stock_qty itself: goods enter stock only
# through Barang Masuk. Receiving can open a DRAFT Barang Masuk document for the lines
# that are linked to a master item; that document still has to be posted there.

SUPPLIER_PO_STATUSES = ("draft", "dipesan", "sebagian_diterima", "diterima", "batal")
# Every status except cancelled holds quantity: a line already on a draft cannot be
# put on a second PO, or the same barang would be ordered twice.
_SPO_COUNTED = ("draft", "dipesan", "sebagian_diterima", "diterima")
_SPO_ISSUED = ("dipesan", "sebagian_diterima", "diterima")
# Where an issued PO is until goods start arriving.
SPO_STAGES = ("menunggu_konfirmasi", "dikonfirmasi", "diproses", "dalam_pengiriman")
SPO_ORDER_STATUSES = ("draft",) + SPO_STAGES + ("diterima_sebagian", "diterima_lengkap", "dibatalkan")
SPO_PAYMENT_STATUSES = ("belum_ditagih", "belum_bayar", "sebagian", "lunas", "lewat_jatuh_tempo")
SPO_CANCEL_ACTIONS = ("kembali_ke_customer", "simpan_stok_toko", "retur_supplier", "tidak_ada")
SPO_PAY_METHODS = ("transfer", "cash", "giro", "lainnya")
# What a PO line can be before any of it arrives; "diterima_sebagian" / "diterima" / "dibatalkan" are derived.
SPO_ITEM_SHIP_STATUSES = ("menunggu_konfirmasi", "dikonfirmasi", "diproses", "dalam_pengiriman", "tiba_di_hub")
SPO_SHIP_METHODS = ("udara", "laut", "darat", "ambil_sendiri", "lainnya")
SPO_ITEM_CONDITIONS = ("baik", "rusak", "tidak_sesuai")
_SPO_QTY_LEFT_SQL = "(spi.quantity - spi.qty_cancelled)"


def _next_supplier_po_no(con: sqlite3.Connection, po_date: str) -> str:
    """PO-SUP-YYYYMMDD-NNN: a counter per PO date, restarting at 001. (Older POs keep their number:
    PO-SUP-NNN, or POS-YYYYMMDD-NNNN before that.)"""
    prefix = f"PO-SUP-{po_date.replace('-', '')}-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(po_number, ?) AS INTEGER)) AS n FROM supplier_pos WHERE po_number LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:03d}"


def peek_next_supplier_po_no(po_date: str | None = None) -> str:
    """The number the next Purchase Order dated `po_date` will get (shown read-only on the form)."""
    with _conn() as con:
        return _next_supplier_po_no(con, po_date or shop_today().isoformat())


def _next_internal_invoice_no(con: sqlite3.Connection, po_date: str) -> str:
    """INV-SUP-YYYYMMDD-NNN: our own number for the purchase invoice, one counter per day."""
    prefix = f"INV-SUP-{po_date.replace('-', '')[:8]}-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(internal_invoice_no, ?) AS INTEGER)) AS n FROM supplier_pos WHERE internal_invoice_no LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:03d}"


def _placeholders(values: tuple) -> str:
    return ",".join("?" for _ in values)


def _spo_event(con: sqlite3.Connection, supplier_po_id: str, kind: str, note: str | None, by: str | None) -> None:
    con.execute(
        "INSERT INTO spo_events (supplier_po_id, at, by_user, kind, note) VALUES (?, ?, ?, ?, ?)",
        (supplier_po_id, _now(), by, kind, note),
    )


def _spo_candidate_rows(con: sqlite3.Connection, query: str | None, exclude_po: str | None) -> list[dict]:
    """Customer preorder lines that still have quantity nobody has ordered yet.
    `exclude_po` is the supplier PO being edited, so its own quantities count as
    available again. Lines already at the shop (or installed) need no ordering."""
    params: list = list(_SPO_COUNTED)
    exclude_sql = ""
    if exclude_po:
        exclude_sql = " AND sp.id != ?"
        params.append(exclude_po)
    where = ["p.is_draft=0", "pi.status NOT IN ('sampai_di_toko','dititipkan','terpasang','dibatalkan')",
             "COALESCE(pi.source, '') NOT IN ('jasa', 'stok')"]
    if query:
        where.append(
            "(p.po_number LIKE ? OR p.customer_name LIKE ? OR p.plate_full LIKE ? OR p.customer_phone LIKE ? "
            "OR pi.product_name LIKE ?)"
        )
        params.extend([f"%{query}%"] * 5)
    rows = con.execute(
        f"""SELECT pi.id AS preorder_item_id, pi.preorder_id, p.po_number AS customer_po_number,
                   p.customer_name, p.plate_full, pi.product_name, pi.unit, pi.quantity AS qty_ordered_by_customer,
                   pi.master_item_id, COALESCE(o.ordered, 0) AS qty_bought,
                   pi.ship_route, pi.priority, pi.eta_date, pi.source, s.id AS supplier_id, s.name AS supplier_name,
                   CASE WHEN COALESCE(pi.cost_hint, 0) > 0 THEN pi.cost_hint ELSE COALESCE(m.last_cost_price, 0) END AS est_unit_price
            FROM preorder_items pi
            JOIN preorders p ON p.id = pi.preorder_id
            LEFT JOIN (
                SELECT spi.preorder_item_id, SUM({_SPO_QTY_LEFT_SQL}) AS ordered
                FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                WHERE sp.status IN ({_placeholders(_SPO_COUNTED)}){exclude_sql}
                GROUP BY spi.preorder_item_id
            ) o ON o.preorder_item_id = pi.id
            LEFT JOIN master_items m ON m.id = pi.master_item_id
            LEFT JOIN suppliers s ON s.id = CASE WHEN pi.source IN ('supplier', 'campur') THEN COALESCE(pi.supplier_id, p.supplier_id)
                                                 ELSE COALESCE(p.supplier_id, m.supplier_id) END
            WHERE {' AND '.join(where)}
            ORDER BY p.created_at DESC, pi.position ASC, pi.id ASC""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["qty_left"] = d["qty_ordered_by_customer"] - d["qty_bought"]
        if d["qty_left"] > 0:
            out.append(d)
    return out


def get_supplier_po_candidates(
    query: str | None = None, progress: str = "belum", exclude_po: str | None = None,
    limit: int = 10, offset: int = 0,
) -> dict:
    """`progress` is the modal's filter: 'belum' = nothing ordered yet, 'sebagian' =
    some ordered, 'semua' = both. The counts always cover the search but not the
    filter, so the three tabs can show their numbers at once."""
    with _conn() as con:
        rows = _spo_candidate_rows(con, query, exclude_po)
    belum = [r for r in rows if r["qty_bought"] == 0]
    sebagian = [r for r in rows if r["qty_bought"] > 0]
    chosen = {"belum": belum, "sebagian": sebagian}.get(progress, rows)
    return {
        "rows": chosen[max(offset, 0): max(offset, 0) + limit],
        "total": len(chosen),
        "counts": {"belum": len(belum), "sebagian": len(sebagian), "semua": len(rows)},
    }


def _spo_money(po: dict, items: list[dict]) -> dict:
    """The invoice maths: per-line discount %, then the header discount %, then PPN on that,
    then shipping. Cancelled quantity is not billed."""
    lines = 0
    for i in items:
        gross = (i["quantity"] - i["qty_cancelled"]) * i["est_unit_price"]
        lines += gross - round(gross * (i["discount_pct"] or 0) / 100)
    discount = round(lines * (po["discount_pct"] or 0) / 100)
    taxable = lines - discount
    tax = round(taxable * (po["tax_rate"] or 0) / 100)
    total = taxable + tax + (po["shipping_cost"] or 0)
    paid = po["paid_amount"] or 0
    return {"subtotal": lines, "discount_amount": discount, "tax_amount": tax, "grand_total": total,
            "outstanding": max(total - paid, 0)}


def _spo_order_status(po: dict) -> str:
    mapped = {"draft": "draft", "sebagian_diterima": "diterima_sebagian",
              "diterima": "diterima_lengkap", "batal": "dibatalkan"}.get(po["status"])
    return mapped or (po["stage"] if po["stage"] in SPO_STAGES else SPO_STAGES[0])


def _spo_payment_status(po: dict, grand_total: int) -> str | None:
    """None for a draft or cancelled PO: there is nothing to pay yet. K-12: the debt is the posted
    Faktur Pembelian's; without one the PO is not billed yet (money paid so far is DP)."""
    if po["status"] in ("draft", "batal"):
        return None
    f = po.get("_faktur")
    if not f or f["status"] != "posted":
        return "belum_ditagih"
    left = f["total"] - f["amount_paid"]
    if left <= 0:
        return "lunas"
    if f["due_date"] and f["due_date"] < shop_today().isoformat():
        return "lewat_jatuh_tempo"
    return "sebagian" if f["amount_paid"] > 0 else "belum_bayar"


def _spo_totals(po: dict, items: list[dict]) -> dict:
    money = _spo_money(po, items)
    return {
        "item_count": len(items),
        "total_estimate": sum((i["quantity"] - i["qty_cancelled"]) * i["est_unit_price"] for i in items),
        "qty_total": sum(i["quantity"] - i["qty_cancelled"] for i in items),
        "qty_received": sum(i["qty_received"] for i in items),
        "order_status": _spo_order_status(po),
        "payment_status": _spo_payment_status(po, money["grand_total"]),
        **money,
        **_spo_faktur_fields(po, money),
    }


def _spo_faktur_fields(po: dict, money: dict) -> dict:
    """K-12 view of a PO's money: DP paid on the PO (and how much is still unapplied), and its faktur."""
    f = po.get("_faktur")
    dp_paid = po["paid_amount"] or 0
    out = {"dp_paid": dp_paid, "dp_unapplied": po.get("_dp_unapplied", dp_paid),
           "faktur_id": None, "faktur_number": None, "faktur_status": None, "faktur_total": None,
           "faktur_paid": None, "faktur_outstanding": None}
    if f:
        out.update({"faktur_id": f["id"], "faktur_number": f["invoice_number"], "faktur_status": f["status"],
                    "faktur_total": f["total"], "faktur_paid": f["amount_paid"],
                    "faktur_outstanding": max(f["total"] - f["amount_paid"], 0)})
        if f["status"] == "posted":
            out["outstanding"] = out["faktur_outstanding"]
    return out


def _spo_attach_faktur(con: sqlite3.Connection, pos: list[dict]) -> None:
    fakturs = {}
    for r in con.execute("SELECT * FROM purchase_invoices WHERE supplier_po_id IS NOT NULL AND status<>'void' ORDER BY id ASC"):
        fakturs[r["supplier_po_id"]] = dict(r)
    dp = {r["supplier_po_id"]: r["left"] for r in con.execute(
        "SELECT supplier_po_id, COALESCE(SUM(amount - applied_amount), 0) AS left FROM spo_payments GROUP BY supplier_po_id")}
    for po in pos:
        po["_faktur"] = fakturs.get(po["id"])
        po["_dp_unapplied"] = dp.get(po["id"], 0)


def _spo_items(con: sqlite3.Connection, supplier_po_id: str) -> list[dict]:
    rows = con.execute(
        """SELECT spi.*, p.po_number AS customer_po_number, p.customer_name, p.plate_full, pi.ship_route AS so_ship_route,
                  mi.sell_price AS sell_price, mi.barcode AS barcode
           FROM supplier_po_items spi
           LEFT JOIN preorder_items pi ON pi.id = spi.preorder_item_id
           LEFT JOIN preorders p ON p.id = pi.preorder_id
           LEFT JOIN master_items mi ON mi.id = spi.master_item_id
           WHERE spi.supplier_po_id=? ORDER BY spi.position ASC, spi.id ASC""",
        (supplier_po_id,),
    ).fetchall()
    po_stage = con.execute("SELECT stage FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()["stage"]
    out = [dict(r) for r in rows]
    _spo_attach_pkg(out, _spo_packages(con, supplier_po_id))
    for d in out:
        d["item_status"] = _spo_item_effective_status(d, po_stage)
        pkg = d.get("_pkg")
        d["pkg_open"] = pkg["open"] if pkg else 0
        d["pkg_moving"] = pkg["moving"] if pkg else 0
        d["pkg_free"] = pkg["free"] if pkg else max(d["quantity"] - d["qty_cancelled"] - d["qty_received"], 0)
        d["has_packages"] = bool(pkg)
        if pkg and pkg["etas"]:
            d["eta_date"] = min(pkg["etas"])
        d.pop("_pkg", None)
    return out


def _spo_item_effective_status(item: dict, po_stage: str | None) -> str:
    """Where one PO line is: derived once goods arrive or the line is cancelled, else what was set on it,
    else the PO's own stage."""
    live = item["quantity"] - item["qty_cancelled"]
    if live <= 0:
        return "dibatalkan"
    if item["qty_received"] >= live:
        return "diterima"
    base = item.get("item_status") or (po_stage if po_stage in SPO_STAGES else SPO_STAGES[0])
    pkg = item.get("_pkg")
    if pkg and pkg["open"] > 0 and item["qty_received"] < live:
        # A line split into packages is as far along as its least advanced package still on the road
        # (and its unpackaged part, if any, counts at the line's own status).
        seen = list(pkg["statuses"]) + ([base] if pkg["free"] > 0 else [])
        rank = lambda st: SPO_ITEM_SHIP_STATUSES.index(st) if st in SPO_ITEM_SHIP_STATUSES else 0
        base = min(seen, key=rank)
    if item["qty_received"] > 0:
        return "diterima_sebagian"
    return base


def get_supplier_po(supplier_po_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            return None
        po = dict(row)
        items = _spo_items(con, supplier_po_id)
        events = [dict(r) for r in con.execute(
            "SELECT * FROM spo_events WHERE supplier_po_id=? ORDER BY at ASC, id ASC", (supplier_po_id,))]
        payments = [dict(r) for r in con.execute(
            "SELECT * FROM spo_payments WHERE supplier_po_id=? ORDER BY paid_at ASC, id ASC", (supplier_po_id,))]
        cancellations = [dict(r) for r in con.execute(
            "SELECT * FROM spo_cancellations WHERE supplier_po_id=? ORDER BY cancelled_at ASC, id ASC", (supplier_po_id,))]
        receipts = []
        for r in con.execute(
            """SELECT r.*, d.status AS inbound_status FROM spo_receipts r
               LEFT JOIN inbound_documents d ON d.id = r.inbound_document_id
               WHERE r.supplier_po_id=? ORDER BY r.created_at ASC, r.id ASC""", (supplier_po_id,)
        ).fetchall():
            lines = [dict(l) for l in con.execute(
                """SELECT l.item_id, l.quantity, l.condition, l.location, l.package_id, spi.product_name FROM spo_receipt_lines l
                   JOIN supplier_po_items spi ON spi.id = l.item_id WHERE l.receipt_id=?""", (r["id"],))]
            receipts.append({**dict(r), "lines": lines})
        shipments = [dict(r) for r in con.execute(
            "SELECT * FROM spo_item_shipments WHERE supplier_po_id=? ORDER BY at ASC, id ASC", (supplier_po_id,))]
        packages = _spo_packages(con, supplier_po_id)
        pkg_history = [dict(r) for r in con.execute(
            "SELECT * FROM spo_package_history WHERE supplier_po_id=? ORDER BY at ASC, id ASC", (supplier_po_id,))]
        dr = con.execute("SELECT * FROM spo_receive_drafts WHERE supplier_po_id=?", (supplier_po_id,)).fetchone()
        receive_draft = {**json.loads(dr["payload"]), "saved_by": dr["saved_by"], "saved_at": dr["saved_at"]} if dr else None
        # K-13: lines still coming whose customer line was cancelled (Pembatalan Khusus). They stay
        # until the admin cancels them with the supplier or receives them as shop stock.
        so_cancelled = [dict(r) for r in con.execute(
            """SELECT spi.id, spi.product_name, p.po_number AS customer_po_number, p.customer_name,
                      spi.quantity - spi.qty_cancelled - spi.qty_received AS qty_left
               FROM supplier_po_items spi JOIN preorder_items pi ON pi.id = spi.preorder_item_id
               JOIN preorders p ON p.id = pi.preorder_id
               WHERE spi.supplier_po_id=? AND pi.status=? AND spi.quantity - spi.qty_cancelled - spi.qty_received > 0""",
            (supplier_po_id, PO_LINE_CANCELLED))] if po["status"] not in ("draft", "batal") else []
    with _conn() as con:
        _spo_attach_faktur(con, [po])
    claims = list_refund_claims(supplier_po_id)
    with _conn() as con:
        open_invoices = [dict(r) for r in con.execute(
            """SELECT id, invoice_number, total, amount_paid, total - amount_paid AS outstanding, due_date FROM purchase_invoices
               WHERE supplier_id=? AND status='posted' AND total > amount_paid ORDER BY due_date, id""", (po["supplier_id"],))] if claims else []
    return {**po, **_spo_totals(po, items), "refund_claims": claims, "open_invoices": open_invoices,
            "items": items, "events": events, "payments": payments,
            "receipts": receipts, "cancellations": cancellations, "shipments": shipments,
            "packages": packages, "package_history": pkg_history, "receive_draft": receive_draft, "so_cancelled_lines": so_cancelled,
            "shipping_plan": _spo_shipping_plan(po, items, packages)}


def _spo_shipping_status(po: dict, items: list[dict]) -> str | None:
    """Where the goods are, across the lines still open: belum_dikirim / diproses / dalam_pengiriman / tiba."""
    if po["status"] in ("draft", "batal"):
        return None
    live = [i for i in items if i["quantity"] - i["qty_cancelled"] > 0]
    if not live:
        return None
    eff = [_spo_item_effective_status(i, po["stage"]) for i in live]
    if all(i["qty_received"] >= i["quantity"] - i["qty_cancelled"] for i in live) or all(e == "tiba_di_hub" for e in eff):
        return "tiba"
    if "dalam_pengiriman" in eff:
        return "dalam_pengiriman"
    if "diproses" in eff:
        return "diproses"
    return "belum_dikirim"


def _spo_receipt_status(po: dict, items: list[dict]) -> str | None:
    if po["status"] in ("draft", "batal"):
        return None
    ordered = sum(i["quantity"] - i["qty_cancelled"] for i in items)
    got = sum(i["qty_received"] for i in items)
    if ordered > 0 and got >= ordered:
        return "lengkap"
    return "sebagian" if got > 0 else "belum"


def get_all_supplier_pos(
    query: str | None = None, status: str | None = None, order_status: str | None = None,
    payment_status: str | None = None, shipping_status: str | None = None, receipt_status: str | None = None,
    supplier_id: int | None = None, date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    where, params = [], []
    if supplier_id is not None:
        where.append("sp.supplier_id=?")
        params.append(supplier_id)
    if date_from:
        where.append("sp.po_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("sp.po_date <= ?")
        params.append(date_to)
    if status:
        where.append("sp.status=?")
        params.append(status)
    if query:
        where.append(
            "(sp.po_number LIKE ? OR sp.supplier_name_snapshot LIKE ? OR sp.supplier_invoice_no LIKE ? OR sp.id IN ("
            "SELECT spi.supplier_po_id FROM supplier_po_items spi "
            "LEFT JOIN preorder_items pi ON pi.id = spi.preorder_item_id "
            "LEFT JOIN preorders p ON p.id = pi.preorder_id "
            "WHERE spi.product_name LIKE ? OR spi.sku LIKE ? OR p.po_number LIKE ? OR p.customer_name LIKE ? OR p.plate_full LIKE ?))"
        )
        params.extend([f"%{query}%"] * 8)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT sp.* FROM supplier_pos sp {where_sql} ORDER BY sp.created_at DESC, sp.rowid DESC", params
        ).fetchall()]
        _spo_attach_faktur(con, rows)
        items_by_po: dict[str, list[dict]] = {}
        for item in con.execute("SELECT * FROM supplier_po_items").fetchall():
            items_by_po.setdefault(item["supplier_po_id"], []).append(dict(item))
        all_pkgs = _spo_packages(con)
        for po_id, its_ in items_by_po.items():
            _spo_attach_pkg(its_, [p for p in all_pkgs if p["supplier_po_id"] == po_id])
        refs_by_po: dict[str, list[dict]] = {}
        for ref in con.execute(
            """SELECT spi.supplier_po_id AS po_id, p.po_number, p.customer_name FROM supplier_po_items spi
               JOIN preorder_items pi ON pi.id = spi.preorder_item_id JOIN preorders p ON p.id = pi.preorder_id
               ORDER BY p.po_number"""
        ).fetchall():
            seen = refs_by_po.setdefault(ref["po_id"], [])
            if not any(x["po_number"] == ref["po_number"] for x in seen):
                seen.append({"po_number": ref["po_number"], "customer_name": ref["customer_name"]})
    out = []
    for r in rows:
        its = items_by_po.get(r["id"], [])
        out.append({**r, **_spo_totals(r, its), "customer_refs": refs_by_po.get(r["id"], []),
                    "item_names": [i["product_name"] for i in its],
                    "shipping_status": _spo_shipping_status(r, its), "receipt_status": _spo_receipt_status(r, its)})
    if order_status:
        out = [r for r in out if r["order_status"] == order_status]
    if payment_status:
        out = [r for r in out if r["payment_status"] == payment_status]
    if shipping_status:
        out = [r for r in out if r["shipping_status"] == shipping_status]
    if receipt_status:
        out = [r for r in out if r["receipt_status"] == receipt_status]
    return out


def get_supplier_po_summary() -> dict:
    """Header tiles of the Purchase Order list."""
    rows = get_all_supplier_pos()
    live = [r for r in rows if r["status"] not in ("draft", "batal")]
    open_pos = [r for r in live if r["status"] in ("dipesan", "sebagian_diterima")]
    return {
        "order_aktif": len(open_pos),
        "menunggu_konfirmasi": sum(1 for r in open_pos if r["order_status"] == "menunggu_konfirmasi"),
        "dalam_pengiriman": sum(1 for r in open_pos if r["order_status"] == "dalam_pengiriman"),
        "diterima_sebagian": sum(1 for r in live if r["order_status"] == "diterima_sebagian"),
        # K-12: the debt is what posted Faktur Pembelian still owe (with or without a PO); DP is money
        # already paid to suppliers that no faktur has absorbed yet.
        **(lambda s: {"faktur_belum_lunas": s["unpaid_count"], "total_utang": s["outstanding_total"]})(get_purchase_payable_summary()),
        "dp_supplier": sum(r["dp_unapplied"] for r in live),
    }


def _spo_validate_items(
    con: sqlite3.Connection, supplier_id: int, items: list[dict], exclude_po: str | None
) -> None:
    """Server-side guard behind the picker: the modal already greys these out, but a
    stale tab or a hand-made request must not be able to over-order a customer line
    or put another supplier's barang on this PO."""
    if not items:
        raise ValueError("Minimal harus ada satu barang.")
    seen: set[int] = set()
    left_by_line = {r["preorder_item_id"]: r for r in _spo_candidate_rows(con, None, exclude_po)}
    for item in items:
        if item["quantity"] <= 0:
            raise ValueError(f"Jumlah untuk “{item['product_name']}” harus lebih dari 0.")
        if item["est_unit_price"] < 0:
            raise ValueError(f"Harga untuk “{item['product_name']}” tidak boleh minus.")
        if not 0 <= (item.get("discount_pct") or 0) <= 100:
            raise ValueError(f"Diskon untuk “{item['product_name']}” harus 0-100%.")
        line_id = item.get("preorder_item_id")
        if line_id is None:
            continue
        if line_id in seen:
            raise ValueError(f"“{item['product_name']}” dipilih lebih dari sekali.")
        seen.add(line_id)
        line = left_by_line.get(line_id)
        if line is None:
            raise ValueError(f"“{item['product_name']}” sudah tidak tersedia untuk dipesan (sudah penuh dipesan atau sudah di toko).")
        if item["quantity"] > line["qty_left"]:
            raise ValueError(f"Jumlah “{item['product_name']}” melebihi sisa yang belum dibeli ({line['qty_left']}).")
        if line["supplier_id"] is not None and line["supplier_id"] != supplier_id:
            raise ValueError(f"“{item['product_name']}” berasal dari supplier {line['supplier_name']}, bukan supplier PO ini.")


def _spo_replace_items(con: sqlite3.Connection, supplier_po_id: str, items: list[dict]) -> None:
    con.execute("DELETE FROM supplier_po_items WHERE supplier_po_id=?", (supplier_po_id,))
    for position, item in enumerate(items):
        master_id = item.get("master_item_id")
        sku = (item.get("sku") or "").strip().upper() or None
        if master_id is None and sku:
            master_id = _resolve_master_item_id(con, sku)
        if master_id is not None and not sku:
            mi = con.execute("SELECT sku FROM master_items WHERE id=?", (master_id,)).fetchone()
            sku = mi["sku"] if mi else None
        ship_method = item.get("ship_method") or None
        if ship_method is not None and ship_method not in SPO_SHIP_METHODS:
            raise ValueError(f"Metode pengiriman harus salah satu dari: {', '.join(SPO_SHIP_METHODS)}.")
        eta = item.get("eta_date") or None
        if eta is not None:
            try:
                date.fromisoformat(eta)
            except ValueError:
                raise ValueError("Estimasi datang harus berformat YYYY-MM-DD.")
        con.execute(
            """INSERT INTO supplier_po_items
               (supplier_po_id, preorder_item_id, master_item_id, product_name, unit, quantity,
                est_unit_price, qty_received, position, sku, discount_pct, ship_method, courier, eta_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (supplier_po_id, item.get("preorder_item_id"), master_id, item["product_name"],
             item.get("unit"), item["quantity"], item["est_unit_price"], position, sku,
             item.get("discount_pct") or 0, ship_method, (item.get("courier") or "").strip() or None, eta),
        )


def _sync_preorder_line(con: sqlite3.Connection, preorder_item_id: int, changed_by: str | None, now: str) -> None:
    """Advances a customer line to match its supplier POs, and never moves it
    backwards (a line someone already set to 'dalam_pengiriman' stays there when
    a later PO is merely issued). Each move lands in the preorder's status history."""
    line = con.execute("SELECT * FROM preorder_items WHERE id=?", (preorder_item_id,)).fetchone()
    if line is None or line["status"] == PO_LINE_CANCELLED:
        return
    agg = con.execute(
        f"""SELECT COALESCE(SUM(spi.quantity - spi.qty_cancelled), 0) AS ordered,
                   COALESCE(SUM(spi.qty_received - spi.qty_bad), 0) AS received,
                   -- K-10: goods received on the PO page but whose Barang Masuk is still a draft are not stock yet,
                   -- so they do not move the Sales Order line until that document is posted.
                   COALESCE(SUM((SELECT COALESCE(SUM(srl.quantity), 0)
                                 FROM spo_receipt_lines srl
                                 JOIN spo_receipts sr ON sr.id = srl.receipt_id
                                 JOIN inbound_documents d ON d.id = sr.inbound_document_id
                                 WHERE srl.item_id = spi.id AND d.status = 'draft'
                                   AND COALESCE(srl.condition, 'baik') NOT IN ('rusak', 'tidak_sesuai'))), 0) AS awaiting_post
            FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
            WHERE spi.preorder_item_id=? AND sp.status IN ({_placeholders(_SPO_ISSUED)})""",
        (preorder_item_id, *_SPO_ISSUED),
    ).fetchone()
    # K-02: part of the line may be held from ready stock, with only the shortfall on a Purchase
    # Order; that part already sits on the shelf, so it counts as arrived.
    shelf = 0
    if line["master_item_id"] is not None:
        for ln in _reservation_lines(con, [line["master_item_id"]]):
            if ln["id"] == preorder_item_id:
                shelf = ln["reserved"] - min(ln["arrived"], ln["reserved"])
    if agg["received"] - agg["awaiting_post"] + shelf >= line["quantity"]:
        target = "sampai_di_toko"
    elif agg["ordered"] + shelf >= line["quantity"]:
        target = "sudah_dipesankan"
        # Diagram 08: once the supplier PO items covering the whole line are on their way
        # (Status Item Pengiriman = dalam_pengiriman / tiba_di_hub), the line follows.
        moving = 0
        pkgs_of_po: dict[str, list[dict]] = {}
        for spi in con.execute(
            f"""SELECT spi.*, sp.stage AS po_stage FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                WHERE spi.preorder_item_id=? AND sp.status IN ({_placeholders(_SPO_ISSUED)})""",
            (preorder_item_id, *_SPO_ISSUED),
        ).fetchall():
            if spi["supplier_po_id"] not in pkgs_of_po:
                pkgs_of_po[spi["supplier_po_id"]] = _spo_packages(con, spi["supplier_po_id"])
            summary = _spo_pkg_summary(pkgs_of_po[spi["supplier_po_id"]]).get(spi["id"])
            left = spi["quantity"] - spi["qty_cancelled"] - spi["qty_received"]
            if summary:
                # Split into packages: only the packages already on the road count; the unpackaged part
                # moves with the line's own status.
                free = max(left - summary["open"], 0)
                moving += summary["moving"]
                if free > 0 and (spi["item_status"] or spi["po_stage"]) in SPO_PKG_MOVING:
                    moving += free
            elif (spi["item_status"] or spi["po_stage"]) in SPO_PKG_MOVING:
                moving += max(left, 0)
        if agg["received"] - agg["awaiting_post"] + moving + shelf >= line["quantity"]:
            target = "dalam_pengiriman"
    else:
        return
    if PREORDER_STATUSES.index(target) <= PREORDER_STATUSES.index(line["status"]):
        return
    before = _po_invoice_snapshot(con, line["preorder_id"])
    con.execute("UPDATE preorder_items SET status=? WHERE id=?", (target, preorder_item_id))
    con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, line["preorder_id"]))
    _log_preorder_status(con, line["preorder_id"], preorder_item_id, line["product_name"], target, now,
                         changed_by, line["status"])
    _po_invoice_sync(con, line["preorder_id"], changed_by, before)


def _spo_sync_all(con: sqlite3.Connection, supplier_po_id: str, changed_by: str | None, now: str) -> None:
    for row in con.execute(
        "SELECT preorder_item_id FROM supplier_po_items WHERE supplier_po_id=? AND preorder_item_id IS NOT NULL",
        (supplier_po_id,),
    ).fetchall():
        _sync_preorder_line(con, row["preorder_item_id"], changed_by, now)


def _spo_supplier(con: sqlite3.Connection, supplier_id: int) -> dict:
    row = con.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if row is None:
        raise ValueError("Supplier tidak ditemukan.")
    if not row["is_active"]:
        raise ValueError("Supplier tidak aktif.")
    return dict(row)


_SPO_HEADER_FIELDS = (
    "supplier_invoice_no", "payment_terms_days", "due_date", "est_arrival_date",
    "discount_pct", "shipping_cost", "tax_rate",
)


def _spo_check_header(fields: dict) -> None:
    """Rules on the invoice fields, applied to whatever subset was sent."""
    if not 0 <= (fields.get("discount_pct") or 0) <= 100:
        raise ValueError("Diskon harus 0-100%.")
    if not 0 <= (fields.get("tax_rate") or 0) <= 100:
        raise ValueError("PPN harus 0-100%.")
    if (fields.get("shipping_cost") or 0) < 0:
        raise ValueError("Ongkos kirim tidak boleh minus.")
    days = fields.get("payment_terms_days")
    if days is not None and not 0 <= days <= 365:
        raise ValueError("Termin pembayaran harus 0-365 hari.")
    for key, label in (("due_date", "Jatuh tempo"), ("est_arrival_date", "Estimasi datang")):
        if fields.get(key):
            try:
                date.fromisoformat(fields[key])
            except ValueError:
                raise ValueError(f"{label} harus berformat YYYY-MM-DD.")


def _spo_fill_due_date(fields: dict, po_date: str) -> None:
    """A termin without a typed jatuh tempo means po_date + that many days."""
    if fields.get("payment_terms_days") is not None and not fields.get("due_date"):
        fields["due_date"] = (date.fromisoformat(po_date) + timedelta(days=fields["payment_terms_days"])).isoformat()


def create_supplier_po(
    supplier_po_id: str, supplier_id: int, items: list[dict], po_date: str | None = None,
    notes: str | None = None, issue: bool = False, created_by: str | None = None,
    header: dict | None = None, initial_paid: int = 0,
) -> dict:
    now = _now()
    po_date = po_date or shop_today().isoformat()
    header = {k: v for k, v in (header or {}).items() if k in _SPO_HEADER_FIELDS}
    _spo_check_header(header)
    _spo_fill_due_date(header, po_date)
    if initial_paid < 0:
        raise ValueError("Jumlah sudah dibayar tidak boleh minus.")
    if initial_paid and not issue:
        raise ValueError("Pembayaran hanya bisa dicatat setelah PO diterbitkan.")
    with _conn() as con:
        supplier = _spo_supplier(con, supplier_id)
        _spo_validate_items(con, supplier_id, items, None)
        con.execute(
            """INSERT INTO supplier_pos
               (id, po_number, po_date, supplier_id, supplier_name_snapshot, notes, status,
                created_by, created_at, updated_at, supplier_invoice_no, payment_terms_days, due_date,
                est_arrival_date, discount_pct, shipping_cost, tax_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (supplier_po_id, _next_supplier_po_no(con, po_date), po_date, supplier_id, supplier["name"],
             notes, "dipesan" if issue else "draft", created_by, now, now,
             header.get("supplier_invoice_no"), header.get("payment_terms_days"), header.get("due_date"),
             header.get("est_arrival_date"), header.get("discount_pct") or 0, header.get("shipping_cost") or 0,
             header.get("tax_rate") or 0),
        )
        _spo_replace_items(con, supplier_po_id, items)
        _spo_event(con, supplier_po_id, "created", "PO diterbitkan ke supplier" if issue else "Draft dibuat", created_by)
        if issue:
            con.execute("UPDATE supplier_pos SET internal_invoice_no=? WHERE id=?",
                        (_next_internal_invoice_no(con, shop_today().isoformat()), supplier_po_id))
            _spo_sync_all(con, supplier_po_id, created_by, now)
        if initial_paid:
            _spo_add_payment(con, supplier_po_id, initial_paid, "transfer", "Pembayaran awal", created_by)
    return apply_spo_so_routes(supplier_po_id, created_by) if issue else get_supplier_po(supplier_po_id)


def update_supplier_po(
    supplier_po_id: str, supplier_id: int | None = None, po_date: str | None = None,
    items: list[dict] | None = None, notes: str | None = None, update_notes: bool = False,
    header: dict | None = None, updated_by: str | None = None,
) -> dict:
    """Only a draft can be edited: once issued, the PO is what was sent to the supplier
    (the invoice fields stay editable through update_supplier_po_invoice)."""
    now = _now()
    header = {k: v for k, v in (header or {}).items() if k in _SPO_HEADER_FIELDS}
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError("Purchase Order yang sudah diterbitkan tidak bisa diubah.")
        new_supplier_id = supplier_id if supplier_id is not None else row["supplier_id"]
        supplier = _spo_supplier(con, new_supplier_id)
        new_date = po_date if po_date is not None else row["po_date"]
        merged = {k: header[k] if k in header else row[k] for k in _SPO_HEADER_FIELDS}
        _spo_check_header(merged)
        if "payment_terms_days" in header and "due_date" not in header:
            merged["due_date"] = None
        _spo_fill_due_date(merged, new_date)
        current_items = items if items is not None else [
            {"preorder_item_id": i["preorder_item_id"], "product_name": i["product_name"],
             "quantity": i["quantity"], "est_unit_price": i["est_unit_price"], "discount_pct": i["discount_pct"]}
            for i in _spo_items(con, supplier_po_id)
        ]
        _spo_validate_items(con, new_supplier_id, current_items, supplier_po_id)
        con.execute(
            f"""UPDATE supplier_pos SET supplier_id=?, supplier_name_snapshot=?, po_date=?, notes=?, updated_at=?,
                {', '.join(f + '=?' for f in _SPO_HEADER_FIELDS)} WHERE id=?""",
            (new_supplier_id, supplier["name"], new_date, notes if update_notes else row["notes"], now,
             *(merged[f] for f in _SPO_HEADER_FIELDS), supplier_po_id),
        )
        if items is not None:
            _spo_replace_items(con, supplier_po_id, items)
            # The price typed on a draft is remembered on the Sales Order line, so a line that is
            # rebuilt later (supplier changed, SO edited) does not fall back to a missing cost.
            for it in items:
                if it.get("preorder_item_id") and (it.get("est_unit_price") or 0) > 0:
                    con.execute("UPDATE preorder_items SET cost_hint=? WHERE id=?",
                                (it["est_unit_price"], it["preorder_item_id"]))
    return get_supplier_po(supplier_po_id)


def update_supplier_po_invoice(supplier_po_id: str, fields: dict, updated_by: str | None = None) -> dict:
    """Invoice side of an issued PO: faktur number, termin, jatuh tempo, estimasi datang, discount, PPN
    and shipping. The goods themselves are what was ordered and do not change here."""
    now = _now()
    fields = {k: v for k, v in fields.items() if k in _SPO_HEADER_FIELDS}
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] == "batal":
            raise ValueError("Purchase Order yang dibatalkan tidak bisa diubah.")
        merged = {k: fields[k] if k in fields else row[k] for k in _SPO_HEADER_FIELDS}
        _spo_check_header(merged)
        if "payment_terms_days" in fields and "due_date" not in fields:
            merged["due_date"] = None
        _spo_fill_due_date(merged, row["po_date"])
        con.execute(
            f"UPDATE supplier_pos SET updated_at=?, {', '.join(f + '=?' for f in _SPO_HEADER_FIELDS)} WHERE id=?",
            (now, *(merged[f] for f in _SPO_HEADER_FIELDS), supplier_po_id),
        )
        if fields:
            _spo_event(con, supplier_po_id, "invoice", "Data faktur diperbarui", updated_by)
    return get_supplier_po(supplier_po_id)


def issue_supplier_po(supplier_po_id: str, changed_by: str | None = None) -> dict:
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError("Hanya Purchase Order berstatus draft yang bisa diterbitkan.")
        _spo_validate_items(con, row["supplier_id"], [
            {"preorder_item_id": i["preorder_item_id"], "product_name": i["product_name"],
             "quantity": i["quantity"], "est_unit_price": i["est_unit_price"], "discount_pct": i["discount_pct"]}
            for i in _spo_items(con, supplier_po_id)
        ], supplier_po_id)
        con.execute(
            "UPDATE supplier_pos SET status='dipesan', stage='menunggu_konfirmasi', updated_at=?, "
            "internal_invoice_no=COALESCE(internal_invoice_no, ?) WHERE id=?",
            (now, _next_internal_invoice_no(con, shop_today().isoformat()), supplier_po_id))
        _spo_event(con, supplier_po_id, "stage", "PO diterbitkan ke supplier", changed_by)
        _spo_sync_all(con, supplier_po_id, changed_by, now)
    return apply_spo_so_routes(supplier_po_id, changed_by)


_STAGE_LABELS = {
    "menunggu_konfirmasi": "Menunggu Konfirmasi", "dikonfirmasi": "Dikonfirmasi",
    "diproses": "Diproses", "dalam_pengiriman": "Dalam Pengiriman",
}


def set_supplier_po_stage(supplier_po_id: str, to_stage: str, note: str, changed_by: str | None = None) -> dict:
    """Ubah Status Order. A note is required: the timeline is how the shop tells later what happened."""
    if to_stage not in SPO_STAGES:
        raise ValueError(f"Status order harus salah satu dari: {', '.join(SPO_STAGES)}.")
    if not note or not note.strip():
        raise ValueError("Catatan wajib diisi saat mengubah status order.")
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] != "dipesan":
            raise ValueError("Status order hanya bisa diubah untuk PO yang sudah diterbitkan dan belum ada barang diterima.")
        if row["stage"] == to_stage:
            raise ValueError("PO sudah berstatus itu.")
        con.execute("UPDATE supplier_pos SET stage=?, updated_at=? WHERE id=?", (to_stage, _now(), supplier_po_id))
        _spo_event(con, supplier_po_id, "stage", f"Status: {_STAGE_LABELS[to_stage]} — {note.strip()}", changed_by)
        _spo_sync_all(con, supplier_po_id, changed_by, _now())
    return get_supplier_po(supplier_po_id)


def set_supplier_po_tracking(supplier_po_id: str, lines: list[dict], changed_by: str | None = None) -> dict:
    """Resi, kurir and ETA per PO line: [{item_id, tracking_no, courier, eta_date}]."""
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] in ("draft", "batal"):
            raise ValueError("Tracking hanya untuk PO yang sudah diterbitkan.")
        for line in lines:
            if line.get("eta_date"):
                try:
                    date.fromisoformat(line["eta_date"])
                except ValueError:
                    raise ValueError("ETA harus berformat YYYY-MM-DD.")
            cur = con.execute(
                "UPDATE supplier_po_items SET tracking_no=?, courier=?, eta_date=? WHERE id=? AND supplier_po_id=?",
                (line.get("tracking_no"), line.get("courier"), line.get("eta_date"), line["item_id"], supplier_po_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Baris {line['item_id']} tidak ada di PO ini.")
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (_now(), supplier_po_id))
        _spo_event(con, supplier_po_id, "tracking", "Info pengiriman diperbarui", changed_by)
    return get_supplier_po(supplier_po_id)


_SHIP_STATUS_LABELS = {
    "menunggu_konfirmasi": "Menunggu Konfirmasi", "dikonfirmasi": "Dikonfirmasi", "diproses": "Diproses",
    "dalam_pengiriman": "Dalam Pengiriman", "tiba_di_hub": "Tiba di Hub",
}


def update_supplier_po_item_shipping(
    supplier_po_id: str, item_id: int, data: dict, changed_by: str | None = None
) -> dict:
    """Update Status Item Pengiriman: each PO line has its own shipping method, status, forwarder,
    resi and dates, because lines of one order can travel differently (one by air, one by sea).
    Every update is kept in spo_item_shipments."""
    status = data.get("item_status")
    if status not in SPO_ITEM_SHIP_STATUSES:
        raise ValueError(f"Status item harus salah satu dari: {', '.join(SPO_ITEM_SHIP_STATUSES)}.")
    method = data.get("ship_method")
    if method is not None and method not in SPO_SHIP_METHODS:
        raise ValueError(f"Metode pengiriman harus salah satu dari: {', '.join(SPO_SHIP_METHODS)}.")
    for key, label in (("shipped_date", "Tanggal kirim"), ("eta_date", "Estimasi tiba")):
        if data.get(key):
            try:
                date.fromisoformat(data[key])
            except ValueError:
                raise ValueError(f"{label} harus berformat YYYY-MM-DD.")
    if status in ("dalam_pengiriman", "tiba_di_hub") and not method:
        raise ValueError("Metode pengiriman wajib diisi untuk status ini.")
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Status pengiriman hanya bisa diubah pada PO yang sudah diterbitkan dan belum selesai.")
        item = con.execute(
            "SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?", (item_id, supplier_po_id)
        ).fetchone()
        if item is None:
            raise LookupError(f"Baris {item_id} tidak ada di PO ini.")
        if item["quantity"] - item["qty_cancelled"] - item["qty_received"] <= 0:
            raise ValueError("Item ini sudah diterima lengkap atau dibatalkan, status pengiriman tidak bisa diubah.")
        if _spo_pkg_open_qty(con, supplier_po_id, item_id) > 0:
            raise ValueError("Item ini sudah dibagi ke paket pengiriman. Ubah status lewat paketnya.")
        con.execute(
            """UPDATE supplier_po_items SET item_status=?, ship_method=?, courier=?, tracking_no=?, shipped_date=?,
                      eta_date=?, transit_location=? WHERE id=?""",
            (status, method, data.get("forwarder"), data.get("tracking_no"), data.get("shipped_date"),
             data.get("eta_date"), data.get("transit_location"), item_id),
        )
        con.execute(
            """INSERT INTO spo_item_shipments (supplier_po_id, item_id, at, by_user, status, ship_method, forwarder, tracking_no,
                                               shipped_date, eta_date, transit_location, note, notify_customer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (supplier_po_id, item_id, now, changed_by, status, method, data.get("forwarder"), data.get("tracking_no"),
             data.get("shipped_date"), data.get("eta_date"), data.get("transit_location"), data.get("note"),
             1 if data.get("notify_customer") else 0),
        )
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "item_shipping",
                   f"{item['product_name']}: {_SHIP_STATUS_LABELS[status]}"
                   + (f" ({method})" if method else "") + (f" — {data['note']}" if data.get("note") else ""), changed_by)
        _spo_sync_all(con, supplier_po_id, changed_by, now)
    return get_supplier_po(supplier_po_id)


# ---------------------------------------------------------------------------
# Master Ekspedisi and Paket Pengiriman
# ---------------------------------------------------------------------------
# A Purchase Order can travel in several packages; one PO line can be split over packages with a quantity
# each. A package has its own forwarder (Master Ekspedisi), route, resi, ETA and status. Goods arriving
# (Terima Barang / Barang Masuk) name the package they came in, so the package knows what is still on the
# road. Stock still moves only when the Barang Masuk is posted; a package never touches stock.

SPO_PKG_MOVING = ("dalam_pengiriman", "tiba_di_hub")


def _fwd_text(value, label: str, limit: int, required: bool = False) -> str | None:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{label} wajib diisi.")
    if len(text) > limit:
        raise ValueError(f"{label} maksimal {limit} karakter.")
    return text or None


def list_forwarders(include_inactive: bool = True) -> list[dict]:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM forwarders" + ("" if include_inactive else " WHERE is_active=1") + " ORDER BY name COLLATE NOCASE")]
        used = {r["fid"]: r["n"] for r in con.execute(
            "SELECT forwarder_id AS fid, COUNT(*) AS n FROM spo_packages WHERE forwarder_id IS NOT NULL GROUP BY forwarder_id")}
    for r in rows:
        r["package_count"] = used.get(r["id"], 0)
    return rows


def _forwarder_fields(data: dict) -> dict:
    return {"name": _fwd_text(data.get("name"), "Nama ekspedisi", 60, True), "phone": _fwd_text(data.get("phone"), "Telepon", 30),
            "contact": _fwd_text(data.get("contact"), "Kontak", 80), "note": _fwd_text(data.get("note"), "Catatan", 300)}


def create_forwarder(data: dict) -> dict:
    f = _forwarder_fields(data)
    with _conn() as con:
        if con.execute("SELECT 1 FROM forwarders WHERE name=? COLLATE NOCASE", (f["name"],)).fetchone():
            raise ValueError("Ekspedisi dengan nama itu sudah ada.")
        cur = con.execute("INSERT INTO forwarders (name, phone, contact, note, is_active, created_at) VALUES (?,?,?,?,1,?)",
                          (f["name"], f["phone"], f["contact"], f["note"], _now()))
        return dict(con.execute("SELECT * FROM forwarders WHERE id=?", (cur.lastrowid,)).fetchone())


def update_forwarder(forwarder_id: int, data: dict) -> dict:
    """Renaming is fine: packages keep the name they were made with, so old shipments stay readable."""
    f = _forwarder_fields(data)
    active = 0 if data.get("is_active") in (False, 0, "0") else 1
    with _conn() as con:
        if con.execute("SELECT 1 FROM forwarders WHERE id=?", (forwarder_id,)).fetchone() is None:
            raise LookupError("Ekspedisi tidak ditemukan.")
        if con.execute("SELECT 1 FROM forwarders WHERE name=? COLLATE NOCASE AND id<>?", (f["name"], forwarder_id)).fetchone():
            raise ValueError("Ekspedisi dengan nama itu sudah ada.")
        con.execute("UPDATE forwarders SET name=?, phone=?, contact=?, note=?, is_active=?, updated_at=? WHERE id=?",
                    (f["name"], f["phone"], f["contact"], f["note"], active, _now(), forwarder_id))
        return dict(con.execute("SELECT * FROM forwarders WHERE id=?", (forwarder_id,)).fetchone())


def delete_forwarder(forwarder_id: int) -> dict:
    """A forwarder that never carried a package is deleted; one already used is only switched off."""
    with _conn() as con:
        row = con.execute("SELECT * FROM forwarders WHERE id=?", (forwarder_id,)).fetchone()
        if row is None:
            raise LookupError("Ekspedisi tidak ditemukan.")
        if con.execute("SELECT 1 FROM spo_packages WHERE forwarder_id=?", (forwarder_id,)).fetchone():
            con.execute("UPDATE forwarders SET is_active=0, updated_at=? WHERE id=?", (_now(), forwarder_id))
            return {"deleted": False, "deactivated": True}
        con.execute("DELETE FROM forwarders WHERE id=?", (forwarder_id,))
        return {"deleted": True, "deactivated": False}


def _pkg_received(con: sqlite3.Connection, supplier_po_id: str) -> dict[tuple[int, int], int]:
    """(package_id, item_id) → quantity that arrived through that package."""
    return {(r["package_id"], r["item_id"]): r["q"] for r in con.execute(
        """SELECT l.package_id, l.item_id, SUM(l.quantity) AS q FROM spo_receipt_lines l
           JOIN spo_receipts r ON r.id = l.receipt_id
           WHERE r.supplier_po_id=? AND l.package_id IS NOT NULL GROUP BY l.package_id, l.item_id""", (supplier_po_id,))}


def _spo_packages(con: sqlite3.Connection, supplier_po_id: str | None = None) -> list[dict]:
    """Packages with their lines, how much of each arrived and the state shown to the user. With no PO id,
    every package of every PO (used by the PO list)."""
    where, params = ("WHERE supplier_po_id=?", (supplier_po_id,)) if supplier_po_id else ("", ())
    pkgs = [dict(r) for r in con.execute(f"SELECT * FROM spo_packages {where} ORDER BY id", params)]
    if not pkgs:
        return []
    by_id = {p["id"]: p for p in pkgs}
    received: dict[tuple[int, int], int] = {}
    for po_id in ({p["supplier_po_id"] for p in pkgs}):
        received.update(_pkg_received(con, po_id))
    for p in pkgs:
        p["lines"] = []
    for ln in con.execute(
        f"""SELECT pl.*, spi.product_name, spi.unit, spi.sku FROM spo_package_lines pl
            JOIN supplier_po_items spi ON spi.id = pl.item_id
            WHERE pl.package_id IN ({','.join('?' * len(by_id))}) ORDER BY pl.id""", tuple(by_id)):
        d = dict(ln)
        d["received"] = min(received.get((d["package_id"], d["item_id"]), 0), d["quantity"])
        d["remaining"] = max(d["quantity"] - d["received"], 0)
        by_id[d["package_id"]]["lines"].append(d)
    for p in pkgs:
        total = sum(l["quantity"] for l in p["lines"])
        got = sum(l["received"] for l in p["lines"])
        p["total_qty"], p["received_qty"], p["remaining_qty"] = total, got, total - got
        if p["cancelled"]:
            p["state"] = "dibatalkan"
        elif total > 0 and got >= total:
            p["state"] = "diterima"
        elif got > 0:
            p["state"] = "diterima_sebagian"
        else:
            p["state"] = p["status"]
        p["open"] = (not p["cancelled"]) and p["remaining_qty"] > 0
    return pkgs


def _spo_pkg_summary(pkgs: list[dict]) -> dict[int, dict]:
    """item_id → what the packages say about that PO line: qty still travelling, qty moving, statuses."""
    out: dict[int, dict] = {}
    for p in pkgs:
        if p["cancelled"]:
            continue
        for l in p["lines"]:
            if l["remaining"] <= 0:
                continue
            e = out.setdefault(l["item_id"], {"open": 0, "moving": 0, "statuses": [], "etas": []})
            e["open"] += l["remaining"]
            if p["status"] in SPO_PKG_MOVING:
                e["moving"] += l["remaining"]
            e["statuses"].append(p["status"])
            if p["eta_date"]:
                e["etas"].append(p["eta_date"])
    return out


def _spo_attach_pkg(items: list[dict], pkgs: list[dict]) -> None:
    """Gives each PO line its package summary (`_pkg`), so its effective status follows its packages."""
    summary = _spo_pkg_summary(pkgs)
    for it in items:
        e = summary.get(it["id"])
        if e is None:
            continue
        left = it["quantity"] - it["qty_cancelled"] - it["qty_received"]
        it["_pkg"] = {**e, "free": max(left - e["open"], 0)}


def _spo_pkg_free(con: sqlite3.Connection, supplier_po_id: str, item_id: int, exclude_package: int | None = None) -> int:
    """How much of a PO line is not yet put in any open package (can be added to a package)."""
    item = con.execute("SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?", (item_id, supplier_po_id)).fetchone()
    if item is None:
        raise ValueError(f"Baris {item_id} tidak ada di PO ini.")
    left = item["quantity"] - item["qty_cancelled"] - item["qty_received"]
    taken = sum(l["remaining"] for p in _spo_packages(con, supplier_po_id) if not p["cancelled"] and p["id"] != exclude_package
                for l in p["lines"] if l["item_id"] == item_id)
    return max(left - taken, 0)


def _spo_pkg_open_qty(con: sqlite3.Connection, supplier_po_id: str, item_id: int) -> int:
    return sum(l["remaining"] for p in _spo_packages(con, supplier_po_id) if not p["cancelled"]
               for l in p["lines"] if l["item_id"] == item_id)


def _spo_check_receive_package(con: sqlite3.Connection, supplier_po_id: str, item_id: int, package_id, qty: int) -> int | None:
    """Barang Masuk / Terima Barang must name the package the goods came in once the line is split into
    packages. Returns the package id to store (or None for a line with no open package)."""
    pkgs = [p for p in _spo_packages(con, supplier_po_id) if not p["cancelled"]]
    open_here = [p for p in pkgs if any(l["item_id"] == item_id and l["remaining"] > 0 for l in p["lines"])]
    if package_id in (None, "", 0):
        if open_here:
            names = ", ".join(p["package_no"] for p in open_here)
            raise ValueError(f"Barang ini sedang dalam paket pengiriman ({names}). Pilih paket yang datang.")
        return None
    package_id = int(package_id)
    pkg = next((p for p in pkgs if p["id"] == package_id), None)
    if pkg is None:
        raise ValueError("Paket pengiriman tidak ditemukan di PO ini atau sudah dibatalkan.")
    line = next((l for l in pkg["lines"] if l["item_id"] == item_id), None)
    if line is None:
        raise ValueError(f"Barang ini tidak ada di paket {pkg['package_no']}.")
    if qty > line["remaining"]:
        raise ValueError(f"Jumlah melebihi sisa paket {pkg['package_no']} untuk barang ini ({line['remaining']}). "
                         "Ubah isi paket dulu bila jumlah kirimnya memang berbeda.")
    return package_id


def _pkg_validate_info(data: dict) -> dict:
    status = data.get("status") or "menunggu_konfirmasi"
    if status not in SPO_ITEM_SHIP_STATUSES:
        raise ValueError(f"Status paket harus salah satu dari: {', '.join(SPO_ITEM_SHIP_STATUSES)}.")
    method = data.get("ship_method") or None
    if method is not None and method not in SPO_SHIP_METHODS:
        raise ValueError(f"Jalur pengiriman harus salah satu dari: {', '.join(SPO_SHIP_METHODS)}.")
    for key, label in (("shipped_date", "Tanggal kirim"), ("eta_date", "Estimasi tiba")):
        if data.get(key):
            try:
                date.fromisoformat(data[key])
            except ValueError:
                raise ValueError(f"{label} harus berformat YYYY-MM-DD.")
    if status in SPO_PKG_MOVING:
        if not method:
            raise ValueError("Jalur pengiriman (laut/udara/darat) wajib diisi untuk status ini.")
        if not data.get("forwarder_id"):
            raise ValueError("Ekspedisi wajib dipilih dari Master Ekspedisi untuk status ini.")
    return {"status": status, "ship_method": method,
            "tracking_no": _fwd_text(data.get("tracking_no"), "Nomor resi", 80),
            "shipped_date": data.get("shipped_date") or None, "eta_date": data.get("eta_date") or None,
            "transit_location": _fwd_text(data.get("transit_location"), "Lokasi transit", 100),
            "note": _fwd_text(data.get("note"), "Catatan", 500)}


def _pkg_forwarder(con: sqlite3.Connection, forwarder_id, keep: int | None = None) -> tuple[int | None, str | None]:
    if not forwarder_id:
        return None, None
    row = con.execute("SELECT * FROM forwarders WHERE id=?", (int(forwarder_id),)).fetchone()
    if row is None:
        raise ValueError("Ekspedisi tidak ditemukan di Master Ekspedisi.")
    if not row["is_active"] and int(forwarder_id) != keep:
        raise ValueError(f"Ekspedisi {row['name']} sudah dinonaktifkan.")
    return row["id"], row["name"]


def _pkg_lines_input(con: sqlite3.Connection, supplier_po_id: str, lines: list[dict], exclude_package: int | None,
                     received_by_item: dict[int, int] | None = None) -> list[tuple[int, int]]:
    seen: set[int] = set()
    out: list[tuple[int, int]] = []
    for ln in lines or []:
        item_id, qty = int(ln.get("item_id") or 0), int(ln.get("quantity") or 0)
        if qty <= 0:
            continue
        if item_id in seen:
            raise ValueError("Satu barang hanya boleh muncul sekali dalam satu paket.")
        seen.add(item_id)
        free = _spo_pkg_free(con, supplier_po_id, item_id, exclude_package)
        floor = (received_by_item or {}).get(item_id, 0)
        if qty < floor:
            raise ValueError(f"Jumlah paket tidak boleh di bawah yang sudah diterima ({floor}).")
        # `free` already excludes this package's own open remainder, so the package may hold up to free + received.
        if qty - floor > free:
            name = con.execute("SELECT product_name FROM supplier_po_items WHERE id=?", (item_id,)).fetchone()["product_name"]
            raise ValueError(f"Jumlah “{name}” melebihi yang belum masuk paket mana pun ({free}).")
        out.append((item_id, qty))
    return out


def _pkg_history(con: sqlite3.Connection, pkg_id: int, supplier_po_id: str, info: dict, fwd_name: str | None,
                 by: str | None, notify: bool = False) -> None:
    con.execute(
        """INSERT INTO spo_package_history (package_id, supplier_po_id, at, by_user, status, ship_method, forwarder_name,
                                            tracking_no, eta_date, transit_location, note, notify_customer)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pkg_id, supplier_po_id, _now(), by, info["status"], info["ship_method"], fwd_name, info["tracking_no"],
         info["eta_date"], info["transit_location"], info["note"], 1 if notify else 0))


def create_spo_package(supplier_po_id: str, data: dict, by: str | None = None) -> dict:
    info = _pkg_validate_info(data)
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if po is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if po["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Paket pengiriman hanya untuk PO yang sudah diterbitkan dan belum selesai.")
        lines = _pkg_lines_input(con, supplier_po_id, data.get("lines"), None)
        if not lines:
            raise ValueError("Isi minimal satu barang beserta jumlahnya di paket ini.")
        fid, fname = _pkg_forwarder(con, data.get("forwarder_id"))
        n = con.execute("SELECT COUNT(*) AS n FROM spo_packages WHERE supplier_po_id=?", (supplier_po_id,)).fetchone()["n"] + 1
        package_no = f"{po['po_number']}-P{n}"
        now = _now()
        cur = con.execute(
            """INSERT INTO spo_packages (supplier_po_id, package_no, forwarder_id, forwarder_name, ship_method, tracking_no,
                                         shipped_date, eta_date, transit_location, status, note, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_po_id, package_no, fid, fname, info["ship_method"], info["tracking_no"], info["shipped_date"],
             info["eta_date"], info["transit_location"], info["status"], info["note"], by, now, now))
        pkg_id = cur.lastrowid
        con.executemany("INSERT INTO spo_package_lines (package_id, item_id, quantity) VALUES (?,?,?)",
                        [(pkg_id, i, q) for i, q in lines])
        _pkg_history(con, pkg_id, supplier_po_id, info, fname, by, bool(data.get("notify_customer")))
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "package", f"Paket {package_no} dibuat ({sum(q for _, q in lines)} pcs)"
                   + (f" lewat {fname}" if fname else "") + f", status {_SHIP_STATUS_LABELS[info['status']]}", by)
        _spo_sync_all(con, supplier_po_id, by, now)
    return get_supplier_po(supplier_po_id)


def update_spo_package(supplier_po_id: str, package_id: int, data: dict, by: str | None = None) -> dict:
    """Changes a package's shipping info/status and, when `lines` is given, what it holds. Every change is
    kept in the package history. Lines may not drop below what already arrived through the package."""
    info = _pkg_validate_info(data)
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if po is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if po["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Paket pengiriman hanya bisa diubah pada PO yang sudah diterbitkan dan belum selesai.")
        pkg = next((p for p in _spo_packages(con, supplier_po_id) if p["id"] == package_id), None)
        if pkg is None:
            raise LookupError("Paket tidak ditemukan di PO ini.")
        if pkg["cancelled"]:
            raise ValueError("Paket ini sudah dibatalkan.")
        if pkg["remaining_qty"] <= 0:
            raise ValueError("Paket ini sudah diterima lengkap.")
        fid, fname = _pkg_forwarder(con, data.get("forwarder_id"), keep=pkg["forwarder_id"])
        now = _now()
        con.execute(
            """UPDATE spo_packages SET forwarder_id=?, forwarder_name=?, ship_method=?, tracking_no=?, shipped_date=?, eta_date=?,
                      transit_location=?, status=?, note=?, updated_at=? WHERE id=?""",
            (fid, fname, info["ship_method"], info["tracking_no"], info["shipped_date"], info["eta_date"],
             info["transit_location"], info["status"], info["note"], now, package_id))
        if data.get("lines") is not None:
            got = {l["item_id"]: l["received"] for l in pkg["lines"]}
            new = _pkg_lines_input(con, supplier_po_id, data["lines"], package_id, got)
            # A line that already had goods arrive stays (at least at what arrived) even if the form dropped it.
            keep = {i for i, _ in new}
            new += [(i, g) for i, g in got.items() if g > 0 and i not in keep]
            if not any(q - got.get(i, 0) > 0 for i, q in new) and not any(got.values()):
                raise ValueError("Paket tidak boleh kosong. Batalkan paketnya bila memang tidak jadi dikirim.")
            con.execute("DELETE FROM spo_package_lines WHERE package_id=?", (package_id,))
            con.executemany("INSERT INTO spo_package_lines (package_id, item_id, quantity) VALUES (?,?,?)",
                            [(package_id, i, q) for i, q in new])
        _pkg_history(con, package_id, supplier_po_id, info, fname, by, bool(data.get("notify_customer")))
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "package", f"Paket {pkg['package_no']}: {_SHIP_STATUS_LABELS[info['status']]}"
                   + (f" ({info['ship_method']})" if info["ship_method"] else "") + (f" — {info['note']}" if info["note"] else ""), by)
        _spo_sync_all(con, supplier_po_id, by, now)
    return get_supplier_po(supplier_po_id)


def cancel_spo_package(supplier_po_id: str, package_id: int, reason: str, by: str | None = None) -> dict:
    """Cancels a package that carried nothing yet; its quantity becomes free to put in another package.
    Goods that already arrived through it are not undone (they are on their Barang Masuk)."""
    reason = _fwd_text(reason, "Alasan", 300, True)
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        pkg = next((p for p in _spo_packages(con, supplier_po_id) if p["id"] == package_id), None)
        if pkg is None:
            raise LookupError("Paket tidak ditemukan di PO ini.")
        if pkg["cancelled"]:
            raise ValueError("Paket ini sudah dibatalkan.")
        if pkg["received_qty"] > 0:
            raise ValueError("Paket ini sudah ada barangnya yang diterima, tidak bisa dibatalkan. Kecilkan jumlah di paket untuk menutup sisanya.")
        now = _now()
        con.execute("UPDATE spo_packages SET cancelled=1, cancel_reason=?, updated_at=? WHERE id=?", (reason, now, package_id))
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "package", f"Paket {pkg['package_no']} dibatalkan — {reason}", by)
    return get_supplier_po(supplier_po_id)



# ---------------------------------------------------------------------------
# Rencana Pengiriman (owner, 22-10-2026): the route of every unit is set in one table per PO (rows = PO lines,
# columns = Udara / Laut / ...). Each route is a package: saving the plan grows or shrinks the open package of
# that route, or makes a new one, so the shipping info (forwarder, resi, ETA, status) lives only on packages.
# ---------------------------------------------------------------------------
SPO_PLAN_METHOD_LABELS = {"udara": "Udara", "laut": "Laut", "darat": "Darat", "ambil_sendiri": "Ambil Sendiri", "lainnya": "Lainnya"}


def _spo_lane_target(pkgs: list[dict], method: str) -> dict | None:
    """The package of a route that the plan may still change: the newest one of that route that is not on the
    road yet and not fully received. Packages already moving keep what they carry."""
    for p in reversed(pkgs):
        if p["cancelled"] or p["ship_method"] != method or p["status"] in SPO_PKG_MOVING:
            continue
        if p["lines"] and p["remaining_qty"] <= 0:
            continue
        return p
    return None


def _spo_shipping_plan(po: dict, items: list[dict], pkgs: list[dict]) -> dict:
    """Per PO line: units per route (sum of that route's package lines), the least each route may be set to (units in
    packages the plan cannot change, plus what already arrived in the changeable one), units in a package with no
    route, and what is not on any route yet."""
    live = [p for p in pkgs if not p["cancelled"]]
    targets = {m: _spo_lane_target(live, m) for m in SPO_SHIP_METHODS}
    rows = []
    for it in items:
        ordered = it["quantity"] - it["qty_cancelled"]
        if ordered <= 0:
            continue
        lanes: dict[str, dict] = {}
        other = got_in_pkg = 0
        for p in live:
            for l in p["lines"]:
                if l["item_id"] != it["id"]:
                    continue
                got_in_pkg += l["received"]
                if not p["ship_method"]:
                    other += l["quantity"]
                    continue
                e = lanes.setdefault(p["ship_method"], {"qty": 0, "floor": 0, "packages": []})
                e["qty"] += l["quantity"]
                t = targets.get(p["ship_method"])
                e["floor"] += l["received"] if t and t["id"] == p["id"] else l["quantity"]
                e["packages"].append({"id": p["id"], "no": p["package_no"].rsplit("-", 1)[-1], "qty": l["quantity"],
                                      "received": l["received"], "state": p["state"], "status": p["status"]})
        outside = max(it["qty_received"] - got_in_pkg, 0)   # arrived on a Barang Masuk that named no package
        capacity = max(ordered - outside, 0)
        planned = sum(e["qty"] for e in lanes.values()) + other
        rows.append({"item_id": it["id"], "product_name": it["product_name"], "sku": it.get("sku"), "unit": it.get("unit"),
                     "customer_po_number": it.get("customer_po_number"), "ordered": ordered, "received": it["qty_received"],
                     "received_outside": outside, "capacity": capacity, "lanes": lanes, "other": other,
                     "unassigned": capacity - planned})
    used = [m for m in SPO_SHIP_METHODS if any(m in r["lanes"] for r in rows)]
    return {"rows": rows, "methods": SPO_PLAN_METHOD_LABELS, "used_methods": used,
            "editable": po["status"] in ("dipesan", "sebagian_diterima")}


def save_spo_shipping_plan(supplier_po_id: str, data: dict, by: str | None = None,
                           source: str = "Rencana Pengiriman") -> dict:
    """Simpan Rencana Pengiriman: data = {"items": [{"item_id", "lanes": {route: qty}}]}. A route left out of a
    row keeps what it has. All or nothing; stock never moves here. `source` names where the plan came from
    in the package note and the timeline."""
    rows_in = data.get("items") or []
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if po is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        po = dict(po)
        if po["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Rencana pengiriman diatur setelah PO diterbitkan dan sebelum selesai diterima.")
        pkgs = _spo_packages(con, supplier_po_id)
        plan = _spo_shipping_plan(po, _spo_items(con, supplier_po_id), pkgs)
        by_item = {r["item_id"]: r for r in plan["rows"]}
        deltas: dict[str, dict[int, int]] = {}
        summary = []
        for d in rows_in:
            r = by_item.get(int(d.get("item_id") or 0))
            if r is None:
                raise ValueError("Barang tidak ada di PO ini atau sudah dibatalkan.")
            wants = {}
            for m, q in (d.get("lanes") or {}).items():
                if m not in SPO_SHIP_METHODS:
                    raise ValueError(f"Jalur pengiriman tidak dikenal: {m}.")
                try:
                    q = int(q or 0)
                except (TypeError, ValueError):
                    raise ValueError("Jumlah harus angka.")
                if q < 0:
                    raise ValueError("Jumlah tidak boleh minus.")
                wants[m] = q
            final = {m: e["qty"] for m, e in r["lanes"].items()}
            for m, q in wants.items():
                floor = r["lanes"].get(m, {}).get("floor", 0)
                if q < floor:
                    raise ValueError(f"“{r['product_name']}”: jalur {SPO_PLAN_METHOD_LABELS[m]} minimal {floor} "
                                     "(sudah dalam perjalanan / sudah diterima).")
                final[m] = q
            total = sum(final.values()) + r["other"]
            if total > r["capacity"]:
                raise ValueError(f"“{r['product_name']}”: total rencana {total} melebihi jumlah yang dipesan ({r['capacity']}).")
            for m, q in final.items():
                delta = q - r["lanes"].get(m, {}).get("qty", 0)
                if delta:
                    deltas.setdefault(m, {})[r["item_id"]] = delta
            if any(deltas.get(m, {}).get(r["item_id"]) for m in final):
                summary.append(f"{r['product_name']}: " + ", ".join(f"{SPO_PLAN_METHOD_LABELS[m]} {q}" for m, q in final.items() if q)
                               + (f" (belum diatur {r['capacity'] - total})" if r["capacity"] - total else ""))
        if not deltas:
            return get_supplier_po(supplier_po_id)
        now = _now()
        live = [p for p in pkgs if not p["cancelled"]]
        n = con.execute("SELECT COUNT(*) AS n FROM spo_packages WHERE supplier_po_id=?", (supplier_po_id,)).fetchone()["n"]
        notes = []
        for m in SPO_SHIP_METHODS:
            dm = deltas.get(m)
            if not dm:
                continue
            target = _spo_lane_target(live, m)
            if target is None:
                lines = [(i, q) for i, q in dm.items() if q > 0]
                if not lines:
                    continue
                n += 1
                package_no = f"{po['po_number']}-P{n}"
                pid = con.execute(
                    """INSERT INTO spo_packages (supplier_po_id, package_no, ship_method, status, note, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (supplier_po_id, package_no, m, "menunggu_konfirmasi", f"Dari {source}", by, now, now)).lastrowid
                con.executemany("INSERT INTO spo_package_lines (package_id, item_id, quantity) VALUES (?,?,?)",
                                [(pid, i, q) for i, q in lines])
                _pkg_history(con, pid, supplier_po_id, {"status": "menunggu_konfirmasi", "ship_method": m, "tracking_no": None,
                                                        "eta_date": None, "transit_location": None, "note": f"Dari {source}"}, None, by)
                notes.append(f"paket {package_no.rsplit('-', 1)[-1]} {SPO_PLAN_METHOD_LABELS[m]} dibuat ({sum(q for _, q in lines)} pcs)")
                continue
            qty = {l["item_id"]: l["quantity"] for l in target["lines"]}
            for i, dq in dm.items():
                qty[i] = qty.get(i, 0) + dq
            keep = [(i, q) for i, q in qty.items() if q > 0]
            short = target["package_no"].rsplit("-", 1)[-1]
            if not keep:
                con.execute("UPDATE spo_packages SET cancelled=1, cancel_reason=?, updated_at=? WHERE id=?",
                            ("Kosong setelah Rencana Pengiriman diubah", now, target["id"]))
                notes.append(f"paket {short} {SPO_PLAN_METHOD_LABELS[m]} dibatalkan (kosong)")
                continue
            con.execute("DELETE FROM spo_package_lines WHERE package_id=?", (target["id"],))
            con.executemany("INSERT INTO spo_package_lines (package_id, item_id, quantity) VALUES (?,?,?)",
                            [(target["id"], i, q) for i, q in keep])
            con.execute("UPDATE spo_packages SET updated_at=? WHERE id=?", (now, target["id"]))
            _pkg_history(con, target["id"], supplier_po_id, {"status": target["status"], "ship_method": m, "tracking_no": target["tracking_no"],
                                                             "eta_date": target["eta_date"], "transit_location": target["transit_location"],
                                                             "note": f"Isi paket diubah lewat {source}"}, target["forwarder_name"], by)
            notes.append(f"paket {short} {SPO_PLAN_METHOD_LABELS[m]} jadi {sum(q for _, q in keep)} pcs")
        con.execute("UPDATE supplier_pos SET updated_at=? WHERE id=?", (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "package", ("Rencana pengiriman" if source == "Rencana Pengiriman" else source) + ": " + "; ".join(summary) + (" — " + ", ".join(notes) if notes else ""), by)
        _spo_sync_all(con, supplier_po_id, by, now)
    return get_supplier_po(supplier_po_id)


def apply_spo_so_routes(supplier_po_id: str, by: str | None = None) -> dict:
    """Owner 24-10-2026: the route picked on the Sales Order line (Jalur Kirim) carries over to the PO. Units not in
    any package yet, of lines whose SO line has a route, go to that route's package (made or grown like a saved plan).
    Runs when the PO is issued and from "Ikuti Jalur SO"; lines with no SO route stay for + Paket."""
    with _conn() as con:
        po = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if po is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if po["status"] not in ("dipesan", "sebagian_diterima"):
            return get_supplier_po(supplier_po_id)
        items = _spo_items(con, supplier_po_id)
        plan = _spo_shipping_plan(dict(po), items, _spo_packages(con, supplier_po_id))
    route_of = {i["id"]: i.get("so_ship_route") for i in items}
    rows_in = []
    for r in plan["rows"]:
        m = route_of.get(r["item_id"])
        if m in SPO_SHIP_METHODS and r["unassigned"] > 0:
            rows_in.append({"item_id": r["item_id"], "lanes": {m: r["lanes"].get(m, {}).get("qty", 0) + r["unassigned"]}})
    if not rows_in:
        return get_supplier_po(supplier_po_id)
    return save_spo_shipping_plan(supplier_po_id, {"items": rows_in}, by, source="Jalur Sales Order")


def _spo_finish_if_settled(con: sqlite3.Connection, supplier_po_id: str) -> None:
    """Moves a PO that has nothing left to receive to its end state: 'diterima' if anything
    arrived, 'batal' if every line was cancelled. Otherwise sebagian_diterima once something arrived."""
    agg = con.execute(
        """SELECT SUM(quantity - qty_cancelled - qty_received) AS left_qty, SUM(qty_received) AS got
           FROM supplier_po_items WHERE supplier_po_id=?""", (supplier_po_id,)).fetchone()
    left_qty, got = agg["left_qty"] or 0, agg["got"] or 0
    if left_qty <= 0:
        new = "diterima" if got > 0 else "batal"
    else:
        new = "sebagian_diterima" if got > 0 else "dipesan"
    con.execute("UPDATE supplier_pos SET status=?, updated_at=? WHERE id=?", (new, _now(), supplier_po_id))


def save_spo_receive_draft(supplier_po_id: str, payload: dict, by: str | None = None) -> dict:
    """Simpan Draft penerimaan: the half-filled receiving form, kept until it is posted. No stock moves."""
    with _conn() as con:
        row = con.execute("SELECT status FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Draft penerimaan hanya untuk Purchase Order yang sudah diterbitkan dan belum selesai.")
        con.execute(
            "INSERT INTO spo_receive_drafts (supplier_po_id, payload, saved_by, saved_at) VALUES (?,?,?,?) "
            "ON CONFLICT(supplier_po_id) DO UPDATE SET payload=excluded.payload, saved_by=excluded.saved_by, saved_at=excluded.saved_at",
            (supplier_po_id, json.dumps(payload), by, _now()),
        )
        _spo_event(con, supplier_po_id, "receive_draft", "Draft penerimaan disimpan", by)
    return get_supplier_po(supplier_po_id)


def delete_spo_receive_draft(supplier_po_id: str) -> dict:
    with _conn() as con:
        if con.execute("SELECT 1 FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone() is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        con.execute("DELETE FROM spo_receive_drafts WHERE supplier_po_id=?", (supplier_po_id,))
    return get_supplier_po(supplier_po_id)


def receive_supplier_po(
    supplier_po_id: str, receipts: list[dict], changed_by: str | None = None, received_date: str | None = None,
    surat_jalan_no: str | None = None, warehouse_location: str | None = None, note: str | None = None,
    create_inbound: bool = False, officer: str | None = None,
    accept_over: bool = False, over_reason: str | None = None, is_admin: bool = False,
) -> dict:
    """Penerimaan Barang. `receipts` is [{item_id, quantity}]: how many of each line arrived now.
    With create_inbound, a DRAFT Barang Masuk document is opened for the lines linked to a master
    item (stock still moves only when that document is posted)."""
    over_ok = _over_permission(accept_over, over_reason, is_admin)
    if over_ok:
        create_inbound = True  # K-11: the excess becomes stock only through a Barang Masuk document
    over_list: list[tuple[dict, int]] = []
    now = _now()
    received_date = received_date or shop_today().isoformat()
    try:
        date.fromisoformat(received_date)
    except ValueError:
        raise ValueError("Tanggal terima harus berformat YYYY-MM-DD.")
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Barang hanya bisa diterima untuk Purchase Order yang sudah diterbitkan dan belum selesai.")
        arrived = [r for r in receipts if r["quantity"] > 0]
        if not arrived:
            raise ValueError("Isi jumlah barang yang diterima.")
        con.execute("DELETE FROM spo_receive_drafts WHERE supplier_po_id=?", (supplier_po_id,))
        for r in arrived:
            if r.get("condition") and r["condition"] not in SPO_ITEM_CONDITIONS:
                raise ValueError(f"Kondisi barang harus salah satu dari: {', '.join(SPO_ITEM_CONDITIONS)}.")
        gr_number = _next_gr_number(con, received_date.replace("-", ""))
        cur = con.execute(
            """INSERT INTO spo_receipts (supplier_po_id, received_date, surat_jalan_no, warehouse_location, note,
                                         created_by, created_at, gr_number, officer) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (supplier_po_id, received_date, surat_jalan_no, warehouse_location, note, changed_by, now, gr_number,
             officer or changed_by),
        )
        receipt_id = cur.lastrowid
        got_lines = []
        for r in arrived:
            item = con.execute(
                "SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?", (r["item_id"], supplier_po_id)
            ).fetchone()
            if item is None:
                raise ValueError(f"Baris {r['item_id']} tidak ada di PO ini.")
            left = item["quantity"] - item["qty_cancelled"] - item["qty_received"]
            counted = r["quantity"]
            if r["quantity"] > left:
                if not over_ok:
                    raise ValueError(f"Jumlah diterima untuk “{item['product_name']}” melebihi sisa ({left}). "
                                     "Admin dapat menerima kelebihannya sebagai stok umum.")
                counted = max(left, 0)  # K-11: the PO counts what it expected; the rest is general stock
                over_list.append((dict(item), r["quantity"] - counted))
            cond = r.get("condition") or "baik"
            bad = cond in BAD_RECEIVE_CONDITIONS
            pkg_id = _spo_check_receive_package(con, supplier_po_id, r["item_id"], r.get("package_id"), counted)
            con.execute("UPDATE supplier_po_items SET qty_received = qty_received + ?, qty_bad = qty_bad + ? WHERE id=?",
                        (counted, counted if bad else 0, r["item_id"]))
            con.execute("INSERT INTO spo_receipt_lines (receipt_id, item_id, quantity, condition, location, package_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (receipt_id, r["item_id"], counted, cond, r.get("location") or warehouse_location, pkg_id))
            if bad:
                # Decision 3: damaged / wrong goods never reach the sellable shelf; they wait in quarantine
                # as a candidate for a return to this supplier.
                gross = item["est_unit_price"]
                _quarantine_in(
                    con, master_item_id=item["master_item_id"], product_name=item["product_name"], qty=r["quantity"],
                    condition=cond, source_type="terima_barang", source_ref=f"{gr_number} / {row['po_number']}",
                    source_id=str(receipt_id), supplier_id=row["supplier_id"],
                    unit_cost=gross - round(gross * (item["discount_pct"] or 0) / 100), candidate=True,
                    note=r.get("note") or note, by=changed_by)
            got_lines.append((dict(item), r["quantity"], bad))
        _spo_finish_if_settled(con, supplier_po_id)
        if over_list:
            _spo_event(con, supplier_po_id, "over",
                       "Kelebihan barang diterima sebagai stok umum: " + ", ".join(f"{n} pcs “{it['product_name']}”" for it, n in over_list)
                       + f". Izin admin: {over_ok}", changed_by)
        total_got = sum(q for _, q, _ in got_lines)
        total_bad = sum(q for _, q, b in got_lines if b)
        _spo_event(con, supplier_po_id, "receive",
                   f"Barang diterima {gr_number} ({total_got} pcs)" + (f", surat jalan {surat_jalan_no}" if surat_jalan_no else "")
                   + (f"; {total_bad} pcs rusak/tidak sesuai masuk Stok Karantina" if total_bad else ""),
                   changed_by)
        if create_inbound:
            by_master: dict[int, dict] = {}
            for item, qty, bad in got_lines:
                if item["master_item_id"] is None or bad:
                    continue
                gross = item["est_unit_price"]
                cost = gross - round(gross * (item["discount_pct"] or 0) / 100)
                slot = by_master.setdefault(item["master_item_id"], {"master_item_id": item["master_item_id"], "qty_in": 0, "cost_price": cost})
                slot["qty_in"] += qty
            if by_master:
                doc_number = _next_doc_number(con, date.today().strftime("%Y%m%d"))
                notes_txt = f"Dari Purchase Order {row['po_number']}" + (f", surat jalan {surat_jalan_no}" if surat_jalan_no else "")
                doc = con.execute(
                    """INSERT INTO inbound_documents
                       (doc_number, supplier_id, received_date, supplier_invoice_no, notes, status, total_value, created_at,
                        supplier_po_id, source, surat_jalan_no, created_by)
                       VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, 'po_supplier', ?, ?)""",
                    (doc_number, row["supplier_id"], received_date, row["supplier_invoice_no"], notes_txt,
                     sum(v["qty_in"] * v["cost_price"] for v in by_master.values()), now,
                     supplier_po_id, surat_jalan_no, changed_by),
                )
                _insert_inbound_items(con, doc.lastrowid, list(by_master.values()))
                con.execute("UPDATE spo_receipts SET inbound_document_id=? WHERE id=?", (doc.lastrowid, receipt_id))
                _spo_event(con, supplier_po_id, "inbound", f"Draft Barang Masuk {doc_number} dibuat", changed_by)
        _spo_sync_all(con, supplier_po_id, changed_by, now)
    return get_supplier_po(supplier_po_id)


def _spo_add_payment(
    con: sqlite3.Connection, supplier_po_id: str, amount: int, method: str, note: str | None, by: str | None,
    paid_date: str | None = None, reference_no: str | None = None, bank_account: str | None = None,
) -> None:
    row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
    if row is None:
        raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
    if row["status"] in ("draft", "batal"):
        raise ValueError("Pembayaran hanya untuk PO yang sudah diterbitkan.")
    if amount <= 0:
        raise ValueError("Jumlah pembayaran harus lebih dari 0.")
    if method not in SPO_PAY_METHODS:
        raise ValueError(f"Metode bayar harus salah satu dari: {', '.join(SPO_PAY_METHODS)}.")
    po = dict(row)
    faktur = _active_po_faktur(con, supplier_po_id)
    if faktur and faktur["status"] == "posted":
        raise ValueError(f"PO ini sudah punya Faktur Pembelian {faktur['invoice_number']}. Catat pembayaran di faktur itu.")
    money = _spo_money(po, _spo_items(con, supplier_po_id))
    dp_room = money["grand_total"] - (po["paid_amount"] or 0)
    if amount > dp_room:
        raise ValueError(f"Uang muka melebihi nilai PO yang belum dibayar (Rp {max(dp_room, 0):,}).".replace(",", "."))
    paid_at = _now()
    if paid_date:
        try:
            date.fromisoformat(paid_date)
        except ValueError:
            raise ValueError("Tanggal pembayaran harus berformat YYYY-MM-DD.")
        paid_at = f"{paid_date}T{_now()[11:]}"
    con.execute(
        """INSERT INTO spo_payments (supplier_po_id, paid_at, amount, method, note, created_by, reference_no, bank_account)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (supplier_po_id, paid_at, amount, method, note, by, reference_no, bank_account),
    )
    con.execute("UPDATE supplier_pos SET paid_amount = paid_amount + ?, updated_at=? WHERE id=?",
                (amount, _now(), supplier_po_id))
    _spo_event(con, supplier_po_id, "payment", f"Uang muka (DP) supplier Rp {amount:,}".replace(",", "."), by)


def add_supplier_po_payment(
    supplier_po_id: str, amount: int, method: str = "transfer", note: str | None = None, by: str | None = None,
    paid_date: str | None = None, reference_no: str | None = None, bank_account: str | None = None,
) -> dict:
    if method == "transfer" and not (reference_no or "").strip():
        raise ValueError("Nomor referensi transfer wajib diisi.")
    with _conn() as con:
        _spo_add_payment(con, supplier_po_id, amount, method, note, by, paid_date, reference_no, bank_account)
    return get_supplier_po(supplier_po_id)


def cancel_supplier_po_item(
    supplier_po_id: str, item_id: int, quantity: int, reason: str, action: str,
    note: str | None = None, by: str | None = None,
) -> dict:
    """Batalkan Item Purchase Order: drops `quantity` of a line that is still to come. The quantity goes
    back to the customer line it served (it can be ordered again), the bill shrinks, and the cancellation
    is logged with its reason and what to do next."""
    if not reason or not reason.strip():
        raise ValueError("Alasan pembatalan wajib diisi.")
    if action not in SPO_CANCEL_ACTIONS:
        raise ValueError(f"Tindakan lanjutan harus salah satu dari: {', '.join(SPO_CANCEL_ACTIONS)}.")
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Item hanya bisa dibatalkan pada PO yang sudah diterbitkan dan belum selesai.")
        item = con.execute(
            "SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?", (item_id, supplier_po_id)
        ).fetchone()
        if item is None:
            raise ValueError(f"Baris {item_id} tidak ada di PO ini.")
        pending = con.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM spo_cancellations WHERE item_id=? AND status='menunggu_persetujuan'",
            (item_id,),
        ).fetchone()["q"]
        left = item["quantity"] - item["qty_cancelled"] - item["qty_received"] - pending
        if quantity <= 0:
            raise ValueError("Jumlah yang dibatalkan harus lebih dari 0.")
        if quantity > left:
            raise ValueError(
                f"Jumlah dibatalkan melebihi sisa yang belum diterima ({left})"
                + (f", {pending} lagi sedang menunggu persetujuan supplier." if pending else ".")
            )
        # The supplier has not agreed yet, so nothing changes on the PO: the request is recorded and
        # the other lines (and other customers' allocations) stay exactly as they were.
        con.execute(
            """INSERT INTO spo_cancellations (supplier_po_id, item_id, product_name, quantity, reason, action, note,
                                              cancelled_by, cancelled_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'menunggu_persetujuan')""",
            (supplier_po_id, item_id, item["product_name"], quantity, reason.strip(), action,
             (note or "").strip() or None, by, now),
        )
        _spo_event(con, supplier_po_id, "cancel_request",
                   f"Pembatalan diajukan, menunggu persetujuan supplier: {item['product_name']} x{quantity} — {reason.strip()}", by)
    return get_supplier_po(supplier_po_id)


def release_spo_line_to_stock(supplier_po_id: str, item_id: int, reason: str, by: str | None = None) -> dict:
    """K-14, case 'Tetap beli': the customer cancelled the Sales Order line this Purchase Order line was bought for,
    and the admin decides to keep the order with the supplier. The line is cut loose from the cancelled customer
    line, so what arrives is plain shop stock (it advances no Sales Order). The decision is logged with its reason."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan wajib diisi (minimal 3 karakter).")
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Hanya untuk PO yang sudah diterbitkan dan belum selesai.")
        item = con.execute("SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?", (item_id, supplier_po_id)).fetchone()
        if item is None:
            raise ValueError(f"Baris {item_id} tidak ada di PO ini.")
        line = con.execute("SELECT pi.status, p.po_number FROM preorder_items pi JOIN preorders p ON p.id = pi.preorder_id WHERE pi.id=?",
                           (item["preorder_item_id"],)).fetchone() if item["preorder_item_id"] else None
        if line is None or line["status"] != PO_LINE_CANCELLED:
            raise ValueError("Baris ini tidak terkait barang Sales Order yang dibatalkan.")
        left = item["quantity"] - item["qty_cancelled"] - item["qty_received"]
        if left <= 0:
            raise ValueError("Tidak ada sisa barang yang menunggu datang di baris ini.")
        con.execute("UPDATE supplier_po_items SET preorder_item_id=NULL WHERE id=?", (item_id,))
        _spo_event(con, supplier_po_id, "so_released",
                   f"{item['product_name']} x{left}: Sales Order {line['po_number']} dibatalkan; PO tetap berjalan dan barang yang datang "
                   f"menjadi stok umum. Keputusan admin: {reason}", by)
    return get_supplier_po(supplier_po_id)


def _resolve_cancellation_tx(con: sqlite3.Connection, supplier_po_id: str, c, approved: bool, note: str | None, by: str | None) -> None:
    now = _now()
    if c["status"] != "menunggu_persetujuan":
        raise ValueError("Permintaan pembatalan ini sudah diputuskan.")
    item = con.execute("SELECT * FROM supplier_po_items WHERE id=?", (c["item_id"],)).fetchone()
    if approved:
        left = item["quantity"] - item["qty_cancelled"] - item["qty_received"]
        if c["quantity"] > left:
            raise ValueError(
                f"Barang sudah datang sebagian: sisa yang belum diterima hanya {left}, tidak bisa dibatalkan {c['quantity']}. "
                "Tolak permintaan ini atau ajukan jumlah yang lebih kecil."
            )
        packed = _spo_pkg_open_qty(con, supplier_po_id, c["item_id"])
        if left - c["quantity"] < packed:
            raise ValueError(f"Sebagian barang ini masih ada di paket pengiriman yang belum datang ({packed}). "
                             "Kecilkan atau batalkan paketnya dulu sebelum membatalkan item.")
        con.execute("UPDATE supplier_po_items SET qty_cancelled = qty_cancelled + ? WHERE id=?", (c["quantity"], c["item_id"]))
    con.execute(
        "UPDATE spo_cancellations SET status=?, resolved_by=?, resolved_at=?, resolution_note=? WHERE id=?",
        ("disetujui" if approved else "ditolak", by, now, (note or "").strip() or None, c["id"]),
    )
    _spo_event(
        con, supplier_po_id, "cancel_item" if approved else "cancel_rejected",
        (f"Pembatalan disetujui supplier: {c['product_name']} x{c['quantity']}" if approved
         else f"Pembatalan ditolak supplier: {c['product_name']} x{c['quantity']} tetap dipesan")
        + (f" — {note.strip()}" if note and note.strip() else ""), by)


REFUND_KINDS = {"refund": "Refund (uang kembali)", "kredit": "Kredit Supplier (potong tagihan berikutnya)"}


def _spo_refund_claimed(con: sqlite3.Connection, supplier_po_id: str) -> int:
    return con.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_refund_claims WHERE supplier_po_id=? AND status<>'dibatalkan'",
                       (supplier_po_id,)).fetchone()["n"]


def _spo_open_refund(con: sqlite3.Connection, supplier_po_id: str) -> int:
    """Money paid on a PO that its (shrunk or cancelled) bill no longer covers and no claim covers yet. Zero while a
    Faktur Pembelian exists: from then on the debt, and what was paid, live on the faktur."""
    po = dict(con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone())
    if _active_po_faktur(con, supplier_po_id):
        return 0
    bill = 0 if po["status"] == "batal" else _spo_money(po, _spo_items(con, supplier_po_id))["grand_total"]
    return max((po["paid_amount"] or 0) - bill - _spo_refund_claimed(con, supplier_po_id), 0)


def _spo_make_refund_claim(con: sqlite3.Connection, supplier_po_id: str, kind: str | None, by: str | None) -> int:
    """Called after a cancellation was approved: if the PO now holds more money than it bills, the surplus becomes a
    Piutang Refund / Kredit Supplier claim. Returns the amount (0 when nothing is owed back)."""
    due = _spo_open_refund(con, supplier_po_id)
    if due <= 0:
        return 0
    if kind not in REFUND_KINDS:
        raise ValueError(f"PO ini sudah dibayar Rp {due:,} melebihi tagihan setelah pembatalan. Pilih apakah uang kembali sebagai Refund atau Kredit Supplier.".replace(",", "."))
    po = con.execute("SELECT supplier_id, po_number FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
    open_claim = con.execute("SELECT * FROM supplier_refund_claims WHERE supplier_po_id=? AND status='menunggu' ORDER BY id DESC LIMIT 1",
                             (supplier_po_id,)).fetchone()
    if open_claim is not None:
        if open_claim["kind"] != kind:
            raise ValueError(f"Klaim yang sudah ada untuk PO ini berupa {REFUND_KINDS[open_claim['kind']]}. Gunakan pilihan yang sama.")
        con.execute("UPDATE supplier_refund_claims SET amount = amount + ? WHERE id=?", (due, open_claim["id"]))
        no = open_claim["claim_no"]
    else:
        no = _sr_number(con, "RFS", "supplier_refund_claims", "claim_no")
        con.execute("INSERT INTO supplier_refund_claims (claim_no, supplier_id, supplier_po_id, kind, amount, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (no, po["supplier_id"], supplier_po_id, kind, due, by, _now()))
    _spo_event(con, supplier_po_id, "refund_claim",
               (f"{'Piutang Refund Supplier' if kind == 'refund' else 'Kredit Supplier'} {no} Rp {due:,}: uang muka yang sudah dibayar melebihi tagihan setelah pembatalan. "
                "Belum dianggap diterima sampai uang benar-benar masuk / nota debit dikonfirmasi.").replace(",", "."), by)
    return due


def resolve_spo_cancellation(
    supplier_po_id: str, cancellation_id: int, approved: bool, note: str | None = None, by: str | None = None,
    refund_kind: str | None = None,
) -> dict:
    """The supplier's answer to a cancellation request. Approved: the quantity is dropped from the line
    (the bill shrinks, the PO may finish) and money already paid beyond the new bill becomes a refund / credit claim.
    Rejected: the request is closed and the line is untouched, the PO carries on."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        c = con.execute(
            "SELECT * FROM spo_cancellations WHERE id=? AND supplier_po_id=?", (cancellation_id, supplier_po_id)
        ).fetchone()
        if c is None:
            raise LookupError("Permintaan pembatalan tidak ditemukan.")
        _resolve_cancellation_tx(con, supplier_po_id, c, approved, note, by)
        if approved:
            _spo_finish_if_settled(con, supplier_po_id)
            _spo_make_refund_claim(con, supplier_po_id, refund_kind, by)
    return get_supplier_po(supplier_po_id)


def request_spo_cancel_all(supplier_po_id: str, reason: str, action: str, note: str | None = None, by: str | None = None) -> dict:
    """Batalkan PO: asks the supplier to cancel everything of the PO that has not arrived. Nothing changes on the PO until the
    supplier agrees (resolve_spo_cancel_all); goods already received stay and are handled by Retur Supplier."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("dipesan", "sebagian_diterima"):
            raise ValueError("Pembatalan ke supplier hanya untuk PO yang sudah diterbitkan dan belum selesai.")
        if not (reason or "").strip():
            raise ValueError("Alasan pembatalan wajib diisi.")
        if action not in SPO_CANCEL_ACTIONS:
            raise ValueError(f"Tindakan lanjutan harus salah satu dari: {', '.join(SPO_CANCEL_ACTIONS)}.")
        made = 0
        for item in con.execute("SELECT * FROM supplier_po_items WHERE supplier_po_id=?", (supplier_po_id,)).fetchall():
            pending = con.execute("SELECT COALESCE(SUM(quantity), 0) AS q FROM spo_cancellations WHERE item_id=? AND status='menunggu_persetujuan'",
                                  (item["id"],)).fetchone()["q"]
            left = item["quantity"] - item["qty_cancelled"] - item["qty_received"] - pending
            if left > 0:
                con.execute(
                    """INSERT INTO spo_cancellations (supplier_po_id, item_id, product_name, quantity, reason, action, note,
                                                      cancelled_by, cancelled_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'menunggu_persetujuan')""",
                    (supplier_po_id, item["id"], item["product_name"], left, reason.strip(), action, (note or "").strip() or None, by, _now()))
                made += 1
        if not made:
            raise ValueError("Tidak ada sisa barang yang bisa dibatalkan (sudah diterima semua atau sudah diajukan).")
        _spo_event(con, supplier_po_id, "cancel_request",
                   f"Pembatalan seluruh sisa PO diajukan ({made} barang), menunggu persetujuan supplier — {reason.strip()}", by)
    return get_supplier_po(supplier_po_id)


def resolve_spo_cancel_all(supplier_po_id: str, approved: bool, note: str | None = None, by: str | None = None,
                           refund_kind: str | None = None) -> dict:
    """The supplier's answer to every pending cancellation request of a PO at once. Agreed: the PO is cancelled for what
    has not arrived (and any money paid beyond what remains becomes a claim). Refused: the PO simply continues."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        pend = con.execute("SELECT * FROM spo_cancellations WHERE supplier_po_id=? AND status='menunggu_persetujuan'", (supplier_po_id,)).fetchall()
        if not pend:
            raise LookupError("Tidak ada permintaan pembatalan yang menunggu supplier.")
        for c in pend:
            _resolve_cancellation_tx(con, supplier_po_id, c, approved, note, by)
        if approved:
            _spo_finish_if_settled(con, supplier_po_id)
            if con.execute("SELECT status FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()["status"] == "batal":
                con.execute("UPDATE spo_packages SET cancelled=1, cancel_reason='PO dibatalkan', updated_at=? WHERE supplier_po_id=? AND cancelled=0",
                            (_now(), supplier_po_id))
            _spo_make_refund_claim(con, supplier_po_id, refund_kind, by)
    return get_supplier_po(supplier_po_id)


def list_refund_claims(supplier_po_id: str | None = None, status: str | None = None) -> list[dict]:
    with _conn() as con:
        where, params = [], []
        if supplier_po_id:
            where.append("c.supplier_po_id=?"); params.append(supplier_po_id)
        if status:
            where.append("c.status=?"); params.append(status)
        rows = [dict(r) for r in con.execute(
            f"""SELECT c.*, s.name AS supplier_name, p.po_number FROM supplier_refund_claims c
                JOIN suppliers s ON s.id = c.supplier_id JOIN supplier_pos p ON p.id = c.supplier_po_id
                {('WHERE ' + ' AND '.join(where)) if where else ''} ORDER BY c.id DESC""", params)]
        for r in rows:
            r["left"] = r["amount"] - r["settled_amount"] if r["status"] == "menunggu" else 0
            r["credit_notes"] = []
            for cn in con.execute("SELECT * FROM supplier_credit_notes WHERE claim_id=? ORDER BY id", (r["id"],)):
                cn = dict(cn)
                cn["allocated"] = _sr_cn_allocated(con, cn["id"])
                cn["unallocated"] = cn["amount"] - cn["allocated"] if cn["kind"] == "kredit" and cn["status"] == "aktif" else 0
                r["credit_notes"].append(cn)
        return rows


def settle_refund_claim(claim_id: int, data: dict, user: dict) -> dict:
    """Admin: the refund money really arrived (kind refund: amount, method, reference) or the supplier's debit note is
    confirmed (kind kredit: the supplier's debit note number). Only then does the claim count."""
    _sr_require_admin(user, "mencatat refund / kredit supplier")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cl = con.execute("SELECT * FROM supplier_refund_claims WHERE id=?", (claim_id,)).fetchone()
        if cl is None:
            raise LookupError("Klaim tidak ditemukan.")
        if cl["status"] != "menunggu":
            raise ValueError("Klaim ini sudah selesai atau dibatalkan.")
        left = cl["amount"] - cl["settled_amount"]
        amount = int(data.get("amount") or 0)
        if amount <= 0 or amount > left:
            raise ValueError(f"Nilai harus antara 1 dan sisa klaim (Rp {left:,}).".replace(",", "."))
        issued = _sr_text(data.get("received_at") or data.get("issued_at")) or shop_today().isoformat()
        try:
            date.fromisoformat(issued[:10])
        except ValueError:
            raise ValueError("Tanggal harus berformat YYYY-MM-DD.")
        if cl["kind"] == "refund":
            if data.get("method") not in ("transfer", "cash", "giro"):
                raise ValueError("Pilih metode uang masuk (transfer / cash / giro).")
            if not _sr_text(data.get("reference_no")):
                raise ValueError("Isi nomor referensi / bukti uang masuk.")
        elif not _sr_text(data.get("supplier_cn_no")):
            raise ValueError("Isi nomor nota debit dari supplier.")
        con.execute(
            """INSERT INTO supplier_credit_notes (cn_number, kind, supplier_id, return_id, amount, supplier_cn_no, issued_at, method,
                   reference_no, note, created_by, created_at, supplier_po_id, claim_id, subtotal, posted_by, posted_at, source, updated_at)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pembatalan_po', ?)""",
            (_sr_number(con, "DN-SUP", "supplier_credit_notes", "cn_number") if cl["kind"] == "kredit" else None, cl["kind"], cl["supplier_id"],
             amount, _sr_text(data.get("supplier_cn_no")), issued, data.get("method") if cl["kind"] == "refund" else None,
             _sr_text(data.get("reference_no")), _sr_text(data.get("note")), by, _now(), cl["supplier_po_id"], claim_id, amount, by, _now(), _now()))
        settled = cl["settled_amount"] + amount
        done = settled >= cl["amount"]
        con.execute("UPDATE supplier_refund_claims SET settled_amount=?, status=?, closed_by=?, closed_at=? WHERE id=?",
                    (settled, ("diterima" if cl["kind"] == "refund" else "dikonfirmasi") if done else "menunggu",
                     by if done else None, _now() if done else None, claim_id))
        _spo_event(con, cl["supplier_po_id"], "refund_claim",
                   (f"Refund diterima Rp {amount:,} ({data.get('method')}, ref {_sr_text(data.get('reference_no'))})" if cl["kind"] == "refund"
                    else f"Nota debit supplier {_sr_text(data.get('supplier_cn_no'))} Rp {amount:,} dikonfirmasi; saldo kredit bisa dipakai").replace(",", ".")
                   + f" — klaim {cl['claim_no']}" + ("" if done else f", sisa Rp {left - amount:,}".replace(",", ".")), by)
    return next(c for c in list_refund_claims() if c["id"] == claim_id)


def cancel_refund_claim(claim_id: int, reason: str, user: dict) -> dict:
    """Admin: drops a claim that was entered by mistake or that the supplier will not honour. Only what has not been
    settled yet; money / credit already recorded stays on its own rows."""
    _sr_require_admin(user, "membatalkan klaim refund / kredit supplier")
    if not (reason or "").strip():
        raise ValueError("Alasan wajib diisi.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cl = con.execute("SELECT * FROM supplier_refund_claims WHERE id=?", (claim_id,)).fetchone()
        if cl is None:
            raise LookupError("Klaim tidak ditemukan.")
        if cl["status"] != "menunggu":
            raise ValueError("Klaim ini sudah selesai atau dibatalkan.")
        con.execute("UPDATE supplier_refund_claims SET status='dibatalkan', closed_by=?, closed_at=?, close_reason=? WHERE id=?",
                    (user.get("username"), _now(), reason.strip()[:300], claim_id))
        _spo_event(con, cl["supplier_po_id"], "refund_claim", f"Klaim {cl['claim_no']} dibatalkan: {reason.strip()}", user.get("username"))
    return next(c for c in list_refund_claims() if c["id"] == claim_id)


def cancel_supplier_po(supplier_po_id: str, by: str | None = None) -> dict:
    """Releases the quantities back to the customer lines. A line already advanced to
    'sudah_dipesankan' by this PO is left where it is: status history only moves
    forward, so move it back by hand if the order really did not go out."""
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()
        if row is None:
            raise LookupError(f"Purchase Order {supplier_po_id} tidak ditemukan.")
        if row["status"] not in ("draft", "dipesan"):
            raise ValueError("Purchase Order ini tidak bisa dibatalkan (sudah ada barang diterima atau sudah dibatalkan).")
        if con.execute(
            "SELECT 1 FROM supplier_po_items WHERE supplier_po_id=? AND qty_received > 0", (supplier_po_id,)
        ).fetchone():
            raise ValueError("Purchase Order ini sudah menerima barang, tidak bisa dibatalkan.")
        if row["paid_amount"] > 0:
            raise ValueError("Purchase Order ini sudah ada pembayaran. Ajukan pembatalan ke supplier (Batalkan PO): setelah supplier setuju, "
                             "uang muka dicatat sebagai Piutang Refund atau Kredit Supplier.")
        faktur = _active_po_faktur(con, supplier_po_id)
        if faktur:
            raise ValueError(f"Purchase Order ini sudah punya Faktur Pembelian {faktur['invoice_number']}. Hapus/void fakturnya dulu.")
        con.execute("UPDATE supplier_pos SET status='batal', updated_at=? WHERE id=?", (now, supplier_po_id))
        con.execute("UPDATE spo_packages SET cancelled=1, cancel_reason='PO dibatalkan', updated_at=? WHERE supplier_po_id=? AND cancelled=0",
                    (now, supplier_po_id))
        _spo_event(con, supplier_po_id, "cancel", "PO dibatalkan", by)
    return get_supplier_po(supplier_po_id)


# --- Idempotency keys ---------------------------------------------------------
# A POST that carries an Idempotency-Key header runs at most once: the first request claims the key,
# runs, and its response is stored; a repeat (double click, slow-network retry) gets that stored
# response back instead of creating a second nota / payment / stock movement. See backend/idempotency.py.


def claim_idempotency_key(scope: str) -> tuple[str, dict | None]:
    """('new', None) when this request owns the key, ('done', stored) when an earlier one finished,
    ('busy', None) when an earlier one is still running."""
    with _conn() as con:
        con.execute(
            "DELETE FROM idempotency_keys WHERE created_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),),
        )
        try:
            con.execute("INSERT INTO idempotency_keys (scope, created_at) VALUES (?, ?)", (scope, _now()))
            return "new", None
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT status, content_type, body FROM idempotency_keys WHERE scope=?", (scope,)
            ).fetchone()
            if row is None or row["status"] is None:
                return "busy", None
            return "done", dict(row)


def finish_idempotency_key(scope: str, status: int, content_type: str | None, body: bytes) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE idempotency_keys SET status=?, content_type=?, body=? WHERE scope=?",
            (status, content_type, body, scope),
        )


def release_idempotency_key(scope: str) -> None:
    """A failed attempt must not block the corrected retry."""
    with _conn() as con:
        con.execute("DELETE FROM idempotency_keys WHERE scope=? AND status IS NULL", (scope,))


# ---------------------------------------------------------------------------
# Master Kategori: kategori utama -> subkategori (two levels, never deleted)
# ---------------------------------------------------------------------------

CATEGORY_STATUSES = ("aktif", "nonaktif", "arsip")
_CATEGORY_CODE_RE = re.compile(r"^[A-Z0-9]+([-.][A-Z0-9]+)*$")


def _category_name_key(name: str) -> str:
    """Case/space-insensitive key; unlike _normalize_name it keeps every word."""
    return " ".join(re.sub(r"[^A-Z0-9 ]", "", name.upper()).split())


def get_category_row(category_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        return dict(row) if row else None


def category_subtree_ids(category_id: int) -> list[int]:
    with _conn() as con:
        rows = con.execute("SELECT id FROM categories WHERE parent_id=?", (category_id,)).fetchall()
    return [category_id] + [r["id"] for r in rows]


def list_categories_with_stats() -> list[dict]:
    """Every category with its own product count and a rolled-up total (a parent counts its
    subcategories' products too). Small table, so one pass in Python is simplest."""
    with _conn() as con:
        cats = [dict(r) for r in con.execute(
            """SELECT c.*, p.name AS parent_name, p.code AS parent_code FROM categories c
               LEFT JOIN categories p ON p.id = c.parent_id ORDER BY c.name_normalized ASC, c.id ASC"""
        ).fetchall()]
        counts = {r["category_id"]: dict(r) for r in con.execute(
            """SELECT category_id, COUNT(*) AS n,
                      SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active_n,
                      SUM(CASE WHEN stock_qty <= 0 THEN 1 ELSE 0 END) AS out_n
               FROM master_items WHERE category_id IS NOT NULL GROUP BY category_id"""
        ).fetchall()}
    by_parent: dict[int, list[dict]] = {}
    for c in cats:
        by_parent.setdefault(c["parent_id"] or 0, []).append(c)
    for c in cats:
        own = counts.get(c["id"], {"n": 0, "active_n": 0, "out_n": 0})
        subs = by_parent.get(c["id"], [])
        c["own_product_count"] = own["n"]
        c["product_count"] = own["n"] + sum(counts.get(s["id"], {}).get("n", 0) for s in subs)
        c["active_product_count"] = (own["active_n"] or 0) + sum(counts.get(s["id"], {}).get("active_n", 0) or 0 for s in subs)
        c["out_of_stock_count"] = (own["out_n"] or 0) + sum(counts.get(s["id"], {}).get("out_n", 0) or 0 for s in subs)
        c["child_count"] = len(subs)
    return cats


def get_categories_summary() -> dict:
    cats = list_categories_with_stats()
    mains = [c for c in cats if c["parent_id"] is None]
    subs = [c for c in cats if c["parent_id"] is not None]
    return {
        "total": len(cats),
        "total_utama": len(mains),
        "aktif": sum(1 for c in cats if c["status"] == "aktif"),
        "subkategori": len(subs),
        "tanpa_produk": sum(1 for c in cats if c["product_count"] == 0 and c["status"] != "arsip"),
        "diarsipkan": sum(1 for c in cats if c["status"] == "arsip"),
    }


def _letters_for_code(name: str, n: int = 3) -> str:
    letters = re.sub(r"[^A-Z0-9]", "", name.upper())
    consonants = re.sub(r"[AEIOU]", "", letters)
    base = (letters[:1] + consonants[1:] if consonants else letters) or "KAT"
    return (base + "XXX")[:n]


def category_code_preview(name: str, parent_id: int | None) -> str:
    with _conn() as con:
        if parent_id is None:
            prefix = "KAT-"
        else:
            parent = con.execute("SELECT code FROM categories WHERE id=?", (parent_id,)).fetchone()
            if parent is None:
                raise ValueError("Kategori induk tidak ditemukan.")
            prefix = "SUB-" + parent["code"].split("-", 1)[-1] + "-"
        base = prefix + _letters_for_code(name)
        code, n = base, 1
        while con.execute("SELECT 1 FROM categories WHERE code=?", (code,)).fetchone():
            n += 1
            code = f"{base}{n}"
        return code


def find_similar_categories(name: str, parent_id: int | None = None, exclude_id: int | None = None) -> list[dict]:
    """Soft warning for the form: same name elsewhere, or one name containing the other."""
    key = _category_name_key(name)
    if len(key) < 3:
        return []
    out = []
    for c in list_categories_with_stats():
        if c["id"] == exclude_id or c["status"] == "arsip":
            continue
        other = _category_name_key(c["name"])
        if other == key or key in other or other in key:
            out.append({"id": c["id"], "name": c["name"], "code": c["code"], "parent_name": c["parent_name"],
                        "same_parent": c["parent_id"] == parent_id, "exact": other == key})
    return out[:5]


def _validate_category_fields(con: sqlite3.Connection, name: str, parent_id: int | None, exclude_id: int | None) -> None:
    if not name.strip():
        raise ValueError("Nama kategori wajib diisi.")
    if len(name.strip()) > 80:
        raise ValueError("Nama kategori maksimal 80 karakter.")
    if parent_id is not None:
        parent = con.execute("SELECT * FROM categories WHERE id=?", (parent_id,)).fetchone()
        if parent is None:
            raise ValueError("Kategori induk tidak ditemukan.")
        if parent["parent_id"] is not None:
            raise ValueError("Struktur kategori hanya dua tingkat: pilih kategori utama sebagai induk.")
        if parent["status"] == "arsip":
            raise ValueError("Kategori induk sudah diarsipkan, pilih induk lain.")
        if exclude_id is not None and parent_id == exclude_id:
            raise ValueError("Kategori tidak bisa menjadi induk bagi dirinya sendiri.")
    key = _category_name_key(name)
    for r in con.execute(
        "SELECT id, name FROM categories WHERE parent_id IS ? AND id IS NOT ?", (parent_id, exclude_id)
    ).fetchall():
        if _category_name_key(r["name"]) == key:
            raise ValueError(f"Sudah ada kategori bernama '{r['name']}' di tingkat yang sama.")


def create_category(
    name: str, code: str | None, parent_id: int | None, status: str, icon: str | None,
    description: str | None, created_by: str | None,
) -> dict:
    if status not in ("aktif", "nonaktif"):
        raise ValueError("Status baru harus aktif atau nonaktif.")
    name = " ".join(name.split()).upper()
    now = _now()
    with _conn() as con:
        _validate_category_fields(con, name, parent_id, None)
        code = (code or "").strip().upper()
    if not code:
        code = category_code_preview(name, parent_id)
    if not _CATEGORY_CODE_RE.match(code) or len(code) > 30:
        raise ValueError("Kode hanya boleh huruf besar, angka, titik dan tanda hubung (maks 30 karakter), contoh: 01.04 atau KAT-LMP.")
    with _conn() as con:
        if con.execute("SELECT 1 FROM categories WHERE code=?", (code,)).fetchone():
            raise ValueError(f"Kode {code} sudah dipakai kategori lain.")
        cur = con.execute(
            """INSERT INTO categories (code, name, name_normalized, parent_id, status, icon, description,
                                       created_by, created_at, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, _category_name_key(name), parent_id, status, (icon or "").strip() or None,
             (description or "").strip() or None, created_by, now, created_by, now),
        )
        return dict(con.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone())


def update_category(
    category_id: int, name: str | None, parent_id: int | None, set_parent: bool, status: str | None,
    icon: str | None, set_icon: bool, description: str | None, set_description: bool,
    updated_by: str | None, expected_updated_at: str | None = None,
) -> dict:
    """The code is deliberately not editable: filters, CSV imports and reports refer to it."""
    with _conn() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if row is None:
            raise ValueError("Kategori tidak ditemukan.")
        if expected_updated_at and expected_updated_at != row["updated_at"]:
            raise ValueError("Data ini baru saja diubah oleh pengguna lain. Muat ulang halaman lalu ulangi perubahan Anda.")
        if row["status"] == "arsip":
            raise ValueError("Kategori ini diarsipkan. Pulihkan dulu sebelum mengubahnya.")
        new_name = " ".join((name if name is not None else row["name"]).split()).upper()
        new_parent = parent_id if set_parent else row["parent_id"]
        if set_parent and new_parent is not None:
            if con.execute("SELECT 1 FROM categories WHERE parent_id=?", (category_id,)).fetchone():
                raise ValueError("Kategori yang punya subkategori tidak bisa dijadikan subkategori.")
        _validate_category_fields(con, new_name, new_parent, category_id)
        new_status = status if status is not None else row["status"]
        if new_status not in ("aktif", "nonaktif"):
            raise ValueError("Status hanya bisa aktif atau nonaktif. Gunakan Arsipkan untuk mengarsipkan.")
        now = _now()
        con.execute(
            """UPDATE categories SET name=?, name_normalized=?, parent_id=?, status=?, icon=?, description=?,
                      updated_by=?, updated_at=? WHERE id=?""",
            (new_name, _category_name_key(new_name), new_parent, new_status,
             ((icon or "").strip() or None) if set_icon else row["icon"],
             ((description or "").strip() or None) if set_description else row["description"],
             updated_by, now, category_id),
        )
        return dict(con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone())


def set_category_archived(category_id: int, archived: bool, by: str | None) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if row is None:
            raise ValueError("Kategori tidak ditemukan.")
        now = _now()
        if archived:
            if row["status"] == "arsip":
                raise ValueError("Kategori ini sudah diarsipkan.")
            live = con.execute(
                "SELECT COUNT(*) AS n FROM categories WHERE parent_id=? AND status<>'arsip'", (category_id,)
            ).fetchone()["n"]
            if live:
                raise ValueError(f"Masih ada {live} subkategori yang belum diarsipkan. Arsipkan subkategorinya dulu.")
            con.execute("UPDATE categories SET status='arsip', updated_by=?, updated_at=? WHERE id=?", (by, now, category_id))
        else:
            if row["status"] != "arsip":
                raise ValueError("Kategori ini tidak sedang diarsipkan.")
            if row["parent_id"] is not None:
                parent = con.execute("SELECT status FROM categories WHERE id=?", (row["parent_id"],)).fetchone()
                if parent and parent["status"] == "arsip":
                    raise ValueError("Kategori induk masih diarsipkan. Pulihkan induknya dulu.")
            con.execute("UPDATE categories SET status='aktif', updated_by=?, updated_at=? WHERE id=?", (by, now, category_id))
        return dict(con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone())


def list_category_items(category_id: int, limit: int = 20, offset: int = 0) -> dict:
    ids = category_subtree_ids(category_id)
    marks = ",".join("?" * len(ids))
    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) AS n FROM master_items WHERE category_id IN ({marks})", ids).fetchone()["n"]
        rows = con.execute(
            f"""SELECT mi.id, mi.sku, mi.name, mi.stock_qty, mi.min_stock, mi.sell_price, mi.is_active, c.name AS category_name
                FROM master_items mi JOIN categories c ON c.id = mi.category_id
                WHERE mi.category_id IN ({marks}) ORDER BY mi.name ASC LIMIT ? OFFSET ?""",
            ids + [limit, offset],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


def get_categories_for_export() -> list[dict]:
    return [
        {"code": c["code"], "name": c["name"], "parent_code": c["parent_code"] or "", "status": c["status"],
         "description": c["description"] or "", "product_count": c["product_count"]}
        for c in list_categories_with_stats()
    ]


def import_categories(rows: list[dict], by: str | None) -> dict:
    """rows: kode (optional), nama, induk_kode (optional), status, deskripsi. A parent row must come
    before its children or already exist. Each row succeeds or fails on its own.

    A row whose name already exists at its level is skipped, but its `kode` is remembered as an alias
    of the existing category: the shop may already have "Lampu & Penerangan" under another code, and
    the subcategories that name K01 as their parent must still land under it."""
    created, skipped, errors = 0, 0, []
    alias: dict[str, int] = {}  # kode in this file -> id of the existing category it matched by name
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        name = (r.get("nama") or "").strip()
        if not name:
            errors.append({"line": i, "message": "Nama kosong."}); continue
        code = (r.get("kode") or "").strip().upper()
        parent_code = (r.get("induk_kode") or "").strip().upper()
        parent_id = None
        if parent_code:
            parent_id = alias.get(parent_code)
            if parent_id is None:
                with _conn() as con:
                    p = con.execute("SELECT id FROM categories WHERE code=?", (parent_code,)).fetchone()
                if p is None:
                    errors.append({"line": i, "message": f"Kode induk {parent_code} tidak ditemukan."}); continue
                parent_id = p["id"]
        status = (r.get("status") or "aktif").strip().lower()
        try:
            create_category(name, code or None, parent_id,
                            status if status in ("aktif", "nonaktif") else "aktif", None, r.get("deskripsi"), by)
            created += 1
        except ValueError as e:
            if "Sudah ada" in str(e) or "sudah dipakai" in str(e):
                skipped += 1
                if code:
                    key = _category_name_key(name)
                    with _conn() as con:
                        same = next((c["id"] for c in con.execute(
                            "SELECT id, name FROM categories WHERE parent_id IS ?", (parent_id,)
                        ).fetchall() if _category_name_key(c["name"]) == key), None)
                    if same is not None:
                        alias[code] = same
            else:
                errors.append({"line": i, "message": str(e)})
    return {"created": created, "skipped": skipped, "errors": errors}


def get_receipt_preorders(receipt_id: str) -> list[dict]:
    """Sales Order yang terkait ke sebuah nota: lewat alokasi DP atau barisnya yang sudah dijadikan nota."""
    with _conn() as con:
        rows = con.execute(
            """SELECT p.id, p.po_number, p.customer_name,
                      COALESCE((SELECT SUM(a.amount) FROM preorder_payment_allocations a
                                WHERE a.preorder_id = p.id AND a.receipt_id = ?), 0) AS dp_allocated,
                      (SELECT COUNT(*) FROM preorder_items pi
                        WHERE pi.preorder_id = p.id AND pi.receipt_id = ?) AS item_count
               FROM preorders p
               WHERE p.id IN (SELECT preorder_id FROM preorder_payment_allocations WHERE receipt_id = ?)
                  OR p.id IN (SELECT preorder_id FROM preorder_items WHERE receipt_id = ?)
               ORDER BY p.created_at ASC""",
            (receipt_id, receipt_id, receipt_id, receipt_id),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Retur Supplier -------------------------------------------------------------------------------
# Draft → Menunggu Persetujuan → Diproses → Selesai (or Dibatalkan before the goods leave). The goods
# sit in quarantine from approval; physical stock goes down exactly once, at handover (courier with a
# waybill, or the supplier's salesman signing the Bukti Serah Terima). The supplier's answer is per
# item; settling it never edits a faktur: replacements come in as Barang Masuk at the original cost,
# debit notes are allocated onto Faktur Pembelian, refunds are recorded as money received.

SR_TYPES = {"bermasalah": "Barang bermasalah", "garansi": "Klaim garansi", "salah_kirim": "Kelebihan / salah kirim"}
SR_SETTLEMENTS = {"ganti": "Ganti barang", "kredit": "Nota debit (potong tagihan)", "refund": "Refund uang"}
SR_METHODS = {"ekspedisi": "Dikirim lewat ekspedisi", "sales": "Diambil sales supplier"}
SR_COST_BEARERS = {"supplier": "Supplier", "toko": "Toko", "khusus": "Kesepakatan khusus"}
SR_REASONS = {
    "tidak_berfungsi": "Tidak berfungsi", "rusak_fisik": "Rusak fisik / pecah", "cacat_produksi": "Cacat produksi",
    "rusak_pengiriman": "Rusak saat pengiriman", "salah_model": "Salah model / tipe", "salah_warna": "Salah warna / varian",
    "tidak_lengkap": "Tidak lengkap", "kelebihan_kirim": "Kelebihan kirim", "tidak_sesuai_pesanan": "Tidak sesuai pesanan",
    "klaim_garansi": "Klaim garansi", "lainnya": "Lainnya",
}
SR_REJECT_FATES = {"kirim_balik": "Dikirim balik ke toko", "dimusnahkan": "Dimusnahkan supplier", "ditahan": "Ditahan supplier"}
SR_PHOTO_STAGES = {"bukti": "Bukti barang (sebelum dikemas)", "sesudah_kemas": "Sesudah dikemas", "resi": "Resi ekspedisi",
                   "serah_terima": "Bukti Serah Terima ditandatangani"}
SR_DOC_STATUS = {"draft": "Draft", "menunggu_persetujuan": "Menunggu Persetujuan", "diproses": "Diproses",
                 "selesai": "Selesai", "dibatalkan": "Dibatalkan"}
SR_HANDOVER = {"belum": "Belum Disiapkan", "siap": "Siap Dikirim", "menunggu_ambil": "Menunggu Pengambilan",
               "dikirim": "Dalam Pengiriman", "diserahkan": "Sudah Diserahkan ke Sales", "diterima_supplier": "Diterima Supplier"}
SR_OUT = ("dikirim", "diserahkan", "diterima_supplier")
SR_PHOTO_MAX_BYTES = 6 * 1024 * 1024
SR_PHOTO_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
SR_PAGE_SIZE = 50


def _sr_number(con: sqlite3.Connection, prefix: str, table: str, column: str) -> str:
    """{prefix}-YYYYMMDD-NNN, counted per day. Rows are never deleted, so a number is never reused."""
    head = f"{prefix}-{shop_today().strftime('%Y%m%d')}-"
    row = con.execute(f"SELECT {column} AS n FROM {table} WHERE {column} LIKE ? ORDER BY {column} DESC LIMIT 1",
                      (f"{head}%",)).fetchone()
    seq = int(row["n"].rsplit("-", 1)[-1]) + 1 if row else 1
    return f"{head}{seq:03d}"


def peek_supplier_return_number() -> str:
    """The number the next Retur Supplier will get, shown read-only on the new-return form."""
    with _conn() as con:
        return _sr_number(con, "RTS", "supplier_returns", "return_number")


def _sr_event(con: sqlite3.Connection, return_id: int, kind: str, note: str | None, by: str | None) -> None:
    con.execute("INSERT INTO supplier_return_events (return_id, kind, note, by_user, at) VALUES (?, ?, ?, ?, ?)",
                (return_id, kind, note, by, _now()))
    con.execute("UPDATE supplier_returns SET updated_at=? WHERE id=?", (_now(), return_id))


def _sr_get(con: sqlite3.Connection, return_id: int) -> dict:
    row = con.execute("SELECT * FROM supplier_returns WHERE id=?", (return_id,)).fetchone()
    if row is None:
        raise LookupError("Retur supplier tidak ditemukan.")
    return dict(row)


def _sr_items(con: sqlite3.Connection, return_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        """SELECT i.*, mi.sku, mi.unit, mi.stock_qty AS item_stock_qty, mi.quarantine_qty AS item_quarantine_qty,
                  ql.source_type AS lot_source_type, ql.source_id AS lot_source_id, ql.source_ref AS lot_source_ref FROM supplier_return_items i
           LEFT JOIN master_items mi ON mi.id = i.master_item_id LEFT JOIN quarantine_lots ql ON ql.id = i.source_lot_id
           WHERE i.return_id=? ORDER BY i.position, i.id""", (return_id,))]


def _sr_rupiah(n: int) -> str:
    return f"{int(n or 0):,}".replace(",", ".")


def _sr_text(v, limit: int = 300) -> str | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return (v[:limit] if isinstance(v, str) else v) or None


def _sr_is_admin(user: dict) -> bool:
    return (user or {}).get("role") == "admin"


def _sr_require_admin(user: dict, what: str) -> None:
    if not _sr_is_admin(user):
        raise PermissionError(f"Hanya admin yang bisa {what}.")


def _sr_rate(con: sqlite3.Connection, ret: dict) -> float:
    """PPN % of the faktur the return belongs to: what the supplier was paid per unit includes it."""
    if not ret.get("purchase_invoice_id"):
        return 0.0
    row = con.execute("SELECT tax_rate FROM purchase_invoices WHERE id=?", (ret["purchase_invoice_id"],)).fetchone()
    return float(row["tax_rate"] or 0) if row else 0.0


def _sr_value(qty: int, unit_cost: int, rate: float) -> int:
    return int(round(qty * unit_cost * (1 + float(rate or 0) / 100)))


def _sr_money(con: sqlite3.Connection, ret: dict, items: list[dict]) -> dict:
    """Goods value (purchase price), the money part the supplier approved (approved qty settled by credit
    or refund incl. the faktur's PPN, plus the shipping claim), and how much of it is settled by posted credit
    notes / refunds. A Debit Note still in Draft / verification settles nothing yet (credit_pending); what the
    shop gave up on a posted note (waived) is not claimed again."""
    goods = sum(i["qty"] * i["unit_cost"] for i in items)
    rate = _sr_rate(con, ret)
    claim = sum(_sr_value(i["qty_approved"], i["unit_cost"], rate) for i in items if i["settlement"] in ("kredit", "refund")) \
        + (ret["shipping_claim_approved"] or 0)
    settled = con.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_credit_notes WHERE return_id=? AND status='aktif'",
                          (ret["id"],)).fetchone()["n"]
    pending = con.execute("""SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_credit_notes WHERE return_id=? AND kind='kredit'
                             AND status IN ('draft','review','disetujui')""", (ret["id"],)).fetchone()["n"]
    waived = con.execute("""SELECT COALESCE(SUM(ci.waived), 0) AS n FROM supplier_credit_note_items ci JOIN supplier_credit_notes c ON c.id = ci.credit_note_id
                            WHERE c.return_id=? AND c.status='aktif'""", (ret["id"],)).fetchone()["n"]
    return {"cost_value": goods, "claim_value": claim, "settled_value": settled, "claim_left": max(claim - settled - waived, 0),
            "credit_pending": pending, "credit_waived": waived}


def _sr_validate_items(con: sqlite3.Connection, ret_id: int | None, supplier_id: int, items: list[dict]) -> list[dict]:
    out = []
    for pos, it in enumerate(items):
        src = it.get("source")
        qty = int(it.get("qty") or 0)
        if qty <= 0:
            raise ValueError("Qty retur harus lebih dari 0.")
        reason = it.get("reason_code") or None
        if reason and reason not in SR_REASONS:
            raise ValueError("Alasan retur tidak dikenal.")
        cond = it.get("condition") or "rusak"
        if cond not in QUARANTINE_CONDITIONS:
            raise ValueError("Kondisi harus Rusak, Tidak sesuai, atau Perlu dicek.")
        row = {"source": src, "qty": qty, "reason_code": reason, "inbound_item_id": int(it["inbound_item_id"]) if it.get("inbound_item_id") else None, "reason_detail": _sr_text(it.get("reason_detail")),
               "test_result": _sr_text(it.get("test_result")), "serial_no": _sr_text(it.get("serial_no"), 120),
               "condition": cond, "position": pos, "source_lot_id": None, "quarantine_lot_id": None}
        if src == "karantina":
            lot = con.execute("SELECT * FROM quarantine_lots WHERE id=?", (it.get("quarantine_lot_id"),)).fetchone()
            if lot is None:
                raise ValueError("Barang karantina yang dipilih tidak ditemukan.")
            lot = dict(lot)
            free = lot["qty_left"] - _q_pending(con, lot["id"]) + (_q_return_hold(con, lot["id"]) - _q_return_hold(con, lot["id"], ret_id))
            if qty > free:
                raise ValueError(f"“{lot['product_name']}” di karantina hanya bisa diretur {max(free, 0)}.")
            row.update(source_lot_id=lot["id"], quarantine_lot_id=lot["id"], master_item_id=lot["master_item_id"],
                       product_name=lot["product_name"], condition=lot["condition"],
                       unit_cost=int(it.get("unit_cost") or 0) or lot["unit_cost"] or 0)
        elif src == "stok":
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (it.get("master_item_id"),)).fetchone()
            if mi is None:
                raise ValueError("Barang yang dipilih tidak ditemukan di Master Barang.")
            row.update(master_item_id=mi["id"], product_name=mi["name"],
                       unit_cost=int(it.get("unit_cost") or 0) or mi["avg_cost_price"] or mi["last_cost_price"] or 0)
        else:
            raise ValueError("Sumber barang retur harus Stok Karantina atau Stok Toko.")
        if row["unit_cost"] < 0:
            raise ValueError("Harga beli tidak boleh minus.")
        out.append(row)
    per_line: dict[int, int] = {}
    for r in out:
        if r["inbound_item_id"]:
            per_line[r["inbound_item_id"]] = per_line.get(r["inbound_item_id"], 0) + r["qty"]
    for ii, q in per_line.items():
        line = con.execute("SELECT i.qty_in, d.doc_number FROM inbound_items i JOIN inbound_documents d ON d.id = i.document_id WHERE i.id=?",
                           (ii,)).fetchone()
        if line is None:
            raise ValueError("Baris Barang Masuk tidak ditemukan.")
        left = line["qty_in"] - _sr_returned_from_line(con, ii, ret_id)
        if q > left:
            raise ValueError(f"Dari {line['doc_number']} barang ini hanya bisa diretur {max(left, 0)} lagi.")
    return out


def _sr_returned_from_line(con: sqlite3.Connection, inbound_item_id: int, excluding_return: int | None) -> int:
    """Qty of one Barang Masuk line already on another return that is not cancelled."""
    return con.execute(
        """SELECT COALESCE(SUM(i.qty), 0) AS n FROM supplier_return_items i JOIN supplier_returns r ON r.id = i.return_id
           WHERE i.inbound_item_id=? AND r.doc_status<>'dibatalkan' AND r.id<>?""", (inbound_item_id, excluding_return or 0)).fetchone()["n"]


def _sr_blocked_reason(con: sqlite3.Connection, inbound_item_id: int, left: int, avail: int, source: str,
                       excluding_return: int | None) -> str:
    """Why a Barang Masuk line offers nothing to return, in words the staff can act on."""
    if left <= 0:
        nums = [r["return_number"] for r in con.execute(
            """SELECT DISTINCT r.return_number FROM supplier_return_items i JOIN supplier_returns r ON r.id = i.return_id
               WHERE i.inbound_item_id=? AND r.doc_status<>'dibatalkan' AND r.id<>? AND i.qty > 0""", (inbound_item_id, excluding_return or 0))]
        return "Sudah diretur" + (f" di {', '.join(nums)}" if nums else "")
    if source == "karantina":
        return "Sudah tidak ada di Stok Karantina"
    return "Stok di toko 0 (sudah terjual / terpasang)" if avail <= 0 else "Tidak tersedia"


def supplier_return_inbound_lines(document_id: int, excluding_return: int | None = None) -> dict:
    """What a posted Barang Masuk offers for a return: its lines (good ones are on the shelf, bad ones in
    quarantine) and the quarantine lots its PO receipt put aside, with received / already returned / returnable."""
    with _conn() as con:
        doc = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if doc is None:
            raise LookupError("Barang Masuk tidak ditemukan.")
        if doc["status"] != "posted":
            raise ValueError("Pilih Barang Masuk yang sudah diposting (bukan draft / void).")
        rows = []
        for i in con.execute(
                """SELECT i.*, mi.name, mi.sku, mi.unit, mi.stock_qty FROM inbound_items i
                   LEFT JOIN master_items mi ON mi.id = i.master_item_id WHERE i.document_id=? ORDER BY i.id""", (document_id,)):
            i = dict(i)
            returned = _sr_returned_from_line(con, i["id"], excluding_return)
            row = {"key": f"bm{i['id']}", "inbound_item_id": i["id"], "master_item_id": i["master_item_id"],
                   "product_name": i["name"] or i["pending_product_name"], "sku": i["sku"], "unit": i["unit"],
                   "qty_received": i["qty_in"], "unit_cost": i["cost_price"], "returned": returned,
                   "condition": i.get("condition") or "baik", "source": "stok", "quarantine_lot_id": None, "stock_qty": i["stock_qty"]}
            avail = i["stock_qty"] or 0
            if row["condition"] in BAD_RECEIVE_CONDITIONS:
                lot = con.execute("""SELECT * FROM quarantine_lots WHERE source_type='barang_masuk' AND source_id=? AND master_item_id=?
                                     ORDER BY id LIMIT 1""", (str(document_id), i["master_item_id"])).fetchone()
                if lot:
                    row.update(source="karantina", quarantine_lot_id=lot["id"])
                    avail = lot["qty_left"] - _q_pending(con, lot["id"]) + _q_return_hold(con, lot["id"]) - _q_return_hold(con, lot["id"], excluding_return)
                else:
                    avail = 0
            row["returnable"] = max(min(i["qty_in"] - returned, avail), 0)
            row["blocked_reason"] = _sr_blocked_reason(con, i["id"], i["qty_in"] - returned, avail, row["source"], excluding_return) \
                if row["returnable"] == 0 else None
            rows.append(row)
        for lot in con.execute(
                """SELECT q.*, mi.sku, mi.unit FROM quarantine_lots q LEFT JOIN master_items mi ON mi.id = q.master_item_id
                   WHERE q.source_type='terima_barang' AND q.source_id IN
                         (SELECT CAST(id AS TEXT) FROM spo_receipts WHERE inbound_document_id=?)""", (document_id,)):
            lot = dict(lot)
            free = lot["qty_left"] - _q_pending(con, lot["id"]) + _q_return_hold(con, lot["id"]) - _q_return_hold(con, lot["id"], excluding_return)
            rows.append({"key": f"lot{lot['id']}", "inbound_item_id": None, "master_item_id": lot["master_item_id"],
                         "product_name": lot["product_name"], "sku": lot["sku"], "unit": lot["unit"], "qty_received": lot["quantity"],
                         "unit_cost": lot["unit_cost"], "returned": lot["quantity"] - lot["qty_left"], "condition": lot["condition"],
                         "source": "karantina", "quarantine_lot_id": lot["id"], "stock_qty": None, "returnable": max(free, 0)})
        return {"id": doc["id"], "doc_number": doc["doc_number"], "supplier_id": doc["supplier_id"], "supplier_po_id": doc["supplier_po_id"],
                "received_date": doc["received_date"], "lines": rows}


def supplier_return_sources(supplier_id: int) -> dict:
    """Pickers for step 1: the supplier's issued POs, posted Barang Masuk and posted fakturs."""
    with _conn() as con:
        pos = [dict(r) for r in con.execute(
            "SELECT id, po_number, po_date FROM supplier_pos WHERE supplier_id=? AND status NOT IN ('draft', 'batal') ORDER BY po_date DESC, rowid DESC LIMIT 200",
            (supplier_id,))]
        bms = [dict(r) for r in con.execute(
            """SELECT id, doc_number, received_date, supplier_po_id FROM inbound_documents
               WHERE supplier_id=? AND status='posted' ORDER BY received_date DESC, id DESC LIMIT 200""", (supplier_id,))]
        fks = [dict(r) for r in con.execute(
            """SELECT id, invoice_number, supplier_invoice_no, supplier_po_id, total, amount_paid FROM purchase_invoices
               WHERE supplier_id=? AND status='posted' ORDER BY invoice_date DESC, id DESC LIMIT 200""", (supplier_id,))]
        sup = con.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        return {"purchase_orders": pos, "inbound_documents": bms, "purchase_invoices": fks,
                "supplier": dict(sup) if sup else None}


_SR_HEADER_FIELDS = (
    "return_date", "return_type", "requested_settlement", "rma_number", "supplier_po_id", "inbound_document_id",
    "purchase_invoice_id", "notes", "method", "courier", "courier_service", "plan_date", "ship_address", "ship_recipient",
    "ship_phone", "sales_name", "sales_phone", "sales_company", "pickup_at", "cost_bearer", "shipping_estimate",
    "packages", "packing_text", "approver_note",
)


def _sr_clean_header(data: dict) -> dict:
    h = {}
    for k in _SR_HEADER_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "packages":
            v = json.dumps(v or [], ensure_ascii=False)
        elif k in ("shipping_estimate",):
            v = max(int(v or 0), 0)
        elif k in ("inbound_document_id", "purchase_invoice_id"):
            v = int(v) if v else None
        else:
            v = _sr_text(v, 2000 if k in ("packing_text", "notes") else 300)
        h[k] = v
    if h.get("return_type") and h["return_type"] not in SR_TYPES:
        raise ValueError("Jenis retur tidak dikenal.")
    if h.get("requested_settlement") and h["requested_settlement"] not in SR_SETTLEMENTS:
        raise ValueError("Penyelesaian yang diminta harus Ganti, Nota Debit, atau Refund.")
    if h.get("method") and h["method"] not in SR_METHODS:
        raise ValueError("Metode pengembalian harus Ekspedisi atau Diambil Sales.")
    if h.get("cost_bearer") and h["cost_bearer"] not in SR_COST_BEARERS:
        raise ValueError("Penanggung ongkir harus Supplier, Toko, atau Kesepakatan Khusus.")
    if h.get("return_date"):
        try:
            date.fromisoformat(h["return_date"])
        except ValueError:
            raise ValueError("Tanggal retur harus berformat YYYY-MM-DD.")
    return h


def save_supplier_return(return_id: int | None, data: dict, user: dict) -> dict:
    """Creates or edits a draft. Nothing moves: stock, quarantine and debt stay as they are."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        header = _sr_clean_header(data)
        if return_id is None:
            supplier_id = data.get("supplier_id")
            if not con.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone():
                raise ValueError("Pilih supplier.")
            now = _now()
            cur = con.execute(
                """INSERT INTO supplier_returns (return_number, return_date, supplier_id, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_sr_number(con, "RTS", "supplier_returns", "return_number"), header.get("return_date") or shop_today().isoformat(),
                 supplier_id, by, now, now))
            return_id = cur.lastrowid
            ret = _sr_get(con, return_id)
            _sr_event(con, return_id, "dibuat", f"Draft {ret['return_number']} dibuat", by)
        else:
            ret = _sr_get(con, return_id)
            if ret["doc_status"] != "draft":
                raise ValueError("Retur hanya bisa diubah selagi Draft. Admin bisa mengembalikannya ke Draft.")
            if data.get("version") and data["version"] != ret["updated_at"]:
                raise ValueError("Retur ini baru saja diubah orang lain. Muat ulang dulu.")
            if data.get("supplier_id") and int(data["supplier_id"]) != ret["supplier_id"]:
                if con.execute("SELECT 1 FROM supplier_return_items WHERE return_id=?", (return_id,)).fetchone() and "items" not in data:
                    raise ValueError("Supplier tidak bisa diganti selagi ada barang di retur ini.")
                con.execute("UPDATE supplier_returns SET supplier_id=? WHERE id=?", (int(data["supplier_id"]), return_id))
            _sr_event(con, return_id, "diubah", "Draft diperbarui", by)
        if header:
            con.execute(f"UPDATE supplier_returns SET {', '.join(f'{k}=?' for k in header)} WHERE id=?",
                        (*header.values(), return_id))
        if "items" in data:
            ret = _sr_get(con, return_id)
            rows = _sr_validate_items(con, return_id, ret["supplier_id"], data.get("items") or [])
            con.execute("DELETE FROM supplier_return_items WHERE return_id=?", (return_id,))
            for r in rows:
                con.execute(
                    """INSERT INTO supplier_return_items (return_id, source, source_lot_id, quarantine_lot_id, master_item_id,
                           product_name, qty, unit_cost, condition, reason_code, reason_detail, test_result, serial_no, position,
                           inbound_item_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (return_id, r["source"], r["source_lot_id"], r["quarantine_lot_id"], r["master_item_id"], r["product_name"],
                     r["qty"], r["unit_cost"], r["condition"], r["reason_code"], r["reason_detail"], r["test_result"],
                     r["serial_no"], r["position"], r["inbound_item_id"]))
        con.execute("UPDATE supplier_returns SET updated_at=? WHERE id=?", (_now(), return_id))
    return get_supplier_return(return_id)


def _sr_photo_count(con: sqlite3.Connection, return_id: int, stage: str) -> int:
    return con.execute("SELECT COUNT(*) AS n FROM supplier_return_photos WHERE return_id=? AND stage=?",
                       (return_id, stage)).fetchone()["n"]


def _sr_checklist(con: sqlite3.Connection, ret: dict, items: list[dict]) -> list[dict]:
    """What must be right before the return can be submitted. level 'blok' stops it; 'peringatan' does not."""
    out = []
    def add(ok, level, text):
        out.append({"ok": bool(ok), "level": level, "text": text})
    add(items, "blok", "Minimal satu barang diretur")
    add(all(i["reason_code"] for i in items), "blok", "Setiap barang punya alasan retur")
    add(all(i["reason_code"] != "tidak_berfungsi" or i["test_result"] for i in items), "blok",
        "Barang “Tidak berfungsi” punya hasil uji")
    add(all(i["reason_code"] != "lainnya" or i["reason_detail"] for i in items), "blok", "Alasan “Lainnya” dijelaskan")
    add(_sr_photo_count(con, ret["id"], "bukti") > 0, "blok", "Foto bukti barang (sebelum dikemas) sudah diunggah")
    pk = json.loads(ret["packages"] or "[]")
    add(pk, "blok", "Packing note: minimal satu koli")
    add(ret["method"], "blok", "Metode pengembalian dipilih")
    if ret["method"] == "ekspedisi":
        add(ret["ship_address"] and ret["ship_recipient"], "blok", "Alamat & penerima pengiriman diisi")
        add(ret["courier"], "peringatan", "Ekspedisi direncanakan")
    if ret["method"] == "sales":
        add(ret["sales_name"] and ret["pickup_at"], "blok", "Nama sales & jadwal ambil diisi")
    add(ret["cost_bearer"], "blok", "Penanggung ongkir dipilih")
    add(all(i["unit_cost"] > 0 for i in items), "peringatan", "Harga beli setiap barang terisi")
    return out


def submit_supplier_return(return_id: int, note: str | None, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "draft":
            raise ValueError("Hanya Draft yang bisa diajukan.")
        items = _sr_items(con, return_id)
        bad = [c["text"] for c in _sr_checklist(con, ret, items) if c["level"] == "blok" and not c["ok"]]
        if bad:
            raise ValueError("Belum bisa diajukan: " + "; ".join(bad) + ".")
        _sr_validate_items(con, return_id, ret["supplier_id"], [
            {**i, "quarantine_lot_id": i["source_lot_id"]} for i in items])
        n = con.execute("UPDATE supplier_returns SET doc_status='menunggu_persetujuan', submitted_by=?, submitted_at=?, approver_note=? "
                        "WHERE id=? AND doc_status='draft'", (by, _now(), _sr_text(note), return_id)).rowcount
        if n != 1:
            raise ValueError("Retur ini sudah diajukan.")
        _sr_event(con, return_id, "diajukan", "Diajukan untuk persetujuan admin" + (f": {note.strip()}" if note and note.strip() else ""), by)
    return get_supplier_return(return_id)


def _sr_shortage(con: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """K-02: stock items whose return would leave less on the shelf than Sales Orders have reserved."""
    ids = [i["master_item_id"] for i in items if i["source"] == "stok" and i["master_item_id"]]
    if not ids:
        return []
    reserved = _reserved_by_item(con, ids)
    out = []
    need: dict[int, int] = {}
    for i in items:
        if i["source"] == "stok" and i["master_item_id"]:
            need[i["master_item_id"]] = need.get(i["master_item_id"], 0) + i["qty"]
    for mid, q in need.items():
        mi = con.execute("SELECT name, stock_qty FROM master_items WHERE id=?", (mid,)).fetchone()
        r = reserved.get(mid, 0)
        if mi and mi["stock_qty"] - q < r:
            out.append({"master_item_id": mid, "product_name": mi["name"], "stock_qty": mi["stock_qty"], "reserved": r,
                        "qty": q, "short": r - (mi["stock_qty"] - q)})
    return out


def approve_supplier_return(return_id: int, shortage_ack: bool, note: str | None, user: dict) -> dict:
    """Admin: from Draft or Menunggu Persetujuan to Diproses. Stock items are moved off the shelf into
    quarantine now; quarantine items are already there and are held for this return."""
    _sr_require_admin(user, "menyetujui retur")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] not in ("draft", "menunggu_persetujuan"):
            raise ValueError("Retur ini sudah diproses atau dibatalkan.")
        items = _sr_items(con, return_id)
        bad = [c["text"] for c in _sr_checklist(con, ret, items) if c["level"] == "blok" and not c["ok"]]
        if bad:
            raise ValueError("Belum bisa disetujui: " + "; ".join(bad) + ".")
        _sr_validate_items(con, return_id, ret["supplier_id"], [{**i, "quarantine_lot_id": i["source_lot_id"]} for i in items])
        short = _sr_shortage(con, items)
        if short and not shortage_ack:
            raise ValueError("Stok ini dicadangkan untuk Sales Order: " + "; ".join(
                f"{s['product_name']} (stok {s['stock_qty']}, dicadangkan {s['reserved']}, diretur {s['qty']})" for s in short)
                + ". Centang “lanjutkan dan tandai kekurangan” bila tetap diretur.")
        for i in items:
            if i["source"] != "stok":
                continue
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (i["master_item_id"],)).fetchone()
            if mi["stock_qty"] < i["qty"]:
                raise ValueError(f"Stok {mi['name']} tinggal {mi['stock_qty']}, tidak cukup untuk diretur {i['qty']}.")
            new_qty = mi["stock_qty"] - i["qty"]
            con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_qty, mi["id"]))
            lot_id = _quarantine_in(
                con, master_item_id=mi["id"], product_name=mi["name"], qty=i["qty"], condition=i["condition"],
                source_type="retur_supplier", source_ref=ret["return_number"], source_id=str(return_id),
                supplier_id=ret["supplier_id"], unit_cost=i["unit_cost"], candidate=False, note=None, by=by)
            con.execute("UPDATE supplier_return_items SET quarantine_lot_id=? WHERE id=?", (lot_id, i["id"]))
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'KARANTINA', ?, ?, 'supplier_return', ?, ?, ?, ?)""",
                (mi["id"], -i["qty"], new_qty, return_id, f"Disiapkan untuk retur {ret['return_number']}", _now(), by))
        handover = "siap" if ret["method"] == "ekspedisi" else "menunggu_ambil"
        n = con.execute(
            "UPDATE supplier_returns SET doc_status='diproses', handover_status=?, approved_by=?, approved_at=? "
            "WHERE id=? AND doc_status IN ('draft', 'menunggu_persetujuan')", (handover, by, _now(), return_id)).rowcount
        if n != 1:
            raise ValueError("Retur ini sudah diproses.")
        msg = "Disetujui admin; barang ditahan di karantina sampai keluar"
        if short:
            msg += ". Kekurangan Sales Order diterima: " + ", ".join(f"{s['product_name']} kurang {s['short']}" for s in short)
        if note and note.strip():
            msg += f". Catatan: {note.strip()}"
        _sr_event(con, return_id, "disetujui", msg, by)
    return get_supplier_return(return_id)


def _sr_unhold_stock_items(con: sqlite3.Connection, ret: dict, keep_in_quarantine: bool, by: str | None, why: str) -> None:
    """Undo what approval did to stock items: back to the shelf, or left in quarantine as a candidate."""
    back: list[int] = []
    for i in _sr_items(con, ret["id"]):
        if i["source"] != "stok" or not i["quarantine_lot_id"]:
            continue
        lot = _q_lot(con, i["quarantine_lot_id"])
        if keep_in_quarantine:
            con.execute("UPDATE quarantine_lots SET retur_candidate=1 WHERE id=?", (lot["id"],))
            _q_event(con, lot["id"], "catatan", 0, f"Retur {ret['return_number']} dibatalkan; barang tetap di karantina", by)
        elif lot["qty_left"] > 0:
            q = lot["qty_left"]
            _quarantine_take(con, lot, q, "lepas", f"{why} ({ret['return_number']}), kembali ke stok", by)
            mi = con.execute("SELECT stock_qty FROM master_items WHERE id=?", (lot["master_item_id"],)).fetchone()
            new_qty = mi["stock_qty"] + q
            con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_qty, lot["master_item_id"]))
            con.execute(
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'LEPAS_KARANTINA', ?, ?, 'supplier_return', ?, ?, ?, ?)""",
                (lot["master_item_id"], q, new_qty, ret["id"], f"{why} {ret['return_number']}", _now(), by))
            back.append(lot["master_item_id"])
        con.execute("UPDATE supplier_return_items SET quarantine_lot_id=NULL WHERE id=?", (i["id"],))
    _promote_shelf_ready_lines(con, by, master_item_ids=back)


def return_supplier_return_to_draft(return_id: int, note: str, user: dict) -> dict:
    _sr_require_admin(user, "mengembalikan retur ke Draft")
    by = user.get("username")
    if not (note or "").strip():
        raise ValueError("Tulis alasan dikembalikan ke Draft.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] not in ("menunggu_persetujuan", "diproses") or ret["handover_status"] in SR_OUT:
            raise ValueError("Hanya retur yang belum keluar yang bisa dikembalikan ke Draft.")
        _sr_unhold_stock_items(con, ret, False, by, "Dikembalikan ke Draft")
        con.execute("UPDATE supplier_returns SET doc_status='draft', handover_status='belum', approved_by=NULL, approved_at=NULL WHERE id=?",
                    (return_id,))
        _sr_event(con, return_id, "ke_draft", f"Dikembalikan ke Draft: {note.strip()}", by)
    return get_supplier_return(return_id)


# A draft nobody has acted on yet: only created, edited or given photos. Anything else (submitted and sent back,
# printed, a decision, money) has left a trail and is cancelled instead.
SR_UNTOUCHED_EVENTS = ("dibuat", "diubah", "foto")


def supplier_return_deletable(con: sqlite3.Connection, ret: dict) -> bool:
    if ret["doc_status"] != "draft":
        return False
    q = f"SELECT 1 FROM supplier_return_events WHERE return_id=? AND kind NOT IN ({_placeholders(SR_UNTOUCHED_EVENTS)}) LIMIT 1"
    if con.execute(q, (ret["id"], *SR_UNTOUCHED_EVENTS)).fetchone():
        return False
    return not any(con.execute(f"SELECT 1 FROM {t} WHERE return_id=? LIMIT 1", (ret["id"],)).fetchone()
                   for t in ("supplier_return_replacements", "supplier_credit_notes"))


def delete_supplier_return(return_id: int, user: dict) -> None:
    """Owner 25-10-2026: an untouched draft can be deleted outright (nothing moved: stock, quarantine and debt
    are untouched by a draft). Its photos' files go too."""
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if not supplier_return_deletable(con, ret):
            raise ValueError("Hanya retur berstatus Draft yang belum diajukan, dicetak, atau diproses yang bisa dihapus. "
                             "Retur lain dibatalkan lewat tombol Batalkan.")
        paths = [r["path"] for r in con.execute("SELECT path FROM supplier_return_photos WHERE return_id=?", (return_id,))]
        for t in ("supplier_return_photos", "supplier_return_events", "supplier_return_items"):
            con.execute(f"DELETE FROM {t} WHERE return_id=?", (return_id,))
        con.execute("DELETE FROM supplier_returns WHERE id=?", (return_id,))
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


def cancel_supplier_return(return_id: int, reason: str, keep_in_quarantine: bool, user: dict) -> dict:
    _sr_require_admin(user, "membatalkan retur")
    by = user.get("username")
    if not (reason or "").strip():
        raise ValueError("Alasan pembatalan wajib diisi.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] in ("selesai", "dibatalkan"):
            raise ValueError("Retur ini sudah selesai atau dibatalkan.")
        if ret["handover_status"] in SR_OUT:
            raise ValueError("Barang sudah keluar ke supplier, retur tidak bisa dibatalkan. Minta barang dikembalikan, lalu catat sebagai barang ditolak yang kembali.")
        _sr_unhold_stock_items(con, ret, keep_in_quarantine, by, "Retur dibatalkan")
        con.execute("UPDATE supplier_returns SET doc_status='dibatalkan', cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
                    (by, _now(), reason.strip(), return_id))
        _sr_event(con, return_id, "dibatalkan", f"Dibatalkan: {reason.strip()}" + (" (barang tetap di karantina)" if keep_in_quarantine else ""), by)
    return get_supplier_return(return_id)


def confirm_supplier_return_out(return_id: int, data: dict, user: dict) -> dict:
    """The one moment physical stock leaves: the parcel is handed to the courier (waybill) or the
    salesman signs the Bukti Serah Terima. Compare-and-set on handover_status, so a double click or
    two users can post it only once."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "diproses" or ret["handover_status"] not in ("siap", "menunggu_ambil"):
            if ret["handover_status"] in SR_OUT:
                ev = con.execute("SELECT by_user, at FROM supplier_return_events WHERE return_id=? AND kind='keluar' ORDER BY id DESC LIMIT 1",
                                 (return_id,)).fetchone()
                raise ValueError(f"Barang retur ini sudah dikonfirmasi keluar{' oleh ' + ev['by_user'] if ev and ev['by_user'] else ''}.")
            raise ValueError("Retur harus disetujui admin sebelum barang keluar.")
        if _sr_photo_count(con, return_id, "sesudah_kemas") == 0:
            raise ValueError("Unggah foto paket sesudah dikemas dulu.")
        handed_at = _sr_text(data.get("handed_at")) or _now()[:16]
        officer = _sr_text(data.get("officer")) or by
        notes = []
        if ret["method"] == "ekspedisi":
            awb = (_sr_text(data.get("awb"), 60) or "").upper()
            if not awb:
                raise ValueError("Nomor resi wajib diisi.")
            dup = con.execute("SELECT return_number FROM supplier_returns WHERE awb=? AND id<>?", (awb, return_id)).fetchone()
            if dup:
                raise ValueError(f"Resi {awb} sudah dipakai retur {dup['return_number']}.")
            if _sr_photo_count(con, return_id, "resi") == 0:
                raise ValueError("Unggah foto resi dulu.")
            courier = _sr_text(data.get("courier")) or ret["courier"]
            if not courier:
                raise ValueError("Ekspedisi wajib diisi.")
            cost = max(int(data.get("shipping_cost") or 0), 0)
            bearer = data.get("cost_bearer") or ret["cost_bearer"]
            if bearer not in SR_COST_BEARERS:
                raise ValueError("Pilih penanggung ongkir.")
            n = con.execute(
                """UPDATE supplier_returns SET handover_status='dikirim', awb=?, courier=?, handed_at=?, shipping_cost=?, cost_bearer=?,
                       officer=?, koli_handed=? WHERE id=? AND handover_status='siap'""",
                (awb, courier, handed_at, cost, bearer, officer, int(data.get("koli") or 0) or None, return_id)).rowcount
            notes.append(f"Diserahkan ke {courier}, resi {awb}, ongkir Rp {_sr_rupiah(cost)} ditanggung {SR_COST_BEARERS[bearer].lower()}")
        else:
            taker = _sr_text(data.get("taker_name"), 120)
            if not taker:
                raise ValueError("Nama pengambil (sesuai identitas) wajib diisi.")
            if _sr_photo_count(con, return_id, "serah_terima") == 0:
                raise ValueError("Unggah foto Bukti Serah Terima yang sudah ditandatangani.")
            if ret["sales_name"] and taker.strip().lower() != ret["sales_name"].strip().lower():
                if not _sr_is_admin(user):
                    raise ValueError(f"Pengambil berbeda dari rencana ({ret['sales_name']}). Minta admin yang mencatat serah terima.")
                notes.append(f"Pengambil berbeda dari rencana ({ret['sales_name']}), dilanjutkan admin")
            n = con.execute(
                """UPDATE supplier_returns SET handover_status='diserahkan', taker_name=?, taker_id_no=?, handed_at=?, officer=?
                   WHERE id=? AND handover_status='menunggu_ambil'""",
                (taker, _sr_text(data.get("taker_id_no"), 60), handed_at, officer, return_id)).rowcount
            notes.insert(0, f"Diserahkan ke {taker} (BST-{ret['return_number']})")
        if n != 1:
            raise ValueError("Barang retur ini sudah dikonfirmasi keluar.")
        for i in _sr_items(con, return_id):
            lot = _q_lot(con, i["quarantine_lot_id"]) if i["quarantine_lot_id"] else None
            if lot is None or lot["qty_left"] < i["qty"]:
                raise ValueError(f"“{i['product_name']}” di karantina tidak cukup ({lot['qty_left'] if lot else 0}). Admin perlu menyesuaikan qty.")
            _quarantine_take(con, lot, i["qty"], "retur_supplier", f"Keluar ke supplier, retur {ret['return_number']}", by)
        if data.get("note"):
            notes.append(str(data["note"]).strip())
        _sr_event(con, return_id, "keluar", "Barang keluar: " + "; ".join(notes), by)
    return get_supplier_return(return_id)


def mark_supplier_return_received(return_id: int, received_date: str | None, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["handover_status"] not in ("dikirim", "diserahkan"):
            raise ValueError("Hanya retur yang sudah keluar yang bisa ditandai diterima supplier.")
        when = received_date or shop_today().isoformat()
        con.execute("UPDATE supplier_returns SET handover_status='diterima_supplier', received_by_supplier_at=? WHERE id=?", (when, return_id))
        _sr_event(con, return_id, "diterima_supplier", f"Supplier mengonfirmasi barang diterima ({when})", by)
    return get_supplier_return(return_id)


def flag_supplier_return_problem(return_id: int, note: str | None, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] in ("selesai", "dibatalkan"):
            raise ValueError("Retur ini sudah selesai atau dibatalkan.")
        note = _sr_text(note)
        con.execute("UPDATE supplier_returns SET problem_note=? WHERE id=?", (note, return_id))
        _sr_event(con, return_id, "bermasalah" if note else "masalah_selesai",
                  f"Ditandai bermasalah: {note}" if note else "Tanda bermasalah dihapus", by)
    return get_supplier_return(return_id)


def record_supplier_return_decision(return_id: int, data: dict, user: dict) -> dict:
    """Admin records the supplier's answer per item (absolute values; may be revised as long as
    nothing that is already settled is taken back). Stock and money do not move here."""
    _sr_require_admin(user, "mencatat keputusan supplier")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "diproses":
            raise ValueError("Keputusan supplier hanya untuk retur yang sedang diproses.")
        items = {i["id"]: i for i in _sr_items(con, return_id)}
        lines = []
        for d in data.get("items") or []:
            i = items.get(int(d.get("item_id") or 0))
            if i is None:
                raise ValueError("Barang retur tidak ditemukan.")
            appr, rej = int(d.get("qty_approved") or 0), int(d.get("qty_rejected") or 0)
            if appr < 0 or rej < 0 or appr + rej > i["qty"]:
                raise ValueError(f"“{i['product_name']}”: disetujui + ditolak tidak boleh lebih dari {i['qty']}.")
            sett = d.get("settlement") or None
            if appr and sett not in SR_SETTLEMENTS:
                raise ValueError(f"“{i['product_name']}”: pilih penyelesaian untuk qty yang disetujui.")
            if i["settlement"] and sett != i["settlement"] and (i["qty_replaced"] or
                    (i["settlement"] != "ganti" and _sr_money(con, ret, list(items.values()))["settled_value"])):
                raise ValueError(f"“{i['product_name']}”: penyelesaian sudah berjalan, tidak bisa diganti.")
            if sett == "ganti" and appr < i["qty_replaced"]:
                raise ValueError(f"“{i['product_name']}”: sudah diterima pengganti {i['qty_replaced']}.")
            fate = d.get("reject_fate") or None
            if rej and fate not in SR_REJECT_FATES:
                raise ValueError(f"“{i['product_name']}”: pilih nasib barang yang ditolak.")
            if rej < i["qty_rejected_back"] or (i["qty_rejected_back"] and fate != "kirim_balik"):
                raise ValueError(f"“{i['product_name']}”: barang ditolak sudah kembali {i['qty_rejected_back']}.")
            if rej and not _sr_text(d.get("reject_reason")):
                raise ValueError(f"“{i['product_name']}”: tulis alasan supplier menolak.")
            i.update(qty_approved=appr, qty_rejected=rej, settlement=sett if appr else None,
                     reject_reason=_sr_text(d.get("reject_reason")) if rej else None, reject_fate=fate if rej else None)
            lines.append(i)
        ship = max(int(data.get("shipping_claim_approved") or 0), 0)
        if ship and ret["cost_bearer"] != "supplier":
            raise ValueError("Klaim ongkir hanya bila ongkir ditanggung supplier.")
        if ship > (ret["shipping_cost"] or ret["shipping_estimate"] or 0):
            raise ValueError("Klaim ongkir melebihi ongkir yang dicatat.")
        test = dict(ret, shipping_claim_approved=ship)
        money = _sr_money(con, test, list(items.values()))
        if money["claim_value"] < money["settled_value"]:
            raise ValueError(f"Nilai yang disetujui (Rp {_sr_rupiah(money['claim_value'])}) lebih kecil dari yang sudah dikredit/direfund "
                             f"(Rp {_sr_rupiah(money['settled_value'])}). Batalkan nota debitnya dulu.")
        for i in lines:
            con.execute("""UPDATE supplier_return_items SET qty_approved=?, qty_rejected=?, settlement=?, reject_reason=?, reject_fate=?
                           WHERE id=?""", (i["qty_approved"], i["qty_rejected"], i["settlement"], i["reject_reason"], i["reject_fate"], i["id"]))
        con.execute("UPDATE supplier_returns SET decided_at=?, decision_channel=?, rma_number=COALESCE(?, rma_number), shipping_claim_approved=? WHERE id=?",
                    (_sr_text(data.get("decided_at")) or shop_today().isoformat(), _sr_text(data.get("channel")),
                     _sr_text(data.get("rma_number")), ship, return_id))
        summary = "; ".join(
            f"{i['product_name']}: setuju {i['qty_approved']}" + (f" ({SR_SETTLEMENTS[i['settlement']].lower()})" if i["settlement"] else "")
            + (f", tolak {i['qty_rejected']} ({SR_REJECT_FATES[i['reject_fate']].lower()})" if i["qty_rejected"] else "") for i in lines)
        _sr_event(con, return_id, "keputusan", f"Keputusan supplier dicatat: {summary}" + (f"; klaim ongkir Rp {_sr_rupiah(ship)}" if ship else ""), by)
        _sr_refresh(con, return_id, by)
    return get_supplier_return(return_id)


def receive_supplier_return_replacement(return_id: int, data: dict, user: dict) -> dict:
    """Replacement goods come in as their own Barang Masuk (source retur_pengganti) at the original
    purchase price, so the average cost does not move and no new debt is created."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "diproses":
            raise ValueError("Retur ini tidak sedang diproses.")
        items = {i["id"]: i for i in _sr_items(con, return_id)}
        lines = []
        for d in data.get("items") or []:
            q = int(d.get("qty") or 0)
            if q <= 0:
                continue
            i = items.get(int(d.get("item_id") or 0))
            if i is None or i["settlement"] != "ganti":
                raise ValueError("Barang itu tidak disetujui untuk diganti.")
            left = i["qty_approved"] - i["qty_replaced"]
            if q > left:
                raise ValueError(f"“{i['product_name']}”: sisa pengganti {left}. Kelebihannya dicatat sebagai pembelian biasa.")
            if not i["master_item_id"]:
                raise ValueError(f"“{i['product_name']}” tidak tertaut ke Master Barang.")
            cond = d.get("condition") or "baik"
            if cond not in ("baik", *QUARANTINE_CONDITIONS):
                raise ValueError("Kondisi tidak dikenal.")
            lines.append((i, q, cond))
        if not lines:
            raise ValueError("Isi qty pengganti yang diterima.")
        ids = []
        for i, q, cond in lines:
            cur = con.execute("""INSERT INTO supplier_return_replacements (return_id, item_id, qty, condition, created_by, created_at)
                                 VALUES (?, ?, ?, ?, ?, ?)""", (return_id, i["id"], q, cond, by, _now()))
            ids.append(cur.lastrowid)
            con.execute("UPDATE supplier_return_items SET qty_replaced = qty_replaced + ? WHERE id=?", (q, i["id"]))
    # Barang Masuk is its own document with its own transaction; undo the claim if it fails.
    try:
        doc = create_inbound_document(
            ret["supplier_id"], data.get("received_date") or shop_today().isoformat(), None,
            f"Barang pengganti retur {ret['return_number']}",
            [{"master_item_id": i["master_item_id"], "qty_in": q, "cost_price": i["unit_cost"], "condition": cond} for i, q, cond in lines],
            {"source": "retur_pengganti", "manual_reason": f"Pengganti retur {ret['return_number']}",
             "surat_jalan_no": _sr_text(data.get("surat_jalan_no")), "officer": _sr_text(data.get("officer")) or by}, by)
        post_inbound_document(doc["id"], by)
    except Exception:
        with _conn() as con:
            for (i, q, _), rid in zip(lines, ids):
                con.execute("DELETE FROM supplier_return_replacements WHERE id=?", (rid,))
                con.execute("UPDATE supplier_return_items SET qty_replaced = qty_replaced - ? WHERE id=?", (q, i["id"]))
        raise
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"UPDATE supplier_return_replacements SET inbound_document_id=? WHERE id IN ({_placeholders(tuple(ids))})", (doc["id"], *ids))
        _sr_event(con, return_id, "pengganti", f"Barang pengganti diterima lewat Barang Masuk {doc['doc_number']}: "
                  + ", ".join(f"{i['product_name']} × {q}" + ("" if c == "baik" else f" ({QUARANTINE_CONDITIONS[c].lower()}, ke karantina)") for i, q, c in lines), by)
        _sr_refresh(con, return_id, by)
    return get_supplier_return(return_id)


def receive_supplier_return_rejected(return_id: int, data: dict, user: dict) -> dict:
    """Goods the supplier refused and sent back: physical stock again, straight into quarantine."""
    _sr_require_admin(user, "menerima kembali barang ditolak")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "diproses" or ret["handover_status"] not in SR_OUT:
            raise ValueError("Barang ditolak hanya bisa diterima kembali setelah barangnya keluar.")
        items = {i["id"]: i for i in _sr_items(con, return_id)}
        done = []
        for d in data.get("items") or []:
            q = int(d.get("qty") or 0)
            if q <= 0:
                continue
            i = items.get(int(d.get("item_id") or 0))
            if i is None or i["reject_fate"] != "kirim_balik":
                raise ValueError("Barang itu tidak dicatat dikirim balik oleh supplier.")
            left = i["qty_rejected"] - i["qty_rejected_back"]
            if q > left:
                raise ValueError(f"“{i['product_name']}”: sisa yang ditunggu kembali {left}.")
            cond = d.get("condition") or i["condition"]
            if cond not in QUARANTINE_CONDITIONS:
                raise ValueError("Kondisi harus Rusak, Tidak sesuai, atau Perlu dicek.")
            _quarantine_in(con, master_item_id=i["master_item_id"], product_name=i["product_name"], qty=q, condition=cond,
                           source_type="retur_ditolak", source_ref=ret["return_number"], source_id=str(return_id),
                           supplier_id=ret["supplier_id"], unit_cost=i["unit_cost"], candidate=False,
                           note=f"Ditolak supplier: {i['reject_reason'] or '-'}", by=by)
            con.execute("UPDATE supplier_return_items SET qty_rejected_back = qty_rejected_back + ? WHERE id=?", (q, i["id"]))
            done.append(f"{i['product_name']} × {q}")
        if not done:
            raise ValueError("Isi qty barang ditolak yang kembali.")
        _sr_event(con, return_id, "ditolak_kembali", "Barang ditolak kembali ke toko, masuk karantina: " + ", ".join(done), by)
        _sr_refresh(con, return_id, by)
    return get_supplier_return(return_id)


def _sr_allocate(con: sqlite3.Connection, cn: dict, invoice_id: int, amount: int, by: str | None) -> int:
    inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError("Faktur pembelian tidak ditemukan.")
    if inv["status"] != "posted" or inv["supplier_id"] != cn["supplier_id"]:
        raise ValueError("Nota debit hanya bisa dialokasikan ke Faktur Pembelian supplier yang sama dan sudah diposting.")
    room = inv["total"] - inv["amount_paid"]
    if amount > room:
        raise ValueError(f"Alokasi melebihi sisa tagihan faktur {inv['invoice_number']} (Rp {_sr_rupiah(room)}).")
    if amount <= 0:
        return 0
    con.execute("INSERT INTO supplier_credit_allocations (credit_note_id, invoice_id, amount, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (cn["id"], invoice_id, amount, by, _now()))
    con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid + ?, updated_at=? WHERE id=?", (amount, _now(), invoice_id))
    _pi_payment_row(con, invoice_id, amount, "kredit", by, reference_no=cn["cn_number"],
                    note=f"Nota debit retur {cn['cn_number']}")
    return amount


def _sr_cn_allocated(con: sqlite3.Connection, cn_id: int) -> int:
    return con.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM supplier_credit_allocations WHERE credit_note_id=?", (cn_id,)).fetchone()["n"]


def record_supplier_return_credit(return_id: int, data: dict, user: dict) -> dict:
    """Admin: refund money received for the approved claim. A debit note is no longer written here: 'kredit' opens
    (or reuses) a Draft in the Debit Note module, which is verified, posted and allocated there."""
    _sr_require_admin(user, "mencatat nota debit / refund")
    by = user.get("username")
    kind = data.get("kind")
    if kind not in ("kredit", "refund"):
        raise ValueError("Pilih Nota Debit atau Refund.")
    if kind == "kredit":
        from backend import credit_notes
        made = credit_notes.create_from_return(return_id, user, data.get("note"))
        return {**get_supplier_return(return_id), "credit_note_id": made["id"]}
    amount = int(data.get("amount") or 0)
    if amount <= 0:
        raise ValueError("Nilai harus lebih dari 0.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] != "diproses":
            raise ValueError("Retur ini tidak sedang diproses.")
        money = _sr_money(con, ret, _sr_items(con, return_id))
        if amount > money["claim_left"]:
            raise ValueError(f"Nilai melebihi sisa klaim yang disetujui (Rp {_sr_rupiah(money['claim_left'])}).")
        issued = _sr_text(data.get("issued_at")) or shop_today().isoformat()
        if kind == "refund" and data.get("method") not in ("transfer", "cash", "giro"):
            raise ValueError("Pilih metode refund.")
        cur = con.execute(
            """INSERT INTO supplier_credit_notes (cn_number, kind, supplier_id, return_id, amount, supplier_cn_no, issued_at, method,
                   reference_no, note, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_sr_number(con, "DN-SUP", "supplier_credit_notes", "cn_number") if kind == "kredit" else None, kind, ret["supplier_id"],
             return_id, amount, _sr_text(data.get("supplier_cn_no")), issued, data.get("method") if kind == "refund" else None,
             _sr_text(data.get("reference_no")), _sr_text(data.get("note")), by, _now()))
        cn = dict(con.execute("SELECT * FROM supplier_credit_notes WHERE id=?", (cur.lastrowid,)).fetchone())
        msg = f"Nota debit {cn['cn_number']} Rp {_sr_rupiah(amount)}" if kind == "kredit" else \
            f"Refund diterima Rp {_sr_rupiah(amount)} ({data.get('method')})"
        if kind == "kredit" and data.get("invoice_id"):
            inv = con.execute("SELECT total, amount_paid, invoice_number FROM purchase_invoices WHERE id=?", (int(data["invoice_id"]),)).fetchone()
            take = min(amount, (inv["total"] - inv["amount_paid"]) if inv else 0)
            got = _sr_allocate(con, cn, int(data["invoice_id"]), take, by)
            msg += f", dialokasikan Rp {_sr_rupiah(got)} ke {inv['invoice_number']}"
            if amount - got:
                msg += f", sisa Rp {_sr_rupiah(amount - got)} jadi saldo kredit supplier"
        elif kind == "kredit":
            msg += ", belum dialokasikan (saldo kredit supplier)"
        _sr_event(con, return_id, "keuangan", msg, by)
        _sr_refresh(con, return_id, by)
    return get_supplier_return(return_id)


def allocate_supplier_credit(cn_id: int, invoice_id: int, amount: int, user: dict) -> dict:
    _sr_require_admin(user, "mengalokasikan nota debit")
    from backend import credit_notes
    with _conn() as con:
        row = con.execute("SELECT kind, return_id FROM supplier_credit_notes WHERE id=?", (cn_id,)).fetchone()
    if row is not None and row["kind"] == "kredit":
        credit_notes.allocate(cn_id, {"allocations": [{"invoice_id": invoice_id, "amount": amount}],
                                      "note": "Alokasi dari Retur Supplier"}, user)
        return get_supplier_return(row["return_id"]) if row["return_id"] else {}
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = con.execute("SELECT * FROM supplier_credit_notes WHERE id=?", (cn_id,)).fetchone()
        if cn is None or cn["kind"] != "kredit" or cn["status"] != "aktif":
            raise ValueError("Nota debit tidak ditemukan atau sudah dibatalkan.")
        cn = dict(cn)
        left = cn["amount"] - _sr_cn_allocated(con, cn_id)
        if amount <= 0 or amount > left:
            raise ValueError(f"Saldo nota debit yang bisa dialokasikan Rp {_sr_rupiah(left)}.")
        _sr_allocate(con, cn, invoice_id, amount, by)
        inv = con.execute("SELECT invoice_number FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if cn["return_id"]:
            _sr_event(con, cn["return_id"], "keuangan", f"Nota debit {cn['cn_number']}: Rp {_sr_rupiah(amount)} dialokasikan ke {inv['invoice_number']}", by)
    return get_supplier_return(cn["return_id"]) if cn["return_id"] else {}


def cancel_supplier_credit(cn_id: int, reason: str, user: dict) -> dict:
    """Reverses a debit note or refund with counter rows; nothing is deleted."""
    _sr_require_admin(user, "membatalkan nota debit / refund")
    by = user.get("username")
    if not (reason or "").strip():
        raise ValueError("Alasan pembatalan wajib diisi.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        cn = con.execute("SELECT * FROM supplier_credit_notes WHERE id=?", (cn_id,)).fetchone()
        if cn is None or cn["status"] != "aktif":
            raise ValueError("Nota debit tidak ditemukan atau sudah dibatalkan.")
        cn = dict(cn)
        if cn["kind"] == "kredit":
            raise ValueError("Debit Note yang sudah diposting dibatalkan lewat Ajukan Reversal di menu Debit Note (disetujui Owner).")
        per_invoice: dict[int, int] = {}
        for a in con.execute("SELECT invoice_id, amount FROM supplier_credit_allocations WHERE credit_note_id=?", (cn_id,)):
            per_invoice[a["invoice_id"]] = per_invoice.get(a["invoice_id"], 0) + a["amount"]
        for inv_id, amt in per_invoice.items():
            if amt <= 0:
                continue
            inv = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (inv_id,)).fetchone()
            if inv["status"] != "posted":
                raise ValueError(f"Faktur {inv['invoice_number']} sudah tidak aktif; alokasinya tidak bisa dibalik otomatis.")
            con.execute("INSERT INTO supplier_credit_allocations (credit_note_id, invoice_id, amount, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                        (cn_id, inv_id, -amt, by, _now()))
            con.execute("UPDATE purchase_invoices SET amount_paid = amount_paid - ?, updated_at=? WHERE id=?", (amt, _now(), inv_id))
            _pi_payment_row(con, inv_id, -amt, "kredit", by, reference_no=cn["cn_number"], note=f"Nota debit {cn['cn_number']} dibatalkan")
        con.execute("UPDATE supplier_credit_notes SET status='dibatalkan', cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
                    (by, _now(), reason.strip(), cn_id))
        if cn["return_id"]:
            label = f"Nota debit {cn['cn_number']}" if cn["kind"] == "kredit" else f"Refund Rp {_sr_rupiah(cn['amount'])}"
            _sr_event(con, cn["return_id"], "keuangan", f"{label} dibatalkan: {reason.strip()}", by)
            _sr_refresh(con, cn["return_id"], by)
    return get_supplier_return(cn["return_id"]) if cn["return_id"] else {}


def _sr_progress(con: sqlite3.Connection, ret: dict, items: list[dict]) -> dict:
    money = _sr_money(con, ret, items)
    undecided = sum(i["qty"] - i["qty_approved"] - i["qty_rejected"] for i in items)
    repl_left = sum(i["qty_approved"] - i["qty_replaced"] for i in items if i["settlement"] == "ganti")
    back_left = sum(i["qty_rejected"] - i["qty_rejected_back"] for i in items if i["reject_fate"] == "kirim_balik")
    done = (ret["handover_status"] in SR_OUT and items and undecided == 0 and repl_left == 0 and back_left == 0
            and money["claim_left"] == 0)
    return {**money, "undecided_qty": undecided, "replacement_left": repl_left, "rejected_back_left": back_left, "complete": bool(done)}


def _sr_refresh(con: sqlite3.Connection, return_id: int, by: str | None) -> None:
    """Selesai is never set by hand: it follows from every unit having its final settlement."""
    ret = _sr_get(con, return_id)
    if ret["doc_status"] not in ("diproses", "selesai"):
        return
    done = _sr_progress(con, ret, _sr_items(con, return_id))["complete"]
    if done and ret["doc_status"] == "diproses":
        con.execute("UPDATE supplier_returns SET doc_status='selesai', completed_at=? WHERE id=?", (_now(), return_id))
        _sr_event(con, return_id, "selesai", "Semua barang sudah punya penyelesaian akhir; retur selesai", by)
    elif not done and ret["doc_status"] == "selesai":
        con.execute("UPDATE supplier_returns SET doc_status='diproses', completed_at=NULL WHERE id=?", (return_id,))
        _sr_event(con, return_id, "dibuka", "Penyelesaian dibatalkan; retur kembali diproses", by)


def _sr_next_step(ret: dict, prog: dict) -> str:
    st, ho = ret["doc_status"], ret["handover_status"]
    if st == "draft":
        return "Lengkapi barang, foto bukti, packing note, dan metode pengembalian, lalu ajukan."
    if st == "menunggu_persetujuan":
        return "Menunggu admin menyetujui retur."
    if st == "dibatalkan":
        return "Retur dibatalkan."
    if st == "selesai":
        return "Retur selesai."
    if ho == "siap":
        return "Kemas barang, unggah foto sesudah dikemas dan foto resi, lalu Konfirmasi Barang Keluar."
    if ho == "menunggu_ambil":
        return "Saat sales datang: cetak Bukti Serah Terima, minta tanda tangan, unggah fotonya, lalu Catat Serah Terima Sales."
    if prog["undecided_qty"]:
        return "Catat keputusan supplier untuk setiap barang."
    parts = []
    if prog["replacement_left"]:
        parts.append(f"terima barang pengganti ({prog['replacement_left']} lagi)")
    if prog["credit_pending"]:
        parts.append(f"verifikasi & posting Debit Note di menu Debit Note (Rp {_sr_rupiah(prog['credit_pending'])})")
    if prog["claim_left"] > prog["credit_pending"]:
        parts.append(f"buat Debit Note / catat refund (sisa Rp {_sr_rupiah(prog['claim_left'] - prog['credit_pending'])})")
    if prog["rejected_back_left"]:
        parts.append(f"terima kembali barang ditolak ({prog['rejected_back_left']} lagi)")
    return ("Selanjutnya: " + ", ".join(parts) + ".") if parts else "Menunggu penyelesaian."


def _sr_flags(ret: dict, prog: dict) -> list[str]:
    """Reminders from the design (late send, salesman no-show, no answer) next to the manual 'bermasalah' flag."""
    today = shop_today()
    out = []
    if ret["problem_note"]:
        out.append(f"Bermasalah: {ret['problem_note']}")
    try:
        if ret["handover_status"] == "siap" and ret["plan_date"] and (today - date.fromisoformat(ret["plan_date"][:10])).days >= 2:
            out.append("Terlambat kirim (lewat 2 hari dari rencana tanpa resi)")
        if ret["handover_status"] == "menunggu_ambil" and ret["pickup_at"] and (today - date.fromisoformat(ret["pickup_at"][:10])).days >= 1:
            out.append("Sales belum datang sesuai jadwal")
        if ret["handover_status"] in ("dikirim", "diserahkan") and ret["handed_at"] and (today - date.fromisoformat(ret["handed_at"][:10])).days >= 7:
            out.append("Supplier belum konfirmasi terima lebih dari 7 hari")
        if ret["handover_status"] in SR_OUT and prog["undecided_qty"] and ret["handed_at"] and (today - date.fromisoformat(ret["handed_at"][:10])).days >= 14:
            out.append("Keputusan supplier belum dicatat lebih dari 14 hari")
    except ValueError:
        pass
    return out


def get_supplier_return(return_id: int) -> dict:
    with _conn() as con:
        ret = _sr_get(con, return_id)
        items = _sr_items(con, return_id)
        for i in items:
            i["cost_value"] = i["qty"] * i["unit_cost"]
            i["reason_label"] = SR_REASONS.get(i["reason_code"] or "", "")
            i["condition_label"] = QUARANTINE_CONDITIONS.get(i["condition"], i["condition"])
        prog = _sr_progress(con, ret, items)
        sup = con.execute("SELECT * FROM suppliers WHERE id=?", (ret["supplier_id"],)).fetchone()
        photos = [dict(r) for r in con.execute(
            "SELECT id, item_id, stage, filename, mime, size, created_by, created_at FROM supplier_return_photos WHERE return_id=? ORDER BY id",
            (return_id,))]
        events = [dict(r) for r in con.execute("SELECT * FROM supplier_return_events WHERE return_id=? ORDER BY id DESC", (return_id,))]
        credits = []
        from backend import credit_notes
        for c in con.execute("SELECT * FROM supplier_credit_notes WHERE return_id=? ORDER BY id", (return_id,)):
            c = dict(c)
            c["allocations"] = [dict(a) for a in con.execute(
                """SELECT a.*, pi.invoice_number FROM supplier_credit_allocations a JOIN purchase_invoices pi ON pi.id = a.invoice_id
                   WHERE a.credit_note_id=? ORDER BY a.id""", (c["id"],))]
            c["allocated"] = sum(a["amount"] for a in c["allocations"])
            c["unallocated"] = credit_notes.available(con, c) if c["kind"] == "kredit" else 0
            c["status_label"] = credit_notes.CN_STATUS.get(c["status"], c["status"]) if c["kind"] == "kredit" else \
                ("Diterima" if c["status"] == "aktif" else "Dibatalkan")
            credits.append(c)
        replacements = [dict(r) for r in con.execute(
            """SELECT r.*, d.doc_number, i.product_name FROM supplier_return_replacements r
               JOIN supplier_return_items i ON i.id = r.item_id LEFT JOIN inbound_documents d ON d.id = r.inbound_document_id
               WHERE r.return_id=? ORDER BY r.id""", (return_id,))]
        open_invoices = [dict(r) for r in con.execute(
            """SELECT id, invoice_number, total, amount_paid, total - amount_paid AS outstanding, due_date FROM purchase_invoices
               WHERE supplier_id=? AND status='posted' AND total > amount_paid ORDER BY due_date, id""", (ret["supplier_id"],))]
        po = con.execute("SELECT po_number FROM supplier_pos WHERE id=?", (ret["supplier_po_id"],)).fetchone() if ret["supplier_po_id"] else None
        bm = con.execute("SELECT doc_number FROM inbound_documents WHERE id=?", (ret["inbound_document_id"],)).fetchone() if ret["inbound_document_id"] else None
        fk = con.execute("SELECT invoice_number FROM purchase_invoices WHERE id=?", (ret["purchase_invoice_id"],)).fetchone() if ret["purchase_invoice_id"] else None
        checklist = _sr_checklist(con, ret, items) if ret["doc_status"] in ("draft", "menunggu_persetujuan") else []
        ids = [i["master_item_id"] for i in items if i["master_item_id"]]
        reserved = _reserved_by_item(con, ids) if ids else {}
        for i in items:
            i["item_reserved"] = reserved.get(i["master_item_id"], 0)
        shortage = _sr_shortage(con, items) if ret["doc_status"] in ("draft", "menunggu_persetujuan") else []
    return {
        **ret, "packages": json.loads(ret["packages"] or "[]"), "version": ret["updated_at"],
        "supplier_name": sup["name"] if sup else None, "supplier_phone": sup["phone"] if sup else None,
        "supplier_address": sup["address"] if sup else None, "supplier_contact": sup["contact_person"] if sup else None,
        "supplier_po_number": po["po_number"] if po else None, "inbound_doc_number": bm["doc_number"] if bm else None,
        "purchase_invoice_number": fk["invoice_number"] if fk else None,
        "doc_status_label": SR_DOC_STATUS.get(ret["doc_status"]), "handover_label": SR_HANDOVER.get(ret["handover_status"]),
        "items": items, "photos": photos, "events": events, "credits": credits, "replacements": replacements,
        "open_invoices": open_invoices, "checklist": checklist, "shortage": shortage,
        **prog, "next_step": _sr_next_step(ret, prog), "flags": _sr_flags(ret, prog),
    }


def list_supplier_returns(tab: str = "semua", query: str | None = None, supplier_id: int | None = None, page: int = 1,
                          method: str | None = None, doc_status: str | None = None) -> dict:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT r.*, s.name AS supplier_name, sp.po_number AS supplier_po_number FROM supplier_returns r
               JOIN suppliers s ON s.id = r.supplier_id LEFT JOIN supplier_pos sp ON sp.id = r.supplier_po_id
               ORDER BY r.created_at DESC, r.id DESC""")]
        out = []
        kpi = {"total": len(rows), "selesai": sum(1 for r in rows if r["doc_status"] == "selesai"), "draft": 0, "menunggu_persetujuan": 0, "perlu_keluar": 0, "dalam_pengiriman": 0, "menunggu_keputusan": 0,
               "penyelesaian_berjalan": 0, "bermasalah": 0, "cost_value": 0}
        for r in rows:
            items = _sr_items(con, r["id"])
            prog = _sr_progress(con, r, items)
            flags = _sr_flags(r, prog)
            live = r["doc_status"] not in ("selesai", "dibatalkan")
            stage = None
            if r["doc_status"] == "draft":
                stage = "draft"
            elif r["doc_status"] == "menunggu_persetujuan":
                stage = "menunggu_persetujuan"
            elif r["doc_status"] == "diproses":
                if r["handover_status"] in ("siap", "menunggu_ambil"):
                    stage = "perlu_keluar"
                elif prog["undecided_qty"]:
                    stage = "dalam_pengiriman" if r["handover_status"] in ("dikirim", "diserahkan") else "menunggu_keputusan"
                else:
                    stage = "penyelesaian_berjalan"
            if stage:
                kpi[stage] += 1
            if live and flags:
                kpi["bermasalah"] += 1
            if live:
                kpi["cost_value"] += prog["cost_value"]
            action = live and (stage in ("menunggu_persetujuan", "perlu_keluar") or bool(flags))
            if tab == "perlu_tindakan" and not action:
                continue
            if tab == "aktif" and not live:
                continue
            if tab in ("selesai", "dibatalkan") and r["doc_status"] != tab:
                continue
            if supplier_id and r["supplier_id"] != supplier_id:
                continue
            if method and r["method"] != method:
                continue
            if doc_status and r["doc_status"] != doc_status:
                continue
            if query:
                hay = " ".join(str(x or "") for x in (r["return_number"], r["supplier_name"], r["awb"], r["rma_number"],
                                                        *[i["product_name"] for i in items], *[i.get("sku") for i in items])).lower()
                if query.strip().lower() not in hay:
                    continue
            decisions = {("setuju" if i["qty_approved"] == i["qty"] else "tolak" if i["qty_rejected"] == i["qty"]
                          else "menunggu" if not (i["qty_approved"] or i["qty_rejected"]) else "sebagian") for i in items}
            settlements = sorted({i["settlement"] for i in items if i["settlement"]})
            out.append({**{k: r[k] for k in ("id", "return_number", "return_date", "supplier_id", "supplier_name", "method", "doc_status",
                                             "handover_status", "awb", "created_by", "requested_settlement", "supplier_po_number")},
                        "settlements": settlements, "rejected_qty": sum(i["qty_rejected"] for i in items),
                        "doc_status_label": SR_DOC_STATUS.get(r["doc_status"]), "handover_label": SR_HANDOVER.get(r["handover_status"]),
                        "item_count": len(items), "qty": sum(i["qty"] for i in items), "products": ", ".join(i["product_name"] for i in items[:3]),
                        "decision": decisions.pop() if len(decisions) == 1 else ("sebagian" if decisions else "menunggu"),
                        "flags": flags, "stage": stage, "can_delete": supplier_return_deletable(con, r), **prog})
        total = len(out)
        page = max(page, 1)
        return {"items": out[(page - 1) * SR_PAGE_SIZE: page * SR_PAGE_SIZE], "total": total, "page": page,
                "page_size": SR_PAGE_SIZE, "kpi": kpi}


def _sr_upload_dir(return_id: int) -> Path:
    d = DB_PATH.parent / "uploads" / "supplier-returns" / str(return_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_supplier_return_photo(return_id: int, stage: str, item_id: int | None, filename: str, mime: str, data: bytes, user: dict) -> dict:
    if stage not in SR_PHOTO_STAGES:
        raise ValueError("Tahap foto tidak dikenal.")
    if mime not in SR_PHOTO_MIMES:
        raise ValueError("Foto harus JPG, PNG, atau WEBP.")
    if not data or len(data) > SR_PHOTO_MAX_BYTES:
        raise ValueError("Ukuran foto maksimal 6 MB.")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ret = _sr_get(con, return_id)
        if ret["doc_status"] in ("selesai", "dibatalkan"):
            raise ValueError("Retur ini sudah selesai atau dibatalkan.")
        if stage == "bukti" and ret["doc_status"] != "draft":
            raise ValueError("Foto bukti hanya bisa ditambah selagi Draft.")
        if stage in ("resi", "serah_terima", "sesudah_kemas") and ret["handover_status"] in SR_OUT:
            raise ValueError("Barang sudah keluar; foto tahap ini sudah terkunci.")
        name = f"{uuid.uuid4().hex}{SR_PHOTO_MIMES[mime]}"
        path = _sr_upload_dir(return_id) / name
        path.write_bytes(data)
        cur = con.execute(
            """INSERT INTO supplier_return_photos (return_id, item_id, stage, filename, mime, path, size, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (return_id, item_id, stage, (filename or name)[:120], mime, str(path), len(data), by, _now()))
        _sr_event(con, return_id, "foto", f"Foto {SR_PHOTO_STAGES[stage].lower()} ditambahkan", by)
        return {"id": cur.lastrowid}


def get_supplier_return_photo(photo_id: int) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM supplier_return_photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        raise LookupError("Foto tidak ditemukan.")
    return dict(row)


def delete_supplier_return_photo(photo_id: int, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ph = con.execute("SELECT * FROM supplier_return_photos WHERE id=?", (photo_id,)).fetchone()
        if ph is None:
            raise LookupError("Foto tidak ditemukan.")
        ret = _sr_get(con, ph["return_id"])
        locked = (ph["stage"] == "bukti" and ret["doc_status"] != "draft") or \
            (ph["stage"] != "bukti" and ret["handover_status"] in SR_OUT) or ret["doc_status"] in ("selesai", "dibatalkan")
        if locked:
            raise ValueError("Foto ini sudah menjadi bukti dan tidak bisa dihapus.")
        con.execute("DELETE FROM supplier_return_photos WHERE id=?", (photo_id,))
        _sr_event(con, ph["return_id"], "foto", f"Foto {SR_PHOTO_STAGES.get(ph['stage'], ph['stage']).lower()} dihapus", by)
    try:
        Path(ph["path"]).unlink()
    except OSError:
        pass
    return get_supplier_return(ph["return_id"])


def supplier_return_candidates(supplier_id: int | None = None) -> list[dict]:
    """Quarantine lots that can still go on a return (free units), with their supplier."""
    with _conn() as con:
        rows = [dict(r) for r in con.execute(_Q_SELECT + " WHERE q.qty_left > 0 ORDER BY q.retur_candidate DESC, q.created_at DESC")]
        out = []
        for lot in rows:
            lot = _q_row(con, lot)
            if lot["free_qty"] <= 0 or lot["source_type"] == "retur_supplier":
                continue
            if supplier_id and lot["supplier_id"] not in (None, supplier_id):
                continue
            out.append(lot)
        return out


def _sr_koli(packages: list) -> int:
    return sum(int(p.get("count") or 1) for p in packages or [])


def _sr_line(left: str, right: str, width: int = 80) -> str:
    return (left[: width - len(right) - 1].ljust(width - len(right)) + right)[:width]


def supplier_return_print_text(return_id: int, doc: str, user: dict) -> dict:
    """Plain fixed-width text (80 columns, 10 cpi) for the Epson LX-310: printed as text, never as an image."""
    r = get_supplier_return(return_id)
    show_value = _sr_is_admin(user)
    shop = "SINAR GALAKSI VARIASI MOBIL"
    rule = "-" * 80
    fmt_d = lambda s: (f"{s[8:10]}/{s[5:7]}/{s[0:4]}" if s and len(s) >= 10 else "")
    L = []
    if doc == "nota":
        L += [_sr_line(shop, "NOTA RETUR SUPPLIER"), _sr_line("", f"No.  : {r['return_number']}"),
              _sr_line(f"Kepada : {r['supplier_name'] or ''}", f"Tgl. : {fmt_d(r['return_date'])}"),
              _sr_line(f"         {(r['supplier_address'] or '')[:40]}", f"PO   : {r['supplier_po_number'] or '-'}"),
              _sr_line(f"Up.    : {r['supplier_contact'] or '-'}  {r['supplier_phone'] or ''}", f"BM   : {r['inbound_doc_number'] or '-'}"),
              _sr_line(f"RMA    : {r['rma_number'] or '-'}", f"Fakt.: {r['purchase_invoice_number'] or '-'}"),
              rule, f"{'No':<3}{'Nama Barang':<32}{'Qty':>4} {'Sat':<4} {'Alasan':<20}{'Nilai (Rp)':>15}"[:80], rule]
        for n, i in enumerate(r["items"], 1):
            val = _sr_rupiah(i["cost_value"]) if show_value else ""
            L.append(f"{n:>2} {i['product_name'][:31]:<32}{i['qty']:>4} {(i.get('unit') or '')[:4]:<4} {(i['reason_label'] or '')[:20]:<20}{val:>15}"[:80])
            extra = " - ".join(x for x in [f"SN {i['serial_no']}" if i["serial_no"] else "", i["reason_detail"] or "", i["test_result"] or ""] if x)
            while extra:
                L.append(("   " + extra[:77]))
                extra = extra[77:]
        L += [rule, _sr_line("", f"Total {sum(i['qty'] for i in r['items'])} unit  {(_sr_rupiah(r['cost_value']) if show_value else ''):>15}"),
              f"Diminta : {SR_SETTLEMENTS.get(r['requested_settlement'], '')}"[:80]]
        if r["method"] == "ekspedisi":
            L.append(f"Kirim   : {r['courier'] or '-'} {r['courier_service'] or ''}  Resi: {r['awb'] or '-'}"[:80])
        else:
            L.append(f"Diambil : {r['sales_name'] or '-'} {r['sales_phone'] or ''}"[:80])
        bearer = SR_COST_BEARERS.get(r["cost_bearer"] or "", "-").lower()
        L.append(f"Koli    : {_sr_koli(r['packages'])}   Ongkir Rp {_sr_rupiah(r['shipping_cost'] or r['shipping_estimate'])} ditanggung {bearer}"[:80])
        L += ["", "   Dibuat oleh,              Disetujui,                 Diterima supplier,", "", "",
              f"  ( {(r['created_by'] or '')[:16]:^16} )    ( {(r['approved_by'] or '')[:16]:^16} )      (......................)"]
    elif doc == "bst":
        L += [_sr_line(shop, "BUKTI SERAH TERIMA BARANG RETUR"), _sr_line("", f"No.  : BST-{r['return_number']}"),
              _sr_line("", f"Retur: {r['return_number']}"), f"Supplier : {r['supplier_name'] or ''}",
              f"Diambil  : {r['taker_name'] or r['sales_name'] or '.................'} {'(sales)' if r['method'] == 'sales' else ''}  HP {r['sales_phone'] or '-'}"[:80],
              f"Tanggal  : {fmt_d((r['handed_at'] or r['pickup_at'] or '')[:10]) or '..../..../......'}  Jam {(r['handed_at'] or '')[11:16] or '.....'}",
              rule, f"{'No':<3}{'Nama Barang':<32}{'Qty':>4} {'Sat':<4} Kemasan / Kelengkapan", rule]
        pack = "; ".join(f"Koli {k + 1}: " + ", ".join(p.get("materials") or []) for k, p in enumerate(r["packages"]))
        for n, i in enumerate(r["items"], 1):
            L.append(f"{n:>2} {i['product_name'][:31]:<32}{i['qty']:>4} {(i.get('unit') or '')[:4]:<4} {pack[:34] if n == 1 else ''}"[:80])
        L += [rule, "Barang di atas diterima dalam keadaan terkemas sesuai daftar.", "",
              "   Diserahkan (toko),                     Diterima (sales supplier),", "", "",
              "  (......................)                (......................)"]
    elif doc == "packing":
        L += [_sr_line(shop, "PACKING NOTE - RETUR SUPPLIER"), _sr_line(f"Kepada : {r['supplier_name'] or ''}", f"No.  : {r['return_number']}"),
              rule]
        for p in r["packages"]:
            L.append(f"Jumlah koli: {p.get('count') or 1}  {p.get('main') or p.get('profile') or ''}  {p.get('weight') or '-'} kg  {p.get('dims') or ''}"[:80])
            L.append(f"  Kondisi kemasan: {p.get('pack_condition') or '-'}"[:80])
            L.append(f"  Pelindung: {', '.join(p.get('materials') or []) or '-'}"[:80])
            L.append(f"  Isi      : {p.get('contents') or '-'}"[:80])
        L.append(rule)
        for line in (r["packing_text"] or "").splitlines():
            while line:
                L.append(line[:80])
                line = line[80:]
    else:
        raise ValueError("Dokumen cetak harus nota, bst, atau packing.")
    with _conn() as con:
        _sr_event(con, return_id, "cetak", f"Dicetak: {dict(nota='Nota Retur', bst='Bukti Serah Terima', packing='Packing Note')[doc]} (3 rangkap)",
                  user.get("username"))
    return {"text": "\n".join(L), "copies": ["Lembar 1 - Supplier", "Lembar 2 - Gudang", "Lembar 3 - Admin/Arsip"]}


# --- Service / Garansi ----------------------------------------------------------------------------
# A customer's unit that comes in for service, a warranty claim or a trip to the factory. The unit is
# the customer's property: it lives in a custody register (service_orders.location), never in
# stock_ledger. Stock moves only when the shop swaps the unit for one of its own (cara B: OUT from stock,
# the broken unit into Stok Karantina as 'service_klaim', then a Retur Supplier) or when a spare part
# from stock is billed on the Nota Jasa Service (a normal Kasir receipt). Status is kept on separate
# tracks: stage (where the document is), location (where the unit is), result, warranty and fee.
# Design: docs/service/SERVICE_DESAIN.md (owner decisions K-S1..K-S10, 19-10-2026).

SVC_STAGES = {"draft": "Draft", "diterima": "Diterima", "pengecekan": "Dalam Pengecekan", "menunggu_kirim": "Menunggu Dikirim",
              "di_supplier": "Di Supplier / Principal", "menunggu_customer": "Menunggu Keputusan Customer",
              "kembali": "Kembali ke Toko", "siap_diambil": "Siap Diambil", "selesai": "Selesai", "dibatalkan": "Dibatalkan"}
SVC_LOCATIONS = {"di_toko": "Di Toko", "di_ekspedisi": "Di Ekspedisi", "dibawa_sales": "Dibawa Sales",
                 "di_pabrik": "Di Pabrik / Principal", "perjalanan_balik": "Dalam Perjalanan Balik",
                 "kembali_di_toko": "Kembali di Toko", "diserahkan": "Diserahkan ke Customer"}
SVC_RESULTS = {"belum": "Belum Diputuskan", "diperbaiki": "Selesai Diperbaiki", "diganti": "Diganti Baru", "ditolak": "Ditolak",
               "apa_adanya": "Dikembalikan Apa Adanya", "dibatalkan": "Dibatalkan"}
SVC_WARRANTY = {"belum": "Belum Diverifikasi", "aktif": "Garansi Aktif", "habis": "Garansi Habis", "ditolak": "Garansi Ditolak"}
SVC_WARRANTY_BASIS = {"nota": "Nota toko", "kartu": "Kartu garansi pabrik", "tanpa": "Tanpa bukti"}
SVC_FEE = {"tanpa_biaya": "Tanpa Biaya", "menunggu_estimasi": "Menunggu Estimasi", "menunggu_persetujuan": "Menunggu Persetujuan Customer",
           "disetujui": "Disetujui Customer", "ditolak": "Ditolak Customer", "sudah_dibayar": "Sudah Dibayar"}
SVC_HANDLING = {"internal": "Pemeriksaan Internal", "kirim": "Kirim ke Pabrik / Principal", "sales": "Dibawa Sales Supplier",
                "menunggu": "Menunggu Keputusan"}
SVC_NO_INVOICE_REASONS = {"beli_luar": "Beli di tempat lain", "nota_hilang": "Nota hilang", "hadiah": "Hadiah", "lainnya": "Lainnya"}
SVC_SHIP_MODES = {"udara": "Udara", "laut": "Laut", "darat": "Darat / kurir"}
SVC_COST_BEARERS = {"toko": "Toko", "customer": "Customer", "supplier": "Supplier"}
SVC_DECISIONS = {"perbaiki_gratis": "Bisa Diperbaiki (gratis)", "perbaiki_biaya": "Bisa Diperbaiki dengan Biaya",
                 "ganti": "Ganti Unit Baru", "tolak": "Ditolak / Tidak Bisa Diperbaiki"}
SVC_COST_KINDS = {"service": "Jasa service", "sparepart": "Sparepart", "penggantian": "Penggantian / selisih harga",
                  "ongkir": "Ongkir", "lainnya": "Lainnya"}
SVC_STOP_KINDS = {"garansi_ditolak": "Garansi ditolak", "tidak_bisa": "Tidak bisa diperbaiki", "customer_batal": "Customer batal",
                  "tolak_biaya": "Customer tolak biaya", "tidak_rusak": "Tidak ditemukan kerusakan",
                  "ambil_kembali": "Diambil kembali tanpa perbaikan", "salah_input": "Salah input (draft)"}
SVC_STOP_SOURCES = {"pabrik": "Pabrik / supplier", "customer": "Customer", "toko": "Toko"}
SVC_MEDIA = {"wa": "WhatsApp", "telepon": "Telepon", "datang": "Datang ke toko"}
SVC_REPLACE_MODES = {"pabrik": "Pabrik mengirim unit pengganti", "stok_toko": "Tukar dari stok toko"}
SVC_PHOTO_STAGES = {
    "masuk": "Unit masuk", "sn": "Nomor seri", "kelengkapan": "Kelengkapan", "segel": "Segel / stiker garansi",
    "kartu_garansi": "Kartu garansi", "nota_manual": "Foto nota / bukti pembelian (manual)", "uji": "Bukti uji di toko", "kemasan": "Kemasan", "resi": "Resi / bukti serah ekspedisi",
    "isi_paket": "Isi paket", "serah_sales": "Foto serah terima sales", "ttd_sales": "Tanda tangan sales",
    "diagnosis": "Bukti diagnosis", "persetujuan": "Bukti persetujuan customer", "penolakan": "Bukti penolakan",
    "balik": "Bukti penerimaan balik", "serah_customer": "Foto serah ke customer", "ttd_customer": "Tanda tangan customer",
}
_SVC_PHOTO_LOCK_RECEIVE = ("masuk", "sn", "kelengkapan", "segel", "kartu_garansi", "nota_manual")
_SVC_PHOTO_LOCK_OUT = ("kemasan", "resi", "isi_paket", "serah_sales", "ttd_sales")
SVC_OPEN = ("diterima", "pengecekan", "menunggu_kirim", "di_supplier", "menunggu_customer", "kembali", "siap_diambil")
_SVC_AWAY = ("di_ekspedisi", "dibawa_sales", "di_pabrik", "perjalanan_balik")
_SVC_AT_SHOP = ("di_toko", "kembali_di_toko")
SVC_PAGE_SIZE = 50
SVC_TERMS_DEFAULT = ("Bawa tanda terima ini saat mengambil barang.\n"
                     "Barang yang tidak diambil 90 hari setelah dikabari bukan tanggung jawab toko.\n"
                     "Garansi mengikuti hasil pemeriksaan pabrik.")

# K-S1: starting values, editable in Master Service. Category codes cover both the dotted list
# (categories_seed.json) and the older K02-01 style, whichever the shop's Master Kategori has.
_SVC_SEED = [
    ("SV-01", "Head Unit Android", 12, ["02.01", "K02-01"], True, True, True,
     ["Head unit", "Kabel set", "Frame", "Kamera", "Antena GPS", "Remote", "Box / Dus", "Buku manual"],
     ["Mati total", "Layar gelap", "Tidak ada suara", "Touchscreen error", "Restart sendiri", "Kamera tidak tampil"], "14-21 hari"),
    ("SV-02", "Subwoofer Aktif", 12, ["02.08", "K02-08"], True, False, False,
     ["Unit subwoofer", "Remote / knob bass", "Kabel power", "Kabel RCA", "Box / Dus"],
     ["Mati total", "Suara pecah", "Dengung", "Remote tidak berfungsi"], "14-21 hari"),
    ("SV-03", "Speaker / Speaker Aktif", 6, ["02.03", "K02-03"], False, False, False,
     ["Speaker (pasang)", "Tweeter", "Crossover", "Kabel", "Box / Dus"], ["Tidak ada suara", "Suara pecah", "Sobek / rusak fisik"], "7-14 hari"),
    ("SV-04", "Kamera", 6, ["03.02", "03.01", "03.05", "K03-02"], False, False, False,
     ["Kamera", "Kabel video", "Kabel power", "Bracket", "Box / Dus"], ["Gambar tidak tampil", "Gambar buram", "Berembun"], "7-14 hari"),
    ("SV-05", "Power Amplifier", 12, ["02.06", "K02-06"], True, False, False,
     ["Unit amplifier", "Kabel power", "Kabel RCA", "Remote bass", "Box / Dus"], ["Mati total", "Protect", "Panas berlebih", "Suara pecah"], "14-21 hari"),
    ("SV-06", "Audio Processor / DSP", 12, ["02.05", "K02-05"], True, False, False,
     ["Unit DSP", "Harness / kabel", "Remote / kontrol", "Kabel USB", "Box / Dus"], ["Mati total", "Tidak terbaca software", "Channel mati"], "14-21 hari"),
    ("SV-07", "Elektronik Lainnya", 3, ["05.03", "05.09", "K05-03"], False, False, False,
     ["Unit", "Kabel", "Box / Dus"], ["Mati total", "Tidak berfungsi normal"], "7-21 hari"),
]


def _svc_init(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS service_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            warranty_months INTEGER NOT NULL DEFAULT 0,
            after_warranty_days INTEGER NOT NULL DEFAULT 30,
            est_days TEXT,
            require_serial INTEGER NOT NULL DEFAULT 0,
            require_photo INTEGER NOT NULL DEFAULT 1,
            record_serial_at_sale INTEGER NOT NULL DEFAULT 0,
            ask_reset INTEGER NOT NULL DEFAULT 0,
            terms_text TEXT,
            warranty_terms TEXT,
            checklist TEXT NOT NULL DEFAULT '[]',
            complaints TEXT NOT NULL DEFAULT '[]',
            default_supplier_id INTEGER REFERENCES suppliers(id),
            status TEXT NOT NULL DEFAULT 'aktif',
            created_by TEXT, created_at TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS service_profile_categories (
            profile_id INTEGER NOT NULL REFERENCES service_profiles(id),
            category_id INTEGER NOT NULL REFERENCES categories(id),
            PRIMARY KEY (profile_id, category_id)
        );
        CREATE TABLE IF NOT EXISTS service_profile_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES service_profiles(id),
            brand_id INTEGER REFERENCES brands(id),
            master_item_id INTEGER REFERENCES master_items(id),
            warranty_months INTEGER,
            supplier_id INTEGER REFERENCES suppliers(id),
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS service_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_no TEXT UNIQUE,
            stage TEXT NOT NULL DEFAULT 'draft',
            location TEXT NOT NULL DEFAULT 'di_toko',
            result TEXT NOT NULL DEFAULT 'belum',
            warranty_status TEXT NOT NULL DEFAULT 'belum',
            warranty_basis TEXT,
            warranty_months INTEGER,
            warranty_until TEXT,
            warranty_note TEXT,
            fee_status TEXT NOT NULL DEFAULT 'menunggu_estimasi',
            customer_id INTEGER REFERENCES customers(id),
            customer_name TEXT, customer_phone TEXT, plate TEXT, vehicle TEXT,
            receipt_id TEXT, receipt_item_id INTEGER, receipt_no TEXT, purchase_date TEXT, no_invoice_reason TEXT,
            profile_id INTEGER REFERENCES service_profiles(id),
            master_item_id INTEGER, brand_name TEXT, product_name TEXT, model TEXT, serial_no TEXT, serial_new TEXT,
            qty INTEGER NOT NULL DEFAULT 1, unit TEXT,
            condition_in TEXT, complaint TEXT, reset_consent INTEGER, lock_removed INTEGER,
            handling TEXT, supplier_id INTEGER REFERENCES suppliers(id), pic TEXT, tech_note TEXT,
            est_days TEXT, terms_text TEXT, after_warranty_days INTEGER,
            ready_at TEXT, qc_note TEXT,
            picked_up_at TEXT, picked_up_by TEXT, picker_name TEXT, picker_phone TEXT, picker_is_customer INTEGER,
            condition_out TEXT, handover_note TEXT, after_warranty_until TEXT, unpaid_reason TEXT,
            fee_receipt_id TEXT, rma_no TEXT,
            stop_kind TEXT, stop_source TEXT, stop_reason TEXT, stop_at TEXT,
            received_at TEXT, received_by TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created_by TEXT, created_at TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_orders_customer ON service_orders(customer_id);
        CREATE INDEX IF NOT EXISTS idx_service_orders_item ON service_orders(receipt_item_id);
        CREATE INDEX IF NOT EXISTS idx_service_orders_serial ON service_orders(serial_no);
        CREATE TABLE IF NOT EXISTS service_accessories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            name TEXT NOT NULL,
            brought INTEGER NOT NULL DEFAULT 1,
            sent INTEGER NOT NULL DEFAULT 0,
            returned INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_service_acc_order ON service_accessories(order_id);
        CREATE TABLE IF NOT EXISTS service_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            stage TEXT NOT NULL,
            filename TEXT NOT NULL, mime TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_photos_order ON service_photos(order_id);
        CREATE TABLE IF NOT EXISTS service_shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            doc_no TEXT UNIQUE,
            direction TEXT NOT NULL DEFAULT 'keluar',
            channel TEXT NOT NULL DEFAULT 'ekspedisi',
            mode TEXT, courier TEXT, awb TEXT, koli INTEGER, weight TEXT,
            packing TEXT, ship_cost INTEGER NOT NULL DEFAULT 0, cost_bearer TEXT,
            sales_name TEXT, sales_phone TEXT, scheduled_at TEXT,
            handed_at TEXT, officer TEXT, received_at TEXT, note TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_ship_order ON service_shipments(order_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_service_ship_awb ON service_shipments(awb) WHERE awb IS NOT NULL AND status <> 'draft';
        CREATE TABLE IF NOT EXISTS service_diagnoses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            rma_no TEXT, diagnosed_at TEXT, examiner TEXT,
            factory_warranty TEXT, reject_reason TEXT,
            decision TEXT NOT NULL, description TEXT,
            is_current INTEGER NOT NULL DEFAULT 1,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_diag_order ON service_diagnoses(order_id);
        CREATE TABLE IF NOT EXISTS service_cost_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            diagnosis_id INTEGER,
            kind TEXT NOT NULL,
            description TEXT,
            master_item_id INTEGER,
            qty INTEGER NOT NULL DEFAULT 1,
            factory_cost INTEGER NOT NULL DEFAULT 0,
            customer_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'usulan',
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_cost_order ON service_cost_lines(order_id);
        CREATE TABLE IF NOT EXISTS service_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            status TEXT NOT NULL,
            contacted_at TEXT, medium TEXT, note TEXT, deadline TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_appr_order ON service_approvals(order_id);
        CREATE TABLE IF NOT EXISTS service_replacements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'menunggu',
            old_serial TEXT, new_serial TEXT,
            master_item_id INTEGER, product_name TEXT,
            return_awb TEXT, received_at TEXT, qc_note TEXT,
            ledger_id INTEGER, quarantine_lot_id INTEGER,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_repl_order ON service_replacements(order_id);
        CREATE TABLE IF NOT EXISTS service_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES service_orders(id),
            kind TEXT NOT NULL, note TEXT, by_user TEXT, at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_events_order ON service_events(order_id);
    """)
    rc = {r["name"] for r in con.execute("PRAGMA table_info(receipts)").fetchall()}
    if "service_order_id" not in rc:
        con.execute("ALTER TABLE receipts ADD COLUMN service_order_id INTEGER")
    ric = {r["name"] for r in con.execute("PRAGMA table_info(receipt_items)").fetchall()}
    if "serial_no" not in ric:
        con.execute("ALTER TABLE receipt_items ADD COLUMN serial_no TEXT")
    # Three separate warranties: product/sparepart (warranty_months), installation work, and the result of a repair
    # (after_warranty_days). 0 / empty means "not set": the status then reads Belum Diverifikasi, never Habis.
    for table, cols in (
        ("service_profiles", (("install_warranty_months", "INTEGER NOT NULL DEFAULT 0"),)),
        ("service_profile_overrides", (("install_warranty_months", "INTEGER"), ("after_warranty_days", "INTEGER"))),
        ("service_orders", (("install_warranty_months", "INTEGER"), ("install_warranty_until", "TEXT"), ("install_warranty_status", "TEXT"))),
        # Swap from shop stock in three steps (blueprint garansi 22-10, owner 23-10): staff request, admin approves
        # (Owner when the warranty is not active), then staff hands the unit out of stock.
        ("service_replacements", (("request_note", "TEXT"), ("requested_by", "TEXT"), ("requested_at", "TEXT"),
                                  ("decided_by", "TEXT"), ("decided_at", "TEXT"), ("decision_note", "TEXT"),
                                  ("owner_override", "INTEGER NOT NULL DEFAULT 0"), ("price_diff", "INTEGER NOT NULL DEFAULT 0"),
                                  ("diff_cost", "INTEGER NOT NULL DEFAULT 0"), ("diff_medium", "TEXT"), ("diff_note", "TEXT"),
                                  ("posted_by", "TEXT"))),
    ):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    if not con.execute("SELECT 1 FROM app_migrations WHERE name='service_profiles_seed'").fetchone():
        now = _now()
        for code, name, months, cats, serial, sale_sn, reset, checklist, complaints, est in _SVC_SEED:
            if con.execute("SELECT 1 FROM service_profiles WHERE code=?", (code,)).fetchone():
                continue
            pid = con.execute(
                """INSERT INTO service_profiles (code, name, warranty_months, after_warranty_days, est_days, require_serial,
                       require_photo, record_serial_at_sale, ask_reset, terms_text, warranty_terms, checklist, complaints,
                       created_by, created_at, updated_at)
                   VALUES (?, ?, ?, 30, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'sistem', ?, ?)""",
                (code, name, months, est, 1 if serial else 0, 1 if sale_sn else 0, 1 if reset else 0, SVC_TERMS_DEFAULT,
                 "Segel & stiker utuh, tidak kena air, tidak dibongkar pihak lain.", json.dumps(checklist), json.dumps(complaints),
                 now, now)).lastrowid
            for c in cats:
                row = con.execute("SELECT id FROM categories WHERE code=?", (c,)).fetchone()
                if row and not con.execute("SELECT 1 FROM service_profile_categories WHERE category_id=?", (row["id"],)).fetchone():
                    con.execute("INSERT INTO service_profile_categories (profile_id, category_id) VALUES (?, ?)", (pid, row["id"]))
        con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('service_profiles_seed', ?)", (now,))
    # Master Kategori can be replaced wholesale (scripts/replace_categories.py deletes every category). Links to
    # categories that no longer exist are dropped; a starter profile left with none is linked again by its codes.
    lost = {r["profile_id"] for r in con.execute(
        "SELECT profile_id FROM service_profile_categories WHERE category_id NOT IN (SELECT id FROM categories)")}
    if lost:
        con.execute("DELETE FROM service_profile_categories WHERE category_id NOT IN (SELECT id FROM categories)")
        seed_codes = {code: cats for code, _n, _m, cats, *_ in _SVC_SEED}
        for p in con.execute(f"SELECT id, code FROM service_profiles WHERE id IN ({_placeholders(tuple(lost))})", tuple(lost)).fetchall():
            if con.execute("SELECT 1 FROM service_profile_categories WHERE profile_id=?", (p["id"],)).fetchone():
                continue
            for c in seed_codes.get(p["code"], []):
                row = con.execute("SELECT id FROM categories WHERE code=?", (c,)).fetchone()
                if row and not con.execute("SELECT 1 FROM service_profile_categories WHERE category_id=?", (row["id"],)).fetchone():
                    con.execute("INSERT INTO service_profile_categories (profile_id, category_id) VALUES (?, ?)", (p["id"], row["id"]))


QUARANTINE_SOURCES.setdefault("service_klaim", "Unit klaim service (ditukar dari stok)")


# ---- helpers ----

def _svc_event(con: sqlite3.Connection, order_id: int, kind: str, note: str | None, by: str | None) -> None:
    con.execute("INSERT INTO service_events (order_id, kind, note, by_user, at) VALUES (?, ?, ?, ?, ?)",
                (order_id, kind, note, by, _now()))
    con.execute("UPDATE service_orders SET updated_at=?, updated_by=?, version=version+1 WHERE id=?", (_now(), by, order_id))


def _svc_get(con: sqlite3.Connection, order_id: int) -> dict:
    row = con.execute("SELECT * FROM service_orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        raise LookupError("Dokumen service tidak ditemukan.")
    return dict(row)


def _svc_int(v, what: str, lo: int = 0, hi: int = 10**12) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{what} harus berupa angka.")
    if n < lo or n > hi:
        raise ValueError(f"{what} harus antara {lo} dan {hi}.")
    return n


def _svc_add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = (date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def _svc_age(start: str | None, end: date | None = None) -> str:
    if not start:
        return ""
    a, b = date.fromisoformat(start[:10]), end or shop_today()
    months = (b.year - a.year) * 12 + b.month - a.month - (1 if b.day < a.day else 0)
    days = (b - _svc_add_months(a, months)).days if months >= 0 else (b - a).days
    return (f"{months} bulan " if months > 0 else "") + f"{max(days, 0)} hari"


def _svc_local_now() -> str:
    """Wall-clock 'YYYY-MM-DD HH:MM' for the hand-entered times (handed, contacted, picked up), which the
    forms send in shop time; a default must match them, not the UTC created_at."""
    return (datetime.now(timezone.utc) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)).strftime("%Y-%m-%d %H:%M")


def _svc_local_date(iso: str | None) -> str | None:
    return _shop_local_date(iso) if iso else None


def _svc_profile_row(con: sqlite3.Connection, profile_id: int) -> dict:
    row = con.execute("SELECT * FROM service_profiles WHERE id=?", (profile_id,)).fetchone()
    if row is None:
        raise LookupError("Jenis service tidak ditemukan.")
    p = dict(row)
    p["checklist"] = json.loads(p["checklist"] or "[]")
    p["complaints"] = json.loads(p["complaints"] or "[]")
    return p


def _svc_profile_for(con: sqlite3.Connection, master_item_id: int | None, profile_id: int | None = None) -> dict | None:
    """The service rule for a product: an override for the item, else for its brand, else the profile covering its
    category (or the category's parent). Returns the profile with the effective warranty and destination."""
    mi = con.execute("SELECT id, brand_id, category_id, supplier_id FROM master_items WHERE id=?", (master_item_id,)).fetchone() \
        if master_item_id else None
    over = None
    if mi:
        over = con.execute("""SELECT o.* FROM service_profile_overrides o JOIN service_profiles p ON p.id=o.profile_id
                              WHERE o.master_item_id=? AND p.status='aktif'""", (mi["id"],)).fetchone()
        if over is None and mi["brand_id"]:
            q = "SELECT o.* FROM service_profile_overrides o JOIN service_profiles p ON p.id=o.profile_id WHERE o.brand_id=? AND p.status='aktif'"
            args = [mi["brand_id"]]
            if profile_id:
                q += " AND o.profile_id=?"
                args.append(profile_id)
            else:
                q += """ AND o.profile_id IN (SELECT pc.profile_id FROM service_profile_categories pc JOIN categories c ON c.id=pc.category_id
                                              WHERE c.id=? OR c.id=(SELECT parent_id FROM categories WHERE id=?))"""
                args += [mi["category_id"] or 0, mi["category_id"] or 0]
            over = con.execute(q, args).fetchone()
    pid = profile_id or (over["profile_id"] if over else None)
    if pid is None and mi and mi["category_id"]:
        row = con.execute("""SELECT pc.profile_id FROM service_profile_categories pc JOIN service_profiles p ON p.id=pc.profile_id
                             WHERE p.status='aktif' AND pc.category_id IN (?, (SELECT parent_id FROM categories WHERE id=?))
                             ORDER BY pc.category_id=? DESC LIMIT 1""", (mi["category_id"], mi["category_id"], mi["category_id"])).fetchone()
        pid = row["profile_id"] if row else None
    if pid is None:
        return None
    p = _svc_profile_row(con, pid)
    p["effective_months"] = over["warranty_months"] if over and over["warranty_months"] is not None else p["warranty_months"]
    p["effective_supplier_id"] = (over["supplier_id"] if over and over["supplier_id"] else None) or p["default_supplier_id"] \
        or (mi["supplier_id"] if mi else None)
    p["effective_install_months"] = over["install_warranty_months"] if over and over["install_warranty_months"] is not None else p["install_warranty_months"]
    p["effective_after_days"] = over["after_warranty_days"] if over and over["after_warranty_days"] is not None else p["after_warranty_days"]
    p["override_id"] = over["id"] if over else None
    return p


def _svc_install_warranty(purchase_date: str | None, months: int | None, on: date | None = None) -> tuple[str | None, str | None]:
    """Warranty on the shop's installation work, counted from the nota date. (None, None) when the shop gives none."""
    if not months or not purchase_date:
        return None, None
    until = _svc_add_months(date.fromisoformat(purchase_date[:10]), months).isoformat()
    return ("aktif" if (on or shop_today()).isoformat() <= until else "habis"), until


def _svc_warranty(purchase_date: str | None, months: int | None, warranty_date: str | None, on: date | None = None) -> tuple[str, str | None]:
    """(status, until) from the purchase date: aktif while today ≤ until. A per-line warranty date from old
    receipts wins over the profile's months."""
    if not purchase_date:
        return "belum", None
    until = warranty_date[:10] if warranty_date else (
        _svc_add_months(date.fromisoformat(purchase_date[:10]), months).isoformat() if months else None)
    if until is None:
        return "belum", None  # no warranty rule or date on record: unverified, not expired
    return ("aktif" if (on or shop_today()).isoformat() <= until else "habis"), until


def _svc_lines(con: sqlite3.Connection, order_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        """SELECT l.*, mi.sku, mi.name AS item_name FROM service_cost_lines l LEFT JOIN master_items mi ON mi.id=l.master_item_id
           WHERE l.order_id=? ORDER BY l.id""", (order_id,))]


def _svc_money(con: sqlite3.Connection, order: dict) -> dict:
    lines = _svc_lines(con, order["id"])
    billable = sum(l["customer_price"] * l["qty"] for l in lines if l["status"] in ("disetujui", "otomatis"))
    proposed = sum(l["customer_price"] * l["qty"] for l in lines if l["status"] == "usulan")
    factory = sum(l["factory_cost"] * l["qty"] for l in lines if l["status"] in ("disetujui", "otomatis", "usulan"))
    rc = None
    if order["fee_receipt_id"]:
        rc = con.execute("SELECT id, receipt_no, status, total, amount_paid FROM receipts WHERE id=?", (order["fee_receipt_id"],)).fetchone()
        rc = dict(rc) if rc else None
        if rc and rc["status"] == "void":
            rc = None
    paid = rc["amount_paid"] if rc else 0
    return {"billable": billable, "proposed": proposed, "factory_total": factory, "paid": paid,
            "remaining": max(billable - paid, 0), "fee_receipt": rc, "lines": lines}


def _svc_refresh(con: sqlite3.Connection, order_id: int) -> None:
    """Recomputes the fee status from the cost lines, the last approval and the Nota Jasa."""
    o = _svc_get(con, order_id)
    m = _svc_money(con, o)
    lines = m["lines"]
    last_appr = con.execute("SELECT status FROM service_approvals WHERE order_id=? AND status<>'menunggu' ORDER BY id DESC LIMIT 1",
                            (order_id,)).fetchone()
    has_diag = con.execute("SELECT 1 FROM service_diagnoses WHERE order_id=?", (order_id,)).fetchone() is not None
    if m["billable"] > 0 and m["fee_receipt"] and m["fee_receipt"]["status"] == "done" and not any(l["status"] == "usulan" for l in lines):
        fee = "sudah_dibayar"
    elif any(l["status"] == "usulan" for l in lines):
        fee = "menunggu_persetujuan"
    elif m["billable"] > 0:
        fee = "disetujui"
    elif last_appr and last_appr["status"] == "tolak":
        fee = "ditolak"
    elif has_diag or o["result"] != "belum" or o["stage"] in ("selesai", "dibatalkan", "siap_diambil"):
        fee = "tanpa_biaya"
    else:
        fee = "menunggu_estimasi"
    if fee != o["fee_status"]:
        con.execute("UPDATE service_orders SET fee_status=? WHERE id=?", (fee, order_id))


def _svc_require_admin(user: dict, what: str) -> None:
    if (user or {}).get("role") != "admin":
        raise PermissionError(f"Hanya admin yang bisa {what}.")


def _svc_photo_count(con: sqlite3.Connection, order_id: int, *stages: str) -> int:
    return con.execute(f"SELECT COUNT(*) AS n FROM service_photos WHERE order_id=? AND stage IN ({_placeholders(stages)})",
                       (order_id, *stages)).fetchone()["n"]


def _svc_check_version(o: dict, data: dict) -> None:
    v = data.get("version")
    if v is not None and int(v) != o["version"]:
        raise ValueError("Dokumen ini baru saja diubah orang lain. Muat ulang lalu ulangi.")


def _svc_check_line_qty(con, order_id: int, item_id: int, qty: int) -> None:
    """GAR-BR-003: the units claimed on one nota line, across every open service, never exceed what was
    bought minus what was returned."""
    line = con.execute("SELECT quantity, returned_qty FROM receipt_items WHERE id=?", (item_id,)).fetchone()
    if line is None:
        return
    eligible = line["quantity"] - (line["returned_qty"] or 0)
    open_rows = con.execute(f"""SELECT service_no, qty FROM service_orders WHERE receipt_item_id=? AND id<>?
                                AND stage IN ({_placeholders(SVC_OPEN)})""", (item_id, order_id, *SVC_OPEN)).fetchall()
    used = sum(r["qty"] for r in open_rows)
    if used + qty > eligible:
        left = max(eligible - used, 0)
        docs = ", ".join(r["service_no"] for r in open_rows if r["service_no"])
        raise ValueError(f"Baris nota ini {eligible} unit" + (f", {used} sedang diklaim di {docs}" if used else "")
                         + f". Sisa yang bisa diklaim: {left}.")


def _svc_swap(con, order_id: int) -> dict | None:
    """The open swap-from-stock request (diajukan / disetujui), with the replacement's stock on hand."""
    r = con.execute("""SELECT r.*, mi.stock_qty, mi.name AS item_name, mi.sku FROM service_replacements r
                       LEFT JOIN master_items mi ON mi.id=r.master_item_id
                       WHERE r.order_id=? AND r.mode='stok_toko' AND r.status IN ('diajukan','disetujui') ORDER BY r.id DESC LIMIT 1""",
                    (order_id,)).fetchone()
    return dict(r) if r else None


SVC_SUPPLIER_TRACK = {"belum_diajukan": "Belum diajukan ke supplier", "draft": "Retur Supplier draft",
                      "menunggu_kirim": "Menunggu dikirim ke supplier", "menunggu_keputusan": "Menunggu keputusan supplier",
                      "diputuskan": "Supplier sudah memutuskan", "selesai": "Selesai", "dihapus": "Dihapus dari karantina"}
_SVC_SUPPLIER_FINAL = ("selesai", "dihapus")


def _svc_supplier_track(con, order_id: int) -> dict | None:
    """Blueprint §7: the supplier side of a swap from stock, tracked apart from the customer. The broken unit
    sits in Stok Karantina (service_klaim) and goes on a Retur Supplier; None when there was no swap."""
    lots = con.execute("SELECT id, qty_left, created_at FROM quarantine_lots WHERE source_type='service_klaim' AND source_id=?",
                       (str(order_id),)).fetchall()
    if not lots:
        return None
    ids = tuple(l["id"] for l in lots)
    rts = con.execute(f"""SELECT r.id, r.return_number, r.doc_status, r.handover_status,
                                 SUM(CASE WHEN i.settlement IS NOT NULL OR i.qty_rejected>0 THEN 1 ELSE 0 END) AS decided
                          FROM supplier_return_items i JOIN supplier_returns r ON r.id=i.return_id
                          WHERE i.source_lot_id IN ({_placeholders(ids)}) AND r.doc_status<>'dibatalkan'
                          GROUP BY r.id ORDER BY r.id DESC LIMIT 1""", ids).fetchone()
    if rts is None:
        code = "belum_diajukan" if any(l["qty_left"] for l in lots) else "dihapus"
    elif rts["doc_status"] == "selesai":
        code = "selesai"
    elif rts["doc_status"] in ("draft", "menunggu_persetujuan"):
        code = "draft"
    elif rts["decided"]:
        code = "diputuskan"
    elif rts["handover_status"] in SR_OUT:
        code = "menunggu_keputusan"
    else:
        code = "menunggu_kirim"
    since = _svc_local_date(min(l["created_at"] for l in lots))
    return {"code": code, "label": SVC_SUPPLIER_TRACK[code], "final": code in _SVC_SUPPLIER_FINAL,
            "return_id": rts["id"] if rts else None, "return_number": rts["return_number"] if rts else None,
            "since": since, "age_days": (shop_today() - date.fromisoformat(since)).days if since else None}


# ---- Master Service ----

def list_service_profiles(include_inactive: bool = False) -> list[dict]:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT p.*, s.name AS supplier_name FROM service_profiles p LEFT JOIN suppliers s ON s.id=p.default_supplier_id "
            + ("" if include_inactive else "WHERE p.status='aktif' ") + "ORDER BY p.code")]
        for p in rows:
            p["checklist"] = json.loads(p["checklist"] or "[]")
            p["complaints"] = json.loads(p["complaints"] or "[]")
            p["categories"] = [dict(r) for r in con.execute(
                """SELECT c.id, c.code, c.name FROM service_profile_categories pc JOIN categories c ON c.id=pc.category_id
                   WHERE pc.profile_id=? ORDER BY c.code""", (p["id"],))]
            ids = [c["id"] for c in p["categories"]]
            p["item_count"] = con.execute(
                f"""SELECT COUNT(*) AS n FROM master_items WHERE is_active=1 AND (category_id IN ({_placeholders(tuple(ids)) if ids else 'NULL'})
                        OR category_id IN (SELECT id FROM categories WHERE parent_id IN ({_placeholders(tuple(ids)) if ids else 'NULL'}))
                        OR id IN (SELECT master_item_id FROM service_profile_overrides WHERE profile_id=? AND master_item_id IS NOT NULL))""",
                (*ids, *ids, p["id"])).fetchone()["n"]
            p["overrides"] = [dict(r) for r in con.execute(
                """SELECT o.*, b.name AS brand_name, mi.name AS item_name, mi.sku, s.name AS supplier_name
                   FROM service_profile_overrides o LEFT JOIN brands b ON b.id=o.brand_id
                   LEFT JOIN master_items mi ON mi.id=o.master_item_id LEFT JOIN suppliers s ON s.id=o.supplier_id
                   WHERE o.profile_id=? ORDER BY o.master_item_id IS NOT NULL, b.name, mi.name""", (p["id"],))]
        return rows


def save_service_profile(profile_id: int | None, data: dict, user: dict) -> dict:
    _svc_require_admin(user, "mengubah Master Service")
    by = user.get("username")
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Nama jenis service wajib diisi.")
    months = _svc_int(data.get("warranty_months"), "Masa garansi (bulan)", 0, 120)
    after = _svc_int(data.get("after_warranty_days") if data.get("after_warranty_days") not in (None, "") else 0, "Garansi hasil service (hari)", 0, 1095)
    install = _svc_int(data.get("install_warranty_months") if data.get("install_warranty_months") not in (None, "") else 0, "Garansi pemasangan (bulan)", 0, 120)
    checklist = [str(x).strip()[:60] for x in (data.get("checklist") or []) if str(x).strip()]
    if not checklist:
        raise ValueError("Isi minimal satu item kelengkapan.")
    complaints = [str(x).strip()[:60] for x in (data.get("complaints") or []) if str(x).strip()]
    cats = sorted({int(c) for c in (data.get("category_ids") or [])})
    sup = data.get("default_supplier_id") or None
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        dup = con.execute("SELECT code FROM service_profiles WHERE LOWER(name)=LOWER(?) AND id<>?", (name, profile_id or 0)).fetchone()
        if dup:
            raise ValueError(f"Nama itu sudah dipakai {dup['code']}.")
        for c in cats:
            if not con.execute("SELECT 1 FROM categories WHERE id=?", (c,)).fetchone():
                raise ValueError("Kategori tidak ditemukan.")
            other = con.execute("""SELECT p.code, p.name FROM service_profile_categories pc JOIN service_profiles p ON p.id=pc.profile_id
                                   WHERE pc.category_id=? AND pc.profile_id<>?""", (c, profile_id or 0)).fetchone()
            if other:
                raise ValueError(f"Kategori itu sudah tercakup di {other['code']} {other['name']}.")
        if sup and not con.execute("SELECT 1 FROM suppliers WHERE id=?", (sup,)).fetchone():
            raise ValueError("Supplier tujuan tidak ditemukan.")
        vals = (name, months, after, install, (data.get("est_days") or "").strip()[:40] or None, 1 if data.get("require_serial") else 0,
                1 if data.get("require_photo", True) else 0, 1 if data.get("record_serial_at_sale") else 0, 1 if data.get("ask_reset") else 0,
                (data.get("terms_text") or "").strip() or SVC_TERMS_DEFAULT, (data.get("warranty_terms") or "").strip() or None,
                json.dumps(checklist), json.dumps(complaints), sup)
        now = _now()
        if profile_id is None:
            row = con.execute("SELECT code FROM service_profiles ORDER BY code DESC LIMIT 1").fetchone()
            seq = int(row["code"].split("-")[1]) + 1 if row and row["code"].startswith("SV-") else 1
            profile_id = con.execute(
                """INSERT INTO service_profiles (name, warranty_months, after_warranty_days, install_warranty_months, est_days, require_serial, require_photo,
                       record_serial_at_sale, ask_reset, terms_text, warranty_terms, checklist, complaints, default_supplier_id,
                       code, created_by, created_at, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*vals, f"SV-{seq:02d}", by, now, by, now)).lastrowid
        else:
            n = con.execute(
                """UPDATE service_profiles SET name=?, warranty_months=?, after_warranty_days=?, install_warranty_months=?, est_days=?, require_serial=?, require_photo=?,
                       record_serial_at_sale=?, ask_reset=?, terms_text=?, warranty_terms=?, checklist=?, complaints=?, default_supplier_id=?,
                       updated_by=?, updated_at=? WHERE id=?""", (*vals, by, now, profile_id)).rowcount
            if n != 1:
                raise LookupError("Jenis service tidak ditemukan.")
        con.execute("DELETE FROM service_profile_categories WHERE profile_id=?", (profile_id,))
        con.executemany("INSERT INTO service_profile_categories (profile_id, category_id) VALUES (?, ?)", [(profile_id, c) for c in cats])
    return next(p for p in list_service_profiles(True) if p["id"] == profile_id)


def set_service_profile_status(profile_id: int, active: bool, user: dict) -> dict:
    _svc_require_admin(user, "mengubah Master Service")
    with _conn() as con:
        n = con.execute("UPDATE service_profiles SET status=?, updated_by=?, updated_at=? WHERE id=?",
                        ("aktif" if active else "nonaktif", user.get("username"), _now(), profile_id)).rowcount
        if n != 1:
            raise LookupError("Jenis service tidak ditemukan.")
    return next(p for p in list_service_profiles(True) if p["id"] == profile_id)


def save_service_override(profile_id: int, data: dict, user: dict) -> dict:
    _svc_require_admin(user, "mengubah Master Service")
    brand_id, item_id = data.get("brand_id") or None, data.get("master_item_id") or None
    if bool(brand_id) == bool(item_id):
        raise ValueError("Pilih salah satu: brand atau barang.")
    months = data.get("warranty_months")
    months = None if months in (None, "") else _svc_int(months, "Masa garansi (bulan)", 0, 120)
    inst = data.get("install_warranty_months")
    inst = None if inst in (None, "") else _svc_int(inst, "Garansi pemasangan (bulan)", 0, 120)
    aft = data.get("after_warranty_days")
    aft = None if aft in (None, "") else _svc_int(aft, "Garansi hasil service (hari)", 0, 1095)
    sup = data.get("supplier_id") or None
    if months is None and inst is None and aft is None and not sup:
        raise ValueError("Isi salah satu: masa garansi, garansi pemasangan, garansi hasil service, atau tujuan service yang berbeda.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        _svc_profile_row(con, profile_id)
        if item_id and not con.execute("SELECT 1 FROM master_items WHERE id=?", (item_id,)).fetchone():
            raise ValueError("Barang tidak ditemukan.")
        if brand_id and not con.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone():
            raise ValueError("Brand tidak ditemukan.")
        dup = con.execute("SELECT 1 FROM service_profile_overrides WHERE profile_id=? AND brand_id IS ? AND master_item_id IS ?",
                          (profile_id, brand_id, item_id)).fetchone() if brand_id else \
            con.execute("SELECT 1 FROM service_profile_overrides WHERE master_item_id=?", (item_id,)).fetchone()
        if dup:
            raise ValueError("Pengecualian untuk itu sudah ada.")
        con.execute("""INSERT INTO service_profile_overrides (profile_id, brand_id, master_item_id, warranty_months, install_warranty_months,
                           after_warranty_days, supplier_id, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (profile_id, brand_id, item_id, months, inst, aft, sup, user.get("username"), _now()))
    return next(p for p in list_service_profiles(True) if p["id"] == profile_id)


def delete_service_override(override_id: int, user: dict) -> dict:
    _svc_require_admin(user, "mengubah Master Service")
    with _conn() as con:
        row = con.execute("SELECT profile_id FROM service_profile_overrides WHERE id=?", (override_id,)).fetchone()
        if row is None:
            raise LookupError("Pengecualian tidak ditemukan.")
        con.execute("DELETE FROM service_profile_overrides WHERE id=?", (override_id,))
    return next(p for p in list_service_profiles(True) if p["id"] == row["profile_id"])


def service_serial_item_ids() -> list[int]:
    """K-S2: master items whose service profile asks for the serial number at sale (Kasir shows an SN field)."""
    with _conn() as con:
        out = set()
        rows = con.execute(
            """SELECT mi.id FROM master_items mi JOIN categories c ON c.id=mi.category_id
               JOIN service_profile_categories pc ON pc.category_id IN (c.id, c.parent_id)
               JOIN service_profiles p ON p.id=pc.profile_id
               WHERE p.status='aktif' AND p.record_serial_at_sale=1 AND mi.is_active=1
               UNION SELECT o.master_item_id FROM service_profile_overrides o JOIN service_profiles p ON p.id=o.profile_id
               WHERE o.master_item_id IS NOT NULL AND p.record_serial_at_sale=1 AND p.status='aktif'""").fetchall()
        out.update(r["id"] for r in rows)
        return sorted(out)


def service_serial_skus() -> list[str]:
    """The same list as SKUs: Kasir lines carry the SKU, not the master item id."""
    ids = service_serial_item_ids()
    if not ids:
        return []
    with _conn() as con:
        return sorted(r["sku"] for r in con.execute(
            f"SELECT sku FROM master_items WHERE id IN ({_placeholders(tuple(ids))}) AND sku IS NOT NULL", tuple(ids)))


# ---- Intake: lookups ----

def service_invoice_search(query: str) -> dict:
    """Receipt lines matching a receipt number, customer name, phone, plate or serial, with the warranty
    indication per line, plus earlier service documents for the same phone / serial."""
    q = (query or "").strip()
    if len(q) < 3:
        return {"lines": [], "services": []}
    like = f"%{q}%"
    phone = f"%{normalize_phone(q)}%"
    plate = f"%{' '.join(q.upper().split())}%"
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            f"""SELECT ri.id AS receipt_item_id, ri.product_name, ri.quantity, ri.unit_price, ri.master_item_id, ri.warranty_date,
                       ri.returned_qty, ri.serial_no, r.id AS receipt_id, r.receipt_no, r.created_at, r.status AS receipt_status,
                       r.plate_full, r.vehicle_make, r.vehicle_model, r.customer_id, r.customer_phone,
                       CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_name,
                       b.name AS brand_name
                FROM receipt_items ri JOIN receipts r ON r.id=ri.receipt_id LEFT JOIN customers c ON c.id=r.customer_id
                LEFT JOIN master_items mi ON mi.id=ri.master_item_id LEFT JOIN brands b ON b.id=mi.brand_id
                WHERE COALESCE(ri.item_type,'barang')='barang' AND r.work_status<>'draft'
                  AND (r.receipt_no LIKE ? OR r.customer_phone LIKE ? OR r.plate_full LIKE ? OR ri.serial_no LIKE ?
                       OR r.customer_name LIKE ? OR {_CUSTOMER_FULL_NAME_SQL} LIKE ?)
                ORDER BY r.created_at DESC, ri.id LIMIT 60""", (like, phone, plate, like, like, like))]
        lines = []
        for r in rows:
            prof = _svc_profile_for(con, r["master_item_id"])
            pdate = _svc_local_date(r["created_at"])
            st, until = _svc_warranty(pdate, prof["effective_months"] if prof else 0, r["warranty_date"])
            ist, iuntil = _svc_install_warranty(pdate, prof["effective_install_months"] if prof else 0)
            after_until = con.execute(
                "SELECT MAX(after_warranty_until) AS u FROM service_orders WHERE receipt_item_id=? AND result='diperbaiki' AND after_warranty_until IS NOT NULL",
                (r["receipt_item_id"],)).fetchone()["u"]
            active = con.execute("SELECT service_no FROM service_orders WHERE receipt_item_id=? AND stage IN ({})".format(_placeholders(SVC_OPEN)),
                                 (r["receipt_item_id"], *SVC_OPEN)).fetchone()
            warn = []
            if r["receipt_status"] == "void":
                warn.append("Nota dibatalkan (void)")
                st = "belum"
            if r["returned_qty"] and r["returned_qty"] >= r["quantity"]:
                warn.append("Barang ini sudah diretur penuh")
                st = "belum"
            if active:
                warn.append(f"Sudah ada service aktif {active['service_no']}")
            lines.append({**r, "purchase_date": pdate, "age": _svc_age(pdate), "profile_id": prof["id"] if prof else None,
                          "profile_name": prof["name"] if prof else None, "warranty_months": prof["effective_months"] if prof else None,
                          "warranty_status": st, "warranty_until": until, "warnings": warn, "serviceable": prof is not None,
                          "install_warranty_months": prof["effective_install_months"] if prof else None, "install_warranty_status": ist, "install_warranty_until": iuntil,
                          "after_service_until": after_until,
                          "after_service_status": (None if not after_until else ("aktif" if shop_today().isoformat() <= after_until else "habis"))})
        services = [dict(r) for r in con.execute(
            """SELECT id, service_no, stage, result, product_name, serial_no, received_at, customer_name FROM service_orders
               WHERE service_no IS NOT NULL AND (customer_phone LIKE ? OR serial_no LIKE ? OR serial_new LIKE ? OR service_no LIKE ?)
               ORDER BY id DESC LIMIT 20""", (phone, like, like, like))]
        return {"lines": lines, "services": services}


def service_customer_summary(phone: str) -> dict:
    phone = normalize_phone(phone)
    with _conn() as con:
        c = con.execute(f"SELECT id, {_CUSTOMER_FULL_NAME_SQL.replace('c.', '')} AS name, phone, created_at FROM customers WHERE phone=?",
                        (phone,)).fetchone()
        if c is None:
            return {"customer": None}
        rc = con.execute("SELECT COUNT(*) AS n, MAX(created_at) AS last FROM receipts WHERE customer_id=? AND status<>'void'", (c["id"],)).fetchone()
        last = con.execute("""SELECT plate_full, vehicle_make, vehicle_model FROM receipts WHERE customer_id=? AND status<>'void'
                              ORDER BY created_at DESC LIMIT 1""", (c["id"],)).fetchone()
        svcs = [dict(r) for r in con.execute(
            "SELECT id, service_no, stage, product_name, received_at FROM service_orders WHERE customer_id=? AND service_no IS NOT NULL ORDER BY id DESC LIMIT 10",
            (c["id"],))]
        vehicles = [_vehicle_row(r) for r in con.execute(
            f"{_VEHICLE_SELECT} WHERE v.customer_id=? AND v.is_active=1 ORDER BY last_used_at IS NULL, last_used_at DESC, v.id DESC", (c["id"],))]
        return {"customer": dict(c), "receipt_count": rc["n"], "last_receipt_at": rc["last"], "last_vehicle": dict(last) if last else None,
                "vehicles": vehicles,
                "services": svcs, "active": [s for s in svcs if s["stage"] in SVC_OPEN]}


# ---- Intake: save & receive ----

_SVC_DRAFT_TEXT = {"customer_name": 120, "plate": 20, "vehicle": 120, "brand_name": 80, "product_name": 160, "model": 120,
                   "serial_no": 80, "unit": 20, "condition_in": 500, "complaint": 1000, "pic": 120, "tech_note": 1000}


def save_service_order(order_id: int | None, data: dict, user: dict) -> dict:
    """Creates or updates a draft (intake steps 1-3). Once received, only an admin can correct the text fields."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if order_id is None:
            now = _now()
            order_id = con.execute("INSERT INTO service_orders (created_by, created_at, updated_by, updated_at) VALUES (?, ?, ?, ?)",
                                   (by, now, by, now)).lastrowid
            o = _svc_get(con, order_id)
            fresh = True
        else:
            o = _svc_get(con, order_id)
            _svc_check_version(o, data)
            fresh = False
        if o["stage"] != "draft":
            _svc_require_admin(user, "mengoreksi data service yang sudah diterima")
            if o["stage"] in ("selesai", "dibatalkan"):
                raise ValueError("Dokumen ini sudah ditutup.")
            sets = {k: (str(data[k]).strip()[:n] or None) for k, n in _SVC_DRAFT_TEXT.items()
                    if k in data and k not in ("customer_name",) and data[k] is not None}
            if not sets:
                raise ValueError("Tidak ada yang diubah.")
            con.execute(f"UPDATE service_orders SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), order_id))
            _svc_event(con, order_id, "koreksi", "Data dikoreksi admin: " + ", ".join(sets), by)
            return {"id": order_id}
        f = {k: (str(data.get(k) or "").strip()[:n] or None) for k, n in _SVC_DRAFT_TEXT.items()}
        f["customer_phone"] = normalize_phone(data.get("customer_phone")) or None
        f["customer_id"] = data.get("customer_id") or None
        f["qty"] = _svc_int(data.get("qty") or 1, "Qty", 1, 99)
        f["reset_consent"] = None if data.get("reset_consent") is None else (1 if data.get("reset_consent") else 0)
        f["lock_removed"] = None if data.get("lock_removed") is None else (1 if data.get("lock_removed") else 0)
        handling = data.get("handling") or None
        if handling and handling not in SVC_HANDLING:
            raise ValueError("Jalur penanganan tidak dikenal.")
        f["handling"] = handling
        f["supplier_id"] = data.get("supplier_id") or None
        if f["supplier_id"] and not con.execute("SELECT 1 FROM suppliers WHERE id=?", (f["supplier_id"],)).fetchone():
            raise ValueError("Supplier tujuan tidak ditemukan.")
        item_id = data.get("receipt_item_id") or None
        profile_id = data.get("profile_id") or None
        f.update(receipt_item_id=None, receipt_id=None, receipt_no=None, purchase_date=None, no_invoice_reason=None,
                 master_item_id=data.get("master_item_id") or None)
        if item_id:
            line = con.execute(
                """SELECT ri.*, r.receipt_no, r.created_at AS r_created, r.status AS r_status, r.customer_id AS r_customer, r.plate_full,
                          r.vehicle_make, r.vehicle_model, r.customer_phone AS r_phone, r.customer_name AS r_name, b.name AS brand
                   FROM receipt_items ri JOIN receipts r ON r.id=ri.receipt_id LEFT JOIN master_items mi ON mi.id=ri.master_item_id
                   LEFT JOIN brands b ON b.id=mi.brand_id WHERE ri.id=?""", (item_id,)).fetchone()
            if line is None:
                raise ValueError("Baris nota tidak ditemukan.")
            _svc_check_line_qty(con, order_id, line["id"], f["qty"])
            f.update(receipt_item_id=line["id"], receipt_id=line["receipt_id"], receipt_no=line["receipt_no"],
                     purchase_date=_svc_local_date(line["r_created"]), master_item_id=line["master_item_id"])
            f["product_name"] = f["product_name"] or line["product_name"]
            f["brand_name"] = f["brand_name"] or line["brand"]
            f["serial_no"] = f["serial_no"] or line["serial_no"]
            f["plate"] = f["plate"] or line["plate_full"]
            f["vehicle"] = f["vehicle"] or " ".join(x for x in (line["vehicle_make"], line["vehicle_model"]) if x) or None
            f["customer_phone"] = f["customer_phone"] or line["r_phone"] or None
            f["customer_name"] = f["customer_name"] or line["r_name"]
            f["customer_id"] = f["customer_id"] or line["r_customer"]
            prof = _svc_profile_for(con, line["master_item_id"], profile_id)
            if prof is None:
                raise ValueError("Barang di baris nota ini tidak termasuk jenis service di Master Service. Pilih jenis service secara manual.")
            wst, until = _svc_warranty(f["purchase_date"], prof["effective_months"], line["warranty_date"])
            if line["r_status"] == "void" or (line["returned_qty"] and line["returned_qty"] >= line["quantity"]):
                wst = "belum"
            ist, iuntil = _svc_install_warranty(f["purchase_date"], prof["effective_install_months"])
            f.update(warranty_basis="nota", warranty_months=prof["effective_months"], warranty_until=until, warranty_status=wst,
                     install_warranty_months=prof["effective_install_months"] or None, install_warranty_until=iuntil, install_warranty_status=ist)
        else:
            reason = data.get("no_invoice_reason") or None
            if reason and reason not in SVC_NO_INVOICE_REASONS:
                raise ValueError("Alasan tanpa nota tidak dikenal.")
            f["no_invoice_reason"] = reason
            prof = _svc_profile_for(con, f["master_item_id"], profile_id) if (profile_id or f["master_item_id"]) else None
            basis = data.get("warranty_basis") or "tanpa"
            if basis not in ("kartu", "tanpa"):
                basis = "tanpa"
            f.update(warranty_basis=basis, warranty_months=prof["effective_months"] if prof else None, warranty_until=None, warranty_status="belum",
                     install_warranty_months=None, install_warranty_until=None, install_warranty_status=None)
        f["profile_id"] = prof["id"] if prof else None
        if prof:
            f.update(est_days=prof["est_days"], terms_text=prof["terms_text"], after_warranty_days=prof["effective_after_days"])
            if not f["supplier_id"] and prof.get("effective_supplier_id"):
                f["supplier_id"] = prof["effective_supplier_id"]
        ov = data.get("warranty_override")
        if ov:
            _svc_require_admin(user, "mengubah status garansi")
            if ov.get("status") not in SVC_WARRANTY or not (ov.get("note") or "").strip():
                raise ValueError("Pilih status garansi dan tulis alasannya.")
            f["warranty_status"], f["warranty_note"] = ov["status"], ov["note"].strip()[:300]
        # K-15: a vehicle picked from the customer's list gives the plate and the vehicle line.
        f["vehicle_id"] = None
        if data.get("vehicle_id"):
            v = con.execute("SELECT * FROM vehicles WHERE id=?", (data["vehicle_id"],)).fetchone()
            if v is None:
                raise ValueError("Kendaraan tidak ditemukan.")
            if f["customer_id"] and v["customer_id"] != int(f["customer_id"]):
                raise ValueError("Kendaraan ini terdaftar atas pelanggan lain.")
            f.update(vehicle_id=v["id"], customer_id=f["customer_id"] or v["customer_id"], plate=v["plate_full"],
                     vehicle=" ".join(x for x in (v["make"], v["model"], v["year"]) if x) or f["vehicle"])
        elif f["customer_id"] and _veh_plate(f["plate"]):
            v = con.execute("SELECT id FROM vehicles WHERE customer_id=? AND plate_full=?", (f["customer_id"], _veh_plate(f["plate"]))).fetchone()
            f["vehicle_id"] = v["id"] if v else None
        cols = list(f)
        con.execute(f"UPDATE service_orders SET {', '.join(c + '=?' for c in cols)} WHERE id=?", (*[f[c] for c in cols], order_id))
        # Kirim ke Pabrik: the planned courier / route / waybill ride on the draft shipment so the handover form comes prefilled.
        if handling == "kirim":
            plan = {k: data.get("ship_" + k) for k in ("courier", "mode", "awb") if "ship_" + k in data}
            if plan:
                _svc_apply_shipment(con, _svc_get(con, order_id), _svc_draft_shipment(con, order_id, by), plan)
        else:
            con.execute("DELETE FROM service_shipments WHERE order_id=? AND status='draft'", (order_id,))
        if "accessories" in data:
            con.execute("DELETE FROM service_accessories WHERE order_id=?", (order_id,))
            for pos, a in enumerate(data.get("accessories") or []):
                name = str(a.get("name") or "").strip()[:80]
                if name:
                    con.execute("INSERT INTO service_accessories (order_id, name, brought, position) VALUES (?, ?, ?, ?)",
                                (order_id, name, 1 if a.get("brought") else 0, pos))
        if ov:
            _svc_event(con, order_id, "garansi", f"Status garansi diubah admin ke {SVC_WARRANTY[ov['status']]}: {ov['note'].strip()}", by)
        _svc_event(con, order_id, "draft", "Draft dibuat" if fresh else "Draft disimpan", by)
    return get_service_order(order_id)


def receive_service_order(order_id: int, data: dict, user: dict) -> dict:
    """Locks the document number and puts the unit in the custody register. The first stage follows the
    chosen handling: internal check, waiting to go out, or waiting for a decision."""
    by = user.get("username")
    skip = (data.get("photo_skip_reason") or "").strip()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        _svc_check_version(o, data)
        if o["stage"] != "draft":
            raise ValueError("Dokumen ini sudah diterima.")
        miss = []
        if not o["customer_name"]:
            miss.append("nama customer")
        if not o["customer_phone"] or len(o["customer_phone"]) < 8:
            miss.append("nomor HP")
        if not o["product_name"]:
            miss.append("nama barang")
        if not o["profile_id"]:
            miss.append("jenis service")
        if not o["condition_in"]:
            miss.append("kondisi fisik")
        if not o["complaint"]:
            miss.append("keluhan")
        if not o["handling"]:
            miss.append("jalur penanganan")
        if not o["receipt_item_id"] and not o["no_invoice_reason"]:
            miss.append("nota asal atau alasan tanpa nota")
        if miss:
            raise ValueError("Lengkapi dulu: " + ", ".join(miss) + ".")
        if o["receipt_item_id"]:
            _svc_check_line_qty(con, order_id, o["receipt_item_id"], o["qty"])
        prof = _svc_profile_row(con, o["profile_id"])
        if prof["require_serial"] and not o["serial_no"]:
            raise ValueError(f"Serial number wajib untuk {prof['name']}.")
        if o["handling"] in ("kirim", "sales") and not o["supplier_id"]:
            raise ValueError("Pilih supplier / principal tujuan.")
        if not con.execute("SELECT 1 FROM service_accessories WHERE order_id=? AND brought=1", (order_id,)).fetchone():
            raise ValueError("Centang minimal satu kelengkapan yang dibawa.")
        if o["serial_no"]:
            dup = con.execute(f"""SELECT service_no FROM service_orders WHERE id<>? AND stage IN ({_placeholders(SVC_OPEN)})
                                  AND (serial_no=? OR serial_new=?)""", (order_id, *SVC_OPEN, o["serial_no"], o["serial_no"])).fetchone()
            if dup:
                raise ValueError(f"Serial {o['serial_no']} sedang dalam service aktif {dup['service_no']}.")
        lacking = []
        if prof["require_photo"]:
            need = ["masuk"] + (["sn"] if prof["require_serial"] else [])
            lacking = [SVC_PHOTO_STAGES[s].lower() for s in need if not _svc_photo_count(con, order_id, s)]
            if lacking:
                if not skip:
                    raise ValueError("Unggah foto " + " dan ".join(lacking) + " dulu.")
                _svc_require_admin(user, "melewati foto wajib")
        cid, phone, name = _resolve_receipt_customer(con, o["customer_id"], o["customer_phone"], o["customer_name"])
        stage = {"internal": "pengecekan", "kirim": "menunggu_kirim", "sales": "menunggu_kirim", "menunggu": "diterima"}[o["handling"]]
        no = _sr_number(con, "SVC", "service_orders", "service_no")
        n = con.execute("""UPDATE service_orders SET service_no=?, stage=?, location='di_toko', customer_id=?, customer_phone=?, received_at=?,
                               received_by=? WHERE id=? AND stage='draft'""", (no, stage, cid, phone, _now(), by, order_id)).rowcount
        if n != 1:
            raise ValueError("Dokumen ini sudah diterima.")
        make, model = _split_vehicle_text(o["vehicle"])
        vid = o.get("vehicle_id") or _ensure_vehicle(con, cid, o["plate"], make, model, by=by)
        if vid:
            con.execute("UPDATE service_orders SET vehicle_id=? WHERE id=?", (vid, order_id))
        note = f"Diterima di toko · {SVC_HANDLING[o['handling']]}"
        if skip and lacking:
            note += f" · foto wajib dilewati admin: {skip}"
        _svc_event(con, order_id, "diterima", note, by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def set_service_handling(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    handling = data.get("handling")
    if handling not in SVC_HANDLING:
        raise ValueError("Pilih jalur penanganan.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] not in ("diterima", "pengecekan", "menunggu_kirim") or o["location"] not in _SVC_AT_SHOP:
            raise ValueError("Jalur hanya bisa diubah selagi unit ada di toko dan belum dikirim.")
        sup = None if handling in ("internal", "menunggu") else (data.get("supplier_id") or o["supplier_id"])
        if handling in ("kirim", "sales") and not sup:
            raise ValueError("Pilih supplier / principal tujuan.")
        stage = {"internal": "pengecekan", "kirim": "menunggu_kirim", "sales": "menunggu_kirim", "menunggu": "diterima"}[handling]
        con.execute("UPDATE service_orders SET handling=?, supplier_id=?, pic=COALESCE(?, pic), tech_note=COALESCE(?, tech_note), stage=? WHERE id=?",
                    (handling, sup, (data.get("pic") or "").strip() or None, (data.get("tech_note") or "").strip() or None, stage, order_id))
        con.execute("DELETE FROM service_shipments WHERE order_id=? AND status='draft'", (order_id,))
        _svc_event(con, order_id, "penanganan", f"Jalur penanganan: {SVC_HANDLING[handling]}", by)
    return get_service_order(order_id)


def _svc_insert_lines(con, order_id: int, lines: list[dict], status: str, diagnosis_id: int | None, by: str | None) -> int:
    total = 0
    for ln in lines or []:
        kind = ln.get("kind")
        if kind not in SVC_COST_KINDS:
            raise ValueError("Jenis biaya tidak dikenal.")
        fc = _svc_int(ln.get("factory_cost"), "Biaya pabrik")
        cp = _svc_int(ln.get("customer_price") if ln.get("customer_price") not in (None, "") else fc, "Harga ke customer")
        qty = _svc_int(ln.get("qty") or 1, "Qty", 1, 99)
        mid = ln.get("master_item_id") or None
        if mid and not con.execute("SELECT 1 FROM master_items WHERE id=?", (mid,)).fetchone():
            raise ValueError("Sparepart tidak ditemukan di Master Barang.")
        if fc == 0 and cp == 0:
            continue
        con.execute("""INSERT INTO service_cost_lines (order_id, diagnosis_id, kind, description, master_item_id, qty, factory_cost, customer_price,
                           status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, diagnosis_id, kind, (ln.get("description") or "").strip()[:160] or None, mid, qty, fc, cp, status, by, _now()))
        total += cp * qty
    return total


def _svc_fee_locked(con, o: dict) -> None:
    if o["fee_receipt_id"]:
        r = con.execute("SELECT status FROM receipts WHERE id=?", (o["fee_receipt_id"],)).fetchone()
        if r and r["status"] != "void":
            raise ValueError("Nota Jasa Service sudah dibuat. Batalkan (void) nota jasanya dulu bila biaya berubah.")


def record_internal_result(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    outcome = data.get("outcome")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] != "pengecekan":
            raise ValueError("Unit ini tidak sedang dalam pemeriksaan internal.")
        note = (data.get("note") or "").strip()[:500] or None
        if outcome == "tidak_rusak":
            con.execute("UPDATE service_orders SET result='apa_adanya', stage='siap_diambil', ready_at=?, qc_note=? WHERE id=?",
                        (_now(), note, order_id))
            _svc_event(con, order_id, "internal", "Pemeriksaan internal: tidak ditemukan kerusakan" + (f" — {note}" if note else ""), by)
        elif outcome == "perbaiki_toko":
            _svc_fee_locked(con, o)
            total = _svc_insert_lines(con, order_id, data.get("lines") or [], "usulan", None, by)
            if total > 0:
                con.execute("UPDATE service_orders SET stage='menunggu_customer', qc_note=? WHERE id=?", (note, order_id))
                _svc_event(con, order_id, "internal", f"Diperbaiki di toko, estimasi Rp {_sr_rupiah(total)} menunggu persetujuan customer", by)
            else:
                con.execute("UPDATE service_orders SET result='diperbaiki', stage='siap_diambil', ready_at=?, qc_note=? WHERE id=?",
                            (_now(), note, order_id))
                _svc_event(con, order_id, "internal", "Diperbaiki di toko tanpa biaya" + (f" — {note}" if note else ""), by)
        elif outcome == "selesai_perbaikan":
            if con.execute("SELECT 1 FROM service_cost_lines WHERE order_id=? AND status='usulan'", (order_id,)).fetchone():
                raise ValueError("Masih ada biaya yang menunggu persetujuan customer.")
            con.execute("UPDATE service_orders SET result='diperbaiki', stage='siap_diambil', ready_at=?, qc_note=? WHERE id=?",
                        (_now(), note, order_id))
            _svc_event(con, order_id, "internal", "Perbaikan di toko selesai, siap diambil" + (f" — {note}" if note else ""), by)
        elif outcome == "perlu_pabrik":
            handling = data.get("handling") if data.get("handling") in ("kirim", "sales") else "kirim"
            sup = data.get("supplier_id") or o["supplier_id"]
            if not sup:
                raise ValueError("Pilih supplier / principal tujuan.")
            con.execute("UPDATE service_orders SET handling=?, supplier_id=?, stage='menunggu_kirim', tech_note=COALESCE(?, tech_note) WHERE id=?",
                        (handling, sup, note, order_id))
            _svc_event(con, order_id, "internal", f"Pemeriksaan internal: perlu {SVC_HANDLING[handling].lower()}", by)
        else:
            raise ValueError("Pilih hasil pemeriksaan internal.")
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


# ---- Out to the factory ----

def _svc_draft_shipment(con, order_id: int, by: str | None) -> dict:
    row = con.execute("SELECT * FROM service_shipments WHERE order_id=? AND direction='keluar' AND status='draft'", (order_id,)).fetchone()
    if row:
        return dict(row)
    sid = con.execute("INSERT INTO service_shipments (order_id, direction, status, created_by, created_at) VALUES (?, 'keluar', 'draft', ?, ?)",
                      (order_id, by, _now())).lastrowid
    return dict(con.execute("SELECT * FROM service_shipments WHERE id=?", (sid,)).fetchone())


def _svc_apply_shipment(con, o: dict, sh: dict, data: dict) -> dict:
    channel = "sales" if o["handling"] == "sales" else "ekspedisi"
    vals = {"channel": channel}
    for k, n in (("courier", 60), ("weight", 20), ("sales_name", 120), ("sales_phone", 30), ("scheduled_at", 20), ("note", 500)):
        if k in data:
            vals[k] = (str(data.get(k) or "").strip()[:n]) or None
    if "mode" in data:
        if data["mode"] and data["mode"] not in SVC_SHIP_MODES:
            raise ValueError("Jalur kirim harus udara, laut, atau darat.")
        vals["mode"] = data["mode"] or None
    if "awb" in data:
        vals["awb"] = (str(data.get("awb") or "").strip().upper()[:60]) or None
    if "koli" in data:
        vals["koli"] = _svc_int(data.get("koli") or 0, "Jumlah koli", 0, 99) or None
    if "ship_cost" in data:
        vals["ship_cost"] = _svc_int(data.get("ship_cost"), "Ongkir")
    if "cost_bearer" in data:
        if data["cost_bearer"] and data["cost_bearer"] not in SVC_COST_BEARERS:
            raise ValueError("Penanggung ongkir tidak dikenal.")
        vals["cost_bearer"] = data["cost_bearer"] or None
    if "packing" in data:
        vals["packing"] = json.dumps(data.get("packing") or {})
    con.execute(f"UPDATE service_shipments SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), sh["id"]))
    if "sent_accessory_ids" in data:
        ids = {int(x) for x in data.get("sent_accessory_ids") or []}
        for a in con.execute("SELECT id FROM service_accessories WHERE order_id=? AND brought=1", (o["id"],)).fetchall():
            con.execute("UPDATE service_accessories SET sent=? WHERE id=?", (1 if a["id"] in ids else 0, a["id"]))
    return dict(con.execute("SELECT * FROM service_shipments WHERE id=?", (sh["id"],)).fetchone())


def save_service_shipment(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] != "menunggu_kirim" or o["location"] not in _SVC_AT_SHOP:
            raise ValueError("Unit ini tidak sedang menunggu dikirim.")
        _svc_apply_shipment(con, o, _svc_draft_shipment(con, order_id, by), data)
        _svc_event(con, order_id, "kirim_draft", "Jadwal / draft pengiriman disimpan", by)
    return get_service_order(order_id)


def confirm_service_out(order_id: int, data: dict, user: dict) -> dict:
    """The unit leaves the shop exactly once: handed to the courier (waybill) or to the supplier's salesman
    (signature or photo). Compare-and-set on location. No stock moves: it is the customer's unit."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["location"] not in _SVC_AT_SHOP or o["stage"] != "menunggu_kirim":
            if o["location"] in _SVC_AWAY:
                raise ValueError("Unit ini sudah dikonfirmasi keluar toko.")
            raise ValueError("Unit ini tidak sedang menunggu dikirim.")
        sh = _svc_apply_shipment(con, o, _svc_draft_shipment(con, order_id, by), data)
        sup = con.execute("SELECT * FROM suppliers WHERE id=?", (o["supplier_id"],)).fetchone() if o["supplier_id"] else None
        if sup is None:
            raise ValueError("Pilih supplier / principal tujuan.")
        if not con.execute("SELECT 1 FROM service_accessories WHERE order_id=? AND sent=1", (order_id,)).fetchone():
            raise ValueError("Tandai kelengkapan yang ikut dikirim (minimal unitnya).")
        handed = (data.get("handed_at") or "").strip()[:16] or _svc_local_now()
        if sh["channel"] == "ekspedisi":
            if not (sup["address"] or "").strip():
                raise ValueError(f"Alamat {sup['name']} belum diisi di Master Supplier.")
            if not sh["mode"]:
                raise ValueError("Pilih jalur kirim: udara, laut, atau darat.")
            if not sh["courier"]:
                raise ValueError("Ekspedisi wajib diisi.")
            if not sh["awb"]:
                raise ValueError("Nomor resi wajib diisi.")
            dup = con.execute("""SELECT o.service_no FROM service_shipments s JOIN service_orders o ON o.id=s.order_id
                                 WHERE s.awb=? AND s.status<>'draft' AND s.id<>?""", (sh["awb"], sh["id"])).fetchone()
            if dup:
                raise ValueError(f"Resi {sh['awb']} sudah dipakai service {dup['service_no']}.")
            if not _svc_photo_count(con, order_id, "resi"):
                raise ValueError("Unggah foto resi / bukti serah ekspedisi dulu.")
            bearer = sh["cost_bearer"] or ("toko" if o["warranty_status"] == "aktif" else "customer")
            loc = "di_ekspedisi"
            note = f"Diserahkan ke ekspedisi {sh['courier']} ({SVC_SHIP_MODES[sh['mode']].lower()}), resi {sh['awb']}"
        else:
            if not sh["sales_name"] or not sh["sales_phone"]:
                raise ValueError("Nama dan HP sales pengambil wajib diisi.")
            if not _svc_photo_count(con, order_id, "serah_sales", "ttd_sales"):
                raise ValueError("Simpan tanda tangan sales atau foto serah terima dulu.")
            bearer = sh["cost_bearer"] or "supplier"
            loc = "dibawa_sales"
            note = f"Diambil sales {sh['sales_name']} ({sh['sales_phone']})"
        n = con.execute(f"UPDATE service_orders SET location=?, stage='di_supplier' WHERE id=? AND location IN ({_placeholders(_SVC_AT_SHOP)}) AND stage='menunggu_kirim'",
                        (loc, order_id, *_SVC_AT_SHOP)).rowcount
        if n != 1:
            raise ValueError("Unit ini sudah dikonfirmasi keluar toko.")
        doc = _sr_number(con, "SJS", "service_shipments", "doc_no")
        con.execute("UPDATE service_shipments SET status='dikirim', doc_no=?, handed_at=?, officer=?, cost_bearer=? WHERE id=?",
                    (doc, handed, (data.get("officer") or "").strip() or by, bearer, sh["id"]))
        if sh["ship_cost"] and bearer == "customer":
            con.execute("""INSERT INTO service_cost_lines (order_id, kind, description, qty, factory_cost, customer_price, status, created_by, created_at)
                           VALUES (?, 'ongkir', ?, 1, ?, ?, 'otomatis', ?, ?)""",
                        (order_id, f"Ongkir kirim ke {sup['name']} ({doc})", sh["ship_cost"], sh["ship_cost"], by, _now()))
            note += f"; ongkir Rp {_sr_rupiah(sh['ship_cost'])} ditagih ke customer (K-S5)"
        elif sh["ship_cost"]:
            note += f"; ongkir Rp {_sr_rupiah(sh['ship_cost'])} ditanggung {SVC_COST_BEARERS[bearer].lower()}"
        _svc_event(con, order_id, "keluar", f"{note} · {doc}", by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def mark_service_at_factory(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["location"] not in ("di_ekspedisi", "dibawa_sales"):
            raise ValueError("Unit ini tidak sedang dalam perjalanan ke pabrik.")
        at = (data.get("received_at") or "").strip()[:10] or shop_today().isoformat()
        con.execute("UPDATE service_orders SET location='di_pabrik' WHERE id=?", (order_id,))
        con.execute("UPDATE service_shipments SET received_at=?, status='diterima' WHERE order_id=? AND direction='keluar' AND status='dikirim'",
                    (at, order_id))
        rma = (data.get("rma_no") or "").strip()[:60]
        _svc_event(con, order_id, "pabrik_terima", f"Pabrik menerima unit {at[8:10]}/{at[5:7]}/{at[:4]}" + (f", RMA {rma}" if rma else ""), by)
        if rma:
            con.execute("UPDATE service_orders SET rma_no=? WHERE id=?", (rma, order_id))
    return get_service_order(order_id)


# ---- Factory decision, approval, replacement ----

def record_service_diagnosis(order_id: int, data: dict, user: dict) -> dict:
    _svc_require_admin(user, "mencatat hasil pemeriksaan pabrik")
    by = user.get("username")
    decision = data.get("decision")
    if decision not in SVC_DECISIONS:
        raise ValueError("Pilih keputusan pabrik.")
    fw = data.get("factory_warranty")
    if fw not in ("ditanggung", "tidak"):
        raise ValueError("Pilih status garansi menurut pabrik.")
    desc = (data.get("description") or "").strip()
    if not desc:
        raise ValueError("Uraian diagnosis wajib diisi.")
    diag_at = (data.get("diagnosed_at") or "").strip()[:10] or shop_today().isoformat()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] not in ("di_supplier", "menunggu_customer") or o["location"] not in ("di_pabrik", "dibawa_sales"):
            raise ValueError("Hasil pabrik dicatat setelah unit diterima pabrik (atau dibawa sales).")
        _svc_fee_locked(con, o)
        if fw == "tidak" and not (data.get("reject_reason") or "").strip():
            raise ValueError("Tulis alasan garansi tidak ditanggung.")
        con.execute("UPDATE service_diagnoses SET is_current=0 WHERE order_id=?", (order_id,))
        con.execute("UPDATE service_cost_lines SET status='batal' WHERE order_id=? AND status='usulan'", (order_id,))
        con.execute("UPDATE service_replacements SET status='batal' WHERE order_id=? AND status='menunggu'", (order_id,))
        did = con.execute("""INSERT INTO service_diagnoses (order_id, rma_no, diagnosed_at, examiner, factory_warranty, reject_reason, decision,
                                 description, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (order_id, (data.get("rma_no") or "").strip()[:60] or None, diag_at, (data.get("examiner") or "").strip()[:120] or None,
                           fw, (data.get("reject_reason") or "").strip()[:300] or None, decision, desc[:2000], by, _now())).lastrowid
        notes = [f"Hasil pabrik: {SVC_DECISIONS[decision]}"]
        if fw == "tidak" and o["warranty_status"] != "ditolak":
            con.execute("UPDATE service_orders SET warranty_status='ditolak', warranty_note=? WHERE id=?", (data.get("reject_reason"), order_id))
            notes.append(f"garansi ditolak pabrik ({data.get('reject_reason')})")
        stage, result = "di_supplier", "belum"
        if decision == "perbaiki_biaya":
            total = _svc_insert_lines(con, order_id, data.get("lines") or [], "usulan", did, by)
            if total <= 0:
                raise ValueError("Isi rincian biaya untuk keputusan berbayar.")
            stage = "menunggu_customer"
            notes.append(f"estimasi ke customer Rp {_sr_rupiah(total)}")
        elif decision == "ganti":
            con.execute("""INSERT INTO service_replacements (order_id, mode, status, old_serial, master_item_id, product_name, created_by, created_at)
                           VALUES (?, 'pabrik', 'menunggu', ?, ?, ?, ?, ?)""",
                        (order_id, o["serial_no"], o["master_item_id"], o["product_name"], by, _now()))
        elif decision == "tolak":
            result = "ditolak"
            notes.append("tindak lanjut penolakan perlu dicatat")
        con.execute("UPDATE service_orders SET stage=?, result=? WHERE id=?", (stage, result, order_id))
        _svc_event(con, order_id, "diagnosis", "; ".join(notes), by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def record_service_approval(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    status = data.get("status")
    if status not in ("menunggu", "setuju", "tolak"):
        raise ValueError("Pilih Menunggu, Setuju, atau Tolak.")
    medium = data.get("medium") or None
    if medium and medium not in SVC_MEDIA:
        raise ValueError("Media persetujuan tidak dikenal.")
    note = (data.get("note") or "").strip()[:500] or None
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] != "menunggu_customer":
            raise ValueError("Tidak ada biaya yang menunggu persetujuan customer.")
        if data.get("prices"):
            _svc_require_admin(user, "mengubah harga ke customer")
            for lid, price in data["prices"].items():
                con.execute("UPDATE service_cost_lines SET customer_price=? WHERE id=? AND order_id=? AND status='usulan'",
                            (_svc_int(price, "Harga ke customer"), int(lid), order_id))
        total = con.execute("SELECT COALESCE(SUM(customer_price*qty),0) AS n FROM service_cost_lines WHERE order_id=? AND status='usulan'",
                            (order_id,)).fetchone()["n"]
        if status != "menunggu" and not medium:
            raise ValueError("Pilih media persetujuan (WhatsApp / telepon / datang).")
        if status == "tolak" and not note:
            raise ValueError("Tulis alasan customer menolak.")
        con.execute("""INSERT INTO service_approvals (order_id, status, contacted_at, medium, note, deadline, total, created_by, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (order_id, status, (data.get("contacted_at") or "").strip()[:16] or _svc_local_now(), medium, note,
                     (data.get("deadline") or "").strip()[:10] or (shop_today() + timedelta(days=7)).isoformat(), total, by, _now()))
        if status == "menunggu":
            _svc_event(con, order_id, "persetujuan", f"Estimasi Rp {_sr_rupiah(total)} dikirim ke customer" + (f" lewat {SVC_MEDIA[medium]}" if medium else ""), by)
        elif status == "setuju":
            con.execute("UPDATE service_cost_lines SET status='disetujui' WHERE order_id=? AND status='usulan'", (order_id,))
            stage = "pengecekan" if o["location"] in _SVC_AT_SHOP else "di_supplier"
            con.execute("UPDATE service_orders SET stage=? WHERE id=?", (stage, order_id))
            _svc_event(con, order_id, "persetujuan", f"Customer setuju biaya Rp {_sr_rupiah(total)} ({SVC_MEDIA[medium]})"
                       + ("; pabrik diberi tahu untuk mengerjakan" if stage == "di_supplier" else "; dikerjakan di toko"), by)
        else:
            con.execute("UPDATE service_cost_lines SET status='ditolak' WHERE order_id=? AND status='usulan'", (order_id,))
            at_shop = o["location"] in _SVC_AT_SHOP
            con.execute("""UPDATE service_orders SET result='apa_adanya', stage=?, ready_at=CASE WHEN ? THEN ? ELSE ready_at END,
                               stop_kind='tolak_biaya', stop_source='customer', stop_reason=?, stop_at=? WHERE id=?""",
                        ("siap_diambil" if at_shop else "kembali", 1 if at_shop else 0, _now(), note, _now(), order_id))
            _svc_event(con, order_id, "persetujuan", f"Customer menolak biaya Rp {_sr_rupiah(total)}: {note}"
                       + ("" if at_shop else "; unit diminta dikembalikan apa adanya"), by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def _svc_add_diff_line(con, order_id: int, diff: int, cost: int, medium: str | None, note: str | None, by: str | None) -> None:
    """A price difference for another model, already agreed by the customer, billed on the Nota Jasa."""
    con.execute("""INSERT INTO service_cost_lines (order_id, kind, description, qty, factory_cost, customer_price, status, created_by, created_at)
                   VALUES (?, 'penggantian', ?, 1, ?, ?, 'disetujui', ?, ?)""",
                (order_id, (note or "Selisih harga ganti model").strip()[:160], cost, diff, by, _now()))
    con.execute("INSERT INTO service_approvals (order_id, status, contacted_at, medium, note, total, created_by, created_at) VALUES (?, 'setuju', ?, ?, ?, ?, ?, ?)",
                (order_id, _svc_local_now(), medium, "Selisih harga ganti model", diff, by, _now()))


def record_service_replacement(order_id: int, data: dict, user: dict) -> dict:
    """Cara A: the factory sends a replacement (custody, no stock). Cara B: a swap from the shop's own stock,
    which is only a request here: an admin approves it (decide_service_swap) and staff then hand the unit out
    of stock (post_service_swap). The broken unit must already be at the shop. A price difference (another
    model) is billed as an approved cost line."""
    by = user.get("username")
    mode = data.get("mode")
    if mode not in SVC_REPLACE_MODES:
        raise ValueError("Pilih cara penggantian.")
    diff = _svc_int(data.get("price_diff"), "Selisih harga")
    diff_cost = _svc_int(data.get("diff_cost"), "Modal selisih")
    medium = data.get("diff_medium") or None
    if diff and medium not in SVC_MEDIA:
        raise ValueError("Selisih harga harus sudah disetujui customer: pilih medianya.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] in ("draft", "selesai", "dibatalkan", "siap_diambil"):
            raise ValueError("Penggantian tidak bisa dicatat di tahap ini.")
        _svc_fee_locked(con, o) if diff else None
        new_item = data.get("master_item_id") or o["master_item_id"]
        if mode == "pabrik":
            if diff:
                _svc_add_diff_line(con, order_id, diff, diff_cost, medium, data.get("diff_note"), by)
            cur = con.execute("SELECT id FROM service_replacements WHERE order_id=? AND status='menunggu'", (order_id,)).fetchone()
            name = (data.get("product_name") or "").strip()[:160] or None
            if cur:
                con.execute("UPDATE service_replacements SET master_item_id=?, product_name=COALESCE(?, product_name) WHERE id=?", (new_item, name, cur["id"]))
            else:
                con.execute("""INSERT INTO service_replacements (order_id, mode, status, old_serial, master_item_id, product_name, created_by, created_at)
                               VALUES (?, 'pabrik', 'menunggu', ?, ?, ?, ?, ?)""", (order_id, o["serial_no"], new_item, name or o["product_name"], by, _now()))
            _svc_event(con, order_id, "pengganti", "Penggantian: pabrik mengirim unit pengganti" + (f"; selisih harga Rp {_sr_rupiah(diff)}" if diff else ""), by)
        else:
            if o["location"] not in _SVC_AT_SHOP:
                raise ValueError("Unit rusak customer harus ada di toko sebelum ditukar dari stok.")
            if _svc_swap(con, order_id):
                raise ValueError("Sudah ada pengajuan tukar dari stok untuk service ini.")
            if con.execute("SELECT 1 FROM service_replacements WHERE order_id=? AND mode='stok_toko' AND status='diterima'", (order_id,)).fetchone():
                raise ValueError("Unit ini sudah ditukar dari stok toko.")
            if o["receipt_item_id"]:
                ln = con.execute("SELECT quantity, returned_qty FROM receipt_items WHERE id=?", (o["receipt_item_id"],)).fetchone()
                if ln and (ln["returned_qty"] or 0) >= ln["quantity"]:
                    raise ValueError("Barang di nota ini sudah diretur / di-refund, jadi tidak bisa diganti lagi.")
            mi = con.execute("SELECT * FROM master_items WHERE id=?", (new_item,)).fetchone() if new_item else None
            if mi is None:
                raise ValueError("Pilih barang pengganti dari Master Barang.")
            note = (data.get("request_note") or "").strip()[:300] or None
            if o["warranty_status"] != "aktif" and not note:
                raise ValueError(f"Status garansi unit ini {SVC_WARRANTY[o['warranty_status']]}: tulis alasan pengajuan. "
                                 "Penggantian gratisnya perlu persetujuan Owner.")
            rid = con.execute("""INSERT INTO service_replacements (order_id, mode, status, old_serial, master_item_id, product_name, request_note,
                                     requested_by, requested_at, price_diff, diff_cost, diff_medium, diff_note, created_by, created_at)
                                 VALUES (?, 'stok_toko', 'diajukan', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                              (order_id, o["serial_no"], mi["id"], mi["name"], note, by, _now(), diff, diff_cost, medium,
                               (data.get("diff_note") or "").strip()[:160] or None, by, _now())).lastrowid
            short = f"; stok sekarang {mi['stock_qty']}, menunggu pengganti" if mi["stock_qty"] < o["qty"] else ""
            _svc_event(con, order_id, "pengganti", f"Diajukan tukar dari stok toko: {mi['name']}" + (f" — {note}" if note else "")
                       + (f"; selisih harga Rp {_sr_rupiah(diff)}" if diff else "") + short, by)
            if user.get("role") == "admin" and (o["warranty_status"] == "aktif" or user.get("is_owner")):
                _svc_decide_swap(con, o, rid, "setuju", "Diajukan admin, langsung disetujui", user)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def _svc_decide_swap(con, o: dict, swap_id: int, decision: str, note: str | None, user: dict) -> None:
    by = user.get("username")
    sw = dict(con.execute("SELECT * FROM service_replacements WHERE id=?", (swap_id,)).fetchone())
    if decision == "setuju":
        override = o["warranty_status"] != "aktif"
        if override and not user.get("is_owner"):
            raise PermissionError(f"Status garansi unit ini {SVC_WARRANTY[o['warranty_status']]}. "
                                  "Hanya Owner yang boleh menyetujui penggantian gratisnya.")
        if sw["price_diff"]:
            _svc_fee_locked(con, o)
        n = con.execute("""UPDATE service_replacements SET status='disetujui', decided_by=?, decided_at=?, decision_note=?, owner_override=?
                           WHERE id=? AND status='diajukan'""", (by, _now(), note, 1 if override else 0, swap_id)).rowcount
        if n != 1:
            raise ValueError("Pengajuan ini sudah diputuskan.")
        if sw["price_diff"]:
            _svc_add_diff_line(con, o["id"], sw["price_diff"], sw["diff_cost"], sw["diff_medium"], sw["diff_note"], by)
        _svc_event(con, o["id"], "pengganti", "Tukar dari stok disetujui" + (" Owner (garansi tidak aktif)" if override else "")
                   + (f": {note}" if note else "") + ". Unit pengganti boleh dikeluarkan dari stok", by)
    else:
        if not note:
            raise ValueError("Tulis alasan penolakan.")
        n = con.execute("""UPDATE service_replacements SET status='ditolak', decided_by=?, decided_at=?, decision_note=?
                           WHERE id=? AND status='diajukan'""", (by, _now(), note, swap_id)).rowcount
        if n != 1:
            raise ValueError("Pengajuan ini sudah diputuskan.")
        _svc_event(con, o["id"], "pengganti", f"Tukar dari stok ditolak: {note}", by)


def decide_service_swap(order_id: int, data: dict, user: dict) -> dict:
    """Admin approves or rejects a staff request to swap from stock; an inactive warranty needs the Owner."""
    _svc_require_admin(user, "menyetujui tukar dari stok")
    decision = data.get("decision")
    if decision not in ("setuju", "tolak"):
        raise ValueError("Pilih Setujui atau Tolak.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        sw = _svc_swap(con, order_id)
        if sw is None or sw["status"] != "diajukan":
            raise ValueError("Tidak ada pengajuan tukar dari stok yang menunggu persetujuan.")
        _svc_decide_swap(con, o, sw["id"], decision, (data.get("note") or "").strip()[:300] or None, user)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def post_service_swap(order_id: int, data: dict, user: dict) -> dict:
    """Hands an approved swap out of stock, exactly once: stock OUT for the new unit (no sale, no revenue,
    no incentive) and the broken unit into Stok Karantina as 'service_klaim', ready for a Retur Supplier.
    With too little stock the swap stays approved (Menunggu Pengganti) and nothing moves."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        sw = _svc_swap(con, order_id)
        if sw is None or sw["status"] != "disetujui":
            raise ValueError("Belum ada tukar dari stok yang disetujui admin.")
        if o["stage"] in ("draft", "selesai", "dibatalkan", "siap_diambil") or o["location"] not in _SVC_AT_SHOP:
            raise ValueError("Unit pengganti hanya bisa dikeluarkan selagi unit rusaknya ada di toko.")
        mi = con.execute("SELECT * FROM master_items WHERE id=?", (sw["master_item_id"],)).fetchone()
        if mi is None:
            raise ValueError("Barang pengganti tidak ditemukan di Master Barang.")
        if mi["stock_qty"] < o["qty"]:
            raise ValueError(f"Stok {mi['name']} tinggal {mi['stock_qty']}. Status tetap Menunggu Pengganti sampai stok masuk.")
        serial = (data.get("new_serial") or "").strip()[:80] or None
        if serial and serial == o["serial_no"]:
            raise ValueError("Serial unit pengganti tidak boleh sama dengan serial unit rusak.")
        n = con.execute("UPDATE service_replacements SET status='diterima', new_serial=?, received_at=?, posted_by=? WHERE id=? AND status='disetujui'",
                        (serial, _now(), by, sw["id"])).rowcount
        if n != 1:
            raise ValueError("Unit pengganti ini sudah dikeluarkan.")
        new_qty = mi["stock_qty"] - o["qty"]
        con.execute("UPDATE master_items SET stock_qty=? WHERE id=?", (new_qty, mi["id"]))
        lid = con.execute("""INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                             VALUES (?, 'SERVICE_TUKAR', ?, ?, 'service_order', ?, ?, ?, ?)""",
                          (mi["id"], -o["qty"], new_qty, order_id, f"Unit pengganti untuk customer, service {o['service_no']}", _now(), by)).lastrowid
        cost = mi["avg_cost_price"] or mi["last_cost_price"] or 0
        lot = _quarantine_in(con, master_item_id=o["master_item_id"], product_name=o["product_name"], qty=o["qty"], condition="rusak",
                             source_type="service_klaim", source_ref=o["service_no"], source_id=str(order_id),
                             supplier_id=o["supplier_id"] or mi["supplier_id"], unit_cost=cost, candidate=True,
                             note=f"Unit customer {o['customer_name']} SN {o['serial_no'] or '-'}, klaim garansi ke supplier", by=by)
        con.execute("UPDATE service_replacements SET ledger_id=?, quarantine_lot_id=? WHERE id=?", (lid, lot, sw["id"]))
        con.execute("UPDATE service_replacements SET status='batal' WHERE order_id=? AND status='menunggu'", (order_id,))
        con.execute("UPDATE service_cost_lines SET status='batal' WHERE order_id=? AND status='usulan'", (order_id,))
        con.execute("""UPDATE service_orders SET result='diganti', serial_new=?, stage='siap_diambil', ready_at=?, location='di_toko' WHERE id=?""",
                    (serial, _now(), order_id))
        _svc_event(con, order_id, "pengganti", f"Ditukar dari stok toko: {mi['name']} (stok {mi['stock_qty']} → {new_qty}); "
                   f"unit rusak masuk Stok Karantina untuk klaim ke supplier", by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def record_service_return_shipment(order_id: int, data: dict, user: dict) -> dict:
    """The factory (or salesman) sends the unit / replacement back. Optional step before receiving it."""
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["location"] not in ("di_pabrik", "dibawa_sales"):
            raise ValueError("Unit ini tidak sedang di pabrik / dibawa sales.")
        awb = (data.get("awb") or "").strip().upper()[:60] or None
        courier = (data.get("courier") or "").strip()[:60] or None
        con.execute("""INSERT INTO service_shipments (order_id, direction, channel, courier, awb, handed_at, status, note, created_by, created_at)
                       VALUES (?, 'balik', ?, ?, ?, ?, 'dikirim', ?, ?, ?)""",
                    (order_id, "ekspedisi" if awb else "sales", courier, awb, (data.get("sent_at") or "").strip()[:16] or _svc_local_now(),
                     (data.get("note") or "").strip()[:300] or None, by, _now()))
        con.execute("UPDATE service_orders SET location='perjalanan_balik' WHERE id=?", (order_id,))
        repl = con.execute("SELECT id FROM service_replacements WHERE order_id=? AND status='menunggu'", (order_id,)).fetchone()
        if repl and awb:
            con.execute("UPDATE service_replacements SET return_awb=? WHERE id=?", (awb, repl["id"]))
        _svc_event(con, order_id, "kirim_balik", ("Unit pengganti" if repl else "Unit") + " dikirim balik ke toko"
                   + (f" lewat {courier}" if courier else "") + (f", resi {awb}" if awb else ""), by)
    return get_service_order(order_id)


def receive_service_back(order_id: int, data: dict, user: dict) -> dict:
    """The unit (or the factory's replacement) is back at the shop. QC passes → Siap Diambil; QC fails on a
    repaired / replaced unit → back to Menunggu Dikirim for a new claim."""
    by = user.get("username")
    qc = data.get("qc")
    if qc not in ("lulus", "gagal"):
        raise ValueError("Pilih hasil QC toko.")
    qc_note = (data.get("qc_note") or "").strip()[:500] or None
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["location"] not in ("di_pabrik", "dibawa_sales", "perjalanan_balik"):
            raise ValueError("Unit ini tidak sedang di luar toko.")
        if o["stage"] == "menunggu_customer":
            raise ValueError("Masih menunggu keputusan customer atas biaya.")
        at = (data.get("received_at") or "").strip()[:10] or shop_today().isoformat()
        repl = con.execute("SELECT * FROM service_replacements WHERE order_id=? AND status='menunggu'", (order_id,)).fetchone()
        diag = con.execute("SELECT * FROM service_diagnoses WHERE order_id=? AND is_current=1", (order_id,)).fetchone()
        result = o["result"]
        notes = [f"Unit tiba kembali di toko {at[8:10]}/{at[5:7]}/{at[:4]}"]
        con.execute("UPDATE service_shipments SET status='diterima', received_at=? WHERE order_id=? AND direction='balik' AND status='dikirim'",
                    (at, order_id))
        if repl:
            serial = (data.get("new_serial") or "").strip()[:80]
            if not serial and qc == "lulus":
                raise ValueError("Isi serial number unit pengganti.")
            con.execute("UPDATE service_replacements SET status='diterima', new_serial=?, received_at=?, qc_note=? WHERE id=?",
                        (serial or None, at, qc_note, repl["id"]))
            con.execute("UPDATE service_orders SET serial_new=? WHERE id=?", (serial or None, order_id))
            result = "diganti"
            notes.append(f"unit pengganti SN {serial or '-'}")
        elif result == "belum" and diag and diag["decision"] in ("perbaiki_gratis", "perbaiki_biaya"):
            result = "diperbaiki"
        elif result == "belum":
            raise ValueError("Catat hasil pemeriksaan pabrik dulu sebelum menerima unit kembali.")
        if qc == "gagal" and result in ("diperbaiki", "diganti"):
            con.execute("UPDATE service_orders SET location='kembali_di_toko', stage='menunggu_kirim', result='belum', qc_note=? WHERE id=?",
                        (qc_note, order_id))
            notes.append(f"QC toko gagal ({qc_note or '-'}); klaim ulang ke pabrik")
        else:
            con.execute("UPDATE service_orders SET location='kembali_di_toko', stage='siap_diambil', result=?, ready_at=?, qc_note=? WHERE id=?",
                        (result, _now(), qc_note, order_id))
            notes.append(f"QC: {qc_note or 'lulus'}; siap diambil")
        _svc_event(con, order_id, "kembali", "; ".join(notes), by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def stop_service_order(order_id: int, data: dict, user: dict) -> dict:
    """Rejected, cancelled or not processed: the reason is required and the unit stays tracked until the
    customer has it back. Cancelling outright is only possible while the unit has never left the shop."""
    _svc_require_admin(user, "mencatat penolakan / pembatalan service")
    by = user.get("username")
    kind, source, follow = data.get("kind"), data.get("source"), data.get("followup")
    reason = (data.get("reason") or "").strip()
    if kind not in SVC_STOP_KINDS:
        raise ValueError("Pilih jenis keputusan.")
    if source not in SVC_STOP_SOURCES:
        raise ValueError("Pilih asal keputusan.")
    if not reason:
        raise ValueError("Alasan wajib diisi.")
    if follow not in ("kembalikan", "periksa_ulang", "batal_sebelum_kirim"):
        raise ValueError("Pilih tindak lanjut.")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if o["stage"] in ("selesai", "dibatalkan"):
            raise ValueError("Dokumen ini sudah ditutup.")
        if kind == "salah_input":
            if o["stage"] != "draft":
                raise ValueError("Salah input hanya untuk draft. Dokumen yang sudah diterima dibatalkan dengan alasan lain.")
            con.execute("UPDATE service_orders SET stage='dibatalkan', result='dibatalkan', stop_kind=?, stop_source=?, stop_reason=?, stop_at=? WHERE id=?",
                        (kind, source, reason, _now(), order_id))
            _svc_event(con, order_id, "batal", f"Draft dibatalkan: {reason}", by)
            return get_service_order(order_id)
        if o["stage"] == "draft":
            raise ValueError("Draft dibatalkan dengan jenis Salah input.")
        if con.execute("SELECT 1 FROM service_replacements WHERE order_id=? AND mode='stok_toko' AND status='diterima'", (order_id,)).fetchone():
            raise ValueError("Unit sudah ditukar dari stok toko. Serahkan unit penggantinya ke customer.")
        if con.execute("UPDATE service_replacements SET status='batal' WHERE order_id=? AND status IN ('diajukan','disetujui')", (order_id,)).rowcount:
            _svc_event(con, order_id, "pengganti", "Pengajuan tukar dari stok dibatalkan karena service dihentikan", by)
        shipped = con.execute("SELECT 1 FROM service_shipments WHERE order_id=? AND direction='keluar' AND status<>'draft'", (order_id,)).fetchone()
        stop = (kind, source, reason, _now())
        if follow == "batal_sebelum_kirim":
            if shipped or o["location"] != "di_toko":
                raise ValueError("Unit sudah pernah keluar toko. Pilih Kembalikan Apa Adanya.")
            con.execute("UPDATE service_cost_lines SET status='batal' WHERE order_id=? AND status='usulan'", (order_id,))
            con.execute("DELETE FROM service_shipments WHERE order_id=? AND status='draft'", (order_id,))
            con.execute("""UPDATE service_orders SET stage='dibatalkan', result='dibatalkan', stop_kind=?, stop_source=?, stop_reason=?, stop_at=?,
                               ready_at=? WHERE id=?""", (*stop, _now(), order_id))
            _svc_event(con, order_id, "batal", f"Dibatalkan sebelum dikirim ({SVC_STOP_KINDS[kind]}): {reason}. Unit diserahkan kembali ke customer", by)
        elif follow == "periksa_ulang":
            if o["result"] != "ditolak" or o["location"] not in ("di_pabrik", "dibawa_sales"):
                raise ValueError("Pemeriksaan ulang hanya untuk unit yang ditolak dan masih di pabrik.")
            con.execute("UPDATE service_orders SET result='belum', stage='di_supplier' WHERE id=?", (order_id,))
            con.execute("UPDATE service_diagnoses SET is_current=0 WHERE order_id=?", (order_id,))
            esc = (data.get("escalation_no") or "").strip()[:60]
            _svc_event(con, order_id, "periksa_ulang", f"Minta pemeriksaan ulang ke pabrik{' (no. ' + esc + ')' if esc else ''}: {reason}", by)
        else:
            result = {"garansi_ditolak": "ditolak", "tidak_bisa": "ditolak", "customer_batal": "dibatalkan", "ambil_kembali": "dibatalkan",
                      "tolak_biaya": "apa_adanya", "tidak_rusak": "apa_adanya"}[kind]
            con.execute("UPDATE service_cost_lines SET status='batal' WHERE order_id=? AND status='usulan'", (order_id,))
            con.execute("UPDATE service_replacements SET status='batal' WHERE order_id=? AND status='menunggu'", (order_id,))
            con.execute("DELETE FROM service_shipments WHERE order_id=? AND status='draft'", (order_id,))
            if kind == "garansi_ditolak" and o["warranty_status"] != "ditolak":
                con.execute("UPDATE service_orders SET warranty_status='ditolak', warranty_note=? WHERE id=?", (reason, order_id))
            at_shop = o["location"] in _SVC_AT_SHOP
            con.execute(f"""UPDATE service_orders SET result=?, stage=?, stop_kind=?, stop_source=?, stop_reason=?, stop_at=?,
                                ready_at=CASE WHEN ? THEN ? ELSE ready_at END WHERE id=?""",
                        (result, "siap_diambil" if at_shop else "kembali", *stop, 1 if at_shop else 0, _now(), order_id))
            _svc_event(con, order_id, "stop", f"{SVC_STOP_KINDS[kind]} ({SVC_STOP_SOURCES[source].lower()}): {reason}. "
                       + ("Unit siap diserahkan kembali apa adanya" if at_shop else "Unit diminta dikembalikan apa adanya"), by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


# ---- Money: Nota Jasa Service ----

def create_service_fee_receipt(order_id: int, user: dict) -> dict:
    """K-S3: the approved service fee is billed on a new Kasir receipt (Nota Jasa Service) so the money goes
    through the normal payments, cash book and reports. The original sale is never edited. Spare parts that
    come from stock are 'barang' lines and leave stock through the receipt as usual."""
    by = user.get("username")
    with _conn() as con:
        o = _svc_get(con, order_id)
        if o["stage"] == "draft":
            raise ValueError("Simpan & terima dokumen dulu.")
        m = _svc_money(con, o)
        if m["fee_receipt"]:
            return {"receipt_id": m["fee_receipt"]["id"], "receipt_no": m["fee_receipt"]["receipt_no"], "existing": True}
        if any(l["status"] == "usulan" for l in m["lines"]):
            raise ValueError("Masih ada biaya yang menunggu persetujuan customer.")
        lines = [l for l in m["lines"] if l["status"] in ("disetujui", "otomatis")]
        if not lines:
            raise ValueError("Tidak ada biaya yang disetujui untuk ditagih.")
        plate = split_plate(o["plate"])
        if plate is None:
            raise ValueError("Isi plat mobil customer (format seperti B 1234 ABC) di data service supaya Nota Jasa bisa dibuat.")
    items = [{"sku": l["sku"], "product_name": (l["item_name"] if l["master_item_id"] else
                                                   f"{SVC_COST_KINDS[l['kind']]}{' - ' + l['description'] if l['description'] else ''}")[:120],
              "quantity": l["qty"], "unit_price": l["customer_price"], "item_type": "barang" if l["master_item_id"] else "jasa"} for l in lines]
    rid = str(uuid.uuid4())
    create_receipt(rid, *plate, items, 0, 0, None, o["customer_phone"] or "", o["customer_name"], None, "cash", o["customer_id"],
                   created_by=by, work_status="selesai",
                   extra={"work_notes": f"Nota Jasa Service {o['service_no']} · {o['product_name']}", "cashier_name": by})
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE receipts SET service_order_id=? WHERE id=?", (order_id, rid))
        con.execute("UPDATE service_orders SET fee_receipt_id=? WHERE id=?", (rid, order_id))
        no = con.execute("SELECT receipt_no FROM receipts WHERE id=?", (rid,)).fetchone()["receipt_no"]
        _log_receipt_event(con, rid, "service", f"Nota Jasa untuk service {o['service_no']}", by)
        _svc_event(con, order_id, "nota_jasa", f"Nota Jasa Service {no} dibuat (Rp {_sr_rupiah(sum(i['quantity'] * i['unit_price'] for i in items))})", by)
        _svc_refresh(con, order_id)
    return {"receipt_id": rid, "receipt_no": no, "existing": False}


# ---- Handover to the customer ----

def handover_service(order_id: int, data: dict, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        ok = o["stage"] == "siap_diambil" or (o["stage"] == "dibatalkan" and o["location"] in _SVC_AT_SHOP)
        if not ok:
            if o["location"] == "diserahkan":
                raise ValueError("Unit ini sudah diserahkan ke customer.")
            raise ValueError("Unit belum siap diambil.")
        _svc_refresh(con, order_id)
        m = _svc_money(con, _svc_get(con, order_id))
        unpaid = (data.get("unpaid_reason") or "").strip()
        if m["billable"] > 0 and m["remaining"] > 0:
            if not m["fee_receipt"]:
                raise ValueError(f"Buat Nota Jasa Service dulu (tagihan Rp {_sr_rupiah(m['billable'])}).")
            if not unpaid:
                raise ValueError(f"Sisa tagihan Rp {_sr_rupiah(m['remaining'])} di Nota Jasa {m['fee_receipt']['receipt_no']} belum dibayar.")
            _svc_require_admin(user, "menyerahkan unit sebelum lunas")
        is_cust = bool(data.get("picker_is_customer", True))
        pname = o["customer_name"] if is_cust else (data.get("picker_name") or "").strip()[:120]
        pphone = o["customer_phone"] if is_cust else normalize_phone(data.get("picker_phone"))
        if not pname or not pphone:
            raise ValueError("Isi nama dan HP orang yang mengambil.")
        returned = {int(x) for x in data.get("returned_accessory_ids") or []}
        acc = [dict(r) for r in con.execute("SELECT * FROM service_accessories WHERE order_id=? AND brought=1", (order_id,))]
        missing = [a["name"] for a in acc if a["id"] not in returned]
        miss_note = (data.get("missing_note") or "").strip()
        if missing and not miss_note:
            raise ValueError("Kelengkapan belum kembali: " + ", ".join(missing) + ". Tulis keterangannya.")
        if not _svc_photo_count(con, order_id, "ttd_customer", "serah_customer"):
            raise ValueError("Simpan tanda tangan penerima atau foto serah terima dulu.")
        for a in acc:
            con.execute("UPDATE service_accessories SET returned=?, note=CASE WHEN ? THEN ? ELSE note END WHERE id=?",
                        (1 if a["id"] in returned else 0, 0 if a["id"] in returned else 1, miss_note[:200], a["id"]))
        picked = (data.get("picked_up_at") or "").strip()[:16] or _svc_local_now()
        after = None
        if o["result"] == "diperbaiki" and o["after_warranty_days"]:
            base = date.fromisoformat(_svc_local_date(o["ready_at"]) or shop_today().isoformat())
            after = (base + timedelta(days=o["after_warranty_days"])).isoformat()
        n = con.execute(f"""UPDATE service_orders SET location='diserahkan', stage=CASE WHEN stage='dibatalkan' THEN 'dibatalkan' ELSE 'selesai' END,
                                picked_up_at=?, picked_up_by=?, picker_name=?, picker_phone=?, picker_is_customer=?, condition_out=?, handover_note=?,
                                after_warranty_until=?, unpaid_reason=? WHERE id=? AND location<>'diserahkan'""",
                        (picked, by, pname, pphone, 1 if is_cust else 0, (data.get("condition_out") or "").strip()[:300] or None,
                         (data.get("note") or "").strip()[:500] or None, after, unpaid or None, order_id)).rowcount
        if n != 1:
            raise ValueError("Unit ini sudah diserahkan ke customer.")
        note = f"Diserahkan ke {'customer' if is_cust else pname + ' (' + pphone + ')'}"
        if missing:
            note += f"; kelengkapan tidak kembali: {', '.join(missing)} ({miss_note})"
        if unpaid and m["remaining"]:
            note += f"; diserahkan sebelum lunas (sisa Rp {_sr_rupiah(m['remaining'])}): {unpaid}"
        if after:
            note += f"; garansi service s/d {after[8:10]}/{after[5:7]}/{after[:4]}"
        _svc_event(con, order_id, "serah", note, by)
        _svc_refresh(con, order_id)
    return get_service_order(order_id)


def add_service_note(order_id: int, note: str, user: dict) -> dict:
    note = (note or "").strip()
    if not note:
        raise ValueError("Catatan kosong.")
    with _conn() as con:
        _svc_get(con, order_id)
        _svc_event(con, order_id, "catatan", note[:1000], user.get("username"))
    return get_service_order(order_id)


# ---- Read ----

def _svc_flags(o: dict, extra: dict) -> list[str]:
    flags, today = [], shop_today()

    def days(iso):
        return (today - date.fromisoformat(iso[:10])).days if iso else 0
    if o["location"] == "di_pabrik" and not extra.get("has_current_diag") and days(extra.get("at_factory")) > 14:
        flags.append("Pabrik belum memberi hasil lebih dari 14 hari")
    if o["location"] == "di_ekspedisi" and days(extra.get("handed_at")) > 14:
        flags.append("Paket belum dikonfirmasi diterima pabrik lebih dari 14 hari")
    if o["stage"] == "menunggu_kirim" and o["handling"] == "sales" and extra.get("scheduled_at") and days(extra["scheduled_at"]) > 1:
        flags.append("Jadwal pengambilan sales sudah lewat")
    if o["stage"] == "menunggu_customer" and days(extra.get("asked_at")) > 7:
        flags.append("Customer belum menjawab estimasi lebih dari 7 hari")
    if o["stage"] == "siap_diambil" and days(_svc_local_date(o["ready_at"])) > 30:
        flags.append("Siap diambil lebih dari 30 hari")
    if o["stage"] == "kembali" and days(o["stop_at"]) > 14 and o["location"] != "perjalanan_balik":
        flags.append("Unit belum dikirim balik lebih dari 14 hari")
    sw = extra.get("swap")
    if sw and sw["status"] == "disetujui" and (sw["stock_qty"] or 0) < o["qty"]:
        flags.append("Menunggu stok pengganti")
    if sw and sw["status"] == "diajukan" and days(_svc_local_date(sw["requested_at"])) > 1:
        flags.append("Pengajuan tukar dari stok belum diputuskan lebih dari 1 hari")
    tr = extra.get("supplier_track")
    if tr and not tr["final"] and (tr["age_days"] or 0) > 30:
        flags.append("Klaim ke supplier belum tuntas lebih dari 30 hari")
    return flags


def _svc_extra(con, order_id: int) -> dict:
    e = {}
    e["has_current_diag"] = con.execute("SELECT 1 FROM service_diagnoses WHERE order_id=? AND is_current=1", (order_id,)).fetchone() is not None
    r = con.execute("SELECT received_at, handed_at, scheduled_at FROM service_shipments WHERE order_id=? AND direction='keluar' ORDER BY id DESC LIMIT 1",
                    (order_id,)).fetchone()
    e["at_factory"] = r["received_at"] if r else None
    e["handed_at"] = r["handed_at"] if r else None
    e["scheduled_at"] = r["scheduled_at"] if r else None
    a = con.execute("SELECT MIN(created_at) AS t FROM service_approvals WHERE order_id=? AND id > COALESCE((SELECT MAX(id) FROM service_approvals WHERE order_id=? AND status<>'menunggu'), 0)",
                    (order_id, order_id)).fetchone()
    d = con.execute("SELECT created_at FROM service_diagnoses WHERE order_id=? AND is_current=1", (order_id,)).fetchone()
    e["asked_at"] = (a["t"] if a and a["t"] else None) or (d["created_at"] if d else None)
    e["swap"] = _svc_swap(con, order_id)
    e["supplier_track"] = _svc_supplier_track(con, order_id)
    return e


def _svc_next_step(o: dict, m: dict, repl: dict | None, swap: dict | None = None) -> str:
    st, loc = o["stage"], o["location"]
    if swap and swap["status"] == "diajukan":
        return "Menunggu persetujuan admin untuk tukar dari stok" + (" (garansi tidak aktif: Owner)" if o["warranty_status"] != "aktif" else "")
    if swap and swap["status"] == "disetujui":
        return ("Keluarkan unit pengganti dari stok" if (swap["stock_qty"] or 0) >= o["qty"]
                else f"Menunggu stok pengganti {swap['item_name'] or ''} (stok {swap['stock_qty'] or 0})")
    if st == "draft":
        return "Lengkapi data lalu Simpan & Terima"
    if st == "diterima":
        return "Pilih jalur penanganan"
    if st == "pengecekan":
        return "Catat hasil pemeriksaan / perbaikan di toko"
    if st == "menunggu_kirim":
        return "Konfirmasi diambil sales" if o["handling"] == "sales" else "Catat pengiriman lalu Konfirmasi Serah Ekspedisi"
    if st == "di_supplier":
        if loc in ("di_ekspedisi", "dibawa_sales") and not o.get("_has_diag"):
            return "Catat pabrik menerima unit" if loc == "di_ekspedisi" else "Catat pabrik menerima / hasil pemeriksaan"
        if o["result"] == "ditolak":
            return "Catat tindak lanjut penolakan"
        if not o.get("_has_diag"):
            return "Catat hasil pemeriksaan pabrik"
        if loc == "perjalanan_balik":
            return "Terima unit kembali di toko"
        return "Tunggu unit " + ("pengganti " if repl else "") + "dikirim balik, lalu terima di toko"
    if st == "menunggu_customer":
        return "Catat persetujuan customer"
    if st == "kembali":
        return "Terima unit kembali di toko" if loc == "perjalanan_balik" else "Catat unit dikirim balik / terima di toko"
    if st == "siap_diambil":
        if m["billable"] > 0 and not m["fee_receipt"]:
            return "Buat Nota Jasa Service, lalu serah ke customer"
        if m["remaining"] > 0:
            return "Tunggu pembayaran Nota Jasa, lalu serah ke customer"
        return "Hubungi customer lalu Serah Terima"
    if st == "dibatalkan" and loc in _SVC_AT_SHOP:
        return "Serahkan unit kembali ke customer"
    return ""


def get_service_order(order_id: int) -> dict:
    with _conn() as con:
        o = _svc_get(con, order_id)
        sup = con.execute("SELECT name, address, phone, contact_person FROM suppliers WHERE id=?", (o["supplier_id"],)).fetchone() if o["supplier_id"] else None
        o["supplier_name"] = sup["name"] if sup else None
        o["supplier_address"] = sup["address"] if sup else None
        o["supplier_phone"] = sup["phone"] if sup else None
        o["supplier_contact"] = sup["contact_person"] if sup else None
        prof = _svc_profile_row(con, o["profile_id"]) if o["profile_id"] else None
        o["profile"] = {k: prof[k] for k in ("id", "code", "name", "require_serial", "require_photo", "ask_reset", "checklist", "complaints",
                                              "est_days", "warranty_terms")} if prof else None
        o["accessories"] = [dict(r) for r in con.execute("SELECT * FROM service_accessories WHERE order_id=? ORDER BY position, id", (order_id,))]
        o["photos"] = [dict(r) for r in con.execute("SELECT id, stage, filename, mime, size, created_by, created_at FROM service_photos WHERE order_id=? ORDER BY id", (order_id,))]
        o["shipments"] = [dict(r) for r in con.execute("SELECT * FROM service_shipments WHERE order_id=? ORDER BY id", (order_id,))]
        for s in o["shipments"]:
            s["packing"] = json.loads(s["packing"]) if s["packing"] else {}
        o["diagnoses"] = [dict(r) for r in con.execute("SELECT * FROM service_diagnoses WHERE order_id=? ORDER BY id", (order_id,))]
        o["approvals"] = [dict(r) for r in con.execute("SELECT * FROM service_approvals WHERE order_id=? ORDER BY id", (order_id,))]
        o["replacements"] = [dict(r) for r in con.execute("SELECT * FROM service_replacements WHERE order_id=? ORDER BY id", (order_id,))]
        o["events"] = [dict(r) for r in con.execute("SELECT * FROM service_events WHERE order_id=? ORDER BY id", (order_id,))]
        m = _svc_money(con, o)
        o["cost_lines"] = m.pop("lines")
        o.update(m)
        o["receipt"] = None
        if o["receipt_id"]:
            r = con.execute("SELECT id, receipt_no, created_at, status FROM receipts WHERE id=?", (o["receipt_id"],)).fetchone()
            o["receipt"] = dict(r) if r else None
        lots = [dict(r) for r in con.execute("SELECT id, qty_left, quantity FROM quarantine_lots WHERE source_type='service_klaim' AND source_id=?", (str(order_id),))]
        o["quarantine_lots"] = lots
        o["supplier_returns"] = [dict(r) for r in con.execute(
            f"""SELECT DISTINCT r.id, r.return_number, r.doc_status FROM supplier_return_items i JOIN supplier_returns r ON r.id=i.return_id
                WHERE i.source_lot_id IN ({_placeholders(tuple(l['id'] for l in lots)) if lots else 'NULL'})""", tuple(l["id"] for l in lots))]
        e = _svc_extra(con, order_id)
        o["_has_diag"] = e["has_current_diag"]
        cur_repl = next((r for r in o["replacements"] if r["status"] == "menunggu"), None)
        o["swap"] = e["swap"]
        o["supplier_track"] = e["supplier_track"]
        o["next_step"] = _svc_next_step(o, m, cur_repl, e["swap"])
        o["flags"] = _svc_flags(o, e)
        o["rma_no"] = next((d["rma_no"] for d in reversed(o["diagnoses"]) if d["rma_no"]), None) or o["rma_no"]
        o["age_days"] = (shop_today() - date.fromisoformat(_svc_local_date(o["received_at"]))).days if o["received_at"] else None
        o["purchase_age"] = _svc_age(o["purchase_date"], date.fromisoformat(_svc_local_date(o["received_at"])) if o["received_at"] else None)
        o["history"] = [dict(r) for r in con.execute(
            """SELECT id, service_no, stage, result, product_name, received_at FROM service_orders
               WHERE id<>? AND service_no IS NOT NULL AND ((customer_id IS NOT NULL AND customer_id=?) OR (serial_no IS NOT NULL AND serial_no=?))
               ORDER BY id DESC LIMIT 10""", (order_id, o["customer_id"], o["serial_no"]))]
        o.update(stage_label=SVC_STAGES.get(o["stage"]), location_label=SVC_LOCATIONS.get(o["location"]), result_label=SVC_RESULTS.get(o["result"]),
                 warranty_label=SVC_WARRANTY.get(o["warranty_status"]), fee_label=SVC_FEE.get(o["fee_status"]),
                 handling_label=SVC_HANDLING.get(o["handling"]) if o["handling"] else None)
        del o["_has_diag"]
        return o


def list_service_orders(tab: str = "semua", query: str | None = None, stage: str | None = None, warranty: str | None = None,
                        fee: str | None = None, handling: str | None = None, supplier_id: int | None = None, page: int = 1) -> dict:
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT o.*, s.name AS supplier_name FROM service_orders o LEFT JOIN suppliers s ON s.id=o.supplier_id
               WHERE NOT (o.stage='dibatalkan' AND o.service_no IS NULL) ORDER BY COALESCE(o.received_at, o.created_at) DESC, o.id DESC""")]
        for r in rows:
            e = _svc_extra(con, r["id"])
            r["flags"] = _svc_flags(r, e)
            sh = con.execute("SELECT courier, awb, sales_name FROM service_shipments WHERE order_id=? AND status<>'draft' ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            r["last_ship"] = dict(sh) if sh else None
            r["swap"] = e["swap"]
            r["supplier_track"] = e["supplier_track"]
        kpi = {"aftersales": 0, "menunggu_tukar": 0, "dalam_proses": 0, "menunggu_kirim": 0, "di_supplier": 0, "menunggu_customer": 0, "siap_diambil": 0, "bermasalah": 0,
               "draft": 0, "total": 0}
        for r in rows:
            kpi["total"] += 1
            if r["stage"] == "draft":
                kpi["draft"] += 1
            if r["stage"] in SVC_OPEN:
                kpi["dalam_proses"] += 1
            if r["stage"] in ("menunggu_kirim", "diterima"):
                kpi["menunggu_kirim"] += 1
            if r["stage"] in ("di_supplier", "kembali"):
                kpi["di_supplier"] += 1
            if r["stage"] == "menunggu_customer":
                kpi["menunggu_customer"] += 1
            if r["stage"] == "siap_diambil" or (r["stage"] == "dibatalkan" and r["location"] in _SVC_AT_SHOP):
                kpi["siap_diambil"] += 1
            if r["flags"]:
                kpi["bermasalah"] += 1
            if r["supplier_track"] and not r["supplier_track"]["final"]:
                kpi["aftersales"] += 1
            if r["swap"]:
                kpi["menunggu_tukar"] += 1
        q = (query or "").strip().lower()
        if q:
            hits = set()
            qp = normalize_phone(q)
            for row in con.execute(
                    """SELECT order_id FROM service_shipments WHERE LOWER(COALESCE(awb,'')) LIKE ? UNION
                       SELECT order_id FROM service_diagnoses WHERE LOWER(COALESCE(rma_no,'')) LIKE ? UNION
                       SELECT id FROM service_orders WHERE LOWER(COALESCE(rma_no,'')) LIKE ?""", (f"%{q}%",) * 3):
                hits.add(row["order_id"])

            def match(r):
                if r["id"] in hits:
                    return True
                hay = " ".join(str(r.get(k) or "") for k in ("service_no", "customer_name", "plate", "receipt_no", "product_name", "serial_no",
                                                             "serial_new", "brand_name", "model", "supplier_name")).lower()
                return q in hay or (len(qp) >= 4 and qp in (r["customer_phone"] or ""))
            rows = [r for r in rows if match(r)]
        if tab == "perlu_tindakan":
            rows = [r for r in rows if r["flags"] or r["stage"] in ("diterima", "pengecekan", "menunggu_kirim", "menunggu_customer", "siap_diambil")
                    or (r["stage"] == "di_supplier" and r["result"] == "ditolak") or (r["stage"] == "dibatalkan" and r["location"] in _SVC_AT_SHOP)
            or r["swap"]]
        elif tab == "aktif":
            rows = [r for r in rows if r["stage"] in SVC_OPEN or r["stage"] == "draft"]
        elif tab == "selesai":
            rows = [r for r in rows if r["stage"] == "selesai"]
        elif tab == "dibatalkan":
            rows = [r for r in rows if r["stage"] == "dibatalkan"]
        elif tab == "aftersales":
            # Customer side may be done while the supplier claim for the swapped unit is still open (GAR-BR-017).
            rows = [r for r in rows if (r["supplier_track"] and not r["supplier_track"]["final"]) or r["swap"]]
        if stage:
            rows = [r for r in rows if r["stage"] == stage]
        if warranty:
            rows = [r for r in rows if r["warranty_status"] == warranty]
        if fee:
            rows = [r for r in rows if r["fee_status"] == fee]
        if handling:
            rows = [r for r in rows if r["handling"] == handling]
        if supplier_id:
            rows = [r for r in rows if r["supplier_id"] == supplier_id]
        total = len(rows)
        page = max(page, 1)
        rows = rows[(page - 1) * SVC_PAGE_SIZE: page * SVC_PAGE_SIZE]
        for r in rows:
            r.update(stage_label=SVC_STAGES.get(r["stage"]), location_label=SVC_LOCATIONS.get(r["location"]), result_label=SVC_RESULTS.get(r["result"]),
                     warranty_label=SVC_WARRANTY.get(r["warranty_status"]), fee_label=SVC_FEE.get(r["fee_status"]),
                     age_days=(shop_today() - date.fromisoformat(_svc_local_date(r["received_at"]))).days if r["received_at"] else None)
        return {"items": rows, "total": total, "page": page, "page_size": SVC_PAGE_SIZE, "kpi": kpi}


# ---- Photos ----

def _svc_upload_dir(order_id: int) -> Path:
    d = DB_PATH.parent / "uploads" / "services" / str(order_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _svc_shipped(con, o: dict) -> bool:
    """The current trip out is confirmed (photos of that trip are evidence now)."""
    return o["stage"] != "menunggu_kirim" and con.execute(
        "SELECT 1 FROM service_shipments WHERE order_id=? AND direction='keluar' AND status<>'draft'", (o["id"],)).fetchone() is not None


def _svc_photo_locked(o: dict, stage: str, shipped: bool) -> bool:
    if o["location"] == "diserahkan":
        return True
    if stage in _SVC_PHOTO_LOCK_RECEIVE and o["stage"] != "draft":
        return True
    return stage in _SVC_PHOTO_LOCK_OUT and shipped


def add_service_photo(order_id: int, stage: str, filename: str, mime: str, data: bytes, user: dict) -> dict:
    if stage not in SVC_PHOTO_STAGES:
        raise ValueError("Tahap foto tidak dikenal.")
    if mime not in SR_PHOTO_MIMES:
        raise ValueError("Foto harus JPG, PNG, atau WEBP.")
    if not data or len(data) > SR_PHOTO_MAX_BYTES:
        raise ValueError("Ukuran foto maksimal 6 MB.")
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        o = _svc_get(con, order_id)
        if _svc_photo_locked(o, stage, _svc_shipped(con, o)):
            raise ValueError("Tahap foto ini sudah terkunci.")
        name = f"{uuid.uuid4().hex}{SR_PHOTO_MIMES[mime]}"
        path = _svc_upload_dir(order_id) / name
        path.write_bytes(data)
        cur = con.execute("""INSERT INTO service_photos (order_id, stage, filename, mime, path, size, created_by, created_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (order_id, stage, (filename or name)[:120], mime, str(path), len(data), by, _now()))
        if o["stage"] != "draft":
            _svc_event(con, order_id, "foto", f"Foto {SVC_PHOTO_STAGES[stage].lower()} ditambahkan", by)
        return {"id": cur.lastrowid}


def get_service_photo(photo_id: int) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM service_photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        raise LookupError("Foto tidak ditemukan.")
    return dict(row)


def delete_service_photo(photo_id: int, user: dict) -> dict:
    by = user.get("username")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        ph = con.execute("SELECT * FROM service_photos WHERE id=?", (photo_id,)).fetchone()
        if ph is None:
            raise LookupError("Foto tidak ditemukan.")
        o = _svc_get(con, ph["order_id"])
        if _svc_photo_locked(o, ph["stage"], _svc_shipped(con, o)):
            raise ValueError("Foto ini sudah menjadi bukti dan tidak bisa dihapus.")
        con.execute("DELETE FROM service_photos WHERE id=?", (photo_id,))
        if o["stage"] != "draft":
            _svc_event(con, o["id"], "foto", f"Foto {SVC_PHOTO_STAGES.get(ph['stage'], ph['stage']).lower()} dihapus", by)
    try:
        Path(ph["path"]).unlink()
    except OSError:
        pass
    return get_service_order(ph["order_id"])


# ---- Print (Epson LX-310, 80 columns) ----

def _svc_wrap(prefix: str, text: str | None, width: int = 80) -> list[str]:
    text = " ".join((text or "-").split())
    out, room = [], width - len(prefix)
    while text:
        out.append((prefix if not out else " " * len(prefix)) + text[:room])
        text = text[room:]
    return out or [prefix + "-"]


def service_print_text(order_id: int, doc: str, user: dict) -> dict:
    o = get_service_order(order_id)
    if not o["service_no"]:
        raise ValueError("Simpan & terima dokumen dulu sebelum mencetak.")
    shop, rule = "SINAR GALAKSI VARIASI MOBIL", "-" * 80
    d = lambda s: (f"{s[8:10]}/{s[5:7]}/{s[0:4]}" if s and len(s) >= 10 else "")
    local = lambda iso: (datetime.fromisoformat(iso) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)).strftime("%d/%m/%Y %H:%M") if iso else ""
    acc = o["accessories"]
    box = lambda flag: "  ".join(f"[{'x' if a[flag] else ' '}] {a['name']}" for a in acc if a["brought"] or flag == "brought")
    L, copies = [], ["Lembar 1 - Customer", "Lembar 2 - Toko / arsip"]
    facts = None  # the same document as plain data, for the printed (graphic) layout of Surat Jalan / Nota Hasil
    gar = {"aktif": f"Aktif s/d {d(o['warranty_until'])}", "habis": f"Habis ({d(o['warranty_until'])})", "ditolak": "Ditolak pabrik",
           "belum": "Belum diverifikasi"}[o["warranty_status"]]
    if doc == "terima":
        L += [_sr_line(shop, "TANDA TERIMA SERVICE"), _sr_line("", f"No.  : {o['service_no']}"),
              _sr_line("", f"Tgl. : {local(o['received_at'])}"),
              _sr_line(f"Customer : {o['customer_name']}", f"Nota : {o['receipt_no'] or 'tanpa nota'}"),
              _sr_line(f"HP       : {o['customer_phone']}", f"Beli : {d(o['purchase_date']) or '-'}"),
              _sr_line(f"Mobil    : {(o['vehicle'] or '')[:24]}  {o['plate'] or ''}", f"Garansi: {gar}")]
        if o["warranty_status"] == "aktif":
            L.append(_sr_line("", "(indikasi, cek pabrik)"))
        L += [rule, f"Barang   : {o['product_name'][:34]}  Merek: {(o['brand_name'] or '-')[:12]}  Tipe: {(o['model'] or '-')[:14]}"[:80],
              f"No. Seri : {(o['serial_no'] or '-'):<22}Qty  : {o['qty']} {o['unit'] or 'PCS'}"[:80]]
        L += _svc_wrap("Kondisi  : ", o["condition_in"]) + _svc_wrap("Keluhan  : ", o["complaint"]) + ["Kelengkapan:"]
        # Fill each 80-column line with whole items and start a new line when the next one does not fit, so a long
        # name is never cut off at the paper edge (was: 5 per line, then clipped at 80).
        line = ""
        for item in (f"[{'x' if a['brought'] else ' '}] {a['name']}" for a in acc):
            for part in _svc_wrap("", item, 76):
                if line and len(line) + 2 + len(part) > 78:
                    L.append(line)
                    line = ""
                line = f"{line}  {part}" if line else f"  {part}"
        if line:
            L.append(line)
        L += [rule, "Biaya    : BELUM DITENTUKAN - menunggu hasil pemeriksaan. Customer dihubungi",
              "           dan diminta persetujuan sebelum ada biaya.", f"Estimasi : {o['est_days'] or '-'}"]
        if o["reset_consent"]:
            L.append("Catatan  : Customer setuju unit boleh di-reset (data/akun bisa hilang).")
        L.append(rule)
        L += ["* " + t.strip() for t in (o["terms_text"] or SVC_TERMS_DEFAULT).splitlines() if t.strip()]
        L += ["", "   Petugas Penerima,                                 Customer / Penyerah,", "", "",
              f"  ( {(o['received_by'] or '')[:16]:^16} )                              ( {(o['customer_name'] or '')[:18]:^18} )"]
        L += ["", "- - - - - - - - - - - - - - - - - - (potong: label unit) - - - - - - - - - - - - -",
              f"{o['service_no']}  {local(o['received_at'])[:5]}  {o['customer_name'][:20]}  {o['customer_phone']}",
              f"{o['serial_no'] or '-'}  {o['product_name'][:50]}"]
    elif doc == "label":
        L = [f"{o['service_no']}  {local(o['received_at'])[:5]}", f"{o['customer_name'][:24]}  {o['customer_phone']}",
             f"{o['serial_no'] or '-'}  {o['product_name'][:40]}"]
        copies = ["Label unit"]
    elif doc == "surat_jalan":
        sh = next((s for s in reversed(o["shipments"]) if s["direction"] == "keluar"), None)
        if sh is None:
            raise ValueError("Belum ada data pengiriman.")
        L += [_sr_line(shop, "SURAT JALAN SERVICE / GARANSI"), _sr_line("", f"No.  : {sh['doc_no'] or '(draft)'}"),
              _sr_line("", f"Tgl. : {d(sh['handed_at']) or d(shop_today().isoformat())}"),
              _sr_line(f"Kepada : {o['supplier_name'] or '-'}", f"Ref  : {o['service_no']}")]
        L += _svc_wrap("         ", (o["supplier_address"] or "")[:60])
        if sh["channel"] == "ekspedisi":
            L += [_sr_line(f"Up.    : {o['supplier_contact'] or o['pic'] or '-'}  {o['supplier_phone'] or ''}",
                           f"Kirim: {SVC_SHIP_MODES.get(sh['mode'] or '', '-')} - {sh['courier'] or '-'}"), _sr_line("", f"Resi : {sh['awb'] or '-'}")]
        else:
            L += [f"Diambil: {sh['sales_name'] or '-'} (sales) {sh['sales_phone'] or ''}  {d(sh['handed_at']) or d(sh['scheduled_at'])}"[:80]]
        claim = (f"Klaim, beli {d(o['purchase_date'])}" if o["warranty_status"] == "aktif" else "Service non-garansi")
        L += [rule, f"{'No':<3}{'Barang':<30}{'Qty':>4}  {'No. Seri':<16}Garansi", rule,
              f" 1 {o['product_name'][:29]:<30}{o['qty']:>4}  {(o['serial_no'] or '-')[:15]:<16}{claim[:26]}"[:80]]
        L += _svc_wrap("   Keluhan : ", o["complaint"]) + _svc_wrap("   Kondisi : ", o["condition_in"])
        sent = ", ".join(a["name"] for a in acc if a["sent"]) or "-"
        held = ", ".join(a["name"] for a in acc if a["brought"] and not a["sent"]) or "-"
        L += _svc_wrap("   Ikut    : ", sent) + _svc_wrap("   Ditahan di toko: ", held)
        if o["tech_note"]:
            L += _svc_wrap("   Catatan : ", o["tech_note"])
        pk = sh["packing"] or {}
        pack = " + ".join(x for x in (pk.get("kemasan"), pk.get("pelindung")) if x)
        facts = {"doc_no": sh["doc_no"] or "(draft)", "date": d(sh["handed_at"]) or d(sh["scheduled_at"]) or d(shop_today().isoformat()),
                 "supplier": o["supplier_name"] or "-", "address": o["supplier_address"] or "",
                 "contact": o["supplier_contact"] or o["pic"] or "", "phone": o["supplier_phone"] or "",
                 "channel": sh["channel"], "mode": SVC_SHIP_MODES.get(sh["mode"] or "", ""), "courier": sh["courier"] or "",
                 "awb": sh["awb"] or "", "sales_name": sh["sales_name"] or "", "sales_phone": sh["sales_phone"] or "",
                 "claim": claim, "sent": [a["name"] for a in acc if a["sent"]],
                 "held": [a["name"] for a in acc if a["brought"] and not a["sent"]], "tech_note": o["tech_note"] or "",
                 "koli": sh["koli"] or 1, "weight": sh["weight"] or "", "packing": pack,
                 "officer": sh["officer"] or o["received_by"] or ""}
        L += [rule, (f"Koli: {sh['koli'] or 1}" + (f"  Berat: {sh['weight']}" if sh["weight"] else "") + (f"  Kemasan: {pack}" if pack else ""))[:80],
              "Mohon hasil pemeriksaan & estimasi biaya dikabarkan sebelum dikerjakan.", "",
              "   Dikirim oleh,                Diperiksa,                 Diterima,", "", "",
              f"  ( {(sh['officer'] or o['received_by'] or '')[:16]:^16} )        (............)            (......................)"]
        copies = ["Lembar 1 - Supplier", "Lembar 2 - Toko / arsip"]
    elif doc == "hasil":
        if o["stage"] not in ("siap_diambil", "selesai", "dibatalkan"):
            raise ValueError("Nota hasil dicetak setelah unit siap diambil.")
        diag = next((x for x in reversed(o["diagnoses"]) if x["is_current"]), None)
        L += [_sr_line(shop, "NOTA HASIL SERVICE"), _sr_line("", f"No.  : {o['service_no']}"),
              _sr_line(f"Customer : {o['customer_name']}  {o['customer_phone']}", f"Selesai : {local(o['ready_at'])[:10]}"),
              _sr_line(f"Barang   : {o['product_name'][:40]}", f"Diambil : {o['picked_up_at'][8:10] + '/' + o['picked_up_at'][5:7] + '/' + o['picked_up_at'][:4] + ' ' + o['picked_up_at'][11:16] if o['picked_up_at'] else '-'}"),
              _sr_line(f"No. Seri : {o['serial_no'] or '-'}" + (f"  -> baru {o['serial_new']}" if o["serial_new"] else ""), f"Nota asal: {o['receipt_no'] or '-'}"),
              rule, f"Hasil    : {SVC_RESULTS[o['result']].upper()}"]
        if diag:
            L += _svc_wrap("Diagnosis: ", diag["description"] + (f" ({diag['rma_no']})" if diag["rma_no"] else ""))
        if o["stop_reason"]:
            L += _svc_wrap("Alasan   : ", o["stop_reason"])
        if o["qc_note"]:
            L += _svc_wrap("QC toko  : ", o["qc_note"])
        L += [rule]
        lines = [l for l in o["cost_lines"] if l["status"] in ("disetujui", "otomatis")]
        if lines:
            L.append(f"{'Rincian biaya':<64}{'Rp':>16}")
            for l in lines:
                name = l["item_name"] if l["master_item_id"] else f"{SVC_COST_KINDS[l['kind']]}{' - ' + l['description'] if l['description'] else ''}"
                L.append(f"  {name[:58]:<62}{_sr_rupiah(l['customer_price'] * l['qty']):>16}")
            L += [f"{'':<52}{'-' * 14:>28}", f"  {'Total':<62}{_sr_rupiah(o['billable']):>16}"]
            if o["fee_receipt"]:
                L.append(f"  {('Dibayar (Nota Jasa ' + o['fee_receipt']['receipt_no'] + ')'):<62}{_sr_rupiah(o['paid']):>16}")
            L.append(f"  {'Sisa':<62}{_sr_rupiah(o['remaining']):>16}")
        else:
            L.append("Biaya    : tanpa biaya")
        L.append(rule)
        L += _svc_wrap("Kelengkapan kembali: ", "  ".join(f"[{'x' if a['returned'] or o['location'] != 'diserahkan' else ' '}] {a['name']}" for a in acc if a["brought"]))
        if o["result"] == "diperbaiki":
            until = o["after_warranty_until"] or ((date.fromisoformat(_svc_local_date(o["ready_at"])) + timedelta(days=o["after_warranty_days"])).isoformat() if o["ready_at"] and o["after_warranty_days"] else None)
            if until:
                L.append(f"Garansi service: {o['after_warranty_days']} hari untuk kerusakan yang sama, s/d {d(until)}.")
        elif o["result"] == "diganti":
            # Owner 23-10-2026: the replacement keeps the original nota's warranty end date, no reset.
            L.append(f"Garansi unit pengganti mengikuti nota asli, s/d {d(o['warranty_until'])}." if o["warranty_until"]
                     else "Garansi unit pengganti mengikuti nota asli.")
        warranty_line = None
        if o["result"] == "diperbaiki" and until:
            warranty_line = f"Garansi service {o['after_warranty_days']} hari untuk kerusakan yang sama, s/d {d(until)}."
        elif o["result"] == "diganti":
            warranty_line = L[-1]
        facts = {"result": SVC_RESULTS[o["result"]], "ready": local(o["ready_at"])[:10] if o["ready_at"] else "",
                 "picked_up": local(o["picked_up_at"]) if o["picked_up_at"] else "",
                 "diagnosis": (diag["description"] + (f" ({diag['rma_no']})" if diag["rma_no"] else "")) if diag else "",
                 "stop_reason": o["stop_reason"] or "", "qc_note": o["qc_note"] or "",
                 "costs": [{"name": l["item_name"] if l["master_item_id"] else f"{SVC_COST_KINDS[l['kind']]}{' - ' + l['description'] if l['description'] else ''}",
                            "qty": l["qty"], "amount": l["customer_price"] * l["qty"]} for l in lines],
                 "billable": o["billable"], "paid": o["paid"], "remaining": o["remaining"],
                 "fee_receipt_no": o["fee_receipt"]["receipt_no"] if o["fee_receipt"] else "",
                 "returned": [{"name": a["name"], "back": bool(a["returned"] or o["location"] != "diserahkan")} for a in acc if a["brought"]],
                 "warranty": warranty_line, "handed_by": o["picked_up_by"] or "",
                 "receiver": o["picker_name"] or o["customer_name"] or "", "receiver_phone": o["picker_phone"] or ""}
        L += ["", "   Diserahkan (toko),                          Diterima (customer),", "", "",
              f"  ( {(o['picked_up_by'] or '')[:16]:^16} )                         ( {(o['picker_name'] or o['customer_name'] or '')[:18]:^18} )"]
    elif doc == "ganti":
        # Blueprint §2: proof of a free warranty replacement. Not a sales invoice: no revenue, nothing to pay.
        sw = next((r for r in reversed(o["replacements"]) if r["mode"] == "stok_toko" and r["status"] == "diterima"), None)
        if sw is None:
            raise ValueError("Bukti penggantian dicetak setelah unit pengganti dikeluarkan dari stok.")
        L += [_sr_line(shop, "BUKTI PENGGANTIAN GARANSI"), _sr_line("", f"No.  : {o['service_no']}"),
              _sr_line("", f"Tgl. : {local(sw['received_at'])}"),
              _sr_line(f"Customer : {o['customer_name']}", f"Nota asal: {o['receipt_no'] or 'tanpa nota'}"),
              _sr_line(f"HP       : {o['customer_phone']}", f"Tgl beli : {d(o['purchase_date']) or '-'}"),
              _sr_line(f"Mobil    : {(o['vehicle'] or '')[:24]}  {o['plate'] or ''}", ""), rule,
              f"Unit lama : {o['product_name'][:46]}  SN {o['serial_no'] or '-'}"[:80],
              f"Pengganti : {(sw['product_name'] or o['product_name'])[:46]}  SN {sw['new_serial'] or '-'}"[:80],
              f"Qty       : {o['qty']} {o['unit'] or 'PCS'}"]
        L += _svc_wrap("Keluhan   : ", o["complaint"])
        basis = ("Disetujui Owner (garansi " + SVC_WARRANTY[o["warranty_status"]].lower() + ")") if sw["owner_override"] \
            else f"Garansi aktif s/d {d(o['warranty_until'])}"
        L += [f"Dasar     : {basis}"[:80], rule, f"{'Dibayar customer untuk penggantian':<64}{'Rp 0':>16}"]
        if sw["price_diff"]:
            L.append(f"{'Selisih harga ganti model (ditagih di Nota Jasa)':<64}{'Rp ' + _sr_rupiah(sw['price_diff']):>16}")
        L += [rule, "* Dokumen ini bukan nota penjualan baru. Nota asal tidak berubah.",
              f"* Garansi unit pengganti mengikuti nota asal" + (f", s/d {d(o['warranty_until'])}." if o["warranty_until"] else "."),
              "* Unit lama menjadi milik toko untuk diklaim ke supplier.",
              "", "   Disetujui,              Diserahkan (toko),            Diterima (customer),", "", "",
              f"  ( {(sw['decided_by'] or '')[:14]:^14} )      ( {(sw['posted_by'] or '')[:14]:^14} )            ( {(o['customer_name'] or '')[:18]:^18} )"]
    else:
        raise ValueError("Dokumen cetak harus terima, label, surat_jalan, hasil, atau ganti.")
    with _conn() as con:
        _svc_event(con, order_id, "cetak", "Dicetak: " + {"terima": "Tanda Terima", "label": "Label Unit", "surat_jalan": "Surat Jalan Service",
                                                         "hasil": "Nota Hasil Service", "ganti": "Bukti Penggantian Garansi"}[doc], user.get("username"))
    return {"text": "\n".join(x[:80] for x in L), "copies": copies, "facts": facts}


# =====================================================================================================
# Master Kendaraan (K-15, decided 19-10-2026): a customer owns vehicles (plate, make, model, year, colour).
# Kasir, Sales Order and Service pick one of them. Documents keep their own plate/make/model snapshot;
# vehicle_id only links the document to the master row, so editing a vehicle never rewrites history.
# =====================================================================================================

def _veh_init(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            plate_full TEXT NOT NULL,
            make TEXT, model TEXT, year TEXT, color TEXT, notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, created_by TEXT,
            updated_at TEXT NOT NULL, updated_by TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicles_customer_plate ON vehicles(customer_id, plate_full);
        CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles(plate_full);
    """)
    for table in ("receipts", "preorders", "service_orders"):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if "vehicle_id" not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN vehicle_id INTEGER REFERENCES vehicles(id)")
    if not con.execute("SELECT 1 FROM app_migrations WHERE name='k15_vehicles_backfill'").fetchone():
        _veh_backfill(con)
        con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('k15_vehicles_backfill', ?)", (_now(),))


def _veh_backfill(con: sqlite3.Connection) -> None:
    """One-off: every customer + plate already seen on a nota becomes a vehicle, carrying the make/model/
    year/colour from the newest nota that had them. Only vehicle_id is set on old documents."""
    rows = con.execute(
        """SELECT customer_id, plate_full, vehicle_make, vehicle_model, vehicle_year, vehicle_color, created_at
           FROM receipts WHERE customer_id IS NOT NULL AND status<>'void' ORDER BY created_at ASC""").fetchall()
    for r in rows:
        _ensure_vehicle(con, r["customer_id"], r["plate_full"], r["vehicle_make"], r["vehicle_model"],
                        r["vehicle_year"], r["vehicle_color"], "sistem", newest=True, when=r["created_at"])
    for r in con.execute("SELECT id, customer_id, plate_full FROM receipts WHERE customer_id IS NOT NULL AND vehicle_id IS NULL").fetchall():
        plate = _veh_plate(r["plate_full"])
        if plate:
            v = con.execute("SELECT id FROM vehicles WHERE customer_id=? AND plate_full=?", (r["customer_id"], plate)).fetchone()
            if v:
                con.execute("UPDATE receipts SET vehicle_id=? WHERE id=?", (v["id"], r["id"]))
    con.execute("""UPDATE preorders SET vehicle_id=(SELECT r.vehicle_id FROM receipts r WHERE r.id=preorders.receipt_id)
                   WHERE vehicle_id IS NULL AND receipt_id IS NOT NULL""")
    for o in con.execute("SELECT id, customer_id, plate FROM service_orders WHERE customer_id IS NOT NULL AND vehicle_id IS NULL").fetchall():
        plate = _veh_plate(o["plate"])
        if plate:
            v = con.execute("SELECT id FROM vehicles WHERE customer_id=? AND plate_full=?", (o["customer_id"], plate)).fetchone()
            if v:
                con.execute("UPDATE service_orders SET vehicle_id=? WHERE id=?", (v["id"], o["id"]))


def _veh_plate(plate_full: str | None) -> str | None:
    """The canonical 'B 1234 ABC' form, or None for an empty/placeholder plate."""
    parts = split_plate(plate_full)
    return " ".join(parts) if parts else None


def _veh_clean(v, n: int) -> str | None:
    return (str(v).strip()[:n] or None) if v is not None else None


def _veh_year(v) -> str | None:
    y = _veh_clean(v, 4)
    if y is None:
        return None
    if not y.isdigit() or not (1950 <= int(y) <= shop_today().year + 1):
        raise ValueError(f"Tahun kendaraan harus 4 angka antara 1950 dan {shop_today().year + 1}.")
    return y


def _ensure_vehicle(con: sqlite3.Connection, customer_id: int | None, plate_full: str | None, make=None, model=None,
                    year=None, color=None, by: str | None = None, newest: bool = False, when: str | None = None) -> int | None:
    """Finds (or registers) this customer's vehicle with this plate. Blank fields on the master are filled from
    the document; with newest=True (backfill) the document's non-empty values win. A vehicle that was
    deactivated comes back when a new document uses it."""
    plate = _veh_plate(plate_full)
    if not customer_id or not plate:
        return None
    vals = {"make": _veh_clean(make, 40), "model": _veh_clean(model, 60), "color": _veh_clean(color, 30)}
    try:
        vals["year"] = _veh_year(year)
    except ValueError:
        vals["year"] = None
    now = when or _now()
    row = con.execute("SELECT * FROM vehicles WHERE customer_id=? AND plate_full=?", (customer_id, plate)).fetchone()
    if row is None:
        return con.execute(
            """INSERT INTO vehicles (customer_id, plate_full, make, model, year, color, created_at, created_by, updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, plate, vals["make"], vals["model"], vals["year"], vals["color"], now, by, now, by)).lastrowid
    sets = {k: v for k, v in vals.items() if v and (newest or not row[k]) and v != row[k]}
    if not row["is_active"] and not newest:
        sets["is_active"] = 1
    if sets:
        con.execute(f"UPDATE vehicles SET {', '.join(k + '=?' for k in sets)}, updated_at=?, updated_by=? WHERE id=?",
                    (*sets.values(), now, by, row["id"]))
    return row["id"]


def _link_receipt_vehicle(con: sqlite3.Connection, receipt_id: str, by: str | None) -> None:
    """Links a nota to its customer's vehicle (registering it when new) and fills the nota's own blank
    make/model/year/colour from the master so the printed nota shows them."""
    r = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if r is None or r["status"] == "void":
        return
    vid = _ensure_vehicle(con, r["customer_id"], r["plate_full"], r["vehicle_make"], r["vehicle_model"],
                          r["vehicle_year"], r["vehicle_color"], by)
    if vid is None:
        if r["vehicle_id"] is not None:
            con.execute("UPDATE receipts SET vehicle_id=NULL WHERE id=?", (receipt_id,))
        return
    v = con.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
    con.execute(
        """UPDATE receipts SET vehicle_id=?, vehicle_make=COALESCE(NULLIF(vehicle_make,''), ?),
               vehicle_model=COALESCE(NULLIF(vehicle_model,''), ?), vehicle_year=COALESCE(NULLIF(vehicle_year,''), ?),
               vehicle_color=COALESCE(NULLIF(vehicle_color,''), ?) WHERE id=?""",
        (vid, v["make"], v["model"], v["year"], v["color"], receipt_id))


def _link_preorder_vehicle(con: sqlite3.Connection, preorder_id: str) -> None:
    """A published Sales Order follows its invoice's vehicle; a draft is matched by phone + plate when the
    customer already has that vehicle (a draft registers nothing)."""
    po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None:
        return
    vid = None
    if po["receipt_id"]:
        r = con.execute("SELECT vehicle_id FROM receipts WHERE id=?", (po["receipt_id"],)).fetchone()
        vid = r["vehicle_id"] if r else None
    else:
        plate = _veh_plate(po["plate_full"])
        phone = normalize_phone(po["customer_phone"])
        if plate and phone:
            v = con.execute("""SELECT v.id FROM vehicles v JOIN customers c ON c.id=v.customer_id
                               WHERE c.phone=? AND v.plate_full=?""", (phone, plate)).fetchone()
            vid = v["id"] if v else None
    if vid != po["vehicle_id"]:
        con.execute("UPDATE preorders SET vehicle_id=? WHERE id=?", (vid, preorder_id))


def _split_vehicle_text(text: str | None) -> tuple[str | None, str | None]:
    """'Toyota Avanza G' -> ('Toyota', 'Avanza G'); Service keeps the vehicle as one free-text line."""
    words = (text or "").split()
    return (words[0], " ".join(words[1:]) or None) if words else (None, None)


_VEHICLE_SELECT = """SELECT v.*, c.first_name, c.last_name, c.phone AS customer_phone,
        (SELECT COUNT(*) FROM receipts r WHERE r.vehicle_id=v.id AND r.status<>'void') AS receipt_count,
        (SELECT MAX(r.created_at) FROM receipts r WHERE r.vehicle_id=v.id AND r.status<>'void') AS last_used_at,
        (SELECT COUNT(*) FROM service_orders s WHERE s.vehicle_id=v.id AND s.service_no IS NOT NULL) AS service_count
    FROM vehicles v JOIN customers c ON c.id=v.customer_id"""


def _vehicle_row(row) -> dict:
    d = dict(row)
    d["customer_name"] = _customer_full_name(d.pop("first_name"), d.pop("last_name"))
    d["label"] = " ".join(x for x in (d["make"], d["model"], d["year"]) if x) or None
    return d


def _veh_other_owners(con: sqlite3.Connection, plate: str, customer_id: int) -> list[dict]:
    rows = con.execute(f"{_VEHICLE_SELECT} WHERE v.plate_full=? AND v.customer_id<>? AND v.is_active=1", (plate, customer_id)).fetchall()
    return [{"vehicle_id": r["id"], "customer_id": r["customer_id"], "customer_name": _customer_full_name(r["first_name"], r["last_name"]),
             "customer_phone": r["customer_phone"]} for r in rows]


def get_vehicle(vehicle_id: int) -> dict:
    with _conn() as con:
        row = con.execute(f"{_VEHICLE_SELECT} WHERE v.id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise LookupError("Kendaraan tidak ditemukan.")
        d = _vehicle_row(row)
        d["other_owners"] = _veh_other_owners(con, d["plate_full"], d["customer_id"])
        return d


def list_customer_vehicles(customer_id: int, include_inactive: bool = False) -> list[dict]:
    with _conn() as con:
        if not con.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
            raise LookupError("Pelanggan tidak ditemukan.")
        rows = con.execute(
            f"""{_VEHICLE_SELECT} WHERE v.customer_id=? {'' if include_inactive else 'AND v.is_active=1'}
                ORDER BY v.is_active DESC, last_used_at IS NULL, last_used_at DESC, v.id DESC""", (customer_id,)).fetchall()
        return [_vehicle_row(r) for r in rows]


def find_vehicles_by_plate(plate_full: str) -> list[dict]:
    """Active vehicles with exactly this plate, with their owners (a sold car can sit under two customers)."""
    plate = _veh_plate(plate_full)
    if not plate:
        return []
    with _conn() as con:
        rows = con.execute(f"{_VEHICLE_SELECT} WHERE v.plate_full=? AND v.is_active=1 ORDER BY last_used_at DESC", (plate,)).fetchall()
        return [_vehicle_row(r) for r in rows]


def _veh_fields(data: dict) -> dict:
    return {"make": _veh_clean(data.get("make"), 40), "model": _veh_clean(data.get("model"), 60),
            "year": _veh_year(data.get("year")), "color": _veh_clean(data.get("color"), 30),
            "notes": _veh_clean(data.get("notes"), 300)}


def create_vehicle(customer_id: int, data: dict, by: str | None) -> dict:
    plate = _veh_plate(data.get("plate_full"))
    if not plate:
        raise ValueError("Plat kendaraan harus berformat seperti B 1234 ABC.")
    f = _veh_fields(data)
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
            raise LookupError("Pelanggan tidak ditemukan.")
        old = con.execute("SELECT * FROM vehicles WHERE customer_id=? AND plate_full=?", (customer_id, plate)).fetchone()
        if old is not None:
            if old["is_active"]:
                raise ValueError(f"Kendaraan {plate} sudah terdaftar untuk pelanggan ini.")
            con.execute("""UPDATE vehicles SET make=?, model=?, year=?, color=?, notes=?, is_active=1, updated_at=?, updated_by=? WHERE id=?""",
                        (f["make"], f["model"], f["year"], f["color"], f["notes"], now, by, old["id"]))
            vid = old["id"]
        else:
            vid = con.execute(
                """INSERT INTO vehicles (customer_id, plate_full, make, model, year, color, notes, created_at, created_by, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (customer_id, plate, f["make"], f["model"], f["year"], f["color"], f["notes"], now, by, now, by)).lastrowid
    return get_vehicle(vid)


def update_vehicle(vehicle_id: int, data: dict, by: str | None) -> dict:
    """Changes the master row only; notas, Sales Orders and Service documents keep the plate they were made with."""
    now = _now()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise LookupError("Kendaraan tidak ditemukan.")
        sets = {}
        if "plate_full" in data:
            plate = _veh_plate(data.get("plate_full"))
            if not plate:
                raise ValueError("Plat kendaraan harus berformat seperti B 1234 ABC.")
            if plate != row["plate_full"] and con.execute(
                    "SELECT 1 FROM vehicles WHERE customer_id=? AND plate_full=? AND id<>?", (row["customer_id"], plate, vehicle_id)).fetchone():
                raise ValueError(f"Kendaraan {plate} sudah terdaftar untuk pelanggan ini.")
            sets["plate_full"] = plate
        f = _veh_fields({**dict(row), **data})
        sets.update({k: v for k, v in f.items() if k in data})
        if "is_active" in data:
            sets["is_active"] = 1 if data["is_active"] else 0
        if not sets:
            raise ValueError("Tidak ada yang diubah.")
        con.execute(f"UPDATE vehicles SET {', '.join(k + '=?' for k in sets)}, updated_at=?, updated_by=? WHERE id=?",
                    (*sets.values(), now, by, vehicle_id))
    return get_vehicle(vehicle_id)
