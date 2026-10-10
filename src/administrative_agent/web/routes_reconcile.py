"""对话对账报告详情 API（D19 ReconciliationReport
跨轮确认载体展示）。

- GET /api/v1/chat/reports/{report_id}?thread_id=…  对账报告详情

线程归属判定（fail closed）：报告属于创建它的 thread_id，请求不带会话
上下文（Account 只有账号粒度，无线程列表），故详情端点要求 query 参数
``thread_id`` 且必须与 ``record.thread_id`` 一致——不匹配一律 404（与
delete_thread 同口径：不泄露存在性）。

响应 = 报告信封（summary/subject_match 自 summary_json 取出）+ entries
全量（entries_json 反序列化，DiffEntry 形态原样透传，前端渲染差异表）。
只读 GET 端点，不落审计（11.1 审计面向写操作与追查链，报告查看可省）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from administrative_agent.auth.dependencies import require_manager
from administrative_agent.domains.reconcile_reports import ReconcileReportStore

logger: structlog.stdlib.BoundLogger = structlog.get_logger("web.routes_reconcile")

router = APIRouter(prefix="/api/v1/chat", tags=["chat-reconcile"])


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），默认 SessionLocal。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


class ReconcileReportOut(BaseModel):
    """对账报告详情（D19 跨轮确认载体；entries 全量 + 报告信封）。"""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    thread_id: str
    entity: str
    subject_name: str
    subject_match: dict[str, Any]
    period_note: str
    row_count: int
    summary: dict[str, Any]
    conversion_errors: list[Any]
    limitations: list[Any]
    data_completeness: str
    status: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    entries: list[Any] = []


def _parse_json(text: str | None, fallback: Any, label: str) -> Any:
    """JSON 文本列解析（落库内容异常 → 500 fail closed，不猜测）。"""
    try:
        return json.loads(text) if text else fallback
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "报告数据异常，请重新对账后查看。"},
        ) from exc


@router.get("/reports/{report_id}", response_model=ReconcileReportOut)
def get_reconcile_report(
    request: Request,
    report_id: str,
    thread_id: str = Query(min_length=1, max_length=128),
    _: Any = Depends(require_manager),
) -> ReconcileReportOut:
    """对账报告详情（manager；thread_id 归属校验，不匹配一律 404）。

    - 报告不存在或 thread_id 不匹配 → 404（同口径，不泄露存在性）；
    - 读取前先清过期报告（TTL；best effort，清理失败不阻断读取）——
      过期行被删后自然落入 404，与「报告不存在」同口径；
    - summary/subject_match 自 summary_json 信封取出（report_to_json
      落库结构），entries/conversion_errors/limitations 为对应 *_json
      列反序列化（DiffEntry/RowError 全量形态，前端直接渲染）。
    """
    store = ReconcileReportStore(_session_factory(request))
    try:
        store.delete_expired()
    except Exception:
        logger.warning("report_delete_expired_failed", exc_info=True)
    record = store.get(report_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "报告不存在。"},
        )
    envelope = _parse_json(record.summary_json, {}, "summary")
    if not isinstance(envelope, dict):
        envelope = {}
    return ReconcileReportOut(
        report_id=record.report_id,
        thread_id=record.thread_id,
        entity=record.entity,
        subject_name=record.subject_name,
        subject_match=envelope.get("subject_match") or {},
        period_note=record.period_note,
        row_count=record.row_count,
        summary=envelope.get("summary") or {},
        conversion_errors=_parse_json(
            record.conversion_errors_json, [], "conversion_errors"
        ),
        limitations=_parse_json(record.limitations_json, [], "limitations"),
        data_completeness=record.data_completeness,
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
        entries=_parse_json(record.entries_json, [], "entries"),
    )
