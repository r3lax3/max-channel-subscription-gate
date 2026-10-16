"""users.free_access, payments.provider_order_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("free_access", sa.Boolean(), nullable=False, server_default="false")
        )
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("provider_order_id", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_payments_provider_order_id", ["provider_order_id"])


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("uq_payments_provider_order_id", type_="unique")
        batch.drop_column("provider_order_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("free_access")
