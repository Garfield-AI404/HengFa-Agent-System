"""redaction 脱敏测试（D8/RV-11：11.2 规则 4、14.4 凭据纪律、L1415）。"""

from __future__ import annotations

from administrative_agent.audit.redaction import (
    is_sensitive_key,
    redact_mapping,
    redact_value,
)


def test_sensitive_key_detection() -> None:
    for key in ("password", "api_key", "API_KEY", "x-api-key", "db_password",
                "Authorization", "token", "secret", "accessToken"):
        assert is_sensitive_key(key), key
    for key in ("name", "status", "order_no", "employee_id", "remark"):
        assert not is_sensitive_key(key), key


def test_redact_value_forms() -> None:
    assert redact_value("s3cr3t") == "***(len=6)"
    assert redact_value("") == ""
    assert redact_value(12345) == "***"
    assert redact_value(None) == "***"


def test_redact_mapping_scalars() -> None:
    out = redact_mapping({"password": "hunter2", "order_no": "SO-1", "status": "submitted"})
    assert out["password"].startswith("***")
    assert out["order_no"] == "SO-1"
    assert out["status"] == "submitted"


def test_redact_mapping_nested_dict() -> None:
    out = redact_mapping({"conn": {"api_key": "k", "host": "10.0.0.1"}})
    assert out["conn"]["api_key"].startswith("***")
    assert out["conn"]["host"] == "10.0.0.1"


def test_redact_mapping_list_items() -> None:
    out = redact_mapping({"items": [{"token": "t1", "name": "a"}, {"token": "t2", "name": "b"}]})
    assert out["items"][0]["token"].startswith("***")
    assert out["items"][0]["name"] == "a"
    assert out["items"][1]["token"].startswith("***")


def test_redact_mapping_sensitive_parent_key_whole_item() -> None:
    """父键本身敏感（如 credentials 列表）：整个值占位，不泄漏子字段与结构。"""
    out = redact_mapping({"credentials": [{"password": "x", "user": "u"}]})
    assert out["credentials"] == "***"
    assert "x" not in repr(out)


def test_redact_mapping_no_mutation_of_input() -> None:
    original = {"api_key": "k", "nested": {"secret": "s"}}
    out = redact_mapping(original)
    assert original["api_key"] == "k"
    assert original["nested"]["secret"] == "s"
    assert out is not original


def test_redact_mapping_empty() -> None:
    assert redact_mapping(None) == {}
    assert redact_mapping({}) == {}


def test_sensitive_chinese_keys() -> None:
    out = redact_mapping({"密码": "123456", "备注": "ok"})
    assert out["密码"].startswith("***")
    assert out["备注"] == "ok"
