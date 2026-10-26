"""Mockups 06-10: metode diambil sales, review, detail + timeline, and the barang-keluar / keputusan-supplier modals."""
from shell import page, steps, doc_header
from screens_a import method_cards

RESI = "0108 2410 1977 5521"

EVENTS = [
    ("18/10 09:12", "Draft dibuat", "andi (staff)", "Sumber BM-20261012-002 · 2 barang, 3 unit, Rp 1.400.000"),
    ("18/10 09:40", "Diajukan untuk persetujuan", "andi (staff)", "Peringatan alokasi SO-20261015-0003 (RIRIN) disetujui untuk dilanjutkan"),
    ("18/10 10:05", "Disetujui untuk diproses", "sinargalaksi88 (admin)", "Stok masuk karantina: Foglamp 1, Frame 2 · draft PO kekurangan RIRIN dibuat"),
    ("18/10 16:20", "Packing selesai", "andi (staff)", "1 koli · 2,6 kg · foto sesudah dikemas diunggah"),
    ("19/10 10:15", "Barang keluar · diserahkan ke JNE REG", "andi (staff)", f"Resi {RESI} · stok fisik −3 unit, karantina −3"),
    ("21/10 14:02", "Diterima supplier", "sinargalaksi88 (admin)", "Konfirmasi WA dari Bpk. Hendra (ASTA JAYA) + foto paket"),
    ("23/10 11:30", "Keputusan supplier dicatat", "sinargalaksi88 (admin)", "RMA-AJ-1018-07 · Foglamp: ganti · Frame: 1 kredit, 1 ditolak"),
]
TODO = {
    "siap": ["Barang keluar (resi / serah terima)", "Diterima supplier", "Keputusan supplier", "Penyelesaian"],
    "diterima": ["Keputusan supplier", "Penyelesaian"],
    "keputusan": ["Terima barang pengganti (Foglamp 1)", "Catat Nota Kredit Rp 343.000", "Frame ditolak: kembali ke toko"],
}
CUT = {"siap": 4, "diterima": 6, "keputusan": 7}


def timeline(state: str) -> str:
    evs = EVENTS[:CUT[state]]
    out = []
    for i, (t, what, who, d) in enumerate(evs):
        cls = "now" if i == len(evs) - 1 else ""
        out.append(f'<div class="ev {cls}"><b>{what}</b><div class="t">{t} · {who}</div><div class="tiny" style="margin-top:2px">{d}</div></div>')
    for w in TODO[state]:
        out.append(f'<div class="ev todo"><span class="muted">{w}</span></div>')
    return f'<div class="tl">{"".join(out)}</div>'


def status_strip(state: str) -> str:
    dims = {
        "siap": [("Status dokumen", "Diproses", "p-coral", "disetujui 18/10 10:05"),
                 ("Barang keluar", "Siap Dikirim", "p-amber", "1 koli dikemas · rencana kirim 19/10"),
                 ("Keputusan supplier", "Menunggu", "p-grey", "belum dikirim"),
                 ("Penyelesaian", "Belum ada", "p-grey", "—")],
        "diterima": [("Status dokumen", "Diproses", "p-coral", "disetujui 18/10 10:05"),
                     ("Barang keluar", "Diterima Supplier", "p-green", f"JNE · {RESI} · 21/10"),
                     ("Keputusan supplier", "Menunggu", "p-amber", "2 hari sejak diterima"),
                     ("Penyelesaian", "Belum ada", "p-grey", "—")],
        "keputusan": [("Status dokumen", "Diproses", "p-coral", "disetujui 18/10 10:05"),
                      ("Barang keluar", "Diterima Supplier", "p-green", f"JNE · {RESI} · 21/10"),
                      ("Keputusan supplier", "Disetujui Sebagian", "p-amber", "RMA-AJ-1018-07 · 23/10"),
                      ("Penyelesaian", "0 dari 3 unit selesai", "p-amber", "ganti 1 · kredit 1 · ditolak 1")],
    }[state]
    cards = "".join(f'<div class="kpi"><div class="label">{a}</div><div style="margin:6px 0 3px"><span class="pill {p}">{b}</span></div><div class="d">{c}</div></div>' for a, b, p, c in dims)
    return f'<div class="grid g4">{cards}</div>'


def items_table(state: str) -> str:
    if state == "keputusan":
        rows = [
            ("AUTOVISION BI-LED FOGLAMP 3\"", "AVF3-2409-11872", "Tidak Berfungsi", "1", "Rp 750.000",
             '<span class="pill p-green">Disetujui</span>', "Ganti barang · 1", '<span class="pill p-amber">Menunggu pengganti</span><div class="bar" style="margin-top:5px;width:120px"><span style="width:0%"></span></div><div class="sub">0 / 1 diterima</div>'),
            ("FRAME HEAD UNIT AVANZA 2022 9\"", "—", "Salah Model / Tipe", "2", "Rp 650.000",
             '<span class="pill p-amber">Sebagian</span><div class="sub">1 setuju · 1 tolak</div>', "Nota kredit · 1<div class=\"sub\">Ditolak · 1: lecet bekas pasang</div>",
             '<span class="pill p-amber">Kredit belum dicatat</span><div class="sub" style="margin-top:4px">1 unit ditolak → <b>dikirim balik</b> ke toko</div>'),
        ]
    else:
        p = '<span class="pill p-grey">Menunggu</span>'
        rows = [("AUTOVISION BI-LED FOGLAMP 3\"", "AVF3-2409-11872", "Tidak Berfungsi", "1", "Rp 750.000", p, '<span class="muted">diminta: ganti</span>', '<span class="muted">—</span>'),
                ("FRAME HEAD UNIT AVANZA 2022 9\"", "—", "Salah Model / Tipe", "2", "Rp 650.000", p, '<span class="muted">diminta: ganti</span>', '<span class="muted">—</span>')]
    tr = "".join(f'<tr><td><b>{a}</b><div class="sub mono">SN {b}</div></td><td>{c}</td><td class="num">{d}</td><td class="num">{e}</td><td>{f}</td><td>{g}</td><td>{h}</td></tr>' for a, b, c, d, e, f, g, h in rows)
    ongkir = '<tr><td colspan="3" class="muted">Ongkir kirim retur (JNE) — ditagihkan ke supplier</td><td class="num">Rp 18.000</td><td><span class="pill p-green">Disetujui</span></td><td>Masuk nota kredit</td><td></td></tr>' if state == "keputusan" else ""
    return f"""<div class="tblwrap"><table class="tbl"><thead><tr><th>Barang</th><th>Alasan</th><th class="num">Qty</th><th class="num">Nilai (beli)</th><th>Keputusan supplier</th><th>Bentuk penyelesaian</th><th>Progres penyelesaian</th></tr></thead>
<tbody>{tr}{ongkir}</tbody></table></div>"""


def next_action(state: str) -> str:
    if state == "siap":
        return '<div class="note warn between"><span><b>Langkah berikutnya:</b> serahkan paket ke JNE lalu catat resi. Stok fisik baru berkurang di langkah ini.</span><span class="btn pri sm">Konfirmasi Barang Keluar</span></div>'
    if state == "diterima":
        return '<div class="note warn between"><span><b>Langkah berikutnya:</b> catat keputusan supplier per barang (setuju / sebagian / tolak) dan bentuk penyelesaiannya.</span><span class="btn pri sm">Catat Keputusan Supplier</span></div>'
    return """<div class="note warn between"><span><b>3 hal belum selesai:</b> terima 1 Foglamp pengganti · catat Nota Kredit Rp 343.000 · terima kembali 1 Frame yang ditolak.</span>
<span class="row" style="gap:6px"><span class="btn sm">Terima Kembali Barang Ditolak</span><span class="btn sm">Catat Nota Kredit / Refund</span><span class="btn pri sm">Terima Barang Pengganti</span></span></div>"""


