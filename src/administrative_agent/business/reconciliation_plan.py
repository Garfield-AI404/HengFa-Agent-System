"""R6 写入计划生成（规格 2.6 business/reconciliation_plan.py；
6.4 步骤 10 L837、7.4 复核对象、RV-30）。

口径（6.4 L837 / 7.4 / L871）：
- 步骤 10"manager 复核具体写入计划"：本模块把 D19 差异报告（6.4 步骤 9）
  转换为**具体写入计划**——每条可写差异一行：目标记录 id、写路径
  （create_order_bundle / update_order，取自 D19 write_entry 标记）、
  写入字段和值（前后值，取自 D19 diffs）、幂等键预留；
- 只生成不执行（L871"R6 差异写入 manager 复核后执行"）——本模块是纯
  函数，无网络/无数据库/无写入口（fail closed）；
- 只有 write_entry 非 None 的差异条目进入计划（D19 已按 L843/L845/
  场景 9 完成标记；无标记的条目只展示不写）；
- 计划可序列化为规范化 payload（键序稳定），供 reviewed_payload 落库与
  reviewed_payload_hash 计算（7.4 复核记录字段；payload hash 变化即
  计划业务变化 → 原复核失效，14.1 L1380 场景）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from administrative_agent.business.reconciliation_rules import (
    WRITE_CREATE_ORDER_BUNDLE,
    WRITE_UPDATE_ORDER,
    FieldDiff,
)
from administrative_agent.domains.reconciliation import ReconciliationReport

#: 可执行写路径（6.4 步骤 12：缺失订单 create_order_bundle、已有订单
#: update_order；D19 write_entry 常量与之一致）
WRITE_PATHS = frozenset({WRITE_CREATE_ORDER_BUNDLE, WRITE_UPDATE_ORDER})

#: payload 规范化键序（reviewed_payload 结构稳定，hash 可复现）
_PAYLOAD_KEYS = (
    "task_id", "template_key", "mapping_version", "source_file_hash", "entity",
    "items",
)
_ITEM_KEYS = (
    "row_number", "classification", "matched_order_id", "write_path",
    "fields", "diffs", "idempotency_key",
)
_DIFF_KEYS = ("field", "excel_value", "erp_value", "kind")


@dataclass(frozen=True)
class WritePlanItem:
    """一条可写差异的写入计划（6.4 步骤 12 对应 D21 执行条目）。

    - matched_order_id：update 目标记录 id；create（erp_missing）为 None；
    - write_path：create_order_bundle | update_order（D19 write_entry）；
    - fields：写入字段和值（(field, excel_value) 有序；create 条目在
      D19 阶段无逐字段值——字段由 D21 从标准行取，此处不编造）；
    - diffs：字段差异前后值（D20 复核页展示；update 条目）；
    - idempotency_key：D21 执行前生成（本单元只预留字段，不生成）。
    """

    row_number: int
    classification: str
    write_path: str
    matched_order_id: int | None = None
    fields: tuple[tuple[str, str], ...] = ()
    diffs: tuple[dict[str, Any], ...] = ()
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "classification": self.classification,
            "matched_order_id": self.matched_order_id,
            "write_path": self.write_path,
            "fields": [list(f) for f in self.fields],
            "diffs": list(self.diffs),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class WritePlan:
    """6.4 步骤 10 写入计划（只生成不执行；复核对象）。

    generated_at 只作展示/审计说明，不进入 payload hash（hash 只覆盖
    确定性业务字段——同一计划任何时候 hash 一致，7.4/7.3 复核机制前提）。
    """

    task_id: str
    template_key: str
    mapping_version: int
    source_file_hash: str
    entity: str
    items: tuple[WritePlanItem, ...] = ()
    generated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        """规范化 payload（键序固定；供 reviewed_payload 与 hash 计算）。

        generated_at 不入 payload：时间戳会使同一计划的 hash 每次不同，
        复核"批准当前 payload_hash"（2.7.3 L514）就无法成立。
        """
        return {
            "task_id": self.task_id,
            "template_key": self.template_key,
            "mapping_version": self.mapping_version,
            "source_file_hash": self.source_file_hash,
            "entity": self.entity,
            "items": [i.to_dict() for i in self.items],
        }

    @property
    def payload_hash(self) -> str:
        """reviewed_payload_hash（7.4；sha256，D19 files/hashing 同算法）。

        对规范化 payload 做确定序列化（键序固定 + ensure_ascii=False +
        sort_keys=True 兜底）再哈希——同计划同哈希，变化即差异。
        """
        payload = self.to_payload()
        raw = json.dumps(
            _ordered(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def count(self) -> int:
        """计划条目数（可写差异行数）。"""
        return len(self.items)


def build_write_plan(report: ReconciliationReport) -> WritePlan:
    """6.4 步骤 10：D19 差异报告 → 写入计划（纯函数，零写）。

    - 只取 write_entry 非 None 的条目（D19 按 L843/L845/场景 9 标记）；
    - 未标记条目（matched/ambiguous/无写路径的 field_diff 等）不进计划，
      复核页差异清单仍可见（D20 详情展示来源为报告，见 demo）；
    - write_path 取自 write_entry；非已知写路径 fail closed（防御）。
    """
    items: list[WritePlanItem] = []
    for entry in report.entries:
        if entry.write_entry is None:
            continue
        if entry.write_entry not in WRITE_PATHS:
            raise ValueError(
                f"未知写路径 {entry.write_entry!r}（行 {entry.row_number}）；"
                "写入计划只接受 create_order_bundle/update_order"
            )
        diffs: list[dict[str, Any]] = []
        fields: list[tuple[str, str]] = []
        for d in entry.diffs:
            diffs.append(_diff_to_dict(d))
            # 写入字段和值：excel 侧值（前后值中的"新值"）；缺失不编造
            if d.excel_value is not None:
                fields.append((d.field, d.excel_value))
        items.append(
            WritePlanItem(
                row_number=entry.row_number,
                classification=entry.classification,
                write_path=entry.write_entry,
                matched_order_id=entry.matched_order_id,
                fields=tuple(fields),
                diffs=tuple(diffs),
                idempotency_key=None,  # D21 执行前生成（预留字段）
            )
        )
    return WritePlan(
        task_id=report.task_id,
        template_key=report.template_key,
        mapping_version=report.mapping_version,
        source_file_hash=report.source_file_hash,
        entity=report.entity,
        items=tuple(items),
    )


def _diff_to_dict(diff: FieldDiff) -> dict[str, Any]:
    return {
        "field": diff.field,
        "excel_value": diff.excel_value,
        "erp_value": diff.erp_value,
        "kind": diff.kind,
    }


def _ordered(value: Any) -> Any:
    """递归规范化：dict 键排序（hash 确定序列化兜底）。"""
    if isinstance(value, Mapping):
        return {k: _ordered(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_ordered(v) for v in value]
    return value
