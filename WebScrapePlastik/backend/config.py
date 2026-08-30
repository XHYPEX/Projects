import os

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
