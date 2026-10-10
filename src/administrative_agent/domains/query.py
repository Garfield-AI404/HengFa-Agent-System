"""R4 查询域（规格 6.1 六条 + 9.2 lookup_erp 六条，mcp-api.md §4）。

口径对照：
- 只使用 query 工具、不写 SQL（6.1 第 1 条）；
- 查询字段必须来自 list_entities（9.2 第 1 条，复用 D5 EntityCache）；
- 强制应用当前 Agent 可查询实体白名单（9.2 第 2 条，scopes.QUERYABLE_ENTITIES）；
- limit 默认 50、最大 200（mcp-api.md §4 / 9.2 第 3 条）；
- 自动分页场景不得丢失 total（9.2 第 4 条）：fetch_all 逐页翻取，返回
  total/pages/是否取全，显式表达已分页（6.1 第 3 条——不把第一页当全量）；
- 金额/日期/枚举保持 ERP 返回格式，本地不做格式转换（6.1 第 5 条）；
- 空结果明确返回"没有符合条件的记录"，不把"未查到"解释成"从未存在"
  （6.1 第 4 条；软删除由 ERP 参数化 SQL 自动排除）；
- structuredContent.code 原样转换为内部错误类型（9.2 第 5 条，MCPClient
  已实现，本模块透传、沿用既有错误码体系不新造）；
- 不把 ERP 任意字符串字段当指令执行（9.2 第 6 条）：filter/order_by
  原样透传不解析不 eval，字段名只与元数据比对。

执行器语义：
- 只读任务（is_write=False）创建为 PENDING、不持锁不排队（8.3/RV-09 §8），
  执行入口接受 PENDING/RUNNING，执行前统一迁移到 running；
- 审计按既有 task_attempts 机制（10.4 顺序 7/11，mcp-api.md L464/L501：
  query 不写 mcp_operation_audits）：每次实际调用一条 attempt + 一条
  result（11.2 规则 2/3），不为查询新造审计机制。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import structlog

from administrative_agent.audit.service import AuditService
from administrative_agent.mcp.client import MCPClient
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskNotRunningError, TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.query")

#: 单页大小默认值（mcp-api.md §4：默认 50）
DEFAULT_QUERY_LIMIT = 50
#: limit 上限（mcp-api.md §4 / 9.2 第 3 条：最大 200）
MAX_QUERY_LIMIT = 200
#: offset 上限（mcp-api.md §4：最大 10000）
MAX_QUERY_OFFSET = 10000
#: fetch_all 翻页保护上限：超出标记 complete=False 并说明限制，不无限翻页
MAX_FETCH_ALL_PAGES = 1000

#: 10.2 操作枚举：R4 查询（与 intent.Operation.QUERY 一致，代码枚举不出消息）
OP_QUERY = "query"

#: 空结果的标准表达（6.1 第 4 条：明确"没有符合条件的记录"；
#: 同时注明软删除自动排除与"未查到 ≠ 从未存在"的边界）
EMPTY_RESULT_MESSAGE = "没有符合条件的记录（查询自动排除软删除数据；未查到不代表数据从未存在）"


@dataclass(frozen=True)
class QuerySpec:
    """一次 query 调用的声明式规格（mcp-api.md §4 参数信封）。"""

    entity: str
    fields: tuple[str, ...] = ()
    filter: dict[str, Any] | None = None
    order_by: str | Sequence[str] | None = None
    limit: int = DEFAULT_QUERY_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if not self.entity:
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.VALIDATION,
                message="entity is required for query.",
            )
        if self.limit < 1 or self.limit > MAX_QUERY_LIMIT:
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.VALIDATION,
                message=f"limit must be between 1 and {MAX_QUERY_LIMIT}.",
                details={"limit": self.limit},
            )
        if self.offset < 0 or self.offset > MAX_QUERY_OFFSET:
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.VALIDATION,
                message=f"offset must be between 0 and {MAX_QUERY_OFFSET}.",
                details={"offset": self.offset},
            )

    def with_offset(self, offset: int) -> "QuerySpec":
        return QuerySpec(
            entity=self.entity,
            fields=self.fields,
            filter=self.filter,
            order_by=self.order_by,
            limit=self.limit,
            offset=offset,
        )

    def to_payload(self) -> dict[str, Any]:
        """mcp-api.md §4 参数信封；可选参数缺省不携带（服务端自行默认）。"""
        payload: dict[str, Any] = {"entity": self.entity, "limit": self.limit, "offset": self.offset}
        if self.fields:
            payload["fields"] = list(self.fields)
        if self.filter:
            payload["filter"] = dict(self.filter)
        if self.order_by is not None:
            payload["order_by"] = self.order_by
        return payload

    def to_dict(self) -> dict[str, Any]:
        """审计 field_diffs 摘要（敏感值由 audit 层统一脱敏）。"""
        return self.to_payload()


def build_query_spec(
    *,
    entity: str,
    fields: Sequence[str] = (),
    filter: Mapping[str, Any] | None = None,
    order_by: str | Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> QuerySpec:
    """构造 QuerySpec：limit 缺省 50（mcp-api.md §4）；超限由 __post_init__ 拒绝。"""
    return QuerySpec(
        entity=entity,
        fields=tuple(fields),
        filter=dict(filter) if filter else None,
        order_by=order_by,
        limit=limit if limit is not None else DEFAULT_QUERY_LIMIT,
        offset=offset,
    )


def validate_query_spec(
    spec: QuerySpec,
    cache: EntityCache,
    allowed_entities: frozenset[str],
) -> None:
    """查询前校验（9.2 第 1/2/6 条 + 6.1 第 2 条，fail closed）。

    顺序：白名单（本地常量，不依赖网络）→ 实体来自 list_entities（D5 缓存，
    读路径 stale-while-error 允许旧缓存）→ 字段与元数据比对。
    filter/order_by 原样透传（第 6 条：不解析、不 eval、不拼接 SQL）。
    """
    if spec.entity not in allowed_entities:
        raise MCPError(
            code=InternalCode.NOT_WHITELISTED.value,
            category=ErrorCategory.LOCAL,
            message=f"Entity {spec.entity!r} is not queryable by this agent.",
            details={"entity": spec.entity},
        )
    entity = cache.get_entity(spec.entity)
    if entity is None:
        raise MCPError(
            code=InternalCode.MCP_METADATA_UNKNOWN_ENTITY.value,
            category=ErrorCategory.LOCAL,
            message=f"Entity {spec.entity!r} is not in metadata (list_entities).",
            details={"entity": spec.entity},
        )
    unknown = [f for f in spec.fields if f not in entity.fields]
    if unknown:
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="Query field(s) not defined by list_entities for this entity.",
            details={"entity": spec.entity, "fields": unknown},
        )


@dataclass(frozen=True)
class QueryPage:
    """单页查询结果（mcp-api.md §4 响应语义 + 分页信息显式表达）。"""

    items: tuple[dict[str, Any], ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        """本页未取全：剩余条数 = total - (offset + 本页实际条数)。"""
        return self.offset + len(self.items) < self.total

    @property
    def empty(self) -> bool:
        return not self.items

    @classmethod
    def from_erp(cls, data: Mapping[str, Any], limit: int, offset: int) -> "QueryPage":
        """解析 query 响应。total/items 结构不符视为解析失败（不把损坏
        响应当空结果——6.1 第 4 条禁止把"未查到"与"从未存在"混为一谈）。"""
        items_raw = data.get("items")
        total_raw = data.get("total")
        if not isinstance(items_raw, list) or not isinstance(total_raw, int):
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.UNKNOWN,
                message="Query response is missing items/total.",
            )
        # 行级防御（O-2）：非 dict 行视为解析失败上抛，不静默丢弃
        # （丢弃会使 returned_count 与 total 关系失真）
        items: list[dict[str, Any]] = []
        for row in items_raw:
            if not isinstance(row, dict):
                raise MCPError(
                    code=InternalCode.VALIDATION_ERROR.value,
                    category=ErrorCategory.UNKNOWN,
                    message="Query response item is not an object.",
                )
            items.append(dict(row))
        return cls(
            items=tuple(items),
            total=total_raw,
            limit=limit,
            offset=offset,
        )

    def audit_payload(self) -> dict[str, Any]:
        """result 事件的业务摘要（11.3 监控按 result 事件计数）。"""
        return {
            "returned_count": len(self.items),
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class FullQueryResult:
    """自动分页取全结果（9.2 第 4 条：不丢 total、显式表达已分页）。"""

    items: tuple[dict[str, Any], ...]
    total: int
    pages: int
    fetched_count: int
    complete: bool
    notes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.items

    def limitations(self) -> tuple[str, ...]:
        """数据不完整/变化时的显式限制说明（6.2 第 4 条口径）。"""
        return self.notes


def _task_not_active(task_id: str, status: TaskStatus | None) -> TaskNotRunningError:
    """任务不存在抛 KeyError、非活动状态抛 TaskNotRunningError（同 D14）。"""
    if status is None:
        raise KeyError(f"task not found: {task_id}")
    return TaskNotRunningError(task_id, status)


class QueryExecutor:
    """R4 查询执行器（审计按既有 task_attempts 机制，不为查询新造审计）。

    组合用法：``fetch_all(..., finalize=False)`` 供分析执行器取数（任务终态
    由分析执行器统一收口）；独立使用 ``execute`` / ``fetch_all`` 默认收口
    任务终态（succeeded/failed）。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        cache: EntityCache,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        duty_code: str | None = None,
        policy: Any | None = None,
        allowed_entities: frozenset[str] = QUERYABLE_ENTITIES,
        target_type: str = "query",
        max_pages: int = MAX_FETCH_ALL_PAGES,
    ) -> None:
        self._client = client
        self._cache = cache
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._duty_code = duty_code
        self._policy = policy
        self._allowed_entities = allowed_entities
        self._target_type = target_type
        self._max_pages = max_pages

    # ---- 任务状态（只读任务：PENDING 创建、直接执行、不排队） ----

    def begin_active(self, task_id: str) -> None:
        """执行入口：只读任务创建为 PENDING（8.3 第 1 条只读不持锁），
        PENDING/RUNNING 均可直接执行，执行前统一迁移到 running；
        终态/不存在拒绝。"""
        record = self._tasks.get_task(task_id)
        if record is None:
            raise _task_not_active(task_id, None)
        if record.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            if record.status == TaskStatus.PENDING:
                self._tasks.transition(task_id, TaskStatus.RUNNING)
            return
        raise _task_not_active(task_id, record.status)

    def finish(self, task_id: str) -> None:
        """成功收口：最终验证通过（响应解析成功 + total 透传），
        只经此路径进入 succeeded（8.1：模型不能直接把任务标记为 succeeded）。"""
        self._tasks.mark_succeeded(task_id, verification_passed=True)

    def fail(self, task_id: str, error_code: str, summary: str) -> None:
        self._tasks.mark_failed(task_id, error_code=error_code, error_summary=summary)

    # ---- 执行 ----

    def execute(self, *, task_id: str, spec: QuerySpec) -> QueryPage:
        """单页查询（mcp-api.md §4 语义）。

        返回 QueryPage；任务终态 succeeded；本地校验/服务端错误 →
        任务 failed + 原样抛 MCPError（错误码沿用既有体系）。
        """
        self.begin_active(task_id)
        try:
            validate_query_spec(spec, self._cache, self._allowed_entities)
        except MCPError as exc:
            self.fail(task_id, exc.code, f"查询参数校验失败：{exc}")
            raise
        try:
            page = self.run_page(task_id, spec)
        except MCPError as exc:
            # 服务端错误/响应解析失败：任务 failed + 原样上抛（错误码沿用既有体系）
            self.fail(task_id, exc.code, f"查询失败：{exc}")
            raise
        self.finish(task_id)
        return page

    def run_page(self, task_id: str, spec: QuerySpec) -> QueryPage:
        """单页实际调用：一条 attempt + 一条 result（11.2 规则 2/3），
        不触碰任务终态（供 fetch_all 逐页与分析取数复用）。"""
        record = self._tasks.get_task(task_id)
        assert record is not None, "task must exist before run_page"
        op_id = record.operation_id
        attempt = self._record_attempt(record, spec)
        result = self._client.call_tool(
            "query",
            spec.to_payload(),
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        if not result.ok:
            err = result.error
            self._record_result(
                attempt,
                "failed",
                err,
                summary=f"ERP 查询失败：{err.message}",
            )
            # 9.2 第 5 条：structuredContent.code 已由 MCPClient 原样映射，
            # 原样上抛，不新造错误码
            raise err
        try:
            page = QueryPage.from_erp(result.data, spec.limit, spec.offset)
        except MCPError as exc:
            self._record_result(
                attempt,
                "failed",
                exc,
                summary=f"查询响应解析失败：{exc.message}",
            )
            raise
        self._record_result(
            attempt,
            "success",
            None,
            summary=f"查询 {spec.entity} 返回 {len(page.items)} 条"
            f"（total={page.total}, offset={page.offset}）",
            payload=page.audit_payload(),
        )
        return page

    def fetch_all(
        self,
        *,
        task_id: str,
        spec: QuerySpec,
        finalize: bool = True,
    ) -> FullQueryResult:
        """自动分页取全（6.1 第 3 条 + 9.2 第 4 条）。

        从 spec.offset 起逐页翻取（每页独立 attempt/result，11.2 规则 3）；
        total 逐页保留并校验一致（分页期间数据变化 → complete=False + 显式
        说明，不掩盖限制）；页数达上限标记 complete=False。finalize=True
        时收口任务终态。
        """
        self.begin_active(task_id)
        try:
            validate_query_spec(spec, self._cache, self._allowed_entities)
        except MCPError as exc:
            self.fail(task_id, exc.code, f"查询参数校验失败：{exc}")
            raise
        items: list[dict[str, Any]] = []
        total: int | None = None
        pages = 0
        notes: list[str] = []
        offset = spec.offset
        try:
            while True:
                page = self.run_page(task_id, spec.with_offset(offset))
                pages += 1
                items.extend(page.items)
                if total is None:
                    total = page.total
                elif page.total != total:
                    notes.append(
                        "分页期间数据发生变化（total 前后不一致），结果可能不完整，"
                        "建议在变更少的时段重新查询"
                    )
                    total = page.total
                offset += len(page.items)
                if not page.items:
                    # 空页 break（O-1）：offset 未达 total 却取不到数据（并发
                    # 漂移变体）→ complete=False 必须显式说明（6.2 第 4 条），
                    # 不留空 notes；offset >= total 属正常取完不追加
                    if offset < (total if total is not None else 0):
                        notes.append(
                            "后续页返回空（数据在分页期间变化），结果可能不完整，"
                            "建议在变更少的时段重新查询"
                        )
                    break
                if offset >= total:
                    break
                if pages >= self._max_pages:
                    notes.append(
                        f"翻页超过 {self._max_pages} 页上限仍未取完，仅返回前 "
                        f"{len(items)} 条，未取全"
                    )
                    break
        except MCPError as exc:
            # 中途失败：任务 failed + 原样上抛（已取部分不当作全量结果返回）
            self.fail(task_id, exc.code, f"分页查询失败（第 {pages + 1} 页）：{exc}")
            raise
        complete = not notes and offset >= (total or 0)
        full = FullQueryResult(
            items=tuple(items),
            total=total if total is not None else len(items),
            pages=pages,
            fetched_count=len(items),
            complete=complete,
            notes=tuple(notes),
        )
        if finalize:
            self.finish(task_id)
        return full

    # ---- 审计 ----

    def _record_attempt(self, record, spec: QuerySpec):
        return self._audit.record_attempt(
            task_id=record.task_id,
            operation_id=record.operation_id,
            trace_id=self._trace_id,
            target_type=self._target_type,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=self._duty_code or record.duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            target_id=spec.entity,
            field_diffs=spec.to_dict(),
        )

    def _record_result(
        self,
        attempt,
        result_status: str,
        error: MCPError | None,
        *,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status=result_status,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            error_code=error.code if error else None,
            result_summary=summary,
            structured_payload=payload,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )
