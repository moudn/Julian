"""Regressions for issues found in the pre-launch audit.

Each test here fails against the code as it stood before that audit.
"""

import io
from datetime import timedelta

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import Lead, LeadState, MessageStatus, Organization, OutreachMessage, utcnow
from app.services import sending
from app.services.leads import normalize_email, upsert_lead
from app.services.scoring import score_leads
from app.services.suppression import suppress_email


@pytest.fixture()
def billing_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    return settings


def _set_status(org_id: int, status: str) -> None:
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        org.subscription_status = status
        db.commit()
    finally:
        db.close()


# ---------- the autopilot must respect the subscription ----------

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _activated_lead(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP", "weight": 60})
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    client.post("/leads/1/activate_sequence")
    db = SessionLocal()
    try:
        for message in db.query(OutreachMessage).filter_by(lead_id=1).all():
            if message.step == 1:
                message.scheduled_at = utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    return 1


class CapturingSender:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body, **kwargs):
        self.sent.append(to)
        return "msg-1"


def test_lapsed_subscription_stops_the_background_send_cycle(client, billing_on):
    """A cancelled tenant must stop having mail sent under their name.

    require_active_subscription only ever guarded the HTTP routes, so the
    scheduler happily kept working every org in the table forever — the
    customer lost the dashboard but Julian carried on emailing for free.
    """
    org_id = client.get("/auth/me").json()["id"]
    _set_status(org_id, "active")
    _activated_lead(client)
    _set_status(org_id, "canceled")

    db = SessionLocal()
    try:
        result = sending.run_send_cycle_all_orgs(db)
    finally:
        db.close()
    assert result["sent"] == 0

    db = SessionLocal()
    try:
        step_one = db.query(OutreachMessage).filter_by(lead_id=1, step=1).one()
        assert step_one.status == MessageStatus.APPROVED  # queued, not sent
    finally:
        db.close()


def test_active_subscription_still_sends(client, billing_on):
    org_id = client.get("/auth/me").json()["id"]
    _set_status(org_id, "active")
    _activated_lead(client)

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        result = sending.run_send_cycle(db, org, sender=CapturingSender())
    finally:
        db.close()
    assert result["sent"] == 1


def test_trialing_subscription_still_sends(client, billing_on):
    """A 30-day trial is a real entitlement, not a lapsed subscription."""
    org_id = client.get("/auth/me").json()["id"]
    _set_status(org_id, "trialing")
    _activated_lead(client)

    db = SessionLocal()
    try:
        result = sending.run_send_cycle_all_orgs(db)
    finally:
        db.close()
    assert result["sent"] == 1


def test_billing_disabled_leaves_the_cycle_running(client):
    """Self-hosted / dev: no Stripe key means no gate."""
    _activated_lead(client)
    db = SessionLocal()
    try:
        result = sending.run_send_cycle_all_orgs(db)
    finally:
        db.close()
    assert result["sent"] == 1


# ---------- addresses differing only in case are one person ----------

def test_normalize_email_lowercases():
    assert normalize_email("  John@Acme.COM ") == "john@acme.com"
    assert normalize_email(None) is None


def test_csv_import_treats_case_variants_as_one_lead(client):
    """Two spellings of one mailbox used to import as two leads, which
    means two independent sequences to the same human."""
    rows = ("name,email\n"
            "John Smith,John@Acme.com\n"
            "John Smith,john@acme.com\n"
            "John Smith,JOHN@ACME.COM\n")
    result = client.post("/leads/import",
                         files={"file": ("l.csv", io.BytesIO(rows.encode()),
                                         "text/csv")}).json()
    assert result["imported"] == 1
    assert result["skipped"] == 2
    leads = client.get("/leads").json()
    assert [lead["email"] for lead in leads] == ["john@acme.com"]


def test_csv_import_dedupes_against_existing_leads_case_insensitively(client):
    client.post("/leads/import", files={"file": (
        "a.csv", io.BytesIO(b"name,email\nJohn Smith,john@acme.com\n"), "text/csv")})
    result = client.post("/leads/import", files={"file": (
        "b.csv", io.BytesIO(b"name,email\nJohn Smith,JOHN@ACME.com\n"),
        "text/csv")}).json()
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_suppressed_address_blocks_a_differently_cased_import(client):
    org_id = client.get("/auth/me").json()["id"]
    db = SessionLocal()
    try:
        suppress_email(db, org_id, "gone@acme.com", "unsubscribed")
        db.commit()
    finally:
        db.close()
    result = client.post("/leads/import", files={"file": (
        "l.csv", io.BytesIO(b"name,email\nGone Away,GONE@Acme.COM\n"),
        "text/csv")}).json()
    assert result["imported"] == 0
    assert "opted out" in " ".join(result["errors"])


