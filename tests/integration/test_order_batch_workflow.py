"""R2/R3 订单批量确认链路集成测试（[2026-09-12 一张卡多条明细] 阶段 2/4）。

本文件补的是「一张卡多条明细」改造后的**端到端通车**验证，镜像
``tests/integration/test_attendance_workflow.py`` 尾部的「考勤确认卡端到端」
用例：走 ``tools_node`` 出确认卡 → ``confirm_node`` 批准 → 执行全链路。

覆盖（对齐派单必测项）：

- 全链路：批量创建卡（两条同客户同型号）出卡零写入 → 批准 → 逐条写 ERP
  → 任务 succeeded + 审计 attempt/result = 2N；
- 批量更新卡：出卡 → 批准 → 逐条 update_order；
- **整卡失效**：确认后任一条快照被他人改动 → 执行期整卡重建哈希失配 →
  **整批零写入** + 任务 failed（7.3：绝不部分执行一张已失效的卡）；
- **在飞旧卡兼容**：execution 不含 "items" 键（改造前的执行形状，单条
  简写路径产出）→ 仍走旧单条执行器，语义不变；
- 单条（items 单元素）委托旧函数：执行载荷不含 "items"（零回归口径 4）。

假 ERP 复用 ``tests/domains.conftest.FakeOrdersErp``（respx 假服务，D5 规则：
测试夹具，不代表 ERP 真实运行态），仅在其上补 customers/suppliers 只读查询
（名称唯一匹配与写前快照反查名称需要）。真实 MCPClient 走 respx，写调用
全部落在内存假服务里——**零 HTTP、零真实 ERP**。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import respx
from langchain_core.messages import HumanMessage

from administrative_agent import lifecycle
from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.audit.service import (
    EVENT_ATTEMPT,
    EVENT_RESULT,
    AuditService,
)
from administrative_agent.config import Settings
from administrative_agent.mcp.client import MCPClient
from administrative_agent.persistence.models import TaskStatus
from administrative_agent.tasks.service import TaskService
from tests.audit.conftest import (
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.domains.conftest import FakeOrdersErp
from tests.integration.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ERP_MCP_URL,
)
from tests.tasks.conftest import InMemoryTaskRepository

THREAD = "thread-order-batch-confirm"
CUSTOMER_ID = 11
CUSTOMER_NAME = "测试客户甲公司"
SUPPLIER_ID = 21
SUPPLIER_NAME = "测试供货商乙公司"

# 坑：agent/nodes/__init__.py re-export 了与子模块同名的节点函数，
# `from ...nodes import tools_node` 拿到的是函数不是模块——必须走 importlib
# 才能 patch confirm_node 的 interrupt（同 test_attendance_workflow.py）。
tools_node_mod = importlib.import_module("administrative_agent.agent.nodes.tools_node")
confirm_node_mod = importlib.import_module(
    "administrative_agent.agent.nodes.confirm_node"
)


class _BatchOrdersErp(FakeOrdersErp):
    """在 FakeOrdersErp 之上补只读实体（customers/suppliers/list_entities）。

    订单创建的准备阶段要按名称全表查 customers/suppliers（名称唯一匹配），
    更新阶段的展示层要按 id 反查客户/供货商名称——基类只服务 metal/powder
    订单查询，故在此补上；写路径（create_order_bundle/update_order）原样
    委派基类，保证幂等/乐观锁语义与域级用例同源。
    """

    def __init__(self) -> None:
        super().__init__()
        self.customers: list[dict] = [{
            "id": CUSTOMER_ID,
            "company_name": CUSTOMER_NAME,
            "unit_price": "120.0000",
            "price_type": "含税",
            "price_unit": "元/kg",
        }]
        self.suppliers: list[dict] = [{
            "id": SUPPLIER_ID,
            "name": SUPPLIER_NAME,
        }]

    def _dispatch(self, name: str, args: dict) -> Any:
        if name == "list_entities":
            return self._ok({"entities": []})
        if name == "query" and args.get("entity") in ("customers", "suppliers"):
            entity = args["entity"]
            self.calls.append(args)
            rows = self.customers if entity == "customers" else self.suppliers
            filt = args.get("filter") or {}
            items = [
                dict(r) for r in rows
                if all(r.get(k) == v for k, v in filt.items())
            ]
            fields = args.get("fields")
            if fields:
                items = [
                    {k: v for k, v in it.items() if k in fields} for it in items
                ]
            return self._ok(
                {"items": items, "total": len(items), "entity": entity}
            )
        return super()._dispatch(name, args)

    # ---- 断言辅助 ----

    @property
    def create_calls(self) -> list[dict]:
        return [c for c in self.calls if "idempotency_key" in c]

    @property
    def update_calls(self) -> list[dict]:
        return [c for c in self.calls if "expected_updated_at" in c]

    def seed_metal(self, order_id: int, **overrides: Any) -> int:
        payload = dict(
            customer_id=CUSTOMER_ID, model="MK-01", unit_price="120.0000",
            price_type="含税", status="未完成", weight_per_unit="0.0000",
            weight_unit="kg", quantity="2.00", quantity_unit="套",
            in_date="2026-08-20", out_date=None, notes=None,
        )
        payload.update(overrides)
        return self.seed_update_order(order_id, payload)


@dataclass
class OrderBatchHarness:
    """订单确认链路装配（respx 假服务 + 真 MCPClient + 内存任务/审计）。"""

    runtime: RuntimeBag
    erp: _BatchOrdersErp
    tasks: TaskService
    audit: AuditService
    client: MCPClient
    sink_events: list[tuple[str, dict]] = field(default_factory=list)

    def prepare(self, tool_name: str, tool_arguments: dict) -> dict:
        """走 tools_node 出卡，返回 State.pending_confirmation。"""
        state = {
            "messages": [HumanMessage(content="批量下单")],
            "pending_action": {
                "tool_name": tool_name,
                "tool_arguments": tool_arguments,
            },
        }
        out = tools_node_mod.tools_node(state, self.runtime)
        assert out["current_step"] == "tool_prepared", out
        return out["pending_confirmation"]

    def confirm(self, pc: dict, *, approved: bool = True) -> dict:
        """走 confirm_node 批准（payload_hash = 卡片承诺值）→ 执行。"""
        decision = {"approved": approved, "payload_hash": pc["payload_hash"]}
        with patch.object(confirm_node_mod, "interrupt", return_value=decision):
            return confirm_node_mod.confirm_node(
                {"pending_confirmation": pc}, self.runtime
            )


@pytest.fixture
def bctx() -> OrderBatchHarness:
    """订单批量确认链路夹具（零 HTTP；写调用全落在内存假服务）。"""
    lifecycle.reset()
    erp = _BatchOrdersErp()
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(
                InMemoryAuditRepository(), InMemoryAttemptRepository()
            )
            events: list[tuple[str, dict]] = []
            runtime = RuntimeBag(
                tasks=tasks,
                audit=audit,
                context=AgentContext(
                    user_id=ACTOR_ACCOUNT,
                    user_role=ACTOR_ROLE,
                    scope=frozenset(
                        {"orders:create_bundle", "orders:update", "erp:query"}
                    ),
                    thread_id=THREAD,
                ),
                mcp_client=client,
                tools=dict(TOOL_REGISTRY),
                sink=lambda etype, payload: events.append((etype, payload)),
            )
            yield OrderBatchHarness(
                runtime=runtime, erp=erp, tasks=tasks, audit=audit,
                client=client, sink_events=events,
            )
        finally:
            client.close()
            lifecycle.reset()


# ---------------------------------------------------------------------------
# 批量创建：出卡零写入 → 批准 → 逐条写 ERP
# ---------------------------------------------------------------------------


def test_batch_create_full_chain_writes_both_rows(bctx) -> None:
    """全链路：两条同客户同型号明细 → 一张卡两行 → 批准后两条都创建。

    - 出卡零写入（prepare 只发只读 query）；
    - 卡片为批量形状（row_header_label=明细），两条同业务标识**不折叠**；
    - 任务 waiting_confirmation → confirmed_payload_hash 落库（7.3 依据）；
    - 批准后逐条 create_order_bundle，任务 succeeded，审计 attempt/result = 4。
    """
    pc = bctx.prepare("order_create", {
        "order_type": "metal",
        "items": [
            {"customer_name": CUSTOMER_NAME, "model": "MK-01",
             "unit_price": 120.0, "quantity": 2},
            {"customer_name": CUSTOMER_NAME, "model": "MK-01",
             "unit_price": 120.0, "quantity": 5},
        ],
    })

    # 出卡：批量卡两行、外层键唯一（序号保证）、零写调用
    card = pc["card"]
    assert card["row_header_label"] == "明细"
    assert card["row_quantifier"] == "条"
    assert set(card["fields_to_write"]) == {
        f"1. 客户 {CUSTOMER_NAME} / MK-01",
        f"2. 客户 {CUSTOMER_NAME} / MK-01",
    }
    assert bctx.erp.create_calls == []  # 出卡前零写入

    record = bctx.tasks.get_task(pc["task_id"])
    assert record.status == TaskStatus.WAITING_CONFIRMATION
    assert record.confirmed_payload_hash == pc["payload_hash"]

    out = bctx.confirm(pc)

    assert out["current_step"] == "tool_done"
    calls = bctx.erp.create_calls
    assert len(calls) == 2  # 逐条写，一条一次
    assert {c["order_type"] for c in calls} == {"metal"}
    assert sorted(c["order"]["quantity"] for c in calls) == [2, 5]
    assert len({c["idempotency_key"] for c in calls}) == 2  # 逐条独立幂等键
    assert bctx.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED
    events = bctx.audit.events(task_id=pc["task_id"])
    assert sum(1 for e in events if e.event_type == EVENT_ATTEMPT) == 2
    assert sum(1 for e in events if e.event_type == EVENT_RESULT) == 2


def test_batch_create_powder_two_rows_full_chain(bctx) -> None:
    """塑粉批量创建同样一条卡多条明细（供货商/色号为业务标识）。"""
    pc = bctx.prepare("order_create", {
        "order_type": "powder",
        "items": [
            {"supplier_name": SUPPLIER_NAME, "color_code": "R-01",
             "total_weight": 25.0, "price_per_kg": 28.5},
            {"supplier_name": SUPPLIER_NAME, "color_code": "R-02",
             "total_weight": 10.0, "price_per_kg": 30.0},
        ],
    })
    assert set(pc["card"]["fields_to_write"]) == {
        f"1. 供货商 {SUPPLIER_NAME} / R-01",
        f"2. 供货商 {SUPPLIER_NAME} / R-02",
    }
    assert bctx.erp.create_calls == []

    bctx.confirm(pc)

    calls = bctx.erp.create_calls
    assert len(calls) == 2
    assert {c["order_type"] for c in calls} == {"powder"}
    assert sorted(c["order"]["color_code"] for c in calls) == ["R-01", "R-02"]


# ---------------------------------------------------------------------------
# 批量更新：出卡 → 批准 → 逐条 update；整卡失效 → 零写入
# ---------------------------------------------------------------------------


def test_batch_update_full_chain_applies_both(bctx) -> None:
    """全链路：两条更新明细 → 一张卡两行 → 批准后两条 update_order。"""

    bctx.erp.seed_metal(10)
    bctx.erp.seed_metal(20, model="MK-02")
    pc = bctx.prepare("order_update", {
        "order_type": "metal",
        "items": [
            {"order_id": 10, "notes": "批次备注一"},
            {"order_id": 20, "notes": "批次备注二"},
        ],
    })
    assert len(pc["card"]["fields_to_write"]) == 2
    assert bctx.erp.update_calls == []  # 出卡前零写入

    out = bctx.confirm(pc)

    assert out["current_step"] == "tool_done"
    calls = bctx.erp.update_calls
    assert sorted(c["order_id"] for c in calls) == [10, 20]
    assert {c["changes"]["notes"] for c in calls} == {"批次备注一", "批次备注二"}
    assert bctx.erp.orders[10]["notes"] == "批次备注一"
    assert bctx.erp.orders[20]["notes"] == "批次备注二"
    assert bctx.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_batch_update_whole_card_invalidation_writes_nothing(bctx) -> None:
    """整卡失效：确认后任一条被他人改动 → 整批零写入 + 任务 failed。

    7.3：一张已失效的确认卡绝不部分执行；比对是**整卡**的，不是逐条的。
    """

    bctx.erp.seed_metal(10)
    bctx.erp.seed_metal(20, model="MK-02")
    pc = bctx.prepare("order_update", {
        "order_type": "metal",
        "items": [
            {"order_id": 10, "notes": "批次备注一"},
            {"order_id": 20, "notes": "批次备注二"},
        ],
    })
    # 确认后、执行前：他人改动第 2 条（updated_at 推进 → 整卡哈希失配）
    bctx.erp.apply_other_writer_update(20, {"notes": "他人改动"})

    out = bctx.confirm(pc)

    assert out["current_step"] == "tool_done"
    assert bctx.erp.update_calls == []  # 整批零写入
    assert bctx.erp.orders[10]["notes"] is None  # 第 1 条也未被写
    assert bctx.erp.orders[20]["notes"] == "他人改动"  # 只保留他人改动
    assert bctx.tasks.get_task(pc["task_id"]).status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# 兼容红线：在飞旧卡（execution 无 "items"）仍走旧执行器
# ---------------------------------------------------------------------------


def test_single_item_card_delegates_to_legacy_shape(bctx) -> None:
    """items 单元素 == 顶层单条简写：执行载荷不含 items（零回归口径 4）。"""
    pc = bctx.prepare("order_create", {
        "order_type": "metal",
        "items": [{"customer_name": CUSTOMER_NAME, "model": "MK-01",
                   "unit_price": 120.0, "quantity": 2}],
    })
    assert "items" not in pc["execution"]  # 委托旧单条形状
    assert "draft" in pc["execution"]


def test_old_shape_create_card_still_executes(bctx) -> None:
    """在飞旧卡（execution 无 items 的单条形状）仍逐字节按旧执行器执行。"""

    pc = bctx.prepare("order_create", {
        "order_type": "metal", "customer_name": CUSTOMER_NAME,
        "model": "MK-01", "unit_price": 120.0, "quantity": 2,
    })
    assert "items" not in pc["execution"]

    out = bctx.confirm(pc)

    assert out["current_step"] == "tool_done"
    assert len(bctx.erp.create_calls) == 1
    assert bctx.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_old_shape_update_card_still_executes(bctx) -> None:
    """在飞旧更新卡（单条 execution）仍按旧单条执行器执行。"""

    bctx.erp.seed_metal(10)
    pc = bctx.prepare("order_update", {
        "order_type": "metal", "order_id": 10, "notes": "旧卡单条",
    })
    assert "items" not in pc["execution"]

    out = bctx.confirm(pc)

    assert out["current_step"] == "tool_done"
    assert len(bctx.erp.update_calls) == 1
    assert bctx.erp.orders[10]["notes"] == "旧卡单条"
    assert bctx.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED
