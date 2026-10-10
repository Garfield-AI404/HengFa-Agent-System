"""R5 分析域（规格 6.2 五条）。

口径对照：
- 先取足够完整、可追溯的数据（6.2 第 1 条）：复用 QueryExecutor.fetch_all
  自动分页取全（9.2 第 4 条不丢 total），报告携带样本量与数据来源；
- 报告区分"ERP 事实 / 计算结果 / 模型建议"三区（6.2 第 2 条）：
  facts = ERP 返回原样记录；computations = 基于事实的本地计算（每项附
  公式口径）；recommendations = 模型建议（明确标注"供管理者决策参考"，
  不替管理者做决定，6.2 第 5 条）；
- 每个关键结论附时间范围、样本量与计算口径（6.2 第 3 条）：ReportScope
  承载三要素，AnalysisReport 携带；
- 数据不完整显式说明限制（6.2 第 4 条）：分页期间数据变化、翻页超限、
  空结果（"未查到"不解释为"从未存在"）均进入 limitations；
- 不生成无依据的经营结论（6.2 第 5 条）：recommendations 每条必须有
  basis（引用 facts/computations）；空结果不生成任何计算与建议。

计算函数（compute 回调）由调用方注入：本模块只保证报告结构与口径要素，
不内置具体业务指标；示例计算见 scripts/d17_query_analysis_demo.py。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import structlog

from administrative_agent.domains.query import (
    DEFAULT_QUERY_LIMIT,
    EMPTY_RESULT_MESSAGE,
    FullQueryResult,
    QueryExecutor,
    QuerySpec,
    build_query_spec,
)
from administrative_agent.mcp.errors import MCPError

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.analysis")

#: 10.2 操作枚举：R5 分析（与 intent.Operation.ANALYZE 一致，代码枚举不出消息）
OP_ANALYZE = "analyze"


@dataclass(frozen=True)
class ReportScope:
    """口径要素（6.2 第 3 条：时间范围、样本量、计算口径 + 数据来源可追溯）。"""

    period: str
    sample_count: int
    sample_source: str
    calculation_notes: str
    data_completeness: str  # "complete" | "partial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "sample_count": self.sample_count,
            "sample_source": self.sample_source,
            "calculation_notes": self.calculation_notes,
            "data_completeness": self.data_completeness,
        }


@dataclass(frozen=True)
class ComputedMetric:
    """计算结果（6.2 第 2 区）：value 保持与 ERP 一致的数值格式（6.1 第 5 条
    不本地改格式），口径由 formula 说明。"""

    name: str
    value: str
    formula: str
    sample_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "formula": self.formula,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True)
class Recommendation:
    """模型建议（6.2 第 5 条）：必须附依据（basis），措辞供管理者决策参考，
    不直接下经营结论、不替管理者做决定。"""

    text: str
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "basis": self.basis}


@dataclass(frozen=True)
class AnalysisReport:
    """R5 分析报告：三区（ERP 事实/计算结果/模型建议）+ 口径要素 + 限制说明。"""

    scope: ReportScope
    facts: tuple[dict[str, Any], ...]
    computations: tuple[ComputedMetric, ...]
    recommendations: tuple[Recommendation, ...]
    limitations: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.facts

    def to_dict(self) -> dict[str, Any]:
        """结构化输出（key 与 6.2 三区一一对应）。"""
        return {
            "scope": self.scope.to_dict(),
            "facts": [dict(f) for f in self.facts],
            "computations": [c.to_dict() for c in self.computations],
            "recommendations": [r.to_dict() for r in self.recommendations],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class AnalysisPlan:
    """分析取数计划：查询条件 + 口径要素（period/calculation_notes 由
    编排层按用户请求与业务上下文填写，本模块不猜测）。"""

    entity: str
    fields: tuple[str, ...] = ()
    filter: Mapping[str, Any] | None = None
    order_by: str | Sequence[str] | None = None
    period_note: str = ""
    calculation_notes: str = ""
    page_limit: int = DEFAULT_QUERY_LIMIT

    def to_query_spec(self) -> QuerySpec:
        return build_query_spec(
            entity=self.entity,
            fields=self.fields,
            filter=self.filter,
            order_by=self.order_by,
            limit=self.page_limit,
        )

    def source_note(self) -> str:
        """数据来源（可追溯：实体 + 查询条件摘要）。"""
        parts = [f"实体 {self.entity}"]
        if self.filter:
            parts.append(f"filter={self.filter!r}")
        if self.order_by:
            parts.append(f"order_by={self.order_by!r}")
        return "；".join(parts)


def build_analysis_report(
    *,
    scope: ReportScope,
    facts: Sequence[Mapping[str, Any]],
    computations: Sequence[ComputedMetric] = (),
    recommendations: Sequence[Recommendation] = (),
    limitations: Sequence[str] = (),
) -> AnalysisReport:
    """组装报告（纯函数）：三区 + 口径 + 限制。"""
    return AnalysisReport(
        scope=scope,
        facts=tuple(dict(f) for f in facts),
        computations=tuple(computations),
        recommendations=tuple(recommendations),
        limitations=tuple(limitations),
    )


class AnalysisExecutor:
    """R5 分析执行器：取数（fetch_all，可追溯）→ 计算 → 组装报告 → 收口。

    任务状态与审计同 QueryExecutor（只读任务 PENDING 直接执行；attempt/
    result 按 task_attempts 既有机制，每次实际 query 调用独立记录）。
    """

    def __init__(
        self,
        *,
        query_executor: QueryExecutor,
        target_type: str = "analysis",
    ) -> None:
        self._query = query_executor
        self._target_type = target_type

    def execute(
        self,
        *,
        task_id: str,
        plan: AnalysisPlan,
        compute: Callable[[FullQueryResult], Sequence[ComputedMetric]] | None = None,
        recommendations: Sequence[Recommendation] | None = None,
    ) -> AnalysisReport:
        """执行分析并生成报告。

        - 取数：QueryExecutor.fetch_all（finalize=False，任务终态由本方法收口）；
        - 计算：compute 回调输入取全结果，输出计算结果（空结果时不调用）；
        - 建议：recommendations 由调用方提供（必须带 basis）；空结果时清空；
        - 任务终态：报告组装成功 → succeeded（verification_passed=True）；
          取数失败 → failed + 原样上抛 MCPError。
        """
        self._query.begin_active(task_id)
        try:
            full = self._query.fetch_all(
                task_id=task_id, spec=plan.to_query_spec(), finalize=False
            )
        except MCPError:
            # fetch_all 已在内部 fail 任务并记录审计，原样上抛（错误码沿用既有体系）
            raise

        facts = full.items
        limitations: list[str] = list(full.notes)
        if not facts:
            limitations.append(
                f"{EMPTY_RESULT_MESSAGE}；未生成计算结果与建议（不把未查到解释为从未存在）"
            )

        computations: Sequence[ComputedMetric] = ()
        if facts and compute is not None:
            computations = compute(full)

        recs: Sequence[Recommendation] = recommendations or ()
        if not facts:
            recs = ()

        scope = ReportScope(
            period=plan.period_note,
            sample_count=full.fetched_count,
            sample_source=plan.source_note(),
            calculation_notes=plan.calculation_notes,
            data_completeness="complete" if full.complete else "partial",
        )
        report = build_analysis_report(
            scope=scope,
            facts=facts,
            computations=computations,
            recommendations=recs,
            limitations=limitations,
        )
        self._query.finish(task_id)
        return report
