"""D19 R6 契约：对账只读边界（scope 最小权限 + 本地拦截 fail closed）。

## 2026-08-29 重写说明

D26 删除了规则化流水线（`agent/intent.py`），本文件原有的 6 个用例中
4 个随之永久失效——它们测的是 `validate_intent` 与 `OPERATION_SCOPES`
（操作级 scope 映射与角色校验），这两个能力已改由 LLM 决策 + 确定性
防护层承担，不应恢复。

**仍然活着、且此前零覆盖**的是 `mcp/scopes.py` 的本地写保护层：它是
请求发往 ERP 前的最后一道拦截。本文件重写为对它的契约断言。

## 覆盖

- `REQUIRED_SCOPES` 恰为 2.4 八项（防漂移；[2026-09-02 R7 收支写] 追加
  finance:create/finance:update 两项占位 scope，待 ERP 权限表核对）；
- 一期只申请 `reconciliation:query`，不申请 `reconciliation:write`；
- `verify_scope` 对八项放行，对 write / 空串 / 大小写变体 / 通配符一律
  拒绝（fail closed，2.4 最小 scope）；
- 对账类实体（工资发放等）不在通用查询白名单内（不超前开放）。
"""

from __future__ import annotations

import pytest

from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.scopes import (
    QUERYABLE_ENTITIES,
    REQUIRED_SCOPES,
    verify_scope,
)

# 2.4 最小 scope 八项（顺序与 scopes.py 一致；改动此处需同步规格。
# [2026-09-02 R7 收支写] finance:create/finance:update 为占位 scope，
# 待 ERP 权限表核对后确认，上线前需复核本文件）
EXPECTED_SCOPES = frozenset(
    {
        "erp:query",
        "attendance:create",
        "attendance:update",
        "orders:create_bundle",
        "orders:update",
        "reconciliation:query",
        "finance:create",  # R7 占位 scope（待 ERP 权限表核对）
        "finance:update",  # R7 占位 scope（待 ERP 权限表核对）
    }
)


def test_required_scopes_exactly_eight_minimal():
    """2.4 最小 scope 恰为八项：多申请一项即越权，少一项即功能缺失。

    用集合而非逐个 `in` 断言——任何新增/删除都会让本用例失败，
    迫使改动者显式确认这是有意的规格变更。
    """
    assert frozenset(REQUIRED_SCOPES) == EXPECTED_SCOPES
    assert len(REQUIRED_SCOPES) == len(EXPECTED_SCOPES)  # 无重复项


def test_reconciliation_query_requested_not_write():
    """L167-169：一期只申请 reconciliation:query，不申请 reconciliation:write。"""
    assert "reconciliation:query" in REQUIRED_SCOPES
    assert "reconciliation:write" not in REQUIRED_SCOPES


def test_verify_scope_accepts_all_required_scopes():
    """八项全部放行（无异常即通过）。"""
    for scope in REQUIRED_SCOPES:
        verify_scope(scope)  # 不抛即通过


def test_verify_scope_rejects_reconciliation_write():
    """reconciliation:write 不在 2.4 最小 scope → 拒绝。

    本单元不申请，也不允许模型以任何途径获得写 ERP 的通用 scope。
    """
    with pytest.raises(MCPError) as exc:
        verify_scope("reconciliation:write")
    assert exc.value.code == InternalCode.SCOPE_NOT_ALLOWED.value
    assert exc.value.category == ErrorCategory.LOCAL
    assert "not allowed" in exc.value.message
    assert exc.value.details == {"scope": "reconciliation:write"}


@pytest.mark.parametrize(
    "scope",
    [
        "",  # 空串
        "RECONCILIATION:QUERY",  # 大小写变体（不做归一化）
        "erp:query ",  # 尾随空格
        "erp:*",  # 通配符（不做通配展开）
        "delete",  # 危险动词不是 scope
        "erp:admin",  # 管理员权限不能作为 Agent 凭据（2.4）
    ],
)
def test_verify_scope_fails_closed_on_anything_outside_six(scope):
    """白名单外一律拒绝（fail closed）。

    `verify_scope` 是 `scope not in REQUIRED_SCOPES` 的精确匹配，
    不做归一化/通配/前缀。这些变体若被放行等于绕过最小权限，
    故逐条钉死。
    """
    with pytest.raises(MCPError) as exc:
        verify_scope(scope)
    assert exc.value.code == InternalCode.SCOPE_NOT_ALLOWED.value


def test_non_queryable_entities_remain_out_of_whitelist():
    """仍不可查询的实体必须留在白名单外（防白名单被无差别放大）。

    [2026-09-12 只读扩展] 原用例钉 `salary_payments`——用户本次已拍板将该
    实体（连同 ar/ap_payments）开放**只读**查询，故改钉真正永远不可由 Agent
    触达的实体：users/webhook_subscriptions/file_attachments。
    """
    assert "users" not in QUERYABLE_ENTITIES
    assert "webhook_subscriptions" not in QUERYABLE_ENTITIES
    assert "file_attachments" not in QUERYABLE_ENTITIES
    assert "reconciliation" not in QUERYABLE_ENTITIES
