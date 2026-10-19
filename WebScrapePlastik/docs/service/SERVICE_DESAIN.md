# Modul Service / Garansi / Kirim Pabrik — Desain UI/UX & Workflow

**SINAR GALAKSI VARIASI MOBIL** · dokumen desain, 19/10/2026 · status: **DISETUJUI 19-10-2026 (K-S1–K-S10 ikut rekomendasi), v1 SUDAH DIBANGUN**

Dokumen ini menjawab brief "MASTER PROMPT — UI/UX Modul Service / Garansi / Kirim Pabrik". Isinya:

- audit nota service manual;
- alur lengkap dan model status;
- 16 mockup beserta penjelasannya (Gambar 01–15 mengikuti contoh dari owner, Gambar 16 Master Service dibuat baru);
- 4 flowchart;
- panduan programmer, struktur data, relasi, dan format tiga surat service.

Keputusan owner K-S1–K-S10 sudah diambil (§10); pembangunan modul mengikuti dokumen ini.

Data contoh yang dipakai di seluruh dokumen: customer **Budi Santoso** (0812-3456-7890, B 1456 WZU, Innova Reborn) membawa **Head Unit Android 9" AVIX AX-9** (SN HU12345678). Unit itu dibeli dengan nota **PJSG/26/08/0125** tanggal 10/08/2026. Nomor nota memakai format asli aplikasi (`PJSG/YY/MM/NNNN`), bukan `INV-2026-00125` seperti di contoh gambar.

## 1. Ringkasan

- **Barang customer bukan stok toko.** Unit yang masuk service dicatat di **register titipan** (custody), tidak pernah di `stock_ledger`. Stok toko baru bergerak dalam dua kasus:
    - toko menukar unit customer dengan unit dari stok sendiri;
    - toko memakai sparepart dari stok.
- **Satu dokumen service = satu unit.** Contohnya satu head unit, atau sepasang speaker yang dihitung satu unit. Customer yang membawa beberapa unit sekaligus mendapat beberapa dokumen dalam satu **kunjungan**, dan surat terimanya bisa dicetak sekaligus.
- **Status dicatat di empat jalur terpisah**:
    - **Tahap**: posisi dokumen;
    - **Lokasi Unit**: di mana barangnya sekarang;
    - **Hasil**: apa keputusannya;
    - **Garansi** dan **Biaya**: dua status pendukung sesuai brief.

  Lima belas status yang diminta di brief tetap muncul sebagai label, tetapi tidak dicampur dalam satu kolom (§3).
- **Keputusan service atau ganti baru baru dicatat setelah diagnosis tertulis dari pabrik/principal.** Penanganan awal hanya memilih jalur pemeriksaan.
- **Biaya tambahan wajib disetujui customer sebelum pabrik mulai bekerja.** Persetujuan, penolakan, atau "menunggu" dicatat lengkap dengan tanggal, media, dan bukti.
- **Garansi dicek per baris nota**, bukan per nama customer. Masa garansi diambil dari Master Service. Status "Garansi Aktif" di toko hanya indikasi; pabrik yang menentukan akhirnya.
- **Uang service tidak mengubah nota penjualan lama.** Biaya service ditagih lewat **Nota Jasa Service** baru di Kasir, supaya kas, metode bayar, dan laporan tetap satu jalur. Diputuskan owner (K-S3).
- **Tiga surat service**:
    - Tanda Terima Service;
    - Surat Jalan Service ke supplier/principal;
    - Nota Hasil Service / Penggantian.

  Ketiganya berformat teks 80 kolom untuk Epson LX-310, sama seperti Nota Retur Supplier.

## 2. Audit nota service manual

> **Foto nota manual belum diterima.** Lampiran di pesan brief hanya berisi 15 gambar mockup. Audit di bawah memakai daftar elemen yang owner tulis di brief (bagian 6) dan checklist kelengkapan di bagian 3C. Begitu foto dikirim, bagian ini akan dicocokkan ulang, termasuk tata letak, jumlah rangkap, dan syarat yang tercetak di nota.

### Elemen nota manual dan penempatannya di sistem

| Elemen di nota manual | Di sistem | Catatan |
|---|---|---|
| ID Service | **No. Service** otomatis `SVC-YYYYMMDD-NNN` | Sama polanya dengan `RTS-YYYYMMDD-NNN` dan `PO-SUP-YYYYMMDD-NNN`. Nomor dikunci saat dokumen diterima (bukan saat draft) |
| Tanggal | Tanggal + jam terima (waktu toko) | Jam penting untuk sengketa ("unit diterima jam berapa") |
| Nama customer | Master Customer (`customers`) + salinan nama di dokumen | Dicari dari nomor HP; customer baru dibuat dari form yang sama |
| Telepon | `customers.phone` (unik) + salinan di dokumen | Wajib; dipakai untuk kabar biaya dan siap diambil |
| Mobil | Plat + merk/model kendaraan | Diisi otomatis dari nota asal (`receipts.plate_full`, `vehicle_make`, `vehicle_model`) bila nota ditemukan |
| Data barang service | Nama barang (dari Master Barang bila ada) | Terhubung ke baris nota asal (`receipt_items`) |
| Merek | Master Brand | Otomatis dari Master Barang |
| Tipe | Model / tipe (teks) | Mis. "AX-9 · 2/32GB" |
| Nomor seri | Serial number, wajib bila jenis barangnya mewajibkan (Master Service) | Bisa dicari di daftar service |
| Jenis kerusakan | Keluhan customer (kata-kata customer) | Dipisah dari **Diagnosis** (teknisi/pabrik), yang diisi kemudian |
| Kelengkapan (Head unit, Subwoofer, Kabel set, Kamera, Box, Lainnya) | Checklist dari template Master Service + "Lainnya" teks bebas | Checklist yang sama dicek ulang saat serah ke customer |
| Biaya service | **Status Biaya** + estimasi, final, dan pembayaran | Saat terima, isinya "Belum ditentukan", bukan angka |
| Note | Catatan | Tercetak di surat |

### Yang sudah benar dari nota manual (dipertahankan)

- Nota dibuat saat barang diterima dan diberi nomor, sehingga ada bukti titipan untuk customer.
- Kelengkapan berbentuk checklist. Ini cepat diisi dan mudah dicocokkan saat barang dikembalikan.
- Merek, tipe, dan nomor seri dicatat. Nomor seri adalah kunci klaim garansi ke pabrik.
- Keluhan ditulis di nota, sehingga ada dasar untuk diteruskan ke pabrik.
- Ada tempat untuk biaya dan catatan.

### Yang perlu direvisi

| Masalah di proses manual | Risiko | Perbaikan di sistem |
|---|---|---|
| Tidak ada nomor nota pembelian dan tanggal beli | Status garansi ditebak; garansi bisa diberikan pada barang yang dibeli di tempat lain | Cari nota asal di langkah 1; garansi dihitung dari baris nota |
| Tidak ada kondisi fisik masuk dan foto | Sengketa "dulu tidak lecet" saat barang kembali | Kondisi fisik wajib diisi + foto unit, SN, dan kelengkapan |
| Biaya ditulis di nota terima | Customer menganggap angka itu final | Surat terima menulis "Biaya mengikuti hasil pemeriksaan" |
| Tidak tercatat barang sedang di mana | "Sudah dikirim belum? Di sales siapa?" | Lokasi Unit + Surat Jalan + bukti serah terima |
| Tidak ada catatan persetujuan biaya | Customer menolak bayar karena "tidak pernah setuju" | Persetujuan tercatat: tanggal, media, bukti, petugas |
| Tidak ada tanda tangan penerima saat barang diambil | Tidak ada bukti barang sudah kembali | Serah terima mencatat penerima, jam, kelengkapan kembali, tanda tangan |
| Nota rangkap tulisan tangan tidak bisa dicari | Riwayat unit hilang; sulit tahu unit ini pernah diservice | Pencarian berdasarkan no. service, nota, nama, HP, plat, SN |
| Tidak ada syarat (barang tidak diambil, data hilang) | Unit menumpuk di toko; komplain data head unit hilang | Syarat tercetak di surat terima (K-S8: 90 hari, data bisa hilang, tanda terima wajib dibawa) |

### Elemen tambahan yang diusulkan

