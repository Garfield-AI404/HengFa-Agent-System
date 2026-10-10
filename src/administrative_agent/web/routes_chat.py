"""chat 端点（规格 2.7.3 L504-506 + 2.7.4 SSE 事件流；对话式 harness 改造）。

- POST /api/v1/chat/messages              提交消息 → thread_id + stream_id
  （图后台线程执行，POST 快速返回 ~ms 级；task_id 未跑完未知 → None）
- GET  /api/v1/chat/history               对话历史重放（checkpoint）+ 挂起确认卡
- GET  /api/v1/chat/streams/{stream_id}   SSE 模型与任务事件（2.7.4）
- POST /api/v1/chat/streams/{stream_id}/cancel  取消当前生成（L506）
- POST /api/v1/chat/confirm               写操作确认恢复（interrupt resume；
  同款后台化，立即返回 stream_id）
- GET  /api/v1/chat/threads               会话索引列表（chat_threads，当前账号）
- DELETE /api/v1/chat/threads/{thread_id} 删除会话（checkpoint/存档/索引，保护校验）

Phase 3a 移除（注释保留可恢复）：
- 意图全链路：ModelIntentClassifier / ModelIntentExtractor / validate_intent /
  policy_key_for / render_system_prompt 的调用与 import——安全网 = 工具级
  确定性校验（pydantic 参数解析 → scope/角色校验 → 停机检查，tools.py）；
- 无条件 ``tasks.create_task``（建任务下沉工具节点，13.2 复用语义）；
- WRITE_OPERATIONS 停机判定（``lifecycle.accepting_new_writes()`` 在工具
  节点已做——停机中对话消息仍可回复，写工具 fail closed）；
- 旧事件发射（task_created/task_intent/model_stub/task_queued/
  task_waiting_input/task_reply）与 ``ChatMessageResponse.intent`` 字段。
被移除代码可经 git 历史恢复（8757d92 之前）。

SSE 事件流（每 POST 一条 stream，顺序追加；后台化后由后台线程实时
追加，GET /streams 增量轮询即得——agent_started 仍为首发事件）：
  agent_started → (agent_reply | agent_error | tool_running →
      confirmation_requested …)
事件五要素（2.7.4 L530）由 chat_streams.ChatStreamEvent 保证；
断线重连 Last-Event-ID 续传 + 重复 sequence 跳过（L531）由
StreamStore.events(after_event_id=…) 实现；SESSION_EXPIRED（L534）
经 mark_expired 确定性触发。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from administrative_agent.agent.graph import RuntimeBag, build_graph
from administrative_agent.agent.memory.archive import ArchiveStore
from administrative_agent.agent.model_client import ModelDecisionClient
from administrative_agent.agent.state import REMAINING_STEPS, AgentContext
from administrative_agent.agent.tools.rag_memory import without_unconfigured_rag_tools
from administrative_agent.agent.toolsets import AgentKind, resolve_tools
from administrative_agent.audit.service import AuditService
from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.config import settings
from administrative_agent.domains.attachments import AttachmentStore
from administrative_agent.domains.reconcile_reports import ReconcileReportStore
from administrative_agent.files.mapping_service import (
    ExcelMappingService,
    SqlAlchemyMappingStore,
)
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyThreadRepository,
)
from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskService
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_ERROR,
    EVENT_AGENT_REPLY,
    EVENT_AGENT_REPLY_CHUNK,
    EVENT_AGENT_REPLY_START,
    EVENT_AGENT_STARTED,
    EVENT_CONFIRMATION_REQUESTED,
    EVENT_EXPORT_READY,
    EVENT_RECONCILE_REPORT,
    EVENT_SESSION_EXPIRED,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_RUNNING,
    ChatStreamEvent,
    StreamCancelledError,
    StreamNotFoundError,
    StreamStore,
    new_stream_id,
)
from administrative_agent.web.routes_auth import check_csrf
from administrative_agent.web.stream_persistence import default_sink
from administrative_agent.web.schemas import (
    ChatConfirmRequest,
    ChatConfirmResponse,
    ChatHistoryMessage,
    ChatHistoryOut,
    ChatMessageRequest,
    ChatMessageResponse,
    ErrorCode,
    MessageResponse,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("web.routes_chat")

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

#: manager 完整 scope（2.4 六项 + [2026-09-02 R7 收支写] finance 两项，
#: 共八项；与 tests/contract/test_reconciliation.py EXPECTED_SCOPES 一致；
#: scope 名为占位符，待与 ERP 权限表核对——见 mcp/scopes.py REQUIRED_SCOPES
#: 注释。运行态实际注入的 scope 以此为准，只改 scopes.py 不生效）
_MANAGER_SCOPE: frozenset[str] = frozenset(
    {
        "attendance:create",
        "attendance:update",
        "orders:create_bundle",
        "orders:update",
        "erp:query",
        "reconciliation:query",
        "finance:create",
        "finance:update",
    }
)

#: [2026-08-24 harness 改造] 意图分类已移除（Phase 3a）：ModelIntentClassifier/
#: ModelIntentExtractor/validate_intent/policy_key_for 的调用与 import、无条件
#: create_task、WRITE_OPERATIONS 停机判定、task_created/task_intent/model_stub/
#: task_queued/task_waiting_input/task_reply 事件发射全部移除——对话不建任务，
#: 写工具在工具节点内建任务 + interrupt 确认（安全网 = 工具级确定性校验：
#: pydantic 参数解析 → scope/角色 → lifecycle.accepting_new_writes()，停机判定
#: 下沉工具节点）。被移除代码可经 git 历史恢复（8757d92 之前）。
#: 旧事件常量保留于 chat_streams.py（task_failed/task_cancelled 保留发射）。

#: 会话标题长度（首条用户消息截断 40 字符，chat_threads.title 上限 200）。
_THREAD_TITLE_MAX = 40

#: 删除保护状态：线程存在这些非终态任务 → 409 THREAD_BUSY（会话有活动业务，
#: 不允许删除——活动任务清理/确认/复核路径与删除并发会破坏恢复语义）。
_THREAD_DELETE_PROTECTED_STATES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_REVIEW,
    }
)


def _thread_title(message: str) -> str:
    """会话标题 = 首条用户消息截断 40 字符（去除首尾空白；全空白 → 占位）。"""
    title = message.strip()[:_THREAD_TITLE_MAX]
    return title or "（新对话）"


class ThreadOut(BaseModel):
    """会话索引行（GET /api/v1/chat/threads 列表项；chat_threads 账目）。

    只暴露列表所需字段，不返回 account（当前账号列表本身已过滤）。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ThreadListOut(BaseModel):
    """会话索引列表（GET /api/v1/chat/threads；按 updated_at DESC）。"""

    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadOut]


def get_stream_store(request: Request) -> StreamStore:
    """事件流存储依赖：优先 app.state（测试注入），默认模块单例。"""
    store = getattr(request.app.state, "chat_streams", None)
    if store is None:
        store = _DEFAULT_STREAMS
    return store


_DEFAULT_STREAMS = StreamStore(sink=default_sink())
#: 8.3 串行锁必须跨请求共享（同一时刻每线程至多一个写任务 running）——
#: TaskService 缺省自建锁会造成跨请求锁失效，因此经 app.state/模块单例注入
_DEFAULT_WRITE_LOCK = WriteTaskLock()
#: 模型决策客户端（懒构造 ChatOpenAI；key 未配置时决策 fail closed）
_DEFAULT_MODEL_DECISION_CLIENT = ModelDecisionClient()


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），默认 SessionLocal。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _thread_repo(request: Request) -> SqlAlchemyThreadRepository:
    """会话索引仓储依赖（归属校验/登记用；同一 session 工厂）。"""
    return SqlAlchemyThreadRepository(_session_factory(request))


