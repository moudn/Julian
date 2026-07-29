"""Run lead research and persist the results on the lead."""

import logging

from sqlalchemy import select
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
    # Persist the domain research actually resolved (lead.domain may have
    # been blank, with the domain only derived from the email) so a later
    # lead at the same company can be matched against it.
    if result.get("domain") and not lead.domain:
        lead.domain = result["domain"]
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _reuse_existing_company_research(db: Session, lead: Lead, org: Organization,
                                     researcher: LeadResearcher) -> Lead | None:
    """If another lead in this org at the same domain was already
    researched, copy those notes instead of re-fetching the same company's
    website/news — avoids burning search-API quota re-researching a company
    that already has a second contact imported (a common shape for a
    hand-picked target list: several people at the same firm)."""
    domain = researcher.domain_for(lead)
    if not domain:
        return None
    existing = db.scalar(
        select(Lead).where(
            Lead.org_id == org.id, Lead.domain == domain,
            Lead.id != lead.id, Lead.researched_at.isnot(None),
        ).order_by(Lead.researched_at.desc())
    )
    if existing is None:
        return None
    logger.info("reusing research for lead %s from lead %s (domain %s)",
               lead.id, existing.id, domain)
    lead.domain = lead.domain or domain
    lead.research_notes = existing.research_notes
    lead.research_sources = existing.research_sources
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
    reused = _reuse_existing_company_research(db, lead, org, researcher)
    if reused is not None:
        return reused
    return run_research(db, lead, org, researcher)