- Nomor nota pembelian, tanggal beli, masa dan batas garansi, serta dasar garansi (nota / kartu garansi / tanpa bukti).
- Kondisi fisik masuk dan foto.
- Untuk head unit Android: persetujuan bahwa unit boleh di-reset ke pengaturan pabrik (data dan akun bisa hilang). Password atau PIN customer **tidak** disimpan; customer diminta logout atau menghapus kunci layar.
- Estimasi selesai, yaitu perkiraan lama per jenis barang dari Master Service.
- Penerima di toko (petugas login) dan tanda tangan customer.
- **Label unit**: potongan kertas atau stiker dengan no. service, nama, dan HP, ditempel di unit supaya tidak tertukar di gudang atau di pabrik.

## 3. Model status

Brief meminta 15 status dokumen, 4 status garansi, dan 6 status biaya. Kelima belas status dokumen itu sebenarnya mencampur tiga hal: **tahap** dokumen, **lokasi** barang, dan **hasil** keputusan. Kalau digabung dalam satu kolom, satu dokumen bisa "Diganti Baru" sekaligus "Siap Diambil", dan "Dikirim" bisa tertukar dengan "Diterima supplier". Karena itu disimpan di jalur terpisah, sama seperti Retur Supplier.

| Jalur | Nilai | Siapa yang mengubah |
|---|---|---|
| **Tahap** | Draft → Diterima → Dalam Pengecekan → Menunggu Dikirim → Di Supplier/Principal → Menunggu Keputusan Customer → Kembali ke Toko → Siap Diambil → **Selesai** · atau **Dibatalkan** | sistem, dari aksi pengguna; Dibatalkan oleh admin |
| **Lokasi Unit** | Di Toko → Di Ekspedisi / Dibawa Sales → Di Pabrik/Principal → Dalam Perjalanan Balik → Kembali di Toko → Diserahkan ke Customer | aksi kirim, serah, terima |
| **Hasil** | Belum Diputuskan · Selesai Diperbaiki · Diganti Baru · Ditolak · Dikembalikan Apa Adanya · Dibatalkan | admin, dari diagnosis pabrik / keputusan customer |
| **Garansi** | Belum Diverifikasi · Garansi Aktif · Garansi Habis · Garansi Ditolak | sistem (dari nota), admin (override), pabrik (ditolak) |
| **Biaya** | Tanpa Biaya · Menunggu Estimasi · Menunggu Persetujuan Customer · Disetujui Customer · Ditolak Customer · Sudah Dibayar | sistem, dari estimasi, persetujuan, pembayaran |

### Pemetaan 15 status brief ke model

| Status di brief | Disimpan sebagai |
|---|---|
| Draft | Tahap = Draft |
| Diterima | Tahap = Diterima, Lokasi = Di Toko |
| Dalam pengecekan | Tahap = Dalam Pengecekan |
| Menunggu dikirim | Tahap = Menunggu Dikirim (ekspedisi) / Menunggu Diambil Sales |
| Dikirim ke supplier / principal | Tahap = Di Supplier, Lokasi = Di Ekspedisi |
| Dibawa sales supplier | Tahap = Di Supplier, Lokasi = Dibawa Sales |
| Diterima pihak supplier / principal | Tahap = Di Supplier, Lokasi = Di Pabrik |
| Dalam proses service | Tahap = Di Supplier, Lokasi = Di Pabrik, Diagnosis = disetujui service (dikerjakan) |
| Menunggu keputusan customer | Tahap = Menunggu Keputusan Customer, Biaya = Menunggu Persetujuan |
| Selesai diperbaiki | Hasil = Selesai Diperbaiki |
| Diganti baru | Hasil = Diganti Baru |
| Ditolak | Hasil = Ditolak, Garansi = Ditolak (jika karena garansi) |
| Dibatalkan | Tahap = Dibatalkan (unit belum keluar toko) **atau** Hasil = Dibatalkan (unit kembali lalu diserahkan) |
| Siap diambil customer | Tahap = Siap Diambil, Lokasi = Kembali di Toko |
| Selesai | Tahap = Selesai, Lokasi = Diserahkan ke Customer |

Sub-status dari brief bagian 5 menjadi **Keputusan Pabrik** di diagnosis:

- Menunggu Hasil Teknisi Pabrik;
- Approved Service;
- Approved Service Berbayar;
- Approved Ganti Baru;
- Rejected;
- Tidak Bisa Diperbaiki.

### Aturan turunan

- **Selesai hanya setelah serah terima ke customer.** Contohnya: garansi ditolak, unit dikirim balik, lalu customer mengambilnya. Tahapnya berakhir di Selesai dengan Hasil = Ditolak. Penolakan klaim tidak sama dengan barang sudah diambil (pesan di Gambar 13).
- **Dibatalkan sebagai tahap** hanya bisa dipilih selama unit belum pernah keluar toko. Setelah unit keluar, pembatalan berarti "minta unit kembali", lalu jalurnya berakhir di serah terima dengan Hasil = Dibatalkan atau Dikembalikan Apa Adanya.
- **Garansi Ditolak** hanya bisa diisi dari diagnosis pabrik, atau oleh admin dengan alasan (mis. segel rusak terlihat saat terima).
- **Biaya**:
    - Garansi Aktif yang ditanggung pabrik menjadi Tanpa Biaya, kecuali ongkir yang ditagih sesuai kebijakan K-S5.
    - Ada estimasi > 0 → Menunggu Persetujuan Customer.
    - Disetujui dan sudah dibayar penuh → Sudah Dibayar.
- **Tanda "Bermasalah"** adalah tanda, bukan tahap. Ia muncul di KPI, misalnya:
    - pabrik belum memberi kabar lebih dari 14 hari setelah unit diterima;
    - customer belum menjawab estimasi lebih dari 7 hari;
    - unit siap diambil tetapi lebih dari 30 hari belum diambil;
    - paket tidak sampai.

## 4. Alur lengkap

1. **Terima** (Gambar 02–05). Admin:
    - mencari nota asal (atau memilih "tanpa nota");
    - memilih barang dari baris nota;
    - mengisi identitas unit, kondisi, keluhan, kelengkapan, dan foto.

   Simpan → Tahap Diterima, nomor service dikunci, lalu Tanda Terima dan Label Unit dicetak.
2. **Penanganan awal** (Gambar 06). Admin memilih salah satu:
    - Pemeriksaan Internal;
    - Kirim ke Pabrik/Principal;
    - Dibawa Sales Supplier;
    - Menunggu Keputusan.

   Di sini belum ada keputusan service atau ganti.
    - Pemeriksaan internal bisa berakhir "tidak ditemukan kerusakan", "diperbaiki di toko" (dengan biaya jasa), atau "perlu kirim pabrik".
3. **Unit keluar toko**, sekali saja:
    - **Ekspedisi** (Gambar 07): jalur udara / laut / darat-kurir, resi, koli, packing, ongkir, dan foto. "Konfirmasi Serah Ekspedisi" mengubah Lokasi menjadi Di Ekspedisi.
    - **Sales supplier** (Gambar 08): nama dan HP sales, jadwal, tanda tangan, dan foto. "Konfirmasi Sudah Diambil Sales" mengubah Lokasi menjadi Dibawa Sales.
4. **Pabrik menerima.** Lokasi menjadi Di Pabrik, dengan tanggal diterima dan nomor RMA bila ada.
5. **Diagnosis pabrik** (Gambar 10): keputusan, status garansi menurut pabrik, uraian, estimasi biaya, dan bukti.
6. **Biaya tambahan** (Gambar 11): bila ada estimasi, Tahap menjadi Menunggu Keputusan Customer. Customer:
    - **setuju** → pabrik diberi tahu untuk mengerjakan;
    - **tolak** → unit diminta kembali, Hasil Dikembalikan Apa Adanya;
    - **tidak menjawab** → tanda Bermasalah setelah 7 hari.
7. **Ganti baru** (Gambar 12). Dua cara:
    - **Pabrik mengirim unit pengganti.** Unit pengganti itu titipan untuk customer (bukan stok); SN lama dan SN baru dicatat.
    - **Toko menukar dari stok** saat itu juga (K-S6: hanya admin, hanya Garansi Aktif). Unit pengganti keluar dari stok toko, sedangkan unit rusak customer menjadi milik toko dan masuk Stok Karantina. Setelah itu klaimnya diteruskan lewat **Retur Supplier** (§9).
8. **Ditolak / dibatalkan / tidak jadi diproses** (Gambar 13). Alasan wajib dipilih, bukti dilampirkan, dan tindak lanjutnya dipilih: unit dikembalikan, minta periksa ulang, atau batal sebelum dikirim.
9. **Unit kembali ke toko.** Admin mencatat tanggal tiba, resi balik, dan QC toko.
    - Unit lulus → Tahap Siap Diambil, lalu customer dihubungi.
    - Unit gagal QC → kembali ke langkah 5 (klaim ulang ke pabrik).
10. **Serah terima ke customer** (Gambar 14):
    - biaya final dan pembayaran (Nota Jasa Service);
    - uji bersama customer;
    - cocokkan kelengkapan;
    - tanda tangan penerima.

    Hasilnya Tahap Selesai, Lokasi Diserahkan ke Customer, lalu Nota Hasil dicetak.

## 5. Penjelasan per Gambar

Ada enam belas mockup. Setiap mockup adalah layar desktop 1440 px, satu layar per file, disimpan di `mockups/NN_*.png` dengan sumber HTML di `mockups/src/`. Gambar 01–15 digambar ulang dari contoh owner di dalam kerangka aplikasi yang sebenarnya, yaitu sidebar dan nomor dokumen yang ada di aplikasi, dengan perbaikan dari bagian 10. Gambar 16 (Master Service) dibuat baru. Setiap penjelasan memakai format dari brief.

### Gambar 01 — Daftar Service Masuk

#### Fungsi
Menampilkan seluruh unit customer yang sedang atau pernah diservice, beserta yang perlu tindakan hari ini.

#### Dibuka dari
Menu baru **Service / Garansi** di sidebar, di antara Retur Supplier dan Riwayat Invoice.

#### Informasi yang Ditampilkan
- KPI:
    - Dalam Proses;
    - Menunggu Dikirim / Diambil Sales;
    - Di Supplier/Pabrik;
    - Menunggu Customer;
    - Siap Diambil;
    - Bermasalah.
- Tab cepat: Semua, Perlu tindakan, Aktif, Selesai, Dibatalkan.
- Pencarian satu kotak: no. service, nama customer, HP, plat, no. nota, barang, SN, resi, no. RMA.
- Filter: tanggal terima, Tahap, Garansi, Biaya, jalur penanganan, supplier/principal.
- Kolom tabel:
    - No. Service + tanggal;
    - Customer (nama, HP, plat);
    - Nota Asal + tanggal beli;
    - Barang + SN;
    - Tahap · Lokasi;
    - Garansi;
    - Biaya;
    - Hasil;
    - Umur dalam hari, yaitu lama sejak diterima.

#### Aksi Pengguna
- **+ Terima Service Baru**.
- Klik baris untuk membuka Detail (09).
- Menu ··· sesuai tahap:
    - Cetak Tanda Terima / Surat Jalan / Nota Hasil;
    - Penanganan Awal;
    - Catat Pengiriman;
    - Catat Hasil Pabrik;
    - Catat Persetujuan;
    - Serah ke Customer;
    - Batalkan (admin).

#### Catatan Programmer
- Data terhubung ke `customers`, `receipts`/`receipt_items`, dan `master_items`, dengan salinan nama, HP, dan nama barang supaya riwayat tidak berubah jika master diedit.
- Kolom "Tahap · Lokasi" menampilkan dua jalur sekaligus; Hasil, Garansi, dan Biaya punya kolom sendiri.
- Pagination di server.
- Tanda Bermasalah dihitung di query (umur di tahap, bukan cron).
- Catatan di bawah tabel: unit customer bukan stok dagangan dan tidak menambah Stok In Hand.

### Gambar 02 — Terima Service Baru: Customer & Nota Asal

#### Fungsi
Langkah 1 dari 4. Menentukan **siapa** pemilik unit dan **nota pembelian mana** yang menjadi dasar garansi.

#### Dibuka dari
Tombol **+ Terima Service Baru** di Daftar Service (01), atau tombol **Buat Service** di detail nota Riwayat Invoice. Dari Riwayat Invoice, langkah 1 langsung terisi.

#### Informasi yang Ditampilkan
- Stepper: 1 Customer & Nota, 2 Identitas Barang, 3 Penanganan, 4 Review & Simpan.
- No. Service ("dibuat saat disimpan") dan tanggal/jam terima.
- Customer: cari dari HP atau nama di Master Customer. Bila HP belum terdaftar, customer baru dibuat dari sini.
- Kendaraan: plat dan mobil, terisi dari nota asal bila dipilih.
- Kotak pencarian nota (lihat 03) dan ringkasan nota yang dipilih.
- Kunjungan: "Customer membawa lebih dari satu unit?". Setiap unit menjadi dokumen sendiri dalam satu kunjungan.

#### Aksi Pengguna
- Pilih / tambah customer.
- Cari nota (membuka panel 03).
- Pilih "Tanpa nota", disertai alasan: beli di tempat lain, nota hilang, atau hadiah.
- **Simpan Draft**, **Lanjut: Identitas Barang**, **Batalkan**.

#### Catatan Programmer
- Contoh owner mengisi nama barang, merek, SN, dan keluhan di langkah 1 **dan** di langkah 2. Isian barang dipindah ke langkah 2 saja, supaya data tidak dimasukkan dua kali.
- HP dinormalisasi dengan aturan yang sama seperti `customers.phone`.
- Draft boleh disimpan tanpa nomor service. Nomor dikunci saat disimpan sebagai Diterima, dalam transaksi `BEGIN IMMEDIATE`, sama seperti `_next_receipt_no`.

### Gambar 03 — Pencarian Nota & Validasi Garansi

#### Fungsi
Mencari nota pembelian customer dan memilih **baris barang** yang diservice. Dari baris itu sistem menghitung indikasi garansi.

#### Dibuka dari
Langkah 1, kotak "Cari nota / customer / HP / plat / SN".

#### Informasi yang Ditampilkan
- Hasil pencarian per **baris barang**, bukan per nota: no. nota, tanggal, barang, customer, plat, dan status nota (lunas / belum).
- Panel Pemeriksaan Garansi:
    - nota dan tanggal beli;
    - umur pembelian, mis. "2 bulan 6 hari";
    - masa garansi dari Master Service, mis. "Head Unit Android: 12 bulan";
    - batas garansi;
    - SN di nota bila tercatat;
    - indikasi: Garansi Aktif / Habis / Belum Diverifikasi.
- Peringatan:
    - "Garansi tidak otomatis dijamin" (segel, SN, dan jenis kerusakan tetap diperiksa pabrik);
    - baris nota sudah pernah diretur / sudah punya service aktif;
    - nota dibatalkan (void).
- Bila nota tidak ditemukan: jalur "Tanpa nota". Garansi = Belum Diverifikasi, dan dasar garansi bisa berupa kartu garansi (foto) atau tanpa bukti.

#### Aksi Pengguna
- Pilih baris → **Pakai Baris Ini**.
- Ganti pencarian.
- Admin: **Ubah status garansi** dengan alasan, mis. kartu garansi pabrik masih berlaku.

#### Catatan Programmer
- Batas garansi = tanggal nota + masa garansi. Urutan sumber masa garansi:
    1. `receipt_items.warranty_date` bila terisi (data lama);
    2. Master Service per barang;
    3. per brand;
    4. per jenis.

  Kolom garansi di Kasir sudah dihapus, jadi untuk nota baru sumbernya Master Service.
- Hasilnya disalin ke dokumen (`warranty_until`, `warranty_months`, `warranty_basis`) supaya tidak berubah jika Master Service diubah.
- Pencarian SN hanya menemukan SN yang pernah dicatat, yaitu di service sebelumnya atau di Kasir bila K-S2 disetujui.
- Nomor nota fiktif dilarang. Tanpa nota berarti `receipt_id` kosong, bukan nomor buatan.

### Gambar 04 — Detail Barang Service & Kelengkapan

#### Fungsi
Langkah 2. Mencatat identitas fisik unit: kondisinya saat diterima, keluhan, kelengkapan, dan foto. Elemen inti nota manual ada di sini.

#### Dibuka dari
Langkah 2 stepper.

#### Informasi yang Ditampilkan
- Identitas:
    - barang dan jenis service dari Master Service (terisi dari baris nota);
    - merek, model/tipe;
    - serial number (wajib bila jenis mewajibkan);
    - qty dan satuan (mis. 1 PCS, 1 pasang).
