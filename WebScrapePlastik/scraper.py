import re
import time
from urllib.parse import quote
from typing import Callable

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from models import Place

_MAPS_URL = "https://www.google.com/maps/search/{query}"
_SCROLL_PAUSE = 1.5
_CLICK_PAUSE = 2.0


def _parse_latlng(url: str) -> tuple[float, float]:
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if match:
        return float(match.group(1)), float(match.group(2))
    return 0.0, 0.0


def _extract_text(page, selector: str) -> str:
    try:
        el = page.locator(selector).first
        el.wait_for(timeout=3000)
        return el.inner_text().strip()
    except PlaywrightTimeout:
        return ""


def scrape(query: str, headless: bool = True, log_fn: Callable[[str], None] | None = None) -> list[Place]:
    def log(msg: str):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    places: list[Place] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        url = _MAPS_URL.format(query=quote(query))
        log(f"  Navigating to Google Maps...")
        page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_timeout(2000)

        # Dismiss consent dialog if present
        try:
            page.locator('button:has-text("Accept all")').click(timeout=3000)
        except PlaywrightTimeout:
            pass

        # Wait for results feed
        feed = page.locator('div[role="feed"]')
        try:
            feed.wait_for(timeout=10000)
        except PlaywrightTimeout:
            log("  No results feed found.")
            browser.close()
            return places

        # Scroll until no new results appear (2 stable checks) or 30-iteration cap
        log("  Loading results...")
        prev_count, stable = 0, 0
        for _ in range(30):
            feed.evaluate("el => el.scrollBy(0, 3000)")
            time.sleep(_SCROLL_PAUSE)
            count = len(page.locator('div[role="feed"] > div > div[jsaction]').all())
            if count == prev_count:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_count = count

        result_cards = page.locator('div[role="feed"] > div > div[jsaction]').all()
        log(f"  Found {len(result_cards)} listings — extracting details...")

        for card in result_cards:
            try:
                card.click()
                time.sleep(_CLICK_PAUSE)

                name = _extract_text(page, "h1.DUwDvf, h1[class*='fontHeadlineLarge']")
                address = _extract_text(
                    page,
                    'button[data-item-id="address"] div.fontBodyMedium, '
                    'button[aria-label*="Address"] .fontBodyMedium',
                )
                phone = _extract_text(
                    page,
                    'button[data-item-id*="phone"] div.fontBodyMedium, '
                    'button[aria-label*="Phone"] .fontBodyMedium',
                )
                lat, lng = _parse_latlng(page.url)

                if name:
                    places.append(Place(name=name, address=address, phone=phone, lat=lat, lng=lng))
                    log(f"    + {name}")
            except Exception as exc:
                log(f"    ! Skipped: {exc}")
                continue

        browser.close()

    return places
