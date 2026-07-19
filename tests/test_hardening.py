"""Security & compliance hardening: rate limits, resets, suppression,
caps, timezone handling, booking re-check, headers."""

import io
from datetime import timedelta

from app.database import SessionLocal
from app.models import Organization, OutreachMessage, utcnow

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _lead_ready(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    return 1


# ---------- auth hardening ----------

def test_login_rate_limited(client):
    for _ in range(10):
        client.post("/auth/login", json={"email": "x@y.z", "password": "wrongpass"})
    response = client.post("/auth/login",
                           json={"email": "x@y.z", "password": "wrongpass"})
    assert response.status_code == 429


def test_password_reset_flow(client, email_sender):
    response = client.post("/auth/forgot_password",
                           json={"email": "owner@acme-sales.io"})
    assert response.status_code == 200
    reset_mail = [m for m in email_sender.sent if "Reset" in m["subject"]]
    assert len(reset_mail) == 1
    token = next(line for line in reset_mail[0]["body"].splitlines()
                 if line.count(".") == 2 and line.split(".")[0].isdigit())

    response = client.post("/auth/reset_password", json={
        "token": token, "new_password": "brand-new-pass-1"})
    assert response.status_code == 200
    login = client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "brand-new-pass-1"})
    assert login.status_code == 200
    # unknown emails get the same 200 (no account probing)
    assert client.post("/auth/forgot_password",
                       json={"email": "nobody@nowhere.io"}).status_code == 200


def test_reset_rejects_bad_tokens(client):
    assert client.post("/auth/reset_password", json={
        "token": "1.9999999999.deadbeef", "new_password": "whatever-pass"
    }).status_code == 400


def test_api_key_revocation(client):
    keys = client.get("/auth/keys").json()
    assert len(keys) == 1
    login = client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "s3cretpass!"})
    second_key = login.json()["api_key"]

    # revoke the second key; it stops working, the first still does
    key_id = [k["id"] for k in client.get("/auth/keys").json()
              if k["prefix"] == second_key[:8]]
    assert key_id, "new key should be listed"
    assert client.delete(f"/auth/keys/{key_id[0]}").status_code == 204
    denied = client.get("/leads", headers={"Authorization": f"Bearer {second_key}"})
    assert denied.status_code == 401
    assert client.get("/leads").status_code == 200


# ---------- suppression & compliance ----------

def test_optout_suppresses_and_blocks_reimport(client):
    lead_id = _lead_ready(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "please remove me from your list"})

    result = client.post(
        "/leads/import",
        files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")},
    ).json()
    assert result["imported"] == 0
    assert any("opted out" in e for e in result["errors"])


def test_lead_delete_erases_and_suppresses(client):
    lead_id = _lead_ready(client)
    export = client.get(f"/leads/{lead_id}/export").json()
    assert export["lead"]["email"] == "ada@acme.io"
    assert len(export["outreach_messages"]) == 4

    assert client.delete(f"/leads/{lead_id}").status_code == 204
    assert client.get(f"/leads/{lead_id}").status_code == 404

    # erased address can't come back either
    result = client.post(
        "/leads/import",
        files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")},
    ).json()
    assert result["imported"] == 0


def test_activation_requires_footer(client):
    client.patch("/auth/org", json={"email_footer": "   "})
    lead_id = _lead_ready(client)
    response = client.post(f"/leads/{lead_id}/activate_sequence")
    assert response.status_code == 409
    assert "footer" in response.json()["detail"].lower()


def test_csv_size_cap(client):
    big = b"name,email\n" + b"x" * (3 * 1024 * 1024)
    result = client.post(
        "/leads/import",
        files={"file": ("big.csv", io.BytesIO(big), "text/csv")},
    ).json()
    assert result["imported"] == 0
    assert any("too large" in e.lower() for e in result["errors"])


# ---------- sending guardrails ----------

