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
VALID_PAYMENT_METHODS = {"cash", "transfer", "qris", "debit"}


class ReceiptItemRequest(BaseModel):
    product_name: str
    quantity: int
    unit_price: int
    warranty_date: str | None = None
    # Resolved server-side to master_items.id (backend.database._resolve_master_item_id).
    # An unknown/absent SKU is not an error — the line still saves, just unlinked.
    sku: str | None = None
    id: int | None = None  # an existing line, echoed back on edit so its added-by/added-at survive
    item_type: str = "barang"  # "barang" (stock item) or "jasa" (service: never moves stock)
    line_discount: int = 0  # Rp off this line
    # K-22: a price below Master Barang needs a reason; below cost needs an admin to allow it explicitly.
    price_reason: str | None = None
    allow_below_cost: bool = False
    serial_no: str | None = None  # K-S2: SN of a serviceable item, so a later warranty claim can find this line


class ReceiptExtra(BaseModel):
    """Work-order details a nota carries beyond the sale itself. Same on create and edit."""
    work_notes: str | None = None
    cashier_name: str | None = None
    estimated_finish_at: str | None = None
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_color: str | None = None
    vehicle_year: str | None = None


class ReceiptRequest(ReceiptExtra):
    # Admin only: why this sale may use stock reserved for a Sales Order customer (K-02).
    stock_override_reason: str | None = None
    plate_region: str
    plate_number: str
    plate_suffix: str
    # Required unless customer_id is given (then it defaults to that customer's
    # phone) -- the route enforces it, so the old "phone is mandatory" message
    # still comes from one place.
    customer_phone: str | None = None
    customer_name: str | None = None
    # Master customer (Master Pelanggan). When omitted the route links by phone,
    # creating the customer from phone + name if none holds that phone yet.
    customer_id: int | None = None
    items: list[ReceiptItemRequest]
    discount: int = 0
    # Diskon nota is reviewed like Harga Khusus: a reason is required; an admin may allow a final price below cost
    # and may knowingly combine the discount with lines that already have a special price.
    discount_reason: str | None = None
    allow_discount_below_cost: bool = False
    allow_discount_stack: bool = False
    amount_paid: int = 0
    # When the cashier opened this transaction. Server-sanitised on create and
    # never accepted on update -- see _sanitize_started_at / update_receipt.
    started_at: str | None = None
    status: str | None = None  # only "void" is accepted here — pending/done are computed from amount_paid vs total
    payment_method: str = "cash"  # one of VALID_PAYMENT_METHODS
    # K-18: for transfer/QRIS, true = the cashier saw the money arrive (Sah now); otherwise it is recorded Pending.
    payment_confirmed: bool | None = None
    transfer_sender: str | None = None  # required when payment_method is "transfer"
    transfer_account: str | None = None  # the shop's account it was paid into; required for "transfer"
    # Where the nota starts: draft (no stock taken, can't be paid), belum, pengerjaan or selesai.
    work_status: str = "belum"


class ReceiptUpdateRequest(ReceiptExtra):
    # Admin only: why this sale may use stock reserved for a Sales Order customer (K-02).
    stock_override_reason: str | None = None
    # Optimistic locking: the `revision` the client loaded; a mismatch means someone else changed the nota.
    expected_revision: str | None = None
    # Required (by the route) when status='void': why the nota is being cancelled.
    void_reason: str | None = None
    plate_region: str | None = None
    plate_number: str | None = None
    plate_suffix: str | None = None
    # customer_name uses a sentinel default so "omitted" (leave as-is) is distinguishable
    # from "sent as null" (clear it); the route checks fields_set to tell them apart.
    customer_phone: str | None = None
    customer_name: str | None = None
    # null is the same as omitted: a receipt cannot be un-linked, only re-pointed.
    customer_id: int | None = None
    items: list[ReceiptItemRequest] | None = None
    discount: int | None = None
    discount_reason: str | None = None
    allow_discount_below_cost: bool = False
    allow_discount_stack: bool = False
    amount_paid: int | None = None
    status: str | None = None  # only "void" is accepted here — pending/done are computed from amount_paid vs total
    payment_method: str | None = None  # one of VALID_PAYMENT_METHODS
    payment_confirmed: bool | None = None  # K-18, see ReceiptCreateRequest
    # K-20: voiding a paid nota is a full refund; how it was handed back and what proves it.
    refund_method: str | None = None
    refund_proof: str | None = None
    transfer_sender: str | None = None  # name on the sending account; kept only for "transfer"
    transfer_account: str | None = None  # the shop's receiving account; kept only for "transfer"


class ReceiptResponse(BaseModel):
    receipt_id: str
    subtotal: int
    discount: int
    total: int
    amount_paid: int
    status: str
    customer_phone: str
    customer_name: str | None = None
    payment_method: str
    customer_id: int | None = None
    customer_full_name: str | None = None
    receipt_no: str | None = None
    transfer_sender: str | None = None
    transfer_account: str | None = None
    work_status: str = "belum"


class TransferSenderOut(BaseModel):
    id: int
    name: str


class ReceiptItemOut(BaseModel):
    id: int
    product_name: str
    quantity: int
    unit_price: int
    warranty_date: str | None
    serial_no: str | None = None
    sku: str | None = None
    unit: str | None = None  # the linked master item's unit; None for a hand-typed line
    item_type: str = "barang"
    line_discount: int = 0
    added_at: str | None = None
    added_by: str | None = None
    added_during_work: bool = False
    # Set when the line belongs to a Sales Order: it is changed there, not on the nota.
    preorder_item_id: int | None = None
    returned_qty: int = 0  # how many of `quantity` the customer brought back (Retur Barang)
    # K-22 Harga Khusus: the Master Barang price at the time, why it was lowered, who asked and who approved it.
    list_price: int | None = None
    price_state: str | None = None  # pending | approved; null = normal price
    price_reason: str | None = None
    price_requested_by: str | None = None
    price_requested_at: str | None = None
    price_approved_by: str | None = None
    price_approved_at: str | None = None
    price_below_cost: bool = False
    # Today's Master Barang price (to tell a normal price from a special one) and the average cost (admin only:
    # the cost middleware zeroes it for staff).
    master_price: int | None = None
    avg_cost_price: int = 0


