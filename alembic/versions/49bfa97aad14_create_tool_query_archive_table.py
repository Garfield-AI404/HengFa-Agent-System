"""create tool_query_archive table (2026-08-28 两级数据访问: tool query raw data archive)

Revision ID: 49bfa97aad14
Revises: b35e07dfd142
Create Date: 2026-08-28

两级数据访问方案（2026-08-28 批次 2a）存档表：查询类工具（erp_query）
成功路径在结果写对话历史前将原始全量明细落库（items JSONB 不裁剪），
memory_lookup 按 (thread_id, query_id) 查回；query_id 唯一（tool-call-…
UUID 全局唯一）；随会话删除联动清理（ArchiveStore.delete_thread 覆盖本表，
11.4 仅会话内可查）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '49bfa97aad14'
down_revision: Union[str, Sequence[str], None] = 'b35e07dfd142'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tool_query_archive',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('query_id', sa.String(length=128), nullable=False),
        sa.Column('entity', sa.String(length=64), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('queried_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('thread_id', 'query_id', name='uq_tool_query_archive_thread_query'),
    )
    op.create_index('uq_tool_query_archive_query_id', 'tool_query_archive', ['query_id'], unique=True)
    op.create_index('ix_tool_query_archive_thread_id', 'tool_query_archive', ['thread_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_tool_query_archive_thread_id', table_name='tool_query_archive')
    op.drop_index('uq_tool_query_archive_query_id', table_name='tool_query_archive')
    op.drop_table('tool_query_archive')
