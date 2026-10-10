# 前端 e2e（Playwright）

覆盖**稳定、不依赖大模型、默认不写入 ERP** 的链路：登录、登录失败提示、
未登录重定向、角色越权 403、主页面无未捕获 JS 错误。

> **红线一（不变）**：本目录文件全部入库，**不得出现任何账号 / 密码字面量**，
> 凭据一律走环境变量注入（见 §2）。代码与文档里都不回显口令值。
>
> **红线二（口径更新 2026-09-12）**：**默认不得点击确认卡触发 ERP 写入**。写工具
> 链路的 e2e（`order-write.spec.ts`）现在**可以测**了——因为我们已有**完全本地、
> 零生产接触**的环境（本地 SQLite 版 ERP + 本地 agent 后端），写操作只落到本地库
> 的临时文件。但它**默认 skip**：只有显式打开门禁（`E2E_ALLOW_WRITE=1`）且把
> `E2E_LOCAL_ERP_URL` 指向**回环地址**时才真正执行；门禁不是回环**直接 fail**。
> 门禁变量与前置条件见 §3。

## 1. 起服务

后端（不交给 Playwright 托管：依赖 Postgres / ERP MCP、启动慢、环境差异大），
在项目根执行：

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m uvicorn administrative_agent.main:app --host 127.0.0.1 --port 18000
```

探活：`curl http://127.0.0.1:18000/api/v1/health` → `{"status":"ok",...}`。

> 端口固定 18000：本机 8000 落在 Windows 保留端口段（7929-8028），绑定会失败。

前端 dev server 通常**不用手动起**——`playwright.config.ts` 的 `webServer` 会拉起
vite（5173），且 `reuseExistingServer` 保证不与已运行的实例冲突。要手动起：

```bash
cd frontend && npx vite --port 5173
```

前端已配 `/api` 同源代理到 18000，用例全程同源，无需处理 CORS。

## 2. 注入凭据（账号密码只走环境变量）

从仓库根 `.env`（已 gitignore）导出后改名注入，测试文件本身不含任何凭据：

```bash
cd frontend

# 读入 .env（set -a 让所有赋值自动 export）
#
# ⚠️ 关键坑：manager 凭据现在是单个 JSON 列表键 MANAGER_ACCOUNTS，值里含 {} [] 与逗号。
# 未加引号时，`source` 会先对它做花括号展开（{a,b}）与路径名展开，导致解析报错或
# 赋值被破坏。因此 .env 里这一行必须把整个 JSON **用引号括起来**，例如：
#   MANAGER_ACCOUNTS='[{"account":"hengfa","password":"<hex>"},{"account":"manager02","password":"<hex>"}]'
# （单引号对后端 pydantic-dotenv 亦安全，它会剥离外层引号。）旧的四个编号键
# MANAGER_ACCOUNT / MANAGER_PASSWORD / MANAGER_ACCOUNT_2 / MANAGER_PASSWORD_2 已作废、
# 写了不生效；若你的 .env 仍是旧键，请先迁移为上面的 MANAGER_ACCOUNTS 再继续。
set -a
source ../.env
set +a

# 改名成 e2e 读取的变量名。
# manager 不再有独立的 MANAGER_ACCOUNT/PASSWORD 变量：从 MANAGER_ACCOUNTS JSON 列表
# 取第 N 条（下标 0 = 列表第一条；换账号只改末尾的 [0]）。
export E2E_MANAGER_ACCOUNT=$(python -c "import json,os;print(json.loads(os.environ['MANAGER_ACCOUNTS'])[0]['account'])")
export E2E_MANAGER_PASSWORD=$(python -c "import json,os;print(json.loads(os.environ['MANAGER_ACCOUNTS'])[0]['password'])")
export E2E_DEVELOPER_ACCOUNT="$DEVELOPER_ACCOUNT"
export E2E_DEVELOPER_PASSWORD="$DEVELOPER_PASSWORD"
```

变量缺失时用例会 **skip 并在报告里写明缺哪个变量**，不会用默认账号去撞登录接口
（避免污染后端失败计数、也避免误导性的失败信息）。

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `E2E_BACKEND_URL` | `http://127.0.0.1:18000` | 后端探活地址；改过 vite proxy target 时同步覆盖 |

## 3. 写工具链路 e2e（默认跳过，仅本地环境）

`order-write.spec.ts` 覆盖：**一条口语消息包含两条同类钣金订单 → 界面出现一张
确认卡（两行明细）→ 点「确认」→ 本地 ERP 实际落入 2 条**。它会**真实点击确认卡
触发写**，因此**默认 skip**，且只允许打向回环地址上的本地 ERP。

> 为什么是「一张卡两条明细」而不是「两张卡」：真实模型会把同一条消息里的多条
> 同类记录合并成一次 `order_create`（items 两条）；而「两条异类 → 两张卡」在结构
> 上不可能——后端 `route_after_tools`（`src/administrative_agent/agent/graph.py`）
> 在写工具产出 `pending_confirmation` 后直接路由到 `confirm`，**一条用户消息最多
> 产出一张写确认卡**。故用例断言「恰一张卡、两行明细」。

### 3.1 门禁（三道断言，顺序在最前、早于任何写入）

| # | 断言 | 结果 |
|---|---|---|
| ① | 未设 `E2E_ALLOW_WRITE=1`（或非 `1`） | **skip** |
| ② | 未设 `E2E_LOCAL_ERP_URL` / 未设 `E2E_LOCAL_ERP_API_KEY` | **skip** |
| ③ | `E2E_LOCAL_ERP_URL` 的 host **非回环**地址 | **fail** |
| ④ | agent 后端**实际写入的 ERP**（见下）host **非回环**，或探测取不到 | **fail** |

**硬闸（防误打生产）**：③ 与 ④ 都是 `fail` 而不是 `skip`——宁可用例失败，也绝不
让确认卡触发的写请求落到回环之外。

- ③ 校验的是**核对用的地址**（用例去查询落库的那个 ERP）；host 必须是回环
  （`127.0.0.1` / `localhost` / `::1`）。
- ④ 校验的是**写请求真正的落点**：真正执行写入的不是用例，而是**前端经 agent
  后端**打到后端配置的 `ERP_MCP_URL`。用 developer 账号登录取
  `GET /api/v1/developer/health`（`src/administrative_agent/web/routes_developer.py`），
  从 `dependencies` 取出 `erp_mcp` 项的 `detail`（形如 `host=127.0.0.1`，仅 host、
  无凭据），断言该 host 回环。**这是关键闸**：若有人把 `E2E_LOCAL_ERP_URL` 设成
  回环、但后端其实连的是生产 ERP，只有 ④ 能拦住——所以 developer 凭据缺失、
  登录失败、或 `erp_mcp` 无 host（如 `not_configured`）时，用例 **fail 并把配置
  问题暴露出来**，绝不降级为 skip。
  （developer 凭据沿用 §2 的 `E2E_DEVELOPER_ACCOUNT` / `E2E_DEVELOPER_PASSWORD`。）

### 3.2 前置条件（本地栈怎么起、怎么指向）

> 本地栈属于 agent 侧基础设施，**不由本用例托管**；用例只做前置探活，未就绪时
> skip 而非误报失败。

1. **本地 ERP**（本地 SQLite，端口 `18001`）：由 ERP 侧本地启动脚本拉起，运行时
   配置指向 `hengfa-erp/backend/dev-e2e.env`（该文件**不入库、非生产**；含
   `ERP_MCP_URL=http://127.0.0.1:18001/mcp` 等）。
2. **本地 agent 后端**（端口 `18000`）：其 ERP MCP 指向上述本地 ERP
   （`ERP_MCP_URL` / `ERP_MCP_API_KEY` 均指向 18001）。第 ④ 道断言会实测这一点。
3. **前端 dev server**：同 §1，由 playwright `webServer` 托管。
4. **本地 seed 数据**：用例假定已建客户「昌盛五金」（元/kg 计价）——
   见 `hengfa-erp/backend/dev-e2e-seed.json`。改动本地 seed 时同步该用例的
   `CUSTOMER_NAME` 常量。

### 3.3 注入门禁并运行（示例）

```bash
cd frontend
set -a; source ../.env; set +a                 # 拿 MANAGER_ACCOUNTS/DEVELOPER_ 等（同 §2；JSON 值需带引号）
export E2E_MANAGER_ACCOUNT=$(python -c "import json,os;print(json.loads(os.environ['MANAGER_ACCOUNTS'])[0]['account'])")
export E2E_MANAGER_PASSWORD=$(python -c "import json,os;print(json.loads(os.environ['MANAGER_ACCOUNTS'])[0]['password'])")
export E2E_DEVELOPER_ACCOUNT="$DEVELOPER_ACCOUNT"   # 第 ④ 道断言（developer 探测）用
export E2E_DEVELOPER_PASSWORD="$DEVELOPER_PASSWORD"

export E2E_ALLOW_WRITE=1                        # ① 显式开关
export E2E_LOCAL_ERP_URL="http://127.0.0.1:18001"   # ②/③ 回环地址，否则 fail

# ④/核对：**本地 ERP 实例**的 Agent API Key——取自本地 ERP 的运行时配置
# hengfa-erp/backend/dev-e2e.env 的 ERP_MCP_API_KEY（勿用仓库根 .env 的同名变量：
# 后者可能指向生产/已轮换，用它查本地 ERP 会得到 401）。
export E2E_LOCAL_ERP_API_KEY="$(grep -E '^ERP_MCP_API_KEY=' ../../hengfa-erp/backend/dev-e2e.env | cut -d= -f2-)"

pnpm test:e2e -- order-write.spec.ts            # 或 npx playwright test --config=playwright.local.config.ts order-write.spec.ts
```

**严禁**：把 `E2E_LOCAL_ERP_URL` 指向任何非回环地址；把生产凭据注入
`E2E_LOCAL_ERP_*`。核对方式为直连本地 ERP 的
`GET /api/warehouse/metal?model=<本次唯一标记>`（`X-API-Key` 认证），**不信 UI 文案**。

### 3.4 已知残余风险（尚未消除，如实记录）

1. **弱约定 `E2E_ALLOW_WRITE=1`**：只有字符串恰为 `1` 才视为开闸，`true` 之类会
   被当成未开而 skip（偏安全）；但缺少对「误设成其它真值」的显式告警。
2. **无自动化护栏防止把生产凭据注入 `E2E_LOCAL_ERP_*`**：目前依赖操作者自律与
   地址校验。可考虑在门禁里对 API Key 做一次「只读自检」以证明它只能查不能写，
   但会抬高复杂度。
3. **本门禁仍是「客户端自律」层**：三道断言都在测试进程内。若要在架构上更硬，
   建议 agent 后端侧提供「本地模式」标志，只有置位的实例才接受写工具真实执行
   （生产实例永不置位），使前端门禁成为第二道而非唯一一道闸——这属后端改动，
   需转派 `agent-backend`。

### 3.5 常见误配

- **核对查询返回 `HTTP 401`**：`E2E_LOCAL_ERP_API_KEY` 不是**本地 ERP 实例**的
  Agent Key。最常见是误用了仓库根 `.env` 的 `ERP_MCP_API_KEY`（它可能指向生产/
  已轮换）。请改用 §3.3 脚本里取自 `hengfa-erp/backend/dev-e2e.env` 的那个值。
  注意：写入本身可能已经成功（门禁 ④ 只管后端落点），401 只说明**用例查不到**
  结果——所以务必用对 key，否则写链路"跑了却验证不了"。
- **门禁 ④ fail 且提示 `erp_mcp` 无 host / status 非 `configured`**：本地 agent
  后端的 `ERP_MCP_URL` 未配置或未指向本地 ERP，属配置问题，按设计 fail 暴露。

## 4. 跑测试

有 Chromium 的环境（CI / 能访问 cdn.playwright.dev）：

```bash
pnpm exec playwright install chromium
pnpm test:e2e
```

本机（网关拒绝 cdn.playwright.dev，改用系统 Edge）：

```bash
npx playwright test --config=playwright.local.config.ts
```

`playwright.local.config.ts` 被 `.gitignore` 忽略、不入库，只继承 base config 并把
chromium project 的浏览器换成 `channel: 'msedge'`。

常用附加参数：`--headed`（看界面）、`--ui`、`-g "登录"`（按标题过滤）、
`--workers=1`（排查并发干扰）。

类型检查（e2e 目录不在根 tsconfig 的 references 内，单独配置）：

```bash
pnpm exec tsc --noEmit -p e2e/tsconfig.json   # 仅检查 e2e/*.ts
```

## 5. 失败排查

- 报告：`npx playwright show-report`（HTML 报告在 `playwright-report/`，已 gitignore）。
- 失败截图 / trace：在 `test-results/`（已 gitignore）。
- 后端没起 → 用例 skip 并提示「后端未就绪」，不是失败。
- 选择器对不上 → 说明前端改了结构，请同步更新用例（本目录用例的选择器均注明
  了来源组件，改组件时按图索骥即可）。
