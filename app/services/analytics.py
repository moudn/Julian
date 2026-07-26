"""Per-organization outreach effectiveness metrics.

Computed on demand from existing tables (no separate event store):
  - the funnel: contacted -> replied -> interested -> booked
  - per-step reply attribution: which touch actually earns replies
  - per-template (variant) reply rate: how each method performs
  - a weekly trend of sends vs replies

Everything is scoped to one org; a client only ever sees their own numbers.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ConversationMessage,
    Lead,
    LeadState,
    MessageDirection,
    MessageStatus,
    Organization,
    OutreachMessage,
)

POSITIVE_CATEGORIES = ("INTERESTED", "SLOT_SELECTED")
WEEKS_OF_HISTORY = 8


def _rate(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def _week_start(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    monday = moment - timedelta(days=moment.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def compute_analytics(db: Session, org: Organization) -> dict:
    org_filter = OutreachMessage.org_id == org.id

    # --- funnel ---
    contacted_leads = set(db.scalars(
        select(OutreachMessage.lead_id)
        .where(org_filter, OutreachMessage.status == MessageStatus.SENT)
        .distinct()
    ).all())
    replied_leads = set(db.scalars(
        select(ConversationMessage.lead_id)
        .where(ConversationMessage.org_id == org.id,
               ConversationMessage.direction == MessageDirection.INBOUND)
        .distinct()
    ).all())
    interested_leads = set(db.scalars(
        select(ConversationMessage.lead_id)
        .where(ConversationMessage.org_id == org.id,
               ConversationMessage.direction == MessageDirection.INBOUND,
               ConversationMessage.category.in_(POSITIVE_CATEGORIES))
        .distinct()
    ).all())
    booked = db.scalar(select(func.count(Lead.id)).where(
        Lead.org_id == org.id, Lead.state == LeadState.MEETING_CONFIRMED)) or 0
    unsubscribed = db.scalar(select(func.count(Lead.id)).where(
        Lead.org_id == org.id, Lead.state == LeadState.UNSUBSCRIBED)) or 0
    # Sends that never made it out. Without these the page just shows zeros
    # across the board when e.g. SMTP is misconfigured, which reads as
    # "analytics is broken" rather than "nothing actually sent".
    failed = db.scalar(select(func.count(OutreachMessage.id)).where(
        org_filter, OutreachMessage.status == MessageStatus.FAILED)) or 0
    queued = db.scalar(select(func.count(OutreachMessage.id)).where(
        org_filter, OutreachMessage.status == MessageStatus.APPROVED)) or 0

    contacted = len(contacted_leads)
    # only count replies from leads we actually contacted
    replied = len(replied_leads & contacted_leads)
    interested = len(interested_leads & contacted_leads)

    funnel = {
        "contacted": contacted,
        "replied": replied,
        "interested": interested,
        "meetings_booked": booked,
        "unsubscribed": unsubscribed,
        "failed": failed,
        "queued": queued,
        "reply_rate": _rate(replied, contacted),
        "interested_rate": _rate(interested, contacted),
        "meeting_rate": _rate(booked, contacted),
    }

    # --- per step: sends and replies attributed to each touch ---
    sent_by_step = dict(db.execute(
        select(OutreachMessage.step, func.count(OutreachMessage.id))
        .where(org_filter, OutreachMessage.status == MessageStatus.SENT)
        .group_by(OutreachMessage.step)
    ).all())
    replies_by_step = dict(db.execute(
        select(ConversationMessage.replied_after_step, func.count(ConversationMessage.id))
        .where(ConversationMessage.org_id == org.id,
               ConversationMessage.direction == MessageDirection.INBOUND,
               ConversationMessage.replied_after_step.is_not(None))
        .group_by(ConversationMessage.replied_after_step)
    ).all())
    per_step = [
        {
            "step": step,
            "sent": sent_by_step.get(step, 0),
            "replies": replies_by_step.get(step, 0),
            "reply_rate": _rate(replies_by_step.get(step, 0), sent_by_step.get(step, 0)),
        }
        for step in sorted(set(sent_by_step) | set(replies_by_step))
    ]

    # --- per template variant: reply rate of leads by the variant of their
    # first touch ---
    lead_variant = dict(db.execute(
        select(OutreachMessage.lead_id, OutreachMessage.variant)
        .where(org_filter, OutreachMessage.status == MessageStatus.SENT,
               OutreachMessage.step == 1)
    ).all())
    variant_contacted: dict[str, int] = defaultdict(int)
    variant_replied: dict[str, int] = defaultdict(int)
    for lead_id, variant in lead_variant.items():
        variant_contacted[variant] += 1
        if lead_id in replied_leads:
            variant_replied[variant] += 1
    per_variant = [
        {
            "variant": variant,
            "contacted": variant_contacted[variant],
            "replied": variant_replied[variant],
            "reply_rate": _rate(variant_replied[variant], variant_contacted[variant]),
        }
        for variant in sorted(variant_contacted)
    ]

    # --- weekly trend (last 8 weeks) ---
    now = datetime.now(timezone.utc)
    cutoff = _week_start(now) - timedelta(weeks=WEEKS_OF_HISTORY - 1)
    weekly: dict[datetime, dict] = {
        _week_start(now) - timedelta(weeks=offset): {"sent": 0, "replies": 0}
        for offset in range(WEEKS_OF_HISTORY)
    }
    for (sent_at,) in db.execute(
        select(OutreachMessage.sent_at).where(
            org_filter, OutreachMessage.status == MessageStatus.SENT,
            OutreachMessage.sent_at.is_not(None))
    ).all():
        bucket = _week_start(sent_at)
        if bucket in weekly:
            weekly[bucket]["sent"] += 1
    for (created_at,) in db.execute(
        select(ConversationMessage.created_at).where(
            ConversationMessage.org_id == org.id,
            ConversationMessage.direction == MessageDirection.INBOUND)
    ).all():
        bucket = _week_start(created_at)
        if bucket in weekly:
            weekly[bucket]["replies"] += 1
    trend = [
        {"week": week.date().isoformat(), **counts}
        for week, counts in sorted(weekly.items())
    ]

    return {
        "funnel": funnel,
        "per_step": per_step,
        "per_variant": per_variant,
        "weekly": trend,
    }
