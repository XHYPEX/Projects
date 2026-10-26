"""Replaces the whole Master Kategori with categories_seed.json (the revised 22 induk / 198 subkategori).

Every existing category is deleted. Barang (Master Barang) that pointed at an old category keep
everything else; their category is emptied so it can be chosen again from the new list. They are
listed before anything changes. The database file is copied first (scraper.db.bak-kategori-<time>),
and the whole replacement runs in one transaction: it either all happens or nothing does.

Run it from the repo root on the shop computer, with the app stopped:

    python scripts/replace_categories.py --dry-run     # show what would happen, change nothing
    python scripts/replace_categories.py               # do it
    python scripts/replace_categories.py --db path/to/scraper.db
"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402

SEED = Path(__file__).with_name("categories_seed.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="SQLite file to use instead of the app default")
    ap.add_argument("--dry-run", action="store_true", help="only report, change nothing")
    args = ap.parse_args()
    if args.db:
        db.DB_PATH = Path(args.db)
    if not db.DB_PATH.exists():
        print(f"Database tidak ditemukan: {db.DB_PATH}")
        return 1
    db.init_db()

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    parents, subs = seed["parents"], seed["subs"]
    parent_codes = {p["code"] for p in parents}
    for s in subs:
        if s["parent"] not in parent_codes:
            print(f"! Seed rusak: induk {s['parent']} untuk {s['code']} tidak ada.")
            return 1
    for c in [p["code"] for p in parents] + [s["code"] for s in subs]:
        if not db._CATEGORY_CODE_RE.match(c):
            print(f"! Kode tidak valid di seed: {c}")
            return 1

    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    old = con.execute("SELECT COUNT(*) AS n, SUM(parent_id IS NULL) AS p FROM categories").fetchone()
    items = con.execute(
        """SELECT mi.sku, mi.name, c.code, c.name AS cat FROM master_items mi
           JOIN categories c ON c.id = mi.category_id ORDER BY c.code, mi.name""").fetchall()
    print(f"Database: {db.DB_PATH}")
    print(f"Kategori lama: {old['n']} ({old['p'] or 0} induk) -> dihapus semua")
    print(f"Kategori baru: {len(parents)} induk, {len(subs)} subkategori")
    print(f"Barang yang kategorinya dikosongkan: {len(items)}")
    for it in items:
        print(f"  - {it['sku'] or '-'}  {it['name']}  (dulu {it['code']} {it['cat']})")
    if args.dry_run:
        print("Dry run: tidak ada yang diubah.")
        con.close()
        return 0

    backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.bak-kategori-{datetime.now():%Y%m%d-%H%M%S}")
    con.close()
    shutil.copy2(db.DB_PATH, backup)
    print(f"Backup: {backup}")

    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    now, by = db._now(), "import-revisi"
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE master_items SET category_id=NULL WHERE category_id IS NOT NULL")
        con.execute("DELETE FROM categories WHERE parent_id IS NOT NULL")
        con.execute("DELETE FROM categories")
        ids: dict[str, int] = {}
        for row in parents + subs:
            name = " ".join(row["name"].split()).upper()
            cur = con.execute(
                """INSERT INTO categories (code, name, name_normalized, parent_id, status, icon, description,
                                           created_by, created_at, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, 'aktif', NULL, NULL, ?, ?, ?, ?)""",
                (row["code"], name, db._category_name_key(name), ids.get(row.get("parent")), by, now, by, now),
            )
            ids[row["code"]] = cur.lastrowid
        con.commit()
    except Exception as e:
        con.rollback()
        print(f"GAGAL, tidak ada yang diubah: {e}")
        return 1
    finally:
        con.close()
    print(f"Selesai: {len(parents)} induk dan {len(subs)} subkategori dimasukkan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
