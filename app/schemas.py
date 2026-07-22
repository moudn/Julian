from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import BookingStatus, LeadState


# ---------- Leads ----------

class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str | None
    company: str | None
    title: str | None
    phone: str | None
    location: str | None
    linkedin_url: str | None
    domain: str | None
    company_size: int | None
    source: str
    state: LeadState
    score: float | None
    outreach_draft: str | None
    proposed_slots: list[str] | None
    research_notes: str | None
    research_sources: list[str] | None
    researched_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CSVImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]


# ---------- ICP rules ----------

class ICPRuleIn(BaseModel):
    name: str
    field: str
    operator: str = Field(pattern="^(equals|contains|in|gte|lte)$")
    value: str | int | float | list
    weight: float = 10.0
    active: bool = True


class ICPRuleOut(ICPRuleIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


class ScoreResult(BaseModel):
    lead_id: int
    score: float
    threshold: float
    state: LeadState


# ---------- Apollo ----------

class ApolloSearchRequest(BaseModel):
    titles: list[str] | None = None
    locations: list[str] | None = None
    organization_domains: list[str] | None = None
    keywords: str | None = None
    page: int = 1
    per_page: int = 10
    save_to_db: bool = False


class ApolloEnrichRequest(BaseModel):
    name: str
    domain: str


# ---------- Messages ----------

class MessageDraftOut(BaseModel):
    lead_id: int
    draft: str
    state: LeadState


class OutreachMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    step: int
    send_after_days: int
    subject: str
    body: str
    status: str
    spam_flags: list[str] | None
    created_at: datetime


class SequenceOut(BaseModel):
    lead_id: int
    state: LeadState
    messages: list[OutreachMessageOut]


# ---------- Scheduling ----------

class ProposeMeetingRequest(BaseModel):
    duration_minutes: int = 30
    slot_count: int = Field(default=3, ge=2, le=3)


class ProposedSlotsOut(BaseModel):
    lead_id: int
    slots: list[datetime]
    state: LeadState


class SelectSlotRequest(BaseModel):
    slot_start: datetime


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    slot_start: datetime
    slot_end: datetime
    status: BookingStatus
    rep_email: str
    calendar_event_id: str | None
    created_at: datetime
    decided_at: datetime | None
