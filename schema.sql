-- ============================================================
-- Receipt Issuance App — Database Schema (PostgreSQL)
-- Local payments only (cash / bank transfer / mobile money /
-- cheque) — no online payment gateway. Receipts are entered
-- manually by the main user (landlord/business owner) or staff.
-- ============================================================

-- ---------- Extensions ----------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "citext";

-- ---------- Enums ----------
CREATE TYPE user_role AS ENUM ('owner', 'admin', 'staff');
CREATE TYPE receipt_status AS ENUM ('issued', 'voided', 'refunded');
CREATE TYPE delivery_channel AS ENUM ('email', 'sms');
CREATE TYPE delivery_status AS ENUM ('queued', 'sent', 'delivered', 'bounced', 'failed', 'opened');
CREATE TYPE plan_tier AS ENUM ('free_trial', 'starter', 'growth', 'enterprise');
-- Local payment rails only — no card/online gateway in this phase
CREATE TYPE payment_method_type AS ENUM ('cash', 'bank_transfer', 'mobile_money', 'cheque', 'other');

-- ============================================================
-- TENANTS — root of multi-tenancy (the landlord/business account)
-- Doubles as the receipt "letterhead" record.
-- ============================================================
CREATE TABLE tenants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_name       TEXT NOT NULL,
    logo_url            TEXT,
    brand_color         TEXT,                 -- hex color for receipt templates
    po_box              TEXT,                  -- P.O. Box number for letterhead
    physical_address     TEXT,
    contact_phone       TEXT,
    contact_email       TEXT,
    bank_name           TEXT,                  -- for display on receipt / letterhead
    bank_account_number TEXT,
    plan_tier           plan_tier NOT NULL DEFAULT 'free_trial',
    default_currency    CHAR(3) NOT NULL DEFAULT 'UGX',
    receipt_template    TEXT NOT NULL DEFAULT 'classic',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ           -- soft delete
);

-- ============================================================
-- USERS — main user (landlord/owner) and any staff accounts.
-- Only these accounts can create/issue receipts.
-- ============================================================
CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    full_name      TEXT NOT NULL,            -- printed on receipts as issuer name
    email          CITEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    role           user_role NOT NULL DEFAULT 'owner',
    signature_image_url TEXT,                -- uploaded signature used on issued receipts
    is_verified    BOOLEAN NOT NULL DEFAULT FALSE,
    email_verification_token TEXT,           -- token emailed to the user to verify their address
    password_reset_token TEXT,               -- token emailed to the user to reset their password
    password_reset_expires_at TIMESTAMPTZ,
    last_login_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE INDEX idx_users_tenant ON users(tenant_id);

-- ============================================================
-- CUSTOMERS — the payer (e.g. tenant/renter, buyer). Not a
-- login account in MVP; just contact info for delivery.
-- ============================================================
CREATE TABLE customers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    email       CITEXT,
    phone       TEXT,                 -- store E.164 format, e.g. +256701234567
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_customer_has_contact CHECK (email IS NOT NULL OR phone IS NOT NULL)
);

CREATE INDEX idx_customers_tenant ON customers(tenant_id);
CREATE INDEX idx_customers_email ON customers(tenant_id, email);
CREATE INDEX idx_customers_phone ON customers(tenant_id, phone);

-- ============================================================
-- RECEIPTS — the core object; immutable once issued.
-- Records a payment already received locally; the app does not
-- process or collect the payment itself.
-- ============================================================
CREATE TABLE receipts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id         UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    reference_number    TEXT NOT NULL,          -- human-facing receipt number, e.g. RCT-2026-000123
    status              receipt_status NOT NULL DEFAULT 'issued',

    -- Amount
    currency            CHAR(3) NOT NULL,
    amount_cents        BIGINT NOT NULL,        -- amount received, in figures
    amount_in_words     TEXT NOT NULL,           -- e.g. "Five Hundred Thousand Shillings Only"
    balance_cents       BIGINT NOT NULL DEFAULT 0, -- 0 => render as "Nil" on the receipt
    reason              TEXT NOT NULL,           -- purpose of payment, e.g. "Rent for July 2026"

    -- How/when it was paid locally
    payment_method      payment_method_type NOT NULL,
    payment_reference   TEXT,                    -- bank slip #, mobile money transaction ID, cheque #
    paid_at             DATE NOT NULL,            -- date the payment was received

    -- Issuance / signature
    issued_by_user_id   UUID NOT NULL REFERENCES users(id),
    issuer_printed_name TEXT NOT NULL,           -- snapshot of issuer's name at time of issuance
    signature_image_url TEXT,                    -- snapshot of signature used, if any

    -- Corrections: a correction is a new receipt referencing the original; the
    -- original is never overwritten (receipts are immutable once issued).
    corrects_receipt_id UUID REFERENCES receipts(id),

    hosted_view_token   TEXT NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'), -- unguessable public link token
    pdf_url             TEXT,                    -- generated/stored PDF location
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, reference_number)
);

