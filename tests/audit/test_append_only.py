"""append-only 强制测试（D8/RV-11：11.1 只追加，不可覆盖须有测试验证）。

- 服务层：AuditService 与 AuditRepository 协议不暴露任何 update/delete 方法
  （普通应用路径不可覆盖）；
- 仓储层（SQLAlchemy 实现）同样没有 update/delete 方法；
- DB 层物理强制（触发器拒 UPDATE/DELETE）由 test_pg_integration.py 在
  真实 PostgreSQL 上验证（演示容器）。
"""

from __future__ import annotations

from administrative_agent.audit.service import (
    AuditRepository,
    AuditService,
    AttemptRepository,
)
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
)


def _class_attr_names(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_audit_service_has_no_update_or_delete() -> None:
    names = _class_attr_names(AuditService)
    assert "update" not in names
    assert "delete" not in names
    assert not any("update" in n.lower() or "delete" in n.lower() for n in names)


def test_audit_repository_protocol_has_no_update_or_delete() -> None:
    names = _class_attr_names(AuditRepository)
    # 只读方法集（append + list_by + D22 list_page 分页只读查询）；
    # 11.1 契约：绝不出现任何 update/delete 路径
    assert names == {"append", "list_by", "list_page"}, names
    assert not any("update" in n.lower() or "delete" in n.lower() for n in names)


def test_sqlalchemy_audit_repository_has_no_update_or_delete() -> None:
    names = _class_attr_names(SqlAlchemyAuditRepository)
    assert "update" not in names
    assert "delete" not in names


def test_attempt_repository_is_not_append_only() -> None:
    """task_attempts 允许回填（result 调用后 UPDATE 同一行）——设计如此。"""
    names = _class_attr_names(AttemptRepository)
    assert "record_result" in names
    assert "append_attempt" in names
    names_sql = _class_attr_names(SqlAlchemyAttemptRepository)
    assert "record_result" in names_sql
