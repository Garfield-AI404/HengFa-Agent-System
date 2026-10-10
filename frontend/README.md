# 恒发 Agent 一期 · 前端工程

独立 Node 前端工程（React + TypeScript + Vite + pnpm），不依赖 Python 环境。

## 环境要求

- Node.js >= 22（当前验证环境 Node v24.16.0，兼容已接受）
- pnpm >= 11（当前验证环境 pnpm 11.10.0）

## 安装

```bash
pnpm install
```

锁文件 `pnpm-lock.yaml` 随仓库提交；CI 或复现环境使用：

```bash
pnpm install --frozen-lockfile
```

## 启动开发服务器

```bash
pnpm dev
```

Vite dev server 默认运行于 `http://127.0.0.1:5173`（strictPort）。后端 API 地址通过
`.env`（模板见 `.env.example`）中的 `VITE_API_BASE_URL` 配置，默认
`http://127.0.0.1:8000/api/v1`。

## 常用命令

| 命令 | 说明 |
|---|---|
| `pnpm dev` | 启动 Vite 开发服务器 |
| `pnpm build` | TypeScript 检查并生成 `dist/` |
| `pnpm preview` | 本地预览 `dist/` 构建产物 |
| `pnpm lint` | ESLint 检查 |
| `pnpm typecheck` | TypeScript 全量类型检查（app + node 配置） |
| `pnpm test` | Vitest 单元测试 |
| `pnpm test:e2e` | Playwright 端到端测试（首次先 `pnpm exec playwright install chromium`） |
| `pnpm api:types` | 从后端 OpenAPI 生成 `src/api/types.ts`（见 `scripts/generate-api-types.mjs`） |

## 测试

单元测试（Vitest + Testing Library）：`pnpm test`

端到端测试（Playwright）：用例在 `e2e/`，运行前提（后端启动、凭据环境变量注入、
本机无 Chromium 时改用 Edge 的 local config）见 [`e2e/README.md`](./e2e/README.md)。

```bash
pnpm exec playwright install chromium
pnpm test:e2e
```

## 构建与部署边界（规格 2.7.1）

- 生产构建为静态 `dist/`，由独立静态服务器或反向代理托管，不由 Jinja2 渲染。
- FastAPI 只提供 `/api/v1`、文件上传、SSE 和健康检查，不提供 HTML 模板。
- 浏览器只使用 HttpOnly、Secure、SameSite 会话 Cookie；认证令牌不放入 localStorage。
- 前端不持有 ERP MCP 凭据、模型 API Key 或数据库连接信息。
