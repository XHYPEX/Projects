import asyncio
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from backend import product_images
from backend.auth import get_current_user, require_admin, require_inbound_poster
from backend.database import (
    reserved_qty_by_item,
    get_stock_reservations,
    category_subtree_ids,
    get_category_row,
    autocomplete_master_items,
    create_brand,
    create_inbound_document,
    create_master_item,
    create_stock_adjustment,
    create_supplier,
    create_unit,
    delete_all_brands,
    delete_all_suppliers,
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
    get_inbound_extras,
    get_supplier_po,
    review_inbound_document,
    get_inbound_document_items,
    get_inventory_overview,
    get_master_item,
    get_master_item_batch_history,
    get_master_item_by_code,
    get_master_item_price_history,
    get_master_item_purchases,
    get_master_item_suppliers,
    get_master_item_usage,
    set_master_item_archived,
    get_master_items_for_export,
    get_stock_ledger,
    get_supplier,
    get_unit,
    get_unit_by_name,
    list_inbound_documents,
    MASTER_SORTS,
    MASTER_STOCK_CLAUSES,
    get_master_items_summary,
    list_master_items,
    post_inbound_document,
    preview_brand_code,
    preview_sku,
    preview_supplier_code,
    search_brands,
    search_suppliers,
    set_master_item_image_path,
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
    MasterItemArchiveRequest,
    OpenPreorderOut,
    InboundPostRequest,
    PendingInvoiceOut,
    ReadyHandoverOut,
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
    # Dispatches on the message text, so it has to recognise both languages:
    # user-facing messages are Indonesian, but older ones are still English and
    # a missed match silently downgrades a 404 into a 409.
    msg = str(e)
    if "tidak ditemukan" in msg:
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=409, detail=msg)


def _validate_received_date(received_date: str) -> None:
    try:
        rd = date.fromisoformat(received_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Tanggal terima tidak valid, gunakan format YYYY-MM-DD.")
    today = date.today()
    if rd > today:
        raise HTTPException(status_code=422, detail="Tanggal terima tidak boleh di masa depan.")
    if (today - rd).days > 30:
        raise HTTPException(status_code=422, detail="Tanggal terima tidak boleh lebih dari 30 hari yang lalu.")


def _validate_inbound_po(req) -> None:
    """A document against a Purchase Order: same supplier, PO issued, every PO line belongs to it and fits its remainder."""
    if req.source is not None and req.source not in ("po_supplier", "manual"):
        raise HTTPException(status_code=422, detail="Sumber harus po_supplier atau manual.")
    if req.source == "manual" and not (req.manual_reason or "").strip():
        raise HTTPException(status_code=422, detail="Alasan penerimaan manual wajib diisi.")
    for it in req.items:
        if it.condition is not None and it.condition not in ("baik", "rusak", "tidak_sesuai"):
            raise HTTPException(status_code=422, detail="Kondisi barang harus baik, rusak, atau tidak_sesuai.")
    if not req.supplier_po_id:
        if any(i.spo_item_id for i in req.items):
            raise HTTPException(status_code=422, detail="Baris Purchase Order butuh Purchase Order terpilih.")
        return
    po = get_supplier_po(req.supplier_po_id)
    if po is None:
        raise HTTPException(status_code=422, detail="Purchase Order tidak ditemukan.")
    if po["supplier_id"] != req.supplier_id:
        raise HTTPException(status_code=422, detail="Supplier dokumen berbeda dari supplier PO.")
    # A draft opened from the PO's own "Terima Barang" carries no PO lines: the PO already counted those goods
    # (and may be complete by now), so only a document that still counts quantities against the PO needs it open.
    if any(i.spo_item_id for i in req.items) and po["status"] not in ("dipesan", "sebagian_diterima"):
        raise HTTPException(status_code=422, detail="Purchase Order belum diterbitkan atau sudah selesai/dibatalkan.")
    lines = {i["id"]: i for i in po["items"]}
    for it in req.items:
        if it.spo_item_id is None:
            continue
        line = lines.get(it.spo_item_id)
        if line is None:
            raise HTTPException(status_code=422, detail="Baris Purchase Order bukan bagian dari PO ini.")
        if line["master_item_id"] != it.master_item_id:
            raise HTTPException(status_code=422, detail="Barang tidak sesuai dengan baris Purchase Order.")
        left = line["quantity"] - line["qty_cancelled"] - line["qty_received"]
        if it.qty_in > left:
            raise HTTPException(status_code=422, detail=f"Jumlah untuk {line['product_name']} melebihi sisa pesanan PO ({left}).")
        # Paket Pengiriman: a line split into packages must say which package the goods came in.
        open_pk = [p for p in po.get("packages", []) if not p["cancelled"]
                   and any(l["item_id"] == it.spo_item_id and l["remaining"] > 0 for l in p["lines"])]
        if it.spo_package_id is None:
            if open_pk:
                raise HTTPException(status_code=422, detail=f"{line['product_name']} sedang dalam paket pengiriman "
                                    f"({', '.join(p['package_no'] for p in open_pk)}). Pilih paket yang datang.")
        else:
            pk = next((p for p in open_pk if p["id"] == it.spo_package_id), None)
            if pk is None:
                raise HTTPException(status_code=422, detail=f"Paket untuk {line['product_name']} tidak ditemukan atau sudah diterima/dibatalkan.")
            remaining = next(l["remaining"] for l in pk["lines"] if l["item_id"] == it.spo_item_id)
            if it.qty_in > remaining:
                raise HTTPException(status_code=422, detail=f"Jumlah untuk {line['product_name']} melebihi sisa paket {pk['package_no']} ({remaining}).")


def _validate_inbound_items(items: list) -> None:
    if not items:
        raise HTTPException(status_code=422, detail="Minimal harus ada satu baris barang.")
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
            raise HTTPException(status_code=422, detail="Jumlah masuk harus bilangan bulat positif.")
        if item.cost_price < 0:
            raise HTTPException(status_code=422, detail="Harga modal tidak boleh negatif.")

        if get_master_item(item.master_item_id) is None:
            raise HTTPException(status_code=422, detail=f"Barang dengan ID {item.master_item_id} tidak ditemukan.")
        key = ("existing", item.master_item_id)

        if key in seen:
            raise HTTPException(
                status_code=422,
                detail="Baris barang ganda: dua baris di dokumen ini merujuk ke barang yang sama — gabungkan menjadi satu baris.",
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
        description=row.get("description"),
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        deactivated_reason=row.get("deactivated_reason"),
        product_count=row.get("product_count", 0),
        active_product_count=row.get("active_product_count", 0),
        in_use=bool(row.get("in_use", False)),
    )


def _reserved_map(rows: list[dict]) -> dict[int, int]:
    """One reservation lookup for a whole list of items (K-02)."""
    return reserved_qty_by_item([r["id"] for r in rows]) if rows else {}


def _row_to_master_item(row: dict, reserved: dict[int, int] | None = None) -> MasterItemOut:
    if reserved is None:
        reserved = reserved_qty_by_item([row["id"]])
    held = reserved.get(row["id"], 0)
    return MasterItemOut(
        reserved_qty=held,
        available_qty=max(row["stock_qty"] - held, 0),
        quarantine_qty=row.get("quarantine_qty") or 0,
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
        barcode=row.get("barcode"),
        image_url=product_images.image_url(row["id"], row.get("image_path")),
        description=row.get("description"),
        min_stock=row.get("min_stock") if row.get("min_stock") is not None else 5,
        location=row.get("location"),
        archived_reason=row.get("archived_reason"),
        archived_note=row.get("archived_note"),
        archived_at=row.get("archived_at"),
        archive_mode=row.get("archive_mode"),
        updated_at=row.get("updated_at"),
        category_id=row.get("category_id"),
        category_name=row.get("category_name"),
        category_parent_id=row.get("category_parent_id"),
        category_parent_name=row.get("category_parent_name"),
        updated_by=row.get("updated_by"),
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
        spo_item_id=row.get("spo_item_id"),
        spo_package_id=row.get("spo_package_id"),
        condition=row.get("condition"),
        location=row.get("location"),
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
        **{k: row.get(k) for k in ("supplier_po_id", "supplier_po_number", "surat_jalan_no", "officer", "source",
                                   "manual_reason", "created_by", "reviewed_by", "reviewed_at", "posted_by", "posted_at")},
        item_count=row.get("item_count") or 0,
        qty_total=row.get("qty_total") or 0,
    )


def _row_to_document_detail(row: dict, items: list[dict]) -> InboundDocumentDetail:
    base = _row_to_document(row)
    return InboundDocumentDetail(**base.model_dump(), items=[_row_to_inbound_item(i) for i in items],
                                 **get_inbound_extras(row["id"]))


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
        created_by=row.get("created_by"),
        ref_doc_number=row.get("ref_doc_number"),
        actor=row.get("actor"),
        ref_receipt_id=row.get("ref_receipt_id"),
        supplier_po_number=row.get("supplier_po_number"),
        supplier_po_id=row.get("supplier_po_id"),
    )


