from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    keyword: str
    city: str
    kecamatan: list[str]


class ScrapeResponse(BaseModel):
    job_id: str


class JobStatus(BaseModel):
    job_id: str
    keyword: str
    city: str
    kecamatan: list[str]
    status: str
    progress: int
    error: str | None
    created_at: str
    completed_at: str | None


class PlaceOut(BaseModel):
    id: int
    name: str
    address: str
    phone: str
    lat: float
    lng: float


VALID_RECEIPT_STATUSES = {"pending", "done", "void"}


class ReceiptItemRequest(BaseModel):
    product_name: str
    quantity: int
    unit_price: int
    warranty_date: str | None = None


class ReceiptRequest(BaseModel):
    plate_region: str
    plate_number: str
    plate_suffix: str
    customer_phone: str
    customer_name: str | None = None
    items: list[ReceiptItemRequest]
    discount: int = 0
    amount_paid: int = 0
    status: str | None = None  # only "void" is accepted here — pending/done are computed from amount_paid vs total


class ReceiptUpdateRequest(BaseModel):
    plate_region: str | None = None
    plate_number: str | None = None
    plate_suffix: str | None = None
    # customer_name uses a sentinel default so "omitted" (leave as-is) is distinguishable
    # from "sent as null" (clear it); the route checks fields_set to tell them apart.
    customer_phone: str | None = None
    customer_name: str | None = None
    items: list[ReceiptItemRequest] | None = None
    discount: int | None = None
    amount_paid: int | None = None
    status: str | None = None  # only "void" is accepted here — pending/done are computed from amount_paid vs total


class ReceiptResponse(BaseModel):
    receipt_id: str
    subtotal: int
    discount: int
    total: int
    amount_paid: int
    status: str
    customer_phone: str
    customer_name: str | None = None


class ReceiptItemOut(BaseModel):
    id: int
    product_name: str
    quantity: int
    unit_price: int
    warranty_date: str | None


class ReceiptOut(BaseModel):
    id: str
    plate_region: str
    plate_number: str
    plate_suffix: str
    plate_full: str
    customer_phone: str
    customer_name: str | None = None
    subtotal: int
    discount: int
    total: int
    amount_paid: int
    status: str
    created_at: str
    item_count: int


class ReceiptDetail(ReceiptOut):
    items: list[ReceiptItemOut]


class SalesDayOut(BaseModel):
    date: str
    revenue: int
    receipt_count: int


class SalesSummaryOut(BaseModel):
    total_revenue: int
    receipt_count: int
    daily: list[SalesDayOut]
    top_days: list[SalesDayOut]


# ---------------------------------------------------------------------------
# Barang Masuk / Master Barang (inventory & procurement) module
# ---------------------------------------------------------------------------


class SupplierRequest(BaseModel):
    name: str
    code: str | None = None
    phone: str | None = None
    address: str | None = None
    contact_person: str | None = None
    npwp: str | None = None


class SupplierUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    phone: str | None = None
    address: str | None = None
    contact_person: str | None = None
    npwp: str | None = None