class ReceiptOut(ReceiptExtra):
    revision: str | None = None
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
    started_at: str | None = None
    payment_method: str
    item_count: int
    customer_id: int | None = None
    # The linked master customer's name when there is one, else the receipt's own
    # customer_name -- what a listing should show. customer_name/customer_phone
    # stay the snapshot taken when the receipt was saved.
    customer_full_name: str | None = None
    receipt_no: str | None = None  # printed nota number, PJSG/YY/MM/NNNN
    transfer_sender: str | None = None  # for a transfer: the name on the sending account
    transfer_account: str | None = None  # for a transfer: the shop account it was paid into
    work_status: str = "belum"  # draft | menunggu | belum | pengerjaan | selesai
    # Set on the invoice of a Sales Order. K-19: invoice_seq 1 is the main invoice; invoice_count is how many
    # live invoices the order has.
    preorder_id: str | None = None
    po_number: str | None = None
    invoice_seq: int | None = None
    invoice_count: int | None = None
    pending_amount: int = 0  # K-18: transfer/QRIS recorded but not yet confirmed as received
    work_stage: str | None = None  # K-06: persiapan | pemasangan | qc, while Dalam Pengerjaan
    handed_over_at: str | None = None  # K-08: vehicle given back to the customer
    handed_over_by: str | None = None
    handover_note: str | None = None
    handover_debt: int = 0  # K-09: balance still owed when it left (needs admin)
    handover_receiver: str | None = None  # who took the vehicle
    # Diskon nota review: pending (waiting for an admin) or approved; the amount a rejection goes back to.
    discount_state: str | None = None
    discount_reason: str | None = None
    discount_requested_by: str | None = None
    discount_requested_at: str | None = None
    discount_approved_by: str | None = None
    discount_approved_at: str | None = None
    discount_prev: int = 0
    discount_below_cost: int = 0
    discount_stacked: int = 0
    closed_at: str | None = None
    # Set on a Nota Jasa Service (the fee of a Service / Garansi document).
    service_order_id: int | None = None
    service_no: str | None = None
    return_total: int = 0  # what Retur Barang took off the total
    void_reason: str | None = None
    voided_by: str | None = None
    voided_at: str | None = None
    draft_saved_at: str | None = None


class ReceiptEventOut(BaseModel):
    id: int
    at: str
    by_user: str | None = None
    kind: str
    note: str | None = None


class ReceiptWorkStageRequest(BaseModel):
    stage: str  # persiapan | pemasangan | qc


class ReceiptHandoverRequest(BaseModel):
    note: str | None = None
    receiver_name: str | None = None  # who took the vehicle
    signature: str | None = None  # the receiver's signature as a PNG data URL
    photos: list[str] = []  # up to 4 photos of the vehicle as image data URLs
    reason: str | None = None  # required (admin only) when the nota still has a balance


class ReceiptWorkStatusRequest(BaseModel):
    to: str  # belum | pengerjaan | selesai
    note: str | None = None
    # Admin only: why this sale may use stock reserved for a Sales Order customer (K-02).
    stock_override_reason: str | None = None


class ReceiptPaymentOut(BaseModel):
    id: int
    paid_at: str
    amount: int  # negative only for an "adjustment" row written when an edit lowers the paid amount
    method: str
    transfer_sender: str | None = None
    transfer_account: str | None = None
    note: str | None = None
    created_by: str | None = None
    reversed_of: int | None = None  # set on the negative row that cancels payment #reversed_of
    reversed: bool = False  # true on an original payment that has been cancelled
    state: str = "sah"  # K-18: sah (counted) | pending (transfer/QRIS not yet seen in the account) | gagal
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    fail_reason: str | None = None
    kind: str | None = None  # pindah (K-19 DP move) | refund (K-20 money handed back)
    proof_ref: str | None = None  # K-20: transfer reference, or the name of the person who received cash


class ReceiptRefundRequest(BaseModel):
    amount: int
    method: str = "cash"
    reason: str
    proof_ref: str | None = None  # K-20: required; transfer reference, or who received the cash


class ReceiptReturnRequest(BaseModel):
    item_id: int
    quantity: int
    destination: str | None = None  # stok | supplier | rusak, for all units; or use the split below
    refund_amount: int = 0  # at most what those units were sold for; less keeps the rest as a fee
    method: str = "cash"  # how money is handed back, when any is
    reason: str = ""
    kind: str = "retur"  # retur | void_item (a mistaken line: full refund, all stock back)
    reason_code: str | None = None
    restock_qty: int | None = None  # split of `quantity`; must add up to it
    supplier_qty: int | None = None
    damaged_qty: int | None = None
    proof_ref: str | None = None  # K-20: required when money goes back to the customer


class ReceiptReturnOut(BaseModel):
    id: int
    receipt_item_id: int
    product_name: str
    quantity: int
    item_value: int
    refund_amount: int
    cash_out: int
    method: str | None = None
    destination: str
    reason: str
    returned_by: str | None = None
    returned_at: str
    kind: str = "retur"
    reason_code: str | None = None
    restock_qty: int | None = None
    supplier_qty: int | None = None
    damaged_qty: int | None = None


class ReceiptReopenRequest(BaseModel):
    reason: str


class ReceiptPaymentReverseRequest(BaseModel):
    reason: str


class ReceiptPaymentRequest(BaseModel):
    amount: int
    method: str = "cash"  # one of VALID_PAYMENT_METHODS
    note: str | None = None
    transfer_sender: str | None = None  # required when method is "transfer"
    transfer_account: str | None = None  # the shop's account it was paid into; required for "transfer"
    confirmed: bool | None = None  # K-18: transfer/QRIS only; true = money already seen in the account


class ReceiptDetail(ReceiptOut):
    items: list[ReceiptItemOut]
    payments: list[ReceiptPaymentOut] = []


class ReceiptSettleRequest(BaseModel):
    # Optional: how the remaining balance was paid. Omitted keeps the receipt's own.
    payment_method: str | None = None  # one of VALID_PAYMENT_METHODS
    # What the customer paid now. Must be exactly the remaining balance (total - amount_paid).
    amount: int
    confirmed: bool | None = None  # K-18, see ReceiptPaymentRequest


class CustomerRequest(BaseModel):
    first_name: str
    last_name: str | None = None
    phone: str
    email: str | None = None
    birth_date: str | None = None  # YYYY-MM-DD
    address: str | None = None


class CustomerUpdateRequest(BaseModel):
    # Only fields the client sent are applied (the route reads model_fields_set),
    # so for the nullable ones an omitted field is left alone and an explicit
    # null/blank clears it.
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: str | None = None
    birth_date: str | None = None
    address: str | None = None
    is_active: bool | None = None


class CustomerOut(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    full_name: str
    phone: str
    email: str | None = None
    birth_date: str | None = None
    address: str | None = None
    is_active: bool
    created_at: str
    updated_at: str
    # Receipts linked to this customer, of any status (void included), and the
    # created_at of the newest one (UTC ISO string, null when there are none).
    receipt_count: int = 0
    last_visit: str | None = None


class CustomerListOut(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    page_size: int


class CustomerAutocompleteOut(BaseModel):
    id: int
    full_name: str
    phone: str


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
    email: str | None = None


class SupplierUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    phone: str | None = None
    address: str | None = None
    contact_person: str | None = None
    npwp: str | None = None
    email: str | None = None


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
    email: str | None = None
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
    description: str | None = None  # "Pieces" for PCS; shown next to the name in dropdowns


class UnitUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    reason: str | None = None  # optional note when deactivating
    confirm_rename: bool = False  # required when renaming a unit that products already use


class UnitOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    is_active: bool
    created_at: str
    deactivated_reason: str | None = None
    product_count: int = 0
    active_product_count: int = 0
    in_use: bool = False


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
    # Not sellable, kept apart (Stok Karantina); physical stock = stock_qty + quarantine_qty.
    quarantine_qty: int = 0
    # K-02: part of stock_qty held for Sales Order customers, and what is left to sell (stock − reserved).
    reserved_qty: int = 0
    available_qty: int | None = None
    first_received_date: str | None
    is_active: bool
    created_at: str
    # In-store CODE128 barcode (10-digit numeric string), generated from the row
    # id. Null only for the brief instant between insert and backfill inside the
    # same transaction that creates it.
    barcode: str | None = None
    # Optional product photo. Null when none has been uploaded; otherwise a
    # same-origin URL whose ?v= token changes on every upload, so an <img> never
    # shows a stale picture after a replace. Built from master_items.image_path,
    # which is deliberately not exposed.
    image_url: str | None = None
    description: str | None = None
    min_stock: int = 5
    location: str | None = None
    archived_reason: str | None = None
    archived_note: str | None = None
    archived_at: str | None = None
    archive_mode: str | None = None
    updated_at: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    # Set when the item's category is a subcategory: the main category above it.
    category_parent_id: int | None = None
    category_parent_name: str | None = None
    updated_by: str | None = None


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
    # Opening stock counted at entry time; recorded in the stock ledger.
    stock_qty: int = 0
    description: str | None = None
    min_stock: int = 5
    location: str | None = None
    category_id: int | None = None
    is_active: bool = True


class MasterItemUpdateRequest(BaseModel):
    expected_updated_at: str | None = None
    supplier_id: int | None = None
    name: str | None = None
    sell_price: int | None = None
    unit: str | None = None
    description: str | None = None
    min_stock: int | None = None
    location: str | None = None
    price_note: str | None = None
    # Send null to clear the category; leave the field out to keep it.
    category_id: int | None = None


class MasterItemArchiveRequest(BaseModel):
    reason: str
    note: str | None = None
    mode: str = "nonaktif"  # nonaktif | arsip


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
    spo_item_id: int | None = None  # the Purchase Order line this quantity counts against
    spo_package_id: int | None = None  # the Paket Pengiriman it came in (required once the line is split into packages)
    condition: str | None = None  # baik | rusak | tidak_sesuai
    location: str | None = None


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
    spo_item_id: int | None = None
    spo_package_id: int | None = None
    condition: str | None = None
    location: str | None = None


class InboundDocumentCreateRequest(BaseModel):
    supplier_id: int
    received_date: str
    supplier_invoice_no: str | None = None
    notes: str | None = None
    items: list[InboundItemRequest]
    supplier_po_id: str | None = None  # set when the goods come against a Purchase Order
    surat_jalan_no: str | None = None
    officer: str | None = None
    source: str | None = None  # po_supplier | manual
    manual_reason: str | None = None  # required for source=manual


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
    supplier_po_id: str | None = None
    supplier_po_number: str | None = None
    surat_jalan_no: str | None = None
    officer: str | None = None
    source: str | None = None
    manual_reason: str | None = None
    created_by: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    posted_by: str | None = None
    posted_at: str | None = None
    item_count: int = 0
    qty_total: int = 0


class InboundDocumentDetail(InboundDocumentOut):
    items: list[InboundItemOut]
    events: list[dict] = []
    po: dict | None = None
    fulfilment: list[dict] = []
    customers: list[dict] = []
    mutations: list[dict] = []
    invoice: dict | None = None


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
    # Shipping as on the supplier's faktur: cost, the DPP it is taxed on (0 = no PPN), PPN amount (None = DPP × rate).
    shipping_cost: int = 0
    shipping_dpp: int = 0
    shipping_tax: int | None = None
    items: list[PurchaseInvoiceItemRequest]


class PurchaseInvoicePaymentRequest(BaseModel):
    amount: int
    method: str | None = None
    paid_date: str | None = None
    reference_no: str | None = None
    note: str | None = None
    source_account: str | None = None  # the shop's account the money left from
    dest_account: str | None = None  # the supplier's account it went to


class PurchaseInvoicePaymentOut(BaseModel):
    id: int
    paid_at: str
    amount: int
    kind: str  # bayar | batal | dp | dp_batal | kredit
    method: str | None = None
    reference_no: str | None = None
    note: str | None = None
    created_by: str | None = None
    reverses_id: int | None = None
    spo_payment_id: int | None = None
    source_account: str | None = None
    dest_account: str | None = None
    proof_name: str | None = None
    proof_status: str | None = None  # None = no proof yet | tersimpan | terverifikasi
    proof_by: str | None = None
    proof_at: str | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    kk_number: str | None = None
    has_proof: bool = False


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
    shipping_cost: int = 0
    shipping_dpp: int = 0
    shipping_tax: int = 0
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
    supplier_po_id: str | None = None
    supplier_po_number: str | None = None
    verify_status: str = "belum"
    verified_by: str | None = None
    verified_at: str | None = None
    verify_note: str | None = None
    exception_reason: str | None = None
    hold_note: str | None = None


class PurchaseInvoiceDetail(PurchaseInvoiceOut):
    items: list[PurchaseInvoiceItemOut]
    payments: list[PurchaseInvoicePaymentOut] = []


class InvoiceVerifyRequest(BaseModel):
    checklist: dict[str, bool] = {}
    note: str | None = None


class InvoiceHoldRequest(BaseModel):
    note: str
    ask_admin: bool = False


class InvoiceExceptionRequest(BaseModel):
    reason: str


class DpAllocateRequest(BaseModel):
    spo_payment_id: int
    amount: int


class DpReverseRequest(BaseModel):
    reason: str


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
    created_by: str | None = None
    ref_doc_number: str | None = None
    # Who did it (the cashier for a sale) and the nota id behind a receipt movement, for the
    # activity timeline's name and link.
    actor: str | None = None
    ref_receipt_id: str | None = None
    # The Purchase Order behind an inbound document that came from a Penerimaan Barang.
    supplier_po_number: str | None = None
    supplier_po_id: str | None = None


class PendingInvoiceOut(BaseModel):
    id: str
    plate_full: str
    customer_name: str | None
    total: int
    amount_paid: int
    outstanding: int
    created_at: str


class ReadyHandoverOut(BaseModel):
    id: str
    receipt_no: str | None = None
    plate_full: str
    customer_name: str | None = None
    total: int
    amount_paid: int
    outstanding: int
    created_at: str


class OpenPreorderOut(BaseModel):
    """Slim preorder row for the dashboard; `status` is the order-level stage
    derived from its line items."""
    id: str
    customer_name: str
    customer_phone: str
    item_count: int
    total: int
    deposit: int
    remaining: int
    status: str
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
    ready_handover: list[ReadyHandoverOut] = []  # K-08: Pengerjaan Selesai, not yet handed over
    latest_incoming: list[IncomingItemOut]
    top_selling: list[TopSellingOut]
    sales_month_revenue: int  # K-21 Penerimaan Kas: Sah payments minus refunds this month
    sales_month_count: int
    sales_closed_month_revenue: int = 0  # K-21 Penjualan: total of notas that closed (Lunas + Selesai) this month
    sales_closed_month_count: int = 0
    preorder_open_count: int
    preorder_outstanding: int
    preorder_ready_count: int
    preorders: list[OpenPreorderOut]


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
    # Owner: an admin who gives the final approval for losses (quarantine write-off).
    is_owner: bool = False
    can_post_inbound: bool = False  # K-22: may post Barang Masuk


class UserCreateRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=72)
    role: str = "staff"


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    is_owner: bool | None = None
    can_post_inbound: bool | None = None


class QuarantineQtyRequest(BaseModel):
    qty: int
    reason: str


class QuarantineFromStockRequest(BaseModel):
    master_item_id: int
    qty: int
    condition: str = "rusak"  # rusak | tidak_sesuai | perlu_cek
    note: str


class QuarantineDecisionRequest(BaseModel):
    approve: bool
    note: str | None = None


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=72)


