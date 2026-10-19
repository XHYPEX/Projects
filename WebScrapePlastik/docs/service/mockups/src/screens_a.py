"""Mockups 01-05: daftar service, terima (customer & nota), cari nota & garansi, identitas barang, tanda terima."""
from shell import page, steps, field, chk


def s01():
    kpis = [("Dalam Proses", "11", "semua yang belum selesai"), ("Menunggu Dikirim / Diambil", "3", "1 jadwal sales lewat"),
            ("Di Supplier / Pabrik", "5", "2 lebih dari 14 hari"), ("Menunggu Customer", "2", "estimasi belum dijawab"),
            ("Siap Diambil", "3", "1 lebih dari 30 hari"), ("Bermasalah", "2", "perlu tindakan")]
    k = "".join(f'<div class="kpi"><div class="label">{a}</div><div class="v">{b}</div><div class="d">{c}</div></div>' for a, b, c in kpis)
    k = k.replace('<div class="v">2</div><div class="d">perlu tindakan', '<div class="v" style="color:var(--failed)">2</div><div class="d">perlu tindakan')
    rows = [
        ("SVC-20261016-001", "16/10/2026", "Budi Santoso", "0812-3456-7890 · B 1456 WZU", "PJSG/26/08/0125", "beli 10/08/2026",
         'HEAD UNIT ANDROID 9" AVIX', "SN HU12345678", ("Di Supplier", "p-blue"), "Di ekspedisi · JNE YES", ("Aktif", "p-green"),
         ("Menunggu Estimasi", "p-grey"), "Belum diputuskan", "3"),
        ("SVC-20261016-002", "16/10/2026", "Ririn Oktavia", "0813-2200-4411 · B 2011 KJT", "PJSG/26/07/0210", "beli 21/07/2026",
         'SUBWOOFER AKTIF 12"', "SN SW98765432", ("Di Supplier", "p-blue"), "Di pabrik · ASTA JAYA", ("Aktif", "p-green"),
         ("Tanpa Biaya", "p-green"), "Diganti Baru", "3"),
        ("SVC-20261015-001", "15/10/2026", "Andi Wijaya", "0857-1100-2323 · B 1802 PQA", "PJSG/25/09/0098", "beli 12/09/2025",
         "KAMERA MUNDUR AHD 170°", "SN —", ("Menunggu Customer", "p-amber"), "Di pabrik · CAHAYA AUDIO", ("Habis", "p-red"),
         ("Menunggu Persetujuan", "p-amber"), "Belum diputuskan", "4"),
        ("SVC-20261014-001", "14/10/2026", "Dewi Lestari", "0812-7788-1200 · B 1290 TRD", "PJSG/26/06/0105", "beli 03/06/2026",
         "POWER AMPLIFIER 4CH", "SN PA4C-0091", ("Siap Diambil", "p-green"), "Kembali di toko", ("Aktif", "p-green"),
         ("Sudah Dibayar", "p-green"), "Selesai Diperbaiki", "5"),
        ("SVC-20261013-001", "13/10/2026", "Maya Sari", "0821-3344-5566 · B 2290 SMA", "PJSG/26/05/0087", "beli 18/05/2026",
         'HEAD UNIT ANDROID 10"', "SN HU10-77812", ("Di Supplier", "p-blue"), "Dibawa sales · Bpk. Rudi", ("Aktif", "p-green"),
         ("Menunggu Estimasi", "p-grey"), "Belum diputuskan", "6"),
        ("SVC-20261012-001", "12/10/2026", "Siti Aisyah", "0838-9900-1122 · B 1777 SAA", "PJSG/26/04/0080", "beli 09/04/2026",
         "SPEAKER AKTIF 6.5\"", "SN SPK-2210", ("Kembali ke Toko", "p-teal"), "Dalam perjalanan balik", ("Ditolak", "p-red"),
         ("Ditolak Customer", "p-red"), "Ditolak", "7"),
        ("SVC-20261010-001", "10/10/2026", "Hendra Wijaya", "0812-4455-6677 · B 1010 HWJ", "Tanpa nota", "beli di tempat lain",
         "DSP AUDIO 8CH", "SN DSP8-1180", ("Menunggu Dikirim", "p-amber"), "Di toko", ("Belum Diverifikasi", "p-grey"),
         ("Menunggu Estimasi", "p-grey"), "Belum diputuskan", "9"),
        ("SVC-20261009-001", "09/10/2026", "Lina Marlina", "0819-2233-4455 · B 1919 LMA", "PJSG/26/03/0065", "beli 14/03/2026",
         "SUBWOOFER PASIF 10\"", "SN —", ("Selesai", "p-green"), "Diserahkan 15/10", ("Aktif", "p-green"),
         ("Tanpa Biaya", "p-green"), "Selesai Diperbaiki", "—"),
    ]
    trs = []
    for r in rows:
        no, tgl, cust, cs, nota, ns, brg, sn, st, loc, gar, bia, has, umur = r
        hl = ' class="hl"' if no == "SVC-20261016-001" else ""
        flag = ' <span class="pill p-red no" title="Bermasalah">!</span>' if no in ("SVC-20261013-001", "SVC-20261010-001") else ""
        trs.append(f"""<tr{hl}><td style="white-space:nowrap"><b class="mono">{no}</b>{flag}<div class="sub">{tgl}{" · " + umur + " hari" if umur != "—" else ""}</div></td>
<td style="white-space:nowrap"><b>{cust}</b><div class="sub">{cs.split(" · ")[0]}</div><div class="sub">{cs.split(" · ")[1]}</div></td><td class="mono small">{nota}<div class="sub">{ns}</div></td>
<td class="w-item">{brg}<div class="sub mono">{sn}</div></td>
<td><span class="pill {st[1]}">{st[0]}</span><div class="sub">{loc}</div></td>
<td><span class="pill {gar[1]}">{gar[0]}</span></td><td><span class="pill {bia[1]}">{bia[0]}</span></td>
<td class="small">{has}</td><td style="text-align:right"><span class="btn sm">···</span></td></tr>""")
    menu = """<div class="modal" style="right:40px;top:352px;width:236px;border-radius:10px;padding:6px 0">
      <div class="tiny muted" style="padding:4px 12px 6px">SVC-20261016-001 · Di ekspedisi</div>
      <div style="padding:6px 12px">Lihat Detail</div><div style="padding:6px 12px">Cetak Tanda Terima</div>
      <div style="padding:6px 12px">Cetak Surat Jalan Service</div>
      <div style="padding:6px 12px;color:var(--accent-dark);font-weight:600">Catat Pabrik Menerima</div>
      <div style="padding:6px 12px">Catat Hasil Pabrik</div><div style="padding:6px 12px">Hubungi Customer (WA)</div><div class="divider"></div>
      <div style="padding:6px 12px;color:var(--muted)">Batalkan… <span class="tag">unit sudah keluar</span></div></div>"""
    body = f"""
<div class="between"><div class="row" style="gap:8px;align-items:center">
  <div class="input" style="width:470px"><span class="muted">⌕</span><span style="color:#A9A79F">Cari no. service, customer, HP, plat, no. nota, barang, SN, resi, RMA…</span></div>
  <span class="input sel" style="width:168px">01/09/2026 – 19/10/2026</span><span class="input sel" style="width:150px">Semua tahap</span>
  <span class="btn">Filter lainnya · 0</span></div>
  <span class="btn pri">+ Terima Service Baru</span></div>
<div class="grid g6">{k}</div>
<div class="between"><div class="row" style="gap:6px">
  <span class="btn sm dark">Semua · 28</span><span class="btn sm">Perlu tindakan · 6</span><span class="btn sm">Aktif · 11</span>
  <span class="btn sm">Selesai · 15</span><span class="btn sm">Dibatalkan · 2</span></div>
  <div class="small muted">Filter: <span class="tag">Garansi: semua</span> <span class="tag">Biaya: semua</span> <span class="tag">Penanganan: semua</span> <span class="tag">Supplier: semua</span></div></div>
<div class="tblwrap"><table class="tbl"><thead><tr><th>No. Service</th><th>Customer</th><th>Nota Asal</th><th>Barang</th><th>Tahap · Lokasi Unit</th><th>Garansi</th><th>Biaya</th><th>Hasil</th><th></th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>
<div class="between small muted"><span>Tanggal terima · umur sejak diterima. Klik baris untuk membuka detail · <span class="pill p-red no">!</span> Bermasalah (pabrik &gt; 14 hari tanpa kabar, jadwal sales lewat, siap diambil &gt; 30 hari). Menampilkan 8 dari 28.</span><span>‹ 1 2 3 4 ›</span></div>
<div class="note ok"><b>Unit customer bukan stok dagangan.</b> Barang yang masuk service dicatat di register titipan dan tidak pernah menambah atau mengurangi Stok In Hand.</div>"""
    return "01_Daftar_Service_Masuk", page("Service / Garansi", "Unit customer yang diservice, diklaim garansi, atau dikirim ke pabrik", body, extra=menu)


