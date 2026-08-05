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
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.calendar import CalendarError
from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.gmail import GmailError, GmailReaderAdapter
from app.adapters.llm import OpenRouterAdapter
from app.deps import get_org_calendar
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
from app.services.schedule_manager import ScheduleError, ScheduleManager
from app.services.sending import get_outbound_sender
from app.services.suppression import suppress_email
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


# Marks the outbound "here's what we do, worth a call?" reply, so it is sent
# at most once per lead and so a later positive reply can be read as the
# answer to the call question it asked.
RUNDOWN_CATEGORY = "INTEREST_RUNDOWN"


def _rundown_already_sent(db: Session, lead: Lead) -> bool:
    return db.scalar(select(ConversationMessage).where(
        ConversationMessage.lead_id == lead.id,
        ConversationMessage.direction == MessageDirection.OUTBOUND,
        ConversationMessage.category == RUNDOWN_CATEGORY,
    )) is not None


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


_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_ORDINAL_WORDS = r"(first|1st|second|2nd|third|3rd)"
# An ordinal only counts as picking a slot when it sits in a selecting
# phrase. A bare word search matched things like "can you tell me about
# pricing first" or "I'd need to check with my team first" and booked the
# lead into slot 1 — then emailed them saying it was pencilled in.
_ORDINAL_SELECTION_PATTERNS = [
    rf"\b{_ORDINAL_WORDS}\s+(?:one|option|slot|time)\b",
    rf"\b(?:option|slot)\s+(?:the\s+)?{_ORDINAL_WORDS}\b",
    rf"\b(?:do|take|pick|choose|go with|prefer|book)\s+(?:the\s+)?{_ORDINAL_WORDS}\b",
    rf"\b{_ORDINAL_WORDS}\s+(?:works|suits|is good|is best|would work|sounds good)\b",
]


def extract_slot_choice(body: str, proposed_slots: list[str] | None) -> datetime | None:
    """Deterministically match a reply against the proposed slots.

    Understands "option 2", "the first one", weekday names, and times like
    "9am" / "09:00". Returns the chosen slot only when the match is
    unambiguous — anything uncertain returns None and goes to a human.
    """
    if not proposed_slots:
        return None
    slots = []
    for raw in proposed_slots:
        slot = datetime.fromisoformat(raw)
        if slot.tzinfo is None:
            slot = slot.replace(tzinfo=timezone.utc)
        slots.append(slot)
    lowered = body.lower()

    match = re.search(r"\b(?:option|slot|number)\s*#?\s*([1-9])\b", lowered)
    if match:
        index = int(match.group(1)) - 1
        return slots[index] if 0 <= index < len(slots) else None

    ordinal_hits = {_ORDINALS[m.group(1)]
                    for pattern in _ORDINAL_SELECTION_PATTERNS
                    for m in re.finditer(pattern, lowered)}
    if len(ordinal_hits) == 1:
        index = ordinal_hits.pop() - 1
        return slots[index] if 0 <= index < len(slots) else None

    weekday_hits = [s for s in slots if s.strftime("%A").lower() in lowered]
    if len({s.date() for s in weekday_hits}) == 1 and weekday_hits:
        if len(weekday_hits) == 1:
            return weekday_hits[0]
        # same day, several times — try to disambiguate by time below
        slots = weekday_hits

    compact = lowered.replace(" ", "").replace(".", "")
    time_hits = [s for s in slots
                 if s.strftime("%H:%M") in lowered
                 or s.strftime("%I%p").lstrip("0").lower() in compact
                 or s.strftime("%I:%M%p").lstrip("0").lower() in compact]
    if len(time_hits) == 1:
        return time_hits[0]
    return None


