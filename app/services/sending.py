"""Autopilot sending: sequence activation and the cadence send cycle.

Activation is the customer's one approval ("this sequence is good — go").
From then on the send cycle mails each step when due, exclusively while the
lead is still in SEQUENCE_ACTIVE — a reply (handled by the reply pipeline)
moves the lead out of that state and silences the rest of the sequence
automatically.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.gmail import GmailError, GmailSenderAdapter
from app.adapters.google_oauth import get_valid_access_token
from app.models import (
    GoogleCredential,
    Lead,
    LeadState,
    MessageStatus,
    Organization,
    OutreachMessage,
    utcnow,
)
from app.state_machine import transition

logger = logging.getLogger(__name__)

DEFAULT_FOOTER = "\n\n--\nIf you'd rather not hear from me again, just reply \"no thanks\"."


class SendingError(Exception):
    pass


def activate_sequence(db: Session, lead: Lead, org: Organization) -> list[OutreachMessage]:
    """Approve all drafts and schedule them; the lead goes on autopilot."""
    if lead.state != LeadState.OUTREACH_PENDING:
        raise SendingError(
            f"Lead must be OUTREACH_PENDING to activate its sequence "
            f"(currently {lead.state.value})"
        )
    if not lead.email:
        raise SendingError("Lead has no email address")

    drafts = db.scalars(
        select(OutreachMessage)
        .where(OutreachMessage.lead_id == lead.id,
               OutreachMessage.status == MessageStatus.DRAFT)
        .order_by(OutreachMessage.step)
    ).all()
    if not drafts:
        raise SendingError(
            "No drafts to activate — generate a sequence first "
            "(POST /leads/{id}/generate_sequence)"
        )

    now = utcnow()
    for message in drafts:
        message.status = MessageStatus.APPROVED
        message.scheduled_at = now + timedelta(days=message.send_after_days)

    transition(lead, LeadState.SEQUENCE_ACTIVE)
    db.commit()
    for message in drafts:
        db.refresh(message)
    return drafts


def get_outbound_sender(db: Session, org: Organization):
    """The tenant's Gmail when connected; console/SMTP fallback otherwise."""
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is not None:
        return GmailSenderAdapter(
            token_provider=lambda: get_valid_access_token(db, credential)
        )
    return EmailSenderAdapter()


def run_send_cycle(db: Session, org: Organization, sender=None) -> dict:
    """Send every due, approved message for one organization.

    Only leads still in SEQUENCE_ACTIVE receive mail; anything that left the
    state (replied, unsubscribed, booked) is skipped and the pending steps
    are marked SKIPPED so they can never fire later.
    """
    now = utcnow()
    due = db.scalars(
        select(OutreachMessage)
        .where(OutreachMessage.org_id == org.id,
               OutreachMessage.status == MessageStatus.APPROVED,
               OutreachMessage.scheduled_at <= now)
        .order_by(OutreachMessage.lead_id, OutreachMessage.step)
    ).all()

    sent, skipped, errors = 0, 0, []
    if not due:
        return {"sent": 0, "skipped": 0, "errors": []}

    sender = sender or get_outbound_sender(db, org)
    footer = org.email_footer if org.email_footer is not None else DEFAULT_FOOTER

    for message in due:
        lead = db.get(Lead, message.lead_id)
        if lead is None or lead.state != LeadState.SEQUENCE_ACTIVE or not lead.email:
            message.status = MessageStatus.SKIPPED
            skipped += 1
            continue
        try:
            sender.send(to=lead.email, subject=message.subject,
                        body=message.body + (footer or ""))
        except (GmailError, OSError) as exc:
            # Leave the message APPROVED so the next cycle retries it
            errors.append(f"message {message.id} (lead {lead.id}): {exc}")
            logger.warning("send failed for message %s: %s", message.id, exc)
            continue
        message.status = MessageStatus.SENT
        message.sent_at = utcnow()
        sent += 1

    db.commit()
    return {"sent": sent, "skipped": skipped, "errors": errors}


def run_send_cycle_all_orgs(db: Session) -> dict:
    """Background-loop entry point: process every organization."""
    totals = {"sent": 0, "skipped": 0, "errors": []}
    for org in db.scalars(select(Organization)).all():
        result = run_send_cycle(db, org)
        totals["sent"] += result["sent"]
        totals["skipped"] += result["skipped"]
        totals["errors"].extend(result["errors"])
    return totals
