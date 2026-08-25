# BotTelegram — Signal Forwarder + LLM Polish

Alur kerja:

1. **Userbot** (login pakai akun Telegram pribadi via Telethon) listen pesan baru di grup/channel sumber (kamu cuma member biasa di situ, jadi bot biasa gak bisa baca pesannya — makanya pakai akun pribadi). Bisa listen sampai **5 pasangan** grup sumber -> channel tujuan sekaligus (lihat `routes.json`).
2. Setiap pesan baru dikirim ke **Claude** dengan instruksi pre-made (`prompts/system_prompt.txt`) untuk dirapikan formatnya. Kalau pesan bukan sinyal trading, LLM akan menandainya dan pesan otomatis di-skip (tidak diteruskan).
3. Hasil polish dikirim ke channel/grup tujuan pasangan masing-masing lewat **bot resmi** yang sama (dibuat via @BotFather).

## Setup

1. Install dependency:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` jadi `.env`, lalu isi:
   - `TELEGRAM_API_ID` & `TELEGRAM_API_HASH` — daftar dulu di https://my.telegram.org/apps (login pakai akun Telegram yang jadi member grup sumber).
   - `TARGET_BOT_TOKEN` — buat bot baru lewat @BotFather di Telegram, copy token-nya. Bot yang sama dipakai buat post ke semua channel tujuan.
   - `ANTHROPIC_API_KEY` — API key dari Anthropic Console.

3. Copy `routes.json.example` jadi `routes.json`, lalu isi minimal 1 pasangan (maksimum 5):
   - `source_chat` — ID numerik grup/channel sumber sinyal (bisa dilihat gampang lewat `config_app.py`).
   - `target_chat` — channel/grup tujuan kamu. **Tambahkan bot dari BotFather sebagai admin** di channel ini dulu supaya bisa post.
   - `sender_whitelist` — opsional, daftar Telegram user ID yang pesannya mau diambil dari grup itu. Kosongkan (`[]`) buat ambil semua pengirim.
   - Paling gampang diatur lewat `config_app.py` (lihat bagian "App konfigurasi" di bawah) daripada edit manual.

4. Jalankan pertama kali (perlu login interaktif — masukkan nomor HP & kode OTP yang dikirim ke Telegram kamu):
   ```bash
   python userbot.py
   ```
   Setelah login sukses, session tersimpan di `session/userbot.session` — run berikutnya gak perlu login ulang.

## Custom instruksi LLM

Edit `prompts/system_prompt.txt` sesuai gaya format sinyal yang kamu mau. Model diinstruksikan untuk:
- Tidak pernah mengubah angka (entry/SL/TP/leverage).
- Merapikan bahasa & format.
- Membalas `SKIP` kalau pesan bukan sinyal (chat basa-basi dll) — pesan ini otomatis tidak diteruskan.

## Deploy 24/7 (VPS + systemd)

Supaya bot gak ke-miss sinyal saat laptop mati/tutup, jalankan di VPS kecil (1 vCPU/1GB RAM cukup) pakai `systemd` biar auto-restart kalau crash atau server reboot.

1. Clone repo & isi `.env` + `routes.json` di VPS (lihat langkah Setup di atas).
2. Login interaktif sekali secara manual dulu (WAJIB, biar session Telethon ke-generate — systemd gak bisa handle input OTP):
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python3 userbot.py   # masukkan nomor HP + OTP, lalu Ctrl+C setelah "Userbot aktif"
   ```
3. Jalankan setup otomatis (bikin user sistem, pasang systemd unit, enable service):
   ```bash
   sudo bash deploy/setup.sh
   ```
4. Start service-nya:
   ```bash
   sudo systemctl start bottelegram
   sudo systemctl status bottelegram
   journalctl -u bottelegram -f   # lihat log live
   ```

**Update ke versi terbaru** (setelah `git push` dari lokal): jalankan `sudo bash deploy/update.sh` di VPS — otomatis `git pull`, install ulang dependency kalau berubah, dan restart service.

