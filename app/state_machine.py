"""Lead lifecycle state machine.

Main path:
NEW -> SCORED -> OUTREACH_PENDING -> SEQUENCE_ACTIVE -> ENGAGED
    -> MEETING_PROPOSED -> AWAITING_APPROVAL -> MEETING_CONFIRMED

OUTREACH_PENDING holds generated drafts awaiting activation. SEQUENCE_ACTIVE
is autopilot: the scheduler sends steps on cadence. ENGAGED means the lead
replied and a human owns the conversation (Julian assists). A meeting can be
proposed from either OUTREACH_PENDING (manual flow), SEQUENCE_ACTIVE, or
ENGAGED.

AWAITING_APPROVAL may fall back to MEETING_PROPOSED when the rep rejects a
booking. NOT_INTERESTED and UNSUBSCRIBED are terminal: the scheduler must
never send to leads in those states.
"""

from app.models import Lead, LeadState

ALLOWED_TRANSITIONS: dict[LeadState, set[LeadState]] = {
    LeadState.NEW: {LeadState.SCORED},
    LeadState.SCORED: {LeadState.OUTREACH_PENDING},
    LeadState.OUTREACH_PENDING: {
        LeadState.SEQUENCE_ACTIVE,
        LeadState.MEETING_PROPOSED,
    },
    LeadState.SEQUENCE_ACTIVE: {
        LeadState.ENGAGED,
        LeadState.MEETING_PROPOSED,
        LeadState.NOT_INTERESTED,
        LeadState.UNSUBSCRIBED,
    },
    LeadState.ENGAGED: {
        LeadState.MEETING_PROPOSED,
        LeadState.NOT_INTERESTED,
        LeadState.UNSUBSCRIBED,
    },
    LeadState.MEETING_PROPOSED: {LeadState.AWAITING_APPROVAL},
    LeadState.AWAITING_APPROVAL: {LeadState.MEETING_CONFIRMED, LeadState.MEETING_PROPOSED},
    LeadState.MEETING_CONFIRMED: set(),
    LeadState.NOT_INTERESTED: set(),
    LeadState.UNSUBSCRIBED: set(),
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
