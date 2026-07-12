"""Lead lifecycle state machine.

NEW -> SCORED -> OUTREACH_PENDING -> MEETING_PROPOSED -> AWAITING_APPROVAL -> MEETING_CONFIRMED

Transitions are strictly forward along this path, with one exception:
AWAITING_APPROVAL may fall back to MEETING_PROPOSED when the rep rejects a
booking, so the lead can pick another slot.
"""

from app.models import Lead, LeadState

ALLOWED_TRANSITIONS: dict[LeadState, set[LeadState]] = {
    LeadState.NEW: {LeadState.SCORED},
    LeadState.SCORED: {LeadState.OUTREACH_PENDING},
    LeadState.OUTREACH_PENDING: {LeadState.MEETING_PROPOSED},
    LeadState.MEETING_PROPOSED: {LeadState.AWAITING_APPROVAL},
    LeadState.AWAITING_APPROVAL: {LeadState.MEETING_CONFIRMED, LeadState.MEETING_PROPOSED},
    LeadState.MEETING_CONFIRMED: set(),
}


class InvalidTransition(Exception):
    def __init__(self, current: LeadState, target: LeadState):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition lead from {current.value} to {target.value}")


def transition(lead: Lead, target: LeadState) -> Lead:
    if target not in ALLOWED_TRANSITIONS[lead.state]:
        raise InvalidTransition(lead.state, target)
    lead.state = target
    return lead
