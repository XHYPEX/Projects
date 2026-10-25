# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python Google Maps scraper that extracts place name, address, and phone number for a given search keyword, saves results to CSV, and renders an interactive Folium HTML map.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run the scraper
python main.py --query "toko plastik Jakarta"

# With custom output paths
python main.py --query "plastik supplier Surabaya" --output data.csv --map map.html

# Debug with visible browser
python main.py --query "toko plastik" --show-browser
```

## Architecture

| File | Role |
|---|---|
| `main.py` | CLI entry point — argparse, orchestrates scrape → CSV → map |
| `scraper.py` | Playwright (sync) scraping of Google Maps; scrolls results sidebar, clicks each card, extracts name/address/phone/lat/lng |
| `mapper.py` | Builds a Folium map centered on scraped places with clickable markers |
| `models.py` | `Place` dataclass (name, address, phone, lat, lng) |

## Key Details

- Lat/lng are parsed from the Google Maps URL after clicking a result (`@lat,lng,zoom`).
- The scraper scrolls the sidebar 8 times before collecting cards to load paginated results.
- Use `--show-browser` to run Playwright in non-headless mode for debugging selector issues.
- Google Maps DOM selectors can change; selectors live in `scraper.py` and may need updating.

## Theme Style
"Navy Bengkel" (owner's choice, 24-10-2026), replacing the earlier cream/coral look:
--sidebar:     #0F1B2D;  /* navy sidebar, light text (#F1F5F9 / muted #94A3B8) */
--bg:          #F6F7F9;  /* light grey canvas */
--bg-subtle:   #EEF0F3;  /* table heads, chips, read-only fields */
--card:        #FFFFFF;
--text:        #111827;
--text-muted:  #6B7280;
--accent:      #EA580C;  /* orange: primary actions (Pembayaran, Pesanan Baru, Simpan) */
--accent-hover:#C2410C;
--border:      #E3E6EB;
Typography: Inter for everything (headings included); IBM Plex Mono for codes/numbers.
Tokens live in the Tailwind config at the top of frontend/index.html (bg, bg2, ink, muted, accent, accentDark, line);
the navy sidebar is the `#drawer-panel` block in the <style>. Kasir buttons keep colour-by-role: accent = transaction,
blue #1D4ED8 = operations/status, green #15803D = print, red #DC2626 = void/delete, slate #334155 = standard.
Feel: soft rounded corners (~8–12px), subtle borders instead of heavy shadows, calm/minimal motion.
