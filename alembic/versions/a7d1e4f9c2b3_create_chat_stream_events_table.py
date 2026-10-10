"""create chat_stream_events table (2026-09-19 可观测性第 0 步：SSE 事件流结构性元数据落库)

Revision ID: a7d1e4f9c2b3
Revises: c9f3a81b2e07
Create Date: 2026-09-19

SSE 事件流结构性元数据（A 档：只落结构，绝不落模型输入输出原文）持久化，
容器重建后仍可回放「失败对话在哪一步、调了什么工具、确认卡长什么样、
为什么被抑制」。保留期 14 天（清理逻辑在 web/stream_persistence.py）。

两条本仓约定（勿删，勿模仿到大表上）：
1. **本迁移含 PG-only 约束，``alembic --autogenerate`` 会误报差异**
   （ORM 模型 ``ChatStreamEventRow.__table_args__`` 刻意不含这三条——它们
   是 PG 函数，写进 ORM 会让 SQLite ``create_all`` 报 no such function，
   炸掉 API 测试套件）。**本仓约定迁移全手写**，勿用 autogenerate 覆盖。
2. 若将来要把 ``pg_column_size`` 这类约束加到**已有大表**，必须
   ``ADD CONSTRAINT ... NOT VALID`` + 单独 ``VALIDATE CONSTRAINT`` 两步，
   否则建约束即全表校验并长持锁。本表是新表，无此问题。

明确否决 ``PARTITION BY RANGE (created_at)`` + ``DROP PARTITION`` 清理：
PG 要求分区表主键必须包含分区键 → PK 变成 ``(event_id, created_at)``，
破坏「``event_id`` 单键 ``ON CONFLICT DO NOTHING`` 幂等」语义；单机 PG +
14 天窗口下分区收益不抵复杂度。清理走 ``created_at`` 索引上的分批 DELETE。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7d1e4f9c2b3'
down_revision: Union[str, Sequence[str], None] = 'c9f3a81b2e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chat_stream_events',
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('stream_id', sa.String(length=64), nullable=False),
        sa.Column('thread_id', sa.String(length=128), nullable=True),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('record_kind', sa.String(length=16), nullable=False, server_default=sa.text("'pushed'")),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('sequence >= 0', name='ck_chat_stream_events_sequence'),
        sa.CheckConstraint(
            "record_kind IN ('pushed', 'suppressed', 'stream_created', 'unmapped')",
            name='ck_chat_stream_events_record_kind',
        ),
        sa.PrimaryKeyConstraint('event_id'),
    )
    op.create_index(
        'ix_chat_stream_events_stream_seq', 'chat_stream_events',
        ['stream_id', 'sequence'], unique=False,
    )
    op.create_index(
        'ix_chat_stream_events_thread_created', 'chat_stream_events',
        ['thread_id', 'created_at'], unique=False,
    )
    op.create_index(
        'ix_chat_stream_events_task_created', 'chat_stream_events',
        ['task_id', 'created_at'], unique=False,
    )
    op.create_index(
        'ix_chat_stream_events_created_at', 'chat_stream_events',
        ['created_at'], unique=False,
    )

    # ---- PG-only 第二道锁（应用层写错 → DB 直接拒写） ----
    # 1. payload 必须是 JSON 对象（NOT NULL 不排除 '[]'::jsonb，这条补齐）
    op.create_check_constraint(
        'ck_chat_stream_events_payload_is_object',
        'chat_stream_events',
        "jsonb_typeof(payload) = 'object'",
    )
    # 2. 投影后 payload 硬性上界：任何投影漏网的原文被 DB 拒绝而非静默入库
    op.create_check_constraint(
        'ck_chat_stream_events_payload_size',
        'chat_stream_events',
        'pg_column_size(payload) <= 4096',
    )
    # 3. 两个 chunk 事件（模型原文流式增量）的 payload **只允许 `chunk_chars`
    #    这一个数字键**：投影把 ``chunk`` 原文降维成「输出了 N 字」的结构事实
    #    （见 web/stream_projection.py 的规则 11/12）。旧实现要求 payload 必须
    #    等于 ``{}`` 与本投影自相矛盾——PG 上整批写入被此约束拒绝、
    #    面板误显 ``database_unavailable``（生产实为 schema/投影漂移）。
    #    ``payload - 'chunk_chars' = '{}'::jsonb``：删掉该键后剩余必须为空 →
    #    **任何其它内容依然进不来**（比「必须为空」更精确，锁不放松）。
    op.create_check_constraint(
        'ck_chat_stream_events_text_events_empty',
        'chat_stream_events',
        "event_type NOT IN ('agent_reply_chunk', 'agent_reasoning_chunk') "
        "OR (jsonb_typeof(payload->'chunk_chars') = 'number' "
        "AND (payload - 'chunk_chars') = '{}'::jsonb)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'ck_chat_stream_events_text_events_empty', 'chat_stream_events', type_='check'
    )
    op.drop_constraint(
        'ck_chat_stream_events_payload_size', 'chat_stream_events', type_='check'
    )
    op.drop_constraint(
        'ck_chat_stream_events_payload_is_object', 'chat_stream_events', type_='check'
    )
    op.drop_index('ix_chat_stream_events_created_at', table_name='chat_stream_events')
    op.drop_index('ix_chat_stream_events_task_created', table_name='chat_stream_events')
    op.drop_index('ix_chat_stream_events_thread_created', table_name='chat_stream_events')
    op.drop_index('ix_chat_stream_events_stream_seq', table_name='chat_stream_events')
    op.drop_table('chat_stream_events')