def _thread_owned_by_other(request: Request, account: Account, thread_id: str) -> bool:
    """thread 归属校验的**唯一判定入口**：索引行存在且 account 非本人 → True。

    **索引行缺失一律放行（返回 False）**。为什么不能用严格口径（缺失即
    404）：``chat_threads`` 索引行有**两个写入点**——``_claim_thread``
    （提交时登记，仅限「缺行且线程无既有内容」，见其 docstring）与
    ``_run_submit_background`` 的 try/except/else **成功分支**（见其
    docstring）。两者都可能在图执行后才落行，因此「有 checkpoint、无索引行」
    是合法且可复现的状态——取消、图内异常、``upsert`` 被吞、后台线程启动
    失败四条路径都会留下这种状态，且**首次提交时行本来就不存在**。
    严格 404 会：①打红既有用例（``tests/api/test_chat_api.py`` 钉死「无快照
    → 200 空三元组」）；②让用户重开失败/取消线程时看到「历史加载失败」；
    ③让新线程首次提交就 404（功能直接不可用）。
    """
    record = _thread_repo(request).get(thread_id)
    return record is not None and record.account != account.account


def _require_thread_access(request: Request, account: Account, thread_id: str) -> None:
    """归属校验不通过 → 404（文案「会话不存在。」，不泄露线程存在性）。"""
    if _thread_owned_by_other(request, account, thread_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "会话不存在。"},
        )


def _claim_thread(
    request: Request, account: Account, thread_id: str, title: str
) -> None:
    """**仅在索引行缺失、且线程无既有 checkpoint 内容时** upsert 登记归属
    （提交即登记，不等后台 drain）。

    ``chat_threads`` 的两个写入点之一（另一个是 ``_run_submit_background``
    的成功分支）。

    为什么只在行缺失时写：``upsert`` 既有语义「已存在只刷新 updated_at、
    不改 account」保持不变 → claim **永远不会改绑既有归属**（不抢占他人
    线程）。

    为什么还要「线程无既有内容」这一前置：「索引行缺失」既覆盖「真正的
    新线程」，也覆盖「**已有 checkpoint 内容、但索引行缺失**」的线程——
    后者是系统自己合法产生的状态（取消、图内异常、``upsert`` 被吞、后台
    线程启动失败四条路径）。只看行缺失会让先提交者把**别人的**线程登记
    成自己的 → 对方被永久 404 锁出、登记者可重放对方全部对话并可确认对方
    挂起的确认卡（thread_id 可经 ``GET /tasks`` 枚举）。故此处补一刀：
    **线程已有内容 → 不登记**。

    **但这是部分缓解、非完整收口**：本前置**只堵住「提交后未跑成功」的
    接管路径**——提交者消息在图内失败/取消时，本函数与后台成功分支都不
    落行，他人线程的索引行保持缺失。**「跑成功」路径的接管仍然成立**：
    ``_run_submit_background`` 的 try/except/else **成功分支**仍**无条件**
    ``upsert(thread_id, account.account, _thread_title(message))``（见
    ``_run_submit_background`` 成功分支），而 ``SqlAlchemyThreadRepository.upsert``
    （persistence/repositories.py 约 :207）在**行缺失**时是 **INSERT 且
    用传入的 account**。于是提交者只要让消息正常跑完（发一句问候即可），
    成功分支就把缺失的索引行 INSERT 成提交者的账号 → **原主照样被永久
    404 锁出**。成功与否由提交者选消息即可控制，故本前置挡不住该路径。
    完整收口需「服务端强制生成 thread_id / 不信任客户端传入」或把归属在
    **提交时**持久化（先于图执行落行），**留待后续批次**。

    同时，``_require_thread_access``「行缺失一律放行」的宽松主口径**不变**
    （他人仍可读/追加同一线程，属已知残留，留待后续批次）。

    内容探测复用 ``CheckpointService.load_thread``——**只读**：底层
    ``saver.list`` 对 Postgres 是 SELECT、对 MemorySaver 是内存读，都不
    建 checkpoint、不写任何行、不改状态（``_prepare_submit`` 早已用同一
    调用做只读历史装载，见其 docstring「load_thread 只读提前执行无副作
    用」）。判定基于**线程是否已有内容**，不看 thread_id 字符串格式（前端
    附件路径自造 ``thread-client-*`` 无账号前缀，前缀判据会误伤）。

    整个操作 best effort——失败只记日志不阻断（与后台线程里索引缺口
    best effort、不阻断用户消息同哲学）；checkpoint 未接线时探测抛 503，
    同样被吞并跳过登记（该请求随后在 ``_prepare_submit`` 本就 503）。
    """
    try:
        repo = _thread_repo(request)
        if repo.get(thread_id) is not None:
            return
        # 无索引行：仅当线程**没有既有 checkpoint 内容**时才视为「真新线程」
        # 并登记归属；有内容 → 他人（或历史）线程，不接管。
        if _checkpoint_service(request).load_thread(thread_id) is not None:
            return
        repo.upsert(thread_id, account.account, title)
    except Exception:
        logger.exception(
            "chat_thread_claim_failed",
            thread_id=thread_id,
            account=account.account,
        )


def _write_lock(request: Request) -> WriteTaskLock:
    """串行写锁依赖（跨请求共享；测试经 app.state.write_lock 注入）。"""
    lock = getattr(request.app.state, "write_lock", None)
    if lock is None:
        lock = _DEFAULT_WRITE_LOCK
    return lock


def _mcp_metadata(request: Request) -> Any:
    """ERP 实体元数据缓存依赖（Phase 3c 补接线：AttendanceExecutor 契约校验）。

    [2026-08-24 真实验证补接线] RuntimeBag.metadata 此前从未填充——考勤
    execute 在 ``ensure_write_allowed``/``_assert_contract`` 处必然抛错
    （契约校验在任何 MCP 写调用之前，fail closed），确认恢复事件序列为
    tool_running → tool_result{ok:false}（冒烟复验实测，同问题 1 的
    harness 接线缺口类）。修复理由：属 harness 装配缺口而非业务逻辑——
    graph 层测试本就显式 ``runtime.metadata = EntityCache(...)``
    （tests/agent/test_interrupt.py），生产装配漏填。

    解析顺序：app.state.mcp_metadata（main.py lifespan 与 mcp_client 同
    生命周期接线）→ 按当前 mcp_client 懒构造（api 测试只注入
    FakeMCPClient 的夹具路径仍可用——EntityCache 只依赖 list_entities
    信封）→ mcp_client 未接线 → None（写工具 fail closed 门控在 graph
    层第 3 步，不重复）。
    """
    cache = getattr(request.app.state, "mcp_metadata", None)
    if cache is not None:
        return cache
    client = getattr(request.app.state, "mcp_client", None)
    if client is None:
        return None
    return EntityCache(client, ttl_seconds=settings.erp_mcp_metadata_ttl)


def _model_decision_client(request: Request) -> ModelDecisionClient:
    """模型决策客户端依赖：优先 app.state（测试注入假客户端），默认单例。"""
    client = getattr(request.app.state, "model_decision_client", None)
    if client is None:
        client = _DEFAULT_MODEL_DECISION_CLIENT
    return client


def _tasks(request: Request) -> TaskService:
    return TaskService(
        SqlAlchemyTaskRepository(_session_factory(request)),
        _write_lock(request),
    )


# ---------------------------------------------------------------------------
# [2026-08-24 harness 改造] Phase 3a：线程并发防护（防重复提交，后端兜底）
# ---------------------------------------------------------------------------

#: 进程内活动循环的 thread_id 集合（同一线程有活动图循环 → 409 THREAD_BUSY）。
#: 同步请求在 drain 期间持有；中断/完成即释放。进程内共享（单进程部署边界
#: 见方案风险 4）。
_ACTIVE_THREADS: set[str] = set()
_ACTIVE_THREADS_LOCK = threading.Lock()


def _enter_thread(thread_id: str) -> None:
    """登记线程活动循环；已活动 → 409 THREAD_BUSY（防重复提交）。"""
    with _ACTIVE_THREADS_LOCK:
        if thread_id in _ACTIVE_THREADS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": ErrorCode.THREAD_BUSY.value,
                    "message": "该会话线程有进行中的消息处理，请勿重复提交。",
                },
            )
        _ACTIVE_THREADS.add(thread_id)


def _leave_thread(thread_id: str) -> None:
    """释放线程活动登记（无论成功/异常/挂起）。"""
    with _ACTIVE_THREADS_LOCK:
        _ACTIVE_THREADS.discard(thread_id)


