"""The org's do-not-contact list: view it, and remove an entry deliberately.

Deliberately NOT behind require_active_subscription. Honouring opt-outs is a
legal obligation that outlives a lapsed subscription, so a customer must be
able to see and produce this list even when billing has stopped.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.database import get_db
from app.models import Organization, SuppressedEmail

router = APIRouter(prefix="/suppressions", tags=["suppressions"])
logger = logging.getLogger(__name__)

# Why an address ended up on the list, and how risky it is to take it off.
# An unsubscribe is a person's explicit request not to be contacted —
# removing it is the customer's decision and their liability, so the UI
# warns before allowing it.
REASON_LABELS = {
    "unsubscribed": "Asked to stop being contacted",
    "not_interested": "Said they weren't interested",
    "bounced": "Address was undeliverable",
    "erased": "Erasure request (right to be forgotten)",
}


class SuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    reason: str
    reason_label: str
    created_at: str


@router.get("", response_model=list[SuppressionOut])
def list_suppressions(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Every address this organization may not contact."""
    records = db.scalars(
        select(SuppressedEmail)
        .where(SuppressedEmail.org_id == org.id)
        .order_by(SuppressedEmail.created_at.desc())
    ).all()
    return [
        SuppressionOut(
            id=r.id, email=r.email, reason=r.reason,
            reason_label=REASON_LABELS.get(r.reason, r.reason),
            created_at=r.created_at.isoformat(),
        )
        for r in records
    ]


@router.delete("/{suppression_id}", status_code=204)
def remove_suppression(
    suppression_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Take one address off the do-not-contact list.

    One at a time and never in bulk: re-contacting someone who opted out is
    a decision that should cost a deliberate click each time.
    """
    record = db.get(SuppressedEmail, suppression_id)
    if record is None or record.org_id != org.id:
        raise HTTPException(status_code=404, detail="Suppression not found")
    # Leaves an audit trail of who was un-suppressed and on what grounds.
    logger.warning("org %s removed %s from its do-not-contact list "
                   "(was suppressed as %s)", org.id, record.email, record.reason)
    db.delete(record)
    db.commit()