def test_daily_cap_limits_send_cycle(client, monkeypatch):
    from app.services import sending
    monkeypatch.setattr(sending, "RAMP_BASE_PER_DAY", 1)  # new org -> cap 1/day

    csv = ("name,email,title\n"
           "A One,a1@x.io,VP Sales\nB Two,b2@x.io,VP Ops\n")
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(csv.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    for lead_id in (1, 2):
        client.post(f"/leads/{lead_id}/score")
        client.post(f"/leads/{lead_id}/generate_sequence")
        client.post(f"/leads/{lead_id}/activate_sequence")

    assert client.post("/scheduler/run").json()["sent"] == 1  # capped
    assert client.post("/scheduler/run").json()["sent"] == 0  # still capped today


def test_send_window_blocks_weekend(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enforce_send_window", True)
    from datetime import datetime, timezone as tz
    from app.services import sending
    # Saturday noon UTC
    saturday = datetime(2026, 7, 18, 12, 0, tzinfo=tz.utc)
    assert sending._in_send_window(Organization(name="x", timezone="UTC"),
                                   saturday) is False
    monday = datetime(2026, 7, 20, 12, 0, tzinfo=tz.utc)
    assert sending._in_send_window(Organization(name="x", timezone="UTC"),
                                   monday) is True
    # 12:00 UTC is 05:00 in Los_Angeles — outside their window
    assert sending._in_send_window(
        Organization(name="x", timezone="America/Los_Angeles"), monday) is False


def test_timezone_used_for_slot_proposals(client):
    client.patch("/auth/org", json={"timezone": "America/New_York"})
    lead_id = _lead_ready(client)
    slots = client.post(f"/leads/{lead_id}/propose_meeting", json={}).json()["slots"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    first = datetime.fromisoformat(slots[0])
    local_hour = first.astimezone(ZoneInfo("America/New_York")).hour
    assert 9 <= local_hour < 17  # business hours in the ORG's zone


def test_invalid_timezone_rejected(client):
    response = client.patch("/auth/org", json={"timezone": "Mars/Olympus_Mons"})
    assert response.status_code == 422


def test_approval_rechecks_calendar(client, calendar):
    lead_id = _lead_ready(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.post("/replies/ingest", json={"lead_id": lead_id,
                                         "body": "sounds good, send times"})
    slots = client.get(f"/leads/{lead_id}").json()["proposed_slots"]
    booking_id = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "option 1 please"}).json()["booking_id"]

    # the slot gets taken before the rep approves
    from datetime import datetime, timedelta as td
    start = datetime.fromisoformat(slots[0])
    calendar.busy.append((start, start + td(minutes=30)))

    response = client.post(f"/approve_booking/{booking_id}")
    assert response.status_code == 409
    assert "no longer free" in response.json()["detail"]
    assert calendar.events == []  # nothing was double-booked


# ---------- misc ----------

def test_security_headers_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_lead_stats_and_pagination(client):
    _lead_ready(client)
    stats = client.get("/leads/stats").json()
    assert stats["total"] == 1
    assert stats["by_state"]["OUTREACH_PENDING"] == 1
    assert client.get("/leads?limit=1&offset=1").json() == []


def test_auto_reply_disabled_escalates_kb_answers(client, email_sender):
    """Default posture: Julian drafts, human approves — even for KB answers."""
    from app.deps import get_llm_adapter
    from app.main import app

    lead_id = _lead_ready(client)
    client.post(f"/leads/{lead_id}/activate_sequence")
    client.patch("/auth/org", json={"knowledge_base": "Pricing: $99/seat."})

    class KBLlm:
        def classify_reply(self, lead, org, reply_text, thread=None):
            return {"category": "QUESTION", "suggested_reply": "",
                    "answer": "It's $99 per seat. Fancy a quick call?"}

    app.dependency_overrides[get_llm_adapter] = lambda: KBLlm()
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "how much is it?"}).json()

    assert result["auto_replied"] is False
    assert result["escalated"] is True
    assert result["suggested_reply"] == "It's $99 per seat. Fancy a quick call?"
    assert any("your turn" in m["subject"] for m in email_sender.sent)