# ---------------------------------------------------------------------------
# [2026-08-24 harness 改造] Phase 2/3a：harness 装配（图运行时 + 事件门控 sink）
# ---------------------------------------------------------------------------


#: 图内事件类型 → SSE 事件类型映射（graph.py 节点 sink 发射 → 2.7.4 契约）。
#: re_requested（hash 失效重新确认）复用 confirmation_requested 事件，
#: payload 带 re_requested=True 标记（前端重新展示卡片并提示确认已失效）。
#: [2026-08-24 流式改造] agent_reply_start / agent_reply_chunk：决策轮
#: 流式增量事件，sink 缓冲逻辑只针对 agent_reply，两个新事件自动直接
#: 落流（前端逐字渲染）。
_GRAPH_EVENT_TO_SSE: dict[str, str] = {
    "confirmation_requested": EVENT_CONFIRMATION_REQUESTED,
    "re_requested": EVENT_CONFIRMATION_REQUESTED,
    "tool_running": EVENT_TOOL_RUNNING,
    "tool_result": EVENT_TOOL_RESULT,
    "agent_reply": EVENT_AGENT_REPLY,
    "agent_reply_start": EVENT_AGENT_REPLY_START,
    "agent_reply_chunk": EVENT_AGENT_REPLY_CHUNK,
    "agent_error": EVENT_AGENT_ERROR,
    # [2026-08-31 对话对账批次 3] reconcile_analyze 生成的差异报告事件：
    # 值即原名（_harness_sink 未知类型本就按原名透传），显式映射声明类型
    # 稳定，前端按 event_type=reconcile_report 展示报告卡片
    "reconcile_report": EVENT_RECONCILE_REPORT,
    # [2026-09-01 导出功能批次] export_data 导出的 Excel 文件事件：值即原名，
    # 显式映射声明类型稳定，前端按 event_type=export_ready 展示导出卡片
    "export_ready": EVENT_EXPORT_READY,
}


def _harness_sink(
    streams: StreamStore, stream_id: str, thread_id: str
) -> tuple[Callable[[str, dict], None], Callable[..., None]]:
    """图内事件 sink → SSE 事件追加（Phase 3a：终局 agent_reply 门控）。

    事件五要素由 chat_streams.ChatStreamEvent 保证；task_id 从图事件
    payload 提取（纯对话事件为 None）；thread_id 登记最新流映射。

    Phase 3a 门控（graph.py 零改动，收敛在路由层）：
    - **agent_reply 缓冲**：model 节点每轮都发 agent_reply（含 call_tool
      中间轮）；本 sink 只保留最后一条，drain 结束后由 ``flush`` 决定是否
      落流——终局轮（reply/needs_input）才落（前端视 agent_reply 为终态
      事件，中间轮发出会误报"回复结束"）；
    - [2026-08-25 loop 不展示] **call_tool 轮直接落流**：call_tool 中间轮
      的 agent_reply 带 ``next_action=call_tool`` 标记，绕过缓冲直接落流
      （前端据此移除流式气泡，不当作终局、不误报"回复结束"）；终局轮
      （reply/needs_input）照旧缓冲、由 flush 收敛落流；
    - **tool_running 合成**：submit 路径写工具挂起前图内不发 tool_running
      （Phase 2 图只在确认恢复时发）——收到首次 confirmation_requested
      （非 re_requested 重挂）时补发，保证契约序列
      ``tool_running → confirmation_requested``（前端"工具执行中"指示）。

    返回 (emit, flush)：``emit`` 为图内 sink 回调；``flush`` 在 drain 结束
    后调用（``flush(interrupted=bool, final_step=str|None)``）收敛缓冲。
    """
    emitted_tool_running: set[str] = set()
    agent_reply_candidate: tuple[str, dict] | None = None

    def _append_sse(event_type: str, payload: dict) -> None:
        sse_type = _GRAPH_EVENT_TO_SSE.get(event_type, event_type)
        data = dict(payload or {})
        if event_type == "re_requested":
            data["re_requested"] = True
        streams.append(
            stream_id,
            event_type=sse_type,
            task_id=data.get("task_id"),
            payload=data,
            thread_id=thread_id,
        )

    def _emit(event_type: str, payload: dict) -> None:
        nonlocal agent_reply_candidate
        if event_type == EVENT_AGENT_REPLY:
            # [2026-08-25 loop 不展示] call_tool 中间轮直接落流（带
            # next_action=call_tool 标记；前端据此移除流式气泡，不当作
            # 终局、不误报"回复结束"）；reply/needs_input 终局轮仍缓冲，
            # 由 flush 收敛后落流（Phase 3a 门控语义不变）。
            if payload.get("next_action") == "call_tool":
                _append_sse(EVENT_AGENT_REPLY, payload)
                return
            agent_reply_candidate = (event_type, dict(payload or {}))
            return
        if event_type == "confirmation_requested":
            task_id = payload.get("task_id")
            if task_id and task_id not in emitted_tool_running:
                # 首次确认（非 re_requested 重挂）：补发 tool_running，
                # 保证 submit 路径契约序列 tool_running → confirmation_requested
                emitted_tool_running.add(task_id)
                _append_sse(EVENT_TOOL_RUNNING, {
                    "task_id": task_id,
                    "tool_name": payload.get("tool_name"),
                })
        _append_sse(event_type, payload)

    def _flush(*, interrupted: bool, final_step: str | None = None) -> None:
        nonlocal agent_reply_candidate
        if agent_reply_candidate is None:
            return
        if interrupted:
            # 挂起（确认卡）：中间轮的"正在调用工具"等不得落流
            # [2026-09-19 可观测性第 0 步] 落库集合 ⊋ 推送集合：被抑制的这条
            # 恰恰出现在事故场景里，走影子通道旁路落库（不入流、不改状态）。
            streams.record_suppressed(
                stream_id,
                event_type=agent_reply_candidate[0],
                payload=agent_reply_candidate[1],
                reason="interrupted",
                thread_id=thread_id,
            )
            agent_reply_candidate = None
            return
        if final_step in ("model_failed", "steps_exhausted"):
            # 终局轮为模型失败/步数耗尽：缓冲的是更早轮的回复，不得冒充终局
            streams.record_suppressed(
                stream_id,
                event_type=agent_reply_candidate[0],
                payload=agent_reply_candidate[1],
                reason=f"final_step:{final_step}",
                thread_id=thread_id,
            )
            agent_reply_candidate = None
            return
        _append_sse(*agent_reply_candidate)
        agent_reply_candidate = None

    return _emit, _flush


