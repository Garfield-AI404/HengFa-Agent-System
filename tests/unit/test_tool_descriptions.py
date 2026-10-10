"""[2026-09-10 审计 S2] 工具描述关键语义快照（防描述静默回退）。

背景：本批重写了 14 条 ``ToolSpec.description``（含参数 schema 的 Field
description），但仓库此前对描述**零断言**——下轮改描述若把本批确立的语义句
改丢，没有任何测试会红。

取舍：**不逐字快照全文**（描述是给模型看的高频演进文案，逐字 pin 会让每次
微调都要改测试）。改为「关键子串 in 描述」——只 pin 本批审计确立、且直接
影响模型决策正确性的语义句：

- erp_query：order_by 降序写法（``-in_date``）与「不填时按实体默认序」、
  limit 上限 200、fields 缺省含义、filter 操作符含 like/between；
  [2026-09-12 收口] 明细展示名称显示硬要求（客户/供货商列必须显示名称，先查
  customers/suppliers 取映射；内部主键 id 不得作表格列/正文输出）；
- erp_calc：一律由本工具计算、不心算；单位不混加口径；
- export_data：实体清单 14 个（含 2026-09-12 新开的 7 个只读实体）；
- list_entities：仅这 14 个实体可被 erp_query/erp_calc 查询；
- order_create：金属按客户主数据 / 塑粉按供货商 + 色号（用户给价优先）；
- 写工具计数为七个（含 mapping_template）。

词元取自当前 ``TOOL_REGISTRY``（description ∪ 各参数字段 description），
描述与参数说明同属注入模型的文本，一并纳入。
"""

from __future__ import annotations

import re

from administrative_agent.agent.model_client import build_args_schema
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.attendance import AttendanceExceptionArgs
from administrative_agent.agent.tools.calc import ErpCalcArgs
from administrative_agent.agent.tools.query import ErpQueryArgs
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

# 实体枚举守护的**真源**：本文件三处「工具描述须枚举哪些实体」的用例一律以
# QUERYABLE_ENTITIES 为准，不再手写元组——手写 14 个的旧判据会让第 15 个
# 实体（ar_settlements）的漂移静默通过（守护测试与真源不同构）。
# 当前**无任何实体被有意豁免**：15 个可查询实体全部应出现在
# export_data / list_entities / erp_query 的模型可见文本里。若将来某实体
# 有意不出现在某工具的描述里，请在此处加**显式命名的豁免集合**并注释理由，
# 不要退回手写白名单。


def _spec_text(name: str) -> str:
    """工具注入模型的完整文案：description ∪ 各参数字段 description。

    参数说明与工具描述同属模型可见文本（tool_args 轮注入），任何一处改丢
    语义都影响决策，故合并成一份快照文本供子串断言。
    """
    spec = TOOL_REGISTRY[name]
    parts = [spec.description]
    schema = spec.args_schema
    model_fields = getattr(schema, "model_fields", None)
    if isinstance(model_fields, dict):
        for field in model_fields.values():
            description = getattr(field, "description", None)
            if isinstance(description, str) and description:
                parts.append(description)
    return "\n".join(parts)


def test_erp_query_description_pins_sort_limit_fields_filter() -> None:
    """erp_query：order_by 降序写法 / 默认序 / limit 上限 200 / fields
    缺省 / filter 操作符（[2026-09-10 S2] 防描述回退）。"""
    text = _spec_text("erp_query")
    # order_by：降序加连字符（-in_date 形式），不填按实体默认序
    assert "-in_date" in text
    assert "不填时按实体默认序" in text
    # [2026-09-10 排序口径] 订单类实体默认按入库日期降序（描述与真实行为一致）
    assert "最近入库在前" in text
    # limit 上限 200
    assert "上限 200" in text
    # fields 缺省含义
    assert "缺省 = 全部字段" in text
    # filter 操作符含 like / between
    assert "like" in text
    assert "between" in text


def test_erp_query_description_pins_name_mapping_requirement() -> None:
    """[2026-09-12 收口] erp_query 描述须含明细名称显示硬要求：查
    metal_orders/powder_orders 展示给用户时，客户/供货商列必须显示名称，
    先查 customers（company_name）/suppliers（name）取映射；内部主键 id
    不得作为表格列/正文输出（仅确已查过且查不到时以「客户ID 16」占位）。
    防该句从描述中被静默删回退（描述是模型最高频阅读文本）。"""
    text = _spec_text("erp_query")
    assert "**查询 metal_orders/powder_orders 明细并要展示给用户时**" in text
    assert "客户/供货商列**必须**显示名称" in text
    assert "先查一次 customers（company_name）/suppliers（name）取映射" in text
    assert "内部主键 id 不得作为表格列或正文数据输出" in text
    assert "「客户ID 16」占位并说明" in text


