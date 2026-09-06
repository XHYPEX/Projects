"""Audit trail for every mutating API call.

This is a pure ASGI middleware rather than per-route logging calls. There are
~37 mutating endpoints; instrumenting each one means the log silently loses
coverage the first time someone adds a route and forgets. Wrapping the ASGI
layer means a new route is logged the moment it exists.

It only observes: the request body is teed as it streams past, the response
status is read off the start message, and nothing downstream sees a difference.
A failure while logging is swallowed -- an audit trail must never be the reason
a sale cannot be recorded.
"""

import json
import re

from backend.auth import SESSION_COOKIE_NAME, hash_token
from backend.database import get_session, insert_activity

# Bodies are stored for context, so anything secret has to be dropped before it
# reaches the database. Matched case-insensitively against the JSON key.
REDACTED_KEYS = {
    "password", "new_password", "current_password", "old_password",
    "password_hash", "token", "session_token", "secret", "api_key",
}
REDACTED_PLACEHOLDER = "***"

# Cap what we keep: a large inbound document should not bloat every log row.
MAX_BODY_BYTES = 8_000
MAX_PAYLOAD_CHARS = 4_000

METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# (path regex, method, human label, entity). First match wins, so the more
# specific patterns are listed before the collection-level ones.
ACTIVITY_RULES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"^/api/auth/login$"), "POST", "Login", "auth"),
    (re.compile(r"^/api/auth/logout$"), "POST", "Logout", "auth"),
    (re.compile(r"^/api/auth/setup$"), "POST", "Buat akun admin pertama", "user"),

    (re.compile(r"^/api/users/\d+/reset-password$"), "POST", "Reset password pengguna", "user"),
    (re.compile(r"^/api/users/\d+$"), "PATCH", "Ubah pengguna", "user"),
    (re.compile(r"^/api/users$"), "POST", "Tambah pengguna", "user"),

    (re.compile(r"^/api/receipts$"), "POST", "Buat nota penjualan", "receipt"),
    (re.compile(r"^/api/receipts/[^/]+$"), "PATCH", "Ubah nota penjualan", "receipt"),
    (re.compile(r"^/api/receipts/[^/]+$"), "DELETE", "Hapus nota penjualan", "receipt"),

    (re.compile(r"^/api/suppliers/\d+$"), "PATCH", "Ubah supplier", "supplier"),
    (re.compile(r"^/api/suppliers/\d+$"), "DELETE", "Hapus supplier", "supplier"),
    (re.compile(r"^/api/suppliers$"), "POST", "Tambah supplier", "supplier"),
    (re.compile(r"^/api/suppliers$"), "DELETE", "Hapus semua supplier", "supplier"),

    (re.compile(r"^/api/brands/\d+$"), "PATCH", "Ubah merek", "brand"),
    (re.compile(r"^/api/brands/\d+$"), "DELETE", "Hapus merek", "brand"),
    (re.compile(r"^/api/brands$"), "POST", "Tambah merek", "brand"),
    (re.compile(r"^/api/brands$"), "DELETE", "Hapus semua merek", "brand"),

    (re.compile(r"^/api/units/\d+$"), "PATCH", "Ubah satuan", "unit"),
    (re.compile(r"^/api/units/\d+$"), "DELETE", "Hapus satuan", "unit"),
    (re.compile(r"^/api/units$"), "POST", "Tambah satuan", "unit"),
    (re.compile(r"^/api/units$"), "DELETE", "Hapus semua satuan", "unit"),

    (re.compile(r"^/api/inventory/master-items/\d+$"), "PATCH", "Ubah barang", "master_item"),
    (re.compile(r"^/api/inventory/master-items/\d+$"), "DELETE", "Hapus barang", "master_item"),
    (re.compile(r"^/api/inventory/master-items$"), "POST", "Tambah barang", "master_item"),

    (re.compile(r"^/api/inventory/inbound-documents/\d+/post$"), "POST", "Posting barang masuk", "inbound_document"),
    (re.compile(r"^/api/inventory/inbound-documents/\d+/void$"), "POST", "Batalkan barang masuk", "inbound_document"),
    (re.compile(r"^/api/inventory/inbound-documents/\d+$"), "PUT", "Ubah dokumen barang masuk", "inbound_document"),
    (re.compile(r"^/api/inventory/inbound-documents$"), "POST", "Buat dokumen barang masuk", "inbound_document"),
    (re.compile(r"^/api/inventory/stock-adjustments$"), "POST", "Penyesuaian stok", "stock"),

    (re.compile(r"^/api/purchase-invoices/\d+/post$"), "POST", "Posting faktur pembelian", "purchase_invoice"),
    (re.compile(r"^/api/purchase-invoices/\d+/void$"), "POST", "Batalkan faktur pembelian", "purchase_invoice"),
    (re.compile(r"^/api/purchase-invoices/\d+/payments$"), "POST", "Catat pembayaran faktur", "purchase_invoice"),
    (re.compile(r"^/api/purchase-invoices/\d+$"), "PUT", "Ubah faktur pembelian", "purchase_invoice"),
    (re.compile(r"^/api/purchase-invoices/\d+$"), "DELETE", "Hapus faktur pembelian", "purchase_invoice"),
    (re.compile(r"^/api/purchase-invoices$"), "POST", "Buat faktur pembelian", "purchase_invoice"),

    (re.compile(r"^/api/scrape$"), "POST", "Jalankan scraper", "scrape"),
    (re.compile(r"^/api/jobs/[^/]+$"), "DELETE", "Hapus job scraper", "job"),
]

