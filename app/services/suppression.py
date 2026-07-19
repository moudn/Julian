"""Per-org do-not-contact list helpers (opt-outs, erasure requests)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SuppressedEmail


def suppress_email(db: Session, org_id: int, email: str | None, reason: str) -> None:
    """Add an address to the org's permanent do-not-contact list."""
    if not email:
        return
    exists = db.scalar(select(SuppressedEmail).where(
        SuppressedEmail.org_id == org_id,
        SuppressedEmail.email == email.lower()))
    if exists is None:
        db.add(SuppressedEmail(org_id=org_id, email=email.lower(), reason=reason))


def is_suppressed(db: Session, org_id: int, email: str | None) -> bool:
    if not email:
        return False
    return db.scalar(select(SuppressedEmail).where(
        SuppressedEmail.org_id == org_id,
        SuppressedEmail.email == email.lower())) is not None
