"""Google Calendar OAuth connection flow, with the token endpoint mocked."""

from urllib.parse import parse_qs, urlparse

import pytest

from app.adapters import google_oauth
from app.adapters.google_oauth import GoogleOAuthError, consume_state, make_state
from app.database import SessionLocal


def _mint_state(org_id: int) -> str:
    db = SessionLocal()
    try:
        return make_state(db, org_id)
    finally:
        db.close()


def test_state_is_single_use(client):
    org_id = client.get("/auth/me").json()["id"]
    state = _mint_state(org_id)
    db = SessionLocal()
    try:
        assert consume_state(db, state) == org_id
        with pytest.raises(GoogleOAuthError, match="already-used"):
            consume_state(db, state)
    finally:
        db.close()


def test_forged_and_expired_states_rejected(client):
    org_id = client.get("/auth/me").json()["id"]
    db = SessionLocal()
    try:
        with pytest.raises(GoogleOAuthError, match="Unknown"):
            consume_state(db, "forged-token-value")

        from datetime import timedelta
        from app.models import OAuthState, utcnow
        expired = OAuthState(token="expired-tok", org_id=org_id,
                             expires_at=utcnow() - timedelta(minutes=1))
        db.add(expired)
        db.commit()
        with pytest.raises(GoogleOAuthError, match="expired"):
            consume_state(db, "expired-tok")
    finally:
        db.close()


def test_connect_requires_configured_client(client):
    response = client.get("/integrations/google/connect")
    assert response.status_code == 503
    assert "GOOGLE_CLIENT_ID" in response.json()["detail"]


def test_connect_builds_consent_url(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "google_client_id", "client-123")

    response = client.get("/integrations/google/connect")
    assert response.status_code == 200
    url = urlparse(response.json()["authorize_url"])
    params = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert params["client_id"] == ["client-123"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "gmail.send" in params["scope"][0]
    assert "state" in params


def test_callback_stores_credential_and_status_reports_connected(client, monkeypatch):
    org_id = client.get("/auth/me").json()["id"]

    from app.routers import integrations as integrations_router
    monkeypatch.setattr(integrations_router, "exchange_code", lambda code: {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
    })
    monkeypatch.setattr(integrations_router, "_fetch_account_email",
                        lambda token: "owner@gmail.example")

    assert client.get("/integrations/google/status").json()["connected"] is False

    response = client.get("/integrations/google/callback",
                          params={"code": "auth-code", "state": _mint_state(org_id)})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"

    status = client.get("/integrations/google/status").json()
    assert status["connected"] is True
    assert status["calendar_id"] == "primary"
    assert status["account_email"] == "owner@gmail.example"

    # replaying the same callback fails (state already consumed)
    response = client.get("/integrations/google/callback",
                          params={"code": "auth-code-2", "state": "reused"})
    assert response.status_code == 400

    # disconnect removes it
    assert client.delete("/integrations/google").status_code == 204
    assert client.get("/integrations/google/status").json()["connected"] is False


def test_tokens_are_encrypted_at_rest(client, monkeypatch):
    from sqlalchemy import text

    org_id = client.get("/auth/me").json()["id"]
    from app.routers import integrations as integrations_router
    monkeypatch.setattr(integrations_router, "exchange_code", lambda code: {
        "access_token": "at-secret", "refresh_token": "rt-secret", "expires_in": 3600,
    })
    monkeypatch.setattr(integrations_router, "_fetch_account_email", lambda t: None)
    client.get("/integrations/google/callback",
               params={"code": "c", "state": _mint_state(org_id)})

    db = SessionLocal()
    try:
        raw = db.execute(text(
            "SELECT refresh_token, access_token FROM google_credentials"
        )).one()
        # ciphertext on disk, plaintext through the ORM
        assert "rt-secret" not in raw[0]
        assert "at-secret" not in (raw[1] or "")
        from app.models import GoogleCredential
        credential = db.query(GoogleCredential).one()
        assert credential.refresh_token == "rt-secret"
    finally:
        db.close()


def test_access_token_refresh_when_expired(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.models import GoogleCredential

    calls = []
    monkeypatch.setattr(google_oauth, "refresh_access_token", lambda rt: (
        calls.append(rt) or {"access_token": "fresh-token", "expires_in": 3600}
    ))

    class FakeDb:
        def commit(self):
            pass

    credential = GoogleCredential(
        org_id=1, refresh_token="rt-1", access_token="stale",
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    token = google_oauth.get_valid_access_token(FakeDb(), credential)
    assert token == "fresh-token"
    assert calls == ["rt-1"]

    token = google_oauth.get_valid_access_token(FakeDb(), credential)
    assert token == "fresh-token"
    assert calls == ["rt-1"]
