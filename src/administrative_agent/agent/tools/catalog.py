"""list_entities 工具（2026-09-09 架构改造第一步：ERP 能力目录直挂）。

背景：ERP server 的 list_entities（实体/字段元数据）此前只被代码侧
EntityCache 消费用于字段校验，**模型从来看不到**——模型选字段靠猜
（本地 A/B 试验实证：模型断言「没有重量字段」；字段目录不可见是根因）。
拍板方案 B：把 server 的 list_entities 能力直挂为模型可选工具——模型在
写 erp_query/erp_calc 的 fields/filter/measures 参数前，如不确定字段名
可先调用本工具查看实体字段元数据。

- 只读工具（is_write=False）：无确认卡、不建任务、不进写工具分支——
  权限在 run 内自行校验（fail closed：manager 角色 + erp:query scope，
  与 erp_query/export_data 同款，8.4/10.5）；
- 两级裁剪（控制进上下文体量；实测本地 ERP 16 实体全量 ≈53KB，裁剪后
  紧凑目录 ≈12KB，模型可完整读到——明细无字符预算，全量进上下文）：
  * 不带 entity：全量紧凑目录——每实体 name/label + 每字段
    {name, label, type, enum(枚举值)}。不要 description 长文与 required/
    default/min/max/read_only/writable 等决策无关键（字段类型/枚举才是
    模型选字段与写单位条件的关键）；
  * 带 entity：该实体**完整公开字段细节**——保留 server 返回原样结构
    （mcp-api.md §3：type/label/required/nullable/enum_values/default/
    min/max/length/writable/system_managed 等），只过滤该实体外内容；
- 裁剪白名单（crop.QUERY_ITEM_WHITELIST）外的字段取值
  不会随 erp_query 结果回传：单列 ``filter_only_fields``（仅筛选字段）标记，
  与 tool_args_node 目录段同口径（crop.partition_catalog_fields）；「三不」
  字段（计算列 ∩ 白名单外）不入 fields 也不入 filter_only_fields；
- 敏感过滤由 server 按 scope 完成（mcp-api.md §3：users.password 等永不
  返回），本工具不自行拼接、不再叠加上层白名单——「server 能力直挂」；
- 失败 fail closed：权限/接线缺失/ERP 调用失败/返回不可解析/指定实体
  不存在 → ok=False + 错误说明（对齐 _err_text 用法），不静默。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary, _err_text
from administrative_agent.agent.tools.crop import partition_catalog_fields

#: items 信封的 entity 标记（非 ERP 实体名）：tools 节点摘要器按 entity
#: 键裁剪 items（crop_items 白名单）——目录条目结构不在任何实体裁剪
#: 白名单内，标记必须落在白名单外，条目才能原样进上下文。标记同时写
#: tool_query_archive 的 entity 列（会话内可见，无碍）。
CATALOG_ENTITY_MARKER = "catalog"


class ListEntitiesArgs(BaseModel):
    """list_entities 参数：可选 entity——不传返回全量紧凑目录，传了返回
    该实体的完整字段细节。"""

    entity: str | None = Field(
        None,
        description=(
            "实体名（如 metal_orders）。不传 = 返回全部可查实体的紧凑目录"
            "（实体名 + 字段名/类型/枚举），用于确定可查实体与字段名；"
            "传 = 只返回该实体的完整字段细节（含必填/默认值/可写性等），"
            "体积更小、信息更全"
        ),
    )


# ---------------------------------------------------------------------------
# 两级裁剪（纯函数，便于单测；raw_entity 为 server list_entities 返回的
# 单实体 dict，mcp-api.md §3 结构）
# ---------------------------------------------------------------------------


def _compact_field(fname: str, raw: dict[str, Any]) -> dict[str, Any]:
    """紧凑字段条目：{name, label?, type?, enum?}——写 fields/filter 参数
    决策所需的全部关键信息（字段名 + 中文含义 + 类型 + 枚举取值）。"""
    item: dict[str, Any] = {"name": fname}
    for key in ("label", "type"):
        value = raw.get(key)
        if value not in (None, ""):
            item[key] = value
    enum_values = raw.get("enum_values")
    if isinstance(enum_values, list) and enum_values:
        item["enum"] = [str(v) for v in enum_values]
    return item


def _partition_raw(raw: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """按实体元数据把字段名三段划分（复用 crop.partition_catalog_fields 同口径）。

    返回 ``(regular, computed_ok, filter_only)``：普通可回传 / 计算列可回传 /
    仅筛选（白名单外，取值不回传）。未登记实体 → filter_only 空、原样全字段。
    """
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        return [], [], []
    computed = {
        name for name, spec in fields.items()
        if isinstance(spec, dict) and spec.get("computed")
    }
    return partition_catalog_fields(str(raw.get("name") or ""), list(fields), computed)


def _compact_entity(raw: dict[str, Any]) -> dict[str, Any]:
    """紧凑实体条目：{name, label, fields: […], filter_only_fields?: […]}——
    不要 description 长文与 default/read_only 等决策无关键（实测 16 实体紧凑
    目录 ≈12KB）。

    [2026-09-15 方案 A] ``fields`` 只列**可回传**字段（regular + 计算列）；
    ``filter_only_fields`` 另列「白名单外、仅可 filter/order_by、取值不回传」
    字段——与 tool_args_node 目录段三段同口径（crop.partition_catalog_fields）。
    「三不」字段（计算列 ∩ 白名单外）不入任何列表（不可 filter/order_by、
    也不可 fields 取回）。
    """
    regular, computed_ok, filter_only = _partition_raw(raw)
    fields = raw.get("fields")
    keep = set(regular) | set(computed_ok)
    field_list = (
        [_compact_field(name, fields[name]) for name in fields
         if name in keep and isinstance(fields[name], dict)]
        if isinstance(fields, dict)
        else []
    )
    item: dict[str, Any] = {
        "name": raw.get("name"),
        "label": raw.get("label"),
        "fields": field_list,
    }
    if filter_only:
        item["filter_only_fields"] = filter_only
    return item


def _detail_entity(raw: dict[str, Any]) -> dict[str, Any]:
    """指定实体的完整公开细节（server 原样结构）——[2026-09-15 方案 A] 追加
    ``filter_only_fields``（仅筛选字段），并把「三不」字段（计算列 ∩ 白名单外）
    从 ``fields`` 剔除（不可 filter/order_by、也不可 fields 取回，列出只会诱导
    模型）。未登记实体 → 不加 filter_only_fields、fields 原样（与今天一致）。"""
    regular, computed_ok, filter_only = _partition_raw(raw)
    item = dict(raw)
    fields = raw.get("fields")
    if isinstance(fields, dict):
        keep = set(regular) | set(computed_ok)
        item["fields"] = {name: spec for name, spec in fields.items() if name in keep}
    if filter_only:
        item["filter_only_fields"] = filter_only
    return item


def _parse_entity_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从 server 返回取实体列表（兼容 {"entities": [...]} 与裸数组两种
    信封——与 mcp/metadata.py parse_entities 同款防御）。"""
    raw_list = data.get("entities") if isinstance(data.get("entities"), list) else None
    if raw_list is None and isinstance(data, list):
        raw_list = data
    if raw_list is None:
        return None
    return [e for e in raw_list if isinstance(e, dict)]


