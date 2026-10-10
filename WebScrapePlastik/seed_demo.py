"""Builds a self-contained demo database with dummy data.

Never touches data/scraper.db: it writes data/demo.db and data/demo_product_images/.
Run the app against it with:

    DB_PATH=data/demo.db PRODUCT_IMAGE_DIR=data/demo_product_images python run.py

Login: admin / demo1234 (admin) and kasir / demo1234 (staff).
Re-running deletes and rebuilds the demo files.
"""

import os
import random
import shutil
import sqlite3
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DEMO_DB = ROOT / "data" / "demo.db"
DEMO_IMAGES = ROOT / "data" / "demo_product_images"

# Must be set before backend modules read their config.
os.environ["DB_PATH"] = str(DEMO_DB)
os.environ["PRODUCT_IMAGE_DIR"] = str(DEMO_IMAGES)
sys.path.insert(0, str(ROOT))

for path in (DEMO_DB, Path(str(DEMO_DB) + "-wal"), Path(str(DEMO_DB) + "-shm")):
    path.unlink(missing_ok=True)
shutil.rmtree(DEMO_IMAGES, ignore_errors=True)

from backend import database as db  # noqa: E402
from backend import product_images  # noqa: E402
from backend.auth import hash_password  # noqa: E402

random.seed(7)
db.init_db()

# --- users ---------------------------------------------------------------
db.create_user("admin", hash_password("demo1234"), "admin")
db.create_user("kasir", hash_password("demo1234"), "staff")

# --- suppliers, brands, units ---------------------------------------------
SUPPLIERS = ["CV Sumber Plastik", "PT Mitra Kemasan", "UD Asta Jaya", "Sinar Plastik"]
BRANDS = ["Lion Star", "Lock&Lock", "Nambarte", "Tanpa Merek", "Dny"]
existing_units = {u["name"].lower() for u in db.get_all_units()}
for unit in ("pcs", "lusin", "pak", "roll", "kg"):
    if unit not in existing_units:
        db.create_unit(unit)

sup = {n: db.create_supplier(n) for n in SUPPLIERS}
brand = {}
for n in BRANDS:
    try:
        brand[n] = db.create_brand(n)
    except ValueError:  # seeded brand list already has it
        brand[n] = next(b for b in db.get_all_brands() if b["name"].lower() == n.lower())

# --- products (name, supplier, brand, unit, cost, sell, stock) -------------
PRODUCTS = [
    ("Kantong Plastik Hitam 30x50", "CV Sumber Plastik", "Tanpa Merek", "pak", 9000, 14000, 60),
    ("Kantong Plastik Bening 20x30", "CV Sumber Plastik", "Tanpa Merek", "pak", 7500, 12000, 80),
    ("Kantong Zip Lock 10x15", "PT Mitra Kemasan", "Tanpa Merek", "pak", 11000, 18000, 45),
    ("Kantong Zip Lock 15x20", "PT Mitra Kemasan", "Tanpa Merek", "pak", 15000, 24000, 30),
    ("Gelas Plastik 16oz", "CV Sumber Plastik", "Lion Star", "pak", 18000, 26000, 50),
    ("Gelas Plastik 22oz", "CV Sumber Plastik", "Lion Star", "pak", 22000, 31000, 40),
    ("Tutup Gelas Cembung 16oz", "CV Sumber Plastik", "Lion Star", "pak", 9000, 15000, 55),
    ("Sedotan Bening 6mm", "CV Sumber Plastik", "Lion Star", "pak", 4500, 8000, 120),
    ("Botol Plastik 500ml Bening", "Sinar Plastik", "Nambarte", "lusin", 36000, 48000, 24),
    ("Botol Plastik 250ml Spray", "Sinar Plastik", "Nambarte", "lusin", 42000, 56000, 18),
    ("Kotak Makan 500ml Persegi", "Sinar Plastik", "Lock&Lock", "pcs", 9000, 12500, 40),
    ("Kotak Makan 750ml Persegi", "Sinar Plastik", "Lock&Lock", "pcs", 13000, 18500, 35),
    ("Kotak Makan 1000ml Bulat", "Sinar Plastik", "Lock&Lock", "pcs", 16000, 22000, 28),
    ("Toples Plastik 1L Bening", "Sinar Plastik", "Lion Star", "pcs", 5000, 8000, 70),
    ("Toples Plastik 2L Bening", "Sinar Plastik", "Lion Star", "pcs", 8500, 13000, 50),
    ("Ember Plastik 10L", "UD Asta Jaya", "Dny", "pcs", 14000, 21000, 25),
    ("Ember Plastik 15L", "UD Asta Jaya", "Dny", "pcs", 19000, 28000, 20),
    ("Gayung Plastik Besar", "UD Asta Jaya", "Dny", "pcs", 4500, 8000, 60),
    ("Baskom Plastik 30cm", "UD Asta Jaya", "Dny", "pcs", 9000, 14500, 33),
    ("Keranjang Belanja Plastik", "UD Asta Jaya", "Dny", "pcs", 17000, 25000, 15),
    ("Lampu Kombinasi", "UD Asta Jaya", "Dny", "pcs", 100, 10000, 5),
    ("Rak Piring Plastik 2 Susun", "PT Mitra Kemasan", "Tanpa Merek", "pcs", 24000, 36000, 12),
    ("Plastik Wrap 30cm x 100m", "PT Mitra Kemasan", "Tanpa Merek", "roll", 17000, 25000, 4),
    ("Tali Rafia Merah", "PT Mitra Kemasan", "Tanpa Merek", "roll", 5500, 9000, 3),
    ("Karung Plastik 50kg", "CV Sumber Plastik", "Tanpa Merek", "lusin", 48000, 66000, 0),
    ("Mika Bening Tebal", "PT Mitra Kemasan", "Tanpa Merek", "kg", 32000, 45000, 9),
]

# Placeholder photo for some products: a solid-colour PNG built by hand (no Pillow dependency).
PALETTE = [(217, 119, 87), (106, 155, 204), (120, 140, 93), (201, 164, 92), (155, 126, 189), (88, 160, 160)]


def make_png(rgb: tuple[int, int, int], size: int = 160) -> bytes:
    light = tuple(min(255, c + 45) for c in rgb)
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type: none
        for x in range(size):
            inside = size // 4 <= x < size * 3 // 4 and size // 4 <= y < size * 3 // 4
            rows.extend(light if inside else rgb)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


items = []
for i, (name, s, b, unit, cost, sell, stock) in enumerate(PRODUCTS):
    item = db.create_master_item(sup[s]["id"], brand[b]["id"], name, unit, sell, cost, stock)
    if i % 3 != 2:  # two of every three products get a photo
        filename = product_images.save_image(make_png(PALETTE[i % len(PALETTE)]), "image/png")
        db.set_master_item_image_path(item["id"], filename)
    items.append(item)

# --- customers --------------------------------------------------------------
CUSTOMERS = [
    ("Budi", "Santoso", "081234567890", "budi.santoso@example.com", "1988-04-23", "Jl. Mawar No. 12, Balikpapan"),
    ("Siti", "Rahmawati", "081355501122", "siti.r@example.com", "1992-11-05", "Jl. Melati 3, Samarinda"),
    ("Agus", "Prasetyo", "085700112233", None, "1975-12-01", "Jl. Sudirman 88, Balikpapan"),
    ("Dewi", "Lestari", "082198765432", "dewi.lestari@example.com", "1995-02-17", "Perum Griya Asri B7"),
    ("Rudi", "Hartono", "087811223344", None, None, "Jl. Ahmad Yani 45"),
    ("Ani", "Wijaya", "081277788899", "ani.w@example.com", "1990-08-30", None),
    ("Hendra", "Gunawan", "081911100022", None, "1983-06-14", "Jl. Pelabuhan 9"),
    ("Maya", "Sari", "085211122233", "maya.sari@example.com", "1998-01-09", "Jl. Cendana 21"),
    ("Joko", "Susilo", "081399988877", None, "1970-03-26", "Kampung Baru RT 05"),
    ("Lina", "Marlina", "082144455566", "lina.m@example.com", "1987-09-12", "Jl. Kenanga 6"),
    ("Eko", "Saputra", "087755544433", None, "1993-05-20", "Jl. Gunung Malang 14"),
    ("Rina", "Kusuma", "081222233344", "rina.kusuma@example.com", "1991-10-03", "Jl. Pahlawan 77"),
    ("Tono", "Wibowo", "085322211100", None, None, None),
    ("Yuni", "Astuti", "081566677788", "yuni.a@example.com", "1985-07-28", "Jl. Veteran 18"),
    ("Fajar", "Nugroho", "082333344455", None, "1996-12-24", "Jl. Mulawarman 31"),
    ("Putri", "Ayu", "081888899900", "putri.ayu@example.com", "2000-04-04", "Jl. Sepinggan 2"),
    ("Dian", "Permana", "087866677700", None, "1979-01-19", "Jl. Antasari 52"),
    ("Wati", "Handayani", "081611122200", "wati.h@example.com", "1989-06-06", "Jl. Pattimura 40"),
    ("Bambang", "Irawan", "085755566677", None, "1972-11-11", "Jl. Imam Bonjol 8"),
    ("Nia", "Anggraini", "082277788811", "nia.anggraini@example.com", "1994-03-15", "Jl. Diponegoro 63"),
]
customers = [db.create_customer(f, l, p, e, b, a) for f, l, p, e, b, a in CUSTOMERS]

