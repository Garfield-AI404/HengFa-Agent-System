"""14.3 端到端 13 场景 pytest 化（D24；规格 L1394-1408；16.6 RV-37）。

每个场景落为可重复运行的集成测试：补全/计划构建 → 执行器 → ERP 假服务
断言场景验收。

假服务为 FakeAttendanceErp / FakeOrdersErp / MultiFieldQueryErp（respx，
D5 规则：测试夹具，不代表 ERP 真实运行态）；各场景深层分支（幂等四态、
版本冲突重算、网络未知恢复、复核门禁等）在 tests/domains/* 与
tests/contract/* 已有专项覆盖，本套件断言场景级验收口径并引用专项用例；
scripts/d7-d21 演示脚本为人工证据（RV-37 引用，不替代本套件）。

## 2026-08-29 重写说明

D26 删除了规则化意图分类（`agent/intent.py`）、策略解析器
（`agent/policy_resolver.py`）与「自然语言 → 意图 → 规则包」这条确定性
链路，改由 LLM 直接决策。原文件的 13 个场景中：

- **8 个（S02/S05–S11）不依赖已删模块**，原样保留；
- **3 个（S01/S03/S04）只有开头的意图分类前奏依赖已删模块**，删除前奏，
  业务验收断言（ERP 调用、计划动作、任务状态）全部保留——那才是场景的
  验收口径；
- **2 个（S12/S13）整体就是被删链路的断言**，已改写为断言 D26 下**仍然
  存在**的等价护栏：
  * S12（developer 查询 fail closed）→ 护栏下沉到工具层
    `tools/query.py:run_erp_query`：非 manager 或缺 `erp:query` scope 一律
    返回 `ok=False`。比原意图层校验更靠后、更本质（绕不过）。
  * S13（不支持职责明确拒绝）→ 护栏分两处：提示词 10.1 第 4/6 条明令收支
    记录只走 finance_create/finance_update 专用工具、禁止绕过工具自建财务
    流水、禁止删除订单、禁止报销或请假审批；工具注册表只有职责内业务工具
    （[2026-09-02 R7 收支写] 后为 R1-R7 域 11 个），「取消订单」「联系
    客户」无工具可调用（无执行路径）。

[2026-09-09] 追加 run_erp_query 续查合并/去重行为用例（fields 不含 id 时
行塌缩 bug 的修复回归 + id 去重回归保护，直接调用点同场景 12），见文末
「场景 12 配套」段。

[2026-09-10] 追加 order_by 降序前缀/数组透传与 limit 上限 200 的参数
契约用例（同段）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from administrative_agent.agent.prompts.base import BASE_PROMPT_RULES
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.business.attendance_rules import (
    AttendanceException,
    ExistingRecord,
    plan_attendance,
)
from administrative_agent.business.order_rules import (
    ERR_POWDER_PRICE_AMBIGUOUS,
    ERR_POWDER_WEIGHT_REQUIRED,
    CustomerProfile,
    MetalOrderInput,
    PowderOrderError,
    PowderOrderInput,
    complete_metal_order,
    complete_powder_order,
)
from administrative_agent.domains.orders import (
    MetalOrderExecutor,
    OrderUpdateExecutor,
    build_metal_confirmation_card,
    build_update_confirmation_card,
    is_confirmation_valid,
)
from administrative_agent.domains.reconciliation import (
    ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW,
    ITEM_CONFLICT,
    ITEM_REPLAYED,
)
from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.tasks.machine import TaskStatus

from tests.domains.test_orders import (
    make_metal_order,
    make_powder_order,
)
from tests.domains.test_reconciliation_execute import (
    CREATE_ROW,
    create_diff_entry,
    create_item,
    make_report,
    make_write_plan,
    update_item,
    write_calls,
)
from tests.api.conftest import FakeMCPClient
from tests.integration.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ATTENDANCE_DATE,
    EMPLOYEE_NAMES,
    EMPLOYEES,
    TRACE_ID,
    create_task,
    order_task,
    scenario1_plan,
)

TODAY = "2026-08-22"
CUSTOMER_ID = 11
CUSTOMER_NAME = "示例机械有限公司"
POWDER_SUPPLIER_ID = 101
POWDER_SUPPLIER_NAME = "示例塑粉材料有限公司"
POWDER_COLOR = "R-01"
KEY_D24 = "d24-scenario-key-000001"

def confirm_light(tasks, task_id, card) -> None:
    """7.3 轻确认：请求确认 → 用户确认 → 任务 running（执行前）。"""
    tasks.request_confirmation(task_id, card.payload_hash)
    tasks.confirm(task_id, approved=True)
    assert is_confirmation_valid(card, card.payload_hash)


def r6_run(executor, tasks, reviews, items, *, rows=None, report=None):
    """R6 场景辅助：任务（running）→ 复核（approved）→ 执行。"""
    task = order_task(
        tasks, duty="R6",
        structured_intent={"duty": "R6", "operation": "reconcile_execute"},
    )
    plan = make_write_plan(task.task_id, items)
    review = reviews.request_review(
        task_id=plan.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    review = reviews.approve(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,
    )
    summary = executor.execute(
        task_id=task.task_id, plan=plan, review=review,
        report=report, rows=rows or {},
    )
    return task, plan, review, summary


# ---------------------------------------------------------------------------
# 场景 1/2：考勤首次与下午再次录入（4.3/14.1；深度专项 tests/integration/
# test_attendance_workflow.py + scripts/d12_attendance_demo.py）
# ---------------------------------------------------------------------------


def test_scenario01_first_attendance_entry(ctx) -> None:
    """首次录入：1 请假 + 1 加班 + 其余正常 → 5 条创建、验证通过、succeeded。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.counts == {"created": 5, "updated": 0, "unchanged": 0, "failed": 0}
    assert summary.verification_passed is True
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["张三"].final_action == "created"  # 请假
    assert by_ref["李四"].final_action == "created"  # 加班
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    created = {c["data"]["employee_id"]: c["data"] for c in erp.created_calls}
    assert created[1]["leave_minutes"] == 120
    assert created[2]["overtime_minutes"] == 180


