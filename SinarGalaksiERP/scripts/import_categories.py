"""Adds the Master Kategori tree (22 parents, 126 subcategories) from categories_seed.json.

Safe to run repeatedly: a category whose code already exists is left alone, so nothing is
duplicated or overwritten. Run it from the repo root on any server:

    python scripts/import_categories.py --dry-run     # show what would be added
    python scripts/import_categories.py               # add the missing ones
    python scripts/import_categories.py --db path/to/scraper.db
"""

import argparse
import json
import sqlite3
import sys
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
    db.init_db()

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    existing = {r["code"]: dict(r) for r in con.execute("SELECT id, code, name FROM categories")}
    con.close()

    added = skipped = 0
    for parent in seed["parents"]:
        if parent["code"] in existing:
            skipped += 1
            continue
        print(f"+ {parent['code']} {parent['name']}")
        if not args.dry_run:
            row = db.create_category(parent["name"], parent["code"], None, "aktif", None, None, "import")
            existing[row["code"]] = row
        added += 1

    for sub in seed["subs"]:
        if sub["code"] in existing:
            skipped += 1
            continue
        print(f"+ {sub['code']} {sub['name']}")
        if not args.dry_run:
            parent = existing.get(sub["parent"])
            if parent is None:
                print(f"  ! induk {sub['parent']} tidak ada, dilewati")
                continue
            row = db.create_category(sub["name"], sub["code"], parent["id"], "aktif", None, None, "import")
            existing[row["code"]] = row
        added += 1

    print(f"{'Akan ditambah' if args.dry_run else 'Ditambah'}: {added}, sudah ada: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
