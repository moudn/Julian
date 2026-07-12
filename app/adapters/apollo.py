"""Adapter for the Apollo.io REST API.

Exposes the two MCP-style functions required by the agent:
  - search_people: find leads matching title/location/domain filters
  - enrich_person: match a person by name + company domain and return
    enriched contact data (title, email, LinkedIn)

Both return normalized dicts shaped like our Lead model so callers can
upsert them directly.
"""

from typing import Any

import httpx

from app.config import get_settings


class ApolloError(Exception):
    pass


class ApolloAdapter:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.apollo_api_key
        self.base_url = (base_url or settings.apollo_base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ApolloError("APOLLO_API_KEY is not configured")
        try:
            response = self._client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={
                    "X-Api-Key": self.api_key,
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApolloError(
                f"Apollo API returned {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ApolloError(f"Apollo API request failed: {exc}") from exc
        return response.json()

    def search_people(
        self,
        titles: list[str] | None = None,
        locations: list[str] | None = None,
        organization_domains: list[str] | None = None,
        keywords: str | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """Search Apollo for people matching the given filters.

        Returns a list of normalized lead dicts.
        """
        payload: dict[str, Any] = {"page": page, "per_page": per_page}
        if titles:
            payload["person_titles"] = titles
        if locations:
            payload["person_locations"] = locations
        if organization_domains:
            payload["q_organization_domains"] = "\n".join(organization_domains)
        if keywords:
            payload["q_keywords"] = keywords

        data = self._post("/mixed_people/search", payload)
        return [self._normalize_person(p) for p in data.get("people", [])]

    def enrich_person(self, name: str, domain: str) -> dict[str, Any]:
        """Match a single person by name and company domain.

        Returns a normalized lead dict with enriched fields (title, email,
        LinkedIn URL, ...). Raises ApolloError if no match is found.
        """
        data = self._post(
            "/people/match",
            {"name": name, "domain": domain, "reveal_personal_emails": False},
        )
        person = data.get("person")
        if not person:
            raise ApolloError(f"Apollo found no match for {name!r} at {domain!r}")
        return self._normalize_person(person)

    @staticmethod
    def _normalize_person(person: dict[str, Any]) -> dict[str, Any]:
        organization = person.get("organization") or {}
        name = person.get("name") or " ".join(
            part for part in (person.get("first_name"), person.get("last_name")) if part
        )
        email = person.get("email")
        if email in ("email_not_unlocked@domain.com", ""):
            email = None
        return {
            "name": name,
            "email": email,
            "title": person.get("title"),
            "company": organization.get("name"),
            "domain": organization.get("primary_domain"),
            "company_size": organization.get("estimated_num_employees"),
            "location": ", ".join(
                part for part in (person.get("city"), person.get("state"), person.get("country"))
                if part
            ) or None,
            "linkedin_url": person.get("linkedin_url"),
            "phone": (person.get("sanitized_phone") or None),
            "source": "apollo",
        }
