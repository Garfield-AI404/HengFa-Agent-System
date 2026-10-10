"""add read-tool arguments/ok to tool_query_archive (2026-10-06 确认卡第 3 批 §3.8 读工具调用参数落审计)

Revision ID: b1f2a3c4d5e6
Revises: a7d1e4f9c2b3
Create Date: 2026-10-06

问题（已侦察确认）：模型的**读**工具调用参数不可回放——SSE tool_running 只有
``{tool_name, stage}``、chat_stream_events 按设计「只落结构不落模型输入输出
原文」、checkpoint 只记通过校验后的**写**动作。确认卡五轮实测里大量归因卡在
「它到底查了什么 filter/fields/时间窗」。

落点（用户拍板）：扩展现有 ``tool_query_archive``（不新建表）：
- 新增 ``arguments JSONB NULL``：本次读工具调用**模型传的入参**（JSON 形态）；
- 新增 ``ok BOOLEAN NULL``：本次调用成败。

两列**均可空** → 历史行不受影响（NULL），无需回填。写入条件同步放宽：只读
工具在「正常返回（成功 / 0 行成功 / 失败）+ 执行抛异常 + 参数校验失败」三条
路径各落一行（未知工具 / NotImplementedError / 写工具 / archive 未接线不落），
见 tools_node 的 ``_archive_read_tool``；挂给模型的 query_id（可回查标识）
**仍仅在「成功 + 非空 items + 存档标识」时给出**（模型可见行为不变）。

本仓约定：迁移全手写（``alembic --autogenerate`` 会误报差异，见
``a7d1e4f9c2b3`` 文件头说明）。本次仅 ADD/DROP 两个可空列，无 PG-only 约束。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b1f2a3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'a7d1e4f9c2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tool_query_archive',
        sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'tool_query_archive',
        sa.Column('ok', sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tool_query_archive', 'ok')
    op.drop_column('tool_query_archive', 'arguments')
