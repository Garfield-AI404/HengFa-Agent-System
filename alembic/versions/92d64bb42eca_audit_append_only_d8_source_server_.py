"""audit append-only (D8): source server_default + trigger

Revision ID: 92d64bb42eca
Revises: 2cbb7f06dcfa
Create Date: 2026-08-22 09:56:21.774381

两处审计表加固（RV-04 决策 c / O-1 跟踪项 + 11.1 append-only 要求）：
1. audit_events.source 补 DB 层 server_default='agent'：此前仅有 ORM 层
   Python default，绕过 ORM 的裸 SQL INSERT（如运维脚本/演示直连）会因
   NOT NULL 被拒——补 server_default 后裸 INSERT 亦得到 'agent'。
2. append-only 触发器：audit_events 禁止 UPDATE/DELETE（RAISE EXCEPTION）。
   选择 DB 层触发器而非仅应用层强制的理由：
   - 审计完整性必须由数据库物理约束保证，应用层约束防不住误操作、
     直连 SQL 或未来其他写入方（11.1"普通应用账号不能 update 或 delete"）；
   - RV-11 通过标准"记录不可覆盖"需要可验证的强制（真实 PG 上
     UPDATE/DELETE 被拒可实测）；
   - task_attempts 不属于 append-only：result 需回填同一条 attempt 记录
     （11.2 规则 2），触发器只作用于 audit_events。

兼容性：已有行 source 均有值（ORM 写入时 Python default 填充），
server_default 只影响新插入；触发器为新增约束，不影响已有数据。

迁移链修正（D25，16.7 上报处理决策，2026-08-23 项目负责人批准方案 A）：
D3（2cbb7f06dcfa）已创建同名触发器 trg_audit_events_append_only（函数
f_audit_events_append_only），本迁移若再 CREATE 同名触发器，全新库
upgrade head 会因 DuplicateObject 失败（12.2-3 全新环境部署演练实测暴露）。
upgrade 先 DROP IF EXISTS D3 的触发器与函数再创建本迁移对象；downgrade
对称恢复 D3 对象，保证 downgrade→upgrade 循环完整。变更面仅触发器对象，
行为等价（append-only 语义不变，终态 = 既有环境一致的
audit_events_append_only_protect 生效）；供 RV-04/RV-11 复核。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '92d64bb42eca'
down_revision: Union[str, Sequence[str], None] = '2cbb7f06dcfa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 触发器与函数名（downgrade 精确清理）
_TRIGGER_NAME = "trg_audit_events_append_only"
_FUNCTION_NAME = "audit_events_append_only_protect"


def upgrade() -> None:
    """Upgrade schema."""
    # O-1：source 补 DB 层 server_default='agent'（裸 SQL INSERT 不再被 NOT NULL 拒）
    op.alter_column(
        "audit_events",
        "source",
        server_default=sa.text("'agent'"),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    # 11.1 append-only：DB 层触发器禁止 UPDATE/DELETE
    # 迁移链修正（16.7 决策）：D3（2cbb7f06dcfa）已创建同名触发器，先清理
    # D3 对象再创建自身对象，全新库 upgrade head 不再 DuplicateObject
    # （终态与既有环境一致：本迁移的 audit_events_append_only_protect 生效）。
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS f_audit_events_append_only()")
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: UPDATE/DELETE forbidden';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION_NAME}()
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON audit_events")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
    # 迁移链修正（16.7 决策）对称恢复：回到 D3（2cbb7f06dcfa）创建的
    # f_audit_events_append_only + trg_audit_events_append_only，
    # 保证 downgrade→upgrade 循环完整（与 D3 upgrade 初始状态一致）。
    op.execute(
        """
        CREATE FUNCTION f_audit_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: update or delete is not allowed';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION f_audit_events_append_only();
        """
    )
    op.alter_column(
        "audit_events",
        "source",
        server_default=None,
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
