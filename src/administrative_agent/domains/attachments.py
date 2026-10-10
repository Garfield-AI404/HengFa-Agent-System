"""对话附件域（规格 2.6 domains/attachments.py）。

对话上传 Excel（对账文件）的临时附件登记：
- 元数据落 chat_attachments（sha256/路径/过期时间），内容落
  attachment_dir 临时目录——6.5/11.2 规则 4：不保存完整原始文件；
- 校验 fail closed：扩展名/content_type/大小任一不满足
  → AttachmentValidationError（不猜测、不静默放行）；
- 过期清理：attachment_ttl_seconds 后由 delete_expired 删除
  （启动时 + 每次上传时调用；路由层负责调 storage_path 对应文件删除）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeAlias

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from administrative_agent.config import settings
from administrative_agent.persistence.models import ChatAttachment

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.attachments")

#: 会话工厂（sessionmaker 或函数）；与 persistence/repositories.py 同款
SessionFactory: TypeAlias = Callable[[], Session]

#: 扩展名白名单（6.3 Excel 对账文件；fail closed：白名单之外一律拒绝）
#: 仅 .xlsx：下游 attachment_preview/reconcile_analyze 统一走 openpyxl，
#: openpyxl 无法读 .xls/.csv（抛 "File is not a zip file"）——收窄为
#: 前后端一致的可解析集，避免「上传成功但工具层才报不可读」的割裂文案。
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".xlsx"})

#: content_type 白名单（Excel/CSV 常见声明；浏览器可能以 text/plain
#: 上报 CSV——白名单包含以兼容，之外一律拒绝）
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "text/csv",
        "text/plain",
    }
)


# ---------------------------------------------------------------------------
# 领域错误（fail closed：拒绝时给出中文原因）
# ---------------------------------------------------------------------------


class AttachmentError(ValueError):
    """附件领域错误基类。"""


class AttachmentValidationError(AttachmentError):
    """上传校验失败（扩展名/content_type/大小不满足白名单或上限）。"""


# ---------------------------------------------------------------------------
# 领域记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentRecord:
    """chat_attachments 行记录（attachment_id/expires_at 由仓储生成）。

    - storage_path：attachment_dir 下的相对路径（路由层拼绝对路径读写
      文件内容；本域不落完整文件内容）；
    - sha256：文件指纹（去重与审计定位）。
    """

    attachment_id: str = ""
    thread_id: str = ""
    filename: str = ""
    content_type: str = ""
    sha256: str = ""
    size_bytes: int = 0
    storage_path: str = ""
    created_at: datetime | None = None
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "thread_id": self.thread_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "storage_path": self.storage_path,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _to_record(row: ChatAttachment) -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id=row.attachment_id,
        thread_id=row.thread_id,
        filename=row.filename,
        content_type=row.content_type,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        storage_path=row.storage_path,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


# ---------------------------------------------------------------------------
# 上传校验（fail closed）
# ---------------------------------------------------------------------------


def validate_upload(*, filename: str, content_type: str, size_bytes: int) -> None:
    """上传前校验：扩展名/content_type/大小任一不满足即拒绝。

    - 扩展名白名单 {.xlsx}（按小写匹配）；
    - content_type 白名单（Excel/CSV 常见类型）；
    - size_bytes ≤ settings.attachment_max_bytes（负值视为非法）。
    任一失败抛 AttachmentValidationError（中文原因，不猜测不静默）。
    """
    name = filename or ""
    ext = os.path.splitext(name)[1].lower()
    if not name or not ext:
        raise AttachmentValidationError(
            f"文件名非法（需带扩展名）：{name!r}（允许 {sorted(ALLOWED_EXTENSIONS)}）"
        )
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentValidationError(
            f"不支持的扩展名 {ext!r}（允许 {sorted(ALLOWED_EXTENSIONS)}）"
        )
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise AttachmentValidationError(
            f"不支持的 content_type {content_type!r}（允许 {sorted(ALLOWED_CONTENT_TYPES)}）"
        )
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise AttachmentValidationError(f"附件大小非法：{size_bytes!r}")
    if size_bytes > settings.attachment_max_bytes:
        raise AttachmentValidationError(
            f"附件大小 {size_bytes} 字节超过上限 "
            f"{settings.attachment_max_bytes} 字节（10MB）"
        )


# ---------------------------------------------------------------------------
# 仓储（SQLAlchemy；chat_attachments）
# ---------------------------------------------------------------------------


class AttachmentStore:
    """chat_attachments 持久化仓储（2026-08-31 对话对账批次 1）。

    create 生成 attachment_id（att-<uuid16>）与 expires_at
    （now + settings.attachment_ttl_seconds）；delete_expired 供启动时与
    每次上传时清理过期附件（路由层同步删除 storage_path 文件）。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def create(self, record: AttachmentRecord) -> AttachmentRecord:
        """登记附件元数据（attachment_id/expires_at 由本方法生成）。"""
        now = datetime.now(timezone.utc)
        rec = replace(
            record,
            attachment_id=record.attachment_id
            or f"att-{uuid.uuid4().hex[:16]}",
            expires_at=record.expires_at
            or now + timedelta(seconds=settings.attachment_ttl_seconds),
        )
        row = ChatAttachment(
            attachment_id=rec.attachment_id,
            thread_id=rec.thread_id,
            filename=rec.filename,
            content_type=rec.content_type,
            sha256=rec.sha256,
            size_bytes=rec.size_bytes,
            storage_path=rec.storage_path,
            expires_at=rec.expires_at,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return _to_record(row)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._factory() as session:
            row = session.get(ChatAttachment, attachment_id)
            return _to_record(row) if row is not None else None

    def delete_expired(self) -> list[str]:
        """删除已过期附件（expires_at <= now，含到期时刻），返回被删行的
        storage_path 列表（调用方据此同步删除磁盘文件，不留孤儿文件）。

        到期边界用 <=：TTL 极短（或清理与创建同一时钟刻度）时，
        expires_at == now 的记录同样视为过期，避免漏清。
        实现：先 SELECT 过期行的 storage_path 再 DELETE（同会话，
        RETURNING 依赖 DB 方言，SELECT 先行可移植且与代码库风格一致）；
        无过期行时返回空列表，不产生 DELETE。
        """
        now = datetime.now(timezone.utc)
        with self._factory() as session:
            rows = (
                session.execute(
                    select(ChatAttachment.storage_path).where(
                        ChatAttachment.expires_at <= now
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                session.execute(
                    delete(ChatAttachment).where(ChatAttachment.expires_at <= now)
                )
                session.commit()
        return [str(path) for path in rows]


def cleanup_expired_files(store: AttachmentStore, storage_dir: str) -> list[str]:
    """清理过期附件：删元数据行 + 同步删除 storage_path 对应磁盘文件。

    - 全程 best effort：元数据清理失败只记 warning 不阻断调用方；
    - storage_dir 为空（附件功能未配置）时只删元数据，跳过文件删除；
    - 返回被清理的 storage_path 列表（供日志/审计定位）。
    """
    try:
        paths = store.delete_expired()
    except Exception:
        logger.warning("attachment_delete_expired_failed", exc_info=True)
        return []
    if not storage_dir:
        return paths
    for path in paths:
        try:
            os.remove(os.path.join(storage_dir, path))
        except OSError:
            logger.warning("attachment_file_cleanup_failed", path=path, exc_info=True)
    return paths
