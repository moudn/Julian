"""Dashboard serving: static SPA mounted at /app, root redirects there."""


def test_root_redirects_to_dashboard(anon_client):
    response = anon_client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/app/"


def test_dashboard_serves_spa(anon_client):
    response = anon_client.get("/app/")
    assert response.status_code == 200
    assert "Julian" in response.text
    assert 'src="app.js"' in response.text

    for asset in ("app.js", "styles.css"):
        assert anon_client.get(f"/app/{asset}").status_code == 200


def test_dashboard_does_not_shadow_api(anon_client):
    assert anon_client.get("/health").json() == {"status": "ok"}
    assert anon_client.get("/leads").status_code == 401  # API still guarded
