"""Autopilot sending: sequence activation and the cadence send cycle.

Activation is the customer's one approval ("this sequence is good — go").
From then on the send cycle mails each step when due, exclusively while the
lead is still in SEQUENCE_ACTIVE — a reply (handled by the reply pipeline)
moves the lead out of that state and silences the rest of the sequence
automatically.
"""

import html as html_lib
import logging
import random
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.calendar import safe_zone

from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.gmail import GmailAuthError, GmailError, GmailSenderAdapter
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


def build_signature(org: Organization) -> tuple[str, str]:
    """Render the org's structured signature fields into (html, plain).

    Structured, not raw HTML the org pastes in — there's nothing to
    sanitize, and the output is always well-formed. Only non-empty fields
    produce a line; the company name always comes from org.name (the whole
    point is the email reads as genuinely from the organization).
    """
    name = (org.sender_name or "").strip()
    title = (org.signature_title or "").strip()
    phone = (org.signature_phone or "").strip()
    website = (org.signature_website or "").strip()
    company = (org.name or "").strip()
    role = f"{title} at {company}" if title and company else (title or company)

    plain_lines = [line for line in (name, role, phone, website) if line]
    plain = "\n\n--\n" + "\n".join(plain_lines) if plain_lines else ""

    html_lines = []
    if name:
        html_lines.append(f"<strong>{html_lib.escape(name)}</strong>")
    if role:
        html_lines.append(html_lib.escape(role))
    if phone:
        html_lines.append(html_lib.escape(phone))
    if website:
        url = website if website.startswith(("http://", "https://")) else f"https://{website}"
        html_lines.append(f'<a href="{html_lib.escape(url)}">{html_lib.escape(website)}</a>')
    if org.logo_image and org.logo_content_type:
        html_lines.append(f'<img src="cid:logo" alt="{html_lib.escape(company)}" '
                          'style="max-height:60px;margin-top:4px">')
    html = "<div>" + "<br>".join(html_lines) + "</div>" if html_lines else ""
    return html, plain


def _html_footer(footer: str) -> str:
    return f"<div>{html_lib.escape(footer).replace(chr(10), '<br>')}</div>" if footer else ""

# Deliverability guardrails: new orgs ramp up slowly (Gmail flags sudden
# volume from quiet accounts), and nothing sends outside local business hours.
RAMP_BASE_PER_DAY = 20
SEND_WINDOW = (8, 18)  # local hours
JITTER_MINUTES = 45
MAX_SEND_ATTEMPTS = 4  # after this a message is marked FAILED, not retried

# Substrings in a send error that mean the address is undeliverable — no
# point retrying, and the address should be suppressed.
HARD_BOUNCE_SIGNALS = (
    "550", "551", "553", "invalid recipient", "recipient rejected",
    "no such user", "user unknown", "mailbox unavailable",
    "address rejected", "does not exist", "recipient not found",
)


# CAN-SPAM (and the UK/EU equivalents) require every commercial email to
# carry a working opt-out and a real postal address. The old gate only
# checked the footer wasn't blank, which a single space satisfied — these
# phrases and the digit check are a heuristic for the two things the law
# actually requires. Not a substitute for the customer reading the actual
# rules, but it stops the most obvious way to launch non-compliant by
# accident.
FOOTER_OPTOUT_PHRASES = (
    "unsubscribe", "opt out", "opt-out", "opt-out", "no thanks",
    "stop emailing", "stop contacting", "remove me", "reply stop",
    "click here to unsubscribe", "to opt out", "to stop",
)


def footer_missing_requirements(footer: str) -> list[str]:
    """Return a human-readable list of what a footer is missing, or []."""
    lowered = footer.lower()
    missing = []
    if not any(p in lowered for p in FOOTER_OPTOUT_PHRASES):
        missing.append('an opt-out instruction (e.g. "reply STOP to unsubscribe")')
    if not re.search(r"\d", footer):
        missing.append("a postal address (needs at least a street number or postcode)")
    return missing


def _is_hard_bounce(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in HARD_BOUNCE_SIGNALS)