def s02():
    body = f"""{steps(1)}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Customer &amp; Kendaraan</h3>
    <div class="grid g3">
      {field("No. Service", "dibuat saat disimpan · SVC-YYYYMMDD-NNN", "ro")}
      {field("Tanggal / Jam Terima", "16/10/2026 · 10:30", "ro", hint="waktu toko, otomatis")}
      {field("Diterima oleh", "andi (Staff)", "ro")}
      {field("Nomor HP", "0812-3456-7890", "focus", req=True, hint="dicari di Master Customer · ditemukan")}
      {field("Nama Customer", "Budi Santoso", req=True)}
      {field("Customer", '<span class="pill p-green">Terdaftar</span> <span class="small muted">sejak 03/2025 · 4 nota · 1 service</span>', "ro")}
      {field("Plat Nomor", "B 1456 WZU", hint="terisi dari nota asal")}
      {field("Mobil", "Toyota Innova Reborn · Hitam", span="span2")}
    </div></div>
  <div class="card"><h3>Nota Pembelian Asal <small>dasar garansi</small></h3>
    <div class="row" style="align-items:center">
      <div class="input grow" style="flex:1"><span class="muted">⌕</span>Cari nota / nama / HP / plat / SN…</div>
      <span class="btn">Cari Nota</span><span class="btn ghost">Tanpa nota…</span></div>
    <div class="card soft" style="margin-top:10px;padding:10px 12px">
      <div class="between"><div><b class="mono">PJSG/26/08/0125</b> <span class="muted small">· 10/08/2026 · Lunas · kasir andi</span>
        <div class="small" style="margin-top:3px">Baris dipilih: <b>HEAD UNIT ANDROID 9" AVIX AX-9</b> · 1 PCS · Rp 2.850.000</div></div>
        <div class="row" style="gap:8px;align-items:center"><span class="pill p-green">Garansi Aktif (indikasi)</span><span class="btn sm">Ganti</span></div></div></div>
  </div></div>
  <div style="width:400px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Kunjungan</h3>
      <div class="small">Customer membawa lebih dari satu unit?</div>
      <div class="row" style="gap:14px;margin-top:8px">{chk("Satu unit", True, True)}{chk("Beberapa unit", False, True)}</div>
      <div class="hint" style="margin-top:8px">Setiap unit menjadi dokumen service sendiri dalam satu kunjungan; tanda terima bisa dicetak sekaligus.</div></div>
    <div class="card"><h3>Riwayat Customer</h3>
      <div class="kv tight small"><dt>Nota</dt><dd>4 nota · terakhir 10/08/2026</dd><dt>Service</dt><dd>1 · SVC-20260512-002 (kamera, selesai)</dd>
      <dt>Aktif</dt><dd><span class="muted">tidak ada service aktif</span></dd></div></div>
    <div class="note info"><b>Barang diisi di langkah 2.</b> Nama barang, merek, SN, dan keluhan tidak diminta di sini supaya tidak dobel input.</div>
  </div>
</div>
<div class="between"><span class="btn ghost">Batalkan</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Identitas Barang →</span></div></div>"""
    return "02_Terima_Service_Customer_Nota", page("Terima Service Baru", "Langkah 1 dari 4 · siapa pemilik unit dan nota pembelian mana yang menjadi dasar garansi", body)


