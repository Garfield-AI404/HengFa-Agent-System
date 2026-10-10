"""[2026-09-13 skill 层接入] skill loader / 渲染 / 注入单测。

覆盖四块：

1. **加载器 fail closed 全分支**（缺 frontmatter 起止行、缺必填字段、
   skill_key 与文件名不一致、policies/tools 越界引用、native_tools 非空、
   index/when_not 超长、正文为空）——全部 ``RuntimeError``（import 即失败）；
2. **渲染**：hash 稳定性与敏感性、text 重新编号、索引表一行一 skill、
   schema 追加段、无 skill 时渲染为空串；
3. **``AgentDecision.skill_name``**：字段缺席 = null（宽松侧，见 model_client
   注释里的爆炸半径论证）、model_dump 携带该字段；
4. **注入与覆盖语义**：决策轮 system prompt 的索引表/激活正文/引用规则包正文、
   未注册 skill 退化为常规对话、``active_skill`` last-write-wins（不声明即退出，
   且在**下一轮**生效）、call_tool 路径同样写入；
5. **参数轮注入**（tool_args 节点）：只给编排段不给口径段（工具族已注入口径，
   重叠规则包在同一 prompt 里**出现恰好一次**）、**工具隶属门**（本轮工具须
   ∈ skill 的 ``tools``，表外工具即模型已跑偏 → 不注入）、未激活/幻觉键退化为
   常规、注入不扰动 ``pending_action`` 的其余字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.model_client import AgentDecision
from administrative_agent.agent.nodes.model_node import _build_system_prompt, model_node
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.prompts import skills
from administrative_agent.agent.prompts.policies import POLICY_REGISTRY
from administrative_agent.agent.prompts.skills import (
    ALL_SKILL_KEYS,
    SKILL_ADMISSION_MAX_CHARS,
    SKILL_REGISTRY,
    SkillPackage,
    _parse_md,
    render_index,
    render_schema_instruction,
    skill_for,
)
from administrative_agent.agent.tools import TOOL_REGISTRY

SKILL_KEY = "SK.financial_report"
RECEIVABLES_KEY = "SK.receivables"
TAX_KEY = "SK.tax_estimate"
SALARY_KEY = "R1.salary.calculation"
QUERY_KEY = "R4.query"


def _body(
    *,
    skill_key: str = SKILL_KEY,
    title: str = "测试流程",
    index: str = "何时用本流程",
    policies: str = "R4.query",
    tools: str = "erp_query",
    when_not: str = "何时不要用",
    native_tools: str = "",
    version: str = "2026-09-13.1",
    steps: str = "1. 第一步。\n2. 第二步。\n",
    include_key: bool = True,
) -> str:
    """构造一份 skill 正文（默认合法；按需覆盖单个字段造非法样本）。"""
    head = ["---"]
    if include_key:
        head.append(f"skill_key: {skill_key}")
    head += [
        f"title: {title}",
        f"version: {version}",
        f"index: {index}",
        f"policies: {policies}",
        f"tools: {tools}",
        f"when_not: {when_not}",
        f"native_tools: {native_tools}",
        "---",
    ]
    return "\n".join(head) + "\n" + steps


def _write(tmp_path: Path, body: str, stem: str = SKILL_KEY) -> Path:
    path = tmp_path / f"{stem}.md"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 加载器正样本


class TestLoaderHappyPath:
    def test_financial_report_is_registered(self) -> None:
        assert ALL_SKILL_KEYS == frozenset({SKILL_KEY, RECEIVABLES_KEY, TAX_KEY})
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert pkg.title == "财务报表（当期净利润）"
        assert pkg.version == "2026-09-15.1"

    def test_receivables_is_registered(self) -> None:
        """[2026-09-16 当期应收口径开放] SK.receivables 已登记：9 步、引用
        R4.balance.scope、只挂 erp_calc/erp_query 两个只读工具。"""
        pkg = SKILL_REGISTRY[RECEIVABLES_KEY]
        assert pkg.title == "当期应收账款（按客户）"
        assert pkg.version == "2026-09-17.1"
        assert len(pkg.steps) == 9
        assert pkg.policies == ("R4.balance.scope",)
        assert pkg.tools == ("erp_calc", "erp_query")
        assert pkg.native_tools == ()

    def test_receivables_first_step_pins_two_month_offset(self) -> None:
        """SK.receivables 第 1 步（定期间）须钉住「到期月 = 当期月往前推 2 个月」
        这一账期口径：语义词（「到期月」）与数字 2 个月须**同处一个小句**。

        本仓此前对 SK.receivables 只断言 ``len(steps)``/``policies``/``tools``
        （见上一用例），**正文一字不校**——把第 1 步整段删掉、或把「2 个月」
        改成别的数字，全套测试仍绿。本条补上取数口径的内容守护（R4.balance.scope
        侧的同一口径由 test_balance_scope_policy.py 守护，skill 侧在此）。

        按本仓纪律：不逐字锚整句（正文会继续演进），只锚「语义词 + 数字 2 个月」
        同句。
        """
        step1 = SKILL_REGISTRY[RECEIVABLES_KEY].steps[0]
        clauses = [c for c in re.split(r"[。；\n]", step1) if c.strip()]
        assert any(
            "到期月" in c and re.search(r"往前\s*推?\s*2\s*个?\s*月", c)
            for c in clauses
        ), f"SK.receivables 第 1 步缺「到期月 = 当期月往前推 2 个月」同句口径：{clauses}"

    def test_receivables_total_caliber_excludes_negative_customers(self) -> None:
        """[2026-09-17 当期应收口径收口] SK.receivables 第 8 步的合计口径：
        负值户不计入合计、只把当期应收为正的客户相加，并另求负值户小计使闭合
        校验可核。

        守护两处「同句」判据（本仓「prompt 守护测试假锁」教训：锚点与后果必须
        落在**同一个小句**内，否则会被邻近同字词救活）：

        1. 「为正」与「不计入」同句——授权（只加正数）与其后果（负值户不计入）
           绑在同一个分句内；把「负值户按 0 计、不计入」拆出去后，剩余句里
           孤立出现「为正」不得救活本判据；
        2. 「负值户小计」与闭合式「= A 合计」必须绑在**不含顿号**的同一跨度内
           ——第 8 步四条校验以「、」相连、不切句，(a) 的「= A 合计」与 (d) 的
           「负值户小计」会天然落在同一个小句里；只断「同一个小句」会被 (a)
           救活（2026-09-17 复审实测：把 (d) 的闭合式掏空成「= 三项总计相减」、
           只留字面「负值户小计」，旧判据照样绿——本仓「prompt 守护测试假锁」
           的老毛病）。故用正则把锚点与后果锁进**同一段无「、。；」的跨度**，
           并要求同句另含 B 合计 与 S 合计，确认绑到的确实是 (d) 那条三项式；
           ``A`` 与 ``合计`` 之间按正文实际写法（有空格）用正则容忍空白。
        """
        clauses = [
            c
            for c in re.split(r"[。；\n]", SKILL_REGISTRY[RECEIVABLES_KEY].text)
            if c.strip()
        ]
        assert any("为正" in c and "不计入" in c for c in clauses), (
            "SK.receivables 缺「为正 … 不计入」同句的合计口径"
            f"（负值户不计入、只加正数）：clauses={clauses}"
        )
        assert any(
            re.search(r"负值户小计[^、。；\n]{0,40}?=\s*A\s*合计", c)
            and "B 合计" in c
            and "S 合计" in c
            for c in clauses
        ), (
            "SK.receivables 缺「负值户小计 … = A 合计 − B 合计 − S 合计」同句"
            f"（不含顿号跨度）的闭合校验式：clauses={clauses}"
        )

    def test_tax_estimate_is_registered(self) -> None:
        """SK.tax_estimate 已登记：10 步、不引用规则包、挂 4 个只读工具（含固定
        渲染器 render_tax_worksheet）。"""
        pkg = SKILL_REGISTRY[TAX_KEY]
        assert pkg.title == "当期增值税估算（含附加税）"
        assert pkg.version == "2026-09-19.1"
        assert len(pkg.steps) == 10
        assert pkg.policies == ()
        assert pkg.tools == (
            "erp_calc", "erp_query", "list_entities", "render_tax_worksheet",
        )
        assert pkg.native_tools == ()

    def test_tax_estimate_step4_binds_price_type_to_taxing(self) -> None:
        """SK.tax_estimate 第 4 步的含税/不含税/未知分桶口径：语义词与其后果必须
        落在**同一个小句**内。

        [本仓「prompt 守护测试假锁」教训] 不逐字锚整句；只锚「桶名 + 后果」同句：
        - 含税客户 → 销项桶，其收款「进入算税」；
        - 不含税客户 → 未计税桶，其收款「只列」「不计税」（授权与后果同句）；
        - 查不到主数据的客户 → 未知桶，其收款「只列」「不计税」。

        锚点用**桶名（销项桶/未计税桶/未知桶）**而非「含税/不含税」——「含税」
        是「不含税」的子串，用它会误命中（同 test_receivables 的老毛病）。
        """
        step4 = SKILL_REGISTRY[TAX_KEY].steps[3]
        assert step4.startswith("按 price_type 分三桶"), step4
        clauses = [c for c in re.split(r"[。；\n]", step4) if c.strip()]
        assert any("销项桶" in c and "进入算税" in c for c in clauses), (
            f"SK.tax_estimate 第 4 步缺「含税客户 → 销项桶 → 进入算税」同句口径：{clauses}"
        )
        assert any(
            "未计税桶" in c and "只列" in c and "不计税" in c for c in clauses
        ), (
            "SK.tax_estimate 第 4 步缺「不含税客户 → 未计税桶 → 只列不计税」同句"
            f"（授权与后果同句）：{clauses}"
        )
        assert any(
            "未知桶" in c and "只列" in c and "不计税" in c for c in clauses
        ), (
            "SK.tax_estimate 第 4 步缺「查不到主数据 → 未知桶 → 只列不计税」同句"
            f"（授权与后果同句）：{clauses}"
        )

    def test_tax_estimate_negative_lists_are_closed(self) -> None:
        """SK.tax_estimate 的封闭取数negative约束：不取的类目与其后果必须落在
        **同一个小句**内（否则「其余收款不取」被删掉或挪走仍会全绿）。

        - 收入侧：只取「钣金收款」，「其余收款」不取（不计税、也不出展示行）；
        - 支出侧：进项只取「塑粉付款」「电费」「天然气」，其它支出类目一律不取。
        """
        steps = SKILL_REGISTRY[TAX_KEY].steps
        step2 = steps[1]
        clauses2 = [c for c in re.split(r"[。；\n]", step2) if c.strip()]
        assert any("其余收款" in c and "不取" in c for c in clauses2), (
            f"SK.tax_estimate 第 2 步缺「其余收款 不取」同句口径：{clauses2}"
        )
        step8 = steps[7]
        clauses8 = [c for c in re.split(r"[。；\n]", step8) if c.strip()]
        assert any(
            "其它任何支出类目" in c and "不取" in c for c in clauses8
        ), f"SK.tax_estimate 第 8 步缺「其它支出类目 不取」同句口径：{clauses8}"

    def test_financial_report_step9_renders_even_when_period_empty(self) -> None:
        """[P0-a] SK.financial_report 第 9 步（出报告）的**交付义务**：「必须调用
        render_financial_report」与「当期为空/无记录**不是**跳过第 9 步的理由」
        必须落在**同一个小句**内（不跨 ``。；``）。

        背景（已取证）：第 9 步原本已有「当期无记录的表照样出并记 0」，但埋在
        300+ 字长段中段、未独立成条、未绑后果——实测模型仍以「收入侧为 0、
        报表参考意义有限」跳过第 9 步（FR07 原话级实证）。故本批把义务提成独立
        小句并绑后果；本条按本仓纪律（锚点与后果同句，否则会被邻近同字词救活）
        守护该绑定：``必须调用``（授权）、``render_financial_report``（工具）、
        ``为空``/``无记录``（触发条件）、``不是跳过``（后果）四者同句。
        """
        step9 = SKILL_REGISTRY[SKILL_KEY].steps[8]
        assert step9.startswith("出报告"), step9
        clauses = [c for c in re.split(r"[。；\n]", step9) if c.strip()]
        assert any(
            "必须调用" in c
            and "render_financial_report" in c
            and ("为空" in c or "无记录" in c)
            and "不是跳过" in c
            for c in clauses
        ), (
            "SK.financial_report 第 9 步缺「必须调用 render_financial_report」与"
            f"「为空/无记录不是跳过第 9 步的理由」同句的交付义务：clauses={clauses}"
        )

    def test_tax_estimate_step9_renders_even_when_period_empty(self) -> None:
        """[P0-a] SK.tax_estimate 第 9 步（出底稿）的**交付义务**：「必须调用
        render_tax_worksheet」与「当期为空/无记录**不是**跳过第 9 步的理由」
        必须落在**同一个小句**内（不跨 ``。；``）。

        背景（已取证）：税务第 9 步原本**连「照常出表」都没有**——实测模型以
        「无法生成有意义的底稿」跳过 render_tax_worksheet（TX04 原话级实证）。
        本条同 test_financial_report_step9（锚点与后果同句）守护该绑定。
        """
        step9 = SKILL_REGISTRY[TAX_KEY].steps[8]
        assert step9.startswith("出底稿"), step9
        clauses = [c for c in re.split(r"[。；\n]", step9) if c.strip()]
        assert any(
            "必须调用" in c
            and "render_tax_worksheet" in c
            and ("为空" in c or "无记录" in c)
            and "不是跳过" in c
            for c in clauses
        ), (
            "SK.tax_estimate 第 9 步缺「必须调用 render_tax_worksheet」与"
            f"「为空/无记录不是跳过第 9 步的理由」同句的交付义务：clauses={clauses}"
        )

    def test_manifest_lists_parsed_in_order(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert pkg.policies == (SALARY_KEY, "R4.query")
        assert pkg.tools == (
            "erp_calc", "erp_query", "list_entities", "erp_salary_calc",
            "render_financial_report",
        )
        assert pkg.native_tools == ()

    def test_steps_parsed_without_number_prefix(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert len(pkg.steps) == 10
        assert not pkg.steps[0].startswith("1.")
        assert pkg.text.splitlines()[0].startswith("1. ")

    def test_admission_combines_index_and_when_not(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert pkg.index in pkg.admission
        assert pkg.when_not in pkg.admission

    def test_parse_md_accepts_valid_sample(self, tmp_path: Path) -> None:
        pkg = _parse_md(_write(tmp_path, _body()))
        assert pkg.skill_key == SKILL_KEY
        assert pkg.steps == ("第一步。", "第二步。")
        assert pkg.policies == ("R4.query",)
        assert pkg.tools == ("erp_query",)
        assert pkg.native_tools == ()

    def test_variable_default_version_when_omitted(self, tmp_path: Path) -> None:
        body = _body().replace("version: 2026-09-13.1\n", "")
        assert _parse_md(_write(tmp_path, body)).version == skills.SKILL_VERSION


# ------------------------------------------------------- 加载器 fail closed 样本


class TestLoaderFailClosed:
    @pytest.mark.parametrize(
        ("body", "stem", "match"),
        [
            ("没有 frontmatter\n1. 步骤。\n", SKILL_KEY, "起始行"),
            (
                # 无结束行：frontmatter 里的行**都带冒号**——否则会先被
                # "既非空行也不含 ':'" 那道新检查拦下，本条覆盖不到结束行分支
                f"---\nskill_key: {SKILL_KEY}\ntitle: t\nindex: i\npolicies: R4.query\n",
                SKILL_KEY,
                "结束行",
            ),
            (_body(include_key=False), SKILL_KEY, "缺少"),
            (_body(title=""), SKILL_KEY, "缺少"),
            (_body(index=""), SKILL_KEY, "缺少"),
            (_body(skill_key="SK.other"), SKILL_KEY, "不一致"),
            (_body(policies="R4.query, R99.nope"), SKILL_KEY, "未注册的 policy_key"),
            (_body(tools="erp_query, no_such_tool"), SKILL_KEY, "未注册的工具"),
            (_body(native_tools="foo"), SKILL_KEY, "native_tools"),
            (
                _body(index="x" * (SKILL_ADMISSION_MAX_CHARS + 1)),
                SKILL_KEY,
                "index 超长",
            ),
            (
                _body(when_not="y" * (SKILL_ADMISSION_MAX_CHARS + 1)),
                SKILL_KEY,
                "when_not 超长",
            ),
            (_body(steps=""), SKILL_KEY, "正文为空"),
            # [reviewer 2026-09-13] frontmatter 三处"静默吞"的封堵
            (
                _body().replace("policies: R4.query", "policy: R4.query"),
                SKILL_KEY,
                "未知键",
            ),
            (
                _body().replace(
                    "policies: R4.query", "policies: R99.nope\npolicies: R4.query"
                ),
                SKILL_KEY,
                "重复声明",
            ),
            (
                _body().replace(
                    "tools: erp_query", "tools: erp_query\ntools: no_such_tool"
                ),
                SKILL_KEY,
                "重复声明",
            ),
            (
                _body().replace("when_not: 何时不要用", "when_not: 何时不要用\n  折行续写"),
                SKILL_KEY,
                "折行",
            ),
        ],
    )
    def test_parse_md_raises(
        self, tmp_path: Path, body: str, stem: str, match: str
    ) -> None:
        with pytest.raises(RuntimeError, match=match):
            _parse_md(_write(tmp_path, body, stem))

    def test_missing_directory_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(skills, "SKILL_TEXT_DIR", tmp_path / "不存在")
        with pytest.raises(RuntimeError, match="目录缺失"):
            skills._load_skill_texts()

    def test_unregistered_file_raises(self, tmp_path: Path, monkeypatch) -> None:
        # 期望键集内的文件齐全，只多一个未注册文件（否则先被"缺少"命中）
        (tmp_path / f"{SKILL_KEY}.md").write_text(_body(), encoding="utf-8")
        (tmp_path / f"{RECEIVABLES_KEY}.md").write_text(
            _body(skill_key=RECEIVABLES_KEY, tools="erp_query"), encoding="utf-8"
        )
        (tmp_path / f"{TAX_KEY}.md").write_text(
            _body(skill_key=TAX_KEY, tools="erp_query"), encoding="utf-8"
        )
        (tmp_path / "SK.extra.md").write_text(
            _body(skill_key="SK.extra"), encoding="utf-8"
        )
        monkeypatch.setattr(skills, "SKILL_TEXT_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="未注册流程包文件"):
            skills._load_skill_texts()

    def test_missing_expected_file_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(skills, "SKILL_TEXT_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="缺少流程包文件"):
            skills._load_skill_texts()


# ---------------------------------------------------------------------- 渲染


class TestRender:
    def test_text_renumbers_from_one(self) -> None:
        pkg = SkillPackage(
            skill_key="SK.t", title="t", steps=("甲。", "乙。", "丙。"), index="i"
        )
        assert pkg.text == "1. 甲。\n2. 乙。\n3. 丙。"

    def test_hash_stable_and_covers_text_and_version(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert pkg.hash == SKILL_REGISTRY[SKILL_KEY].hash
        changed_steps = SkillPackage(
            skill_key=pkg.skill_key,
            title=pkg.title,
            steps=pkg.steps[:-1] + ("改过的一步。",),
            index=pkg.index,
            version=pkg.version,
            policies=pkg.policies,
            tools=pkg.tools,
            when_not=pkg.when_not,
        )
        bumped = SkillPackage(
            skill_key=pkg.skill_key,
            title=pkg.title,
            steps=pkg.steps,
            index=pkg.index,
            version="2099-01-01.1",
            policies=pkg.policies,
            tools=pkg.tools,
            when_not=pkg.when_not,
        )
        assert changed_steps.hash != pkg.hash
        assert bumped.hash != pkg.hash

    def test_render_index_one_line_per_skill(self) -> None:
        text = render_index()
        assert len(text.splitlines()) == len(SKILL_REGISTRY)
        for key in ALL_SKILL_KEYS:
            assert f"- {key}：" in text

    def test_render_schema_instruction_declares_field_and_enum(self) -> None:
        text = render_schema_instruction()
        assert "skill_name" in text
        assert "null" in text
        for key in ALL_SKILL_KEYS:
            assert key in text

    def test_render_empty_when_no_skill_registered(self, monkeypatch) -> None:
        monkeypatch.setattr(skills, "SKILL_REGISTRY", {})
        assert render_index() == ""
        assert render_schema_instruction() == ""

    def test_skill_for_none_and_unknown_return_none(self) -> None:
        assert skill_for(None) is None
        assert skill_for("") is None
        assert skill_for("SK.nope") is None
        assert skill_for(SKILL_KEY) is SKILL_REGISTRY[SKILL_KEY]


# ------------------------------------------------- AgentDecision.skill_name 字段


class TestAgentDecisionField:
    def test_absent_equals_null(self) -> None:
        decision = AgentDecision(reply_text="好", next_action="reply")
        assert decision.skill_name is None

    def test_declared_value_is_kept_and_dumped(self) -> None:
        decision = AgentDecision(
            reply_text="好", next_action="reply", skill_name=SKILL_KEY
        )
        assert decision.model_dump()["skill_name"] == SKILL_KEY


# ------------------------------------------------------------------ 注入与覆盖


class _StubClient:
    """最小决策客户端桩（非 ModelDecisionClient 实例 → 不挂流式回调）。"""

    def __init__(self, decision: AgentDecision) -> None:
        self._decision = decision
        self.prompts: list[str] = []
        self.schemas: list[str] = []

    def generate_decision(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: object
    ) -> AgentDecision:
        self.prompts.append(system_prompt)
        self.schemas.append(schema_instruction)
        return self._decision


def _run_model_node(
    decision: AgentDecision, state_active: str | None = None
) -> tuple[dict, _StubClient]:
    client = _StubClient(decision)
    runtime = RuntimeBag(client=client, tools={})
    state: dict = {"messages": [], "remaining_steps": 5}
    if state_active is not None:
        state["active_skill"] = state_active
    return model_node(state, runtime), client


class TestInjection:
    def test_index_table_carried_by_schema_only_and_once(self) -> None:
        """索引表的**单一来源**是 `render_schema_instruction()`（决策轮由
        model_node 拼进 system prompt 末尾；末条 human 消息那份由 client 注入）。

        这里显式传入渲染好的 schema，模拟真实调用。断言索引在其中，且准入描述
        在**整条 prompt 里恰好出现一次**——reviewer 收口前 `_build_system_prompt`
        自己另起过一段"可用流程"，导致同一份索引在决策轮出现 3 次；这条计数断言
        就是防止那段被重新加回来。
        """
        runtime = RuntimeBag(tools={})
        schema = render_schema_instruction()
        first_step = SKILL_REGISTRY[SKILL_KEY].steps[0]
        admission = SKILL_REGISTRY[SKILL_KEY].admission

        inactive = _build_system_prompt(runtime, {"active_skill": None}, schema)
        assert SKILL_KEY in inactive  # 索引表恒定注入
        assert first_step not in inactive  # 未激活 → 正文缺席
        assert inactive.count(admission) == 1  # 索引只有一份

        active = _build_system_prompt(runtime, {"active_skill": SKILL_KEY}, schema)
        assert first_step in active
        assert active.count(admission) == 1  # 激活也不额外复制索引

    def test_no_separate_flow_index_segment(self) -> None:
        """反向哨兵：schema 为空串时，system prompt 里**不应**出现 skill_key
        ——说明本函数没有自己另起索引段（reviewer #1 的收口点）。"""
        text = _build_system_prompt(RuntimeBag(tools={}), {"active_skill": None}, "")
        assert SKILL_KEY not in text

    def test_active_skill_pulls_in_referenced_policy_text(self) -> None:
        runtime = RuntimeBag(tools={})
        active = _build_system_prompt(runtime, {"active_skill": SKILL_KEY}, "SCHEMA")
        assert POLICY_REGISTRY[SALARY_KEY].rules[0] in active
        assert SALARY_KEY in active

    def test_unknown_active_skill_degrades_to_plain_dialogue(self) -> None:
        runtime = RuntimeBag(tools={})
        text = _build_system_prompt(runtime, {"active_skill": "SK.nope"}, "SCHEMA")
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in text

    def test_schema_instruction_carries_skill_contract(self) -> None:
        out, client = _run_model_node(
            AgentDecision(reply_text="好", next_action="reply")
        )
        assert "skill_name" in client.schemas[0]
        assert out["active_skill"] is None

    def test_declared_skill_is_written(self) -> None:
        out, _ = _run_model_node(
            AgentDecision(reply_text="好", next_action="reply", skill_name=SKILL_KEY)
        )
        assert out["active_skill"] == SKILL_KEY

    def test_call_tool_path_also_writes_skill(self) -> None:
        out, _ = _run_model_node(
            AgentDecision(
                reply_text="正在处理",
                next_action="call_tool",
                tool_name="erp_calc",
                skill_name=SKILL_KEY,
            )
        )
        assert out["active_skill"] == SKILL_KEY
        assert out["messages"] == []

    def test_not_declaring_clears_skill(self) -> None:
        """覆盖语义：上一轮激活，本轮不声明 → 清空（生效于**下一轮**）。"""
        out, client = _run_model_node(
            AgentDecision(reply_text="好", next_action="reply"), state_active=SKILL_KEY
        )
        assert out["active_skill"] is None
        # 本轮 system prompt 仍带旧流程正文（声明在本轮决策之后才生效）
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] in client.prompts[0]

    def test_hallucinated_skill_is_ignored(self) -> None:
        out, client = _run_model_node(
            AgentDecision(reply_text="好", next_action="reply", skill_name="SK.nope")
        )
        assert out["active_skill"] is None
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]


# ------------------------------------------------------- 参数轮（tool_args）注入


@dataclass
class _ArgsResult:
    tool_name: str
    tool_arguments: dict


class _StubArgsClient:
    """最小参数客户端桩：只回 tool_name/tool_arguments，记录收到的 prompt。"""

    def __init__(self, tool_name: str = "erp_query", arguments: dict | None = None) -> None:
        self._tool_name = tool_name
        self._arguments = (
            arguments if arguments is not None else {"entity": "metal_orders"}
        )
        self.prompts: list[str] = []

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: object
    ) -> _ArgsResult:
        self.prompts.append(system_prompt)
        return _ArgsResult(self._tool_name, self._arguments)


def _run_tool_args_node(
    state_active: str | None, tool_name: str = "erp_query"
) -> tuple[dict, _StubArgsClient]:
    """跑参数轮。默认工具用 erp_query——它的 policy_family 恰是 R4.query，
    与财报 skill 引用的规则包**重叠**，是"双重注入"哨兵的最坏样本；且它在
    财报 skill 的 `tools` 表内（工具隶属门放行）。"""
    client = _StubArgsClient(tool_name)
    runtime = RuntimeBag(client=client, tools={tool_name: TOOL_REGISTRY[tool_name]})
    state: dict = {
        "messages": [],
        "pending_action": {
            "reply_text": "正在查",
            "next_action": "call_tool",
            "tool_name": tool_name,
        },
    }
    if state_active is not None:
        state["active_skill"] = state_active
    return tool_args_node(state, runtime), client


