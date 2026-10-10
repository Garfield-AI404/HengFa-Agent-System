"""``web/stream_event_reader._iso_utc`` 纯函数单测（方案 §7.2 用例 17）。

为什么必须写成纯函数单测：**时区陷阱在 UTC 的 CI 机器上测不出来**。
PG 的 ``DateTime(timezone=True)`` 回 tz-aware，SQLite 回 **naive**；若读侧
原样输出不带偏移的字符串，前端 ``new Date('…T02:11:07')`` 会按**本地时区**
解析 → 时间线整体偏移 8 小时。集成测试跑在 UTC 上时两边恰好相等，
只有直接喂 naive datetime 才能稳定钉住这个行为。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from administrative_agent.web.stream_event_reader import _iso_utc


def test_iso_utc_naive_is_treated_as_utc() -> None:
    """naive（SQLite 路径）→ 必须补出 ``+00:00`` 偏移，且**值不变**。"""
    naive = datetime(2026, 9, 18, 2, 11, 7, 421337)
    out = _iso_utc(naive)
    assert out == "2026-09-18T02:11:07.421337+00:00"
    assert out.endswith("+00:00")


def test_iso_utc_aware_utc_unchanged() -> None:
    """tz-aware UTC（PG 路径）→ 逐字符不变。"""
    aware = datetime(2026, 9, 18, 2, 11, 7, tzinfo=timezone.utc)
    assert _iso_utc(aware) == "2026-09-18T02:11:07+00:00"


def test_iso_utc_aware_non_utc_is_converted() -> None:
    """tz-aware 非 UTC → 归一为 UTC（同一时刻，不是同一墙上时间）。"""
    cst = timezone(timedelta(hours=8))
    aware = datetime(2026, 9, 18, 10, 11, 7, tzinfo=cst)
    assert _iso_utc(aware) == "2026-09-18T02:11:07+00:00"


def test_iso_utc_none_passthrough() -> None:
    assert _iso_utc(None) is None


def test_iso_utc_non_datetime_passthrough() -> None:
    """非法/非 datetime 值原样返回（不猜、不改写）。"""
    assert _iso_utc("2026-09-18T02:11:07") == "2026-09-18T02:11:07"
    assert _iso_utc(0) == 0