# --- receipts over the last 30 days -------------------------------------------
PLATES = [("KT", "1234", "AB"), ("KT", "5521", "CD"), ("KT", "908", "X"), ("B", "1234", "XYZ"), ("DD", "4410", "FA"),
          ("KT", "7788", "MN"), ("L", "2200", "PQ"), ("KT", "3141", "RS"), ("AB", "6006", "TU"), ("KT", "1999", "BB")]
METHODS = ["cash", "cash", "cash", "qris", "transfer"]
sellable = [it for it in items if it["stock_qty"] > 0]

now = datetime.now(timezone.utc)
receipt_ids = []
for n in range(42):
    customer = random.choice(customers)
    region, number, suffix = random.choice(PLATES)
    lines = []
    for it in random.sample(sellable, random.randint(1, 4)):
        qty = random.randint(1, 3)
        if qty > it["stock_qty"]:
            continue
        lines.append({"product_name": it["name"], "quantity": qty, "unit_price": it["sell_price"], "sku": it["sku"]})
        it["stock_qty"] -= qty
    if not lines:
        continue
    total = sum(l["quantity"] * l["unit_price"] for l in lines)
    discount = random.choice([0, 0, 0, 1000, 2000])
    pending = random.random() < 0.3
    paid = round(total * random.uniform(0.3, 0.7), -3) if pending else total - discount
    receipt_id = f"demo-{n:03d}"
    db.create_receipt(
        receipt_id, region, number, suffix, lines, discount, int(paid), None,
        customer["phone"], f"{customer['first_name']} {customer['last_name'] or ''}".strip(),
        None, random.choice(METHODS), customer["id"],
    )
    receipt_ids.append(receipt_id)

# Spread them back over 30 days (the API always stamps "now"), oldest first.
con = sqlite3.connect(str(DEMO_DB))
for idx, rid in enumerate(receipt_ids):
    days_ago = 30 - round(idx * 30 / max(len(receipt_ids), 1))
    when = now - timedelta(days=days_ago, hours=random.randint(0, 9), minutes=random.randint(0, 59))
    if days_ago == 0:
        when = now - timedelta(minutes=random.randint(5, 240))
    stamp = when.isoformat()
    started = (when - timedelta(minutes=random.randint(3, 15))).isoformat()
    con.execute("UPDATE receipts SET created_at=?, started_at=? WHERE id=?", (stamp, started, rid))
con.commit()

