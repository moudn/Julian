from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.llm import LLMError, OpenRouterAdapter
from app.config import get_settings
from app.database import get_db
from app.deps import get_llm_adapter
from app.models import Lead, LeadState
from app.schemas import CSVImportResult, LeadOut, MessageDraftOut, ScoreResult
from app.services.leads import import_leads_csv
from app.services.scoring import score_lead
from app.state_machine import transition

router = APIRouter(prefix="/leads", tags=["leads"])


def _get_lead(db: Session, lead_id: int) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")
    return lead


@router.post("/import", response_model=CSVImportResult)
async def import_csv(file: UploadFile, db: Session = Depends(get_db)):
    if file.filename and not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Expected a .csv file")
    imported, skipped, errors = import_leads_csv(db, await file.read())
    return CSVImportResult(imported=imported, skipped=skipped, errors=errors)


@router.get("", response_model=list[LeadOut])
def list_leads(state: LeadState | None = None, db: Session = Depends(get_db)):
    query = select(Lead).order_by(Lead.id)
    if state is not None:
        query = query.where(Lead.state == state)
    return db.scalars(query).all()


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: int, db: Session = Depends(get_db)):
    return _get_lead(db, lead_id)


@router.post("/{lead_id}/score", response_model=ScoreResult)
def score(lead_id: int, db: Session = Depends(get_db)):
    lead = score_lead(db, _get_lead(db, lead_id))
    return ScoreResult(
        lead_id=lead.id, score=lead.score, threshold=get_settings().score_threshold,
        state=lead.state,
    )


@router.post("/score_all", response_model=list[ScoreResult])
def score_all(db: Session = Depends(get_db)):
    threshold = get_settings().score_threshold
    results = []
    for lead in db.scalars(select(Lead).where(Lead.state == LeadState.NEW)).all():
        lead = score_lead(db, lead)
        results.append(ScoreResult(lead_id=lead.id, score=lead.score,
                                   threshold=threshold, state=lead.state))
    return results


@router.post("/{lead_id}/generate_message", response_model=MessageDraftOut)
def generate_message(
    lead_id: int,
    db: Session = Depends(get_db),
    llm: OpenRouterAdapter = Depends(get_llm_adapter),
):
    lead = _get_lead(db, lead_id)
    if lead.state != LeadState.SCORED:
        raise HTTPException(
            status_code=409,
            detail=f"Lead must be in SCORED state to generate outreach "
                   f"(currently {lead.state.value})",
        )
    try:
        draft = llm.generate_first_touch_email(lead)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    lead.outreach_draft = draft
    transition(lead, LeadState.OUTREACH_PENDING)
    db.commit()
    return MessageDraftOut(lead_id=lead.id, draft=draft, state=lead.state)
