"""Whether an org's subscription entitles it to run.

Lives here rather than in the billing router because the background
autopilot needs the same answer the HTTP layer does — a service importing
a router to ask "is this tenant paid up?" would be backwards, and letting
the two drift is how a cancelled tenant ends up still being served.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.stripe_billing import billing_enabled
from app.models import Organization

# Stripe statuses that mean "this tenant may use the product". Trialing
# counts: a 30-day trial is a real, intended entitlement.
ACTIVE_STATUSES = {"active", "trialing"}


def subscription_is_active(org: Organization) -> bool:
    """True when the org may use the product.

    Always True while billing is disabled (no Stripe key configured), which
    is the development and self-hosted case.
    """
    return not billing_enabled() or org.subscription_status in ACTIVE_STATUSES


def active_orgs(db: Session) -> list[Organization]:
    """Every org the background autopilot is allowed to act for.

    The scheduler must not keep sending outreach for a tenant whose
    subscription lapsed — they've stopped paying, and mail would still be
    going out under their name and from their mailbox.
    """
    orgs = db.scalars(select(Organization)).all()
    if not billing_enabled():
        return list(orgs)
    return [org for org in orgs if org.subscription_status in ACTIVE_STATUSES]
