"""Master Barang to and from Excel (19-11-2026).

Export and import share one column layout, so a file can be exported, edited in Excel and imported back.
Owner decisions, 19-11-2026:
- A row without SKU (or with an SKU not in the system) is a new item; a row whose SKU exists updates that item.
- Stok is only the opening stock of a NEW item (recorded in the stock ledger like the form's Stok Awal). Import
  never changes the stock of an existing item: that stays Stock Opname / Barang Masuk.
- A Supplier / Merek / Kategori / Satuan name that is not in its master rejects the row (no auto-create), so a typo
  never makes a second supplier or brand.
Import is admin only. Every row is checked first (preview); applying checks again and saves the rows without error.
On an existing item an empty cell means "leave as is", never "clear".
"""
import csv
import io
import re

from backend import database as db
from backend.xlsx_min import build_xlsx, read_xlsx

COLUMNS = ["SKU", "Nama Barang", "Supplier", "Merek", "Kategori", "Subkategori", "Satuan", "Harga Jual",
           "Harga Modal", "Stok", "Stok Minimum", "Deskripsi", "Status"]
WIDTHS = [24, 44, 24, 18, 30, 34, 10, 14, 14, 8, 13, 40, 10]
MAX_BYTES = 5 * 1024 * 1024


def _key(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def _int(raw: str, label: str, errors: list[str]) -> int | None:
    """'1.500.000', 'Rp 1,500,000', '1500000' and Excel's '1500000.0' all read as 1500000. Empty is None."""
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"(?i)^rp\.?\s*", "", s)
    if re.fullmatch(r"-?\d+(\.0+)?", s):
        return int(float(s))
    digits = re.sub(r"[.,\s]", "", s)
    if re.fullmatch(r"-?\d+", digits):
        return int(digits)
    errors.append(f"{label} '{raw}' bukan angka")
    return None


def export_xlsx(is_admin: bool) -> bytes:
    with db._conn() as con:
        rows = con.execute(
            """SELECT mi.sku, mi.name, s.name AS supplier, b.name AS brand, c.name AS cat, c.parent_id, p.name AS parent,
                      mi.unit, mi.sell_price, mi.avg_cost_price, mi.stock_qty, mi.min_stock, mi.description, mi.is_active
               FROM master_items mi JOIN suppliers s ON s.id=mi.supplier_id JOIN brands b ON b.id=mi.brand_id
               LEFT JOIN categories c ON c.id=mi.category_id LEFT JOIN categories p ON p.id=c.parent_id
               ORDER BY mi.sku""").fetchall()
    out = [COLUMNS]
    for r in rows:
        main, sub = (r["parent"], r["cat"]) if r["parent_id"] else (r["cat"], "")
        out.append([r["sku"], r["name"], r["supplier"], r["brand"], main or "", sub or "", r["unit"], r["sell_price"],
                    r["avg_cost_price"] if is_admin else "", r["stock_qty"], r["min_stock"], r["description"] or "",
                    "Aktif" if r["is_active"] else "Nonaktif"])
    return build_xlsx(out, "Master Barang", widths=WIDTHS)


def template_xlsx() -> bytes:
    return build_xlsx([COLUMNS], "Master Barang", widths=WIDTHS)


def _read_rows(data: bytes, filename: str) -> list[list[str]]:
    if not data:
        raise ValueError("File kosong.")
    if len(data) > MAX_BYTES:
        raise ValueError("File terlalu besar (maksimal 5 MB).")
    if (filename or "").lower().endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t") if text.strip() else csv.excel
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    if data[:2] != b"PK":
        raise ValueError("Format file tidak dikenali. Gunakan Excel (.xlsx) atau CSV. File .xls lama: buka di Excel lalu Simpan Sebagai .xlsx.")
    return read_xlsx(data)


def _lookups(con) -> dict:
    def by_name(sql):
        return {_key(r["name"]): dict(r) for r in con.execute(sql)}
    cats = [dict(r) for r in con.execute("SELECT id, name, parent_id, status FROM categories")]
    return {
        "supplier": by_name("SELECT id, name, is_active FROM suppliers"),
        "brand": by_name("SELECT id, name, is_active FROM brands"),
        "unit": by_name("SELECT name, is_active FROM units"),
        "main": {_key(c["name"]): c for c in cats if c["parent_id"] is None},
        "sub": {(c["parent_id"], _key(c["name"])): c for c in cats if c["parent_id"] is not None},
        "has_subs": {c["parent_id"] for c in cats if c["parent_id"] is not None},
        "items": {r["sku"].upper(): dict(r) for r in con.execute("SELECT * FROM master_items")},
        "names": {(r["supplier_id"], r["brand_id"], r["name_normalized"]): r["sku"]
                  for r in con.execute("SELECT supplier_id, brand_id, name_normalized, sku FROM master_items")},
    }


