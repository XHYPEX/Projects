"""Mockups 01-05: daftar retur, buat retur (informasi), barang & alasan, packing note, metode kirim."""
from shell import page, steps, doc_header


def s01():
    kpis = [("Draft", "2", "belum diajukan"), ("Perlu Dikirim / Diambil", "3", "1 lewat jadwal ambil"),
            ("Dalam Pengiriman", "2", "di ekspedisi"), ("Menunggu Keputusan Supplier", "4", "2 lebih dari 7 hari"),
            ("Penyelesaian Berjalan", "3", "pengganti / refund / kredit"), ("Bermasalah", "1", "paket hilang")]
    k = "".join(f'<div class="kpi"><div class="label">{a}</div><div class="v">{b}</div><div class="d">{c}</div></div>' for a, b, c in kpis)
    k = k.replace('<div class="v">1</div><div class="d">paket hilang', '<div class="v" style="color:var(--failed)">1</div><div class="d">paket hilang')
    rows = [
        ("RTS-20261018-001", "18/10/2026", "ASTA JAYA", "PO-SUP-20261010-001", "BM-20261012-002", "AUTOVISION BI-LED FOGLAMP 3\"", "+1 barang", "3", "Rp 1.400.000",
         ("Kirim", "p-blue"), ("Draft", "p-grey"), "Belum siap", ("—", "muted"), ("—", "muted")),
        ("RTS-20261017-001", "17/10/2026", "MITRA LAMPU JAYA", "PO-SUP-20261003-002", "BM-20261005-001", "HEADLAMP ASSY INNOVA REBORN", "", "1", "Rp 2.150.000",
         ("Diambil Sales", "p-teal"), ("Aktif", "p-coral"), "Menunggu pengambilan · jadwal 19/10", ("Menunggu", "p-amber"), ("Belum", "p-grey")),
        ("RTS-20261015-001", "15/10/2026", "CAHAYA AUDIO", "PO-SUP-20260928-004", "BM-20261001-003", "HEAD UNIT ANDROID 9\" 4/64", "+2 barang", "3", "Rp 4.950.000",
         ("Kirim", "p-blue"), ("Aktif", "p-coral"), "Dalam pengiriman · JNE 0123 4567 8890", ("Menunggu", "p-amber"), ("Belum", "p-grey")),
        ("RTS-20261012-001", "12/10/2026", "ASTA JAYA", "PO-SUP-20260920-003", "BM-20260925-002", "KAMERA MUNDUR AHD 170°", "", "2", "Rp 560.000",
         ("Diambil Sales", "p-teal"), ("Aktif", "p-coral"), "Sudah diambil sales · 14/10", ("Disetujui", "p-green"), ("Menunggu pengganti", "p-amber")),
        ("RTS-20261009-001", "09/10/2026", "PRIMA BODYKIT", "PO-SUP-20260901-001", "BM-20260910-001", "BODY KIT FORTUNER GR SPORT", "", "1", "Rp 6.800.000",
         ("Kirim", "p-blue"), ("Aktif", "p-coral"), "Bermasalah · paket tidak sampai", ("Menunggu", "p-amber"), ("Belum", "p-grey")),
        ("RTS-20261005-001", "05/10/2026", "CAHAYA AUDIO", "PO-SUP-20260915-002", "BM-20260918-001", "FRAME HEAD UNIT AVANZA 2022", "+1 barang", "4", "Rp 1.120.000",
         ("Kirim", "p-blue"), ("Aktif", "p-coral"), "Diterima supplier · 08/10", ("Sebagian", "p-amber"), ("Kredit sebagian", "p-amber")),
        ("RTS-20260930-001", "30/09/2026", "MITRA LAMPU JAYA", "PO-SUP-20260910-001", "BM-20260912-002", "STOPLAMP ASSY BRIO 2019", "", "2", "Rp 1.300.000",
         ("Diambil Sales", "p-teal"), ("Selesai", "p-green"), "Diterima supplier", ("Disetujui", "p-green"), ("Refund diterima", "p-green")),
        ("RTS-20260926-001", "26/09/2026", "ASTA JAYA", "PO-SUP-20260901-002", "BM-20260905-001", "BI-LED PROJECTOR 2.5\"", "", "1", "Rp 950.000",
         ("Kirim", "p-blue"), ("Dibatalkan", "p-red"), "Dibatalkan sebelum dikirim", ("—", "muted"), ("—", "muted")),
    ]
    trs = []
    for r in rows:
        no, tgl, sup, po, bm, brg, more, qty, nilai, met, st, keluar, kep, pen = r
        hl = ' class="hl"' if no == "RTS-20261017-001" else ""
        kp = f'<span class="pill {kep[1]}">{kep[0]}</span>' if kep[1] != "muted" else '<span class="muted">—</span>'
        pn = f'<span class="pill {pen[1]}">{pen[0]}</span>' if pen[1] != "muted" else '<span class="muted">—</span>'
        trs.append(f"""<tr{hl}><td style="white-space:nowrap"><b class="mono">{no}</b><div class="sub">{tgl}</div><div class="small strong" style="margin-top:3px">{sup}</div></td>
<td class="mono small">{po}<div class="sub mono">{bm}</div></td><td class="w-item">{brg}{f'<div class="sub">{more}</div>' if more else ''}</td>
<td class="num">{qty}</td><td class="num">{nilai}</td>
<td><span class="pill {st[1]}">{st[0]}</span> <span class="pill no {met[1]}">{met[0]}</span><div class="sub">{keluar}</div></td><td>{kp}</td><td>{pn}</td>
<td style="text-align:right;white-space:nowrap"><span class="btn sm">···</span></td></tr>""")
    menu = """<div class="modal" style="right:56px;top:452px;width:228px;border-radius:10px;padding:6px 0">
      <div class="tiny muted" style="padding:4px 12px 6px">RTS-20261017-001 · Menunggu pengambilan</div>
      <div style="padding:6px 12px">Lihat Detail</div><div style="padding:6px 12px">Cetak Nota Retur</div>
      <div style="padding:6px 12px">Cetak Bukti Serah Terima</div>
      <div style="padding:6px 12px;color:var(--accent-dark);font-weight:600">Catat Serah Terima Sales</div>
      <div style="padding:6px 12px">Ubah Jadwal Pengambilan</div><div class="divider"></div>
      <div style="padding:6px 12px;color:var(--failed)">Batalkan Retur… <span class="tag">admin</span></div></div>"""
    body = f"""
<div class="between"><div class="row" style="gap:8px;align-items:center">
  <div class="input" style="width:430px"><span class="muted">⌕</span><span class="ph" style="color:#A9A79F">Cari no. retur, PO, Barang Masuk, supplier, barang, SKU, resi, no. RMA…</span></div>
  <span class="input sel" style="width:168px">01/09/2026 – 18/10/2026</span><span class="input sel" style="width:150px">Semua supplier</span>
  <span class="btn">Filter lainnya · 2</span></div>
  <span class="btn pri">+ Buat Retur Supplier</span></div>
<div class="grid g6">{k}</div>
<div class="between"><div class="row" style="gap:6px">
  <span class="btn sm dark">Semua · 14</span><span class="btn sm">Perlu tindakan · 5</span><span class="btn sm">Aktif · 9</span>
  <span class="btn sm">Selesai · 3</span><span class="btn sm">Dibatalkan · 2</span></div>
  <div class="small muted">Filter aktif: <span class="tag">Metode: semua</span> <span class="tag">Penyelesaian: semua</span> <span class="tag">Status keputusan: semua</span></div></div>
<div class="tblwrap"><table class="tbl"><thead><tr><th>No. Retur · Supplier</th><th>Referensi PO / BM</th><th>Barang</th><th class="num">Qty</th><th class="num">Nilai Retur</th><th>Status · Metode · Barang Keluar</th><th>Keputusan</th><th>Penyelesaian</th><th></th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>
<div class="between small muted"><span>Klik baris untuk membuka detail · ··· untuk aksi cepat. Menampilkan 8 dari 14 retur · nilai retur aktif <b class="strong" style="color:var(--ink)">Rp 17.380.000</b> (harga beli)</span><span>‹ 1 2 ›</span></div>"""
    return "01_Daftar_Retur_Supplier", page("Retur Supplier", "Pengembalian barang ke supplier: rusak, salah kirim, cacat, atau klaim garansi.", body,
                                           extra="") .replace("</main>", menu + "</main>")


