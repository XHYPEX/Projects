# Retur Barang ke Supplier — Desain UI/UX & Workflow

**SINAR GALAKSI VARIASI MOBIL** · dokumen desain, 18/10/2026 · status: **USULAN, BELUM DIIMPLEMENTASIKAN**

Dokumen ini menjawab brief "Redesign UI/UX & Workflow Retur Barang ke Supplier". Isinya hanya desain: 14 mockup, 4 flowchart, aturan stok, aturan keuangan, validasi, usulan database, dan format cetak. Kode aplikasi (`backend/`, `frontend/`) tidak diubah dan modul ini **belum ada di aplikasi**.

Sudah disesuaikan dengan keputusan terbaru: DP minimal Sales Order **30%**, nomor PO baru **PO-SUP-YYYYMMDD-NNN**, nomor Sales Order baru **SO-YYYYMMDD-NNNN**, aturan K-01 (barang SO keluar saat Terpasang), K-02 (pencadangan stok untuk SO), dan K-13 (pembatalan per item; PO yang sudah terbit tidak dibatalkan otomatis).

## 1. Ringkasan

- **Satu dokumen retur, empat jalur status.** Status dokumen, status barang keluar, keputusan supplier, dan penyelesaian dicatat terpisah. Dengan begitu "paket di JNE" tidak tertukar dengan "supplier sudah setuju", dan "supplier setuju" tidak tertukar dengan "uang sudah kembali".
- **Dua metode pengembalian**:
    - **A · Dikirim lewat ekspedisi**: resi wajib.
    - **B · Diambil sales di toko**: tanpa resi; buktinya Bukti Serah Terima yang ditandatangani dan difoto.
- **Stok Karantina.** Barang yang akan diretur langsung tidak bisa dijual sejak retur disetujui. Stok fisik baru berkurang sekali, tepat saat barang diserahkan ke ekspedisi atau ke sales.
- **Keuangan hanya bergerak saat supplier memutuskan.** Bentuknya:
    - nota kredit, dialokasikan ke faktur tanpa mengedit fakturnya;
    - refund, yang boleh diterima bertahap;
    - barang pengganti, masuk sebagai Barang Masuk baru dengan HPP asal.
- **Cetak teks polos untuk Epson LX-310**: Nota Retur, Packing Note, Bukti Serah Terima, dan Label Koli.

## 2. Evaluasi workflow & penyimpangan dari brief

### Yang sudah tepat di brief

- Alasan dicatat per barang, bukan per dokumen.
- Metode ekspedisi dan diambil sales dipisah; resi tidak dipaksakan untuk sales.
- Packing note dengan bahan multi-pilih dan template otomatis.
- Keputusan supplier boleh sebagian, dan penyelesaiannya bisa ganti, refund, atau kredit.
- Larangan stok keluar dua kali, peringatan alokasi Sales Order, dan tautan ke PO, Barang Masuk, dan Faktur.

### Celah di aplikasi sekarang yang ditemukan saat membaca kode (hanya dibaca)

| Temuan | Dampak | Usulan |
|---|---|---|
| Terima Barang (`receive_supplier_po`) memasukkan **semua** qty ke Barang Masuk, termasuk baris berkondisi `rusak` / `tidak_sesuai` | Barang rusak ikut menjadi stok yang bisa dijual di Kasir | Posting tetap IN (stok fisik benar), tetapi baris rusak / tidak sesuai otomatis **QUAR** dan menjadi kandidat retur supplier |
| Retur pelanggan (`receipt_returns`) dengan tujuan `supplier` / `rusak` hanya mengembalikan `restock_qty` ke stok; `supplier_qty` dan `damaged_qty` tidak tercatat di mana pun | Unit fisiknya ada di toko tetapi "hilang" dari sistem; tidak bisa diretur ke supplier dengan rapi | Unit tersebut masuk lewat IN + QUAR sebagai "kandidat retur supplier" (mockup 14) |
| `spo_cancellations.action = 'retur_supplier'` sudah ada tetapi tidak punya tindak lanjut | Barang batal SO yang mau diretur tidak bisa ditelusuri | Tindakan ini membuat draft retur supplier yang tertaut ke pembatalan tersebut |
| Belum ada konsep stok karantina; K-02 menghitung Available = In Hand − Reserved | Barang yang menunggu retur bisa terjual atau dicadangkan untuk SO | Available = In Hand − Reserved − Quarantine. Pool pencadangan memakai In Hand − Quarantine, sehingga tidak ada hitung ganda |

### Penyimpangan dari brief (dan alasannya)

1. **Ada langkah persetujuan admin** (diputuskan, keputusan 2). Staff membuat draft dan mengunggah bukti; admin menyetujui atau menolak. Admin bisa membuat sekaligus menyetujui. Untuk kerugian (hapus stok karantina, jual sebagai second) persetujuan akhir ada pada **Owner**: admin yang diberi tanda Owner di halaman Pengguna (bukan peran baru).
2. **Stok karantina dimulai saat disetujui, bukan saat draft.** Draft sering dibuat sambil menunggu jawaban supplier. Kalau draft sudah mengunci stok, Kasir akan terhambat oleh dokumen yang mungkin batal.
3. **"Dalam Pengiriman" dan "Diterima Supplier" adalah dua status berbeda.** Brief kadang menyamakan "dikirim" dengan "selesai dikirim". Di sini paket di ekspedisi belum dianggap diterima.
4. **Barang yang ditolak dan dikirim balik masuk karantina, bukan langsung ke stok jual.** Kondisinya belum tentu layak dijual; admin yang memutuskan.
5. **Nota kredit tidak mengedit total faktur.** Potongan dicatat sebagai alokasi, sehingga faktur asli tetap utuh untuk audit dan pajak.
6. **Nomor retur RTS-YYYYMMDD-NNN** (diputuskan, keputusan 4), seragam dengan PO-SUP-YYYYMMDD-NNN dan SO-YYYYMMDD-NNNN. Urutan mulai 001 tiap hari; nomor yang dibatalkan tidak dipakai ulang.
7. **Halaman Stok Karantina ditambahkan** di menu Inventori (bukan permintaan brief). Tanpa halaman ini, barang rusak dari Terima Barang dan retur pelanggan tidak punya tempat.

## 3. Model status: empat dimensi

