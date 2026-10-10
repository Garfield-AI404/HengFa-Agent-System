"""R6 复核有效性门禁（规格 2.6 business/review_gate.py；7.4 L888-913、
7.3 L886、RV-30 通过标准）。

通过标准（16.6 RV-30 L1637）：**旧 review 无法执行变化后的计划**。

门禁检查（D21 execute 与本单元 approve 共用同一校验）：
- review 记录存在；
- decision = approved（未决定/已拒绝不构成有效复核）；
- invalidated_at 为空（业务变化后旧复核已标记失效，7.4 步骤 2）；
- changed_after_review = false（7.4 步骤 1）；
- 当前计划 hash 与 reviewed_payload_hash 一致（7.3 同机制：复核内容/
  目标记录/关键字段变化 → 原复核失效须重新复核；hash 不一致 → 拒绝）。

任一不满足 → 返回原因（None = 有效）。纯函数、零写；D21 execute
调用 ``check_review_valid`` 通过后才能执行写入，本单元不提供绕过路径。
"""

from __future__ import annotations

from typing import Any, Protocol

#: 复核决定常量（与 models.ReviewDecision 一致）
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"


class ReviewLike(Protocol):
    """门禁所需的复核记录最小视图（ReviewRecord / 仓储行均满足）。"""

    review_id: str
    decision: str | None
    invalidated_at: Any
    changed_after_review: bool
    reviewed_payload_hash: str


def check_review_valid(
    review: ReviewLike,
    *,
    current_payload_hash: str | None = None,
) -> str | None:
    """复核有效性校验：返回 None 表示有效，否则返回拒绝原因。

    current_payload_hash：当前（最新）写入计划的 payload hash；提供时
    必须与复核记录一致（7.3/7.4：计划变化 → 旧复核失效，不得执行）。
    """
    if review.decision != DECISION_APPROVED:
        return (
            f"复核 {review.review_id} 未通过（decision={review.decision!r}）；"
            "只有 approved 的复核才能执行写入"
        )
    if review.invalidated_at is not None:
        return (
            f"复核 {review.review_id} 已失效（invalidated_at 已置位，7.4 步骤 2）；"
            "旧复核不得执行变化后的计划"
        )
    if review.changed_after_review:
        return (
            f"复核 {review.review_id} 后计划已变化（changed_after_review=true，"
            "7.4 步骤 1）；需重新生成差异并重新复核"
        )
    if (
        current_payload_hash is not None
        and review.reviewed_payload_hash != current_payload_hash
    ):
        return (
            f"复核 {review.review_id} 的 payload hash 与当前计划不一致"
            f"（{review.reviewed_payload_hash[:12]}… vs {current_payload_hash[:12]}…）；"
            "计划已变化，需重新复核（7.3/14.1 L1380）"
        )
    return None


def is_review_valid(
    review: ReviewLike,
    *,
    current_payload_hash: str | None = None,
) -> bool:
    """布尔形态（D21 execute 门禁入口；原因见 check_review_valid）。"""
    return check_review_valid(review, current_payload_hash=current_payload_hash) is None
