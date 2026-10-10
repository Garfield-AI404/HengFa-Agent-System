"""[2026-09-22 RAG 长期记忆接入] 三个长期记忆工具（rag_remember/rag_recall/
rag_forget）的**账号注入**守护。

本文件的核心判据按「prompt 守护测试假锁」纪律设计：断言**钉在注入后真实
下发给 MCP 客户端的 payload**（由假 client 记录 ``call_tool`` 的实参），
而不是「函数被调用过」这类可被邻近逻辑救活的弱判据。测的是出参。

覆盖：
1. remember：preference → subject=登录账号；fact/episode → **必须剥掉**模型
   幻觉出的 subject（置 None，RAG 侧对非 preference 带 subject 会直接报错）；
2. recall：**无条件**注入 subject=登录账号（否则账号级偏好永远召不回）；
3. forget：query 批量路径 → subject=登录账号（收紧在本账号内）；memory_id
   路径 → subject=None（注入会让「忘一条记忆」命中 0 行、静默失效）；
4. subject 绝不进提示词/State —— 只能经 ctx.context.user_id 注入：模型给的
   subject 一律被忽略（本层重算）；
5. 未配置/无身份 → fail closed（不触网、不静默成功）；
6. without_unconfigured_rag_tools：未配置时三个工具从模型可见工具集剔除。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.rag_memory import (
    RAG_TOOL_NAMES,
    RagForgetArgs,
    RagRecallArgs,
    RagRememberArgs,
    RagSearchDocumentsArgs,
    run_rag_forget,
    run_rag_recall,
    run_rag_remember,
    run_rag_search_documents,
    without_unconfigured_rag_tools,
)
from administrative_agent.config import Settings
from administrative_agent.mcp.client import (
    MCPClient,
    ToolResult,
    _build_rag_timeout,
    _NEVER_RETRY_TOOLS,
    _should_retry,
    _unstructured_error,
    build_rag_mcp_client,
)
from administrative_agent.mcp.errors import ErrorCategory, MCPError

#: 假登录账号（注入来源是请求级 ctx.context.user_id，不是模型）。
_ACCOUNT = "hengfa-internal-01"


class _FakeAudit:
    """记录 record_rag_memory / record_rag_recall 的**实参**（断言钉在落审计的
    字段值上，不是「函数被调用过」这类弱判据）。"""

    def __init__(self, *, raises: bool = False, exc_message: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.recall_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self._raises = raises
        self._exc_message = exc_message

    def _maybe_raise(self) -> None:
        if self._raises:
            from administrative_agent.audit.service import AuditFailureError

            raise AuditFailureError(None, self._exc_message or "审计写入失败")

    def record_rag_memory(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        self._maybe_raise()
        return kwargs

    def record_rag_recall(self, **kwargs: Any) -> dict[str, Any]:
        self.recall_calls.append(kwargs)
        self._maybe_raise()
        return kwargs

    def record_rag_search_documents(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        self._maybe_raise()
        return kwargs


class _FakeRagClient:
    """记录每次 call_tool 的工具名与 payload，返回预置成功结果。

    记录的是**真正下发**给 MCP 协议的 payload——断言钉在出参上，不是「是否
    调用了函数」。
    """

    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or ToolResult(ok=True, data={"ok": True})

    def call_tool(self, tool_name: str, payload: dict[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(payload)))
        return self._result


def _ctx(
    rag_client: Any,
    *,
    user_id: str | None = _ACCOUNT,
    thread_id: str | None = "t-1",
    audit: Any = None,
) -> ToolContext:
    context = (
        AgentContext(
            user_id=user_id,
            user_role="manager",
            scope=frozenset(),
            thread_id=thread_id,
        )
        if user_id is not None
        else None
    )
    return ToolContext(context=context, rag_client=rag_client, audit=audit)


def _payload(client: _FakeRagClient, idx: int = 0) -> dict[str, Any]:
    _, payload = client.calls[idx]
    return payload


# ---------------------------------------------------------------------------
# 1) remember：subject 按 kind 重算，模型幻觉的 subject 被忽略
# ---------------------------------------------------------------------------


def test_remember_preference_injects_account_subject() -> None:
    """preference → subject=登录账号（RAG 侧偏好必带 subject）。"""
    client = _FakeRagClient()
    result = run_rag_remember(
        _ctx(client),
        RagRememberArgs(content="客户偏好用微信沟通", kind="preference"),
    )
    assert result.ok is True
    assert client.calls[0][0] == "remember"
    assert _payload(client)["subject"] == _ACCOUNT


def test_remember_preference_overrides_hallucinated_subject() -> None:
    """preference：模型若瞎给 subject，**以登录账号为准**（不是照单全收）。"""
    client = _FakeRagClient()
    run_rag_remember(
        _ctx(client),
        RagRememberArgs(content="偏好微信", kind="preference", subject="someone-else"),
    )
    assert _payload(client)["subject"] == _ACCOUNT


def test_remember_fact_and_episode_strip_hallucinated_subject() -> None:
    """fact/episode：**必须剥掉**模型给的 subject（置 None）。

    RAG 侧对非 preference 带 subject 会直接报错 subject_not_allowed——本层
    重算为 None 是唯一正确行为；若把模型给的串透传，这条断言即红。
    """
    for kind in ("fact", "episode"):
        client = _FakeRagClient()
        run_rag_remember(
            _ctx(client),
            RagRememberArgs(content="某事实", kind=kind, subject="someone-else"),
        )
        assert _payload(client)["subject"] is None, f"kind={kind} 未剥掉幻觉 subject"


def test_remember_passes_content_kind_and_thread() -> None:
    """正文/类别/来源线程照常下发（只有 subject 是系统重算的）。"""
    client = _FakeRagClient()
    run_rag_remember(
        _ctx(client, thread_id="thread-xyz"),
        RagRememberArgs(content="塑粉按 kg 报价", kind="fact", source_note="口头确认"),
    )
    payload = _payload(client)
    assert payload["content"] == "塑粉按 kg 报价"
    assert payload["kind"] == "fact"
    assert payload["source_thread"] == "thread-xyz"
    assert payload["source_note"] == "口头确认"


# ---------------------------------------------------------------------------
# 2) recall：无条件注入账号
# ---------------------------------------------------------------------------


def test_recall_unconditionally_injects_account_subject() -> None:
    """recall 无论模型是否给 subject，一律注入登录账号（漏则偏好召不回）。"""
    client = _FakeRagClient()
    run_rag_recall(_ctx(client), RagRecallArgs(query="客户沟通偏好"))
    assert client.calls[0][0] == "recall"
    assert _payload(client)["subject"] == _ACCOUNT


def test_recall_overrides_hallucinated_subject() -> None:
    """recall：模型瞎给的 subject 被覆盖为登录账号。"""
    client = _FakeRagClient()
    run_rag_recall(
        _ctx(client), RagRecallArgs(query="偏好", subject="someone-else")
    )
    assert _payload(client)["subject"] == _ACCOUNT


def test_recall_omits_absent_optionals() -> None:
    """可选参数缺省时**不下发**：RAG 侧 recall 的 top_k 不接受 None（会 TypeError）。"""
    client = _FakeRagClient()
    run_rag_recall(_ctx(client), RagRecallArgs(query="偏好"))
    payload = _payload(client)
    assert "top_k" not in payload
    assert "kind" not in payload
    assert "since" not in payload
    assert "min_score" not in payload


def test_recall_forwards_explicit_optionals() -> None:
    """显式给出的可选参数原样下发。"""
    client = _FakeRagClient()
    run_rag_recall(
        _ctx(client),
        RagRecallArgs(query="偏好", top_k=3, kind="preference", since="2026-09-01", min_score=0.4),
    )
    payload = _payload(client)
    assert payload["top_k"] == 3
    assert payload["kind"] == "preference"
    assert payload["since"] == "2026-09-01"
    assert payload["min_score"] == 0.4


# ---------------------------------------------------------------------------
# 3) forget：两条路径的 subject 差异（有意的）
# ---------------------------------------------------------------------------


def test_forget_query_path_injects_account_subject() -> None:
    """query 批量路径 → subject=登录账号（把批量软删收紧在本账号内）。"""
    client = _FakeRagClient()
    run_rag_forget(_ctx(client), RagForgetArgs(query="过期偏好"))
    assert client.calls[0][0] == "forget"
    assert _payload(client)["subject"] == _ACCOUNT


def test_forget_memory_id_path_does_not_inject_subject() -> None:
    """memory_id 路径 → subject=None（注入会让目标行按字面等值过滤命中 0 行、静默失效）。"""
    client = _FakeRagClient()
    run_rag_forget(_ctx(client), RagForgetArgs(memory_id=42))
    payload = _payload(client)
    assert payload["memory_id"] == 42
    assert payload["subject"] is None


def test_forget_memory_id_path_ignores_hallucinated_subject() -> None:
    """memory_id 路径：模型瞎给 subject 也**不**透传（必须为 None）。"""
    client = _FakeRagClient()
    run_rag_forget(_ctx(client), RagForgetArgs(memory_id=42, subject="someone-else"))
    assert _payload(client)["subject"] is None


# ---------------------------------------------------------------------------
# 4) fail closed：未接线 / 无身份
# ---------------------------------------------------------------------------


def test_unwired_rag_fails_closed_without_calling() -> None:
    """rag_client=None → 返回错误、不触网（不静默成功）。"""
    ctx = ToolContext(context=AgentContext(user_id=_ACCOUNT, user_role="manager", scope=frozenset()), rag_client=None)
    result = run_rag_recall(ctx, RagRecallArgs(query="x"))
    assert result.ok is False
    assert "error" in result.summary


def test_missing_identity_fails_closed_without_calling() -> None:
    """请求身份缺失（context/ user_id 空）→ fail closed（不匿名写入）。"""
    client = _FakeRagClient()
    result = run_rag_remember(
        _ctx(client, user_id=None),
        RagRememberArgs(content="x", kind="preference"),
    )
    assert result.ok is False
    assert client.calls == []  # 未下发任何调用


# ---------------------------------------------------------------------------
# 5) RAG 工具失败时错误文本带上服务端 reason（不静默）
# ---------------------------------------------------------------------------


def test_rag_error_reason_surfaces() -> None:
    """RAG 侧 isError 的 details.reason 被拼进错误文本，便于模型自纠。"""
    from administrative_agent.mcp.errors import ErrorCategory, MCPError

    err = MCPError(
        code="VALIDATION_ERROR",
        category=ErrorCategory.VALIDATION,
        message="subject not allowed",
        details={"reason": "subject_not_allowed"},
    )
    client = _FakeRagClient(ToolResult(ok=False, error=err))
    result = run_rag_remember(
        _ctx(client), RagRememberArgs(content="x", kind="fact")
    )
    assert result.ok is False
    assert "subject_not_allowed" in result.summary["error"]


# ---------------------------------------------------------------------------
# 6) 未配置时不向模型暴露
# ---------------------------------------------------------------------------


def test_without_unconfigured_rag_tools_removes_all_rag() -> None:
    """rag_client=None → 全部 rag_*（含文档检索）从工具集剔除（模型看不到）。

    变异：``RAG_TOOL_NAMES`` 漏收 ``rag_search_documents`` → 该工具在未配置时
    仍裸露给模型（下面 ``- set(filtered) == set(RAG_TOOL_NAMES)`` 的等式即红）。
    """
    filtered = without_unconfigured_rag_tools(TOOL_REGISTRY, None)
    assert "rag_search_documents" in RAG_TOOL_NAMES  # 文档检索必须在剔除名单内
    assert RAG_TOOL_NAMES.isdisjoint(filtered)
    assert set(TOOL_REGISTRY) - set(filtered) == set(RAG_TOOL_NAMES)


def test_without_unconfigured_rag_tools_keeps_all_when_wired() -> None:
    """已配置 → 原样返回同一对象（零拷贝，零行为变更）。"""
    tools = {"rag_recall": TOOL_REGISTRY["rag_recall"], "erp_query": TOOL_REGISTRY["erp_query"]}
    assert without_unconfigured_rag_tools(tools, _FakeRagClient()) is tools


# ---------------------------------------------------------------------------
# 7) 注册表形态：只读路径、非 ERP 写工具、无规则包
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(RAG_TOOL_NAMES))
def test_rag_tools_registered_as_readonly_memory(name: str) -> None:
    """RAG 工具在注册表中为只读路径（无确认卡/不建任务/不落 ERP 审计）。

    ``RAG_TOOL_NAMES`` 现含 4 个（记忆三工具 + ``rag_search_documents``）；
    参数化即逐个守护它们在注册表里的形态一致。
    """
    spec = TOOL_REGISTRY[name]
    assert spec.is_write is False
    assert spec.prepare is None
    assert spec.policy_family == ()
    assert spec.duty_code == "MEM"
    assert spec.run.__name__ == f"run_{name}"


def test_rag_search_documents_in_office_toolset() -> None:
    """文档检索工具必须进 OFFICE 档（否则 import 期 G3 断言会直接红）。

    这里显式再钉一次：OFFICE 是手写清单，G3 断言保证 OFFICE == 注册表全集；
    本用例把「工具确实登记且被 OFFICE 收录」的直接证据留在测试里。
    """
    from administrative_agent.agent.toolsets import AGENT_TOOLSETS, AgentKind

    assert "rag_search_documents" in TOOL_REGISTRY
    assert "rag_search_documents" in AGENT_TOOLSETS[AgentKind.OFFICE]


def test_rag_tools_are_not_erp_write_tools() -> None:
    """绝不加进 mcp/client.py 的 _WRITE_TOOLS（RAG 不是 ERP 写语义）。"""
    from administrative_agent.mcp.client import _WRITE_TOOLS

    assert RAG_TOOL_NAMES.isdisjoint(_WRITE_TOOLS)


# ---------------------------------------------------------------------------
# 8) 端到端装配：RuntimeBag.rag_client → _tool_ctx → 工具包装层 → MCP payload
#
# 钉住真正易断的一环：RuntimeBag 里注入的 rag_client 是否真流到工具上下文
# （configuration._tool_ctx），且经 tools_node 分派后账号仍由 ctx 注入。
# ---------------------------------------------------------------------------


def test_tools_node_dispatch_injects_account_into_mcp_payload() -> None:
    """走真 tools_node 分派 rag_recall：下发的 payload.subject = 登录账号。"""
    from administrative_agent.agent.configuration import RuntimeBag
    from administrative_agent.agent.nodes.tools_node import tools_node

    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [], "hit_count": 0})
    )
    runtime = RuntimeBag(
        tools=TOOL_REGISTRY,
        rag_client=client,
        context=AgentContext(
            user_id=_ACCOUNT, user_role="manager", scope=frozenset(), thread_id="t-9"
        ),
    )
    state = {
        "pending_action": {
            "tool_name": "rag_recall",
            "tool_arguments": {"query": "客户沟通偏好"},
        },
        "messages": [],
    }
    tools_node(state, runtime)  # type: ignore[arg-type]

    assert client.calls, "tools_node 未把 rag_recall 分派到 RAG 客户端"
    assert client.calls[0][0] == "recall"
    assert client.calls[0][1]["subject"] == _ACCOUNT


# ---------------------------------------------------------------------------
# 9) F1：remember/forget 成功补 agent 侧审计（谁/哪条记忆/做了什么）
# ---------------------------------------------------------------------------


def test_remember_records_audit_with_actor_and_memory_id() -> None:
    """成功 remember → 落一条审计，字段 = 谁（账号）/哪条（memory_id）/什么操作。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"memory_id": 7, "action": "created", "similar": []})
    )
    result = run_rag_remember(
        _ctx(client, audit=audit, thread_id="t-42"),
        RagRememberArgs(content="客户偏好微信", kind="preference"),
    )
    assert result.ok is True
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["operation"] == "remember"
    assert call["actor_account"] == _ACCOUNT          # 谁
    assert call["trace_id"] == "thread:t-42"          # 何时/哪轮（created_at 由仓储落库）
    assert call["memory_id"] == 7                     # 哪条记忆
    assert call["subject"] == _ACCOUNT                # 归属（preference → 账号）
    assert call["kind"] == "preference"