# --- supplier contact details (shown on purchase invoices) --------------------
SUPPLIER_INFO = {
    "CV Sumber Plastik": ("0542-555101", "Jl. Industri No. 10, Balikpapan", "Pak Hasan"),
    "PT Mitra Kemasan": ("0541-777202", "Kawasan Niaga Blok C3, Samarinda", "Bu Linda"),
    "UD Asta Jaya": ("0542-888303", "Jl. Pasar Baru 21, Balikpapan", "Pak Asta"),
    "Sinar Plastik": ("0541-999404", "Jl. Gerbang Dayaku 5, Samarinda", "Koh Aming"),
}
for name, (phone, address, contact) in SUPPLIER_INFO.items():
    db.update_supplier(sup[name]["id"], phone=phone, address=address, contact_person=contact)

by_name = {it["name"]: it for it in items}
today = datetime.now().date()


def day(offset: int) -> str:
    return (today + timedelta(days=offset)).isoformat()


# --- inbound documents (Barang Masuk) + stock movement ----------------------------
def inbound(supplier: str, days_ago: int, inv_no: str, notes: str | None, lines: list[tuple[str, int, int]]):
    return db.create_inbound_document(
        sup[supplier]["id"], day(-days_ago), inv_no, notes,
        [{"master_item_id": by_name[n]["id"], "qty_in": q, "cost_price": c} for n, q, c in lines],
    )


posted_docs = [
    inbound("CV Sumber Plastik", 20, "SP-2231", "Restock bulanan", [("Gelas Plastik 16oz", 30, 18500), ("Sedotan Bening 6mm", 50, 4500)]),
    inbound("Sinar Plastik", 12, "SNR-0987", None, [("Kotak Makan 500ml Persegi", 24, 9200), ("Toples Plastik 1L Bening", 36, 5000)]),
    inbound("UD Asta Jaya", 5, "UAJ-410", "Kiriman sebagian", [("Ember Plastik 10L", 10, 14000)]),
]
for doc in posted_docs:
    db.post_inbound_document(doc["id"])

voided = inbound("PT Mitra Kemasan", 8, "MK-5521", "Salah input", [("Plastik Wrap 30cm x 100m", 2, 17000)])
db.post_inbound_document(voided["id"])
db.void_inbound_document(voided["id"], "Barang tidak jadi dikirim")

# Drafts: one with existing items, one introducing a brand-new product.
inbound("PT Mitra Kemasan", 1, "MK-5600", "Menunggu pengecekan fisik", [("Kantong Zip Lock 10x15", 20, 11000), ("Tali Rafia Merah", 12, 5500)])
db.create_inbound_document(
    sup["Sinar Plastik"]["id"], day(0), "SNR-1001", "Produk baru, belum ada di master",
    [{"pending_brand_id": brand["Lock&Lock"]["id"], "pending_product_name": "Kotak Bekal Sekat 3",
      "pending_unit": "pcs", "pending_sell_price": 27000, "qty_in": 15, "cost_price": 19000}],
)

# --- manual stock adjustments ---------------------------------------------------------
db.create_stock_adjustment(by_name["Gayung Plastik Besar"]["id"], -3, "Rusak / retak di gudang")
db.create_stock_adjustment(by_name["Baskom Plastik 30cm"]["id"], -1, "Hilang saat stock opname")
db.create_stock_adjustment(by_name["Toples Plastik 2L Bening"]["id"], 4, "Koreksi hasil stock opname")
db.create_stock_adjustment(by_name["Keranjang Belanja Plastik"]["id"], 5, "Retur dari pelanggan")

# --- purchase invoices (Faktur Pembelian) --------------------------------------------
def invoice(supplier: str, days_ago: int, terms_days: int, inv_no: str, discount: int, tax: float,
            lines: list[tuple[str, int, int]], notes: str | None = None):
    return db.create_purchase_invoice(
        sup[supplier]["id"], day(-days_ago), day(-days_ago + terms_days), f"NET {terms_days}",
        inv_no, notes, discount, tax,
        [{"master_item_id": by_name[n]["id"], "product_name": n, "sku": by_name[n]["sku"],
          "unit": by_name[n]["unit"], "qty": q, "unit_price": p} for n, q, p in lines],
    )