def test_upsert_lead_matches_across_case(client):
    org_id = client.get("/auth/me").json()["id"]
    db = SessionLocal()
    try:
        first, created_first = upsert_lead(
            db, {"name": "Ada", "email": "Ada@Acme.io", "title": "VP"}, org_id)
        second, created_second = upsert_lead(
            db, {"name": "Ada", "email": "ada@acme.io", "title": "CTO"}, org_id)
        assert created_first is True
        assert created_second is False  # same person, not a new lead
        assert first.id == second.id
        assert second.email == "ada@acme.io"
        assert second.title == "CTO"
    finally:
        db.close()


# ---------- bulk scoring ----------

class FakeLLM:
    """Records how it was called so the batch path can be checked."""

    def __init__(self, score=80):
        self.score = score
        self.contexts = []

    @staticmethod
    def fit_context(lead, org):
        return f"lead:{lead.id}"

    def score_fit_context(self, context, lead_id="?"):
        self.contexts.append(context)
        return self.score

    def score_fit(self, lead, org):
        return self.score_fit_context(self.fit_context(lead, org), lead.id)


def _import_n(client, n: int) -> None:
    rows = "name,email,title\n" + "\n".join(
        f"Lead {i},lead{i}@x.com,VP of Engineering" for i in range(n))
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(rows.encode()), "text/csv")})


def test_score_leads_scores_every_lead_in_one_commit(client):
    _import_n(client, 5)
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    db = SessionLocal()
    try:
        org = db.get(Organization, client.get("/auth/me").json()["id"])
        leads = db.query(Lead).filter_by(org_id=org.id).all()
        scored = score_leads(db, leads, org)
        assert len(scored) == 5
        assert all(lead.score == 60 for lead in scored)
        assert all(lead.state == LeadState.SCORED for lead in scored)
    finally:
        db.close()


def test_score_leads_fans_out_one_fit_call_per_lead(client):
    _import_n(client, 6)
    db = SessionLocal()
    try:
        org = db.get(Organization, client.get("/auth/me").json()["id"])
        org.ai_fit_scoring_enabled = True
        org.ai_fit_weight = 50.0
        db.commit()
        leads = db.query(Lead).filter_by(org_id=org.id).all()
        llm = FakeLLM(score=80)
        scored = score_leads(db, leads, org, llm=llm)
        assert len(llm.contexts) == 6
        assert sorted(llm.contexts) == sorted(f"lead:{lead.id}" for lead in leads)
        # 80/100 of a 50-point weight = 40, on top of 0 rule points
        assert all(lead.ai_fit_score == 80 and lead.score == 40 for lead in scored)
    finally:
        db.close()


def test_score_leads_survives_one_failing_fit_call(client):
    _import_n(client, 3)

    class FlakyLLM(FakeLLM):
        def score_fit_context(self, context, lead_id="?"):
            if context.endswith("2"):
                raise RuntimeError("upstream blew up")
            return 60

    db = SessionLocal()
    try:
        org = db.get(Organization, client.get("/auth/me").json()["id"])
        org.ai_fit_scoring_enabled = True
        db.commit()
        leads = db.query(Lead).filter_by(org_id=org.id).all()
        scored = score_leads(db, leads, org, llm=FlakyLLM())
        assert len(scored) == 3  # batch completed despite the failure
        assert any(lead.ai_fit_score is None for lead in scored)
        assert any(lead.ai_fit_score == 60 for lead in scored)
    finally:
        db.close()


def test_score_all_endpoint_uses_the_bulk_path(client):
    _import_n(client, 4)
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 70})
    results = client.post("/leads/score_all").json()
    assert len(results) == 4
    assert all(r["score"] == 70 and r["state"] == "SCORED" for r in results)


# ---------- cron endpoint ----------

def test_cron_secret_rejects_a_wrong_value(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cron_secret", "s3cret")
    assert client.post("/internal/run-cycle",
                       headers={"X-Cron-Secret": "wrong"}).status_code == 403
    assert client.post("/internal/run-cycle").status_code == 403
    assert client.post("/internal/run-cycle",
                       headers={"X-Cron-Secret": "s3cret"}).status_code == 200