def test_forget_records_audit_with_affected_and_ids() -> None:
    """成功 forget → 审计带 affected 与 memory_ids（批量软删的关键信息）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"affected": 2, "memory_ids": [3, 5]})
    )
    run_rag_forget(
        _ctx(client, audit=audit), RagForgetArgs(query="过期偏好")
    )
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["operation"] == "forget"
    assert call["affected"] == 2
    assert call["memory_ids"] == [3, 5]
    assert call["actor_account"] == _ACCOUNT


def test_failed_rag_call_records_no_audit() -> None:
    """RAG 侧失败（未改变存储）→ 不落审计（只记成功的事实）。"""
    audit = _FakeAudit()
    err = MCPError(code="BUSINESS_ERROR", category=ErrorCategory.VALIDATION, message="x")
    client = _FakeRagClient(ToolResult(ok=False, error=err))
    result = run_rag_remember(
        _ctx(client, audit=audit), RagRememberArgs(content="x", kind="fact")
    )
    assert result.ok is False
    assert audit.calls == []


def test_audit_failure_reports_error_not_success() -> None:
    """审计写入失败 → 不得假报成功（10.4），错误文本带上已生效的 memory_id。"""
    audit = _FakeAudit(raises=True)
    client = _FakeRagClient(
        ToolResult(ok=True, data={"memory_id": 9, "action": "created", "similar": []})
    )
    result = run_rag_remember(
        _ctx(client, audit=audit), RagRememberArgs(content="x", kind="preference")
    )
    assert result.ok is False
    assert "9" in result.summary["error"]
    assert result.summary["memory_id"] == 9


# ---------------------------------------------------------------------------
# 9b) [2026-09-23] 纯读 recall 补 agent 侧审计（EVENT_RAG_RECALL）：
#     谁/条件/命中几何；失败语义与写路径**刻意不同**（读工具审计失败仍返回）
# ---------------------------------------------------------------------------


def test_recall_records_audit_with_conditions_and_hit_ids() -> None:
    """成功召回 → 落一条审计：条件（top_k/kind/since）+ 命中几何（ids）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(
            ok=True,
            data={
                "hits": [
                    {"memory_id": 42, "content": "偏好微信"},
                    {"memory_id": 43, "content": "偏好电话"},
                ],
                "hit_count": 2,
            },
        )
    )
    result = run_rag_recall(
        _ctx(client, audit=audit, thread_id="t-r9"),
        RagRecallArgs(query="客户沟通偏好", top_k=5, kind="preference", since="2026-09-01"),
    )
    assert result.ok is True
    assert len(audit.recall_calls) == 1
    call = audit.recall_calls[0]
    assert call["actor_account"] == _ACCOUNT          # 谁
    assert call["trace_id"] == "thread:t-r9"
    assert call["top_k"] == 5
    assert call["kind"] == "preference"
    assert call["since"] == "2026-09-01"
    assert call["hit_count"] == 2
    assert call["memory_ids"] == [42, 43]
    assert call["query_chars"] == len("客户沟通偏好")


