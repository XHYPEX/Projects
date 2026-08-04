import asyncio
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.auth import require_admin
from backend.database import (
    autocomplete_master_items,
    create_brand,
    create_inbound_document,
    create_master_item,
    create_stock_adjustment,
    create_supplier,
    create_unit,
    delete_all_brands,
    delete_all_suppliers,
    delete_all_units,
    delete_brand,
    delete_master_item,
    delete_supplier,
    delete_unit,
    find_cross_supplier_master_items,
    find_exact_duplicate_master_item,
    get_all_brands,
    get_all_suppliers,
    get_all_units,
    get_brand,
    get_inbound_document,
    get_inbound_document_items,
    get_inventory_overview,
    get_master_item,
    get_master_item_batch_history,
    get_master_item_by_sku,
    get_master_items_for_export,
    get_stock_ledger,
    get_supplier,
    get_unit,
    get_unit_by_name,
    list_inbound_documents,
    list_master_items,
    post_inbound_document,
    preview_brand_code,
    preview_sku,
    preview_supplier_code,
    search_brands,
    search_suppliers,
    update_brand,
    update_inbound_document,
    update_master_item,
    update_supplier,
    update_unit,
    void_inbound_document,
)
from backend.schemas import (
    BrandOut,
    BrandRequest,
    BrandUpdateRequest,
    BulkDeleteOut,
    CodePreviewOut,
    DuplicateCheckOut,
    InboundDocumentCreateRequest,
    InboundDocumentDetail,
    InboundDocumentOut,
    InboundItemOut,
    IncomingItemOut,
    InventoryOverviewOut,
    MasterItemBatchOut,
    MasterItemCreateRequest,
    MasterItemDetailOut,
    MasterItemListOut,
    MasterItemOut,
    MasterItemUpdateRequest,
    PendingInvoiceOut,
    SkuPreviewOut,
    StockAdjustmentRequest,
    StockAdjustmentResponse,
    StockLedgerOut,
    SupplierOut,
    SupplierRequest,
    SupplierUpdateRequest,
    TopSellingOut,
    UnitOut,
    UnitRequest,
    UnitUpdateRequest,
    VoidRequest,
)

router = APIRouter()


# --- helpers ----------------------------------------------------------------


def _raise_from_value_error(e: ValueError) -> None:
    msg = str(e)
    if "not found" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