CREATE INDEX idx_receipts_tenant_created ON receipts(tenant_id, created_at DESC);
CREATE INDEX idx_receipts_customer ON receipts(customer_id);
CREATE UNIQUE INDEX idx_receipts_hosted_token ON receipts(hosted_view_token);

-- ============================================================
-- DELIVERY ATTEMPTS — one row per send attempt per channel
-- ============================================================
CREATE TABLE delivery_attempts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    receipt_id          UUID NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    channel             delivery_channel NOT NULL,
    status              delivery_status NOT NULL DEFAULT 'queued',
    recipient           TEXT NOT NULL,          -- email address or phone number used for this attempt
    provider            TEXT,                   -- 'sendgrid', 'twilio', etc.
    provider_message_id TEXT,                   -- ID returned by provider, for webhook correlation
    provider_response   JSONB,                  -- raw response/error payload for debugging
    attempt_number      INT NOT NULL DEFAULT 1,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ             -- when status last changed (delivered/failed/bounced)
);

CREATE INDEX idx_delivery_receipt ON delivery_attempts(receipt_id);
CREATE INDEX idx_delivery_status ON delivery_attempts(status);
CREATE INDEX idx_delivery_provider_msg ON delivery_attempts(provider_message_id);

-- ============================================================
-- API KEYS — reserved for Phase 3 integrations. Not used by the
-- MVP, since all receipts are entered manually by the main user.
-- ============================================================
CREATE TABLE api_keys (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_prefix    TEXT NOT NULL,          -- shown to user, e.g. "rk_live_9f2a"
    key_hash      TEXT NOT NULL,          -- hash of full secret key, never store plaintext
    rate_limit_per_minute INT NOT NULL DEFAULT 60,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id UUID REFERENCES users(id),
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_tenant ON api_keys(tenant_id);
CREATE UNIQUE INDEX idx_api_keys_prefix ON api_keys(key_prefix);

-- ============================================================
-- SUPPRESSION LIST — bounced emails / SMS opt-outs (STOP)
-- Required for compliance (CAN-SPAM / TCPA-style rules) — do not
-- send to these.
-- ============================================================
CREATE TABLE suppressions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE, -- NULL = platform-wide suppression
    channel     delivery_channel NOT NULL,
    contact     TEXT NOT NULL,            -- email or phone
    reason      TEXT,                     -- 'hard_bounce', 'complaint', 'stop_keyword', 'manual'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, contact)
);

CREATE INDEX idx_suppressions_lookup ON suppressions(channel, contact);

-- ============================================================
-- AUDIT LOG — who did what, when (compliance + support)
-- ============================================================
CREATE TABLE audit_logs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES users(id),
    action        TEXT NOT NULL,           -- e.g. 'receipt.created', 'receipt.resent', 'api_key.revoked'
    entity_type   TEXT NOT NULL,           -- 'receipt', 'api_key', 'user', etc.
    entity_id     UUID,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_tenant_created ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX idx_audit_entity ON audit_logs(entity_type, entity_id);

-- ============================================================
-- PHASE 2 (STUB) — platform subscription usage tracking.
-- No online payment gateway assumed; platform fees can be
-- invoiced/collected the same local way (cash/bank/mobile money)
-- until an online gateway is deliberately reintroduced.
-- ============================================================
CREATE TABLE usage_counters (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    emails_sent      INT NOT NULL DEFAULT 0,
    sms_sent         INT NOT NULL DEFAULT 0,
    receipts_created INT NOT NULL DEFAULT 0,
    UNIQUE (tenant_id, period_start, period_end)
);

CREATE INDEX idx_usage_tenant_period ON usage_counters(tenant_id, period_start);

-- ============================================================
-- Trigger: auto-update updated_at columns
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tenants_updated_at BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_customers_updated_at BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Migration 0003: invoicing, quotations, payment schedules, notifications.
-- Mirrors app/db/migrations/versions/0003_invoicing_quotes_schedules.py.
-- Money is integer minor units; statuses are plain text validated by the app.
-- paid / partially_paid / overdue are derived from amounts and due_date, not stored.
-- ---------------------------------------------------------------------------
ALTER TABLE tenants ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Africa/Kampala';
ALTER TABLE tenants ADD COLUMN reminder_settings JSONB;

CREATE TABLE document_counters (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind       VARCHAR(16) NOT NULL,
    last_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, kind)
);

