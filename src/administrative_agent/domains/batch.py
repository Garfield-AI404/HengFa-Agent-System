"""通用批量执行壳。

语义照抄 domains/attendance.py 的批量执行骨架（4.3 顺序 6-10 / 13.2），把
「逐员工」抽象为「逐明细条目」，供订单/收支 4 条路径（order_create /
order_update / finance_create / finance_update）共用：

- 顺序 6：写入 task 当前步骤 + 条目台账（TaskService.update_progress）；
- 逐条：op_id = f"{task_id}:{tool_key}:{item_id}"（对齐 attendance.py:110-112
  的稳定子操作 ID）；
- 已有成功 attempt → 标 skipped、**零写调用**（中断恢复只补未完成项）；
- 达重试上限 → 标 failed(ERROR_RETRY_LIMIT)，不再循环（13.2）；
- 否则调 runner(item, op_id)——runner 内部完成 attempt / 写调用 / result
  （与 AttendanceExecutor._execute_employee 同分工）。
- 聚合终态（确定性策略，对齐 attendance `_final_verify` 三档）：
    全 success/skipped/unchanged → mark_succeeded(verification_passed=True)；
    有 failed 且无条目达重试上限 → update_progress(current_step=
      "awaiting_recovery")，任务**保持 running**（恢复入口 = 再次调用同一工具）；
    有 failed 且存在条目达重试上限 → mark_failed(ERROR_RETRY_LIMIT)。

**不引入跨条目最终复核查询**（考勤的 `_final_verify` 不搬）：订单/收支今天
以逐条响应为最终校验；ERP 查询分页上限 200，按日期窗口复核会被他人写入污染
（假阴性），收益为负。

**考勤本次不迁移到本模块**——它是零回归基线，先不动；接受一段时间的逻辑重复。

依赖方向（同 attendance.py）：只依赖 audit、tasks，不依赖 web。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import structlog

from administrative_agent.audit.service import AuditService
from administrative_agent.domains.orders import ACTION_UNCHANGED
from administrative_agent.tasks.service import TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.batch")

#: 任务终态错误码（对齐 attendance：13.2 达上限不循环 / 部分失败可恢复）
ERROR_RETRY_LIMIT = "RETRY_LIMIT_REACHED"
ERROR_PARTIAL = "BATCH_PARTIAL_FAILURE"

#: 每条明细 attempt 上限（13.2：达上限 → failed 不循环；默认 3）
DEFAULT_RETRY_LIMIT = 3


def stable_item_operation_id(task_id: str, tool_key: str, item_id: str) -> str:
    """4.4：每条明细的稳定子操作 ID（防重与追查；对齐 attendance 口径）。

    同 task + 同 tool + 同 item_id 跨多次执行产出同一 op_id——恢复执行时
    据此命中已成功的 attempt，标 skipped 零写调用。
    """
    return f"{task_id}:{tool_key}:{item_id}"


def build_item_ids(items: Sequence["BatchItem"]) -> list[str]:
    """为条目生成唯一且**同批次稳定**的 item_id。

    同 item_id（同业务标识）重复出现时，**按出现次序**为每次出现加
    ``#n`` 后缀（n 从 1 起，仅重复项加后缀）——保证 op_id 唯一；非重复项
    保持原样。同批次两次调用产出同一序列（稳定），恢复执行不重复写。
    """
    counts = Counter(item.item_id for item in items)
    seen: dict[str, int] = {}
    result: list[str] = []
    for item in items:
        if counts[item.item_id] > 1:
            seen[item.item_id] = seen.get(item.item_id, 0) + 1
            result.append(f"{item.item_id}#{seen[item.item_id]}")
        else:
            result.append(item.item_id)
    return result


# ---------------------------------------------------------------------------
# 输出汇总（对齐 AttendanceExecutionSummary 的形状）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchItemResult:
    """单条明细的最终结果。

    - final_action：created/updated/unchanged/skipped/failed；
    - status：success（写入成功或无需写入/已成功跳过）/ failed。
    """

    index: int
    item_id: str
    label: str
    final_action: str
    status: str
    error_code: str | None = None
    error_summary: str | None = None
    attempt_id: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "item_id": self.item_id,
            "label": self.label,
            "final_action": self.final_action,
            "status": self.status,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "attempt_id": self.attempt_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class BatchSummary:
    """批量执行汇总（对齐 AttendanceExecutionSummary：counts/item_results/
    verification_passed/resume_position/error_code）。"""

    task_id: str
    counts: dict[str, int]
    item_results: tuple[BatchItemResult, ...]
    verification_passed: bool
    resume_position: str | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "counts": dict(self.counts),
            "item_results": [r.to_dict() for r in self.item_results],
            "verification_passed": self.verification_passed,
            "resume_position": self.resume_position,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


@dataclass(frozen=True)
class BatchItem:
    """一条待执行明细（4 条路径 prepare 阶段构造，execution 序列化后落卡）。

    - index：1-based 序号（台账与展示行号）；
    - item_id：稳定业务标识（op_id 组成；同批次重复由 build_item_ids 去重）；
    - action：created/updated/unchanged（unchanged 执行期零写调用）；
    - label：展示业务标识（如「客户 铭泰 / M1」）；
    - payload：该条的执行载荷（runner 消费；不参与本壳逻辑）。
    """

    index: int
    item_id: str
    action: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "item_id": self.item_id,
            "action": self.action,
            "label": self.label,
            "payload": dict(self.payload),
        }


#: runner 契约：给定条目与稳定 op_id，完成 attempt/写调用/result 并返回结果。
BatchRunner = Callable[[BatchItem, str], BatchItemResult]


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class BatchExecutor:
    """通用批量执行编排（4.3 顺序 6-10 的确定性实现）。

    线程安全：本类无共享可变状态；同一 task 不允许两个执行器并发由 D6
    WriteTaskLock 的 task 级互斥保证（13.3），run 入口再校验任务状态
    （必须 running）兜底。
    """

    def __init__(
        self,
        *,
        tasks: TaskService,
        audit: AuditService,
        tool_key: str,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
    ) -> None:
        self._tasks = tasks
        self._audit = audit
        #: op_id 命名空间（如 "order_create"/"finance_update"）
        self._tool_key = tool_key
        self._retry_limit = retry_limit

    # ---- 入口（4.3 顺序 6-10） ----

    def run(
        self,
        *,
        task_id: str,
        items: Sequence[BatchItem],
        runner: BatchRunner,
    ) -> BatchSummary:
        """执行整批：写 task 步骤/台账 → 逐条（skip/上限/runner）→ 聚合终态。"""
        self._require_running(task_id)
        items = list(items)

        # [2026-09-12 fail-closed] item_id 在批内必须唯一：本壳的 op_id
        # （stable_item_operation_id）以 item_id 为区分，dup 会让两条明细共用
        # 同一 op_id → skip/重试上限判定互相串台。调用方**必须**先经
        # build_item_ids 去重（同业务标识重复项加 #n 后缀）；此处兜底断言，
        # 绝不静默继续。
        item_ids = [it.item_id for it in items]
        if len(set(item_ids)) != len(item_ids):
            duplicates = sorted({i for i in item_ids if item_ids.count(i) > 1})
            raise ValueError(
                f"BatchExecutor.run 收到重复 item_id {duplicates}："
                "调用方必须先经 build_item_ids 去重（fail closed，不得静默继续）"
            )

        # 顺序 6：写入 task 当前步骤与条目台账（8.2 execution_plan）
        self._tasks.update_progress(
            task_id,
            current_step="executing_batch",
            execution_plan={
                "batch_size": len(items),
                "items": [
                    {
                        "index": it.index,
                        "item_id": it.item_id,
                        "label": it.label,
                        "action": it.action,
                    }
                    for it in items
                ],
            },
        )
        logger.info("batch_plan_written", task_id=task_id, batch_size=len(items))

        results = [self._execute_item(task_id, item, runner) for item in items]
        return self._finalize(task_id, results)

    # ---- 逐条执行 ----

    def _execute_item(
        self, task_id: str, item: BatchItem, runner: BatchRunner
    ) -> BatchItemResult:
        """单条：unchanged 零写 → 已有成功 skip → 达上限 failed → runner。"""
        # unchanged：本条本次无需写入（规则 5 展示与折叠），零写调用
        if item.action == ACTION_UNCHANGED:
            return BatchItemResult(
                index=item.index,
                item_id=item.item_id,
                label=item.label,
                final_action=ACTION_UNCHANGED,
                status="success",
                note="本条无需写入（unchanged），零写调用",
            )

        op_id = stable_item_operation_id(task_id, self._tool_key, item.item_id)
        prior = self._audit.attempts(op_id)

        # 已有成功 result → 跳过（已成功不重复写；恢复执行只补未完成项）
        if any(a.result_status == "success" for a in prior):
            logger.info("batch_skip_already_success", operation_id=op_id)
            return BatchItemResult(
                index=item.index,
                item_id=item.item_id,
                label=item.label,
                final_action="skipped",
                status="success",
                note="已有成功 result，跳过（已成功不重复写）",
            )

        # 13.2：达上限 → failed 不循环（runner 不再调用）
        if len(prior) >= self._retry_limit:
            return BatchItemResult(
                index=item.index,
                item_id=item.item_id,
                label=item.label,
                final_action="failed",
                status="failed",
                error_code=ERROR_RETRY_LIMIT,
                error_summary=f"该条目已尝试 {len(prior)} 次仍未成功，不再循环",
            )

        return runner(item, op_id)

    # ---- 聚合终态（对齐 attendance `_final_verify` 三档） ----

    def _finalize(
        self, task_id: str, results: list[BatchItemResult]
    ) -> BatchSummary:
        """全成功 → succeeded；部分失败未达上限 → awaiting_recovery（保持
        running）；有 failed 且达上限 → failed(ERROR_RETRY_LIMIT)。"""
        failed = [r for r in results if r.status == "failed"]
        counts = {
            "created": sum(1 for r in results if r.final_action == "created"),
            "updated": sum(1 for r in results if r.final_action == "updated"),
            "unchanged": sum(1 for r in results if r.final_action == "unchanged"),
            "skipped": sum(1 for r in results if r.final_action == "skipped"),
            "failed": len(failed),
        }

        # 全部 success/skipped/unchanged（无 failed）→ 唯一成功路径
        if not failed:
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            logger.info("batch_task_succeeded", task_id=task_id, counts=counts)
            return self._summary(
                task_id, results, counts=counts, verification_passed=True
            )

        # 13.2：任一条目达上限 → 任务 failed，不循环
        at_limit = [
            r
            for r in failed
            if len(
                self._audit.attempts(
                    stable_item_operation_id(task_id, self._tool_key, r.item_id)
                )
            )
            >= self._retry_limit
        ]
        if at_limit:
            # 达上限的失败条目统一归类为 ERROR_RETRY_LIMIT（对齐 attendance
            # `_mark_at_limit`）——不可继续，故不提供恢复位置（见 _summary）。
            at_limit_ids = {r.item_id for r in at_limit}
            results = [
                self._mark_at_limit(r)
                if r.status == "failed" and r.item_id in at_limit_ids
                else r
                for r in results
            ]
            names = "、".join(r.label for r in at_limit)
            summary_text = (
                f"部分明细已达重试上限（{names}），任务终止不再循环（13.2）；"
                "已成功条目保留，需人工处理"
            )
            self._fail_task(task_id, ERROR_RETRY_LIMIT, summary_text)
            return self._summary(
                task_id, results, counts=counts, verification_passed=False,
                error_code=ERROR_RETRY_LIMIT, error_summary=summary_text,
            )

        # 未达上限：任务保持 running，等待恢复执行（只补未完成项）
        self._tasks.update_progress(task_id, current_step="awaiting_recovery")
        names = "、".join(r.label for r in failed)
        summary_text = (
            f"部分成功：{len(failed)} 条未完成（{names}）；"
            "已成功条目保留，可继续（恢复执行只补未完成项）"
        )
        logger.warning(
            "batch_task_awaits_recovery", task_id=task_id, failed=names
        )
        return self._summary(
            task_id, results, counts=counts, verification_passed=False,
            error_code=ERROR_PARTIAL, error_summary=summary_text,
        )

    # ---- 汇总与内部 ----

    @staticmethod
    def _mark_at_limit(result: BatchItemResult) -> BatchItemResult:
        """达重试上限的条目结果归类（13.2：failed 不循环）。"""
        return BatchItemResult(
            index=result.index,
            item_id=result.item_id,
            label=result.label,
            final_action="failed",
            status="failed",
            error_code=ERROR_RETRY_LIMIT,
            error_summary="达重试上限，不再循环（13.2）",
            attempt_id=result.attempt_id,
            note=result.note,
        )

    @staticmethod
    def _summary(
        task_id: str,
        results: list[BatchItemResult],
        *,
        counts: dict[str, int],
        verification_passed: bool,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> BatchSummary:
        resume_position = None
        for r in results:
            # 达上限的条目不可继续（13.2），不提供恢复位置
            if r.status == "failed" and r.error_code != ERROR_RETRY_LIMIT:
                resume_position = (
                    f"上次中断于明细 {r.label}（该条本次仍失败，"
                    "恢复执行将从该条继续）"
                )
                break
        return BatchSummary(
            task_id=task_id,
            counts=counts,
            item_results=tuple(results),
            verification_passed=verification_passed,
            resume_position=resume_position,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _require_running(self, task_id: str) -> None:
        """执行前校验：任务必须 running（8.3：等待/终态任务不执行；
        13.3：同 task 不并发由锁保证，状态校验兜底）。"""
        from administrative_agent.tasks.machine import TaskStatus
        from administrative_agent.tasks.service import TaskNotRunningError

        task = self._tasks.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            raise TaskNotRunningError(task_id, task.status)

    def _fail_task(self, task_id: str, error_code: str, error_summary: str) -> None:
        """任务标记 failed（8.1/13.2：需人工处理或重新发起；不假报成功）。"""
        try:
            self._tasks.mark_failed(
                task_id, error_code=error_code, error_summary=error_summary
            )
        except Exception:
            # 任务已终态时忽略；审计失败路径由调用方另行处理（同 attendance 口径）
            logger.exception("batch_fail_task_error", task_id=task_id)