def _check(rows: list[list[str]]) -> tuple[list[dict], dict]:
    if not rows:
        raise ValueError("File tidak berisi data.")
    header = [_key(h) for h in rows[0]]
    col = {name: header.index(_key(name)) for name in COLUMNS if _key(name) in header}
    missing = [c for c in ("Nama Barang", "Supplier", "Merek", "Satuan", "Harga Jual") if c not in col]
    if missing:
        raise ValueError("Kolom wajib tidak ada di baris pertama: " + ", ".join(missing)
                         + ". Gunakan file dari tombol Unduh Template / Export Excel.")
    with db._conn() as con:
        lk = _lookups(con)
    out, seen_sku, seen_new = [], {}, {}
    for n, raw in enumerate(rows[1:], start=2):
        get = lambda c: (raw[col[c]].strip() if c in col and col[c] < len(raw) else "")  # noqa: E731
        if not any(x.strip() for x in raw):
            continue
        errors: list[str] = []
        notes: list[str] = []
        sku = get("SKU").upper()
        item = lk["items"].get(sku) if sku else None
        if sku and item is None:
            notes.append(f"SKU {sku} belum ada, dibuat sebagai barang baru (SKU baru dibuat otomatis)")
        if sku and sku in seen_sku:
            errors.append(f"SKU {sku} juga ada di baris {seen_sku[sku]}")
        if sku:
            seen_sku.setdefault(sku, n)
        name = re.sub(r"\s+", " ", get("Nama Barang")).upper()
        sell = _int(get("Harga Jual"), "Harga Jual", errors)
        cost = _int(get("Harga Modal"), "Harga Modal", errors)
        stock = _int(get("Stok"), "Stok", errors)
        min_stock = _int(get("Stok Minimum"), "Stok Minimum", errors)
        desc = get("Deskripsi")
        status = _key(get("Status"))
        if status and status not in ("AKTIF", "NONAKTIF", "TIDAK AKTIF", "YA", "TIDAK"):
            errors.append(f"Status '{get('Status')}' harus Aktif atau Nonaktif")
        active = status not in ("NONAKTIF", "TIDAK AKTIF", "TIDAK")

        def master(kind: str, label: str, required: bool):
            v = get(label)
            if not v:
                if required:
                    errors.append(f"{label} wajib diisi")
                return None
            hit = lk[kind].get(_key(v))
            if hit is None:
                errors.append(f"{label} '{v}' tidak ditemukan di Master {label}")
            elif not hit.get("is_active", 1):
                errors.append(f"{label} '{v}' tidak aktif")
            return hit

        new = item is None
        supplier = master("supplier", "Supplier", new)
        brand = master("brand", "Merek", new)
        unit = master("unit", "Satuan", new)
        # Kategori + Subkategori -> the category id stored on the item (the subcategory when the main has any).
        cat_id, cat_given = None, bool(get("Kategori") or get("Subkategori"))
        if cat_given:
            main = lk["main"].get(_key(get("Kategori"))) if get("Kategori") else None
            if get("Kategori") and main is None:
                errors.append(f"Kategori '{get('Kategori')}' tidak ditemukan di Master Kategori")
            elif not get("Kategori"):
                errors.append("Kategori wajib diisi bila Subkategori diisi")
            elif get("Subkategori"):
                sub = lk["sub"].get((main["id"], _key(get("Subkategori"))))
                if sub is None:
                    errors.append(f"Subkategori '{get('Subkategori')}' tidak ada di bawah Kategori {main['name']}")
                elif sub["status"] != "aktif" or main["status"] != "aktif":
                    errors.append(f"Kategori {main['name']} / {sub['name']} tidak aktif")
                else:
                    cat_id = sub["id"]
            elif main["id"] in lk["has_subs"]:
                errors.append(f"Subkategori wajib diisi untuk Kategori {main['name']}")
            elif main["status"] != "aktif":
                errors.append(f"Kategori {main['name']} tidak aktif")
            else:
                cat_id = main["id"]
        elif new:
            errors.append("Kategori dan Subkategori wajib diisi")

        for label, v in (("Harga Modal", cost), ("Stok", stock), ("Stok Minimum", min_stock)):
            if v is not None and v < 0:
                errors.append(f"{label} tidak boleh negatif")
        changes: list[str] = []
        if new:
            if not name:
                errors.append("Nama Barang wajib diisi")
            if sell is None or sell <= 0:
                errors.append("Harga Jual wajib diisi (lebih dari 0)")
            if (stock or 0) > 0 and not cost:
                errors.append("Harga Modal wajib diisi bila ada Stok awal")
            if supplier and brand and name:
                k = (supplier["id"], brand["id"], db._normalize_name(name))
                if k in lk["names"]:
                    errors.append(f"Barang ganda: sudah ada SKU {lk['names'][k]} dengan supplier, merek dan nama ini")
                elif k in seen_new:
                    errors.append(f"Barang yang sama juga ada di baris {seen_new[k]}")
                else:
                    seen_new[k] = n
            action = "baru"
        else:
            if sell is not None and sell <= 0:
                errors.append("Harga Jual harus lebih dari 0")
            upd = {}
            if name and name != item["name"]:
                upd["name"] = name; changes.append(f"Nama: {item['name']} → {name}")
            if sell is not None and sell != item["sell_price"]:
                upd["sell_price"] = sell; changes.append(f"Harga Jual: {item['sell_price']:,} → {sell:,}".replace(",", "."))
            if unit and unit["name"] != item["unit"]:
                upd["unit"] = unit["name"]; changes.append(f"Satuan: {item['unit']} → {unit['name']}")
            if min_stock is not None and min_stock != item["min_stock"]:
                upd["min_stock"] = min_stock; changes.append(f"Stok Minimum: {item['min_stock']} → {min_stock}")
            if desc and desc != (item["description"] or ""):
                upd["description"] = desc; changes.append("Deskripsi diubah")
            if cat_id is not None and cat_id != item["category_id"]:
                upd["category_id"] = cat_id; changes.append("Kategori diubah")
            if supplier and supplier["id"] != item["supplier_id"]:
                upd["supplier_id"] = supplier["id"]; changes.append(f"Supplier → {supplier['name']}")
            if brand and brand["id"] != item["brand_id"]:
                notes.append("Merek tidak bisa diubah lewat import (bagian dari SKU), diabaikan")
            if stock is not None and stock != item["stock_qty"]:
                notes.append(f"Stok di file ({stock}) diabaikan; stok tetap {item['stock_qty']} (ubah lewat Stock Opname)")
            if cost is not None and cost != item["avg_cost_price"]:
                if (item["stock_qty"] or 0) == 0 and (item["quarantine_qty"] or 0) == 0:
                    upd["cost_price"] = cost; changes.append(f"Harga Modal: {item['avg_cost_price']:,} → {cost:,}".replace(",", "."))
                else:
                    notes.append("Harga Modal diabaikan: barang masih punya stok, modal dihitung dari Barang Masuk")
            if status and active != bool(item["is_active"]):
                notes.append("Status tidak diubah lewat import (pakai Nonaktifkan/Aktifkan di Master Barang)")
            action = "ubah" if upd else "sama"
        row = {"row": n, "sku": item["sku"] if item else "", "name": name or (item["name"] if item else ""),
               "action": "error" if errors else action, "errors": errors, "notes": notes, "changes": changes}
        if not errors:
            if new:
                row["_new"] = {"supplier_id": supplier["id"], "brand_id": brand["id"], "name": name, "unit": unit["name"],
                               "sell_price": sell, "cost_price": cost or 0, "stock_qty": stock or 0,
                               "min_stock": min_stock if min_stock is not None else db.LOW_STOCK_THRESHOLD,
                               "description": desc or None, "category_id": cat_id, "is_active": active}
            elif upd:
                row["_upd"] = {"id": item["id"], **upd}
        out.append(row)
    summary = {k: sum(1 for r in out if r["action"] == k) for k in ("baru", "ubah", "sama", "error")}
    summary["total"] = len(out)
    return out, summary


