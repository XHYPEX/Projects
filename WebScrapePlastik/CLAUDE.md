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
The "Claude/Anthropic" aesthetic, roughly
The recognizable feel is: warm cream backgrounds, a coral/clay accent, dark warm (not pure-black) text, generous whitespace, restrained and editorial. Here's a palette that captures it (these approximate the look — they're not official brand assets):
--bg:          #FAF9F5;  /* warm cream, main background */
--bg-subtle:   #F0EEE6;  /* slightly deeper cream for cards/sections */
--text:        #141413;  /* warm near-black */
--text-muted:  #6B6B66;  /* warm gray */
--accent:      #D97757;  /* the coral / clay — the signature color */
--accent-hover:#C4633F;
--border:      #E5E3DC;  /* soft warm border */
Typography — Anthropic pairs a serif for display/headings with a clean sans for body/UI. Their actual fonts are proprietary, but close free substitutes:

Serif (headings): Tiempos, Lora, Fraunces, or Georgia
Sans (body/UI): Inter, or system-ui

And the feel rules that make it read as "Claude" rather than generic: soft rounded corners (~8–12px), lots of breathing room, low-contrast subtle borders instead of hard shadows, calm/minimal motion.