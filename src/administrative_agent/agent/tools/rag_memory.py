"""[2026-09-22 RAG 长期记忆接入] 三个长期记忆工具：rag_remember /
rag_recall / rag_forget。

[2026-10 文档检索接入] 第 4 个工具 ``rag_search_documents``（对接 RAG 侧
``search_documents``）——检索**公司共享知识库**（``documents``/``chunks``）
切分出的片段。它与记忆三工具**同属 RAG 侧工具**（同一 ``ctx.rag_client``、
同一调用/收口/审计机制、RAG 未配置时一并剔除），但语义分属**两条线**：
记忆三工具操作 ``memories`` 一张表、有账号（subject）归属；文档线是共享
知识库、**无账号维度**（故本工具不注入 subject，RAG 侧也不收）。命名刻意
用 ``search_documents`` 而非 ``search``，与 ``recall``（召回记忆）在名字上
就分得清。**纯读**（is_write=False，无确认卡、不建任务）；成功后补一条
agent 侧审计（EVENT_RAG_SEARCH，``AuditService.record_rag_search_documents``），
与 ``rag_recall`` 的 EVENT_RAG_RECALL 同款，便于事后分辨「文档是被检索到
但没引用，还是压根没调」。

---

对接同级仓库 ``hengfa-rag`` 的长期记忆服务（独立 Git 仓库、独立 PG 容器、
独立进程；MCP Streamable HTTP，``POST /mcp``，X-API-Key 认证）。三个工具
经 ``ctx.rag_client``（第二个 MCP 客户端实例，复用 mcp/client.py 的协议实现）
调用，**不属于 ERP 写入语义**——不建任务、不走确认卡、不进 _WRITE_TOOLS
（与既有只会话记忆工具 ``memory_lookup`` 同款：is_write=False）。

## 审计（F1，2026-09-22）

``rag_remember`` 跨系统新增记忆、``rag_forget`` 软删——**二者改变存储状态**，
与纯读的 ``memory_lookup`` 不同类。用户拍板：**不上确认卡**
（否则"记住我的偏好"会被反复打断），但成功调用后补一条 agent 侧审计事件
（``AuditService.record_rag_memory`` → ``audit_events`` 的 EVENT_RAG_MEMORY），
回答「谁 / 何时 / 对哪条记忆 / 做了什么操作」。落点与 ``export_data`` 的
record_export 同层（工具 run 内调 ``ctx.audit``）；**不经** task/attempt
路径，两段式写入/payload hash 链路不动。审计失败按 10.4 转错误结果（不假报
成功），错误文本带上已生效的 memory_id 供人工复核。

``rag_recall`` 是**纯读**（不改变存储状态），与上面二者语义不同类，故走**独立
事件类型** EVENT_RAG_RECALL（``AuditService.record_rag_recall``，2026-09-23）：
此前纯读召回无任何痕迹，「这一轮为什么这么答」在 agent 侧断链（记忆是被召回但
没记、还是压根没调，事后分不出）。**不落 query 正文**（只落 ``query_chars``，
query 是用户会话内容）。**失败语义刻意不同**：召回无副作用可回滚、结果已在手，
故审计失败 ``logger.error`` 后**照常返回 hits**，不让读工具被审计拖挂——与写
路径的 10.4 不冲突（后者前提是"有状态改变"）。详见 ``_record_recall_audit``。

## 账号（subject）注入——本模块的核心纪律

账号**只能**经请求级 ``ctx.context.user_id`` 在**本包装层**注入；绝不写入
任何提示词文本、绝不进 LangGraph State（依据 ``agent/state.py``：身份与权限
不进入 State，模型无法经 State 读取或伪造身份）。模型可能幻觉出 ``subject``
——本层**一律不信任**模型给的 subject，按工具与路径重算（见 ``_subject_for_*``）：

- **remember**：``kind=preference`` → 注入 ``subject=ctx.context.user_id``；
  ``fact``/``episode`` → ``subject=None``（**必须剥掉**模型可能给的 subject，
  RAG 侧对非 preference 给 subject 会直接报错，不静默忽略）；
- **recall**：**无条件**注入 ``subject=ctx.context.user_id``。不注入则 RAG
  只召回全局记忆，账号级偏好**永远召不回**（最容易漏的一条）；
- **forget**：``query`` 批量路径 → 注入 ``subject=ctx.context.user_id``（把
  批量软删收紧在本账号内，避免误删其它账号；RAG 侧不给 subject 时只作用于
  全局）。``memory_id`` 路径 → ``subject=None``：读 ``hengfa-rag`` 的
  ``memory/service.forget``——该路径对给定 subject 按**字面等值**过滤，注入
  user_id 会把「忘掉一条全局记忆」变成 ``subject='global' AND id=N`` 命中 0 行
  → ``affected=0``（非错误）被模型读成"已忘掉"，而那条记忆其实仍可召回
  （静默失效）。故该路径**不注入**。两条路径的差异是有意的。

## 未配置时不暴露

装配层（``main._wire_rag_client`` + ``web/routes_chat._harness_runtime``）在
``rag_mcp_url`` **或** ``rag_mcp_api_key`` 未配置时（F4：半配置也按未配置处理）
**不把这三个工具注入图运行时**（``without_unconfigured_rag_tools``）——模型看
不到它们，会自然改用别的手段；看不到就不必反复调用一个必然失败的工具、
白耗轮次。
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary

logger: structlog.stdlib.BoundLogger = structlog.get_logger("agent.tools.rag_memory")

#: RAG 侧工具名（装配层据此在未配置时从运行时工具集里剔除）。
#: [2026-10 文档检索接入] 加入第 4 个 ``rag_search_documents``（对接 RAG 侧
#: ``search_documents``）——与记忆三工具同属「RAG 未配置即不可用」，必须一并
#: 列入，否则 RAG 未配时这个工具会裸露给模型（一个必然失败的工具，白耗轮次）。
RAG_TOOL_NAMES: frozenset[str] = frozenset(
    {"rag_remember", "rag_recall", "rag_forget", "rag_search_documents"}
)

#: RAG 工具结果进对话历史前的**显式字符预算**（信封口径，见 ``_bounded_result``）。
#: RAG 结果**没有 items 键**（recall 是 {hits, hit_count}、remember 是
#: {memory_id, action, similar}），故走 ``tools_node.summarize_tool_result`` 的
#: 非明细分支——超限会被**裸字符串从中间腰斩**成非法 JSON（模型读到断开的
#: JSON、命中项静默丢失，无错误/提示/回查路径）。规模可达：top_k 上限 20 ×
#: 每条 content 上限 1000 字（RAG 侧 CONTENT_MAX_CHARS）≈ 20KB。
#: 本预算把信封收到 ``tools_node.TOOL_RESULT_HISTORY_CHARS``（4096）以下，使
#: 该兜底**永不触发**——与 ``attachment_preview.MAX_PREVIEW_CHARS`` 同款
#: （同样是「工具包装层收口 < 4096」的先例）。3900 相对 4096 留 196 字符。
RAG_RESULT_CHARS: int = 3900
#: 消费点信封 ``{"ok": <bool>, **summary}`` 相对裸 summary 多出的字符预留
#: （``{"ok": true, `` 替换 ``{`` 为 +11、``false`` 再 +1）；收口按**信封**
#: 尺寸计算，而非仅 summary。
_ENVELOPE_RESERVE: int = 16
#: 单条命中 content 的截断长度（字符）。逐条先截到此长度再加截断标记，是为了
#: 在总预算内**尽量保留更多命中条目**（而非只留少数几条完整内容）。
RAG_ITEM_CONTENT_CHARS: int = 500
#: 逐条截断标记（与 ``memory_lookup`` 段号路径同款措辞）。
_TRUNCATION_MARK: str = "…（截断）"
#: 最终兜底提示（结果含列表键、连列表条目都放不下时；理论不可达，见
#: ``_bounded_result``）。这里给 top_k 建议是因为该分支只可能由「命中条目
#: 过多」撑破——有列表键、可逐条丢，模型据 ``hit_count`` 调小 top_k 重试。
_FINAL_NOTE: str = "长期记忆结果超出字符上限，已截断；可调小 top_k 重试"
#: 无列表键结果的兜底提示（``list_keys=()``：如 RAG 调用失败 / 前置守卫失败，
#: 由 ``_call_rag`` / ``_require_rag`` 产出）。这类结果**没有可逐条丢的条目**，
#: 超预算只可能是错误文本本身过长——对一次**失败**给「调小 top_k 重试」方向
#: 错误（该建议只对命中过多的查询成立），故给一句独立文案，别把查询建议套到
#: 错误上。
_FINAL_NOTE_NO_LIST: str = "长期记忆服务返回的错误信息过长，已省略；详情见服务端日志"
#: 审计失败结果里**异常文本**的截断上限（字符）。审计库写入失败时底层
#: SQLAlchemy/DBAPI 异常默认带完整 ``[SQL: INSERT ...][parameters: ...]``
#: （数百到上千字符是常态）；不封顶会让异常文本独自撑破信封预算、把
#: 「记忆已生效但审计缺失、需人工复核」这句**不可回滚的告警**挤掉（最坏
#: 结果）。截断后异常文本 ~250 字符，告警正文仍完整保留。
_AUDIT_ERROR_CHARS: int = 250
#: 各工具结果里承载「条目列表」的键名（收口据此逐条裁剪 / 按预算丢弃尾部条目）。
#: 键名用 **MCP 工具名**（不带 ``rag_`` 前缀），与 ``_call_rag(ctx, "…")` 一致。
_RAG_LIST_KEYS: dict[str, tuple[str, ...]] = {
    "recall": ("hits",),
    "remember": ("similar",),
    "forget": ("memory_ids",),
    "search_documents": ("hits",),
}

_KIND = Literal["fact", "preference", "episode"]


class RagRememberArgs(BaseModel):
    """rag_remember 参数：content（记忆原文，1..1000 字，超长报错不截断）、
    kind（fact 事实 / preference 偏好 / episode 经历）、source_note（可选来源
    说明，只记账不过滤）。subject 由系统按当前登录账号自动注入，模型无需填写
    （填写也会被忽略）。"""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(..., min_length=1, max_length=1000,
                         description="记忆原文（1..1000 字；超长报错、不截断）")
    kind: _KIND = Field(..., description="记忆类别：fact 事实 / preference 偏好 / episode 经历")
    source_note: str | None = Field(None, description="来源自由说明（可选，只记账不作过滤）")
    subject: str | None = Field(
        None,
        description="（系统管理）归属账号由系统按当前登录账号注入，模型不要填写；填写会被忽略",
    )


class RagRecallArgs(BaseModel):
    """rag_recall 参数：query（查询文本）、top_k（返回条数，1..20，缺省 5）、
    kind（可选限定类别）、since（可选，只召回该时刻之后创建的记忆，ISO 8601；
    不带时区按北京时间 +08:00 解释）、min_score（可选相似度下限）。subject 由
    系统按当前登录账号注入（只召回本账号偏好 + 全局记忆），模型不要填写。"""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(..., min_length=1, description="查询文本")
    top_k: int | None = Field(None, ge=1, le=20,
                              description="返回条数（1..20，缺省 5）")
    kind: _KIND | None = Field(None, description="只召回某一类（缺省不限）")
    since: str | None = Field(
        None,
        description="只召回该时刻之后创建的记忆（ISO 8601，如 2026-09-01；不带时区按北京时间）",
    )
    min_score: float | None = Field(None, description="相似度下限（缺省用服务端配置）")
    subject: str | None = Field(
        None,
        description="（系统管理）归属账号由系统按当前登录账号注入，模型不要填写；填写会被忽略",
    )


class RagForgetArgs(BaseModel):
    """rag_forget 参数：memory_id 与 query **必须二选一**——memory_id 忘掉
    指定的一条（按 id 精确定位）；query 按内容子串批量遗忘（大小写不敏感）。
    subject 由系统按当前登录账号注入（按 query 批量遗忘时收紧在本账号内），
    模型不要填写。"""

    model_config = ConfigDict(extra="ignore")

    memory_id: int | None = Field(None, ge=1, description="要忘掉的那一条记忆 id（与 query 二选一）")
    query: str | None = Field(None, min_length=1,
                              description="按内容子串批量遗忘（与 memory_id 二选一）")
    subject: str | None = Field(
        None,
        description="（系统管理）归属账号由系统注入，模型不要填写；填写会被忽略",
    )


class RagSearchDocumentsArgs(BaseModel):
    """rag_search_documents 参数：query（查询文本，1..500 字，空/超长报错、
    不截断）、top_k（返回条数，1..20，缺省 5，越界报错、不夹取）。

    文档线是**公司共享知识库**（已入库文档切分出的片段），**没有账号归属维度**
    ——与记忆线的 recall 不同，故本参数模型**不带 subject**（RAG 侧
    ``search_documents`` 也刻意不收 subject；别在此发明租户模型）。
    """

    model_config = ConfigDict(extra="ignore")

    query: str = Field(..., min_length=1, max_length=500,
                       description="查询文本（1..500 字；空/超长报错、不截断）")
    top_k: int | None = Field(None, ge=1, le=20,
                              description="返回条数（1..20，缺省 5）")


def without_unconfigured_rag_tools(
    tools: dict[str, Any], rag_client: Any | None
) -> dict[str, Any]:
    """未配置 RAG（``rag_client is None``）时从运行时工具集剔除三个长期记忆工具。

    理由（见模块 docstring）：模型看到一个必然失败的工具会反复调它、白耗
    轮次；看不到它会自然改用别的手段。已配置时原样返回（同一对象，零拷贝）。
    """
    if rag_client is not None:
        return tools
    return {name: spec for name, spec in tools.items() if name not in RAG_TOOL_NAMES}


def _require_rag(ctx: ToolContext) -> ToolResultSummary | None:
    """前置：RAG 客户端与请求身份必须齐备（缺任一 fail closed，不静默）。"""
    if ctx.rag_client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "长期记忆服务未接线（RAG MCP 未配置），无法执行"},
        )
    if ctx.context is None or not ctx.context.user_id:
        return ToolResultSummary(
            ok=False,
            summary={"error": "缺少请求身份（user_id），长期记忆操作 fail closed"},
        )
    return None


def _subject_for_remember(kind: str, user_id: str) -> str | None:
    """remember 的 subject：preference→账号；fact/episode→None（剥掉模型给的）。"""
    return user_id if kind == "preference" else None


def _envelope(summary: dict[str, Any], *, ok: bool) -> str:
    """真实消费点信封字符串（``tools_node`` 的 ``{"ok": ok, **summary}``）。"""
    return json.dumps({"ok": ok, **summary}, ensure_ascii=False)


def _bounded_result(data: dict[str, Any], *, ok: bool, list_keys: tuple[str, ...]) -> dict[str, Any]:
    """RAG 工具结果尺寸收口（进入对话历史前的**显式预算**，P1）。

    在**工具包装层**把结果收口到信封 ``{"ok": <bool>, **summary}`` ≤
    ``RAG_RESULT_CHARS``（< ``tools_node.TOOL_RESULT_HISTORY_CHARS`` 4096），
    使该 4096 非 JSON 兜底**永不触发**——否则 RAG 结果（无 items 键、走
    ``summarize_tool_result`` 非明细分支）会被裸字符串从中间腰斩成非法 JSON。
    与 ``attachment_preview._fit_summary`` 同款思路，只是 RAG 结果更简单。

    裁剪顺序（尽量保留对模型有用的命中条目）：
    1. 逐条把列表元素 ``content`` 截到 ``RAG_ITEM_CONTENT_CHARS`` 并加
       「…（截断）」标记；
    2. 仍超预算 → 从列表尾部丢弃条目（``hit_count`` / ``affected`` 等顶层计数
       字段保持服务端原值，模型据此知道总命中数/总删数，可调小 top_k 重试）；
    3. 仍超预算（非列表字段异常撑破，理论不可达）→ 只留 ``note`` 提示
       （有列表键用 ``_FINAL_NOTE``；无列表键的错误结果用
       ``_FINAL_NOTE_NO_LIST``，不给查询类建议）。
    任一裁剪置 ``truncated: true``（模型据此可判定「有没有被截断」）。
    """
    cap = RAG_RESULT_CHARS - _ENVELOPE_RESERVE
    out = dict(data)
    truncated = False

    for key in list_keys:
        items = out.get(key)
        if not isinstance(items, list):
            continue
        clipped: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str) and len(content) > RAG_ITEM_CONTENT_CHARS:
                    item = {**item, "content": content[:RAG_ITEM_CONTENT_CHARS] + _TRUNCATION_MARK}
                    truncated = True
            clipped.append(item)
        out[key] = clipped

    while len(_envelope(out, ok=ok)) > cap and any(
        isinstance(out.get(k), list) and out[k] for k in list_keys
    ):
        longest = max(list_keys, key=lambda k: len(out.get(k) or []))
        out[longest] = out[longest][:-1]
        truncated = True

    if len(_envelope(out, ok=ok)) > cap:
        out = {"note": _FINAL_NOTE if list_keys else _FINAL_NOTE_NO_LIST}
        truncated = True

    if truncated:
        out["truncated"] = True
    return out


def _call_rag(ctx: ToolContext, tool_name: str, payload: dict[str, Any]) -> ToolResultSummary:
    """调用 RAG MCP 工具并把结果映射为 ToolResultSummary（失败不静默）。

    RAG 侧工具失败以 ``CallToolResult(isError=True)`` 返回（HTTP 200），
    ``details.reason`` 是机器可读细分原因；本层把它拼进错误文本，便于模型
    与用户看到具体原因（如 subject_required / content_too_long）。

    成功结果经 ``_bounded_result`` 尺寸收口（P1）：RAG 结果无 items 键、走
    ``summarize_tool_result`` 非明细分支，若不收口会被 4096 兜底腰斩成非法
    JSON。错误结果同样收口（``list_keys=()``，仅超大 error 文本会触发）。
    """
    result = ctx.rag_client.call_tool(tool_name, payload)
    list_keys = _RAG_LIST_KEYS.get(tool_name, ())
    if not result.ok:
        error = result.error
        message = getattr(error, "message", None) or "长期记忆服务调用失败"
        details = getattr(error, "details", None) or {}
        reason = ""
        if isinstance(details, dict) and details.get("reason"):
            reason = f"（{details['reason']}）"
        return ToolResultSummary(
            ok=False,
            summary=_bounded_result(
                {"error": f"长期记忆服务调用失败：{message}{reason}"},
                ok=False,
                list_keys=(),
            ),
        )
    data = result.data if isinstance(result.data, dict) else {}
    return ToolResultSummary(
        ok=True, summary=_bounded_result(data, ok=True, list_keys=list_keys)
    )


def _record_memory_audit(
    ctx: ToolContext,
    *,
    operation: str,
    data: dict[str, Any],
    subject: str | None,
    kind: str | None,
) -> ToolResultSummary | None:
    """成功增删长期记忆后补一条 agent 侧审计（F1）。

    落点选择见模块 docstring：与 export_data 的 record_export 同层——**工具
    run 内**调用 ``ctx.audit``（AuditService），把「谁/对哪条记忆/做了什么」
    写进 audit_events（append-only）。**不经** task/attempt 路径（RAG 记忆不是
    ERP 写语义，三个工具仍 is_write=False、prepare=None，确认卡与 payload hash
    链路不动）。审计服务未接线时只告警跳过（生产 harness 恒接线 audit；单元
    测试的裸 ToolContext 无 audit）。

    审计写入失败 → 返回 ok=False 且**带上已生效的 memory_id**：RAG 侧记忆已
    落库（副作用不可回滚），但按 10.4「审计失败不能假报成功」不得报成功，
    让模型/用户知道「记忆已建但审计缺失」而非静默。失败 summary 同样过
    ``_bounded_result``（P6-a）：``data`` 可能已贴预算上限（如 forget 批量
    800 条），原样再带一遍 + 无界 ``{exc}`` 会让信封冲破 4096、被消费点腰斩
    成非法 JSON、连同这句告警一起丢掉。故：异常文本先按 ``_AUDIT_ERROR_CHARS``
    封顶，整个 summary 再过 ``_bounded_result(list_keys=("memory_ids",))``——
    让 while 循环逐条丢尾部 id 收敛，而非整段落进 ``note`` 兜底。
    """
    audit = ctx.audit
    context = ctx.context
    if audit is None or context is None:
        logger.warning("rag_memory_audit_unwired", operation=operation)
        return None
    try:
        audit.record_rag_memory(
            operation=operation,
            trace_id=f"thread:{context.thread_id}",
            actor_account=context.user_id,
            actor_role=context.user_role,
            memory_id=data.get("memory_id"),
            memory_ids=data.get("memory_ids"),
            affected=data.get("affected"),
            kind=kind,
            subject=subject,
        )
    except Exception as exc:  # AuditFailureError 等：绝不假报成功（10.4）
        logger.error(
            "rag_memory_audit_failed",
            operation=operation,
            memory_id=data.get("memory_id"),
            error=type(exc).__name__,
        )
        exc_text = str(exc)
        if len(exc_text) > _AUDIT_ERROR_CHARS:
            exc_text = exc_text[:_AUDIT_ERROR_CHARS] + _TRUNCATION_MARK
        return ToolResultSummary(
            ok=False,
            summary=_bounded_result(
                {
                    "error": (
                        f"长期记忆已{operation}（memory_id={data.get('memory_id')}），"
                        f"但本地审计写入失败，需人工复核：{exc_text}"
                    ),
                    **{k: data.get(k) for k in ("memory_id", "memory_ids", "affected")},
                },
                ok=False,
                list_keys=("memory_ids",),
            ),
        )
    return None


def _record_recall_audit(
    ctx: ToolContext,
    *,
    args: RagRecallArgs,
    data: dict[str, Any],
) -> None:
    """成功召回长期记忆后补一条 agent 侧审计（2026-09-23，EVENT_RAG_RECALL）。

    落点与 ``_record_memory_audit`` 同层（工具 run 内调 ``ctx.audit``），**不经**
    task/attempt 路径（recall 纯读、is_write=False，确认卡与 payload hash 链路
    不动）。补这条是为了让「这一轮为什么这么答」可追：记忆是被召回但没记、还是
    压根没调，事后能分出来（此前纯读的 recall 无任何痕迹）。

    **不落 query 正文**——只落 ``query_chars``（与写侧「只落事实摘要、不落记忆
    正文」同口径，query 是用户会话内容）；理由见 ``AuditService.record_rag_recall``
    的 docstring。

    **hit_count 与 memory_ids 的口径（2026-09-23 修复自相矛盾）**：``data`` 是
    ``_bounded_result`` 收口**之后**的信封——它**故意保留顶层 ``hit_count`` 为
    服务端原值**、却会从命中列表尾部丢弃条目（见该函数注释）。故当结果被截断时
    ``hit_count > len(memory_ids)``：``memory_ids`` 是命中集的**前缀子集**（按
    服务端命中顺序取前若干条），而非全部命中。为让事后读者能区分「只命中 N 条」
    与「命中 M 条、只记了前 N 条」，本函数把信封里的 ``truncated`` 标志一并带进
    审计（``AuditService.record_rag_recall(truncated=...)``）：命中数不相等时，
    ``truncated`` 即为说明。另记录模型显式设定的 ``min_score``——否则「显式下限
    导致 0 命中」与「本来就没命中」在审计里无法区分。

    **失败语义（与写路径刻意不同的选择）**：审计写入失败**不得让召回失败**——
    调用方（``run_rag_recall``）捕获后 ``logger.error`` 并**照常返回 hits**。
    理由：recall 无副作用可回滚、结果已在手；与 10.4「审计失败不假报成功」**不
    冲突**（该条的前提是"有状态改变"，此处没有）。代价是"有一次召回没留痕"可能
    被忽略：这里**尽最大努力留痕**（error 级日志），但本仓日志无持久化、容器一
    重启即失，故**不保证这条痕迹事后可追**；审计表里也不会留下对应行。审计服务
    未接线时同样只告警跳过（生产 harness 恒接线 audit；单元测试的裸 ToolContext
    无 audit）。

    **关联键（如实说明）**：本事件经 ``record_event`` 落 ``task_id=None``、
    ``operation_id=None``、``trace_id=f"thread:{thread_id}"``。而 ``/audit``（
    ``AuditService.list_events``）的过滤键是 task_id/operation_id/event_type——
    三者都不含 thread，故**本事件挂不进某轮 ERP 任务的追查链**，只能按 thread
    （trace_id）关联同一会话轮次，或按 event_type=rag_recall 全表筛。这是纯读
    事件不建 task/attempt 的必然结果，非缺陷，但读审计时须知道。
    """
    audit = ctx.audit
    context = ctx.context
    if audit is None or context is None:
        logger.warning("rag_recall_audit_unwired")
        return
    hits = data.get("hits")
    memory_ids = [
        hit.get("memory_id")
        for hit in hits
        if isinstance(hit, dict) and hit.get("memory_id") is not None
    ] if isinstance(hits, list) else []
    try:
        audit.record_rag_recall(
            trace_id=f"thread:{context.thread_id}",
            actor_account=context.user_id,
            actor_role=context.user_role,
            query_chars=len(args.query),
            top_k=args.top_k,
            kind=args.kind,
            since=args.since,
            min_score=args.min_score,
            hit_count=data.get("hit_count") or 0,
            memory_ids=memory_ids,
            # 信封被尺寸收口而从尾部丢过条目时，hit_count（服务端原值）会大于
            # len(memory_ids)——把 truncated 一并落库，让「命中总数 vs 实际记下
            # 的 ids」这处差异有解释（见 docstring）。
            truncated=bool(data.get("truncated")),
        )
    except Exception as exc:  # 读路径：审计失败不影响召回（见 docstring）
        logger.error(
            "rag_recall_audit_failed",
            error=type(exc).__name__,
            hit_count=data.get("hit_count"),
        )


def _record_search_audit(
    ctx: ToolContext,
    *,
    args: RagSearchDocumentsArgs,
    data: dict[str, Any],
) -> None:
    """成功检索文档后补一条 agent 侧审计（EVENT_RAG_SEARCH）。

    落点与 ``_record_recall_audit`` 同层（工具 run 内调 ``ctx.audit``），**不经**
    task/attempt 路径（检索纯读、is_write=False，确认卡与 payload hash 链路不动）。
    补这条是为了让「这一轮为什么这么答」可追：文档片段是被检索到但没引用、还是
    压根没调，事后能分出来。

    **不落 query 正文**——只落 ``query_chars``（与 ``_record_recall_audit`` 同口径，
    query 是用户会话内容）；理由见 ``AuditService.record_rag_search_documents``。

    **hit_count 与 ids 的口径**：``data`` 是 ``_bounded_result`` 收口**之后**的
    信封——它故意保留顶层 ``hit_count`` 为服务端原值、却会从命中列表尾部丢弃
    条目。故结果被截断时 ``hit_count > len(ids)``：ids 是命中集的**前缀子集**。
    把信封里的 ``truncated`` 标志一并带进审计，让这处差异有解释（见
    ``record_rag_search_documents`` docstring）。

    **失败语义（与写路径刻意不同的选择）**：审计写入失败**不得让检索失败**——
    调用方（``run_rag_search_documents``）捕获后 ``logger.error`` 并**照常返回
    hits**。理由：检索无副作用可回滚、结果已在手（与 ``_record_recall_audit``
    一致）。审计服务未接线时只告警跳过。
    """
    audit = ctx.audit
    context = ctx.context
    if audit is None or context is None:
        logger.warning("rag_search_audit_unwired")
        return
    hits = data.get("hits")
    chunk_ids = [
        hit.get("chunk_id")
        for hit in hits
        if isinstance(hit, dict) and hit.get("chunk_id") is not None
    ] if isinstance(hits, list) else []
    document_ids = [
        hit.get("document_id")
        for hit in hits
        if isinstance(hit, dict) and hit.get("document_id") is not None
    ] if isinstance(hits, list) else []
    try:
        audit.record_rag_search_documents(
            trace_id=f"thread:{context.thread_id}",
            actor_account=context.user_id,
            actor_role=context.user_role,
            query_chars=len(args.query),
            top_k=args.top_k,
            hit_count=data.get("hit_count") or 0,
            chunk_ids=chunk_ids,
            document_ids=document_ids,
            truncated=bool(data.get("truncated")),
        )
    except Exception as exc:  # 读路径：审计失败不影响检索（见 docstring）
        logger.error(
            "rag_search_audit_failed",
            error=type(exc).__name__,
            hit_count=data.get("hit_count"),
        )


def run_rag_remember(ctx: ToolContext, args: RagRememberArgs) -> ToolResultSummary:
    """记一条长期记忆（subject 由本层按 kind 与登录账号注入，忽略模型给的 subject）。"""
    guard = _require_rag(ctx)
    if guard is not None:
        return guard
    user_id = ctx.context.user_id
    thread_id = ctx.context.thread_id or None
    payload: dict[str, Any] = {
        "content": args.content,
        "kind": args.kind,
        "subject": _subject_for_remember(args.kind, user_id),
        "source_thread": thread_id,
        "source_note": args.source_note,
    }
    result = _call_rag(ctx, "remember", payload)
    if result.ok:
        # F1：成功新增记忆后补审计（谁/哪条 memory_id/做了什么）——失败转错误
        # 结果（记忆已落 RAG，审计缺失不静默，10.4）。
        audit_error = _record_memory_audit(
            ctx,
            operation="remember",
            data=result.summary,
            subject=payload["subject"],
            kind=args.kind,
        )
        if audit_error is not None:
            return audit_error
    return result


def run_rag_recall(ctx: ToolContext, args: RagRecallArgs) -> ToolResultSummary:
    """按语义召回长期记忆（**无条件**注入 subject=登录账号，否则账号偏好召不回）。"""
    guard = _require_rag(ctx)
    if guard is not None:
        return guard
    payload: dict[str, Any] = {
        "query": args.query,
        "subject": ctx.context.user_id,
    }
    # top_k 等可选项只在模型显式给出时下发——RAG 侧 recall 的 top_k 不接受
    # None（`1 <= None` 会 TypeError），故不传即由服务端取默认值 5。
    if args.top_k is not None:
        payload["top_k"] = args.top_k
    if args.kind is not None:
        payload["kind"] = args.kind
    if args.since is not None:
        payload["since"] = args.since
    if args.min_score is not None:
        payload["min_score"] = args.min_score
    result = _call_rag(ctx, "recall", payload)
    if result.ok:
        # 2026-09-23：成功召回补一条 agent 侧审计（纯读，EVENT_RAG_RECALL）。
        # 失败不改变返回值——读工具审计失败不得让召回失败（见
        # _record_recall_audit docstring）。RAG 未接线/MCP 调用失败 → 无
        # result.ok，不记（没有可观测的事发生）。
        _record_recall_audit(ctx, args=args, data=result.summary)
    return result


def run_rag_search_documents(
    ctx: ToolContext, args: RagSearchDocumentsArgs
) -> ToolResultSummary:
    """检索公司共享知识库的文档片段（**纯读**，无账号归属、不注入 subject）。

    与 ``run_rag_recall`` 同款：**不注入 subject**（文档线是共享知识库，
    RAG 侧 ``search_documents`` 也不收 subject——别发明租户模型）。成功结果由
    MCP 客户端从 ``content[0].text`` 解析成 dict（见 ``mcp.client._content_to_data``）
    后经 ``_call_rag`` 收口；失败读 ``structuredContent`` 的错误信封——两条通道
    由 ``_call_rag`` / ``mcp.client._unpack_tool_result`` 统一处理，本层不重造。
    ``top_k`` 缺省时**不下发**（交给 RAG 侧取默认 5）。
    """
    guard = _require_rag(ctx)
    if guard is not None:
        return guard
    payload: dict[str, Any] = {"query": args.query}
    if args.top_k is not None:
        payload["top_k"] = args.top_k
    result = _call_rag(ctx, "search_documents", payload)
    if result.ok:
        # 成功检索补一条 agent 侧审计（纯读，EVENT_RAG_SEARCH）。失败不改变
        # 返回值——读工具审计失败不得让检索失败（见 _record_search_audit）。
        _record_search_audit(ctx, args=args, data=result.summary)
    return result


def run_rag_forget(ctx: ToolContext, args: RagForgetArgs) -> ToolResultSummary:
    """遗忘长期记忆（软删）。subject 按路径注入：query 路径→账号；memory_id 路径→None。

    读 ``hengfa-rag`` ``memory/service.forget``：memory_id 路径对给定 subject
    按字面等值过滤，注入 user_id 会让「忘一条全局记忆」命中 0 行（affected=0
    被读成已忘掉，实为静默失效）。query 路径不给 subject 只作用于全局，故注入
    user_id 把批量软删收紧在本账号内（防误删其它账号）。
    """
    guard = _require_rag(ctx)
    if guard is not None:
        return guard
    payload: dict[str, Any] = {
        "memory_id": args.memory_id,
        "query": args.query,
    }
    # 只有 query 批量路径才注入账号（memory_id 路径不注入，见 docstring）。
    payload["subject"] = ctx.context.user_id if args.memory_id is None else None
    result = _call_rag(ctx, "forget", payload)
    if result.ok:
        # F1：软删是状态变更（与纯读 memory_lookup 不同类），成功即补审计。
        # affected 可能为 0（幂等无命中），仍记录——它是一次遗忘操作的事实。
        audit_error = _record_memory_audit(
            ctx,
            operation="forget",
            data=result.summary,
            subject=payload["subject"],
            kind=None,
        )
        if audit_error is not None:
            return audit_error
    return result
