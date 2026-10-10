"""R2 钣金订单创建测试（RV-19/RV-20；规格 7.3 六要素、L543 自动补全
展示、5.3 步骤 3-8、9.3、10.4 顺序 7/11、14.3 场景 11）。

- RV-19（D13）：卡片六要素、载荷哈希稳定与变化失效（7.3）、确认有效性
  判定、参数快照（单元交付：与卡片一致性 + total_amount 排除）。
- RV-20（D14）：幂等键格式/唯一/复用（mcp-api §6.1，5.3 步骤 3-4）、
  执行器四分支（success/replayed/conflict/failed）、网络结果未知同 key
  重试（14.3 场景 11）、禁止字段 fail closed（9.3）、审计 result 事件
  与 replayed 指标（11.2/11.5）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.audit.service import EVENT_MCP_RETRY, TaskAttemptRecord
from administrative_agent.business.order_rules import (
    SOURCE_CUSTOMER_PROFILE,
    SOURCE_DEFAULT,
    CustomerProfile,
    MetalOrderInput,
    complete_metal_order,
)
from administrative_agent.domains.orders import (
    BATCH_HASH_PREFIX,
    CONFIRMATION_FIELD_LABELS,
    ConfirmationCard,
    ERROR_FORBIDDEN_FIELD,
    ERROR_IN_PROGRESS,
    ERROR_KEY_CONFLICT,
    ERROR_MCP_FAILURE,
    ERROR_NETWORK_UNKNOWN,
    OP_CREATE_METAL_ORDER,
    MetalOrderExecutor,
    OrderExecutionSummary,
    OrderParameterSnapshot,
    build_metal_confirmation_card,
    build_parameter_snapshot,
    generate_idempotency_key,
    is_confirmation_valid,
    is_valid_idempotency_key,
    payload_hash_for,
    resolve_idempotency_key,
)
from administrative_agent.mcp.errors import MCPError
from administrative_agent.persistence.models import TaskStatus
from administrative_agent.tasks.service import TaskNotRunningError
from tests.domains.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    TRACE_ID,
    create_task,
)

TODAY = "2026-08-22"
CUSTOMER_ID = 11
CUSTOMER_NAME = "示例机械有限公司"


def L(field: str) -> str:
    """确认卡字段的展示名。

    [2026-08-28 确认卡字段中文化] 后，展示层（fields_to_write / auto_filled /
    参数快照）使用中文标签，而执行载荷（MetalOrderDraft.to_write_dict 等）仍保持
    ERP 英文键契约。这里经 CONFIRMATION_FIELD_LABELS 取展示名而非硬编码中文：
    标签再调整时测试不会碎，同时保留断言的英文语义可读性。
    """
    return CONFIRMATION_FIELD_LABELS[field]


from tests.conftest import mcp_max_retries  # noqa: F401  （跨目录共享，供本模块与同目录用例引用）


def make_draft(**overrides) -> object:
    """构造补全草稿：用户提供单价/数量，价格类型与单位走资料补全。"""
    fields = dict(
        customer_name=CUSTOMER_NAME,
        model="MK-01",
        unit_price=120.0,
        quantity=2,
    )
    fields.update(overrides)
    # [2026-09-08 口径修正] 计价单位在主数据存 price_unit（元/kg），
    # weight_unit 由映射得出（complete_metal_order 内部换算）
    profile = CustomerProfile(price_type="含税", price_unit="元/kg")
    return complete_metal_order(
        MetalOrderInput(**fields), customer_id=CUSTOMER_ID,
        customer_name=CUSTOMER_NAME, profile=profile, today=TODAY,
    )


def make_card():
    return build_metal_confirmation_card(make_draft())


# ---------------------------------------------------------------------------
# 卡片六要素（7.3 L875-886）
# ---------------------------------------------------------------------------

def test_card_has_six_elements_plus_hash() -> None:
    """卡片含六要素 + payload_hash（可展示/审计）。

    [2026-09-10 确认卡改版] 要素 2 的客户名称改以 fields_to_write 首行
    「客户」呈现，target 置空。
    """
    card = make_card()
    assert card.operation_type == OP_CREATE_METAL_ORDER
    assert card.target == ""
    assert card.fields_to_write
    assert card.auto_filled
    assert card.expected_impact
    assert card.limitations
    assert card.payload_hash


def test_card_target_empty_and_customer_name_is_first_write_row() -> None:
    """要素 2：[2026-09-10 确认卡改版] 钣金创建卡 target 置空（前端不渲染
    目标行），客户名称作 fields_to_write 首行「客户」，值是纯名称（不是 id、
    不带「客户 」前缀）；hash_target 仍取旧串以保持哈希等价。"""
    card = make_card()
    assert card.target == ""
    assert card.fields_to_write[0] == ("客户", CUSTOMER_NAME)
    assert card.hash_target == f"客户 {CUSTOMER_NAME}"
    # 展示层本地化不污染名称（ACTION_VALUE_LABELS 只匹配 created/updated/
    # unchanged，人名/公司名原样）
    assert dict(card.fields_to_write)["客户"] == CUSTOMER_NAME
    assert "customer_id" not in card.target
    assert "customer_id" not in dict(card.fields_to_write)


def test_card_fields_to_write_hides_internal_ids() -> None:
    """要素 3：将要写入的字段和值（不含 total_amount 与内部关联主键）。

    [2026-09-10 展示口径] customer_id 不进卡片展示（用户记不住 id）；但
    执行载荷（draft.to_write_dict()）仍保留 customer_id——仅展示层过滤，
    写入 ERP 的真实参数不变。
    """
    fields = dict(make_card().fields_to_write)
    assert L("customer_id") not in fields
    assert fields[L("model")] == "MK-01"
    assert fields[L("unit_price")] == 120.0
    assert fields[L("quantity")] == 2
    assert fields[L("status")] == "未完成"
    # total_amount 是 ERP 派生金额，既不写入也不参与展示层本地化，按原键断言
    assert "total_amount" not in fields
    # 执行载荷不受展示过滤影响：customer_id 照常写入 ERP
    assert make_draft().to_write_dict()["customer_id"] == CUSTOMER_ID


def test_card_auto_filled_with_sources() -> None:
    """要素 4（L543）：自动补全字段及来源（来源/旧值/新值）。"""
    card = make_card()
    by_field = {f.field: f for f in card.auto_filled}
    assert by_field[L("price_type")].source == SOURCE_CUSTOMER_PROFILE
    assert by_field[L("price_type")].value == "含税"
    assert by_field[L("price_type")].previous_value is None
    assert by_field[L("weight_unit")].source == SOURCE_CUSTOMER_PROFILE
    assert by_field[L("quantity_unit")].source == SOURCE_DEFAULT
    assert by_field[L("quantity_unit")].value == "套"
    assert by_field[L("weight_per_unit")].source == SOURCE_DEFAULT
    assert by_field[L("weight_per_unit")].value == 0
    assert by_field[L("in_date")].source == SOURCE_DEFAULT
    assert by_field[L("in_date")].value == TODAY
    assert by_field[L("status")].source == SOURCE_DEFAULT


def test_card_expected_impact_mentions_no_financial_ledger() -> None:
    """要素 5（5.3 步骤 8）：创建"未完成"订单；不建 ar/ap/transactions。"""
    impact = make_card().expected_impact
    assert "未完成" in impact
    assert "ar_payments" in impact
    assert "ap_payments" in impact
    assert "transactions" in impact


def test_card_limitations_covers_erp_computed_fields() -> None:
    """要素 6：ERP 计算项与待补充项的说明。"""
    limitations = " ".join(make_card().limitations)
    assert "total_amount" in limitations
    assert "0" in limitations  # weight_per_unit=0 仅表示待补充


def test_card_to_dict_roundtrip() -> None:
    """to_dict 结构化视图：六要素 + 哈希齐全。"""
    data = make_card().to_dict()
    assert data["operation_type"] == OP_CREATE_METAL_ORDER
    # [2026-09-10 确认卡改版] target 置空；客户名称进 fields_to_write 首行
    assert data["target"] == ""
    assert set(data["fields_to_write"]) == {
        "客户",
        L("model"), L("unit_price"), L("price_type"),
        L("weight_unit"), L("quantity"), L("quantity_unit"),
        L("weight_per_unit"), L("in_date"), L("status"),
    }
    assert data["fields_to_write"]["客户"] == CUSTOMER_NAME
    assert data["auto_filled"]
    assert data["expected_impact"]
    assert data["limitations"]
    assert data["payload_hash"] == make_card().payload_hash


# ---------------------------------------------------------------------------
# 载荷哈希：稳定与变化失效（7.3）
# ---------------------------------------------------------------------------

def test_payload_hash_deterministic() -> None:
    """同义载荷 → 相同哈希（规范化 JSON）。"""
    fields = (("a", 1), ("b", "x"))
    assert payload_hash_for("op", "t", fields) == payload_hash_for("op", "t", fields)


def test_payload_hash_changes_on_field_value() -> None:
    """任一写值变化 → 哈希变化（自动补全值变化即失效）。"""
    card = make_card()
    other = build_metal_confirmation_card(make_draft(unit_price=130.0))
    assert card.payload_hash != other.payload_hash
    other2 = build_metal_confirmation_card(make_draft(quantity=3))
    assert card.payload_hash != other2.payload_hash


def test_payload_hash_changes_on_target() -> None:
    """目标记录变化 → 哈希变化。"""
    card = make_card()
    other = build_metal_confirmation_card(
        complete_metal_order(
            MetalOrderInput(customer_name="另一客户", model="MK-01",
                            unit_price=120.0, quantity=2),
            customer_id=99, customer_name="另一客户",
            profile=CustomerProfile(price_type="含税", price_unit="元/kg"),
            today=TODAY,
        )
    )
    assert card.payload_hash != other.payload_hash


def test_confirmation_valid_when_hash_matches() -> None:
    """确认时哈希与卡片一致 → 有效（is_confirmation_valid=True）。"""
    card = make_card()
    assert is_confirmation_valid(card, card.payload_hash) is True


def test_confirmation_invalid_when_key_fields_changed() -> None:
    """确认后写值变化 → 失效（7.3：变化失效必须重新确认）。"""
    confirmed_hash = make_card().payload_hash
    changed_card = build_metal_confirmation_card(
        make_draft(unit_price=130.0)  # 关键字段（单价）变化
    )
    assert is_confirmation_valid(changed_card, confirmed_hash) is False


def test_confirmation_invalid_on_stale_hash() -> None:
    """任意不匹配哈希 → 失效（不信任旧确认）。"""
    card = make_card()
    assert is_confirmation_valid(card, "0" * 64) is False
    assert is_confirmation_valid(card, "") is False


# ---------------------------------------------------------------------------
# 参数快照（单元交付：确认卡片 + 参数快照）
# ---------------------------------------------------------------------------

def test_parameter_snapshot_matches_card() -> None:
    """快照参数与卡片写值一致、哈希可对账、total_amount 恒排除。"""
    card = make_card()
    snapshot = build_parameter_snapshot(card, task_id="t-1")
    assert isinstance(snapshot, OrderParameterSnapshot)
    assert snapshot.task_id == "t-1"
    assert snapshot.operation_type == OP_CREATE_METAL_ORDER
    assert snapshot.target == card.target
    assert snapshot.parameters == dict(card.fields_to_write)
    assert snapshot.auto_filled == card.auto_filled
    assert snapshot.payload_hash == card.payload_hash
    assert snapshot.total_amount_excluded is True
    assert "total_amount" not in snapshot.parameters
    assert snapshot.created_at  # ISO 时间戳


def test_parameter_snapshot_created_at_injectable() -> None:
    """created_at 可注入固定时间戳（确定性测试）。"""
    card = make_card()
    now = datetime(2026, 8, 22, 10, 30, 0)
    snapshot = build_parameter_snapshot(card, task_id="t-2", now=now)
    assert snapshot.created_at == "2026-08-22T10:30:00"


def test_parameter_snapshot_to_dict() -> None:
    """快照 to_dict：参数/来源/哈希/排除标记齐全。"""
    card = make_card()
    data = build_parameter_snapshot(card, task_id="t-3").to_dict()
    assert data["task_id"] == "t-3"
    assert data["parameters"][L("model")] == "MK-01"
    assert data["auto_filled"]
    assert data["payload_hash"] == card.payload_hash
    assert data["total_amount_excluded"] is True


# ---------------------------------------------------------------------------
# 参数快照 · 批量卡按条归档（[2026-09-12 一张卡多条明细] 阶段 6）
# ---------------------------------------------------------------------------

def _batch_card(
    labels_and_entries: tuple[tuple[str, dict], ...],
) -> ConfirmationCard:
    """构造一张批量卡（hash_fields 外层键 = detail-{i}，展示键 = f"{i}. {标签}"）。"""
    raw = tuple(
        (f"{BATCH_HASH_PREFIX}{i}", entry)
        for i, (_label, entry) in enumerate(labels_and_entries, start=1)
    )
    display = tuple(
        (f"{i}. {label}", entry)
        for i, (label, entry) in enumerate(labels_and_entries, start=1)
    )
    return ConfirmationCard(
        operation_type=OP_CREATE_METAL_ORDER,
        target="",
        fields_to_write=display,
        hash_fields=raw,
        hash_target="批量创建 2 条",
        auto_filled=(),
        expected_impact="impact",
        limitations=(),
        payload_hash="",
        row_header_label="明细",
        row_quantifier="条",
    )


def test_parameter_snapshot_batch_single_card_items_empty() -> None:
    """单条卡：items 为空元组，to_dict 不落 items 键（单条快照 JSON 零回归）。"""
    snapshot = build_parameter_snapshot(make_card(), task_id="t-single")
    assert snapshot.items == ()
    assert "items" not in snapshot.to_dict()


def test_parameter_snapshot_batch_items_archived() -> None:
    """批量卡：按条归档 items = (业务标签, 原始执行 entry)，含内部主键不本地化。"""
    entries = (
        ("客户 铭泰 / M1",
         {"action": "created", "fields": {"customer_id": 3, "model": "M1"}}),
        ("客户 示例 / M2",
         {"action": "created", "fields": {"customer_id": 7, "model": "M2"}}),
    )
    card = _batch_card(entries)
    snapshot = build_parameter_snapshot(card, task_id="t-batch")
    assert snapshot.items == entries
    data = snapshot.to_dict()
    assert data["items"][0] == {
        "label": "客户 铭泰 / M1",
        "entry": {"action": "created", "fields": {"customer_id": 3, "model": "M1"}},
    }
    assert data["items"][1]["label"] == "客户 示例 / M2"


def test_parameter_snapshot_batch_duplicate_labels_kept() -> None:
    """同业务标签两行（序号键唯一）：按条归档两条都在，不因 dict() 折叠丢行。"""
    entries = (
        ("客户 铭泰 / M1", {"action": "created", "fields": {"quantity": 1}}),
        ("客户 铭泰 / M1", {"action": "created", "fields": {"quantity": 2}}),
    )
    snapshot = build_parameter_snapshot(_batch_card(entries), task_id="t-dup")
    assert len(snapshot.items) == 2
    assert snapshot.items[0][1]["fields"] == {"quantity": 1}
    assert snapshot.items[1][1]["fields"] == {"quantity": 2}


# ===========================================================================
# D14（RV-20）：幂等键（mcp-api §6.1 / 5.3 步骤 3-4）
# ===========================================================================

FIXED_KEY = "agent-job-11112222333344445555666677778888"


def make_executor(*, client, tasks, audit, key_factory=generate_idempotency_key,
                  retry_limit=3, policy=policy_for("R2.order.create.metal"),
                  target_type="metal_order") -> MetalOrderExecutor:
    """自建执行器（注入固定 key_factory 以复现 ERP 已有同 key 记录场景；
    D15 塑粉复用：policy=policy_for("R2.order.create.powder") + target_type="powder_order"）。"""
    return MetalOrderExecutor(
        client=client,
        tasks=tasks,
        audit=audit,
        trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        policy=policy,
        retry_limit=retry_limit,
        key_factory=key_factory,
        target_type=target_type,
    )


def test_idempotency_key_format_and_unique() -> None:
    """生成 key：1-128、字母数字开头、合法字符集、唯一（5.3 步骤 3）。"""
    key = generate_idempotency_key()
    assert key.startswith("agent-job-")
    assert 1 <= len(key) <= 128
    assert is_valid_idempotency_key(key)
    assert key != generate_idempotency_key()  # 每次确认动作唯一


def test_idempotency_key_rejects_invalid_format() -> None:
    """非法 key 一律拒绝（§6.1：字符集 + 字母数字开头 + 长度）。"""
    assert is_valid_idempotency_key("Ab1._:-c") is True  # 合法字符集
    assert is_valid_idempotency_key("a" * 128) is True    # 上界
    assert is_valid_idempotency_key("") is False         # 空
    assert is_valid_idempotency_key("-abc") is False     # 非字母数字开头
    assert is_valid_idempotency_key("a b") is False      # 空格非法
    assert is_valid_idempotency_key("a#b") is False      # # 非法
    assert is_valid_idempotency_key("a" * 129) is False  # 超长


def test_resolve_idempotency_key_fresh_when_no_attempts() -> None:
    """无历史尝试 → 生成新 key（确认后首次执行，5.3 步骤 3）。"""
    payload = {"customer_id": CUSTOMER_ID, "model": "MK-01"}
    key = resolve_idempotency_key(payload, [], key_factory=lambda: "fresh-1")
    assert key == "fresh-1"


def test_resolve_idempotency_key_reuses_on_same_payload() -> None:
    """同一次业务操作重试：任一 prior attempt 载荷一致 → 复用持久化 key。"""
    payload = make_draft().to_write_dict()
    prior = TaskAttemptRecord(
        attempt_id="a1", task_id="t-1", operation_id="op-1", attempt_no=1,
        target_type="metal_order", idempotency_key=FIXED_KEY, field_diffs=payload,
    )
    assert (
        resolve_idempotency_key(payload, [prior], key_factory=lambda: "new-1")
        == FIXED_KEY
    )


def test_resolve_idempotency_key_new_key_on_payload_change() -> None:
    """载荷变化（如确认后单价修改）→ 必须换 key（§6.1/5.3 L751）。"""
    prior_payload = make_draft().to_write_dict()  # unit_price=120.0
    changed_payload = make_draft(unit_price=130.0).to_write_dict()
    prior = TaskAttemptRecord(
        attempt_id="a1", task_id="t-1", operation_id="op-1", attempt_no=1,
        target_type="metal_order", idempotency_key=FIXED_KEY, field_diffs=prior_payload,
    )
    assert (
        resolve_idempotency_key(changed_payload, [prior], key_factory=lambda: "new-2")
        == "new-2"
    )


def test_resolve_idempotency_key_ignores_key_order() -> None:
    """载荷键序不同不算变化（canonical_json sort_keys，§6.2 同口径）。"""
    a = {"customer_id": CUSTOMER_ID, "model": "MK-01", "quantity": 2}
    b = {"quantity": 2, "model": "MK-01", "customer_id": CUSTOMER_ID}
    prior = TaskAttemptRecord(
        attempt_id="a1", task_id="t-1", operation_id="op-1", attempt_no=1,
        target_type="metal_order", idempotency_key=FIXED_KEY, field_diffs=a,
    )
    assert resolve_idempotency_key(b, [prior], key_factory=lambda: "new-3") == FIXED_KEY


# ===========================================================================
# D14（RV-20）：执行器（5.3 步骤 3-8 / 9.3 / 14.3 场景 11 / 13.2）
# ===========================================================================

def test_execute_success_creates_order(ctx) -> None:
    """成功分支：创建订单、保存响应与 other_effects、不建 ar/ap/transactions。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert isinstance(summary, OrderExecutionSummary)
    assert summary.result_status == "success"
    assert summary.operation_type == OP_CREATE_METAL_ORDER
    assert summary.order_id is not None
    assert summary.verification_passed is True  # ERP 返回订单即最终校验（步骤 7）
    assert summary.attempts == 1
    assert summary.error_code is None
    assert is_valid_idempotency_key(summary.idempotency_key)

    assert len(erp.orders) == 1  # 恰创建 1 单（不重复创建）
    assert erp.orders[summary.order_id]["model"] == "MK-01"
    assert summary.order_response["customer_id"] == CUSTOMER_ID
    assert summary.other_effects  # 步骤 7：保存 other_effects
    # 步骤 8：仅 create_order_bundle 一个调用——不创建 ar/ap/transactions
    assert len(erp.calls) == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_execute_success_persists_key_in_attempt(ctx) -> None:
    """5.3 步骤 3：幂等键与 operation_id 随 attempt 持久化（task_attempts）。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    attempts = audit.attempts(task.operation_id)
    assert len(attempts) == 1
    assert attempts[0].idempotency_key == summary.idempotency_key
    assert attempts[0].operation_id == task.operation_id
    assert attempts[0].target_type == "metal_order"
    assert attempts[0].field_diffs == make_draft().to_write_dict()
    assert attempts[0].result_status == "success"


def test_execute_sends_operation_id_and_trace_id(ctx) -> None:
    """写工具调用携带 operation_id/trace_id（L1179 追查链贯通）。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    executor.execute(task_id=task.task_id, draft=make_draft())

    call = erp.calls[0]
    assert call["operation_id"] == task.operation_id
    assert call["trace_id"] == TRACE_ID
    assert call["idempotency_key"]  # 载荷恒不含 total_amount（D13 已保证）
    assert "total_amount" not in call["order"]


