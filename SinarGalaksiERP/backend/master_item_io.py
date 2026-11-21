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
from backend.xlsx_min import build_xlsx, build_xlsx_sheets, read_xlsx

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


def export_xlsx() -> bytes:
    """Owner 21-11-2026: the export never shows Harga Modal, not even for admin (the file gets passed around).
    The import still reads a Harga Modal column when someone adds it (opening stock of a new item needs it)."""
    with db._conn() as con:
        rows = con.execute(
            """SELECT mi.sku, mi.name, s.name AS supplier, b.name AS brand, c.name AS cat, c.parent_id, p.name AS parent,
                      mi.unit, mi.sell_price, mi.stock_qty, mi.min_stock, mi.description, mi.is_active
               FROM master_items mi JOIN suppliers s ON s.id=mi.supplier_id JOIN brands b ON b.id=mi.brand_id
               LEFT JOIN categories c ON c.id=mi.category_id LEFT JOIN categories p ON p.id=c.parent_id
               ORDER BY mi.sku""").fetchall()
    cols = [c for c in COLUMNS if c != "Harga Modal"]
    out = [cols]
    for r in rows:
        main, sub = (r["parent"], r["cat"]) if r["parent_id"] else (r["cat"], "")
        out.append([r["sku"], r["name"], r["supplier"], r["brand"], main or "", sub or "", r["unit"], r["sell_price"],
                    r["stock_qty"], r["min_stock"], r["description"] or "",
                    "Aktif" if r["is_active"] else "Nonaktif"])
    return build_xlsx(out, "Master Barang", widths=[w for c, w in zip(COLUMNS, WIDTHS) if c != "Harga Modal"])


def template_xlsx() -> bytes:
    """Sheet 1 is the one that is imported (only the header row). The other sheets are help and are never read:
    Contoh (example rows built from this shop's own master names, so they would pass), Petunjuk (what goes in each
    column) and Daftar Master (the names a Supplier/Merek/Kategori/Satuan cell must match)."""
    with db._conn() as con:
        sups = [r["name"] for r in con.execute("SELECT name FROM suppliers WHERE is_active=1 ORDER BY name")]
        brands = [r["name"] for r in con.execute("SELECT name FROM brands WHERE is_active=1 ORDER BY name")]
        units = [r["name"] for r in con.execute("SELECT name FROM units WHERE is_active=1 ORDER BY name")]
        cats = con.execute(
            """SELECT p.name AS main, c.name AS sub FROM categories c LEFT JOIN categories p ON p.id=c.parent_id
               WHERE c.status='aktif' AND (c.parent_id IS NOT NULL
                     OR NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id=c.id))
               ORDER BY COALESCE(p.name, c.name), c.name""").fetchall()
        old = con.execute(
            """SELECT mi.sku, mi.name, mi.sell_price FROM master_items mi WHERE mi.is_active=1 ORDER BY mi.id LIMIT 1""").fetchone()
    cat_pairs = [(r["main"], r["sub"]) if r["main"] else (r["sub"], "") for r in cats]
    sup = sups[0] if sups else "NAMA SUPPLIER"
    brand = brands[0] if brands else "NAMA MEREK"
    unit = "PCS" if "PCS" in units else (units[0] if units else "PCS")
    default_cat = cat_pairs[0] if cat_pairs else ("NAMA KATEGORI", "NAMA SUBKATEGORI")

    def cat_for(words: str) -> tuple[str, str]:
        try:
            got = db.suggest_categories(words)
            m = (got.get("matches") if isinstance(got, dict) else got) or []
        except Exception:  # an example only; never fail the download over it
            m = []
        if not m:
            return default_cat
        return (m[0]["parent_name"], m[0]["name"]) if m[0].get("parent_id") else (m[0]["name"], "")
    examples = [COLUMNS,
                ["", "BUMPER DEPAN AVANZA 2019", sup, brand, *cat_for("BUMPER DEPAN"), unit, 1500000, 1100000, 3, 1,
                 "WARNA HITAM DOFF", "Aktif"],
                ["", "LAMPU LED H4 6000K", sup, brand, *cat_for("LAMPU LED"), unit, 250000, "", "", 2, "", ""]]
    if old:
        examples.append([old["sku"], "", "", "", "", "", "", old["sell_price"] + 10000, "", "", "", "", ""])
    else:
        examples.append(["SGV-XXX-CONTOH-001", "", "", "", "", "", "", 260000, "", "", "", "", ""])
    examples += [[], ["Keterangan contoh:"],
                 ["Baris 2", "Barang baru dengan stok awal 3. SKU kosong: dibuat otomatis. Harga Modal wajib karena ada Stok."],
                 ["Baris 3", "Barang baru tanpa stok (stok 0). Harga Modal, Stok, Deskripsi dan Status boleh kosong."],
                 ["Baris 4", "Mengubah barang yang sudah ada: isi SKU-nya, isi hanya kolom yang mau diubah (di sini Harga Jual)."],
                 [], ["Isi data di sheet pertama (Master Barang). Hanya sheet pertama yang diimport; sheet ini tidak."]]
    guide = [["Kolom", "Wajib untuk barang baru?", "Keterangan"],
             ["SKU", "Tidak", "Kosongkan untuk barang baru (dibuat otomatis). Isi SKU yang sudah ada untuk mengubah barang itu."],
             ["Nama Barang", "Ya", "Nama barang. Huruf otomatis jadi huruf besar."],
             ["Supplier", "Ya", "Harus sama dengan nama di Master Supplier (lihat sheet Daftar Master)."],
             ["Merek", "Ya", "Harus sama dengan nama di Master Brand. Merek barang lama tidak bisa diubah lewat import."],
             ["Kategori", "Ya", "Kategori utama, sama dengan Master Kategori."],
             ["Subkategori", "Ya, bila kategorinya punya subkategori", "Lihat pasangan Kategori - Subkategori di sheet Daftar Master."],
             ["Satuan", "Ya", "Sama dengan Master Satuan, misalnya PCS, SET."],
             ["Harga Jual", "Ya", "Angka saja, boleh 1500000 atau 1.500.000 atau Rp 1.500.000."],
             ["Harga Modal", "Ya, bila Stok diisi", "Untuk barang lama hanya bisa diubah bila stoknya 0."],
             ["Stok", "Tidak", "Stok awal barang baru. Untuk barang lama diabaikan (pakai Stock Opname)."],
             ["Stok Minimum", "Tidak", "Batas peringatan stok menipis."],
             ["Deskripsi", "Tidak", "Keterangan bebas."],
             ["Status", "Tidak", "Aktif atau Nonaktif (hanya untuk barang baru). Kosong = Aktif."],
             [],
             ["Catatan", "", "Sel kosong pada barang lama berarti tidak diubah. Baris dengan nama master yang tidak ditemukan ditolak."]]
    n = max(len(sups), len(brands), len(units), len(cat_pairs), 1)
    pad = lambda xs, i: xs[i] if i < len(xs) else ""  # noqa: E731
    lists = [["Supplier", "Merek", "Satuan", "", "Kategori", "Subkategori"]]
    lists += [[pad(sups, i), pad(brands, i), pad(units, i), "",
               cat_pairs[i][0] if i < len(cat_pairs) else "", cat_pairs[i][1] if i < len(cat_pairs) else ""] for i in range(n)]
    return build_xlsx_sheets([("Master Barang", [COLUMNS], WIDTHS, (0,)),
                              ("Contoh", examples, WIDTHS, (0, 6)),
                              ("Petunjuk", guide, [16, 34, 100], (0,)),
                              ("Daftar Master", lists, [28, 22, 12, 3, 30, 34], (0,))])


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
        "supplier": by_name("SELECT id, name, code, is_active FROM suppliers"),
        "brand": by_name("SELECT id, name, code, is_active FROM brands"),
        "unit": by_name("SELECT name, is_active FROM units"),
        "main": {_key(c["name"]): c for c in cats if c["parent_id"] is None},
        "sub": {(c["parent_id"], _key(c["name"])): c for c in cats if c["parent_id"] is not None},
        "has_subs": {c["parent_id"] for c in cats if c["parent_id"] is not None},
        "items": {r["sku"].upper(): dict(r) for r in con.execute("SELECT * FROM master_items")},
        "names": {(r["supplier_id"], r["brand_id"], r["name_normalized"]): r["sku"]
                  for r in con.execute("SELECT supplier_id, brand_id, name_normalized, sku FROM master_items")},
        "seq": {r["sku_prefix"]: r["m"] for r in con.execute(
            "SELECT sku_prefix, MAX(sku_seq) AS m FROM master_items WHERE sku_prefix IS NOT NULL GROUP BY sku_prefix")},
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
        sku_auto = ""
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
                    # SKU left empty (or unknown): show the SKU it will get, same rule as preview_sku.
                    prefix = f"{supplier['code']}-{brand['code']}-{db._compute_product_segment(k[2])}"
                    lk["seq"][prefix] = (lk["seq"].get(prefix) or 0) + 1
                    sku_auto = f"{prefix}-{lk['seq'][prefix]:03d}"
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
                    upd["cost_price"] = cost; changes.append("Harga Modal diubah")  # no amounts shown (Owner 21-11-2026)
                else:
                    notes.append("Harga Modal diabaikan: barang masih punya stok, modal dihitung dari Barang Masuk")
            if status and active != bool(item["is_active"]):
                notes.append("Status tidak diubah lewat import (pakai Nonaktifkan/Aktifkan di Master Barang)")
            action = "ubah" if upd else "sama"
        row = {"row": n, "sku": item["sku"] if item else (sku_auto if not errors else ""), "sku_auto": item is None, "name": name or (item["name"] if item else ""),
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