def test_recall_zero_hit_still_records_audit() -> None:
    """零命中也要记——「查了没命中」本身可观测（区分"没调"）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [], "hit_count": 0})
    )
    result = run_rag_recall(_ctx(client, audit=audit), RagRecallArgs(query="无命中"))
    assert result.ok is True
    assert len(audit.recall_calls) == 1
    assert audit.recall_calls[0]["hit_count"] == 0
    assert audit.recall_calls[0]["memory_ids"] == []


def test_recall_audit_records_min_score() -> None:
    """模型显式设的 min_score 须入审计（否则「下限致 0 命中」与"没命中"分不出）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [], "hit_count": 0})
    )
    run_rag_recall(
        _ctx(client, audit=audit), RagRecallArgs(query="偏好", min_score=0.4)
    )
    assert audit.recall_calls[0]["min_score"] == 0.4


def test_truncated_recall_audit_shows_total_and_id_subset() -> None:
    """会被尺寸裁剪的召回（≥20 条大 content）：审计须能看出「命中总数」与
    「ids 是前缀子集」这件事——钉**字段值**，不是「函数被调用过」。

    ``_bounded_result`` 故意保留顶层 ``hit_count`` 为服务端原值，却从命中列表
    尾部丢弃条目；审计若不落 ``truncated``，事后读者会把「命中 20、只记了前
    N 条」误读成「只命中 N 条」。
    """
    audit = _FakeAudit()
    client = _FakeRagClient(ToolResult(ok=True, data=_big_recall_data(20, 1000)))
    result = run_rag_recall(
        _ctx(client, audit=audit), RagRecallArgs(query="客户偏好", top_k=20)
    )
    assert result.ok is True
    assert result.summary["truncated"] is True       # 信封确实被裁剪过
    assert len(audit.recall_calls) == 1
    call = audit.recall_calls[0]
    ids = call["memory_ids"]
    assert call["hit_count"] == 20                    # 服务端命中总数（裁剪前）
    assert 0 < len(ids) < 20                          # ids 只记了前缀一部分
    assert ids == list(range(len(ids)))               # 确为按命中序的前缀
    assert call["truncated"] is True                  # 该差异有说明（非矛盾）


def test_untruncated_recall_audit_marks_not_truncated() -> None:
    """未截断的召回：truncated=False 且 ids 即全部命中（回归对照）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(
            ok=True,
            data={
                "hits": [
                    {"memory_id": 42, "content": "偏好微信"},
                    {"memory_id": 43, "content": "偏好电话"},
                ],
                "hit_count": 2,
            },
        )
    )
    run_rag_recall(_ctx(client, audit=audit), RagRecallArgs(query="偏好", top_k=5))
    call = audit.recall_calls[0]
    assert call["truncated"] is False
    assert call["hit_count"] == 2
    assert call["memory_ids"] == [42, 43]


def test_recall_audit_never_carries_query_text() -> None:
    """**不落 query 正文**：审计实参里任何位置都不得出现原查询串。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [{"memory_id": 1, "content": "x"}], "hit_count": 1})
    )
    private = "账户密码相关的私密查询文本SENTINEL"
    run_rag_recall(_ctx(client, audit=audit), RagRecallArgs(query=private))
    assert len(audit.recall_calls) == 1
    dumped = json.dumps(audit.recall_calls[0], ensure_ascii=False)
    assert private not in dumped                       # 正文绝不出现在审计实参
    assert audit.recall_calls[0]["query_chars"] == len(private)


