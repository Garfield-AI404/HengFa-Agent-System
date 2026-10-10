"""D23 developer 端点 API 测试（2.7.3 L520-521；10.5/10.6/12.4）。

覆盖：
- GET /api/v1/developer/health：developer 鉴权、依赖状态（无业务数据、
  无凭据）、401/403 拒绝路径（manager 访问 403）
- GET /api/v1/developer/logs：脱敏输出（敏感键/凭证形态/长度截断）、
  level 过滤、非法 level 422、limit 上限 422
- developer 访问业务 API（chat/tasks/reviews/audit/mappings）一律 403
  （10.5：developer 只允许健康检查与脱敏技术日志）
"""

from __future__ import annotations

from administrative_agent.auth.models import Role
from administrative_agent.observability.log_capture import LogRing
from administrative_agent.web.schemas import ErrorCode

from tests.api.conftest import DEVELOPER, MANAGER, MGR, cookies

#: health 依赖清单（2.7.3 L520：MCP/Agent 库/Checkpoint 库/会话配置状态）
DEPENDENCIES = {"erp_mcp", "agent_database", "checkpoint_database", "session"}

DEV = cookies(Role.DEVELOPER, account=DEVELOPER)


class TestDeveloperHealth:
    def test_health_ok_with_developer(self, client):
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["app"] == "hengfa-administrative-agent"
        assert body["log_level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        names = {d["name"] for d in body["dependencies"]}
        assert names == DEPENDENCIES  # 依赖状态完备，无多余依赖
        for dep in body["dependencies"]:
            assert dep["status"] in {"configured", "missing_credentials",
                                     "not_configured", "invalid"}
        # 测试环境：SESSION_SECRET 由 tests/conftest.py 注入 → configured
        by_name = {d["name"]: d for d in body["dependencies"]}
        assert by_name["session"]["status"] == "configured"

    def test_health_no_session_401(self, client):
        resp = client.get("/api/v1/developer/health")
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    def test_health_manager_forbidden_403(self, client):
        """10.5：manager 无权访问 developer 端点，403 且不透露内部细节。"""
        resp = client.get("/api/v1/developer/health", cookies=MGR)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == ErrorCode.FORBIDDEN.value
        assert "developer" in body["message"]  # 只说明所需权限
        assert body["details"] is None  # 不透露内部认证细节

    def test_health_mcp_missing_credentials_fail_closed(self, client, monkeypatch):
        """12.4：URL 已配但 API Key 缺失 → missing_credentials（fail closed）。

        （本地 .env 已配 ERP_MCP_URL/API_KEY，测试显式清空 key 模拟。）"""
        monkeypatch.setattr(
            "administrative_agent.config.settings.erp_mcp_url",
            "http://erp.internal:8000/mcp",
        )
        monkeypatch.setattr(
            "administrative_agent.config.settings.erp_mcp_api_key", "",
        )
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        by_name = {d["name"]: d for d in resp.json()["dependencies"]}
        assert by_name["erp_mcp"]["status"] == "missing_credentials"

    def test_health_mcp_configured_with_key(self, client, monkeypatch):
        monkeypatch.setattr(
            "administrative_agent.config.settings.erp_mcp_url",
            "http://erp.internal:8000/mcp",
        )
        monkeypatch.setattr(
            "administrative_agent.config.settings.erp_mcp_api_key", "test-key-123",
        )
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        by_name = {d["name"]: d for d in resp.json()["dependencies"]}
        assert by_name["erp_mcp"]["status"] == "configured"
        # detail 只含 host，不含 key 或 URL 全貌
        assert "test-key-123" not in resp.text
        assert "erp.internal" in (by_name["erp_mcp"]["detail"] or "")

    def test_health_db_url_never_exposes_password(self, client, monkeypatch):
        """12.4 L1305：DB URL 中的密码不得出现在响应中。"""
        monkeypatch.setattr(
            "administrative_agent.config.settings.agent_database_url",
            "postgresql://user:SuperSecret123@db.internal:5432/agent",
        )
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        assert "SuperSecret123" not in resp.text
        by_name = {d["name"]: d for d in resp.json()["dependencies"]}
        dep = by_name["agent_database"]
        assert dep["status"] == "configured"
        assert "postgresql" in dep["detail"]
        assert "db.internal" in dep["detail"]

    def test_health_contains_no_business_data(self, client):
        """健康端点无业务数据（L520）：响应不得出现任务/订单/审计字段。"""
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        text = resp.text
        for token in ("task_id", "order", "audit", "review", "payload"):
            assert token not in text


class TestDeveloperLogs:
    def _seed(self, client, records):
        ring = LogRing()
        for r in records:
            ring.append(r)
        client.app.state.log_ring = ring
        return ring

    def _get(self, client, **params):
        return client.get("/api/v1/developer/logs", cookies=DEV, params=params)

    def test_logs_contract(self, client):
        client.app.state.log_ring = LogRing()
        resp = self._get(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["limit"] == 50
        assert body["items"] == []

    def test_logs_returns_seeded_records_latest_first(self, client):
        self._seed(client, [
            {"timestamp": "2026-08-23T01:00:00+00:00", "level": "INFO",
             "event": "first", "logger": "test", "step": 1},
            {"timestamp": "2026-08-23T02:00:00+00:00", "level": "ERROR",
             "event": "second", "logger": "test", "step": 2},
        ])
        resp = self._get(client)
        body = resp.json()
        assert body["total"] == 2
        assert [e["event"] for e in body["items"]] == ["second", "first"]
        assert body["items"][0]["payload"] == {"step": 2}

    def test_logs_redacts_sensitive_keys(self, client):
        """11.2 规则 4：敏感键值必须脱敏输出。"""
        self._seed(client, [
            {"timestamp": "t", "level": "INFO", "event": "e", "logger": "l",
             "db_password": "super-secret-credential",
             "api_key": "live-1-2-3-4-5-6-7-8-9-0",
             "safe_field": "普通文本"},
        ])
        resp = self._get(client)
        assert resp.status_code == 200
        text = resp.text
        assert "super-secret-credential" not in text
        assert "live-1-2-3-4-5-6-7-8-9-0" not in text
        payload = resp.json()["items"][0]["payload"]
        assert payload["db_password"].startswith("***")
        assert payload["api_key"].startswith("***")
        assert payload["safe_field"] == "普通文本"

    def test_logs_redacts_credential_shapes(self, client):
        """凭证形态（sk-/AKIA/JWT/私钥）即便在普通键名下也须脱敏。"""
        self._seed(client, [
            {"timestamp": "t", "level": "INFO", "event": "e", "logger": "l",
             "note": "sk-live12345678901234",
             "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
             "ak": "AKIAIOSFODNN7EXAMPLE"},
        ])
        resp = self._get(client)
        assert resp.status_code == 200
        text = resp.text
        assert "sk-live12345678901234" not in text
        assert "eyJhbGci" not in text
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        payload = resp.json()["items"][0]["payload"]
        assert payload["note"] == "***"
        assert payload["jwt"] == "***"
        assert payload["ak"] == "***"

    def test_logs_truncates_long_fields(self, client):
        """10.6 L1193：外部字符串长度限制（单字段上限 1000）。"""
        long_value = "x" * 5000
        self._seed(client, [
            {"timestamp": "t", "level": "INFO", "event": "e", "logger": "l",
             "note": long_value},
        ])
        resp = self._get(client)
        assert resp.status_code == 200
        value = resp.json()["items"][0]["payload"]["note"]
        assert len(value) == 1000 + len("…(truncated)")
        assert value.endswith("…(truncated)")

    def test_logs_level_filter(self, client):
        self._seed(client, [
            {"timestamp": "t", "level": "INFO", "event": "info1", "logger": "l"},
            {"timestamp": "t", "level": "ERROR", "event": "err1", "logger": "l"},
            {"timestamp": "t", "level": "ERROR", "event": "err2", "logger": "l"},
        ])
        resp = self._get(client, level="ERROR")
        body = resp.json()
        assert body["total"] == 2
        assert {e["event"] for e in body["items"]} == {"err1", "err2"}
        # 小写过滤值同样生效
        resp = self._get(client, level="info")
        assert resp.json()["total"] == 1

    def test_logs_invalid_level_422(self, client):
        """非法 level 过滤值 → 422 VALIDATION_ERROR（2.7.3 稳定契约）。"""
        client.app.state.log_ring = LogRing()
        resp = self._get(client, level="TRACE")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value

    def test_logs_limit_validation(self, client):
        client.app.state.log_ring = LogRing()
        assert self._get(client, limit=0).status_code == 422
        assert self._get(client, limit=201).status_code == 422
        assert self._get(client, limit=1).status_code == 200

    def test_logs_no_session_401_and_manager_403(self, client):
        client.app.state.log_ring = LogRing()
        assert client.get("/api/v1/developer/logs").status_code == 401
        resp = client.get("/api/v1/developer/logs", cookies=MGR)
        assert resp.status_code == 403
        assert resp.json()["error_code"] == ErrorCode.FORBIDDEN.value


class TestDeveloperDeniedOnBusinessApis:
    """10.5：developer 访问业务 API 一律 403（chat/tasks/reviews/audit/mappings）。"""

    def test_developer_denied_chat(self, client):
        resp = client.post(
            "/api/v1/chat/messages",
            json={"message": "查询今天的订单"},
            headers={"Origin": "http://127.0.0.1:5173"},
            cookies=DEV,
        )
        assert resp.status_code == 403
        assert resp.json()["error_code"] == ErrorCode.FORBIDDEN.value

    def test_developer_denied_tasks(self, client):
        resp = client.get("/api/v1/tasks", cookies=DEV)
        assert resp.status_code == 403
        assert resp.json()["error_code"] == ErrorCode.FORBIDDEN.value

    def test_developer_denied_reviews(self, client):
        resp = client.get("/api/v1/reviews", cookies=DEV)
        assert resp.status_code == 403

    def test_developer_denied_audit(self, client):
        resp = client.get("/api/v1/audit", cookies=DEV)
        assert resp.status_code == 403

    def test_developer_denied_mappings(self, client):
        resp = client.get("/api/v1/mappings", cookies=DEV)
        assert resp.status_code == 403

    def test_manager_denied_developer_endpoints(self, client):
        """10.5 双向：manager 访问 developer 端点同样 403（已在上文验证 health，
        此处验证 logs）。"""
        client.app.state.log_ring = LogRing()
        resp = client.get("/api/v1/developer/logs", cookies=MGR)
        assert resp.status_code == 403
