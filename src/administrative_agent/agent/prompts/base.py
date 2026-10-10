"""固定基础 System Prompt（10.1 九条正文 + 5 个固定追加段）。

每次模型调用都注入；业务细则不在此处，由规则包层（policies）条件式
注入最小规则包（10.2 两层结构）。

本模块正文的**历次改动（改了什么、为什么）见 ``docs/debug-log.md``**
（更新记录不再写在代码层，该文档不进 git、本地可查）。

**改正文改 md，不改代码**：本模块只剩加载器与 fail-closed 校验，改一条约束
不需要再处理 Python 字面量的隐式拼接、逗号与缩进。正文改动属于**改模型输入、
不是排版**——纯重构必须以**逐字节基线比对**验收（测试全绿看不见字节级偏差，
见 ``docs/debug-log.md`` 的实测教训），基线取最近一次**内容定稿**版。

**本模块条号可自由重排**：工具描述与本模块之间已无跨文件编号耦合，两者
一致性由 ``tests/unit/test_prompts.py`` 的
``test_tool_exclusivity_agrees_between_base_and_tool_descriptions`` 强制。

``base_prompt.md`` 的格式（三种行、两条拼接规则）::

    # <段标识>            ← 起一个新段；段标识是固定枚举，见 _SEGMENT_KEYS
    ## <子段标题>          ← 仅在首段内，渲染为【子段标题】
    N. <条目>             ← 仅在首段内，渲染为 "N. <条目>"（编号原样渲染）

- **首段**（``base_system_prompt``）用 ``## `` 划分子段、``N. `` 划条目；
  条目必须**一行一个**（续行不受支持，见下）；编号必须从 1 连续递增。
- **其余段**是整段纯文本：段内非空行按 ``""`` 直接拼接（等价于 Python 的
  隐式字符串连接），故**在哪里断行都不改变渲染结果**——这是「文件能排版
  而不改变模型可见文本」的关键，也是这些长段能按标点折行的前提。

拼接按 ``""`` 而非 ``"\\n"``，是因为原 Python 字面量就是隐式拼接；若改成
按行用换行拼接，每一段都会凭空多出换行，等于悄悄改写了注入给模型的文本。

两条 fail-closed 规则（都在 import 期抛 ``RuntimeError``）：

1. **段内行首尾不得有空白**——按 ``""`` 拼接时首尾空白会被 ``strip`` 吃掉，
   在空格处折行就会静默少一个空格。需要空格请写在行中间。
2. **首段条目不得用续行**——同理，续行会在折行点丢空格。宁可报错，
   也不要静默改文本；将来真要支持折行，得先定好「空格归属」的语义。

本模块末尾的 ``REPLY_FORMAT_INSTRUCTION`` 是组装层（model_node）追加的
回复格式引导，与其余段同源（都在 md 里），仅注入位置不同。

条件注入（RAG 长期记忆正文）::

    {{#rag}} …长期记忆正文… {{/rag}}      ← 仅当 RAG 已配置时注入

正文里可（在行内或整段）用 ``{{#rag}}`` / ``{{/rag}}`` 括起**只应在 RAG
已配置时注入**的文字。装载器按 RAG 是否配置产出两份正文：

- **已配置态**（``_SOURCE``）：剔除标记、**保留**括起的正文——与加标记前
  **逐字节相同**（标记本就不进模型可见文本）；
- **未配置态**（``_SOURCE_NO_RAG``）：连标记带正文一并摘除。

为何要条件化：RAG 未配置时装配层会经 ``without_unconfigured_rag_tools`` 把
``rag_*`` 工具从运行时工具集剔除（模型看不到），若正文仍无条件教它「先
rag_recall 再回答」，模型会被告知使用一个手里没有的工具，转向编造。故正文
与工具的可见性必须同源（同一判据 ``rag_client is None``）。

标记语法刻意极浅（只有一对布尔标记、不嵌套），且**fail closed**：标记不成对
/ 嵌套 / 有多余的关闭标记 → import 期 ``RuntimeError``（与段解析同款），
绝不静默渲染出与作者意图不符的 system prompt。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 固定正文文件（随代码包分发；与 policy_texts/、skill_texts/ 同款机制）。
#: 目录缺失/文件缺失 → RuntimeError（fail closed，import 即失败，不在运行期
#: 静默降级成空提示词——一个空的 base system prompt 不会让任何测试变红）。
BASE_PROMPT_PATH: Path = Path(__file__).parent / "base_texts" / "base_prompt.md"

#: 段标识枚举（代码枚举；md 里出现未注册的段 → RuntimeError）。
#: 顺序即本文件底部的声明顺序，与注入位置无关（注入顺序由 model_node 决定：
#: base → 业务背景 → 查证规则 → 确认机制 → 对账流程 → … → 回复格式）。
_SEGMENT_KEYS: tuple[str, ...] = (
    "base_system_prompt",
    "business_context",
    "lookup_first_rule",
    "confirmation_mechanism_rule",
    "reconcile_workflow_rule",
    "reply_format_instruction",
)

_SEGMENT_PREFIX = "# "
_SECTION_PREFIX = "## "

#: 首段条目行（``N. 正文``；编号原样渲染进 system prompt）
_ITEM_PREFIX: re.Pattern[str] = re.compile(r"^(\d+)\.\s+(.+)$")


def _normalize(text: str) -> str:
    """CRLF / 裸 CR 归一为 LF。

    仓库在 Windows 上开发，git 的 autocrlf 会把检出的 md 变成 CRLF。正文会
    原样进 system prompt，带 ``\\r`` 的提示词没必要多担一分风险，故读写两侧
    一律归一（这是本模块唯一一处「规范化用户可见文本」的地方）。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_source() -> str:
    """读取正文文件。缺失/不可读 → RuntimeError（fail closed）。"""
    try:
        return _normalize(BASE_PROMPT_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(
            f"固定正文文件缺失或不可读（fail closed）：{BASE_PROMPT_PATH}（{exc}）"
            "——固定基础 System Prompt 是每次模型调用的必注入段，缺失时不能"
            "静默降级成空串"
        ) from exc


def _parse_segments(raw: str) -> dict[str, list[str]]:
    """按 ``# <段标识>`` 切段，返回 段标识 → 段内原始行列表。

    ``_SEGMENT_KEYS`` 之外的段、重复段、缺失段一律 RuntimeError。段标记之前
    的内容（文件头的说明注释）不进任何段，被忽略。
    """
    segments: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith(_SEGMENT_PREFIX) and not stripped.startswith(
            _SECTION_PREFIX
        ):
            key = stripped[len(_SEGMENT_PREFIX):].strip()
            if key not in _SEGMENT_KEYS:
                raise RuntimeError(
                    f"base_prompt.md 出现未注册的段 {key!r}（fail closed；"
                    f"合法段标识={list(_SEGMENT_KEYS)}）——新段必须先在 "
                    "_SEGMENT_KEYS 登记，否则它不会被导出、正文等于没写"
                )
            if key in segments:
                raise RuntimeError(
                    f"base_prompt.md 段 {key!r} 重复出现（fail closed）——"
                    "同名段会被静默丢弃前一段，正文等于少了一半"
                )
            segments[key] = []
            current = key
            continue
        if current is not None:
            segments[current].append(line)

    missing = [key for key in _SEGMENT_KEYS if key not in segments]
    if missing:
        raise RuntimeError(
            f"base_prompt.md 缺少段：{missing}（fail closed）——这些段每次模型"
            "调用都注入，缺失即约束静默消失"
        )
    return segments


def _build_sections(
    lines: list[str],
) -> tuple[tuple[str, tuple[tuple[int, str], ...]], ...]:
    """把首段的行解析为 ((子段标题, ((编号, 条目)…)), …)。

    编号**原样取自 md**，渲染时照用——md 是文本与编号的共同真源，与本文件
    无关。两条 fail-closed 规则见模块 docstring：条目不得用续行、编号须从 1
    连续递增（后者保证「第 N 条」式引用与文件里看到的一致）。
    """
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_SECTION_PREFIX):
            sections.append((stripped[len(_SECTION_PREFIX):].strip(), []))
            continue
        if not sections:
            raise RuntimeError(
                f"base_prompt.md 首段在第一个子段标题（'## …'）之前出现内容 "
                f"{stripped[:40]!r}（fail closed）——该段的每一条都必须落在"
                "某个子段内，否则会被渲染成无标题的孤立条目"
            )
        match = _ITEM_PREFIX.match(stripped)
        if not match:
            raise RuntimeError(
                f"base_prompt.md 首段条目 {stripped[:40]!r} 未以 'N. ' 编号开头"
                "（fail closed）——**条目必须一行一个、续行不受支持**：续行两侧"
                "的空白会被 strip，在空格处折行就会静默吃掉一个空格、"
                "悄悄改写模型可见文本。若要折行，需先定义空格归属语义"
            )
        sections[-1][1].append((int(match.group(1)), match.group(2).strip()))

    if not sections:
        raise RuntimeError("base_prompt.md 首段为空或没有子段标题（fail closed）")

    numbers = [num for _, items in sections for num, _ in items]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(
            f"base_prompt.md 首段条目编号必须从 1 连续递增，实际={numbers}"
            f"（fail closed）——编号原样渲染进 system prompt，断号/重号会让"
            "模型看到与条数不符的编号，也会让「第 N 条」式引用悄悄指错条。"
            "在中间插入/删除一条后，请把该子段起往后的编号顺次改写"
        )
    for title, items in sections:
        if not items:
            raise RuntimeError(
                f"base_prompt.md 子段【{title}】没有任何条目（fail closed）——"
                "空的子段标题会渲染成一个只有标题没有内容的段"
            )
    return tuple((title, tuple(items)) for title, items in sections)