| Dimensi | Nilai | Tingkat | Siapa yang mengubah |
|---|---|---|---|
| **Status Dokumen** | Draft → Menunggu Persetujuan → Diproses → Selesai · atau Dibatalkan | dokumen | staff (ajukan), admin (setujui / batal); Selesai otomatis |
| **Barang Keluar / Serah Terima** | Belum Disiapkan → Siap Dikirim (A) / Menunggu Pengambilan (B) → Dalam Pengiriman (A) / Sudah Diserahkan ke Sales (B) → Diterima Supplier · atau Bermasalah | dokumen (per koli di versi lanjutan) | staff / admin |
| **Keputusan Supplier** | Menunggu · Disetujui · Disetujui Sebagian · Ditolak | per item dan per qty | admin |
| **Penyelesaian** | Ganti: menunggu → diterima sebagian / penuh · Refund: disetujui → diterima sebagian / penuh · Kredit: dicatat → dipakai · Dikembalikan ke toko · Dihapusbukukan | per item dan per qty | admin (staff boleh menerima pengganti) |

Aturan turunan:

- **Selesai** otomatis jika setiap unit sudah mendapat penyelesaian akhir:
    - pengganti diterima;
    - refund diterima penuh;
    - kredit dicatat;
    - barang ditolak sudah kembali dan diputuskan;
    - atau dihapusbukukan.
- **Bermasalah** adalah tanda, bukan akhir. Contohnya paket hilang, sales tidak datang lewat jadwal, atau supplier tidak menjawab lebih dari 14 hari. Tanda ini muncul di KPI dan filter "Perlu tindakan".
- **Dibatalkan** hanya mungkin sebelum barang keluar. Setelah barang keluar, jalurnya adalah "Minta Barang Kembali" lalu "Terima Kembali".

## 4. Penjelasan per Gambar

Empat belas mockup adalah layar desktop 1440 px, satu layar per file, di `mockups/NN_*.png` (sumber HTML ada di `mockups/src/`). Di PDF, setiap penjelasan dicetak di samping gambarnya. Formatnya: Fungsi, Dibuka dari, Field, Aksi, Setelah Disimpan, Catatan Programmer.

### Gambar 01 — Daftar Retur Supplier

#### Fungsi
Pusat kerja retur: semua retur, KPI per tahap, dan yang perlu tindakan.

#### Dibuka dari
Menu baru **Retur Supplier** di sidebar, di antara Purchase Order dan Riwayat Invoice.

#### Field
- Pencarian satu kotak: no. retur, PO, Barang Masuk, supplier, barang/SKU, resi, no. RMA.
- Filter: rentang tanggal, supplier, metode, status dokumen, keputusan, dan penyelesaian.
- Tab cepat: Semua, Perlu tindakan, Aktif, Selesai, Dibatalkan.
- KPI: Draft, Perlu Dikirim/Diambil, Dalam Pengiriman, Menunggu Keputusan, Penyelesaian Berjalan, Bermasalah.

#### Aksi
- Klik baris untuk membuka Detail (08).
- Menu ··· sesuai status: cetak Nota / Bukti Serah Terima, Catat Serah Terima Sales, Ubah Jadwal, Batalkan (admin).
- **+ Buat Retur Supplier**.

#### Setelah Disimpan
Halaman ini hanya membaca. Angka KPI dan nilai retur aktif (harga beli) dihitung ulang.

#### Catatan Programmer
- Kolom status menampilkan dua dimensi sekaligus: status dokumen + metode, dan barang keluar.
- Keputusan dan penyelesaian punya kolom sendiri. Jika item berbeda-beda, ringkas menjadi "Sebagian".
- Pagination di server, karena retur bisa ratusan.

### Gambar 02 — Buat Retur: Informasi Retur

#### Fungsi
Langkah 1 wizard: memilih sumber retur sehingga supplier, PO, faktur, dan harga beli terisi otomatis.

#### Dibuka dari
Tombol + Buat Retur Supplier, atau tombol "Buat retur" di Stok Karantina (14), Barang Masuk, atau pembatalan SO dengan tindakan retur_supplier.

#### Field
- Sumber: Barang Masuk / PO, Stok Karantina, atau Tanpa dokumen (admin, alasan wajib).
- No. retur (otomatis), tanggal, supplier (terkunci dari BM), Barang Masuk, PO, no. faktur supplier.
- Jenis retur: bermasalah / klaim garansi / kelebihan atau salah kirim.
- Penyelesaian yang diminta, no. RMA (opsional), catatan.

#### Aksi
Batal · Simpan Draft · Lanjut.

#### Setelah Disimpan
Draft tersimpan dengan status Draft. **Stok, karantina, dan tagihan tidak berubah.**

#### Catatan Programmer
- Panel kanan menampilkan isi BM dengan kolom Diterima, Sudah/sedang diretur, dan Bisa diretur.
- Sudah/sedang diretur = semua item retur aktif yang belum dibatalkan.
- Panel tagihan membaca faktur yang terkait. Jika faktur belum ada, tampil "Belum ada faktur".

### Gambar 03 — Barang & Alasan Retur

#### Fungsi
Langkah 2: memilih barang, qty, kondisi, dan alasan **per barang**, beserta buktinya.

#### Dibuka dari
Wizard, langkah 2.

#### Field
- Per baris: centang, qty retur (≤ bisa diretur), harga beli (dari BM), nilai, kondisi, dan alasan (11 pilihan).
- Detail per baris: hasil uji (wajib untuk Tidak Berfungsi), no. seri, foto/video, catatan, serta "yang dipesan vs yang datang" untuk salah kirim.

#### Aksi
Kembali · Simpan Draft · Lanjut.

#### Setelah Disimpan
Item tersimpan di draft. Peringatan alokasi SO dicatat beserta pilihan yang diambil (lanjut dan tandai kekurangan, atau tunda).

#### Catatan Programmer
- **Peringatan K-02.** Jika qty retur > In Hand − Quarantine − Reserved, tampilkan nomor SO, nama customer, dan status DP (≥30%).
- "Lanjutkan" hanya untuk admin. Setelah disetujui, buat draft PO kekurangan (bukan PO terbit).
- Alasan Rusak / Cacat: foto wajib (bisa diatur).

### Gambar 04 — Packing Note Retur

