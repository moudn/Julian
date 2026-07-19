import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import (
    apollo,
    auth,
    billing,
    bookings,
    icp,
    integrations,
    leads,
    replies,
    scheduler,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _run_agent_cycle() -> dict:
    """One full autopilot pass: triage new replies first, then send due steps."""
    from app.services.replies import run_reply_cycle_all_orgs
    from app.services.sending import run_send_cycle_all_orgs

    db = SessionLocal()
    try:
        replies_result = run_reply_cycle_all_orgs(db)
        send_result = run_send_cycle_all_orgs(db)
    finally:
        db.close()
    return {"replies": replies_result, "send": send_result}


async def _agent_loop(interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(_run_agent_cycle)
            if (result["send"]["sent"] or result["send"]["errors"]
                    or result["replies"]["processed"] or result["replies"]["errors"]):
                logger.info("agent cycle: %s", result)
        except Exception:  # keep the loop alive through transient failures
            logger.exception("agent cycle crashed; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    task = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_agent_loop(settings.scheduler_interval_seconds))
    yield
    if task is not None:
        task.cancel()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # 'unsafe-inline' is required by the dashboard's inline event handlers;
    # XSS is mitigated by output escaping. Still blocks external script/data
    # exfiltration targets.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'"
    ),
}


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
app.include_router(replies.router)
app.include_router(scheduler.router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


# Dashboard SPA (no build step; talks to the JSON API above)
app.mount("/app", StaticFiles(directory=Path(__file__).parent / "static", html=True),
          name="dashboard")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/app/")
