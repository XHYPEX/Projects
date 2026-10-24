"""Surat Penawaran Harga (Blueprint Penawaran Harga, 23-10-2026).

A quotation is a pre-sale letter: it never writes stock, a nota, piutang, a payment or an incentive (QT-001). One
quotation is one offer with one total; an alternative is a Duplikat with its own number (QT-018).

    draft ──issue──▶ issued ──revise──▶ draft (revision R01…) ──issue──▶ issued …
      └──cancel──▶ cancelled             issued ──cancel──▶ cancelled

The working copy (sales_quotations + sales_quotation_lines) is edited only while the quotation is a draft. Issuing
freezes it into an immutable version row (snapshot JSON: recipient, lines, texts, shop identity, bank account, totals)
that every preview, print and re-print renders from (QT-014/015/017). A revision gets the next number suffix (-R01),
the earlier version stays readable. Delivery (sent or not) and the customer's decision are separate from the document
state (blueprint §4); "Kedaluwarsa" is computed from valid_until.

Prices start from Master Barang. A price under the master price or a line discount is a special price reviewed exactly
like K-22 on Kasir / Sales Order (database._price_review: staff ask with a reason, an Admin approves, below cost is
blocked); the master price never changes (QT-003). The quotation-level discount follows the same rule. Conversion only
prefills Kasir or Sales Order, which check stock and price again on save; the saved document is linked here once
(QT-023/024).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from backend import company
from backend import database as db

STATUS_LABELS = {"draft": "Draft", "revisi": "Draft Revisi", "terbit": "Diterbitkan", "dikirim": "Dikirim",
                 "disetujui": "Disetujui", "ditolak": "Ditolak", "kedaluwarsa": "Kedaluwarsa", "dikonversi": "Dikonversi",
                 "dibatalkan": "Dibatalkan"}
RECIPIENT_TYPES = {"person": "Perorangan", "company": "Perusahaan"}
THEMES = {"modern": "Modern Minimal", "formal": "Professional Formal"}
CHANNELS = {"whatsapp": "WhatsApp", "email": "Email", "langsung": "Diserahkan langsung", "lainnya": "Lainnya"}
TARGETS = {"kasir": "Transaksi Kasir", "so": "Sales Order"}
TEMPLATES = {
    "ramah": {
        "label": "Profesional Ramah", "version": "ramah-v1",
        "opening": ("Dengan hormat,\n\nTerima kasih atas kesempatan yang diberikan kepada kami untuk membantu memenuhi kebutuhan "
                    "aksesori kendaraan Bapak/Ibu. Menindaklanjuti kebutuhan tersebut, bersama surat ini kami dari {toko} "
                    "menyampaikan penawaran harga sebagai bahan pertimbangan."),
        "table": "Berikut rincian produk dan jasa yang kami tawarkan untuk kebutuhan kendaraan Bapak/Ibu:",
        "closing": ("Demikian surat penawaran ini kami sampaikan. Apabila Bapak/Ibu memerlukan informasi lebih lanjut atau "
                    "penyesuaian pilihan produk dan pekerjaan, kami dengan senang hati siap membantu. Atas perhatian dan "
                    "kepercayaan Bapak/Ibu, kami mengucapkan terima kasih."),
    },
    "formal": {
        "label": "Formal Profesional", "version": "formal-v1",
        "opening": ("Dengan hormat,\n\nSehubungan dengan kebutuhan pengadaan aksesori dan perlengkapan kendaraan, bersama surat "
                    "ini kami dari {toko} menyampaikan penawaran harga untuk dipertimbangkan oleh {penerima}. Rincian produk "
                    "dan jasa kami sajikan sebagai berikut."),
        "table": "Berikut rincian produk dan jasa yang kami tawarkan:",
        "closing": ("Demikian surat penawaran ini kami sampaikan. Apabila Bapak/Ibu memerlukan informasi lebih lanjut atau "
                    "penyesuaian pilihan produk dan pekerjaan, kami dengan senang hati siap membantu. Atas perhatian dan "
                    "kerja sama yang baik, kami mengucapkan terima kasih."),
    },
}
DEFAULT_TERMS = ("Ketersediaan produk dikonfirmasi kembali pada saat pemesanan.\n"
                 "Pelaksanaan pemasangan mengikuti kesepakatan jadwal.\n"
                 "Pembayaran mengikuti kesepakatan.")
_TEXT_LIMITS = {"recipient_name": 120, "company_name": 160, "pic_name": 120, "pic_title": 80, "office_address": 300,
                "whatsapp": 30, "email": 120, "vehicle": 120, "subject": 160, "officer": 80, "signer_name": 80, "signer_title": 80,
                "opening_text": 3000, "table_text": 600, "closing_text": 3000, "terms_text": 2000}
_DISC_COLS = ("discount_reason", "discount_state", "discount_requested_by", "discount_requested_at", "discount_approved_by",
              "discount_approved_at")
_DRAFT_FIELDS = ("recipient_type", "recipient_name", "company_name", "pic_name", "pic_title", "office_address", "whatsapp", "email",
                 "vehicle", "subject", "valid_until", "officer", "template", "pdf_theme", "opening_text", "table_text", "closing_text",
                 "terms_text", "signer_name", "signer_title", "show_bank", "bank_account_id", "discount", *_DISC_COLS)
_LINE_FIELDS = ("position", "item_type", "master_item_id", "sku", "product_name", "unit", "quantity", "unit_price", "line_discount",
                *db._PRICE_COLS)


class Conflict(ValueError):
    """The quotation moved on (already issued, converted): HTTP 409."""


def migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sales_quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_number TEXT NOT NULL UNIQUE,
            recipient_type TEXT NOT NULL DEFAULT 'person' CHECK (recipient_type IN ('person','company')),
            recipient_name TEXT, company_name TEXT, pic_name TEXT, pic_title TEXT, office_address TEXT,
            whatsapp TEXT, email TEXT, vehicle TEXT, customer_id INTEGER,
            subject TEXT, issue_date TEXT, valid_until TEXT, officer TEXT,
            template TEXT NOT NULL DEFAULT 'ramah', pdf_theme TEXT NOT NULL DEFAULT 'modern',
            opening_text TEXT, table_text TEXT, closing_text TEXT, terms_text TEXT,
            signer_name TEXT, signer_title TEXT,
            show_bank INTEGER NOT NULL DEFAULT 0, bank_account_id INTEGER REFERENCES company_bank_accounts(id),
            discount INTEGER NOT NULL DEFAULT 0,
            discount_reason TEXT, discount_state TEXT, discount_requested_by TEXT, discount_requested_at TEXT,
            discount_approved_by TEXT, discount_approved_at TEXT,
            subtotal INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','issued','cancelled')),
            revision_no INTEGER NOT NULL DEFAULT 0,
            latest_version_id INTEGER,
            delivery_status TEXT NOT NULL DEFAULT 'not_sent', sent_at TEXT,
            decision TEXT NOT NULL DEFAULT 'pending', decision_note TEXT, decided_by TEXT, decided_at TEXT,
            follow_up_note TEXT, follow_up_date TEXT,
            source_quote_id INTEGER REFERENCES sales_quotations(id),
            cancel_reason TEXT, cancelled_by TEXT, cancelled_at TEXT,
            created_by TEXT, created_at TEXT NOT NULL, updated_by TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sales_quotation_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER NOT NULL REFERENCES sales_quotations(id),
            position INTEGER NOT NULL DEFAULT 0,
            item_type TEXT NOT NULL DEFAULT 'barang' CHECK (item_type IN ('barang','jasa')),
            master_item_id INTEGER REFERENCES master_items(id),
            sku TEXT, product_name TEXT NOT NULL, unit TEXT,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price INTEGER NOT NULL DEFAULT 0,
            line_discount INTEGER NOT NULL DEFAULT 0,
            list_price INTEGER, price_state TEXT, price_reason TEXT, price_requested_by TEXT, price_requested_at TEXT,
            price_approved_by TEXT, price_approved_at TEXT, price_below_cost INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sql_quote ON sales_quotation_lines(quotation_id);
        CREATE TABLE IF NOT EXISTS sales_quotation_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER NOT NULL REFERENCES sales_quotations(id),
            revision_no INTEGER NOT NULL,
            label TEXT NOT NULL,
            doc_number TEXT NOT NULL UNIQUE,
            snapshot TEXT NOT NULL,
            total INTEGER NOT NULL,
            issue_token TEXT UNIQUE,
            issued_by TEXT, issued_at TEXT NOT NULL,
            superseded_at TEXT,
            print_count INTEGER NOT NULL DEFAULT 0, last_printed_at TEXT,
            UNIQUE (quotation_id, revision_no)
        );
        CREATE TABLE IF NOT EXISTS sales_quotation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER NOT NULL REFERENCES sales_quotations(id),
            version_id INTEGER,
            kind TEXT NOT NULL,
            note TEXT, channel TEXT,
            actor TEXT, at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sqe_quote ON sales_quotation_events(quotation_id);
        CREATE TABLE IF NOT EXISTS sales_quotation_conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER NOT NULL UNIQUE REFERENCES sales_quotations(id),
            version_id INTEGER NOT NULL REFERENCES sales_quotation_versions(id),
            target_type TEXT NOT NULL CHECK (target_type IN ('kasir','so')),
            target_id TEXT NOT NULL, target_no TEXT,
            created_by TEXT, created_at TEXT NOT NULL
        );
    """)


# ── helpers ──────────────────────────────────────────────────────────────────
def _rp(n) -> str:
    return "Rp" + f"{int(n or 0):,}".replace(",", ".")


def _event(con, qid: int, kind: str, note: str | None, actor: str | None, version_id: int | None = None, channel: str | None = None):
    con.execute("INSERT INTO sales_quotation_events (quotation_id, version_id, kind, note, channel, actor, at) VALUES (?,?,?,?,?,?,?)",
                (qid, version_id, kind, note, channel, actor, db._now()))


def _get(con, qid: int) -> dict:
    r = con.execute("SELECT * FROM sales_quotations WHERE id=?", (qid,)).fetchone()
    if r is None:
        raise LookupError("Penawaran tidak ditemukan.")
    return dict(r)


def _lines(con, qid: int) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM sales_quotation_lines WHERE quotation_id=? ORDER BY position, id", (qid,))]


def _line_total(ln: dict) -> int:
    return int(ln["quantity"]) * int(ln["unit_price"]) - int(ln.get("line_discount") or 0)


def _totals(lines: list[dict], discount: int) -> tuple[int, int]:
    subtotal = sum(_line_total(ln) for ln in lines)
    return subtotal, subtotal - min(int(discount or 0), subtotal)


def _conversion(con, qid: int) -> dict | None:
    r = con.execute("SELECT * FROM sales_quotation_conversions WHERE quotation_id=?", (qid,)).fetchone()
    return dict(r) if r else None


def display_status(q: dict, converted: bool, today: date | None = None) -> str:
    today = today or db.shop_today()
    if q["status"] == "cancelled":
        return "dibatalkan"
    if converted:
        return "dikonversi"
    if q["status"] == "draft":
        return "revisi" if q.get("latest_version_id") else "draft"
    if q["decision"] == "accepted":
        return "disetujui"
    if q["decision"] == "declined":
        return "ditolak"
    if q.get("valid_until") and q["valid_until"] < today.isoformat():
        return "kedaluwarsa"
    return "dikirim" if q["delivery_status"] == "sent" else "terbit"


def _next_number(con, day: date) -> str:
    prefix = f"PNW/{day.year}/{day.month:02d}/"
    row = con.execute("SELECT MAX(CAST(SUBSTR(quote_number, ?) AS INTEGER)) AS n FROM sales_quotations WHERE quote_number LIKE ?",
                      (len(prefix) + 1, prefix + "%")).fetchone()
    return f"{prefix}{(row['n'] or 0) + 1:04d}"


def next_number() -> str:
    with db._conn() as con:
        return _next_number(con, db.shop_today())


def default_texts(template: str, recipient: str = "") -> dict:
    t = TEMPLATES.get(template) or TEMPLATES["ramah"]
    shop = company.settings()["name"]
    fill = lambda s: s.replace("{toko}", shop).replace("{penerima}", recipient or "perusahaan Bapak/Ibu")
    return {"opening_text": fill(t["opening"]), "table_text": fill(t["table"]), "closing_text": fill(t["closing"]), "terms_text": DEFAULT_TERMS}


def _clean_text(v, key: str) -> str | None:
    v = str(v or "").strip()
    if len(v) > _TEXT_LIMITS[key]:
        raise ValueError(f"Isian terlalu panjang (maks. {_TEXT_LIMITS[key]} karakter).")
    return v or None


def _int(v, label: str, minimum: int = 0) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} harus berupa angka.")
    if n < minimum:
        raise ValueError(f"{label} minimal {minimum}.")
    return n


# ── draft: create / edit ─────────────────────────────────────────────────────
def _apply_draft(con, q: dict | None, data: dict, actor: dict) -> tuple[dict, list[dict]]:
    """The fields and lines to store for a draft. Special prices go through K-22's review (scope 'quotation')."""
    fields = {}
    rtype = data.get("recipient_type", (q or {}).get("recipient_type") or "person")
    if rtype not in RECIPIENT_TYPES:
        raise ValueError("Jenis penerima harus Perorangan atau Perusahaan.")
    fields["recipient_type"] = rtype
    for key in ("recipient_name", "company_name", "pic_name", "pic_title", "office_address", "whatsapp", "email", "vehicle",
                "subject", "officer", "signer_name", "signer_title", "opening_text", "table_text", "closing_text", "terms_text"):
        fields[key] = _clean_text(data[key], key) if key in data else (q or {}).get(key)
    if fields.get("whatsapp"):
        phone = db.normalize_phone(fields["whatsapp"])
        if not db.PHONE_RE.match(phone):
            raise ValueError("Nomor WhatsApp tidak valid (7–15 angka).")
        fields["whatsapp"] = phone
    if fields.get("email") and ("@" not in fields["email"] or " " in fields["email"]):
        raise ValueError("Email tidak valid.")
    valid = data.get("valid_until", (q or {}).get("valid_until"))
    if valid:
        try:
            db.parse_ymd(valid)
        except ValueError:
            raise ValueError("Masa berlaku harus tanggal (YYYY-MM-DD).")
    fields["valid_until"] = valid or None
    template = data.get("template", (q or {}).get("template") or ("formal" if rtype == "company" else "ramah"))
    if template not in TEMPLATES:
        raise ValueError("Template naskah tidak dikenal.")
    fields["template"] = template
    theme = data.get("pdf_theme", (q or {}).get("pdf_theme") or ("formal" if rtype == "company" else "modern"))
    if theme not in THEMES:
        raise ValueError("Tema PDF tidak dikenal.")
    fields["pdf_theme"] = theme
    fields["show_bank"] = 1 if data.get("show_bank", (q or {}).get("show_bank")) else 0
    bank_id = data.get("bank_account_id", (q or {}).get("bank_account_id")) or None
    if bank_id is not None:
        bank_id = _int(bank_id, "Rekening", 1)
        if not con.execute("SELECT 1 FROM company_bank_accounts WHERE id=?", (bank_id,)).fetchone():
            raise ValueError("Rekening tidak terdaftar di Pengaturan Perusahaan.")
    fields["bank_account_id"] = bank_id

    # lines
    prev_lines = {ln["id"]: ln for ln in (_lines(con, q["id"]) if q else [])}
    raw_lines = data.get("lines")
    lines = []
    if raw_lines is None:
        lines = list(prev_lines.values())
    else:
        if not isinstance(raw_lines, list) or len(raw_lines) > 200:
            raise ValueError("Daftar produk tidak valid (maks. 200 baris).")
        doc_id = str(q["id"]) if q else "baru"
        for pos, raw in enumerate(raw_lines):
            kind = raw.get("item_type") or "barang"
            if kind not in ("barang", "jasa"):
                raise ValueError("Jenis baris harus barang atau jasa.")
            prev = prev_lines.get(raw.get("id")) if raw.get("id") else None
            qty = _int(raw.get("quantity", 1), "Qty", 1)
            price = _int(raw.get("unit_price", 0), "Harga")
            disc = _int(raw.get("line_discount", 0) or 0, "Diskon")
            if disc > qty * price:
                raise ValueError("Diskon baris tidak boleh melebihi jumlahnya.")
            ln = {"id": prev["id"] if prev else None, "position": pos, "item_type": kind, "quantity": qty, "unit_price": price,
                  "line_discount": disc}
            if kind == "barang":
                mid = raw.get("master_item_id") or (prev or {}).get("master_item_id")
                mi = con.execute("SELECT id, sku, name, unit, is_active FROM master_items WHERE id=?", (mid,)).fetchone() if mid else None
                if mi is None:
                    raise ValueError("Barang harus dipilih dari Master Barang (QT-002).")
                if not mi["is_active"] and not (prev and prev["master_item_id"] == mi["id"]):
                    raise ValueError(f"“{mi['name']}” sudah nonaktif di Master Barang.")
                same = prev is not None and prev["master_item_id"] == mi["id"]
                ln.update(master_item_id=mi["id"], sku=mi["sku"], product_name=prev["product_name"] if same else mi["name"], unit=mi["unit"])
                ln.update(db._price_review(con, scope="quotation", doc_id=doc_id, master_item_id=mi["id"], product_name=ln["product_name"],
                                           qty=qty, unit_price=price, line_discount=disc,
                                           prev=prev if same else None,
                                           reason=raw.get("price_reason"), allow_below_cost=bool(raw.get("allow_below_cost")), actor=actor))
            else:
                name = str(raw.get("product_name") or "").strip()
                if not name or len(name) > 160:
                    raise ValueError("Nama jasa wajib diisi (maks. 160 karakter).")
                unit = str(raw.get("unit") or "JASA").strip()[:20] or "JASA"
                ln.update(master_item_id=None, sku=None, product_name=name, unit=unit, **db._NO_PRICE)
            lines.append(ln)

    # quotation-level discount: reviewed like a nota discount (a reason; an Admin approves a staff request)
    subtotal = sum(_line_total(ln) for ln in lines)
    disc = _int(data.get("discount", (q or {}).get("discount") or 0) or 0, "Diskon keseluruhan")
    if disc > subtotal:
        raise ValueError("Diskon keseluruhan tidak boleh melebihi subtotal.")
    prev_disc = (q or {}).get("discount") or 0
    if disc == 0:
        fields.update({c: None for c in _DISC_COLS}, discount=0)
    elif q and disc == prev_disc and q.get("discount_state"):
        fields.update({c: q.get(c) for c in _DISC_COLS}, discount=disc)
    else:
        reason = str(data.get("discount_reason") or "").strip()
        if len(reason) < 3:
            raise ValueError("Alasan diskon keseluruhan wajib diisi.")
        now = db._now()
        approved = actor["is_admin"]
        fields.update(discount=disc, discount_reason=reason[:300], discount_state="approved" if approved else "pending",
                      discount_requested_by=actor["username"], discount_requested_at=now,
                      discount_approved_by=actor["username"] if approved else None, discount_approved_at=now if approved else None)
    fields["subtotal"], fields["total"] = _totals(lines, fields["discount"])
    return fields, lines


def _store_lines(con, qid: int, lines: list[dict]) -> None:
    keep = [ln["id"] for ln in lines if ln.get("id")]
    con.execute("DELETE FROM sales_quotation_lines WHERE quotation_id=?" + (f" AND id NOT IN ({','.join('?' * len(keep))})" if keep else ""),
                (qid, *keep))
    for ln in lines:
        vals = [ln.get(c) if c != "price_below_cost" else (ln.get(c) or 0) for c in _LINE_FIELDS]
        if ln.get("id"):
            con.execute(f"UPDATE sales_quotation_lines SET {', '.join(c + '=?' for c in _LINE_FIELDS)} WHERE id=? AND quotation_id=?",
                        (*vals, ln["id"], qid))
        else:
            con.execute(f"INSERT INTO sales_quotation_lines (quotation_id, {', '.join(_LINE_FIELDS)}) VALUES (?, {', '.join('?' * len(_LINE_FIELDS))})",
                        (qid, *vals))


def create(data: dict, user: dict) -> dict:
    actor = db.price_actor(user)
    st = company.settings()
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        rtype = data.get("recipient_type") or "person"
        template = data.get("template") or ("formal" if rtype == "company" else "ramah")
        base = {**default_texts(template, str(data.get("company_name") or "").strip()), "signer_name": st["signer_name"],
                "signer_title": st["signer_title"] or None, "officer": user["username"],
                "valid_until": (db.shop_today() + timedelta(days=st["quote_valid_days"])).isoformat(),
                "subject": "Penawaran Aksesori Kendaraan"}
        fields, lines = _apply_draft(con, None, {**base, **{k: v for k, v in data.items() if v is not None}}, actor)
        now = db._now()
        number = _next_number(con, db.shop_today())
        cols = list(fields)
        qid = con.execute(f"INSERT INTO sales_quotations (quote_number, {', '.join(cols)}, source_quote_id, created_by, created_at, updated_by, updated_at) "
                          f"VALUES (?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?)",
                          (number, *fields.values(), data.get("source_quote_id"), user["username"], now, user["username"], now)).lastrowid
        _store_lines(con, qid, lines)
        con.execute("UPDATE price_events SET doc_id=? WHERE scope='quotation' AND doc_id='baru'", (str(qid),))
        _event(con, qid, "create", f"Draft {number} dibuat", user["username"])
    return detail(qid, user)


def update_draft(qid: int, data: dict, user: dict) -> dict:
    actor = db.price_actor(user)
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        if q["status"] != "draft":
            raise Conflict("Penawaran sudah terbit dan tidak bisa diubah. Gunakan Buat Revisi (QT-017).")
        fields, lines = _apply_draft(con, q, data, actor)
        con.execute(f"UPDATE sales_quotations SET {', '.join(c + '=?' for c in fields)}, updated_by=?, updated_at=? WHERE id=?",
                    (*fields.values(), user["username"], db._now(), qid))
        _store_lines(con, qid, lines)
        last = con.execute("SELECT kind, actor, at FROM sales_quotation_events WHERE quotation_id=? ORDER BY id DESC LIMIT 1", (qid,)).fetchone()
        recent = last and last["kind"] == "edit" and last["actor"] == user["username"] and \
            datetime.fromisoformat(last["at"]) > datetime.now(timezone.utc) - timedelta(minutes=10)
        if not recent:  # a burst of saves by one person is one audit line; the price / issue events stay separate
            _event(con, qid, "edit", "Draft diubah", user["username"])
    return detail(qid, user)


def decide_price(qid: int, line_id: int, approve: bool, note: str | None, user: dict) -> dict:
    """Admin only. Approve keeps the special price; reject puts the line back at its Master Barang price, no discount."""
    if user.get("role") != "admin":
        raise PermissionError("Hanya Admin yang dapat menyetujui atau menolak harga khusus.")
    note = (note or "").strip()[:300] or None
    if not approve and not note:
        raise ValueError("Tulis alasan penolakan harga khusus.")
    by = user["username"]
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        if q["status"] != "draft":
            raise Conflict("Penawaran sudah terbit.")
        ln = con.execute("SELECT * FROM sales_quotation_lines WHERE id=? AND quotation_id=?", (line_id, qid)).fetchone()
        if ln is None:
            raise LookupError("Baris penawaran tidak ditemukan.")
        ln = dict(ln)
        if ln["price_state"] != "pending":
            raise ValueError("Harga khusus ini tidak sedang menunggu persetujuan.")
        qty, listp, net = ln["quantity"], ln["list_price"] or ln["unit_price"], _line_total(ln)
        if approve:
            con.execute("UPDATE sales_quotation_lines SET price_state='approved', price_approved_by=?, price_approved_at=? WHERE id=?",
                        (by, db._now(), line_id))
            db._price_event(con, "quotation", str(qid), ln["product_name"], "approved", qty, listp, None, round(net / qty), qty * listp - net,
                            note or ln["price_reason"], ln["price_below_cost"] or 0, by)
            _event(con, qid, "price", f"{ln['product_name']}: harga khusus DISETUJUI Admin", by)
        else:
            con.execute(f"UPDATE sales_quotation_lines SET unit_price=?, line_discount=0, {', '.join(c + '=?' for c in db._PRICE_COLS)} WHERE id=?",
                        (listp, *db._NO_PRICE.values(), line_id))
            db._price_event(con, "quotation", str(qid), ln["product_name"], "rejected", qty, listp, round(net / qty), listp, 0, note, 0, by)
            _event(con, qid, "price", f"{ln['product_name']}: harga khusus DITOLAK Admin ({note}); kembali ke harga Master Barang", by)
        _retotal(con, qid)
    return detail(qid, user)