def _harness_runtime(
    request: Request,
    account: Account,
    thread_id: str,
    stream_id: str,
    *,
    sink: Callable[[str, dict], None] | None = None,
    agent_kind: AgentKind | str | None = None,
) -> RuntimeBag:
    """组装 harness 图运行时闭包（Phase 2/3a：submit 与 confirm 恢复共用）。

    依赖全部请求级注入（与旧流水线同模式：app.state 优先，默认单例）：
    - client：模型决策客户端（app.state.model_decision_client 或默认）；
    - tasks/audit：TaskService(SqlAlchemyTaskRepository, 共享串行锁) +
      AuditService（policy 三列/attempt 由 Executor 内部保持，11.1）；
    - context：AgentContext（真实账号 + manager scope + thread_id，
      8.4 身份不信任前端）；
    - mcp_client：app.state.mcp_client（测试注入真 MCPClient + respx 假
      服务；生产未接线 → 写工具 fail closed）；
    - rag_client：[2026-09-22 RAG 长期记忆接入] app.state.rag_client（第二个
      MCP 客户端实例，与 mcp_client 独立；生产未配置 rag_mcp_url 时为 None
      → 三个 rag_* 工具 fail closed 且**不注入模型可见工具集**）；
    - lock：共享 WriteTaskLock（app.state.write_lock，8.3 跨请求串行）；
    - sink：图事件 → SSE（缺省经 ``_harness_sink`` 门控包装）；
    - agent_kind：agent 类型口子（本轮恒为 ``None`` = 未指定 → 全量工具，
      经 ``resolve_tools`` 解析；来源未定，调用点一律走默认值不显式传）。
    """
    streams = get_stream_store(request)
    factory = _session_factory(request)
    if sink is None:
        sink, _ = _harness_sink(streams, stream_id, thread_id)
    # [2026-09-22 RAG 长期记忆接入] rag_mcp_url 未配置时 rag_client 为 None
    # → 从模型可见工具集剔除 RAG 侧全部工具（rag_remember/rag_recall/rag_forget/
    # rag_search_documents；模型看不到就不会反复调一个必然失败的工具）；
    # 已配置时零拷贝返回同一工具集。
    rag_client = getattr(request.app.state, "rag_client", None)
    tools = without_unconfigured_rag_tools(resolve_tools(agent_kind), rag_client)
    return RuntimeBag(
        client=_model_decision_client(request),
        tools=tools,
        tasks=_tasks(request),
        audit=AuditService(
            SqlAlchemyAuditRepository(factory),
            SqlAlchemyAttemptRepository(factory),
        ),
        context=AgentContext(
            user_id=account.account,
            user_role=account.role.value,
            scope=_MANAGER_SCOPE,
            thread_id=thread_id,
        ),
        mcp_client=getattr(request.app.state, "mcp_client", None),
        rag_client=rag_client,
        metadata=_mcp_metadata(request),
        lock=_write_lock(request),
        archive=ArchiveStore(factory),
        # [2026-08-31 对话对账批次 3] 对话对账依赖槽：app.state 对应属性
        # 优先（测试注入假仓储），缺省用 _session_factory(request) 构造
        # （与 _session_factory 同为 app.state.session_factory 优先、默认
        # SessionLocal；None 时落回构造，测试未注入即生产装配）
        attachment_store=getattr(request.app.state, "attachment_store", None)
        or AttachmentStore(factory),
        report_store=getattr(request.app.state, "report_store", None)
        or ReconcileReportStore(factory),
        mapping_service=getattr(request.app.state, "mapping_service", None)
        or ExcelMappingService(SqlAlchemyMappingStore(factory)),
        review_repo=getattr(request.app.state, "review_repo", None)
        or SqlAlchemyReviewRepository(factory),
        sink=sink,
    )


def _checkpoint_service(request: Request) -> Any:
    """checkpoint 服务依赖（main.py lifespan 接线；未接线 → 503 fail closed）。"""
    service = getattr(request.app.state, "checkpoint_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": ErrorCode.SERVICE_UNAVAILABLE.value,
                "message": "chat 服务暂不可用（checkpoint 未接线），请稍后重试。",
            },
        )
    return service


def _initial_inputs(
    restored: dict[str, Any] | None,
    message: str,
    attachment_note: str = "",
) -> dict[str, Any]:
    """构造图初始输入：checkpoint 重放状态（无历史 → 全默认）+ 本次 human 消息。

    - 保留线程既有状态（messages/policy_keys/pending_confirmation 等）；
    - [2026-09-09 步数预算语义调整] remaining_steps 不再随线程累计：
      每条新 human 消息（用户一条新指示进入图执行）无条件重置回
      REMAINING_STEPS（见函数体注释，与 write_retry_counts 同哲学）；
      确认卡 resume（Command(resume)）不经本函数、不重置——恢复执行
      继续消耗挂起轮剩余的步数额度，防死循环语义不变；
    - 缺省键补默认值（首次提交的全新线程）；
    - 本次 human 消息追加到 messages 末尾（add_messages 按 id 去重，
      与 checkpoint 既有消息合并不重复）；
    - [2026-08-31 对话对账批次 3] attachment_note 非空时以
      ``[附件：…]`` 说明块拼在原始消息**之前**——模型据此知道附件存在、
      触发 reconcile_analyze 读取；附件内容不进消息（由工具经 file_id
      读取）。
    """
    inputs = dict(restored or {})
    inputs.setdefault("messages", [])
    inputs.setdefault("trace_id", "trace-" + uuid.uuid4().hex[:16])
    inputs.setdefault("current_step", "created")
    # [2026-09-09 生产故障修复] remaining_steps 原为线程级累计预算（每次
    # 模型决策 -1，跨整条会话永不重置）：某条消息耗尽后该线程所有后续
    # 提问都被 model 节点拦截（"已达本轮执行上限"）。业务决定改为**每条
    # 用户消息重置**——与下方 write_retry_counts 同哲学：每次新 human
    # 消息即用户明确指示，额度无条件恢复为 REMAINING_STEPS（setdefault
    # 会把 checkpoint 里的旧值原样带进新一轮，耗尽线程依旧锁死）。防
    # 失控循环语义不受影响：单条消息内的模型-工具循环仍在一次图执行内
    # 连续递减（model 节点每次决策 -1），耗尽拦截照常生效；确认卡
    # resume（Command(resume)）不经本函数、不重置，继续在挂起轮剩余
    # 额度内执行。
    inputs["remaining_steps"] = REMAINING_STEPS
    inputs.setdefault("policy_keys", [])
    inputs.setdefault("pending_action", None)
    inputs.setdefault("pending_confirmation", None)
    # [2026-08-31 修复确认卡重复循环] 每次新 human 消息即用户明确指示：
    # 写操作连续失败计数无条件归零（setdefault 会把 checkpoint 里的旧计数
    # 原样带进新一轮，导致用户重新发起后仍被拦截锁死）；模型轮内自主重试
    # 仍在 checkpoint 中累积计数（tools/confirm 节点 bump），拦截照常生效。
    inputs["write_retry_counts"] = {}
    inputs.setdefault("compression_stats", {})
    inputs.setdefault("summaries", [])
    inputs.setdefault("summary_boundary", 0)
    content = message if not attachment_note else f"{attachment_note}\n{message}"
    inputs["messages"] = [*inputs["messages"], HumanMessage(content=content)]
    return inputs


def _attachment_expired(record: Any) -> bool:
    """附件是否已过期（expires_at <= now；SQLite 测试库回读 naive datetime
    时补时区后比较——与 checkpoint.py _ensure_aware 同口径）。"""
    expires_at = getattr(record, "expires_at", None)
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _validate_attachments(
    request: Request, attachment_ids: list[str] | None, thread_id: str
) -> str:
    """附件引用校验（[2026-08-31 对话对账批次 3]；fail closed 整条拒绝）。

    - 任一附件不存在/已过期/不属于当前会话 → HTTPException 400（不部分
      放行——消息要么带全部有效附件进入，要么整条拒绝）；
    - 重复 id 去重（同附件只说明一次）；
    - 返回附件说明块：``[附件：{filename}（{content_type}，{size_bytes}
      字节，id={attachment_id}）]`` 按序换行拼接，供 _initial_inputs 拼在
      原始消息之前。
    """
    if not attachment_ids:
        return ""
    store = AttachmentStore(_session_factory(request))
    records: list[Any] = []
    seen: set[str] = set()
    for aid in attachment_ids:
        if not aid or aid in seen:
            continue
        seen.add(aid)
        record = store.get(aid)
        if (
            record is None
            or _attachment_expired(record)
            or record.thread_id != thread_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "附件无效、已过期或不属于当前会话，请重新上传后重试。"},
            )
        records.append(record)
    return "\n".join(
        f"[附件：{r.filename}（{r.content_type}，{r.size_bytes} 字节，"
        f"id={r.attachment_id}）]"
        for r in records
    )


# ---------------------------------------------------------------------------
# 提交消息（写 API：manager + CSRF；2.7.3 L504；Phase 3a 重写）
# ---------------------------------------------------------------------------