#### Fungsi
Langkah 3: mencatat cara barang dikemas per koli, agar klaim kerusakan di jalan bisa dibuktikan.

#### Dibuka dari
Wizard, langkah 3.

#### Field
- Per koli: profil barang (elektronik & lampu, body kit, kaca/mika, jok & trim), berat, dimensi.
- Bahan pelindung (multi-pilih, 11 opsi).
- Isi koli: barang, qty, kelengkapan, kondisi kemasan.
- Foto sebelum dan sesudah dikemas.

#### Aksi
- + Tambah koli.
- **Susun ulang dari pilihan**: template teks catatan packing dibuat ulang dari pilihan di kiri.
- Teks catatan tetap bisa diedit.

#### Setelah Disimpan
Packing note tersimpan dan dipakai di cetak Packing Note, Bukti Serah Terima, dan Label Koli. Stok tidak berubah.

#### Catatan Programmer
- Validasi: setiap unit retur masuk tepat satu koli.
- Foto sesudah dikemas boleh kosong saat diajukan, tetapi wajib sebelum Konfirmasi Barang Keluar.
- Template = profil + bahan + isi. Jangan menimpa teks yang sudah diedit pengguna tanpa konfirmasi.

### Gambar 05 — Metode A: Dikirim lewat Ekspedisi

#### Fungsi
Langkah 4, metode A: merencanakan pengiriman ke supplier lewat ekspedisi.

#### Dibuka dari
Wizard, langkah 4, pilihan A.

#### Field
- Ekspedisi, layanan, rencana tanggal, jumlah koli (dari packing note).
- Alamat tujuan (dari Master Supplier, boleh diubah untuk retur ini), penerima, no. HP.
- Berat, perkiraan ongkir, ongkir ditanggung (toko / supplier / dibagi).
- Resi, jam serah, ongkir aktual, dan foto resi ditampilkan terkunci; diisi di 09.

#### Aksi
Kembali · Simpan Draft · Lanjut.

#### Setelah Disimpan
Rencana kirim tersimpan. Setelah disetujui, status barang keluar menjadi **Siap Dikirim**.

#### Catatan Programmer
- Ongkir "ditanggung supplier" masuk ke klaim (nota kredit / refund) sebagai baris tersendiri.
- Reminder: H+2 dari rencana kirim tanpa resi, dan H+7 dari kirim tanpa konfirmasi diterima.

### Gambar 06 — Metode B: Diambil Sales Supplier

#### Fungsi
Langkah 4, metode B: menjadwalkan pengambilan oleh sales supplier di toko, **tanpa resi**.

#### Dibuka dari
Wizard, langkah 4, pilihan B.

#### Field
- Nama sales, HP, perusahaan, jadwal ambil, perkiraan jam, konfirmasi dari kantor supplier, catatan.
- Bagian Serah Terima ditampilkan terkunci: jam diambil, nama pengambil sesuai identitas, ID, petugas, tanda tangan, foto.

#### Aksi
Kembali · Simpan Draft · Lanjut. Saat sales datang: **Catat Serah Terima Sales** (dari 01 atau 08).

#### Setelah Disimpan
Status barang keluar menjadi **Menunggu Pengambilan** setelah disetujui. Jika sales tidak datang, tanda lewat jadwal muncul H+1; stok tidak berubah.

#### Catatan Programmer
- Bukti sah = BST-{no retur} bernomor + tanda tangan dua pihak + foto.
- Jika nama pengambil berbeda dari rencana, tampilkan peringatan; admin boleh melanjutkan dengan catatan.

### Gambar 07 — Review & Ajukan

#### Fungsi
Langkah 5: memeriksa semua isian, melihat dampak stok, lalu mengajukan.

#### Dibuka dari
Wizard, langkah 5.

#### Field
- Ringkasan informasi, pengemasan, pengembalian, tabel barang, dan total nilai (harga beli).
- Daftar pemeriksaan (✓ / !).
- Dampak stok saat disetujui: fisik, karantina, bisa dijual.
- Catatan untuk penyetuju.

#### Aksi
- Staff: **Ajukan untuk Persetujuan**.
- Admin: **Setujui & Proses** atau **Kembalikan ke Draft**.

#### Setelah Disimpan
- Diajukan: status Menunggu Persetujuan, notifikasi ke admin.
- Disetujui: status Diproses, QUAR, dan SO yang kena ditandai kekurangan.
- Belum ada efek keuangan.

#### Catatan Programmer
- Pemeriksaan bertanda "!" tidak memblokir pengajuan. Yang bertanda merah memblokir (qty, alasan, alamat).
- Persetujuan memakai BEGIN IMMEDIATE dan memeriksa ulang qty bisa diretur serta stok di luar karantina.

### Gambar 08 — Detail Retur & Timeline

#### Fungsi
Satu halaman untuk seluruh perjalanan retur, berikut langkah berikutnya yang harus dilakukan.

#### Dibuka dari
Klik baris di Daftar Retur, notifikasi, atau tautan dari PO, Barang Masuk, Faktur, dan Master Barang.

#### Field
- Empat kartu status (dokumen, barang keluar, keputusan, penyelesaian).
- Banner "langkah berikutnya".
- Tab: Ringkasan, Barang & Alasan, Packing Note, Pengiriman, Keputusan Supplier, Penyelesaian, Dokumen & Foto, Log Audit.
- Tabel item dengan keputusan dan progres.
- Kartu Pengiriman, Dokumen terkait, dan Nilai retur.
- Riwayat (tidak bisa diubah).

#### Aksi
- Tombol berubah sesuai status: Konfirmasi Barang Keluar, Catat Keputusan Supplier, Terima Barang Pengganti, Catat Nota Kredit / Refund, Terima Kembali Barang Ditolak.
- Cetak.
- Batalkan Retur (sebelum barang keluar) atau Minta Barang Kembali (setelahnya).

#### Setelah Disimpan
Setiap aksi menambah satu baris riwayat dengan user, waktu, dan ringkasan efek stok/keuangan.

#### Catatan Programmer
- Nilai retur dibagi tiga: barang, ongkir diklaim, dan disetujui.
- Sisa klaim = disetujui − diganti − dikredit − direfund.
- Timeline dibaca dari supplier_return_events, bukan dirangkai dari kolom tanggal.

