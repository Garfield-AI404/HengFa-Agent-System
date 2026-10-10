"""mapping_list 工具（映射版本列表只读查询）。

- 只读工具（is_write=False）：无确认卡、不建任务、不进写工具分支——依赖
  接线在 run 内自行 fail closed（与 memory_lookup 同款：本地配置表读取，
  无 ERP scope；requires_manager=True 仅注册表一致性保留，读路径无分支
  强制角色）；
- 查询范围：excel_mapping_versions/excel_mapping_columns 配置表（6.3
  规则 7：数据库配置表是执行事实源）——版本列表/状态/列数，供对账前
  检查 published 映射是否存在、当前发布版本号、历史版本与退役记录；
- policy_key_of 同 mapping_template 约定（"R6.mapping"）；
  policy_family=()（只读工具无规则包——写工具族非空断言只约束写工具）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary


class MappingListArgs(BaseModel):
    """mapping_list 参数：按模板键/状态筛选 + 分页（全部可选）。"""

    template_key: str | None = Field(
        None, description="映射模板键（筛选该模板全部历史版本；缺省返回所有模板）"
    )
    status: Literal["draft", "published", "retired"] | None = Field(
        None, description="按状态筛选：draft/published/retired"
    )
    limit: int = Field(50, ge=1, le=200, description="每批返回条数（缺省 50、上限 200）")
    offset: int = Field(0, ge=0, description="起始偏移（与 limit 配合翻页）")


def run_mapping_list(ctx: ToolContext, args: MappingListArgs) -> ToolResultSummary:
    """映射版本列表（只读）：本地配置表查询，全程 fail closed。"""
    service = ctx.mapping_service
    if service is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "映射服务未接线（harness 运行时缺 mapping_service），不能执行"},
        )
    try:
        records, total = service.list_versions(
            template_key=args.template_key,
            status=args.status,
            limit=args.limit,
            offset=args.offset,
        )
    except Exception as exc:
        return ToolResultSummary(
            ok=False, summary={"error": f"映射版本列表查询失败：{exc}"}
        )
    items = [
        {
            "template_key": r.template_key,
            "version_id": r.version_id,
            "version": r.version,
            "status": r.status,
            "sheet_name": r.sheet_name,
            "header_row": r.header_row,
            "column_count": len(r.columns),
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]
    return ToolResultSummary(
        ok=True,
        summary={"count": len(items), "total": total, "items": items},
    )
