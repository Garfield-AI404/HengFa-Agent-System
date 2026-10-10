# Skill 正文（skill_texts/）

本目录存放 **skill 正文**，每个文件 = 一个 `skill_key`（文件名与
frontmatter 的 `skill_key` 必须一致，文件名即 `{skill_key}.md`）。
`README.md` 为说明文件，加载时跳过。

## skill 与 policy 是正交两层（勿混淆）

| 层 | 回答什么 | 粒度 | 举例 | 存放位置 |
|---|---|---|---|---|
| **policy** | 什么是对的（领域口径） | 按领域 | 单价按客户主数据、口径归一化用 `over=calendar_days` | `../policy_texts/` |
| **skill** | 先做什么后做什么（编排） | 按任务 | 定口径 → 查目录 → 取数算数 → 补名称 → 导出 → 出报告 | 本目录 |

- skill **引用** policy，**不承载、不替换、不复制**：正文只写步骤与顺序
  （先做什么、再做什么、每步用哪个工具、产出什么），领域口径与格式细则
  由被引用规则包和系统提示词负责，**正文不得复述**。
- 注入时两段并列：编排段来自 skill，口径段来自 policy 原文。

## 文件格式

- frontmatter：`---` 起止，每行 `key: value`。字段：

  | 字段 | 必填 | 语义 | 校验 |
  |---|---|---|---|
  | `skill_key` | 是 | 唯一键，独立命名空间 `SK.*` | 必须等于文件名 stem |
  | `title` | 是 | 显示名 | 非空 |
  | `version` | 否 | 版本号；缺省用代码内 `SKILL_VERSION` | 非空（若给） |
  | `index` | 是 | 决策轮索引表用，**一行** | 非空；单字段上限 `SKILL_ADMISSION_MAX_CHARS`（200 字），超长 RuntimeError |
  | `when_not` | 否 | 不适用边界（何时**不要**用）；与 `index` 一正一反构成准入描述 | 单字段上限同上 |
  | `policies` | 否 | 引用的 policy_key 列表（逗号分隔） | 每个 ∈ `ALL_POLICY_KEYS`，否则 RuntimeError |
  | `tools` | 否 | 允许调用的工具名列表 | 每个 ∈ `TOOL_REGISTRY`，否则 RuntimeError；**不做强制白名单**，但参数轮按它决定注不注入流程编排段（本轮工具在表外 → 不注入） |
  | `native_tools` | 否 | **预留位**，一期必须为空 | 非空则 RuntimeError（一期不支持） |

  解析纪律（均 fail closed，误写不会被静默容忍）：**未知键**（如把 `policies`
  拼成 `policy`）→ RuntimeError，否则引用会静默变空；**重复键** → RuntimeError，
  否则后值静默覆盖前值、可绕过越界引用校验；**非空且不含 `:` 的行**（值折行）
  → RuntimeError，否则折行的部分会被静默截断；整个 frontmatter 里每个字段
  各占**一行**，长值不要换行。

- 正文：编号步骤行（`1. ` 起），加载时剥离编号前缀、渲染时重新编号；
  纯文本，**不写条件跳转 / 前置校验等 DSL**（一期只渲染不解释）。

## version 与 hash 纪律

- 修改任何 skill 正文**必须**递增 frontmatter 的 `version`；
- `skill_hash = sha256("{skill_key}|{version}|{text}")`——与
  `PolicyPackage.hash`（`../policies.py`）同口径，便于审计一致性；
- skill 走独立命名空间，**不写入** `audit_events` 的
  `policy_key/policy_version/policy_hash` 三列（那三列语义是"写操作当时的
  领域规则版本"，skill 是编排，不改变规则）；
- 新增 / 删除 skill 必须同步 loader 内的期望键集（fail closed：目录缺失、
  文件缺失、存在未注册文件、解析失败、键集不一致、引用越界 → import 即
  `RuntimeError`，不在运行期静默降级）。

## 当前状态

本目录为 skill 层一期交付，**已接入运行期**（2026-09-13）：

- **已登记流程包**：`SK.financial_report`（财务报表）、`SK.receivables`
  （当期应收账款）、`SK.tax_estimate`（当期增值税估算）；期望键集以
  `../skills.py` 的 `_EXPECTED_SKILL_KEYS` 为准（新增/删除必须同步，否则
  import 即 `RuntimeError`）；
- **加载器** `../skills.py`：import 期建注册表，本目录下的每个 `SK.*.md`
  都会被解析并做完整校验（`RuntimeError` = 应用起不来，这是刻意设计：正文
  与代码同包分发，写错必须在启动时就暴露，而不是运行期静默降级）；
- **声明字段** `AgentDecision.skill_name`（模型每轮声明当前流程，可为 null）；
- **状态字段** `State.active_skill`（**last-write-wins 覆盖语义**，不是
  `policy_keys` 那种累积语义）；
- **注入点**：决策轮 `model_node`（索引表 + 激活流程正文 + 其引用规则包正文）、
  参数轮 `tool_args_node`（仅当本轮工具 ∈ 本 skill 的 `tools` 时注入编排段，
  不重复注入口径段——口径由工具的 `policy_family` 负责）；
- **本轮不写审计列、不新增 SSE 事件、前端零改动**（见方案 §2.3 / §3.4）。

新增 skill 的完整步骤：写 `SK.<name>.md` → 把 key 加进 `skills.py` 的
`_EXPECTED_SKILL_KEYS` → 跑 `tests/unit/test_skills.py`。方案全文见
`docs/plans/skill-layer-plan.md`（本地规划文档，gitignored）。
