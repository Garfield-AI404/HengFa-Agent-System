"""MCP 契约测试共享夹具（规格 14.2：respx 假服务）。

在本 conftest 顶层先于任何 administrative_agent 导入设置测试 ERP 环境变量；
契约测试统一用 Settings() 新实例（不依赖 config 模块级单例），保证
ERP_MCP_URL/API_KEY 读到确定性测试值，覆盖 .env 中的真实值。
"""

from __future__ import annotations

import json
import os

os.environ["ERP_MCP_URL"] = "http://erp-mcp.test"
os.environ["ERP_MCP_API_KEY"] = "test-api-key-not-for-production"
os.environ["ERP_MCP_JWT"] = ""
os.environ["ERP_MCP_CONNECT_TIMEOUT"] = "5"
os.environ["ERP_MCP_READ_TIMEOUT"] = "30"
os.environ["ERP_MCP_TOTAL_TIMEOUT"] = "60"
os.environ["ERP_MCP_METADATA_TTL"] = "3600"

import pytest  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.mcp.client import MCPClient  # noqa: E402

ERP_BASE = "http://erp-mcp.test"
ERP_MCP_URL = f"{ERP_BASE}/mcp"
TEST_API_KEY = "test-api-key-not-for-production"


def jsonrpc_result(payload: dict) -> dict:
    """JSON-RPC 成功信封：result = CallToolResult。"""
    return {"jsonrpc": "2.0", "id": 1, "result": payload}


def call_tool_result(
    data: dict,
    is_error: bool = False,
    structured: dict | None = None,
) -> dict:
    """构造 CallToolResult（mcp-api.md §8：content[0].text + structuredContent）。"""
    payload: dict = {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": is_error,
    }
    if structured is not None:
        payload["structuredContent"] = structured
    return jsonrpc_result(payload)


def structured_error(
    code: str,
    message: str = "",
    retryable: bool = False,
    requires_operator_action: bool = False,
    details: dict | None = None,
) -> dict:
    """mcp-api.md §8 结构化错误。"""
    return call_tool_result(
        {},
        is_error=True,
        structured={
            "code": code,
            "message": message,
            "retryable": retryable,
            "requires_operator_action": requires_operator_action,
            "details": details or {},
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def client(settings: Settings) -> MCPClient:
    c = MCPClient(settings)
    yield c
    c.close()