- Kondisi fisik awal: pilihan cepat (lecet, retak, penyok, bekas bongkar, segel utuh / rusak) dan teks.
- Keluhan customer dengan kata-kata customer, plus pilihan cepat dari Master Service (mati total, layar gelap, tidak ada suara, dst.).
- Checklist kelengkapan dari template jenis (Head Unit, Kabel Set, Frame, Kamera, Antena GPS, Remote, Box/Dus, Buku Manual) + "Barang lainnya".
- Khusus head unit Android: "Customer setuju unit boleh di-reset; data/akun bisa hilang" dan "Kunci layar sudah dilepas".
- Foto: depan unit, nomor seri, kelengkapan, segel/stiker garansi, dan bukti uji di toko (opsional).

#### Aksi Pengguna
- Centang kelengkapan, tambah barang lainnya.
- Unggah foto.
- **Kembali**, **Simpan Draft**, **Lanjut: Penanganan**.

#### Catatan Programmer
- Kelengkapan disimpan per baris (`service_accessories`: nama, dibawa ya/tidak, kembali ya/tidak), bukan teks. Saat serah terima, setiap baris dicek ulang.
- Foto disimpan seperti foto Retur Supplier: `uploads/services/<id>/`, base64 data URL, signature dicek dengan `matches_content_type`.
- Minimal 1 foto unit dan 1 foto SN wajib sebelum dokumen bisa disimpan sebagai Diterima. Admin boleh melewati syarat ini dengan alasan.

### Gambar 05 — Tanda Terima Service (Surat Terima)

#### Fungsi
Bukti untuk customer bahwa unitnya dititipkan ke toko. Ini pengganti nota manual.

#### Dibuka dari
Otomatis setelah langkah 4 (Simpan & Terima), atau dari menu cetak di Daftar (01) / Detail (09).

#### Informasi yang Ditampilkan
- Pratinjau 80 kolom persis seperti hasil cetak LX-310, dengan isi:
    - kepala toko, no. service, tanggal/jam;
    - data customer (nama, HP, mobil/plat);
    - nota asal dan tanggal beli;
    - status garansi ("indikasi, menunggu verifikasi pabrik");
    - data barang (merek, tipe, SN, qty);
    - kondisi fisik, keluhan, kelengkapan dalam bentuk kotak centang;
    - biaya: "Belum ditentukan, menunggu hasil pemeriksaan";
    - estimasi lama;
    - syarat singkat;
    - tanda tangan petugas dan customer.
- Panel kanan:
    - pilihan dokumen (Tanda Terima / Label Unit);
    - rangkap: lembar 1 customer, lembar 2 toko;
    - offset printer;
    - catatan "biaya belum final".

#### Aksi Pengguna
- **Cetak Tanda Terima**, **Cetak Label Unit**, **Kirim ke WhatsApp customer** (versi lanjutan), **Tutup**.

#### Catatan Programmer
- Teks polos ESC/P (bukan gambar), memakai fungsi cetak yang sama dengan `supplier_return_print_text`.
- Biaya **tidak pernah** tercetak sebagai angka final di surat terima.
- Setiap cetak dicatat di timeline (siapa, kapan, dokumen apa).
- Format lengkap ada di §11.

### Gambar 06 — Keputusan Penanganan Awal

#### Fungsi
Langkah 3. Memilih **jalur pemeriksaan**, bukan memutuskan diperbaiki atau diganti.

#### Dibuka dari
Langkah 3 stepper saat menerima, atau aksi **Penanganan Awal** untuk dokumen Diterima yang masih "Menunggu Keputusan".

#### Informasi yang Ditampilkan
- Ringkasan: dokumen, customer/barang, keluhan, keputusan akhir ("Belum ditentukan oleh pabrik").
- Empat pilihan:
    - **Pemeriksaan Internal**: teknisi toko menguji dulu;
    - **Kirim ke Pabrik/Principal**: dikemas lalu dikirim lewat ekspedisi;
    - **Dibawa Sales Supplier**: diserahkan ke sales yang datang;
    - **Menunggu Keputusan**: unit disimpan dulu, mis. menunggu supplier membalas.
- Tujuan: supplier/principal, dengan default dari Master Service (principal per brand), dan PIC/kontak.
- Catatan teknisi.
- Peringatan: "Keputusan SERVICE atau GANTI BARU belum dipilih di sini."

#### Aksi Pengguna
- Pilih jalur dan tujuan.
- **Kembali**, **Simpan & Lanjut** (ke 07 / 08, atau kembali ke Detail).

#### Catatan Programmer
- Hasil pemeriksaan internal yang tersedia:
    - "Tidak ditemukan kerusakan" → Siap Diambil, Hasil Dikembalikan Apa Adanya;
    - "Diperbaiki di toko" → biaya jasa / sparepart lewat 11 lalu 14;
    - "Perlu kirim pabrik" → pilih jalur lagi.
- Tujuan memakai tabel `suppliers` dengan tanda "bisa menerima service". Tidak perlu master principal baru.

### Gambar 07 — Pengiriman Service ke Supplier / Pabrik

#### Fungsi
Mencatat paket yang dikirim lewat ekspedisi, termasuk bukti bahwa unit customer sudah keluar dari toko.

#### Dibuka dari
Pilihan "Kirim ke Pabrik/Principal" di 06, atau aksi **Catat Pengiriman** di Detail.

#### Informasi yang Ditampilkan
- Jalur kirim: **Udara / Laut / Darat-Kurir**. Ekspedisi dipilih dari daftar (JNE, J&T, Indah Cargo, Dakota, lainnya).
- Supplier/tujuan dan alamat, wajib terisi sebelum konfirmasi.
- Nomor resi/AWB (wajib saat konfirmasi), tanggal kirim, jumlah koli, berat.
- Ongkir keluar dan penanggungnya: toko / customer / supplier.
- Packing note: jenis kemasan, pelindung, isi paket (otomatis dari kelengkapan yang ikut dikirim), catatan packing.
- Kelengkapan yang **ikut dikirim** vs **ditahan di toko**, mis. box ditahan.
- Foto: kemasan, resi/bukti serah, isi paket.
- Peringatan: "Mengisi form tidak berarti barang sudah keluar."

#### Aksi Pengguna
- **Cetak Surat Jalan Service**, **Cetak Label Koli**.
- **Simpan Draft Pengiriman**, **Konfirmasi Serah Ekspedisi**.

#### Catatan Programmer
- Konfirmasi memakai compare-and-set: Lokasi harus `di_toko`, lalu menjadi `di_ekspedisi`, dalam `BEGIN IMMEDIATE`, dengan Idempotency-Key. Unit tidak bisa "keluar" dua kali.
- Tidak ada `stock_ledger`.
- Satu dokumen bisa punya beberapa pengiriman (`service_shipments`): kirim, kirim balik, atau klaim ulang.
- Ongkir yang ditanggung toko masuk sebagai biaya service toko; yang ditanggung customer menjadi baris biaya (11).

### Gambar 08 — Serah Terima ke Sales Supplier

#### Fungsi
Jalur tanpa resi: sales supplier mengambil unit langsung di toko.

#### Dibuka dari
Pilihan "Dibawa Sales Supplier" di 06, atau aksi **Catat Serah Sales** di Detail.

#### Informasi yang Ditampilkan
- Supplier/principal, nama sales pengambil, HP sales, jadwal rencana, tanggal/jam aktual.
- Ringkasan paket: customer, nota, barang, SN, kelengkapan yang dibawa sales, packing.
- Petugas toko yang menyerahkan (petugas login).
- Tanda tangan sales (di layar) dan foto serah terima.
- Peringatan: "Barang titipan customer, bukan potongan stok."

#### Aksi Pengguna
- **Cetak Bukti Serah Terima**, **Simpan Jadwal**, **Konfirmasi Sudah Diambil Sales**.

#### Catatan Programmer
- Resi tidak wajib, tetapi tanda tangan **atau** foto wajib.
- Jadwal yang lewat 1 hari tanpa konfirmasi menjadi tanda Bermasalah ("sales belum datang").
- Compare-and-set sama dengan 07: Lokasi berubah dari `di_toko` ke `dibawa_sales`.

### Gambar 09 — Detail Service & Timeline

#### Fungsi
Satu halaman untuk melihat semua tentang satu unit: status di semua jalur, dokumen terkait, dan jejak perpindahan barang.

#### Dibuka dari
Klik baris di Daftar Service (01), atau tautan No. Service di mana pun (Riwayat Invoice, Retur Supplier, Stok Karantina).

#### Informasi yang Ditampilkan
- Ringkasan: no. service, customer + HP, nota asal, barang + SN.
- Lima kotak status: Tahap, Lokasi Unit, Garansi, Biaya, Hasil.
- Tombol aksi berikutnya yang tepat, mis. "Catat Pabrik Menerima".
- Riwayat aktivitas & dokumen: tanggal, peristiwa, oleh siapa, bukti/referensi (Tanda Terima, foto, Surat Jalan, resi, RMA, persetujuan, Nota Jasa).
- Timeline vertikal: Diterima → Pengecekan → Dikirim → Pabrik menerima → Diagnosis → Persetujuan biaya → Kembali ke toko → Serah ke customer.
- Dokumen terkait: nota asal, Nota Jasa Service, Retur Supplier / Stok Karantina (bila tukar dari stok).

#### Aksi Pengguna
- Aksi berikutnya.
- **Cetak Riwayat**, cetak tiga surat.
- **Catat Update Pabrik** (catatan bebas + foto), **Hubungi Customer** (salin pesan WA).
- Admin: Batalkan, Ubah Garansi.

#### Catatan Programmer
- Timeline dari `service_events`, ditulis di setiap aksi. Tidak ada "edit status" bebas; status hanya berubah lewat aksi.
- Staff tidak melihat biaya modal pabrik. Mekanismenya sama dengan `_for` di Retur Supplier dan `COST_KEYS`.

### Gambar 10 — Hasil Pemeriksaan Supplier / Pabrik

#### Fungsi
Mencatat diagnosis tertulis dari pabrik/principal. Keputusan final berasal dari sini.

#### Dibuka dari
Aksi **Catat Hasil Pabrik** di Detail atau Daftar. Aksi ini hanya tersedia setelah Lokasi = Di Pabrik, atau Dibawa Sales bila sales yang memeriksa.

#### Informasi yang Ditampilkan
- No. service, referensi RMA pabrik, tanggal diterima pabrik, tanggal diagnosis, pemeriksa/PIC.
- Status garansi menurut pabrik: Ditanggung / Tidak Ditanggung, beserta alasannya (segel rusak, kena air, human error, di luar masa).
- Uraian diagnosis.
- Keputusan pabrik:
    - **Bisa Diperbaiki** (gratis);
    - **Bisa Diperbaiki dengan Biaya**;
    - **Ganti Unit Baru**;
    - **Ditolak / Tidak Bisa Diperbaiki**.
- Estimasi biaya per jenis: service / sparepart / penggantian / ongkir / lainnya.
- Bukti: surat diagnosis, foto kerusakan, penawaran biaya.

#### Aksi Pengguna
- **Simpan Hasil Diagnosis**.
- **Kirim Permintaan Persetujuan Customer** (muncul bila ada biaya; menuju 11).
- **Lanjut Penggantian** (menuju 12) / **Lanjut Penolakan** (menuju 13).

#### Catatan Programmer
- Satu dokumen bisa punya beberapa diagnosis, mis. klaim ulang. Yang berlaku adalah yang terbaru dan tidak dibatalkan.
- Biaya dari pabrik adalah **modal** (`factory_cost`); harga ke customer diisi di 11. Staff hanya melihat harga ke customer.
- Garansi Ditolak dari pabrik mengubah status Garansi menjadi Garansi Ditolak.

### Gambar 11 — Persetujuan Biaya Tambahan Customer

#### Fungsi
Mencatat biaya yang baru diketahui setelah pemeriksaan, dan jawaban customer, **sebelum** pabrik mengerjakan.

#### Dibuka dari
Tombol di Hasil Pemeriksaan (10), aksi **Catat Persetujuan** di Detail, atau hasil "Diperbaiki di toko" dari pemeriksaan internal.

#### Informasi yang Ditampilkan
- Ringkasan: service, customer + HP, barang, hasil pabrik.
- Rincian biaya per baris:
    - jenis;
    - keterangan;
    - modal pabrik (admin saja);
    - **harga ke customer**.
- Total ditagih ke customer.
- Persetujuan:
    - status: Menunggu / Setuju / Tolak;
    - tanggal dihubungi;
    - media: WhatsApp / Telepon / Datang ke toko;
    - bukti: screenshot WA atau tanda tangan;
    - catatan customer;
    - batas jawab.
- Peringatan: "Jangan perintahkan pabrik mengerjakan sebelum persetujuan dicatat."

#### Aksi Pengguna
- Pilih Menunggu / Setuju / Tolak.
- **Salin Pesan WA** (pesan estimasi siap kirim), **Simpan Draft**, **Catat Persetujuan**.

#### Catatan Programmer
- Setuju → Biaya = Disetujui Customer, Tahap kembali ke Di Supplier (dikerjakan).
- Tolak → alasan wajib, Biaya = Ditolak Customer, lalu ke 13 dengan Hasil Dikembalikan Apa Adanya.
- Perubahan estimasi setelah disetujui membuat persetujuan baru. Persetujuan lama tidak diedit.
- DP tidak wajib (K-S4); admin boleh mencatat pembayaran sebagian di Nota Jasa.

### Gambar 12 — Penggantian Barang / Unit Baru

#### Fungsi
Cabang ketika pabrik memutuskan unit diganti baru.

#### Dibuka dari
Keputusan "Ganti Unit Baru" di 10.

#### Informasi yang Ditampilkan
- Persetujuan ganti dari pabrik: no. service, customer, nota asal, barang customer, hasil diagnosis, PIC pabrik.
- Cara penggantian:
    - **A · Pabrik mengirim unit pengganti**: default;
    - **B · Tukar dari stok toko sekarang**: hanya admin, hanya Garansi Aktif (K-S6);
    - **C · Ganti model lain dengan selisih harga**: bila model lama tidak ada.
- Detail unit pengganti: jenis/model, SN lama, SN baru, resi balik dari pabrik, biaya tambahan.
- Alur penerimaan unit pengganti: Pabrik setuju → Dikirim balik → Diterima toko → QC & SN → Serah ke customer.
- Peringatan: "Unit pengganti untuk customer tetap milik customer dan tidak masuk stok dagangan."

#### Aksi Pengguna
- **Lihat Dokumen RMA**, **Catat Pengiriman Balik**, **Terima Unit Pengganti**.
- Untuk cara B: **Tukar dari Stok** (admin).

#### Catatan Programmer
- **Cara A**: unit pengganti dicatat sebagai titipan (`service_replacements`), dan SN baru menggantikan SN customer untuk serah terima. Tidak ada stok.
- **Cara B**, ditulis dalam satu transaksi:
    1. OUT stok unit pengganti (`ref_type='service_tukar'`);
    2. IN + QUAR unit rusak customer (sumber karantina baru `service_klaim`, HPP = HPP unit pengganti);
    3. tautkan lot karantina ke dokumen service.

  Klaim ke supplier lalu dibuat dari lot itu lewat Retur Supplier (sumber Stok Karantina). Pengganti dari supplier masuk lewat Barang Masuk `retur_pengganti` yang sudah ada.
- **Cara C**: selisih harga menjadi baris biaya di 11.

### Gambar 13 — Service Ditolak / Dibatalkan / Tidak Jadi Diproses

#### Fungsi
Mencatat alasan yang jelas ketika unit tidak jadi diservice, diretur, atau dikirim, lalu memastikan unit tetap dilacak sampai kembali ke customer.

#### Dibuka dari
Keputusan "Ditolak" di 10, "Tolak" di 11, atau aksi **Batalkan** di Detail/Daftar.

#### Informasi yang Ditampilkan
- No. service, customer, produk.
- Asal keputusan: pabrik/supplier, customer, atau toko.
- Jenis keputusan: Garansi Ditolak, Tidak Bisa Diperbaiki, Customer Batal, Customer Tolak Biaya, Tidak Ditemukan Kerusakan, Diambil Kembali Tanpa Perbaikan, Salah Input (hanya draft).
- Tanggal keputusan dan alasan wajib.
- Tindak lanjut:
    - **Kembalikan Apa Adanya**: unit diminta kembali dari pabrik, lalu diserahkan ke customer;
    - **Minta Pemeriksaan Ulang**: eskalasi dengan bukti pendukung dan nomor eskalasi;
    - **Batalkan Sebelum Pengiriman**: hanya bila unit belum pernah keluar toko.
- Bukti: surat penolakan pabrik, bukti komunikasi customer, bukti penerimaan balik.
- Peringatan: "Klaim ditolak tidak sama dengan barang sudah diambil customer."

#### Aksi Pengguna
- **Simpan Keputusan**, **Lanjut Pengembalian Barang**.