def test_erp_calc_description_pins_calc_and_unit_no_mixing() -> None:
    """erp_calc：汇总一律由本工具计算、不心算；单位不混加口径。"""
    text = _spec_text("erp_calc")
    assert "一律" in text
    assert "由代码查库计算" in text
    assert "不心算" in text or "不要自己口算或心算" in text
    assert "不同单位不可混加" in text


def test_export_data_description_lists_queryable_entities() -> None:
    """export_data：实体枚举须与真源 QUERYABLE_ENTITIES **完全一致**
    （[2026-09-12] 新开 7 个只读实体；[2026-09-16] 追加 ar_settlements）。

    导出跟随读白名单（domains/export_fields.validate_export_fields 直接以
    QUERYABLE_ENTITIES 判定可导出实体），故 export_data 描述必须枚举全部
    可查询实体，当前无豁免项。
    """
    text = _spec_text("export_data")
    for entity in sorted(QUERYABLE_ENTITIES):
        assert entity in text, f"export_data 描述缺实体：{entity}"


def test_list_entities_description_pins_queryable_entities() -> None:
    """list_entities：可被 erp_query/erp_calc 查询的实体枚举须与真源
    QUERYABLE_ENTITIES **完全一致**（不再手写 14 个）。"""
    text = _spec_text("list_entities")
    assert "可被 erp_query/erp_calc 查询" in text
    for entity in sorted(QUERYABLE_ENTITIES):
        assert entity in text, f"list_entities 描述缺可查实体：{entity}"


def test_erp_query_description_lists_all_queryable_entities() -> None:
    """erp_query：实体枚举须含全部 QUERYABLE_ENTITIES（含 2026-09-12 新开
    7 个只读实体与 2026-09-16 的 ar_settlements），且描述给出
    powder_inventory 净库存的**正确**口径：[2026-09-13 反向钉住]
    powder_inventory 已**直接暴露** stock（净库存）字段，净库存直接查 stock；
    旧文案「查不到净库存 / 须按公式另算」不仅过时，还会主动教模型自算，必须
    不再出现（防描述静默回退到错误口径）。"""
    text = _spec_text("erp_query")
    for entity in sorted(QUERYABLE_ENTITIES):
        assert entity in text, f"erp_query 描述缺实体：{entity}"
    # 新口径：直接查 stock，禁止自算
    assert "直接" in text and "stock" in text  # 净库存直接查 stock 字段
    assert "禁止" in text  # 明令禁止按旧公式自算
    # 旧口径必须清干净（过时 + 主动教模型自算，双刃）
    assert "查不到净库存" not in text
    assert "无净库存字段" not in text
    assert "powder_consumption" in text  # 引用为「禁止」自算公式的实体


def test_erp_query_description_pins_truncated_supplement_and_offset() -> None:
    """[根因 A 全量取数] erp_query 的**截断续查指令**（``tools/__init__.py``
    描述）与 **offset 参数说明**（``ErpQueryArgs.offset``）必须钉住——本批
    新增、此前零覆盖（``grep "必须关掉 fetch_all"`` / ``grep "起始偏移"``
    在 tests/ 下均零命中）。

    语义：结果 truncated: true 表示只返回了前 500 行，要补足剩余行用
    fetch_all=false + offset=<已得行数> 续查，**必须关掉 fetch_all**（否则
    工具会从该偏移继续自动续查并在 500 行上限处再次截断）；export_data 不
    截断但超上限会被拒绝。

    锚点与后果绑在**同一小句**（``[^。；（]*``），不跨「。；（」。本批文案里
    fetch_all / offset / 500 行多处重复，裸子串断言会被邻近同字词救活（既有
    假锁教训）。**两处注入面必须分别钉住**（``spec.description`` 走决策轮、
    ``ErpQueryArgs.offset`` 走参数轮）——只钉合并文本 ``_spec_text`` 会让其中
    一处的漂移被另一处同字词救活（假锁）。把 ``tools/__init__.py`` 里这段续查
    指引整体替换回旧文案、或把 offset 描述改回「一般不需要」、或删掉
    export_data 上限 caveat → 本用例红。
    """
    # ① 工具描述（决策轮注入）：截断续查指令——truncated → fetch_all=false
    #    → offset 三者在同一小句内串联
    desc = TOOL_REGISTRY["erp_query"].description
    assert re.search(
        r"truncated[^。；（]*fetch_all=false[^。；（]*offset", desc
    ), "erp_query 描述缺「truncated → fetch_all=false + offset 续查」同一小句"
    # ② 工具描述：「必须关掉 fetch_all」与其后果（再次截断）绑在同一小句内
    assert re.search(
        r"关掉 fetch_all[^。；（]*截断", desc
    ), "erp_query 描述缺「必须关掉 fetch_all，否则再次截断」的后果句"
    # ③ offset 参数说明（参数轮注入）：存在「起始偏移」且「补足剩余行须传
    #    fetch_all=false」与 offset 绑在同一小句（「须传」是该参数说明独有措辞）
    offset_desc = ErpQueryArgs.model_fields["offset"].description
    assert "起始偏移" in offset_desc
    assert re.search(
        r"要补足剩余行须传 fetch_all=false[^。；（]*offset", offset_desc
    ), "erp_query 的 offset 参数说明缺「补足剩余行须传 fetch_all=false + offset」"
    # ④ P2：export_data 上限 caveat（不截断但超上限会被拒绝，需先缩小 filter）
    assert re.search(
        r"超上限会被拒绝[^。；（]*filter", desc
    ), "erp_query 描述缺「export_data 超上限会被拒绝，需先缩小 filter」的 caveat"


