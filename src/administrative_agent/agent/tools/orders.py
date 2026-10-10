"""订单写工具（R2 创建 / R3 修改）参数/确认卡/准备/执行。

订单域专用：OrderCreateArgs/OrderUpdateArgs 参数模型（含 metal/powder 业务类型
字段冲突 validator）、build_powder_confirmation_card 确认卡、prepare_order_
create/execute_order_create（_prepare_metal_create/_prepare_powder_create）
与 prepare_order_update/execute_order_update 两段式接线，以及订单域私有
序列化辅助（_filled_*_dict/_metal_draft_*_dict/_powder_draft_*_dict）。

Phase 2 接线目标（domains/orders.py 内部零改动——本模块只做参数适配与
调用）：prepare 做全部只读前置（客户/供货商唯一匹配/资料补全/写前查询
唯一目标订单/确认卡构造），执行上下文（draft 序列化/期望变化）与卡片
一并写入 State.pending_confirmation；确认通过后 run 用同一执行上下文调
MetalOrderExecutor.execute（metal；塑粉复用 target_type="powder_order"，
5.2）/OrderUpdateExecutor.execute（保证"批准的卡片"就是"执行的载荷"，
payload_hash 变化即失效）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from pydantic import BaseModel, Field, model_validator

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.tools.base import (
    MAX_BATCH_ITEMS,
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
    _METAL_FORBIDDEN_FIELDS,
    _POWDER_FORBIDDEN_FIELDS,
    _match_unique,
    _query_all,
    _require_wiring,
    _summary_for,
    _today,
)
from administrative_agent.business.matching import (
    CustomerCandidate,
    SupplierCandidate,
    match_customer,
    match_supplier,
)
from administrative_agent.business.order_rules import (
    FilledField,
    MetalOrderDraft,
    PowderOrderDraft,
    complete_metal_order,
    complete_powder_order,
    extract_metal_order_input,
    extract_powder_order_input,
    resolve_customer_profile,
)
from administrative_agent.domains.orders import (
    ACTION_CREATE,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    ConfirmationCard,
    MetalOrderExecutor,
    OP_CREATE_METAL_ORDER,
    OrderBatchItem,
    OrderUpdateBatchItem,
    OrderUpdateExecutor,
    _build_update_target,
    _resolve_update_party_name,
    build_batch_detail_rows,
    build_metal_confirmation_card,
    build_update_batch_confirmation_card,
    build_update_changes,
    build_update_confirmation_card,
    canonical_json,
    card_payload_hash,
    localize_auto_filled,
    localize_confirmation_fields,
    plan_update_item,
    precheck_update_inputs,
)
from administrative_agent.mcp.errors import MCPError
from administrative_agent.mcp.scopes import verify_order_bundle_arguments


class OrderCreateItemArgs(BaseModel):
    """order_create 的一条明细。

    字段与 OrderCreateArgs 的单条字段一致（不含 order_type——一张卡不得
    混 metal/powder，order_type 由外层统一给定）。字段侧冲突校验在
    OrderCreateArgs 的 validator 内按外层 order_type 逐条执行。
    """

    customer_name: str | None = None
    model: str | None = None
    unit_price: float | None = None
    price_type: str | None = None
    weight_unit: str | None = None
    quantity: int | None = None
    quantity_unit: str | None = None
    weight_per_unit: float | None = None
    surcharge_amount: float | None = None
    supplier_name: str | None = None
    color_code: str | None = None
    total_weight: float | None = None
    price_per_kg: float | None = None
    in_date: str | None = None
    out_date: str | None = None
    status: str | None = None
    notes: str | None = None


class OrderCreateArgs(BaseModel):
    """order_create 参数（metal 侧字段来自 MetalOrderInput，powder 侧来自
    PowderOrderInput；order_type 判别后禁止另一侧字段——_field_conflicts
    语义迁移，L1368 fail closed）。

    metal 订单禁止 supplier_name/color_code/price_per_kg/total_weight；
    powder 订单禁止 customer_name/model/unit_price/price_type/weight_unit/
    quantity/quantity_unit/weight_per_unit/surcharge_amount——与业务类型
    冲突的字段不得携带（附加款仅钣金订单支持，塑粉禁止）。

    `items` 为批量明细（一张卡多条订单）；
    顶层单条字段保留为**单条简写**（向后兼容）。二者**不可同时提供**
    （fail closed）；`order_type` 恒为顶层（一张卡不混 metal/powder）。"""

    order_type: str = Field(
        ...,
        description="metal（钣金）或 powder（塑粉）；口语“XX 来了/到了/送来 YY kg 色号 Z 塑粉”= 供货商送货到货 → powder（供货商+色号+kg）；9010 等纯数字为塑粉色号，非钣金型号",
    )
    items: list[OrderCreateItemArgs] | None = Field(
        None,
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
        description=(
            "批量创建：一张确认卡多条订单明细（每条一条订单，最多 20 条）。"
            "与顶层单条字段二选一：提供 items 时不得再给顶层 customer_name/"
            "model 等单条字段；order_type 仍由顶层给定（一张卡不混 metal/powder）"
        ),
    )
    # metal 侧（10.2 规则表：钣金=客户/价格类型/重量单位；附加款见下）
    customer_name: str | None = None
    model: str | None = None
    unit_price: float | None = Field(None, description="单价（元，数字）；按该客户主数据 unit_price/price_unit 口径（可 erp_query 查 customers）；主数据未定价（单价 0）时须询问确认")
    price_type: str | None = None
    weight_unit: str | None = None
    quantity: int | None = Field(None, description="数量（整数）")
    quantity_unit: str | None = Field(None, description="数量单位（如 套）")
    weight_per_unit: float | None = Field(None, description="单重（kg，数字）；钣金重量喷涂前未知时省略该字段或填数字 0（0=待补充，不参与计价）；禁止填写文字或非数字")
    surcharge_amount: float | None = Field(
        None,
        description="附加款（元，数字，可选）：仅钣金订单支持，塑粉订单没有此字段；"
        "须 ≥0 且最多 2 位小数（如 12.50）；用户未提及时省略该字段（ERP 默认 0.00）；"
        "禁止填写文字或非数字",
    )
    # powder 侧（塑粉=供货商/色号；weight_unit 固定 kg 不提取，5.2）
    supplier_name: str | None = None
    color_code: str | None = None
    total_weight: float | None = Field(None, description="总重量（kg，数字）；未知时省略该字段；禁止填写文字或非数字")
    price_per_kg: float | None = Field(None, description="单价（元/kg，数字）")
    # 通用
    in_date: str | None = None
    out_date: str | None = None
    status: str | None = Field(
        None,
        description="订单状态；创建时固定为'未完成'（业务规则 10.2），禁止填写其他值（如'草稿'、'完成'），用户未指定时填'未完成'",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _check_business_type_field_conflicts(self) -> "OrderCreateArgs":
        if self.order_type not in ("metal", "powder"):
            raise ValueError(f"order_type 只能是 metal 或 powder，收到 {self.order_type!r}")
        forbidden = (
            _METAL_FORBIDDEN_FIELDS
            if self.order_type == "metal"
            else _POWDER_FORBIDDEN_FIELDS
        )
        # 单条简写字段（不含 order_type/items）
        flat_present = {
            f
            for f, v in self.model_dump().items()
            if f not in ("order_type", "items") and v is not None
        }
        # items 与顶层单条字段二选一（fail closed，避免"批量 + 单条"语义歧义）
        if self.items is not None and flat_present:
            raise ValueError(
                "items 与顶层单条字段不能同时提供（fail closed）："
                f"提供了 items 又给出 {sorted(flat_present)}；"
                "请只保留 items（批量）或只保留单条字段"
            )
        # 逐条明细的字段侧冲突（10.3）
        for idx, item in enumerate(self.items or [], start=1):
            present = {
                f for f, v in item.model_dump().items() if v is not None
            }
            conflict = present & forbidden
            if conflict:
                raise ValueError(
                    f"第 {idx} 条明细业务类型与字段冲突（10.3）："
                    f"order_type={self.order_type} 不允许 {sorted(conflict)}；"
                    "要求重新分类，不得修正后直接写入"
                )
        conflict = flat_present & forbidden
        if conflict:
            raise ValueError(
                f"业务类型与字段冲突（10.3）：order_type={self.order_type} 不允许"
                f" {sorted(conflict)}；要求重新分类，不得修正后直接写入"
            )
        return self

    @property
    def effective_items(self) -> list[dict[str, Any]]:
        """归一化为明细 dict 列表（items 优先；未给 items 时用顶层单条简写）。"""
        if self.items is not None:
            return [it.model_dump() for it in self.items]
        return [
            {
                k: v
                for k, v in self.model_dump().items()
                if k not in ("order_type", "items")
            }
        ]


class OrderUpdateItemArgs(BaseModel):
    """order_update 的一条明细。

    每条 = 目标 order_id + 期望变化字段（字段语义同 OrderUpdateArgs 单条）。
    """

    order_id: int = Field(..., gt=0, description="目标订单 ID")
    status: str | None = Field(None, description="未完成 或 完成")
    out_date: str | None = None
    notes: str | None = None
    quantity: int | None = None
    quantity_unit: str | None = None
    weight_per_unit: float | None = None
    surcharge_amount: float | None = None
    total_weight: float | None = Field(None, gt=0)
    price_per_kg: float | None = None
    color_code: str | None = None


class OrderUpdateArgs(BaseModel):
    """order_update 参数（5.4 八条：order_id 唯一目标 + 期望变化字段；
    status 只允许 未完成/完成——precheck_update_inputs L747 语义）。

    order_type 为 metal 或 powder；status 为 未完成 或 完成；out_date 为
    YYYY-MM-DD 或 null；total_weight 更新必须 > 0（L741）；metal 侧可更
    新 quantity/quantity_unit/weight_per_unit/surcharge_amount（附加款
    须 ≥0 且 ≤2 位小数），powder 侧可更新
    total_weight/price_per_kg/color_code——与业务类型冲突的字段不得携带。

    `items` 为批量明细（一张卡多条改单）；
    顶层单条字段（order_id + 变化字段）保留为单条简写。二者不可同时提供；
    **同一张卡内 order_id 不得重复**（fail closed：同一订单一次卡不应改两次）。"""

    order_type: str = Field(..., description="metal（钣金）或 powder（塑粉）")
    order_id: int | None = Field(
        None, gt=0,
        description="目标订单 ID（单条简写；使用 items 批量时省略）",
    )
    items: list[OrderUpdateItemArgs] | None = Field(
        None,
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
        description=(
            "批量修改：一张确认卡多条改单明细（每条 = order_id + 期望变化字段，"
            "最多 20 条）。与顶层单条字段二选一；同一卡内 order_id 不得重复"
        ),
    )
    status: str | None = Field(None, description="未完成 或 完成")
    out_date: str | None = None
    notes: str | None = None
    # metal 侧可更新字段
    quantity: int | None = Field(None, description="数量（整数）")
    quantity_unit: str | None = None
    weight_per_unit: float | None = Field(None, description="单重（kg，数字）；钣金重量喷涂前未知时省略该字段或填数字 0（0=待补充，不参与计价）；禁止填写文字或非数字")
    surcharge_amount: float | None = Field(
        None,
        description="附加款（元，数字，可选）：仅钣金订单支持，塑粉订单没有此字段；"
        "须 ≥0 且最多 2 位小数（如 12.50）；填 0 表示清除附加款；禁止填写文字或非数字",
    )
    # powder 侧可更新字段（total_weight 更新必须 > 0，L741）
    total_weight: float | None = Field(None, gt=0, description="总重量（kg，数字，必须大于 0）；禁止填写文字或非数字")
    price_per_kg: float | None = Field(None, description="单价（元/kg，数字）")
    color_code: str | None = None

    @model_validator(mode="after")
    def _check_business_type_field_conflicts(self) -> "OrderUpdateArgs":
        if self.order_type not in ("metal", "powder"):
            raise ValueError(f"order_type 只能是 metal 或 powder，收到 {self.order_type!r}")
        forbidden = (
            _METAL_FORBIDDEN_FIELDS
            if self.order_type == "metal"
            else _POWDER_FORBIDDEN_FIELDS
        )

        def _check_status(value: str | None, where: str) -> None:
            if value is not None and value not in ("未完成", "完成"):
                raise ValueError(
                    f"{where}status 只允许 未完成/完成（L747 预检），收到 {value!r}"
                )

        _check_status(self.status, "")

        flat_present = {
            f
            for f, v in self.model_dump().items()
            if f not in ("order_type", "order_id", "items") and v is not None
        }
        if self.items is not None:
            if flat_present or self.order_id is not None:
                raise ValueError(
                    "items 与顶层单条字段（order_id/变化字段）不能同时提供（fail closed）："
                    f"提供了 items 又给出 order_id={self.order_id} "
                    f"与字段 {sorted(flat_present)}；请只用 items 或只用单条字段"
                )
            # 同一卡内 order_id 不得重复（同一订单一次卡不应改两次，fail closed）
            ids = [it.order_id for it in self.items]
            if len(set(ids)) != len(ids):
                dupes = sorted({i for i in ids if ids.count(i) > 1})
                raise ValueError(
                    f"同一张卡内 order_id 不得重复（fail closed）：{dupes}；"
                    "同一订单无法在一张卡上修改两次"
                )
            for idx, item in enumerate(self.items, start=1):
                _check_status(item.status, f"第 {idx} 条明细 ")
                present = {f for f, v in item.model_dump().items() if v is not None}
                conflict = present & forbidden
                if conflict:
                    raise ValueError(
                        f"第 {idx} 条明细业务类型与字段冲突（10.3）："
                        f"order_type={self.order_type} 不允许 {sorted(conflict)}；"
                        "要求重新分类，不得修正后直接写入"
                    )
            return self

        if self.order_id is None:
            raise ValueError(
                "未提供 order_id（且未给 items）：单条更新必须给出目标订单 ID"
            )
        conflict = flat_present & forbidden
        if conflict:
            raise ValueError(
                f"业务类型与字段冲突（10.3）：order_type={self.order_type} 不允许"
                f" {sorted(conflict)}；要求重新分类，不得修正后直接写入"
            )
        return self

    @property
    def effective_items(self) -> list[dict[str, Any]]:
        """归一化为明细 dict 列表（items 优先；未给 items 时用顶层单条简写）。"""
        if self.items is not None:
            return [it.model_dump() for it in self.items]
        return [
            {
                k: v
                for k, v in self.model_dump().items()
                if k not in ("order_type", "items")
            }
        ]


def build_powder_confirmation_card(draft: PowderOrderDraft) -> ConfirmationCard:
    """塑粉订单轻确认卡（5.2 语义；与 build_metal_confirmation_card 同构）。
    [2026-08-28 确认卡字段中文化] 展示层翻译，执行载荷不变。

    要素 5：ERP 创建塑粉订单（重量单位固定 kg，5.2）；不创建
    ar_payments/ap_payments/transactions（5.3 步骤 8）。

    [2026-09-10 展示口径] 目标只展示供货商名称（不写 supplier_id）。

    [2026-09-10 确认卡改版] target 置空（前端不渲染「目标」行），供货商名称
    改以 fields_to_write 首行「供货商」呈现；hash_target 仍取旧的
    f"供货商 {supplier_name}" 串，故 payload_hash 与改版前逐字节一致——
    card_payload_hash 优先用 hash_target，哈希口径零变化。

    hash_fields：[2026-09-10 修复] 取原始 to_write_dict()（含 supplier_id/
    customer_id 真实执行载荷）；展示层本地化/隐藏/首行名称行不影响哈希。
    """
    raw_fields = tuple(draft.to_write_dict().items())
    card = ConfirmationCard(
        operation_type="创建订单（塑粉）",
        target="",
        fields_to_write=localize_confirmation_fields(
            (("supplier_display_name", draft.supplier_name),) + raw_fields
        ),
        hash_fields=raw_fields,
        hash_target=f"供货商 {draft.supplier_name}",
        auto_filled=localize_auto_filled(draft.auto_filled()),
        expected_impact=(
            "在 ERP 创建塑粉订单；重量单位固定 kg，不写入字段（5.2）；"
            "不创建 ar_payments/ap_payments/transactions（5.3 步骤 8）。"
        ),
        limitations=(
            "订单总金额 total_amount 由 ERP 计算，不在此确认范围内；",
            "price_per_kg 未提供时按“供货商 + 色号”历史价格自动补全，"
            "无唯一历史价则要求询问（5.2）；",
            "最终校验以 ERP 创建响应为准（订单号等字段）。",
        ),
        payload_hash="",
    )
    return replace(card, payload_hash=card_payload_hash(card))


def prepare_order_create(ctx: ToolContext, args: OrderCreateArgs) -> PreparedToolWrite:
    """订单创建写：客户/供货商唯一匹配 → 资料补全 → 草稿 → 确认卡
    （5.1/5.2；metal/powder 判别）。

    [2026-09-12 一张卡多条明细] 单条（len==1，含顶层简写与 items 单元素）
    **委托既有单条函数**（卡片与执行载荷逐字节零回归）；多条走批量准备。
    """
    _require_wiring(ctx, tool="order_create")
    item_dicts = args.effective_items
    if len(item_dicts) == 1:
        single = OrderCreateArgs(order_type=args.order_type, **item_dicts[0])
        if args.order_type == "metal":
            return _prepare_metal_create(ctx, single)
        return _prepare_powder_create(ctx, single)
    return _prepare_order_create_batch(ctx, args.order_type, item_dicts)


def _metal_draft_for(
    ctx: ToolContext,
    item: dict[str, Any],
    *,
    name_outcome: Any | None = None,
    profile: Any | None = None,
) -> MetalOrderDraft:
    """钣金创建内核（单条与批量共用；不做确认卡/不落 IO 写）。

    - name_outcome/profile 为 None → 单条路径（各自查询，语义与旧实现一致）；
    - 批量路径传入预算好的 outcome 与按客户缓存的 profile（一次查询复用，
      避免 N 次全表查询）。
    """
    if name_outcome is None:
        name_outcome = _match_unique(
            ctx, entity="customers", ref=item.get("customer_name"),
            match_fn=match_customer,
            make_candidate=lambda r: CustomerCandidate(
                customer_id=int(r["id"]), name=str(r["company_name"])
            ),
            label="客户", name_field="company_name",
        )
    # [2026-09-08 口径修正] 单价与计价单位是客户主数据绑定的固定口径：
    # customers 以 price_unit（枚举「元/kg」「元/平方」，NOT NULL 默认
    # 「元/kg」）承载计价单位——weight_unit 由 price_unit 确定性映射得出
    # （元/kg→kg、元/平方→m²，order_rules.PRICE_UNIT_TO_WEIGHT_UNIT），
    # 不查历史金属订单推断，也不查询 customers 不存在的 weight_unit 字段
    if profile is None:
        profile_rows = _query_all(
            ctx, entity="customers",
            fields=("id", "unit_price", "price_type", "price_unit"),
            filter={"id": name_outcome.entity_id},
        )
        profile = resolve_customer_profile(profile_rows)
    inp = extract_metal_order_input(item)
    return complete_metal_order(
        inp,
        customer_id=name_outcome.entity_id,
        customer_name=name_outcome.matched_name,
        profile=profile,
        today=_today(),
    )


def _powder_draft_for(
    ctx: ToolContext,
    item: dict[str, Any],
    *,
    name_outcome: Any | None = None,
    history_rows: list[dict[str, Any]] | None = None,
    history_cache: dict[tuple[int, Any], list[dict[str, Any]]] | None = None,
) -> PowderOrderDraft:
    """塑粉创建内核（单条与批量共用）。

    - name_outcome 为 None → 单条路径（唯一匹配供货商）；
    - 历史价格：仅当用户未提供 price_per_kg 时查询（与旧实现同判据）；
      批量路径传 history_cache 按 (supplier_id, color_code) 复用查询结果。
    """
    if name_outcome is None:
        name_outcome = _match_unique(
            ctx, entity="suppliers", ref=item.get("supplier_name"),
            match_fn=match_supplier,
            make_candidate=lambda r: SupplierCandidate(
                supplier_id=int(r["id"]), name=str(r["name"])
            ),
            label="供货商",
        )
    inp = extract_powder_order_input(item)
    if history_rows is None:
        if inp.price_per_kg:
            history_rows = []
        else:
            key = (name_outcome.entity_id, inp.color_code)
            if history_cache is not None and key in history_cache:
                history_rows = history_cache[key]
            else:
                # 5.2 历史价格补全仅在用户未提供 price_per_kg 时查询
                # （complete_powder_order 内部判定；此处取全历史行供补全算法去重决策）
                history_rows = _query_all(
                    ctx, entity="powder_orders",
                    fields=("id", "price_per_kg", "supplier_id", "color_code"),
                    filter={
                        "supplier_id": name_outcome.entity_id,
                        "color_code": inp.color_code,
                    },
                )
                if history_cache is not None:
                    history_cache[key] = history_rows
    return complete_powder_order(
        inp,
        supplier_id=name_outcome.entity_id,
        supplier_name=name_outcome.matched_name,
        history_rows=history_rows,
        today=_today(),
    )


def _prepare_metal_create(ctx: ToolContext, args: OrderCreateArgs) -> PreparedToolWrite:
    item = args.model_dump()
    try:
        draft = _metal_draft_for(ctx, item)
    except ToolPreparationError:
        raise
    except Exception as exc:  # 业务补全失败（如 model 必填、资料未定价）→ 统一准备失败
        raise ToolPreparationError(
            f"订单创建准备失败（客户 {args.customer_name!r}）：{exc}"
        ) from exc
    card = build_metal_confirmation_card(draft)
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "order_create",
            "order_type": "metal",
            "draft": _metal_draft_to_dict(draft),
            "policy_key": "R2.order.create.metal",
        },
        policy_key="R2.order.create.metal",
    )


def _prepare_powder_create(ctx: ToolContext, args: OrderCreateArgs) -> PreparedToolWrite:
    item = args.model_dump()
    try:
        draft = _powder_draft_for(ctx, item)
    except ToolPreparationError:
        raise
    except Exception as exc:  # 业务补全失败（如无历史价格须询问）→ 统一准备失败
        raise ToolPreparationError(
            f"订单创建准备失败（供货商 {args.supplier_name!r}）：{exc}"
        ) from exc
    card = build_powder_confirmation_card(draft)
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "order_create",
            "order_type": "powder",
            "draft": _powder_draft_to_dict(draft),
            "policy_key": "R2.order.create.powder",
        },
        policy_key="R2.order.create.powder",
    )


# ---- 批量创建（[2026-09-12 一张卡多条明细]） ----

def _match_failure_message(label: str, ref: Any, outcome: Any) -> str:
    """批量名称匹配失败信息（与 base._match_unique 逐字一致）。"""
    if outcome.status == "ambiguous":
        names = "、".join(name for _, name in outcome.candidates)
        return (
            f"{label} {ref!r} 存在多个同名候选（{names}），"
            "需用户确认后重试（不自动选择，fail closed）"
        )
    return f"{label} {ref!r} 未找到精确匹配，需核对名称后重试（fail closed）"


def _reject_exact_duplicate_items(item_dicts: list[dict[str, Any]]) -> None:
    """批量创建**完全重复明细**直接拒绝（fail closed）。

    订单重复创建属高危（幂等键机制对"用户确实想建两单"无能为力）；内容
    完全一致（非空字段集合与值全等）的两条明细视为误重复，直接拒绝整批。
    """
    seen: dict[str, int] = {}
    for i, item in enumerate(item_dicts, start=1):
        key = canonical_json({k: v for k, v in item.items() if v is not None})
        if key in seen:
            raise ToolPreparationError(
                f"批量创建含完全重复的明细（第 {seen[key]} 条与第 {i} 条内容一致）："
                "订单重复创建风险高，已拒绝整批（fail closed），请去重后重试"
            )
        seen[key] = i


def _metal_batch_drafts(
    ctx: ToolContext, item_dicts: list[dict[str, Any]]
) -> list[tuple[int, MetalOrderDraft]]:
    """批量钣金草稿：名字匹配用**一次** _query_all（不是 N 次全表查询）。"""
    name_rows = _query_all(ctx, entity="customers", fields=("id", "company_name"))
    candidates = [
        CustomerCandidate(customer_id=int(r["id"]), name=str(r["company_name"]))
        for r in name_rows
        if r.get("id") is not None and r.get("company_name")
    ]
    profile_cache: dict[int, Any] = {}
    prepared: list[tuple[int, MetalOrderDraft]] = []
    errors: list[str] = []
    for i, item in enumerate(item_dicts, start=1):
        try:
            outcome = match_customer(item.get("customer_name"), candidates)
            if not outcome.writable:
                raise ToolPreparationError(
                    _match_failure_message("客户", item.get("customer_name"), outcome)
                )
            pid = outcome.entity_id
            if pid not in profile_cache:
                profile_cache[pid] = resolve_customer_profile(
                    _query_all(
                        ctx, entity="customers",
                        fields=("id", "unit_price", "price_type", "price_unit"),
                        filter={"id": pid},
                    )
                )
            draft = _metal_draft_for(
                ctx, item, name_outcome=outcome, profile=profile_cache[pid]
            )
        except ToolPreparationError as exc:
            errors.append(f"第 {i} 条：{exc}")
            continue
        except Exception as exc:  # 业务补全失败
            errors.append(f"第 {i} 条订单创建准备失败：{exc}")
            continue
        prepared.append((i, draft))
    if errors:
        raise ToolPreparationError("批量创建准备失败：" + "；".join(errors))
    return prepared


def _powder_batch_drafts(
    ctx: ToolContext, item_dicts: list[dict[str, Any]]
) -> list[tuple[int, PowderOrderDraft]]:
    """批量塑粉草稿：名字匹配一次 _query_all + 历史价按 (供货商,色号) 缓存。"""
    name_rows = _query_all(ctx, entity="suppliers", fields=("id", "name"))
    candidates = [
        SupplierCandidate(supplier_id=int(r["id"]), name=str(r["name"]))
        for r in name_rows
        if r.get("id") is not None and r.get("name")
    ]
    history_cache: dict[tuple[int, Any], list[dict[str, Any]]] = {}
    prepared: list[tuple[int, PowderOrderDraft]] = []
    errors: list[str] = []
    for i, item in enumerate(item_dicts, start=1):
        try:
            outcome = match_supplier(item.get("supplier_name"), candidates)
            if not outcome.writable:
                raise ToolPreparationError(
                    _match_failure_message("供货商", item.get("supplier_name"), outcome)
                )
            draft = _powder_draft_for(
                ctx, item, name_outcome=outcome, history_cache=history_cache
            )
        except ToolPreparationError as exc:
            errors.append(f"第 {i} 条：{exc}")
            continue
        except Exception as exc:  # 业务补全失败
            errors.append(f"第 {i} 条订单创建准备失败：{exc}")
            continue
        prepared.append((i, draft))
    if errors:
        raise ToolPreparationError("批量创建准备失败：" + "；".join(errors))
    return prepared


def _create_batch_item_id(order_type: str, draft: MetalOrderDraft | PowderOrderDraft) -> str:
    """明细稳定业务标识（op_id 命名空间；重复项后续由 build_item_ids 加 #n）。"""
    if order_type == "metal":
        return f"metal:{draft.customer_id}:{draft.model}:{draft.in_date}"
    return f"powder:{draft.supplier_id}:{draft.color_code}:{draft.in_date}"