def _build_items(entities: list[dict[str, Any]], entity: str | None) -> list[dict[str, Any]]:
    """两级裁剪 → items 条目。

    - entity 为空：全部实体的紧凑目录（每实体一条 _compact_entity）；
    - entity 指定：命中实体的完整公开细节——server 原样结构（只过滤该
      实体外的内容；字段细节全保留，含 description）。
    """
    if entity is None:
        return [_compact_entity(e) for e in entities]
    return [_detail_entity(e) for e in entities if e.get("name") == entity]


def run_list_entities(ctx: ToolContext, args: ListEntitiesArgs) -> ToolResultSummary:
    """ERP 能力目录（只读直挂）：两级裁剪后返回（fail closed 全程）。"""
    # 1. 权限（非写工具分支无 scope 校验，本工具自行 fail closed）
    if (
        ctx.context is None
        or ctx.context.user_role != "manager"
        or "erp:query" not in (ctx.context.scope or frozenset())
    ):
        return ToolResultSummary(
            ok=False,
            summary={"error": "权限不足：查看实体目录需要 manager 角色与 erp:query scope（8.4/10.5）"},
        )
    # 2. 客户端接线（harness 运行时缺 client 不静默）
    if ctx.client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 客户端未接线（harness 运行时缺 client），不能执行"},
        )
    # 3. 实时调用 server list_entities（不读缓存——模型要的就是「现在
    #    有什么」；server 按调用者 scope 过滤敏感字段与不可见实体）
    result = ctx.client.call_tool("list_entities", {})
    if not result.ok:
        return ToolResultSummary(
            ok=False,
            summary={"error": f"ERP 实体目录获取失败（fail closed）：{_err_text(result)}"},
        )
    entities = _parse_entity_list(result.data or {})
    if not entities:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 实体目录返回无法解析（无实体列表），fail closed"},
        )
    # 4. 指定实体不存在（拼写错/scope 不可见）→ fail closed 附可见清单
    if args.entity is not None:
        found = any(e.get("name") == args.entity for e in entities)
        if not found:
            names = "、".join(str(e.get("name")) for e in entities)
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"实体 {args.entity!r} 不在可查目录中（名称有误或当前"
                        f"账号不可见），可见实体：{names}"
                    )
                },
            )
    # 5. 两级裁剪 + items 信封（entity 标记值落在 crop 白名单外，条目
    #    原样进上下文；目标实体名经 requested 键给模型核对）
    items = _build_items(entities, args.entity)
    scope = "entity" if args.entity else "compact"
    summary: dict[str, Any] = {
        "entity": CATALOG_ENTITY_MARKER,
        "scope": scope,
        "count": len(items),
        "note": (
            "字段目录（实体字段元数据）：写 erp_query/erp_calc 的 fields/"
            "filter/measures 参数前如不确定字段名，可从本目录取字段名与"
            "枚举取值；条目内 filter_only_fields 列出的字段**取值不会随 "
            "erp_query 结果回传**（勿写进 fields），但可用于 erp_query 的 "
            "filter/order_by；目录可能含 erp_query/erp_calc 白名单外实体"
            "（如实测不可查会 fail closed 报错并附可用清单）"
        ),
    }
    if args.entity:
        summary["requested"] = args.entity
    summary["items"] = items
    return ToolResultSummary(ok=True, summary=summary)