def test_scenario02_afternoon_second_entry(ctx) -> None:
    """下午再次录入：保留上午请假、新增加班、补建缺失正常记录。"""
    erp, tasks, audit, executor, _ = ctx
    erp.seed({
        "employee_id": 1, "att_date": ATTENDANCE_DATE,
        "leave_minutes": 120, "overtime_minutes": None,
    })
    plan = plan_attendance(
        ATTENDANCE_DATE,
        EMPLOYEE_NAMES,
        [AttendanceException("李四", overtime_hours=3)],
        existing_records=[ExistingRecord("张三", 120, None)],
    )
    assert {p.employee_ref: p.action for p in plan.all_records} == {
        "张三": "unchanged", "李四": "created", "王五": "created",
        "赵六": "created", "钱七": "created",
    }
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=plan,
        employees=EMPLOYEES,
    )
    assert summary.verification_passed is True
    assert summary.counts == {"created": 4, "updated": 0, "unchanged": 1, "failed": 0}
    assert len(erp.created_calls) == 4
    assert all(
        c["data"]["employee_id"] != 1 for c in erp.created_calls
    )  # 上午请假保留，不重复 create
    # 张三记录仍在 ERP 且请假未丢
    zhang = [r for r in erp.records.values() if r["employee_id"] == 1]
    assert len(zhang) == 1 and zhang[0]["leave_minutes"] == 120


# ---------------------------------------------------------------------------
# 场景 3-6：钣金/塑粉创建（RV-19/RV-21/RV-22；深度专项 tests/domains/
# test_orders.py + scripts/d14_order_demo.py / d15_powder_demo.py）
# ---------------------------------------------------------------------------


def test_scenario03_metal_missing_weight_writes_zero(octx) -> None:
    """钣金缺重量：写 0（MISSING_WEIGHT_PER_UNIT）+ 客户价格/单位唯一补全
    + 轻确认后创建。"""
    erp, tasks, audit, executor, client = octx
    draft = complete_metal_order(
        MetalOrderInput(
            customer_name=CUSTOMER_NAME, model="MK-01",
            unit_price=120.0, quantity=2,  # 未提供重量 → 写 0
        ),
        customer_id=CUSTOMER_ID, customer_name=CUSTOMER_NAME,
        profile=CustomerProfile(price_type="含税", price_unit="元/kg"),
        today=TODAY,
    )
    assert draft.weight_per_unit == 0  # 14.3 场景 3：缺重量写 0
    assert draft.price_type == "含税"  # 客户资料唯一补全
    assert draft.weight_unit == "kg"
    card = build_metal_confirmation_card(draft)
    task = order_task(tasks, structured_intent={"duty": "R2", "order_type": "metal"})
    confirm_light(tasks, task.task_id, card)

    summary = executor.execute(task_id=task.task_id, draft=draft)
    assert summary.result_status == "success"
    assert summary.order_id is not None
    assert summary.verification_passed is True
    order = erp.orders[summary.order_id]
    assert order["weight_per_unit"] == 0
    assert order["price_type"] == "含税"
    assert order["weight_unit"] == "kg"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert len(erp.orders) == 1  # 恰 1 单


def test_scenario04_powder_missing_weight_asks_no_create(octx) -> None:
    """塑粉缺重量：直接询问（ERR_POWDER_WEIGHT_REQUIRED），不创建、不触网。"""
    erp, tasks, audit, executor, client = octx
    task = order_task(tasks, structured_intent={"duty": "R2", "order_type": "powder"})
    try:
        complete_powder_order(
            PowderOrderInput(
                supplier_name=POWDER_SUPPLIER_NAME, color_code=POWDER_COLOR,
                price_per_kg=28.5,  # 无 total_weight
            ),
            supplier_id=POWDER_SUPPLIER_ID,
            supplier_name=POWDER_SUPPLIER_NAME,
            history_rows=[],
            today=TODAY,
        )
        raise AssertionError("缺重量必须拒绝")
    except PowderOrderError as exc:
        assert exc.code == ERR_POWDER_WEIGHT_REQUIRED
    assert erp.calls == []  # 询问分支零 ERP 调用
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING  # 未创建未失败


def test_scenario05_powder_unique_history_price_autofill(octx) -> None:
    """塑粉同供货商同色号唯一历史价 → 自动补全并创建（RV-21）。"""
    erp, tasks, audit, executor, client = octx
    history = [
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 27.0},
    ]
    draft = complete_powder_order(
        PowderOrderInput(
            supplier_name=POWDER_SUPPLIER_NAME, color_code=POWDER_COLOR,
            total_weight=50.0,  # 未提供价格 → 历史唯一价补全
        ),
        supplier_id=POWDER_SUPPLIER_ID,
        supplier_name=POWDER_SUPPLIER_NAME,
        history_rows=history,
        today=TODAY,
    )
    assert draft.price_per_kg == 27.0  # 历史唯一价自动补全（5.2/14.3 场景 5）
    task = order_task(tasks, structured_intent={"duty": "R2", "order_type": "powder"})
    powder_exec = MetalOrderExecutor(
        client=client, tasks=tasks, audit=audit, trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE,
        policy=policy_for("R2.order.create.powder"), target_type="powder_order",
    )
    summary = powder_exec.execute(task_id=task.task_id, draft=draft)
    assert summary.result_status == "success"
    assert erp.orders[summary.order_id]["price_per_kg"] == 27.0
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_scenario06_powder_multiple_history_prices_ask(octx) -> None:
    """塑粉历史出现多个价格 → 停止询问（ERR_POWDER_PRICE_AMBIGUOUS），不创建。"""
    erp, tasks, audit, executor, client = octx
    history = [
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 25.0},
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 30.0},
    ]
    try:
        complete_powder_order(
            PowderOrderInput(
                supplier_name=POWDER_SUPPLIER_NAME, color_code=POWDER_COLOR,
                total_weight=50.0,  # 未提供价格 → 多值歧义
            ),
            supplier_id=POWDER_SUPPLIER_ID,
            supplier_name=POWDER_SUPPLIER_NAME,
            history_rows=history,
            today=TODAY,
        )
        raise AssertionError("多价格必须询问")
    except PowderOrderError as exc:
        assert exc.code == ERR_POWDER_PRICE_AMBIGUOUS
    assert erp.calls == []


# ---------------------------------------------------------------------------
# 场景 7/8：订单更新（RV-23/RV-24；深度专项 tests/domains/test_orders.py
# L825/L884 + scripts/d16_order_update_demo.py）
# ---------------------------------------------------------------------------


