"""Excel 映射版本管理服务（规格 6.3 处理规则 7 条，模块树 L262）。

定位（D18）：
- 映射由人工通过管理页面导入并发布；Agent 没有新增、修改、发布或删除
  映射的工具（L798/L34/L444）——本服务只被 manager 管理 API 调用
  （web/routes_mappings.py），不创建任何 Agent 工具/意图/R 流程。
- 人工管理操作，不经过 task/轻确认/审计 attempt 体系（任务书边界：
  不为映射管理造新审计）。
- ``get_published`` 为 published 映射读取服务（规则 1：只使用 published
  版本），供 D19 R6 对账（6.4 步骤 2 按 template_key 读取）复用；本单元
  不实现匹配/差异（D19 边界）。

处理规则落地（6.3 L816-824）：
1. 只使用 published 版本 → get_published 只查 status=published；
2. 同一 template_key 同一时刻只有一个当前发布版本 → publish 时同 key
   已存在 published 版本即拒绝（MAPPING_PUBLISHED_EXISTS），表级部分
   唯一索引 uq_excel_mapping_versions_published 兜底（D3 已建成）；
3. 缺少必填列时停止 → 导入校验：required_columns（模板必含列）必须
   全部被 columns 覆盖，且（提供 source_columns 时）必填列必须存在于
   source_columns，缺失 → MappingValidationError 停止；
4. 未知列告警并忽略，不自行建立映射 → source_columns 中不在 columns
   的列进 warnings，不建立映射、不报错；
5. 日期、数字或枚举转换失败列出行号和原因 → 属 D19 Excel 行转换应用
   阶段，本单元只管理转换规则定义与合法性校验（data_type 枚举、
   enum_mapping 结构），不实现行转换执行；
6. 映射变更发布新版本，不覆盖历史版本 → 版本号按 template_key 递增，
   历史版本与列快照只读保留（models.py L322 注释）；
7. 原始映射文件不是模型记忆；数据库配置表才是执行事实源 → 数据全部
   落 excel_mapping_versions/excel_mapping_columns（D3），服务不缓存
   文件内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from administrative_agent.persistence.models import (
    ExcelMappingColumn,
    ExcelMappingVersion,
    MappingStatus,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("files.mapping_service")

#: 合法 data_type（6.3 数据类型与转换规则；规则 5 的转换执行在 D19）
DATA_TYPES = ("string", "int", "decimal", "date", "enum", "bool")


# ---------------------------------------------------------------------------
# 领域异常（API 层映射为稳定 error_code，2.7.3）
# ---------------------------------------------------------------------------


class MappingError(Exception):
    """映射领域异常基类。"""

    error_code = "MAPPING_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class MappingNotFoundError(MappingError):
    error_code = "MAPPING_NOT_FOUND"


class MappingValidationError(MappingError):
    """导入校验失败（规则 3/转换规则合法性）；停止，不产生版本。"""

    error_code = "MAPPING_VALIDATION_FAILED"


class MappingStateError(MappingError):
    """状态机非法迁移（draft→draft 重复发布、retired 再迁移等）。"""

    error_code = "MAPPING_INVALID_STATE"


class MappingPublishedExistsError(MappingError):
    """规则 2：同一 template_key 已有 published 版本，发布被拒。"""

    error_code = "MAPPING_PUBLISHED_EXISTS"


class MappingNotPublishedError(MappingError):
    """published 读取（规则 1）：该 template_key 无 published 版本。"""

    error_code = "MAPPING_PUBLISHED_NOT_FOUND"


# ---------------------------------------------------------------------------
# 领域记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingColumnRecord:
    excel_column: str
    target_field: str
    data_type: str
    conversion_rule: str | None = None
    is_required: bool = False
    enum_mapping: dict[str, str] | None = None
    default_value: str | None = None


@dataclass(frozen=True)
class MappingVersionRecord:
    """映射版本（6.3 版本字段子集 + 列级映射快照）。"""

    version_id: int
    template_key: str
    version: int
    sheet_name: str
    header_row: int
    status: str
    source_file_hash: str
    created_by: str
    created_at: datetime
    published_at: datetime | None = None
    columns: tuple[MappingColumnRecord, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    """导入校验结果（页面"预览"能力的数据支撑：映射表 + 未知列告警 +
    缺必填列清单；校验失败时 errors 非空且不产生版本）。"""

    version: MappingVersionRecord | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_column_definitions(
    columns: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """转换规则合法性校验（内容②）。

    返回 (errors, warnings)：errors 非空 → 停止导入（规则 3 语义扩展）；
    warnings 不影响导入。校验项：
    - columns 非空、excel_column/target_field 非空且各自唯一；
    - data_type ∈ {string,int,decimal,date,enum,bool}；
    - data_type=enum 时 enum_mapping 必填且值全为字符串；
    - 非 enum 类型携带 enum_mapping → 告警忽略（不参与转换）；
    - conversion_rule/default_value 为字符串或 None。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not columns:
        errors.append("至少需要一个列映射。")
        return errors, warnings
    seen_excel: set[str] = set()
    seen_target: set[str] = set()
    for i, col in enumerate(columns, start=1):
        excel = col.get("excel_column")
        target = col.get("target_field")
        dtype = col.get("data_type")
        if not excel or not isinstance(excel, str):
            errors.append(f"第 {i} 个列映射缺少 excel_column。")
        elif excel in seen_excel:
            errors.append(f"excel_column 重复：{excel!r}。")
        else:
            seen_excel.add(excel)
        if not target or not isinstance(target, str):
            errors.append(f"第 {i} 个列映射缺少 target_field。")
        elif target in seen_target:
            errors.append(f"target_field 重复：{target!r}。")
        else:
            seen_target.add(target)
        if dtype not in DATA_TYPES:
            errors.append(
                f"列 {excel or i} 的 data_type={dtype!r} 非法，"
                f"必须是 {sorted(DATA_TYPES)} 之一。"
            )
        enum_map = col.get("enum_mapping")
        if enum_map is not None:
            if not isinstance(enum_map, dict):
                errors.append(f"列 {excel or i} 的 enum_mapping 必须是对象。")
            elif not all(isinstance(k, str) and isinstance(v, str) for k, v in enum_map.items()):
                errors.append(f"列 {excel or i} 的 enum_mapping 键值必须为字符串。")
            elif dtype != "enum":
                warnings.append(
                    f"列 {excel or i} 声明了 enum_mapping 但 data_type 非 enum，已忽略。"
                )
        elif dtype == "enum":
            errors.append(f"列 {excel or i} 的 data_type=enum 时必须提供 enum_mapping。")
        rule = col.get("conversion_rule")
        if rule is not None and not isinstance(rule, str):
            errors.append(f"列 {excel or i} 的 conversion_rule 必须是字符串。")
        default = col.get("default_value")
        if default is not None and not isinstance(default, str):
            errors.append(f"列 {excel or i} 的 default_value 必须是字符串。")
    return errors, warnings


