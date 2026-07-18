from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.calendar import CalendarAdapter, CalendarError
from app.adapters.email_sender import EmailSenderAdapter
from app.auth import get_current_org
from app.database import get_db
from app.deps import get_calendar_adapter, get_email_sender
from app.models import BookingStatus, Lead, Organization, PendingBooking
from app.schemas import (
    BookingOut,
    ProposedSlotsOut,
    ProposeMeetingRequest,
    SelectSlotRequest,
)
from app.routers.billing import require_active_subscription
from app.services.schedule_manager import ScheduleError, ScheduleManager

router = APIRouter(tags=["scheduling"],
                   dependencies=[Depends(require_active_subscription)])


def get_schedule_manager(
    calendar: CalendarAdapter = Depends(get_calendar_adapter),
    email_sender: EmailSenderAdapter = Depends(get_email_sender),
    org: Organization = Depends(get_current_org),
) -> ScheduleManager:
    return ScheduleManager(calendar=calendar, email_sender=email_sender, org=org)


def _get_lead(db: Session, lead_id: int, org: Organization) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")
    return lead


def _get_booking(db: Session, booking_id: int, org: Organization) -> PendingBooking:
    booking = db.get(PendingBooking, booking_id)
    if booking is None or booking.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")
    return booking


@router.post("/leads/{lead_id}/propose_meeting", response_model=ProposedSlotsOut)
def propose_meeting(
    lead_id: int,
    request: ProposeMeetingRequest = ProposeMeetingRequest(),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    manager: ScheduleManager = Depends(get_schedule_manager),
):
    lead = _get_lead(db, lead_id, org)
    try:
        slots = manager.propose_meeting(
            db, lead,
            duration_minutes=request.duration_minutes,
            slot_count=request.slot_count,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ProposedSlotsOut(lead_id=lead.id, slots=slots, state=lead.state)


@router.post("/leads/{lead_id}/select_slot", response_model=BookingOut)
def select_slot(
    lead_id: int,
    request: SelectSlotRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    manager: ScheduleManager = Depends(get_schedule_manager),
):
    """The lead picked a slot. Creates a PendingBooking and notifies the rep.

    No calendar event is created here — that requires rep approval.
    """
    lead = _get_lead(db, lead_id, org)
    try:
        booking = manager.select_slot(db, lead, request.slot_start)
    except ScheduleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return booking


@router.get("/bookings/pending", response_model=list[BookingOut])
def list_pending_bookings(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Simple approval dashboard for the sales rep."""
    return db.scalars(
        select(PendingBooking)
        .where(PendingBooking.status == BookingStatus.AWAITING_APPROVAL,
               PendingBooking.org_id == org.id)
        .order_by(PendingBooking.created_at)
    ).all()


@router.post("/approve_booking/{booking_id}", response_model=BookingOut)
def approve_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    manager: ScheduleManager = Depends(get_schedule_manager),
):
    """Explicit human approval — the only endpoint that creates a calendar event."""
    booking = _get_booking(db, booking_id, org)
    try:
        return manager.approve_booking(db, booking)
    except ScheduleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/bookings/{booking_id}/reject", response_model=BookingOut)
def reject_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    manager: ScheduleManager = Depends(get_schedule_manager),
):
    booking = _get_booking(db, booking_id, org)
    try:
        return manager.reject_booking(db, booking)
    except ScheduleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