### Gambar 09 — Konfirmasi Barang Keluar

#### Fungsi
Titik tunggal **stok fisik keluar**: barang diserahkan ke ekspedisi (metode A). Versi metode B memakai form Serah Terima Sales.

#### Dibuka dari
Detail Retur (08) dengan status Siap Dikirim, lewat tombol Konfirmasi Barang Keluar.

#### Field
- Ekspedisi, no. resi (wajib, dicek unik), tanggal dan jam serah, ongkir aktual, ditanggung, petugas.
- Foto resi (wajib), foto paket sesudah dikemas (wajib), jumlah koli diserahkan.
- Pratinjau stok yang keluar.
- Centang pernyataan.

#### Aksi
Batal · **Konfirmasi Barang Keluar**.

#### Setelah Disimpan
- OUT sekali per item: fisik turun, karantina turun.
- Status barang keluar menjadi **Dalam Pengiriman**.
- Tombol terkunci.

#### Catatan Programmer
- Lindungi dengan tiga lapis: BEGIN IMMEDIATE, `UPDATE … SET handover_status='dikirim' WHERE id=? AND handover_status='siap'` (rowcount 1), dan Idempotency-Key.
- OUT `ref_type='supplier_return'`.
- Jika In Hand < qty (barang terlanjur terjual lewat override), tolak dan minta admin menyesuaikan qty.

### Gambar 10 — Catat Keputusan Supplier

#### Fungsi
Mencatat jawaban supplier per barang dan per qty: setuju, sebagian, atau tolak, berikut bentuk penyelesaiannya.

#### Dibuka dari
Detail Retur, setelah Diterima Supplier (atau lebih awal jika supplier sudah menjawab dari foto).

#### Field
- Tanggal keputusan, saluran, no. RMA/surat, lampiran.
- Per item: keputusan, qty disetujui, qty ditolak, penyelesaian (ganti / refund / nota kredit), alasan tolak, nasib barang ditolak (kirim balik / dimusnahkan / ditahan supplier).
- Baris ongkir sebagai klaim.

#### Aksi
Batal · Simpan Keputusan. Boleh bertahap: qty yang belum diputuskan tetap Menunggu.

#### Setelah Disimpan
Keputusan tersimpan. **Stok dan uang tidak berubah.** Barang ditolak yang dikirim balik mendapat status "menunggu barang kembali".

#### Catatan Programmer
- disetujui + ditolak ≤ qty dikirim − sudah diputuskan.
- Keputusan bisa direvisi selama belum ada penyelesaian yang memakainya. Revisi dicatat di log.

### Gambar 11 — Terima Barang Pengganti

#### Fungsi
Menerima pengganti dari supplier sebagai Barang Masuk baru yang tertaut ke retur.

#### Dibuka dari
Detail Retur, lewat tombol Terima Barang Pengganti (jika ada qty yang disetujui ganti).

#### Field
- Tanggal terima, surat jalan, datang lewat, petugas.
- Per item: disetujui ganti, sudah diterima, barang yang datang (sama / lain), no. seri baru, qty (maksimal sisa), kondisi, rak.

#### Aksi
Batal · **Terima & Posting Barang Masuk**.

#### Setelah Disimpan
- Barang Masuk `source='retur_pengganti'` tertaut ke item retur.
- IN dengan HPP = harga beli unit asal, sehingga rata-rata HPP tidak bergeser. Tagihan supplier tidak bertambah.
- Jika ada SO yang kekurangan, kekurangannya tertutup, dan sistem *menawarkan* membatalkan draft PO kekurangan.

#### Catatan Programmer
- Pengganti rusak: IN + QUAR.
- Spesifikasi berbeda: Barang Masuk untuk item lain. Selisih nilai menjadi nota kredit (lebih murah) atau tambahan tagihan (lebih mahal).
- Penerimaan bertahap diizinkan.

### Gambar 12 — Refund / Nota Kredit

#### Fungsi
Mencatat penyelesaian uang untuk bagian yang disetujui: refund, nota kredit yang memotong tagihan, atau saldo kredit supplier.

#### Dibuka dari
Detail Retur, lewat tombol Catat Nota Kredit / Refund. Hanya admin.

#### Field
- Bentuk penyelesaian.
- No. nota kredit (CN-SUP-YYYYMMDD-NNN), no. nota supplier, tanggal, nilai (maksimal sisa), bukti.
- Alokasi ke faktur yang belum lunas.
- Panel klaim: disetujui, sudah dikredit, sudah direfund, sisa.
- Untuk refund: daftar setiap uang yang diterima.

#### Aksi
Batal · Simpan Nota Kredit (atau Simpan Refund).

#### Setelah Disimpan
- Nota kredit tercatat. Sisa tagihan faktur turun lewat alokasi; faktur tidak diedit.
- Refund: status "diterima sebagian" sampai jumlahnya sama dengan nilai disetujui.

#### Catatan Programmer
- Pembatalan membuat baris pembalik, bukan menghapus.
- Alokasi memakai (doc_type, doc_id), sehingga bisa menunjuk Faktur Pembelian atau pembayaran PO, tergantung keputusan K-12.

### Gambar 13 — Cetak Nota Retur (Epson LX-310)

#### Fungsi
Pratinjau dan cetak dokumen retur sebagai teks polos untuk printer dot matrix.

#### Dibuka dari
Detail Retur → Cetak ▾, atau menu ··· di Daftar Retur.

#### Field
- Dokumen: Nota Retur, Packing Note, Bukti Serah Terima, Label Koli.
- Ukuran kertas (**perlu konfirmasi**), lebar baris (80 kolom / 10 cpi atau 96 kolom / 12 cpi).
- Geser kiri/atas untuk kalibrasi, rangkap, salinan.

#### Aksi
Unduh PDF · Cetak Kalibrasi · Cetak ke LX-310.

#### Setelah Disimpan
Pengaturan printer disimpan per komputer. Setiap cetak dicatat di log: siapa, kapan, dan rangkap ke berapa.

#### Catatan Programmer
- Hasilkan teks lebar tetap dan kirim apa adanya (ESC/P, font printer). Jangan merender sebagai gambar.
- Potong atau bungkus nama barang di batas kolom.
- Kalibrasi = cetak penggaris kolom 1–80 dan penanda baris.

### Gambar 14 — Integrasi Retur & Inventory

