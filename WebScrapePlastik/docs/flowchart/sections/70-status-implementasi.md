# Ringkasan Status Implementasi

Perbandingan alur target dengan kode saat ini. Detail ada di kolom Status Implementasi tiap gambar.

## Sudah sesuai

- Status pembayaran dan status pengerjaan terpisah; 9 kombinasi valid (Gambar 13).
- Invoice berjalan: nomor tetap, riwayat pembayaran tetap, tambah/ganti/hapus barang, penjaga edit bersamaan (409) (Gambar 14).
- DP SO pindah ke invoice satu kali, tanpa dobel (Gambar 10, 11).
- Satu SO = satu invoice; satu SO bisa beberapa PO; satu PO bisa beberapa SO dengan referensi per baris (Gambar 05, 06, 29).
- Draft PO otomatis per supplier dan idempoten (Gambar 05).
- PO tidak menambah stok; stok bertambah hanya saat Barang Masuk diposting (Gambar 07, 09).
- Stock Opname dengan snapshot, verifikasi admin, cek stok bergerak, cegah posting ganda (Gambar 25).
- Semua perubahan stok tercatat di Riwayat Stok; semua aksi tulis tercatat di Audit Log (Gambar 24, 27).

## Berbeda dari target

| Hal | Perilaku saat ini | Gambar |
|---|---|---|
| Invoice dari SO | Dibuat otomatis saat SO terbit, bukan tombol "Lanjutkan ke Kasir" | 11 |
| Status pengiriman per item PO | Tidak menggerakkan baris SO ke Dalam Pengiriman | 08 |

## Belum ada

| Hal | Gambar |
|---|---|
| Sumber pemenuhan per item (Inventory / Supplier / Campuran) | 03 |
| Notice Center (belum dipesan, pengiriman terlambat, datang sebagian, utang supplier, dll.) | 26 |
| (selesai) Serah Terima Kendaraan dan Master Kendaraan sudah ada; belum ada: lampiran foto/tanda tangan saat serah terima | 13, 15, 22 |
| Balik bayar PO ke supplier saat PO dibatalkan setelah dibayar | 07, 28 |
| Tautan Faktur Pembelian ↔ PO / Barang Masuk; riwayat per pembayaran faktur | 17 |
| Penagihan parsial SO (beberapa invoice per SO) | 11 |
| Paket pengiriman dengan qty (satu baris PO terbagi ke beberapa paket); master ekspedisi | 08, 20 |
| Penjaga perubahan arti satuan (PCS → METER) | 21 |

## Celah teknis yang perlu diperbaiki

1. Void Barang Masuk yang barangnya sudah terjual bisa membuat stok minus (`qty_remaining` tidak pernah dikurangi penjualan).
2. Posting Barang Masuk hanya dijaga cek status, belum compare-and-set / transaksi berkunci.
3. Verifikasi Barang Masuk manual belum dipaksa server sebelum posting.
4. Supplier yang hanya dipakai PO/SO masih bisa dihapus permanen.