def check_required_and_unknown_columns(
    *,
    columns: Sequence[Mapping[str, Any]],
    source_columns: Sequence[str] | None,
    required_columns: Sequence[str] | None,
) -> tuple[list[str], list[str]]:
    """规则 3/4 校验：缺必填列停止、未知列告警忽略。

    返回 (errors, warnings)：
    - errors：必填列缺失 → 停止导入；
    - warnings：未知列（source_columns 中不在映射定义内）→ 告警忽略，
      不自行建立映射。
    必填列 = required_columns（模板必含列）∪ columns 中 is_required=true 的列。
    """
    errors: list[str] = []
    warnings: list[str] = []
    mapped = {c.get("excel_column") for c in columns}
    required = set(required_columns or [])
    required |= {c.get("excel_column") for c in columns if c.get("is_required")}
    missing_from_mapping = sorted(required - mapped)
    if missing_from_mapping:
        errors.append(
            "缺少必填列映射（停止）："
            + "、".join(repr(c) for c in missing_from_mapping)
        )
    if source_columns is not None:
        source_set = set(source_columns)
        # 必填列中已被映射定义的部分，必须存在于 Excel 实际列清单（规则 3
        # 停止语义）；未定义映射的必填列已由上面的错误覆盖，不重复上报。
        missing_in_source = sorted((required & mapped) - source_set)
        if missing_in_source:
            errors.append(
                "Excel 列清单缺少必填列（停止）："
                + "、".join(repr(c) for c in missing_in_source)
            )
        unknown = sorted(source_set - mapped)
        for col in unknown:
            warnings.append(
                f"未知列已忽略（不自行建立映射）：{col!r}"
            )
    return errors, warnings


