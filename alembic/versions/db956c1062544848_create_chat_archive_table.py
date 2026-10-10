"""create chat_archive table (2026-08-24 定稿: session memory compression archive)

Revision ID: db956c1062544848
Revises: 92d64bb42eca
Create Date: 2026-08-24

会话记忆压缩机制（2026-08-24 定稿）存档表：压缩前的原始会话全量落库
（[{role, content}] 文本形态，不还原为消息对象），memory_lookup 工具按
(thread_id, segment_no) 查询；唯一约束保证同线程同段号幂等追加。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'db956c1062544848'
down_revision: Union[str, Sequence[str], None] = '92d64bb42eca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chat_archive',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('segment_no', sa.Integer(), nullable=False),
        sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('thread_id', 'segment_no', name='uq_chat_archive_thread_segment'),
    )
    op.create_index('ix_chat_archive_thread_id', 'chat_archive', ['thread_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chat_archive_thread_id', table_name='chat_archive')
    op.drop_table('chat_archive')
