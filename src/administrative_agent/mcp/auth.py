"""ERP MCP 认证头（mcp-api.md §1）：X-API-Key 优先，Authorization: Bearer <JWT> 回退。

凭据只从 config/.env 读取（15.2），任何路径不得输出凭据：
- 本模块的返回值只进 HTTP 请求头；
- 异常消息、日志、details 均不携带凭据（由调用方统一用 MCPError 安全消息）；
- 两通道都未配置时抛 ``CredentialsNotConfiguredError``，调用方必须 fail closed
  （10.5），禁止发出可能泄露内部信息的请求。
"""

from __future__ import annotations

from administrative_agent.config import Settings

# 凭据在请求头中的输出位置（用于测试断言，不含任何真实值）
X_API_KEY_HEADER = "X-API-Key"
AUTHORIZATION_HEADER = "Authorization"


class CredentialsNotConfiguredError(RuntimeError):
    """ERP MCP 认证凭据未配置。安全消息，不含任何敏感值。"""

    def __init__(self) -> None:
        super().__init__(
            "ERP MCP credentials are not configured; MCP calls disabled. "
            "Configure ERP_MCP_API_KEY in the environment."
        )


def build_key_headers(api_key: str, jwt: str = "") -> dict[str, str]:
    """按 X-API-Key 优先、Bearer <JWT> 回退构造认证头；均缺失时 fail closed。

    与 ``build_auth_headers`` 共用同一策略、只是凭据来源参数化——供 RAG MCP
    等第二个 MCP 客户端实例复用（凭据同样只进请求头，绝不进日志/异常）。
    """
    key = (api_key or "").strip()
    if key:
        return {X_API_KEY_HEADER: key}
    token = (jwt or "").strip()
    if token:
        return {AUTHORIZATION_HEADER: f"Bearer {token}"}
    raise CredentialsNotConfiguredError()


def build_auth_headers(settings: Settings) -> dict[str, str]:
    """构造 ERP MCP 认证请求头（X-API-Key 优先，Bearer <JWT> 回退）。

    fail closed：两通道均未配置时抛 ``CredentialsNotConfiguredError``。
    """
    return build_key_headers(settings.erp_mcp_api_key, settings.erp_mcp_jwt)
