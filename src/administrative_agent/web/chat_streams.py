"""SSE 事件流存储（2.7.4 前端状态规则；D22 前端整合后端底座）。

- 每条消息提交（POST /api/v1/chat/messages）建立一条 stream，事件按
  提交顺序追加；
- 事件五要素：event_id / event_type / task_id / sequence / payload
  （2.7.4 L530），全部落在 ``ChatStreamEvent``；
- sequence 自 1 单调递增且不可重复；event_id = ``{stream_id}-e{seq}``。
  断线重连携带 Last-Event-ID（L531）时按事件 ID 定位续传——重复
  sequence 天然被跳过（前端亦按 sequence 丢弃重复）；
- SESSION_EXPIRED（L534）：``mark_expired`` 后，流读取端（routes_chat）
  在事件末尾追加 SESSION_EXPIRED 事件（真实部署中会话过期发生在长连接
  打开期间；本单元提供确定性标记能力与契约测试，前端收到后清理
  Query cache 并跳转登录）；
- cancel（L506）：取消当前生成——只终止事件流继续产出（``cancel`` 后
  ``append`` 拒绝），**不等于**取消已执行的写操作（写操作取消走
  POST /tasks/:taskId/cancel + 8.1 状态机）。
- 内存实现（原型声明）：进程内短时事件投递；页面刷新后从 task API
  恢复状态（L532），流存储不承担持久化。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from administrative_agent.web.stream_persistence import StreamEventRecord
from administrative_agent.web.stream_projection import project, project_suppressed

if TYPE_CHECKING:
    from administrative_agent.web.stream_persistence import StreamEventSink

#: 事件类型（2.7.4 事件流的稳定取值）。
#: 旧任务事件常量（task_created/task_intent/model_stub/task_queued/
#: task_waiting_input/task_reply）已于 harness 改造后删除。
EVENT_TASK_FAILED = "task_failed"
EVENT_TASK_CANCELLED = "task_cancelled"
EVENT_SESSION_EXPIRED = "SESSION_EXPIRED"

#: [2026-08-24 harness 改造] 会话循环图事件类型（Phase 2 由路由层映射
#: 发射；task_failed/task_cancelled 保留）。
EVENT_AGENT_STARTED = "agent_started"
EVENT_TOOL_RUNNING = "tool_running"
EVENT_TOOL_RESULT = "tool_result"
EVENT_CONFIRMATION_REQUESTED = "confirmation_requested"
EVENT_AGENT_REPLY = "agent_reply"
EVENT_AGENT_ERROR = "agent_error"

#: [2026-08-24 流式改造] 决策轮流式回复增量事件（model 节点发射）：
#: agent_reply_start 每轮模型生成开始（前端开新流式气泡）；
#: agent_reply_chunk 回复文本增量（前端逐字追加）；agent_reply 仍为
#: 终态整段（前端落定替换增量文本；缓冲门控只针对 agent_reply，
#: 两个新事件直接落流）。
EVENT_AGENT_REPLY_START = "agent_reply_start"
EVENT_AGENT_REPLY_CHUNK = "agent_reply_chunk"

#: [2026-08-25 思考流式] 模型思考增量（决策轮流式 reasoning_content 透传；
#: 11.4 不落日志、不持久化，SSE 事件流进程内短时投递；前端展示后不持久化、
#: 刷新不重放）
EVENT_AGENT_REASONING_CHUNK = "agent_reasoning_chunk"

#: [2026-08-24 定稿] 记忆压缩事件（SSE 透传；前端未知事件类型忽略）
EVENT_MEMORY_COMPRESSED = "memory_compressed"
EVENT_MEMORY_COMPRESSION_FAILED = "memory_compression_failed"

#: [2026-09-19 可观测性第 2 步] 压缩**判定**台账（compression 节点 5 条出口
#: 各发一条，含 no-op 与成功轮）：与 memory_compressed 分工——一个记「判定」、
#: 一个记「结果」。前端未知事件类型忽略（routes_chat 未登记 → 按原名透传）。
EVENT_COMPRESSION_DECISION = "compression_decision"

#: [2026-09-19 可观测性第 2 步] 模型输入摘要（决策轮 model 节点 / 参数轮
#: tool_args 节点各发一条）：把「模型本轮看到了什么段」落成结构量
#: （历史条数/驱逐明细数/摘要段数/激活流程），回答「模型为什么没看到 X」。
EVENT_DECISION_INPUT_SUMMARY = "decision_input_summary"
EVENT_ARGS_INPUT_SUMMARY = "args_input_summary"

#: [2026-08-31 对话对账批次 3] 对账分析报告事件（reconcile_analyze 生成
#: 差异报告落库后经 sink 发射；路由层 _GRAPH_EVENT_TO_SSE 显式映射——
#: 值即原名，显式声明供前端按 event_type 分支展示报告卡片）
EVENT_RECONCILE_REPORT = "reconcile_report"

#: [2026-09-01 导出功能批次] 导出完成事件（export_data 成功落盘后经 sink
#: 发射；路由层 _GRAPH_EVENT_TO_SSE 显式映射——值即原名，前端按
#: event_type=export_ready 展示导出卡片 + 下载链接）
EVENT_EXPORT_READY = "export_ready"


@dataclass(frozen=True)
class ChatStreamEvent:
    """流内事件（2.7.4 L530 五要素）。

    [2026-08-24 harness 改造] ``task_id`` 可空化：纯对话轮次（无任务）
    的事件 task_id 为 None；现有链路（routes_chat）仍填 task_id，
    行为不变。
    """

    event_id: str
    event_type: str
    task_id: str | None
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)


class StreamNotFoundError(KeyError):
    """stream_id 不存在（2.7.3 → 404 STREAM_NOT_FOUND）。"""


class StreamCancelledError(ValueError):
    """流已取消（L506：取消当前生成后不再产出新事件）。"""


class StreamStore:
    """进程内事件流存储（线程安全）。

    [2026-09-19 可观测性第 0 步] 可注入 ``sink``（``StreamEventSink``）把
    事件流按 A 档粒度旁路落库；``sink=None`` 时 ``append``/``cancel``/
    ``create`` 与改前逐字节等价（落库分支全短路）。**落库集合 ⊋ 推给浏览器
    的集合**：被展示门控抑制的中间轮 ``agent_reply`` 走 ``record_suppressed``
    影子通道（只落库不入流），见其 docstring。
    """

    def __init__(self, sink: "StreamEventSink | None" = None) -> None:
        self._streams: dict[str, list[ChatStreamEvent]] = {}
        self._cancelled: set[str] = set()
        self._expired: set[str] = set()
        #: [2026-08-24 harness 改造] thread_id → 最近创建/追加的 stream_id
        #: （供 /chat/history 恢复路径定位最新流；进程重启丢流但 checkpoint
        #: 不丢，latest_stream_id 丢失时上层回退全量重放）
        self._latest_by_thread: dict[str, str] = {}
        #: stream_id → thread_id 的**反向映射**（归属校验用）。create/append
        #: 传入 thread_id 时同时登记。不能靠读事件 payload 代替：confirm/
        #: resume 路径新建的流不追加 agent_started（routes_chat 只 create），
        #: 事件里没有 thread 信息。
        self._thread_by_stream: dict[str, str] = {}
        self._lock = threading.RLock()
        #: 旁路落库 sink（可为 None；None 时全部落库分支短路）。
        self._sink = sink
        #: 影子行序号（``{stream_id}-x{n:04d}`` 独立命名空间；**绝不写
        #: ``_streams``**，从而不影响真实事件的 ``sequence``/``event_id``）。
        self._suppressed_counts: dict[str, int] = {}

    @property
    def sink(self) -> "StreamEventSink | None":
        """旁路落库 sink（developer 面板读取用；未接线 → None）。"""
        return self._sink

    # ---- 写 ----

    def create(self, stream_id: str, *, thread_id: str | None = None) -> None:
        """建立空流（先于任何事件；重复 create 幂等）。

        [2026-08-24 harness 改造] 可传 thread_id 登记最新流映射
        （不传则只建流，行为与旧调用一致）。

        [2026-09-19 可观测性第 0 步] 追加落库行（``record_kind=
        'stream_created'``，``event_id={stream_id}-c0001``）：``thread_id →
        stream_id`` 的归属映射**只在 create() 时可知**（confirm/resume 路径
        新建的流不追加 agent_started，事件里没有 thread 信息），不落这行
        库里就永远无法把某些流关联回会话。
        """
        with self._lock:
            self._streams.setdefault(stream_id, [])
            if thread_id is not None:
                self._latest_by_thread[thread_id] = stream_id
                self._thread_by_stream[stream_id] = thread_id
        self._submit_stream_created(stream_id, thread_id)

    def append(
        self,
        stream_id: str,
        *,
        event_type: str,
        task_id: str | None,
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> ChatStreamEvent:
        """追加事件：sequence 单调递增（流内第 N 个事件 = N）。

        [2026-08-24 harness 改造] task_id 可 None（纯对话轮次）；可传
        thread_id 登记最新流映射（不传则只追加，行为与旧调用一致）。

        [2026-09-19 可观测性第 0 步] 事件构造在锁内（``_append_locked``），
        **落库提交在锁外**——``_lock`` 是整个 SSE 事件流的唯一串行点，锁内
        只许内存操作（``submit`` 是微秒级纯内存入队，且 ``except
        BaseException`` 全吞，绝不因落库影响业务线程）。
        """
        with self._lock:
            events = self._streams.get(stream_id)
            if events is None:
                raise StreamNotFoundError(stream_id)
            if stream_id in self._cancelled:
                raise StreamCancelledError(stream_id)
            event = self._append_locked(
                stream_id,
                event_type=event_type,
                task_id=task_id,
                payload=payload,
                thread_id=thread_id,
            )
        self._submit_event(stream_id, event, thread_id=thread_id)
        return event

    def _append_locked(
        self,
        stream_id: str,
        *,
        event_type: str,
        task_id: str | None,
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> ChatStreamEvent:
        """锁内事件构造（``sequence``/``event_id`` 算法逐字符保持现状）。

        调用方须已持有 ``self._lock``。**不做取消检查**（``append`` 在调用前
        自判；``cancel`` 需在置取消标记后仍能收尾追加一条终态事件）。
        """
        events = self._streams.get(stream_id)
        if events is None:
            raise StreamNotFoundError(stream_id)
        sequence = len(events) + 1
        event = ChatStreamEvent(
            event_id=f"{stream_id}-e{sequence:04d}",
            event_type=event_type,
            task_id=task_id,
            sequence=sequence,
            payload=dict(payload or {}),
        )
        events.append(event)
        if thread_id is not None:
            self._latest_by_thread[thread_id] = stream_id
            self._thread_by_stream[stream_id] = thread_id
        return event

    def cancel(self, stream_id: str, *, task_id: str = "") -> None:
        """取消当前生成（L506）。

        以 ``task_cancelled`` 事件收尾并标记已取消：后续 append 拒绝；
        不删除已产出事件（前端按 sequence 续播即可看到取消）。
        """
        with self._lock:
            events = self._streams.get(stream_id)
            if events is None:
                raise StreamNotFoundError(stream_id)
            self._cancelled.add(stream_id)
            event = self._append_locked(
                stream_id,
                event_type=EVENT_TASK_CANCELLED,
                task_id=task_id,
                payload={"reason": "generation_cancelled"},
            )
        self._submit_event(stream_id, event)

    # ---- 旁路落库（A 档结构性元数据；sink=None 时全短路） ----

    def record_suppressed(
        self,
        stream_id: str,
        *,
        event_type: str,
        payload: dict[str, Any] | None,
        reason: str,
        thread_id: str | None = None,
    ) -> None:
        """影子行：被展示门控抑制、**未推给浏览器**的事件也落库（⊋ 关系）。

        ``_harness_sink`` 会抑制中间轮的 ``agent_reply``（恰在失败场景里），
        若只把落库挂在 ``append`` 上，事故回溯时{"恰好丢的是最该看的那条"}。
        本方法把这些事件以 ``record_kind='suppressed'`` 旁路落库：

        - **绝不**写 ``_streams``/``_cancelled``/``_expired``/``_latest_by_thread``
          /``_thread_by_stream``——即不改变任何真实流状态，保证推给浏览器的
          事件流与改前逐字节一致；
        - ``event_id`` 用独立命名空间 ``{stream_id}-x{n:04d}``（不与真实
          ``-e{seq}`` 冲突，因此逐流无重复计数）；
        - ``sequence`` 恒 0（影子行不占用真实 sequence）；
        - 本方法自身 ``except BaseException`` 全吞：落库永不影响业务线程。
        """
        try:
            with self._lock:
                n = self._suppressed_counts.get(stream_id, 0) + 1
                self._suppressed_counts[stream_id] = n
            task_id = None
            if isinstance(payload, dict):
                raw_task = payload.get("task_id")
                if isinstance(raw_task, str):
                    task_id = raw_task
            projected = project_suppressed(event_type, payload or {}, reason)
            row = StreamEventRecord(
                event_id=f"{stream_id}-x{n:04d}",
                stream_id=stream_id,
                event_type=event_type,
                record_kind=projected.record_kind,
                payload=projected.payload,
                thread_id=thread_id,
                task_id=task_id,
                sequence=0,
            )
            self._safe_submit(row)
        except BaseException:  # pragma: no cover - 落库守卫（绝不外抛）
            pass

    def _submit_event(
        self,
        stream_id: str,
        event: ChatStreamEvent,
        *,
        thread_id: str | None = None,
    ) -> None:
        """把真实流事件旁路落库（锁外调用；异常全吞）。"""
        sink = self._sink
        if sink is None:
            return
        try:
            projected = project(event.event_type, event.payload)
            row = StreamEventRecord(
                event_id=event.event_id,
                stream_id=stream_id,
                event_type=event.event_type,
                record_kind=projected.record_kind,
                payload=projected.payload,
                thread_id=thread_id or self._thread_by_stream.get(stream_id),
                task_id=event.task_id,
                sequence=event.sequence,
            )
            self._safe_submit(row)
        except BaseException:  # pragma: no cover - 落库守卫（绝不外抛）
            pass

    def _submit_stream_created(
        self, stream_id: str, thread_id: str | None
    ) -> None:
        """落 ``stream_created`` 行（``event_id={stream_id}-c0001``）。

        ``thread_id → stream_id`` 归属只在 create() 时可知（confirm/resume
        路径新建的流不追加 agent_started）。``sink=None`` 时短路。
        """
        sink = self._sink
        if sink is None:
            return
        try:
            row = StreamEventRecord(
                event_id=f"{stream_id}-c0001",
                stream_id=stream_id,
                event_type="stream_created",
                record_kind="stream_created",
                payload={},
                thread_id=thread_id,
                task_id=None,
                sequence=0,
            )
            self._safe_submit(row)
        except BaseException:  # pragma: no cover - 落库守卫（绝不外抛）
            pass

    def _safe_submit(self, row: StreamEventRecord) -> None:
        """``sink.submit`` 二次守卫（submit 自身已吞异常，此处纵深防御）。"""
        sink = self._sink
        if sink is None:
            return
        try:
            sink.submit(row)
        except BaseException:  # pragma: no cover - 落库守卫（绝不外抛）
            pass

    def mark_expired(self, stream_id: str) -> None:
        """标记流会话已过期（L534；routes_chat 据此追加 SESSION_EXPIRED）。"""
        with self._lock:
            if stream_id not in self._streams:
                raise StreamNotFoundError(stream_id)
            self._expired.add(stream_id)

    # ---- 读 ----

    def exists(self, stream_id: str) -> bool:
        with self._lock:
            return stream_id in self._streams

    def latest_stream_id(self, thread_id: str) -> str | None:
        """[2026-08-24 harness 改造] 线程最近流的 stream_id（未登记 None）。

        由 create/append 传 thread_id 时登记；进程内短时定位用，
        checkpoint 才是对话历史的持久恢复路径（方案 §C/风险 2）。
        """
        with self._lock:
            return self._latest_by_thread.get(thread_id)

    def thread_id_of(self, stream_id: str) -> str | None:
        """stream → thread 反向映射（归属校验用；未登记 → None）。"""
        with self._lock:
            return self._thread_by_stream.get(stream_id)

    def is_cancelled(self, stream_id: str) -> bool:
        with self._lock:
            return stream_id in self._cancelled

    def is_expired(self, stream_id: str) -> bool:
        with self._lock:
            return stream_id in self._expired

    def events(
        self, stream_id: str, *, after_event_id: str | None = None
    ) -> list[ChatStreamEvent]:
        """重放事件（2.7.4 L531 断线重连语义）。

        - after_event_id 为 ``{stream_id}-e{seq}``：返回 seq 之后的事件
          （重复 sequence 被跳过）；
        - 无法解析（非本流 ID / 格式错误）→ 从头重放全部事件——前端
          按 sequence 丢弃已处理重复（L531）。
        """
        with self._lock:
            events = self._streams.get(stream_id)
            if events is None:
                raise StreamNotFoundError(stream_id)
            if after_event_id is None:
                return list(events)
            seq = _sequence_of(after_event_id)
            if seq is None or seq < 1 or seq > len(events):
                return list(events)
            return list(events[seq:])


def _sequence_of(event_id: str) -> int | None:
    """从事件 ID 解析 sequence（``{stream_id}-e{seq:04d}``；失败返回 None）。"""
    marker = "-e"
    idx = event_id.rfind(marker)
    if idx < 0:
        return None
    try:
        return int(event_id[idx + len(marker):])
    except ValueError:
        return None


def new_stream_id() -> str:
    return f"stream-{uuid.uuid4().hex[:16]}"
