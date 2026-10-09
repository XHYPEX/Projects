import asyncio
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import (
    PHONE_RE,
    autocomplete_customers,
    create_customer,
    delete_customer,
    get_customer,
    list_customers,
    normalize_phone,
    parse_ymd,
    shop_today,
    update_customer,
)
from backend.schemas import (
    CustomerAutocompleteOut,
    CustomerListOut,
    CustomerOut,
    CustomerRequest,
    CustomerUpdateRequest,
)

router = APIRouter()

# Deliberately loose: one @, no spaces, a dot in the domain. Anything stricter
# rejects real addresses; anything looser lets "budi" through as an email.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254
MAX_ADDRESS_LENGTH = 500
# Earliest birth date accepted: anything before this is a typo, not a customer.
EARLIEST_BIRTH_DATE = date(1900, 1, 1)


def _row_to_customer(row: dict) -> CustomerOut:
    return CustomerOut(
        id=row["id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        full_name=row["full_name"],
        phone=row["phone"],
        email=row["email"],
        birth_date=row["birth_date"],
        address=row["address"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        receipt_count=row["receipt_count"],
        last_visit=row["last_visit"],
    )


def _raise_from_value_error(e: ValueError) -> None:
    msg = str(e)
    if "tidak ditemukan" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


# --- field validators: each returns the cleaned value to store -------------------


def _clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    return " ".join(raw.split()) or None


def _validate_first_name(raw: str | None) -> str:
    first_name = _clean_text(raw)
    if not first_name:
        raise HTTPException(status_code=422, detail="Nama depan wajib diisi.")
    if len(first_name) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=422, detail=f"Nama depan maksimal {MAX_NAME_LENGTH} karakter.")
    return first_name


def _validate_last_name(raw: str | None) -> str | None:
    last_name = _clean_text(raw)
    if last_name and len(last_name) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=422, detail=f"Nama belakang maksimal {MAX_NAME_LENGTH} karakter.")
    return last_name


def _validate_phone(raw: str | None) -> str:
    phone = normalize_phone(raw)
    if not phone:
        raise HTTPException(status_code=422, detail="Nomor HP wajib diisi.")
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="Nomor HP harus 7-15 digit (boleh diawali tanda +).")
    return phone


def _validate_email(raw: str | None) -> str | None:
    email = (raw or "").strip()
    if not email:
        return None
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Format email tidak valid.")
    return email


def _validate_birth_date(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        birth_date = parse_ymd(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal lahir tidak valid, gunakan format YYYY-MM-DD.")
    if birth_date > shop_today():
        raise HTTPException(status_code=422, detail="Tanggal lahir tidak boleh di masa depan.")
    if birth_date < EARLIEST_BIRTH_DATE:
        raise HTTPException(status_code=422, detail="Tanggal lahir tidak masuk akal (sebelum tahun 1900).")
    return value


def _validate_address(raw: str | None) -> str | None:
    address = (raw or "").strip()
    if len(address) > MAX_ADDRESS_LENGTH:
        raise HTTPException(status_code=422, detail=f"Alamat maksimal {MAX_ADDRESS_LENGTH} karakter.")
    return address or None


# --- routes --------------------------------------------------------------------
# /customers/autocomplete is declared before /customers/{customer_id} so the
# literal path is not captured as an id.


@router.get("/customers/autocomplete", response_model=list[CustomerAutocompleteOut])
async def autocomplete_customers_route(query: str, limit: int = 8):
    if not query.strip():
        raise HTTPException(status_code=422, detail="query tidak boleh kosong.")
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=422, detail="limit harus antara 1 dan 50.")
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, autocomplete_customers, query, limit)
    return [CustomerAutocompleteOut(**r) for r in rows]


@router.get("/customers", response_model=CustomerListOut)
async def list_customers_route(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    is_active: bool | None = None,
):
    if page < 1:
        raise HTTPException(status_code=422, detail="page harus >= 1.")
    if page_size < 1 or page_size > 200:
        raise HTTPException(status_code=422, detail="page_size harus antara 1 dan 200.")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, list_customers, page, page_size, search, is_active)
    return CustomerListOut(
        items=[_row_to_customer(r) for r in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.post("/customers", response_model=CustomerOut, status_code=201)
async def create_new_customer(req: CustomerRequest):
    first_name = _validate_first_name(req.first_name)
    last_name = _validate_last_name(req.last_name)
    phone = _validate_phone(req.phone)
    email = _validate_email(req.email)
    birth_date = _validate_birth_date(req.birth_date)
    address = _validate_address(req.address)

    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(
            None, create_customer, first_name, last_name, phone, email, birth_date, address
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_customer(row)


@router.get("/customers/{customer_id}", response_model=CustomerOut)
async def get_customer_route(customer_id: int):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_customer, customer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
    return _row_to_customer(row)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
async def patch_customer(customer_id: int, req: CustomerUpdateRequest):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")

    # Only what the client sent is applied; for the nullable fields an explicit
    # null (or blank) clears the stored value, an omitted field leaves it alone.
    sent = req.model_fields_set
    changes: dict = {}
    if "first_name" in sent:
        changes["first_name"] = _validate_first_name(req.first_name)
    if "last_name" in sent:
        changes["last_name"] = _validate_last_name(req.last_name)
    if "phone" in sent:
        changes["phone"] = _validate_phone(req.phone)
    if "email" in sent:
        changes["email"] = _validate_email(req.email)
    if "birth_date" in sent:
        changes["birth_date"] = _validate_birth_date(req.birth_date)
    if "address" in sent:
        changes["address"] = _validate_address(req.address)
    if "is_active" in sent:
        if req.is_active is None:
            raise HTTPException(status_code=422, detail="is_active harus true atau false.")
        changes["is_active"] = req.is_active

    try:
        row = await loop.run_in_executor(None, update_customer, customer_id, changes)
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_customer(row)


@router.delete("/customers/{customer_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_customer_route(customer_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
    try:
        await loop.run_in_executor(None, delete_customer, customer_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)
