"""Wrapper tipis Google Calendar API (service account)."""
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build

from booking_parser import Booking

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarClient:
    def __init__(self, credentials_file: str, calendar_id: str, timezone: str):
        creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._calendar_id = calendar_id
        self._timezone = timezone

    def is_free(self, start: datetime, end: datetime) -> bool:
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self._calendar_id}],
        }
        result = self._service.freebusy().query(body=body).execute()
        calendar = result["calendars"][self._calendar_id]
        if calendar.get("errors"):
            raise RuntimeError(f"Gagal cek kalender: {calendar['errors']}")
        return not calendar["busy"]

    def create_event(self, booking: Booking, customer_phone: str, message_id: str) -> str:
        description = (
            f"Customer: {booking.name}\nEmail: {booking.email}\nNo Telp: +{booking.phone}\n"
            f"WhatsApp pengirim: +{customer_phone}"
        )
        if booking.notes:
            description += f"\nCatatan: {booking.notes}"
        body = {
            "summary": f"Appointment: {booking.name}",
            "description": description,
            "start": {"dateTime": booking.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": booking.end.isoformat(), "timeZone": self._timezone},
            "extendedProperties": {"private": {"wa_message_id": message_id, "wa_phone": customer_phone}},
        }
        event = self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return event["htmlLink"]
