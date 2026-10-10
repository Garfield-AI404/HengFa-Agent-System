/**
 * 对话页文案常量表（2026-08-29 从 ChatPage.tsx 抽出）。
 *
 * 与 types.ts 同批抽出：三张表均为纯数据、零依赖，抽到独立模块后
 * reducer 与展示组件可各自引用，测试里也能直接断言文案分支。
 *
 * [15.14] STREAM_STATUS_LABEL（流状态 → 输入区状态字）已随输入区状态字
 * 删除一并移除（ChatPage 曾用它渲染 statusText，span 已删、无其他引用）。
 */

/** agent_error 事件 error 取值 → 前端文案（图内 fail closed 口径对齐）。 */
export const AGENT_ERROR_TEXT: Record<string, string> = {
  // [2026-08-25 POST 快速返回改造] 路由层安全网（后台图执行异常时由
  // 后台线程追加的终态事件；保证流一定收到终态、输入框恢复）
  chat_run_failed: '对话处理失败，请稍后重试。',
  model_unavailable: '模型暂时不可用，未执行任何操作，请稍后重试。',
  unknown_tool: '无法识别该操作指令，未执行任何写入，请换个说法重试。',
  invalid_arguments: '指令参数无法通过校验，未执行任何写入，请检查后重试。',
  not_implemented: '该操作暂未接入执行链路。',
  shutting_down: '系统正在优雅停机，写入操作暂不可用，请稍后重试。',
  // [2026-09-28 确认卡回执一致性] 补「本次未生成确认卡」一层语义：prepare 失败
  // 时图内不设 pending_confirmation（无卡），而模型在同一回合可能已说出「请点击
  // 确认卡完成写入」（该句在工具报错前生成）——文案必须显式点明卡片未生成，
  // 否则用户会去找一张并不存在的卡。见 reducer 的 revokeOptimisticCardPromise。
  preparation_error: '执行准备未通过（资料缺失或未命中），未执行任何写入，本次未生成确认卡。',
  // [2026-09-09 额度语义变更] remaining_steps 由"线程累计、耗尽须新建对话"
  // 改为"每条用户消息重置 25"：耗尽后发下一条新消息即重置额度，无需开新对话
  steps_exhausted: '已达本轮执行上限，请发送新消息继续。',
  history_truncated: '对话历史过长已截断，早期内容可能不再参与回答。',
  task_state_changed: '任务状态已变化，确认已失效，请刷新后查看。',
}

/** [2026-08-28 文案区分] agent_error 且 tool_executed=true（本回合写工具已
 *  执行落 ERP）时的覆盖文案：任务实际已执行，"未执行任何操作"口径会误导用户。
 *  [2026-09-09 steps_exhausted 同构] 额度耗尽时写工具已执行 → 任务已落 ERP
 *  但结果总结未生成，指引发新消息继续询问结果（与 model_unavailable 同构，
 *  后端耗尽分支事件 payload 同步携带 tool_executed 布尔）。 */
export const AGENT_ERROR_EXECUTED_TEXT: Record<string, string> = {
  model_unavailable: '任务已执行完成，但回复生成时模型暂时不可用，请继续询问以确认结果。',
  steps_exhausted: '任务已执行完成，但当前消息的执行额度已用完，未能生成结果总结；请发送新消息继续询问结果。',
}

/**
 * [2026-09-10 单气泡收敛] 属**非终局**的 agent_error 码：后端发完该事件后
 * 图仍继续（模型节点继续调用模型生成终局回复）。前端收到它时**不得**据此
 * 判定流终态停止轮询（否则真回复被丢弃、本回合变空白，见 useTaskStream
 * :354-357 注释）。
 *
 * - history_truncated：model_node 在 ``_truncate_history`` 判定截断时发射，
 *   随后同一节点仍然 ``EVENT_AGENT_REPLY_START`` + 调用模型 → 终局
 *   ``agent_reply``。该码**不产可见气泡**（纯提示，见
 *   SILENT_NON_TERMINAL_AGENT_ERROR_CODES）。
 * - [2026-09-28] preparation_error：写工具 ``spec.prepare`` 抛错（fail
 *   closed，无卡；tools_node.py ``_write_tool_branch`` 的 ``except`` :586）
 *   时发射——``pending_action``/``pending_confirmation`` 均不设 ⇒
 *   ``route_after_tools`` 走 ``route_to_compression`` ⇒ 图继续到 model，
 *   模型随后生成一条**解释性** ``agent_reply``。该码**必须**渲染错误气泡
 *   （带 detail 与「本次未生成确认卡」），故与 history_truncated 不同——见
 *   SILENT_NON_TERMINAL_AGENT_ERROR_CODES 的说明。
 *
 * ⚠ [2026-09-28 复审修正前提] 本表是**逐码推断**的结果，**不是**「除下表
 * 外其余码一律终局」——旧表述曾写「其余码后端发射后本轮即结束」，它对本仓
 * 一大批码是**假的**。当前**只有** `history_truncated` 与 `preparation_error`
 * 经核实为「图会继续」而列入本表；其余码**目前按终局处理**，但已核实其中
 * 若干（`unknown_tool` / `invalid_arguments` / `not_implemented` /
 * `execution_error` / `write_retry_suppressed` / `scope_denied` /
 * `shutting_down` / `harness_misconfigured` / `task_creation_error` /
 * `queue_cancelled` / `queue_wait_timeout` / `argument_generation_error` /
 * `tool_name_mismatch`）的发射点返回值均为 `pending_action: None` 且**不设**
 * `pending_confirmation`，故图**其实会继续**回到 model 再产 `agent_reply`
 * （后端 `tools_node.py:264` 注释原文即「执行异常：不终止循环，模型下轮生成
 * 失败说明」）。把它们按终局处理属**已知缺口**（前端提前停轮询会丢掉那份
 * 模型解释回复），**另立批次**逐码收口——本批次不扩大范围、不改行为。
 * __理想方案是后端在 payload 显式携带「终局/非终局」标志__，届时上表与
 * 这张「已知缺口」清单都可删（见交付报告「需要后端配合」）。
 */
export const NON_TERMINAL_AGENT_ERROR_CODES: ReadonlySet<string> = new Set([
  'history_truncated',
  'preparation_error',
])

/**
 * [2026-09-28 确认卡回执一致性] NON_TERMINAL_AGENT_ERROR_CODES 的**子集**：
 * 非终局且**不产生任何可见代码气泡**的码（reducer 收到即原样返回 state）。
 *
 * 当前仅 ``history_truncated``：它只是「历史被截断」的提示，模型随后立即
 * 给出真正答复，插代码气泡会与模型回复形成两条（违反单气泡收敛）。
 *
 * ``preparation_error`` **不在**此表：它虽同属非终局（流须继续等模型解释
 * 回复），但携可操作 detail 与「本次未生成确认卡」，必须由 reducer 渲染成
 * 错误气泡，否则用户只看到模型的解释、看不到失败原因。
 */
export const SILENT_NON_TERMINAL_AGENT_ERROR_CODES: ReadonlySet<string> = new Set([
  'history_truncated',
])

