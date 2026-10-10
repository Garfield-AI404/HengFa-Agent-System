"""D17 R5 分析域测试（规格 6.2 五条）。

覆盖：报告三区分区（ERP 事实/计算结果/模型建议）、口径要素（时间范围/
样本量/计算口径）、数据不完整显式限制、空结果不生成计算与建议、建议
必须带依据、事实保持 ERP 格式、任务状态与审计链。

复用 tests/domains/test_query.py 的 FakeQueryErp 假服务（D5 规则：
测试夹具，不代表 ERP 真实运行态）。
"""

from __future__ import annotations

import os

os.environ["ERP_MCP_URL"] = "http://erp-mcp.test"
os.environ["ERP_MCP_API_KEY"] = "test-api-key-not-for-production"
os.environ["ERP_MCP_JWT"] = ""
os.environ["ERP_MCP_CONNECT_TIMEOUT"] = "5"
os.environ["ERP_MCP_READ_TIMEOUT"] = "30"
os.environ["ERP_MCP_TOTAL_TIMEOUT"] = "60"
os.environ["ERP_MCP_METADATA_TTL"] = "3600"

import pytest  # noqa: E402

from administrative_agent.audit.service import AuditService  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.analysis import (  # noqa: E402
    AnalysisExecutor,
    AnalysisPlan,
    AnalysisReport,
    ComputedMetric,
    Recommendation,
    ReportScope,
    build_analysis_report,
)
from administrative_agent.domains.query import FullQueryResult, QueryExecutor  # noqa: E402
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.mcp.errors import MCPError  # noqa: E402
from administrative_agent.mcp.metadata import EntityCache  # noqa: E402
from administrative_agent.tasks.machine import TaskStatus  # noqa: E402
from administrative_agent.tasks.service import TaskService  # noqa: E402
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.domains.test_query import FakeQueryErp, TRACE_ID, ACTOR_ACCOUNT, ACTOR_ROLE, create_task  # noqa: E402
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

from tests.domains.test_query import ERP_MCP_URL  # noqa: E402
import respx  # noqa: E402


def demo_compute(full: FullQueryResult):
    """示例计算：按月聚合订单数（口径：in_date 前 7 位为月份）。"""
    months: dict[str, int] = {}
    for row in full.items:
        month = str(row.get("in_date", ""))[:7]
        months[month] = months.get(month, 0) + 1
    return tuple(
        ComputedMetric(
            name=f"{month} 订单数",
            value=str(count),
            formula="按 in_date 的 YYYY-MM 聚合订单数",
            sample_count=count,
        )
        for month, count in sorted(months.items())
    )


def demo_recommendations() -> tuple[Recommendation, ...]:
    return (
        Recommendation(
            text="建议对 2026-06 订单波动情况人工核查（供管理者决策参考）",
            basis="2026-06 订单数为 10，占总样本 100%（计算区）",
        ),
    )


@pytest.fixture
def erp() -> FakeQueryErp:
    return FakeQueryErp()


@pytest.fixture
def actx(erp: FakeQueryErp):
    """组装分析执行器上下文。yield (erp, tasks, audit, analysis, query, client)。"""
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            cache = EntityCache(client)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            query_exec = QueryExecutor(
                client=client,
                cache=cache,
                tasks=tasks,
                audit=audit,
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                duty_code="R5",
            )
            analysis = AnalysisExecutor(query_executor=query_exec)
            yield erp, tasks, audit, analysis, query_exec, client
        finally:
            client.close()


def make_plan(**overrides) -> AnalysisPlan:
    base = dict(
        entity="metal_orders",
        period_note="2026-01 至 2026-07（按 in_date）",
        calculation_notes="金额/日期保持 ERP 原格式；按月聚合以 in_date 计",
    )
    base.update(overrides)
    return AnalysisPlan(**base)


# ---- 三区分区与口径要素（6.2 第 2/3 条） ----


