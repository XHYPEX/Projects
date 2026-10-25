"""Parser format booking WhatsApp.

Format yang diterima (key gak case-sensitive, urutan bebas, titik dua opsional):

    Nama Lengkap: Budi Santoso
    Email: budi@gmail.com
    No Telp: 08123456789
    Tanggal Booking: 05-10-2026
    Jam Booking: 14:00

Opsional: "Durasi: 90" (menit) dan "Catatan: ...". Pesan dianggap booking kalau
ada Nama Lengkap plus Tanggal/Jam Booking; chat biasa diabaikan.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Dicocokkan dari yang terpanjang dulu, jadi "nama lengkap" menang atas "nama".
_KEY_ALIASES = {
    "nama lengkap": "nama",
    "nama": "nama",
    "email": "email",
    "e-mail": "email",
    "no telp": "telp",
    "no. telp": "telp",
    "no telepon": "telp",
    "no hp": "telp",
    "telp": "telp",
    "tanggal booking": "tanggal",
    "tanggal": "tanggal",
    "tgl": "tanggal",
    "jam booking": "jam",
    "jam": "jam",
    "durasi": "durasi",
    "catatan": "catatan",
}
_KEYS_LONGEST_FIRST = sorted(_KEY_ALIASES, key=len, reverse=True)

_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d")
_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_DURATION_MINUTES = 8 * 60


@dataclass(frozen=True)
class Booking:
    name: str
    email: str
    phone: str
    start: datetime
    end: datetime
    notes: str


class BookingError(ValueError):
    """Pesan booking ada tapi isinya salah. `errors` siap dikirim ke customer."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _extract_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("*-• ").strip()
        lowered = line.lower()
        for key in _KEYS_LONGEST_FIRST:
            if lowered.startswith(key):
                rest = line[len(key):]
                # Pastikan key-nya utuh: "Jam Booking" jangan kebaca sebagai "Jam" + "Booking...".
                if rest and rest[0] not in ": \t":
                    continue
                value = rest.strip().lstrip(":").strip()
                if value:
                    fields[_KEY_ALIASES[key]] = value
                break
    return fields


def is_booking_message(text: str) -> bool:
    fields = _extract_fields(text)
    return "nama" in fields and ("tanggal" in fields or "jam" in fields)


def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[\s\-().]", "", raw)
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return None
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


def _parse_date(raw: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(raw: str) -> time | None:
    m = _TIME_RE.match(raw)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_booking(
    text: str,
    tz: ZoneInfo,
    default_duration: int = 60,
    business_start: time | None = None,
    business_end: time | None = None,
    now: datetime | None = None,
) -> Booking:
    """Parse pesan jadi Booking. Raise BookingError kalau ada field yang salah."""
    fields = _extract_fields(text)
    errors: list[str] = []

    name = fields.get("nama", "")
    if not name:
        errors.append("Nama Lengkap wajib diisi.")

    email = fields.get("email", "")
    if not email:
        errors.append("Email wajib diisi.")
    elif not _EMAIL_RE.match(email):
        errors.append(f"Email '{email}' gak valid.")

    phone = ""
    if "telp" not in fields:
        errors.append("No Telp wajib diisi.")
    else:
        phone = _normalize_phone(fields["telp"]) or ""
        if not phone:
            errors.append(f"No Telp '{fields['telp']}' gak valid. Contoh: 08123456789.")

    day = None
    if "tanggal" not in fields:
        errors.append("Tanggal Booking wajib diisi (contoh: 05-10-2026).")
    else:
        day = _parse_date(fields["tanggal"])
        if day is None:
            errors.append(f"Tanggal '{fields['tanggal']}' gak valid. Pakai format DD-MM-YYYY.")

    start_time = None
    if "jam" not in fields:
        errors.append("Jam Booking wajib diisi (contoh: 14:00).")
    else:
        start_time = _parse_time(fields["jam"])
        if start_time is None:
            errors.append(f"Jam '{fields['jam']}' gak valid. Pakai format HH:MM (24 jam).")

    duration = default_duration
    if "durasi" in fields:
        raw = re.sub(r"\s*(menit|mnt|min|m)$", "", fields["durasi"].lower())
        if raw.isdigit() and 0 < int(raw) <= MAX_DURATION_MINUTES:
            duration = int(raw)
        else:
            errors.append(f"Durasi '{fields['durasi']}' gak valid. Isi angka menit, maks {MAX_DURATION_MINUTES}.")

    if errors:
        raise BookingError(errors)

    start = datetime.combine(day, start_time, tzinfo=tz)
    end = start + timedelta(minutes=duration)

    now = now or datetime.now(tz)
    if start <= now:
        errors.append("Waktu booking sudah lewat. Pilih tanggal/jam yang akan datang.")

    if business_start and business_end:
        opens = datetime.combine(day, business_start, tzinfo=tz)
        closes = datetime.combine(day, business_end, tzinfo=tz)
        if start < opens or end > closes:
            errors.append(
                f"Di luar jam operasional ({business_start:%H:%M}-{business_end:%H:%M})."
            )

    if errors:
        raise BookingError(errors)

    return Booking(
        name=name, email=email, phone=phone, start=start, end=end, notes=fields.get("catatan", "")
    )