def test_execute_replayed_when_erp_has_same_key(ctx) -> None:
    """步骤 6：ERP 已有同 key 同载荷 → replayed=true 视为成功重放，不重复创建。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    payload = make_draft().to_write_dict()
    seed_id = erp.seed_order(FIXED_KEY, payload)  # 同 key 同载荷已存在
    executor = make_executor(
        client=client, tasks=tasks, audit=audit, key_factory=lambda: FIXED_KEY
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "replayed"
    assert summary.order_id == seed_id  # 返回原订单，不新建
    assert summary.verification_passed is True
    assert len(erp.orders) == 1  # 未重复创建
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 11.5 指标来源：result 事件 result_code="replayed"
    results = audit.events(task_id=task.task_id, event_type="result")
    assert [e.result_code for e in results] == ["replayed"]


def test_execute_conflict_reports_no_create(ctx) -> None:
    """IDEMPOTENCY_KEY_CONFLICT：report 不创建、不改原记录（13.1）。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    erp.fail_conflict(FIXED_KEY)
    executor = make_executor(
        client=client, tasks=tasks, audit=audit, key_factory=lambda: FIXED_KEY
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "conflict"
    assert summary.error_code == ERROR_KEY_CONFLICT
    assert "不创建订单" in summary.error_summary
    assert summary.order_id is None
    assert summary.verification_passed is False
    assert len(erp.orders) == 0  # 未创建
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    results = audit.events(task_id=task.task_id, event_type="result")
    assert [e.result_code for e in results] == ["conflict"]
    assert audit.attempts(task.operation_id)[0].result_status == "conflict"


def test_execute_in_progress_no_auto_retry_no_key_change(ctx) -> None:
    """IDEMPOTENCY_IN_PROGRESS：1 次尝试、不自动重试、不换 key 绕过、转人工。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    erp.fail_in_progress(FIXED_KEY)
    executor = make_executor(
        client=client, tasks=tasks, audit=audit, key_factory=lambda: FIXED_KEY
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_IN_PROGRESS
    assert summary.attempts == 1  # 不自动重试
    assert len(erp.calls) == 1    # 不换 key 重试绕过
    assert erp.calls[0]["idempotency_key"] == FIXED_KEY
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    result = audit.events(task_id=task.task_id, event_type="result")[0]
    assert result.result_code == "failed"
    assert result.structured_payload["requires_operator_action"] is True


def test_execute_network_unknown_retries_same_key_then_replayed(ctx) -> None:
    """14.3 场景 11：响应丢失（结果未知）→ 同 key 重试 → replayed=true，
    ERP 仅 1 单（同 key 查询确认，不重复创建）。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    erp.fail_network_unknown(FIXED_KEY)
    executor = make_executor(
        client=client, tasks=tasks, audit=audit, key_factory=lambda: FIXED_KEY
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "replayed"
    # 2026-08-29 口径：task_attempts 一行 = 业务层一次逻辑调用。瞬时重试
    # 已在 MCP 协议层内部完成（2026-08-27 批准），不计入业务 attempt，
    # 改用持久化审计事件 event_type=mcp_retry 留痕（详见下方断言）。
    assert summary.attempts == 1
    assert len(erp.calls) == 2
    # 两次调用同一幂等键（13.2：使用同一幂等键重试）
    assert erp.calls[0]["idempotency_key"] == erp.calls[1]["idempotency_key"] == FIXED_KEY
    assert len(erp.orders) == 1  # 第一次已写入，重试未重复创建
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    attempts = audit.attempts(task.operation_id)
    assert [a.result_status for a in attempts] == ["replayed"]
    # 传输层瞬时重试留痕（11.2 规则 3 可追溯性）：连接失败属"结果未知"，
    # 写操作是否执行一度不可知，必须落持久化审计事件——此前只存在于有界
    # 500 条且进程内存的结构化日志环形缓冲，负载高时会被淘汰。
    retries = audit.events(task_id=task.task_id, event_type=EVENT_MCP_RETRY)
    assert len(retries) == 1
    assert retries[0].operation_id == task.operation_id
    assert retries[0].structured_payload["error_code"] == "MCP_CONNECT_FAILED"
    assert retries[0].structured_payload["tool"] == "create_order_bundle"
    # 11.5 replayed 数量指标：result 事件 result_code="replayed" 恰 1 条
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len([e for e in results if e.result_code == "replayed"]) == 1


def test_execute_network_fail_reaches_retry_limit(ctx) -> None:
    """13.2：结果未知达上限 → failed 不循环；不重复创建（未写入）。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    erp.fail_network_persistent(FIXED_KEY)
    executor = make_executor(
        client=client, tasks=tasks, audit=audit,
        key_factory=lambda: FIXED_KEY, retry_limit=2,
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_NETWORK_UNKNOWN
    assert summary.attempts == 2  # 达 retry_limit，不再循环
    # 调用次数 = 业务 attempt 数 × 每 attempt 的调用数；后者 = 1 + 传输层瞬时
    # 重试上限（erp_mcp_max_retries，2026-08-27 批准默认 3）
    assert len(erp.calls) == 2 * (1 + mcp_max_retries())
    assert erp.calls[0]["idempotency_key"] == erp.calls[1]["idempotency_key"]
    assert len(erp.orders) == 0  # 网络失败未写入——不重复创建
    assert "不重复创建" in summary.error_summary
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_execute_other_error_no_retry(ctx) -> None:
    """其他错误（BUSINESS_ERROR）：retryable=false 不重试 → failed。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    erp.fail_reject(FIXED_KEY)
    executor = make_executor(
        client=client, tasks=tasks, audit=audit, key_factory=lambda: FIXED_KEY
    )

    summary = executor.execute(task_id=task.task_id, draft=make_draft())

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_MCP_FAILURE
    assert summary.attempts == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_execute_rejects_when_task_not_running(ctx) -> None:
    """非 running（等待确认）不执行：TaskNotRunningError + 零 ERP 调用。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    tasks.request_confirmation(task.task_id, payload_hash="0" * 64)
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_CONFIRMATION

    with pytest.raises(TaskNotRunningError):
        executor.execute(task_id=task.task_id, draft=make_draft())
    assert erp.calls == []
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_CONFIRMATION


def test_execute_fails_closed_on_forbidden_total_amount(ctx) -> None:
    """9.3 fail closed：载荷含 total_amount → 拒绝且不调用 ERP。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)

    class ForgedDraft:
        """模拟绕过 D13 提取层的非法 draft（duck type：仅 to_write_dict）。"""

        def to_write_dict(self) -> dict:
            return {"customer_id": CUSTOMER_ID, "model": "MK-01", "total_amount": 999}

    with pytest.raises(MCPError):
        executor.execute(task_id=task.task_id, draft=ForgedDraft())

    assert erp.calls == []  # 本地拒绝，未触网
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    # 本地校验失败未发起调用：无 attempt/result 事件
    assert audit.events(task_id=task.task_id, event_type="result") == []


# ---- [2026-09-08 ERP 附加款上线] surcharge_amount（create 载荷透传） ----

def test_create_order_with_surcharge_passthrough(ctx) -> None:
    """附加款随 create_order_bundle 载荷透传（mcp-api.md §6.1）：确认卡
    即载荷——ERP 收到后行金额 = 数量×单重×单价 + 附加款。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    draft = make_draft(surcharge_amount=12.5)

    summary = executor.execute(task_id=task.task_id, draft=draft)

    assert summary.result_status == "success"
    assert summary.verification_passed is True
    assert len(erp.orders) == 1
    call = erp.calls[0]
    assert call["order"]["surcharge_amount"] == 12.5
    assert float(erp.orders[summary.order_id]["surcharge_amount"]) == 12.5
    # 审计 field_diffs 与载荷一致（确认卡即载荷 → hash 一致才执行）
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.field_diffs == draft.to_write_dict()
    assert attempt.field_diffs["surcharge_amount"] == 12.5


def test_create_order_without_surcharge_omits_field(ctx) -> None:
    """未提供附加款 → 载荷不含该键（None 不入写值；ERP 默认 0.00，
    幂等载荷保持最小化、不破坏与历史确认卡的可重放性）。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)

    executor.execute(task_id=task.task_id, draft=make_draft())

    call = erp.calls[0]
    assert "surcharge_amount" not in call["order"]
    assert "total_amount" not in call["order"]  # 行总额仍由 ERP 计算


