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

# WebScrapePlastik/data/scraper.db dulu di-track git, terus sengaja di-untrack biar
# update gak numpuk/ngerusak data user. Efek sampingnya: `git pull` yang ngelewatin
# commit itu bakal NGEHAPUS file-nya dari working tree, padahal itu database live.
# Jadi: backup sebelum pull, balikin lagi kalau kehapus.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PROJECT_DIR")"
SCRAPER_DB="$REPO_ROOT/WebScrapePlastik/data/scraper.db"
SCRAPER_DB_BACKUP=""
if [ -f "$SCRAPER_DB" ]; then
  SCRAPER_DB_BACKUP="$(mktemp "${TMPDIR:-/tmp}/scraper.db.XXXXXX")"
  cp -p "$SCRAPER_DB" "$SCRAPER_DB_BACKUP"
  echo "==> Backup scraper.db ke $SCRAPER_DB_BACKUP (git pull bisa ngehapus file ini)..."
fi

echo "==> git pull..."
git pull

# Balikin scraper.db kalau git pull tadi ngehapusnya. File-nya sudah gak di-track,
# jadi restore-an ini gak bikin working tree jadi dirty.
if [ -n "$SCRAPER_DB_BACKUP" ]; then
  if [ ! -f "$SCRAPER_DB" ]; then
    echo "==> scraper.db kehapus sama git pull, dibalikin dari backup..."
    mkdir -p "$(dirname "$SCRAPER_DB")"
    cp -p "$SCRAPER_DB_BACKUP" "$SCRAPER_DB"
  fi
  rm -f "$SCRAPER_DB_BACKUP"
fi

echo "==> Install ulang dependency (kalau ada perubahan)..."
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# prompts/system_prompt.txt gak di-track git (biar editan user lewat config_app.py
# gak ketimpa tiap deploy), jadi kalau belum ada -- mis. install baru, atau file-nya
# kehapus waktu pindah dari versi lama yang masih nge-track file ini -- bikin lagi
# dari template. Yang sudah ada TIDAK ditimpa.
if [ ! -f "$PROJECT_DIR/prompts/system_prompt.txt" ]; then
  echo "==> prompts/system_prompt.txt belum ada, copy dari .example..."
  cp "$PROJECT_DIR/prompts/system_prompt.txt.example" "$PROJECT_DIR/prompts/system_prompt.txt"
fi

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
