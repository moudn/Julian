"""Rule-based ICP scoring, optionally blended with an LLM-judged fit score.

Each active ICPRule is evaluated against the lead; matching rules add their
weight to the score (a negative weight penalizes rather than qualifies). If
the org has AI fit scoring enabled, an LLM judgment (0-100) contributes up
to org.ai_fit_weight additional points on top of that. A lead in NEW whose
total score reaches the threshold moves to SCORED.
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


def score_lead(db: Session, lead: Lead, org: Organization, force: bool = False,
               llm=None) -> Lead:
    rules = db.scalars(select(ICPRule).where(
        ICPRule.active.is_(True), ICPRule.org_id == org.id)).all()
    total = sum(rule.weight for rule in rules if _rule_matches(rule, lead))

    ai_score = None
    if org.ai_fit_scoring_enabled and llm is not None:
        ai_score = llm.score_fit(lead, org)
    lead.ai_fit_score = ai_score
    if ai_score is not None:
        total += round(ai_score / 100 * org.ai_fit_weight)
    lead.score = total

    threshold = org.score_threshold
    if lead.state == LeadState.NEW and (lead.score >= threshold or force):
        transition(lead, LeadState.SCORED)

    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead
