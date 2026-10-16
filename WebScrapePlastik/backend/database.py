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
        for col, ddl in (("ship_route", "TEXT"), ("priority", "TEXT NOT NULL DEFAULT 'normal'"), ("eta_date", "TEXT")):
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
        for col, ddl in (("spo_item_id", "INTEGER"), ("condition", "TEXT"), ("location", "TEXT")):
            if col not in ibi_cols:
                con.execute(f"ALTER TABLE inbound_items ADD COLUMN {col} {ddl}")
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
        """)
        for table, cols in (
            ("spo_receipts", (("gr_number", "TEXT"), ("officer", "TEXT"))),
            ("spo_receipt_lines", (("condition", "TEXT NOT NULL DEFAULT 'baik'"), ("location", "TEXT"))),
            ("spo_payments", (("reference_no", "TEXT"), ("bank_account", "TEXT"))),
            ("receipt_payments", (("reversed_of", "INTEGER"),)),
            ("preorder_payments", (("method", "TEXT NOT NULL DEFAULT 'cash'"),)),
        ):
            have = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, ddl in cols:
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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


PO_ARRIVED_STATUSES = ("sampai_di_toko", "terpasang")


def _receipt_item_holdings(items: list[dict], con: sqlite3.Connection | None = None) -> dict[int, int]:
    """Sum of quantity per master_item_id across a set of receipt items, skipping
    lines with no master item link. This is the 'holding' referenced throughout
    the stock-movement code: what a non-void receipt currently has taken off the
    shelf, per product. A line that comes from a Sales Order holds nothing until
    that PO line has arrived (sampai di toko / terpasang) -- pass `con` so it can look."""
    holdings: dict[int, int] = {}
    for item in items:
        master_item_id = item.get("master_item_id")
        if master_item_id is None or item.get("item_type") == "jasa":
            continue
        if con is not None and item.get("preorder_item_id"):
            po_line = con.execute("SELECT status FROM preorder_items WHERE id=?", (item["preorder_item_id"],)).fetchone()
            if po_line is None or po_line["status"] not in PO_ARRIVED_STATUSES:
                continue
        held = item["quantity"] - (item.get("returned_stock_qty") or 0)
        if held > 0:
            holdings[master_item_id] = holdings.get(master_item_id, 0) + held
    return holdings


def _apply_receipt_stock_delta(
    con: sqlite3.Connection, receipt_id: str, before: dict[int, int], after: dict[int, int]
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
    || (SELECT COUNT(*) FROM receipt_payments p WHERE p.receipt_id = r.id)"""

_RECEIPT_SELECT = f"""SELECT r.*,
        CASE WHEN c.id IS NOT NULL THEN {_CUSTOMER_FULL_NAME_SQL} ELSE r.customer_name END AS customer_full_name,
        {_RECEIPT_REVISION_SQL} AS revision,
        (SELECT p.id FROM preorders p WHERE p.receipt_id = r.id) AS preorder_id,
        (SELECT p.po_number FROM preorders p WHERE p.receipt_id = r.id) AS po_number
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


def _insert_receipt_payment(
    con: sqlite3.Connection, receipt_id: str, amount: int, method: str, note: str | None,
    created_by: str | None, transfer_sender: str | None = None, transfer_account: str | None = None,
    paid_at: str | None = None,
) -> None:
    if method != "transfer":
        transfer_sender = transfer_account = None
    con.execute(
        """INSERT INTO receipt_payments
               (receipt_id, paid_at, amount, method, transfer_sender, transfer_account, note, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (receipt_id, paid_at or _now(), amount, method, transfer_sender, transfer_account, note, created_by),
    )


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
        if pay["amount"] <= 0:
            raise ValueError("Hanya pembayaran masuk yang bisa dibatalkan.")
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


