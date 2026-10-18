# Validasi Sebelum Diserahkan

| No | Pemeriksaan | Hasil | Catatan |
|---|---|---|---|
| 1 | Nomor gambar berurutan | ✅ | GAMBAR 01–30; build menolak nomor yang lompat |
| 2 | Seluruh modul tercakup | ✅ | SO, PO, Pengiriman, Barang Masuk, Kasir, Riwayat & Cetak, Faktur, 6 master, Riwayat Stok, Opname, Dashboard, Hak Akses, Pembatalan, 2 end-to-end (Flow A–D) |
| 3 | Setiap percabangan punya alur jelas | ✅ | Setiap belah ketupat berlabel YA/TIDAK atau kondisi |
| 4 | Status pembayaran dan pengerjaan tidak tercampur | ✅ | Gambar 13; perilaku "Tandai Selesai" yang mencampur keduanya ditandai K-07 |
| 5 | Stock Available dihitung benar | ✅ target | In Hand − Allocation (Gambar 04, 23); ditegakkan di kasir (K-02) |
| 6 | Booking SO tidak langsung mengurangi In Hand | ✅ | Booking hanya menambah Allocation (Gambar 04); di sistem, SO juga tidak memotong stok saat dibuat |
| 7 | Nomor PO muncul di item SO | ✅ | Gambar 06 |
| 8 | Satu SO bisa beberapa PO | ✅ | Gambar 05, 06 (satu PO per supplier; satu baris bisa terbagi ke beberapa PO) |
| 9 | Satu PO bisa memenuhi beberapa SO | ✅ | Gambar 07, 29 Flow C |
| 10 | PO tidak langsung menambah stok | ✅ | Gambar 07, 09; catatan K-10 tentang Terima Barang di PO |
| 11 | Barang Masuk tidak bisa diposting dua kali | ✅ | Gambar 09, 28; celah teknis no. 2 dicatat |
| 12 | DP SO tidak dihitung dua kali | ✅ | Gambar 10, 11 |
| 13 | Invoice berjalan mempertahankan nomor & riwayat bayar | ✅ | Gambar 14 |
| 14 | Pembatalan melepas alokasi hanya bila aman | ✅ target | Gambar 04 (skenario batal), 28; K-02 sudah diterapkan |
| 15 | Diagram tidak saling bertentangan | ✅ | Perbedaan target vs sistem selalu ditulis sebagai "target" / "saat ini", dan titik posting stok dirujuk ke satu keputusan (K-01) |

PDF dibuat dari sumber Mermaid dengan `build.py` dan tiap halaman diperiksa keterbacaannya (gambar PNG per halaman ada di `out/png/`).
