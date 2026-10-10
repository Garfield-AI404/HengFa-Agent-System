"""D12 考勤工作流集成测试共享夹具（4.3/14.3：respx 假服务 + 内存仓储）。

在本 conftest 顶层先于任何 administrative_agent 导入设置测试 ERP 环境
变量（与 tests/contract/conftest.py 同模式：Settings() 新实例读到确定性
测试值，覆盖 .env 中的真实值）。

FakeAttendanceErp 为 respx 假服务（D5 规则：测试夹具，不代表 ERP 真实
运行态；材料中明确标注来源）：内存记录 + 唯一约束（4.4 防重第二件）+
失败注入（RV-18 中途失败/网络结果未知/ERP 丢写等场景）。

D24 增补：14.3 场景套件（订单/更新/对账执行）复用 tests/domains 的
FakeOrdersErp 与 MultiFieldQueryErp（同为 respx 假服务），并新增
octx/uctx/rctx14 三个上下文夹具（见文件尾部）。
"""

from __future__ import annotations

import json
import os

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

from administrative_agent.agent.prompts.policies import policy_for  # noqa: E402
from administrative_agent.audit.service import AuditService  # noqa: E402
from administrative_agent.business.attendance_rules import (  # noqa: E402
    AttendanceException,
    AttendancePlan,
    plan_attendance,
)
from administrative_agent.business.matching import EmployeeCandidate  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.attendance import AttendanceExecutor  # noqa: E402
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.mcp.metadata import EntityCache  # noqa: E402
from administrative_agent.tasks.records import NewTaskSpec  # noqa: E402
from administrative_agent.tasks.service import TaskService  # noqa: E402
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

ERP_BASE = "http://erp-mcp.test"
ERP_MCP_URL = f"{ERP_BASE}/mcp"

#: attendance 实体元数据（mcp-api.md §3 格式；字段能力与 RV-07 §8.1 真实运行态一致）
ATTENDANCE_ENTITY_META: dict = {
    "name": "attendance",
    "label": "考勤",
    "description": "每日考勤记录",
    "readable": True,
    "creatable": True,
    "updatable": True,
    "deletable": False,
    "read_only": False,
    "business_operations": [],
    "default_order": "-id",
    "fields": {
        "employee_id": {
            "type": "int", "required": True, "nullable": False,
            "writable_on_create": True, "writable_on_update": True,
            "system_managed": False,
        },
        "att_date": {
            "type": "date", "required": True,
            "writable_on_create": True, "writable_on_update": True,
            "system_managed": False,
        },
        "leave_minutes": {
            "type": "int", "default": 0,
            "writable_on_create": True, "writable_on_update": True,
        },
        "overtime_minutes": {
            "type": "int", "default": 0,
            "writable_on_create": True, "writable_on_update": True,
        },
        "id": {
            "type": "int",
            "writable_on_create": False, "writable_on_update": False,
            "system_managed": True,
        },
    },
}


