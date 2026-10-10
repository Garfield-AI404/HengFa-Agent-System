"""会话循环图工具注册表（Phase 2：真实 Executor 接线）。

基础类型与共享查询辅助位于 base.py，域实现位于 attendance.py（R1）/orders.py（R2/R3）/
reconciliation.py（R6）/finance.py（R7）；本模块组装
``TOOL_REGISTRY`` 并 re-export 全部公开符号——``from
administrative_agent.agent.tools import ...`` 外部导入路径保持不变。

七个写工具（考勤/订单创建/订单修改/对账写入/收支创建/收支修改/映射模板
mapping_template）以 ``ToolSpec`` 登记进 ``TOOL_REGISTRY``，图内 tools
节点按模型 AgentDecision.tool_name 分派。
Phase 2 接线目标（domains/audit/tasks 内部零改动——各域文件只做参数适配
与调用）：prepare 做全部只读前置（ERP 实体查询/名称唯一匹配/资料补全/
计划生成/确认卡构造），执行上下文（draft/plan 序列化）与卡片一并写入
State.pending_confirmation；确认通过后 run 用同一执行上下文调真实
Executor（保证"批准的卡片"就是"执行的载荷"，payload_hash 变化即失效）。

- 七个写工具接线：
  * attendance_plan_execute → domains/attendance.py:189 AttendanceExecutor
    （execute(task_id, attendance_date, plan, employees)）；
  * order_create → domains/orders.py:343 MetalOrderExecutor
    （execute(task_id, draft=MetalOrderDraft)；塑粉复用 target_type=
    "powder_order"，5.2）；
  * order_update → domains/orders.py:881 OrderUpdateExecutor
    （execute(task_id, order_type, order_id, desired)）；
  * reconciliation_execute → domains/reconciliation.py:542
    ReconciliationExecuteExecutor（execute(task_id, plan, review, ...)）；
  * finance_create → domains/finance.py FinanceCreateExecutor
    （execute(task_id, payload)；通用 create 无幂等键 → 结果未知
    写后验证签名，不盲目重试）；
  * finance_update → domains/finance.py FinanceUpdateExecutor
    （execute(task_id, record_id, desired)；本地乐观锁 = 写前重查 +
    重建确认卡 hash 比对；expected_updated_at 仅审计列）；
  * mapping_template → tools/mapping_template.py prepare_mapping_template/
    run_mapping_template（create_draft/publish/retire 写本地映射配置表，
    不触 ERP；确认卡在 prepare 内构造）。
- **确认卡**：metal/update 复用 domains/orders.py 现有构造函数；考勤/塑粉/
  对账确认卡按 build_metal_confirmation_card 同构新建（六要素 +
  payload_hash_for），纯函数可单测；finance_update 确认卡共用
  domains/finance.py build_finance_update_confirmation_card（执行器重建
  hash 必须与 prepare 字节一致）；finance_create 确认卡在
  tools/finance.py（执行器不重建创建卡 hash，同考勤口径）。
- **审计接线**：policy 三列（key/version/hash）经 PolicyPackage 注入各
  Executor（PolicyInfo 协议，11.1）；attempt/result 由 Executor 内部保持
  （本模块不重复审计）。
- **fail closed 纪律**：prepare 失败（匹配歧义/未命中/查询失败/资料缺失）
  → ToolPreparationError → 图内转错误 ToolMessage（不落 ERP，模型向用户
  说明后自纠）；Executor 抛错 → ok=False + 错误信息（不静默）。
"""

from administrative_agent.agent.tools.base import (
    SCOPE_ATTENDANCE_WRITE,
    SCOPE_FINANCE_CREATE,
    SCOPE_FINANCE_UPDATE,
    SCOPE_ORDER_CREATE,
    SCOPE_ORDER_UPDATE,
    SCOPE_QUERY,
    SCOPE_RECONCILE_EXECUTE,
    EventSink,
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
    ToolSpec,
)
from administrative_agent.agent.tools.attendance import (
    AttendanceExceptionArgs,
    AttendancePlanExecuteArgs,
    build_attendance_confirmation_card,
    execute_attendance,
    prepare_attendance,
)
from administrative_agent.agent.tools.orders import (
    OrderCreateArgs,
    OrderUpdateArgs,
    build_powder_confirmation_card,
    execute_order_create,
    execute_order_update,
    prepare_order_create,
    prepare_order_update,
)
from administrative_agent.agent.tools.finance import (
    FinanceCreateArgs,
    FinanceUpdateArgs,
    build_finance_create_confirmation_card,
    execute_finance_create,
    execute_finance_update,
    prepare_finance_create,
    prepare_finance_update,
)
from administrative_agent.agent.tools.reconciliation import (
    ReconciliationExecuteArgs,
    ReconcileAnalyzeArgs,
    ReconcileConfirmArgs,
    build_reconciliation_confirmation_card,
    execute_reconciliation,
    prepare_reconciliation,
    prepare_reconcile_confirm,
    reconcile_analyze,
)
from administrative_agent.agent.tools.attachment_preview import (
    AttachmentPreviewArgs,
    attachment_preview,
)
from administrative_agent.agent.tools.mapping_template import (
    MappingColumnSpec,
    MappingTemplateArgs,
    prepare_mapping_template,
    run_mapping_template,
)
from administrative_agent.agent.tools.mapping_list import (
    MappingListArgs,
    run_mapping_list,
)
from administrative_agent.agent.tools.memory import MemoryLookupArgs, run_memory_lookup
from administrative_agent.agent.tools.rag_memory import (
    RagForgetArgs,
    RagRecallArgs,
    RagRememberArgs,
    RagSearchDocumentsArgs,
    run_rag_forget,
    run_rag_recall,
    run_rag_remember,
    run_rag_search_documents,
)
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.agent.tools.catalog import ListEntitiesArgs, run_list_entities
from administrative_agent.agent.tools.calc import ErpCalcArgs, run_erp_calc
from administrative_agent.agent.tools.salary import (
    ErpSalaryCalcArgs,
    run_erp_salary_calc,
)
from administrative_agent.agent.tools.financial_report import (
    RenderFinancialReportArgs,
    run_render_financial_report,
)
from administrative_agent.agent.tools.tax_worksheet import (
    RenderTaxWorksheetArgs,
    run_render_tax_worksheet,
)
from administrative_agent.agent.tools.export import ExportDataArgs, run_export_data

from administrative_agent.agent.prompts.policies import ALL_POLICY_KEYS

