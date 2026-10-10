"""真实模型客户端（跟踪项"真实模型接入"第一批落地：模型生成回复）。

- 新增 ``AgentDecision``（五字段结构化决策）+ ``ModelDecisionClient``：
  会话循环图 model 节点使用——``generate_decision(system_prompt, messages)``
  接收**任意长度**对话历史（LangChain 消息对象或 ("system"/"human"/"ai",
  content) 元组），json_mode 结构化输出 AgentDecision；
- 动态 schema 指令 + 工具参数生成：决策轮字段说明
  由固定常量改为 ``build_select_schema``
  （工具枚举表由 model_node 的「可用工具」段随注册表动态生成；本函数
  只出字段结构 + 指向该表的指针句）；参数轮新增 ``ToolArguments`` +
  ``generate_tool_arguments``（tool_args 节点第二轮调用，注入该工具完整
  参数 schema 与规则包正文）；
- 统一封装 langchain_openai.ChatOpenAI（deepseek 兼容 OpenAI 协议）：
  base_url / model / api_key 全部来自 Settings（15.2；key 只进 .env，
  绝不写入源码）；
- 结构化输出（pydantic v2，``with_structured_output(..., method="json_mode")``）：
  * 实测约束（deepseek 兼容层）：默认 method 的 json_schema
    response_format 不受支持（400 "response_format type unavailable"），
    function_calling 的 tool_choice 在思考模式下亦不受支持——因此用
    json_mode（response_format=json_object），且 prompt 必须含 "json"
    字样与字段结构说明（``build_select_schema``/``build_args_schema``
    注入 user 内容）；
- 决策轮 ``generate_decision`` 由 invoke 改为
  ``chat.stream`` 逐 token 流式：每 chunk 经 ``on_chunk`` 回调把增量文本
  交给 model 节点（发射 agent_reply_chunk SSE 事件，前端逐字渲染），
  流结束后整体 json 解析（json.loads + AgentDecision.model_validate，
  语义与 with_structured_output 的 json_mode 一致）；工具参数
  （``generate_tool_arguments``）/ 压缩摘要（``generate_summary``）两轮
  保持 invoke 不动（内部输出不对用户展示）；
- fail closed 纪律：
  * model_api_key 为空（或 model_base_url 为空——防止 key 泄漏到默认
    端点）→ ``ModelUnavailableError``，不发起任何调用；
  * 调用异常（超时/HTTP/解析失败/结构化输出解析失败）统一归类为
    ``ModelCallError``，上层按失败处理（13.1 稳定错误码）；异常消息
    不携带 key / base_url 等敏感信息（14.4 口径）；
- 决策轮新增 ``_coerce_decision`` 中间件
  （``generate_decision`` 解析路径 model_validate 前调用）：只吸收零
  语义损失的畸形（reply_text 缺失/None/空白串时按 next_action 补过渡
  文案），涉及决策语义的畸形绝不编造——fail closed 语义不变；参数轮
  （``generate_tool_arguments``）无零语义损失修复点，不挂中间件；
  ``build_select_schema``/``build_args_schema`` 提示强化必填字符串字段
  （禁止 null）；
- 决策轮流式改裸 httpx SSE 解析（LangChain
  兼容层丢弃 ``reasoning_content``——chunk 无任何字段暴露该增量）；
  思考增量经 ``on_reasoning_chunk`` 回调透传（SSE 事件流投递，11.4
  不落日志不持久化）；json 结构化解析（``_coerce_decision`` +
  ``AgentDecision.model_validate``）链路原样保留，reasoning 不参与
  json 解析；
- ``generate_decision`` / ``generate_tool_arguments``
  调用前把当前日期行（``_current_date_line``：服务器本地时间）追加到
  system_prompt——模型知识截止时间 ≠ 当前时间，注入后模型可正确解析
  「7月」「上周」等相对时间语义；压缩摘要（``generate_summary``）不注入。
- ``generate_decision`` /
  ``generate_tool_arguments`` 重试循环由 for(attempt in
  range(max_retries+1)) 改为 while True + 双预算：瞬时故障重试
  （``_should_retry_model_call``，预算 settings.model_call_max_retries=2）
  与语法级 JSON 解析重试（``_JSON_PARSE_MAX_RETRIES=5``，总尝试 6 次）互相
  独立、各自用尽才 fail closed——修复此前 JSON 解析重试的 continue 跳回外层
  for 迭代（仅 max_retries+1 次），总尝试被锁死为 3 次、JSON 重试预算从未
  生效的结构 bug（实测 attempt=4/5 缺失）；全部既有语义（指数退避 / JSON
  重试 sleep 1s / suppress_stream / 墙钟 420s / 空输出与语义级畸形 fail
  closed / attempt 日志口径）不变。
- 模型调用解析失败的**脱敏诊断特征**（2026-09-09
  定稿口径，2026-09-10 03:33:33Z 生产事故触发实施：tool_path=decision、
  error_type=ValidationError，当时日志分辨不了「max_tokens 截断」与
  「模型畸形输出」）：
  * 决策轮流式消费循环捕获最后一个 chunk 的 ``finish_reason``
    （length=输出被 max_tokens 截断 / stop 等=模型侧畸形），重试与最终
    失败日志附带；参数轮为 langchain invoke 无流式 chunk，finish_reason
    不可得（省略该字段）；
  * 语法级解析失败（JSONDecodeError，含 langchain OutputParserException
    包装）最终失败日志附 ``output_bytes`` / ``ends_with_brace`` /
    ``output_head`` / ``output_tail``（首尾各 20 字符——定稿允许的脱敏
    边界，仅此路径）；重试日志只附 finish_reason（不重复 head/tail）；
  * 语义级解析失败（pydantic ValidationError）最终失败日志附
    ``validation_issues``（``err.errors()`` 的 loc+type 摘要，如
    ``reply_text:missing``，绝不带 input 值）+ ``parsed_keys``（已解析
    JSON 顶层键名集合，非内容）——**不记原文**；
  * langchain 侧把 ValidationError 包成 OutputParserException（根因在
    ``__cause__``），日志特征按 ``__cause__`` 解包区分两类（解包只用于
    日志，重试/失败归类不变）；
  * **行为零变化**：重试策略 / fail-closed / 异常类型 / 用户文案全部
    不变，只加日志字段（值为 None 的字段不落，避免噪音）；脱敏纪律
    11.4 不变（不记原始输出、key、base_url、ERP 业务数据）。
- 决策轮模型返回合法 JSON 但顶层非对象（``[]`` /
  ``"text"`` / ``null`` / ``123`` / ``true``）时，``_coerce_decision``
  原 ``data.get`` 抛 ``AttributeError``——不在调用方
  ``except (ValueError, TypeError)`` 内、非 ModelCallError，穿透
  model_node 的 ``except ModelCallError``（用户见「对话处理失败」而非
  「模型暂时不可用」，且无 model_call_failed 特征日志；reviewer 探针 E
  复现）。现首行加类型守卫（非 dict 原样返回）→
  ``AgentDecision.model_validate`` 抛 ValidationError → 既有 except
  捕获 → 优雅 ModelCallError（文案/异常类型与其他语义级畸形一致）+
  可观测性特征（``validation_issues=["model_type"]``）。这是本修复的
  预期行为变化；重试策略 / fail-closed / 其余语义零变化。
- 本模块不感知 HTTP 与任务状态机（调用方为 routes_chat / 图节点等编排层）。
"""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
import structlog
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from administrative_agent.config import Settings, get_settings
from administrative_agent.agent.tools.base import ToolSpec

logger = structlog.get_logger(__name__)


def _current_date_line() -> str:
    """当前日期行（2026-08-26 修订）：模型知识截止时间 ≠ 当前时间——
    每次模型调用时取服务器本地时间注入，让模型能解析「7月」「上周」
    等相对时间语义；决策轮与参数轮共用。"""
    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    return f"当前日期：{now:%Y-%m-%d}（星期{weekday}）"


def build_select_schema(tools: dict[str, ToolSpec]) -> str:
    """决策轮（model 节点）json_mode 结构化输出说明——动态生成。

    由原决策轮固定 schema 常量演化而来（该常量已删除）：字段
    结构与 next_action 三值枚举保持原措辞。**不再内联工具枚举表**
    （原逐行 ``name（description）`` 表已删除）：本函数文本既拼进
    system prompt 尾部，又被 ``_with_schema_instruction`` 原样注入末条
    human 消息，同一份 ``name（description）`` 表因此在同一决策轮出现
    两次；而真正的工具枚举表由 ``model_node._build_system_prompt`` 的
    「可用工具」段**一次性**给出（每工具一行 ``- name：description``，
    位置在「结构化输出说明：」**之前**）。故此处的 ``tool_name`` 约束改为
    **指针句**：指向「上方『可用工具』表」，不得臆造工具名。
    ``tools`` 为空时仍明确说明本轮不调用工具、不臆造枚举。tool_arguments
    已从决策轮移除——参数由 tool_args 节点第二轮生成
    （``build_args_schema``）。含 "json" 字样满足 deepseek json_mode 约束。
    """
    if tools:
        tool_line = "tool_name 只能取上方『可用工具』表中的名称，不得臆造其他工具名"
    else:
        tool_line = "（当前未加载任何工具，本轮不调用工具）"
    return (
        '请只输出一个 JSON 对象（不要输出任何其他内容），字段结构：'
        '{"reply_text": "回复正文（必填非空字符串，禁止 null 或空串）", "next_action": "reply 或 call_tool 或 needs_input", '
        '"tool_name": "工具名（仅 next_action=call_tool 时填写，否则为 null）", '
        '"questions": ["追问问题"]（仅 next_action=needs_input 时填写，否则为 null）}。'
        "字段合法值："
        'next_action 只能是 "reply"（直接回复，不调用工具）/"call_tool"（调用工具）'
        '/"needs_input"（仅在 ERP 中查不到且确需用户提供时才追问，questions 必填；'
        '凡能通过 erp_query 查到的信息——客户/供货商名称→id、订单归属、型号'
        '是否存在、订单/记录是否存在、订单类型等——必须先查再决定，禁止直接'
        '追问用户）；'
        f"{tool_line}"
    )


def _args_schema_json(spec: ToolSpec) -> str:
    """参数 schema 的 JSON（**剔除顶层 description**）——两个调用点统一入口。

    Pydantic v2 会把 args_schema 类的 docstring 自动复制进
    ``model_json_schema()`` 的**顶层** ``description`` 键；而
    ``build_args_schema`` 与 tool_args_node 的工具卡又各自 append 了一遍
    ``args_schema.__doc__``。同一段 docstring 因此在提示词里出现两份——
    本函数剔除 schema 内那份，**只保留代码里 append 的那份**（方案 A）。

    选 A（删 schema 内、保留 append）的理由：
    - append 那份有真换行、带「参数说明：」标签，可读性优于 schema 内
      被 ``json.dumps`` 转义成字面 ``\\n`` 的副本；
    - 两者**只能删一个**：都删掉等于把 docstring 从提示词里彻底移除，会
      丢业务口径（如 order_create 的「metal/powder 二选一、同时提供 fail
      close」），故 append 必须在。

    **只剔顶层 description**，绝不触碰字段级/嵌套级 description——参数的
    ``Field(description=...)`` 是模型填充参数的必读说明（见
    tests/unit/test_financial_report.py 对 ``_DynamicBlock.rows`` 字段说明
    的守护）。仅当 docstring 非空时才 pop（此时顶层 description 必来自
    docstring）；``ensure_ascii=False`` 保证中文原样不转义。
    """
    schema = spec.args_schema.model_json_schema()
    if getattr(spec.args_schema, "__doc__", None):
        schema.pop("description", None)
    return json.dumps(schema, ensure_ascii=False)


