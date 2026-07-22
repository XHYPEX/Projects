import asyncio
import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.database import get_job, get_places
from backend.schemas import PlaceOut

router = APIRouter()


@router.get("/results/{job_id}", response_model=list[PlaceOut])
async def get_results(job_id: str):
    loop = asyncio.get_event_loop()
    job = await loop.run_in_executor(None, get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="Job not complete yet")
    rows = await loop.run_in_executor(None, get_places, job_id)
    return [PlaceOut(**r) for r in rows]


@router.get("/results/{job_id}/csv")
async def download_csv(job_id: str):
    loop = asyncio.get_event_loop()
    job = await loop.run_in_executor(None, get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="Job not complete yet")

    rows = await loop.run_in_executor(None, get_places, job_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "name", "address", "phone", "lat", "lng"])
    for r in rows:
        writer.writerow([r["id"], r["name"], r["address"], r["phone"], r["lat"], r["lng"]])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="results_{job_id[:8]}.csv"'},
    )


@router.get("/map/{job_id}")
async def get_map(job_id: str):
    loop = asyncio.get_event_loop()
    job = await loop.run_in_executor(None, get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = await loop.run_in_executor(None, get_places, job_id)
    features = []
    for r in rows:
        if r["lat"] == 0.0 and r["lng"] == 0.0:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {"name": r["name"], "address": r["address"], "phone": r["phone"]},
        })
    return {"type": "FeatureCollection", "features": features}
