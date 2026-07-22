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


def _init_sentry():
    """Enable error tracking if a DSN is configured (optional dependency)."""
    dsn = get_settings().sentry_dsn
    if not dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, environment=get_settings().environment,
                        traces_sample_rate=0.0)
        logger.info("Sentry error tracking enabled")
    except ImportError:
        logger.warning("SENTRY_DSN set but sentry-sdk not installed; skipping")


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
    _init_sentry()
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
    """Liveness only — always cheap, no dependencies."""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    """Readiness — checks the database is reachable. 503 if not."""
    from sqlalchemy import text

    from fastapi import HTTPException
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    finally:
        db.close()
    return {"status": "ready"}


# Dashboard SPA (no build step; talks to the JSON API above)
app.mount("/app", StaticFiles(directory=Path(__file__).parent / "static", html=True),
          name="dashboard")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/app/")
