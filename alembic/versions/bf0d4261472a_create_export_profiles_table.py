"""create export_profiles table (2026-09-01 导出功能批次)

Revision ID: bf0d4261472a
Revises: e562efa98b5c
Create Date: 2026-09-01 19:33:04.749717

对话导出（2026-09-01 导出功能批次）模板表：
- export_profiles：导出模板（profile_id=prof-<uuid16>；profile_name 唯一；
  四个 JSON 文本列 entity/filter/columns/layout 承载模板载荷——entity 为
  实体清单、filter 为筛选、columns 为列序、layout 含 column_aliases/
  summary 布局配置；created_by 记录创建账号）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf0d4261472a'
down_revision: Union[str, Sequence[str], None] = 'e562efa98b5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_export_profiles_profile_name', table_name='export_profiles')
    op.drop_table('export_profiles')