def test_recall_audit_failure_does_not_fail_recall() -> None:
    """**失败语义（刻意差异）**：审计写入失败，召回仍返回 hits、ok=True。

    读工具无副作用可回滚、结果已在手，故不让审计拖挂召回（与写路径 10.4
    「不假报成功」不冲突——后者前提是"有状态改变"）。
    """
    audit = _FakeAudit(raises=True)
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [{"memory_id": 7, "content": "m"}], "hit_count": 1})
    )
    result = run_rag_recall(_ctx(client, audit=audit), RagRecallArgs(query="x"))
    assert result.ok is True                           # 不 fail
    assert result.summary["hits"][0]["memory_id"] == 7 # hits 照常返回
    assert len(audit.recall_calls) == 1                # 确实尝试记过


def test_failed_recall_records_no_audit() -> None:
    """RAG 侧调用失败（没有可观测的事发生）→ 不落 recall 审计。"""
    audit = _FakeAudit()
    err = MCPError(code="BUSINESS_ERROR", category=ErrorCategory.VALIDATION, message="x")
    client = _FakeRagClient(ToolResult(ok=False, error=err))
    result = run_rag_recall(_ctx(client, audit=audit), RagRecallArgs(query="x"))
    assert result.ok is False
    assert audit.recall_calls == []


# ---------------------------------------------------------------------------
# 10) F2：服务端声明的 retryable 被保留（仅只读工具；写工具一票否决）
# ---------------------------------------------------------------------------


def test_known_code_honors_server_retryable_for_read_tool() -> None:
    """BUSINESS_ERROR + retryable=true（RAG embedding_unavailable）→ 只读工具可重试。"""
    sc = {
        "code": "BUSINESS_ERROR",
        "message": "嵌入服务不可用",
        "retryable": True,
        "details": {"reason": "embedding_unavailable"},
    }
    err = MCPError.from_structured_content(sc, "recall")
    assert err.retryable is True
    assert _should_retry(err, "recall") is True


def test_known_code_retry_true_blocked_for_erp_write_tool() -> None:
    """写工具（ERP）即便服务端声明 true 也不自动重试（13.2，防误声明）。"""
    sc = {"code": "BUSINESS_ERROR", "message": "x", "retryable": True, "details": {}}
    err = MCPError.from_structured_content(sc, "create_order_bundle")
    assert _should_retry(err, "create_order_bundle") is False


def test_erp_business_error_stays_non_retryable() -> None:
    """ERP 回归：BUSINESS_ERROR + retryable=false → 只读工具也不重试（行为不变）。"""
    sc = {"code": "BUSINESS_ERROR", "message": "x", "retryable": False, "details": {}}
    err = MCPError.from_structured_content(sc, "erp_query")
    assert err.retryable is False
    assert _should_retry(err, "erp_query") is False


def test_known_code_without_declared_retryable_falls_back_false() -> None:
    """服务端未声明 retryable → 回退缺省 false（保守 fail closed）。"""
    err = MCPError.from_structured_content({"code": "VALIDATION_ERROR", "message": "x"})
    assert err.retryable is False


# ---------------------------------------------------------------------------
# 11) F5：兜底文案按客户端归属参数化（RAG 不被说成 ERP）
# ---------------------------------------------------------------------------


def test_unstructured_fallback_labels_rag_not_erp() -> None:
    err = _unstructured_error(
        [{"type": "text", "text": "boom"}], server_label="RAG"
    )
    assert "RAG tool failed without a structured error code" in err.message
    assert "ERP" not in err.message


def test_unknown_code_message_labels_rag_not_erp() -> None:
    err = MCPError.from_structured_content(
        {"code": "SOME_NEW_CODE", "message": "x"}, server_label="RAG"
    )
    assert "Unknown RAG error code" in err.message
    assert "ERP" not in err.message


def test_fallback_default_label_is_erp() -> None:
    """缺省归属仍是 ERP（既有文案逐字节不变）。"""
    err = _unstructured_error([{"type": "text", "text": "boom"}])
    assert "ERP tool failed without a structured error code" in err.message


# ---------------------------------------------------------------------------
# 12) G1：RAG 专用超时/重试（ERP 旋钮逐字节不变）
# ---------------------------------------------------------------------------


def test_rag_client_uses_rag_timeouts_and_retry_budget() -> None:
    """RAG 客户端取 rag_mcp_* 旋钮，且**生效值**为 3/30/30/3（实测重定后）。"""
    settings = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_connect_timeout=3.0,
        rag_mcp_read_timeout=30.0,
        rag_mcp_total_timeout=66.0,
        rag_mcp_max_retries=1,
    )
    client = build_rag_mcp_client(settings)
    assert client._max_retries == 1
    assert client._server_label == "RAG"
    # 收敛公式：connect=min(3, 66/4=16.5)=3；read=min(30,(66-6)/2=30)=30。
    # 断言**生效值**（httpx.Timeout 四组件），不是配置对象——配置值与期望值
    # 相等时"配置接管了"与"配置被完全忽略"是同一断言，会假绿（本仓教训）。
    assert client._timeout.connect == 3.0
    assert client._timeout.read == 30.0
    assert client._timeout.write == 30.0
    assert client._timeout.pool == 3.0
    # 直接对收敛函数再钉一次生效值（绕开客户端实例的行间耦合）
    t = _build_rag_timeout(settings)
    assert (t.connect, t.read, t.write, t.pool) == (3.0, 30.0, 30.0, 3.0)


def test_rag_timeout_effective_values_follow_settings_not_hardcoded() -> None:
    """生效值**随配置变化**（P2 弱判据修复：钉住「配置真的接管了」）。

    上一条用例传的配置值恰好等于期望生效值（connect=3/read=30 → 生效 3/30），
    于是「收敛函数完全忽略 settings、硬编码 3/30/30/3」也能全绿（假绿）。
    本条把配置与硬编码基线**拉开**，令忽略配置的实现必然对不上：

    - 放宽：connect=7 / read=20 / total=66 → 生效 connect=min(7, 66/4=16.5)=7
      （≠硬编码 3）、read=min(20, (66-14)/2=26)=20（≠硬编码 30）；
    - total 改小：connect=3 / read=30 / total=20 → 生效 read=min(30,
      (20-6)/2=7)=7（≠硬编码 30）——「配置改小 → 生效值跟着改小」。

    任一硬编码 3/30 的实现都会在下面任一条断言上变红。
    """
    wide = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_connect_timeout=7.0,
        rag_mcp_read_timeout=20.0,
        rag_mcp_total_timeout=66.0,
        rag_mcp_max_retries=1,
    )
    t = _build_rag_timeout(wide)
    assert (t.connect, t.read, t.write, t.pool) == (7.0, 20.0, 20.0, 7.0)
    # 同一读数经真实客户端实例再钉一次（绕开收敛函数与客户端的行间耦合）
    client = build_rag_mcp_client(wide)
    assert client._timeout.connect == 7.0
    assert client._timeout.read == 20.0

    tight = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_connect_timeout=3.0,
        rag_mcp_read_timeout=30.0,
        rag_mcp_total_timeout=20.0,
        rag_mcp_max_retries=1,
    )
    t2 = _build_rag_timeout(tight)
    assert (t2.connect, t2.read, t2.write, t2.pool) == (3.0, 7.0, 7.0, 3.0)


