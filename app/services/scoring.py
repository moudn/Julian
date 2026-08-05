"""Rule-based ICP scoring, optionally blended with an LLM-judged fit score.

Each active ICPRule is evaluated against the lead; matching rules add their
weight to the score (a negative weight penalizes rather than qualifies). If
the org has AI fit scoring enabled, an LLM judgment (0-100) contributes up
to org.ai_fit_weight additional points on top of that. A lead in NEW whose
total score reaches the threshold moves to SCORED.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ICPRule, Lead, LeadState, Organization
from app.state_machine import transition

logger = logging.getLogger(__name__)


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


def _active_rules(db: Session, org: Organization):
    return db.scalars(select(ICPRule).where(
        ICPRule.active.is_(True), ICPRule.org_id == org.id)).all()


def _apply_score(lead: Lead, org: Organization, rules, ai_score: int | None,
                 force: bool) -> None:
    """Set score/state on one lead. No I/O — caller owns the transaction."""
    total = sum(rule.weight for rule in rules if _rule_matches(rule, lead))
    lead.ai_fit_score = ai_score
    if ai_score is not None:
        total += round(ai_score / 100 * org.ai_fit_weight)
    lead.score = total
    if lead.state == LeadState.NEW and (lead.score >= org.score_threshold or force):
        transition(lead, LeadState.SCORED)


def score_lead(db: Session, lead: Lead, org: Organization, force: bool = False,
               llm=None) -> Lead:
    rules = _active_rules(db, org)
    ai_score = None
    if org.ai_fit_scoring_enabled and llm is not None:
        ai_score = llm.score_fit(lead, org)
    _apply_score(lead, org, rules, ai_score, force)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


# How many fit-score calls run at once. The work is entirely network-bound
# (one small OpenRouter round-trip each), so threads are the right tool;
# the ceiling keeps a 300-lead Scale account from opening 300 sockets.
FIT_SCORE_CONCURRENCY = 12


def score_leads(db: Session, leads: list[Lead], org: Organization,
                force: bool = False, llm=None) -> list[Lead]:
    """Score many leads in one transaction.

    Scoring one lead at a time meant re-reading the ICP rules and issuing a
    commit per lead, and — with AI fit scoring on — one blocking LLM call
    per lead in series. At a Scale account's 300 leads that put the request
    into the minutes, past every host's timeout. The rules are read once,
    the fit calls are fanned out, and everything commits together.
    """
    if not leads:
        return []
    rules = _active_rules(db, org)

    ai_scores: dict[int, int | None] = {}
    if org.ai_fit_scoring_enabled and llm is not None:
        # Build every prompt here, on the thread that owns the Session:
        # reading lead attributes can emit lazy-load SQL, which is not safe
        # from a worker.
        contexts = [(lead.id, llm.fit_context(lead, org)) for lead in leads]
        with ThreadPoolExecutor(max_workers=FIT_SCORE_CONCURRENCY) as pool:
            futures = {
                pool.submit(llm.score_fit_context, context, lead_id): lead_id
                for lead_id, context in contexts
            }
            for future in as_completed(futures):
                lead_id = futures[future]
                try:
                    ai_scores[lead_id] = future.result()
                except Exception:  # one bad lead must not sink the batch
                    logger.exception("fit scoring failed for lead %s", lead_id)
                    ai_scores[lead_id] = None

    for lead in leads:
        _apply_score(lead, org, rules, ai_scores.get(lead.id), force)
    db.commit()
    return leads
