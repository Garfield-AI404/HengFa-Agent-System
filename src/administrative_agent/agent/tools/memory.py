"""memory_lookup 工具（2026-08-24 定稿：查询已压缩的早期会话存档；
按 query_id 查工具查询原始数据存档）。

- 只读工具（is_write=False，无 ERP scope 依赖——会话记忆查询，不进 tools
  节点写工具分支）；
- 两种查询语义（二选一）：
  * 按段号：segment_no（段号，与目录条目【段N】对应）+ query（可选关键词
    过滤），返回 {role, content} 文本行；
  * 按 query_id：工具结果里的 query_id / archived_note 提示
    （形如 tool-call-xxxx）→ 返回该查询原始全量明细 items（按实体
    白名单裁剪后返回——丢弃噪音字段，条数不变）——limit + offset 可分
    批翻页拉取全量（缺省 limit 200、offset 0；大结果建议 limit=30 逐批
    分析，offset=30/60/… 翻页，直至取齐 total 条；响应
    含 total=存档总行数（全量口径，不随分页窗口变化）与 count=本次已
    返回条数）；
- 结果截断：段号路径按 lookup_result_chars（settings）；query_id 路径
  返回结构化 items（明细无字符预算，字段裁剪
  后全量进上下文并由 tools 节点打 detail 标记——当轮可见、隔轮由模型
  输入视图层驱逐），不在此截断。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary
from administrative_agent.agent.tools.crop import crop_items
from administrative_agent.config import settings

#: query_id 路径缺省返回明细条数上限（原始全量行数超限时按 limit 截取；
#: 调用方可调小 limit 分批拉取，防上下文超预算）。
TOOL_QUERY_LOOKUP_LIMIT: int = 200


class MemoryLookupArgs(BaseModel):
    """memory_lookup 参数：二选一——按段号查会话压缩存档（segment_no），
    或按 query_id 查工具查询原始数据存档（query_id）。"""

    segment_no: int | None = Field(
        None, description="会话压缩存档段号（与系统提示历史摘要区目录【段N】对应）"
    )
    query: str | None = Field(None, description="段内关键词过滤（仅 segment_no 路径）")
    query_id: str | None = Field(
        None,
        description="工具查询原始数据存档标识（工具结果里的 query_id 字段，"
                    "即 archived_note 提示的存档标识，形如 tool-call-xxxx；"
                    "返回该次查询的原始明细行）",
    )
    limit: int | None = Field(
        None,
        ge=1,
        le=500,
        description="query_id 路径每批返回明细条数上限（缺省 200；大结果建议调小如 limit=30 分批分析）",
    )
    offset: int | None = Field(
        None,
        ge=0,
        description="query_id 路径起始偏移（从第 offset 条明细开始返回；"
                    "与 limit 配合分批翻页拉取全量，如 limit=30, offset=0 → 30, offset=30 → …）",
    )


def run_memory_lookup(ctx: ToolContext, args: MemoryLookupArgs) -> ToolResultSummary:
    """按段号取回存档原始会话，或按 query_id 取回工具查询原始明细。

    fail closed：archive 未接线 / 无 thread_id / 目标不存在 → ok=False +
    原因（模型向用户说明后自纠）。segment_no 与 query_id 都不给 → 参数
    错误（ok=False）。
    """
    archive = ctx.archive
    if archive is None:
        return ToolResultSummary(ok=False, summary={"error": "记忆存档未接线（archive 缺失），无法查询"})
    thread_id = ctx.context.thread_id if ctx.context else ""
    if not thread_id:
        return ToolResultSummary(ok=False, summary={"error": "缺少 thread_id，无法查询"})
    if args.query_id:
        # ---- query_id 路径（2026-08-28 两级数据访问 2c） ----
        try:
            found = archive.get_tool_query(thread_id, args.query_id)
        except Exception as exc:
            return ToolResultSummary(ok=False, summary={"error": f"存档查询失败：{exc}"})
        if not found:
            return ToolResultSummary(
                ok=False, summary={"error": f"查询存档 {args.query_id!r} 不存在或为空"}
            )
        rows, total, entity = found  # total = 存档总行数（写档 count 列，全量口径）
        # [2026-08-28 smoke 暴露] limit 外补 offset：分批翻页拉取全量——
        # 仅 limit 时每次调用都返回同一窗口，超过单批容量的明细不可达
        # （150 条 × ~470 字符 ≈ 70KB 直读会显著膨胀上下文，深分析仍
        # 建议 limit=30 翻页取齐）。
        offset = args.offset or 0
        rows = rows[offset : offset + (args.limit or TOOL_QUERY_LOOKUP_LIMIT)]
        # [2026-08-28] items 返回前按实体白名单裁剪（crop_items，与 tools
        # 节点摘要器同口径）：丢弃 notes/系统字段等噪音后体积显著下降
        # （30 条 ≈12KB → ~5KB）；total（存档总行数）与 count（本次窗口
        # 条数）不受裁剪影响，仍为全量口径。
        rows = crop_items(rows, entity)
        # 返回结构化 items（裁剪后明细）——[2026-09-10 明细全量改版] 明细
        # 无字符预算，全量进上下文（tools 节点打 detail 标记、隔轮驱逐），
        # 不在此按字符截断。
        # [2026-08-28 D1] total 恒为存档总数（不随窗口变化）——模型按
        # "取齐 total 条"分批翻页；count 为本次窗口已返回条数（两值分离，
        # 避免 count=窗口大小导致模型取一批即停、深分析只覆盖部分数据）。
        return ToolResultSummary(
            ok=True,
            summary={
                "query_id": args.query_id,
                "total": total,
                "count": len(rows),
                "items": rows,
            },
        )
    if args.segment_no is None:
        return ToolResultSummary(
            ok=False, summary={"error": "memory_lookup 需提供 segment_no（会话存档段号）或 query_id（查询存档标识）之一"}
        )
    # ---- 段号路径（2026-08-24 定稿语义） ----
    try:
        seg = archive.get_segment(thread_id, args.segment_no)
    except Exception as exc:
        return ToolResultSummary(ok=False, summary={"error": f"存档查询失败：{exc}"})
    if not seg:
        return ToolResultSummary(ok=False, summary={"error": f"存档段 {args.segment_no} 不存在"})
    rows: list[str] = []
    for item in seg:
        content = str(item.get("content", ""))
        if args.query and args.query not in content:
            continue
        rows.append(f"{item.get('role', '?')}: {content}")
    text = "\n".join(rows) or "（该段无匹配内容）"
    if len(text) > settings.lookup_result_chars:
        text = text[: settings.lookup_result_chars] + "…（截断）"
    return ToolResultSummary(ok=True, summary={"segment_no": args.segment_no, "text": text})