def _join_raw(key: str, lines: list[str]) -> str:
    """整段纯文本：非空行按 ``""`` 直接拼接（等价 Python 隐式字符串连接）。

    行首尾空白 → RuntimeError：按 ``""`` 拼接时它会被吃掉，在空格处折行就会
    静默少一个空格（见模块 docstring 的 fail-closed 规则 1）。
    """
    for line in lines:
        if line != line.strip():
            raise RuntimeError(
                f"base_prompt.md 段 {key!r} 的行 {line[:30]!r} 首尾有空白"
                "（fail closed）——段内行按 '' 直接拼接，首尾空白会被吃掉；"
                "需要空格请写在行中间，折行点必须落在两侧都非空白处"
            )
    return "".join(line for line in lines if line)


def _render_base_system_prompt(
    sections: tuple[tuple[str, tuple[tuple[int, str], ...]], ...],
) -> str:
    """渲染固定基础 System Prompt：逐子段输出「【标题】+ 编号条目」。

    **编号取 md 里手写的号，不重新编号**——模板是 1..N 连续（import 期已校验），
    故渲染出的是 1..10 的全局连号（跨子段连续），与迁移前逐字节相同。
    （写成「段内位置 +1」会渲染成 1,2 / 1,2 / …，子段间重复编号——
    2026-09-16 迁移时逐字节比对正是靠这一点抓到的。）
    条号按编号索引回 ``BASE_PROMPT_RULES``：两者由连续的 1..N 保证一致。
    段间空一行便于模型区分层次。
    """
    blocks: list[str] = []
    for title, items in sections:
        lines = [f"【{title}】"]
        lines.extend(f"{num}. {item}" for num, item in items)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