# ===========================================================================
# D15（RV-21/RV-22）：塑粉创建执行（复用 MetalOrderExecutor，5.2/9.3）
# ===========================================================================

from administrative_agent.agent.prompts.policies import policy_for  # noqa: E402
from administrative_agent.business.order_rules import (  # noqa: E402
    ERR_POWDER_PRICE_AMBIGUOUS,
    ERR_POWDER_PRICE_MISSING,
    PowderOrderError,
    PowderOrderInput,
    complete_powder_order,
)

POWDER_SUPPLIER_ID = 101
POWDER_SUPPLIER_NAME = "示例塑粉材料有限公司"
POWDER_COLOR = "R-01"


def make_powder_draft(history=None, **overrides) -> object:
    """构造塑粉草稿（补全后；价格默认用户提供，历史默认无）。"""
    fields = dict(
        supplier_name=POWDER_SUPPLIER_NAME,
        color_code=POWDER_COLOR,
        total_weight=50.0,
        price_per_kg=28.5,
    )
    fields.update(overrides)
    return complete_powder_order(
        PowderOrderInput(**fields),
        supplier_id=POWDER_SUPPLIER_ID,
        supplier_name=POWDER_SUPPLIER_NAME,
        history_rows=history or [],
        today=TODAY,
    )


def test_powder_card_target_empty_and_supplier_name_is_first_write_row() -> None:
    """[2026-09-10 确认卡改版] 塑粉创建卡：target 置空、供货商名称作
    fields_to_write 首行「供货商」（纯名称）、写值字段不含 supplier_id；
    hash_target 仍取旧串保持哈希等价；执行载荷仍保留 supplier_id。"""
    from administrative_agent.agent.tools.orders import build_powder_confirmation_card

    draft = make_powder_draft()
    card = build_powder_confirmation_card(draft)
    assert card.target == ""
    assert card.fields_to_write[0] == ("供货商", POWDER_SUPPLIER_NAME)
    assert card.hash_target == f"供货商 {POWDER_SUPPLIER_NAME}"
    assert "supplier_id" not in dict(card.fields_to_write)
    fields = dict(card.fields_to_write)
    assert L("supplier_id") not in fields
    assert fields[L("color_code")] == POWDER_COLOR
    assert draft.to_write_dict()["supplier_id"] == POWDER_SUPPLIER_ID


