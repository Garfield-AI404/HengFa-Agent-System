"""实体字段裁剪白名单与裁剪函数。

crop_items 两个消费方共用本模块：
- tools 节点摘要器（tools_node.summarize_tool_result）：erp_query 结果
  进上下文前按实体裁剪（裁剪后全量装入，无字符预算）；
- memory_lookup 工具（memory.py query_id 路径）：存档明细返回前按实体
  裁剪——两处同一口径，裁剪后体积显著下降（30 条钣金订单 ≈12KB → ~5KB）。
"""

from __future__ import annotations

from collections.abc import Container, Sequence
from typing import Any

#: 实体字段裁剪白名单（[2026-08-28 两级数据访问 1a] 字段裁剪）。
#:
#: 取舍说明——查询结果进上下文前按实体丢弃噪音字段，只保留模型分析所需
#: 业务字段：
#: - metal_orders：保留 id/客户/型号/数量/数量单位/单价/价格类型/总额/
#:   附加款/状态/单重/重量单位/入库与出库日期等分析字段；丢弃 notes 与
#:   系统字段（created_by/updated_by/created_at/updated_at/deleted_at）
#:   ——审计类噪音不参与业务分析，裁剪后体积显著下降（150 条钣金订单
#:   ≈60KB 原始 → ~35KB 精简）；
#: - powder_orders：保留 id/供货商/色号/总重量/每公斤单价/总额/入库日期
#:   （ERP 元数据仅暴露这些业务列；用量/数量单位/单价类型/单重/状态/
#:   出货日期不在此实体，勿凭印象补）；
#: - customers：保留 id/名称/计价等业务字段（名称字段以 ERP 字段目录为准：
#:   company_name）；suppliers：仅保留 id/name，**无任何计价字段**（ERP
#:   suppliers 也无 unit_price）；两者均无 region 列；
#: - employees：[2026-08-28 项目负责人批准开放查询；2026-09-13 口径变更]
#:   原只保留 id/name（demo 契约即此二字段）；按 R1.salary.calculation 的取数
#:   需要放行 employee_no（工号）/position（岗位）/daily_wage（日工资）/
#:   work_hours_per_day（每日工时）/is_active（在职标志）——规则包第 2 条
#:   要求先经 erp_query 取员工日薪/每日工时，旧白名单在裁剪阶段把 daily_wage
#:   裁掉，模型取不到值反复重试、财报「工资账款」算不出；is_active 供
#:   erp_salary_calc 的 include_inactive 在职口径过滤用。phone/emergency_phone
#:   （个人联系方式）仍排除；本次只放宽上述五个字段，其余字段仍按原裁剪口径
#:   取舍（本白名单只决定哪些字段进上下文，不是脱敏机制——凭据类脱敏由
#:   audit/redaction.py 独立承担）。
#: - transactions/transaction_categories：[2026-09-02 R7] 收支查询结果裁剪。
#:   transactions 保留 id/收支类型/分类/项目/金额/日期/往来单位 id，丢弃
#:   notes 与系统字段（审计噪音不参与业务分析，与订单口径一致）；
#:   transaction_categories 保留 id/收支类型/分类名/排序（分类名以 ERP
#:   字段目录为准：category_name）。
#: - [2026-09-12 只读扩展] 新开 7 个只读实体同口径登记：只保留分析所需
#:   业务字段，丢弃 notes 与系统字段（created_by/updated_by/created_at/
#:   updated_at）。customer_contacts 保留联系人业务字段；attendance 保留
#:   员工/日期/请假/加班分钟；ar_payments/ap_payments 保留往来单位/金额/
#:   日期；salary_payments 保留员工/年月/金额/发放时间；powder_inventory
#:   保留色号/预警阈值与 ERP 派生库存列（order_total/consumed_total/
#:   stock——净库存是核心业务量）；powder_consumption 保留色号/日期/数量。
#: - [2026-09-16 当期应收口径开放] ar_settlements（应收平账/抹零，只读）：
#:   保留 id/customer_id/settled_amount/received_amount/settle_date；
#:   丢弃 notes、三个 *_before 快照列、payment_id 与系统字段（快照列是历史
#:   余额、不参与 SK.receivables 流程）。
#:
#: 键序即裁剪后字段序（全量明细确定性输出）；未登记实体 → 原样
#: 返回（保守，不误删未登记实体的字段）。
#:
#: [2026-09-15 方案 A；reviewer 口径裁定——勿「顺手补全」本节！]
#: 白名单外字段在 erp_query fields 路径上取不到（crop 只在 tools_node
#: 摘要器与 memory_lookup 两处生效），而 erp_calc 曾能不经裁剪把 group_by
#: 组键值原样带回 summary。目录段（tool_args_node 第三段 / catalog 的
#: filter_only_fields）**刻意不宣传** erp_calc 的 group_by/measure 路径——
#: 该裁定仍然成立且现在更重要：这些字段含个人联系方式/银行账号/税号，而
#: audit/redaction.py 只脱敏 password/secret/token 类**凭据键**，并不覆盖
#: 它们；在提示词里宣传 group_by 等于给模型指一条官方通道（fail-safe 反
#: 方向，宁可少说不指路）。下一位请勿把句「erp_calc 也能用」加回目录。
#: [2026-09-15 现状更新] 敏感字段**不再需要「另立清单」**：真源是 ERP
#: 元数据下发的 `sensitive` 键（ERP 侧 `_SENSITIVE_FIELDS`，registry.py:170-187，
#: 共 12 个字段；agent 侧 mcp/metadata.py 的 FieldSpec.sensitive 已解析）。
#: erp_calc 已在 group_by 与 measure（field 与 multiply_field）上 fail closed
#: （calc.py:1115-1149），count 一并拦截。**filter/order_by 有意保持放行**：
#: 目录第三段与 catalog 的 filter_only_fields 都明文告诉模型这些字段可用于
#: erp_query 的 filter/order_by，封掉会让提示词变成谎话。filter 的值来自调用
#: 方自己给出的条件：等值/范围条件不新增泄露，但「filter={"bank_account":
#: {"like": "6222%"}} + count」这类组合理论上构成枚举 oracle——借计数逐位逼近
#: 出调用方原本不知道的值。**记为已接受的残余风险**（现实可行性低：账号约 19
#: 位、每消息 25 步预算、每次一条工具调用），**不因此封 filter**（封了就与目录
#: 第三段措辞打架）。**将来若要连 filter/order_by 一起封，必须同时改这两处
#: 渲染的措辞**，不能只改 calc。
QUERY_ITEM_WHITELIST: dict[str, tuple[str, ...]] = {
    "metal_orders": (
        "id", "customer_id", "model", "quantity", "quantity_unit",
        "unit_price", "price_type", "weight_per_unit", "weight_unit",
        # [2026-09-08 ERP 附加款] 钣金行金额 = 基础金额 + 附加款，分析口径
        # 需保留 surcharge_amount（塑粉无此字段，powder 白名单不补）
        "total_amount", "surcharge_amount", "status", "in_date", "out_date",
    ),
    "powder_orders": (
        "id", "supplier_id", "color_code", "total_weight", "price_per_kg",
        "total_amount", "in_date",
    ),
    "customers": (
        "id", "company_name", "unit_price", "price_type",
        # [2026-09-08 口径修正] price_unit（元/kg/元/平方）是主数据计价单位，
        # 钣金 weight_unit 由其确定性映射得出（order_rules 5.1），须放行查询
        "price_unit",
    ),
    "suppliers": ("id", "name"),
    # [2026-09-13 R1.salary 取数] 放行工号/岗位/日工资/每日工时（键序即裁剪
    # 后字段序）；phone/emergency_phone 仍排除（个人联系方式不进上下文）
    "employees": (
        "id", "name", "employee_no", "position", "daily_wage",
        "work_hours_per_day", "is_active",
    ),
    "transactions": (
        "id", "tx_type", "category", "project", "amount", "tx_date",
        "customer_id", "supplier_id",
    ),
    "transaction_categories": ("id", "tx_type", "category_name", "sort_order"),
    # [2026-09-12 只读扩展] 新开 7 个只读实体（字段名照 ERP list_entities 元数据）
    "customer_contacts": (
        "id", "customer_id", "contact_name", "contact_phone", "is_primary",
    ),
    "attendance": (
        "id", "employee_id", "att_date", "leave_minutes", "overtime_minutes",
    ),
    "ar_payments": ("id", "customer_id", "amount", "pay_date"),
    "ap_payments": ("id", "supplier_id", "amount", "pay_date"),
    "salary_payments": (
        "id", "employee_id", "year", "month", "amount", "paid_at",
    ),
    "powder_inventory": (
        "id", "color_code", "warn_threshold",
        # ERP 派生列：净库存 = order_total − consumed_total（实时计算）
        "order_total", "consumed_total", "stock",
    ),
    "powder_consumption": (
        "id", "inventory_id", "color_code", "consume_date", "quantity",
    ),
    # [2026-09-16 当期应收口径开放] ar_settlements（应收平账/抹零，只读）：
    # SK.receivables 第 4 步按客户求 Σsettled_amount、第 8 步取顶层 totals，
    # 需要 id（erp_calc 跨页去重强制要求，缺 id 时多行会塌成一行——见
    # ar_settlements 去重塌缩教训）与 customer_id（分组/对齐键）、
    # settled_amount（抹零额）、received_amount（本次实收）、settle_date
    # （filter 用）。**刻意不放** notes / 三个 *_before 快照列
    # （outstanding_before/total_amount_before/paid_amount_before——历史余额、
    # 不参与本流程）/ payment_id / created_by / created_at：精简上下文。
    "ar_settlements": (
        "id", "customer_id", "settled_amount", "received_amount", "settle_date",
    ),
}


def returnable_fields(entity: str) -> tuple[str, ...] | None:
    """该实体「结果可回传」字段（白名单）；未登记实体 → ``None``。

    [2026-09-15 方案 A] 单一真源：结果裁剪白名单只在 ``QUERY_ITEM_WHITELIST``
    定义一份，本函数是它唯一的对外读取口——目录渲染（tool_args_node /
    catalog）、``is_returnable``、``crop_items`` 与 erp_query 字段校验都经
    本函数判定，杜绝「白名单副本」。

    语义与 ``crop_items`` 的 ``if not whitelist: return items`` 严格一致：
    ``None`` = 未登记 = 不裁剪 = 全部字段可回传（保守放行）；登记实体返回
    其白名单字段（可回传的子集）。
    """
    return QUERY_ITEM_WHITELIST.get(entity or "")


def is_returnable(entity: str, field: str) -> bool:
    """``field`` 的值是否会被回传（未登记实体恒 ``True``：不裁剪 = 全部可回传）。

    与 ``returnable_fields`` 同一口径：``None``（未登记）保守放行为可回传。
    """
    whitelist = returnable_fields(entity)
    if not whitelist:
        return True
    return field in whitelist


def partition_catalog_fields(
    entity: str,
    field_names: Sequence[str],
    computed_names: Container[str],
) -> tuple[list[str], list[str], list[str]]:
    """目录字段三段划分（tool_args_node 与 catalog 两处渲染**同口径**，防漂移）。

    返回 ``(regular, computed_ok, filter_only)``：

    - ``regular``：非计算列且**可回传** → 既可写 ``fields`` 取回，也可
      filter/order_by；
    - ``computed_ok``：计算列且**可回传** → 只能写 ``fields`` 取回，不可
      erp_query 的 filter/order_by（ERP 拒绝）；
    - ``filter_only``：非计算列且**不可回传** → 可用于 erp_query 的
      filter/order_by，但 erp_query 结果行**取不到其值**，勿写进 ``fields``。
      注：**其中被 ERP 标为 sensitive 的字段**在 erp_calc 的 group_by/measure
      路径上**已被 fail closed**（calc.py，真源 FieldSpec.sensitive）；而未标
      敏感的仅筛选字段（如 customers.address）在 calc 的 group_by/measure 上
      **仍可用**。目录**刻意不宣传**该路径——见本模块顶部 ``QUERY_ITEM_WHITELIST``
      注释块（敏感字段含个人联系方式/银行账号/税号，redaction 不覆盖）。

    未登记实体（``returnable_fields`` → ``None``）→ ``filter_only`` 为空、
    不做任何标记：``regular`` / ``computed_ok`` 原样含全部字段（与今天一致）。

    「三不」字段（既不可回传、又是计算列 → filter 与 fields 都不行）**不进入
    任何一段**（列出来只会诱导模型写进 fields 白烧一轮）。

    判定由 ``is_returnable`` 单点驱动（未登记实体恒 True → filter_only 自然为
    空、不加标记），与 ``crop_items`` / erp_query 字段校验同一真源。
    """
    regular: list[str] = []
    computed_ok: list[str] = []
    filter_only: list[str] = []
    for name in field_names:
        if is_returnable(entity, name):
            (computed_ok if name in computed_names else regular).append(name)
        elif name in computed_names:
            # 三不字段（计算列 ∩ 不可回传）→ 丢弃，不入任何段
            continue
        else:
            filter_only.append(name)
    return regular, computed_ok, filter_only


def crop_items(items: list, entity: str | None) -> list:
    """按实体白名单裁剪每条明细（未登记实体 → 原样返回，保守）。

    [2026-08-28 两级数据访问 1a] 字段裁剪：只保留分析所需业务字段，
    丢弃 notes 与系统字段等噪音；键序 = 白名单序（确定性输出）。

    [2026-08-28 D3 修复] 非 dict 元素（防御：`key in item` 对非映射抛
    TypeError）→ 原样返回（不裁剪不丢弃），裁剪仅作用于 dict。
    """
    whitelist = returnable_fields(entity or "")
    if not whitelist:
        return items

    def _crop(item: Any) -> Any:
        if not isinstance(item, dict):
            return item  # 非 dict 元素原样返回（不裁剪不丢弃）
        return {key: item[key] for key in whitelist if key in item}

    return [_crop(item) for item in items]