@router.post(
    "/messages",
    response_model=ChatMessageResponse,
    responses={
        409: {"description": "THREAD_BUSY"},
        503: {"description": "SERVICE_UNAVAILABLE"},
    },
)
def submit_message(
    request: Request,
    body: ChatMessageRequest,
    account: Account = Depends(require_manager),
) -> ChatMessageResponse:
    """提交消息：会话循环图后台执行 + SSE 实时推送（Phase 3a；对话不建任务）。

    [2026-08-25 POST 快速返回改造] POST **立即返回**（~ms 级）——
    ``{thread_id, stream_id, task_id: None}``（task_id 图未跑完未知；
    None 为既有合法值，前端不使用）；图在后台 daemon 线程同步 drain，
    事件实时追加到 StreamStore，前端经 GET /streams 轮询/SSE 增量拉取
    （agent_started 仍为首发事件）。

    流程（方案 E 节 + 后台化）：
    1. CSRF + body 校验；thread_id 缺省生成 ``thread-{account}-{uuid8}``
       （保持兼容，Phase 3b 前端开始显式传）并返回；
    2. 并发防护：``_enter_thread`` 在 handler 同步登记（提交瞬间重复提交
       → 409 THREAD_BUSY，语义不变），锁由后台线程 finally 释放（活动
       循环期间持续持有）；
    3. 同步装配：checkpoint 历史装载（load_thread 只读）+ 本次 human
       消息 → inputs/config + harness（emit/flush/runtime/graph）——
       checkpoint 未接线 → 503 同步返回（fail closed 语义不变）；
    4. 后台线程 ``graph.stream(inputs, config, stream_mode="updates")``
       同步 drain：agent_started 路由首发；agent_reply 门控（终局轮才
       落）；写工具挂起 → 工具节点确认卡事件（confirmation_requested，
       经 sink mapping）已落流——挂起检测用 ``__interrupt__`` chunk
       （Phase 2 遗留 quirk：hash-mismatch 重挂时 get_state().next
       为 ()，不可靠）；
    5. 响应：``{thread_id, stream_id, task_id}``——task_id 图未跑完未知
       → None。

    模型失败路径：图内 model 节点 fail closed（agent_error + 失败消息，
    fail closed 纪律不变）——后台线程异常安全网：图外异常 → 流内
    agent_error{error="chat_run_failed"} 终态事件（前端一定收到终态、
    输入框恢复），不再 500（14.4 不泄露内部细节）。
    """
    check_csrf(request)
    thread_id = body.thread_id or f"thread-{account.account}-{uuid.uuid4().hex[:8]}"
    # [thread 归属校验] 提交前置校验：他人线程 → 404（在附件校验**之前**，
    # 保证 404 优先于 400，且不对他人线程做附件表探测）。
    _require_thread_access(request, account, thread_id)
    # [2026-08-31 对话对账批次 3] 附件引用校验（fail closed：任一附件
    # 无效/过期/跨线程 → 400 整条拒绝，不进并发防护登记）
    attachment_note = _validate_attachments(request, body.attachment_ids, thread_id)
    # claim 在附件校验**之后**（附件 400 时不建索引行——否则用户侧栏出现
    # 一条没有回复的幽灵会话），且在并发防护登记**之前**（不占活动锁）。
    _claim_thread(request, account, thread_id, _thread_title(body.message))
    _enter_thread(thread_id)
    try:
        stream_id, inputs, config, flush, graph, streams = _prepare_submit(
            request, account, thread_id, body.message, attachment_note=attachment_note
        )
    except BaseException:
        # 装配异常（checkpoint 未接线 503 / 装载失败等）→ 同步返回，锁即刻释放
        _leave_thread(thread_id)
        raise
    try:
        threading.Thread(
            target=_run_submit_background,
            kwargs={
                "request": request,
                "account": account,
                "thread_id": thread_id,
                "message": body.message,
                "stream_id": stream_id,
                "streams": streams,
                "inputs": inputs,
                "config": config,
                "flush": flush,
                "graph": graph,
            },
            daemon=True,
        ).start()
    except Exception:
        # 线程启动异常（不可能但兜底）：锁释放后 500，复用现 CHAT_RUN_FAILED 文案
        _leave_thread(thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": ErrorCode.CHAT_RUN_FAILED.value,
                "message": "对话处理失败，请稍后重试。",
            },
        ) from None
    return ChatMessageResponse(thread_id=thread_id, stream_id=stream_id, task_id=None)


def _prepare_submit(
    request: Request,
    account: Account,
    thread_id: str,
    message: str,
    attachment_note: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any], Callable[..., None], Any, StreamStore]:
    """提交同步装配（handler 层；POST 快速返回的前半段）。

    [2026-08-25 POST 快速返回改造] 事件流建立 + agent_started 首发 +
    历史装载 + inputs/config + harness（emit/flush/runtime/graph）全部在
    handler 同步完成：checkpoint 未接线 → 503 仍同步返回（fail closed
    语义不变）；load_thread 只读提前执行无副作用；图/运行时装配为纯
    对象构造无 I/O。drain 部分移交后台线程（``_run_submit_background``）。

    [2026-08-31 对话对账批次 3] attachment_note：附件说明块（_validate_
    attachments 产出），经 _initial_inputs 拼入本次 human 消息之前。
    """
    service = _checkpoint_service(request)
    streams = get_stream_store(request)

    stream_id = new_stream_id()
    streams.create(stream_id, thread_id=thread_id)
    streams.append(
        stream_id,
        event_type=EVENT_AGENT_STARTED,
        task_id=None,
        payload={"thread_id": thread_id},
        thread_id=thread_id,
    )

    restored = service.load_thread(thread_id)
    inputs = _initial_inputs(restored, message, attachment_note)
    config = service.new_graph_config(thread_id)

    emit, flush = _harness_sink(streams, stream_id, thread_id)
    runtime = _harness_runtime(request, account, thread_id, stream_id, sink=emit)
    graph = build_graph(checkpointer=service.checkpointer, runtime=runtime)
    return stream_id, inputs, config, flush, graph, streams