def s02():
    src_rows = [("AUTOVISION BI-LED FOGLAMP 3\"", "AV-FL3-BL", "PCS", "2", "0", "2", "Rp 750.000"),
                ("FRAME HEAD UNIT AVANZA 2022 9\"", "FR-AVZ22-9", "PCS", "3", "0", "3", "Rp 325.000"),
                ("SOCKET H11 KERAMIK", "SK-H11-CR", "PSG", "10", "2", "8", "Rp 18.000")]
    tr = "".join(f'<tr><td>{a}<div class="sub mono">{b}</div></td><td>{c}</td><td class="num">{d}</td><td class="num">{e}</td><td class="num strong">{f}</td><td class="num">{g}</td></tr>' for a, b, c, d, e, f, g in src_rows)
    body = f"""{doc_header()}
{steps(1)}
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Sumber retur <small>menentukan barang dan harga beli yang bisa dipilih</small></h3>
   <div class="grid g3">
    <div class="choice on"><span class="chk radio on"><i></i><b>Dari Barang Masuk / PO Supplier</b></span><div class="tiny muted" style="margin:4px 0 0 20px">Barang yang diterima dari supplier ini. Paling umum.</div></div>
    <div class="choice"><span class="chk radio"><i></i><b>Dari Stok Karantina</b></span><div class="tiny muted" style="margin:4px 0 0 20px">Retur/klaim garansi pelanggan yang sudah diperiksa dan layak diteruskan.</div></div>
    <div class="choice"><span class="chk radio"><i></i><b>Tanpa dokumen asal</b> <span class="tag">admin</span></span><div class="tiny muted" style="margin:4px 0 0 20px">Hanya bila asal barang tidak tercatat; alasan wajib.</div></div>
   </div></div>
  <div class="card"><h3>Informasi retur</h3>
   <div class="grid g4">
    <div class="field"><label>No. Retur</label><div class="input ro mono">RTS-20261018-001 (otomatis)</div></div>
    <div class="field"><label>Tanggal Retur <span class="req">*</span></label><div class="input">18/10/2026</div></div>
    <div class="field span2"><label>Supplier <span class="req">*</span></label><div class="input ro">ASTA JAYA <span class="tiny muted">· terisi dari Barang Masuk</span></div></div>
    <div class="field span2"><label>Barang Masuk <span class="req">*</span></label><div class="input sel focus"><b class="mono">BM-20261012-002</b><span class="muted">· diposting 12/10/2026 · 3 barang</span></div></div>
    <div class="field"><label>PO Supplier</label><div class="input ro mono">PO-SUP-20261010-001</div></div>
    <div class="field"><label>No. Faktur Supplier</label><div class="input ro mono">AJ/INV/2610/0412</div></div>
    <div class="field span2"><label>Jenis Retur / Klaim <span class="req">*</span></label><div class="input sel">Retur barang bermasalah (baru diterima)</div><div class="hint">Pilihan lain: Klaim garansi supplier · Kelebihan / salah kirim</div></div>
    <div class="field span2"><label>Penyelesaian yang diminta ke supplier</label><div class="input sel">Ganti barang</div><div class="hint">Keputusan akhir dicatat saat supplier merespons; bisa berbeda per barang.</div></div>
    <div class="field span2"><label>No. RMA / Persetujuan Supplier <span class="muted" style="text-transform:none">(opsional)</span></label><div class="input ph">mis. RMA-AJ-1018-07</div></div>
    <div class="field span2"><label>Catatan umum</label><div class="input area">Ditemukan saat uji pasang di bengkel 17/10. Sudah dikonfirmasi lewat WA ke Pak Budi (sales ASTA JAYA).</div></div>
   </div></div>
 </div>
 <div style="width:470px;display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Isi BM-20261012-002</h3>
   <table class="tbl"><thead><tr><th>Barang</th><th>Sat.</th><th class="num">Terima</th><th class="num">Diretur</th><th class="num">Bisa</th><th class="num">Harga</th></tr></thead><tbody>{tr}</tbody></table>
   <div class="hint" style="margin-top:8px">"Diretur" = sudah keluar ke supplier + sedang diproses di retur lain (RTS-20260920-001, 2 PSG).</div></div>
  <div class="card soft"><h3>Tagihan supplier terkait</h3>
   <dl class="kv"><dt>Faktur</dt><dd class="mono">FP-2026-0041 · AJ/INV/2610/0412</dd><dt>Total faktur</dt><dd>Rp 5.000.000</dd><dt>Sudah dibayar</dt><dd>Rp 1.500.000</dd><dt>Sisa tagihan</dt><dd class="strong">Rp 3.500.000</dd></dl></div>
  <div class="note info"><b>Draft tidak mengubah apa pun.</b> Stok, karantina, dan tagihan supplier baru berubah setelah retur disetujui, barang benar-benar keluar, atau supplier memberi keputusan.</div>
 </div>
</div>
<div class="between"><span class="btn">Batal</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Barang & Alasan →</span></div></div>"""
    return "02_Buat_Retur_Supplier", page("Buat Retur Supplier", "Langkah 1 dari 5 — pilih dokumen asal; supplier, PO, dan faktur terisi otomatis.", body)