**CI**: ada GitHub Actions ringan (`.github/workflows/bottelegram-ci.yml`) yang jalanin syntax check & import check tiap push/PR ke folder ini — bukan auto-deploy, cuma early-warning kalau ada perubahan yang bikin bot gak bisa start.

## App konfigurasi (pilih sumber/tujuan/sender lewat browser)

Daripada edit `.env` manual, ada app lokal (`config_app.py`) buat pilih grup sumber, channel tujuan, dan filter sender lewat browser.

1. Pastikan dependency sudah ke-install (`pip install -r requirements.txt`, sudah termasuk `fastapi`+`uvicorn`).
2. Jalankan:
   ```bash
   python3 config_app.py
   ```
   Run pertama kali bakal minta login interaktif lagi (nomor HP + OTP) — ini WAJAR, app ini pakai session Telethon terpisah (`session/configapp.session`) dari punya `userbot.py`, supaya bisa dipakai bersamaan tanpa bikin bot utama crash.
3. Buka `http://127.0.0.1:8000` di browser.
   - Kalau dijalankan di VPS (bukan laptop), akses dari laptop kamu lewat SSH tunnel dulu, jangan expose port ini ke internet:
     ```bash
     ssh -L 8000:localhost:8000 user@ip-vps
     ```
     lalu buka `http://127.0.0.1:8000` di browser laptop kamu.
4. Di halaman itu: atur tiap pasangan (grup sumber, channel tujuan, dan opsional filter sender), klik **+ Tambah pasangan channel** kalau mau tambah sampai maksimum 5 pasangan. Kosongkan semua centang sender buat ambil pesan dari semua orang di grup itu. Klik **Simpan & Restart Bot**.
   - Kalau bot dijalankan via `systemd` (`bottelegram.service`) dan app ini punya izin `systemctl`, bot otomatis di-restart supaya config baru langsung kepakai. Kalau enggak (mis. bot masih dijalankan manual di terminal), restart manual sendiri.
5. Di halaman yang sama juga ada editor **System Prompt** — edit langsung dari browser dan klik **Simpan & Restart Bot** buat update `prompts/system_prompt.txt`. Tiap kali disimpan, versi lama otomatis di-backup ke **History Prompt** (maksimum 20 versi terakhir, disimpan di `prompts/history/`) — klik **Lihat** buat intip isi versi lama, atau **Pulihkan** buat pakai versi itu lagi.

## Alerting

Set `ALERT_CHAT_ID` di `.env` (opsional) — chat pribadi (DM ke bot tujuan kamu sendiri, kirim `/start` dulu ke bot-nya) buat nerima notifikasi kalau bot baru nyala, crash, atau gagal kirim sinyal berulang. Kosongkan kalau belum mau pakai fitur ini.

## Catatan

- Media (foto chart dengan caption) sudah didukung — caption-nya ikut di-polish, fotonya diteruskan apa adanya. Media tanpa caption teks di-skip (belum ada OCR/vision).
- Reply di grup sumber otomatis ikut jadi reply di channel tujuan pasangan yang sama (mapping ID pesan disimpan di `session/sent_message_map.json`, jadi tetap ke-link meskipun bot di-restart).
- Bot otomatis retry kalau kena flood control Telegram (`RetryAfter`), maksimal 3x percobaan.
- Multi-channel: sampai 5 pasangan grup sumber -> channel tujuan sekaligus, tiap pasangan bisa punya filter sender sendiri-sendiri. Diatur di `routes.json` (lihat `routes.json.example`), paling gampang lewat `config_app.py`. Tiap pasangan harus punya grup sumber yang beda.
- Jaga kerahasiaan file `.env`, `routes.json`, dan folder `session/` — semuanya berisi kredensial/ID sensitif (jangan pernah di-commit ke git; sudah masuk `.gitignore`).