# ---------------------------------------------------------------------------
# Preorders
# ---------------------------------------------------------------------------
# The status values themselves live in backend.database (PREORDER_STATUSES) so
# the storage layer and the route validation cannot drift apart.


class PreorderItemRequest(BaseModel):
    # Present when editing an existing line; omitted for a newly added one. The
    # id is what preserves that line's stage and its status history across an edit.
    id: int | None = None
    master_item_id: int | None = None
    product_name: str
    unit: str | None = None
    quantity: int
    unit_price: int
    # K-22: a price below Master Barang needs a reason; below cost needs an admin to allow it explicitly.
    price_reason: str | None = None
    allow_below_cost: bool = False
    status: str | None = None
    ship_route: str | None = None  # udara|laut|darat|ambil_sendiri|lainnya
    priority: str = "normal"  # normal|urgent
    eta_date: str | None = None
    # Where the goods come from: supplier (bought for this order) | stok (from the shelf) | jasa
    # (a service, no goods). Null = decided automatically (stock first, the rest bought).
    source: str | None = None
    # The supplier to buy from, for source 'supplier'. Empty means it still has to be chosen.
    supplier_id: int | None = None


class PreorderRequest(BaseModel):
    customer_name: str
    customer_phone: str
    # Free-text car plate (e.g. "B 1234 ABC"); required on a new preorder.
    plate_full: str
    # YYYY-MM-DD; omitted means today on the shop's clock.
    po_date: str | None = None
    items: list[PreorderItemRequest]
    # Saved parked ("Simpan Draft") instead of onto the live list.
    is_draft: bool = False
    # A deposit given up front becomes the first payment ("DP awal"). Further
    # payments go through POST /preorders/{id}/payments.
    deposit: int = 0
    deposit_method: str = "cash"  # cash | transfer | qris | debit
    deposit_confirmed: bool | None = None  # K-18: transfer/QRIS DP already seen in the account
    notes: str | None = None
    # Buy every barang of this order from this supplier; null = each follows its Master Barang supplier.
    supplier_id: int | None = None


