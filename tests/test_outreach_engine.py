"""Julian's outreach writing engine: sequences, linting, and API behavior."""

import io
import json
import re

import httpx

from app.adapters.llm import (
    SEQUENCE_CADENCE,
    OpenRouterAdapter,
    _parse_draft,
    _parse_example_emails,
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


def test_lint_catches_sales_and_ai_cliches():
    from app.adapters.llm import lint_cliches
    flagged = lint_cliches(
        "Hi Ada, I hope this email finds you well. I wanted to reach out "
        "because our best-in-class platform can help you leverage synergy "
        "and streamline your pain points. Best regards, Sam"
    )
    for phrase in ("i hope this email finds you well", "i wanted to reach out",
                   "best-in-class", "leverage", "synergy", "streamline",
                   "pain points", "best regards"):
        assert phrase in flagged


def test_fallback_templates_are_free_of_cliches():
    """The no-API-key templates are real customer-facing copy — they must
    clear the same bar the LLM output is held to."""
    from app.adapters.llm import _template_step, lint_cliches
    from app.models import Lead, Organization

    lead = Lead(name="Ada Lovelace", company="Acme", title="VP of Engineering")
    org = Organization(name="Kingsley", sender_name="Mo",
                       product_description="an AI sales agent")
    for step in (1, 2, 3, 4):
        draft = _template_step(lead, org, step)
        text = draft["subject"] + " " + draft["body"]
        assert lint_cliches(text) == [], f"step {step}: {lint_cliches(text)}"
        assert lint_spam_phrases(text) == []


def test_fallback_templates_read_sensibly_without_a_company():
    """Regression: substituting the literal string "your team" wherever a
    company name would go produced nonsense like "Most teams the size of
    your team" for a consumer lead with no company on file."""
    from app.adapters.llm import _template_step
    from app.models import Lead, Organization

    lead = Lead(name="Sarah Jenkins", company=None)
    org = Organization(name="Kingsley", sender_name="Mo",
                       product_description="an AI sales agent")
    for step in (1, 2, 3, 4):
        draft = _template_step(lead, org, step)
        text = draft["subject"] + " " + draft["body"]
        assert "your team" not in text.lower()
        assert "size of" not in text.lower()  # the self-referential sentence


def test_cliche_in_draft_triggers_one_corrective_rewrite():
    """A cliche-laden first draft must be sent back, exactly like a
    spam-flagged one, rather than mailed to a prospect as-is."""
    from app.models import Lead, Organization

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["messages"][-1]["content"])
        body = ("I wanted to reach out. Best regards, Mo" if len(calls) == 1
                else "You don't know me, so I'll get to the point. Mo")
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(
                {"subject": "quick one", "body": body})}}]})

    adapter = OpenRouterAdapter(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    draft = adapter.generate_step(
        Lead(name="Ada Lovelace", company="Acme"),
        Organization(name="Kingsley", sender_name="Mo"), step=1)

    assert len(calls) == 2, "cliche draft should have been sent back once"
    assert "i wanted to reach out" in calls[1].lower()  # told what was wrong
    assert "reach out" not in draft["body"].lower()     # rewrite was kept


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


def test_force_generate_sequence_overrides_below_threshold_lead(client):
    """A lead that never clears the ICP threshold is stuck in NEW forever
    with no path forward. The override lets a rep pursue it anyway."""
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    scored = client.post("/leads/1/score").json()
    assert scored["score"] == 0
    assert scored["state"] == "NEW"

    response = client.post("/leads/1/force_generate_sequence")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "OUTREACH_PENDING"
    assert len(body["messages"]) == 4

    lead = client.get("/leads/1").json()
    assert lead["state"] == "OUTREACH_PENDING"


def test_regenerate_replaces_drafts_not_duplicates(client):
    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")
    client.post(f"/leads/{lead_id}/generate_sequence")  # regenerate
    sequence = client.get(f"/leads/{lead_id}/sequence").json()
    assert len(sequence["messages"]) == 4


def test_regenerating_tells_the_model_what_it_wrote_last_time(client):
    """Regression: regenerate deleted the old drafts before generating new
    ones, so the model calling itself again had zero signal this was a
    repeat request — nothing stopped it reproducing a near-duplicate."""
    from app.deps import get_llm_adapter
    from app.main import app

    captured_prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_prompts.append(body["messages"][-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"subject": "Quick one", "body": "Draft text. Julian"})}}]})

    adapter = OpenRouterAdapter(
        api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    app.dependency_overrides[get_llm_adapter] = lambda: adapter

    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")
    step_1_prompts_before = len(captured_prompts)
    assert not any("REGENERATION" in p for p in captured_prompts)

    client.post(f"/leads/{lead_id}/generate_sequence")  # regenerate
    step_1_regen_prompt = captured_prompts[step_1_prompts_before]
    assert "REGENERATION" in step_1_regen_prompt
    assert "Draft text." in step_1_regen_prompt  # the previous body was included


def test_parse_example_emails_splits_on_delimiter():
    assert _parse_example_emails(None) == []
    assert _parse_example_emails("  \n") == []
    assert _parse_example_emails("Hi A,\n\nBody.\n\nMo") == ["Hi A,\n\nBody.\n\nMo"]
    parsed = _parse_example_emails("First one.\n---\nSecond one.")
    assert parsed == ["First one.", "Second one."]


def test_example_emails_flow_into_generation_prompt(client):
    """The org's own pasted-in example emails should reach the LLM as style
    exemplars, so drafts sound like the sender rather than a generic voice."""
    from app.deps import get_llm_adapter
    from app.main import app

    captured_prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_prompts.append(body["messages"][-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"subject": "Quick one", "body": "Draft text. Julian"})}}]})

    adapter = OpenRouterAdapter(
        api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    app.dependency_overrides[get_llm_adapter] = lambda: adapter

    client.patch("/auth/org", json={
        "example_emails": "Hi Jamie,\n\nQuick one for you.\n\nMo"
                         "\n---\n"
                         "Hi Sam,\n\nSaw your launch — nice work.\n\nMo",
    })
    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")

    assert any("Style examples" in p for p in captured_prompts)
    assert any("Saw your launch" in p for p in captured_prompts)


def test_step_templates_apply_only_to_their_own_step(client):
    """Per-step template guidance should reach the prompt for that specific
    step and no other."""
    from app.deps import get_llm_adapter
    from app.main import app

    captured_prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_prompts.append(body["messages"][-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"subject": "Quick one", "body": "Draft text. Julian"})}}]})

    adapter = OpenRouterAdapter(
        api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    app.dependency_overrides[get_llm_adapter] = lambda: adapter

    client.patch("/auth/org", json={
        "step_templates": {"1": "Open by referencing their recent funding round."},
    })
    lead_id = _scored_lead(client)
    client.post(f"/leads/{lead_id}/generate_sequence")

    assert len(captured_prompts) == 4
    assert "recent funding round" in captured_prompts[0]
    assert all("recent funding round" not in p for p in captured_prompts[1:])


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
