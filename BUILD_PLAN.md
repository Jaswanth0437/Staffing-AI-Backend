# Job-to-Lead AI Outreach Platform — Build Plan

## Context

Solo hackathon build (backend + AI layer only; teammate owns the Next.js frontend against the API contract defined here). The pipeline discovers job postings via Apify, qualifies them with rules + AI, resolves a contact via Apollo, matches the opportunity to an internal employee (kept strictly internal), generates a de-identified capability-pitch email, and sends it via Resend. No real auth (single hardcoded demo user), no Celery/Redis (FastAPI BackgroundTasks only), no Alembic (SQLModel `create_all()` on startup). This plan confirms the schema, resolves the ambiguities that would otherwise cause rework mid-build, and lays out an ordered module sequence matching the stated priority (steps 1–6 required, step 7 dedup is the first thing to cut if time is short).

## Schema — confirmed, with decisions baked in

Decisions locked in from clarifying questions (rationale noted so they're not re-litigated mid-build):

- **emails**: one row per lead, overwritten in place by regenerate/edit (no version history). Simplest contract for the frontend — always GET the current draft.
- **contacts**: one row per lead — whichever tier (`job_poster` / `hr_contact` / `company_level`) actually resolved. `type` records which tier won. No rows for failed attempts.
- **qualifications**: one row per job, updated in place. Manual override via `POST /jobs/{id}/qualify` flips `status`/`reason`/`decided_by='manual'` on the same row — no separate audit trail of the original AI verdict.
- **campaigns.status / async pipeline**: `POST /campaigns` returns immediately after creating the row; Apify search + per-job qualification run as a `BackgroundTask`. `campaigns.status` enum: `pending → running → completed | failed`. Frontend polls `GET /campaigns/{id}` and `GET /campaigns/{id}/jobs`.
- **employee_matches**: multiple ranked rows per lead (top-N candidates from the AI ranking call), exactly one flips to `confirmed=true` via `POST /leads/{id}/confirm-employee`.

Table definitions (as specified, with the additions below):

```
campaigns
  id, name, search_criteria JSON, created_at, last_checked_at,
  status  -- pending | running | completed | failed

jobs
  id, campaign_id FK, external_job_id, title, company, location, description,
  posted_date, applicant_count, company_employee_size, raw_data JSON, created_at
  UNIQUE (campaign_id, external_job_id)   -- needed for step 7 dedup; cheap to add now, ignore until step 7 is built

qualifications
  id, job_id FK (1:1 with jobs), status (qualified|rejected), reason text,
  rule_flags JSON, decided_by (ai|manual), created_at

leads
  id, job_id FK, campaign_id FK, status, created_at
  -- status enum: new | contacted | replied | closed (exact values only matter for frontend badges — confirm with teammate, not a backend blocker)

contacts
  id, lead_id FK, type (job_poster|hr_contact|company_level), name, designation,
  linkedin_url, email, phone, source

employees
  id, name, role, skills JSON array, experience_summary, seniority

employee_matches
  id, lead_id FK, employee_id FK, match_score, ai_reasoning, confirmed bool

emails
  id, lead_id FK, employee_id FK, sender, recipient, subject, body,
  status (draft|sent|failed), sent_at

activity_log
  id, entity_type, entity_id, action, timestamp
```

No other structural changes needed — the original schema was sound; the additions are the dedup unique constraint and the two status enums.

## Ordered module build plan

Each module lists what it delivers and its hard dependency on the prior module. Build strictly top to bottom — nothing here parallelizes usefully for a solo build.

**M0 — Project skeleton**
FastAPI app, SQLModel models for all 9 tables, Postgres connection, `create_all()` on startup, hardcoded demo user constant, base router structure, activity_log helper (`log_activity(entity_type, entity_id, action)`) used by every later module. No endpoints yet beyond a health check.
- `config.py` — all cross-module tunables live here as named constants with placeholder defaults, so no later module re-decides or restates them:
  ```python
  # Qualification rules (M2)
  EMPLOYEE_SIZE_FLOOR = 10          # reject jobs at companies smaller than this
  APPLICANT_COUNT_CEILING = 200     # reject jobs with more applicants than this
  STAFFING_AGENCY_KEYWORDS = ["staffing", "recruiting", "recruitment", "talent solutions", "agency"]

  # Resend (M6)
  RESEND_SENDER_EMAIL = "outreach@yourdomain.com"   # must be a Resend-verified sending domain

  # Email generation (M5)
  COMPANY_NAME = "Winfomi Technologies"
  COMPANY_SERVICES = ["staff augmentation", "contract-to-hire", "dedicated engineering teams"]
  COMPANY_PITCH_TONE = "concise, consultative, no hard-sell"
  ```
  M2, M5, and M6 read from `config.py` rather than defining or re-deciding any of the above.

**M1 — Campaigns + Apify job search** *(pipeline step 1)*
- `POST /campaigns`: create row (`status=pending`), kick off BackgroundTask, return the campaign immediately.
- BackgroundTask: call Apify actor with `search_criteria`, insert raw results into `jobs`, flip campaign `status` to `running` → `completed`/`failed`.
- `GET /campaigns`, `GET /campaigns/{id}`.
Depends on: M0.

**M2 — Job qualification (rules + AI)** *(pipeline step 2)*
- Rule constants (`EMPLOYEE_SIZE_FLOOR`, `APPLICANT_COUNT_CEILING`, `STAFFING_AGENCY_KEYWORDS`) read from `config.py`; run first per job, reject short-circuits before the AI call to save tokens.
- AI call (Claude/OpenAI) for jobs that pass rules: nuanced qualify/reject + reason, written to `qualifications` alongside `rule_flags`.
- Wire into M1's BackgroundTask so qualification runs immediately after jobs are inserted.
- `GET /campaigns/{id}/jobs` (jobs joined with qualification status/reason).
- `POST /jobs/{id}/qualify` (manual override, updates the row per the decision above).
- **Response contract**: each job in `GET /campaigns/{id}/jobs` carries a `qualification_status` field with exactly one of three literal values — `"pending"` (no `qualifications` row yet, campaign still `running`), `"qualified"`, or `"rejected"`. `reason` is `null` for `pending`, populated for the other two.

  Pending (no qualifications row yet):
  ```json
  {
    "id": "job_123",
    "title": "Senior Backend Engineer",
    "company": "Acme Corp",
    "qualification_status": "pending",
    "reason": null
  }
  ```

  Qualified:
  ```json
  {
    "id": "job_124",
    "title": "Platform Engineer",
    "company": "Beta Inc",
    "qualification_status": "qualified",
    "reason": "Matches core backend stack; company size and applicant count within thresholds."
  }
  ```

  Rejected:
  ```json
  {
    "id": "job_125",
    "title": "Recruiter",
    "company": "Talent Solutions LLC",
    "qualification_status": "rejected",
    "reason": "Company matched staffing-agency keyword filter."
  }
  ```
Depends on: M1.

**M3 — Employee seed + AI matching** *(pipeline step 3)*
- Seed script: 15–20 mock employees.
- `POST /leads/{id}/match-employees`: AI call ranks employees against the job's requirements, writes top-N `employee_matches` rows with `ai_reasoning`.
- `POST /leads/{id}/confirm-employee`: sets `confirmed=true` on one match.
- `GET /leads/{id}/matches`: returns the lead's `employee_matches` rows (ranked list + reasoning). Needed so the review screen can re-fetch on reload instead of relying solely on the original `POST /leads/{id}/match-employees` response.
- Requires leads to exist, so build the lead-creation endpoint here too: `POST /jobs/{id}/create-lead` (only allowed from a `qualified` job) → creates `leads` row.
- `GET /leads/{id}`.
Depends on: M2 (needs qualified jobs to create leads from).

**M4 — Apollo contact resolution** *(pipeline step 4)*
- Company-level lookup only for the hackathon cut. Called synchronously inside `POST /jobs/{id}/create-lead` (contact resolution is fast enough not to need a BackgroundTask) or as its own step right after lead creation — pick one; recommend folding it into create-lead so the endpoint's response already includes the resolved contact + tier, matching the contract (`POST /jobs/{id}/create-lead → returns lead + resolved contact`).
- **Guaranteed floor, Apollo-failure handling**: try the Apollo company lookup first. Whether or not it returns data, always ensure a `contacts` row exists afterward — a lead must never end up with a null/missing contact. If Apollo returns nothing, create the `company_level` row anyway using the job's own `company` field (already present from the Apify job data) as the minimum viable name, leaving enrichment fields (industry, size, domain, email, phone) empty. If Apollo succeeds, use its data to fill/upgrade that same row. So `company_level` is always the floor; Apollo only adds detail on top of it when available.
- Stretch (only if time remains after M6): layer in job-poster-name search, then HR/recruiting contact, falling back to company-level.
Depends on: M3 (needs a lead to attach a contact to) — in practice merges into the create-lead endpoint, so build alongside M3's tail end.

**M5 — Outreach email generation** *(pipeline step 5)*
- Skill-overlap extraction: intersect job requirement terms with the confirmed employee's `skills` array → generalized tag list (no name, no PII).
- AI call generates subject + body using: job's specific reference, generalized overlapping skills, capability-pitch framing, soft CTA. Prompt pulls `COMPANY_NAME`, `COMPANY_SERVICES`, and `COMPANY_PITCH_TONE` from `config.py` rather than hardcoding them in the prompt template, so the pitch identity/tone can be tuned in one place.
- `POST /leads/{id}/generate-email` → creates/overwrites the lead's single `emails` row (draft), returns `id` + subject + body (the `id` is required by the frontend to call `PUT /emails/{id}`, `POST /emails/{id}/regenerate`, and `POST /emails/{id}/send` afterward).
- `PUT /emails/{id}`, `POST /emails/{id}/regenerate` (both overwrite in place per the decision above).
Depends on: M3+M4 (needs confirmed employee match and a resolved contact/recipient).

**M6 — Resend integration** *(pipeline step 6)*
- `POST /emails/{id}/send`: sends via Resend using the draft's subject/body + contact's email as recipient, flips `status` to `sent`/`failed`, sets `sent_at`, logs activity. Reads `RESEND_SENDER_EMAIL` from `config.py`.
Depends on: M5.

**M7 — Dedup on recheck** *(pipeline step 7 — cut first if time runs short)*
- `POST /campaigns/{id}/recheck`: re-run Apify search for the campaign, insert only jobs whose `external_job_id` isn't already present for that campaign (enforced by the unique constraint added in M1's schema), update `last_checked_at`.
Depends on: M1 (schema constraint already in place from the start; this module is just the recheck endpoint + skip logic).