def s03():
    rows = [
        ("PJSG/26/08/0125", "10/08/2026", 'HEAD UNIT ANDROID 9" AVIX AX-9', "1 PCS", "Budi Santoso", "B 1456 WZU", "Lunas", True, ""),
        ("PJSG/26/08/0125", "10/08/2026", "FRAME HEAD UNIT INNOVA REBORN 9\"", "1 PCS", "Budi Santoso", "B 1456 WZU", "Lunas", False, "bukan jenis service"),
        ("PJSG/26/08/0119", "03/08/2026", "KAMERA MUNDUR AHD 170°", "1 PCS", "Budi Santoso", "B 1456 WZU", "Lunas", False, ""),
        ("PJSG/25/03/0042", "11/03/2025", "SPEAKER COAXIAL 6.5\"", "1 PSG", "Budi Santoso", "B 1456 WZU", "Lunas", False, "garansi habis"),
    ]
    trs = []
    for no, tgl, brg, q, c, pl, st, sel, note in rows:
        act = '<span class="pill p-green">Dipilih</span>' if sel else ('<span class="btn sm">Pilih</span>' if note != "bukan jenis service" else '<span class="tag">bukan jenis service</span>')
        nn = f'<div class="sub">{note}</div>' if note and note != "bukan jenis service" else ""
        trs.append(f'<tr{" class=hl" if sel else ""}><td class="mono">{no}</td><td>{tgl}</td><td class="w-item">{brg}{nn}</td><td>{q}</td><td style="white-space:nowrap">{c}<div class="sub">{pl}</div></td><td><span class="pill p-green">{st}</span></td><td style="text-align:right">{act}</td></tr>')
    body = f"""{steps(1)}
<div class="row">
  <div class="grow card"><h3>Cari Nota Pembelian Customer <small>hasil per baris barang, bukan per nota</small></h3>
    <div class="row" style="align-items:center"><div class="input focus" style="flex:1"><span class="muted">⌕</span>0812-3456-7890</div>
      <span class="input sel" style="width:170px">Semua · nota / HP / plat / SN</span></div>
    <div class="tblwrap" style="margin-top:10px"><table class="tbl"><thead><tr><th>No. Nota</th><th>Tanggal</th><th>Barang</th><th>Qty</th><th>Customer</th><th>Status</th><th></th></tr></thead>
    <tbody>{''.join(trs)}</tbody></table></div>
    <div class="note ok" style="margin-top:10px"><b>Garansi dicek per baris barang.</b> Yang dihitung adalah barang yang benar-benar dibeli, bukan sekadar nama customer. Frame tidak termasuk jenis barang service di Master Service.</div>
  </div>
  <div style="width:420px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Pemeriksaan Garansi</h3>
      <dl class="kv"><dt>Nota</dt><dd class="mono">PJSG/26/08/0125</dd><dt>Tanggal beli</dt><dd>10 Agustus 2026</dd>
      <dt>Barang</dt><dd>HEAD UNIT ANDROID 9" AVIX AX-9</dd><dt>Jenis service</dt><dd>Head Unit Android <span class="tag">Master Service</span></dd>
      <dt>Masa garansi</dt><dd>12 bulan <span class="muted small">(contoh · K-S1)</span></dd><dt>Batas garansi</dt><dd><b>10 Agustus 2027</b></dd>
      <dt>Umur pembelian</dt><dd>2 bulan 6 hari</dd><dt>SN di nota</dt><dd class="muted">tidak tercatat (lihat K-S2)</dd>
      <dt>Indikasi</dt><dd><span class="pill p-green">Garansi Aktif</span></dd></dl></div>
    <div class="note warn"><b>Garansi tidak otomatis dijamin.</b> Tanggal nota hanya indikasi. Segel, SN, dan jenis kerusakan tetap diperiksa pabrik; keputusan akhir dicatat di Hasil Pemeriksaan.</div>
    <div class="card soft"><h3 style="font-size:13.5px">Nota tidak ditemukan?</h3>
      <div class="small">Pilih <b>Tanpa nota</b> dan alasannya: beli di tempat lain, nota hilang, atau hadiah. Garansi menjadi <span class="pill p-grey">Belum Diverifikasi</span>; kartu garansi pabrik bisa difoto sebagai dasar.</div>
      <div class="hint err" style="margin-top:6px">Jangan membuat nomor nota fiktif.</div></div>
  </div>
</div>
<div class="between"><span class="btn">← Kembali</span><div class="row" style="gap:8px"><span class="btn">Ubah status garansi… <span class="tag">admin</span></span><span class="btn pri">Pakai Baris Ini</span></div></div>"""
    return "03_Cari_Nota_Validasi_Garansi", page("Terima Service Baru", "Langkah 1 · cari nota pembelian dan cek indikasi garansi", body)