def test_erp_calc_description_lists_all_queryable_entities() -> None:
    """erp_calc：实体枚举须与真源 QUERYABLE_ENTITIES **完全一致**
    （[2026-09-16] 此前 erp_calc 的实体枚举**无任何守护用例**——ToolSpec
    description 与 calc.py ErpCalcArgs.entity 字段说明两处枚举漂移都不会
    报警；本条补齐。erp_calc 的实体枚举既在工具描述里、也在参数 entity
    说明里，``_spec_text`` 合并两处，任一缺失即红）。
    """
    text = _spec_text("erp_calc")
    for entity in sorted(QUERYABLE_ENTITIES):
        assert entity in text, f"erp_calc 描述缺实体：{entity}"


def test_erp_calc_description_pins_id_name_mapping_beyond_orders() -> None:
    """erp_calc：id 桶取名称映射的提示须覆盖新实体（ar_payments.customer_id/
    ap_payments.supplier_id/salary_payments.employee_id 同为裸 id）。"""
    text = _spec_text("erp_calc")
    assert "ar_payments.customer_id" in text
    assert "ap_payments.supplier_id" in text
    assert "salary_payments.employee_id" in text
    assert "employee_id" in text


def test_erp_calc_description_pins_selfcontained_name_columns() -> None:
    """erp_calc：分组条目**已自带名称列**、报告直接用该列、不要再为取名去
    erp_query；仅当条目确无名称列时才自行兜底。

    [2026-09-21] 上一批把本段权威语义**反转**了：旧文案是「分组结果是 id 桶，
    报告前先经 erp_query 取 customers/suppliers/employees 名称映射」，新文案是
    「条目已自带名称列，不要再为取名去 erp_query」。新旧两版**都含**
    ar_payments.customer_id / ap_payments.supplier_id / salary_payments.employee_id
    等裸实体名，故既有的裸子串用例
    （test_erp_calc_description_pins_id_name_mapping_beyond_orders）在反转前/后
    **都绿**——这次语义改动此前无任何用例在守。本用例补这个缺口。

    锚点与后果绑在**同一小句**（`[^。；（]*`），不跨「。；（」。「已自带名称列」
    与「不要再…erp_query」在正文里**隔着一个「。」**，故不得写跨句正则（那样
    在正常文本上即红，正是本批次前车之鉴）。

    反转回旧文案 / 「已自带名称列」改成「不带名称列」 / 删掉「不要再…erp_query」
    → 本用例红（届时旧裸子串用例仍绿，量化旧闸门零覆盖）。
    """
    text = _spec_text("erp_calc")
    # ① 分组条目已自带名称列（新权威：名称由 erp_calc 自身给出，无需另查）
    assert re.search(
        r"分组条目[^。；（]*已自带[^。；（]*名称列", text
    ), "erp_calc 描述缺「分组条目已自带名称列」的新权威句"
    # ② 直接用该名称列报告、不要再为取名去 erp_query
    assert re.search(
        r"用该名称列报告[^。；（]*不要再[^。；（]*erp_query", text
    ), "erp_calc 描述缺「直接用该名称列报告、不要再为取名去 erp_query」"
    # ③ 兜底必须是**条件式**（仅当没有名称列）——防退化成无条件 erp_query
    assert re.search(
        r"仅当[^。；（]*没有名称列", text
    ), "erp_calc 描述缺「仅当条目里没有名称列时才自行兜底」的条件式"


