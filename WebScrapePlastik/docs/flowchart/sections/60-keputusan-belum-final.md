# Daftar Keputusan yang Belum Final

Keputusan di bawah **tidak diambil diam-diam**. Di diagram, pilihan-pilihannya digambar abu-abu putus-putus. Rekomendasi hanya saran; pemilik toko yang memutuskan.

| No | Keputusan | Pilihan | Rekomendasi | Gambar |
|---|---|---|---|---|
| K-01 | **Titik posting stok keluar untuk SO** — ✅ DIPUTUSKAN | (b) saat baris SO Terpasang / diserahkan; 'Pengerjaan Selesai' memasang semua baris yang sudah datang | Diterapkan | 04, 09, 11, 29 |
| K-02 | **Stock In Allocation ditegakkan** — ✅ DIPUTUSKAN | Ya: dicadangkan saat SO terbit (sebagian boleh, kekurangan jadi PO); kasir ditolak, admin boleh izinkan dengan alasan; dilepas saat baris dihapus / SO batal-arsip / invoice void | Diterapkan | 04, 12, 23 |
| K-03 | **DP wajib sebelum SO dikonfirmasi** — ✅ DIPUTUSKAN | Wajib, minimal 50% dari total, tanpa pengecualian; dicek saat SO diterbitkan | Diterapkan | 02, 10 |
| K-04 | **Supplier per item vs supplier header SO** | Header mengalahkan item (saat ini) / per item saja / header hanya default | Header sebagai default, per item boleh diubah | 03 |
| K-05 | **Sumber per item (Inventory / Pesan Supplier / Campuran)** dipilih manual atau otomatis dari stok? | Manual / otomatis | Otomatis dari Available, bisa diubah | 03 |
| K-06 | **"Sedang dikerjakan" sebelum pemasangan fisik** | A: tetap Belum; B: boleh Sedang; C: tahap kerja terpisah (Persiapan → Pemasangan → QC) | C, tanpa dicampur status bayar | 13 |
| K-07 | **Tombol "Tandai Selesai"** saat ini mengisi Lunas + Pengerjaan Selesai sekaligus | Biarkan / pisah jadi "Lunasi" dan "Pengerjaan Selesai" | Pisah, sesuai aturan status terpisah | 13, 15 |
| K-08 | **Serah Terima Kendaraan** dicatat terpisah? | Ya (status/tanggal sendiri) / tidak | Ya | 13, 15, 30 |
| K-09 | **Mobil keluar dengan piutang** | Boleh dengan izin admin / tidak boleh | Boleh dengan izin admin + tercatat | 15 |
| K-10 | **Terima Barang di PO vs Barang Masuk** — mana yang memajukan SO? | Terima PO (saat ini) / posting Barang Masuk | Posting Barang Masuk, agar stok tidak dipotong sebelum masuk | 09, 29 |
| K-11 | **Barang lebih / rusak saat Barang Masuk** | Tolak / terima lebih sebagai stok umum / karantina barang rusak / retur supplier / selisih resmi di dokumen | Lebih: tolak default, admin bisa terima sebagai stok umum. Rusak: karantina lalu retur supplier | 09 |
| K-12 | **Sumber utang supplier**: data faktur di PO atau modul Faktur Pembelian? | PO saja / Faktur saja / Faktur ditautkan ke PO | Satu sumber, Faktur ditautkan ke PO | 17 |
| K-13 | **Pembatalan Sales Order** — ✅ DIPUTUSKAN (kebijakan resmi, per barang) | Barang INDEN: pelanggan boleh batal hanya ≤ 24 jam sejak terbit DAN sebelum PO-nya terbit (mana yang lebih dulu). Barang STOK TOKO: admin boleh batal kapan saja selama belum terpasang/diserahkan. SO campuran dinilai per barang. Admin, alasan wajib, refund ditentukan admin (sisa = biaya pembatalan), cadangan stok dilepas hanya untuk barang yang disetujui, audit log. Pembatalan Khusus (admin) bila toko/supplier gagal, barang salah, atau janji kirim dilanggar: refund penuh barang terdampak. Barang terpasang dan PO terbit tidak pernah dibatalkan otomatis | Diterapkan | 28 |
| K-14 | **Barang SO batal setelah PO dikirim supplier** | Simpan stok toko / retur supplier / tawarkan customer lain | Admin memilih per kasus, dijalankan otomatis (saat ini hanya dicatat) | 28 |
| K-15 | **Master Kendaraan** (satu customer banyak mobil) | Buat tabel sendiri / tetap di transaksi | Buat jika customer sering punya >1 mobil | 22 |
| K-16 | **Layout cetak nota** 11 × 9,5 in (lanskap) vs format sekarang 9,5 × 11 in (potret) | Lanskap / potret | Uji cetak di Epson LX-310 sebelum memutuskan | 16 |
| K-17 | **Hak akses PO/SO** (batal PO, ubah status) | Semua pengguna (saat ini) / admin | Batal PO = admin | 07, 27 |
| K-18 | **Pembayaran pending**: transfer yang belum masuk rekening dihitung uang masuk? | Langsung sah (saat ini) / status Pending lalu dikonfirmasi | Pending untuk transfer & QRIS; booking SO aktif hanya setelah sah | 02, 10 |
| K-19 | **Penagihan parsial SO**: satu baris SO boleh masuk beberapa invoice? | Tidak, 1 SO = 1 invoice (saat ini) / invoice per pemenuhan dengan batas qty | Tetap 1 SO = 1 invoice kecuali sering ada pemasangan bertahap | 11 |
| K-20 | **Refund & kelebihan bayar**: siapa menyetujui, bagaimana kas dicatat? | Admin saja (saat ini, tanpa catatan kas) / approval pemilik + catatan kas keluar | Admin + catatan kas keluar & bukti; void nota berbayar juga lewat refund tercatat | 14, 28 |
| K-21 | **Definisi omzet Dashboard** | Pembayaran masuk (saat ini) / invoice terbit / invoice final / serah terima | Tampilkan dua angka: Penerimaan Kas dan Penjualan (invoice final) | 26 |
| K-22 | **Matriks role-permission final**: posting stok, ubah harga, void, refund, approval, audit log | admin/staff (saat ini) / peran tambahan (Kasir, Gudang, Pemilik) | Susun matriks per aksi bersama operasional | 27 |