class FakeAttendanceErp:
    """respx 假服务（D5 规则：测试夹具，不代表 ERP 真实运行态）。

    - 内存记录 + (att_date, employee_id) 唯一约束（4.4 防重第二件）；
    - 失败注入：fail_create_employee（结构化错误拒绝）、
      fail_network_create_employee（已写入但响应丢失——网络结果未知）、
      lose_write_employee（返回成功但不落库——ERP 丢写）；
    - omit_fields：构造时从元数据移除字段（9.5 契约错误场景）。
    """

    def __init__(self, *, omit_fields: tuple[str, ...] = ()) -> None:
        self.records: dict[int, dict] = {}
        self.next_id = 100
        self.unique_index: dict[tuple[str, int], int] = {}
        self.created_calls: list[dict] = []
        self.updated_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.fail_create_employee: set[int] = set()
        self.fail_query_employee: set[int] = set()
        self.fail_network_create_employee: set[int] = set()
        self.lose_write_employee: set[int] = set()
        self.entity_meta = json.loads(json.dumps(ATTENDANCE_ENTITY_META))
        if omit_fields:
            fields = dict(self.entity_meta["fields"])
            for f in omit_fields:
                fields.pop(f, None)
            self.entity_meta["fields"] = fields

    def seed(self, record: dict) -> None:
        """预置当天已有记录（增量场景）。"""
        rid = record.get("id") or self.next_id
        if "id" not in record:
            self.next_id += 1
        self.records[rid] = dict(record, id=rid)
        self.unique_index[(record["att_date"], record["employee_id"])] = rid

    # ---- respx 路由 ----

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return self._resp(
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-erp", "version": "0.0.0"},
                }
            )
        assert body["method"] == "tools/call"
        params = body["params"]
        return self._dispatch(params["name"], params.get("arguments") or {})

    def _dispatch(self, name: str, args: dict) -> httpx.Response:
        if name == "list_entities":
            return self._ok({"entities": [self.entity_meta]})
        if name == "query":
            self.query_calls.append(args)
            filter = args["filter"] or {}
            eid = filter.get("employee_id")
            if isinstance(eid, int) and eid in self.fail_query_employee:
                return self._error("MCP_SERVER_ERROR", "query boom")
            items = [
                dict(r)
                for r in sorted(self.records.values(), key=lambda r: r["id"])
                if all(r.get(k) == v for k, v in filter.items())
            ]
            return self._ok(
                {
                    "items": items,
                    "total": len(items),
                    "limit": args.get("limit", 50),
                    "offset": 0,
                }
            )
        if name == "create":
            data = args["data"]
            key = (data["att_date"], data["employee_id"])
            if key in self.unique_index:
                return self._error("VALIDATION_ERROR", "duplicate attendance record")
            self.created_calls.append(args)
            eid = data["employee_id"]
            if eid in self.fail_create_employee:
                return self._error("BUSINESS_ERROR", "erp create rejected")
            if eid in self.fail_network_create_employee:
                # 已写入但响应丢失（网络结果未知）：恢复时写前 query 应看到记录
                self._insert(data)
                raise httpx.ConnectError("connection reset (fake erp)")
            if eid in self.lose_write_employee:
                # 返回成功但不落库（ERP 丢写）：最终验证将发现记录缺失
                return self._ok(
                    {
                        "status": "created",
                        "record": {"id": 99999, **data},
                        "operation_id": args.get("operation_id"),
                        "trace_id": args.get("trace_id"),
                    }
                )
            rid = self._insert(data)
            return self._ok(
                {
                    "status": "created",
                    "record": self.records[rid],
                    "operation_id": args.get("operation_id"),
                    "trace_id": args.get("trace_id"),
                }
            )
        if name == "update":
            data = args["data"]
            rid = args["id"]
            if rid not in self.records:
                return self._error("ORDER_NOT_FOUND", "record not found")
            self.updated_calls.append(args)
            self.records[rid].update(data)
            return self._ok(
                {
                    "status": "updated",
                    "record": self.records[rid],
                    "operation_id": args.get("operation_id"),
                    "trace_id": args.get("trace_id"),
                }
            )
        raise AssertionError(f"unexpected tool: {name}")

    # ---- 内部 ----

    def _insert(self, data: dict) -> int:
        rid = self.next_id
        self.next_id += 1
        self.records[rid] = {"id": rid, **data}
        self.unique_index[(data["att_date"], data["employee_id"])] = rid
        return rid

    def _resp(self, result: dict) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": result}
        )

    def _ok(self, data: dict) -> httpx.Response:
        return self._resp(
            {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "isError": False,
                "structuredContent": data,
            }
        )

    def _error(self, code: str, message: str) -> httpx.Response:
        return self._resp(
            {
                "content": [{"type": "text", "text": f"{code}: {message}"}],
                "isError": True,
                "structuredContent": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "requires_operator_action": False,
                    "details": {},
                },
            }
        )


#: 有效员工集合（4.3 顺序 1 query 结果；与 14.3 场景 1/2 人物一致）
EMPLOYEES: list[EmployeeCandidate] = [
    EmployeeCandidate(1, "张三"),
    EmployeeCandidate(2, "李四"),
    EmployeeCandidate(3, "王五"),
    EmployeeCandidate(4, "赵六"),
    EmployeeCandidate(5, "钱七"),
]
EMPLOYEE_NAMES = [e.name for e in EMPLOYEES]
ATTENDANCE_DATE = "2026-08-21"
TRACE_ID = "trace-d12-test-000001"
ACTOR_ACCOUNT = "manager01"
ACTOR_ROLE = "manager"


def scenario1_plan() -> AttendancePlan:
    """14.3 场景 1：张三请假 2h、李四加班 3h、其余员工正常出勤。"""
    return plan_attendance(
        ATTENDANCE_DATE,
        EMPLOYEE_NAMES,
        [
            AttendanceException("张三", leave_hours=2),
            AttendanceException("李四", overtime_hours=3),
        ],
    )


def create_task(tasks: TaskService, **overrides) -> object:
    """创建 R1 写任务（D6：is_write=True 自动取串行锁并进入 running）。"""
    base = dict(
        thread_id="thread-mgr-d12",
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code="R1",
        original_user_message="记录今天的考勤",
        structured_intent={"duty": "R1", "date": ATTENDANCE_DATE},
        is_write=True,
    )
    base.update(overrides)
    return tasks.create_task(NewTaskSpec(**base))


@pytest.fixture
def erp() -> FakeAttendanceErp:
    return FakeAttendanceErp()


@pytest.fixture
def ctx(erp: FakeAttendanceErp, request):
    """组装执行器上下文：respx 假服务 + 真 MCPClient + 内存仓储。

    yield (erp, tasks, audit, executor, metadata)；resp 拦截随 fixture
    作用域生效（每次测试独立的假服务实例）。request.param 可传
    retry_limit（默认 3，达上限演示用）——13.2 每员工最多尝试次数。
    """
    retry_limit = getattr(request, "param", 3)
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            metadata = EntityCache(client, ttl_seconds=3600)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            executor = AttendanceExecutor(
                client=client,
                metadata=metadata,
                tasks=tasks,
                audit=audit,
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                policy=policy_for("R1.attendance.daily"),
                retry_limit=retry_limit,
            )
            yield erp, tasks, audit, executor, metadata
        finally:
            client.close()


# ---------------------------------------------------------------------------
# D24 14.3 场景套件补充夹具（订单创建/更新/对账执行；复用 tests/domains
# 假服务 FakeOrdersErp / MultiFieldQueryErp——D5 规则：测试夹具）
# ---------------------------------------------------------------------------

from administrative_agent.agent.prompts.policies import policy_for  # noqa: E402
from administrative_agent.domains.orders import (  # noqa: E402
    MetalOrderExecutor,
    OrderUpdateExecutor,
)
from administrative_agent.domains.query import QueryExecutor  # noqa: E402
from administrative_agent.domains.reconciliation import (  # noqa: E402
    ReconciliationExecuteExecutor,
)
from administrative_agent.domains.reviews import ReviewService  # noqa: E402
from tests.domains.conftest import FakeOrdersErp  # noqa: E402
from tests.domains.test_mapping_service import InMemoryMappingStore  # noqa: E402
from tests.domains.test_reconciliation_execute import (  # noqa: E402
    MultiFieldQueryErp,
    published_service,
)
from tests.domains.test_reviews import InMemoryReviewRepository  # noqa: E402


def _orders_context(erp, request, *, metal_executor=True):
    """组装订单场景上下文：respx 假服务 + 真 MCPClient + 内存仓储。

    yield (erp, tasks, audit, executor, client)。executor 为
    MetalOrderExecutor（metal_executor=True）或 None（场景自建）；
    request.param 可传 retry_limit（默认 3）。
    """
    retry_limit = getattr(request, "param", 3)
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            if metal_executor:
                executor = MetalOrderExecutor(
                    client=client,
                    tasks=tasks,
                    audit=audit,
                    trace_id=TRACE_ID,
                    actor_account=ACTOR_ACCOUNT,
                    actor_role=ACTOR_ROLE,
                    policy=policy_for("R2.order.create.metal"),
                    retry_limit=retry_limit,
                )
            else:
                executor = None
            yield erp, tasks, audit, executor, client
        finally:
            client.close()


@pytest.fixture
def oerp() -> FakeOrdersErp:
    """R2 订单场景假服务（14.3 场景 3-8/11）。"""
    return FakeOrdersErp()


@pytest.fixture
def octx(oerp: FakeOrdersErp, request):
    """R2 钣金/塑粉创建场景上下文（MetalOrderExecutor，policy 由场景自建覆盖）。"""
    yield from _orders_context(oerp, request, metal_executor=True)


@pytest.fixture
def uctx(oerp: FakeOrdersErp, request):
    """R3 更新场景上下文（自建 OrderUpdateExecutor）。"""
    yield from _orders_context(oerp, request, metal_executor=False)


@pytest.fixture
def rctx14():
    """R6 对账执行场景上下文（D21 同构：复核门禁 + 映射版本检查）。

    yield (erp, tasks, audit, reviews, executor, mappings)；多字段查询
    假服务 MultiFieldQueryErp + 预置零重量订单 id=11（场景 9/10 素材）。
    """
    erp = MultiFieldQueryErp()
    erp.seed_update_order(
        11,
        {
            "customer_id": 101, "model": "M-ZW", "status": "未完成",
            "unit_price": "12.50", "quantity": "10", "weight_per_unit": "0",
            "weight_unit": "kg", "price_type": "含税", "in_date": "2026-06-20",
            "out_date": None, "notes": "",
        },
    )
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            cache = EntityCache(client)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            query_exec = QueryExecutor(
                client=client, cache=cache, tasks=tasks, audit=audit,
                trace_id=TRACE_ID, actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE, duty_code="R6",
            )
            mappings = published_service()
            reviews = ReviewService(
                review_repo=InMemoryReviewRepository(), tasks=tasks, audit=audit
            )
            executor = ReconciliationExecuteExecutor(
                client=client, mapping_service=mappings,
                query_executor=query_exec, tasks=tasks, audit=audit,
                trace_id=TRACE_ID, actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE, policy=policy_for("R6.reconciliation.execute"),
                key_factory=lambda: "d24-scenario-key-000001",
            )
            yield erp, tasks, audit, reviews, executor, mappings
        finally:
            client.close()


def order_task(tasks: TaskService, *, duty: str = "R2", **overrides) -> object:
    """订单场景任务（D6：is_write=True 自动取串行锁并进入 running）。"""
    base = dict(
        thread_id="thread-mgr-d24",
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code=duty,
        original_user_message="创建订单",
        structured_intent={"duty": duty},
        is_write=True,
    )
    base.update(overrides)
    return tasks.create_task(NewTaskSpec(**base))
