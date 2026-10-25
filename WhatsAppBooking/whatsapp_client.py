"""Kirim pesan lewat WhatsApp Cloud API (Meta langsung atau Kirimdev) + verifikasi signature webhook."""
import hashlib
import hmac
import time
from typing import Callable, Mapping

import httpx

# (raw body, headers) -> True kalau request beneran dari provider.
SignatureCheck = Callable[[bytes, Mapping[str, str]], bool]

KIRIMDEV_API = "https://api.kirimdev.com/v1"


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Cek header X-Hub-Signature-256 biar request palsu (bukan dari Meta) ditolak."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def verify_kirim_signature(secret: str, body: bytes, header: str | None, now: float | None = None,
                           tolerance: int = 300) -> bool:
    """Header X-Kirim-Signature: "t=<unix>,v1=<hex>[,v1=...]", HMAC-SHA256 atas "<t>.<raw body>".
    Timestamp lebih tua dari `tolerance` detik ditolak biar request lama gak bisa diputar ulang."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for v in parts.get("v1", []))


def meta_signature_check(app_secret: str) -> SignatureCheck:
    return lambda body, headers: verify_signature(app_secret, body, headers.get("X-Hub-Signature-256"))


def kirim_signature_check(secret: str) -> SignatureCheck:
    return lambda body, headers: verify_kirim_signature(secret, body, headers.get("X-Kirim-Signature"))


class WhatsAppClient:
    def __init__(self, access_token: str, messages_url: str):
        self._url = messages_url
        self._headers = {"Authorization": f"Bearer {access_token}"}

    @classmethod
    def meta(cls, access_token: str, phone_number_id: str, graph_version: str) -> "WhatsAppClient":
        return cls(access_token, f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages")

    @classmethod
    def kirimdev(cls, api_key: str, phone_id: str) -> "WhatsAppClient":
        # Endpoint Kirimdev ikut format Meta, jadi payload-nya sama persis.
        return cls(api_key, f"{KIRIMDEV_API}/{phone_id}/messages")

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        resp = httpx.post(self._url, headers=self._headers, json=payload, timeout=15)
        resp.raise_for_status()
