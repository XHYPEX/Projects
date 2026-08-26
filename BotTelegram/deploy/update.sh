#!/usr/bin/env bash
# Update BotTelegram di VPS: pull kode terbaru, install ulang dependency kalau berubah,
# lalu restart service. Jalankan dari root project.
#
# Usage: sudo bash deploy/update.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="bottelegram"
CONFIG_SERVICE_NAME="bottelegram-config"
SERVICE_USER="bottelegram"

cd "$PROJECT_DIR"

echo "==> git pull..."
git pull

echo "==> Install ulang dependency (kalau ada perubahan)..."
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# git pull di atas jalan sebagai root, jadi tiap file tracked yang berubah jadi
# milik root -- dan config_app.py (jalan sebagai $SERVICE_USER) langsung gak bisa
# nulis prompts/system_prompt.txt lagi (PermissionError pas save prompt).
echo "==> Kembalikan ownership ke $SERVICE_USER..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"

echo "==> Restart service..."
systemctl restart "$SERVICE_NAME"
# Config UI juga harus ikut di-restart, kalau enggak perubahan di config_app.py
# gak kepakai sampai ada yang restart manual.
systemctl restart "$CONFIG_SERVICE_NAME" 2>/dev/null \
  || echo "  (service $CONFIG_SERVICE_NAME belum terpasang -- jalankan 'sudo bash deploy/setup.sh')"

# status ngasih exit code non-nol kalau service lagi failed; jangan bikin script
# ikut mati di sini, biar outputnya tetap kelihatan.
systemctl status "$SERVICE_NAME" --no-pager || true
systemctl status "$CONFIG_SERVICE_NAME" --no-pager || true
