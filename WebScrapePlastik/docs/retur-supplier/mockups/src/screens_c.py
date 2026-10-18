"""Mockups 11-14: barang pengganti, refund / nota kredit, cetak nota (LX-310), integrasi stok."""
from shell import page
from screens_b import modal_page, RESI


def s11():
    m = """<div class="mh"><div><h2>Terima Barang Pengganti</h2><div class="tiny muted">RTS-20261018-001 · RMA-AJ-1018-07 · supplier ASTA JAYA</div></div><span class="btn sm ghost">✕</span></div>
<div class="mb">
 <div class="grid g4">
  <div class="field"><label>Tanggal terima <span class="req">*</span></label><div class="input">25/10/2026</div></div>
  <div class="field"><label>No. surat jalan supplier</label><div class="input mono">SJ-AJ-2610-233</div></div>
  <div class="field"><label>Datang lewat</label><div class="input sel">Diantar sales</div></div>
  <div class="field"><label>Petugas penerima</label><div class="input ro">andi</div></div>
 </div>
 <table class="tbl"><thead><tr><th>Barang yang diretur</th><th class="num">Disetujui ganti</th><th class="num">Sudah diterima</th><th style="width:250px">Barang yang datang</th><th class="num" style="width:72px">Qty</th><th style="width:140px">Kondisi</th><th style="width:110px">Rak</th></tr></thead><tbody>
  <tr class="hl"><td><b>AUTOVISION BI-LED FOGLAMP 3"</b><div class="sub">SN lama AVF3-2409-11872</div></td><td class="num">1</td><td class="num">0</td>
   <td><span class="chk radio on"><i></i>Barang yang sama</span><br><span class="chk radio"><i></i>Barang lain / spesifikasi beda</span><div class="input mono" style="margin-top:5px">SN baru AVF3-2410-00391</div></td>
   <td><div class="input focus" style="justify-content:flex-end">1</div><div class="hint">maks 1</div></td><td><div class="input sel">Baik — diuji menyala</div></td><td><div class="input sel">A-03</div></td></tr>
 </tbody></table>
 <div class="row">
  <div class="card soft grow" style="padding:11px 13px"><div class="label" style="margin-bottom:6px">Yang dibuat otomatis</div>
   <dl class="kv"><dt>Barang Masuk</dt><dd><b class="mono">BM-20261025-001</b> · sumber <span class="tag">retur_pengganti</span> · tertaut ke RTS-20261018-001</dd>
   <dt>Harga pokok</dt><dd>Rp 750.000 (= harga beli unit yang diretur, rata-rata HPP tidak bergeser)</dd>
   <dt>Tagihan supplier</dt><dd>tidak bertambah — pengganti bukan pembelian baru</dd>
   <dt>Ledger</dt><dd class="mono tiny">IN +1 · ref inbound_document BM-20261025-001</dd></dl></div>
  <div class="card soft" style="width:300px;padding:11px 13px"><div class="label" style="margin-bottom:6px">Dampak stok Foglamp 3"</div>
   <table class="tbl"><tbody><tr><td>Fisik</td><td class="num">1 → 2</td></tr><tr><td>Dicadangkan SO</td><td class="num">2</td></tr><tr><td>Bisa dijual</td><td class="num">−1 → 0</td></tr></tbody></table></div>
 </div>
 <div class="note ok"><b>Kekurangan SO-20261015-0003 (RIRIN) tertutup.</b> Draft PO kekurangan untuk Foglamp (1) bisa dibatalkan — sistem menawarkan, tidak membatalkan sendiri.</div>
 <div class="note info">Pengganti boleh datang bertahap. Bila spesifikasi beda dengan harga beli berbeda, selisihnya dicatat sebagai nota kredit (lebih murah) atau tambahan tagihan (lebih mahal) — tidak mengubah faktur lama.</div>
</div>
<div class="mf"><span class="btn">Batal</span><span class="btn pri">Terima &amp; Posting Barang Masuk</span></div>"""
    return modal_page("11_Retur_Penggantian_Barang", "Detail Retur Supplier", "Satu halaman untuk seluruh perjalanan retur.", "keputusan", m, 1000, 56)


