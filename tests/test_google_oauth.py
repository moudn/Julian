"""Google Calendar OAuth connection flow, with the token endpoint mocked."""

from urllib.parse import parse_qs, urlparse

import pytest

from app.adapters import google_oauth
from app.adapters.google_oauth import GoogleOAuthError, make_state, parse_state


def test_state_roundtrip_and_tamper_detection():
    state = make_state(42)
    assert parse_state(state) == 42
    with pytest.raises(GoogleOAuthError):
        parse_state(f"43.{state.split('.')[1]}")  # signature for a different org
    with pytest.raises(GoogleOAuthError):
        parse_state("garbage")


def test_connect_requires_configured_client(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
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
    assert "state" in params


def test_callback_stores_credential_and_status_reports_connected(client, monkeypatch):
    org_id = client.get("/auth/me").json()["id"]

    monkeypatch.setattr(google_oauth, "exchange_code", lambda code: {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
    })
    # the router imported the symbol directly, so patch it there too
    from app.routers import integrations as integrations_router
    monkeypatch.setattr(integrations_router, "exchange_code", lambda code: {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
    })

    assert client.get("/integrations/google/status").json()["connected"] is False

    response = client.get("/integrations/google/callback",
                          params={"code": "auth-code", "state": make_state(org_id)})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"

    status = client.get("/integrations/google/status").json()
    assert status["connected"] is True
    assert status["calendar_id"] == "primary"

    # disconnect removes it
    assert client.delete("/integrations/google").status_code == 204
    assert client.get("/integrations/google/status").json()["connected"] is False


def test_callback_rejects_tampered_state(client):
    response = client.get("/integrations/google/callback",
                          params={"code": "auth-code", "state": "1.badsignature"})
    assert response.status_code == 400


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

    # Expired token -> refresh happens
    credential = GoogleCredential(
        org_id=1, refresh_token="rt-1", access_token="stale",
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    token = google_oauth.get_valid_access_token(FakeDb(), credential)
    assert token == "fresh-token"
    assert calls == ["rt-1"]

    # Still-valid token -> no second refresh
    token = google_oauth.get_valid_access_token(FakeDb(), credential)
    assert token == "fresh-token"
    assert calls == ["rt-1"]