def s03():
    body = f"""{doc_header()}
{steps(2)}
<div class="note warn"><b>AUTOVISION BI-LED FOGLAMP 3" sedang dicadangkan.</b> Stok di toko 2 PCS, keduanya dicadangkan untuk Sales Order <b class="mono">SO-20261015-0003</b> milik <b>RIRIN</b> (DP 30% sudah dibayar). Unit yang diretur akan membuat pesanan RIRIN kurang 1.
 <div class="row" style="gap:18px;margin-top:7px"><span class="chk radio on"><i></i>Lanjutkan &amp; tandai kekurangan RIRIN — draft PO kekurangan dibuat otomatis <span class="tag">admin</span></span>
 <span class="chk radio"><i></i>Tunda retur barang ini sampai ada stok pengganti</span></div></div>
<div class="tblwrap"><table class="tbl"><thead><tr><th style="width:28px"></th><th>Barang</th><th class="num">Diterima</th><th class="num">Sudah / sedang diretur</th><th class="num">Bisa diretur</th><th class="num" style="width:86px">Qty retur</th><th class="num">Harga beli</th><th class="num">Nilai retur</th><th style="width:150px">Kondisi</th><th style="width:170px">Alasan retur</th></tr></thead>
<tbody>
<tr class="hl"><td><span class="chk on"><i></i></span></td><td><b>AUTOVISION BI-LED FOGLAMP 3"</b><div class="sub mono">AV-FL3-BL · PCS</div></td><td class="num">2</td><td class="num">0</td><td class="num">2</td><td><div class="input focus" style="justify-content:flex-end">1</div></td><td class="num">Rp 750.000</td><td class="num strong">Rp 750.000</td><td><div class="input sel">Tidak berfungsi</div></td><td><div class="input sel">Tidak Berfungsi</div></td></tr>
<tr class="hl"><td colspan="10" style="padding:0 9px 12px 46px"><div class="card soft" style="padding:11px 13px"><div class="grid g4">
  <div class="field span2"><label>Hasil pengujian <span class="req">*</span> <span class="muted" style="text-transform:none">(wajib untuk "Tidak Berfungsi")</span></label><div class="input area">Diuji di aki 12V dan di mobil Innova, lampu kiri tidak menyala sama sekali. Kabel & soket sudah diganti, tetap mati.</div></div>
  <div class="field"><label>No. seri / identitas unit</label><div class="input mono">AVF3-2409-11872</div><div class="hint">Disalin ke nota retur.</div></div>
  <div class="field"><label>Foto / video bukti <span class="muted" style="text-transform:none">(disarankan)</span></label><div class="photos"><div class="photo">foto 1</div><div class="photo">foto 2</div><div class="photo">video</div><div class="photo add">+</div></div></div>
  <div class="field span4"><label>Catatan detail</label><div class="input">Fisik mulus, lensa tidak retak. Kelengkapan: kabel bawaan + baut.</div></div></div></div></td></tr>
<tr class="hl"><td><span class="chk on"><i></i></span></td><td><b>FRAME HEAD UNIT AVANZA 2022 9"</b><div class="sub mono">FR-AVZ22-9 · PCS</div></td><td class="num">3</td><td class="num">0</td><td class="num">3</td><td><div class="input" style="justify-content:flex-end">2</div></td><td class="num">Rp 325.000</td><td class="num strong">Rp 650.000</td><td><div class="input sel">Baik / belum dipakai</div></td><td><div class="input sel">Salah Model / Tipe</div></td></tr>
<tr class="hl"><td colspan="10" style="padding:0 9px 12px 46px"><div class="grid g4" style="font-size:12px"><div class="field span2"><label>Yang dipesan vs yang datang <span class="req">*</span></label><div class="input">Dipesan frame Avanza 2022 (9"), 2 dari 3 unit yang datang frame Avanza 2019 (7")</div></div><div class="field"><label>Foto</label><div class="photos"><div class="photo">foto 1</div><div class="photo add">+</div></div></div><div class="field"><label>No. seri</label><div class="input ph">—</div></div></div></td></tr>
<tr><td><span class="chk"><i></i></span></td><td>SOCKET H11 KERAMIK<div class="sub mono">SK-H11-CR · PSG</div></td><td class="num">10</td><td class="num">2</td><td class="num">8</td><td><div class="input ph" style="justify-content:flex-end">0</div></td><td class="num">Rp 18.000</td><td class="num muted">—</td><td></td><td></td></tr>
</tbody></table></div>
<div class="row">
 <div class="card grow"><h3>Pilihan alasan retur <small>per barang, bukan per dokumen</small></h3>
  <div class="row" style="gap:6px;flex-wrap:wrap">{''.join(f'<span class="tag">{x}</span>' for x in ["Rusak Saat Diterima","Cacat Produksi","Tidak Berfungsi → hasil uji wajib","Salah Barang","Salah Model / Tipe","Salah Warna","Tidak Sesuai Spesifikasi","Kelebihan Pengiriman","Klaim Garansi","Kesepakatan dengan Supplier","Lainnya → penjelasan wajib"])}</div>
  <div class="hint" style="margin-top:8px">Rusak / Cacat: foto wajib (bisa diubah di Pengaturan). Kelebihan Pengiriman: kondisi otomatis "Baik", tidak masuk karantina rusak.</div></div>
 <div class="card" style="width:340px"><dl class="kv"><dt>Barang dipilih</dt><dd>2 barang · 3 unit</dd><dt>Nilai retur</dt><dd class="strong serif" style="font-size:18px">Rp 1.400.000</dd><dt>Dasar nilai</dt><dd class="small">harga beli di BM-20261012-002</dd></dl></div>
</div>
<div class="between"><span class="btn">← Informasi Retur</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Packing Note →</span></div></div>"""
    return "03_Item_Alasan_Retur", page("Buat Retur Supplier", "Langkah 2 dari 5 — pilih barang, jumlah, kondisi, dan alasan per barang.", body)


