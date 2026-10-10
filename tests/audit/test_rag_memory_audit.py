"""[2026-09-22 F1] RAG 长期记忆增删的 agent 侧审计事件。

判据：record_rag_memory 落一条 EVENT_RAG_MEMORY，能回答「谁（actor_account）
/ 对哪条记忆（target_id / memory_ids）/ 做了什么操作（operation）」，且
**不落记忆正文**（structured_payload 无 content）。审计失败抛
AuditFailureError（10.4 不假报成功）。

[2026-09-23] 追加：纯读 recall 的审计事件（EVENT_RAG_RECALL）——落一条事件、
字段值断言、**零命中也要记**、且**不落 query 正文**（只落 query_chars）。
"""

from __future__ import annotations

import inspect

import pytest

from administrative_agent.audit.redaction import redact_mapping
from administrative_agent.audit.service import (
    EVENT_RAG_MEMORY,
    EVENT_RAG_RECALL,
    EVENT_RAG_SEARCH,
    AuditFailureError,
    AuditService,
)


def test_remember_audit_event_fields(audit_service: AuditService, audit_repo) -> None:
    audit_service.record_rag_memory(
        operation="remember",
        trace_id="thread:t-1",
        actor_account="hengfa-internal-01",
        actor_role="manager",
        memory_id=42,
        kind="preference",
        subject="hengfa-internal-01",
    )
    events = audit_service.events(event_type=EVENT_RAG_MEMORY)
    assert len(events) == 1
    e = events[0]
    assert e.actor_account == "hengfa-internal-01"   # 谁
    assert e.target_type == "rag_memory"
    assert e.target_id == "42"                        # 哪条记忆
    assert e.duty_code == "MEM"
    assert e.structured_payload["operation"] == "remember"
    assert e.structured_payload["kind"] == "preference"
    assert "content" not in e.structured_payload      # 不落正文


def test_batch_forget_target_id_none_but_ids_in_payload(
    audit_service: AuditService,
) -> None:
    audit_service.record_rag_memory(
        operation="forget",
        trace_id="thread:t-2",
        actor_account="acct-b",
        actor_role="manager",
        memory_ids=[3, 5],
        affected=2,
        subject="acct-b",
    )
    e = audit_service.events(event_type=EVENT_RAG_MEMORY)[0]
    assert e.target_id is None  # 批量：逐条见 payload
    assert e.structured_payload["memory_ids"] == [3, 5]
    assert e.structured_payload["affected"] == 2


def test_single_forget_uses_memory_id_target(audit_service: AuditService) -> None:
    audit_service.record_rag_memory(
        operation="forget",
        trace_id="thread:t-3",
        actor_account="acct-c",
        actor_role="manager",
        memory_id=9,
    )
    e = audit_service.events(event_type=EVENT_RAG_MEMORY)[0]
    assert e.target_id == "9"


def test_invalid_operation_rejected(audit_service: AuditService) -> None:
    with pytest.raises(ValueError):
        audit_service.record_rag_memory(
            operation="upsert",
            trace_id="t",
            actor_account="a",
            actor_role="manager",
        )


def test_audit_write_failure_raises(
    audit_service: AuditService, audit_repo
) -> None:
    audit_repo.fail_next = True
    with pytest.raises(AuditFailureError):
        audit_service.record_rag_memory(
            operation="remember",
            trace_id="t",
            actor_account="a",
            actor_role="manager",
            memory_id=1,
        )


# ---------------------------------------------------------------------------
# [2026-09-23] 纯读 recall 的审计事件（EVENT_RAG_RECALL）
# ---------------------------------------------------------------------------


def test_recall_audit_event_fields(audit_service: AuditService) -> None:
    """成功召回 → 落一条 EVENT_RAG_RECALL，字段 = 谁/条件/命中几何。"""
    audit_service.record_rag_recall(
        trace_id="thread:t-r1",
        actor_account="hengfa-internal-01",
        actor_role="manager",
        query_chars=12,
        top_k=5,
        kind="preference",
        since="2026-09-01",
        min_score=0.4,
        hit_count=2,
        memory_ids=[42, 43],
    )
    events = audit_service.events(event_type=EVENT_RAG_RECALL)
    assert len(events) == 1
    e = events[0]
    assert e.actor_account == "hengfa-internal-01"   # 谁
    assert e.trace_id == "thread:t-r1"
    assert e.target_type == "rag_memory"
    assert e.target_id is None                         # 召回不针对单条
    assert e.duty_code == "MEM"
    p = e.structured_payload
    assert p["top_k"] == 5
    assert p["kind"] == "preference"
    assert p["since"] == "2026-09-01"
    assert p["min_score"] == 0.4                       # 显式相似度下限入审计
    assert p["hit_count"] == 2
    assert p["memory_ids"] == [42, 43]
    assert p["truncated"] is False                     # 未截断：ids 即全部命中
    assert p["query_chars"] == 12


