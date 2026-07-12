from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.adapters.apollo import ApolloAdapter, ApolloError
from app.database import get_db
from app.deps import get_apollo_adapter
from app.schemas import ApolloEnrichRequest, ApolloSearchRequest, LeadOut
from app.services.leads import upsert_lead

router = APIRouter(prefix="/apollo", tags=["apollo"])


@router.post("/search_people")
def search_people(
    request: ApolloSearchRequest,
    db: Session = Depends(get_db),
    apollo: ApolloAdapter = Depends(get_apollo_adapter),
):
    """Search Apollo.io for people matching the filters.

    With save_to_db=true, matches are upserted as Leads.
    """
    try:
        people = apollo.search_people(
            titles=request.titles,
            locations=request.locations,
            organization_domains=request.organization_domains,
            keywords=request.keywords,
            page=request.page,
            per_page=request.per_page,
        )
    except ApolloError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    saved_ids = []
    if request.save_to_db:
        saved_ids = [upsert_lead(db, person).id for person in people]
    return {"count": len(people), "people": people, "saved_lead_ids": saved_ids}


@router.post("/enrich_person", response_model=LeadOut)
def enrich_person(
    request: ApolloEnrichRequest,
    db: Session = Depends(get_db),
    apollo: ApolloAdapter = Depends(get_apollo_adapter),
):
    """Enrich a person by name + domain via Apollo and upsert the Lead."""
    try:
        data = apollo.enrich_person(request.name, request.domain)
    except ApolloError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return upsert_lead(db, data)
