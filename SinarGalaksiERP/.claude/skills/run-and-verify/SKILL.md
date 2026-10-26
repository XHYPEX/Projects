---
name: run-and-verify
description: Run the SINAR GALAKSI POS app on a throwaway copy of a database and verify a change in a real headless browser (page errors, failing API calls, screenshots, PDFs of printed nota). Use after any frontend or backend change before calling it done, when asked to test/check/verify something in the app, or when a screenshot/PDF of a page is needed.
---

# Run and verify the app

Never test against `data/scraper.db` or the user's running server. Work on a copy, on a free port, and stop
the server when done. Port 8000 may belong to another project on this machine (WhatsAppBooking): don't kill it.

## 1. A database to test on

```bash
S=.claude/skills/run-and-verify/scripts          # from the SinarGalaksiERP folder
T=${TMPDIR:-/tmp}/sg-qa && mkdir -p $T
python3 $S/qa_db.py data/scraper.db $T/test.db   # or any older copy; the schema is migrated on the copy
```

`qa_db.py` copies the file, migrates it like the app does at startup, and makes sure there is an admin login
**qa / qa-test-1234** (override with `QA_PASSWORD`). Need specific data (an SO with an invoice, a service order
with a shipment…)? Create it through the API with that login, or edit the copy with `sqlite3`; never the original.

## 2. Start a server on the copy

```bash
(DB_PATH=$T/test.db PAYMENT_PROOF_DIR=$T/proofs PRODUCT_IMAGE_DIR=$T/images \
  python3 -m uvicorn backend.main:app --port 8769 > $T/server.log 2>&1 &) ; sleep 3
```

`frontend/index.html` is served fresh on every load, so frontend edits need no restart; backend edits do.
Stop it: `pid=$(lsof -tiTCP:8769 -sTCP:LISTEN); [ -n "$pid" ] && kill $pid`.

## 3. Look at the page

One-time setup of the browser helper: `cd $S && npm install` (installs `playwright-core`, uses the Chromium
already in `~/Library/Caches/ms-playwright`; set `CHROME_PATH` if it lives elsewhere).

```bash
node $S/page_check.mjs --route /sales-order --shot $T/so.png
node $S/page_check.mjs --route /service --eval "svcOpen(1)" --wait 1800 --shot $T/svc.png
```

It logs in as qa, opens the hash route, runs `--eval` (any JS expression; an async IIFE is awaited), and
prints **page errors** and **failing /api calls**. Exit code 1 = a page error. Then `Read` the screenshot and
actually look at it: overlapping text, clipped columns, wrong alignment are bugs even with zero errors.
For a multi-step flow write a small Playwright script next to the helper (same login/launch code).

Useful entry points for `--eval`: `openPreorder(id)`, `newPreorderForm()`, `openReceiptFromHistory(id)`,
`svcOpen(id)`, `svcOpenPrint(id, doc)`, `rtsOpen(id)`, `openSupplierPoFromActivity(id)`, `openPurchaseInvoice(id)`.
Functions that change the route may need a short wait before the next step.

## 4. Printed documents (nota)

Render exactly what the printer would get, at the real paper size, then look at the PDF:

```bash
node $S/page_check.mjs --route /service --pdf $T/terima.pdf --wait 800 \
  --eval "(async () => { await svcOpenPrint(1, 'terima'); await new Promise(r => setTimeout(r, 1500)); svcDoPrint(); })()"
cd $T && qlmanage -t -s 1600 -o . terima.pdf >/dev/null 2>&1   # → terima.pdf.png, Read it
```

`--pdf` stubs the print dialog and keeps the page rule on while saving. Print entry points: `svcDoPrint()`
(service docs, after `svcOpenPrint`), `printPreorderNota()` (after `openPreorderPrint(id)`),
`printReceiptNota(receiptId)` (Nota Penjualan). See the `nota-lx310` skill for the layout rules.

## 5. Report

Say what was actually observed: route, data used, page errors, failing calls, what the screenshot/PDF shows.
If something could not be driven (needs the real printer, an external API), say so instead of inferring.
