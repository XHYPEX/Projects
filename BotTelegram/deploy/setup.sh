#!/usr/bin/env bash
# Setup awal BotTelegram di VPS (Ubuntu/Debian). Jalankan sekali dari root project
# setelah repo di-clone dan .env sudah diisi.
#
# Usage: sudo bash deploy/setup.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="bottelegram"
SERVICE_USER="bottelegram"

if [ "$EUID" -ne 0 ]; then
  echo "Jalankan pakai sudo: sudo bash deploy/setup.sh"
  exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "File .env belum ada di $PROJECT_DIR. Isi dulu (copy dari .env.example) sebelum lanjut."
  exit 1
fi

echo "==> Bikin user sistem '$SERVICE_USER' (kalau belum ada)..."
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home "$PROJECT_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Bikin folder session/ (kalau belum ada)..."
mkdir -p "$PROJECT_DIR/session"

echo "==> Setup virtualenv & install dependencies..."
# --clear: kalau .venv ini pindahan/copy-an dari lokasi lain, shebang/symlink internalnya
# nunjuk ke path lama dan bakal rusak (bad interpreter) -- --clear paksa dibikin ulang bersih.
python3 -m venv --clear "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "==> Set ownership ke $SERVICE_USER..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"

echo "==> Pasang systemd unit (bot + config UI)..."
sed "s#/opt/bottelegram#$PROJECT_DIR#g" "$PROJECT_DIR/deploy/bottelegram.service" > "/etc/systemd/system/$SERVICE_NAME.service"
sed "s#/opt/bottelegram#$PROJECT_DIR#g" "$PROJECT_DIR/deploy/config-app.service" > "/etc/systemd/system/bottelegram-config.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl enable bottelegram-config

echo "==> Kasih izin '$SERVICE_USER' buat restart/stop service-nya sendiri (dipakai config_app.py pas save routes/prompt/ganti akun)..."
cat > /etc/sudoers.d/bottelegram-service <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start $SERVICE_NAME, /usr/bin/systemctl stop $SERVICE_NAME, /usr/bin/systemctl restart $SERVICE_NAME, /usr/bin/systemctl status $SERVICE_NAME
EOF
chmod 440 /etc/sudoers.d/bottelegram-service
visudo -c -f /etc/sudoers.d/bottelegram-service

echo ""
echo "Setup selesai. Sebelum start service, pastikan session Telethon sudah pernah login interaktif:"
echo "  sudo -u $SERVICE_USER $PROJECT_DIR/.venv/bin/python3 $PROJECT_DIR/userbot.py"
echo "(masukkan nomor HP + OTP sekali, lalu Ctrl+C setelah 'Userbot aktif' muncul -- atau lakukan ini"
echo " lewat config UI di bawah, jadi gak perlu SSH sama sekali)"
echo ""
echo "Setelah itu jalankan:"
echo "  systemctl start $SERVICE_NAME"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f    # buat lihat log live"
echo ""
echo "Config UI (routes/prompt/ganti akun) jalan sebagai service terpisah, 'bottelegram-config':"
echo "  systemctl start bottelegram-config"
echo "  journalctl -u bottelegram-config -f"
echo "Pastikan CONFIG_APP_USERNAME, CONFIG_APP_PASSWORD, CONFIG_APP_SECRET_KEY sudah diisi di .env dulu,"
echo "kalau enggak service ini bakal langsung exit (lihat komentar di .env.example)."
