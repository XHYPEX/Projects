# Cara Menjalankan Aplikasi Toko Plastik

## Pertama kali (sekali saja)

### 1. Pasang Python

- Buka https://www.python.org/downloads/windows/
- Klik tombol kuning **"Download Python"**
- Jalankan file yang terunduh
- **PENTING:** di layar pertama, centang **"Add python.exe to PATH"**, baru klik **"Install Now"**

> Kalau langkah ini terlewat, aplikasi tidak akan bisa jalan. Tinggal jalankan
> ulang installer-nya dan pilih "Modify" untuk memperbaiki.

### 2. Klik dua kali `Start.bat`

Selesai. Saat pertama kali, aplikasi butuh **2–3 menit** untuk menyiapkan diri
(akan terlihat banyak tulisan berjalan — itu normal). Browser akan terbuka
sendiri ke halaman aplikasi.

Di layar pertama, buat **akun admin** — username dan password ini yang dipakai
untuk masuk seterusnya.

---

## Setiap hari

Klik dua kali **`Start.bat`**. Aplikasi akan:

1. mengecek pembaruan otomatis,
2. memasang yang perlu dipasang (kalau ada),
3. membuka aplikasi di browser.

Biasanya hanya butuh beberapa detik.

> **Jangan tutup jendela hitamnya** selama memakai aplikasi — jendela itu adalah
> aplikasinya. Untuk berhenti, tutup jendela tersebut.

Kalau browser tidak terbuka sendiri, buka manual: **http://127.0.0.1:8000**

---

## Kalau ada yang salah

Foto layar jendela hitam tersebut dan kirim ke admin. Pesan errornya ada di
situ.

Aplikasi ini berjalan **hanya di komputer ini** dan tidak bisa diakses dari
komputer atau internet lain.

---

## Di mana data disimpan

Database tersimpan di:

```
C:\Users\<nama-anda>\AppData\Local\TokoPlastik\scraper.db
```

Sengaja **di luar** folder aplikasi, supaya update tidak pernah menimpa data
penjualan dan stok.

### Backup

Salin file `scraper.db` di atas ke flashdisk atau Google Drive secara berkala.
Satu file itu berisi seluruh data: penjualan, invoice, stok, dan pengguna.

Untuk memulihkan: tutup aplikasi, timpa file tersebut dengan hasil backup, lalu
jalankan `Start.bat` lagi.

---

## Untuk developer

```bash
pip install -r requirements.txt          # aplikasi web saja (4 paket)
pip install -r requirements-scraper.txt  # + Playwright/folium/pandas untuk scraper & CLI
playwright install chromium

python run.py                            # http://0.0.0.0:8000
HOST=127.0.0.1 PORT=8000 python run.py   # bind lokal saja (yang dipakai Start.bat)
```

Variabel lingkungan:

| Variabel | Default | Keterangan |
|---|---|---|
| `DB_PATH` | `data/scraper.db` | Lokasi database SQLite |
| `HOST` | `0.0.0.0` | `Start.bat` memakai `127.0.0.1` |
| `PORT` | `8000` | |
| `SCRAPER_ENABLED` | `false` | Google Maps scraper (lihat `backend/config.py`) |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` kalau diakses lewat HTTPS |

### Update otomatis di komputer klien

`Start.bat` melakukan `git fetch` + `git reset --hard origin/main` setiap kali
dijalankan — bukan `git pull`. `git pull` akan **gagal** begitu ada perubahan
lokal sekecil apa pun, dan komputer klien diam-diam berhenti mendapat update.
`reset --hard` membuang perubahan lokal sehingga update tidak pernah macet.

Aman karena database ada di luar folder repo, dan `reset --hard` tidak menyentuh
file untracked (`.venv/`, `results/`). **Jangan tambahkan `git clean`** — itu
akan menghapus `.venv/` dan memaksa pemasangan ulang tiap kali aplikasi dibuka.

Di komputer developer, buat file kosong `.no-auto-update` di folder aplikasi
untuk mematikan update otomatis:

```bash
touch .no-auto-update      # macOS/Linux
type nul > .no-auto-update  # Windows
```

Docker masih tersedia (`docker compose up -d --build`) untuk deployment server;
`Start.bat` ditujukan untuk pemakaian lokal satu komputer.