def test_scenario07_metal_weight_supplement_recalc_and_snapshots(uctx) -> None:
    """补钣金重量：update_order 重算金额，保存前后快照（6.5）。

    断言口径与 tests/domains/test_orders.py::test_update_metal_weight_recalc
    _amount_and_snapshots（L825）一致：前后快照 + ERP 同事务重算金额 +
    receivable_current_impact。
    """
    erp, tasks, audit, _, client = uctx
    erp.seed_update_order(10, make_metal_order())  # weight 0.0000, qty 2, price 120
    executor = OrderUpdateExecutor(
        client=client, tasks=tasks, audit=audit, trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE,
        policy=policy_for("R3.order.update.metal"),
    )
    changes = {"weight_per_unit": "2.7500"}
    card = build_update_confirmation_card(
        order_type="metal", order_id=10,
        before=erp.orders[10], changes=changes,
        expected_updated_at=erp.orders[10]["updated_at"],
    )
    task = order_task(tasks, duty="R3", structured_intent={"duty": "R3", "order_type": "metal"})
    confirm_light(tasks, task.task_id, card)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )
    assert summary.result_status == "success"
    assert summary.error_code is None
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 前后快照（场景 7：保存更新前快照与更新后订单）
    assert summary.before_snapshot["weight_per_unit"] == "0.0000"
    assert summary.after_snapshot["weight_per_unit"] == "2.7500"
    # ERP 同事务重算金额（规则 5）：2 × 2.75 × 120 = 660.00
    assert summary.after_snapshot["total_amount"] == "660.00"
    assert "total_amount" in summary.changed_fields
    assert summary.other_effects[0]["type"] == "receivable_current_impact"


def test_scenario08_powder_price_fix_payable_impact(uctx) -> None:
    """修正塑粉价格：update_order 更新并返回 payable_current_impact。

    断言口径与 tests/domains/test_orders.py::test_update_powder_price_
    payable_impact（L884）一致。
    """
    erp, tasks, audit, _, client = uctx
    erp.seed_update_order(20, make_powder_order())  # 50 kg × 28.5 = 1425.00
    executor = OrderUpdateExecutor(
        client=client, tasks=tasks, audit=audit, trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE,
        policy=policy_for("R3.order.update.powder"),
    )
    changes = {"price_per_kg": "30.0000"}
    card = build_update_confirmation_card(
        order_type="powder", order_id=20,
        before=erp.orders[20], changes=changes,
        expected_updated_at=erp.orders[20]["updated_at"],
    )
    task = order_task(tasks, duty="R3", structured_intent={"duty": "R3", "order_type": "powder"})
    confirm_light(tasks, task.task_id, card)

    summary = executor.execute(
        task_id=task.task_id, order_type="powder", order_id=20, desired=changes,
    )
    assert summary.result_status == "success"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert summary.after_snapshot["total_amount"] == "1500.00"  # 50 × 30
    assert summary.before_snapshot["total_amount"] == "1425.00"
    assert summary.other_effects[0]["type"] == "payable_current_impact"
    assert summary.other_effects[0]["amount"] == "1500.00"


# ---------------------------------------------------------------------------
# 场景 9-11：R6 对账写入（RV-31/RV-32；深度专项 tests/domains/
# test_reconciliation_execute.py + scripts/d21_execute_demo.py）
# ---------------------------------------------------------------------------


def test_scenario09_r6_write_weight_to_zero_order(rctx14) -> None:
    """R6 型号+日期唯一匹配：把客户清单重量写入原重量 0 的订单（L843）。"""
    erp, tasks, audit, reviews, executor, mappings = rctx14
    items = [update_item(weight="5.00", erp_weight="0")]
    task, plan, review, summary = r6_run(executor, tasks, reviews, items)
    item = summary.items[0]
    assert item.status == "success"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 订单 11（M-ZW，in_date 2026-06-20）重量已更新；金额重算 10×5×12.50=625.00
    assert erp.orders[11]["weight_per_unit"] == "5.00"
    assert erp.orders[11]["total_amount"] == "625.00"
    # 唯一匹配：恰一条写调用（无歧义候选，13.2 不盲重试）
    assert len(write_calls(erp)) == 1


def test_scenario10_r6_recheck_after_review_no_overwrite(rctx14) -> None:
    """复核后目标被他人更新：版本冲突 → 重新生成差异，不覆盖新数据。"""
    erp, tasks, audit, reviews, executor, mappings = rctx14
    erp.apply_other_writer_update(11, {"weight_per_unit": "7.00"})
    items = [update_item(weight="5.00", erp_weight="0")]
    task, plan, review, summary = r6_run(executor, tasks, reviews, items)
    item = summary.items[0]
    assert item.status == ITEM_CONFLICT
    assert item.error_code == ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW
    assert item.refetched_changes == {"weight_per_unit": "5.00"}
    assert erp.orders[11]["weight_per_unit"] == "7.00"  # 不覆盖新数据
    assert write_calls(erp) == []  # 写前检查已拦截，未调用 update_order
    assert summary.conflict_count == 1


def test_scenario11_r6_network_unknown_same_key_no_duplicate(rctx14) -> None:
    """create_order_bundle 网络结果未知：查询 + 同一幂等键处理，不重复创建。"""
    erp, tasks, audit, reviews, executor, mappings = rctx14
    erp.fail_network_unknown(KEY_D24)
    task, plan, review, summary = r6_run(
        executor, tasks, reviews, [create_item()],
        report=make_report(create_diff_entry(row_number=2)), rows=CREATE_ROW,
    )
    item = summary.items[0]
    assert item.status == ITEM_REPLAYED  # 结果未知 → 同 key 查询确认 → replayed
    assert summary.replayed_count == 1
    assert summary.task_status == "succeeded"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # ERP 侧仅 1 单（M-999），未重复创建
    created = [
        o for o in erp.orders.values()
        if o.get("customer_id") == 101 and o.get("model") == "M-999"
    ]
    assert len(created) == 1
    # 两次写调用同一幂等键（13.2：同 key 重试，不换 key）
    calls = write_calls(erp)
    assert len(calls) == 2
    assert all(c.get("idempotency_key") == KEY_D24 for c in calls)


# ---------------------------------------------------------------------------
# 场景 12/13：权限与拒绝路径（10.5/1.4；深度专项 tests/security/* 与
# tests/intent/test_unsupported.py + tests/api/test_developer_api.py）
# ---------------------------------------------------------------------------