# --- suppliers ---------------------------------------------------------


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_new_supplier(req: SupplierRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
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
            raise HTTPException(status_code=422, detail="Kata pencarian tidak boleh kosong.")
        rows = await loop.run_in_executor(None, search_suppliers, search.strip(), True, 8)
    else:
        rows = await loop.run_in_executor(None, get_all_suppliers, active_only)
    return [_row_to_supplier(r) for r in rows]


@router.get("/suppliers/code-preview", response_model=CodePreviewOut)
async def supplier_code_preview(name: str):
    if not name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, preview_supplier_code, name.strip())
    return CodePreviewOut(code=code)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
async def patch_supplier(supplier_id: int, req: SupplierUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, supplier_id) is None:
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} tidak ditemukan.")
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
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, delete_supplier, supplier_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- brands --------------------------------------------------------------


@router.post("/brands", response_model=BrandOut, status_code=201)
async def create_new_brand(req: BrandRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
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
            raise HTTPException(status_code=422, detail="Kata pencarian tidak boleh kosong.")
        rows = await loop.run_in_executor(None, search_brands, search.strip(), True, 8)
    else:
        rows = await loop.run_in_executor(None, get_all_brands, active_only)
    return [_row_to_brand(r) for r in rows]


@router.get("/brands/code-preview", response_model=CodePreviewOut)
async def brand_code_preview(name: str):
    if not name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(None, preview_brand_code, name.strip())
    return CodePreviewOut(code=code)


@router.patch("/brands/{brand_id}", response_model=BrandOut)
async def patch_brand(brand_id: int, req: BrandUpdateRequest):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_brand, brand_id) is None:
        raise HTTPException(status_code=404, detail=f"Merek {brand_id} tidak ditemukan.")
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
        raise HTTPException(status_code=404, detail=f"Merek {brand_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, delete_brand, brand_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- units (Satuan) ---------------------------------------------------------


@router.post("/units", response_model=UnitOut, status_code=201)
async def create_new_unit(req: UnitRequest):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    try:
        row = await loop.run_in_executor(None, create_unit, req.name.strip(), req.description)
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
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail=f"Satuan {unit_id} tidak ditemukan.")
    try:
        row = await loop.run_in_executor(
            None, lambda: update_unit(unit_id, req.name, req.is_active, req.description, req.reason, req.confirm_rename,
                                      fields_set=set(req.model_fields_set)))
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_unit(row)


@router.delete("/units/{unit_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_unit_route(unit_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail=f"Satuan {unit_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, delete_unit, unit_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return Response(status_code=204)


# --- sku preview / duplicate check / autocomplete --------------------------


@router.get("/inventory/sku-preview", response_model=SkuPreviewOut)
async def sku_preview(supplier_id: int, brand_id: int, product_name: str):
    if not product_name.strip():
        raise HTTPException(status_code=422, detail="Nama barang wajib diisi.")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, preview_sku, supplier_id, brand_id, product_name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return SkuPreviewOut(**result)


@router.get("/inventory/duplicate-check", response_model=DuplicateCheckOut)
async def duplicate_check(supplier_id: int, brand_id: int, product_name: str):
    if not product_name.strip():
        raise HTTPException(status_code=422, detail="Nama barang wajib diisi.")
    loop = asyncio.get_event_loop()
    exact = await loop.run_in_executor(None, find_exact_duplicate_master_item, supplier_id, brand_id, product_name)
    cross = await loop.run_in_executor(None, find_cross_supplier_master_items, supplier_id, brand_id, product_name)
    return DuplicateCheckOut(
        exact_match=_row_to_master_item(exact) if exact is not None else None,
        cross_supplier_matches=[_row_to_master_item(r, res) for res in [_reserved_map(cross)] for r in cross],
    )


@router.get("/inventory/master-items/autocomplete", response_model=list[MasterItemOut])
async def autocomplete(query: str, supplier_id: int | None = None, brand_id: int | None = None, limit: int = 20):
    if not query.strip():
        raise HTTPException(status_code=422, detail="Kata kunci tidak boleh kosong.")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="Limit harus antara 1 dan 100.")
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, autocomplete_master_items, query, supplier_id, brand_id, limit)
    res = _reserved_map(rows)
    return [_row_to_master_item(r, res) for r in rows]


