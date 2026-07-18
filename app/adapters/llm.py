"""Julian's outreach writing engine (OpenRouter-backed).

Encodes research-backed cold email practice:
- First touch: PAS (problem -> agitate -> solve), under 80 words, single CTA,
  anchored to the recipient's role/company. PAS outperforms other frameworks
  on first touch because it earns relevance before asking for anything.
- Follow-ups capture ~42% of total replies. Cadence: bump with proof (day 3),
  value-add with no ask (day 7), polite breakup (day 12) — the breakup email
  has the highest reply rate of the sequence.
- Style: conversational with contractions, plain text, short sentences,
  sentence-case subject under 50 characters, no spam-trigger phrasing.

Falls back to deterministic templates when no OPENROUTER_API_KEY is set so
the workflow can be exercised end-to-end in development.
"""

import json
import re

import httpx

from app.config import get_settings
from app.models import Lead, Organization

# Common spam-filter trigger phrases; drafts are linted against these and
# the LLM is asked to rewrite if any appear.
SPAM_TRIGGER_PHRASES = [
    "act now", "buy now", "order now", "click here", "limited time",
    "limited offer", "urgent", "don't miss", "once in a lifetime",
    "100% free", "100% guaranteed", "guaranteed", "risk-free", "no risk",
    "no obligation", "no strings attached", "money back", "cash bonus",
    "earn money", "make money", "double your", "free trial", "free access",
    "special promotion", "exclusive deal", "amazing offer", "incredible deal",
    "winner", "congratulations", "dear friend", "this isn't spam",
    "not spam", "increase sales", "increase revenue overnight",
]

SEQUENCE_CADENCE = {  # step -> days after previous acceptance into sequence
    1: 0,
    2: 3,
    3: 7,
    4: 12,
}

STEP_GUIDANCE = {
    1: (
        "First touch. Use the PAS framework: open with a specific problem "
        "someone in the recipient's role at their kind of company faces, "
        "agitate it in one sentence (cost/pain of ignoring it), then present "
        "the sender's offering as the solve in one sentence. Under 80 words. "
        "End with a single low-friction question CTA (e.g. asking if this is "
        "a priority, or offering to send times for a short call). Never open "
        "with 'My name is' or 'I hope this finds you well'."
    ),
    2: (
        "Bump with proof, sent a few days after no reply. Reference the "
        "previous note in half a sentence, then add ONE new piece of value: "
        "a concrete result, mini case study, or benchmark relevant to their "
        "role. Under 60 words. Same single CTA, phrased differently."
    ),
    3: (
        "Value-add touch, sent about a week in. Give something useful with "
        "NO ask: an insight, benchmark, or resource relevant to their role "
        "and company type. One soft closing line that leaves the door open. "
        "Under 70 words."
    ),
    4: (
        "Breakup email. Politely acknowledge the timing may be wrong and say "
        "you'll stop reaching out. No guilt-tripping. Offer one final "
        "specific piece of value or an easy way to re-engage later. Under 50 "
        "words. This note gets the highest reply rate of the sequence — keep "
        "it warm and graceful."
    ),
}

SYSTEM_PROMPT = """You are Julian, an expert sales development writer. You write cold outreach emails that real busy people actually answer.

Non-negotiable rules:
- Sound like one human writing to another: contractions, plain words, short sentences. Read-aloud natural. Never robotic or salesy.
- Be specific to the recipient: their role, company, industry. Never generic flattery ("I love what you're doing").
- One idea per email, ONE call to action, never two.
- Subject line: sentence case, under 50 characters, specific and honest — never clickbait, never ALL CAPS, at most zero exclamation marks.
- Plain text only. No bullet lists, no links unless given one, no signatures beyond a first name.
- Never invent facts, metrics, case studies, or customer names not provided to you. If you lack a real proof point, write around it.
- Never use spam-trigger phrasing (act now, guaranteed, risk-free, limited time, 100% free, click here, etc.).
- Do not mention being an AI.

Return ONLY valid JSON: {"subject": "...", "body": "..."}. The body ends with the sender's first name only."""


class LLMError(Exception):
    pass


def lint_spam_phrases(text: str) -> list[str]:
    """Return spam-trigger phrases present in the text (case-insensitive)."""
    lowered = text.lower()
    return [phrase for phrase in SPAM_TRIGGER_PHRASES if phrase in lowered]


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


