"""R2 业务默认值（规格 2.6 business/defaults.py：当天/套/重量 0 等）。

5.1/mcp-api.md §6 字段默认值集中定义，供 order_rules 补全流程引用：
- quantity_unit 未提供时默认"套"；
- weight_per_unit 未提供时写 0（0 仅表示待补充，14.3 场景 3）；
- in_date 未提供时默认业务当天；
- status 创建时固定"未完成"。

纯规则层（2.6 规则 4）：无 IO。business_today 允许注入 now 便于确定性测试。
"""

from __future__ import annotations

from datetime import date

#: 数量单位（mcp-api.md §6：套/个；5.1 未提供默认"套"）
QUANTITY_UNITS: tuple[str, ...] = ("套", "个")
DEFAULT_QUANTITY_UNIT = "套"

#: 重量单位枚举（mcp-api.md §6：kg/m²）。注意：只做枚举对齐，
#: 不设默认值——5.1 未提供时从客户资料查询，资料亦缺失时由客户补全
#: 流程询问（order_rules.ERR_CUSTOMER_PROFILE_MISSING），defaults
#: 不兜底重量单位，避免静默选 kg。
WEIGHT_UNITS: tuple[str, ...] = ("kg", "m²")

#: 价格类型（mcp-api.md §6：含税/不含税）
PRICE_TYPES: tuple[str, ...] = ("含税", "不含税")

#: 创建时固定状态（5.1：status 创建时固定"未完成"）
DEFAULT_STATUS = "未完成"

#: 重量缺失时的写入值（5.1/14.3 场景 3：缺重量写 0；mcp-api.md §6：
#: 0 仅表示待补充，不视为实际重量）
MISSING_WEIGHT_PER_UNIT = 0


def business_today(now: date | None = None) -> str:
    """业务当天（ISO 日期，5.1：in_date 未提供时默认业务当天）。

    now 供测试注入固定日期；缺省取系统当天（调用方按业务时区传入）。
    """
    return (now or date.today()).isoformat()
