from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.database import get_db
from app.models import Organization
from app.routers.billing import require_active_subscription
from app.services.sending import run_send_cycle

router = APIRouter(prefix="/scheduler", tags=["scheduler"],
                   dependencies=[Depends(require_active_subscription)])


class SendCycleOut(BaseModel):
    sent: int
    skipped: int
    errors: list[str]


@router.post("/run", response_model=SendCycleOut)
def run_cycle(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Send this org's due sequence messages now.

    The background loop does this automatically; this endpoint exists for
    cron-based deployments and for kicking a cycle manually.
    """
    return SendCycleOut(**run_send_cycle(db, org))