class PreorderUpdateRequest(BaseModel):
    expected_updated_at: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    plate_full: str | None = None
    po_date: str | None = None
    items: list[PreorderItemRequest] | None = None
    # Nullable, so "omitted" (leave as-is) and "sent as null" (clear it) are told
    # apart via fields_set in the route, same as ReceiptUpdateRequest.customer_name.
    notes: str | None = None
    # No null state to distinguish here -- a plain bool | None (omitted = leave as-is) is enough.
    is_draft: bool | None = None
    # Omitted = keep; null = clear (follow each barang's supplier).
    supplier_id: int | None = None


class PreorderItemStatusRequest(BaseModel):
    status: str


class PreorderStatusRequest(BaseModel):
    """Moves every line of the order to this stage ("Ubah Status PO")."""
    status: str


class PreorderPaymentRequest(BaseModel):
    amount: int
    note: str | None = None
    method: str = "cash"  # cash | transfer | qris | debit
    # A PO with an invoice takes the payment there, where a transfer needs both names.
    transfer_sender: str | None = None
    transfer_account: str | None = None
    # K-19: which invoice takes it when the order has several.
    receipt_id: str | None = None
    confirmed: bool | None = None  # K-18: transfer/QRIS already seen in the account


class PaymentFailRequest(BaseModel):
    reason: str


class PreorderPaymentOut(BaseModel):
    id: int
    paid_at: str
    amount: int
    note: str | None = None
    created_by: str | None = None
    method: str = "cash"
    state: str = "sah"
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    fail_reason: str | None = None
    receipt_id: str | None = None  # set when the payment sits on an invoice (confirm/fail go through the nota)
    proof_ref: str | None = None
    receipt_no: str | None = None
    kind: str | None = None


class PreorderInvoiceOut(BaseModel):
    id: str
    receipt_no: str | None = None
    invoice_seq: int = 1
    total: int = 0
    amount_paid: int = 0
    status: str = "pending"
    work_status: str = "belum"
    split_reason: str | None = None
    pending_amount: int = 0
    item_ids: list[int] = []


class PreorderSplitRequest(BaseModel):
    item_ids: list[int]
    reason: str
    dp_move: int | None = None  # None = the old invoice's paid money beyond its new total follows the lines


class PreorderReasonRequest(BaseModel):
    reason: str


class PreorderAllocationOut(BaseModel):
    id: int
    receipt_id: str
    receipt_no: str | None = None
    amount: int
    allocated_at: str
    allocated_by: str | None = None


class PreorderConvertRequest(BaseModel):
    # Omit to take every line that has arrived and is not on a nota yet.
    item_ids: list[int] | None = None
    # Omit to use the plate saved on the preorder.
    plate_region: str | None = None
    plate_number: str | None = None
    plate_suffix: str | None = None


class PreorderConvertResponse(BaseModel):
    receipt_id: str
    receipt_no: str | None = None
    total: int
    dp_applied: int
    amount_due: int


class PreorderItemSupplierPo(BaseModel):
    supplier_po_id: str
    po_number: str
    po_status: str
    qty_ordered: int
    qty_received: int
    packages: list[dict] = []  # Paket Pengiriman carrying this line (package_no, forwarder, route, state, eta, qty)


class PriceDecisionRequest(BaseModel):
    note: str | None = None


class PreorderItemOut(BaseModel):
    # K-22 Harga Khusus: the Master Barang price at the time, why it was lowered, who asked and who approved it.
    list_price: int | None = None
    price_state: str | None = None  # pending | approved; null = normal price
    price_reason: str | None = None
    price_requested_by: str | None = None
    price_requested_at: str | None = None
    price_approved_by: str | None = None
    price_approved_at: str | None = None
    price_below_cost: bool = False
    # Today's Master Barang price (to tell a normal price from a special one) and the average cost (admin only:
    # the cost middleware zeroes it for staff).
    master_price: int | None = None
    avg_cost_price: int = 0
    supplier_pos: list[PreorderItemSupplierPo] = []
    id: int
    master_item_id: int | None = None
    # SKU of the linked master item, null for a free-text line.
    sku: str | None = None
    product_name: str
    unit: str | None = None
    quantity: int
    unit_price: int
    status: str
    # Set once the line is on a nota (Sales Order -> nota).
    receipt_id: str | None = None
    ship_route: str | None = None
    priority: str = "normal"
    eta_date: str | None = None
    source: str | None = None  # supplier | stok | jasa | null (automatic)
    supplier_id: int | None = None
    supplier_name: str | None = None
    # K-02: held for this line on the shelf, still coming from a supplier, and needed but not covered.
    reserved_qty: int = 0
    coming_qty: int = 0
    shortfall_qty: int = 0
    # K-13: set when the line was cancelled (status 'dibatalkan').
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    cancel_kind: str | None = None
    cancel_reason: str | None = None


class PreorderHistoryOut(BaseModel):
    id: int
    preorder_item_id: int
    product_name: str
    status: str
    # Null for a line's first entry, when it had no previous stage.
    old_status: str | None = None
    changed_at: str
    changed_by: str | None = None


