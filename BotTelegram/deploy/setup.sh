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

echo "==> Setup virtualenv & install dependencies..."
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "==> Set ownership ke $SERVICE_USER..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"

echo "==> Pasang systemd unit..."
sed "s#/opt/bottelegram#$PROJECT_DIR#g" "$PROJECT_DIR/deploy/bottelegram.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "Setup selesai. Sebelum start service, pastikan session Telethon sudah pernah login interaktif:"
echo "  sudo -u $SERVICE_USER $PROJECT_DIR/.venv/bin/python3 $PROJECT_DIR/userbot.py"
echo "(masukkan nomor HP + OTP sekali, lalu Ctrl+C setelah 'Userbot aktif' muncul)"
echo ""
echo "Setelah itu jalankan:"
echo "  systemctl start $SERVICE_NAME"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f    # buat lihat log live"
