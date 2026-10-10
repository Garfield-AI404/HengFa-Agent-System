"""MCP Streamable HTTP 客户端（JSON-RPC 2.0，POST {base}/mcp）。

契约唯一依据 mcp-api.md：
- §1 认证（X-API-Key / Bearer）；工具失败返回 CallToolResult + isError=true
- §5 写工具请求带 operation_id/trace_id（只读工具 query/list_entities 无此字段）
- §8 结构化错误；客户端只读 structuredContent.code

安全边界（14.4/9.x）：
- 凭据只进请求头，绝不进日志、异常或响应；本模块不记录参数与响应体
- 写工具（create/update/delete/create_order_bundle/update_order）调用前
  由 scopes.py 本地拦截；网络失败一律按"结果未知"处理（13.1/13.2），
  上层重试决策基础不变；2026-08-27 起瞬时故障（HTTP 404/5xx/连接失败）
  在本层做有限重试（最多 settings.erp_mcp_max_retries 次；401/403、
  读超时结果未知、业务错误码不重试，见 _should_retry）。

  重试的"不会重复写入"由两条不同机制保证，不可混为一谈（2026-08-29 更正，
  原注释笼统说成"服务端靠同 key 去重"）：
  - create_order_bundle：请求带 idempotency_key（mcp-api.md §6.1），
    服务端同 key 同载荷直接返回原订单 replayed=true，同 key 异载荷
    返回 IDEMPOTENCY_KEY_CONFLICT；
  - update_order：**无独立幂等键**，靠 expected_updated_at 行锁
    （mcp-api.md §7.2）。若首次已应用，重试时 expected_updated_at 已
    陈旧 → 返回 ORDER_VERSION_CONFLICT。因此"已应用但响应丢失"的写操作
    重试后会表现为版本冲突，而非重放成功；业务层的版本冲突重查分支会
    发现期望变更已生效并按最新快照展示，终态正确（不会重复叠加变更）。
- 超时连接/读取/总超时均来自配置（13.3）

本客户端为同步 httpx.Client（协议层）；后续 agent 异步调用时用
asyncio.to_thread 包装或迁移 AsyncClient（仅本文件改动，见 RV-07 材料）。
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import httpx
import structlog

from administrative_agent import lifecycle
from administrative_agent.config import Settings
from administrative_agent.mcp import auth
from administrative_agent.mcp.errors import (
    ErrorCategory,
    InternalCode,
    MCPError,
    _KNOWN_SERVER_CODES,
)

logger = structlog.get_logger(__name__)

# mcp-api.md §5：operation_id/trace_id 只用于写工具（1-128 字符）
_WRITE_TOOLS = frozenset(
    {"create", "update", "delete", "create_order_bundle", "update_order"}
)

# 2026-08-27 瞬时故障重试：可重试的 HTTP 状态（请求确定未达业务逻辑——
# 404=路由未挂载/服务重启窗口、502/503/504=网关或上游未处理），读写工具都可重试。
# 只读工具额外可重试任意 5xx（读无副作用，见 _should_retry）。
_RETRYABLE_HTTP_STATUS = frozenset({404, 502, 503, 504})

# 绝不自动重试的工具（2026-09-23）：**无幂等键的写工具**。
# 这里用 MCP 层工具名（tools/call 的 name），**不是**模型可见名——RAG 长期记忆
# 的写入工具在模型侧叫 ``rag_remember``，但 tools/rag_memory.py 下发给 MCP 的
# name 是 ``remember``（``_call_rag(ctx, "remember", payload)``），`_should_retry`
# 收到的正是后者，故键必须是 ``remember``。
# 为什么必须挡：RAG 的 ``memory/service.remember`` 无条件 ``session.add``、**没有
# 幂等键**，重试会在库里静默建出重复记忆；而任何一次尝试都含云端嵌入调用，实测
# 有 5.8% 的新建连接长尾 21.3s（见 config.py 的 RAG 超时注释），"超时/结果未知"
# 这一形态最可能触发重试 —— 重试窗口是**真实存在**的，不是理论担忧。
# 注意：**不要**把它加进 _WRITE_TOOLS —— 那个集合是 ERP 写语义（create/update/…）
# 且联动 call_tool 的 lifecycle.in_flight_write() 在途写保护，语义不同，混进去
# 会污染 ERP 的在途写保护。
_NEVER_RETRY_TOOLS = frozenset({"remember"})

_JSONRPC_VERSION = "2.0"
_METHOD_TOOLS_CALL = "tools/call"
# MCP Streamable HTTP 标准握手（真实服务端 FastMCP 1.29+ 强制要求：
# 首次请求必须是 initialize，后续请求带 Mcp-Session-Id 响应头回传的会话 ID）
_METHOD_INITIALIZE = "initialize"
_SESSION_HEADER = "mcp-session-id"
_PROTOCOL_VERSION = "2025-03-26"


@dataclass(frozen=True)
class ToolResult:
    """一次 MCP 工具调用的结果。失败时 data 为空 dict，错误见 error。

    elapsed_ms 用于技术日志；ok 与 error 是上层重试/人工介入决策的唯一依据。
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: MCPError | None = None
    operation_id: str | None = None
    trace_id: str | None = None
    elapsed_ms: int = 0
    #: 传输层瞬时重试留痕（2026-08-29 补，11.2 规则 3 可追溯性）。
    #: 每条为 {"attempt_no", "error_code", "error_category", "delay_s"}，
    #: 按发生顺序。重试在本层内部完成，业务层只看到最终结果，因此这些
    #: 尝试不会各自产生 task_attempts 行——但连接失败等"结果未知"的写操作
    #: 重试意味着执行结果一度不可知，属可审计性证据。此前只存在于有界
    #: （500 条）且进程内存的结构化日志环形缓冲，负载高时会被淘汰，故由
    #: 上层补记持久化审计事件（audit.AuditService.record_mcp_retries）。
    #: 读工具的重试同样记录在此，是否落审计由调用方按"是否有副作用"决定。
    retries: tuple[dict[str, Any], ...] = ()


