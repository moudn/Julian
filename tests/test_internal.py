"""POST /internal/run-cycle: the external-cron fallback for hosts that
can't keep app.main's in-process background loop running (e.g. a free
tier that sleeps the app when idle)."""

from app.config import get_settings


def test_disabled_when_no_secret_configured(client):
    response = client.post("/internal/run-cycle")
    assert response.status_code == 503


def test_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cron_secret", "s3cret")
    response = client.post("/internal/run-cycle", headers={"X-Cron-Secret": "wrong"})
    assert response.status_code == 403


def test_rejects_missing_secret_header(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cron_secret", "s3cret")
    response = client.post("/internal/run-cycle")
    assert response.status_code == 403


def test_runs_cycle_with_correct_secret(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cron_secret", "s3cret")
    response = client.post("/internal/run-cycle", headers={"X-Cron-Secret": "s3cret"})
    assert response.status_code == 200
    body = response.json()
    assert "replies" in body and "send" in body
