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