def _query_ctx(
    *, user_role: str, scope: frozenset[str], erp, metadata: object | None = None
) -> ToolContext:
    """构造 erp_query 的工具上下文（erp 记录调用，可断言「是否触达 ERP」）。

    ``metadata`` 为 EntityCache：权限门之后依次还有「实体元数据接线」与
    「实体白名单」两道，做正向对照时必须接上，否则会停在半路。
    """
    return ToolContext(
        context=AgentContext(
            user_id="dev01" if user_role == "developer" else "manager01",
            user_role=user_role,
            scope=scope,
        ),
        tasks=None,
        audit=None,
        metadata=metadata,
        sink=None,
        client=erp,
        archive=None,
    )


def test_scenario12_developer_query_fail_closed() -> None:
    """developer 请求查订单：工具层 fail closed（角色校验拒绝执行，10.5）。

    D26 前这道门在意图层（`validate_intent` 按 context 校验）；D26 删除
    规则化意图后，护栏下沉到 `tools/query.py` 的 `run_erp_query` 第一行——
    比原来更靠后也更本质：无论模型怎么决策，非 manager 都绕不过这一行。
    """
    erp = FakeMCPClient()

    result = run_erp_query(
        _query_ctx(user_role="developer", scope=frozenset({"erp:query"}), erp=erp),
        ErpQueryArgs(entity="metal_orders"),
    )
    assert result.ok is False
    assert "manager 角色" in result.summary["error"]
    assert erp.calls == []  # 关键：不触达 ERP

    # 角色对但缺 scope 同样拒绝（2.4 最小 scope 集）
    no_scope = run_erp_query(
        _query_ctx(user_role="manager", scope=frozenset(), erp=erp),
        ErpQueryArgs(entity="metal_orders"),
    )
    assert no_scope.ok is False
    assert erp.calls == []

    # 正向对照：manager + erp:query 通过权限门，确实发起了一次查询。
    # 需要接上 EntityCache——权限门之后依次还有「实体元数据接线」「实体
    # 白名单」两道，缺任一都会停在半路，那样就证明不了门确实是放行的。
    erp_ok = FakeMCPClient()
    ctx_ok = _query_ctx(
        user_role="manager",
        scope=frozenset({"erp:query"}),
        erp=erp_ok,
        metadata=EntityCache(erp_ok),
    )
    ok = run_erp_query(ctx_ok, ErpQueryArgs(entity="metal_orders"))
    assert ok.ok is True
    assert [c["tool"] for c in erp_ok.calls] == ["list_entities", "query"]
    # API 级 403 另由 tests/api/test_developer_api.py 覆盖


def test_scenario13_unsupported_duties_explicitly_refused() -> None:
    """报销/请假审批/联系客户/取消订单：无执行路径（提示词明令 + 无工具）。

    D26 前由规则化分类器判定 `duty=UNSUPPORTED` 并给出拒绝文案。该分类器
    已删，护栏改为两道**仍然存在的**硬约束，本用例逐条断言：

    1. **提示词层面**（10.1 固定基础，每次模型调用都注入）：【工具纪律】
       段声明收支记录只走 finance_create/finance_update 专用工具、不得
       绕过工具自建财务流水（报销）；【安全与信任边界】段明令禁止删除订单
       （取消/作废）、执行报销或请假审批。[2026-09-16 正文重构] 这两组约束
       原先同在 10.1 第 7 条，重构时拆成基础第 4 条（工具纪律）与第 6 条
       （安全与信任边界）；当日用户定稿删去原第 2 条后两者各前移为第 4/6
       条（编号见本文件断言所依赖的当前正文）——两段都是**无条件注入**，
       故护栏强度不变。
    2. **工具层面**：注册表只有职责内业务工具（[2026-09-02 R7 收支写] 后
       R1-R7 域 11 个 + 查询/记忆 + 对话对账的 R6 域工具），「联系客户」
       「取消订单」没有任何可调用工具，模型即使想做也无执行路径（不是靠
       模型自觉）。

    注：D26 下"拒绝"的**措辞**由模型生成（不再是固定文案），因此不断言
    具体拒绝文本——能确定性断言的是"没有执行路径"。
    """
    # [2026-09-16 正文重构] 原先按裸索引取整条 10.1 第 7 条；该条已拆为
    # 【工具纪律】的写操作唯一入口（基础第 4 条）+【安全与信任边界】的权限
    # 边界（基础第 6 条）。改为按**渲染后的固定基础全文**逐条定位这四件事：
    # 断言的对象是「护栏在不在」，与它落在第几条无关，条序再变也不会失效。
    # （裸索引锚定会在条序变动时静默锁到别的条，见 prompt 守护测试假锁教训。）
    joined = "\n".join(BASE_PROMPT_RULES)
    assert "finance_create" in joined and "finance_update" in joined  # 收支走专用工具
    assert "不绕过工具自建财务流水" in joined  # 不绕过工具自建流水（报销）
    assert "不能删除订单" in joined  # 取消/作废订单
    assert "不能执行报销或请假审批" in joined  # 报销 + 请假审批

    # 工具层：不存在任何取消/作废订单或联系客户的工具
    forbidden_fragments = ("cancel", "delete", "void", "contact", "notify", "reimburse")
    for name in TOOL_REGISTRY:
        lowered = name.lower()
        assert not any(f in lowered for f in forbidden_fragments), (
            f"工具注册表出现职责外工具：{name}"
        )
    # 正向对照：业务工具覆盖 R1-R7，职责内路径完整（[2026-08-31 对话对账
    # 批次 2] reconcile_analyze/mapping_template/mapping_list 为 R6 域新增，
    # [2026-09-02 R7 收支写] finance_create/finance_update 为 R7 域新增，
    # 均不含取消/作废/联系客户等职责外片段）
    assert set(TOOL_REGISTRY) == {
        "attendance_plan_execute",  # R1
        "order_create",  # R2
        "order_update",  # R3
        "erp_query",  # R4/R5
        "reconciliation_execute",  # R6（复核后写入）
        "reconcile_analyze",  # R6（只读对账分析）
        "mapping_template",  # R6（映射模板写）
        "mapping_list",  # R6（映射版本只读查询）
        "attachment_preview",  # [2026-09-11] R6（对账前只读预览附件内容）
        "export_data",  # R4（导出字段规则）
        "finance_create",  # R7（收支记录创建）
        "finance_update",  # R7（收支记录更新）
        "memory_lookup",
        "list_entities",  # [2026-09-09 架构改造第一步] 能力目录直挂（只读）
        "erp_calc",  # [2026-09-09 架构改造第一步] 一体式查+算（只读）
        "erp_salary_calc",  # [2026-09-13] 工资口径专用计算（只读）
        "render_financial_report",  # [2026-09-13] 财报固定渲染（只读，产正文）
        "render_tax_worksheet",  # 当期增值税估算固定渲染（只读，产正文）
        "rag_remember",  # [2026-09-22] RAG 长期记忆写入（软删归 rag_forget，非订单/流水作废）
        "rag_recall",  # [2026-09-22] RAG 长期记忆语义召回（只读）
        "rag_forget",  # [2026-09-22] RAG 长期记忆遗忘（软删，非 ERP 写）
        "rag_search_documents",  # [2026-10] RAG 公司共享知识库文档检索（只读）
    }


