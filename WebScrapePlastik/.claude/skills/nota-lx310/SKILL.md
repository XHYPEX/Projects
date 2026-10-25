---
name: nota-lx310
description: Rules and building blocks for the printed documents of the SINAR GALAKSI POS app (Tanda Terima / Surat Jalan / Nota Hasil Service, Invoice Sales Order, Nota Penjualan Kasir) on the Epson LX-310 dot matrix with 3-ply carbonless continuous forms. Use when creating or changing any nota, invoice, receipt or other printable document, or when the owner reports a print looking wrong.
---

# Printed documents (nota) for the LX-310

## The paper and the printer

- Epson LX-310 dot matrix, continuous form, **3-ply carbonless**: white = arsip toko, yellow = supplier,
  red = customer. One print gives all three copies, so a document is printed **once** and names the copies
  in its footer (`svcLegend`).
- Paper: **half form 9.5" × 5.5"** (default for service docs and the Kasir nota), full 9.5" × 11" optional.
  Content room on the half form ≈ 480 px tall at 96 dpi with 0.25"/0.3" page margins.
- Dot matrix rules: **black only**, solid rules ≥ 1px (1.5–2px for structure), **no grey fills or tints**
  (they print as bands), Arial/Helvetica, body 9–9.5pt, smallest text 7.5pt. Logo is printed as a black
  silhouette (`filter:brightness(0)`) of `frontend/assets/logo-nota.png` (transparent PNG). A logo on a black
  background would print as a solid block.
- Keep text off the paper edge: the tractor strips and the printer's dead zone cut it. The Kasir nota adds a
  3 mm inner margin on top of the page margin from "Atur Cetak".

## Building blocks (frontend/index.html, section "Printed documents: one letterhead and one spacing scale")

| Helper | Use |
|---|---|
| `docHeadHtml(title, lines)` | The one letterhead: logo, shop name / address / WA (`docShop()`, from Pengaturan Perusahaan with letterhead fallbacks), boxed title with number, date, status |
| `DOC_GAP` | Spacing scale {xs 4, sm 8, md 12, lg 16}: 12 between blocks, 4 from a section label to its content |
| `docKv`, `docKvTable`, `docCols` | Label : value rows with one label width per document; two equal columns sharing the page edges |
| `docSum`, `docSumRule` | Totals (label, Rp, right-aligned amount, rule before the total) |
| `svcSec`, `svcFacts`, `svcItemTable`, `svcNote`, `svcSigns`, `svcLegend`, `svcSheet` | Section label, fact rows, ruled item table, rule-topped note, fixed-width signature boxes on both edges, copy legend, sheet wrapper |

Documents: `svcTerimaHtml`, `svcSuratJalanHtml`, `svcHasilHtml` (service; the backend `/api/services/{id}/print/{doc}`
returns `facts` for surat_jalan / hasil), `poInvoiceHtml` (Invoice SO, paper from `PO_PAPERS`),
`receiptNotaPageHtml` (Nota Penjualan, paper/margins/zoom from `notaPrintPrefs`, page split by item count).

## When changing or adding a document

1. Reuse the helpers; don't hand-roll a header, signature or totals block. New global names must be unique.
2. Show only what the reader needs (owner: the Tanda Terima carries service facts, no warranty explanations).
3. Check it fits: service preview warns when content is taller than the half form; keep Tanda Terima and
   Surat Jalan within it.
4. Render the PDF at the real size and look at it (see the `run-and-verify` skill, step 4). Check: same
   head as the other documents, colons aligned, shared left/right edges, nothing on a rule, signature room,
   no grey.
5. The real paper can't be verified from software: tell the owner to print one sample on the LX-310
   (`docs/nota/UJI_CETAK_LX310.md` has the test-sheet procedure for the Kasir nota).
