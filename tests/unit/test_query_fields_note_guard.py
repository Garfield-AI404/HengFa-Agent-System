"""[2026-09-28 §3.1 字段盲区] 自选 fields 的「未取到 ≠ 不存在」守护测试。

背景（实测三处同源）：模型自选 fields 时，结果与「该字段不存在」**形状完全
相同**——钣金订单写入时同一条 customers 查询在运行之间摇摆（30 次全字段 /
8 次窄，仅 id+company_name）；塑粉写入时去 powder_inventory（无单价字段）
翻价 → 0 行 → 断「没有历史价」；订单更新时 notes 不在回传白名单 → 读不到被
读成「不可写」。缺口有两块：**A** 规则 1 后半句在鼓励收窄（「显式指定时只
返回所需字段」）；**B** 结果侧完全没有回显「本次只返回了什么」。

本轮两处落点：

- 落点一：规则包正文 `R4.query.md` 规则 1——anchors「未出现的字段」与禁令
  「不得据此推断…不存在」须**绑在同一无句读跨度内**；
- 落点二：结果回注 `fields_note`（query.py）——实际文本（经真实调用路径取
  到，非复制粘贴）须含「未取到」且**不含**「不存在」，且「显式传 fields 才
  出现、不传不出现、0 行也出现」的行为判据。

本文件刻意避开**单点裸子串**断言（本仓「prompt 守护测试假锁」教训：``assert
"余额" in text`` 会被邻近同字词救活）。对「方向 / 绑定」类判据改用
**多词逐字连续子串**——**宽松正则反而是假锁**：``才[^。；\n]{0,10}收窄`` 的
0~10 字空隙足够塞进否定词（``才不显式收窄``）或把方向词换掉（``过大→过小``），
故「整句还在」不等于「语义没被反转」。

**有意偏离「同一小句」纪律（理由）**：``_rule_containing`` 取的是**整条规则
行**（会跨行内的 ``；``），因为注入的最小单位是整条编号规则，整行删任意一半
都该失配。但**关键绑定断言**用的是**跨不过 `；` 的逐字连续子串**
（``只有结果体量明显过大时才显式收窄`` / ``优先缩小 filter/时间窗，不要先砍
字段``），其内部只有 `，`、无 `。；`，故「锚点与后果同处一无句读跨度」的纪律
在**断言层**仍然成立；只有 ``默认不传`` 这类单词硬钉子是整行存在性检查。
"""

from __future__ import annotations

import re

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache

POLICY_KEY = "R4.query"


def _clauses(text: str) -> list[str]:
    """按句末标点/换行切小句（与「锚点与后果同处一个小句」的判据同构）。"""
    return [c for c in re.split(r"[。；\n]", text) if c.strip()]


def _rule_lines(text: str) -> list[str]:
    """按换行切**规则行**（每行 = 一条编号规则；规则行内的 ；/。 不算切分）。"""
    return [ln for ln in text.split("\n") if ln.strip()]


def _rule_containing(text: str, need: str) -> str:
    return next(ln for ln in _rule_lines(text) if need in ln)


def _field(name: str, ftype: str = "str", **extra) -> dict:
    return {"type": ftype, "label": name, **extra}


#: customers 的最小字段目录（覆盖本用例用到的字段；贴近 ERP 注册表）
_ENTITIES: list[dict] = [
    {
        "name": "customers",
        "label": "客户",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "company_name": _field("公司名称"),
            "unit_price": _field("单价", "decimal"),
            "price_type": _field("价格类型"),
            "price_unit": _field("计价单位"),
            "notes": _field("备注"),
            "created_at": _field("创建时间", "datetime"),
        },
    },
]


class _FakeErp:
    """最小假 ERP：list_entities 元数据 + query 按请求 fields 裁剪返回行。

    query 的行裁剪模拟 ERP 契约：传了 fields 就回这些字段（否则回全字段），
    且**恒补回主键 id**——所以「行键 ≠ 请求键」。这是刻意的：``fields_note``
    有行分支声称列出的是**实际返回**的字段名，若实现偷懒写成 ``list(fields)``
    （请求键）就测不出差别；恒补 id 后「请求 company_name → 行含 id」使实现
    必须读行键、而非请求键，本模块据此能抓到该退化。
    """

    def __init__(self, full_row: dict | None = None, empty: bool = False) -> None:
        self.calls: list[str] = []
        self._full_row = full_row or {
            "id": 9,
            "company_name": "示例客户",
            "unit_price": "12.50",
            "price_type": "普通",
            "price_unit": "元/kg",
            "notes": "n",
            "created_at": "2026-09-01T00:00:00",
        }
        self._empty = empty

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": list(_ENTITIES)})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        self.calls.append(tool_name)
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            if self._empty:
                return ToolResult(ok=True, data={"items": [], "total": 0})
            args = arguments or {}
            fields = args.get("fields")
            if fields:
                # 模拟 ERP 恒补回 id：行键 = 请求键 ∪ {id}
                row = {
                    k: v
                    for k, v in self._full_row.items()
                    if k in fields or k == "id"
                }
            else:
                row = dict(self._full_row)
            return ToolResult(ok=True, data={"items": [row], "total": 1})
        raise AssertionError(f"unexpected tool: {tool_name}")


def _ctx(erp: _FakeErp) -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}),
        ),
        tasks=None,
        audit=None,
        sink=None,
        client=erp,
        metadata=EntityCache(erp),
    )


def _query(erp: _FakeErp, **kwargs) -> dict:
    result = run_erp_query(_ctx(erp), ErpQueryArgs(**kwargs))
    assert result.ok is True, result.summary
    return result.summary


# ---------------------------------------------------------------------------
# 落点一：规则包正文（绑定式判据，锚点与后果不得被句读切开）
# ---------------------------------------------------------------------------


def test_r4_query_pins_unseen_field_not_inferred_absent() -> None:
    """R4.query 规则 1：「未出现的字段」与禁令「不得据此推断…不存在」必须
    绑在**同一无句读跨度**内——拆句（。；）即失配。

    靶子：把「不得据此推断该字段不存在或不可写」删掉、或改成「可据此推断该
    字段不存在」，本用例即红。
    """
    clauses = _clauses(policy_for(POLICY_KEY).text)
    assert any(
        re.search(r"未出现的字段[^、。；\n]{0,30}不得据此推断", c) for c in clauses
    ), f"R4.query 缺「未出现的字段 … 不得据此推断」同句（不含句读跨度）：{clauses}"
    # 「未取到」语义也必须在（与结果侧 fields_note 同口径，不得出现「不存在」读法）
    assert any("未取到" in c for c in clauses), clauses


def test_r4_query_pins_default_no_fields_and_narrow_only_when_huge() -> None:
    """R4.query 规则 1 的**正面指引**（缺口 A 的修复半边）必须被钉住——光有
    「未取到≠不存在」的禁令不够：一次「精简措辞」把「默认不传 / 只有结果体量
    明显过大时才收窄 / 优先缩 filter 时间窗而非砍字段」整句删掉，禁令还在、
    收窄诱导却原样退回。

    判据（取**整条规则行**——注入的最小单位，整行删任意一半即失配）：
    - 硬钉子：锚点「默认不传」必须存在（常态）；
    - **方向 + 条件 + 动作三者绑定**用**逐字连续子串**
      ``只有结果体量明显过大时才显式收窄`` 钉死——方向词（过大）、条件词
      （才）、动作词（收窄）必须同处该连续串，故：
        * MA「过大→过小」方向反转 → 失配（红）；
        * MC「才显式收窄→才不显式收窄」动作取反 → 失配（红）。
    - **本守护有意「以钉词形换抓方向反转」**：代价是**纯同义改写会误报**
      （``只有结果体量明显过大时才显式收窄`` → ``仅当返回体量明显偏大时才
      收窄字段`` 会红）。本仓已有结论「钉词形与钉语义不可兼得」，这是**有意
      选择**，不是缺陷。
    - **不放反向断言 ``"不收窄" not in line`` / ``"不显式收窄" not in line``**
      （2026-09-28 复审删除）：它把「出现否定词」等同于「方向被反转」，而
      「不收窄」正是本批立意本身（默认不传、别先砍字段）→ 语义同向的合法
      追加（``正常体量的结果不收窄字段。`` / ``无需收窄时也不显式收窄字段。``）
      也会被误报成红；其增量检出力（「保住连续串、另处补否定」）已被上游
      连续子串覆盖。**后来者勿以为此处少了一条防线而把它加回来。**
    - **第二半句的绑定不许解除**：``优先缩小 filter/时间窗，不要先砍字段``
      必须作**单个连续子串**——原实现是两个独立 ``in``（不要求相邻/同句），
      MD 那种把「优先缩小」挪进声明已废弃的小句、把「不要先砍字段」另置的
      改写照样通过（假锁），此处必须整段连续且顺序不变。

    靶子：删整句 → 全部断言红；只删第二半句 → 第三条红；
    MA（方向）/ MC（动作）/ MD（解绑）三条语义变异 → 至少本条红。
    """
    line = _rule_containing(policy_for(POLICY_KEY).text, "查询字段")
    # (1) 硬钉子：默认不传（常态）
    assert "默认不传" in line, f"规则 1 缺「默认不传」：{line}"
    # (2) 方向（过大）+ 条件（才）+ 动作（收窄）逐字绑定的连续子串；
    #     MA 换方向词、MC 在「才」后插否定词，均使该连续串失配。
    assert "只有结果体量明显过大时才显式收窄" in line, (
        f"规则 1 缺「只有结果体量明显过大时才显式收窄」的方向/条件/动作绑定"
        f"（过大→过小、才显式收窄→才不显式收窄 均会失配）：{line}"
    )
    # (2b) 【2026-09-28 复审删除的反向断言，勿加回】
    #      曾在此处写 ``assert "不收窄" not in line and "不显式收窄" not in line``
    #      作为「防换位式取反」的补充防线。已删除，理由（复审要求记账）：
    #        * 它把「出现否定词」当成「方向被反转」（本仓「极性盲」族），而
    #          「不收窄」正是本批立意本身 → 语义同向的合法追加
    #          （``正常体量的结果不收窄字段。`` / ``无需收窄时也不显式收窄字段。``）
    #          也会被误报成红；实测这两条追加均应绿。
    #        * 其增量检出力很窄：真正的取反（MA 方向 / MC 动作）已被 (2) 的逐字
    #          连续子串 ``只有结果体量明显过大时才显式收窄`` 挡下；而「保住该连续
    #          串、另处补一句否定」这一形态**接受不被检出**——它与本批的合法措辞
    #          （「不收窄」「不要先砍字段」）同形，属极性盲族的固有代价，不是"已覆盖"。
    #      本守护的取舍：**钉词形换抓方向反转**，代价是纯同义改写会误报
    #      （上串 → ``仅当返回体量明显偏大时才收窄字段`` 会红）。本仓已有结论
    #      「钉词形与钉语义不可兼得」，这是有意选择，不是缺陷。
    # (3) 第二半句必须整段连续且顺序不变（绑定不许解除）
    assert "优先缩小 filter/时间窗，不要先砍字段" in line, (
        f"规则 1 缺「优先缩小 filter/时间窗，不要先砍字段」的连续绑定"
        f"（把「优先缩小」与「不要先砍字段」拆开或换序即失配）：{line}"
    )


def test_r4_query_does_not_encourage_narrowing_fields_first() -> None:
    """反向：规则 1 不得再退回旧措辞「显式指定时只返回所需字段，不返回与需求
    无关的字段」（那正是鼓励收窄的根因）。改回即红。"""
    text = policy_for(POLICY_KEY).text
    assert "显式指定时只返回所需字段" not in text, "规则 1 退回「显式指定时只返回所需字段」"
    assert "不返回与需求无关的字段" not in text, "规则 1 退回「不返回与需求无关的字段」"


# ---------------------------------------------------------------------------
# 落点二：fields_note 的实际文本（经真实调用路径，非复制粘贴）
# ---------------------------------------------------------------------------


def test_fields_note_wording_has_not_retrieved_not_absent() -> None:
    """有行路径：``fields_note`` 实际文本须含「未取到」且**不含**「不存在」。"""
    payload = _query(
        _FakeErp(),
        entity="customers",
        fields=["id", "company_name"],
    )
    assert "fields_note" in payload, payload
    note = payload["fields_note"]
    assert "未取到" in note, note
    assert "不存在" not in note, note


def test_fields_note_wording_on_empty_result() -> None:
    """0 行路径：``fields_note`` 也必须含「未取到」且不含「不存在」。"""
    payload = _query(
        _FakeErp(empty=True),
        entity="customers",
        fields=["unit_price"],
    )
    assert "fields_note" in payload, payload
    note = payload["fields_note"]
    assert "未取到" in note, note
    assert "不存在" not in note, note


# ---------------------------------------------------------------------------
# 落点二：行为判据（出现条件）
# ---------------------------------------------------------------------------


def test_fields_note_present_and_lists_actual_returned_fields_not_requested() -> None:
    """显式 fields 且**实际返回键 ≠ 请求键**时，fields_note 必须列出**行里的
    实际键集合**，而非请求的 fields。

    本假 ERP 恒补回主键 id：请求 ``fields=["company_name"]`` 但行是
    ``{id, company_name}``。若实现偷懒写成 ``returned = list(fields)``（请求
    键），清单里就没有 id → 本用例红。这正是该键 docstring「列出实际返回字段」
    所声称的能力，必须被真正测到。
    """
    payload = _query(
        _FakeErp(),
        entity="customers",
        fields=["company_name"],
    )
    note = payload["fields_note"]
    assert "id、company_name" in note, note  # 行键序（full_row 序）
    assert "unit_price" not in note, note  # 未返回的字段不得进清单


def test_fields_note_absent_when_fields_not_given() -> None:
    """不传 fields → **不含** fields_note（避免每轮都涨上下文）。"""
    payload = _query(_FakeErp(), entity="customers")
    assert "fields_note" not in payload, payload


def test_fields_note_present_on_zero_rows_lists_effective_sent_fields() -> None:
    """显式 fields 且 0 行 → 仍含 fields_note，并列出**过滤后实际下发的**收窄
    字段集（这些才是本次真正请求 ERP 的字段）。"""
    payload = _query(
        _FakeErp(empty=True),
        entity="customers",
        fields=["unit_price", "price_type"],
    )
    assert "fields_note" in payload, payload
    note = payload["fields_note"]
    assert "unit_price" in note and "price_type" in note, note


def test_fields_note_absent_when_all_requested_fields_unknown_and_zero_rows() -> None:
    """P1：模型传一个**元数据里没有**的字段且本次 0 行 → **不得**出现
    fields_note。

    否则同一 payload 内自相矛盾：note 说「已忽略未知字段: bogus」，fields_note
    却又列出 ``bogus`` 并反向断言「不代表 ERP 没有该字段」——本批立意是「未取到
    ≠ 不存在」，不能对已被判定为未知的字段名说「不代表没有」。
    """
    payload = _query(
        _FakeErp(empty=True),
        entity="customers",
        fields=["bogus"],
    )
    assert "note" in payload and "已忽略未知字段" in payload["note"], payload
    assert "fields_note" not in payload, payload


def test_fields_note_on_zero_rows_excludes_unknown_fields() -> None:
    """P1（混合形态）：未知字段 + 可回传字段 + 0 行 → fields_note 只列**过滤后
    实际下发**的字段，未知字段名不得出现在 fields_note 里（不许反向断言
    「不代表 ERP 没有 bogus」）。"""
    payload = _query(
        _FakeErp(empty=True),
        entity="customers",
        fields=["bogus", "unit_price"],
    )
    assert "fields_note" in payload, payload
    note = payload["fields_note"]
    assert "unit_price" in note, note
    assert "bogus" not in note, note


def test_payload_unchanged_keys_when_fields_not_given() -> None:
    """不传 fields 且 0 行 → 键集与旧输出一致（仅 entity/count/items/
    total/empty_note），fields_note 不得凭空出现或顶替既有键。"""
    payload = _query(_FakeErp(empty=True), entity="customers")
    assert set(payload) == {"entity", "count", "items", "empty_note"}, payload
