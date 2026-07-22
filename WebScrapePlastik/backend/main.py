from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.auth import get_current_user, require_admin
from backend.database import fail_orphaned_jobs, init_db, purge_expired_sessions
from backend.routes.auth import router as auth_router
from backend.routes.inventory import router as inventory_router
from backend.routes.receipts import router as receipts_router
from backend.routes.results import router as results_router
from backend.routes.sales import router as sales_router
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(users_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(inventory_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(receipts_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(results_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(sales_router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(scrape_router, prefix="/api", dependencies=[Depends(get_current_user)])

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