def detail_body(state: str) -> str:
    tabs = "".join(f'<span class="btn sm {"dark" if i == 0 else "ghost"}">{t}</span>' for i, t in enumerate(
        ["Ringkasan", "Barang & Alasan", "Packing Note", "Pengiriman", "Keputusan Supplier", "Penyelesaian", "Dokumen & Foto", "Log Audit"]))
    send = (f'<dl class="kv tight"><dt>Metode</dt><dd>Dikirim · JNE REG</dd><dt>Resi</dt><dd class="mono">{RESI}</dd><dt>Diserahkan</dt><dd>19/10/2026 10:15 · andi</dd><dt>Ongkir</dt><dd>Rp 18.000 · ditagihkan ke supplier</dd><dt>Diterima</dt><dd>21/10/2026 · Bpk. Hendra</dd></dl>'
            if state != "siap" else
            '<dl class="kv tight"><dt>Metode</dt><dd>Dikirim · JNE REG</dd><dt>Rencana kirim</dt><dd>19/10/2026</dd><dt>Tujuan</dt><dd>ASTA JAYA — Gudang Retur, Jakarta Utara</dd><dt>Paket</dt><dd>1 koli · 2,6 kg · foto sesudah kemas ✓</dd><dt>Resi</dt><dd class="muted">belum ada</dd></dl>')
    return f"""<div class="between"><div class="row" style="align-items:center;gap:10px">
  <span class="btn sm">← Daftar Retur</span><h2 class="serif" style="margin:0;font-size:18px">RTS-20261018-001</h2>
  <span class="tag">ASTA JAYA</span><span class="tag">Retur barang bermasalah</span><span class="tag">dibuat 18/10/2026 · andi</span></div>
  <div class="row" style="gap:6px"><span class="btn sm">Cetak ▾</span><span class="btn sm">Tambah catatan</span><span class="btn sm danger">{"Batalkan Retur" if state == "siap" else "Minta Barang Kembali"}</span></div></div>
{status_strip(state)}
{next_action(state)}
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:12px">
  <div class="row" style="gap:4px">{tabs}</div>
  {items_table(state)}
  <div class="grid g3">
   <div class="card"><h3>Pengiriman</h3>{send}</div>
   <div class="card"><h3>Dokumen terkait</h3><dl class="kv tight"><dt>PO Supplier</dt><dd class="mono">PO-SUP-20261010-001</dd><dt>Barang Masuk</dt><dd class="mono">BM-20261012-002</dd><dt>Faktur supplier</dt><dd class="mono">FP-2026-0041 · sisa Rp 3.500.000</dd><dt>Sales Order</dt><dd class="mono">SO-20261015-0003 · RIRIN <span class="pill p-amber">kurang 1</span></dd><dt>PO kekurangan</dt><dd class="mono">draft · Foglamp 1</dd></dl></div>
   <div class="card"><h3>Nilai retur</h3><dl class="kv tight"><dt>Barang</dt><dd>Rp 1.400.000</dd><dt>Ongkir diklaim</dt><dd>Rp 18.000</dd><dt>Disetujui</dt><dd>{"Rp 1.093.000" if state == "keputusan" else '<span class="muted">—</span>'}</dd><dt>Sudah diselesaikan</dt><dd>Rp 0</dd><dt>Sisa klaim</dt><dd class="strong">{"Rp 1.093.000" if state == "keputusan" else '<span class="muted">—</span>'}</dd></dl>
    <div class="hint">Disetujui = ganti 750.000 + kredit 325.000 + ongkir 18.000.</div></div>
  </div>
 </div>
 <div class="card" style="width:330px"><h3>Riwayat <small>tidak bisa diubah</small></h3>{timeline(state)}</div>
</div>"""


def modal_page(name, title, subtitle, state, modal_html, width, top=70):
    left = (1440 - width) // 2
    html = page(title, subtitle, detail_body(state),
                extra=f'<div class="modal" style="left:{left}px;top:{top}px;width:{width}px">{modal_html}</div>')
    return name, html.replace('<div class="shell">', '<div class="shell overlay">', 1)


