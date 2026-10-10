"""finance_create / finance_update 参数/确认卡/
准备/执行。

域实现位于 domains/finance.py（FinanceCreateExecutor/FinanceUpdateExecutor，
本模块只做参数适配与调用，domains 内部零改动）——prepare 做全部只读前置
（transaction_categories 分类强校验/外键存在性校验/写前查询唯一目标记录/
差异确认卡构造），执行上下文（payload/desired 序列化）与卡片一并写入
State.pending_confirmation；确认通过后 run 用同一执行上下文调对应
Executor（保证"批准的卡片"就是"执行的载荷"，payload_hash 变化即失效）。

分类强校验（R7）：category 必须命中 transaction_categories 且归属所选
tx_type——prepare 阶段 fail closed（报错附可用清单，模型先查表重试）；
执行器写前再复核（确认后分类表可能变化的防御，domains/finance.py）。

确认卡/载荷口径：
- **更新确认卡构造函数共用 domains/finance.py
  build_finance_update_confirmation_card**——执行器以执行时快照重建 hash
  与确认时保存的 payload_hash 比对（本地乐观锁，7.3）；prepare 与执行
  必须字节一致，故不在此另建；
- 创建确认卡（build_finance_create_confirmation_card）本模块构造
  （与考勤创建卡同构：执行器不重建创建卡 hash）。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.tools.base import (
    MAX_BATCH_ITEMS,
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
    _query_all,
    _require_wiring,
    _summary_for,
)
from administrative_agent.domains.batch import BatchItem, build_item_ids
from administrative_agent.domains.finance import (
    CATEGORY_FIELD,
    OP_CREATE_TRANSACTION,
    FinanceCreateExecutor,
    FinanceUpdateExecutor,
    _finance_source_note,
    build_finance_create_batch_confirmation_card,
    build_finance_update_batch_confirmation_card,
    build_finance_update_confirmation_card,
    normalize_amount,
    resolve_party_display,
)
from administrative_agent.domains.orders import (
    ACTION_CREATE,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    ConfirmationCard,
    build_update_changes,
    card_payload_hash,
    localize_confirmation_fields,
)

#: 收支类型枚举（10.2：参数值来自代码枚举——与 ERP transaction_categories
#: tx_type 实测口径一致：收入/支出/投资款）
TX_TYPES: tuple[str, ...] = ("收入", "支出", "投资款")

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _validate_amount(value: Any) -> str:
    """金额规范（同 domains.finance.normalize_amount 单一口径）。"""
    return normalize_amount(value)


def _validate_iso_date(v: str) -> str:
    """日期双校验（仿 attendance.py 考勤日期校验）：形状 + 日历双拦。"""
    if not _ISO_DATE_RE.fullmatch(v):
        raise ValueError(f"tx_date 必须是 YYYY-MM-DD 格式，收到 {v!r}")
    date.fromisoformat(v)  # 拦 2026-13-99 这类形状合法但日期非法的输入
    return v


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------


class FinanceCreateItemArgs(BaseModel):
    """批量创建卡中的**单条明细**。

    与扁平 FinanceCreateArgs 同字段同校验——每条明细**自带 tx_type**
    （不做顶层默认值，避免一张卡里方向混淆）。单条简写仍走扁平字段，
    `FinanceCreateArgs.model_validator` 把扁平字段规范化为一条本模型。
    """

    tx_type: Literal["收入", "支出", "投资款"] = Field(
        ...,
        description=(
            "收支类型：收入/支出/投资款（与 transaction_categories 的 tx_type 一致）。"
            "方向判定引导：用户口语开销语义（花了/付了/支付/买了/运费/房租/水电/"
            "人情/售后等）→ 支出；收款语义（收到/回款/打款/收款/进账/入账等）→ 收入；"
            "入股/投资 → 投资款（详细口径见规则包第 2 条）"
        ),
    )
    category: str = Field(
        ...,
        description=(
            "收支分类名：分类目录已由系统注入本轮上下文则直接从中选择（按 tx_type "
            "归属）；未注入时先用 erp_query 查 transaction_categories 取可用分类；"
            "分类必须命中且归属该类型，否则 fail closed 报错并附可用清单"
        ),
    )
    amount: str | float | int = Field(
        ...,
        description=(
            "金额（元，大于 0 的数字或数字字符串，如 165 或 \"165.00\"）；方向由 "
            "tx_type 表达，禁止负数、文字或公式；按两位小数规范化存储"
        ),
    )
    tx_date: str = Field(..., description="收支日期 YYYY-MM-DD")
    project: str | None = Field(
        None,
        description=(
            "项目名（可选）——**照用户原话填**：用户原话中陈述的具体业务内容"
            "（工程/订单/物料/产品/用途等，无论哪类）照原样填入，不解释不改写；"
            "原话无业务内容时留空。备注与此字段分工：notes 仅用户明示要备注才填"
        ),
    )
    notes: str | None = Field(
        None,
        description=(
            "备注——**默认空值**：仅当用户明确要求记录备注（如「备注：…」等明示）"
            "时才填写；用户未明示备注时不得填写，禁止把项目内容/用途/描述性文字"
            "写入备注（此类内容应填 project）"
        ),
    )
    customer_id: int | None = Field(
        None, gt=0,
        description="关联客户 ID（可选；先用 erp_query 查 customers 确认存在；与 supplier_id 互斥）",
    )
    supplier_id: int | None = Field(
        None, gt=0,
        description="关联供货商 ID（可选；先用 erp_query 查 suppliers 确认存在；与 customer_id 互斥）",
    )

    @field_validator("amount")
    @classmethod
    def _norm_amount(cls, v: str | float | int) -> str:
        return _validate_amount(v)

    @field_validator("tx_date")
    @classmethod
    def _norm_date(cls, v: str) -> str:
        return _validate_iso_date(v)

    @model_validator(mode="after")
    def _check_fk_conflict(self) -> "FinanceCreateItemArgs":
        if self.customer_id is not None and self.supplier_id is not None:
            raise ValueError(
                "customer_id 与 supplier_id 互斥（一条收支记录只关联客户或供货商"
                "一方，R7）；需确认后保留其一重试"
            )
        return self


class FinanceCreateArgs(FinanceCreateItemArgs):
    """finance_create 参数（收支记录创建，R7；支持一张卡多条明细）。

    两种形态，**二选一**（同时提供 → fail closed）：
    - **扁平字段**（单条简写）：tx_type/category/amount/tx_date 必填，
      project/notes/customer_id/supplier_id 可选——行为与改造前逐字节一致；
    - **items 列表**（批量，1..MAX_BATCH_ITEMS 条）：每条明细自带
      tx_type/category/amount/tx_date 等（**不做顶层默认值**，避免方向混淆）。

    禁止把同轮的多条收支拆成多次 finance_create 调用——一次调用把所有明细
    放进 items，系统只出一张确认卡、点一次确认即批量写入；单条明细失败不
    中断其余条目（可再点确认恢复补写）。

    category 是强校验分类（归属所填 tx_type）；amount 金额（元，>0）方向由
    tx_type 表达，两位小数规范化（如 "165.00"）；系统字段
    （source_type/source_id/created_at 等）禁止传入。
    """

    tx_type: Literal["收入", "支出", "投资款"] | None = None
    category: str | None = None
    amount: str | float | int | None = None
    tx_date: str | None = None
    items: list[FinanceCreateItemArgs] | None = Field(
        None,
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
        description=(
            "批量明细列表（1..20 条；一条卡多条收支）：每项含 tx_type/category/"
            "amount/tx_date（+可选 project/notes/customer_id/supplier_id）。"
            "同轮多条收支**必须放进本列表一次调用**，不要拆成多次调用"
        ),
    )

    @model_validator(mode="after")
    def _normalize_batch(self) -> "FinanceCreateArgs":
        flat = {
            "tx_type": self.tx_type,
            "category": self.category,
            "amount": self.amount,
            "tx_date": self.tx_date,
            "project": self.project,
            "notes": self.notes,
            "customer_id": self.customer_id,
            "supplier_id": self.supplier_id,
        }
        provided = [k for k, v in flat.items() if v is not None]
        if self.items is not None:
            if provided:
                raise ValueError(
                    "items 与扁平字段（" + "、".join(provided) + "）不能同时提供，"
                    "请二选一（fail closed）"
                )
            return self
        missing = [k for k in ("tx_type", "category", "amount", "tx_date") if flat[k] is None]
        if missing:
            raise ValueError(
                "必须提供 items 列表，或完整的扁平字段（缺 "
                + "、".join(missing) + "）"
            )
        self.items = [FinanceCreateItemArgs(**flat)]
        return self

    def effective_items(self) -> list[FinanceCreateItemArgs]:
        """规范化后的明细列表（扁平简写已折叠为 1 条）。"""
        return list(self.items or [])


class FinanceUpdateItemArgs(BaseModel):
    """批量更新卡中的**单条明细**。

    id（目标记录）+ 期望变化字段（可选，只改真正变化的）；同一张卡内 id
    重复直接拒（同一条被更新两次语义不明，乐观锁必然冲突）。
    """

    id: int = Field(..., gt=0, description="目标收支记录 ID（写前先查确认存在）")
    tx_type: Literal["收入", "支出", "投资款"] | None = None
    category: str | None = None
    amount: str | float | int | None = Field(None, description="金额（元，>0，两位小数规范化）")
    tx_date: str | None = Field(None, description="收支日期 YYYY-MM-DD")
    project: str | None = Field(
        None,
        description=(
            "项目名（可选）——仅当用户明确要求修改时才作为变化字段传入；用户要求"
            "修改项目名时按其原话内容改（与 create 同口径：照用户原话填，不改写）"
        ),
    )
    notes: str | None = Field(
        None,
        description=(
            "备注——默认空值：仅当用户明确要求修改备注时才作为变化字段传入（未要求"
            "修改时不得列入，与当前值相同也不更新）；禁止把项目内容/用途/描述性文字"
            "写入备注（此类内容应填 project）"
        ),
    )
    customer_id: int | None = Field(None, gt=0, description="关联客户 ID（与 supplier_id 互斥）")
    supplier_id: int | None = Field(None, gt=0, description="关联供货商 ID（与 customer_id 互斥）")

    @field_validator("amount")
    @classmethod
    def _norm_amount(cls, v: str | float | int | None) -> str | None:
        if v is None:
            return None
        return _validate_amount(v)

    @field_validator("tx_date")
    @classmethod
    def _norm_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_iso_date(v)

    @model_validator(mode="after")
    def _check_fk_conflict(self) -> "FinanceUpdateItemArgs":
        if self.customer_id is not None and self.supplier_id is not None:
            raise ValueError(
                "customer_id 与 supplier_id 互斥（一条收支记录只关联客户或供货商"
                "一方，R7）；需确认后保留其一重试"
            )
        return self


class FinanceUpdateArgs(BaseModel):
    """finance_update 参数（R7 更新；支持一张卡多条明细）。

    两种形态，**二选一**（同时提供 → fail closed）：
    - **扁平字段**（单条简写）：id 必填 + 期望变化字段（仿 OrderUpdateArgs
      扁平可选模式，只修改真正变化的字段）——行为与改造前逐字节一致；
    - **items 列表**（批量，1..MAX_BATCH_ITEMS 条）：每条 = id + 期望变化
      字段；**同一张卡内 id 重复直接拒**。

    与 create 同口径：category 变化须归属生效 tx_type（desired 或当前
    记录）；amount 两位小数规范化；tx_date YYYY-MM-DD；customer_id 与
    supplier_id 互斥；系统字段禁止。
    """

    id: int | None = Field(None, gt=0, description="目标收支记录 ID（写前先查确认存在）")
    tx_type: Literal["收入", "支出", "投资款"] | None = None
    category: str | None = None
    amount: str | float | int | None = Field(None, description="金额（元，>0，两位小数规范化）")
    tx_date: str | None = Field(None, description="收支日期 YYYY-MM-DD")
    project: str | None = Field(
        None,
        description=(
            "项目名（可选）——仅当用户明确要求修改时才作为变化字段传入；用户要求"
            "修改项目名时按其原话内容改（与 create 同口径：照用户原话填，不改写）"
        ),
    )
    notes: str | None = Field(
        None,
        description=(
            "备注——默认空值：仅当用户明确要求修改备注时才作为变化字段传入（未要求"
            "修改时不得列入，与当前值相同也不更新）；禁止把项目内容/用途/描述性文字"
            "写入备注（此类内容应填 project）"
        ),
    )
    customer_id: int | None = Field(None, gt=0, description="关联客户 ID（与 supplier_id 互斥）")
    supplier_id: int | None = Field(None, gt=0, description="关联供货商 ID（与 customer_id 互斥）")
    items: list[FinanceUpdateItemArgs] | None = Field(
        None,
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
        description=(
            "批量明细列表（1..20 条；一张卡多条收支修改）：每项含 id + 期望变化"
            "字段。同一张卡内 id 不得重复；同轮多条修改**必须放进本列表一次调用**"
        ),
    )

    @field_validator("amount")
    @classmethod
    def _norm_amount(cls, v: str | float | int | None) -> str | None:
        if v is None:
            return None
        return _validate_amount(v)

    @field_validator("tx_date")
    @classmethod
    def _norm_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_iso_date(v)

    @model_validator(mode="after")
    def _check_fk_conflict(self) -> "FinanceUpdateArgs":
        if self.customer_id is not None and self.supplier_id is not None:
            raise ValueError(
                "customer_id 与 supplier_id 互斥（一条收支记录只关联客户或供货商"
                "一方，R7）；需确认后保留其一重试"
            )
        return self

    @model_validator(mode="after")
    def _normalize_batch(self) -> "FinanceUpdateArgs":
        flat = {
            "id": self.id,
            "tx_type": self.tx_type,
            "category": self.category,
            "amount": self.amount,
            "tx_date": self.tx_date,
            "project": self.project,
            "notes": self.notes,
            "customer_id": self.customer_id,
            "supplier_id": self.supplier_id,
        }
        provided = [k for k, v in flat.items() if v is not None]
        if self.items is not None:
            if provided:
                raise ValueError(
                    "items 与扁平字段（" + "、".join(provided) + "）不能同时提供，"
                    "请二选一（fail closed）"
                )
            ids = [it.id for it in self.items]
            if len(ids) != len(set(ids)):
                raise ValueError(
                    "同一张卡内 id 重复（同一条记录被更新两次语义不明），"
                    "请合并为一条后重试（fail closed）"
                )
            return self
        if self.id is None:
            raise ValueError("必须提供 items 列表，或 id + 期望变化字段")
        self.items = [FinanceUpdateItemArgs(**flat)]
        return self

    def effective_items(self) -> list[FinanceUpdateItemArgs]:
        """规范化后的明细列表（扁平简写已折叠为 1 条）。"""
        return list(self.items or [])


# ---------------------------------------------------------------------------
# 确认卡（创建侧在本模块；更新侧共用 domains/finance.py 构造）
# ---------------------------------------------------------------------------


def build_finance_create_confirmation_card(
    payload: dict[str, Any],
    party_display: tuple[tuple[str, str], ...] = (),
) -> ConfirmationCard:
    """收支创建轻确认卡（与 build_attendance_confirmation_card 同构，
    纯函数；R7 六要素）。

    - 目标：收支类型·分类（日期）；
    - 写值：payload 全字段（中文标签展示）+ party_display——[2026-09-10
      P1-1] customer_id/supplier_id 被 CONFIRMATION_HIDDEN_FIELDS 隐藏后，
      以 resolve_party_display 解析出的名称（或「客户ID 16」占位）呈现，
      不静默丢失关联方（执行载荷仍原样保留 id）；
    - 要素 5：ERP 创建该收支记录（通用 create，mcp-api.md §5）；
    - 要素 6：分类强校验（命中 transaction_categories 且归属 tx_type）、
      金额两位小数规范化、系统字段禁止。

    hash_fields：[2026-09-10 修复] 取原始 payload（含 customer_id/
    supplier_id 真实执行载荷）；party_display 为派生展示字段，绝不入哈希。
    """
    card = ConfirmationCard(
        operation_type=OP_CREATE_TRANSACTION,
        target=(
            f"{payload.get('tx_type')}·{payload.get('category')}"
            f"（{payload.get('tx_date')}）"
        ),
        fields_to_write=localize_confirmation_fields(
            tuple(payload.items()) + tuple(party_display)
        ),
        hash_fields=tuple(payload.items()),
        auto_filled=(),
        expected_impact=(
            "在 ERP 创建该收支记录（transactions，通用 create，mcp-api.md §5："
            "直接写入，不触发订单/收付款等其他记录联动）；金额按两位小数"
            "规范化存储（如 165 → \"165.00\"）。"
        ),
        limitations=(
            "分类必须命中 transaction_categories 且归属所选收支类型"
            "（写前/写前复查 fail closed，R7）；",
            "source_type/source_id/created_at 等系统字段由 ERP 联动维护，"
            "禁止传入；",
            "最终校验以 ERP 创建响应为准（record id）。",
        ),
        payload_hash="",
        # [2026-10-05 确认卡改进第 3 批] 收支卡固定口径提示（纯展示级，不进
        # 哈希；None 时不落键）。仅当卡面确实含分类字段（category）时才挂。
        source_note=_finance_source_note(payload.keys()),
    )
    return replace(card, payload_hash=card_payload_hash(card))


# ---------------------------------------------------------------------------
# prepare / execute（两段式接线）
# ---------------------------------------------------------------------------


def prepare_finance_create(
    ctx: ToolContext, args: FinanceCreateArgs
) -> PreparedToolWrite:
    """收支创建写：分类强校验 → 外键存在性 → 载荷 → 确认卡（R7）。

    单条（len == 1，含扁平简写）**委托单条路径**（逐字节零回归）；
    ≥2 条走批量路径（一张卡多条明细，一次确认批量写入）。
    """
    _require_wiring(ctx, tool="finance_create")
    items = args.effective_items()
    if len(items) == 1:
        return _prepare_finance_create_single(ctx, items[0])
    return _prepare_finance_create_batch(ctx, items)


def _prepare_finance_create_single(
    ctx: ToolContext, item: FinanceCreateItemArgs
) -> PreparedToolWrite:
    """单条收支创建（改造前的原路径，行为逐字节不变）。"""
    # 1. 分类强校验（fail closed：未命中归属 → 报错附可用清单，先查表重试）
    available = _available_categories(ctx, item.tx_type)
    if item.category not in available:
        raise ToolPreparationError(
            f"收支分类 {item.category!r} 不属于 {item.tx_type}"
            f"（可用分类：{'、'.join(available)}）；先用 erp_query 查"
            " transaction_categories 确认分类后重试（fail closed）"
        )

    # 2. 外键存在性（customer_id/supplier_id 必须命中对应实体，fail closed）
    #    并取回关联方名称作展示项（[2026-09-10 P1-1]）
    party_display = _verify_foreign_keys(
        ctx, customer_id=item.customer_id, supplier_id=item.supplier_id
    )

    # 3. 写值载荷（必填四项 + 非空可选；恒不含系统字段）
    payload = _create_payload(item)
    card = build_finance_create_confirmation_card(payload, party_display=party_display)
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "finance_create",
            # 顶层镜像（tools 侧 args_raw 与 confirm 侧 execution 同键，
            # tools_node._retry_key 特例块读取，见 tools/__init__ 说明）
            "tx_type": item.tx_type,
            "category": item.category,
            "tx_date": item.tx_date,
            "payload": payload,
            "policy_key": "R7.finance.create",
        },
        policy_key="R7.finance.create",
    )


def _prepare_finance_create_batch(
    ctx: ToolContext, items: list[FinanceCreateItemArgs]
) -> PreparedToolWrite:
    """批量收支创建（≥2 条）：分类去重查询 + 逐条外键/载荷 → 批量卡。

    预检失败 → ToolPreparationError **聚合列出全部失败条目**（整卡不挂、
    任务 mark_failed，由 tools 节点 _mark_prepare_failed 处理）；不静默、
    不落库。
    """
    # 1. 分类强校验：按 tx_type **去重后**查一次（避免 N 条重复查询同一类型）
    cat_cache: dict[str, list[str]] = {}
    failures: list[str] = []
    for idx, item in enumerate(items, start=1):
        try:
            if item.tx_type not in cat_cache:
                cat_cache[item.tx_type] = _available_categories(ctx, item.tx_type)
        except ToolPreparationError as exc:
            failures.append(f"第 {idx} 条：分类表查询失败（{exc}）")
            continue
        available = cat_cache[item.tx_type]
        if item.category not in available:
            failures.append(
                f"第 {idx} 条：收支分类 {item.category!r} 不属于 {item.tx_type}"
                f"（可用分类：{'、'.join(available)}）"
            )

    # 2. 外键存在性 + 载荷（逐条；外键失败同样聚合，不中断收集）
    parts: list[tuple[str, dict[str, Any], tuple[tuple[str, str], ...]]] = []
    for idx, item in enumerate(items, start=1):
        try:
            party_display = _verify_foreign_keys(
                ctx, customer_id=item.customer_id, supplier_id=item.supplier_id
            )
        except ToolPreparationError as exc:
            failures.append(f"第 {idx} 条：{exc}")
            continue
        label = f"{item.tx_type}·{item.category}（{item.tx_date}）"
        parts.append((label, _create_payload(item), party_display))

    if failures:
        raise ToolPreparationError(
            "批量创建预检未通过（整卡未挂起、未写入任何记录），请修正以下条目后"
            "重试（fail closed）：\n- " + "\n- ".join(failures)
        )

    # 3. 批量卡 + 执行载荷（业务标识复用单条 target 串，取不到时不影响哈希）
    card = build_finance_create_batch_confirmation_card(parts)
    base_item_ids = [
        f"{it.tx_type}:{it.category}:{it.tx_date}:{it.amount}" for it in items
    ]
    stable_ids = build_item_ids([
        BatchItem(index=i, item_id=base_item_ids[i - 1], action=ACTION_CREATE,
                  label=parts[i - 1][0], payload={})
        for i in range(1, len(items) + 1)
    ])
    execution_items = [
        {
            "index": i,
            "item_id": stable_ids[i - 1],
            "label": parts[i - 1][0],
            "action": ACTION_CREATE,
            "payload": parts[i - 1][1],
            "tx_type": items[i - 1].tx_type,
            "category": items[i - 1].category,
            "tx_date": items[i - 1].tx_date,
        }
        for i in range(1, len(items) + 1)
    ]
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "finance_create",
            # 顶层镜像（取首条；tools 侧批量 args_raw 无顶层键、confirm 侧
            # execution 有——[2026-09-12 阶段 6] _retry_key 对批量形状（items
            # ≥2）统一降级为 "{tool}:batch"，两形态同键不再依赖这些镜像键；
            # 镜像键保留仅供单条/调试阅读，勿删）
            "tx_type": items[0].tx_type,
            "category": items[0].category,
            "tx_date": items[0].tx_date,
            "batch_size": len(items),
            "items": execution_items,
            "policy_key": "R7.finance.create",
        },
        policy_key="R7.finance.create",
    )


def prepare_finance_update(
    ctx: ToolContext, args: FinanceUpdateArgs
) -> PreparedToolWrite:
    """收支修改写：写前查询唯一目标记录 → 期望变化 → 分类一致性 →
    差异确认卡（R7 规则 1/2；本地乐观锁 expected_updated_at 进卡）。

    单条（len == 1）委托单条路径（逐字节零回归）；≥2 条走批量路径。
    """
    _require_wiring(ctx, tool="finance_update")
    items = args.effective_items()
    if len(items) == 1:
        return _prepare_finance_update_single(ctx, items[0])
    return _prepare_finance_update_batch(ctx, items)


def _prepare_finance_update_single(
    ctx: ToolContext, item: FinanceUpdateItemArgs
) -> PreparedToolWrite:
    """单条收支修改（改造前的原路径，行为逐字节不变）。"""
    # 规则 1：写前查询唯一目标记录（不基于记忆或猜测写入）
    rows = _query_all(ctx, entity="transactions", filter={"id": item.id})
    if not rows:
        raise ToolPreparationError(
            f"收支记录 {item.id} 不存在（写前查询为空），需核对记录 ID 后重试"
            "（fail closed）"
        )
    before = rows[0]

    desired = {k: v for k, v in item.model_dump().items() if v is not None and k != "id"}
    if not desired:
        raise ToolPreparationError(
            "未提供任何期望变化字段（类型/分类/金额/日期/项目等），无更新内容"
            "（fail closed）"
        )
    # 规则 2：只生成真正变化字段（数值归一化比较；与执行侧重建卡同口径——
    # desired 中已被当前值满足的字段不再进卡，防「确认卡 ⊆ 执行卡」不一致）
    changes = build_update_changes(before, desired, "transactions")
    if not changes:
        raise ToolPreparationError(
            f"收支记录 {item.id} 的期望值 {desired} 与当前值一致，无需更新"
            "（R7 规则 2）"
        )

    # 分类一致性（生效 tx_type = desired 或当前记录；同执行器口径）
    if "category" in changes or "tx_type" in changes:
        _check_category_consistency(ctx, desired, before)

    expected_updated_at = str(before.get("updated_at") or "")
    # [2026-09-10 P1-1] changes 含 customer_id/supplier_id 时解析关联方名称
    # 作展示项（与执行器重建卡同函数同输入 → hash 同口径）
    party_display = resolve_party_display(ctx.client, changes)
    card = build_finance_update_confirmation_card(
        record_id=item.id,
        before=before,
        changes=changes,
        expected_updated_at=expected_updated_at,
        party_display=party_display,
    )
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "finance_update",
            "id": item.id,  # 顶层镜像（_retry_key finance_update 读取）
            "desired": desired,
            "expected_updated_at": expected_updated_at,
            "policy_key": "R7.finance.update",
        },
        policy_key="R7.finance.update",
    )


def _prepare_finance_update_batch(
    ctx: ToolContext, items: list[FinanceUpdateItemArgs]
) -> PreparedToolWrite:
    """批量收支修改（≥2 条）：逐条写前查询 → 差异 → 分类一致性 → 整卡。

    无差异条目 → action="unchanged"（执行期零写调用，卡上显示「保持」）；
    全部条目都无差异 → 预检失败（无更新内容，不挂空卡）。
    """
    failures: list[str] = []
    entries: list[dict[str, Any]] = []
    labels: list[str] = []
    for idx, item in enumerate(items, start=1):
        rows = _query_all(ctx, entity="transactions", filter={"id": item.id})
        if not rows:
            failures.append(f"第 {idx} 条：收支记录 {item.id} 不存在（写前查询为空）")
            continue
        before = rows[0]
        desired = {
            k: v for k, v in item.model_dump().items() if v is not None and k != "id"
        }
        if not desired:
            failures.append(f"第 {idx} 条：未提供任何期望变化字段，无更新内容")
            continue
        changes = build_update_changes(before, desired, "transactions")
        if "category" in changes or "tx_type" in changes:
            try:
                _check_category_consistency(ctx, desired, before)
            except ToolPreparationError as exc:
                failures.append(f"第 {idx} 条：{exc}")
                continue
        expected_updated_at = str(before.get("updated_at") or "")
        entries.append({
            "record_id": item.id,
            "before": before,
            "changes": changes,
            "expected_updated_at": expected_updated_at,
            "party_display": resolve_party_display(ctx.client, changes),
        })
        key_label = (
            f"{str(before.get('tx_date') or '')} {str(before.get('category') or '')}"
        ).strip()
        labels.append(key_label or "收支记录")

    if failures:
        raise ToolPreparationError(
            "批量修改预检未通过（整卡未挂起、未写入任何记录），请修正以下条目后"
            "重试（fail closed）：\n- " + "\n- ".join(failures)
        )
    if not any(e["changes"] for e in entries):
        raise ToolPreparationError(
            "所有条目期望值均与当前值一致，无更新内容（R7 规则 2；fail closed）"
        )

    card = build_finance_update_batch_confirmation_card(entries)
    stable_ids = build_item_ids([
        BatchItem(index=idx, item_id=str(items[idx - 1].id), action=ACTION_UPDATE,
                  label=labels[idx - 1], payload={})
        for idx in range(1, len(items) + 1)
    ])
    execution_items = [
        {
            "index": idx,
            "item_id": stable_ids[idx - 1],
            "label": labels[idx - 1],
            "action": ACTION_UPDATE if entries[idx - 1]["changes"] else ACTION_UNCHANGED,
            "id": items[idx - 1].id,
            "desired": {
                k: v for k, v in items[idx - 1].model_dump().items()
                if v is not None and k != "id"
            },
        }
        for idx in range(1, len(items) + 1)
    ]
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "finance_update",
            "batch_size": len(items),
            "items": execution_items,
            "policy_key": "R7.finance.update",
        },
        policy_key="R7.finance.update",
    )


def execute_finance_create(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """收支创建执行（形状嗅探）：execution 无 items → 旧单条执行器（逐字节
    不变）；有 items → 批量执行器（写前签名预查 + 逐条 + 待恢复语义）。"""
    _require_wiring(ctx, tool="finance_create")
    executor = FinanceCreateExecutor(
        client=ctx.client,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=execution.get("trace_id") or "",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        policy=policy_for(execution["policy_key"]) if execution.get("policy_key") else None,
    )
    if "items" not in execution:
        summary = executor.execute(task_id=task_id, payload=execution["payload"])
        return _summary_for(ctx, execution, task_id, summary.to_dict())
    batch = executor.execute_batch(task_id=task_id, items=execution["items"])
    return _summary_for(ctx, execution, task_id, batch.to_dict())


def execute_finance_update(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """收支修改执行（形状嗅探）：execution 无 items → 旧单条执行器（逐字节
    不变）；有 items → 批量执行器（整卡确认比对 + 逐条 + 待恢复语义）。"""
    _require_wiring(ctx, tool="finance_update")
    executor = FinanceUpdateExecutor(
        client=ctx.client,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=execution.get("trace_id") or "",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        policy=policy_for(execution["policy_key"]) if execution.get("policy_key") else None,
    )
    if "items" not in execution:
        summary = executor.execute(
            task_id=task_id,
            record_id=execution["id"],
            desired=execution["desired"],
        )
        data = summary.to_dict()
        if data.get("refetched_changes"):
            # R7 规则 6/7（R3 语义迁移）：内容变化 → 原确认失效，不得执行
            return ToolResultSummary(
                ok=False,
                summary={
                    "tool": "finance_update",
                    "task_id": task_id,
                    **data,
                    "error": "目标收支记录内容已变化（结果未知后重查），已重新生成差异，"
                             "需重新确认后执行（R7 规则 6/7）",
                },
            )
        return _summary_for(ctx, execution, task_id, data)
    batch = executor.execute_batch(task_id=task_id, items=execution["items"])
    return _summary_for(ctx, execution, task_id, batch.to_dict())


# ---------------------------------------------------------------------------
# 内部：分类/外键/载荷辅助
# ---------------------------------------------------------------------------


def _available_categories(ctx: ToolContext, tx_type: str) -> list[str]:
    """transaction_categories 可用分类名（按 tx_type 归属；查询失败
    fail closed——分类表不可查时不允许写入）。"""
    rows = _query_all(
        ctx,
        entity="transaction_categories",
        fields=(CATEGORY_FIELD,),
        filter={"tx_type": tx_type},
    )
    names = {
        str(r.get(CATEGORY_FIELD))
        for r in rows
        if r.get(CATEGORY_FIELD) is not None
    }
    return sorted(names)


def _verify_foreign_keys(
    ctx: ToolContext, *, customer_id: int | None, supplier_id: int | None
) -> tuple[tuple[str, str], ...]:
    """外键存在性校验（mcp-api.md §4：id 过滤唯一目标，fail closed），
    并顺带取回名称作展示项（[2026-09-10 P1-1]：一次查询同时完成存在性校验
    与名称解析，且不是新造调用协议——沿用既有 customers/suppliers query）。

    返回关联方展示项（(展示键, 名称或「客户ID 16」占位)），供创建确认卡
    呈现被隐藏的 customer_id/supplier_id；查不到名称时占位而非删除。
    """
    display: list[tuple[str, str]] = []
    if customer_id is not None:
        rows = _query_all(
            ctx, entity="customers",
            fields=("id", "company_name"), filter={"id": customer_id},
        )
        if not rows:
            raise ToolPreparationError(
                f"客户 {customer_id} 不存在（customers 查无此 ID），"
                "需核对客户 ID 后重试（fail closed）"
            )
        name = rows[0].get("company_name")
        display.append((
            "customer_name",
            name.strip() if isinstance(name, str) and name.strip() else f"客户ID {customer_id}",
        ))
    if supplier_id is not None:
        rows = _query_all(
            ctx, entity="suppliers",
            fields=("id", "name"), filter={"id": supplier_id},
        )
        if not rows:
            raise ToolPreparationError(
                f"供货商 {supplier_id} 不存在（suppliers 查无此 ID），"
                "需核对供货商 ID 后重试（fail closed）"
            )
        name = rows[0].get("name")
        display.append((
            "supplier_name",
            name.strip() if isinstance(name, str) and name.strip() else f"供货商ID {supplier_id}",
        ))
    return tuple(display)


def _check_category_consistency(
    ctx: ToolContext, desired: dict[str, Any], before: dict[str, Any]
) -> None:
    """desired 涉及 tx_type/category 变化时的归属复查（执行器同口径）。

    - desired 含 category → 按生效 tx_type（desired 或当前记录）复查；
    - 仅改 tx_type → 当前记录 category（若有）必须归属新类型，否则要求
      一并修正（防止写出生理上不可能的 类型×分类 组合）。
    """
    if "category" in desired:
        eff_tx_type = str(desired.get("tx_type") or before.get("tx_type") or "")
        available = _available_categories(ctx, eff_tx_type)
        if str(desired["category"]) not in available:
            raise ToolPreparationError(
                f"收支分类 {desired['category']!r} 不属于 {eff_tx_type}"
                f"（可用分类：{'、'.join(available)}）；先用 erp_query 查"
                " transaction_categories 确认后重试（fail closed）"
            )
        return
    # 仅改 tx_type：当前分类必须仍归属新类型
    new_tx_type = str(desired["tx_type"] or "")
    current_category = before.get("category")
    if current_category:
        available = _available_categories(ctx, new_tx_type)
        if str(current_category) not in available:
            raise ToolPreparationError(
                f"收支分类 {current_category!r} 不归属新类型 {new_tx_type}"
                f"（可用分类：{'、'.join(available)}）；需同时更新 category "
                "后重试（fail closed）"
            )


def _create_payload(args: FinanceCreateItemArgs) -> dict[str, Any]:
    """写值载荷：必填四项 + 非空可选（project/notes 空串不写）；
    恒不含 source_type/source_id/系统字段。"""
    payload: dict[str, Any] = {
        "tx_type": args.tx_type,
        "category": args.category,
        "amount": args.amount,
        "tx_date": args.tx_date,
    }
    if args.project:
        payload["project"] = args.project
    if args.notes:
        payload["notes"] = args.notes
    if args.customer_id is not None:
        payload["customer_id"] = args.customer_id
    if args.supplier_id is not None:
        payload["supplier_id"] = args.supplier_id
    return payload
