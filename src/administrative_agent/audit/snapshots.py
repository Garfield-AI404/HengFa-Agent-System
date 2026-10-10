"""写前/写后快照（规格 2.6 audit/snapshots.py；11.1 before/after_snapshot、
6.5/L852：审计保存差异摘要和操作依据，不保存完整原始文件）。

- ``snapshot_payload``：按字段白名单生成结构化快照（只保存审计需要的
  关键业务字段），并在落库前经 redaction 脱敏——不保存完整原始文件、
  不保存无关工作表内容（6.5）、不保存密码/API Key/完整原始文件（11.2 规则 4）。
- ``diff_summary``：写前/写后差异摘要，只含有差异的字段（操作依据），
  供 attempt 记录 field_diffs 与审计阅读。
"""

from __future__ import annotations

from typing import Any, Iterable

from administrative_agent.audit.redaction import redact_mapping

#: 快照可选字段白名单（调用方也可显式传入，缺省不限制——由业务层决定
#: 保存哪些字段；本模块负责脱敏与摘要）。
_ALLOW_ALL: Iterable[str] | None = None


def snapshot_payload(
    data: dict[str, Any] | None,
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """生成脱敏后的结构化快照。

    - ``keys`` 白名单：只保留列出的字段（6.5 不保存无关内容；缺省保存
      全部键但同样脱敏——业务层应尽量显式传 keys）；
    - 返回值经 redaction 脱敏，敏感键为 ``***`` 占位。
    """
    if not data:
        return {}
    if keys is None:
        selected = dict(data)
    else:
        allowed = set(keys)
        selected = {k: v for k, v in data.items() if k in allowed}
    return redact_mapping(selected)


def diff_summary(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """写前/写后差异摘要：只含有差异的字段（操作依据，11.1）。

    值为 ``(before, after)`` 二元组（均已是脱敏快照）；before 无而 after
    有视为新增（before=None）；after 无视为删除（after=None）。
    无差异返回空 dict。
    """
    before = before or {}
    after = after or {}
    diffs: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diffs[key] = (old, new)
    return diffs


def snapshot_diff_bundle(
    before_raw: dict[str, Any] | None,
    after_raw: dict[str, Any] | None,
    *,
    keys: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """一次完成：写前快照 + 写后快照 + 差异摘要（三者均脱敏）。

    返回 ``(before_snapshot, after_snapshot, diff_summary)``，供
    11.1 审计事件 before/after_snapshot 与 11.2 attempt field_diffs 使用。
    """
    before = snapshot_payload(before_raw, keys=keys)
    after = snapshot_payload(after_raw, keys=keys)
    return before, after, diff_summary(before, after)
