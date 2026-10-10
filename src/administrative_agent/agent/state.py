"""LangGraph State 与不可变身份 context（8.4 / 对话式 harness 改造）。

会话循环图（Phase 1）：
- ``ConversationState`` 为本图的状态类型：``messages`` 是唯一恢复单元
  （Annotated[list, add_messages]，checkpoint 重放粒度），任务式流水线
  的业务字段（structured_intent 等）不再进入 State；写操作经工具节点
  interrupt 确认（Phase 2），挂起确认卡存 ``pending_confirmation``。
- ``task_id`` 可空：纯对话轮次无任务；写工具执行时才产生任务（Phase 2）。
- ``policy_keys`` 记录已加载规则包 key（模型 system prompt 注入用）。
- ``pending_action`` 为模型最新决策的字典化 AgentDecision（model 节点写、
  tools 节点消费后清空，route_after_model 按 next_action 路由）。

- ``AgentContext`` 为 frozen dataclass（D4 身份模型 Account 的角色/账号 +
  请求级 scope 集）；身份和权限由每次请求的不可变 ``AgentContext`` 注入，
  **不允许写入 State**（8.4 末段：user_id/user_role/scope 不入 State）。
- ``remaining_steps`` 常量 25（13.3）：**预算按每条用户消息重置**——
  web 提交入口（routes_chat._initial_inputs）每收到一条新 human 消息
  即写回 REMAINING_STEPS；图内 model 节点每轮决策消费 1，单条消息内
  的模型-工具循环连续递减，耗尽时停止新的工具调用并 fail closed
  （防失控循环不变量）。确认卡 resume（Command(resume)）不重置——
  恢复执行继续消耗挂起轮剩余的额度。
- 本模块不 import tools（避免循环）：EventSink/ToolContext 在 tools.py
  定义；状态侧只依赖 Callable[[str, dict], None] 形态的事件回调。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph.message import add_messages
from typing_extensions import Annotated

#: 13.3：单条用户消息的决策步数上限（web 提交入口每条新 human 消息
#: 重置回本值——见 routes_chat._initial_inputs；model 节点每轮消费 1）。
#: 实现口径：model 节点入口 remaining <= 0 即拦截本轮（steps_exhausted
#: fail closed），不再发起新一轮模型决策/工具调用。
REMAINING_STEPS: int = 25


@dataclass(frozen=True)
class AgentContext:
    """每次请求的不可变身份与权限 context（8.4 末段）。

    user_id 为真实账号（12.5：manager 为共享账号，不虚构自然人身份）；
    scope 为 2.4 最小 scope 集（erp:query 等六项，mcp/scopes.py 校验）。
    context 由请求级依赖注入，不进入 LangGraph State。
    """

    user_id: str  # 真实账号（actor_account）
    user_role: str  # manager / developer
    scope: frozenset[str] = frozenset()  # 2.4 六项子集
    thread_id: str = ""  # 会话对话线程（task 记录 thread_id）

    def has_scope(self, scope: str) -> bool:
        return scope in self.scope


class ConversationState(TypedDict, total=False):
    """[2026-08-24 harness 改造] 会话循环图 State。

    只保存恢复所需的结构化字段：``messages`` 为完整对话历史（LangChain
    消息对象，add_messages 合并——每次模型/工具输出追加为一条消息），
    checkpoint 以 thread_id 粒度整段重放（8.5 升级定位为对话历史持久层）。

    注意：本 TypedDict 没有 user_id/user_role/scope 键——身份与权限
    不允许写入 State（8.4 末段），模型无法经 State 读取或伪造身份。
    """

    #: 完整对话历史（模型/工具输出逐条追加；Phase 1 无截断预算线，
    #: 40k token 滚动摘要为后续演进点，见 docs/plan/harness-phase0-report.md）
    messages: Annotated[list[Any], add_messages]
    #: 记忆压缩（2026-08-24 定稿）
    compression_stats: dict[str, int]   # {档位(str): 已触发次数}（checkpoint 序列化后键为 str）
    summaries: list[dict[str, Any]]     # 摘要+目录条目：[{"segment_no", "tier", "summary", "index"}]
    summary_boundary: int               # 已归档消息总数（全局计数；下次压缩只处理其后的消息）
    #: 最近写任务 ID（可空：纯对话轮次无任务；写工具执行后回填，Phase 2）
    task_id: str | None
    trace_id: str
    current_step: str
    #: 本轮剩余步骤（13.3；model 节点每轮决策消费 1；每条新 human 消息
    #: 由 web 提交入口重置为 REMAINING_STEPS，确认卡 resume 不重置）
    remaining_steps: int
    #: 已加载规则包 key 清单（模型 system prompt 注入；工具节点追加）
    policy_keys: list[str]
    #: [2026-09-13 skill 层接入] 本轮激活的流程 skill_key（**last-write-wins
    #: 覆盖语义**——模型每轮在 AgentDecision.skill_name 里重新声明，model 节点
    #: 原样写入；不声明（null）即覆盖为 None = 退出流程。**不是** policy_keys
    #: 那种"工具节点追加"的累积语义，两者实现口径不同，勿混用。
    #: 流程正文每轮现场渲染进 system prompt，不写入 messages、不占对话预算。
    active_skill: str | None
    #: 模型最新决策（AgentDecision 字典化；model 节点写、tools 节点消费清空）
    pending_action: dict[str, Any] | None
    #: 挂起确认卡（Phase 2 interrupt 时写入；恢复路径/历史重放读取）
    pending_confirmation: dict[str, Any] | None
    #: 写工具连续失败重试计数（{抑制键: 次数}；同一写操作成功清零，达上限拦截自动重试——修复确认卡重复循环）
    write_retry_counts: dict[str, int]