# ---------------------------------------------------------------------------
# 数据访问（模块树 L262：mapping_service.py 自包含，不扩 repositories.py）
# ---------------------------------------------------------------------------


class MappingStore(Protocol):
    """映射版本/列持久化契约。SqlAlchemyMappingStore 为 PG/SQLite 实现；
    测试用内存实现（tests/domains/）。"""

    def next_version_number(self, template_key: str) -> int: ...

    def create_version(
        self,
        *,
        template_key: str,
        version: int,
        sheet_name: str,
        header_row: int,
        status: str,
        source_file_hash: str,
        created_by: str,
        created_at: datetime,
    ) -> int: ...

    def create_columns(self, version_id: int, columns: Sequence[MappingColumnRecord]) -> None: ...

    def get(self, version_id: int) -> MappingVersionRecord | None: ...

    def list_versions(
        self,
        *,
        template_key: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[MappingVersionRecord], int]: ...

    def published_by_key(self, template_key: str) -> MappingVersionRecord | None: ...

    def set_status(
        self, version_id: int, status: str, published_at: datetime | None = None
    ) -> MappingVersionRecord: ...


class SqlAlchemyMappingStore:
    """SQLAlchemy 实现（D3 表结构 excel_mapping_versions/columns）。"""

    def __init__(self, session_factory: Any) -> None:
        self._factory = session_factory

    def next_version_number(self, template_key: str) -> int:
        with self._factory() as session:
            top = session.scalar(
                select(ExcelMappingVersion.version)
                .where(ExcelMappingVersion.template_key == template_key)
                .order_by(ExcelMappingVersion.version.desc())
                .limit(1)
            )
            return (top or 0) + 1

    def create_version(
        self,
        *,
        template_key: str,
        version: int,
        sheet_name: str,
        header_row: int,
        status: str,
        source_file_hash: str,
        created_by: str,
        created_at: datetime,
    ) -> int:
        with self._factory() as session:
            row = ExcelMappingVersion(
                template_key=template_key,
                version=version,
                sheet_name=sheet_name,
                header_row=header_row,
                status=status,
                source_file_hash=source_file_hash,
                created_by=created_by,
                created_at=created_at,
            )
            session.add(row)
            session.commit()
            return row.id

    def create_columns(self, version_id: int, columns: Sequence[MappingColumnRecord]) -> None:
        with self._factory() as session:
            for col in columns:
                session.add(
                    ExcelMappingColumn(
                        version_id=version_id,
                        excel_column=col.excel_column,
                        target_field=col.target_field,
                        data_type=col.data_type,
                        conversion_rule=col.conversion_rule,
                        is_required=col.is_required,
                        enum_mapping=col.enum_mapping,
                        default_value=col.default_value,
                    )
                )
            session.commit()

    def get(self, version_id: int) -> MappingVersionRecord | None:
        with self._factory() as session:
            row = session.get(ExcelMappingVersion, version_id)
            if row is None:
                return None
            return _to_record(row, _columns_of(session, version_id))

    def list_versions(
        self,
        *,
        template_key: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[MappingVersionRecord], int]:
        with self._factory() as session:
            stmt = select(ExcelMappingVersion)
            if template_key:
                stmt = stmt.where(ExcelMappingVersion.template_key == template_key)
            if status:
                stmt = stmt.where(ExcelMappingVersion.status == status)
            total = session.scalar(
                select(func.count()).select_from(stmt.subquery())
            ) or 0
            rows = session.scalars(
                stmt.order_by(
                    ExcelMappingVersion.template_key, ExcelMappingVersion.version.desc()
                ).limit(limit).offset(offset)
            ).all()
            records = [_to_record(r, _columns_of(session, r.id)) for r in rows]
            return records, total

    def published_by_key(self, template_key: str) -> MappingVersionRecord | None:
        with self._factory() as session:
            row = session.scalar(
                select(ExcelMappingVersion).where(
                    ExcelMappingVersion.template_key == template_key,
                    ExcelMappingVersion.status == MappingStatus.PUBLISHED.value,
                )
            )
            if row is None:
                return None
            return _to_record(row, _columns_of(session, row.id))

    def set_status(
        self, version_id: int, status: str, published_at: datetime | None = None
    ) -> MappingVersionRecord:
        with self._factory() as session:
            row = session.get(ExcelMappingVersion, version_id)
            if row is None:
                raise MappingNotFoundError(
                    f"映射版本 {version_id} 不存在。",
                    {"version_id": version_id},
                )
            row.status = status
            # published_at 只记录发布时刻，退役/废弃不清除（历史保留，规则 6）
            if published_at is not None:
                row.published_at = published_at
            session.commit()
            return _to_record(row, _columns_of(session, version_id))