#### Fungsi
Menunjukkan efek retur pada stok di Master Barang: stok fisik, dicadangkan, karantina, bisa dijual, dan kekurangan pesanan. Juga menampilkan daftar Stok Karantina.

#### Dibuka dari
Master Barang → detail barang, dan menu baru Inventori → Stok Karantina.

#### Field
- KPI stok dan rumusnya.
- Kartu stok (stock_ledger) dengan baris IN / QUAR / OUT. Baris yang akan datang ditampilkan miring sebagai pratinjau.
- Daftar karantina dengan asalnya: retur, rusak saat diterima, retur pelanggan.

#### Aksi
Buat retur (dari kandidat) · Lepas ke stok jual… · Hapus buku… (admin, alasan wajib).

#### Setelah Disimpan
- Lepas: UNQUAR.
- Hapus buku: OUT write-off + QUAR berkurang.
- Keduanya tercatat di ledger dengan alasannya.

#### Catatan Programmer
- Kasir memakai Bisa dijual. Stock Opname membandingkan **stok fisik**, yang mencakup rak karantina.
- Laporan nilai stok menampilkan nilai karantina terpisah.

## 5. Aturan stok

### Rumus

- **Stok fisik (In Hand)** = jumlah IN − OUT di stock_ledger (sama seperti sekarang).
- **Karantina** = jumlah QUAR − UNQUAR (− bagian karantina yang keluar lewat OUT retur / write-off).
- **Dicadangkan** = hasil `_reservation_lines` (K-02). **Pool pencadangan = In Hand − Karantina**, sehingga unit karantina tidak pernah dianggap tersedia untuk SO.
- **Bisa dijual** = In Hand − Dicadangkan − Karantina. Jika negatif, tampilkan 0 dan munculkan kekurangan di SO yang terdampak, dengan urutan pencadangan seperti K-02.

### Kapan stok bergerak

| Kejadian | Ledger | Stok fisik | Karantina | Bisa dijual |
|---|---|---|---|---|
| Draft dibuat / diajukan | — | tetap | tetap | tetap |
| Retur disetujui | QUAR +qty | tetap | naik | turun |
| Retur dibatalkan sebelum keluar | UNQUAR −qty (atau tetap karantina bila barangnya memang rusak) | tetap | turun | naik |
| **Konfirmasi Barang Keluar / Serah Terima Sales** | **OUT −qty** + karantina −qty | **turun** | turun | tetap |
| Diterima supplier / keputusan dicatat | — | tetap | tetap | tetap |
| Pengganti diterima (kondisi baik) | IN +qty (Barang Masuk retur_pengganti) | naik | tetap | naik |
| Pengganti rusak | IN + QUAR | naik | naik | tetap |
| Barang ditolak kembali ke toko | IN + QUAR | naik | naik | tetap |
| Lepas karantina ke stok jual | UNQUAR | tetap | turun | naik |
| Hapus buku | OUT write-off | turun | turun | tetap |
| Terima Barang dengan kondisi rusak / tidak_sesuai | IN + QUAR (satu posting) | naik | naik | tetap |
| Retur pelanggan tujuan supplier / rusak | IN + QUAR, menjadi kandidat retur | naik | naik | tetap |

### Titik stok keluar yang tepat

Stok fisik keluar **satu kali**, saat barang benar-benar berpindah tangan:

- **Metode A**: saat Konfirmasi Barang Keluar disimpan, yaitu ketika paket sudah diserahkan ke ekspedisi dan resi sudah ada.
- **Metode B**: saat Serah Terima Sales disimpan, yaitu ketika Bukti Serah Terima sudah ditandatangani.

Penerimaan oleh supplier, keputusan supplier, dan nota kredit tidak menggerakkan stok fisik.

### Anti double posting

1. Status serah terima diubah dengan compare-and-set (`WHERE handover_status = 'siap'`) di dalam BEGIN IMMEDIATE. Jika rowcount = 0, tidak ada posting.
2. Header Idempotency-Key (sudah ada di wrapper fetch) dipakai untuk semua POST retur.
3. Indeks unik `(ref_type, ref_id, master_item_id, type)` untuk baris ledger yang berasal dari dokumen retur, dengan satu posting per item per kejadian. Untuk kejadian berulang seperti pengganti bertahap, `ref_id` menunjuk baris penerimaan masing-masing, bukan dokumen retur.
4. Qty bisa diretur dihitung ulang di server saat disetujui dan saat barang keluar, bukan dipercaya dari browser.

## 6. Aturan keuangan

Prinsip:

- Draft dan persetujuan tidak punya efek keuangan.
- Uang atau hutang baru berubah saat supplier memutuskan **dan** penyelesaiannya dicatat.
- Faktur tidak pernah diedit; semua koreksi dicatat lewat dokumen baru yang tertaut.

| Kasus | Perlakuan |
|---|---|
| Draft / Menunggu Persetujuan / Diproses tanpa keputusan | Tidak ada pencatatan keuangan. Nilai retur hanya informasi (harga beli). |
| Retur sebelum faktur dibuat | Nota kredit dicatat "menunggu faktur". Saat Faktur Pembelian untuk supplier itu dibuat, sistem menyarankan alokasi. Opsi lain: faktur dibuat dengan qty bersih jika barang dikembalikan sebelum ditagih (keputusan admin). |
| Retur saat faktur belum lunas | Nota kredit dialokasikan ke faktur (sisa tagihan turun). Total faktur tidak diedit. |
| Retur setelah faktur lunas | Pilih refund (supplier mengembalikan uang) atau saldo kredit supplier yang dipakai di faktur berikutnya. |
| Refund bertahap | Catat nilai disetujui, lalu setiap penerimaan (tanggal, metode, bukti). Status: disetujui → diterima sebagian → diterima penuh. Penerimaan > disetujui ditolak. |
| Disetujui sebagian | Hanya bagian yang disetujui yang menghasilkan kredit atau refund. Bagian ditolak tidak berefek keuangan. |
| Barang pengganti sama | Tidak ada efek keuangan. HPP = harga unit asal. |
| Pengganti spesifikasi beda | Selisih lebih murah menjadi nota kredit; lebih mahal menjadi tambahan tagihan (dokumen terpisah, persetujuan admin). |
| Ongkir ditanggung supplier | Baris klaim ongkir ikut keputusan supplier; jika disetujui, masuk nota kredit / refund. |
| Ongkir ditanggung toko | Dicatat sebagai biaya retur (laporan), bukan bagian dari nilai barang. |
| Paket hilang di ekspedisi | Klaim ke ekspedisi (catatan + nilai klaim). Jika gagal, hapus buku dengan nilai harga beli sebagai kerugian. |
| Retur dibatalkan | Jika sudah ada nota kredit / refund (seharusnya tidak mungkin sebelum barang keluar), nota tersebut dibalik terlebih dahulu. |

