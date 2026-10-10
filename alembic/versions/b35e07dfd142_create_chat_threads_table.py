"""create chat_threads table (2026-08-24: conversation thread index for sidebar)

Revision ID: b35e07dfd142
Revises: db956c1062544848
Create Date: 2026-08-24 21:24:29.042817

对话会话（thread）索引表：checkpoint 只有 thread_id 粒度、没有
「账号 → 线程列表」索引——本表补齐会话列表/恢复/删除的账目层：

- thread_id 主键：与 LangGraph checkpoint 的 thread_id 一一对应；
- account：归属账号（会话列表只返回当前账号；删除时校验归属，不泄露
  存在性——account 不匹配一律 404）；
- title：会话标题 = 首条用户消息截断 40 字符（upsert 只首插，不覆盖）；
- updated_at：每次提交消息更新（列表按 updated_at DESC 排序）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b35e07dfd142'
down_revision: Union[str, Sequence[str], None] = 'db956c1062544848'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chat_threads',
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('account', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('thread_id'),
    )
    op.create_index('ix_chat_threads_account', 'chat_threads', ['account'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chat_threads_account', table_name='chat_threads')
    op.drop_table('chat_threads')