def add_receipt_payment(
    receipt_id: str, amount: int, method: str, note: str | None = None,
    transfer_sender: str | None = None, transfer_account: str | None = None, created_by: str | None = None,
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
        remaining = max(row["total"] - row["amount_paid"], 0)
        if amount <= 0:
            raise ValueError("Jumlah pembayaran harus lebih dari 0.")
        if amount > remaining:
            raise ValueError(f"Jumlah pembayaran melebihi sisa tagihan (Rp {remaining:,}).".replace(",", "."))
        new_paid = row["amount_paid"] + amount
        if method != "transfer":
            transfer_sender = transfer_account = None
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
) -> dict:
    plate_full = f"{plate_region} {plate_number} {plate_suffix}"
    if payment_method != "transfer":
        transfer_sender = transfer_account = None
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
                estimated_finish_at, vehicle_make, vehicle_model, vehicle_color, vehicle_year, draft_saved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
                subtotal, discount, total, created_at, started_at, final_status, amount_paid, payment_method,
                customer_id, receipt_no, transfer_sender, transfer_account, work_status,
                extra.get("work_notes"), extra.get("cashier_name"), extra.get("estimated_finish_at"),
                extra.get("vehicle_make"), extra.get("vehicle_model"), extra.get("vehicle_color"),
                extra.get("vehicle_year"), created_at if work_status == "draft" else None,
            ),
        )
        _remember_transfer_name(con, "sender", transfer_sender)
        _remember_transfer_name(con, "account", transfer_account)
        if amount_paid > 0 and final_status != "void":
            _insert_receipt_payment(con, receipt_id, amount_paid, payment_method, None, created_by,
                                    transfer_sender, transfer_account, created_at)
        resolved_items = [
            {**item, "master_item_id": _resolve_master_item_id(con, item.get("sku"))} for item in items
        ]
        con.executemany(
            """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date, master_item_id,
                                          item_type, line_discount, added_at, added_by, added_during_work)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            [
                (
                    receipt_id, item["product_name"], item["quantity"], item["unit_price"], item.get("warranty_date"),
                    item["master_item_id"], item.get("item_type") or "barang", item.get("line_discount") or 0,
                    created_at, created_by,
                )
                for item in resolved_items
            ],
        )

        # A brand-new receipt has no prior holding; a draft or a receipt created straight
        # into 'void' (the only status a caller can set directly) holds nothing.
        after_holdings = (
            _receipt_item_holdings(resolved_items, con) if _holds_stock(final_status, work_status) else {}
        )
        _apply_receipt_stock_delta(con, receipt_id, {}, after_holdings)
        _log_receipt_event(con, receipt_id, "created", {
            "draft": "Draft dibuat", "pengerjaan": "Nota dibuat, pengerjaan dimulai",
            "belum": "Nota dibuat, belum dikerjakan", "selesai": "Nota dibuat, pengerjaan selesai",
        }.get(work_status, "Nota dibuat"), created_by)

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
                   (SELECT p.id FROM preorders p WHERE p.receipt_id = r.id) AS preorder_id,
                   (SELECT p.po_number FROM preorders p WHERE p.receipt_id = r.id) AS po_number
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
    "vehicle_year",
)


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
                    con.execute(
                        """INSERT INTO receipt_items (id, receipt_id, product_name, quantity, unit_price, warranty_date,
                               master_item_id, item_type, line_discount, added_at, added_by, added_during_work, preorder_item_id,
                               returned_qty, returned_stock_qty)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (old["id"], receipt_id, item["product_name"], item["quantity"], item["unit_price"],
                         item.get("warranty_date"), item["master_item_id"], item.get("item_type") or "barang",
                         item.get("line_discount") or 0, old["added_at"], old["added_by"], old["added_during_work"],
                         old.get("preorder_item_id"), old.get("returned_qty") or 0, old.get("returned_stock_qty") or 0),
                    )
                else:
                    con.execute(
                        """INSERT INTO receipt_items (receipt_id, product_name, quantity, unit_price, warranty_date,
                               master_item_id, item_type, line_discount, added_at, added_by, added_during_work)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (receipt_id, item["product_name"], item["quantity"], item["unit_price"],
                         item.get("warranty_date"), item["master_item_id"], item.get("item_type") or "barang",
                         item.get("line_discount") or 0, now_iso, changed_by, during_work),
                    )
                    added_names.append(item["product_name"])
            if added_names and not is_draft:
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
        if amount_paid is not None and amount_paid != existing["amount_paid"] and new_status != "void":
            _insert_receipt_payment(
                con, receipt_id, amount_paid - existing["amount_paid"], new_payment_method,
                "Penyesuaian saat nota diubah", changed_by, new_transfer_sender, new_transfer_account,
            )

        con.execute(
            """UPDATE receipts SET plate_region=?, plate_number=?, plate_suffix=?, plate_full=?,
               customer_phone=?, customer_name=?, customer_id=?, subtotal=?, discount=?, total=?, status=?,
               amount_paid=?, payment_method=?, transfer_sender=?, transfer_account=?, work_status=?,
               work_notes=?, cashier_name=?, estimated_finish_at=?, vehicle_make=?, vehicle_model=?,
               vehicle_color=?, vehicle_year=?, draft_saved_at=? WHERE id=?""",
            (
                new_plate_region, new_plate_number, new_plate_suffix, new_plate_full,
                new_customer_phone, new_customer_name, new_customer_id,
                new_subtotal, new_discount, new_total, new_status, new_amount_paid, new_payment_method,
                new_transfer_sender, new_transfer_account, new_work_status,
                *(extra.get(k, existing[k]) for k in _RECEIPT_EXTRA_FIELDS),
                _now() if is_draft else existing["draft_saved_at"], receipt_id,
            ),
        )

        # Stock: holding after this change is nothing if the receipt ends up void
        # (covers a fresh void here, and keeps a receipt that was already void
        # from re-applying anything even if its items/discount were also sent in
        # the same request); otherwise it's whatever items are in effect now —
        # the ones just written, or the untouched existing ones.
        after_items = effective_items if effective_items is not None else existing_items
        after_holdings = (
            _receipt_item_holdings(after_items, con) if _holds_stock(new_status, new_work_status) else {}
        )
        _apply_receipt_stock_delta(con, receipt_id, before_holdings, after_holdings)
        if new_status == "void" and existing["status"] != "void":
            reason = (void_reason or "").strip() or None
            refund = existing["amount_paid"] or 0
            note = "Nota dibatalkan" + (f": {reason}" if reason else "")
            if refund > 0:
                note += f". Pembayaran {refund:,} dibalik; kembalikan ke pelanggan.".replace(",", ".")
            con.execute(
                "UPDATE receipts SET void_reason=?, voided_by=?, voided_at=? WHERE id=?",
                (reason, changed_by, _now(), receipt_id),
            )
            _log_receipt_event(con, receipt_id, "void", note, changed_by)

        updated = con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone()
        return dict(updated)


def settle_receipt(
    receipt_id: str, payment_method: str | None = None, amount: int | None = None, settled_by: str | None = None
) -> dict:
    """Mark a pending receipt paid in full: the remaining balance is recorded as
    paid (amount_paid = total), which makes the computed status 'done'. Touches
    only the payment columns -- items, totals and stock are exactly as they were.
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
        remaining = max(row["total"] - row["amount_paid"], 0)
        if amount is not None and amount != remaining:
            raise ValueError(f"Jumlah dibayar harus sama dengan sisa pembayaran (Rp {remaining:,}).".replace(",", "."))
        # status='pending' in the WHERE makes this a compare-and-set: a double click (or
        # two tills) that both read 'pending' above cannot both win -- the loser matches
        # no row and is told the receipt is already done instead of reporting a second success.
        cur = con.execute(
            "UPDATE receipts SET amount_paid=?, status=?, work_status='selesai', payment_method=? WHERE id=? AND status='pending'",
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
    receipt_id: str, amount: int, method: str, reason: str, by: str | None = None
) -> dict:
    """Kembalikan Kelebihan Bayar (admin): writes a negative payment row for money handed back, never
    more than the nota is overpaid. Messages: 'tidak ditemukan' -> 404, 'Jumlah'/'Alasan' -> 422, else 409."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Alasan refund wajib diisi (minimal 3 karakter).")
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
        _insert_receipt_payment(con, receipt_id, -amount, method, f"Refund kelebihan bayar: {reason}", by)
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
            "UPDATE receipts SET work_status='pengerjaan' WHERE id=? AND status='done' AND work_status='selesai'",
            (receipt_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
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
            if po_line is None or po_line["status"] not in PO_ARRIVED_STATUSES:
                raise ValueError("Barang PO ini belum sampai ke pelanggan. Batalkan di halaman Sales Order saja.")
        left = item["quantity"] - (item["returned_qty"] or 0)
        if quantity <= 0 or quantity > left:
            raise ValueError(f"Jumlah retur harus 1 sampai {left}.")
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
            _insert_receipt_payment(con, receipt_id, -cash_out, method, f"Refund retur {item['product_name']}: {reason}", by)
        now = _now()
        con.execute(
            """INSERT INTO receipt_returns (receipt_id, receipt_item_id, product_name, master_item_id, quantity, item_value,
                   refund_amount, cash_out, method, destination, reason, returned_by, returned_at,
                   kind, reason_code, restock_qty, supplier_qty, damaged_qty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, item_id, item["product_name"], item["master_item_id"], quantity, value, refund_amount,
             cash_out, method if cash_out else None, destination, reason_text, by, now,
             kind, reason_code, restock_qty, supplier_qty, damaged_qty),
        )
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


def change_receipt_work_status(
    receipt_id: str, to_status: str, note: str | None = None, changed_by: str | None = None
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
        items = [dict(r) for r in con.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id ASC", (receipt_id,)
        ).fetchall()]
        if current == "draft":
            if not items:
                raise ValueError("Nota belum punya item.")
            _apply_receipt_stock_delta(con, receipt_id, {}, _receipt_item_holdings(items, con))
        cur = con.execute(
            "UPDATE receipts SET work_status=?, draft_saved_at=NULL WHERE id=? AND work_status=?",
            (to_status, receipt_id, current),
        )
        if cur.rowcount == 0:
            raise ValueError("Nota ini baru saja berubah. Muat ulang lalu coba lagi.")
        labels = {"belum": "Belum Dikerjakan", "pengerjaan": "Dalam Pengerjaan", "selesai": "Pengerjaan Selesai"}
        text = f"Status: {labels[to_status]}" + (f" — {note}" if note else "")
        _log_receipt_event(con, receipt_id, "status", text, changed_by)
        return dict(con.execute(_RECEIPT_SELECT + " WHERE r.id=?", (receipt_id,)).fetchone())


