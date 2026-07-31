import io

CSV = (
    "name,email,company,title,company_size,location\n"
    "Ada Lovelace,ada@acme.io,Acme,VP of Engineering,250,London\n"
    "Bob Smith,bob@small.co,SmallCo,Intern,3,Austin\n"
    ",noname@x.com,X,CEO,10,\n"
    "Ada Lovelace,ada@acme.io,Acme,VP of Engineering,250,London\n"
)


def _import_csv(client):
    return client.post(
        "/leads/import",
        files={"file": ("leads.csv", io.BytesIO(CSV.encode()), "text/csv")},
    )


def test_csv_import(client):
    response = _import_csv(client)
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 2
    assert body["skipped"] == 2  # missing name + duplicate email
    assert len(body["errors"]) == 2

    leads = client.get("/leads").json()
    assert len(leads) == 2
    assert leads[0]["name"] == "Ada Lovelace"
    assert leads[0]["state"] == "NEW"
    assert leads[0]["company_size"] == 250


def _upload(client, text, name="leads.csv"):
    return client.post("/leads/import", files={
        "file": (name, io.BytesIO(text.encode()), "text/csv")}).json()


def test_reimport_adds_new_rows_and_explains_the_skips(client):
    """Re-uploading a list with one row added is the normal way people grow
    a lead list — the new row must land, and the UI needs to be told why the
    rest didn't."""
    first = "name,email\nAda,ada@acme.io\nBob,bob@acme.io\n"
    assert _upload(client, first)["imported"] == 2

    again = first + "Carol,carol@acme.io\n"
    result = _upload(client, again)
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert all("duplicate email" in e for e in result["errors"])
    assert {lead["name"] for lead in client.get("/leads").json()} == {
        "Ada", "Bob", "Carol"}


def test_import_accepts_split_first_and_last_name_columns(client):
    """Apollo/HubSpot/LinkedIn exports ship first+last, not a single name."""
    result = _upload(client, "First Name,Last Name,Email,Company\n"
                             "Dan,Smith,dan@acme.io,Acme\n")
    assert result["imported"] == 1
    assert client.get("/leads").json()[0]["name"] == "Dan Smith"


def test_import_handles_semicolon_separated_files(client):
    """Excel writes ';' in many locales; read as commas every row looked
    like it was missing a name."""
    result = _upload(client, "name;email;company;title\n"
                             "Eve;eve@acme.io;Acme;VP\n")
    assert result["imported"] == 1
    assert client.get("/leads").json()[0]["email"] == "eve@acme.io"


def test_unrecognisable_header_says_so_instead_of_silently_skipping(client):
    result = _upload(client, "foo,bar\n1,2\n")
    assert result["imported"] == 0
    assert "No recognisable columns" in result["errors"][0]
    assert "foo" in result["errors"][0]   # shows what it actually found


def test_rejects_non_csv(client):
    response = client.post(
        "/leads/import",
        files={"file": ("leads.pdf", io.BytesIO(b"x"), "application/pdf")},
    )
    assert response.status_code == 400


def test_icp_scoring_moves_lead_to_scored(client):
    _import_csv(client)
    client.post("/icp/rules", json={
        "name": "Senior title", "field": "title", "operator": "in",
        "value": ["VP", "Director", "Head of"], "weight": 30,
    })
    client.post("/icp/rules", json={
        "name": "Mid-size company", "field": "company_size", "operator": "gte",
        "value": 100, "weight": 30,
    })

    results = client.post("/leads/score_all").json()
    by_id = {r["lead_id"]: r for r in results}

    ada = client.get("/leads/1").json()
    bob = client.get("/leads/2").json()
    assert by_id[ada["id"]]["score"] == 60
    assert ada["state"] == "SCORED"
    assert by_id[bob["id"]]["score"] == 0
    assert bob["state"] == "NEW"


def test_generate_message_requires_scored_state(client):
    _import_csv(client)
    response = client.post("/leads/1/generate_message")
    assert response.status_code == 409


