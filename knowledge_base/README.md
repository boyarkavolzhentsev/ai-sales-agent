# Knowledge Base — authoring guide

Approved source material the AI Sales Agent may use to ground its replies.

**Rule:** only verified, approved information belongs here. If a question cannot be answered
from this knowledge base, the agent escalates to the operator instead of inventing an answer.

This directory currently contains no company data — only the domain structure and this guide.
All examples below are **fictional** ("Samplewidget Co" does not exist).

## 1. Knowledge domains

Put each document in the folder of its domain. The folder must match the `domain` field.

| # | Domain | `domain` value | Folder | What belongs here |
|---|--------|----------------|--------|-------------------|
| 1 | Company | `COMPANY` | `company/` | Company overview, mission, history, team, locations, general facts. |
| 2 | Products / Services | `PRODUCTS_SERVICES` | `products/` | Product and service descriptions, features, capabilities, limitations, specifications. |
| 3 | Pricing / Commercial Terms | `PRICING_COMMERCIAL` | `pricing/` | Approved price lists, plans, discount policy, payment and contract terms. |
| 4 | ICP / Target Customers | `ICP` | `icp/` | Ideal customer profiles, target segments, personas, qualification criteria. |
| 5 | Sales Playbooks | `SALES_PLAYBOOKS` | `sales_playbooks/` | Sales process, stages, qualification frameworks, recommended conversation flows. |
| 6 | FAQ | `FAQ` | `faq/` | Approved answers to frequently asked customer questions. |
| 7 | Objections | `OBJECTIONS` | `objections/` | Common objections and approved responses. |
| 8 | Case Studies | `CASE_STUDIES` | `case_studies/` | Customer stories and results cleared for external use. |
| 9 | Outbound Messaging | `OUTBOUND_MESSAGING` | `outbound_messaging/` | Approved outreach templates, value propositions, tone and style guidance. |
| 10 | Industry Knowledge | `INDUSTRY` | `industry/` | Market context, terminology, trends and pain points relevant to prospects. |
| 11 | Competitor / Alternative Positioning | `COMPETITORS` | `competitors/` | Approved positioning versus alternatives; what may and may not be claimed. |
| 12 | Legal / Compliance / Approved Communication | `LEGAL_COMPLIANCE` | `legal_compliance/` | Compliance rules, forbidden claims, required disclaimers, opt-out wording. |
| 13 | Contact / Routing Knowledge | `CONTACTS_ROUTING` | `contacts/` | Who (internally) handles what, escalation paths, routing rules. |
| 14 | Meeting / Next-Step Guidance | `MEETING_GUIDANCE` | `meeting_guidance/` | How to propose meetings/demos, booking rules, next steps per lead stage. |
| 15 | Approved Marketing Materials | `MARKETING_MATERIALS` | `marketing_materials/` | Brochures, one-pagers and links cleared for sharing with prospects. |

## 2. What must NOT be stored here

The knowledge base is for approved *content*. Never put operational data in it:

- lead or campaign state, send counts, quotas, statistics, follow-up schedules;
- do-not-contact / unsubscribe lists;
- prospect contact data (that is operational data, not knowledge);
- secrets, credentials, API keys;
- unverified claims, rumours, drafts you are not ready to approve (keep them `DRAFT`).

## 3. File formats

Supported extensions: `.md`, `.yaml` / `.yml`, `.json` (UTF-8). Anything else is rejected.
`README.md` and `.gitkeep` files are ignored.

### Narrative documents: Markdown + YAML front matter

```markdown
---
source_id: samplewidget-faq-onboarding
domain: FAQ
title: Onboarding FAQ (fictional example)
version: 1
approval_status: APPROVED
external_use: EXTERNAL_OK
approved_by: jane.approver
approved_at: 2026-01-10T09:00:00+00:00
effective_from: 2026-01-10T00:00:00+00:00
review_by: 2026-07-10T00:00:00+00:00
locale: en
tags: [onboarding]
---
# Onboarding

## Duration
Onboarding for the (fictional) Sample Widget usually takes two weeks.
```

Headings matter: the agent sees each paragraph together with its heading path
("Onboarding > Duration"), so use clear, specific headings.

### Fact-heavy documents: YAML or JSON with `metadata`, `facts`, optional `body`

Use these for prices, limits, SLAs and other exact values.

```yaml
metadata:
  source_id: samplewidget-price-list
  domain: PRICING_COMMERCIAL
  title: Sample Widget price list (fictional example)
  version: 3
  approval_status: APPROVED
  external_use: EXTERNAL_OK
  approved_by: jane.approver
  approved_at: 2026-02-01T09:00:00+00:00
  effective_from: 2026-02-01T00:00:00+00:00
  review_by: 2026-05-01T00:00:00+00:00
  locale: en
  tags: [pricing]
  product: sample-widget
facts:
  - key: plan.basic.monthly_price
    value: "100"          # always a quoted string
    unit: EUR
    statement: The Basic plan costs 100 EUR per month.
body: |
  ## Billing
  Invoices are issued monthly.
```

