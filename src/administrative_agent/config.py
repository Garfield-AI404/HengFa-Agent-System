"""集中式配置：pydantic-settings 读取 15.2 环境变量。

密钥纪律：本模块只读取环境变量（env_file=.env），不硬编码任何密钥。
生产密钥只通过密钥管理注入，绝不提交仓库。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 15.2 列出的敏感变量。代码中统一用 is_configured() 判断，
# 未配置时相关能力必须 fail closed（由后续单元实现）。
# `manager_accounts` 在此列（2026-09-12 由 manager_password 归并而来）：它承载
# manager 口令，属敏感配置；`is_configured` 对其做「至少一条真正生效」判定——
# 空列表 = 未配置，且**列表非空但每条都因 account/password 为空被整条丢弃**时
# 同样视为未配置（该场景现出现在启动日志的 `sensitive_settings_missing` 里，
# 否则会误打印 sensitive_settings_loaded 而实际无人能登录）。相比旧编号键本项
# 覆盖更宽：旧方案只有主 manager 对在 SENSITIVE_SETTINGS 内，第二个 manager 半配
# 完全静默——覆盖面更宽是有意的取舍（半配属配置缺陷，见下）。条目级的丢弃明细另由
# auth/service.dropped_incomplete_accounts + main.lifespan 的
# `incomplete_credential_entries_dropped` 告警给出（同样不阻断启动、只报账号名/索引）。
# 结构性错误（JSON 语法/键名笔误）不在此兜底：由 pydantic 在 Settings 实例化时
# 抛错 → 进程直接起不来（有意取舍，见 README「恶性配置」）。
SENSITIVE_SETTINGS = (
    "model_api_key",
    "erp_mcp_api_key",
    "erp_mcp_jwt",
    "rag_mcp_api_key",
    "agent_database_url",
    "checkpoint_database_url",
    "session_secret",
    "manager_accounts",
    "developer_password",
)


class ManagerAccount(BaseModel):
    """单条 manager 凭据（账号 + 口令），来自 MANAGER_ACCOUNTS JSON 列表。

    ``extra="forbid"``：条目键名笔误（如把 ``password`` 写成 ``pasword``）
    **必须炸出来**，不得被静默忽略成一个"空口令"条目——那是本次改造要消灭的
    失败模式。JSON 语法/结构/键名错误统一由 pydantic 在 ``Settings`` 实例化时
    抛 ``ValidationError``，应用直接启动失败（响亮），好过静默变成"没有 manager
    可登录"。

    ``password`` 用 ``Field(repr=False)``：pydantic 默认 repr 会打印全部字段，
    同 ``auth.service._Credential`` 口径固化为口令不进 repr（纵深防御——日志/
    调试/异常拼接即使打印整个条目也不带出密钥值）。
    """

    model_config = ConfigDict(extra="forbid")

    account: str
    password: str = Field(repr=False)


class Settings(BaseSettings):
    """规格 15.2 后端环境变量全集。

    非敏感变量提供开发默认值；敏感变量默认空串，必须由环境注入。
    VITE_API_BASE_URL 属前端变量（15.2），不在此处读取。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 模型接入（规格 0.3：LangChain 通用 ChatModel 接口，配置化接入）
    model_name: str = "deepseek-v4-flash"
    model_base_url: str = ""
    model_api_key: str = ""
    # 思考档位：low/medium/high=调强度（deepseek-v4-flash 实测支持）；
    # off=显式关闭 thinking（2026-08-28 实测 `thinking: {"type": "disabled"}`
    # 生效——SSE 全程 0 个 reasoning delta chunks，关闭后无
    # reasoning_content，根除多轮对话 thinking 模式 400 风险）；
    # 空串=不传该参数保持 API 默认（deepseek-v4-flash 默认开启思考）。
    # 改动后重启后端生效。
    model_reasoning_effort: Literal["low", "medium", "high", "off", ""] = "medium"
    # 模型调用瞬时故障重试次数上限（连接失败/HTTP 429/5xx；0=关闭重试保持
    # fail closed；正文已部分输出或解析失败不重试；2026-08-28 项目负责人批准）
    model_call_max_retries: int = 2

    # ERP MCP（规格 0.3：MCP Streamable HTTP；mcp-api.md §1 认证）
    # 一期只用 X-API-Key（15.2 ERP_MCP_API_KEY）；Bearer <JWT> 回退通道预留，
    # 两者都未配置时 MCP 调用 fail closed，禁止发出请求。
    erp_mcp_url: str = ""
    erp_mcp_api_key: str = ""
    erp_mcp_jwt: str = ""
    # 每次 MCP 调用的连接、读取、总超时（秒，规格 13.3：连接/读取/总超时均配置化）
    erp_mcp_connect_timeout: float = 5.0
    erp_mcp_read_timeout: float = 30.0
    erp_mcp_total_timeout: float = 60.0
    # 瞬时故障重试次数上限（404/5xx/连接失败；0=关闭重试保持 fail closed；
    # 2026-08-27 项目负责人批准：服务重启窗口内自动重试，最多 3 次）
    erp_mcp_max_retries: int = 3
    # list_entities 元数据缓存有效期（秒）；过期后下一次访问时刷新（规格 2.3）
    erp_mcp_metadata_ttl: int = 3600

    # RAG 长期记忆 MCP（同级仓库 hengfa-rag；MCP Streamable HTTP，POST /mcp，
    # X-API-Key 认证）。它与 ERP MCP 是两个独立服务、独立客户端实例。
    # 未配置 rag_mcp_url 或 rag_mcp_api_key（任一为空，含半配置）→ **不向模型
    # 暴露** RAG 侧全部工具（rag_remember/rag_recall/rag_forget/
    # rag_search_documents；见 tools/rag_memory.py 的 RAG_TOOL_NAMES，否则模型
    # 会反复调用一个必然失败的工具、白耗轮次），
    # 并在启动期打一条 rag_mcp_unconfigured 告警；agent 其余能力照常工作。
    # rag_mcp_api_key 属敏感凭据：登记进 SENSITIVE_SETTINGS，只经环境注入。
    # [2026-09-22 G1] 超时/重试**独立于** ERP MCP：RAG 每次检索/写入都含一次
    # 云端嵌入 API 调用，比 ERP 本地查询慢，复用 erp_mcp_* 会把 RAG 的云端
    # 抖动误判为 ERP 超时口径。ERP 的 erp_mcp_* 旋钮行为逐字节不变。
    # [2026-09-23 按实测重定] 本机真调 DashScope 原生多模态端点
    # （qwen3-vl-embedding，86 次）实测：正常 p50 0.14–0.22s、p90≈0.75s、
    # max≈0.85s；但有 **5/86≈5.8% 的新建连接长尾落在 21.2–21.3s**（HTTP 200
    # 成功，只是极慢）。RAG 侧嵌入客户端自身 httpx 超时是 30s
    # （hengfa-rag 的 embedding.DEFAULT_TIMEOUT_S），故这类长尾在 RAG 侧**会被
    # 容忍并成功返回**。此前配 connect=3/read=15/total=20，按收敛公式生效
    # read=min(15,(20-6)/2)=7s —— **调用方 7s 就放弃、被调用方要 21s 才成功**，
    # 顺序是反的；且读超时映射为 MCP_TIMEOUT_RESULT_UNKNOWN（不可重试），
    # 用户表现为「记忆时灵时不灵、且不自愈」。
    # 故 read 必须 > 实测合法长尾 21.3s 并取到 RAG 每尝试预算 30s：**让被调用方
    # 先返回结构化错误（带 code/retryable，可判定），而不是调用方先超时
    # （结果未知，不可判定）**。新值 connect=3 / read=30 / total=66：
    # 生效 connect=min(3, 66/4)=3.0；read=min(30,(66-6)/2)=30.0（两项均为生效值）。
    # 最坏墙钟 ≈ 2×(2×3 + 2×30) = 132s（单次尝试 2×connect+2×read；
    # max_retries=1 即 2 次尝试，另加一次 1s 退避）。
    # 生效的 connect/read 由 mcp/client._build_rag_timeout 按 total 收敛
    # （connect≤total/4；read≤(total-2×connect)/2）。
    # [2026-09-23 P3 补句 1/2] 实话：read=30 只覆盖**实测到的那条 21.2–21.3s
    # 建连长尾**（21.3 < 30，方向正确），**不是**「被调用方一定会先返回结构化
    # 错误」的保证。上游一旦进入「慢 + 重试风暴」形态，**调用方仍会先超时**，
    # 拿到 MCP_TIMEOUT_RESULT_UNKNOWN（不可重试、无 reason）；**把 read 抬到
    # 40–45s 也改变不了这一点**——因为 RAG 的嵌入客户端自己会重试（hengfa-rag
    # 的 embedding.py ``_embed_batch`` 捕获 httpx.TimeoutException、MAX_RETRIES=3、
    # 退避 1/2/4），慢形态下它要 **≥127s** 才返回 embedding_unavailable（4×30+7
    # 是下界——httpx 按操作计时，一次 post 可吃满 connect 30 + read 30 = 60s，
    # 最坏 4×60+7 = 247s；RAG MCP 层无外层超时，调用方是唯一封顶者）；要覆盖它
    # 得把 read 抬到 ~250s 量级，更不值得。
    # [2026-09-23 P3 补句 2/2] F2 的 retryable 重试放行只在**「快速失败」形态**
    # 下可达（429/5xx 立刻返回 → RAG 退避几秒就给结构化错误），**别把它当成
    # 「RAG 抖动会自愈」的保证**。
    rag_mcp_url: str = ""
    rag_mcp_api_key: str = ""
    rag_mcp_connect_timeout: float = 3.0
    rag_mcp_read_timeout: float = 30.0
    rag_mcp_total_timeout: float = 66.0
    # RAG 瞬时故障重试次数上限（F2 修好后 embedding_unavailable /
    # store_unavailable 会按服务端声明的 retryable 自动重试；0=不重试）。
    rag_mcp_max_retries: int = 1

    # Agent 自有数据库（规格 12 章）
    agent_database_url: str = ""
    checkpoint_database_url: str = ""

    # 会话与安全（规格 12.4：凭据只进 .env；12.1 无用户表，账号密码来自环境）
    session_secret: str = ""
    # manager 账号：list 环境变量需 JSON 格式（同 compression_tiers），
    # 单行无空格，如 MANAGER_ACCOUNTS=[{"account":"a","password":"<hex>"},...]。
    # 唯一来源：加 manager 只改 .env + docker compose up -d，无需改代码/发版
    # （替旧 MANAGER_ACCOUNT / MANAGER_PASSWORD / MANAGER_ACCOUNT_2 /
    # MANAGER_PASSWORD_2 四个编号键，旧键已作废、写新代码不再读取）。
    # 凭据顺序 = 列表顺序，其后追加 developer；重名按「首个生效、后者丢弃」
    # （见 auth/service.py）。条目 account/password 任一为空 = 整条丢弃（fail
    # closed，不得变成"口令为空即可登录"）。列表允许为空 = 无 manager 可登录。
    # 字段 repr=False：整个凭据列表不进 Settings.repr（口令不明文外泄）。
    # 不提供开发默认值（默认空列表 = 未配置，由 SENSITIVE_SETTINGS 告警）。
    # NoDecode：关闭 pydantic-settings 对复杂字段的默认 json.loads（它遇到空串会
    # 直接抛 SettingsError），改由下方 _parse_manager_accounts 统一解析——空串/未设
    # = []（默认部署 docker-compose 传 `${MANAGER_ACCOUNTS:-}` 即空串，必须容忍，
    # 否则默认部署起不来）；非法 JSON/键名笔误仍抛 ValidationError（起不来）。
    manager_accounts: Annotated[list[ManagerAccount], NoDecode] = Field(
        default_factory=list, repr=False
    )
    developer_account: str = ""
    developer_password: str = ""
    # 会话有效期（分钟）；签名 Cookie 过期即失效
    session_ttl_minutes: int = 480
    # 本地 http 联调置 False；生产必须 True（2.7.1 浏览器只用 HttpOnly+Secure+SameSite Cookie）
    cookie_secure: bool = False
    # SameSite 属性：本地跨 host 联调设 "none"（需配合 cookie_secure=true）；
    # 生产同域部署保持默认 "lax" 更安全（2.7.1）
    cookie_samesite: str = "lax"

    # 可观测性（规格 0.3：structlog JSON）
    log_level: str = "INFO"

    # 数据保留（规格 12.3）
    checkpoint_retention_days: int = 30

    # 优雅停机（规格 15.3 步骤 7）：等待进行中写调用落审计的超时秒数
    shutdown_drain_timeout: float = 30.0

    # 独立前端来源白名单（规格 15.2 FRONTEND_ORIGIN；多来源用英文逗号分隔，
    # 本机联调 .env 设 http://127.0.0.1:5173）
    frontend_origin: str = "http://localhost:5173"

    @field_validator("manager_accounts", mode="before")
    @classmethod
    def _parse_manager_accounts(cls, value: object) -> object:
        """MANAGER_ACCOUNTS 入口解析（配合 NoDecode，见字段注释）。

        - 未设 / 空串 / 纯空白 → ``[]``（未配置，非错）；
        - 字符串 → JSON 解析；语法错误由 pydantic 包成 ``ValidationError``；
        - 已是列表（init 直接传 / 测试 monkeypatch）→ 原样交给条目模型校验。

        条目校验用 ``ManagerAccount(extra="forbid")``：键名笔误在此时抛
        ``ValidationError``，应用直接启动失败（响亮），绝不静默吞成空口令条目。
        """
        if value is None:
            return []
        if isinstance(value, str):
            if not value.strip():
                return []
            return json.loads(value)
        return value

    @property
    def frontend_origins(self) -> list[str]:
        """白名单来源列表（CSRF Origin 校验与 CORS 共用）。"""
        return [o.strip() for o in self.frontend_origin.split(",") if o.strip()]

    def is_configured(self, name: str) -> bool:
        """敏感变量是否已由环境注入（未配置时必须 fail closed）。

        ``manager_accounts`` 例外：非空列表 ≠ 可用——条目 account/password 任一
        为空会被 ``auth.service`` 整条丢弃，故按「至少一条真正生效」判定，避免
        "全部半配"被误报为已配置（否则启动日志会打印 sensitive_settings_loaded
        而实际一条 manager 凭据都不生效）。空列表仍为未配置。
        """
        if name not in SENSITIVE_SETTINGS:
            raise ValueError(f"{name} 不是敏感配置项，无需检查")
        if name == "manager_accounts":
            return any(m.account and m.password for m in self.manager_accounts)
        return bool(getattr(self, name))

    # 会话记忆压缩（对话历史压缩机制，2026-08-24 定稿；list 环境变量需 JSON 格式）
    compression_tiers: list[int] = [30, 60, 90]      # 触发档位（上下文占用百分比，升序；90 档豁免配额=强制）
    max_compressions_per_tier: int = 2               # 每档触发次数上限
    dialogue_budget_chars: int = 140000              # 对话预算（上下文占用分母；原 MAX_MODEL_INPUT_CHARS 值）
    summary_max_chars: int = 1500                    # 单摘要上限（字符）
    summary_total_budget: float = 0.10               # 总摘要占对话预算比例（超限丢最老摘要）
    compression_output_max_tokens: int = 2000        # 压缩调用输出上限（max_tokens）
    lookup_result_chars: int = 500                   # memory_lookup 返回截断（字符）
    memory_recent_messages: int = 5                  # 压缩后保留的最近消息条数（未压缩尾段）

    # 对话对账附件与报告保留（2026-08-31 批次 1：对话上传 Excel 对账）
    attachment_dir: str = ""  # 附件临时目录；空串 = 不启用附件功能
    attachment_max_bytes: int = 10 * 1024 * 1024  # 附件大小上限 10MB
    attachment_ttl_seconds: int = 3600  # 附件保留 1h（启动时 + 每次上传时清过期）
    reconcile_report_ttl_seconds: int = 86400  # 对账报告保留 24h（跨轮确认窗口）

    # 对话导出（2026-09-01 导出功能批次：Excel 文件临时目录 + 上限 + TTL）
    export_dir: str = ""  # 导出文件目录；空串 = 不启用导出功能
    export_max_rows: int = 50000  # 单次导出行数上限（超限 fail closed，提示加过滤条件）
    export_file_ttl_seconds: int = 3600  # 导出文件保留 1h（启动时 + 每次导出时清过期）

    # SSE 事件流结构性元数据落库（2026-09-19 可观测性第 0 步）
    stream_event_ttl_seconds: int = 14 * 24 * 3600  # 保留 14 天（启动 + 周期分批清理）
    stream_event_queue_capacity: int = 10000  # 有界队列容量（满则丢最老）
    stream_event_cleanup_interval_seconds: int = 3600  # 周期清理间隔（复用写线程）
    stream_event_cleanup_batch_size: int = 5000  # 单批 DELETE 行数（避免长事务锁表）
    stream_event_drain_timeout: float = 5.0  # 停机写尽事件流 sink 的超时（秒）


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