def decide_discount(qid: int, approve: bool, note: str | None, user: dict) -> dict:
    if user.get("role") != "admin":
        raise PermissionError("Hanya Admin yang dapat menyetujui atau menolak diskon.")
    note = (note or "").strip()[:300] or None
    if not approve and not note:
        raise ValueError("Tulis alasan penolakan diskon.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        if q["status"] != "draft":
            raise Conflict("Penawaran sudah terbit.")
        if q["discount_state"] != "pending":
            raise ValueError("Diskon keseluruhan tidak sedang menunggu persetujuan.")
        if approve:
            con.execute("UPDATE sales_quotations SET discount_state='approved', discount_approved_by=?, discount_approved_at=? WHERE id=?",
                        (user["username"], db._now(), qid))
            _event(con, qid, "price", f"Diskon keseluruhan {_rp(q['discount'])} DISETUJUI Admin", user["username"])
        else:
            con.execute(f"UPDATE sales_quotations SET discount=0, {', '.join(c + '=NULL' for c in _DISC_COLS)} WHERE id=?", (qid,))
            _event(con, qid, "price", f"Diskon keseluruhan {_rp(q['discount'])} DITOLAK Admin ({note})", user["username"])
        _retotal(con, qid)
    return detail(qid, user)


def _retotal(con, qid: int) -> None:
    q = _get(con, qid)
    subtotal, total = _totals(_lines(con, qid), q["discount"])
    con.execute("UPDATE sales_quotations SET subtotal=?, total=? WHERE id=?", (subtotal, total, qid))


# ── checks, snapshot, preview, issue ─────────────────────────────────────────
def _recipient_name(q: dict) -> str:
    return (q.get("company_name") if q["recipient_type"] == "company" else q.get("recipient_name")) or ""


def _checks(con, q: dict, lines: list[dict], today: date) -> list[dict]:
    """Pemeriksaan Sebelum Penerbitan (mockup 07/14). level: block (fix first), approval (an Admin decides), action."""
    out = []

    def add(key, label, ok, level="block", hint=""):
        out.append({"key": key, "label": label, "ok": bool(ok), "level": level, "hint": "" if ok else hint})

    if q["recipient_type"] == "company":
        add("recipient", "Sapaan penerima benar", q.get("company_name"), hint="Nama perusahaan wajib diisi.")
    else:
        add("recipient", "Sapaan penerima benar", q.get("recipient_name"), hint="Nama customer wajib diisi.")
    add("subject", "Perihal surat terisi", q.get("subject"), hint="Perihal wajib diisi.")
    add("texts", "Paragraf pembuka & penutup ada", q.get("opening_text") and q.get("closing_text"),
        hint="Paragraf pembuka dan penutup tidak boleh kosong.")
    add("lines", "Rincian harga sesuai produk", lines, hint="Tambahkan minimal satu produk atau jasa (QT-010).")
    inactive = [ln["product_name"] for ln in lines if ln["master_item_id"] and not
                (con.execute("SELECT is_active FROM master_items WHERE id=?", (ln["master_item_id"],)).fetchone() or {"is_active": 0})["is_active"]]
    if inactive:
        add("inactive", "Produk masih aktif", False, hint="Nonaktif di Master Barang: " + ", ".join(inactive))
    pending = [ln["product_name"] for ln in lines if ln.get("price_state") == "pending"]
    add("prices", "Harga khusus disetujui", not pending, "approval", "Menunggu persetujuan Admin: " + ", ".join(pending))
    add("discount", "Diskon keseluruhan disetujui", q.get("discount_state") != "pending", "approval",
        "Diskon keseluruhan menunggu persetujuan Admin.")
    add("total", "Satu total penawaran", True)
    add("dates", "Ketentuan dan tanggal tampil", q.get("valid_until") and q["valid_until"] >= today.isoformat(),
        hint="Masa berlaku harus hari ini atau sesudahnya.")
    add("signer", "Penanggung jawab terisi", q.get("signer_name"), hint="Isi nama penanda tangan.")
    if q.get("show_bank"):
        add("bank", "Tidak ada rekening fiktif", company.active_account(con, q.get("bank_account_id")), "action",
            "Rekening tidak tersedia: pilih rekening resmi aktif atau matikan opsi rekening.")
    else:
        add("bank", "Tidak ada rekening fiktif", True)
    return out


def _blocking(checks: list[dict]) -> list[dict]:
    return [c for c in checks if not c["ok"]]


def _snapshot(con, q: dict, lines: list[dict], issue_date: date, label: str, doc_number: str) -> dict:
    st = company.settings(con)
    bank = company.active_account(con, q.get("bank_account_id")) if q.get("show_bank") else None
    subtotal, total = _totals(lines, q["discount"])
    if q["recipient_type"] == "company":
        to = [q.get("company_name") or ""]
        if q.get("pic_name"):
            to.append(f"Up. {q['pic_name']}" + (f" ({q['pic_title']})" if q.get("pic_title") else ""))
        else:
            to = [f"Manajemen {q.get('company_name') or ''}"]
        if q.get("office_address"):
            to.append(q["office_address"])
    else:
        to = [q.get("recipient_name") or ""]
    to.append("Di Tempat")
    valid = q.get("valid_until") or ""
    terms = [f"Penawaran berlaku hingga {_tanggal(valid)}."] + [t.strip() for t in (q.get("terms_text") or "").splitlines() if t.strip()]
    tpl = TEMPLATES.get(q["template"]) or TEMPLATES["ramah"]
    return {
        "doc_number": doc_number, "quote_number": q["quote_number"], "label": label, "revision_no": q["revision_no"],
        "issue_city": st["city"], "issue_date": issue_date.isoformat(), "valid_until": valid, "subject": q.get("subject") or "",
        "recipient": {"type": q["recipient_type"], "type_label": RECIPIENT_TYPES[q["recipient_type"]], "name": _recipient_name(q),
                      "person_name": q.get("recipient_name"), "company_name": q.get("company_name"), "pic_name": q.get("pic_name"),
                      "pic_title": q.get("pic_title"), "address": q.get("office_address"), "whatsapp": q.get("whatsapp"),
                      "email": q.get("email"), "vehicle": q.get("vehicle"), "to_lines": to},
        "company": {k: st[k] for k in ("name", "tagline", "city", "region", "address", "phone", "email")},
        "lines": [{"no": i + 1, "item_type": ln["item_type"], "master_item_id": ln["master_item_id"], "sku": ln.get("sku"),
                   "name": ln["product_name"], "unit": ln.get("unit") or "", "qty": ln["quantity"], "price": ln["unit_price"],
                   "list_price": ln.get("list_price"), "discount": ln.get("line_discount") or 0, "total": _line_total(ln),
                   "price_state": ln.get("price_state"), "price_reason": ln.get("price_reason")} for i, ln in enumerate(lines)],
        "subtotal": subtotal, "discount": min(q["discount"] or 0, subtotal), "total": total,
        "texts": {"opening": q.get("opening_text") or "", "table": q.get("table_text") or "", "closing": q.get("closing_text") or ""},
        "terms": terms,
        "bank": {k: bank[k] for k in ("id", "bank_name", "account_number", "account_name")} if bank else None,
        "signer": {"name": q.get("signer_name") or "", "title": q.get("signer_title") or ""},
        "officer": q.get("officer") or "", "template": q["template"], "template_version": tpl["version"],
        "theme": q["pdf_theme"], "theme_label": THEMES[q["pdf_theme"]],
        # the working copy as it was, so cancelling a later revision draft restores exactly this
        "draft": {"fields": {k: q.get(k) for k in _DRAFT_FIELDS}, "lines": [{c: ln.get(c) for c in _LINE_FIELDS} for ln in lines]},
    }


_MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]


def _tanggal(ymd: str) -> str:
    try:
        d = db.parse_ymd(ymd)
    except ValueError:
        return ymd or "-"
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _doc_number(q: dict) -> tuple[str, str]:
    n = q["revision_no"]
    return ("V01", q["quote_number"]) if n == 0 else (f"R{n:02d}", f"{q['quote_number']}-R{n:02d}")


