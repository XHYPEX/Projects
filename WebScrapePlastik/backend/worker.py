import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper import scrape
from backend.database import create_job, get_places, insert_log, insert_place, update_job_status

executor = ThreadPoolExecutor(max_workers=2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_log_fn(job_id: str):
    def log(msg: str):
        insert_log(job_id, msg)
    return log


def run_scrape_job(job_id: str, keyword: str, city: str, kecamatan_list: list[str]) -> None:
    try:
        update_job_status(job_id, "running", progress=0)
        total = len(kecamatan_list)
        seen: set[tuple[str, str]] = set()

        for i, kec in enumerate(kecamatan_list):
            progress = int((i / total) * 100)
            update_job_status(job_id, "running", progress=progress)
            insert_log(job_id, f"▶ [{i + 1}/{total}] {kec}, {city}")

            query = f"{keyword} {kec} {city}"
            results = scrape(query, headless=True, log_fn=_make_log_fn(job_id))

            new_count = 0
            for p in results:
                key = (p.name.lower().strip(), p.address.lower().strip())
                if key not in seen:
                    seen.add(key)
                    insert_place(job_id, p.name, p.address, p.phone, p.lat, p.lng)
                    new_count += 1

            total_so_far = len(get_places(job_id))
            insert_log(job_id, f"  ✔ {new_count} new unique places (total: {total_so_far})")

        insert_log(job_id, f"━━━ Done. {len(get_places(job_id))} total unique places. ━━━")
        update_job_status(job_id, "done", progress=100, completed_at=_now())

    except Exception as exc:
        insert_log(job_id, f"  ✗ Error: {exc}")
        update_job_status(job_id, "failed", error=str(exc), completed_at=_now())


def submit_job(job_id: str, keyword: str, city: str, kecamatan_list: list[str]) -> None:
    executor.submit(run_scrape_job, job_id, keyword, city, kecamatan_list)
