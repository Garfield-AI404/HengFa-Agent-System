# 恒发 ERP MCP Server 接口文档

适用版本：2026-09-15（字段元数据 `sensitive` 语义段 + 钣金订单附加款 surcharge_amount 上线版；已按运行态 7 个工具复核），MCP SDK `mcp>=1.29.0,<2.0`。

本文档以当前代码注册的工具和订单 Service 为准；订单更新契约最后复核于 2026-09-08。本轮复核内容：新增字段元数据 `sensitive` 语义段（§3：置 `true` 的字段值不得用于 **agent 侧 `erp_calc`** 的 `group_by`/`measure`，agent 侧 fail closed、ERP 本身不拦；agent 侧 `export_data` 默认导出列排除敏感字段、显式点名时放行并注明）；`metal_orders` 新增业务字段 `surcharge_amount`（中文名「附加款」，decimal(10,2)，非负，默认 `0.00`，仅钣金订单；塑粉订单无此字段），可传/可改字段表与金额口径已同步（§6.1/§6.2/§7.1/§7.2/§7.4）。

本版接口行为包括：订单组合创建、乐观锁订单更新、订单通用 CRUD 硬限制、账户级 scope、账户级创建幂等，以及 Agent 完整业务审计与 ERP 最小技术日志的职责分离。

## 1. 连接、认证与作用域

- 协议：MCP Streamable HTTP，JSON-RPC 2.0。
- 地址：`POST /mcp`。
- 认证：`X-API-Key` 优先，`Authorization: Bearer <JWT>` 回退。
- 无效凭据、停用或软删除账号返回 HTTP 401。
- 工具失败仍返回 MCP `CallToolResult`，并设置 `isError=true`。

管理员或执行董事通过 `POST /api/auth/register-agent` 创建 Agent。请求可带 `scopes`：

```json
{
  "username": "erp_agent",
  "password": "备用密码",
  "scopes": ["erp:query", "orders:create_bundle", "orders:update"]
}
```

省略时，新 Agent 默认获得 `erp:query`、`attendance:create`、`attendance:update`、`orders:create_bundle`、`orders:update`、`reconciliation:query`。默认不授予 `reconciliation:write` 或通用 delete。既有 Agent 不自动获得新增的 `orders:update`，由管理员显式补授。

| Scope | 能力 |
|---|---|
| `erp:query` | 查询普通实体、调用 `list_entities` |
| `attendance:create` | 通用 create 创建考勤 |
| `attendance:update` | 通用 update 更新考勤 |
| `orders:create_bundle` | 调用 `create_order_bundle` |
| `orders:update` | 调用 `update_order` |
| `reconciliation:query` | 查询收支、应收收款、应付付款、工资发放 |
| `reconciliation:write` | 写收支、应收收款、应付付款；不开放工资、塑粉消耗或 delete |

admin、执行董事不受 Agent scope 限制。非执行董事保留普通查询能力，但无写权限。`created_by`、`updated_by` 始终由认证账号生成。

## 2. 工具清单

MCP Server 当前共暴露 7 个工具：

| 工具                    | 用途                         |
| --------------------- | -------------------------- |
| `list_entities`       | 返回按调用者过滤后的实体、能力和字段约束       |
| `query`               | 声明式只读查询                    |
| `create`              | 创建允许通用写入的普通实体              |
| `update`              | 更新允许通用写入的普通实体              |
| `delete`              | 删除允许通用删除的普通实体              |
| `create_order_bundle` | 创建钣金或塑粉订单，唯一允许的 MCP 订单创建入口 |
| `update_order`        | 更新钣金或塑粉订单，包含钣金状态修改         |

Agent 项目后续应按岗位只挂载需要的业务工具；创建和修改订单的 Agent 推荐挂载 `query`、`create_order_bundle`、`update_order`，不应直接挂载全部基础 CRUD。本仓库只负责 MCP 服务端约束，本次未修改 Agent 项目的工具挂载。

订单通用 CRUD 在代码层禁止：

```text
create(metal_orders|powder_orders) -> ORDER_CREATE_REQUIRES_BUNDLE
update(metal_orders|powder_orders) -> ORDER_UPDATE_REQUIRES_ORDER_TOOL
delete(metal_orders|powder_orders) -> ORDER_DELETE_NOT_ALLOWED
```

订单的读取仍使用通用 `query`。只有写入需要使用领域工具：创建使用
`create_order_bundle`，修改（包括状态、重量、价格、日期、型号/色号及其他合法业务字段）使用
`update_order`。不要把订单写入拆成多个通用 CRUD 调用。

## 3. `list_entities`

无业务参数。结果按角色和 scope 过滤；无权查询 `users` 的调用者看不到 `users`。敏感字段永不返回。

