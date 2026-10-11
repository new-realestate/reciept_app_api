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
