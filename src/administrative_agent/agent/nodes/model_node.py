"""model 节点：历史截断 → system prompt 组装 →
模型决策 → 追加 AIMessage + pending_action。

由 agent/graph.py 拆分后位于 nodes/model_node.py——
模型面与系统提示组装（LangGraph 惯例：节点实现独立于图装配）。

工具表为**索引**（name + 何时调用，去掉参数 JSON
示例）；完整参数 schema 与规则包族正文由 tool_args 节点注入（参数轮
system prompt），决策轮只给工具枚举表。

**40k token 预算**（``MAX_MODEL_INPUT_CHARS``，~3.5 字符/token，drop-front
截断）防历史无限膨胀；截断优先保留最新一条
role=human 消息（用户最新问题永远可见——超大工具结果顶在前面时不再丢首条
用户问题）；成功路径发射 ``agent_reply`` 事件（前端 SSE 展示最终回复）。

决策轮改流式：调用前发射 ``agent_reply_start``
（前端开新气泡），生成过程经 ``on_chunk`` 逐 token 发射
``agent_reply_chunk``（前端逐字渲染），终态 ``agent_reply`` 语义不变。

决策轮思考增量经 ``on_reasoning_chunk`` 逐 token
发射 ``agent_reasoning_chunk``（前端灰色"正在思考"区逐字渲染；11.4
不落日志、不持久化——SSE 事件流进程内短时投递，刷新不重放）。

call_tool 过渡轮：**不追加 AIMessage**（过渡文案
"正在处理，请稍候。"不进对话历史——历史重放与模型上下文都无噪音）；
``agent_reply`` 事件带 ``next_action: "call_tool"`` 标记（reply_text 字段
保留，契约稳定），前端据此丢弃流式气泡；reply / needs_input 轮照常追加
AIMessage（needs_input 追问是用户回答问题的依据）。

模型输入组装（本节点与 tool_args 节点共用）在
40k 预算截断之前先经 ``_filter_stale_details`` 过滤「最新 human 之前」的
只读查询明细 ToolMessage（``additional_kwargs.detail`` 标记、已存档有
query_id 可回查者；含 attachment_preview 预览——其无存档、
按 file_id 重取）——明细只在产生它的那一轮对模型可见；State/checkpoint/
压缩/存档一律保留原文（纯视图层过滤，不物理删除消息）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from administrative_agent.agent.configuration import RuntimeBag, _emit
from administrative_agent.agent.model_client import (
    ModelCallError,
    ModelDecisionClient,
    build_select_schema,
)
from administrative_agent.agent.prompts.base import (
    BUSINESS_CONTEXT,
    CONFIRMATION_MECHANISM_RULE,
    RECONCILE_WORKFLOW_RULE,
    REPLY_FORMAT_INSTRUCTION,
    base_system_prompt_for,
    lookup_first_rule_for,
)
from administrative_agent.agent.prompts.policies import (
    ALL_POLICY_KEYS,
    POLICY_ADMISSION,
    POLICY_REGISTRY,
)
from administrative_agent.agent.prompts.skills import (
    ALL_SKILL_KEYS,
    SkillPackage,
    render_schema_instruction,
    skill_for,
)
from administrative_agent.agent.state import ConversationState
from administrative_agent.agent.tools import POLICY_TOOLS
from administrative_agent.config import settings
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_ERROR,
    EVENT_AGENT_REASONING_CHUNK,
    EVENT_AGENT_REPLY,
    EVENT_AGENT_REPLY_CHUNK,
    EVENT_AGENT_REPLY_START,
    EVENT_DECISION_INPUT_SUMMARY,
)

#: 40k token 预算的字符上限（约 3.5 字符/token；对话历史超限时 drop-front
#: 截断——保留最近上下文（确认卡/工具结果），丢弃最早消息）。
#: [2026-08-24 定稿] 由 settings.dialogue_budget_chars 派生（原 140000 常量值
#: 即配置项默认值；压缩档位判定共用同一"对话预算"分母）。
MAX_MODEL_INPUT_CHARS: int = settings.dialogue_budget_chars


def _render_skill_body(skill: SkillPackage, *, include_policies: bool = True) -> str:
    """激活流程的正文段：编排段（skill 步骤）+ 口径段（其引用的规则包原文）。

    [2026-09-13 skill 层接入] 两段并列是 skill/policy 正交两层的注入口径：
    编排来自 skill，口径来自 policy 原文，skill **不复述**口径。

    两轮各自注入、口径段只在决策轮给：

    - **决策轮**（``include_policies=True``）：选工具前就该看到口径，故编排 +
      引用规则包正文一并注入。
    - **参数轮**（``include_policies=False``，tool_args 节点调用）：那里的口径
      由工具的 ``policy_family`` 注入，而工具族与 skill 引用的规则包**会重
      叠**（erp_query 的 family 就是 ``R4.query``，财报 skill 也引用它）——
      同一个 prompt 里注入两遍既费 token 又给出两处「口径以此为准」。故参数轮
      只给编排段，告诉它此刻在走哪一步、这一步的取数口径是什么形状。
    """
    text = (
        f"当前流程：【{skill.title}】（{skill.skill_key} v{skill.version}）"
        "——按下列步骤顺序执行，不得跳步或并行：\n" + skill.text
    )
    if not include_policies:
        return text
    # `if k in POLICY_REGISTRY` 是**刻意保留**的防御，不是冗余：加载期已校验
    # `policies ⊆ ALL_POLICY_KEYS`，所以正常情况下恒真。但若注册表在运行期被
    # 改动（测试 monkeypatch、热更），直接下标会 KeyError → 在建 prompt 时抛，
    # 即**整轮对话崩**；守卫则退化为"少一段口径"。与 `AgentDecision.skill_name`
    # 给默认值同一个论证：引导性内容的爆炸半径不对称，取退化侧。
    refs = [POLICY_REGISTRY[k] for k in skill.policies if k in POLICY_REGISTRY]
    if refs:
        text += "\n\n本流程引用的业务规则（口径以此为准）：\n" + "\n\n".join(
            f"【{p.policy_key}】{p.title}\n{p.text}" for p in refs
        )
    return text


def _build_system_prompt(
    runtime: RuntimeBag, state: ConversationState, schema_instruction: str
) -> str:
    """组装 model 节点 system prompt：基础十一条 + 业务背景（独立段，紧跟
    十一条之后）+ 信息查证规则（独立段）+ 确认机制（独立段）+ 对账流程
    （独立段）+ 工具枚举表（**唯一一份**：每工具一行 ``- name：description``，
    完整参数 schema 由 tool_args 节点注入；本段必须位于下方「结构化输出说明：」
    之前——末条 human 消息里的 schema 说明以指针句「上方『可用工具』表」指代
    本段）+ 流程索引表（[2026-09-13] 每 skill 一行，恒定注入）+ 已加载规则包
    （policy_keys 逐 key 渲染，仅注册键；每行 = key：title｜准入句｜触发工具（无引用工具时省略该子句））
    + 激活流程正文（[2026-09-13] active_skill 非空时）+ 摘要 + 回复格式指令
    + json_mode 字段说明（「结构化输出说明：」段）。"""
    # [2026-10-10 条件注入] RAG 长期记忆正文（基础首段第 6 条的从句 + 查证规则
    # 末尾四个子块）只在 RAG 已配置时注入。判据与工具剔除**同源**
    # （``rag_client is None``，见 tools/rag_memory.without_unconfigured_rag_tools）：
    # 未配置时工具已被摘掉，若正文还在，模型会被教用一个手里没有的工具→编造。
    rag_configured = runtime.rag_client is not None
    parts: list[str] = [base_system_prompt_for(rag_configured)]

    # [2026-08-26 业务背景] 与项目负责人对齐的业务共识独立段（不属 10.1
    # 十一条；紧跟十一条之后、工具表之前注入）
    parts.append(BUSINESS_CONTEXT)

    # [2026-08-26 信息查证] 模型先自查 ERP 补全，减少追问用户；紧跟业务
    # 背景之后、工具表之前注入
    parts.append(lookup_first_rule_for(rag_configured))

    # [2026-08-27 确认机制] 写工具调用后由系统确认卡承载确认，模型不在
    # 文本中请求用户确认（防双确认）；紧跟信息查证规则之后、工具表之前注入
    parts.append(CONFIRMATION_MECHANISM_RULE)

    # [2026-08-31 对话对账批次 3] 对账流程引导（两段式 + [附件：…] 标记
    # 语义 + fail closed 红线）；紧跟确认机制段之后、工具表之前注入
    parts.append(RECONCILE_WORKFLOW_RULE)

    # [2026-09-16 工具描述去重] 本段是**唯一**一份工具枚举表（每工具一行
    # ``- name：description``）。build_select_schema 不再内联同名表，改为
    # 指针句「tool_name 只能取上方『可用工具』表中的名称」——该句随 schema
    # 说明同时进 system prompt 尾部与末条 human 消息，故**本段必须排在这两处
    # 之前**（本段在 parts 前部、schema 说明在 parts 末尾，相对顺序由本函数
    # 保证；测试 test_decision_round_prompt_index 的 test_tool_table_precedes_schema_section
    # 钉住该位置依赖，改顺序即红）。
    tools = runtime.tools or {}
    if tools:
        lines: list[str] = []
        for spec in tools.values():
            lines.append(f"- {spec.name}：{spec.description}")
        parts.append("可用工具（tool_name 只能从下列枚举表选择）：\n" + "\n".join(lines))
    else:
        parts.append("可用工具：（当前未加载，本轮不调用任何工具）")

    # [2026-09-13 skill 层接入 · reviewer 收口] 流程索引表**不设独立段**。
    #
    # 原先这里另起一段"可用流程"（与工具索引表同构、紧随其后），但索引表正文
    # 同时被 `render_schema_instruction()` 内联——而 schema 说明既拼进本函数
    # 末尾，又被 `ModelDecisionClient._with_schema_instruction` 注入末条 human
    # 消息，于是同一份索引在**同一个决策轮**里出现了 3 次（约 +640 字/轮，且随
    # skill 数量线性膨胀）。三份里 human 消息那份是既有分工（防枚举漂移，工具
    # 字段同款），不动；我加的独立段是纯重复，删掉。
    #
    # 单一来源 = `render_schema_instruction()`：它仍在 system prompt 末尾
    # （本函数最后一行拼 schema_instruction）给出完整枚举与"每轮必须重新声明"
    # 的契约，模型照样"看得到"才能声明；激活的流程正文另见上方"激活流程"段。
    # 注意：删掉这段后 `render_index` 在本模块不再被直接调用，改由 skills.py
    # 的 `render_schema_instruction` 内部调用（勿以为索引表丢了）。

    policy_keys = state.get("policy_keys") or []
    # 只渲染注册键（MEM.lookup 等未注册键跳过——业务规则上下文只能由
    # 系统 policy_resolver 注入，10.2 要求 1）
    registered = [key for key in policy_keys if key in ALL_POLICY_KEYS]
    if registered:
        # [2026-09-16 P2-1 形态 A] 索引行由「裸 key 名」升级为
        # ``key：title｜准入句｜触发工具 X、Y``：决策轮只有此处列已加载规则包，
        # 原渲染只给 key 名（如 ``- R4.query``），模型看不出各包管什么、也就
        # 无从判断该不该触发相应工具。title 取自规则包本身、准入句取自
        # POLICY_ADMISSION、触发工具由 TOOL_REGISTRY 的 policy_family 逆 **并**
        # policy_key_of 静态输出键（POLICY_TOOLS，**代码生成**，不手写常量）——
        # 本段渲染的键来自 state["policy_keys"]（即 policy_key_of 的产物），
        # 只看 policy_family 会把 reconcile_analyze 登记过的
        # R6.reconciliation.plan 说成「无任何工具引用」，与模型刚执行完的动作
        # 直接矛盾。正文仍不在决策轮注入——决策轮在选中 skill 后、参数轮在选中
        # 工具后按 policy_family 注入，本段只作「哪个包管什么、由谁触发」的路标。
        lines: list[str] = []
        for key in registered:
            line = f"- {key}：{POLICY_REGISTRY[key].title}｜{POLICY_ADMISSION[key]}"
            tools = POLICY_TOOLS.get(key, ())
            if tools:
                line += f"｜触发工具 {'、'.join(tools)}"
            # 无工具时**整段省略**该子句——不写「（当前无工具引用）」这类否定
            # 断言：它把"当前未登记"当成事实陈述，与本批新断言直接冲突。并集后
            # 对当前可渲染的键此分支不可达（R5.analysis 无工具、也不会有键），
            # 但保留为 fail-safe。
            lines.append(line)
        parts.append(
            "已加载规则包（标出各包管什么、由哪个工具触发；正文不在本段）：\n"
            + "\n".join(lines)
        )
    else:
        parts.append("已加载规则包：（当前未加载；工具执行后将按结果登记）")

    # [2026-09-13 skill 层接入] 激活流程正文（active_skill 非空时）。
    # 每轮现场渲染、**不写入 state["messages"]**：不累积、不被 checkpoint
    # 持久化、不吃对话 40k 预算；active_skill 被覆盖为 None 即整段消失，
    # 无需任何"用完清除"机制。skill_for 对 None/未注册键返回 None（模型幻觉
    # 的 skill_name 退化为常规对话，不中断本轮）。
    active = skill_for(state.get("active_skill"))
    if active is not None:
        parts.append(_render_skill_body(active))

    # [2026-08-24 定稿] 早期会话历史摘要（压缩节点写入；不占 messages 通道）
    summaries = state.get("summaries") or []
    if summaries:
        parts.append(
            "早期会话历史摘要（已压缩存档；需要细节时可调用 memory_lookup 按段号查询原始会话，或按 query_id 回查工具查询明细）：\n"
            + "\n".join(
                f"【段{s.get('segment_no')}】{s.get('index', '')}\n{s.get('summary', '')}"
                for s in summaries
            )
        )

    # [2026-08-26 回复格式] Markdown 输出引导（不属 10.1 固定条（十条 +
    # 2026-09-10 第 11 条）；前端已用 react-markdown + remark-gfm 渲染终态
    # 回复）
    parts.append(REPLY_FORMAT_INSTRUCTION)

    parts.append(f"结构化输出说明：\n{schema_instruction}")
    return "\n\n".join(parts)


def _msg_chars(message: Any) -> int:
    """消息字符数（content 为 str 时按长度计；异常形态按保守值 400）。"""
    content = getattr(message, "content", "")
    return len(content) if isinstance(content, str) else 400


def _latest_human_index(messages: list[Any]) -> int | None:
    """最新一条 role=human 消息的下标（从后往前；无 human → None）。"""
    for idx in range(len(messages) - 1, -1, -1):
        if getattr(messages[idx], "type", "") == "human":
            return idx
    return None


def _is_detail_message(message: Any) -> bool:
    """只读查询明细 ToolMessage 判定（[2026-09-10 明细隔轮驱逐]）。

    明细标记由 tools 节点非写成功路径写入
    （``ToolMessage.additional_kwargs = {"detail": True, "query_id": …}``，
    仅当结果已存档、可经 memory_lookup 按 query_id 回查时打标）。写工具
    （确认卡/执行结果）与错误 ToolMessage 无标记，不参与驱逐。

    [2026-09-11 attachment_preview] 例外：只读附件预览打 ``{"detail": True}``
    但**不带 query_id**（不进 tool_query_archive，其内容可由模型按 file_id
    重新调用 attachment_preview 幂等取回——「可重取」替代「可存档回查」）。
    本判定只看 detail 布尔，故两类标记同口径驱逐。
    """
    if getattr(message, "type", "") != "tool":
        return False
    kwargs = getattr(message, "additional_kwargs", None) or {}
    return bool(kwargs.get("detail"))


def _filter_stale_details(messages: list[Any]) -> list[Any]:
    """[2026-09-10 明细隔轮驱逐] 过滤「最新 human 之前」的只读查询明细。

    明细（已存档的查询结果 ToolMessage）只在产生它的那一轮对模型可见：
    下一条 human 消息进来后，其之前的明细不再进模型输入。**纯视图层
    过滤**——State/checkpoint/压缩/存档/审计一律原样保留（不物理删除
    消息，AddMessages 合并语义与压缩节点的 RemoveMessage"尾部连续"
    不变式都不受影响）；需要明细时可经 memory_lookup 按 query_id 回查
    或重新查询。最新 human 及其之后的明细（本轮工具循环内）全部保留。

    无 human 消息（图内异常形态）→ 原样返回（保守，不驱逐）。返回新
    列表，不修改入参；调用方在 ``_truncate_history`` 之前调用（先驱逐
    再按预算截断——驱逐的消息不占对话预算）。
    """
    human_idx = _latest_human_index(messages)
    if human_idx is None or human_idx == 0:
        return messages
    kept = [m for m in messages[:human_idx] if not _is_detail_message(m)]
    if len(kept) == human_idx:
        return messages  # 无明细可驱逐：原列表原样返回（避免无谓拷贝）
    return [*kept, *messages[human_idx:]]


def _retain_from_latest_human(
    messages: list[Any], human_idx: int, max_chars: int
) -> list[Any]:
    """[2026-08-28 历史膨胀治理 B] 从最新 human 起保留（其后续全部），仍超
    阈值时在其中从前往后丢（同 drop-front 循环逻辑）；最新 human 永不移除。

    返回：优先 ``messages[human_idx:]``；仍超阈值时丢 human 之后最早的消息
    （从前往后），结果 = [最新 human] + 剩余尾部——用户最新问题永远可见。
    极端场景（human 之后全部丢光仍超阈值，含 human 单条即超 max_chars）：
    保留最新 human 该条原样（宁可单轮略超预算也不丢用户问题，下一轮
    压缩/新一轮对话自会收敛）。
    """
    kept = messages[human_idx:]
    acc = sum(_msg_chars(m) for m in kept)
    if acc <= max_chars:
        return kept
    for i in range(human_idx + 1, len(messages) - 1):
        acc -= _msg_chars(messages[i])
        if acc <= max_chars:
            return [messages[human_idx]] + messages[i + 1:]
    return kept


def _truncate_history(
    messages: list[Any], max_chars: int = MAX_MODEL_INPUT_CHARS
) -> tuple[list[Any], bool]:
    """40k token 预算（约 3.5 字符/token）：总字符超限时 drop-front（保留最近消息）。

    [2026-08-28 历史膨胀治理 B] 截断优先保留**最新一条 role=human 消息**
    （用户最新问题永远可见）：drop-front 结果若不含最新 human（实测：超大
    工具结果顶在前面时首条用户问题被丢 → 模型看不到问题 → 无法回答 →
    恶性循环），改为从最新 human 起保留（``_retain_from_latest_human``）；
    无 human 消息时维持 drop-front 原行为。

    返回 (截断后消息, 是否发生截断)。至少保留最后一条消息；截断结果恒为
    原列表的连续后缀，或「最新 human + 连续尾部」形态（compression_node
    按长度差算 RemoveMessage 依赖"尾部连续"不变式）。
    """
    if not messages:
        return messages, False
    total = sum(_msg_chars(m) for m in messages)
    if total <= max_chars:
        return messages, False
    remaining = list(messages)
    last_human = _latest_human_index(remaining)
    acc = total
    for i in range(len(remaining) - 1):
        acc -= _msg_chars(remaining[i])
        if acc <= max_chars:
            if last_human is None or i + 1 <= last_human:
                # 最新 human 在保留集内：drop-front 原行为不变
                return remaining[i + 1:], True
            # [2026-08-28 治理 B] drop-front 会丢最新 human → 从它起保留
            return _retain_from_latest_human(remaining, last_human, max_chars), True
    # 自然结束：丢至只剩最后一条仍超阈值 → 保最后一条；最后一条非 human
    # 时（最新 human 被丢）→ 治理 B 兜底从最新 human 起保留
    if last_human is not None and last_human != len(remaining) - 1:
        return _retain_from_latest_human(remaining, last_human, max_chars), True
    return remaining[-1:], True


def model_node(state: ConversationState, runtime: RuntimeBag) -> dict[str, Any]:
    """model 节点：读历史（40k 截断）→ 决策 → 追加 AIMessage + pending_action。
    （[2026-08-25 前端改造] call_tool 过渡轮不追加 AIMessage——过渡文案不进
    对话历史，agent_reply 事件带 next_action 标记供前端丢弃过渡气泡）

    fail closed 分支：
    - remaining_steps <= 0（13.3 步数预算耗尽——额度按每条用户消息由
      web 提交入口重置回 REMAINING_STEPS，本条消息内的模型-工具循环
      每轮决策 -1，耗尽只可能来自单条消息内的失控循环）→ 追加耗尽
      文案后 reply + ``agent_error`` 事件（payload 携带 tool_executed，
      文案按 task_id 区分——同 ModelCallError 分支口径）；
    - ModelCallError（未配置/超时/解析失败）→ 追加错误说明 + reply +
      ``agent_error`` 事件（消息不携带敏感信息，14.4）。[2026-08-28 文案
      区分] 本回合已执行写工具（state.task_id 非空，写工具执行时回填）时
      "未执行任何操作"是误导（任务可能已成功落 ERP）——按 task_id 区分文案：
      已执行 → "任务已执行完成，但生成回复时模型暂时不可用…"，未执行 →
      原"模型暂时不可用，未执行任何操作…"；``agent_error`` payload 携带
      ``tool_executed`` 布尔（无敏感信息）供前端同口径区分。
    成功路径：发射 ``agent_reply`` 事件（前端 SSE 展示最终回复）。
    """
    remaining = int(state.get("remaining_steps") or 0)
    if remaining <= 0:
        # [2026-09-09 文案区分] 与 ModelCallError 分支同款 task_id 区分
        # （写工具执行时回填）：本轮已执行写工具时，"请开启新对话"是误导
        # （结果可能已生效，只差结果总结）；且新语义下**同线程**发新消息
        # 即恢复额度（routes_chat._initial_inputs），无需开新会话。payload
        # 携带 tool_executed 布尔供前端同口径区分（无敏感信息，14.4）。
        # 确认卡挂起态（task_id 已建但未执行）不会走到本分支——interrupt
        # 等待用户，无模型调用。
        tool_executed = bool(state.get("task_id"))
        if tool_executed:
            reply_text = (
                "任务已执行完成，但当前消息的执行额度已用完，未能生成结果总结；"
                "请发送新消息继续询问结果。"
            )
        else:
            reply_text = "已达本轮执行上限，请发送新消息继续。"
        _emit(
            runtime,
            EVENT_AGENT_ERROR,
            {"error": "steps_exhausted", "tool_executed": tool_executed},
        )
        return {
            "messages": [AIMessage(content=reply_text)],
            "pending_action": {"next_action": "reply"},
            "current_step": "steps_exhausted",
        }

    # [2026-09-10 明细隔轮驱逐] 模型输入组装：先过滤「最新 human 之前」的
    # 只读查询明细（视图层，State/checkpoint 原文保留），再按 40k 预算
    # drop-front 截断——驱逐不占对话预算，明细需要时经 memory_lookup 回查。
    raw_messages = list(state.get("messages") or [])
    visible = _filter_stale_details(raw_messages)
    messages, truncated = _truncate_history(visible)
    if truncated:
        _emit(runtime, EVENT_AGENT_ERROR, {"error": "history_truncated"})
    # [2026-09-19 可观测性第 2 步] 决策轮输入摘要：把「本轮模型看到了什么段」
    # 落成结构量。落的是**段存在性**（历史条数 / 驱逐明细数 / 摘要段数 /
    # 激活流程），不是 prompt 字节数——后者绝大部分是静态段（近似常量），
    # 回答不了「模型为什么没看到 X」。取值全在既有变量上，零签名改动。
    # active_skill 走 derive 三态（见 stream_projection._enum_or_none）：
    # 它会被 checkpoint 持久化、并在 resume/confirm 时跨版本重放（决策轮那处
    # ALL_SKILL_KEYS 过滤只在决策轮跑、恢复时不重跑）→ 时间维度上可被历史数据污染。
    _emit(
        runtime,
        EVENT_DECISION_INPUT_SUMMARY,
        {
            "history_raw": len(raw_messages),
            "history_sent": len(messages),
            # 驱逐条数，O(1) 差：_filter_stale_details 只删 detail 消息，早退分支
            # 返回同一对象 → 差为 0，无需改其签名（参数轮共用）。
            "evicted_details": len(raw_messages) - len(visible),
            "summaries_count": len(state.get("summaries") or []),
            "active_skill": state.get("active_skill"),
        },
    )
    # 决策轮 schema 说明（system prompt 尾部段与 generate_decision 注入
    # 共用同一文本，避免两份枚举漂移）
    # [2026-09-13 skill 层接入] 流程字段契约追加在字段结构说明之后（无 skill
    # 时 render_schema_instruction 返回空串，原文本字节不变）
    schema_instruction = build_select_schema(
        runtime.tools or {}
    ) + render_schema_instruction()
    system_prompt = _build_system_prompt(runtime, state, schema_instruction)
    # [2026-08-24 流式改造] 每轮模型生成开始：前端开新流式气泡（等待动画）；
    # 增量文本经 on_chunk 逐 token 发射 agent_reply_chunk，终态
    # agent_reply 由下方成功路径原样保留（前端落定替换增量文本）。
    # [2026-08-25 思考流式] 模型思考增量经 on_reasoning_chunk 逐 token 发射
    # agent_reasoning_chunk（前端灰色"正在思考"区逐字渲染；11.4 不落日志、
    # 不持久化——SSE 事件流进程内短时投递，前端展示后不持久化、刷新不重放）。
    _emit(runtime, EVENT_AGENT_REPLY_START, {})
    # on_chunk/on_reasoning_chunk 仅真实 ModelDecisionClient 支持：空桩
    # （_UnconfiguredDecisionClient）签名不含这些参数，且本就 fail closed
    # （ModelUnavailableError），不传保持语义
    stream_kwargs: dict[str, Any] = {}
    if isinstance(runtime.client, ModelDecisionClient):
        stream_kwargs["on_chunk"] = lambda text: _emit(
            runtime, EVENT_AGENT_REPLY_CHUNK, {"chunk": text}
        )
        stream_kwargs["on_reasoning_chunk"] = lambda text: _emit(
            runtime, EVENT_AGENT_REASONING_CHUNK, {"chunk": text}
        )
    try:
        decision = runtime.client.generate_decision(
            system_prompt=system_prompt,
            messages=messages,
            schema_instruction=schema_instruction,
            thread_id=runtime.context.thread_id if runtime.context else None,
            task_id=state.get("task_id"),
            **stream_kwargs,
        )
    except ModelCallError:
        # [2026-08-28 文案区分] 本回合已执行写工具（state.task_id 非空：写
        # 工具执行时回填）时，"未执行任何操作"是误导（任务可能已成功落 ERP）。
        # 区分文案 + agent_error payload 携带 tool_executed 供前端区分；
        # 不携带敏感信息（14.4）。确认卡挂起态（task_id 已建但未执行）不会
        # 走到本分支——interrupt 等待用户，无模型调用。
        tool_executed = bool(state.get("task_id"))
        if tool_executed:
            reply_text = "任务已执行完成，但生成回复时模型暂时不可用。结果可能已生效，可继续询问确认。"
        else:
            reply_text = "模型暂时不可用，未执行任何操作，请稍后重试。"
        _emit(runtime, EVENT_AGENT_ERROR, {"error": "model_unavailable", "tool_executed": tool_executed})
        return {
            "messages": [AIMessage(content=reply_text)],
            "pending_action": {"next_action": "reply"},
            "current_step": "model_failed",
        }
    # [2026-09-13 skill 层接入] 本轮声明的流程。未注册键（模型幻觉）按"不进入
    # 流程"处理（与 skills.skill_for 同口径——skill 是引导不是执行，无可被绕过
    # 的副作用）。**覆盖语义**：每轮成功决策都写一次，声明 null 即清空；失败
    # 分支（steps_exhausted / model_failed）不写——本轮没有新声明，保留原值。
    declared_skill = (
        decision.skill_name if decision.skill_name in ALL_SKILL_KEYS else None
    )
    if decision.next_action == "call_tool":
        # [2026-08-25 前端改造] call_tool 过渡轮：不追加 AIMessage（过渡文案
        # 不进对话历史——历史重放与模型上下文都无噪音）；agent_reply 事件带
        # next_action 标记，前端据此移除流式气泡（loop 中间过程不在对话框
        # 展示，只看 agent 最终回答）。reply_text 字段保留，契约稳定。
        _emit(
            runtime,
            EVENT_AGENT_REPLY,
            {"reply_text": decision.reply_text, "next_action": "call_tool"},
        )
        return {
            "messages": [],
            "pending_action": decision.model_dump(),
            "remaining_steps": remaining - 1,
            "current_step": "model_done",
            "active_skill": declared_skill,
        }
    _emit(runtime, EVENT_AGENT_REPLY, {"reply_text": decision.reply_text})
    return {
        "messages": [AIMessage(content=decision.reply_text)],
        "pending_action": decision.model_dump(),
        "remaining_steps": remaining - 1,
        "current_step": "model_done",
        "active_skill": declared_skill,
    }
