#!/usr/bin/env bash
# Update BotTelegram di VPS: pull kode terbaru, install ulang dependency kalau berubah,
# lalu restart service. Jalankan dari root project.
#
# Usage: sudo bash deploy/update.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="bottelegram"

cd "$PROJECT_DIR"

echo "==> git pull..."
git pull

echo "==> Install ulang dependency (kalau ada perubahan)..."
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "==> Restart service..."
systemctl restart "$SERVICE_NAME"
systemctl status "$SERVICE_NAME" --no-pager
