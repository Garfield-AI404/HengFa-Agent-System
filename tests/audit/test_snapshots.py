"""写前/写后快照测试（D8/RV-11：6.5/L852 不保存完整原始文件、脱敏）。"""

from __future__ import annotations

from administrative_agent.audit.snapshots import (
    diff_summary,
    snapshot_diff_bundle,
    snapshot_payload,
)


def test_snapshot_payload_keys_whitelist() -> None:
    raw = {"status": "draft", "remark": "r", "amount": 100}
    out = snapshot_payload(raw, keys=("status", "amount"))
    assert out == {"status": "draft", "amount": 100}


def test_snapshot_payload_no_keys_keeps_all_but_redacts() -> None:
    raw = {"order_no": "SO-1", "api_key": "k"}
    out = snapshot_payload(raw)
    assert out["order_no"] == "SO-1"
    assert out["api_key"].startswith("***")


def test_snapshot_payload_empty() -> None:
    assert snapshot_payload(None) == {}
    assert snapshot_payload({}) == {}


def test_diff_summary_only_changed_fields() -> None:
    before = {"status": "draft", "remark": "a", "amount": 100}
    after = {"status": "submitted", "remark": "a", "amount": 100}
    assert diff_summary(before, after) == {"status": ("draft", "submitted")}


def test_diff_summary_added_and_removed() -> None:
    before = {"status": "draft"}
    after = {"status": "submitted", "checked_by": "u1"}
    diffs = diff_summary(before, after)
    assert diffs["status"] == ("draft", "submitted")
    assert diffs["checked_by"] == (None, "u1")
    diffs2 = diff_summary({"extra": "x"}, {})
    assert diffs2["extra"] == ("x", None)


def test_diff_summary_no_diff_empty() -> None:
    assert diff_summary({"a": 1}, {"a": 1}) == {}
    assert diff_summary(None, None) == {}


def test_snapshot_diff_bundle_bundle_shape() -> None:
    before_raw = {"status": "draft", "api_key": "s3cr3t"}
    after_raw = {"status": "submitted", "api_key": "s3cr3t", "checked_by": "u1"}
    before, after, diffs = snapshot_diff_bundle(
        before_raw, after_raw, keys=("status", "checked_by", "api_key")
    )
    # 三件套均脱敏：敏感键不出现明文
    assert "s3cr3t" not in repr(before) and "s3cr3t" not in repr(after)
    assert before["status"] == "draft"
    assert after["status"] == "submitted"
    assert diffs == {
        "status": ("draft", "submitted"),
        "checked_by": (None, "u1"),
    }