Batas nilai:

- Kredit + refund + nilai pengganti ≤ nilai disetujui.
- Nilai disetujui ≤ nilai barang dikirim + klaim ongkir.

Semua dicek di server.

## 7. Validasi & peringatan

| # | Skenario | Perilaku sistem | Jenis |
|---|---|---|---|
| 1 | Qty retur > diterima − sudah/sedang diretur | Tidak bisa disimpan; tampil batas maksimal | Blokir |
| 2 | Barang dicadangkan untuk SO (K-02) | Peringatan dengan no. SO, customer, DP. Admin memilih lanjut + tandai kekurangan (draft PO kekurangan) atau tunda | Peringatan admin |
| 3 | Stok fisik di luar karantina < qty retur (barang sudah terjual) | Tidak bisa disetujui. Kurangi qty atau pilih unit dari karantina | Blokir |
| 4 | Alasan Tidak Berfungsi tanpa hasil uji | Tidak bisa diajukan | Blokir |
| 5 | Rusak / Cacat tanpa foto | Tidak bisa diajukan (bisa diubah di pengaturan) | Blokir |
| 6 | Alasan "Lainnya" tanpa penjelasan | Tidak bisa diajukan | Blokir |
| 7 | Unit belum masuk koli mana pun | Tidak bisa diajukan | Blokir |
| 8 | Metode A tanpa alamat / penerima | Tidak bisa diajukan | Blokir |
| 9 | Konfirmasi keluar tanpa resi atau foto resi | Tidak bisa disimpan | Blokir |
| 10 | Resi sudah dipakai retur lain | Tidak bisa disimpan; tampil nomor retur pemakainya | Blokir |
| 11 | Tombol Konfirmasi ditekan dua kali / dua user bersamaan | Posting kedua tidak terjadi (CAS + Idempotency-Key); user kedua melihat "sudah dikonfirmasi oleh …" | Aman |
| 12 | Foto sesudah dikemas belum ada saat konfirmasi keluar | Tidak bisa disimpan | Blokir |
| 13 | Rencana kirim lewat 2 hari tanpa resi | Tanda "terlambat kirim" dan masuk filter Perlu tindakan | Reminder |
| 14 | Sales tidak datang sesuai jadwal | Tanda lewat jadwal H+1; jadwal ulang / ganti metode / batal | Reminder |
| 15 | Nama pengambil ≠ rencana | Peringatan; admin boleh lanjut dengan catatan | Peringatan |
| 16 | Supplier belum konfirmasi terima 7 hari | Reminder | Reminder |
| 17 | Paket hilang | Status Bermasalah; klaim ekspedisi / hapus buku; stok tidak kembali otomatis | Proses |
| 18 | Disetujui + ditolak > qty dikirim | Tidak bisa disimpan | Blokir |
| 19 | Keputusan belum dicatat 14 hari | Reminder | Reminder |
| 20 | Pengganti > qty disetujui ganti − sudah diterima | Tidak bisa disimpan; kelebihan dicatat sebagai pembelian biasa | Blokir |
| 21 | Pengganti datang bertahap | Diizinkan; progres x / y | Proses |
| 22 | Pengganti spesifikasi / harga beda | Peringatan + selisih menjadi nota kredit atau tambahan tagihan | Peringatan |
| 23 | Nota kredit > sisa nilai disetujui | Tidak bisa disimpan | Blokir |
| 24 | Alokasi kredit > sisa faktur | Tidak bisa disimpan; sisanya menjadi saldo kredit | Blokir |
| 25 | Refund diterima > nilai disetujui | Tidak bisa disimpan | Blokir |
| 26 | Faktur belum ada saat kredit dicatat | Kredit "menunggu faktur", disarankan saat faktur dibuat | Proses |
| 27 | Batalkan retur sebelum barang keluar | Admin + alasan; UNQUAR atau tetap karantina | Admin |
| 28 | Batalkan retur setelah barang keluar | Tidak bisa; gunakan Minta Barang Kembali → Terima Kembali | Blokir |
| 29 | Barang ditolak kembali ke toko | IN + QUAR; admin memutuskan jual / hapus buku / retur ulang | Proses |
| 30 | Kasir menjual barang karantina | Tidak tampil sebagai bisa dijual. Override admin K-02 tidak berlaku untuk karantina | Blokir |
| 31 | Stock opname saat ada karantina | Hitung fisik dibandingkan dengan stok fisik (termasuk karantina) | Aturan |
| 32 | Edit draft oleh dua user bersamaan | Simpan dengan versi (updated_at). Simpanan kedua ditolak: "diubah oleh … muat ulang" | Aman |
| 33 | Edit item setelah disetujui | Tidak bisa; kembalikan ke draft (admin) yang otomatis melepas karantina | Blokir |
| 34 | Retur dari BM yang sudah dihapus / di-void | Tidak bisa memilih sumber tersebut | Blokir |

## 8. Usulan database

Tabel baru, semuanya dengan `created_at`, `created_by`, dan `updated_at` untuk versi:

| Tabel | Kolom penting | Tautan ke tabel yang ada |
|---|---|---|
| `supplier_returns` | id, return_number (unik), supplier_id, source_type (inbound / quarantine / none), inbound_document_id, supplier_po_id, purchase_invoice_id, return_type, requested_settlement, rma_number, method (kirim / sales), doc_status, handover_status, approved_by/at, cancelled_by/at, cancel_reason, notes | `inbound_documents`, `supplier_pos`, `purchase_invoices`, `suppliers` |
| `supplier_return_items` | id, return_id, master_item_id, inbound_item_id, spo_item_id, qty, unit_cost (dari BM), condition, reason_code, reason_detail, test_result, serial_no, so_shortage_ack | `inbound_items`, `supplier_po_items`, `master_items` |
| `supplier_return_photos` | id, return_id, item_id (nullable), kind (bukti / sebelum_kemas / sesudah_kemas / resi / serah_terima / bst), path | — |
| `supplier_return_packages` | id, return_id, koli_no, profile, materials (JSON), weight, dims, packing_text | — |
| `supplier_return_package_items` | package_id, item_id, qty, accessories, pack_condition | — |
| `supplier_return_handover` | return_id (unik), method, courier, service, awb (unik bila tidak null), handed_at, shipping_cost, cost_bearer, address, recipient, sales_name, sales_phone, sales_id_no, pickup_schedule, received_by_supplier_at, problem_flag | — |
| `supplier_return_responses` | id, return_id, item_id, decided_at, channel, qty_approved, qty_rejected, settlement_type, approved_value, reject_reason, rejected_goods_fate | — |
| `supplier_return_replacements` | id, item_id, inbound_document_id, master_item_id, qty, unit_cost, condition | `inbound_documents` (source = retur_pengganti) |
| `supplier_credit_notes` | id, cn_number, supplier_id, return_id, supplier_cn_no, amount, issued_at, status, reversed_by | — |
| `supplier_credit_allocations` | id, credit_note_id, doc_type (purchase_invoice / supplier_po), doc_id, amount | `purchase_invoices` atau pembayaran PO (K-12) |
| `supplier_refunds` | id, return_id, item_id, approved_amount; dan `supplier_refund_receipts` (refund_id, received_at, method, amount, proof) | — |
| `supplier_return_events` | id, return_id, event, payload (JSON), user, at | sumber timeline dan log audit |

Perubahan pada tabel yang ada:

- `stock_ledger.type` mendapat nilai `QUAR` dan `UNQUAR` (qty positif atau negatif terhadap karantina; tidak mengubah `balance_after` fisik). Kolom baru `quarantine_after` mempercepat pembacaan.
- `ref_type` baru: `supplier_return`, `supplier_return_rejected_back`, `quarantine_writeoff`, `quarantine_release`.
- `master_items` (atau view) mendapat `quarantine_qty` sebagai cache yang dihitung dari ledger dan dicek oleh stock opname.
- `inbound_documents.source` mendapat nilai `retur_pengganti` dan `retur_ditolak_kembali`; `inbound_items.condition` rusak / tidak_sesuai memicu QUAR.
- `spo_cancellations` dengan action `retur_supplier` mendapat kolom `supplier_return_id`.
- `receipt_returns.supplier_qty` / `damaged_qty` menjadi IN + QUAR dengan `ref_type='customer_return'`.

## 9. Format cetak Epson LX-310

Prinsip:

- Teks polos dengan lebar tetap, font bawaan printer (Draft / NLQ). Dikirim ke printer sebagai teks (ESC/P), bukan gambar.
- Lebar 80 kolom pada 10 cpi (9,5" continuous form, area cetak ± 8"), atau 96 kolom pada 12 cpi.
- **Kertas: continuous form 9,5" × 11", 3 rangkap** (diputuskan, keputusan 5): lembar 1 Supplier, lembar 2 Gudang, lembar 3 Admin/Arsip. Pastikan kertas yang dibeli sesuai template.
- Offset kiri/atas bisa dikalibrasi dari layar cetak (mockup 13).

### Nota Retur Supplier (80 kolom)

```
SINAR GALAKSI VARIASI MOBIL                         NOTA RETUR SUPPLIER
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
Diminta : Ganti barang     Kirim: JNE REG  Resi: 0108 2410 1977 5521
Koli    : 1 (2,6 kg)       Ongkir Rp 18.000 ditanggung supplier

   Dibuat oleh,              Disetujui,                 Diterima supplier,


  (   andi   )          ( sinargalaksi88 )          (......................)
```

### Packing Note / Bukti Serah Terima (80 kolom)

```
SINAR GALAKSI VARIASI MOBIL                   BUKTI SERAH TERIMA BARANG RETUR
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


  (......................)                (......................)
```

Packing Note memakai kepala yang sama dengan judul "PACKING NOTE — RETUR SUPPLIER". Isinya per koli: berat, dimensi, isi, bahan pelindung, dan catatan packing dari mockup 04. Label Koli berisi supplier tujuan, no. retur, koli x/y, dan "FRAGILE" bila profilnya elektronik atau kaca.

## 10. Fitur wajib v1 vs fitur lanjutan

| Wajib v1 | Lanjutan |
|---|---|
| Daftar retur + filter + KPI | Dashboard analitik retur per supplier / alasan |
| Wizard 5 langkah, sumber dari Barang Masuk | Sumber tanpa dokumen dengan pencocokan HPP otomatis |
| Alasan per item, hasil uji, foto | Video besar / kompresi otomatis |
| Packing note per koli + template teks | Label koli dengan barcode / QR |
| Metode A (resi) dan B (Bukti Serah Terima) | Cek status resi otomatis lewat API ekspedisi |
| Persetujuan admin + Stok Karantina (QUAR / UNQUAR) | Lokasi rak karantina terpisah per gudang |
| Konfirmasi Barang Keluar sekali (anti double posting) | Barang keluar per koli (multi-koli bertahap) |
| Keputusan supplier per item / qty, termasuk sebagian | Portal supplier untuk menjawab sendiri |
| Pengganti sebagai Barang Masuk retur_pengganti | Pencocokan otomatis no. seri garansi |
| Nota kredit + alokasi ke faktur, refund bertahap | Saldo kredit dipakai otomatis saat bayar faktur |
| Terima kembali barang ditolak (IN + QUAR) | Penjualan barang second dengan harga khusus |
| Peringatan K-02 dan draft PO kekurangan | Notifikasi WhatsApp ke customer SO yang terdampak |
| Cetak Nota Retur + BST teks untuk LX-310 | Template cetak yang bisa diubah sendiri |
| Timeline + log audit | Reminder terjadwal lewat bot Telegram yang sudah ada |
| Karantina untuk Terima Barang rusak dan retur pelanggan | Klaim ke ekspedisi dengan dokumen lengkap |

## 11. Keputusan owner (diputuskan 18-10-2026)

