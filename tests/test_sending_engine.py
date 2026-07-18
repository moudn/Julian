"""Autopilot sending: activation, cadence scheduling, stop-on-state-change."""

import base64
import io
import json
from datetime import timedelta

import httpx

from app.adapters.gmail import GmailSenderAdapter
from app.database import SessionLocal
from app.models import Lead, LeadState, MessageStatus, OutreachMessage, utcnow

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _lead_with_sequence(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    return 1


def _make_due(lead_id: int, steps: list[int]):
    """Backdate scheduled_at so the given steps are due now."""
    db = SessionLocal()
    try:
        for message in db.query(OutreachMessage).filter_by(lead_id=lead_id).all():
            if message.step in steps:
                message.scheduled_at = utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()


def test_activation_schedules_cadence(client):
    lead_id = _lead_with_sequence(client)
    response = client.post(f"/leads/{lead_id}/activate_sequence")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "SEQUENCE_ACTIVE"
    assert all(m["status"] == "APPROVED" for m in body["messages"])

    db = SessionLocal()
    try:
        messages = (db.query(OutreachMessage).filter_by(lead_id=lead_id)
                    .order_by(OutreachMessage.step).all())
        deltas = [(m.scheduled_at.replace(tzinfo=None)
                   - messages[0].scheduled_at.replace(tzinfo=None)).days
                  for m in messages]
        assert deltas == [0, 3, 7, 12]
    finally:
        db.close()


def test_activation_requires_drafts_and_state(client):
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    assert client.post("/leads/1/activate_sequence").status_code == 409  # NEW state


def test_send_cycle_sends_only_due_steps(client):
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")

    result = client.post("/scheduler/run").json()
    assert result["sent"] == 1  # only step 1 is due at activation

    sequence = client.get(f"/leads/{lead_id}/sequence").json()["messages"]
    statuses = {m["step"]: m["status"] for m in sequence}
    assert statuses == {1: "SENT", 2: "APPROVED", 3: "APPROVED", 4: "APPROVED"}

    # running again immediately sends nothing new
    assert client.post("/scheduler/run").json()["sent"] == 0

    # time passes: step 2 becomes due
    _make_due(lead_id, steps=[2])
    assert client.post("/scheduler/run").json()["sent"] == 1


def test_send_cycle_appends_default_optout_footer(client, email_sender, monkeypatch):
    from app.services import sending
    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: email_sender)
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")
    assert len(email_sender.sent) == 1
    assert 'reply "no thanks"' in email_sender.sent[0]["body"]


def test_send_cycle_uses_custom_footer(client, email_sender, monkeypatch):
    from app.services import sending
    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: email_sender)
    client.patch("/auth/org", json={
        "email_footer": "\n--\nAcme Inc, 1 Main St. Reply STOP to opt out.",
    })
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")
    assert "1 Main St" in email_sender.sent[0]["body"]


def test_sequence_stops_when_lead_leaves_active_state(client):
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")  # step 1 out

    # lead replies -> ENGAGED (simulating the future reply pipeline)
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        lead.state = LeadState.ENGAGED
        db.commit()
    finally:
        db.close()

    _make_due(lead_id, steps=[2, 3, 4])
    result = client.post("/scheduler/run").json()
    assert result["sent"] == 0
    assert result["skipped"] == 3  # remaining steps permanently retired

    sequence = client.get(f"/leads/{lead_id}/sequence").json()["messages"]
    assert {m["step"]: m["status"] for m in sequence} == {
        1: "SENT", 2: "SKIPPED", 3: "SKIPPED", 4: "SKIPPED"}


def test_send_failure_leaves_message_retryable(client, monkeypatch):
    from app.adapters.gmail import GmailError
    from app.services import sending

    class FailingSender:
        def send(self, to, subject, body):
            raise GmailError("boom")

    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: FailingSender())
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    result = client.post("/scheduler/run").json()
    assert result["sent"] == 0
    assert len(result["errors"]) == 1
    sequence = client.get(f"/leads/{lead_id}/sequence").json()["messages"]
    assert sequence[0]["status"] == "APPROVED"  # still queued for retry


def test_gmail_adapter_builds_rfc822_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["raw"] = json.loads(request.content)["raw"]
        return httpx.Response(200, json={"id": "gmail-msg-1"})

    adapter = GmailSenderAdapter(
        token_provider=lambda: "tok-123",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    message_id = adapter.send("ada@acme.io", "Quick question", "Hi Ada,\n\nJulian")

    assert message_id == "gmail-msg-1"
    assert captured["path"].endswith("/users/me/messages/send")
    assert captured["auth"] == "Bearer tok-123"
    decoded = base64.urlsafe_b64decode(captured["raw"]).decode()
    assert "To: ada@acme.io" in decoded
    assert "Subject: Quick question" in decoded
    assert "Hi Ada," in decoded


def test_state_machine_terminal_states_block_everything():
    import pytest
    from app.state_machine import InvalidTransition, transition

    lead = Lead(name="X", state=LeadState.NOT_INTERESTED)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.SEQUENCE_ACTIVE)
    lead = Lead(name="Y", state=LeadState.UNSUBSCRIBED)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.ENGAGED)
