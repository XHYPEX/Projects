"""On-disk storage for proof of supplier payment (purchase_invoice_payments.proof_file).

Same safety rules as product_images.py: filenames are server-generated (32 hex + fixed extension), every read
re-validates the stored name, files appear atomically. Images (JPEG/PNG/WebP) and PDF are accepted, 5 MB at most.
"""
import logging
import os
import re
import secrets
from pathlib import Path

from backend.config import PAYMENT_PROOF_DIR

logger = logging.getLogger(__name__)

MAX_PROOF_BYTES = 5 * 1024 * 1024
PROOF_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
_MEDIA_TYPES = {ext: mt for mt, ext in PROOF_EXTENSIONS.items()}
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp|pdf)$")


def matches_content_type(content_type: str, data: bytes) -> bool:
    if content_type == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if content_type == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type == "application/pdf":
        return data[:5] == b"%PDF-"
    return False


def save_proof(data: bytes, content_type: str) -> str:
    name = secrets.token_hex(16) + PROOF_EXTENSIONS[content_type]
    PAYMENT_PROOF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PAYMENT_PROOF_DIR / f".upload-{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PAYMENT_PROOF_DIR / name)
    finally:
        tmp.unlink(missing_ok=True)
    return name


def delete_proof_file(filename: str | None) -> None:
    if not filename or not _NAME_RE.match(filename):
        return
    try:
        (PAYMENT_PROOF_DIR / filename).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove payment proof %s", filename, exc_info=True)


def proof_file(filename: str | None) -> Path | None:
    if not filename or not _NAME_RE.match(filename):
        return None
    path = PAYMENT_PROOF_DIR / filename
    return path if path.is_file() else None


def media_type_for(filename: str) -> str:
    return _MEDIA_TYPES[Path(filename).suffix]