class TestParameterTurnInjection:
    def test_active_skill_puts_steps_into_parameter_prompt(self) -> None:
        _, client = _run_tool_args_node(SKILL_KEY)
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] in client.prompts[0]

    def test_inactive_skill_absent_from_parameter_prompt(self) -> None:
        _, client = _run_tool_args_node(None)
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]

    def test_hallucinated_skill_degrades_in_parameter_turn(self) -> None:
        _, client = _run_tool_args_node("SK.nope")
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]

    def test_referenced_policy_text_injected_once_not_twice(self) -> None:
        """回归哨兵：erp_query 的族与 skill 引用同为 R4.query——全量复用
        ``_render_skill_body``（include_policies 默认 True）会把同一份正文
        在这一个 prompt 里注入两遍，并给出两处「口径以此为准」。"""
        body = POLICY_REGISTRY[QUERY_KEY].text
        _, client = _run_tool_args_node(SKILL_KEY)
        assert client.prompts[0].count(body) == 1
        assert QUERY_KEY in client.prompts[0]  # 族注入确实发生过

    def test_decision_turn_still_carries_referenced_policy_text(self) -> None:
        """对照面：决策轮没有 policy_family，引用规则包正文必须照给。"""
        runtime = RuntimeBag(tools={})
        prompt = _build_system_prompt(runtime, {"active_skill": SKILL_KEY}, "SCHEMA")
        assert prompt.count(POLICY_REGISTRY[QUERY_KEY].text) == 1

    def test_arguments_and_other_pending_fields_preserved(self) -> None:
        out, _ = _run_tool_args_node(SKILL_KEY)
        assert out["current_step"] == "tool_args_done"
        assert out["pending_action"]["tool_arguments"] == {"entity": "metal_orders"}
        assert out["pending_action"]["reply_text"] == "正在查"
        assert out["pending_action"]["tool_name"] == "erp_query"

    def test_off_script_tool_hides_flow_in_parameter_turn(self) -> None:
        """reviewer 收口：本轮工具在 skill 的 `tools` 之外（模型已跑偏）→
        参数轮不注入流程编排段。

        场景：`active_skill` 是 last-write-wins、清空要下一轮才生效，所以存在
        "用户已从财报转向写操作、模型本轮忘了置 null"的窗口。此时若还无条件
        注入，同一条 prompt 里就既有财报正文的「本流程全程只读，不写任何
        数据」，又有填 `finance_create` 参数的要求 —— 自相矛盾。
        """
        _, client = _run_tool_args_node(SKILL_KEY, tool_name="finance_create")
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]

    def test_declared_tools_are_exactly_the_gate(self) -> None:
        """门的判据就是 frontmatter 的 `tools` 表本身（不是另抄一份白名单）。"""
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert pkg.tools, "本 skill 必须声明 tools，否则参数轮永不注入流程段"
        for tool in pkg.tools:
            _, client = _run_tool_args_node(SKILL_KEY, tool_name=tool)
            assert pkg.steps[0] in client.prompts[0], tool


