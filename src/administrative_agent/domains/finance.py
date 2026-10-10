"""R7 收支写执行编排（薄执行器，仿 domains/attendance.py；更新侧确认
失效/重查语义仿 domains/orders.py OrderUpdateExecutor，R3 规则 6/7）。

FinanceCreateExecutor / FinanceUpdateExecutor：
- 写目标为 transactions（收支记录）；通用 create/update 直接写入
  （mcp-api.md §5；ERP agent_office 权限已实测 create/update 生效，
  delete 未开且用户明确不开放删权限）；
- 载荷安全：create/update 前经 scopes.verify_generic_write 本地拦截
  （fail closed）——系统字段与 source_type/source_id 恒禁止（transactions
  侧 amount 是业务字段，由 scopes 实体感知放行，订单/考勤侧 amount
  仍拒）；
- **通用 create 无幂等键**（mcp-api.md §5）：网络结果未知不盲目重试
  （重试可能重复落库）——改走写后验证：按业务签名（类型/分类/日期/
  金额等写入字段，金额数值归一化）查询，命中视为已应用（不二次写）；
  未命中或复查查询失败 → failed（requires_operator_action=True，
  结果未知需人工核查，13.1）；
- **通用 update 无 expected_updated_at 服务端乐观锁**：本地乐观锁——
  写前重查当前记录 → 重建确认卡 hash 与任务 confirmed_payload_hash
  比对（内容/目标变化 → 确认失效须重新确认，7.3，不覆盖他人新写入）；
  expected_updated_at 仅随审计列记录，不随 update 载荷发给 ERP；
  结果未知（NETWORK/SERVER）→ 先重查再决定，绝不盲目重复 update
  （R3 规则 7 语义：期望变更已应用 → 按最新快照展示，不重复调用）；
- 分类强校验（R7）：写入前**再次**查询 transaction_categories 复核分类
  归属（确认后分类表可能变化——防御性复核，fail closed 不写）；
- 金额字符串格式 + 域层 Decimal 校验（>0，两位小数规范化，与 ERP
  transactions.amount 展示口径一致，如 "1650.00"）。

依赖方向（2.6 规则 3）：只依赖 mcp/audit/tasks 与 domains/orders 的纯
规则层（ConfirmationCard/payload_hash_for/localize_confirmation_fields/
build_update_changes/is_confirmation_valid），不依赖 web/agent。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Protocol, Sequence

import structlog

from administrative_agent.audit.service import AuditService, TaskAttemptRecord
from administrative_agent.domains.batch import (
    BatchExecutor,
    BatchItem,
    BatchItemResult,
    BatchSummary,
)
from administrative_agent.domains.orders import (
    ACTION_CREATE,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    BATCH_HASH_PREFIX,
    DETAIL_ACTION_KEY,
    DETAIL_FIELDS_KEY,
    ConfirmationCard,
    build_update_changes,
    card_payload_hash,
    is_confirmation_valid,
    localize_confirmation_fields,
)
from administrative_agent.mcp.client import MCPClient, ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.scopes import verify_generic_write
from administrative_agent.tasks.service import TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.finance")

#: 操作类型（10.2：operation 来自代码枚举，不接受用户提供）
OP_CREATE_TRANSACTION = "创建收支记录"
OP_UPDATE_TRANSACTION = "更新收支记录"

#: [2026-10-05 确认卡改进第 3 批] 收支卡固定口径提示（纯展示级 source_note，
#: 不进 payload_hash）。治「分类串位看不见来源」（模型按用户说法判定可落错
#: 分类时由用户在确认前比对修正）。仅收支卡设置；订单卡/考勤卡不带本键。
FINANCE_SOURCE_NOTE = (
    "分类由模型按你的说法判定；若与你的说法不符，请在确认前修改"
)


def _finance_source_note(field_keys: Iterable[str]) -> str | None:
    """收支卡 ``source_note`` 判据（纯展示级；不进 payload_hash）。

    仅当卡面**确实含「分类」字段**时才挂提示——判据锚在原始字段名
    ``category``（中文化后为「分类」，见 CONFIRMATION_FIELD_LABELS）。

    为什么这样收窄：收支**更新卡**的 fields_to_write 只列本次差异字段，
    「只改金额」的卡面没有分类行，若仍挂「分类由模型判定、不符请改」会让
    用户困惑「我没改分类」。故只在卡面出现分类字段时提示。

    ``project``（项目）**不计入**：它是与分类并列的独立字段，本提示文案
    只谈「分类判定」；若把「只改项目」的卡也算作含分类，会复现同款困惑
    （卡面无「分类」行却有分类提示），方向相反而已。B18r1 的错法
    （category=水费 / project=电费）始终涉及 category，锚 category 即可覆盖。
    """
    return FINANCE_SOURCE_NOTE if "category" in field_keys else None


#: 审计职责编号（task_attempts/audit_events duty_code，11.1）
DUTY_CODE = "R7"

#: 任务终态错误码（13.1；R7 收支写）
ERROR_CATEGORY_INVALID = "FINANCE_CATEGORY_INVALID"
ERROR_UPDATE_NOT_FOUND = "FINANCE_UPDATE_NOT_FOUND"
ERROR_UPDATE_NO_CHANGES = "FINANCE_UPDATE_NO_CHANGES"
ERROR_UPDATE_CONFIRMATION_INVALID = "FINANCE_UPDATE_CONFIRMATION_INVALID"
ERROR_NETWORK_UNKNOWN = "FINANCE_NETWORK_UNKNOWN"
ERROR_MCP_FAILURE = "FINANCE_MCP_FAILURE"
ERROR_PAYLOAD_FORBIDDEN = "FINANCE_PAYLOAD_FORBIDDEN"
ERROR_AMOUNT_INVALID = "FINANCE_AMOUNT_INVALID"
#: 响应 ok 但未回传 record.id（mcp-api.md §5 成功响应应含 id，缺 id 属
#: 契约异常）→ 写后验证未命中时任务 fail 用码
ERROR_RESPONSE_UNVERIFIED = "FINANCE_RESPONSE_UNVERIFIED"

#: 写前/写后查询字段（changes 比较 + 签名验证所需业务字段 + id/updated_at）
_TRANSACTION_QUERY_FIELDS = (
    "id", "tx_type", "category", "project", "amount", "tx_date",
    "notes", "customer_id", "supplier_id", "updated_at",
)

#: transaction_categories 的分类名字段（ERP 元数据实测 2026-09-02；
#: 规则包措辞按 plan §风险 不绑定字段名，实现侧集中在两处共用）
CATEGORY_FIELD = "category_name"

#: 分类复查/签名验证查询窗口上限（13.3：ERP query limit 上限 200）
_QUERY_LIMIT = 200


class PolicyInfo(Protocol):
    """规则包的最小结构化视图（policy 三列进审计，11.1）。
    调用方传入 agent/prompts/policies.POLICY_REGISTRY 对应条目。"""

    policy_key: str
    version: str

    @property
    def hash(self) -> str: ...


def normalize_amount(value: Any) -> str:
    """金额字符串规范化（两位小数，ERP transactions.amount 展示同口径）。

    Decimal 解析 >0 且有限（R7：金额字符串格式 + 域层 Decimal 校验；
    方向语义由 tx_type 承担，金额恒为正）。非法输入抛 ValueError
    （fail closed），返回形如 "165.00" 的规范化字符串。
    """
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(
            f"amount 必须是数字（元，大于 0），收到 {value!r}；"
            "禁止文字、负数或公式（如 165 或 165.00）"
        ) from None
    if not dec.is_finite() or dec <= 0:
        raise ValueError(
            f"amount 必须是大于 0 的有限数字，收到 {value!r}"
        )
    try:
        quantized = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # 超出上下文精度的极端输入 → fail closed
        raise ValueError(f"amount 超出可处理精度：{value!r}") from None
    return str(quantized)


def _amount_equals(a: Any, b: Any) -> bool:
    """金额数值相等比较（ERP 返回字符串小数，如 "1650.00"）。"""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError, TypeError):
        return str(a) == str(b)


def _payload_signature_key(
    payload: Mapping[str, Any],
) -> tuple[tuple[str, Any], ...]:
    """载荷的业务签名键（与 ``_signature_matches`` 同口径的**分组键**）。

    逐字段参与；金额一律转 ``Decimal``（数值相等即键相等，**与
    ``_amount_equals`` 的数值语义一致**，「100」与「100.00」归入同一分组），
    其余字段按原值。用作「同批次内同签名明细」的次序统计键（见
    ``_signature_ordinals``）。
    """
    parts: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if key == "amount":
            try:
                value = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                pass
        parts.append((key, value))
    return tuple(sorted(parts))


def _signature_ordinals(
    items: Sequence["BatchItem"],
) -> dict[int, int]:
    """统计每条明细的业务签名在**本批内**的**出现次序**（1 起，按 index）。

    不能用 item_id 的 ``#n`` 后缀替代：item_id 含 amount 但不含
    project/notes/外键，而 ``_verify_applied`` 的签名是**完整载荷**——两者
    口径不同，故直接按载荷签名重新计数。同批次两次调用产出同一序列（稳定），
    恢复执行时次序不变。
    """
    seen: Counter[tuple[tuple[str, Any], ...]] = Counter()
    ordinals: dict[int, int] = {}
    for item in items:
        key = _payload_signature_key(item.payload)
        seen[key] += 1
        ordinals[item.index] = seen[key]
    return ordinals


def _extract_record_id(data: dict[str, Any]) -> int | None:
    """create/update 响应取 record.id（mcp-api.md §5 成功响应形态）。"""
    record = data.get("record")
    if isinstance(record, dict):
        rid = record.get("id")
        if isinstance(rid, int):
            return rid
    rid = data.get("id")
    return rid if isinstance(rid, int) else None


def _policy_fields(policy: PolicyInfo | None) -> dict[str, str | None]:
    """审计 policy 三列（key/version/hash；11.1 无规则包时为 None）。"""
    return {
        "policy_key": policy.policy_key if policy else None,
        "policy_version": policy.version if policy else None,
        "policy_hash": policy.hash if policy else None,
    }


# ---------------------------------------------------------------------------
# 输出汇总
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinanceCreateSummary:
    """finance_create 执行汇总。

    - result_status：success / failed（11.2 两值；通用 create 无
      replayed/conflict 语义——幂等不确定性由写后验证承担）；
    - record_id/record_response：ERP create 响应（record.id；响应未回传
      id 时经写后验证命中行回填，仍无则 None）；
    - applied_via_post_verify：True = 网络结果未知后写后验证确认已应用
      （任务 succeeded，未重复写入）。
    """

    task_id: str
    operation_type: str
    result_status: str
    record_id: int | None
    record_response: dict[str, Any] | None
    attempts: int
    verification_passed: bool
    applied_via_post_verify: bool = False
    error_code: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "result_status": self.result_status,
            "record_id": self.record_id,
            "record_response": dict(self.record_response or {}),
            "attempts": self.attempts,
            "verification_passed": self.verification_passed,
            "applied_via_post_verify": self.applied_via_post_verify,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


@dataclass(frozen=True)
class FinanceUpdateSummary:
    """finance_update 执行汇总（R3 UpdateExecutionSummary 同构口径）。

    - before_snapshot：写前查询快照；after_snapshot：ERP 响应记录或
      结果未知后重查的最新快照；
    - changed_fields：本次实际变化字段（通用 update 响应不返回派生影响，
      以差异字段集合为准）；
    - refetched_changes：结果未知后重新查询重新生成的差异（内容变化 →
      需重新确认，不覆盖新数据）。
    """

    task_id: str
    operation_type: str
    record_id: int | None
    expected_updated_at: str
    changes: dict[str, Any]
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any] | None
    changed_fields: tuple[str, ...]
    result_status: str
    attempts: int
    verification_passed: bool
    error_code: str | None = None
    error_summary: str | None = None
    refetched_changes: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "record_id": self.record_id,
            "expected_updated_at": self.expected_updated_at,
            "changes": dict(self.changes),
            "before_snapshot": dict(self.before_snapshot or {}),
            "after_snapshot": dict(self.after_snapshot or {}),
            "changed_fields": list(self.changed_fields),
            "result_status": self.result_status,
            "attempts": self.attempts,
            "verification_passed": self.verification_passed,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "refetched_changes": (
                dict(self.refetched_changes) if self.refetched_changes is not None else None
            ),
        }


def build_finance_update_confirmation_card(
    *,
    record_id: int,
    before: dict[str, Any],
    changes: dict[str, Any],
    expected_updated_at: str,
    party_display: tuple[tuple[str, str], ...] = (),
) -> ConfirmationCard:
    """R7 收支更新轻确认卡片（7.3 六要素；与 build_update_confirmation_card
    （domains/orders.py）同构——**工具层 prepare 与执行器重建必须共用本
    构造函数**，保证执行时重建的 hash 与确认时保存的 payload_hash 同口径）。

    - 目标：日期/分类标识（取自写前快照）——不写收支记录 id（[2026-09-10
      展示口径] ERP 内部主键，用户看不到记不住）；
    - 写值：changes + expected_updated_at（本地乐观锁口径——快照变化即
      确认失效，重新确认后执行，R3 规则 6/7 语义迁移）；
    - party_display：[2026-09-10 P1-1] resolve_party_display 解析出的
      关联方名称展示项（changes 含 customer_id/supplier_id 时由调用方传入）
      ——替代被 CONFIRMATION_HIDDEN_FIELDS 隐藏的 id，避免「只改 customer_id
      的更新卡看不出任何实际变更」；
    - 要素 5 预计影响：ERP 更新该收支记录（通用 update，mcp-api.md §5：
      不触发收付款/订单等其他记录联动）；
    - 要素 6 限制：expected_updated_at 只用于本地一致性审计，通用 update
      载荷不携带该参数。

    hash_fields：[2026-09-10 修复] 取原始 changes + expected_updated_at，
    并**恢复身份绑定**追加 record_id——本批把 id 移出 target 后哈希不再
    覆盖记录身份（不同 record_id 可能碰撞）。record_id 只进哈希不进展示；
    party_display（关联方名称/占位）为派生展示字段，绝不入哈希——执行期
    重查名称失败退化为占位不再导致确认被误拒。
    """
    key_label = f"{str(before.get('tx_date') or '')} {str(before.get('category') or '')}".strip()
    # [2026-09-10 展示口径] 目标不写收支记录 id（ERP 内部主键），改以
    # 日期/分类定位；无标识时退为「收支记录」（record_id 仍参与执行与审计）。
    target = f"收支记录（{key_label}）" if key_label else "收支记录"
    hash_fields = tuple(changes.items()) + (
        ("expected_updated_at", expected_updated_at),
        ("id", record_id),
    )
    card = ConfirmationCard(
        operation_type=OP_UPDATE_TRANSACTION,
        target=target,
        fields_to_write=localize_confirmation_fields(
            tuple(changes.items())
            + tuple(party_display)
            + (("expected_updated_at", expected_updated_at),)
        ),
        hash_fields=hash_fields,
        auto_filled=(),
        expected_impact=(
            "在 ERP 更新该收支记录（通用 update，mcp-api.md §5：不触发"
            "收付款/订单等其他记录联动）；保存更新前快照与更新后记录。"
        ),
        limitations=(
            "只修改确认卡列出的差异字段，未确认字段保持不变；",
            "金额按数值比较（\"1650\" 与 \"1650.00\" 视为相同，不进差异）；",
            "快照变化或结果未知时需重新查询并重新确认（R7），"
            "不覆盖他人新写入的数据。",
        ),
        payload_hash="",
        # 只改金额等（无分类字段）的更新卡不挂分类提示；改动含 category 才挂
        source_note=_finance_source_note(changes.keys()),
    )
    return replace(card, payload_hash=card_payload_hash(card))


# ---------------------------------------------------------------------------
# [2026-09-12 一张卡多条明细] 批量确认卡（创建 / 更新）
# ---------------------------------------------------------------------------


def _batch_detail_rows(
    rows: Sequence[tuple[str, str, dict[str, Any], dict[str, Any]]],
) -> tuple[tuple[tuple[str, object], ...], tuple[tuple[str, object], ...]]:
    """批量卡明细行公共构造（收支创建/更新共用；纯函数）。

    入参每行 ``(label, action, raw_fields, display_fields)``：

    - raw（hash_fields）外层键用 ``f"{BATCH_HASH_PREFIX}{i}"``（位置序号），
      **不含业务标识**——展示键里的关联方名称/tx_date 执行期重查时可能变化，
      绝不污染哈希（见 build_update_confirmation_card 的 hash_target 教训）；
      内层 ``action``/``fields`` 为原始执行语义（未本地化）；
    - 展示（fields_to_write）外层键用 ``f"{i}. {label}"``，序号保证唯一
      （同内容两行不会因 dict() 折叠而丢行）；内层经
      ``localize_confirmation_fields`` 中文化并递归隐藏 customer_id/supplier_id。

    display_fields 与 raw_fields **可不同**：更新卡的关联方名称（party_display）
    只进展示、不进哈希（[2026-09-10 修复] 既有口径）。
    """
    hash_fields = tuple(
        (f"{BATCH_HASH_PREFIX}{i}", {DETAIL_ACTION_KEY: action, DETAIL_FIELDS_KEY: raw})
        for i, (_label, action, raw, _display) in enumerate(rows, start=1)
    )
    display_fields = tuple(
        (f"{i}. {label}", {DETAIL_ACTION_KEY: action, DETAIL_FIELDS_KEY: display})
        for i, (label, action, _raw, display) in enumerate(rows, start=1)
    )
    return hash_fields, localize_confirmation_fields(display_fields)


def build_finance_create_batch_confirmation_card(
    rows: Sequence[tuple[str, dict[str, Any], tuple[tuple[str, str], ...]]],
) -> ConfirmationCard:
    """R7 收支**批量创建**轻确认卡（一张卡 N 条明细；≥2 条时使用）。

    入参 rows：``[(业务标识, payload, party_display), ...]``（业务标识复用
    单条卡的 ``tx_type·category（tx_date）`` 串；party_display 为解析出的
    关联方名称展示项，可选）。

    - raw 进哈希：每条 payload 原始字段（含 customer_id/supplier_id）；
    - 展示：payload + party_display（关联方名称）；内层译为「字段和值」；
    - 单条 **不** 走本函数（由 prepare 委托单条构造函数，逐字节零回归）。

    hash_fields 外层键为位置序号（detail-{i}），业务标识/关联方名称不入哈希
    ——执行期不会重建创建卡，但保持与更新卡同一口径，防后人误用。
    """
    n = len(rows)
    entries = [
        (
            label,
            ACTION_CREATE,
            dict(payload),
            {**dict(payload), **dict(party_display)},
        )
        for label, payload, party_display in rows
    ]
    hash_fields, display_fields = _batch_detail_rows(entries)
    card = ConfirmationCard(
        operation_type=OP_CREATE_TRANSACTION,
        target=f"批量创建 {n} 条收支记录",
        fields_to_write=display_fields,
        hash_fields=hash_fields,
        auto_filled=(),
        expected_impact=(
            "在 ERP 逐条创建上述收支记录（transactions，通用 create，"
            "mcp-api.md §5：直接写入，不触发订单/收付款等其他记录联动）；"
            "金额按两位小数规范化存储；单条失败不中断其余条目。"
        ),
        limitations=(
            "分类必须命中 transaction_categories 且归属所选收支类型"
            "（写前复核 fail closed，R7）；",
            "source_type/source_id/created_at 等系统字段由 ERP 联动维护，"
            "禁止传入；",
            "通用 create 无幂等键：逐条写入前按业务签名（类型/分类/日期/"
            "金额等）预查，命中即跳过（零写调用），防恢复期重复写入；",
            "最终校验以 ERP 创建响应为准（record id）。",
        ),
        payload_hash="",
        row_header_label="明细",
        row_quantifier="条",
        # 创建卡每条都含分类字段 → 挂；判据扫任一行字段键（嵌套行结构）
        source_note=_finance_source_note(
            k for _label, payload, _pd in rows for k in payload
        ),
    )
    return replace(card, payload_hash=card_payload_hash(card))


def build_finance_update_batch_confirmation_card(
    entries: Sequence[Mapping[str, Any]],
) -> ConfirmationCard:
    """R7 收支**批量更新**轻确认卡（一张卡 N 条明细；≥2 条时使用）。

    入参 entries 每项：``{record_id, before, changes, expected_updated_at,
    party_display}``。**prepare 与执行器整卡重建必须共用本函数**——执行期
    逐条重查快照重建整张卡，与 task.confirmed_payload_hash 比对（7.3 整卡
    一致性；任一条重查失败/内容变化 → 整卡哈希必变 → 整批不写）。

    - action：changes 非空 → ``updated``，为空 → ``unchanged``（执行期零写）；
    - raw 进哈希：changes + expected_updated_at + **record_id**（恢复身份绑定，
      同单条卡口径；record_id 只进哈希不进展示）；
    - 展示：changes + party_display（关联方名称）+ expected_updated_at；
      **party_display 绝不入哈希**（[2026-09-10 P1-1]：执行期重查名称失败
      退化为占位不再导致确认被误拒）。
    """
    n = len(entries)
    rows: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    for e in entries:
        changes = dict(e.get("changes") or {})
        expected_updated_at = str(e.get("expected_updated_at") or "")
        record_id = e.get("record_id")
        before = e.get("before") or {}
        action = ACTION_UPDATE if changes else ACTION_UNCHANGED
        key_label = (
            f"{str(before.get('tx_date') or '')} "
            f"{str(before.get('category') or '')}"
        ).strip()
        label = key_label or "收支记录"
        raw = {
            **changes,
            "expected_updated_at": expected_updated_at,
            "id": record_id,
        }
        display = {
            **changes,
            **dict(e.get("party_display") or ()),
            "expected_updated_at": expected_updated_at,
        }
        rows.append((label, action, raw, display))
    hash_fields, display_fields = _batch_detail_rows(rows)
    card = ConfirmationCard(
        operation_type=OP_UPDATE_TRANSACTION,
        target=f"批量更新 {n} 条收支记录",
        fields_to_write=display_fields,
        hash_fields=hash_fields,
        auto_filled=(),
        expected_impact=(
            "在 ERP 逐条更新上述收支记录（通用 update，mcp-api.md §5：不触发"
            "收付款/订单等其他记录联动）；保持（unchanged）条目零写调用。"
        ),
        limitations=(
            "只修改确认卡列出的差异字段，未确认字段保持不变；",
            "快照变化时整卡确认失效、整批不写（R7 规则 6/7），需重新查询并"
            "重新确认，不覆盖他人新写入的数据；",
            "expected_updated_at 仅用于本地一致性审计，通用 update 载荷不携带。",
        ),
        payload_hash="",
        row_header_label="明细",
        row_quantifier="条",
        # 任一条目的 changes 含 category → 挂；只改金额等则整卡不挂
        source_note=_finance_source_note(
            k for e in entries for k in (e.get("changes") or {})
        ),
    )
    return replace(card, payload_hash=card_payload_hash(card))


# ---------------------------------------------------------------------------
# 共享查询辅助
# ---------------------------------------------------------------------------


def _query_items(
    client: MCPClient,
    *,
    entity: str,
    fields: tuple[str, ...] | list[str],
    filter: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """通用只读查询辅助（失败抛 MCPError，调用方决定 fail 分支）。"""
    result = client.call_tool(
        "query",
        {"entity": entity, "fields": list(fields), "filter": filter, "limit": limit},
    )
    if not result.ok:
        raise result.error
    return list(result.data.get("items") or [])


def _query_category_names(client: MCPClient, tx_type: str) -> list[str]:
    """transaction_categories 可用分类名（按 tx_type 归属；查询失败抛
    MCPError → 调用方按分类复查失败处理，fail closed）。"""
    items = _query_items(
        client,
        entity="transaction_categories",
        fields=(CATEGORY_FIELD,),
        filter={"tx_type": tx_type},
        limit=_QUERY_LIMIT,
    )
    names = {
        str(r.get(CATEGORY_FIELD))
        for r in items
        if r.get(CATEGORY_FIELD) is not None
    }
    return sorted(names)


#: 关联方（FK 字段, 实体, 名称字段, 展示键, 中文类名）
_PARTY_FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
    ("customer_id", "customers", "company_name", "customer_name", "客户"),
    ("supplier_id", "suppliers", "name", "supplier_name", "供货商"),
)


def resolve_party_display(
    client: MCPClient,
    record: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    """把 customer_id/supplier_id 解析为展示字段（[2026-09-10 P1-1]）。

    返回有序 (展示键, 值) 对（如 ``(("customer_name", "昌盛"),)``）：record
    里带 customer_id/supplier_id 时，按 id 查对应实体取名称字段；查不到名称
    （无行 / 名称为空）或查询异常时用「客户ID 16」类占位（与 prompts/base.py
    REPLY_FORMAT 占位口径一致）——**绝不因解析失败抛异常、绝不静默删除关联
    方**（旧行为：CONFIRMATION_HIDDEN_FIELDS 直接删掉整行，用户看不到实际
    变更）。

    仅展示层使用：执行载荷与 payload_hash 的 customer_id/supplier_id 原样
    保留（服务端执行载荷，不进本函数、不受影响）。
    """
    out: list[tuple[str, str]] = []
    for fk, entity, name_field, display_key, kind in _PARTY_FIELDS:
        raw_id = record.get(fk)
        if raw_id is None:
            continue
        name: str | None = None
        try:
            rows = _query_items(
                client,
                entity=entity,
                fields=("id", name_field),
                filter={"id": raw_id},
                limit=1,
            )
            if rows:
                value = rows[0].get(name_field)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
        except MCPError:
            name = None  # 查询失败 → 占位，不阻断建卡、不抛异常
        out.append((display_key, name if name is not None else f"{kind}ID {raw_id}"))
    return tuple(out)


# ---------------------------------------------------------------------------
# 执行器：finance_create
# ---------------------------------------------------------------------------


class FinanceCreateExecutor:
    """R7 收支创建执行（通用 create + 写后验证；无幂等键不盲目重试）。

    与创建型幂等执行器（MetalOrderExecutor）的本质区别（mcp-api.md §5）：
    通用 create 无 idempotency_key——网络结果未知时重试可能重复落库，
    故绝不盲目重试；改由写后验证签名（写入的业务字段数值归一化比较）
    判定是否已应用（已应用 → 不二次写、任务成功）。

    流程：
    1. _require_running（8.1/8.3；13.3 同 task 不并发由锁保证，状态兜底）；
    2. verify_generic_write 载荷本地拦截（系统字段/source_type/source_id
       恒拒，fail closed；transactions.amount 实体感知放行）
       + 金额域层 Decimal 校验；
    3. 分类写前复查（确认后分类表变化防御：未命中 → fail closed 不写）；
    4. record_attempt（target_type="transaction"，field_diffs=payload；
       通用 create 无幂等键列）→ 调用 create（operation_id 链路关联）；
    5. 成功 → record_result(success) + mark_succeeded（8.1 唯一成功路径）；
    6. NETWORK/SERVER 结果未知 → 不盲目重试，写后验证：签名命中视为
       已应用（该次调用 result 记 failed + task mark_succeeded，不二次
       写）；未命中/验证查询失败 → failed（requires_operator_action=True，
       结果未知需人工核查，13.1）；
    7. 其他错误（VALIDATION 等）→ record_result(failed) + fail_task
       （retryable=false 不重试）。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: PolicyInfo | None = None,
    ) -> None:
        self._client = client
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy

    # ---- 入口 ----

    def execute(
        self, *, task_id: str, payload: dict[str, Any]
    ) -> FinanceCreateSummary:
        """确认后执行收支创建（payload = 确认时的写值载荷，工具层构造）。"""
        self._require_running(task_id)

        # 1. 载荷本地拦截（scopes 兜底：系统字段/source_type/source_id 恒拒）
        try:
            verify_generic_write("create", "transactions", payload)
        except MCPError as exc:
            summary = f"收支载荷含禁止字段或实体不允许：{exc}（fail closed，未写入）"
            self._fail_task(task_id, ERROR_PAYLOAD_FORBIDDEN, summary)
            return self._summary_create(
                task_id, "failed", summary, error_code=ERROR_PAYLOAD_FORBIDDEN
            )

        # 2. 金额域层校验（工具层已规范化，此处 fail closed 兜底）
        try:
            amount = normalize_amount(payload.get("amount"))
        except ValueError as exc:
            summary = f"收支金额校验失败：{exc}（fail closed，未写入）"
            self._fail_task(task_id, ERROR_AMOUNT_INVALID, summary)
            return self._summary_create(
                task_id, "failed", summary, error_code=ERROR_AMOUNT_INVALID
            )
        payload = dict(payload)
        payload["amount"] = amount

        # 3. 分类写前复查（分类表在确认后可能变化——防御性复核）
        category_message = self._category_check(
            str(payload.get("tx_type") or ""), str(payload.get("category") or "")
        )
        if category_message is not None:
            summary = (
                f"收支分类复查未通过：{category_message}；确认后分类表已变化，"
                "拒绝写入（fail closed）"
            )
            self._fail_task(task_id, ERROR_CATEGORY_INVALID, summary)
            return self._summary_create(
                task_id, "failed", summary, error_code=ERROR_CATEGORY_INVALID
            )

        # 4. attempt（通用 create 无幂等键：不带 idempotency_key）→ 调用 create
        task = self._tasks.get_task(task_id)
        op_id = task.operation_id if task else ""
        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type="transaction",
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            field_diffs=payload,
            **_policy_fields(self._policy),
        )
        result = self._client.call_tool(
            "create",
            {"entity": "transactions", "data": payload},
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(result, task_id, op_id)
        if result.ok:
            return self._handle_success(
                task_id, op_id, attempt, result.data, payload
            )

        err = result.error
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            # 结果未知：不盲目重试（无幂等键，重试可能重复落库）
            return self._handle_unknown(task_id, op_id, attempt, err, payload)

        # 其他错误（VALIDATION 等）：retryable=false，不重试
        self._record_result(attempt, err)
        summary = f"create transaction 失败（{err.code}）：{err.message}"
        self._fail_task(task_id, ERROR_MCP_FAILURE, summary)
        return self._summary_create(
            task_id, "failed", summary, error_code=ERROR_MCP_FAILURE
        )

    # ---- 批量创建（一张卡多条明细；≥2 条） ----

    def execute_batch(
        self, *, task_id: str, items: Sequence[Mapping[str, Any]]
    ) -> BatchSummary:
        """确认后批量执行收支创建（≥2 条；单条走 execute 旧路径）。

        items 每项：``{index, item_id, label, action, payload, tx_type,
        category, tx_date}``（工具层 prepare 序列化）。逐条经 BatchExecutor
        编排：已有成功 attempt → skipped（零写调用）；达重试上限 → failed；
        否则跑 runner（内含**写前签名预查**：命中已存在记录 → skipped，零写）。
        任务终态由 BatchExecutor 按确定性策略聚合（全成功 → succeeded；
        部分失败未达上限 → awaiting_recovery 保持 running）。
        """
        batch_items = [
            BatchItem(
                index=int(it["index"]),
                item_id=str(it["item_id"]),
                action=str(it.get("action") or ACTION_CREATE),
                label=str(it.get("label") or ""),
                payload=dict(it.get("payload") or {}),
            )
            for it in items
        ]
        executor = BatchExecutor(
            tasks=self._tasks, audit=self._audit, tool_key="finance_create"
        )

        # 同签名重复明细的「出现次序」：本批第 k 条同签名明细，写前预查只有
        # 在 ERP 已存在 ≥ k 条同签名记录时才认定「该条已写入」（见
        # _verify_applied 的 required_matches）。首次执行 → 前 k-1 条已写 →
        # 计数不足 k → 照写（N 条同签名必落 N 条）；恢复期结果未知的条目其
        # 记录已计入计数 → 认出而不重复写。
        ordinals = _signature_ordinals(batch_items)

        def runner(item: BatchItem, op_id: str) -> BatchItemResult:
            return self._run_batch_item(
                task_id, item, op_id, ordinal=ordinals[item.index]
            )

        return executor.run(task_id=task_id, items=batch_items, runner=runner)

    def _run_batch_item(
        self, task_id: str, item: BatchItem, op_id: str, *, ordinal: int = 1
    ) -> BatchItemResult:
        """单条创建（无任务级 mark_*，终态由 BatchExecutor 聚合）。

        **写前签名预查为无条件前置**（本阶段最重要的正确性措施）：通用
        create 无幂等键，恢复期若用户改了明细内容则 item_id 变化不被识别为
        已成功——每条写调用前先按业务签名查一次 ERP，命中即 skipped（零写
        调用）；预查查询失败 → fail closed（无法排除已存在记录，拒绝写入）。

        ``ordinal`` 为该条**业务签名在本批内的出现次序**（1 起）：预查要求
        ERP 已存在 ≥ ordinal 条同签名记录才 skip——同批次同签名重复明细
        因前 k-1 条已写入而计数不足 k，故照写（N 条必落 N 条）；恢复期结果
        未知的条目其记录已计入计数，故被认出而不重复写。
        """
        payload = dict(item.payload)

        # 1. 写前签名预查（命中即跳过；查询失败 fail closed 不写）
        applied, match_id, verify_error = self._verify_applied(
            payload, required_matches=ordinal
        )
        if verify_error is not None:
            return BatchItemResult(
                index=item.index,
                item_id=item.item_id,
                label=item.label,
                final_action="failed",
                status="failed",
                error_code=ERROR_NETWORK_UNKNOWN,
                error_summary=(
                    f"写前签名预查查询失败（{verify_error}）：无法排除已存在"
                    "记录，拒绝写入（fail closed，零写调用）"
                ),
            )
        if applied:
            return BatchItemResult(
                index=item.index,
                item_id=item.item_id,
                label=item.label,
                final_action="skipped",
                status="success",
                note=(
                    f"写前签名预查命中已存在记录（id={match_id}），跳过"
                    "（零写调用，防重复写入）"
                ),
            )

        # 2. 载荷本地拦截（scopes 兜底：系统字段/source_type/source_id 恒拒）
        try:
            verify_generic_write("create", "transactions", payload)
        except MCPError as exc:
            return self._batch_failed(
                item, ERROR_PAYLOAD_FORBIDDEN,
                f"收支载荷含禁止字段或实体不允许：{exc}（fail closed，未写入）",
            )

        # 3. 金额域层校验（工具层已规范化，此处 fail closed 兜底）
        try:
            payload["amount"] = normalize_amount(payload.get("amount"))
        except ValueError as exc:
            return self._batch_failed(
                item, ERROR_AMOUNT_INVALID,
                f"收支金额校验失败：{exc}（fail closed，未写入）",
            )

        # 4. 分类写前复查（分类表在确认后可能变化——防御性复核）
        category_message = self._category_check(
            str(payload.get("tx_type") or ""), str(payload.get("category") or "")
        )
        if category_message is not None:
            return self._batch_failed(
                item, ERROR_CATEGORY_INVALID,
                f"收支分类复查未通过：{category_message}；确认后分类表已变化，"
                "拒绝写入（fail closed）",
            )

        # 5. attempt → create（通用 create 无幂等键：不带 idempotency_key）
        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type="transaction",
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            field_diffs=payload,
            **_policy_fields(self._policy),
        )
        result = self._client.call_tool(
            "create",
            {"entity": "transactions", "data": payload},
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(result, task_id, op_id)
        if result.ok:
            return self._batch_success(
                task_id, item, attempt, result.data, payload, ordinal=ordinal
            )
        err = result.error
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            return self._batch_unknown(
                task_id, item, attempt, err, payload, ordinal=ordinal
            )
        self._record_result(attempt, err)
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=ERROR_MCP_FAILURE,
            error_summary=f"create transaction 失败（{err.code}）：{err.message}",
            attempt_id=attempt.attempt_id,
        )

    def _batch_success(
        self,
        task_id: str,
        item: BatchItem,
        attempt: TaskAttemptRecord,
        data: dict[str, Any],
        payload: dict[str, Any],
        *,
        ordinal: int = 1,
    ) -> BatchItemResult:
        """单条 create 响应 ok：record.id 回填；无 id 时写后验证兜底。"""
        record_id = _extract_record_id(data)
        if record_id is None:
            applied, match_id, verify_error = self._verify_applied(
                payload, required_matches=ordinal
            )
            if not applied:
                reason = (
                    "写后验证未命中" if verify_error is None
                    else f"写后验证查询失败（{verify_error}）"
                )
                self._record_result(
                    attempt,
                    MCPError(
                        code=ERROR_RESPONSE_UNVERIFIED,
                        category=ErrorCategory.SERVER,
                        retryable=False,
                        requires_operator_action=True,
                        message=f"create transaction 响应缺 record id 且{reason}",
                    ),
                    operator=True,
                )
                return BatchItemResult(
                    index=item.index, item_id=item.item_id, label=item.label,
                    final_action="failed", status="failed",
                    error_code=ERROR_RESPONSE_UNVERIFIED,
                    error_summary=f"响应 ok 但缺 record id 且{reason}，需人工核查",
                    attempt_id=attempt.attempt_id,
                )
            record_id = match_id
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            result_summary=f"created transaction record {record_id}",
            **_policy_fields(self._policy),
        )
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action=ACTION_CREATE, status="success",
            attempt_id=attempt.attempt_id,
        )

    def _batch_unknown(
        self,
        task_id: str,
        item: BatchItem,
        attempt: TaskAttemptRecord,
        err: MCPError,
        payload: dict[str, Any],
        *,
        ordinal: int = 1,
    ) -> BatchItemResult:
        """单条结果未知：写后验证签名（命中视为已应用，不二次写）。"""
        applied, match_id, verify_error = self._verify_applied(
            payload, required_matches=ordinal
        )
        if applied:
            self._record_result(
                attempt, err,
                result_summary=(
                    f"create transaction 结果未知（{err.code}），写后验证确认"
                    f"已应用（record id={match_id}）：不重复写入"
                ),
                operator=False,
            )
            return BatchItemResult(
                index=item.index, item_id=item.item_id, label=item.label,
                final_action=ACTION_CREATE, status="success",
                attempt_id=attempt.attempt_id,
                note=f"结果未知（{err.code}），写后验证确认已应用，未重复写入",
            )
        self._record_result(attempt, err, operator=True)
        reason = (
            "写后验证未命中" if verify_error is None
            else f"写后验证查询失败（{verify_error}）"
        )
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=ERROR_NETWORK_UNKNOWN,
            error_summary=(
                f"create transaction 结果未知（{err.code}）且{reason}；不盲目"
                "重试（无幂等键，重试可能重复落库），需人工核查（13.1）"
            ),
            attempt_id=attempt.attempt_id,
        )

    @staticmethod
    def _batch_failed(
        item: BatchItem, error_code: str, summary: str
    ) -> BatchItemResult:
        """批量单条失败（零写调用；终态由 BatchExecutor 聚合）。"""
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=error_code, error_summary=summary,
        )

    # ---- 成功分支 ----

    def _handle_success(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        data: dict[str, Any],
        payload: dict[str, Any],
    ) -> FinanceCreateSummary:
        """create 成功（响应 ok）：record.id 回填；mark_succeeded。

        响应未回传 record.id（无法以响应证明已落库）→ 不直接报 success，
        降级 _handle_success_unverified 走写后验证兜底（有证据才
        verification_passed=True）。
        """
        record_id = _extract_record_id(data)
        if record_id is None:
            return self._handle_success_unverified(
                task_id, op_id, attempt, data, payload
            )
        record = data.get("record")
        record_response = dict(record) if isinstance(record, dict) else None
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            result_summary=(
                f"created transaction record {record_id}" if record_id
                else "created transaction record"
            ),
            structured_payload={"record": record_response} if record_response else None,
            **_policy_fields(self._policy),
        )
        self._tasks.mark_succeeded(task_id, verification_passed=True)
        logger.info(
            "transaction_created",
            task_id=task_id,
            record_id=record_id,
            trace_id=self._trace_id,
        )
        return self._summary_create(
            task_id, "success", None,
            record_id=record_id, record_response=record_response,
        )

    def _handle_success_unverified(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        data: dict[str, Any],
        payload: dict[str, Any],
    ) -> FinanceCreateSummary:
        """响应 ok 但缺 record.id：契约异常（mcp-api.md §5 成功响应应含
        id）——不假报成功，降级 _verify_applied 写后验证兜底：

        - 签名命中（证据）→ 该次调用 result 记 success（11.2：实际调用
          一条 result）+ mark_succeeded(verification_passed=True)，
          record_id 回填命中行、applied_via_post_verify=True；
        - 未命中 / 验证查询失败 → result 记 failed
          （requires_operator_action=True）+ 任务 failed（响应异常需人工
          核查；不盲目重试——无幂等键，重试可能重复落库）。
        """
        record = data.get("record")
        record_response = dict(record) if isinstance(record, dict) else None
        applied, match_id, verify_error = self._verify_applied(payload)
        if applied:
            self._audit.record_result(
                attempt_id=attempt.attempt_id,
                result_status="success",
                trace_id=self._trace_id,
                actor_account=self._actor_account,
                actor_role=self._actor_role,
                duty_code=DUTY_CODE,
                result_summary=(
                    f"created transaction（响应缺 record id，写后验证确认"
                    f"已应用：record id={match_id}）"
                ),
                structured_payload=(
                    {"record": record_response} if record_response else None
                ),
                **_policy_fields(self._policy),
            )
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            logger.info(
                "transaction_created_post_verify",
                task_id=task_id,
                record_id=match_id,
                trace_id=self._trace_id,
            )
            return self._summary_create(
                task_id, "success", None,
                record_id=match_id, record_response=record_response,
                applied_via_post_verify=True,
            )
        reason = (
            "且写后验证未命中（类型/分类/日期/金额签名无匹配记录）"
            if verify_error is None
            else f"且写后验证查询失败（{verify_error}）"
        )
        synthetic_err = MCPError(
            code=ERROR_RESPONSE_UNVERIFIED,
            category=ErrorCategory.SERVER,
            retryable=False,
            requires_operator_action=True,
            details={"response_missing_record_id": True},
            message=(
                f"create transaction 响应 ok 但未回传 record id（{reason}），"
                "无法证明已落库，需人工核查"
            ),
        )
        self._record_result(attempt, synthetic_err, operator=True)
        summary = (
            f"create transaction 响应异常（缺 record id）{reason}；不盲目"
            "重试（无幂等键，重试可能重复落库），需人工核查（13.1）"
        )
        self._fail_task(task_id, ERROR_RESPONSE_UNVERIFIED, summary)
        return self._summary_create(
            task_id, "failed", summary, error_code=ERROR_RESPONSE_UNVERIFIED
        )

    # ---- 结果未知：写后验证（不盲目重试） ----

    def _handle_unknown(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        err: MCPError,
        payload: dict[str, Any],
    ) -> FinanceCreateSummary:
        """NETWORK/SERVER 结果未知 → 写后验证业务签名。

        - 签名命中（已应用）→ 该次调用 result 记 failed（11.2：结果未知
          的一次实际调用一条 result）+ task mark_succeeded（写后验证
          确认已应用，不二次写、不假报未应用）；
        - 未命中 / 验证查询失败 → result failed
          （requires_operator_action=True）+ task failed
          （结果未知需人工核查，13.1）。
        """
        applied, match_id, verify_error = self._verify_applied(payload)
        if applied:
            self._record_result(
                attempt, err,
                result_summary=(
                    f"create transaction 结果未知（{err.code}），写后验证确认"
                    f"已应用（record id={match_id}）：不重复写入"
                ),
                operator=False,
            )
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            summary = (
                f"create transaction 结果未知（{err.code}），写后验证已命中"
                f"（record id={match_id}）：视为已应用，未重复写入"
            )
            logger.info(
                "transaction_created_post_verify",
                task_id=task_id,
                record_id=match_id,
                trace_id=self._trace_id,
            )
            return self._summary_create(
                task_id, "success", summary,
                record_id=match_id,
                applied_via_post_verify=True,
            )
        reason = (
            "且写后验证未命中（类型/分类/金额/日期签名无匹配记录）"
            if verify_error is None
            else f"且写后验证查询失败（{verify_error}）"
        )
        self._record_result(attempt, err, operator=True)
        summary = (
            f"create transaction 结果未知（{err.code}）{reason}；不盲目重试"
            "（无幂等键，重试可能重复落库），需人工核查（13.1）"
        )
        self._fail_task(task_id, ERROR_NETWORK_UNKNOWN, summary)
        return self._summary_create(
            task_id, "failed", summary, error_code=ERROR_NETWORK_UNKNOWN
        )

    def _verify_applied(
        self, payload: dict[str, Any], *, required_matches: int = 1
    ) -> tuple[bool, int | None, str | None]:
        """写后验证：按业务签名（类型/分类/日期 + 其余写入字段）查询。

        返回 (命中, 命中记录 id, 查询失败错误码)；命中 = 查询结果中与载荷
        全部写入字段一致（金额按数值归一化）的记录数 **≥ required_matches**。

        ``required_matches`` 用于**同批次同签名重复明细**的次序对齐：本批第
        k 条同签名明细要求 ≥ k 条同签名记录才认定「已写入」——既保证首次
        执行时 N 条同签名明细各写一条（前 k-1 条刚写入 → 计数 = k-1 < k →
        照写），又保证恢复期结果未知的条目其记录已计入计数而被认出（≥ k）。
        单条路径（execute / 旧执行器）恒为默认 1，行为逐字节不变。
        """
        try:
            items = _query_items(
                self._client,
                entity="transactions",
                fields=_TRANSACTION_QUERY_FIELDS,
                filter={
                    "tx_type": payload.get("tx_type"),
                    "tx_date": payload.get("tx_date"),
                    "category": payload.get("category"),
                },
                limit=_QUERY_LIMIT,
            )
        except MCPError as exc:
            return False, None, exc.code
        matches = 0
        match_id: int | None = None
        for row in items:
            if self._signature_matches(payload, row):
                matches += 1
                if match_id is None:
                    rid = row.get("id")
                    if isinstance(rid, int):
                        match_id = rid
        if matches >= required_matches:
            return True, match_id, None
        return False, None, None

    @staticmethod
    def _signature_matches(payload: dict[str, Any], row: dict[str, Any]) -> bool:
        """签名比对：载荷每个写入字段都必须与候选记录一致（金额数值化）。"""
        for key, want in payload.items():
            have = row.get(key)
            if key == "amount":
                if not _amount_equals(want, have):
                    return False
            elif have is None or str(want) != str(have):
                return False
        return True

    # ---- 分类复查 / 审计 ----

    def _category_check(self, tx_type: str, category: str) -> str | None:
        """分类归属复查（未命中/查询失败 → 返回中文消息；成功 None）。

        查询调用失败按分类复查失败处理（fail closed——分类表不可查时
        不允许写入）。
        """
        try:
            names = _query_category_names(self._client, tx_type)
        except MCPError as exc:
            return f"分类表查询失败（{exc.code}）：{exc.message}"
        if category not in names:
            return (
                f"收支分类 {category!r} 不属于 {tx_type}"
                f"（可用分类：{'、'.join(names)}）"
            )
        return None

    def _record_result(
        self,
        attempt: TaskAttemptRecord,
        err: MCPError,
        *,
        result_summary: str | None = None,
        operator: bool | None = None,
    ) -> None:
        """调用返回后立即 result（11.2：结果未知的实际调用恒记 failed；
        写后验证的解析结果不追加第二条 result）。"""
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="failed",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            error_code=err.code,
            requires_operator_action=(
                operator if operator is not None
                else bool(err.requires_operator_action)
            ),
            result_summary=result_summary or err.message or "failed",
            **_policy_fields(self._policy),
        )

    def _record_mcp_retries(
        self,
        result: ToolResult,
        task_id: str,
        op_id: str,
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
            target_type="transaction",
            duty_code=DUTY_CODE,
            **_policy_fields(self._policy),
        )

    # ---- 汇总与内部 ----

    def _summary_create(
        self,
        task_id: str,
        result_status: str,
        error_summary: str | None,
        *,
        record_id: int | None = None,
        record_response: dict[str, Any] | None = None,
        applied_via_post_verify: bool = False,
        error_code: str | None = None,
    ) -> FinanceCreateSummary:
        return FinanceCreateSummary(
            task_id=task_id,
            operation_type=OP_CREATE_TRANSACTION,
            result_status=result_status,
            record_id=record_id,
            record_response=record_response,
            attempts=1,
            verification_passed=result_status == "success",
            applied_via_post_verify=applied_via_post_verify,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _require_running(self, task_id: str) -> None:
        """执行前校验：任务必须 running（8.1 L930；8.3 等待/终态不执行；
        13.3 同 task 不并发由锁保证，状态兜底）。"""
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
            # 任务已终态（如确认流程异常取消）时忽略；审计失败路径由
            # 调用方另行处理（与 attendance/orders 执行器同口径）
            logger.exception("finance_fail_task_error", task_id=task_id)


# ---------------------------------------------------------------------------
# 执行器：finance_update
# ---------------------------------------------------------------------------


class FinanceUpdateExecutor:
    """R7 收支更新执行（写前重查 + 本地乐观锁；无服务端 expected_updated_at）。

    与订单更新（OrderUpdateExecutor，R3）同构，差异（mcp-api.md §5）：
    通用 update 无 expected_updated_at 服务端校验——本地乐观锁由
    「写前重查 → 重建确认卡 hash 比对」承担（快照变化即确认失效，
    is_confirmation_valid，7.3/R3 规则 6），expected_updated_at 仅随
    审计记录（字段差异审计列），不随 update 载荷发给 ERP。

    流程：
    1. _require_running；2. 写前重查唯一目标记录（空 → NOT_FOUND）；
    3. changes = build_update_changes（数值归一化；空 → NO_CHANGES）；
    4. 金额域层校验（amount 在变化集内时）+ verify_generic_write 载荷
       拦截（双重保护）；5. 分类一致性复查（desired 变化涉及
       tx_type/category 时，同工具层 prepare 口径，fail closed）；
    6. 重建确认卡 hash 与 confirmed_payload_hash 比对（为空/不一致 →
       确认失效须重新确认，7.3，不覆盖新数据）；
    7. attempt（field_diffs=changes，expected_updated_at 仅审计列）；
    8. 调用 update → 成功 → record_result(success) + mark_succeeded +
       after_snapshot + changed_fields；
    9. 结果未知（NETWORK/SERVER）→ 先重查重算差异（R3 规则 7 语义）：
       期望变更已应用 → 展示最新快照（mark_succeeded，不重复调用）；
       内容变化 → failed（ERROR_NETWORK_UNKNOWN）需重新确认；
       重查失败 → failed 无法确认（需人工核查）。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: PolicyInfo | None = None,
    ) -> None:
        self._client = client
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy

    # ---- 入口 ----

    def execute(
        self,
        *,
        task_id: str,
        record_id: int,
        desired: dict[str, Any],
    ) -> FinanceUpdateSummary:
        """确认后执行收支更新（desired = 目标期望字段值，工具层过滤后）。"""
        self._require_running(task_id)

        # 2. 写前重查唯一目标记录（不基于记忆或猜测写入，R7 规则 1）
        try:
            current = self._query_current(record_id)
        except MCPError as exc:
            summary = (
                f"写前查询失败（{exc.code}）：{exc.message}；"
                "未执行更新，需核查后重试（R7 规则 1）"
            )
            self._fail_task(task_id, ERROR_MCP_FAILURE, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes={}, expected_updated_at="",
                error_code=ERROR_MCP_FAILURE,
            )
        if current is None:
            summary = (
                f"未查询到目标收支记录（record_id={record_id}）："
                "不更新、不猜测（R7 规则 1）"
            )
            self._fail_task(task_id, ERROR_UPDATE_NOT_FOUND, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes={}, expected_updated_at="",
                error_code=ERROR_UPDATE_NOT_FOUND,
            )

        # 3. 只生成真正变化字段（数值归一化比较，R3 规则 2 同口径）
        changes = build_update_changes(current, desired, "transactions")
        if not changes:
            summary = (
                f"目标收支记录（id={record_id}）期望值与当前值一致，无需更新："
                "不调用 update（R7 规则 2）"
            )
            self._fail_task(task_id, ERROR_UPDATE_NO_CHANGES, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes={}, expected_updated_at=str(current.get("updated_at") or ""),
                before=current, error_code=ERROR_UPDATE_NO_CHANGES,
            )

        # 4. 金额域层校验 + 载荷本地拦截（scopes 兜底：系统字段恒拒）
        try:
            changes = dict(changes)
            if "amount" in changes:
                changes["amount"] = normalize_amount(changes["amount"])
            verify_generic_write("update", "transactions", changes)
        except ValueError as exc:
            summary = f"收支金额校验失败：{exc}（fail closed，未更新）"
            self._fail_task(task_id, ERROR_AMOUNT_INVALID, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes=changes,
                expected_updated_at=str(current.get("updated_at") or ""),
                before=current, error_code=ERROR_AMOUNT_INVALID,
            )
        except MCPError as exc:
            summary = f"收支载荷含禁止字段或实体不允许：{exc}（fail closed，未更新）"
            self._fail_task(task_id, ERROR_PAYLOAD_FORBIDDEN, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes=changes,
                expected_updated_at=str(current.get("updated_at") or ""),
                before=current, error_code=ERROR_PAYLOAD_FORBIDDEN,
            )

        # 5. 分类一致性复查（desired 涉及 tx_type/category 时，同 prepare 口径）
        category_message = self._category_consistency_check(desired, current)
        if category_message is not None:
            summary = (
                f"收支分类复查未通过：{category_message}；确认后分类表已变化，"
                "拒绝更新（fail closed）"
            )
            self._fail_task(task_id, ERROR_CATEGORY_INVALID, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes=changes,
                expected_updated_at=str(current.get("updated_at") or ""),
                before=current, error_code=ERROR_CATEGORY_INVALID,
            )

        # 6. 本地乐观锁：重建确认卡 hash 与确认时保存的比对（7.3/R3 规则 6）
        task = self._tasks.get_task(task_id)
        confirmed_hash = task.confirmed_payload_hash if task else None
        expected_updated_at = str(current.get("updated_at") or "")
        card = build_finance_update_confirmation_card(
            record_id=record_id, before=current, changes=changes,
            expected_updated_at=expected_updated_at,
            # [2026-09-10 P1-1] changes 含 customer_id/supplier_id 时解析
            # 名称展示（与 prepare 侧同函数同输入 → hash 同口径）
            party_display=resolve_party_display(self._client, changes),
        )
        if not confirmed_hash or not is_confirmation_valid(card, confirmed_hash):
            summary = (
                "执行时收支记录快照与确认时不一致（或未经过确认），确认已失效："
                "需重新查询并重新确认（7.3 / R7 规则 6），不执行更新"
            )
            self._fail_task(task_id, ERROR_UPDATE_CONFIRMATION_INVALID, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=current, error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
            )

        # 7. attempt（expected_updated_at 仅审计列；update 载荷不含该参数）
        op_id = task.operation_id
        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type="transaction",
            target_id=str(record_id),
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            expected_updated_at=expected_updated_at,
            field_diffs=changes,
            **_policy_fields(self._policy),
        )
        # 8. 调用 update（mcp-api.md §5：data 只含差异字段，无 expected_updated_at）
        result = self._client.call_tool(
            "update",
            {"entity": "transactions", "id": record_id, "data": changes},
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(result, task_id, op_id, target_id=str(record_id))
        if result.ok:
            return self._handle_success(
                task_id, op_id, attempt, result.data, record_id,
                changes, expected_updated_at, current,
            )

        err = result.error
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            # 9. 结果未知：先重查，不盲目重试（R3 规则 7 语义迁移）
            return self._handle_unknown(
                task_id, op_id, attempt, err, record_id, desired,
                changes, expected_updated_at, current,
            )
        self._record_result(attempt, err)
        summary = f"update transaction 失败（{err.code}）：{err.message}"
        self._fail_task(task_id, ERROR_MCP_FAILURE, summary)
        return self._summary_update(
            task_id, record_id, "failed", summary,
            changes=changes, expected_updated_at=expected_updated_at,
            before=current, error_code=ERROR_MCP_FAILURE,
        )

    # ---- 批量更新（一张卡多条明细；≥2 条） ----

    def execute_batch(
        self, *, task_id: str, items: Sequence[Mapping[str, Any]]
    ) -> BatchSummary:
        """确认后批量执行收支更新（≥2 条；单条走 execute 旧路径）。

        items 每项：``{index, item_id, label, action, id, desired}``（工具层
        prepare 序列化）。

        **整卡确认有效性**（7.3；本阶段风险最高的一处）：执行前逐条重查快照
        → 用与 prepare **同一个纯函数**（build_finance_update_batch_confirmation_
        card）重建整张批量卡 → 与 task.confirmed_payload_hash 比对。任一条
        重查失败/不存在/内容变化（含他人改动、分类/金额漂移）→ 整卡哈希必变
        → **整批一条都不写** + mark_failed(CONFIRMATION_INVALID)。**绝不改为
        逐条比对**——否则部分执行一张已失效的卡。校验通过后才交 BatchExecutor
        逐条执行（未达上限的部分失败 → awaiting_recovery 保持 running）。
        """
        self._require_running(task_id)
        task = self._tasks.get_task(task_id)
        confirmed_hash = task.confirmed_payload_hash if task else None

        rebuilt: list[dict[str, Any]] = []
        batch_items: list[BatchItem] = []
        for it in items:
            record_id = int(it["id"])
            desired = {
                k: v for k, v in dict(it.get("desired") or {}).items() if v is not None
            }
            try:
                current = self._query_current(record_id)
            except MCPError as exc:
                return self._fail_batch_invalid(
                    task_id, items,
                    f"整卡重放失败：写前查询记录 {record_id} 失败（{exc.code}）："
                    "整批不写（R7 规则 6/7）",
                )
            if current is None:
                return self._fail_batch_invalid(
                    task_id, items,
                    f"整卡重放失败：记录 {record_id} 不存在：整批不写"
                    "（R7 规则 6/7）",
                )
            changes = build_update_changes(current, desired, "transactions")
            expected_updated_at = str(current.get("updated_at") or "")
            rebuilt.append({
                "record_id": record_id,
                "before": current,
                "changes": changes,
                "expected_updated_at": expected_updated_at,
                "party_display": resolve_party_display(self._client, changes),
            })
            batch_items.append(BatchItem(
                index=int(it["index"]),
                item_id=str(it["item_id"]),
                action=str(it.get("action") or ACTION_UPDATE),
                label=str(it.get("label") or ""),
                payload={
                    "record_id": record_id,
                    "desired": desired,
                    "changes": changes,
                    "expected_updated_at": expected_updated_at,
                    "before": current,
                },
            ))

        card = build_finance_update_batch_confirmation_card(rebuilt)
        if not confirmed_hash or not is_confirmation_valid(card, confirmed_hash):
            return self._fail_batch_invalid(
                task_id, items,
                "执行时收支记录快照与确认时不一致（或未经过确认），确认已失效："
                "整批不写；需重新查询并重新确认（7.3 / R7 规则 6）",
            )

        executor = BatchExecutor(
            tasks=self._tasks, audit=self._audit, tool_key="finance_update"
        )

        def runner(item: BatchItem, op_id: str) -> BatchItemResult:
            return self._run_batch_update_item(task_id, item, op_id)

        return executor.run(task_id=task_id, items=batch_items, runner=runner)

    def _run_batch_update_item(
        self, task_id: str, item: BatchItem, op_id: str
    ) -> BatchItemResult:
        """单条更新（无任务级 mark_*；终态由 BatchExecutor 聚合）。

        本地乐观锁由**整卡比对**承担（execute_batch 入口），本条只做写调用；
        expected_updated_at 仍只进审计列（不随 update 载荷发给 ERP）。
        """
        p = item.payload
        record_id = int(p["record_id"])
        changes = dict(p["changes"])
        expected_updated_at = str(p.get("expected_updated_at") or "")
        desired = dict(p.get("desired") or {})
        before = dict(p.get("before") or {})

        # 载荷本地拦截 + 金额域层校验（scopes 兜底：系统字段恒拒）
        try:
            if "amount" in changes:
                changes["amount"] = normalize_amount(changes["amount"])
            verify_generic_write("update", "transactions", changes)
        except ValueError as exc:
            return self._batch_failed(
                item, ERROR_AMOUNT_INVALID,
                f"收支金额校验失败：{exc}（fail closed，未更新）",
            )
        except MCPError as exc:
            return self._batch_failed(
                item, ERROR_PAYLOAD_FORBIDDEN,
                f"收支载荷含禁止字段或实体不允许：{exc}（fail closed，未更新）",
            )

        # 分类一致性复查（desired 涉及 tx_type/category 时，同 prepare 口径）
        category_message = self._category_consistency_check(desired, before)
        if category_message is not None:
            return self._batch_failed(
                item, ERROR_CATEGORY_INVALID,
                f"收支分类复查未通过：{category_message}；确认后分类表已变化，"
                "拒绝更新（fail closed）",
            )

        attempt = self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type="transaction",
            target_id=str(record_id),
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            expected_updated_at=expected_updated_at,
            field_diffs=changes,
            **_policy_fields(self._policy),
        )
        result = self._client.call_tool(
            "update",
            {"entity": "transactions", "id": record_id, "data": changes},
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(result, task_id, op_id, target_id=str(record_id))
        if result.ok:
            self._audit.record_result(
                attempt_id=attempt.attempt_id,
                result_status="success",
                trace_id=self._trace_id,
                actor_account=self._actor_account,
                actor_role=self._actor_role,
                duty_code=DUTY_CODE,
                result_summary=f"updated transaction record {record_id}",
                structured_payload={
                    "record_id": record_id,
                    "expected_updated_at": expected_updated_at,
                    "changes": dict(changes),
                },
                **_policy_fields(self._policy),
            )
            return BatchItemResult(
                index=item.index, item_id=item.item_id, label=item.label,
                final_action=ACTION_UPDATE, status="success",
                attempt_id=attempt.attempt_id,
            )

        err = result.error
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            return self._batch_update_unknown(
                item, attempt, err, record_id, desired, changes,
            )
        self._record_result(attempt, err)
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=ERROR_MCP_FAILURE,
            error_summary=f"update transaction 失败（{err.code}）：{err.message}",
            attempt_id=attempt.attempt_id,
        )

    def _batch_update_unknown(
        self,
        item: BatchItem,
        attempt: TaskAttemptRecord,
        err: MCPError,
        record_id: int,
        desired: dict[str, Any],
        changes: dict[str, Any],
    ) -> BatchItemResult:
        """单条结果未知：先重查重算差异（R3 规则 7 语义，不盲目重试）。"""
        self._record_result(attempt, err)
        try:
            fresh = self._query_current(record_id)
        except MCPError as query_err:
            return BatchItemResult(
                index=item.index, item_id=item.item_id, label=item.label,
                final_action="failed", status="failed",
                error_code=ERROR_NETWORK_UNKNOWN,
                error_summary=(
                    f"结果未知（{err.code}）且重查失败（{query_err.code}）："
                    "无法确认是否已应用，需人工核查（R7 规则 6/7）"
                ),
                attempt_id=attempt.attempt_id,
            )
        fresh_changes = build_update_changes(fresh or {}, desired, "transactions")
        if not fresh_changes:
            return BatchItemResult(
                index=item.index, item_id=item.item_id, label=item.label,
                final_action=ACTION_UPDATE, status="success",
                attempt_id=attempt.attempt_id,
                note="结果未知，重查确认期望变更已应用，不重复调用",
            )
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=ERROR_NETWORK_UNKNOWN,
            error_summary=(
                f"结果未知（{err.code}）且重查确认未应用（当前差异 "
                f"{fresh_changes}）；需重新确认后执行，不盲目重试（R7 规则 6/7）"
            ),
            attempt_id=attempt.attempt_id,
        )

    def _fail_batch_invalid(
        self,
        task_id: str,
        items: Sequence[Mapping[str, Any]],
        message: str,
    ) -> BatchSummary:
        """整卡确认失效：整批一条不写 + mark_failed(CONFIRMATION_INVALID)。"""
        self._fail_task(task_id, ERROR_UPDATE_CONFIRMATION_INVALID, message)
        results = tuple(
            BatchItemResult(
                index=int(it["index"]),
                item_id=str(it["item_id"]),
                label=str(it.get("label") or ""),
                final_action="failed",
                status="failed",
                error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
                error_summary=message,
            )
            for it in items
        )
        return BatchSummary(
            task_id=task_id,
            counts={
                "created": 0, "updated": 0, "unchanged": 0,
                "skipped": 0, "failed": len(results),
            },
            item_results=results,
            verification_passed=False,
            error_code=ERROR_UPDATE_CONFIRMATION_INVALID,
            error_summary=message,
        )

    @staticmethod
    def _batch_failed(
        item: BatchItem, error_code: str, summary: str
    ) -> BatchItemResult:
        """批量单条失败（零写调用；终态由 BatchExecutor 聚合）。"""
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action="failed", status="failed",
            error_code=error_code, error_summary=summary,
        )

    # ---- 成功分支 ----

    def _handle_success(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        data: dict[str, Any],
        record_id: int,
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any],
    ) -> FinanceUpdateSummary:
        """成功：after_snapshot = ERP 响应记录（record）；changed_fields =
        差异字段（通用 update 响应无派生影响）。"""
        record = data.get("record")
        after = dict(record) if isinstance(record, dict) else None
        changed_fields = tuple(sorted(changes))
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            result_summary=f"updated transaction record {record_id}",
            structured_payload={
                "record_id": record_id,
                "expected_updated_at": expected_updated_at,
                "changes": dict(changes),
                "changed_fields": list(changed_fields),
                "before_snapshot": dict(before),
                "after_snapshot": after,
            },
            **_policy_fields(self._policy),
        )
        self._tasks.mark_succeeded(task_id, verification_passed=True)
        logger.info(
            "transaction_updated",
            task_id=task_id,
            record_id=record_id,
            changed_fields=list(changed_fields),
            trace_id=self._trace_id,
        )
        return self._summary_update(
            task_id, record_id, "success", None,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, after=after, changed_fields=changed_fields,
        )

    # ---- 结果未知：重查重算（R3 规则 6/7 语义迁移） ----

    def _handle_unknown(
        self,
        task_id: str,
        op_id: str,
        attempt: TaskAttemptRecord,
        err: MCPError,
        record_id: int,
        desired: dict[str, Any],
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any],
    ) -> FinanceUpdateSummary:
        """update 结果未知 → 重新查询最新记录并重算差异。

        11.2 规则 2：每次实际调用只追加一条 result——本分支单次记录
        result_status=failed（11.3 监控按 result 事件计数不重复）。

        - 差异为空（本更新已应用/他人已更新为期望值）→ 展示最新快照，
          不覆盖、不重复调用 update；
        - 内容变化 → failed（ERROR_NETWORK_UNKNOWN）需重新确认，
          不覆盖新数据；
        - 重查失败 → failed 无法确认（需人工核查）。
        """
        self._record_result(attempt, err)
        try:
            fresh = self._query_current(record_id)
        except MCPError as query_err:
            summary = (
                f"update transaction 结果未知（{err.code}）且重新查询失败"
                f"（{query_err.code}）：无法确认是否已应用；不盲目重试，"
                "需人工核查（R7 规则 6/7）"
            )
            self._fail_task(task_id, ERROR_NETWORK_UNKNOWN, summary)
            return self._summary_update(
                task_id, record_id, "failed", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, error_code=ERROR_NETWORK_UNKNOWN,
            )
        if fresh is None:
            fresh = {}
        fresh_changes = build_update_changes(fresh, desired, "transactions")
        if not fresh_changes:
            summary = (
                f"update transaction 结果未知（{err.code}），重新查询确认期望"
                "变更已应用：按最新快照展示，不重复调用（R7 规则 6/7）"
            )
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            return self._summary_update(
                task_id, record_id, "success", summary,
                changes=changes, expected_updated_at=expected_updated_at,
                before=before, after=dict(fresh),
                refetched_changes=fresh_changes,
            )
        summary = (
            f"update transaction 结果未知（{err.code}），重新查询确认未应用；"
            f"当前差异 {fresh_changes}；需重新确认后执行，不盲目重试（R7 规则 6/7）"
        )
        self._fail_task(task_id, ERROR_NETWORK_UNKNOWN, summary)
        return self._summary_update(
            task_id, record_id, "failed", summary,
            changes=changes, expected_updated_at=expected_updated_at,
            before=before, error_code=ERROR_NETWORK_UNKNOWN,
            refetched_changes=fresh_changes,
        )

    # ---- 写前查询 / 分类一致性 / 审计 ----

    def _query_current(self, record_id: int) -> dict[str, Any] | None:
        """写前重查唯一目标记录（返回记录 dict；无记录返回 None；
        查询失败抛 MCPError——调用方决定 fail 分支）。"""
        items = _query_items(
            self._client,
            entity="transactions",
            fields=_TRANSACTION_QUERY_FIELDS,
            filter={"id": record_id},
            limit=1,
        )
        return dict(items[0]) if items else None

    def _category_consistency_check(
        self, desired: dict[str, Any], current: dict[str, Any]
    ) -> str | None:
        """desired 涉及 tx_type/category 变化时的归属复查（工具层同口径）。

        - desired 含 category → 按生效 tx_type（desired 或当前记录）复查；
        - 仅改 tx_type → 当前记录的 category（若有）必须归属新类型，
          否则要求一并修正（防止写出生理上不可能的 类型×分类 组合）。

        按 desired（用户请求全集）宽查而非仅按 changes（实际差异集）触发：
        请求里出现过 category/tx_type 即复查，即使请求值与现值相同（不进
        changes）也复查——确认→执行间隙分类表可能漂移，这是确认后分类
        表漂移的兜底防御（一次只读查询换写错分类的代价，R7 确认卡防御
        口径）。
        """
        if "category" not in desired and "tx_type" not in desired:
            return None
        if "category" in desired:
            eff_tx_type = desired.get("tx_type") or current.get("tx_type") or ""
            return self._category_check(str(eff_tx_type), str(desired["category"]))
        new_tx_type = str(desired["tx_type"] or "")
        current_category = current.get("category")
        if current_category:  # 记录已有分类：必须仍归属新类型
            return self._category_check(new_tx_type, str(current_category))
        return None

    def _category_check(self, tx_type: str, category: str) -> str | None:
        """分类归属复查（与 FinanceCreateExecutor 同口径，消息格式一致）。"""
        try:
            names = _query_category_names(self._client, tx_type)
        except MCPError as exc:
            return f"分类表查询失败（{exc.code}）：{exc.message}"
        if category not in names:
            return (
                f"收支分类 {category!r} 不属于 {tx_type}"
                f"（可用分类：{'、'.join(names)}）"
            )
        return None

    def _record_result(
        self,
        attempt: TaskAttemptRecord,
        err: MCPError,
    ) -> None:
        """调用返回后立即 result（11.2 L1226：success/failed）。"""
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="failed",
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=DUTY_CODE,
            error_code=err.code,
            requires_operator_action=bool(err.requires_operator_action),
            result_summary=err.message or "failed",
            **_policy_fields(self._policy),
        )

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
            target_type="transaction",
            target_id=target_id,
            duty_code=DUTY_CODE,
            **_policy_fields(self._policy),
        )

    # ---- 汇总与内部 ----

    def _summary_update(
        self,
        task_id: str,
        record_id: int | None,
        result_status: str,
        error_summary: str | None,
        *,
        changes: dict[str, Any],
        expected_updated_at: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        changed_fields: tuple[str, ...] = (),
        error_code: str | None = None,
        refetched_changes: dict[str, Any] | None = None,
    ) -> FinanceUpdateSummary:
        return FinanceUpdateSummary(
            task_id=task_id,
            operation_type=OP_UPDATE_TRANSACTION,
            record_id=record_id,
            expected_updated_at=expected_updated_at,
            changes=dict(changes),
            before_snapshot=dict(before) if before is not None else None,
            after_snapshot=dict(after) if after is not None else None,
            changed_fields=changed_fields,
            result_status=result_status,
            attempts=1,
            verification_passed=result_status == "success",
            error_code=error_code,
            error_summary=error_summary,
            refetched_changes=(
                dict(refetched_changes) if refetched_changes is not None else None
            ),
        )

    def _require_running(self, task_id: str) -> None:
        """执行前校验：任务必须 running（8.1 L930；8.3 等待/终态不执行；
        13.3 同 task 不并发由锁保证，状态兜底）。"""
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
            # 任务已终态（如确认流程异常取消）时忽略；审计失败路径由
            # 调用方另行处理（与 attendance/orders 执行器同口径）
            logger.exception("finance_fail_task_error", task_id=task_id)
