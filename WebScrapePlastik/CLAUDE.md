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
