"""Daftarin URL webhook di Kirimdev. Jalanin tiap kali URL tunnel berubah:

    python register_kirimdev_webhook.py https://xxxx.trycloudflare.com

Semua langganan lama dihapus dulu, biar Kirimdev gak terus nyoba URL tunnel yang udah mati.
Secret dari Kirimdev langsung ditulis ke .env (KIRIM_WEBHOOK_SECRET); restart server setelahnya.
"""
import sys

import httpx
from dotenv import dotenv_values, set_key

from whatsapp_client import KIRIMDEV_API

env = {k: (v or "").strip() for k, v in dotenv_values(".env").items()}

if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
    sys.exit("Usage: python register_kirimdev_webhook.py https://<domain>")
callback = sys.argv[1].rstrip("/").removesuffix("/webhook") + "/webhook"

health = httpx.get(callback.removesuffix("/webhook") + "/health", timeout=15)
if health.status_code != 200:
    sys.exit(f"GAGAL: {callback} gak bisa diakses ({health.status_code}). Tunnel/server jalan?")

headers = {"Authorization": f"Bearer {env['KIRIM_API_KEY']}"}
existing = httpx.get(f"{KIRIMDEV_API}/webhook_subscriptions", headers=headers, timeout=30)
existing.raise_for_status()
subs = existing.json()
# Semua dihapus, termasuk yang URL-nya sama: secret-nya cuma dikasih sekali, jadi gak bisa dipakai ulang.
for sub in subs if isinstance(subs, list) else subs.get("data", []):
    httpx.delete(f"{KIRIMDEV_API}/webhook_subscriptions/{sub['id']}", headers=headers, timeout=30).raise_for_status()
    print(f"Hapus langganan lama -> {sub.get('url')}")

r = httpx.post(f"{KIRIMDEV_API}/webhook_subscriptions", headers=headers, timeout=30, json={
    "url": callback,
    "events": ["message.received", "message.status"],
    "description": "wa-booking",
})
if r.status_code >= 300:
    sys.exit(f"GAGAL daftar webhook ({r.status_code}): {r.text}")
body = r.json()
secret = body.get("data", body).get("initial_secret")
if not secret:
    sys.exit(f"GAGAL: Kirimdev gak ngasih initial_secret: {r.text}")
set_key(".env", "KIRIM_WEBHOOK_SECRET", secret, quote_mode="never")
print(f"OK webhook -> {callback}\nSecret disimpan di .env. Restart server biar kepake.")
