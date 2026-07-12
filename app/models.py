import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeadState(str, enum.Enum):
    NEW = "NEW"
    SCORED = "SCORED"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    MEETING_PROPOSED = "MEETING_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    MEETING_CONFIRMED = "MEETING_CONFIRMED"


class BookingStatus(str, enum.Enum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="csv")

    state: Mapped[LeadState] = mapped_column(
        Enum(LeadState, native_enum=False, length=32), default=LeadState.NEW
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    outreach_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ISO-8601 datetimes offered to the lead while in MEETING_PROPOSED
    proposed_slots: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    bookings: Mapped[list["PendingBooking"]] = relationship(back_populates="lead")


class ICPRule(Base):
    """A single admin-defined ICP criterion.

    `field` names a Lead attribute (e.g. "title", "company_size", "location").
    `operator` is one of: equals, contains, in, gte, lte.
    `value` is the comparison value (a list for "in", a number for gte/lte).
    Matching rules add `weight` points to the lead's score.
    """

    __tablename__ = "icp_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(16))
    value: Mapped[dict | list | str | int | float] = mapped_column(JSON)
    weight: Mapped[float] = mapped_column(Float, default=10.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingBooking(Base):
    """A meeting slot the lead picked, waiting for the sales rep's approval.

    The calendar event is created only when the booking is approved via the
    /approve_booking/{id} endpoint — never before.
    """

    __tablename__ = "pending_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, native_enum=False, length=32),
        default=BookingStatus.AWAITING_APPROVAL,
    )
    rep_email: Mapped[str] = mapped_column(String(255))
    calendar_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="bookings")
