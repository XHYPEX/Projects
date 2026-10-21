from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.activity import ActivityLogMiddleware
from backend.auth import get_current_user, require_admin
from backend.config import SCRAPER_ENABLED
from backend.cost_privacy import CostPrivacyMiddleware
from backend.idempotency import IdempotencyMiddleware
from backend.database import fail_orphaned_jobs, init_db, purge_expired_sessions
from backend.routes.activity import router as activity_router
from backend.routes.auth import router as auth_router
from backend.routes.categories import router as categories_router
from backend.routes.customers import router as customers_router
from backend.routes.inventory import router as inventory_router
from backend.routes.goods_receipts import router as goods_receipts_router
from backend.routes.incentives import router as incentives_router, work_router as work_orders_router
from backend.routes.opname import router as opname_router
from backend.routes.preorders import router as preorders_router
from backend.routes.quarantine import router as quarantine_router
from backend.routes.supplier_returns import router as supplier_returns_router
from backend.routes.services import router as services_router
from backend.routes.forwarders import router as forwarders_router
from backend.routes.supplier_pos import router as supplier_pos_router
from backend.routes.purchase_invoices import router as purchase_invoices_router
from backend.routes.receipts import router as receipts_router
from backend.routes.results import router as results_router
from backend.routes.sales import router as sales_router
from backend.routes.cash_receipts import router as cash_receipts_router
from backend.routes.account_payable import router as account_payable_router
from backend.routes.order_dashboards import router as order_dashboards_router
from backend.routes.scrape import router as scrape_router
from backend.routes.users import router as users_router

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    fail_orphaned_jobs("Interrupted: the server restarted while this job was in progress.")
    purge_expired_sessions()
    yield


app = FastAPI(title="Google Maps Scraper", lifespan=lifespan)

# Added before CORS so it wraps the outermost layer and sees every request
# that reaches the app, including ones rejected further in.
app.add_middleware(CostPrivacyMiddleware)
app.add_middleware(ActivityLogMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost: a replayed POST is answered from storage before anything (including the activity
# log) sees it, so a double click is neither executed nor logged twice.
app.add_middleware(IdempotencyMiddleware)


@app.get("/api/config")
async def get_app_config():
    """Feature flags the frontend needs before it renders its nav. Unauthenticated:
    it exposes nothing beyond which pages exist."""
    return {"scraper_enabled": SCRAPER_ENABLED}


app.include_router(auth_router, prefix="/api")
app.include_router(users_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(activity_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(customers_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(inventory_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(goods_receipts_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(work_orders_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(incentives_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(categories_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(opname_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(preorders_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(quarantine_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(supplier_returns_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(services_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(forwarders_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(supplier_pos_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(purchase_invoices_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(receipts_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(results_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(sales_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(cash_receipts_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(account_payable_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(order_dashboards_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(scrape_router, prefix="/api", dependencies=[Depends(get_current_user)])

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