@router.get("/inventory/master-items/by-sku/{sku}", response_model=MasterItemOut)
async def get_master_item_by_sku_route(sku: str):
    # `sku` here is whatever the cashier's scanner/keyboard sent -- a CODE128
    # barcode or a SKU. See get_master_item_by_code for how that's resolved;
    # the route path/param name stay as-is so nothing on the frontend changes.
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_master_item_by_code, sku)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Barang dengan kode {sku} tidak ditemukan.")
    return _row_to_master_item(row)


# --- master items --------------------------------------------------------


async def _require_assignable_category(loop, category_id: int | None) -> None:
    """A product may only be put in an existing, active category (not archived/nonaktif)."""
    if category_id is None:
        return
    cat = await loop.run_in_executor(None, get_category_row, category_id)
    if cat is None:
        raise HTTPException(status_code=422, detail=f"Kategori dengan ID {category_id} tidak ditemukan.")
    if cat["status"] != "aktif":
        raise HTTPException(status_code=422, detail=f"Kategori '{cat['name']}' tidak aktif, pilih kategori lain.")


@router.post("/inventory/master-items", response_model=MasterItemOut, status_code=201)
async def create_new_master_item(req: MasterItemCreateRequest, user: dict = Depends(get_current_user)):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    if not req.unit.strip():
        raise HTTPException(status_code=422, detail="Satuan wajib diisi.")
    if req.sell_price < 0:
        raise HTTPException(status_code=422, detail="Harga jual tidak boleh negatif.")
    if req.cost_price < 0:
        raise HTTPException(status_code=422, detail="Harga modal tidak boleh negatif.")
    if req.stock_qty < 0:
        raise HTTPException(status_code=422, detail="Stok tidak boleh negatif.")
    if req.min_stock < 0:
        raise HTTPException(status_code=422, detail="Stok minimum tidak boleh negatif.")

    loop = asyncio.get_event_loop()
    await _require_assignable_category(loop, req.category_id)
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"Supplier dengan ID {req.supplier_id} tidak ditemukan.")
    if await loop.run_in_executor(None, get_brand, req.brand_id) is None:
        raise HTTPException(status_code=422, detail=f"Merek dengan ID {req.brand_id} tidak ditemukan.")
    if await loop.run_in_executor(None, get_unit_by_name, req.unit) is None:
        raise HTTPException(status_code=422, detail=f"Satuan '{req.unit}' tidak valid.")

    try:
        row = await loop.run_in_executor(
            None, create_master_item, req.supplier_id, req.brand_id, req.name.strip(), req.unit.strip(),
            req.sell_price, (req.cost_price if user["role"] == "admin" else 0), req.stock_qty, req.description, req.min_stock, req.location,
            user["username"], req.category_id, req.is_active,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_master_item(await loop.run_in_executor(None, get_master_item, row["id"]))


@router.get("/inventory/master-items/summary")
async def master_items_summary():
    """Counts for the Master Barang header tiles. Declared before /master-items/{id}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_master_items_summary)


@router.get("/inventory/master-items", response_model=MasterItemListOut)
async def list_master_items_route(
    user: dict = Depends(get_current_user),
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    supplier_id: int | None = None,
    brand_id: int | None = None,
    is_active: bool | None = None,
    stock_status: str | None = None,
    stock_min: int | None = None,
    stock_max: int | None = None,
    no_price: bool = False,
    sort: str = "newest",
    category_id: int | None = None,
    archived: bool = False,
):
    if stock_status is not None and stock_status not in MASTER_STOCK_CLAUSES:
        raise HTTPException(status_code=422, detail=f"stock_status harus salah satu dari: {', '.join(MASTER_STOCK_CLAUSES)}.")
    if sort not in MASTER_SORTS:
        raise HTTPException(status_code=422, detail=f"sort harus salah satu dari: {', '.join(MASTER_SORTS)}.")
    if sort == "margin_desc" and user["role"] != "admin":
        sort = "newest"  # margin order would reveal the cost
    if stock_min is not None and stock_max is not None and stock_min > stock_max:
        raise HTTPException(status_code=422, detail="Stok minimum tidak boleh lebih besar dari stok maksimum.")
    if page < 1:
        raise HTTPException(status_code=422, detail="Halaman minimal 1.")
    if page_size < 1 or page_size > 200:
        raise HTTPException(status_code=422, detail="Ukuran halaman harus antara 1 dan 200.")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: list_master_items(
            page, page_size, search, supplier_id, brand_id, is_active,
            stock_status=stock_status, stock_min=stock_min, stock_max=stock_max, no_price=no_price, sort=sort,
            category_ids=(category_subtree_ids(category_id) if category_id is not None else None),
            archived=archived,
        ),
    )
    return MasterItemListOut(
        items=[_row_to_master_item(r, res) for res in [_reserved_map(result["items"])] for r in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get("/inventory/master-items/export.csv")
async def export_master_items_csv(user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_master_items_for_export)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["SKU", "Nama Barang", "Supplier", "Brand", "Satuan", "Stok", "Stok Minimum", "Lokasi", "Deskripsi", "Harga Rata-rata", "Harga Beli Terakhir", "Harga Jual", "Aktif"]
    )
    is_admin = user["role"] == "admin"
    for r in rows:
        writer.writerow(
            [
                r["sku"], r["name"], r["supplier_name"], r["brand_name"], r["unit"], r["stock_qty"],
                r["min_stock"], r["location"] or "", r["description"] or "", r["avg_cost_price"] if is_admin else "", r["last_cost_price"] if is_admin else "", r["sell_price"], "Ya" if r["is_active"] else "Tidak",
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
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    batches = await loop.run_in_executor(None, get_master_item_batch_history, master_item_id)
    base = _row_to_master_item(row)
    return MasterItemDetailOut(**base.model_dump(), batches=[_row_to_batch(b) for b in batches])


@router.patch("/inventory/master-items/{master_item_id}", response_model=MasterItemOut)
async def patch_master_item(master_item_id: int, req: MasterItemUpdateRequest, user: dict = Depends(get_current_user)):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    if req.unit is not None and not req.unit.strip():
        raise HTTPException(status_code=422, detail="Satuan wajib diisi.")
    if req.sell_price is not None and req.sell_price < 0:
        raise HTTPException(status_code=422, detail="Harga jual tidak boleh negatif.")
    if req.min_stock is not None and req.min_stock < 0:
        raise HTTPException(status_code=422, detail="Stok minimum tidak boleh negatif.")

    loop = asyncio.get_event_loop()
    current = await loop.run_in_executor(None, get_master_item, master_item_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    if req.expected_updated_at and current.get("updated_at") and req.expected_updated_at != current["updated_at"]:
        raise HTTPException(status_code=409, detail="Data ini baru saja diubah oleh pengguna lain. Muat ulang halaman lalu ulangi perubahan Anda.")
    if req.unit is not None and await loop.run_in_executor(None, get_unit_by_name, req.unit) is None:
        raise HTTPException(status_code=422, detail=f"Satuan '{req.unit}' tidak valid.")
    new_supplier_id = None
    if req.supplier_id is not None and req.supplier_id != current["supplier_id"]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Hanya admin yang dapat mengganti supplier barang.")
        if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
            raise HTTPException(status_code=422, detail=f"Supplier dengan ID {req.supplier_id} tidak ditemukan.")
        new_supplier_id = req.supplier_id
    set_category = "category_id" in req.model_fields_set
    if set_category and req.category_id != current.get("category_id"):
        await _require_assignable_category(loop, req.category_id)
    try:
        row = await loop.run_in_executor(
            None, lambda: update_master_item(
                master_item_id, req.name, req.sell_price, req.unit, req.description, req.min_stock,
                req.location, user["username"], req.price_note,
                set_category=set_category, category_id=req.category_id, supplier_id=new_supplier_id,
            ),
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return _row_to_master_item(await loop.run_in_executor(None, get_master_item, master_item_id))


@router.post("/inventory/master-items/{master_item_id}/archive", response_model=MasterItemOut)
async def archive_master_item(master_item_id: int, req: MasterItemArchiveRequest, user: dict = Depends(get_current_user)):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="Alasan pengarsipan wajib dipilih.")
    if req.mode not in ("nonaktif", "arsip"):
        raise HTTPException(status_code=422, detail="Tindakan pengarsipan harus 'nonaktif' atau 'arsip'.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    await loop.run_in_executor(None, set_master_item_archived, master_item_id, True, req.reason, req.note, user["username"], req.mode)
    return _row_to_master_item(await loop.run_in_executor(None, get_master_item, master_item_id))


@router.post("/inventory/master-items/{master_item_id}/restore", response_model=MasterItemOut)
async def restore_master_item(master_item_id: int, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    await loop.run_in_executor(None, set_master_item_archived, master_item_id, False, None, None, user["username"])
    return _row_to_master_item(await loop.run_in_executor(None, get_master_item, master_item_id))


@router.get("/inventory/master-items/{master_item_id}/usage")
async def master_item_usage(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    return await loop.run_in_executor(None, get_master_item_usage, master_item_id)


@router.get("/inventory/master-items/{master_item_id}/reservations")
async def master_item_reservations(master_item_id: int):
    """Which Sales Orders hold this item's shelf stock (K-02), for the Alokasi SO tab."""
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_master_item, master_item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    lines = await loop.run_in_executor(None, get_stock_reservations, master_item_id)
    held = sum(ln["reserved"] for ln in lines)
    return {"stock_qty": row["stock_qty"], "reserved_qty": held, "available_qty": max(row["stock_qty"] - held, 0), "lines": lines}


@router.get("/inventory/master-items/{master_item_id}/prices")
async def master_item_prices(master_item_id: int):
    """Price history, suppliers and recent purchases for the Riwayat Harga & Supplier page."""
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    return {
        "history": await loop.run_in_executor(None, get_master_item_price_history, master_item_id),
        "suppliers": await loop.run_in_executor(None, get_master_item_suppliers, master_item_id),
        "purchases": await loop.run_in_executor(None, get_master_item_purchases, master_item_id),
    }


@router.get("/inventory/master-items/{master_item_id}/ledger", response_model=list[StockLedgerOut])
async def master_item_ledger(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    rows = await loop.run_in_executor(None, get_stock_ledger, master_item_id)
    return [_row_to_ledger(r) for r in rows]


@router.delete("/inventory/master-items/{master_item_id}", status_code=204, response_class=Response, dependencies=[Depends(require_admin)])
async def delete_master_item_route(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    try:
        image_filename = await loop.run_in_executor(None, delete_master_item, master_item_id)
    except ValueError as e:
        _raise_from_value_error(e)
    # Only after the delete committed: a rejected delete (item has history) must
    # keep its photo.
    await loop.run_in_executor(None, product_images.delete_image_file, image_filename)
    return Response(status_code=204)


# --- master item photo -------------------------------------------------------
# Upload is a raw-body PUT (the body *is* the image, Content-Type names its
# format) so no multipart parser dependency is needed. The file lives on disk
# under a server-generated name (backend/product_images.py); the row only keeps
# that name. Auth is the router-level get_current_user, exactly like PATCH on
# the item -- any logged-in user may set or clear a photo, only deleting the
# item itself is admin-only.


async def _read_image_body(request: Request) -> bytes:
    too_big = HTTPException(status_code=413, detail="Ukuran gambar maksimal 2 MB.")
    declared = request.headers.get("content-length", "")
    # Reject on the declared size before reading anything; the running total below
    # still guards chunked bodies and a Content-Length that lies.
    if declared.isdigit() and int(declared) > product_images.MAX_IMAGE_BYTES:
        raise too_big
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > product_images.MAX_IMAGE_BYTES:
            raise too_big
    return bytes(data)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = [c.strip().removeprefix("W/") for c in if_none_match.split(",")]
    return "*" in candidates or etag in candidates


@router.put("/inventory/master-items/{master_item_id}/image", response_model=MasterItemOut)
async def put_master_item_image(master_item_id: int, request: Request):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in product_images.IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail="Format gambar harus JPEG, PNG, atau WebP (Content-Type: image/jpeg, image/png, atau image/webp).",
        )
    data = await _read_image_body(request)
    if not data:
        raise HTTPException(status_code=422, detail="File gambar kosong.")
    # The header is client-controlled; the bytes have to agree with it.
    if not product_images.matches_content_type(content_type, data):
        raise HTTPException(
            status_code=422,
            detail=f"Isi file bukan gambar {content_type.split('/')[1].upper()} yang valid (tidak cocok dengan Content-Type {content_type}).",
        )

    filename = await loop.run_in_executor(None, product_images.save_image, data, content_type)
    try:
        previous = await loop.run_in_executor(None, set_master_item_image_path, master_item_id, filename)
    except Exception as e:
        # The new file was never referenced by a row; don't leave it behind.
        await loop.run_in_executor(None, product_images.delete_image_file, filename)
        if isinstance(e, ValueError):
            _raise_from_value_error(e)
        raise
    await loop.run_in_executor(None, product_images.delete_image_file, previous)

    row = await loop.run_in_executor(None, get_master_item, master_item_id)
    if row is None:
        # Deleted by someone else between the update and this read. Their delete
        # removed the file that row pointed at, so there is nothing to clean up.
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    return _row_to_master_item(row)


@router.get("/inventory/master-items/{master_item_id}/image")
async def get_master_item_image(master_item_id: int, request: Request, v: str | None = None):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_master_item, master_item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    filename = row.get("image_path")
    path = await loop.run_in_executor(None, product_images.image_file, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Barang ini belum memiliki foto.")

    version = product_images.image_version(filename)
    etag = f'"{filename.rsplit(".", 1)[0]}"'  # a filename is never reused, so it is a strong validator
    headers = {
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        # "private": the photo is behind the login, shared caches must not keep it.
        # The URL the API hands out carries the current ?v= token, which changes on
        # every upload, so it can be cached for good. A request without the current
        # token (a hand-typed or stale URL) must revalidate instead.
        "Cache-Control": "private, max-age=31536000, immutable" if v == version else "private, no-cache",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=product_images.media_type_for(filename), headers=headers)


@router.delete("/inventory/master-items/{master_item_id}/image", status_code=204, response_class=Response)
async def delete_master_item_image(master_item_id: int):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, master_item_id) is None:
        raise HTTPException(status_code=404, detail=f"Barang {master_item_id} tidak ditemukan.")
    try:
        previous = await loop.run_in_executor(None, set_master_item_image_path, master_item_id, None)
    except ValueError as e:
        _raise_from_value_error(e)
    # No photo to begin with is fine: previous is None and this is a no-op, so the
    # call is idempotent.
    await loop.run_in_executor(None, product_images.delete_image_file, previous)
    return Response(status_code=204)


# --- inbound documents -----------------------------------------------------


@router.post("/inventory/inbound-documents", response_model=InboundDocumentDetail, status_code=201)
async def create_new_inbound_document(req: InboundDocumentCreateRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"Supplier dengan ID {req.supplier_id} tidak ditemukan.")
    _validate_received_date(req.received_date)
    _validate_inbound_items(req.items)
    await loop.run_in_executor(None, _validate_inbound_po, req)

    items = [i.model_dump() for i in req.items]
    meta = req.model_dump(include={"supplier_po_id", "surat_jalan_no", "officer", "source", "manual_reason"})
    if not meta.get("source"):
        meta["source"] = "po_supplier" if req.supplier_po_id else "manual"
    row = await loop.run_in_executor(
        None, create_inbound_document, req.supplier_id, req.received_date, req.supplier_invoice_no, req.notes, items,
        meta, user["username"],
    )
    document_items = await loop.run_in_executor(None, get_inbound_document_items, row["id"])
    row = await loop.run_in_executor(None, get_inbound_document, row["id"])
    return _row_to_document_detail(row, document_items)


@router.get("/inventory/inbound-documents", response_model=list[InboundDocumentOut])
async def list_inbound_documents_route(
    status: str | None = None, supplier_id: int | None = None, q: str | None = None,
    date_from: str | None = None, date_to: str | None = None, supplier_po_id: str | None = None,
):
    if status is not None and status not in ("draft", "posted", "void"):
        raise HTTPException(status_code=422, detail="Status harus salah satu dari: draft, posted, void.")
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, list_inbound_documents, status, supplier_id, q or None, date_from or None, date_to or None, supplier_po_id or None)
    return [_row_to_document(r) for r in rows]


@router.get("/inventory/inbound-documents/{document_id}", response_model=InboundDocumentDetail)
async def get_inbound_document_detail(document_id: int):
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Dokumen barang masuk {document_id} tidak ditemukan.")
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


@router.put("/inventory/inbound-documents/{document_id}", response_model=InboundDocumentDetail)
async def put_inbound_document(document_id: int, req: InboundDocumentCreateRequest, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Dokumen barang masuk {document_id} tidak ditemukan.")
    if await loop.run_in_executor(None, get_supplier, req.supplier_id) is None:
        raise HTTPException(status_code=422, detail=f"Supplier dengan ID {req.supplier_id} tidak ditemukan.")
    _validate_received_date(req.received_date)
    _validate_inbound_items(req.items)
    await loop.run_in_executor(None, _validate_inbound_po, req)

    items = [i.model_dump() for i in req.items]
    meta = req.model_dump(include={"supplier_po_id", "surat_jalan_no", "officer", "source", "manual_reason"})
    if not meta.get("source"):
        meta["source"] = "po_supplier" if req.supplier_po_id else "manual"
    try:
        await loop.run_in_executor(
            None, update_inbound_document, document_id, req.supplier_id, req.received_date,
            req.supplier_invoice_no, req.notes, items, meta, user["username"],
        )
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    document_items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, document_items)


@router.post("/inventory/inbound-documents/{document_id}/review", response_model=InboundDocumentDetail)
async def review_document(document_id: int, user: dict = Depends(get_current_user)):
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Dokumen barang masuk {document_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, review_inbound_document, document_id, user["username"])
    except ValueError as e:
        _raise_from_value_error(e)
    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


@router.post("/inventory/inbound-documents/{document_id}/post", response_model=InboundDocumentDetail)
async def post_document(document_id: int, req: InboundPostRequest | None = None, user: dict = Depends(require_inbound_poster)):
    req = req or InboundPostRequest()
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Dokumen barang masuk {document_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, lambda: post_inbound_document(
            document_id, user["username"], req.accept_over, req.over_reason, user.get("role") == "admin"))
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


@router.post("/inventory/inbound-documents/{document_id}/void", response_model=InboundDocumentDetail, dependencies=[Depends(require_admin)])
async def void_document(document_id: int, req: VoidRequest, user: dict = Depends(get_current_user)):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="Alasan wajib diisi.")
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_inbound_document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Dokumen barang masuk {document_id} tidak ditemukan.")
    try:
        await loop.run_in_executor(None, void_inbound_document, document_id, req.reason.strip(), user["username"])
    except ValueError as e:
        _raise_from_value_error(e)

    row = await loop.run_in_executor(None, get_inbound_document, document_id)
    items = await loop.run_in_executor(None, get_inbound_document_items, document_id)
    return _row_to_document_detail(row, items)


