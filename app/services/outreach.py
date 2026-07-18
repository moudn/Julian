"""Sequence generation: turn a SCORED lead into a 4-step outreach plan."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.llm import SEQUENCE_CADENCE, OpenRouterAdapter
from app.models import Lead, LeadState, MessageStatus, Organization, OutreachMessage
from app.state_machine import transition


class OutreachError(Exception):
    pass


def generate_sequence(db: Session, lead: Lead, org: Organization,
                      llm: OpenRouterAdapter) -> list[OutreachMessage]:
    """Generate (or regenerate) the full outreach sequence for a lead.

    Allowed while SCORED (first generation, advances the lead to
    OUTREACH_PENDING) or OUTREACH_PENDING (regenerates unsent drafts).
    """
    if lead.state not in (LeadState.SCORED, LeadState.OUTREACH_PENDING):
        raise OutreachError(
            f"Lead must be SCORED or OUTREACH_PENDING to generate a sequence "
            f"(currently {lead.state.value})"
        )

    # Drop existing unsent drafts; never touch sent messages
    existing = db.scalars(select(OutreachMessage).where(
        OutreachMessage.lead_id == lead.id,
        OutreachMessage.status.in_([MessageStatus.DRAFT, MessageStatus.APPROVED]),
    )).all()
    for message in existing:
        db.delete(message)

    messages: list[OutreachMessage] = []
    prior_bodies: list[str] = []
    for step, days in SEQUENCE_CADENCE.items():
        draft = llm.generate_step(lead, org, step, prior_bodies)
        prior_bodies.append(draft["body"])
        messages.append(OutreachMessage(
            org_id=org.id,
            lead_id=lead.id,
            step=step,
            send_after_days=days,
            subject=draft["subject"],
            body=draft["body"],
            spam_flags=draft.get("spam_flags") or None,
        ))
    db.add_all(messages)

    lead.outreach_draft = messages[0].body
    if lead.state == LeadState.SCORED:
        transition(lead, LeadState.OUTREACH_PENDING)
    db.commit()
    for message in messages:
        db.refresh(message)
    return messages
