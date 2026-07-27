"""The do-not-contact list: visibility, deliberate removal, tenant isolation."""

import io

from tests.conftest import signup

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _opted_out_lead(client) -> str:
    """Import a lead and have them opt out, landing them on the list."""
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    client.post("/leads/1/score")
    client.post("/leads/1/generate_sequence")
    client.post("/replies/ingest", json={"lead_id": 1,
                                         "body": "unsubscribe me please"})
    return "ada@acme.io"


def _opt_out_for(client, headers):
    """Drive a lead to an opted-out state for the org holding `headers`.

    The lead must be past NEW for a reply to be triaged at all, so it gets
    scored and drafted first.
    """
    client.post("/leads/import", headers=headers, files={
        "file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", headers=headers, json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60})
    lead_id = client.get("/leads", headers=headers).json()[0]["id"]
    client.post(f"/leads/{lead_id}/score", headers=headers)
    client.post(f"/leads/{lead_id}/generate_sequence", headers=headers)
    client.post("/replies/ingest", headers=headers,
                json={"lead_id": lead_id, "body": "unsubscribe me please"})
    return lead_id


def test_optouts_appear_on_the_list_with_a_reason(client):
    email = _opted_out_lead(client)
    entries = client.get("/suppressions").json()
    assert [e["email"] for e in entries] == [email]
    assert entries[0]["reason"] == "unsubscribed"
    assert entries[0]["reason_label"] == "Asked to stop being contacted"


def test_empty_list_when_nobody_opted_out(client):
    assert client.get("/suppressions").json() == []


def test_removing_an_entry_allows_importing_that_address_again(client):
    """The actual reason this exists: an address on the list can't be
    re-imported, and before this there was no way to undo that."""
    email = _opted_out_lead(client)
    blocked = client.post("/leads/import", files={
        "file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")}).json()
    assert blocked["imported"] == 0
    assert "opted out" in blocked["errors"][0]

    entry = client.get("/suppressions").json()[0]
    assert client.delete(f"/suppressions/{entry['id']}").status_code == 204
    assert client.get("/suppressions").json() == []

    # The opted-out lead row still exists in a terminal state and still owns
    # that email, so re-importing now trips the duplicate check instead —
    # suppression is no longer what's blocking it.
    still_blocked = client.post("/leads/import", files={
        "file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")}).json()
    assert "duplicate email" in still_blocked["errors"][0]

    # Clearing the stale row (without re-suppressing) frees the address.
    client.delete("/leads/1?suppress=false")
    allowed = client.post("/leads/import", files={
        "file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")}).json()
    assert allowed["imported"] == 1, allowed
    assert client.get("/suppressions").json() == []  # delete didn't re-add it


def test_suppression_list_is_per_org(anon_client):
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    _opt_out_for(anon_client, headers_a)

    assert len(anon_client.get("/suppressions", headers=headers_a).json()) == 1
    # B must not see A's opt-outs — they reveal who A is prospecting
    assert anon_client.get("/suppressions", headers=headers_b).json() == []


def test_org_cannot_remove_another_orgs_suppression(anon_client):
    """Un-suppressing across tenants would let one org re-enable mailing to
    someone who opted out of a different org entirely."""
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    _opt_out_for(anon_client, headers_a)
    entry_id = anon_client.get("/suppressions", headers=headers_a).json()[0]["id"]

    assert anon_client.delete(f"/suppressions/{entry_id}",
                              headers=headers_b).status_code == 404
    # still suppressed for A
    assert len(anon_client.get("/suppressions", headers=headers_a).json()) == 1


def test_suppressions_require_auth(anon_client):
    assert anon_client.get("/suppressions").status_code == 401
    assert anon_client.delete("/suppressions/1").status_code == 401


def test_delete_lead_then_unsuppress_frees_the_address_for_reuse(client):
    """The whole round trip a customer performs from the dashboard when they
    want to contact an address again: delete the lead (which suppresses it),
    then take it off the do-not-contact list."""
    _opted_out_lead(client)

    client.delete("/leads/1")                      # UI's Delete lead button
    entry = client.get("/suppressions").json()[0]
    client.delete(f"/suppressions/{entry['id']}")  # UI's Remove button

    result = client.post("/leads/import", files={
        "file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")}).json()
    assert result["imported"] == 1, result
    assert client.get("/leads").json()[0]["state"] == "NEW"