#: 工具注册表（AgentDecision.tool_name 枚举的权威来源；决策轮枚举表由
#: model_node._build_system_prompt 的「可用工具」段从本注册表动态生成）
#: [2026-09-16 工具描述去重] 枚举表不再由 model_client.build_select_schema
#: 生成——就 `tool_name` 约束而言，后者现只出指针句「tool_name 只能取上方『可用工具』表中的名称」；
#: 唯一一份枚举表在 model_node._build_system_prompt 的「可用工具」段。
TOOL_REGISTRY: dict[str, ToolSpec] = {
    "attendance_plan_execute": ToolSpec(
        name="attendance_plan_execute",
        description=(
            # [2026-09-16 引用解耦] 原为「考勤只调用 attendance_plan_execute
            # （规则 6）」，括号里是对基础提示词条号的**硬引用**——条号一重排
            # 就静默指向别的约束。该括号对模型零信息量（本句已自带同一断言），
            # 故删除；一致性改由 tests/unit/test_prompts.py 的
            # test_tool_exclusivity_agrees_between_base_and_tool_descriptions 强制。
            "记录每日考勤（R1）：按员工名单与日期批量写入考勤记录；"
            "考勤只调用 attendance_plan_execute；调用后系统自动"
            "展示确认卡（含写入计划明细），由用户点击卡片确认后执行，无需"
            "在文本中请求用户确认。"
        ),
        args_schema=AttendancePlanExecuteArgs,
        required_scopes=SCOPE_ATTENDANCE_WRITE,
        requires_manager=True,
        is_write=True,
        duty_code="R1",
        policy_key_of=lambda args: "R1.attendance.daily",
        run=execute_attendance,
        prepare=prepare_attendance,
        policy_family=("R1.attendance.daily", "R1.salary.calculation"),
        # [2026-08-30 撤回考勤免确认] 不写 require_confirmation，走 base.py
        # ToolSpec 默认 True（fail closed）——考勤恢复确认卡，与 R2/R3/R6 同路径
    ),
    "order_create": ToolSpec(
        name="order_create",
        description=(
            # [2026-09-16 引用解耦] 同 attendance：删「（规则 5）」对基础条号的
            # 硬引用，断言保留在描述正文里，一致性由 test_prompts 的
            # test_tool_exclusivity_agrees_between_base_and_tool_descriptions 强制。
            "创建订单（R2）：订单创建只走 order_create（ERP："
            "create_order_bundle）；"
            "钣金（metal）需要客户/价格类型/重量单位，塑粉（powder）需要"
            "供货商/色号；metal 与 powder 字段互斥，冲突字段必须剔除后重试。"
            "客户 id、单价/价格类型/重量单位等参数应先用 erp_query 查询补全（客户按"
            "名称查 customers 确认；钣金（metal）单价/价格类型/重量单位按该客户"
            " ERP 主数据（customers 的 unit_price/price_type/price_unit）固定口径"
            "确定，不查历史订单推断；塑粉（powder）价格按该供货商 + 该色号的"
            "历史价格口径确定（用户明确提供价格时以用户为准，无历史价询问确认）；"
            "钣金客户主数据未定价"
            "（单价 0）须询问确认；钣金重量喷涂前未知可暂不填）；补全内容写入系统"
            "确认卡由用户核对。"
            "**同轮要创建多条订单时，把所有明细放进 items 列表一次调用本工具"
            "（每条 = 一条订单，一张卡最多 20 条；同一张卡不得混装钣金与塑粉："
            "order_type 恒在顶层、一张卡只允许 metal 或 powder 之一），禁止把"
            "同轮多条拆成多次调用**；单条可用扁平字段简写。系统只出一张确认卡，"
            "点一次确认即批量写入；单条明细失败不中断其余条目，可再发起同一工具"
            "补齐未完成项。"
            "调用后系统自动展示确认卡，由用户点击卡片确认后执行，无需在文本中"
            "请求用户确认。"
            "口语“XX 来了/到了/送来 YY kg 色号 Z 塑粉/粉”= 供货商送货到货 → "
            "塑粉订单（powder）：供货商=XX、色号=Z、重量=YY kg、日期当天；"
            "9010 等纯数字为塑粉色号（非钣金型号），不得误建/误查金属订单。"
        ),
        args_schema=OrderCreateArgs,
        required_scopes=SCOPE_ORDER_CREATE,
        requires_manager=True,
        is_write=True,
        duty_code="R2",
        policy_key_of=lambda args: f"R2.order.create.{args.get('order_type')}",
        run=execute_order_create,
        prepare=prepare_order_create,
        policy_family=("R2.order.create.metal", "R2.order.create.powder"),
    ),
    "order_update": ToolSpec(
        name="order_update",
        description=(
            # [2026-09-16 引用解耦] 同 order_create：删「（规则 5）」。
            "修改订单（R3）：订单修改只走 order_update（ERP："
            "update_order）；"
            "需先给出目标订单 ID 与期望变化字段；status 只允许 未完成/完成。"
            "目标订单/客户等可查信息先用 erp_query 确认，不向用户追问能查到的问题。"
            "**同轮要修改多条订单时，把所有明细放进 items 列表一次调用本工具"
            "（每条 = 目标 order_id + 期望变化字段，一张卡最多 20 条，同一张卡"
            "内 order_id 不得重复），禁止把同轮多条拆成多次调用**；单条可用扁平"
            "字段简写。系统只出一张确认卡，点一次确认即批量更新；单条明细失败"
            "不中断其余条目，可再发起同一工具补齐未完成项。"
            "调用后系统自动展示确认卡，由用户点击卡片确认后执行，无需在文本中"
            "请求用户确认。"
        ),
        args_schema=OrderUpdateArgs,
        required_scopes=SCOPE_ORDER_UPDATE,
        requires_manager=True,
        is_write=True,
        duty_code="R3",
        policy_key_of=lambda args: f"R3.order.update.{args.get('order_type')}",
        run=execute_order_update,
        prepare=prepare_order_update,
        policy_family=("R3.order.update.metal", "R3.order.update.powder"),
    ),
    "finance_create": ToolSpec(
        name="finance_create",
        # [2026-09-16 描述瘦身] 原文 529 字符，逐句复述了本工具**同轮注入**的
        # 规则包 R7.finance.create（分类目录/金额两位小数/外键互斥/确认卡机制/
        # 部分失败补写，规则包第 1、3、5、8、10、11 条均有且更全）。工具描述在
        # 决策轮**只注入一次**（「可用工具」段；末条 human 消息的 schema 说明已
        # 改为指向该表的指针句，见 model_client.build_select_schema），故每字省
        # ×1；而规则包只在**参数轮**注入，两处并不等价。
        # 保留的只有决策轮独有的信息：工具用途、方向不反问、批量一次调用、
        # 扁平字段简写（决策轮看不到参数 schema）。参数口径一律以规则包为准。
        description=(
            "创建收支记录（R7）：记录一条或多条收入/支出/投资款记录。"
            "方向（tx_type）由用户口语判定——开销语义（花了/付了/运费/房租/"
            "工资等）→ 支出；收款语义（收到/回款/进账等）→ 收入；入股/投资 → "
            "投资款。口语已表达语义时不得反问方向，拿不准按最常见开销语义就近"
            "判定，确认卡会呈现方向·分类供用户核对纠正，不得因怕判错而不调用"
            "本工具。"
            "**同轮要记录多条收支时，把所有明细放进 items 列表一次调用本工具"
            "（一张卡最多 20 条），禁止拆成多次调用**；单条可用扁平字段简写。"
        ),
        args_schema=FinanceCreateArgs,
        required_scopes=SCOPE_FINANCE_CREATE,
        requires_manager=True,
        is_write=True,
        duty_code="R7",
        policy_key_of=lambda args: "R7.finance.create",
        run=execute_finance_create,
        prepare=prepare_finance_create,
        policy_family=("R7.finance.create",),
    ),
    "finance_update": ToolSpec(
        name="finance_update",
        # [2026-09-16 描述瘦身] 同 finance_create：删去对同轮注入的规则包
        # R7.finance.update（写前查询/只改指定字段/分类一致性/确认卡机制/
        # 批量与补写，第 1、2、3、7、8、9 条）的逐句复述。**方向判定词表保留**：
        # 该包的正文**没有**方向词表（只在 create 包第 2 条），而参数轮只注入
        # 本轮工具自己的族——若这里也删，update 参数轮就彻底看不到方向口径。
        # 方向条若要归位到规则包，须同时改 R7.finance.update 正文（另议）。
        description=(
            "修改收支记录（R7）：修改已存在的收支记录，须给出目标记录 ID 与"
            "期望变化的字段，只改用户明确要求变化的字段。"
            "方向（tx_type）由用户口语判定——开销语义（花了/付了/运费/房租/"
            "工资等）→ 支出；收款语义（收到/回款/进账等）→ 收入；入股/投资 → "
            "投资款。口语已表达语义或记录已有方向时不得反问方向，拿不准按最"
            "常见开销语义就近判定，确认卡会呈现方向·分类供用户核对纠正。"
            "**同轮要修改多条收支时，把所有明细放进 items 列表一次调用本工具"
            "（每项 = id + 期望变化字段；一张卡最多 20 条，同一张卡内 id 不得"
            "重复），禁止拆成多次调用**；单条可用扁平字段简写。"
        ),
        args_schema=FinanceUpdateArgs,
        required_scopes=SCOPE_FINANCE_UPDATE,
        requires_manager=True,
        is_write=True,
        duty_code="R7",
        policy_key_of=lambda args: "R7.finance.update",
        run=execute_finance_update,
        prepare=prepare_finance_update,
        policy_family=("R7.finance.update",),
    ),
    "reconciliation_execute": ToolSpec(
        name="reconciliation_execute",
        description=(
            "执行对账写入（R6 复核后写入）：参数为 reconcile_analyze 返回的"
            "report_id；按报告重建写入计划并展示确认卡，确认后逐条执行"
            "create/update 差异（对话确认 = D20 有效复核，7.5）；报告已变化"
            "（plan hash 不一致）则拒绝执行（fail closed）。"
        ),
        args_schema=ReconcileConfirmArgs,
        required_scopes=SCOPE_RECONCILE_EXECUTE,
        requires_manager=True,
        is_write=True,
        duty_code="R6",
        policy_key_of=lambda args: "R6.reconciliation.execute",
        run=execute_reconciliation,
        prepare=prepare_reconcile_confirm,
        policy_family=("R6.reconciliation.execute",),
    ),
    "reconcile_analyze": ToolSpec(
        name="reconcile_analyze",
        description=(
            "对账只读分析（R6 步骤 1-2）：对上传的 Excel 附件（file_id）按"
            "已发布的映射模板解析并对账（entity 为 metal_orders/"
            "powder_orders，subject_name 为对账主体名称：钣金=客户名、"
            "塑粉=供货商名，按名称唯一匹配）；生成差异报告并落库"
            "（status=analyzed），返回报告摘要与 report_id；确认后调用 "
            "reconciliation_execute（参数 report_id）执行差异写入。"
            "硬性前置：template_key 必须取自已发布的映射模板——先调用 "
            "mapping_list 查询（参数可留空查全部）确认存在 status=published "
            "的模板并用其 template_key；无 published 模板时先用 "
            "mapping_template（action=create_draft 基于该附件 + publish）"
            "创建并发布后再分析；切勿猜测模板键（无 published 映射会 "
            "fail closed 报错）。"
        ),
        args_schema=ReconcileAnalyzeArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="R6",
        policy_key_of=lambda args: "R6.reconciliation.plan",
        run=reconcile_analyze,
        prepare=None,
        policy_family=(),  # 只读工具默认无规则包（export_data 绑定 R4.export.fields、erp_query 绑定 R4.query；memory_lookup/mapping_list 无规则包）
    ),
    "attachment_preview": ToolSpec(
        name="attachment_preview",
        description=(
            "预览附件内容（只读）：只凭 file_id（上传返回的 attachment_id；"
            "用户消息 [附件：…（…，id=…）] 标记里的 id）即可查看该 Excel "
            "附件的工作表清单、表头行、前若干行数据与每列样例值；用于在正式"
            "对账前先看清「这是什么单、哪个客户/供货商、哪个月」。"
            "**看到附件后应优先用本工具查看内容，再决定 reconcile_analyze 的 "
            "entity / subject_name，不要一上来就向用户追问是哪个客户/供货商**"
            "（推不出时才向用户索取）。可选参数 sheet_name 指定工作表，不传"
            "预览活动工作表。注意：预览只给结构/样例行，**不能替代 "
            "reconcile_analyze**——确认附件内容后仍须按流程用 mapping_list "
            "查已发布映射并调用 reconcile_analyze 生成差异报告。"
        ),
        args_schema=AttachmentPreviewArgs,
        required_scopes=SCOPE_QUERY,  # 与 reconcile_analyze 一致（只读 ERP 场景）
        requires_manager=True,
        is_write=False,  # 只读工具：无确认卡、不建任务、不落审计
        duty_code="R6",  # 服务于对账流程（对账前置读附件）
        policy_key_of=lambda args: "R6.reconciliation.plan",
        run=attachment_preview,
        prepare=None,
        # 只读工具默认无规则包（mapping_list/reconcile_analyze 同款口径）
        policy_family=(),
    ),
    "list_entities": ToolSpec(
        name="list_entities",
        description=(
            "查看 ERP 实体目录（只读）：列出当前账号可见的实体及字段元数据"
            "（字段名/中文含义/类型/枚举取值；其中 metal_orders（钣金订单）/"
            "powder_orders（塑粉订单）/customers（客户）/suppliers（供货商）/"
            "employees（员工）/transactions（收支记录）/transaction_categories"
            "（收支分类）/customer_contacts（客户联系人）/attendance（考勤记录）/"
            "ar_payments（应收收款）/ap_payments（应付付款）/salary_payments"
            "（工资发放）/powder_inventory（塑粉库存色号）/powder_consumption"
            "（塑粉消耗记录）/ar_settlements（应收平账）可被 erp_query/erp_calc "
            "查询，其余实体查了会被拒绝）。"
            "需要了解「有哪些实体/字段可查、字段类型/单位/枚举」时调用——写 "
            "erp_query 的 fields/filter 参数或 erp_calc 的 filter/measures "
            "参数前，如不确定字段名或枚举取值（如 weight_unit ∈ kg/m²）可先"
            "调用本工具，不要凭记忆猜（猜错会被 fail closed 拒绝）。可选参数 "
            "entity：只查看单个实体以减小返回体积（不传 = 全部实体紧凑目录，"
            "约 12KB；传 = 该实体完整字段细节）。目录中的枚举取值（如重量单位 "
            "kg/m²）可直接用于 filter 条件。"
        ),
        args_schema=ListEntitiesArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.catalog",
        run=run_list_entities,
        prepare=None,
        policy_family=(),  # 无规则包（目录工具：无查询字段/分页/先查后答语义，
        # R4.query 不适用——见本文件 reconcile_analyze 注册注释同款口径）
    ),
    "erp_calc": ToolSpec(
        name="erp_calc",
        description=(
            "ERP 汇总计算（只读，一体式「查+算」）：凡涉及汇总计算的 ERP 数据"
            "问题（求和/平均/最大/最小/计数/按日分组统计/平均每日之类），一律"
            "调用本工具由代码查库计算并返回结果——不要自己口算或心算明细"
            "（模型算术不可靠，已有实测教训）。参数是声明式：entity（实体名，"
            "来自 list_entities）+ filter（筛选）+ group_by（分组字段，可选）+ "
            "measures（每项算什么：sum/avg/count/min/max，field 可选"
            " multiply_field 表达整单重量=单件重量×数量等行级乘积）。"
            "「平均每日/日均」一类口径一律用 over 声明归一化，除法由代码完成、"
            "不要自己算：over=\"calendar_days\"（分母=filter 日期边界的自然日数，"
            "如 8-01..8-31 → 31；推不出会 fail closed 提示）或 over=正整数兜底"
            "（如 over=31）；over=\"groups\"（分母=出现订单行的分组数，含当日"
            "行全待补充的组；需 group_by）。"
            "「常量除法」用 divide_by：写在 measure 里（如把加班分钟合计换算成"
            "小时 = measure {field: overtime_minutes, divide_by: 60}），先聚合"
            "再整体除以该常量一次、除法由代码完成——不要自己把分钟口算成小时；"
            "含 divide_by 的结果量化 4 位小数（除不尽为近似值）。"
            "有 group_by 且未传 over 时，返回顶层 totals 给出**按单位桶分别**的"
            "合计——报告该合计必须用 totals，不要自己把各组值相加；"
            "例外：金额类字段（total_amount）单位无关，其 measure 跨桶合并给出"
            "**单一**合计、条目不带 unit 键（物理量如 weight_per_unit 仍分桶）；"
            "**仅当存在 sum/count 类 measure 时才有 totals**（全为 avg/min/max "
            "时不产出）；totals 里 **count 类 value 是整数、其余 measure 是"
            "字符串**。传了 over 时权威数字是 normalized.total（per 才是均值"
            "口径），勿与 totals 混用。"
            "结果 items 各桶合计原样保留（总量可核对），normalized 段给每条 "
            "total/divisor/per——报告日均必须以 normalized 的 divisor 为准。"
            "**按月/季汇总用 time_granularity**（month/quarter）：把日期型 "
            "group_by 字段在代码内截断成桶键后再聚合，直接得到各月/各季合计"
            "（桶键形如 \"2026-08\" / \"2026-Q3\"）。例：group_by=\"in_date\" + "
            "time_granularity=\"month\" + measures=[{op:\"sum\", field:"
            "\"total_amount\"}]，**不要**按原始日期逐日取数、再把几百行搬进"
            "上下文自己手工归并（逐日组数多，既易漏行又易算错）。"
            "**按计算列排序取 Top-N 用 order_by + limit**：order_by 取值 = 某条 "
            "measure 的别名（字段名前加 \"-\" 表降序），limit 取排序后前 N 条 "
            "items（1..200）。金属**无**整单重量存储列，erp_query 的 order_by "
            "只收存储列、拿不到这个计算列——「按整单重量取前 10 单」必须写成"
            "一次本工具调用：group_by=\"id\" + measures=[{op:\"sum\", field:"
            "\"weight_per_unit\", multiply_field:\"quantity\", alias:\"整单重量\"}] "
            "+ order_by=\"-整单重量\" + limit=10（排序与取前 N 全由代码完成，"
            "不要退回按单件重量排序、也不要取几百行再本地重排）。limit **只"
            "截断 items**，totals 仍是该筛选下**全量**原始行的权威合计——真的"
            "截断时结果会附一句 note 提醒：不得把子集之和当成总计报告（该情形下"
            "顶层 count 是本次返回的条目数，**全量条目数见 note**）。混单位实体上 "
            "Top-N 是**跨单位桶**排序，报告仍按单位桶分别；**被截断时某单位桶"
            "可能整桶不出现在 items，须以 totals 核对**。"
            "字段与枚举取值不要凭记忆猜：写 filter/measures 前若不确定字段名或"
            "枚举取值（weight_unit ∈ kg/m²），先调 list_entities(entity=…) 查"
            "目录。单位语义警示：weight_per_unit 类字段的数值含义随 weight_unit "
            "桶变（kg 桶×数量=重量、m² 桶×数量=面积）——不同单位不可混加，请把"
            "单位条件写进 filter（如 weight_unit=kg），或按分桶结果分别报告，"
            "报告必须带桶单位。返回里 measures 的 value 为空串（\"\"）表示该桶/"
            "组无有效样本（0/空/待补充行全被跳过，其 n=0）——不是数值零，不要"
            "当作 0 报告。明细/单条核对用 erp_query；汇总数值一律本工具，不要"
            "先查明细再自己加。**取多客户/多供货商的订单金额合计**（如「各客户"
            "钣金订单 total_amount 合计」）就是**一次**本工具调用："
            "group_by=customer_id + measures=[sum(total_amount)]——**不要逐户 "
            "erp_query 出金额再自己相加**，多户相加一律交给本工具；"
            "该次调用**只放金额 measure**，混入重量/数量会让金额也被按单位分桶。"
            "按 customer_id/supplier_id/employee_id 分组时，分组条目**已自带"
            "名称列**——customer_id→customer_name（取 customers.company_name）、"
            "supplier_id→supplier_name（取 suppliers.name）、employee_id→"
            "employee_name（取 employees.name）。**直接用该名称列报告，不要再"
            "为取名去 erp_query**（同名列覆盖所有实体：ar_payments.customer_id、"
            "ap_payments.supplier_id、salary_payments.employee_id 等一律适用）。"
            "**仅当条目里没有名称列时**（分组用的外键未支持解析、名称档案缺失/"
            "已删除、或提示「未解析名称」），才自行 erp_query customers"
            "（company_name）/suppliers（name）/employees（name）兜底取名；"
            "任何情况下都不得把 id 当名称报给用户。"
            # [2026-09-16 当期应收口径开放] 决策轮只渲染 description，口径约束
            # 必须落在 description 才能被模型答题前看到（policy_family 正文
            # 只到参数轮）；详见 R4.balance.scope 规则包
            "「客户还欠多少」= 当期应收，必须经 SK.receivables 流程给出（到期月"
            "订单金额累计 − 累计实际收款 − 累计平账抹零）；未走该流程不得用"
            "订单金额相加或收付款流水相减自行推算。未结余额是另一个口径（全量"
            "订单累计、不扣账期），要与 ERP 财务页对得上时引导用户到 ERP"
            "财务-资产管理页查看；实际收付流水（ar_payments/ap_payments）"
            "照常可查可报。"
        ),
        args_schema=ErpCalcArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.calc",
        run=run_erp_calc,
        prepare=None,
        policy_family=("R4.balance.scope",),  # 只绑余额口径包：不绑 R4.query
        # （R4.query 的分页/先查后答面向 erp_query 明细查询：erp_calc 取数由
        # 工具内部确定性完成，单位语义等已写入 description 与参数说明，绑定
        # R4.query 反而会误导参数轮先查明细——原注释论证仍成立）；而余额口径
        # 与 erp_calc 的 measure 相减/单实体缺口直接相关，参数轮必须看到
    ),
    "erp_salary_calc": ToolSpec(
        name="erp_salary_calc",
        description=(
            "算工资（只读，专用口径）：凡是要算某个时段每位员工的工资/工资总额，"
            "**一律调用本工具一次算出**——入参只有时段起止日（start_date/"
            "end_date，闭区间含首尾）与可选 include_inactive；工资口径"
            "（工资 = 考勤天数 × 日薪 + 时薪 × 加班时长 − 时薪 × 请假时长，"
            "时薪 = 日薪 ÷ 每日工作时长）以及日薪/每日工作时长/考勤的取数"
            "全部由工具内部完成。**不要自己先用 erp_query 查 employees 取日薪、"
            "再查 attendance 取考勤、再用 erp_calc 逐人算**（erp_calc 没有除法、"
            "单实体、measure 不能相加减，逐人算工资根本拼不出来；2026-07 实测"
            "那样算 12 行错 11 行）。**禁用捷径：**不得取 transactions（收支"
            "记录）中「工资」类目的金额充当工资，也不得用 salary_payments"
            "（工资发放记录）代替——二者都是已发生的付款/发放流水，不是计算"
            "依据。返回 items 为逐人员工明细（含考勤天数/日薪/加班时长/请假"
            "时长/工资）；每位员工的工资有两个字段，分工不同：**wage 为 2 位"
            "小数**（与 ERP 系统记录逐分一致的**权威值**，用于核对与合计）；"
            "**wage_display 为 1 位小数**（**仅供向用户逐人展示**——给用户报"
            "每个人的工资时用它）。**合计数一律以 totals.wage 为准**（totals "
            "是各行 wage 2 位权威值的逐行求和）；**wage_display 逐行相加不等"
            "于 totals.wage（每人最多差 0.05 元），报告合计不得用展示列相加**。"
            "missing 里的是缺参（无考勤/缺日薪/缺每日"
            "工作时长等）未计入的人，需向用户确认后处理，不得用猜测值补齐。"
            "单看某人工资时同样直接调用本工具（时段填该人所在区间），不要读"
            "明细心算。summary.coverage 常驻给出本时段考勤覆盖度"
            "（first_att_date/last_att_date/distinct_days/period_days）——"
            "供你判断工资是否完整；**报告工资时若 note 提示「考勤记录仅覆盖…"
            "工资可能不完整」（或本时段无任何考勤记录），必须把覆盖不全如实"
            "告知用户（缺录日期需核实），不得当作完整工资汇报**。注意"
            "distinct_days < period_days 不必然异常（只录工作日、不录周末是"
            "正常数据），以 note 是否提示为准。"
        ),
        args_schema=ErpSalaryCalcArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.salary_calc",
        run=run_erp_salary_calc,
        prepare=None,
        policy_family=(),  # 不绑 R1 规则包：工资口径已整条下沉进本工具代码
        # （公式在实现里），绑规则包会让参数轮误以为还要按规则正文自己搭
        # 公式流程——理由与 erp_calc 注册处注释同款
    ),
    "render_financial_report": ToolSpec(
        name="render_financial_report",
        description=(
            "渲染财务报表（只读，固定格式）：**只用于财报最后一步**——钣金/"
            "塑粉/动态账款/工资/房租五项都算完、拿到各自合计数之后调用它排版。"
            "入参是五项各自的明细与合计（metal 按客户/powder 按供货商/dynamic "
            "按类目/salary 按员工/rent 传单价与天数）。**各项合计必须传既有工具"
            "给的 totals**（钣金/塑粉/动态用 erp_calc 的 totals，工资用 "
            "erp_salary_calc 的 totals.wage），**不要自己重算**——本工具会把每项"
            "明细逐行相加与传入的 total 核对，不一致会报错退回。本工具只做三笔"
            "算数：① 房租 = 单价 × 天数、② 净利润 = 五项合计的加减、"
            "③ 塑粉占比 = 塑粉合计 ÷ 钣金合计。"
            "**拿到返回的 text 后必须原样输出给用户**——不得改写、不得增删表格"
            "行、不得自己重新排版、不得只输出一部分；text 里五张表一张不少，"
            "净利润单独一行算式（不放进任何表格），末尾另出「塑粉金额占钣金订单"
            "金额比」算式段（百分比两位小数，除数为零时写「无法计算」）。"
        ),
        args_schema=RenderFinancialReportArgs,
        required_scopes=SCOPE_QUERY,  # 与 erp_calc/erp_salary_calc 同款只读查询面
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.financial_report",
        run=run_render_financial_report,
        prepare=None,
        policy_family=(),  # 纯排版工具，无规则包：不碰 ERP、不重算口径，
        # 公式（房租乘积/净利润加减）在代码里；理由同 erp_calc/erp_salary_calc
    ),
    "render_tax_worksheet": ToolSpec(
        name="render_tax_worksheet",
        description=(
            "渲染当期增值税估算底稿（只读，固定格式）：**只用于本流程最后一步**"
            "——含税销项/未计税收入/单价类型未知/进项 13% 档/进项 9% 档五块都"
            "算完、拿到各自合计数之后调用它排版。入参是五块各自的明细与合计"
            "（销项桶与未计税桶与单价类型未知桶按客户、13% 档与 9% 档按类目）。"
            "**各块合计必须传 erp_calc 给的 totals，不要自己重算**——本工具会把"
            "每块明细逐行相加与传入的 total 核对，不一致会报错退回。税率、销项"
            "税额、进项税额、应纳增值税、附加税、合计应缴一律由本工具按固定口径"
            "算（放最后一步：各客户/各类目先各自取整到分，再在「分」域相加），"
            "**不要自己算任何税额、不要自己相加**。**拿到返回的 text 后必须原样"
            "输出给用户**——不得改写、不得增删表格行、不得自己重新排版、不得只"
            "输出一部分；text 里销项/未计税收入/单价类型未知/进项四张表一张不少"
            "（单价类型未知表仅展示、不计税，其客户行只列金额），另出销项税额/"
            "应纳增值税/附加税/合计应缴四行算式（应纳增值税为留抵时附加税记 0）。"
        ),
        args_schema=RenderTaxWorksheetArgs,
        required_scopes=SCOPE_QUERY,  # 与 erp_calc 同款只读查询面
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.tax_worksheet",
        run=run_render_tax_worksheet,
        prepare=None,
        policy_family=(),  # 纯排版工具，无规则包：不碰 ERP、不重算口径，
        # 税率与算式都在代码里；理由同 render_financial_report
    ),
    "erp_query": ToolSpec(
        name="erp_query",
        description=(
            "查询 ERP 数据（只读）：按实体名查询记录，支持字段筛选/排序/分页；"
            "实体名固定为 metal_orders（钣金订单）/powder_orders（塑粉订单）/"
            "customers（客户）/suppliers（供货商）/employees（员工）/"
            "transactions（收支记录，R7 先查后写的前提）/transaction_categories"
            "（收支分类，按 tx_type 过滤取可用分类清单）/customer_contacts"
            "（客户联系人）/attendance（考勤记录）/ar_payments（应收收款）/"
            "ap_payments（应付付款）/salary_payments（工资发放）/powder_inventory"
            "（塑粉库存色号）/powder_consumption（塑粉消耗记录）/ar_settlements"
            "（应收平账），其他实体不可查询。"
            "注意 powder_inventory **直接暴露** order_total（订单总重量）/"
            "consumed_total（消耗总量）/stock（净库存，实时计算）三个可查询"
            "字段——**净库存直接查 stock**，禁止再按「该色号 Σ 未软删 "
            "powder_orders.total_weight − 该色号 Σ 未软删 powder_consumption"
            ".quantity」自己另算（旧口径已作废，勿套公式）；也勿把色号主数据"
            "（color_code/warn_threshold）当库存数量报告。"
            "涉及 ERP 明细/单条核对类问题（订单/客户/供货商/员工/收支记录信息、"
            "时间范围、状态、数量等可查参数）必须先经本工具查询再回答，不反问"
            "用户能查到的问题；汇总/统计类问题用 erp_calc。"
            "结果 truncated: true 表示只返回了前 500 行——要补足剩余行，用 "
            "fetch_all=false + offset=<已取得的行数> 续查（必须关掉 fetch_all，"
            "否则工具会从该偏移继续自动续查并在 500 行上限处再次截断）；要全量"
            "明细用 export_data 导出（不截断；超上限会被拒绝，需先缩小 filter）；"
            "只需汇总/计数用 erp_calc（不受 "
            "500 行上限限制）。"
            "**查询 metal_orders/powder_orders 明细并要展示给用户时**：记录里"
            "只有 customer_id/supplier_id，客户/供货商列**必须**显示名称——"
            "先查一次 customers（company_name）/suppliers（name）取映射；内部"
            "主键 id 不得作为表格列或正文数据输出（除确已查过该档案且查不到"
            "时以「客户ID 16」占位并说明）。"
            # [2026-09-16 当期应收口径开放] 决策轮只渲染 description（policy_family
            # 正文只到参数轮），口径约束必须落在 description 才能被模型答题前
            # 看到；详见 R4.balance.scope 规则包
            "「客户还欠多少」= 当期应收，必须经 SK.receivables 流程给出（到期月"
            "订单金额累计 − 累计实际收款 − 累计平账抹零）；未走该流程不得用"
            "订单金额相加或收付款流水相减自行推算。未结余额是另一个口径（全量"
            "订单累计、不扣账期），要与 ERP 财务页对得上时引导用户到 ERP"
            "财务-资产管理页查看；实际收付流水（ar_payments/ap_payments）"
            "照常可查可报。"
        ),
        args_schema=ErpQueryArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "R4.query",
        run=run_erp_query,
        prepare=None,
        policy_family=("R4.query", "R4.balance.scope"),
    ),
    "export_data": ToolSpec(
        name="export_data",
        description=(
            "导出 ERP 实体为 Excel 文件（只读）：按实体名"
            "导出记录到 Excel，字段清单/筛选/排序直接透传（fields 缺省 = 实体默认"
            "导出列——metal_orders/powder_orders 按 R4.export.fields：客户名称/"
            "供货商名称与总重量/总价格由导出器自动关联/计算，filter 缺省 = 全量）；"
            "导出完成后返回下载标识（file_id/filename），文件 1 小时内可下载；"
            "行数超出上限会拒绝并提示增加过滤条件（不截断导出）。"
        ),
        args_schema=ExportDataArgs,
        required_scopes=SCOPE_QUERY,
        requires_manager=True,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "R4.export.fields",
        run=run_export_data,
        prepare=None,
        policy_family=("R4.export.fields",),
    ),
    "memory_lookup": ToolSpec(
        name="memory_lookup",
        description=(
            "查询存档细节（只读）：①按段号查会话压缩存档（segment_no，可选 "
            "query 关键词过滤；段号与主题见系统提示的历史摘要区目录）；②按 "
            "query_id 查工具查询原始数据存档（query_id，形如 tool-call-xxxx——"
            "工具结果里的 query_id 字段（archived_note 提示的存档标识）；返回"
            "该次查询的原始明细行，大结果用 "
            "limit + offset 分批翻页拉取如 limit=30, offset=0/30/60…，逐批分析"
            "直至取齐 total 条（响应含 total=存档总行数与 count=本次已返回条数；"
            "total 不随分页窗口变化，以它判定是否取齐）。"
            "硬性前置：必须先翻页取齐 total 条明细（offset 递增），取齐后方可"
            "进行分析/给出结论；只取部分即下结论视为未完成任务；只需核对个别"
            "条目时可按 limit 取窗口，并在回复中说明覆盖面。"
        ),
        args_schema=MemoryLookupArgs,
        required_scopes=frozenset(),  # 会话记忆查询，无需 ERP scope
        requires_manager=True,
        is_write=False,
        duty_code="MEM",
        policy_key_of=lambda args: "MEM.lookup",
        run=run_memory_lookup,
        prepare=None,
        policy_family=(),  # 无规则包（查询工具；不注入规则正文）
    ),
    # [2026-09-22 RAG 长期记忆接入] 三个长期记忆工具（对接 hengfa-rag 的
    # MCP 服务）。只读工具路径（is_write=False）：不建任务、无确认卡、不落
    # ERP 写入审计（attempt/result）——它们操作的是 RAG 长期记忆库，不是 ERP
    # 写入语义（故绝不加进 mcp/client.py 的 _WRITE_TOOLS）。[F1] remember/
    # forget 成功后会补一条 agent 侧审计事件（EVENT_RAG_MEMORY，见
    # tools/rag_memory.py 的 _record_memory_audit）——二者改变存储状态，与
    # 纯读的 memory_lookup 不同类；不上确认卡是用户拍板。账号（subject）由
    # 工具包装层经 ctx.context.user_id 注入，不经 State/提示词（见
    # tools/rag_memory.py）。
    # policy_key_of 返回未注册键（不在 ALL_POLICY_KEYS 内），渲染时被过滤。
    # 装配层在 rag_mcp_url 或 rag_mcp_api_key 未配置（含半配置）时不把这三个
    # 工具注入运行时（模型不可见）。
    "rag_remember": ToolSpec(
        name="rag_remember",
        description=(
            "记住一条长期记忆（跨会话长期保留、供以后 rag_recall 召回）："
            "content 为记忆原文（1..1000 字，超长报错、不截断）；kind 只能是 "
            "fact（事实）/ preference（偏好）/ episode（经历）。**归属账号由系统"
            "按当前登录账号自动注入**：偏好（preference）归属当前账号，事实与"
            "经历是全局的（无需、也不要指定归属）。返回 {memory_id, action, "
            "similar}——action 当前恒为 created（服务端不自动去重/合并）；similar "
            "是库中已有的相似记忆（只含全局与本账号，返回 {id, score, content}），"
            "是否取代旧记忆由你判断，要忘掉旧记忆用 similar 里的 id 调 rag_forget。"
        ),
        args_schema=RagRememberArgs,
        required_scopes=frozenset(),  # RAG 长期记忆（非 ERP），无 ERP scope
        requires_manager=True,
        is_write=False,  # 只读工具路径：无确认卡、不建任务；成功后落 F1 审计事件
        duty_code="MEM",
        policy_key_of=lambda args: "MEM.remember",
        run=run_rag_remember,
        prepare=None,
        policy_family=(),  # 无规则包（记忆工具，不注入业务规则正文）
    ),
    "rag_recall": ToolSpec(
        name="rag_recall",
        description=(
            "按语义召回长期记忆（只读）：query 为查询文本；可选 top_k（返回条数，"
            "1..20，缺省 5）、kind（限定 fact/preference/episode）、since（只召回"
            "该时刻之后创建的，ISO 8601，如 2026-09-01；不带时区按北京时间）。"
            "**归属由系统按当前登录账号自动注入**——只召回「本账号的偏好 + 全局"
            "记忆」，不会带出其它账号的记忆，模型不要指定归属。返回 {hits, "
            "hit_count}，hits 每条含 memory_id/content/kind/subject/created_at/"
            "score 等，按相似度降序；**无命中返回 {hits: [], hit_count: 0}，不是"
            "错误**。"
        ),
        args_schema=RagRecallArgs,
        required_scopes=frozenset(),
        requires_manager=True,
        is_write=False,
        duty_code="MEM",
        policy_key_of=lambda args: "MEM.recall",
        run=run_rag_recall,
        prepare=None,
        policy_family=(),
    ),
    "rag_forget": ToolSpec(
        name="rag_forget",
        description=(
            "遗忘长期记忆（**不弹确认卡**、调用即生效；服务端为**软删**，保留 14 天挽回窗口）："
            "memory_id 与 query **必须二选一**——memory_id 忘掉指定的一条（按 id "
            "精确定位，prior rag_recall 返回的 memory_id）；query 按内容子串批量"
            "遗忘（大小写不敏感）。**按 query 批量遗忘时范围自动收在当前登录账号"
            "内**（不会误删其它账号的记忆），模型不要指定归属。返回 {affected, "
            "memory_ids}；affected 可能为 0（id 不存在/内容不匹配/已遗忘）——**这"
            "不是错误**，重复遗忘是幂等的。"
        ),
        args_schema=RagForgetArgs,
        required_scopes=frozenset(),
        requires_manager=True,
        is_write=False,
        duty_code="MEM",
        policy_key_of=lambda args: "MEM.forget",
        run=run_rag_forget,
        prepare=None,
        policy_family=(),
    ),
    # [2026-10 文档检索接入] RAG 文档线工具（对接 hengfa-rag 的
    # search_documents）。与记忆三工具同属 RAG 侧只读路径（is_write=False，
    # 无确认卡、不建任务、不进 _WRITE_TOOLS）；成功后补一条 agent 侧审计
    # （EVENT_RAG_SEARCH，见 tools/rag_memory.py 的 _record_search_audit）。
    # 与记忆三工具**不同**：文档线是公司共享知识库，**无账号归属**，故本工具
    # 不注入 subject（RAG 侧也不收）。装配层在 RAG 未配置时一并剔除（见
    # RAG_TOOL_NAMES）。policy_key_of 返回未注册键（不在 ALL_POLICY_KEYS 内），
    # 渲染时被过滤。
    "rag_search_documents": ToolSpec(
        name="rag_search_documents",
        description=(
            "检索公司共享知识库（只读）：在**已入库的文档**里按查询文本检索"
            "最相关的片段（按相似度降序），用于回答需要查公司资料/制度/文件"
            "内容的问题。query 为查询文本（1..500 字，空/超长报错、不截断）；"
            "可选 top_k（返回条数，1..20，缺省 5）。返回 {hits, hit_count}，"
            "每条命中含 chunk_id/document_id/title/source_uri/heading_text/"
            "line_start/line_end/page_start/page_end/char_count/score/content；"
            "**没有命中返回 {hits: [], hit_count: 0}，不是错误**——知识库里"
            "没有相关内容是合法答案。"
            "**出处字段怎么报（务必按此，勿乱引用）**：line_start/line_end 只"
            "对文本类文档是真行号；**对 PDF 是提取器内部的假行号，翻不回原页**。"
            "**PDF 命中看 page_start/page_end（物理页序，1 起），报告出处必须报"
            "「第 N 页」而不是行号**；判据是 page_start 非空即为 PDF。非 PDF"
            "（page_start 为 null）才用 line_start/line_end 报行号。"
            "本工具检索的是**文档片段**，与 rag_recall（召回长期记忆）是两条线，"
            "不要用本工具找记忆、也不要向本工具指定账号归属（文档是公司共享的）。"
        ),
        args_schema=RagSearchDocumentsArgs,
        required_scopes=frozenset(),  # RAG 文档库（非 ERP），无 ERP scope
        requires_manager=True,
        is_write=False,  # 只读工具路径：无确认卡、不建任务；成功后落审计事件
        duty_code="MEM",
        policy_key_of=lambda args: "MEM.search_documents",
        run=run_rag_search_documents,
        prepare=None,
        policy_family=(),  # 无规则包（检索工具，不注入业务规则正文）
    ),
    "mapping_template": ToolSpec(
        name="mapping_template",
        description=(
            "管理映射模板（R6 前置，6.3）：action=create_draft（上传映射来源"
            " Excel 附件 file_id + 列映射定义 columns，导入新草稿版本）/publish"
            "（version_id，发布 draft→published；同模板已有 published 则被拒，"
            "须先退役）/retire（version_id，退役 published/draft→终态）。"
            "写本地映射配置表，不触 ERP；调用后系统自动展示确认卡，由用户点击"
            "卡片确认后执行，无需在文本中请求用户确认。"
        ),
        args_schema=MappingTemplateArgs,
        required_scopes=frozenset(),  # 本地配置表操作，无 ERP scope（模块 docstring 决策）
        requires_manager=True,
        is_write=True,
        duty_code="R6",
        policy_key_of=lambda args: "R6.mapping",
        run=run_mapping_template,
        prepare=prepare_mapping_template,
        policy_family=("R6.mapping",),
    ),
    "mapping_list": ToolSpec(
        name="mapping_list",
        description=(
            "查询映射模板版本列表（只读）：按 template_key/status 筛选 + "
            "分页；返回各版本状态/列数/发布信息，用于对账前确认 published "
            "映射是否存在及当前发布版本号（6.3 规则 1：只使用 published）。"
        ),
        args_schema=MappingListArgs,
        required_scopes=frozenset(),  # 本地配置表读取，无 ERP scope
        requires_manager=True,
        is_write=False,
        duty_code="R6",
        policy_key_of=lambda args: "R6.mapping",
        run=run_mapping_list,
        prepare=None,
        policy_family=(),  # 无规则包（查询工具；不注入规则正文）
    ),
}

