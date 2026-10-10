"""[2026-09-10 P1-1] 收支确认卡关联方展示行为测试。

背景（reviewer P1-1）：CONFIRMATION_HIDDEN_FIELDS 全局过滤把带
customer_id/supplier_id 的收支卡把关联方信息**整条删掉**——实测
``finance_update(changes={'customer_id': 16})`` 卡片字段只剩
``(('预期更新时间', 'T1'),)``，看不出任何实际变更，用户零机会察觉模型
选错实体（base.py 明确存在「客户/供货商同名按订单类型选实体」的歧义）。

修复口径：在 prepare/建卡阶段把 customer_id/supplier_id 解析成名称后作为
展示字段呈现（查不到名称 → 「客户ID 16」类占位，不删整行）；执行载荷与
payload_hash 的 customer_id/supplier_id 原样保留（服务端存档，不受影响）。

本文件用最小 fake ERP（call_tool → query）覆盖：
- resolve_party_display：id→名称、查不到 → 占位、查询失败不抛异常；
- prepare_finance_create：带 customer_id 的创建卡展示「关联客户=昌盛」，
  且不出现裸数字 id；execution 载荷仍保留 customer_id；
- prepare_finance_update：只改 customer_id 的更新卡展示关联方名称（旧
  行为只剩「预期更新时间」）。
"""

from __future__ import annotations

from typing import Any

from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.finance import (
    FinanceCreateArgs,
    FinanceUpdateArgs,
    prepare_finance_create,
    prepare_finance_update,
)
from administrative_agent.audit.service import AuditService
from administrative_agent.domains.finance import (
    FinanceUpdateExecutor,
    build_finance_update_confirmation_card,
    resolve_party_display,
)
from administrative_agent.domains.orders import is_confirmation_valid
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.tasks.records import NewTaskSpec
from administrative_agent.tasks.service import TaskService
from tests.audit.conftest import (
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.tasks.conftest import InMemoryTaskRepository

CUSTOMER_ID = 16
CUSTOMER_NAME = "昌盛"
SUPPLIER_ID = 7
SUPPLIER_NAME = "示例塑粉"


class _FakeErp:
    """prepare 只读 fake：按 entity 返回预置行；等值 filter + fields 裁剪。

    fail_on: entity 集合 → 对该实体查询返回 ok=False（模拟查询失败，
    验证 resolve_party_display 不抛异常、退化占位）。
    """

    def __init__(self, rows: dict[str, list[dict]], *, fail_on: set[str] | None = None) -> None:
        self._rows = rows
        self.fail_on = fail_on or set()
        self.calls: list[dict] = []

    def call_tool(self, name: str, args: dict):
        self.calls.append(dict(args))
        assert name == "query"
        entity = args["entity"]
        if entity in self.fail_on:
            class _Err:
                ok = False
                error = MCPError(
                    code="ERP_BUSY", category=ErrorCategory.SERVER,
                    message="server busy",
                )

            return _Err()
        rows = self._rows.get(entity, [])
        filt = args.get("filter") or {}
        items = [
            dict(r) for r in rows
            if all(r.get(k) == v for k, v in filt.items())
        ]
        fields = args.get("fields")
        if fields:
            items = [{k: it[k] for k in fields if k in it} for it in items]

        class _Result:
            ok = True
            data: dict[str, Any] = {"items": items}

        return _Result()


def _ctx(erp: _FakeErp) -> ToolContext:
    """prepare 只经 _require_wiring 校验 client/tasks/audit 非 None。"""
    return ToolContext(client=erp, tasks=object(), audit=object())


#: transaction_categories 分类目录（分类强校验须命中且归属 tx_type）
_CATEGORIES = {"transaction_categories": [
    {"id": 1, "tx_type": "支出", "category_name": "运费"},
    {"id": 2, "tx_type": "收入", "category_name": "回款"},
]}


def test_resolve_party_display_maps_id_to_name() -> None:
    """customer_id/supplier_id → 展示项（名称）；无 FK → 空。"""
    erp = _FakeErp({
        "customers": [{"id": CUSTOMER_ID, "company_name": CUSTOMER_NAME}],
        "suppliers": [{"id": SUPPLIER_ID, "name": SUPPLIER_NAME}],
    })
    assert resolve_party_display(erp, {"customer_id": CUSTOMER_ID}) == (
        ("customer_name", CUSTOMER_NAME),
    )
    assert resolve_party_display(erp, {"supplier_id": SUPPLIER_ID}) == (
        ("supplier_name", SUPPLIER_NAME),
    )
    assert resolve_party_display(erp, {}) == ()


def test_resolve_party_display_placeholder_and_failure_no_raise() -> None:
    """查不到名称 → 「客户ID 16」占位；查询失败 → 占位且不抛异常。"""
    erp = _FakeErp({"customers": []})
    # 无此 id → 占位（不静默删除关联方）
    assert resolve_party_display(erp, {"customer_id": CUSTOMER_ID}) == (
        ("customer_name", f"客户ID {CUSTOMER_ID}"),
    )
    # 查询失败 → 占位，不抛异常
    failing = _FakeErp({}, fail_on={"customers"})
    assert resolve_party_display(failing, {"customer_id": 99}) == (
        ("customer_name", "客户ID 99"),
    )


def test_finance_create_card_shows_customer_name_not_id() -> None:
    """带 customer_id 的创建卡展示「关联客户=昌盛」，不出现裸数字 id；
    执行载荷原样保留 customer_id（两段式写入不受展示层影响）。"""
    erp = _FakeErp({**_CATEGORIES, "customers": [
        {"id": CUSTOMER_ID, "company_name": CUSTOMER_NAME},
    ]})
    prep = prepare_finance_create(_ctx(erp), FinanceCreateArgs(
        tx_type="支出", category="运费", amount="100",
        tx_date="2026-09-10", customer_id=CUSTOMER_ID,
    ))
    fields = prep.card["fields_to_write"]
    assert fields["关联客户"] == CUSTOMER_NAME
    assert "客户ID" not in fields  # id 行不再展示
    assert CUSTOMER_ID not in fields.values()  # 展示的是名称，不是裸数字
    # 执行载荷保留 id（写入 ERP 的真实参数不变）
    assert prep.execution["payload"]["customer_id"] == CUSTOMER_ID
    assert prep.payload_hash


def test_finance_update_card_shows_customer_name_for_fk_change() -> None:
    """只改 customer_id 的更新卡展示关联方名称（旧行为：整条被隐藏，
    卡片只剩「预期更新时间」，看不出实际变更）。"""
    erp = _FakeErp({
        "transactions": [{
            "id": 5, "tx_type": "支出", "category": "运费", "amount": "100",
            "tx_date": "2026-09-01", "customer_id": 3, "updated_at": "T0",
        }],
        "customers": [{"id": CUSTOMER_ID, "company_name": CUSTOMER_NAME}],
    })
    prep = prepare_finance_update(_ctx(erp), FinanceUpdateArgs(
        id=5, customer_id=CUSTOMER_ID,
    ))
    fields = prep.card["fields_to_write"]
    # 唯一实际变更（关联客户）以名称呈现，用户能核对
    assert fields["关联客户"] == CUSTOMER_NAME
    assert "客户ID" not in fields
    assert CUSTOMER_ID not in fields.values()
    # 乐观锁参数仍在，卡片非空（旧 bug：只剩 expected_updated_at）
    assert "预期更新时间" in fields


# ---------------------------------------------------------------------------
# [2026-09-10 修复] hash 与展示解耦：展示层解析波动不得污染执行期校验
# ---------------------------------------------------------------------------

_TX_BEFORE = {
    "id": 5, "tx_type": "支出", "category": "运费", "project": None,
    "amount": "100.00", "tx_date": "2026-09-01", "notes": None,
    "customer_id": None, "supplier_id": None, "updated_at": "T0",
}


def _update_card(record_id=5, *, changes=None, party_display=()):
    return build_finance_update_confirmation_card(
        record_id=record_id, before=dict(_TX_BEFORE),
        changes=changes if changes is not None else {"customer_id": CUSTOMER_ID},
        expected_updated_at="T0",
        party_display=party_display,
    )


def test_finance_update_hash_ignores_party_display_fluctuation() -> None:
    """[2026-09-10 修复] 展示层关联方名称在 prepare 期「昌盛」、执行期退化
    为「客户ID 16」占位 → payload_hash 不变、is_confirmation_valid 仍 True
    （旧 bug：占位混进 hash → 用户写操作被误拒「确认已失效」）。

    同时断言展示不回退：占位仍呈现，字段不含裸数字 id。
    """
    card_prepare = _update_card(party_display=(("customer_name", CUSTOMER_NAME),))
    card_exec = _update_card(party_display=(("customer_name", f"客户ID {CUSTOMER_ID}"),))
    # hash 只看原始执行载荷（changes + expected_updated_at + id），
    # 派生展示字段 party_display 不入哈希
    assert card_exec.payload_hash == card_prepare.payload_hash
    assert card_exec.hash_fields == card_prepare.hash_fields
    assert is_confirmation_valid(card_exec, card_prepare.payload_hash) is True
    # 展示仍呈现关联方（占位），且不含裸数字 id / 裸 id 键
    fields = dict(card_exec.fields_to_write)
    assert fields["关联客户"] == f"客户ID {CUSTOMER_ID}"
    assert "客户ID" not in fields  # 隐藏键仍不展示
    assert CUSTOMER_ID not in fields.values()  # 占位是字符串，不是裸数字
    assert "id" not in fields  # record_id 只进 hash，不进展示


def test_finance_update_hash_binds_record_identity() -> None:
    """[2026-09-10 修复] 恢复身份绑定：同 before/同 changes/同 expected 但
    record_id 不同 → payload_hash 不同（id 只进 hash_fields，不进 target）。
    """
    card_a = _update_card(record_id=111, changes={"amount": "1650.00"})
    card_b = _update_card(record_id=112, changes={"amount": "1650.00"})
    assert card_a.target == card_b.target  # 展示目标一致（不含 id）
    assert card_a.payload_hash != card_b.payload_hash  # 但身份不同 → 哈希不同
    assert ("id", 111) in card_a.hash_fields
    # 展示不回退：target / fields_to_write 均无裸 record_id
    assert "111" not in card_a.target and "112" not in card_b.target
    assert "id" not in dict(card_a.fields_to_write)


class _OkResult:
    ok = True

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.retries = 0


class _ErrResult:
    ok = False

    def __init__(self, error: MCPError) -> None:
        self.error = error
        self.retries = 0


class _ExecFakeErp:
    """执行期 fake：transactions 单行 + 关联方查询；可切换 customers 查询失败。

    fail_customers=True 时 customers/query 返回 ok=False，模拟「prepare 期查到
    名称、执行期查名失败退化为占位」的生产抖动场景。
    """

    def __init__(self, transaction: dict, *, customers_rows: list[dict] | None = None):
        self.transaction = dict(transaction)
        self.customers_rows = customers_rows or []
        self.fail_customers = False
        self.update_args: dict | None = None

    def call_tool(self, name: str, args: dict, **_kwargs):
        entity = args.get("entity")
        if name == "update":
            self.update_args = dict(args)
            self.transaction.update(args.get("data") or {})
            return _OkResult({"record": dict(self.transaction)})
        if name == "query":
            if entity == "customers":
                if self.fail_customers:
                    return _ErrResult(MCPError(
                        code="ERP_BUSY", category=ErrorCategory.SERVER,
                        message="server busy",
                    ))
                rows = self.customers_rows
            elif entity == "transactions":
                rows = [self.transaction]
            else:
                rows = []
            filt = args.get("filter") or {}
            items = [dict(r) for r in rows if all(r.get(k) == v for k, v in filt.items())]
            fields = args.get("fields")
            if fields:
                items = [{k: it[k] for k in fields if k in it} for it in items]
            return _OkResult({"items": items})
        raise AssertionError(f"unexpected tool: {name}")


def test_finance_update_execution_not_rejected_when_party_lookup_degrades() -> None:
    """[2026-09-10 修复] 端到端回归：prepare 期查到「昌盛」，执行期 customers
    查询失败 → 名称退化为占位；确认仍有效、update 正常执行（不被误拒）。

    旧 bug：执行期重建卡把占位混进 hash → is_confirmation_valid=False →
    任务以 FINANCE_UPDATE_CONFIRMATION_INVALID 失败，用户写操作被拒。
    """
    erp = _ExecFakeErp(
        _TX_BEFORE,
        customers_rows=[{"id": CUSTOMER_ID, "company_name": CUSTOMER_NAME}],
    )
    tasks = TaskService(InMemoryTaskRepository())
    audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
    ctx = ToolContext(client=erp, tasks=tasks, audit=audit)

    # prepare：customers 查询可用 → 展示解析出名称「昌盛」
    prep = prepare_finance_update(ctx, FinanceUpdateArgs(id=5, customer_id=CUSTOMER_ID))
    assert dict(prep.card["fields_to_write"])["关联客户"] == CUSTOMER_NAME

    task = tasks.create_task(NewTaskSpec(
        thread_id="thread-fin", actor_account="manager01", actor_role="manager",
        duty_code="R7", original_user_message="改收支关联客户",
        structured_intent={"duty": "R7"}, is_write=True,
    ))
    tasks.request_confirmation(task.task_id, prep.payload_hash)
    tasks.confirm(task.task_id, approved=True)

    # 执行期关联方查询抖动 → 名称退化为占位
    erp.fail_customers = True
    executor = FinanceUpdateExecutor(
        client=erp, tasks=tasks, audit=audit, trace_id="trace-fin-0001",
        actor_account="manager01", actor_role="manager",
    )
    summary = executor.execute(
        task_id=task.task_id, record_id=5, desired={"customer_id": CUSTOMER_ID},
    )

    assert summary.result_status == "success"
    assert summary.error_code is None
    assert erp.update_args == {
        "entity": "transactions", "id": 5, "data": {"customer_id": CUSTOMER_ID},
    }
    assert tasks.get_task(task.task_id).status.value == "succeeded"
