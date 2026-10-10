"""SSE 事件投影白名单（A 档：只落结构性元数据，绝不落模型输入输出原文）。

可观测性第 0 步：「SSE 事件流结构性元数据落库」的核心闸门。把图/路由层
发射的原始事件 payload **降维**成结构量（长度、枚举、键名、布尔标记），
任何未声明的事件类型与未声明的键一律丢弃（**fail closed**）。

设计要点：
- **纯函数、零 I/O、零依赖**：本模块只做 dict → dict 变换。放在 ``web/``
  而非 ``observability/``，因为 ``routes_chat`` 已 import ``observability``，
  反向依赖会成环；也**刻意不 import ``web.chat_streams``**——事件类型键用
  字符串字面量（见下「漂移守护」），避免 ``chat_streams → stream_persistence
  → stream_projection → chat_streams`` 的循环 import。
- **禁止通配符**：``PROJECTION`` 只列显式键白名单，未列出的键一律丢弃，
  且**任何情况下不得引入 ``"*"`` 或正则式放行**。未来扩白名单时若有人想
  「顺手透传」——那正是本模块要挡的失败模式（DB 层还有第二道锁，见迁移）。
- **`allow` 是键白名单，`derive` 是降维**：纯键过滤做不到「把 ``chunk``
  原文变成长度」，所以允许的键先原样通过（仅限标量），再由 ``derive`` 覆盖/
  补充结构量。``derive`` 不做「删除」——要删某键就不把它放进 ``allow``。
- **字符串字面量而非 import 常量**：常量改名时白名单静默失配 = fail-closed
  生效（落 ``{}``），而失配会被漂移测试
  ``test_stream_projection.test_projection_covers_all_event_constants``
  立刻抓到。这正是我们要的：**改常量的人必须显式来改本表**。

A 档边界（务必遵守）：
- ``agent_reply.reply_text`` / ``agent_reply_chunk.chunk`` /
  ``agent_reasoning_chunk.chunk`` / ``memory_compressed.summary`` —— 模型
  原文，**一律剥掉**，只落 ``*_chars`` 长度；
- ``memory_compressed.index``（模型生成的短索引名，可能夹带客户名）——
  按用户口径**只落 ``index_chars``，不落内容**；
- ``confirmation_requested.card`` / ``tool_result.summary`` /
  ``reconcile_report.summary_counts`` / ``export_ready.filename`` —— 含业务
  数据（金额/客户名/户数），**剥掉**（``card`` 降维为**键名排序列表**——
  「这张卡有哪些字段」正是排查「卡片缺字段」的关键，键名是字段名不是数据）；
- ``agent_error.message`` —— 剥掉。13+ 个构造点里只有 ``routes_chat.py``
  一处带 ``message``（写死常量），收益为零、风险为正（未来任何人写
  ``message=f"未找到客户 {name}"`` 都会自动透传）。``has_message: bool``
  保留「这条错误带不带人话」这个结构信息，零泄漏。

关于 ``agent_reasoning_chunk``：规格 11.4 写了「不落日志、不持久化」。本模块
落的**不是思考内容，是「思考了 N 字」这个事实**——与 11.4 不冲突。此判断已
在方案评审中单独标注复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: 值级字符串长度上限：``index``/``summary`` 之外的长值基本可断定是原文泄漏。
MAX_VALUE_CHARS = 200
#: 截断后缀（与 ``observability/log_capture`` 的 ``…(truncated)`` 不同字面量，
#: 避免与日志缓冲口径混淆）。
TRUNCATED_SUFFIX = "_truncated"

#: 记录类别（与 ORM CheckConstraint 取值一致）。
RECORD_KIND_PUSHED = "pushed"
RECORD_KIND_SUPPRESSED = "suppressed"
RECORD_KIND_STREAM_CREATED = "stream_created"
RECORD_KIND_UNMAPPED = "unmapped"

#: 抑制原因枚举（路由层 ``_flush`` 的三个抑制分支；影子行 payload 注入用）。
SUPPRESSED_REASONS: frozenset[str] = frozenset(
    {
        "interrupted",
        "final_step:model_failed",
        "final_step:steps_exhausted",
    }
)

#: 代码枚举（投影时把原始值收敛到已知集合，未知 → "other"）。
_CANCELLED_REASONS = frozenset({"generation_cancelled"})
#: 压缩判定台账的四态闭集（compression_node 的 5 条出口各映射其一）。
_COMPRESSION_DECISIONS = frozenset(
    {"compressed", "no_segment", "tier_gone", "failed"}
)
_SESSION_EXPIRED_REASONS = frozenset({"session_expired"})
_MEMORY_COMPRESSION_ERRORS = frozenset({"summary_failed"})
#: agent_error 的已知 error 码全集（扫 tools_node/tool_args_node/confirm_node/
#: model_node/compression_node/routes_chat 的构造点汇总；未知一律 "other"，
#: 防有人塞 ``str(exc)`` 原文）。
_AGENT_ERROR_CODES = frozenset(
    {
        "task_not_found",
        "task_state_changed",
        "unknown_tool",
        "history_truncated",
        "model_unavailable",
        "steps_exhausted",
        "invalid_arguments",
        "not_implemented",
        "execution_error",
        "scope_denied",
        "shutting_down",
        "harness_misconfigured",
        "task_creation_error",
        "queue_cancelled",
        "queue_wait_timeout",
        "argument_generation_error",
        "tool_name_mismatch",
        "chat_run_failed",
        # tools_node.py:493 写工具连续失败熔断
        "write_retry_suppressed",
        # tools_node.py:577 写工具 prepare 阶段失败（fail closed）
        "preparation_error",
    }
)


def _text_len(value: Any) -> int:
    """文本长度（非字符串 → 0；不落内容，只落长度）。"""
    return len(value) if isinstance(value, str) else 0


def _len_of(key: str) -> Callable[[dict[str, Any]], int]:
    """``derive`` 工厂：``{key + "_chars": len(raw[key])}``。"""
    return lambda raw: _text_len(raw.get(key))


def _enum_of(
    key: str, allowed: frozenset[str]
) -> Callable[[dict[str, Any]], str]:
    """``derive`` 工厂：把 ``raw[key]`` 收敛到已知枚举，未知 → ``"other"``。

    fail closed：集合外的值一律收敛为 ``"other"``，**不提供通配符放行**——
    模块 docstring 明令禁止 ``"*"``/正则式透传，任何「顺手放行」的口子都是
    要挡的失败模式。
    """
    def _derive(raw: dict[str, Any]) -> str:
        value = raw.get(key)
        if isinstance(value, str) and value in allowed:
            return value
        return "other"

    return _derive


def _registered_tool_names() -> frozenset[str]:
    """工具注册表**键集**（枚举真源；延迟导入，见下）。

    ⚠️ **必须函数内延迟 import**，不得提到模块顶层：本模块的不变量是
    「纯函数、零依赖」（见模块 docstring），顶层反向 import ``agent.*`` 会与
    ``agent/nodes/compression_node.py`` 反向 import ``web`` 形成只靠运气不
    闭合的环（``web/chat_streams.py`` 运行时 import 本模块）——一旦闭合就是
    应用 import 期直接炸，且**无任何测试会提前变红**。先例：
    ``agent/prompts/skills`` 的 ``_registered_tool_names``，同为函数内延迟 import。

    ⛔ **守卫不可删**：投影按**注册表的键**判定，而模型看到/回声的是
    ``spec.name``（``model_node`` 渲染进提示词的工具名）。两者一旦分叉，该工具
    的所有事件会静默变 ``"other"``，而三层测试**全部照绿**（期望值取自同一
    注册表）——本仓「双真源」的典型形态。断言在**本函数被调用时**（即投影求值
    ``loader()``）执行，**不是 import 期**；且 ``python -O`` 会剥离 assert，
    故它只是开发/测试期闸门，运行期指望它挡分叉是不成立的。
    """
    from administrative_agent.agent.tools import TOOL_REGISTRY

    assert set(TOOL_REGISTRY) == {s.name for s in TOOL_REGISTRY.values()}
    return frozenset(TOOL_REGISTRY)


def _registered_skill_keys() -> frozenset[str]:
    """流程键集（``ALL_SKILL_KEYS``；延迟导入，理由同 ``_registered_tool_names``）。"""
    from administrative_agent.agent.prompts.skills import ALL_SKILL_KEYS

    return frozenset(ALL_SKILL_KEYS)


def _enum_or_none(
    key: str, loader: Callable[[], frozenset[str]]
) -> Callable[[dict[str, Any]], str]:
    """``derive`` 工厂：**三态**枚举（区别于 ``_enum_of`` 的两态）。

    | 输入 | 输出 |
    |---|---|
    | 已知名（``loader()`` 成员） | 原名 |
    | 其他非空字符串 | ``"other"``（= 模型幻觉串，fail closed） |
    | 缺失 / ``None`` / 空串 / 非字符串 | ``"none"`` |

    ⛔ **不能用 ``_enum_of`` 代替**：它对 ``None`` 与幻觉串**返回同一个
    ``"other"``**，而「本轮没有值」与「值是幻觉串」必须可分——后者正是要抓的
    失败模式（``model_node`` 早退分支发出的 ``agent_error`` payload 本就不带
    ``tool_name``，那些事件应落 ``"none"``）。``loader`` 由调用方给（延迟导入
    的真源函数），本模块因此保持零顶层依赖。
    """
    def _derive(raw: dict[str, Any]) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            return "none"
        if value in loader():
            return value
        return "other"

    return _derive


def _card_keys(raw: dict[str, Any]) -> list[str]:
    """确认卡 ``card`` 降维为**键名排序列表**（只落 schema，不落值）。"""
    card = raw.get("card")
    if isinstance(card, Mapping):
        return sorted(str(k) for k in card.keys())
    return []


def _as_bool(key: str) -> Callable[[dict[str, Any]], bool]:
    return lambda raw: bool(raw.get(key))


def _always(value: Any) -> Callable[[dict[str, Any]], Any]:
    return lambda _raw: value


@dataclass(frozen=True)
class Rule:
    """单个事件类型的投影规则。

    - ``allow``：键白名单（原样通过，仅限标量；**禁止通配符**）；
    - ``derive``：降维字段（在 ``allow`` 之后应用，可覆盖同名键或不含在
      ``allow`` 里的派生键）。``derive`` 不负责删除——不想要某键就别放进
      ``allow``。
    """

    allow: frozenset[str]
    derive: Mapping[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectedPayload:
    """投影结果：落库 payload + 记录类别 + 值级截断计数。"""

    payload: dict[str, Any]
    record_kind: str
    truncated_values: int = 0


#: 投影白名单（事件类型 → 规则）。键 = 事件类型字符串字面量（见模块 docstring
#: 的「漂移守护」）。**每新增一个 EVENT_* 常量必须同步补这里**，否则该类型落
#: ``{}`` + ``record_kind='unmapped'``（fail closed，且可在面板上看到计数）。
PROJECTION: dict[str, Rule] = {
    # 1. task_failed：本仓无构造点，payload 不可信 → 全剥
    "task_failed": Rule(allow=frozenset()),
    # 2. task_cancelled：reason 收敛到代码枚举
    "task_cancelled": Rule(
        allow=frozenset({"reason"}),
        derive={"reason": _enum_of("reason", _CANCELLED_REASONS)},
    ),
    # 3. SESSION_EXPIRED：reason 收敛到代码枚举
    "SESSION_EXPIRED": Rule(
        allow=frozenset({"reason"}),
        derive={"reason": _enum_of("reason", _SESSION_EXPIRED_REASONS)},
    ),
    # 4. agent_started：thread_id 是结构量（非业务数据）
    "agent_started": Rule(allow=frozenset({"thread_id"})),
    # 5. tool_running：stage 短枚举；tool_name 收敛到注册表键（三态）
    "tool_running": Rule(
        allow=frozenset({"task_id", "stage"}),
        derive={"tool_name": _enum_or_none("tool_name", _registered_tool_names)},
    ),
    # 6. tool_result：剥 summary（可能含明细/金额/客户名）；tool_name 收敛
    "tool_result": Rule(
        allow=frozenset({"task_id", "ok"}),
        derive={"tool_name": _enum_or_none("tool_name", _registered_tool_names)},
    ),
    # 7. confirmation_requested：剥 card；card 降维为键名排序列表；tool_name 收敛
    "confirmation_requested": Rule(
        allow=frozenset({"task_id", "payload_hash", "re_requested"}),
        derive={
            "tool_name": _enum_or_none("tool_name", _registered_tool_names),
            "card_keys": _card_keys,
            "re_requested": _as_bool("re_requested"),
        },
    ),
    # 8. agent_reply：剥 reply_text（模型原文），只落长度
    "agent_reply": Rule(
        allow=frozenset(),
        derive={"reply_chars": _len_of("reply_text")},
    ),
    # 9. agent_error：剥 message，只留 has_message 布尔；tool_name 收敛三态
    "agent_error": Rule(
        allow=frozenset({"error", "tool_executed"}),
        derive={
            "error": _enum_of("error", _AGENT_ERROR_CODES),
            "tool_name": _enum_or_none("tool_name", _registered_tool_names),
            "tool_executed": _as_bool("tool_executed"),
            "has_message": _as_bool("message"),
        },
    ),
    # 10. agent_reply_start：无信息量
    "agent_reply_start": Rule(allow=frozenset()),
    # 11. agent_reply_chunk：剥 chunk（模型原文），只落长度
    "agent_reply_chunk": Rule(
        allow=frozenset(),
        derive={"chunk_chars": _len_of("chunk")},
    ),
    # 12. agent_reasoning_chunk：剥 chunk（模型思考原文），只落长度
    "agent_reasoning_chunk": Rule(
        allow=frozenset(),
        derive={"chunk_chars": _len_of("chunk")},
    ),
    # 13. memory_compressed：剥 summary；index 只落长度（不落内容）
    "memory_compressed": Rule(
        allow=frozenset(
            {"segment_no", "tier", "summary_chars", "archived_messages"}
        ),
        derive={"index_chars": _len_of("index")},
    ),
    # 14. memory_compression_failed：error 不在已知枚举内 → "other"
    "memory_compression_failed": Rule(
        allow=frozenset({"error"}),
        derive={"error": _enum_of("error", _MEMORY_COMPRESSION_ERRORS)},
    ),
    # 15. reconcile_report：剥 summary_counts（含金额/户数等业务聚合值）
    "reconcile_report": Rule(
        allow=frozenset({"report_id", "row_count", "subject_match_status"}),
    ),
    # 16. export_ready：剥 filename（可能含客户名），只落长度
    "export_ready": Rule(
        allow=frozenset({"file_id", "entity", "row_count", "total", "expires_at"}),
        derive={"filename_chars": _len_of("filename")},
    ),
    # 17. re_requested：路由层 _GRAPH_EVENT_TO_SSE 已映射成 confirmation_requested
    #     （收口处看到的是映射后类型）→ 防御性冗余，留着防未来有人在映射前落库。
    #     tool_name 与 confirmation_requested 同口径收口（三态）——「落库的
    #     tool_name 只可能是枚举值」这条不变量不许有例外：若本类型真不可达，补它
    #     零成本；若可达，这里就是洞。
    "re_requested": Rule(
        allow=frozenset({"task_id", "payload_hash", "re_requested"}),
        derive={
            "tool_name": _enum_or_none("tool_name", _registered_tool_names),
            "card_keys": _card_keys,
            "re_requested": _always(True),
        },
    ),
    # 18. compression_decision：压缩判定台账（4 键；decision 走闭集枚举）
    "compression_decision": Rule(
        allow=frozenset(
            {"occupancy_pct", "messages_count", "segment_messages"}
        ),
        derive={"decision": _enum_of("decision", _COMPRESSION_DECISIONS)},
    ),
    # 19. decision_input_summary：决策轮模型输入摘要（4 键 + active_skill 三态）
    "decision_input_summary": Rule(
        allow=frozenset(
            {"history_raw", "history_sent", "evicted_details", "summaries_count"}
        ),
        derive={
            "active_skill": _enum_or_none("active_skill", _registered_skill_keys)
        },
    ),
    # 20. args_input_summary：参数轮模型输入摘要（3 键 + active_skill 三态）
    "args_input_summary": Rule(
        allow=frozenset({"history_raw", "history_sent", "evicted_details"}),
        derive={
            "active_skill": _enum_or_none("active_skill", _registered_skill_keys)
        },
    ),
}


def _is_scalar(value: Any) -> bool:
    """原样放行的白名单键取值类型（仅标量；dict/list 一律丢弃 = fail closed）。"""
    return value is None or isinstance(value, (str, int, float, bool))


def _guard_value(value: Any) -> tuple[Any, int]:
    """值级守卫：超长字符串截断到 ``MAX_VALUE_CHARS`` 并计一个截断。

    仅处理 str 与 list[str]（``card_keys`` 这类派生列表）；其余原样返回。
    """
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + TRUNCATED_SUFFIX, 1
        return value, 0
    if isinstance(value, list):
        out: list[Any] = []
        truncated = 0
        for item in value:
            guarded, n = _guard_value(item)
            out.append(guarded)
            truncated += n
        return out, truncated
    return value, 0


def project(event_type: str, payload: dict[str, Any] | None) -> ProjectedPayload:
    """把原始事件 payload 投影成结构量（未声明类型 → ``{}`` + ``unmapped``）。

    未在 ``PROJECTION`` 中的事件类型**绝不默认透传**：返回空 payload 与
    ``record_kind='unmapped'``（调用方据此累加 ``unmapped_events`` 计数）。
    """
    raw = payload if isinstance(payload, dict) else {}
    rule = PROJECTION.get(event_type)
    if rule is None:
        return ProjectedPayload(payload={}, record_kind=RECORD_KIND_UNMAPPED)

    out: dict[str, Any] = {}
    for key in rule.allow:
        if key in raw and _is_scalar(raw[key]):
            out[key] = raw[key]
    for name, derive in rule.derive.items():
        out[name] = derive(raw)

    guarded: dict[str, Any] = {}
    truncated = 0
    for key, value in out.items():
        value2, n = _guard_value(value)
        guarded[key] = value2
        truncated += n
    return ProjectedPayload(
        payload=guarded, record_kind=RECORD_KIND_PUSHED, truncated_values=truncated
    )


def project_suppressed(
    event_type: str, payload: dict[str, Any] | None, reason: str
) -> ProjectedPayload:
    """被展示门控抑制的事件（影子行）投影：先投影再注入 ``suppressed_reason``。

    ``suppressed_reason`` 由本函数内部注入，**不进 ``PROJECTION`` 白名单表**
    （白名单管的是**输入** payload，影子行的 payload 是我们自己构造的）。
    未知 reason 收敛到 ``"unknown"``（fail closed，防有人塞自由文本）。
    """
    base = project(event_type, payload)
    out = dict(base.payload)
    out["suppressed_reason"] = reason if reason in SUPPRESSED_REASONS else "unknown"
    return ProjectedPayload(
        payload=out,
        record_kind=RECORD_KIND_SUPPRESSED,
        truncated_values=base.truncated_values,
    )
