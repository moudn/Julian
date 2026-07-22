"""Run lead research and persist the results on the lead."""

import logging

from sqlalchemy.orm import Session

from app.adapters.research import LeadResearcher
from app.config import get_settings
from app.models import Lead, Organization, utcnow

logger = logging.getLogger(__name__)


def run_research(db: Session, lead: Lead, org: Organization,
                 researcher: LeadResearcher) -> Lead:
    """Research a lead and store the distilled notes + sources. Best-effort:
    on any failure the lead is left unchanged (notes stay None)."""
    try:
        result = researcher.research(lead, org)
    except Exception as exc:  # never let research break the caller
        logger.warning("research failed for lead %s: %s", lead.id, exc)
        return lead
    lead.research_notes = result["notes"] or None
    lead.research_sources = result["sources"] or None
    lead.researched_at = utcnow()
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def maybe_research(db: Session, lead: Lead, org: Organization,
                   researcher: LeadResearcher | None) -> Lead:
    """Research once before writing, if enabled globally and for the org and
    not already done."""
    if researcher is None or not get_settings().research_enabled:
        return lead
    if not org.research_enabled or lead.researched_at is not None:
        return lead
    return run_research(db, lead, org, researcher)