def _build_timeout(settings: Settings) -> httpx.Timeout:
    """把连接/读取/总超时（13.3）收敛为 httpx 四组件超时。

    httpx 0.28 的 Timeout 没有独立 total 组件；单次调用最坏墙钟时长近似为
    connect×2 + read×2（连接获取+建立、读响应）。按此收敛：
    最坏总时长 <= 配置的 ERP_MCP_TOTAL_TIMEOUT（总超时作为硬上界生效）。
    """
    total = max(settings.erp_mcp_total_timeout, 0.1)
    connect = min(settings.erp_mcp_connect_timeout, total / 4)
    read = min(settings.erp_mcp_read_timeout, (total - 2 * connect) / 2)
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


def _build_rag_timeout(settings: Settings) -> httpx.Timeout:
    """RAG 客户端专用超时（G1）：同一收敛公式，但读 RAG_MCP_* 旋钮。

    RAG 每次调用含一次**云端嵌入 API 调用**，比 ERP 本地查询慢/抖；复用
    erp_mcp_* 会把 RAG 的云端抖动纳入 ERP 的超时口径。ERP 的
    ``_build_timeout`` 逐字节不变。
    """
    total = max(settings.rag_mcp_total_timeout, 0.1)
    connect = min(settings.rag_mcp_connect_timeout, total / 4)
    read = min(settings.rag_mcp_read_timeout, (total - 2 * connect) / 2)
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


# 生成器权威位置在 observability/tracing.py（2.6 代码树，D8 起）；
# 此处 re-export 保持既有调用方（tasks/service.py 等）兼容。
from administrative_agent.observability.tracing import new_operation_id, new_trace_id  # noqa: F401