def _run_submit_background(
    request: Request,
    account: Account,
    thread_id: str,
    message: str,
    stream_id: str,
    streams: StreamStore,
    inputs: dict[str, Any],
    config: dict[str, Any],
    flush: Callable[..., None],
    graph: Any,
) -> None:
    """后台线程执行会话图 drain（POST 快速返回后事件实时追加）。

    [2026-08-25 POST 快速返回改造] 图在 daemon 线程同步 drain，事件经
    sink 实时追加到 StreamStore——前端 GET /streams 轮询即得增量；
    finally ``_leave_thread`` 释放并发防护锁（活动循环期间 409 THREAD_BUSY
    语义不变）。

    异常安全网（图外异常不再 500——POST 已返回，改经流内终态事件）：
    - StreamCancelledError（cancel 端点已追加 task_cancelled 终态并标记
      流取消，图继续跑时 sink 的 append 抛此异常）→ 只记日志不再 append
      （append 会再抛）；
    - 其他异常（checkpoint/图执行等，含 HTTPException——图执行路径不发
      它，catch-all 兜底）→ agent_error{error="chat_run_failed"} 终态
      事件（前端一定收到终态、输入框恢复；append 自身再抛 → 吞掉记日志，
      流已有终态事件）。

    会话索引 upsert 仅在 drain 成功路径执行（原语义：失败/取消在下次
    提交自愈，upsert 幂等）。**本处是 ``chat_threads`` 的两个写入点之一**
    （另一个是 ``_claim_thread``，提交时登记、仅限「缺行且线程无既有
    内容」，见其 docstring）——**行已存在**时只刷新 updated_at（不改 account、不覆盖
    title）；但**行缺失**时 ``upsert`` 是 INSERT 且**用传入的 account**
    （即本次提交者）。后者使本处成为「**跑成功路径接管**」的载体：他人
    线程若索引行缺失且本次消息正常跑完，行会被 INSERT 成提交者 → 原主被
    永久 404 锁出（``_claim_thread`` 的前置只挡「跑失败」路径，见其
    docstring）；收口留待后续批次。
    """
    try:
        try:
            interrupted = False
            for chunk in graph.stream(inputs, config=config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    interrupted = True
            snapshot = graph.get_state(config)
            flush(interrupted=interrupted, final_step=snapshot.values.get("current_step"))
        except StreamCancelledError:
            # 异常路径同样收敛缓冲：已缓冲的中间轮 agent_reply 不落流，
            # 但绝不能静默消失——走影子通道落库（reason="interrupted"）。
            # flush 对无缓冲是 no-op，重复调用安全（首个 flush 已清空候选）。
            flush(interrupted=True)
            logger.info(
                "chat_stream_cancelled", stream_id=stream_id, thread_id=thread_id
            )
        except Exception:
            flush(interrupted=True)
            logger.exception(
                "chat_run_background_failed", stream_id=stream_id, thread_id=thread_id
            )
            _append_run_failed(streams, stream_id, thread_id)
        else:
            # 会话索引挂载：登记/刷新 chat_threads（标题 = 首条用户消息截断
            # 40 字符；已存在线程只更新 updated_at，title 不覆盖）。失败
            # **不**阻断——消息已处理完毕并落 checkpoint；索引缺口在下次
            # 提交时自愈（upsert 幂等）。
            try:
                SqlAlchemyThreadRepository(_session_factory(request)).upsert(
                    thread_id, account.account, _thread_title(message)
                )
            except Exception:
                logger.exception(
                    "chat_thread_index_failed",
                    thread_id=thread_id,
                    account=account.account,
                )
    finally:
        _leave_thread(thread_id)


def _append_run_failed(streams: StreamStore, stream_id: str, thread_id: str) -> None:
    """图外异常安全网：追加 agent_error 终态事件（前端一定收到终态恢复输入）。

    [2026-08-25 POST 快速返回改造] 后台线程异常不再映射 500——POST 已
    返回，改经流内终态事件通知前端（前端 AGENT_ERROR_TEXT 含
    chat_run_failed 文案）。append 自身抛异常（流被并发取消等）→ 流已
    有终态事件（task_cancelled），只记日志不再重复处理。
    """
    try:
        streams.append(
            stream_id,
            event_type=EVENT_AGENT_ERROR,
            task_id=None,
            payload={"error": "chat_run_failed", "message": "对话处理失败，请稍后重试。"},
            thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "chat_agent_error_append_failed", stream_id=stream_id, thread_id=thread_id
        )


# ---------------------------------------------------------------------------
# 对话历史（GET；manager；Phase 3a 新增；checkpoint 重放恢复路径）
# ---------------------------------------------------------------------------


@router.get(
    "/history",
    response_model=ChatHistoryOut,
    responses={404: {"description": "会话不存在（归属不匹配一律 404，不泄露存在性）"}},
)
def chat_history(
    request: Request,
    thread_id: str = Query(min_length=1, max_length=128),
    account: Account = Depends(require_manager),
) -> ChatHistoryOut:
    """对话历史重放（Phase 3a；页面刷新/重连恢复路径）。

    - ``CheckpointService.load_thread(thread_id)`` → messages 转
      ``[{role, content}]``（LangChain 消息 → 前端形态；task_id 不逐条
      携带——前端从事件流已有信息补，history 层不强求）；
    - pending_confirmation：线程挂起确认卡（六要素 + payload_hash +
      task_id；**不含**执行上下文 execution——前端确认只回 payload_hash，
      服务端从 checkpoint 取执行载荷）；
    - stream_id：``StreamStore.latest_stream_id(thread_id)``（进程重启丢流
      → null，前端只重放历史不订阅；checkpoint 才是持久恢复路径）；
    - 无快照 → ``{messages: [], pending_confirmation: null, stream_id: null}``。
    """
    service = _checkpoint_service(request)
    _require_thread_access(request, account, thread_id)
    restored = service.load_thread(thread_id)
    if not restored:
        return ChatHistoryOut()
    messages = [
        ChatHistoryMessage(role=role, content=content)
        for role, content in (_history_message_out(m) for m in (restored.get("messages") or []))
    ]
    stream_id = get_stream_store(request).latest_stream_id(thread_id)
    return ChatHistoryOut(
        messages=messages,
        pending_confirmation=_pending_card_out(restored.get("pending_confirmation")),
        stream_id=stream_id,
    )


def _history_message_out(message: Any) -> tuple[str, str]:
    """LangChain 消息 → (role, content)（工具消息为 JSON 字符串）。"""
    role = getattr(message, "type", None) or "unknown"
    content = getattr(message, "content", "")
    if not isinstance(content, str):
        content = str(content)
    return role, content


def _pending_card_out(pc: Any) -> dict[str, Any] | None:
    """挂起确认卡 → 前端形态（六要素 + payload_hash + task_id；不含 execution）。"""
    if not pc:
        return None
    return {
        "confirm_type": pc.get("confirm_type"),
        "task_id": pc.get("task_id"),
        "tool_name": pc.get("tool_name"),
        "card": pc.get("card"),
        "payload_hash": pc.get("payload_hash"),
    }


# ---------------------------------------------------------------------------
# 会话索引（GET/DELETE /api/v1/chat/threads；2026-08-24 对话会话管理）
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=ThreadListOut)
def list_threads(
    request: Request,
    account: Account = Depends(require_manager),
) -> ThreadListOut:
    """会话索引列表（当前账号；updated_at DESC）。

    - 只返回当前账号的会话（chat_threads 按 account 过滤）；
    - 列表项字段 = thread_id/title/created_at/updated_at（前端侧栏
      「历史会话」渲染与恢复/删除入口）。
    """
    repo = SqlAlchemyThreadRepository(_session_factory(request))
    return ThreadListOut(
        threads=[
            ThreadOut(
                thread_id=record.thread_id,
                title=record.title,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in repo.list_by_account(account.account)
        ]
    )


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "会话不存在（归属不匹配一律 404，不泄露存在性）"},
        409: {"description": "THREAD_BUSY（有活动任务/挂起确认/进行中消息处理）"},
        500: {"description": "CHAT_RUN_FAILED"},
        503: {"description": "SERVICE_UNAVAILABLE"},
    },
)
def delete_thread(
    request: Request,
    thread_id: str,
    account: Account = Depends(require_manager),
) -> None:
    """删除会话（2026-08-24：checkpoint + 存档 + 索引，一次性清理）。

    破坏性操作的三重防护（缺一不可，见 2026-08-24 方案）：
    1. **归属校验**：索引记录不存在或 account 不匹配 → 404（两者同口径，
       不泄露线程存在性）；
    2. **活动保护**：线程存在非终态任务（pending/running/waiting_confirmation/
       waiting_review）→ 409 THREAD_BUSY；或 checkpoint 重放含挂起确认
       （pending_confirmation 非空）→ 409 同码；进程内该线程有活动图循环
       （_ACTIVE_THREADS）→ 409 同码（防删除与流式生成并发）；
    3. **执行顺序**：先数据后索引——ArchiveStore.delete_thread →
       checkpointer.delete_thread（PostgresSaver 公开 API，不存在线程时
       幂等删除 0 行）→ chat_threads 索引；任一步失败 → 500
       CHAT_RUN_FAILED 且**索引保持不动**（重试幂等：前序删除再跑无害，
       不会因部分删除而永久丢失索引）。

    只删会话自己的数据（checkpoint/archive/索引）；审计记录、复核记录、
    映射版本等 Agent 表**不随删除**（11.1 审计不可变）。
    """
    check_csrf(request)
    service = _checkpoint_service(request)
    factory = _session_factory(request)
    repo = SqlAlchemyThreadRepository(factory)
    record = repo.get(thread_id)
    if record is None or record.account != account.account:
        # 不存在与归属不匹配同一 404 口径（不泄露存在性）
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "会话不存在。"},
        )
    # 活动保护 1：非终态任务
    for task in SqlAlchemyTaskRepository(factory).list_by_thread(thread_id):
        if task.status in _THREAD_DELETE_PROTECTED_STATES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": ErrorCode.THREAD_BUSY.value,
                    "message": "该会话有进行中的任务，暂不能删除。",
                },
            )
    # 活动保护 2：挂起确认卡（checkpoint 重放）
    restored = service.load_thread(thread_id)
    if restored and restored.get("pending_confirmation"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.THREAD_BUSY.value,
                "message": "该会话有待确认的操作，暂不能删除。",
            },
        )
    # 活动保护 3：进程内活动图循环（防删除与流式生成并发）
    with _ACTIVE_THREADS_LOCK:
        if thread_id in _ACTIVE_THREADS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": ErrorCode.THREAD_BUSY.value,
                    "message": "该会话线程有进行中的消息处理，请稍后重试。",
                },
            )
    # 执行删除：先数据后索引；失败 → 500 且索引保持（重试幂等）
    try:
        ArchiveStore(factory).delete_thread(thread_id)
        service.checkpointer.delete_thread(thread_id)
        repo.delete(thread_id)
    except Exception:
        logger.exception(
            "chat_thread_delete_failed",
            thread_id=thread_id,
            account=account.account,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": ErrorCode.CHAT_RUN_FAILED.value,
                "message": "会话删除失败，请稍后重试。",
            },
        ) from None
    logger.info("chat_thread_deleted", thread_id=thread_id, account=account.account)


