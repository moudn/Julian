"""Lead research: gather raw material about a lead's company, then let the
LLM distill it into citable facts.

Two free-tier sources:
  1. the company website homepage (from the lead's domain), and
  2. recent news via a web search API (Tavily by default).

Both return UNTRUSTED content — a lead's site or a news page could contain
text aimed at the model. The distillation prompt (in llm.py) treats all
gathered material as data, never instructions, and is told never to invent
facts beyond what was found.
"""

import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>",
                              re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
MAX_MATERIAL_CHARS = 3500
MAX_REDIRECTS = 5


def _host_is_public(host: str) -> bool:
    """Reject SSRF targets: private, loopback, link-local, or metadata IPs."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _safe_to_fetch(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return False
    return _host_is_public(host)


def html_to_text(raw: str) -> str:
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", without_scripts)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


class LeadResearcher:
    def __init__(self, llm, client: httpx.Client | None = None):
        settings = get_settings()
        self.llm = llm
        self.timeout = settings.research_timeout_seconds
        self.search_api_key = settings.search_api_key
        self.search_base_url = settings.search_base_url.rstrip("/")
        # Redirects are followed manually (see _get_guarded) so every hop is
        # re-checked against the SSRF guard. Letting httpx follow them meant
        # a lead's own domain could 302 into the cloud metadata service.
        self._client = client or httpx.Client(
            timeout=self.timeout, follow_redirects=False,
            headers={"User-Agent": "JulianResearch/1.0"})

    # ---------- public ----------

    def research(self, lead, org) -> dict:
        """Return {"notes": str, "sources": [url]}; notes empty if nothing
        useful was found. Never raises — research is best-effort."""
        materials: list[tuple[str, str]] = []
        sources: list[str] = []

        website = self._fetch_website(lead)
        if website:
            text, url = website
            materials.append((f"{lead.company or 'Company'} website", text))
            sources.append(url)

        for item in self._search_news(lead):
            materials.append((f"News: {item['title']}", item["content"]))
            sources.append(item["url"])

        if not materials:
            return {"notes": "", "sources": []}

        try:
            notes = self.llm.research_summary(lead, org, materials)
        except Exception as exc:  # distillation must never break the pipeline
            logger.warning("research distillation failed for lead %s: %s",
                           getattr(lead, "id", "?"), exc)
            return {"notes": "", "sources": []}
        return {"notes": notes, "sources": sources if notes else []}

    # ---------- sources ----------

    def _domain_for(self, lead) -> str | None:
        if lead.domain:
            return lead.domain.strip().lower().removeprefix("www.")
        if lead.email and "@" in lead.email:
            candidate = lead.email.split("@", 1)[1].strip().lower()
            # skip free mailbox providers — their homepage tells us nothing
            if candidate not in ("gmail.com", "outlook.com", "hotmail.com",
                                 "yahoo.com", "icloud.com", "proton.me"):
                return candidate
        return None

    def _get_guarded(self, url: str) -> httpx.Response | None:
        """GET a URL, re-running the SSRF guard on every redirect hop.

        The guard is only meaningful if it sees every address actually
        contacted. A lead's domain is attacker-controllable input, so a
        public host that 302s to 169.254.169.254 would otherwise pull cloud
        credentials straight into the lead's research notes.
        """
        for _ in range(MAX_REDIRECTS + 1):
            if not _safe_to_fetch(url):
                logger.info("blocked unsafe research URL: %s", url)
                return None
            response = self._client.get(url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return None
                # Relative redirects resolve against the URL just fetched.
                url = str(httpx.URL(url).join(location))
                continue
            response.raise_for_status()
            return response
        logger.info("too many redirects while researching %s", url)
        return None

    def _fetch_website(self, lead) -> tuple[str, str] | None:
        domain = self._domain_for(lead)
        if not domain:
            return None
        try:
            response = self._get_guarded(f"https://{domain}")
        except httpx.HTTPError as exc:
            logger.info("website fetch failed (%s): %s", domain, exc)
            return None
        if response is None:
            return None
        text = html_to_text(response.text)[:MAX_MATERIAL_CHARS]
        return (text, str(response.url)) if text else None

    def _search_news(self, lead) -> list[dict]:
        if not self.search_api_key or not lead.company:
            return []
        query = f"{lead.company} company news funding launch hiring"
        try:
            response = self._client.post(
                f"{self.search_base_url}/search",
                json={
                    "api_key": self.search_api_key,
                    "query": query,
                    "max_results": 4,
                    "search_depth": "basic",
                    "topic": "news",
                },
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("news search failed for %r: %s", lead.company, exc)
            return []
        return [
            {"title": r.get("title", ""),
             "url": r.get("url", ""),
             "content": (r.get("content") or "")[:MAX_MATERIAL_CHARS]}
            for r in results if r.get("content")
        ]