Prinsip yang diwajibkan: **setiap perubahan stok dan nilai utang punya dokumen sumber, riwayat pengguna, dan jejak audit.** Status fisik barang (mis. Karantina) dan status proses retur (mis. Menunggu Persetujuan) adalah dua hal terpisah dan tidak digabung menjadi satu status.

| No | Keputusan | Aturan implementasi |
|---|---|---|
| 1 | **Utang supplier dicatat di Faktur Pembelian (K-12)**, bukan di pembayaran PO | PO adalah komitmen, belum utang. Uang yang dibayar sebelum faktur dicatat sebagai **uang muka supplier (DP)** dan diperhitungkan saat faktur dibuat. Retur yang disetujui supplier mengurangi utang lewat nota kredit yang dialokasikan ke faktur. |
| 2 | **Retur buatan staff wajib disetujui Admin** | Staff membuat draft + bukti; admin menyetujui / menolak sebelum retur diproses final. |
| 3 | **Barang rusak saat Terima Barang langsung masuk Karantina** | Hanya qty berkondisi baik yang menambah stok jual. Rusak / tidak sesuai / perlu dicek tidak boleh terjual. |
| 4 | **Nomor retur RTS-YYYYMMDD-NNN** | Contoh RTS-20261018-001. Urutan per hari, otomatis, nomor batal tidak dipakai ulang. Nota kredit: CN-SUP-YYYYMMDD-NNN. |
| 5 | **Epson LX-310: continuous form 9,5" × 11", 3 rangkap** | Lembar 1 Supplier, 2 Gudang, 3 Admin/Arsip. |
| 6 | **Foto wajib di dua tahap** | Sebelum dikemas: kerusakan, produk, label/identitas barang. Sesudah dikemas: paket dan label kirim bila ada. Boleh beberapa foto per tahap. |
| 7 | **Ongkir retur karena kesalahan supplier ditanggung supplier** | Salah kirim, cacat dari supplier, jumlah tidak sesuai → supplier. Toko salah pesan → toko. Pilihan penanggung: Supplier / Toko / Kesepakatan Khusus. |
| 8 | **Hapus stok karantina hanya dengan persetujuan Owner** | Staff mengajukan, Admin memeriksa dan merekomendasikan, Owner menyetujui akhir. Simpan alasan, nilai barang, pengguna, dan waktu. |
| 9 | **Barang ditolak supplier boleh dijual sebagai second, dengan syarat ketat** | Lolos pemeriksaan kelayakan, disetujui Owner, pindah ke stok barang second dengan kondisi dan harga jelas. Barang elektronik / komponen keselamatan yang berbahaya tidak boleh dijual kembali. |
| 10 | **Retur pelanggan "ke supplier" otomatis menjadi kandidat retur**, bukan retur final | Dibuat otomatis, tertaut ke retur pelanggan dan pembelian asal, menunggu pemeriksaan Admin. Tidak mengurangi stok / utang otomatis. |

**Owner** diterapkan sebagai tanda pada akun admin (halaman Pengguna), bukan peran ketiga. Selama ada Owner aktif, hanya Owner yang boleh mengubah tanda itu; bila belum ada, admin mana pun boleh memberikannya pertama kali.

### Status pembangunan

- **Tahap 1 — sudah dibangun (18-10-2026):** Stok Karantina (`master_items.quarantine_qty` terpisah dari stok jual, lot karantina + riwayat), keputusan 3 (Terima Barang PO dan posting Barang Masuk berkondisi rusak / tidak sesuai → karantina; barang itu juga tidak dihitung tiba untuk Sales Order), keputusan 10 (retur pelanggan ke supplier → karantina sebagai kandidat; rusak → karantina), keputusan 8 (pengajuan hapus: staff → admin → Owner), lepas ke stok jual oleh admin dengan alasan, pindah stok jual ke karantina oleh admin, tanda Owner. Void Barang Masuk ikut membatalkan lot karantinanya.
- **Tahap 2 — sudah dibangun (19-10-2026):** keputusan 1 / K-12 (utang di Faktur Pembelian; pembayaran PO sebelum faktur = DP supplier, otomatis masuk faktur saat diposting, kembali ke PO bila faktur di-void). Dokumen Retur Supplier: menu **Retur Supplier**, daftar + KPI + filter, form 4 langkah (Referensi retur → Pilih item dari Barang Masuk / Stok Karantina / stok toko → Packing & metode → Review), persetujuan admin (staff mengajukan), peringatan stok booking SO (K-02), foto wajib dua tahap, Konfirmasi Barang Keluar (resi) / Serah Terima Sales (BST) sebagai satu-satunya titik stok keluar, keputusan supplier per barang, barang pengganti sebagai Barang Masuk `retur_pengganti` dengan harga beli asal, nota kredit CN-SUP-YYYYMMDD-NNN yang dialokasikan ke Faktur Pembelian (sisa = saldo kredit), refund, barang ditolak kembali ke karantina, Selesai otomatis, cetak Nota Retur / Packing Note / BST (teks 80 kolom LX-310 atau grafis, 3 rangkap). Tata letak mengikuti 14 gambar contoh dari owner.
- **Keputusan 9 final (19-10-2026):** barang ditolak supplier masuk karantina untuk diperiksa Admin; bila layak jual dipindah ke SKU/varian BEKAS terpisah dengan referensi barang asal, nilai persediaan ikut pindah (tidak digandakan), harga jual hanya oleh Admin; tidak layak tetap di luar Stock Available.
- **Belum:** klaim ke ekspedisi untuk paket hilang, pengingat terjadwal lewat bot Telegram.

## 12. Daftar file

| File | Isi |
|---|---|
| `RETUR_SUPPLIER_DESAIN.md` | dokumen ini (sumber) |
| `RETUR_SUPPLIER_DESAIN.pdf` | A3 landscape: dokumen + 14 mockup dengan penjelasan + 4 flowchart |
| `mockups/01…14_*.png` | 14 mockup layar desktop |
| `mockups/src/` | `app.css`, `shell.py`, `screens_a/b/c.py` (sumber mockup) + HTML hasil build |
| `flowcharts/FLOW_A…D_*.mmd` / `.png` | 4 flowchart: ekspedisi, diambil sales, keputusan & penolakan, pengganti & keuangan |
| `build.py` | membangun ulang semuanya: `python3.11 docs/retur-supplier/build.py` |
