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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.security import EncryptedText


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeadState(str, enum.Enum):
    NEW = "NEW"
    SCORED = "SCORED"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    SEQUENCE_ACTIVE = "SEQUENCE_ACTIVE"
    ENGAGED = "ENGAGED"
    MEETING_PROPOSED = "MEETING_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    MEETING_CONFIRMED = "MEETING_CONFIRMED"
    NOT_INTERESTED = "NOT_INTERESTED"
    UNSUBSCRIBED = "UNSUBSCRIBED"


class BookingStatus(str, enum.Enum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MessageStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"  # permanently undeliverable (bounce or retry cap)


class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class ReplyCategory(str, enum.Enum):
    INTERESTED = "INTERESTED"
    QUESTION = "QUESTION"
    COMPLEX = "COMPLEX"
    NOT_INTERESTED = "NOT_INTERESTED"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"


class Organization(Base):
    """A tenant. Every lead, rule, booking, user, and credential belongs to one."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Human name Julian signs outreach with (the rep the lead should think
    # they're talking to). Defaults to the signup user's name.
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Where booking-approval notifications go for this tenant
    sales_rep_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    score_threshold: Mapped[float] = mapped_column(Float, default=50.0)
    # What this tenant sells — fed to the LLM so outreach is specific,
    # e.g. "We build payroll software for restaurants that cuts admin 80%"
    product_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Appended to every outgoing sequence email. Should carry the tenant's
    # opt-out line and postal address (CAN-SPAM/GDPR).
    email_footer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pre-approved answers Julian may draw on when replying to basic
    # questions. Questions not answerable from this text escalate to a human.
    knowledge_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IANA timezone for business-hours sending and slot proposals
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # Hard ceiling on sequence emails per day (a ramp-up applies below it)
    daily_send_cap: Mapped[int] = mapped_column(Integer, default=50)
    # When False (default), Julian never auto-sends knowledge-base answers —
    # they're delivered to the rep as suggested replies instead.
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # When True, Julian researches each lead (company site + news) before
    # writing and cites what it finds.
    research_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Stripe billing state (subscription_status mirrors Stripe's values;
    # "none" until the org completes checkout)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subscription_status: Mapped[str] = mapped_column(String(32), default="none")
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="organization")
    google_credential: Mapped["GoogleCredential | None"] = relationship(
        back_populates="organization", uselist=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(512))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="users")


class ApiKey(Base):
    """Bearer credential for API access. Only the SHA-256 hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))  # first chars, for display only

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoogleCredential(Base):
    """Per-tenant Google Calendar OAuth tokens (one connection per org)."""

    __tablename__ = "google_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True
    )
    refresh_token: Mapped[str] = mapped_column(EncryptedText)
    access_token: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    calendar_id: Mapped[str] = mapped_column(String(255), default="primary")
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set when Google refresh fails (access revoked/expired) so the agent
    # stops hammering the API and the customer is told to reconnect.
    broken: Mapped[bool] = mapped_column(Boolean, default=False)
    broken_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    broken_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship(back_populates="google_credential")


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_lead_org_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
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
    # Researched, LLM-distilled factual insights (company site + news) that
    # Julian may cite when writing; research_sources lists the URLs used.
    research_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    researched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

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
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(16))
    value: Mapped[dict | list | str | int | float] = mapped_column(JSON)
    weight: Mapped[float] = mapped_column(Float, default=10.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutreachMessage(Base):
    """One step of a lead's outreach sequence, stored as an approvable draft.

    Steps follow research-backed cadence: 1 = first touch (PAS), 2 = bump
    with proof (day 3), 3 = value-add (day 7), 4 = breakup (day 12).
    """

    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    send_after_days: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, native_enum=False, length=16), default=MessageStatus.DRAFT
    )
    spam_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Set at sequence activation: when this step becomes due to send
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Retry accounting: a send that errors is retried until this cap, then
    # marked FAILED so it can never loop forever.
    send_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lead: Mapped["Lead"] = relationship()


class OAuthState(Base):
    """Single-use, expiring state token for the Google OAuth flow.

    Prevents account-linking CSRF: a callback is only accepted for a state
    we minted for that org within the last few minutes, exactly once.
    """

    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SuppressedEmail(Base):
    """Per-org do-not-contact list. Opt-outs land here permanently and can
    never be re-imported or re-mailed by that org (CAN-SPAM/GDPR)."""

    __tablename__ = "suppressed_emails"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_suppress_org_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    reason: Mapped[str] = mapped_column(String(32), default="unsubscribed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationMessage(Base):
    """One email in a lead's conversation thread (inbound or outbound)."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("org_id", "gmail_message_id", name="uq_conv_gmail"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, native_enum=False, length=16)
    )
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    gmail_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # For inbound messages: how Julian triaged it, and what he suggested
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_reply: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped["Lead"] = relationship()


class PendingBooking(Base):
    """A meeting slot the lead picked, waiting for the sales rep's approval.

    The calendar event is created only when the booking is approved via the
    /approve_booking/{id} endpoint — never before.
    """

    __tablename__ = "pending_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
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
