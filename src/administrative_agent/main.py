"""FastAPI 入口、lifespan 生命周期与运维探活端点。

- GET /api/v1/health：运维探活（不鉴权；D23 的 developer/health 不属于本端点）。
- auth 路由（D4）：登录/注销/me（2.7.3）。
- 统一错误响应：error_code、message、details、trace_id（2.7.3 稳定契约）。
- CORS：FRONTEND_ORIGIN 白名单（web/cors.py）。

规格 15.3 步骤 1 的完整启动检查（MCP 健康、Alembic、白名单校验）在对应单元落地，
此处 lifespan 只做配置加载与 structlog 初始化，不承载任何业务逻辑。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any, AsyncIterator

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from administrative_agent import lifecycle
from administrative_agent.auth.service import (
    dropped_duplicate_accounts,
    dropped_incomplete_accounts,
)
from administrative_agent.agent.checkpoint import (
    CheckpointService,
    CheckpointUnconfiguredError,
    checkpointer_factory,
)
from administrative_agent.config import SENSITIVE_SETTINGS, settings
from administrative_agent.domains.attachments import (
    AttachmentStore,
    cleanup_expired_files,
)
from administrative_agent.domains.export import cleanup_expired_exports
from administrative_agent.domains.reconcile_reports import ReconcileReportStore
from administrative_agent.mcp.client import MCPClient, build_rag_mcp_client
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.observability.log_capture import (
    capture_processor,
    ring,
    sanitize_processor,
)
from administrative_agent.web.cors import install_cors
from administrative_agent.web.routes_attachments import router as attachments_router
from administrative_agent.web.routes_audit import router as audit_router
from administrative_agent.web.routes_auth import router as auth_router
from administrative_agent.web.routes_chat import router as chat_router
from administrative_agent.web.routes_developer import router as developer_router
from administrative_agent.web.routes_export import router as export_router
from administrative_agent.web.routes_mappings import router as mappings_router
from administrative_agent.web.routes_reconcile import router as reconcile_router
from administrative_agent.web.routes_reviews import router as reviews_router
from administrative_agent.web.routes_tasks import router as tasks_router
from administrative_agent.web.schemas import ErrorCode, make_error_response

APP_NAME = "hengfa-administrative-agent"

try:
    APP_VERSION = metadata.version(APP_NAME)
except metadata.PackageNotFoundError:  # 源码树直跑（未安装）时退化为占位
    APP_VERSION = "0.0.0"


def _configure_logging(level: str) -> None:
    """最小 structlog JSON 配置（规格 0.3；D23 增加日志环形缓冲）。

    捕获处理器（capture_processor）在时间戳/级别追加后记录事件 dict 副本
    到有界缓冲（developer/logs 数据源，observability/log_capture.py）；
    缓冲只存结构化键值，不保存完整对话/模型思考（11.4）。
    """
    logging.basicConfig(level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            sanitize_processor,            # 写缓冲前脱敏（环形缓冲只存脱敏数据）
            capture_processor(ring),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            sanitize_processor,            # 输出前再脱敏（扫掉 traceback 字符串中的密钥）
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level.upper()),
        cache_logger_on_first_use=True,
    )


def _wire_checkpoint(app: FastAPI) -> None:
    """[2026-08-24 harness 改造] Phase 2：接线 CheckpointService（chat confirm
    的挂起图持久层，8.5 定位升级为对话历史持久层）。

    两条路径的明确设计（测试环境与生产环境）：
    - **测试/已注入**：``app.state.checkpoint_service`` 已存在（tests/api
      conftest 的 autouse 夹具注入 CheckpointService(MemorySaver())）→
      直接跳过——测试内绝不建立真实 Postgres 连接、不执行建表；
    - **生产**：``checkpointer_factory()`` 按环境变量连接串构建
      PostgresSaver（CHECKPOINT_DATABASE_URL → AGENT_DATABASE_URL，
      12.4/15.2 连接串只走环境变量）：
      * 连接串未配置（CheckpointUnconfiguredError）→
        ``app.state.checkpoint_service = None``：/chat/confirm 返回 503
        （chat confirm 不可用），服务其余能力不受影响——不降级为内存
        checkpoint（否则重启后恢复语义被破坏）；
      * 基础设施失败（连接池创建失败/建表失败）→ 异常传播（fail closed：
        checkpoint 是 harness 核心持久层，启动失败要暴露，与 worker.py
        启动检查同口径）。
    """
    if getattr(app.state, "checkpoint_service", None) is not None:
        return
    log = structlog.get_logger("administrative_agent")
    try:
        saver = checkpointer_factory()
        service = CheckpointService(saver, task_repository=None)
    except CheckpointUnconfiguredError:
        # 未配置连接串：chat confirm 不可用（503），不阻断服务其余能力
        log.warning("checkpoint_unconfigured", app=APP_NAME)
        app.state.checkpoint_service = None
        return
    service.setup()
    app.state.checkpoint_service = service
    log.info("checkpoint_wired", app=APP_NAME)


def _wire_mcp_client(app: FastAPI) -> None:
    """[2026-08-24 harness 改造] Phase 3c：接线 ERP MCP 客户端（写链路依赖）。

    真实验证发现（docs/review/RV-harness-run-2026-08-24.md 问题 1，阻断级）：
    lifespan 此前只接 checkpoint，从未赋值 ``app.state.mcp_client``，而
    routes_chat._harness_runtime 经 ``getattr(request.app.state, "mcp_client",
    None)`` 读取且无回退 → 生产部署下所有写工具（attendance_plan_execute/
    order_create/order_update/reconciliation_execute）在 tools 节点确定性
    ``harness_misconfigured``（graph.py 门控，fail closed）。本函数按
    ``_wire_checkpoint`` 同模式补接线，两条路径：

    - **测试/已注入**：``app.state.mcp_client`` 已存在（tests/api conftest
      的 ``mcp`` fixture 注入 FakeMCPClient）→ 直接跳过——测试内绝不建立
      真实 MCPClient；幂等检查与 fixture 注入时序无关，保证不覆盖注入；
    - **生产**：``settings.erp_mcp_url`` 非空 → ``MCPClient(settings)``
      （mcp/client.py：构造是懒的——只建 httpx.Client，不发任何网络请求，
      可安全在 lifespan 构造；会话/认证在首次调用时建立）＋ 同生命周期
      接线 ``app.state.mcp_metadata = EntityCache(...)``（AttendanceExecutor
      契约校验依赖，见 routes_chat._mcp_metadata 的补接线说明）；url 为空 →
      ``app.state.mcp_client = None``（mcp_metadata 一并置 None）+ 告警——
      写工具 fail closed（与 developer 状态页 not_configured 语义一致，
      不降级为假客户端）。

    收尾（lifespan shutdown 段）：duck-typing 探测 ``close`` 方法，存在则
    best-effort 调用（MCPClient.close 幂等；FakeMCPClient 无 close 方法
    自然跳过；EntityCache 无 close，无需处理）。
    """
    if getattr(app.state, "mcp_client", None) is not None:
        return
    log = structlog.get_logger("administrative_agent")
    url = settings.erp_mcp_url.strip()
    if not url:
        # 未配置 ERP MCP：写工具 fail closed（不降级为假客户端）
        log.warning("mcp_unconfigured", app=APP_NAME)
        app.state.mcp_client = None
        app.state.mcp_metadata = None
        return
    app.state.mcp_client = MCPClient(settings)
    # 实体元数据缓存与客户端同生命周期（AttendanceExecutor 契约校验
    # ``ensure_write_allowed``/``_assert_contract`` 依赖；TTL 走配置）
    app.state.mcp_metadata = EntityCache(
        app.state.mcp_client, ttl_seconds=settings.erp_mcp_metadata_ttl
    )
    log.info("mcp_wired", app=APP_NAME, endpoint=url)


def _wire_rag_client(app: FastAPI) -> None:
    """[2026-09-22 RAG 长期记忆接入] 接线 RAG 长期记忆 MCP 客户端（第二个实例）。

    与 ``_wire_mcp_client`` 同模式，两条路径：
    - **测试/已注入**：``app.state.rag_client`` 已存在 → 直接跳过（测试注入假
      客户端；幂等检查与注入时序无关）；
    - **生产**：``settings.rag_mcp_url`` **与 ``settings.rag_mcp_api_key``
      均非空** → ``build_rag_mcp_client(settings)``（复用 mcp/client.py 的 MCP
      协议实现，只是换端点与凭据）；任一为空 → ``app.state.rag_client = None``
      + 一条 ``rag_mcp_unconfigured`` 告警——装配层据 ``rag_client is None`` 在
      ``routes_chat._harness_runtime`` 把 RAG 侧全部工具（rag_remember/
      rag_recall/rag_forget/rag_search_documents）
      **从运行时工具集剔除**（模型看不见，不会反复调用必然失败的工具），agent
      其余能力照常工作。RAG **不是**启动依赖，未配置不影响启动。

    [F4，2026-09-22] 判据从「仅 url 非空」收紧为「url **且** key 非空」：只配
    url 漏 key 时 ``build_key_headers("")`` 每次调用返回 CREDENTIALS_NOT_CONFIGURED，
    但 rag_client 非 None → 三个工具仍进模型可见集，与该批自称的「未配置就不
    暴露」自相矛盾（模型会反复调一个必然失败的工具）。**只收紧 RAG 侧**：
    ERP 侧 ``_wire_mcp_client`` 仍只看 url（ERP 认证还有 JWT 回退通道，
    erp_mcp_api_key 可空而 jwt 有效，收紧会误伤合法部署；RAG 只有 X-API-Key
    一条认证路径，无此歧义）。

    收尾（lifespan shutdown 段）：与 ERP 客户端同类，duck-typing 探测 ``close``
    best-effort 关闭（rag 是只读为主的记忆服务，无在飞写调用要 drain）。
    """
    if getattr(app.state, "rag_client", None) is not None:
        return
    log = structlog.get_logger("administrative_agent")
    url = settings.rag_mcp_url.strip()
    key = settings.rag_mcp_api_key.strip()
    if not url or not key:
        # url 缺 key（半配置）同样按「未配置」处理：不接线、不暴露工具
        log.warning(
            "rag_mcp_unconfigured",
            app=APP_NAME,
            url_present=bool(url),
            key_present=bool(key),
        )
        app.state.rag_client = None
        return
    app.state.rag_client = build_rag_mcp_client(settings)
    log.info("rag_mcp_wired", app=APP_NAME, endpoint=url)


def _session_factory_for(app: FastAPI) -> Any:
    """lifespan 侧会话工厂：优先 app.state.session_factory（测试注入
    SQLite 工厂），缺省 SessionLocal（生产；与 web 路由 _session_factory
    同口径，仅缺少 Request）。"""
    factory = getattr(app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _cleanup_expired_records(app: FastAPI) -> None:
    """启动时清理过期附件与对账报告（TTL 机制；best effort）。

    - 附件：元数据行 + storage_path 磁盘文件一并删（cleanup_expired_files
      域内自兜底）；对账报告：只删元数据行（无磁盘文件）；
    - 任一失败只 warning 不阻断启动——清理是运维动作，与 15.3 启动检查
      （fail closed）分开；测试未注入 session_factory 时缺省 SessionLocal
      可能连不上库，同样被兜底吞掉。
    """
    log = structlog.get_logger("administrative_agent")
    try:
        factory = _session_factory_for(app)
    except Exception:
        # 连接串未配置（SessionLocal 构造即 RuntimeError）等场景：
        # 清理不可用 → 只告警，绝不阻断启动
        log.warning("expired_cleanup_unavailable", exc_info=True)
        return
    cleanup_expired_files(AttachmentStore(factory), settings.attachment_dir)
    try:
        ReconcileReportStore(factory).delete_expired()
    except Exception:
        log.warning("report_delete_expired_failed", exc_info=True)
    # [2026-09-01 导出功能批次] 启动清一次过期导出文件（meta.json + xlsx；
    # 域内 self-contained，不依赖会话工厂——export_dir 未配置时为空转）
    try:
        cleanup_expired_exports(settings.export_dir)
    except Exception:
        log.warning("export_delete_expired_failed", exc_info=True)
    # [2026-09-19 可观测性第 0 步] 启动清一次过期 SSE 事件行（14 天窗口；
    # 跨进程重建后遗留的过期行靠它收敛——正是本步要解决的场景）。与上面
    # 同款 best effort：失败只 warning，绝不阻断启动。分批 DELETE 见实现。
    try:
        from administrative_agent.web.stream_persistence import (
            cleanup_expired_stream_events,
        )

        cleanup_expired_stream_events(
            factory, ttl_seconds=settings.stream_event_ttl_seconds
        )
    except Exception:
        log.warning("stream_events_cleanup_failed", exc_info=True)


def _wire_stream_event_sink(app: FastAPI) -> None:
    """接线 SSE 事件落库 sink（可观测性第 0 步）。

    测试注入路径：``app.state.stream_event_sink`` 已存在则跳过（同
    ``_wire_checkpoint``/``_wire_mcp_client`` 约定）。生产：模块单例
    ``default_sink()``——与 ``web/routes_chat._DEFAULT_STREAMS`` 共用同一
    实例（惰性构造，只在首次 ``submit`` 时启动后台线程）。
    """
    if getattr(app.state, "stream_event_sink", None) is not None:
        return
    from administrative_agent.web.stream_persistence import default_sink

    app.state.stream_event_sink = default_sink()


def _shutdown_stream_sink(app: FastAPI, *, timeout: float) -> None:
    """停机写尽事件流 sink（15.3 步骤 7 收尾；可观测性第 0 步）。

    置 ``_closed``（此后 ``submit`` 变 no-op）→ 分多批写尽 → ``join(timeout)``；
    超时 abandon 由 sink 内部打一条含丢弃条数的 warning。**绝不阻塞停机**：
    未接线（``app.state`` 无 sink 或已为 None）→ 空转；任何异常不外抛。
    """
    sink = getattr(app.state, "stream_event_sink", None)
    if sink is None:
        return
    try:
        sink.shutdown(timeout)
    except BaseException:  # noqa: BLE001 —— 收尾路径绝不阻断停机
        structlog.get_logger("administrative_agent").warning(
            "stream_event_sink_shutdown_failed", app=APP_NAME, exc_info=True
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging(settings.log_level)
    log = structlog.get_logger("administrative_agent")
    # 进程启动即复位停机状态（lifecycle.py）：停机标志与计数不跨进程存活，
    # 重启后新写任务恢复接收（15.3 步骤 7 与"重启恢复"语义）
    lifecycle.reset()

    # [2026-08-24 harness 改造] Phase 2：接线 checkpoint 持久层
    # （/chat/confirm 挂起图恢复；测试注入路径见函数 docstring）
    _wire_checkpoint(app)
    # [2026-08-24 harness 改造] Phase 3c：接线 ERP MCP 客户端（写链路；
    # 真实验证发现补接线，测试注入路径见函数 docstring）
    _wire_mcp_client(app)
    # [2026-09-22 RAG 长期记忆接入] 接线 RAG 长期记忆 MCP 客户端（未配置则
    # 置 None + 告警，装配层据此不向模型暴露 rag_* 三工具）
    _wire_rag_client(app)
    # [2026-09-19 可观测性第 0 步] 接线 SSE 事件落库 sink（惰性；无 DB 时
    # enabled=False 只打一次 info）
    _wire_stream_event_sink(app)
    # [2026-08-31 对话对账] 启动清一次过期附件/对账报告（TTL 兜底：
    # 跨进程重启后遗留的过期行与孤儿文件在此收敛；best effort）
    _cleanup_expired_records(app)

    # 凭据重名（manager_accounts 列表内 / manager 与 developer 交叉同名）→
    # 启动期以 error 暴露：语义是「首个生效、后者丢弃」（auth/service.py 的
    # tie-break），不阻断启动（一次配置笔误不得锁死整机），但运维要先于用户
    # 看到。只报账号名，绝不带口令（dropped_duplicate_accounts 只返回账号名）。
    duplicate_accounts = dropped_duplicate_accounts()
    if duplicate_accounts:
        log.error(
            "duplicate_credential_accounts_dropped",
            app=APP_NAME,
            accounts=duplicate_accounts,
        )

    # 半配条目（account 或 password 为空串）会被 auth/service.py 整条丢弃。归并为
    # JSON 列表后仅凭 is_configured 不够（列表"非空"但不含任何生效凭据），故在此
    # 并列上报：只报账号名/索引（account 为空串者用 index=<i> 标识），绝不带口令；
    # 语义与重名告警同口径——不阻断启动，但运维要先于用户看到。
    # 覆盖面比旧编号键更宽：旧方案下只有主 manager 对（MANAGER_ACCOUNT/
    # MANAGER_PASSWORD）在 SENSITIVE_SETTINGS 内，第二个 manager（MANAGER_ACCOUNT_2/
    # MANAGER_PASSWORD_2）半配从不进该清单、完全静默。半配属配置缺陷，error 级、只报
    # 账号名/索引、不阻断启动——漏报代价远大于误报（有意取舍）。
    incomplete_entries = dropped_incomplete_accounts()
    if incomplete_entries:
        log.error(
            "incomplete_credential_entries_dropped",
            app=APP_NAME,
            entries=incomplete_entries,
        )

    missing = [name for name in SENSITIVE_SETTINGS if not settings.is_configured(name)]
    if missing:
        # 仅告警，不阻断启动：本地开发 / 测试没有 .env 属正常；
        # 依赖这些凭据的能力在各自单元内 fail closed。
        log.warning(
            "sensitive_settings_missing",
            app=APP_NAME,
            version=APP_VERSION,
            missing=missing,
        )
    else:
        log.info("sensitive_settings_loaded", app=APP_NAME, version=APP_VERSION)

    log.info("app_starting", app=APP_NAME, version=APP_VERSION)
    yield
    # 15.3 步骤 7：先停止接收新写任务 → 等待当前调用落审计或超时 → 再停机。
    # 新写任务入口（web/routes_chat.py）检查 accepting_new_writes()；
    # 进行中写调用计数归零由 drain_writes 等待（lifecycle.py）。
    lifecycle.begin_shutdown()
    started = time.monotonic()
    drained = lifecycle.drain_writes(settings.shutdown_drain_timeout)
    log.info(
        "app_stopping",
        app=APP_NAME,
        version=APP_VERSION,
        drained=drained,
        inflight=lifecycle.inflight_count(),
        drain_timeout_seconds=settings.shutdown_drain_timeout,
        drain_elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    # [2026-09-19 可观测性第 0 步] 停机写尽事件流 sink（置 _closed →
    # 分多批写尽 → join；超时 abandon + sink 内部 warning；绝不阻塞停机）
    _shutdown_stream_sink(app, timeout=settings.stream_event_drain_timeout)
    # [2026-08-24 harness 改造] Phase 3c 收尾：关闭 ERP MCP 客户端会话
    # （drain 之后已无进行中 MCP 调用；best-effort + duck-typing——测试
    # 注入的 FakeMCPClient 无 close 方法自然跳过，生产 MCPClient.close
    # 幂等）
    mcp_client = getattr(app.state, "mcp_client", None)
    mcp_close = getattr(mcp_client, "close", None)
    if callable(mcp_close):
        try:
            mcp_close()
        except Exception:  # noqa: BLE001 —— 收尾路径不阻断停机
            log.warning("mcp_client_close_failed", app=APP_NAME)
    # [2026-09-22 RAG 长期记忆接入] 同款 best-effort 关闭 RAG MCP 客户端会话
    rag_client = getattr(app.state, "rag_client", None)
    rag_close = getattr(rag_client, "close", None)
    if callable(rag_close):
        try:
            rag_close()
        except Exception:  # noqa: BLE001 —— 收尾路径不阻断停机
            log.warning("rag_client_close_failed", app=APP_NAME)


app = FastAPI(
    title="恒发 Agent",
    description="恒发 ERP 自然语言数据操作员（一期 R1-R6）",
    version=APP_VERSION,
    lifespan=lifespan,
)

# 独立前端来源白名单（Cookie 会话需要 allow_credentials 与显式白名单）
install_cors(app)

app.include_router(auth_router)
app.include_router(mappings_router)
app.include_router(reviews_router)
app.include_router(chat_router)
app.include_router(attachments_router)
app.include_router(export_router)
app.include_router(reconcile_router)
app.include_router(tasks_router)
app.include_router(audit_router)
app.include_router(developer_router)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """统一错误响应（2.7.3）：error_code/message/details/trace_id。

    业务异常通过 detail 携带 error_code；其余 HTTPException 映射为通用码。
    响应不包含任何内部认证细节（10.5）。
    """
    detail = exc.detail if isinstance(exc.detail, dict) else None
    error_code = detail.get("error_code") if detail else None
    message = detail.get("message") if detail else "请求失败。"
    details = detail.get("details") if detail else None
    body = make_error_response(
        error_code=error_code or f"HTTP_{exc.status_code}",
        message=message,
        details=details,
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败统一 VALIDATION_ERROR（2.7.3 稳定契约）。"""
    body = make_error_response(
        error_code=ErrorCode.VALIDATION_ERROR.value,
        message="请求参数校验失败。",
        details=exc.errors(),
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body.model_dump())


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    """运维探活：无鉴权、无业务数据，只报告进程健康状态。"""
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
    }
