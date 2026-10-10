"""list_entities 元数据缓存（规格 2.3：启动时及元数据失效后刷新）。

契约依据 mcp-api.md §3：实体含四能力字段（readable/creatable/updatable/
deletable）、business_operations 与 fields（writable_on_create/update 描述
通用 CRUD）。新客户端必须读取四个能力字段，不依赖旧 read_only。

缓存失效策略（可配置，15.2/13.4）：
- TTL（ERP_MCP_METADATA_TTL，默认 3600s）到期后下一次访问触发刷新；
- 刷新失败时有旧缓存：返回旧缓存并标记 stale（读仍可用；写校验由上层
  结合 stale 标志决定，不得以过期元数据放行写操作）；
- 刷新失败且无旧缓存：fail closed，抛 MCPError（ERP 不可达时不允许写入，13.4）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from administrative_agent.mcp.client import MCPClient, ToolResult
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError

# mcp-api.md §3 的字段属性键
_FIELD_ATTRS = (
    "type",
    "label",
    "required",
    "nullable",
    "enum_values",
    "default",
    "min_value",
    "max_value",
    "min_length",
    "max_length",
    "writable_on_create",
    "writable_on_update",
    "system_managed",
    "sensitive",
)


@dataclass(frozen=True)
class FieldSpec:
    """实体字段能力（供上层校验，14.2 字段校验）。"""

    name: str
    type: str
    label: str
    required: bool = False
    nullable: bool = True
    writable_on_create: bool = False
    writable_on_update: bool = False
    system_managed: bool = False
    sensitive: bool = False
    #: 是否为派生/计算列（mcp-api.md §3 `computed` 键）：普通 ORM 列 false，
    #: 派生列 true。计算列只能作为 `fields` 取回，**不可**用于 filter/order_by
    #: ——ERP 侧对计算列 filter/order_by 一律拒绝（query.py `_assert_column_field`）。
    #: 消费方据此在提交请求前把计算列从 filter/order_by 中剔除（宽容剔除 + note）。
    computed: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, name: str, raw: dict[str, Any]) -> "FieldSpec":
        return cls(
            name=name,
            type=str(raw.get("type") or ""),
            label=str(raw.get("label") or ""),
            required=bool(raw.get("required")),
            nullable=bool(raw.get("nullable", True)),
            writable_on_create=bool(raw.get("writable_on_create")),
            writable_on_update=bool(raw.get("writable_on_update")),
            system_managed=bool(raw.get("system_managed")),
            sensitive=bool(raw.get("sensitive")),
            computed=bool(raw.get("computed")),
            raw=dict(raw),
        )


@dataclass(frozen=True)
class EntityMetadata:
    """list_entities 返回的单实体元数据（新客户端读四能力字段）。"""

    name: str
    label: str
    readable: bool
    creatable: bool
    updatable: bool
    deletable: bool
    business_operations: tuple[str, ...] = ()
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "EntityMetadata":
        name = str(raw.get("name") or "")
        fields = {
            fname: FieldSpec.from_raw(fname, fraw)
            for fname, fraw in (raw.get("fields") or {}).items()
            if isinstance(fraw, dict)
        }
        ops = raw.get("business_operations") or ()
        return cls(
            name=name,
            label=str(raw.get("label") or ""),
            readable=bool(raw.get("readable")),
            creatable=bool(raw.get("creatable")),
            updatable=bool(raw.get("updatable")),
            deletable=bool(raw.get("deletable")),
            business_operations=tuple(str(o) for o in ops),
            fields=fields,
            raw=dict(raw),
        )


def parse_entities(data: dict[str, Any]) -> list[EntityMetadata]:
    """解析 list_entities 的 ToolResult.data。

    兼容两种信封：实体数组（mcp-api.md §3 风格）或 {"entities": [...]}。
    """
    raw_list = data.get("entities") if isinstance(data.get("entities"), list) else None
    if raw_list is None and isinstance(data, list):
        raw_list = data
    if raw_list is None:
        raise ValueError("list_entities response has no entity list")
    entities = [EntityMetadata.from_raw(raw) for raw in raw_list if isinstance(raw, dict)]
    return [e for e in entities if e.name]


class EntityCache:
    """list_entities 缓存：TTL 可配置、显式 refresh、刷新失败 stale-while-error。"""

    def __init__(self, client: MCPClient, ttl_seconds: int = 3600) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._entities: dict[str, EntityMetadata] = {}
        # [2026-09-13] ``_loaded_at`` 必须显式表达「从未加载」。历史缺陷：初值用
        # ``0.0``，而新鲜度判据与 ``time.monotonic()`` 作差——后者是**宿主机开机
        # 秒数**（并非进程启动时刻），宿主机 uptime < ttl（默认 3600s，
        # settings.erp_mcp_metadata_ttl）时 ``now - 0.0 < ttl`` 恒真，
        # ``_ensure_fresh`` 在前置判断处直接 return → 缓存永不加载 → 所有依赖
        # 元数据的路径（erp_query 实体白名单 / ensure_write_allowed 写守卫 /
        # 考勤契约校验）fail closed，须等宿主机开机满 1 小时才自愈（重启进程无效）。
        # None 表达「未加载」可消除该量纲不匹配与负值/边界依赖，切勿「优化」回数值。
        self._loaded_at: float | None = None
        self._stale: bool = False
        self._lock = threading.Lock()

    # ---- 读取 ----

    def get_entities(self) -> list[EntityMetadata]:
        """缓存命中直接返回；过期或缺失时刷新。刷新失败且无缓存则抛 MCPError。"""
        self._ensure_fresh()
        return list(self._entities.values())

    def get_entity(self, name: str) -> EntityMetadata | None:
        self._ensure_fresh()
        return self._entities.get(name)

    def field_capabilities(self, entity_name: str, field_name: str) -> FieldSpec | None:
        """查询字段能力供上层校验（任务书交付物 4）。"""
        entity = self.get_entity(entity_name)
        if entity is None:
            return None
        return entity.fields.get(field_name)

    def is_stale(self) -> bool:
        """元数据是否陈旧（上次刷新失败或已过期但未刷新成功）。"""
        return self._stale

    def ensure_write_allowed(self, entity_name: str) -> EntityMetadata:
        """写前守卫（决策项 a，RV-07 reviewer 口径）：陈旧元数据拒绝写入。

        不得以过期元数据放行写操作（13.4）：元数据刷新失败（stale）或实体
        不在元数据内（scope 外/未知）时抛 MCPError，调用方不得发出写请求。
        实体四能力（creatable/updatable）与字段级 writable 校验属领域适配器
        （D12+），本守卫只覆盖元数据可用性。
        """
        if self.is_stale():
            raise MCPError(
                code=InternalCode.MCP_METADATA_STALE.value,
                category=ErrorCategory.LOCAL,
                retryable=False,
                message="ERP entity metadata is stale; writes are disabled.",
            )
        entity = self.get_entity(entity_name)
        if entity is None:
            raise MCPError(
                code=InternalCode.MCP_METADATA_UNKNOWN_ENTITY.value,
                category=ErrorCategory.LOCAL,
                retryable=False,
                message=f"Entity {entity_name!r} is not in metadata; writes are disabled.",
                details={"entity": entity_name},
            )
        return entity

    # ---- 刷新 ----

    def refresh(self) -> None:
        """强制刷新（启动时与元数据失效后调用，规格 2.3）。"""
        with self._lock:
            result = self._client.list_entities()
            self._apply_result(result)

    def _is_fresh(self, now: float) -> bool:
        """唯一的新鲜度判据：「已加载且未过期」（[2026-09-13] 见 ``__init__`` 注释）。

        以 ``_loaded_at is None`` 显式区分「从未加载」（必须刷新）与「已加载但
        过期」（必须刷新）；已加载但实体集为空（scope 收紧后 §2.4）仍是有效缓存，
        在 TTL 内命中、不重复请求——与既有语义一致。不得再引入「空实体集 ==
        未加载」的隐式等价，否则会重现开机窗口内缓存永不加载的缺陷。
        """
        return self._loaded_at is not None and (now - self._loaded_at) < self._ttl

    def _ensure_fresh(self) -> None:
        now = time.monotonic()
        if self._is_fresh(now):
            return
        with self._lock:
            now = time.monotonic()
            if self._is_fresh(now):
                return
            result = self._client.list_entities()
            self._apply_result(result)

    def _apply_result(self, result: ToolResult) -> None:
        if not result.ok:
            if self._entities:
                # stale-while-error：读仍可用，标记陈旧，由上层决定写校验策略（13.4）
                self._stale = True
                return
            # fail closed：无旧缓存时不提供元数据
            error = result.error or MCPError(
                code="MCP_METADATA_UNAVAILABLE",
                category=ErrorCategory.UNKNOWN,
                message="ERP entity metadata unavailable.",
            )
            raise error
        try:
            self._entities = {e.name: e for e in parse_entities(result.data)}
        except ValueError as exc:
            if self._entities:
                self._stale = True
                return
            raise MCPError(
                code="MCP_METADATA_PARSE_ERROR",
                category=ErrorCategory.UNKNOWN,
                message=str(exc),
            ) from exc
        self._loaded_at = time.monotonic()
        self._stale = False