def test_create_cards_payload_hash_equals_legacy_target_hash() -> None:
    """[2026-09-10 确认卡改版] 哈希等价性回归：两张创建卡 target 置空后，
    仍以 hash_target（旧「客户/供货商 名称」串）参与哈希——payload_hash 与
    「改版前的 target 串 + 原始 hash_fields」直接算出的值**逐字节一致**，
    确认卡→执行链路对既有已确认任务的哈希口径零变化。"""
    from administrative_agent.agent.tools.orders import build_powder_confirmation_card

    metal = make_card()
    metal_draft = make_draft()
    assert metal.payload_hash == payload_hash_for(
        OP_CREATE_METAL_ORDER,
        f"客户 {CUSTOMER_NAME}",
        tuple(metal_draft.to_write_dict().items()),
    )
    powder = build_powder_confirmation_card(make_powder_draft())
    assert powder.payload_hash == payload_hash_for(
        "创建订单（塑粉）",
        f"供货商 {POWDER_SUPPLIER_NAME}",
        tuple(make_powder_draft().to_write_dict().items()),
    )


def test_powder_execute_creates_with_autofilled_price(ctx) -> None:
    """5.2 场景 5：唯一历史价自动补全 → 创建成功；审计 target_type=powder_order。"""
    erp, tasks, audit, executor, _client = ctx
    task = create_task(tasks)
    history = [
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 27.0},
    ]
    draft = make_powder_draft(history=history, price_per_kg=None)  # 价格走历史补全
    assert draft.price_per_kg == 27.0

    executor = make_executor(
        client=_client, tasks=tasks, audit=audit,
        policy=policy_for("R2.order.create.powder"), target_type="powder_order",
    )
    summary = executor.execute(task_id=task.task_id, draft=draft)

    assert summary.result_status == "success"
    assert summary.order_id is not None
    assert summary.verification_passed is True
    assert len(erp.orders) == 1
    call = erp.calls[0]
    assert call["order"]["supplier_id"] == POWDER_SUPPLIER_ID
    assert call["order"]["color_code"] == POWDER_COLOR
    assert call["order"]["total_weight"] == 50.0
    assert call["order"]["price_per_kg"] == 27.0
    assert "total_amount" not in call["order"]      # 9.3 禁止字段
    assert "weight_unit" not in call["order"]       # 固定 kg 不传单位（5.2）
    assert "status" not in call["order"]            # 塑粉字段表无 status
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    attempts = audit.attempts(task.operation_id)
    assert attempts[0].target_type == "powder_order"  # 复用执行器审计准确
    assert attempts[0].result_status == "success"


def test_powder_execute_no_history_asks_without_creating(ctx) -> None:
    """14.3 场景 4：无历史价 → 补全层询问（ERR_POWDER_PRICE_MISSING），
    不触网不创建（RV-22 通过标准：唯一价自动补全，其余场景询问）。"""
    erp, tasks, audit, _executor, _client = ctx
    task = create_task(tasks)
    with pytest.raises(PowderOrderError) as excinfo:
        make_powder_draft(price_per_kg=None)  # 无历史价
    assert excinfo.value.code == ERR_POWDER_PRICE_MISSING
    assert erp.calls == []  # 询问分支未发起任何 ERP 调用
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING  # 未创建未失败


def test_powder_execute_multiple_prices_asks_without_creating(ctx) -> None:
    """14.3 场景 6：多个历史价 → 停止询问（ERR_POWDER_PRICE_AMBIGUOUS），
    不创建（RV-21 价格只按供货商加色号）。"""
    erp, tasks, audit, _executor, _client = ctx
    task = create_task(tasks)
    history = [
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 25.0},
        {"supplier_id": POWDER_SUPPLIER_ID, "color_code": POWDER_COLOR,
         "price_per_kg": 30.0},
    ]
    with pytest.raises(PowderOrderError) as excinfo:
        make_powder_draft(history=history, price_per_kg=None)
    assert excinfo.value.code == ERR_POWDER_PRICE_AMBIGUOUS
    assert erp.calls == []
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING


# ===========================================================================
# D16（RV-23/RV-24）：R3 订单更新（5.4 八条 / 9.4 / 14.3 场景 7/8/10）
# ===========================================================================

from administrative_agent.agent.prompts.policies import (    policy_for,)
from administrative_agent.domains.orders import (  # noqa: E402
    ERROR_UPDATE_CONFIRMATION_INVALID,
    ERROR_UPDATE_NETWORK_UNKNOWN,
    ERROR_UPDATE_NO_CHANGES,
    ERROR_UPDATE_NOT_FOUND,
    ERROR_UPDATE_PRECONDITION,
    ERROR_UPDATE_VERSION_CONFLICT,
    OP_UPDATE_METAL_ORDER,
    OrderUpdateExecutor,
    UpdateExecutionSummary,
    build_update_changes,
    build_update_confirmation_card,
    precheck_update_inputs,
)

SEED_UPDATED_AT = "2026-08-23T10:00:00.000000"  # seed_update_order 默认版本


def make_update_executor(client, tasks, audit, *, policy=policy_for("R3.order.update.metal")):
    """自建 R3 更新执行器（D16）。"""
    return OrderUpdateExecutor(
        client=client,
        tasks=tasks,
        audit=audit,
        trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        policy=policy,
    )


def confirm_update(tasks, task_id, card) -> None:
    """轻确认流程（7.3/D6：request_confirmation → confirm → running）。"""
    tasks.request_confirmation(task_id, card.payload_hash)
    tasks.confirm(task_id, approved=True)


def make_metal_order() -> dict:
    """钣金订单快照（写前查询返回形态；数值为 ERP 字符串化）。"""
    return dict(
        customer_id=CUSTOMER_ID,
        model="MK-01",
        unit_price="120.0000",
        price_type="含税",
        status="未完成",
        weight_per_unit="0.0000",
        weight_unit="kg",
        quantity="2.00",
        quantity_unit="套",
        in_date="2026-08-20",
        out_date=None,
        notes=None,
    )


def make_powder_order() -> dict:
    """塑粉订单快照（写前查询返回形态）。"""
    return dict(
        supplier_id=POWDER_SUPPLIER_ID,
        color_code=POWDER_COLOR,
        total_weight="50.00",
        price_per_kg="28.5000",
        in_date="2026-08-20",
        notes=None,
    )


def seed_metal(erp, order_id=10, **overrides) -> int:
    payload = make_metal_order()
    payload.update(overrides)
    return erp.seed_update_order(order_id, payload)


def seed_powder(erp, order_id=20, **overrides) -> int:
    payload = make_powder_order()
    payload.update(overrides)
    return erp.seed_update_order(order_id, payload)


def metal_card(erp, order_id, changes, expected=None) -> object:
    return build_update_confirmation_card(
        order_type="metal", order_id=order_id,
        before=erp.orders[order_id], changes=changes,
        expected_updated_at=expected or erp.orders[order_id]["updated_at"],
    )


# ---- changes 生成（5.4 规则 2 / 9.4） ----

def test_build_update_changes_only_changed_fields() -> None:
    """规则 2：只含真正变化字段；数值字符串与数值按数值比较。"""
    before = {"weight_per_unit": "0.0000", "unit_price": "120.0000", "notes": None}
    desired = {
        "weight_per_unit": 2.75,      # "0.0000" → 变化
        "unit_price": "120.0000",     # 同值 → 不进 changes
        "notes": None,                # 同为空 → 不进 changes
    }
    changes = build_update_changes(before, desired, "metal")
    assert changes == {"weight_per_unit": 2.75}


def test_build_update_changes_null_means_clear() -> None:
    """传 null 表示清空：与当前非空值不同 → 进入 changes。"""
    before = {"out_date": "2026-08-20", "notes": "ok"}
    changes = build_update_changes(before, {"out_date": None, "notes": "ok"}, "metal")
    assert changes == {"out_date": None}


def test_precheck_status_and_out_date_rules() -> None:
    """预检：status 枚举；已完成订单 out_date=null 单独更新拒绝。"""
    precheck_update_inputs({"status": "完成"}, {}, "metal")  # 合法不抛
    with pytest.raises(MCPError):
        precheck_update_inputs({"status": "已发货"}, {}, "metal")
    with pytest.raises(MCPError):
        precheck_update_inputs({"out_date": None}, {"status": "完成"}, "metal")
    # 同时改状态 → 允许（§7.2：联动由 ERP 执行）
    precheck_update_inputs(
        {"status": "未完成", "out_date": None}, {"status": "完成"}, "metal"
    )
    with pytest.raises(MCPError):
        precheck_update_inputs({"total_weight": 0}, {}, "powder")
    precheck_update_inputs({"total_weight": 10}, {}, "powder")


def test_precheck_surcharge_amount_rules() -> None:
    """[2026-09-08 ERP 附加款] 更新预检：金属 surcharge_amount 负数/超
    2 位小数/超列宽 → 本地拒绝（VALIDATION_ERROR）；合法值（含 0 = 清除）
    放行。塑粉侧由白名单层负责（该类型无此字段）。"""
    precheck_update_inputs({"surcharge_amount": 12.5}, {}, "metal")
    precheck_update_inputs({"surcharge_amount": 0}, {}, "metal")  # 0 = 清除
    precheck_update_inputs({"surcharge_amount": "88.80"}, {}, "metal")  # 字符串兼容
    for bad in (-1, 0.001, 12.345, 100000000.0, "abc"):
        with pytest.raises(MCPError):
            precheck_update_inputs({"surcharge_amount": bad}, {}, "metal")