def s04():
    mats = [("Dus original produk", True), ("Bubble wrap", True), ("Foam / busa pelindung", True), ("Kardus luar tebal", True),
            ("Kantong terpisah untuk aksesori", True), ("Plastik packing", False), ("Styrofoam", False), ("Pelindung sudut", False),
            ("Peti kayu", False), ("Tanpa kemasan tambahan", False), ("Lainnya…", False)]
    chips = "".join(f'<span class="chk {"on" if on else ""}" style="border:1px solid var(--line);border-radius:8px;padding:5px 9px;background:#fff"><i></i>{m}</span>' for m, on in mats)
    note = """PACKING NOTE — RETUR SUPPLIER
Nomor Retur : RTS-20261018-001
Supplier    : ASTA JAYA

KOLI 1 dari 1  (± 2,6 kg · 40 × 30 × 22 cm)
1. AUTOVISION BI-LED FOGLAMP 3" — 1 PCS
   Alasan : tidak menyala saat pengujian
   Kondisi: unit tidak berfungsi, bodi fisik baik
   Kemasan: dus original, unit dibungkus bubble
   wrap, kabel & baut di kantong terpisah
2. FRAME HEAD UNIT AVANZA 2022 9" — 2 PCS
   Alasan : salah model (datang tipe 2019 7")
   Kondisi: baik, belum dipakai, plastik utuh
Ruang kosong diisi foam, lalu dimasukkan ke
kardus luar tebal dan dilakban silang.

Catatan:
Kondisi fisik dan kelengkapan diperiksa sebelum
dikemas. Mohon dibuka hati-hati: lampu."""
    body = f"""{doc_header()}
{steps(3)}
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><div class="between"><h3 style="margin:0">Koli 1 dari 1</h3><div class="row" style="gap:6px"><span class="btn sm">+ Tambah koli</span><span class="btn sm ghost">Hapus</span></div></div>
   <div class="grid g4" style="margin-top:10px">
    <div class="field span2"><label>Profil barang <span class="muted" style="text-transform:none">(menentukan saran kemasan)</span></label><div class="row" style="gap:6px;flex-wrap:wrap">
      <span class="btn sm dark">Elektronik &amp; lampu</span><span class="btn sm">Body kit / bumper besar</span><span class="btn sm">Kaca / mika</span><span class="btn sm">Jok &amp; trim</span></div></div>
    <div class="field"><label>Berat (kg)</label><div class="input">2,6</div></div>
    <div class="field"><label>Dimensi (cm)</label><div class="input">40 × 30 × 22</div></div>
   </div>
   <div class="field" style="margin-top:10px"><label>Jenis kemasan &amp; bahan pelindung <span class="req">*</span> <span class="muted" style="text-transform:none">(boleh lebih dari satu)</span></label><div class="row" style="gap:6px;flex-wrap:wrap">{chips}</div></div>
   <div class="field" style="margin-top:10px"><label>Isi koli</label>
    <table class="tbl"><thead><tr><th>Barang</th><th class="num">Qty</th><th>Kelengkapan ikut dikirim</th><th>Kondisi kemasan</th></tr></thead><tbody>
     <tr><td>AUTOVISION BI-LED FOGLAMP 3"</td><td class="num">1</td><td><span class="chk on"><i></i>Kabel bawaan</span> <span class="chk on"><i></i>Baut</span> <span class="chk"><i></i>Buku manual</span></td><td>Dus original sedikit penyok</td></tr>
     <tr><td>FRAME HEAD UNIT AVANZA 2022 9"</td><td class="num">2</td><td><span class="chk on"><i></i>Klip pengunci (4/unit)</span></td><td>Plastik segel utuh</td></tr></tbody></table>
    <div class="hint">Semua barang retur harus masuk tepat satu koli: 3 / 3 unit sudah dimasukkan.</div></div>
   <div class="grid g2" style="margin-top:10px">
    <div class="field"><label>Foto sebelum dikemas</label><div class="photos"><div class="photo">foto 1</div><div class="photo">foto 2</div><div class="photo add">+</div></div></div>
    <div class="field"><label>Foto sesudah dikemas <span class="muted" style="text-transform:none">(wajib sebelum barang keluar)</span></label><div class="photos"><div class="photo add">+</div></div></div>
   </div></div>
  <div class="card soft"><h3>Saran untuk profil "Elektronik &amp; lampu"</h3><div class="grid g2 small">
   <span class="chk on"><i></i>Lensa/kaca menghadap ke dalam, dilapis bubble wrap 2 lapis</span><span class="chk on"><i></i>Kabel &amp; konektor dipisah dalam kantong</span>
   <span class="chk on"><i></i>Ruang kosong diisi foam, barang tidak bergeser saat dikocok</span><span class="chk"><i></i>Label "FRAGILE / JANGAN DIBANTING" di 2 sisi</span></div>
   <div class="hint">Body kit: pelindung sudut, plastik pelindung cat, kardus/peti besar, tulis "mudah lecet". Kaca/mika: karton keras di kedua sisi.</div></div>
 </div>
 <div style="width:410px;display:flex;flex-direction:column;gap:10px">
  <div class="between"><h3 class="serif" style="margin:0;font-size:14.5px">Catatan packing</h3><span class="btn sm">↻ Susun ulang dari pilihan</span></div>
  <div class="paper" style="font-size:11px">{note}</div>
  <div class="hint">Teks disusun otomatis dari pilihan di kiri dan tetap bisa diedit. Dipakai di Packing Note &amp; Bukti Serah Terima.</div>
  <div class="note info">Packing tidak mengubah stok. Stok berkurang hanya saat <b>Konfirmasi Barang Keluar</b>.</div>
 </div>
</div>
<div class="between"><span class="btn">← Barang &amp; Alasan</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Metode Pengembalian →</span></div></div>"""
    return "04_Packing_Note_Retur", page("Buat Retur Supplier", "Langkah 3 dari 5 — catat cara barang dikemas, isi tiap koli, dan foto.", body)