## Flagged decisions still open (non-blocking, but worth pinning down before or during the relevant module)

- **`leads.status` enum values**: not specified. Suggested `new | contacted | replied | closed`; confirm with the frontend teammate since it likely drives UI state/badges, but doesn't block backend module order.
- **Apollo API key + rate limits**: company-level lookup is the M4 scope; confirm you have an Apollo key with enough credits before relying on it live during a demo (have a mocked fallback ready in case of rate limiting).

## Verification approach

No code written yet per this plan; once building starts, verify module-by-module:
- M0: server boots, tables created (`\dt` in psql).
- M1: POST a campaign, confirm `jobs` rows populate after the BackgroundTask completes (poll `GET /campaigns/{id}`).
- M2: confirm rule-rejected jobs never hit the AI call (check logs/token usage), confirm qualified/rejected + reason show in `GET /campaigns/{id}/jobs`.
- M3: seed employees, call match-employees on a qualified lead, confirm ranked list + reasoning returned and persisted.
- M4: confirm create-lead response includes a resolved contact with correct `type`.
- M5: confirm generated email body contains no employee name/PII, only generalized skill terms.
- M6: send to a real test inbox via Resend, confirm delivery and `emails.status` transition.
- M7 (if built): recheck a campaign twice, confirm no duplicate `jobs` rows for the same `external_job_id`.