def _last_sent_step(db: Session, lead: Lead) -> int | None:
    """The highest step number already emailed to this lead — used to
    attribute an inbound reply to the touch that earned it."""
    return db.scalar(
        select(func.max(OutreachMessage.step)).where(
            OutreachMessage.lead_id == lead.id,
            OutreachMessage.status == MessageStatus.SENT)
    )


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

    # A lead choosing one of the proposed slots is the structured path to a
    # booking — checked deterministically before any classification.
    if lead.state == LeadState.MEETING_PROPOSED:
        chosen = extract_slot_choice(body, lead.proposed_slots)
        if chosen is not None:
            return _handle_slot_selection(
                db, lead, org, body, subject, gmail_message_id, chosen,
                notifier, outbound_sender)

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
        replied_after_step=_last_sent_step(db, lead),
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
        suppress_email(db, org.id, lead.email, "unsubscribed")
        if lead.state != LeadState.OUTREACH_PENDING:
            transition(lead, LeadState.UNSUBSCRIBED)
        else:
            lead.state = LeadState.UNSUBSCRIBED

    elif category == ReplyCategory.NOT_INTERESTED:
        _retire_pending_steps(db, lead)
        suppress_email(db, org.id, lead.email, "not_interested")
        if lead.state != LeadState.OUTREACH_PENDING:
            transition(lead, LeadState.NOT_INTERESTED)
        else:
            lead.state = LeadState.NOT_INTERESTED

    elif category == ReplyCategory.OUT_OF_OFFICE:
        _postpone_pending_steps(db, lead, OOO_POSTPONE_DAYS)

    elif (category == ReplyCategory.QUESTION and result.get("answer")
          and org.auto_reply_enabled):
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

    elif (category == ReplyCategory.INTERESTED
          # They asked to meet outright, OR they're answering the call
          # question Julian already put to them in the rundown below — a
          # positive reply to "worth a call?" is a yes.
          and (result.get("wants_meeting") or _rundown_already_sent(db, lead))
          # And only once. Without this, a lead who replies again after
          # times were offered drops back to ENGAGED and gets a fresh set of
          # times on every subsequent positive reply — a real spam loop.
          and not lead.proposed_slots
          and lead.state in (LeadState.SEQUENCE_ACTIVE,
                             LeadState.OUTREACH_PENDING, LeadState.ENGAGED)):
        # Structured path: they asked for a call, so propose concrete slots.
        # Falls back to human handoff if the calendar can't produce slots.
        _retire_pending_steps(db, lead)
        try:
            sender = outbound_sender or get_outbound_sender(db, org)
            manager = ScheduleManager(
                calendar=get_org_calendar(db, org), email_sender=sender, org=org)
            slots = manager.propose_meeting(db, lead)
            db.add(ConversationMessage(
                org_id=org.id, lead_id=lead.id,
                direction=MessageDirection.OUTBOUND,
                subject="Re: " + (subject or "our call"),
                body="[Julian proposed meeting times] "
                     + ", ".join(manager._fmt(s) for s in slots),
            ))
            if org.sales_rep_email:
                notifier.send(
                    to=org.sales_rep_email,
                    subject=f"Julian: {lead.name} is interested — times proposed",
                    body=(f"{lead.name} replied:\n\n{body}\n\n----\n"
                          f"Julian offered them "
                          f"{len(slots)} times from the calendar. When they pick "
                          f"one you'll get an approval request. No action needed "
                          f"right now."),
                )
        except (ScheduleError, CalendarError, GmailError, OSError) as exc:
            logger.warning("auto-propose failed for lead %s: %s", lead.id, exc)
            if lead.state != LeadState.MEETING_PROPOSED:
                _to_engaged(lead)
            escalated = True
            _notify_rep(notifier, org, lead, body,
                        result.get("suggested_reply") or "")

    elif (category == ReplyCategory.INTERESTED
          and org.auto_reply_enabled
          # Once only — if they're still curious after the rundown, that's a
          # conversation for a human, not a second explanation.
          and not _rundown_already_sent(db, lead)
          and lead.state in (LeadState.SEQUENCE_ACTIVE,
                             LeadState.OUTREACH_PENDING, LeadState.ENGAGED)
          and (rundown := (llm.compose_interest_reply(
              lead, org, body, _thread_bodies(db, lead))))):
        # Curious but hasn't committed to a call. Answer the actual question
        # they asked — what is this? — from approved material only, and put
        # the call to them at the end. Emailing calendar slots at this point
        # would be answering a question they didn't ask.
        _retire_pending_steps(db, lead)
        if lead.state in (LeadState.SEQUENCE_ACTIVE, LeadState.OUTREACH_PENDING):
            _to_engaged(lead)
        sender = outbound_sender or get_outbound_sender(db, org)
        reply_subject = "Re: " + (subject or "your question")
        try:
            sender.send(to=lead.email, subject=reply_subject, body=rundown)
            db.add(ConversationMessage(
                org_id=org.id, lead_id=lead.id,
                direction=MessageDirection.OUTBOUND,
                subject=reply_subject, body=rundown,
                category=RUNDOWN_CATEGORY,
            ))
            auto_replied = True
            if org.sales_rep_email:
                notifier.send(
                    to=org.sales_rep_email,
                    subject=f"Julian: {lead.name} asked what this is — briefed them",
                    body=(f"{lead.name} replied:\n\n{body}\n\n----\n"
                          f"Julian sent them this and asked whether a call is "
                          f"worth it:\n\n{rundown}\n\n----\n"
                          f"If they say yes you'll get an approval request. "
                          f"No action needed right now."),
                )
        except (GmailError, OSError) as exc:
            logger.warning("interest rundown failed for lead %s: %s", lead.id, exc)
            escalated = True
            _notify_rep(notifier, org, lead, body,
                        result.get("suggested_reply") or "")

    else:  # COMPLEX, QUESTION without a safe answer, or INTERESTED mid-proposal
        _retire_pending_steps(db, lead)
        if lead.state in (LeadState.SEQUENCE_ACTIVE, LeadState.OUTREACH_PENDING,
                          LeadState.MEETING_PROPOSED):
            _to_engaged(lead)
        escalated = True
        _notify_rep(notifier, org, lead, body,
                    result.get("suggested_reply") or result.get("answer") or "")

    db.commit()
    return {
        "status": "processed",
        "category": category.value,
        "lead_state": lead.state.value,
        "auto_replied": auto_replied,
        "escalated": escalated,
        "suggested_reply": (result.get("suggested_reply")
                            or result.get("answer") or None),
        "booking_id": None,
    }