def s06():
    body = f"""{doc_header("RTS-20261017-001", "Draft", "p-grey").replace("ASTA JAYA", "MITRA LAMPU JAYA").replace("PO-SUP-20261010-001 · BM-20261012-002", "PO-SUP-20261003-002 · BM-20261005-001")}
{steps(4)}
{method_cards("sales")}
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Rencana pengambilan <small>diisi sekarang</small></h3>
   <div class="grid g4">
    <div class="field span2"><label>Nama sales supplier <span class="req">*</span></label><div class="input">Bpk. Rudi Hartono</div></div>
    <div class="field"><label>No. HP sales <span class="req">*</span></label><div class="input">0813 1122 4455</div></div>
    <div class="field"><label>Perusahaan</label><div class="input ro">MITRA LAMPU JAYA</div></div>
    <div class="field"><label>Jadwal ambil <span class="req">*</span></label><div class="input">19/10/2026</div></div>
    <div class="field"><label>Perkiraan jam</label><div class="input">13:00 – 15:00</div></div>
    <div class="field span2"><label>Dikonfirmasi oleh supplier lewat</label><div class="input sel">WhatsApp kantor supplier · 17/10 15:22</div><div class="hint">Konfirmasi ke kantor (bukan hanya ke sales) mencegah barang diserahkan ke orang yang salah.</div></div>
    <div class="field span4"><label>Catatan untuk sales</label><div class="input">Bawa surat tugas / kartu nama. Headlamp dikemas 1 koli besar, mohon bawa mobil.</div></div>
   </div></div>
  <div class="card"><h3>Serah terima <small>diisi saat sales datang — lewat tombol "Catat Serah Terima Sales"</small></h3>
   <div class="grid g4" style="opacity:.62">
    <div class="field"><label>Tanggal &amp; jam diambil</label><div class="input ro">—</div></div>
    <div class="field"><label>Nama pengambil (sesuai identitas)</label><div class="input ro">—</div></div>
    <div class="field"><label>No. identitas / ID sales</label><div class="input ro">opsional</div></div>
    <div class="field"><label>Petugas toko</label><div class="input ro">—</div></div>
    <div class="field span2"><label>Tanda tangan sales + petugas</label><div class="input ro" style="height:58px">di kertas Bukti Serah Terima (LX-310) atau di layar</div></div>
    <div class="field span2"><label>Foto serah terima <span class="req">*</span></label><div class="input ro" style="height:58px">foto sales dengan paket / Bukti Serah Terima bertanda tangan</div></div>
   </div></div>
 </div>
 <div style="width:360px;display:flex;flex-direction:column;gap:12px">
  <div class="card soft"><h3>Status setelah retur disetujui</h3><div class="tl">
   <div class="ev now"><b>Menunggu Pengambilan</b><div class="t">paket di rak karantina</div></div>
   <div class="ev todo"><b>Sudah Diserahkan ke Sales</b><div class="t">Bukti Serah Terima ditandatangani · stok fisik berkurang</div></div>
   <div class="ev todo"><b>Diterima Supplier</b><div class="t">sales sudah menyetor ke kantor/gudang supplier</div></div></div></div>
  <div class="note ok"><b>Tidak perlu resi.</b> Bukti sahnya: Bukti Serah Terima bernomor (BST-RTS-20261017-001) dengan tanda tangan kedua pihak + foto.</div>
  <div class="note warn"><b>Sales tidak datang?</b> Status tetap "Menunggu Pengambilan", muncul tanda <i>lewat jadwal</i> keesokan harinya. Admin bisa ubah jadwal atau ganti ke metode kirim. Stok tidak berubah.</div>
 </div>
</div>
<div class="between"><span class="btn">← Packing Note</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Review →</span></div></div>"""
    return "06_Retur_Diambil_Sales", page("Buat Retur Supplier", "Langkah 4 dari 5 — metode B: barang diambil langsung oleh sales supplier di toko.", body)


