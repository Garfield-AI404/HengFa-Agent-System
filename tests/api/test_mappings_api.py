"""D18 映射管理 API 测试（规格 2.7.3 API 契约 + 6.3 处理规则 + RV-27）。

SQLite 内存库 + app.state.session_factory 注入（routes_mappings._session_factory
优先读 app.state；SQLite 上 JSONB 注册为 JSON 编译器、部分唯一索引原生支持，
D3 表结构实测 create_all 通过）。会话用 D4 测试模式直接构造 Cookie。

覆盖（任务书测试要求）：import 校验（缺必填列停止 / 未知列告警忽略 /
枚举与转换规则合法性）、publish 重复发布同 key 被拒 + 历史版本保留 +
published_at、退役状态转换、GET 版本列表/详情含列级快照、CSRF 校验、
manager/developer 角色拒绝、统一错误响应（error_code/message/details/trace_id）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent.auth.models import Account, Role
from administrative_agent.auth.service import SESSION_COOKIE_NAME, create_session
from administrative_agent.main import app
from administrative_agent.persistence.models import (
    Base,
    ExcelMappingColumn,
    ExcelMappingVersion,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    """SQLite 测试兼容：JSONB → JSON（仅测试环境；生产走 PostgreSQL 16）。"""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_biginteger_sqlite(type_, compiler, **kw):  # noqa: ANN001
    """SQLite 测试兼容：PG BigInteger 主键（D3 Identity）渲染为 "INTEGER"。

    SQLite 的隐式/显式自增只接受类型名恰为 INTEGER（官方文档），
    且列级 PRIMARY KEY AUTOINCREMENT 需表级 sqlite_autoincrement 选项
    （fixture 中设置）。生产走 PostgreSQL 16，不受影响。
    """
    return "INTEGER"


VALID_ORIGIN = "http://127.0.0.1:5173"
FOREIGN_ORIGIN = "https://evil.example.com"


@pytest.fixture()
def client() -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 内存库跨 Session 共享同一连接
    )
    for table in (
        ExcelMappingVersion.__table__,
        ExcelMappingColumn.__table__,
    ):
        table.dialect_options["sqlite"]["autoincrement"] = True
    # D3 的部分唯一索引用 postgresql_where（PG 专用）；SQLite 需等价的
    # sqlite_where 才生成 CREATE UNIQUE INDEX ... WHERE status='published'
    uq_idx = next(
        i for i in ExcelMappingVersion.__table__.indexes
        if i.name == "uq_excel_mapping_versions_published"
    )
    uq_idx.dialect_options["sqlite"]["where"] = text("status = 'published'")
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    app.state.session_factory = TestingSessionLocal
    try:
        yield TestClient(app)
    finally:
        del app.state.session_factory
        Base.metadata.drop_all(engine)


def _cookies(role: Role, account: str = "manager01") -> dict:
    return {SESSION_COOKIE_NAME: create_session(Account(account=account, role=role))}


def _headers(origin: str | None = VALID_ORIGIN) -> dict:
    return {"Origin": origin} if origin else {}


def _columns() -> list[dict]:
    return [
        {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
         "is_required": True},
        {"excel_column": "订单号", "target_field": "order_no", "data_type": "string",
         "is_required": True},
        {"excel_column": "金额", "target_field": "total_amount", "data_type": "decimal"},
        {"excel_column": "状态", "target_field": "status", "data_type": "enum",
         "enum_mapping": {"完成": "completed", "进行中": "in_progress"}},
    ]


def _import_body(**overrides) -> dict:
    body = {
        "template_key": "orders",
        "sheet_name": "Sheet1",
        "header_row": 2,
        "source_file_hash": "sha256-aaaa",
        "columns": _columns(),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 角色与认证（2.7.2 /mappings manager 允许；2.7.3 全部 manager）
# ---------------------------------------------------------------------------


def test_unauthenticated_401(client) -> None:
    resp = client.get("/api/v1/mappings")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


def test_developer_forbidden_on_get(client) -> None:
    resp = client.get("/api/v1/mappings", cookies=_cookies(Role.DEVELOPER, "dev01"))
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "FORBIDDEN"
    assert "需要 manager 权限" in body["message"]  # 10.5 只说明所需权限


def test_developer_forbidden_on_import(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(),
        cookies=_cookies(Role.DEVELOPER, "dev01"),
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "FORBIDDEN"


def test_manager_allowed_on_list(client) -> None:
    resp = client.get(
        "/api/v1/mappings", cookies=_cookies(Role.MANAGER), headers=_headers()
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


# ---------------------------------------------------------------------------
# import：规则 3/4 + 转换规则合法性
# ---------------------------------------------------------------------------


def test_import_valid_draft_creates_version(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    v = body["version"]
    assert v["version"] == 1 and v["status"] == "draft"
    assert v["template_key"] == "orders" and v["header_row"] == 2
    assert v["created_by"] == "manager01"
    assert v["published_at"] is None
    assert len(v["columns"]) == 4  # 列级快照
    assert v["columns"][3]["enum_mapping"] == {"完成": "completed", "进行中": "in_progress"}
    assert body["warnings"] == []
    assert "errors" not in body or body["errors"] == []


def test_import_unknown_columns_warned_and_ignored(client) -> None:
    """规则 4：未知列告警并忽略，不自行建立映射。"""
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(
            source_columns=["日期", "订单号", "金额", "状态", "备注说明"]
        ),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200  # 未知列不阻断
    body = resp.json()
    assert any("备注说明" in w and "不自行建立映射" in w for w in body["warnings"])
    assert len(body["version"]["columns"]) == 4  # 未建立额外映射


def test_import_missing_required_column_stops(client) -> None:
    """规则 3：缺少必填列时停止（不产生版本）。"""
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(required_columns=["日期", "订单号", "客户编号"]),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "MAPPING_VALIDATION_FAILED"
    assert any("客户编号" in e and "缺少必填列" in e for e in body["details"]["errors"])
    assert body["trace_id"]
    # 停止 = 未产生版本
    listed = client.get(
        "/api/v1/mappings", cookies=_cookies(Role.MANAGER), headers=_headers()
    ).json()
    assert listed["total"] == 0


def test_import_missing_required_in_source_stops(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(source_columns=["日期", "金额", "状态"]),  # 缺必填列"订单号"
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "MAPPING_VALIDATION_FAILED"
    assert any("订单号" in e and "Excel 列清单" in e for e in body["details"]["errors"])


def test_import_invalid_data_type_rejected(client) -> None:
    """非法 data_type：schema 层 Literal 拦截 → 422 VALIDATION_ERROR
    （转换规则合法性校验的第一道防线；服务层兜底见单测）。"""
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(
            columns=[{"excel_column": "A", "target_field": "a", "data_type": "datetime2"}]
        ),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_import_duplicate_excel_column_rejected(client) -> None:
    """重复 excel_column 通过 schema（Literal 不查重），服务层校验 → 400。"""
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(
            columns=[
                {"excel_column": "A", "target_field": "a", "data_type": "string"},
                {"excel_column": "A", "target_field": "b", "data_type": "string"},
            ]
        ),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "MAPPING_VALIDATION_FAILED"
    assert any("excel_column 重复" in e for e in body["details"]["errors"])


def test_import_enum_without_mapping_rejected(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(
            columns=[{"excel_column": "状态", "target_field": "status",
                      "data_type": "enum", "enum_mapping": None}]
        ),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "MAPPING_VALIDATION_FAILED"
    assert any("enum_mapping" in e for e in body["details"]["errors"])


def test_import_schema_validation_uniform(client) -> None:
    """2.7.3：请求参数校验失败统一 VALIDATION_ERROR 422 + trace_id。"""
    resp = client.post(
        "/api/v1/mappings/import",
        json={"template_key": "", "sheet_name": "S", "source_file_hash": "h",
              "columns": []},
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["trace_id"]


# ---------------------------------------------------------------------------
# publish：规则 2（唯一当前发布）/ published_at
# ---------------------------------------------------------------------------


def _create_draft(client, template_key: str = "orders", version_tag: str = "h1") -> int:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(template_key=template_key, source_file_hash=version_tag),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200
    return resp.json()["version"]["version_id"]


def test_publish_sets_published_at(client) -> None:
    vid = _create_draft(client)
    resp = client.post(
        f"/api/v1/mappings/{vid}/publish",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["published_at"] is not None
    assert body["version"] == 1


def test_publish_duplicate_same_key_conflict(client) -> None:
    """规则 2：同 key 已发布后重复发布 → 409，v2 保持 draft。"""
    v1 = _create_draft(client, version_tag="h1")
    client.post(f"/api/v1/mappings/{v1}/publish", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    v2 = _create_draft(client, version_tag="h2")
    resp = client.post(
        f"/api/v1/mappings/{v2}/publish",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "MAPPING_PUBLISHED_EXISTS"
    assert body["details"]["existing_version_id"] == v1
    # 被拒后仍是 draft；唯一 published 仍是 v1
    detail = client.get(f"/api/v1/mappings/{v2}",
                        cookies=_cookies(Role.MANAGER), headers=_headers()).json()
    assert detail["status"] == "draft"
    # 历史版本保留（规则 6）：列表 2 条
    listed = client.get("/api/v1/mappings",
                        cookies=_cookies(Role.MANAGER), headers=_headers()).json()
    assert listed["total"] == 2
    assert [i["status"] for i in listed["items"]] == ["draft", "published"]


def test_publish_after_retire_allowed(client) -> None:
    """先退役旧版 → 新版可发布（一次只有一个发布版本）。"""
    v1 = _create_draft(client, version_tag="h1")
    client.post(f"/api/v1/mappings/{v1}/publish", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    client.post(f"/api/v1/mappings/{v1}/retire", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    v2 = _create_draft(client, version_tag="h2")
    resp = client.post(
        f"/api/v1/mappings/{v2}/publish",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2 and resp.json()["status"] == "published"


def test_publish_non_draft_conflict(client) -> None:
    """重复发布同版本 → 409 MAPPING_INVALID_STATE。"""
    vid = _create_draft(client)
    client.post(f"/api/v1/mappings/{vid}/publish", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    resp = client.post(
        f"/api/v1/mappings/{vid}/publish",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "MAPPING_INVALID_STATE"


def test_publish_unknown_version_404(client) -> None:
    resp = client.post(
        "/api/v1/mappings/99999/publish",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "MAPPING_NOT_FOUND"


# ---------------------------------------------------------------------------
# retire：状态转换
# ---------------------------------------------------------------------------


def test_retire_published_to_retired(client) -> None:
    vid = _create_draft(client)
    client.post(f"/api/v1/mappings/{vid}/publish", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    resp = client.post(
        f"/api/v1/mappings/{vid}/retire",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"


def test_retire_retired_conflict(client) -> None:
    """retired 是终态，不可再迁移。"""
    vid = _create_draft(client)
    client.post(f"/api/v1/mappings/{vid}/retire", cookies=_cookies(Role.MANAGER),
                headers=_headers())
    resp = client.post(
        f"/api/v1/mappings/{vid}/retire",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "MAPPING_INVALID_STATE"


# ---------------------------------------------------------------------------
# 查询：版本列表/详情（含列级快照、分页、total）
# ---------------------------------------------------------------------------


def test_get_version_detail_includes_columns(client) -> None:
    vid = _create_draft(client)
    resp = client.get(f"/api/v1/mappings/{vid}",
                      cookies=_cookies(Role.MANAGER), headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_id"] == vid
    assert len(body["columns"]) == 4
    assert body["columns"][0] == {
        "excel_column": "日期", "target_field": "in_date", "data_type": "date",
        "conversion_rule": None, "is_required": True,
        "enum_mapping": None, "default_value": None,
    }


def test_get_version_unknown_404(client) -> None:
    resp = client.get("/api/v1/mappings/99999",
                      cookies=_cookies(Role.MANAGER), headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "MAPPING_NOT_FOUND"


def test_list_pagination_filter_and_status(client) -> None:
    for i in range(3):
        _create_draft(client, template_key=f"orders-{i}", version_tag=f"h{i}")
    resp = client.get(
        "/api/v1/mappings?limit=2&offset=0",
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    body = resp.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 0

    resp2 = client.get(
        "/api/v1/mappings?template_key=orders-1",
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp2.json()["total"] == 1

    resp3 = client.get(
        "/api/v1/mappings?status=published",
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp3.json()["total"] == 0


def test_list_limit_capped(client) -> None:
    resp = client.get(
        "/api/v1/mappings?limit=999",
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp.status_code == 422  # le=200 → VALIDATION_ERROR
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# CSRF（写 API 全量校验；fail closed）
# ---------------------------------------------------------------------------


def test_import_without_origin_403(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(),
        cookies=_cookies(Role.MANAGER),
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_import_foreign_origin_403(client) -> None:
    resp = client.post(
        "/api/v1/mappings/import",
        json=_import_body(),
        cookies=_cookies(Role.MANAGER),
        headers=_headers(FOREIGN_ORIGIN),
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_publish_requires_csrf(client) -> None:
    vid = _create_draft(client)
    resp = client.post(f"/api/v1/mappings/{vid}/publish", cookies=_cookies(Role.MANAGER))
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_retire_requires_csrf(client) -> None:
    vid = _create_draft(client)
    resp = client.post(
        f"/api/v1/mappings/{vid}/retire",
        cookies=_cookies(Role.MANAGER),
        headers=_headers(FOREIGN_ORIGIN),
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"
