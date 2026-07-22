"""Generate LIVE Julian drafts for a lead, using your real API keys.

This is a local demo/QA tool — it does not touch the database or send any
email. It just shows you exactly what Julian would write.

Setup (once):
    1. Get an OpenRouter key (openrouter.ai) and, optionally, a Tavily key
       (tavily.com) for the news-research step.
    2. Put them in your .env (or export them):
         OPENROUTER_API_KEY=sk-or-...
         SEARCH_API_KEY=tvly-...        # optional; enables news research

Run:
    python scripts/try_julian.py

Then edit the SENDER / LEAD block below and re-run to try different leads.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adapters.llm import OpenRouterAdapter  # noqa: E402
from app.adapters.research import LeadResearcher  # noqa: E402
from app.models import Lead, Organization  # noqa: E402

# ---- edit me ----------------------------------------------------------------
ORG = Organization(
    name="FlowState",
    sender_name="Alex Rivera",
    product_description=("an AI scheduling assistant for consultancies that "
                         "cuts meeting-admin time by around 70%"),
    knowledge_base="Pricing starts at $40/user/month. SOC 2 Type II certified.",
    research_enabled=True,
)
LEAD = Lead(
    id=1, name="Sarah Chen", title="VP of Operations",
    company="Meridian Consulting", company_size=180, location="London",
    email="sarah@meridian.co", domain="meridian.co",
)
SAMPLE_REPLIES = [
    "This looks interesting — can you send some times?",
    "How much does it cost, and are you SOC 2 compliant?",
    "Not the right time for us, thanks.",
    "please unsubscribe me",
]
# -----------------------------------------------------------------------------


def main():
    llm = OpenRouterAdapter()
    if not llm.api_key:
        print("No OPENROUTER_API_KEY set — you'd get the template fallback. "
              "Set the key to see the real LLM output.\n")

    print("=" * 70)
    print("RESEARCH")
    print("=" * 70)
    researcher = LeadResearcher(llm=llm)
    result = researcher.research(LEAD, ORG)
    if result["notes"]:
        print(result["notes"])
        print("\nsources:", ", ".join(result["sources"]))
        LEAD.research_notes = result["notes"]  # feed into the writing below
    else:
        print("(no citable research found — Julian will write role-based)")

    print("\n" + "=" * 70)
    print("OUTREACH SEQUENCE")
    print("=" * 70)
    prior = []
    for step in (1, 2, 3, 4):
        draft = llm.generate_step(LEAD, ORG, step, prior)
        prior.append(draft["body"])
        print(f"\n----- STEP {step}  (day {[0, 3, 7, 12][step - 1]})")
        print(f"Subject: {draft['subject']}")
        print(draft["body"])
        if draft.get("spam_flags"):
            print(f"[spam flags: {draft['spam_flags']}]")

    print("\n" + "=" * 70)
    print("REPLY TRIAGE")
    print("=" * 70)
    for reply in SAMPLE_REPLIES:
        r = llm.classify_reply(LEAD, ORG, reply)
        print(f"\nLead: {reply!r}\n  category: {r['category']}")
        if r.get("answer"):
            print(f"  auto-answer (KB): {r['answer']}")
        if r.get("suggested_reply"):
            print(f"  suggested reply for the rep:\n    "
                  + r["suggested_reply"].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
