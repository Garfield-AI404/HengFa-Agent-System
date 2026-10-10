"""D18 Excel 映射服务单测（规格 6.3 处理规则 7 条；模块树 L262）。

覆盖：导入校验（规则 3 缺必填列停止 / 规则 4 未知列告警忽略 / 转换规则
合法性）、版本号递增（规则 6 不覆盖历史）、发布（规则 2 唯一当前发布 /
published_at）、退役状态机、published 读取（规则 1：多 draft 同 key 取
唯一 published / 无 published 报错）、历史版本保留。

InMemoryMappingStore 为测试夹具（同 tests/tasks、tests/audit 模式）：
SQLAlchemy 版（SqlAlchemyMappingStore）由 tests/api 与演示脚本覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from administrative_agent.files.mapping_service import (
    ExcelMappingService,
    MappingColumnRecord,
    MappingNotPublishedError,
    MappingPublishedExistsError,
    MappingStateError,
    MappingValidationError,
    MappingVersionRecord,
    validate_column_definitions,
)


class InMemoryMappingStore:
    """内存映射存储（测试夹具，D5 规则：不代表真实 DB 行为）。"""

    def __init__(self) -> None:
        self.versions: dict[int, MappingVersionRecord] = {}
        self.columns: dict[int, tuple[MappingColumnRecord, ...]] = {}
        self._next_id = 1
        self._clock = datetime(2026, 8, 23, 8, 0, 0, tzinfo=timezone.utc)

    def next_version_number(self, template_key: str) -> int:
        top = max(
            (v.version for v in self.versions.values() if v.template_key == template_key),
            default=0,
        )
        return top + 1

    def create_version(self, *, template_key, version, sheet_name, header_row,
                       status, source_file_hash, created_by, created_at) -> int:
        vid = self._next_id
        self._next_id += 1
        self.versions[vid] = MappingVersionRecord(
            version_id=vid, template_key=template_key, version=version,
            sheet_name=sheet_name, header_row=header_row, status=status,
            source_file_hash=source_file_hash, created_by=created_by,
            created_at=created_at, published_at=None,
        )
        self.columns[vid] = ()
        return vid

    def create_columns(self, version_id, columns) -> None:
        self.columns[version_id] = tuple(columns)

    def get(self, version_id: int) -> MappingVersionRecord | None:
        record = self.versions.get(version_id)
        if record is None:
            return None
        return MappingVersionRecord(**{**record.__dict__, "columns": self.columns[version_id]})

    def list_versions(self, *, template_key, status, limit, offset):
        rows = [
            self.get(v) for v in sorted(self.versions)
            if (template_key is None or self.versions[v].template_key == template_key)
            and (status is None or self.versions[v].status == status)
        ]
        rows.sort(key=lambda r: (r.template_key, -r.version))
        return rows[offset:offset + limit], len(rows)

    def published_by_key(self, template_key: str) -> MappingVersionRecord | None:
        for record in self.versions.values():
            if record.template_key == template_key and record.status == "published":
                return self.get(record.version_id)
        return None

    def set_status(self, version_id, status, published_at=None) -> MappingVersionRecord:
        old = self.versions[version_id]
        # 与 SqlAlchemyMappingStore 一致：published_at 只记录发布时刻，
        # 退役/废弃不清除（历史保留，规则 6）
        keep = old.published_at if published_at is None else published_at
        self.versions[version_id] = MappingVersionRecord(
            **{**old.__dict__, "status": status, "published_at": keep}
        )
        return self.get(version_id)


@pytest.fixture
def service() -> ExcelMappingService:
    return ExcelMappingService(InMemoryMappingStore())


def _columns():
    return [
        {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
         "is_required": True},
        {"excel_column": "订单号", "target_field": "order_no", "data_type": "string",
         "is_required": True},
        {"excel_column": "金额", "target_field": "total_amount", "data_type": "decimal"},
        {"excel_column": "状态", "target_field": "status", "data_type": "enum",
         "enum_mapping": {"完成": "completed", "进行中": "in_progress"}},
    ]


# ---- 导入与版本号递增（规则 6 不覆盖历史） ----


def test_import_draft_creates_version_one(service):
    result = service.import_draft(
        actor="manager01", template_key="orders", sheet_name="Sheet1",
        header_row=2, source_file_hash="sha256-aaaa", columns=_columns(),
    )
    assert result.errors == ()
    assert result.warnings == ()
    v = result.version
    assert v is not None and v.version == 1
    assert v.status == "draft"
    assert v.created_by == "manager01"
    assert v.header_row == 2
    assert len(v.columns) == 4
    assert v.columns[0].excel_column == "日期" and v.columns[0].is_required


def test_import_version_increments_not_overwrites(service):
    """规则 6：映射变更发布新版本，不覆盖历史版本。"""
    v1 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h1", columns=_columns()).version
    v2 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h2", columns=_columns()).version
    assert v1.version == 1 and v2.version == 2
    assert v1.version_id != v2.version_id
    # 历史版本保留（版本 1 仍可查询，列快照随版本）
    got1 = service.get_version(v1.version_id)
    assert got1.status == "draft" and len(got1.columns) == 4


def test_import_different_template_keys_independent(service):
    a = service.import_draft(actor="mgr", template_key="orders-a", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    b = service.import_draft(actor="mgr", template_key="orders-b", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    assert a.version == 1 and b.version == 1


# ---- 规则 3：缺少必填列时停止 ----


def test_import_missing_required_column_stops(service):
    """required_columns 中未在 columns 覆盖的列 → 停止（不产生版本）。"""
    with pytest.raises(MappingValidationError) as exc:
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h", columns=_columns(),
            required_columns=["日期", "订单号", "客户编号"],  # 客户编号未定义
        )
    assert any("缺少必填列" in e for e in exc.value.details["errors"])
    assert any("客户编号" in e for e in exc.value.details["errors"])
    assert service.list_versions()[1] == 0  # 停止 = 未产生版本


def test_import_missing_required_column_in_source_stops(service):
    """提供 source_columns 时，必填列不在 Excel 列清单 → 停止。"""
    with pytest.raises(MappingValidationError) as exc:
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h", columns=_columns(),
            source_columns=["日期", "金额", "状态"],  # 缺必填列"订单号"
        )
    assert any("订单号" in e for e in exc.value.details["errors"])


# ---- 规则 4：未知列告警并忽略，不自行建立映射 ----


def test_import_unknown_columns_warned_and_ignored(service):
    result = service.import_draft(
        actor="mgr", template_key="orders", sheet_name="S", header_row=1,
        source_file_hash="h", columns=_columns(),
        source_columns=["日期", "订单号", "金额", "状态", "备注说明", "Excel 隐藏列"],
    )
    assert result.version is not None  # 未知列不阻断
    assert any("备注说明" in w for w in result.warnings)
    assert any("Excel 隐藏列" in w for w in result.warnings)
    assert all("不自行建立映射" in w for w in result.warnings)
    # 未建立额外映射：列数仍为 4
    assert len(result.version.columns) == 4


# ---- 转换规则合法性校验 ----


def test_validate_column_definitions_data_type_unknown(service):
    with pytest.raises(MappingValidationError) as exc:
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h",
            columns=[{"excel_column": "A", "target_field": "a",
                      "data_type": "datetime2"}],
        )
    assert any("data_type" in e for e in exc.value.details["errors"])


def test_validate_column_definitions_enum_requires_mapping(service):
    with pytest.raises(MappingValidationError) as exc:
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h",
            columns=[{"excel_column": "状态", "target_field": "status",
                      "data_type": "enum", "enum_mapping": None}],
        )
    assert any("enum_mapping" in e for e in exc.value.details["errors"])


def test_validate_column_definitions_duplicate_columns(service):
    with pytest.raises(MappingValidationError):
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h",
            columns=[
                {"excel_column": "A", "target_field": "a", "data_type": "string"},
                {"excel_column": "A", "target_field": "b", "data_type": "string"},
            ],
        )


def test_validate_column_definitions_non_enum_mapping_warned(service):
    errors, warnings = validate_column_definitions(
        [{"excel_column": "A", "target_field": "a", "data_type": "string",
          "enum_mapping": {"x": "y"}}]
    )
    assert errors == []
    assert any("已忽略" in w for w in warnings)


def test_import_empty_columns_rejected(service):
    with pytest.raises(MappingValidationError):
        service.import_draft(
            actor="mgr", template_key="orders", sheet_name="S", header_row=1,
            source_file_hash="h", columns=[],
        )


# ---- 规则 2：同一 template_key 同一时刻只有一个发布版本 ----


def test_publish_sets_published_at(service):
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    published = service.publish(actor="mgr", version_id=v.version_id)
    assert published.status == "published"
    assert published.published_at is not None
    assert service.get_published("orders").version_id == v.version_id


def test_publish_duplicate_same_key_rejected(service):
    """规则 2：v1 已发布后，v2 发布被拒（一次只有一个发布版本）。"""
    v1 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h1", columns=_columns()).version
    service.publish(actor="mgr", version_id=v1.version_id)
    v2 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h2", columns=_columns()).version
    with pytest.raises(MappingPublishedExistsError) as exc:
        service.publish(actor="mgr", version_id=v2.version_id)
    assert exc.value.details["existing_version_id"] == v1.version_id
    # 被拒后仍是 draft，唯一 published 仍是 v1
    assert service.get_version(v2.version_id).status == "draft"
    assert service.get_published("orders").version_id == v1.version_id


def test_publish_after_retire_allowed(service):
    """先退役旧版 → 新版可发布（一次只有一个发布版本的完整切换流）。"""
    v1 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h1", columns=_columns()).version
    service.publish(actor="mgr", version_id=v1.version_id)
    service.retire(actor="mgr", version_id=v1.version_id)
    v2 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h2", columns=_columns()).version
    published = service.publish(actor="mgr", version_id=v2.version_id)
    assert published.version == 2 and published.status == "published"
    assert service.get_published("orders").version_id == v2.version_id


def test_publish_non_draft_rejected(service):
    """重复发布同版本 / retired 再发布 → MappingStateError。"""
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    service.publish(actor="mgr", version_id=v.version_id)
    with pytest.raises(MappingStateError):
        service.publish(actor="mgr", version_id=v.version_id)  # published → 重复发布
    service.retire(actor="mgr", version_id=v.version_id)
    with pytest.raises(MappingStateError):
        service.publish(actor="mgr", version_id=v.version_id)  # retired 不可恢复


# ---- 退役状态机 ----


def test_retire_published_and_draft(service):
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    service.publish(actor="mgr", version_id=v.version_id)
    retired = service.retire(actor="mgr", version_id=v.version_id)
    assert retired.status == "retired"
    # 退役后 published 读取报错（规则 1）
    with pytest.raises(MappingNotPublishedError):
        service.get_published("orders")
    # draft 直接废弃
    v2 = service.import_draft(actor="mgr", template_key="orders2", sheet_name="S",
                              header_row=1, source_file_hash="h", columns=_columns()).version
    assert service.retire(actor="mgr", version_id=v2.version_id).status == "retired"


def test_retire_keeps_published_at_history(service):
    """规则 6：退役不清除发布时刻——published_at 是历史发布记录。"""
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    published = service.publish(actor="mgr", version_id=v.version_id)
    retired = service.retire(actor="mgr", version_id=v.version_id)
    assert retired.published_at is not None
    assert retired.published_at == published.published_at


def test_retire_retired_rejected(service):
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    service.retire(actor="mgr", version_id=v.version_id)
    with pytest.raises(MappingStateError):
        service.retire(actor="mgr", version_id=v.version_id)


# ---- 规则 1：只使用 published 版本；published 读取（D19 复用接口） ----


def test_get_published_only_published(service):
    """多 draft 同 key：published 读取只取唯一 published（有则唯一）。"""
    v1 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h1", columns=_columns()).version
    service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                         header_row=1, source_file_hash="h2", columns=_columns())
    with pytest.raises(MappingNotPublishedError):
        service.get_published("orders")  # 只有 draft → 无 published 报错
    service.publish(actor="mgr", version_id=v1.version_id)
    published = service.get_published("orders")
    assert published.version_id == v1.version_id
    assert published.status == "published"
    assert len(published.columns) == 4  # 列级快照随 published 返回（6.4 步骤 2）


def test_get_published_unknown_key_raises(service):
    with pytest.raises(MappingNotPublishedError):
        service.get_published("no-such-template")


def test_retired_not_readable_as_published(service):
    """退役后 published 读取报错（规则 1：只使用 published 版本）。"""
    v = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                             header_row=1, source_file_hash="h", columns=_columns()).version
    service.publish(actor="mgr", version_id=v.version_id)
    service.retire(actor="mgr", version_id=v.version_id)
    with pytest.raises(MappingNotPublishedError):
        service.get_published("orders")


# ---- 列表与详情（历史保留，规则 6） ----


def test_list_versions_keeps_history(service):
    v1 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h1", columns=_columns()).version
    v2 = service.import_draft(actor="mgr", template_key="orders", sheet_name="S",
                              header_row=1, source_file_hash="h2", columns=_columns()).version
    service.publish(actor="mgr", version_id=v1.version_id)
    service.retire(actor="mgr", version_id=v1.version_id)
    service.publish(actor="mgr", version_id=v2.version_id)
    items, total = service.list_versions(template_key="orders")
    assert total == 2
    assert [i.status for i in items] == ["published", "retired"]  # 按 version 降序
    assert service.list_versions(template_key="orders", status="retired")[1] == 1


def test_list_versions_pagination(service):
    for i in range(3):
        service.import_draft(actor="mgr", template_key=f"t{i}", sheet_name="S",
                             header_row=1, source_file_hash=f"h{i}", columns=_columns())
    items, total = service.list_versions(limit=2, offset=0)
    assert len(items) == 2 and total == 3
    items2, _ = service.list_versions(limit=2, offset=2)
    assert len(items2) == 1
