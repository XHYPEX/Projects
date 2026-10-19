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
- Sumber pemenuhan per item, Notice Center (read-only di Dashboard), penjaga hapus supplier/merek terpakai, penjaga perubahan arti satuan, Faktur Pembelian ↔ PO, penagihan parsial SO (Gambar 03, 11, 17, 21, 26).
- Status Item Pengiriman PO (dalam_pengiriman/tiba_di_hub) yang menutup seluruh qty baris SO menggerakkan baris itu ke Dalam Pengiriman; tidak pernah mundur (Gambar 08).
- Paket Pengiriman + Master Ekspedisi: satu PO beberapa paket, satu baris PO dibagi ke beberapa paket dengan qty, tiap paket punya ekspedisi (Master Ekspedisi), jalur, resi, ETA, status; Terima Barang dan Barang Masuk wajib menyebut paket; status terlihat dari PO Supplier dan Sales Order (Gambar 08, 20).
- Serah Terima Kendaraan: nama penerima, sampai 4 foto kendaraan dan tanda tangan (opsional); file dikunci ke serah terima itu dan disembunyikan (bukan dihapus) bila serah terima dibatalkan (Gambar 13, 15, 22).
- Foto nota / bukti pembelian manual pada Service jalur "Tanpa nota" (tahap foto `nota_manual`, terkunci setelah unit diterima).
- Batal PO yang sudah dibayar: hanya Admin, harus lewat persetujuan supplier (Ajukan Batal PO). Bila disetujui, uang muka di atas tagihan baru dicatat sebagai Piutang Refund Supplier atau Kredit Supplier; refund dianggap diterima saat uang benar-benar masuk, kredit dipakai setelah nota kredit supplier dikonfirmasi. Barang yang sudah diterima lewat Retur Supplier (Gambar 07, 28).
- Diskon nota diajukan staff dan disetujui Admin seperti Harga Khusus K-22 (alasan wajib, blokir di bawah modal, konfirmasi bila digabung harga khusus, pembayaran ditahan selama pending).
- Semua perubahan stok tercatat di Riwayat Stok; semua aksi tulis tercatat di Audit Log (Gambar 24, 27).

## Berbeda dari target

| Hal | Perilaku saat ini | Gambar |
|---|---|---|
| Invoice dari SO | Dibuat otomatis saat SO terbit, bukan tombol "Lanjutkan ke Kasir" | 11 |

## Belum ada

| Hal | Gambar |
|---|---|

## Celah teknis

Semua celah teknis yang sebelumnya tercatat sudah ditutup (void Barang Masuk dijaga, posting compare-and-set, verifikasi manual dipaksa server, supplier/merek terpakai tidak bisa dihapus).
