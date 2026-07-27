"""Lead ingestion: CSV import and upsert of externally-sourced lead data."""

import csv
import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead

# CSV headers (case-insensitive) accepted for each Lead field
CSV_FIELD_ALIASES: dict[str, list[str]] = {
    "name": ["name", "full_name", "full name"],
    "email": ["email", "email_address", "email address"],
    "company": ["company", "organization", "company_name"],
    "title": ["title", "job_title", "job title", "position"],
    "phone": ["phone", "phone_number"],
    "location": ["location", "city"],
    "linkedin_url": ["linkedin_url", "linkedin"],
    "domain": ["domain", "company_domain", "website"],
    "company_size": ["company_size", "employees", "company size"],
}


def _extract_fields(row: dict[str, str]) -> dict[str, Any]:
    normalized = {(key or "").strip().lower(): (value or "").strip()
                  for key, value in row.items()}
    fields: dict[str, Any] = {}
    for field, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if normalized.get(alias):
                fields[field] = normalized[alias]
                break
    if not fields.get("name"):
        # Most CRM/prospecting exports (Apollo, HubSpot, LinkedIn) ship
        # separate first/last name columns rather than a single "name".
        first = next((normalized.get(a) for a in ("first_name", "first name",
                                                  "firstname", "given name")
                      if normalized.get(a)), "")
        last = next((normalized.get(a) for a in ("last_name", "last name",
                                                 "surname", "family name")
                     if normalized.get(a)), "")
        combined = " ".join(part for part in (first, last) if part)
        if combined:
            fields["name"] = combined
    if "company_size" in fields:
        try:
            fields["company_size"] = int(fields["company_size"])
        except ValueError:
            del fields["company_size"]
    return fields


MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 5000

NAME_COLUMNS = {"first_name", "first name", "firstname", "given name",
                "last_name", "last name", "surname", "family name"}


def _sniff_delimiter(text: str) -> str:
    """Excel writes ';' (and sometimes tab) depending on the user's locale.
    Reading such a file as comma-separated yields one giant column and every
    row looks like it's missing a name."""
    header = text.splitlines()[0] if text.splitlines() else ""
    counts = {d: header.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _recognised_columns(fieldnames) -> bool:
    present = {(f or "").strip().lower() for f in fieldnames}
    known = {alias for aliases in CSV_FIELD_ALIASES.values() for alias in aliases}
    return bool(present & (known | NAME_COLUMNS))


def import_leads_csv(db: Session, content: bytes, org_id: int) -> tuple[int, int, list[str]]:
    """Parse a CSV file and create leads for one organization.

    Returns (imported, skipped, errors). Suppressed addresses (prior
    opt-outs) are never re-imported.
    """
    if len(content) > MAX_CSV_BYTES:
        return 0, 0, [f"File too large (max {MAX_CSV_BYTES // (1024 * 1024)} MB)"]
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return 0, 0, ["File is not valid UTF-8 text"]

    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff_delimiter(text))
    if not reader.fieldnames:
        return 0, 0, ["CSV file is empty"]
    if not _recognised_columns(reader.fieldnames):
        return 0, 0, [
            "No recognisable columns. The header row needs at least a name "
            "column (name, or first_name + last_name) — found: "
            + ", ".join(f or "" for f in reader.fieldnames)[:200]
        ]

    from app.services.suppression import is_suppressed

    imported, skipped, errors = 0, 0, []
    seen_emails: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        if line_number - 1 > MAX_CSV_ROWS:
            errors.append(f"Stopped at {MAX_CSV_ROWS} rows (file truncated)")
            break
        fields = _extract_fields(row)
        if not fields.get("name"):
            skipped += 1
            errors.append(f"line {line_number}: missing name")
            continue
        email = fields.get("email")
        if email and is_suppressed(db, org_id, email):
            skipped += 1
            errors.append(f"line {line_number}: {email} previously opted out")
            continue
        if email and (email in seen_emails
                      or db.scalar(select(Lead).where(
                          Lead.email == email, Lead.org_id == org_id))):
            skipped += 1
            errors.append(f"line {line_number}: duplicate email {email}")
            continue
        if email:
            seen_emails.add(email)
        db.add(Lead(**fields, source="csv", org_id=org_id))
        imported += 1

    db.commit()
    return imported, skipped, errors


def upsert_lead(db: Session, data: dict[str, Any], org_id: int) -> Lead:
    """Create or update one org's Lead from normalized external data (e.g. Apollo).

    Matches on email when available; enrichment never clears existing values.
    """
    lead = None
    if data.get("email"):
        lead = db.scalar(select(Lead).where(
            Lead.email == data["email"], Lead.org_id == org_id))
    if lead is None and data.get("name") and data.get("domain"):
        lead = db.scalar(select(Lead).where(
            Lead.name == data["name"], Lead.domain == data["domain"],
            Lead.org_id == org_id))

    if lead is None:
        lead = Lead(**{key: value for key, value in data.items() if value is not None},
                    org_id=org_id)
        db.add(lead)
    else:
        for key, value in data.items():
            if value is not None and key != "source":
                setattr(lead, key, value)

    db.commit()
    db.refresh(lead)
    return lead