def test_generate_message_saves_draft_and_advances_state(client):
    _import_csv(client)
    client.post("/icp/rules", json={
        "name": "Senior title", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/leads/1/score")

    # No OPENROUTER_API_KEY in tests -> deterministic template fallback
    response = client.post("/leads/1/generate_message")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "OUTREACH_PENDING"
    assert "Ada" in body["draft"]

    lead = client.get("/leads/1").json()
    assert lead["outreach_draft"] == body["draft"]


# ---------- ICP scoring: negative weights, expanded fields, AI fit ----------

def test_negative_weight_rule_penalizes_score(client):
    """Negative weights already worked end-to-end (nothing in the schema or
    matcher restricted them) — this locks that in as supported behavior."""
    client.post("/icp/rules", json={
        "name": "Senior", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    client.post("/icp/rules", json={
        "name": "Intern penalty", "field": "title", "operator": "contains",
        "value": "Intern", "weight": -100,
    })
    _import_csv(client)
    ada = client.post("/leads/1/score").json()   # VP of Engineering
    bob = client.post("/leads/2/score").json()   # Intern
    assert ada["score"] == 60
    assert bob["score"] == -100


def test_rule_can_target_research_notes_field(client):
    """Rules can already match against any Lead attribute by name — this
    proves the newly-exposed research_notes option genuinely round-trips,
    letting a rule react to post-research findings."""
    client.post("/icp/rules", json={
        "name": "Funding signal", "field": "research_notes", "operator": "contains",
        "value": "funding", "weight": 25,
    })
    _import_csv(client)
    from app.database import SessionLocal
    from app.models import Lead
    db = SessionLocal()
    try:
        lead = db.get(Lead, 1)
        lead.research_notes = "Recently raised a funding round."
        db.commit()
    finally:
        db.close()
    result = client.post("/leads/1/score").json()
    assert result["score"] == 25


def test_score_fit_returns_none_without_api_key():
    from app.adapters.llm import OpenRouterAdapter
    from app.models import Lead, Organization
    llm = OpenRouterAdapter(api_key="")
    lead = Lead(id=1, name="Ada", title="VP")
    assert llm.score_fit(lead, Organization(name="O")) is None


def test_score_fit_parses_integer_response():
    import httpx
    from app.adapters.llm import OpenRouterAdapter
    from app.models import Lead, Organization

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "78"}}]})

    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    lead = Lead(id=1, name="Ada", title="VP of Engineering", company="Acme")
    score = llm.score_fit(lead, Organization(name="O", product_description="sales software"))
    assert score == 78


def test_score_fit_clamps_out_of_range_values():
    import httpx
    from app.adapters.llm import OpenRouterAdapter
    from app.models import Lead, Organization

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "150"}}]})

    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.score_fit(Lead(id=1, name="Ada"), Organization(name="O")) == 100


def test_score_fit_returns_none_on_unparseable_response():
    import httpx
    from app.adapters.llm import OpenRouterAdapter
    from app.models import Lead, Organization

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "no idea"}}]})

    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.score_fit(Lead(id=1, name="Ada"), Organization(name="O")) is None


def test_score_fit_returns_none_on_api_failure():
    import httpx
    from app.adapters.llm import OpenRouterAdapter
    from app.models import Lead, Organization

    def handler(request):
        return httpx.Response(500, text="server error")

    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.score_fit(Lead(id=1, name="Ada"), Organization(name="O")) is None


def test_ai_fit_scoring_blends_into_rule_score(client):
    from app.deps import get_llm_adapter
    from app.main import app

    class FakeLLM:
        def score_fit(self, lead, org):
            return 80

    app.dependency_overrides[get_llm_adapter] = lambda: FakeLLM()

    client.patch("/auth/org", json={"ai_fit_scoring_enabled": True, "ai_fit_weight": 40})
    client.post("/icp/rules", json={
        "name": "VP", "field": "title", "operator": "contains", "value": "VP", "weight": 20,
    })
    _import_csv(client)
    result = client.post("/leads/1/score").json()  # Ada, VP of Engineering
    assert result["ai_fit_score"] == 80
    assert result["score"] == 20 + round(80 / 100 * 40)  # rule (+20) + AI contribution (+32)


def test_ai_fit_scoring_disabled_by_default_skips_llm_call(client):
    from app.deps import get_llm_adapter
    from app.main import app

    class FakeLLM:
        def score_fit(self, lead, org):
            raise AssertionError("must not be called when AI fit scoring is disabled")

    app.dependency_overrides[get_llm_adapter] = lambda: FakeLLM()
    _import_csv(client)
    result = client.post("/leads/1/score").json()
    assert result["ai_fit_score"] is None