# ---------------------------------------------------- 旧 checkpoint 重放兼容性


class TestLegacyCheckpointReplay:
    """[方案 §7 测试要点] 本次改动前落盘/在途的 checkpoint 里**没有**
    ``active_skill`` 键——重放时两个节点都必须照常工作（退化为常规对话/
    常规参数生成），不得 KeyError。``ConversationState`` 是 ``total=False``，
    且两处都走 ``state.get("active_skill")``，故安全；本类把这条不变量钉住，
    防止以后有人改成 ``state["active_skill"]`` 下标取值。
    """

    def test_model_node_tolerates_missing_key(self) -> None:
        client = _StubClient(AgentDecision(reply_text="好", next_action="reply"))
        runtime = RuntimeBag(client=client, tools={})
        state: dict = {"messages": [], "remaining_steps": 5}  # 故意不带 active_skill
        out = model_node(state, runtime)
        assert out["active_skill"] is None
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]
        assert SKILL_KEY in client.prompts[0]  # 索引表仍在（恒定注入）

    def test_tool_args_node_tolerates_missing_key(self) -> None:
        client = _StubArgsClient()
        runtime = RuntimeBag(client=client, tools={"erp_query": TOOL_REGISTRY["erp_query"]})
        state: dict = {
            "messages": [],
            "pending_action": {"reply_text": "查", "next_action": "call_tool", "tool_name": "erp_query"},
        }
        out = tool_args_node(state, runtime)
        assert out["current_step"] == "tool_args_done"
        assert SKILL_REGISTRY[SKILL_KEY].steps[0] not in client.prompts[0]