# Fields worth showing as the one-line "what was this about", in priority order.
SUMMARY_KEYS = [
    "name", "product_name", "username", "invoice_number", "supplier_invoice_no",
    "plate_number", "keyword", "reason", "note", "notes",
]


def classify(method: str, path: str) -> tuple[str, str | None]:
    for pattern, rule_method, action, entity in ACTIVITY_RULES:
        if rule_method == method and pattern.match(path):
            return action, entity
    # Unmapped route: still logged, just with a generic label, so adding an
    # endpoint never means losing the record of it.
    return f"{method} {path}", None


def redact(value):
    if isinstance(value, dict):
        return {
            k: (REDACTED_PLACEHOLDER if k.lower() in REDACTED_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def extract_entity_id(path: str) -> str | None:
    """The id a route acts on, taken from the path. Creates have no id in the
    path -- their summary carries the name instead."""
    parts = [p for p in path.split("/") if p]
    for part in reversed(parts):
        if part.isdigit():
            return part
        if len(part) >= 32 and "-" in part:  # uuid-ish (receipts, jobs)
            return part
    return None


def summarize(body: dict | None) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in SUMMARY_KEYS:
        val = body.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val)[:200]
    items = body.get("items")
    if isinstance(items, list) and items:
        return f"{len(items)} baris"
    return None


def resolve_user(scope) -> tuple[int | None, str | None]:
    raw_cookie = ""
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            raw_cookie = value.decode("latin-1", "replace")
            break
    if not raw_cookie:
        return None, None
    token = None
    for part in raw_cookie.split(";"):
        name, _, val = part.strip().partition("=")
        if name == SESSION_COOKIE_NAME:
            token = val
            break
    if not token:
        return None, None
    try:
        session = get_session(hash_token(token))
    except Exception:
        return None, None
    if not session:
        return None, None
    return session["user_id"], session["username"]


class ActivityLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        if method not in METHODS or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        kept = 0
        total = 0

        async def receive_logging():
            nonlocal kept, total
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                # Count every byte that goes past, but only keep up to the cap.
                # The server usually delivers the whole body in one message, so
                # truncation has to be judged on the total rather than on
                # whether a later chunk showed up.
                total += len(chunk)
                if kept < MAX_BODY_BYTES:
                    chunks.append(chunk)
                    kept += len(chunk)
            return message

        status_code = 0

        async def send_logging(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        # The session row is read before the handler runs: logout deletes it,
        # and a login has no session on the way in.
        user_id, username = resolve_user(scope)

        await self.app(scope, receive_logging, send_logging)

        try:
            self._record(scope, method, path, status_code, chunks, user_id, username,
                         truncated=total > MAX_BODY_BYTES)
        except Exception:
            # Never let auditing break the request it is auditing.
            pass

    def _record(self, scope, method, path, status_code, chunks, user_id, username, truncated=False):
        body_bytes = b"".join(chunks)[:MAX_BODY_BYTES]
        parsed = None
        if body_bytes:
            try:
                parsed = json.loads(body_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None

        safe = redact(parsed) if parsed is not None else None
        payload = None
        if safe is not None:
            try:
                payload = json.dumps(safe, ensure_ascii=False)[:MAX_PAYLOAD_CHARS]
            except (TypeError, ValueError):
                payload = None
        elif truncated:
            # An oversized body is cut mid-JSON and will not parse. Say so
            # explicitly rather than leaving the row looking like it had no
            # data at all -- the action itself is still fully recorded.
            payload = json.dumps(
                {"_catatan": f"Data terlalu besar untuk disimpan di log (dipotong pada {MAX_BODY_BYTES} byte)."},
                ensure_ascii=False,
            )

        action, entity = classify(method, path)
        summary = summarize(safe)
        if summary is None and truncated:
            summary = "(data besar)"

        # A login carries no session cookie, so name the actor from its payload.
        if username is None and isinstance(safe, dict) and safe.get("username"):
            username = str(safe["username"])[:100]

        client = scope.get("client")
        insert_activity({
            "user_id": user_id,
            "username": username,
            "method": method,
            "path": path,
            "status_code": status_code,
            "action": action,
            "entity": entity,
            "entity_id": extract_entity_id(path),
            "summary": summary,
            "payload": payload,
            "ip": client[0] if client else None,
        })
