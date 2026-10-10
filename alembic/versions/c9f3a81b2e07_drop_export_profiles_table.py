"""drop export_profiles table (2026-09-01 导出功能批次移除)

Revision ID: c9f3a81b2e07
Revises: bf0d4261472a
Create Date: 2026-09-01

对话导出模板功能移除（用户拍板：导出不需要模板，模型直接传字段/筛选透传；
留总计行、去掉按月小计）——drop export_profiles 模板落库表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f3a81b2e07'
down_revision: Union[str, Sequence[str], None] = 'bf0d4261472a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('export_profiles')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        'export_profiles',
        sa.Column('profile_id', sa.String(length=64), nullable=False),
        sa.Column('profile_name', sa.String(length=128), nullable=False),
        sa.Column('entity_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('filter_json', sa.Text(), server_default='{}', nullable=False),
        sa.Column('columns_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('layout_json', sa.Text(), server_default='{}', nullable=False),
        sa.Column('created_by', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('profile_name', name='uq_export_profiles_profile_name'),
        sa.PrimaryKeyConstraint('profile_id'),
    )
    op.create_index('ix_export_profiles_profile_name', 'export_profiles', ['profile_name'], unique=True)
