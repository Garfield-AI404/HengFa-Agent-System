"""R2 业务默认值纯函数测试（RV-19；规格 2.6 business/defaults.py、
5.1 钣金字段表、mcp-api.md §6）。

覆盖：数量/重量单位、价格类型、固定状态、缺失重量写 0、
业务当天（默认取系统当天 / 注入固定日期）。
"""

from __future__ import annotations

from datetime import date

from administrative_agent.business.defaults import (
    DEFAULT_QUANTITY_UNIT,
    DEFAULT_STATUS,
    MISSING_WEIGHT_PER_UNIT,
    PRICE_TYPES,
    QUANTITY_UNITS,
    WEIGHT_UNITS,
    business_today,
)


def test_quantity_units_match_mcp_spec() -> None:
    """数量单位集合（mcp-api.md §6：套/个）与默认"套"。"""
    assert QUANTITY_UNITS == ("套", "个")
    assert DEFAULT_QUANTITY_UNIT == "套"


def test_weight_units_match_mcp_spec() -> None:
    """重量单位枚举对齐（mcp-api.md §6：kg/m²）。

    注意：只做枚举对齐、不设默认值——重量单位缺失时 defaults 不兜底
    选 kg，由客户补全流程询问（order_rules.ERR_CUSTOMER_PROFILE_MISSING），
    避免静默选 kg（defaults 模块无 DEFAULT_WEIGHT_UNIT 常量）。
    """
    assert WEIGHT_UNITS == ("kg", "m²")


def test_price_types_match_mcp_spec() -> None:
    """价格类型集合（mcp-api.md §6：含税/不含税）。"""
    assert PRICE_TYPES == ("含税", "不含税")


def test_status_fixed_to_unfinished() -> None:
    """5.1：创建时 status 固定"未完成"。"""
    assert DEFAULT_STATUS == "未完成"


def test_missing_weight_writes_zero() -> None:
    """5.1/14.3 场景 3：缺重量写 0（0 仅表示待补充）。"""
    assert MISSING_WEIGHT_PER_UNIT == 0


def test_business_today_defaults_to_system_today() -> None:
    """默认取系统当天（ISO 日期）。"""
    assert business_today() == date.today().isoformat()


def test_business_today_accepts_injected_date() -> None:
    """注入 now → 返回注入日期（确定性测试）。"""
    assert business_today(date(2026, 8, 22)) == "2026-08-22"
