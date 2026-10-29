# Receipt Issuance App — Backend

FastAPI backend for a local-payments-only receipt issuance app. A landlord/business owner
(or authorized staff) records a payment already received locally (cash, bank transfer,
mobile money, or cheque) and the app generates a formatted receipt (PDF + hosted HTML view)
and delivers it to the payer by email (Gmail API) and/or SMS (Twilio). The app never
processes or moves money.

## Tech stack

- FastAPI (Python 3.11+)
- PostgreSQL via SQLAlchemy 2.0 (async) + Alembic migrations
- Gmail API (OAuth refresh token) for email delivery
- APScheduler (in-process) for reminders, recurring schedules and quotation expiry
- Twilio for SMS delivery
- WeasyPrint + Jinja2 for PDF rendering
- JWT auth (python-jose + passlib/bcrypt)
- pytest + httpx for tests (run against SQLite in-memory for speed)

## Project structure

```
app/
  main.py                 FastAPI app, router wiring, exception handlers
  core/                   config, security, auth deps, rate limiting, audit logging
  models/                 SQLAlchemy models (mirrors schema.sql)
  schemas/                Pydantic request/response models
  api/                    auth, tenant, customers, receipts, webhooks routers
  services/               email (Gmail API), sms (Twilio), pdf (WeasyPrint), numbers (amount-to-words)
  templates/receipt.html  Jinja2 template rendered to PDF and used for the hosted view
  db/                     session factory + Alembic migrations
tests/                    pytest suite (auth, receipts, delivery/webhooks)
schema.sql                Source-of-truth reference schema (Postgres)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in JWT_SECRET, Gmail/Twilio credentials, DATABASE_URL
```

### Database

Point `DATABASE_URL` at a real PostgreSQL instance, then run migrations:

```bash
alembic upgrade head
```

The initial migration (`app/db/migrations/versions/0001_initial_schema.py`) mirrors
`schema.sql`. Models use portable SQLAlchemy types (`Uuid`, generic `Enum`, `JSON`, with
PostgreSQL-specific variants for CITEXT/JSONB) so the same models also work against SQLite
for fast local tests, while production on Postgres gets the native types.

### Running the API

```bash
uvicorn app.main:app --reload
```

Visit `/docs` for interactive OpenAPI docs.

### Running tests

```bash
pytest
```

Tests spin up an in-memory SQLite database per test and stub out Gmail/Twilio calls —
no external credentials or network access needed to run the suite.

## Environment variables

See `.env.example`. All secrets are read via `pydantic-settings`; nothing is hardcoded.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Async SQLAlchemy URL, e.g. `postgresql+asyncpg://user:pass@host/db` |
| `JWT_SECRET` | Signing secret for access tokens |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` / `GMAIL_SENDER_EMAIL` | Email delivery via the Gmail API. Get the refresh token with `uv run --with google-auth-oauthlib python scripts/gmail_authorize.py` (OAuth consent screen must be published, not in Testing, or tokens expire after 7 days) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | SMS delivery |
| `APP_BASE_URL` | Used to build hosted receipt links (`/r/{token}`) and email/reset links |
| `STORAGE_DIR` | Local disk path for uploaded logos/signatures and generated PDFs (swap for S3/GCS later without changing callers — see `app/services/pdf.py`) |
| `KYC_ENCRYPTION_KEY` | Fernet key that encrypts KYC identity-document numbers. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it secret and back it up: numbers stored with a lost key can't be recovered. KYC endpoints that store or reveal an ID number return 503 while it is unset |

## Properties, staff, tenants and KYC

Roles, permission flags, data scoping and the KYC rules are in [docs/roles-and-permissions.md](docs/roles-and-permissions.md).

```bash
alembic upgrade head   # applies 0005 (staff, properties) and 0006 (customer profiles, renters, tenancies, KYC)
python scripts/report_unmapped.py   # read-only: existing records you may want to link to a property by hand
```

Both migrations are additive. Existing documents stay general transactions (no property is
inferred), customer phone numbers are backfilled to E.164 where they parse, and receipt numbering
moves to the per-business counter, starting after the highest number already issued.

## Performance and hosting region

Every database query is a network round trip, so the API is fast when it sits next to the
database and makes few queries per request:

- The app connects through `DATABASE_URL_UNPOOLED` (Neon's direct endpoint) when it's set, so
  asyncpg can cache prepared statements. It keeps its own small pool (5 + 5 overflow), recycles
  connections every 4 minutes (Neon suspends idle compute after 5) and pings each connection
  before use (`DATABASE_POOL_PRE_PING`, on by default; set `false` for local dev far from the
  database to save a round trip per request).
- The access log shows the SQL statement count per request
  (`GET /receipts -> 200 in 12ms, 2 sql`), and `tests/test_query_budget.py` fails if common
  endpoints start issuing more queries.
- `render.yaml` pins the service to `region: ohio`, the same AWS region as the Neon database.

### Moving an existing Render service to Ohio

Render can't change a service's region in place, so create a new one and switch over:

1. **Create the service.** In Render, create a new Blueprint (or Web Service) from this repo with
   region **Ohio**. `render.yaml` already sets it.
2. **Copy the environment variables** from the old service, keeping the **same values**:
   `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `AWS_*`, `GMAIL_*`, `TWILIO_*`, `SUPERADMIN_*`,
   `CORS_ALLOW_ORIGINS_RAW`, `KYC_ENCRYPTION_KEY`, and **`JWT_SECRET`**. Keeping `JWT_SECRET`
   keeps everyone signed in; a new `KYC_ENCRYPTION_KEY` would make stored ID numbers
   unreadable.
