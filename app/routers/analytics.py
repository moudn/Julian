from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.database import get_db
from app.models import Organization
from app.routers.billing import require_active_subscription
from app.services.analytics import compute_analytics

router = APIRouter(prefix="/analytics", tags=["analytics"],
                   dependencies=[Depends(require_active_subscription)])


@router.get("")
def analytics(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Outreach effectiveness for this organization: funnel, per-step reply
    attribution, per-template reply rate, and an 8-week trend."""
    return compute_analytics(db, org)
