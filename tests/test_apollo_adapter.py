import httpx
import pytest

from app.adapters.apollo import ApolloAdapter, ApolloError

APOLLO_PERSON = {
    "name": "Grace Hopper",
    "title": "CTO",
    "email": "grace@navy.mil",
    "linkedin_url": "https://linkedin.com/in/gracehopper",
    "city": "Arlington",
    "state": "Virginia",
    "country": "United States",
    "organization": {
        "name": "US Navy",
        "primary_domain": "navy.mil",
        "estimated_num_employees": 400000,
    },
}


def _adapter(handler) -> ApolloAdapter:
    transport = httpx.MockTransport(handler)
    return ApolloAdapter(
        api_key="test-key",
        base_url="https://api.apollo.io/v1",
        client=httpx.Client(transport=transport),
    )


def test_search_people_builds_payload_and_normalizes():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        import json
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"people": [APOLLO_PERSON]})

    adapter = _adapter(handler)
    people = adapter.search_people(
        titles=["CTO"], locations=["Virginia"],
        organization_domains=["navy.mil"], per_page=5,
    )

    assert captured["url"].endswith("/mixed_people/search")
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["payload"]["person_titles"] == ["CTO"]
    assert captured["payload"]["person_locations"] == ["Virginia"]
    assert captured["payload"]["q_organization_domains"] == "navy.mil"
    assert captured["payload"]["per_page"] == 5

    assert len(people) == 1
    person = people[0]
    assert person["name"] == "Grace Hopper"
    assert person["email"] == "grace@navy.mil"
    assert person["company"] == "US Navy"
    assert person["domain"] == "navy.mil"
    assert person["company_size"] == 400000
    assert person["location"] == "Arlington, Virginia, United States"
    assert person["source"] == "apollo"


def test_search_people_masks_locked_email():
    locked = dict(APOLLO_PERSON, email="email_not_unlocked@domain.com")

    def handler(request):
        return httpx.Response(200, json={"people": [locked]})

    people = _adapter(handler).search_people(titles=["CTO"])
    assert people[0]["email"] is None


def test_enrich_person_no_match_raises():
    def handler(request):
        return httpx.Response(200, json={"person": None})

    with pytest.raises(ApolloError, match="no match"):
        _adapter(handler).enrich_person("Nobody", "nowhere.dev")


def test_http_error_raises_apollo_error():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})

    with pytest.raises(ApolloError, match="401"):
        _adapter(handler).search_people(titles=["CTO"])


def test_missing_api_key_raises():
    adapter = ApolloAdapter(api_key="", client=httpx.Client())
    with pytest.raises(ApolloError, match="APOLLO_API_KEY"):
        adapter.search_people(titles=["CTO"])


def test_enrich_endpoint_upserts_lead(client, monkeypatch):
    from app.deps import get_apollo_adapter
    from app.main import app

    def handler(request):
        return httpx.Response(200, json={"person": APOLLO_PERSON})

    app.dependency_overrides[get_apollo_adapter] = lambda: _adapter(handler)
    response = client.post(
        "/apollo/enrich_person", json={"name": "Grace Hopper", "domain": "navy.mil"}
    )
    assert response.status_code == 200
    lead = response.json()
    assert lead["email"] == "grace@navy.mil"
    assert lead["source"] == "apollo"

    # Enriching again updates the same lead instead of duplicating
    again = client.post(
        "/apollo/enrich_person", json={"name": "Grace Hopper", "domain": "navy.mil"}
    )
    assert again.json()["id"] == lead["id"]
    assert len(client.get("/leads").json()) == 1
