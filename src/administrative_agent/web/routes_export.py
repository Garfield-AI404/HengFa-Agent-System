"""对话导出 API（规格 2.6 对话导出）。

- GET /api/v1/chat/export/{file_id}  下载导出 Excel 文件（manager）

流程（fail closed）：file_id 不匹配 32 位 hex → 404 EXPORT_FILE_NOT_FOUND
→ export_dir 未配置 → 503 EXPORT_NOT_CONFIGURED → {file_id}.meta.json
缺失/损坏 → 404 EXPORT_FILE_NOT_FOUND → 过期（expires_at <= now）→ 404
EXPORT_FILE_EXPIRED（顺带 best effort 清理过期 meta + xlsx，TTL 纪律）→
xlsx 缺失 → 404 EXPORT_FILE_NOT_FOUND → FileResponse（文件名取
meta.filename，中文经 RFC 5987 filename* 传输；xlsx 内容本身在导出时
已落盘，下载是只读不落库）。

下载为只读操作不记审计——导出动作本身已记 EVENT_EXPORT（ExcelExporter
第 8 步）；过期文件由启动时 + 每次导出时的 cleanup_expired_exports 收敛，
端点内只处理"用户此刻点开已过期文件"这一即时路径。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.config import settings
from administrative_agent.web.schemas import ErrorCode

logger: structlog.stdlib.BoundLogger = structlog.get_logger("web.routes_export")

router = APIRouter(prefix="/api/v1/chat/export", tags=["chat-export"])

#: file_id = uuid4().hex 32 位（ExcelExporter 生成；不匹配 → 404）
_FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _remove_best_effort(path: str) -> None:
    """清理过期导出文件（失败只记日志——兜底动作不掩盖原始异常）。"""
    try:
        os.remove(path)
    except OSError:
        logger.warning("export_file_cleanup_failed", path=path, exc_info=True)


def _expired(meta: dict[str, Any]) -> bool:
    """meta 是否过期（expires_at <= now；naive datetime 补时区后比较）。"""
    raw = meta.get("expires_at")
    if not isinstance(raw, str):
        return False
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


@router.get("/{file_id}")
async def download_export_file(
    file_id: str,
    account: Account = Depends(require_manager),
) -> FileResponse:
    """下载导出 Excel 文件（manager；导出文件含 ERP 业务数据，10.5 门禁）。"""
    if not _FILE_ID_PATTERN.match(file_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.EXPORT_FILE_NOT_FOUND.value,
                "message": "导出文件不存在。",
            },
        )
    export_dir = settings.export_dir
    if not export_dir:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": ErrorCode.EXPORT_NOT_CONFIGURED.value,
                "message": "导出功能未启用（导出目录未配置），无法下载导出文件。",
            },
        )
    meta_path = os.path.join(export_dir, f"{file_id}.meta.json")
    if not os.path.exists(meta_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.EXPORT_FILE_NOT_FOUND.value,
                "message": "导出文件不存在。",
            },
        )
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta: dict[str, Any] = json.load(f)
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.EXPORT_FILE_NOT_FOUND.value,
                "message": "导出文件不存在。",
            },
        ) from None
    if _expired(meta):
        # 用户此刻点开已过期文件：顺带清理（TTL 纪律），404 提示重新导出
        _remove_best_effort(meta_path)
        _remove_best_effort(os.path.join(export_dir, f"{file_id}.xlsx"))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.EXPORT_FILE_EXPIRED.value,
                "message": "导出文件已过期（保留 1 小时），请重新导出。",
            },
        )
    path = os.path.join(export_dir, f"{file_id}.xlsx")
    if not os.path.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.EXPORT_FILE_NOT_FOUND.value,
                "message": "导出文件不存在。",
            },
        )
    filename = str(meta.get("filename") or f"{file_id}.xlsx")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