class MCPClient:
    """薄协议客户端：只负责 JSON-RPC 封装、认证、超时与结构化错误归类。

    [2026-09-22 RAG 长期记忆接入] 同一实现服务多个 MCP 服务端：
    - 缺省（``endpoint=None``）：ERP MCP，端点取 ``settings.erp_mcp_url``、
      认证取 ``settings.erp_mcp_api_key``/``erp_mcp_jwt``（既有行为不变）；
    - 传 ``endpoint``/``build_headers``：第二个 MCP 服务端（如 RAG 长期记忆
      ``rag_mcp_url``），协议层（握手/会话/重试/错误归类）完全复用，不复制
      第二份实现。``build_headers`` 是**每次请求现取凭据**的回调（凭据只进
      请求头，不进任何日志/异常）。
    """

    def __init__(
        self,
        settings: Settings,
        timeout: httpx.Timeout | None = None,
        *,
        endpoint: str | None = None,
        build_headers: Callable[[], dict[str, str]] | None = None,
        max_retries: int | None = None,
        server_label: str = "ERP",
    ) -> None:
        if endpoint is None:
            if not settings.erp_mcp_url.strip():
                raise ValueError("ERP_MCP_URL is not configured.")
            endpoint = settings.erp_mcp_url
        # 端点语义是 MCP 端点完整 URL（如 http://host:8000/mcp）；
        # 兼容只配主机端口的写法（自动补 /mcp）。
        self._endpoint = endpoint.strip().rstrip("/")
        if not self._endpoint:
            raise ValueError("MCP endpoint is not configured.")
        if not self._endpoint.endswith("/mcp"):
            self._endpoint += "/mcp"
        self._settings = settings
        self._timeout = timeout or _build_timeout(settings)
        # 瞬时故障重试上限：缺省取 ERP 旋钮（既有行为不变）；RAG 客户端显式传
        # rag_mcp_max_retries（G1：与 RAG 超时配套，约束最坏阻塞时长）。
        self._max_retries = (
            settings.erp_mcp_max_retries if max_retries is None else max_retries
        )
        # 错误文案归属（F5）：同一实现服务 ERP 与 RAG 两个服务端，兜底文案
        # 需标明来源，否则 RAG 报错会被误读成打到 ERP。
        self._server_label = server_label
        self._build_headers = build_headers or (
            lambda: auth.build_auth_headers(settings)
        )
        self._client = httpx.Client(
            base_url=self._endpoint,
            timeout=self._timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._initialized = False
        self._session_lock = threading.Lock()

    def close(self) -> None:
        if self._initialized and self._session_id:
            try:
                self._client.delete(self._endpoint, headers={_SESSION_HEADER: self._session_id})
            except httpx.HTTPError:
                pass  # best-effort 关闭会话（mcp-api.md §9 技术日志边界）
        self._client.close()

    # ---- 公开调用入口 ----

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        """调用 MCP 工具。写工具自动注入 operation_id/trace_id（mcp-api.md §5）。

        优雅停机（15.3 步骤 7/D25）：写工具调用期间计入 lifecycle 进行中
        计数——停机时先停收新写任务、再等待该计数归零（即当前调用返回、
        结果落审计）后才停机；只读工具不计入（不阻塞停机）。
        返回 ToolResult，不 raise 网络/协议异常；凭据类错误同样以
        ToolResult(error=...) 返回，上层统一处理。

        2026-08-27 瞬时故障有限重试（项目负责人批准：重复次数三次以内）：
        失败经 _call_with_retries 按可重试错误集（_should_retry）做最多
        settings.erp_mcp_max_retries 次重试；写工具重试复用同一
        operation_id/trace_id（服务端幂等键去重，不会重复写入）；只读
        额外重试任意 5xx；401/403、读超时结果未知、业务错误码不重试。
        一切失败仍以 ToolResult 返回，契约不变。
        """
        if tool_name in _WRITE_TOOLS:
            with lifecycle.in_flight_write():
                return self._call_with_retries(
                    tool_name, arguments, operation_id, trace_id
                )
        return self._call_with_retries(tool_name, arguments, operation_id, trace_id)

    def _call_with_retries(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        operation_id: str | None,
        trace_id: str | None,
    ) -> ToolResult:
        """瞬时故障有限重试（2026-08-27）：最多 settings.erp_mcp_max_retries 次。

        写工具幂等键（operation_id/trace_id）在循环外生成一次：每次尝试
        复用同 key（mcp-api.md §5/§6.1/§7.1），服务端靠同 key 去重——若
        首次实际已执行，重试会返回 IDEMPOTENCY_KEY_CONFLICT 而非重复写入；
        只读工具原样透传调用方传入的 id。每次重试前重置会话状态（服务
        重启后旧会话失效，下次尝试重新 initialize 握手），指数退避
        1s/2s/4s。用尽重试次数后返回最后一次的 ToolResult，fail closed
        语义不变（不 raise，契约见 call_tool）。
        """
        max_retries = max(0, int(self._max_retries))
        if tool_name in _WRITE_TOOLS:
            # 循环外生成一次：所有尝试共享同一幂等键
            op_id = operation_id or new_operation_id()
            tr_id = trace_id or new_trace_id()
        else:
            op_id, tr_id = operation_id, trace_id

        retries: list[dict[str, Any]] = []
        for attempt in range(max_retries + 1):
            result = self._call_tool_impl(tool_name, arguments, op_id, tr_id)
            if result.ok or result.error is None or not _should_retry(result.error, tool_name):
                return replace(result, retries=tuple(retries))
            if attempt >= max_retries:
                # 已用尽重试次数：返回最后一次失败结果（fail closed）
                return replace(result, retries=tuple(retries))
            # 服务重启窗口：旧会话失效，下次尝试重新 initialize 握手。
            # _call_tool_impl 已返回、无锁持有（_session_lock 只在
            # _call_tool_impl 内部持有），重置安全。
            self._initialized = False
            self._session_id = None
            delay = 1 << attempt  # 指数退避：1s/2s/4s（每次重试前 sleep）
            # 留痕先于 sleep：即使后续被中断，已发生的重试也有记录
            retries.append(
                {
                    "attempt_no": attempt + 1,
                    "tool": tool_name,
                    "error_code": result.error.code,
                    "error_category": result.error.category.value,
                    "delay_s": delay,
                }
            )
            _log_retry(tool_name, attempt + 1, result.error, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")  # range(max_retries + 1) 恒至少一次

    def _call_tool_impl(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        operation_id: str | None,
        trace_id: str | None,
    ) -> ToolResult:
        started = time.monotonic()
        args = dict(arguments or {})
        if tool_name in _WRITE_TOOLS:
            # mcp-api.md §5/§6.1/§7.1：同一业务操作重试复用 operation_id
            args.setdefault("operation_id", operation_id or new_operation_id())
            args.setdefault("trace_id", trace_id or new_trace_id())

        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": next(self._ids),
            "method": _METHOD_TOOLS_CALL,
            "params": {"name": tool_name, "arguments": args},
        }

        try:
            headers = self._build_headers()
        except auth.CredentialsNotConfiguredError as exc:
            # fail closed：无凭据不发请求（10.5/13.1 认证错误停止写入）
            not_configured = MCPError.credentials_not_configured(
                server_label=self._server_label
            )
            _log_call(tool_name, started, ok=False, error_code=not_configured.code)
            return ToolResult(
                ok=False,
                error=not_configured,
                elapsed_ms=_elapsed_ms(started),
            )

        try:
            with self._session_lock:
                resp = self._post_with_session(payload, headers)
                if resp.status_code == 400 and self._session_id:
                    # 会话已过期（Missing session ID）：重握手一次后重发
                    self._initialized = False
                    self._session_id = None
                    resp = self._post_with_session(payload, headers)
        except httpx.TimeoutException as exc:
            return self._transport_error(tool_name, started, exc)
        except httpx.HTTPError as exc:
            return self._transport_error(tool_name, started, exc)
        except MCPError as exc:
            _log_call(tool_name, started, ok=False, error_code=exc.code)
            return ToolResult(ok=False, error=exc, elapsed_ms=_elapsed_ms(started))

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error = MCPError.from_http_status(
                exc.response.status_code, server_label=self._server_label
            )
            _log_call(tool_name, started, ok=False, error_code=error.code)
            return ToolResult(ok=False, error=error, elapsed_ms=_elapsed_ms(started))

        result = self._parse_response_body(resp)
        if isinstance(result, MCPError):
            _log_call(tool_name, started, ok=False, error_code=result.code)
            return ToolResult(ok=False, error=result, elapsed_ms=_elapsed_ms(started))
        # result 为 dict（JSON-RPC 信封）
        return self._unpack_tool_result(tool_name, started, result)

    def list_entities(
        self,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        """启动时及元数据失效后调用（规格 2.3）；只读，不注入写工具字段。"""
        return self.call_tool("list_entities", {}, operation_id, trace_id)

    # ---- 内部请求与解析 ----

    def _post_with_session(
        self, payload: dict[str, Any], auth_headers: dict[str, str]
    ) -> httpx.Response:
        """MCP 标准握手：首次请求必须是 initialize（真实服务端 FastMCP 强制），
        后续请求携带 Mcp-Session-Id 响应头回传的会话 ID。"""
        if not self._initialized:
            self._initialize()
        headers = dict(auth_headers)
        if self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        return self._client.post(self._endpoint, json=payload, headers=headers)

    def _initialize(self) -> None:
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": next(self._ids),
            "method": _METHOD_INITIALIZE,
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "hengfa-administrative-agent",
                    "version": "0.1.0",
                },
            },
        }
        try:
            resp = self._client.post(
                self._endpoint, json=payload, headers=self._build_headers()
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise MCPError.from_transport(exc, server_label=self._server_label) from exc
        if resp.status_code != 200:
            raise MCPError.from_http_status(
                resp.status_code, server_label=self._server_label
            )
        self._session_id = resp.headers.get(_SESSION_HEADER)
        self._initialized = True

    def _transport_error(self, tool_name: str, started: float, exc: BaseException) -> ToolResult:
        error = MCPError.from_transport(exc, server_label=self._server_label)
        _log_call(tool_name, started, ok=False, error_code=error.code)
        return ToolResult(ok=False, error=error, elapsed_ms=_elapsed_ms(started))

    def _parse_response_body(self, resp: httpx.Response) -> dict[str, Any] | MCPError:
        """解析 JSON-RPC 信封；防御性支持 text/event-stream（Streamable HTTP）。"""
        body = resp.text
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            body = _extract_sse_data(body)
            if body is None:
                return MCPError.rpc_error(-32700, "malformed SSE payload")
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return MCPError.rpc_error(-32700, "response is not JSON")
        if not isinstance(parsed, dict):
            return MCPError.rpc_error(-32603, "response envelope is not an object")
        return parsed

    def _unpack_tool_result(self, tool_name: str, started: float, envelope: dict[str, Any]) -> ToolResult:
        if "error" in envelope:
            rpc_err = envelope["error"]
            error = MCPError.rpc_error(
                int(rpc_err.get("code", -32603)) if isinstance(rpc_err, dict) else -32603,
                str(rpc_err.get("message", "")) if isinstance(rpc_err, dict) else str(rpc_err),
            )
            _log_call(tool_name, started, ok=False, error_code=error.code)
            return ToolResult(ok=False, error=error, elapsed_ms=_elapsed_ms(started))
        result = envelope.get("result")
        if not isinstance(result, dict):
            _log_call(tool_name, started, ok=False, error_code=MCPError.rpc_error(-32603, "no result").code)
            return ToolResult(ok=False, error=MCPError.rpc_error(-32603, "no result"), elapsed_ms=_elapsed_ms(started))

        if result.get("isError"):
            sc = result.get("structuredContent")
            if isinstance(sc, dict):
                error = MCPError.from_structured_content(
                    sc, tool_name, server_label=self._server_label
                )
            else:
                # 无结构化错误：尝试从 content[0].text 提取（mcp-api.md §8 兼容形态）
                error = _unstructured_error(
                    result.get("content"), server_label=self._server_label
                )
            _log_call(tool_name, started, ok=False, error_code=error.code)
            return ToolResult(ok=False, error=error, elapsed_ms=_elapsed_ms(started))

        data = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else None
        if data is None:
            data = _content_to_data(result.get("content"))
        _log_call(tool_name, started, ok=True)
        return ToolResult(ok=True, data=data, elapsed_ms=_elapsed_ms(started))


def build_rag_mcp_client(settings: Settings) -> MCPClient:
    """构造 RAG 长期记忆 MCP 客户端（第二个实例，复用同一套协议实现）。

    端点取 ``settings.rag_mcp_url``、认证用 ``settings.rag_mcp_api_key``
    （X-API-Key）。调用方必须先确认 ``rag_mcp_url``（与 key）非空——本函数
    不自行决定「是否启用」（启用开关在装配层 main._wire_rag_client）。
    RAG 服务的工具失败以 ``CallToolResult(isError=True)`` 返回（200），
    会话失效回 400（客户端已在本层重握手），故无需额外分支。

    G1：显式传 RAG 专用超时与重试上限（``_build_rag_timeout`` /
    ``rag_mcp_max_retries``），不再复用 ERP 旋钮，ERP 行为逐字节不变。
    F5：``server_label="RAG"`` 让兜底错误文案标明来源。
    """
    return MCPClient(
        settings,
        timeout=_build_rag_timeout(settings),
        max_retries=settings.rag_mcp_max_retries,
        server_label="RAG",
        endpoint=settings.rag_mcp_url,
        build_headers=lambda: auth.build_key_headers(settings.rag_mcp_api_key),
    )


def _unstructured_error(content: Any, *, server_label: str = "ERP") -> MCPError:
    text = _content_text(content)
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                return MCPError.from_structured_content(
                    parsed, server_label=server_label
                )
        except json.JSONDecodeError:
            pass
    # 兜底文案透传原始 content 文本（截断 500 字符防爆，超长加 …），便于诊断
    # 真实原因；取不到原始文本时保持原固定文案。code/category/retryable 不变。
    # server_label（F5）：标明来源（ERP/RAG），否则 RAG 报错会被误读成打到 ERP。
    if text:
        shown = text if len(text) <= 500 else text[:500] + "…"
        message = f"{server_label} tool failed without a structured error code: {shown}"
    else:
        message = f"{server_label} tool failed without a structured error code."
    return MCPError(
        code="MCP_TOOL_ERROR_UNSTRUCTURED",
        category=ErrorCategory.UNKNOWN,
        message=message,
    )


def _content_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
    return None


def _content_to_data(content: Any) -> dict[str, Any]:
    """成功响应：structuredContent 优先，否则解析 content[0].text 的 JSON。"""
    text = _content_text(content)
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"content": content} if content is not None else {}


