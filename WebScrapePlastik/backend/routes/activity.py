import asyncio

from fastapi import APIRouter, Query

from backend.database import get_activity, get_activity_actors

router = APIRouter()


@router.get("/activity")
async def list_activity(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: str | None = None,
    entity: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    failures_only: bool = False,
):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: get_activity(
            page=page,
            page_size=page_size,
            username=username,
            entity=entity,
            search=search,
            date_from=date_from,
            date_to=date_to,
            failures_only=failures_only,
        ),
    )


@router.get("/activity/actors")
async def list_activity_actors():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_activity_actors)
