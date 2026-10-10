"""对话对账报告域（规格 2.6 domains/reconcile_reports.py；2026-08-31
对话对账批次 1）。

对话场景的对账报告（D19 ReconciliationReport）跨轮确认载体：
- 落库 reconcile_reports（report_id=rpt-<uuid16>），TTL
  reconcile_report_ttl_seconds 后过期清理（跨轮确认窗口，配置化）；
- status 状态机：analyzed → confirmed → executed，外加回退边
  confirmed → analyzed（执行失败/部分成功后报告回退待确认，可重新走
  确认卡重试——13.2 恢复机制在对话路径的落点；executed 仍为终态）；
  非法迁移抛 ValueError（fail closed，DB 层 CheckConstraint 兜底）；
- plan_hash：confirm 后回填（写入计划 hash，与 7.4 复核机制联动）；
- 序列化：report_to_json 把 ReconciliationReport + 标准行 rows
  （dict[int, dict[str, str]]）拆成五个 *_json 列文本；report_from_json
  全量重建 ReconciliationReport 对象（含 MatchOutcome/RowError/
  DiffEntry/FieldDiff/MatchKey 类型）——build_write_plan
  （business/reconciliation_plan.py L130）的输入是 ReconciliationReport
  对象本身（遍历 entries 的 typed 属性 write_entry/diffs），
  不是 dict，故必须全量重建；rows 只用 dict 形态（execute 阶段
  create 载荷来源，行缺失不编造），无需 StandardRow 类型重建。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, TypeAlias

from sqlalchemy import delete
from sqlalchemy.orm import Session

from administrative_agent.business.matching import MatchOutcome
from administrative_agent.business.reconciliation_rules import (
    DiffEntry,
    FieldDiff,
    MatchKey,
)
from administrative_agent.config import settings
from administrative_agent.domains.reconciliation import ReconciliationReport
from administrative_agent.files.excel_parser import RowError
from administrative_agent.persistence.models import ReconcileReport as ReconcileReportRow

#: 会话工厂（sessionmaker 或函数）；与 persistence/repositories.py 同款
SessionFactory: TypeAlias = Callable[[], Session]

#: 报告状态（reconcile_reports.status；迁移见 _STATUS_TRANSITIONS）
REPORT_STATUS_ANALYZED = "analyzed"
REPORT_STATUS_CONFIRMED = "confirmed"
REPORT_STATUS_EXECUTED = "executed"
REPORT_STATUSES: frozenset[str] = frozenset(
    {REPORT_STATUS_ANALYZED, REPORT_STATUS_CONFIRMED, REPORT_STATUS_EXECUTED}
)

#: 合法状态迁移（analyzed → confirmed → executed，外加回退边
#: confirmed → analyzed——执行失败/部分成功回退待确认，可重新确认重试；
#: executed 仍为终态；其余一律拒绝）
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    REPORT_STATUS_ANALYZED: frozenset({REPORT_STATUS_CONFIRMED}),
    REPORT_STATUS_CONFIRMED: frozenset(
        {REPORT_STATUS_EXECUTED, REPORT_STATUS_ANALYZED}
    ),
    REPORT_STATUS_EXECUTED: frozenset(),
}


# ---------------------------------------------------------------------------
# 领域记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileReportRecord:
    """reconcile_reports 行记录（report_id/expires_at 由仓储生成）。

    - summary_json/conversion_errors_json/entries_json/rows_json/
      limitations_json：JSON 文本列（report_to_json 产出）；
    - status：analyzed/confirmed/executed（合法迁移由仓储校验）；
    - plan_hash：默认 ''，confirm 后回填（7.4 复核机制联动）。
    """

    report_id: str = ""
    thread_id: str = ""
    file_id: str = ""  # 附件 id（chat_attachments.attachment_id）
    template_key: str = ""
    mapping_version: int = 0
    entity: str = ""
    subject_name: str = ""
    period_note: str = ""
    source_file_hash: str = ""
    row_count: int = 0
    summary_json: str = "{}"
    conversion_errors_json: str = "[]"
    entries_json: str = "[]"
    rows_json: str = "{}"
    data_completeness: str = "complete"
    limitations_json: str = "[]"
    status: str = REPORT_STATUS_ANALYZED
    plan_hash: str = ""
    created_at: datetime | None = None
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "thread_id": self.thread_id,
            "file_id": self.file_id,
            "template_key": self.template_key,
            "mapping_version": self.mapping_version,
            "entity": self.entity,
            "subject_name": self.subject_name,
            "period_note": self.period_note,
            "source_file_hash": self.source_file_hash,
            "row_count": self.row_count,
            "summary_json": self.summary_json,
            "conversion_errors_json": self.conversion_errors_json,
            "entries_json": self.entries_json,
            "rows_json": self.rows_json,
            "data_completeness": self.data_completeness,
            "limitations_json": self.limitations_json,
            "status": self.status,
            "plan_hash": self.plan_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _to_record(row: ReconcileReportRow) -> ReconcileReportRecord:
    return ReconcileReportRecord(
        report_id=row.report_id,
        thread_id=row.thread_id,
        file_id=row.file_id,
        template_key=row.template_key,
        mapping_version=row.mapping_version,
        entity=row.entity,
        subject_name=row.subject_name,
        period_note=row.period_note,
        source_file_hash=row.source_file_hash,
        row_count=row.row_count,
        summary_json=row.summary_json,
        conversion_errors_json=row.conversion_errors_json,
        entries_json=row.entries_json,
        rows_json=row.rows_json,
        data_completeness=row.data_completeness,
        limitations_json=row.limitations_json,
        status=row.status,
        plan_hash=row.plan_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


# ---------------------------------------------------------------------------
# 仓储（SQLAlchemy；reconcile_reports）
# ---------------------------------------------------------------------------


class ReconcileReportStore:
    """reconcile_reports 持久化仓储（2026-08-31 对话对账批次 1）。

    create 生成 report_id（rpt-<uuid16>）与 expires_at（now +
    reconcile_report_ttl_seconds）；update_status 允许
    analyzed→confirmed→executed 与 confirmed→analyzed 回退（执行失败/
    部分成功；executed 仍为终态，非法迁移 ValueError，fail closed）；
    delete_expired 供启动时 + analyze/读取时清理过期报告。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def create(self, record: ReconcileReportRecord) -> ReconcileReportRecord:
        """落库报告（report_id/expires_at 由本方法生成）。"""
        now = datetime.now(timezone.utc)
        rec = replace(
            record,
            report_id=record.report_id or f"rpt-{uuid.uuid4().hex[:16]}",
            expires_at=record.expires_at
            or now + timedelta(seconds=settings.reconcile_report_ttl_seconds),
        )
        row = ReconcileReportRow(
            report_id=rec.report_id,
            thread_id=rec.thread_id,
            file_id=rec.file_id,
            template_key=rec.template_key,
            mapping_version=rec.mapping_version,
            entity=rec.entity,
            subject_name=rec.subject_name,
            period_note=rec.period_note,
            source_file_hash=rec.source_file_hash,
            row_count=rec.row_count,
            summary_json=rec.summary_json,
            conversion_errors_json=rec.conversion_errors_json,
            entries_json=rec.entries_json,
            rows_json=rec.rows_json,
            data_completeness=rec.data_completeness,
            limitations_json=rec.limitations_json,
            status=rec.status,
            plan_hash=rec.plan_hash,
            expires_at=rec.expires_at,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return _to_record(row)

    def get(self, report_id: str) -> ReconcileReportRecord | None:
        with self._factory() as session:
            row = session.get(ReconcileReportRow, report_id)
            return _to_record(row) if row is not None else None

    def update_status(
        self,
        report_id: str,
        status: str,
        *,
        plan_hash: str | None = None,
    ) -> ReconcileReportRecord:
        """状态迁移（analyzed→confirmed→executed + confirmed→analyzed
        回退；非法拒绝 fail closed）。

        - status 未知 → ValueError；
        - 迁移不在 _STATUS_TRANSITIONS 内（如 executed→analyzed 回退、
          跳过 confirmed 直升 executed）→ ValueError，不猜测；
        - confirmed→analyzed 仅用于执行失败/部分成功后回退待确认
          （13.2 恢复机制：可重新走确认卡重试，已完成条目按幂等/写前
          重查跳过）；executed 为终态，不可回退；
        - plan_hash 非 None 时回填（confirm 后携带写入计划 hash）。
        """
        if status not in REPORT_STATUSES:
            raise ValueError(
                f"对账报告状态 {status!r} 非法（允许 {sorted(REPORT_STATUSES)}）"
            )
        with self._factory() as session:
            row = session.get(ReconcileReportRow, report_id)
            if row is None:
                raise KeyError(f"reconcile report not found: {report_id}")
            if status != row.status:
                allowed = _STATUS_TRANSITIONS[row.status]
                if status not in allowed:
                    raise ValueError(
                        f"对账报告 {report_id} 状态迁移非法："
                        f"{row.status} → {status}（仅允许 "
                        "analyzed→confirmed→executed 与 confirmed→analyzed"
                        " 回退，fail closed）"
                    )
                row.status = status
            if plan_hash is not None:
                row.plan_hash = plan_hash
            session.commit()
            session.refresh(row)
        return _to_record(row)

    def delete_expired(self) -> int:
        """删除已过期报告（expires_at <= now，含到期时刻），返回删除条数。"""
        now = datetime.now(timezone.utc)
        with self._factory() as session:
            result = session.execute(
                delete(ReconcileReportRow).where(ReconcileReportRow.expires_at <= now)
            )
            session.commit()
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# 序列化：ReconciliationReport + rows → 落库 JSON 文本 / 全量重建
# ---------------------------------------------------------------------------


def report_to_json(
    report: ReconciliationReport,
    *,
    rows: Mapping[int, Mapping[str, str]],
) -> dict[str, str]:
    """对账报告 → 五个 *_json 列文本（键与 reconcile_reports 列同名）。

    - summary_json：信封结构（reconcile_reports 无 task_id/subject_match
      独立列，全量重建所需，本列承载）：
      {"task_id": …, "summary": {分类: 计数}, "subject_match": {ref/status/
      entity_id/matched_name/candidates}}；
    - conversion_errors_json/entries_json/limitations_json：取自
      report.to_dict()（entries 为 DiffEntry.to_dict() 全量，含
      write_entry/diffs/match_key）；
    - rows_json：row_number → 标准行值（StandardRow.values 的 dict
      形态）；JSON 对象键只能是字符串，行号转 str 落库（读取时用
      rows_from_json 还原为 int 键）。
    """
    payload = report.to_dict()
    return {
        "summary_json": json.dumps(
            {
                "task_id": report.task_id,
                "summary": payload["summary"],
                "subject_match": payload["subject_match"],
            },
            ensure_ascii=False,
        ),
        "conversion_errors_json": json.dumps(
            payload["conversion_errors"], ensure_ascii=False
        ),
        "entries_json": json.dumps(payload["entries"], ensure_ascii=False),
        "rows_json": json.dumps(
            {str(k): dict(v) for k, v in rows.items()}, ensure_ascii=False
        ),
        "limitations_json": json.dumps(payload["limitations"], ensure_ascii=False),
    }


def report_from_json(row: Mapping[str, Any] | ReconcileReportRecord) -> ReconciliationReport:
    """从落库载荷重建 ReconciliationReport 对象（build_write_plan 需要
    typed 对象：遍历 report.entries 的 write_entry/diffs 属性）。

    row 可接受三种形态：
    - ReconcileReportRecord（*_json 列为 JSON 文本，本函数自行 json.loads；
      task_id/subject_match 自 summary_json 信封取出）；
    - 解析后的 report dict（report.to_dict() 结构：subject_match/
      conversion_errors/entries/…）；
    - 普通 dict（*_json 文本列或已解析对象均可）。
    DiffEntry/FieldDiff/MatchKey/RowError/MatchOutcome 全量类型重建；
    rows 不属 report（独立存 rows_json，见 rows_from_json）。
    """
    if isinstance(row, ReconcileReportRecord):
        row = asdict(row)
    envelope = _parse(row.get("summary_json") or {})
    if not isinstance(envelope, Mapping):
        envelope = {}
    subject = envelope.get("subject_match") or row.get("subject_match") or {}
    errors = _parse(row.get("conversion_errors_json") or row.get("conversion_errors") or [])
    entries = _parse(row.get("entries_json") or row.get("entries") or [])
    limitations = _parse(row.get("limitations_json") or row.get("limitations") or [])
    if not isinstance(limitations, (list, tuple)):
        limitations = (
            list(limitations.values())
            if isinstance(limitations, Mapping)
            else []
        )
    return ReconciliationReport(
        task_id=str(row.get("task_id") or envelope.get("task_id") or ""),
        template_key=str(row.get("template_key") or ""),
        mapping_version=int(row.get("mapping_version") or 0),
        source_file_hash=str(row.get("source_file_hash") or ""),
        entity=str(row.get("entity") or ""),
        subject_match=_subject_from_dict(subject),
        period_note=str(row.get("period_note") or ""),
        row_count=int(row.get("row_count") or 0),
        conversion_errors=tuple(_row_error_from_dict(e) for e in errors),
        entries=tuple(_diff_entry_from_dict(e) for e in entries),
        data_completeness=str(row.get("data_completeness") or "complete"),
        limitations=tuple(str(x) for x in limitations),
    )


def rows_from_json(rows_json: str) -> dict[int, dict[str, str]]:
    """rows_json 文本 → dict[int, dict[str, str]]（行号还原为 int 键）。

    execute 阶段按 row_number 取标准行（行缺失不编造），键必须为 int。
    """
    raw = json.loads(rows_json) if isinstance(rows_json, str) else dict(rows_json)
    return {int(k): dict(v) for k, v in raw.items()}


# ---- 类型重建辅助 ----


def _parse(value: Any) -> Any:
    """文本 → 解析为 Python 对象（已解析对象原样返回）。"""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _subject_from_dict(data: Mapping[str, Any]) -> MatchOutcome:
    candidates = tuple(
        (int(c[0]), str(c[1]))
        for c in (data.get("candidates") or [])
    )
    return MatchOutcome(
        ref=str(data.get("ref") or ""),
        status=str(data.get("status") or "not_found"),
        entity_id=(
            int(data["entity_id"])
            if data.get("entity_id") is not None
            else None
        ),
        matched_name=(
            str(data["matched_name"])
            if data.get("matched_name") is not None
            else None
        ),
        candidates=candidates,
    )


def _row_error_from_dict(data: Mapping[str, Any]) -> RowError:
    return RowError(
        row_number=int(data.get("row_number") or 0),
        excel_column=str(data.get("excel_column") or ""),
        target_field=str(data.get("target_field") or ""),
        reason=str(data.get("reason") or ""),
    )


def _match_key_from_dict(data: Mapping[str, Any] | None) -> MatchKey | None:
    if not data:
        return None
    return MatchKey(
        entity=str(data.get("entity") or ""),
        subject_id=int(data.get("subject_id") or 0),
        product_ref=str(data.get("product_ref") or ""),
        in_date=str(data.get("in_date") or ""),
    )


def _field_diff_from_dict(data: Mapping[str, Any]) -> FieldDiff:
    return FieldDiff(
        field=str(data.get("field") or ""),
        excel_value=(
            str(data["excel_value"])
            if data.get("excel_value") is not None
            else None
        ),
        erp_value=(
            str(data["erp_value"])
            if data.get("erp_value") is not None
            else None
        ),
        kind=str(data.get("kind") or "regular"),
    )


def _diff_entry_from_dict(data: Mapping[str, Any]) -> DiffEntry:
    return DiffEntry(
        classification=str(data.get("classification") or ""),
        row_number=int(data.get("row_number") or 0),
        source_file_hash=str(data.get("source_file_hash") or ""),
        mapping_version=int(data.get("mapping_version") or 0),
        match_key=_match_key_from_dict(data.get("match_key")),
        matched_order_id=(
            int(data["matched_order_id"])
            if data.get("matched_order_id") is not None
            else None
        ),
        diffs=tuple(_field_diff_from_dict(d) for d in (data.get("diffs") or [])),
        write_entry=(
            str(data["write_entry"])
            if data.get("write_entry") is not None
            else None
        ),
        message=str(data.get("message") or ""),
    )