class PreorderOut(BaseModel):
    id: str
    po_number: str
    po_date: str
    supplier_id: int | None = None
    supplier_name: str | None = None
    # Empty for orders created before the plate was captured.
    plate_full: str
    customer_name: str
    customer_phone: str
    # Sum of the order's payments.
    deposit: int
    notes: str | None = None
    total: int
    remaining: int
    # Derived from the line items: the least advanced stage among them.
    status: str
    item_count: int
    item_names: list[str] = []
    created_at: str
    # Stored, not computed -- a parked/archived preorder, independent of `status`.
    is_draft: bool
    updated_at: str
    # The PO's invoice (every published PO has one). Payments are taken there; deposit/remaining above
    # are the invoice's once it exists.
    receipt_id: str | None = None
    receipt_no: str | None = None
    invoice_total: int | None = None
    invoice_status: str | None = None  # pending | done | void
    invoice_work_status: str | None = None
    # K-13: when the order was confirmed (starts the 24-hour cancellation window) and its cancellation.
    published_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    cancel_kind: str | None = None  # pelanggan | gagal_memenuhi | barang_salah | pelanggaran_pengiriman
    cancel_reason: str | None = None
    # Derived for the list: draft | menunggu | dipesan | tiba_sebagian | siap | selesai | dibatalkan
    procurement_status: str = "menunggu"
    payment_status: str = "belum"  # belum | dp | lunas
    # 'Pesan Supplier' lines that still have no supplier, and the suppliers chosen on the lines.
    missing_supplier_count: int = 0
    line_suppliers: list[str] = []
    invoice_count: int = 1
    pending_amount: int = 0  # K-18: transfer/QRIS recorded but not yet confirmed


class PreorderDetail(PreorderOut):
    items: list[PreorderItemOut]
    history: list[PreorderHistoryOut]
    payments: list[PreorderPaymentOut]
    allocations: list[PreorderAllocationOut] = []
    # DP still free to apply to a nota.
    dp_unallocated: int = 0
    # K-13: {allowed, reasons[], deadline, special_allowed} for Batalkan Sales Order / Pembatalan Khusus.
    cancel_check: dict | None = None
    # K-19: the order's invoices (main first); one entry in the normal flow.
    invoices: list[PreorderInvoiceOut] = []


class PreorderCancelRequest(BaseModel):
    reason: str
    refund_amount: int = 0
    refund_method: str = "cash"
    refund_proof: str | None = None  # K-20: required when refund_amount > 0
    # None = every line the policy allows; else just these lines.
    item_ids: list[int] | None = None


class PreorderSpecialCancelRequest(BaseModel):
    item_ids: list[int]
    category: str
    reason: str
    refund_method: str = "cash"
    refund_proof: str | None = None  # K-20: required when money goes back


# ---------------------------------------------------------------------------
# Purchase Order (purchase orders to a supplier, built from customer preorder lines)
# ---------------------------------------------------------------------------


class SupplierPoItemRequest(BaseModel):
    # Set when the line comes from a customer preorder ("Ambil Barang dari Sales Order");
    # omitted for a barang added by hand.
    preorder_item_id: int | None = None
    master_item_id: int | None = None
    sku: str | None = None  # looked up in Master Barang when master_item_id is not given
    product_name: str
    unit: str | None = None
    quantity: int
    est_unit_price: int = 0
    discount_pct: float = 0  # this line's discount, in percent
    # Chosen while picking from the customer PO: how and through whom this line travels.
    ship_method: str | None = None  # udara | laut | darat | ambil_sendiri | lainnya
    courier: str | None = None  # ekspedisi / forwarder
    eta_date: str | None = None
    item_note: str | None = None


class SupplierPoInvoiceFields(BaseModel):
    """The purchase-invoice side of a PO. Nullable: omitted (leave as-is) and null (clear)
    are told apart through fields_set where it matters."""
    supplier_invoice_no: str | None = None
    payment_terms_days: int | None = None  # NET 14 -> 14
    due_date: str | None = None  # YYYY-MM-DD; filled from po_date + termin when left empty
    est_arrival_date: str | None = None
    discount_pct: float | None = None
    shipping_cost: int | None = None
    tax_rate: float | None = None  # PPN in percent, e.g. 11


class SupplierPoRequest(SupplierPoInvoiceFields):
    supplier_id: int
    # YYYY-MM-DD; omitted means today on the shop's clock.
    po_date: str | None = None
    notes: str | None = None
    items: list[SupplierPoItemRequest]
    # False saves a draft ("Simpan Draft"); True issues it at once ("Simpan PO").
    issue: bool = False
    # "Sudah dibayar" typed on the form; only valid together with issue.
    paid_amount: int = 0


class SupplierPoUpdateRequest(SupplierPoInvoiceFields):
    expected_updated_at: str | None = None
    supplier_id: int | None = None
    po_date: str | None = None
    items: list[SupplierPoItemRequest] | None = None
    # Nullable, so omitted (leave as-is) and null (clear it) are told apart via fields_set.
    notes: str | None = None


class SupplierPoReceiveLine(BaseModel):
    item_id: int
    quantity: int
    package_id: int | None = None  # which Paket Pengiriman these goods came in
    condition: str | None = None  # baik | rusak | tidak_sesuai
    location: str | None = None   # lokasi simpan


class SupplierPoReceiveRequest(BaseModel):
    items: list[SupplierPoReceiveLine]
    received_date: str | None = None
    surat_jalan_no: str | None = None
    warehouse_location: str | None = None
    note: str | None = None
    officer: str | None = None
    # Also open a draft Barang Masuk for the lines linked to a master item.
    create_inbound: bool = False
    # K-11 (admin only): accept goods beyond what the PO still expects as general stock, with a reason.
    accept_over: bool = False
    over_reason: str | None = None


class InboundPostRequest(BaseModel):
    # K-11 (admin only): post lines that exceed the PO's remaining quantity; the excess is general stock.
    accept_over: bool = False
    over_reason: str | None = None


class SupplierPoStageRequest(BaseModel):
    to: str  # menunggu_konfirmasi | dikonfirmasi | diproses | dalam_pengiriman
    note: str


class SupplierPoTrackingLine(BaseModel):
    item_id: int
    tracking_no: str | None = None
    courier: str | None = None
    eta_date: str | None = None


class SupplierPoTrackingRequest(BaseModel):
    items: list[SupplierPoTrackingLine]


