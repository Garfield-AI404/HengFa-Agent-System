# 恒发内勤 Agent · 部署文档

恒发内勤 Agent：连接真实 ERP 的对话式内勤助手。manager / developer 两类账号登录后在对话中完成
ERP 查询、订单与收支录入、考勤记录、Excel 对账与导出——所有写操作先出确认卡、用户确认后才落库，
全程审计留痕。本文件即部署手册，覆盖 Docker Compose 单机部署形态的架构、配置、部署、上线检查与
日常运维；开发联调见[第 8 节](#8-本地开发)。

## 1. 功能与工具域

系统按「对话 → 工具 → 确认卡 → 审计」工作：用户登录后与 Agent 对话；决策循环（LangGraph）中，
涉及 ERP 数据的问题先经 `erp_query` 查询再作答，不反问能查到的问题；**写操作一律先构造确认卡**
（含写入计划明细与载荷 hash），用户点击卡片确认后才执行，attempt / result 全程审计留痕。

写操作是两段式执行：

1. **准备（prepare，只读）**：Agent 完成 ERP 实体查询、名称唯一匹配、资料补全与金额规范化等全部
   只读前置，构造确认卡并把执行上下文（draft/plan 序列化）一并写入会话待确认状态——此时不触碰 ERP；
2. **确认后执行（run）**：用户点击卡片确认后，执行器用同一执行上下文调用真实 Executor 落库——
   「批准的卡片」就是「执行的载荷」，载荷任何变化（payload hash 不一致）即失效拒绝。

失败纪律：prepare 阶段失败（匹配歧义/未命中/查询失败/资料缺失）转为错误消息，由模型向用户说明并
自纠——不落 ERP、不静默；执行阶段错误返回失败原因。业务对话工具（含全部写工具）
`requires_manager=True`，仅供 manager 角色在业务对话中调用。

工具按域注册于 `src/administrative_agent/agent/tools/__init__.py`（`TOOL_REGISTRY` / `ToolSpec`），
下表说明以其注册 description 为准：

| 域 | 工具 | 说明 |
|---|---|---|
| 通用查询（QRY） | `erp_query` | ERP 只读查询：实体固定为 metal_orders / powder_orders / customers / suppliers / employees / transactions / transaction_categories / customer_contacts / attendance / ar_payments / ap_payments / salary_payments / powder_inventory / powder_consumption / ar_settlements 共 15 个，支持字段筛选、排序、分页 |
| 会话记忆（MEM） | `memory_lookup` | 只读：按段号查压缩存档；或按 query_id 分页取回某次工具查询的原始明细（取齐 total 条后才可下结论） |
| 订单（R2） | `order_create` | 创建订单：钣金需客户/价格类型/重量单位，塑粉需供货商/色号；metal 与 powder 字段互斥，冲突字段剔除后重试；可查参数先用 erp_query 补全 |
| 订单（R3） | `order_update` | 修改订单：给出目标订单 ID 与期望变化字段；status 只允许「未完成 / 完成」 |
| 收支（R7） | `finance_create` | 创建收支：方向按口语语义判定（花了/付了/支付…→支出；收到/回款/进账…→收入），分类从 transaction_categories 强校验；确认卡呈现方向/分类供核对 |
| 收支（R7） | `finance_update` | 修改收支：目标记录 ID + 期望变化字段，只改与当前值不同的字段；分类变更须归属生效方向 |
| 考勤与工资（R1） | `attendance_plan_execute` | 记录每日考勤：按员工名单与日期批量写入；确认卡含写入计划明细，卡片即确认入口 |
| 对账映射（R6） | `mapping_template` / `mapping_list` | 映射模板生命周期（草稿 → published → 退役，写本地配置表不触 ERP）与版本查询；对账只使用 published 模板 |
| 对账分析（R6） | `reconcile_analyze` | 对上传的 Excel 附件按已发布映射模板解析对账（钣金=客户名、塑粉=供货商名），生成差异报告并返回 report_id |
| 对账执行（R6） | `reconciliation_execute` | 按 report_id 重建写入计划并出确认卡，确认后逐条执行差异 create/update；报告已变化（hash 不一致）则拒绝执行 |
| 导出（QRY） | `export_data` | 按实体导出 Excel（只读）：字段/筛选直接透传，缺省 = 实体默认列/全量；文件 1 小时内可下载；超行数上限拒绝并提示加过滤 |

角色：`manager` 执行业务对话（上表工具均归其使用）；`developer` 提供运维类 API / 面板。账号口令由
环境注入（见[第 4 节](#4-配置env)）。

会话上下文与记忆：会话状态随 LangGraph checkpoint 持久化；超长对话按 `COMPRESSION_TIERS` 分层阈值
压缩并生成存档段，模型可经 `memory_lookup` 分页取回细节（单次返回受 `LOOKUP_RESULT_CHARS` 限制）。
调优项默认值即代码默认、通常无需修改，完整字典见 `.env.example`「上下文与压缩预算」分组。

## 2. 架构与边界

```text
用户浏览器
   │  演练：http://127.0.0.1:8080（WEB_PORT）；生产：受管反向代理 https 前置
   ▼
web  nginx:1.27-alpine（唯一 HTTP 入口，仅绑 127.0.0.1）
   ├── /            → frontend/dist 静态文件（前端独立制品，无后端模板渲染）
   └── /api/v1/*、SSE → api（内部转发；SSE 禁缓冲 proxy_buffering off）
                          │
api     uvicorn FastAPI + LangGraph（容器内 18000，不发布端口）
worker  python -m administrative_agent.worker（容器内 18002 健康端点，仅供编排）
migrate alembic upgrade head（一次性迁移任务）
db      postgres:16-alpine（业务表 + LangGraph checkpoint 共库；127.0.0.1:15433 回环发布）
                          │  ERP MCP：内网 HTTP，X-API-Key 认证
                          ▼
                   真实 ERP（契约见 mcp-api.md）
```

端口一览：

| 服务 | 命令 | 容器内端口 | 宿主机发布 | 说明 |
|---|---|---|---|---|
| db | postgres:16-alpine | 5432 | 127.0.0.1:15433 | 仅运维/备份访问（pg_dump）；生产不发布 |
| migrate | alembic upgrade head | – | – | 一次性任务，成功后 api/worker 才启动 |
| api | uvicorn :18000 | 18000 | 不发布 | /api/v1 业务端点与 SSE，仅编排内网可达 |
| worker | python -m administrative_agent.worker | 18002 | 不发布 | /healthz 健康端点，仅供编排 healthcheck |
| web | nginx:1.27-alpine | 80 | 127.0.0.1:${WEB_PORT:-8080} | 静态托管 + 反向代理入口 |

部署形态边界：

- **前端是独立制品**：`frontend/dist` 不入库、不经后端渲染，由 nginx 卷挂载直读
  （`./frontend/dist:/usr/share/nginx/html:ro`）；FastAPI 只服务 `/api/v1`、SSE、文件上传与健康检查，
  不提供 HTML 模板。
- **SSE 反代禁缓冲**：nginx 模板对 `/api/v1/chat/streams/*` 等事件流配置 `proxy_buffering off`、
  `proxy_cache off`、读取超时 3600s（见 `deploy/nginx/default.conf.template`）。
- **登录防爆破在网关层**：nginx limit_req 限速登录端点 `/api/v1/auth/login`
  （`NGINX_LOGIN_RATE` / `NGINX_LOGIN_BURST`），超限返回标准 429。
- **凭据纪律**：镜像与编排文件不含任何密钥——compose 全部 `${VAR:-默认}` 兜底，真实值只经 `.env`
  （运行环境）注入；`.env` 不进入镜像层（Dockerfile COPY 白名单未含它），仓库根 `.dockerignore`
  已将其排除在构建上下文之外。镜像仅 EXPOSE 18000/18002，
  对外发布面完全由编排与宿主映射控制。
- **回环绑定**：db 与 web 只监听 127.0.0.1，供本机演练与运维；生产把入口交给受管反向代理（https）
  后移除本机绑定。Cookie Secure 依赖 https 链路，见[第 6 节](#6-生产上线检查清单)。
- **账号与会话**：无用户表——manager / developer 账号口令由环境注入，登录后签发签名会话 Cookie
  （`SESSION_SECRET` / `SESSION_TTL_MINUTES` / `COOKIE_SECURE` / `COOKIE_SAMESITE`）；浏览器只持
  HttpOnly 会话 Cookie，认证令牌不落 localStorage，前端不持有任何 ERP / 模型 / 数据库凭据。
- **数据与文件**：Agent 自有 PostgreSQL，业务表与 LangGraph checkpoint 共库（`CHECKPOINT_DATABASE_URL`
  独立变量、允许分库部署）；checkpoint 保留受 `CHECKPOINT_RETENTION_DAYS` 约束（默认 30）。对话附件、
  导出文件与对账报告均为文件 + 元数据行，服务启动时与附件上传/导出发生时清理过期项
  （附件/导出保留 1 小时、对账报告 24 小时）。
- **可观测与停机**：structlog JSON 结构化日志（`LOG_LEVEL`）；SIGTERM 触发优雅停机，按
  `SHUTDOWN_DRAIN_TIMEOUT`（默认 30s）等待进行中写调用落审计后退出。
- **worker 为预留形态**：独立 worker 进程的部署组件当前职责是运行时宿主就绪验证与运维生命周期
  （启动检查、checkpoint 表就绪、图编译、优雅停机）；任务分派/异步执行随真实执行器接线后落地，
  chat 流水线现为同步路径（详见 `src/administrative_agent/worker.py` 模块注释），不应视为已生效的
  异步执行。

部署相关文件速览：

| 路径 | 作用 |
|---|---|
| docker-compose.yml | 单机编排：服务、依赖链、健康检查、变量插值（服务名 hengfa-agent-system） |
| Dockerfile | 后端镜像（pip install .，migrate/api/worker 三命令共用）；不含前端 |
| deploy/nginx/default.conf.template | 反代模板：静态 + /api/v1 与 SSE 转发、登录限速（nginx 官方镜像 envsubst 渲染） |
| .env.example | 环境变量字典（分组带注释）；复制为 .env 使用 |
| frontend/dist | 前端构建产物（不入库）；nginx 卷挂载目录，部署前在宿主构建 |
| .dockerignore | 构建上下文排除清单：.env、git/缓存/前端产物等不进 Docker daemon（镜像层白名单见 Dockerfile COPY） |

## 3. 前置条件

- **Docker Engine + Docker Compose**（v2 语法，使用 `docker compose` 子命令）。
- **前端构建环境**：Node.js >= 22、pnpm >= 11（仅部署构建时使用，见[第 5 节](#5-部署步骤)步骤 1）。
- **真实 ERP 的 MCP 服务**：内网 HTTP（MCP Streamable HTTP，`POST /mcp`），需服务地址与 API Key
  （X-API-Key 优先，Bearer JWT 回退，见 mcp-api.md §1）。未配置时依赖 ERP 的工具 fail closed——
  报错属预期行为。
  - ERP 侧需预建 Agent 账号并授最小 scope：管理员经 `POST /api/auth/register-agent` 创建（默认授
    erp:query、attendance:create/update、orders:create_bundle/update、reconciliation:query，不授
    reconciliation:write 与 delete）；收支写入与 `finance:create` / `finance:update` 占位 scope 名
    须与 ERP 权限表核对后再收紧（详见 mcp-api.md §1 与 `src/administrative_agent/agent/tools/base.py`
    注释）。
- **OpenAI 兼容协议模型 API**（默认 deepseek-v4-flash）：需 base_url 与 API Key。未配置时模型决策
  fail closed，对话不可用——属预期行为。

> 本手册命令按 Git Bash / POSIX shell 书写；前端构建要求实测验证于 Node v24 / pnpm 11
> （下限 Node >= 22 / pnpm >= 11）。模型与 ERP 未配置时服务可正常启动、健康检查通过，但对话与
> ERP 工具按 fail closed 报错——补全配置即恢复，不是故障。

## 4. 配置（.env）

```bash
cp .env.example .env
```

凭据纪律：镜像与编排文件不含任何密钥，全部经环境变量注入（compose 一律 `${VAR:-默认}` 兜底）；
`.env` 已被 .gitignore 忽略，严禁入库。生产用密钥管理覆盖。

> **注意（容器内注入路径）**：api 容器**只拿到 docker-compose.yml `environment:` 白名单里显式列出的键**
> （`.env` 被 `.dockerignore` 排除、不会整文件进容器）。因此新增任何凭据**仅写 `.env` 不够**，
> **必须在 docker-compose.yml 对应服务的 `environment:` 段补一行 `${VAR:-}` 引用**，否则容器内该变量
> 恒为空串、功能静默不生效（`MANAGER_ACCOUNTS` 已按此补齐，仅 api 需要，
> worker/migrate 不鉴权不加）。必填分组：

> **`MANAGER_ACCOUNTS` 形状与恶性配置（2026-09-12 起）**：manager 账号唯一来源是单个 JSON
> 列表变量 `MANAGER_ACCOUNTS`（旧 `MANAGER_ACCOUNT` / `MANAGER_PASSWORD` /
> `MANAGER_ACCOUNT_2` / `MANAGER_PASSWORD_2` 四个编号键已**全部作废**，写了不生效）。
> 示例（**单行、条目内不含多余空格**）：
> `MANAGER_ACCOUNTS=[{"account":"hengfa","password":"<hex>"},{"account":"manager02","password":"<hex>"}]`。
> 加 manager **只改 `.env` 这一行 + `docker compose up -d`**，不用改代码、不用发版。
> 口令建议用 `openssl rand -hex 32` 生成**纯十六进制**，避开 compose 对 `$` 的插值转义问题。
> **恶性配置 = api 起不来**：JSON 语法错误、结构错误、条目键名笔误（如把 `password` 写成
> `pasword`）会让 pydantic 在 `Settings` 实例化时抛 `ValidationError`，**容器直接启动失败**。
> 这是**有意的取舍**——起不来（响亮、被 health check 与日志立刻发现）**好过**静默变成"没有
> manager 可登录"（没人能进、还看不出原因）。**部署前务必先本地校验这段 JSON**（如
> `python -c "import json,os; json.loads(os.environ['MANAGER_ACCOUNTS'])"`）。

| 分组 | 变量与注意 |
|---|---|
| 数据库 | `POSTGRES_PASSWORD`（生产必须覆盖默认演示值）、`POSTGRES_DB` |
| 会话与账号 | `SESSION_SECRET`、`MANAGER_ACCOUNTS`（JSON 列表，唯一来源，可含多个 manager）、`DEVELOPER_ACCOUNT` / `DEVELOPER_PASSWORD` |
| 模型接入 | `MODEL_BASE_URL` / `MODEL_API_KEY` 未配置 → 模型决策 fail closed（对话不可用）；`MODEL_NAME` 默认 deepseek-v4-flash，通常无需改 |
| ERP MCP | `ERP_MCP_URL`、`ERP_MCP_API_KEY`（空 = 依赖 ERP 的工具 fail closed；认证见 mcp-api.md §1） |
| 安全 | `COOKIE_SECURE`（本地 http 演练 false / 生产 https true）、`FRONTEND_ORIGIN`（演练 `http://127.0.0.1:8080` / 生产 https 入口来源）、`SESSION_TTL_MINUTES`（默认 480） |
| 可选 | `WEB_PORT`（默认 8080）、`NGINX_LOGIN_RATE` / `NGINX_LOGIN_BURST`（登录限速）、`ATTACHMENT_DIR` / `EXPORT_DIR`（空串 = 对应功能不启用） |

各分组的典型用途：

① **数据库**：db 容器初始化与编排构造连接串时使用（示例：演练可保持默认库名 `agent`，生产覆盖
`POSTGRES_PASSWORD`——本地演示默认仅在本机回环演练场景可接受）。
② **会话与账号**：决定谁能登录、以什么角色对话（manager 业务 / developer 运维）。`SESSION_SECRET`
建议随机 64 位 hex，可用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成。
`MANAGER_ACCOUNTS` 为 manager 账号的**唯一来源**（JSON 列表，形状与恶性配置见上方引用块与
`.env.example`）：列表里每个条目是一条 manager 凭据，**凭据顺序 = 列表顺序，其后追加 developer
一条**。条目 `account` / `password` 任一为空 = 该条**整条丢弃**（fail closed，不得变成"口令为空即可
登录"）；列表为空 = 无 manager 可登录（`sensitive_settings_missing` 启动告警会列出
`manager_accounts`）；**列表非空但无一条真正生效**（每条都因 `account` / `password` 为空被整条丢弃，
且无 developer 兜底）同样视为未配置，也会出现在 `sensitive_settings_missing` 里（不误报
`sensitive_settings_loaded`）。账号名与其它条目（含列表内 / 与 developer 交叉）**重名时按「首个生效」处理**：
靠前条目生效、重名的后者整条丢弃，且**启动期以 error 日志 `duplicate_credential_accounts_dropped`
报出被丢弃的账号名**（不阻断启动；一次配置笔误不锁死整机，主 manager / developer 除非自身即被丢弃者
否则照常登录；只报账号名、绝不报口令）。被整条丢弃的半配条目另由**启动期 error 日志
`incomplete_credential_entries_dropped`** 登记（error 级、只报账号名（`account` 为空者报 `index=<i>`）、
绝不报口令、不阻断启动）。重名条目下的同一个账号名仍只对应一个角色与一个口令，歧义身份
不得放行。每个账号的会话相互隔离（会话按账号归属，`thread-{account}-{uuid8}`）。
注意：manager 登录后前端仍会展示"共享 manager 账号"提示，语义待后续调整（见 src/administrative_agent/web/routes_auth.py 的 `_SHARED_MANAGER_NOTE`）。
③ **模型接入**：让对话可用。示例（deepseek 兼容服务）：`MODEL_BASE_URL` 填其 OpenAI 兼容端点、
`MODEL_API_KEY` 填密钥；两者任一留空 = 对话按 fail closed 报错。`MODEL_NAME` 默认
deepseek-v4-flash，一般沿用默认即可。
④ **ERP MCP**：让查询与写工具可用。示例：`ERP_MCP_URL=http://<内网主机>:<端口>/mcp`、
`ERP_MCP_API_KEY=<Agent 账号 Key>`；留空 = 依赖 ERP 的工具 fail closed。认证细节与 scope 语义见
mcp-api.md。
⑤ **安全**：http 演练 / https 生产两态切换。演练态 `COOKIE_SECURE=false`；生产翻为 `true` 并同步
`FRONTEND_ORIGIN=https://<入口域名>`（对应[第 6 节](#6-生产上线检查清单)第一组检查项）。注意
`.env.example` 中 `FRONTEND_ORIGIN` 的默认 `http://localhost:5173` 是前端 dev 用途，compose 部署
（演练入口 8080 / 生产 https）必须改为实际入口来源，否则浏览器请求会被 CORS/CSRF 来源校验拒绝。
`COOKIE_SECURE` 另有两处兜底差异：docker-compose.yml 对其默认 `true`（生产安全默认），而
`.env.example` 显式置 `false` 供本地 http 演练——若手写 .env 漏配此项，http 回环演练会因浏览器
拒存 Secure Cookie 而静默登录失败。
⑥ **可选**：`WEB_PORT` 冲突时改宿主映射端口（同时同步 `FRONTEND_ORIGIN` 默认值）；登录限速与
附件/导出开关按站点需要开闭。

关键注意：

- compose 部署时 `AGENT_DATABASE_URL` / `CHECKPOINT_DATABASE_URL` 由编排自动构造指向 db 服务，
  **无需在 .env 填写**；只有裸进程本地开发才手填（见[第 8 节](#8-本地开发)）。
- `ATTACHMENT_DIR` / `EXPORT_DIR` 生产启用需在 compose 中补持久卷挂载并指定目录（编排默认不挂载）。
- `.env.example` 自带分组注释即完整变量字典：「docker compose 部署变量」分组
  （POSTGRES_* / NGINX_* / WEB_PORT）与「上下文与压缩预算」调优项分组（COMPRESSION_TIERS 等，
  默认值即 config.py 的默认，通常无需修改）按需覆盖即可。

## 5. 部署步骤

以下命令可复制直接执行（在仓库根目录；`pnpm-lock.yaml` 随仓库提交，`--frozen-lockfile` 保证按
锁文件安装）：

```bash
# 1. 构建前端（产出 frontend/dist；nginx 卷挂载直接读取——前端有变更时重跑本步即生效）
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..

# 2. 配置 .env（复制后按第 4 节填必填项）
cp .env.example .env

# 3. 构建后端镜像（migrate/api/worker 共用；Dockerfile COPY 层含 README.md 等仓库文件，
#    仓库文件变更后建议重新构建，避免旧镜像层缓存）
docker compose build

# 4. 启动（首次启动 migrate 自动执行 alembic upgrade head；
#    依赖链 db → migrate → api/worker → web，全部 healthy 后即可用）
docker compose up -d
```

验证：

```bash
docker compose ps            # 服务应全部 healthy；migrate 为 exited 0 属正常（一次性任务）
curl http://127.0.0.1:8080/api/v1/health
# 期望输出：{"status":"ok","app":"hengfa-administrative-agent","version":"..."}
```

浏览器打开 `http://127.0.0.1:8080`：用 manager 账号登录，发一条对话消息（如查询一笔订单）确认能收到
回复；再试一个写操作（如记一笔收支），确认出现确认卡、点击确认后落库。

首次部署与日常更新走同一套命令：重复执行 `docker compose up -d` 只重建有变更的服务，已存在的容器与
数据卷不被清空；只有 `docker compose down -v` 才删除卷（数据即丢）。

日志与排障：

- 日志：`docker compose logs -f api`（structlog JSON 行）；其他服务同理（`-f web` / `-f worker` /
  `-f db`）。
- 各服务均有独立 healthcheck：db 走 pg_isready、api 探 `/api/v1/health`、worker 探 `/healthz`
  （18002）、web 用 wget 拉首页。`docker compose ps` 显示 unhealthy 时先看对应服务日志。
- 首页 404 / 白屏：`frontend/dist` 缺失或为空（步骤 1 未执行）——回到步骤 1 构建后再
  `docker compose up -d`。
- migrate 未成功（`docker compose ps` 中 exited 非 0）：`docker compose logs migrate` 查原因，
  修正 `.env` 后 `docker compose run --rm migrate` 重试，再 `docker compose up -d`。
- worker 启动即退出：多为环境变量缺配（worker 启动检查 fail closed，见 worker.py 模块注释），
  对照第 4 节补全后重试。
- 宿主端口被占用：改 `WEB_PORT` 后 `docker compose up -d`（同时同步 `FRONTEND_ORIGIN` 默认值）。
- 对话不可用：先查模型配置（MODEL_* 未配即 fail closed）；ERP 工具报错：先查 ERP_MCP_* 配置与
  账号 scope（见 mcp-api.md §1）。

## 6. 生产上线检查清单

- [ ] 入口交给受管反向代理（https 终结），移除 db / web 的本机回环绑定。
- [ ] `COOKIE_SECURE=true`、`FRONTEND_ORIGIN` 改为 https 入口来源（nginx 模板注释：本层须处于
  https 链路末端，Secure Cookie 才生效）。
- [ ] 密钥管理覆盖 .env：`SESSION_SECRET`、`POSTGRES_PASSWORD`、manager/developer 口令、
  `MODEL_API_KEY`、`ERP_MCP_API_KEY`。
- [ ] ERP MCP 账号 scope 与 mcp-api.md 权限表一致并按最小权限收紧；`finance:create` /
  `finance:update` 当前为占位 scope 名（见 `src/administrative_agent/agent/tools/base.py` 注释），
  须与 ERP 权限表核对。
- [ ] 附件 / 导出如需启用：为 `ATTACHMENT_DIR` / `EXPORT_DIR` 补持久卷挂载。
- [ ] 备份策略：db 卷（`docker volume`）+ 经 127.0.0.1:15433 的 pg_dump 定期落盘；业务表与
  checkpoint 共库，一并覆盖。示例（需本机有 PostgreSQL 客户端，口令经环境变量注入，库名/用户
  按 `.env` 调整）：

```bash
PGPASSWORD=<POSTGRES_PASSWORD> pg_dump -h 127.0.0.1 -p 15433 -U agent -d agent -Fc -f agent_$(date +%F).dump
```

- [ ] 登录限速参数 `NGINX_LOGIN_RATE` / `NGINX_LOGIN_BURST` 按站点情况核对。
- [ ] `LOG_LEVEL`、`CHECKPOINT_RETENTION_DAYS`（默认 30）按运维口径调整。
- [ ] 验收冒烟：完整走一遍「查询 → 写操作确认卡 → 确认落库 → 审计可查」链路；SSE 对话在 https
  长连接下无中断（中间层若对事件流启用压缩/缓冲会破坏 SSE，nginx 模板已关缓冲）。

## 7. 升级与运维

```bash
git pull
# 前端有变更：重建静态产物（nginx 直读新文件，无需重启容器）
cd frontend && pnpm install --frozen-lockfile && pnpm build && cd ..
# 后端/迁移/镜像内文件有变更：重建并滚动更新（迁移由 migrate 服务自动执行）
docker compose build
docker compose up -d
# 仅改了 .env：直接 docker compose up -d（环境变化会自动重建受影响容器）
```

- 日志：`docker compose logs -f <svc>`；健康：`docker compose ps`。
- 停止服务并保留数据卷：`docker compose down`；彻底重置（删除数据库卷，**慎用**）：
  `docker compose down -v`。

**备份与恢复**：备份 = db 卷（`docker volume`）+ 定期 pg_dump（命令示例见
[第 6 节](#6-生产上线检查清单)）。恢复示意（先停写服务避免写入竞争；库名与备份文件名按实际调整，
恢复会覆盖库内现有数据）：

```bash
docker compose stop api worker
docker compose cp ./agent_2026-09-01.dump db:/tmp/agent.dump
docker compose exec db pg_restore -U agent -d agent --clean --if-exists /tmp/agent.dump
docker compose start api worker
```

## 8. 本地开发

裸进程联调（不依赖部署编排；详细前端说明见 frontend/README.md）：

- 演示数据库：`docker compose -f docker-compose.pg.yml up -d`（回环 127.0.0.1:15432，独立于部署
  编排；默认口令 agent_demo_pw，可经 `AGENT_DEMO_DB_PASSWORD` 覆盖）。首次联调前建表：
  `uv run alembic upgrade head`——迁移 URL 只从 `.env` 的 `AGENT_DATABASE_URL` 注入，未配置即报错
  退出。停止并保留数据：`docker compose -f docker-compose.pg.yml down`；连卷重置：追加 `-v`（慎用）。
- 根目录 `.env` 手填 `AGENT_DATABASE_URL` / `CHECKPOINT_DATABASE_URL`（同库即可，格式示例见
  docker-compose.pg.yml 文件头注释）；模型与 ERP key 也在此（缺失即 fail closed）。
- 后端：仓库根 `uv sync`（首次）后运行：

```bash
uv run uvicorn administrative_agent.main:app --host 127.0.0.1 --port 18000
```

- 前端：`cd frontend && pnpm install && pnpm dev` → http://127.0.0.1:5173（strictPort）；vite proxy
  同源转发 `/api` → 127.0.0.1:18000。`VITE_API_BASE_URL` 见 frontend/.env.example：默认同源
  `/api/v1` 无需修改，只有跨域直连才改绝对地址——注意后端是 18000，不是 8000。
- 端口注意：Windows 开发机上 8000 常处于保留端口段（绑定报 winerror 10013），后端固定 18000。
- 测试：后端 `uv run pytest -q`；前端 `cd frontend && pnpm test`；端到端 Playwright（默认探活
  `http://127.0.0.1:18000`）的运行前提见 frontend/e2e/README.md。

## 9. 文档指针

| 文档 | 内容 |
|---|---|
| [mcp-api.md](mcp-api.md) | 真实 ERP MCP 契约：连接认证、工具与参数、字段白名单、错误语义与 fail closed 规则（运行态以此为准） |
| [frontend/README.md](frontend/README.md) | 前端工程：环境、构建、dev / 测试 / e2e 命令、部署边界 |
| [frontend/e2e/README.md](frontend/e2e/README.md) | Playwright 端到端用例的运行前提（后端启动、凭据注入等） |

早期开发规格与 RV 审核记录不再随版本更新，仅 git 历史可见；`docs/` 现存评审记录
（REVIEW-2026-08-28.md、FRONTEND-REVIEW-2026-08-28.md 等）仅供查阅，不构成运行契约。运行契约以
代码、本部署手册与 mcp-api.md 为准。
