"""Subscription plan definitions: what each tier is called and how many
leads it includes per billing period. Which Stripe price maps to which
plan lives in Settings (stripe_price_id_<plan>) since that's deployment
config, not product logic.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str
    label: str
    lead_limit: int
    # Display price only (GBP/month) — the actual charge is whatever the
    # matching Stripe Price is configured to be; keep these in sync by hand.
    price_gbp: int


PLANS: dict[str, Plan] = {
    "starter": Plan(id="starter", label="Starter", lead_limit=25, price_gbp=99),
    "growth": Plan(id="growth", label="Growth", lead_limit=100, price_gbp=249),
    "scale": Plan(id="scale", label="Scale", lead_limit=300, price_gbp=499),
}
