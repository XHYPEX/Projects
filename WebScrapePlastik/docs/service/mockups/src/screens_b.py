"""Mockups 06-11: penanganan awal, kirim ekspedisi, serah sales, detail & timeline, hasil pabrik, persetujuan biaya."""
from shell import page, steps, doc_header, field, chk, choice

HU = 'HEAD UNIT ANDROID 9" AVIX AX-9'


def s06():
    body = f"""{steps(3)}
<div class="card"><h3>Analisis Awal &amp; Jalur Penanganan</h3>
  <dl class="kv" style="grid-template-columns:150px 1fr"><dt>Dokumen</dt><dd><b class="mono">SVC-20261016-001</b> <span class="muted small">(nomor dikunci saat disimpan)</span></dd>
  <dt>Customer / Barang</dt><dd>Budi Santoso · {HU} · SN HU12345678</dd><dt>Keluhan</dt><dd>Unit tidak menyala, layar hitam setelah diberi tegangan</dd>
  <dt>Garansi</dt><dd><span class="pill p-green">Aktif (indikasi)</span> s/d 10/08/2027</dd>
  <dt>Keputusan akhir</dt><dd><span class="pill p-amber">Belum ditentukan oleh pabrik</span></dd></dl>
  <div class="grid g4" style="margin-top:12px">
    {choice("⌕ Pemeriksaan Internal", "Teknisi toko menguji dulu. Hasil: tidak ada kerusakan, diperbaiki di toko, atau perlu kirim pabrik.")}
    {choice("▣ Kirim ke Pabrik / Principal", "Dikemas lalu dikirim lewat ekspedisi (udara / laut / darat).", True)}
    {choice("⇄ Dibawa Sales Supplier", "Diserahkan ke sales yang datang ke toko; tanpa resi, dengan tanda tangan.")}
    {choice("◷ Menunggu Keputusan", "Unit disimpan dulu, mis. menunggu supplier membalas. Muncul di Perlu tindakan.")}
  </div>
  <div class="note warn" style="margin-top:12px"><b>Keputusan SERVICE atau GANTI BARU belum dipilih di sini.</b> Langkah ini hanya memilih jalur pemeriksaan; keputusan dicatat dari diagnosis tertulis supplier / principal.</div>
</div>
<div class="row">
  <div class="grow card"><h3>Tujuan Pemeriksaan</h3>
    <div class="grid g2">
      {field("Supplier / Principal", "ASTA JAYA — Divisi Service", "sel", req=True, hint="default Master Service: AVIX → ASTA JAYA")}
      {field("PIC / Kontak", "Bpk. Hendra · 0812 8899 1020")}
      {field("Alamat", "Jl. ............, Jakarta Utara", "ro", hint="wajib ada sebelum Konfirmasi Serah Ekspedisi", span="span2")}
    </div></div>
  <div class="grow card"><h3>Catatan Penanganan</h3>
    {field("Catatan teknisi", "Kabel dan tegangan sudah dicek di toko (12,4 V, ACC normal). Unit tetap mati → kirim pabrik.", "area")}
    <div class="hint">Tercetak di Surat Jalan Service sebagai catatan ke pabrik.</div></div>
</div>
<div class="between"><span class="btn">← Kembali</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Lanjut: Review &amp; Simpan →</span></div></div>"""
    return "06_Keputusan_Penanganan_Awal", page("Terima Service Baru", "Langkah 3 dari 4 · memilih jalur pemeriksaan, bukan keputusan perbaikan", body)


