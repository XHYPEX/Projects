"""Alur utama: pesan masuk -> parse -> cek bentrok -> bikin event -> balas customer."""
import logging
import threading
from datetime import time
from zoneinfo import ZoneInfo

from booking_parser import BookingError, is_booking_message, parse_booking

log = logging.getLogger(__name__)

_DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

FORMAT_HELP = (
    "Format booking:\n"
    "Nama Lengkap: Budi Santoso\n"
    "Email: budi@gmail.com\n"
    "No Telp: 08123456789\n"
    "Tanggal Booking: 05-10-2026\n"
    "Jam Booking: 14:00"
)


class BookingService:
    def __init__(
        self,
        calendar,
        whatsapp,
        processed,
        tz: ZoneInfo,
        default_duration: int = 60,
        business_start: time | None = None,
        business_end: time | None = None,
    ):
        self._calendar = calendar
        self._whatsapp = whatsapp
        self._processed = processed
        self._tz = tz
        self._default_duration = default_duration
        self._business_start = business_start
        self._business_end = business_end
        # Cek-bentrok + insert harus atomik, kalau gak dua customer bisa dapet slot yang sama.
        self._slot_lock = threading.Lock()

    def handle_message(self, sender: str, message_id: str, text: str) -> None:
        if not is_booking_message(text):
            return
        if not self._processed.claim(message_id):
            log.info("Pesan %s sudah pernah diproses, skip", message_id)
            return

        try:
            booking = parse_booking(
                text,
                self._tz,
                default_duration=self._default_duration,
                business_start=self._business_start,
                business_end=self._business_end,
            )
        except BookingError as e:
            errors = "\n".join(f"- {err}" for err in e.errors)
            self._reply(sender, message_id, f"Booking belum bisa diproses:\n{errors}\n\n{FORMAT_HELP}")
            return

        try:
            with self._slot_lock:
                if not self._calendar.is_free(booking.start, booking.end):
                    self._reply(
                        sender,
                        message_id,
                        f"Maaf, slot {self._format_slot(booking)} sudah terisi. "
                        "Silakan pilih jam lain dan kirim ulang format booking.",
                    )
                    return
                self._calendar.create_event(booking, sender, message_id)
        except Exception:
            log.exception("Gagal proses booking %s dari %s", message_id, sender)
            self._reply(sender, message_id, "Maaf, sistem lagi gangguan. Coba kirim ulang beberapa saat lagi ya.")
            return

        log.info("Booking dibuat: %s %s (%s)", booking.name, booking.start, sender)
        notes = f"\nCatatan: {booking.notes}" if booking.notes else ""
        self._reply(
            sender,
            message_id,
            f"Booking terkonfirmasi ✅\nNama: {booking.name}\nEmail: {booking.email}\nNo Telp: +{booking.phone}\n"
            f"Waktu: {self._format_slot(booking)}{notes}\n\n"
            "Sampai jumpa!",
        )

    def _format_slot(self, booking) -> str:
        start = booking.start.astimezone(self._tz)
        end = booking.end.astimezone(self._tz)
        return f"{_DAYS[start.weekday()]}, {start:%d-%m-%Y} {start:%H:%M}-{end:%H:%M}"

    def _reply(self, to: str, message_id: str, body: str) -> None:
        try:
            self._whatsapp.send_text(to, body, reply_to=message_id)
        except Exception:
            log.exception("Gagal kirim balasan WhatsApp ke %s", to)
