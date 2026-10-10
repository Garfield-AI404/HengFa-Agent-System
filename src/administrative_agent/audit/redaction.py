"""日志和网页脱敏（规格 2.6 audit/redaction.py；11.2 规则 4、14.4、L1415）。

- 密码、API Key、Token、会话密钥等敏感字段**不进入**审计/日志/网页；
  本模块提供键名识别与递归脱敏。
- 脱敏不是哈希：审计不需要恢复原始值（恢复原始值走密钥管理/ERP 侧）；
  一律以 ``***`` 占位并保留键名（便于审计阅读字段来源）。
- 10.6 外部字符串长度限制属于网页输出层（D21），本模块不截断文本，
  只处理敏感信息。
"""

from __future__ import annotations

import re
from typing import Any

#: 敏感键名（小写匹配，11.2 规则 4：密码/API Key/令牌；14.4 凭据纪律）。
#: 键名包含以下任一词即视为敏感（如 ``db_password``、``x_api_key``）。
_SENSITIVE_WORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "authorization",
    "credential",
    "私钥",
    "密码",
    "密钥",
)

_SENSITIVE_WORDS_RE = re.compile(
    "|".join(re.escape(w) for w in _SENSITIVE_WORDS), re.IGNORECASE
)

#: 脱敏占位值。
REDACTED = "***"


def is_sensitive_key(key: str) -> bool:
    """键名是否为敏感字段（大小写不敏感、词内匹配）。"""
    return bool(_SENSITIVE_WORDS_RE.search(key))


def redact_value(value: Any) -> Any:
    """敏感字段值脱敏：非字符串统一占位；字符串显示形态与长度。"""
    if isinstance(value, str):
        if not value:
            return ""
        return f"{REDACTED}(len={len(value)})"
    return REDACTED


def redact_mapping(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """递归脱敏 dict（list 逐项递归；敏感键值脱敏，其余原样）。

    返回新 dict，不改动入参；嵌套 dict/list 同样处理。非 dict/list
    叶子值原样返回（脱敏只针对敏感键）。
    """
    if not mapping:
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if is_sensitive_key(key):
            out[key] = redact_value(value)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [redact_item(v, key) for v in value]
        else:
            out[key] = value
    return out


def redact_item(value: Any, parent_key: str) -> Any:
    """list 中单个元素脱敏：元素为 dict 递归；父键敏感时整项占位。"""
    if is_sensitive_key(parent_key):
        return REDACTED
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_item(v, parent_key) for v in value]
    return value