def get_receipt_items(receipt_id: str) -> list[dict]:
    with _conn() as con:
        # LEFT JOIN so a line with no master item link (unresolved/hand-typed
        # SKU, or a historical row from before this link existed) still returns,
        # just with sku=NULL — reopening it for edit keeps that link, or lack of
        # one, as-is.
        rows = con.execute(
            """SELECT ri.*, mi.sku AS sku, mi.unit AS unit FROM receipt_items ri
               LEFT JOIN master_items mi ON mi.id = ri.master_item_id
               WHERE ri.receipt_id=? ORDER BY ri.id ASC""",
            (receipt_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_receipt(receipt_id: str) -> None:
    with _conn() as con:
        row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"Nota {receipt_id} tidak ditemukan.")
        if con.execute("SELECT 1 FROM preorders WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Invoice ini milik Sales Order dan tidak bisa dihapus. Void saja bila pesanan batal.")
        if con.execute("SELECT 1 FROM receipt_returns WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ValueError("Nota ini sudah ada retur barang, jadi tidak bisa dihapus.")
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


def get_sales_summary(date_from: str, date_to: str) -> dict:
    """Money in per day, by the date each payment was taken (shop's clock): a DP counts on the day it
    was paid, a pelunasan on its own day. Refunds/reversals (negative rows) net out; void notas are left out."""
    shift = f"{SHOP_UTC_OFFSET_MINUTES:+d} minutes"
    with _conn() as con:
        rows = con.execute(
            """SELECT date(p.paid_at, ?) AS day, SUM(p.amount) AS revenue, COUNT(DISTINCT p.receipt_id) AS receipt_count
               FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id
               WHERE r.status != 'void' AND date(p.paid_at, ?) BETWEEN ? AND ?
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
    entity_label = "Supplier" if table == "suppliers" else "Merek"
    supplier_details = (phone, address, contact_person, npwp)
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
            usage = _unit_usage(con).get(_unit_key(row["name"]), {"products": 0})
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
    return get_master_item_by_sku(code)


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
        preorder_paid = _preorder_paid_by_order(con)
        po_rows = [dict(r) for r in con.execute("SELECT * FROM preorders ORDER BY created_at DESC").fetchall()]
        po_inv = po_invoices(con, [r["receipt_id"] for r in po_rows])
        preorders_open = [
            _preorder_row(r, preorder_items_by_order.get(r["id"], []), preorder_paid.get(r["id"], 0), po_inv.get(r["receipt_id"]))
            for r in po_rows
        ]
        # Fully installed orders are finished business and drop off the dashboard.
        preorders_open = [r for r in preorders_open if r["status"] != PREORDER_STATUSES[-1]]

        # Sales this calendar month (paid receipts).
        month_start = date.today().replace(day=1).isoformat()
        shift = f"{SHOP_UTC_OFFSET_MINUTES:+d} minutes"
        sales_month = con.execute(
            "SELECT COALESCE(SUM(p.amount), 0) AS revenue, COUNT(DISTINCT p.receipt_id) AS c "
            "FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id "
            "WHERE r.status != 'void' AND date(p.paid_at, ?) >= ?",
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
    """BM-YYYYMMDD-NNN. (Older documents were numbered IN-YYYYMMDD-NNNN and keep that.)"""
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
            pending_sell_price, qty_in, cost_price, qty_remaining, received_date, spo_item_id, condition, location)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
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


def update_inbound_document(
    document_id: int,
    supplier_id: int,
    received_date: str,
    supplier_invoice_no: str | None,
    notes: str | None,
    items: list[dict],
    meta: dict | None = None,
    by: str | None = None,
) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(
                f"Dokumen barang masuk {document_id} tidak bisa diubah: statusnya '{row['status']}', hanya dokumen draft yang bisa diubah"
            )
        total_value = sum(i["qty_in"] * i["cost_price"] for i in items)
        con.execute(
            "UPDATE inbound_documents SET supplier_id=?, received_date=?, supplier_invoice_no=?, notes=?, total_value=?, "
            "supplier_po_id=?, surat_jalan_no=?, officer=?, source=?, manual_reason=?, reviewed_by=NULL, reviewed_at=NULL WHERE id=?",
            (supplier_id, received_date, supplier_invoice_no, notes, total_value,
             *(((meta or {}).get(k) or None) for k in _INBOUND_META), document_id),
        )
        con.execute("DELETE FROM inbound_items WHERE document_id=?", (document_id,))
        _insert_inbound_items(con, document_id, items)
        _inbound_event(con, document_id, "updated", "Draft diperbarui", by)
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


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


def _spo_apply_inbound_receipt(con: sqlite3.Connection, header, lines: list[dict], by: str | None) -> None:
    """Counts a posted Barang Masuk against its Purchase Order: qty_received, a GR record, PO events and status."""
    supplier_po_id = header["supplier_po_id"]
    now = _now()
    received_date = header["received_date"]
    gr_prefix = f"GR-{received_date.replace('-', '')}-"
    gr_n = con.execute("SELECT COUNT(*) AS n FROM spo_receipts WHERE gr_number LIKE ?", (gr_prefix + "%",)).fetchone()["n"] + 1
    gr_number = f"{gr_prefix}{gr_n:03d}"
    cur = con.execute(
        """INSERT INTO spo_receipts (supplier_po_id, received_date, surat_jalan_no, warehouse_location, note,
                                     inbound_document_id, created_by, created_at, gr_number, officer)
           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (supplier_po_id, received_date, header["surat_jalan_no"], header["notes"], header["id"], by, now, gr_number,
         header["officer"] or by),
    )
    total = 0
    for i in lines:
        con.execute("UPDATE supplier_po_items SET qty_received = qty_received + ? WHERE id=?", (i["qty_in"], i["spo_item_id"]))
        con.execute("INSERT INTO spo_receipt_lines (receipt_id, item_id, quantity, condition, location) VALUES (?, ?, ?, ?, ?)",
                    (cur.lastrowid, i["spo_item_id"], i["qty_in"], i.get("condition") or "baik", i.get("location")))
        total += i["qty_in"]
    _spo_finish_if_settled(con, supplier_po_id)
    _spo_event(con, supplier_po_id, "receive",
               f"Barang diterima {gr_number} ({total} pcs) lewat Barang Masuk {header['doc_number']}"
               + (f", surat jalan {header['surat_jalan_no']}" if header["surat_jalan_no"] else ""), by)
    _spo_sync_all(con, supplier_po_id, by, now)


def post_inbound_document(document_id: int, by: str | None = None) -> dict:
    with _conn() as con:
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if header["status"] != "draft":
            raise ValueError(
                f"Dokumen barang masuk {document_id} tidak bisa diposting: statusnya '{header['status']}', hanya dokumen draft yang bisa diposting"
            )

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
        items = [dict(r) for r in con.execute("SELECT * FROM inbound_items WHERE document_id=?", (document_id,)).fetchall()]
        if not items:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak bisa diposting: tidak ada baris barang")

        po_lines = [i for i in items if i.get("spo_item_id")]
        if header["supplier_po_id"] and po_lines:
            po_row = con.execute("SELECT status FROM supplier_pos WHERE id=?", (header["supplier_po_id"],)).fetchone()
            if po_row is None or po_row["status"] not in ("dipesan", "sebagian_diterima"):
                raise ValueError("Purchase Order terkait belum diterbitkan atau sudah selesai/dibatalkan.")
            for i in po_lines:
                spi = con.execute("SELECT * FROM supplier_po_items WHERE id=? AND supplier_po_id=?",
                                  (i["spo_item_id"], header["supplier_po_id"])).fetchone()
                if spi is None:
                    raise ValueError("Baris Purchase Order tidak ditemukan pada dokumen ini.")
                left = spi["quantity"] - spi["qty_cancelled"] - spi["qty_received"]
                if i["qty_in"] > left:
                    raise ValueError(f"Jumlah untuk “{spi['product_name']}” melebihi sisa pesanan PO ({left}).")

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
        # Barang Masuk counts it now, when the goods really become stock.
        if header["supplier_po_id"] and po_lines and not con.execute(
                "SELECT 1 FROM spo_receipts WHERE inbound_document_id=?", (document_id,)).fetchone():
            _spo_apply_inbound_receipt(con, header, po_lines, by)
        con.execute("UPDATE inbound_documents SET posted_by=?, posted_at=? WHERE id=?", (by, _now(), document_id))
        _inbound_event(con, document_id, "posted", f"Posting berhasil, stok bertambah {sum(i['qty_in'] for i in items)} pcs", by)
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


def void_inbound_document(document_id: int, reason: str, by: str | None = None) -> dict:
    with _conn() as con:
        header = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        if header is None:
            raise ValueError(f"Dokumen barang masuk {document_id} tidak ditemukan.")
        if header["status"] == "void":
            raise ValueError(f"Dokumen barang masuk {document_id} sudah dibatalkan")

        _inbound_event(con, document_id, "void", f"Dibatalkan: {reason}", by)
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
                    f"Dokumen {document_id} tidak bisa dibatalkan: stok barang SKU {item['sku']} sudah terpakai "
                    f"(sisa={item['qty_remaining']} dari jumlah masuk={item['qty_in']})"
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
                """INSERT INTO stock_ledger (master_item_id, type, qty, balance_after, ref_type, ref_id, note, created_at, created_by)
                   VALUES (?, 'VOID', ?, ?, 'inbound_document', ?, ?, ?, ?)""",
                (master_item_id, -item["qty_in"], new_stock_qty, document_id, reason, _now(), by),
            )

        # The receipt this document counted against its PO is taken back too, so the PO shows what is really in.
        rcpt = con.execute("SELECT * FROM spo_receipts WHERE inbound_document_id=?", (document_id,)).fetchone()
        if rcpt is not None:
            for ln in con.execute("SELECT item_id, quantity FROM spo_receipt_lines WHERE receipt_id=?", (rcpt["id"],)).fetchall():
                con.execute("UPDATE supplier_po_items SET qty_received = MAX(qty_received - ?, 0) WHERE id=?",
                            (ln["quantity"], ln["item_id"]))
            _spo_finish_if_settled(con, rcpt["supplier_po_id"])
            _spo_event(con, rcpt["supplier_po_id"], "receive",
                       f"Penerimaan {rcpt['gr_number'] or ''} dibatalkan (Barang Masuk {header['doc_number']} void)", by)
            _spo_sync_all(con, rcpt["supplier_po_id"], by, _now())

        con.execute("UPDATE inbound_documents SET status='void' WHERE id=?", (document_id,))
        updated = con.execute("SELECT * FROM inbound_documents WHERE id=?", (document_id,)).fetchone()
        return dict(updated)


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
        raise ValueError(f"Supplier {supplier_id} tidak ditemukan.")
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
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(
                f"Faktur pembelian {invoice_id} tidak bisa diubah: statusnya '{row['status']}', "
                "hanya faktur draft yang bisa diubah"
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
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] != "draft":
            raise ValueError(f"Faktur pembelian {invoice_id} sudah berstatus '{row['status']}'")
        item_count = con.execute(
            "SELECT COUNT(*) AS n FROM purchase_invoice_items WHERE invoice_id=?", (invoice_id,)
        ).fetchone()["n"]
        if item_count == 0:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak bisa diposting: tidak ada baris barang")
        con.execute(
            "UPDATE purchase_invoices SET status='posted', updated_at=? WHERE id=?", (_now(), invoice_id)
        )
        updated = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(updated)


def void_purchase_invoice(invoice_id: int, reason: str) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise ValueError(f"Faktur pembelian {invoice_id} tidak ditemukan.")
        if row["status"] == "void":
            raise ValueError(f"Faktur pembelian {invoice_id} sudah dibatalkan")
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


def record_purchase_invoice_payment(invoice_id: int, amount: int) -> dict:
    """Adds to amount_paid. A negative amount reverses an over-recorded payment;
    the running total is clamped to [0, total] either way."""
    with _conn() as con:
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
            raise ValueError(f"Username '{username}' sudah ada")
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
            raise ValueError(f"Pengguna {user_id} tidak ditemukan.")
        if row["role"] == "admin" and role != "admin" and row["is_active"] and _active_admin_count(con, user_id) == 0:
            raise ValueError("Admin terakhir tidak bisa diturunkan perannya")
        con.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, _now(), user_id))
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(updated)


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


def _shop_local_date(created_at: str) -> str:
    """The shop's wall-clock calendar date (YYYY-MM-DD) for a stored UTC timestamp."""
    local = datetime.fromisoformat(created_at) + timedelta(minutes=SHOP_UTC_OFFSET_MINUTES)
    return f"{local:%Y-%m-%d}"


def _next_po_no(con: sqlite3.Connection, po_date: str) -> str:
    """PO-YYYYMMDD-NNNN: a counter per PO date, restarting at 0001. Called inside the
    inserting transaction, so two saves can't take the same number."""
    prefix = f"PO-{po_date.replace('-', '')}-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(po_number, ?) AS INTEGER)) AS n FROM preorders WHERE po_number LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:04d}"


def _preorder_paid_by_order(con: sqlite3.Connection) -> dict[str, int]:
    return {
        r["preorder_id"]: r["paid"]
        for r in con.execute(
            "SELECT preorder_id, COALESCE(SUM(amount), 0) AS paid FROM preorder_payments GROUP BY preorder_id"
        ).fetchall()
    }


def _preorder_row(row: dict, items: list[dict], paid: int = 0, invoice: dict | None = None) -> dict:
    """`paid` is the sum of the order's own payments; the legacy preorders.deposit column is ignored.
    Once the PO has its invoice, paid and remaining are the invoice's (payments are taken there)."""
    total = sum(i["quantity"] * i["unit_price"] for i in items)
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
    }
    if invoice:
        out.update({
            "deposit": invoice["amount_paid"],
            "remaining": max(invoice["total"] - invoice["amount_paid"], 0),
            "receipt_no": invoice["receipt_no"], "invoice_total": invoice["total"],
            "invoice_status": invoice["status"], "invoice_work_status": invoice["work_status"],
        })
    return out


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


def _insert_preorder_item(con: sqlite3.Connection, preorder_id: str, item: dict, position: int, now: str) -> int:
    cur = con.execute(
        """INSERT INTO preorder_items
           (preorder_id, master_item_id, product_name, unit, quantity, unit_price, status, position, created_at,
            ship_route, priority, eta_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            preorder_id, item.get("master_item_id"), item["product_name"], item.get("unit"),
            item["quantity"], item["unit_price"], item.get("status") or PREORDER_STATUSES[0],
            position, now, *_preorder_item_logistics(item),
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
    """Every published Sales Order must have Purchase Order(s): whatever quantity of its lines no
    Purchase Order covers yet goes on a new draft, one per supplier (barang with no supplier go to
    UMUM). Lines already at the shop need none. Safe to call repeatedly: drafts count as ordered."""
    from uuid import uuid4
    po = con.execute("SELECT po_number, is_draft FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None or po["is_draft"]:
        return []
    groups: dict[int, list[dict]] = {}
    for r in _spo_candidate_rows(con, None, None):
        if r["preorder_id"] != preorder_id:
            continue
        sid = r["supplier_id"] if r["supplier_id"] is not None else _general_supplier(con)["id"]
        groups.setdefault(sid, []).append({
            "preorder_item_id": r["preorder_item_id"], "master_item_id": r["master_item_id"],
            "product_name": r["product_name"], "unit": r["unit"], "quantity": r["qty_left"],
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
    return created


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
) -> dict:
    """A deposit given at creation is recorded as the first payment ("DP awal"); on a published PO it
    is booked straight onto the PO's invoice, which is created here too."""
    now = _now()
    po_date = po_date or shop_today().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO preorders
               (id, po_number, po_date, plate_full, customer_name, customer_phone, deposit, notes,
                created_at, updated_at, is_draft)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (preorder_id, _next_po_no(con, po_date), po_date, plate_full, customer_name,
             customer_phone, notes, now, now, int(is_draft)),
        )
        if deposit > 0:
            con.execute(
                "INSERT INTO preorder_payments (preorder_id, paid_at, amount, note, created_by) VALUES (?, ?, ?, ?, ?)",
                (preorder_id, now, deposit, "DP awal", changed_by),
            )
        for position, item in enumerate(items):
            _insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by}, position, now)
        if deposit > 0 and deposit_method != "cash":
            con.execute("UPDATE preorder_payments SET method=? WHERE preorder_id=?", (deposit_method, preorder_id))
        # A published PO gets its invoice now; the DP lands on it.
        receipt_id = _po_invoice_sync(con, preorder_id, changed_by)
        if receipt_id:
            _po_move_payments_to_invoice(con, preorder_id, receipt_id, changed_by)
        _auto_supplier_pos_for_preorder(con, preorder_id, changed_by)
    return get_preorder(preorder_id)


def get_preorder(preorder_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            return None
        items = [dict(r) for r in con.execute(
            "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,)
        ).fetchall()]
        paid = con.execute(
            "SELECT COALESCE(SUM(amount), 0) AS paid FROM preorder_payments WHERE preorder_id=?", (preorder_id,)
        ).fetchone()["paid"]
        invoice = po_invoices(con, [row["receipt_id"]]).get(row["receipt_id"])
    return _preorder_row(dict(row), items, paid, invoice)


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
        items = [dict(r) for r in rows]
        # Two-way reference: which supplier orders carry each line (and how much of it has arrived).
        for it in items:
            links = con.execute(
                """SELECT sp.id AS supplier_po_id, sp.po_number, sp.status AS po_status, spi.quantity - spi.qty_cancelled AS qty_ordered,
                          spi.qty_received
                   FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                   WHERE spi.preorder_item_id=? AND sp.status != 'batal' AND spi.quantity - spi.qty_cancelled > 0
                   ORDER BY sp.created_at ASC""",
                (it["id"],),
            ).fetchall()
            it["supplier_pos"] = [dict(l) for l in links]
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
    preorder_id: str, amount: int, note: str | None = None, created_by: str | None = None, method: str = "cash"
) -> dict:
    now = _now()
    with _conn() as con:
        if con.execute("SELECT 1 FROM preorders WHERE id=?", (preorder_id,)).fetchone() is None:
            raise ValueError(f"Preorder {preorder_id} tidak ditemukan.")
        cur = con.execute(
            "INSERT INTO preorder_payments (preorder_id, paid_at, amount, note, created_by, method) VALUES (?, ?, ?, ?, ?, ?)",
            (preorder_id, now, amount, note, created_by, method),
        )
        con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
        return dict(con.execute("SELECT * FROM preorder_payments WHERE id=?", (cur.lastrowid,)).fetchone())


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
        "SELECT COALESCE(SUM(amount),0) AS n FROM preorder_payments WHERE preorder_id=?", (preorder_id,)
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
    rows = [r for r in rows if r.get("receipt_id") is None and r["status"] in ("sampai_di_toko", "terpasang")]
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
            raise ValueError("Item PO ini baru saja dipakai di nota lain. Muat ulang lalu coba lagi.")
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
            for pp in con.execute("SELECT amount, method FROM preorder_payments WHERE preorder_id=? ORDER BY id ASC", (preorder_id,)):
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