#### Catatan Programmer
- "Batalkan Sebelum Pengiriman" langsung mengubah Tahap ke Dibatalkan, hanya jika belum ada `service_shipments` yang dikonfirmasi. Unit yang masih di toko tetap diserahkan ke customer (serah terima singkat).
- Biaya yang sudah timbul, mis. ongkir yang ditanggung customer atau biaya cek pabrik, tetap bisa ditagih sesuai kebijakan K-S5.

### Gambar 14 — Penyelesaian Service & Serah Terima Customer

#### Fungsi
Menutup dokumen: biaya final, pembayaran, uji bersama customer, kelengkapan kembali, dan tanda terima pemilik.

#### Dibuka dari
Aksi **Serah ke Customer** saat Tahap = Siap Diambil.

#### Informasi yang Ditampilkan
- Nomor service, customer, nota asal, barang + SN (SN baru bila diganti).
- Hasil akhir, hasil QC toko.
- Tanggal barang tiba kembali, tanggal/jam diambil.
- Pengambil: customer sendiri atau orang lain (nama + HP; wajib membawa tanda terima).
- Checklist kelengkapan kembali, dari kelengkapan yang diterima di 04.
- Ringkasan biaya: sparepart, jasa, ongkir, total, sudah dibayar, sisa, status pembayaran, Nota Jasa Service.
- Bukti: diuji bersama customer, kelengkapan cocok, tanda tangan penerima, nota hasil dicetak.
- Garansi service setelah perbaikan, mis. 30 hari untuk kerusakan yang sama (K-S7).

#### Aksi Pengguna
- **Buat / Buka Nota Jasa Service** (menuju Kasir, terisi otomatis).
- **Cetak Nota Hasil Service**, **Konfirmasi Serah Terima**.

#### Catatan Programmer
- Konfirmasi ditolak bila masih ada sisa tagihan. Pengecualiannya admin "Serahkan, bayar nanti", yang mencatat alasan dan menjadi piutang di Nota Jasa.
- "Selesai service" (unit siap) dan "Diambil customer" dicatat terpisah, masing-masing dengan waktu, petugas, dan tanda tangan.
- Kelengkapan yang tidak kembali harus diberi keterangan sebelum konfirmasi.

### Gambar 15 — Integrasi Service, Retur, Penggantian & Inventory

#### Fungsi
Peta untuk programmer dan owner. Halaman ini menjelaskan pemisahan barang titipan customer dan barang milik toko, serta titik-titik di mana keduanya bertemu.

#### Dibuka dari
Bukan menu. Halaman ini lampiran desain.

#### Informasi yang Ditampilkan
- Dua jejak:
    - unit customer: Nota asal → Terima service → Kirim pabrik → Diagnosis → Service/Ganti → Serah customer;
    - unit dagangan toko: PO Supplier → Barang Masuk → Stok Karantina → Retur Supplier → Barang pengganti.
- Titik temu: tukar dari stok toko (Gambar 12 cara B) dan sparepart dari stok yang dipakai di Nota Jasa.
- Tabel pemetaan dokumen, sumber data, pengaruh ke stok, dan syarat penting.

#### Aksi Pengguna
Tidak ada aksi. Halaman ini hanya rujukan.

#### Catatan Programmer
- Satu aturan: **stok dagangan hanya bergerak lewat Kasir, Barang Masuk, Stok Karantina, Retur Supplier, dan Stock Opname**. Modul service memanggil fungsi-fungsi itu dan tidak menulis ke `stock_ledger` sendiri.
- Detail lengkap ada di §9.

### Gambar 16 — Master Service

#### Fungsi
Menentukan jenis barang yang bisa diservice, beserta aturannya: masa garansi, tujuan service, template kelengkapan, keluhan umum, dan syarat foto/SN.

#### Dibuka dari
Menu **Inventori → Master Service** (baru), di bawah Master Customer. Hanya admin yang bisa mengubah.

#### Informasi yang Ditampilkan
- Daftar jenis service:
    - nama jenis (Head Unit Android, Subwoofer Aktif, Speaker, Kamera Mundur, Power Amplifier, Audio Processor/DSP, Lainnya);
    - kategori Master Kategori yang tercakup (mis. 02.01 Head Unit, 02.08 Subwoofer, 03.02 Kamera Mundur);
    - masa garansi default;
    - tujuan default;
    - jumlah barang yang terkena;
    - status.
- Panel edit:
    - nama dan kode;
    - kategori yang tercakup (multi-pilih);
    - masa garansi toko (bulan);
    - masa garansi service setelah perbaikan (hari);
    - perkiraan lama service (hari);
    - wajib SN (ya/tidak);
    - wajib foto;
    - "catat SN saat dijual di Kasir" (K-S2);
    - pertanyaan reset data (head unit);
    - template kelengkapan (urutan bisa diatur);
    - keluhan cepat;
    - syarat garansi yang tercetak (segel, stiker, tidak kena air).
- Pengecualian per brand / per barang: tujuan service (principal/service center) dan masa garansi berbeda, mis. "AVIX → ASTA JAYA, 12 bulan".

#### Aksi Pengguna
- **+ Jenis Service**, edit, nonaktifkan.
- Tambah pengecualian brand/barang.
- Lihat barang yang tercakup (tautan ke Master Barang dengan filter).

#### Catatan Programmer
- `service_profiles` + `service_profile_categories` + `service_profile_overrides` (brand_id / master_item_id).
- Sebuah barang termasuk "bisa diservice" bila kategorinya (atau induk kategorinya) tercakup, atau bila ada override per barang.
- Data tidak dihapus, hanya dinonaktifkan, supaya dokumen lama tetap merujuk ke baris yang ada.
- Dokumen service menyalin aturan yang berlaku saat diterima.

## 6. Status garansi: aturan hitung

| Kondisi | Status |
|---|---|
| Tidak ada nota (tanpa nota) dan tidak ada kartu garansi | Belum Diverifikasi |
| Ada kartu garansi pabrik (foto) tanpa nota toko | Belum Diverifikasi, dengan dasar "kartu garansi". Admin boleh mengubahnya ke Aktif dengan alasan |
| Nota ditemukan, tanggal terima ≤ batas garansi | Garansi Aktif (indikasi) |
| Nota ditemukan, tanggal terima > batas garansi | Garansi Habis |
| Pabrik: tidak ditanggung, atau admin: segel rusak | Garansi Ditolak |
| Nota ditemukan tetapi void / baris sudah diretur penuh | Belum Diverifikasi + peringatan |

Batas garansi = tanggal nota (waktu toko) + masa garansi. Umur pembelian ditampilkan dalam bulan dan hari.

## 7. Status biaya: aturan

| Kejadian | Status Biaya |
|---|---|
| Diterima, belum ada diagnosis | Menunggu Estimasi (atau Tanpa Biaya bila Garansi Aktif dan kebijakan K-S5 menanggung semua) |
| Diagnosis: diperbaiki gratis / ganti baru tanpa biaya | Tanpa Biaya |
| Diagnosis atau perbaikan internal dengan biaya > 0 | Menunggu Persetujuan Customer |
| Customer setuju | Disetujui Customer (belum dibayar) |
| Customer tolak | Ditolak Customer |
| Nota Jasa Service lunas | Sudah Dibayar |

Biaya modal pabrik (yang dibayar toko ke pabrik) dicatat terpisah dari harga ke customer. Utang ke pabrik mengikuti kebijakan K-S3.

## 8. Struktur data (usulan)

Semua tabel baru di SQLite, dibuat dengan migrasi di `init_db` seperti modul lain. Nama kolom mengikuti gaya `supplier_returns`.

