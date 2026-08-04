import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException

from backend.database import get_sales_summary
from backend.schemas import SalesSummaryOut

router = APIRouter()


@router.get("/sales/summary", response_model=SalesSummaryOut)
async def sales_summary(date_from: str, date_to: str):
    try:
        date.fromisoformat(date_from)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date_from, expected YYYY-MM-DD")
    try:
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date_to, expected YYYY-MM-DD")
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_sales_summary, date_from, date_to)
    return SalesSummaryOut(**result)
