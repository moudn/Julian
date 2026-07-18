"""Reply pipeline: classification, triage actions, escalation, polling."""

import base64
import io
import json

import httpx

from app.adapters.gmail import GmailReaderAdapter
from app.adapters.llm import OpenRouterAdapter
from app.database import SessionLocal
from app.models import OutreachMessage

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _active_lead(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    client.post("/leads/1/activate_sequence")
    client.post("/scheduler/run")  # step 1 goes out
    return 1


def _steps(lead_id: int) -> dict[int, str]:
    db = SessionLocal()
    try:
        return {m.step: m.status.value
                for m in db.query(OutreachMessage).filter_by(lead_id=lead_id)}
    finally:
        db.close()


def test_unsubscribe_is_terminal_and_silences_sequence(client):
    lead_id = _active_lead(client)
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "Please remove me from your list.",
    }).json()
    assert result["category"] == "UNSUBSCRIBE"
    assert result["lead_state"] == "UNSUBSCRIBED"
    assert not result["escalated"]
    statuses = _steps(lead_id)
    assert all(s in ("SENT", "SKIPPED") for s in statuses.values())

    # future cycles never mail this lead again
    assert client.post("/scheduler/run").json()["sent"] == 0


def test_not_interested_is_terminal_without_escalation(client):
    lead_id = _active_lead(client)
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "Thanks but we're not interested.",
    }).json()
    assert result["category"] == "NOT_INTERESTED"
    assert result["lead_state"] == "NOT_INTERESTED"
    assert not result["escalated"]


def test_out_of_office_postpones_without_state_change(client):
    lead_id = _active_lead(client)
    db = SessionLocal()
    try:
        before = {m.step: m.scheduled_at
                  for m in db.query(OutreachMessage).filter_by(lead_id=lead_id)
                  if m.status.value == "APPROVED"}
    finally:
        db.close()

    result = client.post("/replies/ingest", json={
        "lead_id": lead_id,
        "body": "Automatic reply: I am currently away on annual leave until Monday.",
    }).json()
    assert result["category"] == "OUT_OF_OFFICE"
    assert result["lead_state"] == "SEQUENCE_ACTIVE"  # still on autopilot

    db = SessionLocal()
    try:
        after = {m.step: m.scheduled_at
                 for m in db.query(OutreachMessage).filter_by(lead_id=lead_id)
                 if m.status.value == "APPROVED"}
        for step, scheduled in after.items():
            delta = scheduled.replace(tzinfo=None) - before[step].replace(tzinfo=None)
            assert delta.days == 7
    finally:
        db.close()


def test_interested_reply_triggers_auto_slot_proposal(client, email_sender):
    lead_id = _active_lead(client)
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id,
        "body": "This sounds good, tell me more — happy to chat next week.",
    }).json()
    assert result["category"] == "INTERESTED"
    assert result["lead_state"] == "MEETING_PROPOSED"  # structured path
    assert not result["escalated"]

    lead = client.get(f"/leads/{lead_id}").json()
    assert 2 <= len(lead["proposed_slots"]) <= 3

    # rep got an FYI, not a to-do
    fyi = [m for m in email_sender.sent if "times proposed" in m["subject"]]
    assert len(fyi) == 1
    assert "No action needed" in fyi[0]["body"]

    # thread records the proposal; autopilot is off for this lead
    conversation = client.get(f"/leads/{lead_id}/conversation").json()
    assert any(m["direction"] == "OUTBOUND"
               and "proposed meeting times" in m["body"] for m in conversation)
    assert client.post("/scheduler/run").json()["sent"] == 0


def test_interested_falls_back_to_human_when_calendar_fails(client, email_sender,
                                                            monkeypatch):
    from app.adapters.calendar import CalendarError
    from app.services import replies as replies_service

    class BrokenCalendar:
        def find_available_slots(self, *a, **k):
            raise CalendarError("google is down")

    monkeypatch.setattr(replies_service, "get_org_calendar",
                        lambda db, org: BrokenCalendar())
    lead_id = _active_lead(client)
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "sounds good, tell me more",
    }).json()
    assert result["category"] == "INTERESTED"
    assert result["lead_state"] == "ENGAGED"
    assert result["escalated"]
    assert any("your turn" in m["subject"] for m in email_sender.sent)


def test_complex_reply_escalates_with_thread_recorded(client, email_sender):
    lead_id = _active_lead(client)
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id,
        "body": "How does your pricing compare to your competitors, and can "
                "you integrate with our on-prem Oracle setup?",
    }).json()
    assert result["category"] == "COMPLEX"
    assert result["lead_state"] == "ENGAGED"
    assert result["escalated"]

    conversation = client.get(f"/leads/{lead_id}/conversation").json()
    assert len(conversation) == 1
    assert conversation[0]["direction"] == "INBOUND"
    assert conversation[0]["category"] == "COMPLEX"


def test_question_with_kb_answer_gets_auto_reply(client, monkeypatch, email_sender):
    from app.deps import get_llm_adapter
    from app.main import app
    from app.services import replies as replies_service

    lead_id = _active_lead(client)  # uses the real (fallback) LLM adapter
    client.patch("/auth/org", json={"knowledge_base": "Pricing: flat monthly fee per seat."})

    class KBLlm:
        def classify_reply(self, lead, org, reply_text, thread=None):
            return {"category": "QUESTION", "suggested_reply": "",
                    "answer": "We charge a flat monthly fee per seat. "
                              "Worth a quick call to see the details?"}

    app.dependency_overrides[get_llm_adapter] = lambda: KBLlm()

    outbound = []

    class FakeSender:
        def send(self, to, subject, body):
            outbound.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(replies_service, "get_outbound_sender",
                        lambda db, org: FakeSender())
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "subject": "Pricing?",
        "body": "Quick one — how does your pricing work?",
    }).json()

    assert result["category"] == "QUESTION"
    assert result["auto_replied"] is True
    assert result["escalated"] is False
    assert result["lead_state"] == "ENGAGED"
    assert outbound[0]["to"] == "ada@acme.io"
    assert "flat monthly fee" in outbound[0]["body"]

    conversation = client.get(f"/leads/{lead_id}/conversation").json()
    directions = [m["direction"] for m in conversation]
    assert directions == ["INBOUND", "OUTBOUND"]


def test_llm_classifier_parses_and_validates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "category": "interested",
            "suggested_reply": "Great — how's Tuesday?",
            "answer": "",
        })}}]})

    adapter = OpenRouterAdapter(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    from app.models import Lead, Organization
    lead = Lead(name="Ada Lovelace", title="VP", company="Acme")
    org = Organization(name="Test")
    result = adapter.classify_reply(lead, org, "sure, sounds interesting")
    assert result["category"] == "INTERESTED"
    assert result["suggested_reply"] == "Great — how's Tuesday?"


def test_optout_never_depends_on_llm(monkeypatch):
    def handler(request):  # LLM should not even be called
        raise AssertionError("LLM called for an unsubscribe reply")

    adapter = OpenRouterAdapter(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    from app.models import Lead, Organization
    result = adapter.classify_reply(Lead(name="X"), Organization(name="Y"),
                                    "unsubscribe me please")
    assert result["category"] == "UNSUBSCRIBE"


def test_gmail_reader_parses_multipart_message():
    plain = base64.urlsafe_b64encode(b"Sounds good, send times!").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={
            "id": "m1",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "Subject", "value": "Re: quick question"},
                    {"name": "From", "value": "Ada <ada@acme.io>"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": plain}},
                    {"mimeType": "text/html", "body": {"data": "aWdub3JlZA=="}},
                ],
            },
        })

    reader = GmailReaderAdapter(
        token_provider=lambda: "tok",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert reader.list_message_ids("from:ada@acme.io") == ["m1"]
    message = reader.get_message("m1")
    assert message["subject"] == "Re: quick question"
    assert message["body"] == "Sounds good, send times!"


def test_full_reply_to_booking_loop(client, email_sender, calendar):
    """Interest -> auto-proposed slots -> 'option 2' reply -> PendingBooking
    -> human approval -> calendar event. The complete agreed design."""
    lead_id = _active_lead(client)
    client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "sounds good, send times",
    })
    assert calendar.events == []  # proposing touches nothing

    result = client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "Option 2 works great for me!",
    }).json()
    assert result["category"] == "SLOT_SELECTED"
    assert result["lead_state"] == "AWAITING_APPROVAL"
    assert result["booking_id"] is not None
    assert calendar.events == []  # still nothing booked without approval

    # rep received the approval request
    assert any("Approval needed" in m["subject"] for m in email_sender.sent)

    # human approves -> the event exists
    approve = client.post(f"/approve_booking/{result['booking_id']}")
    assert approve.status_code == 200
    assert len(calendar.events) == 1
    assert client.get(f"/leads/{lead_id}").json()["state"] == "MEETING_CONFIRMED"


def test_ambiguous_slot_reply_escalates_to_human(client, email_sender):
    lead_id = _active_lead(client)
    client.post("/replies/ingest", json={
        "lead_id": lead_id, "body": "sounds good, send times",
    })
    result = client.post("/replies/ingest", json={
        "lead_id": lead_id,
        "body": "Hmm, could we do something in the afternoon instead?",
    }).json()
    assert result["category"] != "SLOT_SELECTED"
    assert result["lead_state"] == "ENGAGED"  # human takes over
    assert result["escalated"]


def test_extract_slot_choice_heuristics():
    from app.services.replies import extract_slot_choice
    slots = ["2026-07-20T09:00:00+00:00",   # Monday
             "2026-07-20T10:00:00+00:00",   # Monday
             "2026-07-22T14:00:00+00:00"]   # Wednesday

    def chosen(body):
        result = extract_slot_choice(body, slots)
        return result.isoformat() if result else None

    assert chosen("option 2 please") == "2026-07-20T10:00:00+00:00"
    assert chosen("The first one works") == "2026-07-20T09:00:00+00:00"
    assert chosen("Wednesday suits me") == "2026-07-22T14:00:00+00:00"
    assert chosen("Monday at 10:00 works") == "2026-07-20T10:00:00+00:00"
    assert chosen("let's do 2pm") == "2026-07-22T14:00:00+00:00"
    assert chosen("Monday works") is None            # two Monday slots: ambiguous
    assert chosen("any of those work") is None       # no signal
    assert chosen("how about Friday?") is None       # not a proposed slot
    assert extract_slot_choice("option 1", None) is None


def test_duplicate_gmail_message_ignored(client):
    lead_id = _active_lead(client)
    db = SessionLocal()
    try:
        from app.models import Lead, Organization
        lead = db.get(Lead, lead_id)
        org = db.get(Organization, lead.org_id)
        from app.services.replies import ingest_reply
        first = ingest_reply(db, lead, org, body="tell me more",
                             gmail_message_id="gm-1")
        second = ingest_reply(db, lead, org, body="tell me more",
                              gmail_message_id="gm-1")
    finally:
        db.close()
    assert first["status"] == "processed"
    assert second["status"] == "duplicate"
