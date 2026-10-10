"""create chat_attachments and reconcile_reports tables (2026-08-31 对话对账批次 1)

Revision ID: e562efa98b5c
Revises: 49bfa97aad14
Create Date: 2026-08-31

对话对账（2026-08-31 批次 1）两张新表：
- chat_attachments：对话附件元数据（attachment_id=att-<uuid16>；内容落
  attachment_dir 临时目录，TTL 过期清理——6.5/11.2 规则 4 不保存完整
  原始文件，本表只存 sha256/路径等元数据）；
- reconcile_reports：对账分析报告（report_id=rpt-<uuid16>；status 状态机
  analyzed→confirmed→executed，DB 层 CheckConstraint 兜底；plan_hash
  confirm 后回填；*_json 为 JSON 文本列，TTL 24h 跨轮确认窗口过期清理）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e562efa98b5c'
down_revision: Union[str, Sequence[str], None] = '49bfa97aad14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chat_attachments',
        sa.Column('attachment_id', sa.String(length=64), nullable=False),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('filename', sa.String(length=256), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('storage_path', sa.String(length=512), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('attachment_id'),
    )
    op.create_index('ix_chat_attachments_thread_id', 'chat_attachments', ['thread_id'], unique=False)
    op.create_index('ix_chat_attachments_expires_at', 'chat_attachments', ['expires_at'], unique=False)

    op.create_table(
        'reconcile_reports',
        sa.Column('report_id', sa.String(length=64), nullable=False),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('file_id', sa.String(length=64), nullable=False),
        sa.Column('template_key', sa.String(length=128), nullable=False),
        sa.Column('mapping_version', sa.Integer(), nullable=False),
        sa.Column('entity', sa.String(length=64), nullable=False),
        sa.Column('subject_name', sa.String(length=256), nullable=False),
        sa.Column('period_note', sa.Text(), server_default='', nullable=False),
        sa.Column('source_file_hash', sa.String(length=64), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('summary_json', sa.Text(), server_default='{}', nullable=False),
        sa.Column('conversion_errors_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('entries_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('rows_json', sa.Text(), server_default='{}', nullable=False),
        sa.Column('data_completeness', sa.String(length=16), nullable=False),
        sa.Column('limitations_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('plan_hash', sa.String(length=64), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('analyzed', 'confirmed', 'executed')",
            name='ck_reconcile_reports_status',
        ),
        sa.PrimaryKeyConstraint('report_id'),
    )
    op.create_index('ix_reconcile_reports_thread_id', 'reconcile_reports', ['thread_id'], unique=False)
    op.create_index('ix_reconcile_reports_file_id', 'reconcile_reports', ['file_id'], unique=False)
    op.create_index('ix_reconcile_reports_expires_at', 'reconcile_reports', ['expires_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_reconcile_reports_expires_at', table_name='reconcile_reports')
    op.drop_index('ix_reconcile_reports_file_id', table_name='reconcile_reports')
    op.drop_index('ix_reconcile_reports_thread_id', table_name='reconcile_reports')
    op.drop_table('reconcile_reports')
    op.drop_index('ix_chat_attachments_expires_at', table_name='chat_attachments')
    op.drop_index('ix_chat_attachments_thread_id', table_name='chat_attachments')
    op.drop_table('chat_attachments')