def test_erp_client_timeout_retries_unchanged() -> None:
    """ERP 客户端仍取 erp_mcp_* 旋钮，四组件**生效值**逐项不变（G1 不改变 ERP）。"""
    settings = Settings(
        erp_mcp_url="http://erp:8000",
        erp_mcp_connect_timeout=5.0,
        erp_mcp_read_timeout=30.0,
        erp_mcp_total_timeout=60.0,
        erp_mcp_max_retries=3,
    )
    client = MCPClient(settings)
    assert client._max_retries == 3
    assert client._server_label == "ERP"
    # 收敛公式：connect=min(5, 60/4=15)=5；read=min(30,(60-10)/2=25)=25。
    # 钉生效值（不是 settings.erp_mcp_*），否则改回读配置对象即同一条断言。
    assert client._timeout.connect == 5.0
    assert client._timeout.read == 25.0
    assert client._timeout.write == 25.0
    assert client._timeout.pool == 5.0


# ---------------------------------------------------------------------------
# 12b) G1-②：rag_remember 无幂等键 → 绝不自动重试；recall/forget 行为不变
# ---------------------------------------------------------------------------


def _retryable_business_error(tool_name: str) -> MCPError:
    """构造 RAG 服务端声明的可重试业务错误（embedding_unavailable）。"""
    return MCPError.from_structured_content(
        {
            "code": "BUSINESS_ERROR",
            "message": "嵌入服务不可用",
            "retryable": True,
            "details": {"reason": "embedding_unavailable"},
        },
        tool_name,
        server_label="RAG",
    )


def test_should_retry_never_retries_remember_by_mcp_name() -> None:
    """`_should_retry` 收到的 tool_name 是 MCP 层名 "remember"（非模型可见的
    rag_remember）——即便服务端声明 retryable=true 也必须 False。"""
    assert "remember" in _NEVER_RETRY_TOOLS
    assert _should_retry(_retryable_business_error("remember"), "remember") is False
    # 连接失败（原本读写都重试）也要被顶部闸门挡住
    err = MCPError.from_transport(_connect_error(), server_label="RAG")
    assert _should_retry(err, "remember") is False


def test_remember_mcp_tool_name_is_keyed_in_never_retry() -> None:
    """耦合钉子：run_rag_remember 真正下发的 MCP 工具名必须落在 _NEVER_RETRY_TOOLS。

    防止「常量写成 rag_remember（模型可见名）→ 闸门永不命中」这一类静默失效。
    """
    client = _FakeRagClient()
    run_rag_remember(
        _ctx(client),
        RagRememberArgs(content="客户偏好微信", kind="preference"),
    )
    assert client.calls[0][0] in _NEVER_RETRY_TOOLS


def test_rag_remember_is_not_retried_end_to_end(monkeypatch) -> None:
    """端到端：真实 RAG 客户端收到可重试错误时，remember 只尝试一次。"""
    settings = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_connect_timeout=3.0,
        rag_mcp_read_timeout=30.0,
        rag_mcp_total_timeout=66.0,
        rag_mcp_max_retries=1,
    )
    client = build_rag_mcp_client(settings)
    err = _retryable_business_error("remember")
    seen: list[str] = []

    def fake_impl(self, tool_name, arguments, operation_id, trace_id):  # noqa: ANN001
        seen.append(tool_name)
        return ToolResult(ok=False, error=err)

    monkeypatch.setattr(MCPClient, "_call_tool_impl", fake_impl)
    result = client.call_tool("remember", {"content": "x", "kind": "fact"})

    assert seen == ["remember"], f"remember 被重试了：{seen}"
    assert result.ok is False


def test_rag_recall_still_retried_end_to_end(monkeypatch) -> None:
    """对照组：只读 recall 的 retryable 行为**不变**（同一错误仍重试一次）。"""
    settings = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_read_timeout=30.0,
        rag_mcp_total_timeout=66.0,
        rag_mcp_max_retries=1,
    )
    client = build_rag_mcp_client(settings)
    err = _retryable_business_error("recall")
    seen: list[str] = []

    def fake_impl(self, tool_name, arguments, operation_id, trace_id):  # noqa: ANN001
        seen.append(tool_name)
        return ToolResult(ok=False, error=err)

    monkeypatch.setattr(MCPClient, "_call_tool_impl", fake_impl)
    # 重试前有 1s 指数退避（可接受的一次性等待）
    client.call_tool("recall", {"query": "x"})

    assert seen == ["recall", "recall"], f"recall 未被重试：{seen}"


def test_rag_forget_still_retried_end_to_end(monkeypatch) -> None:
    """对照组：forget（memory_id 路径幂等）的 retryable 行为**不变**。"""
    settings = Settings(
        rag_mcp_url="http://rag:8000",
        rag_mcp_api_key="k",
        rag_mcp_read_timeout=30.0,
        rag_mcp_total_timeout=66.0,
        rag_mcp_max_retries=1,
    )
    client = build_rag_mcp_client(settings)
    err = _retryable_business_error("forget")
    seen: list[str] = []

    def fake_impl(self, tool_name, arguments, operation_id, trace_id):  # noqa: ANN001
        seen.append(tool_name)
        return ToolResult(ok=False, error=err)

    monkeypatch.setattr(MCPClient, "_call_tool_impl", fake_impl)
    client.call_tool("forget", {"memory_id": 42, "subject": None})

    assert seen == ["forget", "forget"], f"forget 未被重试：{seen}"


def _connect_error() -> Exception:
    import httpx

    return httpx.ConnectError("boom")


# ---------------------------------------------------------------------------
# 13) F4：半配置（url 有、key 空）不接线 → 工具不进模型可见集
# ---------------------------------------------------------------------------


