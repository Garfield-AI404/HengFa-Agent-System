"""对话附件上传 API（规格 2.6/6.3 附件处理）。

- POST /api/v1/chat/attachments  上传对账 Excel 附件（multipart 表单）

流程：读 bytes → validate_upload（扩展名/content_type/大小白名单，fail
closed）→ sha256 → 落盘 attachment_dir → AttachmentStore.create 登记元数据
（内容不落库，6.5/11.2 规则 4）→ cleanup_expired_files 顺带清理过期
附件（元数据行 + storage_path 磁盘文件，TTL 机制）→ 审计
EVENT_ATTACHMENT（11.1；source=chat）。

角色与安全：
- 端点 require_manager（10.5）+ CSRF 校验（D4 check_csrf，写 API）；
- attachment_dir 未配置（空串）→ 503 fail closed（config.py：空串 = 不启用
  附件功能；批次 2 工具侧 ``_read_attachment_bytes`` 以 attachment_dir 为
  根拼绝对路径——路由回退 tempdir 会造成「上传成功但工具永远读不到」的
  半成品，故上传侧直接拒绝而非降级）；
- 审计失败（AuditFailureError）→ 清理已落盘文件后 500，绝不假报成功
  （10.4）；元数据行由 TTL 清理兜底（本域无单条删除 API）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from administrative_agent.audit.service import (
    EVENT_ATTACHMENT,
    AuditFailureError,
    AuditService,
)
from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.config import settings
from administrative_agent.domains.attachments import (
    AttachmentRecord,
    AttachmentStore,
    AttachmentValidationError,
    cleanup_expired_files,
    validate_upload,
)
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
)
from administrative_agent.web.routes_auth import check_csrf
from administrative_agent.web.schemas import ErrorCode

logger: structlog.stdlib.BoundLogger = structlog.get_logger("web.routes_attachments")

router = APIRouter(prefix="/api/v1/chat", tags=["chat-attachments"])

#: 原文件名长度上限（chat_attachments.filename String(256)；截断不拒绝）
_FILENAME_MAX = 256


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），默认 SessionLocal。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _sanitize_filename(name: str) -> str:
    """防路径穿越：只取 basename（兼容 / 与 \\），非法字符替换为 _。

    返回 ≤128 字符（storage_path 列 String(512)，加 att-<uuid16>- 前缀
    后仍余量充足）；空结果回退 "file"。
    """
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^\w.\-]", "_", base)
    base = base.strip("._ ")
    return (base or "file")[:128]


def _attachment_expired(record: Any) -> bool:
    """附件是否已过期（expires_at <= now；SQLite 测试库回读 naive datetime
    时补时区后比较——与 checkpoint.py _ensure_aware 同口径）。"""
    expires_at = getattr(record, "expires_at", None)
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _remove_best_effort(path: str) -> None:
    """清理已落盘文件（失败只记日志——兜底动作不掩盖原始异常）。"""
    try:
        os.remove(path)
    except OSError:
        logger.warning("attachment_file_cleanup_failed", path=path, exc_info=True)


@router.post("/attachments")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    thread_id: str = Form(min_length=1, max_length=128),
    account: Account = Depends(require_manager),
) -> dict[str, Any]:
    """上传对账 Excel 附件（写 API：manager + CSRF；multipart 表单）。

    失败语义（fail closed；各分支 error_code 见下）：
    - attachment_dir 未配置 → 503 SERVICE_UNAVAILABLE（附件功能未启用）；
    - 扩展名/content_type/大小任一不满足白名单 → 400 VALIDATION_ERROR，
      message 为具体中文拒绝原因（前端 ErrorNotice 有意不登记
      VALIDATION_ERROR 的 HINT，走 ``HINTS[code] ?? error.message``
      回退，故后端原因会被如实展示，不再被 UNKNOWN_ERROR 兜底文案盖掉）；
    - 落盘/元数据登记失败 → 500（通用失败码，不返回成功、不泄露内部细节）；
    - 审计失败 → 清理文件后 500（10.4：审计失败不能假报成功）。

    成功返回：{attachment_id, filename, content_type, size_bytes, sha256,
    expires_at}——前端据此展示上传结果，消息提交时回传 attachment_ids。
    """
    check_csrf(request)
    storage_dir = settings.attachment_dir
    if not storage_dir:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": ErrorCode.SERVICE_UNAVAILABLE.value,
                "message": "附件功能未启用（附件目录未配置），无法上传对账文件。",
            },
        )

    data = await file.read()
    try:
        validate_upload(
            filename=file.filename or "",
            content_type=file.content_type or "",
            size_bytes=len(data),
        )
    except AttachmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.VALIDATION_ERROR.value,
                "message": str(exc),
            },
        ) from None

    sha256 = hashlib.sha256(data).hexdigest()
    storage_name = f"att-{uuid.uuid4().hex[:16]}-{_sanitize_filename(file.filename or '')}"
    path = os.path.join(storage_dir, storage_name)
    try:
        # 目录可能未创建（.env 只配置路径不建目录），首次写入前自愈创建
        os.makedirs(storage_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as exc:
        logger.warning("attachment_write_failed", path=path, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "附件保存失败，请稍后重试。"},
        ) from exc

    store = AttachmentStore(_session_factory(request))
    try:
        created = store.create(
            AttachmentRecord(
                thread_id=thread_id,
                filename=(file.filename or "")[:_FILENAME_MAX],
                content_type=file.content_type or "",
                sha256=sha256,
                size_bytes=len(data),
                storage_path=storage_name,
            )
        )
    except Exception as exc:
        # 登记失败：清理已落盘文件后 500（fail closed，不留孤儿文件）
        _remove_best_effort(path)
        logger.warning("attachment_create_failed", path=path, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "附件保存失败，请稍后重试。"},
        ) from exc

    # 每次上传顺带清理过期附件（TTL 机制；元数据行 + storage_path 磁盘
    # 文件一并删——清理失败不阻断本次上传，下次上传再清）
    cleanup_expired_files(store, settings.attachment_dir)

    # 审计（11.1 EVENT_ATTACHMENT；source=chat，trace_id 由 thread_id 派生；
    # 失败 → 清理文件 + 500，绝不假报成功——元数据行由 TTL 清理兜底）
    try:
        AuditService(
            SqlAlchemyAuditRepository(_session_factory(request)),
            SqlAlchemyAttemptRepository(_session_factory(request)),
        ).record_event(
            event_type=EVENT_ATTACHMENT,
            trace_id=f"thread:{thread_id}",
            actor_account=account.account,
            actor_role=account.role.value,
            target_type="attachment",
            target_id=created.attachment_id,
            request_summary=f"上传附件 {created.filename}",
            structured_payload={
                "filename": created.filename,
                "content_type": created.content_type,
                "size_bytes": created.size_bytes,
                "sha256": created.sha256,
                "thread_id": thread_id,
            },
            source="chat",
        )
    except AuditFailureError as exc:
        _remove_best_effort(path)
        logger.warning(
            "attachment_audit_failed",
            attachment_id=created.attachment_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "附件上传失败（审计未通过），请稍后重试。"},
        ) from exc

    return {
        "attachment_id": created.attachment_id,
        "filename": created.filename,
        "content_type": created.content_type,
        "size_bytes": created.size_bytes,
        "sha256": created.sha256,
        "expires_at": created.expires_at,
    }
