> **DRAFT — not legal advice.** Have this reviewed by qualified counsel
> before publishing. Replace all [BRACKETED] placeholders.

# Data Processing Agreement — [PRODUCT NAME]

This DPA forms part of the agreement between [COMPANY LEGAL NAME]
("Processor", "we") and the customer accepting it ("Controller", "you")
and applies wherever we process personal data on your behalf.

## 1. Roles and scope

You are the Controller of personal data concerning your leads and
contacts ("Customer Personal Data"); we are your Processor. This DPA
covers processing performed by the [PRODUCT NAME] service as described in
Annex 1.

## 2. Our obligations

We will:

1. process Customer Personal Data only on your documented instructions
   (given via the Service's settings and features), unless required by law;
2. ensure persons authorized to process the data are bound by
   confidentiality;
3. implement appropriate technical and organizational measures (Annex 2);
4. engage sub-processors only under §4;
5. assist you, taking into account the nature of processing, with data
   subject requests (the Service provides per-lead export and erasure,
   and automatic permanent opt-out handling);
6. assist you with security, breach notification, and impact-assessment
   obligations;
7. notify you without undue delay after becoming aware of a personal data
   breach affecting Customer Personal Data;
8. at termination, delete or return Customer Personal Data (retaining
   only suppression-list entries needed to honor do-not-contact requests,
   and data we must keep by law);
9. make available information reasonably necessary to demonstrate
   compliance, and allow audits not more than once per year on [30] days'
   notice, at your cost.

## 3. Your obligations

You warrant that you have a lawful basis for the processing you instruct
(including B2B outreach to the leads you import), that your instructions
comply with applicable law, and that you will respond to data subjects
exercising their rights against you as Controller.

## 4. Sub-processors

You authorize the sub-processors listed in Annex 3. We will notify you at
least [14] days before adding or replacing sub-processors; you may object
on reasonable data-protection grounds, in which case either party may
terminate the affected service. We remain liable for our sub-processors'
performance.

## 5. International transfers

Where processing involves transfers of EU/UK personal data to countries
without an adequacy decision, the parties incorporate the European
Commission's Standard Contractual Clauses (Module 2: controller →
processor), and the UK Addendum where applicable.

## 6. Liability

Liability under this DPA is subject to the limitations in the Terms of
Service, except where data protection law does not permit limitation.

---

## Annex 1 — Processing details

- **Subject matter:** operation of an AI-assisted sales outreach and
  scheduling service.
- **Duration:** the term of the customer agreement.
- **Nature and purpose:** storing lead records; generating outreach
  drafts with AI models; sending emails from the Controller's connected
  account; receiving and classifying replies; scheduling meetings;
  maintaining suppression lists.
- **Categories of data subjects:** the Controller's prospective and
  existing business contacts; the Controller's users.
- **Categories of data:** names, business contact details, job titles,
  employers, correspondence content, meeting details. No special
  categories are intended to be processed.

## Annex 2 — Technical and organizational measures

Encryption in transit (TLS); encryption at rest of OAuth credentials
(AES-based Fernet); salted password hashing (PBKDF2-SHA256); hashed API
keys with revocation; per-tenant data isolation enforced at the
application layer; single-use expiring OAuth state tokens; rate limiting;
security headers; principle-of-least-privilege access; logging that
excludes message bodies; daily database backups; documented breach
response targeting notification within 72 hours.

## Annex 3 — Authorized sub-processors

| Sub-processor | Purpose | Location |
|---|---|---|
| [HOSTING PROVIDER] | application and database hosting | [REGION] |
| Google LLC | calendar, email sending/receiving (Customer-connected) | USA/global |
| [LLM PROVIDER(S)] | AI drafting and reply triage (no-training terms) | [REGION] |
| Stripe, Inc. | subscription billing | USA/global |
| [EMAIL PROVIDER] | transactional notifications | [REGION] |