def tog(text, on):
    style = ' style="border-color:var(--accent);color:var(--accent-dark);background:rgba(217,119,87,.08)"' if on else ""
    return f'<span class="btn sm"{style}>{text}</span>'


def s04():
    acc = [("Head Unit", True), ("Kabel Set", True), ("Frame", False), ("Kamera", False), ("Antena GPS", False),
           ("Remote", False), ("Box / Dus", True), ("Buku Manual", False)]
    a = "".join(f'<div>{chk(n, o)}</div>' for n, o in acc)
    cond = "".join(tog(t, on) for t, on in [("Lecet", True), ("Retak", False), ("Penyok", False), ("Bekas bongkar", False),
                                             ("Segel utuh", True), ("Segel rusak", False)])
    comp = "".join(tog(t, on) for t, on in [("Mati total", True), ("Layar gelap", True), ("Tidak ada suara", False),
                                             ("Touchscreen error", False), ("Restart sendiri", False), ("Kamera tidak tampil", False)])
    body = f"""{steps(2)}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Identitas Fisik Barang</h3>
    <div class="grid g3">
      {field("Barang", 'HEAD UNIT ANDROID 9" AVIX AX-9', "ro", hint="dari baris nota PJSG/26/08/0125")}
      {field("Jenis Service", "Head Unit Android", "sel", hint="Master Service · SN wajib · foto wajib")}
      {field("Merek", "AVIX", "ro")}
      {field("Model / Tipe", "AX-9 · RAM 2 / ROM 32GB")}
      {field("Serial Number", "HU12345678", "mono focus", req=True, hint="tidak ada service aktif dengan SN ini")}
      {field("Qty / Satuan", "1 PCS", hint="sepasang speaker = 1 unit, 2 PCS")}
    </div>
    <div class="field" style="margin-top:10px"><label>Kondisi Fisik Awal <span class="req">*</span></label>
      <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:6px">{cond}</div>
      <div class="input area">Layar tidak retak, bodi lecet kecil sisi kanan, segel garansi utuh.</div></div>
    <div class="field" style="margin-top:10px"><label>Keluhan Customer <span class="req">*</span> <span style="text-transform:none;font-weight:400">(kata-kata customer; diagnosis diisi kemudian)</span></label>
      <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:6px">{comp}</div>
      <div class="input area">Unit tidak menyala, layar hitam setelah diberi tegangan sesuai prosedur.</div></div>
  </div>
  <div class="card"><h3>Foto Kondisi Masuk <small>minimal unit + nomor seri</small></h3>
    <div class="grid g5">
      <div class="drop">✓ Depan unit<br><span class="tiny">IMG_001.jpg</span></div><div class="drop">✓ Nomor seri<br><span class="tiny">IMG_002.jpg</span></div>
      <div class="drop">✓ Kelengkapan<br><span class="tiny">IMG_003.jpg</span></div><div class="drop">+ Segel / stiker garansi</div><div class="drop">+ Bukti tes di toko<br><span class="tiny">opsional</span></div>
    </div></div>
  </div>
  <div style="width:380px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Kelengkapan yang Dibawa</h3>
      <div class="grid g2" style="gap:8px">{a}</div>
      <div class="field" style="margin-top:10px"><label>Barang lainnya</label><div class="input ph">Tidak ada</div></div>
      <div class="hint">Template dari Master Service · Head Unit Android. Dicek ulang saat serah ke customer.</div></div>
    <div class="card"><h3>Khusus Head Unit Android</h3>
      <div style="display:flex;flex-direction:column;gap:7px">{chk("Customer setuju unit boleh di-reset (data & akun bisa hilang)", True)}{chk("Kunci layar / akun sudah dilepas customer", True)}</div>
      <div class="hint" style="margin-top:6px">Password / PIN customer tidak disimpan di sistem.</div></div>
  </div>
</div>
<div class="between"><span class="btn">← Kembali</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Penanganan →</span></div></div>"""
    return "04_Detail_Barang_Kelengkapan", page("Terima Service Baru", "Langkah 2 dari 4 · identitas unit, kondisi, keluhan, kelengkapan, foto", body)


SURAT_TERIMA = """SINAR GALAKSI VARIASI MOBIL                            TANDA TERIMA SERVICE
Jl. ............................ Telp. ..........  No.  : SVC-20261016-001
                                                   Tgl. : 16/10/2026 10:30
Customer : Budi Santoso                            Nota : PJSG/26/08/0125
HP       : 0812-3456-7890                          Beli : 10/08/2026
Mobil    : Toyota Innova Reborn  B 1456 WZU        Garansi: Aktif s/d 10/08/2027
                                                            (indikasi, cek pabrik)
--------------------------------------------------------------------------------
Barang   : HEAD UNIT ANDROID 9"   Merek: AVIX   Tipe: AX-9 2/32GB
No. Seri : HU12345678             Qty  : 1 PCS
Kondisi  : Layar tidak retak, bodi lecet kecil sisi kanan, segel utuh
Keluhan  : Unit tidak menyala, layar hitam setelah diberi tegangan
Kelengkapan:
  [x] Head unit   [x] Kabel set   [ ] Frame     [ ] Kamera    [ ] Antena GPS
  [ ] Remote      [x] Box/Dus     [ ] Manual    Lainnya: -
--------------------------------------------------------------------------------
Biaya    : BELUM DITENTUKAN - menunggu hasil pemeriksaan. Customer dihubungi
           dan diminta persetujuan sebelum ada biaya.
Estimasi : 14-21 hari kerja (bila dikirim ke pabrik)
Catatan  : Customer setuju unit boleh di-reset (data/akun bisa hilang).
--------------------------------------------------------------------------------
* Bawa tanda terima ini saat mengambil barang.
* Barang yang tidak diambil 90 hari setelah dikabari bukan tanggung jawab toko.
* Garansi mengikuti hasil pemeriksaan pabrik.

   Petugas Penerima,                                 Customer / Penyerah,


  (   andi   )                                       (   Budi Santoso   )"""


def s05():
    body = f"""<div class="row">
  <div class="grow card soft" style="display:flex;justify-content:center;padding:20px">
    <div class="paper doc">{SURAT_TERIMA}</div></div>
  <div style="width:360px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Cetak</h3>
      {field("Dokumen", "Tanda Terima Service", "sel")}
      <div style="height:8px"></div>{field("Printer", 'Epson LX-310 · continuous form 9,5" × 11"', "sel")}
      <div style="height:8px"></div><div class="field"><label>Rangkap</label><div class="small">Lembar 1 <b>Customer</b> · Lembar 2 <b>Toko / arsip</b> · sobekan bawah: <b>Label Unit</b></div></div>
      <div style="height:8px"></div><div class="grid g2">{field("Offset kiri", "0 kolom")}{field("Offset atas", "0 baris")}</div>
      <div class="row" style="gap:8px;margin-top:12px"><span class="btn pri">Cetak Tanda Terima</span><span class="btn">Cetak Label Unit</span></div></div>
    <div class="card"><h3 style="font-size:13.5px">Label Unit</h3>
      <div class="paper" style="font-size:11px;padding:8px 10px">SVC-20261016-001  16/10
Budi Santoso  0812-3456-7890
HU12345678  HEAD UNIT 9" AVIX</div>
      <div class="hint">Ditempel di unit supaya tidak tertukar di gudang atau di pabrik.</div></div>
    <div class="note ok"><b>Sesuai nota manual:</b> ID service, tanggal, nama, telepon, mobil, merek, tipe, nomor seri, kerusakan, kelengkapan, biaya, note. <b>Tambahan:</b> nota asal, garansi, kondisi fisik, syarat, tanda tangan.</div>
    <div class="note warn"><b>Biaya belum final.</b> Surat terima tidak pernah mencetak angka biaya; estimasi baru ada setelah diagnosis dan persetujuan customer.</div>
  </div>
</div>"""
    return "05_Tanda_Terima_Service", page("Tanda Terima Service", "SVC-20261016-001 · pratinjau 80 kolom persis hasil cetak LX-310", body)


SCREENS = [s01, s02, s03, s04, s05]