| Tabel | Isi penting |
|---|---|
| `service_profiles` | Master Service: code, name, warranty_months, after_service_warranty_days, est_days, require_serial, require_photo, record_serial_at_sale, ask_reset, terms_text, checklist_json, complaints_json, default_supplier_id, status |
| `service_profile_categories` | profile_id, category_id |
| `service_profile_overrides` | profile_id, brand_id / master_item_id, warranty_months, supplier_id |
| `service_orders` | header: id, service_no (unik, dikunci saat diterima), visit_id, stage, location, result, warranty_status, warranty_basis, warranty_months, warranty_until, fee_status, flags; customer_id + customer_name/phone (salinan); plate, vehicle; receipt_id, receipt_item_id, purchase_date; profile_id, master_item_id, brand_id, product_name, model, serial_no, qty, unit; condition_in, complaint, reset_consent; handling (internal/kirim/sales/menunggu), supplier_id, pic; received_at/by, ready_at, picked_up_at/by, picker_name/phone, condition_out, after_warranty_until; fee_receipt_id; cancel_reason; version, created_at/by, updated_at/by |
| `service_accessories` | order_id, name, brought (0/1), sent (0/1), returned (0/1), note, position |
| `service_photos` | order_id, stage (masuk, sn, kelengkapan, segel, kemasan, resi, serah_sales, diagnosis, penolakan, serah_customer, ttd), path, created_by/at |
| `service_shipments` | order_id, direction (keluar / balik), mode (udara, laut, darat, sales), courier, awb, koli, weight, packing_json, ship_cost, cost_bearer, sales_name/phone, scheduled_at, handed_at/by, received_at (pabrik / toko), status |
| `service_diagnoses` | order_id, rma_no, received_at, diagnosed_at, examiner, factory_warranty (ditanggung / tidak), reject_reason, decision, description, is_current |
| `service_cost_lines` | order_id, diagnosis_id, kind (service, sparepart, penggantian, ongkir, lainnya), description, factory_cost (modal, rahasia), customer_price |
| `service_approvals` | order_id, status (menunggu, setuju, tolak), contacted_at, medium, proof_photo_id, note, total_at_approval, decided_by/at |
| `service_replacements` | order_id, mode (pabrik, stok_toko, ganti_model), old_serial, new_serial, master_item_id, return_awb, received_at, qc_note, stock ref (ledger id), quarantine_lot_id, supplier_return_id |
| `service_events` | order_id, at, by, kind, note, ref (timeline) |
| `service_visits` | id, customer_id, received_at: mengikat beberapa unit satu kunjungan |

Tambahan pada tabel lama:

- `suppliers.accepts_service` (0/1);
- sumber karantina baru `service_klaim`;
- `receipts.service_order_id` (Nota Jasa Service);
- `receipt_items.serial_no` (opsional, bila K-S2 disetujui);
- `factory_cost` masuk ke `COST_KEYS`.

## 9. Relasi ke modul lain

| Relasi | Cara | Aturan |
|---|---|---|
| **Invoice (nota penjualan)** | `service_orders.receipt_id` + `receipt_item_id` | Satu baris nota bisa punya banyak service dari waktu ke waktu, tetapi hanya satu yang aktif per unit (per SN). Nota lama tidak pernah diedit oleh modul service. Nota void → peringatan, garansi Belum Diverifikasi |
| **Customer** | `service_orders.customer_id` → `customers`, dengan salinan nama/HP | Customer baru boleh dibuat dari form service. Riwayat service tampil di Master Customer (tab baru "Service") |
| **Supplier / principal** | `service_orders.supplier_id` → `suppliers` (dengan `accepts_service`); default dari Master Service / override brand | Alamat supplier wajib ada sebelum Konfirmasi Serah Ekspedisi (sama seperti Retur Supplier) |
| **Penggantian barang** | `service_replacements` | Cara A titipan: tidak ada stok. Cara B tukar stok: OUT + IN/QUAR dalam satu transaksi. Cara C: selisih harga sebagai biaya |
| **Retur Supplier** | Hanya lewat cara B: lot karantina `service_klaim` → Retur Supplier (sumber Stok Karantina, tipe Klaim Garansi). `supplier_returns` menyimpan `service_order_id` untuk tautan balik | Service customer **tidak pernah** membuat Retur Supplier otomatis. Retur Supplier tetap untuk barang milik toko |
| **Pembayaran biaya service** | Nota Jasa Service di Kasir: `receipts` dengan `service_order_id`, baris `item_type='jasa'` (dan baris barang bila memakai sparepart dari stok) | Uang masuk lewat `receipt_payments` seperti biasa, dihitung per tanggal bayar. Status Biaya = Sudah Dibayar bila nota jasa lunas |
| **Stok** | Tidak ada untuk unit customer. Ada hanya untuk: sparepart dari stok (lewat Nota Jasa → OUT Kasir) dan tukar stok (cara B) | Service tidak menulis `stock_ledger` sendiri; ia memanggil fungsi Kasir / Karantina yang sudah ada |
| **Utang ke pabrik** | `service_cost_lines.factory_cost` | Versi 1: dicatat sebagai biaya, dibayar di luar sistem. Versi lanjutan: Faktur Pembelian jasa dari supplier (K-S3) |

## 10. Panduan untuk programmer

### Sudah Benar (dari contoh gambar owner, dipertahankan)

- Barang customer dipisah tegas dari stok dagangan (01, 08, 12, 15).
- Penanganan awal hanya memilih jalur; keputusan service atau ganti datang dari diagnosis pabrik (06, 10).
- Status garansi dari nota hanya indikasi ("Jangan otomatis menjamin garansi", 03).
- Garansi dicek per baris barang di nota, bukan per nama customer (03).
- Persetujuan customer wajib sebelum pekerjaan berbayar (11).
- Mengisi form kirim tidak sama dengan barang sudah keluar; ada konfirmasi tersendiri (07).
- Jalur sales tanpa resi, dengan tanda tangan/foto sebagai bukti (08).
- "Tolak klaim ≠ barang sudah diambil" (13).
- Selesai service dan diambil customer dicatat terpisah (14).
- Penerimaan unit pengganti dicatat sekali (12).

### Perlu Revisi (dari contoh gambar)

| Gambar | Revisi | Alasan |
|---|---|---|
| Semua | Sidebar mengikuti aplikasi: tidak ada menu "Pembayaran Supplier", "Master Data", atau "Laporan"; Master Service masuk ke Inventori | Konsisten dengan aplikasi yang sebenarnya |
| Semua | Nomor nota `PJSG/YY/MM/NNNN`; nomor service `SVC-YYYYMMDD-NNN` | Format asli aplikasi dan pola nomor dokumen lain |
| 01 | Satu kolom "Status Proses" dipecah menjadi Tahap · Lokasi, Garansi, Biaya, Hasil | Status tidak bercampur (§3) |
| 02 / 04 | Nama barang, merek, SN, dan keluhan hanya di langkah 2 | Di contoh, isian barang muncul di langkah 1 dan 2 |
| 02 / 03 | Pencarian nota adalah bagian langkah 1, bukan dropdown nomor nota | Nota dicari dari nama, HP, plat, atau SN, lalu dipilih per baris barang |
| 05 | Surat terima dalam format 80 kolom LX-310, bukan layout A4 berkotak | Printer toko adalah dot-matrix continuous form (seperti Nota Retur) |
| 07 | Ditambah: kelengkapan yang ikut / ditahan, penanggung ongkir, alamat tujuan | Kelengkapan yang ditahan di toko harus kembali ke customer |
| 11 | Media persetujuan berupa pilihan + bukti foto; modal pabrik dipisah dari harga customer | Bukti sengketa; privasi modal |
| 12 | Ditambah cara B (tukar dari stok toko) dan C (ganti model lain) | Kejadian umum di toko variasi |
| 13 | "Batalkan Sebelum Pengiriman" hanya muncul bila unit belum keluar | Pembatalan setelah unit keluar = minta unit kembali |
| 14 | Pembayaran lewat Nota Jasa Service; pengambil selain customer dicatat | Kas satu jalur; bukti pengambilan |

### Rekomendasi

- **Register titipan (custody).** Setiap unit customer selalu punya Lokasi. Laporan "unit customer di luar toko" bisa ditarik kapan saja, misalnya "ada 5 unit di ASTA JAYA, 2 di atas 14 hari".
- **Aksi, bukan edit status.** Status hanya berubah lewat tombol aksi. Setiap aksi menulis `service_events`.
- **Label unit** dicetak saat terima, supaya unit tidak tertukar.
- **Pesan WA siap salin** untuk estimasi biaya dan unit siap diambil. Versi lanjutan bisa lewat bot Telegram/WA.
- **Pengingat**:
    - tidak ada kabar dari pabrik lebih dari 14 hari;
    - customer belum menjawab lebih dari 7 hari;
    - unit belum diambil lebih dari 30 hari.
- **SN dicatat di Kasir** untuk barang yang bisa diservice (K-S2), supaya klaim berikutnya bisa dicari dari SN dan garansi tidak bisa dipakai untuk unit yang dibeli di tempat lain.
- **Unit pinjaman** (fitur lanjutan): head unit cadangan dipinjamkan selama service dan dicatat sebagai titipan terbalik.
- **Riwayat service per customer dan per SN** di Master Customer dan di pencarian.

