import hashlib
import hmac
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import whatsapp_client
from app import create_app
from service import BookingService
from state import ProcessedMessages
from whatsapp_client import WhatsAppClient, kirim_signature_check, verify_kirim_signature

SECRET = "whsec_test"
NOW = 1_800_000_000
MSG = "Nama Lengkap: Budi\nEmail: budi@gmail.com\nNo Telp: 08123456789\nTanggal Booking: 05-10-2099\nJam Booking: 14:00"


def sign(body: bytes, t: int = NOW, secret: str = SECRET) -> str:
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_signature_accepts_valid_and_rotated_secret():
    body = b'{"a":1}'
    assert verify_kirim_signature(SECRET, body, sign(body), now=NOW)
    # Saat rotasi secret Kirimdev ngirim beberapa v1; salah satu cocok udah cukup.
    rotated = sign(body, secret="old") + "," + sign(body).split(",")[1]
    assert verify_kirim_signature(SECRET, body, rotated, now=NOW)


def test_signature_rejects_tampering_replay_and_empty_secret():
    body = b'{"a":1}'
    assert not verify_kirim_signature(SECRET, b'{"a":2}', sign(body), now=NOW)
    assert not verify_kirim_signature(SECRET, body, sign(body, t=NOW - 301), now=NOW)
    assert not verify_kirim_signature(SECRET, body, "garbage", now=NOW)
    assert not verify_kirim_signature(SECRET, body, None, now=NOW)
    assert not verify_kirim_signature("", body, sign(body, secret=""), now=NOW)


class FakeCalendar:
    def __init__(self):
        self.events = []

    def is_free(self, start, end):
        return True

    def create_event(self, booking, phone, message_id):
        self.events.append(booking)


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body, reply_to=None):
        self.sent.append(to)


def test_webhook_with_kirimdev_payload_creates_event(monkeypatch):
    monkeypatch.setattr(whatsapp_client.time, "time", lambda: NOW)
    cal, wa = FakeCalendar(), FakeWhatsApp()
    service = BookingService(cal, wa, ProcessedMessages(":memory:"), ZoneInfo("Asia/Jakarta"))
    client = TestClient(create_app(service, "verify", kirim_signature_check(SECRET)))
    # Bentuk payload dari docs Kirimdev: amplop webhook Meta + blok "kirim".
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "messages": [{"from": "628111111111", "id": "wamid.X", "type": "text", "text": {"body": MSG}}],
        }}]}],
        "kirim": {"message_id": "msg_01", "contact": {"phone_number": "+628111111111"}},
    }).encode()
    headers = {"Content-Type": "application/json"}
    assert client.post("/webhook", content=body, headers=headers).status_code == 401
    resp = client.post("/webhook", content=body, headers={**headers, "X-Kirim-Signature": sign(body)})
    assert resp.status_code == 200
    assert len(cal.events) == 1 and wa.sent == ["628111111111"]


def test_kirimdev_client_posts_meta_format(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(whatsapp_client.httpx, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    WhatsAppClient.kirimdev("kd_key", "pn_1").send_text("628111", "halo", reply_to="wamid.X")
    url, kw = calls[0]
    assert url == "https://api.kirimdev.com/v1/pn_1/messages"
    assert kw["headers"] == {"Authorization": "Bearer kd_key"}
    assert kw["json"]["text"] == {"body": "halo"} and kw["json"]["to"] == "628111"
