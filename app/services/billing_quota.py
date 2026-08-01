"""Lead-quota enforcement against the org's subscribed plan.

Metering is "leads created this billing period" — matches what the
customer sees ("N of 100 leads used this month") and is simple to reason
about, even though the actual per-lead cost (research, drafting,
classification) is really incurred later, when a lead gets worked rather
than merely imported.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Lead, Organization
from app.plans import PLANS


def leads_used_this_period(db: Session, org: Organization) -> int:
    if org.current_period_start is None:
        return 0
    return db.scalar(select(func.count(Lead.id)).where(
        Lead.org_id == org.id, Lead.created_at >= org.current_period_start,
    )) or 0


def lead_quota_remaining(db: Session, org: Organization) -> int | None:
    """None means unlimited — no recognized plan on record (billing
    disabled, or a subscription not yet synced from Stripe) or no billing
    period known yet. Never blocks in that case; only blocks once a real
    plan and period start are both on record.
    """
    plan = PLANS.get(org.plan or "")
    if plan is None or org.current_period_start is None:
        return None
    return max(0, plan.lead_limit - leads_used_this_period(db, org))
