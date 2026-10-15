"""add tenants.receipt_template

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("receipt_template", sa.Text, nullable=False, server_default="classic"),
    )


def downgrade() -> None:
    op.drop_column("tenants", "receipt_template")
