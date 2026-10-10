"""tools 节点：按 pending_action 分派注册表工具。

由 agent/graph.py 拆分后位于 nodes/tools_node.py——工具
分派与写工具分支（scope → 停机 → 任务 → 锁等待 → prepare → 挂起确认）。

**写工具分支（Phase 2）**：
1. scope 校验（8.4/10.5：manager 角色 + 2.4 最小 scope，服务端注入）；
2. 优雅停机检查（15.3 步骤 7 / D25：``lifecycle.accepting_new_writes()``）；
3. 任务服务缺失 → harness_misconfigured（fail closed）；
4. ``_ensure_task``：state.task_id 复用（13.2）或新建写任务
   （NewTaskSpec：thread_id/actor/role/duty_code/原始消息/结构化意图/
   is_write=True——D6 自动取串行锁）；
5. ``_wait_for_execution_right``：pending 排队（8.3 第 3 条）时
   100ms 轮询任务状态（30s 上限 + 停机检查），晋升 running 后继续；
6. ``spec.prepare``：只读前置 + 确认卡构造（失败 → mark_failed +
   错误 ToolMessage，fail closed 不落 ERP）；
7. 确认（``spec.require_confirmation`` 为真）
   ``request_confirmation(task_id, payload_hash)`` → 任务
   waiting_confirmation + ``pending_confirmation``（确认卡六要素 +
   payload_hash + 执行上下文 execution）写入 State 并 checkpoint，
   路由到 confirm 节点。

当前注册表已无任何免确认工具，``_run_without_confirmation`` 分支不再
被真实工具触发（保留供未来复用）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from administrative_agent import lifecycle
from administrative_agent.agent.configuration import (
    RuntimeBag,
    _emit,
    _mark_audit_write_failed,
    _new_tool_call_id,
    _tool_ctx,
)
from administrative_agent.agent.state import ConversationState
from administrative_agent.agent.tools.base import (
    PreparedToolWrite,
    ToolResultSummary,
    ToolSpec,
    batch_overflow_message,
    batch_overflow_user_detail,
)
from administrative_agent.agent.tools.crop import crop_items
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.records import NewTaskSpec
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_ERROR,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_RUNNING,
)

logger = structlog.get_logger(__name__)

#: 串行锁等待轮询间隔（秒）。
WRITE_QUEUE_WAIT_INTERVAL: float = 0.1
#: 串行锁等待上限（秒；超时取消排队任务，不无限等待）。
WRITE_QUEUE_WAIT_TIMEOUT: float = 30.0

#: 同一写操作连续失败重试上限（[2026-08-31 修复确认卡重复循环]：写工具执行
#: 失败/确认失效后模型不得自动重试同一写操作——连续失败达上限即拦截自动
#: 重试，须向用户说明现状、由用户重新发起，避免「新任务→新确认卡→反复
#: 点击确认」的循环）。
WRITE_RETRY_LIMIT: int = 2

#: 工具结果兜底截断上限（字符；仅防御分支——非 JSON / 非 dict 载荷与
#: 摘要器异常降级。正常 JSON 明细结果不再有字符预算）。
#: [2026-09-10 明细全量改版，用户拍板] 原「历史摘要 4096 / 明细预算
#: 16384（TOOL_QUERY_DETAIL_CHARS）+ 超预算紧凑索引」两级分层整体删除：
#: 明细 ToolMessage 当轮全量进模型上下文，隔轮由 model/tool_args 节点
#: 视图层驱逐（model_node._filter_stale_details）——State/checkpoint/
#: 压缩/存档/审计路径均不受影响。
TOOL_RESULT_HISTORY_CHARS: int = 4096

#: 全量明细尾部提示（存档成功时；模型据此可经 memory_lookup 按 query_id
#: 查回原始全量数据）。query_id 经 payload 顶层 "query_id" 键给出。
_ARCHIVED_NOTE_FULL = "完整原始数据已存档，需要时可查询（memory_lookup，参数 query_id）"

#: 明细标记（隔轮驱逐）适用的工具名——[2026-09-10 P3-31] 只对明细源
#: （erp_query / memory_lookup / list_entities）打 detail 标记；erp_calc
#: 的聚合结果不打（聚合值仅几百字符，驱逐收益低且会让「总量可核对 /
#: normalized divisor」跨轮丢失）。[2026-09-10 复核补] list_entities 的
#: 目录结果（全量紧凑目录约 12KB）必须纳入——它与 erp_query 同为可存档
#: 明细源，此前收窄遗漏导致目录常驻其后每一轮请求（回归），今补回。
#: 写工具/错误结果同样不打。
DETAIL_TOOL_NAMES: frozenset[str] = frozenset(
    {"erp_query", "memory_lookup", "list_entities"}
)

#: 「正文即产物」工具：结果里的大字段必须原样外发（skill 要求模型原样输出），
#: 无法像明细那样自带预算信封——故进历史时**不做字符预算**，否则会被
#: 4096 兜底从中间腰斩成非法 JSON（见 summarize_tool_result 的 items 缺省分支）。
#: 与 DETAIL_TOOL_NAMES 同款：按工具名白名单，不按键名（memory_lookup 也有
#: "text" 键，但那是分页正文，必须继续受预算约束）。
VERBATIM_TEXT_TOOL_NAMES: frozenset[str] = frozenset(
    {"render_financial_report", "render_tax_worksheet"}
)


def fallback_truncate_result(serialized: str, tool_name: str | None) -> str:
    """摘要器异常时的兜底裁剪决策（**防御性 fail-safe，非主路径**）。

    [2026-09-14 补] 与主路径同款白名单：命中 ``VERBATIM_TEXT_TOOL_NAMES``
    的「正文即产物」工具（render_financial_report / render_tax_worksheet）
    **不截断**——它的固定
    Markdown 正文被 4096 腰斩即非法 JSON + 断行 Markdown（与
    ``summarize_tool_result`` items 缺省分支修的是同一类静默损坏，只是触发
    路径更罕见）；其余工具维持既有 4096 截断（既有行为不变）。

    触发条件（**近乎不可达**，切勿误当主路径）：
    ``summarize_tool_result`` 内部 ``json.loads`` 已用 ``except ValueError``
    兜住非法 JSON→走 ``content[:max_chars]`` 返回；本兜底只在其**以外**的
    异常才触发——即解析成功走到 ``json.dumps(dict, ensure_ascii=False)``
    时抛异常（如 dict 含不可序列化对象），或未来摘要器新增逻辑偶发异常。
    调用方传入的是 ``json.dumps(summary, ensure_ascii=False)`` 的结果
    （summary 为纯 str/float/dict/list 载荷时 json.dumps 不抛），故对当前
    注册表中的工具本分支实际到不了——它只是保证摘要器一旦意外抛异常时不
    穿透让图节点崩溃的兜底护栏。
    """
    if tool_name in VERBATIM_TEXT_TOOL_NAMES:
        return serialized
    return serialized[:TOOL_RESULT_HISTORY_CHARS]


def summarize_tool_result(
    content: str,
    max_chars: int = TOOL_RESULT_HISTORY_CHARS,
    *,
    query_id: str | None = None,
    tool_name: str | None = None,
) -> str:
    """工具结果进上下文前的裁剪与存档标注（[2026-09-10 明细全量改版]）。

    处理顺序：
    1. 解析 JSON；非 JSON / 非 dict / 无 items → 防御性按 max_chars
       截断（正常调用方恒传 JSON 结果，本分支仅兜底）。例外：
       ``tool_name`` 命中 ``VERBATIM_TEXT_TOOL_NAMES``（「正文即产物」工具）
       且载荷为 dict 时，无 items 分支**不做字符预算**、原样外发——
       否则 4096 兜底会把固定 Markdown 正文从中间腰斩成非法 JSON；
    2. 有 items 时先按实体白名单裁剪（crop_items，未登记实体原样）；
    3. ``query_id`` 非空（原始数据已存档 tool_query_archive）→ 附
       archived/query_id/archived_note 提示（模型可经 memory_lookup 按
       query_id 查回原始全量明细）；
    4. **明细不再有字符预算**：裁剪后全量进上下文。原「≤16KB 全量 /
       超预算紧凑索引」两级分层与 detail_chars 预算参数已整体删除；
       上下文治理改由隔轮驱逐承担——明细 ToolMessage 在产生它的那一轮
       可见，下一条 human 消息进来后由 model/tool_args 节点在模型输入
       组装处过滤（State/checkpoint 原文保留，见
       ``model_node._filter_stale_details``）。

    仅作用于写入对话历史/checkpoint 的 ToolMessage；其余路径（流内事件、
    审计日志等）不受本摘要影响。
    """
    try:
        data = json.loads(content)
    except ValueError:
        return content[:max_chars]
    if not isinstance(data, dict):
        return content[:max_chars]
    items = data.get("items")
    if not isinstance(items, list):
        serialized = json.dumps(data, ensure_ascii=False)
        # 「正文即产物」工具（如 render_financial_report）豁免字符预算：
        # 正文被 skill 要求原样输出，腰斩即非法 JSON / 断行 Markdown。
        # 仅按工具名白名单（不改按键名——memory_lookup 的 "text" 是分页正文）。
        if tool_name in VERBATIM_TEXT_TOOL_NAMES:
            return serialized
        if len(serialized) <= max_chars:
            return serialized
        return content[:max_chars]
    meta = {key: value for key, value in data.items() if key != "items"}
    entity = data.get("entity")
    cropped = crop_items(items, entity) if entity else items
    if query_id is not None:
        return json.dumps(
            dict(meta, items=cropped, archived=True, query_id=query_id,
                 archived_note=_ARCHIVED_NOTE_FULL),
            ensure_ascii=False,
        )
    return json.dumps(dict(meta, items=cropped), ensure_ascii=False)


def _archive_read_tool(
    runtime: RuntimeBag,
    *,
    spec: ToolSpec,
    tool_name: str,
    tool_call_id: str,
    arguments: Any,
    ok: bool,
    summary: dict[str, Any],
) -> bool:
    """把一次只读工具调用落一行 tool_query_archive（[2026-10-06 §3.8]）。

    返回「是否成功落库」。本函数被 tools_node 的**三条只读路径**调用——
    正常返回（成功/0 行/ok=False 结果）、执行抛异常、参数校验失败；这三条
    路径下**只读工具（``spec.is_write=False``）都会落一行**。**不落行的分支
    必须显式列出**（勿把上面读成「任何只读调用都落一行」）：
    - 未知工具（``spec is None``，tools_node 约 :255）：无 ToolSpec，连
      ``is_write`` 都无从判定，不落；
    - ``NotImplementedError``（工具未接线，约 :319-329）：该分支直接返回
      错误 ToolMessage，不经本函数，不落；
    - 写工具（``spec.is_write=True``）与 archive 未接线（``runtime.archive
      is None``）：本函数内部排除，返回 False；
    - 参数校验失败时入参非 dict（``args_raw`` 非 dict，调用方已折成 ``{}``）
      → 仍落一行（arguments={}）。

    落库内容：
    - ``arguments``：本次调用**模型传的入参**（调用方传校验后 pydantic 模型
      或参数校验失败时的原始 dict；本函数在下方 try 内归一化为
      ``model_dump(mode="json")`` / 原样 dict）——仅供取证回放，**不进模型
      上下文**；
    - ``ok``：本次成败；
    - ``entity``：结果的 entity 标记；无 entity 的读工具
      （memory_lookup/export_data/render_* 等）以**工具名**作标记（该值仅
      供 crop 依据；这些工具不挂 query_id，回查路径不可达）；
    - ``count``/``items``：结果明细条数与原始全量（无 items 的读工具为空）。

    入参归一化（``BaseModel → model_dump(mode="json")``）**刻意放在本函数
    既有的 try/except 内**（[2026-10-06 §3.8 P2]）：调用方传对象本身，序列化
    若抛异常即与本函数其余写入失败同款降级（记日志返回 False），**不穿透
    tools_node**（该节点契约：任何失败都返回错误 ToolMessage、循环不终止）。
    写入失败不阻断：try/except 记日志（与旧行为同款降级——无存档，上下文
    走摘要路径），返回 False；调用方据返回值决定是否把 query_id 挂给模型。
    """
    if spec.is_write or runtime.archive is None:
        return False
    items = summary.get("items")
    entity = summary.get("entity")
    try:
        # 归一化入参：dict 原样、否则按 pydantic 模型序列化（见 docstring
        # P2 说明——放 try 内，序列化异常不外泄到节点层）。
        if isinstance(arguments, dict):
            payload = arguments
        else:
            payload = arguments.model_dump(mode="json")
        runtime.archive.append_tool_query(
            thread_id=runtime.context.thread_id if runtime.context else "",
            query_id=tool_call_id,
            entity=entity if isinstance(entity, str) and entity else tool_name,
            count=len(items) if isinstance(items, list) else 0,
            items=items if isinstance(items, list) else [],
            arguments=payload,
            ok=ok,
        )
    except Exception:
        logger.warning(
            "tool_query_archive_failed",
            tool_name=tool_name,
            query_id=tool_call_id,
        )  # 降级：无存档，上下文走摘要路径
        return False
    return True


def tools_node(state: ConversationState, runtime: RuntimeBag) -> dict[str, Any]:
    """tools 节点：按 pending_action 分派注册表工具。

    任何失败都返回错误 ToolMessage（模型下轮自纠，循环不终止），并
    经 sink 发 ``agent_error`` 事件；非写工具直接 run（Phase 1 路径）；
    写工具走 ``_write_tool_branch``（Phase 2 interrupt 确认流）。
    """
    pending = state.get("pending_action") or {}
    tool_name = pending.get("tool_name")
    spec = (runtime.tools or {}).get(tool_name) if tool_name else None

    if spec is None:
        content = json.dumps(
            {"ok": False, "error": f"未知工具 {tool_name!r}："
             "不执行任何写入（模型幻觉工具名不落 ERP，安全网拦截）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "unknown_tool"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_unknown",
        }

    args_raw = pending.get("tool_arguments") or {}
    try:
        args = spec.args_schema.model_validate(args_raw)
    except Exception as exc:  # pydantic 校验失败：模型幻觉参数不落 ERP（安全网）
        # [超限收口] items 超过单卡上限：把裸英文 too_long 换成含「上限/未写入/
        # 分批」的中文结构化文案，并在错误事件上增设 detail 键（供前端气泡展示）；
        # 其余校验失败逐字保持原行为（不加 detail、正文仍是原样的 pydantic 说明）。
        overflow = batch_overflow_message(exc)
        if overflow is None:
            content = json.dumps(
                {"ok": False, "error": f"工具参数解析失败（确定性校验）：{exc}"},
                ensure_ascii=False,
            )
            _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "invalid_arguments"})
        else:
            content = json.dumps(
                {"ok": False, "error": f"工具参数解析失败（确定性校验）：{overflow}"},
                ensure_ascii=False,
            )
            _emit(runtime, EVENT_AGENT_ERROR, {
                "tool_name": tool_name,
                "error": "invalid_arguments",
                # 面向用户的短文案（第二人称）；模型面完整文案见 content 里的 overflow
                "detail": batch_overflow_user_detail(exc),
            })
        # [2026-10-06 §3.8] 参数校验失败的**读**工具调用同样落一行
        # （arguments=模型原始入参）——与「查了但报错」同类，供回放
        # 「它当时传了什么参数被拒」；写工具由 _archive_read_tool 内部排除。
        tool_call_id = _new_tool_call_id()
        _archive_read_tool(
            runtime,
            spec=spec,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=args_raw if isinstance(args_raw, dict) else {},
            ok=False,
            summary={},
        )
        return {
            "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
            "pending_action": None,
            "current_step": "tool_invalid_args",
        }

    if spec.is_write:
        return _write_tool_branch(state, runtime, spec, tool_name, args, args_raw)

    # ---- 非写工具（Phase 1 路径：直接 run，无 interrupt） ----
    ctx = _tool_ctx(runtime)
    try:
        result = spec.run(ctx, args)
    except NotImplementedError:
        content = json.dumps(
            {"ok": False, "error": f"工具 {tool_name} 尚未接线（Phase 2 落地）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "not_implemented"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_not_implemented",
        }
    except Exception as exc:  # 执行异常：不终止循环，模型下轮生成失败说明
        tool_call_id = _new_tool_call_id()
        # [2026-10-06 §3.8] 执行抛异常的**读**工具调用也落一行（ok=False）：
        # 「它查了但报错了」同样要能回放本次入参（如 MCP 网络错误）。
        _archive_read_tool(
            runtime,
            spec=spec,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=args,
            ok=False,
            summary={},
        )
        content = json.dumps(
            {"ok": False, "error": f"工具执行失败：{exc}"}, ensure_ascii=False
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "execution_error"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
            "pending_action": None,
            "current_step": "tool_failed",
        }

    if isinstance(result, ToolResultSummary):
        summary = {"ok": result.ok, **result.summary}
    else:
        summary = {"ok": bool(result)}
    # [2026-09-10 明细全量改版] 只读查询结果（ERP JSON，含 items 明细）：
    # 按实体白名单裁剪后**全量**进对话历史与 checkpoint（16KB 明细预算/
    # 紧凑索引已删）——当轮模型可读完整明细；下一条 human 消息进来后由
    # 模型输入视图层驱逐（model_node._filter_stale_details，State/
    # checkpoint 原文保留）。
    # [2026-08-28 两级数据访问 2a][2026-10-06 §3.8 扩展] 结果写历史前存档
    # （tool_query_archive，11.4 合规：仅会话内可查、随会话删除清理——
    # routes_chat.delete_thread 已联动 ArchiveStore.delete_thread 覆盖新表）。
    # 旧行为「仅成功 + 非空 items + 存档标识才存档」已放宽：**正常返回
    # （含 0 行成功 / ok=False）+ 执行抛异常 + 参数校验失败**三条只读路径都
    # 落一行；不落行的分支（未知工具 / NotImplementedError / 写工具 /
    # archive 未接线）见 _archive_read_tool docstring 的显式枚举。落库内容与
    # 降级语义见该函数。写失败不阻断：try/except 记日志，降级为无存档
    # （无 query_id 可回查，明细不做隔轮驱逐）。
    tool_call_id = _new_tool_call_id()
    query_id: str | None = None
    items = summary.get("items")
    entity = summary.get("entity")
    archived = _archive_read_tool(
        runtime,
        spec=spec,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args,
        ok=result.ok,
        summary=summary,
    )
    # [硬约束：模型可见行为一字不变] 挂给模型的 query_id（可回查标识）**仍
    # 仅在「成功 + 非空 items + 存档标识成立且落库成功」时给出**——0 行/
    # 失败虽已落库，但**不**因此挂 query_id（下方 detail 标记条件逐字未动）。
    if (
        archived
        and result.ok
        and isinstance(items, list)
        and items
        and isinstance(entity, str)
    ):
        query_id = tool_call_id
    # [2026-08-28 D3 修复] 摘要器调用包 try/except：异常（不应发生）降级为
    # 原样截断（TOOL_RESULT_HISTORY_CHARS），写日志不阻断（与节点其他分支
    # 同款 fail-safe——摘要失败不能穿透让图节点崩溃）。
    # [2026-09-14 补] 兜底分支同样遵守 VERBATIM_TEXT_TOOL_NAMES 白名单：否则
    # 「正文即产物」工具（render_financial_report）的固定 Markdown 正文会被
    # 4096 从中间腰斩成非法 JSON（与 summarize_tool_result items 缺省分支
    # 修的是同一类静默损坏，只是触发路径更罕见——见 fallback_truncate_result
    # 注释：本分支对当前工具近乎不可达，纯防御性）。
    try:
        content = summarize_tool_result(
            json.dumps(summary, ensure_ascii=False),
            query_id=query_id,
            tool_name=tool_name,
        )
    except Exception as exc:
        logger.warning("tool_result_summarize_failed", tool_name=tool_name, error=str(exc))
        content = fallback_truncate_result(
            json.dumps(summary, ensure_ascii=False), tool_name
        )
    # [2026-09-10 明细隔轮驱逐] 明细标记：**仅明细源**（DETAIL_TOOL_NAMES：
    # erp_query / memory_lookup / list_entities）的已存档结果（载荷含非空
    # items 且有存档标识可回查）
    # 打 additional_kwargs {"detail": True, "query_id": …}——model/tool_args
    # 节点组装模型输入时据此过滤「最新 human 之前」的明细（当轮可见、隔轮
    # 驱逐；State/checkpoint 原文保留，不物理删除）。erp_calc 的聚合结果
    # 不打标记（[2026-09-10 P3-31] 聚合值仅几百字符，驱逐收益低且会让
    # 「总量可核对 / normalized divisor」跨轮丢失）；写工具/错误
    # ToolMessage 同样无标记，不参与驱逐（list_entities 目录结果属明细源，
    # 已纳入 DETAIL_TOOL_NAMES 打标记——[2026-09-10 复核补]）。
    # memory_lookup 的 query_id 路径：
    # 结果本身即来自 tool_query_archive（回查等于拿到原始明细），存档标识
    # 取 summary 回显的 query_id（**不是**本次调用自身新落的取证行——[2026-
    # 10-06 §3.8] 起读工具调用本身也落一行，但该行无 entity 标记、不挂
    # query_id，仅供取证）。
    detail_query_id: str | None = None
    if tool_name in DETAIL_TOOL_NAMES:
        detail_query_id = query_id
        if detail_query_id is None and tool_name == "memory_lookup":
            echoed = summary.get("query_id")
            if isinstance(echoed, str) and echoed:
                detail_query_id = echoed
    detail_kwargs: dict[str, Any] = {}
    if (
        detail_query_id is not None
        and result.ok
        and isinstance(items, list)
        and items
    ):
        detail_kwargs = {"detail": True, "query_id": detail_query_id}
    # [2026-09-11 attachment_preview 隔轮驱逐] 只读附件预览结果打 detail 标记：
    # 预览仅在其后紧邻的决策轮有用（看清附件 → 调 reconcile_analyze），隔轮
    # 无价值；结果有界约 4000 字符但多轮累积仍占对话预算。预览**没有
    # query_id**（其内容可由模型按 file_id 重新调用本工具幂等取回，无需存档
    # 回查），故只打 detail、不带 query_id——与明细源（erp_query 等存档可
    # 回查）的区别在此；两者同经 model/tool_args 节点的
    # _filter_stale_details 视图层驱逐，State/checkpoint 原文保留。
    # [2026-10-06 §3.8] 注：预览调用本身仍会落一行 tool_query_archive 取证行
    # （arguments/ok），但该行不挂 query_id、不参与回查——与上面的 detail
    # 标记（无 query_id）口径一致。
    if tool_name == "attachment_preview" and result.ok and not detail_kwargs:
        detail_kwargs = {"detail": True}
    out: dict[str, Any] = {
        "messages": [
            ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
                additional_kwargs=detail_kwargs,
            )
        ],
        "pending_action": None,
        "current_step": "tool_done",
    }
    # policy_keys 追加（非 reducer 字段：整表返回）
    policy_key = spec.policy_key_of(args_raw)
    out["policy_keys"] = list(state.get("policy_keys") or []) + [policy_key]
    # 写工具执行产生任务时回填 State.task_id
    task_id = summary.get("task_id")
    if task_id:
        out["task_id"] = task_id
    return out


def _retry_key(tool_name: str, payload: dict) -> str:
    """写操作抑制键（[2026-08-31 修复确认卡重复循环]）。

    同时接受两种形态——tools 侧参数（args_raw，模型参数原样）与 confirm
    侧载荷（execution，确认卡执行上下文）；对同一调用必须产出同一键，
    确认侧失败计数（tool_failed/ok=False）与拦截侧（prepare 失败）共用。

    [2026-09-12 一张卡多条明细] 批量形状（payload 顶层含 items 列表；
    order_create / order_update / finance_create / finance_update 四个写
    工具）：**注意 args_raw 里没有 item_id**（item_id 形如
    ``metal:{customer_id}:{model}:{in_date}``，customer_id 是 prepare 名称
    匹配之后才知道的），故不能照原设计用 items[].item_id 摘要——那在两种
    形态下取不到同一组值。改为按 items 长度分派，**两种形态必然同键**：
    - 单元素 items（len == 1）：prepare 委托旧单条执行器（口径 4：1 条走
      旧样式），execution 退化为无 items 的旧形状、只剩 draft/payload 等
      顶层键——**解包 items[0] 后复用下方旧分支**，与 execution 形态（无
      items）产出同一键（如 order_create:<客户名>、order_update:<订单号>）；
    - 多元素 items（len >= 2）：args_raw 与 execution **均**为批量形状
      （execution 顶层同样带 items 列表），直接降级为 ``{tool}:batch``
      ——整个工具的批量写共用一个失败计数维度。粒度虽粗，但方向是
      「多抑制」（不同批次共享同一计数上限，最坏是提前提示用户手动重发），
      绝非「漏抑制」（绝不会放过同一批次的重复循环）；计数器用途本就是
      「抑制确认卡重复循环 / 重试上限判定」，多抑制即安全方向。

    旧形状（无 items，含在飞旧卡）**逐字保留**下方全部分支：
    - order_update / reconciliation_execute：order_id / report_id（两
      形态顶层均有）；
    - order_create：customer_name / supplier_name（args 顶层；execution
      内嵌在 draft，兜底取嵌套字段）；
    - attendance_plan_execute：attendance_date（两形态顶层均有）；
    - mapping_template：create_draft 以 template_key 为目标维度，
      publish/retire 以 version_id（args 顶层无 template_key、执行侧回填
      record 的 template_key——按 action 分维度保证两形态产出同一键）；
    - finance_create：[2026-09-02 R7] tx_type:category:tx_date 串联
      （tools 侧 args_raw 顶层与 confirm 侧 execution 顶层均镜像同名键，
      见 tools/finance.py prepare 的 execution 构造）；同一天同一分类同一
      类型视为同一写目标（金额不同不拆分——连续失败计数维度，防误撞）；
    - finance_update：id（两形态顶层均有）；
    - 无任何标识 → tool_name 本身（该工具整体计数，不误撞其他目标）。
    """
    items = payload.get("items")
    if isinstance(items, list) and items:
        if len(items) == 1 and isinstance(items[0], dict):
            # 单元素 items：prepare 委托旧单条执行器 → execution 无 items、
            # 只有 draft/payload 等顶层键。解包 items[0] 复用旧分支，保证
            # args_raw（items 单元素）与 execution（无 items）产出同一键。
            return _retry_key(tool_name, items[0])
        # 多元素 items：args_raw 与 execution 顶层均为批量形状 → 统一工具级
        # 批量键（同键由构造保证，不依赖 item 内字段逐项对齐）。
        return f"{tool_name}:batch"
    if tool_name == "mapping_template":
        action = payload.get("action")
        if action == "create_draft":
            value = payload.get("template_key")
        else:
            value = payload.get("version_id")
        if value is not None and str(value) != "":
            return f"{tool_name}:{action}:{value}"
        return tool_name
    if tool_name == "finance_create":
        parts = [
            str(payload.get("tx_type") or ""),
            str(payload.get("category") or ""),
            str(payload.get("tx_date") or ""),
        ]
        if all(parts):
            return f"{tool_name}:{':'.join(parts)}"
        return tool_name
    if tool_name == "finance_update":
        value = payload.get("id")
        if value is not None and str(value) != "":
            return f"{tool_name}:{value}"
        return tool_name
    for field in (
        "order_id",
        "report_id",
        "customer_name",
        "supplier_name",
        "attendance_date",
    ):
        value = payload.get(field)
        if value is not None and str(value) != "":
            return f"{tool_name}:{value}"
    if tool_name == "order_create":
        # confirm 侧 execution 把客户/供货商嵌套在 draft 内（args 顶层已有
        # 同名键走不到这里；嵌套兜底保证两形态产出同一键）
        draft = payload.get("draft") or {}
        for field in ("customer_name", "supplier_name"):
            value = draft.get(field)
            if value is not None and str(value) != "":
                return f"{tool_name}:{value}"
    return tool_name


def _bump_retry(counts: dict[str, int], key: str) -> dict[str, int]:
    """重试计数 +1（复制语义：write_retry_counts 为覆盖字段，整表返回）。"""
    out = dict(counts)
    out[key] = out.get(key, 0) + 1
    return out


def _write_tool_branch(
    state: ConversationState,
    runtime: RuntimeBag,
    spec: ToolSpec,
    tool_name: str,
    args: Any,
    args_raw: dict[str, Any],
) -> dict[str, Any]:
    """写工具分支（Phase 2）：scope → 停机 → 任务 → 锁等待 → prepare → 挂起。"""
    # 0. 写操作连续失败重试抑制（[2026-08-31 修复确认卡重复循环]）：同一写
    #    操作连续失败达上限后拦截模型自动重试——失败原因须向用户说明，
    #    由用户重新发起，不再自动生成新任务/新确认卡
    counts = state.get("write_retry_counts") or {}
    key = _retry_key(tool_name, args_raw)
    if counts.get(key, 0) >= WRITE_RETRY_LIMIT:
        content = json.dumps(
            {"ok": False, "error": f"写操作 {tool_name} 已连续失败 {counts[key]} 次，"
             "已停止自动重试；请向用户说明现状，由用户重新发起"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {
            "tool_name": tool_name, "error": "write_retry_suppressed",
        })
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "write_retry_suppressed",
        }
    # 1. scope 校验（8.4/10.5：角色与 scope 服务端注入，不信任模型/前端）
    context = runtime.context
    if context is None or context.user_role != "manager" or not (
        context.scope and all(s in context.scope for s in spec.required_scopes)
    ):
        content = json.dumps(
            {"ok": False, "error": f"权限不足：写操作 {tool_name} 需要 manager 角色"
             f"与 scope {sorted(spec.required_scopes)}（8.4/10.5），已拒绝执行"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "scope_denied"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "scope_denied",
        }
    # 2. 优雅停机（15.3 步骤 7 / D25）：停机中不接收新写任务
    if not lifecycle.accepting_new_writes():
        content = json.dumps(
            {"ok": False, "error": "系统正在优雅停机，暂不接受新写任务，"
             "请稍后重试（15.3 步骤 7 / D25）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "shutting_down"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "shutting_down",
        }
    # 3. 任务服务缺失 → harness 配置错误（fail closed）
    if runtime.tasks is None or runtime.audit is None or runtime.mcp_client is None:
        content = json.dumps(
            {"ok": False, "error": f"harness 未接线任务/审计/MCP 依赖，不能执行写操作 "
             f"{tool_name}（fail closed）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "harness_misconfigured"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "harness_misconfigured",
        }
    # 4. 任务确保（13.2：同任务复用；否则新建写任务）
    task_id, task_error = _ensure_task(state, runtime, spec, tool_name, args_raw)
    if task_error is not None:
        return task_error
    # 5. 串行锁等待（8.3 第 3 条：pending 排队 → 轮询晋升；超时/停机取消）
    wait_error = _wait_for_execution_right(state, runtime, task_id, tool_name)
    if wait_error is not None:
        return wait_error
    # 6. prepare（只读前置 + 确认卡；失败 fail closed 不落 ERP）
    if spec.prepare is None:
        content = json.dumps(
            {"ok": False, "error": f"工具 {tool_name} 尚未接线（Phase 2 落地）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "not_implemented"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_not_implemented",
        }
    ctx = _tool_ctx(runtime, state=state)
    try:
        prepared = spec.prepare(ctx, args)
    except Exception as exc:
        detail = str(exc)
        _mark_prepare_failed(runtime, task_id, tool_name, detail)
        content = json.dumps(
            {"ok": False, "error": f"工具准备失败（已取消本次写入，fail closed）：{detail}"},
            ensure_ascii=False,
        )
        # [15.14] preparation_error 带 detail（str(exc)，如分类归属校验失败
        # 附可用清单）——前端 agent_error 分支把 detail 拼进用户可见正文，
        # 否则只见通用文案「执行准备未通过」看不到具体原因
        _emit(runtime, EVENT_AGENT_ERROR, {
            "tool_name": tool_name,
            "error": "preparation_error",
            "detail": detail,
        })
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "tool_prepare_failed",
            # 写操作执行失败 → 连续失败计数 +1（达上限后拦截自动重试）
            "write_retry_counts": _bump_retry(counts, key),
        }
    # 7. 确认（[2026-08-30 撤回考勤免确认]：全部写工具 require_confirmation
    #    均为 True，含考勤 R1——原 2026-08-28 让考勤免确认直写，因"免确认 ×
    #    全员覆盖"叠加风险已撤回。考勤现在与 R2/R3/R6 走同一确认分支，
    #    确认卡/payload_hash/审计路径本身未变）
    if spec.require_confirmation:
        runtime.tasks.request_confirmation(task_id, payload_hash=prepared.payload_hash)
        # [2026-10-05 确认卡改进第 3 批] 整卡级「用户原话对照」：把最近一条
        # 用户原话全文挂到 pending_confirmation.card 上（source_quote），供
        # 用户在确认前比对原话与卡面（治静默改值/漏字段/落错单）。**单一
        # 集中处**——不经各 prepare_*；无原话（空串）时不落键 → 旧卡 JSON
        # 逐字节不变；纯展示级，绝不进 payload_hash（已由 prepare 算好）。
        card = prepared.card
        if ctx.last_user_message:
            card = {**card, "source_quote": ctx.last_user_message}
        return {
            "task_id": task_id,
            "pending_action": None,
            "pending_confirmation": {
                "confirm_type": "tool_write",
                "task_id": task_id,
                "thread_id": context.thread_id,
                "tool_name": tool_name,
                "card": card,
                "payload_hash": prepared.payload_hash,
                "execution": prepared.execution,
                "policy_key": prepared.policy_key,
            },
            "current_step": "tool_prepared",
            # write_retry_counts 覆盖语义：原样透传（与 policy_keys 整表返回
            # 同模式），防止并发轮未返回时计数被覆盖丢失
            "write_retry_counts": counts,
        }
    return _run_without_confirmation(state, runtime, spec, tool_name, task_id, prepared)


def _run_without_confirmation(
    state: ConversationState,
    runtime: RuntimeBag,
    spec: ToolSpec,
    tool_name: str,
    task_id: str,
    prepared: PreparedToolWrite,
) -> dict[str, Any]:
    """[2026-08-28 考勤免确认] 直接执行路径（对应 confirm_node approved 分支
    的执行部分；任务已在 running 无需 confirm 迁移；审计/串行锁路径与确认
    路径一致——prepare 已完成后任务处于 running，跳过确认卡直接 spec.run）。

    [2026-08-30 死路径] 撤回考勤免确认后，当前注册表中**没有任何工具**会走
    到本函数（全部写工具 require_confirmation 均为 True，见 base.py:95）；
    保留供未来复用，目前仅由 tests/unit/test_audit_failure_marking.py 直测
    （它覆盖的是本函数内的审计失败标记分支，不依赖注册表调用到它）。
    """
    _emit(runtime, EVENT_TOOL_RUNNING, {"task_id": task_id, "tool_name": tool_name})
    ctx = _tool_ctx(runtime)
    try:
        result = spec.run(ctx, args=None, task_id=task_id, execution=prepared.execution)
    except Exception as exc:
        # 审计落库失败（AuditFailureError）时补任务级标记；其余异常原样处理
        _mark_audit_write_failed(runtime, task_id, exc)
        content = json.dumps(
            {"ok": False, "tool": tool_name, "task_id": task_id,
             "error": f"工具执行失败：{exc}"}, ensure_ascii=False)
        _emit(runtime, EVENT_TOOL_RESULT, {
            "task_id": task_id, "tool_name": tool_name, "ok": False,
        })
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "tool_failed",
        }
    summary = {"ok": result.ok, **result.summary}
    _emit(runtime, EVENT_TOOL_RESULT, {
        "task_id": task_id, "tool_name": tool_name, "ok": result.ok,
        "summary": result.summary,
    })
    out: dict[str, Any] = {
        "messages": [
            ToolMessage(
                content=json.dumps(summary, ensure_ascii=False),
                tool_call_id=_new_tool_call_id(),
            )
        ],
        "pending_action": None,
        "pending_confirmation": None,
        "current_step": "tool_done",
    }
    out["policy_keys"] = list(state.get("policy_keys") or []) + [prepared.policy_key]
    task_id_from_summary = result.summary.get("task_id")
    if task_id_from_summary:
        out["task_id"] = task_id_from_summary
    return out


def _mark_prepare_failed(runtime: RuntimeBag, task_id: str, tool_name: str, detail: str) -> None:
    """准备失败：任务标记 failed 释放串行锁（fail closed 兜底）。"""
    try:
        runtime.tasks.mark_failed(
            task_id,
            error_code="preparation_failed",
            error_summary=f"{tool_name} 确认前准备失败：{detail}",
        )
    except Exception:  # 状态已变（如外部已取消）：记录失败不阻塞返回
        pass


def _ensure_task(
    state: ConversationState,
    runtime: RuntimeBag,
    spec: ToolSpec,
    tool_name: str,
    args_raw: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """写任务确保：state.task_id 存在且 running → 复用（13.2）；否则新建。

    返回 (task_id, error_dict)。error_dict 非 None 时为错误 ToolMessage
    返回体（建任务失败，fail closed）。
    """
    existing = state.get("task_id")
    if existing:
        record = runtime.tasks.get_task(existing)
        if record is not None and record.status == TaskStatus.RUNNING:
            return existing, None
    thread_id = runtime.context.thread_id if runtime.context else ""
    try:
        record = runtime.tasks.create_task(
            NewTaskSpec(
                thread_id=thread_id,
                actor_account=runtime.context.user_id,
                actor_role=runtime.context.user_role,
                duty_code=spec.duty_code,
                original_user_message=_last_human_content(state),
                structured_intent={"tool": tool_name, "arguments": args_raw},
                is_write=True,
            )
        )
    except Exception as exc:
        content = json.dumps(
            {"ok": False, "error": f"写任务创建失败（fail closed）：{exc}"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "task_creation_error"})
        return None, {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "current_step": "task_creation_failed",
        }
    return record.task_id, None


def _last_human_content(state: ConversationState) -> str:
    """最后一条 human 消息文本（审计 original_user_message 用，8.2）。"""
    for message in reversed(list(state.get("messages") or [])):
        if getattr(message, "type", "") == "human":
            content = getattr(message, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def _wait_for_execution_right(
    state: ConversationState,
    runtime: RuntimeBag,
    task_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """串行锁阻塞等待（8.3 第 3 条）：pending 排队 → 100ms 轮询晋升。

    - 任务晋升 running（队列头释放）→ 返回 None 继续；
    - 停机（15.3 步骤 7）→ 取消排队任务 + 错误 ToolMessage；
    - 30s 超时 → 取消排队任务 + 错误 ToolMessage（不无限等待，fail closed）。
    """
    record = runtime.tasks.get_task(task_id)
    if record is None or record.status != TaskStatus.PENDING:
        return None  # 已获执行权（running）或状态已推进：直接继续
    deadline = time.monotonic() + WRITE_QUEUE_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if not lifecycle.accepting_new_writes():
            _cancel_queued(runtime, task_id, "shutdown_during_queue_wait")
            content = json.dumps(
                {"ok": False, "error": "系统正在优雅停机，排队中的写任务已取消"
                 "（15.3 步骤 7 / D25）"},
                ensure_ascii=False,
            )
            _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "shutting_down"})
            return {
                "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
                "pending_action": None,
                "current_step": "shutting_down",
            }
        time.sleep(WRITE_QUEUE_WAIT_INTERVAL)
        record = runtime.tasks.get_task(task_id)
        if record is None:
            return None
        if record.status == TaskStatus.RUNNING:
            return None  # 晋升：执行权已获得
        if record.status != TaskStatus.PENDING:
            content = json.dumps(
                {"ok": False, "error": f"写任务在排队期间被终止"
                 f"（status={record.status.value}），未执行写入"},
                ensure_ascii=False,
            )
            _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "queue_cancelled"})
            return {
                "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
                "pending_action": None,
                "current_step": "tool_queued_cancelled",
            }
    _cancel_queued(runtime, task_id, "queue_wait_timeout")
    content = json.dumps(
        {"ok": False, "error": f"等待写执行权超时（{WRITE_QUEUE_WAIT_TIMEOUT:g} 秒，串行域"
         f"被其他任务占用），排队任务已取消，请稍后重试（8.3 第 3 条）"},
        ensure_ascii=False,
    )
    _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "queue_wait_timeout"})
    return {
        "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
        "pending_action": None,
        "current_step": "queue_wait_timeout",
    }


def _cancel_queued(runtime: RuntimeBag, task_id: str, reason: str) -> None:
    """取消排队任务（PENDING → CANCELLED 是合法迁移；失败不阻塞）。"""
    try:
        runtime.tasks.cancel(task_id, reason=reason)
    except Exception:  # 状态已变：记录失败不阻塞返回
        pass
