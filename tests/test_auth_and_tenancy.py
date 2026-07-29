"""Auth and multi-tenant isolation tests.

The core guarantee: one organization can never see or act on another
organization's leads, rules, or bookings.
"""

import io
import smtplib

from tests.conftest import signup

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _import_lead(client, headers=None):
    return client.post(
        "/leads/import",
        files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=headers or {},
    )


def test_endpoints_require_auth(anon_client):
    assert anon_client.get("/leads").status_code == 401
    assert anon_client.post("/icp/rules", json={}).status_code == 401
    assert anon_client.get("/bookings/pending").status_code == 401
    assert anon_client.post("/approve_booking/1").status_code == 401
    assert anon_client.get("/integrations/google/connect").status_code == 401


def test_invalid_key_rejected(anon_client):
    response = anon_client.get(
        "/leads", headers={"Authorization": "Bearer sk_not-a-real-key"}
    )
    assert response.status_code == 401


def test_signup_login_me(anon_client):
    api_key = signup(anon_client)
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {api_key}"})
    assert me.status_code == 200
    assert me.json()["name"] == "Acme Sales"
    assert me.json()["sales_rep_email"] == "rep@example.com"

    # login mints a fresh, different key that also works
    login = anon_client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "S3cretpass!",
    })
    assert login.status_code == 200
    new_key = login.json()["api_key"]
    assert new_key != api_key
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {new_key}"})
    assert me.status_code == 200


def test_signup_rejects_weak_passwords(anon_client):
    weak = ["alllowercase1", "ALLUPPERCASE1", "NoDigitsHereX"]
    for i, pw in enumerate(weak):
        response = anon_client.post("/auth/signup", json={
            "organization_name": "X", "name": "Y",
            "email": f"weak{i}@x.io", "password": pw})
        assert response.status_code == 422, f"{pw!r} should have been rejected"
        assert "uppercase" in str(response.json()).lower()


def test_signup_rejects_too_short_password(anon_client):
    response = anon_client.post("/auth/signup", json={
        "organization_name": "X", "name": "Y",
        "email": "tooshort@x.io", "password": "Ab1defg"})
    assert response.status_code == 422


def test_signup_accepts_a_strong_password(anon_client):
    response = anon_client.post("/auth/signup", json={
        "organization_name": "X", "name": "Y",
        "email": "strong@x.io", "password": "Strong-pass-1"})
    assert response.status_code == 201


def test_verification_link_verifies_and_redirects_to_dashboard(anon_client):
    from app.database import SessionLocal
    from app.models import User
    from app.security import make_verify_token

    signup(anon_client, email="link@x.io", verify=False)
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email="link@x.io").one()
        assert user.email_verified is False
        token = make_verify_token(user.id)
    finally:
        db.close()

    response = anon_client.get("/auth/verify", params={"token": token},
                               follow_redirects=False)
    assert response.status_code == 303
    assert "verified=1" in response.headers["location"]
    assert "/app/#/dashboard" in response.headers["location"]

    db = SessionLocal()
    try:
        assert db.query(User).filter_by(email="link@x.io").one().email_verified is True
    finally:
        db.close()


def test_verification_link_with_bad_token_still_redirects_somewhere_useful(anon_client):
    response = anon_client.get("/auth/verify", params={"token": "garbage"},
                               follow_redirects=False)
    assert response.status_code == 303
    assert "verified=expired" in response.headers["location"]


def test_login_wrong_password_rejected(anon_client):
    signup(anon_client)
    response = anon_client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "wrong-password",
    })
    assert response.status_code == 401


def test_duplicate_signup_email_rejected(anon_client):
    signup(anon_client)
    response = anon_client.post("/auth/signup", json={
        "organization_name": "Other", "name": "X",
        "email": "owner@acme-sales.io", "password": "S3cretpass!",
    })
    assert response.status_code == 409


