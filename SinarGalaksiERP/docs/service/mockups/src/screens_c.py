"""Mockups 12-16: penggantian unit, ditolak/dibatalkan, serah terima customer, integrasi, Master Service."""
from shell import page, doc_header, field, chk, choice


def _hdr(no, stage, pill, cust, item, nota, right='<span class="btn">Cetak ▾</span>'):
    return (doc_header(no, stage, pill, right).replace("Budi Santoso · 0812-3456-7890", cust)
            .replace('HEAD UNIT ANDROID 9" · SN HU12345678', item).replace("PJSG/26/08/0125", nota))


def s12():
    flow = [("1. Pabrik setuju", "✓ 20/10", "p-green"), ("2. Dikirim balik", "Menunggu", "p-amber"), ("3. Diterima toko", "Belum", "p-grey"),
            ("4. QC & SN baru", "Belum", "p-grey"), ("5. Serah customer", "Belum", "p-grey")]
    fl = '<span class="arr">→</span>'.join(f'<div class="stat"><div class="label">{a}</div><span class="pill {c}">{b}</span></div>' for a, b, c in flow)
    body = f"""{_hdr("SVC-20261016-002", "Di Supplier", "p-blue", "Ririn Oktavia · 0813-2200-4411", 'SUBWOOFER AKTIF 12" · SN SW98765432', "PJSG/26/07/0210", '<span class="pill p-green">Keputusan pabrik: Ganti Unit Baru</span>')}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Persetujuan Ganti Baru dari Pabrik</h3>
    <div class="grid g3">
      {field("No. Service", "SVC-20261016-002", "ro")}{field("Customer", "Ririn Oktavia", "ro")}{field("Nota Asal", "PJSG/26/07/0210 · 21/07/2026", "ro")}
      {field("Barang Customer", 'SUBWOOFER AKTIF 12" · SW98765432', "ro")}{field("Hasil Diagnosis", "Approved Replacement · RMA-ASTA-1102", "ro")}{field("PIC Pabrik", "ASTA JAYA · Bpk. Hendra", "ro")}
    </div></div>
  <div class="card"><h3>Cara Penggantian</h3>
    <div class="grid g3">
      {choice("A · Pabrik mengirim unit pengganti", "Default. Unit pengganti adalah titipan untuk customer; tidak masuk stok.", True)}
      {choice("B · Tukar dari stok toko sekarang", "Admin, hanya Garansi Aktif (K-S6). Stok OUT; unit rusak → Stok Karantina → Retur Supplier.")}
      {choice("C · Ganti model lain", "Model lama tidak ada. Selisih harga menjadi biaya di Persetujuan Biaya.")}
    </div></div>
  <div class="card"><h3>Alur Penerimaan Unit Pengganti</h3>
    <div class="flowrow">{fl}</div>
    <div class="note ok" style="margin-top:12px"><b>Penerimaan dicatat sekali.</b> Unit pengganti untuk customer dicatat sebagai titipan pengganti, bukan Barang Masuk. Barang Masuk (jenis <i>retur_pengganti</i>) hanya dipakai di cara B, saat supplier mengganti unit milik toko.</div></div>
  </div>
  <div style="width:400px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Detail Unit Pengganti</h3>
      {field("Jenis unit pengganti", 'SUBWOOFER AKTIF 12" (sama)', "sel")}<div style="height:8px"></div>
      <div class="grid g2">{field("Serial lama", "SW98765432", "ro mono")}{field("Serial baru", "isi saat unit tiba", "ph", hint="wajib sebelum QC")}</div><div style="height:8px"></div>
      {field("Resi balik dari pabrik", "menunggu pengiriman", "ph")}<div style="height:8px"></div>
      {field("Biaya tambahan", "Rp 0 · Tanpa Biaya", "ro")}</div>
    <div class="note warn"><b>Barang customer tetap milik customer.</b> Unit pengganti atas klaim customer tidak masuk stok dagangan; SN baru menggantikan SN lama untuk serah terima dan garansi berikutnya.</div>
  </div>
</div>
<div class="between"><span class="btn">Lihat Dokumen RMA</span><div class="row" style="gap:8px"><span class="btn">Catat Pengiriman Balik</span><span class="btn pri">Terima Unit Pengganti</span></div></div>"""
    return "12_Penggantian_Unit_Baru", page("Penggantian Unit Baru", "Cabang ketika pabrik memutuskan unit customer diganti baru", body)