def s12():
    m = """<div class="mh"><div><h2>Catat Penyelesaian Keuangan</h2><div class="tiny muted">RTS-20261018-001 · bagian yang disetujui kredit: 1 frame + ongkir</div></div><span class="btn sm ghost">✕</span></div>
<div class="mb">
 <div class="grid g3">
  <div class="choice"><span class="chk radio"><i></i><b>Refund</b></span><div class="tiny muted" style="margin:3px 0 0 20px">Supplier mengembalikan uang. Boleh diterima bertahap.</div></div>
  <div class="choice on"><span class="chk radio on"><i></i><b>Nota Kredit — potong tagihan</b></span><div class="tiny muted" style="margin:3px 0 0 20px">Mengurangi sisa faktur yang belum lunas.</div></div>
  <div class="choice"><span class="chk radio"><i></i><b>Saldo kredit supplier</b></span><div class="tiny muted" style="margin:3px 0 0 20px">Disimpan, dipakai di faktur berikutnya.</div></div>
 </div>
 <div class="row">
  <div class="grow" style="display:flex;flex-direction:column;gap:10px">
   <div class="grid g3">
    <div class="field"><label>No. Nota Kredit</label><div class="input ro mono">CN-SUP-20261023-001</div></div>
    <div class="field"><label>No. nota kredit supplier</label><div class="input mono">AJ/CN/2610/018</div></div>
    <div class="field"><label>Tanggal <span class="req">*</span></label><div class="input">23/10/2026</div></div>
    <div class="field"><label>Nilai <span class="req">*</span></label><div class="input focus">Rp 343.000</div><div class="hint">maks Rp 343.000 (disetujui − sudah dikredit − sudah direfund)</div></div>
    <div class="field span2"><label>Bukti</label><div class="photos"><div class="photo">PDF</div><div class="photo add">+</div></div></div>
   </div>
   <div class="field"><label>Alokasi ke tagihan yang belum lunas</label>
    <table class="tbl"><thead><tr><th>Faktur</th><th class="num">Total</th><th class="num">Sudah dibayar</th><th class="num">Sisa</th><th class="num" style="width:130px">Dipotong</th></tr></thead><tbody>
     <tr class="hl"><td class="mono">FP-2026-0041 · AJ/INV/2610/0412</td><td class="num">Rp 5.000.000</td><td class="num">Rp 1.500.000</td><td class="num">Rp 3.500.000</td><td><div class="input" style="justify-content:flex-end">Rp 343.000</div></td></tr>
     <tr><td class="mono">FP-2026-0047 · AJ/INV/2610/0455</td><td class="num">Rp 2.200.000</td><td class="num">Rp 0</td><td class="num">Rp 2.200.000</td><td><div class="input ph" style="justify-content:flex-end">0</div></td></tr></tbody></table>
    <div class="hint">Faktur tidak diedit. Potongan disimpan sebagai alokasi, sehingga sisa FP-2026-0041 menjadi Rp 3.157.000 dan riwayatnya tetap bisa ditelusuri.</div></div>
  </div>
  <div style="width:300px;display:flex;flex-direction:column;gap:10px">
   <div class="card soft" style="padding:11px 13px"><div class="label" style="margin-bottom:6px">Klaim keuangan retur ini</div>
    <dl class="kv" style="grid-template-columns:120px 1fr"><dt>Disetujui kredit</dt><dd class="num">Rp 343.000</dd><dt>Sudah dikredit</dt><dd class="num">Rp 0</dd><dt>Sudah direfund</dt><dd class="num">Rp 0</dd><dt class="strong">Sisa</dt><dd class="num strong">Rp 343.000</dd></dl></div>
   <div class="card" style="padding:11px 13px"><div class="label" style="margin-bottom:6px">Bila "Refund" dipilih</div>
    <table class="tbl"><thead><tr><th>Tanggal</th><th>Metode</th><th class="num">Diterima</th></tr></thead><tbody>
     <tr><td>24/10</td><td>Transfer BCA</td><td class="num">Rp 200.000</td></tr><tr><td>31/10</td><td>Tunai</td><td class="num">Rp 143.000</td></tr></tbody></table>
    <div class="hint">Nilai disetujui ≠ uang diterima. Status "Refund diterima penuh" baru muncul saat jumlahnya cocok.</div></div>
  </div>
 </div>
</div>
<div class="mf"><span class="tiny muted" style="margin-right:auto">Hanya admin. Bisa dibatalkan dengan alasan (membuat pembalik, bukan menghapus).</span><span class="btn">Batal</span><span class="btn pri">Simpan Nota Kredit</span></div>"""
    return modal_page("12_Retur_Refund_Credit_Note", "Detail Retur Supplier", "Satu halaman untuk seluruh perjalanan retur.", "keputusan", m, 1060, 56)


