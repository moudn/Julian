"""System-level maintenance endpoint — not part of the customer-facing API.

Exists for hosts that can't keep a persistent background process running
(e.g. a free tier that sleeps the app when idle) — an external cron
service (cron-job.org, GitHub Actions on a schedule, etc.) can hit this on
an interval instead of relying on app.main's in-process _agent_loop. Also
happens to keep such a host from sleeping at all, since every hit counts
as inbound traffic.
"""

import hmac

from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/run-cycle")
def run_cycle(x_cron_secret: str = Header(default="")):
    settings = get_settings()
    if not settings.cron_secret:
        raise HTTPException(status_code=503,
                            detail="CRON_SECRET is not configured — this endpoint is disabled")
    if not hmac.compare_digest(x_cron_secret, settings.cron_secret):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret header")

    from app.services.agent_cycle import run_agent_cycle
    return run_agent_cycle()