def s13():
    reasons = ["Garansi ditolak", "Tidak bisa diperbaiki", "Customer batal", "Customer tolak biaya", "Tidak ditemukan kerusakan",
               "Diambil kembali tanpa perbaikan", "Salah input (draft saja)"]
    rs = "".join(f'<div>{chk(r, r == "Customer tolak biaya", True)}</div>' for r in reasons)
    body = f"""{_hdr("SVC-20261012-001", "Di Supplier", "p-blue", "Siti Aisyah · 0838-9900-1122", 'SPEAKER AKTIF 6.5" · SN SPK-2210', "PJSG/26/04/0080", '<span class="pill p-red">Garansi Ditolak pabrik</span>')}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Catat Penolakan / Pembatalan</h3>
    <div class="grid g3">
      {field("Asal Keputusan", "Customer (setelah garansi ditolak pabrik)", "sel")}
      {field("Tanggal Keputusan", "22/10/2026")}
      {field("Diputuskan oleh", "sinargalaksi88 (Admin)", "ro")}
    </div>
    <div class="field" style="margin-top:10px"><label>Jenis Keputusan <span class="req">*</span></label><div class="grid g4" style="gap:8px;padding-top:4px">{rs}</div></div>
    <div style="height:10px"></div>{field("Alasan Wajib", "Segel unit rusak menurut laporan pabrik, garansi ditolak. Biaya perbaikan Rp 275.000 ditolak customer; customer minta unit dikembalikan apa adanya.", "area", req=True)}</div>
  <div class="card"><h3>Catatan &amp; Bukti</h3>
    <div class="grid g3"><div class="drop">✓ Surat penolakan pabrik<br><span class="tiny">RMA-CA-0441.pdf</span></div><div class="drop">✓ Bukti komunikasi customer<br><span class="tiny">WA_2210.png</span></div><div class="drop">+ Bukti penerimaan balik<br><span class="tiny">diisi saat unit tiba</span></div></div>
    <div class="note warn" style="margin-top:12px"><b>Tolak klaim ≠ barang sudah diambil customer.</b> Dokumen tetap terbuka sampai unit benar-benar kembali dan diserahkan ke pemiliknya. Hasil akhir: <b>Dikembalikan Apa Adanya</b>.</div></div>
  </div>
  <div style="width:390px;display:flex;flex-direction:column;gap:12px">
    <div class="card"><h3>Tindak Lanjut</h3>
      <div style="display:flex;flex-direction:column;gap:8px">
      {choice("↶ Kembalikan Apa Adanya", "Minta unit dikirim balik dari pabrik → terima di toko → serah ke customer.", True)}
      {choice("⌕ Minta Pemeriksaan Ulang", "Eskalasi ke pabrik; catat bukti pendukung dan nomor eskalasi.")}
      <div class="choice" style="opacity:.5"><b>✕ Batalkan Sebelum Pengiriman</b><div class="small muted" style="margin-top:3px">Tidak tersedia: unit sudah keluar toko pada 12/10/2026.</div></div>
      </div></div>
    <div class="card soft"><h3 style="font-size:13.5px">Biaya yang sudah timbul</h3>
      <div class="sum"><span>Ongkir kirim (non-garansi)</span><span class="num">Rp 35.000</span><span class="muted">Ditagih ke customer</span><span class="num">K-S5</span></div></div>
  </div>
</div>
<div class="between"><span></span><div class="row" style="gap:8px"><span class="btn">Simpan Keputusan</span><span class="btn pri">Lanjut Pengembalian Barang →</span></div></div>"""
    return "13_Service_Ditolak_Dibatalkan", page("Ditolak / Dibatalkan", "Alasan wajib, bukti keputusan, dan unit tetap dilacak sampai kembali ke customer", body)