class SupplierPoPaymentRequest(BaseModel):
    amount: int
    method: str = "transfer"
    note: str | None = None
    paid_date: str | None = None
    reference_no: str | None = None
    bank_account: str | None = None


class SupplierPoItemShippingRequest(BaseModel):
    item_status: str  # menunggu_konfirmasi | dikonfirmasi | diproses | dalam_pengiriman | tiba_di_hub
    ship_method: str | None = None  # udara | laut | darat | ambil_sendiri | lainnya
    forwarder: str | None = None
    tracking_no: str | None = None
    shipped_date: str | None = None
    eta_date: str | None = None
    transit_location: str | None = None
    note: str | None = None
    notify_customer: bool = False


class SpoPackageLineRequest(BaseModel):
    item_id: int
    quantity: int


class SpoPackageRequest(BaseModel):
    forwarder_id: int | None = None  # from Master Ekspedisi
    ship_method: str | None = None  # udara | laut | darat | ambil_sendiri | lainnya
    tracking_no: str | None = None
    shipped_date: str | None = None
    eta_date: str | None = None
    transit_location: str | None = None
    status: str = "menunggu_konfirmasi"
    note: str | None = None
    notify_customer: bool = False
    lines: list[SpoPackageLineRequest] | None = None


class SpoPackageCancelRequest(BaseModel):
    reason: str


class ForwarderRequest(BaseModel):
    name: str
    phone: str | None = None
    contact: str | None = None
    note: str | None = None
    is_active: bool = True


class SupplierPoReleaseRequest(BaseModel):
    reason: str


class SupplierPoCancelItemRequest(BaseModel):
    quantity: int
    reason: str
    action: str  # kembali_ke_customer | simpan_stok_toko | retur_supplier | tidak_ada
    note: str | None = None


class SupplierPoItemOut(BaseModel):
    id: int
    preorder_item_id: int | None = None
    master_item_id: int | None = None
    sku: str | None = None
    product_name: str
    unit: str | None = None
    quantity: int
    est_unit_price: int
    discount_pct: float = 0
    qty_received: int
    qty_cancelled: int = 0
    tracking_no: str | None = None
    courier: str | None = None
    eta_date: str | None = None
    ship_method: str | None = None
    item_status: str | None = None
    shipped_date: str | None = None
    transit_location: str | None = None
    # Paket Pengiriman: how much of the line sits in packages still on the road, how much of that is
    # already moving, and how much is not in any package yet.
    has_packages: bool = False
    pkg_open: int = 0
    pkg_moving: int = 0
    pkg_free: int = 0
    # Which customer order the line serves; null for a barang added by hand.
    customer_po_number: str | None = None
    customer_name: str | None = None
    plate_full: str | None = None
    # From the linked Master Barang: the shelf price and product code printed on the stock label.
    sell_price: int | None = None
    barcode: str | None = None


class SupplierPoEventOut(BaseModel):
    id: int
    at: str
    by_user: str | None = None
    kind: str
    note: str | None = None


class SupplierPoPaymentOut(BaseModel):
    id: int
    paid_at: str
    amount: int
    method: str
    note: str | None = None
    created_by: str | None = None
    reference_no: str | None = None
    bank_account: str | None = None


class SupplierPoReceiptLineOut(BaseModel):
    item_id: int
    product_name: str
    quantity: int
    package_id: int | None = None
    condition: str = "baik"
    location: str | None = None


class SupplierPoShipmentOut(BaseModel):
    id: int
    item_id: int
    at: str
    by_user: str | None = None
    status: str
    ship_method: str | None = None
    forwarder: str | None = None
    tracking_no: str | None = None
    shipped_date: str | None = None
    eta_date: str | None = None
    transit_location: str | None = None
    note: str | None = None
    notify_customer: int = 0


class SupplierPoReceiptOut(BaseModel):
    id: int
    received_date: str
    surat_jalan_no: str | None = None
    warehouse_location: str | None = None
    note: str | None = None
    inbound_document_id: int | None = None
    created_by: str | None = None
    created_at: str
    gr_number: str | None = None
    officer: str | None = None
    lines: list[SupplierPoReceiptLineOut]


class SupplierPoCancellationOut(BaseModel):
    id: int
    item_id: int
    product_name: str
    quantity: int
    reason: str
    action: str
    note: str | None = None
    cancelled_by: str | None = None
    cancelled_at: str
    # menunggu_persetujuan (asked, supplier has not answered) | disetujui | ditolak
    status: str = "disetujui"
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolution_note: str | None = None


class SupplierPoResolveCancelRequest(BaseModel):
    approved: bool
    note: str | None = None
    refund_kind: str | None = None  # refund | kredit — needed when money already paid exceeds what the PO bills after the cancellation


class SupplierPoCancelAllRequest(BaseModel):
    reason: str
    action: str = "tidak_ada"
    note: str | None = None


class RefundClaimSettleRequest(BaseModel):
    amount: int
    received_at: str | None = None
    method: str | None = None  # refund: transfer | cash | giro
    reference_no: str | None = None
    supplier_cn_no: str | None = None  # kredit: the supplier's credit note number
    note: str | None = None


class RefundClaimCancelRequest(BaseModel):
    reason: str


class SupplierPoOut(BaseModel):
    id: str
    po_number: str
    po_date: str
    supplier_id: int
    supplier_name_snapshot: str
    notes: str | None = None
    status: str
    # What the list shows: draft, the four stages while the PO is out, diterima_sebagian,
    # diterima_lengkap or dibatalkan; and the payment state (null for a draft/cancelled PO).
    order_status: str
    payment_status: str | None = None
    shipping_status: str | None = None  # belum_dikirim | diproses | dalam_pengiriman | tiba (null for draft/cancelled)
    receipt_status: str | None = None  # belum | sebagian | lengkap
    supplier_invoice_no: str | None = None
    internal_invoice_no: str | None = None
    payment_terms_days: int | None = None
    due_date: str | None = None
    est_arrival_date: str | None = None
    discount_pct: float = 0
    shipping_cost: int = 0
    tax_rate: float = 0
    paid_amount: int = 0
    created_by: str | None = None
    created_at: str
    updated_at: str
    item_count: int
    total_estimate: int
    qty_total: int
    qty_received: int
    subtotal: int
    discount_amount: int
    tax_amount: int
    grand_total: int
    outstanding: int
    # K-12: money paid on the PO before its faktur is supplier DP; the debt is the faktur's.
    dp_paid: int = 0
    dp_unapplied: int = 0
    faktur_id: int | None = None
    faktur_number: str | None = None
    faktur_status: str | None = None
    faktur_total: int | None = None
    faktur_paid: int | None = None
    faktur_outstanding: int | None = None
    # List columns: the customer POs this order serves and the names of its goods.
    customer_refs: list[dict] = []
    item_names: list[str] = []