def _create_batch_label(order_type: str, draft: MetalOrderDraft | PowderOrderDraft) -> str:
    """明细业务标签（取不到关键字段时降级为「明细」，不影响哈希）。"""
    if order_type == "metal":
        if draft.customer_name and draft.model:
            return f"客户 {draft.customer_name} / {draft.model}"
    else:
        if draft.supplier_name and draft.color_code:
            return f"供货商 {draft.supplier_name} / {draft.color_code}"
    return "明细"


def build_create_batch_confirmation_card(
    *, order_type: str, drafts: Sequence[Any], labels: Sequence[str]
) -> ConfirmationCard:
    """批量创建确认卡（4.3；raw 外层键用位置序号、展示用「序号. 业务标签」）。"""
    entry_items = [
        (label, {"action": ACTION_CREATE, "fields": draft.to_write_dict()})
        for label, draft in zip(labels, drafts)
    ]
    hash_fields, fields_to_write = build_batch_detail_rows(entry_items)
    auto_filled: list[FilledField] = []
    for i, draft in enumerate(drafts, start=1):
        # 自动补全项逐条加序号前缀（展示归属；不进哈希）
        for f in localize_auto_filled(draft.auto_filled()):
            auto_filled.append(replace(f, field=f"{i}. {f.field}"))
    if order_type == "metal":
        operation_type = OP_CREATE_METAL_ORDER
        expected_impact = (
            "在 ERP 批量创建状态为“未完成”的钣金订单；"
            "不创建 ar_payments/ap_payments/transactions（5.3 步骤 8）。"
        )
        limitations = (
            "订单总金额 total_amount 由 ERP 计算（基础金额 数量×单件重量×单价"
            "＋附加款 surcharge_amount），不在此确认范围内；",
            "weight_per_unit=0 仅表示待补充，需后续补录实际重量；",
            "最终校验以 ERP 创建响应为准（订单号等字段，D14 落库比对）。",
        )
    else:
        operation_type = "创建订单（塑粉）"
        expected_impact = (
            "在 ERP 批量创建塑粉订单；重量单位固定 kg，不写入字段（5.2）；"
            "不创建 ar_payments/ap_payments/transactions（5.3 步骤 8）。"
        )
        limitations = (
            "订单总金额 total_amount 由 ERP 计算，不在此确认范围内；",
            "price_per_kg 未提供时按“供货商 + 色号”历史价格自动补全，"
            "无唯一历史价则要求询问（5.2）；",
            "最终校验以 ERP 创建响应为准（订单号等字段）。",
        )
    card = ConfirmationCard(
        operation_type=operation_type,
        target="",
        fields_to_write=fields_to_write,
        hash_fields=hash_fields,
        hash_target=f"批量创建订单（{len(drafts)} 条）",
        auto_filled=tuple(auto_filled),
        expected_impact=expected_impact,
        limitations=limitations,
        payload_hash="",
        row_header_label="明细",
        row_quantifier="条",
    )
    return replace(card, payload_hash=card_payload_hash(card))


