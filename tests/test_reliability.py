"""Reliability & pre-launch hardening: bounce handling, send-attempt caps,
Google-revoked handling, email verification, readiness check."""

import io

import httpx
import pytest

from app.database import SessionLocal
from app.models import GoogleCredential, Lead, LeadState, MessageStatus, OutreachMessage
from tests.conftest import signup

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _active_lead(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    client.post("/leads/1/activate_sequence")
    return 1


def _step1(lead_id: int) -> OutreachMessage:
    db = SessionLocal()
    try:
        return db.query(OutreachMessage).filter_by(lead_id=lead_id, step=1).one()
    finally:
        db.close()


# ---------- retry cap ----------

def test_transient_failure_retries_then_fails(client, monkeypatch):
    from app.adapters.gmail import GmailError
    from app.services import sending
    monkeypatch.setattr(sending, "MAX_SEND_ATTEMPTS", 3)

    class FlakySender:
        def send(self, to, subject, body):
            raise GmailError("temporary 503 backend error")

    monkeypatch.setattr(sending, "get_outbound_sender", lambda db, org: FlakySender())
    lead_id = _active_lead(client)

    for attempt in (1, 2):
        result = client.post("/scheduler/run").json()
        assert result["sent"] == 0 and result["failed"] == 0
        assert _step1(lead_id).status.value == "APPROVED"  # still retrying
        assert _step1(lead_id).send_attempts == attempt

    # third attempt hits the cap -> FAILED, no longer retried
    result = client.post("/scheduler/run").json()
    assert result["failed"] == 1
    msg = _step1(lead_id)
    assert msg.status.value == "FAILED"
    assert client.post("/scheduler/run").json()["failed"] == 0  # not picked up again


# ---------- hard bounce ----------

def test_hard_bounce_suppresses_and_stops_sequence(client, monkeypatch):
    from app.adapters.gmail import GmailError
    from app.services import sending

    class BouncingSender:
        def send(self, to, subject, body):
            raise GmailError("550 5.1.1 recipient rejected: no such user")

    monkeypatch.setattr(sending, "get_outbound_sender", lambda db, org: BouncingSender())
    lead_id = _active_lead(client)

    result = client.post("/scheduler/run").json()
    assert result["failed"] == 1
    assert _step1(lead_id).status.value == "FAILED"  # one attempt, not 4

    lead = client.get(f"/leads/{lead_id}").json()
    assert lead["state"] == "NOT_INTERESTED"  # dead lead, sequence stopped

    # bounced address is suppressed -> can't be re-imported
    reimport = client.post(
        "/leads/import",
        files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")},
    ).json()
    assert reimport["imported"] == 0


# ---------- google revoked ----------

def test_google_revocation_pauses_and_notifies(client, email_sender, monkeypatch):
    from app.adapters.google_oauth import GoogleAccessRevoked
    from app.services import sending

    # pretend the org has a Google connection whose refresh is now revoked
    org_id = client.get("/auth/me").json()["id"]
    db = SessionLocal()
    try:
        db.add(GoogleCredential(org_id=org_id, refresh_token="rt", access_token="at"))
        db.commit()
    finally:
        db.close()

    def broken_sender(db, org):
        raise GoogleAccessRevoked("invalid_grant")
    monkeypatch.setattr(sending, "get_outbound_sender", broken_sender)
    # the broken-connection notice is sent via a fresh EmailSenderAdapter in
    # the service; point it at the fixture so the test can observe it
    monkeypatch.setattr(sending, "EmailSenderAdapter", lambda: email_sender)
    # mark the credential broken as the real refresh path would
    db = SessionLocal()
    try:
        cred = db.query(GoogleCredential).filter_by(org_id=org_id).one()
        cred.broken = True
        cred.broken_reason = "Google access was revoked or expired"
        db.commit()
    finally:
        db.close()

    _active_lead(client)
    result = client.post("/scheduler/run").json()
    assert result["sent"] == 0
    assert any("revoked" in e for e in result["errors"])
    assert any("reconnect Google" in m["subject"] for m in email_sender.sent)

    # status endpoint surfaces the broken flag
    status = client.get("/integrations/google/status").json()
    assert status["broken"] is True


def test_revoked_marks_broken_and_recovers(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.adapters import google_oauth
    from app.adapters.google_oauth import GoogleAccessRevoked, get_valid_access_token
    from app.models import GoogleCredential

    class FakeDb:
        def commit(self): pass

    cred = GoogleCredential(org_id=1, refresh_token="rt", access_token="stale",
                            token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5))

    monkeypatch.setattr(google_oauth, "refresh_access_token",
                        lambda rt: (_ for _ in ()).throw(
                            google_oauth.GoogleOAuthError("400 invalid_grant")))
    with pytest.raises(GoogleAccessRevoked):
        get_valid_access_token(FakeDb(), cred)
    assert cred.broken is True

    # reconnect: refresh works again, broken clears
    monkeypatch.setattr(google_oauth, "refresh_access_token",
                        lambda rt: {"access_token": "fresh", "expires_in": 3600})
    assert get_valid_access_token(FakeDb(), cred) == "fresh"
    assert cred.broken is False


# ---------- email verification ----------

def test_unverified_user_cannot_activate(anon_client, email_sender):
    key = signup(anon_client, verify=False)
    headers = {"Authorization": f"Bearer {key}"}
    anon_client.post("/leads/import", headers=headers,
                     files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    anon_client.post("/icp/rules", headers=headers, json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60})
    anon_client.post("/leads/1/score", headers=headers)
    anon_client.post("/leads/1/generate_sequence", headers=headers)

    blocked = anon_client.post("/leads/1/activate_sequence", headers=headers)
    assert blocked.status_code == 403
    assert "verify" in blocked.json()["detail"].lower()

    # a verification email went out at signup; verifying unblocks activation
    verify_mail = [m for m in email_sender.sent if "Confirm your email" in m["subject"]]
    assert verify_mail
    token = next(line.strip() for line in verify_mail[-1]["body"].splitlines()
                 if line.strip().count(".") == 2
                 and line.strip().split(".")[0].isdigit())
    assert anon_client.post("/auth/verify_email",
                            json={"token": token}).status_code == 200
    assert anon_client.post("/leads/1/activate_sequence",
                            headers=headers).status_code == 200


def test_verify_rejects_bad_token(client):
    assert client.post("/auth/verify_email",
                       json={"token": "1.9999999999.bad"}).status_code == 400


def test_me_reports_verification_state(anon_client):
    key = signup(anon_client, verify=False)
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {key}"})
    assert me.json()["email_verified"] is False


# ---------- readiness ----------

def test_liveness_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