def test_recall_zero_hit_is_recorded(audit_service: AuditService) -> None:
    """零命中也要记——「查了没命中」本身就是可观测事实（区分"没调"）。"""
    audit_service.record_rag_recall(
        trace_id="thread:t-r2",
        actor_account="acct-z",
        actor_role="manager",
        query_chars=3,
        hit_count=0,
        memory_ids=[],
    )
    events = audit_service.events(event_type=EVENT_RAG_RECALL)
    assert len(events) == 1
    assert events[0].structured_payload["hit_count"] == 0
    assert events[0].structured_payload["memory_ids"] is None


def test_recall_audit_never_stores_query_text(audit_service: AuditService) -> None:
    """**不落 query 正文**：API 无 query 正文形参、payload 只留 query_chars。

    召回条件是用户会话内容（同写侧「不落记忆正文」口径）。旧断言只查
    ``"query" not in structured_payload``——**漏掉了同样落库的
    ``request_summary``**（audit_events 的 Text 列），于是「把 query 拼进
    request_summary」的变异能全绿通过。此处补两类钉：

    1. **结构钉**：``record_rag_recall`` 签名不得有承载 query 正文的形参——
       唯一含 ``query`` 的形参只能是 ``query_chars``（新增 ``query``/
       ``query_text`` 之类形参即红，从源头堵死落正文的通道）；
    2. **整条记录钉**：把落库的 ``request_summary`` 逐字钉住（它只由 top_k/
       kind/min_score/hits/query_chars/truncated 这些**非正文**字段拼成），
       任何把 query 正文塞进该字段的变异都会破坏逐字相等 → 红。
    """
    # 结构钉：签名里唯一含 "query" 的形参必须是 query_chars（只有长度）
    params = list(inspect.signature(AuditService.record_rag_recall).parameters)
    assert "query" not in params, "record_rag_recall 新增了 query 正文形参"
    assert [p for p in params if "query" in p] == ["query_chars"], (
        f"record_rag_recall 的 query 类形参不止 query_chars：{params}"
    )

    audit_service.record_rag_recall(
        trace_id="thread:t-r3",
        actor_account="acct-p",
        actor_role="manager",
        query_chars=8,
        hit_count=1,
        memory_ids=[7],
    )
    e = audit_service.events(event_type=EVENT_RAG_RECALL)[0]
    assert "query" not in e.structured_payload          # 只落 query_chars，不落正文
    assert e.structured_payload["query_chars"] == 8
    # 整条落库记录钉：request_summary 只含非正文字段（逐字相等，注入正文即红）
    assert e.request_summary == (
        "rag_recall (top_k=None, kind=None, min_score=None, "
        "hits=1, query_chars=8, truncated=False)"
    )


def test_recall_audit_truncated_ids_are_explained(
    audit_service: AuditService,
) -> None:
    """截断时 hit_count（裁剪前总数）> len(memory_ids)（前缀子集）须有说明。

    背景：``_bounded_result`` 故意保留顶层 ``hit_count`` 为服务端原值、却从
    命中列表尾部丢弃条目。若不落 ``truncated``，事后读者无法区分「只命中 5
    条」与「命中 20 条、只记了前 5 条」——本用例即钉住这处差异**有解释**。
    """
    audit_service.record_rag_recall(
        trace_id="thread:t-r4",
        actor_account="acct-t",
        actor_role="manager",
        query_chars=10,
        top_k=20,
        hit_count=20,
        memory_ids=[100, 101, 102, 103, 104],
        truncated=True,
    )
    e = audit_service.events(event_type=EVENT_RAG_RECALL)[0]
    p = e.structured_payload
    assert p["hit_count"] == 20                    # 服务端命中总数（裁剪前）
    assert p["memory_ids"] == [100, 101, 102, 103, 104]  # 记下的前缀子集
    assert len(p["memory_ids"]) < p["hit_count"]   # 确为子集
    assert p["truncated"] is True                  # 差异有说明（非静默）
    assert "truncated=True" in e.request_summary


def test_recall_min_score_survives_redaction(audit_service: AuditService) -> None:
    """``min_score`` 含 ``score`` 子串，须实测不被 ``redact_mapping`` 当敏感键。

    本仓有「字段名含 token/key 被脱敏子串打死」的前科；``min_score`` 若被脱敏，
    落库值会变成 ``***`` 而非浮点数，审计读不出模型设的相似度下限。
    """
    # 直接实测脱敏函数（不与落库路径耦合）
    assert redact_mapping({"min_score": 0.4}) == {"min_score": 0.4}
    # 走真实落库路径再钉一次
    audit_service.record_rag_recall(
        trace_id="thread:t-r5",
        actor_account="acct-s",
        actor_role="manager",
        query_chars=4,
        min_score=0.75,
        hit_count=0,
        memory_ids=[],
    )
    e = audit_service.events(event_type=EVENT_RAG_RECALL)[0]
    assert e.structured_payload["min_score"] == 0.75


