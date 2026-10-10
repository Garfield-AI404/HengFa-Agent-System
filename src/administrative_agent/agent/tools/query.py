"""erp_query 工具（2026-08-25 定稿：ERP 只读查询，"查订单/查客户/查供货商/查员工"）。

- 只读工具（is_write=False）：无确认卡、不建任务、不进写工具分支——
  权限在 run 内自行校验（fail closed：manager 角色 + erp:query scope，
  8.4/10.5）；
- 实体白名单（9.2 第 2 条）：只能查 QUERYABLE_ENTITIES
  （metal_orders/powder_orders/customers/suppliers/employees/transactions/
  transaction_categories/customer_contacts/attendance/ar_payments/
  ap_payments/salary_payments/powder_inventory/powder_consumption），其他
  实体拒绝；收支记录与收支分类开放查询，是收支写（先查
  后写/分类先查后填）的信息前提；新增 7 个只读实体
  （契约见 mcp/scopes.py 同名注释）；
- 结果防爆：行数超出合并安全上限（MAX_MERGE_ROWS=500）时 truncated；
  合并后行（≤500）全量经 ToolResultSummary 直通
  tools 节点——字段裁剪后全量进上下文（明细不再有字符预算），原始全量
  存档（memory_lookup 按 query_id 回查）+ 隔轮驱逐由下游承担，本工具
  不再按字符截断明细。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary, _err_text
from administrative_agent.agent.tools.crop import is_returnable, returnable_fields
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

MAX_MERGE_ROWS = 500  # 自动续查合并上限（超出 truncated，防上下文爆炸）
#: 续查轮数上限（[2026-08-28] 3→10：与 MAX_MERGE_ROWS=500 匹配，
#: 500/窗口缺省 50 = 10 页恰好取齐 500 行；仍为异常保险丝——循环另有
#: not items / offset+len>=total 兜底退出，防 ERP 契约异常死循环）。
MAX_PAGES = 10

#: 订单类实体（调用方未指定 order_by 时的默认序，[2026-09-10 排序口径]）：
#: 按入库日期降序，让最近入库的订单最先进入模型上下文、最先被写出——用户
#: 口径是「按入库日期看订单」，ERP 实体默认序（-id 主键倒序）不是业务序，
#: 也把内部 id 的排序暴露出来。非订单实体（customers/suppliers/employees/
#: transactions 等）保持 ERP 实体默认序不受影响；本默认序仅在 Agent 侧注入，
#: 不改变 ERP 契约（mcp-api.md §4：order_by 缺省由 ERP 按 default_order 处理）。
#:
#: [2026-09-10 P2-4] 默认序为**字符串数组**，第二键 -id 恒定追加：
#: `-in_date` 单键同日大量重复，ERP 端只按给定字段排序、不追加 tiebreaker
#: （hengfa-erp/backend/app/mcp/query.py），`order_by(...).offset().limit()`
#: 翻页时同日记录跨页顺序不稳定 → 续查合并会重复/漏行（Agent 侧按 id 去重
#: 只掩盖重复，漏行无法发现 → 导出少行/汇总偏差）。追加唯一主键 -id 作
#: tiebreaker 使排序全序，翻页稳定。ERP 契约支持字符串数组（mcp-api.md §4
#: order_by 为 string/string[]，`-field` 降序）。
ORDER_ENTITIES: frozenset[str] = frozenset({"metal_orders", "powder_orders"})
ORDER_DEFAULT_ORDER_BY: tuple[str, ...] = ("-in_date", "-id")


def default_order_by(entity: str) -> list[str] | None:
    """调用方未指定 order_by 时的实体默认排序（订单类 → ["-in_date","-id"]
    ——入库日期降序、同日按 id 降序作稳定 tiebreaker；其余 → None，即不注入、
    走 ERP 实体默认序）。返回 list 以保证 JSON 序列化为数组形态。"""
    return list(ORDER_DEFAULT_ORDER_BY) if entity in ORDER_ENTITIES else None


class ErpQueryArgs(BaseModel):
    """erp_query 参数：实体名（必填）+ 可选字段/筛选/排序/分页。"""

    entity: str = Field(
        description=(
            "实体名：metal_orders（钣金订单）/powder_orders（塑粉订单）/"
            "customers（客户）/suppliers（供货商）/employees（员工）/"
            "transactions（收支记录，R7 收支写先查后写的前提）/"
            "transaction_categories（收支分类，R7 分类先查后填，按 tx_type 过滤取清单）/"
            "customer_contacts（客户联系人）/attendance（考勤记录，只读口径）/"
            "ar_payments（应收收款）/ap_payments（应付付款）/salary_payments"
            "（工资发放）/powder_inventory（塑粉库存色号：净库存直接查 stock "
            "字段，勿自行按公式另算）/"
            "powder_consumption（塑粉消耗记录）/ar_settlements（应收平账）"
        )
    )
    fields: list[str] | None = Field(
        None,
        description=(
            "要返回的字段名列表（取自字段目录/list_entities）；缺省 = 全部字段"
            "（返回后按业务白名单裁剪，备注与系统字段不返回）"
        ),
    )
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            '字段筛选：等值如 {"status": "完成"}；范围用 gte/lte 对象如 '
            '{"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}}；'
            "日期格式 YYYY-MM-DD。支持操作符 eq/ne/gt/gte/lt/lte/like"
            "（模糊包含，% 通配）/in（数组）/between（区间）；同一字段多"
            "操作符为 AND。"
        ),
    )
    order_by: str | list[str] | None = Field(
        None,
        description=(
            "排序字段名（如 in_date/updated_at）；降序在字段名前加连字符"
            '（如 -in_date、-id），可传数组如 ["status","-in_date"]；'
            "不要写 'in_date desc'（会被 ERP 当未知字段拒绝）。不填时按"
            "实体默认序（订单类 metal_orders/powder_orders 默认按入库日期"
            '降序并追加 id 作稳定 tiebreaker：["-in_date","-id"]，最近入库'
            "在前且同日订单翻页稳定不重复/不漏；其余实体 -id 最新在前）。"
        ),
    )
    limit: int | None = Field(
        None,
        ge=1,
        le=200,
        description=(
            "每批返回窗口（缺省 50、上限 200；fetch_all=true 时自动续查直至"
            "全量，超出安全上限 500 行时截断）"
        ),
    )
    offset: int | None = Field(
        None,
        ge=0,
        description=(
            "起始偏移（补足被截断的剩余行时用）：结果 truncated: true 表示只"
            "返回了前 500 行，要补足剩余行须传 fetch_all=false + "
            "offset=<已取得的行数>——必须关掉 fetch_all，否则工具会从该偏移"
            "继续自动续查并在 500 行上限处再次截断，你无法控制单批窗口；随后"
            "按已得行数递增 offset 重复直至取齐 total。fetch_all=true 时工具"
            "自动续查补齐全量、无需传 offset。"
        ),
    )
    fetch_all: bool = Field(
        True,
        description=(
            "查询结果自动续查合并至全量（fetch_all=true 默认）；只需部分数据时"
            "传 fetch_all=false 单页返回"
        ),
    )


def run_erp_query(ctx: ToolContext, args: ErpQueryArgs) -> ToolResultSummary:
    """ERP 只读查询：白名单 + 权限 + 结果防爆，全程 fail closed。"""
    # 1. 权限（非写工具分支无 scope 校验，本工具自行 fail closed）
    if (
        ctx.context is None
        or ctx.context.user_role != "manager"
        or "erp:query" not in (ctx.context.scope or frozenset())
    ):
        return ToolResultSummary(
            ok=False,
            summary={"error": "权限不足：查询需要 manager 角色与 erp:query scope（8.4/10.5）"},
        )
    # 2. 客户端接线（harness 运行时缺 client 不静默）
    if ctx.client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 客户端未接线（harness 运行时缺 client），不能执行"},
        )
    # 3. 实体白名单（9.2 第 2 条：强制应用当前 Agent 可查询实体白名单）
    if args.entity not in QUERYABLE_ENTITIES:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"实体 {args.entity!r} 不可查询（9.2 白名单），"
                    f"仅允许：{sorted(QUERYABLE_ENTITIES)}"
                )
            },
        )
    # 3.5 实体字段目录校验（9.2 第 1 条：查询字段必须来自 list_entities）。
    #     宽容剔除：fields/filter 中未知字段/筛选键剔除后继续（防模型猜
    #     字段名 → ERP UNKNOWN_FIELD → 整轮重跑浪费），剔除项经 note 反馈
    #     模型；filter 值原样保留（第 6 条：不解析 filter 值）。元数据本身
    #     不可用（缓存未接线/实体不在元数据内）→ fail closed 拒绝查询。
    cache = ctx.metadata
    if cache is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 实体元数据未接线（harness 运行时缺 metadata），不能执行"},
        )
    try:
        entity = cache.get_entity(args.entity)
    except Exception as exc:
        return ToolResultSummary(
            ok=False,
            summary={"error": f"ERP 实体元数据不可用：{exc}"},
        )
    if entity is None:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"实体 {args.entity!r} 不在元数据（list_entities）中，"
                    "字段目录未知，拒绝查询（fail closed）"
                )
            },
        )
    ignored_notes: list[str] = []
    # 字段合法性两套口径：
    # - fields：全部字段（含计算列——计算列可经 fields 取回）；
    # - filter/order_by：仅非计算列（ERP 侧计算列 filter/order_by 一律拒绝，
    #   见 mcp-api.md §3 `computed` 与 erp query.py `_assert_column_field`）。
    #   本地宽容剔除计算列（避免透传 ERP 被硬拒 → 整轮重跑），剔除项经 note
    #   反馈模型，措辞与「未知字段」区分开（计算列并非字段名拼错）。
    non_computed_fields = {
        name for name, spec in entity.fields.items() if not spec.computed
    }
    fields = args.fields
    if fields:
        unknown_fields = [f for f in fields if f not in entity.fields]
        if unknown_fields:
            ignored_notes.append("已忽略未知字段: " + ", ".join(unknown_fields))
        # [2026-09-15 方案 A] 第二类剔除：字段**在元数据里**、但不在结果裁剪
        # 白名单（crop.QUERY_ITEM_WHITELIST）——值即便从 ERP 取回也会被
        # crop_items 裁掉，白费一轮网络往返且模型误以为取到空值反复重试。
        # 措辞必须与「未知字段（可能拼错）」明确区分：这些字段存在、只是不参与
        # 结果回传（仍可用于 filter/order_by），模型一次就懂、不再重试。
        # 未登记实体 → is_returnable 恒 True → 不剔除（与今天一致）。
        non_returnable = [
            f for f in fields
            if f in entity.fields and not is_returnable(args.entity, f)
        ]
        if non_returnable:
            ignored_notes.append(
                "字段存在但不参与结果回传（仅可用于 filter/筛选；erp_query "
                "结果不回传其取值，勿写进 fields），已从 fields 中忽略: "
                + ", ".join(non_returnable)
            )
        fields = [
            f for f in fields
            if f in entity.fields and is_returnable(args.entity, f)
        ] or None
        # 剔除后为空 → 不传 fields（让 ERP 返回全字段）
    filt = args.filter
    if filt:
        unknown_keys = [k for k in filt if k not in entity.fields]
        if unknown_keys:
            ignored_notes.append("已忽略未知筛选键: " + ", ".join(unknown_keys))
        computed_keys = [
            k for k in filt if k in entity.fields and entity.fields[k].computed
        ]
        if computed_keys:
            ignored_notes.append(
                "已忽略计算列筛选键（实时计算列不可用于 filter，仅可经 fields "
                "取回）: " + ", ".join(computed_keys)
            )
        filt = {k: v for k, v in filt.items() if k in non_computed_fields} or None
        # 剔除后为空 → 不传 filter；值原样保留（9.2 第 6 条不解析）
    # 4. 构造查询参数（limit = 每批返回窗口：缺省 50、上限 200；其余非空透传）
    #    默认序：[2026-09-10 排序口径] 订单类实体未指定 order_by 时注入
    #    ["-in_date","-id"]（入库日期降序 + id 稳定 tiebreaker，最近在前且
    #    翻页稳定）；非订单实体不注入（走 ERP 实体默认序）。显式 order_by
    #    永远优先，不被默认值覆盖。
    window = min(args.limit or 50, 200)
    arguments: dict[str, Any] = {"entity": args.entity, "limit": window}
    if fields:
        arguments["fields"] = fields
    if filt:
        arguments["filter"] = filt
    order_by = args.order_by or default_order_by(args.entity)
    if order_by:
        # order_by 计算列校验（[computed 契约]）：ERP 对计算列排序一律拒绝
        # （query.py `_assert_column_field` → 整轮重跑）。本地剔除计算列条目
        # + note；未知字段仍保持现状（不剔除，交 ERP 拒——不扩大行为变更）。
        # 判前缀前先剥掉前导 "-"（"-stock" 与 "stock" 同一字段）。
        raw_order = [order_by] if isinstance(order_by, str) else list(order_by)
        computed_order = [
            item for item in raw_order
            if isinstance(item, str)
            and item.lstrip("-") in entity.fields
            and entity.fields[item.lstrip("-")].computed
        ]
        if computed_order:
            ignored_notes.append(
                "已忽略计算列排序字段（实时计算列不可用于 order_by，仅可经 "
                "fields 取回）: " + ", ".join(computed_order)
            )
        kept_order = [item for item in raw_order if item not in computed_order]
        if kept_order:
            # 保持原入参形态（字符串仍是字符串，数组仍是数组）——不因校验
            # 改变下发参数类型，避免既有透传语义漂移。
            if isinstance(order_by, str):
                arguments["order_by"] = kept_order[0] if len(kept_order) == 1 else kept_order
            else:
                arguments["order_by"] = kept_order
    # 5. 查询 + 自动续查：截断检测内嵌（truncated_by_rows）。兼容两种 ERP
    #    契约：demo 契约（ERP 全量返回、offset 仅回显）首轮即拿全量退出；
    #    生产契约（ERP 真分页）按窗口 offset 续查合并，直至全量。
    #    fetch_all=false（模型只要概要）→ 首轮后即退出，不自动续查。
    merged: list[dict[str, Any]] = []
    offset = args.offset or 0
    total: int | None = None
    pages = 0
    while True:
        pages += 1
        call_args: dict[str, Any] = dict(arguments)
        if offset:
            call_args["offset"] = offset
        result = ctx.client.call_tool("query", call_args)
        if not result.ok:
            return ToolResultSummary(
                ok=False, summary={"error": f"ERP 查询失败：{_err_text(result)}"}
            )
        data = result.data or {}
        items = data.get("items") or []
        total = data.get("total") or total or len(items)
        merged.extend(dict(r) for r in items)
        # 终止条件：fetch_all=false 单页 / 拿到全量 / 达合并上限 /
        # 达轮数上限 / 空页
        if (
            not args.fetch_all
            or len(merged) >= MAX_MERGE_ROWS
            or pages >= MAX_PAGES
            or not items
            or offset + len(items) >= total
        ):
            break
        offset += window
    # 去重（续查期间 ERP 并发变更防御；保持首次出现顺序）。仅对含 id 键的
    # 行按 id 去重：[2026-09-09 修复] fields 不含 id（模型经 ErpQueryArgs.
    # fields 自选字段，合法字段不强制补 id）时 ERP 返回行无 id 键，旧逻辑
    # str(row.get("id")) 使每行 key 均为 "None"，多行全部塌缩成 1 行（8 月
    # 钣金 165 行 → count=1，ERP total=165）。无 id 的行无法识别实体，不得
    # 去重——一律保留原序合并。
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in merged:
        if "id" not in row:
            deduped.append(row)
            continue
        key = str(row["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    rows = deduped[:MAX_MERGE_ROWS]
    # 到轮数上限且没拿全才是真截断（450 行 3 页取齐 → 不误报；1200 行 →
    # 500<1200 → 仍截断；demo 契约 1 轮退出不受影响）
    truncated_rows = len(deduped) > MAX_MERGE_ROWS or (
        pages >= MAX_PAGES and len(deduped) < total
    )
    # 6. 结果防爆：[2026-09-10 明细全量改版] 合并后行（≤500）全量返回——
    #    字段裁剪后全量进上下文（明细无字符预算），原始数据存档
    #    tool_query_archive（tools_node，memory_lookup 回查）+ 隔轮驱逐由
    #    下游承担，本工具不再按字符截断（否则存档拿不到原始全量）。
    #    truncated 仅标记行数超出合并安全上限（或未拿全）。
    payload = {"entity": args.entity, "count": len(rows), "items": rows}
    if total is not None and total > len(rows):
        payload["total"] = total  # ERP 端全量总数，模型可见差额
    if ignored_notes:
        payload["note"] = "；".join(ignored_notes)  # 被剔除的字段/筛选键，让模型知道
    # 6.1 空结果回显：[根因 A 全量取数] 防「自造空过滤条件 → 当成全表为空
    #    并反过来质疑用户数据」。仅 0 行时新增 empty_note，回显本次**实际
    #    生效**的 filter，与「该实体没有数据」区分开；有行时**不出现**
    #    （不改变现有 payload 形状）。键为新增字段，现有字段一律不动。
    #    - 有生效 filter 且**同时**带 offset：0 行的成因有两种（筛选无匹配 /
    #      offset 越过该筛选结果集末尾），必须**同时**回显 filter 与 offset，
    #      并指明「窗口落空也可能是 offset 越界所致」——否则 A2 续查路径里
    #      模型会被告知「去掉 filter 重查」而真因是 offset 越界，至少白跑
    #      一轮，最坏删掉一个本来正确的 filter 重新拉全表；
    #    - 有生效 filter、无 offset：回显 filter（原样）+ 说明「过滤后的结果
    #      ≠ 实体空」；
    #    - 无 filter 且无 offset：真·全表查询 0 行 → 说明「全表确为 0 行」；
    #    - 无 filter 但有 offset：0 行是 offset 窗口落空所致，不得说成
    #      「全表为空」（避免 A2 补足路径里 offset 越过末尾时给出假事实）。
    if not rows:
        applied_offset = args.offset or 0
        if filt:
            echo = json.dumps(filt, ensure_ascii=False)
            if applied_offset:
                payload["empty_note"] = (
                    "本次查询返回 0 行：本次生效的筛选条件为（"
                    + echo
                    + f"），且本次带了起始偏移 offset={applied_offset}。0 行的"
                    "成因有两种：筛选条件无匹配，或该 offset 已越过此筛选结果集"
                    "的末尾（窗口落空，并非筛选条件本身无匹配）。请先确认 offset "
                    "是否已越过该结果集末尾，再考虑调整或去掉 filter 后重查；「该"
                    "实体没有记录」的结论必须建立在无筛选的全表查询之上。"
                )
            else:
                payload["empty_note"] = (
                    "本次查询返回 0 行：这是本次生效的筛选条件（"
                    + echo
                    + "）过滤后的结果，不代表该实体没有数据。若这不是你要的范围，"
                    "请调整或去掉 filter 后重查；「该实体没有记录」的结论必须建立在"
                    "无筛选的全表查询之上。"
                )
        elif applied_offset:
            payload["empty_note"] = (
                f"本次从 offset={applied_offset} 起的查询窗口返回 0 行，"
                "不代表该实体没有数据（可能是 offset 已越过数据末尾，或该窗口"
                "内无行）。要判断该实体是否有数据，请去掉 offset 做全表查询。"
            )
        else:
            payload["empty_note"] = (
                "本次为无筛选的全表查询且返回 0 行：该实体当前确无记录"
                "（全表查询确为 0 行）。"
            )
    if truncated_rows:
        payload["truncated"] = True  # 超出合并安全上限 / 未拿全
    # 7. 字段回注：[2026-09-28 §3.1 字段盲区] 模型自选 fields 时，结果与「该
    #    字段不存在」**形状相同**（实测：同一条 customers 查询在运行之间摇摆，
    #    8/30 次只取 id+company_name；powder_inventory 无单价字段 → 0 行被读成
    #    「没有历史价」；notes 不在回传白名单 → 读不到被读成「不可写」）。本键
    #    把「本次只返回了什么」显式回显，措辞与 R4.query.md 规则 1 同口径（用
    #    「未取到」，不得出现「不存在」读法）。
    #    出现条件：本次**确实下发了收窄字段集**（`fields` 过滤后非空——即
    #    entity.fields ∩ 结果回传白名单 之后剩下的字段集）：
    #    - 不传 fields → `fields` 为 None → 不出现（避免每轮都涨上下文）；
    #    - 传的全是未知字段或不可回传字段 → 过滤后为空 → 不出现：这些字段已被
    #      payload 的 note 明确说明；若在此处再列一遍、还对**已被判定为未知**的
    #      字段名反向断言「不代表 ERP 没有该字段」，同一 payload 内自相矛盾
    #      （本批立意正是「未取到 ≠ 不存在」，不能对未知字段名说「不代表没有」）；
    #    - 实体须在结果回传白名单内（`returnable_fields` 非 None）：未登记实体
    #      不裁剪，「只返回了 N 个字段」无意义，故不出现。**注：当前
    #      QUERYABLE_ENTITIES 与 QUERY_ITEM_WHITELIST 键集完全一致，本判定恒真
    #      ——保留它是为将来新增可查实体（尚未登记 crop 白名单）时的防线。**
    #    - 有行：列出**实际返回**的字段名（按实际返回行的键集合；各行键集合一致
    #      ——ERP 可能在请求字段之外补回行键，故不能拿请求的 fields 当返回值）；
    #    - 0 行：列出**过滤后实际下发的**收窄字段集并说明本次 0 行——这是最危险
    #      的情形（手里没有任何行，最容易把「没取到」读成「不存在」）。
    #    被 crop 剔除的请求字段不是本键的职责（ignored_notes 已覆盖，勿动）；
    #    本键只回答「本次共返回了什么」。既有 payload 键与取值一律不动，本键
    #    追加在末尾。
    if fields and returnable_fields(args.entity) is not None:
        if rows:
            returned = list(rows[0].keys())
            payload["fields_note"] = (
                "本次查询显式指定了 fields，只返回了 "
                + str(len(returned))
                + " 个字段：`"
                + "、".join(returned)
                + "`；其余字段本次未取到（不代表 ERP 没有该字段，也不代表不可"
                "写入）。需要其他字段时不传 fields 重新查询即可取到全部可回传"
                "字段。"
            )
        else:
            payload["fields_note"] = (
                "本次查询显式指定了 fields：`"
                + "、".join(fields)
                + "`；本次返回 0 行，这些字段本次未取到（不代表 ERP 没有该字段，"
                "也不代表不可写入）。需要这些字段时不传 fields 重新查询，或调整"
                " filter/时间窗后重查。"
            )
    return ToolResultSummary(ok=True, summary=payload)