def s07():
    acc = [("Head unit", True, True), ("Kabel set", True, True), ("Box / Dus", True, False)]
    rows = "".join(f'<tr><td>{n}</td><td>{chk("", b)}</td><td>{chk("ikut", s)}</td><td class="small muted">{"" if s else "ditahan di toko"}</td></tr>' for n, b, s in acc)
    body = f"""{doc_header("SVC-20261016-001", "Menunggu Dikirim", "p-amber")}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Pengiriman ke Pabrik / Principal</h3>
    <div class="grid g3">
      <div class="field"><label>Jalur Kirim <span class="req">*</span></label><div class="row" style="gap:14px;padding-top:6px">{chk("Udara", True, True)}{chk("Laut", False, True)}{chk("Darat / kurir", False, True)}</div></div>
      {field("Ekspedisi", "JNE · YES", "sel")}
      {field("Supplier / Tujuan", "ASTA JAYA — Divisi Service", "ro")}
      {field("Nomor Resi / AWB", "0108 2410 1977 5521", "mono focus", req=True, hint="wajib saat konfirmasi · belum pernah dipakai")}
      {field("Tanggal Kirim", "17/10/2026")}
      {field("Jumlah Koli / Berat", "1 koli · 2,1 kg")}
      {field("Ongkir Keluar", "Rp 45.000")}
      {field("Ditanggung", "Toko (garansi)", "sel", hint="K-S5: garansi → toko; non-garansi → customer")}
      {field("Alamat Tujuan", "Jl. ............, Jakarta Utara", "ro")}
    </div>
    <div class="note warn" style="margin-top:12px"><b>Status fisik barang.</b> Mengisi form tidak berarti barang sudah keluar. Lokasi baru berubah ke <b>Di Ekspedisi</b> setelah <b>Konfirmasi Serah Ekspedisi</b>.</div></div>
  <div class="card"><h3>Lampiran Bukti Pengiriman</h3>
    <div class="grid g3"><div class="drop">✓ Foto kemasan<br><span class="tiny">PK_001.jpg</span></div><div class="drop">+ Resi / bukti serah ekspedisi <span class="req">*</span></div><div class="drop">✓ Foto isi paket<br><span class="tiny">PK_002.jpg</span></div></div></div>
  </div>
  <div style="width:420px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Packing Note</h3>
      <div class="grid g2">{field("Kemasan", "Kardus luar")}{field("Pelindung", "Bubble wrap + foam")}</div>
      <div style="height:8px"></div>{field("Isi paket", "1 PCS Head Unit Android 9” AVIX; kabel set", "ro", hint="otomatis dari kelengkapan yang ikut")}
      <div style="height:8px"></div>{field("Catatan packing", "Unit dibungkus bubble wrap, sudut diberi foam, kabel dalam pouch terpisah. Label unit ditempel.", "area")}</div>
    <div class="card"><h3>Kelengkapan: ikut atau ditahan</h3>
      <table class="tbl"><thead><tr><th>Item</th><th>Dibawa</th><th>Kirim</th><th></th></tr></thead><tbody>{rows}</tbody></table>
      <div class="hint">Yang ditahan tetap tercatat di toko dan dicek saat serah ke customer.</div></div>
    <div class="row" style="gap:8px"><span class="btn">Cetak Surat Jalan Service</span><span class="btn">Cetak Label Koli</span></div>
  </div>
</div>
<div class="between"><span class="small muted">Surat jalan tidak mencantumkan HP customer.</span><div class="row" style="gap:8px"><span class="btn">Simpan Draft Pengiriman</span><span class="btn pri">Konfirmasi Serah Ekspedisi</span></div></div>"""
    return "07_Pengiriman_ke_Pabrik", page("Kirim ke Pabrik / Principal", "Jalur udara / laut / darat, resi, packing, ongkir, dan bukti barang keluar", body)


def s08():
    body = f"""{doc_header("SVC-20261013-001", "Menunggu Diambil Sales", "p-amber").replace("Budi Santoso · 0812-3456-7890", "Maya Sari · 0821-3344-5566").replace('HEAD UNIT ANDROID 9" · SN HU12345678', 'HEAD UNIT ANDROID 10" · SN HU10-77812').replace("PJSG/26/08/0125", "PJSG/26/05/0087")}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Jadwal Pengambilan Sales Supplier</h3>
    <div class="grid g3">
      {field("Supplier / Principal", "ASTA JAYA", "sel")}
      {field("Nama Sales Pengambil", "Bpk. Rudi Hartono", req=True)}
      {field("No. HP Sales", "0813-1122-4455", req=True)}
      {field("Jadwal Rencana", "18/10/2026 · 14:00")}
      {field("Tanggal / Jam Aktual", "18/10/2026 · 14:20", "focus", req=True)}
      {field("Petugas Toko", "andi (login)", "ro")}
    </div>
    <div class="note ok" style="margin-top:12px"><b>Jalur tanpa resi.</b> Nomor AWB tidak wajib. Buktinya tanda tangan sales <b>atau</b> foto serah terima. Jadwal lewat 1 hari tanpa konfirmasi → tanda Bermasalah.</div></div>
  <div class="card"><h3>Bukti Pengambilan Aktual</h3>
    <div class="grid g2"><div class="sig">✎ Tanda tangan sales (di layar) — Rudi Hartono</div><div class="drop" style="height:64px;display:grid;place-items:center">✓ Foto serah terima barang · ST_001.jpg</div></div></div>
  </div>
  <div style="width:420px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Ringkasan Barang &amp; Paket</h3>
      <dl class="kv"><dt>Customer</dt><dd>Maya Sari</dd><dt>Nota</dt><dd class="mono">PJSG/26/05/0087</dd><dt>Barang</dt><dd>HEAD UNIT ANDROID 10"</dd>
      <dt>SN</dt><dd class="mono">HU10-77812</dd><dt>Garansi</dt><dd><span class="pill p-green">Aktif</span></dd><dt>Dibawa sales</dt><dd>Unit, kabel set, frame</dd>
      <dt>Ditahan di toko</dt><dd>Box / dus</dd><dt>Packing</dt><dd>Dus original + bubble wrap</dd></dl></div>
    <div class="note warn"><b>Jangan kurangi inventori.</b> Ini barang titipan customer. Yang berubah hanya lokasi / penanggung jawab titipan, bukan stok produk.</div>
    <span class="btn" style="align-self:flex-start">Cetak Bukti Serah Terima</span>
  </div>
</div>
<div class="between"><span></span><div class="row" style="gap:8px"><span class="btn">Simpan Jadwal</span><span class="btn pri">Konfirmasi Sudah Diambil Sales</span></div></div>"""
    return "08_Serah_Terima_Sales_Supplier", page("Serah ke Sales Supplier", "Jalur alternatif: sales supplier mengambil unit langsung dari toko", body)


def s09():
    stats = [("Tahap", "Di Supplier", "p-blue"), ("Lokasi Unit", "Di Ekspedisi", "p-blue"), ("Garansi", "Aktif (indikasi)", "p-green"),
             ("Biaya", "Menunggu Estimasi", "p-grey"), ("Hasil", "Belum Diputuskan", "p-grey")]
    st = "".join(f'<div class="stat"><div class="label">{a}</div><span class="pill {c}">{b}</span></div>' for a, b, c in stats)
    ev = [("16/10/2026 10:30", "Diterima di toko", "andi", "Tanda Terima"), ("16/10/2026 10:41", "Foto & kelengkapan dicatat", "andi", "3 foto"),
          ("16/10/2026 10:44", "Penanganan: kirim ke pabrik (ASTA JAYA)", "andi", "—"), ("17/10/2026 09:15", "Packing selesai", "andi", "Packing note · 2 foto"),
          ("17/10/2026 14:00", "Diserahkan ke ekspedisi · udara JNE YES", "andi", "SJS-20261017-001 · resi 0108…5521"),
          ("—", "Pabrik menerima unit", "menunggu", "—"), ("—", "Hasil pemeriksaan pabrik", "menunggu", "—")]
    trs = "".join(f'<tr><td class="small" style="white-space:nowrap">{a}</td><td>{b}</td><td class="small">{c}</td><td>{"<span class=tag>" + d + "</span>" if d != "—" else "<span class=muted>—</span>"}</td></tr>' for a, b, c, d in ev)
    tl = [("Barang diterima", "16 Okt 2026 · penerimaan lengkap", ""), ("Penanganan awal", "16 Okt · kirim ke pabrik", ""),
          ("Dikirim", "17 Okt · udara, JNE YES", ""), ("Pabrik menerima", "belum dikonfirmasi", "now"), ("Pemeriksaan pabrik", "belum ada hasil", "todo"),
          ("Persetujuan biaya", "menunggu diagnosis", "todo"), ("Kembali ke toko", "belum", "todo"), ("Serah ke customer", "belum", "todo")]
    tls = "".join(f'<div class="ev {c}"><b>{a}</b><div class="t">{b}</div></div>' for a, b, c in tl)
    body = f"""{doc_header("SVC-20261016-001", "Di Supplier", "p-blue", '<span class="btn">Cetak ▾</span><span class="btn pri">Catat Pabrik Menerima</span>')}
<div class="stat5">{st}</div>
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Ringkasan</h3>
    <div class="grid g2" style="gap:6px 24px">
      <dl class="kv small" style="grid-template-columns:90px 1fr"><dt>Customer</dt><dd>Budi Santoso · 0812-3456-7890</dd><dt>Mobil</dt><dd>Toyota Innova Reborn · B 1456 WZU</dd><dt>Nota asal</dt><dd class="mono">PJSG/26/08/0125 · beli 10/08/2026</dd><dt>Garansi s/d</dt><dd>10/08/2027</dd></dl>
      <dl class="kv small" style="grid-template-columns:90px 1fr"><dt>Barang</dt><dd>HEAD UNIT ANDROID 9" AVIX · SN HU12345678</dd><dt>Keluhan</dt><dd>Mati, layar hitam</dd><dt>Tujuan</dt><dd>ASTA JAYA — Divisi Service</dd><dt>Umur</dt><dd>3 hari · estimasi 14–21 hari</dd></dl>
    </div></div>
  <div class="tblwrap"><table class="tbl"><thead><tr><th>Tanggal</th><th>Peristiwa</th><th>Oleh</th><th>Bukti / Referensi</th></tr></thead><tbody>{trs}</tbody></table></div>
  <div class="card soft"><h3 style="font-size:13.5px">Dokumen terkait</h3>
    <div class="row" style="gap:8px;flex-wrap:wrap"><span class="tag">Nota asal PJSG/26/08/0125</span><span class="tag">Tanda Terima SVC-20261016-001</span><span class="tag">Surat Jalan SJS-20261017-001</span><span class="tag">Nota Jasa: belum ada</span><span class="tag">Retur Supplier: tidak ada (unit customer)</span></div></div>
  </div>
  <div style="width:340px" class="card"><h3>Timeline Service</h3><div class="tl">{tls}</div>
    <div class="divider" style="margin:12px 0"></div>
    <div class="row" style="gap:6px;flex-wrap:wrap"><span class="btn sm">Catat Update Pabrik</span><span class="btn sm">Hubungi Customer (WA)</span><span class="btn sm">Cetak Riwayat</span></div></div>
</div>"""
    return "09_Detail_Service_Timeline", page("Detail Service", "Lima jalur status, dokumen terkait, dan jejak perpindahan unit customer", body)


def s10():
    body = f"""{doc_header("SVC-20261016-001", "Di Supplier", "p-blue", '<span class="pill p-teal">Lokasi: Di Pabrik sejak 19/10</span>')}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Hasil Pemeriksaan dari Pabrik</h3>
    <div class="grid g3">
      {field("Referensi RMA Pabrik", "RMA-ASTA-1088", "mono")}
      {field("Tanggal Diterima Pabrik", "19/10/2026")}
      {field("Tanggal Diagnosis", "21/10/2026", req=True)}
      {field("Pemeriksa / PIC", "Teknisi ASTA JAYA · Bpk. Hendra")}
      {field("Garansi menurut Pabrik", "Tidak ditanggung", "sel", req=True)}
      {field("Alasan", "Kerusakan akibat tegangan berlebih (bukan cacat produksi)", "sel")}
    </div>
    <div style="height:10px"></div>{field("Uraian Diagnosis", "Modul power mengalami kerusakan. Perlu penggantian board power. Pabrik mengirim estimasi biaya.", "area", req=True)}
    <div class="note ok" style="margin-top:12px"><b>Keputusan final berasal dari hasil pabrik.</b> Admin tidak menentukan service / ganti baru sebelum ada diagnosis. Simpan surat diagnosis atau bukti komunikasi supplier.</div></div>
  <div class="card"><h3>Estimasi Biaya dari Pabrik <small>modal · hanya admin</small></h3>
    <table class="tbl"><thead><tr><th>Jenis</th><th>Keterangan</th><th class="num">Biaya pabrik</th></tr></thead><tbody>
    <tr><td>Sparepart</td><td>Board power AX-9</td><td class="num">Rp 250.000</td></tr><tr><td>Service</td><td>Jasa pabrik</td><td class="num">Rp 50.000</td></tr>
    <tr><td>Ongkir</td><td>Kirim balik (ditanggung pabrik)</td><td class="num">Rp 0</td></tr></tbody></table>
    <div class="between" style="margin-top:8px"><span class="small muted">Harga ke customer diisi di Persetujuan Biaya (staff hanya melihat harga customer).</span><b>Total Rp 300.000</b></div></div>
  <div class="card"><h3>Bukti Diagnosis</h3>
    <div class="grid g3"><div class="drop">✓ Surat diagnosis pabrik<br><span class="tiny">RMA-1088.pdf</span></div><div class="drop">✓ Foto kerusakan<br><span class="tiny">DG_001.jpg</span></div><div class="drop">+ Bukti penawaran biaya</div></div></div>
  </div>
  <div style="width:380px;display:flex;flex-direction:column;gap:12px">
    <div class="card"><h3>Keputusan Pabrik</h3>
      <div style="display:flex;flex-direction:column;gap:8px">
      {choice("✓ Bisa Diperbaiki (gratis)", "Ditanggung garansi; tidak ada biaya ke customer.")}
      {choice("🔧 Bisa Diperbaiki dengan Biaya", "Perlu sparepart / jasa berbayar → persetujuan customer.", True)}
      {choice("↻ Ganti Unit Baru", "Supplier menyetujui penggantian unit → alur penggantian.")}
      {choice("✕ Ditolak / Tidak Bisa Diperbaiki", "Alasan wajib → alur penolakan, unit dikembalikan.")}
      </div></div>
    <div class="note warn"><b>Garansi menjadi Ditolak.</b> Karena pabrik menyatakan tidak ditanggung, status Garansi berubah dari Aktif (indikasi) menjadi <b>Garansi Ditolak</b>; Biaya → Menunggu Persetujuan Customer.</div>
  </div>
</div>
<div class="between"><span></span><div class="row" style="gap:8px"><span class="btn">Simpan Hasil Diagnosis</span><span class="btn pri">Kirim Permintaan Persetujuan Customer →</span></div></div>"""
    return "10_Hasil_Pemeriksaan_Pabrik", page("Hasil Pemeriksaan Pabrik", "Keputusan dicatat setelah unit tiba dan diperiksa teknisi pabrik", body)


def s11():
    lines = [("Sparepart", "Board power AX-9", "Rp 250.000", "Rp 300.000"), ("Jasa service", "Pekerjaan pabrik", "Rp 50.000", "Rp 50.000"),
             ("Ongkir tambahan", "Kirim balik ditanggung pabrik", "Rp 0", "Rp 0")]
    trs = "".join(f'<tr><td>{a}</td><td>{b}</td><td class="num secret">{c}</td><td class="num"><b>{d}</b></td></tr>' for a, b, c, d in lines)
    wa = ("Halo Bpk. Budi, unit Head Unit AVIX (SVC-20261016-001) sudah diperiksa pabrik: modul power rusak akibat tegangan berlebih "
          "dan tidak ditanggung garansi. Biaya perbaikan Rp 350.000 (board power + jasa). Mohon balas SETUJU atau TOLAK. Terima kasih — Sinar Galaksi")
    body = f"""{doc_header("SVC-20261016-001", "Menunggu Keputusan Customer", "p-amber")}
<div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:14px">
  <div class="card"><h3>Estimasi Biaya Tambahan</h3>
    <dl class="kv" style="grid-template-columns:130px 1fr"><dt>Customer</dt><dd>Budi Santoso · 0812-3456-7890</dd><dt>Barang</dt><dd>{HU}</dd>
    <dt>Hasil pabrik</dt><dd>Board power perlu diganti · garansi ditolak (tegangan berlebih)</dd></dl>
    <table class="tbl" style="margin-top:10px"><thead><tr><th>Jenis Biaya</th><th>Keterangan</th><th class="num">Modal pabrik <span class="tag">admin</span></th><th class="num">Harga ke customer</th></tr></thead><tbody>{trs}</tbody></table>
    <div class="sum" style="margin-top:10px"><span class="muted">Total modal pabrik</span><span class="num secret">Rp 300.000</span>
      <span class="tot">Ditagihkan ke customer</span><span class="tot num">Rp 350.000</span></div></div>
  <div class="card"><h3>Pilih Hasil Keputusan Customer</h3>
    <div class="grid g3">{choice("◷ Menunggu", "Estimasi sudah dikirim, belum dijawab. Bermasalah setelah 7 hari.", True)}{choice("✓ Setuju", "Simpan bukti & tanggal. Pabrik diberi tahu untuk mengerjakan.")}{choice("✕ Tolak", "Alasan wajib. Unit diminta kembali apa adanya.")}</div></div>
  </div>
  <div style="width:420px;display:flex;flex-direction:column;gap:14px">
    <div class="card"><h3>Persetujuan Customer</h3>
      <div class="grid g2">{field("Tanggal dihubungi", "21/10/2026 · 15:10")}{field("Batas jawab", "28/10/2026", hint="7 hari")}</div>
      <div style="height:8px"></div><div class="field"><label>Media</label><div class="row" style="gap:12px;padding-top:4px">{chk("WhatsApp", True, True)}{chk("Telepon", False, True)}{chk("Datang ke toko", False, True)}</div></div>
      <div style="height:8px"></div><div class="drop">+ Bukti: screenshot WA / tanda tangan</div>
      <div style="height:8px"></div>{field("Catatan customer", "Menunggu balasan customer sebelum perbaikan dimulai.", "area")}</div>
    <div class="card soft"><div class="between"><h3 style="font-size:13.5px;margin:0">Pesan WA siap salin</h3><span class="btn sm">Salin Pesan WA</span></div>
      <div class="small" style="margin-top:6px;line-height:1.45">{wa}</div></div>
    <div class="note warn"><b>Batas persetujuan.</b> Jangan perintahkan pabrik memulai pekerjaan berbayar sebelum persetujuan dicatat. Estimasi yang berubah membuat persetujuan baru.</div>
  </div>
</div>
<div class="between"><span></span><div class="row" style="gap:8px"><span class="btn">Simpan Draft</span><span class="btn pri">Catat Persetujuan</span></div></div>"""
    return "11_Persetujuan_Biaya_Customer", page("Persetujuan Biaya Tambahan", "Estimasi muncul setelah diagnosis; jawaban customer dicatat sebelum pekerjaan dimulai", body)


SCREENS = [s06, s07, s08, s09, s10, s11]