def build_args_schema(spec: ToolSpec) -> str:
    """参数轮（tool_args 节点）json_mode 结构化输出说明——动态生成。

    字段结构 {"tool_name": "…", "tool_arguments": {…}}；tool_name 必须
    等于本轮待调用工具名；tool_arguments 键名必须来自该工具参数结构
    （``_args_schema_json`` 注入，键名漂移风险降为 0）；args_schema 类
    docstring 非空时追加类说明（业务口径，如 order_create 的 metal/powder
    字段互斥说明）。含 "json" 字样满足 deepseek json_mode 约束。
    """
    schema_text = _args_schema_json(spec)
    text = (
        '请只输出一个 JSON 对象（不要输出任何其他内容），字段结构：'
        '{"tool_name": "工具名（必填非空字符串，禁止 null 或空串）", "tool_arguments": {"参数键名": 值}}。'
        f"tool_name 必须等于 {spec.name}（本轮已选定待调用的工具，不得改名）；"
        f"tool_arguments 键名必须来自该工具的参数结构（JSON Schema）：\n{schema_text}"
    )
    doc = getattr(spec.args_schema, "__doc__", None)
    if doc:
        text += f"\n参数说明：{doc}"
    return text


#: 记忆压缩调用 system prompt（2026-08-24 定稿）。{summary_max_chars} 为占位符，
#: 调用时 replace 注入（prompt 内含 JSON 字面花括号，不能用 str.format）。
#: 含 "json" 字样满足 deepseek json_mode 约束（与 build_select_schema 同纪律）。
#:
#: 保留项按**优先级排序**而非平铺：summary 在压缩节点被硬切成
#: summary_max_chars（`compression_node.py` 的 `[: settings.summary_max_chars]`），
#: 不排序则空间不足时模型随机裁，可能裁掉用户诉求、留下流水账——故第 1、2 类
#: 明示不得舍弃，其余从后往前丢。
#: 「不得臆造」是负向约束：摘要是模型自由文本，会把不确定的数字写成确定的，而后续
#: 轮次只能当事实用（base prompt 禁止凭记忆补写），错误一旦固化便看不出处。
#: index 要求带 query_id：明细原文隔轮被删（`model_node.py` 的 `_filter_stale_details`），
#: 回查唯一路径是 memory_lookup(query_id)；而 `model_node.py` 回注摘要时已承诺
#: 「按段号/query_id 回查」，不收集该标识即等于承诺无法兑现。**段号由回注处
#: 自动加前缀**（`f"【段{segment_no}】{index}"`），故 index 自身不得再写段号。
COMPRESSION_PROMPT = (
    "请把下面的对话历史压缩成一段摘要，只输出一个 JSON 对象，字段结构："
    '{"summary": "摘要正文", "index": "目录条目"}。'
    "summary 为中文摘要，不超过 {summary_max_chars} 字符。按下列优先级保留，"
    "空间不足时从后往前舍弃，但第 1、2 类不得舍弃："
    "1. 用户的明确要求与偏好（保留原话要点），以及用户已否决或纠正过的做法；"
    "2. 待办与未收口事项：谁在等什么、已答应做什么、下一步该做什么；"
    "3. 已查证的口径与映射：客户/供货商名称与 id 的对应、单价与计价口径、"
    "型号归属（钣金/塑粉）、字段口径（如 in_date 含义）；"
    "4. 已确认/已执行的操作与结果（订单/考勤/对账/收支的任务状态）；"
    "5. 关键业务事实与数值（订单/考勤/对账/收支），注明出自哪一段，供判断时效。"
    "index 为一行目录条目、不得超过 100 字（每轮随摘要回注进上下文，超长即长期"
    "占用预算），格式：时间段｜主题词列表｜工具查询标识；"
    "不要自行写「【段N】」前缀（段号由系统自动添加）。上文若出现带 query_id 的工具"
    "查询结果（archived_note 提示的存档标识），必须把该 query_id 原样写进本行——"
    "后续轮次靠它回查明细，漏写即无法回查。"
    "硬性约束：只写上文确实出现过的事实与数值，不推断、不补全、不改写数字与单位；"
    "上文没有的一律不写，宁可留空；把握不准的写「未确认」，不得写成已确认。"
)


class SummaryResult(BaseModel):
    """[2026-08-24 定稿] 记忆压缩结构化输出（summary + index 两字段）。

    - summary：中文摘要正文（上限由 settings.summary_max_chars 约束）；
    - index：一行目录条目（【段N】时间段｜主题词列表，检索定位用）。
    """

    summary: str
    index: str


class ModelCallError(RuntimeError):
    """模型链路失败基类（fail closed：模型不可用/调用失败——不执行、不误报成功）。"""


class ModelUnavailableError(ModelCallError):
    """模型未配置（无 API key / 未指定 base_url）：不发起调用即失败。"""


