"""R1 考勤执行编排（4.3 顺序 6-12；规格 2.6 domains/attendance.py）。

消费 D11 AttendancePlan 逐员工执行 R1 考勤写入（attendance_plan_execute
的确定性服务层实现，9.5：普通服务函数，不新增 ERP 工具）：

- 4.3 顺序 6：写入 task 当前步骤和完整执行计划（TaskService.update_progress）；
- 顺序 7：每位员工写前追加 attempt（AuditService.record_attempt，10.4 顺序 7）；
- 顺序 8：写前再次 query 该员工当天记录：存在 → update，不存在 → create
  （分流防重；4.4：防重靠写前查询 + 数据库唯一约束 + 写后验证，Agent 侧
  承担写前查询与写后验证）；
- 顺序 9：每次调用后立即追加 result（record_result，10.4 顺序 11；L1227：
  每次实际调用独立 attempt，网络重试不覆盖成一条）；
- 顺序 10：中断恢复跳过已有成功 result 的员工，只处理未完成项（13.2：
  attendance 单条 CRUD 失败由任务恢复机制处理，不重放整个批次；
  达上限 → failed 不循环）；
- 顺序 11-12：全部执行后再次 query 当天考勤，验证每位有效员工恰好一条
  → TaskService.mark_succeeded(verification_passed=True)（8.1 唯一成功路径）。

4.4 稳定 operation_id：{task_id}:attendance:{attendance_date}:{employee_id}。

9.5 字段映射：ATTENDANCE_FIELD_MAP 把标准字段映射为 ERP attendance 实际
字段（mcp-api.md §5，真实运行态见 RV-07 §8.1）；实体或字段不可写时停止并
报告 AttendanceContractError（接口契约错误），不发出任何写调用。

依赖方向（2.6 规则 3）：只依赖 mcp、business、audit、tasks，不依赖 web。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

import structlog

from administrative_agent.audit.service import AuditService
from administrative_agent.business.attendance_rules import (
    ACTION_UNCHANGED,
    AttendancePlan,
    PlannedRecord,
)
from administrative_agent.business.matching import EmployeeCandidate
from administrative_agent.mcp.client import MCPClient, ToolResult
from administrative_agent.mcp.errors import MCPError
from administrative_agent.mcp.metadata import EntityCache, EntityMetadata
from administrative_agent.mcp.scopes import verify_generic_write
from administrative_agent.tasks.service import TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.attendance")

#: 9.5：标准字段 → ERP attendance 实际字段（mcp-api.md §5/RV-07 §8.1 一致）
ATTENDANCE_FIELD_MAP: dict[str, str] = {
    "employee_id": "employee_id",
    "att_date": "att_date",
    "leave_minutes": "leave_minutes",
    "overtime_minutes": "overtime_minutes",
}

#: create 必须可写字段（9.5 契约校验；update 只写 leave/overtime_minutes）
_CREATE_REQUIRED_WRITABLE = (
    "employee_id",
    "att_date",
    "leave_minutes",
    "overtime_minutes",
)
_UPDATE_REQUIRED_WRITABLE = ("leave_minutes", "overtime_minutes")

#: 任务终态错误码
ERROR_CONTRACT = "ATTENDANCE_CONTRACT_ERROR"
ERROR_PARTIAL = "ATTENDANCE_PARTIAL_FAILURE"
ERROR_VERIFY_FAILED = "ATTENDANCE_VERIFICATION_FAILED"
ERROR_RETRY_LIMIT = "RETRY_LIMIT_REACHED"
ERROR_DATA_ANOMALY = "ATTENDANCE_DATA_ANOMALY"
ERROR_MCP = "ATTENDANCE_MCP_FAILURE"

#: 每员工 attempt 上限（13.2：达上限 → failed 不循环；默认 3）
DEFAULT_RETRY_LIMIT = 3

#: 最终验证查询上限（13.3 query limit 最大 200；单日员工数远小于此）
_VERIFY_QUERY_LIMIT = 200


class AttendanceContractError(RuntimeError):
    """9.5：标准字段映射失败——停止并报告接口契约错误。

    携带缺失/不可写字段清单供审计与人工处理（Agent 无法猜测 ERP 契约）。
    """

    def __init__(self, entity: str, operation: str, missing: list[str]) -> None:
        self.entity = entity
        self.operation = operation
        self.missing = missing
        super().__init__(
            f"attendance contract error: entity {entity!r} {operation} "
            f"不可写字段 {missing}（接口契约错误，停止写入）"
        )


class PolicyInfo(Protocol):
    """规则包的最小结构化视图（policy 三列进审计，11.1）。
    调用方传入 agent/prompts/policies.POLICY_REGISTRY 对应条目。"""

    policy_key: str
    version: str

    @property
    def hash(self) -> str: ...


def stable_operation_id(task_id: str, attendance_date: str, employee_id: int) -> str:
    """4.4：每位员工的稳定子操作 ID（防重与追查）。"""
    return f"{task_id}:attendance:{attendance_date}:{employee_id}"


# ---------------------------------------------------------------------------
# 输出汇总（4.2 L660-667 六要素）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmployeeExecutionResult:
    """每位员工的最终结果（4.2 输出要素 5）。

    - final_action：created/updated/unchanged/skipped/failed；
    - status：success（写入成功或无需写入）/ failed / skipped；
    - record_id：写入响应或最终验证查询回填的 ERP 记录 ID（未确认时 None）。
    """

    employee_id: int
    employee_ref: str
    planned_action: str
    final_action: str
    status: str
    record_id: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    attempt_id: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "employee_ref": self.employee_ref,
            "planned_action": self.planned_action,
            "final_action": self.final_action,
            "status": self.status,
            "record_id": self.record_id,
            "error_code": self.error_code,
            "note": self.note,
        }


@dataclass(frozen=True)
class AttendanceExecutionSummary:
    """attendance_plan_execute 输出汇总（4.2 六要素全）。

    - attendance_date / effective_employee_count / counts / employee_results /
      verification_passed / resume_position。
    """

    task_id: str
    attendance_date: str
    effective_employee_count: int
    counts: dict[str, int]
    employee_results: tuple[EmployeeExecutionResult, ...]
    verification_passed: bool
    resume_position: str | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """4.2 输出契约 JSON 形态（日期/有效员工总数/四类数量/每位员工
        最终结果/是否完成最终一致性验证/部分成功恢复位置）。"""
        return {
            "attendance_date": self.attendance_date,
            "effective_employee_count": self.effective_employee_count,
            "counts": dict(self.counts),
            "employee_results": [r.to_dict() for r in self.employee_results],
            "verification_passed": self.verification_passed,
            "resume_position": self.resume_position,
        }


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class AttendanceExecutor:
    """R1 考勤批量执行编排（4.3 顺序 6-12 的确定性实现）。

    线程安全：本类无共享可变状态；同一 task 不允许两个执行器并发由 D6
    WriteTaskLock 的 task 级互斥保证（13.3），execute 入口再校验任务状态
    （必须 running）兜底。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        metadata: EntityCache,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: PolicyInfo | None = None,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
    ) -> None:
        self._client = client
        self._metadata = metadata
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy
        self._retry_limit = retry_limit

    # ---- 入口（4.3 顺序 6-12） ----

    def execute(
        self,
        *,
        task_id: str,
        attendance_date: str,
        plan: AttendancePlan,
        employees: Iterable[EmployeeCandidate],
    ) -> AttendanceExecutionSummary:
        """执行完整计划：写 task 步骤 → 逐员工 attempt/写前查询分流/result →
        中断恢复跳过已成功 → 最终验证 → succeeded/failed。

        员工引用歧义/不存在属于 4.3 顺序 2（调用方先做唯一匹配并询问），
        本方法要求 employees 已唯一匹配且覆盖 plan 全部引用；不一致抛
        ValueError（不执行写入）。
        """
        self._require_running(task_id)
        employee_map = self._align_employees(plan, employees)

        # 9.5：契约校验失败 → 任务 failed 并抛出，不发出任何写调用
        try:
            entity = self._metadata.ensure_write_allowed("attendance")
            self._assert_contract(entity)
        except MCPError as exc:
            self._fail_task(task_id, exc.code, f"ERP 元数据不可用：{exc}")
            raise
        except AttendanceContractError as exc:
            self._fail_task(task_id, ERROR_CONTRACT, str(exc))
            raise

        # 4.3 顺序 6：写入 task 当前步骤和完整执行计划（8.2 execution_plan）
        self._tasks.update_progress(
            task_id,
            current_step="executing_attendance_plan",
            execution_plan={
                "attendance_date": attendance_date,
                "planned_counts": plan.counts(),
                "employees": [
                    {
                        "employee_id": employee_map[p.employee_ref].employee_id,
                        "name": p.employee_ref,
                    }
                    for p in plan.all_records
                ],
            },
        )
        logger.info(
            "attendance_plan_written",
            task_id=task_id,
            planned_counts=plan.counts(),
        )

        # 顺序 7-10：逐员工执行（unchanged 无需写调用，规则 5）
        results: list[EmployeeExecutionResult] = []
        for planned in plan.all_records:
            employee = employee_map[planned.employee_ref]
            if planned.action == ACTION_UNCHANGED:
                results.append(
                    EmployeeExecutionResult(
                        employee_id=employee.employee_id,
                        employee_ref=planned.employee_ref,
                        planned_action=planned.action,
                        final_action="unchanged",
                        status="success",
                        note="已有记录且本次未提及，保持原样（规则 5，无写调用）",
                    )
                )
                continue
            results.append(
                self._execute_employee(task_id, attendance_date, planned, employee)
            )

        # 顺序 11-12：全部执行后再次 query → 验证 → succeeded/failed
        return self._final_verify(task_id, attendance_date, plan, results)

    # ---- 顺序 7-10：单员工执行 ----

    def _execute_employee(
        self,
        task_id: str,
        attendance_date: str,
        planned: PlannedRecord,
        employee: EmployeeCandidate,
    ) -> EmployeeExecutionResult:
        """顺序 7 attempt → 顺序 8 写前 query 分流 → 顺序 9 result。

        单条失败（含网络结果未知）不重放批次：记录 failed result 后继续
        下一位员工；恢复入口（再次 execute）只处理未完成项（顺序 10）。
        """
        op_id = stable_operation_id(task_id, attendance_date, employee.employee_id)

        # 顺序 10：已有成功 result → 跳过（已成功员工不重复写）
        prior = self._audit.attempts(op_id)
        if any(a.result_status == "success" for a in prior):
            logger.info("attendance_skip_already_success", operation_id=op_id)
            return EmployeeExecutionResult(
                employee_id=employee.employee_id,
                employee_ref=planned.employee_ref,
                planned_action=planned.action,
                final_action="skipped",
                status="success",
                note="已有成功 result，跳过（顺序 10：已成功不重复写）",
            )
        # 13.2：达上限 → failed 不循环
        if len(prior) >= self._retry_limit:
            return EmployeeExecutionResult(
                employee_id=employee.employee_id,
                employee_ref=planned.employee_ref,
                planned_action=planned.action,
                final_action="failed",
                status="failed",
                error_code=ERROR_RETRY_LIMIT,
                error_summary=f"该员工已尝试 {len(prior)} 次仍未成功，不再循环",
            )

        # 顺序 7：写前 attempt（target_id 写前查询前未知，调用后经 result 关联）
        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type="attendance",
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code="R1",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            field_diffs={
                "planned_action": planned.action,
                "leave_minutes": planned.leave_minutes,
                "overtime_minutes": planned.overtime_minutes,
            },
        )

        # 顺序 8：写前再次 query（存在 → update，不存在 → create）
        existing = self._query_employee_record(attendance_date, employee)
        if existing is not None and existing.get("__error"):
            return self._record_and_return_failed(
                attempt, existing["__error"], planned, employee
            )
        if existing is not None and existing.get("__anomaly"):
            return self._record_and_return_failed(
                attempt,
                {
                    "code": ERROR_DATA_ANOMALY,
                    "summary": "写前查询发现同一员工同日多条记录（数据异常）",
                },
                planned,
                employee,
            )

        if existing is None:
            action, tool, tool_args = self._create_call(attendance_date, planned, employee)
        else:
            action, tool, tool_args = self._update_call(existing, planned)

        # 顺序 9：调用后 result
        result = self._client.call_tool(
            tool,
            tool_args,
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(
            result, task_id, op_id, target_id=str(employee.employee_id)
        )
        if result.ok:
            record_id = self._extract_record_id(result.data)
            saved = self._audit.record_result(
                attempt_id=attempt.attempt_id,
                result_status="success",
                trace_id=self._trace_id,
                actor_account=self._actor_account,
                actor_role=self._actor_role,
                duty_code="R1",
                policy_key=self._policy.policy_key if self._policy else None,
                policy_version=self._policy.version if self._policy else None,
                policy_hash=self._policy.hash if self._policy else None,
                result_summary=f"{action} attendance record {record_id}",
            )
            logger.info("attendance_write_success", operation_id=op_id, action=action)
            return EmployeeExecutionResult(
                employee_id=employee.employee_id,
                employee_ref=planned.employee_ref,
                planned_action=planned.action,
                final_action=action,
                status="success",
                record_id=record_id,
                attempt_id=saved.attempt_id,
            )
        return self._record_and_return_failed(attempt, result, planned, employee)

    def _record_and_return_failed(
        self,
        attempt: Any,
        failure: Any,
        planned: PlannedRecord,
        employee: EmployeeCandidate,
    ) -> EmployeeExecutionResult:
        """顺序 9 失败分支：记录 failed result（不重试、不循环），返回失败结果。"""
        if isinstance(failure, dict):
            code = failure.get("code") or ERROR_MCP
            summary = failure.get("summary") or code
            operator = bool(failure.get("requires_operator_action"))
        else:
            # ToolResult：错误归类取 error（MCPError）的原始错误码
            err = getattr(failure, "error", None)
            code = getattr(err, "code", None) or getattr(failure, "code", None) or ERROR_MCP
            summary = getattr(err, "message", "") or code
            operator = bool(getattr(err, "requires_operator_action", False))
        saved = self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="failed",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code="R1",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            error_code=code,
            requires_operator_action=operator,
            result_summary=summary,
        )
        logger.warning(
            "attendance_write_failed",
            operation_id=attempt.operation_id,
            error_code=code,
            requires_operator_action=operator,
        )
        return EmployeeExecutionResult(
            employee_id=employee.employee_id,
            employee_ref=planned.employee_ref,
            planned_action=planned.action,
            final_action="failed",
            status="failed",
            error_code=code,
            error_summary=summary,
            attempt_id=saved.attempt_id,
            note="单条失败不重放批次；由任务恢复机制处理（13.2）",
        )

    # ---- 顺序 8：写前查询与分流 ----

    def _query_employee_record(
        self, attendance_date: str, employee: EmployeeCandidate
    ) -> dict[str, Any] | None:
        """写前再次 query 该员工当天记录：单条返回记录 dict；无记录返回 None；
        查询失败/数据异常返回带 __error/__anomaly 标记的 dict。"""
        result = self._client.call_tool(
            "query",
            {
                "entity": "attendance",
                "filter": {
                    "att_date": attendance_date,
                    "employee_id": employee.employee_id,
                },
                "limit": 10,
            },
        )
        if not result.ok:
            return {
                "__error": {
                    "code": getattr(result.error, "code", ERROR_MCP),
                    "summary": getattr(result.error, "message", "写前查询失败"),
                }
            }
        items = result.data.get("items") or []
        if len(items) > 1:
            return {"__anomaly": True}
        return items[0] if items else None

    def _create_call(
        self,
        attendance_date: str,
        planned: PlannedRecord,
        employee: EmployeeCandidate,
    ) -> tuple[str, str, dict[str, Any]]:
        """构造 create 调用（规则 11：None 字段不写 0）。"""
        payload: dict[str, Any] = {
            "employee_id": employee.employee_id,
            "att_date": attendance_date,
        }
        if planned.leave_minutes is not None:
            payload["leave_minutes"] = planned.leave_minutes
        if planned.overtime_minutes is not None:
            payload["overtime_minutes"] = planned.overtime_minutes
        verify_generic_write("create", "attendance", payload)
        return "created", "create", {"entity": "attendance", "data": payload}

    def _update_call(
        self, existing: dict[str, Any], planned: PlannedRecord
    ) -> tuple[str, str, dict[str, Any]]:
        """构造 update 调用：只传本次变更字段（增量口径，规则 4/8）。"""
        record_id = existing.get("id")
        if record_id is None:
            raise AttendanceContractError(
                "attendance", "update", ["id 未在查询结果中返回"]
            )
        payload: dict[str, Any] = {}
        if planned.leave_minutes is not None:
            payload["leave_minutes"] = planned.leave_minutes
        if planned.overtime_minutes is not None:
            payload["overtime_minutes"] = planned.overtime_minutes
        verify_generic_write("update", "attendance", payload)
        return "updated", "update", {"entity": "attendance", "id": record_id, "data": payload}

    # ---- 顺序 11-12：最终一致性验证 ----

    def _final_verify(
        self,
        task_id: str,
        attendance_date: str,
        plan: AttendancePlan,
        results: list[EmployeeExecutionResult],
    ) -> AttendanceExecutionSummary:
        """全部执行后再次 query 当天考勤，验证每位有效员工恰好一条。

        通过 → mark_succeeded(verification_passed=True)（8.1 唯一成功路径）。
        未通过分三档：
        - 部分成功且未达重试上限 → 任务保持 running（current_step=
          awaiting_recovery，4.3 顺序 10 / RV-18：已成功员工不重复写，
          失败员工由恢复执行继续；13.2 单条失败由任务恢复机制处理）；
        - 部分成功但已达重试上限 → 任务 failed（13.2 达上限不循环）；
        - 无失败但验证发现记录缺失/多余 → 任务 failed（写后验证兜底，
          不假报成功）；验证查询本身失败 → failed（结果未知，不假报成功）。
        """
        query = self._client.call_tool(
            "query",
            {
                "entity": "attendance",
                "filter": {"att_date": attendance_date},
                "limit": _VERIFY_QUERY_LIMIT,
            },
        )

        if not query.ok:
            code = getattr(query.error, "code", ERROR_MCP)
            self._fail_task(
                task_id,
                ERROR_VERIFY_FAILED,
                f"最终验证查询失败（{code}）：结果未知，不宣称成功",
            )
            return self._summary(
                task_id, plan, results, verification_passed=False,
                error_code=ERROR_VERIFY_FAILED,
                error_summary=f"最终验证查询失败（{code}）",
            )

        items = query.data.get("items") or []
        by_employee: dict[int, list[dict[str, Any]]] = {}
        for item in items:
            eid = item.get("employee_id")
            if isinstance(eid, int):
                by_employee.setdefault(eid, []).append(item)

        # 每位有效员工恰好一条（4.2 最终验证 / RV-17 通过标准）
        expected = {r.employee_id for r in results}
        consistency_ok = all(len(by_employee.get(eid, [])) == 1 for eid in expected)

        # 回填 record_id：验证查询结果补充每位员工的实际记录 ID
        results = [
            self._backfill_record_id(r, by_employee.get(r.employee_id))
            for r in results
        ]
        failed = [r for r in results if r.status == "failed"]

        if consistency_ok and not failed:
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            logger.info("attendance_task_succeeded", task_id=task_id)
            return self._summary(task_id, plan, results, verification_passed=True)

        if failed:
            # 13.2：达上限的员工不再循环；任一员工达上限 → 任务 failed 终止
            at_limit = [
                r for r in failed
                if len(
                    self._audit.attempts(
                        stable_operation_id(task_id, attendance_date, r.employee_id)
                    )
                ) >= self._retry_limit
            ]
            if at_limit:
                results = [
                    self._mark_at_limit(r) if r.employee_id in {
                        x.employee_id for x in at_limit
                    } and r.status == "failed" else r
                    for r in results
                ]
                names = "、".join(x.employee_ref for x in at_limit)
                summary = (
                    f"部分员工已达重试上限（{names}），任务终止不再循环（13.2）；"
                    "已成功记录保留，需人工处理"
                )
                self._fail_task(task_id, ERROR_RETRY_LIMIT, summary)
                return self._summary(
                    task_id, plan, results, verification_passed=False,
                    error_code=ERROR_RETRY_LIMIT, error_summary=summary,
                )
            # 未达上限：任务保持 running，等待恢复执行（顺序 10 / RV-18）
            self._tasks.update_progress(task_id, current_step="awaiting_recovery")
            names = "、".join(r.employee_ref for r in failed)
            summary = (
                f"部分成功：{len(failed)} 名员工未完成（{names}）；"
                "已成功记录保留，失败员工可继续（恢复执行只处理未完成项）"
            )
            logger.warning("attendance_task_awaits_recovery", task_id=task_id, failed=names)
            return self._summary(
                task_id, plan, results, verification_passed=False,
                error_code=ERROR_PARTIAL, error_summary=summary,
            )

        summary = "最终验证未通过：有效员工记录数不足/多余，需人工核查"
        self._fail_task(task_id, ERROR_VERIFY_FAILED, summary)
        return self._summary(
            task_id, plan, results, verification_passed=False,
            error_code=ERROR_VERIFY_FAILED, error_summary=summary,
        )

    @staticmethod
    def _mark_at_limit(result: EmployeeExecutionResult) -> EmployeeExecutionResult:
        """达重试上限的员工结果归类（13.2：failed 不循环）。"""
        return EmployeeExecutionResult(
            employee_id=result.employee_id,
            employee_ref=result.employee_ref,
            planned_action=result.planned_action,
            final_action="failed",
            status="failed",
            error_code=ERROR_RETRY_LIMIT,
            error_summary="达重试上限，不再循环（13.2）",
            attempt_id=result.attempt_id,
            note=result.note,
        )

    @staticmethod
    def _backfill_record_id(
        result: EmployeeExecutionResult, records: list[dict[str, Any]] | None
    ) -> EmployeeExecutionResult:
        """最终验证记录回填 record_id（4.2 每位员工最终结果含记录 ID）。"""
        if result.record_id is None and records and len(records) == 1:
            rid = records[0].get("id")
            if isinstance(rid, int):
                return EmployeeExecutionResult(
                    employee_id=result.employee_id,
                    employee_ref=result.employee_ref,
                    planned_action=result.planned_action,
                    final_action=result.final_action,
                    status=result.status,
                    record_id=rid,
                    error_code=result.error_code,
                    error_summary=result.error_summary,
                    attempt_id=result.attempt_id,
                    note=result.note,
                )
        return result

    # ---- 汇总与内部 ----

    def _summary(
        self,
        task_id: str,
        plan: AttendancePlan,
        results: list[EmployeeExecutionResult],
        *,
        verification_passed: bool,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> AttendanceExecutionSummary:
        """4.2 六要素汇总：日期/有效员工总数/四类数量/每位员工结果/
        最终验证/恢复位置。四类数量按本次执行的实际结果统计（计划
        数量在 execution_plan 中保留；失败/跳过不虚计为成功）。"""
        counts = {
            "created": sum(1 for r in results if r.final_action == "created"),
            "updated": sum(1 for r in results if r.final_action == "updated"),
            "unchanged": sum(1 for r in results if r.final_action == "unchanged"),
            "failed": sum(1 for r in results if r.status == "failed"),
        }
        resume_position = None
        for r in results:
            # 达上限的员工不可继续（13.2），不提供恢复位置
            if r.status == "failed" and r.error_code != ERROR_RETRY_LIMIT:
                resume_position = (
                    f"上次中断于员工 {r.employee_ref}（该员工本次仍失败，"
                    "恢复执行将从该员工继续）"
                )
                break
        return AttendanceExecutionSummary(
            task_id=task_id,
            attendance_date=plan.attendance_date,
            effective_employee_count=len(results),
            counts=counts,
            employee_results=tuple(results),
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

    @staticmethod
    def _align_employees(
        plan: AttendancePlan, employees: Iterable[EmployeeCandidate]
    ) -> dict[str, EmployeeCandidate]:
        """plan 引用（员工名）→ 唯一匹配后的候选；不一致拒绝执行（4.3 顺序 2）。"""
        mapping = {e.name: e for e in employees}
        missing = [
            p.employee_ref for p in plan.all_records if p.employee_ref not in mapping
        ]
        if missing:
            raise ValueError(
                f"plan 引用 {missing} 未在有效员工集合中唯一匹配；"
                "4.3 顺序 2 歧义应先询问，不执行写入"
            )
        return mapping

    def _assert_contract(self, entity: EntityMetadata) -> None:
        """9.5：标准字段 → ERP 实际字段可写性校验（create/update 各自口径）。

        缺字段或不可写 → AttendanceContractError，调用方停止并报告接口
        契约错误（本方法不发出任何写调用）。
        """
        for operation, fields in (
            ("create", _CREATE_REQUIRED_WRITABLE),
            ("update", _UPDATE_REQUIRED_WRITABLE),
        ):
            missing: list[str] = []
            for std_name in fields:
                erp_name = ATTENDANCE_FIELD_MAP[std_name]
                spec = entity.fields.get(erp_name)
                if spec is None:
                    missing.append(erp_name)
                    continue
                writable = (
                    spec.writable_on_create
                    if operation == "create"
                    else spec.writable_on_update
                )
                if not writable:
                    missing.append(f"{erp_name}(不可写)")
            if missing:
                raise AttendanceContractError(entity.name, operation, missing)

    def _record_mcp_retries(
        self,
        result: ToolResult,
        task_id: str,
        op_id: str,
        target_id: str | None = None,
    ) -> None:
        """写工具的传输层瞬时重试留痕（见 AuditService.record_mcp_retries）。"""
        if not result.retries:
            return
        self._audit.record_mcp_retries(
            retries=result.retries,
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            target_type="attendance",
            target_id=target_id,
            duty_code="R1",
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )

    @staticmethod
    def _extract_record_id(data: dict[str, Any]) -> int | None:
        """create/update 响应取 record.id（mcp-api.md §5 成功响应形态）。"""
        record = data.get("record")
        if isinstance(record, dict):
            rid = record.get("id")
            if isinstance(rid, int):
                return rid
        rid = data.get("id")
        return rid if isinstance(rid, int) else None

    def _fail_task(self, task_id: str, error_code: str, error_summary: str) -> None:
        """任务标记 failed（8.1/13.2：需人工处理或重新发起；不假报成功）。"""
        try:
            self._tasks.mark_failed(
                task_id, error_code=error_code, error_summary=error_summary
            )
        except Exception:
            # 任务已终态（如复核拒绝后 cancelled）时忽略；审计失败路径由
            # 调用方（10.4 mark_manual_review_required 语义）另行处理
            logger.exception("attendance_fail_task_error", task_id=task_id)