inv_paid = invoice("CV Sumber Plastik", 40, 30, "SP-2101", 0, 11, [("Gelas Plastik 16oz", 40, 18000), ("Gelas Plastik 22oz", 20, 22000)])
inv_partial = invoice("Sinar Plastik", 15, 30, "SNR-0950", 25000, 11, [("Kotak Makan 750ml Persegi", 30, 13000), ("Kotak Makan 1000ml Bulat", 20, 16000)])
inv_overdue = invoice("UD Asta Jaya", 50, 30, "UAJ-377", 0, 0, [("Ember Plastik 15L", 15, 19000), ("Baskom Plastik 30cm", 20, 9000)], "Sudah lewat jatuh tempo")
inv_unpaid = invoice("PT Mitra Kemasan", 3, 14, "MK-5588", 0, 11, [("Kantong Zip Lock 15x20", 25, 15000)])
inv_void = invoice("CV Sumber Plastik", 10, 30, "SP-2200", 0, 0, [("Sedotan Bening 6mm", 100, 4500)])
invoice("Sinar Plastik", 0, 30, "SNR-1002", 0, 11, [("Botol Plastik 500ml Bening", 12, 36000)], "Draft, belum diposting")

for inv in (inv_paid, inv_partial, inv_overdue, inv_unpaid, inv_void):
    db.post_purchase_invoice(inv["id"])
db.record_purchase_invoice_payment(inv_paid["id"], inv_paid["total"])
db.record_purchase_invoice_payment(inv_partial["id"], 300000)
db.void_purchase_invoice(inv_void["id"], "Faktur ganda dari supplier")

# --- preorders (Pesanan) -----------------------------------------------------------------
PO = [
    ("po-demo-001", "Budi Santoso", "081234567890", 150000, "Untuk renovasi kamar mandi",
     [("Ember Plastik 15L", "pcs", 4, 28000, "pesanan_dibuat"), ("Gayung Plastik Besar", "pcs", 4, 8000, "pesanan_dibuat")]),
    ("po-demo-002", "Siti Rahmawati", "081355501122", 0, None,
     [("Rak Piring Plastik 2 Susun", "pcs", 2, 36000, "sudah_dipesankan")]),
    ("po-demo-003", "Warung Bu Tini", "085611122233", 200000, "Ambil hari Sabtu",
     [("Toples Plastik 2L Bening", "pcs", 24, 13000, "dalam_pengiriman"), ("Toples Plastik 1L Bening", "pcs", 24, 8000, "sampai_di_toko")]),
    ("po-demo-004", "Dewi Lestari", "082198765432", 90000, None,
     [("Keranjang Belanja Plastik", "pcs", 6, 25000, "sampai_di_toko")]),
    ("po-demo-005", "Hendra Gunawan", "081911100022", 500000, "Lunas, sudah diambil",
     [("Karung Plastik 50kg", "lusin", 5, 66000, "terpasang"), ("Tali Rafia Merah", "roll", 10, 9000, "terpasang")]),
]
for pid, cname, cphone, deposit, notes, lines in PO:
    db.create_preorder(
        pid, cname, cphone,
        [{"master_item_id": by_name[n]["id"], "product_name": n, "unit": u, "quantity": q, "unit_price": p, "status": st}
         for n, u, q, p, st in lines],
        deposit, notes, "kasir",
    )