def test_orgs_cannot_see_each_others_data(anon_client):
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    assert _import_lead(anon_client, headers_a).json()["imported"] == 1
    lead_id = anon_client.get("/leads", headers=headers_a).json()[0]["id"]

    # Org B sees an empty list and cannot access A's lead directly
    assert anon_client.get("/leads", headers=headers_b).json() == []
    assert anon_client.get(f"/leads/{lead_id}", headers=headers_b).status_code == 404
    assert anon_client.post(f"/leads/{lead_id}/score", headers=headers_b).status_code == 404

    # Both orgs can hold a lead with the same email (per-org uniqueness)
    assert _import_lead(anon_client, headers_b).json()["imported"] == 1

    # Org B's ICP rules don't affect Org A's scoring
    anon_client.post("/icp/rules", headers=headers_b, json={
        "name": "B rule", "field": "title", "operator": "contains",
        "value": "VP", "weight": 100,
    })
    score = anon_client.post(f"/leads/{lead_id}/score", headers=headers_a).json()
    assert score["score"] == 0


def test_org_cannot_approve_other_orgs_booking(anon_client, calendar):
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    _import_lead(anon_client, headers_a)
    anon_client.post("/icp/rules", headers=headers_a, json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    lead_id = anon_client.get("/leads", headers=headers_a).json()[0]["id"]
    anon_client.post(f"/leads/{lead_id}/score", headers=headers_a)
    anon_client.post(f"/leads/{lead_id}/generate_message", headers=headers_a)
    slots = anon_client.post(f"/leads/{lead_id}/propose_meeting",
                             headers=headers_a, json={}).json()["slots"]
    booking_id = anon_client.post(
        f"/leads/{lead_id}/select_slot", headers=headers_a,
        json={"slot_start": slots[0]},
    ).json()["id"]

    # Org B cannot see or approve Org A's pending booking
    assert anon_client.get("/bookings/pending", headers=headers_b).json() == []
    assert anon_client.post(f"/approve_booking/{booking_id}",
                            headers=headers_b).status_code == 404
    assert calendar.events == []  # nothing was booked by the failed attempt

    # Org A approves it fine
    assert anon_client.post(f"/approve_booking/{booking_id}",
                            headers=headers_a).status_code == 200
    assert len(calendar.events) == 1


def _break_email_sender(email_sender, monkeypatch):
    """Simulate a mail-server rejection, e.g. an unverified SMTP_FROM domain."""
    def _raise(*args, **kwargs):
        raise smtplib.SMTPSenderRefused(
            550, b"not a verified domain", "agent@example.com")
    monkeypatch.setattr(email_sender, "send", _raise)


def test_signup_succeeds_even_if_verification_email_fails(
        anon_client, email_sender, monkeypatch):
    """A broken mail server must not make account creation look like it failed
    — the account is already committed by the time the email is sent."""
    _break_email_sender(email_sender, monkeypatch)
    response = anon_client.post("/auth/signup", json={
        "organization_name": "Acme Sales", "name": "Owner",
        "email": "owner@acme-sales.io", "password": "S3cretpass!",
    })
    assert response.status_code == 201
    assert "api_key" in response.json()


def test_resend_verification_surfaces_clean_error_on_send_failure(
        anon_client, email_sender, monkeypatch):
    """Previously this raised an unhandled exception -> generic 500 with no
    useful detail. It must now return a clean, actionable error instead."""
    api_key = signup(anon_client, verify=False)
    _break_email_sender(email_sender, monkeypatch)
    response = anon_client.post(
        "/auth/resend_verification",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 502
    assert "smtp" in response.json()["detail"].lower()


def test_forgot_password_still_returns_ok_if_email_send_fails(
        anon_client, email_sender, monkeypatch):
    """Anti-enumeration: a mail-server failure must not change the response,
    or it would reveal whether the address has an account."""
    signup(anon_client)
    _break_email_sender(email_sender, monkeypatch)
    response = anon_client.post("/auth/forgot_password",
                                json={"email": "owner@acme-sales.io"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
