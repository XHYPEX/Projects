import asyncio
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import get_current_user, require_admin
from backend.database import (
    create_category,
    category_code_preview,
    find_similar_categories,
    get_categories_for_export,
    get_categories_summary,
    get_category_row,
    import_categories,
    list_categories_with_stats,
    list_category_items,
    set_category_archived,
    update_category,
)
from backend.schemas import (
    CategoryCreateRequest,
    CategoryDetailOut,
    CategoryImportRequest,
    CategoryItemsOut,
    CategoryOut,
    CategoryUpdateRequest,
)

router = APIRouter()


def _raise(e: ValueError):
    msg = str(e)
    if "tidak ditemukan" in msg:
        raise HTTPException(status_code=404, detail=msg)
    low = msg.lower()
    if any(k in low for k in ("sudah", "baru saja diubah", "masih ada", "masih diarsipkan", "diarsipkan. pulihkan")):
        raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=422, detail=msg)


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


@router.get("/categories/summary")
async def categories_summary():
    return await _run(get_categories_summary)


@router.get("/categories/code-preview")
async def categories_code_preview(name: str = "", parent_id: int | None = None):
    try:
        return {"code": await _run(category_code_preview, name, parent_id)}
    except ValueError as e:
        _raise(e)


@router.get("/categories/check-name")
async def categories_check_name(name: str, parent_id: int | None = None, exclude_id: int | None = None):
    return {"matches": await _run(find_similar_categories, name, parent_id, exclude_id)}


@router.get("/categories/export.csv")
async def categories_export_csv():
    rows = await _run(get_categories_for_export)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kode", "nama", "induk_kode", "status", "deskripsi", "jumlah_produk"])
    for r in rows:
        w.writerow([r["code"], r["name"], r["parent_code"], r["status"], r["description"], r["product_count"]])
    return Response(
        content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="master-kategori.csv"'},
    )


@router.post("/categories/import", dependencies=[Depends(require_admin)])
async def categories_import(req: CategoryImportRequest, user: dict = Depends(get_current_user)):
    text = req.csv.lstrip("﻿")
    if len(text) > 500_000:
        raise HTTPException(status_code=422, detail="File CSV terlalu besar.")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames or "nama" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(status_code=422, detail="Header CSV harus memuat kolom: nama (opsional: kode, induk_kode, status, deskripsi).")
    rows = [{(k or "").strip().lower(): (v or "") for k, v in r.items()} for r in reader]
    if len(rows) > 2000:
        raise HTTPException(status_code=422, detail="Maksimal 2000 baris per import.")
    return await _run(import_categories, rows, user["username"])


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories():
    return await _run(list_categories_with_stats)


@router.get("/categories/{category_id}", response_model=CategoryDetailOut)
async def get_category_detail(category_id: int):
    cats = await _run(list_categories_with_stats)
    cat = next((c for c in cats if c["id"] == category_id), None)
    if cat is None:
        raise HTTPException(status_code=404, detail="Kategori tidak ditemukan.")
    subs = [c for c in cats if c["parent_id"] == category_id]
    return CategoryDetailOut(**cat, subcategories=[CategoryOut(**s) for s in subs])


@router.get("/categories/{category_id}/items", response_model=CategoryItemsOut)
async def get_category_items(category_id: int, limit: int = 20, offset: int = 0):
    if await _run(get_category_row, category_id) is None:
        raise HTTPException(status_code=404, detail="Kategori tidak ditemukan.")
    return await _run(list_category_items, category_id, max(1, min(limit, 100)), max(0, offset))


async def _out(category_id: int) -> CategoryOut:
    cats = await _run(list_categories_with_stats)
    return CategoryOut(**next(c for c in cats if c["id"] == category_id))


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_new_category(req: CategoryCreateRequest, user: dict = Depends(get_current_user)):
    try:
        row = await _run(
            create_category, req.name, req.code, req.parent_id, req.status, req.icon, req.description, user["username"]
        )
    except ValueError as e:
        _raise(e)
    return await _out(row["id"])


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def patch_category(category_id: int, req: CategoryUpdateRequest, user: dict = Depends(get_current_user)):
    fields = req.model_fields_set
    try:
        await _run(
            lambda: update_category(
                category_id, req.name, req.parent_id, "parent_id" in fields, req.status,
                req.icon, "icon" in fields, req.description, "description" in fields,
                user["username"], req.expected_updated_at,
            )
        )
    except ValueError as e:
        _raise(e)
    return await _out(category_id)


@router.post("/categories/{category_id}/archive", response_model=CategoryOut, dependencies=[Depends(require_admin)])
async def archive_category(category_id: int, user: dict = Depends(get_current_user)):
    try:
        await _run(set_category_archived, category_id, True, user["username"])
    except ValueError as e:
        _raise(e)
    return await _out(category_id)


@router.post("/categories/{category_id}/restore", response_model=CategoryOut, dependencies=[Depends(require_admin)])
async def restore_category(category_id: int, user: dict = Depends(get_current_user)):
    try:
        await _run(set_category_archived, category_id, False, user["username"])
    except ValueError as e:
        _raise(e)
    return await _out(category_id)