# ---------------------------------------------------------------------------
# 一致性断言（import 即执行，fail closed）
# ---------------------------------------------------------------------------

#: 每个工具的 policy_family 键必须 ∈ ALL_POLICY_KEYS（防族配错键）
for _spec in TOOL_REGISTRY.values():
    for _key in _spec.policy_family:
        if _key not in ALL_POLICY_KEYS:
            raise AssertionError(
                f"工具 {_spec.name} 的 policy_family 含未注册键 {_key!r}"
                "（不在 ALL_POLICY_KEYS 内）"
            )

#: order_create/order_update 的族必须同时含 metal 与 powder 两个键（防族漏配）
for _name, _prefix in (("order_create", "R2.order.create"), ("order_update", "R3.order.update")):
    _family = set(TOOL_REGISTRY[_name].policy_family)
    _want = {f"{_prefix}.metal", f"{_prefix}.powder"}
    if not _want <= _family:
        raise AssertionError(
            f"工具 {_name} 的 policy_family 必须同时含 {_prefix}.metal 与"
            f" {_prefix}.powder（当前 {sorted(_family)}）"
        )

#: 写工具族非空；只读工具默认无规则包（export_data 绑定 R4.export.fields、
#: erp_query 绑定 R4.query；memory_lookup/mapping_list 无规则包。
#: policy_family 服务于工具参数轮注入规则正文，is_write=False 豁免）
for _name, _spec in TOOL_REGISTRY.items():
    if _spec.is_write and not _spec.policy_family:
        raise AssertionError(f"工具 {_name} 的 policy_family 为空（规则包族必配）")

#: 免确认只允许写工具（[2026-08-28 考勤免确认] 引入、2026-08-30 撤回后
#: 保留复用）：require_confirmation=False 直接执行跳过确认卡，防误配到
#: 只读工具——只读工具本就无确认卡，误配会绕过只读路径语义。
#: **当前注册表中没有任何工具配 require_confirmation=False**（考勤已于
#: 2026-08-30 恢复确认卡），本循环为空转；保留它是为未来再次启用免确认
#: 时继续守护该不变式。
for _name, _spec in TOOL_REGISTRY.items():
    if not _spec.require_confirmation and not _spec.is_write:
        raise AssertionError(
            f"工具 {_name} 配置了 require_confirmation=False 但不是写工具"
            "（免确认只允许写工具，防止误配到只读工具）"
        )

# ---------------------------------------------------------------------------
# [2026-09-16 P2-1 形态 A] 规则包 → 触发工具 的**反向索引**（代码求逆 · 两关系并集）
# ---------------------------------------------------------------------------
# 决策轮规则包索引段（``model_node._build_system_prompt``）要标出每个包由哪个
# 工具触发——该信息一律由代码求逆生成，**工具名不手写进任何 md / 字符串常量**
# （杜绝「关系改了、文案没改」的双真源）。本表是**两个代码关系的并集**，二者
# 回答**不同问题**，缺一即产生与事实矛盾的渲染：
#   (i)  ``spec.policy_family`` 的逆 —— 该包的**正文在参数轮被哪个工具注入**；
#   (ii) ``spec.policy_key_of`` 的**静态输出键** —— 工具**执行后登记哪个键**
#        （``tools_node`` 把该结果写进 ``state["policy_keys"]``）。
# 索引段渲染的键集恰是 ``state["policy_keys"]``，即 (ii) 的产物；只看 (i) 会把
# 「执行后登记、但族里不含该键」的包渲染成「无任何工具引用」，与模型刚执行完
# 的动作直接矛盾（实测：``reconcile_analyze`` / ``attachment_preview`` 的
# ``policy_family`` 为空，但 ``policy_key_of`` 恒返回 ``R6.reconciliation.plan``）。
# 并集后每个**可渲染**的键都至少有一个真实工具名。
# 键集必然是 ALL_POLICY_KEYS 的**子集**：没有任何工具引用的包不进本表（如
# ``R5.analysis``）。工具名按 TOOL_REGISTRY 注册顺序去重。
_POLICY_TOOLS_ACC: dict[str, list[str]] = {}