def s07():
    checks = [("ok", "Semua barang punya alasan; Tidak Berfungsi punya hasil uji"), ("ok", "Qty retur ≤ qty yang bisa diretur di BM-20261012-002"),
              ("ok", "Foto bukti ada untuk barang Rusak / Tidak Berfungsi"), ("ok", "Semua unit masuk tepat satu koli (3 / 3)"),
              ("ok", "Alamat tujuan &amp; penerima lengkap"),
              ("warn", "Foglamp dicadangkan untuk SO-20261015-0003 (RIRIN) — kekurangan 1 unit akan ditandai"),
              ("warn", "Foto sesudah dikemas belum ada — wajib sebelum Konfirmasi Barang Keluar")]
    ch = "".join(f'<div class="row" style="gap:8px;align-items:flex-start"><span class="pill no {"p-green" if k == "ok" else "p-amber"}" style="min-width:22px;justify-content:center">{"✓" if k == "ok" else "!"}</span><span class="small">{t}</span></div>' for k, t in checks)
    body = f"""{doc_header()}
{steps(5)}
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="grid g3">
   <div class="card"><h3>Informasi</h3><dl class="kv tight"><dt>Supplier</dt><dd>ASTA JAYA</dd><dt>Sumber</dt><dd class="mono">BM-20261012-002</dd><dt>PO</dt><dd class="mono">PO-SUP-20261010-001</dd><dt>Jenis</dt><dd>Retur barang bermasalah</dd><dt>Diminta</dt><dd>Ganti barang</dd></dl></div>
   <div class="card"><h3>Pengemasan</h3><dl class="kv tight"><dt>Koli</dt><dd>1 · 2,6 kg · 40×30×22</dd><dt>Bahan</dt><dd>dus original, bubble wrap, foam, kardus tebal, kantong aksesori</dd><dt>Foto</dt><dd>2 sebelum · 0 sesudah</dd></dl></div>
   <div class="card"><h3>Pengembalian</h3><dl class="kv tight"><dt>Metode</dt><dd>Dikirim · JNE REG</dd><dt>Rencana</dt><dd>19/10/2026</dd><dt>Tujuan</dt><dd>Gudang Retur ASTA JAYA, Jakarta Utara</dd><dt>Ongkir</dt><dd>± Rp 18.000 · ditagihkan ke supplier</dd></dl></div>
  </div>
  <div class="tblwrap"><table class="tbl"><thead><tr><th>Barang</th><th>Alasan</th><th>Kondisi</th><th>Bukti</th><th class="num">Qty</th><th class="num">Harga beli</th><th class="num">Nilai</th></tr></thead><tbody>
   <tr><td><b>AUTOVISION BI-LED FOGLAMP 3"</b><div class="sub mono">SN AVF3-2409-11872</div></td><td>Tidak Berfungsi<div class="sub">lampu kiri tidak menyala, kabel &amp; soket sudah diganti</div></td><td>Tidak berfungsi</td><td>2 foto · 1 video</td><td class="num">1</td><td class="num">Rp 750.000</td><td class="num">Rp 750.000</td></tr>
   <tr><td><b>FRAME HEAD UNIT AVANZA 2022 9"</b></td><td>Salah Model / Tipe<div class="sub">datang tipe 2019 7"</div></td><td>Baik / belum dipakai</td><td>1 foto</td><td class="num">2</td><td class="num">Rp 325.000</td><td class="num">Rp 650.000</td></tr>
   <tr><td colspan="6" class="strong">Total nilai retur (harga beli)</td><td class="num strong serif" style="font-size:15px">Rp 1.400.000</td></tr></tbody></table></div>
  <div class="card soft"><h3>Yang terjadi setelah tombol ditekan</h3><div class="grid g3 small">
   <div><b>Ajukan (staff)</b><div class="muted">Status → Menunggu Persetujuan. Stok &amp; tagihan belum berubah. Admin mendapat notifikasi.</div></div>
   <div><b>Setujui (admin)</b><div class="muted">Status → Diproses. 3 unit masuk <b>Stok Karantina</b>: tidak bisa dijual di Kasir, stok fisik tetap. SO RIRIN ditandai kurang 1.</div></div>
   <div><b>Belum ada efek keuangan</b><div class="muted">Faktur FP-2026-0041 tidak berubah. Nota kredit / refund baru dicatat setelah supplier memutuskan.</div></div></div></div>
 </div>
 <div style="width:380px;display:flex;flex-direction:column;gap:12px">
  <div class="card"><h3>Pemeriksaan</h3><div style="display:flex;flex-direction:column;gap:7px">{ch}</div></div>
  <div class="card"><h3>Dampak stok saat disetujui</h3><table class="tbl"><thead><tr><th>Barang</th><th class="num">Fisik</th><th class="num">Karantina</th><th class="num">Bisa dijual</th></tr></thead><tbody>
   <tr><td>Foglamp 3"</td><td class="num">2 → 2</td><td class="num">0 → 1</td><td class="num">0 → <b style="color:var(--failed)">−1</b></td></tr>
   <tr><td>Frame Avanza 22</td><td class="num">3 → 3</td><td class="num">0 → 2</td><td class="num">3 → 1</td></tr></tbody></table>
   <div class="hint">Bisa dijual = Fisik − Dicadangkan SO − Karantina. Minus = kekurangan pesanan, bukan stok minus.</div></div>
  <div class="field"><label>Catatan untuk penyetuju</label><div class="input area">Foglamp RIRIN: sudah tanya ASTA JAYA, pengganti ± 5 hari kerja.</div></div>
  <div class="row" style="gap:8px;justify-content:flex-end"><span class="btn">← Metode</span><span class="btn">Simpan Draft</span><span class="btn pri">Ajukan untuk Persetujuan</span></div>
  <div class="hint" style="text-align:right">Admin melihat tombol <b>Setujui &amp; Proses</b> dan <b>Kembalikan ke Draft</b> di tempat ini.</div>
 </div>
</div>"""
    return "07_Review_Konfirmasi_Retur", page("Buat Retur Supplier", "Langkah 5 dari 5 — periksa semuanya, lalu ajukan. Belum ada stok atau tagihan yang berubah.", body)


