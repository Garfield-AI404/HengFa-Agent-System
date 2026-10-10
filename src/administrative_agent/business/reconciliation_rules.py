"""R6 对账匹配与差异分类纯规则层（规格 2.6 business/reconciliation_rules.py；
6.4 步骤 5-9、L843/L845 附加条款、RV-29）。

口径（6.4 L826-841）：
- 匹配键 = 客户、型号、日期（步骤 5"客户、型号、日期为主要匹配条件"）；
  塑粉订单同构为 供货商、色号、日期（L845 塑粉差异执行路径与钣金不同，
  匹配键与 ERP 字段对齐：powder_orders 无 customer_id/model，有
  supplier_id/color_code/in_date）；
- 唯一匹配（步骤 6）：同一匹配键命中 1 个 ERP 订单 → 比较重量、数量、
  价格、状态等映射中存在的字段；字段比较数值规范化（12 与 12.00 不视为
  差异），避免假差异；
- ERP 缺失（步骤 7）：匹配键无命中 → 差异行"ERP 缺失"，不自动补建；
- 匹配歧义（步骤 8）：同一匹配键命中多个 ERP 订单 → "匹配歧义"，
  携带全部候选订单 id，绝不自动选一个继续（RV-29 通过标准）；
- L843：客户清单提供重量时可更新原 weight_per_unit=0 的钣金订单 →
  FieldDiff.kind="zero_weight_updatable"（D21 可执行）；非零旧值不一致
  仍列差异（kind="regular"），不静默覆盖；
- L845：塑粉订单价格/重量/色号差异必须使用 update_order →
  DiffEntry.write_entry="update_order"（本单元只标记，D21 执行）；
- 差异生成（步骤 9）不写入：本模块是纯函数，无网络/无数据库/无写入口。

匹配键缺失（Excel 行缺型号/日期等）→ classification="key_incomplete"
（映射必填校验应保证不发生，防御性表达，不猜测匹配）。
客户/供货商名称无法唯一匹配（D13 match_* 返回 ambiguous/not_found）→
classification="customer_unmatched"，报告表达不阻断（任务书口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from administrative_agent.business.matching import MatchOutcome

# ---------------------------------------------------------------------------
# 分类常量（RV-29 四类 + 主体匹配失败/键不完整两类防御表达）
# ---------------------------------------------------------------------------

#: 唯一匹配且无差异（通过标准：唯一匹配）
MATCHED = "matched"
#: 匹配键无 ERP 订单（通过标准：缺失）
ERP_MISSING = "erp_missing"
#: 唯一匹配但字段有差异（通过标准：字段差异，含前后值）
FIELD_DIFF = "field_diff"
#: 同一匹配键多个候选（通过标准：匹配歧义，不得自动选）
AMBIGUOUS = "ambiguous"
#: 客户/供货商名称无法唯一匹配（ambiguous/not_found）→ 报告表达不阻断
CUSTOMER_UNMATCHED = "customer_unmatched"
#: Excel 行缺匹配字段（型号/日期等）→ 不猜测匹配
KEY_INCOMPLETE = "key_incomplete"

#: D21 写入路径标记（本单元只标记不执行；L843/L845 指定的两类）
WRITE_CREATE_ORDER_BUNDLE = "create_order_bundle"
WRITE_UPDATE_ORDER = "update_order"

#: 字段差异类型：普通差异（列差异，不静默）；原重量 0 可更新（L843）
DIFF_KIND_REGULAR = "regular"
DIFF_KIND_ZERO_WEIGHT_UPDATABLE = "zero_weight_updatable"

#: 各实体匹配键的 Excel target_field（6.4 步骤 5：客户、型号、日期；
#: 塑粉同构：供货商、色号、日期）
ENTITY_MATCH_FIELDS: dict[str, tuple[str, ...]] = {
    "metal_orders": ("customer_name", "model", "in_date"),
    "powder_orders": ("supplier_name", "color_code", "in_date"),
}

#: 各实体 ERP 行中的主体 id 字段（customer_id / supplier_id）
ENTITY_SUBJECT_ID_FIELD: dict[str, str] = {
    "metal_orders": "customer_id",
    "powder_orders": "supplier_id",
}

#: 各实体 ERP 行中的产品标识字段（model / color_code）
ENTITY_PRODUCT_FIELD: dict[str, str] = {
    "metal_orders": "model",
    "powder_orders": "color_code",
}

#: 主体查询实体（名称 → 唯一匹配）
ENTITY_SUBJECT_ENTITY: dict[str, str] = {
    "metal_orders": "customers",
    "powder_orders": "suppliers",
}

#: 实体主体名称字段（customers 为 company_name，suppliers 为 name；
#: ERP 字段目录核实）
ENTITY_SUBJECT_NAME_FIELD: dict[str, str] = {
    "customers": "company_name",
    "suppliers": "name",
}

#: 主体匹配函数对应（D13 复用；metal→match_customer，powder→match_supplier）
ENTITY_SUBJECT_MATCH: dict[str, str] = {
    "metal_orders": "match_customer",
    "powder_orders": "match_supplier",
}

#: L845：塑粉订单价格/重量/色号差异 → 必须 update_order（不能通用 update）
POWDER_UPDATE_ORDER_FIELDS = frozenset(
    {"price_per_kg", "total_weight", "color_code"}
)

#: 可比较字段数值（金额/数量/重量等按数值规范化比较，避免假差异）；
#: 其余字段（状态/日期/文本/枚举）按展示值精确比较
_NUMERIC_TYPES = frozenset(
    {"unit_price", "quantity", "weight_per_unit", "total_amount",
     "price_per_kg", "total_weight"}
)


# ---------------------------------------------------------------------------
# 领域记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchKey:
    """6.4 步骤 5 匹配键：主体（客户/供货商）id + 型号/色号 + 日期。"""

    entity: str
    subject_id: int
    product_ref: str
    in_date: str  # YYYY-MM-DD（解析与 ERP 均规范化为该格式）


@dataclass(frozen=True)
class FieldDiff:
    """单个字段差异（RV-29：字段差异含前后值）。

    kind：zero_weight_updatable = L843 原重量 0 可更新；regular = 不一致
    仍列差异，不静默覆盖。
    """

    field: str
    excel_value: str | None
    erp_value: str | None
    kind: str = DIFF_KIND_REGULAR

    @property
    def updatable_without_review(self) -> bool:
        return self.kind == DIFF_KIND_ZERO_WEIGHT_UPDATABLE


@dataclass(frozen=True)
class DiffEntry:
    """一条 Excel 行的差异分类结果（RV-29：四类 + 定位信息）。

    - row_number/source_file_hash/mapping_version：与 6.4 步骤 3 保留的
      定位信息一致（D20 复核页、D21 执行按此定位原文件行）；
    - write_entry：D21 写入路径标记（本单元只标记不执行）；
    - message：人类可读说明（歧义候选清单 / 客户匹配失败原因等）。
    """

    classification: str
    row_number: int
    source_file_hash: str
    mapping_version: int
    match_key: MatchKey | None = None
    matched_order_id: int | None = None
    diffs: tuple[FieldDiff, ...] = ()
    write_entry: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "row_number": self.row_number,
            "source_file_hash": self.source_file_hash,
            "mapping_version": self.mapping_version,
            "match_key": {
                "entity": self.match_key.entity,
                "subject_id": self.match_key.subject_id,
                "product_ref": self.match_key.product_ref,
                "in_date": self.match_key.in_date,
            }
            if self.match_key is not None
            else None,
            "matched_order_id": self.matched_order_id,
            "diffs": [
                {
                    "field": d.field,
                    "excel_value": d.excel_value,
                    "erp_value": d.erp_value,
                    "kind": d.kind,
                }
                for d in self.diffs
            ],
            "write_entry": self.write_entry,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# 规范化比较（避免假差异：12 与 12.00 不算差异；6.1 第 5 条不本地改格式，
# 比较用规范化值、展示用原值）
# ---------------------------------------------------------------------------


def norm_value(value: str | None) -> str | None:
    """数值规范化比较值（仅用于相等性判断，不改变展示值）。

    数值字段 → Decimal.normalize 去尾零（"12.00"→"12"、"12.50"→"12.5"）；
    无法解析的文本原样返回（不猜测）。日期/状态等文本精确比较。
    """
    if value is None:
        return None
    text = str(value).strip()
    try:
        return format(Decimal(text).normalize(), "f")
    except InvalidOperation:
        return text


def _field_diff(
    field: str, excel: str | None, erp: str | None, *, entity: str
) -> FieldDiff:
    kind = DIFF_KIND_REGULAR
    if (
        entity == "metal_orders"
        and field == "weight_per_unit"
        and norm_value(erp) == "0"
    ):
        # L843：客户清单提供重量时可更新原 weight_per_unit=0 的钣金订单
        kind = DIFF_KIND_ZERO_WEIGHT_UPDATABLE
    return FieldDiff(field=field, excel_value=excel, erp_value=erp, kind=kind)


def compare_excel_to_erp(
    *,
    entity: str,
    excel_values: Mapping[str, str],
    erp_values: Mapping[str, Any],
    compare_fields: Sequence[str],
) -> tuple[FieldDiff, ...]:
    """6.4 步骤 6：唯一匹配后比较映射中存在的字段（前后值齐全）。

    - compare_fields = 映射中存在的字段（target_field 集 − 匹配字段集）；
    - 数值字段规范化比较（避免 12 vs 12.00 假差异）；其余精确比较；
    - Excel 缺失的字段不比较（映射未定义该列，ERP 字段不受影响）；
    - L843 重量 0 可更新 vs 非零不一致的 kind 标记在此给出。
    """
    diffs: list[FieldDiff] = []
    for field in compare_fields:
        excel = excel_values.get(field)
        if excel is None:
            continue  # 该列在 Excel 中不存在 → 不参与比较
        erp = erp_values.get(field)
        erp_text = None if erp is None else str(erp)
        if norm_value(excel) == norm_value(erp_text):
            continue
        diffs.append(_field_diff(field, excel, erp_text, entity=entity))
    return tuple(diffs)


# ---------------------------------------------------------------------------
# 匹配键
# ---------------------------------------------------------------------------


def match_key_from_row(
    *, entity: str, subject_id: int, values: Mapping[str, str]
) -> MatchKey | None:
    """从标准行构造匹配键；缺匹配字段（型号/日期）→ None（键不完整，
    不猜测匹配）。subject_id 已由主体名称唯一匹配得出（D13）。"""
    product_field, date_field = ENTITY_MATCH_FIELDS[entity][1], ENTITY_MATCH_FIELDS[entity][2]
    product_ref = values.get(product_field)
    in_date = values.get(date_field)
    if not product_ref or not in_date:
        return None
    return MatchKey(entity=entity, subject_id=subject_id, product_ref=product_ref, in_date=in_date)


def match_key_from_erp(
    *, entity: str, order: Mapping[str, Any]
) -> MatchKey | None:
    """从 ERP 订单行构造匹配键；缺字段 → None（该行不参与匹配）。"""
    subject_field = ENTITY_SUBJECT_ID_FIELD[entity]
    product_field = ENTITY_PRODUCT_FIELD[entity]
    subject_id = order.get(subject_field)
    product_ref = order.get(product_field)
    in_date = order.get("in_date")
    if subject_id is None or not product_ref or not in_date:
        return None
    return MatchKey(
        entity=entity,
        subject_id=int(subject_id),
        product_ref=str(product_ref),
        in_date=str(in_date),
    )


def classify_excel_row(
    *,
    entity: str,
    row_number: int,
    source_file_hash: str,
    mapping_version: int,
    excel_values: Mapping[str, str],
    subject_outcome: MatchOutcome,
    erp_orders: Sequence[Mapping[str, Any]],
    compare_fields: Sequence[str],
) -> DiffEntry:
    """6.4 步骤 5-9 对一行 Excel 数据进行差异分类（纯函数，零写）。

    - 主体（客户/供货商）匹配失败（ambiguous/not_found）→
      customer_unmatched，报告表达不阻断（任务书口径）；
    - 匹配键不完整（缺型号/日期）→ key_incomplete，不猜测匹配；
    - 无 ERP 订单命中 → erp_missing（钣金订单标记 create_order_bundle，
      D21 场景 9 执行路径）；
    - 多个候选 → ambiguous，携带全部候选订单 id，绝不自动选；
    - 唯一命中 → 比较映射中存在的字段（步骤 6）→ 有差异 field_diff
      （L843 重量 0 可更新标记；L845 塑粉 update_order 标记），无差异
      matched。
    """
    base = dict(
        row_number=row_number,
        source_file_hash=source_file_hash,
        mapping_version=mapping_version,
    )
    if not subject_outcome.writable or subject_outcome.entity_id is None:
        detail = "客户名称" if entity == "metal_orders" else "供货商名称"
        message = (
            f"{detail} {subject_outcome.ref!r} 无法唯一匹配"
            f"（状态={subject_outcome.status}）；不执行任何写入"
        )
        if subject_outcome.candidates:
            message += f"；候选={list(subject_outcome.candidates)}"
        return DiffEntry(
            classification=CUSTOMER_UNMATCHED,
            match_key=None,
            message=message,
            **base,
        )
    key = match_key_from_row(
        entity=entity, subject_id=subject_outcome.entity_id, values=excel_values
    )
    if key is None:
        return DiffEntry(
            classification=KEY_INCOMPLETE,
            match_key=None,
            message=f"行缺少匹配字段（应含 {ENTITY_MATCH_FIELDS[entity]}），无法与 ERP 订单匹配",
            **base,
        )
    hits = [
        (order, match_key_from_erp(entity=entity, order=order))
        for order in erp_orders
    ]
    candidates = [
        (order, k) for order, k in hits
        if k is not None and k == key
    ]
    if not candidates:
        entry = DiffEntry(
            classification=ERP_MISSING,
            match_key=key,
            message=f"ERP 中无该匹配键订单（{_describe_key(key)}）",
            **base,
        )
        if entity == "metal_orders":
            # 6.4 步骤 7 差异行 + D21 场景 9：缺失钣金订单 → create_order_bundle
            entry = DiffEntry(
                classification=ERP_MISSING,
                match_key=key,
                write_entry=WRITE_CREATE_ORDER_BUNDLE,
                message=f"ERP 中无该匹配键订单（{_describe_key(key)}）；"
                "标记 create_order_bundle（D21 创建执行）",
                **base,
            )
        return entry
    if len(candidates) > 1:
        ids = [c[0].get("id") for c in candidates]
        return DiffEntry(
            classification=AMBIGUOUS,
            match_key=key,
            message=f"匹配歧义：该键命中多个 ERP 订单 {ids}；不自动选择，需人工核对",
            **base,
        )
    order, _ = candidates[0]
    diffs = compare_excel_to_erp(
        entity=entity,
        excel_values=excel_values,
        erp_values=order,
        compare_fields=compare_fields,
    )
    if not diffs:
        return DiffEntry(
            classification=MATCHED,
            match_key=key,
            matched_order_id=order.get("id"),
            message=f"唯一匹配 ERP 订单 #{order.get('id')}，字段一致",
            **base,
        )
    entry = DiffEntry(
        classification=FIELD_DIFF,
        match_key=key,
        matched_order_id=order.get("id"),
        diffs=diffs,
        message=f"唯一匹配 ERP 订单 #{order.get('id')}，存在 {len(diffs)} 处字段差异",
        **base,
    )
    # L843：钣金原重量 0 可更新（zero_weight_updatable）→ update_order 路径
    if any(d.updatable_without_review for d in diffs):
        entry = DiffEntry(
            classification=FIELD_DIFF,
            match_key=key,
            matched_order_id=order.get("id"),
            diffs=diffs,
            write_entry=WRITE_UPDATE_ORDER,
            message=f"唯一匹配 ERP 订单 #{order.get('id')}，存在 {len(diffs)} 处字段差异；"
            "含原重量 0 可更新项（L843）",
            **base,
        )
    # L845：塑粉订单价格/重量/色号差异必须使用 update_order
    elif entity == "powder_orders" and any(
        d.field in POWDER_UPDATE_ORDER_FIELDS for d in diffs
    ):
        entry = DiffEntry(
            classification=FIELD_DIFF,
            match_key=key,
            matched_order_id=order.get("id"),
            diffs=diffs,
            write_entry=WRITE_UPDATE_ORDER,
            message=f"唯一匹配 ERP 订单 #{order.get('id')}，存在 {len(diffs)} 处字段差异；"
            "塑粉价格/重量/色号差异必须走 update_order（L845）",
            **base,
        )
    return entry


def _describe_key(key: MatchKey) -> str:
    subject = "客户" if key.entity == "metal_orders" else "供货商"
    product = "型号" if key.entity == "metal_orders" else "色号"
    return f"{subject}#{key.subject_id} {product}={key.product_ref} 日期={key.in_date}"
