from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.adapters.apollo import ApolloAdapter, ApolloError
from app.auth import get_current_org
from app.database import get_db
from app.deps import get_apollo_adapter
from app.models import Organization
from app.routers.billing import require_active_subscription
from app.schemas import ApolloEnrichRequest, ApolloSearchRequest, LeadOut
from app.services.leads import upsert_lead

router = APIRouter(prefix="/apollo", tags=["apollo"],
                   dependencies=[Depends(require_active_subscription)])


@router.post("/search_people")
def search_people(
    request: ApolloSearchRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    apollo: ApolloAdapter = Depends(get_apollo_adapter),
):
    """Search Apollo.io for people matching the filters.

    With save_to_db=true, matches are upserted as this org's Leads —
    stopping once the org's plan lead quota for this billing period is
    reached (re-enrichment of an already-saved lead never counts against
    it, since no new lead is added).
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
    quota_note = None
    if request.save_to_db:
        from app.services.billing_quota import lead_quota_remaining
        remaining = lead_quota_remaining(db, org)
        new_count = 0
        for person in people:
            if remaining is not None and new_count >= remaining:
                quota_note = ("This billing period's plan limit was reached — "
                              "the rest of these matches weren't saved. "
                              "Upgrade your plan to save more.")
                break
            lead, created = upsert_lead(db, person, org.id)
            saved_ids.append(lead.id)
            if created:
                new_count += 1
    result = {"count": len(people), "people": people, "saved_lead_ids": saved_ids}
    if quota_note:
        result["quota_note"] = quota_note
    return result


@router.post("/enrich_person", response_model=LeadOut)
def enrich_person(
    request: ApolloEnrichRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    apollo: ApolloAdapter = Depends(get_apollo_adapter),
):
    """Enrich a person by name + domain via Apollo and upsert this org's Lead."""
    from app.services.billing_quota import lead_quota_remaining
    remaining = lead_quota_remaining(db, org)
    if remaining is not None and remaining <= 0:
        raise HTTPException(status_code=402,
                            detail="This billing period's plan lead limit was reached. "
                                   "Upgrade your plan to add more leads.")
    try:
        data = apollo.enrich_person(request.name, request.domain)
    except ApolloError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    lead, _created = upsert_lead(db, data, org.id)
    return lead