def test_report_three_zones_and_scope_factors(erp, actx):
    """报告区分 ERP 事实 / 计算结果 / 模型建议；scope 携带时间范围、
    样本量与计算口径。"""
    _, tasks, _, analysis, _, _ = actx
    task = create_task(tasks, duty="R5")
    report = analysis.execute(
        task_id=task.task_id,
        plan=make_plan(),
        compute=demo_compute,
        recommendations=demo_recommendations(),
    )
    assert isinstance(report, AnalysisReport)
    # ERP 事实区：原样记录
    assert report.facts and report.facts[0]["total_amount"] == "375.00"
    # 计算结果区：每项附公式口径与样本量
    assert report.computations and all(c.formula for c in report.computations)
    assert all(c.sample_count > 0 for c in report.computations)
    # 模型建议区：明确标注（措辞供管理者参考）
    assert report.recommendations
    assert all(r.basis for r in report.recommendations)
    assert "供管理者决策参考" in report.recommendations[0].text
    # 口径要素：时间范围 / 样本量 / 计算口径 / 数据来源可追溯
    assert report.scope.period == "2026-01 至 2026-07（按 in_date）"
    assert report.scope.sample_count == 10
    assert "metal_orders" in report.scope.sample_source
    assert report.scope.calculation_notes
    assert report.scope.data_completeness == "complete"
    # 结构化输出三区 key 完整
    d = report.to_dict()
    assert set(d) == {"scope", "facts", "computations", "recommendations", "limitations"}
    # 任务收口 succeeded（verification_passed=True 路径）
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_facts_keep_erp_format(erp, actx):
    """事实区保持 ERP 返回格式：金额字符串、日期原样（6.1 第 5 条口径
    不本地转换；计算口径另行说明）。"""
    _, tasks, _, analysis, _, _ = actx
    task = create_task(tasks, duty="R5")
    report = analysis.execute(task_id=task.task_id, plan=make_plan())
    assert report.facts[0]["total_amount"] == "375.00"
    assert isinstance(report.facts[0]["total_amount"], str)
    assert report.facts[0]["in_date"].startswith("2026-")


# ---- 数据不完整显式说明（6.2 第 4 条） ----


def test_partial_data_limitations_explicit(erp, actx):
    """分页期间数据变化 → data_completeness=partial + limitations 显式说明。"""
    _, tasks, _, analysis, _, _ = actx
    erp.drift_at_call["metal_orders"] = 2
    task = create_task(tasks, duty="R5")
    report = analysis.execute(
        task_id=task.task_id,
        plan=make_plan(page_limit=4),
        compute=demo_compute,
    )
    assert report.scope.data_completeness == "partial"
    assert any("数据发生变化" in n for n in report.limitations)


def test_empty_result_no_computations_no_recommendations(erp, actx):
    """空结果：不生成计算与建议（6.2 第 5 条：不生成无依据结论）；限制区
    显式说明"未查到"边界（6.1 第 4 条：不解释为从未存在）。"""
    _, tasks, _, analysis, _, _ = actx
    task = create_task(tasks, duty="R5")
    report = analysis.execute(
        task_id=task.task_id,
        plan=make_plan(filter={"model": "NO-SUCH-MODEL"}),
        compute=demo_compute,
        recommendations=demo_recommendations(),
    )
    assert report.empty
    assert report.facts == ()
    assert report.computations == ()
    assert report.recommendations == ()  # 建议随空结果清空
    assert any("没有符合条件的记录" in n for n in report.limitations)
    assert any("从未存在" in n for n in report.limitations)


# ---- 纯函数组装 ----


def test_build_analysis_report_pure():
    scope = ReportScope(
        period="2026-01 至 2026-07",
        sample_count=2,
        sample_source="实体 metal_orders",
        calculation_notes="按月聚合",
        data_completeness="complete",
    )
    report = build_analysis_report(
        scope=scope,
        facts=[{"id": 1, "total_amount": "100.00"}],
        computations=[ComputedMetric("合计", "100.00", "金额求和", 1)],
        recommendations=[Recommendation("建议", "依据")],
        limitations=["限制说明"],
    )
    assert report.scope.sample_count == 2
    assert report.computations[0].value == "100.00"
    assert report.limitations == ("限制说明",)


def test_report_scope_dict_shape():
    scope = ReportScope("期间", 5, "来源", "口径", "complete")
    d = scope.to_dict()
    assert set(d) == {"period", "sample_count", "sample_source", "calculation_notes", "data_completeness"}


# ---- 执行链路：取数失败与审计 ----


def test_analysis_fetch_failure_marks_failed(erp, actx):
    """取数失败：任务 failed、MCPError 原样上抛（错误码沿用既有体系）。"""
    _, tasks, audit, analysis, _, _ = actx
    erp.fail_next.append(("ERP_BUSY", "server busy"))
    task = create_task(tasks, duty="R5")
    with pytest.raises(MCPError) as exc:
        analysis.execute(task_id=task.task_id, plan=make_plan(), compute=demo_compute)
    assert exc.value.code == "ERP_BUSY"
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len(results) == 1
    assert audit.attempts(task.operation_id)[0].result_status == "failed"


def test_analysis_audit_chain_per_page(erp, actx):
    """分析取数：每页独立 attempt/result（11.2 规则 3）；成功收口 succeeded。"""
    _, tasks, audit, analysis, _, _ = actx
    task = create_task(tasks, duty="R5")
    analysis.execute(
        task_id=task.task_id,
        plan=make_plan(page_limit=4),
        compute=demo_compute,
    )
    results = audit.events(task_id=task.task_id, event_type="result")
    assert len(results) == 3  # 10 行 / limit 4 → 3 页
    assert audit.attempts(task.operation_id)[-1].attempt_no == 3
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