#: RAG 条件注入标记（见模块 docstring）：``{{#rag}}`` … ``{{/rag}}`` 之间的
#: 正文仅当 RAG 已配置时进入模型可见文本。
_RAG_OPEN: str = "{{#rag}}"
_RAG_CLOSE: str = "{{/rag}}"


def _resolve_rag_conditionals(raw: str, *, rag_configured: bool) -> str:
    """按 RAG 是否配置解算 ``{{#rag}}``/``{{/rag}}`` 标记。

    - ``rag_configured=True``：只剔除标记本身，括起的正文原样保留——结果与
      加标记前逐字节相同（标记不进模型可见文本，故不是「改正文」）。
    - ``rag_configured=False``：标记与其括起的正文一并摘除。

    **fail closed**（与段解析同款，import 期抛 ``RuntimeError``）：标记不配对、
    嵌套、或出现多余的关闭标记时立即报错——一个写错的标记会让正文「悄悄地」
    进/不进提示词，而少一条约束/多一段误导不会让任何测试自己变红。
    """
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(raw):
        if raw.startswith(_RAG_OPEN, i):
            if depth:
                raise RuntimeError(
                    f"base_prompt.md 出现嵌套的 {_RAG_OPEN}（fail closed）——"
                    "条件标记只支持一对、不可嵌套，写错会静默改变注入内容"
                )
            depth = 1
            i += len(_RAG_OPEN)
            continue
        if raw.startswith(_RAG_CLOSE, i):
            if not depth:
                raise RuntimeError(
                    f"base_prompt.md 出现多余的 {_RAG_CLOSE}（fail closed）——"
                    "关闭标记必须与前面的开启标记配对"
                )
            depth = 0
            i += len(_RAG_CLOSE)
            continue
        if depth and not rag_configured:
            i += 1
            continue
        out.append(raw[i])
        i += 1
    if depth:
        raise RuntimeError(
            f"base_prompt.md 的 {_RAG_OPEN} 未闭合（fail closed）——条件标记"
            "必须成对出现，缺关闭标记会让后续正文被误判归属"
        )
    return "".join(out)


