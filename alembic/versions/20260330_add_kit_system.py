"""add kit system and type hierarchy

Revision ID: 20260330_add_kit_system
Revises: 
Create Date: 2026-03-30 00:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260330_add_kit_system"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kits",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("barcode_id", sa.String(), unique=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("image_path", sa.String()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "kit_components",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kit_id", sa.String(), sa.ForeignKey("kits.id", ondelete="CASCADE")),
        sa.Column("component_id", sa.String(), sa.ForeignKey("components.id", ondelete="CASCADE")),
        sa.Column("quantity", sa.Integer(), server_default="1"),
        sa.Column("notes", sa.Text()),
        sa.Column("position", sa.Integer()),
        sa.UniqueConstraint("kit_id", "component_id"),
    )

    op.add_column("components", sa.Column("type_path", sa.String()))
    op.add_column("components", sa.Column("type_data", postgresql.JSONB()))
    op.create_index("idx_components_type_path", "components", ["type_path"])


def downgrade() -> None:
    op.drop_index("idx_components_type_path", table_name="components")
    op.drop_column("components", "type_data")
    op.drop_column("components", "type_path")
    op.drop_table("kit_components")
    op.drop_table("kits")