def test_erp_calc_description_pins_divide_by_and_totals() -> None:
    """erp_calc：新增能力须在描述里写明（防描述静默回退）——常量除法
    divide_by（先聚合后除一次，如加班分钟合计÷60，除法由代码完成）与顶层
    totals（有 group_by 且未传 over 时按单位桶分别合计，仅 sum/count 有
    totals；报告合计用 totals；over 存在时权威数字是 normalized.total）。"""
    text = _spec_text("erp_calc")
    assert "divide_by" in text
    assert "常量除法" in text
    assert "overtime_minutes" in text  # 典型用例：分钟 ÷ 60 换算小时
    assert "totals" in text
    assert "按单位桶分别" in text
    assert "normalized.total" in text
    # [2026-09-14] 金额类例外句的 pin：锚点与后果绑在**同一小句**。字符类用
    # `[^。；（]*`（而非 `[^。；]*`）是关键——例外句里紧跟的括号子句「（物理量
    # 如 weight_per_unit 则跨桶合并…）」含「跨桶合并」，若允许跨括号，整段反向
    # （金额分桶、物理量合并）后仍能被该括号子句救活（假锁）。禁止跨「（」后，
    # 后果词必须出现在括号子句之前，才真正绑定在本句金额语义上。后果词用
    # `跨桶合并|跨桶汇总` 交替，容忍同义改写、但仍拒绝整段反向。
    # 本 pin 与上面 `assert "按单位桶分别" in text` **语义相反但共享文本**（前者
    # 是默认口径、本句是例外），二者**并列独立成立**：删/反向例外句 → 本 pin 红、
    # 上面仍绿。
    assert re.search(
        r"金额类字段（total_amount）[^。；（]*(?:跨桶合并|跨桶汇总)", text
    )


def test_erp_calc_description_pins_multi_customer_amount_total() -> None:
    """[2026-09-16] 多客户/多供货商的订单金额合计必须**一次**交给本工具
    （group_by=customer_id + measures=[sum(total_amount)]），禁止逐户
    erp_query 出金额再自己相加。

    锚点（具体取法）与后果（不许自己相加）绑在**同一小句**，字符类用
    `[^。；（]*`——本句紧随的「该次调用只放金额 measure，混入重量/数量会让
    金额也被按单位分桶」那一小句含「相加」类邻近同字词，允许跨句/跨括号
    会让整句反向（改成「逐户查询后自己相加」）仍被救活（假锁）。
    """
    text = _spec_text("erp_calc")
    assert re.search(
        r"group_by=customer_id[^。；（]*sum\(total_amount\)"
        r"[^。；（]*(?:不要|不得|禁止)[^。；（]*(?:自己|自行)[^。；（]*相加",
        text,
    ), "erp_calc 描述缺「多客户金额合计一次交给本工具、不要自己相加」的口径句"
    # 配套限制：金额 measure 不得与重量/数量混call（否则金额被按单位分桶）
    assert re.search(r"只放金额[^。；（]*混入[^。；（]*分桶", text), (
        "erp_calc 描述缺「金额合计那次调用只放金额 measure」的配套限制"
    )


def test_erp_calc_description_pins_time_granularity_for_month_quarter() -> None:
    """C：erp_calc 描述须告知「按月/季汇总用 time_granularity、由代码把日期
    字段截断成桶键」，并劝阻「逐日取数 + 自己手工归并」这一实测致错形态
    （003/023/024 同型：逐日结果搬进上下文手工归并 → 分月全错）。

    锚点与后果绑在**同一小句**（``[^。；（]*``，不跨「。；（」），避开本团队
    假锁老坑；连接词微调仍绿。本批新能力此前零覆盖——旧断言
    （test_erp_calc_description_pins_calc_and_unit_no_mixing 等）在新句被反转
    后仍绿（见交付报告变异证据）。
    """
    desc = TOOL_REGISTRY["erp_calc"].description
    assert "time_granularity" in desc
    assert re.search(r"month/quarter[^。；（]*截断成桶键", desc), (
        "erp_calc 描述缺「month/quarter 把日期字段截断成桶键」的能力句"
    )
    # 极性锁：锚点（逐日取数 + 自己手工归并）必须与**劝阻**词绑在同一小句。
    # 仅钉「逐日…自己手工归并」词形是假锁——把「**不要**按原始日期逐日取数」
    # 反转成「**请照**…逐日取数」（描述变成要求模型做那个实测致错的动作）仍绿。
    # 照本文件 :268-287 既有范式把极性写进正则。
    assert re.search(
        r"(?:不要|不得|禁止)[^。；（]*逐日[^。；（]*自己手工归并", desc
    ), "erp_calc 描述缺「不要逐日取数 + 自己手工归并」的**劝阻**（带极性）"


def test_erp_calc_description_pins_order_by_limit_top_n_example() -> None:
    """D：erp_calc 描述须给出**可照抄的 D 型示例**——按整单重量（计算列
    weight_per_unit×quantity 的别名）降序取前 10 单，并声明 limit 只截断
    items、totals 仍是全量权威合计。锚点与后果绑在同一小句。
    """
    desc = TOOL_REGISTRY["erp_calc"].description
    assert re.search(r'order_by="-整单重量"[^。；（]*limit=10', desc), (
        'erp_calc 描述缺可照抄的 order_by="-整单重量" + limit=10 示例'
    )
    assert re.search(r"limit[^。；（]*只截断 items", desc), (
        "erp_calc 描述缺「limit 只截断 items」的语义句"
    )
    assert re.search(r"totals[^。；（]*全量", desc), (
        "erp_calc 描述缺「totals 仍是全量权威合计」的口径句"
    )


def test_erp_calc_param_descriptions_pin_new_args() -> None:
    """F2：**参数轮**文本（ErpCalcArgs 的三条新 Field description）必须单独钉。

    参数轮（``build_args_schema`` 注入）是模型挑参数时**唯一**读到的业务背景
    （历史根因之一「参数轮无业务背景」）；此前两条守护只读
    ``TOOL_REGISTRY[...].description``（决策轮），参数轮文本一个字都没进闸门——
    删「类型必须 date/否则 fail closed」「别名解析不到 fail closed 回列别名」
    「桶键形如 2026-08」等句全绿。照本文件 ``test_erp_query_description_pins_
    truncated_supplement_and_offset``（:157 注释）「两处注入面必须分别钉住」先例，
    决策轮（上一条用例）与参数轮（本用例）各自独立成闸。
    """
    param_text = _spec_text("erp_calc")
    # time_granularity：桶键格式 + 日期型（date/datetime）+ fail closed
    d = ErpCalcArgs.model_fields["time_granularity"].description
    assert d in param_text  # 确认参数说明真的进了模型可见合并文本
    assert "必须与 group_by 同用" in d
    assert re.search(r'"month"[^。；（]*2026-08', d), (
        "time_granularity 参数说明缺桶键格式（month → \"2026-08\"）"
    )
    # R1：极性锁——单字「不」插进谓词（必须不为 date/datetime / 不拒绝）必须红
    assert re.search(
        r"必须为[^。；（]*date/datetime[^。；（]*(?<!不)(?:fail closed|拒绝)", d
    ), "time_granularity 参数说明缺「类型必须为 date/datetime，否则 fail closed 拒绝」"
    # R2：NULL 子句（空值组）此前零覆盖——删除该句必须红（S3 极性锁）
    assert re.search(r"NULL[^。；（]*(?<!不)归入空值组", d), (
        "time_granularity 参数说明缺「NULL 行归入空值组，不报错」"
    )
    assert re.search(r"(?<!不)显示为 None", d), (
        "time_granularity 参数说明缺「空值组 group_by 显示为 None」"
    )
    # order_by：别名解析不到 → fail closed 并回列可用别名
    o = ErpCalcArgs.model_fields["order_by"].description
    assert o in param_text
    # R1：极性锁——「别名解析不到**不**会 fail closed」必须红
    assert re.search(
        r"别名解析不到(?<!不)会[^。；（]*fail closed[^。；（]*可用别名", o
    ), "order_by 参数说明缺「别名解析不到会 fail closed 并回列可用别名」"
    # R5：tiebreaker 必须写清**单位桶序 → 桶键序**（只说「桶键序」会让模型
    # 以为同值时的确定性来源于时间桶，忽略单位桶这一层）——删除该措辞必须红
    # S3 极性锁：裸子串被「不单位桶序」救活，故把否定写进正则
    assert re.search(r"(?<!不)单位桶序 → 桶键序", o), (
        "order_by 参数说明缺「同值按单位桶序 → 桶键序做 tiebreaker」的确定性口径"
    )
    # limit：只截断 items（极性锁）/ totals 全量（极性锁）
    m = ErpCalcArgs.model_fields["limit"].description
    assert m in param_text
    # R1：极性锁——「不截断 items」必须红
    assert re.search(r"(?<!不)(?:只|仅)截断 items", m), (
        "limit 参数说明缺「只截断 items」——反转成与 totals 同步截断必须红"
    )
    # S3 极性锁：裸子串被「不仍是」救活，故把否定写进正则
    assert re.search(r"totals/normalized[^。；（]*(?<!不)(?:仍是|不受)[^。；（]*全量", m), (
        "limit 参数说明缺「totals/normalized 仍是全量」——反转成同步截断必须红"
    )
    # F5：count 语义（R1 极性锁：说成「count 不是本次返回的条目数」必须红）
    assert re.search(r"count[^。；（]*(?<!不)是本次返回[^。；（]*条目数", m), (
        "limit 参数说明缺「count 是本次返回的条目数」的口径"
    )
    # R3：口径写错——是「条目数」不是「组数」（混单位实体条目数 = 单位桶 × 组）
    # S3 极性锁：裸子串被「不全量条目数见 note」救活
    assert re.search(r"(?<!不)全量条目数见 note", m), (
        "limit 参数说明缺「count 是本次返回条目数，全量条目数见 note」"
    )
    # R2：截断 note 子句此前零覆盖——删除该句必须红（S3 极性锁）
    assert re.search(r"note[^。；（]*(?<!不)(?:不得|不要)[^。；（]*子集之和当总计", m), (
        "limit 参数说明缺「截断时附 note 提醒不得把子集之和当总计报告」"
    )
    # R4：不得引导模型退回 export_data（与「聚合结果不进上下文/用本工具」纪律冲突）
    assert "export_data" not in m, (
        "limit 参数说明不得引导模型改用 export_data（应引导按 filter 分批调用本工具）"
    )
    # S3 极性锁：裸子串被「不分批依次调用本工具」救活
    assert re.search(r"(?<!不)分批依次调用本工具", m), (
        "limit 参数说明缺「加 filter 分批依次调用本工具」的替代引导"
    )
    # F8：混单位 Top-N 跨单位排序（R1 极性锁：说成「不跨单位排序」必须红）
    assert re.search(r"(?<!不)跨单位[^。；（]*排序", m), (
        "limit 参数说明缺「混单位时 Top-N 跨单位排序」"
    )
    assert re.search(r"(?<!不)按单位桶分别", m), (
        "limit 参数说明缺「报告仍按单位桶分别」"
    )
    # F8（半句）：被截断时某单位桶可能整桶消失，须以 totals 核对（S3 极性锁）
    assert re.search(r"被截断时某单位桶[^。；（]*(?<!不)须以 totals", m), (
        "limit 参数说明缺「被截断时某单位桶可能整桶不出现，须以 totals 核对」"
    )


def test_erp_salary_calc_description_pins_core_semantics() -> None:
    """erp_salary_calc：工资一律调本工具、不要自搭流程；禁用 transactions
    「工资」类目/salary_payments 代替；missing 需向用户确认；合计以
    totals.wage 为准。"""
    text = _spec_text("erp_salary_calc")
    assert "一律调用本工具" in text
    assert "不要自己先用 erp_query 查 employees" in text
    assert "transactions" in text and "工资" in text
    assert "salary_payments" in text
    assert "已发生的付款/发放流水" in text
    assert "totals.wage" in text
    assert "missing" in text and "向用户确认" in text


def test_query_catalog_order_matches_queryable_entities() -> None:
    """_QUERY_CATALOG_ORDER 必须与 QUERYABLE_ENTITIES **含顺序**精确一致
    （防两处枚举漂移：目录渲染顺序是模型可见的字段目录，漏一个实体模型就
    看不到其字段；顺序漂移会让目录渲染序意外改变）。

    [2026-09-12 收口] 旧断言只比 frozenset + 长度，仅交换顺序也能通过
    （顺序漂移不被发现）。现显式钉住**期望元组顺序**。
    """
    from administrative_agent.agent.nodes.tool_args_node import _QUERY_CATALOG_ORDER
    from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

    assert _QUERY_CATALOG_ORDER == (
        "customers", "suppliers", "employees", "metal_orders", "powder_orders",
        "transactions", "transaction_categories",
        "customer_contacts", "attendance", "ar_payments", "ap_payments",
        "salary_payments", "powder_inventory", "powder_consumption",
        "ar_settlements",
    )
    assert frozenset(_QUERY_CATALOG_ORDER) == QUERYABLE_ENTITIES
    assert len(_QUERY_CATALOG_ORDER) == len(QUERYABLE_ENTITIES)  # 无重复项


def test_order_create_description_pins_metal_and_powder_price_basis() -> None:
    """order_create：金属按客户主数据 / 塑粉按供货商 + 色号且用户给价优先。"""
    text = _spec_text("order_create")
    # 金属：按客户 ERP 主数据确定，不查历史订单推断
    assert "按该客户" in text
    assert "ERP 主数据" in text
    assert "不查历史订单推断" in text
    # 塑粉：供货商 + 色号历史价口径，用户给价优先
    assert "该供货商 + 该色号" in text
    assert "用户明确提供价格时以用户为准" in text


def test_write_tool_count_is_seven_including_mapping_template() -> None:
    """写工具恰 7 个且含 mapping_template（[2026-09-10 审计 P2-17] 口径）。"""
    write_tools = {
        name for name, spec in TOOL_REGISTRY.items() if spec.is_write
    }
    assert len(write_tools) == 7
    assert "mapping_template" in write_tools
    assert write_tools == {
        "attendance_plan_execute",
        "order_create",
        "order_update",
        "reconciliation_execute",
        "finance_create",
        "finance_update",
        "mapping_template",
    }


# ---------------------------------------------------------------------------
# [2026-09-16 描述瘦身] finance_* 描述与自身规则包分层，互不越界
#
# 背景：这两条描述原文（529/503 字符）逐句复述了**同轮注入**的规则包
# R7.finance.create / R7.finance.update（分类目录、金额两位小数、外键互斥、
# 确认卡机制、部分失败补写——规则包第 1/3/5/8/10/11 与 1/2/3/7/8/9 条均有
# 且更全）。改一处口径要记得改两处，是典型的双真源。
#
# 分层依据（**两侧轮次不同，所以不能简单"删掉一边"**）：
#   * 工具描述注入**决策轮**（可用工具段 + system prompt 末尾 schema 说明 +
#     末条 human 消息，共 3 次），此时规则包**不在**上下文里；
#   * 规则包只在**参数轮**注入（tool_args 的 policy_family），此时描述在。
# 故描述只留「决策轮独有」的信息（用途 / 方向不反问 / 批量一次调用 / 扁平
# 字段简写——决策轮看不到参数 schema），参数口径一律归规则包。
#
# 这三条断言是**负向**的：防的不是"描述被改丢"，而是"口径又被抄回描述"。
# 判据用规则包独有的词形，且只在 ``spec.description`` 上判——参数说明
# （``_spec_text`` 的另一半）里出现同类词是合理的（schema 自文档）。
# ---------------------------------------------------------------------------

#: 规则包正文独有、**不得**出现在 finance_* 描述里的词形（描述已删的口径）
_POLICY_OWNED_TOKENS: tuple[str, ...] = (
    "transaction_categories",  # 分类目录/先查分类（create 第 1 条）
    "两位小数",                 # 金额格式（create 第 3 条）
    "source_type",             # 系统字段禁止（create 第 6 条）
    "写前查询",                 # 更新前按 id 确认存在（update 第 1 条）
)

#: 描述长度上限——防"再抄回去"时只涨不降；当前 243 / 293 字符
_FINANCE_DESC_MAX_CHARS = 340


def test_finance_descriptions_keep_only_decision_round_facts() -> None:
    """决策轮独有的事实必须留在 finance_* 描述里（瘦身不得瘦过头）。

    删掉任一条即红：方向不反问（否则模型会向用户追问方向）、批量一次调用
    （否则同轮多条会被拆成多次工具调用）、扁平字段简写（决策轮看不到参数
    schema，不知道单条可以不套 items）。
    """
    create = TOOL_REGISTRY["finance_create"].description
    update = TOOL_REGISTRY["finance_update"].description
    assert "不得反问方向" in create, "finance_create 描述丢了「方向不反问」"
    assert "items 列表一次调用本工具" in create, "finance_create 描述丢了「批量一次调用」"
    assert "扁平字段" in create, "finance_create 描述丢了「扁平字段简写」"
    assert "不得反问方向" in update, "finance_update 描述丢了「方向不反问」"
    assert "items 列表一次调用本工具" in update, "finance_update 描述丢了「批量一次调用」"
    assert "只改用户明确要求变化的字段" in update, "finance_update 描述丢了「只改指定字段」"


