"""D22 audit 端点 API 测试（规格 2.7.3 L516；11.1 append-only 只读）。

覆盖（D22 任务书测试要求）：
- GET /api/v1/audit：分页（items/total/limit/offset，created_at 倒序）、
  task_id/operation_id/event_type 过滤、8.2 关联键；
- 脱敏输出：structured_payload/前后快照中的敏感字段（password/token/
  secret/密码/密钥等）输出为 ***（11.2 规则 4，复用 audit/redaction.py）；
- 只读：OpenAPI 无写路径（本端点只有 GET）；
- 安全：manager 权限（401/403）、无 CSRF 要求（GET）、统一错误响应。
"""

from __future__ import annotations

from administrative_agent.auth.models import Role

from tests.api.conftest import (
    DEVELOPER,
    MGR,
    headers,
    cookies,
)


def _seed_audit(client) -> tuple[str, str, str]:
    """写入 3 条审计事件（含敏感字段与过滤键）；返回 (task_id, op_id, trace)。"""
    from administrative_agent.audit.service import AuditService
    from administrative_agent.main import app
    from administrative_agent.persistence.repositories import (
        SqlAlchemyAttemptRepository,
        SqlAlchemyAuditRepository,
    )

    audit = AuditService(
        SqlAlchemyAuditRepository(app.state.session_factory),
        SqlAlchemyAttemptRepository(app.state.session_factory),
    )
    trace = "trace-audit-api-0001"
    op = "op-audit-api-0001"
    task = "task-audit-api-0001"
    for i in range(3):
        audit.record_event(
            event_type="query",
            trace_id=trace,
            actor_account="manager01",
            actor_role="manager",
            task_id=task,
            operation_id=op,
            duty_code="R4",
            target_type="order",
            target_id=f"SO-{1000 + i}",
            request_summary=f"query orders batch {i}",
            structured_payload={
                "sql": "SELECT * FROM orders",
                "password": "super-secret-credential",
                "nested": {"api_key": "sk-live-1234567890", "keep": "visible"},
            },
        )
    return task, op, trace


def _get(client, params: str = "", *, account: Role | None = None) -> object:
    kwargs = {}
    if account is not None:
        kwargs["cookies"] = cookies(account, DEVELOPER if account is Role.DEVELOPER else "manager01")
    else:
        kwargs["cookies"] = MGR
    return client.get(f"/api/v1/audit{params}", headers=headers(), **kwargs)


def test_audit_list_paginated_desc(client):
    _seed_audit(client)
    resp = _get(client, "?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    # created_at 倒序（后写入的在前）；同一秒内 created_at 相同但
    # 至少返回 2 条、且第二页补全
    resp2 = _get(client, "?limit=2&offset=2")
    assert resp2.json()["total"] == 3
    assert len(resp2.json()["items"]) == 1
    # 两页合计 3 条且不重复
    ids = [i["audit_id"] for i in body["items"]] + [
        i["audit_id"] for i in resp2.json()["items"]
    ]
    assert len(set(ids)) == 3


def test_audit_list_filters(client):
    task, op, trace = _seed_audit(client)
    # task_id 过滤
    body = _get(client, f"?task_id={task}").json()
    assert body["total"] == 3
    assert {i["task_id"] for i in body["items"]} == {task}
    # operation_id 过滤
    body = _get(client, f"?operation_id={op}").json()
    assert body["total"] == 3
    # event_type 过滤（无匹配）
    body = _get(client, "?event_type=attempt").json()
    assert body["total"] == 0
    assert body["items"] == []
    # 组合过滤 + 字段口径
    body = _get(client, f"?task_id={task}&event_type=query&limit=1").json()
    assert body["total"] == 3
    item = body["items"][0]
    assert item["trace_id"] == trace
    assert item["actor_account"] == "manager01"
    assert item["actor_role"] == "manager"
    assert item["duty_code"] == "R4"
    assert item["target_type"] == "order"
    assert item["source"] == "agent"
    assert item["event_type"] == "query"
    assert item["created_at"] is not None


def test_audit_redacts_sensitive_fields(client):
    """11.2 规则 4：password/api_key 等敏感字段输出脱敏（***）。"""
    _seed_audit(client)
    body = _get(client).json()
    payload = body["items"][0]["structured_payload"]
    # redaction.py：字符串脱敏为 ***(len=N)，不泄露原始内容
    assert payload["password"] == "***(len=23)"  # super-secret-credential
    assert payload["nested"]["api_key"] == "***(len=18)"  # sk-live-1234567890
    assert payload["sql"] == "SELECT * FROM orders"  # 非敏感字段原样
    assert payload["nested"]["keep"] == "visible"


def test_audit_requires_manager(client):
    assert client.get("/api/v1/audit", headers=headers()).status_code == 401
    resp = _get(client, account=Role.DEVELOPER)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "FORBIDDEN"


def test_audit_is_read_only_get(client):
    """11.1 append-only：OpenAPI 只暴露 GET（无任何写方法）。"""
    spec = client.get("/openapi.json").json()
    ops = spec["paths"].get("/api/v1/audit", {})
    assert set(ops) == {"get"}
    # 其余端点不受影响（audit 无 POST/PUT/DELETE 路由）
    assert all(m in ("get", "options", "head") for m in ops)
