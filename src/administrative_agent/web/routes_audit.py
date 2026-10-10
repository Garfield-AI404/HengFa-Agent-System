"""audit 端点（规格 2.7.3 L516；11.1 业务审计；D22 前端整合）。

- GET /api/v1/audit  分页查询业务审计（manager；脱敏输出）

设计要点：
- **只读**：audit_events 是 append-only（11.1：DB 层触发器强制，本模块
  不定义任何写路径）；
- **脱敏输出**：structured_payload/before_snapshot/after_snapshot 落库前
  已经 audit/service.py 的 redact_mapping 脱敏；返回前再经
  redaction.py 处理一遍（纵深防御——防御性深度拷贝，不暴露密码/API
  Key/密钥等敏感字段，11.2 规则 4）；
- 过滤：task_id / operation_id / event_type（7.4 追查链 L1179 关联键）；
- 分页：created_at 倒序，items/total/limit/offset（与 D17/D18 一致）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from administrative_agent.audit.redaction import is_sensitive_key, redact_value
from administrative_agent.audit.service import AuditService
from administrative_agent.auth.dependencies import require_manager
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
)
from administrative_agent.web.schemas import (
    AuditEventOut,
    AuditListResponse,
)

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

#: 列表分页上限（与 D17/D18 分页语义一致：最大 200）
MAX_LIST_LIMIT = 200


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），默认 SessionLocal。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _audit(request: Request) -> AuditService:
    factory = _session_factory(request)
    return AuditService(
        SqlAlchemyAuditRepository(factory),
        SqlAlchemyAttemptRepository(factory),
    )


def _redacted(value: Any) -> Any:
    """脱敏输出（复用 audit/redaction.py；纵深防御，11.2 规则 4）。

    落库前已脱敏（AuditService.record_event），此处再次脱敏防绕过；
    已脱敏占位（``***`` 开头）幂等跳过——避免二次脱敏改写长度标记。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key) and not _is_marker(item):
                out[key] = redact_value(item)
            else:
                out[key] = _redacted(item)
        return out
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _is_marker(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("***")


def _audit_out(event) -> AuditEventOut:
    return AuditEventOut(
        audit_id=event.audit_id,
        trace_id=event.trace_id,
        actor_account=event.actor_account,
        actor_role=event.actor_role,
        event_type=event.event_type,
        task_id=event.task_id,
        operation_id=event.operation_id,
        duty_code=event.duty_code,
        policy_key=event.policy_key,
        policy_version=event.policy_version,
        policy_hash=event.policy_hash,
        target_type=event.target_type,
        target_id=event.target_id,
        request_summary=event.request_summary,
        structured_payload=_redacted(event.structured_payload),
        before_snapshot=_redacted(event.before_snapshot),
        after_snapshot=_redacted(event.after_snapshot),
        source=event.source,
        result_code=event.result_code,
        created_at=event.created_at,
    )


@router.get("", response_model=AuditListResponse)
def list_audit(
    request: Request,
    task_id: str | None = Query(default=None, max_length=128),
    operation_id: str | None = Query(default=None, max_length=128),
    event_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Any = Depends(require_manager),
) -> AuditListResponse:
    """业务审计分页（2.7.3 L516；11.1 append-only 只读；脱敏输出）。"""
    items, total = _audit(request).list_events(
        limit=limit,
        offset=offset,
        task_id=task_id,
        operation_id=operation_id,
        event_type=event_type,
    )
    return AuditListResponse(
        items=[_audit_out(e) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )
