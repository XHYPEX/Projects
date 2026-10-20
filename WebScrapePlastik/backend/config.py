import os
from pathlib import Path

# Where uploaded product photos live on disk (master_items.image_path stores only
# the generated filename, relative to this directory). The default sits inside
# data/, which docker-compose already bind-mounts (./data:/app/data), so photos
# persist across container rebuilds exactly like scraper.db does. Override with
# PRODUCT_IMAGE_DIR (tests point it at a temp dir).
PRODUCT_IMAGE_DIR = Path(
    os.environ.get("PRODUCT_IMAGE_DIR", str(Path(__file__).parent.parent / "data" / "product_images"))
)

# Proof of payment (transfer slip photo / PDF) attached to a supplier payment. Same rules as the product photos:
# server-generated names, bytes on disk, the row keeps only the name. Override with PAYMENT_PROOF_DIR.
PAYMENT_PROOF_DIR = Path(
    os.environ.get("PAYMENT_PROOF_DIR", str(Path(__file__).parent.parent / "data" / "payment_proofs"))
)

# The shop's wall-clock offset from UTC, in minutes (WIB = 420, WITA = 480,
# WIT = 540; Indonesia has no daylight saving, so a fixed offset is exact and
# needs no tz database -- which a local Windows install may not have).
#
# Every timestamp is stored in UTC, but "which day was this sale" is a question
# about the shop's calendar, and the cashier UI answers it from the browser's
# local clock ("Hari ini"). The receipt history date filters use this offset to
# draw the same day boundaries; a client can pass its own offset per request
# (GET /api/receipts?tz_offset=) to match its browser exactly.
SHOP_UTC_OFFSET_MINUTES = int(os.environ.get("SHOP_UTC_OFFSET_MINUTES", "420"))

# Master switch for the Google Maps scraper.
#
# Off by default: the scraper drives Playwright + a real Chromium, which the
# deployment image no longer ships (it tripled the image size and pulls in an
# arm64 browser build we don't need while the feature is parked). Everything
# else -- cashier, invoices, inventory, dashboard -- is unaffected.
#
# To bring it back: set SCRAPER_ENABLED=true AND make sure Chromium is
# installed in the runtime image (`playwright install chromium --with-deps`,
# see the commented-out line in the Dockerfile).
SCRAPER_ENABLED = os.environ.get("SCRAPER_ENABLED", "false").lower() == "true"
