"""Julian's outreach writing engine: sequences, linting, and API behavior."""

import io
import json
import re

import httpx

from app.adapters.llm import (
    SEQUENCE_CADENCE,
    OpenRouterAdapter,
    _parse_draft,
    lint_spam_phrases,
)

CSV = "name,email,company,title,company_size\nAda Lovelace,ada@acme.io,Acme,VP of Engineering,250\n"


def _scored_lead(client) -> int:
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/leads/1/score")
    return 1


# ---------- linting ----------

def test_lint_catches_spam_phrases():
    flagged = lint_spam_phrases("ACT NOW for this 100% guaranteed, risk-free deal!")
    assert "act now" in flagged
    assert "guaranteed" in flagged
    assert "risk-free" in flagged


def test_lint_passes_clean_copy():
    assert lint_spam_phrases(
        "Hi Ada, most VPs tell us follow-up eats hours. Worth a chat?"
    ) == []


# ---------- template fallback quality ----------

def test_sequence_endpoint_generates_four_steps(client):
    lead_id = _scored_lead(client)
    response = client.post(f"/leads/{lead_id}/generate_sequence")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "OUTREACH_PENDING"
    messages = body["messages"]
    assert [m["step"] for m in messages] == [1, 2, 3, 4]
    assert [m["send_after_days"] for m in messages] == [0, 3, 7, 12]

    for message in messages:
        # research-backed constraints hold even for the fallback templates
        assert len(message["subject"]) <= 50
        assert not message["subject"].isupper()
        word_count = len(re.findall(r"\S+", message["body"]))
        assert word_count <= 90
        assert message["spam_flags"] is None
        assert "Ada" in message["body"] or "Acme" in message["body"]
        assert message["status"] == "DRAFT"

    # first touch never opens with the classic dead openers
    first_body = messages[0]["body"].lower()
    assert "my name is" not in first_body
    assert "i hope this finds you well" not in first_body


def test_sequence_saved_and_retrievable(client):
    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")
    sequence = client.get(f"/leads/{lead_id}/sequence").json()
    assert len(sequence["messages"]) == 4
    lead = client.get(f"/leads/{lead_id}").json()
    assert lead["outreach_draft"] == sequence["messages"][0]["body"]


def test_sequence_requires_scored_state(client):
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    response = client.post("/leads/1/generate_sequence")  # still NEW
    assert response.status_code == 409


def test_regenerate_replaces_drafts_not_duplicates(client):
    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")
    client.post(f"/leads/{lead_id}/generate_sequence")  # regenerate
    sequence = client.get(f"/leads/{lead_id}/sequence").json()
    assert len(sequence["messages"]) == 4


def test_product_description_flows_into_drafts(client):
    client.patch("/auth/org", json={
        "product_description": "We build payroll software for restaurants",
    })
    lead_id = _scored_lead(client)
    messages = client.post(f"/leads/{lead_id}/generate_sequence").json()["messages"]
    assert any("payroll software for restaurants" in m["body"] for m in messages)


# ---------- LLM path (mocked API) ----------

def _mock_adapter(responses: list[dict]) -> OpenRouterAdapter:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        reply = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(reply)}}]
        })

    return OpenRouterAdapter(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_llm_spam_draft_triggers_corrective_rewrite(client):
    from app.deps import get_llm_adapter
    from app.main import app

    adapter = _mock_adapter([
        {"subject": "Act now!", "body": "This is a risk-free guaranteed deal. Julian"},
        {"subject": "Quick question about Acme", "body": "Hi Ada, clean rewrite. Julian"},
    ])
    app.dependency_overrides[get_llm_adapter] = lambda: adapter

    lead_id = _scored_lead(client)
    messages = client.post(f"/leads/{lead_id}/generate_sequence").json()["messages"]
    assert messages[0]["subject"] == "Quick question about Acme"
    assert messages[0]["spam_flags"] is None


def test_parse_draft_tolerates_code_fences():
    content = '```json\n{"subject": "Hi", "body": "Text. Julian"}\n```'
    draft = _parse_draft(content)
    assert draft == {"subject": "Hi", "body": "Text. Julian"}


def test_cadence_matches_research():
    assert SEQUENCE_CADENCE == {1: 0, 2: 3, 3: 7, 4: 12}