class _ModelHttpError(RuntimeError):
    """[2026-08-28 瞬时故障重试] 裸 httpx 路径 HTTP 非 200 的内部信号。

    替代原 ``raise RuntimeError(f"HTTP {response.status_code}")``：携带
    状态码与 Retry-After 秒数，供重试判定（``_should_retry_model_call``）
    与脱敏日志使用；对外仍统一转 ModelCallError，上层契约不变。
    """

    def __init__(self, status: int, retry_after_sec: int | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after_sec = retry_after_sec


def _parse_retry_after(raw: str | None) -> int | None:
    """Retry-After 头解析（秒数或 HTTP 日期；解析失败 → None 走指数退避）。

    httpx.Headers.get 大小写不敏感，传入已取出的原始值即可。日期格式
    （RFC 7231）防御性支持：转秒差，过去时间取 0（立即重试）。
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0, int(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def _http_status_of(exc: Exception) -> int | None:
    """从异常提取 HTTP 状态码（脱敏日志字段）。

    - 裸 httpx 路径：``_ModelHttpError.status``；
    - langchain 侧（invoke）：openai.APIStatusError.status_code
      （BadRequestError 400 / AuthenticationError 401 / RateLimitError
      429 / InternalServerError 5xx 等）；
    - 连接类/解析类等无状态码 → None（日志里为 null）。
    """
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    return None


def _extract_retry_after(exc: Exception) -> int | None:
    """429 时服务端 Retry-After 秒数；读不到 → None（退避按指数 1<<attempt）。

    - 裸 httpx 路径：``_ModelHttpError.retry_after_sec``（抛出时已解析）；
    - langchain 侧 openai.RateLimitError：从 ``exc.response.headers`` 现取
      （openai SDK 未把 Retry-After 暴露为异常属性）。
    """
    if isinstance(exc, _ModelHttpError):
        return exc.retry_after_sec
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        try:
            raw = headers.get("Retry-After")
        except Exception:
            return None
    return _parse_retry_after(raw)


#: [2026-08-28 JSON 解析有限重试，2026-09-01 次数上调] 语法级 JSON 解析
#: 失败额外重试次数（额外重试 5 次、总尝试 6 次以内，项目负责人批准）：
#: deepseek API 偶发 HTTP 200 但内容非 JSON（JSONDecodeError）——模型侧
#: 偶发，重试命中率高；决策轮与参数轮共用。
_JSON_PARSE_MAX_RETRIES = 5


def _is_json_parse_error(exc: Exception) -> bool:
    """语法级 JSON 解析失败判定（[2026-08-28 JSON 解析有限重试] 专用）。

    ``json.JSONDecodeError``（HTTP 200 但内容非 JSON）为语法畸形，模型侧
    偶发、重试命中率高；langchain ``with_structured_output(json_mode)``
    解析失败会把 JSONDecodeError 包成 ``OutputParserException``（按类名
    判定，不 import langchain 内部类）。语义级畸形（如 pydantic
    ``ValidationError``——model_validate 校验失败，同为 ValueError 子类）
    返回 False，不重试（fail closed 语义不变）。
    """
    return isinstance(exc, json.JSONDecodeError) or exc.__class__.__name__ in {
        "JSONDecodeError",
        "OutputParserException",
    }


def _should_retry_model_call(exc: Exception, parts_emitted: bool) -> bool:
    """模型调用瞬时故障有限重试判定（2026-08-28，项目负责人批准：最多 2 次）。

    与 MCP 层 ``_should_retry``（2026-08-27 批准模式）同构，只重试
    "请求确定未达 / 未开始产出正文"的瞬时故障：
    - 连接类失败（httpx.ConnectError/ConnectTimeout；langchain 侧
      openai.APIConnectionError/APIConnectionFailedError）：连接未建立，
      请求未达模型服务，重试；
    - [2026-08-28 读超时纳入] 读超时/连接中断（httpx.ReadTimeout /
      httpx.ReadError / httpx.RemoteProtocolError——流中途被服务端关闭；
      langchain 侧 openai.APITimeoutError）：请求未开始产出正文，重试
      （正文已输出由 parts_emitted 判定排除）。httpx 0.28 异常分层独立
      （实测 MRO）：ConnectTimeout 非 ConnectError 子类（直继承
      TimeoutException → TransportError）、RemoteProtocolError 非
      ReadError 子类（直继承 ProtocolError → TransportError），判定按
      类显式枚举，不依赖继承关系；
    - HTTP 429（限速，退避尊重 Retry-After）/ 5xx（服务端瞬时故障）：重试
      （覆盖裸 httpx 路径 _ModelHttpError 与 langchain 侧 RateLimitError /
      APIStatusError）。
    绝不重试（fail closed）：HTTP 401/403/400/404（配置/参数问题，重试
    无益）、正文已部分输出（parts 非空——重试会从零重出导致前端重复文本；
    思考增量 reasoning_parts 不参与判定：11.4 不落日志不持久化，且正文
    为空时思考重出不改变对话语义）、空输出/JSON 解析失败（模型侧问题，
    发生在流结束后，天然不进入本判定——JSON 解析失败另由
    ``generate_decision`` / ``generate_tool_arguments`` 的专用重试预算
    （``_JSON_PARSE_MAX_RETRIES``）处理，本函数语义保持"仅网络/限流类
    瞬时故障"）。
    """
    if parts_emitted:
        return False
    # httpx 0.28 异常分层独立（实测 MRO）：ConnectTimeout 非 ConnectError 子类、
    # RemoteProtocolError 非 ReadError 子类，均直继承 TransportError/ProtocolError
    # 分支，须显式列举。连接失败/连接超时/读超时/读错误/协议错误均为"请求未确定
    # 到达/未开始产出正文"的瞬时故障（parts_emitted 判定已排除部分输出场景）。
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,
    )):
        return True
    # langchain 侧 openai SDK 连接类（不 import openai，按类名判定；
    # APIConnectionFailedError 为 1.x 变体类名）
    if exc.__class__.__name__ in {
        "APIConnectionError",
        "APIConnectionFailedError",
        "APITimeoutError",
    }:
        return True
    status = _http_status_of(exc)
    if status == 429:
        return True
    if isinstance(status, int) and 500 <= status <= 599:
        return True
    return False


def _log_model_retry(
    tool_path: str,
    attempt: int,
    exc: Exception,
    http_status: int | None,
    delay_s: int,
    *,
    finish_reason: str | None = None,
) -> None:
    """重试技术日志（11.4 无敏感信息）：只记路径/第几次重试/错误类型/状态码/退避秒数。

    参照 MCP 层 ``_log_retry``（event="mcp_call_retry"）字段风格；不含
    key / base_url（14.4 脱敏口径）。

    [2026-09-10 可观测性] ``finish_reason`` 非 None 才落（决策轮解析失败
    重试时附流结束原因；定稿口径的 head/tail 等特征只落**最终失败**那条，
    重试日志不重复）。
    """
    fields: dict[str, Any] = {
        "tool_path": tool_path,
        "attempt": attempt,
        "error_type": exc.__class__.__name__,
        "http_status": http_status,
        "delay_s": delay_s,
    }
    if finish_reason is not None:
        fields["finish_reason"] = finish_reason
    logger.info("model_call_retry", **fields)


def _log_model_failed(
    tool_path: str,
    exc: Exception,
    http_status: int | None,
    duration_ms: int,
    **features: Any,
) -> None:
    """最终失败技术日志（11.4 无敏感信息）：失败类型可定位（连接/限速/5xx/解析）。

    [2026-09-10 可观测性] ``**features`` 为解析失败脱敏诊断特征（由
    ``_parse_failure_features`` 生成：finish_reason / output_bytes /
    ends_with_brace / output_head / output_tail / validation_issues /
    parsed_keys）；字段命名沿用现有 snake_case 风格，值为 None 的不落
    （避免 null 噪音）。其余失败类型（连接/限速/5xx）不传 features，
    字段面与改动前完全一致。
    """
    logger.error(
        "model_call_failed",
        tool_path=tool_path,
        error_type=exc.__class__.__name__,
        http_status=http_status,
        duration_ms=duration_ms,
        **{key: value for key, value in features.items() if value is not None},
    )


#: [2026-09-16 观测] 参数轮成功事件里 ``args`` 紧凑 JSON 的字符上限：超出则
#: 截断并附标记（``…(truncated)``）。只读工具（erp_query/erp_calc 等）的查询
#: 条件（entity/fields/filter/measures/日期范围）**此前完全无记录**，是诊断
#: 「参数口径错」的核心证据；但 LogRing 单字段另有 1000 字符兜底，这里先按
#: 800 截，避免一个超大 filter 挤占环形缓冲额度（500 条）。
_ARGS_LOG_MAX_CHARS = 800
_ARGS_LOG_TRUNCATED = "…(truncated)"


def _compact_args_json(args: dict[str, Any]) -> str:
    """参数轮成功事件的 ``args`` 值：紧凑 JSON（保中文），超长截断加标记。

    值来自模型输出（json.loads 结果或 langchain 解析的 dict），一般为 JSON
    可序列化；``default=str`` 兜底非标准值，保证**日志路径绝不抛异常**
    （可观测性不得影响主链路）。
    """
    text = json.dumps(args, ensure_ascii=False, default=str)
    if len(text) > _ARGS_LOG_MAX_CHARS:
        return text[:_ARGS_LOG_MAX_CHARS] + _ARGS_LOG_TRUNCATED
    return text


def _observability_ids(fields: dict[str, Any], thread_id: str | None, task_id: str | None) -> None:
    """把可选关联标识并入事件字段：仅非 None 才落（沿用"None 不落"避免噪音）。"""
    if thread_id is not None:
        fields["thread_id"] = thread_id
    if task_id is not None:
        fields["task_id"] = task_id


def _log_decision_succeeded(
    decision: Any,
    duration_ms: int,
    attempt: int,
    *,
    thread_id: str | None = None,
    task_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    """决策轮成功事件（``model_call_succeeded``，tool_path="decision"）。

    [2026-09-16 观测] 补齐此前完全缺失的**决策可见性**：模型的
    next_action / tool_name / skill_name 与回复长度此前只在 checkpoint 的
    ``pending_action`` 里（tools 节点消费即清空），失败类日志（
    model_call_retry/failed）只覆盖失败路径。**不记 reply 原文**（需要正文
    可从 checkpoint / chat 历史取），只记长度，防回话原文泄漏。
    """
    fields: dict[str, Any] = {
        "tool_path": "decision",
        # tool_name 为 None 时如实写 null（决策可见性：null = 本轮不调工具）
        "tool_name": decision.tool_name,
        "next_action": decision.next_action,
        "skill_name": decision.skill_name,
        "questions_count": (
            len(decision.questions) if isinstance(decision.questions, list) else 0
        ),
        "reply_chars": (
            len(decision.reply_text) if isinstance(decision.reply_text, str) else 0
        ),
        "duration_ms": duration_ms,
        "retried": attempt > 0,
        # [2026-09-16 观测] 落 attempt 数值（与失败事件 _log_model_retry 的
        # ``attempt`` 同口径）：仅凭 retried 分不清「第 3 次才成功」与「第 2 次
        # 成功」，而这正是判断「模型不稳 vs 网络抖动」的关键。retried 保留不动。
        "attempt": attempt,
    }
    _observability_ids(fields, thread_id, task_id)
    if usage:
        fields.update(usage)
    logger.info("model_call_succeeded", **fields)


def _log_args_succeeded(
    result: Any,
    duration_ms: int,
    attempt: int,
    *,
    thread_id: str | None = None,
    task_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    """参数轮成功事件（``model_call_succeeded``，tool_path="tool_arguments"）。

    [2026-09-16 观测] 记 ``arg_keys``（填了哪些字段）与 ``args``（紧凑 JSON
    值，超 800 字符截断）——只读工具的参数此前零记录，是诊断「参数口径错」
    的核心证据。LogRing 侧另有脱敏与单字段 1000 字符上限兜底。
    """
    args = result.tool_arguments
    fields: dict[str, Any] = {
        "tool_path": "tool_arguments",
        "tool_name": result.tool_name,
        "arg_keys": list(args.keys()) if isinstance(args, dict) else [],
        "args": _compact_args_json(args) if isinstance(args, dict) else "…",
        "duration_ms": duration_ms,
        "retried": attempt > 0,
        # [2026-09-16 观测] attempt 数值（与 _log_model_retry 同口径），retried 保留
        "attempt": attempt,
    }
    _observability_ids(fields, thread_id, task_id)
    if usage:
        fields.update(usage)
    logger.info("model_call_succeeded", **fields)


#: [2026-09-16 观测] usage 中认得的 token 计数：**源键 → 日志字段名** 映射
#: （OpenAI 兼容通用 prompt/completion/total + DeepSeek 的 cache hit/miss）。
#:
#: ⚠ **输出名刻意避开 ``token`` 子串，勿"顺手改回规范名"**：本项目脱敏规则
#: （audit/redaction.py ``_SENSITIVE_WORDS_RE``）按**子串**匹配敏感词，``token``
#: 在词表内——sanitize_processor 在真实 processor 链上会把整片 usage 抹成
#: ``***(len=N)``（ring 与 stdout 都拿不到）= 观测死代码。故日志字段用
#: ``ctx_*_count``（语义自明 = 上下文 token 计数，且不含任何敏感子串）。
#: 新增计数字段同样须先跑 ``is_sensitive_key`` 确认不命中。
_USAGE_TOKEN_KEYS: dict[str, str] = {
    "prompt_tokens": "ctx_prompt_count",
    "completion_tokens": "ctx_completion_count",
    "total_tokens": "ctx_total_count",
    "prompt_cache_hit_tokens": "ctx_cache_hit_count",
    "prompt_cache_miss_tokens": "ctx_cache_miss_count",
}

#: [2026-09-16 观测] 嵌套 ``prompt_tokens_details`` 内的缓存命中计数（源键 →
#: 日志字段名；输出名同样避开 ``token`` 子串，理由同上）。
_USAGE_DETAIL_KEYS: dict[str, str] = {
    "cached_tokens": "ctx_cached_count",
}


def _usage_fields(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """从流式 usage 块提取 token 计数（只取认得且为 int 的键）；空则 None。

    [2026-09-16 观测] 字段名以实际返回为准：只挑确定语义的计数落日志，
    其余原样忽略（不落未经解释的结构）。**日志字段名用 ``ctx_*_count`` 而
    非源键名**——源键含 ``token`` 子串会命中脱敏词表被抹掉（见
    ``_USAGE_TOKEN_KEYS`` 注释）。当前请求体未开
    ``stream_options.include_usage``，usage 一般为 None（见报告）。
    """
    if not usage:
        return None
    fields: dict[str, Any] = {}
    for src_key, out_key in _USAGE_TOKEN_KEYS.items():
        value = usage.get(src_key)
        if isinstance(value, int):
            fields[out_key] = value
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for src_key, out_key in _USAGE_DETAIL_KEYS.items():
            value = details.get(src_key)
            if isinstance(value, int):
                fields[out_key] = value
    return fields or None


#: [2026-09-10 可观测性] 解析失败诊断特征的脱敏边界与防御上限（2026-09-09
#: 定稿口径）：首尾预览窗口各 20 字符（**仅语法级路径**，语义级只记结构）；
#: validation_issues / parsed_keys 设条数上限，防畸形输出产生超大日志。
_OUTPUT_PREVIEW_CHARS = 20
_VALIDATION_ISSUES_MAX = 12
_PARSED_KEYS_MAX = 12
_PARSED_KEY_CHARS = 40


def _unwrapped_parse_error(exc: Exception) -> Exception:
    """解析失败异常解包到底层根因（[2026-09-10 可观测性]，仅用于日志特征）。

    langchain ``with_structured_output(json_mode)``（PydanticOutputParser）
    把语法级 JSONDecodeError 与语义级 ValidationError 统一包装成
    OutputParserException（``raise … from e``，根因在 ``__cause__``）——
    按 ``__cause__`` 解包，参数轮两类失败才能各按定稿口径记录特征。
    **不参与**重试/失败归类（``_is_json_parse_error`` 与异常类型判定
    全部不变，行为零变化）。
    """
    cause = exc.__cause__
    if isinstance(cause, (json.JSONDecodeError, ValidationError)):
        return cause
    return exc


def _validation_issue_summary(exc: ValidationError) -> list[str]:
    """ValidationError 的 loc+type 摘要（如 ``reply_text:missing``）——绝不含 input 值。

    [2026-09-10 可观测性] 生产事故（2026-09-10 03:33:33Z，tool_path=
    decision）只记到 error_type=ValidationError，分不清「reply 缺
    reply_text」还是「next_action 非法值」；loc+type 摘要一锤定音。
    ``err.errors()`` 的 input/ctx 等携带输出内容的字段一律丢弃（11.4
    脱敏纪律）；条数设上限（防畸形输出超大错误列表）。
    """
    issues: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc") or ())
        issue_type = str(err.get("type") or "unknown")
        issues.append(f"{loc}:{issue_type}" if loc else issue_type)
    if len(issues) > _VALIDATION_ISSUES_MAX:
        extra = len(issues) - _VALIDATION_ISSUES_MAX
        return [*issues[:_VALIDATION_ISSUES_MAX], f"...(+{extra})"]
    return issues


def _parsed_top_keys(data: Any) -> list[str] | None:
    """已解析 JSON 的顶层键名集合（只记键名不记内容；非 dict → None）。

    [2026-09-10 可观测性] 键名即模型输出的字段结构，足以判断「reply 缺
    reply_text」这类形态问题（键名非业务值）；键名长度与数量设上限，
    防畸形输出把日志撑爆。
    """
    if not isinstance(data, dict):
        return None
    keys = sorted(str(key)[:_PARSED_KEY_CHARS] for key in data)
    if len(keys) > _PARSED_KEYS_MAX:
        extra = len(keys) - _PARSED_KEYS_MAX
        return [*keys[:_PARSED_KEYS_MAX], f"...(+{extra})"]
    return keys


def _output_text_of(exc: Exception) -> str | None:
    """解析失败异常携带的模型输出文本（[2026-09-10 可观测性]）。

    langchain ``OutputParserException.llm_output``：语法级=原始输出文本
    （head/tail 边界适用），语义级=已解析对象的归一化 dumps 结果（只用于
    反解顶层键名，不落原文；参数轮原始输出文本 langchain 未保留，据此算出
    的 output_bytes 是归一化 JSON 规模而非原始输出规模——已知局限）。
    决策轮不经此处（裸 httpx 自行持有输出文本）。非字符串/缺失 → None
    （相关特征字段随之省略）。
    """
    text = getattr(exc, "llm_output", None)
    return text if isinstance(text, str) else None


def _parse_failure_features(
    exc: Exception,
    output_text: str | None,
    *,
    parsed: Any = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """模型输出解析失败的脱敏诊断特征（[2026-09-10 可观测性]，2026-09-09 定稿口径）。

    - ``finish_reason``（非 None 才记）：流式最后一个 chunk 的结束原因
      ——length=输出被 max_tokens 截断（应提高 max_tokens 或分步输出），
      stop 等=模型侧畸形输出（重试/报 API 故障）：一眼区分两类根因；
    - ``output_bytes``：解析候选文本 UTF-8 字节数（决策轮为经
      ``_strip_json_fence`` 去围栏后的文本；参数轮为 llm_output）；
    - 语法级（JSONDecodeError / OutputParserException）：``ends_with_brace``
      / ``output_head`` / ``output_tail``（首尾各 20 字符——定稿允许的
      脱敏边界，**仅此路径**）；
    - 语义级（pydantic ValidationError）：``validation_issues``（loc+type
      摘要）+ ``parsed_keys``（顶层键名集合）——**绝不记原文**；根因
      ValidationError 解包后 ``parsed`` 缺省时，尝试从 ``output_text``
      反解顶层键（参数轮路径）。

    返回值直接展开进 ``_log_model_failed(**features)``；值为 None 的字段
    由日志函数统一过滤不落（避免 null 噪音），空输出文本不产生首尾预览
    字段。行为零变化——本函数只被失败日志路径调用。
    """
    root = _unwrapped_parse_error(exc)
    features: dict[str, Any] = {}
    if finish_reason is not None:
        features["finish_reason"] = finish_reason
    if output_text is not None:
        # 只记长度不记内容（11.4）：字节数比字符数更能反映输出规模
        features["output_bytes"] = len(output_text.encode("utf-8"))
    if isinstance(root, ValidationError):
        issues = _validation_issue_summary(root)
        if issues:
            features["validation_issues"] = issues
        data = parsed
        if data is None and output_text is not None:
            try:
                data = json.loads(output_text)
            except (ValueError, TypeError):
                data = None
        keys = _parsed_top_keys(data)
        if keys is not None:
            features["parsed_keys"] = keys
    elif _is_json_parse_error(root) or _is_json_parse_error(exc):
        if output_text:
            features["ends_with_brace"] = output_text.rstrip().endswith("}")
            features["output_head"] = output_text[:_OUTPUT_PREVIEW_CHARS]
            features["output_tail"] = output_text[-_OUTPUT_PREVIEW_CHARS:]
    return features


class AgentDecision(BaseModel):
    """[2026-08-24 harness 改造] 会话循环图模型决策（Phase 0 探针 §3 五字段）。

    - ``next_action``：reply（直接回复，结束本轮）/ call_tool（调用工具，
      本轮至多一个）/ needs_input（向用户追问，questions 必填）——澄清循环
      （needs_input → 用户补充 → 再决策）为一等公民（探针 §4.3）；
    - ``tool_name``：仅 call_tool 时非空；键名须来自
      model_node._build_system_prompt 的「可用工具」段枚举表（
      build_select_schema 现只出指向该表的指针句；工具节点侧还有 pydantic
      参数解析确定性校验兜底，模型幻觉参数不落 ERP）；
    - ``tool_arguments``：model 节点输出时为 None——由 tool_args 节点
      第二轮调用（``generate_tool_arguments``）生成后填充，再交 tools
      节点执行；
    - ``skill_name``：本轮要进入的流程（``prompts/skills.py`` 的 SKILL_REGISTRY
      之一），``None`` = 不进任何流程。[2026-09-13 skill 层接入] **有默认值**：
      字段缺席等同于 null（不进入流程）——与方案原文"每轮必填"不同，理由是
      爆炸半径不对称（见下），故按"缺席即退出"实现。
    """

    reply_text: str
    next_action: Literal["reply", "call_tool", "needs_input"]
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    questions: list[str] | None = None
    #: [2026-09-13 skill 层接入] 本轮激活的 skill_key；None = 不在任何 skill
    #: 流程里。**故意给默认值**（而非方案 §3.1 写的"每轮必填"）：模型偶发漏
    #: 输出该键时，缺省 = 不进入流程（退化为常规对话，可自愈），而"必填"会让
    #: 一次漏输出直接 ValidationError → ModelCallError → **整轮对话失败**。
    #: 两侧代价不对称，取缺席即退出的宽松侧；非法值（幻觉键名）由
    #: skills.skill_for 按"不进入流程"忽略——skill 是引导不是执行，无可被
    #: 绕过的副作用，故不像 tool_name 那样 fail closed。
    skill_name: str | None = None


def _coerce_decision(data: Any) -> Any:
    """[2026-08-25 畸形输出修复] 决策轮 json_mode 畸形输出的确定性修复中间件。

    设计原则：只吸收**零语义损失**的畸形（过渡性文案可补，不改变决策语义）；
    涉及决策语义的畸形**绝不编造**，保持原样交给 ``model_validate`` 失败
    （fail closed 语义不变）。

    修复点一：``reply_text`` 缺失 / None / 空白串时——
    - ``next_action == "call_tool"`` → 补过渡文案 "正在处理，请稍候。"；
    - ``next_action == "needs_input"`` → 补过渡文案 "请补充以下信息："
      （修复点二若命中，编号追问会追加其后）；
    - ``next_action == "reply"`` → **不修复**：reply 必须携带真实回复内容，
      补文案 = 编造回复，保持 model_validate 失败（fail closed）；
    - 其他（next_action 缺失/非法值等）→ 不修复：无法确定过渡文案归属。

    修复点二（needs_input 追问合并）：模型偶发只把追问写进 questions、
    reply_text 只剩引导句（如"为了准确查询…请确认以下信息："）→ 前端只渲染
    reply_text，追问丢失（感知"回答卡住"）。questions 是模型生成的真实追问
    （非编造），合并进 reply_text 是零语义损失修复：
    - questions 为列表且含非空字符串、且 reply_text 缺失/空白/不含任一追问
      文本时 → 以 reply_text 为基底（缺失时用 "请补充以下信息："）追加
      "1. …" / "2. …" 编号追问行；
    - reply_text 已含任一追问文本 → 跳过（模型已写全，不重复）。

    其余字段一律不修复：next_action 非法值、call_tool 缺 tool_name、
    questions 非列表等 → 原样走 ``model_validate`` → 失败即
    ``ModelCallError``（与现状一致）。

    [2026-09-10 6b 修复] 顶层非对象守卫（reviewer 探针 E 复现，存量缺陷）：
    模型返回合法 JSON 但顶层非对象（``[]`` / ``"text"`` / ``null`` /
    ``123`` / ``true``）时，本函数此前直接 ``data.get(...)`` 抛
    ``AttributeError``——不在调用方 ``except (ValueError, TypeError)``
    内、非 ModelCallError，穿透 model_node 的 ``except ModelCallError``
    （用户见「对话处理失败」而非「模型暂时不可用」，且无
    model_call_failed 特征日志）。现非 dict 原样返回，交给
    ``AgentDecision.model_validate`` 抛 ValidationError（语义级畸形，
    与设计原则一致：不编造、fail closed 不重试）→ 被既有 except 捕获 →
    优雅 ModelCallError + 可观测性特征（``validation_issues=
    ["model_type"]``）。除此之外语义零变化。
    """
    if not isinstance(data, dict):
        return data
    text = data.get("reply_text")
    if text is None or (isinstance(text, str) and not text.strip()):
        if data.get("next_action") == "call_tool":
            data["reply_text"] = "正在处理，请稍候。"
        elif data.get("next_action") == "needs_input":
            data["reply_text"] = "请补充以下信息："
    # needs_input 追问合并：模型偶发把追问只写进 questions、reply_text 只剩
    # 引导句 → 前端只渲染 reply_text，追问丢失（感知"回答卡住"）。
    # questions 是模型生成的真实追问（非编造），合并进 reply_text 是零语义
    # 损失修复；reply_text 已含任一追问文本时跳过（模型已写全，不重复）。
    if data.get("next_action") == "needs_input":
        questions = data.get("questions")
        text = data.get("reply_text")
        if isinstance(questions, list):
            qs = [q for q in questions if isinstance(q, str) and q.strip()]
            if qs and (not isinstance(text, str) or not text.strip() or not any(q in text for q in qs)):
                base = (text or "").strip() or "请补充以下信息："
                base = base.rstrip() if not base.endswith(("：", ":")) else base
                lines = [f"{i + 1}. {q}" for i, q in enumerate(qs)]
                data["reply_text"] = f"{base}\n" + "\n".join(lines)
    return data


class ToolArguments(BaseModel):
    """[2026-08-24 harness 改造] 工具参数生成结果（tool_args 节点第二轮调用）。

    - ``tool_name``：必须等于第一轮决策选定的工具名（不一致 → 取消本次
      调用，回 model 自纠）；
    - ``tool_arguments``：该工具参数结构对应的具体参数（tools 节点侧还有
      pydantic 确定性校验兜底，模型幻觉参数不落 ERP）。
    """

    tool_name: str
    tool_arguments: dict[str, Any]


def _strip_json_fence(text: str) -> str:
    """从模型输出提取 JSON 文本（决策轮流式全文 → json.loads 前的防御处理）。

    先 strip 首尾空白；若文本含 ```json ...``` 围栏则取首个围栏内内容
    （json_mode 通常不产生围栏，防御处理）；围栏不闭合 → 返回原文本，
    交由 json.loads 判定失败（保持现有错误口径）。
    """
    stripped = text.strip()
    marker = "```json"
    idx = stripped.find(marker)
    if idx < 0:
        return stripped
    start = idx + len(marker)
    end = stripped.find("```", start)
    if end < 0:
        return stripped
    return stripped[start:end].strip()


class ModelDecisionClient:
    """[2026-08-24 harness 改造] 会话循环图模型决策客户端。

    与已删除的旧 ModelReplyClient 同范式：懒构造 ChatOpenAI + fail closed
    三件套——
    无 key/base_url → ``ModelUnavailableError``；调用/解析异常 →
    ``ModelCallError``；输出非 AgentDecision → ``ModelCallError``；
    异常消息不携带敏感信息。``generate_decision`` 接收**任意长度**历史：
    messages 为 LangChain 消息对象（HumanMessage/AIMessage/ToolMessage 等）
    或 ("system"/"human"/"ai", content) 元组的混合列表，原样传入模型；
    schema 说明（``build_select_schema`` 生成的 instruction）注入最后一条
    human 消息（无 human 时追加一条），满足 deepseek json_mode 的 "json"
    字样约束。

    [2026-08-24 二次改造] 参数轮：``generate_tool_arguments`` 与决策轮
    同构（``ToolArguments`` 结构化输出 + ``build_args_schema`` 注入），
    供 tool_args 节点第二轮生成工具参数。

    [2026-08-24 定稿] 记忆压缩：``generate_summary`` 为压缩调用（SummaryResult
    结构化输出 + max_tokens 上限）；配置校验与决策客户端共用
    ``_ensure_configured``（key/base_url 缺失 → ModelUnavailableError）。

    [2026-08-24 流式改造] ``generate_decision`` 改 ``chat.stream`` 逐 token
    流式（``on_chunk`` 回调把增量文本交给调用方——model 节点发射
    agent_reply_chunk SSE 事件；不传时保持"整体返回"语义）；结构化解析
    （json.loads + AgentDecision.model_validate）仍在流结束后统一完成。

    [2026-08-25 思考流式] ``generate_decision`` 再改**裸 httpx SSE**（LangChain
    兼容层丢弃 ``reasoning_content``）：同一次请求同时透传思考
    （``on_reasoning_chunk`` → agent_reasoning_chunk SSE，前端灰色"正在思考"
    区逐字渲染）与正文（``on_chunk`` → agent_reply_chunk）；思考不落日志、
    不持久化（11.4，SSE 事件流进程内短时投递）。``_ensure_raw_chat`` 保留
    不删（不再被调用）。

    [2026-08-28 瞬时故障有限重试] ``generate_decision``（裸 httpx）与
    ``generate_tool_arguments``（langchain invoke）增加瞬时故障重试
    （settings.model_call_max_retries，默认 2 次，2026-08-28 项目负责人
    批准）：连接类失败（httpx.ConnectError / openai.APIConnectionError）、
    读超时/连接中断（httpx.ReadTimeout / httpx.ReadError——含
    RemoteProtocolError；langchain 侧 openai.APITimeoutError，2026-08-28
    纳入）、HTTP 429（退避尊重 Retry-After）、HTTP 5xx 才重试——指数退避
    1s/2s，与 MCP 层 mcp_call_retry（2026-08-27 批准模式）同构；
    正文已部分输出（parts 非空，重试会重出文本）、空输出、语义级 JSON
    解析失败（model_validate 校验失败）、401/403/400/404 一律不重试。
    每次尝试 parts/reasoning_parts 从零累积、on_chunk 只在当前尝试成功
    产出时回调；墙钟 420s（t0）在重试循环外，重试期间不重置。重试与最终
    失败各打一条脱敏技术日志（model_call_retry / model_call_failed，
    11.4 口径，不含 key/base_url）；重试用尽仍抛 ModelCallError，上层
    （model 节点 except ModelCallError）契约完全不变。

    [2026-08-28 JSON 解析有限重试] 语法级 JSON 解析失败（HTTP 200 但内容
    非 JSON 的 JSONDecodeError，及 langchain ``with_structured_output``
    解析失败包装的 OutputParserException）在决策轮/参数轮各加独立重试
    预算（``_JSON_PARSE_MAX_RETRIES``：额外重试 5 次、总尝试 6 次以内，
    2026-08-28 引入、2026-09-01 上调批准）——模型侧偶发、重试命中率高。
    决策轮重试轮抑制流式
    回调（前端已收到首轮全部文本，不重放）；语义级畸形（model_validate
    校验失败，如 tool_name 缺失）与空输出仍 fail closed 不重试。
    ``generate_summary`` 摘要路径不在重试范围（非瞬时故障场景），不改。

    [2026-09-01 结构修复] 上述两个重试预算原共享外层 for 循环迭代数
    （range(max_retries+1)），JSON 解析重试的 continue 跳回外层 for——
    总尝试被锁死为 max_retries+1 次（默认 3 次），``_JSON_PARSE_MAX_RETRIES``
    预算完全未生效（实测 attempt=4/5 缺失、第 3 次后直接 fail closed）。
    现改为 while True + 双预算各自递减：瞬时故障重试（network_retries_left
    从 model_call_max_retries 递减，默认额外重试 2 次、指数退避 1s/2s、
    429 尊重 Retry-After）与 JSON 解析重试（json_retries_left 从
    ``_JSON_PARSE_MAX_RETRIES`` 递减，额外重试 5 次、固定 sleep 1s）互相
    独立、各自用尽才 fail closed；suppress_stream / 墙钟 420s（t0 循环外）/
    空输出与语义级畸形 fail closed / attempt 日志口径（第几次尝试，1 起）
    全部不变。

    [2026-08-28 off 档] ``model_reasoning_effort`` 新增 ``off``（用户批准
    关闭 thinking，根除多轮对话 400 的 reasoning_content 回传校验风险）：
    决策轮（裸 httpx）在 payload 顶层注入 ``thinking: {"type": "disabled"}``
    （OpenAI 兼容新格式，实测生效）；参数轮/压缩轮（langchain ChatOpenAI
    路径）经 ``extra_body`` 透传同一字段（langchain_openai 1.6 + openai
    SDK 3.x 严格签名，model_kwargs 顶层合并自定义字段会被拒绝——实测
    验证）；off 时一律不传 ``reasoning_effort``，与 json_mode 的
    response_format 共存无冲突。空串语义不变（不传参数=API 默认开启思考）。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._chat: Any | None = None
        self._raw_chat: Any | None = None
        self._args_chat: Any | None = None
        self._summary_chat: Any | None = None

    def _ensure_configured(self) -> None:
        """模型配置校验（决策/参数/压缩客户端共用；未配置直接失败）。

        base_url 为空时禁止请求——防止把 key 发往默认端点（api.openai.com）。
        """
        if not self._settings.model_api_key:
            raise ModelUnavailableError(
                "模型 API key 未配置（model_api_key 为空）：fail closed，不发起调用"
            )
        if not self._settings.model_base_url:
            raise ModelUnavailableError(
                "模型 base_url 未配置（model_base_url 为空）：fail closed，不发起调用"
            )

    def _ensure_chat(self) -> Any:
        """懒构造带结构化输出的 ChatOpenAI（fail closed：未配置直接失败）。"""
        self._ensure_configured()
        if self._chat is None:
            try:
                effort = self._settings.model_reasoning_effort
                # 2026-08-27 思考强度 / 2026-08-28 off 档：决策/参数/压缩/裸
                # 客户端统一处理 model_reasoning_effort（默认 medium；空串=
                # 不传该参数，保持 API 默认最高思考强度；off=extra_body 透传
                # thinking: {"type": "disabled"} 显式关闭，不传
                # reasoning_effort）。ChatOpenAI 一等字段（走 Chat
                # Completions 请求体原样下发，deepseek 兼容层实测接受
                # low/medium/high），与 with_structured_output(json_mode) 的
                # response_format 共存（call-time bind kwargs 优先，未冲突）。
                extra_body = {"thinking": {"type": "disabled"}} if effort == "off" else None
                self._chat = ChatOpenAI(
                    model=self._settings.model_name,
                    base_url=self._settings.model_base_url,
                    api_key=self._settings.model_api_key,
                    timeout=60.0,  # 同步编排：总超时上限，避免无限挂起
                    max_retries=0,  # fail closed：不静默重试掩盖失败
                    extra_body=extra_body,
                    reasoning_effort=None if effort in ("", "off") else effort,
                ).with_structured_output(AgentDecision, method="json_mode")
            except Exception as exc:
                raise ModelCallError(
                    "模型客户端初始化失败（连接/参数错误）：未执行任何操作"
                ) from exc
        return self._chat

    def _ensure_raw_chat(self) -> Any:
        """[2026-08-24 流式改造] 懒构造裸 ChatOpenAI（无结构化输出包装）。

        与 ``_ensure_chat`` 同参数（model/base_url/api_key/timeout/max_retries），
        但**不带** with_structured_output——决策轮流式化后需逐 chunk 读取
        原始文本，手动补 json_mode 等效参数（``model_kwargs``
        response_format json_object，与 with_structured_output(method=
        "json_mode") 的请求面完全一致）；结构化解析（json.loads +
        AgentDecision.model_validate）在 ``generate_decision`` 流结束后
        统一做。构造失败 → ModelCallError（fail closed，不静默重试）。

        [2026-08-25 思考流式] ``generate_decision`` 已改裸 httpx SSE（LangChain
        兼容层丢弃 reasoning_content），本方法不再被调用——保留不删（参数轮
        /压缩轮走 ``_ensure_args_chat``/``_ensure_summary_chat``，互不影响；
        未来若需 LangChain 裸流式可复用）。
        """
        self._ensure_configured()
        if self._raw_chat is None:
            try:
                effort = self._settings.model_reasoning_effort
                # 2026-08-28 off 档：与参数/压缩轮同构（off=extra_body 透传
                # thinking 显式关闭，不传 reasoning_effort；openai SDK 3.x
                # 严格签名下 model_kwargs 顶层合并自定义字段会被拒绝）。
                extra_body = {"thinking": {"type": "disabled"}} if effort == "off" else None
                self._raw_chat = ChatOpenAI(
                    model=self._settings.model_name,
                    base_url=self._settings.model_base_url,
                    api_key=self._settings.model_api_key,
                    timeout=60.0,  # 同步编排：总超时上限，避免无限挂起
                    max_retries=0,  # fail closed：不静默重试掩盖失败
                    model_kwargs={"response_format": {"type": "json_object"}},
                    extra_body=extra_body,
                    # 2026-08-27 思考强度：与 response_format 同请求下发
                    # （ChatOpenAI 一等字段；空串=不传该参数）
                    reasoning_effort=None if effort in ("", "off") else effort,
                )
            except Exception as exc:
                raise ModelCallError(
                    "模型客户端初始化失败（连接/参数错误）：未执行任何操作"
                ) from exc
        return self._raw_chat

    def _ensure_args_chat(self) -> Any:
        """[2026-08-24 二次改造] 懒构造工具参数生成客户端（ToolArguments）。

        与 ``_ensure_chat`` 同构：裸 ChatOpenAI + json_mode 结构化输出
        ToolArguments；无 max_tokens（参数轮不需要输出长度上限）。
        """
        self._ensure_configured()
        if self._args_chat is None:
            try:
                effort = self._settings.model_reasoning_effort
                # 2026-08-28 off 档：模型无关自定义字段只能走 extra_body 透传
                # （langchain_openai 1.6 + openai SDK 3.x 严格签名——model_kwargs
                # 顶层合并 thinking 会被 SDK 拒绝，实测验证）；off 时改传
                # thinking 不传 reasoning_effort，与 json_mode 的
                # response_format 共存无冲突。
                extra_body = {"thinking": {"type": "disabled"}} if effort == "off" else None
                self._args_chat = ChatOpenAI(
                    model=self._settings.model_name,
                    base_url=self._settings.model_base_url,
                    api_key=self._settings.model_api_key,
                    timeout=60.0,  # 同步编排：总超时上限，避免无限挂起
                    max_retries=0,  # fail closed：不静默重试掩盖失败
                    extra_body=extra_body,
                    # 2026-08-27 思考强度：显式注入 model_reasoning_effort
                    # （默认 medium；空串=不传该参数，保持 API 默认）
                    reasoning_effort=None if effort in ("", "off") else effort,
                ).with_structured_output(ToolArguments, method="json_mode")
            except Exception as exc:
                raise ModelCallError(
                    "模型客户端初始化失败（参数生成连接/参数错误）：未执行任何操作"
                ) from exc
        return self._args_chat

    @staticmethod
    def _sanitize_tool_messages(messages: list[Any]) -> list[Any]:
        """OpenAI 兼容端点（DeepSeek）对话形状修正：tool 角色消息必须紧跟在
        带 ``tool_calls`` 的 assistant 消息之后，否则 400（实测
        "Messages with role 'tool' must be a response to a preceding message
        with 'tool_calls'"）。

        会话循环图（Phase 2/3a）的 AIMessage 是纯内容消息（工具决策在
        ``pending_action``，不写 LangChain ``tool_calls``）→ 工具执行后的
        第二次模型调用（工具结果自纠/确认后终局回复）历史中必有无主
        ToolMessage——真实验证冒烟复验实测 HTTP 400（SSE agent_error
        model_unavailable，写已成功但终局回复失败）。本方法把无匹配
        tool_calls 的 ToolMessage 转为 ("human", "[工具执行结果] ...")
        文本消息：模型仍读到工具结果，对话形状对 OpenAI 兼容端点合法。

        适配层修正：不改图/checkpoint/SSE 事件语义，图内历史原样保留。
        """
        known_ids: set[str] = set()
        out: list[Any] = []
        for message in messages:
            for tc in getattr(message, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    known_ids.add(str(tc_id))
            if getattr(message, "type", None) == "tool":
                tc_id = getattr(message, "tool_call_id", None)
                if tc_id and str(tc_id) in known_ids:
                    out.append(message)
                    continue
                content = getattr(message, "content", "")
                out.append(("human", f"[工具执行结果] {content}"))
                continue
            out.append(message)
        return out

    @staticmethod
    def _with_schema_instruction(messages: list[Any], instruction: str) -> list[Any]:
        """把 json_mode 结构化输出说明（instruction）注入最后一条 human 消息。

        - 最后一条为 human（消息对象或 ("human", content) 元组）→ 追加到其
          content 末尾（保持单条 user 消息形态，探针 C 同口径）；
        - 否则（历史以 ai/tool 结尾等）→ 追加一条仅含说明的 human 消息。
        返回新列表，不修改入参。
        """
        if not messages:
            return [("human", instruction)]
        last = messages[-1]
        if isinstance(last, tuple) and len(last) >= 2 and last[0] == "human":
            return [*messages[:-1], (last[0], f"{last[1]}\n\n{instruction}")]
        # LangChain 消息对象：按 .type 判定
        last_type = getattr(last, "type", None)
        if last_type == "human":
            content = getattr(last, "content", "")
            try:
                cloned = last.model_copy(update={"content": f"{content}\n\n{instruction}"})
            except Exception:
                cloned = ("human", f"{content}\n\n{instruction}")
            return [*messages[:-1], cloned]
        return [*messages, ("human", instruction)]

    @staticmethod
    def _to_openai_messages(messages: list[Any]) -> list[dict[str, str]]:
        """[2026-08-25 思考流式] 混合形态消息列表 → OpenAI 兼容 dict 列表。

        裸 httpx SSE 调用（``generate_decision``）不经过 LangChain 序列化，
        需把 ``_sanitize_tool_messages`` / ``_with_schema_instruction`` 产出
        的混合形态（LangChain 消息对象或 ("system"/"human"/"ai", content)
        元组）统一为 ``{"role": ..., "content": ...}``：
        - LangChain 对象按 ``.type`` 映射 role（human→user、ai→assistant、
          tool→tool、system→system），content 非 str 时保守转 str；
        - 元组直接用（role 即 "system"/"human"/"ai"，映射同上）；
        - 未知 role 兜底 "user"（防御；正常路径不会出现）。
        返回新列表，不修改入参。
        """
        role_map = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}
        out: list[dict[str, str]] = []
        for message in messages:
            if isinstance(message, tuple) and len(message) >= 2:
                role, content = message[0], message[1]
            else:
                role = getattr(message, "type", None)
                content = getattr(message, "content", "")
            mapped = role_map.get(role, role)
            if not isinstance(mapped, str) or not mapped:
                mapped = "user"
            out.append({"role": mapped, "content": str(content)})
        return out

    def generate_decision(
        self,
        *,
        system_prompt: str,
        messages: list,
        schema_instruction: str,
        on_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentDecision:
        """模型基于**全量对话历史**生成决策；失败统一抛 ``ModelCallError``
        （含未配置的 ``ModelUnavailableError``，上层一处捕获）。

        [2026-08-25 思考流式] 由 LangChain ``chat.stream`` 改为**裸 httpx
        SSE 流式**（OpenAI 兼容端点）：LangChain 兼容层把
        ``reasoning_content`` 丢弃（chunk 无任何字段暴露），裸解析可同一次
        请求同时拿到思考与正文（不双调）：
        - 请求面与 json_mode 结构化输出一致（POST
          ``{base_url}/chat/completions``，stream + response_format
          json_object；headers 只带 Authorization Bearer key，不携带/记录
          其他敏感信息）；
        - 每行 SSE（``data:`` 前缀、``[DONE]`` 终止）取 ``choices[0].delta``：
          * ``content``（非空 str）累积全文 + 调 ``on_chunk(text)``（model
            节点经此发射 agent_reply_chunk，前端逐字渲染）；
          * ``reasoning_content``（非空 str）累积 + 调
            ``on_reasoning_chunk(text)``（model 节点经此发射
            agent_reasoning_chunk，前端灰色"正在思考"区逐字渲染；不传时
            静默丢弃——不落日志不持久化，11.4）；
        - 流结束后整体解析：``_strip_json_fence`` 围栏防御 → ``json.loads``
          → ``_coerce_decision`` → ``AgentDecision.model_validate``；
          **reasoning 不参与 json 解析**（仅正文累积 parts 入解析）；
          空输出 / 解析失败 → ModelCallError（同现有文案口径）；
        - 迭代过程异常（超时/网络中断/HTTP 非 200，流中途断开即失败）→
          ModelCallError（同现有文案口径）。

        ``schema_instruction`` 为决策轮 json_mode 结构化输出说明（由
        ``build_select_schema`` 生成，随工具注册表动态变化；与 model 节点
        system prompt 尾部共用同一文本）。异常消息不携带 key / base_url
        等敏感信息（14.4 脱敏口径）。

        [2026-08-26 日期注入] system_prompt 追加当前日期行（服务器本地
        时间），模型可解析「7月」「上周」等相对时间语义。

        [2026-09-10 可观测性] 流式消费循环捕获最后一个 chunk 的
        ``finish_reason``（此前未读取）；解析失败最终日志按定稿口径附
        脱敏诊断特征、重试日志附 finish_reason、空输出日志附
        finish_reason（详见模块 docstring）。行为零变化——重试策略 /
        fail-closed / 异常类型 / 用户文案均不变。
        """
        system_prompt = f"{system_prompt}\n\n{_current_date_line()}"
        # [2026-08-28 瞬时故障有限重试] 请求/流式阶段失败且可重试
        # （_should_retry_model_call：连接类 / 读超时/连接中断 / HTTP 429 /
        # 5xx）→ 指数退避 1s/2s（429 有 Retry-After 时取
        # max(1<<attempt, retry_after)）后整轮重试；每次尝试
        # parts/reasoning_parts 从零累积、on_chunk 只在当前尝试成功产出时
        # 回调（重试前正文为空是前提——正文已输出即不可重试，不会向调用方
        # 重放文本）。墙钟 420s（t0）在重试循环外：重试期间不重置，整体
        # 上限不变。重试用尽仍抛 ModelCallError（fail closed，上层 model
        # 节点 except ModelCallError 契约完全不变）。
        # [2026-08-28 JSON 解析有限重试] 流结束后语法级 JSON 解析失败
        # （_is_json_parse_error）另走独立重试预算（json_retries_left 从
        # _JSON_PARSE_MAX_RETRIES 递减，总尝试 6 次以内）——解析失败发生在
        # 请求/流式阶段之外，不经 _should_retry_model_call 判定；重试轮
        # 抑制流式回调（suppress_stream，前端已收到首轮全部 chunk，不重放
        # 正文/思考）。语义级畸形（model_validate 校验失败）与空输出仍
        # fail closed 不重试。
        # [2026-09-01 重试预算结构修复] 循环由 for(attempt in
        # range(max_retries+1)) 改为 while True + 双预算：瞬时故障（网络/限速，
        # network_retries_left 从 model_call_max_retries 递减）与语法级 JSON
        # 解析失败（json_retries_left 从 _JSON_PARSE_MAX_RETRIES 递减）互相
        # 独立、各自用尽才 fail closed——修复此前 JSON 解析重试的 continue
        # 跳回外层 for 迭代（仅 max_retries+1 次），总尝试被锁死为 3 次、
        # _JSON_PARSE_MAX_RETRIES=5 预算从未生效的结构 bug（实测 attempt=4/5
        # 缺失）。attempt 为共享重试计数（本次是第几次尝试，1 起），仅用于
        # 脱敏日志。
        max_retries = max(0, int(self._settings.model_call_max_retries))
        json_retries_left = _JSON_PARSE_MAX_RETRIES
        network_retries_left = max_retries
        attempt = 0
        t0 = time.monotonic()
        while True:
            suppress_stream = json_retries_left < _JSON_PARSE_MAX_RETRIES
            parts: list[str] = []
            reasoning_parts: list[str] = []
            # [2026-09-10 可观测性] 本次尝试的流结束原因（最后一个 chunk 的
            # finish_reason：stop/length/…；length=输出被 max_tokens 截断）。
            # 非流式异常路径（HTTP/连接失败）保持 None。解析失败日志据此
            # 区分「截断」与「模型畸形输出」（2026-09-09 定稿口径）。
            finish_reason: str | None = None
            # [2026-09-16 观测] 本次尝试的 usage（若 provider 在流中回传，
            # OpenAI 兼容语义下为末块或独立块携带）。当前请求体未传
            # ``stream_options.include_usage``——DeepSeek 在此开关关闭时
            # **不回传** usage（见报告）。此处只做**只读**解析：带就记、不带
            # 就保持 None（不落字段），绝不改请求体。
            usage: dict[str, Any] | None = None
            try:
                self._ensure_configured()
                conv = self._with_schema_instruction(
                    self._sanitize_tool_messages(list(messages)), schema_instruction
                )
                # system 首条已存在于历史时不再重复注入（system_prompt 与历史分离）
                first = conv[0] if conv else None
                first_type = first[0] if isinstance(first, tuple) else getattr(first, "type", None)
                payload = conv if first_type == "system" else [("system", system_prompt), *conv]
                dict_msgs = self._to_openai_messages(payload)
                # 单次决策总时长上限 420s（思考 + 正文流式输出）由墙钟检查实现：
                # t0 = time.monotonic() 起算（重试循环外，重试期间不重置），
                # 流式循环内超 420s 抛 TimeoutError。
                # 长明细表格输出约需 5-7 分钟，墙钟为兜底上限非下限。
                # httpx.Timeout 0.28 起无 total 参数（0.26/0.27 废弃、0.28 删除），
                # 只保留各维度空闲超时（connect 15s / read 30s / write 30s / pool 15s）
                # 防静默挂起——流式持续有数据时 read 不超时，总时长上限靠墙钟兜底；
                # TimeoutError/ReadTimeout/ConnectTimeout → 下方 except 统一归
                # ModelCallError（fail closed，上层按失败处理）。
                # 2026-08-27 思考强度 / 2026-08-28 off 档：决策轮（裸 httpx）
                # 与参数/压缩/裸客户端统一处理 model_reasoning_effort——
                # low/medium/high 注入 reasoning_effort；off 改注入
                # thinking: {"type": "disabled"} 显式关闭思考（不传
                # reasoning_effort）；空串=不传参数，保持 API 默认。
                payload: dict[str, Any] = {
                    "model": self._settings.model_name,
                    "messages": dict_msgs,
                    "stream": True,
                    "response_format": {"type": "json_object"},
                    # 决策轮输出上限：长明细（如 240 行表格 ≈ 1.1 万 tokens）
                    # 需要超出 provider 默认值的输出空间（2026-08-26）
                    "max_tokens": 16384,
                }
                if self._settings.model_reasoning_effort == "off":
                    # off 档（2026-08-28 批准）：显式关闭 thinking（OpenAI
                    # 兼容新格式，裸 httpx 实测 SSE 全程 0 个 reasoning
                    # delta chunks）；关闭后无 reasoning_content，根除多轮
                    # 对话 thinking 模式 400 风险（reasoning 回传校验）。
                    payload["thinking"] = {"type": "disabled"}
                elif self._settings.model_reasoning_effort:
                    payload["reasoning_effort"] = self._settings.model_reasoning_effort
                with httpx.Client(
                    timeout=httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
                ) as client:
                    with client.stream(
                        "POST",
                        f"{self._settings.model_base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {self._settings.model_api_key}"},
                        json=payload,
                    ) as response:
                        if response.status_code != 200:
                            # HTTP 调用失败（400/401/429/5xx 等）→ 抛携带状态码
                            # 与 Retry-After 的内部信号（可重试与否由
                            # _should_retry_model_call 判定；对外统一
                            # ModelCallError，消息不含 key/base_url，14.4 口径）
                            raise _ModelHttpError(
                                status=response.status_code,
                                retry_after_sec=_parse_retry_after(
                                    response.headers.get("Retry-After")
                                ),
                            )
                        for line in response.iter_lines():
                            if time.monotonic() - t0 > 420.0:
                                raise TimeoutError("决策流式输出超过总时长上限（420s）")
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if not data or data == "[DONE]":
                                continue
                            chunk = json.loads(data)
                            # [2026-09-16 观测] usage 块可能在 choices 为空的
                            # 独立 chunk 中携带（OpenAI 兼容：末块 choices=[] +
                            # 顶层 usage），故必须在 choices 判定**之前**读取，
                            # 否则会被下方 `if not choices: continue` 跳过。
                            usage_chunk = chunk.get("usage")
                            if isinstance(usage_chunk, dict):
                                usage = usage_chunk
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            # [2026-09-10 可观测性] 捕获流结束原因——OpenAI 流
                            # 最后一个 chunk 携带（此前未读取，生产事故分辨不了
                            # 「max_tokens 截断 vs 模型畸形输出」的盲点补齐）；
                            # 只取字符串形态，非流式异常路径保持 None。
                            stream_finish = choices[0].get("finish_reason")
                            if isinstance(stream_finish, str) and stream_finish:
                                finish_reason = stream_finish
                            text = delta.get("content")
                            if isinstance(text, str) and text:
                                parts.append(text)
                                if on_chunk is not None and not suppress_stream:
                                    on_chunk(text)
                            reasoning = delta.get("reasoning_content")
                            if isinstance(reasoning, str) and reasoning:
                                reasoning_parts.append(reasoning)
                                if on_reasoning_chunk is not None and not suppress_stream:
                                    on_reasoning_chunk(reasoning)
            except ModelUnavailableError:
                raise
            except Exception as exc:
                if _should_retry_model_call(exc, bool(parts)) and network_retries_left > 0:
                    # 指数退避：第 1 次重试 1s、第 2 次重试 2s（每次重试前
                    # sleep）；429 有 Retry-After 时取 max(退避, Retry-After)。
                    # 预算独立于 JSON 解析重试预算（2026-09-01 结构修复）。
                    delay = 1 << (max_retries - network_retries_left)
                    retry_after = _extract_retry_after(exc)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    network_retries_left -= 1
                    attempt += 1
                    _log_model_retry("decision", attempt, exc, _http_status_of(exc), delay)
                    time.sleep(delay)
                    continue
                # 最终失败（不可重试或已用尽）：脱敏日志定位失败类型，再 fail closed
                _log_model_failed(
                    "decision",
                    exc,
                    _http_status_of(exc),
                    int((time.monotonic() - t0) * 1000),
                )
                raise ModelCallError(
                    "模型调用失败（超时/网络/解析）：未执行任何操作，请稍后重试"
                ) from exc
            if not parts:
                empty_err = ModelCallError(
                    "模型结构化输出解析失败（流式输出为空）：未执行任何操作"
                )
                _log_model_failed(
                    "decision",
                    empty_err,
                    None,
                    int((time.monotonic() - t0) * 1000),
                    # [2026-09-10 可观测性] 空输出附 finish_reason（length=
                    # 输出预算耗尽/截断，stop=模型侧返回空；不可得时为 None
                    # 自动省略字段）
                    finish_reason=finish_reason,
                )
                raise empty_err
            # [2026-09-10 可观测性] 解析候选文本（去 ```json 围栏后）与
            # _coerce_decision 前的 JSON 快照：中间件会补 reply_text
            # （call_tool/needs_input 缺失时）——快照的顶层键名反映模型
            # 原始输出形态，ValidationError 日志用（只记键名不记内容）。
            parse_text = _strip_json_fence("".join(parts))
            parsed_snapshot: Any = None
            try:
                data = json.loads(parse_text)
                parsed_snapshot = dict(data) if isinstance(data, dict) else None
                result = AgentDecision.model_validate(_coerce_decision(data))
            except (ValueError, TypeError) as exc:
                # [2026-08-28 JSON 解析有限重试] 语法级畸形（HTTP 200 但内容非
                # JSON）为模型侧偶发，重试命中率高；总尝试 6 次以内（额外重试
                # _JSON_PARSE_MAX_RETRIES 次）。重试轮抑制流式回调（前端已收到
                # 首轮全部文本，不重放）。语义级畸形（model_validate 校验失败，
                # 如 tool_name 缺失）与空输出仍 fail closed 不重试。
                if _is_json_parse_error(exc) and json_retries_left > 0:
                    json_retries_left -= 1
                    attempt += 1
                    # [2026-09-10 可观测性] 重试日志附 finish_reason（定稿
                    # 口径的 head/tail 等特征只落最终失败那条，重试不重复）
                    _log_model_retry(
                        "decision", attempt, exc, None, 1, finish_reason=finish_reason
                    )
                    time.sleep(1)
                    continue
                parse_err = ModelCallError(
                    "模型结构化输出解析失败（返回非 AgentDecision 形态）：未执行任何操作"
                )
                # [2026-09-10 可观测性] 最终失败附脱敏诊断特征（2026-09-09
                # 定稿口径）：语法级=字节数/ends_with_brace/首尾各 20 字符；
                # 语义级=loc+type 摘要/顶层键名集合（不记原文）。行为零变化
                # ——只加日志字段。
                _log_model_failed(
                    "decision",
                    exc,
                    None,
                    int((time.monotonic() - t0) * 1000),
                    **_parse_failure_features(
                        exc,
                        parse_text,
                        parsed=parsed_snapshot,
                        finish_reason=finish_reason,
                    ),
                )
                raise parse_err from exc
            # [2026-09-16 观测] 成功事件：模型决策此前不落库不落日志（只在
            # checkpoint 的 pending_action、并被 tools 节点消费后清空），
            # 事后无法知道模型当时选了什么工具/什么 next_action。此处只加
            # 日志，不改任何行为、不落 reply 原文。
            # [2026-09-16 观测加固] 观测日志绝不影响主链路：本调用点在裸函数
            # 体内、任何 try/except 之外——若日志自身抛异常（非 ModelCallError），
            # 会逃过 model_node 的 ``except ModelCallError`` 直接炸穿整轮对话。
            # 包住调用点：日志异常降级为「静默 + 事件缺失」，不改变返回值、不外抛。
            try:
                _log_decision_succeeded(
                    result,
                    int((time.monotonic() - t0) * 1000),
                    attempt,
                    thread_id=thread_id,
                    task_id=task_id,
                    usage=_usage_fields(usage),
                )
            except Exception:  # noqa: BLE001 — 观测降级，禁止影响主链路
                logger.debug("model_call_succeeded_log_failed", tool_path="decision")
            return result

    def generate_tool_arguments(
        self,
        *,
        system_prompt: str,
        messages: list,
        schema_instruction: str,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolArguments:
        """[2026-08-24 二次改造] 工具参数生成（tool_args 节点第二轮调用）。

        与 ``generate_decision`` 完全同构：历史 sanitize → schema 说明注入
        最后一条 human 消息 → system 首条判定 → invoke → ToolArguments
        类型检查；失败统一抛 ``ModelCallError``（含未配置的
        ``ModelUnavailableError``，上层一处捕获），异常消息不携带敏感信息。

        [2026-08-25 畸形输出评估] 不挂 ``_coerce_decision`` 同类中间件：
        参数轮无零语义损失修复点——tool_name 缺失/null（补哪个工具名？）与
        tool_arguments 缺失/null/非 dict（补什么参数？）均为**语义数据**，
        修复即编造 → 畸形保持 fail closed（ModelCallError），与决策轮
        中间件设计原则一致；畸形预防靠 ``build_args_schema`` 提示强化
        （tool_name 必填非空，禁止 null）。

        [2026-08-26 日期注入] system_prompt 追加当前日期行（服务器本地
        时间），模型可解析「7月」「上周」等相对时间语义。

        [2026-09-10 可观测性] 最终失败日志附脱敏解析特征（语法级=
        output_bytes/ends_with_brace/head/tail，语义级=loc+type 摘要/
        顶层键名集合；详见模块 docstring）。langchain invoke 无流式
        chunk，finish_reason 不可得（字段省略）。行为零变化。
        """
        system_prompt = f"{system_prompt}\n\n{_current_date_line()}"
        # [2026-08-28 瞬时故障有限重试] invoke 路径无流式正文（无 parts，
        # parts_emitted 恒 False）；可重试判定与决策轮同构
        # （_should_retry_model_call：langchain 侧 openai.APIConnectionError
        # / APITimeoutError / HTTP 429 限速 / 5xx；401/403/400/404 配置参数
        # 问题不重试）。每次尝试重新构造消息与 invoke（从零开始）；重试用尽
        # 仍抛 ModelCallError（fail closed，上层契约不变）。
        # [2026-08-28 JSON 解析有限重试] 参数轮（langchain json_mode invoke）
        # 语法级 JSON 解析失败（JSONDecodeError 或 OutputParserException
        # 包装）另走独立重试预算（json_retries_left 从
        # _JSON_PARSE_MAX_RETRIES 递减，总尝试 6 次以内）；参数轮无流式正文，
        # 重试无重放问题。语义畸形（ToolArguments 类型校验失败）仍 fail
        # closed 不重试。
        # [2026-09-01 重试预算结构修复] 与决策轮同构：for(attempt in
        # range(max_retries+1)) 改为 while True + 双预算——瞬时故障
        # （network_retries_left，默认 2 次）与语法级 JSON 解析失败
        # （json_retries_left 从 _JSON_PARSE_MAX_RETRIES 递减，总尝试 6 次
        # 以内）互相独立、各自用尽才 fail closed（修复 JSON 重试被外层迭代数
        # 锁死为 3 次的结构 bug）。attempt 为共享重试计数，仅用于脱敏日志。
        max_retries = max(0, int(self._settings.model_call_max_retries))
        json_retries_left = _JSON_PARSE_MAX_RETRIES
        network_retries_left = max_retries
        attempt = 0
        t0 = time.monotonic()
        # [2026-08-28 JSON 解析有限重试] result 预置 None（2026-09-01 结构
        # 调整后仅防御：循环只经 break 成功路径或 raise 退出）——下方
        # isinstance(result, ToolArguments) 检查正常走 fail closed
        # （ModelCallError + 脱敏日志），避免未绑定 NameError。
        result = None
        while True:
            try:
                chat = self._ensure_args_chat()
                conv = self._with_schema_instruction(
                    self._sanitize_tool_messages(list(messages)), schema_instruction
                )
                # system 首条已存在于历史时不再重复注入（system_prompt 与历史分离）
                first = conv[0] if conv else None
                first_type = first[0] if isinstance(first, tuple) else getattr(first, "type", None)
                payload = conv if first_type == "system" else [("system", system_prompt), *conv]
                result = chat.invoke(payload)
            except ModelUnavailableError:
                raise
            except Exception as exc:
                # [2026-08-28 JSON 解析有限重试] 参数轮（langchain json_mode invoke）
                # 解析失败同样偶发（JSONDecodeError 或 OutputParserException 包装）；
                # 语法级畸形重试（总尝试 6 次以内），语义畸形（ToolArguments 类型校验
                # 失败）仍 fail closed。参数轮无流式正文，重试无重放问题。
                if _is_json_parse_error(exc) and json_retries_left > 0:
                    json_retries_left -= 1
                    attempt += 1
                    _log_model_retry("tool_arguments", attempt, exc, None, 1)
                    time.sleep(1)
                    continue
                if _should_retry_model_call(exc, False) and network_retries_left > 0:
                    # 指数退避：第 1 次重试 1s、第 2 次重试 2s（每次重试前
                    # sleep）；429 有 Retry-After 时取 max(退避, Retry-After)。
                    # 预算独立于 JSON 解析重试预算（2026-09-01 结构修复）。
                    delay = 1 << (max_retries - network_retries_left)
                    retry_after = _extract_retry_after(exc)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    network_retries_left -= 1
                    attempt += 1
                    _log_model_retry(
                        "tool_arguments", attempt, exc, _http_status_of(exc), delay
                    )
                    time.sleep(delay)
                    continue
                # 最终失败（不可重试或已用尽）：脱敏日志定位失败类型，再 fail closed
                _log_model_failed(
                    "tool_arguments",
                    exc,
                    _http_status_of(exc),
                    int((time.monotonic() - t0) * 1000),
                    # [2026-09-10 可观测性] 与决策轮同口径的解析失败脱敏诊断
                    # 特征：输出文本取自 OutputParserException.llm_output，
                    # ValidationError 经 __cause__ 解包（loc+type 摘要 + 顶层
                    # 键名）；本路径为 langchain invoke 无流式 chunk，
                    # finish_reason 不可得（字段省略）。
                    **_parse_failure_features(exc, _output_text_of(exc)),
                )
                raise ModelCallError(
                    "模型调用失败（超时/网络/解析）：未执行任何操作，请稍后重试"
                ) from exc
            break  # invoke 成功：退出重试循环
        if not isinstance(result, ToolArguments):
            type_err = ModelCallError(
                "模型结构化输出解析失败（返回非 ToolArguments 形态）：未执行任何操作"
            )
            _log_model_failed(
                "tool_arguments", type_err, None, int((time.monotonic() - t0) * 1000)
            )
            raise type_err
        # [2026-09-16 观测] 参数轮成功事件：只读工具（erp_query/erp_calc 等）
        # 的查询条件此前完全无记录——正是"参数口径错"最需要的事后证据。
        # 只加日志，不改任何行为。（本路径为 langchain invoke，取不到流式
        # usage，usage 参数不传。）
        # [2026-09-16 观测加固] 同决策轮：调用点在裸函数体内、try/except 之外，
        # 日志异常若逃出会被 tool_args_node 的 ``except ModelCallError`` 漏过、
        # 炸穿整轮。包住调用点：降级为「静默 + 事件缺失」，返回值与外抛行为不变。
        try:
            _log_args_succeeded(
                result,
                int((time.monotonic() - t0) * 1000),
                attempt,
                thread_id=thread_id,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001 — 观测降级，禁止影响主链路
            logger.debug("model_call_succeeded_log_failed", tool_path="tool_arguments")
        return result

    def _ensure_summary_chat(self) -> Any:
        """[2026-08-24 定稿] 懒构造压缩调用客户端。

        裸 ChatOpenAI + max_tokens=compression_output_max_tokens +
        with_structured_output(SummaryResult, method="json_mode")；
        配置校验复用 ``_ensure_configured``（key/base_url 缺失 →
        ModelUnavailableError），构造失败 → ModelCallError。
        """
        self._ensure_configured()
        if self._summary_chat is None:
            try:
                effort = self._settings.model_reasoning_effort
                # 2026-08-28 off 档：与参数轮同构——off 时 extra_body 透传
                # thinking: {"type": "disabled"} 显式关闭思考，不传
                # reasoning_effort（langchain_openai 1.6 + openai SDK 3.x
                # 下 model_kwargs 顶层合并会被严格签名拒绝，须走 extra_body）。
                extra_body = {"thinking": {"type": "disabled"}} if effort == "off" else None
                self._summary_chat = ChatOpenAI(
                    model=self._settings.model_name,
                    base_url=self._settings.model_base_url,
                    api_key=self._settings.model_api_key,
                    timeout=60.0,  # 同步编排：总超时上限，避免无限挂起
                    max_retries=0,  # fail closed：不静默重试掩盖失败
                    max_tokens=self._settings.compression_output_max_tokens,
                    extra_body=extra_body,
                    # 2026-08-27 思考强度：显式注入 model_reasoning_effort
                    # （默认 medium；空串=不传该参数，保持 API 默认）
                    reasoning_effort=None if effort in ("", "off") else effort,
                ).with_structured_output(SummaryResult, method="json_mode")
            except Exception as exc:
                raise ModelCallError(
                    "模型客户端初始化失败（压缩调用连接/参数错误）：未执行任何操作"
                ) from exc
        return self._summary_chat

    def generate_summary(self, *, messages: list) -> SummaryResult:
        """[2026-08-24 定稿] 记忆压缩调用：COMPRESSION_PROMPT（replace 注入
        summary_max_chars）+ 待压段消息（("human"/"ai", content) 元组形态，
        无需 sanitize——压缩调用不做工具调用）。

        失败统一抛 ``ModelCallError``（含未配置的 ``ModelUnavailableError``），
        异常消息不携带敏感信息（14.4 脱敏口径）；上层（压缩节点）捕获后降级。
        """
        try:
            chat = self._ensure_summary_chat()
            prompt = COMPRESSION_PROMPT.replace(
                "{summary_max_chars}", str(self._settings.summary_max_chars)
            )
            result = chat.invoke([("system", prompt), *messages])
        except ModelUnavailableError:
            raise
        except Exception as exc:
            raise ModelCallError(
                "会话记忆压缩调用失败（超时/网络/解析）：已降级为历史截断"
            ) from exc
        if not isinstance(result, SummaryResult):
            raise ModelCallError(
                "压缩结构化输出解析失败（返回非 SummaryResult 形态）：已降级为历史截断"
            )
        return result