def s14():
    acc = [("Head unit", True), ("Kabel set", True), ("Box / Dus (ditahan di toko)", True)]
    a = "".join(f'<div>{chk(n, o)}</div>' for n, o in acc)
    proof = "".join(f'<div>{chk(n, True)}</div>' for n in ["Unit diuji bersama customer", "Kelengkapan cocok", "Tanda tangan penerima", "Nota hasil dicetak"])
    body = f"""{doc_header("SVC-20261016-001", "Siap Diambil", "p-green", '<span class="pill p-teal">Lokasi: Kembali di Toko</span>')}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Serah Terima Unit ke Customer</h3>
    <div class="grid g3">
      {field("Hasil Akhir", "Selesai Diperbaiki", "ro")}{field("Hasil QC Toko", "Lulus uji nyala &amp; audio")}{field("SN diserahkan", "HU12345678 (sama)", "ro mono")}
      {field("Tanggal Tiba Kembali", "25/10/2026", "ro")}{field("Tanggal / Jam Diambil", "26/10/2026 · 16:30", "focus", req=True)}{field("Petugas Toko", "andi (login)", "ro")}
      <div class="field"><label>Pengambil</label><div class="row" style="gap:12px;padding-top:6px">{chk("Customer sendiri", True, True)}{chk("Orang lain", False, True)}</div></div>
      {field("Nama / HP pengambil", "Budi Santoso · 0812-3456-7890", "ro", hint="orang lain: nama + HP + bawa tanda terima")}
      {field("Garansi Service", "30 hari, s/d 25/11/2026", "ro", hint="K-S7 · kerusakan yang sama")}
    </div>
    <div style="height:10px"></div>{field("Catatan Serah Terima", "Unit diuji bersama customer, menyala normal dan audio keluar. Kelengkapan lengkap: head unit, kabel set, box.", "area")}</div>
  <div class="row">
    <div class="grow card"><h3>Kelengkapan Kembali</h3><div style="display:flex;flex-direction:column;gap:7px">{a}</div><div class="hint">Dari kelengkapan saat terima. Item yang tidak kembali wajib diberi keterangan.</div></div>
    <div class="grow card"><h3>Bukti Pengambilan</h3><div class="grid g2" style="gap:7px">{proof}</div><div class="sig" style="margin-top:8px">✎ Tanda tangan penerima — Budi Santoso</div></div>
  </div>
  </div>
  <div style="width:380px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Ringkasan Biaya Service</h3>
      <div class="sum"><span>Sparepart · board power</span><span class="num">Rp 300.000</span><span>Jasa service pabrik</span><span class="num">Rp 50.000</span>
      <span>Total tagihan</span><span class="num">Rp 350.000</span><span>Sudah dibayar</span><span class="num">Rp 350.000</span>
      <span class="tot">Sisa tagihan</span><span class="tot num">Rp 0</span></div>
      <div class="row" style="gap:8px;margin-top:10px;align-items:center"><span class="pill p-green">Sudah Dibayar</span><span class="tag">Nota Jasa PJSG/26/10/0311 · cash</span></div></div>
    <div class="note ok"><b>Pembayaran lewat Nota Jasa Service.</b> Uang masuk di Kasir seperti nota biasa (K-S3); nota penjualan lama PJSG/26/08/0125 tidak diubah.</div>
    <div class="note info"><b>Dua catatan terpisah.</b> "Siap diambil" (25/10, unit lulus QC) dan "Diambil customer" (26/10 16:30, tanda tangan) disimpan masing-masing dengan waktu dan petugas.</div>
  </div>
</div>
<div class="between"><span class="btn">Buka Nota Jasa Service</span><div class="row" style="gap:8px"><span class="btn">Cetak Nota Hasil Service</span><span class="btn pri">Konfirmasi Serah Terima</span></div></div>"""
    return "14_Serah_Terima_Customer", page("Penyelesaian &amp; Serah Terima", "Biaya final, uji bersama customer, kelengkapan kembali, dan tanda terima pemilik", body)


def s15():
    cust = [("Nota asal", "PJSG/26/08/0125"), ("Terima service", "SVC-20261016-001"), ("Kirim pabrik", "SJS · resi 0108…5521"),
            ("Diagnosis", "RMA-ASTA-1088"), ("Service / ganti", "Keputusan pabrik"), ("Serah customer", "Nota Hasil")]
    shop = [("PO Supplier", "PO-SUP-20260801-002"), ("Barang Masuk", "BM-20260805-001"), ("Stok Karantina", "lot QUAR"),
            ("Retur Supplier", "RTS-20261020-001"), ("Barang keluar", "OUT 1×"), ("Barang pengganti", "BM retur_pengganti")]
    c = "".join(f'<div class="stat"><div class="label">{a}</div><b>{b}</b></div>' for a, b in cust)
    s = "".join(f'<div class="stat"><div class="label">{a}</div><b>{b}</b></div>' for a, b in shop)
    m = [("Service customer", "Customer + baris nota", "Tidak ada", "Unit di register titipan"),
         ("Pengiriman / serah sales", "Service + kemasan + resi / sales", "Tidak ada (lokasi titipan berubah)", "Bukti kirim / tanda tangan"),
         ("Pengganti dari pabrik (cara A)", "Diagnosis + SN baru", "Tidak ada", "SN lama &amp; baru dicatat"),
         ("Tukar dari stok toko (cara B)", "Service + barang stok", "OUT pengganti · IN + QUAR unit rusak", "Satu transaksi; admin; Garansi Aktif"),
         ("Retur Supplier (lanjutan cara B)", "Lot karantina service_klaim", "OUT saat serah ekspedisi / sales", "Tidak double posting"),
         ("Nota Jasa Service", "Biaya disetujui customer", "Hanya bila ada sparepart dari stok", "Nota lama tidak diubah")]
    trs = "".join(f"<tr><td><b>{a}</b></td><td>{b}</td><td>{x}</td><td>{d}</td></tr>" for a, b, x, d in m)
    body = f"""<div class="card"><h3>Barang Titipan Customer vs Barang Milik Toko</h3>
  <div class="grid g2">
    <div class="choice on"><b>Unit customer yang diservice</b><div class="small muted" style="margin-top:3px">Referensi: No. Service + nota asal. Dimiliki customer, bukan stok yang bisa dijual. Dicatat di register titipan dengan Lokasi Unit.</div></div>
    <div class="choice"><b>Barang milik toko yang diretur ke supplier</b><div class="small muted" style="margin-top:3px">Referensi: Retur Supplier + PO + Barang Masuk. Berdampak ke stok In Hand dan Stok Karantina.</div></div>
  </div>
  <div class="note warn" style="margin-top:12px"><b>Jangan mencatat stok dua kali.</b> Modul service tidak menulis ke stock_ledger sendiri. Stok dagangan hanya bergerak lewat Kasir, Barang Masuk, Stok Karantina, Retur Supplier, dan Stock Opname.</div></div>
<div class="row">
  <div class="grow card"><h3>Jejak Unit Customer</h3><div class="chain">{c}</div></div>
  <div class="grow card"><h3>Jejak Barang Dagangan Toko</h3><div class="chain">{s}</div></div>
</div>
<div class="card soft"><b>Titik temu (cara B):</b> unit pengganti keluar dari stok toko → customer; unit rusak customer menjadi milik toko → IN + QUAR (sumber <span class="mono">service_klaim</span>) → Retur Supplier tipe Klaim Garansi → supplier mengganti → Barang Masuk <span class="mono">retur_pengganti</span>. Sparepart dari stok yang dipakai service keluar lewat Nota Jasa Service (Kasir).</div>
<div class="tblwrap"><table class="tbl"><thead><tr><th>Dokumen / Modul</th><th>Sumber Data</th><th>Pengaruh ke Stok</th><th>Syarat Penting</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return "15_Integrasi_Service_Retur_Inventory", page("Integrasi Service, Retur &amp; Inventory", "Memisahkan kepemilikan barang customer dari persediaan barang dagangan toko", body)


def s16():
    rows = [("SV-01", "Head Unit Android", "02.01 Head Unit", "12 bln", "ASTA JAYA", "SN wajib · reset", "42", True),
            ("SV-02", "Subwoofer Aktif", "02.08 Subwoofer", "12 bln", "per brand", "SN wajib", "18", False),
            ("SV-03", "Speaker / Speaker Aktif", "02.03 Speaker &amp; Tweeter", "6 bln", "per brand", "—", "35", False),
            ("SV-04", "Kamera Mundur", "03.02 Kamera Mundur · 03.01 Dashcam · 03.05 Kamera 360°", "6 bln", "CAHAYA AUDIO", "—", "21", False),
            ("SV-05", "Power Amplifier", "02.06 Power Amplifier", "12 bln", "per brand", "SN wajib", "9", False),
            ("SV-06", "Audio Processor / DSP", "02.05 DSP &amp; Audio Processor", "12 bln", "per brand", "SN wajib", "6", False),
            ("SV-07", "Elektronik Lainnya", "05.03 Modul Elektronik · 05.09 Inverter · per barang", "3 bln", "—", "—", "4", False)]
    trs = "".join(f'<tr{" class=hl" if on else ""}><td class="mono">{a}</td><td><b>{b}</b></td><td class="small">{c}</td><td>{d}</td><td class="small">{e}</td><td class="small">{f}</td><td class="num">{g}</td><td><span class="pill p-green">Aktif</span></td></tr>' for a, b, c, d, e, f, g, on in rows)
    acc = ["Head unit", "Kabel set", "Frame", "Kamera", "Antena GPS", "Remote", "Box / Dus", "Buku manual"]
    al = "".join(f'<div class="between small" style="border:1px solid var(--line);border-radius:7px;padding:4px 8px;background:#fff"><span>⋮⋮ {x}</span><span class="muted">×</span></div>' for x in acc)
    body = f"""<div class="between"><div class="input" style="width:380px"><span class="muted">⌕</span><span style="color:#A9A79F">Cari jenis service, kategori, brand…</span></div>
  <div class="row" style="gap:8px"><span class="btn">Tampilkan nonaktif</span><span class="btn pri">+ Jenis Service</span></div></div>
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="tblwrap"><table class="tbl"><thead><tr><th>Kode</th><th>Jenis Service</th><th>Kategori Tercakup</th><th>Garansi</th><th>Tujuan Default</th><th>Syarat</th><th class="num">Barang</th><th>Status</th></tr></thead><tbody>{trs}</tbody></table></div>
  <div class="card"><h3>Pengecualian per Brand / Barang <small>Head Unit Android</small></h3>
    <table class="tbl"><thead><tr><th>Berlaku untuk</th><th>Garansi</th><th>Tujuan service</th><th></th></tr></thead><tbody>
    <tr><td>Brand <b>AVIX</b></td><td>12 bln</td><td>ASTA JAYA — Divisi Service</td><td style="text-align:right"><span class="btn sm">Ubah</span></td></tr>
    <tr><td>Brand <b>SKYWAY</b></td><td>6 bln</td><td>Service center SKYWAY (supplier)</td><td style="text-align:right"><span class="btn sm">Ubah</span></td></tr>
    <tr><td>Barang <b>HEAD UNIT ANDROID 10" PRO 4/64</b></td><td>18 bln</td><td>ASTA JAYA</td><td style="text-align:right"><span class="btn sm">Ubah</span></td></tr>
    </tbody></table><div class="hint" style="margin-top:6px">Urutan: barang → brand → jenis. Angka garansi di mockup hanya contoh (K-S1).</div></div>
  </div>
  <div style="width:430px" class="card"><h3>Ubah Jenis Service · SV-01</h3>
    <div class="grid g2">{field("Nama", "Head Unit Android", req=True)}{field("Kode", "SV-01", "ro mono")}</div><div style="height:8px"></div>
    {field("Kategori tercakup", '<span class="tag">02.01 Head Unit</span> <span class="muted">+ tambah kategori</span>')}<div style="height:8px"></div>
    <div class="grid g3">{field("Garansi toko", "12 bulan")}{field("Garansi service", "30 hari")}{field("Lama service", "14–21 hari")}</div><div style="height:8px"></div>
    {field("Tujuan default", "ASTA JAYA", "sel")}
    <div style="display:flex;flex-direction:column;gap:6px;margin:10px 0">{chk("Serial number wajib saat terima", True)}{chk("Foto unit + SN wajib", True)}{chk("Catat SN saat dijual di Kasir (K-S2)", True)}{chk("Tanya persetujuan reset data", True)}</div>
    <div class="field"><label>Template kelengkapan</label><div class="grid g2" style="gap:5px">{al}</div></div><div style="height:8px"></div>
    {field("Keluhan cepat", "Mati total · Layar gelap · Tidak ada suara · Touchscreen error · Restart sendiri", "area")}<div style="height:8px"></div>
    {field("Syarat garansi (tercetak)", "Segel & stiker utuh, tidak kena air, tidak dibongkar pihak lain.", "area")}
    <div class="between" style="margin-top:12px"><span class="btn danger">Nonaktifkan</span><span class="btn pri">Simpan</span></div></div>
</div>"""
    return "16_Master_Service", page("Master Service", "Jenis barang yang bisa diservice beserta garansi, tujuan, kelengkapan, dan syaratnya", body, active="Master Service")


SCREENS = [s12, s13, s14, s15, s16]
