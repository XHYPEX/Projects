import hashlib
import hmac
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import create_app
from service import BookingService
from state import ProcessedMessages
from whatsapp_client import meta_signature_check

TZ = ZoneInfo("Asia/Jakarta")
SECRET = "app-secret"
MSG = "Nama Lengkap: Budi\nEmail: budi@gmail.com\nNo Telp: 08123456789\nTanggal Booking: 05-10-2099\nJam Booking: 14:00"


class FakeCalendar:
    def __init__(self, free=True):
        self.free = free
        self.events = []

    def is_free(self, start, end):
        return self.free

    def create_event(self, booking, phone, message_id):
        self.events.append((booking, phone, message_id))
        return "https://calendar/event"


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body, reply_to=None):
        self.sent.append((to, body))


def make(free=True):
    cal, wa = FakeCalendar(free), FakeWhatsApp()
    service = BookingService(cal, wa, ProcessedMessages(":memory:"), TZ)
    client = TestClient(create_app(service, "verify-me", meta_signature_check(SECRET)))
    return client, cal, wa


def payload(text, msg_id="wamid.1", sender="6281234567890"):
    return {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": sender, "id": msg_id, "type": "text", "text": {"body": text}}
        ]}}]}]
    }


def post(client, data, secret=SECRET):
    body = json.dumps(data).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def test_verify_handshake():
    client, _, _ = make()
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "123"})
    assert bad.status_code == 403


def test_rejects_bad_signature():
    client, cal, _ = make()
    assert post(client, payload(MSG), secret="wrong").status_code == 401
    assert cal.events == []


def test_creates_event_and_confirms():
    client, cal, wa = make()
    assert post(client, payload(MSG)).status_code == 200
    booking, phone, msg_id = cal.events[0]
    assert booking.start == datetime(2099, 10, 5, 14, 0, tzinfo=TZ)
    assert phone == "6281234567890" and msg_id == "wamid.1"
    assert "terkonfirmasi" in wa.sent[0][1]


def test_duplicate_delivery_creates_one_event():
    client, cal, wa = make()
    post(client, payload(MSG))
    post(client, payload(MSG))
    assert len(cal.events) == 1 and len(wa.sent) == 1


def test_conflict_is_rejected():
    client, cal, wa = make(free=False)
    post(client, payload(MSG))
    assert cal.events == []
    assert "sudah terisi" in wa.sent[0][1]


def test_invalid_format_gets_help():
    client, cal, wa = make()
    post(client, payload("Nama Lengkap: Budi\nTanggal Booking: 05-10-2099"))
    assert cal.events == []
    assert "Jam Booking wajib" in wa.sent[0][1] and "Format booking" in wa.sent[0][1]


def test_ignores_normal_chat_and_status_updates():
    client, cal, wa = make()
    post(client, payload("halo kak"))
    post(client, {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]})
    assert cal.events == [] and wa.sent == []
