"""SSE 事件流落库：有界队列 + 守护线程 + 批量写 + 丢弃计数（可观测性第 0 步）。

事故现场易失（日志只到 stdout + 事件流只在内存），本模块把投影后的 SSE
事件（``web/stream_projection.py``，A 档）异步持久化到 ``chat_stream_events``，
保留 14 天。

三条硬约束（贯穿全模块）：
- **C1 落库集合 ⊋ 推给浏览器的集合**：被展示门控抑制的中间轮 ``agent_reply``
  走影子通道（``record_kind='suppressed'``），不继承前端展示门控；
- **C2 写入绝不阻断业务**：``submit()`` 是微秒级纯内存入队、``except BaseException``
  全吞——落库绝不能让业务线程抛异常。业务侧的锁（``StreamStore._lock``）内
  只许内存操作，``submit`` 必须在锁外调用；
- **C3 import 期 DB 不可用时 fail-open**：本模块**顶层绝不 import
  ``persistence.database``**（其 ``engine = _build_engine()`` 在无
  ``AGENT_DATABASE_URL`` 时 import 即抛 ``RuntimeError``）。默认会话工厂在
  **后台线程第一次写之前**才延迟 import + try/except。

两种降级状态（面板据此区分，避免 dev 环境每 60 s 一条 warning）：
- ``database_not_configured``：构造时判定 URL 为空 → ``enabled=False``，只打
  一次 info，不进重试循环；
- ``database_unavailable``：配了但连不上 → 进退避 + 限流 warning。

字段命名纪律（见 ``audit/redaction.py:34-44``）：``is_sensitive_key`` 是**子串**
匹配，键名含 ``token/key/pwd/secret/credential/密码/密钥`` 即被打死为 ``***``。
本模块所有对外字段名（写进日志/health）均已避开——**绝不要**引入
``dropped_tokens`` / ``credential_status`` / ``api_key_errors`` 这类名字。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger("web.stream_persistence")

#: ``dropped_by_type`` 桶数上限（超出归 ``other``）防无界字典。
_MAX_BUCKETS = 32
#: 退避参数：连续失败 0.5 s 起翻倍，上限 30 s；成功一次复位。
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 30.0
#: 失败日志限流：距上次 ≥ 60 s 补打一条。
_LOG_INTERVAL = 60.0
#: 单轮清理最多批数（每批毫秒级短事务）≈ 240 万行/天，远超实际产生速率。
_MAX_PRUNE_BATCHES = 20
#: 清理批间 sleep（让位给写入，避免长事务锁表）。
_PRUNE_BATCH_SLEEP = 0.05
#: payload 落库字节上界，**与迁移 ``ck_chat_stream_events_payload_size``
#: 的 ``pg_column_size(payload) <= 4096`` 保持一致**（这个 4096 是**契约字面量**，
#: 改一处必须改两处——测试 ``test_payload_byte_limit_matches_migration_contract``
#: 会在只改一边时变红）。
_PAYLOAD_BYTE_LIMIT = 4096

#: 逐行预检裕量：紧凑 JSON 文本低估 jsonb 二进制尺寸的最大字节差（见下）。
_PAYLOAD_OVERHEAD_MARGIN = 64
#: 逐行预检阈值 = **DB 上限 − 裕量**，必须**严格小于** ``_PAYLOAD_BYTE_LIMIT``。
#:
#: 为什么预检不能取 ``_PAYLOAD_BYTE_LIMIT``：``_payload_size_estimate`` 是紧凑
#: UTF-8 JSON 长度，而 PG 约束用的是 ``pg_column_size``（jsonb 二进制尺寸），
#: 二者**不相等**——jsonb 有每值 JEntry + 容器 header 开销，**恒大于**紧凑 JSON
#: 文本（故 ``_payload_size_estimate`` 是**低估**）。真 PG 实测
#: （``pg_column_size(j)`` vs ``length(j::text)``，delta = 差）：
#:   单键 + 长字符串（200 字符，1~100 键）        delta **恒 +8**（不随键数增长）
#:   ``card_keys`` 型短字符串数组（10~170 元素）  delta **恒 +14**
#:   多键 + 数字/bool 值（200 个 ``"k":1``）        delta ≈ **+9/键**（200 键 → +1808）
#: 前两态是当前投影的真实形态；第三态只在**大量标量键**时才显著。
#:
#: 若预检阈值取 ``_PAYLOAD_BYTE_LIMIT``：estimate ∈ (4096−delta, 4096] 的行会被
#: 判「不超限」放行 → 实际 ``pg_column_size`` > 4096 → CheckViolation → **整批**
#: 一起死——预检恰恰在它本要守的边界场景上失效，只留下「以为守住了」的错觉。
#:
#: 判断方向（勿改回）：预检**漏判**超限行 → 整批死（代价大）；预检**误杀**本可
#: 入库的行 → 只丢这一行 + 旁路计数（代价小得多）。两害相权，**必须偏向误杀**，
#: 故预检阈值明显低于 DB 上限。
#:
#: ⚠ 裕量 64 成立的**前提**（非断言，只记口径）：真 PG 实测 + 对全部 17 条
#: 投影规则的敌意取值全量扫描——``allow`` 键数最大 **5**（``export_ready``：
#: file_id/entity/row_count/total/expires_at）；``allow + derive`` 最大 **6**；
#: 全部 17 条规则在敌意取值下 delta 最大 **17**（出现在 ``memory_compressed``，
#: 5 键）。64 对 17 仍有 ≥3.7 倍冗余。**若将来某条规则放行 10+ 个标量键**，
#: 按 +9/键 估算 delta 会击穿 64——届时 P0 会以「precheck 放行、DB 拒批」的
#: 形式原样回来。本仓不为潜在陷阱预置 import 期断言（用户明确拒绝为潜在陷阱
#: 加防御代码），只在此写清前提，扩白名单者自判。
#:
#: ⚠ 这两个常量的关系被 ``test_precheck_limit_is_strictly_below_db_limit`` 钉住，
#: 改成 ``= _PAYLOAD_BYTE_LIMIT`` 会让那条断言变红——那不是测试过时，是方向错了。
#: 而**比较点本身**（使用处 ``_split_oversize`` 的算子）由
#: ``test_precheck_drops_row_in_margin_band`` 钉住（取落在裕量带内的路径）。
_PRECHECK_BYTE_LIMIT = _PAYLOAD_BYTE_LIMIT - _PAYLOAD_OVERHEAD_MARGIN


def _payload_size_estimate(payload: dict[str, Any]) -> int:
    """payload 落库字节数的**粗估**（紧凑 UTF-8 JSON 长度）。

    PG 约束是 ``pg_column_size(payload)``（jsonb 二进制尺寸）。紧凑 JSON
    （无空格分隔符、``ensure_ascii=False`` → UTF-8）是其可用纯 Python 得到的
    最近似量；边界处可能与 jsonb 差数个字节，故本值只用于**明显超限**的
    单行预剔除，不参与任何等值判定。不可序列化 → 返回 0（交给 DB 正常拒绝）。
    """
    try:
        return len(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError):  # pragma: no cover - 投影产物必可序列化
        return 0


def _split_oversize(
    rows: list[StreamEventRecord],
) -> tuple[list[StreamEventRecord], list[StreamEventRecord]]:
    """按 ``_PRECHECK_BYTE_LIMIT`` 逐行预检，返回 ``(保留, 超限丢弃)``。

    动机：单行 payload 超限会让**整批** INSERT 抛 CheckViolation → 最多
    ``batch_max``（默认 200）行一起消失、只留一个批次级计数、看不到是哪行。
    这里把「DB 拒绝整批」降级为「丢弃这一行 + 一个旁路计数」。

    用 ``_PRECHECK_BYTE_LIMIT``（**明显低于** DB 上限）而非 DB 上限本身比较，
    是为了让 estimate 的低估误差落在裕量内、不会漏判（理由见常量注释）。
    """
    kept: list[StreamEventRecord] = []
    oversize: list[StreamEventRecord] = []
    for row in rows:
        if _payload_size_estimate(row.payload) > _PRECHECK_BYTE_LIMIT:
            oversize.append(row)
        else:
            kept.append(row)
    return kept, oversize


@dataclass(frozen=True)
class StreamEventRecord:
    """待落库的一条事件行（**已投影**后的结构量）。"""

    event_id: str
    stream_id: str
    event_type: str
    record_kind: str
    payload: dict[str, Any]
    thread_id: str | None = None
    task_id: str | None = None
    sequence: int = 0


@dataclass(frozen=True)
class StreamEventSinkStats:
    """sink 只读快照（developer health 面板数据源）。"""

    enabled: bool
    queued: int
    written: int
    dropped: int
    write_failures: int
    unmapped_events: int
    oversize_dropped: int
    pruned_rows: int
    last_write_age_seconds: float | None
    degraded_reason: str | None
    #: 最近一次写失败的异常**类名**（``IntegrityError`` / ``OperationalError`` …）。
    #: **绝不存 ``str(exc)``**（可能含连接串/SQL 片段）。它与 ``degraded_reason``
    #: 互补：后者只答「降级了没」，它答「**为什么**降级」——投影 ↔ 约束漂移、
    #: 表未迁移都会以 ``database_unavailable`` 呈现，唯有 error_kind 能区分，
    #: 否则只能翻容器日志（第一轮 P0 的误诊正是这么发生的）。
    last_error_kind: str | None


def _default_session_scope_provider() -> Callable[[], Any]:
    """默认会话工厂：延迟 import ``persistence.database.session_scope``。

    生产路径。``database.py`` 的 engine 在 import 期构建（无 URL 即抛），
    故这里必须是**运行时** import + 由调用方 try/except。
    """
    from administrative_agent.persistence.database import session_scope

    return session_scope


@contextmanager
def _factory_scope(factory: Callable[[], Any]) -> Iterator[Any]:
    """从 sessionmaker 构造一次事务（提交/回滚/关闭），供清理函数使用。"""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _prune_expired(
    scope: Callable[[], Any],
    *,
    cutoff: datetime,
    batch_size: int,
    max_batches: int,
) -> tuple[int, int]:
    """分批 DELETE 过期行（避免长事务锁表）。

    返回 ``(删除行数, 批次数)``。``LIMIT`` 走 ``ix_chat_stream_events_created_at``。
    """
    from sqlalchemy import delete, select

    from administrative_agent.persistence.models import ChatStreamEventRow

    total = 0
    batches = 0
    while batches < max_batches:
        with scope() as session:
            subq = (
                select(ChatStreamEventRow.event_id)
                .where(ChatStreamEventRow.created_at < cutoff)
                .limit(batch_size)
            )
            result = session.execute(
                delete(ChatStreamEventRow).where(
                    ChatStreamEventRow.event_id.in_(subq)
                )
            )
            affected = result.rowcount or 0
        if affected == 0:
            break
        total += affected
        batches += 1
        if batches < max_batches:
            time.sleep(_PRUNE_BATCH_SLEEP)
    return total, batches


def cleanup_expired_stream_events(
    factory: Callable[[], Any],
    *,
    ttl_seconds: int | None = None,
    batch_size: int = 5000,
    max_batches: int = _MAX_PRUNE_BATCHES,
) -> tuple[int, int]:
    """启动清理（main.py ``_cleanup_expired_records``）：删 TTL 之外的过期行。

    与 ``ReconcileReportStore(factory).delete_expired()`` 同款——跨进程重建后
    遗留的过期行靠它收敛。返回 ``(删除行数, 批次数)``。
    """
    from administrative_agent.config import settings

    ttl = settings.stream_event_ttl_seconds if ttl_seconds is None else ttl_seconds
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
    return _prune_expired(
        lambda: _factory_scope(factory),
        cutoff=cutoff,
        batch_size=batch_size,
        max_batches=max_batches,
    )


class StreamEventSink:
    """有界队列 + 守护线程的事件落库 sink（线程安全；fail-open）。

    - ``submit(row)``：纯内存入队，绝不阻塞、绝不抛（``except BaseException``
      全吞）；首次调用惰性启动后台线程；
    - ``stats()`` / ``flush_now()`` / ``shutdown()``：面板与测试/停机接口。

    ``session_factory_provider`` 注入是**可测性的关键**：测试注入一个「每次
    都抛」或「阻塞」的 provider 就能测 DB 失败/队列满路径，无需 monkeypatch
    环境变量 + reload 模块（reload 会污染全局 ``engine``）。
    """

    def __init__(
        self,
        *,
        session_factory_provider: Callable[[], Callable[[], Any]] | None = None,
        capacity: int = 10000,
        batch_max: int = 200,
        batch_interval_ms: int = 500,
        ttl_seconds: int = 14 * 86400,
        cleanup_interval_seconds: int = 3600,
        cleanup_batch_size: int = 5000,
        enabled: bool = True,
    ) -> None:
        self._provider = session_factory_provider
        self._capacity = capacity
        self._batch_max = batch_max
        self._batch_interval = batch_interval_ms / 1000.0
        self._ttl_seconds = ttl_seconds
        self._cleanup_interval = cleanup_interval_seconds
        self._cleanup_batch_size = cleanup_batch_size

        self._enabled = enabled
        self._degraded_reason: str | None = None
        if not enabled:
            # 显式传入 disabled（测试/调用方判定）：只打一次 info，不进重试
            self._degraded_reason = "database_not_configured"
            logger.info("stream_event_sink_disabled", reason=self._degraded_reason)

        self._q: deque[StreamEventRecord] = deque()  # 不设 maxlen：显式判满以计丢弃
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._writing = False

        self._dropped = 0
        self._dropped_by_type: dict[str, int] = {}
        self._written = 0
        self._write_failures = 0
        self._consecutive_failures = 0
        self._unmapped_events = 0
        self._oversize_dropped = 0
        self._pruned_rows = 0
        self._last_write_at: float | None = None
        self._last_cleanup_at: float | None = None
        self._last_prune_batches = 0
        self._last_error_at: float | None = None
        self._last_error_kind: str | None = None
        self._last_failure_log_at: float | None = None
        # 首次成功的会话工厂解析缓存（DB 可用后不再反复 import）
        self._resolved_scope: Callable[[], Any] | None = None
        self._next_cleanup_at = time.monotonic() + self._cleanup_interval

    # ---- 入队（业务线程调用；绝不阻塞、绝不抛） ----

    def submit(self, row: StreamEventRecord) -> None:
        """把一条（已投影）事件入队。**纯内存、绝不抛**——红线，见模块 docstring。"""
        try:
            if not self._enabled:
                return
            with self._cv:
                if self._closed:
                    return
                if row.record_kind == "unmapped":
                    self._unmapped_events += 1
                if len(self._q) >= self._capacity:
                    # 丢最老：事故现场价值集中在尾部（失败前最后几步）。
                    # deque(maxlen=N) 的自动丢弃不通知、无法计数 → 必须显式判满。
                    self._q.popleft()
                    self._dropped += 1
                    self._bump_dropped_type(row.event_type)
                self._q.append(row)
                self._cv.notify()
            self._ensure_thread()
        except BaseException:  # noqa: BLE001 —— 落库绝不能让业务线程抛异常
            # 计数在 except 内再自增时也可能抛（极端）——整体吞掉，绝不外抛
            try:
                self._dropped += 1
            except BaseException:  # noqa: BLE001
                pass

    def _bump_dropped_type(self, event_type: str) -> None:
        if event_type in self._dropped_by_type:
            self._dropped_by_type[event_type] += 1
        elif len(self._dropped_by_type) < _MAX_BUCKETS:
            self._dropped_by_type[event_type] = 1
        else:
            self._dropped_by_type["other"] = self._dropped_by_type.get("other", 0) + 1

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run, name="stream-event-writer", daemon=True
            )
            self._thread = thread
            thread.start()

    # ---- 后台线程循环 ----

    def _run(self) -> None:
        while True:
            rows = self._take_batch()
            if rows:
                try:
                    self._write_batch(rows)
                finally:
                    with self._lock:
                        self._writing = False
            self._maybe_cleanup()
            with self._lock:
                if self._closed and not self._q and not self._writing:
                    return

    def _take_batch(self) -> list[StreamEventRecord]:
        with self._cv:
            if not self._q and not self._closed:
                self._cv.wait(timeout=self._batch_interval)
            if not self._q:
                return []
            n = min(self._batch_max, len(self._q))
            rows = [self._q.popleft() for _ in range(n)]
            self._writing = True
            return rows

    def _resolve_scope(self) -> Callable[[], Any]:
        if self._resolved_scope is not None:
            return self._resolved_scope
        provider = self._provider or _default_session_scope_provider
        scope = provider()
        self._resolved_scope = scope
        return scope

    def _write_batch(self, rows: list[StreamEventRecord]) -> None:
        # 逐行预检：明显超限的行单独丢弃（不拖累整批），只留旁路计数 + 日志。
        rows, oversize = _split_oversize(rows)
        if oversize:
            with self._lock:
                self._oversize_dropped += len(oversize)
            logger.warning(
                "stream_event_rows_dropped_oversize",
                dropped=len(oversize),
                event_types=sorted({r.event_type for r in oversize}),
            )
        if not rows:
            return
        try:
            scope = self._resolve_scope()
        except Exception as exc:  # noqa: BLE001 —— 解析失败同写失败处理
            self._record_failure(exc)
            self._sleep_backoff()
            return
        try:
            from administrative_agent.persistence.models import ChatStreamEventRow

            with scope() as session:
                dialect = session.get_bind().dialect.name
                stmt = _insert_stmt(dialect, ChatStreamEventRow)
                session.execute(stmt, [_row_values(r) for r in rows])
        except BaseException as exc:  # noqa: BLE001 —— 失败批次不重入队，直接丢
            self._record_failure(exc)
            self._sleep_backoff()
            return
        self._on_success(len(rows))

    def _record_failure(self, exc: BaseException) -> None:
        """计数 + 限流日志 + 置降级状态（**只存异常类名，绝不存 str(exc)**）。"""
        with self._lock:
            self._write_failures += 1
            self._consecutive_failures += 1
            self._last_error_at = time.monotonic()
            self._last_error_kind = type(exc).__name__
            self._degraded_reason = "database_unavailable"
            consecutive = self._consecutive_failures
            should_log = self._should_log_failure_locked(consecutive)
            if should_log:
                self._last_failure_log_at = time.monotonic()
        if should_log:
            logger.warning(
                "stream_event_sink_write_failed",
                consecutive_failures=consecutive,
                error_kind=self._last_error_kind,
            )

    def _should_log_failure_locked(self, consecutive: int) -> bool:
        """限流：首次 + 连续次数是 2 的幂 + 距上次 ≥ 60 s（DB 全天不可用 ≈ 1440 条）。"""
        if consecutive <= 1:
            return True
        if consecutive & (consecutive - 1) == 0:  # 2 的幂
            return True
        last = self._last_failure_log_at
        return last is None or (time.monotonic() - last) >= _LOG_INTERVAL

    def _sleep_backoff(self) -> None:
        with self._lock:
            n = max(1, self._consecutive_failures)
            delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (n - 1)))
        # 用条件变量等待：shutdown 的 notify_all 能中断退避，停机不被拖住
        with self._cv:
            self._cv.wait(timeout=delay)

    def _on_success(self, n: int) -> None:
        with self._lock:
            self._written += n
            self._consecutive_failures = 0
            self._last_write_at = time.monotonic()
            if self._degraded_reason == "database_unavailable":
                self._degraded_reason = None

    # ---- 周期清理（复用本线程；不新起线程） ----

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + self._cleanup_interval
        # 停机中不做长 DELETE（见 shutdown）
        with self._lock:
            if self._closed:
                return
        try:
            scope = self._resolve_scope()
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._ttl_seconds)
            total, batches = _prune_expired(
                scope,
                cutoff=cutoff,
                batch_size=self._cleanup_batch_size,
                max_batches=_MAX_PRUNE_BATCHES,
            )
        except Exception:  # noqa: BLE001 —— 清理失败不打断写入循环
            logger.warning("stream_event_sink_cleanup_failed", exc_info=True)
            return
        with self._lock:
            self._pruned_rows += total
            self._last_prune_batches = batches
            self._last_cleanup_at = time.monotonic()

    # ---- 面板 / 测试 / 停机 ----

    def stats(self) -> StreamEventSinkStats:
        """只读快照（不加锁：逐字段读；面板容忍轻微不一致）。"""
        last_write = self._last_write_at
        return StreamEventSinkStats(
            enabled=self._enabled,
            queued=len(self._q),
            written=self._written,
            dropped=self._dropped,
            write_failures=self._write_failures,
            unmapped_events=self._unmapped_events,
            oversize_dropped=self._oversize_dropped,
            pruned_rows=self._pruned_rows,
            last_write_age_seconds=(
                None if last_write is None else max(0.0, time.monotonic() - last_write)
            ),
            degraded_reason=self._degraded_reason,
            last_error_kind=self._last_error_kind,
        )

    def flush_now(self, timeout: float = 2.0) -> bool:
        """等待队列写尽（测试用）。返回是否在超时内写尽。"""
        deadline = time.monotonic() + timeout
        while True:
            with self._cv:
                if not self._q and not self._writing:
                    return True
                self._cv.notify_all()
            if time.monotonic() >= deadline:
                with self._cv:
                    return not self._q and not self._writing
            time.sleep(0.005)

    def shutdown(self, timeout: float = 5.0) -> None:
        """停机 drain：置 ``_closed``（此后 ``submit`` 变 no-op）→ 写尽 → join。

        超时 abandon：**必须打一条含丢弃条数的 warning**（否则「停机丢数据」
        永远不可见），**绝不阻塞停机**。停机不做长 DELETE。
        """
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        if thread is not None and thread.is_alive():
            with self._lock:
                pending = len(self._q)
            logger.warning("stream_event_sink_drain_timeout", abandoned=pending)


def _insert_stmt(dialect: str, model: Any) -> Any:
    """构造方言适配的 ``INSERT ... ON CONFLICT (event_id) DO NOTHING``。"""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    return dialect_insert(model).on_conflict_do_nothing(index_elements=["event_id"])


def _row_values(row: StreamEventRecord) -> dict[str, Any]:
    """``StreamEventRecord`` → INSERT 参数字典（``created_at`` 走 server_default）。"""
    return {
        "event_id": row.event_id,
        "stream_id": row.stream_id,
        "thread_id": row.thread_id,
        "task_id": row.task_id,
        "sequence": row.sequence,
        "event_type": row.event_type,
        "record_kind": row.record_kind,
        "payload": row.payload,
    }


# ---------------------------------------------------------------------------
# 进程级默认 sink（生产单例；developer 面板与停机 drain 共用）
# ---------------------------------------------------------------------------

_DEFAULT_SINK: StreamEventSink | None = None
_DEFAULT_SINK_LOCK = threading.Lock()


def default_sink() -> StreamEventSink:
    """进程级默认 sink（惰性构造；只在首次 ``submit`` 时启动后台线程）。

    构造时若 ``AGENT_DATABASE_URL`` 为空 → ``enabled=False`` + 降级原因
    ``database_not_configured``（只打一次 info，不进重试循环）。
    """
    global _DEFAULT_SINK
    with _DEFAULT_SINK_LOCK:
        if _DEFAULT_SINK is None:
            from administrative_agent.config import settings

            configured = bool(settings.agent_database_url)
            _DEFAULT_SINK = StreamEventSink(
                capacity=settings.stream_event_queue_capacity,
                ttl_seconds=settings.stream_event_ttl_seconds,
                cleanup_interval_seconds=settings.stream_event_cleanup_interval_seconds,
                cleanup_batch_size=settings.stream_event_cleanup_batch_size,
                enabled=configured,
            )
        return _DEFAULT_SINK
