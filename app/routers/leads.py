from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.llm import LLMError, OpenRouterAdapter
from app.auth import get_current_org
from app.database import get_db
from app.deps import get_llm_adapter
from app.models import Lead, LeadState, Organization, OutreachMessage
from app.schemas import (
    CSVImportResult,
    LeadOut,
    MessageDraftOut,
    OutreachMessageOut,
    ScoreResult,
    SequenceOut,
)
from app.routers.billing import require_active_subscription
from app.services.leads import import_leads_csv
from app.services.outreach import OutreachError, generate_sequence
from app.services.scoring import score_lead
from app.services.sending import SendingError, activate_sequence
from app.state_machine import transition

router = APIRouter(prefix="/leads", tags=["leads"],
                   dependencies=[Depends(require_active_subscription)])


def _get_lead(db: Session, lead_id: int, org: Organization) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")
    return lead


@router.post("/import", response_model=CSVImportResult)
async def import_csv(
    file: UploadFile,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    if file.filename and not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Expected a .csv file")
    imported, skipped, errors = import_leads_csv(db, await file.read(), org.id)
    return CSVImportResult(imported=imported, skipped=skipped, errors=errors)


@router.get("", response_model=list[LeadOut])
def list_leads(
    state: LeadState | None = None,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    query = select(Lead).where(Lead.org_id == org.id).order_by(Lead.id)
    if state is not None:
        query = query.where(Lead.state == state)
    return db.scalars(query).all()


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    return _get_lead(db, lead_id, org)


@router.post("/{lead_id}/score", response_model=ScoreResult)
def score(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    lead = score_lead(db, _get_lead(db, lead_id, org), org)
    return ScoreResult(lead_id=lead.id, score=lead.score,
                       threshold=org.score_threshold, state=lead.state)


@router.post("/score_all", response_model=list[ScoreResult])
def score_all(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    results = []
    leads = db.scalars(select(Lead).where(
        Lead.state == LeadState.NEW, Lead.org_id == org.id)).all()
    for lead in leads:
        lead = score_lead(db, lead, org)
        results.append(ScoreResult(lead_id=lead.id, score=lead.score,
                                   threshold=org.score_threshold, state=lead.state))
    return results


@router.post("/{lead_id}/generate_message", response_model=MessageDraftOut)
def generate_message(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    llm: OpenRouterAdapter = Depends(get_llm_adapter),
):
    lead = _get_lead(db, lead_id, org)
    if lead.state != LeadState.SCORED:
        raise HTTPException(
            status_code=409,
            detail=f"Lead must be in SCORED state to generate outreach "
                   f"(currently {lead.state.value})",
        )
    try:
        draft = llm.generate_first_touch_email(lead, org)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    lead.outreach_draft = draft
    transition(lead, LeadState.OUTREACH_PENDING)
    db.commit()
    return MessageDraftOut(lead_id=lead.id, draft=draft, state=lead.state)


@router.post("/{lead_id}/generate_sequence", response_model=SequenceOut)
def generate_outreach_sequence(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    llm: OpenRouterAdapter = Depends(get_llm_adapter),
):
    """Generate the full research-backed 4-step sequence as drafts.

    Step 1 first touch (PAS), step 2 bump with proof (day 3), step 3
    value-add (day 7), step 4 breakup (day 12). Regenerating replaces unsent
    drafts only.
    """
    lead = _get_lead(db, lead_id, org)
    try:
        messages = generate_sequence(db, lead, org, llm)
    except OutreachError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SequenceOut(
        lead_id=lead.id, state=lead.state,
        messages=[OutreachMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{lead_id}/activate_sequence", response_model=SequenceOut)
def activate_outreach_sequence(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """The customer's one approval: arm the whole sequence for autopilot.

    Step 1 becomes due immediately; follow-ups are scheduled on cadence.
    Sending stops automatically if the lead leaves SEQUENCE_ACTIVE.
    """
    lead = _get_lead(db, lead_id, org)
    try:
        messages = activate_sequence(db, lead, org)
    except SendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SequenceOut(
        lead_id=lead.id, state=lead.state,
        messages=[OutreachMessageOut.model_validate(m) for m in messages],
    )


@router.get("/{lead_id}/sequence", response_model=SequenceOut)
def get_outreach_sequence(
    lead_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    lead = _get_lead(db, lead_id, org)
    messages = db.scalars(
        select(OutreachMessage)
        .where(OutreachMessage.lead_id == lead.id)
        .order_by(OutreachMessage.step)
    ).all()
    return SequenceOut(
        lead_id=lead.id, state=lead.state,
        messages=[OutreachMessageOut.model_validate(m) for m in messages],
    )