def method_cards(active: str) -> str:
    a = "on" if active == "kirim" else ""
    b = "on" if active == "sales" else ""
    return f"""<div class="grid g2">
 <div class="choice {a}"><span class="chk radio {a}"><i></i><b>A · Dikirim ke supplier</b></span><div class="small muted" style="margin:4px 0 0 20px">Lewat ekspedisi atau kurir. Butuh alamat tujuan; resi dan bukti kirim dicatat saat barang diserahkan ke ekspedisi.</div></div>
 <div class="choice {b}"><span class="chk radio {b}"><i></i><b>B · Diambil sales supplier di toko</b></span><div class="small muted" style="margin:4px 0 0 20px">Sales datang ke toko. Tidak perlu resi; diganti Bukti Serah Terima yang ditandatangani sales.</div></div>
</div>"""


def s05():
    body = f"""{doc_header()}
{steps(4)}
{method_cards("kirim")}
<div class="row">
 <div class="grow card"><h3>Rencana pengiriman <small>boleh diisi sekarang, dikunci saat barang keluar</small></h3>
  <div class="grid g4">
   <div class="field"><label>Ekspedisi <span class="req">*</span></label><div class="input sel">JNE</div></div>
   <div class="field"><label>Layanan</label><div class="input sel">REG</div></div>
   <div class="field"><label>Rencana tanggal kirim</label><div class="input">19/10/2026</div></div>
   <div class="field"><label>Jumlah koli</label><div class="input ro">1 (dari Packing Note)</div></div>
   <div class="field span2"><label>Alamat tujuan supplier <span class="req">*</span></label><div class="input area">ASTA JAYA — Gudang Retur<br>Jl. Mangga Dua Raya No. 18, Blok C/12, Jakarta Utara 14430</div><div class="hint">Terisi dari Master Supplier; perubahan di sini hanya untuk retur ini.</div></div>
   <div class="field"><label>Nama penerima</label><div class="input">Bpk. Hendra (Admin Retur)</div></div>
   <div class="field"><label>No. HP penerima</label><div class="input">0812 8899 1020</div></div>
   <div class="field"><label>Berat total (kg)</label><div class="input">2,6</div></div>
   <div class="field"><label>Perkiraan ongkir</label><div class="input">Rp 18.000</div></div>
   <div class="field span2"><label>Ongkir ditanggung</label><div class="row" style="gap:14px;padding-top:6px"><span class="chk radio"><i></i>Toko</span><span class="chk radio on"><i></i>Supplier (ditagihkan)</span><span class="chk radio"><i></i>Dibagi / sesuai kesepakatan</span></div><div class="hint">"Supplier" → ongkir ikut dalam klaim (refund / nota kredit), bukan biaya toko.</div></div>
  </div>
  <div class="divider" style="margin:12px 0"></div>
  <div class="grid g4" style="opacity:.62">
   <div class="field"><label>No. resi / AWB</label><div class="input ro">diisi saat konfirmasi keluar</div></div>
   <div class="field"><label>Tanggal &amp; jam serah ke ekspedisi</label><div class="input ro">—</div></div>
   <div class="field"><label>Ongkir aktual</label><div class="input ro">—</div></div>
   <div class="field"><label>Foto resi / bukti kirim</label><div class="input ro">—</div></div></div>
 </div>
 <div style="width:360px;display:flex;flex-direction:column;gap:12px">
  <div class="card soft"><h3>Status setelah retur disetujui</h3><div class="tl">
   <div class="ev now"><b>Siap Dikirim</b><div class="t">barang di rak karantina, paket siap</div></div>
   <div class="ev todo"><b>Dalam Pengiriman</b><div class="t">resi tercatat · stok fisik berkurang</div></div>
   <div class="ev todo"><b>Diterima Supplier</b><div class="t">dikonfirmasi supplier / cek resi</div></div></div>
   <div class="hint" style="margin-top:10px">"Dalam Pengiriman" berarti sudah di ekspedisi, <b>belum tentu</b> diterima supplier.</div></div>
  <div class="note warn">Reminder otomatis bila 2 hari setelah rencana kirim belum ada resi, dan bila 7 hari setelah kirim supplier belum mengonfirmasi terima.</div>
 </div>
</div>
<div class="between"><span class="btn">← Packing Note</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Review →</span></div></div>"""
    return "05_Retur_Dikirim_Ekspedisi", page("Buat Retur Supplier", "Langkah 4 dari 5 — metode A: barang dikirim ke supplier lewat ekspedisi.", body)


SCREENS = [s01, s02, s03, s04, s05]
