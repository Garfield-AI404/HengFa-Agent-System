"""D17 R4 查询域测试（规格 6.1 六条 + 9.2 lookup_erp 六条，mcp-api.md §4）。

覆盖：分页包装与 total 保留、空结果表达、软删除表达、过滤/排序透传、
实体白名单、字段校验、limit 上限、错误码原样归类、只读任务 PENDING
直接执行、审计 attempt/result 链。

FakeQueryErp 为 respx 假服务（D5 规则：测试夹具，不代表 ERP 真实运行态），
实现 mcp-api.md §4 子集：filter 等值/操作符、order_by（field/-field）、
limit/offset 分页、total 全量、软删除自动排除、错误注入、分页期数据变化注入。
"""

from __future__ import annotations

import json
import os
from typing import Any

os.environ["ERP_MCP_URL"] = "http://erp-mcp.test"
os.environ["ERP_MCP_API_KEY"] = "test-api-key-not-for-production"
os.environ["ERP_MCP_JWT"] = ""
os.environ["ERP_MCP_CONNECT_TIMEOUT"] = "5"
os.environ["ERP_MCP_READ_TIMEOUT"] = "30"
os.environ["ERP_MCP_TOTAL_TIMEOUT"] = "60"
os.environ["ERP_MCP_METADATA_TTL"] = "3600"

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from administrative_agent.audit.service import AuditService  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.query import (  # noqa: E402
    EMPTY_RESULT_MESSAGE,
    MAX_QUERY_LIMIT,
    MAX_QUERY_OFFSET,
    QueryExecutor,
    QuerySpec,
    build_query_spec,
    validate_query_spec,
)
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.metadata import EntityCache  # noqa: E402
from administrative_agent.tasks.machine import TaskStatus  # noqa: E402
from administrative_agent.tasks.records import NewTaskSpec  # noqa: E402
from administrative_agent.tasks.service import TaskNotRunningError, TaskService  # noqa: E402
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

ERP_BASE = "http://erp-mcp.test"
ERP_MCP_URL = f"{ERP_BASE}/mcp"

TRACE_ID = "trace-d17-query-0001"
ACTOR_ACCOUNT = "manager01"
ACTOR_ROLE = "manager"

#: 可查询实体白名单之外的实体（在元数据内，但 Agent 不可查询）。
#: [2026-09-12 只读扩展] 原取 attendance——该实体本次已开只读，改取
#: users（既不在读白名单、也不在写白名单，永远不可由 Agent 触达）。
NON_QUERYABLE_ENTITY = "users"

#: list_entities 元数据（mcp-api.md §3 风格，D5 契约）
ENTITIES_PAYLOAD = [
    {
        "name": "metal_orders",
        "label": "钣金订单",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": ["create_order_bundle", "update_order"],
        "fields": {
            "id": {"type": "integer", "label": "ID"},
            "customer_id": {"type": "integer", "label": "客户 ID"},
            "model": {"type": "string", "label": "型号"},
            "status": {"type": "string", "label": "状态"},
            "unit_price": {"type": "string", "label": "单价"},
            "quantity": {"type": "string", "label": "数量"},
            "weight_per_unit": {"type": "string", "label": "单重"},
            "weight_unit": {"type": "string", "label": "重量单位"},
            "price_type": {"type": "string", "label": "计价方式"},
            "total_amount": {"type": "string", "label": "金额"},
            "in_date": {"type": "string", "label": "入库日期"},
            "out_date": {"type": "string", "label": "出库日期"},
            "notes": {"type": "string", "label": "备注"},
            "updated_at": {"type": "string", "label": "更新时间"},
        },
    },
    {
        "name": "powder_orders",
        "label": "塑粉订单",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": ["create_order_bundle", "update_order"],
        "fields": {
            "id": {"type": "integer", "label": "ID"},
            "supplier_id": {"type": "integer", "label": "供应商 ID"},
            "color_code": {"type": "string", "label": "色号"},
            "total_weight": {"type": "string", "label": "重量"},
            "price_per_kg": {"type": "string", "label": "单价"},
            "total_amount": {"type": "string", "label": "金额"},
            "in_date": {"type": "string", "label": "入库日期"},
            "notes": {"type": "string", "label": "备注"},
            "updated_at": {"type": "string", "label": "更新时间"},
        },
    },
    {
        "name": "customers",
        "label": "客户",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "integer", "label": "ID"},
            # company_name 为 ERP 字段目录口径（tools/crop.py:25 注释：
            # "customers 名称字段以 ERP 字段目录为准：company_name，name
            # 兼容 demo 契约"）；两者均在目录内，缺任一会让对账主体查询
            # （reconciliation 步骤 4a）被字段校验拦下。
            "company_name": {"type": "string", "label": "公司名称"},
            "name": {"type": "string", "label": "名称"},
            "region": {"type": "string", "label": "地区"},
        },
    },
    {
        "name": "suppliers",
        "label": "供应商",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "integer", "label": "ID"},
            "name": {"type": "string", "label": "名称"},
        },
    },
    {
        "name": NON_QUERYABLE_ENTITY,
        "label": "用户账号",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {"id": {"type": "integer", "label": "ID"}},
    },
]


