# Uji Cetak Nota 11" × 9,5" (landscape) — Epson LX-310

Keputusan K-16 (19-10-2026): nota Kasir berukuran **11 inci lebar × 9,5 inci tinggi (landscape)**.
Ukuran ini **tidak boleh dianggap pasti muat** di printer; arah, rotasi driver, dan ukuran harus diuji.

## Yang sudah dibangun
- Kasir → nota tersimpan → tombol **Atur Cetak** (di samping **Cetak Nota**): ukuran/arah kertas
  (Landscape 11×9,5 default, Portrait 9,5×11 sebagai cara lama), margin atas/bawah/kiri/kanan (cm),
  skala huruf (70–130%). Pengaturan disimpan di browser komputer kasir itu.
- **Cetak Halaman Uji**: lembar penggaris dengan aturan halaman yang sama persis dengan nota.
  Skala bertanda inci diukur dari *tepi kertas*, ada penanda KIRI/KANAN-ATAS/BAWAH, tulisan
  "ATAS NOTA" untuk memeriksa arah, kotak tebal pada batas area cetak, dan contoh teks nota.
- Banner di Atur Cetak tetap kuning "Belum diuji" sampai pengguna mencentang bahwa hasil uji sudah diperiksa.
- Nota landscape memuat Kendaraan (bila ada) dan "Ref. SO … (Invoice n)" untuk invoice Sales Order.
- Header tabel barang diulang bila nota lebih dari satu halaman.
- Nota teks Sales Order punya pilihan kertas "Landscape 11" × 9,5"" juga.

## Langkah uji (di komputer yang tersambung ke LX-310)
1. Buka Kasir, pilih nota tersimpan, klik **Atur Cetak** → **Simpan & Cetak Halaman Uji**.
2. Di dialog cetak: printer EPSON LX-310, ukuran kertas 9,5 × 11 in **atau** kertas khusus 11 × 9,5 in,
   skala 100%, tanpa header/footer.
3. Periksa hasil kertas:
   - "ATAS NOTA" tegak dan di tepi atas. Bila miring/terbalik: ubah orientasi di driver, ulangi.
   - Ukur skala dengan penggaris: angka 1, 2, … = inci dari tepi kertas. Catat selisih.
   - Keempat sisi kotak tebal tercetak utuh. Bila terpotong: besarkan margin sisi itu.
   - Contoh teks terbaca dan tidak terpotong di kanan.
4. Setelah hasilnya benar, buka **Atur Cetak** lagi, centang "Hasil Halaman Uji sudah saya periksa", **Simpan**.
5. Cetak satu nota asli sebagai pemeriksaan akhir.

Catat hasil ukur (arah driver, margin akhir, selisih skala) di bawah ini setelah uji:

| Tanggal | Komputer | Orientasi driver | Margin akhir (cm) A/B/Ki/Ka | Selisih skala | Catatan |
|---|---|---|---|---|---|
| | | | | | |
