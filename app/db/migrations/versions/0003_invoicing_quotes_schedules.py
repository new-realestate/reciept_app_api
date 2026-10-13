"""invoices, quotations, payment schedules, notifications

Additive only: new tables plus two new columns on tenants (with server defaults),
so existing receipts, customers and users are untouched.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_pk():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()"))


def _tenant_fk():
    return sa.Column(
        "tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


def _ts(name: str, nullable: bool = True):
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True))
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.add_column("tenants", sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Kampala"))
    op.add_column("tenants", sa.Column("reminder_settings", pg.JSONB))

    op.create_table(
        "document_counters",
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("last_value", sa.Integer, nullable=False, server_default="0"),
    )

    # quotations first: invoices reference them.
    op.create_table(
        "quotations",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("number", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("title", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column("valid_until", sa.Date, nullable=False),
        sa.Column("subtotal_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("discount_percent", sa.Numeric(5, 2)),
        sa.Column("discount_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tax_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("terms", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("internal_notes", sa.Text),
        sa.Column("public_token", sa.String(64), unique=True),
        _ts("token_revoked_at"),
        _ts("sent_at"),
        _ts("viewed_at"),
        _ts("responded_at"),
        sa.Column("accepted_name", sa.Text),
        sa.Column("response_comment", sa.Text),
        sa.Column("snapshot", pg.JSONB),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        _ts("cancelled_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "number", name="uq_quotations_tenant_number"),
        sa.CheckConstraint("total_cents >= 0", name="chk_quotation_total_nonneg"),
    )
    op.create_table(
        "quotation_line_items",
        _uuid_pk(),
        sa.Column("quotation_id", pg.UUID(as_uuid=True), sa.ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price_cents", sa.BigInteger, nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2)),
        sa.Column("line_total_cents", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "payment_schedules",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("reference_label", sa.Text),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("first_due_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date),
        sa.Column("max_occurrences", sa.Integer),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("reminders_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("reminder_overrides", pg.JSONB),
        sa.Column("auto_invoice", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
    )
    op.create_table(
        "schedule_occurrences",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("schedule_id", pg.UUID(as_uuid=True), sa.ForeignKey("payment_schedules.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False, index=True),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="expected"),
        sa.Column("amount_paid_cents", sa.BigInteger, nullable=False, server_default="0"),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("schedule_id", "seq", name="uq_occurrence_schedule_seq"),
    )

    op.create_table(
        "invoices",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("number", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("title", sa.Text),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("subtotal_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("discount_percent", sa.Numeric(5, 2)),
        sa.Column("discount_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tax_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("amount_paid_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text),
        sa.Column("payment_instructions", sa.Text),
        sa.Column("public_token", sa.String(64), unique=True),
        _ts("token_revoked_at"),
        sa.Column("quotation_id", pg.UUID(as_uuid=True), sa.ForeignKey("quotations.id", ondelete="SET NULL"), unique=True),
        sa.Column("schedule_occurrence_id", pg.UUID(as_uuid=True), sa.ForeignKey("schedule_occurrences.id", ondelete="SET NULL"), unique=True),
        sa.Column("reminders_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("reminder_overrides", pg.JSONB),
        sa.Column("snapshot", pg.JSONB),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        _ts("issued_at"),
        _ts("cancelled_at"),
        sa.Column("cancel_reason", sa.Text),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "number", name="uq_invoices_tenant_number"),
        sa.CheckConstraint("total_cents >= 0", name="chk_invoice_total_nonneg"),
        sa.CheckConstraint("amount_paid_cents >= 0 AND amount_paid_cents <= total_cents", name="chk_invoice_paid_range"),
    )
    op.create_table(
        "invoice_line_items",
        _uuid_pk(),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price_cents", sa.BigInteger, nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2)),
        sa.Column("line_total_cents", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "invoice_payments",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("receipt_id", pg.UUID(as_uuid=True), sa.ForeignKey("receipts.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("paid_at", sa.Date, nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("reference", sa.Text),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("recorded_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_invoice_payments_idem"),
        sa.CheckConstraint("amount_cents > 0", name="chk_invoice_payment_positive"),
    )

    op.create_table(
        "notification_events",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("recipient_party", sa.String(16), nullable=False, server_default="customer"),
        sa.Column("recipient", sa.Text),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="scheduled", index=True),
        sa.Column("skip_reason", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dedupe_key", sa.String(200), nullable=False, unique=True),
        sa.Column("payload", pg.JSONB),
        _ts("sent_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
    )
    op.create_table(
        "notification_attempts",
        _uuid_pk(),
        sa.Column("event_id", pg.UUID(as_uuid=True), sa.ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(16)),
        sa.Column("provider_message_id", sa.Text),
        sa.Column("response", pg.JSONB),
        _ts("attempted_at", nullable=False),
    )


def downgrade() -> None:
    for table in (
        "notification_attempts",
        "notification_events",
        "invoice_payments",
        "invoice_line_items",
        "invoices",
        "schedule_occurrences",
        "payment_schedules",
        "quotation_line_items",
        "quotations",
        "document_counters",
    ):
        op.drop_table(table)
    op.drop_column("tenants", "reminder_settings")
    op.drop_column("tenants", "timezone")
