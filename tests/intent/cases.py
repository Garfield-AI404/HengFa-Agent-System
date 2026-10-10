"""RV-12 真实表达用例表（≥30 条；61 条）。

每条用例是"用户会说的话"（非构造性关键词串），断言期望的 10.2 六字段
输出与询问/拒绝语义。分类器不变量：全部用例输出必须通过 validate_intent
（test_classifier.py 中统一断言）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    """真实表达用例：text → 期望结构化意图。"""

    text: str
    duty: str
    operation: str | None = None
    business_domain: str | None = None
    order_type: str | None = None
    confidence: float = 0.97
    missing: tuple[str, ...] = ()
    should_ask: bool = False
    #: unsupported / 未识别：拒绝或澄清说明应包含的关键内容
    reason_contains: str | None = None
    group: str = ""


# R1 每日考勤（7.2 R1 直接执行；1.3 职责 1）
R1_CASES: list[Case] = [
    Case("帮我记录今天的考勤", "R1", "record_daily_attendance", "attendance",
         group="R1"),
    Case("张三今天迟到了，补记考勤", "R1", "record_daily_attendance", "attendance",
         group="R1"),
    Case("记录一下李四昨天的加班", "R1", "record_daily_attendance", "attendance",
         group="R1"),
    Case("今天大家的打卡情况怎么样", "R1", "record_daily_attendance", "attendance",
         group="R1"),
    Case("李四今天请了假，考勤记得更新", "R1", "record_daily_attendance",
         "attendance", group="R1"),
    Case("补录上周五的考勤", "R1", "record_daily_attendance", "attendance",
         group="R1"),
    Case("今天王五迟到 10 分钟，帮我记一下", "R1", "record_daily_attendance",
         "attendance", group="R1"),
]

# R2 订单创建（7.2：明确指定直接执行否则询问）
R2_METAL_CASES: list[Case] = [
    Case("创建一个新的钣金订单", "R2", "create_order", "order", "metal",
         group="R2-metal"),
    Case("新增一张钢板订单", "R2", "create_order", "order", "metal",
         group="R2-metal"),
    Case("下个折弯件的订单", "R2", "create_order", "order", "metal",
         group="R2-metal"),
    Case("创建激光切割订单", "R2", "create_order", "order", "metal",
         group="R2-metal"),
    Case("录入一张冲压件订单", "R2", "create_order", "order", "metal",
         group="R2-metal"),
]

R2_POWDER_CASES: list[Case] = [
    Case("创建一个塑粉订单", "R2", "create_order", "order", "powder",
         group="R2-powder"),
    Case("新增粉末喷涂订单", "R2", "create_order", "order", "powder",
         group="R2-powder"),
    Case("录入塑粉订单，供货商是恒泰", "R2", "create_order", "order", "powder",
         group="R2-powder"),
    Case("下一张喷粉订单", "R2", "create_order", "order", "powder",
         group="R2-powder"),
    Case("创建静电喷涂订单", "R2", "create_order", "order", "powder",
         group="R2-powder"),
]

# R2 歧义：无法区分钣金/塑粉 → missing_discriminator、询问、不加载规则
R2_AMBIGUOUS_CASES: list[Case] = [
    Case("创建一个新订单", "R2", "create_order", "order", None, 0.75,
         ("order_type",), True, group="R2-ambiguous"),
    Case("新增订单，客户是华东机械", "R2", "create_order", "order", None, 0.75,
         ("order_type",), True, group="R2-ambiguous"),
    Case("帮我们下个订单", "R2", "create_order", "order", None, 0.75,
         ("order_type",), True, group="R2-ambiguous"),
    Case("开一张新订单", "R2", "create_order", "order", None, 0.75,
         ("order_type",), True, group="R2-ambiguous"),
]

# R3 订单补充/更正/状态更新（7.2：无类型提示不询问，D10/D11+ 校验兜底）
R3_CASES: list[Case] = [
    Case("修改订单 SO-1001 的订单状态", "R3", "update_order", "order",
         group="R3"),
    Case("更新钣金订单 SO-1002 的状态", "R3", "update_order", "order", "metal",
         group="R3"),
    Case("更正塑粉订单的送货地址", "R3", "update_order", "order", "powder",
         group="R3"),
    Case("把 SO-1003 的订单状态改成已提交", "R3", "update_order", "order",
         group="R3"),
    Case("补充订单 SO-1004 的重量信息", "R3", "update_order", "order",
         group="R3"),
    Case("订单 SO-1005 的状态更新为已发货", "R3", "update_order", "order",
         group="R3"),
]

# R4 查询（7.2：直接执行）
R4_CASES: list[Case] = [
    Case("查询订单 SO-1005 的金额", "R4", "query", "query", None, 0.95,
         group="R4"),
    Case("查一下客户华东机械的订单", "R4", "query", "query", None, 0.95,
         group="R4"),
    Case("找出所有今天创建的订单", "R4", "query", "query", None, 0.95,
         group="R4"),
    Case("看看有哪些待发货的订单", "R4", "query", "query", None, 0.95,
         group="R4"),
    Case("导出 8 月的订单明细", "R4", "query", "query", None, 0.95,
         group="R4"),
    Case("列出塑粉订单列表", "R4", "query", "query", None, 0.95,
         group="R4"),
]

# R5 分析（7.2：直接执行；强词 分析/趋势/对比/报表/报告 优先于 R1/R4）
R5_CASES: list[Case] = [
    Case("分析一下各客户的订单金额趋势", "R5", "analyze", "analysis",
         group="R5"),
    Case("对比上季度和本季度的销售额", "R5", "analyze", "analysis",
         group="R5"),
    Case("生成一份季度考勤分析报告", "R5", "analyze", "analysis",
         group="R5"),
    Case("统计本月各客户订单量", "R5", "analyze", "analysis", None, 0.9,
         group="R5"),
    Case("汇总一下订单金额", "R5", "analyze", "analysis", None, 0.9,
         group="R5"),
    Case("上个月订单总额是多少", "R5", "analyze", "analysis", None, 0.9,
         group="R5"),
    Case("平均每天多少订单", "R5", "analyze", "analysis", None, 0.9,
         group="R5"),
]

# R6 Excel 对账（7.2：差异报告只读直接执行；差异写入 manager 复核）
R6_CASES: list[Case] = [
    Case("核对一下 8 月的订单", "R6", "reconcile_plan", "reconciliation",
         None, 0.95, group="R6"),
    Case("生成订单差异报告", "R6", "reconcile_plan", "reconciliation",
         None, 0.95, group="R6"),
    Case("盘点一下仓库订单", "R6", "reconcile_plan", "reconciliation",
         None, 0.95, group="R6"),
    Case("导入 Excel 订单对账", "R6", "reconcile_plan", "reconciliation",
         None, 0.95, group="R6"),
    Case("执行对账差异写入", "R6", "reconcile_execute", "reconciliation",
         None, 0.9, group="R6"),
    Case("把对账差异写入系统", "R6", "reconcile_execute", "reconciliation",
         None, 0.9, group="R6"),
    Case("审批这份对账差异", "R6", "reconcile_plan", "reconciliation",
         None, 0.95, group="R6"),
]

# unsupported：明确不做（1.4 / L1408 第 13 条验收项）
UNSUPPORTED_CASES: list[Case] = [
    Case("帮我报销这个月的打车费", "unsupported", None, None, None, 1.0,
         reason_contains="报销", group="unsupported"),
    Case("审批一下小李的请假申请", "unsupported", None, None, None, 1.0,
         reason_contains="审批", group="unsupported"),
    Case("帮我请假", "unsupported", None, None, None, 1.0,
         reason_contains="请假", group="unsupported"),
    Case("联系客户确认交期", "unsupported", None, None, None, 1.0,
         reason_contains="联系客户", group="unsupported"),
    Case("取消订单 SO-1001", "unsupported", None, None, None, 1.0,
         reason_contains="取消", group="unsupported"),
    Case("删除订单 SO-1002", "unsupported", None, None, None, 1.0,
         reason_contains="删除订单", group="unsupported"),
    Case("帮我把 SO-1003 作废", "unsupported", None, None, None, 1.0,
         reason_contains="作废", group="unsupported"),
    Case("排一下下周的生产计划", "unsupported", None, None, None, 1.0,
         reason_contains="排产", group="unsupported"),
    Case("给客户报个价", "unsupported", None, None, None, 1.0,
         reason_contains="报价", group="unsupported"),
    Case("创建两个新用户账号", "unsupported", None, None, None, 1.0,
         reason_contains="账号", group="unsupported"),
    Case("帮我转账", "unsupported", None, None, None, 1.0,
         reason_contains="转账", group="unsupported"),
]

# 未识别：低置信度 → 询问澄清（L1364 询问而非执行）
UNRECOGNIZED_CASES: list[Case] = [
    Case("你好", "unsupported", None, None, None, 0.4, (), True,
         reason_contains="无法识别", group="unrecognized"),
    Case("今天天气怎么样", "unsupported", None, None, None, 0.4, (), True,
         reason_contains="无法识别", group="unrecognized"),
    Case("帮我弄一下", "unsupported", None, None, None, 0.4, (), True,
         reason_contains="无法识别", group="unrecognized"),
]

ALL_CASES: list[Case] = (
    R1_CASES
    + R2_METAL_CASES
    + R2_POWDER_CASES
    + R2_AMBIGUOUS_CASES
    + R3_CASES
    + R4_CASES
    + R5_CASES
    + R6_CASES
    + UNSUPPORTED_CASES
    + UNRECOGNIZED_CASES
)