def test_finance_descriptions_do_not_restate_their_policy_pack() -> None:
    """finance_* 描述不得复述自己规则包的口径，也不得篇幅反弹。

    **负向断言**：把口径抄回描述 → 红；描述膨胀超上限 → 红。若确需在描述里
    新增决策轮必需的事实，改 ``_FINANCE_DESC_MAX_CHARS`` 时请一并说明为什么
    该事实在决策轮不可缺、以及它是否已在规则包里。
    """
    for name in ("finance_create", "finance_update"):
        desc = TOOL_REGISTRY[name].description
        for token in _POLICY_OWNED_TOKENS:
            assert token not in desc, (
                f"{name} 描述出现规则包独有口径 {token!r}——该口径在参数轮由"
                f"规则包注入，抄进描述会造成双真源（改一处忘另一处）"
            )
        assert len(desc) <= _FINANCE_DESC_MAX_CHARS, (
            f"{name} 描述 {len(desc)} 字符 > 上限 {_FINANCE_DESC_MAX_CHARS}"
            f"——描述在决策轮注入 1 次（「可用工具」段，2026-09-16 去重后），"
            f"篇幅反弹等于每轮多付"
        )


# ---------------------------------------------------------------------------
# [2026-09-16] 考勤时长参数的单位必须显式（防「540 分钟当 540 小时」落库）
#
# 失效链条（三层单位不一致、且每一层都没有拦截）：
#   ERP 存储 = 分钟（``leave_minutes``，裸 Integer、无上下界）；
#   R1 规则包正文 = 分钟口径（规则 6「9 个小时，也就是 540 分钟」）——它在
#   参数轮可见；而工具参数 ``leave_hours`` = 小时（``hours_to_minutes`` 里 ×60），
#   且原本是**裸 ``float | None``、零说明**。模型读到「540 分钟」把它填进
#   ``leave_hours`` ⇒ ×60 ⇒ 落库 32400 分钟（= 540 小时 ≈ 60 个工作日），
#   两侧校验都只管「>0 且能转整数分钟」，无人发现。
#   （540 本身恰是正确答案的分钟形态，错的只是单位。）
#
# 本组守护的是**参数说明这一层**：必须让模型在填参数时看得见单位。写法纪律同
# test_summary_prompt：按小句切分、锚点与后果同句，且断言取自**真实注入面**
# （``build_args_schema`` 的输出），不手抄字符串。
# ---------------------------------------------------------------------------

_UNIT_CLAUSE_SPLIT = re.compile(r"[。；\n]")


def _unit_clauses_with(text: str, *tokens: str) -> list[str]:
    return [
        c
        for c in _UNIT_CLAUSE_SPLIT.split(text)
        if c.strip() and all(t in c for t in tokens)
    ]


def test_attendance_hours_params_declare_hour_unit() -> None:
    """两个时长参数的说明必须**在同一小句**里写明「小时」与「不是分钟」。"""
    props = AttendanceExceptionArgs.model_json_schema()["properties"]
    for field in ("leave_hours", "overtime_hours"):
        desc = props[field].get("description") or ""
        assert _unit_clauses_with(desc, "小时", "不是分钟"), (
            f"{field} 的参数说明未把「小时」与「不是分钟」写在同一小句——"
            f"模型读不到单位口径（R1 正文只讲分钟）。当前说明：{desc!r}"
        )


def test_attendance_leave_hours_warns_against_filling_minutes() -> None:
    """请假参数必须显式警告**不要把分钟数填进来**（唯一实测致错形态）。"""
    desc = AttendanceExceptionArgs.model_json_schema()["properties"]["leave_hours"][
        "description"
    ]
    assert _unit_clauses_with(desc, "不要填", "分钟"), (
        "leave_hours 说明未警告「不要填分钟数」——540 当 540 小时填的失效面仍在"
    )


def test_attendance_hours_unit_description_reaches_model_visible_text() -> None:
    """**反假锁**：说明必须真的出现在参数轮注入文本里（``build_args_schema``）。

    只在参数模型上写 ``description`` 不等于模型看得见——本仓库已有「双真源」与
    「假锁」两类前例。故经真实注入函数取值：改注入逻辑（如 ``_args_schema_json``
    不再保留字段级 description）时这条要红。
    """
    injected = build_args_schema(TOOL_REGISTRY["attendance_plan_execute"])
    for field in ("leave_hours", "overtime_hours"):
        desc = AttendanceExceptionArgs.model_json_schema()["properties"][field][
            "description"
        ]
        assert desc in injected, (
            f"{field} 的参数说明未出现在参数轮注入文本中——说明写了但模型看不到"
        )


def test_attendance_class_docstring_states_unit_too() -> None:
    """类 docstring（作为「参数说明：」追加）也必须点出单位，与字段说明双落点。"""
    doc = AttendanceExceptionArgs.__doc__ or ""
    assert _unit_clauses_with(doc, "小时", "不是分钟"), (
        "AttendanceExceptionArgs 的类说明丢了单位口径——字段说明与类说明应双落点"
    )