def _metal_row(oid: int, month: str = "2026-06", status: str = "完成") -> dict[str, Any]:
    return {
        "id": oid,
        "customer_id": 100 + oid,
        "model": f"M-{oid}",
        "status": status,
        "unit_price": "12.50",
        "quantity": "10",
        "weight_per_unit": "3.00",
        "weight_unit": "kg",
        "price_type": "按重量",
        "total_amount": "375.00",
        "in_date": f"{month}-{1 + (oid % 27):02d}",
        "out_date": f"{month}-{3 + (oid % 25):02d}" if status == "完成" else None,
        "notes": "",
        "updated_at": f"2026-07-01 08:00:{oid % 60:02d}",
    }


class FakeQueryErp:
    """R4 查询假服务（D5 规则：测试夹具，不代表 ERP 真实运行态）。

    mcp-api.md §4 子集：filter 等值简写与操作符（eq/ne/gt/gte/lt/lte/in/
    like）、order_by（field / -field）、limit/offset 分页、total 为过滤后
    全量、软删除行（deleted_at 非空）自动排除、错误注入、分页期数据变化
    注入（drift_at_call：第 N 次调用前删行，模拟并发变更）。
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {
            "metal_orders": [_metal_row(i) for i in range(1, 11)],
            "powder_orders": [
                {
                    "id": 1, "supplier_id": 5, "color_code": "RAL-9016",
                    "total_weight": "500", "price_per_kg": "28.00",
                    "total_amount": "14000.00", "in_date": "2026-06-10",
                    "notes": "", "updated_at": "2026-07-01 09:00:00",
                },
            ],
            "customers": [
                {"id": 101, "name": "示例五金", "company_name": "示例五金", "region": "华东"},
                {"id": 102, "name": "天工机械", "company_name": "天工机械", "region": "华南"},
                {"id": 103, "name": "永盛钣金", "company_name": "永盛钣金", "region": "华东"},
            ],
            "suppliers": [{"id": 5, "name": "江城塑粉厂"}],
        }
        # 软删除行（deleted_at 非空 → ERP 自动排除，mcp-api.md §4）
        self.rows["metal_orders"].append(
            {**_metal_row(99, month="2020-01"), "deleted_at": "2026-07-02 10:00:00"}
        )
        self.calls: list[dict[str, Any]] = []
        self.fail_next: list[tuple[str, str]] = []  # (code, message)
        self.drift_at_call: dict[str, int] = {}  # entity -> 第 N 次调用前删行
        self._entity_calls: dict[str, int] = {}
        #: O-1/O-2 注入：entity -> {offset}；该页返回空（total 保持）或 items 含非 dict 行
        self.empty_pages_at: dict[str, set[int]] = {}
        self.corrupt_items_at: dict[str, set[int]] = {}

    # ---- respx 入口 ----

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return self._resp(
                {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-query-erp", "version": "0.0.0"}}
            )
        params = body.get("params") or {}
        name = params.get("name")
        if name == "list_entities":
            return self._resp({"entities": ENTITIES_PAYLOAD})
        if name == "query":
            return self.query(params.get("arguments") or {})
        return self._error("METHOD_NOT_FOUND", f"unknown tool {name!r}")

    def query(self, args: dict[str, Any]) -> httpx.Response:
        self.calls.append(args)
        entity = args.get("entity")
        self._entity_calls[entity] = self._entity_calls.get(entity, 0) + 1
        if self.fail_next:
            code, message = self.fail_next.pop(0)
            return self._error(code, message)
        if self.drift_at_call.get(entity, 0) == self._entity_calls[entity]:
            # 模拟并发变更：第 N 次调用前删掉 3 行（total 与首页不一致）
            self.rows[entity] = self.rows[entity][:-3]
            self.drift_at_call[entity] = -1
        # 软删除行自动排除（mcp-api.md §4：参数化 SQL 自动排除软删除记录）
        rows = self._apply_filter(
            [r for r in self.rows.get(entity, []) if not r.get("deleted_at")],
            args.get("filter"),
        )
        rows = self._apply_order(rows, args.get("order_by"))
        total = len(rows)
        offset = args.get("offset", 0)
        limit = args.get("limit", 50)
        page = rows[offset : offset + limit]
        fields = args.get("fields")
        if fields:
            page = [{k: v for k, v in r.items() if k in fields} for r in page]
        if offset in self.corrupt_items_at.get(entity, set()):
            page = page + ["GARBAGE-ROW"]  # 非 dict 行（O-2 注入）
        if offset in self.empty_pages_at.get(entity, set()):
            page = []  # 空页但 total 保持（O-1 并发漂移变体注入）
        return self._resp(
            {"items": page, "total": total, "limit": limit, "offset": offset}
        )

    def _apply_filter(self, rows, filt: dict | None) -> list[dict]:
        if not filt:
            return list(rows)
        out = []
        for row in rows:
            ok = True
            for field, cond in filt.items():
                if field not in row:
                    ok = False
                    break
                value = row[field]
                if isinstance(cond, dict):
                    for op, target in cond.items():
                        if not _op_match(op, value, target):
                            ok = False
                            break
                elif value != cond:
                    ok = False
                if not ok:
                    break
            if ok:
                out.append(row)
        return out

    def _apply_order(self, rows, order_by) -> list[dict]:
        if not order_by:
            return list(rows)
        keys = [order_by] if isinstance(order_by, str) else list(order_by)
        # 稳定排序：按 key 逆序应用
        result = list(rows)
        for key in reversed(keys):
            desc = key.startswith("-")
            field = key[1:] if desc else key
            result.sort(key=lambda r, f=field: (r.get(f) is None, str(r.get(f, ""))), reverse=desc)
        return result

    def _resp(self, data: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    "isError": False,
                    "structuredContent": data,
                },
            },
        )

    def _error(self, code: str, message: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": f"{code}: {message}"}],
                    "isError": True,
                    "structuredContent": {
                        "code": code,
                        "message": message,
                        "retryable": False,
                        "requires_operator_action": False,
                        "details": {},
                    },
                },
            },
        )


def _op_match(op: str, value, target) -> bool:
    """mcp-api.md §4 操作符子集（假服务）：比较按字符串（ERP 数值以字符串返回）。"""
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    if op == "gt":
        return str(value) > str(target)
    if op == "gte":
        return str(value) >= str(target)
    if op == "lt":
        return str(value) < str(target)
    if op == "lte":
        return str(value) <= str(target)
    if op == "in":
        return value in target
    if op == "like":
        return str(target) in str(value)
    if op == "between":
        lo, hi = target
        return str(lo) <= str(value) <= str(hi)
    raise AssertionError(f"unexpected op {op!r}")


def create_task(tasks: TaskService, duty: str = "R4", **overrides) -> object:
    """创建 R4/R5 只读任务（D6：is_write=False → PENDING，不持锁不排队）。"""
    base = dict(
        thread_id="thread-d17-query",
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code=duty,
        original_user_message="查询测试",
        structured_intent={"duty": duty},
        is_write=False,
    )
    base.update(overrides)
    return tasks.create_task(NewTaskSpec(**base))


@pytest.fixture
def erp() -> FakeQueryErp:
    return FakeQueryErp()


@pytest.fixture
def ctx(erp: FakeQueryErp):
    """组装查询执行器上下文：respx 假服务 + 真 MCPClient + EntityCache +
    内存任务/审计仓储。yield (erp, tasks, audit, executor, client, cache)。"""
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            cache = EntityCache(client)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            executor = QueryExecutor(
                client=client,
                cache=cache,
                tasks=tasks,
                audit=audit,
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                duty_code="R4",
            )
            yield erp, tasks, audit, executor, client, cache
        finally:
            client.close()


# ---- 分页与 total 保留（6.1 第 3 条 / 9.2 第 4 条） ----


def test_single_page_default_limit_and_total(erp, ctx):
    _, tasks, audit, executor, _, _ = ctx
    task = create_task(tasks)
    assert task.status == TaskStatus.PENDING  # 只读任务创建即 PENDING（8.3）
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders"),
    )
    assert len(page.items) == 10
    assert page.total == 10
    assert page.limit == 50  # 默认 limit 50（mcp-api.md §4）
    assert not page.has_more
    assert not page.empty
    # 单页 attempt + result（11.2 规则 2：每次实际调用一条 result）
    assert len(audit.events(task_id=task.task_id, event_type="result")) == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_page_has_more_and_total_preserved(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    page1 = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", limit=4),
    )
    assert len(page1.items) == 4
    assert page1.total == 10
    assert page1.has_more  # 第一页不是全量：显式表达已分页
    task2 = create_task(tasks)
    page3 = executor.execute(
        task_id=task2.task_id,
        spec=build_query_spec(entity="metal_orders", limit=4, offset=8),
    )
    assert len(page3.items) == 2
    assert page3.total == 10  # total 恒为过滤后全量，不随 offset 变化
    assert not page3.has_more


def test_fetch_all_paginates_and_preserves_total(erp, ctx):
    _, tasks, audit, executor, _, _ = ctx
    task = create_task(tasks)
    full = executor.fetch_all(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", limit=4),
    )
    assert full.fetched_count == 10
    assert full.total == 10
    assert full.pages == 3  # 4 + 4 + 2
    assert full.complete
    assert not full.notes
    # 每页独立 attempt（11.2 规则 3）：3 页 → 3 条 result
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len(results) == 3
    assert audit.attempts(task.operation_id)[-1].attempt_no == 3
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_fetch_all_drift_marks_incomplete_and_notes(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    erp.drift_at_call["metal_orders"] = 3  # 第 3 页前删 3 行（并发变更）
    task = create_task(tasks)
    full = executor.fetch_all(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", limit=4),
    )
    assert not full.complete  # 9.2 第 4 条：分页数据变化不掩盖限制
    assert any("数据发生变化" in n for n in full.notes)
    # 第 3 页前删 3 行：前两页已取 8 条（id 1-8），最终 total=7 → 前后不一致
    assert full.fetched_count == 8
    # 数据不完整显式说明（6.2 第 4 条口径要素由此取）
    assert full.limitations() == full.notes


def test_fetch_all_empty_page_break_notes(erp, ctx):
    """O-1：total>0 但当前 offset 返回空页（并发漂移变体）→ complete=False
    且 notes 显式说明（6.2 第 4 条：数据不完整必须显式说明，不留空 notes）。"""
    _, tasks, _, executor, _, _ = ctx
    erp.empty_pages_at["metal_orders"] = {8}  # 第 3 页（offset=8）返回空，total 保持 10
    task = create_task(tasks)
    full = executor.fetch_all(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", limit=4),
    )
    assert full.fetched_count == 8  # 前两页 8 条，第 3 页空页 break
    assert not full.complete
    assert any("返回空" in n for n in full.notes)
    assert full.limitations() == full.notes  # partial 必有说明，不留空限制区


# ---- 空结果表达（6.1 第 4 条） ----


def test_empty_result_explicit_message(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(
            entity="metal_orders", filter={"model": "NO-SUCH-MODEL"}
        ),
    )
    assert page.empty
    assert page.total == 0
    assert EMPTY_RESULT_MESSAGE == "没有符合条件的记录（查询自动排除软删除数据；未查到不代表数据从未存在）"
    assert "没有符合条件的记录" in EMPTY_RESULT_MESSAGE
    assert "从未存在" in EMPTY_RESULT_MESSAGE  # 不得把"未查到"说成"从未存在"


def test_fetch_all_empty_result(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    full = executor.fetch_all(
        task_id=task.task_id,
        spec=build_query_spec(entity="customers", filter={"region": "西北"}),
    )
    assert full.empty
    assert full.total == 0
    assert full.complete  # 空结果本身是完整结果（未查到 ≠ 数据缺失）


def test_soft_deleted_rows_excluded(erp, ctx):
    """软删除由 ERP 自动排除（参数化 SQL，mcp-api.md §4）；本地表达层
    不得把"未查到"解释成"从未存在"（6.1 第 4 条）。"""
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", fields=("id", "model")),
    )
    assert page.total == 10  # 含软删除行 99 但被排除
    assert all(r["id"] != 99 for r in page.items)


# ---- 过滤与排序（6.1/9.2：原样透传，不解析不 eval） ----


def test_filter_passed_through_verbatim(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    filt = {"status": "完成", "total_amount": {"gte": "100"}}
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", filter=filt, limit=200),
    )
    assert page.total == 10
    # wire 级断言：filter 原样透传（9.2 第 6 条：不解析不 eval）
    last = [c for c in erp.calls if c.get("entity") == "metal_orders"][-1]
    assert last["filter"] == filt
    assert last["limit"] == 200


def test_order_by_passed_through_verbatim(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", order_by="-id", limit=5),
    )
    last = [c for c in erp.calls if c.get("entity") == "metal_orders"][-1]
    assert last["order_by"] == "-id"


def test_fields_filter_applied(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", fields=("id", "status")),
    )
    assert set(page.items[0].keys()) == {"id", "status"}


def test_amount_format_preserved_from_erp(erp, ctx):
    """金额保持 ERP 返回格式（6.1 第 5 条：本地不转 float/不格式化）。"""
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    page = executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(
            entity="metal_orders", fields=("id", "total_amount", "in_date"), limit=1
        ),
    )
    assert page.items[0]["total_amount"] == "375.00"  # 原样字符串
    assert page.items[0]["in_date"].startswith("2026-")  # 日期原样


# ---- 查询前校验（9.2 第 1/2 条 + 6.1 第 2 条，fail closed） ----


def test_entity_not_whitelisted_rejected_before_network(erp, ctx):
    """实体在元数据内但不在可查询白名单 → 本地拒绝，不触网（9.2 第 2 条）。"""
    _, tasks, _, executor, _, _ = ctx
    calls_before = len(erp.calls)
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.execute(
            task_id=task.task_id,
            spec=build_query_spec(entity=NON_QUERYABLE_ENTITY),
        )
    assert exc.value.code == InternalCode.NOT_WHITELISTED.value
    assert exc.value.category == ErrorCategory.LOCAL
    assert erp.calls[calls_before:] == []  # 未发出 query 请求
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_unknown_entity_rejected(erp, ctx):
    """未知实体 → 本地拒绝、不触网。校验顺序：白名单最先（9.2 第 2 条
    本地常量拦截），未知实体在白名单内不可达时由元数据校验兜底
    （MCP_METADATA_UNKNOWN_ENTITY，9.2 第 1 条——validate_query_spec 直调
    场景已在 test_validate_query_spec_direct 覆盖）。"""
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.execute(
            task_id=task.task_id,
            spec=build_query_spec(entity="secret_table"),
        )
    assert exc.value.code == InternalCode.NOT_WHITELISTED.value  # 白名单优先
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_unknown_field_rejected(erp, ctx):
    """字段必须来自 list_entities（6.1 第 2 条 / 9.2 第 1 条）。"""
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.execute(
            task_id=task.task_id,
            spec=build_query_spec(
                entity="metal_orders", fields=("id", "password_hash")
            ),
        )
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value
    assert exc.value.details["fields"] == ["password_hash"]


def test_limit_above_max_rejected(erp, ctx):
    """limit 最大 200（9.2 第 3 条 / mcp-api.md §4），构造即本地拒绝。"""
    with pytest.raises(MCPError) as exc:
        build_query_spec(entity="metal_orders", limit=MAX_QUERY_LIMIT + 1)
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value
    assert exc.value.category == ErrorCategory.VALIDATION


def test_offset_above_max_rejected():
    with pytest.raises(MCPError) as exc:
        build_query_spec(entity="metal_orders", offset=MAX_QUERY_OFFSET + 1)
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value


def test_validate_query_spec_direct(erp, ctx):
    """validate_query_spec 独立校验（组合器/编排层直接复用）。"""
    _, _, _, _, client, cache = ctx
    ok = build_query_spec(entity="metal_orders", fields=("id", "status"))
    validate_query_spec(ok, cache, frozenset({"metal_orders"}))  # 不抛
    with pytest.raises(MCPError):
        validate_query_spec(
            build_query_spec(entity="attendance"),
            cache,
            frozenset({"metal_orders"}),
        )


# ---- 错误码原样归类（9.2 第 5 条：structuredContent.code 原样映射） ----


def test_server_error_code_passthrough(erp, ctx):
    _, tasks, audit, executor, _, _ = ctx
    erp.fail_next.append(("ORDER_NOT_FOUND", "order 404 missing"))
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.execute(
            task_id=task.task_id,
            spec=build_query_spec(entity="metal_orders"),
        )
    assert exc.value.code == "ORDER_NOT_FOUND"  # 原样，不新造错误码
    # 失败：任务 failed + result 恰 1 条（11.2 规则 2）
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len(results) == 1
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.result_status == "failed"
    assert attempt.error_code == "ORDER_NOT_FOUND"


def test_row_level_corruption_rejected(erp, ctx):
    """O-2：items 含非 dict 行 → 视为解析失败上抛（决策 g 行级扩展），
    不静默丢弃（丢弃会使 returned_count 与 total 关系失真）；任务 failed
    + result 恰 1 条。"""
    erp.corrupt_items_at["metal_orders"] = {0}
    _, tasks, audit, executor, _, _ = ctx
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.execute(
            task_id=task.task_id,
            spec=build_query_spec(entity="metal_orders"),
        )
    assert "not an object" in exc.value.message
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len(results) == 1
    assert audit.attempts(task.operation_id)[0].result_status == "failed"


def test_from_erp_row_level_corruption_direct():
    """O-2 纯函数级：from_erp 对非 dict 行直接上抛（决策 g 行级防御证据）。"""
    from administrative_agent.domains.query import QueryPage

    with pytest.raises(MCPError) as exc:
        QueryPage.from_erp({"items": [{"id": 1}, "garbage"], "total": 2}, 50, 0)
    assert "not an object" in exc.value.message
    # 合法行仍正常解析（不回归）
    page = QueryPage.from_erp(
        {"items": [{"id": 1}, {"id": 2}], "total": 2}, 50, 0
    )
    assert len(page.items) == 2 and page.total == 2


def test_fetch_all_midway_failure_marks_failed(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    erp.fail_next.append(("ERP_BUSY", "server busy"))
    task = create_task(tasks)
    with pytest.raises(MCPError) as exc:
        executor.fetch_all(
            task_id=task.task_id,
            spec=build_query_spec(entity="metal_orders", limit=4),
        )
    assert exc.value.code == "ERP_BUSY"
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


# ---- 只读任务状态与审计链（8.3 / 11.2） ----


def test_readonly_task_pending_then_running_then_succeeded(erp, ctx):
    """只读任务创建 PENDING、不持锁不排队（8.3/RV-09 §8）；执行入口
    PENDING→RUNNING 迁移后收口 succeeded。"""
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    assert task.status == TaskStatus.PENDING  # 与写任务（创建即 running）不同
    executor.execute(task_id=task.task_id, spec=build_query_spec(entity="customers"))
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_execute_rejects_terminal_task(erp, ctx):
    _, tasks, _, executor, _, _ = ctx
    task = create_task(tasks)
    executor.execute(task_id=task.task_id, spec=build_query_spec(entity="customers"))
    with pytest.raises(TaskNotRunningError):
        executor.execute(task_id=task.task_id, spec=build_query_spec(entity="customers"))


def test_audit_chain_has_attempt_and_result_events(erp, ctx):
    _, tasks, audit, executor, _, _ = ctx
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        spec=build_query_spec(entity="metal_orders", fields=("id", "model"), limit=3),
    )
    events = audit.events(task_id=task.task_id)
    types = [e.event_type for e in events]
    assert "attempt" in types and "result" in types
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.target_type == "query"
    assert attempt.target_id == "metal_orders"
    assert attempt.field_diffs["entity"] == "metal_orders"
    assert attempt.field_diffs["fields"] == ["id", "model"]
    result_events = [e for e in events if e.event_type == "result"]
    assert result_events[0].structured_payload["total"] == 10


def test_query_spec_payload_shape():
    """mcp-api.md §4 信封：可选参数缺省不携带。"""
    spec = build_query_spec(entity="metal_orders")
    assert spec.to_payload() == {"entity": "metal_orders", "limit": 50, "offset": 0}
    spec2 = build_query_spec(
        entity="metal_orders",
        fields=("id",),
        filter={"status": "完成"},
        order_by="-id",
        limit=10,
        offset=20,
    )
    assert spec2.to_payload() == {
        "entity": "metal_orders",
        "fields": ["id"],
        "filter": {"status": "完成"},
        "order_by": "-id",
        "limit": 10,
        "offset": 20,
    }