def _po_invoice_snapshot(con: sqlite3.Connection, preorder_id: str) -> dict[int, int] | None:
    """What the PO's invoice holds now, taken before a PO change so the stock move can be worked out."""
    row = con.execute("SELECT receipt_id FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    return _po_invoice_holdings(con, row["receipt_id"]) if row and row["receipt_id"] else None


def _po_create_invoice(con: sqlite3.Connection, po: dict, by: str | None) -> str:
    plate = split_plate(po["plate_full"])
    if plate is None:
        raise ValueError("Plat mobil di PO harus berformat seperti B 1234 ABC supaya invoicenya bisa dibuat.")
    from uuid import uuid4
    receipt_id = str(uuid4())
    created_at = _now()
    customer_id, phone, name = _resolve_receipt_customer(con, None, po["customer_phone"], po["customer_name"])
    con.execute(
        """INSERT INTO receipts
           (id, plate_region, plate_number, plate_suffix, plate_full, customer_phone, customer_name,
            subtotal, discount, total, created_at, started_at, status, amount_paid, payment_method, customer_id,
            receipt_no, work_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 'pending', 0, 'cash', ?, ?, 'menunggu')""",
        (receipt_id, *plate, " ".join(plate), phone, name, created_at, created_at, customer_id,
         _next_receipt_no(con, created_at)),
    )
    con.execute("UPDATE preorders SET receipt_id=? WHERE id=?", (receipt_id, po["id"]))
    _log_receipt_event(con, receipt_id, "created", f"Invoice dibuat dari Sales Order {po['po_number']}", by)
    return receipt_id


def _po_move_payments_to_invoice(con: sqlite3.Connection, preorder_id: str, receipt_id: str, by: str | None) -> int:
    """Payments recorded on the PO itself (a draft PO's DP, or a PO from before invoices existed)
    are booked on the invoice once, keeping their date and method, through the allocation ledger."""
    free = preorder_unallocated_dp(con, preorder_id)
    if free <= 0:
        return 0
    already = con.execute(
        "SELECT COALESCE(SUM(a.amount),0) AS n FROM preorder_payment_allocations a LEFT JOIN receipts r ON r.id=a.receipt_id "
        "WHERE a.preorder_id=? AND COALESCE(r.status,'')<>'void'", (preorder_id,),
    ).fetchone()["n"]
    skip, left = already, free
    for pp in con.execute("SELECT * FROM preorder_payments WHERE preorder_id=? ORDER BY id ASC", (preorder_id,)).fetchall():
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
            left -= take
        if left <= 0:
            break
    con.execute(
        "INSERT INTO preorder_payment_allocations (preorder_id, receipt_id, amount, allocated_at, allocated_by) VALUES (?, ?, ?, ?, ?)",
        (preorder_id, receipt_id, free, _now(), by),
    )
    return free


def _po_invoice_sync(
    con: sqlite3.Connection, preorder_id: str, by: str | None, before: dict[int, int] | None = None,
) -> str | None:
    """Makes the PO's invoice match the PO: creates it the first time, adds/changes/removes the PO
    lines, recomputes the totals, moves Menunggu Barang on once everything has arrived, and moves
    stock for lines that have arrived. Returns the invoice id (None for a draft PO)."""
    po = con.execute("SELECT * FROM preorders WHERE id=?", (preorder_id,)).fetchone()
    if po is None or po["is_draft"]:
        return None
    po = dict(po)
    receipt_id = po.get("receipt_id")
    created = False
    if not receipt_id:
        receipt_id = _po_create_invoice(con, po, by)
        created, before = True, {}
    rec = dict(con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone())
    if rec["status"] == "void":
        return receipt_id
    if before is None:
        before = _po_invoice_holdings(con, receipt_id)
    po_items = [dict(r) for r in con.execute(
        "SELECT * FROM preorder_items WHERE preorder_id=? ORDER BY position ASC, id ASC", (preorder_id,)
    ).fetchall()]
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
                       item_type, line_discount, added_at, added_by, added_during_work, preorder_item_id)
                   VALUES (?, ?, ?, ?, NULL, ?, 'barang', 0, ?, ?, ?, ?)""",
                (receipt_id, pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"], now, by,
                 0 if created or rec["work_status"] in ("menunggu", "belum") else 1, pi["id"]),
            )
            added.append(pi["product_name"])
        elif (ln["product_name"], ln["quantity"], ln["unit_price"], ln["master_item_id"]) != \
                (pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"]):
            if ln["returned_qty"]:
                raise ValueError(f"Barang {ln['product_name']} sudah pernah diretur, jadi tidak bisa diubah lagi di PO.")
            con.execute(
                "UPDATE receipt_items SET product_name=?, quantity=?, unit_price=?, master_item_id=? WHERE id=?",
                (pi["product_name"], pi["quantity"], pi["unit_price"], pi["master_item_id"], ln["id"]),
            )
            bits = []
            if ln["quantity"] != pi["quantity"]:
                bits.append(f"qty {ln['quantity']} -> {pi['quantity']}")
            if ln["unit_price"] != pi["unit_price"]:
                bits.append(f"harga {ln['unit_price']:,} -> {pi['unit_price']:,}".replace(",", "."))
            changed.append(f"{pi['product_name']}" + (f": {', '.join(bits)}" if bits else " diubah"))
    for ln in lines.values():
        if ln["returned_qty"]:
            raise ValueError(f"Barang {ln['product_name']} sudah pernah diretur, jadi tidak bisa dihapus dari PO.")
        con.execute("DELETE FROM receipt_items WHERE id=?", (ln["id"],))
        changed.append(f"{ln['product_name']} dibatalkan")
    if (added or changed) and not created and _is_closed(rec):
        raise ValueError("Invoice PO ini sudah lunas dan selesai (ditutup). Minta admin membukanya kembali dulu.")
    con.execute("UPDATE preorder_items SET receipt_id=? WHERE preorder_id=?", (receipt_id, preorder_id))

    items = [dict(r) for r in con.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt_id,)).fetchall()]
    subtotal = sum(_line_total(i) for i in items)
    discount = min(rec["discount"] or 0, subtotal)
    total = subtotal - discount - (rec.get("return_total") or 0)
    status = _compute_receipt_status(total, rec["amount_paid"], None)
    work = rec["work_status"]
    if work == "menunggu" and po_items and all(pi["status"] in PO_ARRIVED_STATUSES for pi in po_items):
        work = "belum"
    con.execute("UPDATE receipts SET subtotal=?, discount=?, total=?, status=?, work_status=? WHERE id=?",
                (subtotal, discount, total, status, work, receipt_id))
    tag = f" (PO {po['po_number']})"
    if added and not created:
        _log_receipt_event(con, receipt_id, "item_added", "Item ditambahkan: " + ", ".join(added) + tag, by)
    if changed:
        _log_receipt_event(con, receipt_id, "item_changed", "Item diubah: " + "; ".join(changed) + tag, by)
    if work != rec["work_status"]:
        _log_receipt_event(con, receipt_id, "status", "Status: Belum Dikerjakan — semua barang PO sudah sampai", by)
    if total < rec["amount_paid"] and total != rec["total"]:
        _log_receipt_event(
            con, receipt_id, "overpaid",
            f"Total invoice turun menjadi Rp {total:,}; kelebihan bayar Rp {rec['amount_paid'] - total:,} perlu dikembalikan ke pelanggan.".replace(",", "."),
            by,
        )
    _apply_receipt_stock_delta(con, receipt_id, before, _po_invoice_holdings(con, receipt_id))
    return receipt_id


def po_invoice_payment(
    preorder_id: str, amount: int, method: str, note: str | None, by: str | None,
    transfer_sender: str | None = None, transfer_account: str | None = None,
) -> str:
    """'Tambah Pembayaran' from the PO page lands on the PO's invoice. Returns the invoice id."""
    with _conn() as con:
        row = con.execute("SELECT receipt_id FROM preorders WHERE id=?", (preorder_id,)).fetchone()
        if row is None:
            raise ValueError("Preorder tidak ditemukan.")
        receipt_id = row["receipt_id"]
    if not receipt_id:
        raise ValueError("PO ini belum punya invoice (masih draft).")
    add_receipt_payment(receipt_id, amount, method, note, transfer_sender, transfer_account, by)
    return receipt_id


def po_invoices(con: sqlite3.Connection, receipt_ids: list[str]) -> dict[str, dict]:
    ids = [r for r in receipt_ids if r]
    if not ids:
        return {}
    return {r["id"]: dict(r) for r in con.execute(
        f"SELECT id, receipt_no, total, amount_paid, status, work_status FROM receipts WHERE id IN ({_placeholders(ids)})", ids
    ).fetchall()}


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


