"""Reply pipeline: ingest inbound email, triage it, act on it.

Triage behaviors (agreed product design):
- UNSUBSCRIBE / NOT_INTERESTED -> terminal state, sequence retired, no human
  needed.
- OUT_OF_OFFICE -> postpone remaining sequence steps; no state change.
- INTERESTED -> lead becomes ENGAGED; the rep is notified with a suggested
  reply ready to send.
- QUESTION -> if Julian has a pre-approved answer from the org's knowledge
  base he replies himself (lead becomes ENGAGED); otherwise escalates.
- COMPLEX -> lead becomes ENGAGED; the rep gets the thread plus a suggested
  draft, so the human closes while Julian does the typing.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.gmail import GmailError, GmailReaderAdapter
from app.adapters.llm import OpenRouterAdapter
from app.models import (
    ConversationMessage,
    GoogleCredential,
    Lead,
    LeadState,
    MessageDirection,
    MessageStatus,
    Organization,
    OutreachMessage,
    ReplyCategory,
    utcnow,
)
from app.services.sending import get_outbound_sender
from app.state_machine import transition

logger = logging.getLogger(__name__)

OOO_POSTPONE_DAYS = 7

# States in which an inbound reply gets full triage treatment
TRIAGEABLE_STATES = {
    LeadState.SEQUENCE_ACTIVE,
    LeadState.ENGAGED,
    LeadState.MEETING_PROPOSED,
    LeadState.OUTREACH_PENDING,
}


class ReplyError(Exception):
    pass


def _retire_pending_steps(db: Session, lead: Lead) -> None:
    steps = db.scalars(select(OutreachMessage).where(
        OutreachMessage.lead_id == lead.id,
        OutreachMessage.status.in_([MessageStatus.APPROVED, MessageStatus.DRAFT]),
    )).all()
    for step in steps:
        step.status = MessageStatus.SKIPPED


def _postpone_pending_steps(db: Session, lead: Lead, days: int) -> None:
    steps = db.scalars(select(OutreachMessage).where(
        OutreachMessage.lead_id == lead.id,
        OutreachMessage.status == MessageStatus.APPROVED,
    )).all()
    for step in steps:
        if step.scheduled_at is not None:
            step.scheduled_at = step.scheduled_at + timedelta(days=days)


def _thread_bodies(db: Session, lead: Lead, limit: int = 6) -> list[str]:
    messages = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.lead_id == lead.id)
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
    ).all()
    return [m.body for m in reversed(messages)]


def ingest_reply(
    db: Session,
    lead: Lead,
    org: Organization,
    body: str,
    subject: str | None = None,
    gmail_message_id: str | None = None,
    llm: OpenRouterAdapter | None = None,
    notifier: EmailSenderAdapter | None = None,
    outbound_sender=None,
) -> dict:
    """Record and triage one inbound reply. Returns a result summary."""
    if gmail_message_id and db.scalar(select(ConversationMessage).where(
            ConversationMessage.org_id == org.id,
            ConversationMessage.gmail_message_id == gmail_message_id)):
        return {"status": "duplicate", "category": None}

    llm = llm or OpenRouterAdapter()
    notifier = notifier or EmailSenderAdapter()

    thread = _thread_bodies(db, lead)
    result = llm.classify_reply(lead, org, body, thread)
    category = ReplyCategory(result["category"])

    inbound = ConversationMessage(
        org_id=org.id,
        lead_id=lead.id,
        direction=MessageDirection.INBOUND,
        subject=subject,
        body=body,
        gmail_message_id=gmail_message_id,
        category=category.value,
        suggested_reply=result.get("suggested_reply") or None,
    )
    db.add(inbound)

    if lead.state not in TRIAGEABLE_STATES:
        db.commit()
        return {"status": "recorded_only", "category": category.value,
                "lead_state": lead.state.value}

    auto_replied = False
    escalated = False

    if category == ReplyCategory.UNSUBSCRIBE:
        _retire_pending_steps(db, lead)
        if lead.state != LeadState.OUTREACH_PENDING:
            transition(lead, LeadState.UNSUBSCRIBED)
        else:
            lead.state = LeadState.UNSUBSCRIBED

    elif category == ReplyCategory.NOT_INTERESTED:
        _retire_pending_steps(db, lead)
        if lead.state != LeadState.OUTREACH_PENDING:
            transition(lead, LeadState.NOT_INTERESTED)
        else:
            lead.state = LeadState.NOT_INTERESTED

    elif category == ReplyCategory.OUT_OF_OFFICE:
        _postpone_pending_steps(db, lead, OOO_POSTPONE_DAYS)

    elif category == ReplyCategory.QUESTION and result.get("answer"):
        # Julian answers from the pre-approved knowledge base only
        _retire_pending_steps(db, lead)
        if lead.state in (LeadState.SEQUENCE_ACTIVE, LeadState.OUTREACH_PENDING):
            _to_engaged(lead)
        sender = outbound_sender or get_outbound_sender(db, org)
        try:
            sender.send(to=lead.email, subject=f"Re: {subject or 'your question'}",
                        body=result["answer"])
            db.add(ConversationMessage(
                org_id=org.id, lead_id=lead.id,
                direction=MessageDirection.OUTBOUND,
                subject=f"Re: {subject or 'your question'}",
                body=result["answer"],
            ))
            auto_replied = True
        except (GmailError, OSError) as exc:
            logger.warning("auto-reply failed for lead %s: %s", lead.id, exc)
            escalated = True
            _notify_rep(notifier, org, lead, body, result.get("suggested_reply")
                        or result.get("answer") or "")

    else:  # INTERESTED, COMPLEX, or QUESTION without a safe answer
        _retire_pending_steps(db, lead)
        if lead.state in (LeadState.SEQUENCE_ACTIVE, LeadState.OUTREACH_PENDING):
            _to_engaged(lead)
        escalated = True
        _notify_rep(notifier, org, lead, body, result.get("suggested_reply") or "")

    db.commit()
    return {
        "status": "processed",
        "category": category.value,
        "lead_state": lead.state.value,
        "auto_replied": auto_replied,
        "escalated": escalated,
        "suggested_reply": result.get("suggested_reply") or None,
    }


def _to_engaged(lead: Lead) -> None:
    if lead.state == LeadState.OUTREACH_PENDING:
        # replies can arrive before activation (e.g. manual first touch)
        lead.state = LeadState.ENGAGED
    else:
        transition(lead, LeadState.ENGAGED)


def _notify_rep(notifier: EmailSenderAdapter, org: Organization, lead: Lead,
                reply_body: str, suggested_reply: str) -> None:
    if not org.sales_rep_email:
        return
    suggestion = (
        f"Suggested reply (edit or send as-is):\n\n{suggested_reply}"
        if suggested_reply else "No suggested reply — needs your judgement."
    )
    notifier.send(
        to=org.sales_rep_email,
        subject=f"Julian: {lead.name} replied — your turn",
        body=(
            f"{lead.name}"
            f"{f' ({lead.company})' if lead.company else ''} replied:\n\n"
            f"{reply_body}\n\n----\n{suggestion}\n\n"
            f"The outreach sequence for this lead has been stopped."
        ),
    )


def poll_replies(db: Session, org: Organization, reader: GmailReaderAdapter,
                 llm: OpenRouterAdapter | None = None,
                 notifier: EmailSenderAdapter | None = None,
                 outbound_sender=None) -> dict:
    """Fetch new inbound mail from active leads and run each through triage."""
    leads = db.scalars(select(Lead).where(
        Lead.org_id == org.id,
        Lead.state.in_(list(TRIAGEABLE_STATES)),
        Lead.email.is_not(None),
    )).all()

    processed, duplicates, errors = 0, 0, []
    for lead in leads:
        try:
            message_ids = reader.list_message_ids(
                f"from:{lead.email} newer_than:14d")
        except GmailError as exc:
            errors.append(f"lead {lead.id}: {exc}")
            continue
        for message_id in message_ids:
            exists = db.scalar(select(ConversationMessage).where(
                ConversationMessage.org_id == org.id,
                ConversationMessage.gmail_message_id == message_id))
            if exists:
                duplicates += 1
                continue
            try:
                message = reader.get_message(message_id)
            except GmailError as exc:
                errors.append(f"message {message_id}: {exc}")
                continue
            result = ingest_reply(
                db, lead, org,
                body=message["body"], subject=message["subject"],
                gmail_message_id=message["id"], llm=llm, notifier=notifier,
                outbound_sender=outbound_sender,
            )
            if result["status"] == "processed":
                processed += 1
    return {"processed": processed, "duplicates": duplicates, "errors": errors}


def run_reply_cycle_all_orgs(db: Session) -> dict:
    """Background-loop entry point: poll Gmail for every connected org."""
    from app.adapters.google_oauth import get_valid_access_token

    totals = {"processed": 0, "duplicates": 0, "errors": []}
    orgs = db.scalars(select(Organization)).all()
    for org in orgs:
        credential = db.scalar(select(GoogleCredential).where(
            GoogleCredential.org_id == org.id))
        if credential is None:
            continue
        reader = GmailReaderAdapter(
            token_provider=lambda c=credential: get_valid_access_token(db, c))
        result = poll_replies(db, org, reader)
        totals["processed"] += result["processed"]
        totals["duplicates"] += result["duplicates"]
        totals["errors"].extend(result["errors"])
    return totals
