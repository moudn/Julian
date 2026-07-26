"""Per-org outreach analytics and Supabase/Postgres URL normalization."""

import io

from app.database import SessionLocal, normalize_db_url
from app.models import ConversationMessage, Lead, MessageDirection, OutreachMessage

CSV = ("name,email,title\n"
       "Ada Lovelace,ada@acme.io,VP Sales\n"
       "Bob Vance,bob@vance.io,VP Ops\n"
       "Carol King,carol@king.io,VP Eng\n")


def _setup_three_leads(client):
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    for lead_id in (1, 2, 3):
        client.post(f"/leads/{lead_id}/score")
        client.post(f"/leads/{lead_id}/generate_sequence")
        client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/scheduler/run")  # step 1 goes to all three


# ---------- url normalization (Supabase / Heroku) ----------

def test_normalize_db_url_maps_bare_postgres():
    assert normalize_db_url("postgres://u:p@host:5432/db") == \
        "postgresql+psycopg://u:p@host:5432/db"
    assert normalize_db_url("postgresql://u:p@host/db") == \
        "postgresql+psycopg://u:p@host/db"
    # already-driverful and sqlite URLs are untouched
    assert normalize_db_url("postgresql+psycopg://x/y") == "postgresql+psycopg://x/y"
    assert normalize_db_url("sqlite:///./x.db") == "sqlite:///./x.db"


# ---------- analytics ----------

def test_empty_analytics(client):
    a = client.get("/analytics").json()
    assert a["funnel"]["contacted"] == 0
    assert a["funnel"]["reply_rate"] == 0.0
    assert a["per_step"] == []
    assert len(a["weekly"]) == 8


def test_failed_sends_are_reported_not_silently_zero(client, monkeypatch):
    """With a broken mail server every metric is legitimately zero, which
    reads as "analytics is broken". The failure count explains why."""
    import io

    from app.adapters.gmail import GmailError
    from app.services import sending

    class FailingSender:
        def send(self, to, subject, body):
            raise GmailError("550 not a verified domain")

    monkeypatch.setattr(sending, "get_outbound_sender",
                        lambda db, org: FailingSender())
    monkeypatch.setattr(sending, "MAX_SEND_ATTEMPTS", 1)

    client.post("/leads/import", files={"file": (
        "l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    client.post("/leads/1/activate_sequence")
    client.post("/scheduler/run")

    f = client.get("/analytics").json()["funnel"]
    assert f["contacted"] == 0     # nothing actually went out
    assert f["failed"] >= 1        # and the page can now say why


def test_funnel_and_rates(client):
    _setup_three_leads(client)
    # lead 1 replies interested, lead 2 unsubscribes, lead 3 stays silent
    client.post("/replies/ingest", json={"lead_id": 1, "body": "sounds great, send times"})
    client.post("/replies/ingest", json={"lead_id": 2, "body": "unsubscribe me"})

    a = client.get("/analytics").json()
    f = a["funnel"]
    assert f["contacted"] == 3
    assert f["replied"] == 2          # leads 1 and 2 both replied
    assert f["interested"] == 1       # only lead 1 was positive
    assert f["unsubscribed"] == 1
    assert f["reply_rate"] == round(100 * 2 / 3, 1)
    assert f["interested_rate"] == round(100 * 1 / 3, 1)


def test_per_step_attribution(client):
    _setup_three_leads(client)
    # all three replied after step 1 (only step 1 has been sent)
    for lead_id in (1, 2, 3):
        client.post("/replies/ingest",
                    json={"lead_id": lead_id, "body": "tell me more"})
    a = client.get("/analytics").json()
    step1 = next(s for s in a["per_step"] if s["step"] == 1)
    assert step1["sent"] == 3
    assert step1["replies"] == 3
    assert step1["reply_rate"] == 100.0


def test_per_variant_reply_rate(client):
    _setup_three_leads(client)
    client.post("/replies/ingest", json={"lead_id": 1, "body": "interested"})
    a = client.get("/analytics").json()
    assert len(a["per_variant"]) == 1
    v = a["per_variant"][0]
    assert v["variant"] == "v1"
    assert v["contacted"] == 3
    assert v["replied"] == 1
    assert v["reply_rate"] == round(100 * 1 / 3, 1)


def test_weekly_trend_records_sends_and_replies(client):
    _setup_three_leads(client)
    client.post("/replies/ingest", json={"lead_id": 1, "body": "interested"})
    a = client.get("/analytics").json()
    current_week = a["weekly"][-1]
    assert current_week["sent"] == 3
    assert current_week["replies"] == 1


def test_reply_attribution_persisted(client):
    _setup_three_leads(client)
    client.post("/replies/ingest", json={"lead_id": 1, "body": "tell me more"})
    db = SessionLocal()
    try:
        inbound = (db.query(ConversationMessage)
                   .filter_by(lead_id=1, direction=MessageDirection.INBOUND).one())
        assert inbound.replied_after_step == 1
    finally:
        db.close()


def test_analytics_isolated_per_org(anon_client):
    from tests.conftest import signup
    key_a = signup(anon_client, org_name="A", email="a@a.io")
    key_b = signup(anon_client, org_name="B", email="b@b.io")
    ha = {"Authorization": f"Bearer {key_a}"}

    anon_client.post("/leads/import", headers=ha,
                     files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    anon_client.post("/icp/rules", headers=ha, json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60})
    anon_client.post("/leads/1/score", headers=ha)
    anon_client.post("/leads/1/generate_sequence", headers=ha)
    anon_client.post("/leads/1/activate_sequence", headers=ha)
    anon_client.post("/scheduler/run", headers=ha)

    # Org B sees nothing from Org A
    b = anon_client.get("/analytics", headers={"Authorization": f"Bearer {key_b}"}).json()
    assert b["funnel"]["contacted"] == 0