```json
{
  "name": "metal_orders",
  "label": "钣金订单",
  "description": "...",
  "readable": true,
  "creatable": false,
  "updatable": false,
  "deletable": false,
  "read_only": true,
  "business_operations": ["create_order_bundle", "update_order"],
  "default_order": "-id",
  "fields": {
    "model": {
      "type": "str",
      "label": "型号",
      "required": true,
      "nullable": false,
      "enum_values": null,
      "default": null,
      "min_value": null,
      "max_value": null,
      "min_length": 1,
      "max_length": 100,
      "writable_on_create": false,
      "writable_on_update": false,
      "system_managed": false,
      "sensitive": false,
      "computed": false
    }
  }
}
```

`read_only` 是旧客户端兼容字段；新客户端必须读取四个能力字段。字段 `writable_on_create/update` 描述通用 CRUD。两类订单均返回 `business_operations=["create_order_bundle","update_order"]`，订单工具输入以第 6、7 节为准。

字段元数据的 `computed` 布尔键区分「普通 ORM 列」与「派生/计算列」：普通列 `computed=false`，派生/计算列（例如 `powder_inventory` 的 `order_total`/`consumed_total`/`stock`，均为实时计算值、非数据库列）`computed=true`。计算列**只能作为 `query.fields` 取回**，**不可用于 `query.filter`/`order_by`**——派生字段无法下推 SQL，ERP 对计算列的过滤/排序一律拒绝（`字段 X 为派生字段（实时计算，非数据库列），不支持过滤/排序；仅可用于 fields 返回`）。调用方应据此在提交请求前把计算列从 `filter`/`order_by` 中剔除（或避免生成），不要等待 ERP 报错。

字段元数据的 `sensitive` 布尔键标记「**字段值不得进入 agent 侧 `erp_calc` 的 `group_by`/`measure`**」：置 `true` 的字段（如员工/供货商联系方式、客户/供货商的税号与银行信息）其**值**不得进入分组键或聚合式——agent 侧 `erp_calc` 的 `group_by` 与 `measure`（含 `multiply_field`）对 `sensitive=true` 字段一律 fail closed 拒绝（拦截在 **agent 侧**，**ERP 本身不拦**；`count` 也**一并拦截**——它只回非空行数、不泄露值，模型可改为对非敏感字段 `count` + `filter` 达到同样目的，故能力不受损），值既不会被分组列出、也不会被聚合回传。`sensitive` 只约束**值的使用方式**，字段照常注册、照常可查：仍**可用于 `query.filter`/`order_by`**（筛选/排序**不把字段值回传给调用方**，且 agent 侧字段目录会照常提示这些字段可用于 filter/order_by）。ERP `query` 的 `fields` 取回**本身不限制**敏感字段——`sensitive` 值是否进入模型上下文，由**调用方（agent）自己的结果裁剪白名单**决定（agent 侧 `agent/tools/crop.py` 的 `QUERY_ITEM_WHITELIST`），ERP 仓不存在结果裁剪白名单。

**导出行为**：agent 侧 `export_data` 的**默认导出列排除** `sensitive=true` 的字段（真源同为元数据 `sensitive` 键）；用户**显式点名 `fields`** 时**照旧放行导出**（含敏感字段），并在导出结果中附说明「本次按显式请求导出敏感字段：<字段名列表>」——说明**只列字段名、不含字段值**。

**残余风险（已接受）**：等值/范围 `filter` 不新增泄露，但 `filter` 用 `like` 前缀配合 `count`（如 `filter={"bank_account": {"like": "6222%"}}` + `count`）理论上构成**枚举 oracle**，可借计数逐位逼近出调用方原本不知道的值。现实可行性低（账号约 19 位、受消息步数预算与「每次一条工具调用」限制），故**不因此收紧 `filter`**。

不暴露：`users.password`、`users.api_key`、`webhook_subscriptions.secret`、`file_attachments.stored_name` 和所有 `deleted_at`。

## 4. `query`

| 参数 | 必填 | 类型 | 规则 |
|---|---|---|---|
| `entity` | 是 | string | 当前 `list_entities` 返回的实体名 |
| `fields` | 否 | string[] | 缺省返回全部可见字段 |
| `filter` | 否 | object | 等值简写或操作符对象 |
| `order_by` | 否 | string/string[] | `field` 升序，`-field` 降序 |
| `limit` | 否 | int | 默认 50，最大 200 |
| `offset` | 否 | int | 默认 0，最大 10000 |

