# Future Development

Catatan rencana pengembangan lanjutan untuk `BotTelegram`. Belum diimplementasikan — ini daftar ide/prioritas buat iterasi berikutnya.

## 1. Deploy 24/7

Saat ini bot cuma jalan selama terminal `python3 userbot.py` terbuka di laptop. Supaya gak ke-miss sinyal saat laptop mati/tutup/tidur, perlu dijalankan terus-menerus di server:

- Pindahkan ke VPS kecil (mis. DigitalOcean, Vultr, Hetzner — spek minimal cukup, 1 vCPU/1GB RAM sudah lebih dari cukup).
- Jalankan sebagai service pakai `systemd` (paling robust, auto-restart kalau crash) atau `pm2` (lebih simpel setup-nya).
- Tambahkan log rotation supaya file log gak membengkak tanpa batas.

## 2. Reply mapping persisten

`sent_message_map` (ID pesan sumber → ID pesan yang dikirim bot ke channel tujuan) saat ini cuma tersimpan di memory proses. Kalau bot di-restart, mapping hilang — reply ke sinyal-sinyal lama sebelum restart gak akan ke-link jadi reply lagi di channel tujuan (tetap terkirim, cuma jadi pesan baru biasa, bukan reply).

Perbaikan: simpan mapping ke file kecil (JSON/SQLite) yang di-load ulang saat startup, supaya reply tetap ke-link meskipun proses di-restart.

## 3. Dukungan media tanpa caption (OCR/vision)

Sekarang pesan berupa foto TANPA caption teks otomatis di-skip (chart screenshot polos tanpa keterangan gak ikut diteruskan). Kalau grup sumber sering post sinyal dalam bentuk gambar tanpa teks, bisa ditambahkan:

- Vision (kirim gambarnya langsung ke Claude buat dibaca/diringkas), atau
- OCR sederhana buat ekstrak teks dari gambar sebelum diproses ke LLM.

## 4. Multi-source

Kalau nanti mau ambil sinyal dari lebih dari satu grup sumber sekaligus (bukan cuma satu `SOURCE_CHAT`), perlu:

- Ubah `SOURCE_CHAT` jadi list.
- Tandain asal grup di pesan hasil polish (opsional), atau bahkan routing ke channel tujuan berbeda per grup sumber.

## 5. Fallback / dual LLM provider

Saat ini cuma pakai Claude. Bisa ditambahkan fallback ke provider lain (mis. GPT) kalau Anthropic API lagi down/error, supaya sinyal tetap jalan gak ketahan.

## 6. Observability

- Alerting (mis. kirim pesan ke Telegram pribadi) kalau bot crash, disconnect, atau error berturut-turut.
- Handle Telegram flood control (`RetryAfter`) dengan retry otomatis, bukan cuma log error.

## 7. Keamanan tambahan

- Enkripsi/backup file session (`session/userbot.session`) — kalau file ini bocor, orang lain bisa login sebagai akun Telegram kamu tanpa perlu OTP lagi.
- Rotasi `ANTHROPIC_API_KEY`/`TARGET_BOT_TOKEN` secara berkala.

## 8. CI ringan + systemd

Project ini dipakai buat client dengan horizon maintenance 6-12 bulan ke depan dan jalan tiap hari — CI/CD penuh (staging environment, automated deploy pipeline, dst) overkill untuk skala satu bot proses tunggal di satu server. Cukup:

- **CI ringan** (GitHub Actions): jalanin `py_compile`/lint tiap push atau PR ke `main`, biar cepat ketahuan kalau ada perubahan yang bikin bot gak bisa start. Gak perlu automated deploy dulu — cukup sinyal "aman untuk di-deploy" atau "ada yang rusak".
- **systemd** di VPS: bikin unit file (`bottelegram.service`) yang jalanin `userbot.py` pakai venv project, dengan `Restart=on-failure` supaya otomatis nyala lagi kalau crash atau server reboot.
- **Deploy manual/semi-otomatis**: `git pull` + `systemctl restart bottelegram` di VPS. Baru worth upgrade ke pipeline auto-deploy penuh kalau ke depan ada banyak developer/perubahan rutin atau butuh rollback cepat & terstruktur.

## 9. Local app/website untuk konfigurasi

Rencana bikin app/website lokal (dijalankan sendiri, bukan diakses publik) buat ganti proses edit `.env` manual, isinya:

- Pilih **channel/grup sumber** dari daftar chat yang ada (mirip `list_chats.py`, tapi via UI).
- Pilih **channel/grup tujuan** dari daftar chat yang ada.
- Pilih **sender/nama orang spesifik** di grup sumber yang pesannya mau diambil (filter berdasarkan pengirim, bukan semua orang di grup) — perlu tambahan logic filter sender di `userbot.py` (`event.sender_id`) yang belum ada sekarang.

Detail teknis (stack, apakah nulis balik ke `.env` atau config terpisah, dsb) masih perlu dibahas sebelum mulai implementasi.
