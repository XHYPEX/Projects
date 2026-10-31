# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Sinar Galaksi ERP**: a small ERP (POS, inventory, purchasing, payables, service) for **SINAR GALAKSI VARIASI MOBIL**
(car accessories and installation shop, Balikpapan). The folder was called `WebScrapePlastik` until 26-10-2026; the
shop PCs still keep their data in `%LOCALAPPDATA%\TokoPlastik` and the install in `%USERPROFILE%\TokoPlastik`.
Modules: Kasir (nota penjualan, pembayaran, pengerjaan), Sales Order, Purchase Order, Barang Masuk / Goods Receipt,
Account Payable (Faktur Pembelian, Aging, Debit Note), Retur Supplier, Stok Karantina, Service / Garansi,
Penawaran Harga, Cash Receipt / Kas Keluar, Insentif, dashboards, master data.
The UI is in Indonesian. The original Google Maps scraper (`main.py`, `scraper.py`, `mapper.py`) is legacy and
disabled (`SCRAPER_ENABLED=false`).

## Commands

```bash
pip install -r requirements.txt
python run.py                          # serves backend + frontend on :8000 (HOST/PORT env override)
docker compose up --build              # production-style; ./data is a volume, so the database survives rebuilds
# Throwaway run against a copy of a database (never touch data/scraper.db while testing):
DB_PATH=/tmp/x.db PAYMENT_PROOF_DIR=/tmp/proofs python -m uvicorn backend.main:app --port 8769
```

Windows shop PCs use `Start.bat` (see `HOW_TO_RUN.md`). Env: `DB_PATH`, `PAYMENT_PROOF_DIR`, `PRODUCT_IMAGE_DIR`,
`SHOP_UTC_OFFSET_MINUTES`, `SESSION_COOKIE_SECURE`, `SCRAPER_ENABLED`.

There is no automated test suite. Verify changes with the project skill **`run-and-verify`** (throwaway
database copy with a `qa` login, server on a spare port, headless-browser check of page errors / failing API
calls, screenshots, PDFs of printed nota). Printed documents follow the **`nota-lx310`** skill. The `qa-tester`
agent uses both.

## Architecture

| Path | Role |
|---|---|
| `backend/main.py` | FastAPI app, mounts every router under `/api`, serves `frontend/` |
| `backend/database.py` | All SQL (stdlib `sqlite3`, no ORM) and most business rules; schema created/migrated additively at startup |
| `backend/schemas.py` | Pydantic request/response models |
| `backend/routes/*.py` | Thin HTTP layer per module (validation, auth, `run_in_executor` around database calls) |
| `backend/order_dashboards.py` | Dashboard SO / PO and the Ringkasan overview (`/api/dashboards/*`) |
| `backend/goods_receipt.py`, `credit_notes.py`, `account_payable.py`, `cash_receipts.py`, `incentives.py`, `quotations.py`, `warehouses.py` | Larger modules kept out of `database.py` (`credit_notes` = **Debit Note** in the UI; `warehouses` = **Master Gudang**: Gudang → Rak list only, stock stays one number per item) |
| `frontend/index.html` | The whole SPA in one file: Tailwind CDN config, `<style>`, markup for every `page-*`, and all JS |
| `docs/` | Owner-approved designs (service, retur supplier, flowcharts, LX-310 print test) |

Frontend routing is hash based (`#/sales-order` etc.): `ROUTES`, `ROUTE_META` and the route handler near the top
of the script decide which `page-*` div shows and which init function runs.

## Conventions

- **Uppercase input**: every typed field is uppercased by a global handler; add `data-keep-case` to opt out
  (`data-no-uppercase` is legacy and does nothing).
- **Money** is integer rupiah; dates are shown on the shop's clock (`SHOP_UTC_OFFSET_MINUTES`).
- `esc()` escapes text nodes only, not quotes: never put user text inside an HTML attribute.
- **Migrations are additive** (`ALTER TABLE … ADD COLUMN` in the init lists); never rename or drop columns.
- **Payments** have a state: `sah` (counted) or `pending` (transfer/QRIS not yet confirmed, never counted).
- **Sales Order line sources**: `supplier` (bought whole), `stok` (held from the shelf), `campur` = "Stok + Pesan
  Supplier" (free stock held, only the shortfall drafted to a PO), `jasa`, or null (automatic). A published SO
  may not have goods at Rp 0; drafts may.
- **Printed documents** (Tanda Terima / Surat Jalan / Nota Hasil Service, Invoice SO, Nota Penjualan) share
  `docHeadHtml` and the `DOC_GAP` spacing scale. They print on an Epson LX-310 dot matrix on continuous,
  3-ply carbonless forms (white: shop archive, yellow: supplier, red: customer): black only, solid rules, no
  grey fills, printed once. Paper is 9.5" × 5.5" (half) by default, 9.5" × 11" optional.
- Global JS names must stay unique: the script is one scope, so a duplicate `const` breaks the whole app.
  Syntax-check `frontend/index.html` after larger edits.
- Owner decisions behind business rules are noted in comments with a date (e.g. "Owner 25-10-2026"); keep them.

## Git

The repository root is the parent folder (`/Users/0xd0kk/Projects`, several projects). Commit only
`SinarGalaksiERP/` files, never `SinarGalaksiERP/data/` (live database, uploads, payment proofs).

## Theme Style
"Navy Bengkel" (owner's choice, 24-10-2026), replacing the earlier cream/coral look:
--sidebar:     #0F1B2D;  /* navy sidebar, light text (#F1F5F9 / muted #94A3B8) */
--bg:          #F6F7F9;  /* light grey canvas */
--bg-subtle:   #EEF0F3;  /* table heads, chips, read-only fields */
--card:        #FFFFFF;
--text:        #111827;
--text-muted:  #6B7280;
--accent:      #EA580C;  /* orange: primary actions (Pembayaran, Pesanan Baru, Simpan) */
--accent-hover:#C2410C;
--border:      #E3E6EB;
Typography: Inter for everything (headings included); IBM Plex Mono for codes/numbers.
Tokens live in the Tailwind config at the top of frontend/index.html (bg, bg2, ink, muted, accent, accentDark, line);
the navy sidebar is the `#drawer-panel` block in the <style>. Kasir buttons keep colour-by-role: accent = transaction,
blue #1D4ED8 = operations/status, green #15803D = print, red #DC2626 = void/delete, slate #334155 = standard.
Feel: soft rounded corners (~8–12px), subtle borders instead of heavy shadows, calm/minimal motion.
The Kasir page carries the approved visual style "03 Balanced" (block "Kasir visual 03" in the <style>), scoped to
Kasir only until the owner approves it for the other pages.
