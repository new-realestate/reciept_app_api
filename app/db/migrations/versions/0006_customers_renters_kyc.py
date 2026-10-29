"""customer profiles, renters and tenancies, KYC, optional property context on documents

- customers: customer_type, legal_name, billing_address, tax_identification_number,
  notes, contact_preference, status, phone_normalized (backfilled to E.164 where the
  stored number parses; left NULL otherwise). UNIQUE(tenant_id, id) for composite FKs.
- renters (one per customer, same business), tenancies (history kept; one active
  tenancy per unit).
- kyc_profiles / kyc_documents / kyc_reviews.
- receipts / invoices / quotations / payment_schedules: nullable property_id,
  unit_id, tenancy_id (composite FKs keep them inside the same business). Existing
  documents are general transactions and stay NULL: no property is inferred.
- receipts: idempotency_key (unique per business).
- document_counters: a 'receipt' counter per business, seeded from the highest
  receipt number already issued, so numbering continues without gaps or reuse.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-27

"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOCUMENT_TABLES = ("receipts", "invoices", "quotations", "payment_schedules")


def _uuid_pk():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()"))


def _tenant_fk():
    return sa.Column(
        "tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


def _user_fk(name: str, ondelete: str = "SET NULL"):
    return sa.Column(name, pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete=ondelete))


def _ts(name: str, nullable: bool = True):
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True))
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _backfill_phone_normalized(bind) -> None:
    try:
        import phonenumbers
    except ImportError:  # pragma: no cover - phonenumbers is a runtime dependency
        return
    rows = bind.execute(sa.text("SELECT id, phone FROM customers WHERE phone IS NOT NULL")).fetchall()
    for customer_id, phone in rows:
        try:
            parsed = phonenumbers.parse(phone, "UG")
        except phonenumbers.NumberParseException:
            continue
        if not phonenumbers.is_valid_number(parsed):
            continue
        e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        bind.execute(sa.text("UPDATE customers SET phone_normalized = :p WHERE id = :id"), {"p": e164, "id": customer_id})


def _seed_receipt_counters(bind) -> None:
    rows = bind.execute(sa.text("SELECT tenant_id, reference_number FROM receipts")).fetchall()
    highest: dict = {}
    for tenant_id, ref in rows:
        match = re.search(r"(\d+)$", ref or "")
        if match:
            highest[tenant_id] = max(highest.get(tenant_id, 0), int(match.group(1)))
    for tenant_id, value in highest.items():
        bind.execute(
            sa.text(
                "INSERT INTO document_counters (tenant_id, kind, last_value) VALUES (:t, 'receipt', :v) "
                "ON CONFLICT (tenant_id, kind) DO UPDATE SET last_value = GREATEST(document_counters.last_value, EXCLUDED.last_value)"
            ),
            {"t": tenant_id, "v": value},
        )


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- customers
    op.add_column("customers", sa.Column("phone_normalized", sa.Text))
    op.add_column("customers", sa.Column("customer_type", sa.String(16), nullable=False, server_default="individual"))
    op.add_column("customers", sa.Column("legal_name", sa.Text))
    op.add_column("customers", sa.Column("billing_address", sa.Text))
    op.add_column("customers", sa.Column("tax_identification_number", sa.Text))
    op.add_column("customers", sa.Column("notes", sa.Text))
    op.add_column("customers", sa.Column("contact_preference", sa.String(16)))
    op.add_column("customers", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.create_unique_constraint("uq_customers_tenant_id", "customers", ["tenant_id", "id"])
    op.create_index("idx_customers_tenant_phone_norm", "customers", ["tenant_id", "phone_normalized"])
    _backfill_phone_normalized(bind)

    # ---------------------------------------------------------------- renters / tenancies
    op.create_table(
        "renters",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("occupation", sa.Text),
        sa.Column("employer", sa.Text),
        sa.Column("notes", sa.Text),
        _user_fk("created_by_user_id"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_renters_tenant_id"),
        sa.UniqueConstraint("customer_id", name="uq_renters_customer"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"], name="fk_renters_customer", ondelete="RESTRICT"
        ),
    )

    op.create_table(
        "tenancies",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("renter_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("property_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("unit_id", pg.UUID(as_uuid=True), index=True),
        sa.Column("reference", sa.Text, nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date),
        sa.Column("rent_amount_cents", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("rent_frequency", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("deposit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("notes", sa.Text),
        _user_fk("created_by_user_id"),
        _ts("ended_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenancies_tenant_id"),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_tenancies_reference"),
        sa.ForeignKeyConstraint(["tenant_id", "renter_id"], ["renters.tenant_id", "renters.id"], name="fk_tenancies_renter", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"], name="fk_tenancies_property", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["property_id", "unit_id"], ["property_units.property_id", "property_units.id"], name="fk_tenancies_unit", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("status IN ('draft', 'active', 'ended', 'terminated')", name="chk_tenancies_status"),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="chk_tenancies_dates"),
    )
    op.create_index(
        "uq_tenancies_active_unit", "tenancies", ["unit_id"], unique=True,
        postgresql_where=sa.text("status = 'active' AND unit_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- property context on documents
    for table in DOCUMENT_TABLES:
        op.add_column(table, sa.Column("property_id", pg.UUID(as_uuid=True)))
        op.add_column(table, sa.Column("unit_id", pg.UUID(as_uuid=True)))
        op.add_column(table, sa.Column("tenancy_id", pg.UUID(as_uuid=True)))
        op.create_index(f"ix_{table}_property_id", table, ["property_id"])
        op.create_index(f"ix_{table}_tenancy_id", table, ["tenancy_id"])
        op.create_foreign_key(f"fk_{table}_property", table, "properties", ["tenant_id", "property_id"], ["tenant_id", "id"])
        op.create_foreign_key(f"fk_{table}_unit", table, "property_units", ["tenant_id", "unit_id"], ["tenant_id", "id"])
        op.create_foreign_key(f"fk_{table}_tenancy", table, "tenancies", ["tenant_id", "tenancy_id"], ["tenant_id", "id"])

    # ---------------------------------------------------------------- receipts
    op.add_column("receipts", sa.Column("idempotency_key", sa.String(128)))
    op.create_unique_constraint("uq_receipts_idempotency", "receipts", ["tenant_id", "idempotency_key"])
    _seed_receipt_counters(bind)

    # ---------------------------------------------------------------- KYC
    op.create_table(
        "kyc_profiles",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("renter_id", pg.UUID(as_uuid=True), sa.ForeignKey("renters.id", ondelete="CASCADE")),
        sa.Column("legal_name", sa.Text),
        sa.Column("date_of_birth", sa.Date),
        sa.Column("residential_address", sa.Text),
        sa.Column("id_document_type", sa.String(24)),
        sa.Column("id_number_encrypted", sa.Text),
        sa.Column("id_number_last4", sa.String(8)),
        sa.Column("id_expiry_date", sa.Date),
        sa.Column("emergency_contact_name", sa.Text),
        sa.Column("emergency_contact_phone", sa.Text),
        sa.Column("emergency_contact_relationship", sa.Text),
        sa.Column("staff_reference", sa.Text),
        sa.Column("status", sa.String(24), nullable=False, server_default="not_submitted"),
        sa.Column("verification_method", sa.String(16), nullable=False, server_default="manual"),
        _ts("submitted_at"),
        _user_fk("submitted_by_user_id"),
        _ts("reviewed_at"),
        _user_fk("reviewed_by_user_id"),
        sa.Column("review_note", sa.Text),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint("(user_id IS NULL) <> (renter_id IS NULL)", name="chk_kyc_one_subject"),
        sa.UniqueConstraint("user_id", name="uq_kyc_user"),
        sa.UniqueConstraint("renter_id", name="uq_kyc_renter"),
    )
    op.create_table(
        "kyc_documents",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("profile_id", pg.UUID(as_uuid=True), sa.ForeignKey("kyc_profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.Text),
        _user_fk("uploaded_by_user_id"),
        _ts("uploaded_at", nullable=False),
        _ts("deleted_at"),
        _user_fk("deleted_by_user_id"),
    )
    op.create_table(
        "kyc_reviews",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("profile_id", pg.UUID(as_uuid=True), sa.ForeignKey("kyc_profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("note", sa.Text),
        _user_fk("reviewer_user_id"),
        _ts("created_at", nullable=False),
    )


def downgrade() -> None:
    # Receipt counters are left in place: they only ever move forward and the old
    # COUNT-based numbering would otherwise risk reusing a number.
    op.drop_table("kyc_reviews")
    op.drop_table("kyc_documents")
    op.drop_table("kyc_profiles")

    op.drop_constraint("uq_receipts_idempotency", "receipts", type_="unique")
    op.drop_column("receipts", "idempotency_key")

    for table in DOCUMENT_TABLES:
        op.drop_constraint(f"fk_{table}_tenancy", table, type_="foreignkey")
        op.drop_constraint(f"fk_{table}_unit", table, type_="foreignkey")
        op.drop_constraint(f"fk_{table}_property", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_tenancy_id", table_name=table)
        op.drop_index(f"ix_{table}_property_id", table_name=table)
        for column in ("tenancy_id", "unit_id", "property_id"):
            op.drop_column(table, column)

    op.drop_index("uq_tenancies_active_unit", table_name="tenancies")
    op.drop_table("tenancies")
    op.drop_table("renters")

    op.drop_index("idx_customers_tenant_phone_norm", table_name="customers")
    op.drop_constraint("uq_customers_tenant_id", "customers", type_="unique")
    for column in (
        "status", "contact_preference", "notes", "tax_identification_number", "billing_address", "legal_name",
        "customer_type", "phone_normalized",
    ):
        op.drop_column("customers", column)