def _rag_settings(**overrides: Any) -> Any:
    from types import SimpleNamespace

    base = {
        "rag_mcp_url": "http://rag:8000",
        "rag_mcp_api_key": "secret",
        "rag_mcp_connect_timeout": 3.0,
        "rag_mcp_read_timeout": 30.0,
        "rag_mcp_total_timeout": 66.0,
        "rag_mcp_max_retries": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_wire_rag_client_half_config_not_wired(monkeypatch) -> None:
    """配了 url 漏 key → rag_client 置 None（不暴露必然失败的三工具）。"""
    from types import SimpleNamespace

    from administrative_agent import main as main_mod

    monkeypatch.setattr(main_mod, "settings", _rag_settings(rag_mcp_api_key=""))
    app = SimpleNamespace(state=SimpleNamespace())
    main_mod._wire_rag_client(app)
    assert app.state.rag_client is None


def test_wire_rag_client_full_config_wired(monkeypatch) -> None:
    """url 与 key 都有 → 接线 RAG 客户端（工具进模型可见集）。"""
    from types import SimpleNamespace

    from administrative_agent import main as main_mod

    monkeypatch.setattr(main_mod, "settings", _rag_settings())
    app = SimpleNamespace(state=SimpleNamespace())
    main_mod._wire_rag_client(app)
    assert isinstance(app.state.rag_client, MCPClient)


def test_wire_mcp_client_erp_still_url_only(monkeypatch) -> None:
    """ERP 侧仍只看 url（erp_mcp_api_key 可空而 jwt 有效，不收紧）。"""
    from types import SimpleNamespace

    from administrative_agent import main as main_mod

    fake = SimpleNamespace(
        erp_mcp_url="http://erp:8000",
        erp_mcp_api_key="",
        erp_mcp_metadata_ttl=3600,
        erp_mcp_connect_timeout=5.0,
        erp_mcp_read_timeout=30.0,
        erp_mcp_total_timeout=60.0,
        erp_mcp_max_retries=3,
    )
    monkeypatch.setattr(main_mod, "settings", fake)
    app = SimpleNamespace(state=SimpleNamespace())
    main_mod._wire_mcp_client(app)
    assert isinstance(app.state.mcp_client, MCPClient)


# ---------------------------------------------------------------------------
# 14) P1：RAG 结果尺寸收口——>4096 的 recall 必须是合法 JSON（不被 4096 腰斩）
#
# RAG 结果无 items 键，走 tools_node.summarize_tool_result 的**非明细分支**：
# 超限会被裸字符串从中间腰斩成非法 JSON。包装层（_bounded_result）把信封收到
# RAG_RESULT_CHARS(<4096)，使该兜底永不触发。判据钉在**真实消费点**上。
# ---------------------------------------------------------------------------


def _big_recall_data(n: int = 20, content_len: int = 1000) -> dict[str, Any]:
    """>4096 的 recall 载荷（20 条 × 1000 字 content ≈ 20KB，真实规模上限）。"""
    return {
        "hits": [
            {
                "memory_id": i,
                "kind": "fact",
                "score": 0.5,
                "content": f"h{i}-" + "x" * content_len,
            }
            for i in range(n)
        ],
        "hit_count": n,
    }


def test_large_recall_result_is_valid_json_and_marks_truncation() -> None:
    """>4096 的 recall：信封 ≤ 预算、是合法 JSON、带 truncated 标记、可过消费点。

    真判据：**真的构造 >4096（~20KB）的载荷**，并跑真实消费点
    ``tools_node.summarize_tool_result``（RAG 结果走其非明细分支）。若包装层
    不收口（变异：去掉 ``_bounded_result`` 调用），信封 ~20KB → summarize 走
    ``content[:4096]`` 裸截断 → 下面 ``json.loads(summarized)`` 抛
    ``Unterminated string`` → 本用例必红。短载荷恒过，故必须用大载荷。
    """
    from administrative_agent.agent.nodes.tools_node import (
        TOOL_RESULT_HISTORY_CHARS,
        summarize_tool_result,
    )
    from administrative_agent.agent.tools.rag_memory import RAG_RESULT_CHARS

    client = _FakeRagClient(ToolResult(ok=True, data=_big_recall_data()))
    result = run_rag_recall(_ctx(client), RagRecallArgs(query="客户偏好", top_k=20))
    assert result.ok is True

    envelope = json.dumps({"ok": True, **result.summary}, ensure_ascii=False)
    # 未超预算，且严格小于 4096 兜底阈值（否则消费点会腰斩）
    assert len(envelope) <= RAG_RESULT_CHARS
    assert len(envelope) < TOOL_RESULT_HISTORY_CHARS
    # 模型可判定「被截断」；服务端总命中数保留（可据此调小 top_k 重试）
    assert result.summary["truncated"] is True
    assert result.summary["hit_count"] == 20
    assert any(
        isinstance(hit, dict) and str(hit.get("content", "")).endswith("…（截断）")
        for hit in result.summary["hits"]
    ), "逐条 content 未被截断并标记"

    # 真实消费点：summarize 原样返回（合法 JSON，未被 4096 腰斩）
    summarized = summarize_tool_result(envelope, tool_name="rag_recall")
    parsed = json.loads(summarized)  # 变异后此处抛 Unterminated string
    assert parsed["truncated"] is True
    assert summarized == envelope  # 未触发兜底改写（逐字节一致）


def test_small_recall_result_is_untouched() -> None:
    """小结果原样透传：不加 truncated、条目不被改写（回归守护）。"""
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [{"memory_id": 1, "content": "短记忆"}], "hit_count": 1})
    )
    result = run_rag_recall(_ctx(client), RagRecallArgs(query="x"))
    assert result.ok is True
    assert "truncated" not in result.summary
    assert result.summary["hits"] == [{"memory_id": 1, "content": "短记忆"}]


def test_forget_many_ids_result_stays_bounded_and_valid() -> None:
    """forget 大批量 memory_ids：同样收口为合法 JSON（不被 4096 腰斩）。"""
    from administrative_agent.agent.nodes.tools_node import summarize_tool_result

    data = {"affected": 800, "memory_ids": list(range(800))}
    client = _FakeRagClient(ToolResult(ok=True, data=data))
    result = run_rag_forget(_ctx(client), RagForgetArgs(query="过期偏好"))
    assert result.ok is True
    envelope = json.dumps({"ok": True, **result.summary}, ensure_ascii=False)
    assert len(envelope) < 4096
    assert result.summary["truncated"] is True
    assert result.summary["affected"] == 800  # 顶层计数保留
    parsed = json.loads(summarize_tool_result(envelope, tool_name="rag_forget"))
    assert parsed["truncated"] is True


# ---------------------------------------------------------------------------
# 15) P6-a：审计失败 summary 也过尺寸预算（不得绕过 -> 被 4096 腰斩丢告警）
#
# 审计失败分支的手搓 summary 曾绕过 _bounded_result，且把**已贴预算上限**的
# data（forget 批量 800 条 memory_ids）原样再带一遍 + 无界 {exc}（底层
# SQLAlchemy DBAPI 异常默认带完整 [SQL: ...][parameters: ...]，数百到上千
# 字符）。真判据：构造「批量命中 800 + 审计写入失败 + ~660 字符异常」，跑
# **真实消费点** summarize_tool_result，断言 json.loads 成功、ok=False、
# 告警文本完整、信封 < TOOL_RESULT_HISTORY_CHARS。
# ---------------------------------------------------------------------------