def _extract_sse_data(body: str) -> str | None:
    """text/event-stream 中取 data 行拼接（仅支持单 JSON 消息场景）。"""
    parts = []
    for line in body.splitlines():
        if line.startswith("data:"):
            parts.append(line[5:].strip())
    return "\n".join(parts) if parts else None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log_call(
    tool_name: str,
    started: float,
    ok: bool,
    error_code: str | None = None,
) -> None:
    """技术日志（9.x 边界）：只记工具名/耗时/结果状态/错误码，不记参数与响应体。"""
    logger.info(
        "mcp_call",
        tool=tool_name,
        ok=ok,
        error_code=error_code,
        duration_ms=_elapsed_ms(started),
    )


def _should_retry(error: MCPError, tool_name: str) -> bool:
    """瞬时故障有限重试判定（2026-08-27，项目负责人批准：最多 3 次）。

    只重试"服务端确定没执行"的瞬时故障：
    - 连接失败（MCP_CONNECT_FAILED）：连接未建立，请求肯定未达 ERP，读写都重试
    - HTTP 404/502/503/504（_RETRYABLE_HTTP_STATUS）：请求未达业务逻辑
      （404=路由未挂载/服务重启窗口），读写都重试
    - 只读工具额外重试任意 5xx（读无副作用，最坏重复读；状态码来自
      error.details["http_status"]，由 errors.from_http_status 写入）

    绝不重试（硬 false，fail closed），分三类：
    1. **无幂等键的写工具**（_NEVER_RETRY_TOOLS，2026-09-23）：如 RAG 的
       ``remember`` —— 服务端无条件 ``session.add`` 无幂等键，重试会静默建出
       重复记忆。**这是函数最顶部的闸门**，用于挡下下面所有会放行的路径。
    2. **内部错误码**：401/403（MCP_AUTH_FAILED / MCP_PERMISSION_DENIED）、
       读超时（MCP_TIMEOUT_RESULT_UNKNOWN，结果未知）、JSON-RPC 层错误
       （MCP_RPC_ERROR），以及任何无法确认请求未执行的状态（无 http_status
       的本地错误码等）—— 这些的 retryable 恒为硬 false。
    3. 服务端已知业务码（_KNOWN_SERVER_CODES）下的 **ERP 写工具**：即便服务端
       声明 retryable=true 也一票否决（13.2 不自动盲重试，防未来误声明）。

    与此对应，**只有内部错误码是硬 false**；服务端声明的业务码走
    ``error.retryable``（F2，2026-09-22；mcp-api.md §8 把 retryable 定为结构化
    契约字段）：ERP 全部显式 false → 行为不变；RAG 的 embedding_unavailable /
    store_unavailable 声明 true → 放行有限重试（重试次数由该客户端 max_retries
    约束，见 G1）。注意：这条放行只对**只读**工具生效（``rag_recall``）；
    ``rag_forget`` 走 memory_id 路径是幂等的、其 retryable 行为也保持不变。
    """
    # 无幂等键的写工具（rag_remember → MCP name "remember"）：一律不重试。
    # 必须在最顶部——下面的 MCP_CONNECT_FAILED、服务端 retryable、只读 5xx
    # 等路径都可能放行，只有这里是全局硬闸门。
    if tool_name in _NEVER_RETRY_TOOLS:
        return False
    # 结果未知 / 认证 / JSON-RPC 层错误：一律不重试
    if error.code in {
        InternalCode.MCP_TIMEOUT_RESULT_UNKNOWN.value,
        InternalCode.MCP_AUTH_FAILED.value,
        InternalCode.MCP_PERMISSION_DENIED.value,
        InternalCode.MCP_RPC_ERROR.value,
    }:
        return False
    # 服务端已知业务码：只读工具只信服务端声明的 retryable（缺省 false）；
    # ERP 写工具保持"业务错误码一律不重试"（13.2 写操作不自动盲重试）——
    # 即便服务端声明 true 也不放行，防未来误声明。
    if error.code in _KNOWN_SERVER_CODES:
        if tool_name in _WRITE_TOOLS:
            return False
        return error.retryable
    if error.code == InternalCode.MCP_CONNECT_FAILED.value:
        return True
    # 其余 HTTP 类错误：需要 HTTP 状态码才能判定（404/502/503/504 或只读 5xx）
    http_status = error.details.get("http_status")
    if not isinstance(http_status, int):
        return False
    if http_status in _RETRYABLE_HTTP_STATUS:
        return True
    return tool_name not in _WRITE_TOOLS and 500 <= http_status <= 599


def _log_retry(
    tool_name: str,
    attempt: int,
    error: MCPError,
    delay_s: int,
) -> None:
    """重试技术日志（11.4 无敏感信息）：只记工具名/第几次重试/错误码/退避秒数。"""
    logger.info(
        "mcp_call_retry",
        tool=tool_name,
        attempt=attempt,
        error_code=error.code,
        delay_s=delay_s,
    )
