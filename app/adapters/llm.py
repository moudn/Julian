"""LLM adapter for personalized outreach drafts, via OpenRouter.

Falls back to a deterministic template when no OPENROUTER_API_KEY is set so
the workflow can be exercised end-to-end in development.
"""

import httpx

from app.config import get_settings
from app.models import Lead

SYSTEM_PROMPT = (
    "You are a sales development representative writing a short, personalized "
    "first-touch email. Be specific to the recipient's role and company, keep "
    "it under 120 words, end with a soft call to action to schedule a call, "
    "and do not invent facts. Return only the email body."
)


class LLMError(Exception):
    pass


class OpenRouterAdapter:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self.base_url = settings.openrouter_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60)

    def generate_first_touch_email(self, lead: Lead) -> str:
        if not self.api_key:
            return self._template_fallback(lead)

        prompt = (
            f"Write a first-touch email to {lead.name}"
            f"{f', {lead.title}' if lead.title else ''}"
            f"{f' at {lead.company}' if lead.company else ''}."
            f"{f' They are based in {lead.location}.' if lead.location else ''}"
        )
        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(f"OpenRouter request failed: {exc}") from exc

    @staticmethod
    def _template_fallback(lead: Lead) -> str:
        role = f" as {lead.title}" if lead.title else ""
        company = f" at {lead.company}" if lead.company else ""
        return (
            f"Hi {lead.name.split()[0]},\n\n"
            f"I came across your work{role}{company} and thought there might be a "
            "good fit with what we're building. Teams like yours use us to cut "
            "manual outreach work while keeping every touchpoint personal.\n\n"
            "Would you be open to a quick 30-minute call to see if it's relevant? "
            "Happy to work around your calendar.\n\nBest regards"
        )
