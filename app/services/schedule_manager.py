"""Human-in-the-loop scheduling workflow.

    propose_meeting  -> agent reads the rep's calendar and offers 2-3 slots
                        (lead state: MEETING_PROPOSED)
    select_slot      -> the lead picks a slot; a PendingBooking is created and
                        the rep is notified (lead state: AWAITING_APPROVAL)
    approve_booking  -> ONLY here is the calendar event created and the
                        confirmation email sent (lead state: MEETING_CONFIRMED)
    reject_booking   -> booking is rejected, lead returns to MEETING_PROPOSED

The calendar's create_event is called exclusively inside approve_booking, so
no event can ever exist without an explicit approval.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.adapters.calendar import CalendarAdapter, safe_zone
from app.adapters.email_sender import EmailSenderAdapter
from app.models import BookingStatus, Lead, LeadState, Organization, PendingBooking
from app.state_machine import transition


class ScheduleError(Exception):
    pass


class ScheduleManager:
    def __init__(self, calendar: CalendarAdapter, email_sender: EmailSenderAdapter,
                 org: Organization):
        self.calendar = calendar
        self.email_sender = email_sender
        self.org = org

    def _fmt(self, moment: datetime) -> str:
        """Format a time in the org's timezone with an explicit zone label."""
        zone = safe_zone(self.org.timezone)
        local = moment.astimezone(zone)
        return f"{local.strftime('%A %B %d, %H:%M')} {local.tzname() or self.org.timezone}"

    def propose_meeting(self, db: Session, lead: Lead,
                        duration_minutes: int = 30, slot_count: int = 3) -> list[datetime]:
        """Find free slots on the rep's calendar and offer them to the lead."""
        allowed = (LeadState.OUTREACH_PENDING, LeadState.SEQUENCE_ACTIVE,
                   LeadState.ENGAGED)
        if lead.state not in allowed:
            raise ScheduleError(
                f"Lead must be in OUTREACH_PENDING, SEQUENCE_ACTIVE, or "
                f"ENGAGED to propose a meeting (currently {lead.state.value})"
            )

        slots = self.calendar.find_available_slots(
            duration_minutes, slot_count, tz_name=self.org.timezone)
        if len(slots) < 2:
            raise ScheduleError("Could not find at least 2 available slots on the calendar")

        lead.proposed_slots = [start.isoformat() for start, _ in slots]
        transition(lead, LeadState.MEETING_PROPOSED)
        db.commit()

        if lead.email:
            slot_lines = "\n".join(
                f"  {index}. {self._fmt(start)} ({duration_minutes} min)"
                for index, (start, _) in enumerate(slots, start=1)
            )
            self.email_sender.send(
                to=lead.email,
                subject="A few times that could work for our call",
                body=(
                    f"Hi {lead.name.split()[0]},\n\n"
                    f"Here are a few times that work on our side:\n\n{slot_lines}\n\n"
                    "Reply with the option that suits you and we'll get it confirmed.\n\n"
                    "Best regards"
                ),
            )
        return [start for start, _ in slots]

    def select_slot(self, db: Session, lead: Lead, slot_start: datetime,
                    duration_minutes: int = 30) -> PendingBooking:
        """Record the lead's chosen slot as a PendingBooking and notify the rep.

        Deliberately does NOT touch the calendar.
        """
        if lead.state != LeadState.MEETING_PROPOSED:
            raise ScheduleError(
                f"Lead must be in MEETING_PROPOSED to select a slot "
                f"(currently {lead.state.value})"
            )

        if slot_start.tzinfo is None:
            slot_start = slot_start.replace(tzinfo=timezone.utc)
        proposed = {datetime.fromisoformat(s) for s in (lead.proposed_slots or [])}
        if slot_start not in proposed:
            raise ScheduleError("Selected time is not one of the proposed slots")

        rep_email = self.org.sales_rep_email
        if not rep_email:
            raise ScheduleError(
                "No sales_rep_email configured for this organization "
                "(set it via PATCH /auth/org)"
            )
        booking = PendingBooking(
            org_id=self.org.id,
            lead_id=lead.id,
            slot_start=slot_start,
            slot_end=slot_start + timedelta(minutes=duration_minutes),
            status=BookingStatus.AWAITING_APPROVAL,
            rep_email=rep_email,
        )
        db.add(booking)
        transition(lead, LeadState.AWAITING_APPROVAL)
        db.commit()
        db.refresh(booking)

        self.email_sender.send(
            to=booking.rep_email,
            subject=f"Approval needed: meeting with {lead.name}",
            body=(
                f"{lead.name} ({lead.company or 'unknown company'}) picked "
                f"{self._fmt(slot_start)}.\n\n"
                f"Approve with: POST /approve_booking/{booking.id}\n"
                f"Reject with:  POST /bookings/{booking.id}/reject\n\n"
                "No calendar event has been created yet."
            ),
        )
        return booking

    def approve_booking(self, db: Session, booking: PendingBooking) -> PendingBooking:
        """Rep approval: the ONLY code path that creates a calendar event."""
        if booking.status != BookingStatus.AWAITING_APPROVAL:
            raise ScheduleError(
                f"Booking {booking.id} is {booking.status.value}, not awaiting approval"
            )
        lead = booking.lead
        if lead.state != LeadState.AWAITING_APPROVAL:
            raise ScheduleError(
                f"Lead must be in AWAITING_APPROVAL to confirm (currently {lead.state.value})"
            )

        # The slot was free at proposal time; things change — re-check before
        # putting it on the calendar. (SQLite returns naive datetimes.)
        slot_start = booking.slot_start
        slot_end = booking.slot_end
        if slot_start.tzinfo is None:
            slot_start = slot_start.replace(tzinfo=timezone.utc)
        if slot_end.tzinfo is None:
            slot_end = slot_end.replace(tzinfo=timezone.utc)
        if not self.calendar.is_slot_free(slot_start, slot_end):
            raise ScheduleError(
                "That slot is no longer free on the calendar. Reject this "
                "booking so the lead can pick another time."
            )

        attendees = [booking.rep_email] + ([lead.email] if lead.email else [])
        event_id = self.calendar.create_event(
            summary=f"Intro call: {lead.name} ({lead.company or 'n/a'})",
            start=booking.slot_start,
            end=booking.slot_end,
            attendee_emails=attendees,
            description=f"Scheduled by AI sales agent for lead #{lead.id}.",
        )

        booking.calendar_event_id = event_id
        booking.status = BookingStatus.APPROVED
        booking.decided_at = datetime.now(timezone.utc)
        transition(lead, LeadState.MEETING_CONFIRMED)
        db.commit()
        db.refresh(booking)

        if lead.email:
            self.email_sender.send(
                to=lead.email,
                subject="Your meeting is confirmed",
                body=(
                    f"Hi {lead.name.split()[0]},\n\n"
                    f"Your call is confirmed for "
                    f"{self._fmt(booking.slot_start)}. "
                    "A calendar invitation is on its way.\n\nSee you then!"
                ),
            )
        return booking

    def reject_booking(self, db: Session, booking: PendingBooking) -> PendingBooking:
        """Rep rejection: lead goes back to MEETING_PROPOSED to pick again."""
        if booking.status != BookingStatus.AWAITING_APPROVAL:
            raise ScheduleError(
                f"Booking {booking.id} is {booking.status.value}, not awaiting approval"
            )
        booking.status = BookingStatus.REJECTED
        booking.decided_at = datetime.now(timezone.utc)
        transition(booking.lead, LeadState.MEETING_PROPOSED)
        db.commit()
        db.refresh(booking)
        return booking