def _long_audit_exception() -> str:
    """模拟底层 SQLAlchemy 审计写失败异常（带 [SQL: ...][parameters: ...]）。"""
    sql = "INSERT INTO audit_events (event) VALUES (%(event)s)"
    params = ", ".join(f"'mem-{i}': {i}" for i in range(30))
    return (
        "(psycopg.errors.UniqueViolation) duplicate key value violates unique "
        f"constraint \"audit_events_pkey\"\n[SQL: {sql}]\n[parameters: {{{params}}}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )


def test_audit_failure_summary_is_bounded_and_alert_kept() -> None:
    """审计失败 + 大批量 + 长异常：信封 < 4096、是合法 JSON、告警文本完整。

    变异验收（去掉失败分支的 ``_bounded_result`` 收口 **或** 把异常文本改回
    无界 ``{exc}``）：信封 ~4.6KB → 消费点 ``content[:4096]`` 裸截断 → 下面
    ``json.loads(summarized)`` 抛 ``JSONDecodeError`` → 本用例必红，且断言
    「告警文本完整」也会因腰斩而失败。
    """
    from administrative_agent.agent.nodes.tools_node import (
        TOOL_RESULT_HISTORY_CHARS,
        summarize_tool_result,
    )
    from administrative_agent.agent.tools.rag_memory import RAG_RESULT_CHARS

    exc_text = _long_audit_exception()
    # 字面阈值（**不**引用 _AUDIT_ERROR_CHARS：否则变异该常量会让本守卫
    # 先红，把「真判据红」掩盖成「守卫红」——自指耦合）。
    assert len(exc_text) > 600  # 确实是「长异常」，非短文本

    audit = _FakeAudit(raises=True, exc_message=exc_text)
    client = _FakeRagClient(
        ToolResult(ok=True, data={"affected": 800, "memory_ids": list(range(800))})
    )
    result = run_rag_forget(
        _ctx(client, audit=audit), RagForgetArgs(query="过期偏好")
    )

    # 不假报成功（10.4）：记忆已软删但审计缺失
    assert result.ok is False
    envelope = json.dumps({"ok": False, **result.summary}, ensure_ascii=False)
    assert len(envelope) <= RAG_RESULT_CHARS
    assert len(envelope) < TOOL_RESULT_HISTORY_CHARS

    # 告警正文完整保留（人可据此定位「需人工复核」），异常文本被截断封顶
    error_text = result.summary["error"]
    assert "长期记忆已forget" in error_text
    assert "但本地审计写入失败，需人工复核" in error_text
    assert exc_text not in error_text, "长异常文本未被封顶（仍全量拼入）"
    assert error_text.endswith("…（截断）")

    # 顶层计数保留（模型仍知道总删数），memory_ids 被逐条收口
    assert result.summary["affected"] == 800
    assert result.summary["truncated"] is True

    # 真实消费点：原样返回合法 JSON（变异后此处抛 JSONDecodeError）
    summarized = summarize_tool_result(envelope, tool_name="rag_forget")
    parsed = json.loads(summarized)
    assert parsed["ok"] is False
    assert "但本地审计写入失败，需人工复核" in parsed["error"]
    assert summarized == envelope  # 未触发 4096 兜底改写（逐字节一致）

    # 第二场景（remember 路径）：data 只有标量 + 小列表（无 memory_ids）时，
    # 失败 summary 只保留 error + memory_id/memory_ids/affected 三标量，同样
    # < 4096、告警完整——钉住「任何输入下告警文本都完整保留」的硬要求。
    big_similar = [
        {"memory_id": i, "content": "y" * 1000} for i in range(20)
    ]
    audit2 = _FakeAudit(raises=True, exc_message=exc_text)
    client2 = _FakeRagClient(
        ToolResult(
            ok=True,
            data={"memory_id": 42, "action": "created", "similar": big_similar},
        )
    )
    res2 = run_rag_remember(
        _ctx(client2, audit=audit2),
        RagRememberArgs(content="客户偏好微信", kind="preference"),
    )
    assert res2.ok is False
    env2 = json.dumps({"ok": False, **res2.summary}, ensure_ascii=False)
    assert len(env2) < TOOL_RESULT_HISTORY_CHARS
    parsed2 = json.loads(summarize_tool_result(env2, tool_name="rag_remember"))
    assert parsed2["ok"] is False
    assert "但本地审计写入失败，需人工复核" in parsed2["error"]
    assert exc_text not in parsed2["error"]


def test_error_result_uses_no_list_fallback_note() -> None:
    """P6-c：无列表键的错误结果超预算时，兜底文案**不给 top_k 建议**。

    错误结果（``list_keys=()``）没有可逐条丢的条目，超预算只可能是错误文本
    过长；复用「可调小 top_k 重试」方向错误。用一个超长（> 预算）的错误文本
    逼出 ``_bounded_result`` 的最终兜底，断言用的是错误专用文案。
    """
    from administrative_agent.agent.tools import rag_memory as rm

    huge = {"error": "长期记忆服务调用失败：" + "e" * (rm.RAG_RESULT_CHARS * 2)}
    bounded = rm._bounded_result(huge, ok=False, list_keys=())
    assert bounded["note"] == rm._FINAL_NOTE_NO_LIST
    assert "top_k" not in bounded["note"]
    assert "错误信息过长" in bounded["note"]
    assert len(rm._envelope(bounded, ok=False)) < 4096


# ---------------------------------------------------------------------------
# 16) 文档检索工具 rag_search_documents（对接 RAG 侧 search_documents）
#
# 覆盖：注册/OFFICE 收录、参数边界、**成功读 content[0].text 与失败读
# structuredContent 两条通道**、空 hits 判成功、未配置时被剔除、payload 形态
# （不下发缺省 top_k、无 subject）、审计（EVENT_RAG_SEARCH）。
# ---------------------------------------------------------------------------


def _doc_hit(chunk_id: int = 1, document_id: int = 10) -> dict[str, Any]:
    """一条文档命中的**契约 12 键**（与 RAG 侧 ``_document_hit_to_dict`` 对齐）。"""
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": "员工手册",
        "source_uri": "file:///docs/handbook.pdf",
        "heading_text": "第三章 报销",
        "line_start": 12,
        "line_end": 20,
        "page_start": 3,
        "page_end": 3,
        "char_count": 120,
        "score": 0.83,
        "content": "报销需附发票……",
    }


# --- 16a) 参数边界：query 空/超长、top_k 0/21 报错；None/合法值放行 -------


def test_search_documents_query_empty_rejected() -> None:
    """query 空串 → Pydantic 校验报错（不截断、不静默）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RagSearchDocumentsArgs(query="")


def test_search_documents_query_too_long_rejected() -> None:
    """query 501 字 → 报错（1..500，超长不截断）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RagSearchDocumentsArgs(query="x" * 501)


def test_search_documents_query_boundary_500_ok() -> None:
    """query 恰好 500 字 → 放行（边界含上界）。"""
    assert len(RagSearchDocumentsArgs(query="x" * 500).query) == 500


