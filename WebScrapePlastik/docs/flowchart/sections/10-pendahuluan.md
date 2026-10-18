# Pendahuluan, Istilah & Cara Membaca

Dokumen ini adalah **blueprint workflow** sistem POS & Inventory Sinar Galaksi Variasi Mobil: dari mana proses dimulai, siapa yang menjalankan, tombol apa yang ditekan, data apa yang disimpan, kapan status/stok/uang berubah, apa yang terjadi jika ada kesalahan, dan kapan transaksi dianggap selesai.

Setiap gambar menunjukkan **alur target** (yang diinginkan). Kolom **Status Implementasi** di bawah tiap gambar membandingkan target itu dengan perilaku kode saat ini (per 18 Oktober 2026), supaya programmer tahu mana yang sudah jalan, mana yang berbeda, dan mana yang belum ada.

Dokumen ini juga memuat poin dari *Lampiran Blueprint Aturan Bisnis SGV* (Lampiran A & B, 16 Oktober 2026); keputusan di Lampiran B dipetakan ke K-01…K-22.

## Istilah modul

| Nama di website (lama) | Istilah sistem dalam dokumen ini |
|---|---|
| Preorder / PO Customer | **Sales Order Customer (SO)** — pesanan customer |
| Pembelian Supplier / PO Supplier | **Purchase Order Supplier (PO)** — pesanan pembelian ke supplier |
| Kasir | Cashier / Invoice Berjalan |
| Riwayat Invoice / Riwayat Nota | Invoice History |
| Barang Masuk | Goods Receipt (BM) |
| Faktur Pembelian | Supplier Invoice |
| Master Barang | Product Master |
| Stock Opname | Stock Count & Adjustment |

SO dan PO adalah dua dokumen berbeda. Nomor SO lama tetap bisa ditelusuri walaupun format penomoran berubah.

## Istilah stok

| Istilah | Arti |
|---|---|
| **Stock In Hand** | Barang fisik yang ada di toko. Hanya berubah lewat transaksi tercatat di Riwayat Stok. |
| **Stock In Allocation** | Bagian In Hand yang sudah dipesan (dibooking) customer lewat SO tetapi belum diserahkan/dipasang. |
| **Stock Available** | In Hand − Allocation. Ini yang boleh dijual ke customer lain. |

## Legenda bentuk

| Bentuk | Arti |
|---|---|
| Kapsul (sudut bulat penuh) | Mulai / selesai |
| Persegi | Proses / langkah sistem |
| Belah ketupat | Keputusan — setiap cabang berlabel YA/TIDAK atau kondisi yang jelas |
| Jajar genjang | Input pengguna (form, tombol, pilihan) |
| Persegi bergaris ganda | Sub-proses yang dijelaskan di gambar lain |
| Silinder | Data tersimpan (tabel, riwayat) |
| Kotak berlatar (cluster) | Tahap; dibaca berurutan mengikuti panah antartahap |

## Legenda warna

| Warna | Arti |
|---|---|
| Oranye muda | Aksi pengguna |
| Biru muda | Proses / status sistem |
| Hijau muda | Berhasil / hasil akhir yang benar |
| Kuning muda | Menunggu / sebagian / perlu tindak lanjut |
| Merah muda | Ditolak / error / warning |
| Abu-abu putus-putus | **Belum ada di sistem** atau **perlu keputusan bisnis** |

Makna juga tertulis di teks setiap kotak, jadi dokumen tetap terbaca jika dicetak hitam-putih.

## Sumber yang bisa diedit

Setiap gambar adalah file Mermaid di `docs/flowchart/diagrams/NN-nama.mmd`. Baris `%%` di atas file adalah tabel penjelasan di bawah gambar. Ubah file, lalu jalankan `python3.11 docs/flowchart/build.py` untuk membuat ulang PDF dan PNG.