- `key`: lower-case, dotted (`plan.basic.monthly_price`); unique within the document.
- `value`: always a **quoted string**, written exactly as it may be quoted to a customer.
- `unit`, `statement`: optional; a clear `statement` sentence helps retrieval.
- JSON uses the same structure (`{"metadata": {...}, "facts": [...], "body": "..."}`).

## 4. Metadata

| Field | Required | Rules |
|-------|----------|-------|
| `source_id` | yes | Letters, digits, `-`, `_`; stable across versions of the same document. |
| `domain` | yes | One of the 15 values above, exactly as written (upper case). Must match the folder. |
| `title` | yes | Non-empty. |
| `version` | yes | Whole number ≥ 1 (not `"1"`, not `1.0`). |
| `approval_status` | yes | `DRAFT`, `APPROVED` or `RETIRED`. |
| `external_use` | yes | `EXTERNAL_OK` or `INTERNAL_ONLY`. |
| `effective_from` | yes | ISO 8601 date-time **with offset**, e.g. `2026-01-10T00:00:00+00:00`. |
| `review_by` | yes | Same format; must be after `effective_from`. |
| `locale` | yes | Language tag, e.g. `en`, `en-GB`, `uk`. |
| `tags` | yes | List (may be empty `[]`), no duplicates. |
| `approved_by`, `approved_at` | for `APPROVED` | Both or neither. Required for `APPROVED`; not allowed on `DRAFT`. `approved_at` may not be in the future. |
| `supersedes` | no | `source_id` of *another* document this one replaces; it must exist. |
| `product`, `industry`, `region`, `source_ref` | no | Stored as tags (`product:…`, `industry:…`, `region:…`, `ref:…`). |

Unknown fields are rejected, so typos never pass silently.

## 5. Approval and external use

The agent only uses a document to answer customers when **all** are true:

1. `approval_status: APPROVED`
2. `external_use: EXTERNAL_OK`
3. it is in force: `effective_from ≤ now ≤ review_by`
4. its `locale` language matches the conversation language

Everything else is excluded *before* searching and ranking, and has no influence at all on
which usable passages are found or how they are ordered. `INTERNAL_ONLY` documents are
never quoted to customers. When the only document that answers a question is unapproved,
internal-only or out of date, the agent escalates to the operator and says why.

**How passages are ranked.** Matching is by words, not meaning: a passage is found only if
it shares words with the question (no synonyms, no word stemming — "price" and "prices" are
different words). Matching passages are scored with the standard BM25 word-relevance formula,
computed only over the passages currently allowed for the conversation; passages that share
more of the question's words, and rarer words, score higher. Write documents using the words
customers actually use.

## 6. `review_by` (staleness)

`review_by` is the date by which a human must re-confirm the document. After that instant it
is **stale** and is no longer used, even if still approved. To keep using it, review it and
publish a new `version` with a new `review_by`. Pick short review periods for volatile
content (prices, offers) and longer ones for stable content (company history).

## 7. Versioning and `supersedes`

- Never edit an ingested version in place. Change the content → increase `version`.
  Re-ingesting the same `source_id` + `version` with different content is rejected.
- Old versions stay in the index for audit; the agent uses the **newest usable** version.
- A newer `DRAFT` does not replace the current approved version until it is approved.
- Retiring: publish a new version with `approval_status: RETIRED` — the whole document
  (all versions) stops being used.
- `supersedes: <other-source-id>` marks that this document replaces a *different* document;
  once this one is usable, the other one is no longer used.

## 8. Validation fails closed

Ingestion validates the whole folder before writing anything. Any of these stops the whole
ingestion run, with the file name in the error, and nothing is imported:

- unsupported file type, invalid UTF-8, missing or unclosed front matter, malformed YAML/JSON,
  duplicate keys;
- a missing, unknown or wrongly typed metadata field; inconsistent approval fields;
  `review_by` not after `effective_from`; date-times without an offset;
- a file outside a domain folder, or a folder that does not match `domain`;
- an empty document (no body and no facts), invalid or duplicate fact keys, unquoted fact values;
- the same `source_id` + `version` twice; `supersedes` pointing to an unknown document.

Nothing is repaired or guessed automatically — fix the file and ingest again.

## 9. Conflicting facts

If two usable documents state different values for the same fact `key` (compared as written,
ignoring upper/lower case), answers that rely on that fact are flagged `CONFLICTING` and
escalated. Keep each fact in exactly one current document. Conflicts in free text are not
detected automatically — another reason to put exact values into `facts`.
