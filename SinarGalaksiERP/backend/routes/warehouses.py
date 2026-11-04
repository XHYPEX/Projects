"""Master Gudang (Gudang → Rak). Everyone may read the list (the dropdowns need it); only an Admin changes it."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import warehouses as wh
from backend.auth import get_current_user, require_admin

router = APIRouter()


async def _run(fn, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class RackIn(BaseModel):
    id: int | None = None  # empty = a new rack
    name: str


class WarehouseIn(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    racks: list[RackIn] = []


class RackActiveIn(BaseModel):
    is_active: bool


@router.get("/warehouses")
async def list_warehouses(active: bool = False):
    return await _run(wh.list_warehouses, not active)


@router.get("/warehouses/summary")
async def warehouses_summary():
    return await _run(wh.summary)


@router.post("/warehouses", status_code=201, dependencies=[Depends(require_admin)])
async def create_warehouse(req: WarehouseIn, user: dict = Depends(get_current_user)):
    return await _run(wh.create_warehouse, req.model_dump(), user["username"])


@router.patch("/warehouses/{warehouse_id}", dependencies=[Depends(require_admin)])
async def update_warehouse(warehouse_id: int, req: WarehouseIn, user: dict = Depends(get_current_user)):
    return await _run(wh.update_warehouse, warehouse_id, req.model_dump(exclude_unset=True), user["username"])


@router.delete("/warehouses/{warehouse_id}", dependencies=[Depends(require_admin)])
async def delete_warehouse(warehouse_id: int):
    return await _run(wh.delete_warehouse, warehouse_id)


@router.patch("/warehouse-racks/{rack_id}", dependencies=[Depends(require_admin)])
async def set_rack_active(rack_id: int, req: RackActiveIn, user: dict = Depends(get_current_user)):
    return await _run(wh.set_rack_active, rack_id, req.is_active, user["username"])


@router.delete("/warehouse-racks/{rack_id}", dependencies=[Depends(require_admin)])
async def delete_rack(rack_id: int):
    return await _run(wh.delete_rack, rack_id)