@pytest.mark.parametrize("bad", [0, 21, -1])
def test_search_documents_top_k_out_of_range_rejected(bad: int) -> None:
    """top_k 0/21/-1 → 报错（1..20，越界不夹取）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RagSearchDocumentsArgs(query="报销", top_k=bad)


def test_search_documents_top_k_none_allowed() -> None:
    """top_k 缺省 None 合法（交给 RAG 侧取默认 5）。"""
    assert RagSearchDocumentsArgs(query="报销").top_k is None


# --- 16b) payload 形态：缺省 top_k 不下发；不带 subject -------------------


def test_search_documents_payload_shape() -> None:
    """下发给 MCP 的工具名是 search_documents；缺省 top_k 不下发；不带 subject。"""
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [_doc_hit()], "hit_count": 1})
    )
    result = run_rag_search_documents(_ctx(client), RagSearchDocumentsArgs(query="报销"))
    assert result.ok is True
    assert client.calls[0][0] == "search_documents"  # MCP 层工具名（无 rag_ 前缀）
    payload = _payload(client)
    assert payload["query"] == "报销"
    assert "top_k" not in payload   # 缺省不下发（服务端取默认）
    assert "subject" not in payload  # 文档线无账号维度，绝不注入 subject


def test_search_documents_forwards_explicit_top_k() -> None:
    """显式 top_k 原样下发。"""
    client = _FakeRagClient(ToolResult(ok=True, data={"hits": [], "hit_count": 0}))
    run_rag_search_documents(
        _ctx(client), RagSearchDocumentsArgs(query="报销", top_k=3)
    )
    assert _payload(client)["top_k"] == 3


# --- 16c) 空 hits 是合法答案（不是错误） --------------------------------


def test_search_documents_empty_hits_is_success() -> None:
    """空 hits → ok=True、hit_count=0（知识库没有相关内容是合法答案）。"""
    client = _FakeRagClient(ToolResult(ok=True, data={"hits": [], "hit_count": 0}))
    result = run_rag_search_documents(_ctx(client), RagSearchDocumentsArgs(query="不存在"))
    assert result.ok is True
    assert result.summary["hits"] == []
    assert result.summary["hit_count"] == 0


# --- 16d) 两条通道：成功读 content[0].text、失败读 structuredContent ------


def _client_parse(  # type: ignore[no-untyped-def]
    *,
    result_obj: dict[str, Any],
) -> Any:
    """用**真实** MCPClient 解析一个 JSON-RPC result 信封（不触网）。"""
    from administrative_agent.mcp.client import MCPClient

    client = MCPClient(Settings(erp_mcp_url="http://erp:8000"))
    envelope = {"jsonrpc": "2.0", "id": 1, "result": result_obj}
    return client._unpack_tool_result("search_documents", 0.0, envelope)


def test_search_documents_success_channel_reads_content_text() -> None:
    """成功通道：只在 ``content[0].text`` 放 JSON（无 structuredContent）时被解析。

    与 ``recall`` 完全一致的通道：RAG 成功结果经 ``content[0].text`` 的 JSON
    文本回来，客户端 ``_content_to_data`` 解析成 dict（顶层 hits/hit_count）。
    变异：把该 JSON 解析去掉/改成返回裸 content → ``data`` 不是契约 dict → 红。
    """
    payload = {"hits": [_doc_hit()], "hit_count": 1}
    parsed = _client_parse(
        result_obj={"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
    )
    assert parsed.ok is True
    assert parsed.data["hit_count"] == 1
    assert parsed.data["hits"][0]["page_start"] == 3   # PDF 出处字段在


def test_search_documents_failure_channel_reads_structured_content() -> None:
    """失败通道：``isError`` + ``structuredContent`` 五键信封 → 错误带 reason。

    变异：把 ``search_documents`` 的失败走成功分支（只看 content、不看 isError）
    → 下面 ok/error.code/details.reason 全红。
    """
    sc = {
        "code": "VALIDATION_ERROR",
        "message": "query 为空",
        "retryable": False,
        "details": {"reason": "query_empty"},
        "requires_operator_action": False,
    }
    parsed = _client_parse(
        result_obj={
            "isError": True,
            "structuredContent": sc,
            "content": [{"type": "text", "text": "query 为空"}],
        }
    )
    assert parsed.ok is False
    assert parsed.error is not None
    assert parsed.error.code == "VALIDATION_ERROR"
    assert parsed.error.details.get("reason") == "query_empty"


def test_search_documents_error_envelope_surfaces_reason() -> None:
    """包装层把 RAG 侧 ``details.reason`` 拼进错误文本（模型可自纠）。"""
    err = MCPError(
        code="VALIDATION_ERROR",
        category=ErrorCategory.VALIDATION,
        message="query 超长",
        details={"reason": "query_too_long"},
    )
    client = _FakeRagClient(ToolResult(ok=False, error=err))
    result = run_rag_search_documents(
        _ctx(client), RagSearchDocumentsArgs(query="x")
    )
    assert result.ok is False
    assert "query_too_long" in result.summary["error"]


# --- 16e) 未接线 / 无身份 fail closed -------------------------------------


def test_search_documents_unwired_fails_closed() -> None:
    """rag_client=None → 报错、不触网。"""
    ctx = ToolContext(
        context=AgentContext(user_id=_ACCOUNT, user_role="manager", scope=frozenset()),
        rag_client=None,
    )
    result = run_rag_search_documents(ctx, RagSearchDocumentsArgs(query="x"))
    assert result.ok is False
    assert "error" in result.summary


# --- 16f) 结果尺寸收口：大 hits 过预算仍是合法 JSON ----------------------


def test_search_documents_large_result_is_valid_json_and_truncated() -> None:
    """>4096 的文档检索结果：信封 ≤ 预算、合法 JSON、带 truncated、可过消费点。

    判据同 recall 的大结果用例：构造 20 条 × 1000 字 content，跑**真实消费点**
    ``summarize_tool_result``；若 ``_RAG_LIST_KEYS`` 未登记 ``search_documents``
    的 ``hits``（收口不生效），信封 ~20KB → 裸截断成非法 JSON → 红。
    """
    from administrative_agent.agent.nodes.tools_node import (
        TOOL_RESULT_HISTORY_CHARS,
        summarize_tool_result,
    )
    from administrative_agent.agent.tools.rag_memory import RAG_RESULT_CHARS

    big = {
        "hits": [
            {**_doc_hit(chunk_id=i, document_id=i), "content": "x" * 1000}
            for i in range(20)
        ],
        "hit_count": 20,
    }
    client = _FakeRagClient(ToolResult(ok=True, data=big))
    result = run_rag_search_documents(
        _ctx(client), RagSearchDocumentsArgs(query="报销", top_k=20)
    )
    assert result.ok is True
    envelope = json.dumps({"ok": True, **result.summary}, ensure_ascii=False)
    assert len(envelope) <= RAG_RESULT_CHARS
    assert len(envelope) < TOOL_RESULT_HISTORY_CHARS
    assert result.summary["truncated"] is True
    assert result.summary["hit_count"] == 20
    summarized = summarize_tool_result(envelope, tool_name="rag_search_documents")
    parsed = json.loads(summarized)  # 收口不生效此处抛 JSONDecodeError
    assert parsed["truncated"] is True
    assert summarized == envelope


# --- 16g) 审计：成功检索落 EVENT_RAG_SEARCH；失败不落；审计失败不拖挂 ----


def test_search_documents_records_audit() -> None:
    """成功检索 → 落一条审计：谁/条件(top_k)/命中几何(chunk_ids/document_ids)。"""
    audit = _FakeAudit()
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [_doc_hit(1, 10), _doc_hit(2, 10)], "hit_count": 2})
    )
    result = run_rag_search_documents(
        _ctx(client, audit=audit, thread_id="t-d1"),
        RagSearchDocumentsArgs(query="报销流程", top_k=5),
    )
    assert result.ok is True
    assert len(audit.search_calls) == 1
    call = audit.search_calls[0]
    assert call["actor_account"] == _ACCOUNT
    assert call["trace_id"] == "thread:t-d1"
    assert call["top_k"] == 5
    assert call["hit_count"] == 2
    assert call["chunk_ids"] == [1, 2]
    assert call["document_ids"] == [10, 10]
    assert call["query_chars"] == len("报销流程")


def test_search_documents_zero_hit_still_records_audit() -> None:
    """零命中也要记——「查了没命中」本身可观测（区分"没调"）。"""
    audit = _FakeAudit()
    client = _FakeRagClient(ToolResult(ok=True, data={"hits": [], "hit_count": 0}))
    run_rag_search_documents(_ctx(client, audit=audit), RagSearchDocumentsArgs(query="无"))
    assert len(audit.search_calls) == 1
    assert audit.search_calls[0]["hit_count"] == 0
    assert audit.search_calls[0]["chunk_ids"] == []


def test_search_documents_audit_never_carries_query_text() -> None:
    """**不落 query 正文**：审计实参里任何位置都不得出现原查询串。"""
    audit = _FakeAudit()
    client = _FakeRagClient(ToolResult(ok=True, data={"hits": [], "hit_count": 0}))
    private = "含私密信息的查询文本SENTINEL"
    run_rag_search_documents(_ctx(client, audit=audit), RagSearchDocumentsArgs(query=private))
    dumped = json.dumps(audit.search_calls[0], ensure_ascii=False)
    assert private not in dumped
    assert audit.search_calls[0]["query_chars"] == len(private)


def test_search_documents_failed_call_records_no_audit() -> None:
    """RAG 侧失败（没有可观测的事发生）→ 不落审计。"""
    audit = _FakeAudit()
    err = MCPError(code="BUSINESS_ERROR", category=ErrorCategory.VALIDATION, message="x")
    client = _FakeRagClient(ToolResult(ok=False, error=err))
    result = run_rag_search_documents(_ctx(client, audit=audit), RagSearchDocumentsArgs(query="x"))
    assert result.ok is False
    assert audit.search_calls == []


def test_search_documents_audit_failure_does_not_fail_search() -> None:
    """**失败语义（与写路径刻意不同）**：审计写入失败，检索仍返回 hits、ok=True。"""
    audit = _FakeAudit(raises=True)
    client = _FakeRagClient(
        ToolResult(ok=True, data={"hits": [_doc_hit()], "hit_count": 1})
    )
    result = run_rag_search_documents(_ctx(client, audit=audit), RagSearchDocumentsArgs(query="x"))
    assert result.ok is True
    assert result.summary["hits"][0]["chunk_id"] == 1
    assert len(audit.search_calls) == 1  # 确实尝试记过