def preview(qid: int, theme: str | None = None) -> dict:
    """What issuing would print now, computed by the same backend code (QT-016). Nothing is stored."""
    with db._conn() as con:
        q = _get(con, qid)
        if q["status"] != "draft":
            v = con.execute("SELECT * FROM sales_quotation_versions WHERE id=?", (q["latest_version_id"],)).fetchone()
            return {"snapshot": json.loads(v["snapshot"]), "checks": [], "issued": True}
        if theme in THEMES:
            q["pdf_theme"] = theme
        lines, today = _lines(con, qid), db.shop_today()
        label, number = _doc_number(q)
        return {"snapshot": _snapshot(con, q, lines, today, label, number), "checks": _checks(con, q, lines, today), "issued": False}


def issue(qid: int, token: str | None, user: dict, theme: str | None = None) -> dict:
    token = (token or "").strip()[:80] or None
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        if token:
            done = con.execute("SELECT quotation_id FROM sales_quotation_versions WHERE issue_token=?", (token,)).fetchone()
            if done:  # the same click sent twice: one version only (QT-022)
                if done["quotation_id"] != qid:
                    raise Conflict("Token penerbitan sudah dipakai dokumen lain.")
                con.rollback()
                return detail(qid, user)
        q = _get(con, qid)
        if q["status"] != "draft":
            raise Conflict("Penawaran ini sudah terbit. Untuk mengubah, gunakan Buat Revisi.")
        if theme in THEMES and theme != q["pdf_theme"]:
            q["pdf_theme"] = theme
            con.execute("UPDATE sales_quotations SET pdf_theme=? WHERE id=?", (theme, qid))
        lines, today = _lines(con, qid), db.shop_today()
        bad = _blocking(_checks(con, q, lines, today))
        if bad:
            raise ValueError("Belum bisa diterbitkan: " + "; ".join(c["hint"] for c in bad))
        label, number = _doc_number(q)
        snap = _snapshot(con, q, lines, today, label, number)
        now = db._now()
        if q.get("latest_version_id"):
            con.execute("UPDATE sales_quotation_versions SET superseded_at=? WHERE id=?", (now, q["latest_version_id"]))
        vid = con.execute("""INSERT INTO sales_quotation_versions (quotation_id, revision_no, label, doc_number, snapshot, total,
                             issue_token, issued_by, issued_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                          (qid, q["revision_no"], label, number, json.dumps(snap, ensure_ascii=False), snap["total"], token,
                           user["username"], now)).lastrowid
        con.execute("""UPDATE sales_quotations SET status='issued', latest_version_id=?, issue_date=?, subtotal=?, total=?,
                       delivery_status='not_sent', sent_at=NULL, decision='pending', decision_note=NULL, decided_by=NULL, decided_at=NULL,
                       updated_by=?, updated_at=? WHERE id=?""",
                    (vid, today.isoformat(), snap["subtotal"], snap["total"], user["username"], now, qid))
        _event(con, qid, "issue", f"{label} terbit — {number}, total {_rp(snap['total'])}", user["username"], vid)
    return detail(qid, user)


def version(qid: int, vid: int) -> dict:
    with db._conn() as con:
        v = con.execute("SELECT * FROM sales_quotation_versions WHERE id=? AND quotation_id=?", (vid, qid)).fetchone()
        if v is None:
            raise LookupError("Versi penawaran tidak ditemukan.")
        out = {k: v[k] for k in v.keys() if k not in ("snapshot", "issue_token")}
        out["snapshot"] = json.loads(v["snapshot"])
        return out


def log_print(qid: int, vid: int, user: dict) -> dict:
    with db._conn() as con:
        v = con.execute("SELECT label FROM sales_quotation_versions WHERE id=? AND quotation_id=?", (vid, qid)).fetchone()
        if v is None:
            raise LookupError("Versi penawaran tidak ditemukan.")
        con.execute("UPDATE sales_quotation_versions SET print_count=print_count+1, last_printed_at=? WHERE id=?", (db._now(), vid))
        _event(con, qid, "print", f"{v['label']} dicetak / diunduh PDF", user["username"], vid)
    return {"ok": True}


# ── after issue: delivery, follow-up, decision, revision, duplicate, cancel ───
def _issued(con, qid: int) -> dict:
    q = _get(con, qid)
    if q["status"] != "issued":
        raise Conflict("Hanya penawaran yang sudah terbit." if q["status"] == "draft" else "Penawaran sudah dibatalkan.")
    return q


def mark_sent(qid: int, data: dict, user: dict) -> dict:
    channel = data.get("channel") or "whatsapp"
    if channel not in CHANNELS:
        raise ValueError("Kanal pengiriman tidak dikenal.")
    to = str(data.get("recipient") or "").strip()[:120]
    note = str(data.get("note") or "").strip()[:300]
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _issued(con, qid)
        now = db._now()
        con.execute("UPDATE sales_quotations SET delivery_status='sent', sent_at=?, updated_by=?, updated_at=? WHERE id=?",
                    (now, user["username"], now, qid))
        text = f"Dikirim via {CHANNELS[channel]}" + (f" ke {to}" if to else "") + (f" — {note}" if note else "")
        _event(con, qid, "send", text, user["username"], q["latest_version_id"], channel)
    return detail(qid, user)


def set_follow_up(qid: int, data: dict, user: dict) -> dict:
    note = str(data.get("note") or "").strip()[:300] or None
    day = str(data.get("date") or "").strip() or None
    if day:
        db.parse_ymd(day)
    with db._conn() as con:
        q = _get(con, qid)
        if q["status"] == "cancelled":
            raise Conflict("Penawaran sudah dibatalkan.")
        con.execute("UPDATE sales_quotations SET follow_up_note=?, follow_up_date=?, updated_by=?, updated_at=? WHERE id=?",
                    (note, day, user["username"], db._now(), qid))
        _event(con, qid, "follow_up", "Tindak lanjut: " + (note or "-") + (f" (follow-up {_tanggal(day)})" if day else ""), user["username"])
    return detail(qid, user)


def decide(qid: int, data: dict, user: dict) -> dict:
    result = data.get("result")
    if result not in ("accepted", "declined", "pending"):
        raise ValueError("Keputusan harus diterima, ditolak, atau menunggu.")
    note = str(data.get("note") or "").strip()[:300] or None
    if result == "declined" and not note:
        raise ValueError("Tulis alasan customer menolak.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _issued(con, qid)
        if _conversion(con, qid):
            raise Conflict("Penawaran sudah dikonversi; keputusan tidak bisa diubah.")
        now = db._now()
        con.execute("UPDATE sales_quotations SET decision=?, decision_note=?, decided_by=?, decided_at=?, updated_by=?, updated_at=? WHERE id=?",
                    (result, note, user["username"], now, user["username"], now, qid))
        label = {"accepted": "Diterima customer", "declined": "Ditolak customer", "pending": "Keputusan dikembalikan ke menunggu"}[result]
        _event(con, qid, "decision", label + (f" — {note}" if note else ""), user["username"], q["latest_version_id"])
    return detail(qid, user)


def revise(qid: int, user: dict) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _issued(con, qid)
        if _conversion(con, qid):
            raise Conflict("Penawaran sudah dikonversi ke transaksi; buat Duplikat untuk penawaran baru.")
        n = q["revision_no"] + 1
        con.execute("UPDATE sales_quotations SET status='draft', revision_no=?, updated_by=?, updated_at=? WHERE id=?",
                    (n, user["username"], db._now(), qid))
        _event(con, qid, "revise", f"Draft revisi R{n:02d} dibuat; versi terbit sebelumnya tetap tersimpan", user["username"], q["latest_version_id"])
    return detail(qid, user)


def cancel_revision(qid: int, user: dict) -> dict:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        if q["status"] != "draft" or not q["latest_version_id"]:
            raise Conflict("Tidak ada draft revisi untuk dibatalkan.")
        v = con.execute("SELECT * FROM sales_quotation_versions WHERE id=?", (q["latest_version_id"],)).fetchone()
        snap = json.loads(v["snapshot"])
        fields = snap["draft"]["fields"]
        con.execute(f"UPDATE sales_quotations SET {', '.join(c + '=?' for c in fields)}, status='issued', revision_no=?, subtotal=?, total=?, "
                    "updated_by=?, updated_at=? WHERE id=?",
                    (*fields.values(), v["revision_no"], snap["subtotal"], snap["total"], user["username"], db._now(), qid))
        con.execute("DELETE FROM sales_quotation_lines WHERE quotation_id=?", (qid,))
        _store_lines(con, qid, [{**ln, "id": None} for ln in snap["draft"]["lines"]])
        _event(con, qid, "revise_cancel", f"Draft revisi dibatalkan; kembali ke {v['label']}", user["username"], v["id"])
    return detail(qid, user)


def duplicate(qid: int, data: dict, user: dict) -> dict:
    """A new quotation with its own number (QT-018). Goods take today's Master Barang price; special prices are not
    carried over, so they are asked and approved again for the new letter (QT-029)."""
    with db._conn() as con:
        src = _get(con, qid)
        lines = []
        for ln in _lines(con, qid):
            if ln["item_type"] == "barang":
                mi = con.execute("SELECT id, sell_price, is_active FROM master_items WHERE id=?", (ln["master_item_id"],)).fetchone()
                if mi is None or not mi["is_active"]:
                    continue
                lines.append({"item_type": "barang", "master_item_id": mi["id"], "quantity": ln["quantity"], "unit_price": mi["sell_price"],
                              "line_discount": 0})
            else:
                lines.append({k: ln[k] for k in ("item_type", "product_name", "unit", "quantity", "unit_price")})
        skipped = sum(1 for ln in _lines(con, qid) if ln["item_type"] == "barang") - sum(1 for ln in lines if ln["item_type"] == "barang")
    body = {k: src[k] for k in ("recipient_type", "recipient_name", "company_name", "pic_name", "pic_title", "office_address", "whatsapp",
                                "email", "vehicle", "template", "pdf_theme", "opening_text", "table_text", "closing_text", "terms_text",
                                "signer_name", "signer_title", "show_bank", "bank_account_id")}
    subject = str(data.get("subject") or "").strip() or (src["subject"] or "")
    body.update(subject=subject, lines=lines, source_quote_id=qid, discount=0)
    out = create(body, user)
    with db._conn() as con:
        _event(con, qid, "duplicate", f"Diduplikat menjadi {out['quote_number']} (penawaran alternatif)", user["username"])
        _event(con, out["id"], "duplicate", f"Duplikat dari {src['quote_number']}"
               + (f"; {skipped} barang nonaktif tidak disalin" if skipped else ""), user["username"])
    out["skipped_inactive"] = skipped
    return out


def cancel(qid: int, reason: str, user: dict) -> dict:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Tulis alasan pembatalan.")
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        if q["status"] == "cancelled":
            raise Conflict("Penawaran sudah dibatalkan.")
        if _conversion(con, qid):
            raise Conflict("Penawaran sudah dikonversi ke transaksi dan tidak bisa dibatalkan.")
        now = db._now()
        if q["status"] == "draft" and q["latest_version_id"]:
            raise Conflict("Batalkan draft revisi dulu, lalu batalkan penawarannya.")
        con.execute("UPDATE sales_quotations SET status='cancelled', cancel_reason=?, cancelled_by=?, cancelled_at=?, updated_by=?, updated_at=? WHERE id=?",
                    (reason[:300], user["username"], now, user["username"], now, qid))
        _event(con, qid, "cancel", f"Dibatalkan: {reason[:300]} (histori tetap tersimpan)", user["username"], q["latest_version_id"])
    return detail(qid, user)


# ── conversion to Kasir / Sales Order ────────────────────────────────────────
def check_convertible(qid: int) -> dict:
    """Before a nota / SO made from this quotation is saved: accepted, issued and not converted yet (QT-024)."""
    with db._conn() as con:
        q = _get(con, qid)
        conv = _conversion(con, qid)
        if conv:
            raise Conflict(f"Penawaran {q['quote_number']} sudah dikonversi ke {TARGETS[conv['target_type']]} {conv['target_no'] or ''}".strip() + ".")
        if q["status"] != "issued" or q["decision"] != "accepted":
            raise ValueError(f"Penawaran {q['quote_number']} belum disetujui customer; tandai Diterima Customer dulu.")
        return q


def link_conversion(qid: int, target_type: str, target_id: str, target_no: str | None, user: dict) -> None:
    with db._conn() as con:
        con.execute("BEGIN IMMEDIATE")
        q = _get(con, qid)
        conv = _conversion(con, qid)
        if conv:
            if conv["target_id"] == target_id:
                return
            raise Conflict("Penawaran sudah dikonversi ke dokumen lain.")
        con.execute("""INSERT INTO sales_quotation_conversions (quotation_id, version_id, target_type, target_id, target_no, created_by, created_at)
                       VALUES (?,?,?,?,?,?,?)""", (qid, q["latest_version_id"], target_type, target_id, target_no, user["username"], db._now()))
        _event(con, qid, "convert", f"Dikonversi ke {TARGETS[target_type]} {target_no or ''}".strip(), user["username"], q["latest_version_id"])


# ── reads ────────────────────────────────────────────────────────────────────
def detail(qid: int, user: dict | None = None) -> dict:
    with db._conn() as con:
        q = _get(con, qid)
        lines = _lines(con, qid)
        for ln in lines:
            if ln["master_item_id"]:
                mi = con.execute("SELECT sell_price, stock_qty, is_active FROM master_items WHERE id=?", (ln["master_item_id"],)).fetchone()
                ln["master_price"] = mi["sell_price"] if mi else None
                ln["stock_qty"] = mi["stock_qty"] if mi else None
                ln["master_active"] = bool(mi and mi["is_active"])
        versions = [dict(v) for v in con.execute(
            "SELECT id, revision_no, label, doc_number, total, issued_by, issued_at, superseded_at, print_count, last_printed_at "
            "FROM sales_quotation_versions WHERE quotation_id=? ORDER BY revision_no DESC", (qid,))]
        events = [dict(e) for e in con.execute("SELECT * FROM sales_quotation_events WHERE quotation_id=? ORDER BY id DESC", (qid,))]
        conv = _conversion(con, qid)
        src = con.execute("SELECT id, quote_number FROM sales_quotations WHERE id=?", (q["source_quote_id"],)).fetchone() if q["source_quote_id"] else None
        dups = [dict(r) for r in con.execute("SELECT id, quote_number FROM sales_quotations WHERE source_quote_id=? ORDER BY id", (qid,))]
        today = db.shop_today()
        q.update(lines=lines, versions=versions, events=events, conversion=conv, source=dict(src) if src else None, duplicates=dups)
        q["show_bank"] = bool(q["show_bank"])
        q["display_status"] = display_status(q, bool(conv), today)
        q["display_label"] = STATUS_LABELS[q["display_status"]]
        q["recipient_label"] = _recipient_name(q)
        q["checks"] = _checks(con, q, lines, today) if q["status"] == "draft" else []
        q["template_label"] = TEMPLATES[q["template"]]["label"]
        q["theme_label"] = THEMES[q["pdf_theme"]]
        q["issue_city"] = company.settings(con)["city"]
        if q["latest_version_id"]:
            v = con.execute("SELECT snapshot FROM sales_quotation_versions WHERE id=?", (q["latest_version_id"],)).fetchone()
            q["issued_snapshot"] = json.loads(v["snapshot"])
        return q


def list_quotes(f: dict) -> dict:
    where, args = ["1=1"], []
    if f.get("q"):
        like = f"%{f['q'].strip()}%"
        where.append("(q.quote_number LIKE ? OR q.recipient_name LIKE ? OR q.company_name LIKE ? OR q.subject LIKE ? OR q.pic_name LIKE ?)")
        args += [like] * 5
    if f.get("recipient_type") in RECIPIENT_TYPES:
        where.append("q.recipient_type=?")
        args.append(f["recipient_type"])
    if f.get("month"):
        where.append("SUBSTR(COALESCE(q.issue_date, q.created_at), 1, 7)=?")
        args.append(f["month"])
    today = db.shop_today()
    with db._conn() as con:
        rows = con.execute(f"""SELECT q.*, c.target_type, c.target_no,
                                      (SELECT COUNT(*) FROM sales_quotation_lines l WHERE l.quotation_id=q.id AND l.price_state='pending')
                                      + (CASE WHEN q.discount_state='pending' THEN 1 ELSE 0 END) AS approvals_pending
                               FROM sales_quotations q LEFT JOIN sales_quotation_conversions c ON c.quotation_id=q.id
                               WHERE {' AND '.join(where)} ORDER BY q.id DESC""", args).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["display_status"] = display_status(d, bool(d["target_type"]), today)
        if f.get("status") == "approval" and not d["approvals_pending"]:
            continue
        if f.get("status") and f["status"] != "approval" and d["display_status"] != f["status"]:
            continue
        items.append({k: d[k] for k in ("id", "quote_number", "recipient_type", "recipient_name", "company_name", "pic_name", "subject",
                                         "issue_date", "valid_until", "created_at", "created_by", "total", "status", "display_status",
                                         "revision_no", "target_type", "target_no", "approvals_pending", "follow_up_date", "officer")}
                     | {"display_label": STATUS_LABELS[d["display_status"]], "recipient_label": _recipient_name(d)})
    with db._conn() as con:
        allq = con.execute("""SELECT q.status, q.decision, q.delivery_status, q.valid_until, q.latest_version_id,
                                     (SELECT 1 FROM sales_quotation_conversions c WHERE c.quotation_id=q.id) AS conv,
                                     (SELECT COUNT(*) FROM sales_quotation_lines l WHERE l.quotation_id=q.id AND l.price_state='pending')
                                     + (CASE WHEN q.discount_state='pending' THEN 1 ELSE 0 END) AS ap
                              FROM sales_quotations q""").fetchall()
    st = [display_status(dict(r), bool(r["conv"]), today) for r in allq]
    kpi = {"total": len(allq), "draft": sum(s in ("draft", "revisi") for s in st), "open": sum(s in ("terbit", "dikirim") for s in st),
           "accepted": sum(s in ("disetujui", "dikonversi") for s in st), "approvals": sum(1 for r in allq if r["ap"])}
    return {"items": items, "kpi": kpi}


def products(q: str | None, category_id: int | None, include_inactive: bool) -> list[dict]:
    """Master Barang for the picker (mockup 04). Stock is information only: picking reserves nothing (QT-025)."""
    where, args = [], []
    if not include_inactive:
        where.append("mi.is_active=1")
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(mi.name LIKE ? OR mi.sku LIKE ? OR IFNULL(mi.manual_code,'') LIKE ? OR IFNULL(mi.barcode,'')=?)")
        args += [like, like, like, q.strip()]
    if category_id:
        where.append("(mi.category_id=? OR mi.category_id IN (SELECT id FROM categories WHERE parent_id=?))")
        args += [category_id, category_id]
    with db._conn() as con:
        rows = con.execute(f"""SELECT mi.id, mi.sku, mi.name, mi.unit, mi.sell_price, mi.stock_qty, mi.is_active, mi.item_condition,
                                      mi.manual_code, b.name AS brand_name
                               FROM master_items mi LEFT JOIN brands b ON b.id = mi.brand_id
                               {('WHERE ' + ' AND '.join(where)) if where else ''} ORDER BY mi.name LIMIT 60""", args).fetchall()
    return [dict(r) for r in rows]