def _register_policy_tool(_key: str, _name: str) -> None:
    """把工具名追加到某键的名下（按注册顺序去重）。"""
    _names = _POLICY_TOOLS_ACC.setdefault(_key, [])
    if _name not in _names:
        _names.append(_name)


def _static_policy_keys(_spec: ToolSpec) -> tuple[str, ...]:
    """``policy_key_of`` 的**静态输出键**（不真的调用它——关系 (ii)）。

    遍历函数字节码常量，取其中 ∈ ALL_POLICY_KEYS 的字符串。静态提取比实际
    调用更安全：后者可能依赖 args 形态、存在异常路径（本函数只提取常量，
    对 ``f"{prefix}.{args.get('order_type')}"`` 这类只取到前缀 ``R2.order.create.``，
    它不在 ALL_POLICY_KEYS 内 → 自然被滤掉，不误登记）。取不到 code 对象
    （非 Python 函数 / 被包装）时返回空元组（fail safe，不抛）。
    """
    code = getattr(_spec.policy_key_of, "__code__", None)
    if code is None:
        return ()
    try:
        consts = code.co_consts
    except AttributeError:  # pragma: no cover - 理论兜底（code 对象必含 co_consts）
        return ()
    return tuple(
        c for c in consts if isinstance(c, str) and c in ALL_POLICY_KEYS
    )


