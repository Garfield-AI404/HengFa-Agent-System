"""MCP structuredContent 错误模型与 13.1 错误分类。

契约唯一依据 mcp-api.md §8：客户端只读 ``structuredContent.code``，
不解析中文 message 决定重试（13.1）。本模块把服务端错误码、HTTP 状态和
传输层异常统一归为内部 ``MCPError``，供上层（agent/domains）做重试与
人工介入决策。本模块不包含任何业务逻辑，也不保存凭据。

分类映射（13.1）：
- VALIDATION_ERROR        → 修正参数或询问，不自动盲重试
- ORDER_NOT_FOUND         → 重新确认目标，不更换 ID 猜测
- ORDER_VERSION_CONFLICT  → 重新 query、重算差异、必要时重新确认
- IDEMPOTENCY_KEY_CONFLICT→ 保留原 key 状态，确认意图并生成新操作
- IDEMPOTENCY_IN_PROGRESS → retryable=false，requires_operator_action=true
- MCP 认证/权限错误        → 停止写入并报告配置或权限问题
- 网络超时且结果未知       → 先 query 确认，不直接重放写操作
- 本地审计失败             → 标记人工复核（D8 落实现，类别已预留）
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# mcp-api.md §8 的完整服务端错误码清单（未知码一律保守处理）
# 认证/权限与网络层错误码为客户端内部码，不来自 structuredContent。
_KNOWN_SERVER_CODES = frozenset(
    {
        "VALIDATION_ERROR",
        "ORDER_NOT_FOUND",
        "ORDER_CREATE_REQUIRES_BUNDLE",
        "ORDER_UPDATE_REQUIRES_ORDER_TOOL",
        "ORDER_DELETE_NOT_ALLOWED",
        "ORDER_VERSION_CONFLICT",
        "IDEMPOTENCY_KEY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "BUSINESS_ERROR",
        "PERMISSION_DENIED",
        "UNKNOWN_ENTITY",
        "UNKNOWN_FIELD",
        "READ_ONLY_ENTITY",
        "SYSTEM_FIELD_FORBIDDEN",
    }
)

# 服务端各错误码的 retryable **缺省**值（mcp-api.md §8 表格，均为 false）。
# 仅在服务端未显式声明 retryable 时回退到本表；显式声明时以服务端为准
# （2026-09-22 F2：RAG 长期记忆服务刻意把 embedding_unavailable /
# store_unavailable 标为 retryable=true，见 mcp/server.py 的 _error_payload）。
# ERP 的 mcp-api.md §8 全部为 false，且 ERP 响应显式带 retryable=false，
# 故本改动对 ERP 逐字节无影响。
_SERVER_RETRYABLE = {code: False for code in _KNOWN_SERVER_CODES}


class ErrorCategory(str, enum.Enum):
    """内部错误类别（13.1 九类的机械映射，供上层决策与审计归类）。

    注：SERVER 类别为 RV-08 决策项 c（项目负责人批准）：HTTP 5xx 单列
    服务端故障类别（不再归 NETWORK"结果未知"），供上层区分告警等级。
    """

    VALIDATION = "VALIDATION"  # 参数/枚举/业务字段错误
    NOT_FOUND = "NOT_FOUND"  # 订单或实体不存在/软删除
    VERSION_CONFLICT = "VERSION_CONFLICT"  # 乐观锁版本过期
    IDEMPOTENCY = "IDEMPOTENCY"  # 幂等键冲突/异常遗留
    AUTH = "AUTH"  # MCP 认证/权限（HTTP 401/403、PERMISSION_DENIED）
    NETWORK = "NETWORK"  # 网络不可达、超时且结果未知
    SERVER = "SERVER"  # HTTP 5xx 服务端故障（决策项 c；结果未知，写不重试）
    RPC = "RPC"  # JSON-RPC 协议层错误
    LOCAL = "LOCAL"  # Agent 本地失败（审计等，D8 使用）
    UNKNOWN = "UNKNOWN"  # 无法归类的服务端错误，fail closed


# 内部错误码（客户端侧生成的规范码）
class InternalCode(str, enum.Enum):
    MCP_AUTH_FAILED = "MCP_AUTH_FAILED"  # HTTP 401：无效凭据/停用账号
    MCP_PERMISSION_DENIED = "MCP_PERMISSION_DENIED"  # HTTP 403 / scope 不足
    MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"  # 无法连接（ERP 不可达，13.4 降级）
    MCP_TIMEOUT_RESULT_UNKNOWN = "MCP_TIMEOUT_RESULT_UNKNOWN"  # 超时且结果未知
    MCP_SERVER_ERROR = "MCP_SERVER_ERROR"  # HTTP 5xx 服务端故障（决策项 c）
    MCP_HTTP_ERROR = "MCP_HTTP_ERROR"  # 其他 4xx
    MCP_RPC_ERROR = "MCP_RPC_ERROR"  # JSON-RPC 层错误
    MCP_TOOL_ERROR_UNSTRUCTURED = "MCP_TOOL_ERROR_UNSTRUCTURED"  # isError 但无结构化码
    CREDENTIALS_NOT_CONFIGURED = "CREDENTIALS_NOT_CONFIGURED"  # 无凭据，fail closed
    NOT_WHITELISTED = "NOT_WHITELISTED"  # 工具不在内勤挂载白名单
    SCOPE_NOT_ALLOWED = "SCOPE_NOT_ALLOWED"  # scope 不在 2.4 六项内
    SYSTEM_FIELD_FORBIDDEN = "SYSTEM_FIELD_FORBIDDEN"  # 伪造系统/派生字段（本地拦截）
    VALIDATION_ERROR = "VALIDATION_ERROR"  # 白名单外/非法参数（本地拦截，码与服务端一致）
    MCP_METADATA_STALE = "MCP_METADATA_STALE"  # 元数据陈旧，拒绝写入（决策项 a）
    MCP_METADATA_UNKNOWN_ENTITY = "MCP_METADATA_UNKNOWN_ENTITY"  # 实体不在元数据内（本地拦截）


@dataclass(frozen=True)
class MCPError(Exception):
    """内部规范错误。message 恒为安全英文文案：不含凭据、SQL、堆栈。

    ``retryable`` 只描述"同一参数原样自动重试"是否安全：
    - 写工具一律 False（13.2：create_order_bundle 复用同 key 时才可重试，
      update_order 不自动盲重试；重试决策由上层结合领域语义做）。
    - 只读 query 的有限指数退避由上层实现（13.2）；2026-08-27 起，瞬时
      HTTP 故障（404/5xx/连接失败）在 client 层做有限重试（见
      MCPClient._should_retry），业务错误码仍不自动重试。
    """

    code: str
    category: ErrorCategory
    retryable: bool = False
    requires_operator_action: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    message: str = "MCP call failed."

    def __str__(self) -> str:
        # 安全断言：异常字符串可能进日志，绝不携带凭据（details 由调用方保证无敏感值）
        return f"{self.code}: {self.message}"

    @classmethod
    def from_structured_content(
        cls,
        sc: dict[str, Any],
        tool_name: str = "",
        *,
        server_label: str = "ERP",
    ) -> "MCPError":
        """从 CallToolResult.structuredContent 归类（mcp-api.md §8）。

        只读取 code/retryable/requires_operator_action/details；
        message 仅作日志参考，不用于重试判断（13.1：不解析中文）。

        ``server_label``：客户端归属文案（F5）——同一实现服务 ERP 与 RAG 两个
        MCP 服务端，兜底文案必须标明来源，否则 RAG 的报错会被误读成打到 ERP。
        ERP 传缺省 "ERP"，文案逐字节不变。

        known 码的 retryable（F2）：**以服务端声明的 ``sc["retryable"]`` 为准**
        （mcp-api.md §8 把 retryable 定为结构化契约字段，读它不违反"不解析中文"）；
        服务端未声明时回退 ``_SERVER_RETRYABLE``（全 false）。ERP 全部显式 false，
        行为不变；RAG 的 embedding_unavailable/store_unavailable 得以为 true。
        """
        code = str(sc.get("code") or "MCP_TOOL_ERROR_UNSTRUCTURED")
        if code not in _KNOWN_SERVER_CODES:
            # 未知码：fail closed（13.1 保守处理），保留原码供审计排查
            server_message = str(sc.get("message") or "")
            suffix = ""
            if server_message:
                shown = server_message if len(server_message) <= 500 else server_message[:500] + "…"
                suffix = f" {server_label} said: {shown}"
            return cls(
                code=code,
                category=ErrorCategory.UNKNOWN,
                retryable=False,
                requires_operator_action=bool(sc.get("requires_operator_action")),
                details=dict(sc.get("details") or {}),
                message=f"Unknown {server_label} error code {code!r}; treated as non-retryable.{suffix}",
            )
        category = _category_for(code)
        if "retryable" in sc:
            retryable = bool(sc.get("retryable"))
        else:
            retryable = _SERVER_RETRYABLE[code]
        return cls(
            code=code,
            category=category,
            retryable=retryable,
            requires_operator_action=code == "IDEMPOTENCY_IN_PROGRESS"
            or bool(sc.get("requires_operator_action")),
            details=dict(sc.get("details") or {}),
            message=str(sc.get("message") or code),
        )

    @classmethod
    def from_http_status(cls, status_code: int, *, server_label: str = "ERP") -> "MCPError":
        """HTTP 层失败归类。401=认证失败且不重试（任务书：不重试）；403=权限。"""
        if status_code == 401:
            return cls(
                code=InternalCode.MCP_AUTH_FAILED.value,
                category=ErrorCategory.AUTH,
                message=f"{server_label} MCP authentication failed (HTTP 401).",
            )
        if status_code == 403:
            return cls(
                code=InternalCode.MCP_PERMISSION_DENIED.value,
                category=ErrorCategory.AUTH,
                message=f"{server_label} MCP permission denied (HTTP 403).",
            )
        if 500 <= status_code <= 599:
            # 决策项 c（项目负责人批准，RV-08 §10-1）：5xx 单列服务端故障，
            # 不再归 NETWORK"结果未知"；结果未知语义不变（写操作不重试，
            # 13.1/13.2），供上层区分告警等级。
            # details.http_status 供 client 层瞬时故障重试判定（2026-08-27）。
            return cls(
                code=InternalCode.MCP_SERVER_ERROR.value,
                category=ErrorCategory.SERVER,
                message=f"{server_label} MCP server error (HTTP {status_code}); outcome unknown.",
                details={"http_status": status_code},
            )
        return cls(
            code=InternalCode.MCP_HTTP_ERROR.value,
            category=ErrorCategory.UNKNOWN,
            message=f"{server_label} MCP returned HTTP {status_code}.",
            details={"http_status": status_code},
        )

    @classmethod
    def from_transport(cls, exc: BaseException, *, server_label: str = "ERP") -> "MCPError":
        """传输层（httpx）异常归类。所有网络失败按"结果未知"处理：
        请求可能已到达服务端并被处理，绝不对写操作直接重放（13.1/13.2）。
        """
        name = type(exc).__name__
        from httpx import ConnectError, ConnectTimeout, ReadTimeout

        if isinstance(exc, (ConnectError, ConnectTimeout)):
            return cls(
                code=InternalCode.MCP_CONNECT_FAILED.value,
                category=ErrorCategory.NETWORK,
                message=f"{server_label} MCP unreachable.",
                details={"transport_error": name},
            )
        if isinstance(exc, ReadTimeout):
            return cls(
                code=InternalCode.MCP_TIMEOUT_RESULT_UNKNOWN.value,
                category=ErrorCategory.NETWORK,
                message=f"{server_label} MCP read timed out; outcome unknown.",
                details={"transport_error": name},
            )
        return cls(
            code=InternalCode.MCP_TIMEOUT_RESULT_UNKNOWN.value,
            category=ErrorCategory.NETWORK,
            message=f"{server_label} MCP transport error; outcome unknown.",
            details={"transport_error": name},
        )

    @classmethod
    def rpc_error(cls, jsonrpc_code: int, message: str) -> "MCPError":
        """JSON-RPC 层错误（id 匹配失败、方法不存在等）。"""
        return cls(
            code=InternalCode.MCP_RPC_ERROR.value,
            category=ErrorCategory.RPC,
            message="ERP MCP JSON-RPC error.",
            details={"jsonrpc_code": jsonrpc_code, "jsonrpc_message": message},
        )

    @classmethod
    def credentials_not_configured(cls, *, server_label: str = "ERP") -> "MCPError":
        return cls(
            code=InternalCode.CREDENTIALS_NOT_CONFIGURED.value,
            category=ErrorCategory.AUTH,
            message=f"{server_label} MCP credentials are not configured; MCP calls disabled.",
        )


def _category_for(code: str) -> ErrorCategory:
    """服务端错误码 → 内部类别（13.1 机械映射）。"""
    mapping = {
        "VALIDATION_ERROR": ErrorCategory.VALIDATION,
        "ORDER_NOT_FOUND": ErrorCategory.NOT_FOUND,
        "ORDER_VERSION_CONFLICT": ErrorCategory.VERSION_CONFLICT,
        "IDEMPOTENCY_KEY_CONFLICT": ErrorCategory.IDEMPOTENCY,
        "IDEMPOTENCY_IN_PROGRESS": ErrorCategory.IDEMPOTENCY,
        "PERMISSION_DENIED": ErrorCategory.AUTH,
        "UNKNOWN_ENTITY": ErrorCategory.VALIDATION,
        "UNKNOWN_FIELD": ErrorCategory.VALIDATION,
        "READ_ONLY_ENTITY": ErrorCategory.VALIDATION,
        "SYSTEM_FIELD_FORBIDDEN": ErrorCategory.VALIDATION,
        # 领域工具禁止路径：参数/用途错误，不重试
        "ORDER_CREATE_REQUIRES_BUNDLE": ErrorCategory.VALIDATION,
        "ORDER_UPDATE_REQUIRES_ORDER_TOOL": ErrorCategory.VALIDATION,
        "ORDER_DELETE_NOT_ALLOWED": ErrorCategory.VALIDATION,
        "BUSINESS_ERROR": ErrorCategory.VALIDATION,
    }
    return mapping.get(code, ErrorCategory.UNKNOWN)