# ---------------------------------------------------------------------------
# SSE 事件流（GET；manager；2.7.4 L530-534）
# ---------------------------------------------------------------------------


@router.get("/streams/{stream_id}")
def stream_events(
    request: Request, stream_id: str, account: Account = Depends(require_manager)
) -> StreamingResponse:
    """SSE 模型与任务事件（2.7.4）。

    - 事件五要素（L530）：id: event_id / event: event_type / data: JSON
      （event_id/event_type/task_id/sequence/payload）；
    - 断线重连携带 Last-Event-ID（L531）：StreamStore 按事件 ID 续传，
      重复 sequence 被跳过；
    - SESSION_EXPIRED（L534）：流被标记过期时在事件末尾追加；
    - 流不存在 → 404 STREAM_NOT_FOUND（稳定错误码）。
    """
    streams = get_stream_store(request)
    # [thread 归属校验] stream 归属他人线程 → 404（复用 STREAM_NOT_FOUND
    # 错误体，不泄露流存在性）。反向映射未登记（owner=None）→ 放行：生产
    # 全部 create/append 调用点都传了 thread_id，此分支不可达，只是保守默认。
    owner = streams.thread_id_of(stream_id)
    if owner is not None and _thread_owned_by_other(request, account, owner):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.STREAM_NOT_FOUND.value,
                "message": "事件流不存在或已过期。",
            },
        )
    try:
        after = request.headers.get("last-event-id")
        events = streams.events(stream_id, after_event_id=after)
        if streams.is_expired(stream_id):
            seq = _next_seq(events)
            expired_event = ChatStreamEvent(
                event_id=f"{stream_id}-e{seq:04d}",
                event_type=EVENT_SESSION_EXPIRED,
                task_id="",
                sequence=seq,
                payload={"reason": "session_expired"},
            )
            events = [*events, expired_event]
    except StreamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.STREAM_NOT_FOUND.value,
                "message": "事件流不存在或已过期。",
            },
        ) from None
    return StreamingResponse(
        _sse_lines(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _next_seq(events: list[ChatStreamEvent]) -> int:
    return (events[-1].sequence + 1) if events else 1


def _sse_lines(events: list[ChatStreamEvent]):
    """SSE 文本行生成器：id/event/data 三段 + 空行分隔（2.7.4 L530）。"""
    for event in events:
        yield (
            f"id: {event.event_id}\n"
            f"event: {event.event_type}\n"
            f"data: {json.dumps(_event_data(event), ensure_ascii=False)}\n\n"
        )


def _event_data(event: ChatStreamEvent) -> dict[str, Any]:
    """data JSON：五要素全量（L530；payload 为嵌套 JSON）。"""
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "task_id": event.task_id,
        "sequence": event.sequence,
        "payload": event.payload,
    }


# ---------------------------------------------------------------------------
# 取消当前生成（写 API：manager + CSRF；2.7.3 L506）
# ---------------------------------------------------------------------------


@router.post(
    "/streams/{stream_id}/cancel",
    response_model=MessageResponse,
    responses={404: {"description": "STREAM_NOT_FOUND"}},
)
def cancel_stream(
    request: Request,
    stream_id: str,
    account: Account = Depends(require_manager),
) -> MessageResponse:
    """取消当前生成（L506）。

    以 ``task_cancelled`` 事件收尾并停止流产出——只影响生成过程，
    **不等于**取消已执行的写操作（写操作取消走 POST /tasks/:taskId/cancel
    与 8.1 状态机）。
    """
    check_csrf(request)
    streams = get_stream_store(request)
    # [thread 归属校验] stream 归属他人线程 → 404（同 STREAM_NOT_FOUND 错误
    # 体）。owner=None（未登记）→ 放行（保守默认，见 stream_events）。
    owner = streams.thread_id_of(stream_id)
    if owner is not None and _thread_owned_by_other(request, account, owner):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.STREAM_NOT_FOUND.value,
                "message": "事件流不存在或已过期。",
            },
        )
    task_id = ""
    try:
        if streams.exists(stream_id):
            existing = streams.events(stream_id)
            if existing:
                task_id = existing[-1].task_id or ""
        streams.cancel(stream_id, task_id=task_id)
    except StreamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.STREAM_NOT_FOUND.value,
                "message": "事件流不存在或已过期。",
            },
        ) from None
    return MessageResponse(message="已取消当前生成。")


# ---------------------------------------------------------------------------
# [2026-08-24 harness 改造] Phase 2：轻确认恢复（POST /api/v1/chat/confirm）
# ---------------------------------------------------------------------------


@router.post(
    "/confirm",
    response_model=ChatConfirmResponse,
    responses={
        404: {"description": "TASK_NOT_FOUND"},
        409: {"description": "CONFIRM_NOT_PENDING / TASK_INVALID_STATE / THREAD_BUSY"},
        503: {"description": "SERVICE_UNAVAILABLE"},
    },
)
def confirm_write(
    request: Request,
    body: ChatConfirmRequest,
    account: Account = Depends(require_manager),
) -> ChatConfirmResponse:
    """轻确认决策（Phase 2 对话式 harness；写 API：manager + CSRF）。

    [2026-08-25 POST 快速返回改造] 与 /chat/messages 同款后台化：前置
    校验（fail closed，同步）+ 流/图装配在 handler 完成，图恢复执行
    移交后台线程（``_resume_thread_background``），**立即返回**
    ``ChatConfirmResponse{stream_id}``（恢复很快——无模型调用或很快，
    确认按钮响应即时）。

    恢复挂起的确认流：``Command(resume={"approved", "payload_hash"})``
    驱动挂起图从 confirm 节点继续（2.7.3 轻确认 + 8.5 checkpoint 恢复）：
    - payload_hash 与挂起卡片一致且 approved=True → 任务回 running，
      真实 Executor 执行（载荷 = 已批准的卡片，7.3）；
    - payload_hash 失效（7.3 内容变化）→ 图重新 interrupt：客户端从
      返回的流读到 re_requested 事件（重新确认）；
    - approved=False → 任务 cancelled（8.1），流产出拒绝 tool_result。

    前置校验（fail closed，不消费无效恢复；均在 handler 同步执行）：
    - checkpoint 未接线（app.state.checkpoint_service=None）→ 503
      SERVICE_UNAVAILABLE（chat confirm 不可用，其余能力不受影响）；
    - 线程无挂起确认卡（load_thread 无 pending_confirmation）→ 409
      CONFIRM_NOT_PENDING（无挂起 interrupt 可恢复）；
    - 任务不存在 → 404 TASK_NOT_FOUND；任务不属于该线程/不在
      waiting_confirmation → 409 TASK_INVALID_STATE（二次确认被拒——
      MemorySaver/PostgresSaver 对 resume-no-pause 是静默 no-op，靠
      前置校验防重复消费）；
    - 线程有活动循环（submit/confirm 处理中）→ 409 THREAD_BUSY
      （Phase 3a 并发防护：防同一线程 checkpoint 并发写；锁由后台线程
      finally 释放）。

    事件流：复用线程最新 stream_id（无则新建），恢复产生的图事件
    （confirmation_requested 重发/tool_running/tool_result/agent_reply/
    re_requested）由后台线程实时追加到该流（Phase 3a：agent_reply 经
    门控收敛，终局轮才落流）；前端以 GET streams/{stream_id} 重放
    （2.7.4 断线续传语义）。

    后台异常安全网（图外异常不再 500 CONFIRM_RESUME_FAILED——POST 已
    返回，改经流内终态事件）：agent_error{error="chat_run_failed"}
    终态事件（前端 AGENT_ERROR_TEXT 已含该 code 文案，输入框恢复）；
    cancel 场景只记日志（task_cancelled 终态已由 cancel 端点追加）。
    """
    check_csrf(request)
    # [thread 归属校验] 他人线程 → 404。**不**放进 _prepare_confirm（那样
    # 会在持锁状态下校验）；_prepare_confirm 的任务级校验保持原样。
    _require_thread_access(request, account, body.thread_id)
    _enter_thread(body.thread_id)
    try:
        stream_id, service, streams, flush, graph = _prepare_confirm(
            request, account, body
        )
    except BaseException:
        # 前置校验异常（503/404/409）→ 同步返回，锁即刻释放
        _leave_thread(body.thread_id)
        raise
    try:
        threading.Thread(
            target=_resume_thread_background,
            kwargs={
                "thread_id": body.thread_id,
                "approved": body.approved,
                "payload_hash": body.payload_hash,
                "stream_id": stream_id,
                "service": service,
                "streams": streams,
                "flush": flush,
                "graph": graph,
            },
            daemon=True,
        ).start()
    except Exception:
        # 线程启动异常（不可能但兜底）：锁释放后 500，复用现
        # CONFIRM_RESUME_FAILED 文案
        _leave_thread(body.thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": ErrorCode.CONFIRM_RESUME_FAILED.value,
                "message": "确认恢复执行失败，请稍后重试。",
            },
        ) from None
    return ChatConfirmResponse(stream_id=stream_id)


