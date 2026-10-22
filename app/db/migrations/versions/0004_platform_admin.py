"""platform super admin: suspension, tenant-less super admins, platform audit events

- users: tenant_id nullable (super admins belong to no business), is_superadmin,
  suspended_at/suspended_reason, a global unique index on email (the app already
  treats emails as unique across businesses).
- tenants: suspended_at/suspended_reason.
- audit_logs: tenant_id nullable for platform-level events.
- Nullable FKs to users.id become ON DELETE SET NULL so a super admin can delete a
  user without losing the documents/audit rows they touched. receipts.issued_by_user_id
  is unchanged: users who issued receipts can only be suspended.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs whose FK to users.id becomes ON DELETE SET NULL.
_USER_FKS = [
    ("audit_logs", "actor_user_id"),
    ("api_keys", "created_by_user_id"),
    ("invoices", "created_by_user_id"),
    ("invoice_payments", "recorded_by_user_id"),
    ("quotations", "created_by_user_id"),
    ("payment_schedules", "created_by_user_id"),
]


def _repoint_user_fks(ondelete: str | None) -> None:
    inspector = sa.inspect(op.get_bind())
    for table, column in _USER_FKS:
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "users" and fk["constrained_columns"] == [column]:
                op.drop_constraint(fk["name"], table, type_="foreignkey")
        op.create_foreign_key(f"{table}_{column}_fkey", table, "users", [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    op.alter_column("users", "tenant_id", nullable=True)
    op.add_column("users", sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("suspended_reason", sa.Text()))
    op.create_check_constraint("chk_users_tenant_or_superadmin", "users", "tenant_id IS NOT NULL OR is_superadmin")
    op.create_index("uq_users_email", "users", ["email"], unique=True)

    op.add_column("tenants", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column("tenants", sa.Column("suspended_reason", sa.Text()))

    op.alter_column("audit_logs", "tenant_id", nullable=True)
    op.create_index("idx_audit_created", "audit_logs", [sa.text("created_at DESC")])

    _repoint_user_fks("SET NULL")


def downgrade() -> None:
    # Remove platform-only rows while the FKs still null out references to deleted users.
    op.execute("DELETE FROM audit_logs WHERE tenant_id IS NULL")
    op.execute("DELETE FROM users WHERE tenant_id IS NULL")
    _repoint_user_fks(None)

    op.drop_index("idx_audit_created", table_name="audit_logs")
    op.alter_column("audit_logs", "tenant_id", nullable=False)

    op.drop_column("tenants", "suspended_reason")
    op.drop_column("tenants", "suspended_at")

    op.drop_index("uq_users_email", table_name="users")
    op.drop_constraint("chk_users_tenant_or_superadmin", "users", type_="check")
    op.drop_column("users", "suspended_reason")
    op.drop_column("users", "suspended_at")
    op.drop_column("users", "is_superadmin")
    op.alter_column("users", "tenant_id", nullable=False)
