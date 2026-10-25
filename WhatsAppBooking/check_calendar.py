"""Cek service account bisa akses kalender: python check_calendar.py <calendar_id>"""
import sys
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_client import SCOPES

calendar_id = sys.argv[1] if len(sys.argv) > 1 else None
if not calendar_id:
    sys.exit("Usage: python check_calendar.py <calendar_id>")

creds = service_account.Credentials.from_service_account_file("credentials/service-account.json", scopes=SCOPES)
service = build("calendar", "v3", credentials=creds, cache_discovery=False)

try:
    cal = service.calendars().get(calendarId=calendar_id).execute()
    print(f"OK  bisa baca kalender: {cal['summary']} ({cal.get('timeZone')})")
    now = datetime.now(timezone.utc)
    fb = service.freebusy().query(body={
        "timeMin": now.isoformat(), "timeMax": (now + timedelta(days=1)).isoformat(),
        "items": [{"id": calendar_id}],
    }).execute()["calendars"][calendar_id]
    if fb.get("errors"):
        sys.exit(f"GAGAL freebusy: {fb['errors']}")
    print(f"OK  cek jadwal jalan ({len(fb['busy'])} slot sibuk 24 jam ke depan)")
    # Tes izin tulis: bikin event dummy lalu langsung hapus.
    start = now + timedelta(days=365)
    event = service.events().insert(calendarId=calendar_id, body={
        "summary": "wa-booking test (auto-delete)",
        "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(minutes=5)).isoformat()},
    }).execute()
    service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
    print("OK  bisa bikin & hapus event (izin 'Make changes to events' aktif)")
except HttpError as e:
    if e.resp.status == 404:
        sys.exit("GAGAL 404: kalender gak ketemu. Cek Calendar ID, atau kalender belum di-share ke service account.")
    if e.resp.status == 403:
        sys.exit(f"GAGAL 403: {e.reason}\n(Kalau soal izin: share kalender ke service account dengan 'Make changes to events'.)")
    raise