# --- activity log (Log Aktivitas) ---------------------------------------------------------
ACTIVITY = [
    ("admin", 1, "POST", "/api/auth/login", 200, "login", "user", "1", "admin masuk"),
    ("kasir", 2, "POST", "/api/auth/login", 200, "login", "user", "2", "kasir masuk"),
    ("kasir", 2, "POST", "/api/receipts", 201, "create", "receipt", "demo-001", "Struk demo-001 dibuat"),
    ("kasir", 2, "POST", "/api/preorders", 201, "create", "preorder", "po-demo-001", "Pesanan po-demo-001 dibuat"),
    ("admin", 1, "POST", "/api/inventory/inbound", 201, "create", "inbound", "1", "Barang masuk SP-2231 dibuat"),
    ("admin", 1, "POST", "/api/inventory/inbound/1/post", 200, "post", "inbound", "1", "Barang masuk SP-2231 diposting"),
    ("admin", 1, "POST", "/api/inventory/adjustments", 201, "adjust", "stock", "1", "Penyesuaian stok -3: Rusak"),
    ("admin", 1, "POST", "/api/purchase-invoices", 201, "create", "purchase_invoice", "1", "Faktur SP-2101 dibuat"),
    ("admin", 1, "POST", "/api/purchase-invoices/5/void", 200, "void", "purchase_invoice", "5", "Faktur SP-2200 dibatalkan"),
    ("admin", 1, "PUT", "/api/inventory/items/3", 200, "update", "master_item", "3", "Harga jual diubah"),
    ("kasir", 2, "POST", "/api/customers", 201, "create", "customer", "20", "Pelanggan Nia Anggraini ditambahkan"),
    ("kasir", 2, "DELETE", "/api/receipts/demo-040", 403, "delete", "receipt", "demo-040", "Ditolak: hanya admin"),
]
for i, (user, uid, method, path, code, action, entity, eid, summary) in enumerate(ACTIVITY):
    db.insert_activity({
        "created_at": (now - timedelta(hours=(len(ACTIVITY) - i) * 5)).isoformat(),
        "user_id": uid, "username": user, "method": method, "path": path, "status_code": code,
        "action": action, "entity": entity, "entity_id": eid, "summary": summary, "ip": "127.0.0.1",
    })

# --- scraper job (hasil scraping) ---------------------------------------------------------
db.create_job("demo-job-1", "toko plastik", "Balikpapan", ["Balikpapan Selatan", "Balikpapan Tengah"])
for name, address, phone, lat, lng in [
    ("Toko Plastik Maju Jaya", "Jl. Jend. Sudirman 12, Balikpapan", "0542-421001", -1.2654, 116.8312),
    ("Sumber Kemasan", "Jl. MT Haryono 88, Balikpapan", "0542-421002", -1.2447, 116.8528),
    ("Grosir Plastik Mandiri", "Jl. Ahmad Yani 5, Balikpapan", "0542-421003", -1.2379, 116.8420),
    ("Toko Berkah Plastik", "Jl. Letjen Suprapto 30, Balikpapan", "0542-421004", -1.2701, 116.8266),
]:
    db.insert_place("demo-job-1", name, address, phone, lat, lng)
db.insert_log("demo-job-1", "Demo job: 4 tempat ditemukan")
db.update_job_status("demo-job-1", "done", 100, None, now.isoformat())

# Backdate the older documents so lists/aging reports look realistic.
con.execute("UPDATE purchase_invoices SET created_at=invoice_date||'T08:00:00+00:00', updated_at=invoice_date||'T08:00:00+00:00'")
con.commit()

stats = {
    "suppliers": con.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0],
    "products": con.execute("SELECT COUNT(*) FROM master_items").fetchone()[0],
    "products with photo": con.execute("SELECT COUNT(*) FROM master_items WHERE image_path IS NOT NULL").fetchone()[0],
    "customers": con.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
    "receipts": con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
    "pending": con.execute("SELECT COUNT(*) FROM receipts WHERE status='pending'").fetchone()[0],
    "done": con.execute("SELECT COUNT(*) FROM receipts WHERE status='done'").fetchone()[0],
    "inbound docs": con.execute("SELECT COUNT(*) FROM inbound_documents").fetchone()[0],
    "stock ledger rows": con.execute("SELECT COUNT(*) FROM stock_ledger").fetchone()[0],
    "purchase invoices": con.execute("SELECT COUNT(*) FROM purchase_invoices").fetchone()[0],
    "preorders": con.execute("SELECT COUNT(*) FROM preorders").fetchone()[0],
    "activity rows": con.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0],
    "scraper places": con.execute("SELECT COUNT(*) FROM places").fetchone()[0],
}
con.close()
print(f"Demo DB written to {DEMO_DB}")
for k, v in stats.items():
    print(f"  {k}: {v}")
print("Login: admin / demo1234   (staff: kasir / demo1234)")
print(f"Run:   DB_PATH={DEMO_DB.relative_to(ROOT)} PRODUCT_IMAGE_DIR={DEMO_IMAGES.relative_to(ROOT)} python run.py")
