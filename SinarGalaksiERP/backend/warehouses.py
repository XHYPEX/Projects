"""Master Gudang: the places goods are kept, as a list to pick from (Gudang → Rak).

Owner decisions 31-10-2026:
  * a list of places only: stock stays ONE number per item; there is no stock per gudang and no transfer;
  * two levels, Gudang → Rak, both with Kode + Nama; only an Admin edits them;
  * used on Master Barang (the item's home, optional), Stock Opname (the counted place) and Barang Masuk (per GR line,
    prefilled from the item's home, recorded on the GR only; it never changes the item's home);
  * a gudang/rak that anything points to can only be set Nonaktif, never deleted; an unused one may be deleted;
  * text typed earlier in 'Lokasi Rak / Gudang' is kept as "Lokasi lama" until a gudang/rak is chosen; nothing guessed.

Owner 04-11-2026: racks are typed inside the Gudang form and saved with it, and codes are generated (GD-01; R-01 per
gudang), never typed. Codes that already exist stay as they are.

The readable place ("GUDANG UTAMA › RAK A1") is also written to the old `location` text columns, so every screen and
printout that already shows a location keeps working; renaming a gudang/rak rewrites those labels.
"""
from __future__ import annotations

import re
import sqlite3

from backend import database as db

WAREHOUSE_PREFIX = "GD-"
RACK_PREFIX = "R-"


def migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, created_by TEXT,
            updated_at TEXT, updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS warehouse_racks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, created_by TEXT,
            updated_at TEXT, updated_by TEXT,
            UNIQUE (warehouse_id, code)
        );
        CREATE INDEX IF NOT EXISTS idx_racks_warehouse ON warehouse_racks(warehouse_id);
    """)
    for table, cols in (
        ("master_items", (("warehouse_id", "INTEGER"), ("rack_id", "INTEGER"), ("location_legacy", "TEXT"))),
        ("stock_opnames", (("warehouse_id", "INTEGER"), ("rack_id", "INTEGER"))),
        ("inbound_items", (("warehouse_id", "INTEGER"), ("rack_id", "INTEGER"))),
    ):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Keep what was typed before Master Gudang existed; it is shown as "Lokasi lama" until a place is chosen.
    if not con.execute("SELECT 1 FROM app_migrations WHERE name='location_legacy_copy'").fetchone():
        con.execute("UPDATE master_items SET location_legacy=location WHERE warehouse_id IS NULL AND TRIM(COALESCE(location,''))<>''")
        con.execute("INSERT INTO app_migrations (name, applied_at) VALUES ('location_legacy_copy', ?)", (db._now(),))
    con.execute("CREATE INDEX IF NOT EXISTS idx_master_items_warehouse ON master_items(warehouse_id, rack_id)")


# ── helpers ───────────────────────────────────────────────────────────────────

def _next_code(existing: list[str], prefix: str) -> str:
    numbers = [int(m.group(1)) for c in existing if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", c))]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


def _name(v) -> str:
    n = re.sub(r"\s+", " ", (v or "").strip().upper())
    if len(n) < 2 or len(n) > 60:
        raise ValueError("Nama 2-60 karakter.")
    return n


def _label(wname: str | None, rname: str | None) -> str | None:
    if not wname:
        return None
    return f"{wname} › {rname}" if rname else wname


def resolve(con: sqlite3.Connection, warehouse_id, rack_id, keep: tuple = (None, None)) -> tuple[int | None, int | None, str | None]:
    """Check a gudang/rak pair and return (warehouse_id, rack_id, label). `keep` is the pair already stored on the
    record: an inactive gudang/rak may stay where it is, but cannot be newly chosen."""
    if warehouse_id in (None, "", 0):
        if rack_id not in (None, "", 0):
            raise ValueError("Pilih gudang dulu sebelum rak.")
        return None, None, None
    w = con.execute("SELECT * FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()
    if w is None:
        raise ValueError("Gudang tidak ditemukan.")
    if not w["is_active"] and warehouse_id != keep[0]:
        raise ValueError(f"Gudang {w['name']} nonaktif; pilih gudang lain.")
    r = None
    if rack_id not in (None, "", 0):
        r = con.execute("SELECT * FROM warehouse_racks WHERE id=?", (rack_id,)).fetchone()
        if r is None or r["warehouse_id"] != w["id"]:
            raise ValueError(f"Rak tidak ada di gudang {w['name']}.")
        if not r["is_active"] and rack_id != keep[1]:
            raise ValueError(f"Rak {r['name']} nonaktif; pilih rak lain.")
    return w["id"], (r["id"] if r else None), _label(w["name"], r["name"] if r else None)


def _usage(con: sqlite3.Connection, where_items: str, where_docs: str, args: tuple) -> dict:
    items = con.execute(f"SELECT COUNT(*) AS n FROM master_items WHERE {where_items}", args).fetchone()["n"]
    opn = con.execute(f"SELECT COUNT(*) AS n FROM stock_opnames WHERE {where_docs}", args).fetchone()["n"]
    gr = con.execute(f"SELECT COUNT(*) AS n FROM inbound_items WHERE {where_docs}", args).fetchone()["n"]
    return {"items": items, "opnames": opn, "gr_lines": gr, "used": bool(items or opn or gr)}


def _refresh_labels(con: sqlite3.Connection, warehouse_id: int) -> None:
    """After a rename, rewrite the readable place on everything that points at this gudang (or one of its racks)."""
    w = con.execute("SELECT name FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()
    racks = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM warehouse_racks WHERE warehouse_id=?", (warehouse_id,))}
    for table in ("master_items", "stock_opnames", "inbound_items"):
        con.execute(f"UPDATE {table} SET location=? WHERE warehouse_id=? AND rack_id IS NULL", (w["name"], warehouse_id))
        for rid, rname in racks.items():
            con.execute(f"UPDATE {table} SET location=? WHERE warehouse_id=? AND rack_id=?", (_label(w["name"], rname), warehouse_id, rid))


# ── list ──────────────────────────────────────────────────────────────────────

def list_warehouses(include_inactive: bool = True) -> list[dict]:
    with db._conn() as con:
        ws = [dict(r) for r in con.execute(
            "SELECT * FROM warehouses" + ("" if include_inactive else " WHERE is_active=1") + " ORDER BY is_active DESC, code")]
        item_w = {r["warehouse_id"]: r["n"] for r in con.execute(
            "SELECT warehouse_id, COUNT(*) AS n FROM master_items WHERE warehouse_id IS NOT NULL GROUP BY warehouse_id")}
        item_r = {r["rack_id"]: r["n"] for r in con.execute(
            "SELECT rack_id, COUNT(*) AS n FROM master_items WHERE rack_id IS NOT NULL GROUP BY rack_id")}
        for w in ws:
            w["is_active"] = bool(w["is_active"])
            w["item_count"] = item_w.get(w["id"], 0)
            w["racks"] = []
            for r in con.execute("SELECT * FROM warehouse_racks WHERE warehouse_id=?" + ("" if include_inactive else " AND is_active=1")
                                 + " ORDER BY is_active DESC, code", (w["id"],)):
                r = dict(r)
                r["is_active"] = bool(r["is_active"])
                r["item_count"] = item_r.get(r["id"], 0)
                w["racks"].append(r)
        return ws


def summary() -> dict:
    with db._conn() as con:
        return {
            "warehouses": con.execute("SELECT COUNT(*) AS n FROM warehouses WHERE is_active=1").fetchone()["n"],
            "racks": con.execute("""SELECT COUNT(*) AS n FROM warehouse_racks r JOIN warehouses w ON w.id=r.warehouse_id
                                    WHERE r.is_active=1 AND w.is_active=1""").fetchone()["n"],
            "items_placed": con.execute("SELECT COUNT(*) AS n FROM master_items WHERE warehouse_id IS NOT NULL").fetchone()["n"],
            "items_unplaced": con.execute("SELECT COUNT(*) AS n FROM master_items WHERE warehouse_id IS NULL AND is_active=1").fetchone()["n"],
        }


# ── Gudang ────────────────────────────────────────────────────────────────────

def _warehouse_out(con, wid: int) -> dict:
    w = dict(con.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone())
    w["is_active"] = bool(w["is_active"])
    return w


def _add_rack(con, wid: int, name: str, by: str) -> None:
    codes = [r["code"] for r in con.execute("SELECT code FROM warehouse_racks WHERE warehouse_id=?", (wid,))]
    now = db._now()
    con.execute("""INSERT INTO warehouse_racks (warehouse_id, code, name, is_active, created_at, created_by, updated_at, updated_by)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?)""", (wid, _next_code(codes, RACK_PREFIX), name, now, by, now, by))


def _save_racks(con, wid: int, rows: list[dict], by: str) -> None:
    """Rename the racks sent with an id and add those without one. A rack left out is untouched: nothing is removed
    here. Names are compared as they will end up, so two racks can swap names in one save."""
    current = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM warehouse_racks WHERE warehouse_id=?", (wid,))}
    renames, new_names = {}, []
    for row in rows:
        rid, name = row.get("id"), _name(row.get("name"))
        if rid is None:
            new_names.append(name)
        elif rid not in current:
            raise ValueError("Rak tidak ada di gudang ini.")
        else:
            renames[rid] = name
    final = [renames.get(rid, name) for rid, name in current.items()] + new_names
    duplicate = next((n for n in final if final.count(n) > 1), None)
    if duplicate:
        raise ValueError(f"Rak bernama {duplicate} lebih dari satu di gudang ini.")
    for rid, name in renames.items():
        if name != current[rid]:
            con.execute("UPDATE warehouse_racks SET name=?, updated_at=?, updated_by=? WHERE id=?", (name, db._now(), by, rid))
    for name in new_names:
        _add_rack(con, wid, name, by)


def create_warehouse(data: dict, by: str) -> dict:
    name = _name(data.get("name"))
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM warehouses WHERE name=?", (name,)).fetchone():
            raise ValueError(f"Gudang bernama {name} sudah ada.")
        code = _next_code([r["code"] for r in con.execute("SELECT code FROM warehouses")], WAREHOUSE_PREFIX)
        now = db._now()
        cur = con.execute("INSERT INTO warehouses (code, name, is_active, created_at, created_by, updated_at, updated_by) VALUES (?, ?, 1, ?, ?, ?, ?)",
                          (code, name, now, by, now, by))
        _save_racks(con, cur.lastrowid, data.get("racks") or [], by)
        return _warehouse_out(con, cur.lastrowid)


def update_warehouse(wid: int, data: dict, by: str) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        w = con.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        if w is None:
            raise LookupError("Gudang tidak ditemukan.")
        name = _name(data["name"]) if data.get("name") is not None else w["name"]
        active = bool(data["is_active"]) if data.get("is_active") is not None else bool(w["is_active"])
        if con.execute("SELECT 1 FROM warehouses WHERE name=? AND id<>?", (name, wid)).fetchone():
            raise ValueError(f"Gudang bernama {name} sudah ada.")
        con.execute("UPDATE warehouses SET name=?, is_active=?, updated_at=?, updated_by=? WHERE id=?",
                    (name, int(active), db._now(), by, wid))
        _save_racks(con, wid, data.get("racks") or [], by)
        _refresh_labels(con, wid)
        return _warehouse_out(con, wid)


def delete_warehouse(wid: int) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        w = con.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        if w is None:
            raise LookupError("Gudang tidak ditemukan.")
        u = _usage(con, "warehouse_id=?", "warehouse_id=?", (wid,))
        if u["used"]:
            raise ValueError(f"Gudang {w['name']} masih dipakai ({_usage_text(u)}). Nonaktifkan saja; data lama tetap menyebut gudang ini.")
        con.execute("DELETE FROM warehouse_racks WHERE warehouse_id=?", (wid,))
        con.execute("DELETE FROM warehouses WHERE id=?", (wid,))
        return {"deleted": True}


def _usage_text(u: dict) -> str:
    parts = [f"{u['items']} barang" if u["items"] else "", f"{u['opnames']} stock opname" if u["opnames"] else "",
             f"{u['gr_lines']} baris Barang Masuk" if u["gr_lines"] else ""]
    return ", ".join(p for p in parts if p)


# ── Rak ───────────────────────────────────────────────────────────────────────

def _rack_out(con, rid: int) -> dict:
    r = dict(con.execute("SELECT * FROM warehouse_racks WHERE id=?", (rid,)).fetchone())
    r["is_active"] = bool(r["is_active"])
    return r


def set_rack_active(rid: int, active: bool, by: str) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM warehouse_racks WHERE id=?", (rid,)).fetchone() is None:
            raise LookupError("Rak tidak ditemukan.")
        con.execute("UPDATE warehouse_racks SET is_active=?, updated_at=?, updated_by=? WHERE id=?", (int(active), db._now(), by, rid))
        return _rack_out(con, rid)


def delete_rack(rid: int) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM warehouse_racks WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise LookupError("Rak tidak ditemukan.")
        u = _usage(con, "rack_id=?", "rack_id=?", (rid,))
        if u["used"]:
            raise ValueError(f"Rak {r['name']} masih dipakai ({_usage_text(u)}). Nonaktifkan saja; data lama tetap menyebut rak ini.")
        con.execute("DELETE FROM warehouse_racks WHERE id=?", (rid,))
        return {"deleted": True}


# ── used by Master Barang, Stock Opname and Barang Masuk ─────────────────────

def set_item_location(master_item_id: int, warehouse_id, rack_id, by: str | None) -> None:
    """The item's home. With a place chosen, `location` reads that place; cleared, it falls back to the old text."""
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT warehouse_id, rack_id, location_legacy FROM master_items WHERE id=?", (master_item_id,)).fetchone()
        if row is None:
            raise LookupError("Barang tidak ditemukan.")
        wid, rid, label = resolve(con, warehouse_id, rack_id, keep=(row["warehouse_id"], row["rack_id"]))
        if (wid, rid) == (row["warehouse_id"], row["rack_id"]):
            return
        con.execute("UPDATE master_items SET warehouse_id=?, rack_id=?, location=?, updated_at=?, updated_by=? WHERE id=?",
                    (wid, rid, label if wid else row["location_legacy"], db._now(), by, master_item_id))


def stamp_opname(opname_id: int, warehouse_id, rack_id) -> None:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT warehouse_id, rack_id FROM stock_opnames WHERE id=?", (opname_id,)).fetchone()
        if row is None:
            return
        wid, rid, label = resolve(con, warehouse_id, rack_id, keep=(row["warehouse_id"], row["rack_id"]))
        con.execute("UPDATE stock_opnames SET warehouse_id=?, rack_id=?, location=? WHERE id=?", (wid, rid, label, opname_id))


def check_pair(warehouse_id, rack_id, keep: tuple = (None, None)) -> tuple[int | None, int | None, str | None]:
    with db._conn() as con:
        return resolve(con, warehouse_id, rack_id, keep)
