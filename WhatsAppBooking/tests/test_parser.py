from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from booking_parser import BookingError, is_booking_message, parse_booking

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=TZ)

VALID = """Nama Lengkap: Budi Santoso
Email: budi@gmail.com
No Telp: 0812-3456-7890
Tanggal Booking: 05-10-2026
Jam Booking: 14:00"""


def test_parses_full_message():
    b = parse_booking(VALID, TZ, now=NOW)
    assert b.name == "Budi Santoso"
    assert b.email == "budi@gmail.com"
    assert b.phone == "6281234567890"
    assert b.start == datetime(2026, 10, 5, 14, 0, tzinfo=TZ)
    assert b.end == datetime(2026, 10, 5, 15, 0, tzinfo=TZ)


def test_colon_optional_and_case_insensitive():
    # Persis template dari user: "Tanggal Booking" tanpa titik dua.
    text = "nama lengkap:Ani\nEMAIL: ani@x.co\nNo Telp: +62 811 111 222\nTanggal Booking 2026-10-05\nJam Booking: 9.30"
    b = parse_booking(text, TZ, now=NOW)
    assert b.name == "Ani" and b.phone == "62811111222"
    assert b.start == datetime(2026, 10, 5, 9, 30, tzinfo=TZ)


def test_optional_duration_and_notes():
    b = parse_booking(VALID + "\nDurasi: 90 menit\nCatatan: servis AC", TZ, now=NOW)
    assert (b.end - b.start).seconds == 90 * 60
    assert b.notes == "servis AC"


def test_detection():
    assert is_booking_message(VALID)
    assert is_booking_message("Nama Lengkap: Budi\nJam Booking: 10:00")
    assert not is_booking_message("halo kak, mau tanya jam buka")
    assert not is_booking_message("Nama Lengkap: Budi")


def test_empty_template_lists_every_missing_field():
    template = "Nama Lengkap: Budi\nEmail:\nNo Telp:\nTanggal Booking\nJam Booking:"
    with pytest.raises(BookingError) as e:
        parse_booking(template, TZ, now=NOW)
    assert len(e.value.errors) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("Email", "budi-at-gmail"),
        ("No Telp", "12ab"),
        ("Tanggal Booking", "31-02-2026"),
        ("Jam Booking", "25:00"),
        ("Jam Booking", "2 siang"),
    ],
)
def test_invalid_values(field, value):
    lines = [f"{field}: {value}" if l.startswith(field) else l for l in VALID.splitlines()]
    with pytest.raises(BookingError):
        parse_booking("\n".join(lines), TZ, now=NOW)


def test_rejects_past():
    with pytest.raises(BookingError, match="lewat"):
        parse_booking(VALID.replace("05-10-2026", "28-09-2026"), TZ, now=NOW)


def test_business_hours():
    kw = dict(business_start=time(9), business_end=time(17), now=NOW)
    parse_booking(VALID, TZ, **kw)
    with pytest.raises(BookingError, match="operasional"):
        parse_booking(VALID.replace("14:00", "16:30"), TZ, **kw)
    with pytest.raises(BookingError, match="operasional"):
        parse_booking(VALID.replace("14:00", "08:00"), TZ, **kw)