#: 原始正文（含条件标记）。
_RAW_SOURCE: str = _read_source()
#: 已配置态正文（标记剥离、RAG 正文保留）——与加标记前逐字节相同。
_SOURCE: str = _resolve_rag_conditionals(_RAW_SOURCE, rag_configured=True)
#: 未配置态正文（RAG 正文连同标记一并摘除）。
_SOURCE_NO_RAG: str = _resolve_rag_conditionals(_RAW_SOURCE, rag_configured=False)

_SEGMENTS: dict[str, list[str]] = _parse_segments(_SOURCE)
_BASE_SECTIONS: tuple[tuple[str, tuple[tuple[int, str], ...]], ...] = _build_sections(
    _SEGMENTS["base_system_prompt"]
)
#: 未配置态的分段（base 首段与查证规则段的正文不同，其余段逐字节相同）。
_SEGMENTS_NO_RAG: dict[str, list[str]] = _parse_segments(_SOURCE_NO_RAG)
_BASE_SECTIONS_NO_RAG: tuple[tuple[str, tuple[tuple[int, str], ...]], ...] = _build_sections(
    _SEGMENTS_NO_RAG["base_system_prompt"]
)

#: 10.1 九条固定内容，扁平元组（顺序 = md 中的出现顺序；编号 = 位置 +1，
#: 因 import 期已校验手写编号就是 1..N，故位置号与手写号恒等）。
#: 原为 2026-08-23 的十条 + 2026-09-10 新增第 11 条；2026-09-16 第三刀因合并
#: 「写操作唯一入口」三条为一条、并把权限边界拆出独立成条而净减为十条。
#: 当日第四刀迁入 md——**正文改动请改 base_prompt.md**，本常量只读。
#: [2026-09-16 用户正文定稿] 删去原第 2 条「当前用户角色由系统 context 提供，
#: 不从对话猜测。」，其后各条号整体前移一位，净为九条（用户拍板：整条删除，
#: 角色来源约束不再写进基础层）。
#:
#: 词元守护见 ``tests/unit/test_prompts.py::test_base_prompt_required_contents``
#: （15 个词元 + 旧工具名负向 pin）。
BASE_PROMPT_RULES: tuple[str, ...] = tuple(
    item for _, items in _BASE_SECTIONS for _, item in items
)

BASE_PROMPT_COUNT: int = len(BASE_PROMPT_RULES)

