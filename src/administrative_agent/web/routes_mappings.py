"""Excel 映射管理 API（规格 2.7.3 API 契约 + 6.3 处理规则）。

- GET   /api/v1/mappings               查询映射版本（分页，D17 语义）
- GET   /api/v1/mappings/{versionId}   版本详情（含列级快照）
- POST  /api/v1/mappings/import        导入 draft 映射（2.7.3）
- POST  /api/v1/mappings/{versionId}/publish  发布映射版本（2.7.3）
- POST  /api/v1/mappings/{versionId}/retire  退役映射版本（同型扩展，
  2.7.3 表未列退役端点——按最小实现，材料 RV-27 §5 说明）

角色与安全：
- 全部端点 require_manager（2.7.2 /mappings 页面 manager 允许、developer
  拒绝；2.7.3 三个契约端点均 manager）；
- 写 API（import/publish/retire）校验 CSRF（Origin/Referer 白名单，
  fail closed，D4 check_csrf）；
- 统一错误响应 error_code/message/details/trace_id（2.7.3，main.py
  全局 handler 组装；服务层领域异常在此映射为稳定错误码）。

Agent 边界（RV-27 通过标准）：本模块不创建任何 Agent 工具/意图/R 流程/
确认卡片；不经过 task/审计 attempt 体系（人工管理操作，6.3 L798）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.files.mapping_service import (
    ExcelMappingService,
    MappingError,
    MappingNotFoundError,
    MappingNotPublishedError,
    MappingPublishedExistsError,
    MappingStateError,
    MappingValidationError,
    MappingVersionRecord,
    SqlAlchemyMappingStore,
)
from administrative_agent.web.routes_auth import check_csrf
from administrative_agent.web.schemas import (
    MappingColumnOut,
    MappingImportRequest,
    MappingImportResult,
    MappingListResponse,
    MappingVersionOut,
)

router = APIRouter(prefix="/api/v1/mappings", tags=["mappings"])

#: 列表分页上限（与 mcp-api.md §4 / D17 QuerySpec 一致：最大 200）
MAX_LIST_LIMIT = 200


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），
    生产默认 SessionLocal（persistence.database）。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _service(request: Request) -> ExcelMappingService:
    return ExcelMappingService(SqlAlchemyMappingStore(_session_factory(request)))


def _version_out(record: MappingVersionRecord) -> MappingVersionOut:
    return MappingVersionOut(
        version_id=record.version_id,
        template_key=record.template_key,
        version=record.version,
        sheet_name=record.sheet_name,
        header_row=record.header_row,
        status=record.status,
        source_file_hash=record.source_file_hash,
        created_by=record.created_by,
        created_at=record.created_at,
        published_at=record.published_at,
        columns=[MappingColumnOut(**c.__dict__) for c in record.columns],
    )


def _mapping_error_to_http(exc: MappingError) -> HTTPException:
    """领域异常 → 稳定错误码（2.7.3：前端分支只读 error_code）。"""
    if isinstance(exc, MappingValidationError):
        http_status = status.HTTP_400_BAD_REQUEST
    elif isinstance(exc, MappingNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, MappingPublishedExistsError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, MappingNotPublishedError):
        http_status = status.HTTP_404_NOT_FOUND
    else:  # MappingStateError 等
        http_status = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=http_status,
        detail={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details or None,
        },
    )


def _columns_from_request(columns) -> list[dict[str, Any]]:
    return [
        {
            "excel_column": c.excel_column,
            "target_field": c.target_field,
            "data_type": c.data_type,
            "conversion_rule": c.conversion_rule,
            "is_required": c.is_required,
            "enum_mapping": c.enum_mapping,
            "default_value": c.default_value,
        }
        for c in columns
    ]


# ---------------------------------------------------------------------------
# 查询（GET）
# ---------------------------------------------------------------------------


@router.get("", response_model=MappingListResponse)
def list_mappings(
    request: Request,
    template_key: str | None = Query(default=None, max_length=128),
    mapping_status: str | None = Query(default=None, alias="status", max_length=16),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Any = Depends(require_manager),
) -> MappingListResponse:
    """版本列表（分页 + total，D17 语义；历史版本保留显示，规则 6）。"""
    items, total = _service(request).list_versions(
        template_key=template_key,
        status=mapping_status,
        limit=limit,
        offset=offset,
    )
    return MappingListResponse(
        items=[_version_out(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{version_id}", response_model=MappingVersionOut)
def get_mapping(request: Request, version_id: int, _: Any = Depends(require_manager)) -> MappingVersionOut:
    """版本详情（含列级映射快照）。"""
    try:
        record = _service(request).get_version(version_id)
    except MappingError as exc:
        raise _mapping_error_to_http(exc) from exc
    return _version_out(record)


# ---------------------------------------------------------------------------
# 写（manager + CSRF；2.7.3 稳定错误契约）
# ---------------------------------------------------------------------------


@router.post("/import", response_model=MappingImportResult)
def import_mapping(
    request: Request,
    body: MappingImportRequest,
    account: Account = Depends(require_manager),
) -> MappingImportResult:
    """导入 draft 映射（规则 3/4/6 + 转换规则合法性校验）。"""
    check_csrf(request)
    service = _service(request)
    try:
        result = service.import_draft(
            actor=account.account,
            template_key=body.template_key,
            sheet_name=body.sheet_name,
            header_row=body.header_row,
            source_file_hash=body.source_file_hash,
            columns=_columns_from_request(body.columns),
            source_columns=body.source_columns,
            required_columns=body.required_columns,
        )
    except MappingValidationError as exc:
        raise _mapping_error_to_http(exc) from exc
    return MappingImportResult(
        version=_version_out(result.version) if result.version else None,
        warnings=list(result.warnings),
    )


@router.post("/{version_id}/publish", response_model=MappingVersionOut)
def publish_mapping(
    request: Request,
    version_id: int,
    account: Account = Depends(require_manager),
) -> MappingVersionOut:
    """发布 draft → published（规则 2：同 key 已有 published 被拒；
    记录 published_at）。"""
    check_csrf(request)
    try:
        record = _service(request).publish(actor=account.account, version_id=version_id)
    except MappingError as exc:
        raise _mapping_error_to_http(exc) from exc
    return _version_out(record)


@router.post("/{version_id}/retire", response_model=MappingVersionOut)
def retire_mapping(
    request: Request,
    version_id: int,
    account: Account = Depends(require_manager),
) -> MappingVersionOut:
    """退役 published → retired（或废弃 draft → retired；终态不可再迁移）。"""
    check_csrf(request)
    try:
        record = _service(request).retire(actor=account.account, version_id=version_id)
    except MappingError as exc:
        raise _mapping_error_to_http(exc) from exc
    return _version_out(record)