def _prepare_order_create_batch(
    ctx: ToolContext, order_type: str, item_dicts: list[dict[str, Any]]
) -> PreparedToolWrite:
    """批量创建准备：草稿 → 完全重复拒绝 → 禁止字段预检 → 唯一 item_id → 批量卡。"""
    from administrative_agent.domains.batch import BatchItem, build_item_ids

    _reject_exact_duplicate_items(item_dicts)
    if order_type == "metal":
        prepared = _metal_batch_drafts(ctx, item_dicts)
        draft_to_dict = _metal_draft_to_dict
        policy_key = "R2.order.create.metal"
    else:
        prepared = _powder_batch_drafts(ctx, item_dicts)
        draft_to_dict = _powder_draft_to_dict
        policy_key = "R2.order.create.powder"

    # L1 预检（用户点确认前）：逐条禁止字段 fail closed（与执行器同口径）
    verify_errors: list[str] = []
    for idx, draft in prepared:
        try:
            verify_order_bundle_arguments({"order": draft.to_write_dict()})
        except MCPError as exc:
            verify_errors.append(f"第 {idx} 条：{exc}")
    if verify_errors:
        raise ToolPreparationError(
            "批量创建载荷校验未通过（fail closed）：" + "；".join(verify_errors)
        )

    indices = [idx for idx, _ in prepared]
    drafts = [draft for _, draft in prepared]
    labels = [_create_batch_label(order_type, draft) for draft in drafts]
    placeholders = [
        BatchItem(
            index=idx,
            item_id=_create_batch_item_id(order_type, draft),
            action=ACTION_CREATE,
            label=label,
        )
        for idx, draft, label in zip(indices, drafts, labels)
    ]
    item_ids = build_item_ids(placeholders)

    card = build_create_batch_confirmation_card(
        order_type=order_type, drafts=drafts, labels=labels
    )
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "order_create",
            "order_type": order_type,
            "policy_key": policy_key,
            "batch_size": len(drafts),
            "items": [
                {
                    "index": idx,
                    "item_id": item_id,
                    "label": label,
                    "action": ACTION_CREATE,
                    "draft": draft_to_dict(draft),
                }
                for idx, item_id, label, draft in zip(indices, item_ids, labels, drafts)
            ],
        },
        policy_key=policy_key,
    )


def execute_order_create(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """订单创建执行：MetalOrderExecutor.execute（塑粉复用 target_type，5.2）。

    [2026-09-12 一张卡多条明细] **形状嗅探**：execution 含 "items" 键 → 批量
    执行器；不含（含在飞旧卡）→ 旧单条执行器，语义逐字节不变。
    """
    _require_wiring(ctx, tool="order_create")
    target_type = "metal_order" if execution["order_type"] == "metal" else "powder_order"
    executor = MetalOrderExecutor(
        client=ctx.client,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=execution.get("trace_id") or "",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        policy=policy_for(execution["policy_key"]) if execution.get("policy_key") else None,
        target_type=target_type,
    )
    if "items" in execution:
        order_type = execution["order_type"]
        batch_items = [
            OrderBatchItem(
                index=row["index"],
                item_id=row["item_id"],
                label=row["label"],
                action=row.get("action") or ACTION_CREATE,
                draft=(
                    _metal_draft_from_dict(row["draft"])
                    if order_type == "metal"
                    else _powder_draft_from_dict(row["draft"])
                ),
            )
            for row in execution["items"]
        ]
        summary = executor.execute_batch(task_id=task_id, items=batch_items)
        return _summary_for(ctx, execution, task_id, summary.to_dict())
    draft = (
        _metal_draft_from_dict(execution["draft"])
        if execution["order_type"] == "metal"
        else _powder_draft_from_dict(execution["draft"])
    )
    summary = executor.execute(task_id=task_id, draft=draft)
    return _summary_for(ctx, execution, task_id, summary.to_dict())


def prepare_order_update(ctx: ToolContext, args: OrderUpdateArgs) -> PreparedToolWrite:
    """订单修改写：写前查询唯一目标订单 → 期望变化 → 确认卡（5.4 规则 1）。

    [2026-09-12 一张卡多条明细] 单条（len==1，含顶层简写与 items 单元素）
    **委托既有单条函数**（卡片与执行载荷逐字节零回归）；多条走批量准备。
    """
    _require_wiring(ctx, tool="order_update")
    item_dicts = args.effective_items
    if len(item_dicts) == 1:
        single = OrderUpdateArgs(order_type=args.order_type, **item_dicts[0])
        return _prepare_single_update(ctx, single)
    return _prepare_order_update_batch(ctx, args.order_type, item_dicts)


def _prepare_single_update(ctx: ToolContext, args: OrderUpdateArgs) -> PreparedToolWrite:
    """单条更新准备（旧语义逐字节保留）。"""
    entity = "metal_orders" if args.order_type == "metal" else "powder_orders"
    rows = _query_all(ctx, entity=entity, filter={"id": args.order_id})
    if not rows:
        raise ToolPreparationError(
            f"订单 {args.order_id} 不存在（写前查询为空），需核对订单号后重试（fail closed）"
        )
    before = rows[0]
    desired = {
        k: v
        for k, v in args.model_dump().items()
        if v is not None and k not in ("order_type", "order_id", "items")
    }
    if not desired:
        raise ToolPreparationError(
            "未提供任何期望变化字段（status/交期/数量等），无更新内容（fail closed）"
        )
    # [2026-08-31 修复确认卡口径] 确认卡只展示「实际差异」：build_update_changes
    # （数值归一化比较，domains/orders.py:804）与执行侧重建卡
    # （OrderUpdateExecutor.execute L1067）同口径——desired 中已被当前值满足
    # 的字段不再进卡，避免「确认卡字段 ⊆ 执行卡字段」不一致导致确认即失效
    # （真实场景：一句话多个字段，部分已一致 → 确认卡 hash 与执行时重建卡
    # hash 必然不同 → ORDER_UPDATE_CONFIRMATION_INVALID 循环）
    changes = build_update_changes(before, desired, args.order_type)
    if not changes:
        raise ToolPreparationError(
            f"订单 {args.order_id} 的期望值 {desired} 与当前值一致，无需更新（5.4 规则 2）"
        )
    # [2026-09-08 ERP 附加款 体验修复] 输入预检提前到确认卡之前：与执行侧
    # 同口径调用 precheck_update_inputs（domains/orders.py L1107）——非法附加款
    # （负数/超 2 位小数/超列宽）与状态/out_date 联动问题在用户确认前拦截，
    # 避免「确认后才失败」；执行侧 OrderUpdateExecutor.execute 原有二次预检
    # 保留不动（防御纵深，payload hash 语义不变）。
    try:
        precheck_update_inputs(changes, before, args.order_type)
    except MCPError as exc:
        raise ToolPreparationError(
            f"订单 {args.order_id} 更新输入预检未通过（确认前拦截）：{exc.message}"
        ) from exc
    expected_updated_at = str(before.get("updated_at") or "")
    card = build_update_confirmation_card(
        order_type=args.order_type,
        order_id=args.order_id,
        before=before,
        changes=changes,
        expected_updated_at=expected_updated_at,
        # [2026-09-10 P2-3] 按 customer_id/supplier_id 反查关联方名称补进展示
        # 目标（生产订单表无名称列）；反查失败降级、不影响 hash。
        client=ctx.client,
    )
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "order_update",
            "order_type": args.order_type,
            "order_id": args.order_id,
            "desired": desired,
            "expected_updated_at": expected_updated_at,
            "policy_key": f"R3.order.update.{args.order_type}",
        },
        policy_key=f"R3.order.update.{args.order_type}",
    )