# ---------------------------------------------------------------------------
# [2026-10] 文档检索的审计事件（EVENT_RAG_SEARCH）
# ---------------------------------------------------------------------------


def test_search_audit_event_fields(audit_service: AuditService) -> None:
    """成功检索 → 落一条 EVENT_RAG_SEARCH，字段 = 谁/条件/命中几何。"""
    audit_service.record_rag_search_documents(
        trace_id="thread:t-s1",
        actor_account="hengfa-internal-01",
        actor_role="manager",
        query_chars=8,
        top_k=5,
        hit_count=2,
        chunk_ids=[11, 12],
        document_ids=[100, 100],
    )
    events = audit_service.events(event_type=EVENT_RAG_SEARCH)
    assert len(events) == 1
    e = events[0]
    assert e.actor_account == "hengfa-internal-01"   # 谁
    assert e.trace_id == "thread:t-s1"
    assert e.target_type == "rag_document"
    assert e.target_id is None                         # 检索不针对单条
    assert e.duty_code == "MEM"
    p = e.structured_payload
    assert p["top_k"] == 5
    assert p["hit_count"] == 2
    assert p["chunk_ids"] == [11, 12]
    assert p["document_ids"] == [100, 100]
    assert p["truncated"] is False
    assert p["query_chars"] == 8


def test_search_zero_hit_is_recorded(audit_service: AuditService) -> None:
    """零命中也要记——「查了没命中」本身可观测（区分"没调"）。"""
    audit_service.record_rag_search_documents(
        trace_id="thread:t-s2",
        actor_account="acct-z",
        actor_role="manager",
        query_chars=3,
        hit_count=0,
        chunk_ids=[],
        document_ids=[],
    )
    events = audit_service.events(event_type=EVENT_RAG_SEARCH)
    assert len(events) == 1
    assert events[0].structured_payload["hit_count"] == 0
    assert events[0].structured_payload["chunk_ids"] is None


def test_search_audit_never_stores_query_text(audit_service: AuditService) -> None:
    """**不落 query 正文**：签名无 query 正文字段、payload/整条记录都不含原文。

    结构钉 + 整条记录钉（同 recall 的写法）：``record_rag_search_documents``
    唯一含 ``query`` 的形参只能是 ``query_chars``；落库的 ``request_summary``
    逐字钉住（只由 top_k/hits/query_chars/truncated 拼成）。
    """
    params = list(inspect.signature(AuditService.record_rag_search_documents).parameters)
    assert [p for p in params if "query" in p] == ["query_chars"], (
        f"record_rag_search_documents 的 query 类形参不止 query_chars：{params}"
    )

    audit_service.record_rag_search_documents(
        trace_id="thread:t-s3",
        actor_account="acct-p",
        actor_role="manager",
        query_chars=6,
        top_k=3,
        hit_count=1,
        chunk_ids=[7],
        document_ids=[9],
    )
    e = audit_service.events(event_type=EVENT_RAG_SEARCH)[0]
    assert "query" not in e.structured_payload
    assert e.structured_payload["query_chars"] == 6
    assert e.request_summary == (
        "rag_search_documents (top_k=3, hits=1, query_chars=6, truncated=False)"
    )


def test_search_audit_truncated_ids_are_explained(audit_service: AuditService) -> None:
    """截断时 hit_count（裁剪前总数）> len(ids)（前缀子集）须有说明。"""
    audit_service.record_rag_search_documents(
        trace_id="thread:t-s4",
        actor_account="acct-t",
        actor_role="manager",
        query_chars=5,
        top_k=20,
        hit_count=20,
        chunk_ids=[100, 101, 102],
        document_ids=[1, 1, 1],
        truncated=True,
    )
    e = audit_service.events(event_type=EVENT_RAG_SEARCH)[0]
    p = e.structured_payload
    assert p["hit_count"] == 20
    assert len(p["chunk_ids"]) < p["hit_count"]
    assert p["truncated"] is True
    assert "truncated=True" in e.request_summary


def test_search_audit_write_failure_raises(
    audit_service: AuditService, audit_repo
) -> None:
    """审计写入失败抛 AuditFailureError（调用方按读路径约定：不拖挂检索）。"""
    audit_repo.fail_next = True
    with pytest.raises(AuditFailureError):
        audit_service.record_rag_search_documents(
            trace_id="t",
            actor_account="a",
            actor_role="manager",
            query_chars=1,
            hit_count=0,
        )