3. **Deploy.** Migrations run in the pre-deploy step; there are no schema changes in this move.
4. **Point the frontend and links at the new URL.** Set the portal's `VITE_API_BASE_URL` (on
   Vercel) and this service's `APP_BASE_URL` to the new URL, then redeploy the portal.
   - Receipt PDFs and hosted links already sent embed the old `APP_BASE_URL`. To keep them
     working, use a custom domain (move it to the new service), or keep the old service
     running until those links no longer matter.
5. **Check, then remove the old service.** Check `/health` and a few portal pages, then
   suspend and delete the old Oregon service.
   - Run only one service with `SCHEDULER_ENABLED=true` at a time, so reminders aren't sent
     twice during the overlap.

## Core flow

1. `POST /auth/signup` → creates a tenant + owner user, sends a verification email.
2. `PATCH /tenant/me`, `POST /tenant/logo`, `POST /users/me/signature` → set up letterhead.
3. `POST /customers` → add a payer (or let `POST /receipts` create one inline).
4. `POST /receipts` → validates required fields, auto-generates `amount_in_words`, assigns
   the next sequential `reference_number` for the tenant, renders and stores the PDF.
   Receipts are immutable — corrections are new receipts with `corrects_receipt_id` set.
5. `POST /receipts/{id}/send` → delivers via email and/or SMS, logging one
   `delivery_attempts` row per channel; retries transient email failures with backoff.
6. `POST /webhooks/postmark` / `POST /webhooks/twilio` → update delivery status from
   provider callbacks; hard bounces and STOP replies are added to the suppression list and
   respected on resend.
7. `GET /r/{hosted_view_token}` → public, unguessable hosted view of the receipt.

## Notes / known limitations (MVP scope)

- File storage (logos, signatures, PDFs) is local disk under `STORAGE_DIR`. Swap
  `app/services/pdf.py::store_receipt_pdf` (and the upload handlers in `app/api/tenant.py`)
  for an S3/GCS-backed implementation when scaling past a single instance.
- Rate limiting (`app/core/rate_limit.py`) is an in-memory fixed-window limiter, fine for a
  single process; move to Redis before running multiple workers.
- The sequential reference-number assignment locks the tenant row (`SELECT ... FOR UPDATE`)
  to avoid collisions under concurrent receipt creation; this is skipped automatically on
  SQLite (used only in tests), which has no row-level locking.
- No online payment gateway anywhere in this codebase, by design — see the product spec.

## Invoices, quotations, schedules and reminders

