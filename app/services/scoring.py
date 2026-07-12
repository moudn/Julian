"""Rule-based ICP scoring.

Each active ICPRule is evaluated against the lead; matching rules add their
weight to the score. A lead in NEW whose score reaches the threshold moves
to SCORED.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ICPRule, Lead, LeadState, Organization
from app.state_machine import transition


def _rule_matches(rule: ICPRule, lead: Lead) -> bool:
    lead_value = getattr(lead, rule.field, None)
    if lead_value is None:
        return False

    op, expected = rule.operator, rule.value
    if op == "equals":
        return str(lead_value).lower() == str(expected).lower()
    if op == "contains":
        return str(expected).lower() in str(lead_value).lower()
    if op == "in":
        options = expected if isinstance(expected, list) else [expected]
        return any(str(option).lower() in str(lead_value).lower() for option in options)
    if op == "gte":
        try:
            return float(lead_value) >= float(expected)
        except (TypeError, ValueError):
            return False
    if op == "lte":
        try:
            return float(lead_value) <= float(expected)
        except (TypeError, ValueError):
            return False
    return False


def score_lead(db: Session, lead: Lead, org: Organization) -> Lead:
    rules = db.scalars(select(ICPRule).where(
        ICPRule.active.is_(True), ICPRule.org_id == org.id)).all()
    lead.score = sum(rule.weight for rule in rules if _rule_matches(rule, lead))

    threshold = org.score_threshold
    if lead.state == LeadState.NEW and lead.score >= threshold:
        transition(lead, LeadState.SCORED)

    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead
