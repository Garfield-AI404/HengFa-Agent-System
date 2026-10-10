"""记忆压缩摘要 prompt（``COMPRESSION_PROMPT``）的守护测试。

背景：摘要是**跨轮唯一的事实载体**——更早的 human/ai 被换成摘要，明细原文隔轮
被 ``model_node._filter_stale_details`` 整条删除，后续轮次只能信摘要（base prompt
恰好禁止凭记忆补写）。因此摘要 prompt 的保留项与负向约束直接决定「跨轮遗忘」
类失效是否复发，而仓库此前对这段 prompt **零断言**（改丢没有任何测试会红）。

写法纪律（见 memory ``prompt-text-guard-false-locks``）：**不写裸子串断言**。
裸 ``assert "xx" in text`` 会被邻近同字词救活（权威句被改掉仍全绿），本仓库一批次
复发过 4 次。改为**按 ``[。；\\n]`` 切小句，要求锚点与后果同处一个小句**——这样
「锚点还在、后果被删」的改动必然变红。凡断言的是「模型可见注入面」的，一律
经真实函数（``build_args_schema``）取值，不手抄字符串。
"""

from __future__ import annotations

import re

from administrative_agent.agent.model_client import COMPRESSION_PROMPT

#: 小句切分边界：句号/分号/换行。刻意**不含**逗号与顿号——同一小句内的逗号
#: 连接常是「锚点…，后果…」的正当形态（如「不推断、不补全」）。
_CLAUSE_SPLIT = re.compile(r"[。；\n]")


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(text) if c.strip()]


def _clauses_with(text: str, *tokens: str) -> list[str]:
    """返回**同时**含全部 tokens 的小句（空列表 = 锚点与后果已不在同一小句）。"""
    return [c for c in _clauses(text) if all(t in c for t in tokens)]


# ---------------------------------------------------------------------------
# 结构性前提：占位符与 json_mode 约束
# ---------------------------------------------------------------------------


def test_summary_max_chars_placeholder_present() -> None:
    """占位符必须在——它缺失时 ``.replace`` 静默变成 no-op。

    调用点（``generate_summary``）用
    ``COMPRESSION_PROMPT.replace("{summary_max_chars}", str(...))`` 注入上限；
    占位符一旦被改字，replace 不报错、只是什么都不做，模型**再也看不到**字符
    上限 ⇒ 摘要会写满并被压缩节点硬切（``[: settings.summary_max_chars]``），
    切在半句上、优先级机制失效。这是静默失效，故单独钉住。
    """
    assert "{summary_max_chars}" in COMPRESSION_PROMPT


def test_prompt_keeps_json_keyword_for_json_mode() -> None:
    """含 "json" 字样是 deepseek json_mode 的硬约束（与 build_select_schema 同纪律）。

    大小写不敏感：既有文案用大写 ``JSON``（生产一直可用），本约束只要求出现该词。
    """
    assert "json" in COMPRESSION_PROMPT.lower()


# ---------------------------------------------------------------------------
# 保留项与优先级
# ---------------------------------------------------------------------------


def test_top_priority_items_are_declared_non_droppable() -> None:
    """第 1、2 类必须**在同一个小句里**被声明为不得舍弃。

    摘要会被硬切成 ``summary_max_chars``，不排序则空间不足时模型随机裁——
    很可能裁掉用户诉求而留下流水账。故「不得舍弃」与「第 1、2 类」必须锁在一起：
    删掉后果句（不得舍弃）或改掉保护对象（第 1、2 类）都要变红。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "不得舍弃", "第 1、2 类")
    assert hits, "「不得舍弃」与「第 1、2 类」已不在同一小句——优先级保护失效"


def test_key_retention_classes_still_listed() -> None:
    """五类保留项不得被静默删除（尤其用户诉求与待办）。"""
    for token in (
        "用户的明确要求与偏好",
        "待办与未收口事项",
        "已查证的口径与映射",
        "已确认/已执行的操作与结果",
        "关键业务事实与数值",
    ):
        assert token in COMPRESSION_PROMPT, f"保留项被删：{token}"


def test_clarified_mapping_retention_names_the_concrete_keys() -> None:
    """第 3 类必须点出具体口径（客户/供货商 id 映射、型号归属），否则等于空话。

    这三点是**查证结论**、跨轮复用：丢了就要重查，而 base prompt 压制反问，
    模型不会问用户、只会重查或猜——是 D 类跨轮遗忘的主要来源。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "已查证的口径与映射", "客户/供货商名称与 id")
    assert hits, "第 3 类未点出具体的口径键（名称与 id 的对应）"


# ---------------------------------------------------------------------------
# 负向约束：不得臆造
# ---------------------------------------------------------------------------


def test_no_fabrication_constraint_binds_claim_to_transcript() -> None:
    """「只写上文确实出现过」必须与「不推断/不补全」同处一个小句。

    摘要是模型自由文本，会把不确定的数字写成确定的；而后续轮次只能当事实用
    （base prompt 禁止凭记忆补写）⇒ 错误被固化且看不出处。这是本约束存在的
    唯一理由，故锚点（不推断、不补全）与出处要求（上文）必须锁死在一起。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "不推断", "不补全", "上文")
    assert hits, "「不得臆造」类约束已与「以上文为准」脱钩——负向约束退化为空话"


def test_uncertain_items_must_be_marked_uncertain() -> None:
    """不确定须标「未确认」，不得写成已确认（与上一约束配套）。"""
    hits = _clauses_with(COMPRESSION_PROMPT, "未确认", "不得写成已确认")
    assert hits, "「把握不准的写未确认」约束缺失或与后果脱钩"


# ---------------------------------------------------------------------------
# index：回查路径必须可兑现
# ---------------------------------------------------------------------------


def test_index_must_carry_query_id_with_purpose() -> None:
    """index 必须带 query_id **并说明理由**（回查明细）。

    明细原文隔轮被整条删除，回查唯一路径是 ``memory_lookup(query_id)``；而
    ``model_node`` 回注摘要时已经承诺「按段号/query_id 回查」——prompt 不收集该
    标识，等于承诺无法兑现（回查路径跨轮实际断路）。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "query_id", "必须", "回查")
    assert hits, "index 未要求携带 query_id 或未说明用途——回查承诺落空"


def test_index_has_a_length_bound() -> None:
    """index 必须有**量级上限**，且与「回注进上下文」的后果同处一个小句。

    index 与摘要一起被回注进后续**每一轮**的 system prompt，所以它的长度不是
    一次性开销而是每轮固定开销。2026-09-17 实测（L1-2c 密集高优先级用例，同一
    输入跑 3 轮）：index 长度 27 / 50 / **731** 字，波动 27 倍——731 字那版等于
    每轮白烧一份目录，27 字那版几乎无检索价值。原文案只有「尽量简短」，没有量级。

    断言按 ``[。；\\n]`` 切小句，要求「index」「100 字」「超过」同处一小句：
    删掉量级数字或去掉约束词都要变红（裸子串会被邻近同字词救活）。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "index", "100 字", "超过")
    assert hits, "index 的长度上限缺失或与后果脱钩——回注开销重新失控"


def test_index_must_not_self_prefix_segment_no() -> None:
    """index 不得自带「【段N】」前缀：段号由回注处 ``f"【段{segment_no}】{index}"`` 添加。

    旧文案要求模型把「【段N】」写进 index，而代码又自动加一次 ⇒ 双前缀。
    """
    hits = _clauses_with(COMPRESSION_PROMPT, "【段N】", "不要", "段号由系统自动添加")
    assert hits, "index 的双前缀防护缺失（模型自写段号 + 代码自动加前缀）"
