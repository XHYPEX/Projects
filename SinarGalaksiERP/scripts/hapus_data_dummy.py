"""One-time wipe of the shop's dummy data before real use (owner's request, 04-11-2026).

Run it through "Hapus Data Dummy.bat", which points DB_PATH at the shop's real database. By hand:

    DB_PATH=path/to/scraper.db python scripts/hapus_data_dummy.py

Owner 04-11-2026: delete everything except Master Brand and Master Kategori (Master Barang goes too) and restart
document numbering. Numbers come from MAX() over the emptied tables, so they restart by themselves; clearing
sqlite_sequence restarts the row ids as well.

Kept besides those two, because the app cannot run or would lose the shop's identity without them: login accounts,
app settings (company identity, policies), company bank accounts and the migration records. Every other table is
emptied, including any table added after this script was written. The next start of the app seeds Satuan (PCS, SET)
and the starter Service profiles again.

Safety: refuses while the app is running, asks for a typed confirmation, verifies a full backup before deleting,
deletes in one transaction, and refuses to run a second time on the same database, so a stray double-click after
go-live cannot wipe real data. Uploaded files are moved into the backup folder, not deleted.
"""

import os
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import PAYMENT_PROOF_DIR, PRODUCT_IMAGE_DIR  # noqa: E402

KEEP_TABLES = {"brands", "categories", "users", "app_settings", "company_bank_accounts", "app_migrations"}
DONE_MARKER = "dummy_data_wiped"
RESEED_MIGRATION = "service_profiles_seed"  # removed so the starter Service profiles come back on the next start
CONFIRM_PHRASE = "HAPUS SEMUA"

SUMMARY_LABELS = (
    ("master_items", "Master Barang"),
    ("suppliers", "Supplier"),
    ("customers", "Customer"),
    ("employees", "Karyawan"),
    ("receipts", "Nota Penjualan"),
    ("preorders", "Sales Order"),
    ("supplier_pos", "Purchase Order"),
    ("inbound_documents", "Barang Masuk"),
    ("purchase_invoices", "Faktur Pembelian"),
    ("service_orders", "Service"),
    ("sales_quotations", "Penawaran Harga"),
    ("stock_ledger", "Riwayat Stok"),
)


class Stop(Exception):
    """A reason not to continue, shown to the user as is."""


def tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def app_is_running() -> bool:
    port = int(os.environ.get("PORT", "8000"))
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_can_run(con: sqlite3.Connection) -> None:
    names = set(tables(con))
    if not {"receipts", "brands", "categories", "users"} <= names:
        raise Stop("File ini bukan database Sinar Galaksi ERP. Tidak ada yang diubah.")
    if con.execute("SELECT 1 FROM app_migrations WHERE name=?", (DONE_MARKER,)).fetchone():
        raise Stop("Data dummy sudah pernah dihapus di database ini. Demi keamanan data asli, "
                   "proses ini tidak bisa dijalankan dua kali. Tidak ada yang diubah.")


def show_summary(con: sqlite3.Connection, wipe: list[str]) -> None:
    print("  AKAN DIHAPUS (semua baris, %d tabel):" % len(wipe))
    for table, label in SUMMARY_LABELS:
        if table in wipe:
            print(f"    - {label:<20} {count(con, table):>7} baris")
    print(f"    - {'Total semua tabel':<20} {sum(count(con, t) for t in wipe):>7} baris")
    print()
    print("  TETAP ADA:")
    print(f"    - {'Master Brand':<20} {count(con, 'brands'):>7} baris")
    print(f"    - {'Master Kategori':<20} {count(con, 'categories'):>7} baris")
    print(f"    - {'Akun login':<20} {count(con, 'users'):>7} baris")
    print("    - Pengaturan Perusahaan dan rekening bank")
    print()
    print("  Satuan (PCS, SET) dan Profil Service kembali ke bawaan saat aplikasi dibuka lagi.")
    print("  Nomor dokumen mulai lagi dari awal.")
    print()


def make_backup(con: sqlite3.Connection, backup_db: Path, wipe: list[str]) -> None:
    backup_db.parent.mkdir(parents=True)
    copy = sqlite3.connect(str(backup_db))
    try:
        con.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Stop("Cadangan database rusak. Tidak ada yang dihapus.")
        for table in wipe + sorted(KEEP_TABLES):
            if count(copy, table) != count(con, table):
                raise Stop(f"Cadangan tidak sama dengan database (tabel {table}). Tidak ada yang dihapus.")
    finally:
        copy.close()


def wipe_tables(con: sqlite3.Connection, wipe: list[str]) -> None:
    kept_before = {t: count(con, t) for t in ("brands", "categories", "users")}
    con.execute("PRAGMA foreign_keys=OFF")  # no-op inside a transaction, so it comes first
    con.execute("BEGIN EXCLUSIVE")
    try:
        for table in wipe:
            con.execute(f'DELETE FROM "{table}"')
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            con.executemany("DELETE FROM sqlite_sequence WHERE name=?", [(t,) for t in wipe])
        con.execute("DELETE FROM app_migrations WHERE name=?", (RESEED_MIGRATION,))
        con.execute("INSERT INTO app_migrations (name, applied_at) VALUES (?, ?)",
                    (DONE_MARKER, datetime.now().isoformat(timespec="seconds")))

        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise Stop(f"Masih ada {len(broken)} data yang saling terkait. Tidak ada yang dihapus.")
        if any(count(con, t) != n for t, n in kept_before.items()):
            raise Stop("Data yang harus tetap ada ikut berubah. Tidak ada yang dihapus.")
        con.commit()
    except BaseException:
        con.rollback()
        raise


def move_uploads(backup_dir: Path, db_path: Path) -> list[str]:
    """Runs after the commit: a failure here must not look like the wipe failed, so it is only reported."""
    problems = []
    for source, name in ((db_path.parent / "uploads", "uploads"),
                         (PAYMENT_PROOF_DIR, "payment_proofs"),
                         (PRODUCT_IMAGE_DIR, "product_images")):
        if not source.exists():
            continue
        try:
            shutil.move(str(source), str(backup_dir / name))
        except OSError as exc:
            problems.append(f"{source}: {exc}")
    return problems


def main() -> None:
    # No default on purpose: this must never fall back to whichever database happens to sit in data/.
    raw_path = os.environ.get("DB_PATH")
    if not raw_path:
        raise Stop("DB_PATH belum diisi. Jalankan lewat file 'Hapus Data Dummy.bat'.")
    db_path = Path(raw_path)
    if not db_path.is_file():
        raise Stop(f"Database tidak ditemukan: {db_path}")
    if app_is_running():
        raise Stop("Aplikasi masih berjalan. Tutup jendela hitam Sinar Galaksi ERP dulu, lalu coba lagi.")

    con = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        check_can_run(con)
        wipe = [t for t in tables(con) if t not in KEEP_TABLES]
        print(f"  Database: {db_path}")
        print()
        show_summary(con, wipe)

        try:
            answer = input(f"  Ketik {CONFIRM_PHRASE} untuk lanjut (selain itu = batal): ")
        except EOFError:
            answer = ""
        print()
        if answer.strip() != CONFIRM_PHRASE:
            print("  Dibatalkan. Tidak ada yang diubah.")
            return

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = db_path.parent / f"cadangan-sebelum-hapus-dummy-{stamp}"
        print("  - Membuat cadangan...")
        make_backup(con, backup_dir / db_path.name, wipe)
        print("  - Menghapus data...")
        wipe_tables(con, wipe)
        print("  - Merapikan database...")
        con.execute("VACUUM")
    finally:
        con.close()

    print("  - Memindahkan file foto dan bukti bayar ke cadangan...")
    problems = move_uploads(backup_dir, db_path)

    print()
    print("  SELESAI.")
    print(f"  Cadangan: {backup_dir}")
    for problem in problems:
        print(f"  [!] File ini gagal dipindahkan, datanya sudah terhapus: {problem}")
    print()
    print("  Buka aplikasi lewat ikon biasa. Semua pengguna perlu login lagi.")
    print("  Kalau cadangan ingin dipulihkan: tutup aplikasi, salin scraper.db dari folder cadangan")
    print(f"  ke {db_path}, lalu kembalikan folder berikut ke tempat semula:")
    print(f"    uploads        -> {db_path.parent / 'uploads'}")
    print(f"    payment_proofs -> {PAYMENT_PROOF_DIR}")
    print(f"    product_images -> {PRODUCT_IMAGE_DIR}")


if __name__ == "__main__":
    try:
        main()
    except Stop as stop:
        print(f"  [!] {stop}")
        sys.exit(1)
