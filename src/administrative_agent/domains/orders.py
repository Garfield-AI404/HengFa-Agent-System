"""R2 钣金订单适配层（规格 2.6 domains/orders.py：订单创建和更新适配）。

D13（RV-19）：7.3 轻确认六要素卡片 + 参数快照。
7.3 轻确认六要素（L875-886）：
1. 操作类型；
2. 目标客户/供货商订单；
3. 将要写入的字段和值；
4. 自动补全字段及来源（L543：来源/旧值/新值及影响）；
5. 预计影响；
6. 无法由 ERP 预先确定的限制。

确认后保存 confirmed_payload_hash（D6 TaskRecord.confirmed_payload_hash）；
实际执行内容、目标记录或关键字段变化 → 哈希变化 → 确认失效
（is_confirmation_valid，7.3：变化失效必须重新确认）。

D14（RV-20）：5.3 订单创建执行（L740-751）+ 9.3 + 14.3 场景 11。
- 确认（waiting_confirmation → running）后执行 MetalOrderExecutor：
  载荷来自 MetalOrderDraft.to_write_dict()（恒不含 total_amount）；
  幂等键生成/复用/换 key（5.3 步骤 3-4，mcp-api.md §6.1/§6.3）；
  create_order_bundle 调用与 replayed/conflict/in_progress/网络未知分支；
  审计 result（11.2 L1226：success/replayed/failed/conflict）。

纯规则层（2.6 规则 4）：卡片/快照/幂等键决策无 IO；执行器依赖
mcp/audit/tasks（同 attendance.py 依赖方向）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Protocol, Sequence

import structlog

from administrative_agent.audit.service import AuditService, TaskAttemptRecord
from administrative_agent.business.order_rules import (
    FilledField,
    MetalOrderDraft,
    surcharge_errors,
)
from administrative_agent.mcp.client import MCPClient, ToolResult
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.scopes import (
    UPDATE_WHITELIST_METAL,
    UPDATE_WHITELIST_POWDER,
    verify_order_bundle_arguments,
    verify_update_order_arguments,
)
from administrative_agent.tasks.service import TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.orders")

#: 操作类型（10.2：operation 来自代码枚举，不接受用户提供）
OP_CREATE_METAL_ORDER = "创建订单（钣金）"


@dataclass(frozen=True)
class ConfirmationCard:
    """一次轻确认的完整卡片（7.3 六要素 + payload_hash）。

    - fields_to_write：将要写入的字段与值（有序 (field, value) 对）；
    - auto_filled：自动补全字段及来源（要素 4）；
    - payload_hash：操作类型+目标+全部写值的哈希（7.3 变化失效依据）；
    - hash_fields：[2026-09-10 修复] 参与哈希的**原始执行载荷**——未经
      本地化、未隐藏内部主键、不含派生展示字段（关联方名称等）。None 时
      回退 fields_to_write（未改造卡零行为变化）。展示层 fields_to_write
      的非确定性（如执行期重查关联方名称失败退化为占位）绝不污染哈希。
    - hash_target：[2026-09-10 修复] 参与哈希的目标串；None 时用 target。
      仅当 target 含派生展示内容（如写前快照里的关联方名称，执行期重查
      可能改名/缺失）时单独给出不含派生的稳定目标串，展示仍用 target。
    """

    operation_type: str
    target: str
    fields_to_write: tuple[tuple[str, object], ...]
    auto_filled: tuple[FilledField, ...]
    expected_impact: str
    limitations: tuple[str, ...]
    payload_hash: str
    hash_fields: tuple[tuple[str, object], ...] | None = None
    hash_target: str | None = None
    #: [2026-09-12 批量卡] 两个**纯展示级**字段（默认 None = 旧卡零回归）：
    #:   row_header_label：矩阵卡首列（行头）的列名（如订单/收支批量卡的「明细」、
    #:     考勤卡的「员工」）；
    #:   row_quantifier：折叠分组的量词（如「条」「人」）。
    #: 二者**绝不进哈希**——card_payload_hash 只读 hash_fields/hash_target；
    #: to_dict() 条件输出（None 时不落 JSON 键），保证旧卡 JSON 逐字节不变。
    row_header_label: str | None = None
    row_quantifier: str | None = None
    #: [2026-10-05 确认卡改进第 3 批] source_note：**纯展示级**口径提示键
    #: （如收支卡的「分类由模型按你的说法判定…」）。默认 None = 旧卡零回归；
    #: 与 row_header_label 同口径——card_payload_hash 只读 hash_fields/
    #: hash_target，本键绝不进哈希；to_dict() 条件输出（未设置时不落 JSON 键）。
    source_note: str | None = None

    def to_dict(self) -> dict:
        """卡片结构化视图（供确认环节展示/审计）。

        [2026-09-12 批量卡] row_header_label/row_quantifier 为纯展示级字段，
        未设置（None）时**不落 JSON 键**——旧卡 to_dict() 逐字节不变（在飞卡
        与既有回归测试零影响）；设置时追加到末尾，不进 payload_hash。

        [2026-10-05 第 3 批] source_note 同为纯展示级（None 时不落键，不进
        哈希）；整卡级 source_quote（用户原话对照）由写分支集中注入 card
        JSON，不经本 dataclass（见 tools_node 写分支）。
        """
        data = {
            "operation_type": self.operation_type,
            "target": self.target,
            "fields_to_write": dict(self.fields_to_write),
            "auto_filled": [
                {
                    "field": f.field,
                    "value": f.value,
                    "source": f.source,
                    "previous_value": f.previous_value,
                }
                for f in self.auto_filled
            ],
            "expected_impact": self.expected_impact,
            "limitations": list(self.limitations),
            "payload_hash": self.payload_hash,
        }
        if self.row_header_label is not None:
            data["row_header_label"] = self.row_header_label
        if self.row_quantifier is not None:
            data["row_quantifier"] = self.row_quantifier
        if self.source_note is not None:
            data["source_note"] = self.source_note
        return data


#: 确认卡字段名中文化映射（[2026-08-28 确认卡字段中文化]：仅展示层；
#: 执行载荷（MetalOrderDraft.to_write_dict 等）保持 ERP 英文键契约不变；
#: payload_hash 在同一构造路径计算，7.3 一致性不受影响）。
CONFIRMATION_FIELD_LABELS: dict[str, str] = {
    # 订单（metal/powder）
    "customer_id": "客户ID", "model": "型号", "unit_price": "单价",
    "price_type": "价格类型", "weight_unit": "重量单位", "quantity": "数量",
    "quantity_unit": "数量单位", "weight_per_unit": "单件重量",
    "surcharge_amount": "附加款",
    "in_date": "入库日期", "out_date": "完成日期", "status": "状态",
    "notes": "备注",
    "supplier_id": "供货商ID", "color_code": "色号", "total_weight": "总重量",
    "price_per_kg": "单价（元/kg）", "expected_updated_at": "预期更新时间",
    # 考勤
    "employee_ref": "员工", "action": "操作",
    "leave_minutes": "请假（分钟）", "overtime_minutes": "加班（分钟）",
    # [2026-09-12 批量卡] 明细内层 fields → 「字段和值」（每条明细的动作与
    # 完整写值；批量卡逐行展开本体）。
    "fields": "字段和值",
    # 对账
    "template_key": "模板键", "mapping_version": "映射版本",
    "source_file_hash": "源文件哈希", "entity": "对账实体", "item_count": "条目数",
    # 收支（finance_create/finance_update；customer_id/supplier_id/notes/
    # expected_updated_at 已在上方订单分组登记，键唯一不复写）
    "tx_type": "收支类型", "category": "分类", "project": "项目",
    "amount": "金额（元）", "tx_date": "日期",
    # [2026-09-10 P1-1] 关联方名称展示键（customer_id/supplier_id 被
    # CONFIRMATION_HIDDEN_FIELDS 隐藏后，以解析出的名称呈现，不静默丢信息）
    "customer_name": "关联客户", "supplier_name": "关联供货商",
    # [2026-09-10 确认卡改版] 订单创建卡首行展示客户/供货商名称（创建卡
    # target 置空后名称移入 fields_to_write 首行）。仅展示层键名——不进
    # hash_fields；哈希口径由卡片的 hash_target（旧「客户/供货商 名称」串）
    # 保持逐字节等价。键名不与既有展示键冲突，也不在 CONFIRMATION_HIDDEN_FIELDS。
    "customer_display_name": "客户", "supplier_display_name": "供货商",
}

#: 计划动作值中文化（考勤卡 action 值 created/updated/unchanged）。
ACTION_VALUE_LABELS: dict[str, str] = {
    "created": "新建", "updated": "更新", "unchanged": "保持",
}


def localize_confirmation_field(key: str) -> str:
    """字段名中文化（未登记键原样返回）。"""
    return CONFIRMATION_FIELD_LABELS.get(key, key)


#: 确认卡不展示的内部关联主键（[2026-09-10 展示口径] 用户只记名称、记不住 id，
#: 客户/供货商改以名称呈现——名称已在卡片 target 给出）。**仅展示层过滤**：
#: 执行载荷（MetalOrderDraft/PowderOrderDraft.to_write_dict() 等）原样保留
#: customer_id/supplier_id，写入 ERP 的真实参数与两段式确认载荷均不变。
CONFIRMATION_HIDDEN_FIELDS: frozenset[str] = frozenset({"customer_id", "supplier_id"})


# ---------------------------------------------------------------------------
# 批量明细卡公共常量与辅助（[2026-09-12 一张卡多条明细]）
# ---------------------------------------------------------------------------
#: 明细条目内层键（原始执行语义，与考勤卡同构）：
#:   action：本条明细的动作（created/updated/unchanged，经 ACTION_VALUE_LABELS
#:     中文化展示）；
#:   fields：本条明细的完整写值（展示层译为「字段和值」，逐行展开）。
DETAIL_FIELDS_KEY = "fields"
DETAIL_ACTION_KEY = "action"
ACTION_CREATE = "created"
ACTION_UPDATE = "updated"
ACTION_UNCHANGED = "unchanged"

#: 批量卡 raw（hash_fields）外层键前缀——用**位置序号**而非业务标识，避免
#: 展示键里的关联方名称在执行期重查时改名/缺失而污染哈希（见
#: build_update_confirmation_card 的 hash_target 教训）。展示外层键另行取
#: f"{i}. {业务标识}"，两个 dict() 折叠同名键的风险由序号消除。
BATCH_HASH_PREFIX = "detail-"


def localize_confirmation_fields(
    fields: tuple[tuple[str, object], ...],
) -> tuple[tuple[str, object], ...]:
    """确认卡写值字段名中文化 + 隐藏内部关联主键（展示层；含嵌套 dict 与
    action 枚举值）。

    customer_id/supplier_id（CONFIRMATION_HIDDEN_FIELDS）不进卡片展示：
    用户只认名称、id 无业务含义（执行载荷不受影响，见常量注释）。
    """
    def t(value: object) -> object:
        if isinstance(value, dict):
            return {
                localize_confirmation_field(k): t(v)
                for k, v in value.items()
                if k not in CONFIRMATION_HIDDEN_FIELDS
            }
        if isinstance(value, str):
            return ACTION_VALUE_LABELS.get(value, value)
        return value

    return tuple(
        (localize_confirmation_field(k), t(v))
        for k, v in fields
        if k not in CONFIRMATION_HIDDEN_FIELDS
    )


def build_batch_detail_rows(
    items: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[tuple[tuple[str, object], ...], tuple[tuple[str, object], ...]]:
    """批量明细卡公共构造辅助（纯函数，供订单/收支 4 条路径共用）。

    入参 items：``[(业务标识, {"action": ..., "fields": {...}}), ...]``
    （业务标识如「客户 铭泰 / M1」，取不到时调用方降级）。

    返回 ``(hash_fields, fields_to_write)``：

    - raw（hash_fields）外层键用 ``f"{BATCH_HASH_PREFIX}{i}"``（i 从 1 起），
      **不含业务标识**——展示键里的关联方名称执行期可能改名/缺失，绝不污染
      哈希；内层 ``action``/``fields`` 为原始执行语义（未本地化）；
    - 展示（fields_to_write）外层键用 ``f"{i}. {业务标识}"``，**序号保证唯一**
      （同客户同型号两条明细不会因 dict() 折叠而丢行）；内层经
      ``localize_confirmation_fields`` 中文化（action→操作 值译中文、
      fields→字段和值、递归隐藏 customer_id/supplier_id）。

    **两条同业务标识的明细**：len(dict(fields_to_write)) == 2 且 hash_fields
    两条都进哈希（序号键互不冲突）——这是防 dict() 静默折叠的关键约束。
    """
    raw_fields: list[tuple[str, object]] = []
    display_fields: list[tuple[str, object]] = []
    for i, (label, entry) in enumerate(items, start=1):
        raw_fields.append((f"{BATCH_HASH_PREFIX}{i}", entry))
        display_fields.append((f"{i}. {label}", entry))
    return (
        tuple(raw_fields),
        localize_confirmation_fields(tuple(display_fields)),
    )


def localize_auto_filled(fields: tuple[FilledField, ...]) -> tuple[FilledField, ...]:
    """自动补全字段名中文化（FilledField.field 为展示键；source/previous_value 不动）。"""
    return tuple(replace(f, field=localize_confirmation_field(f.field)) for f in fields)


def canonical_json(data: Any) -> str:
    """规范化 JSON（request_hash 同口径，mcp-api.md §6.3：排序 JSON、
    Decimal/日期/布尔/null 稳定、不含认证信息/operation_id/trace_id）。

    sort_keys=True + ensure_ascii=False + 紧凑分隔符——同义 JSON 得到
    同一串；业务载荷只含业务字段，天然满足"不含认证/关联字段"。
    """
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload_hash_for(
    operation_type: str,
    target: str,
    fields_to_write: tuple[tuple[str, object], ...],
) -> str:
    """轻确认载荷哈希（7.3 变化失效依据）。

    参与字段：操作类型 + 目标 + 全部写值（自动补全值含在内——任何
    补全值变化 → 哈希变化 → 确认失效，须重新确认）。

    规范化：canonical_json（与 D6 TaskService.request_confirmation、
    mcp-api request_hash 同口径）。
    """
    payload = {
        "operation_type": operation_type,
        "target": target,
        "fields": dict(fields_to_write),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_metal_confirmation_card(draft: MetalOrderDraft) -> ConfirmationCard:
    """补全草稿 → 钣金订单轻确认卡片（7.3 六要素，5.3 步骤 8 语义）。
    [2026-08-28 确认卡字段中文化] 展示层翻译，执行载荷不变。

    要素 5 预计影响：ERP 创建状态"未完成"的钣金订单；不创建
    ar_payments/ap_payments/transactions（5.3 步骤 8）。
    要素 6 限制：total_amount 由 ERP 计算不可预填；weight_per_unit=0
    仅表示待补充；最终校验以 ERP 创建响应为准（订单号等，D14 实现）。

    [2026-09-10 展示口径] 目标只展示客户名称（不写 customer_id）。

    [2026-09-10 确认卡改版] target 置空（前端不渲染「目标」行），客户名称
    改以 fields_to_write 首行「客户」呈现；hash_target 仍取旧的
    f"客户 {customer_name}" 串，故 payload_hash 与改版前逐字节一致——
    card_payload_hash 优先用 hash_target，哈希口径零变化。

    hash_fields：[2026-09-10 修复] 取原始 to_write_dict()（含 customer_id/
    supplier_id 等真实执行载荷）——本已含身份主键，无需额外添加；展示层
    的本地化/隐藏/首行名称行不影响哈希。
    """
    raw_fields = tuple(draft.to_write_dict().items())
    card = ConfirmationCard(
        operation_type=OP_CREATE_METAL_ORDER,
        target="",
        fields_to_write=localize_confirmation_fields(
            (("customer_display_name", draft.customer_name),) + raw_fields
        ),
        hash_fields=raw_fields,
        hash_target=f"客户 {draft.customer_name}",
        auto_filled=localize_auto_filled(draft.auto_filled()),
        expected_impact=(
            "在 ERP 创建状态为“未完成”的钣金订单；"
            "不创建 ar_payments/ap_payments/transactions（5.3 步骤 8）。"
        ),
        limitations=(
            "订单总金额 total_amount 由 ERP 计算（基础金额 数量×单件重量×单价"
            "＋附加款 surcharge_amount），不在此确认范围内；",
            "weight_per_unit=0 仅表示待补充，需后续补录实际重量；",
            "最终校验以 ERP 创建响应为准（订单号等字段，D14 落库比对）。",
        ),
        payload_hash="",
    )
    return replace(card, payload_hash=card_payload_hash(card))


def card_payload_hash(card: ConfirmationCard) -> str:
    """卡片当前内容重算哈希（7.3 一致性口径）。

    [2026-09-10 修复] 优先以 card.hash_fields（原始执行载荷）参与哈希，
    None 时回退 card.fields_to_write（旧行为）——展示层的非确定性不污染
    执行期校验。所有卡构造点与校验点共用本函数，保证同口径。
    """
    fields = (
        card.hash_fields if card.hash_fields is not None else card.fields_to_write
    )
    target = card.hash_target if card.hash_target is not None else card.target
    return payload_hash_for(card.operation_type, target, fields)


def is_confirmation_valid(card: ConfirmationCard, confirmed_hash: str) -> bool:
    """确认是否仍有效（7.3：内容/目标/关键字段变化 → 失效重新确认）。

    以卡片当前内容（hash_fields 优先，回退 fields_to_write）重算哈希与
    确认时保存的 confirmed_hash 比对；不一致（任一写值、目标或操作类型
    变化）→ False。
    """
    return card_payload_hash(card) == confirmed_hash


@dataclass(frozen=True)
class OrderParameterSnapshot:
    """单元交付：钣金订单参数快照（确认卡片 + 任务上下文归档）。

    - parameters：最终写入参数（确认时的写值；单条卡 = 写值字典）；
    - items：**按条归档**（[2026-09-12 一张卡多条明细]）——批量卡每行一条，
      形如 ``(业务标签, 原始执行 entry)``（原始 entry = hash_fields 内层
      ``{"action": ..., "fields": {...}}``，未本地化）；单条卡为空元组
      （parameters 已完整承载，逐条字段无意义）；
    - auto_filled：自动补全字段及来源（审计可追溯）；
    - total_amount_excluded：恒 True（5.1：total_amount 禁止传入）；
    - payload_hash：与卡片一致的载荷哈希（校验可对账）。
    """

    task_id: str
    operation_type: str
    target: str
    parameters: dict
    auto_filled: tuple[FilledField, ...]
    payload_hash: str
    total_amount_excluded: bool = True
    created_at: str = ""
    items: tuple[tuple[str, dict], ...] = ()

    def to_dict(self) -> dict:
        data = {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "target": self.target,
            "parameters": dict(self.parameters),
            "auto_filled": [
                {
                    "field": f.field,
                    "value": f.value,
                    "source": f.source,
                    "previous_value": f.previous_value,
                }
                for f in self.auto_filled
            ],
            "payload_hash": self.payload_hash,
            "total_amount_excluded": self.total_amount_excluded,
            "created_at": self.created_at,
        }
        # 条件输出（None 不落 JSON 的既有风格）：单条卡 items 为空、不出现该键，
        # 保证单条快照 JSON 与改造前逐字节一致。
        if self.items:
            data["items"] = [
                {"label": label, "entry": dict(entry)} for label, entry in self.items
            ]
        return data


def _snapshot_batch_items(card: ConfirmationCard) -> tuple[tuple[str, dict], ...]:
    """批量卡 → 按条归档元组（[2026-09-12 一张卡多条明细]）。

    仅批量卡（hash_fields 外层键全为 ``detail-{i}``，见 build_batch_detail_rows
    / build_update_batch_confirmation_card）返回非空；单条卡返回空元组。
    hash_fields 与 fields_to_write 由各批量卡构造点**同序同长**产出，
    逐位置配对即可取回「业务标签（展示外层键 ``f"{i}. {标签}"`` 去掉序号
    前缀）+ 原始执行 entry（未本地化，含 id/主键，供审计对账）」。
    """
    raw = card.hash_fields or ()
    if not raw or not all(
        str(key).startswith(BATCH_HASH_PREFIX) for key, _ in raw
    ):
        return ()
    display = card.fields_to_write or ()
    # [2026-09-12 fail-closed] 本函数靠「hash_fields 与 fields_to_write 同序
    # 同长」逐位置配对取回标签/执行 entry；不同长即配对错位（会静默张冠李戴
    # 归档条目）→ 明确 fail closed，绝不将错就错。
    if len(raw) != len(display):
        raise ValueError(
            f"批量卡 hash_fields 与 fields_to_write 长度不一致"
            f"（{len(raw)} vs {len(display)}）：无法逐位置配对归档，"
            "拒绝生成快照（fail closed）"
        )
    items: list[tuple[str, dict]] = []
    for idx, (_raw_key, raw_entry) in enumerate(raw):
        label = ""
        if idx < len(display):
            disp_key, _disp_entry = display[idx]
            # 展示外层键形如 f"{i}. 客户 X / M1"，去掉序号前缀取业务标签
            label = str(disp_key).split(". ", 1)[-1]
        if not label:
            label = f"{idx + 1}. 明细"
        items.append((label, dict(raw_entry) if isinstance(raw_entry, dict) else {"fields": raw_entry}))
    return tuple(items)


def build_parameter_snapshot(
    card: ConfirmationCard,
    *,
    task_id: str,
    now: datetime | None = None,
) -> OrderParameterSnapshot:
    """确认卡片 → 参数快照（单元交付；task_id 由调用方任务上下文提供）。

    now 供测试注入固定时间戳；缺省取 UTC 当前时间（ISO 8601）。

    [2026-09-12 一张卡多条明细] 批量卡额外按条归档 ``items``（每行 = 业务
    标签 + 原始执行 entry）；单条卡 items 为空、parameters 与改造前逐字节
    一致。本函数**当前无生产调用点**（仅单测消费），故扩展字段对在飞链路
    零影响。
    """
    return OrderParameterSnapshot(
        task_id=task_id,
        operation_type=card.operation_type,
        target=card.target,
        parameters=dict(card.fields_to_write),
        auto_filled=card.auto_filled,
        payload_hash=card.payload_hash,
        total_amount_excluded=True,
        created_at=(now or datetime.now(timezone.utc)).isoformat(),
        items=_snapshot_batch_items(card),
    )


# ---------------------------------------------------------------------------
# 幂等键（5.3 步骤 3-4；mcp-api.md §6.1/§6.3）
# ---------------------------------------------------------------------------

#: mcp-api.md §6.1：长度 1-128，仅字母/数字/./_/:/-，以字母或数字开头
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")

#: 幂等键前缀（mcp-api §6.1 示例同型；一次已确认创建动作的唯一键）
_IDEMPOTENCY_KEY_PREFIX = "agent-job-"


def is_valid_idempotency_key(key: str) -> bool:
    """校验幂等键格式（mcp-api.md §6.1：1-128、字符集、字母/数字开头）。"""
    return bool(_IDEMPOTENCY_KEY_RE.match(key))


def generate_idempotency_key() -> str:
    """确认后生成新幂等键（5.3 步骤 3）。

    一次已确认创建动作的唯一键——不由客户、型号、日期、数量等业务字段
    拼接判断重复（5.3 L751）。``agent-job-`` + 32 位 hex 共 42 字符，
    仅字母/数字，满足 §6.1 格式约束。
    """
    return f"{_IDEMPOTENCY_KEY_PREFIX}{uuid.uuid4().hex}"


def resolve_idempotency_key(
    payload: dict[str, Any],
    prior_attempts: Iterable[TaskAttemptRecord],
    key_factory: Callable[[], str] = generate_idempotency_key,
) -> str:
    """5.3 步骤 4 / mcp-api.md §6.1：同一次业务操作重试复用 key；
    payload 变化后必须换 key。

    prior_attempts 为该操作的历次实际调用（audit.attempts(operation_id)，
    幂等键持久化于 task_attempts.idempotency_key——重启后仍可复用）：
    - 任一 prior attempt 的载荷规范化后与本次载荷一致 → 复用其 key；
    - 否则（首次执行，或载荷已变化）→ 生成新 key（5.3 步骤 3）。
    """
    canonical = canonical_json(payload)
    for attempt in reversed(list(prior_attempts)):
        if attempt.idempotency_key and canonical_json(attempt.field_diffs) == canonical:
            return attempt.idempotency_key
    return key_factory()


# ---------------------------------------------------------------------------
# 钣金订单创建执行（5.3 步骤 3-8 / 9.3 / 14.3 场景 11）
# ---------------------------------------------------------------------------

#: 任务终态错误码（13.1）
ERROR_KEY_CONFLICT = "ORDER_IDEMPOTENCY_KEY_CONFLICT"
ERROR_IN_PROGRESS = "ORDER_IDEMPOTENCY_IN_PROGRESS"
ERROR_NETWORK_UNKNOWN = "ORDER_CREATE_NETWORK_UNKNOWN"
ERROR_MCP_FAILURE = "ORDER_CREATE_MCP_FAILURE"
ERROR_FORBIDDEN_FIELD = "ORDER_PAYLOAD_FORBIDDEN"

#: 每次业务操作最多尝试次数（13.2：达上限 → failed 不循环；默认 3）
DEFAULT_RETRY_LIMIT = 3


class PolicyInfo(Protocol):
    """规则包的最小结构化视图（policy 三列进审计，11.1）。
    调用方传入 agent/prompts/policies.POLICY_REGISTRY 对应条目。"""

    policy_key: str
    version: str

    @property
    def hash(self) -> str: ...


@dataclass(frozen=True)
class OrderExecutionSummary:
    """create_order_bundle 执行汇总（5.3 步骤 7 保存响应 + 11.5 指标）。

    - result_status：success / replayed / conflict / failed（11.2 四值）；
    - order_id/order_response/other_effects：ERP 返回（步骤 7）；
    - attempts：本次执行的实际调用次数（11.2 规则 3 独立 attempt）；
    - verification_passed：True 仅当 ERP 返回订单（响应即最终校验）。
    """

    task_id: str
    operation_type: str
    idempotency_key: str
    result_status: str
    order_id: int | None
    order_response: dict[str, Any] | None
    other_effects: tuple[dict[str, Any], ...]
    attempts: int
    verification_passed: bool
    error_code: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "idempotency_key": self.idempotency_key,
            "result_status": self.result_status,
            "order_id": self.order_id,
            "order_response": self.order_response,
            "other_effects": [dict(e) for e in self.other_effects],
            "attempts": self.attempts,
            "verification_passed": self.verification_passed,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


@dataclass(frozen=True)
class OrderBatchItem:
    """[2026-09-12 一张卡多条明细] 批量创建的一条明细（tools 层反序列化后传入）。

    - index：1-based 序号（台账与展示行号）；
    - item_id：稳定业务标识（op_id 命名空间；prepare 阶段已由 build_item_ids
      去重）；
    - action：本条动作（创建卡恒为 ACTION_CREATE）；
    - label：展示业务标识（如「客户 铭泰 / M1」）；
    - draft：已补全的订单草稿（执行内核据此生成写载荷）。
    """

    index: int
    item_id: str
    label: str
    action: str
    draft: MetalOrderDraft


class MetalOrderExecutor:
    """R2 钣金订单创建执行（5.3 步骤 3-8 的确定性实现）。

    确认（TaskService.confirm：waiting_confirmation → running）后调用
    execute；入口再校验任务状态必须 running（8.1 L930 / 8.3 等待与终态
    不执行；同 task 并发由 D6 WriteTaskLock 保证，13.3）。

    流程（5.3）：
    - 载荷来自 D13 MetalOrderDraft.to_write_dict()（恒不含 total_amount），
      再经 verify_order_bundle_arguments 兜底（9.3 检查禁止字段，fail
      closed——不接受 total_amount/amount/id/created_at/updated_at）；
    - 步骤 3：生成/复用 idempotency_key，与 operation_id（任务级稳定
      标识，D6 生成）一起经 attempt 记录持久化（task_attempts）；
    - 步骤 5：调用 create_order_bundle（同 key 同 payload 重试安全）；
    - 步骤 6：replayed=true 视为成功重放，不能再次创建；
    - 步骤 7：保存订单响应和 other_effects（result 事件
      structured_payload）；步骤 8：不创建 ar/ap/transactions
      （ERP 侧行为，响应 receivables/payables/transactions 为空）。

    错误分支（9.3/13.1/mcp-api §6.3）：
    - IDEMPOTENCY_KEY_CONFLICT：result=conflict，报告不创建
      （retryable=false，13.1 保留原 key 状态）；
    - IDEMPOTENCY_IN_PROGRESS：result=failed + requires_operator_action
      ——不得自动重试、不得更换 key 绕过（RV-20 通过标准），转人工；
    - 结果未知（NETWORK/SERVER）→ 同一幂等键重试（13.2：使用同一幂等键
      时可重试；14.3 场景 11：查询并用同一幂等键处理——重试返回
      replayed=true 即为 ERP 按 key 查询确认，不重复创建）；达上限 →
      failed 不循环（13.2）；
    - 其他错误（VALIDATION/AUTH 等）→ failed（retryable=false 不重试）。

    审计（11.2 L1226）：每次实际调用独立 attempt + 调用返回后立即
    result（success/replayed/failed/conflict）；11.5 replayed 数量指标
    来源为 result 事件 result_code="replayed" 计数。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: PolicyInfo | None = None,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        key_factory: Callable[[], str] = generate_idempotency_key,
        target_type: str = "metal_order",
    ) -> None:
        self._client = client
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy
        self._retry_limit = retry_limit
        self._key_factory = key_factory
        #: 审计 target_type（5.2 塑粉复用本执行器：传入 "powder_order"）
        self._target_type = target_type

    # ---- 入口（5.3 步骤 3-8） ----

    def execute(self, *, task_id: str, draft: MetalOrderDraft) -> OrderExecutionSummary:
        """确认后执行单条钣金订单创建（载荷 = draft.to_write_dict()）。

        返回 OrderExecutionSummary；任务终态为 succeeded（created/replayed）
        或 failed（conflict/in_progress/达上限/其他错误），不做部分成功。

        [2026-09-12 一张卡多条明细] 单条路径（含**在飞旧卡**：execution 无
        items 键）语义逐字节不变——执行内核 `_execute_one` 与旧实现逐分支
        同构；任务级终态（mark_succeeded/_fail_task）保留在本方法（批量路径
        改由 BatchExecutor 按条目聚合）。
        """
        self._require_running(task_id)
        payload = draft.to_write_dict()

        # 5.3 步骤 3：operation_id 为任务级稳定标识（D6 生成，重试复用）
        task = self._tasks.get_task(task_id)
        op_id = task.operation_id
        try:
            summary = self._execute_one(
                task_id=task_id, op_id=op_id, payload=payload
            )
        except MCPError as exc:
            # 9.3 禁止字段（D13 提取层已拒；此处 fail closed 兜底）：
            # _execute_one 原样抛出，本方法补任务终态后继续抛（旧行为不变）
            self._fail_task(
                task_id, ERROR_FORBIDDEN_FIELD, f"订单载荷含禁止字段：{exc}"
            )
            raise

        if summary.result_status in ("success", "replayed"):
            self._tasks.mark_succeeded(task_id, verification_passed=True)
        else:
            self._fail_task(
                task_id,
                summary.error_code or ERROR_MCP_FAILURE,
                summary.error_summary or "订单创建失败",
            )
        return summary

    def _execute_one(
        self, *, task_id: str, op_id: str, payload: dict[str, Any]
    ) -> OrderExecutionSummary:
        """单条明细执行内核（旧单条路径与批量路径共用；**不做任务终态标记**）。

        保留全部既有分支（9.3 禁止字段、5.3 步骤 3-8 的 replayed/conflict/
        in_progress/结果未知/其他错误、达上限不循环）；任务级终态由调用方
        聚合：单条路径 execute() 直接标记，批量路径由 BatchExecutor 合成。

        op_id 逐条化后 resolve_idempotency_key 自动为每条产出独立幂等键
        （resolve_idempotency_key 本身零改动）。

        禁止字段分支**抛出** MCPError（单条路径需保持旧「抛异常」语义）；
        批量路径的 runner 捕获后记该条 failed + 零写调用。
        """
        # 9.3 检查禁止字段（D13 提取层已拒 total_amount；此处 fail closed
        # 兜底，接受不到 total_amount/amount/id/created_at/updated_at）
        verify_order_bundle_arguments({"order": payload})

        # 5.3 步骤 3-4：idempotency_key 生成/复用后随首次 attempt 持久化
        key = resolve_idempotency_key(
            payload, self._audit.attempts(op_id), self._key_factory
        )

        # 契约 mcp-api.md §6.1：必填 order_type（metal=钣金 / powder=塑粉）。
        # 按审计 target_type 显式映射（与 update_order 侧
        # f"{order_type}_order" 互为逆关系）；未知值 fail closed
        order_type = {"metal_order": "metal", "powder_order": "powder"}[self._target_type]

        attempts = 0
        last_unknown: tuple[str, str] | None = None  # (code, message) 结果未知
        while attempts < self._retry_limit:
            attempts += 1
            attempt = self._record_attempt(task_id, op_id, key, payload)
            result = self._client.call_tool(
                "create_order_bundle",
                {"order": payload, "order_type": order_type, "idempotency_key": key},
                operation_id=op_id,
                trace_id=self._trace_id,
            )
            self._record_mcp_retries(result, task_id, op_id)
            if result.ok:
                return self._handle_success(task_id, op_id, attempt, key, result.data, attempts)

            err = result.error
            if err.code == "IDEMPOTENCY_KEY_CONFLICT":
                # 同 key 不同 payload：报告不创建（13.1 保留原 key 状态）
                self._record_result(attempt, "conflict", err)
                summary = (
                    f"幂等键已用于不同载荷（idempotency_key={key}）："
                    "不创建订单、不改原记录；需人工确认意图后生成新操作（13.1）"
                )
                return self._summary(
                    task_id, key, "conflict", attempts, summary,
                    error_code=ERROR_KEY_CONFLICT,
                )

            if err.code == "IDEMPOTENCY_IN_PROGRESS":
                # 异常遗留 PENDING：不自动重试、不换 key 绕过（RV-20 标准）
                self._record_result(attempt, "failed", err, operator=True)
                summary = (
                    f"幂等键存在异常遗留 PENDING（idempotency_key={key}）："
                    "不得自动重试、不得更换 key 绕过，需人工核查清理后再用原 key 重试"
                )
                return self._summary(
                    task_id, key, "failed", attempts, summary,
                    error_code=ERROR_IN_PROGRESS,
                )

            if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
                # 结果未知：同一幂等键重试（13.2/14.3 场景 11）
                self._record_result(attempt, "failed", err)
                last_unknown = (err.code, err.message)
                continue

            # 其他错误：retryable=false，不重试
            self._record_result(attempt, "failed", err)
            summary = f"create_order_bundle 失败（{err.code}）：{err.message}"
            return self._summary(
                task_id, key, "failed", attempts, summary,
                error_code=ERROR_MCP_FAILURE,
            )

        # 达上限（13.2：failed 不循环；不重复创建）
        code, msg = last_unknown or ("RETRY_LIMIT_REACHED", "重试上限")
        summary = (
            f"create_order_bundle 结果未知且已尝试 {attempts} 次"
            f"（{code}）：不重复创建，需人工核查后用原 key 继续（13.2/场景 11）"
        )
        return self._summary(
            task_id, key, "failed", attempts, summary,
            error_code=ERROR_NETWORK_UNKNOWN,
        )

    def execute_batch(
        self, *, task_id: str, items: Sequence[OrderBatchItem]
    ) -> "BatchSummary":
        """一张卡多条明细的批量创建执行（[2026-09-12]；4.3 顺序 6-10 同构）。

        逐条调 `_execute_one`（每条自己的 op_id → 独立幂等键），任务终态由
        BatchExecutor 聚合：全成功 → succeeded；部分失败未达上限 →
        awaiting_recovery（任务保持 running，恢复只补未完成项）；达上限 →
        failed(ERROR_RETRY_LIMIT)。

        L2 执行期预检：条目在发出任何写调用前失败（禁止字段/写前校验不过）
        → 记该条 failed + 零写调用，其余条目继续（口径 2）。
        """
        from administrative_agent.domains.batch import (
            BatchExecutor,
            BatchItem,
            BatchItemResult,
        )

        by_item_id = {it.item_id: it for it in items}
        executor = BatchExecutor(
            tasks=self._tasks, audit=self._audit, tool_key="order_create"
        )

        def runner(batch_item: BatchItem, op_id: str) -> BatchItemResult:
            order_item = by_item_id[batch_item.item_id]
            try:
                summary = self._execute_one(
                    task_id=task_id,
                    op_id=op_id,
                    payload=order_item.draft.to_write_dict(),
                )
            except MCPError as exc:
                # L2 预检失败（禁止字段等）：零写调用（verify 在 attempt 之前）
                return BatchItemResult(
                    index=batch_item.index,
                    item_id=batch_item.item_id,
                    label=batch_item.label,
                    final_action="failed",
                    status="failed",
                    error_code=ERROR_FORBIDDEN_FIELD,
                    error_summary=f"订单载荷校验未通过：{exc}",
                    note="执行期预检失败，零写调用；其余条目继续（口径 2）",
                )
            if summary.result_status in ("success", "replayed"):
                return BatchItemResult(
                    index=batch_item.index,
                    item_id=batch_item.item_id,
                    label=batch_item.label,
                    final_action=ACTION_CREATE,
                    status="success",
                    note=f"ERP 返回 {summary.result_status}（order_id={summary.order_id}）",
                )
            return BatchItemResult(
                index=batch_item.index,
                item_id=batch_item.item_id,
                label=batch_item.label,
                final_action="failed",
                status="failed",
                error_code=summary.error_code,
                error_summary=summary.error_summary,
                note="单条失败不重放批次；由任务恢复机制处理（13.2）",
            )

        batch_items = [
            BatchItem(
                index=it.index,
                item_id=it.item_id,
                action=it.action,
                label=it.label,
            )
            for it in items
        ]
        return executor.run(task_id=task_id, items=batch_items, runner=runner)

    # ---- 步骤 5-8：成功分支 ----

    def _handle_success(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        key: str,
        data: dict[str, Any],
        attempts: int,
    ) -> OrderExecutionSummary:
        """步骤 6：replayed=true 视为成功重放（不能再次创建）；
        步骤 7：保存订单响应和 other_effects（result 事件载荷）。"""
        replayed = bool(data.get("replayed"))
        order = data.get("order") or {}
        order_id = order.get("id") if isinstance(order.get("id"), int) else None
        other_effects = tuple(data.get("other_effects") or [])
        status = "replayed" if replayed else "success"
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status=status,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code="R2",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            result_summary=(
                f"replayed metal order {order_id}（同 key 同载荷重放）"
                if replayed else f"created metal order {order_id}"
            ),
            structured_payload={
                "order_id": order_id,
                "replayed": replayed,
                "order_type": data.get("order_type"),
                "other_effects": [dict(e) for e in other_effects],
            },
        )
        # 步骤 7 + 8.1：ERP 返回订单即最终校验（5.3 无独立再查询）；
        # 步骤 8：不创建 ar/ap/transactions（ERP 侧行为，响应列表为空）
        # [2026-09-12 一张卡多条明细] 任务级终态**不再在此标记**——单条路径
        # 由 execute() 标记、批量路径由 BatchExecutor 聚合（逐条成功不改变
        # 任务终态）。
        logger.info(
            "metal_order_created",
            task_id=task_id,
            order_id=order_id,
            replayed=replayed,
            idempotency_key=key,
        )
        return self._summary(
            task_id, key, status, attempts,
            error_summary=None,
            order_id=order_id,
            order_response=dict(order),
            other_effects=other_effects,
        )

    # ---- 审计（11.2） ----

    def _record_attempt(
        self, task_id: str, op_id: str, key: str, payload: dict[str, Any]
    ) -> TaskAttemptRecord:
        """步骤 3 持久化载体：attempt 记录幂等键 + 载荷差异（调用前）。"""
        return self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type=self._target_type,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code="R2",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            idempotency_key=key,
            field_diffs=payload,
        )

    def _record_mcp_retries(
        self,
        result: ToolResult,
        task_id: str,
        op_id: str,
        target_id: str | None = None,
    ) -> None:
        """写工具的传输层瞬时重试留痕（11.2 规则 3 可追溯性）。

        重试在 MCP 协议层内部完成，业务层只看到最终结果；连接失败属"结果
        未知"（13.1）——写操作可能已执行而响应丢失，故补记持久化审计事件。
        详见 AuditService.record_mcp_retries（只对写工具调用）。
        """
        if not result.retries:
            return
        self._audit.record_mcp_retries(
            retries=result.retries,
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            target_type=self._target_type,
            target_id=target_id,
            duty_code="R2",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )

    def _record_result(
        self,
        attempt: TaskAttemptRecord,
        status: str,
        err: MCPError,
        operator: bool | None = None,
    ) -> None:
        """调用返回后立即 result（11.2 L1226：success/replayed/failed/conflict）。"""
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status=status,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code="R2",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            error_code=err.code,
            requires_operator_action=(
                operator if operator is not None
                else bool(err.requires_operator_action)
            ),
            result_summary=err.message or status,
        )

    # ---- 汇总与内部 ----

    def _summary(
        self,
        task_id: str,
        key: str,
        result_status: str,
        attempts: int,
        error_summary: str | None,
        *,
        error_code: str | None = None,
        order_id: int | None = None,
        order_response: dict[str, Any] | None = None,
        other_effects: tuple[dict[str, Any], ...] = (),
    ) -> OrderExecutionSummary:
        return OrderExecutionSummary(
            task_id=task_id,
            operation_type=OP_CREATE_METAL_ORDER,
            idempotency_key=key,
            result_status=result_status,
            order_id=order_id,
            order_response=order_response,
            other_effects=other_effects,
            attempts=attempts,
            verification_passed=order_id is not None,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _require_running(self, task_id: str) -> None:
        """执行前校验：任务必须 running（8.1 L930：确认后转 running 执行；
        8.3：等待/终态任务不执行；13.3 同 task 不并发由锁保证，状态兜底）。"""
        from administrative_agent.tasks.machine import TaskStatus
        from administrative_agent.tasks.service import TaskNotRunningError

        task = self._tasks.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            raise TaskNotRunningError(task_id, task.status)

    def _fail_task(self, task_id: str, error_code: str, error_summary: str) -> None:
        """任务标记 failed（8.1/13.2：需人工处理或重新发起；不假报成功）。"""
        try:
            self._tasks.mark_failed(
                task_id, error_code=error_code, error_summary=error_summary
            )
        except Exception:
            # 任务已终态（如确认流程异常取消）时忽略；审计失败路径由
            # 调用方另行处理（与 attendance 执行器同口径）
            logger.exception("metal_order_fail_task_error", task_id=task_id)


# ---------------------------------------------------------------------------
# D16（RV-23/RV-24）：R3 订单更新（5.4 八条 / 9.4 / 14.3 场景 7/8/10）
# ---------------------------------------------------------------------------

#: 操作类型（10.2：operation 来自代码枚举；R3 更新以 order_type 区分目标）
OP_UPDATE_METAL_ORDER = "更新订单（钣金）"
OP_UPDATE_POWDER_ORDER = "更新订单（塑粉）"

_UPDATE_OP_TYPES = {
    "metal": OP_UPDATE_METAL_ORDER,
    "powder": OP_UPDATE_POWDER_ORDER,
}

#: 任务终态错误码（5.4 规则 6/7 / 9.4 / 14.3 场景 10）
ERROR_UPDATE_NOT_FOUND = "ORDER_UPDATE_NOT_FOUND"
ERROR_UPDATE_NO_CHANGES = "ORDER_UPDATE_NO_CHANGES"
ERROR_UPDATE_CONFIRMATION_INVALID = "ORDER_UPDATE_CONFIRMATION_INVALID"
ERROR_UPDATE_VERSION_CONFLICT = "ORDER_UPDATE_VERSION_CONFLICT"
ERROR_UPDATE_NETWORK_UNKNOWN = "ORDER_UPDATE_NETWORK_UNKNOWN"
ERROR_UPDATE_MCP_FAILURE = "ORDER_UPDATE_MCP_FAILURE"
ERROR_UPDATE_PRECONDITION = "ORDER_UPDATE_PRECONDITION"

#: 写前查询字段（R4：显式字段；白名单字段 + id/updated_at 用于 changes
#: 比较与 expected_updated_at 回传，mcp-api.md §7.1；total_amount 为只读
#: 派生金额，快照携带供前后快照差额比较，mcp-api.md §7.2 L356）
_UPDATE_QUERY_FIELDS = {
    "metal": frozenset(
        UPDATE_WHITELIST_METAL | {"id", "updated_at", "total_amount"}
    ),
    "powder": frozenset(
        UPDATE_WHITELIST_POWDER | {"id", "updated_at", "total_amount"}
    ),
}

#: 展示字段（确认卡片目标标识：钣金型号 / 塑粉色号）
_UPDATE_KEY_FIELDS = {"metal": "model", "powder": "color_code"}

#: 目标标识的关联方名称候选键（写前快照 before 里若带名称字段则一并展示；
#: 生产 ERP metal_orders/powder_orders 无名称列（mcp-api.md §6.1 只有
#: customer_id/supplier_id），故通常取不到 → 优雅降级为仅型号+入库日期）。
_UPDATE_NAME_FIELDS = {"metal": "customer_name", "powder": "supplier_name"}

#: [2026-09-10 P2-3] 关联方名称反查表（仅展示层）：order_type →
#: (外键字段, ERP 实体, 名称字段)。生产订单表只有 customer_id/supplier_id，
#: 名称需按 id 反查（metal 用 customers.company_name、powder 用
#: suppliers.name），与 domains/finance.resolve_party_display 同口径。
_UPDATE_PARTY_LOOKUP: dict[str, tuple[str, str, str]] = {
    "metal": ("customer_id", "customers", "company_name"),
    "powder": ("supplier_id", "suppliers", "name"),
}


def _target_part(value: object) -> str | None:
    """目标标识值归一化：非空（去首尾空白）字符串原样，其余 → None。

    绝不抛异常（[2026-09-10 P1-2] 缺失项优雅降级）。
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve_update_party_name(
    client: MCPClient | None, order_type: str, before: dict[str, Any]
) -> str | None:
    """[2026-09-10 P2-3] 按写前快照里的 customer_id/supplier_id 反查名称。

    仅展示层使用：查不到（无 id / 无行 / 名称为空）或查询异常一律返回 None
    （调用方省略该项，降级为「型号 / 入库日期」）——**绝不抛异常、绝不回退
    成显示 customer_id/supplier_id**（内部主键不给用户看）。名称不进
    hash_target，故反查失败不影响确认有效性（执行期不误拒）。
    """
    if client is None:
        return None
    fk, entity, name_field = _UPDATE_PARTY_LOOKUP[order_type]
    raw_id = before.get(fk)
    if raw_id is None:
        return None
    try:
        result = client.call_tool(
            "query",
            {
                "entity": entity,
                "fields": ["id", name_field],
                "filter": {"id": raw_id},
                "limit": 1,
            },
        )
    except Exception:  # noqa: BLE001 —— 展示层兜底：解析失败绝不阻断建卡
        return None
    if not result.ok:
        return None
    items = list(result.data.get("items") or []) if isinstance(result.data, dict) else []
    if not items:
        return None
    value = items[0].get(name_field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_update_target(
    order_type: str, before: dict[str, Any], party_name: str | None = None
) -> str:
    """改单卡目标：可判别的复合业务键（[2026-09-10 P1-2]）。

    由 客户/供货商名称 + 型号/色号 + 入库日期 组成（如
    ``订单（昌盛 / MK-101 / 2026-09-05）``）——不写 ERP 内部主键 order_id，
    但足以让用户分辨同客户同型号的不同订单。

    [2026-09-10 P2-3] 名称优先取调用方反查结果 party_name（生产订单表无
    名称列，须按 id 反查），无则回退写前快照 before 里的名称字段；仍取不到
    该项省略。型号/色号、入库日期取自 before，缺哪项略哪项，全缺时退为
    「订单」；取不到名称时也**不**回退成显示 customer_id/supplier_id。
    """
    parts: list[str] = []
    name = _target_part(party_name) or _target_part(
        before.get(_UPDATE_NAME_FIELDS[order_type])
    )
    if name is not None:
        parts.append(name)
    key = _target_part(before.get(_UPDATE_KEY_FIELDS[order_type]))
    if key is not None:
        parts.append(key)
    in_date = _target_part(before.get("in_date"))
    if in_date is not None:
        parts.append(in_date)
    return f"订单（{' / '.join(parts)}）" if parts else "订单"


def _comparable(value: Any) -> tuple[str, Any]:
    """值归一化（更新差异比较）：ERP 数值字符串与 Python 数值按数值比较。

    "2.7500" == 2.75；日期/枚举按去除首尾空格后的字符串比较；
    null 与缺失键同视为 ("none", None)。
    """
    if value is None:
        return ("none", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        try:
            return ("num", Decimal(value))
        except (InvalidOperation, ValueError):
            return ("str", value.strip())
    if isinstance(value, (int, float)):
        return ("num", Decimal(str(value)))
    return ("str", str(value))


def build_update_changes(
    current: dict[str, Any],
    desired: dict[str, Any],
    order_type: str,
) -> dict[str, Any]:
    """5.4 规则 2 / 9.4：只生成真正变化的字段（写前快照 → changes）。

    - current：写前查询结果（ERP 数值为字符串，如 "2.7500"）；
    - desired：目标期望值（用户意图/复核结果）；
    - 数值字段按数值归一化比较；值相同（含同为 null）→ 不进 changes；
    - 白名单与禁止字段校验由 scopes.verify_update_order_arguments 兜底
      （fail closed，mcp-api.md §7.1）。
    """
    changes: dict[str, Any] = {}
    for field, want in desired.items():
        if _comparable(current.get(field)) != _comparable(want):
            changes[field] = want
    return changes


def precheck_update_inputs(
    changes: dict[str, Any], current: dict[str, Any], order_type: str
) -> None:
    """更新输入预检（L775：Agent 只做输入预检，不复制 ERP 计算）。

    - status 只允许 未完成/完成（mcp-api.md §7.1 枚举）；
    - 已完成订单只改 out_date 且传 null → 拒绝（§7.2 联动会返回
      VALIDATION_ERROR；本地提前拦，避免一次失败调用）；
    - 塑粉 total_weight 更新必须大于 0（R3.order.update.powder 规则 2）；
    - 钣金 surcharge_amount（附加款，[2026-09-08 ERP 上线]）更新必须非负
      且 ≤2 位小数（decimal(10,2) 列契约，surcharge_errors 同 create 规则层）。

    违反抛 MCPError(VALIDATION_ERROR)（fail closed；执行器转为预检
    错误并 fail task）。
    """
    status = changes.get("status")
    if status is not None and status not in ("未完成", "完成"):
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="status must be '未完成' or '完成'.",
            details={"status": status},
        )
    if (
        "out_date" in changes
        and changes["out_date"] is None
        and current.get("status") == "完成"
        and "status" not in changes
    ):
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="out_date cannot be null on a completed order.",
            details={"order_status": current.get("status")},
        )
    if order_type == "powder" and "total_weight" in changes:
        try:
            weight = float(Decimal(str(changes["total_weight"])))
        except (InvalidOperation, ValueError, TypeError):
            weight = 0.0
        if weight <= 0:
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.VALIDATION,
                message="powder total_weight must be greater than 0.",
                details={"total_weight": changes["total_weight"]},
            )
    if order_type == "metal" and "surcharge_amount" in changes:
        # 附加款只允许非负、最多 2 位小数（与 create 规则层同口径；
        # 白名单/类型层由 scopes 与工具 args 兜底，此处只做数值预检）
        surcharge_issue = surcharge_errors(changes["surcharge_amount"])
        if surcharge_issue:
            _code, message = surcharge_issue
            raise MCPError(
                code=InternalCode.VALIDATION_ERROR.value,
                category=ErrorCategory.VALIDATION,
                message=f"surcharge_amount invalid: {message}",
                details={"surcharge_amount": changes["surcharge_amount"]},
            )


@dataclass(frozen=True)
class UpdatePlan:
    """一条更新明细的确定性计划（[2026-09-12 一张卡多条明细]）。

    `plan_update_item` 对同一份「写前快照 + 期望值」**必产出同一结果**
    ——prepare 阶段（确认卡）与执行入口（重建整卡比对）共用此纯函数，
    保证「批准的卡片」=「执行的载荷」（7.3）。

    - action：ACTION_UPDATE（有真实变化）/ ACTION_UNCHANGED（期望值已被满足，
      执行期零写调用）；
    - changes：只含真正变化字段（build_update_changes 口径）；
    - expected_updated_at：乐观锁参数（取自写前快照 updated_at）。
    """

    order_type: str
    order_id: int
    desired: dict[str, Any]
    before: dict[str, Any]
    expected_updated_at: str
    changes: dict[str, Any]
    action: str


def plan_update_item(
    *,
    order_type: str,
    order_id: int,
    desired: dict[str, Any],
    before: dict[str, Any],
) -> UpdatePlan:
    """写前快照 + 期望值 → 确定性更新计划（prepare 与执行入口共用的纯函数）。

    - changes 为空 → action=ACTION_UNCHANGED（**不跑输入预检**，与单条路径
      「无变化直接 NO_CHANGES」同口径；批量卡上展示为「保持」、零写调用）；
    - changes 非空 → precheck_update_inputs + verify_update_order_arguments
      （白名单/禁止字段，fail closed），任一不过抛 MCPError。

    本函数纯 CPU/dict（**不触 IO**）：写前快照由调用方查询后传入，故同输入
    必得同输出；prepare 与该条执行入口都调它，两处 changes/expected_updated_at
    天然一致（whole-card 比对不失稳）。
    """
    expected_updated_at = str(before.get("updated_at") or "")
    changes = build_update_changes(before, desired, order_type)
    if changes:
        precheck_update_inputs(changes, before, order_type)
        verify_update_order_arguments(
            {
                "order_type": order_type,
                "order_id": order_id,
                "changes": changes,
                "expected_updated_at": expected_updated_at,
            }
        )
    return UpdatePlan(
        order_type=order_type,
        order_id=order_id,
        desired=dict(desired),
        before=dict(before),
        expected_updated_at=expected_updated_at,
        changes=dict(changes),
        action=ACTION_UPDATE if changes else ACTION_UNCHANGED,
    )


@dataclass(frozen=True)
class OrderUpdateBatchItem:
    """[2026-09-12 一张卡多条明细] 批量更新的一条明细（prepare/执行共用）。

    - index/item_id/label/action：台账与展示（同 OrderBatchItem 语义）；
    - order_id：目标订单（不进展示，进 hash raw 载荷绑定身份）；
    - desired：期望变化字段（执行入口据此重查重建整卡）；
    - before/changes/expected_updated_at：prepare 阶段由 `plan_update_item`
      产出（执行入口对输入只要求 order_id/desired，这些字段在执行入口经
      重查后由 `plan_update_item` 重新计算）。
    """

    index: int
    item_id: str
    label: str
    action: str
    order_id: int
    desired: dict[str, Any]
    before: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)
    expected_updated_at: str = ""


def build_update_batch_confirmation_card(
    *, order_type: str, items: Sequence[OrderUpdateBatchItem]
) -> ConfirmationCard:
    """R3 批量更新确认卡（[2026-09-12 一张卡多条明细]；与单条卡同哈希口径）。

    - raw（hash_fields）外层键用位置序号 ``detail-{i}``（不含业务标签）：行内
      entry = ``{"action": ..., "fields": changes + expected_updated_at + id}``
      ——order_id 只进哈希不进展示（单条卡同口径：身份绑定，防同型号同日期
      两卡碰撞）；
    - 展示外层键 ``f"{i}. {业务标签}"``，entry 的 fields **去掉 id**（用户
      看不到内部主键）、经 localize_confirmation_fields 中文化；
    - unchanged 明细 entry.fields 为空——留在卡上展示「保持」、执行期零写；
    - hash_target 取稳定串（批量：不含任何关联方名称）——名称执行期重查
      可能改名/缺失，绝不进 hash（见 build_update_confirmation_card 教训）。
    """
    raw_fields: list[tuple[str, object]] = []
    display_fields: list[tuple[str, object]] = []
    for it in items:
        if it.action == ACTION_UNCHANGED:
            entry = {"action": ACTION_UNCHANGED, "fields": {}}
            display_entry = {"action": ACTION_UNCHANGED, "fields": {}}
        else:
            body = dict(it.changes)
            body["expected_updated_at"] = it.expected_updated_at
            entry = {"action": ACTION_UPDATE, "fields": {**body, "id": it.order_id}}
            display_entry = {"action": ACTION_UPDATE, "fields": body}
        raw_fields.append((f"{BATCH_HASH_PREFIX}{it.index}", entry))
        display_fields.append((f"{it.index}. {it.label}", display_entry))
    card = ConfirmationCard(
        operation_type=_UPDATE_OP_TYPES[order_type],
        target="",
        fields_to_write=localize_confirmation_fields(tuple(display_fields)),
        hash_fields=tuple(raw_fields),
        hash_target=f"批量更新订单（{len(items)} 条）",
        auto_filled=(),
        expected_impact=(
            "在 ERP 批量更新订单并同事务重算金额（钣金 quantity×weight_per_unit"
            "×unit_price＋附加款 surcharge_amount / 塑粉 total_weight×price_per_kg，"
            "mcp-api.md §7.2）；保存各自更新前快照与更新后订单（14.3 场景 7）；"
            "标记为「保持」的明细本次不写入；不创建 ar_payments/ap_payments/"
            "transactions。"
        ),
        limitations=(
            "状态/交期日期联动规则由 ERP 执行（mcp-api.md §7.2），本侧仅做输入预检；",
            "任一明细的写前快照在确认后发生变化，**整卡确认作废**、整批不写入，"
            "需重新查询并重新确认（7.3 / 口径 7.3）；",
            "版本冲突或结果未知时该条不盲目重试，按恢复流程处理（5.4 规则 6/7）。",
        ),
        payload_hash="",
        row_header_label="明细",
        row_quantifier="条",
    )
    return replace(card, payload_hash=card_payload_hash(card))


def build_update_confirmation_card(
    *,
    order_type: str,
    order_id: int,
    before: dict[str, Any],
    changes: dict[str, Any],
    expected_updated_at: str,
    client: MCPClient | None = None,
) -> ConfirmationCard:
    """R3 更新轻确认卡片（7.3 六要素；D13 卡片模式复用）。

    - 目标：可判别的复合业务键（客户/供货商名称 / 型号/色号 / 入库日期）——
      不写订单 id（[2026-09-10 展示口径/P1-2] ERP 内部主键，用户看不到记不住；
      名称取不到时也不回退成 id）；
    - client：[2026-09-10 P2-3] 传入时按 before 里 customer_id/supplier_id
      反查名称补进展示目标（生产订单表无名称列）；反查失败/未传则降级为
      「型号 / 入库日期」，**不影响 hash**（名称不进 hash_target）；
    - 写值：changes + expected_updated_at（乐观锁参数；变化即确认失效）；
    - 要素 5 预计影响：ERP 同事务重算金额（§7.2），保存前后快照；
    - 要素 6 限制：状态/日期联动由 ERP 执行；other_effects 非差额；
      版本冲突需重新确认（5.4 规则 6）。
    """
    # [2026-09-10 展示口径/P1-2] 目标不写订单 id（ERP 内部主键），改以复合
    # 业务键定位（客户/供货商名称 + 型号/色号 + 入库日期）；缺项优雅降级、
    # 全缺退为「订单」（order_id 仍参与执行与审计，只是不进展示）。
    # [2026-09-10 P2-3] 名称按 customer_id/supplier_id 反查（仅展示，见 helper）。
    party_name = _resolve_update_party_name(client, order_type, before)
    target = _build_update_target(order_type, before, party_name=party_name)
    # [2026-09-10 修复] target 里的关联方名称来自写前快照 before（[P1-2]
    # _UPDATE_NAME_FIELDS）——生产 ERP 订单表无名称列（mcp-api.md §6.1）
    # 通常取不到，但一旦取到、且执行期重查时客户/供货商改名/缺失，展示目标
    # 会抖动 → 若直接进 hash 会误判「确认失效」。故 hash 用**不含名称**的
    # 稳定目标串（型号/色号 + 入库日期；身份由 hash_fields 里的 order_id
    # 承担），展示 target 保持带名称形态。
    _hash_before = {
        k: v for k, v in before.items()
        if k != _UPDATE_NAME_FIELDS[order_type]
    }
    hash_target = _build_update_target(order_type, _hash_before)
    # hash_fields 取原始执行载荷（changes + 乐观锁时间戳）并**恢复身份绑定**：
    # 追加 order_id——本批把 id 移出 target 后哈希不再覆盖订单身份，同型号同
    # in_date 的两张卡会碰撞（reviewer P1-2 实测 0bf1d6d9a518e9c8）。order_id
    # 只进哈希不进展示。
    hash_fields = tuple(changes.items()) + (
        ("expected_updated_at", expected_updated_at),
        ("id", order_id),
    )
    card = ConfirmationCard(
        operation_type=_UPDATE_OP_TYPES[order_type],
        target=target,
        fields_to_write=localize_confirmation_fields(
            tuple(changes.items())
            + (("expected_updated_at", expected_updated_at),)
        ),
        hash_fields=hash_fields,
        hash_target=hash_target,
        auto_filled=(),
        expected_impact=(
            "在 ERP 更新该订单并同事务重算金额"
            "（钣金 quantity×weight_per_unit×unit_price＋附加款"
            " surcharge_amount / 塑粉 total_weight×price_per_kg，"
            "mcp-api.md §7.2）；"
            "保存更新前快照与更新后订单（14.3 场景 7）；"
            "不创建 ar_payments/ap_payments/transactions。"
        ),
        limitations=(
            "状态/交期日期联动规则由 ERP 执行（mcp-api.md §7.2），"
            "本侧仅做输入预检；",
            "other_effects 为提交后订单的当前派生影响而非差额，"
            "数学差额用更新前快照与更新后订单自行比较；",
            "版本冲突或结果未知时需重新查询并重新确认（5.4 规则 6/7）。",
        ),
        payload_hash="",
    )
    return replace(card, payload_hash=card_payload_hash(card))


@dataclass(frozen=True)
class UpdateExecutionSummary:
    """update_order 执行汇总（5.4 八条 / 14.3 场景 7/8/10）。

    - before_snapshot：写前查询快照（场景 7：保存前后快照）；
    - after_snapshot：ERP 响应订单（成功时）；
    - changed_fields/other_effects：ERP 响应（mcp-api.md §7.4）；
    - refetched_changes：版本冲突/结果未知后重新查询重新生成的差异
      （规则 6/7；内容变化 → 需重新确认，不覆盖新数据）；
    - attempts：本次执行的 update_order 实际调用次数（规则 6/7：
      冲突与结果未知均不盲目重复调用）。
    """

    task_id: str
    operation_type: str
    order_type: str
    order_id: int | None
    expected_updated_at: str
    changes: dict[str, Any]
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any] | None
    changed_fields: tuple[str, ...]
    other_effects: tuple[dict[str, Any], ...]
    result_status: str
    attempts: int
    verification_passed: bool
    error_code: str | None = None
    error_summary: str | None = None
    refetched_changes: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "order_type": self.order_type,
            "order_id": self.order_id,
            "expected_updated_at": self.expected_updated_at,
            "changes": dict(self.changes),
            "before_snapshot": dict(self.before_snapshot or {}),
            "after_snapshot": dict(self.after_snapshot or {}),
            "changed_fields": list(self.changed_fields),
            "other_effects": [dict(e) for e in self.other_effects],
            "result_status": self.result_status,
            "attempts": self.attempts,
            "verification_passed": self.verification_passed,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "refetched_changes": (
                dict(self.refetched_changes) if self.refetched_changes is not None else None
            ),
        }


class OrderUpdateExecutor:
    """R3 订单更新执行（5.4 八条 / 9.4 / 14.3 场景 7/8/10）。

    与创建执行器（D14）的本质区别（mcp-api.md §7.2 L361）：
    - 无创建型幂等键：写前查询 + expected_updated_at 乐观锁（规则 1/4）；
    - 版本冲突 / 网络结果未知 → 重新 query 重新生成差异（规则 6/7），
      内容变化则需重新确认，绝不盲目重复调用 update_order。

    流程（9.4 L1045-1056 落地）：
    1. _require_running（8.1/8.3 同 D14；13.3 同 task 不并发由锁保证）；
    2. 写前查询唯一目标订单（query 只读；updated_at 原样回传，规则 4）；
    3. 写前快照 = 查询结果（before_snapshot，14.3 场景 7）；
    4. changes = build_update_changes（规则 2：只含真正变化字段）；
    5. precheck_update_inputs（状态/out_date 联动输入预检，L775）；
    6. verify_update_order_arguments（白名单+禁止字段，fail closed §7.1）；
    7. 确认有效性：以执行时快照重建卡片哈希，与任务 confirmed_payload_hash
       比对（7.3：快照变化 → 哈希变化 → 确认失效，拒绝执行需重新确认）；
    8. attempt 记录目标/字段差异/expected_updated_at（11.x L1225）；
    9. 调用 update_order（operation_id 链路关联；规则 5 金额重算同事务）；
    10. 成功 → after_snapshot + changed_fields + other_effects（§7.4）；
    11. ORDER_VERSION_CONFLICT → 重新 query → 重新生成差异（规则 6）：
        差异为空（他人已应用同样修改）→ 展示最新快照，不覆盖不重复调用；
        内容变化 → failed 需重新确认（14.3 场景 10：不覆盖新数据）；
    12. 结果未知（NETWORK/SERVER）→ 先查询最新订单（规则 7）：
        已应用期望变更 → 展示；未应用 → failed 需重新确认；
        查询亦失败 → failed 无法确认（恢复流程沿用 D12/D14 已批准语义）。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: PolicyInfo | None = None,
        duty_code: str = "R3",
    ) -> None:
        self._client = client
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy
        self._duty_code = duty_code

    # ---- 入口（9.4 流程） ----

    def execute(
        self,
        *,
        task_id: str,
        order_type: str,
        order_id: int,
        desired: dict[str, Any],
    ) -> UpdateExecutionSummary:
        """确认后执行订单更新（desired = 目标期望字段值）。

        order_type 只接受 "metal"/"powder"（10.2：代码枚举，不接受外部
        字符串指定）；任务终态为 succeeded 或 failed，不做部分成功。

        [2026-09-12 一张卡多条明细] 单条路径（含在飞旧卡：execution 无
        items 键）语义逐字节不变——写前查询/差异/预检/确认校验保留在本
        方法，调用阶段（attempt+update_order+分支）抽到 `_execute_one`；
        任务级终态（mark_succeeded/_fail_task）保留在本方法（批量路径改由
        BatchExecutor 聚合）。
        """
        if order_type not in _UPDATE_OP_TYPES:
            raise ValueError(f"unknown order_type: {order_type}")
        self._require_running(task_id)

        # 规则 1/4：写前查询唯一目标订单（R4 query：只读、显式字段）
        try:
            before, expected_updated_at = self._query_before(order_type, order_id)
        except MCPError as exc:
            summary = (
                f"写前查询失败（{exc.code}）：{exc.message}；"
                "未执行更新，需核查后重试（5.4 规则 1）"
            )
            self._fail_task(task_id, ERROR_UPDATE_MCP_FAILURE, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes={}, expected_updated_at="", error_code=ERROR_UPDATE_MCP_FAILURE,
            )
        if before is None:
            summary = (
                f"未查询到目标订单（order_type={order_type}, order_id={order_id}）："
                "不更新、不猜测（5.4 规则 1）"
            )
            self._fail_task(task_id, ERROR_UPDATE_NOT_FOUND, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes={}, expected_updated_at="", error_code=ERROR_UPDATE_NOT_FOUND,
            )

        # 规则 2 + 输入预检（plan_update_item：差异 + precheck + 白名单兜底）：
        # 与批量路径共用同一纯函数，保证「同快照同期望必得同 changes」。
        try:
            plan = plan_update_item(
                order_type=order_type, order_id=order_id,
                desired=desired, before=before,
            )
        except MCPError as exc:
            summary = f"更新输入预检未通过：{exc}"
            self._fail_task(task_id, ERROR_UPDATE_PRECONDITION, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes=build_update_changes(before, desired, order_type),
                expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_UPDATE_PRECONDITION,
            )
        changes = plan.changes
        if not changes:
            summary = (
                f"目标订单（id={order_id}）期望值与当前值一致，无需更新："
                "不调用 update_order（5.4 规则 2）"
            )
            self._fail_task(task_id, ERROR_UPDATE_NO_CHANGES, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_UPDATE_NO_CHANGES,
            )

        # 7.3：执行内容与确认时一致才继续（确认失效必须重新确认）
        task = self._tasks.get_task(task_id)
        confirmed_hash = task.confirmed_payload_hash if task else None
        if not confirmed_hash:
            summary = "该更新任务未经过轻确认（confirmed_payload_hash 为空）：拒绝执行（7.3）"
            self._fail_task(task_id, ERROR_UPDATE_CONFIRMATION_INVALID, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
            )
        card = build_update_confirmation_card(
            order_type=order_type, order_id=order_id, before=before,
            changes=changes, expected_updated_at=expected_updated_at,
            # [2026-09-10 P2-3] 执行期同样反查名称（展示）；反查失败只影响
            # 展示，名称不进 hash_target → 确认仍有效、不误拒执行。
            client=self._client,
        )
        if not is_confirmation_valid(card, confirmed_hash):
            summary = (
                "执行时订单快照与确认时不一致，确认已失效："
                "需重新查询并重新确认（7.3），不执行更新"
            )
            self._fail_task(task_id, ERROR_UPDATE_CONFIRMATION_INVALID, summary)
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
            )

        # 调用阶段（attempt + update_order + 分支）——任务终态由本方法聚合
        op_id = task.operation_id
        summary = self._execute_one(
            task_id=task_id, op_id=op_id, order_type=order_type,
            order_id=order_id, desired=desired, before=before,
            changes=changes, expected_updated_at=expected_updated_at,
        )
        if summary.result_status == "success":
            self._tasks.mark_succeeded(task_id, verification_passed=True)
        else:
            self._fail_task(
                task_id,
                summary.error_code or ERROR_UPDATE_MCP_FAILURE,
                summary.error_summary or "订单更新失败",
            )
        return summary

    def _execute_one(
        self,
        *,
        task_id: str,
        op_id: str,
        order_type: str,
        order_id: int,
        desired: dict[str, Any],
        before: dict[str, Any],
        changes: dict[str, Any],
        expected_updated_at: str,
    ) -> UpdateExecutionSummary:
        """单条调用阶段（旧单条路径与批量路径共用；**不做任务终态标记**）。

        保留全部既有分支：attempt 记录、update_order 调用、成功/版本冲突
        （ORDER_VERSION_CONFLICT，重查不盲目重试）/结果未知/其他错误。任务
        级终态由调用方聚合（单条 execute() 标记；批量 BatchExecutor 合成）。
        """
        # 11.x L1225：调用前 attempt 记录目标/字段差异/expected_updated_at
        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type=f"{order_type}_order",
            target_id=str(order_id),
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            expected_updated_at=expected_updated_at,
            field_diffs=changes,
        )
        result = self._client.call_tool(
            "update_order",
            {
                "order_type": order_type,
                "order_id": order_id,
                "changes": changes,
                "expected_updated_at": expected_updated_at,
            },
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(
            result, task_id, op_id,
            target_type=f"{order_type}_order", target_id=str(order_id),
        )
        if result.ok:
            return self._handle_success(
                task_id, op_id, attempt, result.data, order_type, order_id,
                changes, expected_updated_at, before,
            )

        err = result.error
        if err.code == "ORDER_VERSION_CONFLICT":
            # 11.2 规则 2：每次实际调用只追加一条 result——冲突分支内记录
            # result_status=conflict（D14 创建执行器同型，各分支单次记录）
            return self._handle_version_conflict(
                task_id, op_id, attempt, err, order_type, order_id, desired,
                changes, expected_updated_at, before,
            )
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            return self._handle_network_unknown(
                task_id, op_id, attempt, err, order_type, order_id, desired,
                changes, expected_updated_at, before,
            )
        self._record_result(attempt, "failed", err)
        summary = f"update_order 失败（{err.code}）：{err.message}"
        return self._summary(
            task_id, order_type, order_id, "failed", summary,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, error_code=ERROR_UPDATE_MCP_FAILURE,
        )

    # ---- 批量入口（[2026-09-12 一张卡多条明细]） ----

    def execute_batch(
        self,
        *,
        task_id: str,
        order_type: str,
        items: Sequence[OrderUpdateBatchItem],
        confirmed_hash: str,
    ) -> "BatchSummary":
        """一张卡多条明细的批量更新执行（本阶段最高风险路径；口径 7.3）。

        执行入口**先逐条重查快照并重建整卡、与 confirmed_hash 整卡比对**：

        - 任一条重查失败/未找到/预检不过，或重建整卡哈希与确认时不一致 →
          **整批零写入** + mark_failed(ORDER_UPDATE_CONFIRMATION_INVALID)
          （7.3：绝不部分执行一张已失效的确认卡）；
        - 比对通过 → 逐条执行（unchanged 明细零写调用），任务终态由
          BatchExecutor 聚合（全成功→succeeded；部分失败未达上限→保持
          running + awaiting_recovery；达上限→failed(RETRY_LIMIT_REACHED)）。

        **严禁在整卡比对通过后退化为逐条比对**（否则卡片部分失效会被静默
        放过——这是本阶段正确性的基石）。
        """
        from administrative_agent.domains.batch import (
            BatchExecutor,
            BatchItem,
            BatchItemResult,
            BatchSummary,
        )

        if order_type not in _UPDATE_OP_TYPES:
            raise ValueError(f"unknown order_type: {order_type}")
        self._require_running(task_id)

        # 1. 逐条重查快照并重建计划（plan_update_item 纯函数 → 同快照同结果）
        rebuilt: list[OrderUpdateBatchItem] = []
        for it in items:
            try:
                before, _ = self._query_before(order_type, it.order_id)
            except MCPError as exc:
                return self._invalid_whole_batch(
                    task_id, items, order_type,
                    f"明细 {it.label} 写前查询失败（{exc.code}）",
                )
            if before is None:
                return self._invalid_whole_batch(
                    task_id, items, order_type,
                    f"明细 {it.label} 未查询到目标订单（可能已被删除）",
                )
            try:
                plan = plan_update_item(
                    order_type=order_type, order_id=it.order_id,
                    desired=it.desired, before=before,
                )
            except MCPError as exc:
                return self._invalid_whole_batch(
                    task_id, items, order_type,
                    f"明细 {it.label} 更新输入预检未通过：{exc}",
                )
            rebuilt.append(
                replace(
                    it, action=plan.action, before=before,
                    changes=plan.changes,
                    expected_updated_at=plan.expected_updated_at,
                )
            )

        # 2. 整卡重建 + **整卡**比对（不是逐条比对）
        card = build_update_batch_confirmation_card(
            order_type=order_type, items=rebuilt
        )
        if card_payload_hash(card) != confirmed_hash:
            return self._invalid_whole_batch(
                task_id, items, order_type,
                "整卡内容（含各条写前快照/期望值/乐观锁时间戳）与确认时不一致，确认已失效",
            )

        # 3. 逐条执行（unchanged 由 BatchExecutor 零写跳过）
        by_item_id = {it.item_id: it for it in rebuilt}
        batch_executor = BatchExecutor(
            tasks=self._tasks, audit=self._audit, tool_key="order_update"
        )

        def runner(batch_item: BatchItem, op_id: str) -> BatchItemResult:
            it = by_item_id[batch_item.item_id]
            summary = self._execute_one(
                task_id=task_id, op_id=op_id, order_type=order_type,
                order_id=it.order_id, desired=it.desired, before=it.before,
                changes=it.changes, expected_updated_at=it.expected_updated_at,
            )
            if summary.result_status == "success":
                return BatchItemResult(
                    index=batch_item.index,
                    item_id=batch_item.item_id,
                    label=batch_item.label,
                    final_action=ACTION_UPDATE,
                    status="success",
                    note=(
                        f"更新成功（order_id={it.order_id}，"
                        f"changed_fields={list(summary.changed_fields)}）"
                    ),
                )
            return BatchItemResult(
                index=batch_item.index,
                item_id=batch_item.item_id,
                label=batch_item.label,
                final_action="failed",
                status="failed",
                error_code=summary.error_code or ERROR_UPDATE_MCP_FAILURE,
                error_summary=summary.error_summary,
                note="单条失败不重放批次；由任务恢复机制处理（13.2）",
            )

        batch_items = [
            BatchItem(
                index=it.index, item_id=it.item_id,
                action=it.action, label=it.label,
            )
            for it in rebuilt
        ]
        return batch_executor.run(task_id=task_id, items=batch_items, runner=runner)

    def _invalid_whole_batch(
        self,
        task_id: str,
        items: Sequence[OrderUpdateBatchItem],
        order_type: str,
        reason: str,
    ) -> "BatchSummary":
        """整卡确认失效：**零写入** + 任务 failed（7.3 / 口径 7.3）。

        所有明细统一标记 failed(ORDER_UPDATE_CONFIRMATION_INVALID)，不调用
        任何写工具；任务终态 failed（需人工处理/重新发起）。
        """
        from administrative_agent.domains.batch import BatchItemResult, BatchSummary

        summary_text = (
            f"批量更新整卡确认失效：{reason}；"
            "未执行任何写入，需重新查询订单后重新发起并确认（7.3）"
        )
        self._fail_task(task_id, ERROR_UPDATE_CONFIRMATION_INVALID, summary_text)
        results = tuple(
            BatchItemResult(
                index=it.index,
                item_id=it.item_id,
                label=it.label,
                final_action="failed",
                status="failed",
                error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
                error_summary=summary_text,
                note="整卡失效：本批零写入",
            )
            for it in items
        )
        return BatchSummary(
            task_id=task_id,
            counts={
                "created": 0, "updated": 0, "unchanged": 0,
                "skipped": 0, "failed": len(results),
            },
            item_results=results,
            verification_passed=False,
            error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
            error_summary=summary_text,
        )

    # ---- 成功分支（§7.4 响应 + 前后快照，14.3 场景 7/8） ----

    def _handle_success(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        data: dict[str, Any],
        order_type: str,
        order_id: int,
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any],
    ) -> UpdateExecutionSummary:
        """成功：after_snapshot = ERP 响应订单（规则 5 金额重算同事务已由
        ERP 完成）；保存 changed_fields/other_effects（§7.4）。"""
        order = data.get("order") or {}
        changed_fields = tuple(data.get("changed_fields") or [])
        other_effects = tuple(data.get("other_effects") or [])
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            result_summary=f"updated {order_type} order {order_id}",
            structured_payload={
                "order_id": order_id,
                "order_type": order_type,
                "expected_updated_at": expected_updated_at,
                "changes": dict(changes),
                "changed_fields": list(changed_fields),
                "other_effects": [dict(e) for e in other_effects],
                "before_snapshot": dict(before),
                "after_snapshot": dict(order),
            },
        )
        # [2026-09-12 一张卡多条明细] 任务终态不再在此标记（单条由 execute()
        # 标记；批量由 BatchExecutor 聚合——逐条成功不改变任务终态）
        logger.info(
            "order_updated",
            task_id=task_id,
            order_id=order_id,
            order_type=order_type,
            changed_fields=list(changed_fields),
        )
        return self._summary(
            task_id, order_type, order_id, "success", None,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, after=dict(order),
            changed_fields=changed_fields, other_effects=other_effects,
        )

    # ---- 版本冲突（规则 6 / 14.3 场景 10） ----

    def _handle_version_conflict(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        err: MCPError,
        order_type: str,
        order_id: int,
        desired: dict[str, Any],
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any],
    ) -> UpdateExecutionSummary:
        """规则 6：版本冲突 → 重新 query → 重新生成差异。

        - 差异为空（他人已把订单更新为期望值）→ 展示最新快照，
          不覆盖、不重复调用 update_order；
        - 内容变化 → failed（ERROR_UPDATE_VERSION_CONFLICT）需重新确认，
          不覆盖新数据（14.3 场景 10）。
        """
        self._record_result(attempt, "conflict", err)
        fresh, _ = self._query_before(order_type, order_id)
        if fresh is None:
            fresh = {}
        fresh_changes = build_update_changes(fresh, desired, order_type)
        if not fresh_changes:
            summary = (
                f"目标订单已被他人更新为期望值（版本冲突）："
                "按最新快照展示当前状态，不重复调用 update_order（5.4 规则 6）"
            )
            # [2026-09-12 一张卡多条明细] 任务终态不再在此标记（单条由
            # execute() 依 result_status=success 标记；批量由 BatchExecutor 聚合）
            return self._summary(
                task_id, order_type, order_id, "success", None,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, after=dict(fresh),
                refetched_changes=fresh_changes,
            )
        summary = (
            f"订单版本冲突（expected_updated_at={expected_updated_at}）："
            f"已重新查询并重新生成差异 {fresh_changes}；"
            "内容变化，需重新确认后执行，不覆盖新数据（5.4 规则 6 / 14.3 场景 10）"
        )
        return self._summary(
            task_id, order_type, order_id, "conflict", summary,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, error_code=ERROR_UPDATE_VERSION_CONFLICT,
            refetched_changes=fresh_changes,
        )

    # ---- 结果未知（规则 7：先查询，不盲目重试） ----

    def _handle_network_unknown(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        err: MCPError,
        order_type: str,
        order_id: int,
        desired: dict[str, Any],
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any],
    ) -> UpdateExecutionSummary:
        """规则 7：update_order 结果未知 → 先查询最新订单。

        - 查询成功且期望变更已应用（重新生成差异为空）→ 展示最新快照；
        - 查询成功但未应用 → failed 需重新确认（不盲目重试 update_order）；
        - 查询亦失败 → failed 无法确认（恢复流程沿用 D12/D14 已批准语义）。

        11.2 规则 2：每次实际调用只追加一条 result——本分支单次记录
        result_status=failed（11.3 监控按 result 事件计数不重复）。
        """
        self._record_result(attempt, "failed", err)
        try:
            fresh, _ = self._query_before(order_type, order_id)
        except MCPError as query_err:
            summary = (
                f"update_order 结果未知（{err.code}）且重新查询失败"
                f"（{query_err.code}）：无法确认是否已应用；"
                "不盲目重试，需人工核查（5.4 规则 7）"
            )
            # [2026-09-12 一张卡多条明细] 任务终态不再在此标记（单条由
            # execute() 依 result_status 标记；批量由 BatchExecutor 聚合）
            return self._summary(
                task_id, order_type, order_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_UPDATE_NETWORK_UNKNOWN,
            )
        if fresh is None:
            fresh = {}
        fresh_changes = build_update_changes(fresh, desired, order_type)
        if not fresh_changes:
            summary = (
                f"update_order 结果未知（{err.code}），重新查询确认期望变更"
                "已应用：按最新快照展示，不重复调用（5.4 规则 7）"
            )
            return self._summary(
                task_id, order_type, order_id, "success", None,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, after=dict(fresh),
                refetched_changes=fresh_changes,
            )
        summary = (
            f"update_order 结果未知（{err.code}），重新查询确认未应用；"
            f"当前差异 {fresh_changes}；需重新确认后执行，不盲目重试（5.4 规则 7）"
        )
        return self._summary(
            task_id, order_type, order_id, "failed", summary,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, error_code=ERROR_UPDATE_NETWORK_UNKNOWN,
            refetched_changes=fresh_changes,
        )

    # ---- 写前查询与审计 ----

    def _query_before(self, order_type: str, order_id: int) -> tuple[dict[str, Any] | None, str]:
        """规则 1/4：写前查询唯一目标订单，返回 (订单或 None, updated_at)。

        R4 query：只读、显式字段（白名单字段 + id/updated_at）；网络
        等失败抛 MCPError（调用方决定 fail 分支，不在此处重试）。
        """
        entity = "metal_orders" if order_type == "metal" else "powder_orders"
        result = self._client.call_tool(
            "query",
            {
                "entity": entity,
                "fields": sorted(_UPDATE_QUERY_FIELDS[order_type]),
                "filter": {"id": order_id},
                "limit": 1,
            },
        )
        if not result.ok:
            raise result.error
        items = result.data.get("items") if isinstance(result.data, dict) else None
        if not items:
            return None, ""
        order = dict(items[0])
        return order, str(order.get("updated_at") or "")

    def _record_mcp_retries(
        self,
        result: ToolResult,
        task_id: str,
        op_id: str,
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        """写工具的传输层瞬时重试留痕（见 AuditService.record_mcp_retries）。

        target_type 由调用方按 order_type 派生（本执行器不持有固定目标类型）。
        """
        if not result.retries:
            return
        self._audit.record_mcp_retries(
            retries=result.retries,
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            target_type=target_type,
            target_id=target_id,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )

    def _record_result(
        self,
        attempt: TaskAttemptRecord,
        status: str,
        err: MCPError,
    ) -> None:
        """调用返回后立即 result（11.2 L1226：success/replayed/failed/conflict）。"""
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status=status,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            error_code=err.code,
            requires_operator_action=bool(err.requires_operator_action),
            result_summary=err.message or status,
        )

    def _summary(
        self,
        task_id: str,
        order_type: str,
        order_id: int | None,
        result_status: str,
        error_summary: str | None,
        *,
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        changed_fields: tuple[str, ...] = (),
        other_effects: tuple[dict[str, Any], ...] = (),
        error_code: str | None = None,
        refetched_changes: dict[str, Any] | None = None,
    ) -> UpdateExecutionSummary:
        return UpdateExecutionSummary(
            task_id=task_id,
            operation_type=_UPDATE_OP_TYPES[order_type],
            order_type=order_type,
            order_id=order_id,
            expected_updated_at=expected_updated_at,
            changes=dict(changes),
            before_snapshot=dict(before) if before is not None else None,
            after_snapshot=dict(after) if after is not None else None,
            changed_fields=changed_fields,
            other_effects=other_effects,
            result_status=result_status,
            attempts=1,
            verification_passed=result_status == "success",
            error_code=error_code,
            error_summary=error_summary,
            refetched_changes=(
                dict(refetched_changes) if refetched_changes is not None else None
            ),
        )

    def _require_running(self, task_id: str) -> None:
        """执行前校验：任务必须 running（8.1 L930；8.3 等待/终态不执行；
        13.3 同 task 不并发由锁保证，状态兜底）。"""
        from administrative_agent.tasks.machine import TaskStatus
        from administrative_agent.tasks.service import TaskNotRunningError

        task = self._tasks.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            raise TaskNotRunningError(task_id, task.status)

    def _fail_task(self, task_id: str, error_code: str, error_summary: str) -> None:
        """任务标记 failed（8.1/13.2：需人工处理或重新发起；不假报成功）。"""
        try:
            self._tasks.mark_failed(
                task_id, error_code=error_code, error_summary=error_summary
            )
        except Exception:
            # 任务已终态时忽略；审计失败路径由调用方另行处理（同 D14 口径）
            logger.exception("order_update_fail_task_error", task_id=task_id)
