"""D20 复核有效性门禁测试（7.4 L888-913、7.3 L886、RV-30 通过标准）。

通过标准（16.6 RV-30 L1637）：**旧 review 无法执行变化后的计划**。
五条件任一不满足 → 拒绝；全部满足 → 有效。纯函数，零写。
"""

from __future__ import annotations

from administrative_agent.business.review_gate import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    check_review_valid,
    is_review_valid,
)
from administrative_agent.domains.reviews import ReviewRecord

OLD_HASH = "a" * 64
NEW_HASH = "b" * 64


def review(**overrides) -> ReviewRecord:
    base = dict(
        review_id="rev-valid-0000000000000001",
        task_id="task-review-0001",
        requested_by="manager01",
        decision=DECISION_APPROVED,
        reviewed_payload_hash=OLD_HASH,
        changed_after_review=False,
        invalidated_at=None,
    )
    base.update(overrides)
    return ReviewRecord(**base)


def test_valid_review_passes_gate():
    assert check_review_valid(review()) is None
    assert is_review_valid(review())
    # 当前 hash 与记录一致 → 有效（7.3：内容未变化）
    assert check_review_valid(review(), current_payload_hash=OLD_HASH) is None


def test_pending_review_rejected():
    reason = check_review_valid(review(decision=None))
    assert reason is not None
    assert "未通过" in reason and "approved" in reason


def test_rejected_review_rejected():
    reason = check_review_valid(review(decision=DECISION_REJECTED))
    assert reason is not None
    assert "未通过" in reason


def test_invalidated_review_rejected():
    """7.4 步骤 2：失效复核不得执行（旧复核无法执行变化后的计划）。"""
    reason = check_review_valid(review(invalidated_at="2026-08-23T09:00:00+00:00"))
    assert reason is not None
    assert "已失效" in reason


def test_changed_after_review_rejected():
    """7.4 步骤 1：计划业务变化 → 原复核失效。"""
    reason = check_review_valid(review(changed_after_review=True))
    assert reason is not None
    assert "已变化" in reason


def test_hash_mismatch_rejected_even_if_approved():
    """核心场景（14.1 L1380）：已批准复核但当前计划 hash 不同 →
    拒绝（旧 review 无法执行变化后的计划，RV-30 通过标准）。"""
    r = review(decision=DECISION_APPROVED, invalidated_at=None,
               changed_after_review=False)
    reason = check_review_valid(r, current_payload_hash=NEW_HASH)
    assert reason is not None
    assert "不一致" in reason
    assert not is_review_valid(r, current_payload_hash=NEW_HASH)


def test_hash_check_only_when_current_hash_provided():
    """current_payload_hash 缺省时只做状态检查（D21 execute 必须提供
    当前 hash——本接口不提供绕过路径，缺省用法限状态门禁）。"""
    assert check_review_valid(review(decision=DECISION_APPROVED)) is None
    assert check_review_valid(review(decision=DECISION_REJECTED)) is not None