def effective_daily_cap(org: Organization) -> int:
    created = org.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    weeks_active = 0
    if created is not None:
        weeks_active = max(0, (datetime.now(timezone.utc) - created).days // 7)
    return min(org.daily_send_cap, RAMP_BASE_PER_DAY * (weeks_active + 1))


def _in_send_window(org: Organization, now: datetime) -> bool:
    from app.config import get_settings
    if not get_settings().enforce_send_window:
        return True
    local = now.astimezone(safe_zone(org.timezone))
    return local.weekday() < 5 and SEND_WINDOW[0] <= local.hour < SEND_WINDOW[1]


def _sent_today(db: Session, org: Organization, now: datetime) -> int:
    day_start = now.astimezone(safe_zone(org.timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count(OutreachMessage.id)).where(
        OutreachMessage.org_id == org.id,
        OutreachMessage.status == MessageStatus.SENT,
        OutreachMessage.sent_at >= day_start.astimezone(timezone.utc),
    )) or 0


def activate_sequence(db: Session, lead: Lead, org: Organization) -> list[OutreachMessage]:
    """Approve all drafts and schedule them; the lead goes on autopilot."""
    if lead.state != LeadState.OUTREACH_PENDING:
        raise SendingError(
            f"Lead must be OUTREACH_PENDING to activate its sequence "
            f"(currently {lead.state.value})"
        )
    if not lead.email:
        raise SendingError("Lead has no email address")
    footer = (org.email_footer or "").strip()
    if not footer:
        raise SendingError(
            "Set your email footer first (Settings): it must include an "
            "opt-out line and your postal address — required by anti-spam "
            "law (CAN-SPAM) before Julian can send on your behalf."
        )
    missing = footer_missing_requirements(footer)
    if missing:
        raise SendingError(
            "Your email footer is missing " + " and ".join(missing) + " — "
            "both are required by anti-spam law (CAN-SPAM and equivalents) "
            "before Julian can send on your behalf. Update it in Settings."
        )
    from app.services.suppression import is_suppressed
    if is_suppressed(db, org.id, lead.email):
        raise SendingError(
            f"{lead.email} previously opted out and cannot be contacted again."
        )

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
        # jitter so sends don't fire in machine-like exact intervals
        message.scheduled_at = (now + timedelta(days=message.send_after_days)
                                + timedelta(minutes=random.randint(0, JITTER_MINUTES)
                                            if message.send_after_days else 0))

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
            token_provider=lambda: get_valid_access_token(db, credential),
            # On a 401, force a refresh: that's what distinguishes a revoked
            # connection (raises GoogleAccessRevoked, marks it broken and
            # tells the customer to reconnect) from a token that merely
            # expired early.
            on_auth_error=lambda: get_valid_access_token(
                db, credential, force_refresh=True),
        )
    return EmailSenderAdapter()


def run_send_cycle(db: Session, org: Organization, sender=None) -> dict:
    """Send every due, approved message for one organization.

    Only leads still in SEQUENCE_ACTIVE receive mail; anything that left the
    state (replied, unsubscribed, booked) is skipped and the pending steps
    are marked SKIPPED so they can never fire later. Sends happen only in
    the org's local business hours and stop at the daily cap (ramp-aware).
    """
    now = utcnow()
    if not _in_send_window(org, now):
        return {"sent": 0, "skipped": 0, "errors": []}

    remaining_today = effective_daily_cap(org) - _sent_today(db, org, now)
    if remaining_today <= 0:
        return {"sent": 0, "skipped": 0, "errors": []}

    # FOR UPDATE SKIP LOCKED prevents double-sends when several workers run
    # the cycle concurrently (no-op on SQLite — run one worker there).
    due = db.scalars(
        select(OutreachMessage)
        .where(OutreachMessage.org_id == org.id,
               OutreachMessage.status == MessageStatus.APPROVED,
               OutreachMessage.scheduled_at <= now)
        .order_by(OutreachMessage.lead_id, OutreachMessage.step)
        .with_for_update(skip_locked=True)
    ).all()

    sent, skipped, errors = 0, 0, []
    if not due:
        return {"sent": 0, "skipped": 0, "errors": []}

    from app.adapters.google_oauth import GoogleAccessRevoked
    from app.services.suppression import is_suppressed, suppress_email

    try:
        sender = sender or get_outbound_sender(db, org)
    except GoogleAccessRevoked as exc:
        # Connection is broken; skip this org entirely until they reconnect.
        _notify_google_broken(db, org)
        return {"sent": 0, "skipped": 0, "errors": [f"google revoked: {exc}"]}
    footer = org.email_footer if org.email_footer is not None else DEFAULT_FOOTER
    sig_html = sig_text = None
    if org.email_signature_enabled:
        sig_html, sig_plain = build_signature(org)
        sig_text = (sig_plain or "") + (footer or "")
        sig_html = (sig_html or "") + _html_footer(footer or "")
        if not sig_html:
            sig_html = None  # nothing to render — stay on the plain-only path

    failed = 0
    for message in due:
        lead = db.get(Lead, message.lead_id)
        if (lead is None or lead.state != LeadState.SEQUENCE_ACTIVE
                or not lead.email or is_suppressed(db, org.id, lead.email)):
            message.status = MessageStatus.SKIPPED
            skipped += 1
            continue
        if sent >= remaining_today:
            break  # cap reached; the rest stays queued for tomorrow
        try:
            if sig_html is not None:
                sender.send(to=lead.email, subject=message.subject, body=message.body,
                            signature_html=sig_html, signature_text=sig_text,
                            logo_bytes=org.logo_image, logo_content_type=org.logo_content_type)
            else:
                sender.send(to=lead.email, subject=message.subject,
                            body=message.body + (footer or ""))
            # Capture the Gmail thread this outreach landed in so reply
            # polling can scope to it instead of a blanket sender search.
            if lead.gmail_thread_id is None:
                thread_id = getattr(sender, "last_thread_id", None)
                if thread_id:
                    lead.gmail_thread_id = thread_id
        except GoogleAccessRevoked as exc:
            _notify_google_broken(db, org)
            errors.append(f"google revoked mid-cycle: {exc}")
            break
        except GmailAuthError as exc:
            # A 401 survived the token refresh, so the connection is dead.
            # Retrying it would just burn send attempts and the customer
            # would never be told to reconnect.
            _mark_google_broken(db, org, "Gmail rejected the access token")
            _notify_google_broken(db, org)
            errors.append(f"google auth rejected: {exc}")
            break
        except (GmailError, OSError) as exc:
            message.send_attempts += 1
            message.last_error = str(exc)[:500]
            if _is_hard_bounce(str(exc)):
                # Undeliverable address: stop, mark failed, suppress it, and
                # end the lead's sequence.
                message.status = MessageStatus.FAILED
                db.flush()  # persist FAILED before retiring the rest (autoflush off)
                suppress_email(db, org.id, lead.email, "bounced")
                _retire_and_stop(db, lead)
                failed += 1
                logger.info("hard bounce for lead %s (%s); suppressed",
                            lead.id, lead.email)
            elif message.send_attempts >= MAX_SEND_ATTEMPTS:
                message.status = MessageStatus.FAILED
                failed += 1
                logger.warning("message %s failed after %d attempts: %s",
                               message.id, message.send_attempts, exc)
            else:
                errors.append(f"message {message.id} (lead {lead.id}): {exc}")
                logger.warning("send failed for message %s (attempt %d): %s",
                               message.id, message.send_attempts, exc)
            continue
        message.status = MessageStatus.SENT
        message.sent_at = utcnow()
        sent += 1

    db.commit()
    return {"sent": sent, "skipped": skipped, "failed": failed, "errors": errors}


