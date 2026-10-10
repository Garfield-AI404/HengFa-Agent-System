"""对话导出字段目录（默认列/关联/计算列）。

- EXPORT_FIELD_ALIASES：可导出实体（QUERYABLE_ENTITIES 共 15 个）的字段
  中文名映射（机械补齐其余 9 个实体
  transactions/transaction_categories/customer_contacts/attendance/
  ar_payments/ap_payments/salary_payments/powder_inventory/
  powder_consumption——字段中文名**照抄** ERP 侧
  hengfa-erp/backend/app/mcp/registry.py 的 _FIELD_LABELS /
  _FIELD_LABEL_OVERRIDES，不自拟；含 transactions.category → 「收支分类」
  （该列名与 file_attachments.category 同名不同义，ERP 侧以表级 override
  区分；本表按实体分字典，天然按表隔离）；订单/客户/供货商/员工五个实体
  既有的中文名来自 domains/erp_field_catalog.py ERP_FIELD_CATALOG 与固定
  映射，保留不动）；
  含系统字段（id/created_by/updated_by/created_at/updated_at/total_amount）
  与关联/计算列（customer_name/supplier_name/total_weight/total_price）的
  中文名——用户显式要求导出系统字段时表头同样全中文；
- EXPORT_DEFAULT_COLUMNS：metal_orders/powder_orders 默认导出列（R4.export.fields
  规则包口径：fields 缺省时的列序，系统字段默认不带）；metal 默认列
  [2026-10-10] = 客户名称/型号/单价/单套重量/数量/总重量/总价格/入库日期
  （入库日期置末、单套重量在数量前）；
- EXPORT_JOIN_FIELDS：关联列定义（customer_name → customers.company_name；
  supplier_name → suppliers.name，均生产 ERP 真实字段）——导出器按 FK
  建名称映射富化；
- EXPORT_COMPUTED_COLUMNS：计算列定义（metal_orders 总重量 = 单套重量 ×
  数量；总价格 = 总重量 × 单价 + 附加款 surcharge_amount——
  与 ERP 行金额口径一致，加法在 export.py _enrich_rows 完成；附加款本身
  非计算列，可直接作显式导出字段）——导出器按来源字段计算富化；
- EXPORT_SUBTOTAL_COLUMNS：总计行只合计的列（metal 总重量/总价格；powder
  只总重量——比率/单价/计数列不合计；新 7 实体逐条
  登记，排掉 id/year/month/customer_id/supplier_id/employee_id 等误求和）；
  多重量单位时总计按单位分桶不跨单位混加（判桶按数据行内 weight_unit 键，
  见 domains/export.py _append_subtotal_rows）；
- header_label(entity, field)：表头中文名（未知字段回退原字段名——导出
  整表落盘，未知字段不阻塞）；
- validate_export_fields(entity, fields, cache)：导出前校验（fail closed）：
  实体必须 ∈ QUERYABLE_ENTITIES；实体必须存在于 list_entities 元数据
  （EntityCache）；fields 为 None → 默认导出列（metal/powder）或元数据全字段
  减系统字段（其余实体），**两种情况统一剔除元数据中 sensitive=True 的字段**
  （真源 = FieldSpec.sensitive，不另立清单）；fields 中任一未知
  字段 → MCPError(VALIDATION_ERROR)——不宽容剔除（与 erp_query 的宽容剔除
  不同：导出结果整表落盘，字段缺失会让用户拿到缺列文件，宁可拒绝）。
  显式 fields 点名敏感字段时**照旧放行**（合法性判定不变：只认「字段在
  元数据里」），由 run_export_data 在结果里附「按显式请求导出敏感字段」说明。
"""

from __future__ import annotations

from typing import Any

from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

#: 实体 → 字段中文名（未知字段回退原字段名，见 header_label）
EXPORT_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "metal_orders": {
        "id": "编号",
        "customer_id": "客户 ID",
        "model": "型号",
        "unit_price": "单价",
        "price_type": "价格类型",
        "status": "状态",
        "weight_per_unit": "单套重量",
        "weight_unit": "重量单位",
        "quantity": "数量",
        "quantity_unit": "数量单位",
        "surcharge_amount": "附加款",
        "in_date": "入库日期",
        "out_date": "出库日期",
        "notes": "备注",
        "total_amount": "总金额",
        "created_by": "创建人",
        "updated_by": "更新人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
        "customer_name": "客户名称",
        "total_weight": "总重量",
        "total_price": "总价格",
    },
    "powder_orders": {
        "id": "编号",
        "supplier_id": "供货商 ID",
        "color_code": "色号",
        "total_weight": "总重量",
        "price_per_kg": "每公斤价格",
        "in_date": "入库日期",
        "notes": "备注",
        "total_amount": "总金额",
        "created_by": "创建人",
        "updated_by": "更新人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
        "supplier_name": "供货商名称",
    },
    "customers": {
        "id": "编号",
        "company_name": "客户名称",
        # name 为 client 已弃用字段（生产 ERP customers 仅有 company_name，
        # mcp-api.md §4.1 声明 name 弃用）——保留本映射仅为兼容 demo 契约的
        # 无触发路径兜底；生产数据不会出现该键，header_label 不会命中。
        "name": "名称",
        "region": "地区",
        "unit_price": "单价",
        "price_type": "价格类型",
        "created_by": "创建人",
        "updated_by": "更新人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "suppliers": {
        "id": "编号",
        "name": "供货商名称",
        "region": "地区",
        "created_by": "创建人",
        "updated_by": "更新人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "employees": {
        "id": "编号",
        "name": "员工姓名",
        # [2026-09-13 R1.salary 取数] 裁剪白名单放行工号/岗位/日工资/每日工时
        # 后，导出表头同步补中文名（逐字取自 ERP _FIELD_LABELS 的 employees
        # 段，见 hengfa-erp/backend/app/mcp/registry.py），否则显式/默认导出
        # 这几列时表头回落成英文字段名。
        "employee_no": "工号",
        "position": "岗位",
        "daily_wage": "日工资",
        "work_hours_per_day": "每日工时",
        "created_by": "创建人",
        "updated_by": "更新人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    # [2026-09-12 只读扩展] 以下 9 个实体的字段中文名照抄 ERP 侧
    # hengfa-erp/backend/app/mcp/registry.py 的 _FIELD_LABELS /
    # _FIELD_LABEL_OVERRIDES（不自拟）。transactions.category 用表级
    # override 的「收支分类」（与 file_attachments.category 同名不同义）；
    # powder_inventory 的 order_total/consumed_total/stock 为 ERP 派生字段
    # （_DERIVED_SPECS 标签），同样登记以免默认导出表头落回英文。
    "transactions": {
        "id": "ID",
        "tx_type": "收支类型",
        "category": "收支分类",
        "project": "项目",
        "amount": "金额",
        "tx_date": "收支日期",
        "notes": "备注",
        "customer_id": "客户ID",
        "supplier_id": "供货商ID",
        "source_type": "来源类型",
        "source_id": "来源ID",
        "created_by": "创建人ID",
        "updated_by": "更新人ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "transaction_categories": {
        "id": "ID",
        "tx_type": "收支类型",
        "category_name": "分类名称",
        "sort_order": "排序号",
        "created_at": "创建时间",
    },
    "customer_contacts": {
        "id": "ID",
        "customer_id": "客户ID",
        "contact_name": "联系人姓名",
        "contact_phone": "联系电话",
        "is_primary": "是否主联系人",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "attendance": {
        "id": "ID",
        "employee_id": "员工ID",
        "att_date": "考勤日期",
        "leave_minutes": "请假分钟数",
        "overtime_minutes": "加班分钟数",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "ar_payments": {
        "id": "ID",
        "customer_id": "客户ID",
        "amount": "金额",
        "pay_date": "收款/付款日期",
        "notes": "备注",
        "created_at": "创建时间",
    },
    "ap_payments": {
        "id": "ID",
        "supplier_id": "供货商ID",
        "amount": "金额",
        "pay_date": "收款/付款日期",
        "notes": "备注",
        "created_at": "创建时间",
    },
    "salary_payments": {
        "id": "ID",
        "employee_id": "员工ID",
        "year": "年份",
        "month": "月份",
        "amount": "金额",
        "paid_at": "发放时间",
        "notes": "备注",
        "created_at": "创建时间",
    },
    "powder_inventory": {
        "id": "ID",
        "color_code": "色号",
        "warn_threshold": "预警阈值",
        "notes": "备注",
        "created_by": "创建人ID",
        "updated_by": "更新人ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
        # 派生字段（ERP _DERIVED_SPECS 标签）
        "order_total": "订单总重量",
        "consumed_total": "消耗总量",
        "stock": "净库存",
    },
    "powder_consumption": {
        "id": "ID",
        "inventory_id": "色号ID",
        "color_code": "色号",
        "consume_date": "消耗日期",
        "quantity": "数量",
        "notes": "备注",
        "created_by": "创建人ID",
        "updated_by": "更新人ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    # [2026-09-16 当期应收口径开放] ar_settlements（应收平账/抹零，只读）：
    # 字段中文名照抄 ERP registry._FIELD_LABELS 的 ar_settlements 段（不自拟）。
    # deleted_by 为 ERP 隐藏字段（不注册），不出现在本表。
    "ar_settlements": {
        "id": "ID",
        "customer_id": "客户ID",
        "outstanding_before": "平账前未结余额",
        "total_amount_before": "平账前订单累计",
        "paid_amount_before": "平账前实收累计",
        "received_amount": "本次实收金额",
        "settled_amount": "抹零额",
        "payment_id": "关联收款ID",
        "settle_date": "平账日期",
        "notes": "备注",
        "created_by": "创建人ID",
        "created_at": "创建时间",
    },
}

#: 默认导出列（R4.export.fields 规则 1：fields 缺省时的列序；系统字段默认
#: 不带，规则 3）。metal_orders：客户名称/型号/单价/单套重量/数量/总重量/
#: 总价格/入库日期——[2026-10-10] 新列序：**入库日期置末、单套重量插在数量
#: 前**（单套重量 = weight_per_unit，与 ERP 前端表单 label 一致；与 unit_price
#: 同为单价/比率类，**不进总计**，见 EXPORT_SUBTOTAL_COLUMNS）。
#: [2026-09-10 P1-10 回退] 默认列不含 weight_unit 输出列——默认列表头不出
#: 「重量单位」；此举**不**影响总计行的跨单位分桶：分桶按**行内数据**
#: （_append_subtotal_rows 读 row.get("weight_unit")），而 total_weight 是
#: 本地计算列、取数走整行，行内恒带 weight_unit 键，与是否作为输出列无关。
#: 用户仍可显式 fields=["weight_unit"] 导出该列，别名保留见
#: EXPORT_FIELD_ALIASES）；powder_orders：供货商名称/色号/总重量/每公斤
#: 价格/入库日期。
EXPORT_DEFAULT_COLUMNS: dict[str, tuple[str, ...]] = {
    "metal_orders": (
        "customer_name",
        "model",
        "unit_price",
        "weight_per_unit",
        "quantity",
        "total_weight",
        "total_price",
        "in_date",
    ),
    "powder_orders": (
        "supplier_name",
        "color_code",
        "total_weight",
        "price_per_kg",
        "in_date",
    ),
}

#: 系统字段/派生金额：默认导出列排除（规则 3；显式 fields 要求时仍可导出）
EXPORT_DEFAULT_EXCLUDED: frozenset[str] = frozenset(
    {"id", "created_by", "updated_by", "created_at", "updated_at", "total_amount"}
)

#: 关联列定义：导出列 → (关联表, 外键字段, 名称字段按优先序)
#: 名称字段只放**生产 ERP 真实字段**（ERP 端严格校验 fields，demo 专属字段
#: 会导致关联查询 fail closed）：customer_name 经 customers 关联取
#: company_name；supplier_name 经 suppliers 关联取 name（规则 2，无回退）。
EXPORT_JOIN_FIELDS: dict[str, dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "metal_orders": {
        "customer_name": ("customers", "customer_id", ("company_name",)),
    },
    "powder_orders": {
        "supplier_name": ("suppliers", "supplier_id", ("name",)),
    },
}

#: 计算列定义：计算列 → 来源字段（按序相乘；任一缺失/不可转数值 → 该列 None）
EXPORT_COMPUTED_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "metal_orders": {
        "total_weight": ("weight_per_unit", "quantity"),
        "total_price": ("weight_per_unit", "quantity", "unit_price"),
    },
}

#: 总计行只合计的列（规则 6：metal 只合计 总重量/总价格；powder 只合计
#: 总重量——比率/单价/计数列不合计）。
#: [2026-09-10 P3] transactions（收支记录）登记 sum_cols={"amount"}：
#: 只合计金额列，**不**把 customer_id/supplier_id 当数值列求和（旧行为
#: 未登记 → sum_cols=None → 全数值列兜底合计，关联 id 被错误求和）；
#: transaction_categories 无金额列 → 显式登记空集（不合计），避免兜底
#: 逻辑把它仅有数值列 id/sort_order 误合计。[2026-09-12 回归修复] 该兜底
#: 路径已删除（None 视为空集），14 实体全部显式登记。
#:
#: [2026-09-12 只读扩展] 新开 7 个只读实体逐条登记口径（不再走 sum_cols=
#: None 的全数值列兜底——兜底会把 id/year/month/customer_id/supplier_id/
#: employee_id 等数值型主键/时间列误求和）：
#: - ar_payments/ap_payments/salary_payments → 只合计 amount；
#: - powder_consumption → 只合计 quantity（消耗数量）；
#: - attendance → 合计 leave_minutes/overtime_minutes（总请假/总加班分钟
#:   数有业务意义；employee_id/att_date 不合计）；
#: - powder_inventory → 显式空集（warn_threshold 预警阈值跨色号求和无
#:   意义；order_total/consumed_total/stock 为派生列、本批不引入跨色号
#:   库存合计口径）；
#: - customer_contacts → 显式空集（仅 id/customer_id/is_primary 数值，
#:   求和无意义）。
#:
#: [2026-09-12 回归修复] customers/suppliers/employees 补显式空集：
#: 三者在只读扩展前**未登记**，走 sum_cols=None → 全数值列兜底合计；而
#: ERP 把 Numeric 序列化成字符串，_is_number 放宽后它们的电话/银行账号/
#: 税号/工号等「非空值全为纯数字串」的列被判数值列并被求和 + 套
#: #,##0.00 → 交付给用户的 Excel 数字错误。现补空集登记后，全部可导出
#: 实体（QUERYABLE_ENTITIES，[2026-09-16] 起 15 个）**全部显式登记**，
#: sum_cols=None 兜底路径彻底
#: 不再可达（domains/export.py 已同步删除该分支）。customers.unit_price
#: （Numeric→字符串）等亦不再被求和。
EXPORT_SUBTOTAL_COLUMNS: dict[str, frozenset[str]] = {
    "metal_orders": frozenset({"total_weight", "total_price"}),
    "powder_orders": frozenset({"total_weight"}),
    "transactions": frozenset({"amount"}),
    "transaction_categories": frozenset(),
    # [2026-09-12 只读扩展] 新 7 实体
    "ar_payments": frozenset({"amount"}),
    "ap_payments": frozenset({"amount"}),
    "salary_payments": frozenset({"amount"}),
    "powder_consumption": frozenset({"quantity"}),
    "attendance": frozenset({"leave_minutes", "overtime_minutes"}),
    "powder_inventory": frozenset(),
    "customer_contacts": frozenset(),
    # [2026-09-12 回归修复] 三个无合计口径实体显式空集（此前未登记 → 兜底）
    "customers": frozenset(),
    "suppliers": frozenset(),
    "employees": frozenset(),
    # [2026-09-16 当期应收口径开放] ar_settlements：只合计两个金额列——
    # 抹零额（settled_amount）与本次实收（received_amount）；三个 *_before
    # 快照列是历史余额、跨行求和无意义，不合计；id/customer_id/payment_id 等
    # 数值型主键/关联键更不合计。
    "ar_settlements": frozenset({"settled_amount", "received_amount"}),
}


def header_label(entity: str, field: str) -> str:
    """表头中文名：EXPORT_FIELD_ALIASES 命中返回中文，未知回退原字段名。"""
    aliases = EXPORT_FIELD_ALIASES.get(entity) or {}
    return aliases.get(field, field)


def _is_sensitive(meta: Any, field: str) -> bool:
    """字段是否被 ERP 元数据标记为敏感（sensitive=True）。

    [2026-09-15] 真源 = agent 侧已解析的 ``FieldSpec.sensitive``
    （mcp/metadata.py）——**不另立清单**：ERP 标了谁这里就排除谁，ERP 新增/
    调整敏感字段时 agent 无需改代码。语义与 calc.py 的敏感拦截一致（同一
    元数据键）。

    - 非元数据原生字段（关联列 customer_name/supplier_name、计算列
      total_weight/total_price）不在 ``meta.fields`` 内 → 一律不敏感
      （False），默认导出列的富化列不受影响；
    - ``meta.fields`` 非 ``dict[str, FieldSpec]``（mapping）→ **显式报错**
      （``TypeError``），**不返回 False**：本函数是默认导出列的敏感过滤器，
      安全过滤器绝不设「静默不排除」的出口。生产 ``EntityMetadata.fields``
      恒为 mapping（mcp/metadata.py），出现非 mapping 只会是调用方接线错误，
      宁可 fail closed 当场炸出来，也不放过一行敏感值。
    """
    fields = getattr(meta, "fields", None)
    if not isinstance(fields, dict):
        raise TypeError(
            "metadata.fields 必须是 mapping[str, FieldSpec]（默认导出列的敏感"
            f"过滤依赖它）；实际得到 {type(fields).__name__}"
        )
    spec = fields.get(field)
    return bool(getattr(spec, "sensitive", False))


def validate_export_fields(
    entity: str,
    fields: list[str] | None,
    cache: Any,
) -> list[str]:
    """导出字段校验（fail closed），返回解析后的字段清单。

    - 实体必须 ∈ QUERYABLE_ENTITIES（9.2 第 2 条白名单，本地常量先于网络）；
    - 实体必须存在于 list_entities 元数据（cache.get_entity 为 None →
      MCP_METADATA_UNKNOWN_ENTITY）；
    - fields 为 None → metal_orders/powder_orders 返回默认导出列
      （EXPORT_DEFAULT_COLUMNS，R4.export.fields 规则 1/3）；其余实体返回
      元数据全字段减去系统字段（EXPORT_DEFAULT_EXCLUDED）；**两种情况都再
      剔除元数据中 sensitive=True 的字段**（_is_sensitive，真源 =
      FieldSpec.sensitive；默认排除敏感字段，显式 fields 仍放行）；
    - fields 非 None → 合法集合 = 元数据字段 ∪ 关联列（customer_name/
      supplier_name 放行，规则 5）∪ 计算列（total_weight/total_price，规则
      2 的本地富化列）；任一未知字段抛 VALIDATION_ERROR（不宽容剔除——
      导出整表落盘，缺列宁可拒绝；与 erp_query 的宽容剔除语义不同）；
      显式要求时允许系统字段（规则 4）。关联/计算列非 ERP 原生字段，导出器
      不会把它们透传 ERP（见 domains/export.py 步骤 2.1）。
    """
    if entity not in QUERYABLE_ENTITIES:
        raise MCPError(
            code=InternalCode.NOT_WHITELISTED.value,
            category=ErrorCategory.LOCAL,
            message=f"Entity {entity!r} is not exportable by this agent.",
            details={"entity": entity},
        )
    meta = cache.get_entity(entity) if cache is not None else None
    if meta is None:
        raise MCPError(
            code=InternalCode.MCP_METADATA_UNKNOWN_ENTITY.value,
            category=ErrorCategory.LOCAL,
            message=f"Entity {entity!r} is not in metadata (list_entities).",
            details={"entity": entity},
        )
    if fields is None:
        default = EXPORT_DEFAULT_COLUMNS.get(entity)
        if default is not None:
            # 已登记默认列的实体（metal/powder）：列序不变，仅剔除元数据中
            # sensitive=True 的字段（真源 FieldSpec.sensitive，不另立清单）。
            return [f for f in default if not _is_sensitive(meta, f)]
        # 其余实体：元数据全字段减系统字段，再剔除敏感字段（默认排除；显式
        # fields 点名时仍照旧放行——见 run_export_data 的显式导出说明 note）。
        return [
            f
            for f in meta.fields
            if f not in EXPORT_DEFAULT_EXCLUDED and not _is_sensitive(meta, f)
        ]
    join_fields = EXPORT_JOIN_FIELDS.get(entity, {})
    computed_fields = EXPORT_COMPUTED_COLUMNS.get(entity, {})
    valid = set(meta.fields) | set(join_fields) | set(computed_fields)
    unknown = [f for f in fields if f not in valid]
    if unknown:
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="Export field(s) not defined by list_entities for this entity.",
            details={"entity": entity, "fields": unknown},
        )
    return list(fields)
