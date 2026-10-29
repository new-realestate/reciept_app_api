"""staff management and business-owned properties

- users: phone, permissions (JSONB; NULL = default staff set), deactivated_at /
  deactivated_by_user_id, created_by_user_id. Existing users keep NULL permissions,
  so existing staff keep exactly the access they had.
- properties, property_units, property_staff_assignments (new). Assignments are
  revoked by setting revoked_at, never deleted, and composite FKs pin units and
  assignments to a property of the same business.

Additive only: no existing rows are changed.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.Text))
    op.add_column("users", sa.Column("permissions", pg.JSONB))
    op.add_column("users", sa.Column("deactivated_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("deactivated_by_user_id", pg.UUID(as_uuid=True)))
    op.add_column("users", sa.Column("created_by_user_id", pg.UUID(as_uuid=True)))
    op.create_foreign_key("users_deactivated_by_user_id_fkey", "users", "users", ["deactivated_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("users_created_by_user_id_fkey", "users", "users", ["created_by_user_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "properties",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("property_type", sa.String(16), nullable=False, server_default="residential"),
        sa.Column("address", sa.Text),
        sa.Column("location", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        _user_fk("created_by_user_id"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_properties_tenant_id"),
        sa.CheckConstraint("status IN ('active', 'inactive', 'archived')", name="chk_properties_status"),
    )

    op.create_table(
        "property_units",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("property_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("unit_type", sa.String(16), nullable=False, server_default="apartment"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("default_rent_cents", sa.BigInteger),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("property_id", "label", name="uq_units_property_label"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_units_tenant_id"),
        sa.UniqueConstraint("property_id", "id", name="uq_units_property_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"], name="fk_units_property", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "property_staff_assignments",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("property_id", pg.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        _user_fk("assigned_by_user_id"),
        _ts("assigned_at", nullable=False),
        _user_fk("revoked_by_user_id"),
        _ts("revoked_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"], name="fk_assignments_property", ondelete="CASCADE"
        ),
    )
    op.create_index(
        "uq_assignments_active", "property_staff_assignments", ["property_id", "user_id"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_assignments_active", table_name="property_staff_assignments")
    op.drop_table("property_staff_assignments")
    op.drop_table("property_units")
    op.drop_table("properties")
    op.drop_constraint("users_created_by_user_id_fkey", "users", type_="foreignkey")
    op.drop_constraint("users_deactivated_by_user_id_fkey", "users", type_="foreignkey")
    for column in ("created_by_user_id", "deactivated_by_user_id", "deactivated_at", "permissions", "phone"):
        op.drop_column("users", column)
