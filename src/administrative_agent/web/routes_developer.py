"""developer 端点（规格 2.7.3 L520-521；10.5/10.6/11.4/12.4；模块树
L216 routes_developer.py）。

- GET /api/v1/developer/health  健康与依赖状态（MCP/DB/会话配置状态，
  无业务数据、无凭据）
- GET /api/v1/developer/logs    脱敏技术日志（环形缓冲 + redaction +
  长度限制）

与 main.py 的 GET /api/v1/health（无鉴权运维探活）并存：后者只报告进程
健康，不暴露依赖细节；本端点 developer 鉴权，报告依赖状态。

权限（10.5）：developer 只允许健康检查、部署和脱敏技术日志接口——
业务 API（chat/tasks/reviews/audit/mappings）一律 require_manager，
developer 访问返回 403（require_manager 已实现）；manager 访问本端点
403（require_developer）。拒绝响应只说明所需权限与当前无权限，不透露
内部认证细节（L1186）。

日志边界（11.4）：本端点只读取进程内结构化日志缓冲，不保存完整对话/
模型思考；输出经 redaction.py（11.2 规则 4）+ 凭证形态探测 + 字符串
长度限制（10.6 L1193）。
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from typing import Any, Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker

from administrative_agent.auth.dependencies import require_developer
from administrative_agent.auth.models import Account
from administrative_agent.config import settings
from administrative_agent.observability.log_capture import (
    VALID_LOG_LEVELS,
    LogRing,
    read_logs,
)
from administrative_agent.web.schemas import (
    DependencyStatus,
    DeveloperHealthResponse,
    DeveloperLogsResponse,
    ErrorCode,
    LogEntryOut,
    StreamEventSinkStatus,
    StreamEventThreadsResponse,
    StreamEventTimelineResponse,
)
from administrative_agent.web.stream_event_reader import list_threads, read_thread

router = APIRouter(prefix="/api/v1/developer", tags=["developer"])

#: 日志分页上限（与列表类端点一致：最大 200）
MAX_LOGS_LIMIT = 200

#: 会话列表上限（列表是纯聚合，组数 = 保留期内会话数）
MAX_THREADS_LIMIT = 200
#: 时间线单页上限——**刻意远高于常规的 200**：一次对话数百行分片，200 的上限
#: 会让「一页全是分片」，折叠后仍然只有一行，端点失去意义。
STREAM_TIMELINE_LIMIT = 2000
#: 时间线默认页大小（500 行 ≈ 100 KB）；2000 由前端在长会话时显式请求。
DEFAULT_TIMELINE_LIMIT = 500

APP_NAME = "hengfa-administrative-agent"


def get_log_ring(request: Request) -> LogRing:
    """日志缓冲依赖：优先 app.state（测试注入空 ring），默认模块 ring。"""
    from administrative_agent.observability import log_capture

    buffer = getattr(request.app.state, "log_ring", None)
    if buffer is None:
        buffer = log_capture.ring
    return buffer


def _dependency(name: str, status: str, detail: str | None = None) -> DependencyStatus:
    return DependencyStatus(name=name, status=status, detail=detail)


def _db_status(database_url: str, name: str) -> DependencyStatus:
    """数据库依赖状态：只报告引擎与主机（绝不输出 URL 凭据，12.4）。"""
    if not database_url:
        return _dependency(name, "not_configured")
    try:
        url = make_url(database_url)
    except Exception:
        return _dependency(name, "invalid")
    parts = [url.drivername or "unknown"]
    if url.host:
        parts.append(url.host)
    if url.port:
        parts.append(str(url.port))
    return _dependency(name, "configured", detail=" ".join(parts))


def _erp_mcp_status() -> DependencyStatus:
    """ERP MCP 依赖状态：URL/Key 均配置才可用（12.4：未配置 fail closed）。"""
    if not settings.erp_mcp_url:
        return _dependency("erp_mcp", "not_configured")
    host = None
    try:
        url = make_url(settings.erp_mcp_url)
        if url.host:
            host = url.host
    except Exception:
        pass
    if not settings.erp_mcp_api_key:
        return _dependency(
            "erp_mcp", "missing_credentials", detail=f"host={host}" if host else None
        )
    return _dependency("erp_mcp", "configured", detail=f"host={host}" if host else None)


def _session_status() -> DependencyStatus:
    """会话签名密钥状态（12.4：SESSION_SECRET 未配置时登录 fail closed）。"""
    if settings.session_secret:
        return _dependency("session", "configured")
    return _dependency("session", "not_configured")


def _stream_event_status(request: Request) -> StreamEventSinkStatus | None:
    """SSE 事件落库 sink 只读状态（可观测性第 0 步）。

    解析顺序：``app.state.stream_event_sink``（生产 lifespan 注入 / 测试注入）
    → ``app.state.chat_streams`` 的 ``sink`` → ``None``（未接线，返回 ``null``
    以区分「压根没接线」与「接线了但队列为 0」）。**读数失败一律 ``None``——
    health 端点必须永远 200**（这是开发者面板 60 s 轮询的探活端点）。

    ⚠️ **与下面两个 ``/stream-events/*`` 数据查询端点刻意相反**：那两个
    **不吞异常**（DB 故障吞成 ``items: []`` 等于用「没有数据」骗人，见其
    docstring）。**不要**为了「统一风格」把其中一处改掉。
    """
    try:
        sink = getattr(request.app.state, "stream_event_sink", None)
        if sink is None:
            store = getattr(request.app.state, "chat_streams", None)
            sink = getattr(store, "sink", None)
        if sink is None:
            return None
        stats = sink.stats()
        return StreamEventSinkStatus(
            enabled=stats.enabled,
            queued=stats.queued,
            written=stats.written,
            dropped=stats.dropped,
            write_failures=stats.write_failures,
            unmapped_events=stats.unmapped_events,
            oversize_dropped=stats.oversize_dropped,
            pruned_rows=stats.pruned_rows,
            last_write_age_seconds=stats.last_write_age_seconds,
            degraded_reason=stats.degraded_reason,
            last_error_kind=stats.last_error_kind,
        )
    except BaseException:  # noqa: BLE001 —— health 端点必须永远 200
        return None


# ---------------------------------------------------------------------------
# 健康与依赖状态（GET；developer；无业务数据）
# ---------------------------------------------------------------------------


@router.get("/health", response_model=DeveloperHealthResponse)
def developer_health(
    request: Request,
    _: Account = Depends(require_developer),
) -> DeveloperHealthResponse:
    """健康与依赖状态（2.7.3 L520）。

    只报告依赖配置状态（MCP/Agent 库/Checkpoint 库/会话密钥），
    不含任何业务数据与凭据（12.4 L1305）。
    """
    try:
        import importlib.metadata as md

        version = md.version(APP_NAME)
    except md.PackageNotFoundError:
        version = "0.0.0"
    return DeveloperHealthResponse(
        status="ok",
        app=APP_NAME,
        version=version,
        log_level=settings.log_level,
        dependencies=[
            _erp_mcp_status(),
            _db_status(settings.agent_database_url, "agent_database"),
            _db_status(settings.checkpoint_database_url, "checkpoint_database"),
            _session_status(),
        ],
        stream_events=_stream_event_status(request),
    )


# ---------------------------------------------------------------------------
# 脱敏技术日志（GET；developer；10.6/11.2/11.4）
# ---------------------------------------------------------------------------


@router.get("/logs", response_model=DeveloperLogsResponse)
def developer_logs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=MAX_LOGS_LIMIT),
    level: str | None = Query(default=None),
    _: Account = Depends(require_developer),
) -> DeveloperLogsResponse:
    """脱敏技术日志（2.7.3 L521；最新在前）。

    - 输出前二次脱敏：敏感键（password/api_key/token/secret/密码/密钥…）
      redaction 占位 + 凭证形态探测（sk-/AKIA/JWT 等）+ 字符串长度限制
      （10.6 L1193，单字段上限 1000）；
    - 不保存完整对话/模型思考（11.4）；
    - level 过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL），非法值 422。
    """
    if level is not None and level.upper() not in VALID_LOG_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": ErrorCode.VALIDATION_ERROR.value,
                "message": f"非法日志级别过滤值：{level!r}。",
                "details": None,
            },
        )
    records, total = read_logs(get_log_ring(request), limit=limit, level=level)
    items = [
        LogEntryOut(
            timestamp=r.get("timestamp"),
            level=r.get("level"),
            event=r.get("event"),
            logger=r.get("logger"),
            payload={k: v for k, v in r.items() if k not in ("timestamp", "level", "event", "logger")},
        )
        for r in records
    ]
    return DeveloperLogsResponse(items=items, total=total, limit=limit)


# ---------------------------------------------------------------------------
# 任务时间线（GET；developer；可观测性第 1 步：只读 chat_stream_events）
# ---------------------------------------------------------------------------


@contextmanager
def _read_scope(factory: Any) -> Iterator[Any]:
    """读事务：只读、不提交，会话必关（写入走 sink，本端点零写路径）。"""
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _event_scope(request: Request) -> Callable[[], Any]:
    """事件表会话 scope（``web/stream_event_reader`` 的注入点）。

    ⚠️ **绝不回退 ``app.state.session_factory``**：测试里它指向**主 SQLite
    库**，那张 ``chat_stream_events`` 由 ``Base.metadata.create_all`` 建出却
    **是空表**——查它会静默返回空结果，把「读错库」伪装成「没有数据」
    （正是刚吃过的假绿模式）。

    正确解析顺序：**``app.state.stream_event_engine`` 优先**（测试注入，就是
    写入端那个 engine），否则**函数内**延迟 import ``SessionLocal``——
    ``persistence.database`` 的 engine 在 import 期构建，无 ``AGENT_DATABASE_URL``
    时 import 即抛；且生产不注入 ``stream_event_engine``（``main.py`` 只 wire
    sink），故生产走 ``SessionLocal``，与 sink 同源于 ``agent_database_url``，
    指向同一个库。
    """
    engine = getattr(request.app.state, "stream_event_engine", None)
    if engine is not None:
        factory: Any = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False
        )
    else:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    # partial 保证每次调用产出**新的**上下文管理器（同一 generator 只能进一次）
    return partial(_read_scope, factory)


@router.get(
    "/stream-events/threads", response_model=StreamEventThreadsResponse
)
def developer_stream_event_threads(
    request: Request,
    limit: int = Query(default=50, ge=1, le=MAX_THREADS_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Account = Depends(require_developer),
) -> StreamEventThreadsResponse:
    """最近会话列表（可观测性第 1 步）。

    纯聚合：**不显示对话标题、不关联会话表**（``title`` 是用户首条消息原文、
    ``account`` 是身份数据，都超出「只看骨架」口径）。按 ``last_seen`` 倒序、
    ``thread_id`` 升序（次级键保证确定性）。

    ⚠️ **读失败不吞异常**（与 ``_stream_event_status`` 刻意相反）：数据查询
    端点把 DB 故障吞成 ``items: []`` 等于**用「没有数据」骗人**。异常直接
    向上抛 → 500，前端 ``ErrorNotice`` 呈现。**不会回显 ``str(exc)``**：
    ``main.py`` 未注册兜底 ``Exception`` 处理器，且 ``FastAPI(...)`` 未传
    ``debug``（默认 ``False``）→ Starlette 返回固定文案，无需脱敏代码。
    """
    items, total, untracked = list_threads(
        _event_scope(request), limit=limit, offset=offset
    )
    return StreamEventThreadsResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        untracked_events=untracked,
    )


@router.get(
    "/stream-events/threads/{thread_id}",
    response_model=StreamEventTimelineResponse,
)
def developer_stream_event_timeline(
    request: Request,
    thread_id: str = Path(min_length=1, max_length=128),
    limit: int = Query(default=DEFAULT_TIMELINE_LIMIT, ge=1, le=STREAM_TIMELINE_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Account = Depends(require_developer),
) -> StreamEventTimelineResponse:
    """某会话的完整事件流水（一条事件一行，不聚合）。

    顺序 **ASC**（时间线语义是从头读）；「跳到最新」由前端按钮补足（长会话的
    失败点通常在尾部）。未知 / 已过保留期的 ``thread_id`` → 200 + 空列表 +
    ``total: 0``，**不 404**：本端点不 join 会话表，这两种情况在数据上
    不可区分，给 404 等于承诺一个我们给不出的区分。

    ⚠️ **读失败不吞异常**——理由同 ``developer_stream_event_threads``
    （**不要**为了「统一风格」把它改成永远 200）。
    """
    return read_thread(
        _event_scope(request), thread_id, limit=limit, offset=offset
    )