| Area | Endpoints | Notes |
|---|---|---|
| Invoices | `POST/GET /invoices`, `GET/PATCH /invoices/{id}`, `POST /invoices/{id}/issue`, `/cancel`, `/send`, `/remind`, `/rotate-link`, `/revoke-link`, `GET /invoices/{id}/pdf`, `/payments`, `/notifications`, `PATCH /invoices/{id}/reminders` | Drafts are editable and unnumbered; issuing assigns `INV-YYYY-NNNNNN`, a public token and freezes the contents (snapshot). Status `paid` / `partially_paid` / `overdue` is derived (`display_status`), never stored. |
| Payments | `POST /invoices/{id}/payments` | Records money received against an invoice, issues a normal receipt, and updates the balance in one transaction under a row lock. Send an `Idempotency-Key` header (the portal always does); a repeat returns the original payment (HTTP 200, `replayed: true`). Overpayments are rejected. |
| Quotations | `POST/GET /quotations`, `GET/PATCH /quotations/{id}`, `POST /quotations/{id}/send`, `/cancel`, `/convert`, `/rotate-link`, `GET /quotations/{id}/pdf`, `/notifications` | `draft → sent → viewed → accepted/rejected`, `expired` when past `valid_until`. Internal notes never appear publicly. Converting an accepted quotation creates a draft (or issued) invoice and never a receipt; one conversion per quotation. |
| Public pages | `GET /i/{token}`, `/i/{token}/pdf`, `GET /q/{token}`, `/q/{token}/pdf`, `POST /q/{token}/accept`, `/q/{token}/reject` | Unauthenticated, 256-bit tokens, `no-store`, rate limited per client IP. Acceptance records the typed name and a confirmation; it is not an electronic signature. |
| Schedules | `POST/GET /schedules`, `GET/PATCH /schedules/{id}`, `POST /schedules/{id}/pause`, `/resume`, `/cancel`, `PATCH /schedules/{id}/reminders`, `POST /schedules/occurrences/{id}/create-invoice`, `/skip` | Occurrences are *expected payments*. An invoice is created only by `create-invoice` (or by the opt-in `auto_invoice` flag); a database constraint allows one invoice per occurrence. Monthly dates are computed from the first due date, so the 31st clamps to short months and returns to the 31st. |
| Reminders | `GET/PUT /reminder-settings`, `GET /notifications`, `POST /notifications/{id}/retry`, `/cancel`, `GET /payments/upcoming`, `/payments/overdue`, `GET /dashboard/summary` | Offsets are days relative to the due date (default `[-3, 0, 3]`, 09:00 in the business timezone, email + SMS). |

Reminder behaviour: every message is a `notification_events` row with a unique `dedupe_key`, so planning twice
never duplicates it; each try is a `notification_attempts` row. Before sending, the invoice is re-checked
(paid/cancelled/disabled → `skipped`), the suppression list is honoured, and reminders that missed their window by
more than 36 hours are dropped instead of sent late. Failed sends are retried automatically up to 3 times with
backoff; `POST /notifications/{id}/retry` retries one by hand. Manual reminders are limited to one per channel per hour.

### Roles

Owner/Admin only: cancelling invoices/quotations, rotating or revoking public links, business-wide reminder
settings. Staff can create, send and record payments. Existing receipt endpoints are unchanged.

### Migrations and tooling

```bash
alembic upgrade head          # applies 0003 (additive; existing receipts/customers untouched)
pytest                        # backend suite (SQLite in memory; Gmail/Twilio are stubbed)
ruff check app tests          # lint (config in pyproject.toml)
mypy app                      # types (3 pre-existing errors in config.py / webhooks.py)
```

`SCHEDULER_ENABLED=true` starts the background job (every `REMINDER_POLL_SECONDS`, default 300). Leave it `false`
for tests and for any process that is not the single production instance.

### Known limitations

- An issued invoice that already has payments cannot be cancelled (credit notes are not implemented); its receipts stay valid.
- The scheduler and rate limiter are in-process, so run a single instance (as `render.yaml` already does).
- Invoice/quotation PDFs are rendered on demand rather than stored, so they always reflect current payments.
- Server access logs record request paths, which include public-link tokens; restrict log access accordingly.
- `/webhooks/postmark` is a legacy route; email now goes through Gmail, so delivery status for invoices comes from send results only.
## Logging

pp/core/logging.py configures logging once at startup (LOG_LEVEL, LOG_JSON). Every request gets an X-Request-ID (an incoming one is honoured if it is a safe 8-64 character token), returned in the response and attached to every log line, and one access line is written per request with status and duration. A redacting filter removes public document tokens (/i/{token}, /q/{token}), bearer tokens and password/secret/token values from all records, including uvicorn's. This also closes the earlier note about tokens appearing in access logs.

## Audit log

| Endpoint | Purpose |
|---|---|
| `GET /audit-logs` | Newest-first trail for your business. Filters: `action`, `action_prefix` (e.g. `invoice`), `entity_type`, `entity_id`, `actor_user_id`, `date_from` / `date_to` (inclusive, business timezone); `limit` (max 200) and `offset`. Each row has the actor's name and email, or none for customer-side actions such as accepting a quotation. |
| `GET /audit-logs/actions` | Distinct action names, for filter menus. |

Owner and Admin only; always scoped to the caller's business; read-only.