def test_update_card_six_elements_and_hash_fragile() -> None:
    """更新卡片六要素 + payload_hash；expected_updated_at 变化 → 确认失效。"""
    before = {"model": "MK-01"}
    card = build_update_confirmation_card(
        order_type="metal", order_id=10, before=before,
        changes={"weight_per_unit": "2.7500"},
        expected_updated_at=SEED_UPDATED_AT,
    )
    assert card.operation_type == OP_UPDATE_METAL_ORDER
    # [2026-09-10 展示口径/P1-2] 目标只给复合业务标识，不写订单 id
    assert card.target == "订单（MK-01）"
    # 展示层为中文标签，执行载荷仍为 ERP 英文键（[2026-08-28 字段中文化]）
    assert card.fields_to_write[-1] == (L("expected_updated_at"), SEED_UPDATED_AT)
    assert card.auto_filled == ()
    assert card.expected_impact and card.limitations and card.payload_hash
    # 版本时间戳变化 → 哈希变化 → 确认失效（7.3）
    other = build_update_confirmation_card(
        order_type="metal", order_id=10, before=before,
        changes={"weight_per_unit": "2.7500"},
        expected_updated_at="2026-08-23T10:00:01.000000",
    )
    assert other.payload_hash != card.payload_hash


def test_update_card_target_is_composite_business_key_distinguishes_orders() -> None:
    """[2026-09-10 P1-2] 改单卡 target 用可判别的复合业务键（客户名称 /
    型号 / 入库日期），不写裸 order_id；同型号不同订单 target 可区分。

    弱断言修正：order_id 取 777（与型号 MK-10、日期均不构成子串关系），
    直接断言 order_id 本身不出现，避免原 `"10" not in target` 被型号
    含 MK-10 误红。
    """
    before_a = {"customer_name": "昌盛", "model": "MK-10", "in_date": "2026-09-05"}
    before_b = {"customer_name": "昌盛", "model": "MK-10", "in_date": "2026-09-06"}
    card_a = build_update_confirmation_card(
        order_type="metal", order_id=777, before=before_a,
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    card_b = build_update_confirmation_card(
        order_type="metal", order_id=778, before=before_b,
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    assert card_a.target == "订单（昌盛 / MK-10 / 2026-09-05）"
    assert "777" not in card_a.target  # order_id 本身不出现（非子串误判）
    assert card_b.target == "订单（昌盛 / MK-10 / 2026-09-06）"
    # 同客户同型号不同单 → target / payload_hash 可区分（旧行为二者相同）
    assert card_a.target != card_b.target
    assert card_a.payload_hash != card_b.payload_hash


def test_update_card_target_degrades_without_name_and_never_shows_id() -> None:
    """[2026-09-10 P1-2] 写前快照缺名称（生产 ERP 订单无名称列）→ 优雅降级
    为「型号 / 入库日期」，不抛异常、不回退显示 customer_id。"""
    # 快照只有 customer_id（无名称）→ 不显示 id
    before = {"customer_id": 16, "model": "MK-01", "in_date": "2026-09-05"}
    card = build_update_confirmation_card(
        order_type="metal", order_id=999, before=before,
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    assert card.target == "订单（MK-01 / 2026-09-05）"
    assert "16" not in card.target and "999" not in card.target
    # 全部标识缺失 → 退为「订单」，绝不抛异常
    empty = build_update_confirmation_card(
        order_type="metal", order_id=999, before={},
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    assert empty.target == "订单"


def test_update_card_hash_binds_order_id_for_identical_composite_key() -> None:
    """[2026-09-10 修复] 恢复身份绑定回归（reviewer P1-2 碰撞）：同型号、同
    in_date、不同 order_id 的两张改单卡 → payload_hash **不同**（旧行为二者
    相同，实测 0bf1d6d9a518e9c8）；同时展示不回退（target/字段不含裸 id）。
    """
    before = {"model": "MK-01", "in_date": "2026-09-05"}
    card_a = build_update_confirmation_card(
        order_type="metal", order_id=101, before=dict(before),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    card_b = build_update_confirmation_card(
        order_type="metal", order_id=102, before=dict(before),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    # 复合业务键（展示目标）相同 → 只能靠 id 区分身份
    assert card_a.target == card_b.target == "订单（MK-01 / 2026-09-05）"
    assert card_a.payload_hash != card_b.payload_hash
    assert ("id", 101) in card_a.hash_fields and ("id", 102) in card_b.hash_fields
    # 展示不回退：order_id 不出现在 target / fields_to_write
    assert "101" not in card_a.target and "102" not in card_b.target
    assert "id" not in dict(card_a.fields_to_write)


def test_update_card_hash_target_is_stable_without_snapshot_name() -> None:
    """[2026-09-10 修复] 写前快照携带关联方名称时，名称只进展示 target，不定
    义哈希——执行期重查名称改名/缺失不再使确认误失效（hash_target 用不含名称
    的稳定目标串，身份由 order_id 承担）。"""
    base = {"model": "MK-01", "in_date": "2026-09-05"}
    card_prepare = build_update_confirmation_card(
        order_type="metal", order_id=101, before={**base, "customer_name": "昌盛"},
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    card_exec = build_update_confirmation_card(
        order_type="metal", order_id=101, before=dict(base),  # 执行期名称缺失
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
    )
    # 展示不同（带名称 vs 无名称），但哈希只由稳定目标 + 原始载荷决定 → 一致
    assert card_prepare.target == "订单（昌盛 / MK-01 / 2026-09-05）"
    assert card_exec.target == "订单（MK-01 / 2026-09-05）"
    assert card_prepare.payload_hash == card_exec.payload_hash
    assert is_confirmation_valid(card_exec, card_prepare.payload_hash) is True


# ---- [2026-09-10 P2-3] 改单卡展示目标按 id 反查关联方名称（仅展示层） ----

class _PartyLookupFake:
    """按 id 反查关联方名称的最小 fake（entity→{id: name}）。

    - mapping 命中 → 返回名称行；未命中 → 名称为 None；
    - error=True → call_tool 抛异常（验证降级不抛、不影响 hash）。
    """

    def __init__(self, mapping: dict | None = None, *, error: bool = False) -> None:
        self.mapping = mapping or {}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if self.error:
            raise RuntimeError("lookup boom")
        entity = args["entity"]
        rid = args["filter"]["id"]
        name_field = "company_name" if entity == "customers" else "name"
        row = {"id": rid, name_field: self.mapping.get((entity, rid))}

        class _Result:
            ok = True
            data = {"items": [row]}

        return _Result()


def test_update_card_target_resolves_party_name_from_id() -> None:
    """[2026-09-10 P2-3] before 带 customer_id（生产订单表无名称列）→ 按 id
    反查名称补进展示 target，形如「订单（昌盛 / MK-101 / 2026-09-05）」；
    裸 customer_id 绝不出现。powder 侧走 suppliers.name。"""
    before = {"customer_id": 16, "model": "MK-101", "in_date": "2026-09-05"}
    fake = _PartyLookupFake({("customers", 16): "昌盛"})
    card = build_update_confirmation_card(
        order_type="metal", order_id=999, before=before,
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=fake,
    )
    assert card.target == "订单（昌盛 / MK-101 / 2026-09-05）"
    assert "16" not in card.target and "999" not in card.target
    # 反查走 customers 表、只取 id+company_name、按 id 过滤、limit 1
    assert fake.calls == [(
        "query",
        {"entity": "customers", "fields": ["id", "company_name"],
         "filter": {"id": 16}, "limit": 1},
    )]

    powder = build_update_confirmation_card(
        order_type="powder", order_id=20,
        before={"supplier_id": 7, "color_code": "RAL-9016", "in_date": "2026-09-05"},
        changes={"total_weight": "50.00"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({("suppliers", 7): "宏源"}),
    )
    assert powder.target == "订单（宏源 / RAL-9016 / 2026-09-05）"


def test_update_card_target_degrades_without_party_name_or_on_error() -> None:
    """[2026-09-10 P2-3] 反查无行 / 无名称 / 无 id / 查询异常 → 展示降级为
    「型号 / 入库日期」，绝不显示 id、绝不抛异常。"""
    base = {"model": "MK-101", "in_date": "2026-09-05"}
    # 有 id 但查不到名称（名称为 None）
    no_name = build_update_confirmation_card(
        order_type="metal", order_id=999, before={**base, "customer_id": 16},
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({}),
    )
    assert no_name.target == "订单（MK-101 / 2026-09-05）"
    assert "16" not in no_name.target
    # 查询异常 → 同样降级（执行期查名失败不阻断）
    on_error = build_update_confirmation_card(
        order_type="metal", order_id=999, before={**base, "customer_id": 16},
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({("customers", 16): "昌盛"}, error=True),
    )
    assert on_error.target == "订单（MK-101 / 2026-09-05）"
    # 无 id → 不查询
    no_id_fake = _PartyLookupFake({})
    no_id = build_update_confirmation_card(
        order_type="metal", order_id=999, before=dict(base),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=no_id_fake,
    )
    assert no_id.target == "订单（MK-101 / 2026-09-05）"
    assert no_id_fake.calls == []


def test_update_card_payload_hash_ignores_resolved_party_name() -> None:
    """[2026-09-10 P2-3] 名称只进展示、不进 hash_target：确认期查到名称 /
    执行期查不到（或反查异常）→ 展示不同但 payload_hash 相同，
    is_confirmation_valid 仍 True（执行期不误拒）。"""
    before = {"customer_id": 16, "model": "MK-101", "in_date": "2026-09-05"}
    card_prepare = build_update_confirmation_card(
        order_type="metal", order_id=101, before=dict(before),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({("customers", 16): "昌盛"}),
    )
    card_exec_missing = build_update_confirmation_card(
        order_type="metal", order_id=101, before=dict(before),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({}),
    )
    card_exec_error = build_update_confirmation_card(
        order_type="metal", order_id=101, before=dict(before),
        changes={"weight_per_unit": "2.7500"}, expected_updated_at=SEED_UPDATED_AT,
        client=_PartyLookupFake({}, error=True),
    )
    assert card_prepare.target == "订单（昌盛 / MK-101 / 2026-09-05）"
    assert card_exec_missing.target == "订单（MK-101 / 2026-09-05）"
    assert card_prepare.payload_hash == card_exec_missing.payload_hash
    assert card_prepare.payload_hash == card_exec_error.payload_hash
    assert is_confirmation_valid(card_exec_missing, card_prepare.payload_hash) is True
    assert is_confirmation_valid(card_exec_error, card_prepare.payload_hash) is True


# ---- 执行（9.4 / 14.3 场景 7/8） ----

def test_update_metal_weight_recalc_amount_and_snapshots(ctx) -> None:
    """14.3 场景 7：补充钣金重量 → 更新成功、金额重算、保存前后快照。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)  # weight 0.0000, qty 2, price 120 → total 0.00
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10,
        desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.verification_passed is True
    assert summary.error_code is None
    # 前后快照（场景 7：保存更新前快照与更新后订单）
    assert summary.before_snapshot["weight_per_unit"] == "0.0000"
    assert summary.after_snapshot["weight_per_unit"] == "2.7500"
    # ERP 同事务重算金额（规则 5）：2 × 2.75 × 120 = 660.00
    assert summary.after_snapshot["total_amount"] == "660.00"
    assert summary.before_snapshot["total_amount"] == "0.00"
    assert "total_amount" in summary.changed_fields
    assert summary.other_effects[0]["type"] == "receivable_current_impact"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 调用参数：expected_updated_at 原样回传（规则 4）+ changes 只含变化字段
    upd_call = [c for c in erp.calls if "expected_updated_at" in c][-1]
    assert upd_call["expected_updated_at"] == SEED_UPDATED_AT
    assert upd_call["changes"] == changes
    assert upd_call["order_id"] == 10
    # 审计：attempt 记录目标/字段差异/expected_updated_at（11.x L1225）
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.target_type == "metal_order"
    assert attempt.target_id == "10"
    assert attempt.expected_updated_at == SEED_UPDATED_AT
    assert attempt.field_diffs == changes
    assert attempt.result_status == "success"


def test_update_metal_weight_and_surcharge_recalc(ctx) -> None:
    """[2026-09-08 ERP 附加款] 场景：改单重 + 改附加款 → ERP 同事务重算
    行金额（基础金额 2×2.75×120 = 660.00 ＋ 附加款 88.80 → 748.80）；
    写前查询与前后快照均含 surcharge_amount（_UPDATE_QUERY_FIELDS 自动
    随白名单包含附加款）。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp, surcharge_amount="12.50")  # 原单已有附加款（基础金额 0）
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500", "surcharge_amount": "88.80"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10,
        desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.verification_passed is True
    # 写前快照含附加款（precheck/卡片展示真实值，不被查询字段遗漏）
    assert summary.before_snapshot["surcharge_amount"] == "12.50"
    assert summary.after_snapshot["surcharge_amount"] == "88.80"
    assert summary.after_snapshot["total_amount"] == "748.80"  # 660 + 88.80
    assert "total_amount" in summary.changed_fields
    assert "surcharge_amount" in summary.changed_fields
    # 调用参数：changes 原样透传（含附加款）
    upd_call = [c for c in erp.calls if "expected_updated_at" in c][-1]
    assert upd_call["changes"] == changes
    assert upd_call["order_id"] == 10
    # 审计：field_diffs 含附加款
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.field_diffs == changes
    assert attempt.result_status == "success"


def test_update_no_changes_rejected_without_calling(ctx) -> None:
    """规则 2：期望值与当前一致 → 不调用 update_order（changes 必非空）。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    desired = {"unit_price": "120.0000", "notes": None}  # 与当前一致
    confirm_update(tasks, task.task_id, metal_card(erp, 10, {}))
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=desired,
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_NO_CHANGES
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_powder_price_payable_impact(ctx) -> None:
    """14.3 场景 8：修正塑粉价格 → payable_current_impact + 金额重算。"""
    erp, tasks, audit, _executor, client = ctx
    seed_powder(erp)  # 50 kg × 28.5 = 1425.00
    task = create_task(tasks)
    changes = {"price_per_kg": "30.0000"}
    card = build_update_confirmation_card(
        order_type="powder", order_id=20, before=erp.orders[20],
        changes=changes, expected_updated_at=erp.orders[20]["updated_at"],
    )
    confirm_update(tasks, task.task_id, card)
    executor = make_update_executor(client, tasks, audit, policy=policy_for("R3.order.update.powder"))

    summary = executor.execute(
        task_id=task.task_id, order_type="powder", order_id=20, desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.after_snapshot["total_amount"] == "1500.00"  # 50 × 30
    assert summary.before_snapshot["total_amount"] == "1425.00"
    assert summary.other_effects[0]["type"] == "payable_current_impact"
    assert summary.other_effects[0]["amount"] == "1500.00"
    attempt = audit.attempts(task.operation_id)[0]
    assert attempt.target_type == "powder_order"
    assert attempt.field_diffs == changes


def test_update_status_complete_out_date_auto(ctx) -> None:
    """§7.2 联动：status→完成 未提供 out_date → ERP 写当天（只做预检+展示）。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"status": "完成"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.after_snapshot["status"] == "完成"
    assert summary.after_snapshot["out_date"] == "2026-08-23"  # 假服务写当天
    assert "out_date" in summary.changed_fields


# ---- 版本冲突（规则 6 / 14.3 场景 10） ----

def test_update_version_conflict_refetch_regenerate_not_overwrite(ctx) -> None:
    """规则 6：冲突 → 重新查询重新生成差异；内容变化 → 需重新确认，
    不覆盖新数据（RV-24 通过标准）；不盲目重试。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    # 调用瞬间他人抢先更新为 9.0000（竞态窗口）
    erp.fail_version_conflict_at_call(10, {"weight_per_unit": "9.0000"})
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "conflict"
    assert summary.error_code == ERROR_UPDATE_VERSION_CONFLICT
    # 重新查询 + 重新生成差异（他人 9.0000 vs 期望 2.7500）
    assert summary.refetched_changes == {"weight_per_unit": "2.7500"}
    # 不覆盖新数据：订单保持他人更新值
    assert erp.orders[10]["weight_per_unit"] == "9.0000"
    assert erp.orders[10]["total_amount"] == "2160.00"  # 2 × 9 × 120
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    # 不盲目重复调用 update_order（仅 1 次调用 + 冲突后重新查询）
    upd_calls = [c for c in erp.calls if "expected_updated_at" in c]
    assert len(upd_calls) == 1
    # 11.2 规则 2：每次实际调用只追加一条 result——冲突分支恰一条
    # （result_status=conflict；O-1 修复：无外层 failed 预记录双重记录）
    result_events = audit.events(task_id=task.task_id, event_type="result")
    assert len(result_events) == 1
    assert audit.attempts(task.operation_id)[0].result_status == "conflict"


def test_update_version_conflict_already_applied_shows_latest(ctx) -> None:
    """规则 6：冲突后重新查询发现他人已应用期望值 → 展示最新快照成功。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    erp.fail_version_conflict_at_call(10, {"weight_per_unit": "2.7500"})
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.verification_passed is True
    assert summary.after_snapshot["weight_per_unit"] == "2.7500"
    assert summary.refetched_changes == {}
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 1


# ---- 网络结果未知（规则 7：先查询，不盲目重试） ----

def test_update_network_unknown_already_applied_shows_latest(ctx) -> None:
    """规则 7：结果未知 → 先查询；已应用期望变更 → 展示最新快照。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    erp.fail_network_unknown_update(10)  # 已应用但响应丢失
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "success"
    assert summary.verification_passed is True
    assert summary.after_snapshot["weight_per_unit"] == "2.7500"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 不盲目重试 update_order：业务层只有 1 次逻辑更新（重新查询确认，而非
    # 换参数重提）。传输层的瞬时重试会重复同一请求，故不按调用计数断言，
    # 而是断言所有调用复用同一 expected_updated_at —— 即同一次逻辑更新。
    update_calls = [c for c in erp.calls if "expected_updated_at" in c]
    assert update_calls
    assert len(update_calls) <= 1 + mcp_max_retries()
    assert {c["expected_updated_at"] for c in update_calls} == {SEED_UPDATED_AT}


def test_update_network_unknown_not_applied_requires_reconfirm(ctx) -> None:
    """规则 7：结果未知 → 先查询；未应用 → 需重新确认，不盲目重试。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    erp.fail_network_persistent_update(10)  # 未应用
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_NETWORK_UNKNOWN
    assert summary.refetched_changes == {"weight_per_unit": "2.7500"}
    assert erp.orders[10]["weight_per_unit"] == "0.0000"  # 订单未变化
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    # 同上：业务层 1 次逻辑更新（同 expected_updated_at），传输层重试至上限
    update_calls = [c for c in erp.calls if "expected_updated_at" in c]
    assert update_calls
    assert len(update_calls) <= 1 + mcp_max_retries()
    assert {c["expected_updated_at"] for c in update_calls} == {SEED_UPDATED_AT}


# ---- 输入预检与 fail closed ----

def test_update_completed_out_date_null_precheck(ctx) -> None:
    """L775 预检：已完成订单只改 out_date 传 null → 本地拒绝，不触网。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp, status="完成", out_date="2026-08-23")
    task = create_task(tasks)
    changes = {"out_date": None}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_PRECONDITION
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_forbidden_fields_fail_closed(ctx) -> None:
    """§7.1 fail closed：系统/派生金额/白名单外字段 → 本地拒绝，不触网。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    executor = make_update_executor(client, tasks, audit)

    for bad in ({"total_amount": "999.00"}, {"id": 1}, {"magic_field": 1}):
        task = create_task(tasks)
        confirm_update(tasks, task.task_id, metal_card(erp, 10, {"notes": "x"}))
        summary = executor.execute(
            task_id=task.task_id, order_type="metal", order_id=10, desired=bad,
        )
        assert summary.result_status == "failed"
        assert summary.error_code == ERROR_UPDATE_PRECONDITION
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_unknown_order_not_found(ctx) -> None:
    """规则 1：写前查询无唯一目标 → 不更新不猜测。"""
    erp, tasks, audit, _executor, client = ctx
    task = create_task(tasks)
    card = build_update_confirmation_card(
        order_type="metal", order_id=999, before={"model": "?"},
        changes={"notes": "x"}, expected_updated_at=SEED_UPDATED_AT,
    )
    confirm_update(tasks, task.task_id, card)
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=999, desired={"notes": "x"},
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_NOT_FOUND
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_without_confirmation_rejected(ctx) -> None:
    """7.3：无已确认载荷哈希 → 拒绝执行（确认失效必须重新确认）。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)  # 未走 request_confirmation/confirm
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10,
        desired={"notes": "x"},
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_CONFIRMATION_INVALID
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_confirmation_invalid_when_snapshot_changed(ctx) -> None:
    """7.3：确认后执行前快照被他人更新 → 确认失效，拒绝执行不覆盖。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    changes = {"weight_per_unit": "2.7500"}
    confirm_update(tasks, task.task_id, metal_card(erp, 10, changes))
    # 确认后、执行前他人更新（快照变化 → 重建卡片哈希不一致）
    erp.apply_other_writer_update(10, {"notes": "other edit"})
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute(
        task_id=task.task_id, order_type="metal", order_id=10, desired=changes,
    )

    assert summary.result_status == "failed"
    assert summary.error_code == ERROR_UPDATE_CONFIRMATION_INVALID
    assert erp.orders[10]["weight_per_unit"] == "0.0000"  # 不覆盖新数据
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 0


def test_update_not_running_rejected(ctx) -> None:
    """8.3：非 running 任务不执行。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp)
    task = create_task(tasks)
    card = metal_card(erp, 10, {"notes": "x"})
    tasks.request_confirmation(task.task_id, card.payload_hash)
    tasks.confirm(task.task_id, approved=False)  # 取消 → CANCELLED
    executor = make_update_executor(client, tasks, audit)

    with pytest.raises(TaskNotRunningError):
        executor.execute(
            task_id=task.task_id, order_type="metal", order_id=10,
            desired={"notes": "x"},
        )


def test_update_summary_to_dict() -> None:
    """UpdateExecutionSummary 可结构化导出（审计/展示）。"""
    summary = UpdateExecutionSummary(
        task_id="t1", operation_type=OP_UPDATE_METAL_ORDER, order_type="metal",
        order_id=10, expected_updated_at=SEED_UPDATED_AT,
        changes={"notes": "x"}, before_snapshot={"notes": None},
        after_snapshot={"notes": "x"}, changed_fields=("notes",),
        other_effects=(), result_status="success", attempts=1,
        verification_passed=True,
    )
    d = summary.to_dict()
    assert d["order_type"] == "metal"
    assert d["before_snapshot"] == {"notes": None}
    assert d["changed_fields"] == ["notes"]


# ===========================================================================
# [2026-09-08 ERP 附加款] prepare_order_update 提前预检（tools 层真实入口；
# 工具 prepare 阶段零 tasks/audit 依赖——只读写前查询 + 卡片构造，故可用
# 最小 fake client 直测；确认卡/执行载荷语义与 domains 执行器测试同源）
# ===========================================================================

from administrative_agent.agent.tools.base import (  # noqa: E402
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
)
from administrative_agent.agent.tools.orders import (  # noqa: E402
    OrderUpdateArgs,
    prepare_order_update,
)


class _PrepareFakeErp:
    """prepare 写前查询最小 fake：call_tool("query") → 固定行；记录调用。"""

    def __init__(self, row: dict | None) -> None:
        self._row = row
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        assert name == "query"
        items = [dict(self._row)] if self._row is not None else []

        class _Result:
            ok = True
            data = {"items": items}

        return _Result()


def _prepare_ctx(erp: _PrepareFakeErp) -> ToolContext:
    """prepare 只经 _require_wiring 校验 client/tasks/audit 非 None。"""
    return ToolContext(client=erp, tasks=object(), audit=object())


def _metal_before(**overrides: object) -> dict:
    """写前查询返回形态（同 make_metal_order + 版本戳）。"""
    row = make_metal_order()
    row.update({"updated_at": SEED_UPDATED_AT, "surcharge_amount": "0.00"})
    row.update(overrides)
    return row


def test_prepare_update_rejects_invalid_surcharge_before_card() -> None:
    """负数/超 2 位小数/超列宽附加款 → prepare 即抛 ToolPreparationError
    （消息含「（确认前拦截）」），不构造确认卡、fake ERP 零写调用。"""
    for bad in (-1, 0.001, 100000000.0):
        erp = _PrepareFakeErp(_metal_before())
        ctx = _prepare_ctx(erp)
        args = OrderUpdateArgs(order_type="metal", order_id=10, surcharge_amount=bad)

        with pytest.raises(ToolPreparationError, match="确认前拦截"):
            prepare_order_update(ctx, args)

        assert [c for c in erp.calls if c[0] != "query"] == []  # 零写调用


def test_prepare_update_accepts_valid_surcharge_builds_card() -> None:
    """合法附加款（12.5）→ 正常返回 PreparedToolWrite：确认卡与载荷
    （desired）含附加款，payload_hash 可展示。"""
    erp = _PrepareFakeErp(_metal_before())
    result = prepare_order_update(
        _prepare_ctx(erp), OrderUpdateArgs(order_type="metal", order_id=10,
                                           surcharge_amount=12.5)
    )

    assert isinstance(result, PreparedToolWrite)
    assert result.payload_hash
    assert result.execution["desired"]["surcharge_amount"] == 12.5
    assert result.policy_key == "R3.order.update.metal"
    assert [c for c in erp.calls if c[0] != "query"] == []  # prepare 只读


def test_prepare_update_intercepts_completed_out_date_clear() -> None:
    """已完成订单的「清 out_date」意图 → prepare 不构造确认卡即拒绝。

    说明：工具入口下清空语义不可表达——desired 生成按
    `v is not None` 过滤（tools/orders.py L354-356），out_date=None 不会
    进入 changes，prepare 以「未提供任何期望变化字段」fail closed（零写
    调用）；「已完成订单 out_date=null」预检组合在执行侧由
    precheck_update_inputs 拦截（domains/orders.py L857-868），域级用例
    见 test_precheck_status_and_out_date_rules / 执行器级
    test_update_completed_out_date_null_precheck。
    """
    erp = _PrepareFakeErp(_metal_before(status="完成", out_date="2026-08-23"))
    args = OrderUpdateArgs(order_type="metal", order_id=10, out_date=None)

    with pytest.raises(ToolPreparationError):
        prepare_order_update(_prepare_ctx(erp), args)

    assert [c for c in erp.calls if c[0] != "query"] == []


# ===========================================================================
# [2026-09-12 一张卡多条明细] 批量创建 / 批量更新域级用例
# ===========================================================================

from administrative_agent.audit.service import (  # noqa: E402
    EVENT_ATTEMPT,
    EVENT_RESULT,
)
from administrative_agent.agent.tools.orders import (  # noqa: E402
    OrderCreateArgs,
    OrderCreateItemArgs,
    OrderUpdateItemArgs,
    prepare_order_create,
)
from administrative_agent.domains.orders import (  # noqa: E402
    ACTION_CREATE,
    ACTION_UNCHANGED,
    OrderBatchItem,
    OrderUpdateBatchItem,
    _UPDATE_QUERY_FIELDS,
    build_update_batch_confirmation_card,
    plan_update_item,
)
from administrative_agent.mcp.scopes import (  # noqa: E402
    UPDATE_WHITELIST_METAL,
    UPDATE_WHITELIST_POWDER,
)

_CUSTOMER_ROW = {
    "id": CUSTOMER_ID,
    "company_name": CUSTOMER_NAME,
    "unit_price": "120.0000",
    "price_type": "含税",
    "price_unit": "元/kg",
}


# ---- 单条委托：字节级零回归（口径 4） ----

def test_create_single_item_delegates_byte_identical() -> None:
    """顶层单条简写与 items 单元素 → 走同一单条函数，卡片/执行载荷逐字节一致。"""
    flat = prepare_order_create(
        _prepare_ctx(_PrepareFakeErp(dict(_CUSTOMER_ROW))),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="MK-01", unit_price=120.0, quantity=2),
    )
    listed = prepare_order_create(
        _prepare_ctx(_PrepareFakeErp(dict(_CUSTOMER_ROW))),
        OrderCreateArgs(order_type="metal", items=[OrderCreateItemArgs(
            customer_name=CUSTOMER_NAME, model="MK-01",
            unit_price=120.0, quantity=2)]),
    )
    assert flat.card == listed.card
    assert flat.payload_hash == listed.payload_hash
    assert flat.execution == listed.execution
    assert "items" not in flat.execution  # 单条 = 旧执行形状（在飞卡兼容）


# ---- 批量创建卡：同客户同型号两行不折叠（必测项 8） ----

def test_create_batch_card_two_same_customer_model_rows() -> None:
    """两行同客户同型号（数量不同）→ 展示外层键唯一且两行都在；执行形状含 items。"""
    args = OrderCreateArgs(order_type="metal", items=[
        OrderCreateItemArgs(customer_name=CUSTOMER_NAME, model="MK-01",
                            unit_price=120.0, quantity=2),
        OrderCreateItemArgs(customer_name=CUSTOMER_NAME, model="MK-01",
                            unit_price=120.0, quantity=5),
    ])
    result = prepare_order_create(_prepare_ctx(_PrepareFakeErp(dict(_CUSTOMER_ROW))), args)
    card = result.card

    display = card["fields_to_write"]
    assert set(display) == {
        "1. 客户 示例机械有限公司 / MK-01",
        "2. 客户 示例机械有限公司 / MK-01",
    }
    assert card["row_header_label"] == "明细"
    assert card["row_quantifier"] == "条"
    assert [item["index"] for item in result.execution["items"]] == [1, 2]
    assert result.execution["batch_size"] == 2
    assert "items" in result.execution


def test_create_batch_rejects_exact_duplicate_items() -> None:
    """完全重复明细 → fail closed（订单重复创建高危）。"""
    item = {"customer_name": CUSTOMER_NAME, "model": "MK-01",
            "unit_price": 120.0, "quantity": 2}
    args = OrderCreateArgs(order_type="metal", items=[
        OrderCreateItemArgs(**item), OrderCreateItemArgs(**item),
    ])
    with pytest.raises(ToolPreparationError, match="完全重复"):
        prepare_order_create(_prepare_ctx(_PrepareFakeErp(dict(_CUSTOMER_ROW))), args)


def test_create_args_items_and_flat_mutually_exclusive() -> None:
    """items 与顶层单条字段同时提供 → 校验拒绝（fail closed）。"""
    with pytest.raises(Exception):
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        items=[OrderCreateItemArgs(model="MK-01")])


# ---- 批量创建执行：全成功 / 部分失败 / 恢复 / 审计 2N ----

def _two_batch_items(*, quantities=(2, 5)) -> list[OrderBatchItem]:
    # item_id 已按 build_item_ids 去重（同业务标识加 #n 后缀），与 tools 层一致
    return [
        OrderBatchItem(index=i, item_id=f"metal:{CUSTOMER_ID}:MK-01:2026-08-22#{i}",
                       label=f"客户 {CUSTOMER_NAME} / MK-01", action=ACTION_CREATE,
                       draft=make_draft(quantity=q))
        for i, q in enumerate(quantities, start=1)
    ]


def _create_call_count(erp) -> int:
    return len([c for c in erp.calls if "idempotency_key" in c and "order_id" not in c])


def test_batch_create_all_success_and_audit_2n(ctx) -> None:
    """全成功 → 任务 succeeded；每条 1 次写调用；attempt+result 事件 = 2N。"""
    erp, tasks, audit, executor, client = ctx
    task = create_task(tasks)
    summary = executor.execute_batch(task_id=task.task_id, items=_two_batch_items())

    assert summary.verification_passed is True
    assert summary.counts["created"] == 2
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert _create_call_count(erp) == 2
    events = audit.events(task_id=task.task_id)
    assert sum(1 for e in events if e.event_type == EVENT_ATTEMPT) == 2
    assert sum(1 for e in events if e.event_type == EVENT_RESULT) == 2


def test_batch_create_partial_failure_awaiting_recovery_then_skip(ctx) -> None:
    """部分失败 → awaiting_recovery 且任务保持 running；恢复只补未完成项
    （已成功条目 op_id 命中 success → skipped，零写调用）。"""
    erp, tasks, audit, _executor, client = ctx
    keys = iter(["k-item-1", "k-item-2"])

    def key_factory():
        return next(keys, generate_idempotency_key())

    executor = make_executor(client=client, tasks=tasks, audit=audit,
                             key_factory=key_factory)
    task = create_task(tasks)
    items = _two_batch_items()
    erp.fail_reject("k-item-2")  # 第 2 条 ERP 业务拒绝

    summary = executor.execute_batch(task_id=task.task_id, items=items)
    assert summary.verification_passed is False
    assert summary.counts["created"] == 1
    assert summary.counts["failed"] == 1
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING
    assert record.current_step == "awaiting_recovery"

    # 恢复：放行第 2 条；第 1 条已有成功 result → skipped，零写调用
    erp.reject_keys.clear()
    before_calls = _create_call_count(erp)
    summary2 = executor.execute_batch(task_id=task.task_id, items=items)
    after_calls = _create_call_count(erp)
    assert after_calls - before_calls == 1  # 只补第 2 条
    assert summary2.counts["skipped"] == 1
    assert summary2.counts["created"] == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


# ---- 批量更新：整卡失效零写入 / 全成功含 unchanged / 部分失败 ----

def _metal_payload(**overrides) -> dict:
    row = make_metal_order()
    row.update(overrides)
    return row


def _update_batch_items(erp, plan_specs) -> tuple[list[OrderUpdateBatchItem], str]:
    """按 (order_id, desired) 构造批量更新明细 + 整卡哈希（= 确认哈希）。"""
    labels = {10: "订单（/ MK-01 / 2026-08-20）", 20: "订单（/ MK-02 / 2026-08-20）"}
    items: list[OrderUpdateBatchItem] = []
    for i, (order_id, desired) in enumerate(plan_specs, start=1):
        plan = plan_update_item(order_type="metal", order_id=order_id,
                                desired=desired, before=erp.orders[order_id])
        items.append(OrderUpdateBatchItem(
            index=i, item_id=f"metal:{order_id}", label=labels[order_id],
            action=plan.action, order_id=order_id, desired=desired,
            before=plan.before, changes=plan.changes,
            expected_updated_at=plan.expected_updated_at,
        ))
    card = build_update_batch_confirmation_card(order_type="metal", items=items)
    return items, card.payload_hash


def test_batch_update_whole_card_invalidation_zero_writes(ctx) -> None:
    """确认后任一条快照变化 → 整卡哈希变化 → 整批零写入 + 任务 failed。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp, 10)
    seed_metal(erp, 20, model="MK-02")
    task = create_task(tasks, duty_code="R3", structured_intent={"duty": "R3"})
    items, confirmed = _update_batch_items(
        erp, [(10, {"notes": "a"}), (20, {"notes": "b"})]
    )
    executor = make_update_executor(client, tasks, audit)
    # 确认后、执行前他人改了第 2 条（updated_at 推进 → 整卡哈希失配）
    erp.apply_other_writer_update(20, {"notes": "other edit"})

    summary = executor.execute_batch(
        task_id=task.task_id, order_type="metal", items=items, confirmed_hash=confirmed,
    )

    assert summary.verification_passed is False
    assert summary.error_code == ERROR_UPDATE_CONFIRMATION_INVALID
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert erp.orders[10]["notes"] is None
    assert erp.orders[20]["notes"] == "other edit"
    assert [c for c in erp.calls if "expected_updated_at" in c] == []  # 零写


def test_batch_update_all_success_with_unchanged_item(ctx) -> None:
    """一条有变化、一条已满足（unchanged）→ 只写 1 次，unchanged 零写。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp, 10)
    seed_metal(erp, 20, model="MK-02", notes="same")
    task = create_task(tasks, duty_code="R3", structured_intent={"duty": "R3"})
    items, confirmed = _update_batch_items(
        erp, [(10, {"notes": "a"}), (20, {"notes": "same"})]
    )
    assert items[1].action == ACTION_UNCHANGED
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute_batch(
        task_id=task.task_id, order_type="metal", items=items, confirmed_hash=confirmed,
    )

    assert summary.verification_passed is True
    assert summary.counts["updated"] == 1
    assert summary.counts["unchanged"] == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert len([c for c in erp.calls if "expected_updated_at" in c]) == 1
    assert erp.orders[10]["notes"] == "a"


def test_batch_update_partial_failure_awaiting_recovery(ctx) -> None:
    """一条成功、一条失败（未达上限）→ 任务保持 running + awaiting_recovery。"""
    erp, tasks, audit, _executor, client = ctx
    seed_metal(erp, 10)
    seed_metal(erp, 20, model="MK-02")
    task = create_task(tasks, duty_code="R3", structured_intent={"duty": "R3"})
    items, confirmed = _update_batch_items(
        erp, [(10, {"notes": "a"}), (20, {"notes": "b"})]
    )
    erp.fail_network_persistent_update(20)  # 第 2 条持续网络失败（未应用）
    executor = make_update_executor(client, tasks, audit)

    summary = executor.execute_batch(
        task_id=task.task_id, order_type="metal", items=items, confirmed_hash=confirmed,
    )

    assert summary.verification_passed is False
    assert summary.error_code == "BATCH_PARTIAL_FAILURE"
    assert summary.counts["updated"] == 1
    assert summary.counts["failed"] == 1
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING
    assert record.current_step == "awaiting_recovery"


def test_update_args_reject_duplicate_order_id() -> None:
    """同一张卡内 order_id 重复 → 校验拒绝（fail closed）。"""
    with pytest.raises(Exception):
        OrderUpdateArgs(order_type="metal", items=[
            OrderUpdateItemArgs(order_id=10, notes="a"),
            OrderUpdateItemArgs(order_id=10, notes="b"),
        ])


# ---------------------------------------------------------------------------
# [2026-09-12 加固] 隐式契约守护：args 可更新字段 ⊆ 执行器写前查询字段集
# ---------------------------------------------------------------------------


def test_update_item_args_fields_covered_by_query_fields() -> None:
    """OrderUpdateItemArgs 各类型可更新字段必须都在执行器写前查询字段集内。

    隐式契约（与收支侧对称，两条路径重建同一张确认卡哈希）：
    - prepare 侧用 ``_query_all``（**不带 fields**，ERP 返回全字段）取快照；
    - 执行器侧 ``_query_before`` 用显式白名单 ``_UPDATE_QUERY_FIELDS[order_type]``
      取快照；
    - 两者都喂给 ``build_update_changes`` 逐字段比较现值——若某个 args 可更新
      字段不在白名单里，执行器读到 ``None``、prepare 读到真实值 → 两侧 changes
      不同 → 整卡哈希永远对不上 → **合并/单条更新退化为「确认永远失效」，功能
      完全不可用**。

    口径与排除依据：
    - 排除 ``order_id``：目标订单主键（定位 + 哈希身份绑定），不是「期望变化
      字段」，不会进入 changes；
    - 分 metal/powder 断言（两侧白名单不同，mcp-api.md §7.1）；
    - 可更新字段取「args 字段 ∩ 该类型契约白名单」：白名单外的字段（含
      ``_METAL/_POWDER_FORBIDDEN_FIELDS`` 与既非白名单又非禁止的字段）由
      ``scopes.verify_update_order_arguments`` 在 prepare 阶段 fail closed
      拦下，**永远不会进入 changes**，故不可能触发本测试守护的哈希错位。
    """
    args_fields = set(OrderUpdateItemArgs.model_fields) - {"order_id"}
    for order_type, whitelist in (
        ("metal", UPDATE_WHITELIST_METAL),
        ("powder", UPDATE_WHITELIST_POWDER),
    ):
        query_fields = set(_UPDATE_QUERY_FIELDS[order_type])
        allowed = args_fields & whitelist
        missing = sorted(allowed - query_fields)
        assert not missing, (
            f"{order_type} 侧 OrderUpdateItemArgs 可更新字段 {missing} 不在 "
            f"_UPDATE_QUERY_FIELDS[{order_type!r}] 内。后果：执行器写前重查取不到"
            "该字段现值（读到 None），与 prepare 全字段快照算出的 changes 不一致 "
            "→ 确认卡哈希永远对不上 → 更新功能不可用。请在白名单/查询字段集补上。"
        )
        # 白名单全集也必须在查询集内：防有人把 _UPDATE_QUERY_FIELDS 改成非派生
        # （手工硬编码漏字段）——那会让某个可改字段悄悄读不到现值。
        wl_missing = sorted(set(whitelist) - query_fields)
        assert not wl_missing, (
            f"_UPDATE_QUERY_FIELDS[{order_type!r}] 未覆盖白名单字段 {wl_missing}："
            "执行器写前查询读不到该字段现值 → changes 与 prepare 不一致 → 确认卡"
            "哈希永远对不上。_UPDATE_QUERY_FIELDS 应由对应白名单派生。"
        )