def _retire_and_stop(db: Session, lead: Lead) -> None:
    """Bounce: stop the sequence and move the lead out of autopilot."""
    pending = db.scalars(select(OutreachMessage).where(
        OutreachMessage.lead_id == lead.id,
        OutreachMessage.status.in_([MessageStatus.APPROVED, MessageStatus.DRAFT]),
    )).all()
    for step in pending:
        step.status = MessageStatus.SKIPPED
    if lead.state == LeadState.SEQUENCE_ACTIVE:
        lead.state = LeadState.NOT_INTERESTED  # undeliverable == dead lead


def _mark_google_broken(db: Session, org: Organization, reason: str) -> None:
    """Flag the connection as unusable so the scheduler stops trying it."""
    credential = db.scalar(select(GoogleCredential).where(
        GoogleCredential.org_id == org.id))
    if credential is not None and not credential.broken:
        credential.broken = True
        credential.broken_reason = reason
        db.commit()


def _notify_google_broken(db: Session, org: Organization) -> None:
    """Tell the rep their Google connection needs reconnecting (once)."""
    credential = db.scalar(select(GoogleCredential).where(
        GoogleCredential.org_id == org.id))
    if credential is None or not credential.broken or credential.broken_notified:
        return
    credential.broken_notified = True
    db.commit()
    if not org.sales_rep_email:
        return
    EmailSenderAdapter().send(
        to=org.sales_rep_email,
        subject="Action needed: reconnect Google in Julian",
        body=(f"Julian can no longer access your Google account "
              f"({credential.account_email or 'connected account'}) — it "
              "looks like access was revoked or expired. Outreach and "
              "scheduling are paused for now.\n\n"
              "Reconnect from Settings -> Connect Google to resume."),
    )


def run_send_cycle_all_orgs(db: Session) -> dict:
    """Background-loop entry point: process every subscribed organization."""
    from app.services.subscription import active_orgs

    totals = {"sent": 0, "skipped": 0, "failed": 0, "errors": []}
    for org in active_orgs(db):
        result = run_send_cycle(db, org)
        totals["sent"] += result["sent"]
        totals["skipped"] += result["skipped"]
        totals["failed"] += result.get("failed", 0)
        totals["errors"].extend(result["errors"])
    return totals
