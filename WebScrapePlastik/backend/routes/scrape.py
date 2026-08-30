import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.config import SCRAPER_ENABLED
from backend.database import (
    create_job,
    delete_job,
    get_all_jobs,
    get_job,
    get_logs_since,
)
from backend.schemas import JobStatus, ScrapeRequest, ScrapeResponse
from backend.worker import submit_job
from cities import CITY_KECAMATAN

router = APIRouter()


def _job_to_status(row: dict) -> JobStatus:
    return JobStatus(
        job_id=row["id"],
        keyword=row["keyword"],
        city=row["city"],
        kecamatan=row["kecamatan_list"],
        status=row["status"],
        progress=row["progress"],
        error=row["error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


@router.post("/scrape", response_model=ScrapeResponse, status_code=202)
async def start_scrape(req: ScrapeRequest):
    # Read-only job/results endpoints below stay available so existing scrape data
    # remains viewable and exportable; only starting new jobs is switched off.
    if not SCRAPER_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="The Maps scraper is currently disabled.",
        )
    if req.city not in CITY_KECAMATAN:
        raise HTTPException(status_code=422, detail=f"Unknown city: {req.city}")
    valid = set(CITY_KECAMATAN[req.city])
    invalid = [k for k in req.kecamatan if k not in valid]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown kecamatan: {invalid}")
    if not req.kecamatan:
        raise HTTPException(status_code=422, detail="kecamatan list must not be empty")

    job_id = str(uuid4())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, create_job, job_id, req.keyword, req.city, req.kecamatan)
    submit_job(job_id, req.keyword, req.city, req.kecamatan)
    return ScrapeResponse(job_id=job_id)


@router.get("/jobs", response_model=list[JobStatus])
async def list_jobs():
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_all_jobs)
    return [_job_to_status(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_status(row)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job_route(job_id: str):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] in ("pending", "running"):
        raise HTTPException(status_code=409, detail="Cannot delete a job that is still in progress")
    await loop.run_in_executor(None, delete_job, job_id)


@router.get("/cities")
async def get_cities():
    return CITY_KECAMATAN


async def _log_event_generator(job_id: str):
    loop = asyncio.get_event_loop()
    last_id = 0

    # Verify job exists
    row = await loop.run_in_executor(None, get_job, job_id)
    if row is None:
        yield f"event: error\ndata: {json.dumps({'message': 'Job not found'})}\n\n"
        return

    while True:
        rows = await loop.run_in_executor(None, get_logs_since, job_id, last_id)
        for r in rows:
            last_id = r["id"]
            payload = json.dumps({"id": r["id"], "message": r["message"], "ts": r["created_at"]})
            yield f"data: {payload}\n\n"

        job = await loop.run_in_executor(None, get_job, job_id)
        if job and job["status"] in ("done", "failed"):
            # Drain any remaining logs
            rows = await loop.run_in_executor(None, get_logs_since, job_id, last_id)
            for r in rows:
                payload = json.dumps({"id": r["id"], "message": r["message"], "ts": r["created_at"]})
                yield f"data: {payload}\n\n"
            yield "event: done\ndata: {}\n\n"
            break

        await asyncio.sleep(0.5)


@router.get("/jobs/{job_id}/logs")
async def stream_logs(job_id: str):
    return StreamingResponse(
        _log_event_generator(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