CREATE TABLE quotations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    number TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',   -- draft|sent|viewed|accepted|rejected|expired|cancelled
    title TEXT, description TEXT,
    currency CHAR(3) NOT NULL,
    issue_date DATE NOT NULL, valid_until DATE NOT NULL,
    subtotal_cents BIGINT NOT NULL DEFAULT 0, discount_percent NUMERIC(5,2),
    discount_cents BIGINT NOT NULL DEFAULT 0, tax_cents BIGINT NOT NULL DEFAULT 0,
    total_cents BIGINT NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    terms TEXT, notes TEXT, internal_notes TEXT,
    public_token VARCHAR(64) UNIQUE, token_revoked_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ, viewed_at TIMESTAMPTZ, responded_at TIMESTAMPTZ,
    accepted_name TEXT, response_comment TEXT, snapshot JSONB,
    created_by_user_id UUID REFERENCES users(id), cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_quotations_tenant_number UNIQUE (tenant_id, number)
);
CREATE TABLE quotation_line_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    quotation_id UUID NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL,
    quantity NUMERIC(12,3) NOT NULL, unit_price_cents BIGINT NOT NULL,
    tax_rate NUMERIC(5,2), line_total_cents BIGINT NOT NULL
);

CREATE TABLE payment_schedules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    description TEXT NOT NULL, reference_label TEXT,
    amount_cents BIGINT NOT NULL, currency CHAR(3) NOT NULL,
    frequency VARCHAR(16) NOT NULL,               -- once|weekly|monthly|quarterly|annually
    first_due_date DATE NOT NULL, end_date DATE, max_occurrences INTEGER,
    status VARCHAR(16) NOT NULL DEFAULT 'active', -- active|paused|completed|cancelled
    reminders_enabled BOOLEAN NOT NULL DEFAULT true, reminder_overrides JSONB,
    auto_invoice BOOLEAN NOT NULL DEFAULT false,
    created_by_user_id UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE schedule_occurrences (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    schedule_id UUID NOT NULL REFERENCES payment_schedules(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, due_date DATE NOT NULL, amount_cents BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'expected', -- expected|invoiced|paid|skipped
    amount_paid_cents BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_occurrence_schedule_seq UNIQUE (schedule_id, seq)
);

CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    number TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',   -- draft|issued|cancelled
    title TEXT, currency CHAR(3) NOT NULL,
    issue_date DATE NOT NULL, due_date DATE NOT NULL,
    subtotal_cents BIGINT NOT NULL DEFAULT 0, discount_percent NUMERIC(5,2),
    discount_cents BIGINT NOT NULL DEFAULT 0, tax_cents BIGINT NOT NULL DEFAULT 0,
    total_cents BIGINT NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    amount_paid_cents BIGINT NOT NULL DEFAULT 0,
    notes TEXT, payment_instructions TEXT,
    public_token VARCHAR(64) UNIQUE, token_revoked_at TIMESTAMPTZ,
    quotation_id UUID UNIQUE REFERENCES quotations(id) ON DELETE SET NULL,
    schedule_occurrence_id UUID UNIQUE REFERENCES schedule_occurrences(id) ON DELETE SET NULL,
    reminders_enabled BOOLEAN NOT NULL DEFAULT true, reminder_overrides JSONB, snapshot JSONB,
    created_by_user_id UUID REFERENCES users(id),
    issued_at TIMESTAMPTZ, cancelled_at TIMESTAMPTZ, cancel_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_invoices_tenant_number UNIQUE (tenant_id, number),
    CONSTRAINT chk_invoice_paid_range CHECK (amount_paid_cents >= 0 AND amount_paid_cents <= total_cents)
);
CREATE TABLE invoice_line_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL,
    quantity NUMERIC(12,3) NOT NULL, unit_price_cents BIGINT NOT NULL,
    tax_rate NUMERIC(5,2), line_total_cents BIGINT NOT NULL
);
CREATE TABLE invoice_payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
    receipt_id UUID NOT NULL UNIQUE REFERENCES receipts(id) ON DELETE RESTRICT,
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    paid_at DATE NOT NULL, method VARCHAR(32) NOT NULL, reference TEXT,
    idempotency_key VARCHAR(128), recorded_by_user_id UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_invoice_payments_idem UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE notification_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL, subject_type VARCHAR(32) NOT NULL, subject_id UUID NOT NULL,
    channel VARCHAR(8) NOT NULL, recipient_party VARCHAR(16) NOT NULL DEFAULT 'customer', recipient TEXT,
    scheduled_for TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'scheduled', -- scheduled|sending|sent|failed|skipped|cancelled
    skip_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    dedupe_key VARCHAR(200) NOT NULL UNIQUE, payload JSONB, sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE notification_attempts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id UUID NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL, status VARCHAR(16) NOT NULL,
    provider VARCHAR(16), provider_message_id TEXT, response JSONB,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