def get_all_preorders(query: str | None = None, status: str | None = None, draft: bool = False) -> list[dict]:
    """`status` filters on the computed order-level stage, so it is applied in
    Python rather than SQL — the value does not exist as a column. `draft` is a
    real column, so it's applied in SQL: False (the default) is the active
    list, True is the parked/archived one — the two never mix."""
    where_clauses = ["is_draft=?"]
    params: list = [1 if draft else 0]
    if query:
        where_clauses.append(
            "(customer_name LIKE ? OR customer_phone LIKE ? OR po_number LIKE ? OR plate_full LIKE ? OR id IN "
            "(SELECT preorder_id FROM preorder_items WHERE product_name LIKE ?))"
        )
        like = f"%{query}%"
        params.extend([like, like, like, like, like])
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
        paid_by_order = _preorder_paid_by_order(con)
        invoices = po_invoices(con, [r["receipt_id"] for r in rows])

    result = [_preorder_row(row, items_by_order.get(row["id"], []), paid_by_order.get(row["id"], 0),
                            invoices.get(row["receipt_id"])) for row in rows]
    if status:
        result = [r for r in result if r["status"] == status]
    return result


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
        before = _po_invoice_snapshot(con, preorder_id)
        if is_draft and not existing["is_draft"] and existing.get("receipt_id"):
            inv = con.execute("SELECT status, work_status FROM receipts WHERE id=?", (existing["receipt_id"],)).fetchone()
            if inv is not None and inv["status"] != "void" and not _is_closed(inv):
                raise ValueError("PO ini punya invoice yang masih aktif. Selesaikan atau void invoicenya dulu sebelum diarsipkan.")

        con.execute(
            """UPDATE preorders SET customer_name=?, customer_phone=?, plate_full=?, po_date=?, notes=?,
               updated_at=?, is_draft=? WHERE id=?""",
            (
                customer_name if customer_name is not None else existing["customer_name"],
                customer_phone if customer_phone is not None else existing["customer_phone"],
                plate_full if plate_full is not None else existing["plate_full"],
                po_date if po_date is not None else existing["po_date"],
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
                           quantity=?, unit_price=?, status=?, position=?, ship_route=?, priority=?, eta_date=? WHERE id=?""",
                        (
                            item.get("master_item_id"), item["product_name"], item.get("unit"),
                            item["quantity"], item["unit_price"], new_status, position,
                            *_preorder_item_logistics(item), item_id,
                        ),
                    )
                    if new_status != prev["status"]:
                        _log_preorder_status(con, preorder_id, item_id, item["product_name"],
                                             new_status, now, changed_by, prev["status"])
                else:
                    kept.add(_insert_preorder_item(con, preorder_id, {**item, "changed_by": changed_by}, position, now))

            for stale_id in set(current) - kept:
                con.execute("DELETE FROM preorder_status_history WHERE preorder_item_id=?", (stale_id,))
                con.execute("DELETE FROM preorder_items WHERE id=?", (stale_id,))

        # A published PO's invoice follows (and a draft that is published now gets its invoice).
        receipt_id = _po_invoice_sync(con, preorder_id, changed_by, before)
        if receipt_id:
            _po_move_payments_to_invoice(con, preorder_id, receipt_id, changed_by)
        _auto_supplier_pos_for_preorder(con, preorder_id, changed_by)
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
        if item["status"] != status:
            before = _po_invoice_snapshot(con, preorder_id)
            con.execute("UPDATE preorder_items SET status=? WHERE id=?", (status, item_id))
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
            _log_preorder_status(con, preorder_id, item_id, item["product_name"], status, now,
                                 changed_by, item["status"])
            _po_invoice_sync(con, preorder_id, changed_by, before)
        updated = dict(con.execute("SELECT * FROM preorder_items WHERE id=?", (item_id,)).fetchone())
    return updated


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
            "SELECT * FROM preorder_items WHERE preorder_id=? AND status<>?", (preorder_id, status)
        ).fetchall():
            con.execute("UPDATE preorder_items SET status=? WHERE id=?", (status, item["id"]))
            _log_preorder_status(con, preorder_id, item["id"], item["product_name"], status, now,
                                 changed_by, item["status"])
            changed += 1
        if changed:
            con.execute("UPDATE preorders SET updated_at=? WHERE id=?", (now, preorder_id))
            _po_invoice_sync(con, preorder_id, changed_by, before)
    return changed


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
    """PO-SUP-NNN: one running counter. (Older POs were numbered POS-YYYYMMDD-NNNN and keep that.)"""
    prefix = "PO-SUP-"
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(po_number, ?) AS INTEGER)) AS n FROM supplier_pos WHERE po_number LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:03d}"


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
    where = ["p.is_draft=0", "pi.status NOT IN ('sampai_di_toko','terpasang')"]
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
                   pi.ship_route, pi.priority, pi.eta_date, m.supplier_id, s.name AS supplier_name, COALESCE(m.last_cost_price, 0) AS est_unit_price
            FROM preorder_items pi
            JOIN preorders p ON p.id = pi.preorder_id
            LEFT JOIN (
                SELECT spi.preorder_item_id, SUM({_SPO_QTY_LEFT_SQL}) AS ordered
                FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
                WHERE sp.status IN ({_placeholders(_SPO_COUNTED)}){exclude_sql}
                GROUP BY spi.preorder_item_id
            ) o ON o.preorder_item_id = pi.id
            LEFT JOIN master_items m ON m.id = pi.master_item_id
            LEFT JOIN suppliers s ON s.id = m.supplier_id
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
    """None for a draft or cancelled PO: there is nothing to pay yet."""
    if po["status"] in ("draft", "batal"):
        return None
    paid = po["paid_amount"] or 0
    if grand_total > 0 and paid >= grand_total:
        return "lunas"
    if not po["supplier_invoice_no"] and paid <= 0:
        return "belum_ditagih"
    if po["due_date"] and po["due_date"] < shop_today().isoformat():
        return "lewat_jatuh_tempo"
    return "sebagian" if paid > 0 else "belum_bayar"


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
    }


def _spo_items(con: sqlite3.Connection, supplier_po_id: str) -> list[dict]:
    rows = con.execute(
        """SELECT spi.*, p.po_number AS customer_po_number, p.customer_name, p.plate_full
           FROM supplier_po_items spi
           LEFT JOIN preorder_items pi ON pi.id = spi.preorder_item_id
           LEFT JOIN preorders p ON p.id = pi.preorder_id
           WHERE spi.supplier_po_id=? ORDER BY spi.position ASC, spi.id ASC""",
        (supplier_po_id,),
    ).fetchall()
    po_stage = con.execute("SELECT stage FROM supplier_pos WHERE id=?", (supplier_po_id,)).fetchone()["stage"]
    out = []
    for r in rows:
        d = dict(r)
        d["item_status"] = _spo_item_effective_status(d, po_stage)
        out.append(d)
    return out


def _spo_item_effective_status(item: dict, po_stage: str | None) -> str:
    """Where one PO line is: derived once goods arrive or the line is cancelled, else what was set on it,
    else the PO's own stage."""
    live = item["quantity"] - item["qty_cancelled"]
    if live <= 0:
        return "dibatalkan"
    if item["qty_received"] >= live:
        return "diterima"
    if item["qty_received"] > 0:
        return "diterima_sebagian"
    return item.get("item_status") or (po_stage if po_stage in SPO_STAGES else SPO_STAGES[0])


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
            "SELECT * FROM spo_receipts WHERE supplier_po_id=? ORDER BY created_at ASC, id ASC", (supplier_po_id,)
        ).fetchall():
            lines = [dict(l) for l in con.execute(
                """SELECT l.item_id, l.quantity, l.condition, l.location, spi.product_name FROM spo_receipt_lines l
                   JOIN supplier_po_items spi ON spi.id = l.item_id WHERE l.receipt_id=?""", (r["id"],))]
            receipts.append({**dict(r), "lines": lines})
        shipments = [dict(r) for r in con.execute(
            "SELECT * FROM spo_item_shipments WHERE supplier_po_id=? ORDER BY at ASC, id ASC", (supplier_po_id,))]
        dr = con.execute("SELECT * FROM spo_receive_drafts WHERE supplier_po_id=?", (supplier_po_id,)).fetchone()
        receive_draft = {**json.loads(dr["payload"]), "saved_by": dr["saved_by"], "saved_at": dr["saved_at"]} if dr else None
    return {**po, **_spo_totals(po, items), "items": items, "events": events, "payments": payments,
            "receipts": receipts, "cancellations": cancellations, "shipments": shipments,
            "receive_draft": receive_draft}


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
        items_by_po: dict[str, list[dict]] = {}
        for item in con.execute("SELECT * FROM supplier_po_items").fetchall():
            items_by_po.setdefault(item["supplier_po_id"], []).append(dict(item))
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
        "faktur_belum_lunas": sum(1 for r in live if r["payment_status"] in ("belum_bayar", "sebagian", "lewat_jatuh_tempo")),
        "total_utang": sum(r["outstanding"] for r in live),
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
    if line is None:
        return
    agg = con.execute(
        f"""SELECT COALESCE(SUM(spi.quantity - spi.qty_cancelled), 0) AS ordered,
                   COALESCE(SUM(spi.qty_received), 0) AS received
            FROM supplier_po_items spi JOIN supplier_pos sp ON sp.id = spi.supplier_po_id
            WHERE spi.preorder_item_id=? AND sp.status IN ({_placeholders(_SPO_ISSUED)})""",
        (preorder_item_id, *_SPO_ISSUED),
    ).fetchone()
    if agg["received"] >= line["quantity"]:
        target = "sampai_di_toko"
    elif agg["ordered"] >= line["quantity"]:
        target = "sudah_dipesankan"
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
    return get_supplier_po(supplier_po_id)


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
    return get_supplier_po(supplier_po_id)


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
    return get_supplier_po(supplier_po_id)


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
) -> dict:
    """Penerimaan Barang. `receipts` is [{item_id, quantity}]: how many of each line arrived now.
    With create_inbound, a DRAFT Barang Masuk document is opened for the lines linked to a master
    item (stock still moves only when that document is posted)."""
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
        gr_prefix = f"GR-{received_date.replace('-', '')}-"
        gr_n = con.execute(
            "SELECT COUNT(*) AS n FROM spo_receipts WHERE gr_number LIKE ?", (gr_prefix + "%",)
        ).fetchone()["n"] + 1
        gr_number = f"{gr_prefix}{gr_n:03d}"
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
            if r["quantity"] > left:
                raise ValueError(f"Jumlah diterima untuk “{item['product_name']}” melebihi sisa ({left}).")
            con.execute("UPDATE supplier_po_items SET qty_received = qty_received + ? WHERE id=?",
                        (r["quantity"], r["item_id"]))
            con.execute("INSERT INTO spo_receipt_lines (receipt_id, item_id, quantity, condition, location) VALUES (?, ?, ?, ?, ?)",
                        (receipt_id, r["item_id"], r["quantity"], r.get("condition") or "baik",
                         r.get("location") or warehouse_location))
            got_lines.append((dict(item), r["quantity"]))
        _spo_finish_if_settled(con, supplier_po_id)
        total_got = sum(q for _, q in got_lines)
        _spo_event(con, supplier_po_id, "receive",
                   f"Barang diterima {gr_number} ({total_got} pcs)" + (f", surat jalan {surat_jalan_no}" if surat_jalan_no else ""),
                   changed_by)
        if create_inbound:
            by_master: dict[int, dict] = {}
            for item, qty in got_lines:
                if item["master_item_id"] is None:
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
                       (doc_number, supplier_id, received_date, supplier_invoice_no, notes, status, total_value, created_at)
                       VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
                    (doc_number, row["supplier_id"], received_date, row["supplier_invoice_no"], notes_txt,
                     sum(v["qty_in"] * v["cost_price"] for v in by_master.values()), now),
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
    money = _spo_money(po, _spo_items(con, supplier_po_id))
    if amount > money["outstanding"]:
        raise ValueError(f"Jumlah pembayaran melebihi sisa utang (Rp {money['outstanding']:,}).".replace(",", "."))
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
    _spo_event(con, supplier_po_id, "payment", f"Pembayaran Rp {amount:,}".replace(",", "."), by)


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


