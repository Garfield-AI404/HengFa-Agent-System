"""tool_args 节点：参数轮二次模型调用。

模型① 选定工具名（model 节点，``pending_action.tool_name``）后，本节点
注入该工具完整参数 schema + 规则包族正文 + 激活流程编排段
（``state["active_skill"]``），二次模型调用生成参数：

- 成功 → 填充 ``pending_action.tool_arguments``（保留其余字段）→ tools
  节点零改动继续（分派执行）；
- 失败（未知工具/生成失败/工具名不一致）→ 错误 ToolMessage +
  ``pending_action`` 清空 → 回 model 自纠（循环不终止，fail closed）。

结构与风格与 tools_node.py 完全同构（错误分支均发 ``agent_error``
事件；unknown_tool 分支与 tools_node 同文案）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import ToolMessage

from administrative_agent.agent.configuration import (
    RuntimeBag,
    _emit,
    _new_tool_call_id,
)
from administrative_agent.agent.model_client import (
    ModelCallError,
    _args_schema_json,
    build_args_schema,
)
from administrative_agent.agent.nodes.model_node import (
    _filter_stale_details,
    _render_skill_body,
    _truncate_history,
)
from administrative_agent.agent.prompts.base import base_system_prompt_for
from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.prompts.skills import skill_for
from administrative_agent.agent.state import ConversationState
from administrative_agent.agent.tools.calc import _DATE_FIELD_TYPES
from administrative_agent.agent.tools.crop import partition_catalog_fields
from administrative_agent.domains.finance import CATEGORY_FIELD
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_ERROR,
    EVENT_ARGS_INPUT_SUMMARY,
    EVENT_TOOL_RUNNING,
)

#: erp_query 字段目录渲染顺序（固定；必须与 QUERYABLE_ENTITIES 一致——
#: [2026-09-02 R7] 追加 transactions/transaction_categories，收支写先查后写前提；
#: [2026-09-12 只读扩展] 追加 7 个只读实体，契约见 mcp/scopes.py 同名注释；
#: [2026-09-16 当期应收口径开放] 追加 ar_settlements（应收平账/抹零，只读））
_QUERY_CATALOG_ORDER = (
    "customers", "suppliers", "employees", "metal_orders", "powder_orders",
    "transactions", "transaction_categories",
    "customer_contacts", "attendance", "ar_payments", "ap_payments",
    "salary_payments", "powder_inventory", "powder_consumption",
    "ar_settlements",
)

#: 收支分类目录固定组序（必须与 FinanceCreateArgs.tx_type 枚举 / R7 规则包
#: 方向判定口径一致：收入/支出/投资款）
_FINANCE_CATEGORY_GROUPS: tuple[str, ...] = ("收入", "支出", "投资款")


def _render_catalog_field(field: str, fields: dict[str, Any]) -> str:
    """单个目录字段的渲染：``列名（中文字段 label）``（与 list_entities 口径一致）。

    - 英文列名在前、保持醒目可直接复制——模型要拿它写 ``fields``/``filter``/
      ``measures`` 参数；
    - 中文 label 取 ``FieldSpec.label``（ERP 侧下发，mcp-api.md §3）；
    - **label 缺失、为空或与列名相同时只渲染列名**：ERP 侧 label 缺省回退为
      列名本身（registry._FIELD_LABELS 无命中即回退，如 deleted_at），此时
      ``in_date（in_date）`` 纯属噪音。

    [2026-09-22] **日期型字段在括号内追加原始类型 token**（``列名（label·date）``
    / 无 label 时 ``列名（date）``）：``erp_calc`` 的 ``time_granularity``（按月/
    季汇总）要求 ``group_by`` 是日期型，否则 fail closed 拒绝；模型此前在参数轮
    **看不出哪个字段是日期型**，只能挑一个试、被拒后读报错重试，白烧一轮。这里
    把类型直接摆在参数轮眼前。要点：

    - **只标日期型**（``FieldSpec.type ∈ {"date", "datetime"}``，与 calc.py 的
      4.0 段守卫 ``_DATE_FIELD_TYPES`` 同一份元数据、同一词汇）——其余字段渲染
      逐字节不变（给所有字段加类型后缀会让 token 成本高一量级，且无收益）；
    - token 用**元数据原样类型串**（``date`` / ``datetime``），与 calc.py
      报错里的词汇一致，模型能直接对上号，不另造类型映射；
    - 判定源与 calc.py 一致：``fields[field].type``（同一 EntityCache 元数据）。
    """
    spec = fields.get(field)
    label = getattr(spec, "label", "") if spec is not None else ""
    ftype = getattr(spec, "type", "") if spec is not None else ""
    # 日期型标记后缀：仅日期型字段拿到非空串（meta 原样类型 token）。
    date_suffix = f"·{ftype}" if ftype in _DATE_FIELD_TYPES else ""
    if label and label != field:
        return f"{field}（{label}{date_suffix}）"
    if date_suffix:
        return f"{field}（{ftype}）"
    return field


def _render_catalog_fields(fields: dict[str, Any], field_names: Iterable[str]) -> str:
    """字段清单渲染（``_erp_query_catalog_section`` 与 ``_erp_calc_catalog_section``
    **共用的唯一渲染口径**——三段同形，防两段漂移）。"""
    return ", ".join(_render_catalog_field(f, fields) for f in field_names)


def _erp_query_catalog_section(cache: Any) -> str | None:
    """erp_query 字段目录段：EntityCache 驱动，字段名动态渲染不硬编码。

    字段名/顺序直接取自实体元数据（``entity.fields`` dict 保序）；每字段渲染
    成 ``列名（中文字段 label）``（与 list_entities 路径同口径，label 缺失/与
    列名相同则只渲染列名）；实体名后括号里是 ``entity.label``（实体中文名）。
    缓存未接线/获取失败/无实体数据 → 返回 None（跳过该段，不报错不炸参数轮）。
    """
    try:
        entities = cache.get_entities() if cache is not None else []
    except Exception:
        return None
    by_name = {e.name: e for e in entities}
    lines: list[str] = []
    computed_lines: list[str] = []
    filter_only_lines: list[str] = []
    for name in _QUERY_CATALOG_ORDER:
        if name not in QUERYABLE_ENTITIES:
            continue
        entity = by_name.get(name)
        if entity is None:
            continue
        label = entity.label or name
        # [computed 契约] 普通字段与计算列分开渲染：计算列不能进 filter/order_by
        # （ERP 拒绝），若混进「必须取自下列列表」会**教**模型拿计算列去 filter。
        # [2026-09-15 方案 A] 再把「非计算列 ∩ 结果裁剪白名单外」分出第三段——
        # 这些字段 ERP 里有、可 filter/order_by，但 erp_query 结果**不回传取值**
        # （crop_items 会裁掉），模型写进 fields 只会白烧一轮；故单列「仅筛选字段」
        # 段明标「但取值不回传，勿写进 fields」。三段划分由 crop.partition_catalog_fields
        # 统一（与 catalog.py 同口径）：「三不」字段（计算列 ∩ 白名单外）不入任何段。
        computed = {f for f, spec in entity.fields.items() if spec.computed}
        regular, computed_ok, filter_only = partition_catalog_fields(
            name, list(entity.fields), computed
        )
        lines.append(
            f"{name}（{label}）：{_render_catalog_fields(entity.fields, regular)}"
        )
        if computed_ok:
            computed_lines.append(
                f"{name}：{_render_catalog_fields(entity.fields, computed_ok)}"
            )
        if filter_only:
            filter_only_lines.append(
                f"{name}：{_render_catalog_fields(entity.fields, filter_only)}"
            )
    if not lines:
        return None
    section = (
        "实体字段目录（erp_query 的 fields 字段名必须取自下列列表，不得编造"
        + ("；filter/order_by 除下列字段外还可用下方「仅筛选字段」段" if filter_only_lines else "")
        + "）：\n"
        + "\n".join(lines)
    )
    if computed_lines:
        # [2026-09-12 措辞收口] 只限定 **erp_query** 的 filter/order_by——
        # 计算列下推 ERP 的筛选/排序会被 ERP 硬拒；但 erp_calc 的
        # group_by/measure 是**本地**按取回行计算、不下推 ERP，计算列
        # 可用（见 tools/calc.py step 5 注释）。旧措辞泛化成「不可用于
        # filter/order_by」会让模型白白少用 erp_calc 这条可用路径。
        section += (
            "\n\n计算列（实时派生字段：ERP 侧不落库、不能下推——"
            "**不可用于 erp_query 的 filter/order_by**（ERP 会拒绝计算列的"
            "筛选/排序）；需要取数时用 fields 取回。注意：erp_calc 的 "
            "group_by/measure 是本地按取回行计算、不下推 ERP，**可以**用"
            "下列计算列；勿拿它们做 erp_query 的筛选或排序）：\n"
            + "\n".join(computed_lines)
        )
    if filter_only_lines:
        # [2026-09-15 方案 A；reviewer 口径裁定] 第三段：白名单外字段（元数据
        # 里有、取值不随 erp_query 结果回传）。措辞只点三件事：① 取值不回传；
        # ② 勿写进 fields；③ 仍可用于 erp_query 的 filter/order_by。
        # **刻意不提 erp_calc 的 group_by/measure**：敏感字段在该路径上已
        # fail closed（calc.py:1115-1149），但即便如此也不宣传——这些字段含
        # 个人联系方式/银行账号/税号，audit/redaction 只脱敏凭据键、不覆盖
        # 它们；在提示词里指路只会诱使模型去试一条被拦的通道（fail-safe 反
        # 方向，宁少说不指路）。filter/order_by 有意保持放行——封掉会让本段
        # 「可用于 erp_query 的 filter/order_by」变成谎话。详见 crop.py 注释。
        section += (
            "\n\n仅筛选字段（ERP 中存在、可用于 erp_query 的 filter/order_by，"
            "但 **erp_query 结果不回传其取值**，**勿写进 fields**）：\n"
            + "\n".join(filter_only_lines)
        )
    return section


def _erp_calc_catalog_section(cache: Any) -> str | None:
    """erp_calc 字段目录段：EntityCache 驱动，与 erp_query 段**同源同集合**。

    信息源与 ``_erp_query_catalog_section`` 完全一致——同一份 EntityCache、
    同一个实体顺序 ``_QUERY_CATALOG_ORDER``、同一个三段划分
    ``crop.partition_catalog_fields``（故两段覆盖的实体与字段集合必然逐一致，
    防双真源漂移）。**只有三段措辞按 erp_calc 的参数面重写**：

    - 第一段普通字段：用于 ``filter`` / ``group_by`` / ``measures``（含 measure
      的 ``field`` 与 ``multiply_field``）；
    - 第二段计算列：**不可用于 filter**（ERP 侧拒绝计算列下推筛选；calc 本地
      也会剔除并回一条 note），但**可用于 group_by/measures**（calc 的这两者
      在本地按取回行计算、不下推 ERP）——**方向与 erp_query 段相反**，照抄
      erp_query 的「需要取数时用 fields 取回」会把模型从一条可用路径上吓走；
    - 第三段仅筛选字段：只点明「可用于 filter 条件」。**刻意不提**
      group_by/measure——这些字段含个人联系方式/银行账号/税号，在该路径上已
      fail closed，在提示词里指路只会诱使模型去试一条被拦的通道（裁定原文见
      ``agent/tools/crop.py`` 顶部注释块与 erp_query 段第三段注释）；也**不说**
      「取值不回传」——那是 erp_query 的 crop 事实，calc 取数不经 crop，照抄
      即说谎。

    降级与 erp_query 段同款：``cache`` 为 None / ``get_entities()`` 抛异常 /
    无实体数据 → 返回 None（跳过该段，不炸参数轮）。字段名/顺序全部取自
    ``entity.fields``（保序）、每字段渲染成 ``列名（中文字段 label）``（与
    erp_query 段共用 ``_render_catalog_fields``，label 缺失/与列名相同则只渲染
    列名）、实体名后括号里是 ``entity.label``，不硬编码任何字段名或实体名。
    """
    try:
        entities = cache.get_entities() if cache is not None else []
    except Exception:
        return None
    by_name = {e.name: e for e in entities}
    lines: list[str] = []
    computed_lines: list[str] = []
    filter_only_lines: list[str] = []
    for name in _QUERY_CATALOG_ORDER:
        if name not in QUERYABLE_ENTITIES:
            continue
        entity = by_name.get(name)
        if entity is None:
            continue
        label = entity.label or name
        # 三段划分与 erp_query 段同一真源（crop.partition_catalog_fields）：
        # 集合必须一致，只有下方措辞不同。
        computed = {f for f, spec in entity.fields.items() if spec.computed}
        regular, computed_ok, filter_only = partition_catalog_fields(
            name, list(entity.fields), computed
        )
        lines.append(
            f"{name}（{label}）：{_render_catalog_fields(entity.fields, regular)}"
        )
        if computed_ok:
            computed_lines.append(
                f"{name}：{_render_catalog_fields(entity.fields, computed_ok)}"
            )
        if filter_only:
            filter_only_lines.append(
                f"{name}：{_render_catalog_fields(entity.fields, filter_only)}"
            )
    if not lines:
        return None
    section = (
        "实体字段目录（erp_calc 的 filter/group_by/measures 字段名必须取自下列"
        "列表（measures 的 field 与 multiply_field 亦然），不得编造"
        + ("；filter 除下列字段外还可用下方「仅筛选字段」段" if filter_only_lines else "")
        + "）：\n"
        + "\n".join(lines)
    )
    if computed_lines:
        # [与 erp_query 段相反] erp_query 段说「不可用于 filter/order_by，需要
        # 取数时用 fields 取回」；erp_calc 的 group_by/measures 是**本地**按
        # 取回行计算、不下推 ERP，计算列在那两条路径上**可用**——故这里只说
        # 「不可用于 filter」，并显式放行 group_by/measures（见 calc.py step 5
        # 与 step 4 的 known 口径注释）。
        section += (
            "\n\n计算列（实时派生字段：ERP 侧不落库、不能下推——"
            "**不可用于 erp_calc 的 filter**（ERP 会拒绝计算列的筛选）；"
            "但 **可用于 group_by/measures**（erp_calc 在本地按取回行计算、"
            "不下推 ERP，把计算列作为分组字段或聚合对象即可）：\n"
            + "\n".join(computed_lines)
        )
    if filter_only_lines:
        # 第三段：白名单外字段（元数据里有、erp_query 结果不回传其取值）。
        # erp_calc 取数不过 crop，本段对 calc 只承诺「可用于 filter 条件」——
        # **刻意不提** group_by/measure（敏感字段在该路径已 fail closed，不指路；
        # 裁定见 crop.py 顶部与 erp_query 段第三段注释），也**不说**「不回传」
        # （那是 erp_query 专属事实，照抄即说谎）。措辞里连 "erp_calc" 都不出现
        # ——避免把模型引向本段字段之上的其他用途。
        section += (
            "\n\n仅筛选字段（ERP 中存在、可用于本工具的 filter 条件）：\n"
            + "\n".join(filter_only_lines)
        )
    return section


def _finance_category_catalog_section(client: Any) -> str | None:
    """收支分类目录段：transaction_categories **数据行**动态渲染。

    [2026-09-02 R7 目录注入] 与 erp_query 字段目录段（EntityCache 元数据，
    runtime.metadata）的本质区别：分类清单是**数据行**，EntityCache 不存
    数据行——必须经 MCP client 真查一次 transaction_categories（与
    tools/finance.py prepare / domains/finance.py 复查同表同口径；category
    的合法取值就是 category_name 行内容，字段名集中绑定 CATEGORY_FIELD）。

    - 按 tx_type 分组、固定组序渲染（收入/支出/投资款）——让参数轮在给定
      清单内检索选择（仿 erp_query 字段目录先例），而不是靠记忆猜分类；
    - [15.14 混淆修复] 段头说明 tx_type 取值词（收入/支出/投资款）与
      category 分类名的区别、行首渲染 tx_type={组} → 分类清单、段尾点出
      「投资款」类型词/分类名同名陷阱——行首组词与清单仍动态来自查询数据，
      静态说明文字与运行时真实分类保持一致；
    - client 未接线 / 查询失败 / 无数据 → 返回 None：该段降级跳过，不炸
      参数轮，模型按既有规则自行 erp_query 查分类兜底；工具层 prepare
      强校验与确认卡防线不受影响（本段只是提示，不是新的拦截点）。
    """
    if client is None:
        return None
    groups: dict[str, list[str]] = {}
    for group in _FINANCE_CATEGORY_GROUPS:
        try:
            result = client.call_tool(
                "query",
                {
                    "entity": "transaction_categories",
                    "fields": [CATEGORY_FIELD],
                    "filter": {"tx_type": group},
                    "limit": 200,
                },
            )
            if not result.ok:
                return None
            names = [
                str(r.get(CATEGORY_FIELD))
                for r in (result.data.get("items") or [])
                if r.get(CATEGORY_FIELD) is not None
            ]
        except Exception:
            return None
        if names:
            groups[group] = names
    if not groups:
        return None
    lines = [
        f"tx_type={group} → {'、'.join(names)}"
        for group in _FINANCE_CATEGORY_GROUPS
        if group in groups
    ]
    # [15.14 混淆修复] 段头/段尾把「tx_type 取值词」与「category 分类名」
    # 双重身份显式拆开（用户实测 tx_type=支出 却选「投资款」——该分类名与
    # 类型词同名是混淆诱因）；清单内容仍运行时查询渲染，不硬编码。
    return (
        "收支分类目录（category 字段的合法取值，按 tx_type 归属分组。行首的"
        "「tx_type=收入」等是 tx_type 字段的取值——tx_type 的取值只有"
        " 收入/支出/投资款 三个，它们本身不是分类名；category 必须从右侧"
        "「→」后、与所填 tx_type 同组的清单中选一个分类名，禁止跨组选：\n"
        + "\n".join(lines)
        + "\n注意：「投资款」既是 tx_type 的一个取值，也恰好是 tx_type=投资款"
        " 组当前清单中的分类名——只有当所填 tx_type=投资款 时才能选它；所填"
        " tx_type=支出 时，应从上方支出行右侧清单选（运费、水费等）。）"
    )


def tool_args_node(state: ConversationState, runtime: RuntimeBag) -> dict[str, Any]:
    """tool_args 节点：注入工具 schema + 规则包族正文，生成 tool_arguments。

    fail closed 分支（与 tools_node 同构：错误 ToolMessage +
    pending_action=None + agent_error 事件）：
    - 未知工具（spec 缺失，模型幻觉工具名安全网拦截，不落 ERP）；
    - ``ModelCallError``（参数生成调用失败/未配置）；
    - ``result.tool_name != tool_name``（一致性检查：参数轮不得改名）。
    成功：pending_action 是 last-write-wins 整表返回，必须
    ``{**pending, "tool_arguments": ...}`` 保留其余字段。
    """
    pending = state.get("pending_action") or {}
    tool_name = pending.get("tool_name")
    spec = (runtime.tools or {}).get(tool_name) if tool_name else None

    if spec is None:
        content = json.dumps(
            {"ok": False, "error": f"未知工具 {tool_name!r}："
             "不执行任何写入（模型幻觉工具名不落 ERP，安全网拦截）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "unknown_tool"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_unknown",
        }

    # 参数收集阶段事件（前端 SSE 展示；tools 节点执行阶段会再发 tool_running）
    _emit(runtime, EVENT_TOOL_RUNNING, {"tool_name": tool_name, "stage": "collecting_arguments"})

    # ---- 参数轮 system prompt：基础十一条 + 工具卡 + 规则包族 + 结构化说明 ----
    # [2026-10-10 条件注入] 与决策轮同源：RAG 未配置时基础首段摘除长期记忆
    # 从句（判据 ``rag_client is None``，与工具剔除一致；参数轮不注入查证规则段）。
    schema_instruction = build_args_schema(spec)
    parts: list[str] = [base_system_prompt_for(runtime.rag_client is not None)]
    tool_card = f"本轮工具：{spec.name}（{spec.description}）"
    tool_card += f"\n参数 JSON Schema：{_args_schema_json(spec)}"
    doc = getattr(spec.args_schema, "__doc__", None)
    if doc:
        tool_card += f"\n{doc}"
    parts.append(tool_card)
    if spec.policy_family:
        family_lines: list[str] = []
        for key in spec.policy_family:
            package = policy_for(key)
            family_lines.append(f"{key}（{package.title}）：\n{package.text}")
        parts.append(
            f"已加载规则包（{spec.name} 的业务口径，参数必须符合）：\n"
            + "\n".join(family_lines)
        )
    # 目录段位置与规格一致：规则包族正文之后、结构化输出说明（schema）
    # 之前。erp_query / erp_calc / finance_* 是互斥工具名（工具注册表单一
    # name），用 if/elif 保证这些段不会同时注入。
    if spec.name == "erp_query":
        # 实体字段目录：让参数轮能看到真实字段名（模型② 曾猜出 customers
        # 不存在的 fields.name → ERP UNKNOWN_FIELD → 整轮重跑），字段列表
        # 动态来自 EntityCache，缓存缺失时该段降级跳过。
        catalog = _erp_query_catalog_section(runtime.metadata)
        if catalog:
            parts.append(catalog)
    elif spec.name == "erp_calc":
        # erp_calc 字段目录（[2026-09-16] 与 erp_query 段同源同集合，措辞按其
        # 参数面重写）：模型写 filter/group_by/measures 时直接看到真实字段名，
        # 不再凭记忆猜、也不必先花一次工具往返调 list_entities（description 里
        # 的「先调 list_entities」本批保留，作为纵深兜底）。缓存缺失 → 该段
        # 降级跳过（None），不炸参数轮。
        catalog = _erp_calc_catalog_section(runtime.metadata)
        if catalog:
            parts.append(catalog)
    elif spec.name in ("finance_create", "finance_update"):
        # 收支分类目录（[2026-09-02 R7 目录注入，用户实测反馈]）：用户口语
        # 提收支时模型从记忆猜方向/分类随机性强（「花了 X 元」分辨不出
        # 支出/收入）——把 transaction_categories 数据行清单注入参数轮，让
        # 模型在给定清单内检索选择（仿 erp_query 字段目录先例），方向判定
        # 引导见规则包第 2 条（口语开销/收款语义→支出/收入）。查询失败/
        # 无 client → 该段降级跳过（None），不炸参数轮。
        catalog = _finance_category_catalog_section(runtime.mcp_client)
        if catalog:
            parts.append(catalog)

    # [2026-09-13 skill 层接入 · reviewer 收口] 激活流程正文（`active_skill`
    # 非空**且本轮工具 ∈ 该 skill 的 `tools`**）。位置：工具数据段（规则包族 /
    # 字段与分类目录）之后、结构化输出说明之前——让模型在填参前最后看到
    # "这一刻在走流程的哪一步、这一步要什么口径"。
    #
    # 两条条件都不是装饰：
    # - **只给编排段**（``include_policies=False``）：口径这里由上面的
    #   policy_family 注入，而工具族与 skill 引用的规则包会重叠——erp_query
    #   的族就是 R4.query，财报 skill 也引用它；全量复用会把 R4.query 正文在
    #   同一 prompt 里注入两遍、并给出两处"口径以此为准"。
    # - **工具隶属门**：`active_skill` 是 last-write-wins，只在模型主动置 null
    #   时才退出，且清空**下一轮**才生效。所以存在"用户已从财报转向写操作、
    #   模型本轮忘了置 null"的窗口——此时若本轮工具是 `finance_create` 这类
    #   表外写工具，无条件注入就会让同一条 prompt 里既有财报正文的"本流程全程
    #   只读，不写任何数据"，又有填 `finance_create` 参数的要求，自相矛盾。
    #   加了门则以"本轮工具 ∈ 本流程声明的工具"为准，矛盾窗口自然消失。
    #   （曾考虑无条件注入有"轻度纠偏"价值而不加门，不成立：纠偏机会在**决策
    #   轮**——工具就是在那里选出来的，那时流程正文已经摆在模型眼前；参数轮
    #   改不了工具名，只剩矛盾。）
    #
    # 参数轮不改工具名（一致性检查在下方），故命中时 skill 段只影响**参数**的
    # 形状（如按第 4 步取"类目属于某九个值之一"该用 in 而非 eq）。skill_for 对
    # None/未注册键返回 None，与决策轮同口径退化为常规参数生成。
    active_skill = skill_for(state.get("active_skill"))
    if active_skill is not None and tool_name in active_skill.tools:
        parts.append(_render_skill_body(active_skill, include_policies=False))

    parts.append(f"结构化输出说明：\n{schema_instruction}")
    system_prompt = "\n\n".join(parts)

    # 消息组装与 model_node 同口径：先隔轮驱逐只读查询明细（视图层，不动
    # State/checkpoint），再按 40k token 预算 drop-front 截断
    raw_messages = list(state.get("messages") or [])
    visible = _filter_stale_details(raw_messages)
    messages, _ = _truncate_history(visible)
    # [2026-09-19 可观测性第 2 步] 参数轮输入摘要：与决策轮 `args_input_summary`
    # 分两个事件（本轮段结构不同——参数轮不注入 BUSINESS_CONTEXT、也不注入
    # summaries 段，单事件会让某键对某一轮恒无意义）。active_skill 走 derive
    # 三态（理由同 model_node：state 值可被 checkpoint 跨版本重放污染）。
    _emit(
        runtime,
        EVENT_ARGS_INPUT_SUMMARY,
        {
            "history_raw": len(raw_messages),
            "history_sent": len(messages),
            "evicted_details": len(raw_messages) - len(visible),
            "active_skill": state.get("active_skill"),
        },
    )
    try:
        result = runtime.client.generate_tool_arguments(
            system_prompt=system_prompt,
            messages=messages,
            schema_instruction=schema_instruction,
            thread_id=runtime.context.thread_id if runtime.context else None,
            task_id=state.get("task_id"),
        )
    except ModelCallError as exc:
        content = json.dumps(
            {"ok": False, "error": f"工具参数生成失败：{exc}"}, ensure_ascii=False
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "argument_generation_error"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_args_failed",
        }

    # 一致性检查：参数轮工具名必须与决策轮一致（防改名调用其他工具）
    if result.tool_name != tool_name:
        content = json.dumps(
            {"ok": False, "error": f"参数生成阶段工具名不一致（{result.tool_name} != "
             f"{tool_name}），已取消本次调用，请重新决策"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "tool_name_mismatch"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_args_mismatch",
        }

    # 成功：pending 是完整旧决策 dict（reply_text/next_action/tool_name/
    # questions），只填充 tool_arguments——tools 节点零改动继续
    return {
        "pending_action": {**pending, "tool_arguments": result.tool_arguments},
        "current_step": "tool_args_done",
    }
