# BotTelegram — Signal Forwarder + LLM Polish

Alur kerja:

1. **Userbot** (login pakai akun Telegram pribadi via Telethon) listen pesan baru di grup/channel sumber (kamu cuma member biasa di situ, jadi bot biasa gak bisa baca pesannya — makanya pakai akun pribadi).
2. Setiap pesan baru dikirim ke **Claude** dengan instruksi pre-made (`prompts/system_prompt.txt`) untuk dirapikan formatnya. Kalau pesan bukan sinyal trading, LLM akan menandainya dan pesan otomatis di-skip (tidak diteruskan).
3. Hasil polish dikirim ke channel/grup tujuan kamu lewat **bot resmi** (dibuat via @BotFather).

## Setup

1. Install dependency:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` jadi `.env`, lalu isi:
   - `TELEGRAM_API_ID` & `TELEGRAM_API_HASH` — daftar dulu di https://my.telegram.org/apps (login pakai akun Telegram yang jadi member grup sumber).
   - `SOURCE_CHAT` — username (`@namagroup`) atau ID numerik grup/channel sumber sinyal.
   - `TARGET_BOT_TOKEN` — buat bot baru lewat @BotFather di Telegram, copy token-nya.
   - `TARGET_CHAT` — channel/grup tujuan kamu. **Tambahkan bot dari BotFather sebagai admin** di channel ini dulu supaya bisa post.
   - `ANTHROPIC_API_KEY` — API key dari Anthropic Console.

3. Jalankan pertama kali (perlu login interaktif — masukkan nomor HP & kode OTP yang dikirim ke Telegram kamu):
   ```bash
   python userbot.py
   ```
   Setelah login sukses, session tersimpan di `session/userbot.session` — run berikutnya gak perlu login ulang.

## Custom instruksi LLM

Edit `prompts/system_prompt.txt` sesuai gaya format sinyal yang kamu mau. Model diinstruksikan untuk:
- Tidak pernah mengubah angka (entry/SL/TP/leverage).
- Merapikan bahasa & format.
- Membalas `SKIP` kalau pesan bukan sinyal (chat basa-basi dll) — pesan ini otomatis tidak diteruskan.

## Catatan

- Media (foto chart dengan caption) sudah didukung — caption-nya ikut di-polish, fotonya diteruskan apa adanya. Media tanpa caption teks di-skip (belum ada OCR/vision).
- Jalankan proses ini terus-menerus (24/7) pakai `pm2`, `systemd`, atau `tmux`/`screen` di server/VPS supaya gak ke-miss sinyal saat laptop mati.
- Jaga kerahasiaan file `.env` dan folder `session/` — keduanya berisi kredensial sensitif (jangan pernah di-commit ke git; sudah masuk `.gitignore`).
