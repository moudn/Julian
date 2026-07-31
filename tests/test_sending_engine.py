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


def test_activation_rejects_footer_missing_address_or_optout(client):
    """A footer that's merely non-empty used to be enough. Anti-spam law
    requires an actual opt-out instruction and a real postal address."""
    lead_id = _lead_with_sequence(client)

    client.patch("/auth/org", json={"email_footer": "Thanks, the team"})
    response = client.post(f"/leads/{lead_id}/activate_sequence")
    assert response.status_code == 409
    assert "opt-out" in response.json()["detail"]
    assert "postal address" in response.json()["detail"]

    client.patch("/auth/org", json={
        "email_footer": "Reply STOP to unsubscribe. No address though."})
    response = client.post(f"/leads/{lead_id}/activate_sequence")
    assert response.status_code == 409
    assert "postal address" in response.json()["detail"]
    assert "opt-out" not in response.json()["detail"]  # that part's satisfied

    client.patch("/auth/org", json={
        "email_footer": "\n--\nAcme, 1 Main St. Reply STOP to unsubscribe."})
    assert client.post(f"/leads/{lead_id}/activate_sequence").status_code == 200


def test_send_cycle_appends_org_optout_footer(client, email_sender, monkeypatch):
    from app.services import sending
    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: email_sender)
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")
    to_lead = [m for m in email_sender.sent if m["to"] == "ada@acme.io"]
    assert len(to_lead) == 1
    body = to_lead[0]["body"].lower()
    assert "no thanks" in body          # opt-out instruction
    assert "1 test street" in body      # postal address (CAN-SPAM)


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
    to_lead = [m for m in email_sender.sent if m["to"] == "ada@acme.io"]
    assert "1 Main St" in to_lead[0]["body"]


def test_send_cycle_uses_branded_signature_when_enabled(client):
    """When a branded signature is on, the footer moves out of the plain
    body and into the signature trailer (both html and plain), which is
    also where the compliance footer must now show up in either form."""
    from app.database import SessionLocal
    from app.models import Organization
    from app.services import sending

    client.patch("/auth/org", json={
        "email_footer": "\n--\nAcme Inc, 1 Main St. Reply STOP to opt out.",
        "email_signature_enabled": True,
        "signature_title": "Head of Sales",
        "signature_phone": "555-0100",
    })
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")

    captured = {}

    class CapturingSender:
        last_thread_id = None

        def send(self, to, subject, body, **kwargs):
            captured["to"] = to
            captured["body"] = body
            captured.update(kwargs)
            return "msg-id"

    db = SessionLocal()
    try:
        org = db.query(Organization).first()
        result = sending.run_send_cycle(db, org, sender=CapturingSender())
    finally:
        db.close()

    assert result["sent"] == 1
    assert "Acme Inc" not in captured["body"]  # footer no longer baked into the body
    assert captured["signature_html"] is not None
    assert "Head of Sales" in captured["signature_html"]
    assert "1 Main St" in captured["signature_html"]  # footer present in the html trailer too
    assert "555-0100" in captured["signature_text"]
    assert "1 Main St" in captured["signature_text"]


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


def test_gmail_adapter_exposes_thread_id_for_reply_scoping():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "gmail-msg-1", "threadId": "thread-xyz"})

    adapter = GmailSenderAdapter(
        token_provider=lambda: "tok-123",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert adapter.last_thread_id is None
    adapter.send("ada@acme.io", "Quick question", "Hi Ada,\n\nJulian")
    assert adapter.last_thread_id == "thread-xyz"


def test_send_cycle_records_gmail_thread_id_on_lead(client, monkeypatch):
    """Regression: without this, reply-polling had no way to scope to the
    right conversation and could ingest unrelated mail (see poll_replies
    tests) that merely shared the lead's sender address."""
    from app.database import SessionLocal
    from app.models import Lead
    from app.services import sending

    class FakeGmailSender:
        def __init__(self):
            self.last_thread_id = "thread-xyz"

        def send(self, to, subject, body):
            return "gmail-msg-1"

    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: FakeGmailSender())
    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")

    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        assert lead.gmail_thread_id == "thread-xyz"
    finally:
        db.close()


