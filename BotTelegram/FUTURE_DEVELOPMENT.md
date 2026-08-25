# Future Development

Catatan rencana pengembangan lanjutan untuk `BotTelegram`.

## Selesai

### Deploy 24/7 + systemd + CI ringan

- Bot dijalankan sebagai `systemd` service (`deploy/bottelegram.service`) — auto-restart kalau crash/reboot. Setup otomatis lewat `deploy/setup.sh`, update lewat `deploy/update.sh`. Detail lengkap ada di README bagian "Deploy 24/7 (VPS + systemd)".
- CI ringan (`.github/workflows/bottelegram-ci.yml`) — syntax check & import check tiap push/PR ke folder ini, bukan auto-deploy.
- Log rotation belum ditambahkan (systemd journal sudah handle rotation secara default lewat `journald`, jadi belum mendesak).

### Reply mapping persisten

`sent_message_map` sekarang di-load/save ke `session/sent_message_map.json` (lihat `state.py`), jadi reply tetap ke-link ke pesan di channel tujuan meskipun bot di-restart. Entry lama otomatis di-prune kalau lebih dari 5000 (`MESSAGE_MAP_MAX_ENTRIES` di `config.py`) biar file gak membengkak tanpa batas.

### Observability

- Alerting: set `ALERT_CHAT_ID` di `.env` (opsional) buat nerima notifikasi Telegram kalau bot start, crash, atau gagal kirim sinyal.
- Flood control: otomatis retry (maks 3x) kalau kena `RetryAfter` dari Telegram Bot API.

### Multi-channel (sampai 5 pasangan)

Bot sekarang bisa listen sampai 5 pasangan grup/channel sumber -> channel/grup tujuan sekaligus, tiap pasangan punya filter sender sendiri-sendiri.

- Konfigurasi pindah dari `.env` (`SOURCE_CHAT`/`TARGET_CHAT`/`SOURCE_SENDER_WHITELIST` tunggal) ke `routes.json` (list of `{source_chat, target_chat, sender_whitelist}`, lihat `routes.json.example`), gampang diedit lewat `config_app.py`.
- Semua pasangan pakai `TARGET_BOT_TOKEN` yang sama (satu bot, jadi admin di semua channel tujuan) — bukan bot terpisah per pasangan.
- `sent_message_map` (reply mapping) kini di-key per `(source_chat_id, message_id)` supaya ID pesan gak tabrakan antar grup sumber (lihat `state.py`).
- `userbot.py` resolve tiap `source_chat` ke chat ID numerik saat startup dan menolak start (raise error) kalau ada 2 pasangan pakai sumber yang sama.

### Local app/website untuk konfigurasi

`config_app.py` (FastAPI, bind ke `127.0.0.1` doang) + `webapp/index.html` — app lokal buat pilih grup sumber, channel tujuan, dan filter sender lewat browser, gak perlu edit `.env` manual. Detail cara pakai di README bagian "App konfigurasi".

- Pakai session Telethon terpisah (`session/configapp.session`) dari `userbot.py`, jadi aman dijalankan bersamaan tanpa bikin bot utama crash/lock.
- Filter sender: `sender_whitelist` per pasangan di `routes.json` (set of Telegram user ID), diimplementasikan di `userbot.py` — kosong berarti ambil semua pengirim.
- Auto-restart `bottelegram` systemd service setelah save (best-effort, butuh app-nya punya izin `systemctl`; kalau gagal, tinggal restart manual).
- Akses dari luar VPS disarankan lewat SSH tunnel (`ssh -L 8000:localhost:8000 user@vps`), bukan expose port ke internet.

## Belum dikerjakan

### 1. Dukungan media tanpa caption (OCR/vision)

Sekarang pesan berupa foto TANPA caption teks otomatis di-skip (chart screenshot polos tanpa keterangan gak ikut diteruskan). Kalau grup sumber sering post sinyal dalam bentuk gambar tanpa teks, bisa ditambahkan:

- Vision (kirim gambarnya langsung ke Claude buat dibaca/diringkas), atau
- OCR sederhana buat ekstrak teks dari gambar sebelum diproses ke LLM.

### 2. Fallback / dual LLM provider

Saat ini cuma pakai Claude. Bisa ditambahkan fallback ke provider lain (mis. GPT) kalau Anthropic API lagi down/error, supaya sinyal tetap jalan gak ketahan.

### 3. Keamanan tambahan

- Enkripsi/backup file session (`session/userbot.session`, `session/configapp.session`) — kalau file ini bocor, orang lain bisa login sebagai akun Telegram kamu tanpa perlu OTP lagi.
- Rotasi `ANTHROPIC_API_KEY`/`TARGET_BOT_TOKEN` secara berkala.
- `config_app.py` belum ada authentication apa pun (murni mengandalkan "cuma bisa diakses via SSH tunnel/localhost"). Kalau nanti mau dibuka lebih luas (mis. diakses tim lain), perlu ditambah login/password.

### 4. Auto-refresh daftar sender

Sekarang daftar sender di `config_app.py` diambil dari 300 pesan terakhir tiap kali halaman dibuka/ganti grup sumber. Kalau ada sender baru yang baru mulai posting setelah itu, perlu buka ulang halaman config buat lihat dia di daftar — belum ada auto-refresh/notifikasi "ada sender baru".
