> **DRAFT — not legal advice.** Have this reviewed by qualified counsel
> before publishing. Replace all [BRACKETED] placeholders.

# Privacy Policy — [PRODUCT NAME]

**Last updated:** [DATE]
**Operator:** [COMPANY LEGAL NAME], [REGISTERED ADDRESS] ("we", "us")
**Contact:** [PRIVACY EMAIL]

[PRODUCT NAME] ("the Service") is an AI-assisted sales outreach platform
used by business customers ("Customers") to contact prospective clients
("Leads") and schedule meetings.

## 1. Two kinds of people, two roles

- **Customers** (account holders): we are the *data controller* for your
  account data.
- **Leads** (people our Customers contact): the Customer is the data
  controller; we process Lead data only on the Customer's instructions as
  a *data processor* under our [Data Processing Agreement](DATA_PROCESSING_AGREEMENT.md).

## 2. Data we process

**Customer account data:** name, email, password (stored as a salted
PBKDF2 hash — we cannot read it), organization details, settings, billing
status. Payment card details go directly to Stripe; we never see them.

**Lead data (on behalf of Customers):** names, work email addresses, job
titles, companies, locations; emails sent to and received from Leads;
meeting bookings. Lead data is imported by the Customer or retrieved from
data providers at the Customer's direction.

**Google user data:** when a Customer connects Google, we access their
calendar free/busy times and events we create, send email from their Gmail
address, and read replies from their Leads. OAuth tokens are encrypted at
rest. Our use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements. **We do not use Google user data to
train AI or machine-learning models.**

## 3. AI processing

The Service uses large language models to draft outreach emails and triage
replies. Lead names, roles, companies, and reply contents are transmitted
to our AI sub-processors for this purpose only. We contractually require
that this data is not used to train models. Automated replies are limited
to Customer-approved content and are off by default; substantive
conversations are handled by humans.

## 4. Sub-processors

[HOSTING PROVIDER] (infrastructure), [DATABASE PROVIDER], Google LLC
(calendar and email APIs), [LLM PROVIDER(S) — e.g. via OpenRouter, pinned
to no-training providers], Stripe, Inc. (payments), [EMAIL PROVIDER]
(transactional email). A current list is available on request.

## 5. Legal bases (GDPR)

Customer data: performance of contract; legitimate interest (service
operation, security); legal obligations. Lead data: processed under the
Customer's instructions; the Customer is responsible for their lawful
basis (typically legitimate interest for B2B outreach).

## 6. Retention & deletion

Customer accounts: retained while active, deleted within [30] days of a
deletion request. Lead data: retained under Customer control — Customers
can erase any Lead (including all messages) at any time; erased addresses
are kept on a minimal suppression list (email address only) to honor
do-not-contact requests. Opt-out requests from Leads are honored
immediately and permanently.

## 7. Your rights

Customers and Leads may request access, correction, deletion, or
portability of their personal data: [PRIVACY EMAIL]. Leads may also simply
reply to any email they received to opt out. EU/UK individuals may lodge a
complaint with their supervisory authority.

## 8. Security

Encryption in transit (TLS) and at rest for OAuth credentials; hashed
passwords and API keys; tenant isolation; rate limiting; access on a
need-to-know basis. In the event of a personal data breach we will notify
affected Customers and, where required, authorities within 72 hours.

## 9. International transfers

Data may be processed in [HOSTING REGION(S)]. Where data of EU/UK
individuals is transferred internationally, we rely on Standard
Contractual Clauses with our sub-processors.

## 10. Changes

We will notify Customers of material changes by email at least [14] days
before they take effect.