操作符：`eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`like`、`in`、`between`。查询使用参数化 SQL，并自动排除软删除记录。

```json
{
  "entity": "metal_orders",
  "fields": ["id", "model", "status", "total_amount"],
  "filter": {"status": "完成", "total_amount": {"gte": "100"}},
  "order_by": "-id",
  "limit": 50,
  "offset": 0
}
```

响应：`{"items": [...], "total": 3, "limit": 50, "offset": 0}`。

### 4.1 常用主数据实体的可查字段（以 `list_entities` 返回的字段目录为权威）

客户、供货商主数据是下单时确定单价/计价口径的**唯一权威来源**（2026-09-08 业务口径：订单单价与计价单位与客户绑定，不按历史订单推断）。

**`customers`（只读）**

| 字段 | 中文 | 类型/取值 | 业务口径 |
|---|---|---|---|
| `id` | 客户ID | int | 匹配/关联键 |
| `company_name` | 公司名称 | string，唯一 | 客户名称字段（`name` 已弃用） |
| `unit_price` | 单价 | decimal(10,4) | **默认 0 = 未定价**，下单须询问，不得采用 |
| `price_type` | 税别 | `含税` / `不含税`，默认 `含税` | 合法默认值，可直接采用 |
| `price_unit` | 计价单位 | `元/kg` / `元/平方`，默认 `元/kg` | 换算订单 `weight_unit`：元/kg→kg、元/平方→m² |
| `freight_bearer` | 运费承担方 | `我方` / `对方` | — |
| `notes` | 备注 | text | — |

`customers` 无 `weight_unit` 列；订单重量单位一律按 `price_unit` 枚举换算，勿再查询不存在的字段。老客户若建档时未维护 `price_unit`，落库为默认「元/kg」——按平方计价的客户须核实主数据。

**`suppliers`（只读）**：`id`、`name`（唯一，名称字段）、`contact_person`、`phone`、`email`、`address`、`bank_account`、`tax_id`、`notes`。塑粉无计价单位列（重量固定 kg），价格按「供货商 + 色号」历史口径（见 §6.1）。

> 供货商 `phone`/`email`/`tax_id`/`bank_branch`/`bank_routing_no`/`bank_account`，以及 `employees.phone`/`emergency_phone`、`customers.tax_id`/`bank_branch`/`bank_routing_no`/`bank_account`，均为元数据 `sensitive=true` 字段，其**值**的使用受限（见 §3）：ERP `query` 照常可查、可 `filter`/`order_by`，但值不得进入 agent 侧 `erp_calc` 的 `group_by`/`measure`；agent 侧默认导出列也排除它们（显式点名 `fields` 才导出并注明）。逐字段权威以 `list_entities` 返回的 `sensitive` 键为准。

## 5. 通用 `create`、`update`、`delete`

均支持可选 `operation_id`（1-128，省略时生成 UUID）和 `trace_id`（1-128）。成功响应回传两者，关联信息写入 `mcp_operation_audits`。同一业务操作重试应复用 `operation_id`；每次传输可以使用不同 `trace_id`。

```json
{"entity": "attendance", "data": {"employee_id": 1, "att_date": "2026-08-19"}, "operation_id": "op-1", "trace_id": "trace-1"}
```

```json
{"entity": "attendance", "id": 10, "data": {"overtime_minutes": 60}, "operation_id": "op-2"}
```

```json
{"entity": "file_attachments", "id": 10, "operation_id": "op-3"}
```

`id`、`deleted_at`、`created_by`、`updated_by`、`created_at`、`updated_at` 等系统字段不可伪造。`transactions.source_type/source_id` 只能由 ERP 联动维护。

管理员/执行董事的基础能力如下；Agent 还会被 scope 收紧，以实际 `list_entities` 为准。

| 实体 | 读 | create | update | delete |
|---|---:|---:|---:|---:|
| `customers` / `customer_contacts` / `suppliers` / `employees` | 是 | 否 | 否 | 否 |
| `users` | 受限 | 否 | 否 | 否 |
| `metal_orders` / `powder_orders` | 是 | 否 | 否 | 否 |
| `file_attachments` | 是 | 否 | 否 | 是，软删除 |
| `webhook_subscriptions` | 是 | 否 | 是 | 是，现有物理删除 |
| `attendance` | 是 | 是 | 是 | 是 |
| `transactions` / `ar_payments` / `ap_payments` | 是 | 是 | 是 | 是 |
| `salary_payments` / `transaction_categories` | 是 | 是 | 是 | 是 |
| `powder_inventory` | 是 | 是 | 是 | 是 |
| `powder_consumption` | 是 | **否** | **否** | **否** |
| `ar_settlements` | 是 | **否** | **否** | **否** |

`ar_settlements`（应收平账 / 抹零结清）：只读实体，`read_only=true`、
`creatable`/`updatable`/`deletable` 均为假，不提供任何写能力（不写
`ar_payments`、不写 `transactions`）。字段为写入时固化的快照，供「当期应收」
按客户求累计平账抹零：`customer_id`、`settled_amount`（抹零额）、
`received_amount`（本次实收）、`settle_date`（平账日期），另有
`outstanding_before`/`total_amount_before`/`paid_amount_before` 三个平账前
快照列；`deleted_by` 为隐藏字段（不注册），记录随软删自动过滤。

`powder_consumption` 的 create/update/delete 已全部关闭（2026-09-11）：MCP 通用写路径不做库存充足性校验，直接改数量会让派生的塑粉净库存变成负数；单独改 `color_code`/`inventory_id` 还会让库存归集到错误色号（`_calc_totals` 按 `color_code` 聚合，而列表按 `inventory_id` 过滤）。`create` 另有独立缺口：`quantity` 列带数据库默认值，通用必填检查不会强制其必填，整条路径也不取任何行锁，与并发的 REST 消耗写入之间没有串行化（可超卖）。三条通路统一改走 ERP REST：新增走 `POST /api/powder-inventory/{id}/consume`、修改走 `PUT /api/powder-inventory/consumptions/{id}`、删除走 `DELETE /api/powder-inventory/consumptions/{id}`（均有行锁 + 库存充足性校验）。

因三个能力位均为否，该实体在 `list_entities` 中 `read_only` 为真、`creatable`/`updatable`/`deletable` 均为假。外部 Agent 若此前依赖通用 `create` 写入消耗记录，需改走上述 REST 端点。

通用工具直接使用 ORM，不触发对应 REST 路由的额外行为。例如通用创建收付款不会自动创建 `transactions`，创建工资不会触发 Webhook。订单必须走公共订单服务。

## 6. `create_order_bundle`

用于创建完整订单。REST 与 MCP 直接调用同一个 Python 业务服务，不通过 HTTP 回调。

说明：MCP 无批量端点。Agent 侧「一张确认卡多条明细」的批量创建，由 agent 逐次单记录调用本工具完成，ERP 侧不提供批量接口。

### 6.1 输入

```json
{
  "idempotency_key": "agent-job-20260819-0001",
  "operation_id": "agent-operation-0001",
  "trace_id": "agent-trace-0001",
  "order_type": "metal",
  "order": {
    "customer_id": 1,
    "model": "HF-001",
    "unit_price": "15.5000",
    "price_type": "含税",
    "status": "未完成",
    "weight_per_unit": "2.5000",
    "weight_unit": "kg",
    "quantity": "100",
    "quantity_unit": "套",
    "surcharge_amount": "0.00",
    "in_date": "2026-08-19",
    "out_date": null,
    "notes": null
  }
}
```

`idempotency_key` 必填，长度 1-128，只允许字母、数字、`.`、`_`、`:`、`-`，且以字母或数字开头。同一业务操作重试必须复用 key；payload 变化后必须换 key。ERP 不按业务字段猜测重复。

`operation_id` 可选，省略时生成 UUID；`trace_id` 可选。二者只用于关联，不表示真实用户，也不进入订单字段或 `request_hash`。同一业务操作重试时，调用方应同时复用 `operation_id` 和 `idempotency_key`；不同请求尝试可以使用新的 `trace_id`。

钣金字段：

| 字段 | 规则 |
|---|---|
| `customer_id` | 必填；客户存在且未软删除 |
| `model` | 必填；去除首尾空格后非空 |
| `unit_price` | 默认 0；不得小于 0 |
| `price_type` | `含税` / `不含税` |
| `status` | 创建时只能为 `未完成` |
| `weight_per_unit` | 默认 0；不得小于 0；0 表示待补充 |
| `weight_unit` | `kg` / `m²` |
| `quantity` | 必须大于 0 |
| `quantity_unit` | `套` / `个` |
| `surcharge_amount` | 「附加款」（元）；可省略（ERP 默认 `0.00`）；不得小于 0；最多 2 位小数；仅钣金订单，塑粉订单无此字段 |
| `in_date` / `out_date` | ISO 日期或 null |
| `notes` | string 或 null |

塑粉字段：

| 字段 | 规则 |
|---|---|
| `supplier_id` | 必填；供货商存在且未软删除 |
| `color_code` | 必填；去除首尾空格后非空 |
| `total_weight` | 默认 0；不得小于 0 |
| `price_per_kg` | 默认 0；不得小于 0 |
| `in_date` | ISO 日期或 null |
| `notes` | string 或 null |

`amount`、`total_amount` 禁止传入。钣金金额（total_amount）为 `quantity × weight_per_unit × unit_price + surcharge_amount`（基础金额 + 附加款）；塑粉金额为 `total_weight × price_per_kg`（塑粉无附加款）。

订单不新增 `ar_payments`、`ap_payments` 或 `transactions`。钣金金额只影响客户动态应收；塑粉金额只影响供货商动态应付；塑粉库存由订单重量减消耗记录动态计算。

### 6.2 成功响应

```json
{
  "status": "created",
  "replayed": false,
  "operation_id": "agent-operation-0001",
  "trace_id": "agent-trace-0001",
  "order_type": "metal",
  "order": {"id": 10, "customer_id": 1, "model": "HF-001", "surcharge_amount": "0.00", "total_amount": "3875.00"},
  "receivables": [],
  "payables": [],
  "transactions": [],
  "other_effects": [
    {"type": "receivable_current_impact", "mode": "derived", "customer_id": 1, "amount": "3875.00"}
  ]
}
```

### 6.3 幂等与并发

数据库唯一键：`principal_id + operation_type + idempotency_key`。不同 Agent 可使用相同文本 key；同一 Agent 的同一操作类型内必须唯一。哈希使用排序 JSON，Decimal、日期、布尔值和 null 稳定规范化，不包含认证信息、`operation_id` 或 `trace_id`。

首次调用先独立记录最小技术 `operation_started`，随后在一个 InnoDB 业务事务中执行：

```text
插入 PENDING
→ 创建订单
→ 计算动态影响
→ 保存安全快照并标记 SUCCEEDED
→ 一次 commit
```

业务事务提交后再独立记录 `operation_succeeded`；技术日志不参与订单原子事务，也不保存完整 payload。

任一步失败，订单、PENDING 和成功快照一起 rollback；原 key 可重试。并发相同 key 时，MySQL 唯一索引插入等待首个事务结束：首个成功则第二个重放并返回 `replayed=true`；首个 rollback 则第二个继续创建。SQLite 不能准确模拟 InnoDB 锁等待，并发测试只在非 SQLite 数据库运行。

正常代码不会提交 PENDING。只有历史或异常数据留下已提交 PENDING 时返回 `IDEMPOTENCY_IN_PROGRESS`，其 `retryable=false` 且 `requires_operator_action=true`，不使用超时猜测。Agent 不得自动重试，也不得更换 key 绕过。运维须确认无活跃事务、无订单和成功快照后人工清理异常记录，再使用原 key 重试。

相同 key、相同 payload 返回原订单和 `replayed=true`。相同 key、不同 payload 返回 `IDEMPOTENCY_KEY_CONFLICT`，不改原记录、不创建订单。

## 7. `update_order`

统一修改钣金和塑粉订单。状态修改已经合并到本工具，不再暴露独立的 `update_order_status`。本工具要求 `orders:update` scope；admin 和执行董事不受 Agent scope 限制。

说明：MCP 无批量端点。Agent 侧「一张确认卡多条明细」的批量修改，由 agent 逐次单记录调用本工具完成，ERP 侧不提供批量接口。

对应的 REST 兼容入口为：

| REST 路由 | 订单类型 | 说明 |
|---|---|---|
| `PUT /api/warehouse/metal/{id}` | `metal` | 通用业务字段更新 |
| `PUT /api/warehouse/powder/{id}` | `powder` | 通用业务字段更新 |
| `PUT /api/warehouse/metal/{id}/status` | `metal` | 历史兼容状态入口，内部仍调用同一 Service |

REST 路由和 MCP 工具不通过 HTTP 互调，均直接调用 ERP 的订单更新 Service。REST 请求可以省略
`expected_updated_at` 以兼容旧客户端；MCP 请求必须提供该字段。无论调用入口如何，金额、外键、状态日期规则和事务边界都相同。

### 7.1 输入

```json
{
  "order_type": "metal",
  "order_id": 10,
  "changes": {
    "weight_per_unit": "2.7500",
    "unit_price": "15.5000",
    "status": "完成",
    "out_date": null
  },
  "expected_updated_at": "2026-08-20T10:15:30.123456",
  "operation_id": "order-update-10",
  "trace_id": "agent-trace-0001"
}
```

| 参数 | 必填 | 类型 | 规则 |
|---|---|---|---|
| `order_type` | 是 | string | `metal` 或 `powder` |
| `order_id` | 是 | int | 大于 0 的订单 ID |
| `changes` | 是 | object | 非空，只包含对应订单允许修改的字段 |
| `expected_updated_at` | 是 | datetime string | 调用前通过 `query` 获取并原样回传，用于乐观锁 |
| `operation_id` | 是 | string | 1-128，跨系统业务操作标识，只进入技术日志 |
| `trace_id` | 否 | string/null | 1-128，调用链标识，只进入技术日志 |

钣金允许修改：

| 字段 | 规则 |
|---|---|
| `customer_id` | 客户必须存在且未软删除 |
| `model` | 去除首尾空格后非空，最多 100 字符 |
| `unit_price` | 大于等于 0，最多 4 位小数 |
| `price_type` | `含税` / `不含税` |
| `status` | `未完成` / `完成` |
| `weight_per_unit` | 大于等于 0，0 表示重量待补充，最多 4 位小数 |
| `weight_unit` | `kg` / `m²` |
| `quantity` | 必须大于 0，最多 2 位小数 |
| `quantity_unit` | `套` / `个` |
| `surcharge_amount` | 「附加款」（元）；大于等于 0，最多 2 位小数（decimal(10,2)）；`0.00` 表示无附加款 |
| `in_date` | `YYYY-MM-DD`，不可为 null |
| `out_date` | `YYYY-MM-DD` 或 null |
| `notes` | string 或 null |

塑粉允许修改：

| 字段 | 规则 |
|---|---|
| `supplier_id` | 供货商必须存在且未软删除 |
| `color_code` | 去除首尾空格后非空，最多 50 字符 |
| `total_weight` | 大于等于 0，0 表示重量待补充，最多 4 位小数 |
| `price_per_kg` | 大于等于 0，0 表示价格待补充，最多 4 位小数 |
| `in_date` | `YYYY-MM-DD`，不可为 null |
| `notes` | string 或 null |

塑粉历史字段 `unit_type`、`quantity`、`unit_weight` 已从模型和数据库删除，不是可查询或可修改字段。塑粉只以 `total_weight` 作为当前功能重量字段。

禁止修改：`id`、`amount`、`total_amount`、`created_by`、`updated_by`、`created_at`、`updated_at`、`deleted_at`，以及未列入上述白名单的字段。系统字段返回 `SYSTEM_FIELD_FORBIDDEN`，其他未知或不适用于该订单类型的字段返回 `VALIDATION_ERROR`。

### 7.2 业务、事务和并发规则

- 钣金金额始终重算为 `quantity × weight_per_unit × unit_price + surcharge_amount`（基础金额 + 附加款；附加款为 `0.00` 时等价于旧口径）。
- 塑粉金额始终重算为 `total_weight × price_per_kg`。
- 字段修改、金额重算、`updated_by/updated_at` 在同一个数据库事务中提交；任一步失败全部 rollback。
- 修改客户或供货商后，动态应收或应付归属随订单变化；不创建 `ar_payments`、`ap_payments` 或 `transactions`。
- 塑粉色号或总重量变化会影响按订单实时派生的塑粉库存，不创建独立库存流水。
- 响应中的 `other_effects` 描述提交后订单对动态应收、应付或库存的当前派生影响，不是“新值减旧值”的数学差额。钣金使用 `receivable_current_impact`；塑粉使用 `payable_current_impact` 和 `inventory_current_impact`。需要数学差额时，Agent 应使用更新前查询快照与更新后订单自行比较。
- 钣金请求显式把 `status` 改为 `完成` 时，`out_date` 未提供或为 null 都写入当天；同时提供具体日期则使用该日期。
- `status` 改为 `未完成` 时，未提供 `out_date` 或传 null 都保留历史日期；传入具体日期时使用该日期。
- 当前已完成的订单只修改 `out_date` 时可以传具体日期修正，但传 null 会返回 `VALIDATION_ERROR`。任何更新结束后，`status=完成` 与 `out_date=null` 的组合都不允许存在。
- ERP 使用行锁并比较 `expected_updated_at`。版本不一致返回 `ORDER_VERSION_CONFLICT`，原订单不变化。
- `update_order` 没有创建型幂等记录。发生版本冲突或网络结果未知时，客户端必须重新 `query`，根据最新订单重新生成差异并重新确认，不得盲目重复调用。

### 7.3 REST 兼容边界

REST `PUT /api/warehouse/metal/{id}`、`PUT /api/warehouse/powder/{id}` 和兼容状态入口 `PUT /api/warehouse/metal/{id}/status` 与 MCP `update_order` 调用同一个订单更新 Service，共用字段白名单、外键校验、金额重算、状态/日期联动和事务规则。REST 保留原 URL 和业务字段格式，并新增可选 `expected_updated_at`；旧前端可以暂时省略该字段，新客户端应提交查询所得版本以启用冲突检测。MCP `update_order` 仍强制要求 `expected_updated_at`。REST Schema 为兼容旧请求仍能解析 `total_amount`/`amount`，但 Service 会返回业务错误，绝不直接写入派生金额。

### 7.4 成功响应

```json
{
  "status": "updated",
  "order_type": "metal",
  "order": {
    "id": 10,
    "customer_id": 1,
    "model": "HF-001",
    "weight_per_unit": "2.7500",
    "unit_price": "15.5000",
    "quantity": "100.00",
    "surcharge_amount": "0.00",
    "total_amount": "4262.50",
    "status": "完成",
    "out_date": "2026-08-20",
    "updated_by": 8,
    "updated_at": "2026-08-20T10:20:00.123456"
  },
  "changed_fields": ["out_date", "status", "total_amount", "unit_price", "weight_per_unit"],
  "other_effects": [
    {"type": "receivable_current_impact", "mode": "derived", "customer_id": 1, "amount": "4262.50"}
  ],
  "operation_id": "order-update-10",
  "trace_id": "agent-trace-0001"
}
```

## 8. 结构化错误

```json
{
  "isError": true,
  "structuredContent": {
    "code": "ORDER_CREATE_REQUIRES_BUNDLE",
    "message": "订单必须使用 create_order_bundle 创建，通用 create 不允许写入订单表",
    "retryable": false,
    "details": {"entity": "metal_orders"}
  },
  "content": [{"type": "text", "text": "ORDER_CREATE_REQUIRES_BUNDLE: ..."}]
}
```

只有需要人工运维介入的错误才额外返回 `requires_operator_action=true`。例如：

```json
{
  "isError": true,
  "structuredContent": {
    "code": "IDEMPOTENCY_IN_PROGRESS",
    "message": "相同幂等键的请求正在处理中",
    "retryable": false,
    "requires_operator_action": true,
    "details": {}
  }
}
```

客户端读取 `structuredContent.code`，不要解析中文。

| Code | 含义 | retryable |
|---|---|---:|
| `VALIDATION_ERROR` | 参数、枚举、格式或业务字段错误 | false |
| `ORDER_NOT_FOUND` | 订单不存在或已经软删除 | false |
| `ORDER_CREATE_REQUIRES_BUNDLE` | 通用 create 创建订单 | false |
| `ORDER_UPDATE_REQUIRES_ORDER_TOOL` | 通用 update 修改订单 | false |
| `ORDER_DELETE_NOT_ALLOWED` | MCP 删除订单 | false |
| `ORDER_VERSION_CONFLICT` | `expected_updated_at` 已过期，必须重新查询 | false |
| `IDEMPOTENCY_KEY_CONFLICT` | key 已用于不同请求 | false |
| `IDEMPOTENCY_IN_PROGRESS` | 异常遗留的已提交 PENDING；固定返回 `retryable=false`、`requires_operator_action=true`，Agent 不得自动重试或换 key 绕过 | false |
| `BUSINESS_ERROR` | 安全包装后的数据库或业务失败 | false |
| `PERMISSION_DENIED` | 角色或 scope 不足 | false |
| `UNKNOWN_ENTITY` | 实体未注册或不可用 | false |
| `UNKNOWN_FIELD` | 字段未注册、敏感或不可见 | false |
| `READ_ONLY_ENTITY` | 实体不允许该通用写动作 | false |
| `SYSTEM_FIELD_FORBIDDEN` | 伪造审计、派生或联动字段 | false |

客户端对 `retryable` 的采纳范围：**只读**（read）工具会采纳服务端声明的 `retryable`（为 `true` 时在重试预算内做有限次自动重试）；**写**（write）工具一票否决——即便服务端声明 `retryable=true` 也不自动重试（依据政策 13.2「不自动盲重试」：结果未知时由调用方按幂等语义决策，不由通用重试层替它决断，防止重复执行）。本节 `retryable` 仍为服务端在结构化错误里声明的既有契约字段，语义与取值不变。

响应不包含堆栈、SQL、密码、API Key 或数据库连接信息。

## 9. Agent 业务审计与 ERP 技术日志边界

Agent 系统是完整业务审计的权威来源，负责保存真实用户、原始指令、结构化意图、轻确认、审批发起人/审批人、审批时间和结果、业务快照、使用过的工具以及最终返回结果。

ERP MCP Server 只记录最小化的技术操作日志，用来证明 ERP 收到了什么工具请求并实际写入了什么结果。ERP 不记录完整对话、模型思考、提示词、审批人、真实用户姓名/角色、完整业务 payload、完整文件或模型输出，也不接受客户端通过 `created_by`/`updated_by` 伪造操作人。`principal_id` 仅表示当前 ERP 认证的 Agent/API 账号。

### 9.1 技术日志字段

`mcp_operation_audits` 字段如下。该表是 ERP 技术操作日志，不是完整业务审计表：

| 字段 | 含义 |
|---|---|
| `event_id` | ERP 事件唯一 ID |
| `event_type` | 技术事件类型，枚举见 9.2 |
| `principal_id` | ERP 认证账号 ID，不是真实用户 |
| `operation_id` | 跨 Agent/ERP 的业务操作关联 ID；同一业务重试复用 |
| `trace_id` | 单次请求/链路关联 ID，可随重试变化 |
| `tool_name` | MCP 工具名 |
| `operation_type` | 当前写入 `create`、`update`、`delete`、`business_tool`；结构预留 `query` |
| `entity_name` | 实体或业务对象名 |
| `record_id` | 实际创建、更新或删除的记录 ID |
| `order_type` | 适用时为 `metal` 或 `powder` |
| `idempotency_key_hash` | 幂等键 SHA-256，不保存原始业务 key |
| `result_status` | `pending`、`success`、`failed`、`replayed`、`rejected` |
| `error_code` | 安全错误码，不保存 SQL、堆栈或连接信息 |
| `created_at` / `completed_at` | 事件开始/完成时间 |
| `duration_ms` | 最终技术事件耗时；`update_order` 成功、失败或拒绝事件均记录 |

旧 `outcome` 字段保留用于历史兼容：`attempt` 对应 `pending`，其他值一一对应新结果状态。

### 9.2 枚举与当前生成规则

`event_type` 支持：

| 值 | 含义 | 当前是否生成 |
|---|---|---:|
| `request_received` | MCP 请求已到达 ERP | 否，预留 |
| `operation_started` | 写工具已通过关联 ID 校验并开始处理 | 是 |
| `operation_succeeded` | 业务写入已经提交 | 是 |
| `operation_failed` | 业务处理失败并已 rollback | 是 |
| `operation_replayed` | 命中成功的订单幂等记录并重放结果 | 是 |
| `operation_rejected` | 权限、参数、状态规则或幂等冲突被拒绝 | 是 |

`result_status` 支持 `pending`、`success`、`failed`、`replayed`、`rejected`。其中 `operation_started` 使用 `pending`；其余事件使用对应的最终结果状态。

当前实际记录技术日志的工具：

| 工具 | `operation_type` | 成功 `record_id` |
|---|---|---|
| `create` | `create` | 新建记录 ID |
| `update` | `update` | 被更新记录 ID |
| `delete` | `delete` | 被删除或软删除记录 ID |
| `create_order_bundle` | `business_tool` | 订单 ID |
| `update_order` | `business_tool` | 钣金或塑粉订单 ID |

`list_entities` 和 `query` 当前不写 `mcp_operation_audits`。HTTP 认证在 MCP 工具执行前失败时也不会产生工具级技术日志。FastMCP 在进入工具函数前拒绝的 JSON Schema 错误，以及非法 `operation_id`/`trace_id`，当前同样无法可靠关联到一条工具日志；客户端仍会收到协议或结构化错误。

### 9.3 事务和失败行为

- `operation_started` 在业务写入前独立提交；它不是业务成功证明。
- 业务事务成功提交后，ERP 记录 `operation_succeeded`；实际结果 ID 必须写入 `record_id`。
- 业务事务 rollback 后记录 `operation_failed`，不会记录 success。
- 幂等重放只记录 `operation_replayed`，不会新增订单或其他业务记录。
- 幂等冲突、权限拒绝和参数拒绝记录 `operation_rejected`。
- 技术日志采用独立、最佳努力事务。日志写入失败会写应用错误日志；当前项目没有外部告警平台，是否触发告警取决于部署环境对应用错误日志的采集配置。日志失败不会阻止业务 rollback，也不会把业务错误改报成成功。
- `operation_id` 和 `trace_id` 不进入订单字段或幂等 `request_hash`；`request_hash` 只由稳定规范化的业务订单输入计算。

MCP Server 不提供技术日志查询工具，普通 Agent 也不得通过通用 `query` 读取技术日志。开发者或运维人员可使用 `operation_id` 和 `trace_id`，通过服务器日志或数据库查询进行关联排查；ERP 不据此推断真实用户或审批结论。

### 9.4 脱敏和保留

- 不保存完整业务 payload、用户对话、模型思考、提示词、审批信息、认证 Header、SQL、堆栈或数据库连接字符串。
- 不保存密码、API Key、JWT、银行卡号或完整文件内容。
- 不保存 `users.password`、`users.api_key`、`webhook_subscriptions.secret`、`file_attachments.stored_name`。
- `idempotency_key_hash` 保存原始幂等键的 SHA-256，用于关联，不保存原值。
- `request_hash` 由规范化订单业务输入计算，不包含认证信息、`operation_id` 或 `trace_id`。
- 当前项目尚未实现 `mcp_operation_audits` 自动清理任务。建议至少保留 90 天；正式保留周期和归档策略由部署方确定。Agent 完整业务审计的保留周期由 Agent 系统独立决定。

## 10. 数据库升级

- `deploy/upgrade-2026-08-19-order-idempotency.sql`：尚未安装幂等和旧审计表的环境，创建 8 月 19 日基础结构。
- `deploy/upgrade-2026-08-19-mcp-hardening.sql`：已经运行过更早的全局唯一幂等脚本时使用；增加 `users.api_scopes`、账户级唯一键、关联字段和旧审计表。
- `deploy/upgrade-2026-08-20-mcp-technical-log.sql`：在已有 `mcp_operation_audits` 上增加最小技术日志字段，保留旧 `outcome` 和历史记录。
- `deploy/upgrade-2026-08-20-remove-powder-legacy-fields.sql`：幂等删除塑粉订单已停用的 `unit_type`、`quantity`、`unit_weight` 列。当前无历史业务数据时直接执行；执行前仍须备份。

旧成功记录从响应快照的 `order.created_by` 迁移到对应主体；无法归属的旧 PENDING 使用保留主体 `0`，上线前应人工核查和清理，不会被任意 Agent 当作自己的幂等记录重放。

生产升级顺序：先根据数据库现状在两个 8 月 19 日脚本中选择一个路径，确保 `mcp_operation_audits` 已存在；随后执行 8 月 20 日技术日志脚本和塑粉遗留列清理脚本。不要在同一旧库重复运行两个 8 月 19 日路径。`orders:update` 不自动写入既有 Agent 的 `api_scopes`，由管理员通过用户管理接口显式补授。全新数据库由 SQLAlchemy `create_all` 创建最终结构。生产目标为 MySQL 8.0.34，升级前必须备份。

## 11. 序列化

| 类型 | JSON 表示 |
|---|---|
| Decimal | 字符串，如 `"15.5000"` |
| Date | `YYYY-MM-DD` |
| DateTime | ISO 8601 |
| Enum | 枚举字符串 |
| bool / int | JSON 原生值 |
| NULL | `null` |