# ---------------------------------------------------------------------------
# 场景 12 配套：run_erp_query 续查合并/去重行为（tools/query.py 去重循环
# 回归；[2026-09-09] 修复 fields 不含 id 时全部行塌缩成 1 行的 bug）
# ---------------------------------------------------------------------------


class PagedQueryErp:
    """run_erp_query 直连假 ERP（不经 respx，同场景 12 FakeMCPClient 形态）：
    list_entities 元数据 + query 真分页（offset/limit 窗口）+ 字段投影
    （fields 缺省行字段，模拟 ERP 只回所选列）+ 可注入跨页重复
    （drift_repeat：后续页开头重复上一窗口尾部行，模拟续查期间 ERP 并发
    变更）。D5：测试夹具，不代表 ERP 真实运行态。"""

    ENTITY_META: dict = {
        "name": "metal_orders",
        "label": "钣金订单",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            name: {"type": type_, "label": label}
            for name, type_, label in [
                ("id", "int", "ID"),
                ("customer_id", "int", "客户 ID"),
                ("model", "string", "型号"),
                ("in_date", "date", "进厂日期"),
                ("quantity", "int", "数量"),
                ("weight_per_unit", "decimal", "单重"),
                ("status", "string", "状态"),
            ]
        },
    }

    def __init__(self, rows: list[dict], *, drift_repeat: int = 0):
        self.rows = rows  # 全量行（含 id；投影前为完整记录）
        self.drift_repeat = drift_repeat  # 每页开头重复上一窗口尾部行数
        self.calls: list[dict] = []

    def call_tool(
        self, tool_name: str, arguments: dict | None = None,
        operation_id: str | None = None, trace_id: str | None = None,
    ) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": [self.ENTITY_META]})
        if tool_name == "query":
            args = arguments or {}
            offset = args.get("offset") or 0
            window = args.get("limit") or 50
            fields = args.get("fields")
            full = self._window(offset, window)
            items = [
                {k: r[k] for k in fields} if fields else dict(r)
                for r in full
            ]
            return ToolResult(
                ok=True,
                data={
                    "items": items,
                    "total": len(self.rows),
                    "limit": window,
                    "offset": offset,
                },
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(
        self, operation_id: str | None = None, trace_id: str | None = None
    ) -> ToolResult:
        """EntityCache 刷新入口（同 FakeMCPClient 形态：委托 call_tool）。"""
        return self.call_tool("list_entities", {})

    def _window(self, offset: int, limit: int) -> list[dict]:
        """当前 offset 窗口的全量行（offset>0 且配置了漂移时，开头补上一
        窗口尾部 drift_repeat 行——ERP 并发变更下续查窗口可能重叠）。"""
        if offset == 0 or self.drift_repeat <= 0:
            return list(self.rows[offset : offset + limit])
        start = max(0, offset - self.drift_repeat)
        return list(self.rows[start : offset + limit])


def _metal_rows(n: int) -> list[dict]:
    """n 行全字段钣金订单（id 1..n、model 唯一 M-xxxx，供跨页防丢断言）。"""
    return [
        {
            "id": i,
            "customer_id": 101,
            "model": f"M-{i:04d}",
            "in_date": "2026-08-01",
            "quantity": 10 + i,
            "weight_per_unit": "12.5000",
            "status": "完成",
        }
        for i in range(1, n + 1)
    ]


#: 模型自选字段（白名单内合法、不含 id——8 月钣金实测复现 bug 的查询形态）
_QUERY_FIELDS_NO_ID = ["customer_id", "model", "in_date", "quantity", "weight_per_unit"]


def _query_ctx_ready(erp) -> ToolContext:
    """manager + erp:query + 元数据接线完备的查询上下文（复用 _query_ctx）。"""
    return _query_ctx(
        user_role="manager",
        scope=frozenset({"erp:query"}),
        erp=erp,
        metadata=EntityCache(erp),
    )


def test_erp_query_fields_without_id_single_page_no_collapse() -> None:
    """fields 不含 id 时单页多行不塌缩（[2026-09-09] bug 复现形态：去重 key
    全为 str(None)='None'，165 行被并成 1 行，ERP total=165 却正确）。"""
    erp = PagedQueryErp(_metal_rows(3))
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", fields=_QUERY_FIELDS_NO_ID),
    )
    assert result.ok
    items = result.summary["items"]
    assert result.summary["count"] == 3
    assert len(items) == 3
    assert all("id" not in r for r in items)  # ERP 按 fields 投影，确实无 id
    assert [r["model"] for r in items] == ["M-0001", "M-0002", "M-0003"]  # 原序全量


def test_erp_query_fields_without_id_multi_page_keeps_all_rows() -> None:
    """fields 不含 id 时跨页续查合并：行数正确且不丢弃/不重复任何行。

    无 id 的行无法识别实体（同实体跨页重复无从判定），一律保留原序合并
    ——250 行 5 页取齐：count=250、无 truncated、每轮 fields 原样透传。
    """
    erp = PagedQueryErp(_metal_rows(250))
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", fields=_QUERY_FIELDS_NO_ID),
    )
    assert result.ok
    items = result.summary["items"]
    assert result.summary["count"] == 250
    assert len(items) == 250
    assert len({r["model"] for r in items}) == 250  # 无任何行被丢弃
    assert [r["model"] for r in items] == [f"M-{i:04d}" for i in range(1, 251)]  # 原序
    assert "truncated" not in result.summary
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert len(query_calls) == 5  # 窗口缺省 50 → 250 行真分页 5 轮
    assert all(
        set(c["arguments"]["fields"]) == set(_QUERY_FIELDS_NO_ID)
        for c in query_calls
    )