### Keputusan owner (diputuskan 19-10-2026: ikut semua rekomendasi)

| Kode | Keputusan | Yang diterapkan |
|---|---|---|
| K-S1 | Masa garansi per jenis barang | Diatur di Master Service, dengan pengecualian per brand / per barang. Nilai awal memakai contoh di mockup: Head Unit 12 bln, Subwoofer 12, Speaker 6, Kamera 6, Amplifier 12, DSP 12, Elektronik lainnya 3. Owner bisa mengubahnya kapan saja |
| K-S2 | Serial number di Kasir | Ya, opsional per jenis. Jenis yang centang "Catat SN saat dijual" (default: Head Unit Android) menampilkan kolom SN di baris Kasir |
| K-S3 | Penagihan biaya service | Lewat Nota Jasa Service di Kasir (`receipts.service_order_id`); uang masuk lewat `receipt_payments` seperti biasa |
| K-S4 | DP biaya service | Tidak wajib. Admin boleh mencatat DP di Nota Jasa (pembayaran sebagian) |
| K-S5 | Ongkir kirim pabrik | Garansi aktif: ditanggung toko. Non-garansi atau garansi ditolak: ditanggung customer dan ditagih di Nota Jasa |
| K-S6 | Tukar dari stok toko (cara B) | Boleh, hanya admin dan hanya bila Garansi Aktif. Masuk versi 1 |
| K-S7 | Garansi setelah service | 30 hari untuk kerusakan yang sama (default di Master Service, bisa diubah per jenis) |
| K-S8 | Syarat di Tanda Terima | Unit tidak diambil 90 hari setelah dikabari bukan tanggung jawab toko; data head unit bisa hilang; tanda terima wajib dibawa. Teks syarat bisa diubah di Master Service |
| K-S9 | Margin biaya service | Harga ke customer boleh lebih besar dari modal pabrik; modal hanya terlihat admin |
| K-S10 | Barang yang tidak dibeli di toko | Diterima sebagai non-garansi berbayar (jalur "Tanpa nota", alasan "beli di tempat lain") |

## 11. Format tiga surat service (Epson LX-310, 80 kolom)

Prinsipnya sama dengan Nota Retur Supplier:

- teks polos ESC/P;
- 80 kolom pada 10 cpi;
- continuous form 9,5" × 11".
- Rangkap:
    - Tanda Terima: 2 rangkap (customer, toko);
    - Surat Jalan: 2 rangkap (supplier, toko);
    - Nota Hasil: 2 rangkap (customer, toko).

  Lembar ketiga continuous form bisa disobek sebagai label unit.

### 1. Tanda Terima Service

```
SINAR GALAKSI VARIASI MOBIL                            TANDA TERIMA SERVICE
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


  (   andi   )                                       (   Budi Santoso   )
```

Label unit dicetak di potongan bawah atau di lembar ketiga, dengan isi: `SVC-20261016-001 · Budi Santoso · 0812-3456-7890 · HU12345678 · 16/10`.

### 2. Surat Jalan Service ke Supplier / Principal

```
SINAR GALAKSI VARIASI MOBIL                     SURAT JALAN SERVICE / GARANSI
Jl. ............................ Telp. ..........  No.  : SJS-20261017-001
                                                   Tgl. : 17/10/2026
Kepada : ASTA JAYA - Divisi Service                Ref  : SVC-20261016-001
         Jl. ............, Jakarta Utara           Kirim: Udara - JNE YES
Up.    : Bpk. Hendra  0812 8899 1020               Resi : 0108 2410 1977 5521
--------------------------------------------------------------------------------
No Barang                        Qty  No. Seri       Garansi
 1 HEAD UNIT ANDROID 9" AVIX       1  HU12345678     Klaim garansi - dibeli
   AX-9 2/32GB                                       10/08/2026 (nota terlampir)
   Keluhan : Unit tidak menyala, layar hitam setelah diberi tegangan
   Kondisi : Bodi lecet kecil sisi kanan, segel utuh
   Ikut    : Head unit, kabel set     Ditahan di toko: Box/dus
--------------------------------------------------------------------------------
Koli: 1 (2,1 kg)  Kemasan: kardus + bubble wrap + foam
Mohon hasil pemeriksaan & estimasi biaya dikabarkan sebelum dikerjakan.

   Dikirim oleh,                Diperiksa,                 Diterima,


  (   andi   )              ( sinargalaksi88 )          (......................)
```

Surat jalan **tidak mencantumkan HP customer**. Supplier hanya menerima nama customer dan nomor service. Untuk jalur sales, baris "Kirim/Resi" diganti "Diambil: Bpk. Andi (sales) 08xx · 18/10/2026 14:00".

### 3. Nota Hasil Service / Penggantian

```
SINAR GALAKSI VARIASI MOBIL                         NOTA HASIL SERVICE
Jl. ............................ Telp. ..........  No.  : SVC-20261016-001
                                                   Selesai : 25/10/2026
Customer : Budi Santoso   0812-3456-7890           Diambil : 26/10/2026 16:30
Barang   : HEAD UNIT ANDROID 9" AVIX AX-9          Nota asal: PJSG/26/08/0125
No. Seri : HU12345678  (baru: -)
--------------------------------------------------------------------------------
Hasil    : SELESAI DIPERBAIKI
Diagnosis: Modul power rusak, board power diganti (RMA-ASTA-1088)
QC toko  : Lulus uji nyala dan audio, diuji bersama customer
--------------------------------------------------------------------------------
Rincian biaya                                                    Rp
  Sparepart - board power                                    300.000
  Jasa service pabrik                                         50.000
                                                     --------------
  Total                                                      350.000
  Dibayar (Nota Jasa PJSG/26/10/0311, cash 26/10)            350.000
  Sisa                                                             0
--------------------------------------------------------------------------------
Kelengkapan kembali: [x] Head unit [x] Kabel set [x] Box/Dus
Garansi service: 30 hari untuk kerusakan yang sama, s/d 25/11/2026.

   Diserahkan (toko),                          Diterima (customer),


  (   andi   )                                 (   Budi Santoso   )
```

Untuk **Diganti Baru**, baris SN berisi `No. Seri : HU12345678 -> baru HU55501234`, dan garansi unit pengganti mengikuti ketentuan pabrik. Untuk **Ditolak / Dikembalikan Apa Adanya**, rincian biaya hanya memuat biaya yang disetujui (mis. ongkir), lalu alasan penolakan dicetak di bawah "Hasil".

## 12. Fitur wajib versi 1 vs fitur lanjutan

| Versi 1 | Lanjutan |
|---|---|
| Master Service (jenis, kategori, garansi, kelengkapan, tujuan) | Pengingat otomatis lewat bot Telegram/WA |
| Terima service 4 langkah + foto + Tanda Terima + Label | Kirim surat ke WA customer |
| Penanganan awal, kirim ekspedisi, serah sales | Unit pinjaman |
| Hasil pabrik, persetujuan biaya, ganti baru (cara A, B, C), ditolak/batal | Faktur Pembelian jasa dari pabrik (utang biaya service) |
| Serah terima + Nota Jasa Service + Nota Hasil | Unit pinjaman |
| Detail + timeline, daftar dengan KPI dan pencarian | Laporan: lama service per supplier, rasio ditolak, unit di luar toko |

## 13. Daftar file

| File | Isi |
|---|---|
| `SERVICE_DESAIN.md` | Dokumen ini |
| `SERVICE_DESAIN.pdf` | A3 landscape: dokumen, 16 mockup dengan penjelasan, 4 flowchart |
| `mockups/NN_*.png` | 16 layar desktop |
| `mockups/src/` | Sumber mockup (`shell.py`, `app.css`, `screens_a/b/c.py`) |
| `flowcharts/FLOW_*.mmd` / `.png` | 4 flowchart Mermaid |
| `build.py` | Membangun ulang semua: `python3.11 docs/service/build.py` |

Kode v1 (dibangun 19/10/2026):

| File | Isi |
|---|---|
| `backend/database.py` | Bagian "Service / Garansi": tabel `service_*`, Master Service, alur, foto, cetak 80 kolom; `receipt_items.serial_no` |
| `backend/routes/services.py` | Endpoint `/api/services/...` |
| `frontend/index.html` | Halaman Service / Garansi (`#/service`), Master Service (`#/master-service`), tombol SN di baris Kasir |
