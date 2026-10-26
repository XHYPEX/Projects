import argparse
import pandas as pd

from scraper import scrape
from mapper import generate


def main():
    parser = argparse.ArgumentParser(description="Scrape Google Maps for place info.")
    parser.add_argument("--query", required=True, help='Search keyword, e.g. "toko plastik Jakarta"')
    parser.add_argument("--output", default="results.csv", help="CSV output file (default: results.csv)")
    parser.add_argument("--map", default="output_map.html", help="HTML map output file (default: output_map.html)")
    parser.add_argument("--show-browser", action="store_true", help="Run browser in non-headless mode")
    args = parser.parse_args()

    print(f'Scraping Google Maps for: "{args.query}"')
    places = scrape(args.query, headless=not args.show_browser)

    if not places:
        print("No results found.")
        return

    df = pd.DataFrame([vars(p) for p in places])
    df.to_csv(args.output, index=False)
    print(f"Saved {len(places)} places to {args.output}")

    generate(places, args.map)


if __name__ == "__main__":
    main()
