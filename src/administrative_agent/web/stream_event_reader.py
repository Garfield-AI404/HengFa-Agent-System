"""SSE 事件流结构性元数据的**只读**查询层（可观测性第 1 步：任务时间线）。

第 0 步把事件流按 A 档粒度落进 ``chat_stream_events``；本模块是全仓**第一个
读**该表的代码，把「最近会话列表 → 某次会话的完整事件流水」读出来。**纯读取**：
零迁移、零 DDL、零写入。

两条不可协商的边界（违反即把模型原文透到开发者页）：

- ⛔ **绝不读 ``StreamStore``**（``web/chat_streams.py`` 的内存事件）——它的
  payload 含 ``reply_text`` / ``chunk`` / ``summary`` / ``card`` **原文**。
  本模块只走 DB；DB 里的 payload 是 ``web/stream_projection.py`` 投影后的结构量
  （``*_chars`` 长度、枚举、键名），**业务原文**（明细/金额/客户名/回复正文/
  思考正文/摘要）**不在表中**。

  ⚠️ **「原文一律不在表中」的例外已收口**（可观测性第 2 步，(c) `tool_name`
  写入侧收口）：``tool_name`` 的值源自模型的结构化输出 ``AgentDecision``
  （``model_node`` 决策轮落盘的 ``model_dump()``），**只有提示词句约束、无代码层
  枚举校验**。此前它在 ``PROJECTION`` 里是 ``allow`` **原样放行**（终审实测
  曾记为缺陷：``project('tool_running', {'tool_name':'<任意串>'})`` → 原样保留），
  模型幻觉出的任意串会落库并被本模块**原样渲染**。
  现已在**写入侧**收口：``stream_projection.py`` 的**五条**规则
  （``tool_running`` / ``tool_result`` / ``confirmation_requested`` /
  ``agent_error`` / ``re_requested``）
  把 ``tool_name`` 从 ``allow`` 改为 ``_enum_or_none`` 三态 derive（对
  ``TOOL_REGISTRY`` 键集判定）——已知名原样、其他非空串 → ``"other"``
  （幻觉串**不再落库**）、缺失/``None``/空串 → ``"none"``。
  五条**全部**收口才成立「落库的 ``tool_name`` 只可能是枚举值」这条不变量
  （任一条漏改即破）。
  **收口在读侧之外**，本模块仍**不做消毒**（如实呈现 DB 里的结构量）。
- ⛔ **绝不复用 ``routes_chat._event_data``**（``routes_chat.py``）——它吐
  **未投影**的原始 payload，是既有 SSE 契约，与落库口径刻意不同。本模块
  **不复用它、也不修它**。

惯例（镜像 ``web/stream_persistence.py``）：

- **模块级函数 + 吃注入的 scope**：无类、无状态，scope 是「返回一个 yield
  session 的上下文管理器」的可调用对象，注入后天然可测；
- **顶层绝不 import ``persistence.database``**：其
  ``engine = _build_engine()`` 在无 ``AGENT_DATABASE_URL`` 时 **import 即抛
  ``RuntimeError``**。默认 scope 由调用方（``routes_developer._event_scope``）
  **函数体内延迟 import**。顶层只 import ``persistence.models``（安全：
  ``models.py`` 不触 ``database.py``）与 ``web.schemas``。
- **只出现 ``chat_stream_events`` 一个表名**，不 JOIN 任何表——因此结构上
  不可能带回会话标题 / 归属（``title`` 是用户首条消息原文、``account`` 是身份
  数据，都超出「只看骨架」口径）。

三条数据侧不变量（写错就出错）：

1. **排序键必须完整**：``created_at ASC, stream_id ASC, sequence ASC,
   event_id ASC``。落库是批量写，PG 的 ``now()`` 是**事务开始时间** → 同一批
   落库的行 ``created_at`` **逐字节相同**（一次 800 分片回复被切成几十批），
   单靠时间戳排序结果不确定。
2. **「真实流事件」的判据是 ``record_kind ∈ ('pushed','unmapped')``**，
   **不是 ``sequence >= 1``**——``unmapped`` 也是真实事件（只是投影白名单没
   认出来），而影子行（``suppressed``）与建流行（``stream_created``）的
   ``sequence`` 恒 0。
3. **``thread_id`` 可空**：生产调用点恒非空、NULL 只出现在测试里，但列表
   查询必须**显式排除 NULL 并显式计数**（``untracked_events``），否则这些行
   会静默消失。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, ContextManager

from sqlalchemy import case, func, select

from administrative_agent.persistence.models import ChatStreamEventRow
from administrative_agent.web.schemas import (
    StreamEventRowOut,
    StreamEventThreadSummary,
    StreamEventTimelineResponse,
)

#: 「真实流事件」的记录类别（见模块 docstring 不变量 2）。
#: ⚠️ **不用 ``sequence >= 1`` 代替**：``unmapped`` 是真实事件但语义上
#: 属于「投影白名单失配」，用 sequence 判会把它和影子行混为一谈。
REAL_RECORD_KINDS = ("pushed", "unmapped")

#: ``error_count`` 的**显式闭集**。
#: ⚠️ **新增会改变 error_count 语义的事件类型时，必须同时改这里 + 前端映射表
#: + 两处测试**（``tests/api/test_stream_event_reader_api.py`` 的错误闭集用例、
#: 前端 ``StreamTimeline`` 的 ``EVENT_TYPE_TEXT``）。绝不用 ``LIKE '%error%'``，
#: 也绝不把 ``task_cancelled`` / ``SESSION_EXPIRED`` 算进来——用户主动取消 /
#: 会话过期不是故障。
ERROR_EVENT_TYPES = ("agent_error", "task_failed", "memory_compression_failed")

#: 分片事件类型（``chunk_count`` 的取数闭集）：一次长回复的绝大部分行是它们，
#: 单列出来让 ``event_count`` 可解释。
CHUNK_EVENT_TYPES = ("agent_reply_chunk", "agent_reasoning_chunk")

#: 列表端点「一次取回全部分组再在 Python 里切片」——见 ``list_threads``。
READ_SCOPE = Callable[[], "ContextManager[Any]"]


def _iso_utc(value: Any) -> str | None:
    """时间戳 → 带 UTC 偏移的 ISO 字符串（**纯函数，独立可测**）。

    ⚠️ **跨方言时区陷阱**：PG 的 ``DateTime(timezone=True)`` 回 **tz-aware**，
    SQLite 回 **naive**（conftest 的裸 DDL 用 ``CURRENT_TIMESTAMP``，值即 UTC）。
    若原样输出不带偏移的字符串，前端 ``new Date('…T02:11:07')`` 会按**本地
    时区**解析 → 时间线整体偏移 8 小时，而这个 bug 在 UTC 的 CI 上**测不出来**。
    故 naive 一律按 UTC 补齐后 ``astimezone(utc).isoformat()``。

    ``None`` 原样返回 ``None``；非 ``datetime`` 值原样返回（不猜、不改写）。
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _iso_utc_required(value: Any) -> str:
    """``_iso_utc`` 的**已证非空**版本（裸 ``str`` 契约的接线点）。

    ``created_at`` 是 NOT NULL 列、``min/max`` 落在 ``GROUP BY`` 的非空分组上
    → 这三个取值点**永不为 null**，故 schema 声明裸 ``str``。真出现 ``None``
    说明前提破了：**直接抛**（fail closed），不做静默兜底、也不退回 ``str | None``
    ——标可选会让下游用 ``?? 0`` 把真 bug 掩成 0。

    ``_iso_utc`` 自身的 ``str | None`` 语义与单测（§7.2 用例 17）保持不变；
    本薄封装只负责「这里不可能是 None」这一契约。
    """
    out = _iso_utc(value)
    if out is None:  # pragma: no cover - 前提：NOT NULL 列 / 非空分组
        raise ValueError("时间字段不可为 None（created_at 为 NOT NULL 列）")
    return out


def list_threads(
    scope: READ_SCOPE, *, limit: int, offset: int
) -> tuple[list[StreamEventThreadSummary], int, int]:
    """最近会话列表一页。

    返回 ``(当页 items, 会话总数 total, 孤儿行数 untracked_events)``。

    **不写 SQL LIMIT/OFFSET，取回全部组后在 Python 里切片**：组数 = 保留期内
    的**会话数**（几千），不是行数；每行只是「ID + 6 个整数 + 2 个时间戳」。
    这样 ``total`` 免费且精确（省掉第二次全表 ``count(distinct thread_id)``），
    且顺带得到 ``untracked_events``（NULL 分组）。保留本仓
    ``{items,total,limit,offset}`` 惯例，**不改 ``has_more``**
    （``Pagination.tsx`` 的 props 按它定义）。

    聚合一律 ``SUM(CASE)`` + ``func.coalesce(..., 0)``：

    - ``coalesce`` 不可省——空组的 ``SUM`` 返回 NULL，不裹会让 Pydantic 直接
      炸在 ``int`` 字段上；
    - **不用 ``FILTER (WHERE …)``**：虽在 SQLite 3.30+ 支持，但本仓其它地方
      从未用过，会让「测试机 sqlite 版本偏低」变成只在某台机器上炸的不确定性；
    - 不用多参数 ``COUNT(DISTINCT a, b)``（PG 扩展），只用单表达式形式。
    """
    col = ChatStreamEventRow
    agg = (
        select(
            col.thread_id,
            func.count().label("event_count"),
            func.count(func.distinct(col.stream_id)).label("stream_count"),
            func.coalesce(
                func.sum(case((col.event_type.in_(CHUNK_EVENT_TYPES), 1), else_=0)),
                0,
            ).label("chunk_count"),
            func.coalesce(
                func.sum(case((col.event_type.in_(ERROR_EVENT_TYPES), 1), else_=0)),
                0,
            ).label("error_count"),
            func.coalesce(
                func.sum(case((col.record_kind == "suppressed", 1), else_=0)), 0
            ).label("suppressed_count"),
            func.coalesce(
                func.sum(case((col.record_kind == "unmapped", 1), else_=0)), 0
            ).label("unmapped_count"),
            func.min(col.created_at).label("first_seen"),
            func.max(col.created_at).label("last_seen"),
        )
        .group_by(col.thread_id)
        # 带次级键 thread_id：last_seen 相同的会话顺序不确定 → 必须定序
        .order_by(func.max(col.created_at).desc(), col.thread_id.asc())
    )

    with scope() as session:
        rows = session.execute(agg).all()

    summaries: list[StreamEventThreadSummary] = []
    untracked = 0
    for row in rows:
        if row.thread_id is None:
            # NULL 分组：孤儿行点不进去，只计数（否则静默消失）
            untracked += int(row.event_count)
            continue
        summaries.append(
            StreamEventThreadSummary(
                thread_id=row.thread_id,
                event_count=int(row.event_count),
                stream_count=int(row.stream_count),
                chunk_count=int(row.chunk_count),
                error_count=int(row.error_count),
                suppressed_count=int(row.suppressed_count),
                unmapped_count=int(row.unmapped_count),
                first_seen=_iso_utc_required(row.first_seen),
                last_seen=_iso_utc_required(row.last_seen),
            )
        )

    total = len(summaries)
    return summaries[offset : offset + limit], total, untracked