def test_erp_query_dedup_still_applies_when_id_present_across_pages() -> None:
    """回归保护：fields 含 id 时跨页同 id 重复仍按 id 去重（首次出现顺序）。

    90 行、窗口 50、第 2 页开头重复上一窗口尾部 10 行（drift_repeat=10，
    续查期间 ERP 并发变更防御的真实场景）→ 合并 100 行按 id 去重回 90 行。
    """
    erp = PagedQueryErp(_metal_rows(90), drift_repeat=10)
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", fields=["id", "model", "quantity"]),
    )
    assert result.ok
    items = result.summary["items"]
    assert result.summary["count"] == 90
    assert len(items) == 90
    assert [r["id"] for r in items] == list(range(1, 91))  # 去重保留首次出现序
    assert "truncated" not in result.summary
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert len(query_calls) == 2


def test_erp_query_order_by_desc_list_and_limit_cap() -> None:
    """[2026-09-10 P0-2/P3-30] order_by 接受降序前缀（-field）与数组形态并
    原样透传（ERP _parse_order_by 契约：字符串或字符串数组，- 前缀降序）；
    limit 上限收紧 200（与工具描述/实现 min(limit, 200) 一致——>200 由参数
    校验拒绝，不再进入「描述 200 / schema 500」不一致的静默降级面）。"""
    erp = PagedQueryErp(_metal_rows(3))
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", order_by=["status", "-in_date"]),
    )
    assert result.ok
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert query_calls and query_calls[0]["arguments"]["order_by"] == [
        "status", "-in_date",
    ]

    # 单字符串降序形态原样透传
    erp2 = PagedQueryErp(_metal_rows(3))
    result2 = run_erp_query(
        _query_ctx_ready(erp2),
        ErpQueryArgs(entity="metal_orders", order_by="-id"),
    )
    assert result2.ok
    calls2 = [c for c in erp2.calls if c["tool"] == "query"]
    assert calls2[0]["arguments"]["order_by"] == "-id"

    # limit 上限 200：超出由 pydantic 参数校验拒绝（fail closed 不静默）
    with pytest.raises(ValidationError):
        ErpQueryArgs(entity="metal_orders", limit=201)


