from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.gmail import GmailReaderAdapter
from app.adapters.google_oauth import get_valid_access_token
from app.adapters.llm import OpenRouterAdapter
from app.auth import get_current_org
from app.database import get_db
from app.deps import get_email_sender, get_llm_adapter
from app.models import ConversationMessage, GoogleCredential, Lead, Organization
from app.routers.billing import require_active_subscription
from app.services.replies import ingest_reply, poll_replies

router = APIRouter(tags=["replies"],
                   dependencies=[Depends(require_active_subscription)])


class ReplyIngestIn(BaseModel):
    lead_id: int
    body: str
    subject: str | None = None


class ReplyIngestOut(BaseModel):
    status: str
    category: str | None
    lead_state: str | None = None
    auto_replied: bool = False
    escalated: bool = False
    suggested_reply: str | None = None


class PollOut(BaseModel):
    processed: int
    duplicates: int
    errors: list[str]


class ConversationMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    subject: str | None
    body: str
    category: str | None
    suggested_reply: str | None
    created_at: datetime


def _get_lead(db: Session, lead_id: int, org: Organization) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")
    return lead


@router.post("/replies/ingest", response_model=ReplyIngestOut)
def ingest(
    request: ReplyIngestIn,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    llm: OpenRouterAdapter = Depends(get_llm_adapter),
    notifier: EmailSenderAdapter = Depends(get_email_sender),
):
    """Feed one inbound reply through triage.

    Used by tests, manual workflows, and any non-Gmail inbound source
    (e.g. a mail-provider webhook).
    """
    lead = _get_lead(db, request.lead_id, org)
    result = ingest_reply(db, lead, org, body=request.body,
                          subject=request.subject, llm=llm, notifier=notifier)
    return ReplyIngestOut(**result)


@router.post("/replies/poll", response_model=PollOut)
def poll(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    llm: OpenRouterAdapter = Depends(get_llm_adapter),
    notifier: EmailSenderAdapter = Depends(get_email_sender),
):
    """Check the connected Gmail for new replies from active leads now.

    The background loop does this automatically for connected orgs.
    """
    credential = db.scalar(select(GoogleCredential).where(
        GoogleCredential.org_id == org.id))
    if credential is None:
        raise HTTPException(status_code=409,
                            detail="Google is not connected for this organization")
    reader = GmailReaderAdapter(
        token_provider=lambda: get_valid_access_token(db, credential))
    return PollOut(**poll_replies(db, org, reader, llm=llm, notifier=notifier))


@router.get("/leads/{lead_id}/conversation",
            response_model=list[ConversationMessageOut])
def conversation(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    lead = _get_lead(db, lead_id, org)
    return db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.lead_id == lead.id)
        .order_by(ConversationMessage.created_at, ConversationMessage.id)
    ).all()