def _handle_slot_selection(db: Session, lead: Lead, org: Organization, body: str,
                           subject: str | None, gmail_message_id: str | None,
                           chosen: datetime, notifier: EmailSenderAdapter,
                           outbound_sender) -> dict:
    """The lead picked a proposed slot: create the PendingBooking.

    ScheduleManager.select_slot records the booking and asks the rep for
    approval — the calendar is still untouched until POST /approve_booking.
    """
    db.add(ConversationMessage(
        org_id=org.id, lead_id=lead.id,
        direction=MessageDirection.INBOUND,
        subject=subject, body=body,
        gmail_message_id=gmail_message_id,
        category="SLOT_SELECTED",
        replied_after_step=_last_sent_step(db, lead),
    ))
    manager = ScheduleManager(
        calendar=get_org_calendar(db, org), email_sender=notifier, org=org)
    try:
        booking = manager.select_slot(db, lead, chosen)
    except ScheduleError as exc:
        logger.warning("slot selection failed for lead %s: %s", lead.id, exc)
        db.commit()
        return {"status": "processed", "category": "SLOT_SELECTED",
                "lead_state": lead.state.value, "auto_replied": False,
                "escalated": False, "suggested_reply": None, "booking_id": None}

    acknowledged = False
    try:
        sender = outbound_sender or get_outbound_sender(db, org)
        ack = (f"Hi {lead.name.split()[0]},\n\n"
               f"Perfect — I've pencilled in "
               f"{manager._fmt(chosen)}. You'll receive a "
               f"calendar invitation shortly.\n\nSpeak soon,\n"
               f"{(org.sender_name or '').strip() or ('The ' + org.name + ' team')}")
        sender.send(to=lead.email, subject="Re: " + (subject or "our call"), body=ack)
        db.add(ConversationMessage(
            org_id=org.id, lead_id=lead.id,
            direction=MessageDirection.OUTBOUND,
            subject="Re: " + (subject or "our call"), body=ack,
        ))
        acknowledged = True
    except (GmailError, OSError) as exc:
        logger.warning("slot ack failed for lead %s: %s", lead.id, exc)

    db.commit()
    return {"status": "processed", "category": "SLOT_SELECTED",
            "lead_state": lead.state.value, "auto_replied": acknowledged,
            "escalated": False, "suggested_reply": None,
            "booking_id": booking.id}


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
            if lead.gmail_thread_id:
                # Scoped to the exact conversation Julian started with this
                # lead — can't pick up unrelated mail that merely happens to
                # share the lead's sender address.
                candidates = reader.get_thread_messages(lead.gmail_thread_id)
            else:
                # No thread captured yet (message predates this feature, or
                # was sent before Google was connected) — fall back to the
                # old broad search.
                candidates = [reader.get_message(mid) for mid in
                             reader.list_message_ids(f"from:{lead.email} newer_than:14d")]
        except GmailError as exc:
            errors.append(f"lead {lead.id}: {exc}")
            continue
        for message in candidates:
            message_id = message["id"]
            exists = db.scalar(select(ConversationMessage).where(
                ConversationMessage.org_id == org.id,
                ConversationMessage.gmail_message_id == message_id))
            if exists:
                duplicates += 1
                continue
            if "SENT" in (message.get("label_ids") or []):
                # A thread contains both directions — this is Julian's own
                # outbound copy, not something the lead sent.
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
    from app.adapters.google_oauth import GoogleAccessRevoked, get_valid_access_token

    from app.services.subscription import active_orgs

    totals = {"processed": 0, "duplicates": 0, "errors": []}
    for org in active_orgs(db):
        credential = db.scalar(select(GoogleCredential).where(
            GoogleCredential.org_id == org.id))
        if credential is None or credential.broken:
            continue
        reader = GmailReaderAdapter(
            token_provider=lambda c=credential: get_valid_access_token(db, c),
            on_auth_error=lambda c=credential: get_valid_access_token(
                db, c, force_refresh=True))
        try:
            result = poll_replies(db, org, reader)
        except GoogleAccessRevoked:
            from app.services.sending import _notify_google_broken
            _notify_google_broken(db, org)
            continue
        totals["processed"] += result["processed"]
        totals["duplicates"] += result["duplicates"]
        totals["errors"].extend(result["errors"])
    return totals