def test_state_machine_terminal_states_block_everything():
    import pytest
    from app.state_machine import InvalidTransition, transition

    lead = Lead(name="X", state=LeadState.NOT_INTERESTED)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.SEQUENCE_ACTIVE)
    lead = Lead(name="Y", state=LeadState.UNSUBSCRIBED)
    with pytest.raises(InvalidTransition):
        transition(lead, LeadState.ENGAGED)


def test_revoked_google_is_detected_even_with_an_unexpired_token(client, monkeypatch):
    """Revoking access in a Google account kills the access token instantly,
    long before its recorded expiry. Julian used to see the resulting 401 as
    an ordinary send failure: it retried, never marked the connection broken,
    and never told the customer to reconnect."""
    from datetime import timedelta

    from app.adapters import google_oauth
    from app.adapters.gmail import GmailSenderAdapter
    from app.adapters.google_oauth import GoogleOAuthError
    from app.database import SessionLocal
    from app.models import GoogleCredential, Organization, utcnow
    from app.services import sending

    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")

    db = SessionLocal()
    try:
        org = db.query(Organization).first()
        db.add(GoogleCredential(
            org_id=org.id, refresh_token="revoked",
            access_token="dead-but-unexpired",
            token_expiry=utcnow() + timedelta(minutes=50)))
        db.commit()
    finally:
        db.close()

    def gmail_401(request):
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    def refused(refresh_token):
        raise GoogleOAuthError("400 invalid_grant")

    monkeypatch.setattr(google_oauth, "refresh_access_token", refused)

    db = SessionLocal()
    try:
        org = db.query(Organization).first()
        credential = db.query(GoogleCredential).first()
        sender = GmailSenderAdapter(
            token_provider=lambda: google_oauth.get_valid_access_token(db, credential),
            on_auth_error=lambda: google_oauth.get_valid_access_token(
                db, credential, force_refresh=True),
            client=httpx.Client(transport=httpx.MockTransport(gmail_401)))

        result = sending.run_send_cycle(db, org, sender=sender)
        db.refresh(credential)

        assert result["sent"] == 0
        assert credential.broken is True
        assert credential.broken_notified is True
        # not burned as a retry — the message is still queued for after they reconnect
        message = db.query(OutreachMessage).filter_by(step=1).one()
        assert message.send_attempts == 0
        assert message.status == MessageStatus.APPROVED
    finally:
        db.close()


def test_a_401_that_survives_refresh_stops_the_cycle(client, monkeypatch):
    """If the token refreshes fine but Gmail still 401s, the connection is
    unusable — stop rather than retrying it four times per message."""
    from app.adapters.gmail import GmailAuthError
    from app.database import SessionLocal
    from app.models import GoogleCredential, Organization, utcnow
    from app.services import sending

    lead_id = _lead_with_sequence(client)
    client.post(f"/leads/{lead_id}/activate_sequence")

    db = SessionLocal()
    try:
        org = db.query(Organization).first()
        db.add(GoogleCredential(org_id=org.id, refresh_token="r",
                                access_token="a", token_expiry=utcnow()))
        db.commit()
    finally:
        db.close()

    class AlwaysUnauthorized:
        def send(self, to, subject, body):
            raise GmailAuthError("Gmail rejected the access token")

    db = SessionLocal()
    try:
        org = db.query(Organization).first()
        result = sending.run_send_cycle(db, org, sender=AlwaysUnauthorized())
        credential = db.query(GoogleCredential).first()
        assert result["sent"] == 0
        assert credential.broken is True
        assert any("auth rejected" in e for e in result["errors"])
    finally:
        db.close()