NOTA = f"""SINAR GALAKSI VARIASI MOBIL                         NOTA RETUR SUPPLIER
Jl. ............................ Telp. ..........  No.  : RTS-20261018-001
                                                    Tgl. : 18/10/2026
Kepada : ASTA JAYA                                  PO   : PO-SUP-20261010-001
         Gudang Retur, Jakarta Utara                BM   : BM-20261012-002
Up.    : Bpk. Hendra  0812 8899 1020                Fakt.: AJ/INV/2610/0412
--------------------------------------------------------------------------------
No Nama Barang                     Qty Sat  Alasan               Nilai (Rp)
--------------------------------------------------------------------------------
 1 AUTOVISION BI-LED FOGLAMP 3"      1 PCS  Tidak berfungsi         750.000
   SN AVF3-2409-11872 - lampu kiri tidak menyala, kabel/soket sudah diganti
 2 FRAME HEAD UNIT AVANZA 2022 9"    2 PCS  Salah model/tipe        650.000
   Datang tipe 2019 7", kondisi baik belum dipakai
--------------------------------------------------------------------------------
                                              Total 3 unit        1.400.000
Diminta : Ganti barang     Kirim: JNE REG  Resi: {RESI}
Koli    : 1 (2,6 kg)       Ongkir Rp 18.000 ditanggung supplier

Catatan: Mohon konfirmasi penerimaan & keputusan retur ke 08xx-xxxx-xxxx.

   Dibuat oleh,              Disetujui,                 Diterima supplier,


  (   andi   )          ( sinargalaksi88 )          (......................)
                                                     Tgl:"""

BST = """SINAR GALAKSI VARIASI MOBIL                   BUKTI SERAH TERIMA BARANG RETUR
                                              No.  : BST-RTS-20261017-001
                                              Retur: RTS-20261017-001
Supplier : MITRA LAMPU JAYA
Diambil  : Bpk. Rudi Hartono (sales)  HP 0813 1122 4455
Tanggal  : 19/10/2026  Jam 14:20
--------------------------------------------------------------------------------
No Nama Barang                     Qty Sat  Kemasan / Kelengkapan
 1 HEADLAMP ASSY INNOVA REBORN       1 PCS  Dus original + bubble wrap + foam
                                            + kardus tebal, 1 koli
--------------------------------------------------------------------------------
Barang di atas diterima dalam keadaan terkemas sesuai daftar.

   Diserahkan (toko),                     Diterima (sales supplier),


  (......................)                (......................)"""