#: 段结构：（段标题, (条索引…)）。**导出为兼容别名，不再是真源**——
#: 2026-09-16 第四刀后由 md 的 ``## `` 子段标题派生（索引按全段连续位置算）。
#: 第一刀时它是手工维护的映射表 + ``_validate_sections`` import 期覆盖校验；
#: 正文进 md 后「漏条/重条」在结构上已不可能（每条必然落在某个子段下），
#: 那套校验随之删除——这是解耦带来的净减法，不是丢了保护。
#: 段名与段序仍由 ``test_base_prompt_sections_structure_pinned`` 钉住。
BASE_PROMPT_SECTIONS: tuple[tuple[str, tuple[int, ...]], ...] = tuple(
    (title, tuple(range(offset, offset + len(items))))
    for title, items, offset in (
        (title, items, sum(len(i) for _, i in _BASE_SECTIONS[:idx]))
        for idx, (title, items) in enumerate(_BASE_SECTIONS)
    )
)

#: 渲染后的固定基础 System Prompt（分段 + 编号条目）
BASE_SYSTEM_PROMPT: str = _render_base_system_prompt(_BASE_SECTIONS)

#: 未配置 RAG 时的固定基础 System Prompt——与 ``BASE_SYSTEM_PROMPT`` 仅差
#: 第 6 条里 ``{{#rag}}…{{/rag}}`` 括起的长期记忆从句（其余逐字节相同）。
BASE_SYSTEM_PROMPT_UNCONFIGURED: str = _render_base_system_prompt(
    _BASE_SECTIONS_NO_RAG
)


def base_system_prompt_for(rag_configured: bool) -> str:
    """按 RAG 是否配置选基础首段（未配置时摘除长期记忆从句）。

    组装层（``model_node`` 决策轮 / ``tool_args_node`` 参数轮）用它取正文，
    **判据与工具剔除同源**（``rag_client is None``，见 ``without_unconfigured_
    rag_tools``）——正文的可见性必须与工具一致，否则会出现「工具摘了、正文还在」
    （模型被教用一个不存在的工具）或反之。
    """
    return BASE_SYSTEM_PROMPT if rag_configured else BASE_SYSTEM_PROMPT_UNCONFIGURED

#: 业务背景（2026-08-26 与项目负责人对齐的业务共识，独立段；不属 10.1 固定条）。
#: 模型据此理解我司业务闭环与订单规则；[2026-09-08 塑粉到货语义] 增
#: 【塑粉业务】段：供货商送货到货的口语 = 创建塑粉订单，9010 等纯数字是色号
#: 非钣金型号）
BUSINESS_CONTEXT: str = _join_raw("business_context", _SEGMENTS["business_context"])

#: 信息查证规则（2026-08-26 与项目负责人对齐：模型先自查 ERP 补全信息，
#: 减少向用户追问；独立段，不属 10.1 固定条；2026-08-28 增第 6 条员工名单
#: 条款：考勤类操作先查 employees 确认全员名单，不向用户索要名单；
#: 2026-09-01 增第 7 条参数型反问禁止；2026-09-02 增第 8 条收支方向不反问
#: ——方向属用户口语语义，决策轮工具表 description 同口径引导；
#: [2026-09-08 口径修正] 第 2 条改为按客户 ERP 主数据确定，不查历史订单推断；
#: [2026-09-08 塑粉到货语义] 第 4 条重写：口语到货表述（来了/到了/送来 +
#: 塑粉/色号）直接判定 powder 创建，9010 等色号不得当钣金型号查金属历史；
#: 客户/供货商同名（如“昌盛”）时按订单类型选对应实体，不反问是哪边；[复审
#: 补充] 无钣金词清单加钣金/喷/上色，并加对照句——给钣金件喷涂塑粉上色
#: （“这批钣金要喷 9010”）属 metal 钣金加工语境，不建 powder 订单）；
#: [2026-09-10 审计 P2-18] 新增第 9 条：汇总一律走 erp_calc 由代码计算、
#: 写 fields/filter/measures 前字段名/枚举不确定先 list_entities 查看）
#: [2026-09-16 用户正文定稿] 该条**去掉字面条号**，改为接在第 8 条之后的无
#: 编号尾段（汇总/工资口径作通用收尾约束）。本条是 raw 段，字面数字即模型
#: 可见文本，改此处措辞/编号都属于**改模型输入**，不是排版。
LOOKUP_FIRST_RULE: str = _join_raw("lookup_first_rule", _SEGMENTS["lookup_first_rule"])