def _public(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def preview(data: bytes, filename: str) -> dict:
    rows, summary = _check(_read_rows(data, filename))
    return {"summary": summary, "rows": _public(rows)}


def apply(data: bytes, filename: str, by: str) -> dict:
    """Saves the rows without error, one by one (each its own transaction). A row that fails now (e.g. changed by
    someone since the preview) is reported and the rest still go in."""
    rows, _ = _check(_read_rows(data, filename))
    created = updated = 0
    failed = []
    for r in rows:
        try:
            if "_new" in r:
                d = r["_new"]
                made = db.create_master_item(d["supplier_id"], d["brand_id"], d["name"], d["unit"], d["sell_price"],
                                             d["cost_price"], d["stock_qty"], d["description"], d["min_stock"], None,
                                             by, d["category_id"], d["is_active"])
                r["sku"] = made["sku"]
                created += 1
            elif "_upd" in r:
                u = dict(r["_upd"])
                item_id = u.pop("id")
                if "cost_price" in u:
                    db.set_master_item_cost(item_id, u.pop("cost_price"), by)
                if u:
                    db.update_master_item(item_id, u.get("name"), u.get("sell_price"), u.get("unit"), u.get("description"),
                                          u.get("min_stock"), None, by, "Import Excel",
                                          set_category="category_id" in u, category_id=u.get("category_id"),
                                          supplier_id=u.get("supplier_id"))
                updated += 1
        except (ValueError, LookupError) as e:
            failed.append({"row": r["row"], "name": r["name"], "error": str(e)})
    return {"created": created, "updated": updated, "unchanged": sum(1 for r in rows if r["action"] == "sama"),
            "skipped": sum(1 for r in rows if r["action"] == "error"), "failed": failed,
            "new_skus": [{"row": r["row"], "sku": r["sku"], "name": r["name"]} for r in rows if "_new" in r and r["sku"]]}