class SupplierPoReceiveDraft(BaseModel):
    """Whatever the receiving form holds; opaque to the server beyond a size cap."""
    form: dict
    lines: list[dict] = []


class SupplierPoDetail(SupplierPoOut):
    items: list[SupplierPoItemOut]
    events: list[SupplierPoEventOut] = []
    payments: list[SupplierPoPaymentOut] = []
    receipts: list[SupplierPoReceiptOut] = []
    cancellations: list[SupplierPoCancellationOut] = []
    shipments: list[SupplierPoShipmentOut] = []
    packages: list[dict] = []
    package_history: list[dict] = []
    # Piutang Refund / Kredit Supplier after a paid PO was cancelled (admin only) and the supplier's open fakturs to put credit on.
    refund_claims: list[dict] = []
    open_invoices: list[dict] = []
    receive_draft: dict | None = None
    # K-13: lines still coming whose customer cancelled (Pembatalan Khusus) — the admin must act.
    so_cancelled_lines: list[dict] = []


class SupplierPoSummary(BaseModel):
    order_aktif: int
    menunggu_konfirmasi: int
    dalam_pengiriman: int
    diterima_sebagian: int
    faktur_belum_lunas: int
    total_utang: int
    dp_supplier: int = 0


class SupplierPoCandidateOut(BaseModel):
    """One customer preorder line that still has quantity nobody has ordered."""
    preorder_item_id: int
    preorder_id: str
    customer_po_number: str
    customer_name: str
    plate_full: str
    product_name: str
    unit: str | None = None
    master_item_id: int | None = None
    qty_ordered_by_customer: int
    qty_bought: int
    qty_left: int
    ship_route: str | None = None
    priority: str = "normal"
    eta_date: str | None = None
    # From the linked Master Barang; null for a free-text line.
    supplier_id: int | None = None
    supplier_name: str | None = None
    est_unit_price: int


class SupplierPoCandidateCounts(BaseModel):
    belum: int
    sebagian: int
    semua: int


class SupplierPoCandidatePage(BaseModel):
    rows: list[SupplierPoCandidateOut]
    total: int
    counts: SupplierPoCandidateCounts


# ---------------------------------------------------------------------------
# Master Kategori
# ---------------------------------------------------------------------------


class CategoryCreateRequest(BaseModel):
    name: str
    code: str | None = None  # blank = generated
    parent_id: int | None = None
    status: str = "aktif"
    icon: str | None = None
    description: str | None = None


class CategoryUpdateRequest(BaseModel):
    expected_updated_at: str | None = None
    name: str | None = None
    parent_id: int | None = None  # send null to make it a main category; omit to keep
    status: str | None = None
    icon: str | None = None
    description: str | None = None


class CategoryOut(BaseModel):
    id: int
    code: str
    name: str
    parent_id: int | None = None
    parent_name: str | None = None
    parent_code: str | None = None
    status: str
    icon: str | None = None
    description: str | None = None
    created_by: str | None = None
    created_at: str
    updated_by: str | None = None
    updated_at: str
    product_count: int = 0
    own_product_count: int = 0
    active_product_count: int = 0
    out_of_stock_count: int = 0
    child_count: int = 0


class CategoryDetailOut(CategoryOut):
    subcategories: list[CategoryOut] = []


class CategoryItemOut(BaseModel):
    id: int
    sku: str
    name: str
    stock_qty: int
    min_stock: int
    sell_price: int
    is_active: bool
    category_name: str


class CategoryItemsOut(BaseModel):
    total: int
    items: list[CategoryItemOut]


class CategoryImportRequest(BaseModel):
    csv: str


# ---------------------------------------------------------------------------
# Stock Opname
# ---------------------------------------------------------------------------


class OpnameItemRequest(BaseModel):
    master_item_id: int
    counted_qty: int | None = None
    reason: str | None = None
    note: str | None = None


class OpnameRequest(BaseModel):
    officer: str
    opname_at: str | None = None
    location: str | None = None
    note: str | None = None
    items: list[OpnameItemRequest]
    submit: bool = False


class OpnameResolveRequest(BaseModel):
    note: str | None = None
    accept_moved: bool = False


class OpnameActivityOut(BaseModel):
    kasir_total: int = 0
    kasir_terbuka: int = 0
    po_customer_aktif: int = 0
    po_supplier_aktif: int = 0


class OpnameItemOut(BaseModel):
    id: int
    master_item_id: int
    sku: str
    name: str
    unit: str | None = None
    category_name: str | None = None
    item_location: str | None = None
    system_qty: int
    current_qty: int
    counted_qty: int | None = None
    diff_qty: int | None = None
    reason: str | None = None
    note: str | None = None
    applied_delta: int | None = None
    activity: OpnameActivityOut


class OpnameOut(BaseModel):
    id: int
    opname_no: str
    opname_at: str
    officer: str
    location: str | None = None
    kind: str
    note: str | None = None
    status: str
    created_by: str | None = None
    created_at: str
    submitted_by: str | None = None
    submitted_at: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolution_note: str | None = None
    updated_at: str
    item_count: int
    total_diff: int
    has_moved: bool
    items: list[OpnameItemOut]
