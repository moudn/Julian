import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import apollo, auth, billing, bookings, icp, integrations, leads, scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _send_loop(interval_seconds: int):
    from app.services.sending import run_send_cycle_all_orgs

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            db = SessionLocal()
            try:
                result = await asyncio.to_thread(run_send_cycle_all_orgs, db)
            finally:
                db.close()
            if result["sent"] or result["errors"]:
                logger.info("send cycle: %s", result)
        except Exception:  # keep the loop alive through transient failures
            logger.exception("send cycle crashed; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    task = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_send_loop(settings.scheduler_interval_seconds))
    yield
    if task is not None:
        task.cancel()


app = FastAPI(
    title="Julian — AI Sales Agent",
    description=(
        "Lead import, ICP scoring, research-backed outreach sequences on "
        "autopilot, and a human-approved meeting scheduling workflow."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(integrations.router)
app.include_router(leads.router)
app.include_router(icp.router)
app.include_router(apollo.router)
app.include_router(bookings.router)
app.include_router(scheduler.router)


@app.get("/health")
def health():
    return {"status": "ok"}