def read_thread(
    scope: READ_SCOPE, thread_id: str, *, limit: int, offset: int
) -> StreamEventTimelineResponse:
    """某会话的事件流水一页 + 完整性指标。

    两次查询：一次按 ``stream_id`` 分组的完整性聚合（**顺带给出 ``total``**，
    省掉第三次 ``count(*)``），一次当页行。

    完整性：``expected = Σ每流 MAX(sequence)``、``missing = max(0, expected −
    actual)``，其中 ``actual`` = 真实事件（``REAL_RECORD_KINDS``）条数。
    **``max(0, …)`` 的 clamp 不可省**：宁可报 0 也不报负数。
    前提「每流 ``sequence`` 从 1 连续」写在上面的常量注释里。
    """
    col = ChatStreamEventRow
    real = col.record_kind.in_(REAL_RECORD_KINDS)
    completeness = (
        select(
            col.stream_id,
            func.count().label("row_count"),
            func.coalesce(func.sum(case((real, 1), else_=0)), 0).label("real_count"),
            func.max(case((real, col.sequence), else_=None)).label("max_seq"),
        )
        .where(col.thread_id == thread_id)
        .group_by(col.stream_id)
    )
    # 完整排序键（见模块 docstring 不变量 1）；ASC 是时间线语义（从头读）
    page = (
        select(col)
        .where(col.thread_id == thread_id)
        .order_by(
            col.created_at.asc(),
            col.stream_id.asc(),
            col.sequence.asc(),
            col.event_id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )

    with scope() as session:
        comp_rows = session.execute(completeness).all()
        page_rows = session.scalars(page).all()

    total = sum(int(r.row_count) for r in comp_rows)
    expected = sum(int(r.max_seq or 0) for r in comp_rows)
    actual = sum(int(r.real_count) for r in comp_rows)
    missing = max(0, expected - actual)

    items = [
        StreamEventRowOut(
            event_id=row.event_id,
            stream_id=row.stream_id,
            sequence=row.sequence,
            event_type=row.event_type,
            record_kind=row.record_kind,
            task_id=row.task_id,
            payload=row.payload,
            created_at=_iso_utc_required(row.created_at),
        )
        for row in page_rows
    ]
    return StreamEventTimelineResponse(
        thread_id=thread_id,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        expected_events=expected,
        missing_events=missing,
    )
