"""[2026-09-12 一张卡多条明细 阶段 3/5] 收支建/改批量卡端到端集成测试。

对齐 ``tests/integration/test_attendance_workflow.py`` 尾部「确认卡端到端」
组的风格与用例命名：走 tools_node → 出批量确认卡 → confirm_node 批准 →
执行全链路，ERP 由内存假客户端承担（零 HTTP、零真实 ERP）。

覆盖的硬约束（计划 §四/§五）：
- 全链路 卡片（出卡前零写）→ 确认 → 批量执行；
- 部分失败 → awaiting_recovery 且任务保持 running（不假报成功）；
- 恢复只补未完成项：命中已成功 op_id 的条目 skipped、零写调用；
- ``finance_create`` **写前签名预查命中即 skipped、零写调用**（无幂等键的
  防重核心）；
- ``finance_update`` **整卡失效零写**：任一条快照变化 → 整批一条不写 +
  任务失败 FINANCE_UPDATE_CONFIRMATION_INVALID（对比：逐条比对会让未变条目
  照写——本文件即该红线的定点突变守护）；
- 旧形状在飞卡仍可执行（execution 无 items → 旧单条执行器，逐字节语义）；
- 审计条数 = 2N（仅实际发生写调用时）。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from administrative_agent import lifecycle
from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.finance import (
    FinanceCreateArgs,
    FinanceUpdateArgs,
    execute_finance_create,
    execute_finance_update,
    prepare_finance_create,
    prepare_finance_update,
)
from administrative_agent.audit.service import AuditService
from administrative_agent.domains.batch import ERROR_PARTIAL
from administrative_agent.domains.finance import (
    ERROR_UPDATE_CONFIRMATION_INVALID,
    FINANCE_SOURCE_NOTE,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskService
from tests.api.conftest import FakeMCPClient
from tests.audit.conftest import (
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.integration.conftest import ACTOR_ACCOUNT, ACTOR_ROLE
from tests.tasks.conftest import InMemoryTaskRepository

THREAD = "thread-finance-batch-confirm"
WRITE_TOOLS: tuple[str, ...] = ("create", "update")

# 坑同 attendance：nodes/__init__.py re-export 同名节点函数，必须走 importlib
tools_node_mod = importlib.import_module("administrative_agent.agent.nodes.tools_node")
confirm_node_mod = importlib.import_module(
    "administrative_agent.agent.nodes.confirm_node"
)

EXPENSE = "支出"
INCOME = "收入"

CATEGORIES: list[dict] = [
    {"id": 1, "tx_type": EXPENSE, "category_name": "运费"},
    {"id": 2, "tx_type": EXPENSE, "category_name": "房租"},
    {"id": 3, "tx_type": INCOME, "category_name": "回款"},
]


class FakeFinanceMCPClient(FakeMCPClient):
    """收支内存假 ERP：query/create/update 具备真实读写语义。

    基类（tests/api/conftest.py）的 query 忽略 filter、写工具只记录不落库；
    收支链路依赖真实结果，故补语义：
    - query(transactions/transaction_categories/customers/suppliers) 按
      filter 等值回读 + fields 裁剪；
    - create(transactions) → 写内存表并回传 record.id（供写后验证/回填）；
    - update(transactions) → 落库并回传最新 record。
    读/写分界由 WRITE_TOOLS 判定，测试据此断言写调用次数与参数。
    """

    def __init__(self, *, categories: list[dict] | None = None) -> None:
        super().__init__()
        self.transactions: dict[int, dict] = {}
        self._next_id = 7000
        self.categories = list(categories or CATEGORIES)
        self.customers: list[dict] = []
        self.suppliers: list[dict] = []
        #: 命中的 create 载荷子集 → 返回 ERP 结构化拒绝（部分失败注入）
        self.fail_create_matching: list[dict] = []
        #: 第 N 次 create 调用（1 起，含所有 create）→ 返回 ERP 结构化拒绝
        #: （不落库），用于对**同内容重复明细**做「只让其中一条失败」的注入。
        self.fail_create_call_numbers: set[int] = set()
        #: 第 N 次 create 调用 → **真实落库后**返回 NETWORK 结果未知
        #: （模拟「写已发生但响应丢失」，用于恢复期预查识别已写入记录）。
        self.unknown_create_call_numbers: set[int] = set()
        self._create_call_no = 0

    # ---- 断言辅助 ----

    @property
    def write_calls(self) -> list[dict]:
        return [c for c in self.calls if c["tool"] in WRITE_TOOLS]

    def seed_transaction(self, record: dict) -> int:
        rid = int(record.get("id") or self._next_id)
        if "id" not in record:
            self._next_id += 1
        else:
            self._next_id = max(self._next_id, rid + 1)
        self.transactions[rid] = {"id": rid, **record}
        return rid

    # ---- 调用分派 ----

    def call_tool(
        self,
        tool_name: str,
        arguments: dict | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        args = dict(arguments or {})
        self.calls.append(
            {"tool": tool_name, "arguments": args, "operation_id": operation_id}
        )
        if tool_name == "query":
            return self._query(args)
        if tool_name == "create":
            return self._create(args)
        if tool_name == "update":
            return self._update(args)
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": self.ENTITIES})
        return ToolResult(ok=True, data={})

    def _rows(self, entity: str) -> list[dict]:
        if entity == "transactions":
            return [dict(r) for r in self.transactions.values()]
        if entity == "transaction_categories":
            return [dict(r) for r in self.categories]
        if entity == "customers":
            return [dict(r) for r in self.customers]
        if entity == "suppliers":
            return [dict(r) for r in self.suppliers]
        return []

    def _query(self, args: dict) -> ToolResult:
        rows = self._rows(args.get("entity", ""))
        filt = args.get("filter") or {}
        items = [
            dict(r) for r in rows if all(r.get(k) == v for k, v in filt.items())
        ]
        fields = args.get("fields")
        if fields:
            items = [{k: it.get(k) for k in fields} for it in items]
        return ToolResult(ok=True, data={"items": items, "total": len(items)})

    def _create(self, args: dict) -> ToolResult:
        data = dict(args.get("data") or {})
        self._create_call_no += 1
        call_no = self._create_call_no
        if call_no in self.fail_create_call_numbers:
            return ToolResult(
                ok=False,
                data={},
                error=MCPError(
                    code="BUSINESS_ERROR",
                    category=ErrorCategory.VALIDATION,
                    message="erp create rejected (fake, call-number)",
                ),
            )
        for pattern in self.fail_create_matching:
            if all(data.get(k) == v for k, v in pattern.items()):
                return ToolResult(
                    ok=False,
                    data={},
                    error=MCPError(
                        code="BUSINESS_ERROR",
                        category=ErrorCategory.VALIDATION,
                        message="erp create rejected (fake)",
                    ),
                )
        rid = self._next_id
        self._next_id += 1
        record = {"id": rid, **data, "updated_at": "2026-09-12T00:00:00"}
        self.transactions[rid] = record
        if call_no in self.unknown_create_call_numbers:
            # 写已发生但响应丢失：结果未知（NETWORK）
            return ToolResult(
                ok=False,
                data={},
                error=MCPError(
                    code="NETWORK_ERROR",
                    category=ErrorCategory.NETWORK,
                    retryable=True,
                    message="erp create response lost (fake)",
                ),
            )
        return ToolResult(
            ok=True, data={"status": "created", "record": dict(record)}
        )

    def _update(self, args: dict) -> ToolResult:
        rid = args.get("id")
        if rid not in self.transactions:
            return ToolResult(
                ok=False,
                data={},
                error=MCPError(
                    code="RECORD_NOT_FOUND",
                    category=ErrorCategory.VALIDATION,
                    message=f"transaction {rid} not found",
                ),
            )
        self.transactions[rid].update(args.get("data") or {})
        return ToolResult(
            ok=True,
            data={"status": "updated", "record": dict(self.transactions[rid])},
        )


@dataclass
class FinanceHarness:
    """收支批量确认链路装配（假客户端 + 内存任务/审计 + RuntimeBag）。"""

    runtime: RuntimeBag
    client: FakeFinanceMCPClient
    tasks: TaskService
    audit: AuditService

    def tool_state(
        self, tool_name: str, tool_arguments: dict, task_id: str | None = None
    ) -> dict:
        state: dict[str, Any] = {
            "messages": [HumanMessage(content="处理这笔收支")],
            "pending_action": {
                "tool_name": tool_name,
                "tool_arguments": tool_arguments,
            },
        }
        if task_id is not None:
            state["task_id"] = task_id
        return state

    def prepare(
        self, tool_name: str, tool_arguments: dict, task_id: str | None = None
    ) -> dict:
        """走 tools_node 出卡，返回完整 State 输出（含 pending_confirmation）。"""
        return tools_node_mod.tools_node(
            self.tool_state(tool_name, tool_arguments, task_id), self.runtime
        )

    def confirm(self, pc: dict) -> dict:
        """批准确认卡并执行（interrupt 打桩返回 approved + 正确 hash）。"""
        with patch.object(
            confirm_node_mod,
            "interrupt",
            return_value={"approved": True, "payload_hash": pc["payload_hash"]},
        ):
            return confirm_node_mod.confirm_node(
                {"pending_confirmation": pc}, self.runtime
            )

    def summary_of(self, out: dict) -> dict:
        return json.loads(out["messages"][0].content)


def _build_harness(client: FakeFinanceMCPClient | None = None) -> FinanceHarness:
    client = client or FakeFinanceMCPClient()
    tasks = TaskService(InMemoryTaskRepository())
    audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
    return FinanceHarness(
        runtime=RuntimeBag(
            tasks=tasks,
            audit=audit,
            context=AgentContext(
                user_id=ACTOR_ACCOUNT,
                user_role=ACTOR_ROLE,
                scope=frozenset({"finance:create", "finance:update", "erp:query"}),
                thread_id=THREAD,
            ),
            mcp_client=client,
            tools=dict(TOOL_REGISTRY),
        ),
        client=client,
        tasks=tasks,
        audit=audit,
    )


@pytest.fixture
def h() -> FinanceHarness:
    lifecycle.reset()
    try:
        yield _build_harness()
    finally:
        lifecycle.reset()


def _create_items() -> list[dict]:
    return [
        {"tx_type": EXPENSE, "category": "运费", "amount": "100",
         "tx_date": "2026-09-10"},
        {"tx_type": EXPENSE, "category": "房租", "amount": "200",
         "tx_date": "2026-09-11"},
    ]


def _seed_tx(rid: int, *, amount: str, category: str = "运费",
             tx_date: str = "2026-09-01", updated_at: str = "T0") -> dict:
    return {
        "id": rid, "tx_type": EXPENSE, "category": category, "project": None,
        "amount": amount, "tx_date": tx_date, "notes": None,
        "customer_id": None, "supplier_id": None, "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# 阶段 3：finance_create 批量
# ---------------------------------------------------------------------------


def test_create_batch_full_chain_no_write_before_confirm(h) -> None:
    """全链路：出卡前零写入 → 确认后逐条 create → 任务 succeeded；审计 2N。"""
    out = h.prepare("finance_create", {"items": _create_items()})

    assert out["current_step"] == "tool_prepared"
    pc = out["pending_confirmation"]
    card = pc["card"]
    assert card["target"] == "批量创建 2 条收支记录"
    assert card["row_header_label"] == "明细"
    # 展示外层键带序号（同内容不折叠）——两条明细如实上卡
    assert list(card["fields_to_write"]) == ["1. 支出·运费（2026-09-10）",
                                             "2. 支出·房租（2026-09-11）"]
    record = h.tasks.get_task(pc["task_id"])
    assert record.status == TaskStatus.WAITING_CONFIRMATION
    assert record.confirmed_payload_hash == pc["payload_hash"]
    # 出卡阶段零写调用
    assert h.client.write_calls == []

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert out2["current_step"] == "tool_done"
    assert summary["counts"] == {
        "created": 2, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0,
    }
    assert summary["verification_passed"] is True
    # 恰好 2 次 create，参数与卡片一致
    assert [c["tool"] for c in h.client.write_calls] == ["create", "create"]
    amounts = sorted(
        c["arguments"]["data"]["amount"] for c in h.client.write_calls
    )
    assert amounts == ["100.00", "200.00"]
    assert len(h.client.transactions) == 2
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED
    # 审计条数 = 2N（仅实际写调用）
    assert len(h.audit.events(task_id=pc["task_id"])) == 4


def test_create_batch_partial_failure_awaits_recovery_then_fills_gap(h) -> None:
    """部分失败 → awaiting_recovery 保持 running；恢复只补未完成项（skipped 零写）。"""
    h.client.fail_create_matching.append({"category": "房租"})
    out = h.prepare("finance_create", {"items": _create_items()})
    pc = out["pending_confirmation"]

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["counts"] == {
        "created": 1, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 1,
    }
    assert summary["verification_passed"] is False
    assert summary["error_code"] == ERROR_PARTIAL
    assert out2["messages"][0] and "房租" in summary["error_summary"]
    # 两条都发起了调用（失败条不中断其余），仅一条落库
    assert len(h.client.write_calls) == 2
    assert len(h.client.transactions) == 1
    # 任务保持 running + awaiting_recovery（未假报成功/未终止）
    rec = h.tasks.get_task(pc["task_id"])
    assert rec.status == TaskStatus.RUNNING
    assert rec.current_step == "awaiting_recovery"
    assert rec.final_error_code is None

    # 故障恢复：同一 task 再次出卡确认（恢复执行只补未完成项）
    h.client.fail_create_matching.clear()
    out3 = h.prepare(
        "finance_create", {"items": _create_items()}, task_id=pc["task_id"]
    )
    assert out3["current_step"] == "tool_prepared"
    out4 = h.confirm(out3["pending_confirmation"])
    summary2 = h.summary_of(out4)
    assert summary2["counts"] == {
        "created": 1, "updated": 0, "unchanged": 0, "skipped": 1, "failed": 0,
    }
    assert summary2["verification_passed"] is True
    # 已成功条目 skipped（零写）：总 create 调用 3 次（首次 2 + 恢复 1）
    assert len(h.client.write_calls) == 3
    assert len(h.client.transactions) == 2
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_create_batch_write_ahead_precheck_hit_skips_zero_write(h) -> None:
    """写前签名预查命中已存在记录 → skipped、**零写调用**（无幂等键防重核心）。

    第 1 条明细的 ERP 记录已存在（如上一次调用已写入但结果丢失）→ 执行期
    预查命中即跳过，不再重复 create；第 2 条照常写入。
    """
    h.client.seed_transaction({
        "tx_type": EXPENSE, "category": "运费", "amount": "100.00",
        "tx_date": "2026-09-10", "updated_at": "T0",
    })
    out = h.prepare("finance_create", {"items": _create_items()})
    pc = out["pending_confirmation"]
    out2 = h.confirm(pc)
    summary = h.summary_of(out2)

    assert summary["counts"]["skipped"] == 1
    assert summary["counts"]["created"] == 1
    assert summary["counts"]["failed"] == 0
    # 仅第 2 条发起 create（第 1 条命中预查 → 零写）
    assert [c["tool"] for c in h.client.write_calls] == ["create"]
    assert h.client.write_calls[0]["arguments"]["data"]["category"] == "房租"
    assert len(h.client.transactions) == 2  # 预置 + 新增，无重复


def _dup_items() -> list[dict]:
    """两条**完全相同**的明细（同 tx_type/分类/金额/日期）——重复行合法输入。"""
    return [
        {"tx_type": EXPENSE, "category": "运费", "amount": "100.00",
         "tx_date": "2026-09-10"},
        {"tx_type": EXPENSE, "category": "运费", "amount": "100.00",
         "tx_date": "2026-09-10"},
    ]


def test_create_batch_identical_rows_both_written(h) -> None:
    """两张完全相同的明细 → 最终**必须落 2 条**、created=2、skipped=0。

    回归守护（[2026-09-12 修复]）：写前签名预查必须按「本批内该业务签名的
    出现次序」对齐——第 2 条同签名明细在 ERP 只有 1 条同签名记录（第 1 条刚
    写入）时不得被误判为「已存在」而跳过。
    """
    out = h.prepare("finance_create", {"items": _dup_items()})
    pc = out["pending_confirmation"]
    # 序号化外层键，两条如实上卡（不折叠）
    assert list(pc["card"]["fields_to_write"]) == [
        "1. 支出·运费（2026-09-10）", "2. 支出·运费（2026-09-10）",
    ]
    # item_id 经 build_item_ids 加 #n 后缀区分（同批稳定）
    exec_items = pc["execution"]["items"]
    assert [it["item_id"] for it in exec_items] == [
        "支出:运费:2026-09-10:100.00#1", "支出:运费:2026-09-10:100.00#2",
    ]

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["counts"] == {
        "created": 2, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0,
    }
    assert summary["verification_passed"] is True
    assert [c["tool"] for c in h.client.write_calls] == ["create", "create"]
    assert len(h.client.transactions) == 2
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_create_batch_identical_rows_recovery_no_duplicate_write(h) -> None:
    """恢复期哨兵：两条同签名明细在 ERP 已各有 1 条记录（上一次「写已发生、
    响应丢失」）→ 预查分别按出现次序认出，skipped=2、**零写调用**。

    若删掉写前签名预查，两条会各再写一次（ERP 变 4 条），本用例即变红——
    这是「不得因修重复行 bug 而丢掉恢复期防重复写入兜底」的哨兵。
    """
    # 上一次尝试的两条记录都真实落库了（结果未知，审计未记 success）
    h.client.seed_transaction({
        "tx_type": EXPENSE, "category": "运费", "amount": "100.00",
        "tx_date": "2026-09-10", "updated_at": "T0",
    })
    h.client.seed_transaction({
        "tx_type": EXPENSE, "category": "运费", "amount": "100.00",
        "tx_date": "2026-09-10", "updated_at": "T1",
    })
    out = h.prepare("finance_create", {"items": _dup_items()})
    pc = out["pending_confirmation"]
    out2 = h.confirm(pc)
    summary = h.summary_of(out2)

    assert summary["counts"] == {
        "created": 0, "updated": 0, "unchanged": 0, "skipped": 2, "failed": 0,
    }
    assert h.client.write_calls == []  # 零写调用
    assert len(h.client.transactions) == 2  # 不重复写
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_create_batch_identical_rows_unknown_result_no_duplicate(h) -> None:
    """同批两条同签名明细均「写已发生但响应丢失」→ 写后验证按出现次序认出
    各自记录，各记 created 且不重复写（ERP 仍 2 条，create 调用 2 次）。"""
    h.client.unknown_create_call_numbers = {1, 2}
    out = h.prepare("finance_create", {"items": _dup_items()})
    pc = out["pending_confirmation"]
    out2 = h.confirm(pc)
    summary = h.summary_of(out2)

    assert summary["counts"]["created"] == 2
    assert summary["counts"]["failed"] == 0
    assert len(h.client.write_calls) == 2
    assert len(h.client.transactions) == 2  # 未重复落库
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_create_batch_identical_rows_partial_failure_recovery(h) -> None:
    """同批同签名明细第 2 条失败 → 恢复只补第 2 条、第 1 条不重复写。

    第 1 条写成功后第 2 条失败（未写），恢复期第 2 条的出现次序为 2、ERP
    只有 1 条同签名记录 → 计数不足 2 → 照写（不得因「本批已有 1 条成功」而
    误判跳过——正是「简单排除本次已写 id」会踩的坑）。
    """
    h.client.fail_create_call_numbers = {2}  # 只让第 2 次 create（第 2 条）失败
    out = h.prepare("finance_create", {"items": _dup_items()})
    pc = out["pending_confirmation"]
    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["counts"] == {
        "created": 1, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 1,
    }
    assert len(h.client.write_calls) == 2
    assert len(h.client.transactions) == 1
    rec = h.tasks.get_task(pc["task_id"])
    assert rec.status == TaskStatus.RUNNING
    assert rec.current_step == "awaiting_recovery"

    # 恢复：第 1 条 prior success → skipped 零写；第 2 条照写补齐
    h.client.fail_create_call_numbers.clear()
    out3 = h.prepare("finance_create", {"items": _dup_items()}, task_id=pc["task_id"])
    out4 = h.confirm(out3["pending_confirmation"])
    summary2 = h.summary_of(out4)
    assert summary2["counts"] == {
        "created": 1, "updated": 0, "unchanged": 0, "skipped": 1, "failed": 0,
    }
    assert summary2["verification_passed"] is True
    assert len(h.client.write_calls) == 3  # 首次 2 + 恢复 1
    assert len(h.client.transactions) == 2
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED


def test_create_single_flat_path_behavior_unchanged(h) -> None:
    """单条（扁平简写，len==1）→ 单条路径：1 次 create、record_id 回填，语义
    与改造前一致（ordinal 恒 1，逐字节不变）。"""
    out = h.prepare("finance_create", {
        "tx_type": EXPENSE, "category": "运费", "amount": "100",
        "tx_date": "2026-09-10",
    })
    pc = out["pending_confirmation"]
    assert pc["card"]["target"] == "支出·运费（2026-09-10）"  # 轻卡（非批量卡）
    assert "items" not in pc["execution"]
    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["result_status"] == "success"
    assert summary["record_id"] is not None
    assert [c["tool"] for c in h.client.write_calls] == ["create"]
    assert len(h.client.transactions) == 1


def test_create_old_shape_execution_uses_old_executor(h) -> None:
    """旧形状在飞卡（execution 无 items）→ 旧单条执行器，语义逐字节不变。"""
    # 用旧路径（扁平单条）prepare 出无 items 的 execution
    prep = prepare_finance_create(
        ToolContext(client=h.client, tasks=h.tasks, audit=h.audit),
        FinanceCreateArgs(
            tx_type=EXPENSE, category="运费", amount="100",
            tx_date="2026-09-10",
        ),
    )
    assert "items" not in prep.execution and "batch_size" not in prep.execution
    task = h.tasks.create_task(_write_task_spec())
    h.tasks.request_confirmation(task.task_id, prep.payload_hash)
    h.tasks.confirm(task.task_id, approved=True)

    result = execute_finance_create(
        ToolContext(
            client=h.client, tasks=h.tasks, audit=h.audit,
            context=AgentContext(
                user_id=ACTOR_ACCOUNT, user_role=ACTOR_ROLE,
                scope=frozenset({"finance:create"}), thread_id=THREAD,
            ),
        ),
        None,
        task_id=task.task_id,
        execution=prep.execution,
    )
    assert result.ok is True
    # 旧汇总形状（record_id 等），且仅 1 次 create
    assert result.summary["record_id"] is not None
    assert [c["tool"] for c in h.client.write_calls] == ["create"]
    assert h.tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 阶段 5：finance_update 批量
# ---------------------------------------------------------------------------


def _seed_two(h) -> None:
    h.client.seed_transaction(_seed_tx(1, amount="100.00", updated_at="T1"))
    h.client.seed_transaction(_seed_tx(2, amount="200.00", category="房租",
                                       updated_at="T2"))


def test_update_batch_full_chain(h) -> None:
    """全链路：出卡前零写 → 确认后逐条 update → succeeded；审计 2N。"""
    _seed_two(h)
    out = h.prepare("finance_update", {"items": [
        {"id": 1, "amount": "150"},
        {"id": 2, "amount": "250"},
    ]})
    assert out["current_step"] == "tool_prepared"
    pc = out["pending_confirmation"]
    card = pc["card"]
    assert card["target"] == "批量更新 2 条收支记录"
    # 两条都是「更新」（有差异）
    rows = dict(card["fields_to_write"])
    assert rows["1. 2026-09-01 运费"]["操作"] == "更新"
    assert rows["2. 2026-09-01 房租"]["操作"] == "更新"
    assert h.client.write_calls == []  # 出卡前零写

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["counts"] == {
        "created": 0, "updated": 2, "unchanged": 0, "skipped": 0, "failed": 0,
    }
    assert [c["tool"] for c in h.client.write_calls] == ["update", "update"]
    assert h.client.transactions[1]["amount"] == "150.00"
    assert h.client.transactions[2]["amount"] == "250.00"
    assert h.tasks.get_task(pc["task_id"]).status == TaskStatus.SUCCEEDED
    assert len(h.audit.events(task_id=pc["task_id"])) == 4  # 2N


def test_update_batch_whole_card_invalidation_zero_write(h) -> None:
    """整卡失效零写：任一条快照变化（他人改动）→ 整批一条不写 + 任务 failed。

    第 2 条的 updated_at 在确认后被外部改动 → 执行期整卡重建哈希必变 →
    ``_fail_batch_invalid`` 整批零写。**绝不改为逐条比对**（逐条会让未变的
    第 1 条照写——本用例即该红线的定点突变守护）。
    """
    _seed_two(h)
    out = h.prepare("finance_update", {"items": [
        {"id": 1, "amount": "150"},
        {"id": 2, "amount": "250"},
    ]})
    pc = out["pending_confirmation"]
    task_id = pc["task_id"]

    # 确认后、执行前：第 2 条被他人改动（updated_at 变化 → 本地乐观锁失效）
    h.client.transactions[2]["updated_at"] = "2099-01-01T00:00:00"

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert out2["current_step"] == "tool_done"
    assert summary["counts"]["updated"] == 0
    assert summary["counts"]["failed"] == 2
    assert summary["error_code"] == ERROR_UPDATE_CONFIRMATION_INVALID
    # 整批零写
    assert h.client.write_calls == []
    assert h.client.transactions[1]["amount"] == "100.00"  # 未变条目也未写
    rec = h.tasks.get_task(task_id)
    assert rec.status == TaskStatus.FAILED
    assert rec.final_error_code == ERROR_UPDATE_CONFIRMATION_INVALID


def test_update_batch_unchanged_item_zero_write(h) -> None:
    """无差异条目 action=unchanged → 执行期零写；仅变化条目 update。"""
    _seed_two(h)
    # 第 2 条期望值 == 当前值（200.00）→ 无差异；第 1 条有变化
    out = h.prepare("finance_update", {"items": [
        {"id": 1, "amount": "150"},
        {"id": 2, "amount": "200.00"},
    ]})
    pc = out["pending_confirmation"]
    rows = dict(pc["card"]["fields_to_write"])
    assert rows["1. 2026-09-01 运费"]["操作"] == "更新"
    assert rows["2. 2026-09-01 房租"]["操作"] == "保持"

    out2 = h.confirm(pc)
    summary = h.summary_of(out2)
    assert summary["counts"] == {
        "created": 0, "updated": 1, "unchanged": 1, "skipped": 0, "failed": 0,
    }
    # 仅第 1 条 update（unchanged 零写）
    assert [c["tool"] for c in h.client.write_calls] == ["update"]
    assert h.client.write_calls[0]["arguments"]["id"] == 1
    assert h.client.transactions[2]["amount"] == "200.00"


def test_update_old_shape_execution_uses_old_executor(h) -> None:
    """旧形状在飞卡（execution 无 items）→ 旧单条执行器，语义不变。"""
    h.client.seed_transaction(_seed_tx(1, amount="100.00", updated_at="T1"))
    prep = prepare_finance_update(
        ToolContext(client=h.client, tasks=h.tasks, audit=h.audit),
        FinanceUpdateArgs(id=1, amount="150"),
    )
    assert "items" not in prep.execution
    task = h.tasks.create_task(_write_task_spec())
    h.tasks.request_confirmation(task.task_id, prep.payload_hash)
    h.tasks.confirm(task.task_id, approved=True)

    result = execute_finance_update(
        ToolContext(
            client=h.client, tasks=h.tasks, audit=h.audit,
            context=AgentContext(
                user_id=ACTOR_ACCOUNT, user_role=ACTOR_ROLE,
                scope=frozenset({"finance:update"}), thread_id=THREAD,
            ),
        ),
        None,
        task_id=task.task_id,
        execution=prep.execution,
    )
    assert result.ok is True
    assert [c["tool"] for c in h.client.write_calls] == ["update"]
    assert h.client.transactions[1]["amount"] == "150.00"
    assert h.tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def _write_task_spec():
    from administrative_agent.tasks.records import NewTaskSpec

    return NewTaskSpec(
        thread_id=THREAD,
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code="R7",
        original_user_message="改收支",
        structured_intent={"duty": "R7"},
        is_write=True,
    )


# ---------------------------------------------------------------------------
# [2026-10-05 确认卡改进第 3 批] 写分支整卡级展示键 source_quote / source_note
# ---------------------------------------------------------------------------


def test_write_branch_attaches_source_quote_from_last_human(h) -> None:
    """写分支出卡：卡上带 source_quote == 最近一条用户原话全文。"""
    out = h.prepare("finance_create", {"items": _create_items()})
    pc = out["pending_confirmation"]
    assert pc["card"]["source_quote"] == "处理这笔收支"
    # 纯展示级：不进 payload_hash（哈希仍等于任务保存的 payload_hash）
    assert h.tasks.get_task(pc["task_id"]).confirmed_payload_hash == pc["payload_hash"]


def test_write_branch_omits_source_quote_without_human(h) -> None:
    """无 human 原话时，卡 JSON 不含 source_quote 键（旧卡零回归）。"""
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "finance_create",
            "tool_arguments": {"items": _create_items()},
        },
    }
    out = tools_node_mod.tools_node(state, h.runtime)
    assert out["current_step"] == "tool_prepared"
    assert "source_quote" not in out["pending_confirmation"]["card"]


def test_finance_card_carries_source_note(h) -> None:
    """收支卡带固定口径 source_note 展示键。"""
    out = h.prepare("finance_create", {"items": _create_items()})
    assert out["pending_confirmation"]["card"]["source_note"] == FINANCE_SOURCE_NOTE
