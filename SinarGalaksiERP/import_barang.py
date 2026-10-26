"""Imports a "LAPORAN MASTER BARANG" export (the old program's .ttx file) into Master Barang.

Run it through "Impor Barang.bat", which points DB_PATH at the shop's real database. By hand:

    DB_PATH=path/to/scraper.db python import_barang.py path/to/barang.ttx

Every item goes in under supplier UMUM and brand "Tanpa Merk", unit PCS; both can be changed per
item in Master Barang afterwards. Persediaan becomes opening stock ("Stok awal" in the stock
ledger), Std. Beli the cost price and Std. Jual the sell price.

Safe to run again: an item already in Master Barang under UMUM / Tanpa Merk is skipped, never
duplicated or overwritten. The database is backed up next to itself before anything is written.
"""

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402

SUPPLIER_NAME = "UMUM"
NO_BRAND_CODE = "NOB"  # the system "Tanpa Merk" brand, seeded by init_db
UNIT = "PCS"


def parse_ttx(path: Path) -> list[dict]:
    # The export is cp1252 text with a NUL byte around every line and tab-separated columns:
    # No. | Perincian | Persediaan | Std. Beli | Std. Jual | Min.Stok | Max.Stok
    text = path.read_bytes().replace(b"\x00", b"").decode("cp1252")
    rows = []
    for line in text.splitlines():
        cols = line.strip().split("\t")
        if len(cols) != 7 or not re.fullmatch(r"[\d,]+", cols[0].strip()):
            continue  # report title, date and header lines
        name = cols[1].strip()
        # Only the outer quotes are delimiters; a quote inside is part of the name (9" HEAD UNIT).
        if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
            name = name[1:-1]
        name = " ".join(name.split())
        if not name:
            continue

        def number(value: str) -> int:
            return round(float(value.strip().strip('"').replace(",", "") or 0))

        rows.append({
            "no": cols[0].strip(),
            "name": name,
            "stock": max(number(cols[2]), 0),
            "cost": max(number(cols[3]), 0),
            "sell": max(number(cols[4]), 0),
        })
    return rows


def find_or_create_supplier() -> dict:
    wanted = db._normalize_name(SUPPLIER_NAME)
    for supplier in db.get_all_suppliers():
        if supplier["name_normalized"] == wanted and supplier["is_active"]:
            return supplier
    return db.create_supplier(SUPPLIER_NAME, source="master")


def no_brand() -> dict:
    for brand in db.get_all_brands():
        if brand["code"] == NO_BRAND_CODE:
            return brand
    raise SystemExit("Merek 'Tanpa Merk' tidak ada di database. Buka aplikasi sekali, lalu coba lagi.")


def ensure_unit() -> None:
    if not any(u["name"].upper() == UNIT for u in db.get_all_units()):
        db.create_unit(UNIT)


def main() -> int:
    if len(sys.argv) != 2:
        print("Pakai: python import_barang.py <file barang.ttx>")
        return 2
    source = Path(sys.argv[1].strip().strip('"'))
    if not source.is_file():
        print(f"File tidak ditemukan: {source}")
        return 1

    rows = parse_ttx(source)
    if not rows:
        print("Tidak ada barang yang terbaca dari file ini. Pastikan ini file LAPORAN MASTER BARANG.")
        return 1
    print(f"Terbaca {len(rows)} barang dari {source.name}.")

    db.init_db()
    db_path = Path(db.DB_PATH)
    backup = db_path.with_name(f"{db_path.name}.sebelum-impor-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"Cadangan database: {backup}")

    supplier = find_or_create_supplier()
    brand = no_brand()
    ensure_unit()

    added = skipped = failed = 0
    stock_items = 0
    problems = []
    for i, row in enumerate(rows, 1):
        try:
            db.create_master_item(
                supplier["id"], brand["id"], row["name"], UNIT,
                sell_price=row["sell"], cost_price=row["cost"], stock_qty=row["stock"],
            )
            added += 1
            stock_items += row["stock"] > 0
        except ValueError as e:
            if str(e).startswith("Barang ganda"):
                skipped += 1  # already imported, or two names that only differ in punctuation
                problems.append(f"  dilewati (sudah ada): No. {row['no']} {row['name']}")
            else:
                failed += 1
                problems.append(f"  GAGAL: No. {row['no']} {row['name']} -- {e}")
        if i % 200 == 0:
            print(f"  ... {i}/{len(rows)}")

    print()
    print(f"Ditambahkan : {added} barang ({stock_items} dengan stok awal)")
    print(f"Dilewati    : {skipped} barang (sudah ada)")
    print(f"Gagal       : {failed} barang")
    if problems:
        print()
        print("\n".join(problems[:50]))
        if len(problems) > 50:
            print(f"  ... dan {len(problems) - 50} lainnya")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
