"""附件上传端点 4xx 响应体契约测试（2026-09-11 错误码收口批次）。

覆盖：POST /api/v1/chat/attachments 校验拒绝路径必须返回稳定
``error_code=VALIDATION_ERROR`` 且 message 透出后端的具体中文原因——
前端 ErrorNotice 有意不登记 VALIDATION_ERROR 的 HINT，走
``HINTS[code] ?? error.message`` 回退，无码时会被 UNKNOWN_ERROR 的
固定兜底文案盖掉（本批次修复的缺陷）。

纯校验路径：在 validate_upload 抛错处直接返回 400，不触碰
AttachmentStore/审计，故不依赖 Postgres（SQLite client 夹具亦足够）。
"""

from __future__ import annotations

import pytest

from administrative_agent.config import settings

from tests.api.conftest import MGR, headers


@pytest.fixture()
def enabled_attachment_dir(tmp_path, monkeypatch):
    """attachment_dir 指向临时目录（非空 = 附件功能启用，越过 503 分支）。"""
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    return tmp_path


def test_upload_unsupported_extension_returns_400_with_error_code(
    client, enabled_attachment_dir
) -> None:
    """不支持的扩展名 → 400 + VALIDATION_ERROR + 具体中文原因透出。"""
    resp = client.post(
        "/api/v1/chat/attachments",
        files={"file": ("reconcile.csv", b"a,b\n1,2\n", "text/csv")},
        data={"thread_id": "thread-att-1"},
        cookies=MGR,
        headers=headers(),
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    # 后端具体原因必须透出（前端据此展示，而非被兜底文案盖掉）
    assert "不支持的扩展名" in body["message"]
    assert ".csv" in body["message"]


def test_upload_disallowed_content_type_returns_400_with_error_code(
    client, enabled_attachment_dir
) -> None:
    """扩展名合规但 content_type 不在白名单 → 同样 400 + VALIDATION_ERROR。"""
    resp = client.post(
        "/api/v1/chat/attachments",
        files={"file": ("reconcile.xlsx", b"PK\x03\x04stub", "application/zip")},
        data={"thread_id": "thread-att-2"},
        cookies=MGR,
        headers=headers(),
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert "content_type" in body["message"]