def s13():
    docs = [("Nota Retur Supplier", True), ("Packing Note", False), ("Bukti Serah Terima (diambil sales)", False), ("Label Koli", False)]
    dl = "".join(f'<span class="chk radio {"on" if on else ""}" style="display:flex;margin:5px 0"><i></i>{d}</span>' for d, on in docs)
    body = f"""<div class="between"><div class="row" style="align-items:center;gap:10px"><span class="btn sm">← RTS-20261018-001</span><h2 class="serif" style="margin:0;font-size:18px">Cetak Dokumen Retur</h2><span class="tag">Epson LX-310 · dot matrix</span></div>
<div class="row" style="gap:8px"><span class="btn">Unduh PDF</span><span class="btn">Cetak Kalibrasi</span><span class="btn pri">Cetak ke LX-310</span></div></div>
<div class="row">
 <div style="width:300px;display:flex;flex-direction:column;gap:12px">
  <div class="card"><h3>Dokumen</h3>{dl}</div>
  <div class="card"><h3>Kertas &amp; printer</h3>
   <div class="field"><label>Ukuran kertas</label><div class="input sel">Continuous form 9,5" × 11" <span class="tag">perlu konfirmasi</span></div><div class="hint">Atau ½ halaman (9,5" × 5,5"). Ditentukan setelah toko memastikan kertas yang dipakai.</div></div>
   <div class="field" style="margin-top:8px"><label>Lebar baris</label><div class="input sel">80 kolom · 10 cpi</div><div class="hint">96 kolom (12 cpi) bila pakai kertas ½.</div></div>
   <div class="grid g2" style="margin-top:8px">
    <div class="field"><label>Geser kiri (kolom)</label><div class="input">0</div></div>
    <div class="field"><label>Geser atas (baris)</label><div class="input">1</div></div>
    <div class="field"><label>Rangkap</label><div class="input sel">3 ply (NCR)</div></div>
    <div class="field"><label>Salinan</label><div class="input">1</div></div></div>
   <div class="hint" style="margin-top:6px">Ply 1 supplier · ply 2 arsip toko · ply 3 ikut paket.</div></div>
  <div class="note info">Dicetak sebagai teks polos (font bawaan printer), bukan gambar — cepat, tajam, dan tetap terbaca di rangkap ke-3.</div>
 </div>
 <div class="grow" style="display:flex;flex-direction:column;gap:12px;align-items:flex-start">
  <div class="tiny muted">Pratinjau 80 kolom — Nota Retur Supplier</div>
  <div class="paper" style="font-size:11.4px">{NOTA}</div>
  <div class="tiny muted">Contoh dokumen lain — Bukti Serah Terima (metode Diambil Sales)</div>
  <div class="paper" style="font-size:11.4px">{BST}</div>
 </div>
</div>"""
    return "13_Cetak_Nota_Retur", page("Cetak Nota Retur", "Pratinjau cetak teks untuk printer dot matrix Epson LX-310.", body)


