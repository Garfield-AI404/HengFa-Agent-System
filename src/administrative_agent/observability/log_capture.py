"""进程内结构化日志环形缓冲（D23 developer/logs 数据源；规格 10.6/11.4）。

- ``capture_processor`` 注册为 structlog processor（main.py 全局配置）：
  每条日志事件追加到有界环形缓冲（默认 500 条，超限淘汰最旧）；
- 只捕获时间戳/级别/事件名/日志器名与结构化键值——**不保存完整对话、
  模型思考、完整文件内容**（11.4 ERP 日志边界：Agent 业务审计与 ERP
  技术日志通过 operation_id/trace_id 关联；chat 编排只记 task_id/
  operation_id，不记原文）；
- ``read_logs`` 读取时应用纵深防御：
  * 脱敏（复用 audit/redaction.py 的 is_sensitive_key/redact_value，
    11.2 规则 4：日志不得输出密码/API Key/密钥）；
  * 值形态探测（sk-…/AKIA/ghp_…/JWT 等凭证格式 → 占位），防敏感键名
    缺失时的漏网；
  * 外部字符串长度限制（10.6 L1193：日志输出中的外部字符串做长度限制）；
- 内存实现（原型声明）：结构化日志同时输出 stdout（JSON），本缓冲只服务
  developer/logs 端点；生产持久化与检索属 D25 部署。
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Callable

from administrative_agent.audit.redaction import is_sensitive_key, redact_value

#: 缓冲上限（条）；超限淘汰最旧日志
RING_MAXLEN = 500

#: 单字段字符串长度上限（10.6 L1193 外部字符串长度限制）
MAX_FIELD_LEN = 1000

_TRUNCATED_SUFFIX = "…(truncated)"

#: 凭证形态探测（12.4 L1305/14.4 L1415：日志不得输出凭据）。
#: 覆盖常见密钥/AK 前缀与 JWT（头部+载荷两段）。
_CREDENTIAL_VALUE_RE = re.compile(
    r"sk-[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
)

#: 允许的日志级别（过滤器取值）
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class LogRing:
    """有界环形缓冲（线程安全足够：append 单条原子）。"""

    def __init__(self, maxlen: int = RING_MAXLEN) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def append(self, record: dict[str, Any]) -> None:
        self._records.append(record)

    def snapshot(self) -> list[dict[str, Any]]:
        """最新在前（list 复制，避免迭代期间被淘汰）。"""
        return list(reversed(self._records))

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


#: 全局默认缓冲（main.py 注册 processor 时使用；测试可注入自建 ring）
ring = LogRing()


def capture_processor(buffer: LogRing | None = None) -> Callable[..., Any]:
    """structlog processor：追加事件 dict 到缓冲后原样放行。

    用法（main.py _configure_logging）：
        processors = [
            TimeStamper(fmt="iso"), add_log_level,
            capture_processor(ring), StackInfoRenderer(),
            format_exc_info, JSONRenderer(ensure_ascii=False),
        ]
    """
    # 显式判 None（不能用 ``or``：LogRing 定义了 __len__，空 ring falsy，
    # 注入的空缓冲会被误判为缺失而回退到全局 ring——顺序相关缺陷）
    target = buffer if buffer is not None else ring

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        target.append(dict(event_dict))
        return event_dict

    return _capture


def sanitize(value: Any) -> Any:
    """日志值纵深脱敏：敏感键脱敏 + 凭证形态探测 + 长度限制。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                out[key] = redact_value(item)
            elif isinstance(item, (dict, list)):
                out[key] = sanitize(item)
            else:
                out[key] = _sanitize_scalar(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return _sanitize_scalar(value)


def _sanitize_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if _CREDENTIAL_VALUE_RE.search(value):
        return "***"
    if len(value) > MAX_FIELD_LEN:
        return value[:MAX_FIELD_LEN] + _TRUNCATED_SUFFIX
    return value


def read_logs(
    buffer: LogRing | None = None,
    *,
    limit: int = 50,
    level: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """读取脱敏日志（最新在前）。

    level 过滤（不区分大小写，先验校验由路由层 422）；返回
    (items, total)——total 为过滤后总条数（供前端分页展示）。
    """
    # 显式判 None（不能用 ``or``：LogRing 定义了 __len__，空 ring falsy，
    # 注入的空缓冲会被误判为缺失而回退到全局 ring——顺序相关缺陷，
    # 见 test_developer_api 的 test_logs_contract）
    target = buffer if buffer is not None else ring
    records = target.snapshot()
    if level:
        level_upper = level.upper()
        records = [r for r in records if str(r.get("level", "")).upper() == level_upper]
    total = len(records)
    return [sanitize(r) for r in records[:limit]], total


def sanitize_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor：日志值纵深脱敏（敏感键脱敏 + 凭证形态探测 +
    长度限制；复用 sanitize）。幂等——对已脱敏值重复执行无害，可安全地
    出现在 processor 链中多个位置（写缓冲前 + JSONRenderer 前）。"""
    return sanitize(event_dict)
