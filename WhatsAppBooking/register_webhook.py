"""Daftarin/ganti URL webhook di Meta. Jalanin tiap kali URL tunnel berubah:

    python register_webhook.py https://xxxx.trycloudflare.com
"""
import sys

import httpx
from dotenv import dotenv_values

env = {k: (v or "").strip() for k, v in dotenv_values(".env").items()}
G = f"https://graph.facebook.com/{env.get('WA_GRAPH_VERSION') or 'v23.0'}"

if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
    sys.exit("Usage: python register_webhook.py https://<domain>")
callback = sys.argv[1].rstrip("/").removesuffix("/webhook") + "/webhook"

app = httpx.get(f"{G}/debug_token", params={"input_token": env["WA_ACCESS_TOKEN"], "access_token": env["WA_ACCESS_TOKEN"]}, timeout=15).json()["data"]["app_id"]

health = httpx.get(callback.removesuffix("/webhook") + "/health", timeout=15)
if health.status_code != 200:
    sys.exit(f"GAGAL: {callback} gak bisa diakses ({health.status_code}). Tunnel/server jalan?")

r = httpx.post(f"{G}/{app}/subscriptions", data={
    "object": "whatsapp_business_account", "callback_url": callback,
    "verify_token": env["WA_VERIFY_TOKEN"], "fields": "messages",
    "access_token": f"{app}|{env['WA_APP_SECRET']}",
}, timeout=30).json()
if not r.get("success"):
    sys.exit(f"GAGAL daftar webhook: {r}")
print(f"OK webhook -> {callback}")
