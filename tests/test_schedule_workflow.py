"""End-to-end test of the human-approved scheduling workflow.

The invariant under test: no calendar event is ever created until the
/approve_booking/{id} endpoint is called explicitly.
"""

import io


def _lead_ready_for_scheduling(client) -> int:
    """Import a lead and walk it to OUTREACH_PENDING."""
    csv = "name,email,company,title,company_size\nAda Lovelace,ada@acme.io,Acme,VP of Engineering,250\n"
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(csv.encode()), "text/csv")})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    lead_id = 1
    assert client.post(f"/leads/{lead_id}/score").json()["state"] == "SCORED"
    assert client.post(f"/leads/{lead_id}/generate_message").json()["state"] == "OUTREACH_PENDING"
    return lead_id


def test_full_workflow_event_created_only_after_approval(client, calendar, email_sender):
    lead_id = _lead_ready_for_scheduling(client)

    # 1. Agent proposes slots from the rep's calendar
    response = client.post(f"/leads/{lead_id}/propose_meeting",
                           json={"duration_minutes": 30, "slot_count": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "MEETING_PROPOSED"
    assert 2 <= len(body["slots"]) <= 3
    assert calendar.events == []  # nothing booked

    # Lead received the proposal email
    assert any(m["to"] == "ada@acme.io" for m in email_sender.sent)

    # 2. Lead picks a slot -> PendingBooking, still NO calendar event
    chosen = body["slots"][0]
    response = client.post(f"/leads/{lead_id}/select_slot", json={"slot_start": chosen})
    assert response.status_code == 200
    booking = response.json()
    assert booking["status"] == "AWAITING_APPROVAL"
    assert booking["calendar_event_id"] is None
    assert calendar.events == []  # CRUCIAL: no event before approval

    lead = client.get(f"/leads/{lead_id}").json()
    assert lead["state"] == "AWAITING_APPROVAL"

    # Rep was notified and the booking shows on the pending dashboard
    assert any(m["to"] == "rep@example.com" and "Approval needed" in m["subject"]
               for m in email_sender.sent)
    pending = client.get("/bookings/pending").json()
    assert [b["id"] for b in pending] == [booking["id"]]

    # 3. Explicit approval -> calendar event + confirmation email
    response = client.post(f"/approve_booking/{booking['id']}")
    assert response.status_code == 200
    approved = response.json()
    assert approved["status"] == "APPROVED"
    assert approved["calendar_event_id"] is not None

    assert len(calendar.events) == 1
    event = calendar.events[0]
    assert "Ada Lovelace" in event["summary"]
    assert set(event["attendees"]) == {"rep@example.com", "ada@acme.io"}

    assert client.get(f"/leads/{lead_id}").json()["state"] == "MEETING_CONFIRMED"
    assert any(m["to"] == "ada@acme.io" and "confirmed" in m["subject"].lower()
               for m in email_sender.sent)
    assert client.get("/bookings/pending").json() == []


def test_cannot_select_unproposed_slot(client, calendar):
    lead_id = _lead_ready_for_scheduling(client)
    client.post(f"/leads/{lead_id}/propose_meeting", json={})
    response = client.post(f"/leads/{lead_id}/select_slot",
                           json={"slot_start": "2030-01-01T09:00:00+00:00"})
    assert response.status_code == 409
    assert calendar.events == []


def test_cannot_approve_twice(client, calendar):
    lead_id = _lead_ready_for_scheduling(client)
    slots = client.post(f"/leads/{lead_id}/propose_meeting", json={}).json()["slots"]
    booking_id = client.post(f"/leads/{lead_id}/select_slot",
                             json={"slot_start": slots[0]}).json()["id"]

    assert client.post(f"/approve_booking/{booking_id}").status_code == 200
    assert client.post(f"/approve_booking/{booking_id}").status_code == 409
    assert len(calendar.events) == 1  # still exactly one event


def test_reject_returns_lead_to_proposed(client, calendar):
    lead_id = _lead_ready_for_scheduling(client)
    slots = client.post(f"/leads/{lead_id}/propose_meeting", json={}).json()["slots"]
    booking_id = client.post(f"/leads/{lead_id}/select_slot",
                             json={"slot_start": slots[0]}).json()["id"]

    response = client.post(f"/bookings/{booking_id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert calendar.events == []  # rejection never touches the calendar

    lead = client.get(f"/leads/{lead_id}").json()
    assert lead["state"] == "MEETING_PROPOSED"

    # Lead can pick a different slot and go through approval again
    booking_id = client.post(f"/leads/{lead_id}/select_slot",
                             json={"slot_start": slots[1]}).json()["id"]
    assert client.post(f"/approve_booking/{booking_id}").status_code == 200
    assert len(calendar.events) == 1


def test_propose_requires_outreach_pending_state(client):
    csv = "name,email\nBob,bob@x.co\n"
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(csv.encode()), "text/csv")})
    response = client.post("/leads/1/propose_meeting", json={})
    assert response.status_code == 409


def test_state_machine_blocks_skipping_states():
    from app.models import Lead, LeadState
    from app.state_machine import InvalidTransition, transition
    import pytest

    lead = Lead(name="X", state=LeadState.NEW)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.MEETING_CONFIRMED)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.OUTREACH_PENDING)
    transition(lead, LeadState.SCORED)
    assert lead.state == LeadState.SCORED