for _spec in TOOL_REGISTRY.values():
    for _key in _spec.policy_family:
        _register_policy_tool(_key, _spec.name)
    for _key in _static_policy_keys(_spec):
        _register_policy_tool(_key, _spec.name)

POLICY_TOOLS: dict[str, tuple[str, ...]] = {
    _key: tuple(_names) for _key, _names in _POLICY_TOOLS_ACC.items()
}

__all__ = [
    "EventSink",
    "SCOPE_ATTENDANCE_WRITE",
    "SCOPE_FINANCE_CREATE",
    "SCOPE_FINANCE_UPDATE",
    "SCOPE_ORDER_CREATE",
    "SCOPE_ORDER_UPDATE",
    "SCOPE_QUERY",
    "SCOPE_RECONCILE_EXECUTE",
    "ToolSpec",
    "ToolContext",
    "ToolResultSummary",
    "PreparedToolWrite",
    "ToolPreparationError",
    "AttendanceExceptionArgs",
    "AttendancePlanExecuteArgs",
    "OrderCreateArgs",
    "OrderUpdateArgs",
    "FinanceCreateArgs",
    "FinanceUpdateArgs",
    "ReconciliationExecuteArgs",
    "ReconcileAnalyzeArgs",
    "ReconcileConfirmArgs",
    "AttachmentPreviewArgs",
    "attachment_preview",
    "MappingColumnSpec",
    "MappingTemplateArgs",
    "MappingListArgs",
    "MemoryLookupArgs",
    "ErpQueryArgs",
    "ListEntitiesArgs",
    "run_list_entities",
    "ErpCalcArgs",
    "run_erp_calc",
    "ErpSalaryCalcArgs",
    "run_erp_salary_calc",
    "RenderFinancialReportArgs",
    "run_render_financial_report",
    "RenderTaxWorksheetArgs",
    "run_render_tax_worksheet",
    "build_attendance_confirmation_card",
    "build_powder_confirmation_card",
    "build_reconciliation_confirmation_card",
    "build_finance_create_confirmation_card",
    "prepare_attendance",
    "execute_attendance",
    "prepare_order_create",
    "execute_order_create",
    "prepare_order_update",
    "execute_order_update",
    "prepare_finance_create",
    "execute_finance_create",
    "prepare_finance_update",
    "execute_finance_update",
    "prepare_reconciliation",
    "prepare_reconcile_confirm",
    "execute_reconciliation",
    "reconcile_analyze",
    "prepare_mapping_template",
    "run_mapping_template",
    "run_mapping_list",
    "run_memory_lookup",
    "RagRememberArgs",
    "RagRecallArgs",
    "RagForgetArgs",
    "RagSearchDocumentsArgs",
    "run_rag_remember",
    "run_rag_recall",
    "run_rag_forget",
    "run_rag_search_documents",
    "run_erp_query",
    "ExportDataArgs",
    "run_export_data",
    "TOOL_REGISTRY",
    "POLICY_TOOLS",
]