# --- stock adjustments / overview -------------------------------------------


@router.post("/inventory/stock-adjustments", response_model=StockAdjustmentResponse, status_code=201, dependencies=[Depends(require_admin)])
async def create_new_stock_adjustment(req: StockAdjustmentRequest, user: dict = Depends(get_current_user)):
    if not req.reason.strip():
        raise HTTPException(status_code=422, detail="Alasan penyesuaian wajib diisi.")
    if req.qty_delta == 0:
        raise HTTPException(status_code=422, detail="Jumlah penyesuaian tidak boleh nol.")

    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, get_master_item, req.master_item_id) is None:
        raise HTTPException(status_code=422, detail=f"Barang dengan id {req.master_item_id} tidak ditemukan.")

    try:
        result = await loop.run_in_executor(
            None, create_stock_adjustment, req.master_item_id, req.qty_delta, req.reason.strip(), user["username"]
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
        low_stock_items=[_row_to_master_item(r, res) for res in [_reserved_map(result["low_stock_items"])] for r in result["low_stock_items"]],
        pending_invoice_count=result["pending_invoice_count"],
        pending_invoice_outstanding=result["pending_invoice_outstanding"],
        pending_invoices=[PendingInvoiceOut(**r) for r in result["pending_invoices"]],
        ready_handover=[ReadyHandoverOut(**r) for r in result["ready_handover"]],
        latest_incoming=[IncomingItemOut(**r) for r in result["latest_incoming"]],
        top_selling=[TopSellingOut(**r) for r in result["top_selling"]],
        sales_month_revenue=result["sales_month_revenue"],
        sales_month_count=result["sales_month_count"],
        sales_closed_month_revenue=result["sales_closed_month_revenue"],
        sales_closed_month_count=result["sales_closed_month_count"],
        preorder_open_count=result["preorder_open_count"],
        preorder_outstanding=result["preorder_outstanding"],
        preorder_ready_count=result["preorder_ready_count"],
        preorders=[OpenPreorderOut(**r) for r in result["preorders"]],
    )