#: 未配置 RAG 时的信息查证规则——与 ``LOOKUP_FIRST_RULE`` 仅差末尾
#: ``{{#rag}}…{{/rag}}`` 括起的四个长期记忆子块（【长期记忆口径】【写与忘】
#: 【召回时机】【分寸】），其余查证规则逐字节相同。
LOOKUP_FIRST_RULE_UNCONFIGURED: str = _join_raw(
    "lookup_first_rule", _SEGMENTS_NO_RAG["lookup_first_rule"]
)


def lookup_first_rule_for(rag_configured: bool) -> str:
    """按 RAG 是否配置选查证规则段（未配置时摘除长期记忆子块）。"""
    return (
        LOOKUP_FIRST_RULE if rag_configured else LOOKUP_FIRST_RULE_UNCONFIGURED
    )

#: 确认机制说明（2026-08-27 对齐：写工具调用后由系统确认卡承载确认，
#: 模型不在文本中请求用户确认；独立段，不属 10.1 固定条）。
#: [2026-08-30 撤回考勤免确认] 考勤（attendance_plan_execute）与其他写工具
#: 同路径：原 2026-08-28 让考勤写入直接执行、无确认卡，因“免确认 × 全员覆盖”
#: 叠加风险撤回，现全部写工具均需用户在系统确认卡上确认）
#: [2026-09-10 审计 P2-17] 写工具清单补 mapping_template（全仓 7 个 is_write
#: 工具齐全）
CONFIRMATION_MECHANISM_RULE: str = _join_raw(
    "confirmation_mechanism_rule", _SEGMENTS["confirmation_mechanism_rule"]
)

#: 对账流程引导（2026-08-31 对话对账批次 3；独立段，不属 10.1 固定条）。
#: 紧跟确认机制段之后、工具索引表之前注入——对账两段式（只读分析 →
#: 确认后写入）的流程约束与 fail closed 红线，以及 [附件：…] 标记语义）
RECONCILE_WORKFLOW_RULE: str = _join_raw(
    "reconcile_workflow_rule", _SEGMENTS["reconcile_workflow_rule"]
)

#: 回复格式引导（不属 10.1 固定条；组装层 model_node 追加的独立段，引导模型
#: 回复主动使用 Markdown 结构——前端 ChatPage 已用 react-markdown 渲染）。
#: [2026-09-09 紧凑排版] 末段追加禁冗余空行硬约束：生产反馈「行与行/行与表格
#: 之间大片空白」系模型输出风格（markdown 习惯性连续空行），CSS 留白已归零
#: 仍复现，故直接在提示词层禁止（边界 ≤1 空行、列表/表格内 0 空行；pin 见
#: tests/unit/test_prompts.py）
#: [2026-09-10 审计 P0-3/P1-7/P1-9/P3-34/P3-35] 明细阈值 400→250 行（决策轮
#: max_tokens=16384 约合 357 行上限，400 行必截断）、明细全列限定为 erp_query
#: 明细查询、汇总直接给 erp_calc 聚合值（normalized/divisor）、隔轮可见性与
#: query_id 回查、客户名称字段用 company_name/未知字段 note 提示、空行约束
#: 合并为一句
#: [2026-09-10 P2-5] id 口径消歧：id 仅可用于映射失败时的占位/定位（如
#: 「客户ID 16」），不得作为表格列/正文数据输出；原「订单号等业务标识照常
#: 显示」改为 ERP 实际存在的业务标识（客户名称、型号/色号、入库日期）
#: ——metal_warehouse/powder_warehouse 无 order_no 字段。
#: [2026-09-12] 收口名称显示：删去「映射取不到时注明「客户ID 16」作为占位」
#: 这一无条件逃生口，改为「确已查过该 id 对应的 customers/suppliers 且确实
#: 查不到」才可占位、未查过不得声称取不到；同时补进 erp_query 工具
#: description（模型决策/作答时最高频阅读的文本）。
REPLY_FORMAT_INSTRUCTION: str = _join_raw(
    "reply_format_instruction", _SEGMENTS["reply_format_instruction"]
)
