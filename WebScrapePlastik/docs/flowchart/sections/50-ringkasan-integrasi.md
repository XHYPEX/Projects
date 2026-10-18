# Ringkasan Integrasi Antarmodul

| Modul | Proses masuk dari | Proses keluar ke | Modul terkait | Data yang berubah | Status yang berubah | Mengubah stok? | Mengubah pembayaran? |
|---|---|---|---|---|---|---|---|
| Sales Order | Customer, Master Customer/Barang/Supplier | Draft PO, Invoice Kasir | PO, Kasir, Dashboard | SO, baris SO, pembayaran DP | Draft/Terbit; baris: Pesanan dibuat → Sudah dipesankan → Dalam pengiriman → Sampai di toko → Terpasang | Tidak langsung. Target: alokasi. Saat ini: stok keluar lewat invoice saat baris Sampai di Toko | Ya (DP → invoice) |
| Purchase Order | SO (otomatis/picker), admin (manual) | Barang Masuk, Faktur | SO, Supplier, BM | PO, baris PO, pengiriman, pembatalan, pembayaran PO | Draft → Dipesan → Sebagian Diterima → Diterima / Batal; tahap order; status kirim per item | **Tidak** menambah stok. Catatan: Terima Barang memajukan SO sehingga invoice SO memotong stok | Ya (pembayaran supplier di PO) |
| Barang Masuk | PO / manual | Stok, PO, SO | PO, Master Barang, Riwayat Stok | Dokumen BM, stok, harga rata-rata, qty diterima PO | Draft → Diposting / Dibatalkan | **Ya** (+ saat posting, − saat void) | Tidak |
| Kasir / Invoice | Customer langsung, SO | Riwayat Invoice, Dashboard | Master Barang, Customer, SO | Invoice, baris, pembayaran, event | Bayar: Belum/Sebagian/Lunas; Kerja: Draft/Menunggu/Belum/Sedang/Selesai; Void | **Ya** (keluar saat tidak Draft; kembali saat void/hapus/retur) | **Ya** |
| Riwayat Invoice & Cetak | Kasir | Printer | Kasir | Tidak ada (cetak tidak mengubah data) | Tidak | Tidak | Ya (tambah pembayaran dari detail) |
| Faktur Pembelian | Supplier | Utang | PO, Supplier | Faktur, total dibayar | Draft → Posted / Void; Belum/Sebagian/Lunas | **Tidak** | Ya (utang supplier) |
| Stock Opname | Admin/staff | Riwayat Stok | Master Barang | Dokumen opname, stok | Draft → Menunggu Verifikasi → Disetujui / Ditolak / Dibatalkan | **Ya** (hanya saat disetujui) | Tidak |
| Master Kategori | Admin / impor CSV | Master Barang | — | Kategori | Aktif / Nonaktif / Arsip | Tidak | Tidak |
| Master Brand | Admin/staff | Master Barang (SKU) | — | Brand | Aktif / Nonaktif | Tidak | Tidak |
| Master Supplier | Admin/staff | Barang, SO, PO, BM, Faktur | — | Supplier | Aktif / Nonaktif | Tidak | Tidak |
| Master Satuan | Admin/staff | Master Barang | — | Satuan | Aktif / Nonaktif | Tidak | Tidak |
| Master Customer | Admin, Kasir, SO | Kasir, SO | — | Customer (kendaraan di transaksi) | Aktif / Nonaktif | Tidak | Tidak |
| Master Barang | Admin/staff | Semua transaksi | Semua master | Produk, harga, riwayat harga | Aktif / Nonaktif / Arsip | Hanya stok awal & penyesuaian admin | Tidak |
| Riwayat Stok | Semua transaksi stok | Master Barang (tampilan) | — | Baris riwayat | — | Mencatat, bukan mengubah | Tidak |
| Dashboard | Semua modul | — | Semua | Tidak ada (hanya baca) | Tidak | Tidak | Tidak |
| Pengguna & Audit Log | Login, setiap aksi | — | Semua | Pengguna, sesi, log | Aktif / Nonaktif; admin/staff | Tidak | Tidak |
