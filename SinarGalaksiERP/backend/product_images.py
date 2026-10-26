"""On-disk storage for per-product photos (master_items.image_path).

Kept out of database.py on purpose: that module is SQL only. The database row
stores nothing but the generated filename; the bytes live in PRODUCT_IMAGE_DIR.

Everything here is synchronous file I/O, so route handlers call it through
`loop.run_in_executor`, same as the database functions.

Safety rules this module enforces for the routes:
  * filenames are always server-generated (32 random hex chars + a fixed
    extension) -- a client-supplied name never reaches the filesystem;
  * every read/delete re-validates the stored name against that exact shape, so
    even a tampered image_path value cannot point outside PRODUCT_IMAGE_DIR;
  * files appear atomically (temp file in the same directory, fsync, then
    os.replace), so a reader never sees a half-written image.
"""

import logging
import os
import re
import secrets
from pathlib import Path

from backend.config import PRODUCT_IMAGE_DIR

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB

# Declared Content-Type -> extension the file is stored under. The extension (not
# anything the client sends later) decides the media type the file is served as.
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MEDIA_TYPES: dict[str, str] = {ext: media_type for media_type, ext in IMAGE_EXTENSIONS.items()}

_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp)$")

# Length of the cache-busting token taken from the filename (see image_version).
_VERSION_LENGTH = 12


def matches_content_type(content_type: str, data: bytes) -> bool:
    """True when the leading bytes are the signature of the declared type. The
    Content-Type header is client-controlled, so the body has to agree with it."""
    if content_type == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if content_type == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def save_image(data: bytes, content_type: str) -> str:
    """Writes `data` under a fresh random name and returns that filename. The
    caller must already have validated the type and size."""
    name = secrets.token_hex(16) + IMAGE_EXTENSIONS[content_type]
    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PRODUCT_IMAGE_DIR / f".upload-{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PRODUCT_IMAGE_DIR / name)
    finally:
        # After a successful os.replace the temp name is already gone; this only
        # has work to do when writing failed part-way.
        tmp.unlink(missing_ok=True)
    return name


def delete_image_file(filename: str | None) -> None:
    """Best-effort removal. A missing file, an empty value or a malformed name is
    a no-op; a filesystem error is logged rather than raised, because by the time
    this runs the database is already correct and an orphaned file must not turn a
    successful request into a failure."""
    if not filename or not _NAME_RE.match(filename):
        return
    try:
        (PRODUCT_IMAGE_DIR / filename).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove product image %s", filename, exc_info=True)


def image_file(filename: str | None) -> Path | None:
    """Path of the stored file, or None when there is no (valid, existing) one."""
    if not filename or not _NAME_RE.match(filename):
        return None
    path = PRODUCT_IMAGE_DIR / filename
    return path if path.is_file() else None


def media_type_for(filename: str) -> str:
    return _MEDIA_TYPES[Path(filename).suffix]


def image_version(filename: str) -> str:
    """Short cache-busting token. Every upload gets a new random filename, so the
    start of the name changes exactly when the picture does."""
    return filename[:_VERSION_LENGTH]


def image_url(master_item_id: int, filename: str | None) -> str | None:
    """URL the frontend puts in an <img src>. The /api prefix is applied by
    main.py's include_router, so it is spelled out here."""
    if not filename or not _NAME_RE.match(filename):
        return None
    return f"/api/inventory/master-items/{master_item_id}/image?v={image_version(filename)}"