def resume_thread(
    request: Request,
    account: Account,
    thread_id: str,
    *,
    approved: bool,
    payload_hash: str,
) -> str:
    """恢复挂起确认线程（Phase 2/3a；同步完整实现，行为不变）。

    [2026-08-25 POST 快速返回改造] 本函数自该日起仅供 F-1 兼容端点
    ``confirm_task``（POST /tasks/:taskId/confirm）使用（其 handler 同步
    drain 后才重读任务状态返回，必须保持同步）；/chat/confirm 已后台化
    走 ``_resume_thread_background``（同款图恢复语义 + 异常安全网）。

    [2026-08-24 harness 改造] F-1：旧端点 ``confirm_task``（POST
    /tasks/:taskId/confirm）委托同一 resume 路径——本函数即"同款恢复
    执行"，调用方负责前置校验（挂起确认卡存在且任务 waiting_confirmation、
    线程并发防护），本函数只做：
    - 事件流：线程最新 stream_id（无则新建）→ 恢复产生的图内事件
      （confirmation_requested 重发/tool_running/tool_result/agent_reply/
      re_requested）经 ``_harness_sink`` 门控追加（Phase 3a：agent_reply
      终局轮才落流），返回 stream_id；
    - 图恢复：``Command(resume={"approved", "payload_hash"})`` 驱动挂起
      图从 confirm 节点继续，同步 drain；hash 失效（7.3 内容变化）时图
      重新 interrupt——stream 不 raise（返回部分 updates），200 语义由
      调用方决定（/tasks/:id/confirm 直接 200，客户端从流感知
      re_requested 重新确认）；
    - 图执行异常 → 500 CONFIRM_RESUME_FAILED（不泄露内部细节，14.4）。
    """
    service = _checkpoint_service(request)
    streams = get_stream_store(request)
    stream_id = streams.latest_stream_id(thread_id) or new_stream_id()
    if not streams.exists(stream_id):
        streams.create(stream_id, thread_id=thread_id)
    emit, flush = _harness_sink(streams, stream_id, thread_id)
    runtime = _harness_runtime(request, account, thread_id, stream_id, sink=emit)
    graph = build_graph(checkpointer=service.checkpointer, runtime=runtime)
    try:
        # stream 而非 invoke：hash 失效重新 interrupt 时 stream 不 raise
        # （返回部分 updates），端点正常返回 200，客户端从流感知 re_requested
        interrupted = False
        for chunk in graph.stream(
            Command(resume={"approved": approved, "payload_hash": payload_hash}),
            config=service.new_graph_config(thread_id),
            stream_mode="updates",
        ):
            if "__interrupt__" in chunk:
                interrupted = True
        flush(
            interrupted=interrupted,
            final_step=graph.get_state(service.new_graph_config(thread_id)).values.get("current_step"),
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": ErrorCode.CONFIRM_RESUME_FAILED.value,
                "message": "确认恢复执行失败，请稍后重试。",
            },
        ) from None
    return stream_id


def _prepare_confirm(
    request: Request, account: Account, body: ChatConfirmRequest
) -> tuple[str, Any, StreamStore, Callable[..., None], Any]:
    """confirm_write 同步前置（handler 层；fail closed 校验 + 流/图装配）。

    [2026-08-25 POST 快速返回改造] 原 _run_confirm 的校验与装配全部同步
    保留（错误语义不变：503 / 409 CONFIRM_NOT_PENDING / 404 TASK_NOT_FOUND
    / 409 TASK_INVALID_STATE ×2），图恢复 drain 移交后台线程
    （``_resume_thread_background``）；返回 stream_id 供立即响应。
    """
    service = _checkpoint_service(request)
    restored = service.load_thread(body.thread_id)
    pc = (restored or {}).get("pending_confirmation")
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.CONFIRM_NOT_PENDING.value,
                "message": "该会话线程没有挂起的确认（无待确认卡片）。",
            },
        )
    task_id = pc["task_id"]
    tasks = _tasks(request)
    task = tasks.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.TASK_NOT_FOUND.value,
                "message": "确认任务不存在（可能已被清理）。",
            },
        )
    if task.thread_id != body.thread_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.TASK_INVALID_STATE.value,
                "message": "确认任务不属于该会话线程。",
            },
        )
    if task.status != TaskStatus.WAITING_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.TASK_INVALID_STATE.value,
                "message": f"任务状态为 {task.status.value}，不在等待确认中"
                "（已确认/已取消/已失败均不可重复确认）。",
            },
        )
    streams = get_stream_store(request)
    stream_id = streams.latest_stream_id(body.thread_id) or new_stream_id()
    if not streams.exists(stream_id):
        streams.create(stream_id, thread_id=body.thread_id)
    emit, flush = _harness_sink(streams, stream_id, body.thread_id)
    runtime = _harness_runtime(request, account, body.thread_id, stream_id, sink=emit)
    graph = build_graph(checkpointer=service.checkpointer, runtime=runtime)
    return stream_id, service, streams, flush, graph


def _resume_thread_background(
    thread_id: str,
    *,
    approved: bool,
    payload_hash: str,
    stream_id: str,
    service: Any,
    streams: StreamStore,
    flush: Callable[..., None],
    graph: Any,
) -> None:
    """后台线程恢复挂起图（/chat/confirm 快速返回后的图执行）。

    [2026-08-25 POST 快速返回改造] 与 ``resume_thread``（/tasks/:id/confirm
    F-1 兼容路径，保持同步完整实现）同款图恢复语义：``Command(resume=...)``
    同步 drain + flush，事件实时追加到 StreamStore。异常安全网与 submit
    后台线程一致（``_append_run_failed``）：StreamCancelledError → 只记
    日志（cancel 端点已追加 task_cancelled 终态）；其他异常 →
    agent_error{error="chat_run_failed"} 终态事件（前端一定收到终态、
    输入框恢复）。finally ``_leave_thread`` 释放并发防护锁。
    """
    try:
        try:
            interrupted = False
            for chunk in graph.stream(
                Command(resume={"approved": approved, "payload_hash": payload_hash}),
                config=service.new_graph_config(thread_id),
                stream_mode="updates",
            ):
                if "__interrupt__" in chunk:
                    interrupted = True
            flush(
                interrupted=interrupted,
                final_step=graph.get_state(
                    service.new_graph_config(thread_id)
                ).values.get("current_step"),
            )
        except StreamCancelledError:
            # 与 _run_submit_background 同款：异常路径收敛缓冲，已缓冲的中间轮
            # agent_reply 走影子通道落库（不静默丢弃）；flush 对无缓冲是 no-op。
            flush(interrupted=True)
            logger.info(
                "chat_confirm_stream_cancelled", stream_id=stream_id, thread_id=thread_id
            )
        except Exception:
            flush(interrupted=True)
            logger.exception(
                "chat_confirm_background_failed",
                stream_id=stream_id,
                thread_id=thread_id,
            )
            _append_run_failed(streams, stream_id, thread_id)
    finally:
        _leave_thread(thread_id)
