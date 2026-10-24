"""Pengaturan Perusahaan: the shop's official identity and bank accounts (Blueprint Penawaran Harga, 23-10-2026).

Documents printed for customers (Surat Penawaran Harga) take the shop's name, city, address, contact and signer from
here instead of hard-coded text, and may show only a bank account an Admin entered here and left active. The letter
editor can pick an account but never type or change its number. Identity lives in app_settings ("company.*");
accounts are rows that are deactivated, never deleted, so an issued letter's snapshot still names a real row.
"""
from __future__ import annotations

import sqlite3

from backend import database as db

FIELDS = {
    "name": ("Nama toko", "SINAR GALAKSI VARIASI MOBIL", 120),
    "tagline": ("Bidang usaha", "Aksesori & Modifikasi Mobil", 120),
    "city": ("Kota surat", "Balikpapan", 60),
    "region": ("Provinsi", "Kalimantan Timur", 60),
    "address": ("Alamat", "", 300),
    "phone": ("Telepon / WhatsApp", "", 60),
    "email": ("Email", "", 120),
    "signer_name": ("Penanda tangan", "Tim Penjualan", 80),
    "signer_title": ("Jabatan penanda tangan", "", 80),
    "quote_valid_days": ("Masa berlaku penawaran (hari)", "7", 3),
}


def migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS company_bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            account_name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT, created_at TEXT NOT NULL,
            updated_by TEXT, updated_at TEXT
        );
    """)


def settings(con: sqlite3.Connection | None = None) -> dict:
    out = {k: (db.get_setting(f"company.{k}", None, con) or default) for k, (_, default, _) in FIELDS.items()}
    try:
        out["quote_valid_days"] = max(1, min(365, int(out["quote_valid_days"])))
    except (TypeError, ValueError):
        out["quote_valid_days"] = 7
    return out


def save_settings(data: dict, user: dict) -> dict:
    clean = {}
    for k, (label, _, limit) in FIELDS.items():
        if k not in data:
            continue
        v = str(data.get(k) or "").strip()
        if len(v) > limit:
            raise ValueError(f"{label} terlalu panjang (maks. {limit} karakter).")
        if k in ("name", "city", "signer_name") and not v:
            raise ValueError(f"{label} wajib diisi.")
        if k == "quote_valid_days":
            if not v.isdigit() or not 1 <= int(v) <= 365:
                raise ValueError("Masa berlaku penawaran harus 1–365 hari.")
        clean[k] = v
    for k, v in clean.items():
        db.set_setting(f"company.{k}", v, user["username"])
    return settings()


def _account_out(r) -> dict:
    d = dict(r)
    d["is_active"] = bool(d["is_active"])
    d["label"] = f"{d['bank_name']} {d['account_number']} a.n. {d['account_name']}"
    return d


def list_accounts(include_inactive: bool = False) -> list[dict]:
    with db._conn() as con:
        rows = con.execute("SELECT * FROM company_bank_accounts" + ("" if include_inactive else " WHERE is_active=1")
                           + " ORDER BY is_active DESC, bank_name, id").fetchall()
    return [_account_out(r) for r in rows]


def active_account(con: sqlite3.Connection, account_id) -> dict | None:
    if not account_id:
        return None
    r = con.execute("SELECT * FROM company_bank_accounts WHERE id=? AND is_active=1", (account_id,)).fetchone()
    return _account_out(r) if r else None


def _clean_account(data: dict) -> tuple[str, str, str]:
    bank = str(data.get("bank_name") or "").strip()
    number = str(data.get("account_number") or "").replace(" ", "").replace("-", "").strip()
    name = str(data.get("account_name") or "").strip()
    if not bank or len(bank) > 40:
        raise ValueError("Nama bank wajib diisi (maks. 40 karakter).")
    if not number.isdigit() or not 5 <= len(number) <= 20:
        raise ValueError("Nomor rekening harus 5–20 angka.")
    if not name or len(name) > 80:
        raise ValueError("Nama pemilik rekening wajib diisi (maks. 80 karakter).")
    return bank, number, name


def save_account(account_id: int | None, data: dict, user: dict) -> dict:
    bank, number, name = _clean_account(data)
    active = 1 if data.get("is_active", True) else 0
    now = db._now()
    with db._conn() as con:
        dup = con.execute("SELECT id FROM company_bank_accounts WHERE bank_name=? AND account_number=? AND id<>?",
                          (bank, number, account_id or 0)).fetchone()
        if dup:
            raise ValueError("Rekening ini sudah terdaftar.")
        if account_id:
            if not con.execute("SELECT 1 FROM company_bank_accounts WHERE id=?", (account_id,)).fetchone():
                raise LookupError("Rekening tidak ditemukan.")
            con.execute("""UPDATE company_bank_accounts SET bank_name=?, account_number=?, account_name=?, is_active=?,
                           updated_by=?, updated_at=? WHERE id=?""", (bank, number, name, active, user["username"], now, account_id))
        else:
            account_id = con.execute("""INSERT INTO company_bank_accounts (bank_name, account_number, account_name, is_active,
                                        created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                                     (bank, number, name, active, user["username"], now)).lastrowid
        return _account_out(con.execute("SELECT * FROM company_bank_accounts WHERE id=?", (account_id,)).fetchone())
