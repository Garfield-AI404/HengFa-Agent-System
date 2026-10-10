# 规则包正文（policy_texts/）

本目录存放 15 个规则包的正文，每个文件 = 一个 policy_key（文件名与
frontmatter 的 `policy_key` 必须一致）。正文由
`src/administrative_agent/agent/prompts/policies.py` 的 `_load_policy_texts()`
在 import 时加载（fail closed：文件缺失/多余/键不匹配/解析失败 → 抛
RuntimeError，import 即失败）。

[2026-09-02 R7 收支写] 新增 `R7.finance.create.md`（收支记录创建）与
`R7.finance.update.md`（收支记录更新），规则包总数 12 → 14。

[2026-09-15 应收应付余额口径] 新增 `R4.balance.scope.md`（应收应付余额
口径，绑定 erp_query/erp_calc 参数轮注入），规则包总数 14 → 15。
[2026-09-16 当期应收口径开放] 只改 `R4.balance.scope.md` 正文（键集不变）：
当期应收改为经 SK.receivables 流程给出，未结余额仍以 ERP 财务-资产管理
页为准；version `2026-09-16.2` → `2026-09-16.3`（把第 3 条禁令对本流程的
豁免绑进第 1 条的授权句）。
[2026-09-17 当期应收合计口径] 只改 `R4.balance.scope.md` 正文第 1 条
（键集不变）：客户级汇总改为「只看当期实际应收」——负值户**不计入**合计
（在合计中按 0 计），只把当期应收为正的客户相加，该数唯一正名为「实际应收
合计（不含负值户）」；另列「负值户小计」，使「实际应收合计（不含负值户）
+ 负值户小计 = A合计 − B合计 − S合计」闭合可核；负值明细行照常保留并注明
预收含义。version `2026-09-16.3` → `2026-09-17.1`。

[2026-09-28 字段盲区] 只改 `R4.query.md` 正文规则 1（键集不变）：由「显式
指定时只返回所需字段」改为「fields 默认不传、只有结果体量明显过大时才收窄，
且优先缩小 filter/时间窗而非砍字段；未出现的字段一律视为本次未取到、不得据此
推断该字段不存在或不可写」。version `2026-09-10.1` → `2026-09-28.1`
（规则 2/3/4 一字未动）。

[2026-09-29 确认卡改进第 2 批] 8 份规则包正文变更（键集不变）：
- **超限例外**：`R2.order.create.metal.md`、`R2.order.create.powder.md`、
  `R3.order.update.metal.md`、`R3.order.update.powder.md`、
  `R7.finance.create.md`、`R7.finance.update.md` 六份，在「禁止把同轮多条
  拆成多次调用」后同条追加「超限（>20 条）例外：不得自行删减条目、也不得只
  提交前 20 条；必须先向用户说明实际条数，并询问是否分批确认」→ 六份
  version 均升 `2026-09-29.1`；
- `R4.query.md` 新增规则 5（目标定位与提问边界：名称→ID、结果多条先自行
  消歧、不得凭一次返回否定存在、提问攒齐）→ `2026-09-29.1`；
- `R1.attendance.daily.md` 规则 3 单位收口为「一律按小时填写」，规则 6 保留
  用户业务事实、单位改小时并保留 540 分钟等值括注，新增规则 7（迟到 N 小时
  → 请假 N 小时）/8（语义折算须显式说明）/9（严禁因无字段而静默丢弃）→
  `2026-09-29.1`。

## 文件格式

- frontmatter：`---` 起止，每行 `key: value`，三字段
  `policy_key` / `title` / `version`（`version` 缺省用代码内
  `POLICY_VERSION`）；
- 正文：编号规则行（`1. ` 起），加载时剥离编号前缀存入 `rules`，
  渲染（`PolicyPackage.text`）时重新编号——正文与编号必须一一对应。

## version 递增纪律

- 修改任何规则正文**必须**递增 frontmatter 的 `version`（与
  `policies.py` 的 `POLICY_VERSION` 缺省值保持同口径）；
- `policy_hash = sha256("{policy_key}|{version}|{text}")`（11.1 审计
  口径）；正文变化而 version 不变会导致审计回放一致性失败
  （audit_events 记录的 version/hash 与当前规则包不一致）；
- 只允许改正文，不允许借机修改业务口径；新增/删除规则包必须同步
  `policies.py` 的 `_EXPECTED_POLICY_KEYS` 与本文档目录清单。