def s08():
    return "08_Detail_Retur_Timeline", page("Detail Retur Supplier", "Satu halaman untuk seluruh perjalanan retur: barang keluar, keputusan supplier, dan penyelesaian.", detail_body("keputusan"))


def s09():
    m = f"""<div class="mh"><div><h2>Konfirmasi Barang Keluar</h2><div class="tiny muted">RTS-20261018-001 · Dikirim lewat ekspedisi · 1 koli</div></div><span class="btn sm ghost">✕</span></div>
<div class="mb">
 <div class="grid g3">
  <div class="field"><label>Ekspedisi <span class="req">*</span></label><div class="input sel">JNE · REG</div></div>
  <div class="field"><label>No. resi / AWB <span class="req">*</span></label><div class="input focus mono">{RESI}</div><div class="hint">Dicek: resi ini belum dipakai di retur lain.</div></div>
  <div class="field"><label>Diserahkan ke ekspedisi <span class="req">*</span></label><div class="input">19/10/2026 · 10:15</div></div>
  <div class="field"><label>Ongkir aktual</label><div class="input">Rp 18.000</div></div>
  <div class="field"><label>Ditanggung</label><div class="input ro">Supplier (ditagihkan)</div></div>
  <div class="field"><label>Petugas yang menyerahkan</label><div class="input ro">andi</div></div>
  <div class="field"><label>Foto resi / bukti kirim <span class="req">*</span></label><div class="photos"><div class="photo">resi</div><div class="photo add">+</div></div></div>
  <div class="field"><label>Foto paket sesudah dikemas <span class="req">*</span></label><div class="photos"><div class="photo">koli 1</div><div class="photo">label</div></div></div>
  <div class="field"><label>Jumlah koli diserahkan</label><div class="input">1 <span class="muted">/ 1 di packing note</span></div></div>
 </div>
 <div class="card soft" style="padding:11px 13px"><div class="label" style="margin-bottom:6px">Stok yang akan keluar (sekali saja)</div>
  <table class="tbl"><thead><tr><th>Barang</th><th class="num">Qty</th><th class="num">Fisik</th><th class="num">Karantina</th><th>Ledger</th></tr></thead><tbody>
  <tr><td>AUTOVISION BI-LED FOGLAMP 3"</td><td class="num">1</td><td class="num">2 → 1</td><td class="num">1 → 0</td><td class="mono tiny">OUT · supplier_return · RTS-20261018-001</td></tr>
  <tr><td>FRAME HEAD UNIT AVANZA 2022 9"</td><td class="num">2</td><td class="num">3 → 1</td><td class="num">2 → 0</td><td class="mono tiny">OUT · supplier_return · RTS-20261018-001</td></tr></tbody></table></div>
 <span class="chk on"><i></i>Saya sudah menyerahkan 1 koli ke JNE dan isi paket sesuai Packing Note.</span>
 <div class="note info">Setelah disimpan: status barang keluar → <b>Dalam Pengiriman</b> (belum berarti diterima supplier). Tombol ini terkunci setelah ditekan; menekan dua kali / koneksi putus tidak mengurangi stok dua kali.</div>
</div>
<div class="mf"><span class="btn">Batal</span><span class="btn pri">Konfirmasi Barang Keluar</span></div>"""
    return modal_page("09_Konfirmasi_Barang_Keluar", "Detail Retur Supplier", "Satu halaman untuk seluruh perjalanan retur.", "siap", m, 780, 60)