class OpenRouterAdapter:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self.base_url = settings.openrouter_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60)

    # ---------- public API ----------

    def generate_step(self, lead: Lead, org: Organization, step: int,
                      prior_bodies: list[str] | None = None) -> dict:
        """Generate one sequence step. Returns {subject, body, spam_flags}."""
        if not self.api_key:
            draft = _template_step(lead, org, step)
        else:
            draft = self._generate_via_api(lead, org, step, prior_bodies or [])
            flags = lint_spam_phrases(draft["subject"] + " " + draft["body"])
            if flags:  # one corrective rewrite, then accept best effort
                draft = self._generate_via_api(
                    lead, org, step, prior_bodies or [],
                    correction=f"Your previous draft contained spam-trigger "
                               f"phrases: {', '.join(flags)}. Rewrite without them.",
                )
        draft["spam_flags"] = lint_spam_phrases(draft["subject"] + " " + draft["body"])
        return draft

    def generate_first_touch_email(self, lead: Lead, org: Organization) -> str:
        """Backward-compatible single first-touch body."""
        return self.generate_step(lead, org, step=1)["body"]

    # ---------- internals ----------

    def _generate_via_api(self, lead: Lead, org: Organization, step: int,
                          prior_bodies: list[str], correction: str = "") -> dict:
        sender_line = (
            f"Sender: a sales rep at {org.name}."
            + (f" What they sell: {org.product_description}" if org.product_description
               else " (No product description configured — keep the offering "
                    "generic but concrete.)")
        )
        recipient_line = (
            f"Recipient: {lead.name}"
            + (f", {lead.title}" if lead.title else "")
            + (f" at {lead.company}" if lead.company else "")
            + (f" ({lead.company_size} employees)" if lead.company_size else "")
            + (f", based in {lead.location}" if lead.location else "")
            + "."
        )
        prior = ""
        if prior_bodies:
            prior = "Earlier emails in this sequence (do not repeat their "
            prior += "angle or wording):\n"
            prior += "\n---\n".join(prior_bodies)

        user_prompt = "\n\n".join(filter(None, [
            f"Write sequence email #{step}. {STEP_GUIDANCE[step]}",
            sender_line,
            recipient_line,
            prior,
            correction,
        ]))

        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(f"OpenRouter request failed: {exc}") from exc

        return _parse_draft(content)


def _parse_draft(content: str) -> dict:
    """Extract {"subject", "body"} from an LLM response, tolerating fences."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        return {"subject": str(data["subject"]).strip(),
                "body": str(data["body"]).strip()}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {"subject": str(data["subject"]).strip(),
                    "body": str(data["body"]).strip()}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    raise LLMError("LLM response was not valid draft JSON")


def _template_step(lead: Lead, org: Organization, step: int) -> dict:
    """Deterministic no-API-key fallback following the same frameworks."""
    first = lead.name.split()[0]
    role = lead.title or "your role"
    company = lead.company or "your team"
    offering = org.product_description or "what we're building"

    if step == 1:
        return {
            "subject": f"Manual outreach at {company}",
            "body": (
                f"Hi {first},\n\n"
                f"Most people in {role} tell us outreach and follow-up eat "
                f"hours every week that should go to closing. Left alone, it "
                f"only compounds as the pipeline grows.\n\n"
                f"That's the problem we work on: {offering}.\n\n"
                f"Is this on your radar this quarter?\n\nJulian"
            ),
        }
    if step == 2:
        return {
            "subject": f"One thought for {company}",
            "body": (
                f"Hi {first},\n\n"
                f"Following up on my last note. Teams like {company} usually "
                f"see the tedious parts of outreach drop dramatically once "
                f"it's automated with a human check on the important bits.\n\n"
                f"Worth a quick chat?\n\nJulian"
            ),
        }
    if step == 3:
        return {
            "subject": "A benchmark you might find useful",
            "body": (
                f"Hi {first},\n\n"
                f"No ask here — just sharing what we see across teams like "
                f"{company}: the first follow-up captures a large share of "
                f"replies most teams never collect because nobody sends it.\n\n"
                f"Happy to share more anytime.\n\nJulian"
            ),
        }
    return {
        "subject": "Closing the loop",
        "body": (
            f"Hi {first},\n\n"
            f"Sounds like the timing isn't right, so I'll stop here. If "
            f"outreach ever becomes a priority at {company}, my door's "
            f"open.\n\nAll the best,\nJulian"
        ),
    }