def _validate_received_date(received_date: str) -> None:
    try:
        rd = date.fromisoformat(received_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid received_date, expected YYYY-MM-DD")
    today = date.today()
    if rd > today:
        raise HTTPException(status_code=422, detail="received_date cannot be in the future")
    if (today - rd).days > 30:
        raise HTTPException(status_code=422, detail="received_date cannot be more than 30 days in the past")


def _validate_inbound_items(items: list) -> None:
    if not items:
        raise HTTPException(status_code=422, detail="At least one line item is required")
    seen: set = set()
    for item in items:
        # Barang can only be received if it already exists in Master Barang.
        # Inline new-product creation (pending_* fields) is no longer allowed.
        if item.master_item_id is None:
            raise HTTPException(
                status_code=422,
                detail="Setiap baris barang harus dipilih dari Master Barang — tambahkan barang baru di Master Barang terlebih dahulu",
            )
        if item.pending_brand_id or item.pending_product_name or item.pending_unit or item.pending_sell_price is not None:
            raise HTTPException(
                status_code=422,
                detail="Barang baru tidak dapat dibuat langsung di Barang Masuk — tambahkan dulu di Master Barang",
            )
        if item.qty_in <= 0:
            raise HTTPException(status_code=422, detail="qty_in must be a positive integer")
        if item.cost_price < 0:
            raise HTTPException(status_code=422, detail="cost_price must be >= 0")

        if get_master_item(item.master_item_id) is None:
            raise HTTPException(status_code=422, detail=f"master_item_id {item.master_item_id} not found")
        key = ("existing", item.master_item_id)

        if key in seen:
            raise HTTPException(
                status_code=422,
                detail="Duplicate product line: two line items in this document resolve to the same product — combine them into one line instead",
            )
        seen.add(key)


def _row_to_supplier(row: dict) -> SupplierOut:
    return SupplierOut(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        is_system=bool(row["is_system"]),
        source=row["source"],
        phone=row["phone"],
        address=row["address"],
        contact_person=row["contact_person"],
        npwp=row["npwp"],
        score=row.get("score"),
    )


def _row_to_brand(row: dict) -> BrandOut:
    return BrandOut(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        is_system=bool(row["is_system"]),
        source=row["source"],
        score=row.get("score"),
    )


def _row_to_unit(row: dict) -> UnitOut:
    return UnitOut(
        id=row["id"],
        name=row["name"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def _row_to_master_item(row: dict) -> MasterItemOut:
    return MasterItemOut(
        id=row["id"],
        sku=row["sku"],
        sku_prefix=row["sku_prefix"],
        sku_seq=row["sku_seq"],
        supplier_id=row["supplier_id"],
        brand_id=row["brand_id"],
        supplier_name=row.get("supplier_name"),
        brand_name=row.get("brand_name"),
        name=row["name"],
        name_normalized=row["name_normalized"],
        unit=row["unit"],
        last_cost_price=row["last_cost_price"],
        avg_cost_price=row["avg_cost_price"],
        sell_price=row["sell_price"],
        stock_qty=row["stock_qty"],
        first_received_date=row["first_received_date"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def _row_to_batch(row: dict) -> MasterItemBatchOut:
    return MasterItemBatchOut(
        inbound_item_id=row["inbound_item_id"],
        document_id=row["document_id"],
        doc_number=row["doc_number"],
        document_status=row["document_status"],
        qty_in=row["qty_in"],
        qty_remaining=row["qty_remaining"],
        cost_price=row["cost_price"],
        received_date=row["received_date"],
    )


def _row_to_inbound_item(row: dict) -> InboundItemOut:
    return InboundItemOut(
        id=row["id"],
        document_id=row["document_id"],
        master_item_id=row["master_item_id"],
        pending_brand_id=row["pending_brand_id"],
        pending_product_name=row["pending_product_name"],
        pending_unit=row["pending_unit"],
        pending_sell_price=row["pending_sell_price"],
        qty_in=row["qty_in"],
        cost_price=row["cost_price"],
        qty_remaining=row["qty_remaining"],
        received_date=row["received_date"],
        resolved_sku=row.get("resolved_sku"),
        resolved_name=row.get("resolved_name"),
        resolved_brand_id=row.get("resolved_brand_id"),
        resolved_brand_name=row.get("resolved_brand_name"),
        resolved_unit=row.get("resolved_unit"),
        resolved_sell_price=row.get("resolved_sell_price"),
        pending_brand_name=row.get("pending_brand_name"),
    )


def _row_to_document(row: dict) -> InboundDocumentOut:
    return InboundDocumentOut(
        id=row["id"],
        doc_number=row["doc_number"],
        supplier_id=row["supplier_id"],
        supplier_name=row.get("supplier_name"),
        received_date=row["received_date"],
        supplier_invoice_no=row["supplier_invoice_no"],
        notes=row["notes"],
        status=row["status"],
        total_value=row["total_value"],
        created_at=row["created_at"],
    )


def _row_to_document_detail(row: dict, items: list[dict]) -> InboundDocumentDetail:
    base = _row_to_document(row)
    return InboundDocumentDetail(**base.model_dump(), items=[_row_to_inbound_item(i) for i in items])


def _row_to_ledger(row: dict) -> StockLedgerOut:
    return StockLedgerOut(
        id=row["id"],
        master_item_id=row["master_item_id"],
        type=row["type"],
        qty=row["qty"],
        balance_after=row["balance_after"],
        ref_type=row["ref_type"],
        ref_id=row["ref_id"],
        note=row["note"],
        created_at=row["created_at"],
    )


# --- suppliers ---------------------------------------------------------


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_new_supplier(req: SupplierRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(
            None,
            create_supplier,
            req.name.strip(),
            req.code,
            "quick_add",
            req.phone,
            req.address,
            req.contact_person,
            req.npwp,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_supplier(row)


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(active_only: bool = False, search: str | None = None):
    loop = asyncio.get_event_loop()
    if search is not None:
        if not search.strip():
            raise HTTPException(status_code=422, detail="search must not be empty")
        rows = await loop.run_in_executor(None, search_suppliers, search.strip(), True, 8)
    else:
        rows = await loop.run_in_executor(None, get_all_suppliers, active_only)
    return [_row_to_supplier(r) for r in rows]


@router.get("/suppliers/code-preview", response_model=CodePreviewOut)
async def supplier_code_preview(name: str):
    if not name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, preview_supplier_code, name.strip())
    return CodePreviewOut(code=code)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
async def patch_supplier(supplier_id: int, req: SupplierUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, supplier_id) is None:
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} not found")
    try:
        row = await loop.run_in_executor(
            None,
            update_supplier,
            supplier_id,
            req.name,
            req.is_active,
            req.phone,
            req.address,
            req.contact_person,
            req.npwp,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_supplier(row)


@router.delete("/suppliers", response_model=BulkDeleteOut, dependencies=[Depends(require_admin)])
async def delete_all_suppliers_route():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, delete_all_suppliers)
    return BulkDeleteOut(**result)


@router.delete("/suppliers/{supplier_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_supplier_route(supplier_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, supplier_id) is None:
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} not found")
    try:
        await loop.run_in_executor(None, delete_supplier, supplier_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- brands --------------------------------------------------------------


@router.post("/brands", response_model=BrandOut, status_code=201)
async def create_new_brand(req: BrandRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(None, create_brand, req.name.strip(), req.code, "quick_add")
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_brand(row)


@router.get("/brands", response_model=list[BrandOut])
async def list_brands(active_only: bool = False, search: str | None = None):
    loop = asyncio.get_event_loop()
    if search is not None:
        if not search.strip():
            raise HTTPException(status_code=422, detail="search must not be empty")
        rows = await loop.run_in_executor(None, search_brands, search.strip(), True, 8)
    else:
        rows = await loop.run_in_executor(None, get_all_brands, active_only)
    return [_row_to_brand(r) for r in rows]


@router.get("/brands/code-preview", response_model=CodePreviewOut)
async def brand_code_preview(name: str):
    if not name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, preview_brand_code, name.strip())
    return CodePreviewOut(code=code)


@router.patch("/brands/{brand_id}", response_model=BrandOut)
async def patch_brand(brand_id: int, req: BrandUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_brand, brand_id) is None:
        raise HTTPException(status_code=404, detail=f"Brand {brand_id} not found")
    try:
        row = await loop.run_in_executor(None, update_brand, brand_id, req.name, req.is_active)
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_brand(row)


@router.delete("/brands", response_model=BulkDeleteOut, dependencies=[Depends(require_admin)])
async def delete_all_brands_route():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, delete_all_brands)
    return BulkDeleteOut(**result)


@router.delete("/brands/{brand_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_brand_route(brand_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_brand, brand_id) is None:
        raise HTTPException(status_code=404, detail=f"Brand {brand_id} not found")
    try:
        await loop.run_in_executor(None, delete_brand, brand_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- units (Satuan) ---------------------------------------------------------


@router.post("/units", response_model=UnitOut, status_code=201)
async def create_new_unit(req: UnitRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(None, create_unit, req.name.strip())
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_unit(row)


@router.get("/units", response_model=list[UnitOut])
async def list_units(active_only: bool = False):
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_all_units, active_only)
    return [_row_to_unit(r) for r in rows]


@router.patch("/units/{unit_id}", response_model=UnitOut)
async def patch_unit(unit_id: int, req: UnitUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail=f"Unit {unit_id} not found")
    try:
        row = await loop.run_in_executor(None, update_unit, unit_id, req.name, req.is_active)
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_unit(row)


@router.delete("/units", response_model=BulkDeleteOut, dependencies=[Depends(require_admin)])
async def delete_all_units_route():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, delete_all_units)
    return BulkDeleteOut(**result)


@router.delete("/units/{unit_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_unit_route(unit_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail=f"Unit {unit_id} not found")
    try:
        await loop.run_in_executor(None, delete_unit, unit_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- sku preview / duplicate check / autocomplete --------------------------


@router.get("/inventory/sku-preview", response_model=SkuPreviewOut)
async def sku_preview(supplier_id: int, brand_id: int, product_name: str):
    if not product_name.strip():
        raise HTTPException(status_code=422, detail="product_name must not be empty")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, preview_sku, supplier_id, brand_id, product_name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return SkuPreviewOut(**result)


@router.get("/inventory/duplicate-check", response_model=DuplicateCheckOut)
async def duplicate_check(supplier_id: int, brand_id: int, product_name: str):
    if not product_name.strip():
        raise HTTPException(status_code=422, detail="product_name must not be empty")
    loop = asyncio.get_event_loop()
    exact = await loop.run_in_executor(None, find_exact_duplicate_master_item, supplier_id, brand_id, product_name)
    cross = await loop.run_in_executor(None, find_cross_supplier_master_items, supplier_id, brand_id, product_name)
    return DuplicateCheckOut(
        exact_match=_row_to_master_item(exact) if exact is not None else None,
        cross_supplier_matches=[_row_to_master_item(r) for r in cross],
    )


@router.get("/inventory/master-items/autocomplete", response_model=list[MasterItemOut])
async def autocomplete(query: str, supplier_id: int | None = None, brand_id: int | None = None, limit: int = 20):
    if not query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, autocomplete_master_items, query, supplier_id, brand_id, limit)
    return [_row_to_master_item(r) for r in rows]


@router.get("/inventory/master-items/by-sku/{sku}", response_model=MasterItemOut)
async def get_master_item_by_sku_route(sku: str):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_master_item_by_sku, sku)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No product found for SKU {sku}")
    return _row_to_master_item(row)


# --- master items --------------------------------------------------------


@router.post("/inventory/master-items", response_model=MasterItemOut, status_code=201)
async def create_new_master_item(req: MasterItemCreateRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    if not req.unit.strip():
        raise HTTPException(status_code=422, detail="unit must not be empty")
    if req.sell_price < 0:
        raise HTTPException(status_code=422, detail="sell_price must be >= 0")
    if req.cost_price < 0:
        raise HTTPException(status_code=422, detail="cost_price must be >= 0")

    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"supplier_id {req.supplier_id} not found")
    if await loop.run_in_executor(None, get_brand, req.brand_id) is None:
        raise HTTPException(status_code=422, detail=f"brand_id {req.brand_id} not found")
    if await loop.run_in_executor(None, get_unit_by_name, req.unit) is None:
        raise HTTPException(status_code=422, detail=f"unit '{req.unit}' is not a valid satuan")

    try:
        row = await loop.run_in_executor(
            None, create_master_item, req.supplier_id, req.brand_id, req.name.strip(), req.unit.strip(),
            req.sell_price, req.cost_price,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_master_item(row)


@router.get("/inventory/master-items", response_model=MasterItemListOut)
async def list_master_items_route(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    supplier_id: int | None = None,
    brand_id: int | None = None,
    is_active: bool | None = None,
):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if page_size < 1 or page_size > 200:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 200")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, list_master_items, page, page_size, search, supplier_id, brand_id, is_active
    )
    return MasterItemListOut(
        items=[_row_to_master_item(r) for r in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get("/inventory/master-items/export.csv")
async def export_master_items_csv():
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_master_items_for_export)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["SKU", "Nama Barang", "Supplier", "Brand", "Satuan", "Stok", "Harga Rata-rata", "Harga Beli Terakhir", "Harga Jual", "Aktif"]
    )
    for r in rows:
        writer.writerow(
            [
                r["sku"], r["name"], r["supplier_name"], r["brand_name"], r["unit"], r["stock_qty"],
                r["avg_cost_price"], r["last_cost_price"], r["sell_price"], "Ya" if r["is_active"] else "Tidak",
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=master-barang.csv"},
    )


@router.get("/inventory/master-items/{master_item_id}", response_model=MasterItemDetailOut)
async def get_master_item_detail(master_item_id: int):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_master_item, master_item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Master item {master_item_id} not found")
    batches = await loop.run_in_executor(None, get_master_item_batch_history, master_item_id)
    base = _row_to_master_item(row)
    return MasterItemDetailOut(**base.model_dump(), batches=[_row_to_batch(b) for b in batches])


@router.patch("/inventory/master-items/{master_item_id}", response_model=MasterItemOut)
async def patch_master_item(master_item_id: int, req: MasterItemUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="name must not be empty")
    if req.unit is not None and not req.unit.strip():
        raise HTTPException(status_code=422, detail="unit must not be empty")
    if req.sell_price is not None and req.sell_price < 0:
        raise HTTPException(status_code=422, detail="sell_price must be >= 0")

    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Master item {master_item_id} not found")
    if req.unit is not None and await loop.run_in_executor(None, get_unit_by_name, req.unit) is None:
        raise HTTPException(status_code=422, detail=f"unit '{req.unit}' is not a valid satuan")
    try:
        row = await loop.run_in_executor(None, update_master_item, master_item_id, req.name, req.sell_price, req.unit)
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_master_item(row)


@router.get("/inventory/master-items/{master_item_id}/ledger", response_model=list[StockLedgerOut])
async def master_item_ledger(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Master item {master_item_id} not found")
    rows = await loop.run_in_executor(None, get_stock_ledger, master_item_id)
    return [_row_to_ledger(r) for r in rows]


@router.delete("/inventory/master-items/{master_item_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_master_item_route(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Master item {master_item_id} not found")
    try:
        await loop.run_in_executor(None, delete_master_item, master_item_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- inbound documents -----------------------------------------------------


@router.post("/inventory/inbound-documents", response_model=InboundDocumentDetail, status_code=201)
async def create_new_inbound_document(req: InboundDocumentCreateRequest):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"supplier_id {req.supplier_id} not found")
    _validate_received_date(req.received_date)
    _validate_inbound_items(req.items)

    items = [i.model_dump() for i in req.items]
    row = await loop.run_in_executor(
        None, create_inbound_document, req.supplier_id, req.received_date, req.supplier_invoice_no, req.notes, items
    )
    document_items = await loop.run_in_executor(None, get_inbound_document_items, row["id"])
    row = await loop.run_in_executor(None, get_inbound_document, row["id"])
    return _row_to_document_detail(row, document_items)


@router.get("/inventory/inbound-documents", response_model=list[InboundDocumentOut])
async def list_inbound_documents_route(status: str | None = None, supplier_id: int | None = None):
    if status is not None and status not in ("draft", "posted", "void"):
        raise HTTPException(status_code=422, detail="status must be one of: draft, posted, void")
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, list_inbound_documents, status, supplier_id)
    return [_row_to_document(r) for r in rows]


@router.get("/inventory/inbound-documents/{document_id}", response_model=InboundDocumentDetail)
async def get_inbound_document_detail(document_id: int):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Inbound document {document_id} not found")
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


@router.put("/inventory/inbound-documents/{document_id}", response_model=InboundDocumentDetail)
async def put_inbound_document(document_id: int, req: InboundDocumentCreateRequest):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Inbound document {document_id} not found")
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"supplier_id {req.supplier_id} not found")
    _validate_received_date(req.received_date)
    _validate_inbound_items(req.items)

    items = [i.model_dump() for i in req.items]
    try:
        await loop.run_in_executor(
            None, update_inbound_document, document_id, req.supplier_id, req.received_date,
            req.supplier_invoice_no, req.notes, items,
        )
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    document_items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, document_items)


@router.post("/inventory/inbound-documents/{document_id}/post", response_model=InboundDocumentDetail)
async def post_document(document_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Inbound document {document_id} not found")
    try:
        await loop.run_in_executor(None, post_inbound_document, document_id)
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


@router.post("/inventory/inbound-documents/{document_id}/void", response_model=InboundDocumentDetail, dependencies=[Depends(require_admin)])
async def void_document(document_id: int, req: VoidRequest):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="reason must not be empty")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Inbound document {document_id} not found")
    try:
        await loop.run_in_executor(None, void_inbound_document, document_id, req.reason.strip())
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


# --- stock adjustments / overview -------------------------------------------


@router.post("/inventory/stock-adjustments", response_model=StockAdjustmentResponse, status_code=201, dependencies=[Depends(require_admin)])
async def create_new_stock_adjustment(req: StockAdjustmentRequest):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="reason must not be empty")
    if req.qty_delta == 0:
        raise HTTPException(status_code=422, detail="qty_delta must not be zero")

    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, req.master_item_id) is None:
        raise HTTPException(status_code=422, detail=f"master_item_id {req.master_item_id} not found")

    try:
        result = await loop.run_in_executor(
            None, create_stock_adjustment, req.master_item_id, req.qty_delta, req.reason.strip()
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return StockAdjustmentResponse(**result)


@router.get("/inventory/overview", response_model=InventoryOverviewOut)
async def inventory_overview():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_inventory_overview)
    return InventoryOverviewOut(
        active_sku_count=result["active_sku_count"],
        total_inventory_value=result["total_inventory_value"],
        inbound_doc_count_30d=result["inbound_doc_count_30d"],
        low_stock_threshold=result["low_stock_threshold"],
        low_stock_items=[_row_to_master_item(r) for r in result["low_stock_items"]],
        pending_invoice_count=result["pending_invoice_count"],
        pending_invoice_outstanding=result["pending_invoice_outstanding"],
        pending_invoices=[PendingInvoiceOut(**r) for r in result["pending_invoices"]],
        latest_incoming=[IncomingItemOut(**r) for r in result["latest_incoming"]],
        top_selling=[TopSellingOut(**r) for r in result["top_selling"]],
        sales_month_revenue=result["sales_month_revenue"],
        sales_month_count=result["sales_month_count"],
    )