def resolve_spo_cancellation(
    supplier_po_id: str, cancellation_id: int, approved: bool, note: str | None = None, by: str | None = None
) -> dict:
    """The supplier's answer to a cancellation request. Approved: the quantity is dropped from the line
    (the bill shrinks, the PO may finish). Rejected: the request is closed and the line is untouched."""
    now = _now()
    with _conn() as con:
        c = con.execute(
            "SELECT * FROM spo_cancellations WHERE id=? AND supplier_po_id=?", (cancellation_id, supplier_po_id)
        ).fetchone()
        if c is None:
            raise LookupError("Permintaan pembatalan tidak ditemukan.")
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
            con.execute("UPDATE supplier_po_items SET qty_cancelled = qty_cancelled + ? WHERE id=?", (c["quantity"], c["item_id"]))
        con.execute(
            "UPDATE spo_cancellations SET status=?, resolved_by=?, resolved_at=?, resolution_note=? WHERE id=?",
            ("disetujui" if approved else "ditolak", by, now, (note or "").strip() or None, cancellation_id),
        )
        _spo_event(
            con, supplier_po_id, "cancel_item" if approved else "cancel_rejected",
            (f"Pembatalan disetujui supplier: {c['product_name']} x{c['quantity']}" if approved
             else f"Pembatalan ditolak supplier: {c['product_name']} x{c['quantity']} tetap dipesan")
            + (f" — {note.strip()}" if note and note.strip() else ""), by)
        if approved:
            _spo_finish_if_settled(con, supplier_po_id)
    return get_supplier_po(supplier_po_id)


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
            raise ValueError("Purchase Order ini sudah ada pembayaran, tidak bisa dibatalkan.")
        con.execute("UPDATE supplier_pos SET status='batal', updated_at=? WHERE id=?", (now, supplier_po_id))
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
_CATEGORY_CODE_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")


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
        raise ValueError("Kode hanya boleh huruf besar, angka dan tanda hubung (maks 30 karakter), contoh: KAT-LMP.")
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