class SupplierOut(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool
    created_at: str
    is_system: bool
    source: str
    phone: str | None = None
    address: str | None = None
    contact_person: str | None = None
    npwp: str | None = None
    score: float | None = None


class BrandRequest(BaseModel):
    name: str
    code: str | None = None


class BrandUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class BrandOut(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool
    created_at: str
    is_system: bool
    source: str
    score: float | None = None


class UnitRequest(BaseModel):
    name: str


class UnitUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class UnitOut(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: str


class CodePreviewOut(BaseModel):
    code: str


class BulkDeleteOut(BaseModel):
    deleted_count: int
    skipped_count: int


class SkuPreviewOut(BaseModel):
    segment: str
    prefix: str
    next_seq: int
    sku: str


class MasterItemOut(BaseModel):
    id: int
    sku: str
    sku_prefix: str
    sku_seq: int
    supplier_id: int
    brand_id: int
    supplier_name: str | None = None
    brand_name: str | None = None
    name: str
    name_normalized: str
    unit: str
    last_cost_price: int
    avg_cost_price: int
    sell_price: int
    stock_qty: int
    first_received_date: str | None
    is_active: bool
    created_at: str


class DuplicateCheckOut(BaseModel):
    exact_match: MasterItemOut | None
    cross_supplier_matches: list[MasterItemOut]


class MasterItemCreateRequest(BaseModel):
    supplier_id: int
    brand_id: int
    name: str
    unit: str
    sell_price: int = 0
    cost_price: int = 0


class MasterItemUpdateRequest(BaseModel):
    name: str | None = None
    sell_price: int | None = None
    unit: str | None = None


class MasterItemListOut(BaseModel):
    items: list[MasterItemOut]
    total: int
    page: int
    page_size: int


class MasterItemBatchOut(BaseModel):
    inbound_item_id: int
    document_id: int
    doc_number: str
    document_status: str
    qty_in: int
    qty_remaining: int
    cost_price: int
    received_date: str


class MasterItemDetailOut(MasterItemOut):
    batches: list[MasterItemBatchOut]


class InboundItemRequest(BaseModel):
    master_item_id: int | None = None
    pending_brand_id: int | None = None
    pending_product_name: str | None = None
    pending_unit: str | None = None
    pending_sell_price: int | None = None
    qty_in: int
    cost_price: int


class InboundItemOut(BaseModel):
    id: int
    document_id: int
    master_item_id: int | None
    pending_brand_id: int | None
    pending_product_name: str | None
    pending_unit: str | None
    pending_sell_price: int | None
    qty_in: int
    cost_price: int
    qty_remaining: int | None
    received_date: str | None
    resolved_sku: str | None = None
    resolved_name: str | None = None
    resolved_brand_id: int | None = None
    resolved_brand_name: str | None = None
    resolved_unit: str | None = None
    resolved_sell_price: int | None = None
    pending_brand_name: str | None = None


class InboundDocumentCreateRequest(BaseModel):
    supplier_id: int
    received_date: str
    supplier_invoice_no: str | None = None
    notes: str | None = None
    items: list[InboundItemRequest]


class InboundDocumentOut(BaseModel):
    id: int
    doc_number: str
    supplier_id: int
    supplier_name: str | None = None
    received_date: str
    supplier_invoice_no: str | None
    notes: str | None
    status: str
    total_value: int
    created_at: str


class InboundDocumentDetail(InboundDocumentOut):
    items: list[InboundItemOut]


class VoidRequest(BaseModel):
    reason: str


VALID_PURCHASE_INVOICE_STATUSES = {"draft", "posted", "void"}
VALID_PAYMENT_STATUSES = {"belum", "sebagian", "lunas"}


class PurchaseInvoiceItemRequest(BaseModel):
    master_item_id: int | None = None  # null when the line is free text (product not in Master Barang)
    product_name: str
    sku: str | None = None
    unit: str | None = None
    qty: int
    unit_price: int


class PurchaseInvoiceItemOut(BaseModel):
    id: int
    invoice_id: int
    master_item_id: int | None
    product_name: str
    sku: str | None
    unit: str
    qty: int
    unit_price: int
    line_total: int
    line_no: int
    current_sku: str | None = None
    current_stock_qty: int | None = None


class PurchaseInvoiceRequest(BaseModel):
    supplier_id: int
    invoice_date: str
    due_date: str | None = None  # derived from payment_terms when omitted
    payment_terms: str = "NET 30"
    supplier_invoice_no: str | None = None
    notes: str | None = None
    discount: int = 0
    tax_rate: float = 0
    items: list[PurchaseInvoiceItemRequest]


class PurchaseInvoicePaymentRequest(BaseModel):
    amount: int


class PurchaseInvoiceOut(BaseModel):
    id: int
    invoice_number: str
    supplier_id: int
    supplier_name: str | None = None
    supplier_invoice_no: str | None
    supplier_name_snapshot: str
    supplier_phone_snapshot: str | None
    supplier_address_snapshot: str | None
    supplier_contact_snapshot: str | None
    supplier_npwp: str | None = None
    invoice_date: str
    payment_terms: str
    due_date: str
    subtotal: int
    discount: int
    tax_rate: float
    tax_amount: int
    total: int
    amount_paid: int
    outstanding: int
    payment_status: str
    is_overdue: bool
    status: str
    notes: str | None
    void_reason: str | None
    created_at: str
    updated_at: str


class PurchaseInvoiceDetail(PurchaseInvoiceOut):
    items: list[PurchaseInvoiceItemOut]


class PayableSummaryOut(BaseModel):
    posted_count: int
    unpaid_count: int
    outstanding_total: int
    overdue_count: int
    overdue_total: int


class StockAdjustmentRequest(BaseModel):
    master_item_id: int
    qty_delta: int
    reason: str


class StockAdjustmentResponse(BaseModel):
    master_item_id: int
    qty_delta: int
    stock_qty: int


class StockLedgerOut(BaseModel):
    id: int
    master_item_id: int
    type: str
    qty: int
    balance_after: int
    ref_type: str | None
    ref_id: int | None
    note: str | None
    created_at: str


class PendingInvoiceOut(BaseModel):
    id: str
    plate_full: str
    customer_name: str | None
    total: int
    amount_paid: int
    outstanding: int
    created_at: str


class IncomingItemOut(BaseModel):
    product_name: str
    sku: str
    supplier_name: str
    qty_in: int
    received_date: str | None


class TopSellingOut(BaseModel):
    product_name: str
    units_sold: int
    revenue: int


class InventoryOverviewOut(BaseModel):
    active_sku_count: int
    total_inventory_value: int
    inbound_doc_count_30d: int
    low_stock_threshold: int
    low_stock_items: list[MasterItemOut]
    pending_invoice_count: int
    pending_invoice_outstanding: int
    pending_invoices: list[PendingInvoiceOut]
    latest_incoming: list[IncomingItemOut]
    top_selling: list[TopSellingOut]
    sales_month_revenue: int
    sales_month_count: int


# ---------------------------------------------------------------------------
# Auth / users
# ---------------------------------------------------------------------------


class SetupRequiredOut(BaseModel):
    setup_required: bool


class SetupRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(max_length=72)


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: str


class UserCreateRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=72)
    role: str = "staff"


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=72)