/**
 * [2026-09-28 确认卡回执一致性] 属**「写入准备失败」类**的 agent_error 码：
 * 写工具在构造确认卡之前失败（fail closed，**不设 ``pending_confirmation``**，
 * 即无卡），但模型在**同一回合的决策轮**（工具尚未运行）可能已经说出
 * 「写入计划如下……请点击确认卡完成写入」——那句乐观文案当时无从知道后面会
 * 失败。
 *
 * 码表来源（`src/administrative_agent/agent/nodes/tools_node.py`）。
 *
 * ⚠ [2026-09-28 复审修正判据] 旧判据曾写作「纳入 = 发射点在 ``if spec.is_write``
 * 写工具分支内」——下表**已不满足**该判据（``invalid_arguments`` 与
 * ``not_implemented`` 的发射点读写共用，见下）。现判据改为：
 *  (a) 该码一旦发生在**写工具**上，必然意味着本次未生成确认卡（fail
 *  closed，不设 ``pending_confirmation``）；(b) 读工具路径不产生「请点击
 *  确认卡」承诺，故即便该码同由读工具触发、被本表卷入撤销，误伤面也可忽略。
 * 逐码发射点（含读写共用/第二发射点的破例说明）：
 * - ``preparation_error``：``except Exception``（:586）包住 ``spec.prepare``
 *   （``_emit`` :596），**写工具分支内**，准备阶段失败；
 * - ``shutting_down``：优雅停机（:544）**写工具分支内**；另 :783 为排队等待
 *   中停机取消，同属写分支；
 * - ``harness_misconfigured``：任务/审计/MCP 依赖未接线（:557），**写工具
 *   分支内**；
 * - ``not_implemented``：**两个发射点**——:577 写工具未接 ``spec.prepare``
 *   （``if spec.is_write`` 门**内**）；:258 非写工具（Phase 1 路径）``spec.run``
 *   抛 ``NotImplementedError``（门**外**，读/写共用）。属**破例**：形态与
 *   ``invalid_arguments`` 相同。行为后果良性（读工具失败那轮本就没卡，追加
 *   作废说明不会说谎），故按判据 (a)(b) 一并纳入**不改**；
 * - ``invalid_arguments``：``spec.args_schema.model_validate`` 异常分支内
 *   **两个发射点**——:228 非超限（普通校验失败，事件**不带** ``detail``）；
 *   :234 超限（``batch_overflow_message`` 非空，事件 payload 另带 ``detail``，
 *   取值 ``batch_overflow_user_detail(exc)``，见 :238）。两处均在
 *   ``if spec.is_write``（:246）**之前**，读/写工具共用。**用户
 *   2026-09-28 裁定纳入**，理由即其批准意图是「全部无卡码」——写工具上的
 *   同一码必然无卡，而模型在同一回合决策轮可能已说过「请点击确认卡」；读
 *   工具路径不会产生卡承诺（判据 (b)），故误伤可忽略。此项为**显式破例**，
 *   与 ``not_implemented`` 的第二发射点同一种形态。
 *
 * 触发本表的事件到达时，reducer 会撤销本回合内「请点击确认卡」的乐观文案
 * （revokeOptimisticCardPromise）；``preparation_error`` 的文案另含「本次未
 * 生成确认卡」（AGENT_ERROR_TEXT.preparation_error）。
 *
 * 与 NON_TERMINAL_AGENT_ERROR_CODES 是**正交**两轴：本表管「有没有卡」，
 * 后者管「图是否继续」。本批次**同时**把 ``preparation_error`` 纳入后者
 * （其图确实继续到 model 生成解释回复），故两轴各按自身口径独立演进、互不
 * 推导（上一批次曾声明「不改后者口径」，本批次已改，特此更正）。
 */
export const PREPARE_FAILURE_AGENT_ERROR_CODES: ReadonlySet<string> = new Set([
  'preparation_error',
  'shutting_down',
  'harness_misconfigured',
  'not_implemented',
  'invalid_arguments',
])

/**
 * [2026-09-28 确认卡回执一致性] 撤销乐观确认卡文案时**就地追加**到该条正文
 * 尾部的可见作废说明（不删除原消息——用户已经看到过的内容不得凭空消失）。
 */
export const CARD_NOT_GENERATED_NOTICE =
  '（本条作废）上句提到的确认卡并未生成，界面中没有可点击的确认卡。'

/**
 * [2026-09-28 确认卡回执一致性] 判定一条助手正文是否**向用户承诺/引导点击
 * 确认卡**（撤销判定的依据）。只认三类形态：
 * - 祈使式承诺「请点击<≤4字>确认卡」——``请`` 紧邻 ``点击``，故
 *   「请勿点击确认卡……」**不**命中（``请`` 后是 ``勿``）；
 * - 行首/句读后的祈使「点击<≤4字>确认卡」——``点击`` 前须是边界
 *   （句首或 ``。！？，；：``/空白），故「未点击确认卡」**不**命中
 *   （``点击`` 前是实词 ``未``）；
 * - 「可点击的确认卡」——断言存在可点击的卡。
 * 不含对确认卡的普通陈述/既成事实（如「已通过确认卡写入」「确认卡核对
 * 无误」「上一步的确认卡完成了写入」）。
 *
 * 首条测试表见 tests/unit/chat/cardPromise.test.ts（命中/不命中各若干条）。
 * 第三条分支是必需的：``CARD_NOT_GENERATED_NOTICE`` 自身含「可点击的确认
 * 卡」，须自命中——它虽在 reducer 里靠**字面量 ``includes`` 先判**保证幂等
 * （reducer.ts:228），这条分支是把该不变量也钉在模式层（测试同钉）。
 * 〔**顺序不要动**：``includes(CARD_NOT_GENERATED_NOTICE)`` 的幂等先判必须在
 * 模式匹配之前，删掉会让 NOTICE 被反复追加。〕
 *
 * ⚠ [2026-09-28 复审] 第三条分支（``可点击的确认卡``）**正确性依赖一个前提**：
 * **本轮不存在已落定的成功文案**。反例（均命中）：「写入成功，请查看上方可
 * 点击的确认卡。」「该卡已失效，成为不可点击的确认卡。」（后者的
 * ``不可点击的确认卡`` **含**该子串）——若这类「正常成功出卡」正文被扫描到，
 * 会被错误盖上「（本条作废）……并未生成」，**那是一句假话**。
 * 当前该前提**成立**（复审已核）：撤销只在 ``PREPARE_FAILURE_AGENT_ERROR_CODES``
 * 命中时调用，而一个用户回合内最多一次写工具调用（``tools_node`` 每次只处理
 * 一个 ``pending_action``；设了 ``pending_confirmation`` 即 ``interrupt`` 挂起），
 * 本轮内唯一的助手正文是 ``agent_reply{next_action:call_tool}`` 过渡文案、被
 * reducer 整条丢弃 ⇒ 扫描面上不存在「已落定的成功文案」。
 * **将来若取消 call_tool 过渡轮的丢弃、或让同一轮出现两条已落定正文，这一支
 * 就会开始说假话**，届时必须一并重审（收紧第三支或改由其调用点保证前提）。
 */
export const CARD_PROMISE_PATTERN =
  /请点击[^。！？\n]{0,4}确认卡|(?:^|[。！？\n，,；;：: ])点击[^。！？\n]{0,4}确认卡|可点击的确认卡/

/**
 * [2026-09-10 单气泡收敛] tool_result(ok=false) 的**代码兜底文案**。
 *
 * 拒绝/执行失败到达时前端无法预知本轮最终以模型回复还是终局错误收尾，
 * 故只记录兜底（reducer 的 turnFallback），不立即插气泡：
 * - 本轮最终收到终局 agent_reply → 丢弃兜底，只留模型回复；
 * - 本轮最终以终局 agent_error 收尾 → 渲染兜底（而非 agent_error 文案：
 *   拒绝/失败时并未执行写入，``tool_executed=true`` 的"任务已执行完成"
 *   口径会误导用户）。
 */
export const TOOL_RESULT_REJECTED_TEXT = '已拒绝确认，任务已取消（未执行任何写入）。'

/** tool_result(ok=false，非拒绝) 的代码兜底文案（带工具名可选）。 */
export function toolResultFailedText(toolName: string): string {
  return toolName ? `工具 ${toolName} 执行失败，未写入 ERP。` : '执行失败，未写入 ERP。'
}

/**
 * [2026-09-13 流异常收尾] task_cancelled（generation_cancelled 通用原因）与
 * 客户端侧流终止（用户点「暂停」/ 会话过期）共用的取消文案。
 *
 * 抽为常量：`stream_aborted`（用户点暂停，见 ChatPage 流终态落点）与
 * `task_cancelled` 取同一句话，避免两处字面量漂移。
 */
export const GENERATION_CANCELLED_TEXT = '已取消当前生成。'

/**
 * [2026-09-13 流异常收尾] 客户端侧流错误（非 2xx 响应 / 流故障）且本轮仍有
 * 未落定的流式气泡时补的可见文案。复用路由层安全网 `chat_run_failed` 的同
 * 一句话（"对话处理失败，请稍后重试。"）——不新造用户可见文案。
 */
export const STREAM_ABORTED_ERROR_TEXT = AGENT_ERROR_TEXT.chat_run_failed