def _prepare_order_update_batch(
    ctx: ToolContext, order_type: str, item_dicts: list[dict[str, Any]]
) -> PreparedToolWrite:
    """批量更新准备：逐条写前查询 + plan_update_item（同执行入口同一纯函数）。

    - 任一条查询失败/不存在/无期望字段/预检不过 → 聚合后整体 fail closed
      （不建卡、不落库）；
    - 无变化明细 → action=unchanged（留卡展示「保持」，执行期零写调用）；
    - **全部**明细都无变化 → 无写入内容，fail closed（与单条同口径）。
    """
    from administrative_agent.domains.batch import BatchItem, build_item_ids

    entity = "metal_orders" if order_type == "metal" else "powder_orders"
    prepared: list[tuple[int, dict[str, Any], Any]] = []  # (index, desired, plan)
    errors: list[str] = []
    for i, d in enumerate(item_dicts, start=1):
        order_id = d["order_id"]
        desired = {
            k: v
            for k, v in d.items()
            if v is not None and k not in ("order_type", "order_id", "items")
        }
        if not desired:
            errors.append(f"第 {i} 条（订单 {order_id}）未提供任何期望变化字段")
            continue
        rows = _query_all(ctx, entity=entity, filter={"id": order_id})
        if not rows:
            errors.append(f"第 {i} 条：订单 {order_id} 不存在（写前查询为空）")
            continue
        before = rows[0]
        try:
            plan = plan_update_item(
                order_type=order_type, order_id=order_id,
                desired=desired, before=before,
            )
        except MCPError as exc:
            errors.append(f"第 {i} 条（订单 {order_id}）预检未通过：{exc.message}")
            continue
        prepared.append((i, desired, plan))
    if errors:
        raise ToolPreparationError(
            "批量更新准备失败（fail closed）：" + "；".join(errors)
        )
    if all(not plan.changes for _, _, plan in prepared):
        raise ToolPreparationError(
            "批量更新所有明细的期望值均与当前值一致，无需更新（5.4 规则 2）"
        )

    labels = [
        _build_update_target(
            order_type,
            plan.before,
            party_name=_resolve_update_party_name(ctx.client, order_type, plan.before),
        )
        for _, _, plan in prepared
    ]
    placeholders = [
        BatchItem(
            index=idx,
            item_id=f"{order_type}:{plan.order_id}",
            action=plan.action,
            label=label,
        )
        for (idx, _, plan), label in zip(prepared, labels)
    ]
    item_ids = build_item_ids(placeholders)
    items = [
        OrderUpdateBatchItem(
            index=idx,
            item_id=item_id,
            label=label,
            action=plan.action,
            order_id=plan.order_id,
            desired=desired,
            before=plan.before,
            changes=plan.changes,
            expected_updated_at=plan.expected_updated_at,
        )
        for (idx, desired, plan), item_id, label in zip(prepared, item_ids, labels)
    ]
    card = build_update_batch_confirmation_card(order_type=order_type, items=items)
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "order_update",
            "order_type": order_type,
            "policy_key": f"R3.order.update.{order_type}",
            "batch_size": len(items),
            "items": [
                {
                    "index": it.index,
                    "item_id": it.item_id,
                    "label": it.label,
                    "action": it.action,
                    "order_id": it.order_id,
                    "desired": it.desired,
                }
                for it in items
            ],
        },
        policy_key=f"R3.order.update.{order_type}",
    )