class _NonOrderQueryErp:
    """仅含非订单实体（customers）元数据的最小假 ERP（验证非订单实体不注入
    默认序）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_tool(self, tool_name, arguments=None, operation_id=None, trace_id=None):
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": [{
                "name": "customers", "label": "客户", "readable": True,
                "fields": {
                    "id": {"type": "int", "label": "ID"},
                    "company_name": {"type": "string", "label": "公司名"},
                },
            }]})
        if tool_name == "query":
            return ToolResult(ok=True, data={
                "items": [], "total": 0, "limit": 50, "offset": 0,
            })
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(self, operation_id=None, trace_id=None):
        return self.call_tool("list_entities", {})


def test_erp_query_default_order_by_in_date_for_order_entities() -> None:
    """[2026-09-10 排序口径/P2-4] 订单类实体未指定 order_by → Agent 侧默认
    注入数组 ["-in_date","-id"]（入库日期降序 + id 稳定 tiebreaker，避免同日
    记录翻页重复/漏行）；显式 order_by 优先不被覆盖；非订单实体不注入
    （走 ERP 实体默认序）。"""
    from administrative_agent.agent.tools.query import default_order_by

    # 订单类（metal_orders）：wire 级断言实际发出的 query 带数组默认序
    # （JSON 序列化后为 list：JSON 数组）
    erp = PagedQueryErp(_metal_rows(3))
    result = run_erp_query(_query_ctx_ready(erp), ErpQueryArgs(entity="metal_orders"))
    assert result.ok
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert query_calls and query_calls[0]["arguments"]["order_by"] == ["-in_date", "-id"]
    # tiebreaker 确实存在于默认序（不止 in_date 单键）
    assert len(query_calls[0]["arguments"]["order_by"]) == 2
    assert query_calls[0]["arguments"]["order_by"][-1] == "-id"

    # 显式 order_by 永远优先，不被默认覆盖
    erp2 = PagedQueryErp(_metal_rows(3))
    run_erp_query(
        _query_ctx_ready(erp2), ErpQueryArgs(entity="metal_orders", order_by="-id"),
    )
    calls2 = [c for c in erp2.calls if c["tool"] == "query"]
    assert calls2[0]["arguments"]["order_by"] == "-id"

    # 非订单实体：wire 级不注入 order_by
    non_order = _NonOrderQueryErp()
    r3 = run_erp_query(
        _query_ctx(
            user_role="manager", scope=frozenset({"erp:query"}),
            erp=non_order, metadata=EntityCache(non_order),
        ),
        ErpQueryArgs(entity="customers"),
    )
    assert r3.ok
    q3 = [c for c in non_order.calls if c["tool"] == "query"]
    assert q3 and "order_by" not in q3[0]["arguments"]

    # 默认序映射：订单类 ["-in_date","-id"]；其余 None
    assert default_order_by("metal_orders") == ["-in_date", "-id"]
    assert default_order_by("powder_orders") == ["-in_date", "-id"]
    for entity in ("customers", "suppliers", "employees", "transactions",
                   "transaction_categories"):
        assert default_order_by(entity) is None


def test_export_data_injects_default_order_by_tiebreaker(monkeypatch) -> None:
    """[2026-09-10 P2-4] export_data 与 erp_query 同源默认序：订单类实体未
    指定 order_by 时注入 ["-in_date","-id"]（tiebreaker 防同日记录翻页漏行）；
    显式 order_by 优先；非订单实体不注入。此处不落盘，monkeypatch
    ExcelExporter.export 只捕获注入的 order_by 参数（wire 级）。"""
    from dataclasses import replace

    from administrative_agent.agent.tools.export import ExportDataArgs, run_export_data
    from administrative_agent.domains import export as export_mod

    captured: list[dict] = []

    def fake_export(self, *, entity, fields, filter, order_by, filename,
                    sensitive_fields=None):
        captured.append({"entity": entity, "order_by": order_by})
        raise export_mod.ExportError("stop-after-capture")

    monkeypatch.setattr(export_mod.ExcelExporter, "export", fake_export)

    def _ctx():
        base = _query_ctx(
            user_role="manager", scope=frozenset({"erp:query"}),
            erp=PagedQueryErp(_metal_rows(1)), metadata=object(),
        )
        return replace(base, audit=object())

    # 订单类默认注入数组序（JSON 数组形态）
    r1 = run_export_data(_ctx(), ExportDataArgs(entity="metal_orders"))
    assert r1.ok is False  # fake export 主动抛错，仅捕获参数
    assert captured[-1]["order_by"] == ["-in_date", "-id"]
    assert captured[-1]["order_by"][-1] == "-id"

    # 显式 order_by 优先
    run_export_data(_ctx(), ExportDataArgs(entity="metal_orders", order_by="-id"))
    assert captured[-1]["order_by"] == "-id"

    # 非订单实体不注入
    run_export_data(_ctx(), ExportDataArgs(entity="customers"))
    assert captured[-1]["order_by"] is None


# ---------------------------------------------------------------------------
# [根因 A 全量取数] erp_query 空结果回显（empty_note）+ 截断续查路径
# ---------------------------------------------------------------------------


class _FilterAwareQueryErp:
    """run_erp_query 直连假 ERP：在 PagedQueryErp 基础上支持 filter
    （等值 + 操作符子集 eq/lt，mcp-api.md §4），使「0 行」确实由 filter 造成，
    用于验证空结果回显（A1）与 fetch_all=false+offset 补足截断（A2）。
    D5：测试夹具，不代表 ERP 真实运行态。"""

    ENTITY_META = PagedQueryErp.ENTITY_META

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def call_tool(self, tool_name, arguments=None, operation_id=None, trace_id=None):
        arguments = arguments or {}
        self.calls.append({"tool": tool_name, "arguments": dict(arguments)})
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": [self.ENTITY_META]})
        assert tool_name == "query", f"unexpected tool: {tool_name}"
        rows = [r for r in self.rows if self._match(r, arguments.get("filter"))]
        offset = arguments.get("offset") or 0
        window = arguments.get("limit") or 50
        page = rows[offset : offset + window]
        fields = arguments.get("fields")
        if fields:
            page = [{k: r[k] for k in fields if k in r} for r in page]
        return ToolResult(
            ok=True,
            data={
                "items": [dict(r) for r in page],
                "total": len(rows),
                "limit": window,
                "offset": offset,
            },
        )

    def list_entities(self, operation_id=None, trace_id=None):
        return self.call_tool("list_entities", {})

    @staticmethod
    def _match(row: dict, filt: dict | None) -> bool:
        if not filt:
            return True
        for field, cond in filt.items():
            value = row.get(field)
            if isinstance(cond, dict):
                for op, target in cond.items():
                    if op == "eq" and value != target:
                        return False
                    if op == "lt" and not (str(value) < str(target)):
                        return False
                    if op not in ("eq", "lt"):
                        raise AssertionError(f"unexpected op {op!r}")
            elif value != cond:
                return False
        return True


class _ComputedFilterErp(_FilterAwareQueryErp):
    """元数据含**计算列**（``computed: True``）的假 ERP：验证 filter 仅含计算列
    键时被本地剔除（计算列不可用于 filter，见 query.py ``non_computed_fields``）
    ——下发 wire 无 filter 键、空结果走「无筛选全表」文案。D5：测试夹具。"""

    ENTITY_META = {
        **PagedQueryErp.ENTITY_META,
        "fields": {
            **PagedQueryErp.ENTITY_META["fields"],
            "total_amount": {"type": "decimal", "label": "总金额", "computed": True},
        },
    }


def test_erp_query_empty_result_echoes_applied_filter() -> None:
    """A1：有 filter 且结果 0 行 → 新增 empty_note 回显本次生效的 filter，并
    明确声明这是过滤结果、不代表该实体没有数据（防「自造空条件 → 当全表为空
    → 反过来质疑用户数据」的根因 A 事故）。"""
    erp = _FilterAwareQueryErp(_metal_rows(3))
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", filter={"id": {"lt": 1}}),
    )
    assert result.ok
    assert result.summary["count"] == 0
    note = result.summary["empty_note"]
    # 回显实际生效的 filter（原样 JSON）
    assert '"id"' in note and '"lt": 1' in note
    assert "不代表该实体没有数据" in note
    # 有 filter 时不得说成「全表查询确为 0 行」（那是无筛选口径）
    assert "全表查询确为 0 行" not in note
    # 只加字段：现有键形状不变
    assert result.summary["items"] == []


def test_erp_query_empty_note_echoes_filter_and_offset_together() -> None:
    """G1：filter 与 offset **同时存在**且 0 行时，文案必须**同时**回显 filter
    与 offset，并说明「0 行也可能是 offset 越过末尾所致」。

    旧实现三分支为 ``if filt: … elif applied_offset: …``，此形态（A2 续查典型
    形态：同一 filter + fetch_all=false + offset=<已得行数>）只走筛选分支、
    全文无 offset 字样——模型会被告知「去掉 filter 重查」，而真因是 offset
    越界，至少白跑一轮，最坏删掉一个本来正确的 filter 重新拉全表。
    """
    erp = _FilterAwareQueryErp(_metal_rows(3))
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(
            entity="metal_orders",
            filter={"id": {"lt": 1}},
            fetch_all=False,
            offset=10,
        ),
    )
    assert result.ok
    assert result.summary["count"] == 0
    note = result.summary["empty_note"]
    # 同时回显 filter（原样 JSON）与 offset
    assert '"id"' in note and '"lt": 1' in note
    assert "offset=10" in note
    # 说明 offset 越过末尾也可能是 0 行成因（不得只归因到 filter）
    assert "越过" in note
    # 有 filter 时仍不得说成「全表查询确为 0 行」
    assert "全表查询确为 0 行" not in note


def test_erp_query_computed_only_filter_dropped_not_echoed() -> None:
    """G3a：filter **仅含计算列键**时被本地剔除（计算列不可用于 filter）→
    实际下发 wire 无 filter 键，空结果走「无筛选全表」文案，不得把被忽略的
    filter 当成生效筛选条件回显。

    **本用例只钉到这一层**：filter 仅含计算列键 → 裁剪后 ``filt is None`` →
    ``run_erp_query`` 里 ``if filt:`` 分支根本不进入 → 那行回显代码从未执行，
    故它**判别不了**「回显裁剪后 filter vs 回显模型原话」这个变量（把回显源
    改成 ``args.filter`` 本用例仍绿——假锁）。该变量由
    ``test_erp_query_empty_note_echoes_cropped_filter_not_raw_args``（filter
    同时含保留键与计算列键，使分支真进入）承担。
    """
    erp = _ComputedFilterErp([])  # 空表 + 仅计算列 filter
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(entity="metal_orders", filter={"total_amount": {"gte": 1}}),
    )
    assert result.ok
    assert result.summary["count"] == 0
    # wire 级：计算列 filter 被本地剔除，不下发 filter 键
    q = [c for c in erp.calls if c["tool"] == "query"]
    assert q and "filter" not in q[0]["arguments"]
    # 被忽略的计算列 filter 不算生效条件 → 走无筛选全表文案，不回显该键
    note = result.summary["empty_note"]
    assert "全表查询确为 0 行" in note
    assert "total_amount" not in note
    # 剔除项经 note 反馈模型（计算列不可用于 filter）
    assert "已忽略计算列筛选键" in result.summary["note"]


def test_erp_query_empty_note_echoes_cropped_filter_not_raw_args() -> None:
    """G3a 重做：filter **同时含**会被裁掉的计算列键（``total_amount``）与
    会被保留的普通键（``id``）时，``filt`` 裁剪后仍非空 → ``if filt:`` 分支
    **真进入**、回显代码行**真执行** → 才判别得了「回显的是裁剪后实际下发的
    filter，而非模型原话」。

    与 ``test_erp_query_computed_only_filter_dropped_not_echoed`` 的关键差别：
    那条的 filter 仅含计算列键，裁剪后 ``filt is None``、分支不进入、回显行
    从未执行（假锁）。本条把回显源从 ``filt`` 改成 ``args.filter`` 时，
    ``empty_note`` 会混入被剔除的 ``total_amount`` → 断言 ``"total_amount"
    not in empty_note`` 必红，这才是区分「裁剪后 vs 原话」的唯一判据。

    夹具：``_ComputedFilterErp`` 元数据里 ``total_amount`` 打 ``computed: True``
    （不可用于 filter），``id`` 是普通字段；``id lt 1`` 必然 0 行。
    """
    erp = _ComputedFilterErp([])
    result = run_erp_query(
        _query_ctx_ready(erp),
        ErpQueryArgs(
            entity="metal_orders",
            filter={"total_amount": 0, "id": {"lt": 1}},
        ),
    )
    assert result.ok
    assert result.summary["count"] == 0
    # wire 级：只剔除计算列键，被保留的普通键仍下发（filt 非空 → 分支进入）
    q = [c for c in erp.calls if c["tool"] == "query"]
    assert q and q[0]["arguments"]["filter"] == {"id": {"lt": 1}}
    # empty_note 回显的是**裁剪后实际下发的 filter**：含保留键、不含被裁掉的
    # 计算列键（回显源若改成模型原话 args.filter，则 total_amount 混入 → 红）
    note = result.summary["empty_note"]
    assert '"id"' in note and '"lt": 1' in note
    assert "total_amount" not in note
    # 剔除项经 note 反馈模型（计算列不可用于 filter）
    assert "已忽略计算列筛选键" in result.summary["note"]


def test_erp_query_empty_full_table_note_distinct() -> None:
    """A1：无 filter 的全表查询 0 行 → 文案不同（说明全表确为 0 行），不得
    归因到「你的条件」（本就没有条件）。"""
    erp = _FilterAwareQueryErp([])
    result = run_erp_query(_query_ctx_ready(erp), ErpQueryArgs(entity="metal_orders"))
    assert result.ok
    assert result.summary["count"] == 0
    note = result.summary["empty_note"]
    assert "全表查询确为 0 行" in note
    assert "你的条件" not in note
    assert "筛选条件" not in note  # 无 filter 时不提筛选条件


def test_erp_query_no_empty_note_when_rows_present() -> None:
    """A1：有行时**不得**出现 empty_note（不改变现有 payload 形状）。"""
    erp = _FilterAwareQueryErp(_metal_rows(3))
    result = run_erp_query(_query_ctx_ready(erp), ErpQueryArgs(entity="metal_orders"))
    assert result.ok
    assert result.summary["count"] == 3
    assert "empty_note" not in result.summary


def test_erp_query_supplement_truncated_tail_via_fetch_all_false_offset() -> None:
    """A2：fetch_all 默认续查至 500 行即 truncated（total=534）；补足剩余行
    的路径 = fetch_all=false + offset=<已得行数>（截断时 fetch_all=true 会在
    500 行上限处再次截断），能正确取回尾部剩余行。

    [根因 A 全量取数 · 假锁修复] 单页窗口必须**小于剩余行数**（此处
    offset=500、limit=10、剩余 34 > 10），否则「fetch_all=false 只取一页就
    停」与「fetch_all 被完全忽略」结果完全相同——旧写法用默认窗口 50
    （剩余 34 < 50）时，即便删掉终止条件里的 ``not args.fetch_all`` 本用例
    仍全绿（count=34/ids/offset 全过）。判据：删掉该保险丝后本用例必红。
    """
    erp = _FilterAwareQueryErp(_metal_rows(534))
    first = run_erp_query(_query_ctx_ready(erp), ErpQueryArgs(entity="metal_orders"))
    assert first.ok
    assert first.summary["count"] == 500
    assert first.summary["total"] == 534
    assert first.summary["truncated"] is True

    # 续查剩余：关掉 fetch_all、从已得行数起翻页，窗口 10 < 剩余 34
    erp2 = _FilterAwareQueryErp(_metal_rows(534))
    tail = run_erp_query(
        _query_ctx_ready(erp2),
        ErpQueryArgs(entity="metal_orders", fetch_all=False, offset=500, limit=10),
    )
    assert tail.ok
    assert tail.summary["count"] == 10  # 只取一页（未自作主张续查到底）
    assert tail.summary["total"] == 534
    assert "truncated" not in tail.summary
    assert [r["id"] for r in tail.summary["items"]] == list(range(501, 511))
    # wire 级：确实以 offset=500、limit=10、fetch_all 关发起**且只发一次**
    # （fetch_all=false 单页即停，不自动续查——剩余 34 > 窗口 10 时方可判别）
    q = [c for c in erp2.calls if c["tool"] == "query"]
    assert len(q) == 1
    assert q[0]["arguments"]["offset"] == 500
    assert q[0]["arguments"]["limit"] == 10