def s14():
    kp = [("Stok fisik", "2", "di toko, termasuk karantina", ""), ("Dicadangkan SO", "2", "SO-20261015-0003 · RIRIN", ""),
          ("Karantina", "1", "RTS-20261018-001 · menunggu keluar", "color:var(--pending)"),
          ("Bisa dijual", "0", "Kasir tidak bisa menjual", "color:var(--failed)"),
          ("Kekurangan pesanan", "1", "draft PO kekurangan dibuat", "color:var(--failed)")]
    k = "".join(f'<div class="kpi"><div class="label">{a}</div><div class="v" style="{s}">{b}</div><div class="d">{c}</div></div>' for a, b, c, s in kp)
    led = [("12/10 14:05", "IN", "+2", "2", "0", "Barang Masuk", "BM-20261012-002", "posting Terima Barang PO-SUP-20261010-001", ""),
           ("15/10 11:20", "—", "", "2", "0", "Pencadangan", "SO-20261015-0003", "dicadangkan 2 untuk RIRIN (bukan baris ledger, dihitung)", "muted"),
           ("18/10 10:05", "QUAR", "1", "2", "1", "Retur supplier", "RTS-20261018-001", "disetujui admin · fisik tetap, bisa dijual turun", ""),
           ("19/10 10:15", "OUT", "−1", "1", "0", "Retur supplier", "RTS-20261018-001", f"barang keluar · JNE {RESI} · karantina ikut turun", "plan"),
           ("25/10 09:40", "IN", "+1", "2", "0", "Barang Masuk", "BM-20261025-001", "pengganti RTS-20261018-001 · HPP Rp 750.000", "plan")]
    rows = []
    for t, ty, q, bal, qbal, src, ref, note, cls in led:
        st = ' style="color:#A9A79F;font-style:italic"' if cls == "plan" else (' class="muted"' if cls == "muted" else "")
        pill = {"IN": "p-green", "OUT": "p-red", "QUAR": "p-amber"}.get(ty)
        tyh = f'<span class="pill no {pill}">{ty}</span>' if pill and cls != "plan" else ty
        rows.append(f'<tr{st}><td>{t}</td><td>{tyh}</td><td class="num">{q}</td><td class="num">{bal}</td><td class="num">{qbal}</td><td>{src}</td><td class="mono">{ref}</td><td class="small">{note}</td></tr>')
    q = [("AUTOVISION BI-LED FOGLAMP 3\"", "1", "Retur RTS-20261018-001", '<span class="pill p-amber">Siap dikirim</span>', ""),
         ("FRAME HEAD UNIT AVANZA 2022 9\"", "2", "Retur RTS-20261018-001", '<span class="pill p-amber">Siap dikirim</span>', ""),
         ("HEADLAMP ASSY INNOVA REBORN", "1", "Retur RTS-20261017-001", '<span class="pill p-teal">Menunggu sales</span>', ""),
         ("STOPLAMP ASSY BRIO 2019", "1", "Rusak saat diterima · BM-20261016-001", '<span class="pill p-grey">Belum diretur</span>', "Buat retur"),
         ("KAMERA MUNDUR AHD 170°", "1", "Retur pelanggan · INV-20261014-0021", '<span class="pill p-grey">Kandidat</span>', "Buat retur")]
    def act(e):
        return f'<div style="margin-top:4px"><span class="btn sm">{e}</span></div>' if e else ""
    qr = "".join(f'<tr><td>{a}<div class="sub">{c}</div></td><td class="num">{b}</td><td>{d}{act(e)}</td></tr>' for a, b, c, d, e in q)
    body = f"""<div class="between"><div class="row" style="align-items:center;gap:10px"><span class="btn sm">← Master Barang</span><h2 class="serif" style="margin:0;font-size:18px">AUTOVISION BI-LED FOGLAMP 3"</h2><span class="tag mono">AV-FL3-BL</span><span class="tag">PCS</span><span class="tag">HPP rata-rata Rp 750.000</span></div>
<span class="tiny muted">Keadaan per 18/10/2026 10:05 — setelah RTS-20261018-001 disetujui</span></div>
<div class="grid g5">{k}</div>
<div class="note warn"><b>Rumus:</b> Bisa dijual = Stok fisik − Dicadangkan SO − Karantina = 2 − 2 − 1 = <b>−1</b> → ditampilkan 0 dan muncul sebagai kekurangan 1 di SO RIRIN. Unit karantina tidak dihitung dua kali: pencadangan SO hanya boleh memakai stok fisik di luar karantina.</div>
<div class="row">
 <div class="grow" style="display:flex;flex-direction:column;gap:10px">
  <div class="between"><h3 class="serif" style="margin:0;font-size:14.5px">Kartu stok (stock_ledger)</h3><span class="tiny muted">baris abu-abu miring = akan terjadi di langkah berikutnya</span></div>
  <div class="tblwrap"><table class="tbl"><thead><tr><th>Waktu</th><th>Tipe</th><th class="num">Qty</th><th class="num">Fisik</th><th class="num">Karantina</th><th>Sumber</th><th>Referensi</th><th>Keterangan</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
  <div class="grid g3">
   <div class="card soft"><h3>Kasir</h3><div class="small">Foglamp tampil <span class="pill p-red">0 bisa dijual</span>. Menjual tetap butuh override admin + alasan (aturan K-02).</div></div>
   <div class="card soft"><h3>Stock Opname</h3><div class="small">Hitung fisik mencakup rak karantina; selisih dibandingkan dengan <b>stok fisik</b>, bukan "bisa dijual".</div></div>
   <div class="card soft"><h3>Laporan nilai stok</h3><div class="small">Karantina tetap aset sampai keluar. Ditampilkan terpisah: "nilai stok karantina" — barang ini Rp 750.000.</div></div>
  </div>
 </div>
 <div class="card" style="width:430px"><h3>Stok Karantina <small>semua barang · 6 unit · Rp 4.480.000</small></h3>
  <table class="tbl"><thead><tr><th>Barang / asal</th><th class="num">Qty</th><th>Status</th></tr></thead><tbody>{qr}</tbody></table>
  <div class="hint" style="margin-top:8px">Masuk karantina dari: retur disetujui, Terima Barang dengan kondisi rusak / tidak sesuai, retur pelanggan tujuan "supplier"/"rusak", dan barang retur yang ditolak lalu kembali.</div>
  <div class="row" style="gap:6px;margin-top:10px"><span class="btn sm">Lepas ke stok jual…</span><span class="btn sm danger">Hapus buku…</span></div></div>
</div>"""
    return "14_Integrasi_Retur_Inventory", page("Master Barang", "Detail barang: bagaimana retur supplier mengubah stok fisik, karantina, dan stok yang bisa dijual.", body, active="Master Barang")


SCREENS = [s11, s12, s13, s14]