def execute_order_update(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """订单修改执行：OrderUpdateExecutor.execute（内部零改动，参数适配）。

    [2026-09-12 一张卡多条明细] **形状嗅探**：execution 含 "items" 键 → 批量
    执行器（整卡比对）；不含（含在飞旧卡）→ 旧单条执行器，语义逐字节不变。
    """
    _require_wiring(ctx, tool="order_update")
    executor = OrderUpdateExecutor(
        client=ctx.client,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=execution.get("trace_id") or "",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        policy=policy_for(execution["policy_key"]) if execution.get("policy_key") else None,
    )
    if "items" in execution:
        task = ctx.tasks.get_task(task_id)
        confirmed_hash = task.confirmed_payload_hash if task else None
        items = [
            OrderUpdateBatchItem(
                index=row["index"],
                item_id=row["item_id"],
                label=row["label"],
                action=row.get("action") or ACTION_UPDATE,
                order_id=row["order_id"],
                desired=row["desired"],
            )
            for row in execution["items"]
        ]
        summary = executor.execute_batch(
            task_id=task_id,
            order_type=execution["order_type"],
            items=items,
            confirmed_hash=confirmed_hash or "",
        )
        return _summary_for(ctx, execution, task_id, summary.to_dict())
    summary = executor.execute(
        task_id=task_id,
        order_type=execution["order_type"],
        order_id=execution["order_id"],
        desired=execution["desired"],
    )
    data = summary.to_dict()
    if data.get("refetched_changes"):
        # 5.4 规则 6/7：内容变化 → 原确认失效，不得执行
        return ToolResultSummary(
            ok=False,
            summary={
                "tool": "order_update",
                "task_id": task_id,
                **data,
                "error": "目标订单内容已变化（版本冲突/结果未知），已重新生成差异，"
                         "需重新确认后执行（5.4 规则 6/7）",
            },
        )
    return _summary_for(ctx, execution, task_id, summary.to_dict())


# ---- 内部：订单域私有序列化辅助 ----

def _filled_to_dict(filled: tuple[FilledField, ...]) -> list[dict[str, Any]]:
    return [
        {
            "field": f.field,
            "value": f.value,
            "source": f.source,
            "previous_value": f.previous_value,
        }
        for f in filled
    ]


def _filled_from_dict(rows: list[dict[str, Any]]) -> tuple[FilledField, ...]:
    return tuple(FilledField(**r) for r in rows)


def _metal_draft_to_dict(draft: MetalOrderDraft) -> dict[str, Any]:
    return {
        "customer_id": draft.customer_id,
        "customer_name": draft.customer_name,
        "model": draft.model,
        "unit_price": draft.unit_price,
        "price_type": draft.price_type,
        "weight_unit": draft.weight_unit,
        "quantity": draft.quantity,
        "quantity_unit": draft.quantity_unit,
        "weight_per_unit": draft.weight_per_unit,
        "surcharge_amount": draft.surcharge_amount,
        "in_date": draft.in_date,
        "status": draft.status,
        "out_date": draft.out_date,
        "notes": draft.notes,
        "filled": _filled_to_dict(draft.filled),
    }


def _metal_draft_from_dict(data: dict[str, Any]) -> MetalOrderDraft:
    return MetalOrderDraft(
        **{k: v for k, v in data.items() if k != "filled"},
        filled=_filled_from_dict(data.get("filled") or []),
    )


def _powder_draft_to_dict(draft: PowderOrderDraft) -> dict[str, Any]:
    return {
        "supplier_id": draft.supplier_id,
        "supplier_name": draft.supplier_name,
        "color_code": draft.color_code,
        "total_weight": draft.total_weight,
        "price_per_kg": draft.price_per_kg,
        "in_date": draft.in_date,
        "notes": draft.notes,
        "filled": _filled_to_dict(draft.filled),
    }


def _powder_draft_from_dict(data: dict[str, Any]) -> PowderOrderDraft:
    return PowderOrderDraft(
        **{k: v for k, v in data.items() if k != "filled"},
        filled=_filled_from_dict(data.get("filled") or []),
    )