def _columns_of(session: Session, version_id: int) -> list[MappingColumnRecord]:
    rows = session.scalars(
        select(ExcelMappingColumn)
        .where(ExcelMappingColumn.version_id == version_id)
        .order_by(ExcelMappingColumn.id)
    ).all()
    return [
        MappingColumnRecord(
            excel_column=r.excel_column,
            target_field=r.target_field,
            data_type=r.data_type,
            conversion_rule=r.conversion_rule,
            is_required=r.is_required,
            enum_mapping=r.enum_mapping,
            default_value=r.default_value,
        )
        for r in rows
    ]


def _to_record(row: ExcelMappingVersion, columns: Sequence[MappingColumnRecord]) -> MappingVersionRecord:
    return MappingVersionRecord(
        version_id=row.id,
        template_key=row.template_key,
        version=row.version,
        sheet_name=row.sheet_name,
        header_row=row.header_row,
        status=row.status,
        source_file_hash=row.source_file_hash,
        created_by=row.created_by,
        created_at=row.created_at,
        published_at=row.published_at,
        columns=tuple(columns),
    )


# ---------------------------------------------------------------------------
# 服务（状态机：draft → published → retired；draft → retired 直接废弃）
# ---------------------------------------------------------------------------


class ExcelMappingService:
    """Excel 映射版本管理（6.3 处理规则；manager 管理 API 调用）。

    只读查询（list_versions/get_version）不要求角色之外的额外检查；
    写操作（import/publish/retire）的 CSRF 与角色由 API 层负责。
    """

    def __init__(self, store: MappingStore) -> None:
        self._store = store

    # ---- 导入（规则 3/4/6 + 转换规则合法性） ----

    def import_draft(
        self,
        *,
        actor: str,
        template_key: str,
        sheet_name: str,
        header_row: int,
        source_file_hash: str,
        columns: Sequence[Mapping[str, Any]],
        source_columns: Sequence[str] | None = None,
        required_columns: Sequence[str] | None = None,
    ) -> ImportResult:
        """导入为新 draft 版本（版本号递增，规则 6 不覆盖历史）。

        校验失败（缺必填列停止 / 转换规则非法）→ MappingValidationError，
        不产生版本；未知列仅告警（warnings）不阻断。
        """
        col_errors, col_warnings = validate_column_definitions(columns)
        req_errors, unknown_warnings = check_required_and_unknown_columns(
            columns=columns,
            source_columns=source_columns,
            required_columns=required_columns,
        )
        errors = col_errors + req_errors
        warnings = col_warnings + unknown_warnings
        if errors:
            raise MappingValidationError(
                "映射导入校验失败。", {"errors": errors}
            )
        version = self._store.next_version_number(template_key)
        version_id = self._store.create_version(
            template_key=template_key,
            version=version,
            sheet_name=sheet_name,
            header_row=header_row,
            status=MappingStatus.DRAFT.value,
            source_file_hash=source_file_hash,
            created_by=actor,
            created_at=_now(),
        )
        records = tuple(
            MappingColumnRecord(
                excel_column=c["excel_column"],
                target_field=c["target_field"],
                data_type=c["data_type"],
                conversion_rule=c.get("conversion_rule"),
                is_required=bool(c.get("is_required", False)),
                enum_mapping=c.get("enum_mapping"),
                default_value=c.get("default_value"),
            )
            for c in columns
        )
        self._store.create_columns(version_id, records)
        created = self._store.get(version_id)
        assert created is not None
        logger.info(
            "mapping_draft_imported",
            template_key=template_key,
            version=version,
            actor=actor,
            warnings=warnings,
        )
        return ImportResult(version=created, warnings=tuple(warnings))

    # ---- 发布（规则 2/6；重复发布同 key 被拒） ----

    def publish(self, *, actor: str, version_id: int) -> MappingVersionRecord:
        """draft → published；记录 published_at。

        - 同 template_key 已存在 published 版本 → MappingPublishedExistsError
          （规则 2：一次只有一个发布版本；表级部分唯一索引兜底）；
        - 非 draft 状态（重复发布/retired）→ MappingStateError。
        """
        record = self._require(version_id)
        if record.status != MappingStatus.DRAFT.value:
            raise MappingStateError(
                f"版本 {version_id} 状态为 {record.status!r}，只有 draft 可发布。",
                {"version_id": version_id, "status": record.status},
            )
        existing = self._store.published_by_key(record.template_key)
        if existing is not None:
            raise MappingPublishedExistsError(
                f"template_key={record.template_key!r} 已有发布版本 "
                f"v{existing.version}（{existing.version_id}），一次只有一个发布版本；"
                "请先退役当前版本再发布新版本。",
                {
                    "template_key": record.template_key,
                    "existing_version_id": existing.version_id,
                    "existing_version": existing.version,
                },
            )
        published = self._store.set_status(
            version_id, MappingStatus.PUBLISHED.value, published_at=_now()
        )
        logger.info(
            "mapping_published",
            template_key=published.template_key,
            version=published.version,
            actor=actor,
        )
        return published

    # ---- 退役（6.3 状态机；端点同型扩展，材料说明） ----

    def retire(self, *, actor: str, version_id: int) -> MappingVersionRecord:
        """published → retired（或废弃 draft → retired）；终态不可再迁移。"""
        record = self._require(version_id)
        if record.status == MappingStatus.RETIRED.value:
            raise MappingStateError(
                f"版本 {version_id} 已退役（retired），历史不可恢复。",
                {"version_id": version_id, "status": record.status},
            )
        retired = self._store.set_status(
            version_id, MappingStatus.RETIRED.value
        )
        logger.info(
            "mapping_retired",
            template_key=retired.template_key,
            version=retired.version,
            actor=actor,
        )
        return retired

    # ---- published 读取（规则 1；D19 6.4 步骤 2 复用接口） ----

    def get_published(self, template_key: str) -> MappingVersionRecord:
        """按 template_key 读取当前 published 版本（含列级快照）。

        无 published → MappingNotPublishedError（D19 上层转业务错误；
        "无 published 宁可报错"——规则 1 只使用 published 版本）。
        """
        record = self._store.published_by_key(template_key)
        if record is None:
            raise MappingNotPublishedError(
                f"template_key={template_key!r} 没有已发布（published）的映射版本。",
                {"template_key": template_key},
            )
        return record

    # ---- 查询 ----

    def list_versions(
        self,
        *,
        template_key: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MappingVersionRecord], int]:
        """版本列表（历史版本保留不随退役修改，规则 6）。"""
        return self._store.list_versions(
            template_key=template_key,
            status=status,
            limit=limit,
            offset=offset,
        )

    def get_version(self, version_id: int) -> MappingVersionRecord:
        return self._require(version_id)

    def _require(self, version_id: int) -> MappingVersionRecord:
        record = self._store.get(version_id)
        if record is None:
            raise MappingNotFoundError(
                f"映射版本 {version_id} 不存在。",
                {"version_id": version_id},
            )
        return record
