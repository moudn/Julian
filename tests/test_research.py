"""Lead research: website + news gathering, SSRF guard, distillation,
injection into drafts, and the org toggle."""

import io
import json

import httpx
import pytest

from app.adapters.research import LeadResearcher, _safe_to_fetch, html_to_text
from app.models import Lead, Organization

CSV = ("name,email,company,title,domain\n"
       "Ada Lovelace,ada@acme.io,Acme Robotics,VP of Engineering,acme.io\n")


# ---------- pure helpers ----------

def test_html_to_text_strips_scripts_and_tags():
    raw = "<html><head><style>.x{}</style><script>evil()</script></head>" \
          "<body><h1>Acme</h1><p>We build&nbsp;robots.</p></body></html>"
    text = html_to_text(raw)
    assert "Acme" in text and "We build robots." in text
    assert "evil" not in text and "{}" not in text


def test_ssrf_guard_blocks_private_and_bad_schemes():
    assert _safe_to_fetch("http://localhost/") is False
    assert _safe_to_fetch("https://127.0.0.1/") is False
    assert _safe_to_fetch("https://10.0.0.5/") is False
    assert _safe_to_fetch("https://169.254.169.254/") is False  # cloud metadata
    assert _safe_to_fetch("https://foo.local/") is False
    assert _safe_to_fetch("file:///etc/passwd") is False
    assert _safe_to_fetch("ftp://example.com/") is False
    # a normal public host passes (resolves to a public IP)
    assert _safe_to_fetch("https://example.com/") is True


# ---------- researcher orchestration ----------

class FakeLLM:
    def __init__(self, notes="- Acme Robotics raised a $12M Series A in June.\n"
                             "- They just opened a Berlin office."):
        self.notes = notes
        self.seen = None

    def research_summary(self, lead, org, materials):
        self.seen = materials
        return self.notes


def _researcher(handler, llm=None, search_key="test-search"):
    from app.config import get_settings
    monkey = get_settings()
    monkey.search_api_key = search_key
    transport = httpx.MockTransport(handler)
    return LeadResearcher(llm=llm or FakeLLM(),
                          client=httpx.Client(transport=transport,
                                              follow_redirects=True))


def test_research_gathers_site_and_news(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.tavily.com" in request.url.host:
            return httpx.Response(200, json={"results": [
                {"title": "Acme raises Series A", "url": "https://news.example/acme",
                 "content": "Acme Robotics raised $12M."}]})
        # company website
        return httpx.Response(200, text="<h1>Acme Robotics</h1><p>Warehouse robots.</p>")

    # bypass SSRF DNS check for the fake host
    monkeypatch.setattr("app.adapters.research._safe_to_fetch", lambda url: True)
    llm = FakeLLM()
    researcher = _researcher(handler, llm=llm)
    lead = Lead(id=1, name="Ada Lovelace", company="Acme Robotics",
                domain="acme.io", email="ada@acme.io", title="VP")
    org = Organization(name="Sender Co")

    result = researcher.research(lead, org)
    assert "Series A" in result["notes"]
    assert "https://news.example/acme" in result["sources"]
    assert any("acme.io" in s for s in result["sources"])
    # both a website block and a news block reached the distiller
    labels = [label for label, _ in llm.seen]
    assert any("website" in l.lower() for l in labels)
    assert any("news" in l.lower() for l in labels)


def test_research_returns_empty_when_nothing_found(monkeypatch):
    def handler(request):
        return httpx.Response(404)
    monkeypatch.setattr("app.adapters.research._safe_to_fetch", lambda url: True)
    researcher = _researcher(handler, search_key="")  # no news
    lead = Lead(id=1, name="X", company="Nope", domain="nope.test",
                email="x@nope.test")
    result = researcher.research(lead, Organization(name="Y"))
    assert result == {"notes": "", "sources": []}


def test_research_skips_freemail_domains(monkeypatch):
    called = {"fetch": False}

    def handler(request):
        called["fetch"] = True
        return httpx.Response(200, text="<p>hi</p>")
    monkeypatch.setattr("app.adapters.research._safe_to_fetch", lambda url: True)
    researcher = _researcher(handler, search_key="")
    lead = Lead(id=1, name="Bob", company=None, domain=None, email="bob@gmail.com")
    result = researcher.research(lead, Organization(name="Y"))
    assert result["notes"] == ""
    assert called["fetch"] is False  # never tried to fetch gmail.com


# ---------- distillation via real adapter ----------

def test_research_summary_returns_empty_on_none():
    def handler(request):
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "NONE"}}]})
    from app.adapters.llm import OpenRouterAdapter
    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    lead = Lead(name="A", company="C")
    assert llm.research_summary(lead, Organization(name="O"),
                                [("Website", "generic filler")]) == ""


def test_research_notes_flow_into_generation_prompt():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content":
            json.dumps({"subject": "hi", "body": "Hi Ada, saw the raise. Julian"})}}]})

    from app.adapters.llm import OpenRouterAdapter
    llm = OpenRouterAdapter(api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    lead = Lead(name="Ada Lovelace", company="Acme", title="VP",
                research_notes="- Raised $12M Series A in June.")
    llm.generate_step(lead, Organization(name="Sender"), step=1)
    assert "Series A" in captured["body"]
    assert "Researched facts" in captured["body"]


# ---------- endpoint + toggle integration ----------

def _import_and_score(client):
    client.post("/leads/import",
                files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")})
    client.post("/icp/rules", json={"name": "VP", "field": "title",
                                    "operator": "contains", "value": "VP",
                                    "weight": 60})
    client.post("/leads/1/score")
    return 1


def _install_fake_researcher(notes="- Acme just launched a new product."):
    from app.deps import get_researcher
    from app.main import app

    class FakeResearcher:
        def research(self, lead, org):
            return {"notes": notes, "sources": ["https://acme.io"]} if notes \
                else {"notes": "", "sources": []}

    app.dependency_overrides[get_researcher] = lambda: FakeResearcher()


def test_research_endpoint_stores_notes(client):
    _install_fake_researcher()
    lead_id = _import_and_score(client)
    result = client.post(f"/leads/{lead_id}/research").json()
    assert "launched a new product" in result["research_notes"]
    assert result["research_sources"] == ["https://acme.io"]
    assert result["researched_at"] is not None


def test_generate_sequence_auto_researches_when_enabled(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "research_enabled", True)
    _install_fake_researcher()
    lead_id = _import_and_score(client)

    client.post(f"/leads/{lead_id}/generate_sequence")
    lead = client.get(f"/leads/{lead_id}").json()
    assert "launched a new product" in (lead["research_notes"] or "")


def test_generate_sequence_skips_research_when_org_disabled(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "research_enabled", True)
    _install_fake_researcher()
    client.patch("/auth/org", json={"research_enabled": False})
    lead_id = _import_and_score(client)

    client.post(f"/leads/{lead_id}/generate_sequence")
    lead = client.get(f"/leads/{lead_id}").json()
    assert lead["research_notes"] is None
    assert lead["researched_at"] is None
