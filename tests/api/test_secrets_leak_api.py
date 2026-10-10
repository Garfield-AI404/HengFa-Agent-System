"""密钥泄漏运行时测试（backend 侧；规格 14.4 L1415 / 12.4 L1305）。

- 审计端点响应体（含敏感字段/凭证形态）不得输出任何凭据明文；
- developer/logs 响应体不得输出任何凭据明文（含敏感键、凭证形态）；
- 统一用 log_capture._CREDENTIAL_VALUE_RE 与敏感键值双重断言，
  与脱敏实现共用单一事实来源。
"""

from __future__ import annotations

import re

from administrative_agent.auth.models import Role
from administrative_agent.observability.log_capture import (
    LogRing,
    _CREDENTIAL_VALUE_RE,
)

from tests.api.conftest import DEVELOPER, MGR, cookies, headers

DEV = cookies(Role.DEVELOPER, account=DEVELOPER)

#: 敏感键值形态（redact_value 输出占位；防止响应泄漏原文）
_REDACTED_PREFIXES = ("***",)


def _assert_no_credentials_in_text(text: str, context: str) -> None:
    assert not _CREDENTIAL_VALUE_RE.search(text), (
        f"{context} 响应泄漏凭据形态：{_CREDENTIAL_VALUE_RE.search(text).group(0)!r}"
    )
    for secret in (
        "super-secret-credential",
        "sk-live-1234567890",
        "test-session-secret-not-for-production",
    ):
        assert secret not in text, f"{context} 响应泄漏明文：{secret!r}"


def _seed_audit(client) -> None:
    from administrative_agent.audit.service import AuditService
    from administrative_agent.main import app
    from administrative_agent.persistence.repositories import (
        SqlAlchemyAttemptRepository,
        SqlAlchemyAuditRepository,
    )

    audit = AuditService(
        SqlAlchemyAuditRepository(app.state.session_factory),
        SqlAlchemyAttemptRepository(app.state.session_factory),
    )
    audit.record_event(
        event_type="query",
        trace_id="trace-secret-leak-0001",
        actor_account="manager01",
        actor_role="manager",
        task_id="task-secret-leak-0001",
        operation_id="op-secret-leak-0001",
        duty_code="R4",
        target_type="order",
        request_summary="query with credentials",
        structured_payload={
            "password": "super-secret-credential",
            "nested": {"api_key": "sk-live-1234567890"},
        },
    )


class TestAuditResponseNoCredentials:
    """14.4 L1415：业务审计响应不输出凭据。"""

    def test_audit_response_has_no_credential_plaintext(self, client):
        _seed_audit(client)
        resp = client.get("/api/v1/audit", cookies=MGR, headers=headers())
        assert resp.status_code == 200
        _assert_no_credentials_in_text(resp.text, "audit")

    def test_audit_redacts_values_not_just_shapes(self, client):
        """敏感键值即使不符合凭证形态也必须脱敏（11.2 规则 4）。"""
        _seed_audit(client)
        payload = client.get("/api/v1/audit", cookies=MGR, headers=headers()).json()[
            "items"
        ][0]["structured_payload"]
        assert payload["password"].startswith(_REDACTED_PREFIXES)
        assert payload["nested"]["api_key"].startswith(_REDACTED_PREFIXES)


class TestLogsResponseNoCredentials:
    """14.4 L1415：developer/logs 响应不输出凭据。"""

    def _seed(self, client, records):
        ring = LogRing()
        for r in records:
            ring.append(r)
        client.app.state.log_ring = ring

    def test_logs_response_has_no_credential_plaintext(self, client):
        self._seed(client, [
            {"timestamp": "t", "level": "INFO", "event": "e", "logger": "l",
             "db_password": "super-secret-credential",
             "note": "sk-live12345678901234",
             "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"},
        ])
        resp = client.get("/api/v1/developer/logs", cookies=DEV)
        assert resp.status_code == 200
        _assert_no_credentials_in_text(resp.text, "developer/logs")
        # 敏感键值输出占位而非原样
        payload = resp.json()["items"][0]["payload"]
        assert payload["db_password"].startswith(_REDACTED_PREFIXES)

    def test_logs_redacts_even_without_configured_logging(self, client):
        """纵深防御：注入环不依赖 structlog 全局配置，读路径二次脱敏。"""
        self._seed(client, [
            {"timestamp": "t", "level": "ERROR", "event": "boom",
             "logger": "test", "ak": "AKIAIOSFODNN7EXAMPLE"},
        ])
        resp = client.get("/api/v1/developer/logs", cookies=DEV)
        assert resp.status_code == 200
        assert "AKIAIOSFODNN7EXAMPLE" not in resp.text
        assert resp.json()["items"][0]["payload"]["ak"] == "***"


class TestHealthResponseNoCredentials:
    def test_health_response_has_no_credential_plaintext(self, client, monkeypatch):
        monkeypatch.setattr(
            "administrative_agent.config.settings.agent_database_url",
            "postgresql://user:SuperSecret123@db.internal:5432/agent",
        )
        monkeypatch.setattr(
            "administrative_agent.config.settings.erp_mcp_api_key", "sk-live12345678901234",
        )
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        _assert_no_credentials_in_text(resp.text, "developer/health")