def s10():
    m = """<div class="mh"><div><h2>Catat Keputusan Supplier</h2><div class="tiny muted">RTS-20261018-001 · diterima supplier 21/10/2026</div></div><span class="btn sm ghost">✕</span></div>
<div class="mb">
 <div class="grid g4">
  <div class="field"><label>Tanggal keputusan <span class="req">*</span></label><div class="input">23/10/2026</div></div>
  <div class="field"><label>Disampaikan lewat</label><div class="input sel">Email + surat RMA</div></div>
  <div class="field"><label>No. RMA / surat supplier</label><div class="input mono">RMA-AJ-1018-07</div></div>
  <div class="field"><label>Lampiran</label><div class="photos"><div class="photo">PDF</div><div class="photo add">+</div></div></div>
 </div>
 <table class="tbl"><thead><tr><th>Barang</th><th class="num">Dikirim</th><th>Keputusan</th><th class="num" style="width:70px">Disetujui</th><th class="num" style="width:62px">Ditolak</th><th style="width:150px">Penyelesaian (yang disetujui)</th><th style="width:190px">Alasan tolak &amp; nasib barang</th></tr></thead><tbody>
  <tr><td>AUTOVISION BI-LED FOGLAMP 3"</td><td class="num">1</td><td><span class="pill p-green">Disetujui</span></td><td><div class="input" style="justify-content:flex-end">1</div></td><td class="num muted">0</td><td><div class="input sel">Ganti barang</div></td><td class="muted small">—</td></tr>
  <tr class="hl"><td>FRAME HEAD UNIT AVANZA 2022 9"</td><td class="num">2</td><td><span class="pill p-amber">Sebagian</span></td><td><div class="input" style="justify-content:flex-end">1</div></td><td class="num">1</td><td><div class="input sel">Nota kredit</div></td><td><div class="input" style="font-size:11.5px">Lecet bekas pasang</div><div class="input sel" style="margin-top:4px;font-size:11.5px">Dikirim balik ke toko</div></td></tr>
  <tr><td class="muted">Ongkir retur (klaim)</td><td class="num">—</td><td><span class="pill p-green">Disetujui</span></td><td class="num">Rp 18.000</td><td></td><td><div class="input sel">Nota kredit</div></td><td></td></tr>
 </tbody></table>
 <div class="grid g3">
  <div class="card soft" style="padding:10px 12px"><div class="label">Disetujui</div><div class="serif strong" style="font-size:17px">Rp 1.093.000</div><div class="tiny muted">ganti 750.000 · kredit 343.000</div></div>
  <div class="card soft" style="padding:10px 12px"><div class="label">Ditolak</div><div class="serif strong" style="font-size:17px">Rp 325.000</div><div class="tiny muted">1 frame · kembali ke toko</div></div>
  <div class="card soft" style="padding:10px 12px"><div class="label">Belum diputuskan</div><div class="serif strong" style="font-size:17px">0 unit</div><div class="tiny muted">boleh disimpan bertahap</div></div>
 </div>
 <div class="note warn">Barang ditolak yang dikirim balik masuk lagi ke <b>Stok Karantina</b> saat diterima (bukan langsung bisa dijual). Admin lalu pilih: jual sebagai barang second, hapus buku, atau ajukan ulang.</div>
 <div class="note info">Belum ada uang atau stok yang berubah di langkah ini. Nota kredit dan barang pengganti dicatat saat benar-benar terjadi.</div>
</div>
<div class="mf"><span class="btn">Batal</span><span class="btn pri">Simpan Keputusan</span></div>"""
    return modal_page("10_Konfirmasi_Supplier", "Detail Retur Supplier", "Satu halaman untuk seluruh perjalanan retur.", "diterima", m, 960, 50)


SCREENS = [s06, s07, s08, s09, s10]
